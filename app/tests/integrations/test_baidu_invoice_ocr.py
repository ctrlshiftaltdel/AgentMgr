"""百度智能财务票据识别（``multiple_invoice``）适配器测试（**全桩、零网络**）。

覆盖 Issue #35 的验收项：

* 凭据缺失 → 构造即 ``MissingCredentialError``，信息只含变量名；``BDOCR_*`` 优先，
  兼容旧名 ``AJAIMGR_BAIDU_*``（两者都在时用新名）；
* token 缓存（同一实例多次识别只取一次）、过期重取、401/110 刷新一次；
* 请求体形态（``image`` / ``pdf_file`` / ``ofd_file`` + base64 + ``probability=true``
  + 页码参数）与 ``Content-Type``；
* 成功映射（中文日期、金额取价税合计而非不含税、购方/销方、税额）；
* ``taxi_online_ticket`` 用 snake_case 字段（``total_fare``/``application_date``/
  ``service_provider``），``taxi_receipt`` 不受影响；
* 置信度：真实响应 ``{"average": ..., "min": ...}`` 优先取 ``min``、回退 ``average``
  （0–100 归一化、负数/非有限值丢弃），缺失回退 0.5；
* 空容器字段（``[]`` / ``{}`` / 仅含空白元素的容器）按**无值**处理——不产出键、不
  产出 ``"[]"`` 之类脏值；非空单元素数组照常取值带置信度（Issue #49 / W2-36 §7 D1）；
* 4 类只分类不识别、空结果、多票取金额最大者；
* 预检：非 clean / 不支持 MIME / 超 4M / 像素越界 / 图片带页码 → 发请求前拒绝；
* 传输异常 → ``provider_error``，不泄漏异常原文与凭据；
* 零网络：``socket.socket`` 与 ``socket.create_connection`` 被替换为抛错桩。

全部测试使用注入的 ``RecordingTransport``，**绝不调用真实百度接口**。
"""

from __future__ import annotations

import base64
import json
import struct
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app.integrations.baidu_invoice_ocr import (
    API_KEY_ENV,
    BAIDU_OCR_ENDPOINT,
    BAIDU_OCR_PARSER_NAME,
    BAIDU_TOKEN_ENDPOINT,
    LEGACY_API_KEY_ENV,
    LEGACY_SECRET_KEY_ENV,
    SECRET_KEY_ENV,
    BaiduInvoiceOcrParser,
    HttpResponse,
    MissingCredentialError,
    SourceDocument,
    TransportError,
)
from app.intake.parsing import (
    InputKind,
    ParseRequest,
    build_draft_payload,
    decide_draft,
    required_fields_for,
)

API_KEY = "example-api-key-do-not-log-0001"
SECRET_KEY = "example-secret-key-do-not-log-9999"
TOKEN_ONE = "24.example-token-one.2592000.1763000000.282335-00000001"
TOKEN_TWO = "24.example-token-two.2592000.1763000000.282335-00000002"
TOKEN_EXPIRES_IN = 2_592_000  # 30 天
LOG_ID = "1394226734160674816"
ENV = {API_KEY_ENV: API_KEY, SECRET_KEY_ENV: SECRET_KEY}


@dataclass
class RecordingTransport:
    """按队列返回预设响应/异常的桩传输（测试专用，**绝不触网**）。

    队列只剩一项时重复使用，便于「固定响应」场景；``calls`` 记录每次调用。
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


class StubReader:
    """只读原件桩：返回固定 ``SourceDocument`` 并记录 ``source_file_id``。"""

    def __init__(self, document: SourceDocument) -> None:
        self.document = document
        self.calls: list[int] = []

    def read(self, source_file_id: int) -> SourceDocument:
        self.calls.append(source_file_id)
        return self.document


class ExplodingReader:
    """读取即失败的桩（验证异常收敛，不泄漏原文）。"""

    def read(self, source_file_id: int) -> SourceDocument:
        raise RuntimeError("boom-secret-reader-error")


class FakeClock:
    """可推进的 UTC 时钟（验证 token 过期重取）。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


# --- 报文与文件构造 -----------------------------------------------------------


def _token_response(
    token: str = TOKEN_ONE, *, expires_in: int = TOKEN_EXPIRES_IN
) -> HttpResponse:
    body = {"access_token": token, "expires_in": expires_in}
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


def _ocr_response(
    entries: list[object], *, log_id: str | None = LOG_ID, status_code: int = 200
) -> HttpResponse:
    body: dict[str, object] = {
        "words_result_num": len(entries),
        "words_result": entries,
    }
    if log_id is not None:
        body["log_id"] = log_id
    return HttpResponse(status_code, json.dumps(body).encode("utf-8"))


