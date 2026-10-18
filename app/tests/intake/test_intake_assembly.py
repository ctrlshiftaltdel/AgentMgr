"""默认解析器装配点（``build_default_parser_registry``）的纯逻辑测试。

覆盖 Issue #45 验收第 2 条（**无数据库、无网络、无真实凭据**）：

* 凭据齐全 → 注册百度票据解析器（``image`` / ``document``），未注册类型
  ``resolve`` 仍返回 ``None``（回退 ``ManualEntryParser`` 的语义不变）；
* 凭据缺失 → **不注册、不抛给调用方**，装配结果仍可用，且原件读取不被触碰；
* 降级**可观测**且日志**不含凭据值**（只含解析器标识与环境变量名）；
* 非凭据类构造失败**不被吞掉**（真实缺陷必须显式失败）；
* 装配本身不触网（默认传输惰性导入，只在真实解析时加载）。
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from app.integrations.baidu_invoice_ocr import (
    API_KEY_ENV,
    BAIDU_OCR_PARSER_NAME,
    SECRET_KEY_ENV,
    BaiduInvoiceOcrParser,
    HttpResponse,
    SourceDocument,
)
from app.intake import assembly
from app.intake.parsing import (
    InputKind,
    ManualEntryParser,
    ParseRequest,
    ParserRegistry,
)

API_KEY = "example-bd-ocr-api-key-0001"
SECRET_KEY = "example-bd-ocr-secret-key-9999"
ENV = {API_KEY_ENV: API_KEY, SECRET_KEY_ENV: SECRET_KEY}
TOKEN = "24.example-token.2592000.1763000000.282335-00000001"
LOGGER_NAME = "app.intake.assembly"


@dataclass
class StubTransport:
    """按 URL 区分 token 与识别的桩传输（**绝不触网**）。"""

    ocr_results: list[object] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        self.calls.append({"url": url, "headers": dict(headers), "body": body})
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


class StubReader:
    """只读原件桩（记录被请求的 ``source_file_id``）。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def read(self, source_file_id: int) -> SourceDocument:
        self.calls.append(source_file_id)
        return SourceDocument(
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
            mime_type="image/png",
            size_bytes=48,
            scan_status="clean",
        )


def _ocr_response(*, probability: float = 0.98) -> HttpResponse:
    """一份最小的 VAT 票据识别响应（金额取价税合计）。"""
    def field_value(word: str) -> list[dict[str, object]]:
        return [
            {
                "word": word,
                "probability": {"average": 0.99, "min": probability},
            }
        ]

    body = {
        "words_result_num": 1,
        "log_id": "1394226734160674816",
        "words_result": [
            {
                "type": "vat_invoice",
                "probability": 0.99,
                "result": {
                    "InvoiceNum": field_value("12345678"),
                    "InvoiceDate": field_value("2019年08月28日"),
                    "AmountInFiguers": field_value("106.00"),
                },
            }
        ],
    }
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


def _request(source_file_id: int = 7) -> ParseRequest:
    return ParseRequest(
        input_kind=InputKind.IMAGE,
        business_type="expense_claim",
        source_file_id=source_file_id,
    )


def _build(**kwargs: Any) -> tuple[ParserRegistry, StubReader, StubTransport]:
    reader = StubReader()
    transport = StubTransport(ocr_results=[_ocr_response()])
    registry = assembly.build_default_parser_registry(
        source_reader=reader, transport=transport, **kwargs
    )
    return registry, reader, transport


# --- 注册路径 -----------------------------------------------------------------


def test_registers_baidu_parser_for_image_and_document() -> None:
    """凭据齐全：注册百度解析器，未登记类型仍为 ``None``（回退人工录入）。"""
    registry, _, _ = _build(env=ENV)

    parser = registry.resolve(InputKind.IMAGE)
    assert isinstance(parser, BaiduInvoiceOcrParser)
    assert parser.parser_name == BAIDU_OCR_PARSER_NAME
    assert registry.resolve(InputKind.DOCUMENT) is parser
    assert registry.resolve(InputKind.TEXT) is None
    assert registry.resolve(InputKind.VOICE) is None
    assert registry.registered_kinds == frozenset(
        {InputKind.IMAGE, InputKind.DOCUMENT}
    )


