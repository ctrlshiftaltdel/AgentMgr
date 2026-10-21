"""Task lifecycle service: submission, responsibility, gates, attempts, recovery.

Every public function is deterministic, never commits and never writes state
outside the frozen transition table.  A mutation follows the same shape:

1. load the task ``FOR UPDATE`` (or fail with ``task_not_found``);
2. refuse any write on a terminal task;
3. validate caller authority and gather *verified* facts;
4. run the pure state machine (:mod:`app.tasks.state_machine`);
5. in **one** transaction: increment ``state_revision``, append the task event,
   append an audit event and enqueue the matching transactional Outbox event.

Responsibilities are distinct columns: ``requester``, ``accountable owner``,
``assignee``, ``executor`` and ``approver``.  Missing owners are rejected, and
self-approval is refused by comparing the *actual* approving principal with the
requester/owner/executor -- not by trusting a task-level field.

Approval reuses ``workflow_instances`` / ``approval_steps`` through
:mod:`app.workflow.service`; confirmations are task-owned but their hash is the
RFC 8785 JCS hash of the execution payload.  Execution authorisation is
re-checked with the governance PEP (:func:`app.governance.pep.enforce_tool_call`)
immediately before a call and the task confirmation is consumed at most once.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.db.models.organization import Employee
from app.governance.errors import GovernanceError
from app.governance.grants import get_grant, issue_grant, revoke_grant
from app.governance.models import ExecutionGrant, GovernanceDecisionRecord
from app.governance.pdp import (
    DEFAULT_POLICY_SET_ID,
    EvaluationRequest,
    GovernanceDecision,
    evaluate,
)
from app.governance.pep import EnforcementResult, enforce_tool_call
from app.governance.policies import payload_hash, validate_resource_scope
from app.governance.principal import (
    GovernancePrincipal,
    audit_actor_id,
    resolve_principal,
)
from app.tasks import priority as scheduling
from app.tasks import verification
from app.tasks.errors import (
    ConfirmationError,
    DependencyError,
    IdempotencyConflict,
    NecessityDecision,
    OwnerError,
    TaskError,
    TaskNotFound,
    TaskStateConflict,
    VerificationError,
)
from app.tasks.models import (
    TASK_TERMINAL_STATES,
    Task,
    TaskAttempt,
    TaskConfirmation,
    TaskDependency,
    TaskEvent,
    TaskLease,
)
from app.tasks.state_machine import (
    AllowedTransition,
    TransitionContext,
    apply_transition,
    check_transition,
    is_terminal,
)
from app.tasks.verification import EvidenceRecord, VerificationOutcome
from app.workflow.models import ApprovalStep, WorkflowInstance
from app.workflow.service import (
    StepAlreadyDecidedError,
    enqueue_event,
    record_decision,
    start_approval,
)

__all__ = [
    "APPROVAL_MAX_TTL_SECONDS",
    "APPROVAL_TTL_SECONDS",
    "CONFIRMATION_MAX_TTL_SECONDS",
    "CONFIRMATION_TTL_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "NecessityResult",
    "TaskEvidenceVerifier",
    "add_dependency",
    "assign_task",
    "classify_necessity",
    "complete_task",
    "confirm_stop",
    "confirm_task",
    "consume_confirmation",
    "create_confirmation",
    "deny_task",
    "dependency_blockers",
    "dispatch_deadline_exceeded",
    "escalate_owner_account",
    "escalate_task",
    "evidence_unavailable",
    "fail_task",
    "mark_running",
    "provide_clarification",
    "recheck_execution",
    "reconcile_task",
    "record_approval",
    "record_attempt_result",
    "record_dispatch_failure",
    "request_cancel",
    "resolve_owner_escalation",
    "retry_task",
    "revoke_approval",
    "satisfy_confirmation",
    "start_attempt",
    "submit_task",
    "takeover_task",
]

CONFIRMATION_TTL_SECONDS = 900
APPROVAL_TTL_SECONDS = 3600
#: Hard upper bound for a gate TTL; a caller cannot mint an effectively
#: permanent confirmation/approval.
CONFIRMATION_MAX_TTL_SECONDS = 86400
APPROVAL_MAX_TTL_SECONDS = 86400
DEFAULT_LEASE_SECONDS = 300
_EVIDENCE_NEVER_STARTED = "evidence:task_never_dispatched"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _audit(
    session: Session,
    *,
    actor: GovernancePrincipal | None,
    action: str,
    task: Task,
    details: Mapping[str, Any] | None = None,
) -> None:
    record_event(
        session,
        actor_id=audit_actor_id(actor) if actor is not None else None,
        action=action,
        resource_type="task",
        resource_id=task.task_id,
        details=dict(details or {}),
    )


def _reject_terminal(
    session: Session,
    task: Task,
    actor: GovernancePrincipal | None,
    *,
    event: str,
    target: str,
    message: str,
) -> None:
    """Audit and reject an attempted write on a terminal task."""
    _audit(
        session,
        actor=actor,
        action="task.transition.rejected",
        task=task,
        details={
            "from_state": task.state,
            "event": event,
            "to_state": target,
            "reason_code": "task_terminal_state_immutable",
        },
    )
    raise TaskError(
        "task_terminal_state_immutable",
        message,
        context={
            "task_id": task.task_id,
            "state": task.state,
            "event": event,
            "to_state": target,
        },
    )


def _load_task(
    session: Session, task_id: str, *, for_update: bool = False
) -> Task:
    stmt = select(Task).where(Task.task_id == task_id)
    if for_update:
        stmt = stmt.with_for_update()
    task = session.scalar(stmt)
    if task is None:
        raise TaskNotFound(task_id)
    return task


def _is_privileged(actor: GovernancePrincipal) -> bool:
    return ("identity", "manage", "company") in actor.effective_permissions


def _is_participant(actor: GovernancePrincipal, task: Task) -> bool:
    return actor.principal_id in {
        task.requester_principal_id,
        task.owner_principal_id,
        task.assignee_principal_id,
        task.executor_principal_id,
        task.approver_principal_id,
    }


def _actor_authorized(actor: GovernancePrincipal, task: Task) -> bool:
    if actor.principal_type in {"service", "runtime"}:
        return True
    return _is_privileged(actor) or _is_participant(actor, task)


def _require_actor(actor: GovernancePrincipal) -> GovernancePrincipal:
    actor.require_active()
    return actor


def _require_human(actor: GovernancePrincipal) -> GovernancePrincipal:
    _require_actor(actor)
    if actor.principal_type != "person":
        raise TaskError(
            "task_unauthorized_actor",
            "this action is reserved for a human principal",
            context={"principal_type": actor.principal_type},
        )
    return actor


def _owner_status(session: Session, owner: GovernancePrincipal) -> str:
    if owner.principal_type == "person" and owner.employee_id is not None:
        employee = session.get(Employee, owner.employee_id)
        if employee is None:
            return "unresolved"
        return "active" if employee.status == "active" else "disabled"
    if owner.principal_type == "person":
        return "unbound"
    return "active"


def _is_execution_producer(actor: GovernancePrincipal, task: Task) -> bool:
    """Only the bound execution boundary may write execution facts.

    There is no B1/B2/B3 registry in Phase 0 (Issue #78 does not persist a
    trusted producer identity), so the producer is the task's bound executor
    (a person/agent for B1, the PEP/connector service for B2, the runtime
    adapter service for B3) or a verified service/runtime principal.  Anything
    else fails closed.
    """
    if actor.principal_type in {"service", "runtime"}:
        return True
    if actor.principal_id == task.executor_principal_id:
        return True
    # A person/agent principal resolving to the same employee is the same
    # execution subject (agent_instance vs person for the same human).
    return bool(
        actor.employee_id is not None
        and task.executor_employee_id is not None
        and actor.employee_id == task.executor_employee_id
    )


def _require_execution_producer(actor: GovernancePrincipal, task: Task) -> None:
    _require_actor(actor)
    if not _is_execution_producer(actor, task):
        raise TaskError(
            "task_unauthorized_actor",
            "only the bound executor or a trusted service may write execution facts",
            context={"actor": actor.principal_id, "executor": task.executor_principal_id},
        )


def _require_task_authority(
    actor: GovernancePrincipal, task: Task, *, allow_service: bool = True
) -> None:
    """Participants, privileged identities and (optionally) services only."""
    _require_actor(actor)
    if allow_service and actor.principal_type in {"service", "runtime"}:
        return
    if _is_privileged(actor) or _is_participant(actor, task):
        return
    raise TaskError(
        "task_unauthorized_actor",
        "only a participant, identity manager or trusted service may do this",
        context={"actor": actor.principal_id},
    )


def _assert_managed_lease(
    session: Session,
    task: Task,
    *,
    actor: GovernancePrincipal,
    worker_id: str | None,
    fencing_token: int | None,
    now: datetime,
) -> None:
    """Managed Execution writes must hold the current lease/fencing token.

    Advisory/Governed boundaries are client/PEP-driven and do not necessarily
    hold a worker lease; Managed Execution is inside the runtime adapter, where
    a stale worker must never write an execution or stop fact (F23/C9).  There
    is deliberately no "expired lease is still mine" escape hatch: an expired
    or released lease can never authorise a fact write.
    """
    if task.execution_mode != "managed_execution":
        return
    if actor.principal_type not in {"service", "runtime"}:
        raise TaskError(
            "task_unauthorized_actor",
            "Managed Execution facts require a trusted service/runtime producer",
            context={"actor": actor.principal_id},
        )
    _assert_lease(
        session,
        task,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=now,
    )


def _clamped_task_budget(
    task: Task, record: GovernanceDecisionRecord | None
) -> tuple[dict[str, Any], str]:
    """Return the grant budget and consumption mode for a task.

    The task-level caps are the caller's *ceiling*; the persisted decision is
    the policy ceiling.  The intersection is what a grant may carry, and the
    task row is narrowed to that intersection so a queued task can never hold a
    grant budget below its own (already narrowed) cap.  A single-use grant is
    always exactly one call; a multi-call task uses ``multi_use`` so
    ``max_calls > 1`` is representable instead of being refused by the grant
    issuer.
    """
    constraints = dict(record.constraints) if record is not None else {}
    policy_calls = constraints.get("max_calls")
    if not isinstance(policy_calls, int) or isinstance(policy_calls, bool):
        policy_calls = None
    policy_runtime = constraints.get("max_runtime_seconds")
    if not isinstance(policy_runtime, int) or isinstance(policy_runtime, bool):
        policy_runtime = None
    ttl = constraints.get("ttl_seconds")
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        ttl = 0

    mode = "single_use" if int(task.max_calls) == 1 else "multi_use"
    if policy_calls is None or policy_calls < 1 or ttl < 1:
        # A non-authorizing decision (deny/clarification/gate) carries an empty
        # constraint set.  It must not narrow the caller's caps, otherwise a
        # later clarification could never restore them.
        budget: dict[str, Any] = {
            "max_calls": int(task.max_calls),
            "max_runtime_seconds": int(task.max_runtime_seconds),
        }
        if task.max_cost_units is not None:
            budget["max_cost_units"] = float(task.max_cost_units)
        return budget, mode

    calls = max(1, min(int(task.max_calls), policy_calls))
    if bool(constraints.get("required_confirmation")):
        # A confirmation is single-use by contract, so the grant and the task
        # cap it backs are exactly one call.
        calls = 1
    budget = {"max_calls": calls}
    runtime: int | None = None
    if policy_runtime is not None:
        runtime = max(1, min(int(task.max_runtime_seconds), policy_runtime, ttl))
        budget["max_runtime_seconds"] = runtime

    policy_cost = constraints.get("max_cost_units")
    cost: float | None
    if task.max_cost_units is None:
        cost = None
    elif policy_cost is None:
        # The decision authorises no cost at all; a positive task cap is
        # clamped to "no cost" rather than silently widening the decision.
        cost = 0.0
    else:
        cost = float(min(float(task.max_cost_units), float(policy_cost)))
    if cost is not None:
        budget["max_cost_units"] = cost

    task.max_calls = calls
    if runtime is not None:
        task.max_runtime_seconds = runtime
    task.max_cost_units = cost
    return budget, ("single_use" if calls == 1 else "multi_use")


def _assert_task_budget(
    session: Session, task: Task, grant: ExecutionGrant | None, *, now: datetime
) -> None:
    """Enforce the persisted task budget at dispatch and the PEP boundary.

    Beyond the per-grant ceiling, a task has a total runtime budget measured
    from its first opened attempt.  ``now`` is therefore authoritative: a
    worker whose attempt outlived the task's runtime cap can never dispatch or
    execute another call, even if its individual grant is still live.
    """
    if grant is None:
        return
    budget = dict(grant.budget or {})
    grant_calls = budget.get("max_calls")
    grant_runtime = budget.get("max_runtime_seconds")
    grant_cost = budget.get("max_cost_units")
    if isinstance(grant_calls, int) and grant_calls > int(task.max_calls):
        raise TaskError(
            "budget_exceeded",
            "the grant exceeds the task call budget",
            context={"grant_max_calls": grant_calls, "task_max_calls": task.max_calls},
        )
    if isinstance(grant_runtime, int) and grant_runtime > int(task.max_runtime_seconds):
        raise TaskError(
            "budget_exceeded",
            "the grant exceeds the task runtime budget",
            context={
                "grant_max_runtime_seconds": grant_runtime,
                "task_max_runtime_seconds": task.max_runtime_seconds,
            },
        )
    if (
        task.max_cost_units is not None
        and isinstance(grant_cost, (int, float))
        and float(grant_cost) > float(task.max_cost_units)
    ):
        raise TaskError(
            "budget_exceeded",
            "the grant exceeds the task cost budget",
            context={
                "grant_max_cost_units": grant_cost,
                "task_max_cost_units": task.max_cost_units,
            },
        )
    started_at = session.scalar(
        select(func.min(TaskAttempt.started_at)).where(
            TaskAttempt.task_id == task.task_id
        )
    )
    if started_at is not None:
        elapsed = now - _to_utc(started_at)  # type: ignore[operator]
        if elapsed > timedelta(seconds=int(task.max_runtime_seconds)):
            raise TaskError(
                "budget_exceeded",
                "the task runtime budget is exhausted",
                context={
                    "task_max_runtime_seconds": task.max_runtime_seconds,
                    "elapsed_seconds": int(elapsed.total_seconds()),
                },
            )


def _urgency_authority(principal: GovernancePrincipal) -> bool:
    """Urgent priority needs a real management/approval authority.

    Phase 0 has no dedicated ``task/prioritize`` permission, so urgency is
    authorised by the existing identity-management permission or an existing
    approval permission.  A caller-supplied string alone is never authority.
    """
    if _is_privileged(principal):
        return True
    return any(
        resource == "expense_claim" and action == "approve"
        for resource, action, _scope in principal.effective_permissions
    )



def _normalized_scope(scope: Mapping[str, Any] | None) -> dict[str, Any]:
    if scope is None:
        return {"scope_type": "undetermined"}
    try:
        return validate_resource_scope(scope, require_concrete=True)
    except GovernanceError as exc:
        raise TaskError(exc.reason_code, context=exc.context) from exc


def _normalize_sla(sla: Mapping[str, Any] | None) -> dict[str, Any]:
    if sla is None:
        return {}
    out: dict[str, Any] = {}
    for key in ("response_by", "complete_by"):
        value = sla.get(key)
        if value is not None:
            if not isinstance(value, datetime):
                raise TaskError(
                    "required_input_missing",
                    f"SLA {key} must be a datetime",
                    context={"field": f"sla.{key}"},
                )
            out[key] = _rfc3339(_to_utc(value))  # type: ignore[arg-type]
    channel = sla.get("confirmation_channel")
    if channel is not None:
        if not isinstance(channel, str) or not channel.strip():
            raise TaskError(
                "required_input_missing",
                "SLA confirmation_channel must be a non-empty string",
                context={"field": "sla.confirmation_channel"},
            )
        out["confirmation_channel"] = channel
    escalation = sla.get("escalation_after_minutes")
    if escalation is not None:
        if not isinstance(escalation, int) or isinstance(escalation, bool) or escalation < 1:
            raise TaskError(
                "required_input_missing",
                "SLA escalation_after_minutes must be a positive integer",
                context={"field": "sla.escalation_after_minutes"},
            )
        out["escalation_after_minutes"] = escalation
    return out


def _record_completeness(task: Task) -> tuple[str, str | None]:
    missing: list[str] = []
    if not task.summary:
        missing.append("goal_missing")
    if not (task.goal or {}).get("completion_criteria"):
        missing.append("completion_criteria_missing")
    if not task.owner_principal_id:
        missing.append("owner_unresolved")
    if task.execution_mode == "undetermined":
        missing.append("execution_mode_undetermined")
    if (task.resource_scope or {}).get("scope_type") in {None, "undetermined"}:
        missing.append("scope_undetermined")
    if not missing:
        return "complete", None
    if len(missing) > 1:
        return "pre_execution_incomplete", "multiple_incomplete_fields"
    return "pre_execution_incomplete", missing[0]


def _event_payload(
    task: Task,
    transition: AllowedTransition,
    *,
    reason_code: str | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "transition": transition.id,
        "event": transition.event,
        "from_state": transition.source,
        "to_state": transition.target,
        "state_revision": task.state_revision,
        "attempt": task.attempt,
        "retry_budget_remaining": task.retry_budget_remaining,
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    if extra:
        payload.update(dict(extra))
    return payload


def _append_event(
    session: Session,
    task: Task,
    transition: AllowedTransition,
    *,
    actor: GovernancePrincipal | None,
    reason_code: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.task_id,
        event_id=_new_id("evt_"),
        event_type=transition.event,
        sequence=task.state_revision,
        from_state=transition.source,
        to_state=transition.target,
        actor_principal_id=(
            actor.principal_id if actor is not None else "svc_task_service"
        ),
        payload=_event_payload(
            task, transition, reason_code=reason_code, extra=extra
        ),
    )
    session.add(event)
    session.flush()
    task.last_event_id = event.event_id
    return event


def _emit_outbox(
    session: Session,
    task: Task,
    transition: AllowedTransition,
    *,
    reason_code: str | None,
) -> None:
    enqueue_event(
        session,
        event_type=transition.event,
        aggregate_type="task",
        aggregate_id=task.task_id,
        idempotency_key=(
            f"task:{transition.event}:{task.task_id}:{task.state_revision}"
        ),
        payload={
            "task_id": task.task_id,
            "state": task.state,
            "state_revision": task.state_revision,
            "transition": transition.id,
            "reason_code": reason_code,
        },
    )


def _reject_and_audit(
    session: Session,
    task: Task,
    *,
    actor: GovernancePrincipal | None,
    event: str,
    target: str,
    exc: TaskError,
) -> None:
    _audit(
        session,
        actor=actor,
        action="task.transition.rejected",
        task=task,
        details={
            "from_state": task.state,
            "event": event,
            "to_state": target,
            "reason_code": exc.reason_code,
            **{k: v for k, v in exc.context.items() if k not in {"task_id"}},
        },
    )


def _preview_transition(
    task: Task,
    *,
    event: str,
    target: str,
    ctx: TransitionContext,
    source: str | None = None,
    expected_revision: int | None = None,
) -> AllowedTransition:
    """Validate an edge/guards *before* any mutation is staged.

    A service function that must write facts before the transition (grant
    binding, stop evidence, attempt closure) calls this first, so a rejected
    transition can never leave a committed partial write behind.
    """
    if expected_revision is not None and task.state_revision != expected_revision:
        raise TaskStateConflict(
            task.task_id, expected=expected_revision, actual=task.state_revision
        )
    bounded = replace(ctx, expected_revision=expected_revision)
    return check_transition(
        task.state if source is None else source, event, target, bounded
    )


def _revoke_unbound_grant(
    session: Session,
    grant: ExecutionGrant,
    *,
    reason_code: str,
    now: datetime,
) -> None:
    """Revoke a freshly issued grant that will not be bound to a task.

    A guard failure after issuance must not leak an active authority: the
    caller catches the rejection, and the orphan grant is revoked in the same
    transaction.
    """
    if grant.status in {"revoked", "consumed"}:
        return
    try:
        subject = resolve_principal(
            session, grant.subject_id, principal_type=grant.subject_type
        )
        revoke_grant(
            session,
            grant_id=grant.grant_id,
            reason_code=reason_code,
            principal=subject,
            now=now,
        )
    except (GovernanceError, TaskError):
        grant.status = "revoked"
        grant.usable = False
        grant.revoked_at = now
        grant.revocation_reason = reason_code
        session.flush()


def _perform_transition(
    session: Session,
    task: Task,
    actor: GovernancePrincipal | None,
    *,
    event: str,
    target: str,
    ctx: TransitionContext,
    expected_revision: int | None = None,
    reason_code: str | None = None,
    extra: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    on_reject: Callable[[], None] | None = None,
) -> AllowedTransition:
    """Validate, apply and record exactly one transition (never commits)."""
    timestamp = now or _utcnow()
    if target in TASK_TERMINAL_STATES and task.state in TASK_TERMINAL_STATES:
        raise TaskError(
            "task_terminal_state_immutable",
            f"task {task.task_id!r} is in terminal state {task.state!r}",
            context={"task_id": task.task_id, "state": task.state},
        )
    if expected_revision is not None and task.state_revision != expected_revision:
        raise TaskStateConflict(
            task.task_id, expected=expected_revision, actual=task.state_revision
        )
    bounded = replace(
        ctx,
        state_revision=task.state_revision,
        expected_revision=expected_revision,
    )
    source: str | None = task.state
    if event == "task.created" and task.state_revision == 0:
        # T00 has no source state: the row exists as an uncommitted shell with
        # the column default ``draft`` but no lifecycle revision yet.
        source = None
    try:
        transition = check_transition(source, event, target, bounded)
    except TaskError as exc:
        if on_reject is not None:
            on_reject()
        _reject_and_audit(
            session,
            task,
            actor=actor,
            event=event,
            target=target,
            exc=exc,
        )
        raise

    apply_transition(task, transition)
    if transition.target == "queued":
        task.queued_at = timestamp
    if transition.target == "evaluated":
        task.evaluated_at = timestamp
    if transition.target in TASK_TERMINAL_STATES:
        task.completed_at = timestamp
    _append_event(
        session,
        task,
        transition,
        actor=actor,
        reason_code=reason_code,
        extra=extra,
    )
    _audit(
        session,
        actor=actor,
        action=f"task.event.{transition.event}",
        task=task,
        details={
            "transition": transition.id,
            "from_state": transition.source,
            "to_state": transition.target,
            "state_revision": task.state_revision,
            "attempt": task.attempt,
            "reason_code": reason_code,
            **dict(extra or {}),
        },
    )
    _emit_outbox(session, task, transition, reason_code=reason_code)
    return transition


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------


def _decision_record(session: Session, task: Task) -> GovernanceDecisionRecord | None:
    if not task.decision_id:
        return None
    return session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == task.decision_id
        )
    )


def _grant_record(session: Session, task: Task) -> ExecutionGrant | None:
    if not task.grant_id:
        return None
    return session.scalar(
        select(ExecutionGrant).where(ExecutionGrant.grant_id == task.grant_id)
    )


def _confirmation_row(
    session: Session, confirmation_id: str | None
) -> TaskConfirmation | None:
    if not confirmation_id:
        return None
    return session.scalar(
        select(TaskConfirmation).where(
            TaskConfirmation.confirmation_id == confirmation_id
        )
    )


def _has_open_attempt(session: Session, task: Task) -> bool:
    return (
        session.scalar(
            select(TaskAttempt.id).where(
                TaskAttempt.task_id == task.task_id,
                TaskAttempt.ended_at.is_(None),
            )
        )
        is not None
    )


def _confirmation_document(row: TaskConfirmation) -> dict[str, Any]:
    document: dict[str, Any] = {
        "confirmation_id": row.confirmation_id,
        "payload_hash": row.payload_hash,
        "canonicalization": "rfc8785_jcs",
        "purpose": row.purpose,
        "confirmed_by_principal_id": row.confirmed_by_principal_id,
        "status": row.status,
        "satisfied_at": (
            _rfc3339(_to_utc(row.satisfied_at))  # type: ignore[arg-type]
            if row.satisfied_at is not None
            else None
        ),
        "expires_at": _rfc3339(_to_utc(row.expires_at)),  # type: ignore[arg-type]
        "consumed_at": (
            _rfc3339(_to_utc(row.consumed_at))  # type: ignore[arg-type]
            if row.consumed_at is not None
            else None
        ),
        "consumed_by_call_id": row.consumed_by_call_id,
        "single_use": True,
    }
    if row.channel_binding is not None:
        document["channel_binding"] = dict(row.channel_binding)
    return document


def _approval_document(task: Task) -> dict[str, Any] | None:
    if not task.approval_ref:
        return None
    return dict(task.approval_ref)


def _live_confirmation_ref(
    session: Session, task: Task, *, now: datetime
) -> dict[str, Any] | None:
    """Return the contract ref only while the confirmation is still usable."""
    confirmation_id = (task.confirmation_ref or {}).get("confirmation_id")
    row = _confirmation_row(session, confirmation_id)
    if row is None or row.task_id != task.task_id:
        return None
    if row.status != "satisfied" or row.consumed_at is not None:
        return None
    if _to_utc(row.expires_at) <= now:  # type: ignore[operator]
        return None
    if row.payload_hash != task.payload_hash:
        return None
    return _confirmation_document(row)


def _live_approval_ref(task: Task, *, now: datetime) -> dict[str, Any] | None:
    approval = _approval_document(task)
    if not approval or approval.get("decision") != "approved":
        return None
    if approval.get("revoked_at"):
        return None
    expires = approval.get("expires_at")
    if not isinstance(expires, str):
        return None
    parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    if _to_utc(parsed) <= now:  # type: ignore[operator]
        return None
    if approval.get("payload_hash") != task.payload_hash:
        return None
    return approval


def _base_context(
    session: Session,
    task: Task,
    actor: GovernancePrincipal | None,
    *,
    now: datetime,
    expected_revision: int | None = None,
) -> TransitionContext:
    record = _decision_record(session, task)
    grant = _grant_record(session, task)
    constraints = dict(record.constraints) if record is not None else {}
    confirmation_row = _confirmation_row(
        session, (task.confirmation_ref or {}).get("confirmation_id")
    )
    completeness, _ = _record_completeness(task)

    confirmation_required = bool(constraints.get("required_confirmation")) or (
        task.decision_type == "needs_confirmation"
    )
    approval_required = bool(constraints.get("required_approval")) or (
        task.decision_type == "needs_approval"
    )

    confirmation_valid = False
    confirmation_satisfied = False
    confirmation_unexpired = False
    confirmation_unconsumed = False
    confirmation_payload_matches = False
    confirmation_channel_matches = True
    if confirmation_row is not None:
        confirmation_satisfied = confirmation_row.status == "satisfied"
        confirmation_unexpired = (
            _to_utc(confirmation_row.expires_at) > now  # type: ignore[operator]
        )
        confirmation_unconsumed = (
            confirmation_row.consumed_at is None
            and confirmation_row.consumed_by_call_id is None
        )
        confirmation_payload_matches = (
            confirmation_row.payload_hash == task.payload_hash
        )
        if confirmation_row.channel_binding is not None and actor is not None:
            expected_channel = (confirmation_row.channel_binding or {}).get("channel")
            actual_channel = (task.sla or {}).get("confirmation_channel")
            if expected_channel is not None:
                # A bound confirmation without a matching task channel is a
                # mismatch, not a free pass.
                confirmation_channel_matches = (
                    actual_channel is not None and expected_channel == actual_channel
                )
        confirmation_valid = (
            confirmation_satisfied
            and confirmation_unexpired
            and confirmation_unconsumed
            and confirmation_payload_matches
            and confirmation_channel_matches
            and confirmation_row.task_id == task.task_id
        )

    approval = task.approval_ref or {}
    approval_approved = approval.get("decision") == "approved"
    approval_separation = approval.get("separation_check") == "passed"
    approval_revoked = bool(approval.get("revoked_at"))
    approval_unexpired = False
    approval_payload_matches = (
        approval.get("payload_hash") == task.payload_hash
        if approval
        else False
    )
    if approval.get("expires_at"):
        parsed = datetime.fromisoformat(
            str(approval["expires_at"]).replace("Z", "+00:00")
        )
        approval_unexpired = _to_utc(parsed) > now  # type: ignore[operator]
    approval_valid = (
        approval_approved
        and approval_separation
        and approval_unexpired
        and approval_payload_matches
        and not approval_revoked
    )

    decision_task_bound = False
    decision_fresh = False
    decision_supersedes = None
    policy_resolved = False
    if record is not None:
        decision_task_bound = record.task_id == task.task_id
        decision_fresh = _to_utc(record.expires_at) > now  # type: ignore[operator]
        decision_supersedes = record.supersedes_decision_id
        policy_resolved = bool(record.policy_version)

    grant_task_matches = bool(grant and grant.task_id == task.task_id)
    grant_decision_matches = bool(
        grant
        and record is not None
        and grant.decision_id == record.decision_id
        and grant.decision_hash == record.decision_hash
    )
    grant_revision_matches = bool(
        grant
        and record is not None
        and int(grant.task_revision) == int(record.task_revision or 0)
    )
    # Issue #78 deliberately does not persist a grant attempt number (see
    # ``grant_document``), so the task layer cannot compare S-07's attempt
    # number.  The grant is bound to the task/revision/decision; the attempt is
    # recorded on task_attempts and enforced there.
    grant_attempt_matches = bool(grant)
    grant_unexpired = bool(grant and _to_utc(grant.expires_at) > now)
    grant_scope_ok = bool(grant)
    grant_consumed_by_this_attempt = bool(
        grant
        and grant.status == "consumed"
        and task.attempt >= 1
        and grant.task_id == task.task_id
    )

    open_attempt = _has_open_attempt(session, task)
    current_attempt = session.scalar(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.task_id)
        .order_by(TaskAttempt.attempt_number.desc())
        .limit(1)
    )
    result_doc = dict(task.result or {})
    # C7: ``no_side_effects`` is never asserted by the service layer.  It is
    # derived from persisted reconciliation facts only: an explicit no-side-
    # effect proof, a reconciled stop, or a task that never opened an attempt.
    no_side_effects = bool(result_doc.get("no_side_effects")) or bool(
        result_doc.get("side_effects_reconciled")
    )
    if task.attempt == 0 and not open_attempt:
        # No attempt was ever opened, so no side effect can have been produced
        # through this task.  This is a structural fact, not a caller default.
        no_side_effects = True
    active_unreconciled = open_attempt and not bool(
        result_doc.get("no_side_effect_evidence")
    )

    # C6: both facts come from the persisted attempt row and its execution
    # result, never from a caller-supplied default.
    same_attempt_running_evidence = bool(
        current_attempt is not None
        and current_attempt.attempt_number == task.attempt
        and current_attempt.state == "running"
        and current_attempt.ended_at is None
    )
    attempt_finished_pending_verification = bool(
        current_attempt is not None
        and current_attempt.attempt_number == task.attempt
        and current_attempt.ended_at is not None
        and current_attempt.state == "verifying"
        and (
            result_doc.get("outcome") in {"completed", "succeeded"}
            or result_doc.get("attempt_finished_pending_verification")
        )
    )

    return TransitionContext(
        actor_verified=actor is not None,
        actor_active=bool(actor and actor.is_active),
        actor_authorized=bool(actor and _actor_authorized(actor, task)),
        actor_is_requester=bool(actor and actor.principal_id == task.requester_principal_id),
        actor_is_owner=bool(actor and actor.principal_id == task.owner_principal_id),
        actor_is_executor=bool(actor and actor.principal_id == task.executor_principal_id),
        actor_is_approver=bool(actor and actor.principal_id == task.approver_principal_id),
        state_revision=task.state_revision,
        expected_revision=expected_revision,
        goal_present=bool(task.summary),
        completion_criteria_present=bool(
            (task.goal or {}).get("completion_criteria")
        ),
        required_inputs_satisfied=bool((task.result or {}).get("inputs_satisfied", True)),
        missing_items=tuple((task.result or {}).get("missing_inputs", ())),
        owner_resolved=bool(task.owner_principal_id),
        owner_active=task.owner_account_status == "active",
        owner_escalation_present=task.owner_escalation is not None,
        approver_resolved=bool(task.approver_principal_id),
        approver_independent=bool(
            task.approver_principal_id
            and task.approver_principal_id
            not in {
                task.requester_principal_id,
                task.owner_principal_id,
                task.executor_principal_id,
            }
        ),
        assignee_present=bool(task.assignee_principal_id),
        executor_present=bool(task.executor_principal_id),
        record_completeness=completeness,
        execution_mode=task.execution_mode,
        scope_type=(task.resource_scope or {}).get("scope_type", "undetermined"),
        mode_capabilities_ok=task.execution_mode in {
            "advisory",
            "governed_resource_access",
        }
        or bool((task.result or {}).get("runtime_capabilities_verified")),
        decision_present=record is not None,
        decision_type=task.decision_type,
        decision_purpose=task.decision_purpose,
        decision_revision=int(task.decision_revision or 1),
        decision_supersedes=decision_supersedes,
        decision_task_bound=decision_task_bound,
        decision_fresh=decision_fresh,
        decision_invalidated=(
            task.decision_type == "deny"
            or (grant is not None and grant.status in {"revoked", "expired"})
        ),
        policy_resolved=policy_resolved,
        fail_closed=bool(
            record is not None and (record.document or {}).get("fail_closed", False)
        ),
        clarification_required=task.decision_type == "needs_clarification",
        confirmation_required=confirmation_required,
        approval_required=approval_required,
        grant_id=task.grant_id,
        grant_status=(
            grant.status if grant is not None else task.grant_status
        ),
        grant_task_matches=grant_task_matches,
        grant_revision_matches=grant_revision_matches,
        grant_decision_matches=grant_decision_matches,
        grant_attempt_matches=grant_attempt_matches,
        grant_unexpired=grant_unexpired,
        grant_scope_ok=grant_scope_ok,
        grant_consumed_by_this_attempt=grant_consumed_by_this_attempt,
        confirmation_valid=confirmation_valid,
        confirmation_satisfied=confirmation_satisfied,
        confirmation_payload_matches=confirmation_payload_matches,
        confirmation_unexpired=confirmation_unexpired,
        confirmation_unconsumed=confirmation_unconsumed,
        confirmation_channel_matches=confirmation_channel_matches,
        approval_valid=approval_valid,
        approval_approved=approval_approved,
        approval_separation_passed=approval_separation,
        approval_unexpired=approval_unexpired,
        approval_payload_matches=approval_payload_matches,
        gates_satisfied=(
            (not confirmation_required or confirmation_valid)
            and (not approval_required or approval_valid)
        ),
        attempt=task.attempt,
        max_attempts=task.max_attempts,
        retry_budget_remaining=task.retry_budget_remaining,
        attempt_started=task.attempt >= 1,
        attempt_open=open_attempt,
        active_attempt_unreconciled=active_unreconciled,
        stop_confirmed=task.stop_confirmed,
        stop_evidence=bool((task.result or {}).get("stop_evidence_refs")),
        no_side_effects=no_side_effects,
        side_effects_reconciled=bool(
            (task.result or {}).get("side_effects_reconciled", False)
        ),
        safely_retryable=bool(task.retry_safe),
        retry_exhausted=(
            task.attempt >= task.max_attempts
            or task.retry_budget_remaining <= 0
            or not task.retry_safe
            or task.attempt == 0
        ),
        retry_allowed=task.retry_budget_remaining > 0,
        execution_handle_bound=bool(task.runtime_task_id or task.execution_ref),
        runtime_supported=task.execution_mode
        in {"advisory", "governed_resource_access"},
        result_present=bool((task.result or {}).get("result_ref")),
        evidence_present=bool((task.result or {}).get("evidence_refs")),
        verification_result=str(
            ((task.result or {}).get("verification") or {}).get(
                "result", "pending"
            )
        ),
        verification_passed=(
            ((task.result or {}).get("verification") or {}).get("result")
            == "passed"
        ),
        verification_partial_allowed=bool(
            (task.result or {}).get("partial_allowed", False)
        ),
        verification_inconclusive=(
            ((task.result or {}).get("verification") or {}).get("result")
            == "inconclusive"
        ),
        evidence_unavailable=bool(
            (task.result or {}).get("evidence_unavailable", False)
        ),
        owner_accepted_partial=bool(
            (task.result or {}).get("owner_accepted_partial", False)
        ),
        all_criteria_passed=bool(
            (task.result or {}).get("all_criteria_passed", False)
        ),
        needs_human_origin=task.needs_human_origin,
        same_attempt_running_evidence=same_attempt_running_evidence,
        attempt_finished_pending_verification=attempt_finished_pending_verification,
        no_side_effect_evidence=bool(
            (task.result or {}).get("no_side_effect_evidence", False)
        ),
    )


def _validate_evidence_refs(refs: Sequence[str]) -> tuple[str, ...]:
    """Validate stop/reconcile evidence references before persisting them."""
    values = tuple(refs)
    if not values:
        raise TaskError(
            "evidence_missing",
            "at least one evidence reference is required",
            context={"field": "evidence_refs"},
        )
    verification.validate_result_refs(None, values, require_result=False)
    return values


def _close_attempt_with_stop(
    session: Session,
    task: Task,
    *,
    state: str,
    now: datetime,
    evidence_refs: Sequence[str],
    result_ref: str | None = None,
    error_code: str | None = None,
    reason: str | None = None,
) -> None:
    """Close the current open attempt with stop evidence before a re-dispatch."""
    attempt = _current_attempt(session, task)
    if attempt is None or attempt.ended_at is not None:
        return
    _close_attempt(
        attempt,
        state=state,
        now=now,
        stop_confirmed=True,
        result_ref=result_ref,
        evidence_refs=evidence_refs,
        error_code=error_code,
        reason=reason,
    )


def _owner_escalation_document(
    task: Task, *, status: str, now: datetime, reason_code: str
) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "owner_principal_id": task.owner_principal_id,
        "owner_employee_id": task.owner_employee_id,
        "owner_account_status": status,
        "escalated_at": _rfc3339(now),
        "requires_human": True,
    }


def _assert_completion_authority(
    session: Session, task: Task, *, now: datetime
) -> None:
    """Re-check the live grant and gates before declaring success.

    T36/T42/T43 must never succeed on an expired, revoked or consumed-by-
    another-attempt grant, a revoked/expired approval or a confirmation that
    was never satisfied.  A confirmation consumed by *this* attempt is allowed
    (that is what single-use consumption means).
    """
    grant = _grant_record(session, task)
    if grant is None or not task.grant_id:
        raise TaskError(
            "grant_missing",
            "completion requires the bound execution grant",
            context={"task_id": task.task_id},
        )
    if grant.task_id != task.task_id:
        raise TaskError(
            "grant_scope_mismatch",
            "the grant is bound to another task",
            context={"grant_id": grant.grant_id},
        )
    if grant.status == "revoked":
        raise TaskError("grant_revoked", context={"grant_id": grant.grant_id})
    if grant.status == "expired":
        raise TaskError("grant_expired", context={"grant_id": grant.grant_id})
    if grant.status == "consumed" and not (task.attempt >= 1):
        raise TaskError("grant_consumed", context={"grant_id": grant.grant_id})
    if _to_utc(grant.expires_at) <= now:  # type: ignore[operator]
        raise TaskError("grant_expired", context={"grant_id": grant.grant_id})
    record = _decision_record(session, task)
    if record is None or grant.decision_id != record.decision_id:
        raise TaskError(
            "grant_scope_mismatch",
            "the grant no longer matches the persisted decision",
            context={"grant_id": grant.grant_id},
        )
    _assert_task_budget(session, task, grant, now=now)

    constraints = dict(record.constraints or {})
    confirmation_required = bool(constraints.get("required_confirmation")) or (
        task.decision_type == "needs_confirmation"
    )
    if confirmation_required:
        row = _confirmation_row(
            session, (task.confirmation_ref or {}).get("confirmation_id")
        )
        if (
            row is None
            or row.task_id != task.task_id
            or row.payload_hash != task.payload_hash
            or row.status not in {"satisfied", "consumed"}
        ):
            raise TaskError(
                "confirmation_missing",
                "the confirmed payload is no longer valid for completion",
                context={"task_id": task.task_id},
            )
        if row.status == "satisfied" and _to_utc(row.expires_at) <= now:  # type: ignore[operator]
            raise TaskError(
                "task_confirmation_expired",
                "the confirmation expired before completion",
                context={"confirmation_id": row.confirmation_id},
            )

    approval_required = bool(constraints.get("required_approval")) or (
        task.decision_type == "needs_approval"
    )
    if approval_required:
        approval = task.approval_ref or {}
        expires_raw = approval.get("expires_at")
        expired = False
        if isinstance(expires_raw, str):
            parsed = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            expired = _to_utc(parsed) <= now  # type: ignore[operator]
        if (
            approval.get("decision") != "approved"
            or approval.get("separation_check") != "passed"
            or approval.get("payload_hash") != task.payload_hash
            or approval.get("revoked_at")
            or expired
        ):
            raise TaskError(
                "approval_missing",
                "the approval is no longer valid for completion",
                context={"task_id": task.task_id},
            )


def _approver_is_independent(
    approver: GovernancePrincipal | None, task: Task
) -> bool:
    if approver is None:
        return False
    if approver.principal_type != "person":
        return False
    return approver.principal_id not in {
        task.requester_principal_id,
        task.owner_principal_id,
        task.executor_principal_id,
    }


# ---------------------------------------------------------------------------
# Necessity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NecessityResult:
    """Deterministic outcome of the duplicate/missing-input/already-done check."""

    outcome: str
    existing_task_id: str | None = None
    missing_inputs: tuple[str, ...] = field(default_factory=tuple)
    reason_code: str | None = None

    @property
    def is_new(self) -> bool:
        return self.outcome == NecessityDecision.NEW


def classify_necessity(
    session: Session,
    *,
    requester: GovernancePrincipal,
    goal: Mapping[str, Any] | None,
    provided_inputs: Sequence[str] | None = None,
    goal_hash: str | None = None,
) -> NecessityResult:
    """Classify a submission before creating anything.

    * ``missing_input``      -- the goal declares required inputs that were not
                                supplied (or the goal itself is incomplete);
    * ``already_satisfied``  -- the same requester already has a ``succeeded``
                                task with an identical goal;
    * ``duplicate``          -- the same requester already has a live task with
                                an identical goal;
    * ``new``                -- safe to create.
    """
    inputs = {str(item) for item in (provided_inputs or ())}
    try:
        normalized_goal = verification.validate_goal(goal)
    except TaskError as exc:
        return NecessityResult(
            NecessityDecision.MISSING_INPUT,
            missing_inputs=(str(exc.context.get("field", "goal")),),
            reason_code=exc.reason_code,
        )
    required = set(normalized_goal.get("required_inputs") or ())
    missing = tuple(sorted(required - inputs))
    if missing:
        return NecessityResult(
            NecessityDecision.MISSING_INPUT,
            missing_inputs=missing,
            reason_code="required_input_missing",
        )
    fingerprint = goal_hash or payload_hash(normalized_goal)

    succeeded = session.scalar(
        select(Task.task_id)
        .where(
            Task.requester_principal_id == requester.principal_id,
            Task.goal_hash == fingerprint,
            Task.state == "succeeded",
        )
        .limit(1)
    )
    if succeeded is not None:
        return NecessityResult(
            NecessityDecision.ALREADY_SATISFIED,
            existing_task_id=succeeded,
            reason_code="evidence_missing",
        )
    live = session.scalar(
        select(Task.task_id)
        .where(
            Task.requester_principal_id == requester.principal_id,
            Task.goal_hash == fingerprint,
            Task.state.notin_(tuple(TASK_TERMINAL_STATES) + ("failed",)),
        )
        .limit(1)
    )
    if live is not None:
        return NecessityResult(
            NecessityDecision.DUPLICATE,
            existing_task_id=live,
            reason_code="task_duplicate",
        )
    return NecessityResult(NecessityDecision.NEW)


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def _request_fingerprint(
    *,
    goal_hash: str,
    payload_hash_value: str,
    owner: GovernancePrincipal,
    executor: GovernancePrincipal,
    approver: GovernancePrincipal | None,
    priority: str,
    due_at: datetime | None,
    sla: Mapping[str, Any],
    execution_mode: str,
    scope: Mapping[str, Any],
    capabilities: Sequence[str],
    max_calls: int = 1,
    max_runtime_seconds: int = 300,
    max_cost_units: float | None = None,
) -> str:
    return payload_hash(
        {
            "goal_hash": goal_hash,
            "payload_hash": payload_hash_value,
            "owner": owner.principal_id,
            "executor": executor.principal_id,
            "approver": approver.principal_id if approver else None,
            "priority": priority,
            "due_at": _rfc3339(_to_utc(due_at)) if due_at else None,
            "sla": dict(sla),
            "execution_mode": execution_mode,
            "scope": dict(scope),
            "capabilities": sorted(capabilities),
            "max_calls": max_calls,
            "max_runtime_seconds": max_runtime_seconds,
            "max_cost_units": max_cost_units,
        }
    )


def submit_task(
    session: Session,
    *,
    requester: GovernancePrincipal,
    idempotency_key: str,
    goal: Mapping[str, Any],
    owner: GovernancePrincipal,
    executor: GovernancePrincipal,
    assignee: GovernancePrincipal | None = None,
    approver: GovernancePrincipal | None = None,
    summary: str | None = None,
    priority: str = "normal",
    priority_authorized_by: str | None = None,
    priority_rationale: str | None = None,
    due_at: datetime | None = None,
    sla: Mapping[str, Any] | None = None,
    execution_mode: str = "advisory",
    resource_scope: Mapping[str, Any] | None = None,
    requested_capabilities: Sequence[str] = (),
    policy_set_id: str = DEFAULT_POLICY_SET_ID,
    policy_version: str | None = None,
    payload: Mapping[str, Any] | None = None,
    confirmation_ref: Mapping[str, Any] | None = None,
    approval_ref: Mapping[str, Any] | None = None,
    provided_inputs: Sequence[str] | None = None,
    max_attempts: int = 3,
    retry_budget: int | None = None,
    retry_safe: bool = True,
    max_calls: int = 1,
    max_runtime_seconds: int = 300,
    max_cost_units: float | None = None,
    client_task_ref: str | None = None,
    correlation_id: str | None = None,
    runtime_capabilities_verified: bool = False,
    now: datetime | None = None,
) -> Task:
    """Create, evaluate and (when admissible) queue one task.

    The submission idempotency key is scoped to the verified requester: the same
    key with the same canonical request returns the existing task without a
    second side effect; the same key with a different request is a conflict.
    """
    timestamp = now or _utcnow()
    _require_actor(requester)
    if not idempotency_key.startswith("idem_") or len(idempotency_key) < 9:
        raise TaskError(
            "required_input_missing",
            "idempotency_key must match idem_*",
            context={"field": "idempotency_key"},
        )
    if owner is None:
        raise OwnerError(
            "accountable_owner_missing",
            "an accountable owner is required",
            context={"field": "owner"},
        )
    _require_actor(owner)
    if owner.principal_type != "person":
        raise OwnerError(
            "accountable_owner_unresolved",
            "the accountable owner must be a person or organisation role",
            context={"principal_type": owner.principal_type},
        )
    if executor is None:
        raise TaskError(
            "accountable_owner_unresolved",
            "an executor is required",
            context={"field": "executor"},
        )
    _require_actor(executor)
    assignee = assignee or executor
    _require_actor(assignee)

    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
        or max_attempts > 100
    ):
        raise TaskError(
            "required_input_missing",
            "max_attempts must be between 1 and 100",
            context={"field": "max_attempts"},
        )
    if retry_budget is not None and (
        isinstance(retry_budget, bool) or not isinstance(retry_budget, int)
    ):
        raise TaskError(
            "required_input_missing",
            "retry_budget must be an integer",
            context={"field": "retry_budget"},
        )
    budget = (
        max(0, max_attempts - 1)
        if retry_budget is None
        else int(retry_budget)
    )
    if budget < 0 or budget > 100:
        raise TaskError(
            "required_input_missing",
            "retry_budget must be between 0 and 100",
            context={"field": "retry_budget"},
        )

    level, authorized_by, rationale = scheduling.effective_priority(
        priority,
        authorized_by=priority_authorized_by,
        rationale=priority_rationale,
    )
    if level == "urgent" and authorized_by:
        # C-urgent: a caller-supplied string is not authority.  Resolve the
        # named principal from the directory and require a real management or
        # approval permission.
        try:
            authorizer = resolve_principal(session, authorized_by)
        except GovernanceError as exc:
            raise TaskError(
                "task_priority_unauthorized",
                "the urgent authorising principal cannot be resolved",
                context={"authorized_by": authorized_by, **exc.context},
            ) from exc
        if not _urgency_authority(authorizer):
            raise TaskError(
                "task_priority_unauthorized",
                "the urgent authorising principal lacks urgency authority",
                context={"authorized_by": authorizer.principal_id},
            )
    if (
        isinstance(max_calls, bool)
        or not isinstance(max_calls, int)
        or max_calls < 1
        or max_calls > 1000
    ):
        raise TaskError(
            "required_input_missing",
            "max_calls must be an integer between 1 and 1000",
            context={"field": "max_calls"},
        )
    if (
        isinstance(max_runtime_seconds, bool)
        or not isinstance(max_runtime_seconds, int)
        or max_runtime_seconds < 1
        or max_runtime_seconds > 86400
    ):
        raise TaskError(
            "required_input_missing",
            "max_runtime_seconds must be an integer between 1 and 86400",
            context={"field": "max_runtime_seconds"},
        )
    if max_cost_units is not None and (
        isinstance(max_cost_units, bool) or float(max_cost_units) < 0
    ):
        raise TaskError(
            "required_input_missing",
            "max_cost_units must be non-negative",
            context={"field": "max_cost_units"},
        )
    normalized_goal = verification.validate_goal(goal)
    goal_hash = payload_hash(normalized_goal)
    scope = _normalized_scope(resource_scope)
    sla_value = _normalize_sla(sla)
    capabilities = tuple(dict.fromkeys(requested_capabilities))
    for capability in capabilities:
        if not isinstance(capability, str) or "@" not in capability:
            raise TaskError(
                "capability_not_granted",
                f"malformed capability {capability!r}",
                context={"field": "requested_capabilities"},
            )
    if not capabilities:
        raise TaskError(
            "required_input_missing",
            "at least one requested capability is required",
            context={"field": "requested_capabilities"},
        )
    if approver is not None:
        _require_actor(approver)
        if approver.principal_id in {
            requester.principal_id,
            owner.principal_id,
            executor.principal_id,
        }:
            raise OwnerError(
                "self_approval_forbidden",
                "the approver must be independent of requester, owner and executor",
                context={"approver": approver.principal_id},
            )

    execution_payload = dict(
        payload
        if payload is not None
        else {
            "goal": normalized_goal,
            "resource_scope": scope,
            "requested_capabilities": list(capabilities),
            "execution_mode": execution_mode,
        }
    )
    payload_hash_value = payload_hash(execution_payload)
    request_hash = _request_fingerprint(
        goal_hash=goal_hash,
        payload_hash_value=payload_hash_value,
        owner=owner,
        executor=executor,
        approver=approver,
        priority=level,
        due_at=due_at,
        sla=sla_value,
        execution_mode=execution_mode,
        scope=scope,
        capabilities=capabilities,
        max_calls=max_calls,
        max_runtime_seconds=max_runtime_seconds,
        max_cost_units=max_cost_units,
    )
    correlation = correlation_id or _new_id("corr_")

    # Idempotency: same key + same canonical request returns the same task.
    existing = session.scalar(
        select(Task).where(
            Task.requester_principal_id == requester.principal_id,
            Task.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                existing.task_id, idempotency_key=idempotency_key
            )
        return existing

    necessity = classify_necessity(
        session,
        requester=requester,
        goal=normalized_goal,
        provided_inputs=provided_inputs,
        goal_hash=goal_hash,
    )
    if necessity.outcome == NecessityDecision.MISSING_INPUT:
        raise TaskError(
            necessity.reason_code or "required_input_missing",
            "the submission is missing required inputs",
            context={"missing_inputs": list(necessity.missing_inputs)},
        )
    if necessity.outcome == NecessityDecision.ALREADY_SATISFIED:
        raise TaskError(
            "task_already_satisfied",
            "an equivalent task has already succeeded",
            context={"existing_task_id": necessity.existing_task_id},
        )
    if necessity.outcome == NecessityDecision.DUPLICATE:
        raise TaskError(
            "task_duplicate",
            "an equivalent task is already in flight",
            context={"existing_task_id": necessity.existing_task_id},
        )

    task = Task(
        task_id=_new_id("task_"),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        goal_hash=goal_hash,
        client_task_ref=client_task_ref,
        correlation_id=correlation,
        requester_principal_id=requester.principal_id,
        requester_employee_id=requester.employee_id,
        owner_principal_id=owner.principal_id,
        owner_employee_id=owner.employee_id,
        owner_account_status=_owner_status(session, owner),
        assignee_principal_id=assignee.principal_id,
        executor_principal_id=executor.principal_id,
        executor_principal_type=executor.principal_type,
        executor_employee_id=executor.employee_id,
        approver_principal_id=approver.principal_id if approver else None,
        approver_employee_id=approver.employee_id if approver else None,
        summary=summary or normalized_goal["summary"],
        goal=normalized_goal,
        priority=level,
        priority_requested=priority,
        priority_rationale=rationale,
        priority_authorized_by=authorized_by,
        due_at=_to_utc(due_at),
        sla=sla_value,
        execution_mode=execution_mode,
        resource_scope=scope,
        requested_capabilities=list(capabilities),
        policy_set_id=policy_set_id,
        policy_version=policy_version,
        state="draft",
        state_revision=0,
        attempt=0,
        max_attempts=max_attempts,
        retry_budget_remaining=budget,
        payload=execution_payload,
        payload_hash=payload_hash_value,
        retry_safe=retry_safe,
        max_calls=max_calls,
        max_runtime_seconds=max_runtime_seconds,
        max_cost_units=(
            float(max_cost_units) if max_cost_units is not None else None
        ),
        created_at=timestamp,
        updated_at=timestamp,
        result={
            "runtime_capabilities_verified": runtime_capabilities_verified,
            "inputs_satisfied": True,
        },
    )
    completeness, incomplete_reason = _record_completeness(task)
    task.record_completeness = completeness
    task.incomplete_reason = incomplete_reason
    task.submitted_at = timestamp

    # Claim the idempotency key before any PDP side effect.  A concurrent
    # duplicate blocks on the unique index and is resolved to the existing row
    # instead of creating a second decision/outbox event.
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(Task).where(
                Task.requester_principal_id == requester.principal_id,
                Task.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflict(
                    existing.task_id, idempotency_key=idempotency_key
                ) from None
            return existing
        # C8: the partial unique index serialised two equivalent goals that
        # were submitted concurrently with different idempotency keys.  The
        # loser is answered deterministically instead of creating a duplicate.
        duplicate = session.scalar(
            select(Task.task_id)
            .where(
                Task.requester_principal_id == requester.principal_id,
                Task.goal_hash == goal_hash,
                Task.state.notin_(tuple(TASK_TERMINAL_STATES) + ("failed",)),
            )
            .limit(1)
        )
        if duplicate is not None:
            raise TaskError(
                "task_duplicate",
                "an equivalent task is already in flight",
                context={"existing_task_id": duplicate},
            ) from None
        raise

    ctx = _base_context(session, task, requester, now=timestamp)
    _perform_transition(
        session,
        task,
        requester,
        event="task.created",
        target="draft",
        ctx=ctx,
        now=timestamp,
    )

    decision = evaluate(
        session,
        EvaluationRequest(
            principal=executor,
            capability=capabilities[0],
            task_id=task.task_id,
            task_revision=task.state_revision + 1,
            task_idempotency_key=task.idempotency_key,
            resource_scope=scope,
            priority=level,
            execution_mode=execution_mode,
            risk="low",
            confirmation_ref=confirmation_ref,
            approval_ref=approval_ref,
            payload_hash=payload_hash_value,
            policy_set_id=policy_set_id,
            policy_version=policy_version,
            correlation_id=correlation,
        ),
        now=timestamp,
        evidence_verifier=TaskEvidenceVerifier(session, task_id=task.task_id),
    )
    _apply_decision(
        session,
        task,
        requester,
        decision,
        from_state="draft",
        now=timestamp,
        runtime_capabilities_verified=runtime_capabilities_verified,
    )
    return task


# ---------------------------------------------------------------------------
# Evaluation / gates
# ---------------------------------------------------------------------------


def _apply_decision(
    session: Session,
    task: Task,
    actor: GovernancePrincipal,
    decision: GovernanceDecision,
    *,
    from_state: str,
    now: datetime,
    runtime_capabilities_verified: bool = False,
) -> None:
    record = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == decision.decision_id
        )
    )
    if record is None:  # pragma: no cover - the PDP always persists
        raise TaskError(
            "required_input_missing",
            "the PDP decision was not persisted",
            context={"decision_id": decision.decision_id},
        )
    task.decision_id = decision.decision_id
    task.decision_revision = decision.decision_revision
    task.decision_type = decision.decision_type
    task.decision_purpose = decision.decision_purpose
    task.policy_set_id = record.policy_set_id
    task.policy_version = record.policy_version
    task.policy_content_hash = record.policy_content_hash
    task.evaluated_at = now
    # Persist only the policy-bounded task caps: a submitted task may never
    # carry a budget ceiling above what its decision authorises, so no later
    # grant can be narrower than the task cap it claims to enforce.
    _clamped_task_budget(task, record)
    if runtime_capabilities_verified:
        task.result = {**dict(task.result or {}), "runtime_capabilities_verified": True}

    event_by_source = {
        "draft": ("task.evaluated", "task.evaluated"),
        "awaiting_clarification": (
            "clarification.provided",
            "clarification.provided",
        ),
        "awaiting_confirmation": ("confirmation.recorded", "confirmation.recorded"),
        "awaiting_approval": ("approval.recorded", "approval.recorded"),
    }
    if from_state not in event_by_source:
        raise TaskError(
            "task_invalid_transition",
            f"cannot apply an evaluation from {from_state!r}",
            context={"from_state": from_state},
        )
    event, _ = event_by_source[from_state]
    ctx = _base_context(session, task, actor, now=now)
    _perform_transition(
        session,
        task,
        actor,
        event=event,
        target="evaluated",
        ctx=ctx,
        now=now,
    )
    _after_evaluated(
        session,
        task,
        actor,
        decision,
        now=now,
        runtime_capabilities_verified=runtime_capabilities_verified,
    )


def _reapply_decision_after_reassignment(
    session: Session,
    task: Task,
    actor: GovernancePrincipal,
    *,
    now: datetime,
) -> None:
    """Re-evaluate an ``evaluated`` task after responsibility changed.

    The old decision/grant is already invalidated by the caller.  This mirrors
    :func:`_after_evaluated` without the ``draft -> evaluated`` edge: the task is
    already evaluated, so only the downstream T04-T08 edge is applied.
    """
    record = _decision_record(session, task)
    executor = resolve_principal(
        session,
        task.executor_principal_id or "",
        principal_type=task.executor_principal_type,
    )
    decision = evaluate(
        session,
        EvaluationRequest(
            principal=executor,
            capability=(task.requested_capabilities or [""])[0],
            task_id=task.task_id,
            task_revision=task.state_revision + 1,
            task_idempotency_key=task.idempotency_key,
            resource_scope=task.resource_scope,
            priority=task.priority,
            execution_mode=task.execution_mode,
            risk="low",
            confirmation_ref=task.confirmation_ref,
            approval_ref=_approval_document(task),
            payload_hash=task.payload_hash,
            policy_set_id=task.policy_set_id or DEFAULT_POLICY_SET_ID,
            policy_version=task.policy_version,
            correlation_id=task.correlation_id,
            decision_purpose="initial_execution",
            supersedes_decision_id=record.decision_id if record is not None else None,
        ),
        now=now,
        evidence_verifier=TaskEvidenceVerifier(session, task_id=task.task_id),
    )
    new_record = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == decision.decision_id
        )
    )
    if new_record is None:  # pragma: no cover - the PDP always persists
        raise TaskError(
            "required_input_missing",
            "the PDP decision was not persisted",
            context={"decision_id": decision.decision_id},
        )
    task.decision_id = decision.decision_id
    task.decision_revision = decision.decision_revision
    task.decision_type = decision.decision_type
    task.decision_purpose = decision.decision_purpose
    task.policy_set_id = new_record.policy_set_id
    task.policy_version = new_record.policy_version
    task.policy_content_hash = new_record.policy_content_hash
    task.evaluated_at = now
    _clamped_task_budget(task, new_record)
    session.flush()
    _after_evaluated(
        session,
        task,
        actor,
        decision,
        now=now,
        runtime_capabilities_verified=bool(
            (task.result or {}).get("runtime_capabilities_verified", False)
        ),
    )


def _after_evaluated(
    session: Session,
    task: Task,
    actor: GovernancePrincipal,
    decision: GovernanceDecision,
    *,
    now: datetime,
    runtime_capabilities_verified: bool,
) -> None:
    if decision.decision_type == "deny":
        ctx = _base_context(session, task, actor, now=now)
        _perform_transition(
            session,
            task,
            actor,
            event="governance.denied",
            target="denied",
            ctx=ctx,
            reason_code=(
                decision.reason_codes[0] if decision.reason_codes else "policy_denied"
            ),
            now=now,
        )
        task.stop_confirmed = True
        task.result = {
            **dict(task.result or {}),
            "outcome": "none",
            "stop_confirmed": True,
            "stop_evidence_refs": [_EVIDENCE_NEVER_STARTED],
        }
        session.flush()
        return

    if decision.decision_type == "needs_clarification":
        missing = tuple(decision.reason_codes)
        task.result = {**dict(task.result or {}), "missing_inputs": list(missing)}
        ctx = replace(
            _base_context(session, task, actor, now=now),
            missing_items=missing or ("required_input_missing",),
        )
        _perform_transition(
            session,
            task,
            actor,
            event="governance.clarification_required",
            target="awaiting_clarification",
            ctx=ctx,
            reason_code=missing[0] if missing else "required_input_missing",
            now=now,
        )
        return

    if decision.decision_type == "needs_confirmation":
        _open_confirmation(session, task, actor, now=now)
        ctx = _base_context(session, task, actor, now=now)
        _perform_transition(
            session,
            task,
            actor,
            event="governance.confirmation_required",
            target="awaiting_confirmation",
            ctx=ctx,
            reason_code="confirmation_required",
            now=now,
        )
        return

    if decision.decision_type == "needs_approval":
        _open_approval(session, task, actor, now=now)
        ctx = _base_context(session, task, actor, now=now)
        _perform_transition(
            session,
            task,
            actor,
            event="governance.approval_required",
            target="awaiting_approval",
            ctx=ctx,
            reason_code="approval_required",
            now=now,
        )
        return

    if decision.decision_type in {"allow", "managed_execution_required"}:
        _issue_grant_and_queue(
            session,
            task,
            actor,
            now=now,
            runtime_capabilities_verified=runtime_capabilities_verified,
        )
        return

    raise TaskError(
        "required_input_missing",
        f"unsupported decision type {decision.decision_type!r}",
        context={"decision_type": decision.decision_type},
    )


def _open_confirmation(
    session: Session,
    task: Task,
    actor: GovernancePrincipal,
    *,
    now: datetime,
    confirmation: TaskConfirmation | None = None,
) -> TaskConfirmation:
    existing = confirmation
    if existing is None:
        existing = session.scalar(
            select(TaskConfirmation).where(
                TaskConfirmation.task_id == task.task_id,
                TaskConfirmation.status.in_(("pending", "satisfied")),
                TaskConfirmation.payload_hash == task.payload_hash,
            )
        )
    if existing is not None:
        return existing
    row = TaskConfirmation(
        confirmation_id=_new_id("conf_"),
        task_id=task.task_id,
        payload_hash=str(task.payload_hash),
        purpose="high_impact_action",
        status="pending",
        issued_at=now,
        expires_at=now + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
        single_use=True,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        actor=actor,
        action="task.confirmation.requested",
        task=task,
        details={"confirmation_id": row.confirmation_id},
    )
    return row


def _open_approval(
    session: Session,
    task: Task,
    actor: GovernancePrincipal,
    *,
    now: datetime,
) -> WorkflowInstance:
    if not task.approver_employee_id:
        raise OwnerError(
            "accountable_owner_unresolved",
            "an approver employee is required for this decision",
            context={"field": "approver"},
        )
    if (
        task.approver_principal_id in {
            task.requester_principal_id,
            task.owner_principal_id,
            task.executor_principal_id,
        }
    ):
        raise OwnerError(
            "self_approval_forbidden",
            "the configured approver is not independent",
            context={"approver": task.approver_principal_id},
        )
    instance = start_approval(
        session,
        business_type="task",
        business_id=task.id,
        workflow_type="task_approval",
        approver_id=task.approver_employee_id,
    )
    task.workflow_instance_id = instance.id
    session.flush()
    return instance


def _issue_grant_and_queue(
    session: Session,
    task: Task,
    actor: GovernancePrincipal,
    *,
    now: datetime,
    runtime_capabilities_verified: bool,
) -> None:
    record = _decision_record(session, task)
    if record is None:
        raise TaskError(
            "required_input_missing",
            "a persisted decision is required before queueing",
            context={"field": "decision_id"},
        )
    confirmation_ref = None
    if task.confirmation_ref:
        row = _confirmation_row(
            session, task.confirmation_ref.get("confirmation_id")
        )
        if row is not None:
            confirmation_ref = _confirmation_document(row)
    approval_ref = _approval_document(task)

    # Validate every non-grant guard *before* issuing authority.  A rejection
    # here must not leave a decision/grant behind for a task that never queued.
    ctx = _base_context(session, task, actor, now=now)
    if (
        task.execution_mode == "managed_execution"
        and not runtime_capabilities_verified
    ):
        ctx = replace(ctx, mode_capabilities_ok=False)
    _preview_transition(
        task,
        event="task.queued",
        target="queued",
        ctx=replace(
            ctx,
            grant_id="grant_preview",
            grant_status="active",
            grant_unexpired=True,
            grant_task_matches=True,
            grant_revision_matches=True,
            grant_decision_matches=True,
            grant_scope_ok=True,
        ),
    )

    budget, consumption_mode = _clamped_task_budget(task, record)
    executor = resolve_principal(
        session,
        task.executor_principal_id or "",
        principal_type=task.executor_principal_type,
    )
    try:
        grant = issue_grant(
            session,
            principal=executor,
            decision_id=record.decision_id,
            task_id=task.task_id,
            task_revision=int(record.task_revision or 0),
            execution_mode=task.execution_mode,
            capabilities=list(task.requested_capabilities),
            resource_scope=task.resource_scope,
            budget=budget,
            consumption_mode=consumption_mode,
            confirmation_ref=confirmation_ref,
            approval_ref=approval_ref,
            tool_contract_version="1.0.0",
            now=now,
        )
    except GovernanceError as exc:
        raise TaskError(
            exc.reason_code,
            f"grant issuance refused: {exc.reason_code}",
            context=exc.context,
        ) from exc

    task.grant_id = grant.grant_id
    task.grant_status = grant.status
    session.flush()

    ctx = _base_context(session, task, actor, now=now)
    if (
        task.execution_mode == "managed_execution"
        and not runtime_capabilities_verified
    ):
        ctx = replace(ctx, mode_capabilities_ok=False)
    try:
        _perform_transition(
            session,
            task,
            actor,
            event="task.queued",
            target="queued",
            ctx=ctx,
            reason_code="policy_allowed",
            now=now,
        )
    except TaskError:
        # Important 11: never leave an orphan active grant when a queue guard
        # rejects the task.  Revoke it in the same transaction so a caller that
        # catches the error and commits does not leak authority.
        _invalidate_grant(
            session, task, actor, reason_code="task_terminal", now=now
        )
        raise


# ---------------------------------------------------------------------------
# Responsibility / dependencies
# ---------------------------------------------------------------------------


def assign_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    assignee: GovernancePrincipal | None = None,
    executor: GovernancePrincipal | None = None,
    owner: GovernancePrincipal | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Update responsibility fields before execution starts.

    Assignment is an audited fact, not a state transition: it cannot move a
    task across the lifecycle, and it is refused once execution has begun.
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    if is_terminal(task.state):
        _reject_terminal(
            session, task, actor,
            event="task.assigned", target=task.state,
            message="a terminal task cannot be reassigned",
        )
    if expected_revision is not None and task.state_revision != expected_revision:
        raise TaskStateConflict(
            task.task_id, expected=expected_revision, actual=task.state_revision
        )
    if not _actor_authorized(actor, task):
        raise TaskError(
            "task_unauthorized_actor",
            "only the requester/owner or an identity manager may assign",
            context={"actor": actor.principal_id},
        )
    if task.state not in {
        "draft",
        "evaluated",
        "awaiting_clarification",
        "awaiting_confirmation",
        "awaiting_approval",
    }:
        raise TaskError(
            "task_invalid_transition",
            "assignment is only allowed before execution",
            context={"state": task.state},
        )
    changed = False
    if owner is not None:
        _require_actor(owner)
        if owner.principal_type != "person":
            raise OwnerError(
                "accountable_owner_unresolved",
                "the accountable owner must be a person",
                context={"principal_type": owner.principal_type},
            )
        owner_status = _owner_status(session, owner)
        if owner_status != "active":
            # Reject deterministically before mutating.  Writing a disabled
            # owner into an active state would otherwise surface as a raw
            # database IntegrityError (active_state_owner_resolved).
            raise TaskError(
                "task_owner_inactive",
                "the new accountable owner is not active",
                context={
                    "owner": owner.principal_id,
                    "owner_account_status": owner_status,
                    "state": task.state,
                },
            )
        changed = changed or owner.principal_id != task.owner_principal_id
        task.owner_principal_id = owner.principal_id
        task.owner_employee_id = owner.employee_id
        task.owner_account_status = owner_status
        task.owner_escalation = None
    if assignee is not None:
        _require_actor(assignee)
        changed = changed or assignee.principal_id != task.assignee_principal_id
        task.assignee_principal_id = assignee.principal_id
    if executor is not None:
        _require_actor(executor)
        changed = changed or executor.principal_id != task.executor_principal_id
        task.executor_principal_id = executor.principal_id
        task.executor_principal_type = executor.principal_type
        task.executor_employee_id = executor.employee_id
    completeness, incomplete_reason = _record_completeness(task)
    task.record_completeness = completeness
    task.incomplete_reason = incomplete_reason
    session.flush()
    if changed:
        # Important 13: stale authority must not survive a responsibility
        # change.  Revoke the current grant, then re-evaluate an already
        # evaluated task so the new executor/owner gets a fresh decision.
        if task.grant_id:
            _invalidate_grant(
                session, task, actor, reason_code="task_terminal", now=timestamp
            )
            task.grant_id = None
            task.grant_status = None
            session.flush()
        if task.state == "evaluated":
            _reapply_decision_after_reassignment(
                session, task, actor, now=timestamp
            )
        elif task.state in {"awaiting_confirmation", "awaiting_approval"}:
            # The pending gate is re-evaluated by the next confirmation/
            # approval; drop the stale snapshot so it cannot be reused.
            task.confirmation_ref = None
            session.flush()
    _audit(
        session,
        actor=actor,
        action="task.assigned",
        task=task,
        details={
            "owner": task.owner_principal_id,
            "assignee": task.assignee_principal_id,
            "executor": task.executor_principal_id,
            "record_completeness": task.record_completeness,
        },
    )
    enqueue_event(
        session,
        event_type="task.assigned",
        aggregate_type="task",
        aggregate_id=task.task_id,
        idempotency_key=f"task:task.assigned:{task.task_id}:{task.state_revision}",
        payload={"task_id": task.task_id, "state_revision": task.state_revision},
    )
    return task


def add_dependency(
    session: Session,
    *,
    task_id: str,
    depends_on_task_id: str,
    actor: GovernancePrincipal,
    type: str = "blocks",
    now: datetime | None = None,
) -> TaskDependency:
    """Create a dependency, rejecting self-references and cycles."""
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    dependency_task = _load_task(session, depends_on_task_id)
    if type not in {"blocks", "relates_to", "supersedes"}:
        raise DependencyError(
            "task_dependency_invalid",
            f"unknown dependency type {type!r}",
            context={"type": type},
        )
    if task.task_id == dependency_task.task_id:
        raise DependencyError(
            "task_dependency_invalid",
            "a task cannot depend on itself",
            context={"task_id": task.task_id},
        )
    if not _actor_authorized(actor, task):
        raise TaskError(
            "task_unauthorized_actor",
            "only a participant may add a dependency",
            context={"actor": actor.principal_id},
        )
    if task.state in {"dispatched", "running", "verifying"} or is_terminal(task.state):
        raise TaskError(
            "task_invalid_transition",
            "dependencies may only change before execution starts",
            context={"state": task.state},
        )
    if _dependency_reaches(session, dependency_task.task_id, task.task_id):
        raise DependencyError(
            "task_dependency_cycle",
            "the dependency would create a cycle",
            context={
                "task_id": task.task_id,
                "depends_on_task_id": dependency_task.task_id,
            },
        )
    existing = session.scalar(
        select(TaskDependency).where(
            TaskDependency.task_id == task.task_id,
            TaskDependency.depends_on_task_id == dependency_task.task_id,
            TaskDependency.type == type,
        )
    )
    if existing is not None:
        return existing
    row = TaskDependency(
        task_id=task.task_id,
        depends_on_task_id=dependency_task.task_id,
        type=type,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        actor=actor,
        action="task.dependency.added",
        task=task,
        details={
            "depends_on_task_id": dependency_task.task_id,
            "type": type,
        },
    )
    return row


def _dependency_reaches(session: Session, start: str, target: str) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(
            session.scalars(
                select(TaskDependency.depends_on_task_id).where(
                    TaskDependency.task_id == current,
                    TaskDependency.type == "blocks",
                )
            ).all()
        )
    return False


def dependency_blockers(session: Session, task_id: str) -> tuple[str, ...]:
    """Return unsatisfied blocking dependency ids for ``task_id``.

    ``blocks`` dependencies are satisfied only by a task that reached
    ``succeeded``; a terminal failure or cancellation keeps the blocker.
    """
    rows = session.execute(
        select(TaskDependency.depends_on_task_id, Task.state)
        .join(
            Task,
            Task.task_id == TaskDependency.depends_on_task_id,
        )
        .where(
            TaskDependency.task_id == task_id,
            TaskDependency.type == "blocks",
        )
    ).all()
    return tuple(
        depends_on for depends_on, state in rows if state != "succeeded"
    )


# ---------------------------------------------------------------------------
# Attempts / execution facts
# ---------------------------------------------------------------------------


def _assert_lease(
    session: Session,
    task: Task,
    *,
    worker_id: str | None,
    fencing_token: int | None,
    now: datetime,
) -> TaskLease:
    """Require the *current*, unexpired lease and its fencing token.

    Only an unreleased lease is considered, so a recovered/expired lease can
    never be reused by the worker that used to hold it.  A recovery writer must
    acquire a fresh lease (and therefore a higher fencing token) first.
    """
    if worker_id is None or fencing_token is None:
        raise TaskError(
            "task_lease_not_held",
            "a worker lease and fencing token are required",
            context={"task_id": task.task_id},
        )
    lease = session.scalar(
        select(TaskLease)
        .where(
            TaskLease.task_id == task.task_id,
            TaskLease.released_at.is_(None),
        )
        .order_by(TaskLease.id.desc())
        .limit(1)
        .with_for_update()
    )
    if lease is None or lease.worker_id != worker_id:
        raise TaskError(
            "task_lease_not_held",
            "no active lease is held by this worker",
            context={"task_id": task.task_id, "worker_id": worker_id},
        )
    if lease.fencing_token != fencing_token:
        raise TaskError(
            "task_fencing_token_stale",
            "the fencing token is not the current lease",
            context={
                "task_id": task.task_id,
                "current_token": lease.fencing_token,
            },
        )
    if _to_utc(lease.lease_expires_at) <= now:  # type: ignore[operator]
        raise TaskError(
            "task_lease_expired",
            "the lease has expired; recover it before writing execution state",
            context={"task_id": task.task_id},
        )
    return lease


def start_attempt(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    runtime_ref: str | None = None,
    execution_ref: str | None = None,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """``queued -> dispatched`` (T23): the boundary accepted the submission.

    This is the **only** place ``attempt`` increments.  A lease with a current
    fencing token is mandatory, and the mode determines which execution handle
    must be bound (``rtask_*`` for Managed, ``exec_*`` otherwise).
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    _require_execution_producer(actor, task)
    if is_terminal(task.state):
        _reject_terminal(
            session, task, actor,
            event="dispatch.accepted", target="dispatched",
            message="a terminal task cannot be dispatched",
        )
    _assert_lease(
        session, task, worker_id=worker_id, fencing_token=fencing_token, now=timestamp
    )
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )
    _assert_task_budget(session, task, _grant_record(session, task), now=timestamp)
    if expected_revision is not None and task.state_revision != expected_revision:
        raise TaskStateConflict(
            task.task_id, expected=expected_revision, actual=task.state_revision
        )
    if task.execution_mode == "managed_execution":
        if not runtime_ref or not runtime_ref.startswith("rtask_"):
            raise TaskError(
                "task_execution_handle_missing",
                "Managed Execution requires a runtime_task_id",
                context={"execution_mode": task.execution_mode},
            )
    elif not execution_ref or not execution_ref.startswith("exec_"):
        raise TaskError(
            "task_execution_handle_missing",
            "Advisory/Governed execution requires an execution_ref",
            context={"execution_mode": task.execution_mode},
        )
    if task.state != "queued":
        raise TaskError(
            "task_invalid_transition",
            "an attempt can only start from queued",
            context={"state": task.state},
        )
    if task.attempt + 1 > task.max_attempts:
        raise TaskError(
            "retry_budget_exhausted",
            "the attempt budget is exhausted",
            context={"attempt": task.attempt, "max_attempts": task.max_attempts},
        )
    blockers = dependency_blockers(session, task.task_id)
    if blockers:
        raise TaskError(
            "task_dependency_blocked",
            "a blocking dependency has not succeeded",
            context={"blocked_by": list(blockers)},
        )

    previous_attempt = task.attempt
    previous_runtime = task.runtime_task_id
    previous_execution = task.execution_ref
    next_attempt_number = task.attempt + 1
    existing_attempt = session.scalar(
        select(TaskAttempt).where(
            TaskAttempt.task_id == task.task_id,
            TaskAttempt.attempt_number == next_attempt_number,
        )
    )
    if existing_attempt is not None and existing_attempt.state == "abandoned":
        # T33 voided this number; a re-dispatch reuses it instead of creating a
        # duplicate attempt number.  The voiding remains in task_events/audit.
        attempt = existing_attempt
        reuse_snapshot = {
            "state": attempt.state,
            "started_at": attempt.started_at,
            "ended_at": attempt.ended_at,
            "stop_confirmed": attempt.stop_confirmed,
            "result_ref": attempt.result_ref,
            "evidence_refs": list(attempt.evidence_refs or []),
            "runtime_ref": attempt.runtime_ref,
            "execution_ref": attempt.execution_ref,
            "idempotency_key": attempt.idempotency_key,
            "error_code": attempt.error_code,
            "reason": attempt.reason,
        }
        attempt.state = "dispatched"
        attempt.started_at = timestamp
        attempt.ended_at = None
        attempt.stop_confirmed = False
        attempt.result_ref = None
        attempt.evidence_refs = []
        attempt.runtime_ref = runtime_ref
        attempt.execution_ref = execution_ref
        attempt.idempotency_key = idempotency_key
        attempt.error_code = None
        attempt.reason = None
    else:
        reuse_snapshot = None
        attempt = TaskAttempt(
            task_id=task.task_id,
            attempt_number=next_attempt_number,
            state="dispatched",
            started_at=timestamp,
            runtime_ref=runtime_ref,
            execution_ref=execution_ref,
            idempotency_key=idempotency_key,
        )
        session.add(attempt)
    # C3: build the guard context **before** incrementing ``attempt`` so
    # ``attempt_within_max`` compares the number of *opened* attempts
    # (``attempt < max_attempts``) and the final authorised attempt is allowed.
    # T65 keeps the same guard semantics: a retry needs ``attempt < max``.
    # The execution handle is bound before the context so ``execution_handle_bound``
    # sees the submission that was just accepted.
    task.runtime_task_id = runtime_ref
    task.execution_ref = execution_ref
    attempt.attempt_number = next_attempt_number
    with session.no_autoflush:
        ctx = _base_context(session, task, actor, now=timestamp)
    task.attempt = next_attempt_number

    def _restore() -> None:
        task.attempt = previous_attempt
        task.runtime_task_id = previous_runtime
        task.execution_ref = previous_execution
        if reuse_snapshot is None:
            session.expunge(attempt)
        else:
            for key, value in reuse_snapshot.items():
                setattr(attempt, key, value)

    _perform_transition(
        session,
        task,
        actor,
        event="dispatch.accepted",
        target="dispatched",
        ctx=ctx,
        expected_revision=expected_revision,
        reason_code="policy_allowed",
        extra={"attempt": task.attempt, "worker_id": worker_id},
        now=timestamp,
        on_reject=_restore,
    )
    return task


