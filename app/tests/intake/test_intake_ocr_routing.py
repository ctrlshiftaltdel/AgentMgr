"""默认装配点的**路由模式**测试（Issue #52，纯逻辑、零网络、零真实凭据）。

覆盖验收标准的装配层部分：

* ``smart``（默认）行为与既有实现**逐字一致**：注册 ``BaiduInvoiceOcrParser``；
* ``routed`` 注册复合路由解析器：按文件特征委派到专用端点、无法判定回退 ``smart``；
* 路由来源：显式参数 → 配置文件 ``[ocr].routing``（``AJAIMGR_CONFIG_FILE`` /
  显式路径）→ 默认 ``smart``；**非法取值显式报错**（fail closed，不静默回退）；
* 凭据缺失时两种模式都**优雅降级**（不注册、不抛给调用方、不发请求）；
* 生效模式在返回值上可观测（``registry.routing``）；
* 注入 ``env`` 即完整环境：其中没有 ``AJAIMGR_CONFIG_FILE`` 时**不读**工作目录下的
  默认配置文件（测试/联调可复现）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.integrations.baidu_dedicated_ocr import (
    BAIDU_TAXI_ITINERARY_ENDPOINT,
    BAIDU_VAT_INVOICE_ENDPOINT,
    ITINERARY_PARSER_NAME,
    OCR_ROUTE_METADATA_KEY,
    ROUTED_PARSER_NAME,
    ROUTE_VAT_INVOICE,
    VAT_INVOICE_PARSER_NAME,
    RoutedOcrParser,
)
from app.integrations.baidu_invoice_ocr import (
    API_KEY_ENV,
    BAIDU_OCR_ENDPOINT,
    BAIDU_OCR_PARSER_NAME,
    SECRET_KEY_ENV,
    BaiduInvoiceOcrParser,
    HttpResponse,
    SourceDocument,
)
from app.integrations.ocr_routing import (
    CODE_CONFIG_INVALID,
    CONFIG_PATH_ENV,
    OcrRoutingConfigError,
    OcrRoutingMode,
)
from app.intake import assembly
from app.intake.parsing import InputKind, ParseRequest

API_KEY = "example-bd-ocr-api-key-0052"
SECRET_KEY = "example-bd-ocr-secret-key-0052"
ENV = {API_KEY_ENV: API_KEY, SECRET_KEY_ENV: SECRET_KEY}
TOKEN = "24.example-token.2592000.1763000000.282335-00000052"


@dataclass
class StubTransport:
    """按 URL 区分 token 与识别请求的桩传输（**绝不触网**）。"""

    ocr_results: list[object] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        self.calls.append({"url": url, "body": body})
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
        assert isinstance(item, HttpResponse)
        return item

    @property
    def ocr_urls(self) -> list[str]:
        return [call["url"].split("?", 1)[0] for call in self.calls if "oauth" not in call["url"]]


class StubReader:
    """只读原件桩。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def read(self, source_file_id: int) -> SourceDocument:
        self.calls.append(source_file_id)
        return SourceDocument(
            content=b"%PDF-1.4 stub body",
            mime_type="application/pdf",
            size_bytes=19,
            scan_status="clean",
        )


def _response(words_result: object) -> HttpResponse:
    return HttpResponse(200, json.dumps({"words_result": words_result}).encode("utf-8"))


#: ``vat_invoice`` 专用端点响应（单票对象 + 标量字段，W2-37 §4）。
VAT_RESULT: dict[str, object] = {
    "InvoiceNum": "2600000012345678",
    "InvoiceDate": "2026年10月15日",
    "AmountInFiguers": "106.00",
    "TotalAmount": "100.00",
    "TotalTax": "6.00",
}

#: ``online_taxi_itinerary`` 专用端点响应（小写 ``items``，W2-37 §5）。
ITINERARY_RESULT: dict[str, object] = {
    "ServiceProvider": "示例出行科技有限公司",
    "TotalFare": "68.00",
    "ApplicationDate": "2026-10-16",
    "items": [{"PickupTime": "08:15", "Distance": "3.5", "Fare": "12.00"}],
}

#: ``multiple_invoice`` 响应（数组 + 每项 ``type``/``result``，W2-35 §2）。
SMART_RESULT: list[object] = [
    {
        "type": "vat_invoice",
        "probability": 0.99,
        "result": {
            "InvoiceNum": [{"word": "12345678", "probability": {"min": 0.9}}],
            "AmountInFiguers": [{"word": "10.00", "probability": {"min": 0.9}}],
        },
    }
]


def _build(
    *,
    env: Mapping[str, str] | None = None,
    results: list[object] | None = None,
    **kwargs: Any,
) -> tuple[assembly.DefaultParserRegistry, StubReader, StubTransport]:
    reader = StubReader()
    transport = StubTransport(ocr_results=results or [_response(VAT_RESULT)])
    registry = assembly.build_default_parser_registry(
        source_reader=reader,
        transport=transport,
        env=ENV if env is None else env,
        **kwargs,
    )
    return registry, reader, transport


