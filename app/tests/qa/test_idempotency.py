"""W1-14 §3 idempotency: a repeated call must never create a second record.

Covers the three cases named by the task:

1. re-submitting the same claim;
2. re-processing the same Outbox event (and re-enqueueing / re-starting a
   workflow with the same key);
3. re-sending the same payment registration.

Each test asserts the *absence* of a duplicate record (audit event, notification,
payment row) against the real database.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.expense.models import ExpenseClaim, ReimbursementPayment
from app.tests.qa.conftest import (
    World,
    auth_headers,
    claim_with_line,
    create_claim,
    submit_claim,
)
from app.workflow.models import (
    ApprovalStep,
    Notification,
    OutboxEvent,
    WorkflowInstance,
)
from app.workflow.service import enqueue_event, process_event, start_approval


def _count(engine: Engine, model: type, **filters: object) -> int:
    with Session(engine) as session:
        stmt = select(func.count()).select_from(model)
        for column, value in filters.items():
            stmt = stmt.where(getattr(model, column) == value)
        return session.scalar(stmt)


def _claim_status(engine: Engine, claim_id: int) -> str:
    with Session(engine) as session:
        return session.get(ExpenseClaim, claim_id).status


# --- 1. duplicate submit ------------------------------------------------------


def test_duplicate_submit_does_not_create_a_second_transition(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A second submit of an already-submitted claim is refused and writes nothing."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)

    first = submit_claim(client, owner, claim_id)
    assert first.status_code == 200
    assert first.json()["status"] == "submitted"

    second = submit_claim(client, owner, claim_id)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_transition"

    assert _claim_status(qa_engine, claim_id) == "submitted"
    assert (
        _count(
            qa_engine,
            AuditEvent,
            resource_id=str(claim_id),
            action="expense_claim.submit",
        )
        == 1
    )


def test_many_repeated_submits_keep_exactly_one_transition(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Repeating the submit call 5 times still yields one transition + one event."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200

    for _ in range(5):
        assert submit_claim(client, owner, claim_id).status_code == 409

    assert _claim_status(qa_engine, claim_id) == "submitted"
    assert (
        _count(
            qa_engine,
            AuditEvent,
            resource_id=str(claim_id),
            action="expense_claim.submit",
        )
        == 1
    )


def test_repeated_approve_keeps_exactly_one_transition(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A manager approving twice moves the claim once and audits once."""
    owner = auth_headers(client, world.claimant)
    manager = auth_headers(client, world.manager)
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200

    first = client.post(f"/claims/{claim_id}/approve", headers=manager)
    assert first.status_code == 200
    for _ in range(3):
        assert client.post(f"/claims/{claim_id}/approve", headers=manager).status_code == 409

    assert _claim_status(qa_engine, claim_id) == "approved"
    assert (
        _count(
            qa_engine,
            AuditEvent,
            resource_id=str(claim_id),
            action="expense_claim.approve",
        )
        == 1
    )


# --- 2. duplicate Outbox processing ------------------------------------------


def test_enqueue_event_same_idempotency_key_creates_one_row(
    qa_engine: Engine, world: World
) -> None:
    """``enqueue_event`` with the same key returns the existing row, never a second."""
    with Session(qa_engine) as session:
        first = enqueue_event(
            session,
            event_type="approval.requested",
            aggregate_type="workflow_instance",
            aggregate_id="9001",
            idempotency_key="W1-14-K-DUP",
            payload={"approver_id": world.manager.id},
        )
        second = enqueue_event(
            session,
            event_type="approval.requested",
            aggregate_type="workflow_instance",
            aggregate_id="9001",
            idempotency_key="W1-14-K-DUP",
            payload={"approver_id": world.finance_lead.id},
        )
        session.commit()
        assert first.id == second.id
        # The existing payload is never overwritten.
        assert second.payload == {"approver_id": world.manager.id}

    assert _count(qa_engine, OutboxEvent, idempotency_key="W1-14-K-DUP") == 1


