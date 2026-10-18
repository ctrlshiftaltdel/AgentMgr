"""Issue #56 写回路径的**纯逻辑**测试（无需 PostgreSQL）。

只覆盖 ``app.intake.writeback`` 中不触碰数据库的部分：字段映射表、费用日期解析、
金额定点化、低置信度字段名与票面字段白名单。落库行为（同事务写入、幂等、挂账、
授权与拒绝审计）在 ``test_intake_writeback.py`` 中于专用测试库上验证。

这些用例把**定稿口径**钉死在测试里（Issue #56 评论）：``expense_date`` 默认取票面
``invoice_date``、人工覆盖优先、费用类别必须显式提供、票面白名单不含税号/主体名。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.intake.parsing import (
    BUSINESS_REQUIRED_FIELDS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    required_fields_for,
)
from app.intake.writeback import (
    FIELD_SOURCES,
    INVOICE_IDENTITY_FIELDS,
    INVOICE_SOURCE_FIELDS,
    SUPPORTED_BUSINESS_TYPE,
    draft_field_value,
    invoice_fields_from_draft,
    low_confidence_fields,
    parse_money_value,
    resolve_expense_date,
)
from app.integrations.invoice_verification import (
    InvoiceFields,
    compute_duplicate_key,
)

#: 可能出现在草稿里、但**不得**进入挂账入参的敏感/无关字段。
NON_WHITELISTED_DRAFT_KEYS = (
    "seller_tax_id",
    "buyer_tax_id",
    "seller_name",
    "buyer_name",
    "invoice_type",
    "check_code",
    "raw_note",
)


# --- 映射表本身 ---------------------------------------------------------------


def test_supported_business_type_is_expense_claim() -> None:
    assert SUPPORTED_BUSINESS_TYPE == "expense_claim"
    assert SUPPORTED_BUSINESS_TYPE in BUSINESS_REQUIRED_FIELDS


def test_field_sources_covers_every_required_business_field() -> None:
    """必填字段口径漂移必须立刻暴露：每个必填字段都要有映射来源。"""
    for field in required_fields_for(SUPPORTED_BUSINESS_TYPE):
        assert field in FIELD_SOURCES, f"{field} has no draft-field mapping"


def test_field_sources_prefers_invoice_date_for_expense_date() -> None:
    """定稿：``expense_date`` 默认取票面开票日期，业务同名字段是回退来源。"""
    assert FIELD_SOURCES["expense_date"] == ("invoice_date", "expense_date")


def test_invoice_whitelist_covers_identity_and_excludes_sensitive_keys() -> None:
    assert set(INVOICE_IDENTITY_FIELDS) <= set(INVOICE_SOURCE_FIELDS)
    for name in NON_WHITELISTED_DRAFT_KEYS:
        assert name not in INVOICE_SOURCE_FIELDS


# --- draft_field_value --------------------------------------------------------


def test_draft_field_value_takes_first_present_and_skips_missing() -> None:
    extracted = {"invoice_date": "   ", "expense_date": "2026-10-01"}
    assert draft_field_value(extracted, ("invoice_date", "expense_date")) == (
        "2026-10-01",
        "expense_date",
    )


@pytest.mark.parametrize("blank", [None, "", "   ", [], {}, ()])
def test_draft_field_value_treats_blank_as_missing(blank: object) -> None:
    assert draft_field_value({"amount": blank}, ("amount",)) is None


def test_draft_field_value_returns_none_when_absent() -> None:
    assert draft_field_value({}, ("amount",)) is None


# --- resolve_expense_date -----------------------------------------------------


def test_resolve_expense_date_prefers_explicit_override() -> None:
    extracted = {"invoice_date": "2026-10-01", "expense_date": "2026-10-02"}
    assert resolve_expense_date(extracted, override="2026-09-30") == (
        date(2026, 9, 30),
        "override",
    )
    assert resolve_expense_date(extracted, override=date(2026, 1, 2)) == (
        date(2026, 1, 2),
        "override",
    )


def test_resolve_expense_date_defaults_to_invoice_date() -> None:
    """票面开票日期优先于业务同名字段（定稿口径）。"""
    extracted = {"invoice_date": "2026-10-01", "expense_date": "2025-12-31"}
    assert resolve_expense_date(extracted) == (date(2026, 10, 1), "invoice_date")


def test_resolve_expense_date_falls_back_to_business_field() -> None:
    """人工录入口径只给业务字段名时仍可解析。"""
    assert resolve_expense_date({"expense_date": "20261002"}) == (
        date(2026, 10, 2),
        "expense_date",
    )


@pytest.mark.parametrize(
    "extracted,override",
    [
        ({}, None),
        ({"invoice_date": "2026年10月01日"}, None),
        ({"expense_date": "01/10/2026"}, None),
        ({"invoice_date": "2026-10-01"}, "not-a-date"),
        ({"invoice_date": "2026-10-01"}, "2026/10/01"),
    ],
)
def test_resolve_expense_date_fails_closed_on_unknown_forms(
    extracted: dict[str, object], override: str | None
) -> None:
    assert resolve_expense_date(extracted, override=override) is None


# --- parse_money_value --------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("123.45", Decimal("123.45")),
        ("123.455", Decimal("123.46")),  # ROUND_HALF_UP
        (123, Decimal("123.00")),
        (Decimal("0.005"), Decimal("0.01")),
        ("0", Decimal("0.00")),
    ],
)
def test_parse_money_value_accepts_fixed_point(value: object, expected: Decimal) -> None:
    assert parse_money_value(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        123.45,  # float：拒绝二进制浮点
        True,  # bool 是 int 子类，但作为金额几乎总是笔误
        None,
        "abc",
        "1,234.00",
        "",
        [1],
        {"amount": 1},
        float("inf"),
        "NaN",
        "Infinity",
    ],
)
def test_parse_money_value_fails_closed_on_unsupported_values(value: object) -> None:
    assert parse_money_value(value) is None


# --- low_confidence_fields ----------------------------------------------------


def test_low_confidence_fields_uses_default_threshold_and_sorts() -> None:
    confidence = {"amount": 0.2, "expense_date": DEFAULT_CONFIDENCE_THRESHOLD, "b": 0.1}
    assert low_confidence_fields(confidence) == ("amount", "b")


def test_low_confidence_fields_accepts_override_threshold() -> None:
    assert low_confidence_fields({"amount": 0.9}, threshold=0.95) == ("amount",)
    assert low_confidence_fields({"amount": 0.9}, threshold=0.5) == ()


@pytest.mark.parametrize("confidence", [None, {}, {"amount": "unknown"}, {"amount": None}])
def test_low_confidence_fields_ignores_non_numeric(confidence: object) -> None:
    assert low_confidence_fields(confidence) == ()  # type: ignore[arg-type]


# --- invoice_fields_from_draft ------------------------------------------------


def test_invoice_fields_from_draft_maps_whitelist_and_computes_duplicate_key() -> None:
    extracted = {
        "invoice_no": "12345678",
        "invoice_code": "011002000211",
        "invoice_date": "2026-10-01",
        "amount": "100.00",
        "tax_amount": "3.45",
        "seller_tax_id": "91110000EXAMPLE0001",
        "buyer_name": "示例采购方",
        "invoice_type": "vat_invoice",
        "raw_note": "附件原文不得外泄",
    }
    mapping = invoice_fields_from_draft(extracted)

    assert mapping.skip_reason is None
    fields = mapping.fields
    assert isinstance(fields, InvoiceFields)
    assert fields.invoice_no == "12345678"
    assert fields.invoice_code == "011002000211"
    assert fields.invoice_date == date(2026, 10, 1)
    assert fields.amount == Decimal("100.00")
    # 白名单之外的字段一律不透传（税号/主体名/OCR 票种分类名/校验码）。
    assert fields.seller_tax_code is None
    assert fields.buyer_tax_code is None
    assert fields.check_code is None
    assert fields.invoice_kind is None
    assert fields.region_code is None
    # 与查验域同一身份摘要（W1-23 语义复用）。
    assert compute_duplicate_key(fields) == compute_duplicate_key(
        InvoiceFields(
            invoice_no="12345678",
            invoice_code="011002000211",
            invoice_date=date(2026, 10, 1),
            amount="100.00",
        )
    )


@pytest.mark.parametrize(
    "extracted",
    [
        {},
        {"invoice_no": "12345678"},
        {"invoice_date": "2026-10-01"},
        {"invoice_no": "   ", "invoice_date": "2026-10-01"},
    ],
)
def test_invoice_fields_from_draft_skips_when_identity_incomplete(
    extracted: dict[str, object],
) -> None:
    mapping = invoice_fields_from_draft(extracted)
    assert mapping.fields is None
    assert mapping.skip_reason == "missing_invoice_fields"


@pytest.mark.parametrize(
    "extracted",
    [
        {"invoice_no": "12345678", "invoice_date": "2026年10月01日"},
        {"invoice_no": "12345678", "invoice_date": "2026-10-01", "amount": 100.0},
    ],
)
def test_invoice_fields_from_draft_skips_invalid_forms(
    extracted: dict[str, object],
) -> None:
    mapping = invoice_fields_from_draft(extracted)
    assert mapping.fields is None
    assert mapping.skip_reason == "invalid_invoice_fields"
