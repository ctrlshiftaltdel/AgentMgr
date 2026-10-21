"""Worker-side lease claiming, fencing and crash recovery.

The worker never writes a state directly.  It claims a lease with
``FOR UPDATE SKIP LOCKED``, and every execution transition is a service call
that re-checks the *current* fencing token.  Two workers therefore cannot write
the same attempt: the loser sees ``task_fencing_token_stale``.

Lease expiry is fail-closed.  A queued task simply becomes claimable again; a
dispatched/running task is only rolled back (T33) when the execution boundary
explicitly proves it never accepted the submission and produced no side effect.
Without that proof the task enters ``unknown`` (grant revoked) and is handed to
reconciliation -- it is never blindly replayed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.governance.principal import GovernancePrincipal, audit_actor_id
from app.tasks import priority as scheduling
from app.tasks import service
from app.tasks.errors import TaskError
from app.tasks.models import Task, TaskAttempt, TaskLease
from app.tasks.service import DEFAULT_LEASE_SECONDS
from app.workflow.service import enqueue_event

__all__ = [
    "LeaseClaim",
    "LeaseRecovery",
    "RecoveryChecker",
    "claim_task",
    "release_lease",
    "recover_expired_leases",
    "renew_lease",
    "retry_or_escalate",
]

#: How many queued rows a claim pass inspects before ordering by priority.
CLAIM_SCAN_LIMIT = 50

BoundaryOutcome = Literal[
    "not_accepted", "running", "completed", "failed", "unknown"
]


class RecoveryChecker(Protocol):
    """Ask the execution boundary what happened to a lost submission."""

    def __call__(
        self, *, task: Task, attempt_number: int, idempotency_key: str | None
    ) -> tuple[BoundaryOutcome, bool]:
        """Return ``(outcome, stop_confirmed)``.

        ``not_accepted`` must only be returned together with a guarantee of no
        side effects; the worker treats any uncertainty as ``unknown``.
        """


@dataclass(frozen=True)
class LeaseClaim:
    task: Task
    lease: TaskLease


@dataclass(frozen=True)
class LeaseRecovery:
    task_id: str
    previous_lease_id: int
    action: str
    state: str
    detail: str = ""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _schedulable(task: Task) -> scheduling.SchedulableTask:
    response_by = None
    raw_response = (task.sla or {}).get("response_by")
    if isinstance(raw_response, str):
        try:
            response_by = datetime.fromisoformat(
                raw_response.replace("Z", "+00:00")
            )
        except ValueError:
            response_by = None
    owner = task.owner_principal_id or task.assignee_principal_id
    return scheduling.SchedulableTask(
        task_id=task.task_id,
        priority=task.priority,
        created_at=_to_utc(task.created_at),
        due_at=_to_utc(task.due_at) if task.due_at is not None else None,
        owner_principal_id=owner,
        state=task.state,
        response_by=_to_utc(response_by) if response_by is not None else None,
    )


def _audit(
    session: Session,
    *,
    actor: GovernancePrincipal | None,
    action: str,
    task: Task,
    details: dict[str, Any],
) -> None:
    record_event(
        session,
        actor_id=audit_actor_id(actor) if actor is not None else None,
        action=action,
        resource_type="task",
        resource_id=task.task_id,
        details=details,
    )


def claim_task(
    session: Session,
    *,
    worker_id: str,
    actor: GovernancePrincipal | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> LeaseClaim | None:
    """Claim the highest-priority admissible task, or return ``None``.

    Ordering is deterministic (bounded ageing, then due date, creation time and
    id).  Tasks with an unsatisfied ``blocks`` dependency are skipped, as are
    tasks whose owner is no longer active.
    """
    timestamp = now or _utcnow()
    if not worker_id:
        raise TaskError(
            "required_input_missing",
            "worker_id is required",
            context={"field": "worker_id"},
        )
    candidates = list(
        session.scalars(
            select(Task)
            .where(Task.state == "queued")
            .order_by(Task.created_at, Task.id)
            .limit(CLAIM_SCAN_LIMIT)
            .with_for_update(skip_locked=True)
        )
    )
    if not candidates:
        return None
    leased = set(
        session.scalars(
            select(TaskLease.task_id).where(
                TaskLease.task_id.in_([task.task_id for task in candidates]),
                TaskLease.released_at.is_(None),
                TaskLease.lease_expires_at > timestamp,
            )
        )
    )
    admissible = [
        task
        for task in candidates
        if task.task_id not in leased
        and task.owner_account_status == "active"
        and not service.dependency_blockers(session, task.task_id)
    ]
    if not admissible:
        return None
    ordered = scheduling.order_queue(
        [_schedulable(task) for task in admissible], now=timestamp
    )
    chosen_id = ordered[0].task_id
    task = next(item for item in admissible if item.task_id == chosen_id)

    current = session.scalar(
        select(TaskLease)
        .where(TaskLease.task_id == task.task_id, TaskLease.released_at.is_(None))
        .with_for_update()
    )
    if current is not None:
        if _to_utc(current.lease_expires_at) > timestamp:
            return None
        current.released_at = timestamp
        session.flush()

    highest = session.scalar(
        select(func.max(TaskLease.fencing_token)).where(
            TaskLease.task_id == task.task_id
        )
    )
    lease = TaskLease(
        task_id=task.task_id,
        worker_id=worker_id,
        fencing_token=int(highest or 0) + 1,
        acquired_at=timestamp,
        lease_expires_at=timestamp + timedelta(seconds=max(1, lease_seconds)),
    )
    session.add(lease)
    session.flush()
    _audit(
        session,
        actor=actor,
        action="task.worker.leased",
        task=task,
        details={
            "worker_id": worker_id,
            "fencing_token": lease.fencing_token,
            "lease_expires_at": lease.lease_expires_at.isoformat(),
        },
    )
    enqueue_event(
        session,
        event_type="worker.leased",
        aggregate_type="task",
        aggregate_id=task.task_id,
        idempotency_key=f"task:worker.leased:{task.task_id}:{lease.fencing_token}",
        payload={
            "task_id": task.task_id,
            "fencing_token": lease.fencing_token,
            "worker_id": worker_id,
        },
    )
    return LeaseClaim(task=task, lease=lease)


def renew_lease(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    fencing_token: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> TaskLease:
    """Extend the current lease; a stale token can never renew."""
    timestamp = now or _utcnow()
    lease = session.scalar(
        select(TaskLease)
        .where(
            TaskLease.task_id == task_id,
            TaskLease.released_at.is_(None),
        )
        .with_for_update()
    )
    if lease is None or lease.worker_id != worker_id:
        raise TaskError(
            "task_lease_not_held",
            "no active lease is held by this worker",
            context={"task_id": task_id, "worker_id": worker_id},
        )
    if lease.fencing_token != fencing_token:
        raise TaskError(
            "task_fencing_token_stale",
            "the fencing token is not the current lease",
            context={"task_id": task_id, "current_token": lease.fencing_token},
        )
    if _to_utc(lease.lease_expires_at) <= timestamp:
        raise TaskError(
            "task_lease_expired",
            "an expired lease cannot be renewed; recover it first",
            context={"task_id": task_id},
        )
    lease.lease_expires_at = timestamp + timedelta(seconds=max(1, lease_seconds))
    session.flush()
    return lease


def release_lease(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    fencing_token: int,
    now: datetime | None = None,
) -> None:
    """Release a lease the worker still owns (idempotent)."""
    timestamp = now or _utcnow()
    lease = session.scalar(
        select(TaskLease)
        .where(TaskLease.task_id == task_id, TaskLease.released_at.is_(None))
        .with_for_update()
    )
    if lease is None:
        return
    if lease.fencing_token != fencing_token or lease.worker_id != worker_id:
        raise TaskError(
            "task_fencing_token_stale",
            "only the current lease holder may release the lease",
            context={"task_id": task_id},
        )
    lease.released_at = timestamp
    session.flush()


def recover_expired_leases(
    session: Session,
    *,
    actor: GovernancePrincipal | None = None,
    checker: RecoveryChecker | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> list[LeaseRecovery]:
    """Recover every expired lease, fail-closed.

    * ``queued``: no execution fact exists; release the lease only.
    * ``dispatched``: with a checker proving ``not_accepted`` **and** no side
      effects, roll the attempt back (T33) and leave the grant active.
      Otherwise enter ``unknown`` and revoke the grant.
    * ``running``/``verifying``: never replayed; enter ``unknown``.
    """
    timestamp = now or _utcnow()
    leases = list(
        session.scalars(
            select(TaskLease)
            .where(
                TaskLease.released_at.is_(None),
                TaskLease.lease_expires_at <= timestamp,
            )
            .order_by(TaskLease.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    results: list[LeaseRecovery] = []
    for lease in leases:
        task = session.scalar(
            select(Task).where(Task.task_id == lease.task_id).with_for_update()
        )
        if task is None:
            lease.released_at = timestamp
            session.flush()
            continue
        lease.released_at = timestamp
        session.flush()
        attempt = session.scalar(
            select(TaskAttempt)
            .where(TaskAttempt.task_id == task.task_id)
            .order_by(TaskAttempt.attempt_number.desc())
            .limit(1)
        )

        if task.state == "queued":
            _audit(
                session,
                actor=actor,
                action="task.worker.lease_expired",
                task=task,
                details={"lease_id": lease.id, "action": "released"},
            )
            results.append(
                LeaseRecovery(
                    task_id=task.task_id,
                    previous_lease_id=lease.id,
                    action="released",
                    state=task.state,
                )
            )
            continue

        outcome: BoundaryOutcome = "unknown"
        stop_confirmed = False
        if checker is not None and task.state == "dispatched":
            try:
                outcome, stop_confirmed = checker(
                    task=task,
                    attempt_number=attempt.attempt_number if attempt else 0,
                    idempotency_key=attempt.idempotency_key if attempt else None,
                )
            except Exception:  # noqa: BLE001 - an unavailable checker is "unknown"
                outcome, stop_confirmed = "unknown", False

        if task.state == "dispatched" and outcome == "not_accepted":
            # T33: void a submission that never started.  No retry budget is
            # consumed and the original grant remains valid.
            task.result = {
                **dict(task.result or {}),
                "no_side_effects": True,
                "side_effect_evidence": ["evidence:worker_lease_expired_no_accept"],
            }
            new_attempt = max(0, task.attempt - 1)
            if new_attempt > 0 and not task.stop_confirmed:
                outcome = "unknown"
            else:
                task.attempt = new_attempt
                if attempt is not None:
                    attempt.state = "abandoned"
                    attempt.ended_at = timestamp
                    attempt.reason = "lease_expired_not_accepted"
                    attempt.evidence_refs = list(
                        [*attempt.evidence_refs, "evidence:worker_lease_expired_no_accept"]
                    )
                with session.no_autoflush:
                    ctx = service._base_context(  # noqa: SLF001 - same domain
                        session, task, actor, now=timestamp
                    )
                ctx = replace(
                    ctx,
                    lease_expired_evidence=True,
                    no_side_effects=True,
                    stop_confirmed=task.stop_confirmed,
                )
                service._perform_transition(  # noqa: SLF001
                    session,
                    task,
                    actor,
                    event="worker.lease_expired",
                    target="queued",
                    ctx=ctx,
                    extra={"lease_id": lease.id},
                    now=timestamp,
                )
                _audit(
                    session,
                    actor=actor,
                    action="task.worker.lease_expired",
                    task=task,
                    details={"lease_id": lease.id, "action": "attempt_rolled_back"},
                )
                results.append(
                    LeaseRecovery(
                        task_id=task.task_id,
                        previous_lease_id=lease.id,
                        action="attempt_rolled_back",
                        state=task.state,
                    )
                )
                continue

        if task.state == "dispatched" and outcome == "failed" and stop_confirmed:
            service.fail_task(
                session,
                task_id=task.task_id,
                actor=actor or _service_principal(),
                reason="lease_expired_failed",
                evidence_refs=["evidence:worker_lease_expired_failed"],
                stop_confirmed=True,
                error_code="execution_failed",
                now=timestamp,
            )
            results.append(
                LeaseRecovery(
                    task_id=task.task_id,
                    previous_lease_id=lease.id,
                    action="failed",
                    state=task.state,
                )
            )
            continue

        if task.state in {"dispatched", "running"}:
            service.record_attempt_result(
                session,
                task_id=task.task_id,
                actor=actor or _service_principal(),
                outcome="lost",
                evidence_refs=["evidence:worker_lease_expired_lost"],
                now=timestamp,
            )
            _audit(
                session,
                actor=actor,
                action="task.worker.lease_expired",
                task=task,
                details={"lease_id": lease.id, "action": "entered_unknown"},
            )
            results.append(
                LeaseRecovery(
                    task_id=task.task_id,
                    previous_lease_id=lease.id,
                    action="entered_unknown",
                    state=task.state,
                )
            )
            continue

        _audit(
            session,
            actor=actor,
            action="task.worker.lease_expired",
            task=task,
            details={
                "lease_id": lease.id,
                "action": "released",
                "state": task.state,
            },
        )
        results.append(
            LeaseRecovery(
                task_id=task.task_id,
                previous_lease_id=lease.id,
                action="released",
                state=task.state,
            )
        )
    return results


def _service_principal() -> GovernancePrincipal:
    """A trusted system principal for recovery writes (no human attribution)."""
    return GovernancePrincipal(
        principal_id=f"svc_{uuid.uuid4().hex[:12]}",
        principal_type="service",
        employee_id=None,
        service_identity_id=None,
        company_id=None,
        role_ids=(),
        account_status="active",
        identity_assurance="verified_signed_token",
    )


def retry_or_escalate(
    session: Session,
    *,
    task_id: str,
    actor: GovernancePrincipal,
    capability: str | None = None,
    now: datetime | None = None,
) -> Task:
    """Retry a failed task if safe, otherwise escalate (no blind replay)."""
    return service.retry_task(
        session,
        task_id=task_id,
        actor=actor,
        capability=capability,
        now=now,
    )
