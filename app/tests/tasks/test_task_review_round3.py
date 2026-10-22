"""Regression coverage for the round-3 review of PR #89.

Scenarios mirror the round-3 items: the cumulative task runtime cap on every
requeue path (F1), managed crash recovery (F2), T34 lease/fencing authority (F3),
validate-before-write on the remaining writers (F4), grant/budget restoration
and honest reconciliation facts (F5), the PEP queued dead end (F6), a recoverable
queue-guard failure (F7) and the SLA escalation label (F8).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.governance.grants import get_grant
from app.governance.models import ExecutionGrant
from app.governance.principal import (
    resolve_service_principal,
)
from app.tasks import service, worker
from app.tasks.errors import TaskError
from app.tasks.models import Task, TaskAttempt, TaskLease
from app.tasks.service import (
    acquire_recovery_lease,
    complete_task,
    confirm_stop,
    confirm_task,
    deny_task,
    dispatch_deadline_exceeded,
    escalate_owner_account,
    escalate_task,
    evidence_unavailable,
    mark_running,
    reconcile_task,
    record_approval,
    record_attempt_result,
    record_dispatch_failure,
    recheck_execution,
    retry_task,
    start_attempt,
    takeover_task,
)
from app.tests.governance.helpers import make_service_identity
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    EVIDENCE_REF,
    GATED_CAPABILITY,
    claim,
    dispatch_and_run,
    evidence,
    move_to_verifying,
    pending_confirmation,
    submit_gated,
    submit_read,
    utcnow,
)

TASK_RUNTIME_CAP = 300


def _failed_attempt(
    session: Session,
    world: TaskWorld,
    *,
    key: str,
    side_effects_reconciled: bool = True,
    **overrides,
):
    task = submit_read(session, world, key=key, **overrides)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="failed",
        evidence_refs=["evidence:round3_failed"],
        stop_confirmed=True,
        side_effects_reconciled=side_effects_reconciled,
        error_code="runtime_error",
    )
    session.commit()
    assert task.state == "failed"
    return task


def _requeued(session: Session, world: TaskWorld, *, key: str):
    task = _failed_attempt(session, world, key=key)
    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "queued"
    assert task.attempt == 1
    return task


def _attempt(session: Session, task: Task) -> TaskAttempt:
    row = session.scalar(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.task_id)
        .order_by(TaskAttempt.attempt_number.desc())
        .limit(1)
    )
    assert row is not None
    return row


def _managed_task(session: Session, world: TaskWorld, *, key: str):
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


def _open_lease(session: Session, task: Task) -> TaskLease:
    lease = session.scalar(
        select(TaskLease)
        .where(TaskLease.task_id == task.task_id, TaskLease.released_at.is_(None))
        .order_by(TaskLease.id.desc())
        .limit(1)
    )
    assert lease is not None, "expected an open lease"
    return lease


def _release_open_lease(session: Session, task: Task) -> None:
    lease = session.scalar(
        select(TaskLease).where(
            TaskLease.task_id == task.task_id, TaskLease.released_at.is_(None)
        )
    )
    if lease is not None:
        lease.released_at = utcnow()
        session.flush()


def _active_grants(session: Session, task_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ExecutionGrant)
            .where(
                ExecutionGrant.task_id == task_id,
                ExecutionGrant.status == "active",
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------
# F1 -- the cumulative task runtime cap must never strand a requeued task
# ---------------------------------------------------------------------------


def test_retry_after_the_cumulative_runtime_cap_escalates(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r3_runtime_retry")
    old_grant = task.grant_id
    attempt = _attempt(session, task)
    # A closed attempt's contribution is ``ended_at - started_at``.
    attempt.started_at = utcnow() - timedelta(seconds=TASK_RUNTIME_CAP + 1)
    attempt.ended_at = utcnow()
    session.flush()

    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()

    # No requeue: a queued task that could never be dispatched is a dead end.
    assert task.state == "needs_human"
    assert task.needs_human_origin == "failed"
    assert task.grant_id == old_grant  # no new grant was bound
    assert _active_grants(session, task.task_id) == 0
    assert task.result["outcome"] != "none"


def test_retry_within_the_cumulative_runtime_cap_still_queues(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r3_runtime_retry_ok")
    attempt = _attempt(session, task)
    attempt.started_at = utcnow() - timedelta(seconds=10)
    attempt.ended_at = utcnow()
    session.flush()

    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "queued"
    assert task.grant_status == "active"
    assert _active_grants(session, task.task_id) == 1

    # The requeued task is genuinely dispatchable (the failed attempt's lease is
    # still the current one).
    lease = _open_lease(session, task)
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
        execution_ref="exec_r3_runtime_ok_0001",
    )
    session.flush()
    assert task.state == "dispatched"


def test_t61_redispatch_after_the_cumulative_runtime_cap_is_refused(
    session: Session, world: TaskWorld
) -> None:
    task = _requeued(session, world, key="idem_r3_runtime_t61")
    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="dispatch_acceptance_unreconciled",
    )
    session.commit()
    assert task.state == "needs_human"
    attempt = _attempt(session, task)
    # A closed attempt's contribution is ``ended_at - started_at``.
    attempt.started_at = utcnow() - timedelta(seconds=TASK_RUNTIME_CAP + 1)
    attempt.ended_at = utcnow()
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="redispatch",
            evidence_refs=["evidence:r3_runtime_t61_stop"],
            side_effects_reconciled=True,
        )
    assert excinfo.value.reason_code == "budget_exceeded"
    session.commit()
    assert task.state == "needs_human"
    assert _active_grants(session, task.task_id) == 0


def test_worker_escalates_a_queued_task_whose_runtime_is_spent(
    session: Session, world: TaskWorld
) -> None:
    task = _requeued(session, world, key="idem_r3_runtime_claim")
    # The dead worker's lease is expired and released by recovery; only then is
    # the row a claim candidate again.
    _release_open_lease(session, task)
    attempt = _attempt(session, task)
    # A closed attempt's contribution is ``ended_at - started_at``.
    attempt.started_at = utcnow() - timedelta(seconds=TASK_RUNTIME_CAP + 1)
    attempt.ended_at = utcnow()
    session.flush()

    assert (
        worker.claim_task(
            session, worker_id="worker-r3-runtime-claim", actor=world.actors.executor
        )
        is None
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"
    assert _active_grants(session, task.task_id) == 0


# ---------------------------------------------------------------------------
# F2 -- managed crash recovery is reachable through a recovery lease
# ---------------------------------------------------------------------------


def _managed_crash(session: Session, world: TaskWorld, *, key: str, future):
    task, service_actor = _managed_task(session, world, key=key)
    lease = claim(session, world, worker_id=f"worker-{key[:18]}")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=service_actor,
        worker_id=f"worker-{key[:18]}",
        fencing_token=lease.lease.fencing_token,
        runtime_ref="rtask_r3_crash_0001",
    )
    session.commit()

    recoveries = worker.recover_expired_leases(session, now=future)
    session.commit()
    assert [item.action for item in recoveries] == ["entered_unknown"]
    assert task.state == "unknown"
    assert lease.lease.released_at is not None
    return task, service_actor, lease


def test_managed_unknown_can_be_reconciled_with_a_recovery_lease(
    session: Session, world: TaskWorld
) -> None:
    future = utcnow() + timedelta(hours=1)
    task, service_actor, crashed = _managed_crash(
        session, world, key="idem_r3_recover_failed", future=future
    )

    recovery = acquire_recovery_lease(
        session, task_id=task.task_id, actor=service_actor, now=future
    )
    session.flush()
    assert recovery.fencing_token > crashed.lease.fencing_token

    reconcile_task(
        session,
        task_id=task.task_id,
        actor=service_actor,
        outcome="failed",
        evidence_refs=["evidence:r3_recovered_failed"],
        stop_confirmed=True,
        worker_id=recovery.worker_id,
        fencing_token=recovery.fencing_token,
        now=future,
    )
    session.commit()
    assert task.state == "failed"
    assert task.grant_status == "revoked"


def test_managed_unknown_can_be_stopped_with_a_recovery_lease(
    session: Session, world: TaskWorld
) -> None:
    future = utcnow() + timedelta(hours=1)
    task, service_actor, crashed = _managed_crash(
        session, world, key="idem_r3_recover_stop", future=future
    )

    recovery = acquire_recovery_lease(
        session, task_id=task.task_id, actor=service_actor, now=future
    )
    session.flush()
    confirm_stop(
        session,
        task_id=task.task_id,
        actor=service_actor,
        evidence_refs=["evidence:r3_recovered_stop"],
        stop_confirmed=True,
        side_effects_reconciled=True,
        worker_id=recovery.worker_id,
        fencing_token=recovery.fencing_token,
        now=future,
    )
    session.commit()
    assert task.state == "cancelled"

    # The crashed worker's token is stale: no fact write is possible.
    with pytest.raises(TaskError) as excinfo:
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=service_actor,
            outcome="completed",
            result_ref="result_r3_stale",
            evidence_refs=[EVIDENCE_REF],
            worker_id=crashed.lease.worker_id,
            fencing_token=crashed.lease.fencing_token,
            now=future,
        )
    assert excinfo.value.reason_code in {
        "task_lease_not_held",
        "task_lease_expired",
        "task_fencing_token_stale",
        "task_terminal_state_immutable",
    }


def test_recovery_lease_refuses_queued_tasks_and_live_leases(
    session: Session, world: TaskWorld
) -> None:
    task, service_actor = _managed_task(session, world, key="idem_r3_recover_guard")
    with pytest.raises(TaskError) as excinfo:
        acquire_recovery_lease(session, task_id=task.task_id, actor=service_actor)
    assert excinfo.value.reason_code == "task_invalid_transition"

    lease = claim(session, world, worker_id="worker-r3-recover-guard")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=service_actor,
        worker_id="worker-r3-recover-guard",
        fencing_token=lease.lease.fencing_token,
        runtime_ref="rtask_r3_recover_guard",
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        acquire_recovery_lease(session, task_id=task.task_id, actor=service_actor)
    assert excinfo.value.reason_code == "task_lease_active"


# ---------------------------------------------------------------------------
# F3 -- T34 authority for a managed task is the lease/fencing token
# ---------------------------------------------------------------------------


def test_managed_t34_requires_the_current_lease_and_fencing_token(
    session: Session, world: TaskWorld
) -> None:
    task, service_actor = _managed_task(session, world, key="idem_r3_t34_lease")
    lease = claim(session, world, worker_id="worker-r3-t34")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=service_actor,
        worker_id="worker-r3-t34",
        fencing_token=lease.lease.fencing_token,
        runtime_ref="rtask_r3_t34_0001",
    )
    session.flush()
    assert task.state == "dispatched"

    # No lease at all: no caller-asserted flag can substitute for it.
    with pytest.raises(TaskError) as excinfo:
        dispatch_deadline_exceeded(
            session,
            task_id=task.task_id,
            actor=service_actor,
            evidence_refs=["evidence:r3_t34_missing"],
            no_side_effects_evidence=True,
        )
    assert excinfo.value.reason_code == "task_lease_not_held"

    # A stale worker cannot declare the window closed.
    with pytest.raises(TaskError) as excinfo:
        dispatch_deadline_exceeded(
            session,
            task_id=task.task_id,
            actor=service_actor,
            evidence_refs=["evidence:r3_t34_stale"],
            no_side_effects_evidence=True,
            worker_id="worker-r3-t34",
            fencing_token=lease.lease.fencing_token + 5,
        )
    assert excinfo.value.reason_code == "task_fencing_token_stale"
    assert task.state == "dispatched"

    # The current lease holder may, and needs no boolean flag.
    dispatch_deadline_exceeded(
        session,
        task_id=task.task_id,
        actor=service_actor,
        evidence_refs=["evidence:r3_t34_held"],
        worker_id="worker-r3-t34",
        fencing_token=lease.lease.fencing_token,
    )
    session.commit()
    assert task.state == "timed_out"
    assert task.stop_confirmed is True


# ---------------------------------------------------------------------------
# F4 -- no writer may stage a fact before the transition is validated
# ---------------------------------------------------------------------------


def _dispatched(session: Session, world: TaskWorld, *, key: str, worker: str):
    task = submit_read(session, world, key=key)
    session.commit()
    lease = claim(session, world, worker_id=worker)
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id=worker,
        fencing_token=lease.lease.fencing_token,
        execution_ref=f"exec_{key[-20:]}",
    )
    session.flush()
    assert task.state == "dispatched"
    return task, lease


def test_mark_running_rejection_leaves_the_attempt_dispatched(
    session: Session, world: TaskWorld
) -> None:
    task, lease = _dispatched(session, world, key="idem_r3_f4_running", worker="w-r3-f4-1")
    with pytest.raises(TaskError):
        mark_running(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            worker_id="w-r3-f4-1",
            fencing_token=lease.lease.fencing_token,
            evidence_ref="evidence:r3_f4_running",
            expected_revision=0,
        )
    session.commit()
    session.refresh(task)
    assert task.state == "dispatched"
    assert _attempt(session, task).state == "dispatched"


def test_record_attempt_result_rejection_leaves_no_result_fact(
    session: Session, world: TaskWorld
) -> None:
    task, _lease = _dispatched(
        session, world, key="idem_r3_f4_result", worker="w-r3-f4-2"
    )
    with pytest.raises(TaskError) as excinfo:
        # ``completed`` is only a running edge (T36); the fact must not land.
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="completed",
            result_ref="result_r3_f4_result",
            evidence_refs=[EVIDENCE_REF],
        )
    assert excinfo.value.reason_code == "task_invalid_transition"
    session.commit()
    session.refresh(task)
    assert task.state == "dispatched"
    result = task.result or {}
    assert result.get("outcome") != "completed"
    assert result.get("result_ref") != "result_r3_f4_result"


def test_complete_task_rejection_leaves_no_verification_result(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_r3_f4_complete")
    session.commit()
    move_to_verifying(session, world, task)
    session.flush()
    before = dict(task.result)

    with pytest.raises(TaskError):
        complete_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            result_ref="result_r3_f4_complete",
            evidence_refs=[EVIDENCE_REF],
            evidence=[evidence()],
            expected_revision=0,
        )
    session.commit()
    session.refresh(task)
    assert task.state == "verifying"
    assert task.result["verification"]["result"] == before["verification"]["result"]
    assert task.result.get("all_criteria_passed") in (None, False)


def test_evidence_unavailable_rejection_leaves_no_evidence_fact(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_r3_f4_evidence")
    session.commit()
    move_to_verifying(session, world, task)
    session.flush()

    with pytest.raises(TaskError):
        evidence_unavailable(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            evidence_refs=["evidence:r3_f4_unavailable"],
            expected_revision=0,
        )
    session.commit()
    session.refresh(task)
    assert task.state == "verifying"
    assert "evidence_unavailable" not in task.result


def test_deny_task_rejection_leaves_no_denial_fact(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_r3_f4_deny")
    session.commit()
    assert task.state == "queued"

    with pytest.raises(TaskError):
        deny_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            reason_code="policy_changed",
            expected_revision=0,
        )
    session.commit()
    session.refresh(task)
    assert task.state == "queued"
    assert task.stop_confirmed is False
    assert "stop_confirmed" not in (task.result or {})
    assert _active_grants(session, task.task_id) == 1


def test_escalate_owner_account_rejection_leaves_no_escalation(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_r3_f4_owner")
    session.commit()
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="timeout",
        evidence_refs=["evidence:r3_f4_timeout"],
    )
    session.commit()
    assert task.state == "unknown"

    with pytest.raises(TaskError) as excinfo:
        escalate_owner_account(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            status="disabled",
            reason_code="accountable_owner_inactive",
        )
    assert excinfo.value.reason_code == "task_invalid_transition"
    session.commit()
    session.refresh(task)
    assert task.state == "unknown"
    assert task.owner_escalation is None
    assert task.owner_account_status == "active"


# ---------------------------------------------------------------------------
# F5 -- refused retries restore the row; denial facts stay honest
# ---------------------------------------------------------------------------


def test_refused_retry_restores_the_narrowed_budget_fields(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r3_f5_budget")
    # Simulate a legacy row whose persisted runtime cap is coarser than the
    # policy ceiling; the retry clamp would narrow it to 300.
    task.max_runtime_seconds = 500
    task.owner_account_status = "disabled"
    task.owner_escalation = {
        "reason_code": "accountable_owner_inactive",
        "owner_principal_id": task.owner_principal_id,
        "requires_human": True,
    }
    session.flush()
    before = (task.max_calls, task.max_runtime_seconds, task.max_cost_units)

    with pytest.raises(TaskError) as excinfo:
        retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    assert excinfo.value.reason_code == "task_owner_inactive"
    session.commit()
    session.refresh(task)
    assert task.state == "failed"
    # The clamp (which would narrow 500 -> 300) never reached the row.
    assert (task.max_calls, task.max_runtime_seconds, task.max_cost_units) == before
    assert task.max_runtime_seconds == 500
    assert _active_grants(session, task.task_id) == 0


def test_deny_task_does_not_fabricate_side_effect_reconciliation(
    session: Session, world: TaskWorld
) -> None:
    task = _requeued(session, world, key="idem_r3_f5_deny")
    # Strip the reconciliation facts a retry would otherwise carry.
    task.result = {
        key: value
        for key, value in dict(task.result or {}).items()
        if key not in {"side_effects_reconciled", "no_side_effects"}
    }
    session.flush()

    deny_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="policy_changed",
    )
    session.commit()
    assert task.state == "denied"
    assert task.result["side_effects_reconciled"] is False
    assert task.result["no_side_effects"] is False
    # A denial is a confirmed stop of the task's authority (DB requirement).
    assert task.stop_confirmed is True


def test_deny_task_honours_explicit_reconciliation_evidence(
    session: Session, world: TaskWorld
) -> None:
    task = _requeued(session, world, key="idem_r3_f5_deny_explicit")
    task.result = {
        key: value
        for key, value in dict(task.result or {}).items()
        if key not in {"side_effects_reconciled", "no_side_effects"}
    }
    session.flush()

    deny_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="policy_changed",
        side_effects_reconciled=True,
    )
    session.commit()
    assert task.result["side_effects_reconciled"] is True


def test_sla_requeued_deadline_is_labelled_needs_human(
    session: Session, world: TaskWorld
) -> None:
    task = _requeued(session, world, key="idem_r3_f5_sla")
    task.due_at = utcnow() - timedelta(minutes=1)
    session.flush()

    results = worker.escalate_overdue_tasks(session, now=utcnow())
    session.commit()
    assert len(results) == 1
    # A requeued task (attempt >= 1) has no attempt=0 T27 edge: it escalates.
    assert results[0].action == "needs_human"
    assert results[0].state == "needs_human"
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"


# ---------------------------------------------------------------------------
# F6 -- the PEP must not consume a grant for a queued task
# ---------------------------------------------------------------------------


def test_pep_refuses_a_queued_task_without_consuming_the_grant(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world, key="idem_r3_f6_queued_pep")
    session.commit()
    confirmation = pending_confirmation(session, task)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    record_approval(
        session, task_id=task.task_id, actor=world.actors.approver, decision="approve"
    )
    session.commit()
    assert task.state == "queued"
    grant_id = task.grant_id

    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability=GATED_CAPABILITY,
            call_id="call_r3_queued_pep",
        )
    assert excinfo.value.reason_code == "task_invalid_transition"
    session.commit()

    # Nothing was consumed: the single-use grant and the confirmation survive.
    assert get_grant(session, grant_id).status == "active"
    session.refresh(confirmation)
    assert confirmation.consumed_at is None

    # ... and the task is still dispatchable (no T23 dead end).
    lease = claim(session, world, worker_id="worker-r3-f6")
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-r3-f6",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_r3_f6_0001",
    )
    session.commit()
    assert task.state == "dispatched"


# ---------------------------------------------------------------------------
# F7 -- a queue-guard failure must leave a recoverable task
# ---------------------------------------------------------------------------


def test_queue_guard_failure_escalates_instead_of_stranding(
    session: Session, world: TaskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = service._perform_transition

    def failing(*args, **kwargs):
        if kwargs.get("event") == "task.queued":
            # Same shape a real guard rejection has, raised *after* the grant
            # was issued.
            raise TaskError(
                "task_owner_inactive",
                "injected queue-guard rejection",
                context={"state": "evaluated"},
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "_perform_transition", failing)
    with pytest.raises(TaskError) as excinfo:
        submit_read(session, world, key="idem_r3_f7_queue_guard")
    assert excinfo.value.reason_code == "task_owner_inactive"
    monkeypatch.setattr(service, "_perform_transition", real)
    session.commit()

    task = session.scalar(
        select(Task).where(Task.idempotency_key == "idem_r3_f7_queue_guard")
    )
    assert task is not None
    session.refresh(task)
    # An ``evaluated`` task has no worker path; the failure is recoverable.
    assert task.state == "needs_human"
    assert task.needs_human_origin == "evaluated"
    assert task.grant_status == "revoked"
    assert _active_grants(session, task.task_id) == 0


# ---------------------------------------------------------------------------
# F5a -- the retry grant/budget narrowing never survives a refused requeue
# ---------------------------------------------------------------------------


def test_t65_refusal_restores_the_bound_grant_snapshot(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_r3_f5a_restore")
    original_grant = task.grant_id
    original_decision = task.decision_id
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
    session.refresh(task)

    assert task.state == "failed"
    assert task.grant_id == original_grant
    assert task.grant_status == "revoked"
    assert task.decision_id == original_decision
    assert task.decision_purpose == "initial_execution"
    assert _active_grants(session, task.task_id) == 0