def _error_response(code: object | None, *, status_code: int = 200) -> HttpResponse:
    body: dict[str, object] = {}
    if code is not None:
        body["error_code"] = code
        body["error_msg"] = "provider says no"
    return HttpResponse(status_code, json.dumps(body).encode("utf-8"))


def _entry(
    invoice_type: object,
    result: object = None,
    *,
    probability: object = 0.99,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "type": invoice_type,
        "probability": probability,
        "left": 0,
        "top": 0,
        "width": 10,
        "height": 10,
    }
    if result is not None:
        entry["result"] = result
    return entry


def _png(width: int, height: int) -> bytes:
    """构造仅含签名 + IHDR 的 PNG（足够像素预检，不是可解码图片）。"""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return signature + ihdr


def _document(
    content: bytes,
    *,
    mime_type: str = "image/png",
    scan_status: str = "clean",
    size_bytes: int | None = None,
) -> SourceDocument:
    return SourceDocument(
        content=content,
        mime_type=mime_type,
        size_bytes=len(content) if size_bytes is None else size_bytes,
        scan_status=scan_status,
    )


def _request(
    kind: InputKind = InputKind.IMAGE, *, metadata: Mapping[str, Any] | None = None
) -> ParseRequest:
    return ParseRequest(
        input_kind=kind,
        business_type="expense_claim",
        source_file_id=7,
        metadata=metadata or {},
    )


def _parser(
    results: list[object],
    *,
    document: SourceDocument | None = None,
    reader: Any = None,
    clock: Any = None,
    env: Mapping[str, str] | None = None,
) -> tuple[BaiduInvoiceOcrParser, RecordingTransport, Any]:
    transport = RecordingTransport(results=list(results))
    source_reader = reader if reader is not None else StubReader(
        document if document is not None else _document(_png(100, 100))
    )
    parser = BaiduInvoiceOcrParser(
        source_reader=source_reader,
        transport=transport,
        clock=clock,
        env=ENV if env is None else env,
    )
    return parser, transport, source_reader


VAT_RESULT: dict[str, object] = {
    "InvoiceCode": "011002100311",
    "InvoiceNum": "12345678",
    "InvoiceDate": "2019年08月28日",
    "TotalAmount": "100.00",
    "AmountInFiguers": "106.00",
    "TotalTax": "6.00",
    "PurchaserName": "北京示例科技有限公司",
    "PurchaserRegisterNum": "91110108MA01ABCDEF",
    "SellerName": "上海示例商贸有限公司",
    "SellerRegisterNum": "91310115MA01XYZ123",
}


class AssertionsMixin(unittest.TestCase):
    def assert_provider_error(self, result: Any) -> None:
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "provider_error")
        self.assertEqual(dict(result.fields), {})
        self.assertEqual(dict(result.field_confidence), {})


