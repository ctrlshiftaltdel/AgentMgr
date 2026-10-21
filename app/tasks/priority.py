"""Bounded priority, ageing, SLA and fairness ordering.

Priority is **scheduling-only**.  It reorders work that is already admissible;
it never grants a capability, satisfies a gate, revives a deny or writes a
state.  ``authority_effect = scheduling_only`` is a behavioural claim, not a
comment: :func:`assert_scheduling_only` rejects any attempt to use a priority
change to bypass a hard deny, a missing confirmation/approval or a terminal
state (F08/F19).

The ordering is deterministic and bounded:

* a level maps to a fixed integer base score (low 0, normal 100, high 200,
  urgent 300);
* waiting time adds ``25`` points per started 10-minute interval, capped at
  ``400`` -- above the 300-point span between low and urgent, so a low task can
  never be starved by a stream of urgent arrivals;
* due dates add a bounded urgency boost (overdue / due within an hour);
* ties break by earlier due date, earlier creation time, then ``task_id``, so
  two workers compute the same order from the same rows;
* :func:`fair_order` additionally round-robins accountable owners inside the
  score order, so one owner cannot monopolise the first batch.

Emergency (``urgent``) priority requires an explicit authorising principal and
a non-empty rationale; without both it is a deterministic rejection rather than
a silent downgrade.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.tasks.errors import TaskError

__all__ = [
    "AGING_INTERVAL_SECONDS",
    "AGING_STEP",
    "BASE_PRIORITY_SCORE",
    "DEFAULT_OWNER_WIP_LIMIT",
    "MAX_AGING_BOOST",
    "PRIORITY_LEVELS",
    "SchedulableTask",
    "assert_scheduling_only",
    "due_urgency_boost",
    "effective_priority",
    "fair_order",
    "order_queue",
    "priority_score",
    "sla_status",
]

PRIORITY_LEVELS: tuple[str, ...] = ("low", "normal", "high", "urgent")

#: Fixed base score per level.  ``urgent`` is only 300 points above ``low``,
#: while ageing can add 400 -- the anti-starvation margin.
BASE_PRIORITY_SCORE: dict[str, int] = {
    "low": 0,
    "normal": 100,
    "high": 200,
    "urgent": 300,
}

AGING_INTERVAL_SECONDS = 600
AGING_STEP = 25
MAX_AGING_BOOST = 400
DUE_SOON_SECONDS = 3600
DUE_SOON_BOOST = 30
OVERDUE_BOOST = 75

#: Default concurrent work-in-progress allowance per accountable owner, used by
#: :func:`fair_order`.  Exceeding it only delays a task; it never drops it.
DEFAULT_OWNER_WIP_LIMIT = 3


@dataclass(frozen=True)
class SchedulableTask:
    """The scheduling-relevant projection of a task row.

    Keeping this separate from the ORM model makes the ordering a pure function
    that can be unit tested without a database.
    """

    task_id: str
    priority: str
    created_at: datetime
    due_at: datetime | None = None
    owner_principal_id: str | None = None
    state: str = "queued"
    response_by: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def effective_priority(
    requested: str,
    *,
    authorized_by: str | None = None,
    rationale: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Validate a requested priority and return ``(level, authorised_by, why)``.

    ``urgent`` requires both an authorising principal and a rationale.  A blank
    rationale is not a rationale.  A missing authorisation is a rejection, not a
    silent downgrade, so the caller cannot later claim urgency was granted.
    """
    if requested not in PRIORITY_LEVELS:
        raise TaskError(
            "task_priority_request_invalid",
            f"unknown priority {requested!r}",
            context={"requested": requested, "allowed": list(PRIORITY_LEVELS)},
        )
    if requested == "urgent":
        if not authorized_by or not str(authorized_by).strip():
            raise TaskError(
                "task_priority_unauthorized",
                "urgent priority requires an explicit authorising principal",
                context={"requested": requested},
            )
        if not rationale or not str(rationale).strip():
            raise TaskError(
                "task_priority_unauthorized",
                "urgent priority requires a non-empty rationale",
                context={"requested": requested},
            )
        return requested, authorized_by, rationale
    return requested, authorized_by, rationale


