"""最小化 Web 管理后台：服务端渲染（FastAPI + Jinja2）。

本模块是 ``templates/PLAN.md`` §3「Web 管理界面：主要用于审核、查询、权限、配置
和审计」与 §10 阶段 1「最小化 Web 管理后台」在 API 层的落点。它**只做**三件事：

1. **认证编排**：登录 / 登出（凭据校验与会话签发委托给 ``app.api.admin_auth``）；
2. **页面渲染**：仪表盘、报销单列表与详情、员工与角色、渠道绑定、审计日志；
3. **权限闸门**：每个页面与每个动作都调用 ``app.identity.authorization`` 的权限
   函数；无权限时渲染 **403 页面**且不泄露任何数据、不改变任何业务状态。

关键约束（不得违反）：

* **授权逐页/逐动作生效**：登录只回答「是谁」，**不授予任何业务权限**（PLAN §9
  「后端对每次调用执行授权」「管理员不因管理服务器而获得财务批准权」）。因此
  ``sysadmin`` 能管理角色与渠道绑定，却**看不到**报销单，也**不能**审批。
* **审批动作必须复用服务层**：``approve`` / ``return`` / ``reject`` /
  ``finance_review`` 一律调用 ``app.expense.service`` 中对应的函数——**绝不**在
  本层自行实现状态迁移、跳过授权或省略审计。服务层抛出的
  ``AuthorizationDeniedError`` 映射为 403 页面，``InvalidTransitionError`` 映射为
  409 页面，``ValidationError`` 映射为 422 页面。
* **响应不泄露内部信息**：页面与错误页都**不含**堆栈、DSN、SQL 或异常 ``repr``
  （对齐 ``docs/DEPLOYMENT-REQUIREMENTS.md`` §7 与 ``app.api.errors`` 的约定）。
* **变更可审计**：角色授予/撤销、渠道绑定/解绑都在同一事务内写 ``audit_events``；
  ``details`` 只放结构化、非敏感字段——**渠道用户 ID 以加盐哈希出现**，绝不写
  明文（``app.audit.service`` 的禁令）。
* **拒绝可审计**：每个页面与每个动作的权限拒绝都在**同一事务内**写一条
  ``admin.deny`` 事件（含 ``actor_id`` / ``action`` / ``resource_type`` /
  ``resource_id`` / ``request_id`` / ``details``），并**随拒绝一起提交**，因此
  拒绝不会因为异常回滚而丢失审计（PLAN §12「权限拒绝可审计」）。**写审计失败绝不
  放行**：审计写入或提交抛出的任何异常都被吞掉，随后仍抛 ``AdminForbidden``——
  默认拒绝优先，宁可少一条审计，也不放行、也不把 403 变成 500。
* **登录失败单独记事件、绝不记凭据**：登录凭据错误写**独立的事件类型**
  ``admin.login_failed``（``actor_id=None``：凭据未通过验证，没有可归因的已核验
  人员），``details`` 只含结构化原因；**绝不记录**令牌、cookie、会话值或任何凭据
  原文。``AdminAuthRequired``（未登录 / 篡改 / 过期 / 主体失效）**不写审计**，理由
  见 ``register_admin_exception_handlers`` 的 docstring。

审批类动作（approve / return / reject / finance-review）的权限拒绝**由**
``app.expense.service`` 在同一事务内写 ``expense_claim.deny``（``app.api.claims``
的 API 层写的是同一条），本层不重复记录，避免一次拒绝产生两条事件。

认证的过渡方案（共享内部令牌 + 操作员自报员工号）详见 ``app.api.admin_auth`` 的
模块 docstring；生产必须接入公司 SSO，本模块不得作为最终形态。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.admin_auth import (
    SESSION_COOKIE_NAME,
    AdminAuthRequired,
    AdminForbidden,
    configured_token,
    create_session_token,
    get_admin_principal,
    session_cookie_kwargs,
    verify_login,
)
from app.api.deps import get_request_id
from app.audit.models import AuditEvent
from app.audit.service import record_event
from app.db.models.organization import Company, Employee
from app.db.session import get_session
from app.expense.models import ExpenseClaim, ExpenseLine
from app.expense.service import (
    AuthorizationDeniedError,
    ClaimNotFoundError,
    ExpenseServiceError,
    ValidationError,
    approve,
    finance_review,
    reject,
    return_for_fix,
)
from app.expense.state_machine import InvalidTransitionError
from app.identity.authorization import (
    Principal,
    can_approve,
    has_permission,
    visible_employee_ids,
)
from app.identity.models import EmployeeChannelBinding, EmployeeRole, Role

__all__ = ["register_admin_exception_handlers", "router"]

router = APIRouter(prefix="/admin", tags=["admin"])

#: 读取范围由宽到窄的优先级（与 ``app.api.claims`` 一致，最宽者胜出）。
_READ_SCOPES_WIDEST_FIRST: tuple[str, ...] = ("company", "team", "authorized", "own")

#: 仪表盘可见所需的最小能力集合（任一命中即可）。
_DASHBOARD_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("expense_claim", "read", "own"),
    ("expense_claim", "read", "team"),
    ("expense_claim", "read", "company"),
    ("expense_claim", "read", "authorized"),
    ("identity", "manage", "company"),
    ("channel_binding", "manage", "company"),
    ("audit", "read", "authorized"),
)

#: 审批动作名 → 服务层函数（复用 ``app.expense.service``，绝不自行实现）。
_ACTION_HANDLERS: dict[str, Callable[..., ExpenseClaim]] = {
    "approve": approve,
    "return": return_for_fix,
    "reject": reject,
    "finance-review": finance_review,
}

#: 后台页面 / 动作的权限被拒时写入的动作名（PLAN §12「权限拒绝可审计」）。
_DENY_ACTION = "admin.deny"

#: 登录凭据错误的事件名。与业务操作（``admin.login`` / ``admin.deny``）明确区分，
#: 且**绝不**携带令牌、cookie 或任何凭据原文。
_LOGIN_FAILED_ACTION = "admin.login_failed"

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# --- 渲染与事务辅助 -----------------------------------------------------------


def _render(template_name: str, *, status_code: int = 200, **context: object) -> HTMLResponse:
    """渲染模板为 HTML 响应；页面内容绝不含内部细节。"""
    html = _env.get_template(template_name).render(**context)
    return HTMLResponse(content=html, status_code=status_code)


@contextmanager
def _unit_of_work(session: Session) -> Iterator[None]:
    """统一事务边界：成功或业务域拒绝都 ``commit``，未预期异常则 ``rollback``。

    与 ``app.api.claims._unit_of_work`` 同一策略：业务域异常意味着服务层已写入一条
    拒绝审计事件但业务状态未变，此时 ``commit`` 只持久化该审计，随后原样抛出交由
    本层映射为页面。
    """
    try:
        yield
    except (ExpenseServiceError, InvalidTransitionError):
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()


def _widest_scope(
    session: Session, principal: Principal, resource: str, action: str
) -> str | None:
    """返回主体在 ``resource/action`` 下拥有的最宽 scope；无则 ``None``。

    优先级 ``company > team > authorized > own``：一旦命中更宽的 scope 即返回，
    不再考虑更窄者。判定完全交给 ``app.identity.authorization.has_permission``
    （默认拒绝）。
    """
    for scope in _READ_SCOPES_WIDEST_FIRST:
        if has_permission(session, principal, resource, action, scope):
            return scope
    return None


def _widest_read_scope(session: Session, principal: Principal) -> str | None:
    """返回主体在 ``expense_claim/read`` 下拥有的最宽 scope；无则 ``None``。"""
    return _widest_scope(session, principal, "expense_claim", "read")


def _visible_read_ids(session: Session, principal: Principal) -> set[int] | None:
    """返回主体可读取的员工 ID 集合；无任何读取权限时返回 ``None``。"""
    scope = _widest_read_scope(session, principal)
    if scope is None:
        return None
    return visible_employee_ids(session, principal, scope)


def _record_denial(
    session: Session,
    principal: Principal,
    *,
    resource_type: str,
    attempted_action: str,
    request_id: str | None,
    resource_id: str | None = None,
    reason: str = "missing_permission",
) -> None:
    """在同一事务内写一条 ``admin.deny`` 事件并**提交**它（PLAN §12）。

    与 ``app.api.claims._authorize_read`` / ``app.expense.service._record_deny``
    同一模式：``details`` 只含结构化的 ``attempted_action`` 与 ``reason``，绝不写
    敏感字段原文。区别在于本层在**抛出** ``AdminForbidden`` 之前必须自己
    ``commit``——页面路径的拒绝以异常收尾，若等到事务边界再提交就会随回滚丢失，
    而 PLAN §12 要求拒绝可审计。

    **默认拒绝优先**：写入或提交抛出的任何异常都被吞掉并 ``rollback``，调用方随后
    仍抛 ``AdminForbidden``。审计后端故障**不得**把 403 变成 500，更不得放行请求。
    """
    try:
        record_event(
            session,
            actor_id=principal.employee_id,
            action=_DENY_ACTION,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            details={"attempted_action": attempted_action, "reason": reason},
        )
        session.commit()
    except Exception:  # noqa: BLE001 - 审计失败一律降级为「仍然拒绝」
        session.rollback()


def _forbid(
    session: Session,
    principal: Principal,
    *,
    resource_type: str,
    attempted_action: str,
    request_id: str | None,
    resource_id: str | None = None,
    reason: str = "missing_permission",
    message: str = "无权访问该页面",
) -> NoReturn:
    """记录拒绝审计后抛 ``AdminForbidden``（渲染 403 页面，不泄露数据）。"""
    _record_denial(
        session,
        principal,
        resource_type=resource_type,
        resource_id=resource_id,
        attempted_action=attempted_action,
        request_id=request_id,
        reason=reason,
    )
    raise AdminForbidden(message)


def _require_or_forbidden(
    session: Session,
    principal: Principal,
    resource: str,
    action: str,
    scope: str,
    *,
    resource_type: str,
    attempted_action: str,
    request_id: str | None,
    resource_id: str | None = None,
) -> None:
    """要求指定权限，否则记一条拒绝审计并抛 ``AdminForbidden``。

    ``resource`` / ``action`` / ``scope`` 用于 ``has_permission`` 判定；
    ``resource_type`` / ``resource_id`` / ``attempted_action`` 描述被拒的审计对象。
    """
    if not has_permission(session, principal, resource, action, scope):
        _forbid(
            session,
            principal,
            resource_type=resource_type,
            resource_id=resource_id,
            attempted_action=attempted_action,
            request_id=request_id,
        )


def _external_id_hash(value: str) -> str:
    """渠道用户 ID 的加盐哈希（以内部令牌为盐），审计里只存它而非明文。"""
    token = configured_token() or ""
    return hmac.new(
        token.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _employee_label(employee: Employee | None) -> str:
    """员工的可读标签：``#<id> <employee_no>``；``None`` 显示占位。"""
    if employee is None:
        return "（已删除）"
    return f"#{employee.id} {employee.employee_no}"