class TestCredentials(AssertionsMixin):
    def _token_query(self, transport: RecordingTransport) -> dict[str, list[str]]:
        """取第 1 次（token）请求的查询参数，用于核对实际使用的凭据。"""
        token_calls = [
            call
            for call in transport.calls
            if call["url"].startswith(BAIDU_TOKEN_ENDPOINT)
        ]
        self.assertTrue(token_calls)
        return parse_qs(urlsplit(token_calls[0]["url"]).query)

    def test_missing_both_credentials_fails_closed(self) -> None:
        with self.assertRaises(MissingCredentialError) as caught:
            BaiduInvoiceOcrParser(
                source_reader=StubReader(_document(_png(100, 100))), env={}
            )
        message = str(caught.exception)
        # 基类按声明顺序校验，缺失的第一个变量名即可定位；只提变量名、不含凭据值。
        self.assertIn(API_KEY_ENV, message)
        self.assertNotIn(API_KEY, message)
        self.assertNotIn(SECRET_KEY, message)

    def test_missing_secret_credential_fails_closed(self) -> None:
        with self.assertRaises(MissingCredentialError) as caught:
            BaiduInvoiceOcrParser(
                source_reader=StubReader(_document(_png(100, 100))),
                env={API_KEY_ENV: API_KEY},
            )
        self.assertIn(SECRET_KEY_ENV, str(caught.exception))
        self.assertNotIn(API_KEY, str(caught.exception))

    def test_primary_credential_names_are_used(self) -> None:
        parser, transport, _ = _parser(
            [
                _token_response(),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ],
            env={API_KEY_ENV: API_KEY, SECRET_KEY_ENV: SECRET_KEY},
        )
        self.assertTrue(parser.parse(_request()).ok)
        query = self._token_query(transport)
        self.assertEqual(query["client_id"], [API_KEY])
        self.assertEqual(query["client_secret"], [SECRET_KEY])

    def test_legacy_credential_names_are_supported(self) -> None:
        # 旧部署只有 AJAIMGR_BAIDU_* 时仍可构造并取 token。
        parser, transport, _ = _parser(
            [
                _token_response(),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ],
            env={LEGACY_API_KEY_ENV: API_KEY, LEGACY_SECRET_KEY_ENV: SECRET_KEY},
        )
        self.assertTrue(parser.credentials_configured)
        self.assertTrue(parser.parse(_request()).ok)
        query = self._token_query(transport)
        self.assertEqual(query["client_id"], [API_KEY])
        self.assertEqual(query["client_secret"], [SECRET_KEY])

    def test_primary_credential_names_win_over_legacy(self) -> None:
        primary_key = "example-primary-key-0002"
        primary_secret = "example-primary-secret-0002"
        parser, transport, _ = _parser(
            [
                _token_response(),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ],
            env={
                API_KEY_ENV: primary_key,
                SECRET_KEY_ENV: primary_secret,
                LEGACY_API_KEY_ENV: API_KEY,
                LEGACY_SECRET_KEY_ENV: SECRET_KEY,
            },
        )
        self.assertTrue(parser.parse(_request()).ok)
        query = self._token_query(transport)
        self.assertEqual(query["client_id"], [primary_key])
        self.assertEqual(query["client_secret"], [primary_secret])

    def test_blank_primary_falls_back_to_legacy(self) -> None:
        # 新名存在但为空白 → 视为未配置，回退旧名（仍 fail closed 的补集）。
        parser, transport, _ = _parser(
            [
                _token_response(),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ],
            env={
                API_KEY_ENV: "   ",
                SECRET_KEY_ENV: "",
                LEGACY_API_KEY_ENV: API_KEY,
                LEGACY_SECRET_KEY_ENV: SECRET_KEY,
            },
        )
        self.assertTrue(parser.parse(_request()).ok)
        query = self._token_query(transport)
        self.assertEqual(query["client_id"], [API_KEY])

    def test_parser_identity_and_supported_inputs(self) -> None:
        parser, _, _ = _parser([_token_response()])
        self.assertEqual(parser.parser_name, BAIDU_OCR_PARSER_NAME)
        self.assertEqual(parser.parser_version, "1")
        self.assertTrue(parser.supports(InputKind.IMAGE))
        self.assertTrue(parser.supports(InputKind.DOCUMENT))
        self.assertFalse(parser.supports(InputKind.TEXT))
        self.assertTrue(parser.credentials_configured)

    def test_unsupported_input_kind_raises(self) -> None:
        parser, _, _ = _parser([_token_response()])
        with self.assertRaises(Exception):
            parser.parse(
                ParseRequest(input_kind=InputKind.TEXT, business_type="expense_claim")
            )