def test_parse_through_registry_uses_injected_reader_and_transport() -> None:
    """注册后的解析器真正走注入的原件读取与桩传输（装配可端到端使用）。"""
    registry, reader, transport = _build(env=ENV)

    parser = registry.resolve(InputKind.IMAGE)
    assert parser is not None
    result = parser.parse(_request(source_file_id=11))

    assert result.ok
    assert reader.calls == [11]
    assert result.fields["amount"] == "106.00"
    assert result.fields["invoice_date"] == "2019-08-28"
    assert set(result.fields) == set(result.field_confidence)
    # token 一次 + 识别一次（两个 URL 都走注入的桩，未触网）。
    assert len(transport.calls) == 2


def test_registry_is_reusable_across_parses() -> None:
    """同一装配结果可复用：token 缓存生效（两次识别只取一次 token）。"""
    registry, _, transport = _build(env=ENV)
    parser = registry.resolve(InputKind.IMAGE)
    assert parser is not None

    assert parser.parse(_request()).ok
    assert parser.parse(_request()).ok

    token_calls = [call for call in transport.calls if "oauth" in call["url"]]
    assert len(transport.calls) == 3
    assert len(token_calls) == 1


# --- 降级路径 -----------------------------------------------------------------


def test_missing_credentials_degrades_without_raising() -> None:
    """凭据缺失：不注册、不抛异常，回退人工录入的语义不变。"""
    registry, reader, transport = _build(env={})

    assert registry.registered_kinds == frozenset()
    assert registry.resolve(InputKind.IMAGE) is None
    assert registry.resolve(InputKind.DOCUMENT) is None
    # 未注册时不触碰原件，也不发任何请求。
    assert reader.calls == []
    assert transport.calls == []

    manual = ManualEntryParser(values={"amount": "88.00"})
    result = manual.parse(_request())
    assert result.ok
    assert result.parser_name == "manual_entry"


def test_partial_credentials_also_degrade() -> None:
    """只配置一半凭据同样降级（fail closed，绝不半配置启动）。"""
    registry, _, _ = _build(env={API_KEY_ENV: API_KEY})

    assert registry.registered_kinds == frozenset()


def test_degradation_is_observable_without_credential_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """降级写 warning：含解析器标识与缺失变量名，**不含凭据值**。"""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        registry, _, _ = _build(env={API_KEY_ENV: API_KEY})

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
    ]
    assert messages, caplog.records
    rendered = "\n".join(messages)
    assert BAIDU_OCR_PARSER_NAME in rendered
    assert SECRET_KEY_ENV in rendered
    assert API_KEY not in rendered
    assert registry.registered_kinds == frozenset()


def test_registration_is_logged_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """成功注册同样可观测，且日志不含凭据值。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        registry, _, _ = _build(env=ENV)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]
    rendered = "\n".join(messages)
    assert BAIDU_OCR_PARSER_NAME in rendered
    assert API_KEY not in rendered
    assert SECRET_KEY not in rendered
    assert registry.registered_kinds == frozenset(
        {InputKind.IMAGE, InputKind.DOCUMENT}
    )


# --- 其它契约 -----------------------------------------------------------------


def test_non_credential_construction_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 ``MissingCredentialError`` 的构造失败必须显式抛出（不静默降级）。"""

    class BoomError(RuntimeError):
        pass

    def _boom(**kwargs: Any) -> Any:
        raise BoomError("boom")

    monkeypatch.setattr(assembly, "BaiduInvoiceOcrParser", _boom)

    with pytest.raises(BoomError):
        assembly.build_default_parser_registry(source_reader=StubReader(), env=ENV)


def test_assembly_never_opens_a_socket() -> None:
    """装配本身不触网：默认传输惰性导入，构造与注册阶段无 socket。"""
    with patch("socket.socket", side_effect=AssertionError("no sockets")):
        with patch(
            "socket.create_connection", side_effect=AssertionError("no sockets")
        ):
            registry, _, _ = _build(env=ENV)

    assert registry.registered_kinds == frozenset(
        {InputKind.IMAGE, InputKind.DOCUMENT}
    )
    assert socket.socket is not None  # patch 已还原
