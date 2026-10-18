"""百度智能云「增值税发票验真」适配器测试（**全部使用本地桩，绝不触网**）。

覆盖 W1-28 交付 2：

* ``build_params`` 只传六个文档化参数、按票种条件裁剪（不臆造、不多传）；
* token（OAuth2.0）获取、缓存、过期重取与失败映射（不重发、不泄凭据）；
* 成功响应（``0001`` + ``InvalidSign=N``）解析与票面字段映射；
* 票面不一致 / 金额不一致 / 作废与红冲（``Y``/``H``/``BH``/``QH``）→ 查验不通过；
* W1-28 §2.4 的 14 个结果码 + 计费列 → 状态映射（未知码 fail closed）；
* 超时/传输失败 → ``UNABLE_TO_VERIFY`` 且只尝试一次；
* 凭据缺失 fail closed（错误信息不含凭据值）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

import pytest

from app.integrations.baidu_vat_verify import (
    API_KEY_ENV,
    BAIDU_ENDPOINT,
    BAIDU_TOKEN_ENDPOINT,
    DEFAULT_TIMEOUT_SECONDS,
    RESULT_CODE_TABLE,
    SECRET_KEY_ENV,
    BaiduVatInvoiceVerifyProvider,
    HttpResponse,
    MissingCredentialError,
    TransportError,
    TransportTimeout,
    is_result_code_billed,
    map_result_code,
)
from app.integrations.invoice_verification import VerificationStatus

from app.tests.integrations.invoice_fixtures import FIXED_NOW, make_fields

API_KEY = "example-api-key-do-not-log-0001"
SECRET_KEY = "example-secret-key-do-not-log-9999"
ACCESS_TOKEN = "24.example-access-token-0001.2592000.1763000000.282335-00000001"
TOKEN_EXPIRES_IN = 2_592_000  # 30 天（文档：Access Token 默认有效期 30 天）
LOG_ID = "1394226734160674816"


@dataclass
class RecordingTransport:
    """记录调用并按队列返回预设响应/异常的桩传输（测试专用，不发起网络请求）。

    ``results`` 中的元素可为 ``HttpResponse`` 或异常实例；队列只剩一项时重复使用，
    便于「固定响应」与「token + 查验」两种序列断言。**绝不触网**。
    """

    results: list[object] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

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
        if not self.results:
            raise AssertionError("recording transport has no queued response")
        item = self.results[0] if len(self.results) == 1 else self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResponse)
        return item


def _token_response(
    token: str = ACCESS_TOKEN,
    *,
    expires_in: int | None = TOKEN_EXPIRES_IN,
    status_code: int = 200,
) -> HttpResponse:
    body: dict[str, object] = {"access_token": token}
    if expires_in is not None:
        body["expires_in"] = expires_in
    return HttpResponse(status_code, json.dumps(body).encode("utf-8"))


def _token_error_response(
    error: str = "invalid_client",
    description: str = "unknown client id",
) -> HttpResponse:
    body = {"error": error, "error_description": description}
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


def _verify_response(
    words: Mapping[str, object],
    *,
    log_id: str | None = LOG_ID,
    status_code: int = 200,
    extra: Mapping[str, object] | None = None,
) -> HttpResponse:
    body: dict[str, object] = {
        "words_result_num": len(words),
        "words_result": dict(words),
    }
    if log_id is not None:
        body["log_id"] = log_id
    if extra:
        body.update(extra)
    return HttpResponse(status_code, json.dumps(body).encode("utf-8"))


def _matching_words(**overrides: object) -> dict[str, object]:
    """与 ``make_fields()`` 默认要素完全一致的票面（可覆盖任意字段）。"""
    words: dict[str, object] = {
        "VerifyResult": "0001",
        "VerifyMessage": "查验成功发票一致",
        "VerifyFrequency": "1",
        "InvalidSign": "N",
        "InvoiceCode": "011002000211",
        "InvoiceNum": "12345678",
        "InvoiceDate": "20261016",
        "SellerRegisterNum": "91310000MA1FL0XXXX",
        "PurchaserRegisterNum": "91320000MA1FL0YYYY",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
        "AmountInFiguers": "113.00",
    }
    words.update(overrides)
    return words


def _real_top(**overrides: object) -> dict[str, object]:
    """#57 实测**真实结构**的顶层结论/身份字段（可覆盖任意字段）。

    #57 跑批的 8/8 渠道响应把 ``VerifyResult``/``InvalidSign``/``InvoiceCode``/
    ``InvoiceNum``/``InvoiceDate``/``InvoiceType``/``CheckCode``/``MachineCode``/
    ``VerifyFrequency``/``VerifyMessage`` 放在**顶层**。此处只用合成占位值
    （**非真实业务值**）。
    """
    top: dict[str, object] = {
        "VerifyResult": "0001",
        "VerifyMessage": "查验成功发票一致",
        "VerifyFrequency": "1",
        "InvalidSign": "N",
        "InvoiceCode": "011002000211",
        "InvoiceNum": "12345678",
        "InvoiceDate": "20261016",
        "InvoiceType": "增值税普通发票（电子）",
        "CheckCode": "654321",
        "MachineCode": "661603113416",
    }
    top.update(overrides)
    return top


def _real_words(**overrides: object) -> dict[str, object]:
    """#57 实测**真实结构**的 ``words_result`` 票面明细（合成占位值）。"""
    words: dict[str, object] = {
        "SellerName": "示例销售方",
        "SellerRegisterNum": "91310000MA1FL0XXXX",
        "PurchaserName": "示例购买方",
        "PurchaserRegisterNum": "91320000MA1FL0YYYY",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
        "AmountInFiguers": "113.00",
    }
    words.update(overrides)
    return words


