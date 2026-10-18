"""百度 OCR **专用端点**适配器：``vat_invoice`` 与 ``online_taxi_itinerary``（Issue #52）。

实现依据（**必读**）：``docs/research/W2-37-ocr-endpoint-comparison.md``（两个专用
端点的真实响应结构、字段级准确率、弱点清单 D1–D5）、``docs/research/
W2-35-baidu-multiple-invoice-ocr.md``（``multiple_invoice`` 基线）、Issue #46 决策
记录（端点路由设为**可配置项**：``smart`` 默认 / ``routed`` 按类型）。

端点事实（逐条对齐 W2-37，**不按官方文档表臆造**）：

* ``POST .../rest/2.0/ocr/v1/vat_invoice`` 与 ``POST .../rest/2.0/ocr/v1/
  online_taxi_itinerary``：鉴权、``Content-Type``、base64→urlencode 与失败收敛与
  ``multiple_invoice`` 完全一致（复用 ``BaiduInvoiceOcrClient``）；
* 两个端点**都不传** ``probability``：``vat_invoice`` 已由真实探针确认会**静默忽略**
  该参数（W2-37 §7 D4①），``online_taxi_itinerary`` 文档未列该开关；
* 响应 ``words_result`` 是**对象**（单票/单份），字段为**标量字符串**；多值字段为
  ``[{"row": "...", "word": "..."}]`` 数组（``vat_invoice`` 的 ``Commodity*`` /
  ``Passenger*`` 等）；行程单的逐笔行程在**小写** ``items`` 数组里（W2-37 §7 D4②）。

字段口径：

| 归一化键 | ``vat_invoice`` | 说明 |
|---|---|---|
| ``amount`` | ``AmountInFiguers`` | **价税合计**（报销金额只取这个，绝不回退到不含税） |
| ``amount_excluding_tax`` | ``TotalAmount`` | 不含税合计；``multiple_invoice`` 无此键，专用端点**直接给出**（W2-37 §6） |
| ``tax_amount`` | ``TotalTax`` | 合计税额 |
| ``invoice_date`` | ``InvoiceDate`` | ``YYYY年M月D日`` → ISO（必须先归一化才能比对） |
| ``invoice_no`` / ``invoice_code`` | ``InvoiceNum`` / ``InvoiceCode`` | ``InvoiceCode`` 空值/空容器按**无值**处理（**绝不**产出 ``"[]"`` 之类脏值，与 W2-36 §7 D1 的修复同一口径） |
| ``buyer_name`` / ``buyer_tax_id`` | ``PurchaserName`` / ``PurchaserRegisterNum`` | |
| ``seller_name`` / ``seller_tax_id`` | ``SellerName`` / ``SellerRegisterNum`` | |
| ``passenger_*`` | ``PassengerName`` / ``PassengerIdNum`` / ``PassengerDate`` / ``PassengerDeparture`` / ``PassengerArrival`` / ``PassengerVehicleType`` / ``PassengerClass`` | 多行字段 → **字符串列表**（保持行序）；``PassengerIdNum`` 由供应商**已脱敏**，原样保留 |

| 归一化键 | ``online_taxi_itinerary`` | 说明 |
|---|---|---|
| ``invoice_date`` | ``ApplicationDate`` | 与 ``multiple_invoice`` 的 ``taxi_online_ticket`` 口径一致（``application_date`` → ``invoice_date``） |
| ``amount`` | ``TotalFare`` | 总金额（W2-37 §5：6/6 等于对应发票的价税合计） |
| ``seller_name`` | ``ServiceProvider`` | 服务商 |
| ``trips`` | ``items[]`` | 逐笔行程（**小写** ``items``）：``start_date`` / ``end_date`` 只取**日期**部分、``pickup_time``、``distance``（+ ``distance_needs_review``）、``fare`` |

两个端点都**没有字段级置信度**（W2-37 §7 D4①），因此逐字段置信度统一取
``DEFAULT_FIELD_CONFIDENCE``（``0.5``，低于默认阈值 ``0.75``）→ 专用端点产出的草稿
**必然**走人工复核。这是**约定回退值**，不是「识别质量差」：本批真实样本上
``vat_invoice`` 9 个可比字段 8/8 精确一致。失去「按置信度阈值分流」的能力是选型的
已知代价（W2-37 §9.2 第 1 项），替代策略（规则核对 / 跨源核对）属产品决策，**不在
本任务内**。

弱点处置（W2-37 §7 的 D1/D4/D5）：

1. **D1 里程误识**：``distance`` 做**数字 + 合理区间**校验（
   :data:`DISTANCE_MIN`–:data:`DISTANCE_MAX`，见下）；失败时**保留供应商原值**并在
   该笔行程上标 ``distance_needs_review=True``（绝不静默修正、绝不静默丢弃）。
   **已知局限**：区间校验抓不到「小数点丢失但数值仍在区间内」的误识（例如 ``3.5``
   被识成 ``35``）——这类错误只能靠跨源核对发现（W2-37 §7「偏差方向」），而跨源
   核对规则属产品决策，不在本任务内。
2. **D4②小写 ``items``**：按**真实响应**的小写键读取（文档参数表写 ``Items``，照
   文档实现会取不到行程）。
3. **D4④时间占位符**：``StartTime`` / ``EndTime`` 的时间部分是占位符
   （``YYYY-MM-DD --:--``），故 ``start_date`` / ``end_date`` **只取日期**，不把
   占位符当行程起止时刻。
4. **D5 明文手机号**：行程单响应顶层返回明文手机号（6/6 份），比票据端点更敏感。
   处置口径是**两层**（见 :func:`mask_sensitive_text` 与 :func:`is_sensitive_field_name`）：
   ①**丢弃**——本适配器是**白名单式映射**（只映射下面登记的别名键，**不做通配
   透传**），未登记的响应键（含手机号）一律不进草稿；命敏感键名模式的别名候选也
   一律不取值；②**脱敏**——进入草稿的字符串值再做一次手机号掩码，防止号码嵌在
   服务商名称等字段里漏出。两者都只影响草稿数据；本模块**绝不**把响应体、字段值
   或异常原文写进日志（只记稳定原因码与 ``log_id``，后者只进 debug）。

路由（``routed`` 模式）：见 :func:`decide_ocr_endpoint` 与 :class:`RoutedOcrParser`
——按调用方给的**文件特征**（``ParseRequest.metadata``）路由到专用端点，**无法判定
时回退 ``smart``**（``multiple_invoice``，当前生产路径，也是唯一提供字段级置信度的
端点）。两个专用端点接受同一批载体（图片/PDF/OFD），``InputKind`` 与 MIME 本身**不
足以**区分票据与行程单，故判别信号来自调用方元数据而非载体类型。

硬边界：本模块**只做全桩可测的适配与路由**，不调用真实外部服务、不加依赖、不做
迁移、不加 HTTP 端点；真实联调须所有者单独授权（DPA/PIPIA 仍未关闭，W2-37 §11）。
"""

