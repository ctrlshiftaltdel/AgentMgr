"""报销服务层：创建、明细、提交、撤回、审批、退回、拒绝、财务复核与付款登记。

本模块把 ``templates/PLAN.md`` §5.2（报销模块）与 §9（权限模型）、以及
``docs/PROJECT-CHARTER.md`` 强制原则 5/6 落成**确定性的后端服务**：

* **状态迁移只经状态机校验**：每次迁移先调用
  ``app.expense.state_machine.next_status``，非法迁移抛
  ``InvalidTransitionError`` 且业务状态不变（强制原则 6：审批状态转换必须由
  确定性程序驱动）；
* **每次调用都在后端执行授权判定**：创建/提交/撤回要求本人，审批/退回/拒绝走
  ``can_approve``（自审一律拒绝），财务复核要求 ``expense_claim/finance_review/
  company``，付款要求 ``payment/record/company``（强制原则 5 / PLAN §9
  「后端对每次调用执行授权」）；
* **金额一律走 money 模块**：固定精度 ``Decimal``，``float`` 被
  ``app.expense.money`` 直接拒绝（强制原则 6 / PLAN §8「金额采用固定精度
  decimal，不用浮点数保存财务金额」）；
* **授权失败或非法迁移时业务状态不变，但必须写一条拒绝审计事件**
  （``action='expense_claim.deny'``，``details`` 含 ``attempted_action`` 与
  ``reason``），对齐 PLAN §12「权限拒绝可审计」。

事务边界由调用方控制：本模块所有函数只 ``flush``，**绝不 ``commit``**（与
``app.audit.service.record_event`` 一致），使业务变更与审计写入处于同一事务，
避免「业务成功但审计丢失」或反之。

审计事件约定（成功迁移）：

* ``action`` = ``expense_claim.<动作>``（如 ``expense_claim.submit``）；
* ``resource_type`` = ``'expense_claim'``；``resource_id`` = ``str(claim.id)``；
* ``actor_id`` = ``principal.employee_id``；``request_id`` 原样透传；
* ``details`` 含 ``from_status`` / ``to_status``。

拒绝审计事件：``action='expense_claim.deny'``，``details`` 含
``attempted_action`` 与 ``reason``（**不含敏感字段原文**，见
``app.audit.service``）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.expense.models import (
    ExpenseClaim,
    ExpenseLine,
    ReimbursementPayment,
)
from app.expense.money import (
    MAX_AMOUNT,
    MIN_AMOUNT,
    quantize,
    sum_amounts,
    to_decimal,
)
from app.expense.state_machine import InvalidTransitionError, next_status
from app.identity.authorization import Principal, can_approve, has_permission

__all__ = [
    "DESCRIPTION_MAX_LENGTH",
    "DESCRIPTION_MIN_LENGTH",
    "AuthorizationDeniedError",
    "ClaimNotFoundError",
    "ExpenseServiceError",
    "InputValidationError",
    "ValidationError",
    "add_line",
    "approve",
    "create_claim",
    "finance_review",
    "record_payment",
    "reject",
    "return_for_fix",
    "submit",
    "withdraw",
]

#: 审计 ``resource_type``：所有报销服务事件都挂在这个资源类型上。
RESOURCE_TYPE = "expense_claim"

#: 拒绝审计动作名（PLAN §12「权限拒绝可审计」）。
DENY_ACTION = "expense_claim.deny"

#: 生成 claim_no 的序列名（由迁移 ``0006_expense_claim_no_seq`` 创建）。
CLAIM_NO_SEQUENCE = "expense_claim_no_seq"

#: 明细说明（``description``）的合法长度区间。
#:
#: 数据库列是 ``text``，没有长度约束；这里在服务层收紧，避免把无上限的
#: 任意长文本写进业务表。上限值会写进错误信息，便于调用方修正输入。
DESCRIPTION_MIN_LENGTH = 1
DESCRIPTION_MAX_LENGTH = 500


class ExpenseServiceError(Exception):
    """报销服务层错误基类。"""


class ClaimNotFoundError(ExpenseServiceError):
    """指定的报销单不存在。"""

    def __init__(self, claim_id: int) -> None:
        self.claim_id = claim_id
        super().__init__(f"expense claim {claim_id!r} not found")


class AuthorizationDeniedError(ExpenseServiceError):
    """授权判定失败：调用主体无权执行该动作（默认拒绝）。

    授权失败时业务状态**保持不变**，并已写入一条拒绝审计事件；本异常携带
    ``attempted_action`` 与 ``reason``，便于调用方与审计日志定位。
    """

    def __init__(
        self,
        *,
        attempted_action: str,
        reason: str,
        claim_id: int | None = None,
    ) -> None:
        self.attempted_action = attempted_action
        self.reason = reason
        self.claim_id = claim_id
        super().__init__(
            f"authorization denied for {attempted_action!r}: {reason}"
        )


class ValidationError(ExpenseServiceError):
    """业务前置条件不满足（如空明细提交、非 draft 加明细、金额非正）。

    与授权失败一样，业务状态**保持不变**，并已写入一条拒绝审计事件。
    """

    def __init__(
        self,
        *,
        attempted_action: str,
        reason: str,
        claim_id: int | None = None,
    ) -> None:
        self.attempted_action = attempted_action
        self.reason = reason
        self.claim_id = claim_id
        super().__init__(
            f"validation failed for {attempted_action!r}: {reason}"
        )


class InputValidationError(ValidationError):
    """输入校验失败，且错误信息**必须**带出具体边界值。

    ``ValidationError`` 的响应文案是固定的（``app.api.errors`` 只返回受控静态
    文案），无法告诉调用方「金额上限是多少」「说明最多多少字符」。凡是需要把
    边界写进 422 响应的校验（金额范围、``description`` 长度）都用这个子类，
    ``message`` 由服务层用常量拼装，**不含任何输入原文**，因此可以安全外传。
    """

    def __init__(
        self,
        *,
        attempted_action: str,
        reason: str,
        message: str,
        claim_id: int | None = None,
    ) -> None:
        self.message = message
        super().__init__(
            attempted_action=attempted_action, reason=reason, claim_id=claim_id
        )


# --- 内部辅助 -----------------------------------------------------------------


def _load_claim(session: Session, claim_id: int) -> ExpenseClaim:
    """按主键加载报销单；不存在抛 ``ClaimNotFoundError``。"""
    claim = session.get(ExpenseClaim, claim_id)
    if claim is None:
        raise ClaimNotFoundError(claim_id)
    return claim


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

    这是 PLAN §12「权限拒绝可审计」的落点：授权失败、非法迁移与业务前置条件
    不满足都经此记录，``details`` 只含结构化的 ``attempted_action`` 与
    ``reason``，不含任何敏感字段原文。
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


def _validate_amount(
    session: Session,
    principal: Principal,
    *,
    value: Decimal | int | str,
    field: str,
    attempted_action: str,
    claim_id: int,
    request_id: str | None,
) -> Decimal:
    """把 ``value`` 量化为 2 位小数并校验范围，返回可直接入库的 ``Decimal``。

    越界（负值、超过 ``NUMERIC(18,2)`` 上限、非有限值，或位数多到无法量化）时
    先写一条拒绝审计事件，再抛 :class:`InputValidationError`（映射为 422），
    因此这类输入**绝不会**落到数据库 CHECK 违反上、也就不会变成 500。

    ``float``/``bool`` 仍由 ``money.to_decimal`` 以 ``TypeError`` 拒绝（在任何
    写入之前），保持「金额必须是固定精度 decimal」的不变量。
    """
    try:
        normalized: Decimal | None = quantize(to_decimal(value))
    except InvalidOperation:  # 位数太多，无法量化到 2 位小数 → 视同越界
        normalized = None
    if (
        normalized is None
        or not normalized.is_finite()
        or normalized < MIN_AMOUNT
        or normalized > MAX_AMOUNT
    ):
        _record_deny(
            session,
            principal,
            resource_id=str(claim_id),
            attempted_action=attempted_action,
            reason=f"{field}_out_of_range",
            request_id=request_id,
        )
        raise InputValidationError(
            attempted_action=attempted_action,
            reason=f"{field}_out_of_range",
            message=(
                f"{field} must be between {MIN_AMOUNT} and {MAX_AMOUNT} "
                "(NUMERIC(18,2))"
            ),
            claim_id=claim_id,
        )
    return normalized


def _validate_description(
    session: Session,
    principal: Principal,
    *,
    description: str,
    attempted_action: str,
    claim_id: int,
    request_id: str | None,
) -> None:
    """校验 ``description`` 长度落在服务层允许区间内，否则抛 422 类错误。

    数据库列是 ``text``（无长度约束），因此这是唯一的长度闸门；上限写进错误
    信息。空字符串同样被拒绝（``DESCRIPTION_MIN_LENGTH``）。
    """
    length = len(description)
    if length < DESCRIPTION_MIN_LENGTH or length > DESCRIPTION_MAX_LENGTH:
        _record_deny(
            session,
            principal,
            resource_id=str(claim_id),
            attempted_action=attempted_action,
            reason="description_out_of_range",
            request_id=request_id,
        )
        raise InputValidationError(
            attempted_action=attempted_action,
            reason="description_out_of_range",
            message=(
                "description must be between "
                f"{DESCRIPTION_MIN_LENGTH} and {DESCRIPTION_MAX_LENGTH} characters"
            ),
            claim_id=claim_id,
        )


def _record_success(
    session: Session,
    principal: Principal,
    claim: ExpenseClaim,
    *,
    action: str,
    from_status: str | None,
    to_status: str,
    request_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    """写入一条成功审计事件，``details`` 始终含 ``from_status``/``to_status``。"""
    merged: dict[str, Any] = {"from_status": from_status, "to_status": to_status}
    if details:
        merged.update(details)
    record_event(
        session,
        actor_id=principal.employee_id,
        action=action,
        resource_type=RESOURCE_TYPE,
        resource_id=str(claim.id),
        request_id=request_id,
        details=merged,
    )


def _validate_transition(
    session: Session,
    principal: Principal,
    claim: ExpenseClaim,
    *,
    action: str,
    request_id: str | None,
) -> str:
    """经状态机校验迁移，返回目标状态。

    非法迁移时先写拒绝审计事件，再原样抛出 ``InvalidTransitionError``（业务
    状态不变，因为 ``next_status`` 是纯函数、不触碰对象）。
    """
    try:
        return next_status(claim.status, action)
    except InvalidTransitionError:
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action=action,
            reason="invalid_transition",
            request_id=request_id,
        )
        raise


def _require_owner(
    session: Session,
    principal: Principal,
    claim: ExpenseClaim,
    *,
    attempted_action: str,
    request_id: str | None,
) -> None:
    """要求调用主体是报销单本人；否则写拒绝审计并抛 ``AuthorizationDeniedError``。"""
    if claim.employee_id != principal.employee_id:
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action=attempted_action,
            reason="not_claim_owner",
            request_id=request_id,
        )
        raise AuthorizationDeniedError(
            attempted_action=attempted_action,
            reason="not_claim_owner",
            claim_id=claim.id,
        )


def _require_can_approve(
    session: Session,
    principal: Principal,
    claim: ExpenseClaim,
    *,
    attempted_action: str,
    request_id: str | None,
) -> None:
    """要求 ``can_approve`` 为真（含自审一律拒绝）；否则写拒绝审计并抛错。"""
    if not can_approve(session, principal, claim.employee_id):
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action=attempted_action,
            reason="cannot_approve",
            request_id=request_id,
        )
        raise AuthorizationDeniedError(
            attempted_action=attempted_action,
            reason="cannot_approve",
            claim_id=claim.id,
        )


def _require_permission(
    session: Session,
    principal: Principal,
    claim: ExpenseClaim,
    *,
    resource: str,
    action: str,
    scope: str,
    attempted_action: str,
    request_id: str | None,
) -> None:
    """要求 ``(resource, action, scope)`` 权限；否则写拒绝审计并抛错。"""
    if not has_permission(session, principal, resource, action, scope):
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action=attempted_action,
            reason="missing_permission",
            request_id=request_id,
        )
        raise AuthorizationDeniedError(
            attempted_action=attempted_action,
            reason="missing_permission",
            claim_id=claim.id,
        )


def _next_claim_no(session: Session) -> str:
    """从序列取下一个编号并格式化为 ``EC-########``。"""
    value = session.execute(
        text(f"SELECT nextval('{CLAIM_NO_SEQUENCE}')")
    ).scalar_one()
    return f"EC-{value:08d}"