def _current_attempt(session: Session, task: Task) -> TaskAttempt | None:
    return session.scalar(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.task_id)
        .order_by(TaskAttempt.attempt_number.desc())
        .limit(1)
    )


def _close_attempt(
    attempt: TaskAttempt | None,
    *,
    state: str,
    now: datetime,
    stop_confirmed: bool,
    result_ref: str | None = None,
    evidence_refs: Sequence[str] = (),
    error_code: str | None = None,
    reason: str | None = None,
) -> None:
    if attempt is None:
        return
    if attempt.ended_at is not None:
        # A closed attempt is immutable.  The only refinement allowed is the
        # terminal verification outcome of an already-ended ``verifying``
        # attempt (``complete_task``); its closing timestamp/evidence are kept.
        if attempt.state != "verifying" or state == "verifying":
            return
        attempt.state = state
        if stop_confirmed:
            # Terminal refinements may add stop proof (cancelled/timed_out
            # require it) but never remove it.
            attempt.stop_confirmed = True
        if result_ref is not None:
            attempt.result_ref = result_ref
        if evidence_refs:
            attempt.evidence_refs = list(evidence_refs)
        if error_code is not None:
            attempt.error_code = error_code
        if reason is not None:
            attempt.reason = reason
        return
    attempt.state = state
    attempt.ended_at = now
    attempt.stop_confirmed = stop_confirmed
    if result_ref is not None:
        attempt.result_ref = result_ref
    if evidence_refs:
        attempt.evidence_refs = list(evidence_refs)
    if error_code is not None:
        attempt.error_code = error_code
    if reason is not None:
        attempt.reason = reason


