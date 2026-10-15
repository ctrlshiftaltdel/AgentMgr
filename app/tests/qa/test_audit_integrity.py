"""W1-14 §9 audit integrity: denials are recorded and the ledger is append-only.

* a denied read and a denied write each append exactly one ``expense_claim.deny``
  event carrying the acting employee, the claim and a structured reason;
* a denied **admin-console** page or action (403) appends exactly one
  ``admin.deny`` event naming the actor, the attempted action, the resource and
  the request id -- and the refusal is still a refusal even if the audit write
  itself fails (DEFECT #4, fixed by W1-18);
* a failed admin login appends an ``admin.login_failed`` event and never the
  credential; an invalid/tampered session writes no event at all;
* a successful transition appends its event with the actor and the propagated
  ``X-Request-Id``;
* the database **itself** refuses ``UPDATE`` and ``DELETE`` on ``audit_events``
  (``restrict_violation`` / SQLSTATE ``23001``), not merely the application;
* ``app.audit.service`` exposes no mutation entry point;
* ``TRUNCATE`` is the only way to empty the table (used by the test cleanup).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update, delete
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.admin_auth import SESSION_COOKIE_NAME, create_session_token
from app.audit import service as audit_service
from app.audit.models import AuditEvent
from app.tests.qa.conftest import (
    TEST_TOKEN,
    World,
    add_line,
    admin_login,
    auth_headers,
    claim_with_line,
    cookie_header,
    submit_claim,
)

#: PostgreSQL SQLSTATE for ``restrict_violation`` -- what the trigger raises.
RESTRICT_VIOLATION = "23001"

_FORBIDDEN_MEMBER = re.compile(
    r"(?i)(update|delete|remove|drop|modify|mutate|edit|purge|truncate|wipe)"
)


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _events(engine: Engine, **filters: object) -> list[AuditEvent]:
    with Session(engine) as session:
        stmt = select(AuditEvent)
        for column, value in filters.items():
            stmt = stmt.where(getattr(AuditEvent, column) == value)
        return list(session.scalars(stmt.order_by(AuditEvent.id)))


# --- denials are audited ------------------------------------------------------


def test_denied_read_writes_a_deny_audit_event(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A denied read appends one deny event naming the actor, claim and reason."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)

    outsider = auth_headers(client, world.outsider)  # read/own, claim not in scope
    response = client.get(f"/claims/{claim_id}", headers=outsider)
    assert response.status_code == 403

    events = _events(
        qa_engine,
        resource_type="expense_claim",
        resource_id=str(claim_id),
        action="expense_claim.deny",
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == world.outsider.id
    assert event.details == {"attempted_action": "read", "reason": "not_in_scope"}


def test_denied_read_without_any_read_permission_is_audited(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A principal with no read scope at all also produces a deny event."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)

    response = client.get(
        f"/claims/{claim_id}", headers=auth_headers(client, world.sysadmin)
    )
    assert response.status_code == 403
    events = _events(
        qa_engine,
        resource_id=str(claim_id),
        action="expense_claim.deny",
    )
    assert len(events) == 1
    assert events[0].actor_id == world.sysadmin.id
    assert events[0].details["reason"] == "missing_permission"


def test_denied_write_writes_a_deny_audit_event(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A denied add-line appends a deny event with ``not_claim_owner``."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)

    response = add_line(
        client, auth_headers(client, world.outsider), world, claim_id, amount="9.99"
    )
    assert response.status_code == 403

    events = _events(
        qa_engine,
        resource_id=str(claim_id),
        action="expense_claim.deny",
    )
    assert len(events) == 1
    assert events[0].actor_id == world.outsider.id
    assert events[0].details == {
        "attempted_action": "add_line",
        "reason": "not_claim_owner",
    }


def test_deny_audit_details_contain_only_structured_non_sensitive_fields(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Deny events never carry sensitive raw values (only attempt + reason)."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world, amount="777.77")
    client.get(f"/claims/{claim_id}", headers=auth_headers(client, world.outsider))

    event = _events(qa_engine, resource_id=str(claim_id), action="expense_claim.deny")[-1]
    assert set(event.details) <= {"attempted_action", "reason"}
    assert "777.77" not in str(event.details)


# --- successful transitions are audited ---------------------------------------


def test_successful_transition_writes_event_with_actor_and_request_id(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A successful submit appends one event carrying the actor and X-Request-Id."""
    owner = {
        **auth_headers(client, world.claimant),
        "X-Request-Id": "w1-14-req-001",
    }
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200

    events = _events(qa_engine, resource_id=str(claim_id))
    actions = [event.action for event in events]
    assert actions == ["expense_claim.create", "expense_claim.add_line", "expense_claim.submit"]
    submit_event = events[-1]
    assert submit_event.actor_id == world.claimant.id
    assert submit_event.request_id == "w1-14-req-001"
    assert submit_event.details["from_status"] == "draft"
    assert submit_event.details["to_status"] == "submitted"