# --- 服务函数 -----------------------------------------------------------------


def create_claim(
    session: Session,
    principal: Principal,
    *,
    currency: str = "CNY",
    request_id: str | None = None,
) -> ExpenseClaim:
    """创建一张新的报销草稿（``status='draft'``，``total_amount=0.00``）。

    需要 ``expense_claim/create/own`` 权限；``claim_no`` 由数据库序列
    ``expense_claim_no_seq`` 生成，格式 ``EC-{nextval:08d}``。只 ``flush``。
    """
    if not has_permission(session, principal, "expense_claim", "create", "own"):
        _record_deny(
            session,
            principal,
            resource_id=None,
            attempted_action="create",
            reason="missing_permission",
            request_id=request_id,
        )
        raise AuthorizationDeniedError(
            attempted_action="create", reason="missing_permission"
        )

    claim_no = _next_claim_no(session)
    claim = ExpenseClaim(
        claim_no=claim_no,
        employee_id=principal.employee_id,
        currency=currency,
        total_amount=Decimal("0.00"),
        status="draft",
    )
    session.add(claim)
    session.flush()

    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.create",
        from_status=None,
        to_status="draft",
        request_id=request_id,
        details={"claim_no": claim_no, "currency": currency},
    )
    return claim


def add_line(
    session: Session,
    principal: Principal,
    claim_id: int,
    *,
    expense_date: date,
    category_id: int,
    description: str,
    amount: Decimal,
    tax_amount: Decimal = Decimal("0.00"),
    cost_center_id: int | None = None,
    request_id: str | None = None,
) -> ExpenseLine:
    """向报销草稿追加一条明细，并用 ``money.sum_amounts`` 重算 ``total_amount``。

    仅限报销人本人且仅限 ``draft``；``amount``/``tax_amount`` 必须可由
    ``Decimal`` 精确表示——``float`` 会被 ``money.to_decimal`` 以 ``TypeError``
    拒绝；金额在写入前先量化到 2 位小数（``ROUND_HALF_UP``）并校验落在
    ``NUMERIC(18,2)`` 的 ``[0.00, 9999999999999999.99]`` 内，``description``
    长度落在 ``[DESCRIPTION_MIN_LENGTH, DESCRIPTION_MAX_LENGTH]`` 内。任何一项
    不满足都在**任何写入之前**抛出 ``InputValidationError``（映射为 422），
    **不产生部分写入**，也不会落到数据库 CHECK 违反上产生 500。
    """
    claim = _load_claim(session, claim_id)
    _require_owner(
        session,
        principal,
        claim,
        attempted_action="add_line",
        request_id=request_id,
    )

    if claim.status != "draft":
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action="add_line",
            reason="claim_not_draft",
            request_id=request_id,
        )
        raise ValidationError(
            attempted_action="add_line",
            reason="claim_not_draft",
            claim_id=claim.id,
        )

    # 输入校验先于任何写入：越界金额 / 超长或空说明在此被拒绝，不会留下半条明细。
    amount_value = _validate_amount(
        session,
        principal,
        value=amount,
        field="amount",
        attempted_action="add_line",
        claim_id=claim.id,
        request_id=request_id,
    )
    tax_value = _validate_amount(
        session,
        principal,
        value=tax_amount,
        field="tax_amount",
        attempted_action="add_line",
        claim_id=claim.id,
        request_id=request_id,
    )
    _validate_description(
        session,
        principal,
        description=description,
        attempted_action="add_line",
        claim_id=claim.id,
        request_id=request_id,
    )

    line = ExpenseLine(
        claim_id=claim.id,
        expense_date=expense_date,
        category_id=category_id,
        description=description,
        amount=amount_value,
        tax_amount=tax_value,
        cost_center_id=cost_center_id,
    )
    session.add(line)
    session.flush()

    amounts = session.scalars(
        select(ExpenseLine.amount).where(ExpenseLine.claim_id == claim.id)
    )
    claim.total_amount = sum_amounts(amounts)

    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.add_line",
        from_status="draft",
        to_status="draft",
        request_id=request_id,
        details={
            "line_id": line.id,
            "amount": str(amount_value),
            "tax_amount": str(tax_value),
            "total_amount": str(claim.total_amount),
        },
    )
    return line


