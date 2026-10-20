"""W1-4 授权判定服务与数据范围解析测试（针对 ajaimgr_test 库）。

覆盖任务验收标准：

1. **默认拒绝**：无任何角色的员工对所有权限判定均为 ``False``；
2. **employee 角色**：``expense_claim/create/own`` 为 ``True``、
   ``expense_claim/read/company`` 为 ``False``；
3. **manager 角色**：``expense_claim/read/team`` 为 ``True``；team 可见范围含自身
   与多层级下属；``manager_id`` 成环（A→B→A）时 ``visible_employee_ids`` 能返回
   且不死循环；
4. **company 范围**：包含同公司全部员工，不包含另一家公司的员工；
5. **authorized 范围**：当前仅返回自身（占位实现，待后续扩展）；
6. **自审防护**：拥有 approve 权限的主管对自己的报销 ``can_approve`` 为
   ``False``，对下属的报销为 ``True``；
7. **sysadmin**：调用 ``can_approve`` 为 ``False``（与 W1-3 权限目录一致）。

每个测试自行造 company/department/employee 与角色授予，session fixture 在结束后
删除这些行（含清理权限目录种子），不残留数据。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.identity.authorization import (
    Principal,
    can_approve,
    effective_permissions,
    effective_permissions_for_role_ids,
    has_permission,
    role_ids_for_employee,
    visible_employee_ids,
)
from app.identity.models import (
    EmployeeChannelBinding,
    EmployeeRole,
    Permission,
    Role,
    RolePermission,
)
from app.identity.seed import clear_permission_catalog, seed_permission_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head 再运行这些测试。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """删除这些测试可能创建的全部行（先子后父），并清掉权限目录种子。"""
    session.execute(delete(EmployeeChannelBinding))
    session.execute(delete(EmployeeRole))
    clear_permission_catalog(session)
    session.execute(delete(Employee))
    session.execute(delete(Department))
    session.execute(delete(Company))


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            # 权限目录是判定的事实来源，先播种 7 角色 / 20 权限 / 32 映射。
            seed_permission_catalog(sess)
            sess.commit()
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


# --- 造数据辅助 ---------------------------------------------------------------


def _company(session: Session, name: str) -> Company:
    company = Company(name=name)
    session.add(company)
    session.flush()
    return company


def _employee(
    session: Session,
    company: Company,
    employee_no: str,
    *,
    manager: Employee | None = None,
    department: Department | None = None,
) -> Employee:
    employee = Employee(
        company_id=company.id,
        department_id=department.id if department is not None else None,
        manager_id=manager.id if manager is not None else None,
        employee_no=employee_no,
    )
    session.add(employee)
    session.flush()
    return employee


def _grant(session: Session, employee: Employee, role_name: str) -> None:
    role = session.scalar(select(Role).where(Role.name == role_name))
    assert role is not None, f"role {role_name!r} not seeded"
    session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))
    session.flush()


# --- 测试 ---------------------------------------------------------------------


def test_deny_by_default_without_roles(session: Session) -> None:
    """无角色的员工：有效权限为空，且对任意权限判定均为 False。"""
    company = _company(session, "W1-4 deny")
    employee = _employee(session, company, "E-deny")
    principal = Principal(employee_id=employee.id, company_id=company.id)

    assert effective_permissions(session, employee.id) == frozenset()

    for resource, action, scope in (
        ("expense_claim", "create", "own"),
        ("expense_claim", "read", "own"),
        ("expense_claim", "approve", "team"),
        ("expense_claim", "read", "company"),
        ("identity", "manage", "company"),
    ):
        assert has_permission(session, principal, resource, action, scope) is False


def test_employee_role_permissions(session: Session) -> None:
    """employee 角色：own 范围内可创建，但没有 company 读取权。"""
    company = _company(session, "W1-4 employee")
    employee = _employee(session, company, "E-emp")
    _grant(session, employee, "employee")
    principal = Principal(employee_id=employee.id, company_id=company.id)

    assert has_permission(session, principal, "expense_claim", "create", "own") is True
    assert has_permission(session, principal, "expense_claim", "read", "company") is False
    assert effective_permissions(session, employee.id) == frozenset(
        {
            ("expense_claim", "create", "own"),
            ("expense_claim", "read", "own"),
            ("expense_claim", "submit", "own"),
            ("expense_claim", "withdraw", "own"),
        }
    )


def test_manager_team_scope_includes_multilevel_subordinates(session: Session) -> None:
    """manager 角色：read/team 为 True，team 范围含自身与多层级下属。"""
    company = _company(session, "W1-4 manager")
    boss = _employee(session, company, "E-boss")
    mid = _employee(session, company, "E-mid", manager=boss)
    leaf = _employee(session, company, "E-leaf", manager=mid)
    peer = _employee(session, company, "E-peer", manager=boss)
    outsider = _employee(session, company, "E-outsider")  # 不属于 boss 的团队
    _grant(session, boss, "manager")
    principal = Principal(employee_id=boss.id, company_id=company.id)

    assert has_permission(session, principal, "expense_claim", "read", "team") is True
    assert visible_employee_ids(session, principal, "team") == {
        boss.id,
        mid.id,
        leaf.id,
        peer.id,
    }
    assert outsider.id not in visible_employee_ids(session, principal, "team")


def test_team_scope_survives_manager_cycle(session: Session) -> None:
    """manager_id 成环（A→B→A）时可见范围能返回，且不死循环。"""
    company = _company(session, "W1-4 cycle")
    a = _employee(session, company, "E-cycle-a")
    b = _employee(session, company, "E-cycle-b", manager=a)
    # 制造环：A 的主管是 B，B 的主管是 A（DB 只禁止自环，不禁止多节点环）。
    a.manager_id = b.id
    session.flush()

    principal = Principal(employee_id=a.id, company_id=company.id)
    assert visible_employee_ids(session, principal, "team") == {a.id, b.id}


def test_company_scope_excludes_other_company(session: Session) -> None:
    """company 范围包含同公司全部员工，不包含另一家公司的员工。"""
    company = _company(session, "W1-4 co-a")
    e1 = _employee(session, company, "E-a1")
    e2 = _employee(session, company, "E-a2")
    e3 = _employee(session, company, "E-a3")

    other = _company(session, "W1-4 co-b")
    f1 = _employee(session, other, "E-b1")

    principal = Principal(employee_id=e1.id, company_id=company.id)
    visible = visible_employee_ids(session, principal, "company")

    assert visible == {e1.id, e2.id, e3.id}
    assert f1.id not in visible


def test_authorized_scope_is_self_only_placeholder(session: Session) -> None:
    """authorized 范围当前仅返回自身（占位实现，显式授权表尚未实现）。"""
    company = _company(session, "W1-4 authorized")
    employee = _employee(session, company, "E-authz")
    subordinate = _employee(session, company, "E-authz-sub", manager=employee)
    _grant(session, employee, "manager")
    principal = Principal(employee_id=employee.id, company_id=company.id)

    # 当前行为：authorized 只返回自身，即使其 team 范围包含下属。
    assert visible_employee_ids(session, principal, "authorized") == {employee.id}
    assert subordinate.id not in visible_employee_ids(session, principal, "authorized")


def test_unknown_scope_raises_value_error(session: Session) -> None:
    """未知 scope 属封闭集合之外，必须抛 ValueError 而非静默放行。"""
    company = _company(session, "W1-4 badscope")
    employee = _employee(session, company, "E-badscope")
    principal = Principal(employee_id=employee.id, company_id=company.id)

    with pytest.raises(ValueError):
        visible_employee_ids(session, principal, "galaxy")


def test_can_approve_rejects_self_and_allows_subordinate(session: Session) -> None:
    """自审防护：主管不能批自己的报销，但能批下属的报销。"""
    company = _company(session, "W1-4 approve")
    boss = _employee(session, company, "E-appr-boss")
    sub = _employee(session, company, "E-appr-sub", manager=boss)
    outsider = _employee(session, company, "E-appr-out")
    _grant(session, boss, "manager")
    principal = Principal(employee_id=boss.id, company_id=company.id)

    # 拥有 approve/team 权限，但仍不能批自己的。
    assert has_permission(session, principal, "expense_claim", "approve", "team") is True
    assert can_approve(session, principal, boss.id) is False
    # 下属在 team 范围内，可以批。
    assert can_approve(session, principal, sub.id) is True
    # 团队外的员工不在 team 范围内，不能批。
    assert can_approve(session, principal, outsider.id) is False


def test_employee_without_approve_cannot_approve(session: Session) -> None:
    """无审批权限的普通员工对任何人的报销都不能批。"""
    company = _company(session, "W1-4 noapprove")
    employee = _employee(session, company, "E-noapprove")
    other = _employee(session, company, "E-noapprove-other", manager=employee)
    _grant(session, employee, "employee")
    principal = Principal(employee_id=employee.id, company_id=company.id)

    assert can_approve(session, principal, employee.id) is False
    assert can_approve(session, principal, other.id) is False


def test_sysadmin_cannot_approve(session: Session) -> None:
    """系统管理员无财务批准权（PLAN §9），can_approve 恒为 False。"""
    company = _company(session, "W1-4 sysadmin")
    admin = _employee(session, company, "E-admin")
    subordinate = _employee(session, company, "E-admin-sub", manager=admin)
    _grant(session, admin, "sysadmin")
    principal = Principal(employee_id=admin.id, company_id=company.id)

    # sysadmin 只有 identity/channel_binding 管理权，没有 expense_claim/approve。
    assert has_permission(session, principal, "expense_claim", "approve", "team") is False
    assert can_approve(session, principal, admin.id) is False
    assert can_approve(session, principal, subordinate.id) is False
    assert effective_permissions(session, admin.id) == frozenset(
        {
            ("identity", "manage", "company"),
            ("channel_binding", "manage", "company"),
        }
    )


def test_role_permission_helpers_reuse_the_catalog(session: Session) -> None:
    """Governance reuses the existing RBAC primitives (no second model)."""
    company = _company(session, "W1-4 helpers")
    employee = _employee(session, company, "E-helpers")
    _grant(session, employee, "manager")

    role_ids = role_ids_for_employee(session, employee.id)
    assert role_ids == tuple(sorted(role_ids))
    assert effective_permissions_for_role_ids(session, role_ids) == (
        effective_permissions(session, employee.id)
    )
    assert effective_permissions_for_role_ids(session, ()) == frozenset()
