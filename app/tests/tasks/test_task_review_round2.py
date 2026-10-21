"""Regression coverage for the round-2 review of PR #89.

The scenarios here mirror the round-2 blocking/important items: validation
before mutation, honest reconciliation facts, task budgets, orphan grants,
requeued pre-dispatch failures, lease/fencing on every managed fact writer,
owner-only partial acceptance, escalation exit paths, confirmation authority,
channel binding and SLA ordering.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.governance.grants import get_grant
from app.governance.models import ExecutionGrant
from app.governance.principal import (
    resolve_employee_principal,
    resolve_service_principal,
)
from app.tasks import worker
from app.tasks.errors import TaskError
from app.tasks.models import Task, TaskAttempt
from app.tasks.service import (
    _close_attempt,
    assign_task,
    complete_task,
    confirm_stop,
    consume_confirmation,
    create_confirmation,
    dispatch_deadline_exceeded,
    reconcile_task,
    record_attempt_result,
    record_dispatch_failure,
    recheck_execution,
    resolve_owner_escalation,
    retry_task,
    satisfy_confirmation,
    start_attempt,
    takeover_task,
)
from app.tests.governance.helpers import (
    DEFAULT_RULES,
    make_service_identity,
    publish_policy,
)
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    EVIDENCE_REF,
    READ_CAPABILITY,
    VERIFIER_ID,
    VERIFIER_VERSION,
    claim,
    dispatch_and_run,
    evidence,
    goal,
    move_to_verifying,
    pending_confirmation,
    submit_gated,
    submit_read,
    utcnow,
)

SECOND_CRITERION = "crit_round2_second"
SECOND_EVIDENCE = "evidence:round2_second"


def _two_criteria_goal() -> dict:
    value = goal()
    value["completion_criteria"].append(
        {
            "criterion_id": SECOND_CRITERION,
            "type": "business_state_equals",
            "verifier": {"verifier_id": VERIFIER_ID, "version": VERIFIER_VERSION},
            "resource_ref": "res_expense_round2",
            "expected": "submitted",
            "evidence_ref": SECOND_EVIDENCE,
            "evidence_hash": None,
        }
    )
    return value


def _failed_attempt(
    session: Session,
    world: TaskWorld,
    *,
    key: str = "idem_round2_failed",
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
        evidence_refs=["evidence:round2_failed"],
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


def _managed_task(session: Session, world: TaskWorld, *, key: str):
    identity = make_service_identity(session, owner=world.org.admin)
    service = resolve_service_principal(session, identity.id)
    task = submit_read(
        session,
        world,
        key=key,
        executor=service,
        requested_capabilities=["managed.task@1.0.0"],
        execution_mode="managed_execution",
        runtime_capabilities_verified=True,
    )
    session.commit()
    return task, service


# ---------------------------------------------------------------------------
# 1 -- confirm_stop validates before mutating and never defaults reconciled
# ---------------------------------------------------------------------------


def _failed_by_verification(session: Session, world: TaskWorld):
    task = submit_read(
        session, world, key="idem_round2_verify_failed", goal=_two_criteria_goal()
    )
    session.commit()
    move_to_verifying(session, world, task)
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref="result_round2_verify",
        evidence_refs=[EVIDENCE_REF, SECOND_EVIDENCE],
        evidence=[
            evidence(),
            evidence(criterion_id=SECOND_CRITERION, observed="draft"),
        ],
        allow_partial=True,
        owner_accepted_partial=False,
    )
    session.commit()
    assert task.state == "failed"
    assert task.stop_confirmed is False
    return task


def test_confirm_stop_rejects_a_failed_task_without_partial_writes(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_by_verification(session, world)
    result_before = dict(task.result)
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    ended_before = attempt.ended_at

    with pytest.raises(TaskError) as excinfo:
        confirm_stop(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            evidence_refs=["evidence:round2_stop_failed"],
            side_effects_reconciled=True,
        )
    assert excinfo.value.reason_code == "task_invalid_transition"
    session.commit()
    session.refresh(task)
    session.refresh(attempt)

    assert task.state == "failed"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    assert task.result == result_before
    assert attempt.state == "failed"
    assert attempt.ended_at == ended_before


def test_confirm_stop_in_an_inapplicable_state_commits_no_mutation(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world)
    session.commit()
    assert task.grant_id is None
    with pytest.raises(TaskError) as excinfo:
        confirm_stop(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            evidence_refs=["evidence:round2_stop_invalid_state"],
            side_effects_reconciled=True,
        )
    assert excinfo.value.reason_code == "task_invalid_transition"
    session.commit()
    session.refresh(task)

    assert task.state == "awaiting_confirmation"
    assert task.stop_confirmed is False
    assert "side_effects_reconciled" not in task.result
    assert task.grant_id is None
    assert (
        session.scalar(
            select(func.count())
            .select_from(TaskAttempt)
            .where(TaskAttempt.task_id == task.task_id)
        )
        == 0
    )


def test_confirm_stop_requires_an_explicit_reconciliation_fact(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_round2_stop_running")
    session.commit()
    dispatch_and_run(session, world, task)
    session.commit()

    with pytest.raises(TaskError) as excinfo:
        confirm_stop(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            evidence_refs=["evidence:round2_stop_running"],
        )
    assert excinfo.value.reason_code == "task_reconcile_evidence_missing"
    assert task.state == "running"
    assert task.stop_confirmed is False

    confirm_stop(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        evidence_refs=["evidence:round2_stop_running"],
        side_effects_reconciled=True,
    )
    session.commit()
    assert task.state == "cancelled"
    assert task.result["side_effects_reconciled"] is True


# ---------------------------------------------------------------------------
# 2 -- T61 derives reconciliation, never hardcodes it
# ---------------------------------------------------------------------------


def test_redispatch_refuses_an_unreconciled_attempt(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_round2_t61_unreconciled")
    session.commit()
    dispatch_and_run(session, world, task)
    from app.tasks.service import escalate_task

    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.commit()
    assert task.state == "needs_human"
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.ended_at is None
    grants_before = _active_grants(session, task.task_id)

    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="redispatch",
            evidence_refs=["evidence:round2_t61_stop"],
        )
    assert excinfo.value.reason_code == "task_reconcile_evidence_missing"
    session.commit()
    session.refresh(task)
    session.refresh(attempt)
    assert task.state == "needs_human"
    assert attempt.ended_at is None
    # No new authority was minted for the refused redispatch.
    assert _active_grants(session, task.task_id) == grants_before

    takeover_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action="redispatch",
        evidence_refs=["evidence:round2_t61_stop"],
        side_effects_reconciled=True,
    )
    session.commit()
    session.refresh(attempt)
    assert task.state == "queued"
    assert attempt.ended_at is not None


# ---------------------------------------------------------------------------
# 3 -- task budgets: representable multi-call, inherited caps, cost
# ---------------------------------------------------------------------------


def test_multi_call_task_cap_is_representable(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_round2_calls", max_calls=3)
    session.commit()
    assert task.state == "queued"
    assert task.max_calls == 3
    grant = get_grant(session, task.grant_id)
    assert grant.budget["max_calls"] == 3
    assert grant.consumption_mode == "multi_use"

    lease = claim(session, world, worker_id="worker-round2-calls")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-round2-calls",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_round2_calls_0001",
    )
    session.flush()
    for call in range(1, 4):
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability=READ_CAPABILITY,
            call_id=f"call_round2_multi_{call}",
        )
        session.flush()
    with pytest.raises(TaskError):
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability=READ_CAPABILITY,
            call_id="call_round2_multi_4",
        )


def test_retry_grant_inherits_a_runtime_cap_below_policy(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(
        session, world, key="idem_round2_runtime", max_runtime_seconds=120
    )
    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    grant = get_grant(session, task.grant_id)
    assert grant.budget["max_runtime_seconds"] == 120
    assert task.max_runtime_seconds == 120

    # The dispatch budget check must not reject the retry grant (120 <= 120).
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-1",
        fencing_token=1,
        execution_ref="exec_round2_runtime_0002",
    )
    session.flush()
    assert task.state == "dispatched"
    assert task.attempt == 2


def test_retry_grant_inherits_calls_and_cost_caps(
    session: Session, world: TaskWorld
) -> None:
    rules = [dict(rule) for rule in DEFAULT_RULES]
    for rule in rules:
        if rule["rule_id"] == "read_self":
            rule["max_cost_units"] = 2.5
    policy = publish_policy(session, world.org, rules=rules)
    task = _failed_attempt(
        session,
        world,
        key="idem_round2_cost",
        policy_set_id=policy.policy_set_id,
        max_calls=2,
        max_runtime_seconds=120,
        max_cost_units=2.0,
    )
    grant = get_grant(session, task.grant_id)
    assert grant.budget["max_calls"] == 2
    assert grant.budget["max_runtime_seconds"] == 120
    assert grant.budget["max_cost_units"] == 2.0
    assert grant.consumption_mode == "multi_use"

    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    retry_grant = get_grant(session, task.grant_id)
    assert retry_grant.grant_id != grant.grant_id
    assert retry_grant.budget["max_calls"] == 2
    assert retry_grant.budget["max_runtime_seconds"] == 120
    assert retry_grant.budget["max_cost_units"] == 2.0
    assert task.max_calls == 2


def test_task_caps_above_policy_are_clamped_at_submit(
    session: Session, world: TaskWorld
) -> None:
    # The default read rule authorises no cost and at most 5 calls / 300s.
    task = submit_read(
        session,
        world,
        key="idem_round2_clamp",
        max_calls=9,
        max_runtime_seconds=600,
        max_cost_units=5.0,
    )
    session.commit()
    assert task.state == "queued"
    assert task.max_calls == 5
    assert task.max_runtime_seconds == 300
    assert task.max_cost_units == 0.0
    grant = get_grant(session, task.grant_id)
    assert grant.budget["max_calls"] == 5
    assert grant.budget["max_runtime_seconds"] == 300
    assert grant.budget["max_cost_units"] is None


# ---------------------------------------------------------------------------
# 4 -- a rejected transition must not leave an active orphan grant
# ---------------------------------------------------------------------------


def test_t65_guard_failure_revokes_the_new_grant(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, key="idem_round2_t65_owner")
    task.owner_account_status = "disabled"
    task.owner_escalation = {
        "reason_code": "accountable_owner_inactive",
        "owner_principal_id": task.owner_principal_id,
        "requires_human": True,
    }
    session.flush()
    assert _active_grants(session, task.task_id) == 0

    with pytest.raises(TaskError) as excinfo:
        retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    assert excinfo.value.reason_code == "task_owner_inactive"
    session.flush()
    assert task.state == "failed"
    assert _active_grants(session, task.task_id) == 0


def test_t61_guard_failure_revokes_the_new_grant(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(
        session, world, key="idem_round2_t61_budget", retry_budget=0
    )
    session.commit()
    dispatch_and_run(session, world, task)
    from app.tasks.service import escalate_task

    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.commit()
    original_grant = task.grant_id

    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="redispatch",
            evidence_refs=["evidence:round2_t61_budget_stop"],
            side_effects_reconciled=True,
        )
    assert excinfo.value.reason_code == "retry_budget_exhausted"
    session.flush()
    assert task.state == "needs_human"
    active = list(
        session.scalars(
            select(ExecutionGrant.grant_id).where(
                ExecutionGrant.task_id == task.task_id,
                ExecutionGrant.status == "active",
            )
        )
    )
    assert active == [original_grant]


def test_t71_guard_failure_issues_no_grant(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_round2_t71_owner")
    session.commit()
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="timeout",
        evidence_refs=["evidence:round2_t71_timeout"],
    )
    session.commit()
    assert task.state == "unknown"
    task.owner_account_status = "disabled"
    task.owner_escalation = {
        "reason_code": "accountable_owner_inactive",
        "requires_human": True,
    }
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        reconcile_task(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="not_found",
            no_side_effect_evidence=True,
            evidence_refs=["evidence:round2_t71_never"],
        )
    assert excinfo.value.reason_code == "task_owner_inactive"
    session.flush()
    assert task.state == "unknown"
    assert _active_grants(session, task.task_id) == 0


# ---------------------------------------------------------------------------
# 5 -- requeued (attempt >= 1) pre-dispatch failures stay honest
# ---------------------------------------------------------------------------


def test_requeued_timeout_enters_unknown(session: Session, world: TaskWorld) -> None:
    task = _requeued(session, world, key="idem_round2_requeue_timeout")
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="timeout",
        evidence_refs=["evidence:round2_requeue_timeout"],
    )
    session.commit()
    assert task.state == "unknown"
    assert task.stop_confirmed is False
    assert task.result["stop_confirmed"] is False
    assert task.result["stop_evidence_refs"] == []


def test_requeued_rejection_escalates_and_stays_dispatchable(
    session: Session, world: TaskWorld
) -> None:
    task = _requeued(session, world, key="idem_round2_requeue_rejected")
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="rejected",
        evidence_refs=["evidence:round2_requeue_rejected"],
        reason="submission_rejected",
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"
    assert task.stop_confirmed is False
    assert task.result["side_effects_reconciled"] is True
    assert _active_grants(session, task.task_id) == 0

    # The human path (T61) is still reachable; the task is not stranded.
    takeover_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action="redispatch",
        evidence_refs=["evidence:round2_requeue_rejected_stop"],
        side_effects_reconciled=True,
    )
    session.commit()
    assert task.state == "queued"


def test_requeued_unsupported_escalates(session: Session, world: TaskWorld) -> None:
    task = _requeued(session, world, key="idem_round2_requeue_unsupported")
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="unsupported",
        evidence_refs=["evidence:round2_requeue_unsupported"],
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"
    assert task.stop_confirmed is False


def test_requeued_deadline_escalates_instead_of_t27(
    session: Session, world: TaskWorld
) -> None:
    task = _requeued(session, world, key="idem_round2_requeue_deadline")
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="deadline_exceeded",
        evidence_refs=["evidence:round2_requeue_deadline"],
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.stop_confirmed is False
    assert task.result["stop_confirmed"] is False


# ---------------------------------------------------------------------------
# 6 -- managed lease/fencing on every fact writer and before the PEP call
# ---------------------------------------------------------------------------


def test_recheck_refuses_missing_stale_and_expired_managed_leases(
    session: Session, world: TaskWorld
) -> None:
    task, service = _managed_task(session, world, key="idem_round2_lease_neg")
    lease = claim(session, world, worker_id="worker-round2-lease")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=service,
        worker_id="worker-round2-lease",
        fencing_token=lease.lease.fencing_token,
        runtime_ref="rtask_round2_lease_0001",
    )
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=service,
            capability="managed.task@1.0.0",
            call_id="call_round2_lease_missing",
        )
    assert excinfo.value.reason_code == "task_lease_not_held"

    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=service,
            capability="managed.task@1.0.0",
            call_id="call_round2_lease_stale",
            worker_id="worker-round2-lease",
            fencing_token=lease.lease.fencing_token + 5,
        )
    assert excinfo.value.reason_code == "task_fencing_token_stale"

    # Keep the row-valid invariant expires_at > acquired_at while making the
    # lease expired relative to the check time.
    lease.lease.acquired_at = utcnow() - timedelta(minutes=10)
    lease.lease.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=service,
            capability="managed.task@1.0.0",
            call_id="call_round2_lease_expired",
            worker_id="worker-round2-lease",
            fencing_token=lease.lease.fencing_token,
        )
    assert excinfo.value.reason_code == "task_lease_expired"


def test_recheck_accepts_the_current_managed_lease(
    session: Session, world: TaskWorld
) -> None:
    task, service = _managed_task(session, world, key="idem_round2_lease_pos")
    lease = claim(session, world, worker_id="worker-round2-positive")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=service,
        worker_id="worker-round2-positive",
        fencing_token=lease.lease.fencing_token,
        runtime_ref="rtask_round2_lease_0002",
    )
    session.flush()

    result = recheck_execution(
        session,
        task_id=task.task_id,
        actor=service,
        capability="managed.task@1.0.0",
        call_id="call_round2_lease_ok",
        worker_id="worker-round2-positive",
        fencing_token=lease.lease.fencing_token,
    )
    session.flush()
    assert result.task_id == task.task_id
    assert result.capability == "managed.task@1.0.0"


def test_stale_worker_cannot_write_a_managed_fact_after_recovery(
    session: Session, world: TaskWorld
) -> None:
    task, service = _managed_task(session, world, key="idem_round2_stale_fact")
    lease = claim(session, world, worker_id="worker-round2-stale")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=service,
        worker_id="worker-round2-stale",
        fencing_token=lease.lease.fencing_token,
        runtime_ref="rtask_round2_stale_0001",
    )
    session.commit()

    worker.recover_expired_leases(
        session, now=utcnow() + timedelta(hours=1)
    )
    session.commit()
    assert task.state == "unknown"
    assert lease.lease.released_at is not None

    with pytest.raises(TaskError) as excinfo:
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=service,
            outcome="completed",
            result_ref="result_round2_stale",
            evidence_refs=[EVIDENCE_REF],
            worker_id="worker-round2-stale",
            fencing_token=lease.lease.fencing_token,
        )
    assert excinfo.value.reason_code in {
        "task_lease_not_held",
        "task_lease_expired",
        "grant_revoked",
    }


# ---------------------------------------------------------------------------
# 7 -- only the accountable owner may accept a partial result
# ---------------------------------------------------------------------------


def test_only_the_owner_can_accept_a_partial_result(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(
        session, world, key="idem_round2_partial", goal=_two_criteria_goal()
    )
    session.commit()
    move_to_verifying(session, world, task)
    session.flush()
    common = dict(
        result_ref="result_round2_partial",
        evidence_refs=[EVIDENCE_REF, SECOND_EVIDENCE],
        evidence=[
            evidence(),
            evidence(criterion_id=SECOND_CRITERION, observed="draft"),
        ],
        allow_partial=True,
        owner_accepted_partial=True,
    )
    with pytest.raises(TaskError) as excinfo:
        complete_task(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            **common,
        )
    assert excinfo.value.reason_code == "task_unauthorized_actor"
    assert task.state == "verifying"

    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        **common,
    )
    session.commit()
    assert task.state == "partial_success"
    assert task.result["owner_accepted_partial"] is True


# ---------------------------------------------------------------------------
# 8 -- owner-disabled escalation has an exit path
# ---------------------------------------------------------------------------


def test_owner_escalation_can_be_resolved_by_an_active_owner(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_round2_owner_escalation")
    session.commit()
    world.org.manager.status = "inactive"
    session.flush()

    assert (
        worker.claim_task(
            session, worker_id="worker-round2-owner", actor=world.actors.executor
        )
        is None
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.owner_escalation is not None

    new_owner = resolve_employee_principal(
        session, employee_id=world.org.admin.id
    )
    resolve_owner_escalation(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        owner=new_owner,
    )
    session.commit()
    session.refresh(task)
    assert task.state == "queued"
    assert task.owner_principal_id == new_owner.principal_id
    assert task.owner_account_status == "active"
    assert task.owner_escalation is None
    assert _active_grants(session, task.task_id) == 1

    claimed = worker.claim_task(
        session, worker_id="worker-round2-owner-2", actor=world.actors.executor
    )
    assert claimed is not None
    assert claimed.task.task_id == task.task_id


# ---------------------------------------------------------------------------
# 9/10/13/14 -- confirmation authority, channel binding, assignment
# ---------------------------------------------------------------------------


def test_outsider_cannot_create_or_consume_a_confirmation(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world, key="idem_round2_conf_authority")
    session.commit()
    with pytest.raises(TaskError) as excinfo:
        create_confirmation(
            session,
            task_id=task.task_id,
            actor=world.actors.outsider,
            channel_binding={"channel": "mcp"},
        )
    assert excinfo.value.reason_code == "task_unauthorized_actor"

    from app.tasks.service import confirm_task

    confirmation = pending_confirmation(session, task)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        consume_confirmation(
            session,
            task_id=task.task_id,
            actor=world.actors.outsider,
            confirmation_id=confirmation.confirmation_id,
            call_id="call_round2_conf_outside",
        )
    assert excinfo.value.reason_code == "task_unauthorized_actor"


def test_omitting_the_channel_does_not_bypass_binding(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(
        session,
        world,
        key="idem_round2_channel",
        sla={
            "confirmation_channel": "mcp",
            "response_by": utcnow() + timedelta(hours=1),
        },
    )
    session.commit()
    assert task.sla["confirmation_channel"] == "mcp"

    row = create_confirmation(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        channel_binding={"channel": "mcp"},
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        satisfy_confirmation(
            session,
            task_id=task.task_id,
            actor=world.actors.requester,
            confirmation_id=row.confirmation_id,
        )
    assert excinfo.value.reason_code == "task_confirmation_channel_mismatch"

    with pytest.raises(TaskError) as excinfo:
        satisfy_confirmation(
            session,
            task_id=task.task_id,
            actor=world.actors.requester,
            confirmation_id=row.confirmation_id,
            channel="web",
        )
    assert excinfo.value.reason_code == "task_confirmation_channel_mismatch"

    satisfied = satisfy_confirmation(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=row.confirmation_id,
        channel="mcp",
    )
    assert satisfied.status == "satisfied"


def test_assignment_to_an_inactive_owner_is_a_deterministic_rejection(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world, key="idem_round2_assign_inactive")
    session.commit()
    # Resolve while the directory row is still active: the point of the test is
    # the assignment decision, not principal resolution.
    inactive = resolve_employee_principal(
        session, employee_id=world.org.finance.id
    )
    world.org.finance.status = "inactive"
    session.flush()
    owner_before = task.owner_principal_id

    with pytest.raises(TaskError) as excinfo:
        assign_task(
            session,
            task_id=task.task_id,
            actor=world.actors.requester,
            owner=inactive,
        )
    assert excinfo.value.reason_code == "task_owner_inactive"
    session.commit()
    session.refresh(task)
    assert task.owner_principal_id == owner_before
    assert task.state == "awaiting_confirmation"


# ---------------------------------------------------------------------------
# 11 -- T34 needs verified no-side-effect evidence from the boundary
# ---------------------------------------------------------------------------


def test_dispatch_deadline_requires_boundary_evidence(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, key="idem_round2_t34")
    session.commit()
    claim(session, world, worker_id="worker-round2-t34")
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-round2-t34",
        fencing_token=1,
        execution_ref="exec_round2_t34_0001",
    )
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        dispatch_deadline_exceeded(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            evidence_refs=["evidence:round2_t34"],
        )
    assert excinfo.value.reason_code == "task_reconcile_evidence_missing"

    # A non-producer participant cannot assert "no side effects" either.
    with pytest.raises(TaskError) as excinfo:
        dispatch_deadline_exceeded(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            evidence_refs=["evidence:round2_t34"],
            no_side_effects_evidence=True,
        )
    assert excinfo.value.reason_code == "task_unauthorized_actor"
    assert task.state == "dispatched"

    dispatch_deadline_exceeded(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        evidence_refs=["evidence:round2_t34"],
        no_side_effects_evidence=True,
    )
    session.flush()
    assert task.state == "timed_out"
    assert task.result["no_side_effect_evidence"] is True


# ---------------------------------------------------------------------------
# 12/16 -- validation before mutation, closed attempts stay closed
# ---------------------------------------------------------------------------


def test_takeover_result_submission_validates_before_writing(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(
        session, world, key="idem_round2_result_validate", goal=_two_criteria_goal()
    )
    session.commit()
    move_to_verifying(session, world, task)
    # One criterion has no evidence: T45 sends the task to a human with a
    # persisted "ended pending verification" marker.
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref="result_round2_inconclusive",
        evidence_refs=[EVIDENCE_REF],
        evidence=[evidence()],
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "verifying"

    # Simulate lost verification evidence: without the persisted marker the
    # service must reject *before* writing the submitted result (otherwise the
    # write itself would make the check pass).
    task.result = {
        **dict(task.result),
        "outcome": "lost",
        "attempt_finished_pending_verification": False,
    }
    session.flush()
    result_before = dict(task.result)

    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="result_submitted",
            result_ref="result_round2_submitted",
            evidence_refs=["evidence:round2_result"],
        )
    assert excinfo.value.reason_code == "evidence_missing"
    session.flush()
    assert task.state == "needs_human"
    assert task.result == result_before


def test_close_attempt_never_overwrites_a_closed_attempt() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    closed = started + timedelta(seconds=5)
    attempt = TaskAttempt(
        task_id="task_round2_close",
        attempt_number=1,
        state="failed",
        started_at=started,
        ended_at=closed,
        stop_confirmed=True,
    )
    _close_attempt(
        attempt,
        state="cancelled",
        now=started + timedelta(minutes=1),
        stop_confirmed=True,
        evidence_refs=["evidence:round2_close"],
    )
    assert attempt.state == "failed"
    assert attempt.ended_at == closed
    assert not attempt.evidence_refs

    verifying = TaskAttempt(
        task_id="task_round2_close",
        attempt_number=2,
        state="verifying",
        started_at=started,
        ended_at=closed,
        stop_confirmed=False,
    )
    _close_attempt(
        verifying,
        state="succeeded",
        now=started + timedelta(minutes=1),
        stop_confirmed=False,
        result_ref="result_round2_close",
    )
    assert verifying.state == "succeeded"
    assert verifying.ended_at == closed


# ---------------------------------------------------------------------------
# 17 -- budget input validation and the now-based task runtime cap
# ---------------------------------------------------------------------------


def test_bool_max_calls_is_rejected(session: Session, world: TaskWorld) -> None:
    with pytest.raises(TaskError) as excinfo:
        submit_read(session, world, key="idem_round2_bool", max_calls=True)
    assert excinfo.value.reason_code == "required_input_missing"


def test_task_runtime_budget_uses_the_supplied_now(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(
        session, world, key="idem_round2_runtime_cap", max_runtime_seconds=1
    )
    session.commit()
    dispatch_and_run(session, world, task)
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability=READ_CAPABILITY,
            call_id="call_round2_runtime_cap",
            now=utcnow() + timedelta(seconds=30),
        )
    assert excinfo.value.reason_code == "budget_exceeded"


# ---------------------------------------------------------------------------
# 18 -- response_by-only rows are not starved by the SLA pass
# ---------------------------------------------------------------------------


def test_sla_pass_orders_response_only_rows_by_their_deadline(
    session: Session, world: TaskWorld
) -> None:
    with_due = submit_read(
        session,
        world,
        key="idem_round2_sla_due",
        goal=goal(summary="Synthetic SLA due task"),
    )
    with_due.due_at = utcnow() + timedelta(hours=2)
    response_only = submit_read(
        session,
        world,
        key="idem_round2_sla_response",
        goal=goal(summary="Synthetic SLA response-only task"),
        sla={"response_by": utcnow() - timedelta(minutes=5)},
    )
    assert response_only.due_at is None
    session.commit()

    results = worker.escalate_overdue_tasks(
        session, actor=world.actors.executor, now=utcnow(), limit=1
    )
    session.commit()
    assert len(results) == 1
    assert results[0].task_id == response_only.task_id
    assert results[0].state == "needs_human"


# ---------------------------------------------------------------------------
# 4 (cont.) -- no orphan grant on a refused queue guard
# ---------------------------------------------------------------------------


def test_refused_queue_guard_leaves_no_active_grant(
    session: Session, world: TaskWorld
) -> None:
    world.org.manager.status = "inactive"
    session.flush()
    with pytest.raises(TaskError):
        submit_read(session, world, key="idem_round2_queue_guard")
    session.commit()
    stored = session.scalar(select(Task).where(Task.state == "evaluated"))
    assert stored is not None
    # The queue guards are previewed before issuance, so no grant is created.
    assert stored.grant_id is None
    assert _active_grants(session, stored.task_id) == 0
