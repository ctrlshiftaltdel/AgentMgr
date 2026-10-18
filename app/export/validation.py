"""校验层：E1–E7 / C1–C5 与 fail closed 条件（§6）。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md`` §6

* §6.1 恒等式 E1–E7；
* §6.2 对账 C1–C5；
* §6.3 fail closed 条件清单（12 条；任一命中即**不产出文件**）；
* §6.5 幂等与可复现。

**纯逻辑**：本模块不导入数据库/ORM。它只做**判定**，不做修复（§6.1 E4 明确
「不在导出时自愈」）。调用方收集全部命中项后由服务层 fail closed：不写文件、
不写 manifest、写拒绝审计。

失败项的 ``code`` 是机器可读码（见 :mod:`app.export.errors`），``details`` 只放
非敏感的结构化字段（ID、金额字符串、期间等）。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from app.expense.money import MAX_AMOUNT, quantize, sum_amounts
from app.export.config import ExportConfig
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
    CODE_PERIOD_INVALID,
    CODE_ROW_COUNT_MISMATCH,
    ValidationFailure,
)
from app.export.mapping import (
    ClaimInput,
    EntryRow,
    basis_date,
    gross_total,
    net_total,
    should_split_tax,
    tax_total,
)

__all__ = [
    "check_claim_set",
    "parse_period",
    "period_contains",
    "validate_claims",
    "validate_export",
]

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")

#: 挂账发票中**不允许导出**的查验状态（fail closed #8）。
REJECTED_INVOICE_STATUS = "verified_fail"


def parse_period(period: str) -> tuple[int, int]:
    """把 ``YYYY-MM`` 解析为 ``(year, month)``；非法即抛 :class:`ValueError`。

    服务层把 :class:`ValueError` 转成机器可读的 ``period_invalid`` 错误。
    """
    match = _PERIOD_RE.match(period or "")
    if match is None:
        raise ValueError(f"period must be formatted YYYY-MM: {period!r}")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"period month out of range: {period!r}")
    return year, month


def period_contains(period: str, value: date) -> bool:
    """``value`` 是否落在 ``YYYY-MM`` 期间内。"""
    year, month = parse_period(period)
    return value.year == year and value.month == month


def _failure(rule: str, code: str, message: str, **details: object) -> ValidationFailure:
    return ValidationFailure(rule=rule, code=code, message=message, details=dict(details))


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(quantize(value))


# --- 单行 / 单凭证 ------------------------------------------------------------


def _check_line_amounts(claim: ClaimInput, config: ExportConfig) -> list[ValidationFailure]:
    """E3 + fail closed #9：价税合计 = 不含税 + 税额，且金额在 ``NUMERIC(18,2)`` 内。"""
    failures: list[ValidationFailure] = []
    for line in claim.lines:
        amount = line.amount
        tax = line.tax_amount
        out_of_range = (
            amount <= Decimal("0.00")
            or amount > MAX_AMOUNT
            or tax < Decimal("0.00")
            or quantize(amount) != amount
            or quantize(tax) != tax
        )
        if out_of_range:
            failures.append(
                _failure(
                    "fail_closed_9",
                    CODE_AMOUNT_OUT_OF_RANGE,
                    "expense line amount is out of the supported range or precision",
                    claim_id=claim.claim_id,
                    line_id=line.line_id,
                    amount=_money(amount),
                    tax_amount=_money(tax),
                )
            )
            continue
        if tax > amount or (amount - tax) + tax != amount or (amount - tax) < Decimal("0.00"):
            failures.append(
                _failure(
                    "E3",
                    CODE_GROSS_NOT_NET_PLUS_TAX,
                    "gross amount does not equal net amount plus tax amount",
                    claim_id=claim.claim_id,
                    line_id=line.line_id,
                    amount=_money(amount),
                    tax_amount=_money(tax),
                )
            )
    return failures


