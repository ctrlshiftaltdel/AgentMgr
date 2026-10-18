"""映射引擎测试（纯逻辑；§5.1 31 列 + §5.5 派生规则）。

覆盖 Issue #53 验收标准第 4 条：

* claim / lines / invoices → 分录行；金额口径复用 ``app.expense.money``；
* 日期 / 期间 / 凭证字 / 摘要 / 辅助核算按配置生成；
* 分录顺序固定（借方费用 → 税费 → 贷方），凭证号按（基准日、claim_id）编号；
* 科目无法解析 / 币别未登记 → fail closed。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.expense.money import MAX_AMOUNT
from app.export.errors import (
    CODE_ACCOUNT_CODE_UNRESOLVED,
    CODE_CURRENCY_UNMAPPED,
    ExportValidationError,
)
from app.export.mapping import (
    ClaimInput,
    EntryRow,
    InvoiceInput,
    LineInput,
    basis_date,
    build_entries,
    gross_total,
    net_total,
    tax_total,
)

from _helpers import make_config

PERIOD = "2026-10"


def _line(**overrides: object) -> LineInput:
    base: dict[str, object] = {
        "line_id": 1,
        "expense_date": date(2026, 10, 5),
        "category_code": "CAT-TRV",
        "category_name": "差旅",
        "description": "出差打车",
        "amount": Decimal("100.00"),
        "tax_amount": Decimal("0.00"),
        "department_name": "财务部",
    }
    base.update(overrides)
    return LineInput(**base)  # type: ignore[arg-type]


def _claim(**overrides: object) -> ClaimInput:
    base: dict[str, object] = {
        "claim_id": 1,
        "claim_no": "EC-00000001",
        "currency": "CNY",
        "status": "paid",
        "total_amount": Decimal("100.00"),
        "employee_no": "E0001",
        "lines": (_line(),),
        "invoices": (),
        "payment_amount": Decimal("100.00"),
        "payment_date": date(2026, 10, 20),
    }
    base.update(overrides)
    return ClaimInput(**base)  # type: ignore[arg-type]


# --- 基本投影 -----------------------------------------------------------------


def test_builds_debit_and_credit_rows(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    entries = build_entries([_claim()], config, period=PERIOD)

    assert len(entries) == 2
    debit, credit = entries
    assert debit.debit == Decimal("100.00")
    assert debit.credit is None
    assert credit.debit is None
    assert credit.credit == Decimal("100.00")
    assert {entry.account_code for entry in entries} == {"560101", "100201"}
    assert debit.summary == "EC-00000001 E0001 出差打车"
    assert credit.summary == "EC-00000001 E0001"
    assert debit.voucher_word == "记"
    assert debit.entry_date == date(2026, 10, 5)
    assert debit.currency_code == "RMB"
    assert debit.currency_name == "人民币"
    assert debit.exchange_rate == Decimal("1")
    assert debit.preparer == "E0001"
    assert debit.business_no == "EC-00000001"
    assert debit.attachment_count == 0


def test_cells_expand_to_31_columns(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (debit,) = build_entries([_claim()], config, period=PERIOD)[:1]
    cells = debit.cells()
    assert len(cells) == 31
    # 工具列一律留空（G/L/M/W..AB/AE）。
    for index in (6, 11, 12, 22, 23, 24, 25, 26, 27, 30):
        assert cells[index] is None
    # A–F, H–K, N, P, AC, AD 有值。
    assert cells[0] == "记"
    assert cells[1] == 1
    assert cells[2] == date(2026, 10, 5)
    assert cells[4] == "560101"
    assert cells[13] == Decimal("100.00")
    assert cells[28] == "EC-00000001"
    assert cells[29] == 0


# --- 进项税拆分（D3） ---------------------------------------------------------


def test_no_split_keeps_gross_in_expense_account(tmp_path: Path) -> None:
    config = make_config(tmp_path, tax_split="none")
    entries = build_entries(
        [_claim(lines=(_line(amount=Decimal("106.00"), tax_amount=Decimal("6.00")),))],
        config,
        period=PERIOD,
    )
    assert len(entries) == 2
    assert entries[0].debit == Decimal("106.00")


def test_split_all_creates_tax_row_and_is_balanced(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, tax_split="all", tax_account_code="22210101", tax_account_name="进项税额"
    )
    entries = build_entries(
        [
            _claim(
                total_amount=Decimal("106.00"),
                payment_amount=Decimal("106.00"),
                lines=(_line(amount=Decimal("106.00"), tax_amount=Decimal("6.00")),),
            )
        ],
        config,
        period=PERIOD,
    )
    assert len(entries) == 3
    expense, tax, credit = entries
    assert expense.debit == Decimal("100.00")
    assert tax.account_code == "22210101"
    assert tax.debit == Decimal("6.00")
    assert credit.credit == Decimal("106.00")
    assert sum(e.debit or Decimal("0") for e in entries) == sum(
        e.credit or Decimal("0") for e in entries
    )


def test_split_categories_only_splits_listed(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        tax_split="categories",
        tax_account_code="22210101",
        tax_split_categories=["CAT-TRV"],
        expense_map={"CAT-TRV": "560101", "CAT-MEAL": "560102"},
        expense_name_map={"CAT-TRV": "管理费用-差旅费", "CAT-MEAL": "管理费用-餐饮费"},
    )
    lines = (
        _line(line_id=1, amount=Decimal("106.00"), tax_amount=Decimal("6.00")),
        _line(
            line_id=2,
            category_code="CAT-MEAL",
            category_name="餐饮",
            amount=Decimal("50.00"),
            tax_amount=Decimal("0.00"),
        ),
    )
    entries = build_entries(
        [
            _claim(
                total_amount=Decimal("156.00"),
                payment_amount=Decimal("156.00"),
                lines=lines,
            )
        ],
        config,
        period=PERIOD,
    )
    # 2 借方（费用）+ 1 税费 + 1 贷方
    assert len(entries) == 4
    assert entries[0].debit == Decimal("100.00")
    assert entries[1].debit == Decimal("50.00")
    assert entries[2].debit == Decimal("6.00")
    assert entries[3].credit == Decimal("156.00")


def test_split_without_tax_account_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path, tax_split="all", tax_account_code="22210101")
    # 手工构造一份「有税额但科目未配置」的配置副本
    import dataclasses

    broken = dataclasses.replace(
        config, tax=dataclasses.replace(config.tax, account_code=None)
    )
    with pytest.raises(ExportValidationError) as excinfo:
        build_entries(
            [_claim(lines=(_line(tax_amount=Decimal("6.00")),))], broken, period=PERIOD
        )
    assert excinfo.value.code == CODE_ACCOUNT_CODE_UNRESOLVED


# --- 科目 / 币别 fail closed ---------------------------------------------------


def test_unmapped_category_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(ExportValidationError) as excinfo:
        build_entries(
            [_claim(lines=(_line(category_code="CAT-UNKNOWN"),))], config, period=PERIOD
        )
    assert excinfo.value.code == CODE_ACCOUNT_CODE_UNRESOLVED


def test_unmapped_category_falls_back_to_default(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        expense_default_code="560199",
        expense_default_name="管理费用-其他",
    )
    entries = build_entries(
        [_claim(lines=(_line(category_code="CAT-UNKNOWN"),))], config, period=PERIOD
    )
    assert entries[0].account_code == "560199"
    assert entries[0].account_name == "管理费用-其他"


def test_unmapped_currency_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(ExportValidationError) as excinfo:
        build_entries([_claim(currency="USD")], config, period=PERIOD)
    assert excinfo.value.code == CODE_CURRENCY_UNMAPPED


def test_missing_credit_account_fails_closed(tmp_path: Path) -> None:
    import dataclasses

    config = make_config(
        tmp_path,
        credit_code="",
        credit_by_status={"paid": {"code": "100201"}},
    )
    broken = dataclasses.replace(
        config, credit=dataclasses.replace(config.credit, by_status={})
    )
    with pytest.raises(ExportValidationError) as excinfo:
        build_entries([_claim()], broken, period=PERIOD)
    assert excinfo.value.code == CODE_ACCOUNT_CODE_UNRESOLVED


# --- 凭证号 / 排序（字节可复现前提） -------------------------------------------


def test_voucher_numbering_follows_basis_date_then_claim_id(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    later = _claim(
        claim_id=5,
        claim_no="EC-00000005",
        lines=(_line(expense_date=date(2026, 10, 9)),),
    )
    earlier = _claim(
        claim_id=3,
        claim_no="EC-00000003",
        lines=(_line(expense_date=date(2026, 10, 2)),),
    )
    entries = build_entries([later, earlier], config, period=PERIOD)
    numbers = {entry.business_no: entry.voucher_no for entry in entries}
    assert numbers["EC-00000003"] == 1
    assert numbers["EC-00000005"] == 2


def test_entry_order_is_debit_then_tax_then_credit(tmp_path: Path) -> None:
    config = make_config(tmp_path, tax_split="all", tax_account_code="22210101")
    lines = (
        _line(line_id=2, amount=Decimal("10.00"), tax_amount=Decimal("1.00")),
        _line(line_id=1, amount=Decimal("20.00"), tax_amount=Decimal("2.00")),
    )
    entries = build_entries(
        [_claim(total_amount=Decimal("33.00"), payment_amount=Decimal("33.00"), lines=lines)],
        config,
        period=PERIOD,
    )
    # 借方按 line_id 升序（1 先于 2），金额为不含税口径（拆税），随后税费，最后贷方。
    assert [e.debit for e in entries[:2]] == [Decimal("18.00"), Decimal("9.00")]
    assert entries[2].account_code == "22210101"
    assert entries[2].debit == Decimal("3.00")
    assert entries[3].credit == Decimal("33.00")


# --- 摘要 / 制单人 / 辅助核算 / 附件数 -----------------------------------------


def test_summary_template_and_truncation(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        summary_template="{claim_no}-{employee_no}",
        summary_max_length=5,
    )
    entries = build_entries([_claim()], config, period=PERIOD)
    assert entries[0].summary == "EC-00"


def test_summary_period_placeholder(tmp_path: Path) -> None:
    config = make_config(tmp_path, summary_template="{period} {claim_no}")
    entries = build_entries([_claim()], config, period=PERIOD)
    assert entries[0].summary == "2026-10 EC-00000001"


def test_preparer_exporter_and_literal(tmp_path: Path) -> None:
    exporter_config = make_config(tmp_path, preparer="exporter")
    entries = build_entries(
        [_claim()], exporter_config, period=PERIOD, exporter_employee_no="E-FIN"
    )
    assert entries[0].preparer == "E-FIN"

    literal_config = make_config(tmp_path, preparer="literal", preparer_literal="财务")
    entries = build_entries([_claim()], literal_config, period=PERIOD)
    assert entries[0].preparer == "财务"


def test_auxiliary_department_mode(tmp_path: Path) -> None:
    config = make_config(tmp_path, aux_mode="department", aux_department_class="部门")
    entries = build_entries([_claim()], config, period=PERIOD)
    debit = entries[0]
    assert debit.aux_class_a == "部门"
    assert debit.aux_name_a == "财务部"
    # 项目代码（部门编码）无权威来源 → 留空。
    assert debit.aux_code_a is None
    # 税费/贷方分录不挂部门辅助核算。
    assert entries[1].aux_name_a is None


def test_auxiliary_counterparty_mode_uses_configured_value(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        aux_mode="department_counterparty",
        aux_department_class="部门",
        aux_counterparty_class="往来单位",
        aux_counterparty_code="W001",
        aux_counterparty_name="示例往来单位",
    )
    entries = build_entries([_claim()], config, period=PERIOD)
    debit = entries[0]
    assert (debit.aux_class_b, debit.aux_code_b, debit.aux_name_b) == (
        "往来单位",
        "W001",
        "示例往来单位",
    )


def test_attachment_count_basis(tmp_path: Path) -> None:
    invoices = (
        InvoiceInput(
            duplicate_key="a" * 64,
            amount=Decimal("100.00"),
            verification_status="verified_pass",
            suspected_duplicate=False,
            has_source_file=True,
        ),
        InvoiceInput(
            duplicate_key="b" * 64,
            amount=None,
            verification_status="pending_verification",
            suspected_duplicate=False,
            has_source_file=False,
        ),
    )
    by_invoices = make_config(tmp_path, attachment_basis="invoices")
    entries = build_entries([_claim(invoices=invoices)], by_invoices, period=PERIOD)
    assert entries[0].attachment_count == 2

    by_files = make_config(tmp_path, attachment_basis="files")
    entries = build_entries([_claim(invoices=invoices)], by_files, period=PERIOD)
    assert entries[0].attachment_count == 1


def test_credit_uses_payment_amount_when_present(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    entries = build_entries(
        [_claim(payment_amount=Decimal("99.50"), total_amount=Decimal("99.50"))],
        config,
        period=PERIOD,
    )
    assert entries[-1].credit == Decimal("99.50")


def test_basis_date_paid_at_uses_payment_date(tmp_path: Path) -> None:
    claim = _claim(payment_date=date(2026, 11, 3))
    assert basis_date(claim, "paid_at") == date(2026, 11, 3)
    assert basis_date(claim, "expense_date") == date(2026, 10, 5)


def test_basis_date_paid_at_without_payment_is_none() -> None:
    assert basis_date(_claim(payment_date=None), "paid_at") is None


# --- 聚合辅助（供校验层复用） ---------------------------------------------------


def test_totals_helpers(tmp_path: Path) -> None:
    claims = [
        _claim(
            lines=(
                _line(line_id=1, amount=Decimal("106.00"), tax_amount=Decimal("6.00")),
                _line(line_id=2, amount=Decimal("50.00")),
            ),
            total_amount=Decimal("156.00"),
        )
    ]
    assert gross_total(claims) == Decimal("156.00")
    assert net_total(claims) == Decimal("150.00")
    assert tax_total(claims) == Decimal("6.00")
    assert gross_total(claims) <= MAX_AMOUNT


def test_entry_row_is_frozen() -> None:
    row = EntryRow(
        voucher_word="记",
        voucher_no=1,
        entry_date=date(2026, 10, 1),
        summary="x",
        account_code="560101",
        account_name=None,
        currency_code="RMB",
        currency_name=None,
        original_amount=Decimal("1.00"),
        exchange_rate=Decimal("1"),
        debit=Decimal("1.00"),
        credit=None,
        preparer="E0001",
        business_no="EC-1",
        attachment_count=0,
    )
    with pytest.raises(FrozenInstanceError):
        row.voucher_no = 2  # type: ignore[misc]
