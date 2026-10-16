"""腾讯云「增值税发票核验（新版）」适配器：``VatInvoiceVerifyNew``。

实现依据（**必读**）：``docs/research/W0-4-invoice-verification.md``（W0-4）
§2.1–§2.5 与 §8。该渠道是 W0-4 §5 推荐的 ``authorized_provider`` **首选实现**。

本模块的关键不变量：

* **只用标准库做签名**：TC3-HMAC-SHA256 完全用 ``hmac``/``hashlib`` 实现，
  **不引入任何新依赖**（本任务硬约束）。签名方法与腾讯云签名方法 v3 一致：
  ``CanonicalRequest`` → ``StringToSign`` → 逐级派生密钥 → ``Authorization``。
* **请求构造**：``Action=VatInvoiceVerifyNew`` / ``Version=2018-11-19`` /
  域名 ``ocr.tencentcloudapi.com``；入参按票种条件传（``InvoiceNo``、
  ``InvoiceDate`` 必填，``InvoiceCode``/``CheckCode``/``Amount``/``RegionCode``/
  ``SellerTaxCode``/``InvoiceKind`` 仅在调用方提供时传，绝不臆造）。
* **凭据只从环境变量读取**（``AJAIMGR_TENCENT_SECRET_ID`` /
  ``AJAIMGR_TENCENT_SECRET_KEY``）；缺失时**构造即失败**（fail closed），
  **绝不**降级为「查验通过」。错误信息只提环境变量名，**不含凭据值**。
* **超时不得盲目重发**：本接口是只读查询，但超时后仍由调用方决定是否重试；
  适配器自身**不自动重发**（``max_attempts`` 默认 1），并把超时映射为
  ``UNABLE_TO_VERIFY``（无法查验/接口异常），**绝不**映射为「查验通过」。
* **响应解析**按 W0-4 §2.3 提取字段，**不臆造**未列出的字段（税额/币种保持
  ``None``，见 W0-4 §8 的诚实标注）；错误码映射按 W0-4 §2.4 的 11 个错误码与
  §8 的状态映射表，未知错误码一律 fail closed 到 ``UNABLE_TO_VERIFY``。

网络出口通过可注入的 ``transport`` 完成：生产默认用 ``httpx``；测试一律注入桩，
**绝不调用真实腾讯云接口**（本任务第五步）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NamedTuple

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
from app.expense.money import quantize, to_decimal

__all__ = [
    "ERROR_CODE_STATUS",
    "SECRET_ID_ENV",
    "SECRET_KEY_ENV",
    "TENCENT_ACTION",
    "TENCENT_ALGORITHM",
    "TENCENT_ENDPOINT",
    "TENCENT_HOST",
    "TENCENT_SERVICE",
    "TENCENT_VERSION",
    "HttpResponse",
    "MissingCredentialError",
    "Tc3Signature",
    "TencentVatInvoiceVerifyProvider",
    "TransportError",
    "TransportTimeout",
    "map_error_code",
    "sign_request",
]

# --- 常量（逐字对齐 W0-4 §2.1） ----------------------------------------------

TENCENT_SERVICE = "ocr"
TENCENT_HOST = "ocr.tencentcloudapi.com"
TENCENT_ENDPOINT = f"https://{TENCENT_HOST}"
TENCENT_ACTION = "VatInvoiceVerifyNew"
TENCENT_VERSION = "2018-11-19"
TENCENT_ALGORITHM = "TC3-HMAC-SHA256"
TENCENT_CONTENT_TYPE = "application/json; charset=utf-8"
TENCENT_PROVIDER_NAME = "tencent_cloud/vat_invoice_verify_new"

#: 凭据环境变量名（**只从环境变量读取**，不落仓库、不落日志）。
SECRET_ID_ENV = "AJAIMGR_TENCENT_SECRET_ID"
SECRET_KEY_ENV = "AJAIMGR_TENCENT_SECRET_KEY"

#: 默认请求超时（秒）。超时即 ``UNABLE_TO_VERIFY``，不盲目重发。
DEFAULT_TIMEOUT_SECONDS = 10.0

#: 错误码 → 状态映射（W0-4 §2.4 的 11 个错误码 + §8 的状态映射表）。
#:
#: * ``FailedOperation.InvoiceMismatch`` → **查验不通过**（票面不一致）；
#: * 其余（欠费、资源包耗尽、限频、税局网络异常、当日票不支持、无此票/地区码、
#:   服务未开通、未知错误）→ **无法查验/接口异常**（可重试/可降级的技术性失败）。
#:
#: 未列出的错误码**一律** ``UNABLE_TO_VERIFY``（fail closed，绝不「查验通过」）。
ERROR_CODE_STATUS: Mapping[str, VerificationStatus] = {
    "FailedOperation.InvoiceMismatch": VerificationStatus.VERIFIED_FAIL,
    "FailedOperation.ArrearsError": VerificationStatus.UNABLE_TO_VERIFY,
    "FailedOperation.CountLimitError": VerificationStatus.UNABLE_TO_VERIFY,
    "FailedOperation.UnKnowError": VerificationStatus.UNABLE_TO_VERIFY,
    # 文档写作 ``UnKnowError``；保留 ``UnknownError`` 变体以防上游改拼写。
    "FailedOperation.UnknownError": VerificationStatus.UNABLE_TO_VERIFY,
    "FailedOperation.UnOpenError": VerificationStatus.UNABLE_TO_VERIFY,
    "ResourceNotFound.NoAreaCode": VerificationStatus.UNABLE_TO_VERIFY,
    "ResourceNotFound.NoInvoice": VerificationStatus.UNABLE_TO_VERIFY,
    "ResourceNotFound.NotSupportCurrentInvoiceQuery": (
        VerificationStatus.UNABLE_TO_VERIFY
    ),
    "ResourceUnavailable.InArrears": VerificationStatus.UNABLE_TO_VERIFY,
    "ResourceUnavailable.ResourcePackageRunOut": VerificationStatus.UNABLE_TO_VERIFY,
    "ResourceUnavailable.TaxNetworkError": VerificationStatus.UNABLE_TO_VERIFY,
}


def map_error_code(code: str | None) -> VerificationStatus:
    """把渠道错误码映射为状态；未知/缺失一律 ``UNABLE_TO_VERIFY``（fail closed）。"""
    if not code:
        return VerificationStatus.UNABLE_TO_VERIFY
    return ERROR_CODE_STATUS.get(code, VerificationStatus.UNABLE_TO_VERIFY)


# --- 异常与传输抽象 -----------------------------------------------------------


class MissingCredentialError(ProviderError):
    """缺少腾讯云凭据（环境变量未设置）。

    **fail closed**：构造即失败，绝不降级为「查验通过」。错误信息只提环境变量名，
    **不含凭据值**（本任务第四步第 4 条）。
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


