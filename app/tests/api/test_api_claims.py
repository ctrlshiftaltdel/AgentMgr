"""End-to-end and negative tests for the expense-claim HTTP endpoints (W1-11).

Covers acceptance points 3, 4, 5, 6 and 7:

3. a valid principal can create a claim (201);
4. cross-employee access → 403 with unchanged business state;
5. invalid transition → 409; empty-claim submit → 422;
6. HTTP happy path: create → line → submit → manager approve → finance review →
   payment, all with a real TestClient and token;
7. list scoping: an ordinary employee sees only their own claims, a manager sees
   their team members' claims.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.expense.models import ExpenseClaim
from app.tests.api.conftest import World


def _create_claim(client: TestClient, headers: dict[str, str]) -> int:
    response = client.post("/claims", json={"currency": "CNY"}, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "draft"
    return int(response.json()["id"])


def _add_line(
    client: TestClient,
    headers: dict[str, str],
    world: World,
    claim_id: int,
    *,
    amount: str,
    tax: str = "0.00",
    description: str = "出差打车",
) -> None:
    response = client.post(
        f"/claims/{claim_id}/lines",
        json={
            "expense_date": "2026-10-01",
            "category_id": world.category.id,
            "description": description,
            "amount": amount,
            "tax_amount": tax,
            "cost_center_id": world.cost_center.id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


def _submitted_claim(
    client: TestClient,
    headers: dict[str, str],
    world: World,
    *,
    amount: str = "100.10",
) -> int:
    claim_id = _create_claim(client, headers)
    _add_line(client, headers, world, claim_id, amount=amount)
    response = client.post(f"/claims/{claim_id}/submit", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "submitted"
    return claim_id


# --- 3. create (201) ----------------------------------------------------------


def test_create_claim_returns_201(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    response = client.post(
        "/claims", json={"currency": "CNY"}, headers=auth(world.claimant)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["employee_id"] == world.claimant.id
    assert body["status"] == "draft"
    assert body["total_amount"] == "0.00"
    assert body["claim_no"].startswith("EC-")


# --- 4. cross-employee --------------------------------------------------------


def test_cross_employee_read_returns_403_and_state_unchanged(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    claimant = auth(world.claimant)
    outsider = auth(world.outsider)
    claim_id = _create_claim(client, claimant)
    _add_line(client, claimant, world, claim_id, amount="100.10")

    response = client.get(f"/claims/{claim_id}", headers=outsider)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"

    # Outsider also cannot add a line or submit.
    assert (
        client.post(
            f"/claims/{claim_id}/lines",
            json={
                "expense_date": "2026-10-02",
                "category_id": world.category.id,
                "description": "越权写入",
                "amount": "999.00",
            },
            headers=outsider,
        ).status_code
        == 403
    )
    assert client.post(f"/claims/{claim_id}/submit", headers=outsider).status_code == 403

    # Business state unchanged.
    with Session(engine) as session:
        claim = session.get(ExpenseClaim, claim_id)
        assert claim is not None
        assert claim.status == "draft"
        assert claim.total_amount == Decimal("100.10")


def test_cross_employee_approve_returns_403(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    claim_id = _submitted_claim(client, auth(world.claimant), world)
    # Outsider (no approve permission) cannot approve.
    response = client.post(f"/claims/{claim_id}/approve", headers=auth(world.outsider))
    assert response.status_code == 403
    with Session(engine) as session:
        assert session.get(ExpenseClaim, claim_id).status == "submitted"


# --- 5. invalid transition (409) / empty submit (422) -------------------------


def test_invalid_transition_returns_409(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    claimant = auth(world.claimant)
    claim_id = _submitted_claim(client, claimant, world)
    # A second submit from 'submitted' is not allowed.
    response = client.post(f"/claims/{claim_id}/submit", headers=claimant)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"


def test_empty_claim_submit_returns_422(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    claimant = auth(world.claimant)
    claim_id = _create_claim(client, claimant)
    response = client.post(f"/claims/{claim_id}/submit", headers=claimant)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_self_approval_returns_403(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    manager = auth(world.manager)
    # Manager files their own claim, then cannot approve it (self-approval).
    claim_id = _create_claim(client, manager)
    _add_line(client, manager, world, claim_id, amount="50.00")
    assert client.post(f"/claims/{claim_id}/submit", headers=manager).status_code == 200
    response = client.post(f"/claims/{claim_id}/approve", headers=manager)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"


# --- 6. HTTP happy path -------------------------------------------------------


def test_http_happy_path_end_to_end(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    claimant = auth(world.claimant)
    manager = auth(world.manager)
    finance_operator = auth(world.finance_operator)
    finance_lead = auth(world.finance_lead)

    claim_id = _create_claim(client, claimant)
    _add_line(client, claimant, world, claim_id, amount="100.10", tax="3.00", description="高铁票")
    _add_line(client, claimant, world, claim_id, amount="200.20", description="打车")

    # Exact decimal total via the API.
    detail = client.get(f"/claims/{claim_id}", headers=claimant)
    assert detail.status_code == 200
    assert detail.json()["total_amount"] == "300.30"

    submitted = client.post(f"/claims/{claim_id}/submit", headers=claimant)
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"
    assert submitted.json()["submitted_at"] is not None

    approved = client.post(f"/claims/{claim_id}/approve", headers=manager)
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    reviewed = client.post(f"/claims/{claim_id}/finance-review", headers=finance_operator)
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "finance_reviewed"

    paid = client.post(
        f"/claims/{claim_id}/payments",
        json={"amount": "300.30", "payment_ref": "PAY-0001"},
        headers=finance_lead,
    )
    assert paid.status_code == 201
    assert paid.json()["status"] == "paid"
    assert paid.json()["amount"] == "300.30"
    assert paid.json()["currency"] == "CNY"

    final = client.get(f"/claims/{claim_id}", headers=claimant)
    assert final.json()["status"] == "paid"


def test_withdraw_then_resubmit_over_http(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    claimant = auth(world.claimant)
    claim_id = _submitted_claim(client, claimant, world)
    withdrawn = client.post(f"/claims/{claim_id}/withdraw", headers=claimant)
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    resubmitted = client.post(f"/claims/{claim_id}/submit", headers=claimant)
    assert resubmitted.status_code == 200
    assert resubmitted.json()["status"] == "submitted"


# --- 7. list scoping ----------------------------------------------------------


def test_list_scope_employee_vs_manager(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    claimant = auth(world.claimant)
    outsider = auth(world.outsider)
    manager = auth(world.manager)

    claimant_claim = _create_claim(client, claimant)
    outsider_claim = _create_claim(client, outsider)

    # Employee (read/own) sees only their own.
    own = client.get("/claims", headers=claimant)
    assert own.status_code == 200
    own_ids = {item["id"] for item in own.json()}
    assert claimant_claim in own_ids
    assert outsider_claim not in own_ids

    # Outsider sees only their own too.
    other = client.get("/claims", headers=outsider)
    other_ids = {item["id"] for item in other.json()}
    assert outsider_claim in other_ids
    assert claimant_claim not in other_ids

    # Manager (read/team) sees their subordinate's claim (team = self + claimant).
    team = client.get("/claims", headers=manager)
    assert team.status_code == 200
    team_ids = {item["id"] for item in team.json()}
    assert claimant_claim in team_ids
    assert outsider_claim not in team_ids


def test_list_without_read_permission_returns_empty(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    # sysadmin holds no expense_claim/read permission at all.
    response = client.get("/claims", headers=auth(world.sysadmin))
    assert response.status_code == 200
    assert response.json() == []


def test_list_is_scoped_by_company_for_finance(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    # finance_lead holds read/company → sees every claim in the company.
    claimant = auth(world.claimant)
    outsider = auth(world.outsider)
    finance_lead = auth(world.finance_lead)
    claimant_claim = _create_claim(client, claimant)
    outsider_claim = _create_claim(client, outsider)

    response = client.get("/claims", headers=finance_lead)
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert {claimant_claim, outsider_claim} <= ids
