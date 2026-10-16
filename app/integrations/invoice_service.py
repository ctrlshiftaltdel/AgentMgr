"""发票查验服务层：授权、状态机、幂等、当日限次与审计。

本模块把 ``templates/PLAN.md`` §7.1/§7.2、§8 与
``docs/research/W0-4-invoice-verification.md``（W0-4）§8 的**硬性业务规则**落成
确定性的后端服务：

1. **只有来自被认可渠道的结果才能进入「查验通过」**：``verify_invoice`` 先经
   ``app.integrations.invoice_verification.can_set_verified_pass`` 判定渠道类型；
   未识别来源直接拒绝（``UnrecognizedProviderError``），``manual_assisted`` 即便
   声称「通过」也会被**降级为「待人工确认」**。OCR 识别成功不等于查验通过。
2. **「查验不通过」与「无法查验/接口异常」严格区分**：状态合并一律经
   ``resolve_status``，技术性失败不得抹掉票面核验结论，反之亦然。
3. **幂等 + 当日限次**：``duplicate_key`` 有唯一约束，同一张票重复查验**不产生第二
   条记录**（在既有行上更新）；同一张票当日查验次数达到
   ``DAILY_VERIFY_LIMIT``（5，W0-4 §2.5）后，服务层在**调用渠道之前**拒绝第 6 次
   （``DailyVerifyLimitExceeded``），既不改库也不发请求。
4. **日志与审计不得出现凭据、税号或票号明文**：``details`` 只含状态、错误码、渠道
   标识、脱敏后的证据引用与 ``duplicate_key``（单向 sha256 摘要）；证据在落库前经
   ``sanitize_evidence`` 打码。
5. **人工辅助的「录入路径」是受权限门控的显式动作，与 provider 闸门互补**：
   ``record_manual_verification`` 允许**授权财务人员**（``invoice/verify/company``）
   在官方平台核验后**显式录入**结果、留存证据，从而把「待人工确认」推进为「查验
   通过」（PLAN §7.1 第 1 类；W0-4 §8）。它**不改变**上一条 provider 闸门：
   ``ManualAssistedProvider`` 与任何自动流程**永远**不能经 ``verify_invoice`` 产生
   「查验通过」（该语义由 W1-20 固定）；反过来，渠道已给出「查验不通过」终态时，
   人工录入也不得把它静默抹掉（拒绝审计 + 业务异常）。

事务边界由调用方控制：本模块所有函数只 ``flush``，**绝不 ``commit``**（与
``app.audit.service.record_event``、``app.expense.service`` 一致），使业务变更与审计
写入处于同一事务。

审计事件约定：

* 成功：``action='invoice.verify'``、``resource_type='invoice_verification'``、
  ``resource_id=str(row.id)``、``actor_id=principal.employee_id``，``details`` 含
  ``from_status`` / ``to_status`` / ``status`` / ``provider_type`` /
  ``provider_name`` / ``error_code`` / ``duplicate_check_result`` /
  ``duplicate_key``（**均为非敏感值**）；
* 拒绝：``action='invoice.verify.deny'``，``details`` 含 ``attempted_action`` 与
  ``reason``（**不含票号/税号/凭据**）。
* 人工录入成功：``action='invoice.manual_verify'``；拒绝：``action=''
  invoice.manual_verify.deny'``——``details`` 同样只含非敏感结构化值
  （``from_status`` / ``to_status`` / ``provider_type`` / ``has_evidence`` /
  ``duplicate_key`` 摘要等），**不含票号/税号明文**。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.identity.authorization import Principal, has_permission
from app.integrations.invoice_verification import (
    DAILY_VERIFY_LIMIT,
    InvoiceFace,
    InvoiceFields,
    InvoiceVerificationProvider,
    ProviderType,
    VerificationResult,
    VerificationStatus,
    can_set_verified_pass,
    coerce_provider_type,
    compute_duplicate_key,
    evidence_ref_for,
    is_pending_status,
    resolve_status,
    sanitize_evidence,
)
from app.integrations.models import InvoiceVerification

__all__ = [
    "DENY_ACTION",
    "DUPLICATE_UNIQUE",
    "DUPLICATE_SUSPECTED",
    "MANUAL_DENY_ACTION",
    "MANUAL_PROVIDER_NAME",
    "MANUAL_PROVIDER_TYPE",
    "MANUAL_VERIFY_ACTION",
    "PERMISSION",
    "RESOURCE_TYPE",
    "VERIFY_ACTION",
    "DailyVerifyLimitExceeded",
    "InvoiceServiceError",
    "InvoiceVerificationDeniedError",
    "ManualVerificationConflictError",
    "ManualVerificationDeniedError",
    "ManualVerificationEvidenceError",
    "UnrecognizedProviderError",
    "classify_duplicate",
    "record_manual_verification",
    "verify_invoice",
]

#: 审计 ``resource_type``：所有发票查验服务事件都挂在这个资源类型上。
RESOURCE_TYPE = "invoice_verification"

#: 成功审计动作名。
VERIFY_ACTION = "invoice.verify"

#: 拒绝审计动作名（PLAN §12「权限拒绝可审计」）。
DENY_ACTION = "invoice.verify.deny"

#: 人工辅助录入的成功审计动作名（W0-4 §8：人工结果只能来自授权人员的显式录入）。
MANUAL_VERIFY_ACTION = "invoice.manual_verify"

#: 人工辅助录入的拒绝审计动作名。
MANUAL_DENY_ACTION = "invoice.manual_verify.deny"

#: 执行查验所需的权限（PLAN §9：财务经办与财务主管拥有 ``invoice/verify/company``）。
PERMISSION: tuple[str, str, str] = ("invoice", "verify", "company")

#: 人工辅助录入落库时使用的渠道类型与渠道标识（PLAN §7.1 第 1 类）。
MANUAL_PROVIDER_TYPE = ProviderType.MANUAL_ASSISTED.value
MANUAL_PROVIDER_NAME = "manual_assisted"

#: 去重结果取值（渠道不提供重复判定，由业务去重产生，W0-4 §8）。
DUPLICATE_UNIQUE = "unique"
DUPLICATE_SUSPECTED = "suspected_duplicate"


class InvoiceServiceError(Exception):
    """发票查验服务层错误基类。"""


class InvoiceVerificationDeniedError(InvoiceServiceError):
    """授权判定失败：调用主体无权执行查验（默认拒绝）。

    业务状态**保持不变**，并已写入一条拒绝审计事件。
    """

    def __init__(self, *, attempted_action: str, reason: str) -> None:
        self.attempted_action = attempted_action
        self.reason = reason
        super().__init__(
            f"authorization denied for {attempted_action!r}: {reason}"
        )


class UnrecognizedProviderError(InvoiceServiceError):
    """渠道类型不是 PLAN §7.1 的三类已定义实现（fail closed）。

    未识别来源一律不得进入业务，更不得置为「查验通过」；业务状态保持不变，并已写
    入一条拒绝审计事件。
    """

    def __init__(self, *, attempted_action: str, reason: str) -> None:
        self.attempted_action = attempted_action
        self.reason = reason
        super().__init__(
            f"unrecognized provider for {attempted_action!r}: {reason}"
        )


class DailyVerifyLimitExceeded(InvoiceServiceError):
    """同一张票当日查验次数已达渠道上限（W0-4 §2.5：超过 5 次当日不可再查）。

    服务层在**调用渠道之前**拒绝，因此**不改库、不发请求**（幂等且遵守限次）。
    """

    def __init__(self, *, duplicate_key: str, limit: int) -> None:
        self.duplicate_key = duplicate_key
        self.limit = limit
        super().__init__(
            f"daily verification limit reached for this invoice "
            f"(limit={limit}, duplicate_key={duplicate_key})"
        )


class ManualVerificationDeniedError(InvoiceServiceError):
    """授权判定失败：调用主体无权录入人工辅助查验结果（默认拒绝）。

    业务状态**保持不变**，并已写入一条拒绝审计事件（``invoice.manual_verify.deny``）。
    """

    def __init__(self, *, attempted_action: str, reason: str) -> None:
        self.attempted_action = attempted_action
        self.reason = reason
        super().__init__(
            f"authorization denied for {attempted_action!r}: {reason}"
        )


class ManualVerificationEvidenceError(InvoiceServiceError):
    """人工录入缺少必填要素：非空证据说明（或经核验的操作人主体）。

    拒绝且**不落任何状态变更**（不写审计、不改库），供 API 映射为 422。
    """

    def __init__(self) -> None:
        super().__init__(
            "manual verification requires a non-empty evidence note "
            "and a verified operator"
        )


class ManualVerificationConflictError(InvoiceServiceError):
    """人工录入与既有渠道结论冲突（如「查验不通过」是渠道终态，不得被抹掉）。

    业务状态**保持不变**，并已写入一条拒绝审计事件。
    """

    def __init__(self, *, current_status: str, reason: str) -> None:
        self.current_status = current_status
        self.reason = reason
        super().__init__(
            f"manual verification conflicts with current status "
            f"{current_status!r}: {reason}"
        )


# --- 内部辅助 -----------------------------------------------------------------


def _record_deny(
    session: Session,
    principal: Principal,
    *,
    resource_id: str | None,
    attempted_action: str,
    reason: str,
    request_id: str | None,
) -> None:
    """写入一条拒绝审计事件（不 commit）。

    ``details`` 只含结构化的 ``attempted_action`` 与 ``reason``，**不含**票号、税号、
    凭据或其它敏感原文（本任务第四步第 4 条）。
    """
    record_event(
        session,
        actor_id=principal.employee_id,
        action=DENY_ACTION,
        resource_type=RESOURCE_TYPE,
        resource_id=resource_id,
        request_id=request_id,
        details={"attempted_action": attempted_action, "reason": reason},
    )


def _record_manual_deny(
    session: Session,
    principal: Principal,
    *,
    resource_id: str | None,
    reason: str,
    request_id: str | None,
) -> None:
    """写入一条**人工录入**拒绝审计事件（``invoice.manual_verify.deny``，不 commit）。

    ``details`` 只含 ``attempted_action`` 与 ``reason``，**不含**票号、税号或凭据
    原文（与 ``_record_deny`` 同一约束）。
    """
    record_event(
        session,
        actor_id=principal.employee_id,
        action=MANUAL_DENY_ACTION,
        resource_type=RESOURCE_TYPE,
        resource_id=resource_id,
        request_id=request_id,
        details={"attempted_action": "manual_verify", "reason": reason},
    )


def classify_duplicate(
    existing_source_file_id: int | None,
    incoming_source_file_id: int | None,
) -> str:
    """业务去重判定：同一张票出现在**不同原件**上即视为「疑似重复」。

    渠道不提供重复判定（W0-4 §8：渠道的「查验次数」不是重复标志），因此这里只依据
    本项目的原件引用做判定：

    * 首次入库（``existing_source_file_id is None``）→ ``unique``；
    * 同一原件重复查验（同一份材料再查一次）→ ``unique``；
    * 同一张票但来自**另一份原件** → ``suspected_duplicate``。
    """
    if existing_source_file_id is None or incoming_source_file_id is None:
        return DUPLICATE_UNIQUE
    if existing_source_file_id == incoming_source_file_id:
        return DUPLICATE_UNIQUE
    return DUPLICATE_SUSPECTED


def _apply_face(
    row: InvoiceVerification,
    face: InvoiceFace,
    fields: InvoiceFields,
    *,
    new: bool,
) -> None:
    """把票面要素并入记录。

    * 新建行先落**提交要素**（票号/代码/开票日期/票种，调用方已确认的输入）；
    * 渠道返回的值**非 None 时**才覆盖——「无法查验」时渠道不返回票面，此时不得把
      已确认的要素或金额抹成空（W0-4 §8 的诚实标注：未返回即保持未知）。
    """
    if new:
        row.invoice_no = fields.invoice_no
        row.invoice_date = fields.invoice_date
        row.invoice_code = fields.invoice_code
        row.invoice_type = fields.invoice_kind

    if face.invoice_no is not None:
        row.invoice_no = face.invoice_no
    if face.invoice_code is not None:
        row.invoice_code = face.invoice_code
    if face.invoice_date is not None:
        row.invoice_date = face.invoice_date
    if face.seller_tax_code is not None:
        row.seller_tax_code = face.seller_tax_code
    if face.buyer_tax_code is not None:
        row.buyer_tax_code = face.buyer_tax_code
    if face.amount_without_tax is not None:
        row.amount_without_tax = face.amount_without_tax
    if face.tax_amount is not None:
        row.tax_amount = face.tax_amount
    if face.amount_with_tax is not None:
        row.amount_with_tax = face.amount_with_tax
    if face.currency is not None:
        row.currency = face.currency
    if row.invoice_type is None and fields.invoice_kind is not None:
        row.invoice_type = fields.invoice_kind


def _apply_evidence(row: InvoiceVerification, result: VerificationResult) -> None:
    """把渠道原始响应脱敏后落库，并写入内容寻址的 ``evidence_ref``。

    只有本次结果**带证据**时才覆盖既有证据：技术性失败（无响应体）不得抹掉上一次
    已保存的脱敏证据。
    """
    sanitized = sanitize_evidence(result.evidence)
    if sanitized is None:
        return
    row.evidence_json = sanitized
    row.evidence_ref = evidence_ref_for(sanitized)


def _audit_details(
    *,
    from_status: str | None,
    to_status: str,
    provider_type: ProviderType,
    provider_name: str,
    error_code: str | None,
    duplicate_check_result: str,
    duplicate_key: str,
) -> dict[str, Any]:
    """成功审计的 ``details``：只放**非敏感**结构化值。"""
    return {
        "from_status": from_status,
        "to_status": to_status,
        "status": to_status,
        "provider_type": provider_type.value,
        "provider_name": provider_name,
        "error_code": error_code,
        "duplicate_check_result": duplicate_check_result,
        "duplicate_key": duplicate_key,
    }


# --- 服务函数 -----------------------------------------------------------------


def verify_invoice(
    session: Session,
    principal: Principal,
    provider: InvoiceVerificationProvider,
    fields: InvoiceFields,
    *,
    source_file_id: int | None = None,
    request_id: str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> InvoiceVerification:
    """对 ``fields`` 执行一次查验并落库（同一张票始终只有一行）。

    步骤（任一步失败都在**任何写入之前**抛错并写拒绝审计）：

    1. 要求 ``invoice/verify/company`` 权限（PLAN §9），否则
       ``InvoiceVerificationDeniedError``；
    2. 校验渠道类型属于 PLAN §7.1 的三类之一，否则 ``UnrecognizedProviderError``
       （fail closed）；
    3. 计算 ``duplicate_key`` 并查找既有行；若同一张票当日已查验满
       ``DAILY_VERIFY_LIMIT`` 次 → ``DailyVerifyLimitExceeded``（**不改库、不发
       请求**）；
    4. 调用 ``provider.verify``；
    5. 渠道闸门：``VERIFIED_PASS`` 仅在 ``can_set_verified_pass(provider_type)`` 为真
       时保留，否则降级为「待人工确认」（``manual_assisted`` 与未识别来源**永不**
       产生「查验通过」）；
    6. 合并状态（``resolve_status``，保证「不通过」与「无法查验」互不覆盖）、并入
       票面、脱敏落证据、更新计数；
    7. 写成功审计（``details`` 非敏感）。

    新建行与更新行都只 ``flush``，事务边界由调用方掌握。并发插入由
    ``duplicate_key`` 唯一约束兜底：极端竞态下会以约束冲突失败（可重试），但**绝不
    会产生第二条记录**。
    """
    today_value = today if today is not None else datetime.now(UTC).date()
    now_value = now if now is not None else datetime.now(UTC)

    # 1) 授权：默认拒绝。
    if not has_permission(session, principal, *PERMISSION):
        _record_deny(
            session,
            principal,
            resource_id=None,
            attempted_action="verify",
            reason="missing_permission",
            request_id=request_id,
        )
        raise InvoiceVerificationDeniedError(
            attempted_action="verify", reason="missing_permission"
        )

    # 2) 渠道类型 fail closed。
    try:
        provider_type = coerce_provider_type(provider.provider_type)
    except Exception as exc:  # ProviderError 及任何属性异常都不得放行。
        _record_deny(
            session,
            principal,
            resource_id=None,
            attempted_action="verify",
            reason="unrecognized_provider",
            request_id=request_id,
        )
        raise UnrecognizedProviderError(
            attempted_action="verify", reason="unrecognized_provider"
        ) from exc

    duplicate_key = compute_duplicate_key(fields)
    existing = session.scalar(
        select(InvoiceVerification).where(
            InvoiceVerification.duplicate_key == duplicate_key
        )
    )

    # 3) 当日限次：达到上限即拒绝，且**不调用渠道、不产生新记录**。
    if (
        existing is not None
        and existing.last_verify_date == today_value
        and existing.daily_verify_count >= DAILY_VERIFY_LIMIT
    ):
        _record_deny(
            session,
            principal,
            resource_id=str(existing.id),
            attempted_action="verify",
            reason="daily_verify_limit_reached",
            request_id=request_id,
        )
        raise DailyVerifyLimitExceeded(
            duplicate_key=duplicate_key, limit=DAILY_VERIFY_LIMIT
        )

    # 4) 调用渠道（只读查询；适配器自身不盲目重发）。
    result = provider.verify(fields)

    # 5) 渠道闸门：只有被认可渠道能进入「查验通过」。
    incoming = result.status
    if incoming is VerificationStatus.VERIFIED_PASS and not can_set_verified_pass(
        provider_type
    ):
        incoming = VerificationStatus.PENDING_MANUAL_CONFIRMATION

    duplicate_result = classify_duplicate(
        existing.source_file_id if existing is not None else None,
        source_file_id,
    )

    if existing is None:
        row = InvoiceVerification(
            source_file_id=source_file_id,
            duplicate_key=duplicate_key,
            duplicate_check_result=duplicate_result,
            provider_type=provider_type.value,
            provider_name=provider.provider_name,
            provider_ref=result.provider_ref,
            status=incoming.value,
            error_code=result.error_code,
            reason=result.reason,
            verify_attempt_count=1,
            daily_verify_count=1,
            last_verify_date=today_value,
        )
        _apply_face(row, result.face, fields, new=True)
        session.add(row)
        session.flush()
        from_status: str | None = None
    else:
        row = existing
        from_status = row.status
        # 业务去重：同一张票出现在另一份原件上，且尚未取得渠道结论时标记「疑似重复」。
        if (
            duplicate_result == DUPLICATE_SUSPECTED
            and is_pending_status(VerificationStatus(row.status))
        ):
            incoming = VerificationStatus.SUSPECTED_DUPLICATE

        resolved = resolve_status(VerificationStatus(row.status), incoming)
        row.status = resolved.value
        # error_code/reason 只在「本次结果被采纳」时更新，保持与记录的 status 一致
        # （例如终态「查验不通过」不得被后一次超时的 error_code 覆盖）。
        if resolved is incoming:
            row.error_code = result.error_code
            row.reason = result.reason
        row.provider_type = provider_type.value
        row.provider_name = provider.provider_name
        row.provider_ref = result.provider_ref
        row.duplicate_check_result = duplicate_result
        row.verify_attempt_count = (row.verify_attempt_count or 0) + 1
        if row.last_verify_date == today_value:
            row.daily_verify_count = (row.daily_verify_count or 0) + 1
        else:
            row.daily_verify_count = 1
            row.last_verify_date = today_value
        _apply_face(row, result.face, fields, new=False)
        session.flush()

    _apply_evidence(row, result)

    # 只有取得渠道结论（非「待…」类）时才记查验时间与操作人。
    if not is_pending_status(VerificationStatus(row.status)):
        row.verified_at = now_value
        row.verified_by = principal.employee_id

    record_event(
        session,
        actor_id=principal.employee_id,
        action=VERIFY_ACTION,
        resource_type=RESOURCE_TYPE,
        resource_id=str(row.id),
        request_id=request_id,
        details=_audit_details(
            from_status=from_status,
            to_status=row.status,
            provider_type=provider_type,
            provider_name=provider.provider_name,
            error_code=result.error_code,
            duplicate_check_result=duplicate_result,
            duplicate_key=duplicate_key,
        ),
    )
    session.flush()
    return row


def record_manual_verification(
    session: Session,
    principal: Principal,
    fields: InvoiceFields,
    *,
    evidence: str | None = None,
    source_file_id: int | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
) -> InvoiceVerification:
    """**显式录入**一笔人工辅助查验结果（PLAN §7.1 第 1 类的人工录入路径）。

    ``ManualAssistedProvider`` 经 ``verify_invoice`` **永远**只产出「待人工确认」
    （该 provider 闸门语义不得改动）；本函数是授权财务人员经官方平台核验后把结果
    录入系统、留存证据的唯一入口（W0-4 §8）。它**只能被受信调用方（API 路由）显式
    调用**，不得被任何 provider 流程或自动路径触发。

    步骤（任一步失败都在**任何状态变更之前**抛错）：

    1. 要求 ``invoice/verify/company`` 权限（与 ``verify_invoice`` 同一权限，PLAN
       §9）；无权限 → 拒绝审计（``invoice.manual_verify.deny``，``details`` 只含
       ``attempted_action``/``reason``）+ ``ManualVerificationDeniedError``；
    2. 必填要素：``evidence`` 必须是非空说明字符串；操作人取自已验证主体的
       ``principal.employee_id``（**绝不**读请求体/模型输出）。缺任一项 →
       ``ManualVerificationEvidenceError``，不落任何状态变更；
    3. 按发票身份（``invoice_no`` + ``invoice_code``（可空）+ ``invoice_date``，
       ``compute_duplicate_key``）定位既有行；不存在则新建
       （``provider_type=manual_assisted``）。同一张票永远只有一行；
    4. 状态规则（遵循 ``resolve_status`` 语义）：「查验不通过」是渠道终态，人工
       录入**不得**把它静默抹掉 → 拒绝审计 + ``ManualVerificationConflictError``；
       已有「查验通过」→ 幂等（在既有行上重录，不产生第二条记录）；其余（待…/
       无法查验/疑似重复）→ 「查验通过」；
    5. 落库：``provider_type=provider_name='manual_assisted'``、``verified_at``
       取服务端时钟（可注入）、``verified_by`` 取操作人、证据结构化落
       ``evidence_json``（``channel``/``note``/``recorded_by``/``recorded_at``）并
       写内容寻址的 ``evidence_ref``；行侧「最新结果」字段按**本次采纳结论**更新
       （新建/既有行同口径：``error_code=None``、``reason`` 为
       ``manual_verification_recorded``；被取代的渠道失败信息只保留在
       ``invoice.verify`` 审计中）；``source_file_id`` 给定时在**新建**行上一并
       记录（既有行不重指原件，只按提供的新原件重算去重结论）；
    6. 写成功审计（``invoice.manual_verify``，``details`` 只含非敏感结构化值：
       ``from_status``/``to_status``/``provider_type``/``provider_name``/
       ``has_evidence``/``duplicate_check_result``/``duplicate_key`` 摘要，**不含
       票号/税号明文**）。

    只 ``flush``，事务边界由调用方掌握（与 ``verify_invoice`` 同一约定）。
    """
    now_value = now if now is not None else datetime.now(UTC)

    # 1) 授权：默认拒绝（与 verify_invoice 同一权限；PLAN §9）。
    if not has_permission(session, principal, *PERMISSION):
        _record_manual_deny(
            session,
            principal,
            resource_id=None,
            reason="missing_permission",
            request_id=request_id,
        )
        raise ManualVerificationDeniedError(
            attempted_action="manual_verify", reason="missing_permission"
        )

    # 2) 必填要素：非空证据说明 + 经核验的操作人主体。操作人只来自 principal，
    #    请求体/模型输出中的任何身份字段都不参与（PLAN §2.1）。
    note = evidence.strip() if isinstance(evidence, str) else ""
    operator_id = getattr(principal, "employee_id", None)
    if not note or not operator_id:
        raise ManualVerificationEvidenceError()

    # 3) 按票面身份定位既有行（同一张票永远只有一行）。
    duplicate_key = compute_duplicate_key(fields)
    existing = session.scalar(
        select(InvoiceVerification).where(
            InvoiceVerification.duplicate_key == duplicate_key
        )
    )

    # 4) 渠道「查验不通过」是终态：人工录入不得把它静默抹成「通过」。
    current: VerificationStatus | None = None
    if existing is not None:
        current = VerificationStatus(existing.status)
        if current is VerificationStatus.VERIFIED_FAIL:
            _record_manual_deny(
                session,
                principal,
                resource_id=str(existing.id),
                reason="verified_fail_is_terminal",
                request_id=request_id,
            )
            raise ManualVerificationConflictError(
                current_status=current.value, reason="verified_fail_is_terminal"
            )

    resolved = resolve_status(current, VerificationStatus.VERIFIED_PASS)
    if resolved is not VerificationStatus.VERIFIED_PASS:
        # 兜底（当前 ``resolve_status`` 语义下不可达）：任何将来的规则变化若让既有
        # 状态压过「查验通过」，都必须在此 fail closed，而不是静默写入非通过结论。
        _record_manual_deny(
            session,
            principal,
            resource_id=str(existing.id) if existing is not None else None,
            reason="status_conflict",
            request_id=request_id,
        )
        raise ManualVerificationConflictError(
            current_status=current.value if current is not None else "none",
            reason="status_conflict",
        )

    # 5) 落库：既有行原地更新 / 新建行；录入不调用渠道，故不改动渠道计数列。
    if existing is None:
        duplicate_result = classify_duplicate(None, source_file_id)
        row = InvoiceVerification(
            source_file_id=source_file_id,
            # 提交要素（与 verify_invoice 的新建行同一口径：只落已确认的票面身份）。
            invoice_no=fields.invoice_no,
            invoice_date=fields.invoice_date,
            invoice_code=fields.invoice_code,
            invoice_type=fields.invoice_kind,
            duplicate_key=duplicate_key,
            duplicate_check_result=duplicate_result,
            provider_type=MANUAL_PROVIDER_TYPE,
            provider_name=MANUAL_PROVIDER_NAME,
            status=VerificationStatus.VERIFIED_PASS.value,
            reason="manual_verification_recorded",
            verify_attempt_count=0,
            daily_verify_count=0,
        )
        session.add(row)
        session.flush()
        from_status: str | None = None
    else:
        row = existing
        from_status = row.status
        # 去重结论只在提供新原件时重算：缺省不抹掉既有「疑似重复」标记。
        if source_file_id is not None:
            duplicate_result = classify_duplicate(row.source_file_id, source_file_id)
            row.duplicate_check_result = duplicate_result
        else:
            duplicate_result = row.duplicate_check_result
        row.status = resolved.value
        # 「最新结果」与新建行同口径：本次采纳的是人工录入结论，被取代的渠道失败
        # 信息不得继续挂在行上（历史保留在 ``invoice.verify`` 审计 ``details`` 中）。
        row.error_code = None
        row.reason = "manual_verification_recorded"
        row.provider_type = MANUAL_PROVIDER_TYPE
        row.provider_name = MANUAL_PROVIDER_NAME
        session.flush()

    evidence_payload: dict[str, Any] = {
        "channel": MANUAL_PROVIDER_TYPE,
        "note": note,
        "recorded_by": operator_id,
        "recorded_at": now_value.isoformat(),
    }
    row.evidence_json = evidence_payload
    row.evidence_ref = evidence_ref_for(evidence_payload)
    row.verified_at = now_value
    row.verified_by = operator_id

    record_event(
        session,
        actor_id=operator_id,
        action=MANUAL_VERIFY_ACTION,
        resource_type=RESOURCE_TYPE,
        resource_id=str(row.id),
        request_id=request_id,
        details={
            "from_status": from_status,
            "to_status": row.status,
            "provider_type": MANUAL_PROVIDER_TYPE,
            "provider_name": MANUAL_PROVIDER_NAME,
            "has_evidence": True,
            "duplicate_check_result": duplicate_result,
            "duplicate_key": duplicate_key,
        },
    )
    session.flush()
    return row
