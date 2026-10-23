"""Managed dispatcher preflight: claim fence, lease release and fail-closed.

These tests use the real PostgreSQL task fixtures and the scripted
:class:`FakeRuntimeAdapter`.  The fake is test-only evidence; nothing here
claims that a real runtime supports Managed Execution.

Scope of this file in Task 3: ``run_once`` stops at a successful preflight and
never submits.  Submit/status/cancel behaviour arrives in Tasks 4-7.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.errors import PrincipalResolutionError
from app.governance.grants import revoke_grant
from app.governance.principal import (
    GovernancePrincipal,
    resolve_service_principal,
)
from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    Capability,
    CapabilityStatus,
    HealthStatus,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeType,
    VersionSource,
)
from app.tasks.errors import TaskError
from app.tasks.execution import (
    DispatcherConfig,
    DispatcherResult,
    RuntimeDispatcher,
)
from app.tasks.models import Task, TaskLease
from app.tests.governance.helpers import make_service_identity
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.fake_runtime import FakeRuntimeAdapter
from app.tests.tasks.helpers import submit_managed, submit_read, utcnow

WORKER_ID = "dispatcher-test-1"


def _service_principal(
    session: Session, world: TaskWorld
) -> GovernancePrincipal:
    """Resolve a real service identity; the dispatcher must never act as a person."""
    identity = make_service_identity(session, owner=world.org.admin)
    return resolve_service_principal(session, identity.id)


def _dispatcher(
    session: Session,
    world: TaskWorld,
    *,
    adapter: FakeRuntimeAdapter | None = None,
    principal: GovernancePrincipal | None = None,
) -> RuntimeDispatcher:
    return RuntimeDispatcher(
        FakeRuntimeAdapter() if adapter is None else adapter,
        DispatcherConfig(worker_id=WORKER_ID),
        service_principal=(
            _service_principal(session, world) if principal is None else principal
        ),
        session=session,
    )


def _leases(session: Session, task: Task) -> list[TaskLease]:
    return list(
        session.scalars(
            select(TaskLease)
            .where(TaskLease.task_id == task.task_id)
            .order_by(TaskLease.id)
        )
    )


def _assert_lease_released(session: Session, task: Task) -> None:
    leases = _leases(session, task)
    assert leases, "the dispatcher must claim before it can release"
    assert all(lease.released_at is not None for lease in leases)


def _capabilities(
    *,
    submit_status: CapabilityStatus = CapabilityStatus.VERIFIED,
    status_status: CapabilityStatus = CapabilityStatus.VERIFIED,
) -> RuntimeCapabilities:
    """A minimal snapshot so one capability can be unverified in isolation."""

    def capability(name: RuntimeCapability, status: CapabilityStatus) -> Capability:
        return Capability(
            name=name.value,
            version="1.0.0",
            status=status,
            evidence_ref=None,
        )

    return RuntimeCapabilities(
        adapter_contract_version=ADAPTER_CONTRACT_VERSION,
        boundary=BOUNDARY,
        runtime_id="rt_fake_0001",
        runtime_type=RuntimeType.OTHER,
        runtime_version=None,
        version_source=VersionSource.UNKNOWN,
        capabilities=(
            capability(RuntimeCapability.TASK_SUBMIT, submit_status),
            capability(RuntimeCapability.TASK_STATUS, status_status),
        ),
        observed_at=utcnow(),
    )


# -- idle and preflight -------------------------------------------------------


def test_run_once_returns_idle_without_a_managed_task(
    session: Session, world: TaskWorld
) -> None:
    submit_read(session, world, key="idem_dispatch_idle_advisory")
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result == DispatcherResult(outcome="idle")
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []
    assert session.scalars(select(TaskLease)).all() == []


def test_run_once_reaches_preflight_ok_without_submitting(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "preflight_ok"
    assert result.task_id == task.task_id
    assert result.runtime_task_id is None
    # The managed task is claimable again and no execution fact was written.
    assert task.state == "queued"
    assert task.attempt == 0
    assert task.runtime_task_id is None
    assert adapter.capability_probes == [False]
    assert adapter.health_calls == 1
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- capabilities -------------------------------------------------------------


def test_run_once_escalates_unverified_capability(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(capabilities_verified=False)

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unsupported"
    assert result.task_id == task.task_id
    assert result.detail.startswith("runtime_capability_unverified")
    assert "task.submit" in result.detail
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"
    assert task.result["outcome"] == "unsupported"
    assert adapter.submit_calls == []
    # The capability gap is found before any health probe or submission.
    assert adapter.health_calls == 0
    _assert_lease_released(session, task)


@pytest.mark.parametrize(
    ("submit_status", "status_status"),
    [
        (CapabilityStatus.UNVERIFIED, CapabilityStatus.VERIFIED),
        (CapabilityStatus.VERIFIED, CapabilityStatus.UNVERIFIED),
        (CapabilityStatus.PARTIALLY_VERIFIED, CapabilityStatus.VERIFIED),
    ],
    ids=["submit-unverified", "status-unverified", "submit-partial"],
)
def test_run_once_requires_both_submit_and_status_verified(
    session: Session,
    world: TaskWorld,
    submit_status: CapabilityStatus,
    status_status: CapabilityStatus,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        capabilities=_capabilities(
            submit_status=submit_status, status_status=status_status
        )
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unsupported"
    assert task.state == "needs_human"
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- health -------------------------------------------------------------------


@pytest.mark.parametrize(
    "health",
    [HealthStatus.UNAVAILABLE, HealthStatus.DEGRADED],
    ids=["unavailable", "degraded"],
)
def test_unhealthy_runtime_releases_lease_and_keeps_queued(
    session: Session, world: TaskWorld, health: HealthStatus
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(health=health)

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "adapter_unavailable"
    assert result.task_id == task.task_id
    assert result.detail == health.value
    # The lease is released and the task stays claimable for the next pass.
    assert task.state == "queued"
    assert task.attempt == 0
    # No dispatch outcome was recorded; only the submit-time capability check.
    assert "outcome" not in task.result
    assert task.result["runtime_capabilities_verified"] is True
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- grant rejection ----------------------------------------------------------


def test_revoked_grant_never_reaches_the_adapter(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    revoke_grant(
        session,
        grant_id=task.grant_id,
        reason_code="policy_changed",
        principal=world.actors.executor,
    )
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unavailable"
    assert result.task_id == task.task_id
    assert result.detail == "grant_not_active"
    assert task.state == "failed"
    assert task.result["outcome"] == "unavailable"
    assert task.result["error_code"] == "grant_not_active"
    assert task.grant_status == "revoked"
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


def test_expired_grant_never_reaches_the_adapter(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once(
        now=utcnow() + timedelta(hours=1)
    )

    assert result.outcome == "unavailable"
    assert result.detail == "grant_expired"
    assert task.state == "failed"
    assert task.result["outcome"] == "unavailable"
    assert task.grant_status == "revoked"
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- service principal --------------------------------------------------------


def test_dispatcher_rejects_a_person_as_service_principal(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter()

    with pytest.raises(TaskError) as excinfo:
        _dispatcher(
            session, world, adapter=adapter, principal=world.actors.executor
        ).run_once()

    assert excinfo.value.reason_code == "task_unauthorized_actor"
    assert session.scalars(select(TaskLease)).all() == []
    assert session.scalar(
        select(Task).where(Task.task_id == task.task_id)
    ).state == "queued"
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []


def test_dispatcher_rejects_an_inactive_service_principal(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    disabled = replace(
        _service_principal(session, world), account_status="disabled"
    )

    with pytest.raises(PrincipalResolutionError) as excinfo:
        _dispatcher(
            session, world, principal=disabled
        ).run_once()

    assert excinfo.value.reason_code == "principal_disabled"
    assert session.scalars(select(TaskLease)).all() == []
    assert session.scalar(
        select(Task).where(Task.task_id == task.task_id)
    ).state == "queued"


def test_dispatcher_accepts_an_active_runtime_principal(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    runtime_principal = replace(
        _service_principal(session, world),
        principal_type="runtime",
        service_identity_id=None,
    )

    result = _dispatcher(
        session, world, principal=runtime_principal
    ).run_once()

    assert result.outcome == "preflight_ok"
    assert result.task_id == task.task_id
    _assert_lease_released(session, task)


# -- transaction boundary and loop -------------------------------------------


def test_unexpected_adapter_error_rolls_back_the_claim(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(health=RuntimeError("probe exploded"))

    with pytest.raises(RuntimeError, match="probe exploded"):
        _dispatcher(session, world, adapter=adapter).run_once()

    # The claim, its audit row and any flush are rolled back; nothing is left
    # half-claimed for a recovery pass to trip over.
    assert session.scalars(select(TaskLease)).all() == []
    assert session.scalar(
        select(Task).where(Task.task_id == task.task_id)
    ).state == "queued"


def test_run_forever_returns_immediately_when_the_stop_event_is_set(
    session: Session, world: TaskWorld
) -> None:
    stop_event = threading.Event()
    stop_event.set()
    opened: list[bool] = []

    def session_factory() -> Session:  # pragma: no cover - must not be called
        opened.append(True)
        raise AssertionError("run_forever must not open a session after stop")

    _dispatcher(session, world).run_forever(
        stop_event, session_factory=session_factory
    )

    assert opened == []