def _request(metadata: Mapping[str, Any] | None = None) -> ParseRequest:
    return ParseRequest(
        input_kind=InputKind.DOCUMENT,
        business_type="expense_claim",
        source_file_id=7,
        metadata=metadata or {},
    )


def _config(tmp_path: Path, routing: str) -> Path:
    path = tmp_path / "ajaimgr.toml"
    path.write_text(f"[ocr]\nrouting = \"{routing}\"\n", encoding="utf-8")
    return path


# --- 默认：smart（与现状一致） ------------------------------------------------


def test_default_registers_the_existing_smart_parser() -> None:
    registry, _, _ = _build()

    parser = registry.resolve(InputKind.IMAGE)
    assert isinstance(parser, BaiduInvoiceOcrParser)
    assert parser.parser_name == BAIDU_OCR_PARSER_NAME
    assert registry.resolve(InputKind.DOCUMENT) is parser
    assert registry.routing is OcrRoutingMode.SMART
    assert registry.degraded is False


def test_injected_env_without_config_path_is_hermetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入 ``env`` 且无 ``AJAIMGR_CONFIG_FILE`` → 不读工作目录下的默认配置文件。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ajaimgr.toml").write_text(
        "[ocr]\nrouting = \"routed\"\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)

    registry, _, _ = _build(env=ENV)  # 注入环境里没有配置文件路径
    assert registry.routing is OcrRoutingMode.SMART
    assert isinstance(registry.resolve(InputKind.IMAGE), BaiduInvoiceOcrParser)

    # 不注入 env（生产口径）时按文档读取工作目录下的默认配置文件（凭据是否齐备与
    # 本断言无关：只核对生效模式）。
    production = assembly.build_default_parser_registry(
        source_reader=StubReader(), transport=StubTransport()
    )
    assert production.routing is OcrRoutingMode.ROUTED


# --- 配置文件驱动 -------------------------------------------------------------


def test_config_file_switches_to_routed(tmp_path: Path) -> None:
    registry, _, _ = _build(env={**ENV, CONFIG_PATH_ENV: str(_config(tmp_path, "routed"))})

    parser = registry.resolve(InputKind.IMAGE)
    assert isinstance(parser, RoutedOcrParser)
    assert parser.parser_name == ROUTED_PARSER_NAME
    assert registry.routing is OcrRoutingMode.ROUTED
    assert registry.resolve(InputKind.TEXT) is None


def test_explicit_config_path_switches_to_routed(tmp_path: Path) -> None:
    registry, _, _ = _build(routing_config_path=_config(tmp_path, "routed"))
    assert registry.routing is OcrRoutingMode.ROUTED


def test_config_file_can_keep_smart(tmp_path: Path) -> None:
    registry, _, _ = _build(
        env={**ENV, CONFIG_PATH_ENV: str(_config(tmp_path, "smart"))}
    )
    assert registry.routing is OcrRoutingMode.SMART
    assert isinstance(registry.resolve(InputKind.IMAGE), BaiduInvoiceOcrParser)


def test_missing_config_file_defaults_to_smart(tmp_path: Path) -> None:
    registry, _, _ = _build(
        env={**ENV, CONFIG_PATH_ENV: str(tmp_path / "absent.toml")}
    )
    assert registry.routing is OcrRoutingMode.SMART
    assert isinstance(registry.resolve(InputKind.IMAGE), BaiduInvoiceOcrParser)


def test_config_without_ocr_section_defaults_to_smart(tmp_path: Path) -> None:
    path = tmp_path / "ajaimgr.toml"
    path.write_text("[template]\nlayout = \"entry_rows\"\n", encoding="utf-8")
    registry, _, _ = _build(env={**ENV, CONFIG_PATH_ENV: str(path)})
    assert registry.routing is OcrRoutingMode.SMART


def test_invalid_config_value_fails_closed_without_fallback(tmp_path: Path) -> None:
    """非法取值**显式报错**，绝不静默回退默认（否则「以为切了路由其实没切」）。"""
    path = _config(tmp_path, "legacy")
    with pytest.raises(OcrRoutingConfigError) as caught:
        _build(env={**ENV, CONFIG_PATH_ENV: str(path)})

    assert caught.value.code == CODE_CONFIG_INVALID
    assert "ocr.routing" in caught.value.message
    assert "legacy" not in caught.value.message  # 不回显配置取值
    assert API_KEY not in caught.value.message


# --- 显式参数 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (OcrRoutingMode.ROUTED, OcrRoutingMode.ROUTED),
        ("routed", OcrRoutingMode.ROUTED),
        (OcrRoutingMode.SMART, OcrRoutingMode.SMART),
        ("smart", OcrRoutingMode.SMART),
    ],
)
def test_explicit_routing_argument_wins(
    tmp_path: Path, value: Any, expected: OcrRoutingMode
) -> None:
    # 配置文件说 smart，显式参数说 routed（或反之）→ 以显式参数为准。
    registry, _, _ = _build(
        env={**ENV, CONFIG_PATH_ENV: str(_config(tmp_path, "smart"))},
        routing=value,
    )
    assert registry.routing is expected