class TestTokenHandling(AssertionsMixin):
    def test_token_is_cached_across_recognitions(self) -> None:
        parser, transport, _ = _parser(
            [
                _token_response(),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ]
        )
        first = parser.parse(_request())
        second = parser.parse(_request())
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)

        token_calls = [
            call
            for call in transport.calls
            if call["url"].startswith(BAIDU_TOKEN_ENDPOINT)
        ]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(len(transport.calls), 3)

    def test_token_is_refreshed_after_expiry_margin(self) -> None:
        clock = FakeClock()
        parser, transport, _ = _parser(
            [
                _token_response(expires_in=120),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
                _token_response(TOKEN_TWO, expires_in=120),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ],
            clock=clock,
        )
        self.assertTrue(parser.parse(_request()).ok)
        clock.advance(61)  # 剩余寿命 59s < 60s 提前量
        self.assertTrue(parser.parse(_request()).ok)

        token_calls = [
            call
            for call in transport.calls
            if call["url"].startswith(BAIDU_TOKEN_ENDPOINT)
        ]
        self.assertEqual(len(token_calls), 2)
        self.assertIn(TOKEN_ONE, transport.calls[1]["url"])
        self.assertIn(TOKEN_TWO, transport.calls[3]["url"])

    def test_http_401_refreshes_token_once(self) -> None:
        parser, transport, _ = _parser(
            [
                _token_response(),
                HttpResponse(401, b"{}"),
                _token_response(TOKEN_TWO),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ]
        )
        result = parser.parse(_request())
        self.assertTrue(result.ok)
        token_calls = [
            call
            for call in transport.calls
            if call["url"].startswith(BAIDU_TOKEN_ENDPOINT)
        ]
        self.assertEqual(len(token_calls), 2)
        self.assertIn(TOKEN_TWO, transport.calls[-1]["url"])

    def test_error_code_110_refreshes_token_once(self) -> None:
        parser, transport, _ = _parser(
            [
                _token_response(),
                _error_response(110),
                _token_response(TOKEN_TWO),
                _ocr_response([_entry("vat_invoice", VAT_RESULT)]),
            ]
        )
        result = parser.parse(_request())
        self.assertTrue(result.ok)
        self.assertEqual(len(transport.calls), 4)
        self.assertIn(TOKEN_TWO, transport.calls[-1]["url"])

    def test_token_refresh_is_attempted_only_once(self) -> None:
        parser, transport, _ = _parser(
            [
                _token_response(),
                _error_response(110),
                _token_response(TOKEN_TWO),
                _error_response(110),
            ]
        )
        result = parser.parse(_request())
        self.assert_provider_error(result)
        self.assertEqual(len(transport.calls), 4)

    def test_token_http_error_fails_closed(self) -> None:
        parser, transport, _ = _parser(
            [_error_response("invalid_client", status_code=400)]
        )
        result = parser.parse(_request())
        self.assert_provider_error(result)
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("invalid_client", str(result.error))

    def test_transport_failure_does_not_leak_exception_text(self) -> None:
        parser, _, _ = _parser(
            [_token_response(), TransportError(f"boom {SECRET_KEY}")]
        )
        result = parser.parse(_request())
        self.assert_provider_error(result)
        self.assertNotIn("boom", str(result.error))
        self.assertNotIn(SECRET_KEY, str(result.error))
        self.assertNotIn(SECRET_KEY, repr(dict(result.fields)))


class TestRequestShape(AssertionsMixin):
    def _form(
        self, transport: RecordingTransport, index: int = -1
    ) -> dict[str, list[str]]:
        return parse_qs(transport.calls[index]["body"].decode("utf-8"))

    def test_image_request_is_base64_urlencoded(self) -> None:
        image = _png(120, 80)
        parser, transport, _ = _parser(
            [_token_response(), _ocr_response([_entry("vat_invoice", VAT_RESULT)])],
            document=_document(image, mime_type="image/png"),
        )
        self.assertTrue(parser.parse(_request()).ok)

        request_call = transport.calls[-1]
        self.assertEqual(
            request_call["headers"]["Content-Type"],
            "application/x-www-form-urlencoded",
        )
        form = self._form(transport)
        self.assertEqual(base64.b64decode(form["image"][0]), image)
        self.assertEqual(form["probability"], ["true"])
        self.assertNotIn("pdf_file", form)

        parsed_url = urlsplit(request_call["url"])
        self.assertEqual(parsed_url.scheme, "https")
        self.assertEqual(
            f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}",
            BAIDU_OCR_ENDPOINT,
        )
        self.assertIn("access_token", parse_qs(parsed_url.query))

    def test_pdf_request_supports_page_number(self) -> None:
        pdf = b"%PDF-1.4 fake invoice body"
        parser, transport, _ = _parser(
            [_token_response(), _ocr_response([_entry("vat_invoice", VAT_RESULT)])],
            document=_document(pdf, mime_type="application/pdf"),
        )
        result = parser.parse(_request(InputKind.DOCUMENT, metadata={"page": 2}))
        self.assertTrue(result.ok)

        form = self._form(transport)
        self.assertEqual(base64.b64decode(form["pdf_file"][0]), pdf)
        self.assertEqual(form["pdf_file_num"], ["2"])
        self.assertEqual(form["probability"], ["true"])

    def test_ofd_request_supports_page_number(self) -> None:
        ofd = b"OFD fake body"
        parser, transport, _ = _parser(
            [_token_response(), _ocr_response([_entry("vat_invoice", VAT_RESULT)])],
            document=_document(ofd, mime_type="application/ofd"),
        )
        result = parser.parse(_request(InputKind.DOCUMENT, metadata={"page": 1}))
        self.assertTrue(result.ok)

        form = self._form(transport)
        self.assertEqual(base64.b64decode(form["ofd_file"][0]), ofd)
        self.assertEqual(form["ofd_file_num"], ["1"])

    def test_image_with_page_is_rejected_before_request(self) -> None:
        parser, transport, _ = _parser([_token_response()])
        result = parser.parse(_request(metadata={"page": 2}))
        self.assert_provider_error(result)
        self.assertEqual(transport.calls, [])