def _real_verify_response(
    *,
    top: Mapping[str, object] | None = None,
    words: Mapping[str, object] | None = None,
    log_id: str | None = LOG_ID,
    status_code: int = 200,
) -> HttpResponse:
    """构造 #57 实测**真实响应结构**：结论/身份字段在顶层、票面明细在 ``words_result``。"""
    body: dict[str, object] = {
        "words_result_num": len(words or {}),
        "words_result": dict(words or {}),
    }
    if top:
        body.update(top)
    if log_id is not None:
        body["log_id"] = log_id
    return HttpResponse(status_code, json.dumps(body).encode("utf-8"))


def _provider(transport: RecordingTransport) -> BaiduVatInvoiceVerifyProvider:
    return BaiduVatInvoiceVerifyProvider(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        transport=transport,
        clock=lambda: FIXED_NOW,
    )


# --- 入参构造 -----------------------------------------------------------------


def test_build_params_sends_only_documented_parameters() -> None:
    params = BaiduVatInvoiceVerifyProvider.build_params(
        make_fields(
            invoice_kind="normal_invoice",
            check_code="654321",
            amount="100.00",
            # 腾讯云特有的字段即便被填了也不得出现在百度请求里（不多传、不臆造）。
            region_code="4400",
            seller_tax_code="91310000MA1FL0XXXX",
        )
    )
    assert params == {
        "invoice_num": "12345678",
        "invoice_date": "20261016",
        "invoice_type": "normal_invoice",
        "invoice_code": "011002000211",
        "check_code": "654321",
        "total_amount": "100.00",
    }
    assert "region_code" not in params
    assert "RegionCode" not in params
    assert "seller_tax_code" not in params
    assert "SellerTaxCode" not in params


def test_build_params_uses_empty_string_for_absent_conditional_fields() -> None:
    """文档将六参数均标为必选、并按票种标注「可为空」：缺省以空串表达，不臆造。"""
    params = BaiduVatInvoiceVerifyProvider.build_params(
        make_fields(
            invoice_kind="elec_invoice_special",
            invoice_code=None,
            check_code=None,
            amount=None,
        )
    )
    assert params["invoice_code"] == ""
    assert params["check_code"] == ""
    assert params["total_amount"] == ""
    assert params["invoice_type"] == "elec_invoice_special"


def test_build_params_omits_invoice_type_when_kind_absent() -> None:
    """``invoice_type`` 无「可为空」例外：缺失时不做空串兜底（由 verify fail closed）。"""
    params = BaiduVatInvoiceVerifyProvider.build_params(
        make_fields(invoice_kind=None, amount=None)
    )
    assert "invoice_type" not in params


