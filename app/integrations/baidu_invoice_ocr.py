"""百度智能云「智能财务票据识别」适配器：``multiple_invoice``（Issue #35）。

实现依据（**必读**）：Issue #35 正文与两条评论（含 Master 核对官方文档后的「实现前
补充」）、``docs/research/W2-ocr-asr-vendor-selection.md`` §4/§6。

本模块把百度 ``multiple_invoice`` 接成 ``app.intake.parsing.ExternalParser`` 的一个
实现，输出**只进草稿**的 ``ParseResult``；人工确认仍是进入正式记录的唯一路径。它
**不修改** ``ParseRequest``（该请求只有 ``source_file_id``、没有文件字节），而是由调用
方注入只读的 ``SourceReader`` 换取 ``SourceDocument``。

接口事实（逐条对齐官方文档与 Issue #35 评论）：

* ``POST https://aip.baidubce.com/rest/2.0/ocr/v1/multiple_invoice``，
  ``Content-Type: application/x-www-form-urlencoded``，``access_token`` 放 URL 参数；
* 输入四选一：``image`` / ``url`` / ``pdf_file`` / ``ofd_file``；其中
  ``image``/``pdf_file``/``ofd_file`` 需 **base64 → urlencode**，编码后 **≤ 4M**；
  图片最短边 ≥15px、最长边 ≤4096px；支持 jpg/jpeg/png/bmp；PDF/OFD 可用
  ``pdf_file_num`` / ``ofd_file_num`` 指定页码（默认第 1 页）；
* 可选 ``probability=true``（文档称「返回字段置信度」）、``location``、
  ``verify_parameter``；本模块固定传 ``probability=true``，其余**不传**（不臆造）；
* 返回 ``words_result[]``：每项 ``probability``（**分类置信度**，始终返回）、``type``、
  ``left/top/width/height``、``result``（按类型给出的字段集合）。``log_id`` 在顶层；
* **4 类只分类不识别**：``limit_invoice`` / ``shopping_receipt`` / ``pos_invoice`` /
  ``others``——``result`` 为空，本模块只产出 ``invoice_type``，**不伪造**金额等字段；
* 金额口径：``TotalAmount`` 是**不含税**合计，``AmountInFiguers`` 是**价税合计**；
  报销金额 ``amount`` **只取后者**（取错会造成系统性少报）。

关键设计：

1. **Client 与 Parser 分离**。``BaiduInvoiceOcrClient`` 只管 OAuth token（按
   ``expires_in`` 提前 60 秒过期、同一实例内加锁避免并发重复取 token）与一次识别
   请求；``BaiduInvoiceOcrParser`` 负责预检、字段归一化与失败收敛。
2. **预检 fail closed（发请求前）**：``scan_status != 'clean'``、MIME 不在允许列表、
   空内容、base64+urlencode 后超过 4MiB、图片像素越界、图片却指定页码——一律拒绝，
   **连 token 都不取**。图片尺寸用标准库解析 PNG/JPEG/BMP 头；解析不了就跳过像素
   检查并记一条 warning 日志（不阻断，best-effort）。
3. **字段归一化**（只映射确实出现的字段，键集合与 ``field_confidence`` 严格一致）：
   ``invoice_type`` / ``invoice_code`` / ``invoice_no`` / ``invoice_date``（ISO
   ``YYYY-MM-DD``，支持 ``2019年08月28日``）/ ``amount``（价税合计）/
   ``tax_amount`` / ``buyer_name`` / ``buyer_tax_id`` / ``seller_name`` /
   ``seller_tax_id``。金额统一为 2 位小数字符串（避免浮点与 JSON 序列化问题）。
4. **置信度**：``words_result[].probability`` 是**票种分类置信度**，只允许用于
   ``invoice_type`` 这一个字段；其余字段若字段项自带 ``probability``（取值 >1 视为
   0–100）则归一化使用，没有就回退 ``0.5``（低于默认阈值 0.75 → 必然人工复核）。
   **绝不**把分类置信度当金额/税号等字段置信度。
5. **失败收敛**：预检失败、传输异常、4xx、业务错误码、响应形态异常，统一产出
   ``error='provider_error'`` 的失败结果；异常原文、凭据、响应体**不进结果、不进
   日志**。``log_id`` 只写 debug 日志（定位用），**不进** ``ParseResult.fields``。
   错误码按 Issue #35 的约定映射（``110/111`` token、``17/18/19`` 配额、
   ``216631`` 尺寸、``282000`` 服务），未知一律 ``provider_error``。
6. **多票（混贴）**：``words_result`` 为空 → 失败；多于一张时只解析**金额最大**的
   一张（并列取第一张），因为本接口下游一次只接收一张票的草稿。**不伪造**
   ``invoice_count`` 之类下游不认识的业务字段；一页多票的拆分属于后续 Issue。

待真实响应核实（**未验证**）：

* ``result`` 的确切形态。Issue #35 描述为「字段数组，元素形如 ``{"word": "..."}``」，
  研究文档写作 ``result{...}``。本模块**同时容忍** ``result`` 为映射（``{"TotalAmount":
  "100.00"}``）与数组（``{"key": "TotalAmount", "word": "100.00"}`` 或
  ``{"TotalAmount": {"word": "100.00", "probability": 0.9}}``）；无键名的裸
  ``{"word": ...}`` 无法安全映射，直接忽略。
* 字段置信度的实际返回形态与取值域（0–1 还是 0–100）。
* 4M 的精确口径（十进制 4,000,000 还是 4MiB）、``probability=true`` 是否对
  ``multiple_invoice`` 生效、各票种字段名。
* 错误码表的完整性（文档未给全表）。

网络出口通过可注入的 ``transport`` 完成：生产默认 ``httpx``（**惰性导入**，仅真实
请求时加载）；测试一律注入桩，**绝不调用真实百度接口、不使用真实 Key**。本模块只
依赖标准库与项目既有模块。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote, urlencode

from app.intake.parsing import (
    ExternalParser,
    InputKind,
    MissingCredentialError,
    ParseRequest,
    ParseResult,
    ParserKind,
)

__all__ = [
    "API_KEY_ENV",
    "BAIDU_OCR_ENDPOINT",
    "BAIDU_OCR_PARSER_NAME",
    "BAIDU_OCR_PARSER_VERSION",
    "BAIDU_TOKEN_ENDPOINT",
    "CLASSIFICATION_ONLY_TYPES",
    "DEFAULT_FIELD_CONFIDENCE",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ENCODED_BYTES",
    "MAX_IMAGE_SIDE",
    "MIN_IMAGE_SIDE",
    "PROVIDER_ERROR",
    "SECRET_KEY_ENV",
    "TOKEN_ERROR_CODES",
    "TOKEN_EXPIRY_MARGIN_SECONDS",
    "BaiduInvoiceOcrClient",
    "BaiduInvoiceOcrParser",
    "HttpResponse",
    "InvoiceOcrError",
    "MissingCredentialError",
    "OcrProviderError",
    "SourceDocument",
    "SourceReader",
    "Transport",
    "TransportError",
    "TransportTimeout",
]

_LOGGER = logging.getLogger(__name__)

# --- 常量 ---------------------------------------------------------------------

BAIDU_OCR_ENDPOINT = "https://aip.baidubce.com/rest/2.0/ocr/v1/multiple_invoice"
BAIDU_TOKEN_ENDPOINT = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_OCR_PARSER_NAME = "baidu_cloud/multiple_invoice"
BAIDU_OCR_PARSER_VERSION = "1"

#: 凭据环境变量名（**只从环境变量读取**，不落仓库、不落日志）。
API_KEY_ENV = "AJAIMGR_BAIDU_API_KEY"
SECRET_KEY_ENV = "AJAIMGR_BAIDU_SECRET_KEY"

#: 默认请求超时（秒）。超时即 ``provider_error``，不自动重发。
DEFAULT_TIMEOUT_SECONDS = 10.0

#: token 过期提前量（秒）：剩余寿命不足该值即视为过期、重新获取。
TOKEN_EXPIRY_MARGIN_SECONDS = 60

#: base64+urlencode 后的请求体上限。文档写「4M」，按二进制 MiB 取严。
MAX_ENCODED_BYTES = 4 * 1024 * 1024

#: 图片像素约束（文档：最短边 ≥15px、最长边 ≤4096px）。
MIN_IMAGE_SIDE = 15
MAX_IMAGE_SIDE = 4096

#: 无逐字段置信度时的保守回退值（< 默认阈值 0.75 → 全部进 ``needs_review``）。
DEFAULT_FIELD_CONFIDENCE = 0.5

#: ``ParseResult.error`` 的唯一失败码：不把供应商错误码泄漏给上层。
PROVIDER_ERROR = "provider_error"

#: 4 类只分类不识别（``result`` 为空，只产出 ``invoice_type``）。
CLASSIFICATION_ONLY_TYPES: frozenset[str] = frozenset(
    {"limit_invoice", "shopping_receipt", "pos_invoice", "others"}
)

#: token 失效错误码（需刷新一次后重试）。
TOKEN_ERROR_CODES: frozenset[str] = frozenset({"110", "111"})

#: 业务错误码 → 内部稳定原因（仅用于日志；对外仍是 ``provider_error``）。
_ERROR_REASONS: Mapping[str, str] = {
    "110": "invalid_token",
    "111": "token_expired",
    "17": "quota_daily_exceeded",
    "18": "quota_qps_exceeded",
    "19": "quota_total_exceeded",
    "216631": "image_size_invalid",
    "282000": "provider_service_error",
}

#: MIME → (请求字段名, 载体类别)。
#:
#: 仅允许文档列出的 jpg/jpeg/png/bmp 与 PDF/OFD；``url`` 模式不实现（本适配器只
#: 处理已入库的原件，避免把外链交给第三方抓取）。
_MIME_SPECS: Mapping[str, tuple[str, str]] = {
    "image/jpeg": ("image", "image"),
    "image/jpg": ("image", "image"),
    "image/png": ("image", "image"),
    "image/bmp": ("image", "image"),
    "image/x-ms-bmp": ("image", "image"),
    "application/pdf": ("pdf_file", "pdf"),
    "application/ofd": ("ofd_file", "ofd"),
}

#: 未知类型的通用候选（只映射确实出现的字段）。
_ID_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invoice_code", ("InvoiceCode", "invoice_code")),
    ("invoice_no", ("InvoiceNum", "InvoiceNo", "invoice_num", "invoice_no")),
)

_VAT_ALIASES = _ID_ALIASES + (
    ("invoice_date", ("InvoiceDate",)),
    # 只取价税合计；绝不回退到不含税的 TotalAmount。
    ("amount", ("AmountInFiguers",)),
    ("tax_amount", ("TotalTax",)),
    ("buyer_name", ("PurchaserName",)),
    ("buyer_tax_id", ("PurchaserRegisterNum",)),
    ("seller_name", ("SellerName",)),
    ("seller_tax_id", ("SellerRegisterNum",)),
)

_TAXI_ALIASES = _ID_ALIASES + (
    ("invoice_date", ("date", "Date", "InvoiceDate")),
    ("amount", ("TotalFare", "Fare", "fare")),
)

_TRAIN_ALIASES = _ID_ALIASES + (
    ("invoice_date", ("date", "Date", "StartDate", "start_date", "InvoiceDate")),
    ("amount", ("TotalAmount", "total_amount", "amount", "Fare", "fare")),
)

_AIR_ALIASES = _ID_ALIASES + (
    ("invoice_no", ("InvoiceNum", "invoice_num", "Number")),
    ("invoice_date", ("date", "Date", "InvoiceDate", "start_date")),
    ("amount", ("TotalAmount", "total_amount", "Fare", "fare", "amount")),
)

_QUOTA_ALIASES = _ID_ALIASES + (
    ("invoice_date", ("InvoiceDate", "Date", "date")),
    ("amount", ("Amount", "amount", "TotalAmount")),
)

_GENERIC_ALIASES = _ID_ALIASES + (
    ("invoice_date", ("InvoiceDate", "Date", "date", "start_date", "StartDate")),
    (
        "amount",
        (
            "AmountInFiguers",
            "Amount",
            "amount",
            "TotalAmount",
            "TotalFare",
            "Fare",
            "fare",
        ),
    ),
    ("tax_amount", ("TotalTax", "Tax", "tax")),
    ("buyer_name", ("PurchaserName", "BuyerName", "buyer_name")),
    ("buyer_tax_id", ("PurchaserRegisterNum", "BuyerTaxID", "buyer_tax_id")),
    ("seller_name", ("SellerName", "Seller", "seller_name")),
    ("seller_tax_id", ("SellerRegisterNum", "SellerTaxID", "seller_tax_id")),
)

#: 票种 → 字段别名。未登记票种走通用表；票种字段名**以真实响应为准**。
_TYPE_ALIASES: Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "vat_invoice": _VAT_ALIASES,
    "taxi_receipt": _TAXI_ALIASES,
    "taxi_online_ticket": _TAXI_ALIASES,
    "train_ticket": _TRAIN_ALIASES,
    "air_ticket": _AIR_ALIASES,
    "quota_invoice": _QUOTA_ALIASES,
}

# --- 异常与传输抽象 -----------------------------------------------------------


class InvoiceOcrError(Exception):
    """适配器内部错误基类（消息只含稳定原因码，不含原文/凭据/响应体）。"""


class TransportError(InvoiceOcrError):
    """底层网络传输失败（非超时）。"""


class TransportTimeout(TransportError):
    """底层网络传输超时（**不得盲目重发**）。"""


class OcrProviderError(InvoiceOcrError):
    """供应商标识化失败：``reason`` 是稳定原因码，可安全写日志。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HttpResponse(NamedTuple):
    """传输层返回：HTTP 状态码 + 原始响应体字节。"""

    status_code: int
    body: bytes


