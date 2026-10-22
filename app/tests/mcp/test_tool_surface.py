"""Non-database MCP surface checks: names, schemas, auth and protocol errors."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.tests.mcp.helpers import open_mcp_session, tool_error_code, tool_text

#: This module is intentionally runnable without PostgreSQL.  Its own fixtures
#: still set a token so the authentication path is exercised.
_SURFACE_TOKEN = "issue-80-surface-test-token"


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AJAIMGR_INTERNAL_TOKEN", _SURFACE_TOKEN)
    from app.api.app import create_app

    return create_app()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AJAIMGR_INTERNAL_TOKEN", _SURFACE_TOKEN)
    from app.api.app import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"

EXPECTED_TOOLS = {
    "governance.evaluate_task",
    "governance.submit_task",
    "governance.get_task",
    "governance.list_my_tasks",
    "governance.request_confirmation",
    "governance.request_approval",
    "governance.cancel_task",
    "governance.get_result",
}

FORBIDDEN_PROPERTIES = {
    "principal_id",
    "principal_type",
    "employee_id",
    "service_identity_id",
    "owner",
    "approver",
    "role_ids",
    "roles",
    "priority",
    "permissions",
}


@pytest.mark.anyio
async def test_streamable_http_exposes_the_client_tool_surface(app):
    async with open_mcp_session(app) as session:
        tools = (await session.list_tools()).tools

    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == EXPECTED_TOOLS
    for tool in tools:
        description = tool.description or ""
        assert "Side effects" in description
        assert "Risk" in description
        assert "Not applicable" in description
        assert "Confirmation" in description
        assert "Approval" in description
        schema = tool.input_schema
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False
        properties = set(schema.get("properties", {}))
        assert not (properties & FORBIDDEN_PROPERTIES), (tool.name, properties)


@pytest.mark.anyio
async def test_submit_schema_requires_core_inputs_and_has_no_authority_fields(app):
    async with open_mcp_session(app) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    schema = tools["governance.submit_task"].input_schema
    assert set(schema["required"]) == {
        "idempotency_key",
        "goal",
        "requested_capabilities",
        "resource_scope",
    }
    props = schema["properties"]
    assert props["idempotency_key"]["pattern"].startswith("^idem_")
    assert props["requested_capabilities"]["items"]["pattern"].endswith(
        "@[0-9]+\\.[0-9]+\\.[0-9]+$"
    )
    assert "execution_mode" not in props
    defs = schema["$defs"]
    assert defs["ResourceScope"]["additionalProperties"] is False
    assert defs["Goal"]["additionalProperties"] is False


@pytest.mark.anyio
async def test_unauthenticated_tool_call_is_rejected(app):
    async with open_mcp_session(app) as session:
        result = await session.call_tool("governance.get_task", {"task_id": "task_0001"})

    assert result.is_error is True
    assert tool_error_code(result) == "unauthorized"
    assert "Traceback" not in tool_text(result)
    assert "sqlalchemy" not in tool_text(result).lower()


@pytest.mark.anyio
async def test_forged_bearer_token_is_rejected(app):
    async with open_mcp_session(
        app, headers={"Authorization": "Bearer not-the-token", "X-Employee-Id": "1"}
    ) as session:
        result = await session.call_tool("governance.get_task", {"task_id": "task_0001"})

    assert result.is_error is True
    assert tool_error_code(result) == "unauthorized"


@pytest.mark.anyio
async def test_malformed_employee_header_is_rejected(app):
    async with open_mcp_session(
        app,
        headers={
            "Authorization": f"Bearer {_SURFACE_TOKEN}",
            "X-Employee-Id": "not-an-employee-id",
        },
    ) as session:
        result = await session.call_tool("governance.get_task", {"task_id": "task_0001"})

    assert result.is_error is True
    assert tool_error_code(result) == "unauthorized"


@pytest.mark.anyio
async def test_unknown_tool_and_invalid_arguments_use_stable_codes(app):
    async with open_mcp_session(app) as session:
        unknown = await session.call_tool("governance.no_such_tool", {})
        extra = await session.call_tool(
            "governance.get_task",
            {"task_id": "task_0001", "employee_id": 1},
        )

    assert unknown.is_error is True
    assert tool_error_code(unknown) == "unknown_tool"
    assert extra.is_error is True
    assert tool_error_code(extra) == "invalid_arguments"
    assert "Traceback" not in tool_text(extra)


@pytest.mark.anyio
async def test_rest_mirror_requires_the_same_credentials(client):
    response = client.get("/governance/tasks/task_0001")
    assert response.status_code == 401
    body = response.json()
    assert body == {
        "error": {"code": "unauthorized", "message": "authentication required"}
    }