def mark_running(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    evidence_ref: str | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """``dispatched -> running`` (T29) on fresh execution evidence."""
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_execution_producer(actor, task)
    _assert_lease(
        session, task, worker_id=worker_id, fencing_token=fencing_token, now=timestamp
    )
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )
    attempt = _current_attempt(session, task)
    if attempt is not None:
        attempt.state = "running"
        session.flush()
    ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        running_evidence=True,
    )
    _perform_transition(
        session,
        task,
        actor,
        event="execution.started",
        target="running",
        ctx=ctx,
        expected_revision=expected_revision,
        extra={"evidence_ref": evidence_ref} if evidence_ref else None,
        now=timestamp,
    )
    return task


def record_attempt_result(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    outcome: str,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    result_ref: str | None = None,
    evidence_refs: Sequence[str] = (),
    stop_confirmed: bool = False,
    error_code: str | None = None,
    reason: str | None = None,
    side_effects_reconciled: bool = False,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Record an execution-boundary result (no model self-report).

    ``outcome`` is one of ``completed`` / ``failed`` / ``timed_out`` / ``lost``
    / ``stopped``.  ``lost`` enters ``unknown`` and revokes the grant in the same
    transaction.  A completed run always goes to ``verifying`` -- never directly
    to success.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_execution_producer(actor, task)
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )
    if is_terminal(task.state):
        _reject_terminal(
            session, task, actor,
            event="execution.completed", target="verifying",
            message="a terminal task cannot accept another execution result",
        )
    refs = tuple(evidence_refs)
    if outcome == "completed":
        verification.validate_result_refs(result_ref, refs, require_result=True)
    elif outcome in {"failed", "timed_out", "stopped"}:
        verification.validate_result_refs(result_ref, refs, require_result=False)
        if not stop_confirmed:
            raise TaskError(
                "stop_not_confirmed",
                "failure/stop/timed-out requires confirmed stop evidence",
                context={"outcome": outcome},
            )
    elif outcome != "lost":
        raise TaskError(
            "required_input_missing",
            f"unknown execution outcome {outcome!r}",
            context={"outcome": outcome},
        )

    attempt = _current_attempt(session, task)
    if outcome == "completed":
        # Important 3: T36 must not accept a completed run on a revoked,
        # expired or otherwise invalid grant/gate.
        _assert_completion_authority(session, task, now=timestamp)
    result = {
        **dict(task.result or {}),
        "outcome": outcome,
        "result_ref": result_ref,
        "evidence_refs": list(refs),
        "stop_confirmed": stop_confirmed,
        "stop_evidence_refs": list(refs) if stop_confirmed else [],
        "side_effects_reconciled": side_effects_reconciled,
        "error_code": error_code,
        "verification": {
            "result": "pending",
            "verifier_id": None,
            "verified_at": None,
            "evidence_refs": [],
        },
    }
    task.result = result
    task.stop_confirmed = bool(stop_confirmed)
    session.flush()

    ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        result_present=bool(result_ref),
        evidence_present=bool(refs),
        stop_confirmed=stop_confirmed,
        stop_evidence=bool(refs) if stop_confirmed else False,
        side_effects_reconciled=side_effects_reconciled,
        loss_evidence=(outcome == "lost"),
        running_evidence=True,
    )

    state = task.state
    if outcome == "lost":
        if state not in {"dispatched", "running", "cancel_requested"}:
            raise TaskError(
                "task_invalid_transition",
                "execution can only be lost from dispatched/running/cancel_requested",
                context={"state": state},
            )
        event = "execution.lost"
        _invalidate_grant(
            session, task, actor, reason_code="execution_evidence_lost", now=timestamp
        )
        _perform_transition(
            session, task, actor, event=event, target="unknown", ctx=ctx,
            expected_revision=expected_revision, reason_code="stop_not_confirmed",
            now=timestamp,
        )
        return task

    if outcome == "completed":
        if state not in {"running", "cancel_requested"}:
            raise TaskError(
                "task_invalid_transition",
                "completion can only be reported from running/cancel_requested",
                context={"state": state},
            )
        event = "execution.completed"
        _perform_transition(
            session, task, actor, event=event, target="verifying", ctx=ctx,
            expected_revision=expected_revision, now=timestamp,
        )
        _close_attempt(
            attempt, state="verifying", now=timestamp, stop_confirmed=False,
            result_ref=result_ref, evidence_refs=refs,
        )
        session.flush()
        return task

    if outcome == "stopped":
        if state != "cancel_requested":
            raise TaskError(
                "task_invalid_transition",
                "a stop is only a terminal fact after a cancel request",
                context={"state": state},
            )
        ctx = replace(ctx, side_effects_reconciled=side_effects_reconciled)
        _invalidate_grant(
            session, task, actor, reason_code="task_cancelled", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="execution.stopped", target="cancelled",
            ctx=ctx, expected_revision=expected_revision, now=timestamp,
        )
        _close_attempt(
            attempt, state="cancelled", now=timestamp, stop_confirmed=True,
            result_ref=result_ref, evidence_refs=refs, reason=reason,
        )
        session.flush()
        return task

    # failed / timed_out
    if state not in {"dispatched", "running"}:
        raise TaskError(
            "task_invalid_transition",
            f"{outcome} can only be reported from dispatched/running",
            context={"state": state},
        )
    event = "execution.failed" if outcome == "failed" else "execution.timed_out"
    target = "failed" if outcome == "failed" else "timed_out"
    _invalidate_grant(
        session, task, actor, reason_code="attempt_failed", now=timestamp
    )
    _perform_transition(
        session, task, actor, event=event, target=target, ctx=ctx,
        expected_revision=expected_revision, reason_code=error_code,
        now=timestamp,
    )
    _close_attempt(
        attempt, state=outcome, now=timestamp, stop_confirmed=True,
        result_ref=result_ref, evidence_refs=refs, error_code=error_code,
        reason=reason,
    )
    session.flush()
    return task