from __future__ import annotations

import logging
import re
from abc import abstractmethod
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.integrations.baidu_invoice_ocr import (
    API_KEY_ENV,
    BAIDU_OCR_ENDPOINT,
    BAIDU_TOKEN_ENDPOINT,
    DEFAULT_FIELD_CONFIDENCE,
    DEFAULT_TIMEOUT_SECONDS,
    PROVIDER_ERROR,
    SECRET_KEY_ENV,
    BaiduInvoiceOcrClient,
    BaiduInvoiceOcrParser,
    OcrProviderError,
    SourceReader,
    Transport,
    clean_field_text,
    normalize_amount,
    normalize_invoice_date,
    precheck_source_document,
    resolve_credential_env,
)
from app.intake.parsing import (
    ExternalParser,
    InputKind,
    ParseRequest,
    ParseResult,
    ParserKind,
)

__all__ = [
    "BAIDU_TAXI_ITINERARY_ENDPOINT",
    "BAIDU_VAT_INVOICE_ENDPOINT",
    "DISTANCE_MAX",
    "DISTANCE_MIN",
    "ITINERARY_ITEMS_KEY",
    "ITINERARY_PARSER_NAME",
    "KNOWN_OCR_ROUTES",
    "OCR_FILENAME_METADATA_KEYS",
    "OCR_ROUTE_METADATA_KEY",
    "ROUTED_PARSER_NAME",
    "ROUTE_SMART",
    "ROUTE_TAXI_ITINERARY",
    "ROUTE_VAT_INVOICE",
    "VAT_INVOICE_PARSER_NAME",
    "BaiduTaxiItineraryParser",
    "BaiduVatInvoiceParser",
    "RoutedOcrParser",
    "decide_ocr_endpoint",
    "is_sensitive_field_name",
    "mask_sensitive_text",
    "normalize_distance",
]

