"""确认草稿 → 正式报销记录写入路径（Issue #56）。

``app.intake.pipeline`` 只写**草稿区**：``confirm_draft`` 仅把
``extraction_drafts.review_status`` 置为 ``confirmed`` 并写审计，**绝不写任何正式
业务表**（W1-26 的硬边界）。本模块补上那条缺失的最后一跳——把**已人工确认**的草稿
转成正式报销记录，对齐 ``templates/PLAN.md`` §4.2「人工确认 → 后端校验 → 写入正式
业务表」。

口径（所有者 2026-10-18 定稿，见 Issue #56 评论）：

1. **字段映射**：``expense_date`` 默认取票面开票日期（``invoice_date``）；确认时可
   经 ``expense_date=`` 参数人工覆盖。映射表是 :data:`FIELD_SOURCES`，显式声明并
   由测试锁定。金额/税额取票面（``amount`` / ``tax_amount``）。**费用类别必须由
   调用方显式提供**（``category_id``）——**未提供即 fail closed，绝不猜**。
2. **写入粒度**：生成「报销单草稿（``status='draft'``）+ 明细行」。**一张草稿 =
   一条明细行**（当前草稿载荷是扁平字段表，没有多行结构；多行载荷结构属后续任务）。
   不挂到既有报销单。
3. **发票挂账**：草稿含**足够票面字段**（``invoice_no`` + ``invoice_date``）时**同
   事务**创建 ``expense_invoices`` 行，复用 W1-23 的 :func:`app.expense.invoices.attach_invoice`
   语义（``duplicate_key`` 幂等、跨单疑似重复标记、状态只从查验记录推导）。字段不足
   时**不挂账**，并在审计 ``details`` 里显式记录 ``invoice_attached=false`` 与
   ``invoice_skip_reason``（不静默、不臆造票面）。

硬边界：

* **人工确认仍是进入正式记录的唯一路径**：本函数**只接受 ``review_status ==
  'confirmed'`` 的草稿**，不自动确认、不代确认；其它状态一律 fail closed。
* **授权不绕过**：要求 ``expense_claim/create/own`` 权限，且草稿所属受理作业的
  ``submitted_by`` 必须等于调用主体——即「谁提交的资料，谁写回自己的报销单」，防止
  把他人草稿写成自己名下的正式记录。授权/校验失败写拒绝审计并**不产生任何写入**。
* **同事务**：本模块只 ``add`` / ``flush``，**绝不 ``commit``**（与
  ``app.audit.service.record_event``、``app.expense.service`` 一致）——「报销单 +
  明细 + 挂账 + 审计」要么一起成功，要么由调用方回滚，不留半成品。
* **幂等**：以**草稿**为幂等键。进入写入前先对草稿行加 ``FOR UPDATE`` 行锁（把同一
  草稿的并发写回串行化），再查本模块此前写入的 ``extraction_draft.writeback`` 审计
  事件；命中则**原样返回既有结果**（``created=False``），不重复建单。**不新增迁移、
  不新增列**（因此并发幂等依赖行锁 + 审计查询，而非数据库唯一约束；见
  :func:`write_back_confirmed_draft` 的说明）。
* **低置信度字段带标记**：草稿置信度低于阈值的字段名记入写回审计
  ``low_confidence_fields``（**只记字段名**），草稿记录本身仍保留逐字段置信度；报销
  单以 ``draft`` 状态落库，继续走既有的提交/审批流程由人工完善。当前 schema 没有可
  承载「单据上可见标记」的列，因此标记落在审计轨迹上（如需单据级可见标记，需
  schema 变更并另行授权）。
* **审计非敏感**：``details`` 只含标识、字段名、状态与金额字符串；**不含**票号/税号
  明文、附件原文或识别全文（票面身份只以单向摘要 ``duplicate_key`` 出现）。

本模块不新增 HTTP 端点、不新增依赖、不做迁移。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.audit.service import record_event
from app.expense.invoices import attach_invoice
from app.expense.models import (
    CostCenter,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseInvoice,
    ExpenseLine,
)
from app.expense.money import MAX_AMOUNT, MIN_AMOUNT, quantize, to_decimal
from app.expense.service import (
    AuthorizationDeniedError,
    ExpenseServiceError,
    ValidationError,
    add_line,
    create_claim,
)
from app.identity.authorization import Principal, has_permission
from app.intake.models import ExtractionDraft, IntakeJob
from app.intake.parsing import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    _is_missing_value,
    required_fields_for,
)
from app.integrations.invoice_verification import InvoiceFields, parse_invoice_date

__all__ = [
    "DEFAULT_DESCRIPTION_TEMPLATE",
    "FIELD_SOURCES",
    "INVOICE_IDENTITY_FIELDS",
    "INVOICE_SOURCE_FIELDS",
    "SUPPORTED_BUSINESS_TYPE",
    "WRITEBACK_ACTION",
    "WRITEBACK_DENY_ACTION",
    "DraftNotFoundError",
    "InvoiceMapping",
    "WritebackResult",
    "draft_field_value",
    "invoice_fields_from_draft",
    "low_confidence_fields",
    "parse_money_value",
    "resolve_expense_date",
    "write_back_confirmed_draft",
]

#: 本写入路径当前**唯一**支持的 ``business_type``：报销单。
SUPPORTED_BUSINESS_TYPE = "expense_claim"

#: 写回成功审计的动作名（``resource_type='extraction_draft'``）。
WRITEBACK_ACTION = "extraction_draft.writeback"

#: 写回被拒审计的动作名（``details`` 含 ``attempted_action`` 与 ``reason``）。
WRITEBACK_DENY_ACTION = "extraction_draft.writeback_deny"

#: 审计资源类型：写回事件挂在**草稿**上（幂等键也是草稿）。
DRAFT_RESOURCE_TYPE = "extraction_draft"

#: 业务必填字段 → 草稿字段名候选（**按顺序取第一个非缺失值**）。
#:
#: 定稿口径：``expense_date`` 默认取票面开票日期 ``invoice_date``；草稿若已按业务
#: 字段名给出 ``expense_date``（人工录入口径），作为**回退来源**使用。人工覆盖走
#: :func:`write_back_confirmed_draft` 的 ``expense_date=`` 参数，优先于两者。
#: ``amount`` / ``tax_amount`` 取票面同名键（百度适配器统一为 2 位小数字符串）。
FIELD_SOURCES: Mapping[str, tuple[str, ...]] = {
    "expense_date": ("invoice_date", "expense_date"),
    "amount": ("amount",),
    "tax_amount": ("tax_amount",),
}

#: 挂账所需的**票面身份字段**：两者齐备才建 ``expense_invoices`` 行。
#: ``compute_duplicate_key`` 只依赖 ``invoice_code`` + ``invoice_no`` +
#: ``invoice_date``，而 ``invoice_code`` 对全电发票可为空，因此身份下限是这两项。
INVOICE_IDENTITY_FIELDS: tuple[str, ...] = ("invoice_no", "invoice_date")

#: 允许进入 ``InvoiceFields`` 的草稿字段白名单（**显式枚举**，不做通配透传）。
#:
#: 刻意**不含** ``seller_tax_id`` / ``buyer_tax_id``（B 类敏感字段，挂账快照也不保存
#: 税号，见 ``app.expense.invoices._invoice_fields_snapshot``）与
#: ``buyer_name`` / ``seller_name`` / ``invoice_type``（前者是主体名称，后者是 OCR 的
#: 票种分类名，**不是** ``InvoiceFields.invoice_kind`` 的票种代码，映射即为臆造）。
INVOICE_SOURCE_FIELDS: tuple[str, ...] = (
    "invoice_no",
    "invoice_date",
    "invoice_code",
    "amount",
)

#: 未显式提供 ``description`` 时使用的确定性摘要模板。
#:
#: 只由**非敏感标识**拼装（草稿 ID、业务类型、费用日期），不含票号/税号/主体名，也不
#: 含任何附件原文。
DEFAULT_DESCRIPTION_TEMPLATE = (
    "受理草稿 #{draft_id} 转报销（{business_type}，费用日期 {expense_date}）"
)


class DraftNotFoundError(ExpenseServiceError):
    """指定的解析草稿不存在（与 ``ClaimNotFoundError`` 同形，携带 ``draft_id``）。"""

    def __init__(self, draft_id: int) -> None:
        self.draft_id = draft_id
        super().__init__(f"extraction draft {draft_id!r} not found")


@dataclass(frozen=True)
class InvoiceMapping:
    """草稿 → ``InvoiceFields`` 的映射结果。

    * ``fields``：可挂账的票面要素（``None`` 表示不挂账）；
    * ``skip_reason``：不挂账时的机器可读原因（``missing_invoice_fields`` /
      ``invalid_invoice_fields``），可挂账时为 ``None``。
    """

    fields: InvoiceFields | None
    skip_reason: str | None


@dataclass(frozen=True)
class WritebackResult:
    """一次写回的结果。

    * ``claim``：新建或既有（幂等命中）的报销单草稿；
    * ``lines``：该报销单当前的全部明细行（按 id 升序）；
    * ``invoice``：本次挂账的发票行；未挂账为 ``None``；
    * ``created``：``True`` 表示本次真的建单，``False`` 表示幂等命中既有结果；
    * ``expense_date_source``：费用日期来源（``override`` / ``invoice_date`` /
      ``expense_date``）；
    * ``low_confidence_fields``：低于阈值的草稿字段名（只记字段名）；
    * ``invoice_skip_reason``：未挂账原因（已挂账为 ``None``）。
    """

    claim: ExpenseClaim
    lines: tuple[ExpenseLine, ...]
    invoice: ExpenseInvoice | None
    created: bool
    expense_date_source: str
    low_confidence_fields: tuple[str, ...]
    invoice_skip_reason: str | None


# --- 纯逻辑：字段映射（无数据库，可独立单测） ---------------------------------


def draft_field_value(
    extracted: Mapping[str, Any], names: Iterable[str]
) -> tuple[Any, str] | None:
    """按 ``names`` 顺序返回**第一个非缺失**的草稿取值及其来源字段名。

    缺失判据与草稿裁决同口径（``app.intake.parsing._is_missing_value``：``None`` /
    空白字符串 / 空容器）。全部缺失返回 ``None``（调用方据此 fail closed）。
    """
    for name in names:
        if name in extracted and not _is_missing_value(extracted[name]):
            return extracted[name], name
    return None


def resolve_expense_date(
    extracted: Mapping[str, Any],
    *,
    override: date | str | None = None,
) -> tuple[date, str] | None:
    """解析费用日期，返回 ``(date, 来源字段名或 'override')``；无法解析返回 ``None``。

    优先级（定稿口径）：

    1. ``override``（确认时的人工覆盖参数）；
    2. 票面开票日期 ``invoice_date``；
    3. 草稿的业务字段名 ``expense_date``（人工录入口径的回退）。

    只接受 ``date`` / ``datetime`` / ``YYYY-MM-DD`` / ``YYYYMMDD``（复用查验域的
    :func:`parse_invoice_date`）；其余一律 ``None``——**不猜测日期格式**。
    """
    if override is not None:
        parsed = parse_invoice_date(override)
        return None if parsed is None else (parsed, "override")

    found = draft_field_value(extracted, FIELD_SOURCES["expense_date"])
    if found is None:
        return None
    value, source = found
    parsed = parse_invoice_date(value)
    return None if parsed is None else (parsed, source)


def parse_money_value(value: Any) -> Decimal | None:
    """把草稿里的金额取值转成 2 位定点 ``Decimal``；不可用返回 ``None``。

    ``float`` / ``bool`` 被 ``app.expense.money.to_decimal`` 拒绝（财务金额必须固定
    精度），非有限值与无法量化的取值同样返回 ``None``——调用方据此 fail closed，
    **绝不把二进制浮点写进正式金额列**。
    """
    try:
        normalized = quantize(to_decimal(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return normalized if normalized.is_finite() else None


def low_confidence_fields(
    confidence: Mapping[str, Any] | None,
    *,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> tuple[str, ...]:
    """返回置信度低于 ``threshold`` 的字段名（排序、去重）；非数值取值忽略。

    与 ``app.intake.parsing.decide_draft`` 同一阈值口径；只返回**字段名**，不返回值。
    """
    names: set[str] = set()
    for name, value in (confidence or {}).items():
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if score < threshold:
            names.add(str(name))
    return tuple(sorted(names))


def invoice_fields_from_draft(extracted: Mapping[str, Any]) -> InvoiceMapping:
    """把草稿的票面字段映射为 ``InvoiceFields``（纯函数，不触碰数据库）。

    * 票面身份字段（``invoice_no`` + ``invoice_date``）齐备且可构造 → 返回
      ``InvoiceMapping(fields=..., skip_reason=None)``；
    * 缺身份字段 → ``skip_reason='missing_invoice_fields'``；
    * 身份字段在但构造失败（日期/金额形态不可用）→
      ``skip_reason='invalid_invoice_fields'``。

    只透传 :data:`INVOICE_SOURCE_FIELDS` 白名单字段；税号与主体名称**不进入**挂账
    入参。
    """
    for name in INVOICE_IDENTITY_FIELDS:
        if draft_field_value(extracted, (name,)) is None:
            return InvoiceMapping(fields=None, skip_reason="missing_invoice_fields")

    kwargs: dict[str, Any] = {}
    for name in INVOICE_SOURCE_FIELDS:
        found = draft_field_value(extracted, (name,))
        if found is not None:
            kwargs[name] = found[0]
    try:
        return InvoiceMapping(fields=InvoiceFields(**kwargs), skip_reason=None)
    except (TypeError, ValueError):
        return InvoiceMapping(fields=None, skip_reason="invalid_invoice_fields")


# --- 内部辅助 -----------------------------------------------------------------


def _record_deny(
    session: Session,
    principal: Principal,
    *,
    draft_id: int,
    reason: str,
    request_id: str | None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """写入一条写回拒绝审计（不 commit；``details`` 只含非敏感结构化值）。"""
    payload: dict[str, Any] = {"attempted_action": "writeback", "reason": reason}
    if details:
        payload.update(details)
    record_event(
        session,
        actor_id=principal.employee_id,
        action=WRITEBACK_DENY_ACTION,
        resource_type=DRAFT_RESOURCE_TYPE,
        resource_id=str(draft_id),
        request_id=request_id,
        details=payload,
    )


def _deny_and_raise(
    session: Session,
    principal: Principal,
    *,
    draft_id: int,
    reason: str,
    request_id: str | None,
    details: Mapping[str, Any] | None = None,
    authorization: bool = False,
) -> NoReturn:
    """写拒绝审计后抛错（授权类 → ``AuthorizationDeniedError``，其余 → ``ValidationError``）。"""
    _record_deny(
        session,
        principal,
        draft_id=draft_id,
        reason=reason,
        request_id=request_id,
        details=details,
    )
    if authorization:
        raise AuthorizationDeniedError(attempted_action="writeback", reason=reason)
    raise ValidationError(attempted_action="writeback", reason=reason)


def _writeback_event(
    session: Session,
    principal: Principal,
    *,
    draft: ExtractionDraft,
    claim: ExpenseClaim,
    line_count: int,
    category_id: int,
    expense_date_source: str,
    low_confidence: tuple[str, ...],
    field_names: Iterable[str],
    invoice_attached: bool,
    invoice_skip_reason: str | None,
    request_id: str | None,
) -> None:
    """写入写回成功审计（``details`` 非敏感：标识、字段名、状态与金额字符串）。"""
    record_event(
        session,
        actor_id=principal.employee_id,
        action=WRITEBACK_ACTION,
        resource_type=DRAFT_RESOURCE_TYPE,
        resource_id=str(draft.id),
        request_id=request_id,
        details={
            "intake_job_id": draft.intake_job_id,
            "business_type": draft.business_type,
            "source_file_id": draft.source_file_id,
            "claim_id": claim.id,
            "claim_no": claim.claim_no,
            "line_count": line_count,
            "total_amount": str(claim.total_amount),
            "category_id": category_id,
            "expense_date_source": expense_date_source,
            "low_confidence_fields": list(low_confidence),
            "field_names": sorted(field_names),
            "invoice_attached": invoice_attached,
            "invoice_skip_reason": invoice_skip_reason,
        },
    )


def _existing_result(
    session: Session, claim: ExpenseClaim, details: Mapping[str, Any]
) -> WritebackResult:
    """幂等命中：按既有报销单重建 :class:`WritebackResult`（不写任何数据）。"""
    lines = tuple(
        session.scalars(
            select(ExpenseLine)
            .where(ExpenseLine.claim_id == claim.id)
            .order_by(ExpenseLine.id)
        )
    )
    invoice = session.scalar(
        select(ExpenseInvoice)
        .where(ExpenseInvoice.claim_id == claim.id)
        .order_by(ExpenseInvoice.id)
        .limit(1)
    )
    return WritebackResult(
        claim=claim,
        lines=lines,
        invoice=invoice,
        created=False,
        expense_date_source=str(details.get("expense_date_source") or "unknown"),
        low_confidence_fields=tuple(details.get("low_confidence_fields") or ()),
        invoice_skip_reason=details.get("invoice_skip_reason"),
    )


def _prior_writeback(
    session: Session, draft_id: int
) -> Mapping[str, Any] | None:
    """返回该草稿此前成功写回的审计 ``details``；没有则 ``None``。

    以本模块写入的 ``extraction_draft.writeback`` 事件为幂等凭据（``audit_events``
    只增不改，因此该记录一旦存在即代表「已建过单」）。
    """
    event = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.resource_type == DRAFT_RESOURCE_TYPE,
            AuditEvent.resource_id == str(draft_id),
            AuditEvent.action == WRITEBACK_ACTION,
        )
        .order_by(AuditEvent.id)
        .limit(1)
    )
    if event is None:
        return None
    return event.details or {}


# --- 服务入口 -----------------------------------------------------------------


def write_back_confirmed_draft(
    session: Session,
    principal: Principal,
    draft_id: int,
    *,
    category_id: int,
    expense_date: date | str | None = None,
    description: str | None = None,
    cost_center_id: int | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    request_id: str | None = None,
) -> WritebackResult:
    """把**已确认**的草稿写成报销单草稿 + 明细行（+ 发票挂账），并写审计。

    步骤（**任何一步不满足都在写入之前抛错并写拒绝审计**）：

    1. 取草稿并加 ``FOR UPDATE`` 行锁（串行化同一草稿的并发写回）；
    2. **授权**：``expense_claim/create/own`` 权限 + 受理作业 ``submitted_by`` 等于
       调用主体（他人草稿不得写成自己名下的正式记录）；
    3. **幂等**：命中既有写回审计 → 原样返回既有结果（``created=False``）；
    4. **结构校验**（复用草稿裁决/必填口径，缺关键字段 fail closed）：业务类型为
       ``expense_claim``、草稿 ``review_status='confirmed'``、``category_id`` 显式
       提供且属于本公司、业务必填字段（:func:`required_fields_for`）可解析、日期与
       金额形态合法且金额为正；
    5. **同事务写入**：``create_claim``（``draft``）→ ``add_line`` → 票面字段足够时
       ``attach_invoice``（W1-23 语义）→ 写回审计。本函数**不 commit**。

    参数：

    * ``category_id``：**必填**。费用类别由调用方显式映射；未提供或不属于调用主体的
      公司 → fail closed（**不猜类别**）。
    * ``expense_date``：可选人工覆盖（``date`` 或 ``YYYY-MM-DD``/``YYYYMMDD``）；
      缺省取票面 ``invoice_date``，再回退草稿 ``expense_date``。
    * ``description``：可选明细摘要；缺省按 :data:`DEFAULT_DESCRIPTION_TEMPLATE`
      生成确定性摘要（空白字符串视同未提供）。
    * ``cost_center_id``：可选成本中心（必须存在且属于调用主体的公司）。

    幂等键是**草稿**：重复调用不会重复建单，但**第二次调用的参数不生效**（返回既有
    结果）。并发下由草稿行锁串行化；本实现不新增唯一约束/迁移，因此**跨进程的
    幂等依赖行锁**（``FOR UPDATE``）而非数据库约束——若将来需要约束级保证，须走
    迁移并另行授权。
    """
    draft = session.get(ExtractionDraft, draft_id, with_for_update=True)
    if draft is None:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft_id,
            reason="draft_not_found",
            request_id=request_id,
        )

    job = session.get(IntakeJob, draft.intake_job_id)
    if job is None:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="intake_job_missing",
            request_id=request_id,
        )

    # --- 1. 授权（不绕过） ----------------------------------------------------
    if not has_permission(session, principal, "expense_claim", "create", "own"):
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="missing_permission",
            request_id=request_id,
            authorization=True,
        )
    if job.submitted_by != principal.employee_id:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="not_draft_owner",
            request_id=request_id,
            authorization=True,
        )

    # --- 2. 幂等（以草稿为键） ------------------------------------------------
    prior = _prior_writeback(session, draft.id)
    if prior is not None:
        prior_claim_id = prior.get("claim_id")
        existing = (
            session.get(ExpenseClaim, int(prior_claim_id))
            if prior_claim_id is not None
            else None
        )
        if existing is None:
            _deny_and_raise(
                session,
                principal,
                draft_id=draft.id,
                reason="writeback_state_inconsistent",
                request_id=request_id,
            )
        return _existing_result(session, existing, prior)

    # --- 3. 结构校验（fail closed，先于任何写入） ------------------------------
    extracted = dict(draft.extracted_json or {})
    confidence = dict(draft.confidence_json or {})

    if draft.business_type != SUPPORTED_BUSINESS_TYPE:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="unsupported_business_type",
            request_id=request_id,
            details={"business_type": draft.business_type},
        )

    if draft.review_status != "confirmed":
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="draft_not_confirmed",
            request_id=request_id,
            details={"review_status": draft.review_status},
        )

    if isinstance(category_id, bool) or not isinstance(category_id, int):
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="category_not_provided",
            request_id=request_id,
        )

    required = required_fields_for(draft.business_type)
    missing: list[str] = []
    for field in required:
        if field == "expense_date" and expense_date is not None:
            continue  # 人工覆盖即视为已提供
        if draft_field_value(extracted, FIELD_SOURCES.get(field, (field,))) is None:
            missing.append(field)
    if missing:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="missing_required_fields",
            request_id=request_id,
            details={"missing_fields": sorted(missing)},
        )

    resolved_date = resolve_expense_date(extracted, override=expense_date)
    if resolved_date is None:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="invalid_expense_date",
            request_id=request_id,
            details={"field_names": sorted(extracted)},
        )
    expense_date_value, expense_date_source = resolved_date

    amount_found = draft_field_value(extracted, FIELD_SOURCES["amount"])
    amount_value = None if amount_found is None else parse_money_value(amount_found[0])
    if amount_value is None or not (MIN_AMOUNT < amount_value <= MAX_AMOUNT):
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="invalid_amount",
            request_id=request_id,
            details={"field_names": sorted(extracted)},
        )

    tax_found = draft_field_value(extracted, FIELD_SOURCES["tax_amount"])
    tax_value = (
        Decimal("0.00") if tax_found is None else parse_money_value(tax_found[0])
    )
    if tax_value is None or not (MIN_AMOUNT <= tax_value <= MAX_AMOUNT):
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="invalid_tax_amount",
            request_id=request_id,
        )

    if description is not None and not isinstance(description, str):
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="invalid_description",
            request_id=request_id,
        )

    category = session.get(ExpenseCategory, category_id)
    if category is None:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="category_not_found",
            request_id=request_id,
            details={"category_id": category_id},
        )
    if category.company_id != principal.company_id:
        _deny_and_raise(
            session,
            principal,
            draft_id=draft.id,
            reason="category_company_mismatch",
            request_id=request_id,
            details={"category_id": category_id},
        )

    if cost_center_id is not None:
        cost_center = session.get(CostCenter, cost_center_id)
        if cost_center is None:
            _deny_and_raise(
                session,
                principal,
                draft_id=draft.id,
                reason="cost_center_not_found",
                request_id=request_id,
                details={"cost_center_id": cost_center_id},
            )
        if cost_center.company_id != principal.company_id:
            _deny_and_raise(
                session,
                principal,
                draft_id=draft.id,
                reason="cost_center_company_mismatch",
                request_id=request_id,
                details={"cost_center_id": cost_center_id},
            )

    # --- 4. 同事务写入 --------------------------------------------------------
    resolved_description = (
        description.strip()
        if isinstance(description, str) and description.strip()
        else DEFAULT_DESCRIPTION_TEMPLATE.format(
            draft_id=draft.id,
            business_type=draft.business_type,
            expense_date=expense_date_value.isoformat(),
        )
    )

    claim = create_claim(session, principal, request_id=request_id)
    line = add_line(
        session,
        principal,
        claim.id,
        expense_date=expense_date_value,
        category_id=category.id,
        description=resolved_description,
        amount=amount_value,
        tax_amount=tax_value,
        cost_center_id=cost_center_id,
        request_id=request_id,
    )

    mapping = invoice_fields_from_draft(extracted)
    invoice: ExpenseInvoice | None = None
    if mapping.fields is not None:
        invoice = attach_invoice(
            session,
            principal,
            claim.id,
            fields=mapping.fields,
            source_file_id=draft.source_file_id,
            request_id=request_id,
        )

    low_confidence = low_confidence_fields(
        confidence, threshold=confidence_threshold
    )
    _writeback_event(
        session,
        principal,
        draft=draft,
        claim=claim,
        line_count=1,
        category_id=category.id,
        expense_date_source=expense_date_source,
        low_confidence=low_confidence,
        field_names=extracted,
        invoice_attached=invoice is not None,
        invoice_skip_reason=mapping.skip_reason,
        request_id=request_id,
    )

    return WritebackResult(
        claim=claim,
        lines=(line,),
        invoice=invoice,
        created=True,
        expense_date_source=expense_date_source,
        low_confidence_fields=low_confidence,
        invoice_skip_reason=mapping.skip_reason,
    )