def _check_claim_totals(claim: ClaimInput) -> list[ValidationFailure]:
    """E4 / E5：报销单合计与明细合计一致，付款金额与合计一致。"""
    failures: list[ValidationFailure] = []
    lines_total = sum_amounts(line.amount for line in claim.lines)
    if lines_total != quantize(claim.total_amount):
        failures.append(
            _failure(
                "E4",
                CODE_CLAIM_TOTAL_MISMATCH,
                "claim total_amount does not equal the sum of its line amounts",
                claim_id=claim.claim_id,
                claim_total=_money(claim.total_amount),
                lines_total=_money(lines_total),
            )
        )
    if claim.payment_amount is not None and quantize(claim.payment_amount) != quantize(
        claim.total_amount
    ):
        failures.append(
            _failure(
                "E5",
                CODE_PAYMENT_TOTAL_MISMATCH,
                "reimbursement payment amount does not equal the claim total",
                claim_id=claim.claim_id,
                payment_amount=_money(claim.payment_amount),
                claim_total=_money(claim.total_amount),
            )
        )
    return failures


def _check_invoices(claim: ClaimInput, config: ExportConfig) -> list[ValidationFailure]:
    """E6（票面三口径）/ E7（挂账票面 vs 明细）/ fail closed #8（异常票处置）。"""
    failures: list[ValidationFailure] = []
    if not claim.invoices:
        return failures

    invoice_total = Decimal("0.00")
    for invoice in claim.invoices:
        if invoice.verification_status == REJECTED_INVOICE_STATUS and (
            config.invoice_policy.on_verified_fail == "fail"
        ):
            failures.append(
                _failure(
                    "fail_closed_8",
                    CODE_INVOICE_STATUS_REJECTED,
                    "claim has an invoice whose verification status forbids export",
                    claim_id=claim.claim_id,
                    duplicate_key=invoice.duplicate_key,
                    verification_status=invoice.verification_status,
                )
            )
        if invoice.suspected_duplicate and (
            config.invoice_policy.on_suspected_duplicate == "fail"
        ):
            failures.append(
                _failure(
                    "fail_closed_8",
                    CODE_INVOICE_STATUS_REJECTED,
                    "claim has a suspected duplicate invoice and no disposition policy",
                    claim_id=claim.claim_id,
                    duplicate_key=invoice.duplicate_key,
                )
            )
        if (
            invoice.amount_without_tax is not None
            and invoice.amount_with_tax is not None
        ):
            expected = quantize(
                invoice.amount_without_tax + (invoice.verified_tax_amount or Decimal("0.00"))
            )
            if expected != quantize(invoice.amount_with_tax):
                failures.append(
                    _failure(
                        "E6",
                        CODE_INVOICE_AMOUNT_INCONSISTENT,
                        "invoice amount_without_tax + tax_amount != amount_with_tax",
                        claim_id=claim.claim_id,
                        duplicate_key=invoice.duplicate_key,
                        amount_without_tax=_money(invoice.amount_without_tax),
                        tax_amount=_money(invoice.verified_tax_amount),
                        amount_with_tax=_money(invoice.amount_with_tax),
                    )
                )
        if invoice.amount is None:
            failures.append(
                _failure(
                    "E7",
                    CODE_INVOICE_AMOUNT_INCONSISTENT,
                    "booked invoice snapshot has no amount to reconcile against lines",
                    claim_id=claim.claim_id,
                    duplicate_key=invoice.duplicate_key,
                )
            )
        else:
            invoice_total += invoice.amount

    if all(invoice.amount is not None for invoice in claim.invoices):
        lines_total = sum_amounts(line.amount for line in claim.lines)
        if quantize(invoice_total) != lines_total:
            failures.append(
                _failure(
                    "E7",
                    CODE_INVOICE_AMOUNT_INCONSISTENT,
                    "booked invoice amounts do not reconcile with the line amounts",
                    claim_id=claim.claim_id,
                    invoice_total=_money(invoice_total),
                    lines_total=_money(lines_total),
                )
            )
    return failures