_LOGGER = logging.getLogger(__name__)

# --- 端点与解析器标识 ---------------------------------------------------------

#: 增值税发票识别端点（W2-37 端点 A）。
BAIDU_VAT_INVOICE_ENDPOINT = "https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice"
#: 网约车行程单识别端点（W2-37 端点 B）。
BAIDU_TAXI_ITINERARY_ENDPOINT = (
    "https://aip.baidubce.com/rest/2.0/ocr/v1/online_taxi_itinerary"
)

VAT_INVOICE_PARSER_NAME = "baidu_cloud/vat_invoice"
ITINERARY_PARSER_NAME = "baidu_cloud/online_taxi_itinerary"
ROUTED_PARSER_NAME = "baidu_cloud/routed"
DEDICATED_PARSER_VERSION = "1"

#: 专用端点响应的行程数组键：真实响应为**小写** ``items``（W2-37 §7 D4②）。
ITINERARY_ITEMS_KEY = "items"

#: 归一化 ``invoice_type``：与 ``multiple_invoice`` 同一词汇（同一批票在两种模式下
#: 得到同一个取值）。专用端点响应**没有**票种分类字段（``vat_invoice`` 的
#: ``InvoiceType`` 只到「电子发票(普通发票)」，无法区分业务类别，W2-37 §7 D2），故取
#: **端点身份**而非供应商字段——这是稳定标识，不是臆造的业务取值。
VAT_INVOICE_TYPE = "vat_invoice"
ITINERARY_TYPE = "online_taxi_itinerary"

# --- 里程校验（W2-37 §7 D1） --------------------------------------------------

#: 单程里程的合理区间（保守 sanity bound）：一笔**付费**网约车行程的里程小于 0.1 或
#: 大于 500 都不合常理。区间外或非数字 → 标记待人工复核（见 :func:`normalize_distance`）。
#: 该区间是**本地保守默认**，不是供应商口径；供应商未记载里程单位（W2-37 §14），
#: 故归一化键保持单位中立（``distance``），调参属产品决策（本任务不引入配置项）。
DISTANCE_MIN = Decimal("0.1")
DISTANCE_MAX = Decimal("500")

_DISTANCE_NOISE_RE = re.compile(r"[,\s]")

# --- 敏感字段（W2-37 §7 D5） --------------------------------------------------

#: 敏感键名模式（大小写不敏感）：命中的响应键**一律不取值**（防御性；本适配器本就
#: 只做白名单映射，不会通配透传）。刻意**不含**证件号类模式——``PassengerIdNum``
#: 由供应商**已脱敏**，且是 Issue #52 明确要求保留的字段（W2-37 §4.2）。
SENSITIVE_KEY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"phone",
        r"mobile",
        r"tel(?:ephone)?",
        r"手机",
        r"联系方式",
        r"contact",
        r"e-?mail",
        r"邮箱",
        r"bank",
        r"卡号",
    )
)

#: 11 位中国大陆手机号（前后不再是数字，避免误伤更长的号码串/单号）。
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# --- 响应归一化：``vat_invoice`` -----------------------------------------------

_VAT_FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invoice_code", ("InvoiceCode",)),
    ("invoice_no", ("InvoiceNum",)),
    ("invoice_date", ("InvoiceDate",)),
    # 只取价税合计；绝不回退到不含税的 TotalAmount（W2-35 §3 的口径）。
    ("amount", ("AmountInFiguers",)),
    ("amount_excluding_tax", ("TotalAmount",)),
    ("tax_amount", ("TotalTax",)),
    ("buyer_name", ("PurchaserName",)),
    ("buyer_tax_id", ("PurchaserRegisterNum",)),
    ("seller_name", ("SellerName",)),
    ("seller_tax_id", ("SellerRegisterNum",)),
)

