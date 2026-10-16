"""腾讯云 ``VatInvoiceVerifyNew`` 适配器测试（**全部使用本地桩，绝不触网**）。

覆盖 W1-20 第二步与第五步：

* TC3-HMAC-SHA256 签名确定性（固定时间戳 → 同输入同签名）；
* 入参按票种条件构造；
* 成功响应解析与票面字段提取；
* 票面不一致 → 查验不通过；
* 作废 / 红字标志 → 查验不通过；
* W0-4 §2.4 的 11 个错误码 → 状态映射（+ 未知码 fail closed）；
* 超时与限频（``CountLimitError``）；
* 凭据缺失 fail closed（且不泄露凭据值）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from app.integrations.invoice_verification import (
    VerificationStatus,
)
from app.integrations.tencent_vat_verify import (
    ERROR_CODE_STATUS,
    SECRET_ID_ENV,
    SECRET_KEY_ENV,
    TENCENT_ACTION,
    TENCENT_ALGORITHM,
    TENCENT_ENDPOINT,
    TENCENT_HOST,
    TENCENT_VERSION,
    HttpResponse,
    MissingCredentialError,
    TencentVatInvoiceVerifyProvider,
    TransportError,
    TransportTimeout,
    map_error_code,
    sign_request,
)

from app.tests.integrations.invoice_fixtures import FIXED_NOW, make_fields

SECRET_ID = "AKIDexample-secret-id-0001"
SECRET_KEY = "example-secret-key-do-not-log-9999"
FIXED_TS = 1_763_000_000  # 2025-11-15T06:13:20Z


@dataclass
class RecordingTransport:
    """记录调用并返回预设响应的桩传输（测试专用，不发起任何网络请求）。"""

    response: HttpResponse | None = None
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _ok_response(
    invoice: dict[str, object],
    *,
    request_id: str = "req-0001",
    status_code: int = 200,
) -> HttpResponse:
    body = {
        "Response": {
            "RequestId": request_id,
            "Invoice": invoice,
        }
    }
    return HttpResponse(status_code, json.dumps(body).encode("utf-8"))


def _error_response(code: str, *, request_id: str = "req-err") -> HttpResponse:
    body = {
        "Response": {
            "Error": {"Code": code, "Message": "provider message"},
            "RequestId": request_id,
        }
    }
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


def _provider(transport: RecordingTransport) -> TencentVatInvoiceVerifyProvider:
    return TencentVatInvoiceVerifyProvider(
        secret_id=SECRET_ID,
        secret_key=SECRET_KEY,
        transport=transport,
        clock=lambda: FIXED_NOW,
    )


# --- 签名确定性 ---------------------------------------------------------------


def test_sign_request_is_deterministic_for_fixed_timestamp() -> None:
    payload = b'{"InvoiceNo":"12345678"}'
    first = sign_request(
        secret_id=SECRET_ID,
        secret_key=SECRET_KEY,
        host=TENCENT_HOST,
        service="ocr",
        action=TENCENT_ACTION,
        payload=payload,
        timestamp=FIXED_TS,
    )
    second = sign_request(
        secret_id=SECRET_ID,
        secret_key=SECRET_KEY,
        host=TENCENT_HOST,
        service="ocr",
        action=TENCENT_ACTION,
        payload=payload,
        timestamp=FIXED_TS,
    )
    assert first == second
    assert first.string_to_sign == second.string_to_sign
    assert first.signature == second.signature
    assert first.timestamp == FIXED_TS

    # 待签串结构：算法 / 时间戳 / 凭证范围 / 规范请求哈希。
    lines = first.string_to_sign.split("\n")
    assert lines[0] == TENCENT_ALGORITHM
    assert lines[1] == str(FIXED_TS)
    assert lines[2] == f"{first.date}/ocr/tc3_request"
    assert len(lines) == 4

    # 规范请求：POST + / + 空查询串 + 规范头 + 签名头 + 载荷哈希。
    canonical = first.canonical_request.split("\n")
    assert canonical[0] == "POST"
    assert canonical[1] == "/"
    assert canonical[2] == ""
    assert "content-type:application/json; charset=utf-8" in canonical
    assert f"host:{TENCENT_HOST}" in canonical
    assert f"x-tc-action:{TENCENT_ACTION.lower()}" in canonical
    assert first.signed_headers == "content-type;host;x-tc-action"

    # Authorization 结构与「不含 SecretKey」。
    assert first.authorization.startswith(f"{TENCENT_ALGORITHM} Credential={SECRET_ID}/")
    assert f"SignedHeaders={first.signed_headers}" in first.authorization
    assert f"Signature={first.signature}" in first.authorization
    assert SECRET_KEY not in first.authorization
    assert SECRET_KEY not in first.string_to_sign


def test_sign_request_changes_with_timestamp_and_payload() -> None:
    base = dict(
        secret_id=SECRET_ID,
        secret_key=SECRET_KEY,
        host=TENCENT_HOST,
        service="ocr",
        action=TENCENT_ACTION,
    )
    a = sign_request(payload=b"x", timestamp=FIXED_TS, **base)
    b = sign_request(payload=b"x", timestamp=FIXED_TS + 1, **base)
    c = sign_request(payload=b"y", timestamp=FIXED_TS, **base)
    assert a.signature != b.signature
    assert a.signature != c.signature


def test_provider_signs_reproducibly_with_fixed_clock() -> None:
    transport = RecordingTransport(
        response=_ok_response({"Number": "12345678", "Code": "011002000211"})
    )
    provider = _provider(transport)
    fields = make_fields()
    provider.verify(fields)
    provider.verify(fields)
    assert len(transport.calls) == 2
    first_auth = transport.calls[0]["headers"]["Authorization"]  # type: ignore[index]
    second_auth = transport.calls[1]["headers"]["Authorization"]  # type: ignore[index]
    assert first_auth == second_auth
    headers = transport.calls[0]["headers"]  # type: ignore[index]
    assert headers["X-TC-Action"] == TENCENT_ACTION
    assert headers["X-TC-Version"] == TENCENT_VERSION
    assert headers["Host"] == TENCENT_HOST
    assert transport.calls[0]["url"] == TENCENT_ENDPOINT


# --- 入参构造 -----------------------------------------------------------------


def test_build_params_sends_only_provided_fields() -> None:
    minimal = TencentVatInvoiceVerifyProvider.build_params(
        make_fields(invoice_code=None, invoice_kind=None, amount=None)
    )
    assert minimal == {"InvoiceNo": "12345678", "InvoiceDate": "2026-10-16"}
    # 不传 EnableTodayInvoice / EnableCommonElectronic（保持渠道默认：当日票不支持）。
    assert "EnableTodayInvoice" not in minimal
    assert "EnableCommonElectronic" not in minimal


def test_build_params_includes_conditional_fields_by_kind() -> None:
    params = TencentVatInvoiceVerifyProvider.build_params(
        make_fields(
            invoice_kind="04",
            check_code="654321",
            region_code="4400",
            seller_tax_code="91310000MA1FL0XXXX",
            amount="100.00",
        )
    )
    assert params == {
        "InvoiceNo": "12345678",
        "InvoiceDate": "2026-10-16",
        "InvoiceCode": "011002000211",
        "InvoiceKind": "04",
        "CheckCode": "654321",
        "Amount": "100.00",
        "RegionCode": "4400",
        "SellerTaxCode": "91310000MA1FL0XXXX",
    }
    # 定点金额格式（不出现科学计数法）。
    assert "E" not in str(params["Amount"])


# --- 成功解析 -----------------------------------------------------------------


def test_success_response_parses_face_fields() -> None:
    transport = RecordingTransport(
        response=_ok_response(
            {
                "Number": "12345678",
                "Code": "011002000211",
                "Date": "2026-10-16",
                "SellerTaxCode": "91310000MA1FL0XXXX",
                "BuyerTaxCode": "91320000MA1FL0YYYY",
                "AmountWithoutTax": "100.00",
                "AmountWithTax": "113.00",
                "IsAbandoned": "N",
                "RedLetterInvoiceMark": False,
                "ElectronicType": "1",
                "Items": [{"Name": "服务费"}],
            }
        )
    )
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.VERIFIED_PASS
    assert result.provider_ref == "req-0001"
    assert result.error_code is None
    face = result.face
    assert face.invoice_no == "12345678"
    assert face.invoice_code == "011002000211"
    assert face.invoice_date.isoformat() == "2026-10-16"
    assert face.seller_tax_code == "91310000MA1FL0XXXX"
    assert face.buyer_tax_code == "91320000MA1FL0YYYY"
    assert str(face.amount_without_tax) == "100.00"
    assert str(face.amount_with_tax) == "113.00"
    assert face.is_abandoned is False
    assert face.is_red_letter is False
    assert face.items == ({"Name": "服务费"},)
    # W0-4 §2.3：该页未列出独立税额/币种字段，故不臆造。
    assert face.tax_amount is None
    assert face.currency is None


def test_face_mismatch_is_verified_fail() -> None:
    transport = RecordingTransport(
        response=_ok_response(
            {"Number": "99999999", "Code": "011002000211", "AmountWithoutTax": "100.00"}
        )
    )
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == "face_mismatch:invoice_no"
    assert result.error_code is None


def test_amount_mismatch_is_verified_fail() -> None:
    transport = RecordingTransport(
        response=_ok_response(
            {
                "Number": "12345678",
                "AmountWithoutTax": "200.00",
                "AmountWithTax": "226.00",
            }
        )
    )
    result = _provider(transport).verify(make_fields(amount="100.00"))
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == "face_mismatch:amount"


def test_amount_matches_with_tax_total_for_full_electronic() -> None:
    """全电发票的 ``Amount`` 是价税合计，与 ``AmountWithTax`` 相等即一致。"""
    transport = RecordingTransport(
        response=_ok_response(
            {
                "Number": "12345678",
                "AmountWithoutTax": "88.50",
                "AmountWithTax": "100.00",
            }
        )
    )
    result = _provider(transport).verify(make_fields(amount="100.00"))
    assert result.status is VerificationStatus.VERIFIED_PASS


@pytest.mark.parametrize(
    ("invoice", "reason"),
    [
        ({"Number": "12345678", "IsAbandoned": "Y"}, "invoice_abandoned"),
        ({"Number": "12345678", "RedLetterInvoiceMark": True}, "red_letter_invoice"),
    ],
)
def test_void_and_red_letter_are_verified_fail(
    invoice: dict[str, object], reason: str
) -> None:
    transport = RecordingTransport(response=_ok_response(invoice))
    result = _provider(transport).verify(make_fields(invoice_code=None, amount=None))
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == reason


# --- 错误码映射 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("FailedOperation.InvoiceMismatch", VerificationStatus.VERIFIED_FAIL),
        ("FailedOperation.ArrearsError", VerificationStatus.UNABLE_TO_VERIFY),
        ("FailedOperation.CountLimitError", VerificationStatus.UNABLE_TO_VERIFY),
        ("FailedOperation.UnKnowError", VerificationStatus.UNABLE_TO_VERIFY),
        ("FailedOperation.UnOpenError", VerificationStatus.UNABLE_TO_VERIFY),
        ("ResourceNotFound.NoAreaCode", VerificationStatus.UNABLE_TO_VERIFY),
        ("ResourceNotFound.NoInvoice", VerificationStatus.UNABLE_TO_VERIFY),
        (
            "ResourceNotFound.NotSupportCurrentInvoiceQuery",
            VerificationStatus.UNABLE_TO_VERIFY,
        ),
        ("ResourceUnavailable.InArrears", VerificationStatus.UNABLE_TO_VERIFY),
        (
            "ResourceUnavailable.ResourcePackageRunOut",
            VerificationStatus.UNABLE_TO_VERIFY,
        ),
        ("ResourceUnavailable.TaxNetworkError", VerificationStatus.UNABLE_TO_VERIFY),
    ],
)
def test_error_code_mapping(code: str, expected: VerificationStatus) -> None:
    assert map_error_code(code) is expected
    transport = RecordingTransport(response=_error_response(code))
    result = _provider(transport).verify(make_fields())
    assert result.status is expected
    assert result.error_code == code
    assert result.provider_ref == "req-err"
    # 证据原样带回（落库前由服务层脱敏）。
    assert result.evidence is not None


def test_all_documented_error_codes_are_covered() -> None:
    """W0-4 §2.4 列出的 11 个错误码全部在映射表内。"""
    documented = {
        "FailedOperation.ArrearsError",
        "FailedOperation.CountLimitError",
        "FailedOperation.InvoiceMismatch",
        "FailedOperation.UnKnowError",
        "FailedOperation.UnOpenError",
        "ResourceNotFound.NoAreaCode",
        "ResourceNotFound.NoInvoice",
        "ResourceNotFound.NotSupportCurrentInvoiceQuery",
        "ResourceUnavailable.InArrears",
        "ResourceUnavailable.ResourcePackageRunOut",
        "ResourceUnavailable.TaxNetworkError",
    }
    assert documented <= set(ERROR_CODE_STATUS)
    assert len(documented) == 11


@pytest.mark.parametrize("code", ["Something.Unknown", "", None])
def test_unknown_error_code_fails_closed(code: str | None) -> None:
    assert map_error_code(code) is VerificationStatus.UNABLE_TO_VERIFY
    transport = RecordingTransport(response=_error_response(code or "Something.Unknown"))
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.status is not VerificationStatus.VERIFIED_PASS


def test_http_error_status_without_error_body_is_unable() -> None:
    transport = RecordingTransport(
        response=HttpResponse(500, json.dumps({"Response": {}}).encode("utf-8"))
    )
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "HTTP500"


def test_invalid_json_is_unable() -> None:
    transport = RecordingTransport(response=HttpResponse(200, b"<html>not json</html>"))
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "InvalidResponse"


# --- 超时与限频 ---------------------------------------------------------------


def test_timeout_is_unable_and_not_retried() -> None:
    transport = RecordingTransport(error=TransportTimeout("boom"))
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "Timeout"
    assert result.reason == "provider_timeout"
    # 超时绝不盲目重发：只尝试一次。
    assert len(transport.calls) == 1


def test_transport_error_is_unable_and_not_retried() -> None:
    transport = RecordingTransport(error=TransportError("conn reset"))
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "TransportError"
    assert len(transport.calls) == 1


def test_rate_limit_error_is_unable() -> None:
    transport = RecordingTransport(
        response=_error_response("FailedOperation.CountLimitError")
    )
    result = _provider(transport).verify(make_fields())
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "FailedOperation.CountLimitError"


# --- 凭据 fail closed ---------------------------------------------------------


def test_missing_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRET_ID_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    with pytest.raises(MissingCredentialError) as exc:
        TencentVatInvoiceVerifyProvider()
    message = str(exc.value)
    assert SECRET_ID_ENV in message
    assert SECRET_KEY_ENV in message


def test_missing_secret_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ID_ENV, SECRET_ID)
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    with pytest.raises(MissingCredentialError) as exc:
        TencentVatInvoiceVerifyProvider()
    assert SECRET_KEY_ENV in str(exc.value)
    # 错误信息只提环境变量名，不含凭据值。
    assert SECRET_ID not in str(exc.value)


def test_blank_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ID_ENV, "   ")
    monkeypatch.setenv(SECRET_KEY_ENV, "   ")
    with pytest.raises(MissingCredentialError):
        TencentVatInvoiceVerifyProvider()


def test_provider_reads_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SECRET_ID_ENV, SECRET_ID)
    monkeypatch.setenv(SECRET_KEY_ENV, SECRET_KEY)
    transport = RecordingTransport(response=_ok_response({"Number": "12345678"}))
    provider = TencentVatInvoiceVerifyProvider(
        transport=transport, clock=lambda: FIXED_NOW
    )
    result = provider.verify(make_fields(invoice_code=None, amount=None))
    assert result.status is VerificationStatus.VERIFIED_PASS
    # Authorization 含 SecretId（协议要求）但绝不含 SecretKey。
    auth = transport.calls[0]["headers"]["Authorization"]  # type: ignore[index]
    assert SECRET_ID in auth
    assert SECRET_KEY not in auth