def _invalidate_grant(
    session: Session,
    task: Task,
    actor: GovernancePrincipal | None,
    *,
    reason_code: str,
    now: datetime,
) -> None:
    """Force the bound grant out of ``active`` within this transaction (S-10).

    Active grants are revoked through the governance service (which re-checks
    authority and audits).  A grant that already expired is converged to
    ``revoked`` because the frozen contract allows only ``revoked``/``consumed``
    on a terminal/unknown task; the fallback is audited explicitly.
    """
    if not task.grant_id:
        task.grant_status = None
        return
    grant = _grant_record(session, task)
    if grant is None:
        task.grant_status = "revoked"
        return
    if grant.status in {"revoked", "consumed"}:
        task.grant_status = grant.status
        session.flush()
        return
    if grant.status == "expired":
        grant.status = "revoked"
        grant.usable = False
        grant.revoked_at = now
        grant.revoked_by_principal_id = actor.principal_id if actor else None
        grant.revocation_reason = reason_code
        task.grant_status = "revoked"
        session.flush()
        _audit(
            session,
            actor=actor,
            action="task.grant.invalidated",
            task=task,
            details={
                "grant_id": grant.grant_id,
                "reason_code": reason_code,
                "previous_status": "expired",
            },
        )
        return
    try:
        subject = resolve_principal(
            session, grant.subject_id, principal_type=grant.subject_type
        )
        revoked = revoke_grant(
            session,
            grant_id=grant.grant_id,
            reason_code=reason_code,
            principal=subject,
            now=now,
        )
        task.grant_status = revoked.status
    except (GovernanceError, TaskError):
        # Fail closed: mark the snapshot revoked even if the governance layer
        # cannot resolve the subject (the grant is no longer usable).
        grant.status = "revoked"
        grant.usable = False
        grant.revoked_at = now
        grant.revocation_reason = reason_code
        task.grant_status = "revoked"
        session.flush()
        _audit(
            session,
            actor=actor,
            action="task.grant.invalidated",
            task=task,
            details={
                "grant_id": grant.grant_id,
                "reason_code": reason_code,
                "fallback": "terminal_grant_convergence",
            },
        )
    session.flush()