#: 多行字段（``[{"row": ..., "word": ...}]``）→ 字符串列表。
_VAT_ROW_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("passenger_name", ("PassengerName",)),
    ("passenger_id_number", ("PassengerIdNum",)),
    ("passenger_date", ("PassengerDate",)),
    ("passenger_departure", ("PassengerDeparture",)),
    ("passenger_arrival", ("PassengerArrival",)),
    ("passenger_vehicle_type", ("PassengerVehicleType",)),
    ("passenger_class", ("PassengerClass",)),
)

# --- 响应归一化：``online_taxi_itinerary`` ------------------------------------

_ITINERARY_FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invoice_date", ("ApplicationDate", "application_date")),
    ("amount", ("TotalFare", "total_fare")),
    ("seller_name", ("ServiceProvider", "service_provider")),
)

_ITINERARY_ITEM_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pickup_time", ("PickupTime", "pickup_time")),
    ("start_date", ("StartTime", "start_time")),
    ("end_date", ("EndTime", "end_time")),
    ("distance", ("Distance", "distance")),
    ("fare", ("Fare", "fare")),
)

#: 日期前缀（行程单 ``StartTime`` 形如 ``YYYY-MM-DD --:--``，只取日期部分）。
_DATE_PREFIX_RE = re.compile(
    r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})"
)

#: 字段值的归一化函数（与 ``multiple_invoice`` 适配器同一口径）。
_TRANSFORMS: Mapping[str, Callable[[str], str]] = {
    "amount": normalize_amount,
    "amount_excluding_tax": normalize_amount,
    "invoice_date": normalize_invoice_date,
}

# --- 敏感值处置 ---------------------------------------------------------------


def is_sensitive_field_name(name: Any) -> bool:
    """键名是否命中敏感模式（命中即**不取值**，绝不进草稿）。"""
    if not isinstance(name, str):
        return False
    return any(pattern.search(name) for pattern in SENSITIVE_KEY_PATTERNS)


def mask_sensitive_text(text: str) -> str:
    """把文本里的 11 位手机号掩码为 ``138****0000``（**保留字段、去掉明文**）。

    这是白名单映射之外的**第二层**防御：即使号码嵌在服务商名称、地址等字段里，
    也不会以明文进入草稿。掩码只改数字串本身，不改其它字符。
    """
    return _PHONE_RE.sub(lambda match: f"{match.group(0)[:3]}****{match.group(0)[-4:]}", text)


# --- 里程与日期 ---------------------------------------------------------------


def normalize_distance(raw: str) -> tuple[str, bool]:
    """里程 → ``(取值, 是否待人工复核)``（W2-37 §7 D1 的处置）。

    * 能解析为 ``DISTANCE_MIN``–``DISTANCE_MAX`` 之间的有限数 → 规范化为 2 位小数
      字符串，``needs_review=False``；
    * 非数字（如「数字→字母」误识）或越界 → **原样保留**供应商取值并
      ``needs_review=True``：错误值必须让人看到，绝不静默修正成看起来合理的数。

    **局限**：区间内的「小数点丢失」误识（``3.5`` → ``35``）无法由此发现（W2-37
    §7 已指出该误识不带任何可判别标志）。
    """
    cleaned = _DISTANCE_NOISE_RE.sub("", raw).replace("，", "")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return raw, True
    if not value.is_finite() or value < DISTANCE_MIN or value > DISTANCE_MAX:
        return raw, True
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), False


def _date_only(raw: str) -> str:
    """只取日期部分（``StartTime`` / ``EndTime`` 的时间部分是占位符，W2-37 §7 D4④）。"""
    match = _DATE_PREFIX_RE.match(raw)
    if match is not None:
        try:
            return date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ).isoformat()
        except ValueError:
            pass
    return normalize_invoice_date(raw.strip())


