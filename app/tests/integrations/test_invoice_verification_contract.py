"""可替换接口契约层测试（不触网、不触库）。

覆盖 W1-20 第二步与第四步的接口级硬性规则：

* ``ProviderType`` 三类实现类型；
* ``VerificationStatus`` 严格对应 PLAN §7.2 的七种状态（英文名 + 中文注释）；
* ``ManualAssistedProvider`` **永不**返回「查验通过」；
* 只有被认可渠道能进入「查验通过」（``can_set_verified_pass``）；
* 未识别来源 fail closed；
* ``duplicate_key`` 确定性且不泄露票号明文；
* 「查验不通过」与「无法查验」互不覆盖（``resolve_status``）；
* 证据脱敏（票号/代码/校验码/税号打码，凭据类键丢弃）。
"""

from __future__ import annotations

import re

import pytest

from app.integrations.invoice_verification import (
    DAILY_VERIFY_LIMIT,
    InvoiceFields,
    ManualAssistedProvider,
    ProviderError,
    ProviderType,
    VerificationStatus,
    can_set_verified_pass,
    coerce_provider_type,
    compute_duplicate_key,
    evidence_ref_for,
    is_conclusive_status,
    is_pending_status,
    is_recognized_provider_type,
    parse_invoice_date,
    resolve_status,
    sanitize_evidence,
)

from app.tests.integrations.invoice_fixtures import make_fields


# --- 枚举契约 -----------------------------------------------------------------


def test_provider_type_has_three_plan_types() -> None:
    """ProviderType 恰好是 PLAN §7.1 的三类实现。"""
    assert {member.value for member in ProviderType} == {
        "manual_assisted",
        "authorized_provider",
        "official_api",
    }


def test_verification_status_matches_seven_plan_states() -> None:
    """VerificationStatus 恰好是 PLAN §7.2 的七种状态。"""
    assert {member.value for member in VerificationStatus} == {
        "pending_identification",
        "pending_manual_confirmation",
        "pending_verification",
        "verified_pass",
        "verified_fail",
        "unable_to_verify",
        "suspected_duplicate",
    }
    assert len(list(VerificationStatus)) == 7


def test_verification_status_docstring_documents_chinese_labels() -> None:
    """每种状态都在枚举 docstring 里有对应中文（七种状态的中文注释）。"""
    doc = VerificationStatus.__doc__ or ""
    for label in (
        "待识别",
        "待人工确认",
        "待查验",
        "查验通过",
        "查验不通过",
        "无法查验/接口异常",
        "疑似重复",
    ):
        assert label in doc, f"missing Chinese label in docstring: {label}"


def test_pending_and_conclusive_helpers() -> None:
    assert is_pending_status(VerificationStatus.PENDING_IDENTIFICATION)
    assert is_pending_status(VerificationStatus.PENDING_MANUAL_CONFIRMATION)
    assert is_pending_status(VerificationStatus.PENDING_VERIFICATION)
    assert not is_pending_status(VerificationStatus.VERIFIED_PASS)
    assert is_conclusive_status(VerificationStatus.VERIFIED_PASS)
    assert is_conclusive_status(VerificationStatus.VERIFIED_FAIL)
    assert not is_conclusive_status(VerificationStatus.UNABLE_TO_VERIFY)


# --- 渠道识别闸门 -------------------------------------------------------------


def test_only_recognized_channels_can_set_verified_pass() -> None:
    assert can_set_verified_pass(ProviderType.AUTHORIZED_PROVIDER)
    assert can_set_verified_pass(ProviderType.OFFICIAL_API)
    assert not can_set_verified_pass(ProviderType.MANUAL_ASSISTED)