def test_process_event_twice_creates_one_notification(
    qa_engine: Engine, world: World
) -> None:
    """Re-processing the same event returns the existing notification, no second row."""
    with Session(qa_engine) as session:
        event = enqueue_event(
            session,
            event_type="approval.requested",
            aggregate_type="workflow_instance",
            aggregate_id="9002",
            idempotency_key="W1-14-K-NOTIF",
            payload={"approver_id": world.manager.id},
        )
        session.commit()

        first = process_event(session, event)
        second = process_event(session, event)
        session.commit()

        assert first is not None and second is not None
        assert first.id == second.id

    assert _count(qa_engine, Notification, dedup_key="approval.requested:W1-14-K-NOTIF") == 1
    assert _count(qa_engine, Notification) == 1


def test_start_approval_is_idempotent_per_business(
    qa_engine: Engine, world: World
) -> None:
    """Re-starting the same workflow yields one instance, one step, one event."""
    with Session(qa_engine) as session:
        first = start_approval(
            session,
            business_type="expense_claim",
            business_id=9100,
            workflow_type="expense_approval",
            approver_id=world.manager.id,
        )
        session.commit()
        second = start_approval(
            session,
            business_type="expense_claim",
            business_id=9100,
            workflow_type="expense_approval",
            approver_id=world.finance_lead.id,
        )
        session.commit()
        assert first.id == second.id
        instance_id = first.id

    assert _count(qa_engine, WorkflowInstance, business_id=9100) == 1
    assert _count(qa_engine, ApprovalStep, instance_id=instance_id) == 1
    assert _count(qa_engine, OutboxEvent, aggregate_id=str(instance_id)) == 1


# --- 3. duplicate payment registration ---------------------------------------


def test_duplicate_payment_registration_creates_one_payment_row(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Registering the same payment twice leaves exactly one payment row."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200
    assert (
        client.post(
            f"/claims/{claim_id}/approve", headers=auth_headers(client, world.manager)
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/claims/{claim_id}/finance-review",
            headers=auth_headers(client, world.finance_operator),
        ).status_code
        == 200
    )

    payer = auth_headers(client, world.finance_lead)
    first = client.post(
        f"/claims/{claim_id}/payments",
        json={"amount": "100.00", "payment_ref": "PAY-DUP"},
        headers=payer,
    )
    assert first.status_code == 201
    assert first.json()["status"] == "paid"

    for _ in range(3):
        repeat = client.post(
            f"/claims/{claim_id}/payments",
            json={"amount": "100.00", "payment_ref": "PAY-DUP"},
            headers=payer,
        )
        assert repeat.status_code == 409
        assert repeat.json()["error"]["code"] == "invalid_transition"

    assert _count(qa_engine, ReimbursementPayment, claim_id=claim_id) == 1
    assert _claim_status(qa_engine, claim_id) == "paid"
    assert (
        _count(
            qa_engine,
            AuditEvent,
            resource_id=str(claim_id),
            action="expense_claim.record_payment",
        )
        == 1
    )


def test_duplicate_withdraw_creates_one_event(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Withdrawing twice moves the claim once and audits once."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200

    assert client.post(f"/claims/{claim_id}/withdraw", headers=owner).status_code == 200
    assert client.post(f"/claims/{claim_id}/withdraw", headers=owner).status_code == 409

    assert _claim_status(qa_engine, claim_id) == "withdrawn"
    assert (
        _count(
            qa_engine,
            AuditEvent,
            resource_id=str(claim_id),
            action="expense_claim.withdraw",
        )
        == 1
    )


def test_create_endpoint_is_not_idempotent_by_design(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """``POST /claims`` has no idempotency key: each call is a distinct draft.

    Recorded explicitly so the boundary of the idempotency guarantee is visible:
    idempotency is enforced for state transitions, Outbox events and payment
    registration -- not for claim creation, which is a new business object each
    time (the caller/agent must not retry a create blindly).
    """
    owner = auth_headers(client, world.claimant)
    first = create_claim(client, owner)
    second = create_claim(client, owner)
    assert first != second
    with Session(qa_engine) as session:
        count = session.scalar(
            select(func.count()).select_from(ExpenseClaim).where(
                ExpenseClaim.employee_id == world.claimant.id
            )
        )
    assert count == 2