def submit(
    session: Session,
    principal: Principal,
    claim_id: int,
    request_id: str | None = None,
) -> ExpenseClaim:
    """提交报销：``draft``/``withdrawn`` → ``submitted``。

    仅限本人；要求至少一条明细且 ``total_amount > 0``；写入 ``submitted_at``。
    存在挂账发票（W1-23）时，先同步各行的查验状态（live 读
    ``invoice_verifications``），任一行为 ``verified_fail`` 或未知状态则拒绝
    （``ValidationError`` + 拒绝审计），状态原样保留。
    """
    claim = _load_claim(session, claim_id)
    _require_owner(
        session,
        principal,
        claim,
        attempted_action="submit",
        request_id=request_id,
    )

    # 先经状态机校验：非法迁移（如 paid 再 submit）抛 InvalidTransitionError。
    to_status = _validate_transition(
        session, principal, claim, action="submit", request_id=request_id
    )

    line_count = session.scalar(
        select(ExpenseLine.id).where(ExpenseLine.claim_id == claim.id).limit(1)
    )
    amounts = session.scalars(
        select(ExpenseLine.amount).where(ExpenseLine.claim_id == claim.id)
    )
    total = sum_amounts(amounts)
    if line_count is None or total <= 0:
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action="submit",
            reason="empty_claim",
            request_id=request_id,
        )
        raise ValidationError(
            attempted_action="submit", reason="empty_claim", claim_id=claim.id
        )

    # --- W1-23: 挂账发票校验（对既有流程的唯一一处修改，必须最小） ---------
    # 先对全部挂账行做 sync（live 读 invoice_verifications），再拒绝
    # verified_fail 或不为七状态之一的行——只写拒绝审计并抛既有 ValidationError
    # （app/api 不在本任务允许路径内，避免将来映射为 500）。函数级导入避免
    # service <-> invoices 模块级循环依赖。
    from app.expense.invoices import _validate_claim_invoices_for_submit

    _validate_claim_invoices_for_submit(
        session, principal, claim, request_id=request_id
    )

    claim.status = to_status
    claim.submitted_at = datetime.now(UTC)

    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.submit",
        from_status="draft" if to_status == "submitted" else "withdrawn",
        to_status=to_status,
        request_id=request_id,
        details={"total_amount": str(total)},
    )
    return claim


