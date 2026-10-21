"""Confirmation and approval lifecycles: hash, TTL, single use, separation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.grants import get_grant, revoke_grant
from app.tasks.errors import ConfirmationError, OwnerError, TaskError
from app.tasks.models import TaskConfirmation
from app.tasks.service import (
    confirm_task,
    consume_confirmation,
    create_confirmation,
    record_approval,
    recheck_execution,
)
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    EVIDENCE_REF,
    goal,
    pending_confirmation,
    submit_gated,
    utcnow,
)


def _gated(session: Session, world: TaskWorld):
    task = submit_gated(session, world)
    session.commit()
    return task, pending_confirmation(session, task)


def test_confirmation_then_approval_then_queue(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    assert task.state == "awaiting_confirmation"
    assert confirmation.status == "pending"

    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.commit()
    assert task.state == "awaiting_approval"
    assert task.confirmation_ref["status"] == "satisfied"
    assert task.workflow_instance_id is not None

    record_approval(
        session,
        task_id=task.task_id,
        actor=world.actors.approver,
        decision="approve",
    )
    session.commit()
    assert task.state == "queued"
    assert task.grant_id is not None
    assert task.approval_ref["decision"] == "approved"
    assert task.approval_ref["separation_check"] == "passed"


def test_payload_change_denies_the_task(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    with pytest.raises(ConfirmationError) as excinfo:
        confirm_task(
            session,
            task_id=task.task_id,
            actor=world.actors.requester,
            confirmation_id=confirmation.confirmation_id,
            payload={"amount": "999.00", "purpose": "expense.submit"},
        )
    assert excinfo.value.reason_code == "payload_hash_mismatch"
    session.commit()
    session.refresh(task)
    assert task.state == "denied"
    assert task.stop_confirmed is True


def test_expired_confirmation_is_rejected(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    future = utcnow() + timedelta(hours=1)
    with pytest.raises(ConfirmationError) as excinfo:
        confirm_task(
            session,
            task_id=task.task_id,
            actor=world.actors.requester,
            confirmation_id=confirmation.confirmation_id,
            now=future,
        )
    assert excinfo.value.reason_code == "task_confirmation_expired"
    session.commit()
    session.refresh(task)
    assert task.state == "denied"
    session.refresh(confirmation)
    assert confirmation.status == "expired"


def test_confirmation_is_single_use_and_replay_is_detected(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()

    first = consume_confirmation(
        session,
        task_id=task.task_id,
        actor=world.actors.executor,
        confirmation_id=confirmation.confirmation_id,
        call_id="call_confirm_0001",
    )
    assert first.status == "consumed"
    assert first.consumed_by_call_id == "call_confirm_0001"

    with pytest.raises(ConfirmationError) as excinfo:
        consume_confirmation(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            confirmation_id=confirmation.confirmation_id,
            call_id="call_confirm_0002",
        )
    assert excinfo.value.reason_code == "replay_detected"


def test_confirmation_without_the_confirmed_payload_is_refused(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()
    with pytest.raises(ConfirmationError) as excinfo:
        consume_confirmation(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            confirmation_id=confirmation.confirmation_id,
            call_id="call_wrong_payload_0001",
            payload_hash_value="sha256:" + "f" * 64,
        )
    assert excinfo.value.reason_code == "payload_hash_mismatch"


def test_channel_binding_is_enforced(session: Session, world: TaskWorld) -> None:
    task, _ = _gated(session, world)
    row = create_confirmation(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        ttl_seconds=900,
        channel_binding={"channel": "mcp", "channel_identity_ref": "chan_task_0001"},
    )
    session.flush()
    from app.tasks.service import satisfy_confirmation

    with pytest.raises(ConfirmationError) as excinfo:
        satisfy_confirmation(
            session,
            task_id=task.task_id,
            actor=world.actors.requester,
            confirmation_id=row.confirmation_id,
            channel="web",
        )
    assert excinfo.value.reason_code == "task_confirmation_channel_mismatch"


def test_the_wrong_person_cannot_confirm(session: Session, world: TaskWorld) -> None:
    task, confirmation = _gated(session, world)
    with pytest.raises(ConfirmationError) as excinfo:
        confirm_task(
            session,
            task_id=task.task_id,
            actor=world.actors.outsider,
            confirmation_id=confirmation.confirmation_id,
        )
    assert excinfo.value.reason_code == "task_confirmation_actor_invalid"


def test_self_approval_is_refused(session: Session, world: TaskWorld) -> None:
    task, confirmation = _gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()
    with pytest.raises(OwnerError) as excinfo:
        record_approval(
            session,
            task_id=task.task_id,
            actor=world.actors.requester,
            decision="approve",
        )
    assert excinfo.value.reason_code == "self_approval_forbidden"


def test_approver_must_be_the_configured_approver(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()
    with pytest.raises(OwnerError) as excinfo:
        record_approval(
            session,
            task_id=task.task_id,
            actor=world.actors.outsider,
            decision="approve",
        )
    assert excinfo.value.reason_code == "separation_of_duties_violation"


def test_approval_rejection_denies_the_task(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()
    record_approval(
        session,
        task_id=task.task_id,
        actor=world.actors.approver,
        decision="reject",
        reason="approval_missing",
    )
    session.commit()
    session.refresh(task)
    assert task.state == "denied"
    assert task.approval_ref["decision"] == "rejected"
    assert task.stop_confirmed is True


def test_revoked_grant_blocks_execution(
    session: Session, world: TaskWorld
) -> None:
    task, confirmation = _gated(session, world)
    confirm_task(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        confirmation_id=confirmation.confirmation_id,
    )
    session.flush()
    record_approval(
        session,
        task_id=task.task_id,
        actor=world.actors.approver,
        decision="approve",
    )
    session.commit()
    assert task.state == "queued"

    revoke_grant(
        session,
        grant_id=task.grant_id,
        reason_code="policy_changed",
        principal=world.actors.executor,
    )
    session.flush()
    with pytest.raises(TaskError) as excinfo:
        recheck_execution(
            session,
            task_id=task.task_id,
            actor=world.actors.executor,
            capability="expense.submit@1.0.0",
            call_id="call_revoked_0001",
        )
    assert excinfo.value.reason_code == "grant_revoked"
    assert get_grant(session, task.grant_id).status == "revoked"