def test_build_params_formats_date_and_amount_as_documented() -> None:
    params = BaiduVatInvoiceVerifyProvider.build_params(make_fields(amount="1300"))
    assert params["invoice_date"] == "20261016"  # 文档：格式 YYYYMMDD
    assert params["total_amount"] == "1300.00"  # 定点格式，无科学计数法
    assert "E" not in params["total_amount"]


# --- 请求线路格式 --------------------------------------------------------------


def test_verify_sends_form_encoded_request_with_token_in_url() -> None:
    transport = RecordingTransport(
        results=[_token_response(), _verify_response(_matching_words())]
    )
    provider = _provider(transport)
    provider.verify(make_fields(invoice_kind="normal_invoice", check_code="654321"))

    # 第一次调用是 token 获取：POST、空体、参数在 URL。
    token_call = transport.calls[0]
    token_url = str(token_call["url"])
    assert token_url.startswith(f"{BAIDU_TOKEN_ENDPOINT}?")
    assert "grant_type=client_credentials" in token_url
    assert f"client_id={API_KEY}" in token_url
    assert f"client_secret={SECRET_KEY}" in token_url
    assert token_call["body"] == b""

    # 第二次调用是查验：access_token 在 URL，表单体为六个文档化参数。
    verify_call = transport.calls[1]
    assert verify_call["url"] == f"{BAIDU_ENDPOINT}?access_token={ACCESS_TOKEN}"
    assert (
        verify_call["headers"]["Content-Type"]
        == "application/x-www-form-urlencoded"
    )
    assert verify_call["timeout"] == DEFAULT_TIMEOUT_SECONDS
    parsed = parse_qs(str(verify_call["body"].decode("utf-8")))
    assert parsed == {
        "invoice_num": ["12345678"],
        "invoice_date": ["20261016"],
        "invoice_type": ["normal_invoice"],
        "invoice_code": ["011002000211"],
        "check_code": ["654321"],
        "total_amount": ["100.00"],
    }
    # 凭据（API Key / Secret Key）绝不进入查验请求。
    assert SECRET_KEY not in str(verify_call["url"])
    assert API_KEY not in str(verify_call["url"])


# --- 成功解析 -----------------------------------------------------------------


