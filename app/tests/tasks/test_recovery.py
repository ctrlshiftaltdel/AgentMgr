"""Recovery from ``unknown`` and bounded retry (no blind replay)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.models import GovernanceDecisionRecord
from app.tasks.errors import TaskError
from app.tasks.models import TaskAttempt
from app.tasks.service import (
    reconcile_task,
    record_attempt_result,
    record_dispatch_failure,
    retry_task,
    takeover_task,
)
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import dispatch_and_run, submit_read


def _unknown_without_attempt(session: Session, world: TaskWorld):
    task = submit_read(session, world)
    session.commit()
    old_grant = task.grant_id
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="timeout",
        reason="dispatch_timeout",
    )
    session.commit()
    assert task.state == "unknown"
    assert task.attempt == 0
    assert task.grant_status == "revoked"
    return task, old_grant


def _failed_attempt(session: Session, world: TaskWorld, **submit_overrides):
    task = submit_read(session, world, **submit_overrides)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="failed",
        evidence_refs=["evidence:execution_failed_0001"],
        stop_confirmed=True,
        side_effects_reconciled=True,
        error_code="runtime_error",
    )
    session.commit()
    assert task.state == "failed"
    return task


def test_not_found_requires_explicit_no_side_effect_evidence(
    session: Session, world: TaskWorld
) -> None:
    task, _ = _unknown_without_attempt(session, world)
    with pytest.raises(TaskError) as excinfo:
        reconcile_task(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="not_found",
            no_side_effect_evidence=False,
        )
    assert excinfo.value.reason_code == "task_reconcile_evidence_missing"
    assert task.state == "unknown"


def test_not_found_requeues_only_with_a_fresh_decision_and_grant(
    session: Session, world: TaskWorld
) -> None:
    task, old_grant = _unknown_without_attempt(session, world)
    budget = task.retry_budget_remaining
    reconcile_task(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="not_found",
        no_side_effect_evidence=True,
        evidence_refs=["evidence:not_found_0001"],
    )
    session.commit()

    assert task.state == "queued"
    assert task.attempt == 0
    assert task.retry_budget_remaining == budget
    assert task.grant_id != old_grant
    assert task.grant_status == "active"
    assert task.decision_purpose == "retry"
    record = session.scalar(
        select_decision(session, task.decision_id)
    )
    assert record.decision_revision >= 2
    assert record.decision_purpose == "retry"
    assert record.supersedes_decision_id is not None


def select_decision(session: Session, decision_id: str):
    return select(GovernanceDecisionRecord).where(
        GovernanceDecisionRecord.decision_id == decision_id
    )


def test_not_found_is_forbidden_when_an_attempt_started(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="lost",
        evidence_refs=["evidence:lost_0002"],
    )
    session.commit()
    assert task.state == "unknown"

    with pytest.raises(TaskError) as excinfo:
        reconcile_task(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="not_found",
            no_side_effect_evidence=True,
            evidence_refs=["evidence:not_found_0002"],
        )
    assert excinfo.value.reason_code == "task_active_attempt_unreconciled"


def test_reconcile_stopped_closes_the_task(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="lost",
        evidence_refs=["evidence:lost_0003"],
    )
    session.commit()

    reconcile_task(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="stopped",
        stop_confirmed=True,
        evidence_refs=["evidence:stop_0003"],
    )
    session.commit()
    assert task.state == "cancelled"
    assert task.stop_confirmed is True
    assert task.grant_status in {"revoked", "consumed"}
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.state == "cancelled"
    assert attempt.ended_at is not None


def test_reconcile_running_goes_to_human_and_cannot_be_resumed(
    session: Session, world: TaskWorld
) -> None:
    task, _ = _unknown_without_attempt(session, world)
    reconcile_task(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="running",
        evidence_refs=["evidence:still_running_0001"],
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "unknown"

    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="running",
        )
    assert excinfo.value.reason_code == "grant_revoked"


def test_reconcile_completed_goes_to_human_not_verification(
    session: Session, world: TaskWorld
) -> None:
    task, _ = _unknown_without_attempt(session, world)
    reconcile_task(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="completed",
        evidence_refs=["evidence:reconciled_result_0001"],
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "unknown"

    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="result_submitted",
            result_ref="result_human_0001",
            evidence_refs=["evidence:human_result_0001"],
        )
    assert excinfo.value.reason_code == "grant_revoked"


def test_retry_is_bounded_and_uses_a_fresh_decision(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world)
    old_grant = task.grant_id
    old_decision = task.decision_id
    budget = task.retry_budget_remaining
    attempt = task.attempt

    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "queued"
    assert task.retry_budget_remaining == budget - 1
    assert task.attempt == attempt
    assert task.grant_id != old_grant
    assert task.grant_status == "active"
    assert task.decision_id != old_decision
    assert task.decision_purpose == "retry"
    record = session.scalar(select_decision(session, task.decision_id))
    assert record.decision_revision >= 2
    assert record.supersedes_decision_id == old_decision


def test_exhausted_budget_escalates_instead_of_retrying(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, retry_budget=0)
    grant = task.grant_id
    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "failed"
    assert task.grant_id == grant  # no new grant was issued
    assert task.grant_status == "revoked"


def test_unsafe_action_is_not_replayed(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, retry_safe=False)
    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "failed"


def test_attempt_zero_failure_cannot_retry(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="rejected",
        reason="boundary_rejected",
    )
    session.commit()
    assert task.state == "failed"
    assert task.attempt == 0

    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "needs_human"
    assert task.attempt == 0
