"""Migration 0015 and its database-level invariants."""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.tasks.models import Task, TaskEvent
from app.tests.tasks.conftest import TaskWorld

MIGRATION = "migrations.versions.0015_task_lifecycle"


def test_revision_chain_is_linear() -> None:
    module = importlib.import_module(MIGRATION)
    assert module.revision == "0015_task_lifecycle"
    assert module.down_revision == "0014_gov_grant_constraints"


def test_task_tables_and_indexes_exist(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {
        "tasks",
        "task_events",
        "task_attempts",
        "task_dependencies",
        "task_leases",
        "task_confirmations",
    } <= tables
    indexes = {
        index["name"] for index in inspect(engine).get_indexes("task_leases")
    }
    assert "uq_task_leases_active_task" in indexes
    # No second approval or outbox model was introduced.
    assert "task_approvals" not in tables
    assert "task_outbox" not in tables


def _minimal_task(world: TaskWorld, *, task_id: str, key: str) -> Task:
    return Task(
        task_id=task_id,
        idempotency_key=key,
        request_hash="sha256:" + "a" * 64,
        correlation_id="corr_migration_0001",
        requester_principal_id=world.actors.requester.principal_id,
        requester_employee_id=world.actors.requester.employee_id,
        owner_principal_id=world.actors.owner.principal_id,
        owner_employee_id=world.actors.owner.employee_id,
        owner_account_status="active",
        assignee_principal_id=world.actors.executor.principal_id,
        executor_principal_id=world.actors.executor.principal_id,
        executor_employee_id=world.actors.executor.employee_id,
        summary="migration fixture",
        goal={"summary": "migration fixture", "completion_criteria": [
            {
                "criterion_id": "crit_mig_0001",
                "type": "business_state_equals",
                "verifier": {"verifier_id": "ver_mig_0001", "version": "1.0.0"},
                "resource_ref": "res_expense_0001",
                "expected": "submitted",
                "evidence_ref": "evidence:mig_0001",
                "evidence_hash": None,
            }
        ]},
        execution_mode="advisory",
        resource_scope={"scope_type": "self", "resource_ids": ["res_expense_0001"]},
        requested_capabilities=["expense.read@1.0.0"],
        payload={},
        payload_hash="sha256:" + "b" * 64,
        record_completeness="complete",
    )


def test_task_events_are_unique_per_revision(
    session: Session, world: TaskWorld
) -> None:
    task = _minimal_task(
        world, task_id="task_mig_events_0001", key="idem_mig_events_0001"
    )
    session.add(task)
    session.flush()
    for event_id in ("evt_mig_0001", "evt_mig_0002"):
        session.add(
            TaskEvent(
                task_id=task.task_id,
                event_id=event_id,
                event_type="task.created",
                sequence=1,
                from_state=None,
                to_state="draft",
                actor_principal_id=world.actors.requester.principal_id,
                payload={},
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_append_only_trigger_rejects_update_and_delete(
    session: Session, world: TaskWorld
) -> None:
    task = _minimal_task(
        world, task_id="task_mig_trigger_0001", key="idem_mig_trigger_0001"
    )
    session.add(task)
    session.flush()
    session.add(
        TaskEvent(
            task_id=task.task_id,
            event_id="evt_mig_trigger_0001",
            event_type="task.created",
            sequence=1,
            from_state=None,
            to_state="draft",
            actor_principal_id=world.actors.requester.principal_id,
            payload={},
        )
    )
    session.commit()

    engine = session.get_bind()
    with engine.connect() as connection:
        trans = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "UPDATE task_events SET event_type = 'task.evaluated' "
                    "WHERE event_id = 'evt_mig_trigger_0001'"
                )
            )
        trans.rollback()
    with engine.connect() as connection:
        trans = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "DELETE FROM task_events "
                    "WHERE event_id = 'evt_mig_trigger_0001'"
                )
            )
        trans.rollback()


def test_database_backstops_refuse_impossible_rows(
    session: Session, world: TaskWorld
) -> None:
    cases = []

    queued_with_attempt = _minimal_task(
        world, task_id="task_mig_bad_0001", key="idem_mig_bad_0001"
    )
    queued_with_attempt.state = "queued"
    queued_with_attempt.attempt = 1
    queued_with_attempt.stop_confirmed = False
    cases.append(queued_with_attempt)

    # Active state without a resolved owner.
    no_owner = _minimal_task(
        world, task_id="task_mig_bad_0002", key="idem_mig_bad_0002"
    )
    no_owner.state = "queued"
    no_owner.owner_principal_id = None
    no_owner.owner_employee_id = None
    no_owner.owner_account_status = "unknown"
    cases.append(no_owner)

    for task in cases:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(task)
                session.flush()