# --- TC3-HMAC-SHA256 签名（仅标准库） -----------------------------------------


@dataclass(frozen=True)
class Tc3Signature:
    """一次 TC3 签名的全部中间产物（便于测试断言与定位问题）。"""

    authorization: str
    canonical_request: str
    string_to_sign: str
    signature: str
    signed_headers: str
    credential_scope: str
    timestamp: int
    date: str


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """返回 ``(canonical_headers, signed_headers)``（键与值均小写并去空白）。"""
    items = sorted((k.lower().strip(), str(v).strip().lower()) for k, v in headers.items())
    canonical = "".join(f"{key}:{value}\n" for key, value in items)
    signed = ";".join(key for key, _ in items)
    return canonical, signed


def sign_request(
    *,
    secret_id: str,
    secret_key: str,
    host: str,
    service: str,
    action: str,
    payload: bytes,
    timestamp: int,
    content_type: str = TENCENT_CONTENT_TYPE,
) -> Tc3Signature:
    """用 TC3-HMAC-SHA256 计算一次请求签名（纯函数，无网络、无时间读取）。

    ``timestamp`` 由调用方提供，因此同一组输入**必然**得到同一签名（确定性），
    测试可注入固定时间戳断言。

    签名步骤（腾讯云签名方法 v3）：

    1. ``CanonicalRequest`` = 方法 + URI + 查询串 + 规范头 + 签名头 + 载荷哈希；
    2. ``StringToSign`` = 算法 + 时间戳 + 凭证范围 + ``CanonicalRequest`` 哈希；
    3. ``SecretDate`` = HMAC("TC3" + SecretKey, Date) → ``SecretService`` =
       HMAC(SecretDate, Service) → ``SecretSigning`` = HMAC(SecretService,
       "tc3_request") → ``Signature`` = Hex(HMAC(SecretSigning, StringToSign))；
    4. ``Authorization`` = 算法 + Credential + SignedHeaders + Signature。
    """
    date = datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")
    credential_scope = f"{date}/{service}/tc3_request"

    canonical_headers, signed_headers = _canonical_headers(
        {
            "content-type": content_type,
            "host": host,
            "x-tc-action": action,
        }
    )
    canonical_request = "\n".join(
        (
            "POST",
            "/",
            "",
            canonical_headers,
            signed_headers,
            _sha256_hex(payload),
        )
    )
    string_to_sign = "\n".join(
        (
            TENCENT_ALGORITHM,
            str(timestamp),
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        )
    )

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"{TENCENT_ALGORITHM} "
        f"Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    return Tc3Signature(
        authorization=authorization,
        canonical_request=canonical_request,
        string_to_sign=string_to_sign,
        signature=signature,
        signed_headers=signed_headers,
        credential_scope=credential_scope,
        timestamp=timestamp,
        date=date,
    )


