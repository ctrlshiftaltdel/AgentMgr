"""Shared fixtures for the API-layer tests (task W1-11).

These tests exercise the real HTTP surface through ``fastapi.testclient`` against
the dedicated ``ajaimgr_test`` database.  They build their own organization data
(company / department / employees / roles / expense categories) and delete it
afterwards; ``audit_events`` is append-only (enforced by a DB trigger) so it is
never deleted — the committed deny/success events simply remain.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.expense.models import (
    CostCenter,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseLine,
    ReimbursementPayment,
)
from app.identity.models import EmployeeRole, Role
from app.identity.seed import clear_permission_catalog, seed_permission_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Test-only internal token. Never a production credential; supplied via env.
TEST_TOKEN = "w1-11-test-internal-token"

TOKEN_ENV = "AJAIMGR_INTERNAL_TOKEN"


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """Ensure the test database is migrated to head before these tests run."""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture(scope="session")
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@dataclass
class World:
    """A committed organization: one company, five roles, a category/cost center."""

    company: Company
    claimant: Employee
    manager: Employee
    outsider: Employee
    finance_operator: Employee
    finance_lead: Employee
    sysadmin: Employee
    category: ExpenseCategory
    cost_center: CostCenter


def _cleanup(session: Session) -> None:
    """Delete every business row these tests create (children before parents)."""
    session.execute(delete(ReimbursementPayment))
    session.execute(delete(ExpenseLine))
    session.execute(delete(ExpenseClaim))
    session.execute(delete(CostCenter))
    session.execute(delete(ExpenseCategory))
    session.execute(delete(EmployeeRole))
    clear_permission_catalog(session)
    session.execute(delete(Employee))
    session.execute(delete(Department))
    session.execute(delete(Company))
    session.commit()


def _grant(session: Session, employee: Employee, role_name: str) -> None:
    role = session.scalar(select(Role).where(Role.name == role_name))
    assert role is not None, f"role {role_name!r} not seeded"
    session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))


@pytest.fixture()
def world(engine: Engine) -> Iterator[World]:
    """Create and commit a fresh organization; remove it on teardown."""
    with Session(engine, expire_on_commit=False) as session:
        seed_permission_catalog(session)
        session.commit()

        company = Company(name="W1-11 API")
        session.add(company)
        session.flush()
        department = Department(company_id=company.id, name="财务部")
        session.add(department)
        session.flush()

        def _employee(no: str, *, manager: Employee | None = None) -> Employee:
            emp = Employee(
                company_id=company.id,
                department_id=department.id,
                manager_id=manager.id if manager is not None else None,
                employee_no=no,
                status="active",
            )
            session.add(emp)
            session.flush()
            return emp

        claimant = _employee("E-claimant")
        manager = _employee("E-manager")
        claimant.manager_id = manager.id
        outsider = _employee("E-outsider")
        finance_operator = _employee("E-finop")
        finance_lead = _employee("E-finlead")
        sysadmin = _employee("E-sysadmin")

        _grant(session, claimant, "employee")
        _grant(session, manager, "manager")
        _grant(session, outsider, "employee")
        _grant(session, finance_operator, "finance_operator")
        _grant(session, finance_lead, "finance_lead")
        _grant(session, sysadmin, "sysadmin")

        category = ExpenseCategory(company_id=company.id, code="CAT-TRV", name="差旅")
        cost_center = CostCenter(company_id=company.id, code="CC-01", name="总部")
        session.add_all([category, cost_center])
        session.commit()

        world = World(
            company=company,
            claimant=claimant,
            manager=manager,
            outsider=outsider,
            finance_operator=finance_operator,
            finance_lead=finance_lead,
            sysadmin=sysadmin,
            category=category,
            cost_center=cost_center,
        )

    try:
        yield world
    finally:
        with Session(engine) as session:
            _cleanup(session)


@pytest.fixture()
def api_token() -> str:
    return TEST_TOKEN


@pytest.fixture()
def client(api_token: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose app sees a configured internal token."""
    monkeypatch.setenv(TOKEN_ENV, api_token)
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def client_without_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose app has **no** internal token configured (fail closed)."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def auth(api_token: str) -> Callable[..., dict[str, str]]:
    """Return a factory building auth headers for an employee."""

    def _auth(employee: Employee, *, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_token if token is None else token}",
            "X-Employee-Id": str(employee.id),
        }

    return _auth
