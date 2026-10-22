"""REST/MCP parity, authorization and gate behavior against real PostgreSQL."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.rest import operations
from app.api.rest.ratelimit import RateLimiter
from app.db.models.organization import Employee
from app.governance.models import ExecutionGrant
from app.tasks import service as task_service
from app.tasks.models import Task
from app.tests.mcp.helpers import (
    goal_payload,
    open_mcp_session,
    self_scope,
    submit_body,
    tool_error_code,
    tool_json,
    tool_text,
)


def _task(engine, task_id: str) -> Task:
    with Session(engine) as session:
        task = session.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        session.expunge(task)
        return task


def _grant_count(engine, task_id: str) -> int:
    with Session(engine) as session:
        return int(
            session.scalar(
                select(func.count()).select_from(ExecutionGrant).where(
                    ExecutionGrant.task_id == task_id
                )
            )
            or 0
        )


@pytest.mark.anyio
async def test_a_denied_pdp_decision_is_denied_through_rest_and_mcp(
    world, client, live_app, auth
):
    headers = auth(world.manager_id)
    rest = client.post(
        "/governance/tasks/evaluate",
        json={"capability": "payment.execute@1.0.0"},
        headers=headers,
    )
    assert rest.status_code == 200, rest.text
    rest_body = rest.json()
    assert rest_body["decision_type"] == "deny"

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        result = await session.call_tool(
            "governance.evaluate_task",
            {"capability": "payment.execute@1.0.0"},
        )
    assert result.is_error is False, tool_text(result)
    mcp_body = tool_json(result)
    assert mcp_body["decision_type"] == "deny"
    assert mcp_body["reason_codes"] == rest_body["reason_codes"]
    assert mcp_body["read_only"] is True


@pytest.mark.anyio
async def test_rest_and_mcp_submit_delegate_to_the_same_task_service(
    world, client, live_app, auth, monkeypatch
):
    seen: list[str] = []
    real_submit = task_service.submit_task

    def spy(*args, **kwargs):
        seen.append(kwargs["idempotency_key"])
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(task_service, "submit_task", spy)
    headers = auth(world.employee_id)

    rest = client.post(
        "/governance/tasks",
        json=submit_body(idempotency_key="idem_rest_0001", summary="REST task"),
        headers=headers,
    )
    assert rest.status_code == 201, rest.text

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        result = await session.call_tool(
            "governance.submit_task",
            submit_body(idempotency_key="idem_mcp_0001", summary="MCP task"),
        )
    assert result.is_error is False, tool_text(result)

    assert seen == ["idem_rest_0001", "idem_mcp_0001"]


@pytest.mark.anyio
async def test_ownership_cancel_and_result_are_enforced_on_both_transports(
    world, client, live_app, auth
):
    employee_headers = auth(world.employee_id)
    outsider_headers = auth(world.outsider_id)

    created = client.post(
        "/governance/tasks",
        json=submit_body(
            idempotency_key="idem_confirm_0001",
            capability="expense.confirm@1.0.0",
        ),
        headers=employee_headers,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    assert created.json()["state"] == "awaiting_confirmation"

    async with open_mcp_session(live_app, headers=outsider_headers, manage_lifespan=False) as session:
        denied_read = await session.call_tool(
            "governance.get_task", {"task_id": task_id}
        )
        denied_result = await session.call_tool(
            "governance.get_result", {"task_id": task_id}
        )
        denied_cancel = await session.call_tool(
            "governance.cancel_task", {"task_id": task_id}
        )
    for denied in (denied_read, denied_result, denied_cancel):
        assert denied.is_error is True
        assert tool_error_code(denied) == "task_unauthorized_actor"

    assert (
        client.get(
            f"/governance/tasks/{task_id}/result", headers=outsider_headers
        ).status_code
        == 403
    )

    async with open_mcp_session(live_app, headers=employee_headers, manage_lifespan=False) as session:
        visible = await session.call_tool(
            "governance.get_task", {"task_id": task_id}
        )
        cancelled = await session.call_tool(
            "governance.cancel_task",
            {"task_id": task_id, "cancel_id": "cancel_0001"},
        )
        result = await session.call_tool(
            "governance.get_result", {"task_id": task_id}
        )
    assert visible.is_error is False
    assert tool_json(visible)["task_id"] == task_id
    assert cancelled.is_error is False
    assert tool_json(cancelled)["state"] == "cancelled"
    assert result.is_error is False
    payload = tool_json(result)
    assert payload["state"] == "cancelled"
    assert payload["result"]["outcome"] == "cancelled"
    assert set(payload["result"]) == {
        "outcome",
        "result_ref",
        "evidence_refs",
        "verification",
        "stop_confirmed",
        "side_effects_reconciled",
    }


@pytest.mark.anyio
async def test_read_only_evaluate_does_not_mutate_the_task(
    world, client, live_app, auth, engine
):
    headers = auth(world.employee_id)
    created = client.post(
        "/governance/tasks",
        json=submit_body(idempotency_key="idem_readonly_0001"),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    before = _task(engine, task_id)
    grants_before = _grant_count(engine, task_id)

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        evaluated = await session.call_tool(
            "governance.evaluate_task",
            {"capability": "expense.read@1.0.0", "task_id": task_id},
        )
    assert evaluated.is_error is False, tool_text(evaluated)
    after_mcp = _task(engine, task_id)
    assert (after_mcp.state, after_mcp.state_revision) == (
        before.state,
        before.state_revision,
    )
    assert _grant_count(engine, task_id) == grants_before

    rest = client.post(
        "/governance/tasks/evaluate",
        json={"capability": "expense.read@1.0.0", "task_id": task_id},
        headers=headers,
    )
    assert rest.status_code == 200, rest.text
    after_rest = _task(engine, task_id)
    assert (after_rest.state, after_rest.state_revision) == (
        before.state,
        before.state_revision,
    )
    assert _grant_count(engine, task_id) == grants_before


@pytest.mark.anyio
async def test_schema_and_out_of_scope_resources_are_rejected(
    world, client, live_app, auth
):
    headers = auth(world.employee_id)

    # Unknown authority field in the REST body -> 422, not silently ignored.
    bad_body = submit_body(idempotency_key="idem_bad_body_0001")
    bad_body["owner"] = "prin_9999"
    bad = client.post("/governance/tasks", json=bad_body, headers=headers)
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation_error"

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        bad_tool = await session.call_tool(
            "governance.submit_task",
            {**submit_body(idempotency_key="idem_bad_tool_0001"), "priority": "urgent"},
        )
        out_of_scope = await session.call_tool(
            "governance.evaluate_task",
            {
                "capability": "inventory.read@1.0.0",
                "resource_scope": self_scope(),
            },
        )
    assert bad_tool.is_error is True
    assert tool_error_code(bad_tool) == "invalid_arguments"
    assert out_of_scope.is_error is False, tool_text(out_of_scope)
    decision = tool_json(out_of_scope)
    assert decision["decision_type"] == "deny"
    assert set(decision["reason_codes"]) & {
        "capability_not_granted",
        "resource_scope_denied",
    }


@pytest.mark.anyio
async def test_confirmation_and_approval_gates_are_not_bypassed(
    world, client, live_app, auth
):
    headers = auth(world.employee_id)

    confirm = client.post(
        "/governance/tasks",
        json=submit_body(
            idempotency_key="idem_gate_confirm_0001",
            capability="expense.confirm@1.0.0",
            summary="Confirm gate task",
        ),
        headers=headers,
    )
    assert confirm.status_code == 201, confirm.text
    confirm_task = confirm.json()
    assert confirm_task["state"] == "awaiting_confirmation"

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        not_queued_result = await session.call_tool(
            "governance.get_result", {"task_id": confirm_task["task_id"]}
        )
        wrong_gate = await session.call_tool(
            "governance.request_approval",
            {"task_id": confirm_task["task_id"]},
        )
        requested = await session.call_tool(
            "governance.request_confirmation",
            {"task_id": confirm_task["task_id"]},
        )
    assert not_queued_result.is_error is False
    assert tool_json(not_queued_result)["result"] is None
    assert wrong_gate.is_error is True
    assert tool_error_code(wrong_gate) == "task_invalid_transition"
    assert requested.is_error is False, tool_text(requested)
    confirmation = tool_json(requested)
    assert confirmation["status"] == "pending"
    assert confirmation["single_use"] is True

    approve = client.post(
        "/governance/tasks",
        json=submit_body(
            idempotency_key="idem_gate_approve_0001",
            capability="expense.approve@1.0.0",
            summary="Approval gate task",
        ),
        headers=headers,
    )
    assert approve.status_code == 201, approve.text
    approve_task = approve.json()
    assert approve_task["state"] == "awaiting_approval"

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        approval = await session.call_tool(
            "governance.request_approval",
            {"task_id": approve_task["task_id"]},
        )
    assert approval.is_error is False, tool_text(approval)
    body = tool_json(approval)
    assert body["status"] == "in_progress"
    assert body["approver_principal_id"] != f"prin_{world.employee_id:04d}"


@pytest.mark.anyio
async def test_disabled_employee_is_rejected_on_both_transports(
    world, client, live_app, auth, engine
):
    headers = auth(world.employee_id)
    with Session(engine) as session:
        employee = session.get(Employee, world.employee_id)
        assert employee is not None
        employee.status = "inactive"
        session.commit()
    try:
        rest = client.get("/governance/tasks", headers=headers)
        assert rest.status_code == 403
        assert rest.json()["error"]["code"] == "forbidden"

        async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
            result = await session.call_tool("governance.list_my_tasks", {})
        assert result.is_error is True
        assert tool_error_code(result) == "forbidden"
    finally:
        with Session(engine) as session:
            employee = session.get(Employee, world.employee_id)
            assert employee is not None
            employee.status = "active"
            session.commit()


@pytest.mark.anyio
async def test_forged_employee_header_is_rejected_on_both_transports(
    world, client, live_app, auth, api_token
):
    forged = {
        "Authorization": f"Bearer {api_token}",
        "X-Employee-Id": "999999999",
    }
    rest = client.get("/governance/tasks", headers=forged)
    assert rest.status_code == 403
    assert rest.json()["error"]["code"] == "forbidden"

    async with open_mcp_session(
        live_app, headers=forged, manage_lifespan=False
    ) as session:
        result = await session.call_tool("governance.list_my_tasks", {})
    assert result.is_error is True
    assert tool_error_code(result) == "forbidden"


@pytest.mark.anyio
async def test_rate_limit_is_shared_by_rest_and_mcp(
    world, client, live_app, auth, monkeypatch
):
    from app.api.rest import ratelimit

    headers = auth(world.employee_id)
    created = client.post(
        "/governance/tasks",
        json=submit_body(idempotency_key="idem_rate_0001"),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    monkeypatch.setattr(
        ratelimit, "GOVERNANCE_LIMITER", RateLimiter(limit=1, window_seconds=60)
    )

    first = client.get(f"/governance/tasks/{task_id}", headers=headers)
    assert first.status_code == 200, first.text

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        limited = await session.call_tool(
            "governance.get_task", {"task_id": task_id}
        )
    assert limited.is_error is True
    assert tool_error_code(limited) == "rate_limited"

    second = client.get(f"/governance/tasks/{task_id}", headers=headers)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


def test_handlers_do_not_duplicate_authorization_rules():
    root = Path(__file__).resolve().parents[2]
    router_src = (root / "api" / "rest" / "router.py").read_text()
    mcp_src = (root / "api" / "mcp" / "server.py").read_text()
    operations_src = (root / "api" / "rest" / "operations.py").read_text()

    forbidden_rule_tokens = (
        "has_permission",
        "can_approve",
        "_actor_authorized",
        "visible_employee_ids",
        "effective_permissions",
        "owner_principal_id ==",
        "requester_principal_id ==",
    )
    for source in (router_src, mcp_src):
        for token in forbidden_rule_tokens:
            assert token not in source, token

    # The single application layer delegates to the core task/PDP services.
    assert "_require_task_authority" in operations_src
    assert "task_service.submit_task" in operations_src
    assert "governance_service.evaluate" in operations_src
    assert operations.submit_task.__module__ == "app.api.rest.operations"
