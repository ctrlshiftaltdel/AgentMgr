"""Worker leases: ordering, fencing, expiry and fail-closed recovery."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.tasks import worker
from app.tasks.errors import TaskError
from app.tasks.models import TaskAttempt, TaskEvent
from app.tasks.service import record_dispatch_failure, start_attempt
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    goal,
    submit_managed,
    submit_read,
    utcnow,
)


def test_claim_orders_by_bounded_priority(session: Session, world: TaskWorld) -> None:
    submit_read(session, world, key="idem_worker_low_0001", priority="low")
    submit_read(
        session,
        world,
        key="idem_worker_urgent_0001",
        priority="urgent",
        goal=goal(summary="Read a different synthetic expense"),
        priority_authorized_by=world.actors.owner.principal_id,
        priority_rationale="synthetic deadline",
    )
    session.commit()

    claimed = worker.claim_task(
        session, worker_id="worker-order", actor=world.actors.executor
    )
    assert claimed is not None
    assert claimed.task.priority == "urgent"
    assert claimed.lease.fencing_token == 1


def test_claim_skips_a_task_that_already_holds_a_lease(
    session: Session, world: TaskWorld
) -> None:
    first = submit_read(session, world, key="idem_worker_first_0001")
    submit_read(
        session,
        world,
        key="idem_worker_second_0001",
        goal=goal(summary="Read another synthetic expense"),
    )
    session.commit()

    one = worker.claim_task(
        session, worker_id="worker-1", actor=world.actors.executor
    )
    two = worker.claim_task(
        session, worker_id="worker-2", actor=world.actors.executor
    )
    assert one is not None and two is not None
    assert one.task.task_id != two.task.task_id
    assert one.task.task_id == first.task_id


def test_claim_filter_only_returns_managed_tasks(
    session: Session, world: TaskWorld
) -> None:
    submit_read(session, world, key="idem_advisory_filter_0001")
    managed = submit_managed(session, world, key="idem_managed_filter_0001")
    session.commit()

    claimed = worker.claim_task(
        session,
        worker_id="worker-managed",
        actor=world.actors.executor,
        execution_mode="managed_execution",
    )
    assert claimed is not None
    assert claimed.task.task_id == managed.task_id


def test_claim_filter_none_keeps_existing_behavior(
    session: Session, world: TaskWorld
) -> None:
    advisory = submit_read(session, world, key="idem_advisory_none_0001")
    session.commit()

    claimed = worker.claim_task(
        session, worker_id="worker-any", actor=world.actors.executor
    )
    assert claimed is not None
    assert claimed.task.task_id == advisory.task_id


def test_lease_is_renewed_and_released_only_by_its_holder(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    lease = worker.claim_task(
        session, worker_id="worker-1", actor=world.actors.executor
    ).lease
    session.flush()

    renewed = worker.renew_lease(
        session,
        task_id=task.task_id,
        worker_id="worker-1",
        fencing_token=lease.fencing_token,
        lease_seconds=600,
    )
    assert renewed.lease_expires_at > lease.acquired_at

    with pytest.raises(TaskError) as excinfo:
        worker.renew_lease(
            session,
            task_id=task.task_id,
            worker_id="worker-2",
            fencing_token=lease.fencing_token,
        )
    assert excinfo.value.reason_code == "task_lease_not_held"

    with pytest.raises(TaskError) as excinfo:
        worker.release_lease(
            session,
            task_id=task.task_id,
            worker_id="worker-1",
            fencing_token=lease.fencing_token + 5,
        )
    assert excinfo.value.reason_code == "task_fencing_token_stale"

    worker.release_lease(
        session,
        task_id=task.task_id,
        worker_id="worker-1",
        fencing_token=lease.fencing_token,
    )
    assert lease.released_at is not None


def test_expired_queued_lease_is_only_released(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    lease = worker.claim_task(
        session, worker_id="worker-1", actor=world.actors.executor
    ).lease
    session.flush()
    events_before = len(
        list(
            session.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task.task_id)
            )
        )
    )

    recoveries = worker.recover_expired_leases(
        session, actor=world.actors.executor, now=utcnow() + timedelta(hours=1)
    )
    session.commit()

    assert [item.action for item in recoveries] == ["released"]
    assert task.state == "queued"
    assert len(
        list(
            session.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task.task_id)
            )
        )
    ) == events_before
    assert lease.released_at is not None

    again = worker.claim_task(
        session, worker_id="worker-2", actor=world.actors.executor
    )
    assert again is not None
    assert again.lease.fencing_token == lease.fencing_token + 1


def test_unavailable_recovery_checker_enters_unknown(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    lease = worker.claim_task(
        session, worker_id="worker-1", actor=world.actors.executor
    ).lease
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-1",
        fencing_token=lease.fencing_token,
        execution_ref="exec_checker_0001",
    )
    session.commit()

    def broken_checker(**kwargs):
        raise RuntimeError("boundary unavailable")

    recoveries = worker.recover_expired_leases(
        session,
        actor=world.actors.executor,
        checker=broken_checker,
        now=utcnow() + timedelta(hours=1),
    )
    session.commit()

    assert [item.action for item in recoveries] == ["entered_unknown"]
    session.refresh(task)
    assert task.state == "unknown"
    assert task.grant_status == "revoked"


def test_rollback_then_redispatch_reuses_the_attempt_number(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    lease = worker.claim_task(
        session, worker_id="worker-1", actor=world.actors.executor
    ).lease
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-1",
        fencing_token=lease.fencing_token,
        execution_ref="exec_reuse_0001",
    )
    session.commit()

    def checker(**kwargs):
        return ("not_accepted", True)

    worker.recover_expired_leases(
        session,
        actor=world.actors.executor,
        checker=checker,
        now=utcnow() + timedelta(hours=1),
    )
    session.commit()
    assert task.attempt == 0
    abandoned = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert abandoned.state == "abandoned"

    fresh = worker.claim_task(
        session, worker_id="worker-2", actor=world.actors.executor
    )
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id="worker-2",
        fencing_token=fresh.lease.fencing_token,
        execution_ref="exec_reuse_0002",
    )
    session.commit()

    session.refresh(abandoned)
    assert task.attempt == 1
    assert abandoned.attempt_number == 1
    assert abandoned.state == "dispatched"
    assert abandoned.execution_ref == "exec_reuse_0002"
    attempts = list(
        session.scalars(
            select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
        )
    )
    assert len(attempts) == 1