def record_dispatch_failure(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    outcome: str,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    evidence_refs: Sequence[str] = (),
    reason: str | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Record a submission-phase result for a ``queued`` task (T27/T28/T66-T69).

    ``timeout``/``unknown`` never guess "not executed": they enter ``unknown``
    with honest unknown evidence.  For a never-dispatched task
    (``attempt = 0``) ``rejected``/``unavailable`` confirm no side effects and
    ``deadline_exceeded`` closes as T27.  For a requeued task (``attempt >= 1``)
    the attempt=0-only edges are invalid, so the same outcomes escalate to
    ``needs_human`` (T26) instead of rewriting the task or revoking its grant
    without a way forward; ``unsupported`` is T69 in both cases.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_execution_producer(actor, task)
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )
    if task.state != "queued":
        raise TaskError(
            "task_invalid_transition",
            "submission results apply only to a queued task",
            context={"state": task.state},
        )
    if outcome not in {
        "rejected",
        "unavailable",
        "unsupported",
        "timeout",
        "unknown",
        "deadline_exceeded",
    }:
        raise TaskError(
            "required_input_missing",
            f"unknown submission outcome {outcome!r}",
            context={"outcome": outcome},
        )
    refs = tuple(evidence_refs)
    if refs:
        verification.validate_result_refs(None, refs, require_result=False)
    attempt_zero = task.attempt == 0
    unknown_outcome = outcome in {"timeout", "unknown"}
    never_started_claim = attempt_zero and not unknown_outcome
    if unknown_outcome:
        # C10 / Important 10: do not fabricate "never dispatched" or a stop.
        stop_confirmed = False
        no_side_effects = False
        side_effects_reconciled = False
        stored_refs = list(refs)
    elif never_started_claim:
        stop_confirmed = True
        no_side_effects = True
        side_effects_reconciled = True
        stored_refs = list(refs) or [_EVIDENCE_NEVER_STARTED]
    else:
        # A requeued task already has an attempt: T27/T28/T66 (attempt=0 only)
        # would either reject after a partial write or strand the task with a
        # revoked grant.  It is escalated to a human/unknown instead, with no
        # fabricated stop and without overwriting the persisted reconciliation.
        stop_confirmed = False
        no_side_effects = bool((task.result or {}).get("no_side_effects", False))
        side_effects_reconciled = bool(
            (task.result or {}).get("side_effects_reconciled", False)
        )
        stored_refs = list(refs)

    if outcome in {"rejected", "unavailable", "deadline_exceeded"} and not attempt_zero:
        event, target = "human.escalation_required", "needs_human"
    elif outcome == "rejected":
        event, target = "dispatch.rejected", "failed"
    elif outcome == "unavailable":
        event, target = "dispatch.unavailable", "failed"
    elif outcome == "unsupported":
        event, target = "dispatch.unsupported", "needs_human"
    elif outcome == "timeout":
        event, target = "dispatch.timeout", "unknown"
    elif outcome == "unknown":
        event, target = "dispatch.unknown", "unknown"
    else:  # deadline_exceeded with attempt=0
        event, target = "task.deadline_exceeded", "timed_out"

    ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        stop_confirmed=stop_confirmed,
        stop_evidence=bool(stored_refs) if stop_confirmed else False,
        no_side_effects=no_side_effects,
        side_effects_reconciled=side_effects_reconciled,
    )
    if outcome == "unsupported":
        ctx = replace(ctx, runtime_supported=False, escalation_justified=True)
    elif outcome in {"timeout", "unknown"}:
        ctx = replace(ctx, dispatch_outcome_unknown=True)
    elif outcome == "deadline_exceeded" and attempt_zero:
        ctx = replace(ctx, deadline_passed=True)
    elif target == "needs_human":
        ctx = replace(ctx, escalation_justified=True)
    _preview_transition(
        task, event=event, target=target, ctx=ctx,
        expected_revision=expected_revision,
    )

    result: dict[str, Any] = {
        **dict(task.result or {}),
        "outcome": outcome,
        "evidence_refs": stored_refs,
        "error_code": reason,
    }
    if unknown_outcome:
        result.update(
            stop_confirmed=False,
            stop_evidence_refs=[],
            no_side_effects=False,
            side_effects_reconciled=False,
        )
    elif never_started_claim:
        result.update(
            stop_confirmed=True,
            stop_evidence_refs=stored_refs,
            no_side_effects=True,
            side_effects_reconciled=True,
        )
    else:
        result.update(stop_confirmed=False, stop_evidence_refs=[])
    task.stop_confirmed = stop_confirmed
    task.result = result
    session.flush()

    if target in TASK_TERMINAL_STATES or target in {
        "unknown",
        "failed",
        "needs_human",
    }:
        _invalidate_grant(
            session,
            task,
            actor,
            reason_code=(
                "execution_evidence_lost" if target == "unknown" else "task_terminal"
            ),
            now=timestamp,
        )
    _perform_transition(
        session,
        task,
        actor,
        event=event,
        target=target,
        ctx=replace(ctx, state_revision=task.state_revision),
        expected_revision=expected_revision,
        reason_code=reason,
        extra={"evidence_refs": list(refs)} if refs else None,
        now=timestamp,
    )
    if target == "needs_human":
        task.needs_human_origin = "queued"
    session.flush()
    return task


# ---------------------------------------------------------------------------
# Cancellation / stop
# ---------------------------------------------------------------------------

def request_cancel(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    cancel_id: str | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Register a cancellation request.

    Pre-execution tasks become ``cancelled`` directly (confirmed never started);
    dispatched/running/verifying tasks only become ``cancel_requested`` -- a
    request is never treated as a stop.
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    if task.state in {"cancelled", "cancel_requested"}:
        return task
    if is_terminal(task.state):
        _reject_terminal(
            session, task, actor,
            event="task.cancel_requested", target="cancel_requested",
            message="a terminal task cannot be cancelled",
        )
    if not (
        actor.principal_id
        in {
            task.requester_principal_id,
            task.owner_principal_id,
            task.assignee_principal_id,
            task.executor_principal_id,
        }
        or _is_privileged(actor)
        or actor.principal_type in {"service", "runtime"}
    ):
        raise TaskError(
            "task_unauthorized_actor",
            "only a participant or an authorised service may cancel",
            context={"actor": actor.principal_id},
        )
    if task.state in {
        "draft",
        "awaiting_clarification",
        "awaiting_confirmation",
        "awaiting_approval",
    }:
        task.stop_confirmed = True
        task.result = {
            **dict(task.result or {}),
            "outcome": "cancelled",
            "stop_confirmed": True,
            "stop_evidence_refs": [_EVIDENCE_NEVER_STARTED],
            "no_side_effects": True,
            "side_effects_reconciled": True,
        }
        session.flush()
        ctx = _base_context(session, task, actor, now=timestamp)
        _invalidate_grant(
            session, task, actor, reason_code="task_cancelled", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="task.cancelled", target="cancelled",
            ctx=ctx, expected_revision=expected_revision,
            reason_code="never_dispatched", now=timestamp,
        )
        return task

    ctx = _base_context(session, task, actor, now=timestamp)
    _perform_transition(
        session, task, actor, event="task.cancel_requested",
        target="cancel_requested", ctx=ctx, expected_revision=expected_revision,
        extra={"cancel_id": cancel_id} if cancel_id else None, now=timestamp,
    )
    return task


def confirm_stop(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    evidence_refs: Sequence[str],
    stop_confirmed: bool = True,
    side_effects_reconciled: bool | None = None,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Record actual stop evidence and close the task.

    ``cancel_requested -> cancelled`` requires ``stop_confirmed``, at least one
    stop evidence reference **and** an explicitly reconciled side-effect fact;
    omitting ``side_effects_reconciled`` fails closed instead of defaulting to
    true.  From ``running``/``dispatched`` the cancel request is registered
    first in the same transaction, so the audit trail still distinguishes
    "requested" from "stopped".  From ``unknown`` the reconciliation path (T56)
    is used; from ``needs_human`` T63 is used.

    Every guard is previewed before the first write: a failed/terminal task or
    an inapplicable state rejects cleanly without persisting
    ``stop_confirmed``/``side_effects_reconciled``, revoking a grant or closing
    an attempt.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )
    if task.state == "cancelled":
        return task
    if is_terminal(task.state):
        _reject_terminal(
            session, task, actor,
            event="execution.stopped", target="cancelled",
            message="a terminal task cannot be stopped again",
        )
    refs = _validate_evidence_refs(evidence_refs)
    if not stop_confirmed:
        raise TaskError(
            "stop_not_confirmed",
            "a stop cannot be claimed without stop_confirmed=true",
            context={"task_id": task.task_id},
        )
    reconciled = side_effects_reconciled is True
    previous = task.state
    base = _base_context(session, task, actor, now=timestamp)
    stop_ctx = replace(
        base,
        stop_confirmed=True,
        stop_evidence=True,
        side_effects_reconciled=reconciled,
    )
    two_step = previous in {"running", "dispatched", "queued", "verifying"}
    if two_step:
        _preview_transition(
            task, event="task.cancel_requested", target="cancel_requested",
            ctx=base, expected_revision=expected_revision,
        )
        _preview_transition(
            task, event="execution.stopped", target="cancelled",
            source="cancel_requested",
            ctx=replace(stop_ctx, state_revision=task.state_revision + 1),
        )
    elif previous == "cancel_requested":
        _preview_transition(
            task, event="execution.stopped", target="cancelled", ctx=stop_ctx,
            expected_revision=expected_revision,
        )
    elif previous == "unknown":
        _preview_transition(
            task, event="reconcile.stopped", target="cancelled", ctx=stop_ctx,
            expected_revision=expected_revision,
        )
    elif previous == "needs_human":
        _preview_transition(
            task, event="human.stop_confirmed", target="cancelled", ctx=stop_ctx,
            expected_revision=expected_revision,
        )
    else:
        raise TaskError(
            "task_invalid_transition",
            f"stop evidence is not applicable in state {previous!r}",
            context={"state": previous},
        )

    # Only now mutate: the stop fact is persisted for every accepted branch so
    # the database backstops (terminal_stop_confirmed, denied-after-attempt)
    # never see an inconsistent row.
    _invalidate_grant(
        session, task, actor, reason_code="task_cancelled", now=timestamp
    )
    task.stop_confirmed = True
    task.result = {
        **dict(task.result or {}),
        "outcome": "cancelled",
        "stop_confirmed": True,
        "stop_evidence_refs": list(refs),
        "side_effects_reconciled": reconciled,
    }
    session.flush()
    if two_step:
        _perform_transition(
            session, task, actor, event="task.cancel_requested",
            target="cancel_requested",
            ctx=replace(base, state_revision=task.state_revision),
            expected_revision=expected_revision, now=timestamp,
        )
        _perform_transition(
            session, task, actor, event="execution.stopped", target="cancelled",
            ctx=replace(stop_ctx, state_revision=task.state_revision),
            extra={"evidence_refs": list(refs)}, now=timestamp,
        )
    elif previous == "cancel_requested":
        _perform_transition(
            session, task, actor, event="execution.stopped", target="cancelled",
            ctx=replace(stop_ctx, state_revision=task.state_revision),
            expected_revision=expected_revision,
            extra={"evidence_refs": list(refs)}, now=timestamp,
        )
    elif previous == "unknown":
        _perform_transition(
            session, task, actor, event="reconcile.stopped", target="cancelled",
            ctx=replace(stop_ctx, state_revision=task.state_revision),
            expected_revision=expected_revision, now=timestamp,
        )
    else:  # needs_human
        _perform_transition(
            session, task, actor, event="human.stop_confirmed", target="cancelled",
            ctx=replace(stop_ctx, state_revision=task.state_revision),
            expected_revision=expected_revision, now=timestamp,
        )
    _close_attempt(
        _current_attempt(session, task),
        state="cancelled",
        now=timestamp,
        stop_confirmed=True,
        evidence_refs=refs,
    )
    session.flush()
    return task


# ---------------------------------------------------------------------------
# Completion verification
# ---------------------------------------------------------------------------


def complete_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    result_ref: str,
    evidence_refs: Sequence[str],
    evidence: Iterable[EvidenceRecord | Mapping[str, Any]],
    allow_partial: bool = False,
    owner_accepted_partial: bool = False,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Verify structured evidence and move ``verifying`` to a terminal result.

    The verifier recomputes every criterion from the submitted evidence; an
    agent/runtime self-report is rejected outright.  A passing run goes to
    ``succeeded`` only after all criteria pass.  ``partial_success`` needs an
    explicitly policy-allowed subset **and** owner acceptance.
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    if task.state != "verifying":
        raise TaskError(
            "task_invalid_transition",
            "completion verification requires the verifying state",
            context={"state": task.state},
        )
    if not (
        _actor_authorized(actor, task)
        or actor.principal_type in {"service", "runtime"}
    ):
        raise TaskError(
            "task_unauthorized_actor",
            "only a participant or an authorised verifier may complete the task",
            context={"actor": actor.principal_id},
        )
    if (
        task.executor_principal_id == actor.principal_id
        and actor.principal_type != "person"
    ):
        raise VerificationError(
            "task_verification_self_report",
            "an agent cannot certify its own completion",
            context={"actor": actor.principal_id},
        )
    verification.validate_result_refs(result_ref, evidence_refs, require_result=True)
    # Important 3: never declare success on an expired/revoked grant or gate.
    _assert_completion_authority(session, task, now=timestamp)
    evidence_records = [
        item if isinstance(item, EvidenceRecord) else EvidenceRecord.from_mapping(item)
        for item in evidence
    ]
    outcome = verification.verify_completion(
        task.goal, evidence_records, allow_partial=allow_partial
    )
    if owner_accepted_partial and actor.principal_id != task.owner_principal_id:
        # Partial completion is an acceptance of unfinished work: only the
        # accountable owner may grant it, never the executor/assignee.
        raise TaskError(
            "task_unauthorized_actor",
            "only the accountable owner may accept a partial result",
            context={"actor": actor.principal_id, "owner": task.owner_principal_id},
        )
    evidence_tuple = tuple(evidence_refs)
    verifier_ids = sorted({record.verifier_id for record in evidence_records})
    verification_doc = {
        "result": outcome.result,
        "verifier_id": verifier_ids[0] if verifier_ids else None,
        "verified_at": _rfc3339(timestamp),
        "evidence_refs": list(evidence_tuple),
    }
    task.result = {
        **dict(task.result or {}),
        "outcome": outcome.result,
        "result_ref": result_ref,
        "evidence_refs": list(evidence_tuple),
        "verification": verification_doc,
        "all_criteria_passed": outcome.result == "passed",
        "partial_allowed": allow_partial,
        "owner_accepted_partial": owner_accepted_partial,
        "failed_criteria": list(outcome.failed_criteria),
        "verified_at": _rfc3339(timestamp),
    }
    session.flush()
    ctx = _base_context(session, task, actor, now=timestamp)

    if outcome.result == "passed":
        _invalidate_grant(
            session, task, actor, reason_code="task_terminal", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="verification.passed", target="succeeded",
            ctx=ctx, expected_revision=expected_revision,
            extra={"verifier": verification_doc["verifier_id"]}, now=timestamp,
        )
    elif outcome.result == "partial" and owner_accepted_partial:
        _invalidate_grant(
            session, task, actor, reason_code="task_terminal", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="verification.partial",
            target="partial_success", ctx=ctx,
            expected_revision=expected_revision,
            extra={"failed_criteria": list(outcome.failed_criteria)},
            now=timestamp,
        )
    elif outcome.result == "inconclusive":
        ctx = replace(ctx, verification_inconclusive=True)
        _perform_transition(
            session, task, actor, event="verification.inconclusive",
            target="needs_human", ctx=ctx,
            expected_revision=expected_revision,
            reason_code="evidence_missing", now=timestamp,
        )
        # T45: the attempt has ended and its result is pending a human
        # verification (T60 becomes the only resumable origin).
        task.needs_human_origin = "verifying"
        task.result = {
            **dict(task.result or {}),
            "attempt_finished_pending_verification": True,
        }
        _close_attempt(
            _current_attempt(session, task),
            state="verifying",
            now=timestamp,
            stop_confirmed=False,
            result_ref=result_ref,
            evidence_refs=evidence_tuple,
        )
        session.flush()
        return task
    else:
        # A rejected partial/failed verification is a ``verification.failed``
        # edge; the persisted shape must say so even when the raw outcome was
        # ``partial`` (Important 12).
        verification_doc = {**verification_doc, "result": "failed"}
        task.result = {
            **dict(task.result or {}),
            "verification": verification_doc,
        }
        session.flush()
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            verification_result="failed",
        )
        _invalidate_grant(
            session, task, actor, reason_code="task_terminal", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="verification.failed", target="failed",
            ctx=ctx, expected_revision=expected_revision,
            reason_code=outcome.reason_code, now=timestamp,
        )
    _close_attempt(
        _current_attempt(session, task),
        state="succeeded" if outcome.result in {"passed", "partial"} else "failed",
        now=timestamp,
        stop_confirmed=True,
        result_ref=result_ref,
        evidence_refs=evidence_tuple,
    )
    session.flush()
    return task


def fail_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    reason: str,
    evidence_refs: Sequence[str] = (),
    stop_confirmed: bool = False,
    error_code: str | None = None,
    dispatch_outcome: str = "rejected",
    worker_id: str | None = None,
    fencing_token: int | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Record a deterministic failure without pretending a result exists.

    From ``queued`` only pre-dispatch failure outcomes are allowed; those are
    delegated to :func:`record_dispatch_failure`, which produces T28/T66/T69
    for a never-dispatched task and T26/T69 for a requeued (``attempt >= 1``)
    task.  From
    ``dispatched``/``running`` stop evidence is required.  From ``needs_human``
    a human declaration requires ``attempt >= 1`` (F39).
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )
    if is_terminal(task.state):
        _reject_terminal(
            session, task, actor,
            event="execution.failed", target="failed",
            message="a terminal task cannot be failed again",
        )
    if task.state == "queued":
        # Important 2/round-2 5: the queued arm is exactly the submission-result
        # path.  A requeued task (attempt >= 1) is routed by
        # ``record_dispatch_failure`` to T26/T69 instead of the attempt=0-only
        # T27/T28/T66 edges.
        if dispatch_outcome not in {"rejected", "unavailable", "unsupported"}:
            raise TaskError(
                "required_input_missing",
                "queued failures use dispatch.rejected/unavailable/unsupported",
                context={"dispatch_outcome": dispatch_outcome},
            )
        return record_dispatch_failure(
            session,
            task_id=task.task_id,
            actor=actor,
            outcome=dispatch_outcome,
            worker_id=worker_id,
            fencing_token=fencing_token,
            evidence_refs=evidence_refs,
            reason=reason or error_code,
            expected_revision=expected_revision,
            now=timestamp,
        )
    refs = tuple(evidence_refs)
    if refs:
        verification.validate_result_refs(None, refs, require_result=False)
    previous = task.state
    if previous in {"dispatched", "running"}:
        if not stop_confirmed:
            raise TaskError(
                "stop_not_confirmed",
                "an execution failure requires stop_confirmed=true",
                context={"state": previous},
            )
        event, target = "execution.failed", "failed"
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            stop_confirmed=True,
            stop_evidence=bool(refs),
        )
    elif previous == "verifying":
        event, target = "verification.failed", "failed"
        ctx = _base_context(session, task, actor, now=timestamp)
    elif previous == "needs_human":
        if task.attempt < 1:
            raise TaskError(
                "task_attempt_missing",
                "an attempt=0 human failure must be registered as cancelled/denied",
                context={"attempt": task.attempt},
            )
        event, target = "human.declared_failed", "failed"
        ctx = _base_context(session, task, actor, now=timestamp)
    else:
        raise TaskError(
            "task_invalid_transition",
            f"failure is not a defined edge from {previous!r}",
            context={"state": previous},
        )
    # Every rejection above happens before this write: a caught failure leaves
    # the task exactly as it was.
    _preview_transition(
        task, event=event, target=target, ctx=ctx,
        expected_revision=expected_revision,
    )
    task.stop_confirmed = bool(stop_confirmed)
    task.result = {
        **dict(task.result or {}),
        "outcome": "failed",
        "result_ref": None,
        "evidence_refs": list(refs),
        "stop_confirmed": bool(stop_confirmed),
        "stop_evidence_refs": list(refs) if stop_confirmed else [],
        "error_code": error_code,
        "reason": reason,
    }
    session.flush()
    _invalidate_grant(
        session,
        task,
        actor,
        reason_code=(
            "attempt_failed" if previous != "verifying" else "task_terminal"
        ),
        now=timestamp,
    )
    _perform_transition(
        session, task, actor, event=event, target=target,
        ctx=replace(ctx, state_revision=task.state_revision),
        expected_revision=expected_revision, reason_code=error_code,
        now=timestamp,
    )
    _close_attempt(
        _current_attempt(session, task),
        state="failed",
        now=timestamp,
        stop_confirmed=bool(stop_confirmed),
        evidence_refs=refs,
        error_code=error_code,
        reason=reason,
    )
    session.flush()
    return task


# ---------------------------------------------------------------------------
# Retry / recovery / takeover
# ---------------------------------------------------------------------------