def withdraw(
    session: Session,
    principal: Principal,
    claim_id: int,
    request_id: str | None = None,
) -> ExpenseClaim:
    """撤回报销：``submitted`` → ``withdrawn``。仅限本人。"""
    claim = _load_claim(session, claim_id)
    _require_owner(
        session,
        principal,
        claim,
        attempted_action="withdraw",
        request_id=request_id,
    )
    from_status = claim.status
    to_status = _validate_transition(
        session, principal, claim, action="withdraw", request_id=request_id
    )
    claim.status = to_status
    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.withdraw",
        from_status=from_status,
        to_status=to_status,
        request_id=request_id,
    )
    return claim


def approve(
    session: Session,
    principal: Principal,
    claim_id: int,
    request_id: str | None = None,
) -> ExpenseClaim:
    """审批报销：``submitted`` → ``approved``。

    必须 ``can_approve`` 为真——拥有 ``expense_claim/approve/team`` 权限、报销人
    在团队可见范围内、且**不是本人**（自审一律拒绝，PLAN §9）。
    """
    claim = _load_claim(session, claim_id)
    _require_can_approve(
        session,
        principal,
        claim,
        attempted_action="approve",
        request_id=request_id,
    )
    from_status = claim.status
    to_status = _validate_transition(
        session, principal, claim, action="approve", request_id=request_id
    )
    claim.status = to_status
    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.approve",
        from_status=from_status,
        to_status=to_status,
        request_id=request_id,
    )
    return claim