# --- 响应解析 -----------------------------------------------------------------


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_decimal(value: Any) -> Decimal | None:
    """把渠道金额字符串解析为 2 位小数 ``Decimal``；无法解析返回 ``None``。"""
    if value is None or value == "":
        return None
    try:
        return quantize(to_decimal(str(value)))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _parse_bool_flag(value: Any) -> bool | None:
    """解析 ``IsAbandoned``（"N"/"Y"）/ ``RedLetterInvoiceMark``（bool）类标志。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"y", "yes", "true", "1"}:
            return True
        if text in {"n", "no", "false", "0"}:
            return False
    return None


def _parse_face(invoice: Mapping[str, Any]) -> InvoiceFace:
    """按 W0-4 §2.3 从 ``Response.Invoice`` 提取票面要素（不臆造未列字段）。"""
    items = invoice.get("Items")
    return InvoiceFace(
        invoice_code=_optional_str(invoice.get("Code")),
        invoice_no=_optional_str(invoice.get("Number")),
        invoice_date=parse_invoice_date(_optional_str(invoice.get("Date"))),
        seller_tax_code=_optional_str(invoice.get("SellerTaxCode")),
        buyer_tax_code=_optional_str(invoice.get("BuyerTaxCode")),
        amount_without_tax=_parse_decimal(invoice.get("AmountWithoutTax")),
        # W0-4 §2.3/§8：该页输出**未列出**独立「税额」字段，故不臆造字段名。
        tax_amount=None,
        amount_with_tax=_parse_decimal(invoice.get("AmountWithTax")),
        # W0-4 §2.3：该页未列币种字段；默认人民币需按真实响应确认，故不臆造。
        currency=None,
        electronic_type=_optional_str(invoice.get("ElectronicType")),
        is_abandoned=_parse_bool_flag(invoice.get("IsAbandoned")),
        is_red_letter=_parse_bool_flag(invoice.get("RedLetterInvoiceMark")),
        items=tuple(items) if isinstance(items, list) else (),
    )


def _face_mismatch(fields: InvoiceFields, face: InvoiceFace) -> str | None:
    """比较渠道票面与提交要素，返回不一致字段（逗号分隔）或 ``None``。

    只比较双方都提供了值的字段；金额因票种语义不同（不含税 vs 价税合计），只要
    与 ``amount_without_tax`` 或 ``amount_with_tax`` 任一相等即视为一致
    （W0-4 §2.2 对 ``Amount`` 的说明）。
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