def _row_values(value: Any) -> list[str]:
    """多行字段值 → 非空字符串列表（保持行序；空容器/空白行按无值丢弃）。

    容忍三种形态：真实响应的 ``[{"row": ..., "word": ...}]``、标量字符串、以及
    单元素对象 ``{"word": ...}``。``word`` 缺失时回退 ``value``（与既有适配器一致）。
    """
    if isinstance(value, Mapping):
        text = clean_field_text(value.get("word")) or clean_field_text(
            value.get("value")
        )
        return [text] if text else []
    if isinstance(value, str):
        text = clean_field_text(value)
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        rows: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                text = clean_field_text(item.get("word")) or clean_field_text(
                    item.get("value")
                )
            else:
                text = clean_field_text(item)
            if text:
                rows.append(text)
        return rows
    return []


# --- 共享骨架 -----------------------------------------------------------------


class _FormEndpointParser(ExternalParser):
    """百度 OCR 表单端点适配器的共享骨架（读原件 → 预检 → 识别 → 归一化）。

    与 ``BaiduInvoiceOcrParser`` 同一流程与同一失败收敛口径（任何预检/供应商/形态
    失败 → ``error='provider_error'``，异常原文与响应体**不进结果、不进日志**），
    差别只在端点、是否传 ``probability`` 与 ``_normalize`` 的实现。
    """

    credential_env_vars = (API_KEY_ENV, SECRET_KEY_ENV)

    #: 是否随请求发送 ``probability=true``（专用端点**不发送**，见模块文档）。
    send_probability = False

    def __init__(
        self,
        *,
        source_reader: SourceReader,
        endpoint: str,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        env: Mapping[str, str] | None = None,
        token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(env=resolve_credential_env(env))
        self._source_reader = source_reader
        self._client = BaiduInvoiceOcrClient(
            api_key=self._credentials[API_KEY_ENV],
            secret_key=self._credentials[SECRET_KEY_ENV],
            transport=transport,
            clock=clock,
            endpoint=endpoint,
            probability=self.send_probability,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )

    @property
    def parser_version(self) -> str:
        return DEDICATED_PARSER_VERSION

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

    @abstractmethod
    def _normalize(
        self, data: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, float]]:
        """把端点响应归一化为 ``(fields, field_confidence)``（子类实现）。

        声明为抽象方法：子类漏实现时在**构造期**就失败，而不是被
        ``ExternalParser.parse`` 的异常收敛悄悄变成 ``provider_error``。
        """

    def _parse(
        self, request: ParseRequest, credentials: Mapping[str, str]
    ) -> ParseResult:
        del credentials  # 凭据只在客户端私有属性中使用，此处不触碰。

        if request.source_file_id is None:
            _LOGGER.warning("%s: missing source_file_id", self.parser_name)
            return self._failed(request)

        page = request.metadata.get("page") if request.metadata else None
        document = self._source_reader.read(request.source_file_id)

        rejection = precheck_source_document(document, page)
        if rejection is not None:
            # 只记录稳定原因码（不含文件名/内容/凭据）。
            _LOGGER.warning("%s rejected input: %s", self.parser_name, rejection)
            return self._failed(request)

        try:
            data = self._client.recognize(
                document.content, document.mime_type, page=page
            )
            fields, confidence = self._normalize(data)
        except OcrProviderError as exc:
            _LOGGER.warning("%s provider failure: %s", self.parser_name, exc.reason)
            return self._failed(request)

        log_id = clean_field_text(data.get("log_id"))
        if log_id is not None:
            # ``log_id`` 只用于定位问题，**不进** ParseResult.fields。
            _LOGGER.debug("%s log_id=%s", self.parser_name, log_id)

        return ParseResult(
            input_kind=request.input_kind,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            fields=fields,
            field_confidence=confidence,
            source_file_id=request.source_file_id,
        )

    def _confidence_for(self, fields: Mapping[str, Any]) -> dict[str, float]:
        """专用端点**无字段级置信度** → 逐字段统一回退 ``DEFAULT_FIELD_CONFIDENCE``。

        回退值 ``0.5`` 低于默认阈值 ``0.75``，因此专用端点产出的草稿**必然**走人工
        复核（见模块文档：这是约定回退值，不是识别质量结论）。
        """
        return {name: DEFAULT_FIELD_CONFIDENCE for name in fields}