# --- database-level append-only ----------------------------------------------


def _seed_audit_event(engine: Engine) -> int:
    with Session(engine) as session:
        event = audit_service.record_event(
            session,
            actor_id=None,
            action="w1-14.probe",
            resource_type="probe",
            resource_id="P-1",
            request_id="w1-14-probe",
        )
        session.commit()
        return event.id


def test_update_of_audit_row_is_rejected_by_database(qa_engine: Engine) -> None:
    """Raw UPDATE on ``audit_events`` raises ``restrict_violation`` and changes nothing."""
    event_id = _seed_audit_event(qa_engine)
    with Session(qa_engine) as session:
        with pytest.raises(DBAPIError) as excinfo:
            with session.begin_nested():
                session.execute(
                    update(AuditEvent)
                    .where(AuditEvent.id == event_id)
                    .values(action="tampered")
                )
        assert _sqlstate(excinfo.value) == RESTRICT_VIOLATION
        session.rollback()

    with Session(qa_engine) as session:
        still = session.get(AuditEvent, event_id)
        assert still is not None
        assert still.action == "w1-14.probe"


def test_delete_of_audit_row_is_rejected_by_database(qa_engine: Engine) -> None:
    """Raw DELETE on ``audit_events`` raises ``restrict_violation`` and removes nothing."""
    event_id = _seed_audit_event(qa_engine)
    with Session(qa_engine) as session:
        with pytest.raises(DBAPIError) as excinfo:
            with session.begin_nested():
                session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))
        assert _sqlstate(excinfo.value) == RESTRICT_VIOLATION
        session.rollback()

    with Session(qa_engine) as session:
        assert session.get(AuditEvent, event_id) is not None