#: 传输函数签名：``(url, headers, body, timeout) -> HttpResponse``。
Transport = Callable[[str, Mapping[str, str], bytes, float], HttpResponse]


def _httpx_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> HttpResponse:
    """默认传输实现（``httpx``，惰性导入以免构造解析器就加载网络库）。

    超时转 ``TransportTimeout``、其它 HTTP 错误转 ``TransportError``；本函数
    **不重试**。
    """
    import httpx  # 局部导入：仅在实际发起请求时加载。

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=dict(headers), content=body)
    except httpx.TimeoutException as exc:
        raise TransportTimeout("provider request timed out") from exc
    except httpx.HTTPError as exc:
        raise TransportError("provider transport error") from exc
    return HttpResponse(response.status_code, response.content)


# --- 源文件读取（注入，不改 ParseRequest） ------------------------------------


@dataclass(frozen=True)
class SourceDocument:
    """只读原件（由 ``SourceReader`` 提供；**不携带任何解析结果**）。

    ``scan_status`` 取自 ``source_files.scan_status``；只有 ``clean`` 才允许送往第三
    方识别。
    """

    content: bytes
    mime_type: str
    size_bytes: int
    scan_status: str


class SourceReader(Protocol):
    """按 ``source_file_id`` 读取**只读**原件的协议（由调用方注入实现）。"""

    def read(self, source_file_id: int) -> SourceDocument:
        """返回原件；不存在/不可读时应抛异常（由 ``ExternalParser`` 收敛为失败）。"""


