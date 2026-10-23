"""Managed Execution dispatcher: claim, preflight and fail-closed mapping.

This module is the production caller that connects the task/ExecutionGrant core
to the frozen :class:`app.runtime.contract.RuntimeAdapter` protocol.  It only
ever claims ``execution_mode="managed_execution"`` work; advisory and governed
tasks stay client/PEP driven.

Scope of the current implementation
-----------------------------------
Only the **preflight** half of the loop is implemented here:

1. validate the injected service principal;
2. claim one managed task with the existing lease/fencing machinery;
3. load the task-bound :class:`~app.governance.models.ExecutionGrant` and
   project it with :func:`app.runtime.projection.project_runtime_grant`;
4. require ``task.submit`` **and** ``task.status`` to be ``verified``;
5. probe ``adapter.health()``;
6. record a domain rejection through the existing
   :func:`app.tasks.service.record_dispatch_failure` path, or stop at a clean
   ``preflight_ok`` result.

Submission, status/result mapping, cancellation and lease recovery are added by
later tasks.  ``run_once`` therefore never calls ``adapter.submit_task`` today,
and it never calls ``task_service.complete_task``: a runtime self-report can
never become ``succeeded``.

Transaction boundary
--------------------
``run_once`` uses the **injected** session.  It commits successful and
domain-rejected work and rolls back unexpected exceptions; the caller still owns
``close``.  :meth:`RuntimeDispatcher.run_forever` creates a fresh session from
the supplied factory for every iteration.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.models import ExecutionGrant
from app.governance.principal import GovernancePrincipal
from app.runtime.contract import (
    CapabilityStatus,
    HealthStatus,
    RuntimeAdapter,
    RuntimeCapability,
)
from app.runtime.projection import (
    MANAGED_EXECUTION_MODE,
    GrantProjectionError,
    project_runtime_grant,
)
from app.tasks import service as task_service
from app.tasks import worker
from app.tasks.errors import TaskError
from app.tasks.models import Task, TaskLease

__all__ = [
    "REQUIRED_CAPABILITIES",
    "RUNTIME_CAPABILITY_UNVERIFIED",
    "DispatcherConfig",
    "DispatcherResult",
    "RuntimeDispatcher",
]

#: Capabilities the adapter must report as ``verified`` before a managed task
#: may be handed to it.  A partially verified capability is not enough.
REQUIRED_CAPABILITIES: tuple[RuntimeCapability, ...] = (
    RuntimeCapability.TASK_SUBMIT,
    RuntimeCapability.TASK_STATUS,
)

#: Stable reason recorded when the capability preflight fails.  It is a fixed
#: code (the task stores it as ``error_code``); :attr:`DispatcherResult.detail`
#: names the missing capabilities for the caller's logs.
RUNTIME_CAPABILITY_UNVERIFIED = "runtime_capability_unverified"

#: Principal types allowed to write Managed Execution facts.
_SERVICE_PRINCIPAL_TYPES = frozenset({"service", "runtime"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DispatcherConfig:
    """One dispatcher's identity and pacing.

    ``worker_id`` is the lease owner.  One dispatcher handles one task at a
    time; parallel workers are only safe on *different* tasks.
    """

    worker_id: str
    lease_seconds: int = 300
    poll_interval: float = 0.5
    health_backoff_seconds: float = 5.0


@dataclass(frozen=True)
class DispatcherResult:
    """Stable, non-sensitive outcome of one dispatch pass.

    Outcomes produced so far: ``idle``, ``preflight_ok``, ``unavailable``
    (invalid/revoked/expired Grant), ``unsupported`` (capability gap) and
    ``adapter_unavailable`` (health/degraded).  Later tasks add the
    submission/execution outcomes.

    ``detail`` is a short machine-readable code, never a payload, credential or
    runtime URL.
    """

    outcome: str
    task_id: str | None = None
    runtime_task_id: str | None = None
    detail: str | None = None


class RuntimeDispatcher:
    """Claim and preflight managed tasks for one :class:`RuntimeAdapter`."""

    def __init__(
        self,
        adapter: RuntimeAdapter,
        config: DispatcherConfig,
        *,
        service_principal: GovernancePrincipal,
        session: Session,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._service_principal = service_principal
        self._session = session

    # -- public API ----------------------------------------------------------

    def run_once(self, *, now: datetime | None = None) -> DispatcherResult:
        """Run one claim/preflight pass and commit its domain outcome.

        Unexpected exceptions (including an adapter that raises instead of
        returning a health/status fact) roll the whole pass back and propagate;
        a half-claimed lease is never left behind.
        """
        timestamp = now or _utcnow()
        try:
            self._require_service_principal()
            result = self._dispatch_one(timestamp)
        except Exception:
            self._session.rollback()
            raise
        self._session.commit()
        return result

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        session_factory: Callable[[], Session],
    ) -> None:
        """Poll :meth:`run_once` until ``stop_event`` is set.

        Minimal loop for now: one fresh session per iteration, a health backoff
        when the adapter is unavailable, and no parallel task execution.
        Lease recovery, error backoff and signal handling are completed with the
        cancellation/recovery task.
        """
        while not stop_event.is_set():
            with session_factory() as session:
                dispatcher = RuntimeDispatcher(
                    self._adapter,
                    self._config,
                    service_principal=self._service_principal,
                    session=session,
                )
                result = dispatcher.run_once()
            stop_event.wait(self._delay_for(result))

    # -- preflight -----------------------------------------------------------

    def _dispatch_one(self, timestamp: datetime) -> DispatcherResult:
        claimed = worker.claim_task(
            self._session,
            worker_id=self._config.worker_id,
            actor=self._service_principal,
            execution_mode=MANAGED_EXECUTION_MODE,
            lease_seconds=self._config.lease_seconds,
            now=timestamp,
        )
        if claimed is None:
            return DispatcherResult(outcome="idle")
        task = claimed.task
        lease = claimed.lease

        # The authoritative Grant is loaded from persisted state only; a client
        # payload never supplies or widens it.
        grant = self._load_grant(task)
        try:
            project_runtime_grant(task, grant, now=timestamp)
        except GrantProjectionError as exc:
            # No runtime call is made: the Grant does not prove a usable
            # managed authority for this task.
            self._record_failure(
                task,
                lease,
                outcome="unavailable",
                reason=exc.reason_code,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="unavailable",
                task_id=task.task_id,
                detail=exc.reason_code,
            )

        missing = self._unverified_capabilities()
        if missing:
            self._record_failure(
                task,
                lease,
                outcome="unsupported",
                reason=RUNTIME_CAPABILITY_UNVERIFIED,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="unsupported",
                task_id=task.task_id,
                detail=f"{RUNTIME_CAPABILITY_UNVERIFIED}:{','.join(missing)}",
            )

        health = self._adapter.health()
        if health.status is not HealthStatus.HEALTHY:
            # The lease is released and the task stays queued: nothing was
            # submitted, so the next pass (after a backoff) may retry it.
            self._release(task, lease, now=timestamp)
            return DispatcherResult(
                outcome="adapter_unavailable",
                task_id=task.task_id,
                detail=health.status.value,
            )

        # Submission and binding arrive with the next task.  Releasing the
        # lease here keeps the task claimable instead of stranding it until the
        # lease expires.
        self._release(task, lease, now=timestamp)
        return DispatcherResult(outcome="preflight_ok", task_id=task.task_id)

    # -- helpers -------------------------------------------------------------

    def _require_service_principal(self) -> None:
        """Fail closed before claiming unless the actor is a trusted service.

        A person (or agent instance) never dispatches Managed Execution: the
        lease/fencing producer for a runtime boundary is a service/runtime
        principal.
        """
        principal = self._service_principal
        if principal.principal_type not in _SERVICE_PRINCIPAL_TYPES:
            raise TaskError(
                "task_unauthorized_actor",
                "the dispatcher must run as a trusted service/runtime principal",
                context={
                    "principal_id": principal.principal_id,
                    "principal_type": principal.principal_type,
                },
            )
        principal.require_active()

    def _load_grant(self, task: Task) -> ExecutionGrant | None:
        if not task.grant_id:
            return None
        return self._session.scalar(
            select(ExecutionGrant).where(
                ExecutionGrant.grant_id == task.grant_id
            )
        )

    def _unverified_capabilities(self) -> tuple[str, ...]:
        snapshot = self._adapter.capabilities()
        missing: list[str] = []
        for name in REQUIRED_CAPABILITIES:
            capability = snapshot.capability(name)
            if (
                capability is None
                or capability.status is not CapabilityStatus.VERIFIED
            ):
                missing.append(name.value)
        return tuple(missing)

    def _record_failure(
        self,
        task: Task,
        lease: TaskLease,
        *,
        outcome: str,
        reason: str,
        now: datetime,
    ) -> None:
        """Record a pre-submission rejection and release the claim."""
        task_service.record_dispatch_failure(
            self._session,
            task_id=task.task_id,
            actor=self._service_principal,
            outcome=outcome,
            worker_id=self._config.worker_id,
            fencing_token=lease.fencing_token,
            reason=reason,
            now=now,
        )
        self._release(task, lease, now=now)

    def _release(self, task: Task, lease: TaskLease, *, now: datetime) -> None:
        worker.release_lease(
            self._session,
            task_id=task.task_id,
            worker_id=self._config.worker_id,
            fencing_token=lease.fencing_token,
            now=now,
        )

    def _delay_for(self, result: DispatcherResult) -> float:
        if result.outcome == "adapter_unavailable":
            return self._config.health_backoff_seconds
        return self._config.poll_interval
