"""Regression coverage for the owner-authorised round-4 fix (PR #89 / Issue #79).

C1  -- the retry decision chain (supersede the latest decision), the
       ``action_safely_retryable`` override and T70 -> T61 redispatch.
I1  -- the task-before-lease lock order in lease recovery.
I2  -- runtime accounting by actual execution time, not queue/human time, and
       excluding T33-voided attempts.
I3  -- a managed ``queued`` deadline is escalated, never silently skipped.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.governance.models import GovernanceDecisionRecord
from app.governance.principal import resolve_service_principal
from app.tasks import service, worker
from app.tasks.errors import TaskError
from app.tasks.models import Task, TaskAttempt
from app.tasks.service import (
    record_attempt_result,
    retry_task,
    takeover_task,
)
from app.tests.governance.helpers import make_service_identity
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import claim, dispatch_and_run, submit_read, utcnow

TASK_RUNTIME_CAP = 300


def _failed_attempt(
    session: Session,
    world: TaskWorld,
    *,
    key: str,
    **overrides,
) -> Task:
    task = submit_read(session, world, key=key, **overrides)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="failed",
        evidence_refs=["evidence:round4_failed"],
        stop_confirmed=True,
        side_effects_reconciled=True,
        error_code="runtime_error",
    )
    session.commit()
    assert task.state == "failed"
    return task


def _current_attempt(session: Session, task: Task) -> TaskAttempt:
    row = session.scalar(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.task_id)
        .order_by(TaskAttempt.attempt_number.desc())
        .limit(1)
    )
    assert row is not None
    return row


def _latest_decision(session: Session, task: Task) -> GovernanceDecisionRecord:
    row = session.scalar(
        select(GovernanceDecisionRecord)
        .where(GovernanceDecisionRecord.task_id == task.task_id)
        .order_by(GovernanceDecisionRecord.decision_revision.desc())
        .limit(1)
    )
    assert row is not None
    return row


def _decision(session: Session, decision_id: str) -> GovernanceDecisionRecord:
    row = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == decision_id
        )
    )
    assert row is not None
    return row


def _managed_task(
    session: Session, world: TaskWorld, *, key: str
) -> tuple[Task, object]:
    identity = make_service_identity(session, owner=world.org.admin)
    service_actor = resolve_service_principal(session, identity.id)
    task = submit_read(
        session,
        world,
        key=key,
        executor=service_actor,
        requested_capabilities=["managed.task@1.0.0"],
        execution_mode="managed_execution",
        runtime_capabilities_verified=True,
    )
    session.commit()
    assert task.state == "queued"
    return task, service_actor


# ---------------------------------------------------------------------------
# C1 -- the retry decision chain is never poisoned by a refused retry
# ---------------------------------------------------------------------------


def test_refused_retry_does_not_poison_the_decision_chain(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r4_chain_refused")
    budget_before = task.retry_budget_remaining
    task.owner_account_status = "disabled"
    task.owner_escalation = {
        "reason_code": "accountable_owner_inactive",
        "owner_principal_id": task.owner_principal_id,
        "requires_human": True,
    }
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    assert excinfo.value.reason_code == "task_owner_inactive"
    session.commit()
    assert task.state == "failed"

    # The refused retry persisted its allow decision even though the guard
    # rejected the transition, so the task row still points at the older one.
    refused = _latest_decision(session, task)
    assert refused.decision_purpose == "retry"
    assert refused.decision_id != task.decision_id

    task.owner_account_status = "active"
    task.owner_escalation = None
    session.flush()

    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()

    assert task.state == "queued"
    assert task.grant_status == "active"
    assert task.retry_budget_remaining == budget_before - 1
    granted = _decision(session, task.decision_id)
    # The new retry joins the real chain: it supersedes the refused decision,
    # not the older decision still bound to the task row.
    assert granted.supersedes_decision_id == refused.decision_id
    assert granted.decision_revision == refused.decision_revision + 1


def test_action_safely_retryable_override_authorises_the_retry(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(
        session, world, key="idem_r4_retry_override", retry_safe=False
    )

    retry_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action_safely_retryable=True,
    )
    session.commit()

    assert task.state == "queued"
    assert task.grant_status == "active"
    assert task.retry_budget_remaining >= 0


def test_t70_escalated_task_can_be_redispatched(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r4_t70_redispatch")
    task.owner_account_status = "disabled"
    task.owner_escalation = {
        "reason_code": "accountable_owner_inactive",
        "owner_principal_id": task.owner_principal_id,
        "requires_human": True,
    }
    session.flush()

    # First retry is refused by the owner guard and leaves an unbound decision.
    with pytest.raises(TaskError):
        retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    refused = _latest_decision(session, task)

    # An unsafe action escalates to a human (T70) without touching the chain.
    retry_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action_safely_retryable=False,
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "failed"

    task.owner_account_status = "active"
    task.owner_escalation = None
    session.flush()

    # The runtime budget still allows a dispatch, so T61 must succeed.
    takeover_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action="redispatch",
        evidence_refs=["evidence:round4_t61_redispatch"],
        side_effects_reconciled=True,
    )
    session.commit()

    assert task.state == "queued"
    assert task.grant_status == "active"
    redispatch = _decision(session, task.decision_id)
    assert redispatch.supersedes_decision_id == refused.decision_id


# ---------------------------------------------------------------------------
# I1 -- lease recovery takes the task lock before the lease lock
# ---------------------------------------------------------------------------


def test_lease_recovery_skips_a_task_locked_by_another_writer(
    session: Session, world: TaskWorld, engine
) -> None:
    task = submit_read(session, world, key="idem_r4_lock_order")
    session.commit()
    claimed = claim(session, world, worker_id="worker-r4-lock")
    assert claimed.task.task_id == task.task_id
    session.commit()
    expired_at = utcnow() + timedelta(hours=1)

    holder = engine.connect()
    holder_transaction = holder.begin()
    try:
        holder.execute(
            text("SELECT 1 FROM tasks WHERE task_id = :task_id FOR UPDATE"),
            {"task_id": task.task_id},
        )
        with Session(engine) as contender:
            # The recovery pass must skip the locked task instead of blocking
            # on the lease first (the pre-fix lock order) and timing out.
            contender.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            skipped = worker.recover_expired_leases(contender, now=expired_at)
            contender.commit()
            assert skipped == []
    finally:
        holder_transaction.rollback()
        holder.close()

    with Session(engine) as recovery:
        recovered = worker.recover_expired_leases(recovery, now=expired_at)
        recovery.commit()
    assert [item.action for item in recovered] == ["released"]
    assert recovered[0].task_id == task.task_id


# ---------------------------------------------------------------------------
# I2 -- runtime is actual execution time only
# ---------------------------------------------------------------------------


def test_runtime_ignores_queue_and_human_time(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r4_runtime_exec")
    attempt = _current_attempt(session, task)
    now = utcnow()
    # Two hours of queue/human time around a ten-second execution window.
    attempt.started_at = now - timedelta(hours=2)
    attempt.ended_at = attempt.started_at + timedelta(seconds=10)
    session.flush()

    retry_task(session, task_id=task.task_id, actor=world.actors.owner, now=now)
    session.commit()

    assert task.state == "queued"
    elapsed = service._attempt_runtime_elapsed(session, task, now=now)
    assert elapsed is not None
    assert elapsed <= timedelta(seconds=TASK_RUNTIME_CAP)


def test_runtime_excludes_a_t33_voided_attempt(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r4_runtime_voided")
    now = utcnow()
    # A T33 rollback leaves an ``abandoned`` attempt: the submission was never
    # accepted, so the wall-clock window it spans is not execution time.
    session.add(
        TaskAttempt(
            task_id=task.task_id,
            attempt_number=task.attempt + 1,
            state="abandoned",
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(minutes=90),
        )
    )
    session.flush()

    elapsed = service._attempt_runtime_elapsed(session, task, now=now)
    assert elapsed is not None
    assert elapsed < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# I3 -- a managed queued deadline is escalated, never swallowed
# ---------------------------------------------------------------------------


def test_managed_queued_deadline_is_escalated(
    session: Session, world: TaskWorld
) -> None:
    task, service_actor = _managed_task(session, world, key="idem_r4_managed_sla")
    task.due_at = utcnow() - timedelta(minutes=1)
    session.commit()

    results = worker.escalate_overdue_tasks(
        session, actor=service_actor, now=utcnow()
    )
    session.commit()

    assert len(results) == 1
    assert results[0].task_id == task.task_id
    assert results[0].action == "needs_human"
    assert results[0].reason_code == "task_deadline_exceeded"
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"
    assert task.grant_status == "revoked"