def test_invalid_explicit_routing_argument_fails_closed() -> None:
    with pytest.raises(OcrRoutingConfigError) as caught:
        _build(routing="legacy")
    assert caught.value.code == CODE_CONFIG_INVALID
    assert "argument 'routing'" in caught.value.message
    assert "legacy" not in caught.value.message


# --- routed 模式的解析路径（装配层端到端，全桩） -------------------------------


def test_routed_registry_dispatches_to_vat_endpoint(tmp_path: Path) -> None:
    registry, reader, transport = _build(
        env={**ENV, CONFIG_PATH_ENV: str(_config(tmp_path, "routed"))},
        results=[_response(VAT_RESULT)],
    )
    parser = registry.resolve(InputKind.DOCUMENT)
    assert parser is not None

    result = parser.parse(_request({OCR_ROUTE_METADATA_KEY: ROUTE_VAT_INVOICE}))

    assert result.ok, result.error
    assert reader.calls == [7]
    assert transport.ocr_urls == [BAIDU_VAT_INVOICE_ENDPOINT]
    assert result.parser_name == VAT_INVOICE_PARSER_NAME  # 真正产出该值的端点
    assert result.fields["amount"] == "106.00"


def test_routed_registry_dispatches_to_itinerary_endpoint(tmp_path: Path) -> None:
    registry, _, transport = _build(
        env={**ENV, CONFIG_PATH_ENV: str(_config(tmp_path, "routed"))},
        results=[_response(ITINERARY_RESULT)],
    )
    parser = registry.resolve(InputKind.DOCUMENT)
    assert parser is not None

    result = parser.parse(_request({"filename": "网约车行程单-2026-10.pdf"}))

    assert result.ok, result.error
    assert transport.ocr_urls == [BAIDU_TAXI_ITINERARY_ENDPOINT]
    assert isinstance(parser, RoutedOcrParser)
    assert result.fields["amount"] == "68.00"


def test_routed_registry_falls_back_to_smart_when_undecidable(tmp_path: Path) -> None:
    registry, _, transport = _build(
        env={**ENV, CONFIG_PATH_ENV: str(_config(tmp_path, "routed"))},
        results=[_response(SMART_RESULT)],
    )
    parser = registry.resolve(InputKind.DOCUMENT)
    assert parser is not None

    result = parser.parse(_request())  # 无任何路由提示

    assert result.ok, result.error
    assert transport.ocr_urls == [BAIDU_OCR_ENDPOINT]
    assert result.parser_name == BAIDU_OCR_PARSER_NAME
    assert result.fields["amount"] == "10.00"


def test_routed_registry_serves_both_endpoints_from_one_registry() -> None:
    """同一装配结果可服务两类输入（两个专用适配器各自持有客户端与 token 缓存）。"""
    registry, _, transport = _build(
        routing=OcrRoutingMode.ROUTED,
        results=[_response(VAT_RESULT), _response(ITINERARY_RESULT)],
    )
    parser = registry.resolve(InputKind.IMAGE)
    assert isinstance(parser, RoutedOcrParser)
    assert registry.registered_kinds == frozenset(
        {InputKind.IMAGE, InputKind.DOCUMENT}
    )

    vat = parser.parse(_request({"filename": "电子发票.pdf"}))
    trip = parser.parse(_request({"filename": "行程单.pdf"}))

    assert vat.parser_name == VAT_INVOICE_PARSER_NAME
    assert trip.parser_name == ITINERARY_PARSER_NAME
    assert vat.fields["amount"] == "106.00"
    assert trip.fields["amount"] == "68.00"
    assert transport.ocr_urls == [
        BAIDU_VAT_INVOICE_ENDPOINT,
        BAIDU_TAXI_ITINERARY_ENDPOINT,
    ]


# --- 凭据缺失：两种模式都优雅降级 ---------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_parser_name"),
    [
        (OcrRoutingMode.SMART, BAIDU_OCR_PARSER_NAME),
        (OcrRoutingMode.ROUTED, ROUTED_PARSER_NAME),
    ],
)
def test_missing_credentials_degrades_gracefully(
    mode: OcrRoutingMode, expected_parser_name: str
) -> None:
    registry, reader, transport = _build(env={}, routing=mode)

    assert registry.registered_kinds == frozenset()
    assert registry.degraded is True
    assert registry.skipped_parsers == (expected_parser_name,)
    assert registry.routing is mode
    assert registry.resolve(InputKind.IMAGE) is None
    assert registry.resolve(InputKind.DOCUMENT) is None
    # 未注册 → 不触碰原件、不发任何请求。
    assert reader.calls == []
    assert transport.calls == []

    note = registry.notes[0]
    assert note.env_vars == (API_KEY_ENV, SECRET_KEY_ENV)  # 只有变量名
    assert API_KEY not in note.detail
    assert SECRET_KEY not in note.detail


def test_missing_credentials_note_hides_credential_values() -> None:
    registry, _, _ = _build(env={API_KEY_ENV: API_KEY}, routing=OcrRoutingMode.ROUTED)
    note = registry.notes[0]
    assert SECRET_KEY_ENV in note.detail
    assert API_KEY not in note.detail