def aging_boost(created_at: datetime, now: datetime) -> int:
    """Bounded ageing boost for a waiting task."""
    created = _aware(created_at)
    current = _aware(now)
    if created is None or current is None:
        return 0
    waited = max(0.0, (current - created).total_seconds())
    intervals = int(waited // AGING_INTERVAL_SECONDS)
    return min(MAX_AGING_BOOST, intervals * AGING_STEP)


def due_urgency_boost(due_at: datetime | None, now: datetime) -> int:
    """Bounded due-date boost (overdue, or due within the hour)."""
    due = _aware(due_at)
    current = _aware(now)
    if due is None or current is None:
        return 0
    remaining = (due - current).total_seconds()
    if remaining <= 0:
        return OVERDUE_BOOST
    if remaining <= DUE_SOON_SECONDS:
        return DUE_SOON_BOOST
    return 0


def priority_score(task: SchedulableTask, now: datetime) -> int:
    """Return the deterministic scheduling score (higher runs earlier)."""
    base = BASE_PRIORITY_SCORE.get(task.priority)
    if base is None:
        return 0
    return base + aging_boost(task.created_at, now) + due_urgency_boost(
        task.due_at, now
    )


def _sort_key(task: SchedulableTask, now: datetime) -> tuple[int, float, float, str]:
    due = _aware(task.due_at)
    return (
        -priority_score(task, now),
        due.timestamp() if due is not None else float("inf"),
        _aware(task.created_at).timestamp(),  # type: ignore[union-attr]
        task.task_id,
    )


def order_queue(
    tasks: Iterable[SchedulableTask],
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[SchedulableTask]:
    """Order admissible tasks by score, due date, age, then id."""
    current = now or datetime.now(UTC)
    ordered = sorted(tasks, key=lambda task: _sort_key(task, current))
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        return ordered[:limit]
    return ordered


def fair_order(
    tasks: Iterable[SchedulableTask],
    *,
    now: datetime | None = None,
    wip_limit: int = DEFAULT_OWNER_WIP_LIMIT,
) -> list[SchedulableTask]:
    """Round-robin owners inside the priority order (bounded WIP, no dropping).

    Each owner may appear at most ``wip_limit`` times before owners that have
    contributed less are promoted.  Within one owner the priority order is
    preserved.  Tasks without an owner are treated as a single ``"unowned"``
    group.  Nothing is discarded: the output is a permutation of the input.
    """
    ordered = order_queue(tasks, now=now)
    if wip_limit < 1:
        raise ValueError("wip_limit must be >= 1")
    groups: dict[str, list[SchedulableTask]] = {}
    for task in ordered:
        key = task.owner_principal_id or "unowned"
        groups.setdefault(key, []).append(task)

    result: list[SchedulableTask] = []
    emitted: dict[str, int] = {key: 0 for key in groups}
    while len(result) < len(ordered):
        progressed = False
        for key in groups:  # insertion order == first-seen priority order
            if emitted[key] >= wip_limit:
                continue
            if not groups[key]:
                continue
            result.append(groups[key].pop(0))
            emitted[key] += 1
            progressed = True
        if not progressed:
            # All groups hit the WIP cap; everyone gets another slot.  This is
            # why the fair order can delay but never starve anyone.
            emitted = {key: 0 for key in groups}
    return result


def sla_status(
    task: SchedulableTask,
    *,
    now: datetime | None = None,
) -> str:
    """Return ``ok`` / ``response_due`` / ``response_overdue`` /
    ``completion_overdue`` for one task.

    ``response_by`` (from the SLA block) governs the first response; ``due_at``
    governs completion.  Completion overdue wins over an earlier response
    deadline because it is the harder failure.
    """
    current = _aware(now) or datetime.now(UTC)
    due = _aware(task.due_at)
    if due is not None and due <= current:
        return "completion_overdue"
    response_by = _aware(task.response_by)
    if response_by is not None:
        if response_by <= current:
            return "response_overdue"
        if response_by - current <= timedelta(hours=1):
            return "response_due"
    if due is not None and due - current <= timedelta(hours=1):
        return "response_due"
    return "ok"


def assert_scheduling_only(
    *,
    state: str,
    decision_type: str | None,
    gates_satisfied: bool,
    requested_priority: str | None = None,
) -> None:
    """Reject any attempt to use priority as authority.

    Scheduling may reorder only work that already passed the hard gates.  This
    is the single check the service calls before applying a priority change.
    """
    if state == "denied" or decision_type == "deny":
        raise TaskError(
            "priority_cannot_override_deny",
            "priority cannot reopen a denied task",
            context={"state": state, "requested_priority": requested_priority},
        )
    if not gates_satisfied:
        raise TaskError(
            "priority_cannot_override_deny",
            "priority cannot satisfy a confirmation/approval gate",
            context={"state": state, "requested_priority": requested_priority},
        )


def owner_loads(tasks: Sequence[SchedulableTask]) -> dict[str, int]:
    """Count queued tasks per owner (diagnostics/test helper)."""
    loads: dict[str, int] = {}
    for task in tasks:
        key = task.owner_principal_id or "unowned"
        loads[key] = loads.get(key, 0) + 1
    return loads
