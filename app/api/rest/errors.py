"""Stable error mapping for the client governance surface.

Every rejection is reduced to ``(http_status, stable_code, safe_message)``.
Reason codes from the task and governance domains are already stable, closed
sets, so they become the API/tool error code unchanged.  The domain ``context``
is never rendered: it can contain ids and state, but the response only carries
the code and a static message.  No stack trace, SQL or credential is ever
returned.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.errors import ApiError, AuthError, error_body
from app.governance.errors import GovernanceError, PrincipalResolutionError
from app.tasks.errors import TaskError, TaskNotFound

__all__ = [
    "error_body",
    "register_governance_exception_handlers",
    "to_api_error",
]

#: Reason codes that mean "the caller may not do this" -> 403.
_FORBIDDEN_CODES = frozenset(
    {
        "approval_missing",
        "capability_not_granted",
        "confirmation_missing",
        "identity_unverified",
        "payload_hash_mismatch",
        "policy_denied",
        "principal_disabled",
        "principal_unbound",
        "replay_detected",
        "resource_scope_denied",
        "self_approval_forbidden",
        "separation_of_duties_violation",
        "task_approval_actor_invalid",
        "task_confirmation_actor_invalid",
        "task_priority_unauthorized",
        "task_unauthorized_actor",
    }
)

#: Reason codes that mean "the request conflicts with durable state" -> 409.
_CONFLICT_CODES = frozenset(
    {
        "decision_revision_stale",
        "grant_scope_mismatch",
        "task_already_satisfied",
        "task_approval_not_found",
        "task_confirmation_not_found",
        "task_duplicate",
        "task_forbidden_transition",
        "task_idempotency_conflict",
        "task_invalid_transition",
        "task_state_revision_conflict",
        "task_terminal_state_immutable",
    }
)

_NOT_FOUND_CODES = frozenset({"task_not_found"})

_SAFE_MESSAGES = {
    403: "access denied",
    404: "resource not found",
    409: "request conflicts with the current state",
    422: "request failed validation",
    429: "rate limit exceeded",
}


def _status_for(code: str) -> int:
    if code in _NOT_FOUND_CODES:
        return 404
    if code in _FORBIDDEN_CODES:
        return 403
    if code in _CONFLICT_CODES:
        return 409
    return 422


def to_api_error(exc: Exception) -> ApiError:
    """Map a domain/API exception to a safe, stable :class:`ApiError`.

    ``ApiError`` (including the existing auth 401/403/503 errors) is preserved
    so the pre-existing unified handler keeps its status and code.
    """
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, TaskNotFound):
        return ApiError(404, exc.reason_code, _SAFE_MESSAGES[404])
    if isinstance(exc, TaskError):
        status = _status_for(exc.reason_code)
        return ApiError(status, exc.reason_code, _SAFE_MESSAGES[status])
    if isinstance(exc, PrincipalResolutionError):
        status = _status_for(exc.reason_code)
        return ApiError(status, exc.reason_code, _SAFE_MESSAGES[status])
    if isinstance(exc, GovernanceError):
        status = _status_for(exc.reason_code)
        return ApiError(status, exc.reason_code, _SAFE_MESSAGES[status])
    return ApiError(500, "internal_error", "internal server error")


def register_governance_exception_handlers(app: FastAPI) -> None:
    """Register handlers for domain errors raised by the governance REST mirror."""

    async def _handled(_request: Request, exc: Exception) -> JSONResponse:
        mapped = to_api_error(exc)
        return JSONResponse(
            status_code=mapped.status_code,
            content=error_body(mapped.code, mapped.message),
        )

    for cls in (TaskNotFound, TaskError, PrincipalResolutionError, GovernanceError):
        app.add_exception_handler(cls, _handled)

    # ``AuthError`` is an ``ApiError`` and is already handled globally, but
    # registering it here documents the auth contract of this surface.
    app.add_exception_handler(AuthError, _handled)
