"""W1-14 §1/§2: the role x action privilege matrix and separation of duties.

Every cell runs a real HTTP request through ``fastapi.testclient`` against the
real PostgreSQL test database.  Each cell starts from a **fresh** claim owned by
``world.claimant`` in the state the action requires, so one cell can never mutate
the state another cell depends on.  When a cell is expected to be denied we also
assert that the business state is byte-for-byte unchanged (status, total, line
count, payment count).

The matrix below is the executable expectation; the ``A``/``D`` marks are the
asserted outcomes and the parametrized tests verify every cell.

+------------------+------+----------+--------+----------+---------+------------+-----+-----+------+--------------+
| role             | read | add_line | submit | withdraw | approve | fin_review | pay | pdf | xlsx | admin/claims |
+==================+======+==========+========+==========+=========+============+=====+=====+======+==============+
| 普通员工 employee  |  D   |    D     |   D    |    D     |    D    |     D      |  D  |  D  |  D   |      A       |
| 主管 manager      |  A   |    D     |   D    |    D     |    A    |     D      |  D  |  A  |  A   |      A       |
| 财务经办 fin_op    |  A   |    D     |   D    |    D     |    D    |     A      |  D  |  A  |  A   |      A       |
| 财务主管 fin_lead  |  A   |    D     |   D    |    D     |    D    |     A      |  A  |  A  |  A   |      A       |
| 仓储采购 warehouse |  D   |    D     |   D    |    D     |    D    |     D      |  D  |  D  |  D   |      A       |
| 系统管理员 sysadmin|  D   |    D     |   D    |    D     |    D    |     D      |  D  |  D  |  D   |      D       |
| 审计人员 auditor   |  D   |    D     |   D    |    D     |    D    |     D      |  D  |  D  |  D   |      A       |
+------------------+------+----------+--------+----------+---------+------------+-----+-----+------+--------------+

Notes on the expected outcomes:

* ``read`` / ``pdf`` / ``xlsx`` are allowed exactly when the role's widest
  ``expense_claim/read`` scope reaches the claimant (``manager`` → team,
  ``finance_*`` → company).  ``employee``/``warehouse`` hold ``read/own`` and
  ``auditor`` holds the placeholder ``read/authorized`` (self only), so all three
  are denied on another employee's claim.
* ``add_line`` / ``submit`` / ``withdraw`` are owner-only: no role but the owner
  (``claimant``) may act, so every row is denied.
* ``approve`` requires ``expense_claim/approve/team`` and a team-scoped,
  non-self claim owner: only ``manager`` is allowed.
* ``finance_review`` requires ``expense_claim/finance_review/company``: the two
  finance roles are allowed, everyone else denied.
* ``pay`` requires ``payment/record/company``: only ``finance_lead``.
* ``admin/claims`` needs some ``expense_claim/read`` scope; ``sysadmin`` holds
  none and gets the 403 page.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.expense.models import ExpenseClaim, ExpenseLine, ReimbursementPayment
from app.tests.qa.conftest import (
    ROLE_NAMES,
    TEST_TOKEN,
    World,
    add_line,
    admin_login,
    auth_headers,
    claim_with_line,
    cookie_header,
    create_claim,
    submit_claim,
)

ALLOW = "allow"
DENY = "deny"

ACTIONS: tuple[str, ...] = (
    "read_other_claim",
    "add_line",
    "submit",
    "withdraw",
    "approve",
    "finance_review",
    "record_payment",
    "export_pdf",
    "export_xlsx",
    "admin_claims_page",
)

#: The asserted matrix (role -> action -> ALLOW/DENY).
EXPECTED: dict[str, dict[str, str]] = {
    "employee": dict.fromkeys(ACTIONS, DENY) | {"admin_claims_page": ALLOW},
    "manager": {
        "read_other_claim": ALLOW,
        "add_line": DENY,
        "submit": DENY,
        "withdraw": DENY,
        "approve": ALLOW,
        "finance_review": DENY,
        "record_payment": DENY,
        "export_pdf": ALLOW,
        "export_xlsx": ALLOW,
        "admin_claims_page": ALLOW,
    },
    "finance_operator": {
        "read_other_claim": ALLOW,
        "add_line": DENY,
        "submit": DENY,
        "withdraw": DENY,
        "approve": DENY,
        "finance_review": ALLOW,
        "record_payment": DENY,
        "export_pdf": ALLOW,
        "export_xlsx": ALLOW,
        "admin_claims_page": ALLOW,
    },
    "finance_lead": {
        "read_other_claim": ALLOW,
        "add_line": DENY,
        "submit": DENY,
        "withdraw": DENY,
        "approve": DENY,
        "finance_review": ALLOW,
        "record_payment": ALLOW,
        "export_pdf": ALLOW,
        "export_xlsx": ALLOW,
        "admin_claims_page": ALLOW,
    },
    "warehouse": dict.fromkeys(ACTIONS, DENY) | {"admin_claims_page": ALLOW},
    "sysadmin": dict.fromkeys(ACTIONS, DENY),
    "auditor": dict.fromkeys(ACTIONS, DENY) | {"admin_claims_page": ALLOW},
}

#: State a claim owned by ``claimant`` must be in for each action.
_REQUIRED_STATE = {
    "read_other_claim": "draft",
    "add_line": "draft",
    "submit": "draft",
    "withdraw": "submitted",
    "approve": "submitted",
    "finance_review": "approved",
    "record_payment": "finance_reviewed",
    "export_pdf": "draft",
    "export_xlsx": "draft",
}


def _claim_in_state(
    client: TestClient, world: World, state: str, *, owner_headers: dict[str, str]
) -> int:
    """Build a claim owned by the claimant in ``state`` via the HTTP surface."""
    claim_id = claim_with_line(client, owner_headers, world)
    if state == "draft":
        return claim_id
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
    assert state == "finance_reviewed"
    return claim_id


def _snapshot(engine: Engine, claim_id: int) -> tuple:
    """(status, total_amount, line_count, payment_count) straight from the DB."""
    with Session(engine) as session:
        claim = session.get(ExpenseClaim, claim_id)
        assert claim is not None
        lines = session.scalar(
            select(func.count())
            .select_from(ExpenseLine)
            .where(ExpenseLine.claim_id == claim_id)
        )
        payments = session.scalar(
            select(func.count())
            .select_from(ReimbursementPayment)
            .where(ReimbursementPayment.claim_id == claim_id)
        )
        return (claim.status, claim.total_amount, lines, payments)


def _perform(
    client: TestClient,
    world: World,
    action: str,
    claim_id: int,
    headers: dict[str, str],
):
    """Issue the HTTP request for ``action`` as the acting employee."""
    if action == "read_other_claim":
        return client.get(f"/claims/{claim_id}", headers=headers)
    if action == "add_line":
        return add_line(client, headers, world, claim_id, amount="1.00")
    if action == "submit":
        return client.post(f"/claims/{claim_id}/submit", headers=headers)
    if action == "withdraw":
        return client.post(f"/claims/{claim_id}/withdraw", headers=headers)
    if action == "approve":
        return client.post(f"/claims/{claim_id}/approve", headers=headers)
    if action == "finance_review":
        return client.post(f"/claims/{claim_id}/finance-review", headers=headers)
    if action == "record_payment":
        return client.post(
            f"/claims/{claim_id}/payments",
            json={"amount": "100.00"},
            headers=headers,
        )
    if action == "export_pdf":
        return client.get(f"/claims/{claim_id}/export/pdf", headers=headers)
    if action == "export_xlsx":
        return client.get(f"/claims/{claim_id}/export/xlsx", headers=headers)
    raise AssertionError(f"unknown action {action!r}")


@pytest.mark.parametrize("role", ROLE_NAMES)
@pytest.mark.parametrize("action", ACTIONS)
def test_privilege_matrix(
    client: TestClient,
    admin_client: TestClient,
    world: World,
    qa_engine: Engine,
    role: str,
    action: str,
) -> None:
    """One role x action cell: assert allow/deny and, on deny, unchanged state."""
    employee = world.role_employee[role]
    expected = EXPECTED[role][action]

    if action == "admin_claims_page":
        cookie = admin_login(admin_client, world.role_employee_no[role])
        response = admin_client.get("/admin/claims", headers=cookie_header(cookie))
        if expected == ALLOW:
            assert response.status_code == 200, (role, action, response.status_code)
        else:
            assert response.status_code == 403, (role, action, response.status_code)
        return

    owner_headers = auth_headers(client, world.claimant)
    claim_id = _claim_in_state(
        client, world, _REQUIRED_STATE[action], owner_headers=owner_headers
    )
    before = _snapshot(qa_engine, claim_id)

    response = _perform(
        client, world, action, claim_id, auth_headers(client, employee)
    )

    if expected == ALLOW:
        assert response.status_code < 400, (role, action, response.status_code, response.text)
    else:
        assert response.status_code == 403, (
            role,
            action,
            response.status_code,
            response.text,
        )
        assert response.json()["error"]["code"] == "authorization_denied", response.text
        # Denial must leave the business state exactly as it was.
        assert _snapshot(qa_engine, claim_id) == before, (role, action)


def test_matrix_covers_every_role_and_action() -> None:
    """The expected table has no holes."""
    assert set(EXPECTED) == set(ROLE_NAMES)
    for role, row in EXPECTED.items():
        assert set(row) == set(ACTIONS), role


# --- §2 separation of duties --------------------------------------------------


def test_manager_cannot_approve_own_claim(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """主管不能审批自己的报销 (PLAN §9)."""
    manager = auth_headers(client, world.manager)
    claim_id = create_claim(client, manager)
    assert add_line(client, manager, world, claim_id, amount="80.00").status_code == 201
    assert submit_claim(client, manager, claim_id).status_code == 200

    before = _snapshot(qa_engine, claim_id)
    response = client.post(f"/claims/{claim_id}/approve", headers=manager)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"
    assert _snapshot(qa_engine, claim_id) == before


def test_finance_lead_cannot_approve(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """财务主管无 ``expense_claim/approve`` 权限，审批被拒且状态不变."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200
    before = _snapshot(qa_engine, claim_id)

    response = client.post(
        f"/claims/{claim_id}/approve", headers=auth_headers(client, world.finance_lead)
    )
    assert response.status_code == 403
    assert _snapshot(qa_engine, claim_id) == before