class TestPrecheck(AssertionsMixin):
    def _assert_rejected(self, document: SourceDocument) -> None:
        parser, transport, _ = _parser(
            [_token_response(), _ocr_response([_entry("vat_invoice", VAT_RESULT)])],
            document=document,
        )
        result = parser.parse(_request(InputKind.DOCUMENT))
        self.assert_provider_error(result)
        self.assertEqual(transport.calls, [])

    def test_non_clean_scan_status_is_rejected(self) -> None:
        self._assert_rejected(_document(_png(100, 100), scan_status="infected"))

    def test_unsupported_mime_is_rejected(self) -> None:
        self._assert_rejected(_document(b"data", mime_type="application/msword"))

    def test_empty_content_is_rejected(self) -> None:
        self._assert_rejected(_document(b"", mime_type="image/png"))

    def test_encoded_size_over_limit_is_rejected(self) -> None:
        # base64 后约 4.4M，超过 4M 上限；在像素检查前拒绝。
        oversized = b"\x00" * 3_300_000
        self._assert_rejected(_document(oversized, mime_type="image/png"))

    def test_image_shorter_side_below_minimum_is_rejected(self) -> None:
        self._assert_rejected(_document(_png(10, 500), mime_type="image/png"))

    def test_image_longer_side_above_maximum_is_rejected(self) -> None:
        self._assert_rejected(_document(_png(4097, 100), mime_type="image/png"))

    def test_image_at_pixel_bounds_is_accepted(self) -> None:
        parser, transport, _ = _parser(
            [_token_response(), _ocr_response([_entry("vat_invoice", VAT_RESULT)])],
            document=_document(_png(15, 4096), mime_type="image/png"),
        )
        self.assertTrue(parser.parse(_request()).ok)
        self.assertEqual(len(transport.calls), 2)

    def test_unparsable_image_header_is_best_effort(self) -> None:
        parser, transport, _ = _parser(
            [_token_response(), _ocr_response([_entry("vat_invoice", VAT_RESULT)])],
            document=_document(b"not-a-real-png-header", mime_type="image/png"),
        )
        result = parser.parse(_request())
        self.assertTrue(result.ok, "无法解析像素时只跳过检查，不阻断")
        self.assertEqual(len(transport.calls), 2)

    def test_reader_failure_is_collapsed_to_provider_error(self) -> None:
        parser, transport, _ = _parser(
            [_token_response()], reader=ExplodingReader()
        )
        result = parser.parse(_request())
        self.assert_provider_error(result)
        self.assertEqual(transport.calls, [])
        self.assertNotIn("boom", str(result.error))