def test_truncate_is_the_only_cleanup_path(qa_engine: Engine) -> None:
    """``TRUNCATE`` empties the append-only table (row-level DELETE cannot)."""
    _seed_audit_event(qa_engine)
    with Session(qa_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 1
        session.execute(text("TRUNCATE TABLE audit_events RESTART IDENTITY"))
        session.commit()
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_audit_service_exposes_only_append() -> None:
    """``app.audit.service`` has no mutation entry point."""
    public = {name for name in dir(audit_service) if not name.startswith("_")}
    assert {name for name in public if _FORBIDDEN_MEMBER.search(name)} == set()
    assert audit_service.__all__ == ["record_event"]


# --- admin-console denial (DEFECT #4, fixed by W1-18) -------------------------


def test_admin_page_denial_is_audited(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A denied admin page appends exactly one ``admin.deny`` event.

    ``sysadmin`` holds no ``expense_claim/read`` scope, so ``/admin/claims`` is a
    403 page.  PLAN §12 requires the denial to be auditable, so the refusal must
    commit an audit row in the same transaction instead of vanishing with the
    exception.
    """
    cookie = admin_login(admin_client, world.sysadmin.employee_no)
    before = [event.action for event in _events(qa_engine)]
    assert before == ["admin.login"]

    response = admin_client.get(
        "/admin/claims",
        headers={**cookie_header(cookie), "X-Request-Id": "w1-18-admin-deny-001"},
    )
    assert response.status_code == 403

    after = _events(qa_engine)
    assert [event.action for event in after] == ["admin.login", "admin.deny"]

    denial = after[-1]
    assert denial.actor_id == world.sysadmin.id
    assert denial.action == "admin.deny"
    assert denial.resource_type == "expense_claim"
    assert denial.resource_id is None
    assert denial.request_id == "w1-18-admin-deny-001"
    assert denial.details == {
        "attempted_action": "list",
        "reason": "missing_permission",
    }


def test_admin_audit_page_denial_is_audited_for_ordinary_employee(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An ordinary employee's 403 on ``/admin/audit`` carries subject/action/resource.

    This is the exact scenario DEFECT #4 recorded as unaudited: the ``employee``
    role holds no ``audit/read`` scope, so the page must refuse **and** leave a
    ``admin.deny`` row naming the actor, the attempted action and the resource.
    """
    cookie = admin_login(admin_client, world.outsider.employee_no)

    response = admin_client.get(
        "/admin/audit",
        headers={**cookie_header(cookie), "X-Request-Id": "w1-18-admin-deny-002"},
    )
    assert response.status_code == 403
    assert "无权限" in response.text  # 403 page content is unchanged

    events = _events(
        qa_engine,
        action="admin.deny",
        resource_type="audit",
        request_id="w1-18-admin-deny-002",
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == world.outsider.id
    assert event.resource_id is None
    assert event.details == {
        "attempted_action": "read",
        "reason": "missing_permission",
    }
    # No audit event may be mistaken for a business operation.
    assert {row.action for row in _events(qa_engine)} == {"admin.login", "admin.deny"}


def test_admin_resource_denial_records_the_resource_id(
    client: TestClient, admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A denial on a concrete resource records ``resource_id`` and the reason.

    ``sysadmin`` cannot read any claim, so the claim-detail page refuses with
    ``missing_permission`` and must name the claim it refused.
    """
    claim_id = claim_with_line(client, auth_headers(client, world.claimant), world)

    cookie = admin_login(admin_client, world.sysadmin.employee_no)
    response = admin_client.get(
        f"/admin/claims/{claim_id}",
        headers={**cookie_header(cookie), "X-Request-Id": "w1-18-admin-deny-003"},
    )
    assert response.status_code == 403

    events = _events(
        qa_engine,
        action="admin.deny",
        resource_type="expense_claim",
        resource_id=str(claim_id),
        request_id="w1-18-admin-deny-003",
    )
    assert len(events) == 1
    assert events[0].actor_id == world.sysadmin.id
    assert events[0].details == {
        "attempted_action": "read",
        "reason": "missing_permission",
    }


def test_admin_denial_details_contain_only_structured_non_sensitive_fields(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An admin deny event carries only ``attempted_action`` / ``reason``."""
    cookie = admin_login(admin_client, world.outsider.employee_no)
    assert (
        admin_client.get("/admin/audit", headers=cookie_header(cookie)).status_code
        == 403
    )

    denial = _events(qa_engine, action="admin.deny")[-1]
    assert set(denial.details) <= {"attempted_action", "reason"}
    # The session cookie / internal token must never reach the audit ledger.
    assert cookie not in str(denial.details)
    assert TEST_TOKEN not in str(denial.details)


def test_admin_denial_audit_write_failure_still_forbids(
    admin_client: TestClient,
    world: World,
    qa_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing audit backend must not turn a 403 into a 500 (or an allow).

    ``record_event`` is patched to raise *after* login succeeded, so only the
    denial path is affected; the page must still refuse.
    """
    cookie = admin_login(admin_client, world.outsider.employee_no)
    before = [event.action for event in _events(qa_engine)]

    from app.api import admin as admin_module

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit backend unavailable")

    monkeypatch.setattr(admin_module, "record_event", _explode)

    response = admin_client.get("/admin/audit", headers=cookie_header(cookie))
    assert response.status_code == 403
    assert response.status_code != 500
    assert "无权限" in response.text

    # Fail closed: the refusal happened, the audit row simply could not be written.
    assert [event.action for event in _events(qa_engine)] == before


# --- login failure / invalid session are not business operations ---------------


def test_login_failure_is_audited_without_leaking_credentials(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Bad credentials write ``admin.login_failed`` and never the secret itself."""
    bogus = "w1-18-not-the-token-4f21"
    response = admin_client.post(
        "/admin/login",
        data={"token": bogus, "employee_no": world.claimant.employee_no},
        headers={"X-Request-Id": "w1-18-login-failed-001"},
    )
    assert response.status_code == 401

    events = _events(qa_engine, request_id="w1-18-login-failed-001")
    assert [event.action for event in events] == ["admin.login_failed"]
    event = events[0]
    # Credentials were never verified: there is no attributable subject.
    assert event.actor_id is None
    assert event.resource_type == "admin_session"
    assert event.details == {"reason": "invalid_credentials"}
    # Neither the audit row nor the response may contain the credential.
    assert bogus not in str(event.details)
    assert bogus not in response.text
    assert TEST_TOKEN not in response.text
    # And no business operation was recorded for this attempt.
    assert not _events(qa_engine, action="admin.login")


def test_invalid_session_writes_no_audit_event_and_leaks_nothing(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A tampered session is redirected to the login page and writes no audit row.

    ``AdminAuthRequired`` (unauthenticated / tampered / expired) is deliberately
    **not** audited -- see ``app.api.admin.register_admin_exception_handlers`` --
    because there is no verified subject and it would give unauthenticated traffic
    an unbounded write path into the append-only ledger.
    """
    good = create_session_token(world.claimant.id)
    last = good[-1]
    tampered = good[:-1] + ("A" if last != "A" else "B")
    assert tampered != good

    response = admin_client.get(
        "/admin/audit",
        headers={
            "Cookie": f"{SESSION_COOKIE_NAME}={tampered}",
            "X-Request-Id": "w1-18-invalid-session-001",
        },
    )
    assert response.status_code in (302, 303, 307, 308)
    assert "/admin/login" in response.headers["location"]
    assert _events(qa_engine, request_id="w1-18-invalid-session-001") == []
    assert tampered not in response.text
