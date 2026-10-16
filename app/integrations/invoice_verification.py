"""发票查验可替换接口（provider contract）与七种结果状态。

本模块把 ``templates/PLAN.md`` §7.1（先做可替换的查验接口）与 §7.2（发票数据
字段与状态）落成**与渠道无关**的契约层，并严格对齐
``docs/research/W0-4-invoice-verification.md``（W0-4）§8 的「字段与状态对应」：

* ``ProviderType``：三类实现类型 ``manual_assisted`` / ``authorized_provider`` /
  ``official_api``（PLAN §7.1）。当前只有 ``manual_assisted`` 与
  ``authorized_provider`` 有实现——**首选**：百度智能云「增值税发票验真」
  （``app.integrations.baidu_vat_verify``，所有者 2026-10-16 决策）；**备用**：
  腾讯云 ``VatInvoiceVerifyNew``（``app.integrations.tencent_vat_verify``）；
  ``official_api`` 在取得可适用的官方 API 文档与合法授权前**不实现**（W0-4 §5）。
* ``VerificationStatus``：严格对应 PLAN §7.2 的**七种**状态（英文名 + 中文注释）。
* ``InvoiceVerificationProvider``：抽象接口，含 ``provider_type`` /
  ``provider_name`` / ``verify(invoice_fields) -> VerificationResult``。上层业务只
  依赖本接口，切换渠道不影响业务域（与 W0-1 §5「渠道适配层」同一思路）。
* ``VerificationResult``：``status``、票面字段（``face``）、``provider_ref``（渠道
  请求号，如腾讯云 ``RequestId``）、``evidence``（**待脱敏后落库**的原始响应）、
  ``error_code``、``reason``。
* ``ManualAssistedProvider``：兜底实现，**永远不得返回「查验通过」**——它只产出
  「待人工确认」的核验任务（PLAN §7.1 / 本任务第四步第 1 条）。

硬边界（对齐 PLAN §7.1/§7.2 与 W0-4 §8）：

1. **只有来自被认可渠道的结果才能进入「查验通过」**。``can_set_verified_pass``
   只对 ``authorized_provider`` 与 ``official_api`` 返回真；``manual_assisted``
   与任何未识别来源一律为假。**OCR 识别成功不等于查验通过**。
2. **「查验不通过」与「无法查验/接口异常」严格区分**，``resolve_status`` 保证
   二者互不覆盖（技术性失败不得抹掉票面核验结论，反之亦然）。
3. **疑似重复由业务去重产生**，渠道不提供重复判定（渠道的「查验次数」不是重复
   标志，W0-4 §8）。
4. **日志与审计不得出现凭据、税号或票号明文**：``compute_duplicate_key`` 只输出
   单向摘要，``sanitize_evidence`` 在证据落库前对票号/代码/校验码/税号打码，并
   整体丢弃任何凭据类键。

本模块**只依赖标准库**（``dataclasses`` / ``decimal`` / ``enum`` / ``hashlib`` /
``abc`` 等）与既有的 ``app.expense.money``，不引入任何新依赖，也不进行任何网络
调用。
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.expense.money import quantize, to_decimal

__all__ = [
    "DAILY_VERIFY_LIMIT",
    "DUPLICATE_KEY_PREFIX",
    "EVIDENCE_REF_PREFIX",
    "RECOGNIZED_VERIFIED_PASS_PROVIDER_TYPES",
    "InvoiceFace",
    "InvoiceFields",
    "InvoiceVerificationProvider",
    "ManualAssistedProvider",
    "ProviderError",
    "ProviderType",
    "SENSITIVE_EVIDENCE_KEYS",
    "VerificationResult",
    "VerificationStatus",
    "can_set_verified_pass",
    "coerce_provider_type",
    "compute_duplicate_key",
    "evidence_ref_for",
    "is_conclusive_status",
    "is_pending_status",
    "is_recognized_provider_type",
    "parse_invoice_date",
    "resolve_status",
    "sanitize_evidence",
]

#: 渠道侧「同一张票当日查验次数上限」（W0-4 §2.5：``InvoiceCode`` 说明「查验超过
#: 5 次后当日无法再查」）。本项目的服务层据此在**调用渠道之前**拒绝第 6 次。
DAILY_VERIFY_LIMIT = 5

#: ``duplicate_key`` 的域分隔前缀：避免与其它表的摘要撞车，并让摘要不可直接反查。
DUPLICATE_KEY_PREFIX = "invoice-verification:v1:"

#: ``evidence_ref`` 的前缀，标明它是指向「脱敏后证据」的内容寻址引用。
EVIDENCE_REF_PREFIX = "sha256:"


class ProviderType(str, Enum):
    """查验渠道类型（PLAN §7.1 的三类实现）。

    * ``MANUAL_ASSISTED``（``manual_assisted``）：人工辅助兜底——Agent/OCR 提取
      字段、去重并生成查验任务，授权财务人员经官方平台核验后录入结果；
    * ``AUTHORIZED_PROVIDER``（``authorized_provider``）：经批准的授权服务商
      （所有者 2026-10-16 决策：首选为百度智能云「增值税发票验真」，
      ``app.integrations.baidu_vat_verify``；腾讯云 ``VatInvoiceVerifyNew`` 为
      **备用实现**，``app.integrations.tencent_vat_verify``）；
    * ``OFFICIAL_API``（``official_api``）：仅在取得可适用的官方 API 文档和合法
      授权后才实现，当前**不可用**（W0-4 §5）。

    继承 ``str`` 便于直接落库（``TEXT`` + ``CHECK``，不用数据库 ENUM）。
    """

    MANUAL_ASSISTED = "manual_assisted"
    AUTHORIZED_PROVIDER = "authorized_provider"
    OFFICIAL_API = "official_api"


#: 只有这些渠道类型产出的结果可以进入「查验通过」（PLAN §7.2 / W0-4 §8）。
#: ``manual_assisted`` **不在其中**：人工辅助只产出待人工确认的任务。
RECOGNIZED_VERIFIED_PASS_PROVIDER_TYPES: frozenset[ProviderType] = frozenset(
    {ProviderType.AUTHORIZED_PROVIDER, ProviderType.OFFICIAL_API}
)


class VerificationStatus(str, Enum):
    """查验结果状态：严格对应 PLAN §7.2 的七种状态（英文名 + 中文注释）。

    ==========================  ==============================
    成员                        中文（PLAN §7.2）
    ==========================  ==============================
    ``PENDING_IDENTIFICATION``  待识别
    ``PENDING_MANUAL_CONFIRM``  待人工确认
    ``PENDING_VERIFICATION``    待查验
    ``VERIFIED_PASS``           查验通过
    ``VERIFIED_FAIL``           查验不通过
    ``UNABLE_TO_VERIFY``        无法查验/接口异常
    ``SUSPECTED_DUPLICATE``     疑似重复
    ==========================  ==============================
    """

    PENDING_IDENTIFICATION = "pending_identification"  # 待识别
    PENDING_MANUAL_CONFIRMATION = "pending_manual_confirmation"  # 待人工确认
    PENDING_VERIFICATION = "pending_verification"  # 待查验
    VERIFIED_PASS = "verified_pass"  # 查验通过
    VERIFIED_FAIL = "verified_fail"  # 查验不通过
    UNABLE_TO_VERIFY = "unable_to_verify"  # 无法查验/接口异常
    SUSPECTED_DUPLICATE = "suspected_duplicate"  # 疑似重复


#: 「待…」类中间状态：尚未取得任何渠道结论。
_PENDING_STATUSES: frozenset[VerificationStatus] = frozenset(
    {
        VerificationStatus.PENDING_IDENTIFICATION,
        VerificationStatus.PENDING_MANUAL_CONFIRMATION,
        VerificationStatus.PENDING_VERIFICATION,
    }
)

#: 票面核验的**确凿结论**（终态）。
_CONCLUSIVE_STATUSES: frozenset[VerificationStatus] = frozenset(
    {VerificationStatus.VERIFIED_PASS, VerificationStatus.VERIFIED_FAIL}
)


def is_pending_status(status: VerificationStatus) -> bool:
    """是否为「待…」类中间状态（待识别 / 待人工确认 / 待查验）。"""
    return status in _PENDING_STATUSES


def is_conclusive_status(status: VerificationStatus) -> bool:
    """是否为确凿结论（查验通过 / 查验不通过）。"""
    return status in _CONCLUSIVE_STATUSES


def can_set_verified_pass(provider_type: ProviderType) -> bool:
    """该渠道类型能否把结果置为「查验通过」（PLAN §7.2 硬性规则）。

    只有 ``authorized_provider`` 与 ``official_api`` 为真；``manual_assisted``
    与任何未识别来源一律为假。**OCR 识别成功、模型自述「看起来是真的」、未授权
    的网页抓取结果都不得置为「查验通过」。**
    """
    return provider_type in RECOGNIZED_VERIFIED_PASS_PROVIDER_TYPES


def is_recognized_provider_type(value: object) -> bool:
    """``value`` 是否是**已定义**的渠道类型（``ProviderType`` 成员或其取值）。

    未识别来源（任意其它字符串、``None``、未知枚举）一律为假——用于 fail closed。
    """
    if isinstance(value, ProviderType):
        return True
    if isinstance(value, str):
        try:
            ProviderType(value)
        except ValueError:
            return False
        return True
    return False


def coerce_provider_type(value: object) -> ProviderType:
    """把 ``value`` 规范为 ``ProviderType``；未识别来源抛 ``ProviderError``。

    这是服务层的 fail-closed 闸门：任何不是三类已定义渠道的来源都不得进入业务
    流程，更不得进入「查验通过」。
    """
    if isinstance(value, ProviderType):
        return value
    if isinstance(value, str):
        try:
            return ProviderType(value)
        except ValueError:
            pass
    raise ProviderError(f"unrecognized invoice verification provider: {value!r}")


def resolve_status(
    existing: VerificationStatus | None,
    incoming: VerificationStatus,
) -> VerificationStatus:
    """按硬性业务规则合并「已有状态」与「本次结果状态」。

    规则（PLAN §7.2 / W0-4 §8）：

    * ``existing is None``：直接采用 ``incoming``；
    * **查验通过是终态**：已有 ``VERIFIED_PASS`` 时不被任何后续结果覆盖（重复查验
      不产生第二条记录，也不得把「通过」降级）；
    * **查验不通过是终态**：已有 ``VERIFIED_FAIL`` 时不被覆盖——特别是
      ``UNABLE_TO_VERIFY`` **不得**抹掉「票面不一致」的结论（二者严格区分）；
    * 已有 ``UNABLE_TO_VERIFY``（技术性失败）可被确凿结论
      （``VERIFIED_PASS``/``VERIFIED_FAIL``）取代，但**不得**被「待…」类中间状态
      或重复标记降级；
    * 已有 ``SUSPECTED_DUPLICATE`` 可被任何渠道结论取代；
    * 已有「待…」类中间状态：采用 ``incoming``（后续阶段推进）。
    """
    if existing is None:
        return incoming
    if existing == incoming:
        return existing

    if existing is VerificationStatus.VERIFIED_PASS:
        return existing
    if existing is VerificationStatus.VERIFIED_FAIL:
        return existing
    if existing is VerificationStatus.UNABLE_TO_VERIFY:
        if incoming in _CONCLUSIVE_STATUSES:
            return incoming
        return existing
    if existing is VerificationStatus.SUSPECTED_DUPLICATE:
        if incoming in _CONCLUSIVE_STATUSES or incoming is VerificationStatus.UNABLE_TO_VERIFY:
            return incoming
        return existing
    # existing 是「待…」类中间状态：采用后续阶段的状态。
    return incoming


# --- 发票字段 -----------------------------------------------------------------


def parse_invoice_date(value: date | str | None) -> date | None:
    """把 ``YYYY-MM-DD`` 或 ``YYYYMMDD`` 解析为 ``date``；无法解析返回 ``None``。

    只接受这两种文档化格式（腾讯云入参 ``InvoiceDate`` 为 ``YYYY-MM-DD``，
    W0-4 §2.2）；其余形式一律 ``None``（不猜测、不放宽）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class InvoiceFields:
    """提交查验所需的发票要素（渠道入参的与渠道无关表达）。

    * ``invoice_no``：发票号码（**必填**，8 位或全电 20 位）；
    * ``invoice_date``：开票日期（**必填**，``date`` 或 ``YYYY-MM-DD``/``YYYYMMDD``
      字符串）；渠道侧不支持当天发票（``EnableTodayInvoice`` 默认关闭）；
    * ``invoice_code``：发票代码（10/12 位，全电发票为空 → 传 ``None``）；
    * ``invoice_kind``：票种类型代码（腾讯云 ``InvoiceKind``）；
    * ``check_code``：校验码**后 6 位**（部分票种必填）；
    * ``amount``：金额。语义随票种而变（专票/电子专票/机动车/二手车/区块链/财政
      为**不含税金额**；全电发票为**价税合计**），由调用方按票种显式传值；
    * ``region_code``：地区编码（通用机打电子发票必填）；
    * ``seller_tax_code`` / ``buyer_tax_code``：销方/购方税号（**受权限保护**，
      见 ``app.integrations.models.InvoiceVerification`` 的 docstring）。

    金额一律经 ``app.expense.money`` 规范化：``float`` 被 ``TypeError`` 拒绝，
    其余量化到 2 位小数（PLAN §8「金额采用固定精度 decimal」）。
    """

    invoice_no: str
    invoice_date: date
    invoice_code: str | None = None
    invoice_kind: str | None = None
    check_code: str | None = None
    amount: Decimal | None = None
    region_code: str | None = None
    seller_tax_code: str | None = None
    buyer_tax_code: str | None = None

    def __post_init__(self) -> None:
        invoice_no = self.invoice_no.strip() if isinstance(self.invoice_no, str) else ""
        if not invoice_no:
            raise ValueError("invoice_no is required")
        object.__setattr__(self, "invoice_no", invoice_no)

        parsed_date = parse_invoice_date(self.invoice_date)
        if parsed_date is None:
            raise ValueError(
                "invoice_date must be a date or YYYY-MM-DD/YYYYMMDD string, "
                f"got {self.invoice_date!r}"
            )
        object.__setattr__(self, "invoice_date", parsed_date)

        if self.amount is not None:
            # float 在 to_decimal 中以 TypeError 被拒（不引入二进制误差）。
            object.__setattr__(self, "amount", quantize(to_decimal(self.amount)))

        for name in (
            "invoice_code",
            "invoice_kind",
            "check_code",
            "region_code",
            "seller_tax_code",
            "buyer_tax_code",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, value.strip() or None)