def _scalar_field(
    result: Mapping[str, Any],
    candidates: tuple[str, ...],
    *,
    unified_key: str,
) -> str | None:
    """按别名取标量字段值（清洗 + 归一化）；无值/敏感键返回 ``None``。"""
    for candidate in candidates:
        if is_sensitive_field_name(candidate):
            _LOGGER.warning("baidu_dedicated_ocr: skipped sensitive field key")
            continue
        if candidate not in result:
            continue
        text = clean_field_text(result[candidate])
        if text is None:
            continue
        transform = _TRANSFORMS.get(unified_key)
        value = transform(text) if transform is not None else text
        if value:
            return value
    return None


class BaiduVatInvoiceParser(_FormEndpointParser):
    """``vat_invoice``（增值税发票识别）适配器（``ExternalParser`` 实现）。

    输出只进草稿（与既有适配器一致）；``invoice_code`` 空值/空容器按**无值**处理，
    绝不产出脏值。支持 ``image`` / ``document``（PDF/OFD）。
    """

    def __init__(
        self,
        *,
        source_reader: SourceReader,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        env: Mapping[str, str] | None = None,
        endpoint: str = BAIDU_VAT_INVOICE_ENDPOINT,
        token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            source_reader=source_reader,
            endpoint=endpoint,
            transport=transport,
            clock=clock,
            env=env,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )

    @property
    def parser_name(self) -> str:
        return VAT_INVOICE_PARSER_NAME

    def _normalize(
        self, data: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, float]]:
        result = data.get("words_result")
        if not isinstance(result, Mapping) or not result:
            raise OcrProviderError("empty_result")

        fields: dict[str, Any] = {"invoice_type": VAT_INVOICE_TYPE}
        for unified_key, candidates in _VAT_FIELD_ALIASES:
            value = _scalar_field(result, candidates, unified_key=unified_key)
            if value is not None:
                fields[unified_key] = value

        for unified_key, candidates in _VAT_ROW_ALIASES:
            for candidate in candidates:
                if is_sensitive_field_name(candidate):
                    _LOGGER.warning(
                        "baidu_dedicated_ocr: skipped sensitive field key"
                    )
                    continue
                if candidate not in result:
                    continue
                rows = _row_values(result[candidate])
                if not rows:
                    continue
                if unified_key == "passenger_date":
                    rows = [normalize_invoice_date(row) for row in rows]
                fields[unified_key] = rows
                break

        return fields, self._confidence_for(fields)


