"""The single client-governance application layer.

Both transports call these functions:

* the REST mirror :mod:`app.api.rest.router`;
* the MCP tool surface :mod:`app.api.mcp.server`.

No transport handler re-implements a permission, ownership, PDP, PEP or
business-state rule.  The functions delegate to the existing
``app.tasks.service`` and ``app.governance.service`` implementations; the few
private task helpers imported here are the *same* core authorization helpers,
not a second copy of the rules.

Ownership/identity authority is always taken from the trusted
:class:`~app.api.rest.principal.TrustedActor`.  Owner, executor and approver are
resolved by the server; a client cannot name them, set a role, select a stronger
execution mode or raise priority.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.rest import ratelimit
from app.governance.errors import GovernanceError
from app.api.rest.models import (
    CancelTaskRequest,
    ConfirmationRequest,
    EvaluateTaskRequest,
    SubmitTaskRequest,
)
from app.api.rest.principal import TrustedActor
from app.db.models.organization import Employee
from app.governance import service as governance_service
from app.governance.pdp import DENY_ALL_EVIDENCE, EvaluationRequest
from app.governance.principal import GovernancePrincipal, resolve_employee_principal
from app.identity.models import EmployeeRole, Role
from app.tasks import service as task_service
from app.tasks.errors import TaskError
from app.tasks.models import TASK_TERMINAL_STATES, Task, TaskConfirmation
from app.tasks.service import (
    _load_task,
    _require_confirmation_issuer,
    _require_task_authority,
)
from app.workflow.models import WorkflowInstance

__all__ = [
    "cancel_task",
    "evaluate_task",
    "get_result",
    "get_task",
    "list_my_tasks",
    "request_approval",
    "request_confirmation",
    "submit_task",
    "unit_of_work",
]

#: The same operation names are used by the MCP tools and REST routes so the
#: in-process limiter shares one quota per principal/operation.
OPERATION_EVALUATE = "governance.evaluate_task"
OPERATION_SUBMIT = "governance.submit_task"
OPERATION_GET = "governance.get_task"
OPERATION_LIST = "governance.list_my_tasks"
OPERATION_REQUEST_CONFIRMATION = "governance.request_confirmation"
OPERATION_REQUEST_APPROVAL = "governance.request_approval"
OPERATION_CANCEL = "governance.cancel_task"
OPERATION_RESULT = "governance.get_result"


@contextmanager
def unit_of_work(session: Session) -> Iterator[None]:
    """Commit on success and on a domain rejection; roll back on anything else.

    The task/governance services write a denial audit event and then raise.  A
    plain rollback would discard that audit, so the domain-exception path
    commits the rejection record without changing business state.  Unexpected
    errors roll back.
    """
    try:
        yield
    except (GovernanceError, TaskError):
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()


def _rate_limit(operation: str, actor: TrustedActor) -> None:
    ratelimit.GOVERNANCE_LIMITER.check(f"{actor.principal_id}:{operation}")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _task_summary(task: Task) -> dict[str, Any]:
    """A safe task projection: no employee ids, payloads or credentials."""
    return {
        "task_id": task.task_id,
        "status": task.state,
        "state": task.state,
        "state_revision": task.state_revision,
        "attempt": task.attempt,
        "execution_mode": task.execution_mode,
        "decision_type": task.decision_type,
        "decision_revision": task.decision_revision,
        "summary": task.summary,
        "requested_capabilities": list(task.requested_capabilities or []),
        "record_completeness": task.record_completeness,
        "is_terminal": task.state in TASK_TERMINAL_STATES,
        "created_at": _rfc3339(task.created_at),
        "updated_at": _rfc3339(task.updated_at),
        "queued_at": _rfc3339(task.queued_at),
        "completed_at": _rfc3339(task.completed_at),
    }


def _decision_summary(decision: Any) -> dict[str, Any]:
    constraints = dict(decision.constraints or {})
    return {
        "decision_id": decision.decision_id,
        "decision_type": decision.decision_type,
        "decision_revision": decision.decision_revision,
        "reason_codes": list(decision.reason_codes),
        "policy_set_id": (decision.policy_version or {}).get("policy_set_id"),
        "policy_version": (decision.policy_version or {}).get("version"),
        "execution_mode": constraints.get("execution_mode"),
        "resource_scope": constraints.get("resource_scope"),
        "task_id": (decision.task_ref or {}).get("task_id"),
        "task_revision": (decision.task_ref or {}).get("task_revision"),
        "expires_at": decision.expires_at,
    }


def _load_authorized_task(
    session: Session, task_id: str, actor: TrustedActor
) -> Task:
    """Load a task and apply the core task-service read authority rule.

    Importing ``_require_task_authority`` keeps one definition of "participant,
    privileged identity or trusted service".  A second comparison here would be
    exactly the duplicated authorization rule Issue #80 forbids.
    """
    task = _load_task(session, task_id)
    _require_task_authority(actor.governance, task)
    return task


def _resolve_approver(
    session: Session, actor: TrustedActor
) -> GovernancePrincipal | None:
    """Resolve an independent approver from the directory, never from input.

    Preference order: the actor's manager, then any active employee in the same
    company holding an existing management/finance/administration role.  The
    actor is always excluded, so self-approval cannot be constructed by the
    client.  ``None`` is honest: a policy that requires approval then fails
    closed instead of inventing an approver.
    """
    employee = session.get(Employee, actor.employee_id)
    if employee is not None and employee.manager_id is not None:
        manager = session.get(Employee, employee.manager_id)
        if (
            manager is not None
            and manager.status == "active"
            and manager.id != actor.employee_id
        ):
            try:
                return resolve_employee_principal(session, manager.id)
            except GovernanceError:
                pass

    if actor.governance.company_id is None:
        return None
    rows = session.scalars(
        select(Employee.id)
        .join(EmployeeRole, EmployeeRole.employee_id == Employee.id)
        .join(Role, Role.id == EmployeeRole.role_id)
        .where(
            Employee.company_id == actor.governance.company_id,
            Employee.status == "active",
            Employee.id != actor.employee_id,
            Role.name.in_(("manager", "finance_lead", "sysadmin")),
        )
        .order_by(Employee.id)
    ).all()
    for employee_id in rows:
        try:
            return resolve_employee_principal(session, employee_id)
        except GovernanceError:
            continue
    return None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def evaluate_task(
    session: Session,
    actor: TrustedActor,
    request: EvaluateTaskRequest,
) -> dict[str, Any]:
    """Evaluate a proposed/existing task without mutating task state.

    The PDP persists an immutable decision/audit record by contract.  This
    operation never creates a task, issues a grant, mutates a task, or consumes
    a confirmation/approval; ``read_only`` is therefore true in the business
    sense (the test asserts task state/revision and grant count are unchanged).
    """
    _rate_limit(OPERATION_EVALUATE, actor)
    task: Task | None = None
    if request.task_id is not None:
        task = _load_authorized_task(session, request.task_id, actor)
        scope: Mapping[str, Any] | None = task.resource_scope
        execution_mode = task.execution_mode
        task_revision = task.state_revision
        priority = task.priority
        payload_hash = task.payload_hash
        correlation_id = task.correlation_id
        evidence_verifier = task_service.TaskEvidenceVerifier(
            session, task_id=task.task_id
        )
    else:
        scope = (
            request.resource_scope.model_dump()
            if request.resource_scope is not None
            else None
        )
        execution_mode = "advisory"
        task_revision = None
        priority = "normal"
        payload_hash = None
        correlation_id = None
        evidence_verifier = DENY_ALL_EVIDENCE

    decision = governance_service.evaluate(
        session,
        EvaluationRequest(
            principal=actor.governance,
            capability=request.capability,
            task_id=request.task_id,
            task_revision=task_revision,
            task_idempotency_key=task.idempotency_key if task is not None else None,
            resource_scope=scope,
            priority=priority,
            execution_mode=execution_mode,
            risk="low",
            payload_hash=payload_hash,
            correlation_id=correlation_id,
            pre_evaluation=request.task_id is None,
        ),
        evidence_verifier=evidence_verifier,
    )
    result = _decision_summary(decision)
    result["read_only"] = True
    result["task_state_mutated"] = False
    return result


def submit_task(
    session: Session, actor: TrustedActor, request: SubmitTaskRequest
) -> dict[str, Any]:
    """Submit an advisory task; owner/executor/approver are server-resolved."""
    _rate_limit(OPERATION_SUBMIT, actor)
    approver = _resolve_approver(session, actor)
    task = task_service.submit_task(
        session,
        requester=actor.governance,
        idempotency_key=request.idempotency_key,
        goal=request.goal.model_dump(exclude_none=True),
        owner=actor.governance,
        executor=actor.governance,
        assignee=actor.governance,
        approver=approver,
        summary=request.summary,
        priority="normal",
        execution_mode="advisory",
        resource_scope=request.resource_scope.model_dump(),
        requested_capabilities=tuple(request.requested_capabilities),
        payload=request.payload,
        max_attempts=request.max_attempts,
        retry_budget=request.retry_budget,
        retry_safe=request.retry_safe,
        max_calls=request.max_calls,
        max_runtime_seconds=request.max_runtime_seconds,
        max_cost_units=request.max_cost_units,
        client_task_ref=request.client_task_ref,
        correlation_id=request.correlation_id,
    )
    return _task_summary(task)


def get_task(
    session: Session, actor: TrustedActor, task_id: str
) -> dict[str, Any]:
    """Return one authorized task summary."""
    _rate_limit(OPERATION_GET, actor)
    task = _load_authorized_task(session, task_id, actor)
    return _task_summary(task)


def list_my_tasks(
    session: Session, actor: TrustedActor, *, limit: int = 50
) -> dict[str, Any]:
    """List tasks where the caller is a participant (never someone else's)."""
    _rate_limit(OPERATION_LIST, actor)
    bounded = max(1, min(int(limit), 100))
    principal_id = actor.principal_id
    rows = session.scalars(
        select(Task)
        .where(
            or_(
                Task.requester_principal_id == principal_id,
                Task.owner_principal_id == principal_id,
                Task.assignee_principal_id == principal_id,
                Task.executor_principal_id == principal_id,
                Task.approver_principal_id == principal_id,
            )
        )
        .order_by(Task.created_at.desc())
        .limit(bounded)
    ).all()
    return {"tasks": [_task_summary(task) for task in rows], "count": len(rows)}


def request_confirmation(
    session: Session,
    actor: TrustedActor,
    task_id: str,
    request: ConfirmationRequest,
) -> dict[str, Any]:
    """Mint (or return) a pending confirmation for the task's exact payload."""
    _rate_limit(OPERATION_REQUEST_CONFIRMATION, actor)
    task = _load_authorized_task(session, task_id, actor)
    _require_confirmation_issuer(actor.governance, task)
    if task.state in TASK_TERMINAL_STATES:
        raise TaskError(
            "task_terminal_state_immutable",
            "a terminal task cannot request a confirmation",
            context={"task_id": task.task_id, "state": task.state},
        )
    existing = session.scalar(
        select(TaskConfirmation)
        .where(
            TaskConfirmation.task_id == task.task_id,
            TaskConfirmation.status.in_(("pending", "satisfied")),
            TaskConfirmation.payload_hash == task.payload_hash,
        )
        .order_by(TaskConfirmation.id.desc())
        .limit(1)
    )
    row = existing or task_service.create_confirmation(
        session,
        task_id=task.task_id,
        actor=actor.governance,
        ttl_seconds=request.ttl_seconds,
    )
    return {
        "task_id": task.task_id,
        "confirmation_id": row.confirmation_id,
        "status": row.status,
        "purpose": row.purpose,
        "payload_hash": row.payload_hash,
        "issued_at": _rfc3339(row.issued_at),
        "expires_at": _rfc3339(row.expires_at),
        "single_use": bool(row.single_use),
    }


def request_approval(
    session: Session, actor: TrustedActor, task_id: str
) -> dict[str, Any]:
    """Return the existing independent approval request for an awaiting task.

    This is a *request* endpoint: it never records an approval decision and
    never accepts an approver from the client.  The actual decision remains a
    human action in the approval workflow.
    """
    _rate_limit(OPERATION_REQUEST_APPROVAL, actor)
    task = _load_authorized_task(session, task_id, actor)
    if task.state != "awaiting_approval":
        raise ApiError(
            409,
            "task_invalid_transition",
            "approval is not expected in the current task state",
        )
    if task.workflow_instance_id is None:
        raise ApiError(
            409,
            "task_approval_not_found",
            "no approval request exists for this task",
        )
    instance = session.get(WorkflowInstance, task.workflow_instance_id)
    if instance is None:
        raise ApiError(
            409,
            "task_approval_not_found",
            "no approval request exists for this task",
        )
    return {
        "task_id": task.task_id,
        "approval_request_id": str(instance.id),
        "workflow_type": instance.workflow_type,
        "status": instance.status,
        "approver_principal_id": task.approver_principal_id,
        "task_state": task.state,
    }


def cancel_task(
    session: Session,
    actor: TrustedActor,
    task_id: str,
    request: CancelTaskRequest,
) -> dict[str, Any]:
    """Register a cancellation; a request is never reported as a stop."""
    _rate_limit(OPERATION_CANCEL, actor)
    task = task_service.request_cancel(
        session,
        task_id=task_id,
        actor=actor.governance,
        cancel_id=request.cancel_id,
    )
    return _task_summary(task)


def get_result(
    session: Session, actor: TrustedActor, task_id: str
) -> dict[str, Any]:
    """Return only the authorized, whitelisted result/evidence references."""
    _rate_limit(OPERATION_RESULT, actor)
    task = _load_authorized_task(session, task_id, actor)
    state = task.state
    result = dict(task.result or {})
    available = state in {
        "verifying",
        "succeeded",
        "partial_success",
        "failed",
        "cancelled",
        "timed_out",
        "denied",
        "unknown",
    }
    if not available:
        return {
            "task_id": task.task_id,
            "state": state,
            "status": state,
            "result": None,
            "note": "result_not_available",
        }

    verification = result.get("verification") or {}
    evidence_refs = result.get("evidence_refs") or []
    safe_evidence = [
        ref for ref in evidence_refs if isinstance(ref, str) and ref
    ][:100]
    safe_result: dict[str, Any] = {
        "outcome": result.get("outcome"),
        "result_ref": result.get("result_ref"),
        "evidence_refs": safe_evidence,
        "verification": (
            {
                "result": verification.get("result"),
                "verified_at": verification.get("verified_at"),
            }
            if verification
            else None
        ),
        "stop_confirmed": bool(result.get("stop_confirmed", task.stop_confirmed)),
        "side_effects_reconciled": bool(
            result.get("side_effects_reconciled", False)
        ),
    }
    return {
        "task_id": task.task_id,
        "state": state,
        "status": state,
        "result": safe_result,
        "completed_at": _rfc3339(task.completed_at),
        "note": "terminal" if state in TASK_TERMINAL_STATES else "in_progress",
    }
