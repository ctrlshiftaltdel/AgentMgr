"""百度 OCR **专用端点**适配器与路由测试（**全桩、零网络、零真实凭据**）。

覆盖 Issue #52 的验收项：

* ``vat_invoice``：字段按 W2-37 §4（``AmountInFiguers`` 价税合计口径、
  ``TotalAmount`` / ``TotalTax``、``InvoiceDate`` 归一化 ISO、``InvoiceNum``、购销方
  名称/税号、``Passenger*`` 多行字段）；响应**无字段级置信度** → 逐字段统一回退
  ``0.5``；``InvoiceCode`` 空值/空容器**不产出键**（绝不产出 ``"[]"`` 脏值）；
* ``online_taxi_itinerary``：逐笔行程读**小写** ``items``；``StartTime`` /
  ``EndTime`` 只取日期（时间部分是占位符）；``distance`` 做数字 + 区间校验，失败
  **保留原值**并标 ``distance_needs_review``；**明文手机号等敏感字段不入草稿 /
  日志 / 追溯**（丢弃 + 脱敏两层）；
* 路由：``decide_ocr_endpoint`` 的显式提示 / 文件名特征 / 无法判定；``RoutedOcrParser``
  委派到专用端点、无法判定回退 ``smart``，且返回**真正产出该值的解析器标识**；
* 请求形态：专用端点**不传** ``probability``（W2-37 §7 D4①），端点 URL 正确，PDF/OFD
  页码与图片带页码的拒绝口径与既有适配器一致；
* 失败收敛：空结果 / 传输异常 / 形态异常 → ``error='provider_error'``，不泄漏原文；
* 零网络：``socket`` 被替换为抛错桩。

所有响应都按 W2-37 记录的**真实结构**构造（标量字段 + ``[{"row", "word"}]`` 多行
数组 + 小写 ``items``），全部是本地桩，**绝不调用真实百度接口**。
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from app.integrations.baidu_dedicated_ocr import (
    BAIDU_TAXI_ITINERARY_ENDPOINT,
    BAIDU_VAT_INVOICE_ENDPOINT,
    DISTANCE_MAX,
    DISTANCE_MIN,
    ITINERARY_PARSER_NAME,
    ITINERARY_TYPE,
    OCR_FILENAME_METADATA_KEYS,
    OCR_ROUTE_METADATA_KEY,
    ROUTED_PARSER_NAME,
    ROUTE_SMART,
    ROUTE_TAXI_ITINERARY,
    ROUTE_VAT_INVOICE,
    VAT_INVOICE_PARSER_NAME,
    VAT_INVOICE_TYPE,
    BaiduTaxiItineraryParser,
    BaiduVatInvoiceParser,
    RoutedOcrParser,
    decide_ocr_endpoint,
    is_sensitive_field_name,
    mask_sensitive_text,
    normalize_distance,
)
from app.integrations.baidu_invoice_ocr import (
    API_KEY_ENV,
    BAIDU_OCR_ENDPOINT,
    BAIDU_OCR_PARSER_NAME,
    DEFAULT_FIELD_CONFIDENCE,
    SECRET_KEY_ENV,
    HttpResponse,
    MissingCredentialError,
    SourceDocument,
    TransportError,
    clean_field_text,
    normalize_amount,
    normalize_invoice_date,
    precheck_source_document,
    resolve_credential_env,
)
from app.intake.parsing import (
    InputKind,
    ParseRequest,
    build_draft_payload,
    decide_draft,
    parse_provenance,
    required_fields_for,
    safe_log_summary,
)

API_KEY = "example-bd-ocr-api-key-0052"
SECRET_KEY = "example-bd-ocr-secret-key-0052"
ENV = {API_KEY_ENV: API_KEY, SECRET_KEY_ENV: SECRET_KEY}
TOKEN = "24.example-token.2592000.1763000000.282335-00000052"
LOG_ID = "1394226734160674852"

#: 桩里的**明文手机号**（W2-37 §7 D5）：用占位号码，不是任何真实用户的号码。
PHONE = "13800000000"
MASKED_PHONE = "138****0000"

LOGGER_NAME = "app.integrations.baidu_dedicated_ocr"


# --- 桩 -----------------------------------------------------------------------


@dataclass
class StubTransport:
    """按 URL 区分 token 与识别请求的桩传输（**绝不触网**）。"""

    ocr_results: list[object] = field(default_factory=list)
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
        if "oauth" in url:
            payload = {"access_token": TOKEN, "expires_in": 2_592_000}
            return HttpResponse(200, json.dumps(payload).encode("utf-8"))
        if not self.ocr_results:
            raise AssertionError("stub transport has no queued OCR response")
        item = (
            self.ocr_results[0]
            if len(self.ocr_results) == 1
            else self.ocr_results.pop(0)
        )
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResponse)
        return item

    @property
    def ocr_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "oauth" not in call["url"]]

    @property
    def ocr_urls(self) -> list[str]:
        return [
            f"{urlsplit(call['url']).scheme}://{urlsplit(call['url']).netloc}"
            f"{urlsplit(call['url']).path}"
            for call in self.ocr_calls
        ]

    def form(self, index: int = -1) -> dict[str, list[str]]:
        return parse_qs(self.ocr_calls[index]["body"].decode("utf-8"))


class StubReader:
    """只读原件桩（记录被请求的 ``source_file_id``）。"""

    def __init__(self, document: SourceDocument | None = None) -> None:
        self.document = document if document is not None else _pdf()
        self.calls: list[int] = []

    def read(self, source_file_id: int) -> SourceDocument:
        self.calls.append(source_file_id)
        return self.document


@pytest.fixture()
def dedicated_log_records() -> Iterator[list[Any]]:
    """收集 ``app.integrations.baidu_dedicated_ocr`` 的日志记录。

    测试会话的 alembic 迁移会 ``fileConfig(disable_existing_loggers=True)``，把导入期
    创建的 logger 置为 ``disabled``，因此断言「日志可观测」时显式恢复该 logger。
    """
    import logging

    logger = logging.getLogger(LOGGER_NAME)
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector()
    previous_disabled, previous_level = logger.disabled, logger.level
    logger.addHandler(handler)
    logger.disabled = False
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled
        logger.removeHandler(handler)


# --- 报文与文件构造 -----------------------------------------------------------


def _pdf() -> SourceDocument:
    return SourceDocument(
        content=b"%PDF-1.4 stub invoice body",
        mime_type="application/pdf",
        size_bytes=27,
        scan_status="clean",
    )


def _image() -> SourceDocument:
    return SourceDocument(
        content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
        mime_type="image/png",
        size_bytes=48,
        scan_status="clean",
    )


def _response(words_result: object, *, log_id: str | None = LOG_ID) -> HttpResponse:
    body: dict[str, object] = {"words_result": words_result}
    if log_id is not None:
        body["log_id"] = log_id
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


def _request(
    kind: InputKind = InputKind.DOCUMENT,
    *,
    metadata: Mapping[str, Any] | None = None,
    source_file_id: int | None = 7,
) -> ParseRequest:
    return ParseRequest(
        input_kind=kind,
        business_type="expense_claim",
        source_file_id=source_file_id,
        metadata=metadata or {},
    )


def _vat_parser(
    results: list[object],
    *,
    document: SourceDocument | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[BaiduVatInvoiceParser, StubTransport, StubReader]:
    transport = StubTransport(ocr_results=list(results))
    reader = StubReader(document)
    parser = BaiduVatInvoiceParser(
        source_reader=reader, transport=transport, env=ENV if env is None else env
    )
    return parser, transport, reader


def _itinerary_parser(
    results: list[object],
    *,
    document: SourceDocument | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[BaiduTaxiItineraryParser, StubTransport, StubReader]:
    transport = StubTransport(ocr_results=list(results))
    reader = StubReader(document)
    parser = BaiduTaxiItineraryParser(
        source_reader=reader, transport=transport, env=ENV if env is None else env
    )
    return parser, transport, reader


def _routed_parser(
    results: list[object],
    *,
    document: SourceDocument | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[RoutedOcrParser, StubTransport, StubReader]:
    transport = StubTransport(ocr_results=list(results))
    reader = StubReader(document)
    parser = RoutedOcrParser(
        source_reader=reader, transport=transport, env=ENV if env is None else env
    )
    return parser, transport, reader


def _row(word: str, row: str = "1") -> dict[str, str]:
    return {"row": row, "word": word}


#: ``vat_invoice`` 真实响应形态（W2-37 §4/§4.2）：标量字段 + 多行 ``Passenger*``。
VAT_WORDS_RESULT: dict[str, object] = {
    "InvoiceCode": "",
    "InvoiceNum": "2600000012345678",
    "InvoiceDate": "2026年10月15日",
    "AmountInFiguers": "106.00",
    "TotalAmount": "100.00",
    "TotalTax": "6.00",
    "SellerName": "上海示例商贸有限公司",
    "SellerRegisterNum": "91310115MA01XYZ123",
    "PurchaserName": "北京示例科技有限公司",
    "PurchaserRegisterNum": "91110108MA01ABCDEF",
    "InvoiceType": "电子发票(普通发票)",
    "ServiceType": "服务",
    "PassengerName": [_row("示例乘客甲"), _row("示例乘客乙", "2")],
    "PassengerIdNum": [_row("1101**********1234"), _row("1101**********5678", "2")],
    "PassengerDate": [_row("2026年10月14日"), _row("2026年10月15日", "2")],
    "PassengerDeparture": [_row("示例市示例区甲路上客点")],
    "PassengerArrival": [_row("示例市示例区乙路")],
    "PassengerVehicleType": [_row("出租车")],
    "PassengerClass": [_row("经济型")],
    "CommodityName": [_row("运输服务")],
}

#: ``online_taxi_itinerary`` 真实响应形态（W2-37 §5/§7）：顶层标量 + 小写 ``items``。
ITINERARY_WORDS_RESULT: dict[str, object] = {
    "ServiceProvider": "示例出行科技有限公司",
    "ItemNum": "2",
    "PickupDate": "2026-10-15",
    "TotalFare": "68.00",
    "ApplicationDate": "2026-10-16",
    "PhoneNumber": PHONE,
    "ServiceType": "交通",
    "items": [
        {
            "PickupTime": "08:15",
            "StartTime": "2026-10-15 --:--",
            "EndTime": "2026-10-15 --:--",
            "Distance": "3.5",
            "Fare": "12.00",
            "item_provider": "",
        },
        {
            "PickupTime": "18:40",
            "StartTime": "2026-10-15 --:--",
            "EndTime": "2026-10-15 --:--",
            "Distance": "4.2k",
            "Fare": "56.00",
            "item_provider": "",
        },
    ],
}


def _assert_provider_error(result: Any) -> None:
    assert not result.ok
    assert result.error == "provider_error"
    assert dict(result.fields) == {}
    assert dict(result.field_confidence) == {}


# =============================================================================
# 一、``vat_invoice`` 适配器
# =============================================================================


def test_vat_invoice_maps_real_shape_fields() -> None:
    """按 W2-37 §4 的字段口径归一化（价税合计、不含税、税额、购销方、多行旅客）。"""
    parser, transport, reader = _vat_parser([_response(VAT_WORDS_RESULT)])
    result = parser.parse(_request())

    assert result.ok, result.error
    assert reader.calls == [7]
    assert result.parser_name == VAT_INVOICE_PARSER_NAME
    assert result.parser_version == "1"

    fields = dict(result.fields)
    assert fields == {
        "invoice_type": VAT_INVOICE_TYPE,
        "invoice_no": "2600000012345678",
        "invoice_date": "2026-10-15",  # ``2026年10月15日`` → ISO
        "amount": "106.00",  # 价税合计（AmountInFiguers）
        "amount_excluding_tax": "100.00",  # 不含税合计（TotalAmount，mi 无此键）
        "tax_amount": "6.00",
        "buyer_name": "北京示例科技有限公司",
        "buyer_tax_id": "91110108MA01ABCDEF",
        "seller_name": "上海示例商贸有限公司",
        "seller_tax_id": "91310115MA01XYZ123",
        "passenger_name": ["示例乘客甲", "示例乘客乙"],
        "passenger_id_number": ["1101**********1234", "1101**********5678"],
        "passenger_date": ["2026-10-14", "2026-10-15"],
        "passenger_departure": ["示例市示例区甲路上客点"],
        "passenger_arrival": ["示例市示例区乙路"],
        "passenger_vehicle_type": ["出租车"],
        "passenger_class": ["经济型"],
    }
    # 键集合与置信度严格一致（``ParseResult`` 的硬约束）。
    assert set(result.fields) == set(result.field_confidence)
    # 未登记的响应键（``InvoiceType`` / ``ServiceType`` / ``CommodityName``）不透传。
    assert "CommodityName" not in fields
    assert "电子发票(普通发票)" not in fields.values()


def test_vat_invoice_amount_never_falls_back_to_tax_exclusive_total() -> None:
    """只有不含税合计时**不产出** ``amount``（价税合计口径不可回退，W2-35 §3）。"""
    parser, _, _ = _vat_parser(
        [_response({"InvoiceNum": "1", "TotalAmount": "100.00", "TotalTax": "6.00"})]
    )
    result = parser.parse(_request())

    assert result.ok
    assert "amount" not in result.fields
    assert result.fields["amount_excluding_tax"] == "100.00"
    assert result.fields["tax_amount"] == "6.00"


@pytest.mark.parametrize(
    "invoice_code",
    ["", "   ", [], {}, [{"row": "1", "word": "  "}], None],
    ids=["empty-string", "blank", "empty-array", "empty-object", "blank-row", "null"],
)
def test_vat_invoice_blank_invoice_code_yields_no_key(invoice_code: object) -> None:
    """``InvoiceCode`` 空值/空容器按**无值**处理：不产出键，更不产出 ``"[]"`` 脏值。"""
    result_map = dict(VAT_WORDS_RESULT)
    result_map["InvoiceCode"] = invoice_code

    parser, _, _ = _vat_parser([_response(result_map)])
    result = parser.parse(_request())

    assert result.ok
    assert "invoice_code" not in result.fields
    assert "invoice_code" not in result.field_confidence
    assert "[]" not in {str(value) for value in result.fields.values()}
    assert set(result.fields) == set(result.field_confidence)


def test_vat_invoice_has_no_field_level_confidence() -> None:
    """专用端点无字段级置信度 → 逐字段统一回退 ``0.5`` → 草稿必然人工复核。"""
    parser, _, _ = _vat_parser([_response(VAT_WORDS_RESULT)])
    result = parser.parse(_request())

    assert set(result.field_confidence.values()) == {DEFAULT_FIELD_CONFIDENCE}
    decision = decide_draft(
        result, required_fields=required_fields_for("expense_claim")
    )
    assert decision.job_status == "needs_review"
    assert decision.review_status == "pending"  # 人工确认仍是唯一入账路径
    assert set(decision.low_confidence_fields) == set(result.fields)


def test_vat_invoice_empty_or_malformed_result_is_provider_error() -> None:
    for words_result in ({}, [], "not-a-mapping", None):
        parser, _, _ = _vat_parser([_response(words_result)])
        _assert_provider_error(parser.parse(_request()))


def test_vat_invoice_request_shape_and_no_probability_parameter() -> None:
    """专用端点请求：``pdf_file`` + 页码，且**不传** ``probability``（W2-37 §7 D4①）。"""
    parser, transport, _ = _vat_parser([_response(VAT_WORDS_RESULT)])
    assert parser.parse(_request(metadata={"page": 2})).ok

    assert transport.ocr_urls == [BAIDU_VAT_INVOICE_ENDPOINT]
    form = transport.form()
    assert form["pdf_file_num"] == ["2"]
    assert "probability" not in form
    assert transport.ocr_calls[-1]["headers"]["Content-Type"] == (
        "application/x-www-form-urlencoded"
    )


def test_vat_invoice_ofd_and_image_inputs() -> None:
    ofd = SourceDocument(
        content=b"OFD stub body", mime_type="application/ofd", size_bytes=13,
        scan_status="clean",
    )
    parser, transport, _ = _vat_parser([_response(VAT_WORDS_RESULT)], document=ofd)
    assert parser.parse(_request(metadata={"page": 1})).ok
    assert transport.form()["ofd_file_num"] == ["1"]

    parser, transport, _ = _vat_parser(
        [_response(VAT_WORDS_RESULT)], document=_image()
    )
    assert parser.parse(_request(InputKind.IMAGE)).ok
    assert "image" in transport.form()
    assert "probability" not in transport.form()


def test_vat_invoice_image_with_page_is_rejected_before_request() -> None:
    parser, transport, _ = _vat_parser(
        [_response(VAT_WORDS_RESULT)], document=_image()
    )
    _assert_provider_error(parser.parse(_request(InputKind.IMAGE, metadata={"page": 2})))
    assert transport.calls == []


def test_vat_invoice_precheck_rejects_before_request() -> None:
    for document in (
        SourceDocument(b"x", "application/pdf", 1, "infected"),
        SourceDocument(b"x", "application/msword", 1, "clean"),
        SourceDocument(b"", "application/pdf", 0, "clean"),
    ):
        parser, transport, _ = _vat_parser(
            [_response(VAT_WORDS_RESULT)], document=document
        )
        _assert_provider_error(parser.parse(_request()))
        assert transport.calls == []


def test_vat_invoice_transport_failure_does_not_leak_exception_text() -> None:
    parser, _, _ = _vat_parser([TransportError(f"boom {SECRET_KEY}")])
    result = parser.parse(_request())
    _assert_provider_error(result)
    assert "boom" not in str(result.error)
    assert SECRET_KEY not in json.dumps(dict(result.fields), ensure_ascii=False)


def test_vat_invoice_requires_credentials_and_never_leaks_them() -> None:
    with pytest.raises(MissingCredentialError) as caught:
        BaiduVatInvoiceParser(source_reader=StubReader(), env={})
    message = str(caught.value)
    assert API_KEY_ENV in message
    assert API_KEY not in message
    assert SECRET_KEY not in message


# =============================================================================
# 二、``online_taxi_itinerary`` 适配器
# =============================================================================


def test_itinerary_maps_real_shape_fields_and_trips() -> None:
    """顶层字段与逐笔行程（**小写** ``items``）按 W2-37 §5 归一化。"""
    parser, transport, _ = _itinerary_parser([_response(ITINERARY_WORDS_RESULT)])
    result = parser.parse(_request())

    assert result.ok, result.error
    assert result.parser_name == ITINERARY_PARSER_NAME
    assert transport.ocr_urls == [BAIDU_TAXI_ITINERARY_ENDPOINT]
    assert "probability" not in transport.form()

    fields = dict(result.fields)
    assert fields["invoice_type"] == ITINERARY_TYPE
    assert fields["invoice_date"] == "2026-10-16"  # ApplicationDate
    assert fields["amount"] == "68.00"  # TotalFare
    assert fields["seller_name"] == "示例出行科技有限公司"  # ServiceProvider
    assert fields["trips"] == [
        {
            "pickup_time": "08:15",
            "start_date": "2026-10-15",
            "end_date": "2026-10-15",
            "distance": "3.50",
            "distance_needs_review": False,
            "fare": "12.00",
        },
        {
            "pickup_time": "18:40",
            "start_date": "2026-10-15",
            "end_date": "2026-10-15",
            "distance": "4.2k",  # 数字→字母误识：原样保留
            "distance_needs_review": True,
            "fare": "56.00",
        },
    ]
    assert set(result.fields) == set(result.field_confidence)
    assert set(result.field_confidence.values()) == {DEFAULT_FIELD_CONFIDENCE}


def test_itinerary_reads_lowercase_items_key_only() -> None:
    """文档参数表写 ``Items``，真实响应是**小写** ``items``（W2-37 §7 D4②）。"""
    words_result = dict(ITINERARY_WORDS_RESULT)
    words_result["Items"] = words_result.pop("items")

    parser, _, _ = _itinerary_parser([_response(words_result)])
    result = parser.parse(_request())

    assert result.ok
    assert "trips" not in result.fields  # 只认真实响应的小写键，不臆造回退
    assert result.fields["amount"] == "68.00"


def test_itinerary_empty_or_malformed_items_yield_no_trips_key() -> None:
    for items in ([], {}, "x", None, ["not-a-mapping"], [{}]):
        words_result = dict(ITINERARY_WORDS_RESULT)
        words_result["items"] = items
        parser, _, _ = _itinerary_parser([_response(words_result)])
        result = parser.parse(_request())
        assert result.ok
        assert "trips" not in result.fields


def test_itinerary_start_and_end_time_only_keep_the_date() -> None:
    """``StartTime``/``EndTime`` 的时间部分是占位符 → 只取日期（W2-37 §7 D4④）。"""
    words_result = dict(ITINERARY_WORDS_RESULT)
    words_result["items"] = [
        {"StartTime": "2026年10月15日 08:15", "EndTime": "2026-10-15 --:--"},
        {"StartTime": "不详", "EndTime": "2026-10-16 09:00"},
    ]
    parser, _, _ = _itinerary_parser([_response(words_result)])
    result = parser.parse(_request())

    trips = result.fields["trips"]
    assert trips[0]["start_date"] == "2026-10-15"
    assert trips[0]["end_date"] == "2026-10-15"
    assert "--:--" not in json.dumps(trips, ensure_ascii=False)
    assert trips[1]["start_date"] == "不详"  # 解析不了就保留原值交给人工
    assert trips[1]["end_date"] == "2026-10-16"


@pytest.mark.parametrize(
    ("raw", "expected", "needs_review"),
    [
        ("3.5", "3.50", False),
        ("12", "12.00", False),
        ("0.1", "0.10", False),
        ("500", "500.00", False),
        ("4.2k", "4.2k", True),  # 数字→字母误识（D1）
        ("", "", True),
        ("不详", "不详", True),
        ("0.001", "0.001", True),  # 低于合理下限
        ("12345", "12345", True),  # 高于合理上限
        ("-3", "-3", True),
        ("nan", "nan", True),
    ],
)
def test_normalize_distance_validates_number_and_range(
    raw: str, expected: str, needs_review: bool
) -> None:
    assert normalize_distance(raw) == (expected, needs_review)


def test_normalize_distance_bounds_are_the_documented_ones() -> None:
    """区间是**本地保守默认**（本任务不引入配置项），固化为常量以免被无意放宽。"""
    assert DISTANCE_MIN == Decimal("0.1")
    assert DISTANCE_MAX == Decimal("500")
    assert DISTANCE_MIN < DISTANCE_MAX


def test_itinerary_distance_lost_decimal_point_is_a_known_blind_spot() -> None:
    """区间内的「小数点丢失」误识（``3.5`` → ``35``）**无法**由数值校验发现。

    这是 W2-37 §7 D1「偏差方向」的已知局限：错误值落在合理区间内且不带任何可判别
    标志。跨源核对（与发票金额/里程比对）属产品决策，不在本任务内——本测试固化该
    局限，避免被误当成「已修复」。
    """
    value, needs_review = normalize_distance("35")
    assert (value, needs_review) == ("35.00", False)


def test_itinerary_no_field_level_confidence() -> None:
    parser, _, _ = _itinerary_parser([_response(ITINERARY_WORDS_RESULT)])
    result = parser.parse(_request())
    assert set(result.field_confidence.values()) == {DEFAULT_FIELD_CONFIDENCE}


def test_itinerary_empty_result_is_provider_error() -> None:
    for words_result in ({}, [], None):
        parser, _, _ = _itinerary_parser([_response(words_result)])
        _assert_provider_error(parser.parse(_request()))


def test_itinerary_precheck_and_transport_failure() -> None:
    parser, transport, _ = _itinerary_parser(
        [_response(ITINERARY_WORDS_RESULT)],
        document=SourceDocument(b"x", "application/pdf", 1, "pending"),
    )
    _assert_provider_error(parser.parse(_request()))
    assert transport.calls == []

    parser, _, _ = _itinerary_parser([TransportError("boom")])
    _assert_provider_error(parser.parse(_request()))


# --- 敏感字段（W2-37 §7 D5） --------------------------------------------------


@pytest.mark.parametrize(
    ("name", "sensitive"),
    [
        ("PhoneNumber", True),
        ("phone", True),
        ("Mobile", True),
        ("Telephone", True),
        ("手机号", True),
        ("联系方式", True),
        ("Email", True),
        ("邮箱", True),
        ("BankCard", True),
        ("PassengerIdNum", False),  # 供应商已脱敏，且是 #52 明确要求保留的字段
        ("TotalFare", False),
        ("ServiceProvider", False),
        ("Distance", False),
    ],
)
def test_is_sensitive_field_name(name: str, sensitive: bool) -> None:
    assert is_sensitive_field_name(name) is sensitive


def test_mask_sensitive_text_masks_phone_and_keeps_other_digits() -> None:
    assert mask_sensitive_text(PHONE) == MASKED_PHONE
    assert mask_sensitive_text(f"示例出行 {PHONE} 客服") == f"示例出行 {MASKED_PHONE} 客服"
    # 更长的数字串（如票号）里的 11 位片段不被误伤。
    assert mask_sensitive_text("24117000000012345678") == "24117000000012345678"
    assert mask_sensitive_text("3.50") == "3.50"


def test_itinerary_plaintext_phone_never_reaches_draft_or_provenance(
    dedicated_log_records: list[Any],
) -> None:
    """明文手机号**不入**草稿载荷 / 置信度 / 追溯 / 日志摘要 / 日志。"""
    parser, _, _ = _itinerary_parser([_response(ITINERARY_WORDS_RESULT)])
    result = parser.parse(_request())
    assert result.ok

    payload = build_draft_payload(
        intake_job_id=1, business_type="expense_claim", result=result
    )
    rendered = json.dumps(
        {
            "fields": dict(result.fields),
            "confidence": dict(result.field_confidence),
            "draft": payload,
            "provenance": parse_provenance(result),
            "log_summary": safe_log_summary(result),
        },
        ensure_ascii=False,
        default=str,
    )
    assert PHONE not in rendered
    assert "PhoneNumber" not in rendered  # 未登记的响应键一律不透传
    assert PHONE not in "\n".join(record.getMessage() for record in dedicated_log_records)
    # 丢弃是**可观测**的（只记原因，不记键名与取值）。
    assert any(
        "sensitive field present" in record.getMessage()
        for record in dedicated_log_records
    )


def test_itinerary_masks_phone_embedded_in_a_mapped_field() -> None:
    """号码嵌在服务商名称里时按「脱敏」处置（保留字段、去掉明文）。"""
    words_result = dict(ITINERARY_WORDS_RESULT)
    words_result["ServiceProvider"] = f"示例出行 {PHONE} 客服"

    parser, _, _ = _itinerary_parser([_response(words_result)])
    result = parser.parse(_request())

    assert result.fields["seller_name"] == f"示例出行 {MASKED_PHONE} 客服"
    assert PHONE not in json.dumps(dict(result.fields), ensure_ascii=False)


# =============================================================================
# 三、路由
# =============================================================================


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        (ROUTE_VAT_INVOICE, ROUTE_VAT_INVOICE),
        (ROUTE_TAXI_ITINERARY, ROUTE_TAXI_ITINERARY),
        (ROUTE_SMART, None),  # 显式「走 smart」→ 不用专用端点
        ("smart", None),
        ("unknown-route", None),  # 非法提示不静默生效，回退判定
        ("", None),
    ],
)
def test_decide_ocr_endpoint_explicit_hint(hint: str, expected: str | None) -> None:
    request = _request(metadata={OCR_ROUTE_METADATA_KEY: hint})
    assert decide_ocr_endpoint(request) == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"filename": "网约车行程单-2026-10.pdf"}, ROUTE_TAXI_ITINERARY),
        ({"filename": "taxi-itinerary.pdf"}, ROUTE_TAXI_ITINERARY),
        ({"filename": "电子发票-202610.pdf"}, ROUTE_VAT_INVOICE),
        ({"file_name": "invoice-ofd.ofd"}, ROUTE_VAT_INVOICE),
        ({"original_name": "数电发票.pdf"}, ROUTE_VAT_INVOICE),
        ({"declared_name": "行程单.pdf"}, ROUTE_TAXI_ITINERARY),
        ({"filename": "receipt-2026.png"}, None),  # 无特征 → 无法判定
        ({"filename": "   "}, None),
        ({}, None),
        ({"filename": 42}, None),  # 非字符串提示不臆断
    ],
)
def test_decide_ocr_endpoint_filename_hints(
    metadata: dict[str, Any], expected: str | None
) -> None:
    assert decide_ocr_endpoint(_request(metadata=metadata)) == expected


def test_decide_ocr_endpoint_prefers_explicit_hint_over_filename() -> None:
    metadata = {
        OCR_ROUTE_METADATA_KEY: ROUTE_TAXI_ITINERARY,
        "filename": "电子发票.pdf",
    }
    assert decide_ocr_endpoint(_request(metadata=metadata)) == ROUTE_TAXI_ITINERARY


def test_decide_ocr_endpoint_metadata_keys_are_the_documented_ones() -> None:
    assert OCR_FILENAME_METADATA_KEYS == (
        "filename",
        "file_name",
        "original_name",
        "declared_name",
    )


def test_routed_parser_delegates_to_vat_endpoint() -> None:
    parser, transport, _ = _routed_parser([_response(VAT_WORDS_RESULT)])
    result = parser.parse(
        _request(metadata={OCR_ROUTE_METADATA_KEY: ROUTE_VAT_INVOICE})
    )

    assert result.ok
    assert transport.ocr_urls == [BAIDU_VAT_INVOICE_ENDPOINT]
    assert result.parser_name == VAT_INVOICE_PARSER_NAME  # 真正产出该值的端点
    assert parse_provenance(result)["parser_name"] == VAT_INVOICE_PARSER_NAME
    assert result.fields["amount"] == "106.00"


def test_routed_parser_delegates_to_itinerary_endpoint() -> None:
    parser, transport, _ = _routed_parser([_response(ITINERARY_WORDS_RESULT)])
    result = parser.parse(_request(metadata={"filename": "网约车行程单.pdf"}))

    assert result.ok
    assert transport.ocr_urls == [BAIDU_TAXI_ITINERARY_ENDPOINT]
    assert result.parser_name == ITINERARY_PARSER_NAME
    assert result.fields["amount"] == "68.00"


def test_routed_parser_falls_back_to_smart_when_undecidable() -> None:
    parser, transport, _ = _routed_parser(
        [_response([{"type": "vat_invoice", "probability": 0.99,
                     "result": {"InvoiceNum": "1", "AmountInFiguers": "10.00"}}])]
    )
    result = parser.parse(_request())

    assert result.ok
    assert transport.ocr_urls == [BAIDU_OCR_ENDPOINT]
    assert result.parser_name == BAIDU_OCR_PARSER_NAME
    assert result.fields["amount"] == "10.00"


def test_routed_parser_exposes_its_own_identity_and_inputs() -> None:
    parser, _, _ = _routed_parser([_response(VAT_WORDS_RESULT)])
    assert parser.parser_name == ROUTED_PARSER_NAME
    assert parser.supported_input_kinds == frozenset(
        {InputKind.IMAGE, InputKind.DOCUMENT}
    )
    assert parser.credentials_configured


def test_routed_parser_requires_credentials_without_leaking_them() -> None:
    with pytest.raises(MissingCredentialError) as caught:
        RoutedOcrParser(source_reader=StubReader(), env={})
    message = str(caught.value)
    assert API_KEY_ENV in message
    assert API_KEY not in message
    assert SECRET_KEY not in message


def test_routed_parser_failure_is_collapsed_to_provider_error() -> None:
    parser, _, _ = _routed_parser([TransportError(f"boom {SECRET_KEY}")])
    result = parser.parse(_request(metadata={OCR_ROUTE_METADATA_KEY: ROUTE_VAT_INVOICE}))
    _assert_provider_error(result)
    assert "boom" not in str(result.error)


def test_routed_parser_never_opens_a_socket() -> None:
    parser, _, _ = _routed_parser([_response(VAT_WORDS_RESULT)])
    with patch("socket.socket", side_effect=AssertionError("network used")):
        with patch(
            "socket.create_connection", side_effect=AssertionError("network used")
        ):
            result = parser.parse(_request(metadata={"filename": "电子发票.pdf"}))
    assert result.ok
    assert socket.socket is not None  # patch 已还原


# =============================================================================
# 四、共享口径（Issue #52 为专用端点公开的既有实现）
# =============================================================================


def test_shared_primitives_delegate_to_existing_behaviour() -> None:
    # 容器按无值（W2-36 §7 D1 的修复口径），金额两位小数、日期 ISO 与既有适配器一致。
    assert clean_field_text([]) is None
    assert clean_field_text(" x ") == "x"
    assert normalize_amount("￥1,234.5") == "1234.50"
    assert normalize_invoice_date("2019年08月28日") == "2019-08-28"
    assert precheck_source_document(_pdf(), None) is None
    assert precheck_source_document(_pdf(), 2) is None
    assert resolve_credential_env(
        {"AJAIMGR_BAIDU_API_KEY": "k", "AJAIMGR_BAIDU_SECRET_KEY": "s"}
    ) == {API_KEY_ENV: "k", SECRET_KEY_ENV: "s"}
