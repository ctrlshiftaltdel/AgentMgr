"""Fixtures for the client MCP / REST governance tests.

These tests use the real PostgreSQL test database, the real PDP/PEP/task
services and the official MCP SDK.  Nothing in the governance layer is mocked.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.governance.policies import DEFAULT_POLICY_SET_ID
from app.identity.seed import seed_permission_catalog
from app.tests.governance.helpers import (
    DEFAULT_RULES,
    Org,
    PolicyContext,
    alembic_config,
    make_org,
    publish_policy,
)
from app.tests.mcp.helpers import World
from app.tests.tasks.conftest import reset_task_state

TOKEN_ENV = "AJAIMGR_INTERNAL_TOKEN"
TEST_TOKEN = "issue-80-test-token"


def _rules() -> list[dict[str, Any]]:
    """Policy used by the acceptance tests.

    * ``expense.read`` queues without gates (the basic submit path).
    * ``expense.confirm`` requires only a confirmation (confirmation tests).
    * ``expense.approve`` requires only an approval (approval tests).
    * ``payment.execute`` is a hard deny (parity/denial tests).
    """
    rules = [dict(rule) for rule in DEFAULT_RULES]
    rules = [rule for rule in rules if rule["rule_id"] != "submit_self"]
    rules.extend(
        [
            {
                "rule_id": "client_confirm",
                "capability": "expense.confirm@1.0.0",
                "required_permissions": [],
                "allowed_scope_types": ["self"],
                "required_confirmation": True,
                "ttl_seconds": 900,
                "max_calls": 1,
                "max_runtime_seconds": 300,
            },
            {
                "rule_id": "client_approve",
                "capability": "expense.approve@1.0.0",
                "required_permissions": [],
                "allowed_scope_types": ["self"],
                "required_approval": True,
                "ttl_seconds": 900,
                "max_calls": 1,
                "max_runtime_seconds": 300,
            },
        ]
    )
    return rules


@pytest.fixture(scope="module")
def _schema(test_db_url: str) -> None:
    command.upgrade(alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str, _schema: None) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def world(engine: Engine) -> Iterator[World]:
    reset_task_state(engine)
    session = Session(engine)
    try:
        seed_permission_catalog(session)
        org: Org = make_org(session)
        # The API resolves the approver from the directory; make the manager
        # relationship explicit so an approval-requiring policy can open.
        org.employee.manager_id = org.manager.id
        session.flush()
        policy: PolicyContext = publish_policy(
            session,
            org,
            policy_set_id=DEFAULT_POLICY_SET_ID,
            rules=_rules(),
        )
        session.commit()
        yield World(
            org=org,
            policy=policy,
            employee_id=org.employee.id,
            manager_id=org.manager.id,
            outsider_id=org.auditor.id,
            finance_id=org.finance.id,
        )
    finally:
        session.close()
        reset_task_state(engine)


@pytest.fixture()
def api_token() -> str:
    return TEST_TOKEN


@pytest.fixture()
def auth(api_token: str):
    def _auth(employee_id: int, *, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_token if token is None else token}",
            "X-Employee-Id": str(employee_id),
        }

    return _auth


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, api_token: str) -> Iterator[TestClient]:
    monkeypatch.setenv(TOKEN_ENV, api_token)
    from app.api.app import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch, api_token: str):
    monkeypatch.setenv(TOKEN_ENV, api_token)
    from app.api.app import create_app

    return create_app()


@pytest.fixture()
async def live_app(app):
    """Start the application lifespan exactly once for multi-session tests."""
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"
