"""校验层测试（纯逻辑；§6 E1–E7 / C1–C5 / fail closed 12 条）。

覆盖 Issue #53 验收标准第 5 条：**校验分支全覆盖**——借贷不平衡、价税不符、越界值、
模板哈希不符（见 ``test_template.py``）、注入文本（见 ``test_writer.py``）、异常票、
期间包含性、对账失败等；错误机器可读（``rule`` + ``code``）。
"""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.export.errors import (
    CODE_AMOUNT_OUT_OF_RANGE,
    CODE_AMOUNT_TOTAL_MISMATCH,
    CODE_CLAIM_COUNT_MISMATCH,
    CODE_CLAIM_SET_MISMATCH,
    CODE_CLAIM_TOTAL_MISMATCH,
    CODE_DEBIT_CREDIT_IMBALANCE,
    CODE_GROSS_NOT_NET_PLUS_TAX,
    CODE_INVOICE_AMOUNT_INCONSISTENT,
    CODE_INVOICE_STATUS_REJECTED,
    CODE_NO_RECORDS_IN_PERIOD,
    CODE_PAYMENT_TOTAL_MISMATCH,
    CODE_PERIOD_CONTAINMENT_FAILED,
    CODE_ROW_COUNT_MISMATCH,
)
from app.export.mapping import ClaimInput, InvoiceInput, LineInput, build_entries
from app.export.validation import (
    check_claim_set,
    parse_period,
    period_contains,
    validate_export,
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


def _codes(failures) -> set[str]:
    return {failure.code for failure in failures}


def _rules(failures) -> set[str]:
    return {failure.rule for failure in failures}


def _valid(tmp_path: Path):
    config = make_config(tmp_path)
    claims = [_claim()]
    entries = build_entries(claims, config, period=PERIOD)
    return config, claims, entries


# --- 快乐路径 -----------------------------------------------------------------


def test_clean_export_passes(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    assert validate_export(claims, entries, config, period=PERIOD) == []


def test_no_records_in_period_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    failures = validate_export([], [], config, period=PERIOD)
    assert _codes(failures) == {CODE_NO_RECORDS_IN_PERIOD}
    assert _rules(failures) == {"C4"}


# --- E1 / E2 ------------------------------------------------------------------


def test_unbalanced_voucher_fails_closed(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    broken = list(entries)
    broken[-1] = dataclasses.replace(broken[-1], credit=Decimal("99.00"))
    failures = validate_export(claims, broken, config, period=PERIOD)
    assert CODE_DEBIT_CREDIT_IMBALANCE in _codes(failures)
    assert {"E1", "E2"} <= _rules(failures)


def test_entry_with_both_sides_fails_closed(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    broken = list(entries)
    broken[0] = dataclasses.replace(broken[0], credit=Decimal("100.00"))
    failures = validate_export(claims, broken, config, period=PERIOD)
    assert CODE_DEBIT_CREDIT_IMBALANCE in _codes(failures)


# --- E3 -----------------------------------------------------------------------


def test_tax_exceeding_amount_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(
        lines=(_line(amount=Decimal("100.00"), tax_amount=Decimal("120.00")),),
        payment_amount=Decimal("100.00"),
    )
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_GROSS_NOT_NET_PLUS_TAX in _codes(failures)
    assert "E3" in _rules(failures)


# --- E4 / E5 ------------------------------------------------------------------


def test_claim_total_mismatch_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(total_amount=Decimal("120.00"), payment_amount=Decimal("120.00"))
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_CLAIM_TOTAL_MISMATCH in _codes(failures)
    assert "E4" in _rules(failures)


def test_payment_total_mismatch_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(payment_amount=Decimal("90.00"))
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_PAYMENT_TOTAL_MISMATCH in _codes(failures)
    assert "E5" in _rules(failures)


# --- E6 / E7 ------------------------------------------------------------------


def _invoice(**overrides: object) -> InvoiceInput:
    base: dict[str, object] = {
        "duplicate_key": "a" * 64,
        "amount": Decimal("100.00"),
        "verification_status": "verified_pass",
        "suspected_duplicate": False,
        "has_source_file": True,
    }
    base.update(overrides)
    return InvoiceInput(**base)  # type: ignore[arg-type]


def test_invoice_three_amounts_inconsistent_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(
        invoices=(
            _invoice(
                amount_without_tax=Decimal("100.00"),
                verified_tax_amount=Decimal("6.00"),
                amount_with_tax=Decimal("120.00"),
            ),
        )
    )
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_INVOICE_AMOUNT_INCONSISTENT in _codes(failures)
    assert "E6" in _rules(failures)


def test_invoice_total_not_matching_lines_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(invoices=(_invoice(amount=Decimal("80.00")),))
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_INVOICE_AMOUNT_INCONSISTENT in _codes(failures)
    assert "E7" in _rules(failures)


def test_invoice_without_amount_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(invoices=(_invoice(amount=None),))
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert "E7" in _rules(failures)


# --- fail closed #8（异常票处置） ----------------------------------------------


def test_verified_fail_invoice_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(
        invoices=(_invoice(verification_status="verified_fail"),),
    )
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_INVOICE_STATUS_REJECTED in _codes(failures)
    assert "fail_closed_8" in _rules(failures)


def test_suspected_duplicate_invoice_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(invoices=(_invoice(suspected_duplicate=True),))
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_INVOICE_STATUS_REJECTED in _codes(failures)


def test_invoice_policy_allow_permits_export(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, on_verified_fail="allow", on_suspected_duplicate="allow"
    )
    claim = _claim(
        invoices=(
            _invoice(verification_status="verified_fail", suspected_duplicate=True),
        )
    )
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_INVOICE_STATUS_REJECTED not in _codes(failures)


# --- fail closed #9（越界值） ---------------------------------------------------


def test_zero_amount_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(
        lines=(_line(amount=Decimal("0.00")),),
        total_amount=Decimal("0.00"),
        payment_amount=Decimal("0.00"),
    )
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_AMOUNT_OUT_OF_RANGE in _codes(failures)
    assert "fail_closed_9" in _rules(failures)


def test_three_decimal_places_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(
        lines=(_line(amount=Decimal("100.001")),),
        total_amount=Decimal("100.001"),
        payment_amount=Decimal("100.001"),
    )
    failures = validate_export(
        [claim], build_entries([claim], config, period=PERIOD), config, period=PERIOD
    )
    assert CODE_AMOUNT_OUT_OF_RANGE in _codes(failures)


def test_amount_above_numeric_limit_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    huge = Decimal("99999999999999999.99")
    claim = _claim(
        lines=(_line(amount=huge),),
        total_amount=huge,
        payment_amount=huge,
    )
    failures = validate_export(
        [claim], build_entries([claim], config, period=PERIOD), config, period=PERIOD
    )
    assert CODE_AMOUNT_OUT_OF_RANGE in _codes(failures)


# --- C1 / C2 / C3 -------------------------------------------------------------


def test_row_count_mismatch_fails_closed(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    failures = validate_export(claims, list(entries)[:-1], config, period=PERIOD)
    assert CODE_ROW_COUNT_MISMATCH in _codes(failures)
    assert "C1" in _rules(failures)


def test_voucher_count_mismatch_fails_closed(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    second = _claim(
        claim_id=2,
        claim_no="EC-00000002",
        lines=(_line(expense_date=date(2026, 10, 6)),),
    )
    failures = validate_export([claims[0], second], entries, config, period=PERIOD)
    assert CODE_CLAIM_COUNT_MISMATCH in _codes(failures)
    assert "C2" in _rules(failures)


def test_amount_total_mismatch_fails_closed(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    broken = list(entries)
    broken[0] = dataclasses.replace(broken[0], debit=Decimal("80.00"))
    broken[1] = dataclasses.replace(broken[1], credit=Decimal("80.00"))
    failures = validate_export(claims, broken, config, period=PERIOD)
    assert CODE_AMOUNT_TOTAL_MISMATCH in _codes(failures)
    assert "C3" in _rules(failures)


# --- C4（期间包含性） ----------------------------------------------------------


def test_basis_outside_period_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claim = _claim(lines=(_line(expense_date=date(2026, 9, 30)),))
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_PERIOD_CONTAINMENT_FAILED in _codes(failures)
    assert "C4" in _rules(failures)


def test_cross_period_claim_fails_closed(tmp_path: Path) -> None:
    """一张单的部分明细落在期间外 → 整单不导出（跨期数据不得进入导出）。"""
    config = make_config(tmp_path)
    lines = (
        _line(line_id=1, expense_date=date(2026, 10, 31)),
        _line(line_id=2, expense_date=date(2026, 11, 1)),
    )
    claim = _claim(
        lines=lines,
        total_amount=Decimal("200.00"),
        payment_amount=Decimal("200.00"),
    )
    entries = build_entries([claim], config, period=PERIOD)
    failures = validate_export([claim], entries, config, period=PERIOD)
    assert CODE_PERIOD_CONTAINMENT_FAILED in _codes(failures)


def test_paid_at_basis_allows_other_expense_months(tmp_path: Path) -> None:
    config = make_config(tmp_path, date_basis="paid_at")
    claim = _claim(
        lines=(_line(expense_date=date(2026, 9, 15)),),
        payment_date=date(2026, 10, 20),
    )
    entries = build_entries([claim], config, period=PERIOD)
    assert validate_export([claim], entries, config, period=PERIOD) == []


def test_paid_at_basis_without_payment_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path, date_basis="paid_at")
    claim = _claim(payment_date=None, payment_amount=None)
    failures = validate_export([claim], [], config, period=PERIOD)
    assert CODE_PERIOD_CONTAINMENT_FAILED in _codes(failures)


# --- C5（集合一致性） ----------------------------------------------------------


def test_claim_set_mismatch_fails_closed(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    failures = check_claim_set(entries, [999], {999: "EC-99999999"})
    assert _codes(failures) == {CODE_CLAIM_SET_MISMATCH}
    assert _rules(failures) == {"C5"}
    assert check_claim_set(
        entries, [claim.claim_id for claim in claims], {1: "EC-00000001"}
    ) == []


# --- 期间解析 -----------------------------------------------------------------


def test_parse_period_accepts_valid() -> None:
    assert parse_period("2026-10") == (2026, 10)


@pytest.mark.parametrize("bad", ["2026-13", "2026-1", "202610", "", "abc"])
def test_parse_period_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_period(bad)


def test_period_contains() -> None:
    assert period_contains("2026-10", date(2026, 10, 31))
    assert not period_contains("2026-10", date(2026, 11, 1))


# --- 机器可读 -----------------------------------------------------------------


def test_failures_are_machine_readable(tmp_path: Path) -> None:
    config, claims, entries = _valid(tmp_path)
    broken = list(entries)
    broken[-1] = dataclasses.replace(broken[-1], credit=Decimal("1.00"))
    failures = validate_export(claims, broken, config, period=PERIOD)
    payload = [failure.as_dict() for failure in failures]
    assert all(
        set(item) == {"rule", "code", "message", "details"} for item in payload
    )
    assert all(isinstance(item["code"], str) and item["code"] for item in payload)