# --- 小工具 -------------------------------------------------------------------


def _clean_str(value: Any) -> str | None:
    """去空白后的非空字符串；``None`` / 空白 / 布尔值一律视为无值。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _normalize_mime(mime_type: str | None) -> str:
    """取 MIME 主类型并小写（去掉 ``; charset=...`` 等参数）。"""
    if not mime_type:
        return ""
    return str(mime_type).split(";", 1)[0].strip().lower()


def _coerce_page(page: Any) -> int | None:
    """页码只接受 ≥1 的整数（``bool`` 不算）；其它一律 ``None``（非法）。"""
    if page is None or isinstance(page, bool):
        return None
    if isinstance(page, int):
        return page if page >= 1 else None
    return None


def _normalize_confidence(value: Any) -> float | None:
    """把渠道置信度归一化到 ``[0, 1]``；无法解析返回 ``None``。

    取值 >1 视为 0–100 制（如 ``92`` → ``0.92``）；非有限值/负数丢弃。文档未给出
    字段置信度取值域，此规则**待真实响应核实**。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if number > 1:
        number /= 100.0
    return min(1.0, number)


def _field_confidence(value: Any) -> float:
    """字段置信度：有可解析值就用，否则回退 ``DEFAULT_FIELD_CONFIDENCE``。"""
    normalized = _normalize_confidence(value)
    return DEFAULT_FIELD_CONFIDENCE if normalized is None else normalized


