"""Pure, explicit task state machine (frozen v1.0.0).

The table in :data:`TRANSITIONS` is the *only* place a legal edge is declared;
the service layer may not invent one.  It mirrors
``docs/architecture/task-state-machine.md`` §6 exactly:

* 72 effective transitions T00-T73 (T53/T54 are withdrawn and absent);
* every transition names its frozen event and its guard set;
* terminal states have no outgoing edge; ``failed`` leaves only via T65/T70;
* ``unknown`` never returns directly to ``running``/``verifying`` (F37/F38);
  recovery requires a fresh decision + grant (T71), stop evidence (T55-T58) or
  a human takeover (T57/T72/T73);
* a ``needs_human`` state entered from ``unknown`` or ``failed`` cannot resume
  execution/verification (F41) -- the grant is already revoked/terminal.

This module touches no database and holds no session: guards read a fully
materialised :class:`TransitionContext`, so a state check is a deterministic
pure function of facts the service has already verified.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from app.tasks.errors import TaskError, TaskForbiddenTransition, TaskInvalidTransition
from app.tasks.models import TASK_STATES, TASK_TERMINAL_STATES

__all__ = [
    "FORBIDDEN_RULES",
    "TRANSITIONS",
    "TRANSITION_BY_ID",
    "AllowedTransition",
    "ForbiddenRule",
    "TransitionContext",
    "apply_transition",
    "check_transition",
    "find_transition",
    "is_terminal",
    "terminal_states",
]

# ---------------------------------------------------------------------------
# Transition description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowedTransition:
    """One frozen allowed edge."""

    id: str
    source: str | None
    target: str
    event: str
    guards: tuple[str, ...]
    description: str

    @property
    def key(self) -> tuple[str | None, str, str]:
        return (self.source, self.event, self.target)


@dataclass(frozen=True)
class ForbiddenRule:
    """One explicitly forbidden edge family (F01-F41)."""

    id: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    detail: str
    events: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Guard context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionContext:
    """Facts a guard may read.

    Every field is a *server-verified* fact.  A flag defaults to the safe value
    for that field (false/zero), so forgetting to provide evidence denies rather
    than allows.  ``permissive()`` exists only for table-completeness tests.
    """

    # identity / actor
    actor_verified: bool = False
    actor_active: bool = False
    actor_authorized: bool = False
    actor_is_requester: bool = False
    actor_is_owner: bool = False
    actor_is_executor: bool = False
    actor_is_approver: bool = False

    # optimistic locking
    state_revision: int = 0
    expected_revision: int | None = None

    # record completeness / responsibility
    goal_present: bool = False
    completion_criteria_present: bool = False
    required_inputs_satisfied: bool = True
    missing_items: tuple[str, ...] = ()
    owner_resolved: bool = False
    owner_active: bool = False
    owner_escalation_present: bool = False
    approver_resolved: bool = False
    approver_independent: bool = False
    assignee_present: bool = False
    executor_present: bool = False
    record_completeness: str = "pre_execution_incomplete"
    execution_mode: str = "undetermined"
    scope_type: str = "undetermined"
    mode_capabilities_ok: bool = False

    # decision
    decision_present: bool = False
    decision_type: str | None = None
    decision_purpose: str = "initial_execution"
    decision_revision: int = 1
    decision_supersedes: str | None = None
    decision_task_bound: bool = False
    decision_fresh: bool = False
    decision_invalidated: bool = False
    policy_resolved: bool = False
    fail_closed: bool = False
    clarification_required: bool = False
    confirmation_required: bool = False
    approval_required: bool = False

    # grant
    grant_id: str | None = None
    grant_status: str | None = None
    grant_task_matches: bool = False
    grant_revision_matches: bool = False
    grant_decision_matches: bool = False
    grant_attempt_matches: bool = False
    grant_unexpired: bool = False
    grant_scope_ok: bool = False
    grant_consumed_by_this_attempt: bool = False
    fresh_decision_and_grant: bool = False
    old_grant_invalidated: bool = True

    # gates
    confirmation_valid: bool = False
    confirmation_satisfied: bool = False
    confirmation_payload_matches: bool = False
    confirmation_unexpired: bool = False
    confirmation_unconsumed: bool = False
    confirmation_channel_matches: bool = True
    confirmation_invalid: bool = False
    approval_valid: bool = False
    approval_approved: bool = False
    approval_separation_passed: bool = False
    approval_unexpired: bool = False
    approval_payload_matches: bool = False
    approval_invalid: bool = False
    gates_satisfied: bool = False

    # attempts / stop / retry
    attempt: int = 0
    max_attempts: int = 3
    retry_budget_remaining: int = 0
    attempt_started: bool = False
    attempt_open: bool = False
    active_attempt_unreconciled: bool = False
    stop_confirmed: bool = False
    stop_evidence: bool = False
    no_side_effects: bool = False
    side_effects_reconciled: bool = False
    safely_retryable: bool = True
    retry_exhausted: bool = False
    retry_allowed: bool = False

    # execution facts
    lease_held: bool = True
    fencing_valid: bool = True
    lease_expired_evidence: bool = False
    execution_handle_bound: bool = False
    runtime_supported: bool = True
    loss_evidence: bool = False
    running_evidence: bool = False
    result_present: bool = False
    evidence_present: bool = False

    # verification
    verification_result: str = "pending"
    verification_passed: bool = False
    verification_partial_allowed: bool = False
    verification_inconclusive: bool = False
    evidence_unavailable: bool = False
    owner_accepted_partial: bool = False
    all_criteria_passed: bool = False

    # escalation / recovery
    needs_human_origin: str | None = None
    escalation_justified: bool = False
    same_attempt_running_evidence: bool = False
    attempt_finished_pending_verification: bool = False
    no_side_effect_evidence: bool = False
    deadline_passed: bool = False
    dispatch_outcome_unknown: bool = False
    authorization_revoked: bool = False

    @classmethod
    def permissive(cls) -> TransitionContext:
        """A context that satisfies every guard (table test only).

        It is **not** used by the service: there, each fact is derived from a
        persisted row or a verified caller input.
        """
        return cls(
            actor_verified=True,
            actor_active=True,
            actor_authorized=True,
            actor_is_requester=True,
            goal_present=True,
            completion_criteria_present=True,
            owner_resolved=True,
            owner_active=True,
            approver_resolved=True,
            approver_independent=True,
            assignee_present=True,
            executor_present=True,
            record_completeness="complete",
            execution_mode="advisory",
            scope_type="self",
            mode_capabilities_ok=True,
            decision_present=True,
            decision_type="allow",
            decision_purpose="initial_execution",
            decision_revision=2,
            decision_supersedes="gdec_previous",
            decision_task_bound=True,
            decision_fresh=True,
            policy_resolved=True,
            clarification_required=True,
            confirmation_required=True,
            approval_required=True,
            grant_id="grnt_permissive_0001",
            grant_status="active",
            grant_task_matches=True,
            grant_revision_matches=True,
            grant_decision_matches=True,
            grant_attempt_matches=True,
            grant_unexpired=True,
            grant_scope_ok=True,
            grant_consumed_by_this_attempt=True,
            fresh_decision_and_grant=True,
            confirmation_valid=True,
            confirmation_satisfied=True,
            confirmation_payload_matches=True,
            confirmation_unexpired=True,
            confirmation_unconsumed=True,
            confirmation_invalid=True,
            approval_valid=True,
            approval_approved=True,
            approval_separation_passed=True,
            approval_unexpired=True,
            approval_payload_matches=True,
            approval_invalid=True,
            gates_satisfied=True,
            attempt=1,
            max_attempts=3,
            retry_budget_remaining=1,
            attempt_started=True,
            stop_confirmed=True,
            stop_evidence=True,
            no_side_effects=True,
            side_effects_reconciled=True,
            safely_retryable=True,
            retry_allowed=True,
            lease_held=True,
            fencing_valid=True,
            lease_expired_evidence=True,
            execution_handle_bound=True,
            runtime_supported=True,
            loss_evidence=True,
            running_evidence=True,
            result_present=True,
            evidence_present=True,
            verification_result="passed",
            verification_passed=True,
            verification_partial_allowed=True,
            verification_inconclusive=True,
            evidence_unavailable=True,
            owner_accepted_partial=True,
            all_criteria_passed=True,
            needs_human_origin="execution_boundary",
            escalation_justified=True,
            same_attempt_running_evidence=True,
            attempt_finished_pending_verification=True,
            no_side_effect_evidence=True,
            deadline_passed=True,
            dispatch_outcome_unknown=True,
            authorization_revoked=True,
        )


Guard = Callable[[TransitionContext], str | None]


def _ok(_ctx: TransitionContext) -> str | None:
    return None


def _guard(name: str, code: str, predicate: Callable[[TransitionContext], bool]) -> Guard:
    def check(ctx: TransitionContext) -> str | None:
        return None if predicate(ctx) else code

    check.__name__ = name  # type: ignore[attr-defined]
    return check


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

GUARDS: dict[str, Guard] = {
    "revision_matches": _guard(
        "revision_matches",
        "task_state_revision_conflict",
        lambda c: c.expected_revision is None
        or c.state_revision == c.expected_revision,
    ),
    "actor_verified": _guard(
        "actor_verified", "identity_unverified", lambda c: c.actor_verified
    ),
    "actor_active": _guard(
        "actor_active", "principal_disabled", lambda c: c.actor_active
    ),
    "actor_authorized": _guard(
        "actor_authorized",
        "task_unauthorized_actor",
        lambda c: c.actor_authorized,
    ),
    "cancel_authorized": _guard(
        "cancel_authorized",
        "task_unauthorized_actor",
        lambda c: c.actor_authorized,
    ),
    "goal_present": _guard(
        "goal_present", "goal_missing", lambda c: c.goal_present
    ),
    "completion_criteria_present": _guard(
        "completion_criteria_present",
        "task_completion_criteria_missing",
        lambda c: c.completion_criteria_present,
    ),
    "required_inputs_satisfied": _guard(
        "required_inputs_satisfied",
        "required_input_missing",
        lambda c: c.required_inputs_satisfied,
    ),
    "missing_items_enumerable": _guard(
        "missing_items_enumerable",
        "required_input_missing",
        lambda c: bool(c.missing_items),
    ),
    "owner_resolved": _guard(
        "owner_resolved", "accountable_owner_missing", lambda c: c.owner_resolved
    ),
    "owner_active": _guard(
        "owner_active", "task_owner_inactive", lambda c: c.owner_active
    ),
    "owner_escalation_absent": _guard(
        "owner_escalation_absent",
        "accountable_owner_missing",
        lambda c: not c.owner_escalation_present,
    ),
    "assignee_present": _guard(
        "assignee_present", "accountable_owner_unresolved", lambda c: c.assignee_present
    ),
    "executor_present": _guard(
        "executor_present", "accountable_owner_unresolved", lambda c: c.executor_present
    ),
    "approver_resolved_independent": _guard(
        "approver_resolved_independent",
        "accountable_owner_unresolved",
        lambda c: c.approver_resolved and c.approver_independent,
    ),
    "record_complete": _guard(
        "record_complete",
        "required_input_missing",
        lambda c: c.record_completeness == "complete",
    ),
    "mode_determined": _guard(
        "mode_determined",
        "required_input_missing",
        lambda c: c.execution_mode != "undetermined",
    ),
    "scope_determined": _guard(
        "scope_determined",
        "resource_scope_ambiguous",
        lambda c: c.scope_type != "undetermined",
    ),
    "mode_capabilities": _guard(
        "mode_capabilities",
        "runtime_capability_unverified",
        lambda c: c.mode_capabilities_ok,
    ),
    "policy_resolved": _guard(
        "policy_resolved", "policy_unavailable", lambda c: c.policy_resolved
    ),
    "decision_present": _guard(
        "decision_present", "required_input_missing", lambda c: c.decision_present
    ),
    "decision_is_deny": _guard(
        "decision_is_deny",
        "policy_denied",
        lambda c: c.decision_type == "deny" or c.fail_closed,
    ),
    "decision_needs_clarification": _guard(
        "decision_needs_clarification",
        "required_input_missing",
        lambda c: c.decision_type == "needs_clarification",
    ),
    "decision_needs_confirmation": _guard(
        "decision_needs_confirmation",
        "confirmation_required",
        lambda c: c.decision_type == "needs_confirmation",
    ),
    "decision_needs_approval": _guard(
        "decision_needs_approval",
        "approval_required",
        lambda c: c.decision_type == "needs_approval",
    ),
    "decision_authorizes": _guard(
        "decision_authorizes",
        "capability_not_granted",
        lambda c: c.decision_type in {"allow", "managed_execution_required"},
    ),
    "decision_task_bound": _guard(
        "decision_task_bound", "grant_scope_mismatch", lambda c: c.decision_task_bound
    ),
    "decision_fresh": _guard(
        "decision_fresh", "decision_expired", lambda c: c.decision_fresh
    ),
    "decision_is_retry": _guard(
        "decision_is_retry",
        "task_retry_requires_fresh_authorization",
        lambda c: c.decision_purpose == "retry"
        and c.decision_revision >= 2
        and bool(c.decision_supersedes),
    ),
    "decision_invalidated": _guard(
        "decision_invalidated",
        "decision_revision_stale",
        lambda c: c.decision_invalidated,
    ),
    "grant_active_for_queue": _guard(
        "grant_active_for_queue",
        "grant_missing",
        lambda c: bool(c.grant_id)
        and c.grant_status == "active"
        and c.grant_unexpired
        and c.grant_task_matches
        and c.grant_revision_matches
        and c.grant_decision_matches
        and c.grant_scope_ok,
    ),
    "grant_active_for_dispatch": _guard(
        "grant_active_for_dispatch",
        "grant_revoked",
        lambda c: bool(c.grant_id)
        and c.grant_status == "active"
        and c.grant_unexpired
        and c.grant_task_matches
        and c.grant_revision_matches
        and c.grant_attempt_matches
        and c.grant_scope_ok,
    ),
    "grant_usable_during_execution": _guard(
        "grant_usable_during_execution",
        "grant_revoked",
        lambda c: bool(c.grant_id)
        and c.grant_unexpired
        and c.grant_task_matches
        and c.grant_scope_ok
        and (
            c.grant_status == "active"
            or (c.grant_status == "consumed" and c.grant_consumed_by_this_attempt)
        ),
    ),
    "gates_satisfied": _guard(
        "gates_satisfied", "confirmation_missing", lambda c: c.gates_satisfied
    ),
    "confirmation_valid": _guard(
        "confirmation_valid",
        "confirmation_missing",
        lambda c: c.confirmation_valid
        and c.confirmation_satisfied
        and c.confirmation_payload_matches
        and c.confirmation_unexpired
        and c.confirmation_unconsumed
        and c.confirmation_channel_matches,
    ),
    "confirmation_invalid": _guard(
        "confirmation_invalid",
        "confirmation_missing",
        lambda c: c.confirmation_invalid,
    ),
    "approval_valid": _guard(
        "approval_valid",
        "approval_missing",
        lambda c: c.approval_valid
        and c.approval_approved
        and c.approval_separation_passed
        and c.approval_unexpired
        and c.approval_payload_matches,
    ),
    "approval_invalid": _guard(
        "approval_invalid", "approval_missing", lambda c: c.approval_invalid
    ),
    "attempt_started": _guard(
        "attempt_started",
        "task_attempt_missing",
        lambda c: c.attempt_started and c.attempt >= 1,
    ),
    "attempt_within_max": _guard(
        "attempt_within_max",
        "retry_budget_exhausted",
        lambda c: c.attempt < c.max_attempts,
    ),
    "retry_budget_available": _guard(
        "retry_budget_available",
        "retry_budget_exhausted",
        lambda c: c.retry_budget_remaining > 0,
    ),
    "retry_exhausted": _guard(
        "retry_exhausted", "retry_budget_exhausted", lambda c: c.retry_exhausted
    ),
    "safely_retryable": _guard(
        "safely_retryable",
        "task_unsafe_retry_forbidden",
        lambda c: c.safely_retryable,
    ),
    "stop_confirmed": _guard(
        "stop_confirmed", "stop_not_confirmed", lambda c: c.stop_confirmed
    ),
    "stop_evidence": _guard(
        "stop_evidence", "evidence_missing", lambda c: c.stop_evidence
    ),
    "no_side_effects": _guard(
        "no_side_effects", "task_reconcile_evidence_missing", lambda c: c.no_side_effects
    ),
    "side_effects_reconciled": _guard(
        "side_effects_reconciled",
        "task_reconcile_evidence_missing",
        lambda c: c.side_effects_reconciled,
    ),
    "active_attempt_reconciled": _guard(
        "active_attempt_reconciled",
        "task_active_attempt_unreconciled",
        lambda c: not c.active_attempt_unreconciled,
    ),
    "lease_held": _guard(
        "lease_held", "task_lease_not_held", lambda c: c.lease_held
    ),
    "fencing_valid": _guard(
        "fencing_valid", "task_fencing_token_stale", lambda c: c.fencing_valid
    ),
    "lease_expired_evidence": _guard(
        "lease_expired_evidence", "task_lease_expired", lambda c: c.lease_expired_evidence
    ),
    "execution_handle_bound": _guard(
        "execution_handle_bound",
        "task_execution_handle_missing",
        lambda c: c.execution_handle_bound,
    ),
    "runtime_supported": _guard(
        "runtime_supported",
        "runtime_capability_unverified",
        lambda c: c.runtime_supported,
    ),
    "loss_evidence": _guard(
        "loss_evidence", "task_reconcile_evidence_missing", lambda c: c.loss_evidence
    ),
    "running_evidence": _guard(
        "running_evidence", "evidence_missing", lambda c: c.running_evidence
    ),
    "result_present": _guard(
        "result_present", "task_result_missing", lambda c: c.result_present
    ),
    "evidence_present": _guard(
        "evidence_present", "evidence_missing", lambda c: c.evidence_present
    ),
    "verification_passed": _guard(
        "verification_passed",
        "task_completion_evidence_incomplete",
        lambda c: c.verification_passed and c.all_criteria_passed,
    ),
    "verification_partial_allowed": _guard(
        "verification_partial_allowed",
        "task_verification_inconclusive",
        lambda c: c.verification_partial_allowed and c.owner_accepted_partial,
    ),
    "verification_inconclusive": _guard(
        "verification_inconclusive",
        "task_verification_inconclusive",
        lambda c: c.verification_inconclusive,
    ),
    "evidence_unavailable": _guard(
        "evidence_unavailable",
        "evidence_missing",
        lambda c: c.evidence_unavailable,
    ),
    "escalation_justified": _guard(
        "escalation_justified",
        "required_input_missing",
        lambda c: c.escalation_justified,
    ),
    "resumable_origin": _guard(
        "resumable_origin",
        "grant_revoked",
        lambda c: c.needs_human_origin not in {"unknown", "failed"},
    ),
    "same_attempt_running_evidence": _guard(
        "same_attempt_running_evidence",
        "evidence_missing",
        lambda c: c.same_attempt_running_evidence,
    ),
    "attempt_finished_pending_verification": _guard(
        "attempt_finished_pending_verification",
        "evidence_missing",
        lambda c: c.attempt_finished_pending_verification,
    ),
    "stop_or_never_started": _guard(
        "stop_or_never_started",
        "stop_not_confirmed",
        lambda c: c.attempt == 0 or c.stop_confirmed,
    ),
    "fresh_decision_and_grant": _guard(
        "fresh_decision_and_grant",
        "task_retry_requires_fresh_authorization",
        lambda c: c.fresh_decision_and_grant and c.old_grant_invalidated,
    ),
    "no_side_effect_evidence": _guard(
        "no_side_effect_evidence",
        "task_reconcile_evidence_missing",
        lambda c: c.no_side_effect_evidence,
    ),
    "deadline_passed": _guard(
        "deadline_passed", "required_input_missing", lambda c: c.deadline_passed
    ),
    "dispatch_outcome_unknown": _guard(
        "dispatch_outcome_unknown",
        "task_reconcile_evidence_missing",
        lambda c: c.dispatch_outcome_unknown,
    ),
    "authorization_revoked": _guard(
        "authorization_revoked", "grant_revoked", lambda c: c.authorization_revoked
    ),
    "never_dispatched": _guard(
        "never_dispatched", "task_attempt_missing", lambda c: c.attempt == 0
    ),
}


# ``all_criteria_passed`` is also part of ``verification_passed``; the explicit
# alias keeps T42's guard list readable and independently testable.
GUARDS["all_criteria_passed_guard"] = _guard(
    "all_criteria_passed_guard",
    "task_completion_evidence_incomplete",
    lambda c: c.all_criteria_passed,
)


def _t(
    identifier: str,
    source: str | None,
    target: str,
    event: str,
    guards: tuple[str, ...],
    description: str,
) -> AllowedTransition:
    unknown = [guard for guard in guards if guard not in GUARDS]
    if unknown:  # pragma: no cover - import-time programming error
        raise AssertionError(f"unknown guard(s) on {identifier}: {unknown}")
    return AllowedTransition(identifier, source, target, event, guards, description)


# ---------------------------------------------------------------------------
# The frozen transition table (72 effective edges)
# ---------------------------------------------------------------------------

TRANSITIONS: tuple[AllowedTransition, ...] = (
    # 6.1 initial creation
    _t("T00", None, "draft", "task.created",
       ("actor_verified", "actor_active"), "create the draft task"),
    # 6.2 from draft
    _t("T01", "draft", "evaluated", "task.evaluated",
       ("actor_verified", "actor_active", "revision_matches", "decision_present",
        "policy_resolved", "decision_task_bound"),
       "persist and apply the PDP decision"),
    _t("T02", "draft", "denied", "governance.denied",
       ("decision_is_deny",), "fail-closed denial during evaluation"),
    _t("T03", "draft", "cancelled", "task.cancelled",
       ("cancel_authorized", "never_dispatched", "no_side_effects"),
       "cancel before dispatch; confirmed never started"),
    # 6.3 from evaluated
    _t("T04", "evaluated", "awaiting_clarification",
       "governance.clarification_required",
       ("decision_needs_clarification", "missing_items_enumerable"),
       "the decision enumerates missing inputs"),
    _t("T05", "evaluated", "awaiting_confirmation",
       "governance.confirmation_required",
       ("decision_needs_confirmation", "owner_resolved", "owner_active",
        "record_complete"),
       "a payload-bound confirmation is required"),
    _t("T06", "evaluated", "awaiting_approval", "governance.approval_required",
       ("decision_needs_approval", "approver_resolved_independent",
        "owner_resolved", "owner_active", "record_complete"),
       "an independent approver is required"),
    _t("T07", "evaluated", "denied", "governance.denied",
       ("decision_is_deny",), "the PDP denied the request"),
    _t("T08", "evaluated", "queued", "task.queued",
       ("decision_authorizes", "decision_task_bound", "grant_active_for_queue",
        "gates_satisfied", "owner_resolved", "owner_active",
        "owner_escalation_absent", "assignee_present", "executor_present",
        "record_complete", "mode_determined", "scope_determined",
        "mode_capabilities", "completion_criteria_present"),
       "authorised, complete and ready for a worker"),
    _t("T09", "evaluated", "cancel_requested", "task.cancel_requested",
       ("cancel_authorized",), "cancel requested before queueing"),
    _t("T10", "evaluated", "needs_human", "human.escalation_required",
       ("escalation_justified",), "responsibility/policy conflict needs a human"),
    # 6.4 from awaiting_clarification
    _t("T11", "awaiting_clarification", "evaluated", "clarification.provided",
       ("actor_verified", "actor_active", "required_inputs_satisfied",
        "decision_present", "decision_fresh", "decision_task_bound"),
       "new input is complete; re-run the PDP"),
    _t("T12", "awaiting_clarification", "denied", "governance.denied",
       ("decision_is_deny",), "clarification window expired or withdrawn"),
    _t("T13", "awaiting_clarification", "cancelled", "task.cancelled",
       ("cancel_authorized", "never_dispatched", "no_side_effects"),
       "requester cancels before any execution"),
    _t("T14", "awaiting_clarification", "needs_human",
       "human.escalation_required",
       ("escalation_justified",), "clarification responsibility is unresolved"),
    # 6.5 from awaiting_confirmation
    _t("T15", "awaiting_confirmation", "evaluated", "confirmation.recorded",
       ("actor_verified", "actor_active", "confirmation_valid",
        "decision_present", "decision_fresh", "decision_task_bound"),
       "valid confirmation; re-run the PDP"),
    _t("T16", "awaiting_confirmation", "denied", "confirmation.rejected",
       ("confirmation_invalid",), "confirmation rejected, expired or changed"),
    _t("T17", "awaiting_confirmation", "cancelled", "task.cancelled",
       ("cancel_authorized", "never_dispatched", "no_side_effects"),
       "requester cancels before any execution"),
    _t("T18", "awaiting_confirmation", "needs_human",
       "human.escalation_required",
       ("escalation_justified",), "no eligible confirmer or conflict"),
    # 6.6 from awaiting_approval
    _t("T19", "awaiting_approval", "evaluated", "approval.recorded",
       ("actor_verified", "actor_active", "approval_valid",
        "approver_resolved_independent", "decision_present", "decision_fresh",
        "decision_task_bound"),
       "independent approval recorded; re-run the PDP"),
    _t("T20", "awaiting_approval", "denied", "approval.rejected",
       ("approval_invalid",), "approval rejected, expired, revoked or self-dealt"),
    _t("T21", "awaiting_approval", "cancelled", "task.cancelled",
       ("cancel_authorized", "never_dispatched", "no_side_effects"),
       "requester cancels before any execution"),
    _t("T22", "awaiting_approval", "needs_human", "human.escalation_required",
       ("escalation_justified",), "approval chain conflict or unavailability"),
    # 6.7 from queued
    _t("T23", "queued", "dispatched", "dispatch.accepted",
       ("lease_held", "fencing_valid", "grant_active_for_dispatch",
        "gates_satisfied", "active_attempt_reconciled", "mode_capabilities",
        "execution_handle_bound", "attempt_within_max"),
       "the execution boundary accepted the submission"),
    _t("T24", "queued", "cancel_requested", "task.cancel_requested",
       ("cancel_authorized",), "cancel requested before dispatch"),
    _t("T25", "queued", "denied", "governance.denied",
       ("decision_invalidated",), "authority was withdrawn while queued"),
    _t("T26", "queued", "needs_human", "human.escalation_required",
       ("escalation_justified",), "no eligible executor or capability downgrade"),
    _t("T27", "queued", "timed_out", "task.deadline_exceeded",
       ("deadline_passed", "never_dispatched", "no_side_effects",
        "stop_confirmed", "stop_evidence"),
       "queue deadline passed and nothing was dispatched"),
    _t("T28", "queued", "failed", "dispatch.unavailable",
       ("never_dispatched", "no_side_effects"), "no adapter/config available"),
    _t("T66", "queued", "failed", "dispatch.rejected",
       ("never_dispatched", "no_side_effects"), "submission was explicitly rejected"),
    _t("T67", "queued", "unknown", "dispatch.timeout",
       ("dispatch_outcome_unknown",), "submission timed out; outcome unknown"),
    _t("T68", "queued", "unknown", "dispatch.unknown",
       ("dispatch_outcome_unknown",), "submission response unreadable/interrupted"),
    _t("T69", "queued", "needs_human", "dispatch.unsupported",
       ("escalation_justified",),
       "runtime/connector does not support the mode"),
    # 6.8 from dispatched
    _t("T29", "dispatched", "running", "execution.started",
       ("execution_handle_bound", "grant_usable_during_execution",
        "attempt_started", "running_evidence"),
       "the boundary reports started/running"),
    _t("T30", "dispatched", "failed", "execution.failed",
       ("attempt_started", "stop_confirmed", "stop_evidence"),
       "the boundary reports failure and confirmed stop"),
    _t("T31", "dispatched", "unknown", "execution.lost",
       ("loss_evidence",), "status unknown; enter reconciliation"),
    _t("T32", "dispatched", "cancel_requested", "task.cancel_requested",
       ("cancel_authorized",), "cancel registered; stop not yet confirmed"),
    _t("T33", "dispatched", "queued", "worker.lease_expired",
       ("lease_expired_evidence", "no_side_effects", "stop_or_never_started"),
       "accepted submission was voided; attempt rolled back, no side effects"),
    _t("T34", "dispatched", "timed_out", "dispatch.deadline_exceeded",
       ("deadline_passed", "no_side_effects", "stop_confirmed",
        "stop_evidence"),
       "submission window closed and nothing was accepted"),
    _t("T35", "dispatched", "needs_human", "human.escalation_required",
       ("escalation_justified",), "contradictory acceptance facts"),
    # 6.9 from running
    _t("T36", "running", "verifying", "execution.completed",
       ("result_present", "evidence_present"), "execution ended; verify results"),
    _t("T37", "running", "failed", "execution.failed",
       ("stop_confirmed", "stop_evidence"), "execution failed and stopped"),
    _t("T38", "running", "timed_out", "execution.timed_out",
       ("stop_confirmed", "stop_evidence"), "execution timed out and stopped"),
    _t("T39", "running", "cancel_requested", "task.cancel_requested",
       ("cancel_authorized",), "cancel registered; not yet stopped"),
    _t("T40", "running", "needs_human", "human.escalation_required",
       ("escalation_justified",), "execution needs a human decision"),
    _t("T41", "running", "unknown", "execution.lost",
       ("loss_evidence",), "heartbeat/query threshold exceeded"),
    # 6.10 from verifying
    _t("T42", "verifying", "succeeded", "verification.passed",
       ("verification_passed", "all_criteria_passed_guard",
        "result_present", "evidence_present"),
       "all structured criteria passed"),
    _t("T43", "verifying", "partial_success", "verification.partial",
       ("verification_partial_allowed", "evidence_present"),
       "only a policy-allowed subset passed"),
    _t("T44", "verifying", "failed", "verification.failed",
       ("result_present", "evidence_present"), "verification failed"),
    _t("T45", "verifying", "needs_human", "verification.inconclusive",
       ("verification_inconclusive",), "evidence is missing or contradictory"),
    _t("T46", "verifying", "unknown", "verification.evidence_unavailable",
       ("evidence_unavailable",), "evidence source unavailable"),
    _t("T47", "verifying", "cancel_requested", "task.cancel_requested",
       ("cancel_authorized",), "cancel requested after execution"),
    # 6.11 from cancel_requested
    _t("T48", "cancel_requested", "cancelled", "execution.stopped",
       ("stop_confirmed", "stop_evidence", "side_effects_reconciled"),
       "stop confirmed and side effects reconciled"),
    _t("T49", "cancel_requested", "running", "execution.started",
       ("running_evidence", "grant_usable_during_execution",
        "attempt_started"),
       "fresh evidence that execution is still running"),
    _t("T50", "cancel_requested", "verifying", "execution.completed",
       ("result_present", "evidence_present"),
       "execution completed before cancellation took effect"),
    _t("T51", "cancel_requested", "unknown", "execution.lost",
       ("loss_evidence",), "stop state cannot be confirmed"),
    _t("T52", "cancel_requested", "needs_human", "human.escalation_required",
       ("escalation_justified",), "stop unconfirmed beyond threshold"),
    # 6.12 from unknown -- T53/T54 are withdrawn and must not exist
    _t("T55", "unknown", "failed", "reconcile.failed",
       ("stop_confirmed", "stop_evidence"),
       "reconciliation proved failure and stop"),
    _t("T56", "unknown", "cancelled", "reconcile.stopped",
       ("stop_confirmed", "stop_evidence"), "reconciliation proved stop"),
    _t("T57", "unknown", "needs_human", "reconcile.unresolved",
       ("escalation_justified",), "reconciliation exhausted"),
    _t("T58", "unknown", "timed_out", "reconcile.timed_out",
       ("stop_confirmed", "stop_evidence"), "reconciliation proved timeout"),
    _t("T71", "unknown", "queued", "reconcile.not_found",
       ("never_dispatched", "no_side_effect_evidence", "fresh_decision_and_grant",
        "gates_satisfied", "owner_active", "record_complete"),
       "proved never accepted; fresh decision + grant requeue"),
    _t("T72", "unknown", "needs_human", "reconcile.running",
       ("escalation_justified",), "still running; stop and hand to a human"),
    _t("T73", "unknown", "needs_human", "reconcile.completed",
       ("escalation_justified",), "result exists but grant is revoked; human decides"),
    # 6.13 from needs_human
    _t("T59", "needs_human", "running", "human.takeover.running",
       ("resumable_origin", "attempt_started", "grant_usable_during_execution",
        "same_attempt_running_evidence"),
       "a human proved the same attempt is still running"),
    _t("T60", "needs_human", "verifying", "human.result.submitted",
       ("resumable_origin", "attempt_started", "grant_usable_during_execution",
        "attempt_finished_pending_verification", "result_present",
        "evidence_present"),
       "a human submitted structured results for an ended attempt"),
    _t("T61", "needs_human", "queued", "human.redispatch",
       ("active_attempt_reconciled", "stop_or_never_started",
        "fresh_decision_and_grant", "retry_budget_available", "no_side_effects",
        "gates_satisfied", "owner_active", "record_complete"),
       "human redispatch with a fresh decision and grant"),
    _t("T62", "needs_human", "denied", "human.authorization_revoked",
       ("active_attempt_reconciled", "stop_or_never_started",
        "authorization_revoked"),
       "authority revoked; no unreconciled attempt"),
    _t("T63", "needs_human", "cancelled", "human.stop_confirmed",
       ("stop_confirmed", "stop_evidence"), "a human confirmed the stop"),
    _t("T64", "needs_human", "failed", "human.declared_failed",
       ("attempt_started",), "a human declared failure (attempt >= 1 only)"),
    # 6.14 from failed
    _t("T65", "failed", "queued", "task.retry_authorized",
       ("attempt_started", "attempt_within_max", "retry_budget_available",
        "safely_retryable", "decision_is_retry", "fresh_decision_and_grant",
        "no_side_effects", "gates_satisfied", "owner_active",
        "record_complete"),
       "bounded retry with a fresh decision, grant and budget decrement"),
    _t("T70", "failed", "needs_human", "task.retry_exhausted",
       ("retry_exhausted", "escalation_justified"),
       "retry exhausted or the action is not safely retryable"),
)


TRANSITION_BY_ID: Final[dict[str, AllowedTransition]] = {
    transition.id: transition for transition in TRANSITIONS
}


# ---------------------------------------------------------------------------
# Explicitly forbidden edges
# ---------------------------------------------------------------------------

FORBIDDEN_RULES: tuple[ForbiddenRule, ...] = (
    ForbiddenRule("F01", ("draft", "evaluated"),
                  ("running", "succeeded"),
                  "skip evaluation, dispatch and verification"),
    ForbiddenRule("F02", ("queued",), ("running", "succeeded"),
                  "skip dispatch and execution facts"),
    ForbiddenRule("F03", ("dispatched",), ("succeeded",),
                  "skip running and verification"),
    ForbiddenRule("F04", ("running",), ("succeeded", "partial_success"),
                  "skip verifying and the evidence check"),
    ForbiddenRule("F05", ("unknown",), ("succeeded",),
                  "declare success before reconciliation"),
    ForbiddenRule("F06", ("cancel_requested",), ("succeeded",),
                  "must pass verifying and must not drop a finished result"),
    ForbiddenRule("F08", ("denied",), ("queued", "dispatched", "running"),
                  "priority, retry or verbal instruction cannot override a deny"),
    ForbiddenRule("F09", ("awaiting_approval",), ("queued",),
                  "skip or forge approval"),
    ForbiddenRule("F10", ("awaiting_confirmation",), ("queued",),
                  "skip confirmation or replay it"),
    ForbiddenRule("F11", ("timed_out",), ("running",),
                  "a closed execution boundary is not resurrected"),
    ForbiddenRule("F12", ("failed",), ("succeeded",),
                  "skip verification and evidence"),
    ForbiddenRule("F13", ("partial_success",), ("succeeded",),
                  "silently completing unfinished work"),
    ForbiddenRule("F20", ("unknown",), ("cancelled", "timed_out"),
                  "unknown is not 'stopped'"),
    ForbiddenRule("F37", ("unknown",), ("running",),
                  "T53 withdrawn: a revoked grant never resumes execution"),
    ForbiddenRule("F38", ("unknown",), ("verifying",),
                  "T54 withdrawn: reconciliation results go to a human (T73)"),
)


def is_terminal(state: str) -> bool:
    """Return whether ``state`` is a task terminal state."""
    return state in TASK_TERMINAL_STATES


def terminal_states() -> frozenset[str]:
    return TASK_TERMINAL_STATES


def find_transition(
    source: str | None, event: str, target: str
) -> AllowedTransition | None:
    """Return the declared transition, or ``None``."""
    for transition in TRANSITIONS:
        if (
            transition.source == source
            and transition.event == event
            and transition.target == target
        ):
            return transition
    return None


def _find_forbidden(
    source: str, event: str, target: str
) -> ForbiddenRule | None:
    for rule in FORBIDDEN_RULES:
        if source in rule.sources and target in rule.targets:
            if rule.events and event not in rule.events:
                continue
            return rule
    return None


def check_transition(
    source: str | None,
    event: str,
    target: str,
    ctx: TransitionContext,
) -> AllowedTransition:
    """Validate one edge and all of its guards.

    Raises :class:`TaskForbiddenTransition` for an explicitly forbidden family
    (F01-F41) and :class:`TaskInvalidTransition` for any undeclared edge.  A
    guard failure raises :class:`TaskError` with the guard's stable reason code.
    """
    if source is not None and source not in TASK_STATES:
        raise TaskInvalidTransition(
            from_state=source, event=event, to_state=target
        )
    if target not in TASK_STATES:
        raise TaskInvalidTransition(
            from_state=source or "(none)", event=event, to_state=target
        )

    # A declared edge always wins: ``unknown -> cancelled`` is legal through
    # T56, even though ``F20`` forbids inventing a cancellation from ``unknown``.
    transition = find_transition(source, event, target)

    # Explicitly forbidden families are checked before the generic F14 terminal
    # rule, so the audit records the precise reason (F05/F06/F08/F11/F12/F13).
    if transition is None:
        forbidden = _find_forbidden(source or "", event, target)
        if forbidden is not None:
            raise TaskForbiddenTransition(
                from_state=source or "(none)",
                event=event,
                to_state=target,
                rule=forbidden.id,
                detail=forbidden.detail,
            )

    # F14: task terminals have no outgoing edge at all.
    if transition is None and source in TASK_TERMINAL_STATES:
        raise TaskForbiddenTransition(
            from_state=source,
            event=event,
            to_state=target,
            rule="F14",
            detail=(
                "a terminal task cannot be rewritten; create a new task and "
                "link it with parent/supersedes"
            ),
        )

    if transition is None:
        raise TaskInvalidTransition(
            from_state=source or "(none)", event=event, to_state=target
        )

    for guard_name in transition.guards:
        reason = GUARDS[guard_name](ctx)
        if reason is not None:
            raise TaskError(
                reason,
                f"guard {guard_name!r} rejected {transition.id}",
                context={
                    "transition": transition.id,
                    "guard": guard_name,
                    "from_state": source,
                    "to_state": target,
                    "event": event,
                },
            )
    return transition


def apply_transition(task: object, transition: AllowedTransition) -> None:
    """Apply a validated transition to an ORM task and increment the revision.

    The caller must have called :func:`check_transition` first.  Only the two
    lifecycle columns are touched here; events/audit/outbox are the service
    layer's responsibility so they share the same transaction.
    """
    task.state = transition.target  # type: ignore[attr-defined]
    task.state_revision = int(task.state_revision) + 1  # type: ignore[attr-defined]


def reviewed_transition(identifier: str) -> AllowedTransition:
    """Return a transition by id (raises ``KeyError`` when absent)."""
    return TRANSITION_BY_ID[identifier]


def with_revision(ctx: TransitionContext, revision: int) -> TransitionContext:
    """Convenience for tests: bind the expected revision."""
    return replace(ctx, state_revision=revision, expected_revision=revision)
