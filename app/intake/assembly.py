"""默认解析器装配点：把外部解析器接进 ``ParserRegistry``（Issue #45）。

``ParserRegistry``（W1-26）定义了替换点，``BaiduInvoiceOcrParser``（#35/#37）实现了
``ExternalParser``，但产品代码里**没有任何注册**。本模块提供唯一的默认装配函数
``build_default_parser_registry``，使「已入库图片/文件 → 解析 → 草稿 → 人工确认」
在服务层端到端可达。

装配语义（对齐 PLAN §4.2「无供应商也可工作」）：

* 凭据齐全 → 构造并注册百度票据解析器（``image`` 与 ``document`` 两类输入）；
* 凭据缺失（构造抛 ``MissingCredentialError``）→ **不注册**该解析器，且**不把异常
  抛给调用方**：调用方拿到的仍是可用的 ``ParserRegistry``，未注册类型
  ``resolve`` 返回 ``None``，上层按既有语义回退 ``ManualEntryParser``；
* 降级**可观测**：写一条 warning 日志，只含解析器标识与环境变量名（
  ``MissingCredentialError`` 的消息本身只提变量名），**绝不含凭据值**；构造其它异常
  **不吞**（那属于真实缺陷，必须显式失败）。

边界：本模块只做「构造 + 注册」，不做网络调用（默认传输惰性导入 ``httpx``，仅在
真实解析时才加载）、不读数据库（原件读取由注入的 ``SourceReader`` 负责）、不新增
端点、不新增依赖。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime

from app.integrations.baidu_invoice_ocr import (
    BAIDU_OCR_ENDPOINT,
    BAIDU_OCR_PARSER_NAME,
    BAIDU_TOKEN_ENDPOINT,
    DEFAULT_TIMEOUT_SECONDS,
    BaiduInvoiceOcrParser,
    SourceReader,
    Transport,
)
from app.intake.parsing import MissingCredentialError, ParserRegistry

__all__ = ["build_default_parser_registry"]

_LOGGER = logging.getLogger(__name__)


def build_default_parser_registry(
    *,
    source_reader: SourceReader,
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    clock: Callable[[], datetime] | None = None,
    endpoint: str = BAIDU_OCR_ENDPOINT,
    token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ParserRegistry:
    """构造默认 ``ParserRegistry``（凭据缺失时优雅降级为「无外部解析器」）。

    ``source_reader`` 是**必填**：外部解析器需要把 ``source_file_id`` 换成只读原件
    （生产实现见 ``app.intake.source_reader.SourceFileReader``，测试注入桩即可，绝不
    触网）。``env`` / ``transport`` / ``clock`` 只用于测试与联调注入；``env`` 省略时
    凭据从进程环境变量读取（``BDOCR_API_KEY`` / ``BDOCR_SECRET_KEY``，兼容旧名）。

    返回的 ``ParserRegistry`` **可被调用方长期复用**（解析器实例持有 token 缓存与
    传输客户端）。未注册的输入类型 ``resolve`` 返回 ``None``，由上层回退人工录入。
    """
    registry = ParserRegistry()
    try:
        parser = BaiduInvoiceOcrParser(
            source_reader=source_reader,
            transport=transport,
            clock=clock,
            env=env,
            endpoint=endpoint,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )
    except MissingCredentialError as exc:
        # 只记录解析器标识与缺失的环境变量名（消息不含凭据值）。
        _LOGGER.warning(
            "intake parser assembly: %s not registered, falling back to manual "
            "entry for image/document input (%s)",
            BAIDU_OCR_PARSER_NAME,
            exc,
        )
        return registry

    registry.register(parser)
    _LOGGER.info(
        "intake parser assembly: registered %s for %s",
        parser.parser_name,
        ",".join(sorted(kind.value for kind in parser.supported_input_kinds)),
    )
    return registry
