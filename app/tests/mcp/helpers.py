"""Helpers for the client MCP/REST acceptance tests."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.tests.governance.helpers import Org, PolicyContext

MCP_URL = "http://127.0.0.1:8000/mcp/"
BASE_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class World:
    org: Org
    policy: PolicyContext
    employee_id: int
    manager_id: int
    outsider_id: int
    finance_id: int


@asynccontextmanager
async def open_mcp_session(
    app,
    headers: dict[str, str] | None = None,
    *,
    manage_lifespan: bool = True,
    base_url: str = BASE_URL,
):
    """Open an official-SDK Streamable HTTP client against the mounted app.

    A given app's MCP session manager may be started once, so tests that open
    more than one client session use the ``live_app`` fixture and pass
    ``manage_lifespan=False``.  ``base_url`` is configurable so a test can send
    a non-loopback ``Host`` header.
    """
    if manage_lifespan:
        async with app.router.lifespan_context(app):
            async with open_mcp_session(
                app,
                headers,
                manage_lifespan=False,
                base_url=base_url,
            ) as session:
                yield session
        return

    mcp_url = base_url.rstrip("/") + "/mcp/"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=base_url,
        headers=headers or {},
    ) as http_client:
        async with streamable_http_client(
            mcp_url, http_client=http_client
        ) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


def initialize_body() -> dict[str, Any]:
    """A minimal Streamable HTTP ``initialize`` JSON-RPC request."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0.0"},
        },
    }


def transport_headers(token: str) -> dict[str, str]:
    """Only the token header needed by the MCP transport middleware."""
    return {"Authorization": f"Bearer {token}"}


def tool_text(result: Any) -> str:
    """Extract the single text block from a CallToolResult."""
    return result.content[0].text


def tool_json(result: Any) -> dict[str, Any]:
    """Parse the JSON text block returned by a successful tool call."""
    return json.loads(tool_text(result))


def tool_error_code(result: Any) -> str:
    """Extract ``error.code`` from a tool's JSON error envelope, if present."""
    text = tool_text(result)
    marker = '{"error"'
    start = text.find(marker)
    if start < 0:
        return ""
    payload = json.loads(text[start:])
    return payload["error"]["code"]


def goal_payload(summary: str = "Read my expense record") -> dict[str, Any]:
    return {
        "summary": summary,
        "completion_criteria": [
            {
                "criterion_id": "crit_0001",
                "type": "verifier_result",
                "verifier": {"verifier_id": "ver_0001", "version": "1.0.0"},
                "resource_ref": None,
                "expected": "passed",
                "evidence_ref": "evidence:test-run",
                "evidence_hash": None,
            }
        ],
        "out_of_scope": [],
    }


def self_scope() -> dict[str, Any]:
    return {
        "scope_type": "self",
        "resource_ids": ["res_expense_0001"],
        "connector_ids": [],
        "data_classifications": ["internal"],
        "exclude_resource_ids": [],
    }


def submit_body(
    *,
    idempotency_key: str,
    capability: str = "expense.read@1.0.0",
    scope: dict[str, Any] | None = None,
    summary: str = "Read my expense record",
) -> dict[str, Any]:
    return {
        "idempotency_key": idempotency_key,
        "goal": goal_payload(summary),
        "requested_capabilities": [capability],
        "resource_scope": scope or self_scope(),
    }
