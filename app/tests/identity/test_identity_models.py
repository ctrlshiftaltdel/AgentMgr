"""W1-2 identity/RBAC model tests against the dedicated test database.

Covers the constraints fixed by the task:

1. duplicate ``(channel_type, external_user_id)`` bindings are rejected, while the
   same ``external_user_id`` under a *different* channel is allowed (the composite
   identity key of W0-1 §6.5 must not collapse to a single-column unique);
2. deleting an employee cascades ``employee_channel_bindings`` and
   ``employee_roles``;
3. an illegal ``permissions.scope`` is rejected by the CHECK constraint;
4. deleting a permission cascades ``role_permissions``.

Every test builds its own organization data and the session fixture removes all
business rows afterwards, so nothing is left behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.identity.models import (
    EmployeeChannelBinding,
    EmployeeRole,
    Permission,
    Role,
    RolePermission,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

VALID_SCOPES = ("own", "team", "company", "authorized")


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """Make sure the test database is migrated to head before these tests."""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """Delete every business row created by these tests (children first)."""
    for model in (
        EmployeeChannelBinding,
        EmployeeRole,
        RolePermission,
        Role,
        Permission,
        Employee,
        Department,
        Company,
    ):
        session.execute(delete(model))


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


def _make_org(session: Session, *, suffix: str) -> tuple[Company, Department, Employee]:
    company = Company(name=f"W1-2 {suffix}")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name=f"Dept {suffix}")
    session.add(department)
    session.flush()
    employee = Employee(
        company_id=company.id,
        department_id=department.id,
        employee_no=f"E-{suffix}",
    )
    session.add(employee)
    session.flush()
    return company, department, employee


def _count(session: Session, model: type, **filters: object) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return session.scalar(stmt)


def test_duplicate_channel_binding_rejected(session: Session) -> None:
    _, _, employee = _make_org(session, suffix="dup")

    session.add(
        EmployeeChannelBinding(
            employee_id=employee.id,
            channel_type="weixin",
            external_user_id="wx-dup-1",
        )
    )
    session.commit()

    # Same composite key again -> rejected by the unique constraint.
    session.add(
        EmployeeChannelBinding(
            employee_id=employee.id,
            channel_type="weixin",
            external_user_id="wx-dup-1",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # Same external_user_id on a *different* channel is a distinct identity and
    # must be accepted (proves the key is the composite (channel, user id)).
    session.add(
        EmployeeChannelBinding(
            employee_id=employee.id,
            channel_type="dingtalk",
            external_user_id="wx-dup-1",
        )
    )
    session.commit()

    assert _count(session, EmployeeChannelBinding, employee_id=employee.id) == 2


def test_employee_delete_cascades_bindings_and_roles(session: Session) -> None:
    _, _, employee = _make_org(session, suffix="cascade")

    role = Role(name="cascade-role")
    session.add(role)
    session.flush()
    session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))
    session.add(
        EmployeeChannelBinding(
            employee_id=employee.id,
            channel_type="weixin",
            external_user_id="wx-cascade",
        )
    )
    session.commit()

    assert _count(session, EmployeeChannelBinding, employee_id=employee.id) == 1
    assert _count(session, EmployeeRole, employee_id=employee.id) == 1

    session.execute(delete(Employee).where(Employee.id == employee.id))
    session.commit()

    assert _count(session, EmployeeChannelBinding, employee_id=employee.id) == 0
    assert _count(session, EmployeeRole, employee_id=employee.id) == 0
    # The role itself is untouched by the cascade.
    assert _count(session, Role, id=role.id) == 1


def test_employee_delete_sets_granted_by_null(session: Session) -> None:
    _, _, grantor = _make_org(session, suffix="grantor")
    _, _, grantee = _make_org(session, suffix="grantee")

    role = Role(name="granted-by-role")
    session.add(role)
    session.flush()
    session.add(
        EmployeeRole(employee_id=grantee.id, role_id=role.id, granted_by=grantor.id)
    )
    session.commit()

    session.execute(delete(Employee).where(Employee.id == grantor.id))
    session.commit()

    remaining = session.scalar(
        select(EmployeeRole).where(EmployeeRole.employee_id == grantee.id)
    )
    assert remaining is not None
    assert remaining.granted_by is None


def test_permission_scope_check_rejects_invalid(session: Session) -> None:
    session.add(Permission(resource="expense", action="view", scope="global"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # Every documented scope value is accepted.
    for scope in VALID_SCOPES:
        session.add(Permission(resource="expense", action="view", scope=scope))
    session.commit()

    assert _count(session, Permission, resource="expense", action="view") == len(
        VALID_SCOPES
    )


def test_permission_delete_cascades_role_permissions(session: Session) -> None:
    role = Role(name="perm-cascade-role")
    permission = Permission(resource="expense", action="approve", scope="team")
    session.add_all([role, permission])
    session.flush()
    session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    session.commit()

    assert _count(session, RolePermission, permission_id=permission.id) == 1

    session.execute(delete(Permission).where(Permission.id == permission.id))
    session.commit()

    assert _count(session, RolePermission, permission_id=permission.id) == 0
    # The role itself survives.
    assert _count(session, Role, id=role.id) == 1