def test_sysadmin_cannot_approve_or_finance_review(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """管理员不因管理服务器而获得财务批准权 (PLAN §9 强制规则)."""
    owner = auth_headers(client, world.claimant)
    submitted = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, submitted).status_code == 200

    sysadmin = auth_headers(client, world.sysadmin)
    before = _snapshot(qa_engine, submitted)
    approve = client.post(f"/claims/{submitted}/approve", headers=sysadmin)
    assert approve.status_code == 403
    assert _snapshot(qa_engine, submitted) == before

    # finance_review on an approved claim, also denied for the sysadmin.
    approved = _claim_in_state(client, world, "approved", owner_headers=owner)
    before_review = _snapshot(qa_engine, approved)
    review = client.post(f"/claims/{approved}/finance-review", headers=sysadmin)
    assert review.status_code == 403
    assert _snapshot(qa_engine, approved) == before_review


def test_auditor_has_no_write_action(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """审计人员只读：创建/加明细/提交/撤回/审批/复核/付款全部被拒且状态不变."""
    auditor = auth_headers(client, world.auditor)

    # create is denied outright (no expense_claim/create permission).
    create = client.post("/claims", json={"currency": "CNY"}, headers=auditor)
    assert create.status_code == 403

    owner = auth_headers(client, world.claimant)
    draft = claim_with_line(client, owner, world)
    submitted = _claim_in_state(client, world, "submitted", owner_headers=owner)
    approved = _claim_in_state(client, world, "approved", owner_headers=owner)
    reviewed = _claim_in_state(client, world, "finance_reviewed", owner_headers=owner)

    attempts = [
        ("add_line", add_line(client, auditor, world, draft, amount="9.99")),
        ("submit", client.post(f"/claims/{draft}/submit", headers=auditor)),
        ("withdraw", client.post(f"/claims/{submitted}/withdraw", headers=auditor)),
        ("approve", client.post(f"/claims/{submitted}/approve", headers=auditor)),
        ("finance_review", client.post(f"/claims/{approved}/finance-review", headers=auditor)),
        (
            "record_payment",
            client.post(
                f"/claims/{reviewed}/payments",
                json={"amount": "1.00"},
                headers=auditor,
            ),
        ),
    ]
    snapshots = {
        claim_id: _snapshot(qa_engine, claim_id)
        for claim_id in (draft, submitted, approved, reviewed)
    }
    for name, response in attempts:
        assert response.status_code == 403, (name, response.status_code, response.text)
    for claim_id, snap in snapshots.items():
        assert _snapshot(qa_engine, claim_id) == snap, claim_id


def test_auditor_permissions_are_read_only(world: World, qa_engine: Engine) -> None:
    """The auditor's effective permissions contain no write action (PLAN §9)."""
    from app.identity.authorization import effective_permissions

    with Session(qa_engine) as session:
        permissions = effective_permissions(session, world.auditor.id)
    write_actions = {"create", "submit", "approve", "finance_review", "payment", "withdraw"}
    assert permissions, "auditor must hold read permissions"
    assert all(action == "read" for _, action, _ in permissions)
    assert all(action not in write_actions for _, action, _ in permissions)


def test_claimant_owner_controls_are_allowed(
    client: TestClient, world: World
) -> None:
    """Positive control: the owner *can* add a line, submit and withdraw."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    assert add_line(client, owner, world, claim_id, amount="12.34").status_code == 201
    assert submit_claim(client, owner, claim_id).status_code == 200
    withdraw = client.post(f"/claims/{claim_id}/withdraw", headers=owner)
    assert withdraw.status_code == 200
    assert withdraw.json()["status"] == "withdrawn"


def test_denied_read_does_not_leak_claim_content(
    client: TestClient, world: World
) -> None:
    """A denied read returns a uniform 403 body with no claim data."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world, amount="123.45")
    claim_no = client.get(f"/claims/{claim_id}", headers=owner).json()["claim_no"]

    response = client.get(
        f"/claims/{claim_id}", headers=auth_headers(client, world.sysadmin)
    )
    assert response.status_code == 403
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert claim_no not in response.text
    assert "123.45" not in response.text


def test_finance_operator_cannot_record_payment(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """付款登记需 ``payment/record/company``：财务经办被拒，财务主管可登记."""
    owner = auth_headers(client, world.claimant)
    reviewed = _claim_in_state(
        client, world, "finance_reviewed", owner_headers=owner
    )
    before = _snapshot(qa_engine, reviewed)

    denied = client.post(
        f"/claims/{reviewed}/payments",
        json={"amount": "100.00"},
        headers=auth_headers(client, world.finance_operator),
    )
    assert denied.status_code == 403
    assert _snapshot(qa_engine, reviewed) == before

    allowed = client.post(
        f"/claims/{reviewed}/payments",
        json={"amount": "100.00"},
        headers=auth_headers(client, world.finance_lead),
    )
    assert allowed.status_code == 201
    assert _snapshot(qa_engine, reviewed)[0] == "paid"
    assert _snapshot(qa_engine, reviewed)[3] == 1


def test_cross_company_manager_cannot_read_or_approve(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A manager from another company is outside team/company scope → denied."""
    from app.db.models.organization import Company, Department, Employee
    from app.identity.models import EmployeeRole, Role

    with Session(qa_engine, expire_on_commit=False) as session:
        other_company = Company(name="W1-14 other")
        session.add(other_company)
        session.flush()
        department = Department(company_id=other_company.id, name="Other")
        session.add(department)
        session.flush()
        foreign_manager = Employee(
            company_id=other_company.id,
            department_id=department.id,
            employee_no="QA-foreign-manager",
        )
        session.add(foreign_manager)
        session.flush()
        role = session.scalar(select(Role).where(Role.name == "manager"))
        session.add(EmployeeRole(employee_id=foreign_manager.id, role_id=role.id))
        session.commit()
        foreign_id = foreign_manager.id

    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200
    before = _snapshot(qa_engine, claim_id)

    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "X-Employee-Id": str(foreign_id),
    }
    read = client.get(f"/claims/{claim_id}", headers=headers)
    approve = client.post(f"/claims/{claim_id}/approve", headers=headers)
    assert read.status_code == 403
    assert approve.status_code == 403
    assert _snapshot(qa_engine, claim_id) == before


def test_denied_export_matches_denied_read(client: TestClient, world: World) -> None:
    """Export endpoints reuse the read authorization (denied for outsiders)."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world, amount="55.55")
    outsider = auth_headers(client, world.outsider)
    for fmt in ("pdf", "xlsx"):
        response = client.get(f"/claims/{claim_id}/export/{fmt}", headers=outsider)
        assert response.status_code == 403, fmt
        assert response.json()["error"]["code"] == "authorization_denied"


def test_total_amount_is_decimal_not_float(client: TestClient, world: World) -> None:
    """Amounts round-trip as exact decimals (no binary-float artefacts)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    assert add_line(client, owner, world, claim_id, amount="0.10").status_code == 201
    assert add_line(client, owner, world, claim_id, amount="0.20").status_code == 201
    detail = client.get(f"/claims/{claim_id}", headers=owner).json()
    assert detail["total_amount"] == "0.30"
    assert Decimal(detail["total_amount"]) == Decimal("0.30")