def test_success_response_parses_face_fields() -> None:
    transport = RecordingTransport(
        results=[_token_response(), _verify_response(_matching_words())]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_PASS
    assert result.provider_ref == LOG_ID
    assert result.error_code is None
    assert result.reason is None
    face = result.face
    assert face.invoice_code == "011002000211"
    assert face.invoice_no == "12345678"
    assert face.invoice_date is not None
    assert face.invoice_date.isoformat() == "2026-10-16"
    assert face.seller_tax_code == "91310000MA1FL0XXXX"
    assert face.buyer_tax_code == "91320000MA1FL0YYYY"
    assert str(face.amount_without_tax) == "100.00"
    assert str(face.tax_amount) == "13.00"
    assert str(face.amount_with_tax) == "113.00"
    assert face.is_abandoned is False
    assert face.is_red_letter is False
    # 文档未列出币种 / 「电子类型」字段 → 不臆造。
    assert face.currency is None
    assert face.electronic_type is None
    assert face.items == ()
    # 证据为原始响应（脱敏由服务层负责）；契约层无专属槽位的字段
    # （VerifyMessage / VerifyFrequency 等）原样保留在其中。
    assert result.evidence is not None
    assert "words_result" in result.evidence
    words = result.evidence["words_result"]
    assert words["VerifyMessage"] == "查验成功发票一致"
    assert words["VerifyFrequency"] == "1"


def test_log_id_nested_in_words_result_is_used() -> None:
    """文档示例中 ``log_id`` 也有嵌在 ``words_result`` 内的形态。"""
    words = _matching_words(log_id="2077691831063187864")
    transport = RecordingTransport(
        results=[_token_response(), _verify_response(words, log_id=None)]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_PASS
    assert result.provider_ref == "2077691831063187864"


# --- 真实响应层级（#57 实测结构 → #58 修正） ----------------------------------


def test_real_structure_top_level_conclusion_is_verified_pass() -> None:
    """#57 真实结构（结论/身份在顶层、票面明细在 words_result）→ 查验通过。"""
    transport = RecordingTransport(
        results=[
            _token_response(),
            _real_verify_response(top=_real_top(), words=_real_words()),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_PASS
    assert result.provider_ref == LOG_ID
    assert result.error_code is None
    assert result.reason is None
    face = result.face
    # 身份字段取自顶层。
    assert face.invoice_code == "011002000211"
    assert face.invoice_no == "12345678"
    assert face.invoice_date is not None
    assert face.invoice_date.isoformat() == "2026-10-16"
    # 票面明细取自 words_result。
    assert face.seller_tax_code == "91310000MA1FL0XXXX"
    assert face.buyer_tax_code == "91320000MA1FL0YYYY"
    assert str(face.amount_without_tax) == "100.00"
    assert str(face.tax_amount) == "13.00"
    assert str(face.amount_with_tax) == "113.00"
    assert face.is_abandoned is False
    assert face.is_red_letter is False
    # 顶层结论字段（VerifyMessage / VerifyFrequency 等）原样保留在 evidence。
    assert result.evidence is not None
    assert result.evidence["VerifyMessage"] == "查验成功发票一致"
    assert result.evidence["VerifyFrequency"] == "1"


def test_real_structure_top_level_identity_drives_face_mismatch() -> None:
    """顶层 ``InvoiceNum`` 与提交要素不符 → 查验不通过（身份字段确实参与比对）。"""
    transport = RecordingTransport(
        results=[
            _token_response(),
            _real_verify_response(
                top=_real_top(InvoiceNum="99999999"), words=_real_words()
            ),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == "face_mismatch:invoice_no"
    assert result.error_code is None


@pytest.mark.parametrize("sign", ["Y", "H", "BH", "QH"])
def test_real_structure_top_level_invalid_sign_vetoes(sign: str) -> None:
    """顶层 ``InvalidSign`` 的作废/红冲取值同样触发查验不通过。"""
    transport = RecordingTransport(
        results=[
            _token_response(),
            _real_verify_response(
                top=_real_top(InvalidSign=sign), words=_real_words()
            ),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == f"invalid_sign:{sign}"
    if sign == "Y":
        assert result.face.is_abandoned is True
    else:
        assert result.face.is_red_letter is True


def test_top_level_value_takes_precedence_over_words_result() -> None:
    """顶层与 ``words_result`` 同时给出时以**顶层**为准（顶层优先）。"""
    transport = RecordingTransport(
        results=[
            _token_response(),
            _real_verify_response(
                top=_real_top(VerifyResult="0006", InvalidSign="Y"),
                words=_real_words(VerifyResult="0001", InvalidSign="N"),
            ),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    # 顶层 ``0006``（发票信息不一致）胜出，而非 words_result 内的 ``0001``。
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == "invoice_info_mismatch"
    assert result.error_code == "0006"


def test_real_structure_missing_invalid_sign_fails_closed() -> None:
    """真实结构下 ``InvalidSign`` 两处都缺 → 维持 fail closed（不臆断为正常）。"""
    top = _real_top()
    top.pop("InvalidSign")
    transport = RecordingTransport(
        results=[_token_response(), _real_verify_response(top=top, words=_real_words())]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.reason == "invalid_sign_unknown"


def test_real_structure_without_words_result_uses_top_level_conclusion() -> None:
    """真实失败响应可能只有顶层字段（无 ``words_result``）→ 仍按顶层结论映射。"""
    transport = RecordingTransport(
        results=[
            _token_response(),
            _real_verify_response(top=_real_top(VerifyResult="0005"), words=None),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "0005"
    assert result.reason == "verify_result:0005"


def test_missing_verify_result_in_both_layers_fails_closed() -> None:
    """顶层与 ``words_result`` 都缺 ``VerifyResult`` 且无顶层 ``error_code`` → fail closed。"""
    top = _real_top()
    top.pop("VerifyResult")
    transport = RecordingTransport(
        results=[_token_response(), _real_verify_response(top=top, words=_real_words())]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "InvalidResponse"
    assert result.reason == "provider_invalid_response"


def test_words_result_nested_conclusion_remains_supported() -> None:
    """旧结构（结论嵌在 ``words_result`` 内）保持兼容。"""
    transport = RecordingTransport(
        results=[_token_response(), _verify_response(_matching_words())]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_PASS
    assert result.face.invoice_no == "12345678"


# --- 票面一致性 ---------------------------------------------------------------


def test_face_mismatch_is_verified_fail() -> None:
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response(_matching_words(InvoiceNum="99999999")),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == "face_mismatch:invoice_no"
    assert result.error_code is None


def test_amount_mismatch_is_verified_fail() -> None:
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response(
                _matching_words(TotalAmount="200.00", AmountInFiguers="226.00")
            ),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == "face_mismatch:amount"


def test_amount_matches_with_tax_total_for_full_electronic() -> None:
    """全电发票的 ``total_amount`` 是价税合计，与 ``AmountInFiguers`` 相等即一致。"""
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response(
                _matching_words(TotalAmount="88.50", AmountInFiguers="100.00")
            ),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_PASS


# --- InvalidSign（发票状态） ---------------------------------------------------


@pytest.mark.parametrize("sign", ["Y", "H", "BH", "QH"])
def test_invalid_sign_fail_values_are_verified_fail(sign: str) -> None:
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response(_matching_words(InvalidSign=sign)),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_FAIL
    assert result.reason == f"invalid_sign:{sign}"
    assert result.error_code is None
    if sign == "Y":
        assert result.face.is_abandoned is True
    else:  # H / BH / QH 均为红冲类
        assert result.face.is_red_letter is True


@pytest.mark.parametrize("sign", [None, "n", "X", ""])
def test_missing_or_unknown_invalid_sign_fails_closed(sign: str | None) -> None:
    words = _matching_words()
    if sign is None:
        words.pop("InvalidSign")
    else:
        words["InvalidSign"] = sign
    transport = RecordingTransport(
        results=[_token_response(), _verify_response(words)]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.reason == "invalid_sign_unknown"
    assert result.status is not VerificationStatus.VERIFIED_PASS


# --- 结果码映射 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("0001", VerificationStatus.VERIFIED_PASS),
        ("0002", VerificationStatus.UNABLE_TO_VERIFY),
        ("0005", VerificationStatus.UNABLE_TO_VERIFY),
        ("0006", VerificationStatus.VERIFIED_FAIL),
        ("0009", VerificationStatus.UNABLE_TO_VERIFY),
        ("1005", VerificationStatus.UNABLE_TO_VERIFY),
        ("1006", VerificationStatus.UNABLE_TO_VERIFY),
        ("1008", VerificationStatus.UNABLE_TO_VERIFY),
        ("1009", VerificationStatus.UNABLE_TO_VERIFY),
        ("1014", VerificationStatus.UNABLE_TO_VERIFY),
        ("1015", VerificationStatus.UNABLE_TO_VERIFY),
        ("1020", VerificationStatus.UNABLE_TO_VERIFY),
        ("1021", VerificationStatus.UNABLE_TO_VERIFY),
        ("9999", VerificationStatus.UNABLE_TO_VERIFY),
    ],
)
def test_result_code_mapping(code: str, expected: VerificationStatus) -> None:
    assert map_result_code(code) is expected
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response({"VerifyResult": code, "InvalidSign": "N"}),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is expected
    if expected is VerificationStatus.UNABLE_TO_VERIFY:
        assert result.error_code == code
        assert result.reason == f"verify_result:{code}"
    elif expected is VerificationStatus.VERIFIED_FAIL:
        assert result.error_code == code  # 0006：渠道码表明票面不一致
        assert result.reason == "invoice_info_mismatch"
    assert result.evidence is not None


def test_all_documented_result_codes_are_covered() -> None:
    """W1-28 §2.4 文档列出的 14 个结果码全部在映射表内。"""
    documented = {
        "0001",
        "0002",
        "0005",
        "0006",
        "0009",
        "1005",
        "1006",
        "1008",
        "1009",
        "1014",
        "1015",
        "1020",
        "1021",
        "9999",
    }
    assert documented == set(RESULT_CODE_TABLE)
    assert len(documented) == 14


def test_result_code_billing_flags_match_document() -> None:
    """「是否计费」列逐码对齐（是：0001/0006/0009/1005；其余否）。"""
    billed = {"0001", "0006", "0009", "1005"}
    for code, spec in RESULT_CODE_TABLE.items():
        assert spec.billed is (code in billed), code
    assert is_result_code_billed("0001") is True
    assert is_result_code_billed("0002") is False
    assert is_result_code_billed(None) is False
    assert is_result_code_billed("4242") is False  # 未知码不臆断为计费


@pytest.mark.parametrize("code", ["4242", "something-unknown", ""])
def test_unknown_result_code_fails_closed(code: str) -> None:
    assert map_result_code(code) is VerificationStatus.UNABLE_TO_VERIFY
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response({"VerifyResult": code, "InvalidSign": "N"}),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.status is not VerificationStatus.VERIFIED_PASS
    if code:
        assert result.error_code == code  # 未知码原样保留，便于追查


def test_missing_verify_result_without_top_level_error_is_invalid_response() -> None:
    transport = RecordingTransport(
        results=[_token_response(), _verify_response({"InvalidSign": "N"})]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "InvalidResponse"


def test_top_level_error_code_without_verify_result_fails_closed() -> None:
    """文档「失败返回示例」带顶层 ``error_code``（未给出映射表 → 待确认，fail closed）。"""
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response(
                {},
                extra={
                    "error_code": 282162,
                    "error_msg": "Unexpected query result exception",
                },
            ),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "282162"
    assert result.reason == "provider_error"


def test_http_error_status_is_unable() -> None:
    transport = RecordingTransport(
        results=[
            _token_response(),
            _verify_response({}, status_code=500),
        ]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "HTTP500"
    assert result.reason == "provider_http_error"


def test_invalid_json_is_unable() -> None:
    transport = RecordingTransport(
        results=[_token_response(), HttpResponse(200, b"<html>not json</html>")]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "InvalidResponse"


# --- 缺失 invoice_type（本地 fail closed，不发请求） ---------------------------


def test_missing_invoice_type_fails_closed_without_any_network_call() -> None:
    transport = RecordingTransport(results=[])
    result = _provider(transport).verify(make_fields(invoice_kind=None))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.reason == "missing_invoice_type"
    assert transport.calls == []


# --- token 获取、缓存与过期 -----------------------------------------------------


def test_token_is_fetched_once_and_reused_across_verifies() -> None:
    transport = RecordingTransport(
        results=[_token_response(), _verify_response(_matching_words())]
    )
    provider = _provider(transport)
    fields = make_fields(invoice_kind="normal_invoice")
    provider.verify(fields)
    provider.verify(fields)
    assert len(transport.calls) == 3  # token + 两次查验
    assert str(transport.calls[0]["url"]).startswith(BAIDU_TOKEN_ENDPOINT)
    assert str(transport.calls[1]["url"]).startswith(BAIDU_ENDPOINT)
    assert str(transport.calls[2]["url"]).startswith(BAIDU_ENDPOINT)
    assert f"access_token={ACCESS_TOKEN}" in str(transport.calls[2]["url"])


def test_token_is_refetched_after_expiry() -> None:
    current = [FIXED_NOW]
    transport = RecordingTransport(
        results=[
            _token_response(expires_in=100),  # 100 秒有效期（提前 60 秒视为过期）
            _verify_response(_matching_words()),
            _token_response(token="24.refreshed-token.2592000"),
            _verify_response(_matching_words()),
        ]
    )
    provider = BaiduVatInvoiceVerifyProvider(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        transport=transport,
        clock=lambda: current[0],
    )
    fields = make_fields(invoice_kind="normal_invoice")
    provider.verify(fields)
    current[0] = FIXED_NOW + timedelta(seconds=100)  # 越过有效期
    provider.verify(fields)
    assert len(transport.calls) == 4
    assert str(transport.calls[2]["url"]).startswith(BAIDU_TOKEN_ENDPOINT)
    assert "24.refreshed-token.2592000" in str(transport.calls[3]["url"])


def test_token_without_expiry_is_not_cached() -> None:
    """``expires_in`` 缺失 → 有效期未知 → 不缓存（fail safe，绝不冒用可能过期的 token）。"""
    transport = RecordingTransport(
        results=[
            _token_response(expires_in=None),
            _verify_response(_matching_words()),
            _token_response(),
            _verify_response(_matching_words()),
        ]
    )
    provider = _provider(transport)
    fields = make_fields(invoice_kind="normal_invoice")
    provider.verify(fields)
    provider.verify(fields)
    assert len(transport.calls) == 4
    assert str(transport.calls[2]["url"]).startswith(BAIDU_TOKEN_ENDPOINT)


# --- token 失败 / 超时（不重发、不泄凭据） -------------------------------------


def test_token_fetch_error_is_unable_and_not_retried() -> None:
    transport = RecordingTransport(results=[_token_error_response()])
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "invalid_client"
    assert result.reason == "provider_token_error"
    assert len(transport.calls) == 1  # 只尝试一次
    # 错误结果不得携带任何凭据值。
    assert SECRET_KEY not in str(result.error_code)
    assert SECRET_KEY not in str(result.reason)


def test_token_invalid_json_is_unable() -> None:
    transport = RecordingTransport(results=[HttpResponse(200, b"not-json")])
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "TokenInvalidResponse"
    assert result.reason == "provider_token_invalid_response"
    assert len(transport.calls) == 1


def test_token_timeout_is_unable_and_not_retried() -> None:
    transport = RecordingTransport(results=[TransportTimeout("boom")])
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "TokenTimeout"
    assert result.reason == "provider_token_timeout"
    assert len(transport.calls) == 1  # 超时绝不盲目重发


def test_verify_timeout_is_unable_and_not_retried() -> None:
    transport = RecordingTransport(
        results=[_token_response(), TransportTimeout("boom")]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "Timeout"
    assert result.reason == "provider_timeout"
    # token 1 次 + 查验只尝试 1 次（不重发）。
    assert len(transport.calls) == 2


def test_verify_transport_error_is_unable_and_not_retried() -> None:
    transport = RecordingTransport(
        results=[_token_response(), TransportError("conn reset")]
    )
    result = _provider(transport).verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.UNABLE_TO_VERIFY
    assert result.error_code == "TransportError"
    assert result.reason == "provider_transport_error"
    assert len(transport.calls) == 2


# --- 凭据 fail closed ---------------------------------------------------------


def test_missing_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    with pytest.raises(MissingCredentialError) as exc:
        BaiduVatInvoiceVerifyProvider()
    message = str(exc.value)
    assert API_KEY_ENV in message
    assert SECRET_KEY_ENV in message


def test_missing_secret_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    with pytest.raises(MissingCredentialError) as exc:
        BaiduVatInvoiceVerifyProvider()
    message = str(exc.value)
    assert SECRET_KEY_ENV in message
    # 错误信息只提环境变量名，不含凭据值。
    assert API_KEY not in message


def test_blank_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "   ")
    monkeypatch.setenv(SECRET_KEY_ENV, "   ")
    with pytest.raises(MissingCredentialError):
        BaiduVatInvoiceVerifyProvider()


def test_provider_reads_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setenv(SECRET_KEY_ENV, SECRET_KEY)
    transport = RecordingTransport(
        results=[_token_response(), _verify_response(_matching_words())]
    )
    provider = BaiduVatInvoiceVerifyProvider(
        transport=transport, clock=lambda: FIXED_NOW
    )
    result = provider.verify(make_fields(invoice_kind="normal_invoice"))
    assert result.status is VerificationStatus.VERIFIED_PASS
    token_url = str(transport.calls[0]["url"])
    assert f"client_id={API_KEY}" in token_url
    # token 请求按文档示例将 client_secret 作为 URL 参数（HTTPS）。
    assert f"client_secret={SECRET_KEY}" in token_url
    verify_url = str(transport.calls[1]["url"])
    assert f"access_token={ACCESS_TOKEN}" in verify_url
    # 查验请求绝不携带 API Key / Secret Key。
    assert SECRET_KEY not in verify_url
    assert API_KEY not in verify_url
