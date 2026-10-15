"""Authentication / principal-resolution tests for the API layer (W1-11).

Covers acceptance points 1, 2 and 4:

1. missing ``Authorization`` → 401; wrong token → 401; unset
   ``AJAIMGR_INTERNAL_TOKEN`` → 503 (fail closed);
2. unknown or non-active employee → 403;
4. the request-body ``employee_id`` is ignored — the principal is always the
   employee named by ``X-Employee-Id``, never the body.

It also asserts, at the source level, that ``app/api/deps.py`` reads the employee
id from the header only (never from the body / query / cookies).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Employee
from app.tests.api.conftest import TEST_TOKEN, World

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_missing_authorization_returns_401(client: TestClient, world: World) -> None:
    response = client.get("/claims", headers={"X-Employee-Id": str(world.claimant.id)})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_token_returns_401(client: TestClient, world: World) -> None:
    response = client.get(
        "/claims",
        headers={
            "Authorization": "Bearer not-the-right-token",
            "X-Employee-Id": str(world.claimant.id),
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_non_bearer_scheme_returns_401(client: TestClient, world: World) -> None:
    response = client.get(
        "/claims",
        headers={
            "Authorization": f"Basic {TEST_TOKEN}",
            "X-Employee-Id": str(world.claimant.id),
        },
    )
    assert response.status_code == 401


def test_missing_employee_header_returns_401(client: TestClient) -> None:
    response = client.get("/claims", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert response.status_code == 401


def test_non_integer_employee_header_returns_401(
    client: TestClient, world: World
) -> None:
    response = client.get(
        "/claims",
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "X-Employee-Id": "not-an-int",
        },
    )
    assert response.status_code == 401


def test_token_not_configured_returns_503(
    client_without_token: TestClient, world: World
) -> None:
    response = client_without_token.get(
        "/claims",
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "X-Employee-Id": str(world.claimant.id),
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_token_not_configured_fails_closed_without_any_header(
    client_without_token: TestClient,
) -> None:
    # Even with no credentials at all, an unconfigured token means 503 (not 401):
    # the endpoint must not be reachable.
    response = client_without_token.get("/claims")
    assert response.status_code == 503


def test_unknown_employee_returns_403(client: TestClient, world: World) -> None:
    response = client.get(
        "/claims",
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "X-Employee-Id": "99999999",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_inactive_employee_returns_403(
    client: TestClient, engine: Engine, world: World
) -> None:
    with Session(engine) as session:
        inactive = Employee(
            company_id=world.company.id,
            employee_no="E-inactive",
            status="inactive",
        )
        session.add(inactive)
        session.commit()
        inactive_id = inactive.id

    try:
        response = client.get(
            "/claims",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "X-Employee-Id": str(inactive_id),
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
    finally:
        with Session(engine) as session:
            session.execute(delete(Employee).where(Employee.id == inactive_id))
            session.commit()


def test_body_employee_id_is_ignored(
    client: TestClient, world: World, auth
) -> None:
    """A body ``employee_id`` pointing at another employee must be ignored."""
    other = world.outsider
    response = client.post(
        "/claims",
        json={"currency": "CNY", "employee_id": other.id},
        headers=auth(world.claimant),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["employee_id"] == world.claimant.id
    assert body["employee_id"] != other.id


def test_body_employee_id_cannot_hijack_an_existing_employee(
    client: TestClient, world: World, auth
) -> None:
    """Posting with ``employee_id`` of the manager still creates for the caller."""
    response = client.post(
        "/claims",
        json={"employee_id": world.manager.id},
        headers=auth(world.outsider),
    )
    assert response.status_code == 201
    assert response.json()["employee_id"] == world.outsider.id


def test_deps_reads_employee_id_from_header_only() -> None:
    """Source-level guard: deps.py never reads employee_id from body/query/cookie."""
    source = (REPO_ROOT / "app" / "api" / "deps.py").read_text(encoding="utf-8")
    assert "x_employee_id" in source  # header parameter name
    assert "Header(" in source
    for forbidden in ("Body(", "Query(", "Form(", "Cookie(", ".json(", ".body("):
        assert forbidden not in source, f"deps.py must not read identity via {forbidden!r}"