@dataclass(frozen=True)
class InvoiceFace:
    """渠道返回的**票面要素**（W0-4 §2.3）。

    字段名与 PLAN §7.2 的「保存字段」对应；渠道未返回的字段保持 ``None``，
    **绝不臆造**（例如 W0-4 §2.3 指出腾讯云页面未列出独立的「税额」「币种」
    字段，故 ``tax_amount``/``currency`` 在腾讯云渠道下通常为 ``None``，须以真实
    响应或完整字段表核对，见 W0-4 §8）。

    ``is_abandoned`` / ``is_red_letter`` 分别对应腾讯云的 ``IsAbandoned``（作废
    标志）与 ``RedLetterInvoiceMark``（红字发票标志）；``items`` 为 ``Invoice.
    Items`` 货物明细数组（原样保留，不解释）。
    """

    invoice_code: str | None = None
    invoice_no: str | None = None
    invoice_date: date | None = None
    seller_tax_code: str | None = None
    buyer_tax_code: str | None = None
    amount_without_tax: Decimal | None = None
    tax_amount: Decimal | None = None
    amount_with_tax: Decimal | None = None
    currency: str | None = None
    electronic_type: str | None = None
    is_abandoned: bool | None = None
    is_red_letter: bool | None = None
    items: tuple[Any, ...] = ()


