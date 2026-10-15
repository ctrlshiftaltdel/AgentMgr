"""W1-14 §9 audit integrity: denials are recorded and the ledger is append-only.

* a denied read and a denied write each append exactly one ``expense_claim.deny``
  event carrying the acting employee, the claim and a structured reason;
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

from app.audit import service as audit_service
from app.audit.models import AuditEvent
from app.tests.qa.conftest import (
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


# --- admin-console denial (recorded deviation) --------------------------------


def test_admin_page_denial_is_not_audited_current_behaviour(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Pin the observed behaviour of DEFECT #4.

    PLAN §12 requires permission denials to be auditable.  The API layer records
    ``expense_claim.deny`` on a denied read, but the admin console's
    ``_require_or_forbidden`` raises ``AdminForbidden`` **without** recording an
    event.  The only audit row written is the ``admin.login`` from signing in;
    the denied page itself adds nothing.  This test documents the current
    (defective) outcome.
    """
    cookie = admin_login(admin_client, world.sysadmin.employee_no)
    after_login = _events(qa_engine)
    assert [event.action for event in after_login] == ["admin.login"]

    response = admin_client.get("/admin/claims", headers=cookie_header(cookie))
    assert response.status_code == 403

    after_denial = _events(qa_engine)
    # observed defect: the denied page appended nothing.
    assert [event.action for event in after_denial] == ["admin.login"]


@pytest.mark.xfail(
    strict=True,
    reason="W1-14 DEFECT #4: a denied admin-console page (403) writes no audit "
    "event, unlike a denied API call (PLAN §12 requires denials be auditable)",
)
def test_admin_page_denial_is_audited(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A denied admin page must append an audit event."""
    cookie = admin_login(admin_client, world.sysadmin.employee_no)
    before = [event.action for event in _events(qa_engine)]
    response = admin_client.get("/admin/claims", headers=cookie_header(cookie))
    assert response.status_code == 403
    assert [event.action for event in _events(qa_engine)] != before