def return_for_fix(
    session: Session,
    principal: Principal,
    claim_id: int,
    request_id: str | None = None,
) -> ExpenseClaim:
    """退回补件：``submitted`` 或 ``approved`` → ``draft``。需 ``can_approve``。"""
    claim = _load_claim(session, claim_id)
    _require_can_approve(
        session,
        principal,
        claim,
        attempted_action="return",
        request_id=request_id,
    )
    from_status = claim.status
    to_status = _validate_transition(
        session, principal, claim, action="return", request_id=request_id
    )
    claim.status = to_status
    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.return",
        from_status=from_status,
        to_status=to_status,
        request_id=request_id,
    )
    return claim


def reject(
    session: Session,
    principal: Principal,
    claim_id: int,
    request_id: str | None = None,
) -> ExpenseClaim:
    """拒绝报销：``submitted`` 或 ``approved`` → ``rejected``。需 ``can_approve``。"""
    claim = _load_claim(session, claim_id)
    _require_can_approve(
        session,
        principal,
        claim,
        attempted_action="reject",
        request_id=request_id,
    )
    from_status = claim.status
    to_status = _validate_transition(
        session, principal, claim, action="reject", request_id=request_id
    )
    claim.status = to_status
    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.reject",
        from_status=from_status,
        to_status=to_status,
        request_id=request_id,
    )
    return claim


