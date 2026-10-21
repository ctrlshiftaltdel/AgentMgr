"""Stable reason codes: contract alignment and a closed, non-secret vocabulary."""

from __future__ import annotations

import inspect

import pytest

from app.governance.errors import REASON_CODES
from app.tasks import state_machine
from app.tasks.errors import (
    ALL_TASK_REASON_CODES,
    CONTRACT_REASON_CODES,
    TASK_OPERATION_CODES,
    TaskError,
)


def test_contract_codes_are_reused_verbatim() -> None:
    assert CONTRACT_REASON_CODES is REASON_CODES
    for code in (
        "accountable_owner_missing",
        "self_approval_forbidden",
        "separation_of_duties_violation",
        "confirmation_missing",
        "approval_missing",
        "payload_hash_mismatch",
        "replay_detected",
        "grant_missing",
        "grant_revoked",
        "grant_expired",
        "grant_consumed",
        "grant_scope_mismatch",
        "stop_not_confirmed",
        "evidence_missing",
        "retry_budget_exhausted",
        "priority_cannot_override_deny",
    ):
        assert code in CONTRACT_REASON_CODES
        assert code in ALL_TASK_REASON_CODES


def test_operation_codes_do_not_collide_with_policy_codes() -> None:
    assert not (TASK_OPERATION_CODES & CONTRACT_REASON_CODES)
    assert TASK_OPERATION_CODES
    assert ALL_TASK_REASON_CODES == CONTRACT_REASON_CODES | TASK_OPERATION_CODES


def test_every_guard_reason_code_is_declared() -> None:
    default = state_machine.TransitionContext()
    for name, guard in state_machine.GUARDS.items():
        reason = guard(default)
        if reason is not None:
            assert reason in ALL_TASK_REASON_CODES, (name, reason)


def test_unknown_reason_code_is_refused() -> None:
    with pytest.raises(ValueError):
        TaskError("totally_made_up")


def test_task_error_context_is_structured_and_secret_free() -> None:
    error = TaskError(
        "task_duplicate",
        "duplicate",
        context={"existing_task_id": "task_abc_0001", "state": "queued"},
    )
    assert error.to_dict() == {
        "reason_code": "task_duplicate",
        "existing_task_id": "task_abc_0001",
        "state": "queued",
    }
    forbidden = ("password", "secret", "token=", "api_key", "bearer ")
    payload = repr(error.to_dict()).lower()
    for needle in forbidden:
        assert needle not in payload


def test_reason_codes_are_documentation_stable() -> None:
    # A guard's code must not be a bare exception name or a translated sentence.
    for code in ALL_TASK_REASON_CODES:
        assert code == code.lower()
        assert " " not in code
        assert code.isascii()
