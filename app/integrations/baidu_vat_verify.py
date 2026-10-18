"""百度智能云「增值税发票验真」适配器：``vat_invoice_verification``。

实现依据（**必读**）：``docs/research/W1-28-baidu-invoice-verification.md``（W1-28）
与 ``docs/research/W0-4-invoice-verification.md``（W0-4）文末「所有者决策更新
（2026-10-16）」增补节。所有者 2026-10-16 决策：本渠道为 ``authorized_provider``
的**首选实现**；腾讯云 ``VatInvoiceVerifyNew``（``app.integrations.tencent_vat_verify``）
为**备用实现**；聚美智数仅文档留痕、不实现。**不新增任何渠道选择配置项或环境开关**：
接入与否由服务层/部署层决定，本模块只提供渠道适配器。

本模块的关键不变量：

* **认证走 OAuth2.0**：API Key / Secret Key → ``access_token``
  （``client_credentials`` 流程，token 默认有效期 30 天，W1-28 §2.1）；token 在内存中
  缓存，按 ``expires_in`` 提前 60 秒视为过期，过期或缺失时重新获取；全程**不自动
  重发**。
* **凭据只从环境变量读取**（``AJAIMGR_BAIDU_API_KEY`` /
  ``AJAIMGR_BAIDU_SECRET_KEY``）；缺失时**构造即失败**（fail closed），**绝不**降级
  为「查验通过」。错误信息只提环境变量名，**不含凭据值**。
* **请求构造**：``POST`` 表单 ``application/x-www-form-urlencoded``，``access_token``
  置于 URL 参数；六个文档化参数 ``invoice_code`` / ``invoice_num`` / ``invoice_date``
  （``YYYYMMDD``）/ ``invoice_type`` / ``check_code`` / ``total_amount``——按票种条件
  传递，未提供的条件字段以空串表达（文档原文允许「此参数可为空」），**不臆造取值**；
  ``invoice_type`` 为渠道必选且无「可为空」例外，缺失时本地 fail closed（不发起必然
  失败的请求）。金额一律定点格式（不出现科学计数法）。
* **响应解析**按 W1-28 §2.3 提取字段，**不臆造**未列出的字段（币种与「电子类型」
  代码字段文档未列 → 保持 ``None``）；``VerifyResult`` 驱动状态映射、
  ``InvalidSign`` 驱动作废/红冲否决；``VerifyMessage``/``VerifyFrequency``
  与其余票面字段原样保留在 ``evidence``（契约层无对应槽位）；``provider_ref`` 用
  ``log_id``；``evidence`` 放原始响应（脱敏由服务层 ``sanitize_evidence`` 负责）。
* **响应层级**（2026-10-18 真实调用修正，见 #57/#58）：W1-28 §2.3 的字段表把结论与
  身份字段列在 ``words_result`` 内，但**真实响应把结论/身份字段放在顶层**
  （``VerifyResult``/``InvalidSign``/``InvoiceCode``/``InvoiceNum``/``InvoiceDate``
  等），``words_result`` 只承载票面明细（购销方名称/税号、合计金额/税额/价税合计、
  货物明细等）。适配器对**结论/身份字段**采用「**顶层优先、``words_result`` 回退**」
  （``_layered``），对**票面明细**仍从 ``words_result`` 提取——两种结构都兼容。
* **结果码映射**按 W1-28 §2.4 的 14 个逐字码表（含「是否计费」列）：``0001`` 且
  ``InvalidSign=N`` → 查验通过候选（仍须票面一致性比对）；``0006`` 与
  ``InvalidSign ∈ {Y, H, BH, QH}`` → 查验不通过；其余（``0002``/``0005``/``0009``/
  ``1005``/``1006``/``1008``/``1009``/``1014``/``1015``/``1020``/``1021``/``9999``）
  → 无法查验/接口异常；未知码一律 fail closed 到 ``UNABLE_TO_VERIFY``。``0009``
  （发票不存在）归类为「无法查验」，与腾讯云映射（``ResourceNotFound.NoInvoice``
  → ``UNABLE_TO_VERIFY``）保持跨渠道一致，理由见 W1-28 §2.4。
* **超时不得盲目重发**：适配器自身只尝试一次；超时/传输失败一律映射为
  ``UNABLE_TO_VERIFY``（**绝不**映射为「查验通过」）。

网络出口通过可注入的 ``transport`` 完成：生产默认用 ``httpx``（**惰性导入**，仅在
真实发起请求时加载）；测试一律注入桩，**绝不调用真实百度接口**。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple
from urllib.parse import quote, urlencode

from app.expense.money import quantize, to_decimal
from app.integrations.invoice_verification import (
    InvoiceFace,
    InvoiceFields,
    InvoiceVerificationProvider,
    ProviderError,
    ProviderType,
    VerificationResult,
    VerificationStatus,
    parse_invoice_date,
)

__all__ = [
    "API_KEY_ENV",
    "BAIDU_ENDPOINT",
    "BAIDU_HOST",
    "BAIDU_PROVIDER_NAME",
    "BAIDU_TOKEN_ENDPOINT",
    "DEFAULT_TIMEOUT_SECONDS",
    "INVALID_SIGN_FAIL_VALUES",
    "INVALID_SIGN_NORMAL",
    "RESULT_CODE_TABLE",
    "SECRET_KEY_ENV",
    "TOKEN_EXPIRY_MARGIN_SECONDS",
    "BaiduVatInvoiceVerifyProvider",
    "HttpResponse",
    "MissingCredentialError",
    "ResultCodeSpec",
    "Transport",
    "TransportError",
    "TransportTimeout",
    "is_result_code_billed",
    "map_result_code",
]

# --- 常量（逐字对齐 W1-28 §2，抓取证据见 W1-28 §6） ---------------------------

BAIDU_HOST = "aip.baidubce.com"
BAIDU_ENDPOINT = (
    "https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice_verification"
)
BAIDU_TOKEN_ENDPOINT = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_PROVIDER_NAME = "baidu_cloud/vat_invoice_verification"

#: 凭据环境变量名（**只从环境变量读取**，不落仓库、不落日志）。
API_KEY_ENV = "AJAIMGR_BAIDU_API_KEY"
SECRET_KEY_ENV = "AJAIMGR_BAIDU_SECRET_KEY"

#: 默认请求超时（秒）。超时即 ``UNABLE_TO_VERIFY``，不盲目重发。
DEFAULT_TIMEOUT_SECONDS = 10.0

#: token 过期提前量（秒）：剩余寿命不足该值即视为过期、重新获取，避免边界失效。
TOKEN_EXPIRY_MARGIN_SECONDS = 60

#: ``InvalidSign``（发票状态，W1-28 §2.3）：正常值 / 各种「否决」值。
INVALID_SIGN_NORMAL = "N"  # 未作废
INVALID_SIGN_FAIL_VALUES: frozenset[str] = frozenset(
    {"Y", "H", "BH", "QH"}  # Y 已作废 / H 已冲红 / BH 部分红冲 / QH 全额红冲
)


# --- 结果码表（W1-28 §2.4 逐码对齐，含「是否计费」列） ------------------------


@dataclass(frozen=True)
class ResultCodeSpec:
    """单个 ``VerifyResult`` 结果码的文档化语义。

    * ``status``：本项目七状态映射；
    * ``billed``：文档「是否计费」列（True=是 / False=否）；
    * ``message``：文档「查验结果（VerifyMessage）」列原文。
    """

    status: VerificationStatus
    billed: bool
    message: str


#: ``VerifyResult`` → 状态映射 + 计费标记（W1-28 §2.4，共 14 个码）。
#:
#: 归类说明：
#:
#: * ``0001`` → 查验通过**候选**（仍须票面一致性与 ``InvalidSign`` 校验）；
#: * ``0006`` → **查验不通过**（渠道判定票面信息不一致）；
#: * ``0009`` → **无法查验**：与腾讯云 ``ResourceNotFound.NoInvoice`` →
#:   ``UNABLE_TO_VERIFY`` 的既有决策跨渠道一致——「票不存在」既可能是录入错误/票
#:   （正当票据）尚未同步税局，也不足以在**本项目**口径下单独裁定「假票」；
#:   保持一致可避免「同一业务事实因渠道不同而得到不同状态」；
#: * 其余全部 → **无法查验/接口异常**（技术性/环境性失败，可重试或转人工）；
#: * 未知码 fail closed 到 ``UNABLE_TO_VERIFY``（见 ``map_result_code``）。
RESULT_CODE_TABLE: Mapping[str, ResultCodeSpec] = {
    "0001": ResultCodeSpec(
        VerificationStatus.VERIFIED_PASS, True, "查验成功发票一致"
    ),
    "0002": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "超过该张票当天查验次数"
    ),
    "0005": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "请求不合法"
    ),
    "0006": ResultCodeSpec(
        VerificationStatus.VERIFIED_FAIL, True, "发票信息不一致"
    ),
    "0009": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, True, "发票不存在"
    ),
    "1005": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, True, "查询发票不规范"
    ),
    "1006": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "查验异常"
    ),
    "1008": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "字段不能为空"
    ),
    "1009": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "参数长度不正确"
    ),
    "1014": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "日期当天的不能查验"
    ),
    "1015": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "超过5年的不能查验"
    ),
    "1020": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "没有查验权限"
    ),
    "1021": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "网络超时"
    ),
    "9999": ResultCodeSpec(
        VerificationStatus.UNABLE_TO_VERIFY, False, "查验失败"
    ),
}


def map_result_code(code: str | None) -> VerificationStatus:
    """把渠道结果码映射为状态；未知/缺失一律 ``UNABLE_TO_VERIFY``（fail closed）。"""
    if not code:
        return VerificationStatus.UNABLE_TO_VERIFY
    spec = RESULT_CODE_TABLE.get(code)
    if spec is None:
        return VerificationStatus.UNABLE_TO_VERIFY
    return spec.status


def is_result_code_billed(code: str | None) -> bool:
    """该结果码是否计费（文档「是否计费」列）；未知码返回 ``False``（不臆断）。"""
    if not code:
        return False
    spec = RESULT_CODE_TABLE.get(code)
    if spec is None:
        return False
    return spec.billed


# --- 异常与传输抽象 -----------------------------------------------------------


class MissingCredentialError(ProviderError):
    """缺少百度智能云凭据（环境变量未设置）。

    **fail closed**：构造即失败，绝不降级为「查验通过」。错误信息只提环境变量名，
    **不含凭据值**。
    """


class TransportError(ProviderError):
    """底层网络传输失败（非超时）。"""


class TransportTimeout(TransportError):
    """底层网络传输超时（**不得盲目重发**）。"""


class HttpResponse(NamedTuple):
    """传输层返回：HTTP 状态码 + 原始响应体字节。"""

    status_code: int
    body: bytes


#: 传输函数签名：``(url, headers, body, timeout) -> HttpResponse``。
Transport = Callable[[str, Mapping[str, str], bytes, float], HttpResponse]


def _httpx_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> HttpResponse:
    """默认传输实现（``httpx``，惰性导入以免构造 provider 就加载网络库）。

    超时转为 ``TransportTimeout``、其它 HTTP 错误转为 ``TransportError``，二者都由
    ``verify`` 映射为 ``UNABLE_TO_VERIFY``；本函数**不重试**。
    """
    import httpx  # 局部导入：仅在实际发起请求时加载。

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=dict(headers), content=body)
    except httpx.TimeoutException as exc:  # 超时：不得盲目重发。
        raise TransportTimeout("provider request timed out") from exc
    except httpx.HTTPError as exc:
        raise TransportError("provider transport error") from exc
    return HttpResponse(response.status_code, response.content)


# --- 响应解析 -----------------------------------------------------------------


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _layered(top: Mapping[str, Any], words: Mapping[str, Any], key: str) -> Any:
    """按**真实响应层级**取字段值：顶层优先、``words_result`` 回退。

    2026-10-18 的真实调用（#57）证明：渠道把**结论/身份字段**
    （``VerifyResult``/``InvalidSign``/``InvoiceCode``/``InvoiceNum``/
    ``InvoiceDate``/``InvoiceType``/``CheckCode``/``MachineCode``/
    ``VerifyFrequency``/``VerifyMessage``）放在响应**顶层**，``words_result`` 只放
    票面明细；而 W1-28 §2.3 的字段表与既有桩测试把结论字段嵌在 ``words_result``
    内。为兼容两种结构而两处都取：**顶层优先**，顶层缺失（含空值）时回退
    ``words_result``——与 ``log_id`` 的既有取法（``data.get(...) or words.get(...)``）
    同一约定，**不臆造**未出现的取值。
    """
    return top.get(key) or words.get(key)


def _parse_decimal(value: Any) -> Decimal | None:
    """把渠道金额字符串解析为 2 位小数 ``Decimal``；无法解析返回 ``None``。"""
    if value is None or value == "":
        return None
    try:
        return quantize(to_decimal(str(value)))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _parse_face(top: Mapping[str, Any], words: Mapping[str, Any]) -> InvoiceFace:
    """提取票面要素（不臆造未列字段）。

    取值层级（#57 真实结构）：**结论/身份字段**（``InvalidSign``/``InvoiceCode``/
    ``InvoiceNum``/``InvoiceDate``）**顶层优先、``words_result`` 回退**；**票面明细**
    （``SellerRegisterNum``/``PurchaserRegisterNum``/``TotalAmount``/``TotalTax``/
    ``AmountInFiguers``）按真实结构只在 ``words_result``，故仍从 ``words_result``
    提取。

    映射（逐项对齐文档「返回参数」表）：

    * ``InvoiceCode``/``InvoiceNum``/``InvoiceDate`` → 发票代码/号码/开票日期；
    * ``SellerRegisterNum``/``PurchaserRegisterNum`` → 销方/购方纳税人识别号；
    * ``TotalAmount``（合计金额）→ ``amount_without_tax``；
    * ``TotalTax``（合计税额）→ ``tax_amount``；
    * ``AmountInFiguers``（价税合计-小写）→ ``amount_with_tax``；
    * ``InvalidSign`` → ``is_abandoned``（``Y``）/ ``is_red_letter``（``H``/``BH``/
      ``QH``）；
    * ``currency``/``electronic_type``：文档**未列出**对应字段 → 保持 ``None``；
    * 货物明细（``CommodityName`` 等各「列数组」）不做行合并（不臆造结构），
      原始 ``words_result`` 全量保留于 ``evidence``。

    机动车/二手车/铁路/航空/通行费分节的金额字段（``Price``/``Tax``/``PriceTaxLow``/
    ``TotalCarPrice``/``total_tax`` 等）语义按票种分节、与通用槽位无一一对应，
    **不并入**（避免臆造），保留在 ``evidence`` 原文。
    """
    invalid_sign = _optional_str(_layered(top, words, "InvalidSign"))
    return InvoiceFace(
        invoice_code=_optional_str(_layered(top, words, "InvoiceCode")),
        invoice_no=_optional_str(_layered(top, words, "InvoiceNum")),
        invoice_date=parse_invoice_date(
            _optional_str(_layered(top, words, "InvoiceDate"))
        ),
        seller_tax_code=_optional_str(words.get("SellerRegisterNum")),
        buyer_tax_code=_optional_str(words.get("PurchaserRegisterNum")),
        amount_without_tax=_parse_decimal(words.get("TotalAmount")),
        tax_amount=_parse_decimal(words.get("TotalTax")),
        amount_with_tax=_parse_decimal(words.get("AmountInFiguers")),
        # 文档未列币种字段（默认人民币需按真实响应确认），不臆造。
        currency=None,
        # 文档未列与腾讯云 ``ElectronicType`` 对应的字段，不臆造；
        # ``InvoiceType``（发票种类名称）原样保留在 evidence。
        electronic_type=None,
        is_abandoned=invalid_sign == "Y",
        is_red_letter=invalid_sign in {"H", "BH", "QH"},
        items=(),
    )


def _face_mismatch(fields: InvoiceFields, face: InvoiceFace) -> str | None:
    """比较渠道票面与提交要素，返回不一致字段（逗号分隔）或 ``None``。

    与腾讯云适配器同思路（W1-28 §2.2）：只比较双方都提供了值的字段；金额按票种
    语义（不含税 vs 价税合计）与 ``amount_without_tax`` 或 ``amount_with_tax``
    任一相等即视为一致。
    """
    mismatches: list[str] = []
    if face.invoice_no is not None and face.invoice_no != fields.invoice_no:
        mismatches.append("invoice_no")
    if (
        face.invoice_code is not None
        and fields.invoice_code is not None
        and face.invoice_code != fields.invoice_code
    ):
        mismatches.append("invoice_code")
    if (
        face.invoice_date is not None
        and fields.invoice_date is not None
        and face.invoice_date != fields.invoice_date
    ):
        mismatches.append("invoice_date")
    if fields.amount is not None:
        candidates = [
            amount
            for amount in (face.amount_without_tax, face.amount_with_tax)
            if amount is not None
        ]
        if candidates and all(candidate != fields.amount for candidate in candidates):
            mismatches.append("amount")
    return ",".join(mismatches) or None


# --- 适配器 -------------------------------------------------------------------


class BaiduVatInvoiceVerifyProvider(InvoiceVerificationProvider):
    """百度智能云「增值税发票验真」适配器（``authorized_provider``，首选实现）。

    构造参数：

    * ``api_key`` / ``secret_key``：**默认从环境变量读取**
      （``AJAIMGR_BAIDU_API_KEY`` / ``AJAIMGR_BAIDU_SECRET_KEY``）；显式传入仅用于
      测试，**任何情况下都不得写进仓库或日志**。缺失即抛 ``MissingCredentialError``
      （fail closed）；
    * ``transport``：HTTP 传输函数（默认 ``httpx``）；测试注入桩，**绝不发真实外网
      请求**；
    * ``clock``：返回当前 UTC ``datetime`` 的可调用对象（默认 ``datetime.now(UTC)``），
      便于测试注入固定时间与验证 token 过期重取；
    * ``endpoint`` / ``token_endpoint`` / ``timeout``：接口地址与单次请求超时（秒）。

    token 获取失败、传输失败与超时一律映射为 ``UNABLE_TO_VERIFY`` 并**只尝试一次**
    （不自动重发）；是否重试由调用方按幂等键决定（PLAN §6.3 第 3 条）。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        endpoint: str = BAIDU_ENDPOINT,
        token_endpoint: str = BAIDU_TOKEN_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        resolved_secret = (
            secret_key if secret_key is not None else os.environ.get(SECRET_KEY_ENV)
        )
        if not resolved_key or not resolved_key.strip():
            raise MissingCredentialError(
                "missing Baidu Cloud credential: set "
                f"{API_KEY_ENV} and {SECRET_KEY_ENV} in the environment"
            )
        if not resolved_secret or not resolved_secret.strip():
            raise MissingCredentialError(
                f"missing Baidu Cloud credential: set {SECRET_KEY_ENV} in the environment"
            )

        self._api_key = resolved_key.strip()
        self._secret_key = resolved_secret.strip()
        self._transport: Transport = (
            transport if transport is not None else _httpx_transport
        )
        self._clock: Callable[[], datetime] = (
            clock if clock is not None else (lambda: datetime.now(UTC))
        )
        self._endpoint = endpoint
        self._token_endpoint = token_endpoint
        self._timeout = timeout
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.AUTHORIZED_PROVIDER

    @property
    def provider_name(self) -> str:
        return BAIDU_PROVIDER_NAME

    # --- 请求构造 -------------------------------------------------------------

    @staticmethod
    def build_params(fields: InvoiceFields) -> dict[str, str]:
        """按 W1-28 §2.2 构造六个文档化请求参数。

        * ``invoice_num``/``invoice_date``（``YYYYMMDD``）必传；
        * ``invoice_type`` 取 ``fields.invoice_kind``（渠道必选、无「可为空」例外；
          缺失时由 ``verify`` 本地 fail closed，本函数不臆造）；
        * ``invoice_code``/``check_code``/``total_amount`` 按票种条件传：调用方未
          提供时以**空串**表达（文档原文「此参数可为空」），**不编造取值、不多传**
          任何未文档化参数（如腾讯云的 ``RegionCode``/``SellerTaxCode`` 一律不传）。
        金额经 ``app.expense.money`` 定点格式化（如 ``100.00``）。
        """
        params: dict[str, str] = {
            "invoice_num": fields.invoice_no,
            "invoice_date": fields.invoice_date.strftime("%Y%m%d"),
        }
        if fields.invoice_kind:
            params["invoice_type"] = fields.invoice_kind
        params["invoice_code"] = fields.invoice_code or ""
        params["check_code"] = fields.check_code or ""
        params["total_amount"] = (
            format(fields.amount, "f") if fields.amount is not None else ""
        )
        return params

    # --- token（OAuth2.0） -----------------------------------------------------

    def _ensure_access_token(self) -> str | VerificationResult:
        """返回可用的 ``access_token``；获取失败返回 ``UNABLE_TO_VERIFY`` 结果。"""
        now = self._clock()
        if (
            self._access_token is not None
            and self._token_expires_at is not None
            and now < self._token_expires_at - timedelta(
                seconds=TOKEN_EXPIRY_MARGIN_SECONDS
            )
        ):
            return self._access_token
        return self._request_access_token()

    def _request_access_token(self) -> str | VerificationResult:
        """请求一次 ``access_token``；失败映射为 ``UNABLE_TO_VERIFY``（不重发）。

        请求形态按「Access Token获取」文档：``POST`` 到
        ``https://aip.baidubce.com/oauth/2.0/token``，``grant_type`` /
        ``client_id`` / ``client_secret`` 以 URL 参数传递（文档示例即如此），
        空请求体；``Content-Type``/``Accept`` 按文档 Python 示例。

        ``client_secret`` 只出现在请求 URL 中，**绝不**出现在错误信息、``reason``
        或 ``evidence`` 里（响应体本身不含凭据）。
        """
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
        except TransportTimeout:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="TokenTimeout",
                reason="provider_token_timeout",
            )
        except TransportError:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="TokenTransportError",
                reason="provider_token_transport_error",
            )

        try:
            data = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="TokenInvalidResponse",
                reason="provider_token_invalid_response",
            )
        if not isinstance(data, dict):
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="TokenInvalidResponse",
                reason="provider_token_invalid_response",
            )

        error = _optional_str(data.get("error"))
        if response.status_code >= 400 or error is not None:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                evidence=data,
                error_code=error or f"HTTP{response.status_code}",
                reason="provider_token_error",
            )

        token = _optional_str(data.get("access_token"))
        if token is None:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                evidence=data,
                error_code="TokenInvalidResponse",
                reason="provider_token_invalid_response",
            )

        # 缓存 token：``expires_in`` 秒（文档 30 天）；缺失/不可解析时**不缓存**
        # （有效期未知 → 每次重新获取，fail safe，绝不冒用可能过期的 token）。
        self._access_token = token
        self._token_expires_at = self._expiration_from(data)
        return token

    def _expiration_from(self, data: Mapping[str, Any]) -> datetime | None:
        raw = data.get("expires_in")
        try:
            expires_in = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if expires_in <= 0:
            return None
        return self._clock() + timedelta(seconds=expires_in)

    # --- 查验 -----------------------------------------------------------------

    def verify(self, invoice_fields: InvoiceFields) -> VerificationResult:
        """执行一次查验；**不自动重发**，超时/传输失败一律 ``UNABLE_TO_VERIFY``。

        ``invoice_type`` 是渠道六个参数中唯一**必选且无「可为空」例外**者；缺失时
        在本地 fail closed，不获取 token、不发起必然失败的请求（不臆造票种）。
        """
        if not invoice_fields.invoice_kind:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code=None,
                reason="missing_invoice_type",
            )

        token = self._ensure_access_token()
        if isinstance(token, VerificationResult):
            return token

        params = self.build_params(invoice_fields)
        payload = urlencode(params).encode("utf-8")
        url = f"{self._endpoint}?access_token={quote(token, safe='')}"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        response = self._send(url, headers, payload)
        if isinstance(response, VerificationResult):
            return response  # 传输失败已在 _send 内映射为 UNABLE。
        return self._parse(response, invoice_fields)

    def _send(
        self, url: str, headers: Mapping[str, str], payload: bytes
    ) -> HttpResponse | VerificationResult:
        """发起**一次**请求；超时/传输失败映射为 ``UNABLE_TO_VERIFY``（不重发）。

        本接口是只读查询，但 PLAN §6.3 第 3 条禁止「超时后盲目重发可能产生副作用的
        请求」；这里统一**只尝试一次**，是否重试交由调用方按幂等键决定。
        """
        try:
            return self._transport(url, headers, payload, self._timeout)
        except TransportTimeout:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="Timeout",
                reason="provider_timeout",
            )
        except TransportError:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="TransportError",
                reason="provider_transport_error",
            )

    def _parse(
        self, response: HttpResponse, fields: InvoiceFields
    ) -> VerificationResult:
        """解析渠道响应：结果码映射 / 票面提取 / 票面一致性判定。

        结论/身份字段（``VerifyResult``/``InvalidSign`` 及票面身份字段）按 #57 真实
        结构「**顶层优先、``words_result`` 回退**」读取（``_layered``）；票面明细仍
        从 ``words_result`` 提取（见 ``_parse_face``）。
        """
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="InvalidResponse",
                reason="provider_invalid_response",
            )
        if not isinstance(data, dict):
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                error_code="InvalidResponse",
                reason="provider_invalid_response",
            )

        words = data.get("words_result")
        words = words if isinstance(words, dict) else {}
        # 文档示例中 ``log_id`` 出现在顶层，个别示例嵌套在 ``words_result`` 内；
        # 两处都取（顶层优先），用于 provider_ref 定位问题。
        log_id = _optional_str(_layered(data, words, "log_id"))

        if response.status_code >= 400:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                provider_ref=log_id,
                evidence=data,
                error_code=f"HTTP{response.status_code}",
                reason="provider_http_error",
            )

        code = _optional_str(_layered(data, words, "VerifyResult"))
        if code is None:
            # 失败返回示例可能只有顶层 ``error_code``（文档未给出该代码的映射表，
            # 属「待确认」，一律 fail closed）。
            raw_error = _optional_str(data.get("error_code"))
            if raw_error is not None:
                return VerificationResult(
                    status=VerificationStatus.UNABLE_TO_VERIFY,
                    provider_ref=log_id,
                    evidence=data,
                    error_code=raw_error,
                    reason="provider_error",
                )
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                provider_ref=log_id,
                evidence=data,
                error_code="InvalidResponse",
                reason="provider_invalid_response",
            )

        status = map_result_code(code)
        invalid_sign = _optional_str(_layered(data, words, "InvalidSign"))
        face = _parse_face(data, words)

        if status is VerificationStatus.VERIFIED_PASS:
            # ``0001`` 仅是「查验通过**候选**」：仍须 InvalidSign 非否决态，
            # 且与提交要素票面一致。以下否决均源于**票面字段**（渠道判定成功、
            # 无渠道错误码），故与腾讯云适配器同约定：``error_code=None``，
            # 具体缘由放 ``reason``。
            if invalid_sign in INVALID_SIGN_FAIL_VALUES:
                return VerificationResult(
                    status=VerificationStatus.VERIFIED_FAIL,
                    face=face,
                    provider_ref=log_id,
                    evidence=data,
                    error_code=None,
                    reason=f"invalid_sign:{invalid_sign}",
                )
            if invalid_sign != INVALID_SIGN_NORMAL:
                # ``InvalidSign`` 缺失或出现未文档化取值 → 无法确认发票状态，
                # fail closed（不臆断为正常）。
                return VerificationResult(
                    status=VerificationStatus.UNABLE_TO_VERIFY,
                    face=face,
                    provider_ref=log_id,
                    evidence=data,
                    error_code=None,
                    reason="invalid_sign_unknown",
                )
            mismatch = _face_mismatch(fields, face)
            if mismatch is not None:
                return VerificationResult(
                    status=VerificationStatus.VERIFIED_FAIL,
                    face=face,
                    provider_ref=log_id,
                    evidence=data,
                    error_code=None,
                    reason=f"face_mismatch:{mismatch}",
                )
            return VerificationResult(
                status=VerificationStatus.VERIFIED_PASS,
                face=face,
                provider_ref=log_id,
                evidence=data,
                error_code=None,
                reason=None,
            )

        if status is VerificationStatus.VERIFIED_FAIL:
            # ``0006`` 发票信息不一致（渠道已判定票面不符）。
            return VerificationResult(
                status=VerificationStatus.VERIFIED_FAIL,
                face=face,
                provider_ref=log_id,
                evidence=data,
                error_code=code,
                reason="invoice_info_mismatch",
            )

        # 其余结果码（0002/0005/0009/1005/1006/1008/1009/1014/1015/1020/1021/9999
        # 及未知码 fail closed）→ 无法查验/接口异常。
        return VerificationResult(
            status=status,
            face=face,
            provider_ref=log_id,
            evidence=data,
            error_code=code,
            reason=f"verify_result:{code}",
        )