def test_unrecognized_provider_type_fails_closed() -> None:
    assert is_recognized_provider_type("authorized_provider")
    assert is_recognized_provider_type(ProviderType.MANUAL_ASSISTED)
    assert not is_recognized_provider_type("wechat_scrape")
    assert not is_recognized_provider_type(None)
    assert not is_recognized_provider_type(123)

    with pytest.raises(ProviderError):
        coerce_provider_type("wechat_scrape")
    with pytest.raises(ProviderError):
        coerce_provider_type(None)
    assert coerce_provider_type("manual_assisted") is ProviderType.MANUAL_ASSISTED


# --- ManualAssistedProvider ---------------------------------------------------


def test_manual_assisted_provider_never_returns_verified_pass() -> None:
    """人工辅助**永远不得**返回「查验通过」，只产出待人工确认的任务。"""
    provider = ManualAssistedProvider()
    assert provider.provider_type is ProviderType.MANUAL_ASSISTED
    assert provider.provider_name == "manual_assisted"

    result = provider.verify(make_fields())
    assert result.status is VerificationStatus.PENDING_MANUAL_CONFIRMATION
    assert result.status is not VerificationStatus.VERIFIED_PASS
    assert not can_set_verified_pass(provider.provider_type)
    # 反复调用也绝不产出「查验通过」，且不带任何票面结论/证据。
    for _ in range(5):
        again = provider.verify(make_fields())
        assert again.status is not VerificationStatus.VERIFIED_PASS
        assert again.status is VerificationStatus.PENDING_MANUAL_CONFIRMATION
        assert again.evidence is None
        assert again.provider_ref is None


# --- 发票要素与幂等键 ---------------------------------------------------------


def test_invoice_fields_normalizes_date_and_amount() -> None:
    fields = make_fields(invoice_date="20261016", amount="100.5")
    assert fields.invoice_date.isoformat() == "2026-10-16"
    assert str(fields.amount) == "100.50"

    with pytest.raises(ValueError):
        make_fields(invoice_no="   ")
    with pytest.raises(ValueError):
        make_fields(invoice_date="16/10/2026")


def test_float_amount_is_rejected() -> None:
    with pytest.raises(TypeError):
        make_fields(amount=1.5)


def test_parse_invoice_date_accepts_only_documented_formats() -> None:
    assert parse_invoice_date("2026-10-16").isoformat() == "2026-10-16"
    assert parse_invoice_date("20261016").isoformat() == "2026-10-16"
    assert parse_invoice_date("2026/10/16") is None
    assert parse_invoice_date("") is None
    assert parse_invoice_date(None) is None


def test_duplicate_key_is_deterministic_and_opaque() -> None:
    fields = make_fields()
    key_a = compute_duplicate_key(fields)
    key_b = compute_duplicate_key(make_fields())
    assert key_a == key_b
    assert re.fullmatch(r"[0-9a-f]{64}", key_a)
    # 不泄露票号/代码明文（可安全写入日志与审计）。
    assert fields.invoice_no not in key_a
    assert (fields.invoice_code or "") not in key_a
    # 身份任一不同则键不同。
    assert compute_duplicate_key(make_fields(invoice_no="87654321")) != key_a
    assert compute_duplicate_key(make_fields(invoice_code="011002000212")) != key_a
    assert compute_duplicate_key(make_fields(invoice_date="2026-10-15")) != key_a


# --- 状态合并（不通过 vs 无法查验） -------------------------------------------


def test_resolve_status_keeps_verified_fail_against_unable() -> None:
    """「查验不通过」不得被「无法查验/接口异常」覆盖。"""
    assert (
        resolve_status(
            VerificationStatus.VERIFIED_FAIL, VerificationStatus.UNABLE_TO_VERIFY
        )
        is VerificationStatus.VERIFIED_FAIL
    )


