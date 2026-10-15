"""W1-14 §8 channel identity: a channel identity is never trusted as a principal.

**Finding recorded here and in the W1-14 report.**  The product does not (yet)
implement a channel-identity -> principal resolver.  ``employee_channel_bindings``
exists and the admin console can bind/unbind, but no code path turns a
``(channel_type, external_user_id)`` into a ``Principal``: ``app.api.deps``
resolves the caller solely from ``X-Employee-Id`` and requires ``status='active'``
on the employee.  PLAN §12's "未绑定或已解绑的渠道身份默认拒绝" therefore holds
only *trivially* -- because nothing is ever accepted from a channel identity.

These tests still verify the properties that genuinely exist and matter:

* an inactive/left employee is never accepted as a principal;
* a revoked binding does not make a non-active employee a valid subject;
* the binding model enforces its status domain and the composite identity key;
* presenting a raw channel identity (unbound, active or revoked) as a credential
  is refused -- the system stays fail-closed;
* unbinding marks a binding ``revoked`` (never deletes it) and is audited without
  writing the raw channel id.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.db.models.organization import Employee
from app.identity.models import EmployeeChannelBinding
from app.tests.qa.conftest import (
    TEST_TOKEN,
    World,
    admin_login,
    cookie_header,
)

BEARER = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _make_employee(
    engine: Engine, world: World, *, employee_no: str, status: str
) -> int:
    with Session(engine) as session:
        employee = Employee(
            company_id=world.company.id, employee_no=employee_no, status=status
        )
        session.add(employee)
        session.commit()
        return employee.id


def _add_binding(
    engine: Engine,
    *,
    employee_id: int,
    external_user_id: str,
    status: str,
    channel_type: str = "weixin",
) -> None:
    with Session(engine) as session:
        session.add(
            EmployeeChannelBinding(
                employee_id=employee_id,
                channel_type=channel_type,
                external_user_id=external_user_id,
                status=status,
            )
        )
        session.commit()


# --- non-active employees -----------------------------------------------------


def test_inactive_and_left_employees_are_rejected_as_principals(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An employee whose status is not ``active`` can never be a principal."""
    for suffix, status in (("inactive", "inactive"), ("left", "left")):
        employee_id = _make_employee(
            qa_engine, world, employee_no=f"QA-{suffix}", status=status
        )
        response = client.get(
            "/claims",
            headers={"Authorization": f"Bearer {TEST_TOKEN}", "X-Employee-Id": str(employee_id)},
        )
        assert response.status_code == 403, (status, response.status_code)
        assert response.json()["error"]["code"] == "forbidden"