def _check_period(
    claim: ClaimInput, config: ExportConfig, period: str
) -> list[ValidationFailure]:
    """C4：基准日与（费用发生口径下的）全部明细日期必须落在请求期间内。"""
    failures: list[ValidationFailure] = []
    basis = basis_date(claim, config.scope.date_basis)
    if basis is None:
        failures.append(
            _failure(
                "C4",
                CODE_PERIOD_CONTAINMENT_FAILED,
                "claim has no basis date for the configured date basis",
                claim_id=claim.claim_id,
                date_basis=config.scope.date_basis,
            )
        )
        return failures
    if not period_contains(period, basis):
        failures.append(
            _failure(
                "C4",
                CODE_PERIOD_CONTAINMENT_FAILED,
                "claim basis date is outside the requested period",
                claim_id=claim.claim_id,
                date_basis=config.scope.date_basis,
                basis_date=basis.isoformat(),
                period=period,
            )
        )
    if config.scope.date_basis == "expense_date":
        outside = [
            line.line_id
            for line in claim.lines
            if not period_contains(period, line.expense_date)
        ]
        if outside:
            failures.append(
                _failure(
                    "C4",
                    CODE_PERIOD_CONTAINMENT_FAILED,
                    "claim has lines outside the requested period (cross-period claim)",
                    claim_id=claim.claim_id,
                    line_ids=outside,
                    period=period,
                )
            )
    return failures


# --- 分录 / 全量 --------------------------------------------------------------


def _check_entry_shape(entries: list[EntryRow]) -> list[ValidationFailure]:
    """每条分录必须恰有一侧金额（借方异或贷方）。"""
    failures: list[ValidationFailure] = []
    for entry in entries:
        if (entry.debit is None) == (entry.credit is None):
            failures.append(
                _failure(
                    "E1",
                    CODE_DEBIT_CREDIT_IMBALANCE,
                    "entry row must carry exactly one of debit or credit",
                    voucher_no=entry.voucher_no,
                    business_no=entry.business_no,
                )
            )
    return failures


def _check_balance(entries: list[EntryRow]) -> list[ValidationFailure]:
    """E1（逐凭证）/ E2（全量）借贷平衡。"""
    failures: list[ValidationFailure] = []
    voucher_numbers = sorted({entry.voucher_no for entry in entries})
    for voucher_no in voucher_numbers:
        voucher_entries = [e for e in entries if e.voucher_no == voucher_no]
        debit = sum_amounts(e.debit for e in voucher_entries if e.debit is not None)
        credit = sum_amounts(e.credit for e in voucher_entries if e.credit is not None)
        if debit != credit:
            failures.append(
                _failure(
                    "E1",
                    CODE_DEBIT_CREDIT_IMBALANCE,
                    "voucher is not balanced (sum debit != sum credit)",
                    voucher_no=voucher_no,
                    debit_total=_money(debit),
                    credit_total=_money(credit),
                )
            )
    debit_total = sum_amounts(entry.debit for entry in entries if entry.debit is not None)
    credit_total = sum_amounts(entry.credit for entry in entries if entry.credit is not None)
    if debit_total != credit_total:
        failures.append(
            _failure(
                "E2",
                CODE_DEBIT_CREDIT_IMBALANCE,
                "export is not balanced (sum debit != sum credit)",
                debit_total=_money(debit_total),
                credit_total=_money(credit_total),
            )
        )
    return failures