class TestFieldMapping(AssertionsMixin):
    def _parse_one_entry(self, entry: Mapping[str, Any], **kwargs: Any) -> Any:
        parser, _, _ = _parser(
            [_token_response(), _ocr_response([dict(entry)])], **kwargs
        )
        return parser.parse(_request())

    def test_vat_invoice_maps_all_fields_and_prefers_tax_inclusive_amount(self) -> None:
        result = self._parse_one_entry(_entry("vat_invoice", VAT_RESULT))
        self.assertTrue(result.ok)
        self.assertEqual(
            dict(result.fields),
            {
                "invoice_type": "vat_invoice",
                "invoice_code": "011002100311",
                "invoice_no": "12345678",
                "invoice_date": "2019-08-28",
                "amount": "106.00",
                "tax_amount": "6.00",
                "buyer_name": "北京示例科技有限公司",
                "buyer_tax_id": "91110108MA01ABCDEF",
                "seller_name": "上海示例商贸有限公司",
                "seller_tax_id": "91310115MA01XYZ123",
            },
        )
        # 分类置信度只用于 invoice_type；其余字段无逐字段置信度 → 0.5。
        self.assertAlmostEqual(result.field_confidence["invoice_type"], 0.99)
        for name in set(result.fields) - {"invoice_type"}:
            self.assertEqual(result.field_confidence[name], 0.5)

    def test_amount_never_falls_back_to_tax_exclusive_total(self) -> None:
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {"InvoiceNum": "1", "TotalAmount": "100.00", "TotalTax": "6.00"},
            )
        )
        self.assertTrue(result.ok)
        self.assertNotIn("amount", result.fields)
        self.assertEqual(result.fields["tax_amount"], "6.00")

    def test_field_probability_in_wrapped_object_is_normalized(self) -> None:
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {
                    "InvoiceCode": {"word": "011", "probability": 0.8},
                    "AmountInFiguers": {"word": "106.00", "probability": 92},
                    "TotalTax": {"word": "6.00"},
                    "InvoiceNum": "12345678",
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["invoice_code"], "011")
        self.assertAlmostEqual(result.field_confidence["invoice_code"], 0.8)
        self.assertAlmostEqual(result.field_confidence["amount"], 0.92)
        self.assertEqual(result.field_confidence["tax_amount"], 0.5)
        self.assertEqual(result.field_confidence["invoice_no"], 0.5)

    def test_array_result_with_key_and_word_is_supported(self) -> None:
        result = self._parse_one_entry(
            _entry(
                "taxi_receipt",
                [
                    {"key": "date", "word": "2019-08-28"},
                    {"key": "TotalFare", "word": "23.50"},
                    {"key": "InvoiceNum", "word": "00012345"},
                    {"word": "unkeyed value is ignored"},
                ],
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["invoice_type"], "taxi_receipt")
        self.assertEqual(result.fields["invoice_date"], "2019-08-28")
        self.assertEqual(result.fields["amount"], "23.50")
        self.assertEqual(result.fields["invoice_no"], "00012345")
        self.assertNotIn("unkeyed value is ignored", set(result.fields.values()))

    def test_taxi_online_ticket_uses_snake_case_fields(self) -> None:
        # 真实响应：taxi_online_ticket 的 result 是 snake_case，不能复用 taxi_receipt 的表。
        result = self._parse_one_entry(
            _entry(
                "taxi_online_ticket",
                {
                    "total_fare": "123.45",
                    "application_date": "2026-01-02",
                    "service_provider": "示例出行科技有限公司",
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["invoice_type"], "taxi_online_ticket")
        self.assertEqual(result.fields["amount"], "123.45")
        self.assertEqual(result.fields["invoice_date"], "2026-01-02")
        self.assertEqual(result.fields["seller_name"], "示例出行科技有限公司")

    def test_taxi_online_ticket_falls_back_to_pascal_case(self) -> None:
        result = self._parse_one_entry(
            _entry("taxi_online_ticket", {"TotalFare": "23.50", "date": "2026-01-02"})
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["amount"], "23.50")
        self.assertEqual(result.fields["invoice_date"], "2026-01-02")

    def test_taxi_receipt_keeps_its_own_alias_table(self) -> None:
        # 回归：taxi_receipt 仍用 PascalCase，且不会误取 snake_case 字段。
        result = self._parse_one_entry(
            _entry(
                "taxi_receipt",
                {"TotalFare": "23.50", "date": "2026-01-02", "total_fare": "999.99"},
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["amount"], "23.50")

    def test_classification_only_types_return_only_invoice_type(self) -> None:
        for invoice_type in (
            "limit_invoice",
            "shopping_receipt",
            "pos_invoice",
            "others",
        ):
            with self.subTest(invoice_type=invoice_type):
                result = self._parse_one_entry(
                    _entry(invoice_type, [], probability=90)
                )
                self.assertTrue(result.ok)
                self.assertEqual(dict(result.fields), {"invoice_type": invoice_type})
                self.assertAlmostEqual(result.field_confidence["invoice_type"], 0.9)

    def test_empty_words_result_is_provider_error(self) -> None:
        parser, _, _ = _parser([_token_response(), _ocr_response([])])
        result = parser.parse(_request())
        self.assert_provider_error(result)

    def test_missing_invoice_type_is_provider_error(self) -> None:
        entry = {"probability": 0.99, "result": VAT_RESULT}
        result = self._parse_one_entry(entry)
        self.assert_provider_error(result)

    def test_non_mapping_entry_is_provider_error(self) -> None:
        parser, _, _ = _parser([_token_response(), _ocr_response(["not-an-object"])])
        result = parser.parse(_request())
        self.assert_provider_error(result)

    def test_multiple_invoices_use_largest_amount(self) -> None:
        entries = [
            _entry("vat_invoice", {"InvoiceNum": "A", "AmountInFiguers": "100.00"}),
            _entry("vat_invoice", {"InvoiceNum": "B", "AmountInFiguers": "300.00"}),
            _entry("vat_invoice", {"InvoiceNum": "C", "AmountInFiguers": "200.00"}),
        ]
        parser, _, _ = _parser([_token_response(), _ocr_response(entries)])
        result = parser.parse(_request())
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["invoice_no"], "B")
        self.assertEqual(result.fields["amount"], "300.00")
        # 不伪造下游不认识的多票计数字段。
        self.assertNotIn("invoice_count", result.fields)

    def test_multiple_invoices_tie_uses_first(self) -> None:
        entries = [
            _entry("vat_invoice", {"InvoiceNum": "FIRST", "AmountInFiguers": "50.00"}),
            _entry("vat_invoice", {"InvoiceNum": "SECOND", "AmountInFiguers": "50.00"}),
        ]
        parser, _, _ = _parser([_token_response(), _ocr_response(entries)])
        result = parser.parse(_request())
        self.assertEqual(result.fields["invoice_no"], "FIRST")

    def test_unparsable_amount_and_date_are_kept_for_review(self) -> None:
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {
                    "InvoiceDate": "不详",
                    "AmountInFiguers": "壹佰零陆元",
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["invoice_date"], "不详")
        self.assertEqual(result.fields["amount"], "壹佰零陆元")

    def test_currency_symbols_and_commas_are_normalized(self) -> None:
        result = self._parse_one_entry(
            _entry("vat_invoice", {"AmountInFiguers": "￥1,234.5"})
        )
        self.assertEqual(result.fields["amount"], "1234.50")

    # --- 空容器字段（Issue #49 / W2-36 §7 D1）---------------------------------

    def test_empty_array_field_is_treated_as_missing(self) -> None:
        # 数电票形态：百度对 ``InvoiceCode`` 返回空数组 → 视为无值、不产出键
        # （修复前 ``str([]) == "[]"`` 会被写成脏值、置信度回退 0.5）。
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {
                    "InvoiceCode": [],
                    "InvoiceNum": "12345678",
                    "AmountInFiguers": "106.00",
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertNotIn("invoice_code", result.fields)
        self.assertNotIn("invoice_code", result.field_confidence)
        self.assertNotIn("[]", set(result.fields.values()))
        # 键集合仍严格一致（逐字段带置信度的契约）。
        self.assertEqual(set(result.fields), set(result.field_confidence))
        self.assertEqual(result.fields["invoice_no"], "12345678")
        self.assertEqual(result.fields["amount"], "106.00")

    def test_non_empty_single_element_array_still_yields_value_and_confidence(
        self,
    ) -> None:
        # 回归：真实响应的单元素数组形态（``[{"word": ..., "probability": {...}}]``）
        # 取值与置信度照常，空容器规则不得误伤。
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {
                    "InvoiceCode": [
                        {
                            "word": "011002100311",
                            "probability": {"average": 0.92, "min": 0.81},
                        }
                    ],
                    "AmountInFiguers": [
                        {"word": "106.00", "probability": {"min": 0.77}}
                    ],
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["invoice_code"], "011002100311")
        self.assertAlmostEqual(result.field_confidence["invoice_code"], 0.81)
        self.assertEqual(result.fields["amount"], "106.00")
        self.assertAlmostEqual(result.field_confidence["amount"], 0.77)

    def test_empty_mapping_field_is_treated_as_missing(self) -> None:
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {
                    "InvoiceCode": {},
                    "TotalTax": [{}],
                    "AmountInFiguers": "106.00",
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertNotIn("invoice_code", result.fields)
        self.assertNotIn("tax_amount", result.fields)
        self.assertEqual(set(result.fields), set(result.field_confidence))
        self.assertEqual(result.fields["amount"], "106.00")

    def test_whitespace_only_container_field_is_treated_as_missing(self) -> None:
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {
                    "InvoiceCode": [{"word": "   "}],
                    "SellerName": ["  "],
                    "PurchaserName": [{}, None],
                    "AmountInFiguers": "106.00",
                },
            )
        )
        self.assertTrue(result.ok)
        for name in ("invoice_code", "seller_name", "buyer_name"):
            with self.subTest(name=name):
                self.assertNotIn(name, result.fields)
                self.assertNotIn(name, result.field_confidence)
        self.assertEqual(set(result.fields), set(result.field_confidence))
        self.assertEqual(result.fields["amount"], "106.00")

    def test_empty_container_does_not_mask_alias_fallback(self) -> None:
        # 空容器与 ``None``/空白同处理：跳过该候选继续找同义字段，而不是吞掉整组别名。
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {"InvoiceCode": [], "invoice_code": "011002100311"},
            )
        )
        self.assertEqual(result.fields["invoice_code"], "011002100311")

    def test_empty_container_field_keeps_draft_decision_unchanged(self) -> None:
        # 行为边界（验收第 5 条）：本批默认必填为 ``("amount", "expense_date")``，
        # 不含 ``invoice_code``——缺失集合不因本修复变化；脏值/回退置信度带来的
        # 低置信度噪音消失，草稿载荷键集合仍一致。
        result = self._parse_one_entry(
            _entry(
                "vat_invoice",
                {
                    "InvoiceCode": [],
                    "AmountInFiguers": [
                        {"word": "106.00", "probability": {"min": 0.9}}
                    ],
                },
            )
        )
        decision = decide_draft(
            result, required_fields=required_fields_for("expense_claim")
        )
        self.assertEqual(decision.missing_fields, ("expense_date",))
        # 修复前 ``invoice_code == "[]"`` 且置信度回退 0.5 → 这里会多一条低置信度噪音。
        self.assertNotIn("invoice_code", decision.low_confidence_fields)
        self.assertEqual(decision.low_confidence_fields, ())
        self.assertTrue(decision.requires_human_review)
        payload = build_draft_payload(
            intake_job_id=1, business_type="expense_claim", result=result
        )
        self.assertEqual(payload["extracted_json"], dict(result.fields))
        self.assertNotIn("invoice_code", payload["extracted_json"])
        self.assertEqual(
            set(payload["extracted_json"]), set(payload["confidence_json"])
        )

    def test_response_http_error_is_provider_error(self) -> None:
        parser, _, _ = _parser(
            [_token_response(), _error_response(None, status_code=500)]
        )
        result = parser.parse(_request())
        self.assert_provider_error(result)

    def test_unknown_business_error_code_is_provider_error(self) -> None:
        parser, _, _ = _parser(
            [_token_response(), _error_response("999999", status_code=200)]
        )
        result = parser.parse(_request())
        self.assert_provider_error(result)

    def test_invalid_json_is_provider_error(self) -> None:
        parser, _, _ = _parser([_token_response(), HttpResponse(200, b"not-json")])
        result = parser.parse(_request())
        self.assert_provider_error(result)


class TestFieldConfidence(AssertionsMixin):
    """字段置信度：真实响应为 ``{"average": ..., "min": ...}``（已核实）。"""

    def _amount_confidence(self, probability: object) -> float:
        parser, _, _ = _parser(
            [
                _token_response(),
                _ocr_response(
                    [
                        _entry(
                            "vat_invoice",
                            {
                                "AmountInFiguers": {
                                    "word": "123.45",
                                    "probability": probability,
                                }
                            },
                        )
                    ]
                ),
            ]
        )
        result = parser.parse(_request())
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["amount"], "123.45")
        return result.field_confidence["amount"]

    def test_mapping_confidence_prefers_min(self) -> None:
        self.assertAlmostEqual(
            self._amount_confidence({"average": 0.975, "min": 0.42}), 0.42
        )

    def test_mapping_confidence_falls_back_to_average(self) -> None:
        self.assertAlmostEqual(self._amount_confidence({"average": 0.86}), 0.86)

    def test_mapping_confidence_ignores_null_min(self) -> None:
        self.assertAlmostEqual(
            self._amount_confidence({"average": 0.86, "min": None}), 0.86
        )

    def test_mapping_confidence_supports_percentage_scale(self) -> None:
        self.assertAlmostEqual(
            self._amount_confidence({"average": 92.2, "min": 80}), 0.8
        )

    def test_mapping_confidence_drops_negative_min(self) -> None:
        self.assertAlmostEqual(
            self._amount_confidence({"average": 0.7, "min": -0.3}), 0.7
        )

    def test_non_finite_and_negative_scalars_fall_back(self) -> None:
        self.assertEqual(self._amount_confidence(float("nan")), 0.5)
        self.assertEqual(self._amount_confidence(float("inf")), 0.5)
        self.assertEqual(self._amount_confidence(-0.4), 0.5)
        self.assertEqual(
            self._amount_confidence({"average": float("nan"), "min": None}), 0.5
        )

    def test_real_response_single_element_array_is_unwrapped(self) -> None:
        parser, _, _ = _parser(
            [
                _token_response(),
                _ocr_response(
                    [
                        _entry(
                            "taxi_online_ticket",
                            {
                                "total_fare": [
                                    {
                                        "word": "123.45",
                                        "probability": {
                                            "average": 0.92,
                                            "min": 0.81,
                                        },
                                    }
                                ]
                            },
                        )
                    ]
                ),
            ]
        )
        result = parser.parse(_request())
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["amount"], "123.45")
        self.assertAlmostEqual(result.field_confidence["amount"], 0.81)


class TestNoNetwork(AssertionsMixin):
    def test_parse_never_opens_a_socket(self) -> None:
        parser, _, _ = _parser(
            [_token_response(), _ocr_response([_entry("vat_invoice", VAT_RESULT)])]
        )
        with patch("socket.socket", side_effect=AssertionError("network used")):
            with patch(
                "socket.create_connection",
                side_effect=AssertionError("network used"),
            ):
                result = parser.parse(_request())
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["amount"], "106.00")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