def test_revoked_binding_does_not_reactivate_a_left_employee(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A (revoked) binding cannot override a non-active employee status."""
    employee_id = _make_employee(
        qa_engine, world, employee_no="QA-left-bound", status="left"
    )
    _add_binding(
        qa_engine,
        employee_id=employee_id,
        external_user_id="wx-left-1",
        status="revoked",
    )
    response = client.get(
        "/claims",
        headers={"Authorization": f"Bearer {TEST_TOKEN}", "X-Employee-Id": str(employee_id)},
    )
    assert response.status_code == 403


# --- channel identity is never a credential ----------------------------------


def test_unbound_channel_identity_is_not_a_credential(client: TestClient) -> None:
    """Supplying a channel identity in query/body does not authenticate a request."""
    query = client.get(
        "/claims",
        params={"channel_type": "weixin", "external_user_id": "wx-never-bound"},
        headers=BEARER,
    )
    assert query.status_code == 401
    body = client.post(
        "/claims",
        json={"channel_type": "weixin", "external_user_id": "wx-never-bound"},
        headers=BEARER,
    )
    assert body.status_code == 401


def test_bound_and_revoked_channel_identities_are_not_credentials(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Neither an active nor a revoked binding turns a channel id into a principal."""
    _add_binding(
        qa_engine,
        employee_id=world.claimant.id,
        external_user_id="wx-active-1",
        status="active",
    )
    _add_binding(
        qa_engine,
        employee_id=world.manager.id,
        external_user_id="wx-revoked-1",
        status="revoked",
    )
    for external_id in ("wx-active-1", "wx-revoked-1"):
        response = client.get(
            "/claims",
            params={"channel_type": "weixin", "external_user_id": external_id},
            headers=BEARER,
        )
        assert response.status_code == 401, external_id


def test_no_channel_login_endpoint_exists(client: TestClient) -> None:
    """There is no endpoint that exchanges a channel identity for a session."""
    for path in ("/auth/channel", "/channel/login", "/login/channel", "/auth/bind"):
        response = client.post(
            path,
            json={"channel_type": "weixin", "external_user_id": "wx-x"},
            headers=BEARER,
        )
        assert response.status_code == 404, path


# --- binding model invariants -------------------------------------------------


def test_binding_status_check_constraint(
    world: World, qa_engine: Engine
) -> None:
    """An unknown binding status is rejected by the CHECK constraint."""
    with Session(qa_engine) as session:
        session.add(
            EmployeeChannelBinding(
                employee_id=world.claimant.id,
                channel_type="weixin",
                external_user_id="wx-bad-status",
                status="bogus",
            )
        )
        try:
            session.commit()
            raised = False
        except IntegrityError:
            raised = True
            session.rollback()
    assert raised, "invalid binding status must be rejected"


def test_binding_composite_identity_key(world: World, qa_engine: Engine) -> None:
    """The identity key is (channel_type, external_user_id), not external id alone."""
    _add_binding(
        qa_engine,
        employee_id=world.claimant.id,
        external_user_id="wx-dup-key",
        status="active",
    )
    # Same channel + same external id -> rejected.
    with Session(qa_engine) as session:
        session.add(
            EmployeeChannelBinding(
                employee_id=world.claimant.id,
                channel_type="weixin",
                external_user_id="wx-dup-key",
            )
        )
        try:
            session.commit()
            raised = False
        except IntegrityError:
            raised = True
            session.rollback()
    assert raised, "duplicate (channel_type, external_user_id) must be rejected"

    # Same external id on a different channel is a distinct identity.
    _add_binding(
        qa_engine,
        employee_id=world.claimant.id,
        external_user_id="wx-dup-key",
        status="active",
        channel_type="dingtalk",
    )
    with Session(qa_engine) as session:
        count = len(
            session.scalars(
                select(EmployeeChannelBinding).where(
                    EmployeeChannelBinding.external_user_id == "wx-dup-key"
                )
            ).all()
        )
    assert count == 2


# --- unbind marks revoked, never deletes --------------------------------------


def test_unbind_marks_revoked_and_is_audited_without_raw_channel_id(
    admin_client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Unbinding sets status='revoked' (row kept) and audits a salted hash only."""
    _add_binding(
        qa_engine,
        employee_id=world.claimant.id,
        external_user_id="wx-unbind-1",
        status="active",
    )
    with Session(qa_engine) as session:
        binding_id = session.scalar(
            select(EmployeeChannelBinding.id).where(
                EmployeeChannelBinding.external_user_id == "wx-unbind-1"
            )
        )
    assert binding_id is not None

    cookie = admin_login(admin_client, world.sysadmin.employee_no)
    response = admin_client.post(
        "/admin/bindings",
        data={"action": "unbind", "binding_id": str(binding_id)},
        headers=cookie_header(cookie),
    )
    assert response.status_code == 303

    with Session(qa_engine) as session:
        binding = session.get(EmployeeChannelBinding, binding_id)
        assert binding is not None, "unbind must not delete the binding row"
        assert binding.status == "revoked"
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "channel_binding.unbind",
                AuditEvent.resource_id == str(world.claimant.id),
            )
        ).all()
    assert events, "unbind must be audited"
    assert "external_user_id_hash" in events[-1].details
    assert "wx-unbind-1" not in str(events[-1].details)


def test_revoked_binding_is_distinguishable_from_active(
    world: World, qa_engine: Engine
) -> None:
    """A revoked binding is stored and can be filtered out by status='active'."""
    _add_binding(
        qa_engine,
        employee_id=world.claimant.id,
        external_user_id="wx-revoked-2",
        status="revoked",
    )
    with Session(qa_engine) as session:
        active = session.scalars(
            select(EmployeeChannelBinding).where(
                EmployeeChannelBinding.employee_id == world.claimant.id,
                EmployeeChannelBinding.status == "active",
            )
        ).all()
        all_rows = session.scalars(
            select(EmployeeChannelBinding).where(
                EmployeeChannelBinding.employee_id == world.claimant.id
            )
        ).all()
    assert active == []
    assert len(all_rows) == 1
    assert all_rows[0].status == "revoked"