def finance_review(
    session: Session,
    principal: Principal,
    claim_id: int,
    request_id: str | None = None,
) -> ExpenseClaim:
    """财务复核：``approved`` → ``finance_reviewed``。

    需要 ``expense_claim/finance_review/company`` 权限。
    """
    claim = _load_claim(session, claim_id)
    _require_permission(
        session,
        principal,
        claim,
        resource="expense_claim",
        action="finance_review",
        scope="company",
        attempted_action="finance_review",
        request_id=request_id,
    )
    from_status = claim.status
    to_status = _validate_transition(
        session, principal, claim, action="finance_review", request_id=request_id
    )
    claim.status = to_status
    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.finance_review",
        from_status=from_status,
        to_status=to_status,
        request_id=request_id,
    )
    return claim


def record_payment(
    session: Session,
    principal: Principal,
    claim_id: int,
    *,
    amount: Decimal,
    payment_ref: str | None = None,
    request_id: str | None = None,
) -> ReimbursementPayment:
    """登记付款：``finance_reviewed`` → ``paid``，并创建一条付款记录。

    需要 ``payment/record/company`` 权限；``amount`` 必须 > 0（``float`` 被
    ``money.to_decimal`` 以 ``TypeError`` 拒绝）。付款记录的 ``status='paid'``、
    ``paid_at=now()``、``currency`` 取报销单的币种。
    """
    claim = _load_claim(session, claim_id)
    _require_permission(
        session,
        principal,
        claim,
        resource="payment",
        action="record",
        scope="company",
        attempted_action="record_payment",
        request_id=request_id,
    )

    # 金额校验先于任何写入：float 在此被拒绝，越界值被量化+范围校验拒绝，
    # 不会留下半条付款记录。
    amount_value = _validate_amount(
        session,
        principal,
        value=amount,
        field="amount",
        attempted_action="record_payment",
        claim_id=claim.id,
        request_id=request_id,
    )
    if amount_value <= MIN_AMOUNT:
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action="record_payment",
            reason="non_positive_amount",
            request_id=request_id,
        )
        raise ValidationError(
            attempted_action="record_payment",
            reason="non_positive_amount",
            claim_id=claim.id,
        )

    from_status = claim.status
    to_status = _validate_transition(
        session, principal, claim, action="pay", request_id=request_id
    )

    payment = ReimbursementPayment(
        claim_id=claim.id,
        amount=amount_value,
        currency=claim.currency,
        payment_ref=payment_ref,
        status="paid",
        paid_at=datetime.now(UTC),
    )
    session.add(payment)
    session.flush()

    claim.status = to_status

    _record_success(
        session,
        principal,
        claim,
        action="expense_claim.record_payment",
        from_status=from_status,
        to_status=to_status,
        request_id=request_id,
        details={
            "payment_id": payment.id,
            "amount": str(amount_value),
            "currency": claim.currency,
        },
    )
    return payment
