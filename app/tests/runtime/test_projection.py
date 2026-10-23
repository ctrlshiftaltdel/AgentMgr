"""DB-free tests for the structural ExecutionGrant -> RuntimeGrant projection.

The projection only reads attributes, so these tests use small dataclass stubs
instead of ``app.tasks``/``app.governance`` models.  That also pins the import
boundary: :mod:`app.runtime.projection` must never depend on the task layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.runtime.contract import RuntimeGrant
from app.runtime.projection import (
    GRANT_PROJECTION_REASON_CODES,
    GrantProjectionError,
    project_runtime_grant,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(minutes=5)

TASK_ID = "task_runtime_projection_0001"
GRANT_ID = "grnt_runtime_projection_0001"


@dataclass
class _Task:
    """Structural stand-in for ``app.tasks.models.Task``."""

    task_id: str = TASK_ID
    grant_id: str | None = GRANT_ID
    execution_mode: str = "managed_execution"


@dataclass
class _Grant:
    """Structural stand-in for ``app.governance.models.ExecutionGrant``."""

    grant_id: str = GRANT_ID
    task_id: str = TASK_ID
    task_revision: Any = 2
    execution_mode: str = "managed_execution"
    capabilities: Any = ("expense.read@1.0.0",)
    resource_scope: Any = field(
        default_factory=lambda: {"resources": ["expense:exp_0001"]}
    )
    expires_at: datetime | None = EXPIRES_AT
    status: str = "active"
    usable: bool = True


def _project(
    *,
    task: _Task | None = None,
    grant: _Grant | None = None,
) -> RuntimeGrant:
    return project_runtime_grant(
        _Task() if task is None else task,
        _Grant() if grant is None else grant,
        now=NOW,
    )


# -- happy path ---------------------------------------------------------------


def test_projection_accepts_active_managed_grant():
    task, grant = _Task(), _Grant()

    projected = _project(task=task, grant=grant)

    assert isinstance(projected, RuntimeGrant)
    assert projected.grant_id == grant.grant_id
    assert projected.task_id == task.task_id
    assert projected.task_revision == 2
    assert projected.execution_mode == "managed_execution"
    assert projected.capabilities == ("expense.read@1.0.0",)
    assert dict(projected.resource_scope) == grant.resource_scope
    assert projected.expires_at == EXPIRES_AT


def test_projection_preserves_capability_order_and_casts_to_tuple():
    grant = _Grant(capabilities=["expense.read@1.0.0", "expense.submit@1.0.0"])

    projected = _project(grant=grant)

    assert isinstance(projected.capabilities, tuple)
    assert projected.capabilities == ("expense.read@1.0.0", "expense.submit@1.0.0")


def test_projection_accepts_short_lived_grant_before_deadline():
    grant = _Grant(expires_at=NOW + timedelta(microseconds=1))

    projected = _project(grant=grant)

    assert projected.expires_at == grant.expires_at


# -- grant binding ------------------------------------------------------------


def test_projection_rejects_missing_grant():
    with pytest.raises(GrantProjectionError) as exc:
        project_runtime_grant(_Task(), None, now=NOW)

    assert exc.value.reason_code == "grant_missing"


@pytest.mark.parametrize("grant_id", [None, ""], ids=["none", "blank"])
def test_projection_rejects_task_without_grant_reference(grant_id):
    with pytest.raises(GrantProjectionError) as exc:
        _project(task=_Task(grant_id=grant_id))

    assert exc.value.reason_code == "grant_missing"


def test_projection_rejects_grant_not_bound_to_task():
    with pytest.raises(GrantProjectionError) as exc:
        _project(task=_Task(grant_id="grnt_other_grant_0001"))

    assert exc.value.reason_code == "grant_task_mismatch"


def test_projection_rejects_grant_for_other_task():
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(task_id="task_other_0001"))

    assert exc.value.reason_code == "grant_task_mismatch"


def test_projection_rejects_objects_without_grant_attributes():
    """Structural callers get a stable domain error, not an ``AttributeError``."""

    with pytest.raises(GrantProjectionError) as exc:
        project_runtime_grant(object(), object(), now=NOW)

    assert exc.value.reason_code == "grant_missing"


# -- authority state ----------------------------------------------------------


@pytest.mark.parametrize(
    "status", ["revoked", "consumed", "expired", "pending"], ids=lambda s: s
)
def test_projection_rejects_grant_with_non_active_status(status):
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(status=status))

    assert exc.value.reason_code == "grant_not_active"


def test_projection_rejects_unusable_grant():
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(usable=False))

    assert exc.value.reason_code == "grant_not_active"


@pytest.mark.parametrize(
    "expires_at",
    [NOW, NOW - timedelta(seconds=1)],
    ids=["at_deadline", "already_past"],
)
def test_projection_rejects_expired_grant(expires_at):
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(expires_at=expires_at))

    assert exc.value.reason_code == "grant_expired"


def test_projection_rejects_grant_without_expiry():
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(expires_at=None))

    assert exc.value.reason_code == "grant_expired"


# -- mode fence ---------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    ["advisory", "governed_resource_access", "undetermined"],
    ids=["advisory", "governed", "undetermined"],
)
def test_projection_rejects_non_managed_task_mode(mode):
    with pytest.raises(GrantProjectionError) as exc:
        _project(task=_Task(execution_mode=mode))

    assert exc.value.reason_code == "mode_mismatch"


def test_projection_rejects_non_managed_grant_mode():
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(execution_mode="advisory"))

    assert exc.value.reason_code == "mode_mismatch"


# -- revision, capabilities and scope ----------------------------------------


@pytest.mark.parametrize("revision", [0, -1], ids=["zero", "negative"])
def test_projection_rejects_non_positive_revision(revision):
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(task_revision=revision))

    assert exc.value.reason_code == "grant_revision_mismatch"


@pytest.mark.parametrize(
    "revision",
    [True, "2", 2.0, None],
    ids=["bool", "string", "float", "none"],
)
def test_projection_rejects_non_integer_revision(revision):
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(task_revision=revision))

    assert exc.value.reason_code == "grant_revision_mismatch"


@pytest.mark.parametrize(
    "capabilities",
    [(), [], ("",), ("   ",), (None,), ("expense.read@1.0.0", 7)],
    ids=["empty_tuple", "empty_list", "blank", "whitespace", "none_item", "int_item"],
)
def test_projection_rejects_missing_capabilities(capabilities):
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(capabilities=capabilities))

    assert exc.value.reason_code == "capabilities_missing"


def test_projection_rejects_non_sequence_capabilities():
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(capabilities="expense.read@1.0.0"))

    assert exc.value.reason_code == "capabilities_missing"


@pytest.mark.parametrize(
    "resource_scope",
    [None, [], ["expense:exp_0001"], "expense:exp_0001", 42],
    ids=["none", "empty_list", "list", "string", "int"],
)
def test_projection_rejects_non_mapping_scope(resource_scope):
    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(resource_scope=resource_scope))

    assert exc.value.reason_code == "scope_invalid"


# -- error hygiene ------------------------------------------------------------


def test_projection_error_does_not_render_scope_values():
    secret = "sk_live_projection_scope_secret"

    with pytest.raises(GrantProjectionError) as exc:
        _project(grant=_Grant(resource_scope=secret))

    assert exc.value.reason_code == "scope_invalid"
    rendered = f"{exc.value} {exc.value!r} {exc.value.detail}"
    assert secret not in rendered


def test_reason_codes_are_stable():
    assert GRANT_PROJECTION_REASON_CODES == frozenset(
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


def test_error_rejects_unknown_reason_code():
    with pytest.raises(ValueError):
        GrantProjectionError("grant_widened", "unknown reason")