class BaiduTaxiItineraryParser(_FormEndpointParser):
    """``online_taxi_itinerary``（网约车行程单识别）适配器（``ExternalParser`` 实现）。

    * 逐笔行程读**小写** ``items``（W2-37 §7 D4②）；
    * ``start_date`` / ``end_date`` 只取日期（时间部分是占位符，§7 D4④）；
    * ``distance`` 做数字 + 区间校验，失败标 ``distance_needs_review``（§7 D1）；
    * 敏感字段**丢弃或脱敏**（§7 D5，见模块文档的两层口径）。
    """

    def __init__(
        self,
        *,
        source_reader: SourceReader,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        env: Mapping[str, str] | None = None,
        endpoint: str = BAIDU_TAXI_ITINERARY_ENDPOINT,
        token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            source_reader=source_reader,
            endpoint=endpoint,
            transport=transport,
            clock=clock,
            env=env,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )

    @property
    def parser_name(self) -> str:
        return ITINERARY_PARSER_NAME

    def _trips(self, result: Mapping[str, Any]) -> list[dict[str, Any]]:
        """读**小写** ``items`` 并归一化逐笔行程（无值/形态异常 → 空列表）。"""
        raw_items = result.get(ITINERARY_ITEMS_KEY)
        if not isinstance(raw_items, list):
            return []

        trips: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            trip: dict[str, Any] = {}
            for unified_key, candidates in _ITINERARY_ITEM_ALIASES:
                for candidate in candidates:
                    if is_sensitive_field_name(candidate):
                        _LOGGER.warning(
                            "baidu_dedicated_ocr: skipped sensitive field key"
                        )
                        continue
                    if candidate not in item:
                        continue
                    text = clean_field_text(item[candidate])
                    if text is None:
                        continue
                    if unified_key in ("start_date", "end_date"):
                        value = _date_only(text)
                    elif unified_key == "distance":
                        value, needs_review = normalize_distance(text)
                        trip["distance_needs_review"] = needs_review
                    elif unified_key == "fare":
                        value = normalize_amount(text)
                    else:
                        value = text
                    value = mask_sensitive_text(value)
                    if not value:
                        continue
                    trip[unified_key] = value
                    break
            if trip:
                trips.append(trip)
        return trips

    def _normalize(
        self, data: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, float]]:
        result = data.get("words_result")
        if not isinstance(result, Mapping) or not result:
            raise OcrProviderError("empty_result")

        if any(is_sensitive_field_name(name) for name in result):
            # W2-37 §7 D5：行程单响应含明文手机号等敏感键。白名单映射本就不会取值，
            # 这里只留一条**可观测**信号（绝不记录键名与取值）。
            _LOGGER.warning(
                "baidu_dedicated_ocr: sensitive field present in itinerary "
                "response, dropped"
            )

        fields: dict[str, Any] = {"invoice_type": ITINERARY_TYPE}
        for unified_key, candidates in _ITINERARY_FIELD_ALIASES:
            value = _scalar_field(result, candidates, unified_key=unified_key)
            if value is not None:
                fields[unified_key] = mask_sensitive_text(value)

        trips = self._trips(result)
        if trips:
            fields["trips"] = trips

        return fields, self._confidence_for(fields)


# --- 路由（``routed`` 模式） --------------------------------------------------

#: 显式路由提示的元数据键（取值 ∈ :data:`KNOWN_OCR_ROUTES`）。
OCR_ROUTE_METADATA_KEY = "ocr_route"

#: 可能携带文件名的元数据键（按顺序取第一个非空值）。
OCR_FILENAME_METADATA_KEYS: tuple[str, ...] = (
    "filename",
    "file_name",
    "original_name",
    "declared_name",
)

#: 路由目标：``smart``（统一智能识别）与两个专用端点。
ROUTE_SMART = "multiple_invoice"
ROUTE_VAT_INVOICE = "vat_invoice"
ROUTE_TAXI_ITINERARY = "online_taxi_itinerary"

#: 显式路由提示的合法取值。
KNOWN_OCR_ROUTES: frozenset[str] = frozenset(
    {ROUTE_SMART, ROUTE_VAT_INVOICE, ROUTE_TAXI_ITINERARY}
)

#: 文件特征关键词（小写比较；行程单先判，避免「行程单发票」这类混合名走错端点）。
_ITINERARY_HINTS: tuple[str, ...] = ("行程单", "itinerary")
_INVOICE_HINTS: tuple[str, ...] = ("发票", "invoice")


def decide_ocr_endpoint(request: ParseRequest) -> str | None:
    """按 ``ParseRequest.metadata`` 的文件特征决定专用端点；无法判定返回 ``None``。

    判别顺序：

    1. **显式路由提示** ``metadata["ocr_route"]`` ∈ :data:`KNOWN_OCR_ROUTES`（
       ``multiple_invoice`` 表示「走 smart」）；取值非法则记一条 warning 并继续，
       不静默当成合法提示；
    2. **文件名特征** ``metadata`` 的 ``filename`` / ``file_name`` / ``original_name``
       / ``declared_name``：含「行程单」/``itinerary`` → 行程单端点；含「发票」/
       ``invoice`` → 增值税发票端点；
    3. 其余 → ``None``（调用方回退 ``smart``）。

    **只看元数据、不读原件**：两个专用端点接受同一批载体（图片/PDF/OFD），MIME 与
    ``InputKind`` 都不足以区分票据与行程单；而 ``SourceDocument`` 没有文件名，读原件
    既无额外信息、又会白读一次 blob。因此路由信号由调用方在 ``ParseRequest.metadata``
    里给出（元数据约定见模块文档；目前产品代码尚无构造 ``ParseRequest`` 的调用方，
    该约定随受理服务层落地）。
    """
    metadata = request.metadata or {}

    explicit = metadata.get(OCR_ROUTE_METADATA_KEY)
    if isinstance(explicit, str) and explicit.strip():
        candidate = explicit.strip()
        if candidate in KNOWN_OCR_ROUTES:
            return None if candidate == ROUTE_SMART else candidate
        _LOGGER.warning(
            "baidu_dedicated_ocr: unknown ocr_route hint ignored, "
            "falling back to smart"
        )

    for key in OCR_FILENAME_METADATA_KEYS:
        name = metadata.get(key)
        if not isinstance(name, str) or not name.strip():
            continue
        lowered = name.strip().lower()
        if any(hint in lowered for hint in _ITINERARY_HINTS):
            return ROUTE_TAXI_ITINERARY
        if any(hint in lowered for hint in _INVOICE_HINTS):
            return ROUTE_VAT_INVOICE

    return None


