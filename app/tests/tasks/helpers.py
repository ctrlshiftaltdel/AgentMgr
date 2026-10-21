"""Small builders so each test states only what it is about."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.tasks import worker
from app.tasks.models import Task, TaskConfirmation
from app.tasks.service import (
    complete_task,
    mark_running,
    record_attempt_result,
    start_attempt,
    submit_task,
)
from app.tasks.verification import EvidenceRecord
from app.tests.governance.helpers import scope
from app.tests.tasks.conftest import TaskWorld

READ_CAPABILITY = "expense.read@1.0.0"
GATED_CAPABILITY = "expense.submit@1.0.0"

VERIFIER_ID = "ver_state_0001"
VERIFIER_VERSION = "1.0.0"
CRITERION_ID = "crit_state_0001"
EVIDENCE_REF = "evidence:expense_state_0001"


def goal(
    *,
    summary: str = "Read one synthetic expense",
    required_inputs: list[str] | None = None,
    criterion_type: str = "business_state_equals",
    expected: str = "submitted",
    evidence_hash: str | None = None,
    resource_ref: str = "res_expense_0001",
) -> dict[str, Any]:
    return {
        "summary": summary,
        "completion_criteria": [
            {
                "criterion_id": CRITERION_ID,
                "type": criterion_type,
                "verifier": {
                    "verifier_id": VERIFIER_ID,
                    "version": VERIFIER_VERSION,
                },
                "resource_ref": resource_ref,
                "expected": expected,
                "evidence_ref": EVIDENCE_REF,
                "evidence_hash": evidence_hash,
            }
        ],
        "required_inputs": list(required_inputs or ()),
        "out_of_scope": ["No payment execution."],
    }


def evidence(
    *,
    observed: str = "submitted",
    source: str = "business_state",
    criterion_id: str = CRITERION_ID,
    verifier_id: str = VERIFIER_ID,
    verifier_version: str = VERIFIER_VERSION,
    evidence_ref: str = EVIDENCE_REF,
    evidence_hash: str | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        criterion_id=criterion_id,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        evidence_ref=evidence_ref,
        observed=observed,
        evidence_hash=evidence_hash,
        source=source,
    )


def submit_read(
    session: Session,
    world: TaskWorld,
    *,
    key: str = "idem_task_read_0001",
    priority: str = "normal",
    **overrides: Any,
) -> Task:
    params: dict[str, Any] = {
        "requester": world.actors.requester,
        "owner": world.actors.owner,
        "executor": world.actors.executor,
        "idempotency_key": key,
        "goal": goal(),
        "resource_scope": scope(),
        "requested_capabilities": [READ_CAPABILITY],
        "policy_set_id": world.policy.policy_set_id,
        "execution_mode": "advisory",
        "priority": priority,
    }
    params.update(overrides)
    return submit_task(session, **params)


def submit_gated(
    session: Session,
    world: TaskWorld,
    *,
    key: str = "idem_task_gated_0001",
    **overrides: Any,
) -> Task:
    params: dict[str, Any] = {
        "requester": world.actors.requester,
        "owner": world.actors.owner,
        "executor": world.actors.executor,
        "approver": world.actors.approver,
        "idempotency_key": key,
        "goal": goal(summary="Submit one synthetic expense"),
        "resource_scope": scope(),
        "requested_capabilities": [GATED_CAPABILITY],
        "policy_set_id": world.policy.policy_set_id,
        "execution_mode": "advisory",
    }
    params.update(overrides)
    return submit_task(session, **params)


def pending_confirmation(session: Session, task: Task) -> TaskConfirmation:
    row = session.scalar(
        select(TaskConfirmation)
        .where(
            TaskConfirmation.task_id == task.task_id,
            TaskConfirmation.status == "pending",
        )
        .order_by(TaskConfirmation.id)
    )
    assert row is not None, "expected a pending confirmation"
    return row


def claim(session: Session, world: TaskWorld, *, worker_id: str = "worker-1"):
    claim_result = worker.claim_task(
        session, worker_id=worker_id, actor=world.actors.executor
    )
    assert claim_result is not None
    return claim_result


def dispatch_and_run(
    session: Session,
    world: TaskWorld,
    task: Task,
    *,
    worker_id: str = "worker-1",
    execution_ref: str = "exec_task_0001",
) -> int:
    """Claim, dispatch and move to ``running``; returns the fencing token."""
    lease = claim(session, world, worker_id=worker_id)
    start_attempt(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id=worker_id,
        fencing_token=lease.lease.fencing_token,
        execution_ref=execution_ref,
    )
    session.flush()
    mark_running(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        worker_id=worker_id,
        fencing_token=lease.lease.fencing_token,
        evidence_ref="evidence:execution_started_0001",
    )
    session.flush()
    return lease.lease.fencing_token


def move_to_verifying(
    session: Session,
    world: TaskWorld,
    task: Task,
    *,
    result_ref: str = "result_task_0001",
) -> None:
    dispatch_and_run(session, world, task)
    record_attempt_result(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        outcome="completed",
        result_ref=result_ref,
        evidence_refs=[EVIDENCE_REF],
    )
    session.flush()


def succeed(
    session: Session,
    world: TaskWorld,
    task: Task,
    *,
    result_ref: str = "result_task_0001",
) -> None:
    move_to_verifying(session, world, task, result_ref=result_ref)
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref=result_ref,
        evidence_refs=[EVIDENCE_REF],
        evidence=[evidence()],
    )
    session.flush()


def utcnow() -> datetime:
    return datetime.now(UTC)
