"""Priority is bounded, deterministic and scheduling-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.tasks import priority
from app.tasks.errors import TaskError
from app.tasks.priority import SchedulableTask

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _task(
    task_id: str,
    *,
    level: str = "normal",
    created_minutes_ago: int = 0,
    due_in_hours: float | None = None,
    owner: str = "prin_owner_0001",
) -> SchedulableTask:
    return SchedulableTask(
        task_id=task_id,
        priority=level,
        created_at=NOW - timedelta(minutes=created_minutes_ago),
        due_at=NOW + timedelta(hours=due_in_hours) if due_in_hours is not None else None,
        owner_principal_id=owner,
    )


def test_aging_prevents_starvation() -> None:
    old_low = _task("task_old_low_0001", level="low", created_minutes_ago=180)
    new_urgent = _task("task_new_urgent_0001", level="urgent")
    ordered = priority.order_queue([new_urgent, old_low], now=NOW)
    assert [item.task_id for item in ordered] == [
        "task_old_low_0001",
        "task_new_urgent_0001",
    ]
    # The boost is capped, so the relative order never inverts back.
    assert priority.aging_boost(old_low.created_at, NOW) == priority.MAX_AGING_BOOST


def test_priority_and_due_order_is_deterministic() -> None:
    tasks = [
        _task("task_b_0001", level="normal", created_minutes_ago=10),
        _task("task_a_0001", level="normal", created_minutes_ago=10),
        _task("task_due_0001", level="normal", created_minutes_ago=10, due_in_hours=0.2),
        _task("task_overdue_0001", level="normal", created_minutes_ago=10, due_in_hours=-1),
    ]
    first = [item.task_id for item in priority.order_queue(tasks, now=NOW)]
    second = [
        item.task_id for item in priority.order_queue(list(reversed(tasks)), now=NOW)
    ]
    assert first == second
    assert first[0] == "task_overdue_0001"
    assert first[1] == "task_due_0001"
    assert first[2:] == ["task_a_0001", "task_b_0001"]
    assert priority.priority_score(tasks[0], NOW) == priority.priority_score(
        tasks[1], NOW
    )


def test_fair_order_round_robins_owners_without_dropping_work() -> None:
    tasks = [
        _task(f"task_a_{index}_0001", owner="prin_a", created_minutes_ago=index)
        for index in range(4)
    ] + [
        _task(f"task_b_{index}_0001", owner="prin_b", created_minutes_ago=index)
        for index in range(4)
    ]
    ordered = priority.fair_order(tasks, now=NOW, wip_limit=2)
    assert len(ordered) == len(tasks)
    assert {item.task_id for item in ordered} == {item.task_id for item in tasks}
    owners = [item.owner_principal_id for item in ordered]
    assert owners[:4] == ["prin_a", "prin_b", "prin_a", "prin_b"]


def test_urgency_requires_explicit_authorisation_and_a_reason() -> None:
    with pytest.raises(TaskError) as excinfo:
        priority.effective_priority("urgent")
    assert excinfo.value.reason_code == "task_priority_unauthorized"

    with pytest.raises(TaskError) as excinfo:
        priority.effective_priority("urgent", authorized_by="prin_owner_0001")
    assert excinfo.value.reason_code == "task_priority_unauthorized"

    with pytest.raises(TaskError) as excinfo:
        priority.effective_priority("immediate")
    assert excinfo.value.reason_code == "task_priority_request_invalid"

    assert priority.effective_priority(
        "urgent", authorized_by="prin_owner_0001", rationale="legal deadline"
    ) == ("urgent", "prin_owner_0001", "legal deadline")


def test_priority_never_overrides_a_hard_deny_or_a_gate() -> None:
    priority.assert_scheduling_only(
        state="queued", decision_type="allow", gates_satisfied=True
    )
    with pytest.raises(TaskError) as excinfo:
        priority.assert_scheduling_only(
            state="denied", decision_type="deny", gates_satisfied=True
        )
    assert excinfo.value.reason_code == "priority_cannot_override_deny"

    with pytest.raises(TaskError) as excinfo:
        priority.assert_scheduling_only(
            state="evaluated",
            decision_type="allow",
            gates_satisfied=False,
            requested_priority="urgent",
        )
    assert excinfo.value.reason_code == "priority_cannot_override_deny"


def test_sla_status_distinguishes_response_and_completion() -> None:
    overdue_completion = SchedulableTask(
        task_id="task_due_0001",
        priority="normal",
        created_at=NOW - timedelta(hours=2),
        due_at=NOW - timedelta(minutes=1),
    )
    assert priority.sla_status(overdue_completion, now=NOW) == "completion_overdue"

    response_overdue = SchedulableTask(
        task_id="task_resp_0001",
        priority="normal",
        created_at=NOW - timedelta(hours=2),
        response_by=NOW - timedelta(minutes=1),
    )
    assert priority.sla_status(response_overdue, now=NOW) == "response_overdue"

    on_track = SchedulableTask(
        task_id="task_ok_0001",
        priority="normal",
        created_at=NOW,
        due_at=NOW + timedelta(days=2),
    )
    assert priority.sla_status(on_track, now=NOW) == "ok"
