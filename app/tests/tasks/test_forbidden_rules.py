"""One rejection assertion for every forbidden rule F01-F41.

Every rule declares, in :data:`RULE_COVERAGE`, both *where* it is asserted and
*what* the assertion observes: pure state-machine families are executed and
checked against the actual ``error.context["rule"]``; cross-object rules must be
asserted by a named test through their stable ``reason_code`` (or the violated
database constraint).  A rule whose named test stops asserting its mapping fails
the coverage test instead of silently passing on function existence.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    ("F14", "succeeded", "made.up", "queued"),
    ("F20", "unknown", "execution.stopped", "cancelled"),
    ("F37", "unknown", "execution.started", "running"),
    ("F38", "unknown", "execution.completed", "verifying"),
]


@dataclass(frozen=True)
class RuleCoverage:
    """Where one forbidden rule is asserted and what the assertion observes.

    * ``table`` -- the rule is a pure state-machine family; the coverage test
      executes that edge and asserts the *actual* ``context["rule"]``.
    * ``codes`` -- a cross-object rule, asserted by the named test through its
      stable ``reason_code``; the coverage test verifies the test really asserts
      one of the declared codes.
    * ``constraint`` -- a database backstop, asserted by the named test through
      the violated constraint name.
    """

    location: str
    codes: tuple[str, ...] = ()
    table: tuple[str, str, str] | None = None
    constraint: str | None = None


#: Every rule (F01-F41), where it is asserted and what the assertion observes.
RULE_COVERAGE: dict[str, RuleCoverage] = {
    "F01": RuleCoverage("", table=("draft", "execution.started", "running")),
    "F02": RuleCoverage("", table=("queued", "execution.started", "running")),
    "F03": RuleCoverage("", table=("dispatched", "verification.passed", "succeeded")),
    "F04": RuleCoverage("", table=("running", "verification.passed", "succeeded")),
    "F05": RuleCoverage("", table=("unknown", "verification.passed", "succeeded")),
    "F06": RuleCoverage(
        "", table=("cancel_requested", "verification.passed", "succeeded")
    ),
    "F07": RuleCoverage(
        "test_f07_cancel_request_is_not_a_stop", codes=("stop_not_confirmed",)
    ),
    "F08": RuleCoverage("", table=("denied", "task.queued", "queued")),
    "F09": RuleCoverage("", table=("awaiting_approval", "task.queued", "queued")),
    "F10": RuleCoverage("", table=("awaiting_confirmation", "task.queued", "queued")),
    "F11": RuleCoverage("", table=("timed_out", "execution.started", "running")),
    "F12": RuleCoverage("", table=("failed", "verification.passed", "succeeded")),
    "F13": RuleCoverage(
        "", table=("partial_success", "verification.passed", "succeeded")
    ),
    "F14": RuleCoverage("", table=("succeeded", "task.cancelled", "cancelled")),
    "F15": RuleCoverage(
        "test_f15_revoked_grant_cannot_dispatch", codes=("grant_revoked",)
    ),
    "F16": RuleCoverage(
        "test_f16_single_use_grant_is_consumed_once",
        codes=("grant_consumed", "confirmation_missing", "replay_detected"),
    ),
    "F17": RuleCoverage(
        "test_f17_grant_is_bound_to_its_task", codes=("grant_scope_mismatch",)
    ),
    "F18": RuleCoverage(
        "test_f18_self_approval_is_refused", codes=("self_approval_forbidden",)
    ),
    "F19": RuleCoverage(
        "test_f19_priority_cannot_reopen_a_deny",
        codes=("priority_cannot_override_deny",),
    ),
    "F20": RuleCoverage("", table=("unknown", "execution.stopped", "cancelled")),
    "F21": RuleCoverage(
        "test_f21_dispatch_requires_a_live_grant", codes=("grant_revoked",)
    ),
    "F22": RuleCoverage(
        "test_f22_verifying_needs_a_result", codes=("task_result_missing",)
    ),
    "F23": RuleCoverage(
        "test_f23_worker_needs_a_live_lease", codes=("task_lease_not_held",)
    ),
    "F24": RuleCoverage(
        "test_f24_state_is_only_written_by_the_machine",
        codes=("task_forbidden_transition",),
    ),
    "F25": RuleCoverage(
        "test_f15_revoked_grant_cannot_dispatch", codes=("grant_revoked",)
    ),
    "F26": RuleCoverage(
        "test_f26_mode_is_bound_to_the_decision",
        codes=("runtime_capability_unverified",),
    ),
    "F27": RuleCoverage(
        "test_f27_running_requires_an_attempt", codes=("task_attempt_missing",)
    ),
    "F28": RuleCoverage(
        "test_f28_undetermined_scope_is_not_executed",
        codes=("resource_scope_ambiguous",),
    ),
    "F29": RuleCoverage(
        "test_f22_verifying_needs_a_result", codes=("task_result_missing",)
    ),
    "F30": RuleCoverage(
        "test_f30_cancelled_requires_a_stop", codes=("stop_not_confirmed",)
    ),
    "F31": RuleCoverage(
        "test_f31_free_text_goal_is_refused",
        codes=("task_completion_criteria_missing",),
    ),
    "F32": RuleCoverage(
        "test_f32_retry_requires_a_fresh_decision",
        codes=("task_retry_requires_fresh_authorization",),
    ),
    "F33": RuleCoverage(
        "test_f33_agent_cannot_be_owner", codes=("accountable_owner_unresolved",)
    ),
    "F34": RuleCoverage(
        "test_f34_terminal_grant_is_invalidated",
        codes=("grant_revoked", "task_invalid_transition"),
    ),
    "F35": RuleCoverage(
        "test_f35_incomplete_record_cannot_hold_a_grant",
        constraint="incomplete_record_has_no_grant",
    ),
    "F36": RuleCoverage(
        "test_f36_unknown_requeue_needs_evidence",
        codes=("task_reconcile_evidence_missing",),
    ),
    "F37": RuleCoverage("", table=("unknown", "execution.started", "running")),
    "F38": RuleCoverage("", table=("unknown", "execution.completed", "verifying")),
    "F39": RuleCoverage(
        "test_f39_attempt_zero_human_failure_is_refused",
        codes=("task_attempt_missing",),
    ),
    "F40": RuleCoverage(
        "test_f15_revoked_grant_cannot_dispatch", codes=("grant_revoked",)
    ),
    "F41": RuleCoverage(
        "test_f41_unknown_origin_cannot_resume", codes=("grant_revoked",)
    ),
}


def test_every_forbidden_rule_has_a_real_assertion() -> None:
    """F01-F41 must each be checked against its *actual* rule mapping."""
    expected = {f"F{number:02d}" for number in range(1, 42)}
    assert set(RULE_COVERAGE) == expected
    module = sys.modules[__name__]
    for rule, coverage in RULE_COVERAGE.items():
        if coverage.table is not None:
            # Pure table family: execute the edge and assert the *rule id*, not
            # merely that "something" was rejected.
            source, event, target = coverage.table
            assert (source, event, target) in {
                tuple(item[1:]) for item in PURE_RULES if item[0] == rule
            }, f"{rule} table case is not declared in PURE_RULES"
            with pytest.raises(TaskError) as excinfo:
                state_machine.check_transition(
                    source, event, target, TransitionContext.permissive()
                )
            assert excinfo.value.context.get("rule") == rule, excinfo.value.context
            assert excinfo.value.reason_code == "task_forbidden_transition"
            continue

        test = getattr(module, coverage.location, None)
        assert callable(test), f"{rule} points at missing test {coverage.location!r}"
        body = inspect.getsource(test)
        if coverage.constraint is not None:
            assert coverage.constraint in body, (
                f"{rule}: {coverage.location} does not assert the "
                f"{coverage.constraint!r} constraint"
            )
            assert "IntegrityError" in body, (
                f"{rule}: {coverage.location} does not assert IntegrityError"
            )
            continue
        assert coverage.codes, f"{rule} declares no observable reason code"
        assert "reason_code" in body, (
            f"{rule}: {coverage.location} does not assert a reason code"
        )
        assert any(code in body for code in coverage.codes), (
            f"{rule}: {coverage.location} does not assert any of {coverage.codes}"
        )
    # Every pure-table case must be claimed by a coverage entry.
    assert {rule for rule, *_rest in PURE_RULES} <= set(RULE_COVERAGE)


@pytest.mark.parametrize(("rule", "source", "event", "target"), PURE_RULES)
def test_forbidden_state_edges(
    rule: str, source: str, event: str, target: str
) -> None:
    ctx = TransitionContext.permissive()
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition(source, event, target, ctx)
    error = excinfo.value
    # Assert the *actual* forbidden rule.  A bare ``or error.reason_code`` is a
    # tautology: every rejection has a truthy reason code, so it would pass for
    # the wrong rule (or for a plain invalid edge).
    assert error.context.get("rule") == rule, error.context


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
    from app.tasks.service import mark_running

    task = submit_read(session, world)
    session.commit()
    # A controlled call requires an accepted submission (F16/F21): dispatch the
    # task through a real lease, then revoke its grant.
    lease = claim(session, world, worker_id="worker-f15")
    session.flush()
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-f15",
        fencing_token=lease.lease.fencing_token,
        execution_ref="exec_f15_0001",
    )
    mark_running(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-f15",
        fencing_token=lease.lease.fencing_token,
        evidence_ref="evidence:f15_running",
    )
    session.flush()
    revoke_grant(
        session,
        grant_id=task.grant_id,
        reason_code="policy_changed",
        principal=world.actors.executor,
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability="expense.read@1.0.0",
            call_id="call_f15_0001",
        )
    assert excinfo.value.reason_code == "grant_revoked"


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
    # Both tasks need an accepted submission before the PEP runs.  The claim
    # order is scheduling-owned, so dispatch whichever row each claim returns.
    for worker, ref in (
        ("worker-f17-first", "exec_f17_first"),
        ("worker-f17-second", "exec_f17_second"),
    ):
        lease = claim(session, world, worker_id=worker)
        start_attempt(
            session,
            task_id=lease.task.task_id,
            actor=world.actors.executor,
            worker_id=worker,
            fencing_token=lease.lease.fencing_token,
            execution_ref=ref,
        )
        session.flush()
    assert first.state == "dispatched"
    assert second.state == "dispatched"
    second.grant_id = first.grant_id
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=second.task_id,
            actor=world.actors.executor,
            capability="expense.read@1.0.0",
            call_id="call_f17_0001",
        )
    assert excinfo.value.reason_code == "grant_scope_mismatch"


def test_f18_self_approval_is_refused(session: Session, world: TaskWorld) -> None:
    with pytest.raises(OwnerError) as excinfo:
        submit_gated(session, world, approver=world.actors.requester)
    assert excinfo.value.reason_code == "self_approval_forbidden"


def test_f19_priority_cannot_reopen_a_deny(session: Session, world: TaskWorld) -> None:
    from app.tasks.priority import assert_scheduling_only

    with pytest.raises(TaskError) as excinfo:
        assert_scheduling_only(
            state="denied", decision_type="deny", gates_satisfied=False
        )
    assert excinfo.value.reason_code == "priority_cannot_override_deny"


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
    with pytest.raises(TaskError) as excinfo:
        start_attempt(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            worker_id="worker-f21",
            fencing_token=lease.lease.fencing_token,
            execution_ref="exec_f21_0001",
        )
    assert excinfo.value.reason_code == "grant_revoked"


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
    with pytest.raises(TaskError) as excinfo:
        # An undeclared write from ``queued`` is refused as a forbidden family
        # (F02) before it can ever reach the state column.
        state_machine.check_transition("queued", "made.up", "succeeded", ctx)
    assert excinfo.value.reason_code == "task_forbidden_transition"
    assert excinfo.value.context.get("rule") == "F02"


def test_f26_mode_is_bound_to_the_decision(
    session: Session, world: TaskWorld
) -> None:
    # T08 refuses to queue a mode whose capability was never verified.
    ctx = replace(TransitionContext.permissive(), mode_capabilities_ok=False)
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition("evaluated", "task.queued", "queued", ctx)
    assert excinfo.value.reason_code == "runtime_capability_unverified"

    with pytest.raises(TaskError):
        submit_read(session, world, execution_mode="managed_execution")
    session.rollback()


def test_f27_running_requires_an_attempt() -> None:
    ctx = replace(
        TransitionContext.permissive(),
        attempt=0,
        attempt_started=False,
    )
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition("dispatched", "execution.started", "running", ctx)
    assert excinfo.value.reason_code == "task_attempt_missing"


def test_f28_undetermined_scope_is_not_executed(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world, resource_scope=None)
    session.commit()
    assert task.state == "awaiting_clarification"
    assert task.grant_id is None

    # T08 also refuses to queue an undetermined scope directly.
    ctx = replace(
        TransitionContext.permissive(),
        scope_type="undetermined",
        grant_id="grnt_f28",
        grant_status="active",
    )
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition("evaluated", "task.queued", "queued", ctx)
    assert excinfo.value.reason_code == "resource_scope_ambiguous"


def test_f30_cancelled_requires_a_stop() -> None:
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


def test_f31_free_text_goal_is_refused() -> None:
    from app.tasks import verification

    with pytest.raises(TaskError) as excinfo:
        verification.validate_goal(
            {"summary": "done", "completion_criteria": [{"type": "free_text"}]}
        )
    assert excinfo.value.reason_code == "task_completion_criteria_missing"


def test_f32_retry_requires_a_fresh_decision() -> None:
    ctx = replace(
        TransitionContext.permissive(),
        decision_purpose="initial_execution",
        decision_revision=1,
        decision_supersedes=None,
        fresh_decision_and_grant=False,
    )
    with pytest.raises(TaskError) as excinfo:
        state_machine.check_transition("failed", "task.retry_authorized", "queued", ctx)
    assert excinfo.value.reason_code == "task_retry_requires_fresh_authorization"


def test_f33_agent_cannot_be_owner(session: Session, world: TaskWorld) -> None:
    from app.governance.principal import resolve_employee_principal

    agent = resolve_employee_principal(
        session,
        employee_id=world.actors.owner.employee_id,
        principal_type="agent_instance",
    )
    with pytest.raises(OwnerError) as excinfo:
        submit_read(session, world, owner=agent, key="idem_f33_owner")
    assert excinfo.value.reason_code == "accountable_owner_unresolved"


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
    from app.governance.grants import get_grant

    session.commit()
    assert task.grant_status in {"revoked", "consumed"}
    grant = get_grant(session, task.grant_id)
    assert grant.status == "revoked"
    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability="expense.read@1.0.0",
            call_id="call_f34_0001",
        )
    assert excinfo.value.reason_code in {"grant_revoked", "task_invalid_transition"}


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
    with pytest.raises(IntegrityError) as excinfo:
        with session.begin_nested():
            session.add(task)
            session.flush()
    assert "incomplete_record_has_no_grant" in str(excinfo.value)


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
    with pytest.raises(TaskError) as excinfo:
        reconcile_task(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            outcome="not_found",
            no_side_effect_evidence=False,
        )
    assert excinfo.value.reason_code == "task_reconcile_evidence_missing"


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
