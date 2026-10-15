"""Admin console tests: signed-session auth, page authorization, auditing (W1-12).

Covers the acceptance points:

1. unset ``AJAIMGR_INTERNAL_TOKEN`` → login 503 (fail closed);
2. wrong token → 401; right token + unknown/inactive employee_no → 401;
3. unauthenticated page access → redirect to the login page;
4. tampered / expired session cookie → rejected and redirected;
5. an ordinary employee's approval POST → 403 page and unchanged claim state;
6. data-scope filtering: an employee sees only their own claims, a finance lead
   sees the whole company;
7. role grant/revoke and channel-binding changes are written to ``audit_events``;
8. a user without the audit permission gets a 403 page on ``/admin/audit``;
9. page responses never contain stack traces, the DSN or driver names.

Every request uses ``follow_redirects=False`` so 3xx responses are observable;
each test gets a fresh, cookie-less client from the ``admin_client`` fixture.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.admin_auth import SESSION_COOKIE_NAME, create_session_token
from app.audit.models import AuditEvent
from app.db.models.organization import Employee
from app.expense.models import ExpenseClaim
from app.identity.models import EmployeeRole, Role
from app.tests.api.conftest import TOKEN_ENV, World

#: Strings that must never appear in any rendered admin page.
_FORBIDDEN_SUBSTRINGS = ("Traceback", "psycopg", "postgresql://")


# --- fixtures / helpers -------------------------------------------------------


@pytest.fixture()
def admin_client(api_token: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient with the internal token configured and no redirect following."""
    monkeypatch.setenv(TOKEN_ENV, api_token)
    from app.api.app import create_app

    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _login(
    client: TestClient, employee_no: str, *, token: str = "w1-11-test-internal-token"
) -> str:
    """Log in and return the issued session cookie value (asserts 303 + cookie)."""
    response = client.post(
        "/admin/login", data={"token": token, "employee_no": employee_no}
    )
    assert response.status_code == 303, response.text
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert cookie, "login did not set a session cookie"
    return cookie


def _cookie(value: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={value}"}


def _employee_with_role(
    engine: Engine,
    world: World,
    employee_no: str,
    role_name: str,
    *,
    status: str = "active",
) -> SimpleNamespace:
    """Create an employee and (optionally) grant a role; return a detached view."""
    with Session(engine) as session:
        employee = Employee(
            company_id=world.company.id, employee_no=employee_no, status=status
        )
        session.add(employee)
        session.flush()
        if role_name:
            role = session.scalar(select(Role).where(Role.name == role_name))
            assert role is not None, f"role {role_name!r} not seeded"
            session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))
        session.commit()
        return SimpleNamespace(id=employee.id, employee_no=employee_no)


