"""W1-14 §7 concurrency: two real connections must never process one event twice.

The Outbox worker grabs work with ``SELECT ... FOR UPDATE SKIP LOCKED``.  These
tests drive that with two genuinely independent database connections -- including
two OS threads that each own their own ``Session``/connection -- and assert:

* the two claimed batches are disjoint (no event is handed to two workers);
* every event is processed exactly once (one notification, unique dedup key);
* an event held (uncommitted) by one connection is skipped, not double-claimed,
  by a second connection;
* once a batch is released, the events become claimable again.

No mocking: the locks, the ``SKIP LOCKED`` semantics and the notification rows are
all exercised against the real PostgreSQL database.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.tests.qa.conftest import World
from app.workflow.models import Notification, OutboxEvent
from app.workflow.service import (
    claim_batch,
    enqueue_event,
    mark_published,
    process_event,
)

N_EVENTS = 20
HALF = N_EVENTS // 2


def _enqueue_many(engine: Engine, world: World, count: int = N_EVENTS) -> list[int]:
    """Commit ``count`` pending ``approval.requested`` events; return their ids."""
    with Session(engine) as session:
        ids = [
            enqueue_event(
                session,
                event_type="approval.requested",
                aggregate_type="workflow_instance",
                aggregate_id=f"cc-{index}",
                idempotency_key=f"W1-14-CC-{index}",
                payload={"approver_id": world.manager.id},
            ).id
            for index in range(count)
        ]
        session.commit()
    return ids


def _notification_count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Notification))


def _distinct_dedup_keys(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count(func.distinct(Notification.dedup_key)))
        )


def test_two_threaded_connections_claim_disjoint_batches_and_process_once(
    qa_engine: Engine, world: World
) -> None:
    """Two real connections claiming concurrently get disjoint batches and no duplicates."""
    _enqueue_many(qa_engine, world)

    barrier = threading.Barrier(2)
    claimed: dict[str, list[int]] = {}
    errors: list[tuple[str, str]] = []

    def worker(name: str) -> None:
        session = Session(qa_engine)
        try:
            barrier.wait(timeout=15)
            batch = claim_batch(session, limit=HALF)
            claimed[name] = [event.id for event in batch]
            for event in batch:
                process_event(session, event)
                mark_published(session, event)
            barrier.wait(timeout=15)  # hold the locks until the peer has claimed
            session.commit()
        except Exception as exc:  # pragma: no cover - surfaced via ``errors``
            errors.append((name, repr(exc)))
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)

    first, second = claimed["A"], claimed["B"]
    assert len(first) == HALF, first
    assert len(second) == HALF, second
    assert set(first).isdisjoint(set(second)), "an event was claimed by both workers"
    assert len(set(first) | set(second)) == N_EVENTS

    # Each event produced exactly one notification: no duplicate processing.
    assert _notification_count(qa_engine) == N_EVENTS
    assert _distinct_dedup_keys(qa_engine) == N_EVENTS
    with Session(qa_engine) as session:
        published = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == "published")
        )
    assert published == N_EVENTS


def test_large_limit_on_second_connection_gets_nothing_while_locked(
    qa_engine: Engine, world: World
) -> None:
    """A second connection cannot steal events already locked by the first."""
    locked_ids = set(_enqueue_many(qa_engine, world))

    session_a = Session(qa_engine)
    session_b = Session(qa_engine)
    try:
        batch_a = claim_batch(session_a, limit=N_EVENTS * 2)
        assert {event.id for event in batch_a} == locked_ids

        batch_b = claim_batch(session_b, limit=N_EVENTS * 2)
        assert batch_b == []
    finally:
        session_a.rollback()
        session_b.rollback()
        session_a.close()
        session_b.close()

    # After the rollback the events are pending again and can be re-claimed.
    with Session(qa_engine) as session_c:
        reclaimed = claim_batch(session_c, limit=N_EVENTS * 2)
        assert {event.id for event in reclaimed} == locked_ids
        session_c.rollback()


def test_claimed_event_is_not_reclaimed_within_one_connection(
    qa_engine: Engine, world: World
) -> None:
    """A second ``claim_batch`` on the same connection skips already-processing rows."""
    _enqueue_many(qa_engine, world)
    with Session(qa_engine) as session:
        first = claim_batch(session, limit=N_EVENTS)
        assert len(first) == N_EVENTS
        assert all(event.status == "processing" for event in first)
        assert claim_batch(session, limit=N_EVENTS) == []
        session.rollback()


def test_future_available_at_is_not_claimed(qa_engine: Engine, world: World) -> None:
    """An event scheduled in the future is invisible to ``claim_batch``."""
    with Session(qa_engine) as session:
        event = enqueue_event(
            session,
            event_type="approval.requested",
            aggregate_type="workflow_instance",
            aggregate_id="cc-future",
            idempotency_key="W1-14-CC-FUTURE",
            payload={"approver_id": world.manager.id},
        )
        event.available_at = datetime.now(UTC) + timedelta(seconds=300)
        session.flush()
        assert claim_batch(session, limit=N_EVENTS) == []
        assert event.status == "pending"
        session.rollback()


def test_processing_events_are_skipped_by_claim_batch(
    qa_engine: Engine, world: World
) -> None:
    """Only ``pending`` events are eligible; ``processing``/``published`` are skipped."""
    ids = _enqueue_many(qa_engine, world, count=4)
    with Session(qa_engine) as session:
        session.execute(
            OutboxEvent.__table__.update()
            .where(OutboxEvent.id.in_(ids[:2]))
            .values(status="processing")
        )
        session.commit()
    with Session(qa_engine) as session:
        batch = claim_batch(session, limit=N_EVENTS)
        assert {event.id for event in batch} == set(ids[2:])
        session.rollback()