def _nav(session: Session, principal: Principal) -> dict[str, bool]:
    """按权限计算导航链接可见性（不可见者不渲染链接，页面本身也仍会 403）。"""
    return {
        "nav_identity": has_permission(
            session, principal, "identity", "manage", "company"
        ),
        "nav_bindings": has_permission(
            session, principal, "channel_binding", "manage", "company"
        ),
        "nav_audit": _widest_scope(session, principal, "audit", "read") is not None,
    }


def _page_context(
    session: Session, principal: Principal, *, title: str
) -> dict[str, object]:
    """构造所有已登录页面共用的模板上下文。"""
    employee = session.get(Employee, principal.employee_id)
    context: dict[str, object] = {
        "title": title,
        "principal": principal,
        "employee_label": _employee_label(employee),
    }
    context.update(_nav(session, principal))
    return context


# --- 认证：登录 / 登出 --------------------------------------------------------


def _record_login_failure(session: Session, *, request_id: str | None) -> None:
    """记录一次登录凭据失败（``admin.login_failed``），**绝不**写凭据原文。

    设计取舍（PLAN §9「审批记录关联实际人员」与 ``app.audit.service.record_event``
    对 ``actor_id`` 的约束）：

    * ``actor_id=None``：提交的令牌与员工号都**未通过验证**，此刻没有可归因的
      **已核验**人员。把自报的 ``employee_no`` 记成主体，等于把一次匿名尝试错误地
      归到某个员工名下——审计的主体必须是已核验的实际人员，因此宁可为空。
    * 事件类型 ``admin.login_failed`` 与业务操作（``admin.login`` / ``admin.deny``）
      **明确区分**，不会被误读为一次成功的业务操作。
    * ``details`` 只含结构化的失败原因；**不记录**令牌、员工号、cookie、会话值或
      任何凭据原文（``app.audit.service`` 的脱敏禁令）。
    * 与拒绝审计同样**尽力而为**：写入失败一律降级为「照常返回 401」，绝不放行。
    """
    try:
        record_event(
            session,
            actor_id=None,
            action=_LOGIN_FAILED_ACTION,
            resource_type="admin_session",
            resource_id=None,
            request_id=request_id,
            details={"reason": "invalid_credentials"},
        )
        session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不得改变 401 的结果
        session.rollback()