def test_resolve_status_lets_conclusive_result_replace_unable() -> None:
    """「无法查验」是技术性失败，可被确凿结论取代（但不能反向降级）。"""
    assert (
        resolve_status(
            VerificationStatus.UNABLE_TO_VERIFY, VerificationStatus.VERIFIED_FAIL
        )
        is VerificationStatus.VERIFIED_FAIL
    )
    assert (
        resolve_status(
            VerificationStatus.UNABLE_TO_VERIFY, VerificationStatus.PENDING_MANUAL_CONFIRMATION
        )
        is VerificationStatus.UNABLE_TO_VERIFY
    )


def test_resolve_status_pass_is_terminal() -> None:
    for incoming in VerificationStatus:
        assert (
            resolve_status(VerificationStatus.VERIFIED_PASS, incoming)
            is VerificationStatus.VERIFIED_PASS
        )


def test_resolve_status_none_and_pending_progress() -> None:
    assert (
        resolve_status(None, VerificationStatus.VERIFIED_PASS)
        is VerificationStatus.VERIFIED_PASS
    )
    assert (
        resolve_status(
            VerificationStatus.PENDING_IDENTIFICATION,
            VerificationStatus.PENDING_VERIFICATION,
        )
        is VerificationStatus.PENDING_VERIFICATION
    )


def test_daily_verify_limit_matches_w0_4() -> None:
    assert DAILY_VERIFY_LIMIT == 5


# --- 证据脱敏 -----------------------------------------------------------------


def test_sanitize_evidence_masks_invoice_and_tax_identifiers() -> None:
    raw = {
        "Response": {
            "Invoice": {
                "Number": "12345678",
                "Code": "011002000211",
                "CheckCode": "654321",
                "SellerTaxCode": "91310000MA1FL0XXXX",
                "BuyerTaxCode": "91320000MA1FL0YYYY",
                "AmountWithoutTax": "100.00",
                "Date": "2026-10-16",
            },
            "RequestId": "req-1",
        }
    }
    sanitized = sanitize_evidence(raw)
    assert sanitized is not None
    invoice = sanitized["Response"]["Invoice"]
    assert invoice["Number"] == "****5678"
    assert invoice["Code"] == "****0211"
    assert invoice["CheckCode"] == "****4321"
    assert invoice["SellerTaxCode"] == "****XXXX"
    assert invoice["BuyerTaxCode"] == "****YYYY"
    # 金额与日期是 C 类，可保留。
    assert invoice["AmountWithoutTax"] == "100.00"
    assert invoice["Date"] == "2026-10-16"
    assert sanitized["Response"]["RequestId"] == "req-1"
    # 原文不被修改。
    assert raw["Response"]["Invoice"]["Number"] == "12345678"


def test_sanitize_evidence_drops_credentials_and_payloads() -> None:
    sanitized = sanitize_evidence(
        {
            "SecretId": "AKIDshould-not-appear",
            "secret_key": "should-not-appear",
            "AccessToken": "should-not-appear",
            "fileContent": "JVBERi0xLjQK",
            "body": "raw body",
            "safe": "kept",
        }
    )
    assert sanitized is not None
    assert sanitized["SecretId"] == "***redacted***"
    assert sanitized["secret_key"] == "***redacted***"
    assert sanitized["AccessToken"] == "***redacted***"
    assert "fileContent" not in sanitized
    assert "body" not in sanitized
    assert sanitized["safe"] == "kept"
    joined = str(sanitized)
    assert "should-not-appear" not in joined
    assert "JVBERi0xLjQK" not in joined


def test_sanitize_evidence_handles_none_and_nested_lists() -> None:
    assert sanitize_evidence(None) is None
    sanitized = sanitize_evidence({"Items": [{"Number": "87654321"}]})
    assert sanitized == {"Items": [{"Number": "****4321"}]}


def test_evidence_ref_is_content_addressed() -> None:
    a = evidence_ref_for({"b": 1, "a": 2})
    b = evidence_ref_for({"a": 2, "b": 1})
    assert a == b
    assert a.startswith("sha256:")
    assert len(a) == len("sha256:") + 64
    assert evidence_ref_for({"a": 3}) != a
