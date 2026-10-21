"""授权判定服务与数据范围解析（任务 W1-4）。

本模块把 ``templates/PLAN.md`` §9「权限模型」的强制规则编码成后端**每次调用**
都要执行的确定性判定：默认拒绝、最小权限、员工不能审批自己的报销、数据范围
（own/team/company/authorized）解析。

它与 ``permissions.py`` 的分工是：

* ``permissions.py`` 只描述**数据**（有哪些角色/权限、角色被授予哪些权限）；
* 本模块只做**判定**——从数据库读取员工的角色与权限，计算数据范围，并在每次
  调用时执行授权检查。判定绝不能只依赖 Profile 或模型自报的身份。

关键不变量（对齐 PLAN §9 与 ``docs/PROJECT-CHARTER.md`` 强制原则 5）：

* **默认拒绝（deny-by-default）**：没有命中权限即拒绝，绝不默认放行。
* **数据范围是权限的一部分**：``(resource, action, scope)`` 三元组里 ``scope``
  决定能看见哪些员工的数据，判定时必须同时校验权限与范围。
* **自审一律拒绝**：即使拥有 ``expense_claim/approve/team``，员工也不能审批
  自己的报销（``can_approve`` 强制 ``claim_owner != principal.employee_id``）。
* **Profile 不是许可**：能访问某个 Profile 不代表能访问其业务数据；授权由本
  模块基于已验证的身份上下文判定（``docs/research/W0-2-profile-isolation.md``
  §4 身份路由协议草案）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.organization import Employee
from app.identity.models import EmployeeRole, Permission, RolePermission

__all__ = [
    "Principal",
    "can_approve",
    "effective_permissions",
    "effective_permissions_for_role_ids",
    "has_permission",
    "role_ids_for_employee",
    "visible_employee_ids",
]

#: 已定义的数据范围档位（与 ``permissions.scope`` 的 CHECK 约束一致）。
_KNOWN_SCOPES = frozenset({"own", "team", "company", "authorized"})


@dataclass(frozen=True)
class Principal:
    """一次后端调用的操作主体（已验证的操作人）。

    安全约束（**必须遵守，不得绕过**）：``Principal`` 只能由后端从**已验证的
    上下文**构造，例如已校验的渠道身份绑定（``employee_channel_bindings`` 中
    ``status='active'`` 的 ``(channel_type, external_user_id) -> employee_id``）。
    **绝不接受模型或请求体自报的 ``employee_id``**：模型可提取、解释、建议，
    但不能声明自己是谁（PLAN §2.1「不能依靠模型自行填入的 ``employee_id``」、
    ``docs/research/W0-2-profile-isolation.md`` §4「身份路由必须自建、且必须是
    拒绝式授权」）。

    ``company_id`` 同样来自已核验的员工记录，用于把数据范围限定在同一公司内，
    不信任任何外部输入。
    """

    employee_id: int
    company_id: int


def role_ids_for_employee(session: Session, employee_id: int) -> tuple[int, ...]:
    """Return the employee's role IDs in deterministic order.

    This is the shared primitive used by both employee and service-identity
    principals in :mod:`app.governance.principal`.  It reuses the existing
    ``employee_roles`` table; no second RBAC store is introduced.
    """
    rows = session.scalars(
        select(EmployeeRole.role_id)
        .where(EmployeeRole.employee_id == employee_id)
        .order_by(EmployeeRole.role_id)
    ).all()
    return tuple(rows)


def effective_permissions_for_role_ids(
    session: Session, role_ids: Iterable[int]
) -> frozenset[tuple[str, str, str]]:
    """Return the union of permissions granted by the supplied role IDs.

    An empty iterable yields an empty set (deny by default).  Service identities
    use this with their ``service_identity_roles`` rows; employee principals use
    it with :func:`role_ids_for_employee`.
    """
    ids = tuple(sorted(set(role_ids)))
    if not ids:
        return frozenset()
    stmt = (
        select(Permission.resource, Permission.action, Permission.scope)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id.in_(ids))
    )
    return frozenset(
        (resource, action, scope)
        for resource, action, scope in session.execute(stmt)
    )


def effective_permissions(
    session: Session, employee_id: int
) -> frozenset[tuple[str, str, str]]:
    """返回员工经由角色获得的全部权限三元组 ``(resource, action, scope)``。

    路径为 ``employee_roles -> role_permissions -> permissions`` 求并集：一个员工
    可以有多个角色，任一角色授予即算拥有。**无角色时返回空集**（默认拒绝的前置
    事实）。返回值是 ``frozenset``，天然去重且不可变。
    """
    return effective_permissions_for_role_ids(
        session, role_ids_for_employee(session, employee_id)
    )


def has_permission(
    session: Session,
    principal: Principal,
    resource: str,
    action: str,
    scope: str,
) -> bool:
    """判定 ``principal`` 是否拥有 ``(resource, action, scope)`` 权限。

    默认拒绝：只有在 ``effective_permissions`` 的并集中精确命中该三元组时才返回
    ``True``，否则一律 ``False``。后端对每次调用执行本判定（PLAN §9「后端对每次
    调用执行授权」），不得缓存为「已登录即可访问」。
    """
    return (resource, action, scope) in effective_permissions(
        session, principal.employee_id
    )


def _team_member_ids(session: Session, principal: Principal) -> set[int]:
    """返回 ``principal`` 的团队可见员工集合：自身 + 沿 ``manager_id`` 的全部下属。

    遍历为**迭代式且带 visited 集合**：从 ``principal`` 出发逐层向下查找
    ``manager_id`` 指向当前层的员工，任一已访问的 ID 不再入队。这样即使
    ``manager_id`` 数据成环（例如 A→B→A，DB 约束只禁止 ``manager_id = id``
    的自环，不禁止多节点环），也**不会死循环**，且每个员工只被处理一次。

    结果**限定在同一 company 内**：只收集 ``company_id == principal.company_id``
    的员工，跨公司的下属（数据异常）被排除。
    """
    visited: set[int] = {principal.employee_id}
    frontier: list[int] = [principal.employee_id]

    while frontier:
        rows = session.scalars(
            select(Employee.id).where(
                Employee.manager_id.in_(frontier),
                Employee.company_id == principal.company_id,
            )
        ).all()
        next_frontier: list[int] = []
        for employee_id in rows:
            if employee_id not in visited:
                visited.add(employee_id)
                next_frontier.append(employee_id)
        frontier = next_frontier

    return visited


def visible_employee_ids(
    session: Session, principal: Principal, scope: str
) -> set[int]:
    """按 ``scope`` 解析 ``principal`` 可见的员工 ID 集合。

    * ``'own'``：仅 ``{principal.employee_id}``。
    * ``'team'``：自身加全部下属（沿 ``manager_id`` 递归所有层级），限定在同一
      company 内，迭代遍历且带 visited 集合，成环不死循环。
    * ``'company'``：同一 ``company_id`` 下的全部员工。
    * ``'authorized'``：**当前仅返回 ``{principal.employee_id}``**。显式授权表
      （把具体的资源实例显式授予某员工，如审计人员只读特定单据）尚未实现，本
      档位是**占位实现**，待后续任务扩展；**不得假装已支持**显式授权语义。
    * 其它未知 scope：抛 ``ValueError``（封闭集合，未知即错误，不静默放行）。
    """
    if scope == "own":
        return {principal.employee_id}

    if scope == "team":
        return _team_member_ids(session, principal)

    if scope == "company":
        rows = session.scalars(
            select(Employee.id).where(
                Employee.company_id == principal.company_id
            )
        ).all()
        return set(rows)

    if scope == "authorized":
        # 占位实现：显式授权表尚未实现，暂只返回自身，不得假装已支持。
        return {principal.employee_id}

    raise ValueError(f"unknown scope: {scope!r}")


def can_approve(
    session: Session, principal: Principal, claim_owner_employee_id: int
) -> bool:
    """判定 ``principal`` 能否审批 ``claim_owner_employee_id`` 的报销。

    必须**同时**满足三个条件，任一不满足即返回 ``False``：

    1. 拥有 ``expense_claim/approve/team`` 权限（审批权是团队范围的）；
    2. ``claim_owner_employee_id`` 在 ``principal`` 的 team 可见范围内；
    3. ``claim_owner_employee_id != principal.employee_id``——**自审一律拒绝**，
       即使拥有审批权限也不例外（PLAN §9「员工不能审批自己的报销」，章程成功
       标准「主管不能批准自己的报销」）。
    """
    if claim_owner_employee_id == principal.employee_id:
        return False

    if not has_permission(session, principal, "expense_claim", "approve", "team"):
        return False

    return claim_owner_employee_id in visible_employee_ids(
        session, principal, "team"
    )
