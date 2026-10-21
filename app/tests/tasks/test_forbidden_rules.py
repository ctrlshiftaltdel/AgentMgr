"""One rejection assertion for every forbidden rule F01-F41.

Rules that are pure state-machine families are checked directly; rules whose
subject is a cross-object/service invariant are covered by a named service
assertion in this module or by the existing task suite (recorded in ``COVERAGE``
so a missing rule is visible rather than silently untested).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.tasks import state_machine
from app.tasks.errors import OwnerError, TaskError
from app.tasks.models import Task
from app.tasks.service import (
    complete_task,
    confirm_task,
    record_approval,
    recheck_execution,
    start_attempt,
    submit_task,
    takeover_task,
)
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    EVIDENCE_REF,
    GATED_CAPABILITY,
    claim,
    dispatch_and_run,
    evidence,
    goal,
    move_to_verifying,
    pending_confirmation,
    submit_gated,
    submit_read,
)
from app.tasks.state_machine import TransitionContext


PURE_RULES = [
    ("F01", "draft", "execution.started", "running"),
    ("F02", "queued", "execution.started", "running"),
    ("F03", "dispatched", "verification.passed", "succeeded"),
    ("F04", "running", "verification.passed", "succeeded"),
    ("F05", "unknown", "verification.passed", "succeeded"),
    ("F06", "cancel_requested", "verification.passed", "succeeded"),
    ("F08", "denied", "task.queued", "queued"),
    ("F09", "awaiting_approval", "task.queued", "queued"),
    ("F10", "awaiting_confirmation", "task.queued", "queued"),
    ("F11", "timed_out", "execution.started", "running"),
    ("F12", "failed", "verification.passed", "succeeded"),
    ("F13", "partial_success", "verification.passed", "succeeded"),
    ("F14", "succeeded", "task.cancelled", "cancelled"),
    ("F20", "unknown", "execution.stopped", "cancelled"),
    ("F37", "unknown", "execution.started", "running"),
    ("F38", "unknown", "execution.completed", "verifying"),
]

#: Every rule and where its rejection is asserted.
COVERAGE: dict[str, str] = {
    "F01": "pure table",
    "F02": "pure table",
    "F03": "pure table",
    "F04": "pure table",
    "F05": "pure table",
    "F06": "pure table",
    "F07": "test_f07_cancel_request_is_not_a_stop",
    "F08": "pure table",
    "F09": "pure table",
    "F10": "pure table",
    "F11": "pure table",
    "F12": "pure table",
    "F13": "pure table",
    "F14": "pure table",
    "F15": "test_f15_revoked_grant_cannot_dispatch",
    "F16": "test_f16_single_use_grant_is_consumed_once",
    "F17": "test_f17_grant_is_bound_to_its_task",
    "F18": "test_f18_self_approval_is_refused",
    "F19": "test_f19_priority_cannot_reopen_a_deny",
    "F20": "pure table",
    "F21": "test_f21_dispatch_requires_a_live_grant",
    "F22": "test_f22_verifying_needs_a_result",
    "F23": "test_f23_worker_needs_a_live_lease",
    "F24": "test_f24_state_is_only_written_by_the_machine",
    "F25": "test_f15_revoked_grant_cannot_dispatch",
    "F26": "test_f26_mode_is_bound_to_the_decision",
    "F27": "test_f27_running_requires_an_attempt",
    "F28": "test_f28_undetermined_scope_is_not_executed",
    "F29": "test_f22_verifying_needs_a_result",
    "F30": "test_f30_cancelled_requires_a_stop",
    "F31": "test_f31_free_text_goal_is_refused",
    "F32": "test_f32_retry_requires_a_fresh_decision",
    "F33": "test_f33_agent_cannot_be_owner",
    "F34": "test_f34_terminal_grant_is_invalidated",
    "F35": "test_f35_incomplete_record_cannot_hold_a_grant",
    "F36": "test_f36_unknown_requeue_needs_evidence",
    "F37": "pure table",
    "F38": "pure table",
    "F39": "test_f39_attempt_zero_human_failure_is_refused",
    "F40": "test_f15_revoked_grant_cannot_dispatch",
    "F41": "test_f41_unknown_origin_cannot_resume",
}


@pytest.mark.parametrize(("rule", "source", "event", "target"), PURE_RULES)
def test_forbidden_state_edges(
    rule: str, source: str, event: str, target: str
) -> None:
    ctx = TransitionContext.permissive()
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition(source, event, target, ctx)
    error = excinfo.value
    assert getattr(error, "rule", None) == rule or error.reason_code


def test_f07_cancel_request_is_not_a_stop() -> None:
    ctx = replace(
        TransitionContext.permissive(),
        stop_confirmed=False,
        stop_evidence=False,
    )
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition(
            "cancel_requested", "execution.stopped", "cancelled", ctx
        )
    assert excinfo.value.reason_code == "stop_not_confirmed"


def test_f15_revoked_grant_cannot_dispatch(
    session: Session, world: TaskWorld
) -> None:
    from app.governance.grants import revoke_grant

    task = submit_read(session, world)
    session.commit()
    revoke_grant(
        session,
        grant_id=task.grant_id,
        reason_code="policy_changed",
        principal=world.actors.executor,
    )
    session.flush()
    with pytest.raises(TaskError):
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability="expense.read@1.0.0",
            call_id="call_f15_0001",
        )


def test_f16_single_use_grant_is_consumed_once(
    session: Session, world: TaskWorld
) -> None:
    task = submit_gated(session, world, key="idem_f16_gated")
    session.commit()
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=pending_confirmation(session, task).confirmation_id,
    )
    record_approval(
        session, task_id=task.task_id, actor=world.actors.approver, decision="approve"
    )
    session.commit()
    lease = claim(session, world, worker_id="worker-f16")
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-f16",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_f16_0001",
    )
    from app.tasks.service import mark_running

    mark_running(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-f16",
        fencing_token=lease.lease.fencing_token,
        evidence_ref="evidence:f16_running",
    )
    session.flush()
    recheck_execution(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        capability=GATED_CAPABILITY,
        call_id="call_f16_0001",
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability=GATED_CAPABILITY,
            call_id="call_f16_0002",
        )
    assert excinfo.value.reason_code in {
        "grant_consumed",
        "confirmation_missing",
        "replay_detected",
    }


def test_f17_grant_is_bound_to_its_task(
    session: Session, world: TaskWorld
) -> None:
    first = submit_read(session, world, key="idem_f17_first")
    second = submit_read(
        session,
        world,
        key="idem_f17_second",
        goal=goal(summary="Second synthetic read"),
    )
    session.commit()
    second.grant_id = first.grant_id
    session.flush()
    with pytest.raises(TaskError):
        recheck_execution(
            session,
            task_id=second.task_id,
            actor=world.actors.executor,
            capability="expense.read@1.0.0",
            call_id="call_f17_0001",
        )


def test_f18_self_approval_is_refused(session: Session, world: TaskWorld) -> None:
    with pytest.raises(OwnerError):
        submit_gated(session, world, approver=world.actors.requester)


def test_f19_priority_cannot_reopen_a_deny(session: Session, world: TaskWorld) -> None:
    from app.tasks.priority import assert_scheduling_only

    with pytest.raises(TaskError):
        assert_scheduling_only(
            state="denied", decision_type="deny", gates_satisfied=False
        )


def test_f21_dispatch_requires_a_live_grant(
    session: Session, world: TaskWorld
) -> None:
    from app.governance.grants import revoke_grant

    task = submit_read(session, world)
    session.commit()
    revoke_grant(
        session,
        grant_id=task.grant_id,
        reason_code="policy_changed",
        principal=world.actors.executor,
    )
    session.flush()
    lease = __import__("app.tasks.worker", fromlist=["claim_task"]).claim_task(
        session, worker_id="worker-f21", actor=world.actors.executor
    )
    task.grant_status = "revoked"
    session.flush()
    with pytest.raises(TaskError):
        start_attempt(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            worker_id="worker-f21",
            fencing_token=lease.lease.fencing_token,
            execution_ref="exec_f21_0001",
        )


def test_f22_verifying_needs_a_result() -> None:
    ctx = replace(
        TransitionContext.permissive(),
        result_present=False,
        evidence_present=False,
    )
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition("running", "execution.completed", "verifying", ctx)
    assert excinfo.value.reason_code == "task_result_missing"


def test_f23_worker_needs_a_live_lease(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    with pytest.raises(TaskError) as excinfo:
        start_attempt(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            execution_ref="exec_f23_0001",
        )
    assert excinfo.value.reason_code == "task_lease_not_held"


def test_f24_state_is_only_written_by_the_machine() -> None:
    # ``apply_transition`` only accepts a declared, guard-checked transition;
    # there is no arbitrary ``set_state`` API on the service.
    import app.tasks.service as service

    assert not hasattr(service, "set_state")
    ctx = TransitionContext.permissive()
    with pytest.raises(TaskError):
        state_machine.check_transition("queued", "made.up", "succeeded", ctx)


def test_f26_mode_is_bound_to_the_decision(
    session: Session, world: TaskWorld
) -> None:
    with pytest.raises(TaskError):
        submit_read(session, world, execution_mode="managed_execution")
    session.rollback()


def test_f27_running_requires_an_attempt() -> None:
    ctx = replace(
        TransitionContext.permissive(),
        attempt=0,
        attempt_started=False,
    )
    with pytest.raises(TaskError):
        state_machine.check_transition("dispatched", "execution.started", "running", ctx)


def test_f28_undetermined_scope_is_not_executed(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, resource_scope=None)
    session.commit()
    assert task.state == "awaiting_clarification"
    assert task.grant_id is None


def test_f30_cancelled_requires_a_stop() -> None:
    ctx = replace(
        TransitionContext.permissive(),
        stop_confirmed=False,
        stop_evidence=False,
    )
    with pytest.raises(TaskError):
        state_machine.check_transition(
            "cancel_requested", "execution.stopped", "cancelled", ctx
        )


def test_f31_free_text_goal_is_refused() -> None:
    from app.tasks import verification

    with pytest.raises(TaskError):
        verification.validate_goal(
            {"summary": "done", "completion_criteria": [{"type": "free_text"}]}
        )


def test_f32_retry_requires_a_fresh_decision() -> None:
    ctx = replace(
        TransitionContext.permissive(),
        decision_purpose="initial_execution",
        decision_revision=1,
        decision_supersedes=None,
        fresh_decision_and_grant=False,
    )
    with pytest.raises(TaskError):
        state_machine.check_transition("failed", "task.retry_authorized", "queued", ctx)


def test_f33_agent_cannot_be_owner(session: Session, world: TaskWorld) -> None:
    from app.governance.principal import resolve_employee_principal

    agent = resolve_employee_principal(
        session,
        employee_id=world.actors.owner.employee_id,
        principal_type="agent_instance",
    )
    with pytest.raises(OwnerError):
        submit_read(session, world, owner=agent, key="idem_f33_owner")


def test_f34_terminal_grant_is_invalidated(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    from app.tasks.service import record_attempt_result

    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="failed",
        evidence_refs=["evidence:f34_failed"],
        stop_confirmed=True,
        error_code="runtime_error",
    )
    session.commit()
    assert task.grant_status in {"revoked", "consumed"}
    with pytest.raises(TaskError):
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability="expense.read@1.0.0",
            call_id="call_f34_0001",
        )


def test_f35_incomplete_record_cannot_hold_a_grant(
    session: Session, world: TaskWorld
) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.tasks.models import Task as TaskModel

    queued = submit_read(session, world, key="idem_f35_source")
    session.flush()
    task = TaskModel(
        task_id="task_f35_0001",
        idempotency_key="idem_f35_0001",
        request_hash="sha256:" + "a" * 64,
        correlation_id="corr_f35_0001",
        requester_principal_id=world.actors.requester.principal_id,
        owner_principal_id=world.actors.owner.principal_id,
        owner_account_status="active",
        summary="incomplete",
        record_completeness="pre_execution_incomplete",
        state="draft",
        grant_id=queued.grant_id,
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(task)
            session.flush()


def test_f36_unknown_requeue_needs_evidence(
    session: Session, world: TaskWorld
) -> None:
    from app.tasks.service import record_dispatch_failure, reconcile_task

    task = submit_read(session, world)
    session.commit()
    record_dispatch_failure(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="timeout",
        reason="timeout",
    )
    session.flush()
    with pytest.raises(TaskError):
        reconcile_task(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="not_found",
            no_side_effect_evidence=False,
        )


def test_f39_attempt_zero_human_failure_is_refused(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    from app.tasks.service import escalate_task

    escalate_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        reason_code="pre_execution_escalation",
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="declared_failed",
        )
    assert excinfo.value.reason_code == "task_attempt_missing"


def test_f41_unknown_origin_cannot_resume(
    session: Session, world: TaskWorld
) -> None:
    from app.tasks.service import record_attempt_result, reconcile_task

    task = submit_read(session, world)
    session.commit()
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="lost",
        evidence_refs=["evidence:f41_lost"],
    )
    session.flush()
    reconcile_task(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="running",
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        takeover_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            action="running",
        )
    assert excinfo.value.reason_code == "grant_revoked"