def _submitted_claim(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    world: World,
    employee,
    *,
    amount: str = "100.00",
) -> int:
    """Create, populate and submit a claim for ``employee`` via the bearer API."""
    headers = auth(employee)
    response = client.post("/claims", json={"currency": "CNY"}, headers=headers)
    assert response.status_code == 201, response.text
    claim_id = int(response.json()["id"])
    response = client.post(
        f"/claims/{claim_id}/lines",
        json={
            "expense_date": "2026-10-01",
            "category_id": world.category.id,
            "description": "出差打车",
            "amount": amount,
            "tax_amount": "0.00",
            "cost_center_id": world.cost_center.id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    response = client.post(f"/claims/{claim_id}/submit", headers=headers)
    assert response.status_code == 200, response.text
    return claim_id


# --- 1. token not configured → 503 -------------------------------------------


def test_login_returns_503_when_token_not_configured(
    client_without_token: TestClient, world: World
) -> None:
    response = client_without_token.post(
        "/admin/login",
        data={"token": "anything", "employee_no": world.claimant.employee_no},
    )
    assert response.status_code == 503
    assert "fail closed" in response.text


# --- 2. bad credentials → 401 ------------------------------------------------


def test_login_wrong_token_returns_401(admin_client: TestClient, world: World) -> None:
    response = admin_client.post(
        "/admin/login",
        data={"token": "not-the-token", "employee_no": world.claimant.employee_no},
    )
    assert response.status_code == 401
    assert "凭据无效" in response.text


def test_login_unknown_employee_returns_401(
    admin_client: TestClient, world: World
) -> None:
    response = admin_client.post(
        "/admin/login", data={"token": "w1-11-test-internal-token", "employee_no": "NOPE"}
    )
    assert response.status_code == 401


def test_login_inactive_employee_returns_401(
    admin_client: TestClient, engine: Engine, world: World
) -> None:
    inactive = _employee_with_role(
        engine, world, "E-inactive-admin", "employee", status="inactive"
    )
    response = admin_client.post(
        "/admin/login",
        data={"token": "w1-11-test-internal-token", "employee_no": inactive.employee_no},
    )
    assert response.status_code == 401


# --- 3. unauthenticated → redirect -------------------------------------------


def test_unauthenticated_page_redirects_to_login(admin_client: TestClient) -> None:
    response = admin_client.get("/admin/claims")
    assert response.status_code in (302, 303, 307, 308)
    assert "/admin/login" in response.headers["location"]


# --- 4. tampered / expired cookie → redirect ---------------------------------


def test_valid_cookie_grants_access(admin_client: TestClient, world: World) -> None:
    cookie = create_session_token(world.claimant.id)
    response = admin_client.get("/admin/claims", headers=_cookie(cookie))
    assert response.status_code == 200
    assert "报销单" in response.text


def test_tampered_cookie_is_rejected(admin_client: TestClient, world: World) -> None:
    good = create_session_token(world.claimant.id)
    last = good[-1]
    tampered = good[:-1] + ("A" if last != "A" else "B")
    assert tampered != good
    response = admin_client.get("/admin/claims", headers=_cookie(tampered))
    assert response.status_code in (302, 303, 307, 308)
    assert "/admin/login" in response.headers["location"]


def test_expired_cookie_is_rejected(admin_client: TestClient, world: World) -> None:
    expired = create_session_token(world.claimant.id, ttl=-60)
    response = admin_client.get("/admin/claims", headers=_cookie(expired))
    assert response.status_code in (302, 303, 307, 308)
    assert "/admin/login" in response.headers["location"]


# --- 5. ordinary employee cannot approve → 403, state unchanged --------------


def test_employee_approve_returns_403_and_state_unchanged(
    admin_client: TestClient,
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    world: World,
) -> None:
    claim_id = _submitted_claim(client, auth, world, world.outsider)

    _login(admin_client, world.claimant.employee_no)
    response = admin_client.post(f"/admin/claims/{claim_id}/approve")

    assert response.status_code == 403
    assert "无权限" in response.text

    # Independent session: business state must be unchanged.
    with Session(engine) as session:
        claim = session.get(ExpenseClaim, claim_id)
        assert claim is not None
        assert claim.status == "submitted"


# --- 6. data-scope filtering --------------------------------------------------


def test_scope_filtering_own_vs_company(
    admin_client: TestClient,
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    world: World,
) -> None:
    own_id = _submitted_claim(client, auth, world, world.claimant)
    other_id = _submitted_claim(client, auth, world, world.outsider)

    with Session(engine) as session:
        own_no = session.get(ExpenseClaim, own_id).claim_no
        other_no = session.get(ExpenseClaim, other_id).claim_no

    # Ordinary employee: read/own → only their own claim.
    _login(admin_client, world.claimant.employee_no)
    response = admin_client.get("/admin/claims")
    assert response.status_code == 200
    assert own_no in response.text
    assert other_no not in response.text

    # Finance lead: read/company → sees the whole company.
    admin_client.cookies.clear()
    _login(admin_client, world.finance_lead.employee_no)
    response = admin_client.get("/admin/claims")
    assert response.status_code == 200
    assert own_no in response.text
    assert other_no in response.text


# --- 7. role & binding changes are audited -----------------------------------


def _audit_actions(engine: Engine, action: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(select(AuditEvent).where(AuditEvent.action == action))
        )


def test_role_grant_and_revoke_are_audited(
    admin_client: TestClient, engine: Engine, world: World
) -> None:
    _login(admin_client, world.sysadmin.employee_no)

    grant = admin_client.post(
        f"/admin/employees/{world.claimant.id}/roles",
        data={"role": "manager", "action": "grant"},
    )
    assert grant.status_code == 303

    events = [
        e for e in _audit_actions(engine, "identity.role_grant")
        if e.resource_id == str(world.claimant.id)
    ]
    assert events, "grant must be audited"
    assert events[-1].actor_id == world.sysadmin.id
    assert events[-1].details["role"] == "manager"

    revoke = admin_client.post(
        f"/admin/employees/{world.claimant.id}/roles",
        data={"role": "manager", "action": "revoke"},
    )
    assert revoke.status_code == 303
    revoke_events = [
        e for e in _audit_actions(engine, "identity.role_revoke")
        if e.resource_id == str(world.claimant.id)
    ]
    assert revoke_events, "revoke must be audited"
    assert revoke_events[-1].actor_id == world.sysadmin.id


def test_channel_binding_change_is_audited(
    admin_client: TestClient, engine: Engine, world: World
) -> None:
    _login(admin_client, world.sysadmin.employee_no)

    bind = admin_client.post(
        "/admin/bindings",
        data={
            "action": "bind",
            "employee_id": str(world.claimant.id),
            "channel_type": "weixin",
            "external_user_id": "wx-openid-0001",
        },
    )
    assert bind.status_code == 303

    bind_events = _audit_actions(engine, "channel_binding.bind")
    assert bind_events, "bind must be audited"
    assert bind_events[-1].actor_id == world.sysadmin.id
    # The raw channel user id must never be written to audit details.
    assert "external_user_id_hash" in bind_events[-1].details
    assert "wx-openid-0001" not in str(bind_events[-1].details)

    # Resolve the binding id, then unbind it.
    from app.identity.models import EmployeeChannelBinding

    with Session(engine) as session:
        binding_id = session.scalar(
            select(EmployeeChannelBinding.id).where(
                EmployeeChannelBinding.external_user_id == "wx-openid-0001"
            )
        )
    assert binding_id is not None

    unbind = admin_client.post(
        "/admin/bindings", data={"action": "unbind", "binding_id": str(binding_id)}
    )
    assert unbind.status_code == 303
    assert _audit_actions(engine, "channel_binding.unbind"), "unbind must be audited"


# --- 8. audit page requires permission ---------------------------------------


def test_audit_page_forbidden_without_permission(
    admin_client: TestClient, world: World
) -> None:
    _login(admin_client, world.claimant.employee_no)
    response = admin_client.get("/admin/audit")
    assert response.status_code == 403
    assert "无权限" in response.text


def test_audit_page_visible_for_auditor(
    admin_client: TestClient, engine: Engine, world: World
) -> None:
    auditor = _employee_with_role(engine, world, "E-auditor", "auditor")
    _login(admin_client, auditor.employee_no)
    response = admin_client.get("/admin/audit")
    assert response.status_code == 200
    assert "审计日志" in response.text


# --- 9. no internal leakage ---------------------------------------------------


def test_pages_do_not_leak_internals(
    admin_client: TestClient,
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    world: World,
) -> None:
    claim_id = _submitted_claim(client, auth, world, world.claimant)
    _login(admin_client, world.manager.employee_no)

    bodies = [
        admin_client.get("/admin/login").text,
        admin_client.get("/admin").text,
        admin_client.get("/admin/claims").text,
        admin_client.get(f"/admin/claims/{claim_id}").text,
        admin_client.get("/admin/audit").text,  # 403 page for the manager
        admin_client.post(f"/admin/claims/{claim_id}/approve").text,
    ]
    for body in bodies:
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in body, f"leaked {forbidden!r}"


# --- extra: happy-path rendering with Chinese titles --------------------------


def test_dashboard_and_detail_render(
    admin_client: TestClient,
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    world: World,
) -> None:
    claim_id = _submitted_claim(client, auth, world, world.claimant)
    _login(admin_client, world.manager.employee_no)

    dashboard = admin_client.get("/admin")
    assert dashboard.status_code == 200
    assert "仪表盘" in dashboard.text
    assert "待办审批" in dashboard.text

    detail = admin_client.get(f"/admin/claims/{claim_id}")
    assert detail.status_code == 200
    assert "明细" in detail.text
    assert "审计轨迹" in detail.text
    assert "出差打车" in detail.text
