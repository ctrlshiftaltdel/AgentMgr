"""Regression coverage for the round-1 review of PR #89.

Every test exercises a real PostgreSQL transaction: no mock database, no fake
policy engine.  The scenarios mirror the review's blocking and important items
(C1-C10, Important 1-13).
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.governance.grants import get_grant
from app.governance.models import ExecutionGrant, GovernanceDecisionRecord
from app.governance.principal import resolve_employee_principal, resolve_service_principal
from app.tasks import worker
from app.tasks.errors import ConfirmationError, TaskError
from app.tasks.models import Task, TaskAttempt, TaskConfirmation
from app.tasks.service import (
    TaskEvidenceVerifier,
    assign_task,
    complete_task,
    confirm_stop,
    confirm_task,
    deny_task,
    dispatch_deadline_exceeded,
    escalate_owner_account,
    escalate_task,
    evidence_unavailable,
    provide_clarification,
    record_approval,
    record_attempt_result,
    record_dispatch_failure,
    recheck_execution,
    retry_task,
    revoke_approval,
    start_attempt,
    submit_task,
    takeover_task,
)
from app.tests.governance.helpers import make_service_identity, scope
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    CRITERION_ID,
    EVIDENCE_REF,
    GATED_CAPABILITY,
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


def _distinct_gated_world(session: Session, world: TaskWorld):
    """requester != owner != executor != approver, all real employees."""
    requester = resolve_employee_principal(
        session, employee_id=world.org.employee.id
    )
    owner = resolve_employee_principal(session, employee_id=world.org.admin.id)
    executor = resolve_employee_principal(session, employee_id=world.org.manager.id)
    approver = resolve_employee_principal(session, employee_id=world.org.finance.id)
    return requester, owner, executor, approver


def _failed_attempt(
    session: Session,
    world: TaskWorld,
    *,
    side_effects_reconciled: bool = True,
    retry_safe: bool = True,
):
    task = submit_read(session, world, retry_safe=retry_safe)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="failed",
        evidence_refs=["evidence:execution_failed_fix_0001"],
        stop_confirmed=True,
        side_effects_reconciled=side_effects_reconciled,
        error_code="runtime_error",
    )
    session.commit()
    assert task.state == "failed"
    return task


# ---------------------------------------------------------------------------
# C1 -- confirmation/PEP identity
# ---------------------------------------------------------------------------


def test_confirmation_from_requester_satisfies_a_different_executor(
    session: Session, world: TaskWorld
) -> None:
    requester, owner, executor, approver = _distinct_gated_world(session, world)
    task = submit_gated(
        session,
        world,
        requester=requester,
        owner=owner,
        executor=executor,
        approver=approver,
    )
    session.commit()
    assert task.state == "awaiting_confirmation"
    confirmation = pending_confirmation(session, task)

    confirm_task(
        session,
        task_id=task.task_id,
        actor=requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()

    # The PEP verifier is bound to this task and accepts the requester as the
    # confirmer even though the executing principal is somebody else.
    verifier = TaskEvidenceVerifier(session, task_id=task.task_id)
    assert verifier.verify_confirmation(
        task.confirmation_ref,
        principal_id=executor.principal_id,
        payload_hash=task.payload_hash,
        now=utcnow(),
    )

    record_approval(
        session,
        task_id=task.task_id,
        actor=approver,
        decision="approve",
    )
    session.commit()
    assert task.state == "queued"


def test_confirmation_from_another_task_is_refused(session: Session, world: TaskWorld) -> None:
    first = submit_gated(session, world, key="idem_fix_conf_first")
    second = submit_gated(
        session,
        world,
        key="idem_fix_conf_second",
        goal=goal(summary="Submit a second synthetic expense"),
    )
    session.commit()
    first_confirmation = pending_confirmation(session, first)
    verifier = TaskEvidenceVerifier(session, task_id=second.task_id)
    assert not verifier.verify_confirmation(
        {
            "confirmation_id": first_confirmation.confirmation_id,
            "payload_hash": first_confirmation.payload_hash,
        },
        principal_id=world.actors.executor.principal_id,
        payload_hash=second.payload_hash,
        now=utcnow(),
    )


# ---------------------------------------------------------------------------
# C2 -- a consumed grant may stay consumed in unknown
# ---------------------------------------------------------------------------


def test_consumed_single_use_grant_can_enter_unknown(
    session: Session, world: TaskWorld
) -> None:
    requester, owner, executor, approver = _distinct_gated_world(session, world)
    task = submit_gated(
        session,
        world,
        requester=requester,
        owner=owner,
        executor=executor,
        approver=approver,
    )
    confirm_task(
        session,
        task_id=task.task_id,
        actor=requester,
        confirmation_id=pending_confirmation(session, task).confirmation_id,
    )
    record_approval(session, task_id=task.task_id, actor=approver, decision="approve")
    session.commit()

    lease = worker.claim_task(session, worker_id="worker-c2", actor=executor)
    assert lease is not None
    start_attempt(
        session,
        task_id=task.task_id,
        actor=executor,
        worker_id="worker-c2",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_c2_0001",
    )
    from app.tasks.service import mark_running

    mark_running(
        session,
        task_id=task.task_id,
        actor=executor,
        worker_id="worker-c2",
        fencing_token=lease.lease.fencing_token,
        evidence_ref="evidence:c2_running",
    )
    session.commit()

    recheck_execution(
        session,
        task_id=task.task_id,
        actor=executor,
        capability=GATED_CAPABILITY,
        call_id="call_c2_0001",
    )
    session.flush()
    grant = get_grant(session, task.grant_id)
    assert grant.status == "consumed"

    # No IntegrityError: the unknown-state check accepts consumed.
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=executor,
        outcome="lost",
        evidence_refs=["evidence:c2_lost"],
    )
    session.commit()
    session.refresh(task)
    assert task.state == "unknown"
    assert task.grant_status == "consumed"
    assert get_grant(session, task.grant_id).status == "consumed"


# ---------------------------------------------------------------------------
# C3 -- the final authorised attempt is dispatchable
# ---------------------------------------------------------------------------


def test_third_of_three_attempts_can_dispatch(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world, max_attempts=3)
    session.commit()
    lease = claim(session, world, worker_id="worker-c3")
    session.flush()

    for attempt in (1, 2, 3):
        start_attempt(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            worker_id="worker-c3",
            fencing_token=lease.lease.fencing_token,
            execution_ref=f"exec_c3_{attempt:04d}",
        )
        from app.tasks.service import mark_running

        mark_running(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            worker_id="worker-c3",
            fencing_token=lease.lease.fencing_token,
            evidence_ref=f"evidence:c3_running_{attempt}",
        )
        session.flush()
        assert task.attempt == attempt
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="failed",
            evidence_refs=[f"evidence:c3_failed_{attempt}"],
            stop_confirmed=True,
            side_effects_reconciled=True,
            error_code="runtime_error",
        )
        session.flush()
        if attempt < 3:
            retry_task(session, task_id=task.task_id, actor=world.actors.owner)
            session.flush()
    session.commit()
    assert task.attempt == 3
    assert task.state == "failed"


# ---------------------------------------------------------------------------
# C4 -- confirm_stop persists and is authorised
# ---------------------------------------------------------------------------


def test_confirm_stop_from_unknown_persists_and_closes(
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
        evidence_refs=["evidence:c4_lost"],
    )
    session.commit()
    assert task.state == "unknown"

    confirm_stop(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        evidence_refs=["evidence:c4_stopped"],
    )
    session.commit()
    session.refresh(task)
    assert task.state == "cancelled"
    assert task.stop_confirmed is True
    assert task.result["stop_evidence_refs"] == ["evidence:c4_stopped"]
    # No explicit reconciliation: the persisted fact must stay honest, not True.
    assert task.result["side_effects_reconciled"] is False


def test_confirm_stop_from_needs_human_persists(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.commit()
    assert task.state == "needs_human"

    confirm_stop(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        evidence_refs=["evidence:c4_human_stopped"],
    )
    session.commit()
    session.refresh(task)
    assert task.state == "cancelled"
    assert task.stop_confirmed is True
    # needs_human -> cancelled (T63) does not need reconciliation, and the
    # absent fact is persisted honestly rather than defaulted to True.
    assert task.result["side_effects_reconciled"] is False


def test_outsider_cannot_confirm_a_stop(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    with pytest.raises(TaskError) as excinfo:
        confirm_stop(
            session,
            task_id=task.task_id,
            actor=world.actors.outsider,
            evidence_refs=["evidence:c4_outsider"],
        )
    assert excinfo.value.reason_code == "task_unauthorized_actor"


# ---------------------------------------------------------------------------
# C5 -- redispatch closes the open attempt and honestly enforces max_attempts
# ---------------------------------------------------------------------------


def test_redispatch_closes_attempt_then_dispatches(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    lease = claim(session, world, worker_id="worker-c5")
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-c5",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_c5_0001",
    )
    session.flush()
    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "dispatched"
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt.ended_at is None

    takeover_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action="redispatch",
        evidence_refs=["evidence:c5_stopped"],
        side_effects_reconciled=True,
    )
    session.commit()
    session.refresh(attempt)
    assert attempt.ended_at is not None
    assert attempt.stop_confirmed is True
    assert task.state == "queued"
    assert task.attempt == 1
    assert task.retry_budget_remaining == 2

    # The same lease is still valid and the next attempt can open.
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-c5",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_c5_0002",
    )
    session.commit()
    assert task.state == "dispatched"
    assert task.attempt == 2


def test_redispatch_refuses_when_max_attempts_is_reached(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, max_attempts=1)
    session.commit()
    dispatch_and_run(session, world, task)
    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="redispatch",
            evidence_refs=["evidence:c5_max_stopped"],
        )
    assert excinfo.value.reason_code == "retry_budget_exhausted"


def test_authorization_revocation_and_declared_failure_close_attempts(
    session: Session, world: TaskWorld
) -> None:
    denied = submit_read(
        session,
        world,
        key="idem_c5_t62",
        goal=goal(summary="Revoked synthetic read"),
    )
    session.commit()
    dispatch_and_run(session, world, denied)
    escalate_task(
        session,
        task_id=denied.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.flush()
    takeover_task(
        session,
        task_id=denied.task_id,
        actor=world.actors.owner,
        action="authorization_revoked",
        evidence_refs=["evidence:c5_t62_stop"],
    )
    session.commit()
    assert denied.state == "denied"
    assert denied.stop_confirmed is True
    denied_attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == denied.task_id)
    )
    assert denied_attempt.ended_at is not None
    assert denied_attempt.stop_confirmed is True

    failed = submit_read(
        session,
        world,
        key="idem_c5_t64",
        goal=goal(summary="Declared-failed synthetic read"),
    )
    session.commit()
    dispatch_and_run(session, world, failed)
    escalate_task(
        session,
        task_id=failed.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.flush()
    takeover_task(
        session,
        task_id=failed.task_id,
        actor=world.actors.owner,
        action="declared_failed",
        evidence_refs=["evidence:c5_t64_stop"],
    )
    session.commit()
    assert failed.state == "failed"
    failed_attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == failed.task_id)
    )
    assert failed_attempt.ended_at is not None
    assert failed_attempt.stop_confirmed is True


# ---------------------------------------------------------------------------
# C6 -- T59/T60 evidence and origins
# ---------------------------------------------------------------------------


def test_t59_requires_a_running_origin_and_persisted_evidence(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.commit()
    assert task.state == "needs_human"
    takeover_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action="running",
    )
    session.commit()
    assert task.state == "running"


def test_t59_is_refused_from_a_failed_origin(session: Session, world: TaskWorld) -> None:
    task = _failed_attempt(session, world, retry_safe=False)
    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "failed"
    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="running",
        )
    assert excinfo.value.reason_code == "grant_revoked"


def test_t60_only_resumes_a_verifying_origin(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    move_to_verifying(session, world, task)
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref="result_c6_0001",
        evidence_refs=["evidence:c6_claimed"],
        evidence=[],
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "verifying"

    takeover_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        action="result_submitted",
        result_ref="result_c6_0002",
        evidence_refs=[EVIDENCE_REF],
    )
    session.commit()
    assert task.state == "verifying"


def test_t60_is_refused_from_a_running_origin(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="result_submitted",
            result_ref="result_c6_0003",
            evidence_refs=[EVIDENCE_REF],
        )
    assert excinfo.value.reason_code == "grant_revoked"


# ---------------------------------------------------------------------------
# C7 -- unreconciled side effects are never replayed
# ---------------------------------------------------------------------------


def test_unreconciled_failure_escalates_instead_of_retrying(
    session: Session, world: TaskWorld
) -> None:
    task = _failed_attempt(session, world, side_effects_reconciled=False)
    old_grant = task.grant_id
    retry_task(session, task_id=task.task_id, actor=world.actors.owner)
    session.commit()
    assert task.state == "needs_human"
    assert task.needs_human_origin == "failed"
    assert task.grant_id == old_grant


# ---------------------------------------------------------------------------
# C8 -- concurrent equivalent goals with different idempotency keys
# ---------------------------------------------------------------------------


def test_concurrent_equivalent_goals_create_one_task(
    engine, world: TaskWorld
) -> None:
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def run(key: str) -> None:
        with Session(engine) as session:
            requester = resolve_employee_principal(
                session, employee_id=world.actors.requester.employee_id
            )
            owner = resolve_employee_principal(
                session, employee_id=world.actors.owner.employee_id
            )
            executor = resolve_employee_principal(
                session, employee_id=world.actors.executor.employee_id
            )
            barrier.wait(timeout=10)
            try:
                task = submit_read(
                    session,
                    world,
                    key=key,
                    requester=requester,
                    owner=owner,
                    executor=executor,
                )
                session.commit()
                results.append(task.task_id)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                session.rollback()
                errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("idem_c8_alpha",)),
        threading.Thread(target=run, args=("idem_c8_beta",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 1, errors
    assert len(errors) == 1
    assert isinstance(errors[0], TaskError)
    assert errors[0].reason_code == "task_duplicate"

    with Session(engine) as check:
        assert check.scalar(select(func.count()).select_from(Task)) == 1
        task_id = results[0]
        assert (
            check.scalar(
                select(func.count())
                .select_from(GovernanceDecisionRecord)
                .where(GovernanceDecisionRecord.task_id == task_id)
            )
            == 1
        )
        assert (
            check.scalar(
                select(func.count())
                .select_from(ExecutionGrant)
                .where(ExecutionGrant.task_id == task_id)
            )
            == 1
        )


# ---------------------------------------------------------------------------
# C9 -- execution-fact producer and managed lease/fencing
# ---------------------------------------------------------------------------


def test_outsider_cannot_report_an_execution_result(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    with pytest.raises(TaskError) as excinfo:
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=world.actors.outsider,
            outcome="completed",
            result_ref="result_c9_0001",
            evidence_refs=[EVIDENCE_REF],
        )
    assert excinfo.value.reason_code == "task_unauthorized_actor"


def test_managed_execution_requires_a_service_producer_and_fencing(
    session: Session, world: TaskWorld
) -> None:
    identity = make_service_identity(session, owner=world.org.admin)
    service = resolve_service_principal(session, identity.id)
    task = submit_read(
        session,
        world,
        key="idem_c9_managed",
        executor=service,
        requested_capabilities=["managed.task@1.0.0"],
        execution_mode="managed_execution",
        runtime_capabilities_verified=True,
    )
    session.commit()
    assert task.state == "queued"

    lease = claim(session, world, worker_id="worker-c9")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=service,
        worker_id="worker-c9",
        fencing_token=lease.lease.fencing_token,
        runtime_ref="rtask_c9_0001",
    )
    session.flush()

    # A person cannot write a managed execution fact.
    with pytest.raises(TaskError) as excinfo:
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="completed",
            result_ref="result_c9_managed_0001",
            evidence_refs=[EVIDENCE_REF],
        )
    assert excinfo.value.reason_code == "task_unauthorized_actor"

    # A stale service token is refused.
    with pytest.raises(TaskError) as excinfo:
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=service,
            worker_id="worker-c9",
            fencing_token=lease.lease.fencing_token + 10,
            outcome="completed",
            result_ref="result_c9_managed_0001",
            evidence_refs=[EVIDENCE_REF],
        )
    assert excinfo.value.reason_code == "task_fencing_token_stale"


# ---------------------------------------------------------------------------
# C10 / Important 10 -- honest unknown dispatch evidence
# ---------------------------------------------------------------------------


def test_dispatch_timeout_does_not_claim_a_stop(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="timeout",
        evidence_refs=["evidence:c10_timeout"],
        reason="dispatch_timeout",
    )
    session.commit()
    session.refresh(task)
    assert task.state == "unknown"
    assert task.stop_confirmed is False
    assert task.result["stop_confirmed"] is False
    assert task.result["stop_evidence_refs"] == []
    assert "evidence:task_never_dispatched" not in task.result["evidence_refs"]


# ---------------------------------------------------------------------------
# Important 1 -- the remaining legal edges have service paths
# ---------------------------------------------------------------------------


def test_clarification_edge_re_evaluates_and_queues(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, resource_scope=None)
    session.commit()
    assert task.state == "awaiting_clarification"

    provide_clarification(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        resource_scope=scope(),
    )
    session.commit()
    assert task.state == "queued"
    assert task.record_completeness == "complete"


def test_clarification_can_be_denied(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world, resource_scope=None)
    session.commit()
    deny_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        reason_code="resource_scope_ambiguous",
    )
    session.commit()
    assert task.state == "denied"


def test_queued_authority_withdrawal_denies(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    deny_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="authority_withdrawn",
    )
    session.commit()
    assert task.state == "denied"


def test_escalation_edges_reach_needs_human(
    session: Session, world: TaskWorld
) -> None:
    gated = submit_gated(session, world, key="idem_edge_gated")
    session.commit()
    assert gated.state == "awaiting_confirmation"
    escalate_task(
        session,
        task_id=gated.task_id,
        actor=world.actors.owner,
        reason_code="no_eligible_confirmer",
    )
    session.flush()
    assert gated.state == "needs_human"

    queued = submit_read(
        session,
        world,
        key="idem_edge_queued",
        priority="low",
        goal=goal(summary="Queued synthetic read"),
    )
    session.commit()
    escalate_task(
        session,
        task_id=queued.task_id,
        actor=world.actors.owner,
        reason_code="no_eligible_executor",
    )
    session.flush()
    assert queued.state == "needs_human"

    running = submit_read(
        session,
        world,
        key="idem_edge_running",
        priority="high",
        goal=goal(summary="Running synthetic read"),
    )
    session.commit()
    dispatch_and_run(session, world, running)
    escalate_task(
        session,
        task_id=running.task_id,
        actor=world.actors.owner,
        reason_code="execution_deadline_exceeded",
    )
    session.flush()
    assert running.state == "needs_human"


def test_remaining_escalation_edges_reach_needs_human(
    session: Session, world: TaskWorld
) -> None:
    # T14 awaiting_clarification
    clarification = submit_read(
        session,
        world,
        key="idem_edge_t14",
        resource_scope=None,
        goal=goal(summary="Clarification synthetic read"),
    )
    session.commit()
    escalate_task(
        session,
        task_id=clarification.task_id,
        actor=world.actors.requester,
        reason_code="clarification_unresolved",
    )
    session.flush()
    assert clarification.state == "needs_human"

    # T22 awaiting_approval
    approval = submit_gated(session, world, key="idem_edge_t22")
    session.commit()
    confirm_task(
        session,
        task_id=approval.task_id,
        actor=world.actors.requester,
        confirmation_id=pending_confirmation(session, approval).confirmation_id,
    )
    session.flush()
    assert approval.state == "awaiting_approval"
    escalate_task(
        session,
        task_id=approval.task_id,
        actor=world.actors.owner,
        reason_code="approval_chain_unavailable",
    )
    session.flush()
    assert approval.state == "needs_human"

    # T52 cancel_requested
    from app.tasks.service import request_cancel

    cancel = submit_read(
        session,
        world,
        key="idem_edge_t52",
        goal=goal(summary="Cancel synthetic read"),
    )
    session.commit()
    dispatch_and_run(session, world, cancel)
    request_cancel(session, task_id=cancel.task_id, actor=world.actors.requester)
    session.flush()
    assert cancel.state == "cancel_requested"
    escalate_task(
        session,
        task_id=cancel.task_id,
        actor=world.actors.owner,
        reason_code="stop_unconfirmed",
    )
    session.flush()
    assert cancel.state == "needs_human"

    # T10 evaluated: a queue guard failure leaves the task evaluated; a human
    # escalation is then the only legal exit.
    employee = world.org.manager
    employee.status = "inactive"
    session.flush()
    with pytest.raises(TaskError):
        submit_read(
            session,
            world,
            key="idem_edge_t10",
            goal=goal(summary="Evaluated synthetic read"),
        )
    evaluated = session.scalar(
        select(Task).where(Task.state == "evaluated").limit(1)
    )
    assert evaluated is not None
    escalate_task(
        session,
        task_id=evaluated.task_id,
        actor=world.actors.requester,
        reason_code="responsibility_conflict",
    )
    session.flush()
    assert evaluated.state == "needs_human"


def test_dispatch_deadline_and_evidence_unavailable_edges(
    session: Session, world: TaskWorld
) -> None:
    dispatched = submit_read(
        session,
        world,
        key="idem_edge_dispatch",
        goal=goal(summary="Dispatched synthetic read"),
    )
    session.commit()
    claim(session, world, worker_id="worker-edge-1")
    start_attempt(
        session,
        task_id=dispatched.task_id,
        actor=world.actors.executor,
        worker_id="worker-edge-1",
        fencing_token=1,
        execution_ref="exec_edge_0001",
    )
    session.flush()
    dispatch_deadline_exceeded(
        session,
        task_id=dispatched.task_id,
        actor=world.actors.executor,
        evidence_refs=["evidence:edge_deadline"],
        no_side_effects_evidence=True,
    )
    session.flush()
    assert dispatched.state == "timed_out"
    assert dispatched.stop_confirmed is True

    verifying = submit_read(
        session,
        world,
        key="idem_edge_verifying",
        goal=goal(summary="Verifying synthetic read"),
    )
    session.commit()
    move_to_verifying(session, world, verifying)
    evidence_unavailable(
        session,
        task_id=verifying.task_id,
        actor=world.actors.owner,
        evidence_refs=["evidence:edge_source_down"],
    )
    session.flush()
    assert verifying.state == "unknown"


# ---------------------------------------------------------------------------
# Important 2 -- fail_task from queued produces T28/T66
# ---------------------------------------------------------------------------


def test_fail_task_from_queued_sets_dispatch_context(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    failed = __import__("app.tasks.service", fromlist=["fail_task"]).fail_task(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        reason="boundary_rejected",
        dispatch_outcome="rejected",
    )
    session.commit()
    assert failed.state == "failed"
    assert failed.attempt == 0
    assert failed.stop_confirmed is True
    assert failed.result["no_side_effects"] is True


# ---------------------------------------------------------------------------
# Important 3 -- completion re-checks live gates
# ---------------------------------------------------------------------------


def test_completion_is_refused_when_the_approval_expired(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=pending_confirmation(session, task).confirmation_id,
    )
    record_approval(session, task_id=task.task_id, actor=world.actors.approver, decision="approve")
    session.commit()
    assert task.state == "queued"

    lease = claim(session, world, worker_id="worker-imp3")
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-imp3",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_imp3_0001",
    )
    from app.tasks.service import mark_running

    mark_running(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-imp3",
        fencing_token=lease.lease.fencing_token,
        evidence_ref="evidence:imp3_running",
    )
    session.flush()
    task.approval_ref = {
        **dict(task.approval_ref or {}),
        "expires_at": (utcnow() - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    }
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        record_attempt_result(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="completed",
            result_ref="result_imp3_0001",
            evidence_refs=[EVIDENCE_REF],
        )
    assert excinfo.value.reason_code == "approval_missing"


def test_completion_is_refused_after_grant_revocation(
    session: Session, world: TaskWorld
) -> None:
    from app.governance.grants import revoke_grant

    task = submit_read(session, world)
    session.commit()
    move_to_verifying(session, world, task)
    revoke_grant(
        session,
        grant_id=task.grant_id,
        reason_code="policy_changed",
        principal=world.actors.executor,
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        complete_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            result_ref="result_imp3_0002",
            evidence_refs=[EVIDENCE_REF],
            evidence=[evidence()],
        )
    assert excinfo.value.reason_code == "grant_revoked"


# ---------------------------------------------------------------------------
# Important 4/5 -- fairness, SLA worker and owner refresh at claim
# ---------------------------------------------------------------------------


def test_claim_uses_aging_and_fairness(session: Session, world: TaskWorld) -> None:
    old_low = submit_read(
        session, world, key="idem_fair_low", priority="low"
    )
    session.flush()
    old_low.created_at = utcnow() - timedelta(hours=4)
    urgent = submit_read(
        session,
        world,
        key="idem_fair_urgent",
        priority="urgent",
        goal=goal(summary="Urgent synthetic read"),
        priority_authorized_by=world.actors.owner.principal_id,
        priority_rationale="synthetic deadline",
    )
    session.commit()
    claimed = worker.claim_task(session, worker_id="worker-fair", actor=world.actors.executor)
    assert claimed is not None
    assert claimed.task.task_id == old_low.task_id
    assert claimed.task.task_id != urgent.task_id


def test_claim_escalates_a_disabled_owner(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    employee = world.org.manager
    employee.status = "inactive"
    session.flush()

    claimed = worker.claim_task(session, worker_id="worker-owner", actor=world.actors.executor)
    session.commit()
    assert claimed is None
    session.refresh(task)
    assert task.state == "needs_human"
    assert task.owner_account_status == "disabled"
    assert task.owner_escalation is not None


def test_sla_worker_expires_an_overdue_queued_task(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    task.due_at = utcnow() - timedelta(minutes=5)
    session.commit()
    results = worker.escalate_overdue_tasks(
        session, actor=world.actors.executor, now=utcnow()
    )
    session.commit()
    assert any(item.task_id == task.task_id for item in results)
    session.refresh(task)
    assert task.state == "timed_out"


# ---------------------------------------------------------------------------
# Important 6/7 -- urgency authorisation and task budget
# ---------------------------------------------------------------------------


def test_urgent_priority_requires_a_real_authority(
    session: Session, world: TaskWorld
) -> None:
    with pytest.raises(TaskError) as excinfo:
        submit_read(
            session,
            world,
            priority="urgent",
            priority_authorized_by=world.actors.outsider.principal_id,
            priority_rationale="self asserted",
        )
    assert excinfo.value.reason_code == "task_priority_unauthorized"


def test_task_budget_is_persisted_and_narrows_the_grant(
    session: Session, world: TaskWorld
) -> None:
    with pytest.raises(TaskError):
        submit_read(session, world, max_calls=0, key="idem_budget_bad")
    task = submit_read(session, world, max_calls=1, max_runtime_seconds=120)
    session.commit()
    assert task.max_calls == 1
    assert task.max_runtime_seconds == 120
    grant = get_grant(session, task.grant_id)
    assert grant.budget["max_calls"] <= task.max_calls
    assert grant.budget["max_runtime_seconds"] <= task.max_runtime_seconds


# ---------------------------------------------------------------------------
# Important 8/11/12/13 -- approval revocation, orphan grants, result shape, assign
# ---------------------------------------------------------------------------


def test_revoked_approval_cannot_queue_the_task(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=pending_confirmation(session, task).confirmation_id,
    )
    record_approval(session, task_id=task.task_id, actor=world.actors.approver, decision="approve")
    session.flush()
    assert task.state == "queued"

    revoke_approval(
        session,
        task_id=task.task_id,
        actor=world.actors.approver,
        reason_code="approval_revoked",
    )
    session.commit()
    assert task.state == "denied"


def test_failed_queue_guard_revokes_the_orphan_grant(
    session: Session, world: TaskWorld
) -> None:
    employee = world.org.manager
    employee.status = "inactive"
    session.flush()
    task = None
    with pytest.raises(TaskError):
        task = submit_read(session, world)
    session.commit()
    stored = session.scalar(select(Task).where(Task.state == "evaluated"))
    assert stored is not None
    # The guard is now previewed before issuance, so a refused queue leaves no
    # grant at all.  If a grant was ever issued it must not be active.
    if stored.grant_id is not None:
        assert get_grant(session, stored.grant_id).status == "revoked"


def test_rejected_partial_records_a_failed_verification_shape(
    session: Session, world: TaskWorld
) -> None:
    two_criteria = goal()
    two_criteria["completion_criteria"].append(
        {
            "criterion_id": "crit_review_second",
            "type": "business_state_equals",
            "verifier": {"verifier_id": VERIFIER_ID, "version": VERIFIER_VERSION},
            "resource_ref": "res_expense_0002",
            "expected": "submitted",
            "evidence_ref": "evidence:review_second",
            "evidence_hash": None,
        }
    )
    task = submit_read(session, world, goal=two_criteria)
    session.commit()
    move_to_verifying(session, world, task)
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref="result_imp12_0001",
        evidence_refs=[EVIDENCE_REF, "evidence:review_second"],
        evidence=[
            evidence(criterion_id=CRITERION_ID),
            evidence(criterion_id="crit_review_second", observed="draft"),
        ],
        allow_partial=True,
        owner_accepted_partial=False,
    )
    session.commit()
    assert task.state == "failed"
    assert task.result["verification"]["result"] == "failed"


def test_assignment_invalidates_stale_authority(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, resource_scope=None)
    session.commit()
    assert task.state == "awaiting_clarification"

    assign_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        executor=world.actors.owner,
    )
    session.commit()
    assert task.executor_principal_id == world.actors.owner.principal_id
    assert task.record_completeness == "pre_execution_incomplete"


def test_dependency_cannot_change_after_execution_starts(
    session: Session, world: TaskWorld
) -> None:
    first = submit_read(session, world, key="idem_dep_imp13_a", priority="low")
    second = submit_read(
        session,
        world,
        key="idem_dep_imp13_b",
        priority="high",
        goal=goal(summary="Second dependency task"),
    )
    session.commit()
    dispatch_and_run(session, world, second)
    from app.tasks.service import add_dependency

    with pytest.raises(TaskError) as excinfo:
        add_dependency(
            session,
            task_id=second.task_id,
            depends_on_task_id=first.task_id,
            actor=world.actors.requester,
        )
    assert excinfo.value.reason_code == "task_invalid_transition"


# ---------------------------------------------------------------------------
# Minor -- evidence-ref syntax and confirmation issue time
# ---------------------------------------------------------------------------


def test_stop_rejects_malformed_evidence(session: Session, world: TaskWorld) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    with pytest.raises(TaskError):
        confirm_stop(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            evidence_refs=["not-an-evidence-ref"],
        )


def test_confirmation_records_issued_at(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world)
    session.commit()
    confirmation = pending_confirmation(session, task)
    assert confirmation.issued_at is not None
    # ``issued_at`` is a persisted column, not a contract ``confirmationRef``
    # field: keeping it out of the document preserves the frozen schema.
    from app.tasks.service import _confirmation_document

    assert "issued_at" not in _confirmation_document(confirmation)