class RoutedOcrParser(ExternalParser):
    """``routed`` 模式的复合解析器：按文件特征路由，无法判定回退 ``smart``。

    * 路由规则见 :func:`decide_ocr_endpoint`；**无法判定**（无提示、文件名无特征）时
      回退 ``multiple_invoice``——它是当前生产路径，也是唯一提供**字段级置信度**的
      端点（W2-37 §6），比让专用端点去猜更安全；
    * 委派后**返回子解析器的 ``ParseResult``**：草稿与审计里的 ``parser_name`` 因此是
      真正产出该值的端点（可追溯「哪一版解析器产出了该值」），而不是 ``routed`` 这个
      中间层；
    * 三个子解析器各自持有 OAuth 客户端（token 按实例缓存）——``routed`` 模式首次
      识别可能取多次 token（token 端点不按识别次数计费，W2-37 §8 亦单独记账）；
    * 构造即要求凭据齐全（fail closed）；缺凭据时装配层跳过注册并记录**非敏感**说明。
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
        vat_endpoint: str = BAIDU_VAT_INVOICE_ENDPOINT,
        itinerary_endpoint: str = BAIDU_TAXI_ITINERARY_ENDPOINT,
        token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(env=resolve_credential_env(env))
        # 变量名 → 取值；只用于构造子解析器，**不记录、不外传**。
        credentials = dict(self._credentials)

        self._vat = BaiduVatInvoiceParser(
            source_reader=source_reader,
            transport=transport,
            clock=clock,
            env=credentials,
            endpoint=vat_endpoint,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )
        self._itinerary = BaiduTaxiItineraryParser(
            source_reader=source_reader,
            transport=transport,
            clock=clock,
            env=credentials,
            endpoint=itinerary_endpoint,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )
        self._fallback = BaiduInvoiceOcrParser(
            source_reader=source_reader,
            transport=transport,
            clock=clock,
            env=credentials,
            endpoint=endpoint,
            token_endpoint=token_endpoint,
            timeout=timeout,
        )
        self._routes: Mapping[str, ExternalParser] = {
            ROUTE_VAT_INVOICE: self._vat,
            ROUTE_TAXI_ITINERARY: self._itinerary,
        }

    @property
    def parser_name(self) -> str:
        return ROUTED_PARSER_NAME

    @property
    def parser_version(self) -> str:
        return DEDICATED_PARSER_VERSION

    @property
    def parser_kind(self) -> ParserKind:
        return ParserKind.OCR

    @property
    def supported_input_kinds(self) -> frozenset[InputKind]:
        return frozenset({InputKind.IMAGE, InputKind.DOCUMENT})

    def _parse(
        self, request: ParseRequest, credentials: Mapping[str, str]
    ) -> ParseResult:
        del credentials  # 凭据只在子解析器的客户端私有属性中使用。

        route = decide_ocr_endpoint(request)
        parser = self._routes.get(route or "", self._fallback)
        # 只记路由目标标识（不含字段值/文件名）。
        _LOGGER.info(
            "baidu_dedicated_ocr: routed to %s (hint=%s)",
            parser.parser_name,
            route or "none",
        )
        return parser.parse(request)
