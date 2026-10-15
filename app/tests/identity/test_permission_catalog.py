"""W1-3 permission catalog / role matrix tests against the test database.

Covers the acceptance criteria of task W1-3:

1. counts: 7 roles, 19 permissions, 31 role-permission mappings;
2. PLAN §9 mandatory rules: ``sysadmin`` holds neither ``expense_claim/approve``
   nor ``expense_claim/finance_review``; ``auditor`` is read-only (no create /
   submit / approve / finance_review / payment);
3. matrix spot checks (manager / employee / finance_lead);
4. idempotency: seeding twice in one session leaves the three tables unchanged;
5. cleanup: ``clear_permission_catalog`` empties the three seed tables without
   touching other tables such as ``employee_channel_bindings``.

Every test seeds its own data and the session fixture removes the catalog seed
and any organization rows it created, so nothing is left behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.identity.models import (
    EmployeeChannelBinding,
    EmployeeRole,
    Permission,
    Role,
    RolePermission,
)
from app.identity.permissions import (
    AUDITOR_FORBIDDEN_ACTIONS,
    EXPECTED_PERMISSION_COUNT,
    EXPECTED_ROLE_COUNT,
    EXPECTED_ROLE_PERMISSION_COUNT,
    PERMISSIONS,
    ROLES,
    ROLE_PERMISSION_NUMBERS,
    SYSADMIN_FORBIDDEN,
    validate_catalog,
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
    """Delete every row these tests could have created (children first)."""
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
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


def _count(session: Session, model: type, **filters: object) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return session.scalar(stmt)


def _seed(session: Session) -> None:
    """Reset to an empty catalog, then seed it (deterministic per test)."""
    clear_permission_catalog(session)
    session.commit()
    seed_permission_catalog(session)
    session.commit()


def _role_permission_keys(session: Session, role_name: str) -> set[tuple[str, str, str]]:
    """Return the ``(resource, action, scope)`` triples granted to a role."""
    stmt = (
        select(Permission.resource, Permission.action, Permission.scope)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.name == role_name)
    )
    return {(resource, action, scope) for resource, action, scope in session.execute(stmt)}


def test_catalog_is_self_consistent() -> None:
    """The pure catalog satisfies PLAN §9 before any row reaches the database."""
    validate_catalog()
    assert len(ROLES) == EXPECTED_ROLE_COUNT
    assert len(PERMISSIONS) == EXPECTED_PERMISSION_COUNT
    assert sum(len(v) for v in ROLE_PERMISSION_NUMBERS.values()) == (
        EXPECTED_ROLE_PERMISSION_COUNT
    )


def test_seed_counts(session: Session) -> None:
    """7 roles, 19 permissions, 31 mappings land in the database."""
    _seed(session)

    assert _count(session, Role) == EXPECTED_ROLE_COUNT
    assert _count(session, Permission) == EXPECTED_PERMISSION_COUNT
    assert _count(session, RolePermission) == EXPECTED_ROLE_PERMISSION_COUNT


def test_sysadmin_has_no_financial_approval(session: Session) -> None:
    """PLAN §9: administrators do not automatically gain financial approval."""
    _seed(session)

    keys = _role_permission_keys(session, "sysadmin")
    assert keys == {
        ("identity", "manage", "company"),
        ("channel_binding", "manage", "company"),
    }
    for resource, action in SYSADMIN_FORBIDDEN:
        assert (resource, action, "company") not in keys
        assert not any(k[0] == resource and k[1] == action for k in keys)


def test_auditor_is_read_only(session: Session) -> None:
    """PLAN §9: the auditor role is read-only."""
    _seed(session)

    keys = _role_permission_keys(session, "auditor")
    assert keys == {
        ("expense_claim", "read", "authorized"),
        ("invoice", "read", "authorized"),
        ("inventory", "read", "authorized"),
        ("audit", "read", "authorized"),
    }
    for _, action, _ in keys:
        assert action not in AUDITOR_FORBIDDEN_ACTIONS


def test_matrix_spot_checks(session: Session) -> None:
    """Spot-check the matrix for manager / employee / finance_lead."""
    _seed(session)

    manager = _role_permission_keys(session, "manager")
    assert ("expense_claim", "approve", "team") in manager
    assert ("expense_claim", "read", "company") not in manager

    employee = _role_permission_keys(session, "employee")
    assert ("expense_claim", "read", "company") not in employee
    assert employee == {
        ("expense_claim", "create", "own"),
        ("expense_claim", "read", "own"),
        ("expense_claim", "submit", "own"),
        ("expense_claim", "withdraw", "own"),
    }

    finance_lead = _role_permission_keys(session, "finance_lead")
    assert ("payment", "record", "company") in finance_lead


def test_seed_is_idempotent(session: Session) -> None:
    """Seeding twice in one session must not change the three table counts."""
    _seed(session)

    before = (
        _count(session, Role),
        _count(session, Permission),
        _count(session, RolePermission),
    )

    created = seed_permission_catalog(session)
    session.commit()

    after = (
        _count(session, Role),
        _count(session, Permission),
        _count(session, RolePermission),
    )
    assert created == {"roles": 0, "permissions": 0, "role_permissions": 0}
    assert after == before == (
        EXPECTED_ROLE_COUNT,
        EXPECTED_PERMISSION_COUNT,
        EXPECTED_ROLE_PERMISSION_COUNT,
    )


def test_clear_removes_seed_only(session: Session) -> None:
    """Cleanup empties the seed tables and leaves other tables untouched."""
    # An unrelated business row that must survive the catalog cleanup.
    company = Company(name="W1-3 clear")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name="Dept clear")
    session.add(department)
    session.flush()
    employee = Employee(
        company_id=company.id,
        department_id=department.id,
        employee_no="E-clear",
    )
    session.add(employee)
    session.flush()
    session.add(
        EmployeeChannelBinding(
            employee_id=employee.id,
            channel_type="weixin",
            external_user_id="wx-clear-1",
        )
    )
    session.commit()

    _seed(session)
    assert _count(session, Role) == EXPECTED_ROLE_COUNT

    clear_permission_catalog(session)
    session.commit()

    assert _count(session, Role) == 0
    assert _count(session, Permission) == 0
    assert _count(session, RolePermission) == 0
    # Other tables are untouched by the catalog cleanup.
    assert _count(session, EmployeeChannelBinding, employee_id=employee.id) == 1
    assert _count(session, Employee, id=employee.id) == 1
