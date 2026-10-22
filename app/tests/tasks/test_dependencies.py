"""Dependency gating: a blocked task cannot be dispatched."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.tasks import worker
from app.tasks.errors import DependencyError, TaskError
from app.tasks.service import (
    add_dependency,
    dependency_blockers,
    start_attempt,
)
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import READ_CAPABILITY, claim, goal, submit_read, succeed


def _two_tasks(session: Session, world: TaskWorld):
    first = submit_read(session, world, key="idem_dep_first_0001")
    second = submit_read(
        session,
        world,
        key="idem_dep_second_0001",
        goal=goal(summary="Read a second synthetic expense"),
    )
    session.commit()
    return first, second


def test_blocking_dependency_prevents_dispatch(
    session: Session, world: TaskWorld
) -> None:
    first, second = _two_tasks(session, world)
    dependency = add_dependency(
        session,
        task_id=second.task_id,
        depends_on_task_id=first.task_id,
        actor=world.actors.requester,
    )
    session.commit()
    assert dependency.type == "blocks"
    assert dependency_blockers(session, second.task_id) == (first.task_id,)
    assert dependency_blockers(session, first.task_id) == ()

    # The first task can be claimed; the blocked one cannot.
    claimed = worker.claim_task(
        session, worker_id="worker-1", actor=world.actors.executor
    )
    assert claimed is not None and claimed.task.task_id == first.task_id
    session.flush()
    blocked_claim = worker.claim_task(
        session, worker_id="worker-2", actor=world.actors.executor
    )
    assert blocked_claim is None
    session.rollback()


def test_start_attempt_rechecks_dependencies(
    session: Session, world: TaskWorld
) -> None:
    first, second = _two_tasks(session, world)
    add_dependency(
        session,
        task_id=second.task_id,
        depends_on_task_id=first.task_id,
        actor=world.actors.requester,
    )
    session.commit()

    # Give the second task a lease directly so the dependency check, not the
    # lease check, is what refuses the dispatch.
    from app.tasks.service import DEFAULT_LEASE_SECONDS
    from app.tasks.models import TaskLease
    from datetime import timedelta

    lease = TaskLease(
        task_id=second.task_id,
        worker_id="worker-dep",
        fencing_token=1,
        acquired_at=second.updated_at,
        lease_expires_at=second.updated_at + timedelta(seconds=DEFAULT_LEASE_SECONDS),
    )
    session.add(lease)
    session.flush()

    with pytest.raises(TaskError) as excinfo:
        start_attempt(
            session,
            task_id=second.task_id,
            actor=world.actors.executor,
            worker_id="worker-dep",
            fencing_token=1,
            execution_ref="exec_dep_0001",
        )
    assert excinfo.value.reason_code == "task_dependency_blocked"
    assert excinfo.value.context["blocked_by"] == [first.task_id]


def test_dependency_is_released_after_success(
    session: Session, world: TaskWorld
) -> None:
    first, second = _two_tasks(session, world)
    add_dependency(
        session,
        task_id=second.task_id,
        depends_on_task_id=first.task_id,
        actor=world.actors.requester,
    )
    session.commit()
    succeed(session, world, first)
    session.commit()

    assert dependency_blockers(session, second.task_id) == ()
    claimed = worker.claim_task(
        session, worker_id="worker-3", actor=world.actors.executor
    )
    assert claimed is not None and claimed.task.task_id == second.task_id


def test_dependency_rejects_self_and_cycles(
    session: Session, world: TaskWorld
) -> None:
    first, second = _two_tasks(session, world)
    with pytest.raises(DependencyError) as excinfo:
        add_dependency(
            session,
            task_id=first.task_id,
            depends_on_task_id=first.task_id,
            actor=world.actors.requester,
        )
    assert excinfo.value.reason_code == "task_dependency_invalid"

    add_dependency(
        session,
        task_id=first.task_id,
        depends_on_task_id=second.task_id,
        actor=world.actors.requester,
    )
    session.flush()
    with pytest.raises(DependencyError) as excinfo:
        add_dependency(
            session,
            task_id=second.task_id,
            depends_on_task_id=first.task_id,
            actor=world.actors.requester,
        )
    assert excinfo.value.reason_code == "task_dependency_cycle"
