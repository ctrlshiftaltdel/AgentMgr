"""REST/MCP parity, authorization and gate behavior against real PostgreSQL."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.rest import operations
from app.api.rest.ratelimit import RateLimiter
from app.audit.models import AuditEvent
from app.db.models.organization import Company, Employee
from app.governance.models import ExecutionGrant
from app.identity.models import EmployeeRole, Role
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
    assert rest_body["side_effects"]["persists_governance_decision"] is True
    assert rest_body["side_effects"]["mutates_task_state"] is False
    assert "read_only" not in rest_body
    assert mcp_body["side_effects"] == rest_body["side_effects"]


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
    assert body["approver_assigned"] is True
    assert body["separation_of_duties"] == "enforced"
    # The approver identity is server-side and intentionally not disclosed.
    assert "approver_principal_id" not in body


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


def _tasks_with_key(engine, idempotency_key: str) -> int:
    with Session(engine) as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.idempotency_key == idempotency_key)
            )
            or 0
        )


def _submission_rejections(engine, idempotency_key: str) -> list[AuditEvent]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "governance.task.submission_rejected"
                )
            )
        )
        return [
            row
            for row in rows
            if (row.details or {}).get("idempotency_key") == idempotency_key
        ]


@pytest.mark.anyio
async def test_multi_capability_submission_is_rejected_before_creation(
    world, client, live_app, auth, engine
):
    """B1 accepts exactly one capability; a longer list never creates a task."""
    headers = auth(world.employee_id)
    body = submit_body(idempotency_key="idem_multi_0001")
    body["requested_capabilities"] = [
        "expense.read@1.0.0",
        "expense.confirm@1.0.0",
    ]
    rest = client.post("/governance/tasks", json=body, headers=headers)
    assert rest.status_code == 422, rest.text
    assert rest.json()["error"]["code"] == "validation_error"

    async with open_mcp_session(live_app, headers=headers, manage_lifespan=False) as session:
        mcp = await session.call_tool("governance.submit_task", body)
    assert mcp.is_error is True
    assert tool_error_code(mcp) == "invalid_arguments"

    assert _tasks_with_key(engine, "idem_multi_0001") == 0


@pytest.mark.anyio
async def test_no_approver_rejection_leaves_no_task_and_retry_is_deterministic(
    world, client, live_app, auth, engine
):
    """A gated submission without an approver rolls the partial task back."""
    with Session(engine) as session:
        company = Company(name=f"Lonely {uuid4().hex[:8]}")
        session.add(company)
        session.flush()
        employee = Employee(company_id=company.id, employee_no="E-lonely")
        session.add(employee)
        session.flush()
        role = session.scalar(select(Role).where(Role.name == "employee"))
        assert role is not None
        session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))
        session.commit()
        lonely_id = employee.id

    headers = auth(lonely_id)
    body = submit_body(
        idempotency_key="idem_noapprover_0001",
        capability="expense.approve@1.0.0",
    )
    first = client.post("/governance/tasks", json=body, headers=headers)
    assert first.status_code == 422, first.text
    assert first.json()["error"]["code"] == "accountable_owner_unresolved"
    assert _tasks_with_key(engine, "idem_noapprover_0001") == 0
    rejections = _submission_rejections(engine, "idem_noapprover_0001")
    assert len(rejections) == 1
    assert rejections[0].details["reason_code"] == "accountable_owner_unresolved"

    # The retry must re-run and fail identically, not return a stranded row.
    second = client.post("/governance/tasks", json=body, headers=headers)
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "accountable_owner_unresolved"
    assert _tasks_with_key(engine, "idem_noapprover_0001") == 0


@pytest.mark.anyio
async def test_non_gated_task_has_no_approver_and_is_invisible_to_the_manager(
    world, client, live_app, auth, engine
):
    """Only an actual approval gate makes the resolved approver a participant."""
    employee_headers = auth(world.employee_id)
    created = client.post(
        "/governance/tasks",
        json=submit_body(idempotency_key="idem_nongated_0001"),
        headers=employee_headers,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    with Session(engine) as session:
        task = session.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        assert task.approver_principal_id is None

    manager_headers = auth(world.manager_id)
    listed = client.get("/governance/tasks", headers=manager_headers)
    assert listed.status_code == 200, listed.text
    assert all(item["task_id"] != task_id for item in listed.json()["tasks"])
    denied = client.get(f"/governance/tasks/{task_id}", headers=manager_headers)
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "task_unauthorized_actor"


@pytest.mark.anyio
async def test_cross_company_manager_is_not_used_as_approver(
    world, client, auth, engine
):
    """The manager shortcut is company-scoped; fallback stays in-company."""
    with Session(engine) as session:
        other_company = Company(name=f"Other {uuid4().hex[:8]}")
        session.add(other_company)
        session.flush()
        cross_manager = Employee(
            company_id=other_company.id, employee_no="E-cross-manager"
        )
        session.add(cross_manager)
        session.flush()
        role = session.scalar(select(Role).where(Role.name == "manager"))
        assert role is not None
        session.add(
            EmployeeRole(employee_id=cross_manager.id, role_id=role.id)
        )
        employee = session.get(Employee, world.employee_id)
        assert employee is not None
        employee.manager_id = cross_manager.id
        session.commit()
        cross_manager_id = cross_manager.id

    created = client.post(
        "/governance/tasks",
        json=submit_body(
            idempotency_key="idem_cross_company_0001",
            capability="expense.approve@1.0.0",
        ),
        headers=auth(world.employee_id),
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    with Session(engine) as session:
        task = session.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        assert task.approver_employee_id == world.manager_id
        assert task.approver_employee_id != cross_manager_id

    # An actual approval gate does make the approver a participant.
    manager_headers = auth(world.manager_id)
    listed = client.get("/governance/tasks", headers=manager_headers)
    assert listed.status_code == 200, listed.text
    assert any(item["task_id"] == task_id for item in listed.json()["tasks"])
    visible = client.get(f"/governance/tasks/{task_id}", headers=manager_headers)
    assert visible.status_code == 200, visible.text


@pytest.mark.anyio
async def test_reserved_payload_authority_keys_are_rejected(
    world, client, live_app, auth, engine
):
    """The payload is opaque; authority-bearing keys fail before creation."""
    headers = auth(world.employee_id)
    for index, payload in enumerate(
        (
            {"execution_mode": "managed_execution"},
            {"step": {"owner": "prin_9999"}},
        )
    ):
        key = f"idem_payload_bad_{index:04d}"
        body = submit_body(idempotency_key=key)
        body["payload"] = payload
        rest = client.post("/governance/tasks", json=body, headers=headers)
        assert rest.status_code == 422, rest.text
        assert rest.json()["error"]["code"] == "validation_error"
        assert _tasks_with_key(engine, key) == 0

        async with open_mcp_session(
            live_app, headers=headers, manage_lifespan=False
        ) as session:
            mcp = await session.call_tool("governance.submit_task", body)
        assert mcp.is_error is True
        assert tool_error_code(mcp) == "invalid_arguments"
        assert _tasks_with_key(engine, key) == 0

    ok = submit_body(idempotency_key="idem_payload_ok_0001")
    ok["payload"] = {"note": "opaque execution data"}
    accepted = client.post("/governance/tasks", json=ok, headers=headers)
    assert accepted.status_code == 201, accepted.text


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
