"""End-to-end lifecycle: dispatching, stopping, failure, terminal immutability."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.governance.grants import get_grant
from app.tasks import worker
from app.tasks.errors import TaskError
from app.tasks.models import Task, TaskAttempt, TaskEvent
from app.tasks.service import (
    complete_task,
    confirm_stop,
    fail_task,
    mark_running,
    record_attempt_result,
    request_cancel,
    start_attempt,
)
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    EVIDENCE_REF,
    claim,
    dispatch_and_run,
    evidence,
    move_to_verifying,
    submit_read,
    succeed,
)


def _events(session: Session, task: Task) -> list[TaskEvent]:
    return list(
        session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.task_id)
            .order_by(TaskEvent.sequence)
        )
    )


def test_success_path_records_every_legal_edge(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    succeed(session, world, task)
    session.commit()

    assert task.state == "succeeded"
    assert task.attempt == 1
    assert task.result["verification"]["result"] == "passed"
    assert task.stop_confirmed is False
    assert task.completed_at is not None
    assert [event.to_state for event in _events(session, task)] == [
        "draft",
        "evaluated",
        "queued",
        "dispatched",
        "running",
        "verifying",
        "succeeded",
    ]
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.state == "succeeded"
    assert attempt.ended_at is not None
    grant = get_grant(session, task.grant_id)
    assert grant.status in {"revoked", "consumed"}
    assert task.grant_status in {"revoked", "consumed"}
    assert "succeeded" not in (task.result.get("error_code") or "")


def test_pre_execution_cancel_needs_no_stop_evidence(
    session: Session, world: TaskWorld
) -> None:
    from app.tests.tasks.helpers import submit_gated

    task = submit_gated(session, world)
    session.commit()
    assert task.state == "awaiting_confirmation"
    cancelled = request_cancel(
        session, task_id=task.task_id, actor=world.actors.requester
    )
    session.commit()

    assert cancelled.state == "cancelled"
    assert cancelled.stop_confirmed is True
    assert cancelled.result["stop_evidence_refs"] == [
        "evidence:task_never_dispatched"
    ]
    assert cancelled.attempt == 0
    assert cancelled.grant_status in {"revoked", "consumed", None}
    assert _events(session, cancelled)[-1].event_type == "task.cancelled"


def test_cancel_request_is_not_a_stop(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    session.commit()

    requested = request_cancel(
        session, task_id=task.task_id, actor=world.actors.requester
    )
    session.commit()
    assert requested.state == "cancel_requested"
    assert requested.stop_confirmed is False
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.ended_at is None, "a request must not close the active attempt"
    assert requested.grant_status == "active"

    stopped = confirm_stop(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        evidence_refs=["evidence:execution_stopped_0001"],
        side_effects_reconciled=True,
    )
    session.commit()
    assert stopped.state == "cancelled"
    assert stopped.stop_confirmed is True
    session.refresh(attempt)
    assert attempt.state == "cancelled"
    assert attempt.ended_at is not None
    assert stopped.grant_status in {"revoked", "consumed"}


def test_confirm_stop_without_evidence_is_refused(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    request_cancel(session, task_id=task.task_id, actor=world.actors.requester)
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        confirm_stop(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            evidence_refs=[],
        )
    assert excinfo.value.reason_code == "evidence_missing"
    assert task.state == "cancel_requested"
    session.rollback()


def test_terminal_states_are_immutable_at_the_service_layer(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    succeed(session, world, task)
    session.commit()
    revision = task.state_revision
    event_count = len(_events(session, task))

    for call in (
        lambda: request_cancel(
            session, task_id=task.task_id, actor=world.actors.requester
        ),
        lambda: fail_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            reason="late",
        ),
        lambda: complete_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            result_ref="result_late_0001",
            evidence_refs=[EVIDENCE_REF],
            evidence=[evidence()],
        ),
    ):
        with pytest.raises(TaskError) as excinfo:
            call()
        assert excinfo.value.reason_code in {
            "task_terminal_state_immutable",
            "task_forbidden_transition",
            "task_invalid_transition",
        }

    session.commit()
    session.refresh(task)
    assert task.state == "succeeded"
    assert task.state_revision == revision
    assert len(_events(session, task)) == event_count
    rejected = session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "task.transition.rejected",
            AuditEvent.resource_id == task.task_id,
        )
    )
    assert rejected is not None


def test_lease_and_fencing_are_mandatory(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()

    with pytest.raises(TaskError) as excinfo:
        start_attempt(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            execution_ref="exec_no_lease_0001",
        )
    assert excinfo.value.reason_code == "task_lease_not_held"
    session.rollback()

    lease = claim(session, world)
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        start_attempt(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            worker_id="worker-1",
            fencing_token=lease.lease.fencing_token + 99,
            execution_ref="exec_stale_0001",
        )
    assert excinfo.value.reason_code == "task_fencing_token_stale"
    session.rollback()


def test_failed_execution_requires_stop_and_revokes_the_grant(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    session.commit()

    with pytest.raises(TaskError) as excinfo:
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="failed",
            evidence_refs=["evidence:execution_failed_0001"],
            stop_confirmed=False,
            error_code="runtime_error",
        )
    assert excinfo.value.reason_code == "stop_not_confirmed"
    session.rollback()

    failed = record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="failed",
        evidence_refs=["evidence:execution_failed_0001"],
        stop_confirmed=True,
        error_code="runtime_error",
    )
    session.commit()
    assert failed.state == "failed"
    assert failed.result["verification"].get("result", "pending") == "pending"
    assert failed.grant_status in {"revoked", "consumed"}
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.state == "failed"
    assert attempt.ended_at is not None


def test_execution_lost_enters_unknown_and_revokes_grant(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    session.commit()

    lost = record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="lost",
        evidence_refs=["evidence:heartbeat_lost_0001"],
    )
    session.commit()

    assert lost.state == "unknown"
    assert lost.grant_status == "revoked"
    assert get_grant(session, lost.grant_id).revocation_reason == (
        "execution_evidence_lost"
    )
    # unknown does not close the attempt: reconciliation must still resolve it.
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.ended_at is None


def test_restart_recovery_of_an_expired_lease_is_fail_closed(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    lease = claim(session, world, worker_id="worker-1")
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-1",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_restart_0001",
    )
    session.commit()

    from datetime import timedelta

    from app.tests.tasks.helpers import utcnow

    future = utcnow() + timedelta(hours=1)
    recoveries = worker.recover_expired_leases(
        session, actor=world.actors.executor, checker=None, now=future
    )
    session.commit()

    assert [item.action for item in recoveries] == ["entered_unknown"]
    session.refresh(task)
    assert task.state == "unknown"
    assert task.grant_status == "revoked"
    assert task.attempt == 1, "an unknown attempt is never rolled back"


def test_expired_lease_with_proof_of_no_acceptance_rolls_back_the_attempt(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    lease = claim(session, world, worker_id="worker-1")
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-1",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_restart_0002",
    )
    session.commit()
    budget = task.retry_budget_remaining

    from datetime import timedelta

    from app.tests.tasks.helpers import utcnow

    def checker(*, task, attempt_number, idempotency_key):
        return ("not_accepted", True)

    recoveries = worker.recover_expired_leases(
        session,
        actor=world.actors.executor,
        checker=checker,
        now=utcnow() + timedelta(hours=1),
    )
    session.commit()

    assert [item.action for item in recoveries] == ["attempt_rolled_back"]
    session.refresh(task)
    assert task.state == "queued"
    assert task.attempt == 0
    assert task.retry_budget_remaining == budget
    assert task.grant_status == "active"
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.state == "abandoned"

    # A fresh worker can pick it up with a new fencing token.
    again = claim(session, world, worker_id="worker-2")
    session.commit()
    assert again.lease.fencing_token == lease.lease.fencing_token + 1


def test_no_blind_replay_after_unknown(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="lost",
        evidence_refs=["evidence:lost_0001"],
    )
    session.commit()

    from app.tasks.service import recheck_execution

    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability="expense.read@1.0.0",
            call_id="call_replay_0001",
        )
    assert excinfo.value.reason_code == "grant_revoked"
    session.rollback()
    session.refresh(task)
    assert task.state == "unknown"