@dataclass(frozen=True)
class VerificationResult:
    """一次查验的结果（渠道无关）。

    * ``status``：七种状态之一（PLAN §7.2）；
    * ``face``：渠道返回的票面要素（``InvoiceFace``）；
    * ``provider_ref``：渠道请求号（腾讯云 ``RequestId``），用于定位问题与幂等；
    * ``evidence``：**待脱敏后落库**的原始响应（渠道返回一律视为不可信数据，
      PLAN §8）；落库前必须经 ``sanitize_evidence``；
    * ``error_code``：渠道错误码（如 ``FailedOperation.InvoiceMismatch``）；
    * ``reason``：机器可读的判定原因（如 ``face_mismatch:invoice_no``）。
    """

    status: VerificationStatus
    face: InvoiceFace = field(default_factory=InvoiceFace)
    provider_ref: str | None = None
    evidence: Mapping[str, Any] | None = None
    error_code: str | None = None
    reason: str | None = None


# --- 摘要与脱敏 ---------------------------------------------------------------


def compute_duplicate_key(fields: InvoiceFields) -> str:
    """按发票身份字段算 ``duplicate_key``（幂等键 + 去重键）。

    身份 = ``invoice_code`` + ``invoice_no`` + ``invoice_date``（W0-4 §8：全电票
    代码可为空）。返回的是**单向 sha256 摘要**，因此可以安全地写进日志、审计
    ``details`` 与数据库唯一约束，而不泄露票号/代码明文（本任务第四步第 4 条）。
    """
    identity = "|".join(
        (
            fields.invoice_code or "",
            fields.invoice_no,
            fields.invoice_date.isoformat(),
        )
    )
    return hashlib.sha256((DUPLICATE_KEY_PREFIX + identity).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> bytes:
    """稳定序列化（键排序、无多余空白），用于内容寻址摘要。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def evidence_ref_for(sanitized_evidence: Mapping[str, Any]) -> str:
    """计算脱敏证据的内容寻址引用（``sha256:<hex>``）。"""
    digest = hashlib.sha256(_canonical_json(sanitized_evidence)).hexdigest()
    return f"{EVIDENCE_REF_PREFIX}{digest}"


#: 需要打码的票面字段名（小写比较）。对齐 W0-5 §3.1：票号/代码/校验码与购销方
#: 税号为 B 类（必须脱敏后才可入日志/模型/审计）。
SENSITIVE_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {
        "invoiceno",
        "invoicecode",
        "number",
        "code",
        "checkcode",
        "sellertaxcode",
        "buyertaxcode",
    }
)

#: 键名包含这些子串即整体替换（凭据类，绝不落库/落日志）。
_CREDENTIAL_KEY_MARKERS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "apikey",
    "api_key",
    "accesskey",
    "privatekey",
)

#: 键名包含这些子串即整体丢弃（附件/文档原文，A 类字段，W0-5 §3.1）。
_PAYLOAD_KEY_MARKERS: tuple[str, ...] = ("content", "payload", "body", "filedata")


def _mask(value: Any) -> Any:
    """打码单个敏感值：保留末 4 位，其余以 ``****`` 取代。

    ``None``/空串保持原样；非字符串先转成字符串再打码，避免把任意对象塞进证据。
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    return "****" + text[-4:]


def sanitize_evidence(evidence: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """把渠道原始响应脱敏为**可落库**的证据字典。

    处理规则（W0-5 §3.1/§3.2）：

    * 票号/代码/校验码/购销方税号 → 保留末 4 位、其余打码（B 类字段）；
    * 键名含 ``secret``/``token``/``password``/``credential``/``apikey`` 等 → 整体
      替换为 ``"***redacted***"``（凭据绝不落库）；
    * 键名含 ``content``/``payload``/``body``/``filedata`` → 整体丢弃（A 类原文）；
    * 递归处理嵌套 dict/list，保证深层字段同样脱敏。

    返回新的 dict；``None`` 入参返回 ``None``。本函数**不修改入参**。
    """
    if evidence is None:
        return None

    def _walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                lowered = name.lower()
                if any(marker in lowered for marker in _CREDENTIAL_KEY_MARKERS):
                    out[name] = "***redacted***"
                    continue
                if any(marker in lowered for marker in _PAYLOAD_KEY_MARKERS):
                    continue
                if lowered in SENSITIVE_EVIDENCE_KEYS:
                    out[name] = _mask(item)
                    continue
                out[name] = _walk(item)
            return out
        if isinstance(value, (list, tuple)):
            return [_walk(item) for item in value]
        return value

    result = _walk(evidence)
    # 入参是 Mapping，因此顶层一定是 dict；兜底以满足类型检查。
    return result if isinstance(result, dict) else {"value": result}


# --- 抽象接口 -----------------------------------------------------------------


class ProviderError(Exception):
    """查验渠道层错误基类。

    错误信息**不得**包含凭据值、税号或票号明文（本任务第四步第 4 条）。
    """


class InvoiceVerificationProvider(ABC):
    """发票查验渠道的抽象接口（PLAN §7.1）。

    上层业务只依赖本接口；``verify`` 是**只读查询**（不改动任何外部状态），但实现
    仍必须记录 ``provider_ref`` 并保持幂等：同一张票重复查验不得产生副作用，
    超时后不得盲目重发（PLAN §6.3 第 3 条 / 本任务第二步）。
    """

    @property
    @abstractmethod
    def provider_type(self) -> ProviderType:
        """渠道类型（PLAN §7.1 的三类之一）。"""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """渠道标识（落库到 ``provider_name``，如 ``tencent_cloud/vat_invoice_verify_new``）。"""

    @abstractmethod
    def verify(self, invoice_fields: InvoiceFields) -> VerificationResult:
        """对 ``invoice_fields`` 执行一次查验，返回渠道无关的 ``VerificationResult``。"""


class ManualAssistedProvider(InvoiceVerificationProvider):
    """人工辅助兜底实现（PLAN §7.1 第 1 类）。

    **它永远不得返回「查验通过」。** 它只产出「待人工确认」的核验任务：Agent/OCR
    提取字段、去重后生成任务，由授权财务人员经官方平台核验后**另行录入**结果与
    证据（该录入路径属于人工辅助 UI/工作流，不在本任务范围）。

    因此本实现的 ``verify`` 恒定返回
    ``VerificationStatus.PENDING_MANUAL_CONFIRMATION``，且不带任何票面结论与证据
    ——这从接口层保证了「OCR 识别成功不等于查验通过」（PLAN §7.1 硬边界）。
    """

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.MANUAL_ASSISTED

    @property
    def provider_name(self) -> str:
        return "manual_assisted"

    def verify(self, invoice_fields: InvoiceFields) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.PENDING_MANUAL_CONFIRMATION,
            face=InvoiceFace(
                invoice_code=invoice_fields.invoice_code,
                invoice_no=invoice_fields.invoice_no,
                invoice_date=invoice_fields.invoice_date,
            ),
            provider_ref=None,
            evidence=None,
            error_code=None,
            reason="manual_verification_required",
        )
