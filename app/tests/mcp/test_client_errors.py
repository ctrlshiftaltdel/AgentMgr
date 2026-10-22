"""Stable error mapping for the client governance surface (no database)."""

from __future__ import annotations

from app.api.errors import ApiError
from app.api.rest.errors import to_api_error
from app.governance.errors import GovernanceError
from app.tasks.errors import TaskError, TaskNotFound


def test_task_reason_codes_map_to_safe_http_errors():
    forbidden = to_api_error(TaskError("task_unauthorized_actor"))
    assert (forbidden.status_code, forbidden.code) == (403, "task_unauthorized_actor")
    assert forbidden.message == "access denied"

    conflict = to_api_error(TaskError("task_terminal_state_immutable"))
    assert (conflict.status_code, conflict.code) == (
        409,
        "task_terminal_state_immutable",
    )

    validation = to_api_error(TaskError("required_input_missing"))
    assert (validation.status_code, validation.code) == (
        422,
        "required_input_missing",
    )

    missing = to_api_error(TaskNotFound("task_missing"))
    assert (missing.status_code, missing.code) == (404, "task_not_found")


def test_governance_denials_are_403_and_do_not_leak_context():
    denied = to_api_error(
        GovernanceError(
            "resource_scope_denied",
            context={"resource_ids": ["res_secret"]},
        )
    )
    assert (denied.status_code, denied.code) == (403, "resource_scope_denied")
    assert "res_secret" not in denied.message
    assert "resource_ids" not in denied.message


def test_api_errors_and_unknown_errors_are_safe():
    limited = ApiError(429, "rate_limited", "rate limit exceeded")
    assert to_api_error(limited) is limited

    crashed = to_api_error(RuntimeError("postgresql://user:secret@host/db"))
    assert (crashed.status_code, crashed.code) == (500, "internal_error")
    assert "postgresql" not in crashed.message
    assert "secret" not in crashed.message