_AMOUNT_NOISE_RE = re.compile(r"[,\s￥¥$]")
_DATE_PATTERNS = (
    re.compile(r"^(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)


def _normalize_amount(raw: str) -> str:
    """金额 → 2 位小数字符串；无法解析时保留原值（交给人工复核）。"""
    cleaned = _AMOUNT_NOISE_RE.sub("", raw).replace("，", "")
    if not cleaned:
        return raw
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return raw
    if not value.is_finite():
        return raw
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _normalize_invoice_date(raw: str) -> str:
    """开票日期 → ISO ``YYYY-MM-DD``；无法解析时保留原值（交给人工复核）。"""
    text = raw.strip()
    for pattern in _DATE_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        try:
            value = date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
        except ValueError:
            continue
        return value.isoformat()
    return text


#: 字段值的归一化函数（日期 → ISO、金额 → 2 位小数字符串）。
_TRANSFORMS: Mapping[str, Callable[[str], str]] = {
    "amount": _normalize_amount,
    "invoice_date": _normalize_invoice_date,
}


# --- 图片尺寸（标准库解析，best-effort） --------------------------------------

_JPEG_SOF_MARKERS: frozenset[int] = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _png_dimensions(content: bytes) -> tuple[int, int] | None:
    """PNG：签名 + ``IHDR`` 的宽高（大端 4 字节）。"""
    if (
        len(content) < 24
        or content[:8] != b"\x89PNG\r\n\x1a\n"
        or content[12:16] != b"IHDR"
    ):
        return None
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    """JPEG：遍历段直到 SOFn，取高/宽（大端 2 字节）。"""
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        return None
    position = 2
    while position + 4 <= len(content):
        if content[position] != 0xFF:
            position += 1
            continue
        marker = content[position + 1]
        if marker == 0xFF:  # 填充字节
            position += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        if marker == 0xD9:  # EOI，未遇到 SOF
            return None
        segment_length = int.from_bytes(content[position + 2 : position + 4], "big")
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if position + 9 > len(content):
                return None
            height = int.from_bytes(content[position + 5 : position + 7], "big")
            width = int.from_bytes(content[position + 7 : position + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return width, height
        position += 2 + segment_length
    return None


def _bmp_dimensions(content: bytes) -> tuple[int, int] | None:
    """BMP：``BM`` + DIB 头宽高（小端有符号 4 字节，高度可为负）。"""
    if len(content) < 26 or content[:2] != b"BM":
        return None
    width = abs(int.from_bytes(content[18:22], "little", signed=True))
    height = abs(int.from_bytes(content[22:26], "little", signed=True))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _image_dimensions(content: bytes, mime: str) -> tuple[int, int] | None:
    """按 MIME 解析图片宽高；不支持的格式返回 ``None``。"""
    if mime == "image/png":
        return _png_dimensions(content)
    if mime == "image/jpeg":
        return _jpeg_dimensions(content)
    if mime in ("image/bmp", "image/x-ms-bmp"):
        return _bmp_dimensions(content)
    return None


# --- 预检 ---------------------------------------------------------------------


def _precheck(document: SourceDocument, page: Any) -> str | None:
    """发请求前的本地预检；返回稳定拒绝原因，通过返回 ``None``。

    顺序：扫描状态 → MIME → 空内容 → 页码 → 编码后大小 → 图片像素。拒绝原因只写
    日志（常量，不含文件内容）。
    """
    if str(document.scan_status).strip().lower() != "clean":
        return "scan_status_not_clean"

    mime = _normalize_mime(document.mime_type)
    spec = _MIME_SPECS.get(mime)
    if spec is None:
        return "unsupported_mime"

    if not document.content:
        return "empty_content"

    field_name, carrier = spec
    if page is not None:
        if carrier == "image" or _coerce_page(page) is None:
            return "invalid_page"

    encoded = base64.b64encode(document.content).decode("ascii")
    if len(urlencode({field_name: encoded})) > MAX_ENCODED_BYTES:
        return "payload_too_large"

    if carrier == "image":
        dimensions = _image_dimensions(document.content, mime)
        if dimensions is None:
            _LOGGER.warning(
                "baidu_invoice_ocr: image dimensions unavailable, "
                "skipping pixel precheck"
            )
        else:
            width, height = dimensions
            if min(width, height) < MIN_IMAGE_SIDE:
                return "image_too_small"
            if max(width, height) > MAX_IMAGE_SIDE:
                return "image_too_large"

    return None


# --- 响应归一化 ---------------------------------------------------------------

#: 数组元素中不属于业务字段的元数据键（裸 ``word`` 无法安全映射）。
_FIELD_META_KEYS: frozenset[str] = frozenset(
    {
        "word",
        "probability",
        "row",
        "column",
        "location",
        "left",
        "top",
        "width",
        "height",
        "key",
        "name",
        "field",
        "title",
        "value",
        "text",
        "content",
    }
)


def _unwrap_field(value: Any, item_confidence: Any) -> tuple[Any, Any]:
    """展开字段值（``{"word": ..., "probability": ...}``）并取出置信度。"""
    confidence = item_confidence
    if isinstance(value, Mapping):
        if "probability" in value:
            confidence = value["probability"]
        for wrapper in ("word", "value", "text", "content"):
            if wrapper in value:
                value = value[wrapper]
                break
        else:
            value = None
    return value, confidence


def _collect_named_fields(result: Any) -> dict[str, tuple[Any, Any]]:
    """把 ``result`` 摊平成 ``字段名 → (值, 置信度)``（**容忍两种返回形态**）。

    * 映射形态：``{"TotalAmount": "100.00"}`` 或
      ``{"TotalAmount": {"word": "100.00", "probability": 0.9}}``；
    * 数组形态：元素带标识键（``key``/``name``/``field``/``title``）+ ``word``；
      或元素直接以业务字段名为键；
    * 无标识键的裸 ``{"word": ...}`` 无法安全映射 → 忽略（不臆造）。

    同一字段名重复出现时保留首次出现的值。形态**待真实响应核实**。
    """
    collected: dict[str, tuple[Any, Any]] = {}
    if isinstance(result, Mapping):
        for key, value in result.items():
            if not isinstance(key, str):
                continue
            collected.setdefault(key, _unwrap_field(value, None))
        return collected

    if isinstance(result, list):
        for item in result:
            if not isinstance(item, Mapping):
                continue
            item_confidence = item.get("probability")
            identifier: str | None = None
            for key in ("key", "name", "field", "title"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    identifier = candidate.strip()
                    break
            if identifier is not None:
                raw: Any = None
                for wrapper in ("word", "value", "text", "content"):
                    if wrapper in item:
                        raw = item[wrapper]
                        break
                collected.setdefault(
                    identifier, _unwrap_field(raw, item_confidence)
                )
                continue
            for key, value in item.items():
                if not isinstance(key, str) or key in _FIELD_META_KEYS:
                    continue
                collected.setdefault(key, _unwrap_field(value, item_confidence))
    return collected


def _aliases_for(
    invoice_type: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """返回票种对应的字段别名表（未登记票种走通用表）。"""
    return _TYPE_ALIASES.get(invoice_type, _GENERIC_ALIASES)


def _extract_invoice(
    entry: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, float]]:
    """把``words_result`` 的一项归一化为统一字段 + 逐字段置信度。

    ``invoice_type`` 使用百度返回的 ``type`` **原值**（如 ``vat_invoice``），置信度
    沿用该票的分类置信度——这是唯一允许使用分类置信度的场景（票种字段本身）。
    其余字段只映射确实出现且非空的值；映射不到就不放该键。
    """
    invoice_type = _clean_str(entry.get("type"))
    if invoice_type is None:
        raise OcrProviderError("missing_invoice_type")

    type_confidence = _normalize_confidence(entry.get("probability"))
    fields: dict[str, Any] = {"invoice_type": invoice_type}
    confidence: dict[str, float] = {
        "invoice_type": (
            DEFAULT_FIELD_CONFIDENCE
            if type_confidence is None
            else type_confidence
        )
    }

    # 4 类只分类不识别：不尝试从空 ``result`` 造字段。
    if invoice_type in CLASSIFICATION_ONLY_TYPES:
        return fields, confidence

    named = _collect_named_fields(entry.get("result"))
    for unified_key, candidates in _aliases_for(invoice_type):
        for candidate in candidates:
            if candidate not in named:
                continue
            raw, raw_confidence = named[candidate]
            text = _clean_str(raw)
            if text is None:
                continue
            transform = _TRANSFORMS.get(unified_key)
            value = transform(text) if transform is not None else text
            if not value:
                continue
            fields[unified_key] = value
            confidence[unified_key] = _field_confidence(raw_confidence)
            break
    return fields, confidence


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalize_response(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, float]]:
    """从响应中选一张票并归一化（多票取金额最大者，并列取第一张）。"""
    entries = data.get("words_result")
    if not isinstance(entries, list) or not entries:
        raise OcrProviderError("empty_result")

    best_fields: dict[str, Any] | None = None
    best_confidence: dict[str, float] | None = None
    best_amount: Decimal | None = None

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        try:
            fields, confidence = _extract_invoice(entry)
        except OcrProviderError:
            continue
        amount = _decimal_or_none(fields.get("amount"))
        if best_fields is None or (
            amount is not None
            and (best_amount is None or amount > best_amount)
        ):
            best_fields = fields
            best_confidence = confidence
            best_amount = amount

    if best_fields is None or best_confidence is None:
        raise OcrProviderError("empty_result")
    return best_fields, best_confidence


# --- 客户端（OAuth + 识别） ---------------------------------------------------


class BaiduInvoiceOcrClient:
    """百度 ``multiple_invoice`` 客户端：OAuth token 缓存 + 单次识别请求。

    * ``api_key`` / ``secret_key`` 默认从环境变量读取；缺失即 ``MissingCredentialError``
      （fail closed），错误信息只提环境变量名；
    * token 按 ``expires_in`` 缓存，剩余寿命不足 60 秒即重新获取；同一实例内用锁
      串行化 token 获取（避免并发重复取 token，不做 token 预热）；
    * 401 或 ``error_code`` ∈ {110, 111} → 失效缓存并**刷新一次**后重试一次；
      其它错误**不重试**；
    * 所有失败抛 ``OcrProviderError``（稳定原因码），不携带响应体/凭据原文。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        endpoint: str = BAIDU_OCR_ENDPOINT,
        token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved_key = (
            api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        )
        resolved_secret = (
            secret_key if secret_key is not None else os.environ.get(SECRET_KEY_ENV)
        )
        if not resolved_key or not str(resolved_key).strip():
            raise MissingCredentialError(
                "missing Baidu Cloud credential: set "
                f"{API_KEY_ENV} and {SECRET_KEY_ENV} in the environment"
            )
        if not resolved_secret or not str(resolved_secret).strip():
            raise MissingCredentialError(
                "missing Baidu Cloud credential: set "
                f"{SECRET_KEY_ENV} in the environment"
            )

        self._api_key = str(resolved_key).strip()
        self._secret_key = str(resolved_secret).strip()
        self._transport: Transport = (
            transport if transport is not None else _httpx_transport
        )
        self._clock: Callable[[], datetime] = (
            clock if clock is not None else (lambda: datetime.now(UTC))
        )
        self._endpoint = endpoint
        self._token_endpoint = token_endpoint
        self._timeout = timeout
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    # --- token ---------------------------------------------------------------

    def _access_token_value(self) -> str:
        """返回可用 token；缓存有效则复用，否则在锁内获取一次。"""
        with self._token_lock:
            if (
                self._access_token is not None
                and self._token_expires_at is not None
                and self._clock()
                < self._token_expires_at
                - timedelta(seconds=TOKEN_EXPIRY_MARGIN_SECONDS)
            ):
                return self._access_token
            return self._fetch_token_locked()

    def _fetch_token_locked(self) -> str:
        """请求一次 token（调用方必须持有 ``_token_lock``）。"""
        query = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._api_key,
                "client_secret": self._secret_key,
            }
        )
        url = f"{self._token_endpoint}?{query}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = self._transport(url, headers, b"", self._timeout)
        except TransportTimeout as exc:
            raise OcrProviderError("provider_token_timeout") from exc
        except TransportError as exc:
            raise OcrProviderError("provider_token_transport_error") from exc

        data = _decode_json(response)
        token = _clean_str(data.get("access_token"))
        if response.status_code >= 400 or token is None:
            # 不把 ``error``/``error_description`` 带出（可能含客户端信息）。
            raise OcrProviderError("provider_token_error")

        self._access_token = token
        self._token_expires_at = self._expiration_from(data)
        return token

    def _invalidate_token(self, stale: str) -> None:
        """仅在缓存仍是失效 token 时清空（避免覆盖其它线程刚刷新的新 token）。"""
        with self._token_lock:
            if self._access_token == stale:
                self._access_token = None
                self._token_expires_at = None

    def _expiration_from(self, data: Mapping[str, Any]) -> datetime | None:
        raw = data.get("expires_in")
        try:
            expires_in = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if expires_in <= 0:
            return None
        return self._clock() + timedelta(seconds=expires_in)

    # --- 识别 ----------------------------------------------------------------

    def recognize(
        self, content: bytes, mime_type: str, *, page: int | None = None
    ) -> dict[str, Any]:
        """调用一次 ``multiple_invoice``，返回原始响应 dict。

        ``image``/``pdf_file``/``ofd_file`` 一律 base64 → urlencode；``probability``
        固定为 ``true``（请求字段置信度）。PDF/OFD 可传 ``page``（默认第 1 页）；
        图片传 ``page`` 直接拒绝。失败抛 ``OcrProviderError``。
        """
        if not content:
            raise OcrProviderError("empty_content")

        mime = _normalize_mime(mime_type)
        spec = _MIME_SPECS.get(mime)
        if spec is None:
            raise OcrProviderError("unsupported_mime")
        field_name, carrier = spec

        if page is not None:
            page_number = _coerce_page(page)
            if page_number is None or carrier == "image":
                raise OcrProviderError("invalid_page")
        else:
            page_number = None

        encoded = base64.b64encode(content).decode("ascii")
        if len(urlencode({field_name: encoded})) > MAX_ENCODED_BYTES:
            raise OcrProviderError("payload_too_large")

        params: dict[str, str] = {field_name: encoded, "probability": "true"}
        if page_number is not None:
            page_field = "pdf_file_num" if carrier == "pdf" else "ofd_file_num"
            params[page_field] = str(page_number)
        body = urlencode(params).encode("utf-8")

        for attempt in (0, 1):
            token = self._access_token_value()
            response = self._post(token, body)
            data = _decode_json(response)
            code = _error_code(data)

            if response.status_code == 401 or code in TOKEN_ERROR_CODES:
                if attempt == 0:
                    self._invalidate_token(token)
                    continue
                raise OcrProviderError(
                    _ERROR_REASONS.get(code or "", "invalid_token")
                )

            if response.status_code >= 400:
                raise OcrProviderError("provider_http_error")
            if code is not None:
                raise OcrProviderError(_ERROR_REASONS.get(code, PROVIDER_ERROR))
            if not isinstance(data.get("words_result"), list):
                raise OcrProviderError("invalid_response")
            return data

        raise OcrProviderError("invalid_token")  # pragma: no cover - 循环不可达

    def _post(self, token: str, body: bytes) -> HttpResponse:
        """发起**一次**识别请求；传输失败映射为稳定原因码（不重试）。"""
        url = f"{self._endpoint}?access_token={quote(token, safe='')}"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            return self._transport(url, headers, body, self._timeout)
        except TransportTimeout as exc:
            raise OcrProviderError("provider_timeout") from exc
        except TransportError as exc:
            raise OcrProviderError("provider_transport_error") from exc


def _decode_json(response: HttpResponse) -> Mapping[str, Any]:
    """把响应体解析为 dict；失败抛 ``OcrProviderError``（不含原文）。"""
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OcrProviderError("invalid_response") from exc
    if not isinstance(data, Mapping):
        raise OcrProviderError("invalid_response")
    return data


def _error_code(data: Mapping[str, Any]) -> str | None:
    """提取业务错误码（顶层 ``error_code``），统一为字符串。"""
    return _clean_str(data.get("error_code"))


# --- 解析器 -------------------------------------------------------------------


class BaiduInvoiceOcrParser(ExternalParser):
    """``multiple_invoice`` 的 ``ExternalParser`` 实现（输出只进草稿）。

    构造时注入只读 ``source_reader``（协议见 ``SourceReader``），**不修改**
    ``ParseRequest``。凭据由 ``ExternalParser`` 从环境变量读取并 fail closed；构造
    成功即意味着 ``AJAIMGR_BAIDU_API_KEY`` / ``AJAIMGR_BAIDU_SECRET_KEY`` 均已配置。

    支持的输入类型：``image`` 与 ``document``（PDF/OFD）；其它类型由基类抛
    ``UnsupportedInputError``。所有预检/供应商失败统一返回
    ``error='provider_error'`` —— 上层只据 ``ParseResult.ok`` 决定回退人工录入。
    """

    credential_env_vars = (API_KEY_ENV, SECRET_KEY_ENV)

    def __init__(
        self,
        *,
        source_reader: SourceReader,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        env: Mapping[str, str] | None = None,
        endpoint: str = BAIDU_OCR_ENDPOINT,
        token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(env=env)
        self._source_reader = source_reader
        self._client = BaiduInvoiceOcrClient(
            api_key=self._credentials[API_KEY_ENV],
            secret_key=self._credentials[SECRET_KEY_ENV],
            transport=transport,
            clock=clock,
            endpoint=endpoint,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )

    @property
    def parser_name(self) -> str:
        return BAIDU_OCR_PARSER_NAME

    @property
    def parser_version(self) -> str:
        return BAIDU_OCR_PARSER_VERSION

    @property
    def parser_kind(self) -> ParserKind:
        return ParserKind.OCR

    @property
    def supported_input_kinds(self) -> frozenset[InputKind]:
        return frozenset({InputKind.IMAGE, InputKind.DOCUMENT})

    def _failed(self, request: ParseRequest) -> ParseResult:
        return ParseResult.failed(
            input_kind=request.input_kind,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            error=PROVIDER_ERROR,
            source_file_id=request.source_file_id,
        )

    def _parse(
        self, request: ParseRequest, credentials: Mapping[str, str]
    ) -> ParseResult:
        """读取原件 → 预检 → 识别 → 归一化（异常由基类收敛为 ``provider_error``）。

        ``credentials`` 参数由基类传入，但客户端已在构造时持有同一份凭据；本方法
        **不读取、不记录**其内容。
        """
        del credentials  # 凭据只在客户端私有属性中使用，此处不触碰。

        if request.source_file_id is None:
            _LOGGER.warning("baidu_invoice_ocr: missing source_file_id")
            return self._failed(request)

        page = request.metadata.get("page") if request.metadata else None
        document = self._source_reader.read(request.source_file_id)

        rejection = _precheck(document, page)
        if rejection is not None:
            # 只记录稳定原因码（不含文件名/内容/凭据）。
            _LOGGER.warning("baidu_invoice_ocr rejected input: %s", rejection)
            return self._failed(request)

        try:
            data = self._client.recognize(
                document.content, document.mime_type, page=page
            )
            fields, confidence = _normalize_response(data)
        except OcrProviderError as exc:
            _LOGGER.warning("baidu_invoice_ocr provider failure: %s", exc.reason)
            return self._failed(request)

        log_id = _clean_str(data.get("log_id"))
        if log_id is not None:
            # ``log_id`` 只用于定位问题，**不进** ParseResult.fields。
            _LOGGER.debug("baidu_invoice_ocr log_id=%s", log_id)

        return ParseResult(
            input_kind=request.input_kind,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            fields=fields,
            field_confidence=confidence,
            source_file_id=request.source_file_id,
        )