def _fresh_retry_decision(
    session: Session,
    task: Task,
    *,
    actor: GovernancePrincipal,
    now: datetime,
    capability: str | None = None,
) -> tuple[GovernanceDecision, ExecutionGrant, GovernanceDecisionRecord | None]:
    """Evaluate a retry decision (revision >= 2, purpose=retry) and grant it.

    The task is deliberately **not** mutated: a caller must first prove the
    transition guards, then bind the returned decision/grant.  A guard failure
    therefore cannot leave ``task.decision_id``/``grant_id`` pointing at an
    unapplied decision, and the orphan grant is revoked by the caller.
    """
    record = _decision_record(session, task)
    capability = capability or (task.requested_capabilities or [None])[0]
    if not capability:
        raise TaskError(
            "capability_not_granted",
            "the task has no capability to re-evaluate",
            context={"field": "requested_capabilities"},
        )
    executor = resolve_principal(
        session,
        task.executor_principal_id or "",
        principal_type=task.executor_principal_type,
    )
    decision = evaluate(
        session,
        EvaluationRequest(
            principal=executor,
            capability=capability,
            task_id=task.task_id,
            task_revision=task.state_revision + 1,
            task_idempotency_key=task.idempotency_key,
            resource_scope=task.resource_scope,
            priority=task.priority,
            execution_mode=task.execution_mode,
            risk="low",
            confirmation_ref=_live_confirmation_ref(session, task, now=now),
            approval_ref=_live_approval_ref(task, now=now),
            payload_hash=task.payload_hash,
            policy_set_id=task.policy_set_id or DEFAULT_POLICY_SET_ID,
            policy_version=task.policy_version,
            correlation_id=task.correlation_id,
            decision_purpose="retry",
            supersedes_decision_id=record.decision_id if record is not None else None,
        ),
        now=now,
        evidence_verifier=TaskEvidenceVerifier(session, task_id=task.task_id),
    )
    if decision.decision_type not in {"allow", "managed_execution_required"}:
        raise TaskError(
            "task_retry_requires_fresh_authorization",
            "a retry must be authorised by a fresh allow decision",
            context={"decision_type": decision.decision_type},
        )
    new_record = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == decision.decision_id
        )
    )
    budget, consumption_mode = _clamped_task_budget(task, new_record)
    grant = issue_grant(
        session,
        principal=executor,
        decision_id=decision.decision_id,
        task_id=task.task_id,
        task_revision=int(
            (new_record.task_revision if new_record else task.state_revision + 1) or 0
        ),
        execution_mode=task.execution_mode,
        capabilities=list(task.requested_capabilities),
        resource_scope=task.resource_scope,
        budget=budget,
        consumption_mode=consumption_mode,
        confirmation_ref=_live_confirmation_ref(session, task, now=now),
        approval_ref=_live_approval_ref(task, now=now),
        tool_contract_version="1.0.0",
        now=now,
    )
    # The decision and grant are returned rather than bound here: the task is
    # still in ``failed`` (or ``unknown``), where an *active* grant is forbidden
    # by the database invariant.  The caller binds them after the transition.
    return decision, grant, new_record


def retry_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    capability: str | None = None,
    action_safely_retryable: bool | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Bounded retry of a ``failed`` task (T65) or escalation (T70).

    Retry requires ``attempt >= 1``, a remaining budget and a **fresh**
    decision/grant; the budget is decremented exactly once.  An unsafe action,
    an exhausted budget or a task that never started goes to ``needs_human``
    (T70) instead of being replayed.
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    if task.state != "failed":
        raise TaskError(
            "task_invalid_transition",
            "only a failed task can be retried",
            context={"state": task.state},
        )
    if not _actor_authorized(actor, task):
        raise TaskError(
            "task_unauthorized_actor",
            "only a participant may retry a task",
            context={"actor": actor.principal_id},
        )
    retryable = task.retry_safe if action_safely_retryable is None else action_safely_retryable
    if task.attempt < 1:
        # F39: never-started failures cannot enter the T64/T70 loop.
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            retry_exhausted=True,
            escalation_justified=True,
        )
        _perform_transition(
            session, task, actor, event="task.retry_exhausted",
            target="needs_human", ctx=ctx, expected_revision=expected_revision,
            reason_code="retry_budget_exhausted", now=timestamp,
        )
        task.needs_human_origin = "failed"
        session.flush()
        return task
    if (
        not retryable
        or task.attempt >= task.max_attempts
        or task.retry_budget_remaining <= 0
    ):
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            retry_exhausted=True,
            escalation_justified=True,
        )
        _perform_transition(
            session, task, actor, event="task.retry_exhausted",
            target="needs_human", ctx=ctx, expected_revision=expected_revision,
            reason_code=(
                "task_unsafe_retry_forbidden" if not retryable else "retry_budget_exhausted"
            ),
            now=timestamp,
        )
        task.needs_human_origin = "failed"
        session.flush()
        return task

    # C7: only a reconciled failure may be replayed.  A failure without
    # persisted reconciliation evidence escalates to a human instead of issuing
    # a new decision/grant.
    base_ctx = _base_context(session, task, actor, now=timestamp)
    if not base_ctx.no_side_effects:
        ctx = replace(
            base_ctx,
            retry_exhausted=True,
            escalation_justified=True,
        )
        _perform_transition(
            session, task, actor, event="task.retry_exhausted",
            target="needs_human", ctx=ctx, expected_revision=expected_revision,
            reason_code="task_reconcile_evidence_missing", now=timestamp,
        )
        task.needs_human_origin = "failed"
        session.flush()
        return task

    try:
        decision, grant, rule = _fresh_retry_decision(
            session, task, actor=actor, now=timestamp, capability=capability
        )
    except GovernanceError as exc:
        raise TaskError(
            exc.reason_code, f"retry authorisation refused: {exc.reason_code}",
            context=exc.context,
        ) from exc

    budget_before = task.retry_budget_remaining
    ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        decision_purpose="retry",
        decision_revision=int(decision.decision_revision or 2),
        decision_supersedes=(rule.supersedes_decision_id if rule else None),
        fresh_decision_and_grant=True,
        old_grant_invalidated=True,
        # The guard reads the pre-decrement balance; the stored/reported value
        # is the post-decrement balance (the last allowed retry may be 0).
        retry_budget_remaining=budget_before,
        grant_id=grant.grant_id,
        grant_status="active",
        grant_task_matches=True,
        grant_revision_matches=True,
        grant_decision_matches=True,
        grant_attempt_matches=True,
        grant_unexpired=True,
        grant_scope_ok=True,
        gates_satisfied=(
            _live_confirmation_ref(session, task, now=timestamp) is not None
            or not bool((rule.constraints if rule else {}).get("required_confirmation"))
        )
        and (
            _live_approval_ref(task, now=timestamp) is not None
            or not bool((rule.constraints if rule else {}).get("required_approval"))
        ),
    )
    try:
        _preview_transition(
            task, event="task.retry_authorized", target="queued",
            ctx=ctx, expected_revision=expected_revision,
        )
    except TaskError:
        _revoke_unbound_grant(
            session,
            grant,
            reason_code="task_terminal",
            now=timestamp,
        )
        raise

    previous_decision = (
        task.decision_id,
        task.decision_revision,
        task.decision_type,
        task.decision_purpose,
        task.policy_set_id,
        task.policy_version,
        task.policy_content_hash,
        task.grant_id,
        task.grant_status,
        task.retry_budget_remaining,
        dict(task.result or {}),
    )
    task.decision_id = decision.decision_id
    task.decision_revision = decision.decision_revision
    task.decision_type = decision.decision_type
    task.decision_purpose = "retry"
    if rule is not None:
        task.policy_set_id = rule.policy_set_id
        task.policy_version = rule.policy_version
        task.policy_content_hash = rule.policy_content_hash
    task.grant_id = grant.grant_id
    task.grant_status = grant.status
    task.retry_budget_remaining = max(0, budget_before - 1)
    task.result = {
        **dict(task.result or {}),
        "outcome": "none",
        "no_side_effects": False,
        "side_effects_reconciled": True,
    }
    session.flush()
    try:
        _perform_transition(
            session, task, actor, event="task.retry_authorized", target="queued",
            ctx=ctx,
            expected_revision=expected_revision,
            extra={"retry_budget_remaining": task.retry_budget_remaining},
            now=timestamp,
        )
    except TaskError:
        _revoke_unbound_grant(
            session, grant, reason_code="task_terminal",
            now=timestamp,
        )
        (
            task.decision_id,
            task.decision_revision,
            task.decision_type,
            task.decision_purpose,
            task.policy_set_id,
            task.policy_version,
            task.policy_content_hash,
            task.grant_id,
            task.grant_status,
            task.retry_budget_remaining,
            previous_result,
        ) = previous_decision
        task.result = previous_result
        session.flush()
        raise
    return task


def reconcile_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    outcome: str,
    evidence_refs: Sequence[str] = (),
    stop_confirmed: bool = False,
    no_side_effect_evidence: bool = False,
    capability: str | None = None,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Resolve an ``unknown`` task; never resume execution directly.

    * ``not_found`` (T71) requires ``attempt = 0``, explicit no-side-effect
      evidence and a fresh decision + grant;
    * ``failed`` (T55) / ``stopped`` (T56) / ``timed_out`` (T58) require stop
      evidence;
    * ``unresolved`` (T57) / ``running`` (T72) / ``completed`` (T73) go to
      ``needs_human`` -- a revoked grant never resumes running/verifying.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )
    if task.state != "unknown":
        raise TaskError(
            "task_invalid_transition",
            "reconciliation requires the unknown state",
            context={"state": task.state},
        )
    refs = tuple(evidence_refs)
    if refs:
        verification.validate_result_refs(None, refs, require_result=False)
    if outcome in {"failed", "stopped", "timed_out"}:
        if not stop_confirmed or not refs:
            raise TaskError(
                "stop_not_confirmed" if not stop_confirmed else "evidence_missing",
                "reconciliation terminal facts require stop evidence",
                context={"outcome": outcome},
            )
    elif outcome == "not_found":
        if task.attempt != 0:
            raise TaskError(
                "task_active_attempt_unreconciled",
                "T71 is only valid when no attempt ever started",
                context={"attempt": task.attempt},
            )
        if not no_side_effect_evidence:
            raise TaskError(
                "task_reconcile_evidence_missing",
                "not_found requires explicit no-side-effect evidence",
                context={"outcome": outcome},
            )
    elif outcome not in {"unresolved", "running", "completed"}:
        raise TaskError(
            "required_input_missing",
            f"unknown reconciliation outcome {outcome!r}",
            context={"outcome": outcome},
        )

    base_ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        stop_confirmed=stop_confirmed,
        stop_evidence=bool(refs) if stop_confirmed else False,
        no_side_effect_evidence=no_side_effect_evidence,
    )
    if outcome == "not_found":
        # Validate T71 *before* issuing a decision/grant: an unreconciled
        # owner/record must reject instead of leaving an orphan authority.
        ctx = replace(
            base_ctx,
            no_side_effect_evidence=True,
            fresh_decision_and_grant=True,
            old_grant_invalidated=True,
            decision_purpose="retry",
            decision_revision=int(task.decision_revision or 2),
            grant_id="grant_preview",
            grant_status="active",
            grant_task_matches=True,
            grant_revision_matches=True,
            grant_decision_matches=True,
            grant_attempt_matches=True,
            grant_unexpired=True,
            grant_scope_ok=True,
            retry_budget_remaining=task.retry_budget_remaining,
        )
        _preview_transition(
            task, event="reconcile.not_found", target="queued", ctx=ctx,
            expected_revision=expected_revision,
        )
        decision, grant, rule = _fresh_retry_decision(
            session, task, actor=actor, now=timestamp, capability=capability
        )
        ctx = replace(
            ctx,
            grant_id=grant.grant_id,
            gates_satisfied=(
                _live_confirmation_ref(session, task, now=timestamp) is not None
                or not bool((rule.constraints if rule else {}).get("required_confirmation"))
            )
            and (
                _live_approval_ref(task, now=timestamp) is not None
                or not bool((rule.constraints if rule else {}).get("required_approval"))
            ),
        )
        task.result = {
            **dict(task.result or {}),
            "no_side_effect_evidence": True,
            "reconcile_outcome": outcome,
            "evidence_refs": list(refs),
            "stop_confirmed": bool(stop_confirmed),
            "stop_evidence_refs": list(refs) if stop_confirmed else [],
        }
        task.stop_confirmed = bool(stop_confirmed)
        session.flush()
        previous_authority = (
            task.decision_id,
            task.decision_revision,
            task.decision_type,
            task.decision_purpose,
            task.policy_set_id,
            task.policy_version,
            task.policy_content_hash,
            task.grant_id,
            task.grant_status,
            dict(task.result or {}),
        )
        task.decision_id = decision.decision_id
        task.decision_revision = decision.decision_revision
        task.decision_type = decision.decision_type
        task.decision_purpose = "retry"
        if rule is not None:
            task.policy_set_id = rule.policy_set_id
            task.policy_version = rule.policy_version
            task.policy_content_hash = rule.policy_content_hash
        task.grant_id = grant.grant_id
        task.grant_status = grant.status
        task.result = {
            **dict(task.result or {}),
            "outcome": "none",
            "no_side_effect_evidence": True,
            "side_effects_reconciled": True,
        }
        session.flush()
        try:
            _perform_transition(
                session, task, actor, event="reconcile.not_found", target="queued",
                ctx=ctx, expected_revision=expected_revision,
                extra={"decision_id": decision.decision_id}, now=timestamp,
            )
        except TaskError:
            _revoke_unbound_grant(
                session, grant, reason_code="task_terminal",
                now=timestamp,
            )
            (
                task.decision_id,
                task.decision_revision,
                task.decision_type,
                task.decision_purpose,
                task.policy_set_id,
                task.policy_version,
                task.policy_content_hash,
                task.grant_id,
                task.grant_status,
                previous_result,
            ) = previous_authority
            task.result = previous_result
            session.flush()
            raise
        return task

    event_target = {
        "failed": ("reconcile.failed", "failed"),
        "stopped": ("reconcile.stopped", "cancelled"),
        "timed_out": ("reconcile.timed_out", "timed_out"),
        "unresolved": ("reconcile.unresolved", "needs_human"),
        "running": ("reconcile.running", "needs_human"),
        "completed": ("reconcile.completed", "needs_human"),
    }[outcome]
    event, target = event_target
    ctx = base_ctx
    if target == "needs_human":
        ctx = replace(ctx, escalation_justified=True)
    _preview_transition(
        task, event=event, target=target, ctx=ctx,
        expected_revision=expected_revision,
    )

    task.result = {
        **dict(task.result or {}),
        "no_side_effect_evidence": bool(no_side_effect_evidence),
        "reconcile_outcome": outcome,
        "evidence_refs": list(refs),
        "stop_confirmed": bool(stop_confirmed),
        "stop_evidence_refs": list(refs) if stop_confirmed else [],
    }
    task.stop_confirmed = bool(stop_confirmed)
    session.flush()

    if target == "failed":
        _perform_transition(
            session, task, actor, event=event, target=target,
            ctx=ctx, expected_revision=expected_revision, now=timestamp,
        )
        _close_attempt(
            _current_attempt(session, task), state="failed", now=timestamp,
            stop_confirmed=True, evidence_refs=refs,
        )
    elif target == "cancelled":
        _invalidate_grant(
            session, task, actor, reason_code="task_cancelled", now=timestamp
        )
        _perform_transition(
            session, task, actor, event=event, target=target,
            ctx=ctx, expected_revision=expected_revision, now=timestamp,
        )
        _close_attempt(
            _current_attempt(session, task), state="cancelled", now=timestamp,
            stop_confirmed=True, evidence_refs=refs,
        )
    elif target == "timed_out":
        _invalidate_grant(
            session, task, actor, reason_code="task_terminal", now=timestamp
        )
        _perform_transition(
            session, task, actor, event=event, target=target,
            ctx=ctx, expected_revision=expected_revision, now=timestamp,
        )
        _close_attempt(
            _current_attempt(session, task), state="timed_out", now=timestamp,
            stop_confirmed=True, evidence_refs=refs,
        )
    else:
        _perform_transition(
            session, task, actor, event=event, target=target, ctx=ctx,
            expected_revision=expected_revision, now=timestamp,
        )
        task.needs_human_origin = "unknown"
        session.flush()
        return task
    session.flush()
    return task


def takeover_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    action: str,
    result_ref: str | None = None,
    evidence_refs: Sequence[str] = (),
    side_effects_reconciled: bool = False,
    capability: str | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Audited human takeover of a ``needs_human`` task (T59-T64).

    T59/T60 are refused when the state was entered from ``unknown`` or
    ``failed`` (F41): those grants are already revoked/terminal.  Redispatch
    (T61) needs stop evidence or a never-started proof plus a fresh grant.
    """
    timestamp = now or _utcnow()
    _require_human(actor)
    task = _load_task(session, task_id, for_update=True)
    if task.state != "needs_human":
        raise TaskError(
            "task_invalid_transition",
            "takeover requires the needs_human state",
            context={"state": task.state},
        )
    if not (_is_privileged(actor) or actor.principal_id == task.owner_principal_id):
        raise TaskError(
            "task_unauthorized_actor",
            "only the accountable owner or an identity manager may take over",
            context={"actor": actor.principal_id},
        )
    refs = tuple(evidence_refs)
    if refs:
        verification.validate_result_refs(None, refs, require_result=False)
    ctx = _base_context(session, task, actor, now=timestamp)

    if action == "running":
        # C6: only T40/T35/T52 origins may resume the same attempt, and the
        # persisted attempt row must still be running.  Never default the
        # evidence to true.
        if task.needs_human_origin not in {"running", "dispatched", "cancel_requested"}:
            raise TaskError(
                "grant_revoked",
                "this needs_human origin cannot resume execution",
                context={"needs_human_origin": task.needs_human_origin},
            )
        if not ctx.same_attempt_running_evidence:
            raise TaskError(
                "evidence_missing",
                "no persisted evidence that the same attempt is still running",
                context={"attempt": task.attempt},
            )
        ctx = replace(
            ctx,
            needs_human_origin=task.needs_human_origin,
            same_attempt_running_evidence=True,
        )
        _perform_transition(
            session, task, actor, event="human.takeover.running", target="running",
            ctx=ctx, expected_revision=expected_revision, now=timestamp,
        )
    elif action == "result_submitted":
        verification.validate_result_refs(result_ref, refs, require_result=True)
        # C6: only T45 (verification inconclusive) may submit a result for an
        # already-ended attempt.
        if task.needs_human_origin != "verifying":
            raise TaskError(
                "grant_revoked",
                "this needs_human origin cannot resume verification",
                context={"needs_human_origin": task.needs_human_origin},
            )
        # Validate the persisted attempt evidence *before* writing the new
        # result, otherwise the write itself would satisfy the check.
        ctx = _base_context(session, task, actor, now=timestamp)
        if not ctx.attempt_finished_pending_verification:
            raise TaskError(
                "evidence_missing",
                "no persisted evidence that the attempt ended pending verification",
                context={"attempt": task.attempt},
            )
        ctx = replace(
            ctx,
            needs_human_origin=task.needs_human_origin,
            attempt_finished_pending_verification=True,
            result_present=bool(result_ref),
            evidence_present=bool(refs),
        )
        _preview_transition(
            task, event="human.result.submitted", target="verifying", ctx=ctx,
            expected_revision=expected_revision,
        )
        task.result = {
            **dict(task.result or {}),
            "outcome": "completed",
            "result_ref": result_ref,
            "evidence_refs": list(refs),
        }
        session.flush()
        _perform_transition(
            session, task, actor, event="human.result.submitted",
            target="verifying", ctx=ctx, expected_revision=expected_revision,
            now=timestamp,
        )
        _close_attempt(
            _current_attempt(session, task), state="verifying", now=timestamp,
            stop_confirmed=False, result_ref=result_ref, evidence_refs=refs,
        )
    elif action == "redispatch":
        # C5/S-19: attempt>=1 needs real stop evidence; an open attempt is
        # closed with that evidence *before* T61.  attempt=0 is the proof that
        # nothing was dispatched.  max_attempts is enforced honestly.
        if task.attempt >= 1 and not refs:
            raise TaskError(
                "stop_not_confirmed",
                "redispatch requires stop evidence (or a never-started proof)",
                context={"attempt": task.attempt},
            )
        if task.attempt >= task.max_attempts:
            raise TaskError(
                "retry_budget_exhausted",
                "the attempt budget is exhausted",
                context={"attempt": task.attempt, "max_attempts": task.max_attempts},
            )
        base = _base_context(session, task, actor, now=timestamp)
        # T61 must derive reconciliation from persisted (or explicitly
        # supplied and validated) evidence -- never from a hardcoded True.
        reconciled = side_effects_reconciled is True or base.side_effects_reconciled
        if task.attempt >= 1 and not reconciled:
            raise TaskError(
                "task_reconcile_evidence_missing",
                "redispatch requires reconciled side effects",
                context={"attempt": task.attempt},
            )
        if _has_open_attempt(session, task) and not reconciled:
            raise TaskError(
                "task_active_attempt_unreconciled",
                "the open attempt must be stopped with reconciled side effects",
                context={"attempt": task.attempt},
            )
        _decision, grant, rule = _fresh_retry_decision(
            session, task, actor=actor, now=timestamp, capability=capability
        )
        preview = replace(
            base,
            stop_confirmed=bool(refs) or task.attempt == 0,
            stop_evidence=bool(refs),
            side_effects_reconciled=bool(reconciled),
            no_side_effects=bool(task.attempt == 0 or reconciled or base.no_side_effects),
            active_attempt_unreconciled=False,
            fresh_decision_and_grant=True,
            old_grant_invalidated=True,
            decision_purpose="retry",
            decision_revision=int(_decision.decision_revision or 2),
            grant_id=grant.grant_id,
            grant_status="active",
            grant_task_matches=True,
            grant_revision_matches=True,
            grant_decision_matches=True,
            grant_attempt_matches=True,
            grant_unexpired=True,
            grant_scope_ok=True,
            retry_budget_remaining=task.retry_budget_remaining,
            gates_satisfied=(
                _live_confirmation_ref(session, task, now=timestamp) is not None
                or not bool((rule.constraints if rule else {}).get("required_confirmation"))
            )
            and (
                _live_approval_ref(task, now=timestamp) is not None
                or not bool((rule.constraints if rule else {}).get("required_approval"))
            ),
        )
        try:
            _preview_transition(
                task, event="human.redispatch", target="queued", ctx=preview,
                expected_revision=expected_revision,
            )
        except TaskError:
            _revoke_unbound_grant(
                session, grant,
                reason_code="task_terminal",
                now=timestamp,
            )
            raise
        if refs:
            _close_attempt_with_stop(
                session,
                task,
                state="cancelled",
                now=timestamp,
                evidence_refs=refs,
            )
        task.result = {
            **dict(task.result or {}),
            "stop_confirmed": bool(refs) or task.attempt == 0,
            "stop_evidence_refs": list(refs) if refs else [],
            "side_effects_reconciled": bool(reconciled),
        }
        task.stop_confirmed = bool(refs) or task.attempt == 0
        session.flush()
        _invalidate_grant(
            session, task, actor, reason_code="task_terminal", now=timestamp
        )
        previous_authority = (
            task.decision_id,
            task.decision_revision,
            task.decision_type,
            task.decision_purpose,
            task.policy_set_id,
            task.policy_version,
            task.policy_content_hash,
            task.grant_id,
            task.grant_status,
        )
        task.decision_id = _decision.decision_id
        task.decision_revision = _decision.decision_revision
        task.decision_type = _decision.decision_type
        task.decision_purpose = "retry"
        if rule is not None:
            task.policy_set_id = rule.policy_set_id
            task.policy_version = rule.policy_version
            task.policy_content_hash = rule.policy_content_hash
        task.grant_id = grant.grant_id
        task.grant_status = grant.status
        session.flush()
        try:
            _perform_transition(
                session, task, actor, event="human.redispatch", target="queued",
                ctx=preview, expected_revision=expected_revision, now=timestamp,
            )
        except TaskError:
            _revoke_unbound_grant(
                session, grant,
                reason_code="task_terminal",
                now=timestamp,
            )
            (
                task.decision_id,
                task.decision_revision,
                task.decision_type,
                task.decision_purpose,
                task.policy_set_id,
                task.policy_version,
                task.policy_content_hash,
                task.grant_id,
                task.grant_status,
            ) = previous_authority
            session.flush()
            raise
        task.result = {
            **dict(task.result or {}),
            "outcome": "none",
        }
        session.flush()
    elif action == "authorization_revoked":
        if task.attempt >= 1 and not refs:
            raise TaskError(
                "stop_not_confirmed",
                "revoking authorization requires stop evidence for an opened attempt",
                context={"attempt": task.attempt},
            )
        if refs:
            _close_attempt_with_stop(
                session,
                task,
                state="cancelled",
                now=timestamp,
                evidence_refs=refs,
            )
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            stop_confirmed=bool(refs) or task.attempt == 0,
            stop_evidence=bool(refs),
            authorization_revoked=True,
        )
        task.stop_confirmed = bool(refs) or task.attempt == 0
        if refs:
            task.result = {
                **dict(task.result or {}),
                "stop_confirmed": True,
                "stop_evidence_refs": list(refs),
            }
        session.flush()
        _invalidate_grant(
            session, task, actor, reason_code="task_terminal", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="human.authorization_revoked",
            target="denied", ctx=ctx, expected_revision=expected_revision,
            now=timestamp,
        )
    elif action == "stop_confirmed":
        refs = _validate_evidence_refs(refs)
        task.stop_confirmed = True
        task.result = {
            **dict(task.result or {}),
            "outcome": "cancelled",
            "stop_confirmed": True,
            "stop_evidence_refs": list(refs),
        }
        session.flush()
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            stop_confirmed=True,
            stop_evidence=True,
        )
        _invalidate_grant(
            session, task, actor, reason_code="task_cancelled", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="human.stop_confirmed", target="cancelled",
            ctx=ctx, expected_revision=expected_revision, now=timestamp,
        )
        _close_attempt(
            _current_attempt(session, task), state="cancelled", now=timestamp,
            stop_confirmed=True, evidence_refs=refs,
        )
    elif action == "declared_failed":
        if task.attempt < 1:
            raise TaskError(
                "task_attempt_missing",
                "an attempt=0 human failure must be registered as cancelled/denied",
                context={"attempt": task.attempt},
            )
        if refs:
            _close_attempt_with_stop(
                session,
                task,
                state="failed",
                now=timestamp,
                evidence_refs=refs,
            )
        else:
            attempt = _current_attempt(session, task)
            if attempt is not None and attempt.ended_at is None:
                _close_attempt(
                    attempt,
                    state="failed",
                    now=timestamp,
                    stop_confirmed=False,
                )
        _invalidate_grant(
            session, task, actor, reason_code="attempt_failed", now=timestamp
        )
        _perform_transition(
            session, task, actor, event="human.declared_failed", target="failed",
            ctx=ctx, expected_revision=expected_revision,
            reason_code="evidence_missing", now=timestamp,
        )
    else:
        raise TaskError(
            "required_input_missing",
            f"unknown takeover action {action!r}",
            context={"action": action},
        )
    session.flush()
    return task


# ---------------------------------------------------------------------------
# Remaining legal edges (escalation, clarification, denial, evidence loss)
# ---------------------------------------------------------------------------

_ESCALATABLE_STATES = {
    "evaluated",
    "awaiting_clarification",
    "awaiting_confirmation",
    "awaiting_approval",
    "queued",
    "dispatched",
    "running",
    "cancel_requested",
}


def escalate_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    reason_code: str,
    evidence_refs: Sequence[str] = (),
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """T10/T14/T18/T22/T26/T35/T40/T52: hand the task to a human.

    The origin is recorded exactly as the source state so T59 can later require
    a running/dispatched/cancel_requested origin and T60 a verifying origin.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if is_terminal(task.state) or task.state not in _ESCALATABLE_STATES:
        raise TaskError(
            "task_invalid_transition",
            f"escalation is not available from {task.state!r}",
            context={"state": task.state},
        )
    if not reason_code:
        raise TaskError(
            "required_input_missing",
            "an escalation reason code is required",
            context={"field": "reason_code"},
        )
    refs = tuple(evidence_refs)
    if refs:
        verification.validate_result_refs(None, refs, require_result=False)
    previous = task.state
    # T40/T35/T52 retain the possibility of T59: the grant must stay usable
    # (active/consumed) for the same attempt.  Pre-execution escalation has no
    # such recovery path, so its grant is invalidated.
    if previous in {"queued"}:
        _invalidate_grant(
            session, task, actor, reason_code=reason_code, now=timestamp
        )
    ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        escalation_justified=True,
    )
    _perform_transition(
        session,
        task,
        actor,
        event="human.escalation_required",
        target="needs_human",
        ctx=ctx,
        expected_revision=expected_revision,
        reason_code=reason_code,
        extra={"evidence_refs": list(refs)} if refs else None,
        now=timestamp,
    )
    task.needs_human_origin = previous
    if task.owner_account_status != "active" and task.owner_escalation is None:
        task.owner_escalation = _owner_escalation_document(
            task,
            status=task.owner_account_status,
            now=timestamp,
            reason_code=reason_code,
        )
    session.flush()
    return task


