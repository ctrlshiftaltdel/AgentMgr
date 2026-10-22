"""REST mirror of the client MCP governance tools (B1 boundary).

Every route is a thin adapter over :mod:`app.api.rest.operations`; the MCP tool
surface calls the exact same functions.  Handlers do not decide permissions,
ownership, state transitions, approval/confirmation gates or priority.

Route / MCP-tool map
--------------------
``POST /governance/tasks/evaluate``          -> ``governance.evaluate_task``
``POST /governance/tasks``                   -> ``governance.submit_task``
``GET  /governance/tasks``                   -> ``governance.list_my_tasks``
``GET  /governance/tasks/{task_id}``         -> ``governance.get_task``
``POST /governance/tasks/{task_id}/confirmations`` -> ``governance.request_confirmation``
``POST /governance/tasks/{task_id}/approvals``     -> ``governance.request_approval``
``POST /governance/tasks/{task_id}/cancel``  -> ``governance.cancel_task``
``GET  /governance/tasks/{task_id}/result``  -> ``governance.get_result``
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import get_principal
from app.api.rest import operations
from app.api.rest.models import (
    CancelTaskRequest,
    ConfirmationRequest,
    EvaluateTaskRequest,
    SubmitTaskRequest,
)
from app.api.rest.principal import actor_from_identity
from app.db.session import get_session
from app.identity.authorization import Principal

__all__ = ["router"]

router = APIRouter(prefix="/governance", tags=["governance"])

_TASK_ID = r"^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
TaskId = Annotated[str, Path(pattern=_TASK_ID)]


def _execute(session: Session, principal: Principal, operation):
    """Resolve the trusted actor and run one operation in the shared UoW."""
    with operations.unit_of_work(session):
        actor = actor_from_identity(session, principal)
        return operation(actor)


@router.post("/tasks/evaluate")
def evaluate_task(
    body: EvaluateTaskRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Read-only PDP evaluation; never mutates task state or issues a grant."""
    return _execute(
        session, principal, lambda actor: operations.evaluate_task(session, actor, body)
    )


@router.post("/tasks", status_code=201)
def submit_task(
    body: SubmitTaskRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Submit an advisory task and return its task id + current status."""
    return _execute(
        session, principal, lambda actor: operations.submit_task(session, actor, body)
    )


@router.get("/tasks")
def list_my_tasks(
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """List only tasks where the caller is a participant."""
    return _execute(
        session,
        principal,
        lambda actor: operations.list_my_tasks(session, actor, limit=limit),
    )


@router.get("/tasks/{task_id}")
def get_task(
    task_id: TaskId,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Return one task summary when the caller is authorized to read it."""
    return _execute(
        session, principal, lambda actor: operations.get_task(session, actor, task_id)
    )


@router.post("/tasks/{task_id}/confirmations", status_code=201)
def request_confirmation(
    task_id: TaskId,
    body: ConfirmationRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Request (or return) a pending confirmation bound to the task payload."""
    return _execute(
        session,
        principal,
        lambda actor: operations.request_confirmation(session, actor, task_id, body),
    )


@router.post("/tasks/{task_id}/approvals")
def request_approval(
    task_id: TaskId,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Return the existing independent approval request for an awaiting task."""
    return _execute(
        session,
        principal,
        lambda actor: operations.request_approval(session, actor, task_id),
    )


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: TaskId,
    body: CancelTaskRequest | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Register a cancellation request; it is not reported as a confirmed stop."""
    return _execute(
        session,
        principal,
        lambda actor: operations.cancel_task(
            session, actor, task_id, body or CancelTaskRequest()
        ),
    )


@router.get("/tasks/{task_id}/result")
def get_result(
    task_id: TaskId,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Return only authorized result/evidence references, never a raw payload."""
    return _execute(
        session,
        principal,
        lambda actor: operations.get_result(session, actor, task_id),
    )
