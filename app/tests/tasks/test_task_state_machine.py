"""Pure state-machine tests: 72 allowed edges and F01-F41 refusals.

These tests touch no database.  Every allowed transition is exercised with a
context that supplies its verified facts; every forbidden family is asserted to
raise the stable ``F##`` rule, not merely a generic error.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.tasks.errors import TaskError, TaskForbiddenTransition, TaskInvalidTransition
from app.tasks.models import TASK_STATES, TASK_TERMINAL_STATES
from app.tasks.state_machine import (
    FORBIDDEN_RULES,
    TRANSITIONS,
    TransitionContext,
    check_transition,
    find_transition,
    is_terminal,
)

_NEVER_DISPATCHED = {"T03", "T13", "T17", "T21", "T27", "T28", "T66", "T71"}
_DENY_EVENTS = {"governance.denied"}
_CLARIFY = {"governance.clarification_required"}
_CONFIRM = {"governance.confirmation_required"}
_APPROVE = {"governance.approval_required"}
_CONFIRM_REJECT = {"confirmation.rejected"}
_APPROVAL_REJECT = {"approval.rejected"}
_RETRY_EXHAUSTED = {"task.retry_exhausted"}


def satisfying_context(transition) -> TransitionContext:
    """Return a context that supplies exactly the facts the row describes."""
    overrides: dict = {}
    if transition.id in _NEVER_DISPATCHED:
        overrides["attempt"] = 0
    if transition.event in _DENY_EVENTS:
        overrides["decision_type"] = "deny"
    if transition.event in _CLARIFY:
        overrides.update(
            decision_type="needs_clarification", missing_items=("required_input_missing",)
        )
    if transition.event in _CONFIRM:
        overrides["decision_type"] = "needs_confirmation"
    if transition.event in _APPROVE:
        overrides["decision_type"] = "needs_approval"
    if transition.event in _CONFIRM_REJECT:
        overrides["confirmation_invalid"] = True
    if transition.event in _APPROVAL_REJECT:
        overrides["approval_invalid"] = True
    if transition.event in _RETRY_EXHAUSTED:
        overrides["retry_exhausted"] = True
    if transition.id == "T33":
        # Rollback to "never accepted": no open attempt remains.
        overrides.update(attempt=0, stop_confirmed=False)
    if transition.id in {"T59", "T60"}:
        overrides["needs_human_origin"] = "execution_boundary"
    if transition.id == "T25":
        overrides["decision_invalidated"] = True
    if transition.id == "T65":
        overrides.update(
            decision_purpose="retry",
            decision_revision=2,
            decision_supersedes="gdec_previous",
        )
    return replace(TransitionContext.permissive(), **overrides)


def test_transition_table_is_complete_and_frozen() -> None:
    assert len(TRANSITIONS) == 72
    assert len({item.id for item in TRANSITIONS}) == 72
    ids = {item.id for item in TRANSITIONS}
    assert "T53" not in ids and "T54" not in ids
    assert ids == {
        f"T{number:02d}"
        for number in range(74)
        if number not in {53, 54}
    }

    # Every state has an inbound edge; every non-terminal state has an outbound
    # edge; every terminal state has none.
    targets = {item.target for item in TRANSITIONS}
    sources = {item.source for item in TRANSITIONS if item.source}
    assert set(TASK_STATES) <= targets
    assert set(TASK_STATES) - TASK_TERMINAL_STATES <= sources
    assert not (set(TASK_STATES) & TASK_TERMINAL_STATES & sources)
    assert TASK_TERMINAL_STATES == {
        "succeeded",
        "denied",
        "cancelled",
        "timed_out",
        "partial_success",
    }
    assert is_terminal("succeeded") and not is_terminal("failed")
    assert not is_terminal("unknown")

    # The event table is the one frozen by §4.
    events = {
        "task.created",
        "task.evaluated",
        "task.queued",
        "task.cancel_requested",
        "task.cancelled",
        "task.deadline_exceeded",
        "task.retry_authorized",
        "task.retry_exhausted",
        "governance.denied",
        "governance.clarification_required",
        "governance.confirmation_required",
        "governance.approval_required",
        "clarification.provided",
        "confirmation.recorded",
        "confirmation.rejected",
        "approval.recorded",
        "approval.rejected",
        "worker.lease_expired",
        "dispatch.accepted",
        "dispatch.rejected",
        "dispatch.unsupported",
        "dispatch.unavailable",
        "dispatch.timeout",
        "dispatch.unknown",
        "dispatch.deadline_exceeded",
        "execution.started",
        "execution.completed",
        "execution.failed",
        "execution.timed_out",
        "execution.lost",
        "execution.stopped",
        "verification.passed",
        "verification.partial",
        "verification.failed",
        "verification.inconclusive",
        "verification.evidence_unavailable",
        "reconcile.running",
        "reconcile.completed",
        "reconcile.failed",
        "reconcile.not_found",
        "reconcile.stopped",
        "reconcile.timed_out",
        "reconcile.unresolved",
        "human.escalation_required",
        "human.takeover.running",
        "human.result.submitted",
        "human.redispatch",
        "human.authorization_revoked",
        "human.stop_confirmed",
        "human.declared_failed",
    }
    assert {item.event for item in TRANSITIONS} <= events
    for transition in TRANSITIONS:
        assert transition.guards, transition.id


@pytest.mark.parametrize("transition", TRANSITIONS, ids=[t.id for t in TRANSITIONS])
def test_every_allowed_transition_accepts_its_guards(transition) -> None:
    checked = check_transition(
        transition.source,
        transition.event,
        transition.target,
        satisfying_context(transition),
    )
    assert checked.id == transition.id


@pytest.mark.parametrize("state", sorted(TASK_TERMINAL_STATES))
def test_terminal_states_are_immutable(state: str) -> None:
    for event, target in (
        ("task.evaluated", "evaluated"),
        ("execution.completed", "verifying"),
        ("human.takeover.running", "running"),
    ):
        with pytest.raises(TaskForbiddenTransition) as excinfo:
            check_transition(
                state, event, target, TransitionContext.permissive()
            )
        assert excinfo.value.context["rule"] in {
            "F05",
            "F06",
            "F08",
            "F11",
            "F12",
            "F13",
            "F14",
        }
    assert all(item.source != state for item in TRANSITIONS)


_EXPLICIT_FORBIDDEN = [
    ("draft", "execution.started", "running", "F01"),
    ("evaluated", "verification.passed", "succeeded", "F01"),
    ("queued", "execution.started", "running", "F02"),
    ("dispatched", "verification.passed", "succeeded", "F03"),
    ("running", "verification.passed", "succeeded", "F04"),
    ("running", "verification.partial", "partial_success", "F04"),
    ("unknown", "verification.passed", "succeeded", "F05"),
    ("cancel_requested", "verification.passed", "succeeded", "F06"),
    ("denied", "task.queued", "queued", "F08"),
    ("denied", "execution.started", "running", "F08"),
    ("awaiting_approval", "task.queued", "queued", "F09"),
    ("awaiting_confirmation", "task.queued", "queued", "F10"),
    ("timed_out", "execution.started", "running", "F11"),
    ("failed", "verification.passed", "succeeded", "F12"),
    ("partial_success", "verification.passed", "succeeded", "F13"),
    ("unknown", "execution.stopped", "cancelled", "F20"),
    ("unknown", "execution.started", "running", "F37"),
    ("unknown", "execution.completed", "verifying", "F38"),
]


@pytest.mark.parametrize(
    "source,event,target,rule", _EXPLICIT_FORBIDDEN,
    ids=[f"{rule}-{event}" for _, event, _, rule in _EXPLICIT_FORBIDDEN],
)
def test_explicitly_forbidden_edges(
    source: str, event: str, target: str, rule: str
) -> None:
    with pytest.raises(TaskForbiddenTransition) as excinfo:
        check_transition(source, event, target, TransitionContext.permissive())
    assert excinfo.value.context["rule"] == rule
    assert excinfo.value.reason_code == "task_forbidden_transition"
    assert find_transition(source, event, target) is None


def test_undeclared_edge_is_invalid_not_forbidden() -> None:
    with pytest.raises(TaskInvalidTransition) as excinfo:
        check_transition(
            "queued", "verification.passed", "verifying",
            TransitionContext.permissive(),
        )
    assert excinfo.value.reason_code == "task_invalid_transition"


def test_unknown_cannot_return_to_running_or_verifying() -> None:
    # The withdrawn ids must not reappear under any name.
    assert find_transition("unknown", "reconcile.running", "running") is None
    assert find_transition("unknown", "reconcile.completed", "verifying") is None
    for transition in TRANSITIONS:
        assert not (
            transition.source == "unknown"
            and transition.target in {"running", "verifying"}
        )


def test_confirmation_guards_require_hash_and_unconsumed_status() -> None:
    t15 = next(item for item in TRANSITIONS if item.id == "T15")
    base = satisfying_context(t15)
    with pytest.raises(TaskError) as excinfo:
        check_transition(
            "awaiting_confirmation", "confirmation.recorded", "evaluated",
            replace(base, confirmation_payload_matches=False),
        )
    assert excinfo.value.reason_code == "confirmation_missing"

    with pytest.raises(TaskError) as excinfo:
        check_transition(
            "awaiting_confirmation", "confirmation.recorded", "evaluated",
            replace(base, confirmation_unconsumed=False),
        )
    assert excinfo.value.reason_code == "confirmation_missing"

    # F10: a confirmation cannot be bypassed to reach queued.
    with pytest.raises(TaskForbiddenTransition) as excinfo:
        check_transition(
            "awaiting_confirmation", "task.queued", "queued", base
        )
    assert excinfo.value.context["rule"] == "F10"


def test_retry_requires_attempt_budget_and_a_fresh_decision() -> None:
    t65 = next(item for item in TRANSITIONS if item.id == "T65")
    base = satisfying_context(t65)
    cases = [
        ({"attempt": 0, "attempt_started": False}, "task_attempt_missing"),
        ({"retry_budget_remaining": 0}, "retry_budget_exhausted"),
        ({"attempt": 3}, "retry_budget_exhausted"),
        (
            {"decision_purpose": "initial_execution", "decision_supersedes": None},
            "task_retry_requires_fresh_authorization",
        ),
        ({"fresh_decision_and_grant": False}, "task_retry_requires_fresh_authorization"),
        ({"safely_retryable": False}, "task_unsafe_retry_forbidden"),
    ]
    for overrides, code in cases:
        with pytest.raises(TaskError) as excinfo:
            check_transition(
                "failed", "task.retry_authorized", "queued",
                replace(base, **overrides),
            )
        assert excinfo.value.reason_code == code


def test_needs_human_from_unknown_cannot_resume_execution_or_verification() -> None:
    t59 = next(item for item in TRANSITIONS if item.id == "T59")
    t60 = next(item for item in TRANSITIONS if item.id == "T60")
    for transition in (t59, t60):
        for origin in ("unknown", "failed"):
            ctx = replace(satisfying_context(transition), needs_human_origin=origin)
            with pytest.raises(TaskError) as excinfo:
                check_transition(
                    "needs_human", transition.event, transition.target, ctx
                )
            assert excinfo.value.reason_code == "grant_revoked"


def test_attempt_zero_human_failure_is_forbidden() -> None:
    t64 = next(item for item in TRANSITIONS if item.id == "T64")
    with pytest.raises(TaskError) as excinfo:
        check_transition(
            "needs_human", "human.declared_failed", "failed",
            replace(satisfying_context(t64), attempt=0),
        )
    assert excinfo.value.reason_code == "task_attempt_missing"


def test_cancelled_requires_real_stop_evidence() -> None:
    t48 = next(item for item in TRANSITIONS if item.id == "T48")
    with pytest.raises(TaskError) as excinfo:
        check_transition(
            "cancel_requested", "execution.stopped", "cancelled",
            replace(satisfying_context(t48), stop_confirmed=False),
        )
    assert excinfo.value.reason_code == "stop_not_confirmed"
    with pytest.raises(TaskError) as excinfo:
        check_transition(
            "cancel_requested", "execution.stopped", "cancelled",
            replace(satisfying_context(t48), stop_evidence=False),
        )
    assert excinfo.value.reason_code == "evidence_missing"


def test_queue_guards_cover_owner_mode_scope_and_criteria() -> None:
    t08 = next(item for item in TRANSITIONS if item.id == "T08")
    base = satisfying_context(t08)
    cases = [
        ({"owner_active": False}, "task_owner_inactive"),
        ({"owner_resolved": False}, "accountable_owner_missing"),
        ({"owner_escalation_present": True}, "accountable_owner_missing"),
        ({"execution_mode": "undetermined"}, "required_input_missing"),
        ({"scope_type": "undetermined"}, "resource_scope_ambiguous"),
        ({"completion_criteria_present": False}, "task_completion_criteria_missing"),
        ({"gates_satisfied": False}, "confirmation_missing"),
        ({"mode_capabilities_ok": False}, "runtime_capability_unverified"),
    ]
    for overrides, code in cases:
        with pytest.raises(TaskError) as excinfo:
            check_transition(
                "evaluated", "task.queued", "queued", replace(base, **overrides)
            )
        assert excinfo.value.reason_code == code