class TencentVatInvoiceVerifyProvider(InvoiceVerificationProvider):
    """腾讯云 ``VatInvoiceVerifyNew`` 适配器（``authorized_provider``）。

    构造参数：

    * ``secret_id`` / ``secret_key``：**默认从环境变量读取**
      （``AJAIMGR_TENCENT_SECRET_ID`` / ``AJAIMGR_TENCENT_SECRET_KEY``）；显式传入
      仅用于测试，**任何情况下都不得写进仓库或日志**。缺失即抛
      ``MissingCredentialError``（fail closed）；
    * ``transport``：HTTP 传输函数（默认 ``httpx``）；测试注入桩，**绝不发真实
      外网请求**；
    * ``clock``：返回当前 UTC ``datetime`` 的可调用对象（默认 ``datetime.now(UTC)``），
      便于测试注入固定时间戳，使签名可复现；
    * ``timeout``：单次请求超时（秒）。

    超时与传输失败一律映射为 ``UNABLE_TO_VERIFY`` 并**只尝试一次**（不自动重发）；
    是否重试由调用方按幂等键决定（PLAN §6.3 第 3 条）。
    """

    def __init__(
        self,
        *,
        secret_id: str | None = None,
        secret_key: str | None = None,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        endpoint: str = TENCENT_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved_id = secret_id if secret_id is not None else os.environ.get(SECRET_ID_ENV)
        resolved_key = secret_key if secret_key is not None else os.environ.get(SECRET_KEY_ENV)
        if not resolved_id or not resolved_id.strip():
            raise MissingCredentialError(
                "missing Tencent Cloud credential: set "
                f"{SECRET_ID_ENV} and {SECRET_KEY_ENV} in the environment"
            )
        if not resolved_key or not resolved_key.strip():
            raise MissingCredentialError(
                f"missing Tencent Cloud credential: set {SECRET_KEY_ENV} in the environment"
            )

        self._secret_id = resolved_id.strip()
        self._secret_key = resolved_key.strip()
        self._transport: Transport = transport if transport is not None else _httpx_transport
        self._clock: Callable[[], datetime] = (
            clock if clock is not None else (lambda: datetime.now(UTC))
        )
        self._endpoint = endpoint
        self._timeout = timeout

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.AUTHORIZED_PROVIDER

    @property
    def provider_name(self) -> str:
        return TENCENT_PROVIDER_NAME

    # --- 请求构造 -------------------------------------------------------------

    @staticmethod
    def build_params(fields: InvoiceFields) -> dict[str, Any]:
        """按 W0-4 §2.2 构造 ``VatInvoiceVerifyNew`` 入参。

        ``InvoiceNo``/``InvoiceDate`` 必填；其余字段**仅在调用方提供时**传入——
        按票种裁剪，不多传（W0-4 §6「按票种只传该票种必需的字段」）。
        ``EnableTodayInvoice``/``EnableCommonElectronic`` 保持默认关闭（不传）。
        """
        params: dict[str, Any] = {
            "InvoiceNo": fields.invoice_no,
            "InvoiceDate": fields.invoice_date.isoformat(),
        }
        if fields.invoice_code:
            params["InvoiceCode"] = fields.invoice_code
        if fields.invoice_kind:
            params["InvoiceKind"] = fields.invoice_kind
        if fields.check_code:
            params["CheckCode"] = fields.check_code
        if fields.amount is not None:
            # 定点格式，避免科学计数法（如 1E+2）。
            params["Amount"] = format(fields.amount, "f")
        if fields.region_code:
            params["RegionCode"] = fields.region_code
        if fields.seller_tax_code:
            params["SellerTaxCode"] = fields.seller_tax_code
        return params

    # --- 查验 -----------------------------------------------------------------

    def verify(self, invoice_fields: InvoiceFields) -> VerificationResult:
        """执行一次查验；**不自动重发**，超时/传输失败一律 ``UNABLE_TO_VERIFY``。"""
        params = self.build_params(invoice_fields)
        payload = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = int(self._clock().timestamp())
        signature = sign_request(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            host=TENCENT_HOST,
            service=TENCENT_SERVICE,
            action=TENCENT_ACTION,
            payload=payload,
            timestamp=timestamp,
        )
        headers = {
            "Authorization": signature.authorization,
            "Content-Type": TENCENT_CONTENT_TYPE,
            "Host": TENCENT_HOST,
            "X-TC-Action": TENCENT_ACTION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": TENCENT_VERSION,
        }

        response = self._send(payload, headers)
        if isinstance(response, VerificationResult):
            return response  # 传输失败已在 _send 内映射为 UNABLE。
        return self._parse(response, invoice_fields)

    def _send(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> HttpResponse | VerificationResult:
        """发起**一次**请求；超时/传输失败映射为 ``UNABLE_TO_VERIFY``（不重发）。

        本接口是只读查询，但 PLAN §6.3 第 3 条禁止「超时后盲目重发可能产生副作用的
        请求」；这里统一**只尝试一次**，是否重试交由调用方按幂等键决定。
        """
        try:
            return self._transport(self._endpoint, headers, payload, self._timeout)
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
        """解析渠道响应：错误码映射 / 票面提取 / 票面一致性判定。"""
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

        body = data.get("Response")
        body = body if isinstance(body, dict) else {}
        request_id = _optional_str(body.get("RequestId") or data.get("RequestId"))

        error = body.get("Error")
        if isinstance(error, dict):
            code = _optional_str(error.get("Code"))
            return VerificationResult(
                status=map_error_code(code),
                provider_ref=request_id,
                evidence=data,
                error_code=code,
                reason="provider_error",
            )

        if response.status_code >= 400:
            return VerificationResult(
                status=VerificationStatus.UNABLE_TO_VERIFY,
                provider_ref=request_id,
                evidence=data,
                error_code=f"HTTP{response.status_code}",
                reason="provider_http_error",
            )

        invoice = body.get("Invoice")
        invoice = invoice if isinstance(invoice, dict) else {}
        face = _parse_face(invoice)

        mismatch = _face_mismatch(fields, face)
        if mismatch is not None:
            return VerificationResult(
                status=VerificationStatus.VERIFIED_FAIL,
                face=face,
                provider_ref=request_id,
                evidence=data,
                error_code=None,
                reason=f"face_mismatch:{mismatch}",
            )
        if face.is_abandoned:
            return VerificationResult(
                status=VerificationStatus.VERIFIED_FAIL,
                face=face,
                provider_ref=request_id,
                evidence=data,
                error_code=None,
                reason="invoice_abandoned",
            )
        if face.is_red_letter:
            return VerificationResult(
                status=VerificationStatus.VERIFIED_FAIL,
                face=face,
                provider_ref=request_id,
                evidence=data,
                error_code=None,
                reason="red_letter_invoice",
            )
        return VerificationResult(
            status=VerificationStatus.VERIFIED_PASS,
            face=face,
            provider_ref=request_id,
            evidence=data,
            error_code=None,
            reason=None,
        )
