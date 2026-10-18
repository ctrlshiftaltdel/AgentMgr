"""OCR 端点路由配置：项目统一配置文件里的 ``[ocr]`` 节（Issue #52）。

设计依据：

* Issue #46 决策记录（所有者 2026-10-18）：端点路由是**配置项**——既支持
  ``smart``（统一走 ``multiple_invoice`` 智能财务票据识别，**与当前生产行为一致**，
  也是默认值），也支持 ``routed``（按输入类型路由到专用端点）；
* Issue #43 决策：全部（非秘密）设置集中在**一个**文件里（``tomllib``、详尽注释、
  零新增依赖）。本模块读的正是 ``app.export.config`` 使用的**同一个文件**与同一个
  ``AJAIMGR_CONFIG_FILE`` 口径（示例文件 ``config/ajaimgr.example.toml``）；
* 本模块只读 ``[ocr]`` 一节，**不重载**导出配置：导出配置的 fail closed 语义
  （缺必填项即失败）不适用于本项——路由是**可选项**，缺省即 ``smart``。

读取口径（与 ``app.export.config`` 一致；由测试断言两者常量相同，避免口径漂移）：

1. 显式 ``path`` 参数；
2. 环境变量 ``AJAIMGR_CONFIG_FILE``；
3. 默认 ``config/ajaimgr.toml``（相对当前工作目录）。

取值口径（Issue #52 验收标准逐条对应）：

| 情形 | 结果 |
|---|---|
| 配置文件**整体缺失** | 默认 ``smart`` |
| 文件存在但**不是合法 UTF-8 TOML** | **报错**（fail closed——文件既然存在，说明部署方想配置它，静默回默认会掩盖误配置） |
| ``[ocr]`` 节缺失 | 默认 ``smart`` |
| ``[ocr]`` 存在但不是表 | **报错** |
| ``[ocr].routing`` 键缺失 | 默认 ``smart`` |
| ``[ocr].routing`` 取值非法（含非字符串） | **报错**（fail closed） |
| ``[ocr].routing`` ∈ {``smart``, ``routed``} | 该值 |

硬边界：

* **不影响应用启动**：本模块只在装配解析器时被调用，导入期不读文件；
* **错误只报键名与允许取值**：不回显配置取值（与 ``app.export.config`` 同一纪律）；
  「文件不存在 / 不可读 / 非法 TOML」的消息会带配置路径本身（运维定位用，路径来自
  部署方自己的环境变量或参数）；
* **零新增依赖**：只用 ``tomllib``（标准库）与标准库类型；不做网络调用、不读数据库。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_ROUTING",
    "OCR_ROUTING_KEY",
    "OCR_ROUTING_SECTION",
    "OcrRoutingConfigError",
    "OcrRoutingMode",
    "coerce_routing_mode",
    "config_path_from_env",
    "load_ocr_routing",
]

#: 环境变量名：覆盖运行时配置文件路径（与 ``app.export.config`` 同一个变量）。
CONFIG_PATH_ENV = "AJAIMGR_CONFIG_FILE"

#: 默认运行时配置文件路径（相对当前工作目录，与导出配置同一文件）。
DEFAULT_CONFIG_PATH = "config/ajaimgr.toml"

#: 本模块读取的配置节与键。
OCR_ROUTING_SECTION = "ocr"
OCR_ROUTING_KEY = f"{OCR_ROUTING_SECTION}.routing"

#: 机器可读错误码（风格与 ``app.export.errors`` 一致；本模块不依赖导出域）。
CODE_CONFIG_INVALID = "config_invalid"


class OcrRoutingMode(str, Enum):
    """OCR 端点路由模式（``[ocr].routing``）。

    继承 ``str`` 便于直接落日志/配置回显，且与 TOML 字符串取值一一对应。
    """

    #: 统一走 ``multiple_invoice``（智能财务票据识别）——**默认**，与当前生产行为一致。
    SMART = "smart"
    #: 按输入类型/文件特征路由到专用端点（``vat_invoice`` / ``online_taxi_itinerary``）。
    ROUTED = "routed"


#: 缺省路由模式：``smart``（保守默认，与 #46 决策一致）。
DEFAULT_ROUTING = OcrRoutingMode.SMART


class OcrRoutingConfigError(Exception):
    """路由配置非法（**fail closed**）。

    ``code`` 是机器可读错误码；``message`` **只含配置键名与允许取值**，不回显配置
    取值。调用方（装配层）不吞该异常——非法配置必须显式失败，不得静默回默认。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> OcrRoutingConfigError:
    return OcrRoutingConfigError(code, message)


def _allowed_values() -> str:
    return ", ".join(mode.value for mode in OcrRoutingMode)


def config_path_from_env(environ: Mapping[str, str] | None = None) -> str:
    """返回运行时配置路径：``AJAIMGR_CONFIG_FILE`` 或默认值（与导出配置同口径）。"""
    source = os.environ if environ is None else environ
    raw = source.get(CONFIG_PATH_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_CONFIG_PATH
    return str(raw).strip()


def coerce_routing_mode(value: Any, *, where: str) -> OcrRoutingMode:
    """把取值归一化为 :class:`OcrRoutingMode`；非法取值 **fail closed**。

    ``where`` 是错误消息里的定位描述（如 ``config key 'ocr.routing'`` 或
    ``argument 'routing'``）。只接受**精确**取值（允许首尾空白），不做大小写折叠：
    ``"Smart"`` 属于非法取值而不是等价写法。消息不回显取值本身。
    """
    if isinstance(value, OcrRoutingMode):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        for mode in OcrRoutingMode:
            if mode.value == candidate:
                return mode
    raise _fail(
        CODE_CONFIG_INVALID,
        f"{where} has an unsupported value; allowed: {_allowed_values()}",
    )


def load_ocr_routing(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> OcrRoutingMode:
    """装载 ``[ocr].routing``（缺省/缺节/缺文件 → ``smart``；非法取值 → 报错）。

    只在**装配解析器**时调用，不在导入期调用：配置文件缺失或非法**不影响应用启动**
    （与导出配置同一约定）。
    """
    resolved = Path(path) if path is not None else Path(config_path_from_env(environ))
    try:
        raw_bytes = resolved.read_bytes()
    except FileNotFoundError:
        # 路由是可选项：整个配置文件缺失时按默认 ``smart`` 运行（= 当前生产行为）。
        return DEFAULT_ROUTING
    except OSError:
        raise _fail(
            CODE_CONFIG_INVALID,
            f"OCR routing configuration file is not readable: {resolved}",
        )

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise _fail(
            CODE_CONFIG_INVALID,
            f"configuration file is not valid UTF-8 TOML: {resolved}",
        )

    if not isinstance(data, Mapping):
        raise _fail(CODE_CONFIG_INVALID, "configuration root must be a table")

    section = data.get(OCR_ROUTING_SECTION)
    if section is None:
        # 同一个文件还承载导出设置；没有 ``[ocr]`` 节即未配置路由 → 默认 ``smart``。
        return DEFAULT_ROUTING
    if not isinstance(section, Mapping):
        raise _fail(
            CODE_CONFIG_INVALID,
            f"config key '{OCR_ROUTING_SECTION}' must be a table",
        )

    raw = section.get("routing")
    if raw is None:
        return DEFAULT_ROUTING
    return coerce_routing_mode(raw, where=f"config key '{OCR_ROUTING_KEY}'")
