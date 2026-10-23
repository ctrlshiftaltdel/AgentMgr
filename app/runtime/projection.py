"""Structural projection of a server-resolved Grant into a ``RuntimeGrant``.

The dispatcher loads the authoritative task and ``ExecutionGrant`` rows from the
database and passes them here.  This module deliberately reads **attributes
only** (duck-typed), so :mod:`app.runtime` never imports ``app.tasks`` or
``app.governance`` and the runtime package stays free of the task layer.

The function is pure: it opens no session, writes no state, generates no Grant
and never reads a client payload.  A Grant that does not prove an active,
unexpired, managed, task-bound authority is rejected with a stable
:class:`GrantProjectionError` instead of a runtime call.

Errors carry a short structural ``detail``.  They never interpolate a secret, a
raw payload, a host path or a runtime URL — only the fixed reason text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.runtime.contract import RuntimeGrant

__all__ = [
    "GRANT_PROJECTION_REASON_CODES",
    "MANAGED_EXECUTION_MODE",
    "GrantProjectionError",
    "project_runtime_grant",
]

#: The only execution mode this dispatcher may project.  Advisory and governed
#: tasks stay client/PEP driven and never reach a runtime adapter.
MANAGED_EXECUTION_MODE = "managed_execution"

#: Stable reason codes; the dispatcher maps these to dispatch outcomes without
#: parsing error text.
GRANT_PROJECTION_REASON_CODES: frozenset[str] = frozenset(
    {
        "grant_missing",
        "grant_not_active",
        "grant_expired",
        "grant_task_mismatch",
        "grant_revision_mismatch",
        "mode_mismatch",
        "capabilities_missing",
        "scope_invalid",
    }
)

_ACTIVE_GRANT_STATUS = "active"


class GrantProjectionError(Exception):
    """A Grant cannot be projected into a :class:`RuntimeGrant`.

    ``reason_code`` is one of :data:`GRANT_PROJECTION_REASON_CODES` and never
    changes meaning.  ``detail`` is fixed, non-sensitive explanatory text.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        if reason_code not in GRANT_PROJECTION_REASON_CODES:
            raise ValueError(
                f"unknown grant projection reason code: {reason_code!r}"
            )
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")

    def to_dict(self) -> dict[str, str]:
        return {"reason_code": self.reason_code, "detail": self.detail}


def project_runtime_grant(
    task: object, grant: object, *, now: datetime
) -> RuntimeGrant:
    """Project an active managed ``ExecutionGrant`` for a task.

    ``task`` and ``grant`` are loaded domain objects; only attributes are read.
    ``now`` is injected so expiry is decided by one caller-owned clock.

    Raises:
        GrantProjectionError: the Grant does not prove a usable managed
            authority for this task.  The caller must not call the runtime.
    """

    if grant is None:
        raise GrantProjectionError(
            "grant_missing", "the task has no execution grant"
        )

    # -- task / grant binding ------------------------------------------------
    task_grant_id = _text(getattr(task, "grant_id", None))
    grant_id = _text(getattr(grant, "grant_id", None))
    if task_grant_id is None or grant_id is None:
        raise GrantProjectionError("grant_missing", "grant identifier is missing")
    if task_grant_id != grant_id:
        raise GrantProjectionError(
            "grant_task_mismatch", "grant is not the grant bound to the task"
        )

    task_id = _text(getattr(task, "task_id", None))
    grant_task_id = _text(getattr(grant, "task_id", None))
    if task_id is None or grant_task_id is None or task_id != grant_task_id:
        raise GrantProjectionError(
            "grant_task_mismatch", "grant is bound to a different task"
        )

    # -- authority state -----------------------------------------------------
    if getattr(grant, "status", None) != _ACTIVE_GRANT_STATUS:
        raise GrantProjectionError("grant_not_active", "grant status is not active")
    if getattr(grant, "usable", None) is not True:
        raise GrantProjectionError("grant_not_active", "grant is not usable")

    expires_at = getattr(grant, "expires_at", None)
    if not isinstance(expires_at, datetime):
        raise GrantProjectionError("grant_expired", "grant has no expiry")
    if _is_expired(expires_at, now):
        raise GrantProjectionError("grant_expired", "grant has expired")

    # -- managed mode fence --------------------------------------------------
    if getattr(task, "execution_mode", None) != MANAGED_EXECUTION_MODE:
        raise GrantProjectionError(
            "mode_mismatch", "task is not a managed execution task"
        )
    if getattr(grant, "execution_mode", None) != MANAGED_EXECUTION_MODE:
        raise GrantProjectionError(
            "mode_mismatch", "grant execution mode is not managed execution"
        )

    # -- projection invariants ----------------------------------------------
    # The decision-level revision binding belongs to the dispatcher, which owns
    # the decision record; the projection can only pin a positive revision.
    revision = getattr(grant, "task_revision", None)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise GrantProjectionError(
            "grant_revision_mismatch", "grant revision is not a positive integer"
        )

    capabilities = _capabilities(getattr(grant, "capabilities", None))

    resource_scope = getattr(grant, "resource_scope", None)
    if not isinstance(resource_scope, Mapping):
        raise GrantProjectionError(
            "scope_invalid", "grant resource scope is not a mapping"
        )

    return RuntimeGrant(
        grant_id=grant_id,
        task_id=task_id,
        task_revision=revision,
        execution_mode=MANAGED_EXECUTION_MODE,
        capabilities=capabilities,
        resource_scope=dict(resource_scope),
        expires_at=expires_at,
    )


def _text(value: object) -> str | None:
    """Return a non-blank string, or ``None`` when the value is unusable."""

    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    """Compare against ``now`` with naive timestamps treated as UTC."""

    expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    current = now if now.tzinfo else now.replace(tzinfo=UTC)
    return expiry <= current


def _capabilities(value: Any) -> tuple[str, ...]:
    """Return the capability list as a non-empty tuple of non-blank strings."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GrantProjectionError(
            "capabilities_missing", "grant capabilities are not a sequence"
        )
    items = tuple(value)
    if not items or any(
        not isinstance(item, str) or not item.strip() for item in items
    ):
        raise GrantProjectionError(
            "capabilities_missing", "grant declares no usable capabilities"
        )
    return items
