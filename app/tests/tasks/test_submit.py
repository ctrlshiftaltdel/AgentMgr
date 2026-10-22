"""Submission: idempotency, necessity decisions, responsibility and atomicity."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.governance.models import GovernanceDecisionRecord
from app.tasks.errors import IdempotencyConflict, OwnerError, TaskError
from app.tasks.models import Task, TaskEvent
from app.tasks.service import submit_task
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import READ_CAPABILITY, goal, submit_read, succeed
from app.workflow.models import OutboxEvent


def test_submit_queues_with_grant_and_records_event_audit_outbox(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()

    assert task.state == "queued"
    assert task.attempt == 0
    assert task.grant_id is not None
    assert task.grant_status == "active"
    assert task.record_completeness == "complete"
    assert task.owner_principal_id == world.actors.owner.principal_id
    assert task.requester_principal_id == world.actors.requester.principal_id
    assert task.executor_principal_id == world.actors.executor.principal_id

    events = list(
        session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.task_id)
            .order_by(TaskEvent.sequence)
        )
    )
    assert [event.event_type for event in events] == [
        "task.created",
        "task.evaluated",
        "task.queued",
    ]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.from_state for event in events] == [None, "draft", "evaluated"]
    assert [event.to_state for event in events] == [
        "draft",
        "evaluated",
        "queued",
    ]
    assert task.state_revision == 3
    assert task.last_event_id == events[-1].event_id

    outbox = list(
        session.scalars(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == task.task_id)
        )
    )
    assert {event.event_type for event in outbox} == {
        "task.created",
        "task.evaluated",
        "task.queued",
    }
    assert all(event.aggregate_type == "task" for event in outbox)

    audits = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.resource_type == "task",
                AuditEvent.resource_id == task.task_id,
            )
        )
    )
    assert {
        "task.event.task.created",
        "task.event.task.evaluated",
        "task.event.task.queued",
    } <= {event.action for event in audits}

    decisions = session.scalar(
        select(func.count())
        .select_from(GovernanceDecisionRecord)
        .where(GovernanceDecisionRecord.task_id == task.task_id)
    )
    assert decisions == 1


def test_same_key_same_request_is_idempotent(session: Session, world: TaskWorld) -> None:
    first = submit_read(session, world)
    session.commit()
    events_before = session.scalar(
        select(func.count())
        .select_from(TaskEvent)
        .where(TaskEvent.task_id == first.task_id)
    )

    again = submit_read(session, world)
    session.commit()

    assert again.task_id == first.task_id
    assert (
        session.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(TaskEvent.task_id == first.task_id)
        )
        == events_before
    )
    assert (
        session.scalar(select(func.count()).select_from(Task)) == 1
    )


def test_same_key_different_payload_is_a_conflict(
    session: Session, world: TaskWorld
) -> None:
    submit_read(session, world)
    session.commit()
    with pytest.raises(IdempotencyConflict) as excinfo:
        submit_read(
            session, world, goal=goal(summary="A different goal entirely")
        )
    assert excinfo.value.reason_code == "task_idempotency_conflict"


def test_duplicate_live_task_is_rejected(session: Session, world: TaskWorld) -> None:
    first = submit_read(session, world)
    session.commit()
    with pytest.raises(TaskError) as excinfo:
        submit_read(session, world, key="idem_task_read_0002")
    assert excinfo.value.reason_code == "task_duplicate"
    assert excinfo.value.context["existing_task_id"] == first.task_id


def test_already_satisfied_goal_is_rejected(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    succeed(session, world, task)
    session.commit()

    with pytest.raises(TaskError) as excinfo:
        submit_read(session, world, key="idem_task_read_0003")
    assert excinfo.value.reason_code == "task_already_satisfied"
    assert excinfo.value.context["existing_task_id"] == task.task_id


def test_missing_required_input_is_rejected(
    session: Session, world: TaskWorld
) -> None:
    with pytest.raises(TaskError) as excinfo:
        submit_read(
            session,
            world,
            goal=goal(required_inputs=["synthetic receipt reference"]),
        )
    assert excinfo.value.reason_code == "required_input_missing"
    assert excinfo.value.context["missing_inputs"] == [
        "synthetic receipt reference"
    ]


def test_missing_owner_is_rejected(session: Session, world: TaskWorld) -> None:
    with pytest.raises(OwnerError) as excinfo:
        submit_read(session, world, owner=None)
    assert excinfo.value.reason_code == "accountable_owner_missing"


def test_self_approval_is_rejected_before_creation(
    session: Session, world: TaskWorld
) -> None:
    with pytest.raises(OwnerError) as excinfo:
        submit_read(session, world, approver=world.actors.requester)
    assert excinfo.value.reason_code == "self_approval_forbidden"
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_unauthorized_executor_is_denied_by_the_pdp(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, executor=world.actors.outsider)
    session.commit()

    assert task.state == "denied"
    assert task.decision_type == "deny"
    assert task.grant_id is None
    assert task.attempt == 0
    record = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == task.decision_id
        )
    )
    assert record is not None
    assert "capability_not_granted" in record.reason_codes
    denied = session.scalar(
        select(TaskEvent).where(
            TaskEvent.task_id == task.task_id,
            TaskEvent.event_type == "governance.denied",
        )
    )
    assert denied is not None


def test_concurrent_duplicate_submission_has_one_side_effect(
    engine, world: TaskWorld
) -> None:
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        with Session(engine) as session:
            from app.governance.principal import resolve_employee_principal

            requester = resolve_employee_principal(
                session, employee_id=world.actors.requester.employee_id
            )
            owner = resolve_employee_principal(
                session, employee_id=world.actors.owner.employee_id
            )
            barrier.wait(timeout=10)
            try:
                task = submit_read(
                    session, world, requester=requester, owner=owner
                )
                session.commit()
                results.append((task.task_id, task.state))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the test
                session.rollback()
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert len(results) == 2
    assert results[0][0] == results[1][0]

    with Session(engine) as check:
        assert check.scalar(select(func.count()).select_from(Task)) == 1
        task_id = results[0][0]
        assert (
            check.scalar(
                select(func.count())
                .select_from(TaskEvent)
                .where(
                    TaskEvent.task_id == task_id,
                    TaskEvent.event_type == "task.created",
                )
            )
            == 1
        )
        assert (
            check.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "task.created")
            )
            == 1
        )
        assert (
            check.scalar(
                select(func.count())
                .select_from(GovernanceDecisionRecord)
                .where(GovernanceDecisionRecord.task_id == task_id)
            )
            == 1
        )


def test_missing_scope_goes_to_awaiting_clarification(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, resource_scope=None)
    session.commit()

    assert task.state == "awaiting_clarification"
    assert task.grant_id is None
    assert task.record_completeness == "pre_execution_incomplete"
    assert task.incomplete_reason == "scope_undetermined"
    reasons = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == task.decision_id
        )
    ).reason_codes
    assert "resource_scope_ambiguous" in reasons


def test_priority_is_scheduling_only_and_urgent_needs_authorisation(
    session: Session, world: TaskWorld
) -> None:
    with pytest.raises(TaskError) as excinfo:
        submit_read(session, world, priority="urgent")
    assert excinfo.value.reason_code == "task_priority_unauthorized"

    urgent = submit_read(
        session,
        world,
        key="idem_task_read_urgent",
        priority="urgent",
        priority_authorized_by=world.actors.owner.principal_id,
        priority_rationale="synthetic legal deadline",
    )
    session.commit()
    assert urgent.priority == "urgent"
    assert urgent.state == "queued"  # priority did not alter the gates