def provide_clarification(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    provided_inputs: Sequence[str] | None = None,
    resource_scope: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """T11: the missing inputs were supplied; re-run the PDP."""
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if task.state != "awaiting_clarification":
        raise TaskError(
            "task_invalid_transition",
            "clarification is only accepted in awaiting_clarification",
            context={"state": task.state},
        )
    required = set((task.goal or {}).get("required_inputs") or ())
    supplied = {str(item) for item in (provided_inputs or ())}
    missing = tuple(sorted(required - supplied))
    if missing:
        raise TaskError(
            "required_input_missing",
            "the clarification did not supply every required input",
            context={"missing_inputs": list(missing)},
        )
    if resource_scope is not None:
        scope = _normalized_scope(resource_scope)
        task.resource_scope = scope
        base_payload = (
            dict(payload)
            if payload is not None
            else {
                **dict(task.payload or {}),
                "resource_scope": scope,
            }
        )
        task.payload = base_payload
        task.payload_hash = payload_hash(base_payload)
    elif payload is not None:
        task.payload = dict(payload)
        task.payload_hash = payload_hash(dict(payload))
    task.result = {
        **dict(task.result or {}),
        "inputs_satisfied": True,
        "missing_inputs": [],
    }
    task.record_completeness, task.incomplete_reason = _record_completeness(task)
    task.incomplete_reason = None if task.record_completeness == "complete" else task.incomplete_reason
    session.flush()
    record = _decision_record(session, task)
    if record is None:
        raise TaskError(
            "required_input_missing",
            "an evaluated decision is required before clarification",
            context={"field": "decision_id"},
        )
    executor = resolve_principal(
        session,
        task.executor_principal_id or "",
        principal_type=task.executor_principal_type,
    )
    decision = evaluate(
        session,
        EvaluationRequest(
            principal=executor,
            capability=(task.requested_capabilities or [""])[0],
            task_id=task.task_id,
            task_revision=task.state_revision + 1,
            task_idempotency_key=task.idempotency_key,
            resource_scope=task.resource_scope,
            priority=task.priority,
            execution_mode=task.execution_mode,
            risk="low",
            confirmation_ref=task.confirmation_ref,
            approval_ref=_approval_document(task),
            payload_hash=task.payload_hash,
            policy_set_id=task.policy_set_id or DEFAULT_POLICY_SET_ID,
            policy_version=task.policy_version,
            correlation_id=task.correlation_id,
            decision_purpose="initial_execution",
            supersedes_decision_id=record.decision_id,
        ),
        now=timestamp,
        evidence_verifier=TaskEvidenceVerifier(session, task_id=task.task_id),
    )
    _apply_decision(
        session,
        task,
        actor,
        decision,
        from_state="awaiting_clarification",
        now=timestamp,
        runtime_capabilities_verified=bool(
            (task.result or {}).get("runtime_capabilities_verified", False)
        ),
    )
    return task


def deny_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    reason_code: str,
    evidence_refs: Sequence[str] = (),
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """T12/T25: withdraw authority for an awaiting-clarification or queued task."""
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if task.state not in {"awaiting_clarification", "queued"}:
        raise TaskError(
            "task_invalid_transition",
            f"denial is not a defined edge from {task.state!r}",
            context={"state": task.state},
        )
    if not reason_code:
        raise TaskError(
            "required_input_missing",
            "a denial reason code is required",
            context={"field": "reason_code"},
        )
    refs = tuple(evidence_refs)
    if refs:
        verification.validate_result_refs(None, refs, require_result=False)
    task.stop_confirmed = True
    task.result = {
        **dict(task.result or {}),
        "outcome": "none",
        "stop_confirmed": True,
        "stop_evidence_refs": list(refs) or [_EVIDENCE_NEVER_STARTED],
        "no_side_effects": True,
        "side_effects_reconciled": True,
    }
    session.flush()
    previous = task.state
    ctx = _base_context(session, task, actor, now=timestamp)
    if previous == "awaiting_clarification":
        ctx = replace(ctx, decision_type="deny")
    else:
        ctx = replace(ctx, decision_invalidated=True)
    _invalidate_grant(
        session, task, actor, reason_code="task_terminal", now=timestamp
    )
    _perform_transition(
        session,
        task,
        actor,
        event="governance.denied",
        target="denied",
        ctx=ctx,
        expected_revision=expected_revision,
        reason_code=reason_code,
        now=timestamp,
    )
    return task


def dispatch_deadline_exceeded(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    evidence_refs: Sequence[str],
    no_side_effects_evidence: bool = False,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """T34: a dispatched submission window closed with verified no side effects.

    T34 is a "nothing was accepted" claim.  It is only allowed when the
    execution boundary supplies an explicit no-side-effect proof **and** the
    caller is the bound execution producer or a privileged/service principal;
    an arbitrary participant can never assert it on someone else's behalf.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if not _is_privileged(actor) and actor.principal_type not in {
        "service",
        "runtime",
    }:
        _require_execution_producer(actor, task)
    if task.state != "dispatched":
        raise TaskError(
            "task_invalid_transition",
            "dispatch deadline expiry requires the dispatched state",
            context={"state": task.state},
        )
    if no_side_effects_evidence is not True:
        raise TaskError(
            "task_reconcile_evidence_missing",
            "a dispatch deadline needs verified no-side-effect evidence",
            context={"outcome": "deadline_exceeded"},
        )
    refs = _validate_evidence_refs(evidence_refs)
    ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        deadline_passed=True,
        no_side_effects=True,
        stop_confirmed=True,
        stop_evidence=True,
    )
    _preview_transition(
        task, event="dispatch.deadline_exceeded", target="timed_out", ctx=ctx,
        expected_revision=expected_revision,
    )
    task.stop_confirmed = True
    task.result = {
        **dict(task.result or {}),
        "outcome": "timed_out",
        "stop_confirmed": True,
        "stop_evidence_refs": list(refs),
        "no_side_effects": True,
        "side_effects_reconciled": True,
        "no_side_effect_evidence": True,
    }
    session.flush()
    _invalidate_grant(session, task, actor, reason_code="task_terminal", now=timestamp)
    _perform_transition(
        session,
        task,
        actor,
        event="dispatch.deadline_exceeded",
        target="timed_out",
        ctx=replace(ctx, state_revision=task.state_revision),
        expected_revision=expected_revision,
        reason_code="dispatch_deadline_exceeded",
        extra={"evidence_refs": list(refs)},
        now=timestamp,
    )
    _close_attempt(
        _current_attempt(session, task),
        state="timed_out",
        now=timestamp,
        stop_confirmed=True,
        evidence_refs=refs,
    )
    session.flush()
    return task


def evidence_unavailable(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    evidence_refs: Sequence[str] = (),
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """T46: the evidence source is unavailable while verifying -> unknown."""
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if task.state != "verifying":
        raise TaskError(
            "task_invalid_transition",
            "evidence unavailability requires the verifying state",
            context={"state": task.state},
        )
    refs = tuple(evidence_refs)
    if refs:
        verification.validate_result_refs(None, refs, require_result=False)
    task.result = {
        **dict(task.result or {}),
        "evidence_unavailable": True,
        "evidence_refs": list(refs),
    }
    session.flush()
    ctx = replace(
        _base_context(session, task, actor, now=timestamp),
        evidence_unavailable=True,
    )
    _invalidate_grant(
        session, task, actor, reason_code="execution_evidence_lost", now=timestamp
    )
    _perform_transition(
        session,
        task,
        actor,
        event="verification.evidence_unavailable",
        target="unknown",
        ctx=ctx,
        expected_revision=expected_revision,
        reason_code="evidence_missing",
        extra={"evidence_refs": list(refs)} if refs else None,
        now=timestamp,
    )
    return task


def revoke_approval(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    reason_code: str,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Revoke a recorded approval and re-close the gated task.

    From ``awaiting_approval`` this is T20 (denied); from ``queued`` it is T25
    (authority withdrawn before dispatch).  An approval that already authorised
    execution cannot be silently revoked: the caller must stop/reconcile first.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if task.state not in {"awaiting_approval", "queued"}:
        raise TaskError(
            "task_invalid_transition",
            "approval revocation requires awaiting_approval or queued",
            context={"state": task.state},
        )
    approval = dict(task.approval_ref or {})
    if not approval:
        raise TaskError(
            "approval_missing",
            "the task has no recorded approval",
            context={"task_id": task.task_id},
        )
    approval.update(
        {
            "decision": "revoked",
            "revoked_at": _rfc3339(timestamp),
            "revoked_by_principal_id": actor.principal_id,
            "revocation_reason": reason_code,
        }
    )
    task.approval_ref = approval
    task.stop_confirmed = True
    task.result = {
        **dict(task.result or {}),
        "outcome": "none",
        "stop_confirmed": True,
        "stop_evidence_refs": [_EVIDENCE_NEVER_STARTED],
        "no_side_effects": True,
        "side_effects_reconciled": True,
    }
    session.flush()
    ctx = _base_context(session, task, actor, now=timestamp)
    if task.state == "awaiting_approval":
        ctx = replace(ctx, approval_invalid=True)
        event = "approval.rejected"
    else:
        ctx = replace(ctx, decision_invalidated=True)
        event = "governance.denied"
    _invalidate_grant(
        session, task, actor, reason_code="task_terminal", now=timestamp
    )
    _perform_transition(
        session,
        task,
        actor,
        event=event,
        target="denied",
        ctx=ctx,
        expected_revision=expected_revision,
        reason_code=reason_code,
        now=timestamp,
    )
    return task


def escalate_owner_account(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    status: str,
    reason_code: str = "accountable_owner_inactive",
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Record owner escalation and hand an active task to a human (T26).

    The owner snapshot is written in the same UPDATE as the ``needs_human``
    transition, so the active-state owner constraint is never violated.
    """
    timestamp = now or _utcnow()
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if task.state in {"queued", "dispatched", "running"}:
        _invalidate_grant(
            session, task, actor, reason_code=reason_code, now=timestamp
        )
    task.owner_account_status = status
    task.owner_escalation = _owner_escalation_document(
        task, status=status, now=timestamp, reason_code=reason_code
    )
    with session.no_autoflush:
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            escalation_justified=True,
            owner_active=False,
            owner_escalation_present=True,
        )
    previous = task.state
    _perform_transition(
        session,
        task,
        actor,
        event="human.escalation_required",
        target="needs_human",
        ctx=ctx,
        expected_revision=expected_revision,
        reason_code=reason_code,
        now=timestamp,
    )
    task.needs_human_origin = previous
    session.flush()
    return task


def resolve_owner_escalation(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    owner: GovernancePrincipal,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Assign a new active owner and resume a pre-execution owner escalation.

    An owner-disabled escalation otherwise has no exit: ``needs_human`` has no
    generic "assign owner" edge, and T61 needs ``owner_active`` to pass.  This
    service path validates the new owner, re-evaluates a fresh decision/grant
    and requeues through T61 -- the same guards as a human redispatch.  It is
    restricted to tasks that never opened an attempt; a task with an opened
    attempt must be stopped/reconciled first.
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    _require_task_authority(actor, task)
    if task.state != "needs_human":
        raise TaskError(
            "task_invalid_transition",
            "owner escalation can only be resolved in needs_human",
            context={"state": task.state},
        )
    if task.owner_escalation is None:
        raise TaskError(
            "accountable_owner_missing",
            "the task has no owner escalation to resolve",
            context={"task_id": task.task_id},
        )
    _require_actor(owner)
    if owner.principal_type != "person":
        raise OwnerError(
            "accountable_owner_unresolved",
            "the accountable owner must be a person",
            context={"principal_type": owner.principal_type},
        )
    owner_status = _owner_status(session, owner)
    if owner_status != "active":
        raise TaskError(
            "task_owner_inactive",
            "the new accountable owner is not active",
            context={"owner": owner.principal_id, "owner_account_status": owner_status},
        )
    if task.attempt != 0 or _has_open_attempt(session, task):
        raise TaskError(
            "task_active_attempt_unreconciled",
            "stop and reconcile the attempt before resolving the escalation",
            context={"attempt": task.attempt},
        )
    base = _base_context(session, task, actor, now=timestamp)
    capability = (task.requested_capabilities or [None])[0]
    try:
        decision, grant, rule = _fresh_retry_decision(
            session, task, actor=actor, now=timestamp, capability=capability
        )
    except GovernanceError as exc:
        raise TaskError(
            exc.reason_code,
            f"retry authorisation refused: {exc.reason_code}",
            context=exc.context,
        ) from exc
    ctx = replace(
        base,
        owner_resolved=True,
        owner_active=True,
        owner_escalation_present=False,
        no_side_effects=True,
        active_attempt_unreconciled=False,
        fresh_decision_and_grant=True,
        old_grant_invalidated=True,
        decision_purpose="retry",
        decision_revision=int(decision.decision_revision or 2),
        grant_id=grant.grant_id,
        grant_status="active",
        grant_task_matches=True,
        grant_revision_matches=True,
        grant_decision_matches=True,
        grant_attempt_matches=True,
        grant_unexpired=True,
        grant_scope_ok=True,
        retry_budget_remaining=task.retry_budget_remaining,
        gates_satisfied=(
            _live_confirmation_ref(session, task, now=timestamp) is not None
            or not bool((rule.constraints if rule else {}).get("required_confirmation"))
        )
        and (
            _live_approval_ref(task, now=timestamp) is not None
            or not bool((rule.constraints if rule else {}).get("required_approval"))
        ),
    )
    try:
        _preview_transition(
            task, event="human.redispatch", target="queued", ctx=ctx,
            expected_revision=expected_revision,
        )
    except TaskError:
        _revoke_unbound_grant(
            session, grant,
            reason_code="task_terminal",
            now=timestamp,
        )
        raise
    previous_owner = (
        task.owner_principal_id,
        task.owner_employee_id,
        task.owner_account_status,
        dict(task.owner_escalation) if task.owner_escalation else None,
    )
    previous_authority = (
        task.decision_id,
        task.decision_revision,
        task.decision_type,
        task.decision_purpose,
        task.policy_set_id,
        task.policy_version,
        task.policy_content_hash,
        task.grant_id,
        task.grant_status,
        dict(task.result or {}),
    )
    task.owner_principal_id = owner.principal_id
    task.owner_employee_id = owner.employee_id
    task.owner_account_status = owner_status
    task.owner_escalation = None
    task.decision_id = decision.decision_id
    task.decision_revision = decision.decision_revision
    task.decision_type = decision.decision_type
    task.decision_purpose = "retry"
    if rule is not None:
        task.policy_set_id = rule.policy_set_id
        task.policy_version = rule.policy_version
        task.policy_content_hash = rule.policy_content_hash
    task.grant_id = grant.grant_id
    task.grant_status = grant.status
    task.result = {**dict(task.result or {}), "outcome": "none"}
    session.flush()
    try:
        _perform_transition(
            session, task, actor, event="human.redispatch", target="queued",
            ctx=ctx, expected_revision=expected_revision, now=timestamp,
        )
    except TaskError:
        _revoke_unbound_grant(
            session, grant,
            reason_code="task_terminal",
            now=timestamp,
        )
        (
            task.owner_principal_id,
            task.owner_employee_id,
            task.owner_account_status,
            task.owner_escalation,
        ) = previous_owner
        (
            task.decision_id,
            task.decision_revision,
            task.decision_type,
            task.decision_purpose,
            task.policy_set_id,
            task.policy_version,
            task.policy_content_hash,
            task.grant_id,
            task.grant_status,
            previous_result,
        ) = previous_authority
        task.result = previous_result
        session.flush()
        raise
    session.flush()
    return task


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------


def create_confirmation(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    payload: Mapping[str, Any] | None = None,
    purpose: str = "high_impact_action",
    ttl_seconds: int = CONFIRMATION_TTL_SECONDS,
    channel_binding: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> TaskConfirmation:
    """Create a pending confirmation for the task's current payload."""
    timestamp = now or _utcnow()
    _require_actor(actor)
    if ttl_seconds < 1 or ttl_seconds > CONFIRMATION_MAX_TTL_SECONDS:
        raise TaskError(
            "required_input_missing",
            "ttl_seconds is outside the allowed confirmation TTL",
            context={"field": "ttl_seconds", "max": CONFIRMATION_MAX_TTL_SECONDS},
        )
    task = _load_task(session, task_id, for_update=True)
    # Only the parties who own the request may mint a confirmation; an
    # executor/assignee must not be able to generate the consent it needs.
    _require_confirmation_issuer(actor, task)
    if is_terminal(task.state):
        _reject_terminal(
            session, task, actor,
            event="governance.confirmation_required",
            target="awaiting_confirmation",
            message="a terminal task cannot request a confirmation",
        )
    value = dict(payload) if payload is not None else dict(task.payload or {})
    row = TaskConfirmation(
        confirmation_id=_new_id("conf_"),
        task_id=task.task_id,
        payload_hash=payload_hash(value),
        purpose=purpose,
        status="pending",
        issued_at=timestamp,
        expires_at=timestamp + timedelta(seconds=max(1, ttl_seconds)),
        single_use=True,
        channel_binding=dict(channel_binding) if channel_binding is not None else None,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        actor=actor,
        action="task.confirmation.created",
        task=task,
        details={
            "confirmation_id": row.confirmation_id,
            "payload_hash": row.payload_hash,
            "purpose": row.purpose,
        },
    )
    return row


def _confirmation_for_task(
    session: Session, task: Task, confirmation_id: str
) -> TaskConfirmation:
    row = session.scalar(
        select(TaskConfirmation)
        .where(
            TaskConfirmation.confirmation_id == confirmation_id,
            TaskConfirmation.task_id == task.task_id,
        )
        .with_for_update()
    )
    if row is None:
        raise ConfirmationError(
            "task_confirmation_not_found",
            "the confirmation does not belong to this task",
            context={"confirmation_id": confirmation_id},
        )
    return row


def _confirmer_authorized(actor: GovernancePrincipal, task: Task) -> bool:
    return actor.principal_id in {
        task.requester_principal_id,
        task.owner_principal_id,
    }


def _require_confirmation_issuer(
    actor: GovernancePrincipal, task: Task
) -> None:
    """Requester, accountable owner or an identity manager may mint a gate."""
    _require_actor(actor)
    if _is_privileged(actor):
        return
    if _confirmer_authorized(actor, task):
        return
    raise TaskError(
        "task_unauthorized_actor",
        "only the requester, accountable owner or an identity manager may "
        "create a confirmation",
        context={"actor": actor.principal_id},
    )


def satisfy_confirmation(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    confirmation_id: str,
    payload: Mapping[str, Any] | None = None,
    channel: str | None = None,
    now: datetime | None = None,
) -> TaskConfirmation:
    """Record the affected user's confirmation for exactly this payload."""
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    row = _confirmation_for_task(session, task, confirmation_id)
    if not _confirmer_authorized(actor, task):
        raise ConfirmationError(
            "task_confirmation_actor_invalid",
            "the confirmation must come from the requester or accountable owner",
            context={"actor": actor.principal_id},
        )
    if row.status == "consumed":
        raise ConfirmationError(
            "replay_detected",
            "a consumed confirmation cannot be satisfied again",
            context={"confirmation_id": confirmation_id},
        )
    if row.status == "satisfied":
        if row.confirmed_by_principal_id == actor.principal_id:
            return row
        raise ConfirmationError(
            "replay_detected",
            "the confirmation was already satisfied by another principal",
            context={"confirmation_id": confirmation_id},
        )
    if row.status in {"expired", "rejected"}:
        raise ConfirmationError(
            "task_confirmation_expired"
            if row.status == "expired"
            else "confirmation_missing",
            f"the confirmation is {row.status}",
            context={"confirmation_id": confirmation_id, "status": row.status},
        )
    if _to_utc(row.expires_at) <= timestamp:  # type: ignore[operator]
        row.status = "expired"
        session.flush()
        raise ConfirmationError(
            "task_confirmation_expired",
            "the confirmation TTL has elapsed",
            context={"confirmation_id": confirmation_id},
        )
    supplied_hash = payload_hash(dict(payload)) if payload is not None else task.payload_hash
    if supplied_hash != row.payload_hash or task.payload_hash != row.payload_hash:
        raise ConfirmationError(
            "payload_hash_mismatch",
            "the payload changed after the confirmation was issued",
            context={
                "confirmation_id": confirmation_id,
                "confirmation_payload_hash": row.payload_hash,
                "current_payload_hash": task.payload_hash,
            },
        )
    if row.channel_binding is not None:
        # Omitting the channel must not bypass a bound confirmation.
        if channel is None or row.channel_binding.get("channel") != channel:
            raise ConfirmationError(
                "task_confirmation_channel_mismatch",
                "the confirmation is bound to a different channel",
                context={"confirmation_id": confirmation_id},
            )
    row.status = "satisfied"
    row.confirmed_by_principal_id = actor.principal_id
    row.satisfied_at = timestamp
    session.flush()
    _audit(
        session,
        actor=actor,
        action="task.confirmation.satisfied",
        task=task,
        details={"confirmation_id": row.confirmation_id},
    )
    return row


def confirm_task(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    confirmation_id: str,
    payload: Mapping[str, Any] | None = None,
    channel: str | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Satisfy a confirmation and re-evaluate the task (T15).

    The confirmation is bound to the JCS hash of the exact payload; a changed
    payload, an elapsed TTL or a replayed confirmation is rejected and the task
    is denied (``confirmation.rejected``, T16) rather than silently queued.
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    if task.state != "awaiting_confirmation":
        if task.state in {"evaluated", "queued", "awaiting_approval"} and task.confirmation_ref:
            return task
        raise TaskError(
            "task_invalid_transition",
            "confirmation is not expected in this state",
            context={"state": task.state},
        )
    row = _confirmation_for_task(session, task, confirmation_id)
    try:
        row = satisfy_confirmation(
            session,
            task_id=task_id,
            actor=actor,
            confirmation_id=confirmation_id,
            payload=payload,
            channel=channel,
            now=timestamp,
        )
    except ConfirmationError as exc:
        if exc.reason_code in {
            "payload_hash_mismatch",
            "task_confirmation_expired",
            "replay_detected",
        }:
            task.confirmation_ref = _confirmation_document(row)
            ctx = replace(
                _base_context(session, task, actor, now=timestamp),
                confirmation_invalid=True,
            )
            _perform_transition(
                session, task, actor, event="confirmation.rejected",
                target="denied", ctx=ctx,
                expected_revision=expected_revision,
                reason_code=exc.reason_code, now=timestamp,
            )
            task.stop_confirmed = True
            task.result = {
                **dict(task.result or {}),
                "outcome": "none",
                "stop_confirmed": True,
                "stop_evidence_refs": [_EVIDENCE_NEVER_STARTED],
            }
            session.flush()
        raise

    task.confirmation_ref = _confirmation_document(row)
    session.flush()
    record = _decision_record(session, task)
    if record is None:
        raise TaskError(
            "required_input_missing",
            "an evaluated decision is required before confirmation",
            context={"field": "decision_id"},
        )
    executor = resolve_principal(
        session,
        task.executor_principal_id or "",
        principal_type=task.executor_principal_type,
    )
    decision = evaluate(
        session,
        EvaluationRequest(
            principal=executor,
            capability=(task.requested_capabilities or [""])[0],
            task_id=task.task_id,
            task_revision=task.state_revision + 1,
            task_idempotency_key=task.idempotency_key,
            resource_scope=task.resource_scope,
            priority=task.priority,
            execution_mode=task.execution_mode,
            risk="low",
            confirmation_ref=task.confirmation_ref,
            approval_ref=_approval_document(task),
            payload_hash=task.payload_hash,
            policy_set_id=task.policy_set_id or DEFAULT_POLICY_SET_ID,
            policy_version=task.policy_version,
            correlation_id=task.correlation_id,
        ),
        now=timestamp,
        evidence_verifier=TaskEvidenceVerifier(session, task_id=task.task_id),
    )
    _apply_decision(
        session,
        task,
        actor,
        decision,
        from_state="awaiting_confirmation",
        now=timestamp,
        runtime_capabilities_verified=bool(
            (task.result or {}).get("runtime_capabilities_verified", False)
        ),
    )
    return task


def consume_confirmation(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    confirmation_id: str,
    call_id: str,
    payload_hash_value: str | None = None,
    channel: str | None = None,
    now: datetime | None = None,
) -> TaskConfirmation:
    """Consume a satisfied confirmation exactly once (atomic, replay-proof)."""
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    # Consumption is an execution-boundary action: the bound executor (or
    # another participant/service) may consume, but an outsider may not burn
    # someone else's single-use consent.
    _require_task_authority(actor, task)
    row = _confirmation_for_task(session, task, confirmation_id)
    if row.status == "consumed":
        raise ConfirmationError(
            "replay_detected",
            "this confirmation has already been consumed",
            context={
                "confirmation_id": confirmation_id,
                "consumed_by_call_id": row.consumed_by_call_id,
            },
        )
    if row.status != "satisfied":
        raise ConfirmationError(
            "confirmation_missing" if row.status != "expired" else "task_confirmation_expired",
            f"the confirmation is {row.status}, not satisfied",
            context={"confirmation_id": confirmation_id, "status": row.status},
        )
    if _to_utc(row.expires_at) <= timestamp:  # type: ignore[operator]
        row.status = "expired"
        session.flush()
        raise ConfirmationError(
            "task_confirmation_expired",
            "the confirmation TTL has elapsed",
            context={"confirmation_id": confirmation_id},
        )
    expected_hash = payload_hash_value or task.payload_hash
    if expected_hash != row.payload_hash or task.payload_hash != row.payload_hash:
        raise ConfirmationError(
            "payload_hash_mismatch",
            "the call payload is not the confirmed payload",
            context={"confirmation_id": confirmation_id},
        )
    if row.channel_binding is not None:
        # Omitting the channel must not bypass a bound confirmation.
        if channel is None or row.channel_binding.get("channel") != channel:
            raise ConfirmationError(
                "task_confirmation_channel_mismatch",
                "the confirmation is bound to a different channel",
                context={"confirmation_id": confirmation_id},
            )
    row.status = "consumed"
    row.consumed_at = timestamp
    row.consumed_by_call_id = call_id
    session.flush()
    if task.confirmation_ref and (
        task.confirmation_ref.get("confirmation_id") == confirmation_id
    ):
        task.confirmation_ref = _confirmation_document(row)
        session.flush()
    _audit(
        session,
        actor=actor,
        action="task.confirmation.consumed",
        task=task,
        details={"confirmation_id": confirmation_id, "call_id": call_id},
    )
    return row


# ---------------------------------------------------------------------------
# Approvals (existing workflow instance)
# ---------------------------------------------------------------------------


def record_approval(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    decision: str,
    reason: str | None = None,
    ttl_seconds: int = APPROVAL_TTL_SECONDS,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> Task:
    """Record an independent approval through the existing workflow service.

    Self-approval (requester/owner/executor) is refused by comparing the actual
    principal, as is an approver that differs from the configured approver
    (S-14).  Approval re-evaluates the PDP before any queueing.
    """
    timestamp = now or _utcnow()
    _require_human(actor)
    task = _load_task(session, task_id, for_update=True)
    if task.state != "awaiting_approval":
        if task.approval_ref and task.state in {"evaluated", "queued"}:
            return task
        raise TaskError(
            "task_invalid_transition",
            "approval is not expected in this state",
            context={"state": task.state},
        )
    if decision not in {"approve", "reject"}:
        raise TaskError(
            "required_input_missing",
            "approval decision must be approve or reject",
            context={"field": "decision"},
        )
    if ttl_seconds < 1 or ttl_seconds > APPROVAL_MAX_TTL_SECONDS:
        raise TaskError(
            "required_input_missing",
            "ttl_seconds is outside the allowed approval TTL",
            context={"field": "ttl_seconds", "max": APPROVAL_MAX_TTL_SECONDS},
        )
    if task.workflow_instance_id is None:
        raise ConfirmationError(
            "task_approval_not_found",
            "the task has no workflow approval instance",
            context={"task_id": task.task_id},
        )
    if not _approver_is_independent(actor, task):
        raise OwnerError(
            "self_approval_forbidden",
            "requester/owner/executor cannot approve their own task",
            context={"actor": actor.principal_id},
        )
    if (
        task.approver_employee_id is not None
        and actor.employee_id != task.approver_employee_id
    ):
        raise OwnerError(
            "separation_of_duties_violation",
            "the actual approver does not match the configured approver",
            context={
                "configured_approver": task.approver_principal_id,
                "actual_approver": actor.principal_id,
            },
        )
    instance = session.get(WorkflowInstance, task.workflow_instance_id)
    if instance is None:
        raise ConfirmationError(
            "task_approval_not_found",
            "the workflow instance is missing",
            context={"instance_id": task.workflow_instance_id},
        )

    step = session.scalar(
        select(ApprovalStep)
        .where(
            ApprovalStep.instance_id == instance.id,
            ApprovalStep.status == "pending",
        )
        .order_by(ApprovalStep.step_no)
        .limit(1)
    )
    if step is None:
        # Already decided: idempotent for the same outcome.
        if instance.status == "approved" and decision == "approve":
            return task
        if instance.status == "rejected" and decision == "reject":
            return task
        raise TaskError(
            "task_approval_not_found",
            "the approval was already decided differently",
            context={"instance_status": instance.status},
        )
    try:
        record_decision(
            session,
            instance_id=instance.id,
            decision=decision,
            actor_id=int(actor.employee_id or 0),
        )
    except StepAlreadyDecidedError:
        return task

    if decision == "reject":
        task.approval_ref = {
            "approval_id": _new_id("appr_"),
            "approver_principal_id": actor.principal_id,
            "payload_hash": task.payload_hash,
            "canonicalization": "rfc8785_jcs",
            "decision": "rejected",
            "decided_at": _rfc3339(timestamp),
            "expires_at": _rfc3339(timestamp + timedelta(seconds=max(1, ttl_seconds))),
            "separation_check": "passed",
        }
        session.flush()
        ctx = replace(
            _base_context(session, task, actor, now=timestamp),
            approval_invalid=True,
        )
        _perform_transition(
            session, task, actor, event="approval.rejected", target="denied",
            ctx=ctx, expected_revision=expected_revision,
            reason_code=reason or "approval_missing", now=timestamp,
        )
        task.stop_confirmed = True
        task.result = {
            **dict(task.result or {}),
            "outcome": "none",
            "stop_confirmed": True,
            "stop_evidence_refs": [_EVIDENCE_NEVER_STARTED],
        }
        session.flush()
        return task

    task.approval_ref = {
        "approval_id": _new_id("appr_"),
        "approver_principal_id": actor.principal_id,
        "payload_hash": task.payload_hash,
        "canonicalization": "rfc8785_jcs",
        "decision": "approved",
        "decided_at": _rfc3339(timestamp),
        "expires_at": _rfc3339(timestamp + timedelta(seconds=max(1, ttl_seconds))),
        "separation_check": "passed",
    }
    session.flush()
    record = _decision_record(session, task)
    if record is None:
        raise TaskError(
            "required_input_missing",
            "an evaluated decision is required before approval",
            context={"field": "decision_id"},
        )
    executor = resolve_principal(
        session,
        task.executor_principal_id or "",
        principal_type=task.executor_principal_type,
    )
    fresh = evaluate(
        session,
        EvaluationRequest(
            principal=executor,
            capability=(task.requested_capabilities or [""])[0],
            task_id=task.task_id,
            task_revision=task.state_revision + 1,
            task_idempotency_key=task.idempotency_key,
            resource_scope=task.resource_scope,
            priority=task.priority,
            execution_mode=task.execution_mode,
            risk="low",
            confirmation_ref=(
                task.confirmation_ref if task.confirmation_ref else None
            ),
            approval_ref=task.approval_ref,
            payload_hash=task.payload_hash,
            policy_set_id=task.policy_set_id or DEFAULT_POLICY_SET_ID,
            policy_version=task.policy_version,
            correlation_id=task.correlation_id,
        ),
        now=timestamp,
        evidence_verifier=TaskEvidenceVerifier(session, task_id=task.task_id),
    )
    _apply_decision(
        session,
        task,
        actor,
        fresh,
        from_state="awaiting_approval",
        now=timestamp,
        runtime_capabilities_verified=bool(
            (task.result or {}).get("runtime_capabilities_verified", False)
        ),
    )
    return task


# ---------------------------------------------------------------------------
# Execution recheck (governance PEP + confirmation consumption)
# ---------------------------------------------------------------------------


class TaskEvidenceVerifier:
    """Trusted gate verifier backed by task confirmations/approvals.

    Injected into the governance PEP so a gate is satisfied only by re-reading
    the task-owned evidence, never by a caller-asserted reference.  The verifier
    is bound to one task: a confirmation from another task can never satisfy
    this task, and the confirmer must be an authorised confirmer (requester or
    accountable owner) -- not necessarily the executing principal (C1).
    """

    def __init__(self, session: Session, *, task_id: str | None = None) -> None:
        self.session = session
        self.task_id = task_id

    def _task(self, task_id: str | None) -> Task | None:
        if task_id is None:
            return None
        return self.session.scalar(select(Task).where(Task.task_id == task_id))

    def verify_confirmation(
        self,
        ref: Mapping[str, Any],
        *,
        principal_id: str,
        payload_hash: str | None,
        now: datetime,
    ) -> bool:
        row = _confirmation_row(self.session, ref.get("confirmation_id"))
        if row is None:
            return False
        if self.task_id is None or row.task_id != self.task_id:
            return False
        task = self._task(self.task_id)
        if task is None or task.task_id != row.task_id:
            return False
        confirmer = row.confirmed_by_principal_id
        if confirmer not in {
            task.requester_principal_id,
            task.owner_principal_id,
        }:
            return False
        return (
            row.status == "satisfied"
            and row.consumed_at is None
            and row.consumed_by_call_id is None
            and _to_utc(row.expires_at) > now  # type: ignore[operator]
            and row.payload_hash == ref.get("payload_hash")
            and row.payload_hash == payload_hash
            and row.issued_at is not None
            and row.satisfied_at is not None
            and (_to_utc(row.satisfied_at) or now) <= now  # type: ignore[operator]
        )

    def verify_approval(
        self,
        ref: Mapping[str, Any],
        *,
        requester_principal_id: str,
        owner_principal_ids: frozenset[str],
        payload_hash: str | None,
        now: datetime,
    ) -> str | None:
        approval_id = ref.get("approval_id")
        if not isinstance(approval_id, str):
            return None
        task = self.session.scalar(
            select(Task)
            .where(Task.approval_ref["approval_id"].astext == approval_id)
            .limit(1)
        )
        if task is None or task.approval_ref is None:
            return None
        if self.task_id is not None and task.task_id != self.task_id:
            return None
        stored = task.approval_ref
        if stored.get("decision") != "approved":
            return None
        if stored.get("separation_check") != "passed":
            return None
        if stored.get("revoked_at"):
            return None
        if stored.get("payload_hash") != payload_hash:
            return None
        expires = datetime.fromisoformat(
            str(stored.get("expires_at", "")).replace("Z", "+00:00")
        )
        if _to_utc(expires) <= now:  # type: ignore[operator]
            return None
        approver = str(stored.get("approver_principal_id"))
        if approver == requester_principal_id:
            return None
        if approver in owner_principal_ids:
            return None
        if approver == task.executor_principal_id:
            return None
        return approver


def recheck_execution(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    capability: str,
    call_id: str,
    payload_hash_value: str | None = None,
    resource_scope: Mapping[str, Any] | None = None,
    tool_contract_version: str = "1.0.0",
    channel: str | None = None,
    confirmation_id: str | None = None,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    now: datetime | None = None,
) -> EnforcementResult:
    """Re-check the live task, grant, RBAC and gates immediately before a call.

    Terminal and ``unknown`` tasks refuse any active grant (S-09/F34).  A bound
    confirmation is consumed at most once, in the same transaction as the
    successful enforcement.
    """
    timestamp = now or _utcnow()
    _require_actor(actor)
    task = _load_task(session, task_id, for_update=True)
    if task.state in TASK_TERMINAL_STATES or task.state == "unknown":
        raise TaskError(
            "grant_revoked",
            "a terminal/unknown task cannot execute",
            context={"state": task.state},
        )
    if task.state not in {"queued", "dispatched", "running"}:
        raise TaskError(
            "task_invalid_transition",
            "controlled calls are only allowed while an execution attempt is live",
            context={"state": task.state},
        )
    if not task.grant_id:
        raise TaskError(
            "grant_missing",
            "the task has no execution grant",
            context={"task_id": task.task_id},
        )
    grant = _grant_record(session, task)
    if grant is None:
        raise TaskError(
            "grant_missing",
            "the bound grant no longer exists",
            context={"grant_id": task.grant_id},
        )
    if grant.status == "revoked":
        raise TaskError("grant_revoked", context={"grant_id": grant.grant_id})
    if grant.status == "expired":
        raise TaskError("grant_expired", context={"grant_id": grant.grant_id})
    if grant.status == "consumed" and not (
        task.state in {"running", "verifying"} and task.attempt >= 1
    ):
        raise TaskError("grant_consumed", context={"grant_id": grant.grant_id})
    _assert_task_budget(session, task, grant, now=timestamp)
    # Managed Execution is inside the runtime adapter: a stale worker must not
    # reach the PEP/tool call at all.  Advisory/Governed remain client-driven.
    _assert_managed_lease(
        session,
        task,
        actor=actor,
        worker_id=worker_id,
        fencing_token=fencing_token,
        now=timestamp,
    )

    bound_confirmation = confirmation_id or (
        (task.confirmation_ref or {}).get("confirmation_id")
    )
    try:
        result = enforce_tool_call(
            session,
            grant_id=grant.grant_id,
            principal=actor,
            task_id=task.task_id,
            task_revision=int(grant.task_revision),
            task_idempotency_key=task.idempotency_key,
            capability=capability,
            call_id=call_id,
            resource_scope=resource_scope or task.resource_scope,
            payload_hash=payload_hash_value or task.payload_hash,
            tool_contract_version=tool_contract_version,
            evidence_verifier=TaskEvidenceVerifier(session, task_id=task.task_id),
            now=timestamp,
        )
    except GovernanceError as exc:
        raise TaskError(
            exc.reason_code, f"execution recheck denied: {exc.reason_code}",
            context=exc.context,
        ) from exc

    if bound_confirmation:
        consume_confirmation(
            session,
            task_id=task.task_id,
            actor=actor,
            confirmation_id=bound_confirmation,
            call_id=call_id,
            payload_hash_value=payload_hash_value or task.payload_hash,
            channel=channel,
            now=timestamp,
        )
    return result
