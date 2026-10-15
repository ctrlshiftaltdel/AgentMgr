"""Shared fixtures for the W1-14 end-to-end negative / privilege-escalation suite.

Every test here runs against the **real** PostgreSQL test database and a **real**
``fastapi.testclient.TestClient`` (no mocked authorization, no mocked database).
Isolation is provided by :func:`clean_state`, which truncates every business table
-- including the append-only ``audit_events`` -- before and after each test and
re-seeds the permission catalog, so the tests are order-independent and repeatable
under arbitrary pre-existing dirty data.

The organization built by :func:`world` covers all seven roles from PLAN §9:

===========================  ==================  ==========================
attribute                    role                notes
===========================  ==================  ==========================
``claimant``                 employee            subordinate of ``manager``
``outsider``                 employee            a second ordinary employee
``manager``                  manager             ``claimant``'s manager
``finance_operator``         finance_operator
``finance_lead``             finance_lead
``warehouse``                warehouse
``sysadmin``                 sysadmin
``auditor``                  auditor
===========================  ==================  ==========================
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.admin_auth import SESSION_COOKIE_NAME
from app.db.models.organization import Company, Department, Employee
from app.expense.models import CostCenter, ExpenseCategory
from app.identity.models import EmployeeRole, Role
from app.tests.conftest import (
    alembic_config,
    reset_business_state,
    seed_permission_catalog_if_schema_present,
)

#: Test-only internal token.  Never a production credential; supplied via env.
TEST_TOKEN = "w1-14-test-internal-token"

TOKEN_ENV = "AJAIMGR_INTERNAL_TOKEN"

#: The seven role names from ``templates/PLAN.md`` §9, in matrix column order.
ROLE_NAMES: tuple[str, ...] = (
    "employee",
    "manager",
    "finance_operator",
    "finance_lead",
    "warehouse",
    "sysadmin",
    "auditor",
)


# --- schema / isolation -------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """Ensure the test database is migrated to head before these tests run."""
    from alembic import command

    command.upgrade(alembic_config(), "head")


@pytest.fixture(scope="session")
def qa_engine(test_db_url: str) -> Iterator[Engine]:
    """A real engine bound to the dedicated test database."""
    engine = create_engine(test_db_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_state(qa_engine: Engine) -> Iterator[None]:
    """Truncate every business table around each test, then re-seed the catalog.

    ``TRUNCATE ... RESTART IDENTITY CASCADE`` empties ``audit_events`` too (its
    append-only trigger only blocks row-level UPDATE/DELETE), so nothing a test
    writes -- committed or not -- can leak into the next one.
    """
    reset_business_state(qa_engine)
    seed_permission_catalog_if_schema_present(qa_engine)
    try:
        yield
    finally:
        reset_business_state(qa_engine)
        seed_permission_catalog_if_schema_present(qa_engine)


# --- organization -------------------------------------------------------------


@dataclass
class World:
    """One company with all seven roles, a category and a cost centre."""

    company: Company
    department: Department
    claimant: Employee
    outsider: Employee
    manager: Employee
    finance_operator: Employee
    finance_lead: Employee
    warehouse: Employee
    sysadmin: Employee
    auditor: Employee
    category: ExpenseCategory
    cost_center: CostCenter
    #: role name -> that role's employee (the ``employee`` column uses ``outsider``)
    role_employee: dict[str, Employee]
    #: role name -> employee_no (for admin-console login)
    role_employee_no: dict[str, str]

    def matrix_roles(self) -> list[tuple[str, Employee, str]]:
        """The seven matrix rows as ``(role_name, employee, employee_no)``."""
        return [
            (role, self.role_employee[role], self.role_employee_no[role])
            for role in ROLE_NAMES
        ]


@pytest.fixture()
def world(qa_engine: Engine, clean_state: None) -> Iterator[World]:
    """Create and commit a fresh organization; the table reset removes it."""
    with Session(qa_engine, expire_on_commit=False) as session:
        company = Company(name="W1-14 QA")
        session.add(company)
        session.flush()
        department = Department(company_id=company.id, name="QA 部")
        session.add(department)
        session.flush()

        def _employee(no: str, role_name: str, *, manager: Employee | None = None) -> Employee:
            employee = Employee(
                company_id=company.id,
                department_id=department.id,
                manager_id=manager.id if manager is not None else None,
                employee_no=no,
                status="active",
            )
            session.add(employee)
            session.flush()
            role = session.scalar(select(Role).where(Role.name == role_name))
            assert role is not None, f"role {role_name!r} not seeded"
            session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))
            session.flush()
            return employee

        claimant = _employee("QA-claimant", "employee")
        manager = _employee("QA-manager", "manager")
        claimant.manager_id = manager.id  # claimant is in the manager's team
        session.flush()
        outsider = _employee("QA-outsider", "employee")
        finance_operator = _employee("QA-finop", "finance_operator")
        finance_lead = _employee("QA-finlead", "finance_lead")
        warehouse = _employee("QA-warehouse", "warehouse")
        sysadmin = _employee("QA-sysadmin", "sysadmin")
        auditor = _employee("QA-auditor", "auditor")

        category = ExpenseCategory(company_id=company.id, code="QA-CAT", name="差旅")
        cost_center = CostCenter(company_id=company.id, code="QA-CC", name="总部")
        session.add_all([category, cost_center])
        session.commit()

        role_employee = {
            "employee": outsider,
            "manager": manager,
            "finance_operator": finance_operator,
            "finance_lead": finance_lead,
            "warehouse": warehouse,
            "sysadmin": sysadmin,
            "auditor": auditor,
        }
        role_employee_no = {
            name: employee.employee_no for name, employee in role_employee.items()
        }

        built = World(
            company=company,
            department=department,
            claimant=claimant,
            outsider=outsider,
            manager=manager,
            finance_operator=finance_operator,
            finance_lead=finance_lead,
            warehouse=warehouse,
            sysadmin=sysadmin,
            auditor=auditor,
            category=category,
            cost_center=cost_center,
            role_employee=role_employee,
            role_employee_no=role_employee_no,
        )
    yield built


# --- clients ------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A ``TestClient`` with the internal token set; 500s are observable."""
    monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
    from app.api.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def admin_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A cookie-less ``TestClient`` that does not follow redirects."""
    monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
    from app.api.app import create_app

    with TestClient(
        create_app(), raise_server_exceptions=False, follow_redirects=False
    ) as test_client:
        yield test_client


@pytest.fixture()
def auth() -> Callable[..., dict[str, str]]:
    """A factory building bearer auth headers for an employee."""

    def _auth(employee: Employee, *, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {TEST_TOKEN if token is None else token}",
            "X-Employee-Id": str(employee.id),
        }

    return _auth


# --- HTTP helpers -------------------------------------------------------------


def create_claim(client: TestClient, headers: dict[str, str]) -> int:
    """Create a draft claim and return its id."""
    response = client.post("/claims", json={"currency": "CNY"}, headers=headers)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def add_line(
    client: TestClient,
    headers: dict[str, str],
    world: World,
    claim_id: int,
    *,
    amount: object = "100.00",
    tax: object = "0.00",
    description: str = "差旅打车",
    expense_date: object = "2026-10-01",
):
    """POST one claim line and return the raw response."""
    return client.post(
        f"/claims/{claim_id}/lines",
        json={
            "expense_date": expense_date,
            "category_id": world.category.id,
            "description": description,
            "amount": amount,
            "tax_amount": tax,
            "cost_center_id": world.cost_center.id,
        },
        headers=headers,
    )


def claim_with_line(
    client: TestClient,
    headers: dict[str, str],
    world: World,
    *,
    amount: str = "100.00",
) -> int:
    """Create a draft claim carrying exactly one line."""
    claim_id = create_claim(client, headers)
    response = add_line(client, headers, world, claim_id, amount=amount)
    assert response.status_code == 201, response.text
    return claim_id


def submit_claim(
    client: TestClient, headers: dict[str, str], claim_id: int
):
    return client.post(f"/claims/{claim_id}/submit", headers=headers)


def advance_to(
    client: TestClient, world: World, state: str, *, owner_headers: dict[str, str]
) -> int:
    """Drive a fresh claim to ``state`` through the public HTTP surface."""
    claim_id = claim_with_line(client, owner_headers, world)
    assert submit_claim(client, owner_headers, claim_id).status_code == 200
    if state == "submitted":
        return claim_id
    approved = client.post(
        f"/claims/{claim_id}/approve", headers=auth_headers(client, world.manager)
    )
    assert approved.status_code == 200, approved.text
    if state == "approved":
        return claim_id
    reviewed = client.post(
        f"/claims/{claim_id}/finance-review",
        headers=auth_headers(client, world.finance_operator),
    )
    assert reviewed.status_code == 200, reviewed.text
    if state == "finance_reviewed":
        return claim_id
    paid = client.post(
        f"/claims/{claim_id}/payments",
        json={"amount": "100.00"},
        headers=auth_headers(client, world.finance_lead),
    )
    assert paid.status_code == 201, paid.text
    assert state == "paid"
    return claim_id


def auth_headers(client: TestClient, employee: Employee) -> dict[str, str]:
    """Bearer headers for ``employee`` using the suite's fixed test token."""
    return {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "X-Employee-Id": str(employee.id),
    }


def admin_login(
    admin_client: TestClient, employee_no: str, *, token: str = TEST_TOKEN
) -> str:
    """Log in to the admin console and return the issued session cookie value."""
    response = admin_client.post(
        "/admin/login", data={"token": token, "employee_no": employee_no}
    )
    assert response.status_code == 303, response.text
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert cookie, "login did not set a session cookie"
    return cookie


def cookie_header(cookie: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"}
