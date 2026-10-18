"""报销流程 HTTP 端点：创建、明细、状态迁移、付款、列表、导出与发票挂账。

本模块是报销域（``app.expense.service``）的**薄 HTTP 适配层**：它只负责
「解析请求 → 调用服务层 → 序列化响应」，**绝不自行重写授权或状态迁移逻辑**。
所有授权判定与状态迁移都由服务层 / ``app.identity.authorization`` 执行
（``templates/PLAN.md`` §9「后端对每次调用执行授权」）。

关键约束：

* **主体只来自受验证的头部**：每个端点的 ``principal`` 都来自
  ``app.api.deps.get_principal``（内部令牌 + ``X-Employee-Id``）。请求体、查询
  参数与模型输出中的任何 ``employee_id`` **一律忽略**（PLAN §2.1）。
* **事务边界**：服务层只 ``flush``，本层在每次调用结束时 ``commit``。业务域
  异常（``ExpenseServiceError`` / ``InvalidTransitionError``）发生时，服务层
  已写入一条拒绝审计事件，本层在 ``commit`` 持久化该审计后再向上抛出，交由
  ``app.api.errors`` 映射为统一错误码。

列表过滤规则（模块级不变量，实现见 :func:`_widest_read_scope` 与
:func:`list_claims`）
--------------------------------------------------------------------------------

``GET /claims`` 按主体在 ``expense_claim/read`` 下**拥有的最宽数据范围**过滤：
按 ``company > team > authorized > own`` 的优先级取第一个命中的 scope，再用
``app.identity.authorization.visible_employee_ids`` 解析出该 scope 下可见的员工
ID 集合，只返回 ``employee_id`` 落在该集合内的报销单。若主体**没有任何**
``expense_claim/read`` 权限，则返回**空列表**（不报错，也绝不泄露他人数据）。

单条读取（``GET /claims/{id}``）与两个导出端点复用**同一套**读取授权：先按上述
规则解析最宽 scope，再判定该报销单的 ``employee_id`` 是否可见，不可见即 403。
``GET /claims/{id}/invoices`` 复用**同一套**读取授权（同一资源、同一口径）。

发票挂账端点（``POST`` / ``GET /claims/{claim_id}/invoices``）
--------------------------------------------------------------------------------

两个端点同样是 ``app.expense.invoices``（任务 W1-23）的**薄 HTTP 适配层**：挂账
的幂等键（``duplicate_key``）、状态规则（仅本人 + 仅 ``draft``、状态一律由服务端
从查验记录推导）、授权与审计全部由服务层执行，本层**不重复实现**：

* ``POST`` 请求体只承载票面要素与可选原件引用，**没有**任何可指定挂账状态的字
  段；同一张票在同一报销单内重复挂账按服务层语义**先查后拒**（422，不产生第二
  行，唯一约束 ``uq_expense_invoices_claim_duplicate`` 兜底并发）；
* 响应**脱敏**：票面身份只以单向摘要 ``duplicate_key`` 出现，**不回显**票号 /
  发票代码明文（与 ``app.api.invoices`` 的响应口径及服务层审计口径一致）；
* ``GET`` 先做与 ``GET /claims/{id}`` 相同的读取授权（不可见 → 403 + 拒绝审计），
  再按服务层函数列出该单的挂账行（按 ``id`` 升序）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, get_request_id
from app.api.errors import ApiError
from app.audit.service import record_event
from app.db.models.organization import Company, Employee
from app.db.session import get_session
from app.documents.export import render_claim_pdf, render_claim_xlsx
from app.documents.storage import PDF_MIME, XLSX_MIME
from app.expense.invoices import attach_invoice, list_claim_invoices
from app.expense.models import (
    ExpenseClaim,
    ExpenseInvoice,
    ExpenseLine,
    ReimbursementPayment,
)
from app.expense.service import (
    AuthorizationDeniedError,
    ClaimNotFoundError,
    ExpenseServiceError,
    add_line,
    approve,
    create_claim,
    finance_review,
    record_payment,
    reject,
    return_for_fix,
    submit,
    withdraw,
)
from app.expense.state_machine import InvalidTransitionError
from app.identity.authorization import (
    Principal,
    has_permission,
    visible_employee_ids,
)
from app.integrations.invoice_verification import InvoiceFields, parse_invoice_date

__all__ = ["router"]

router = APIRouter(tags=["claims"])

#: 与 ``app.api.errors`` 的受控文案保持一致：本层只把输入校验失败映射为既有的
#: 422 ``validation_error``，不新增错误映射语义，也不回显输入原文。
_MESSAGE_VALIDATION = "request failed business validation"

#: 读取范围由宽到窄的优先级（最宽者胜出）。
_READ_SCOPES_WIDEST_FIRST: tuple[str, ...] = ("company", "team", "authorized", "own")

#: 读取被拒时写入拒绝审计的动作名（PLAN §12「权限拒绝可审计」）。
_DENY_ACTION = "expense_claim.deny"


# --- 请求体模型 ---------------------------------------------------------------


class ClaimCreateRequest(BaseModel):
    """创建报销单的请求体。

    ``employee_id`` 字段**仅为兼容性而声明，且被显式忽略**：报销单归属一律取
    自已验证的 ``principal``（``X-Employee-Id`` 头），绝不采信请求体。
    """

    currency: str = "CNY"
    # 显式忽略：身份只能来自 X-Employee-Id 头（PLAN §2.1 / W0-2 §4）。
    employee_id: int | None = None


class LineCreateRequest(BaseModel):
    """追加一条报销明细的请求体（金额为固定精度 ``Decimal``）。"""

    expense_date: date
    category_id: int
    description: str
    amount: Decimal
    tax_amount: Decimal = Decimal("0.00")
    cost_center_id: int | None = None


class PaymentCreateRequest(BaseModel):
    """登记付款的请求体。"""

    amount: Decimal
    payment_ref: str | None = None


class InvoiceAttachRequest(BaseModel):
    """把一张发票挂到报销单草稿上的请求体（票面要素 + 可选原件引用）。

    * ``invoice_no``（发票号码）与 ``invoice_date``（开票日期，``YYYY-MM-DD`` 或
      ``YYYYMMDD``）必填，规范性由契约层 ``parse_invoice_date`` 判定；
    * ``invoice_code``（发票代码，全电票为空）/ ``invoice_kind``（票种代码）/
      ``amount``（金额，固定精度 ``Decimal``，禁止浮点）/ ``source_file_id``
      （原件引用，可空）可选；
    * **没有**任何可指定挂账状态的字段——``verification_status`` 一律由服务层从
      查验记录推导（W1-23 硬边界）；
    * ``employee_id`` **仅为兼容性而声明，且被显式忽略**：挂账归属与审计主体
      一律取自已验证的 ``principal``（``X-Employee-Id`` 头），请求体里的身份字段
      不能改变它（PLAN §2.1 / W0-2 §4）。
    """

    invoice_no: str
    invoice_date: str
    invoice_code: str | None = None
    invoice_kind: str | None = None
    amount: Decimal | None = None
    source_file_id: int | None = None
    # 显式忽略：身份只能来自 X-Employee-Id 头（PLAN §2.1 / W0-2 §4）。
    employee_id: int | None = None


# --- 事务与序列化辅助 ---------------------------------------------------------


@contextmanager
def _unit_of_work(session: Session) -> Iterator[None]:
    """统一事务边界：成功或业务域拒绝都 ``commit``，未预期异常则 ``rollback``。

    业务域异常（``ExpenseServiceError`` / ``InvalidTransitionError``）意味着服务
    层已写入一条拒绝审计事件但业务状态未变；此时 ``commit`` 只持久化该审计，
    随后原样抛出交由错误处理器映射状态码。
    """
    try:
        yield
    except (ExpenseServiceError, InvalidTransitionError):
        # 服务层已写拒绝审计：持久化它，业务状态不受影响。
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()


def _iso(value) -> str | None:  # type: ignore[no-untyped-def]
    """把 ``datetime`` 序列化为 ISO 字符串；``None`` 原样返回。"""
    return None if value is None else value.isoformat()


def _claim_dict(claim: ExpenseClaim) -> dict[str, object]:
    """序列化报销单。金额用 ``str`` 保留固定精度，绝不经过浮点。"""
    return {
        "id": claim.id,
        "claim_no": claim.claim_no,
        "employee_id": claim.employee_id,
        "currency": claim.currency,
        "total_amount": str(claim.total_amount),
        "status": claim.status,
        "submitted_at": _iso(claim.submitted_at),
        "created_at": _iso(claim.created_at),
        "updated_at": _iso(claim.updated_at),
    }


def _line_dict(line: ExpenseLine) -> dict[str, object]:
    """序列化报销明细。金额用 ``str`` 保留固定精度。"""
    return {
        "id": line.id,
        "claim_id": line.claim_id,
        "expense_date": str(line.expense_date),
        "category_id": line.category_id,
        "description": line.description,
        "amount": str(line.amount),
        "tax_amount": str(line.tax_amount),
        "cost_center_id": line.cost_center_id,
    }


def _payment_dict(payment: ReimbursementPayment) -> dict[str, object]:
    """序列化付款记录。金额用 ``str`` 保留固定精度。"""
    return {
        "id": payment.id,
        "claim_id": payment.claim_id,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "payment_ref": payment.payment_ref,
        "status": payment.status,
        "paid_at": _iso(payment.paid_at),
    }


def _invoice_dict(invoice: ExpenseInvoice) -> dict[str, object]:
    """序列化一张挂账行（**脱敏**：不回显票号 / 发票代码明文）。

    票面身份只以单向摘要 ``duplicate_key`` 出现（与查验域
    ``compute_duplicate_key`` 及服务层审计口径一致）；票面金额用 ``str`` 保留
    固定精度，绝不经过浮点；``invoice_fields_json`` 中未列出的键（含税号一类
    渠道字段）一律不外传。
    """
    snapshot = invoice.invoice_fields_json or {}
    return {
        "id": invoice.id,
        "claim_id": invoice.claim_id,
        "verification_status": invoice.verification_status,
        "duplicate_key": invoice.duplicate_key,
        "suspected_duplicate": bool(invoice.suspected_duplicate),
        "source_file_id": invoice.source_file_id,
        "invoice_date": snapshot.get("invoice_date"),
        "invoice_kind": snapshot.get("invoice_kind"),
        "amount": snapshot.get("amount"),
        "created_at": _iso(invoice.created_at),
    }


def _load_claim(session: Session, claim_id: int) -> ExpenseClaim:
    """按主键加载报销单；不存在抛 ``ClaimNotFoundError``（映射为 404）。"""
    claim = session.get(ExpenseClaim, claim_id)
    if claim is None:
        raise ClaimNotFoundError(claim_id)
    return claim


def _load_lines(session: Session, claim_id: int) -> list[ExpenseLine]:
    """加载报销单的全部明细（按 id 升序）。"""
    return list(
        session.scalars(
            select(ExpenseLine)
            .where(ExpenseLine.claim_id == claim_id)
            .order_by(ExpenseLine.id)
        )
    )


# --- 读取授权（列表 / 单条 / 导出共用） ---------------------------------------


def _widest_read_scope(session: Session, principal: Principal) -> str | None:
    """返回主体在 ``expense_claim/read`` 下拥有的最宽 scope；无则 ``None``。

    优先级 ``company > team > authorized > own``：一旦命中更宽的 scope 即返回，
    不再考虑更窄者。
    """
    for scope in _READ_SCOPES_WIDEST_FIRST:
        if has_permission(session, principal, "expense_claim", "read", scope):
            return scope
    return None


def _visible_ids_for_read(
    session: Session, principal: Principal
) -> set[int] | None:
    """返回主体可读取的员工 ID 集合；无任何读取权限时返回 ``None``。

    ``None`` 表示「无读取权限」，调用方据此对列表返回空、对单条返回 403。
    """
    scope = _widest_read_scope(session, principal)
    if scope is None:
        return None
    return visible_employee_ids(session, principal, scope)


def _authorize_read(
    session: Session,
    principal: Principal,
    claim: ExpenseClaim,
    *,
    request_id: str | None,
) -> None:
    """读取授权：报销单归属必须落在主体最宽 read scope 的可见集合内。

    不可见（含无任何读取权限）时写一条拒绝审计并抛 ``AuthorizationDeniedError``
    （映射为 403）。**先授权、后读取**，绝不先返回数据再判断。
    """
    visible = _visible_ids_for_read(session, principal)
    if visible is None or claim.employee_id not in visible:
        reason = "missing_permission" if visible is None else "not_in_scope"
        record_event(
            session,
            actor_id=principal.employee_id,
            action=_DENY_ACTION,
            resource_type="expense_claim",
            resource_id=str(claim.id),
            request_id=request_id,
            details={"attempted_action": "read", "reason": reason},
        )
        raise AuthorizationDeniedError(
            attempted_action="read", reason=reason, claim_id=claim.id
        )


# --- 路由：创建 / 列表 / 单条 -------------------------------------------------


@router.post("/claims", status_code=201)
def create_claim_endpoint(
    body: ClaimCreateRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """创建报销草稿（201）。归属取自已验证主体，请求体的 ``employee_id`` 被忽略。"""
    with _unit_of_work(session):
        claim = create_claim(
            session, principal, currency=body.currency, request_id=request_id
        )
    return _claim_dict(claim)


@router.get("/claims")
def list_claims(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> list[dict[str, object]]:
    """列出主体可见的报销单。

    取主体在 ``expense_claim/read`` 下最宽的 scope，用
    ``visible_employee_ids`` 过滤；**无任何 read 权限返回空列表**（不报错、不
    泄露）。返回按 ``id`` 升序。
    """
    visible = _visible_ids_for_read(session, principal)
    if not visible:
        return []
    claims = session.scalars(
        select(ExpenseClaim)
        .where(ExpenseClaim.employee_id.in_(visible))
        .order_by(ExpenseClaim.id)
    ).all()
    return [_claim_dict(claim) for claim in claims]


@router.get("/claims/{claim_id}")
def get_claim(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """读取单张报销单（先做与导出端点相同的读取授权）。"""
    claim = _load_claim(session, claim_id)
    with _unit_of_work(session):
        _authorize_read(session, principal, claim, request_id=request_id)
    return _claim_dict(claim)


# --- 路由：明细 ---------------------------------------------------------------


@router.post("/claims/{claim_id}/lines", status_code=201)
def add_line_endpoint(
    claim_id: int,
    body: LineCreateRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """向草稿追加一条明细（201）。仅限本人且仅限 ``draft``。"""
    with _unit_of_work(session):
        line = add_line(
            session,
            principal,
            claim_id,
            expense_date=body.expense_date,
            category_id=body.category_id,
            description=body.description,
            amount=body.amount,
            tax_amount=body.tax_amount,
            cost_center_id=body.cost_center_id,
            request_id=request_id,
        )
    return _line_dict(line)


# --- 路由：状态迁移 -----------------------------------------------------------


def _transition(  # noqa: PLR0913 - explicit service dispatch, one param per concern
    session: Session,
    principal: Principal,
    claim_id: int,
    *,
    action: str,
    request_id: str | None,
) -> ExpenseClaim:
    """把 ``action`` 分派到对应的服务层函数，返回迁移后的报销单。

    只做分派，不含任何授权/迁移逻辑（那些在服务层）。
    """
    if action == "submit":
        return submit(session, principal, claim_id, request_id=request_id)
    if action == "withdraw":
        return withdraw(session, principal, claim_id, request_id=request_id)
    if action == "approve":
        return approve(session, principal, claim_id, request_id=request_id)
    if action == "return":
        return return_for_fix(session, principal, claim_id, request_id=request_id)
    if action == "reject":
        return reject(session, principal, claim_id, request_id=request_id)
    if action == "finance_review":
        return finance_review(session, principal, claim_id, request_id=request_id)
    raise ValueError(f"unknown transition action: {action!r}")  # pragma: no cover


@router.post("/claims/{claim_id}/submit")
def submit_claim(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """提交报销（仅本人；要求至少一条明细）。"""
    with _unit_of_work(session):
        claim = _transition(
            session, principal, claim_id, action="submit", request_id=request_id
        )
    return _claim_dict(claim)


@router.post("/claims/{claim_id}/withdraw")
def withdraw_claim(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """撤回报销（仅本人；``submitted`` → ``withdrawn``）。"""
    with _unit_of_work(session):
        claim = _transition(
            session, principal, claim_id, action="withdraw", request_id=request_id
        )
    return _claim_dict(claim)


@router.post("/claims/{claim_id}/approve")
def approve_claim(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """主管审批（``can_approve``：团队范围、非本人）。"""
    with _unit_of_work(session):
        claim = _transition(
            session, principal, claim_id, action="approve", request_id=request_id
        )
    return _claim_dict(claim)


@router.post("/claims/{claim_id}/return")
def return_claim(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """退回补件（``can_approve``；回到 ``draft``）。"""
    with _unit_of_work(session):
        claim = _transition(
            session, principal, claim_id, action="return", request_id=request_id
        )
    return _claim_dict(claim)


@router.post("/claims/{claim_id}/reject")
def reject_claim(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """拒绝报销（``can_approve``；到 ``rejected``）。"""
    with _unit_of_work(session):
        claim = _transition(
            session, principal, claim_id, action="reject", request_id=request_id
        )
    return _claim_dict(claim)


@router.post("/claims/{claim_id}/finance-review")
def finance_review_claim(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """财务复核（需 ``expense_claim/finance_review/company``）。"""
    with _unit_of_work(session):
        claim = _transition(
            session,
            principal,
            claim_id,
            action="finance_review",
            request_id=request_id,
        )
    return _claim_dict(claim)


# --- 路由：付款 ---------------------------------------------------------------


@router.post("/claims/{claim_id}/payments", status_code=201)
def record_payment_endpoint(
    claim_id: int,
    body: PaymentCreateRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """登记付款（201）。需 ``payment/record/company``；``finance_reviewed`` → ``paid``。"""
    with _unit_of_work(session):
        payment = record_payment(
            session,
            principal,
            claim_id,
            amount=body.amount,
            payment_ref=body.payment_ref,
            request_id=request_id,
        )
    return _payment_dict(payment)


# --- 路由：发票挂账 -----------------------------------------------------------


def _invoice_fields_from_body(body: InvoiceAttachRequest) -> InvoiceFields:
    """把请求体解析为 ``InvoiceFields``；格式非法（如开票日期）→ 422。

    发票身份的规范形式（``YYYY-MM-DD`` / ``YYYYMMDD``）以契约层
    ``parse_invoice_date`` 为**唯一**判定来源；校验失败只回受控文案，绝不回显
    输入原文。金额的固定精度与 ``float`` 拒绝由 ``InvoiceFields`` 内部经
    ``app.expense.money`` 执行，本层不重复实现——但**必须**把量化阶段可能抛出的
    ``InvalidOperation``（例如位数多到无法量化到 2 位小数的数字串）一并映射为
    既有的 422，绝不落到 500。非有限值（``NaN`` / ``Infinity``）由请求体模型
    直接拒绝（``finite_number``），同样映射为 422。
    """
    parsed_date = parse_invoice_date(body.invoice_date)
    if parsed_date is None:
        raise ApiError(422, "validation_error", _MESSAGE_VALIDATION)
    try:
        return InvoiceFields(
            invoice_no=body.invoice_no,
            invoice_date=parsed_date,
            invoice_code=body.invoice_code,
            invoice_kind=body.invoice_kind,
            amount=body.amount,
        )
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ApiError(422, "validation_error", _MESSAGE_VALIDATION) from exc


@router.post("/claims/{claim_id}/invoices", status_code=201)
def attach_invoice_endpoint(
    claim_id: int,
    body: InvoiceAttachRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """把一张发票挂到报销单草稿上（201），返回**脱敏**的挂账行摘要。

    授权、幂等键与状态规则全部由 ``app.expense.invoices.attach_invoice`` 执行
    （本层不重复实现）：

    * 仅报销人本人 + 仅 ``draft``；否则 403 ``authorization_denied`` /
      422 ``validation_error``（均含拒绝审计）；
    * ``duplicate_key`` 为幂等键：同一张票在同一报销单内重复挂账按服务层语义
      **先查后拒**（422，不产生第二行；并发由唯一约束兜底）；
    * ``verification_status`` 一律由服务端从查验记录推导，**请求体无法指定**；
    * 跨报销单出现同一张票时，本行与既有行均置 ``suspected_duplicate``。

    响应不回显票号 / 发票代码明文（票面身份只以单向摘要 ``duplicate_key``
    出现）；``source_file_id`` 原样透传，其存在性由外键保证，归属校验属服务层
    语义，本层不改动。
    """
    fields = _invoice_fields_from_body(body)
    with _unit_of_work(session):
        row = attach_invoice(
            session,
            principal,
            claim_id,
            fields=fields,
            source_file_id=body.source_file_id,
            request_id=request_id,
        )
    return _invoice_dict(row)


@router.get("/claims/{claim_id}/invoices")
def list_claim_invoices_endpoint(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> list[dict[str, object]]:
    """列出某报销单的全部挂账行（只读，按 ``id`` 升序，**脱敏**）。

    先做与 ``GET /claims/{claim_id}`` **完全相同**的读取授权（主体在
    ``expense_claim/read`` 下最宽 scope 的可见集合内必须包含该报销单归属；否则
    403 ``authorization_denied`` + 拒绝审计），再调用服务层
    ``list_claim_invoices`` 取行——本层不自行改写可见性判定，也不回显票号 /
    发票代码明文。
    """
    claim = _load_claim(session, claim_id)
    with _unit_of_work(session):
        _authorize_read(session, principal, claim, request_id=request_id)
    rows = list_claim_invoices(session, claim)
    return [_invoice_dict(row) for row in rows]


# --- 路由：导出（先授权、后渲染） ---------------------------------------------


def _render_context(
    session: Session, claim: ExpenseClaim
) -> tuple[str, str]:
    """返回导出所需的 ``(employee_name, company_name)``。

    ``employees`` 表当前没有姓名字段，故以 ``employee_no`` 作为员工标识。
    """
    owner = session.get(Employee, claim.employee_id)
    employee_name = owner.employee_no if owner is not None else ""
    company_name = ""
    if owner is not None:
        company = session.get(Company, owner.company_id)
        if company is not None:
            company_name = company.name
    return employee_name, company_name


@router.get("/claims/{claim_id}/export/pdf")
def export_claim_pdf(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """导出报销单 PDF（先做与 GET 单条相同的读取授权，再调用纯渲染函数）。"""
    claim = _load_claim(session, claim_id)
    with _unit_of_work(session):
        _authorize_read(session, principal, claim, request_id=request_id)
    lines = _load_lines(session, claim.id)
    employee_name, company_name = _render_context(session, claim)
    data = render_claim_pdf(
        claim, lines, employee_name=employee_name, company_name=company_name
    )
    return Response(content=data, media_type=PDF_MIME)


@router.get("/claims/{claim_id}/export/xlsx")
def export_claim_xlsx(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """导出报销明细 XLSX（先做与 GET 单条相同的读取授权，再调用纯渲染函数）。"""
    claim = _load_claim(session, claim_id)
    with _unit_of_work(session):
        _authorize_read(session, principal, claim, request_id=request_id)
    lines = _load_lines(session, claim.id)
    data = render_claim_xlsx(claim, lines)
    return Response(content=data, media_type=XLSX_MIME)
