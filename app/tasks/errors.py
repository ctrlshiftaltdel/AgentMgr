"""Stable error and reason codes for the task domain.

Every rejection carries a machine-judgeable ``reason_code``.  Natural-language
messages are explanatory only and never a substitute for the code.

Alignment with the frozen contracts
-----------------------------------
``CONTRACT_REASON_CODES`` is the reason-code enum of
``docs/contracts/governance-decision.schema.json`` (re-exported by
:mod:`app.governance.errors`).  Whenever a task rejection corresponds to a
governance outcome, the task layer **reuses that code** instead of inventing a
synonym -- for example a missing accountable owner is
``accountable_owner_missing``, a replayed confirmation is ``replay_detected`` and
a stop without evidence is ``stop_not_confirmed``.

Pure lifecycle codes that have no governance-decision meaning (an illegal state
transition, a stale fencing token, a dependency cycle, ...) are listed in
``TASK_OPERATION_CODES`` and prefixed with the ``task_``/``lease_`` namespace so
they can never be confused with a policy verdict.  ``ALL_TASK_REASON_CODES`` is
the closed set this package may emit; :mod:`app.tests.tasks.test_errors` asserts
it stays a superset of the contract codes it claims and never contains secrets.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from app.governance.errors import REASON_CODES as CONTRACT_REASON_CODES

__all__ = [
    "ALL_TASK_REASON_CODES",
    "CONTRACT_REASON_CODES",
    "ConfirmationError",
    "DependencyError",
    "IdempotencyConflict",
    "LeaseError",
    "NecessityDecision",
    "OwnerError",
    "TASK_OPERATION_CODES",
    "TaskError",
    "TaskForbiddenTransition",
    "TaskInvalidTransition",
    "TaskNotFound",
    "TaskStateConflict",
    "VerificationError",
    "raise_task_error",
]

#: Lifecycle/operational codes that are not governance-decision reason codes.
TASK_OPERATION_CODES: frozenset[str] = frozenset(
    {
        "task_not_found",
        "task_invalid_transition",
        "task_forbidden_transition",
        "task_guard_rejected",
        "task_terminal_state_immutable",
        "task_state_revision_conflict",
        "task_idempotency_conflict",
        "task_duplicate",
        "task_already_satisfied",
        "task_missing_input",
        "task_unauthorized_actor",
        "task_owner_inactive",
        "task_missing_owner_input",
        "task_dependency_blocked",
        "task_dependency_cycle",
        "task_dependency_invalid",
        "task_attempt_missing",
        "task_attempt_rollback_forbidden",
        "task_active_attempt_unreconciled",
        "task_unsafe_retry_forbidden",
        "task_retry_requires_fresh_authorization",
        "task_priority_unauthorized",
        "task_priority_request_invalid",
        "task_confirmation_pending",
        "task_confirmation_not_found",
        "task_confirmation_actor_invalid",
        "task_confirmation_expired",
        "task_confirmation_channel_mismatch",
        "task_approval_not_found",
        "task_approval_actor_invalid",
        "task_approver_unresolved",
        "task_verification_self_report",
        "task_verification_inconclusive",
        "task_completion_criteria_missing",
        "task_completion_evidence_incomplete",
        "task_lease_not_held",
        "task_lease_expired",
        "task_lease_active",
        "task_fencing_token_stale",
        "task_worker_mismatch",
        "task_mode_capability_missing",
        "task_execution_handle_missing",
        "task_reconcile_evidence_missing",
        "task_result_missing",
    }
)

#: Closed set of codes the task layer may emit.
ALL_TASK_REASON_CODES: frozenset[str] = CONTRACT_REASON_CODES | TASK_OPERATION_CODES


class TaskError(Exception):
    """Base class for every deterministic task rejection.

    ``reason_code`` is always in :data:`ALL_TASK_REASON_CODES`; ``context`` is a
    structured, non-sensitive payload (ids, state names, revision numbers).  It
    must never contain credentials, free-text personal data or secrets.
    """

    def __init__(
        self,
        reason_code: str,
        message: str | None = None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        if reason_code not in ALL_TASK_REASON_CODES:
            raise ValueError(f"unknown task reason code: {reason_code!r}")
        self.reason_code = reason_code
        self.context: dict[str, Any] = dict(context or {})
        if message is None:
            message = f"task rejected: {reason_code}"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"reason_code": self.reason_code, **self.context}


class TaskNotFound(TaskError):
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(
            "task_not_found",
            f"task {task_id!r} not found",
            context={"task_id": task_id},
        )


class TaskStateConflict(TaskError):
    """A concurrent writer used an outdated ``state_revision``."""

    def __init__(self, task_id: str, *, expected: int, actual: int) -> None:
        super().__init__(
            "task_state_revision_conflict",
            (
                f"task {task_id!r} revision conflict: "
                f"expected {expected}, actual {actual}"
            ),
            context={"task_id": task_id, "expected_revision": expected,
                     "actual_revision": actual},
        )


class TaskInvalidTransition(TaskError):
    def __init__(self, *, from_state: str, event: str, to_state: str | None) -> None:
        super().__init__(
            "task_invalid_transition",
            f"{from_state} --{event}--> {to_state} is not an allowed transition",
            context={
                "from_state": from_state,
                "event": event,
                "to_state": to_state,
            },
        )


class TaskForbiddenTransition(TaskInvalidTransition):
    """An explicitly forbidden transition (F01-F41 in the frozen document)."""

    def __init__(
        self,
        *,
        from_state: str,
        event: str,
        to_state: str | None,
        rule: str,
        detail: str,
    ) -> None:
        TaskError.__init__(
            self,
            "task_forbidden_transition",
            f"{rule}: {detail}",
            context={
                "from_state": from_state,
                "event": event,
                "to_state": to_state,
                "rule": rule,
                "detail": detail,
            },
        )


class OwnerError(TaskError):
    pass


class IdempotencyConflict(TaskError):
    def __init__(self, task_id: str | None, *, idempotency_key: str) -> None:
        super().__init__(
            "task_idempotency_conflict",
            "the idempotency key was already used with a different payload",
            context={
                "idempotency_key": idempotency_key,
                "existing_task_id": task_id,
            },
        )


class ConfirmationError(TaskError):
    pass


class LeaseError(TaskError):
    pass


class DependencyError(TaskError):
    pass


class VerificationError(TaskError):
    pass


class NecessityDecision(str):
    """Deterministic outcome of the pre-submission necessity checks."""

    # Values are plain strings so they serialize without ceremony.
    NEW = "new"
    DUPLICATE = "duplicate"
    ALREADY_SATISFIED = "already_satisfied"
    MISSING_INPUT = "missing_input"


def raise_task_error(
    reason_code: str,
    message: str | None = None,
    *,
    context: Mapping[str, Any] | None = None,
) -> NoReturn:
    """Raise a :class:`TaskError` (mirrors ``governance.errors.deny``)."""

    raise TaskError(reason_code, message, context=context)