@router.get("/login", response_class=HTMLResponse)
def login_form() -> HTMLResponse:
    """渲染登录表单。GET 始终可访问（POST 在令牌未配置时 503）。"""
    return _render("login.html", title="管理后台登录", principal=None)


@router.post("/login")
def login_submit(
    token: str = Form(default=""),
    employee_no: str = Form(default=""),
    session: Session = Depends(get_session),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """校验凭据并下发签名会话 cookie。

    内部令牌未配置 → **503**（fail closed）；令牌不符或员工号无效 → **401**，
    并写一条**独立事件类型** ``admin.login_failed``（绝不写凭据原文）。

    失败路径的审计写入是**尽力而为**的：写审计抛错也只降级为「照常返回 401」，
    绝不放行、绝不变成 500。
    """
    if configured_token() is None:
        return _render(
            "login.html",
            status_code=503,
            title="管理后台登录",
            principal=None,
            error="服务未配置内部令牌，登录已拒绝（fail closed）。",
        )

    employee = verify_login(session, token=token, employee_no=employee_no)
    if employee is None:
        _record_login_failure(session, request_id=request_id)
        return _render(
            "login.html",
            status_code=401,
            title="管理后台登录",
            principal=None,
            error="凭据无效。",
        )

    with _unit_of_work(session):
        record_event(
            session,
            actor_id=employee.id,
            action="admin.login",
            resource_type="admin_session",
            request_id=request_id,
            details={},
        )

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(value=create_session_token(employee.id), **session_cookie_kwargs())
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    """清除会话 cookie 并重定向到登录页。"""
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/admin")
    return response


# --- 页面：仪表盘 -------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> HTMLResponse:
    """仪表盘：可见报销单计数与待办审批数（按权限与数据范围计算）。"""
    if not any(
        has_permission(session, principal, *perm) for perm in _DASHBOARD_PERMISSIONS
    ):
        _forbid(
            session,
            principal,
            resource_type="admin_console",
            attempted_action="dashboard",
            request_id=request_id,
        )

    visible = _visible_read_ids(session, principal)
    claim_count = 0
    pending_approvals = 0
    if visible:
        claim_count = session.scalar(
            select(func.count())
            .select_from(ExpenseClaim)
            .where(ExpenseClaim.employee_id.in_(visible))
        ) or 0
        submitted = session.scalars(
            select(ExpenseClaim).where(
                ExpenseClaim.status == "submitted",
                ExpenseClaim.employee_id.in_(visible),
            )
        ).all()
        pending_approvals = sum(
            1 for claim in submitted if can_approve(session, principal, claim.employee_id)
        )

    return _render(
        "dashboard.html",
        **_page_context(session, principal, title="仪表盘"),
        claim_count=claim_count,
        pending_approvals=pending_approvals,
    )


# --- 页面：报销单列表与详情 ---------------------------------------------------


@router.get("/claims", response_class=HTMLResponse)
def claims_list(
    status: str | None = None,
    employee_no: str | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> HTMLResponse:
    """报销单列表：按 Principal 数据范围过滤，支持状态与员工号筛选。"""
    visible = _visible_read_ids(session, principal)
    if visible is None:
        # 无任何读取权限：记拒绝审计并渲染 403 页面，不返回任何数据。
        _forbid(
            session,
            principal,
            resource_type="expense_claim",
            attempted_action="list",
            request_id=request_id,
        )

    stmt = select(ExpenseClaim).where(ExpenseClaim.employee_id.in_(visible))
    if status:
        stmt = stmt.where(ExpenseClaim.status == status)
    if employee_no:
        matching = session.scalars(
            select(Employee.id).where(Employee.employee_no == employee_no.strip())
        ).all()
        stmt = stmt.where(ExpenseClaim.employee_id.in_(matching))

    claims = session.scalars(stmt.order_by(ExpenseClaim.id)).all()
    rows = [
        {
            "claim": claim,
            "owner_label": _employee_label(session.get(Employee, claim.employee_id)),
        }
        for claim in claims
    ]
    return _render(
        "claims.html",
        **_page_context(session, principal, title="报销单"),
        rows=rows,
        status_filter=status or "",
        employee_no_filter=employee_no or "",
        statuses=(
            "draft",
            "submitted",
            "approved",
            "finance_reviewed",
            "paid",
            "withdrawn",
            "rejected",
        ),
    )


@router.get("/claims/{claim_id}", response_class=HTMLResponse)
def claim_detail(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> HTMLResponse:
    """报销单详情：明细、总额、状态与审计轨迹（先授权、后读取）。"""
    claim = session.get(ExpenseClaim, claim_id)
    if claim is None:
        return _render(
            "message.html",
            status_code=404,
            title="未找到",
            heading="未找到该报销单",
            detail="请求的报销单不存在或已被移除。",
            principal=principal,
        )

    visible = _visible_read_ids(session, principal)
    if visible is None or claim.employee_id not in visible:
        reason = "missing_permission" if visible is None else "not_in_scope"
        _forbid(
            session,
            principal,
            resource_type="expense_claim",
            resource_id=str(claim.id),
            attempted_action="read",
            request_id=request_id,
            reason=reason,
        )

    lines = session.scalars(
        select(ExpenseLine)
        .where(ExpenseLine.claim_id == claim.id)
        .order_by(ExpenseLine.id)
    ).all()
    events = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.resource_type == "expense_claim",
            AuditEvent.resource_id == str(claim.id),
        )
        .order_by(AuditEvent.id)
    ).all()

    return _render(
        "claim_detail.html",
        **_page_context(session, principal, title=f"报销单 {claim.claim_no}"),
        claim=claim,
        lines=lines,
        events=events,
        owner_label=_employee_label(session.get(Employee, claim.employee_id)),
        can_approve_flag=can_approve(session, principal, claim.employee_id),
        can_finance_review=has_permission(
            session, principal, "expense_claim", "finance_review", "company"
        ),
    )


def _run_action(
    session: Session,
    principal: Principal,
    claim_id: int,
    *,
    action: str,
    request_id: str | None,
) -> Response:
    """把审批类动作分派到服务层并映射结果为页面响应。

    授权与状态迁移**全部**在 ``app.expense.service`` 中执行；本函数只做错误到
    页面的映射，绝不自行改写业务状态。
    """
    handler = _ACTION_HANDLERS[action]
    try:
        with _unit_of_work(session):
            claim = handler(session, principal, claim_id, request_id=request_id)
    except ClaimNotFoundError:
        return _render(
            "message.html",
            status_code=404,
            title="未找到",
            heading="未找到该报销单",
            detail="请求的报销单不存在或已被移除。",
            principal=principal,
        )
    except AuthorizationDeniedError:
        return _render(
            "forbidden.html",
            status_code=403,
            title="无权限",
            message="你无权对该报销单执行此操作。",
            principal=principal,
        )
    except InvalidTransitionError:
        return _render(
            "message.html",
            status_code=409,
            title="状态不允许",
            heading="当前状态不允许该操作",
            detail="该报销单的状态已变化，无法执行所选动作。",
            principal=principal,
        )
    except ValidationError:
        return _render(
            "message.html",
            status_code=422,
            title="校验失败",
            heading="操作未通过业务校验",
            detail="请检查报销单内容后重试。",
            principal=principal,
        )
    return RedirectResponse(url=f"/admin/claims/{claim.id}", status_code=303)


@router.post("/claims/{claim_id}/approve")
def claim_approve(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """审批（复用 ``expense.service.approve``：团队范围、非本人）。"""
    return _run_action(
        session, principal, claim_id, action="approve", request_id=request_id
    )


@router.post("/claims/{claim_id}/return")
def claim_return(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """退回补件（复用 ``expense.service.return_for_fix``）。"""
    return _run_action(
        session, principal, claim_id, action="return", request_id=request_id
    )


@router.post("/claims/{claim_id}/reject")
def claim_reject(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """拒绝（复用 ``expense.service.reject``）。"""
    return _run_action(
        session, principal, claim_id, action="reject", request_id=request_id
    )


@router.post("/claims/{claim_id}/finance-review")
def claim_finance_review(
    claim_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """财务复核（复用 ``expense.service.finance_review``：公司范围）。"""
    return _run_action(
        session, principal, claim_id, action="finance-review", request_id=request_id
    )


# --- 页面：员工与角色 ---------------------------------------------------------


@router.get("/employees", response_class=HTMLResponse)
def employees_list(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> HTMLResponse:
    """员工列表与角色授予/撤销（需 ``identity/manage/company``）。"""
    _require_or_forbidden(
        session,
        principal,
        "identity",
        "manage",
        "company",
        resource_type="identity",
        attempted_action="manage",
        request_id=request_id,
    )

    employees = session.scalars(
        select(Employee)
        .where(Employee.company_id == principal.company_id)
        .order_by(Employee.id)
    ).all()
    roles = session.scalars(select(Role).order_by(Role.id)).all()
    role_names = {role.id: role.name for role in roles}
    granted = session.scalars(select(EmployeeRole)).all()
    by_employee: dict[int, list[str]] = {}
    for link in granted:
        name = role_names.get(link.role_id)
        if name is not None:
            by_employee.setdefault(link.employee_id, []).append(name)

    rows = [
        {
            "employee": employee,
            "roles": sorted(by_employee.get(employee.id, [])),
        }
        for employee in employees
    ]
    return _render(
        "employees.html",
        **_page_context(session, principal, title="员工与角色"),
        rows=rows,
        all_roles=[role.name for role in roles],
    )


@router.post("/employees/{employee_id}/roles")
def employee_role_change(
    employee_id: int,
    role: str = Form(default=""),
    action: str = Form(default=""),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """授予或撤销某员工的角色，并在同一事务内写审计。"""
    _require_or_forbidden(
        session,
        principal,
        "identity",
        "manage",
        "company",
        resource_type="employee_role",
        resource_id=str(employee_id),
        attempted_action="role_change",
        request_id=request_id,
    )

    if action not in {"grant", "revoke"}:
        return _render(
            "message.html",
            status_code=422,
            title="校验失败",
            heading="无法识别的角色操作",
            detail="允许的操作只有 grant 与 revoke。",
            principal=principal,
        )

    target = session.get(Employee, employee_id)
    if target is None or target.company_id != principal.company_id:
        return _render(
            "message.html",
            status_code=404,
            title="未找到",
            heading="未找到该员工",
            detail="目标员工不存在或不属于当前公司。",
            principal=principal,
        )

    role_obj = session.scalar(select(Role).where(Role.name == role))
    if role_obj is None:
        return _render(
            "message.html",
            status_code=422,
            title="校验失败",
            heading="无法识别的角色",
            detail="请求的角色不在权限目录中。",
            principal=principal,
        )

    with _unit_of_work(session):
        existing = session.get(EmployeeRole, (employee_id, role_obj.id))
        if action == "grant":
            if existing is None:
                session.add(
                    EmployeeRole(
                        employee_id=employee_id,
                        role_id=role_obj.id,
                        granted_by=principal.employee_id,
                    )
                )
        else:  # revoke
            if existing is not None:
                session.delete(existing)
        record_event(
            session,
            actor_id=principal.employee_id,
            action=f"identity.role_{action}",
            resource_type="employee_role",
            resource_id=str(employee_id),
            request_id=request_id,
            details={"role": role, "change": action},
        )

    return RedirectResponse(url="/admin/employees", status_code=303)


# --- 页面：渠道身份绑定 -------------------------------------------------------


@router.get("/bindings", response_class=HTMLResponse)
def bindings_list(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> HTMLResponse:
    """渠道身份绑定列表（需 ``channel_binding/manage/company``）。"""
    _require_or_forbidden(
        session,
        principal,
        "channel_binding",
        "manage",
        "company",
        resource_type="employee_channel_binding",
        attempted_action="manage",
        request_id=request_id,
    )

    bindings = session.scalars(
        select(EmployeeChannelBinding).order_by(EmployeeChannelBinding.id)
    ).all()
    rows = [
        {
            "binding": binding,
            "employee_label": _employee_label(session.get(Employee, binding.employee_id)),
        }
        for binding in bindings
    ]
    employees = session.scalars(
        select(Employee)
        .where(Employee.company_id == principal.company_id)
        .order_by(Employee.id)
    ).all()
    return _render(
        "bindings.html",
        **_page_context(session, principal, title="渠道绑定"),
        rows=rows,
        employees=[
            {"id": employee.id, "label": _employee_label(employee)}
            for employee in employees
        ],
    )


@router.post("/bindings")
def bindings_change(
    action: str = Form(default=""),
    binding_id: str = Form(default=""),
    employee_id: str = Form(default=""),
    channel_type: str = Form(default=""),
    external_user_id: str = Form(default=""),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> Response:
    """绑定或解绑渠道身份，并在同一事务内写审计（渠道 ID 只写哈希）。"""
    _require_or_forbidden(
        session,
        principal,
        "channel_binding",
        "manage",
        "company",
        resource_type="employee_channel_binding",
        attempted_action="manage",
        request_id=request_id,
    )

    if action == "bind":
        return _bind_channel(
            session,
            principal,
            employee_id=employee_id,
            channel_type=channel_type,
            external_user_id=external_user_id,
            request_id=request_id,
        )
    if action == "unbind":
        return _unbind_channel(
            session, principal, binding_id=binding_id, request_id=request_id
        )
    return _render(
        "message.html",
        status_code=422,
        title="校验失败",
        heading="无法识别的绑定操作",
        detail="允许的操作只有 bind 与 unbind。",
        principal=principal,
    )


def _bind_channel(
    session: Session,
    principal: Principal,
    *,
    employee_id: str,
    channel_type: str,
    external_user_id: str,
    request_id: str | None,
) -> Response:
    """把 ``(channel_type, external_user_id)`` 绑定到目标员工（幂等）。"""
    if not employee_id.strip().isdigit() or not channel_type.strip() or not external_user_id.strip():
        return _render(
            "message.html",
            status_code=422,
            title="校验失败",
            heading="绑定参数不完整",
            detail="需要员工、渠道类型与渠道用户 ID。",
            principal=principal,
        )
    target = session.get(Employee, int(employee_id))
    if target is None or target.company_id != principal.company_id:
        return _render(
            "message.html",
            status_code=404,
            title="未找到",
            heading="未找到该员工",
            detail="目标员工不存在或不属于当前公司。",
            principal=principal,
        )

    with _unit_of_work(session):
        existing = session.scalar(
            select(EmployeeChannelBinding).where(
                EmployeeChannelBinding.channel_type == channel_type.strip(),
                EmployeeChannelBinding.external_user_id == external_user_id.strip(),
            )
        )
        if existing is None:
            session.add(
                EmployeeChannelBinding(
                    employee_id=target.id,
                    channel_type=channel_type.strip(),
                    external_user_id=external_user_id.strip(),
                    status="active",
                )
            )
        else:
            existing.employee_id = target.id
            existing.status = "active"
        record_event(
            session,
            actor_id=principal.employee_id,
            action="channel_binding.bind",
            resource_type="employee_channel_binding",
            resource_id=str(target.id),
            request_id=request_id,
            details={
                "channel_type": channel_type.strip(),
                "external_user_id_hash": _external_id_hash(external_user_id.strip()),
                "status": "active",
            },
        )
    return RedirectResponse(url="/admin/bindings", status_code=303)


def _unbind_channel(
    session: Session,
    principal: Principal,
    *,
    binding_id: str,
    request_id: str | None,
) -> Response:
    """把某条绑定置为 ``revoked``（不物理删除，保留审计价值）。"""
    if not binding_id.strip().isdigit():
        return _render(
            "message.html",
            status_code=422,
            title="校验失败",
            heading="绑定参数不完整",
            detail="解绑需要有效的绑定 ID。",
            principal=principal,
        )
    binding = session.get(EmployeeChannelBinding, int(binding_id))
    if binding is None:
        return _render(
            "message.html",
            status_code=404,
            title="未找到",
            heading="未找到该绑定",
            detail="请求的渠道绑定不存在。",
            principal=principal,
        )

    with _unit_of_work(session):
        binding.status = "revoked"
        record_event(
            session,
            actor_id=principal.employee_id,
            action="channel_binding.unbind",
            resource_type="employee_channel_binding",
            resource_id=str(binding.employee_id),
            request_id=request_id,
            details={
                "channel_type": binding.channel_type,
                "external_user_id_hash": _external_id_hash(binding.external_user_id),
                "status": "revoked",
            },
        )
    return RedirectResponse(url="/admin/bindings", status_code=303)


# --- 页面：审计日志 -----------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit_list(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_admin_principal),
    request_id: str | None = Depends(get_request_id),
) -> HTMLResponse:
    """审计日志查看：按主体在 ``audit/read`` 下**最宽的** scope 过滤事件。

    过滤规则（与 ``app.api.claims`` 同模式，模块级不变量）：

    * 取主体在 ``audit/read`` 下拥有的最宽 scope，优先级
      ``company > team > authorized > own``；一旦命中更宽者即采用，不再考虑更窄者；
    * 用 ``app.identity.authorization.visible_employee_ids(session, principal,
      scope)`` 解析出该 scope 下可见的员工 ID 集合，只渲染
      ``AuditEvent.actor_id`` 落在该集合内的事件（按 ``id`` 降序，最多 200 条）；
    * 主体**没有任何** ``audit/read`` 权限时抛 ``AdminForbidden``（渲染 403 页面，
      不返回任何事件，也不泄露存在性）。

    **已知限制（必须显式声明，不得假装已支持）**：当最宽 scope 为 ``authorized``
    时，``visible_employee_ids`` 对 ``authorized`` 是**占位实现**——显式授权表
    （把具体资源实例显式授予某员工，如「审计人员只读特定单据」）**尚未实现**，该
    分支当前只返回 ``{principal.employee_id}``。因此此情形下页面实际只显示
    ``actor_id == principal.employee_id`` 的事件（即「仅自身」），**并非**「只看被
    授权单据」的语义；这正是它比「公司范围审计查看」（``company`` scope）**更窄**
    的原因。待显式授权表落地后，此处的 ``authorized`` 分支才会真正收窄到被授权
    单据范围（对齐 ``app.identity.authorization.visible_employee_ids`` 的占位说明；
    权限目录与 PLAN §9 的对齐属独立任务 W1-13）。
    """
    scope = _widest_scope(session, principal, "audit", "read")
    if scope is None:
        # 无任何 audit/read 权限：记拒绝审计并渲染 403 页面，不返回任何事件。
        _forbid(
            session,
            principal,
            resource_type="audit",
            attempted_action="read",
            request_id=request_id,
        )

    visible = visible_employee_ids(session, principal, scope)
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.actor_id.in_(visible))
        .order_by(AuditEvent.id.desc())
        .limit(200)
    ).all()
    return _render(
        "audit.html",
        **_page_context(session, principal, title="审计日志"),
        events=events,
    )


# --- 异常处理注册 -------------------------------------------------------------


def register_admin_exception_handlers(app: FastAPI) -> None:
    """注册管理后台专用的异常处理器（重定向登录页 / 渲染 403 页面）。

    ``AdminForbidden`` → 403 页面：拒绝审计**已在**抛出点（``_forbid`` /
    ``_require_or_forbidden``）随拒绝一起提交，处理器本身不再写审计，只负责渲染，
    因此 403 页面内容与改动前完全一致。

    ``AdminAuthRequired`` → 重定向登录页，**刻意不写审计**（PLAN §12 允许，理由如下）：

    1. **没有可归因的主体**：未登录 / 篡改 / 过期 / 主体失效的请求里没有**已核验**
       的人员，而 ``app.audit.service.record_event`` 要求 ``actor_id`` 必须是已验证
       的实际人员（``None`` 仅用于系统自身发起、确无人员主体的操作）。为一次匿名
       请求写审计会把未经核验的请求伪装成有主体的事件。
    2. **未认证写放大**：该分支对**任何**匿名请求都会触发（含爬虫、健康检查与预取），
       而 ``audit_events`` 是只增不改、数据库触发器强制的表——按请求写事件等于给
       未认证流量一个无界的写放大面，得不偿失。
    3. **无可误读风险**：这里没有写任何业务事件，因此不存在「被误读为业务操作」的
       问题；登录凭据错误仍由 ``admin.login_failed`` 独立记录。

    ``AdminAuthRequired`` 与登录失败都**绝不**记录令牌、cookie 或会话值明文。
    """

    async def _login_required(_request: Request, _exc: Exception) -> RedirectResponse:
        # 不写审计：见本函数 docstring 的三条理由。
        return RedirectResponse(url="/admin/login", status_code=303)

    async def _forbidden(_request: Request, exc: Exception) -> HTMLResponse:
        message = getattr(exc, "message", "无权访问该页面")
        return _render(
            "forbidden.html",
            status_code=403,
            title="无权限",
            message=message,
            principal=None,
        )

    app.add_exception_handler(AdminAuthRequired, _login_required)
    app.add_exception_handler(AdminForbidden, _forbidden)