def _check_reconciliation(
    claims: list[ClaimInput], entries: list[EntryRow], config: ExportConfig
) -> list[ValidationFailure]:
    """C1（行数）/ C2（笔数）/ C3（金额）对账。"""
    failures: list[ValidationFailure] = []

    expected_rows = 0
    for claim in claims:
        split_rows = 1 if any(should_split_tax(line, config) for line in claim.lines) else 0
        expected_rows += len(claim.lines) + split_rows + 1
    if len(entries) != expected_rows:
        failures.append(
            _failure(
                "C1",
                CODE_ROW_COUNT_MISMATCH,
                "entry row count does not match the expected per-voucher composition",
                entry_count=len(entries),
                expected_entry_count=expected_rows,
            )
        )

    voucher_count = len({entry.voucher_no for entry in entries})
    if voucher_count != len(claims):
        failures.append(
            _failure(
                "C2",
                CODE_CLAIM_COUNT_MISMATCH,
                "voucher count does not match the number of selected claims",
                voucher_count=voucher_count,
                claim_count=len(claims),
            )
        )
    expense_line_count = sum(len(claim.lines) for claim in claims)
    if len(entries) and expense_line_count == 0:
        failures.append(
            _failure(
                "C2",
                CODE_CLAIM_COUNT_MISMATCH,
                "export produced entries without any expense line",
                claim_count=len(claims),
            )
        )

    gross = gross_total(claims)
    claim_total = sum_amounts(claim.total_amount for claim in claims)
    debit_total = sum_amounts(entry.debit for entry in entries if entry.debit is not None)
    if gross != claim_total:
        failures.append(
            _failure(
                "C3",
                CODE_AMOUNT_TOTAL_MISMATCH,
                "sum of gross line amounts does not match the sum of claim totals",
                gross_total=_money(gross),
                claim_total=_money(claim_total),
            )
        )
    if debit_total != gross:
        failures.append(
            _failure(
                "C3",
                CODE_AMOUNT_TOTAL_MISMATCH,
                "sum of debit amounts does not match the gross total",
                debit_total=_money(debit_total),
                gross_total=_money(gross),
                net_total=_money(net_total(claims)),
                tax_total=_money(tax_total(claims)),
            )
        )
    return failures


def check_claim_set(
    entries: list[EntryRow], claim_ids: list[int], claim_nos_by_id: dict[int, str]
) -> list[ValidationFailure]:
    """C5：manifest 的 ``claim_ids`` 与文件内业务编号集合**完全一致**。"""
    expected_nos = {claim_nos_by_id[claim_id] for claim_id in claim_ids}
    actual_nos = {entry.business_no for entry in entries}
    if expected_nos != actual_nos:
        return [
            _failure(
                "C5",
                CODE_CLAIM_SET_MISMATCH,
                "manifest claim set does not match the business numbers in the file",
                expected_count=len(expected_nos),
                actual_count=len(actual_nos),
            )
        ]
    return []


def validate_claims(
    claims: list[ClaimInput],
    config: ExportConfig,
    *,
    period: str,
) -> list[ValidationFailure]:
    """**凭证级**校验（E3–E7 + C4 + fail closed #6/#8/#9），不需要分录行。

    服务层在生成分录行**之前**先跑这一层：科目/币别解析与分录构造都依赖「凭证级
    前提成立」，先跑可以给出完整失败清单，也避免用非法数据构造分录行。
    """
    if not claims:
        return [
            _failure(
                "C4",
                CODE_NO_RECORDS_IN_PERIOD,
                "no eligible records found in the requested period",
                period=period,
            )
        ]

    failures: list[ValidationFailure] = []
    for claim in claims:
        failures.extend(_check_line_amounts(claim, config))
        failures.extend(_check_claim_totals(claim))
        failures.extend(_check_invoices(claim, config))
        failures.extend(_check_period(claim, config, period))
    return failures


def validate_export(
    claims: list[ClaimInput],
    entries: list[EntryRow],
    config: ExportConfig,
    *,
    period: str,
) -> list[ValidationFailure]:
    """执行 §6 的全部校验，返回**全部**命中的失败项（空列表 = 通过）。

    fail closed #6 的「请求期间内无任何合格记录」在此实现：``claims`` 为空即失败。
    调用方在失败时**不写任何文件、不写 manifest**。
    """
    failures = validate_claims(claims, config, period=period)
    if not claims:
        return failures

    failures.extend(_check_entry_shape(entries))
    failures.extend(_check_balance(entries))
    failures.extend(_check_reconciliation(claims, entries, config))
    failures.extend(
        check_claim_set(
            entries,
            [claim.claim_id for claim in claims],
            {claim.claim_id: claim.claim_no for claim in claims},
        )
    )
    return failures
