"""B1 Task Intake 的真实 PostgreSQL 契约测试（由 Master 在测试机运行）。

覆盖需要真实绑定表 / 任务核心的部分：

* 身份未绑定 / 已撤销 / 未验证 / 员工非 active / 跨公司 -> 拒绝且不建任务；
* 首次提交 -> 一个任务，``execution_mode`` 只来自 ``ActionSpec.final_mode``，
  requester/owner/executor 都是服务端解析出的同一名员工，approver 为空；
* 同一事件重复投递 -> ``duplicate`` 且返回同一 task，任务数不增；
* 不同事件 -> 新任务；
* Bridge ``handle_action`` 复用 Task Intake 去重。

Implementer 只做 ``--collect-only``；真实库运行由 Master 执行。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Employee
from app.identity.models import EmployeeChannelBinding
from app.identity.seed import seed_permission_catalog
from app.integrations.wechat.bridge import WeChatBridge
from app.integrations.wechat.config import WeChatBridgeConfig
from app.tasks.intake import (
    INTAKE_ACCEPTED,
    INTAKE_DUPLICATE,
    INTAKE_REJECTED,
    ActionRegistry,
    ActionSpec,
    submit_action,
)
from app.tasks.models import Task
from app.tests.governance.helpers import (
    Org,
    PolicyContext,
    alembic_config,
    make_org,
    publish_policy,
    scope,
)
from app.tests.tasks.conftest import reset_task_state

TEST_CAPABILITY = "expense.read@1.0.0"
TEST_USER = "wx-b1-user-1"


def _rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "wechat_b1_test_read",
            "capability": TEST_CAPABILITY,
            "required_permissions": [],
            "allowed_scope_types": ["self"],
            "allowed_execution_modes": ["advisory"],
            "ttl_seconds": 900,
            "max_calls": 5,
            "max_runtime_seconds": 300,
        }
    ]


def _goal(summary: str | None = None) -> dict[str, Any]:
    return {
        "summary": summary or f"WeChat B1 test read {uuid.uuid4().hex[:8]}",
        "completion_criteria": [
            {
                "criterion_id": f"crit_wechat_{uuid.uuid4().hex[:8]}",
                "type": "business_state_equals",
                "verifier": {
                    "verifier_id": "ver_wechat_0001",
                    "version": "1.0.0",
                },
                "resource_ref": "res_wechat_0001",
                "expected": "read",
                "evidence_ref": "evidence:wechat_0001",
            }
        ],
        "required_inputs": [],
        "out_of_scope": [],
    }


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    command.upgrade(alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    reset_task_state(engine)
    with Session(engine) as sess:
        try:
            seed_permission_catalog(sess)
            sess.commit()
            yield sess
        finally:
            sess.rollback()
            reset_task_state(engine)


@pytest.fixture()
def org(session: Session) -> Org:
    built = make_org(session)
    session.commit()
    return built


@pytest.fixture()
def policy(session: Session, org: Org) -> PolicyContext:
    published = publish_policy(session, org, rules=_rules())
    session.commit()
    return published


def _registry(policy: PolicyContext) -> ActionRegistry:
    return ActionRegistry(
        (
            ActionSpec(
                action_id="test_read",
                capability=TEST_CAPABILITY,
                final_mode="advisory",
                policy_set=policy.policy_set_id,
                allowed_scope=scope(),
            ),
        )
    )


def _bind(
    engine: Engine,
    *,
    employee_id: int,
    external_user_id: str = TEST_USER,
    status: str = "active",
    verified: bool = True,
) -> None:
    from datetime import datetime, timezone

    with Session(engine) as other:
        other.add(
            EmployeeChannelBinding(
                employee_id=employee_id,
                channel_type="weixin",
                external_user_id=external_user_id,
                status=status,
                verified_at=datetime.now(timezone.utc) if verified else None,
            )
        )
        other.commit()


def _count_tasks(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Task)) or 0


# --- identity rejections -------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("unbound", "unbound"),
        ("revoked", "revoked"),
        ("unverified", "unverified"),
        ("employee_inactive", "employee_inactive"),
        ("company_mismatch", "company_mismatch"),
    ],
)
def test_identity_rejections_create_no_task(
    session: Session,
    engine: Engine,
    org: Org,
    policy: PolicyContext,
    setup: str,
    reason: str,
) -> None:
    expected_company_id = None
    if setup == "revoked":
        _bind(engine, employee_id=org.employee.id, status="revoked")
    elif setup == "unverified":
        _bind(engine, employee_id=org.employee.id, verified=False)
    elif setup == "employee_inactive":
        inactive = Employee(
            company_id=org.company.id,
            employee_no="E-b1-inactive",
            status="inactive",
        )
        session.add(inactive)
        session.flush()
        session.commit()
        _bind(engine, employee_id=inactive.id)
    elif setup == "company_mismatch":
        _bind(engine, employee_id=org.employee.id)
        expected_company_id = org.company.id + 10_000

    result = submit_action(
        session,
        external_user_id=TEST_USER,
        external_message_id=f"{TEST_USER}:1700000000000",
        action_id="test_read",
        payload={"goal": _goal()},
        expected_company_id=expected_company_id,
        registry=_registry(policy),
    )

    assert result.status == INTAKE_REJECTED
    assert result.reason == reason
    assert result.task_id is None
    assert _count_tasks(session) == 0


# --- accepted / mode / ownership ----------------------------------------------


def test_accepted_task_uses_spec_mode_and_resolved_employee(
    session: Session,
    engine: Engine,
    org: Org,
    policy: PolicyContext,
) -> None:
    _bind(engine, employee_id=org.employee.id)

    result = submit_action(
        session,
        external_user_id=TEST_USER,
        external_message_id=f"{TEST_USER}:1700000000000",
        action_id="test_read",
        payload={"goal": _goal()},
        registry=_registry(policy),
    )
    assert result.status == INTAKE_ACCEPTED
    assert result.task_id is not None
    session.commit()

    task = session.scalar(select(Task).where(Task.task_id == result.task_id))
    assert task is not None
    assert task.execution_mode == "advisory"
    assert task.requested_capabilities == [TEST_CAPABILITY]
    assert task.policy_set_id == policy.policy_set_id
    assert task.requester_employee_id == org.employee.id
    assert task.owner_employee_id == org.employee.id
    assert task.executor_employee_id == org.employee.id
    assert task.requester_principal_id == task.owner_principal_id == task.executor_principal_id
    assert task.approver_principal_id is None
    assert task.approver_employee_id is None


# --- dedupe --------------------------------------------------------------------


def test_same_event_returns_same_task_and_does_not_create_another(
    session: Session,
    engine: Engine,
    org: Org,
    policy: PolicyContext,
) -> None:
    _bind(engine, employee_id=org.employee.id)
    registry = _registry(policy)
    event_id = f"{TEST_USER}:1700000000000"
    # 同一事件的重投必须携带同一份 canonical request；随机 criterion_id 会被
    # 任务核心判为 idempotency conflict，而不是 duplicate。
    same_goal = _goal("Same event goal")

    first = submit_action(
        session,
        external_user_id=TEST_USER,
        external_message_id=event_id,
        action_id="test_read",
        payload={"goal": same_goal},
        registry=registry,
    )
    assert first.status == INTAKE_ACCEPTED
    session.commit()
    assert _count_tasks(session) == 1

    second = submit_action(
        session,
        external_user_id=TEST_USER,
        external_message_id=event_id,
        action_id="test_read",
        payload={"goal": same_goal},
        registry=registry,
    )
    session.commit()

    assert second.status == INTAKE_DUPLICATE
    assert second.task_id == first.task_id
    assert _count_tasks(session) == 1


def test_different_event_creates_a_new_task(
    session: Session,
    engine: Engine,
    org: Org,
    policy: PolicyContext,
) -> None:
    _bind(engine, employee_id=org.employee.id)
    registry = _registry(policy)

    first = submit_action(
        session,
        external_user_id=TEST_USER,
        external_message_id=f"{TEST_USER}:1700000000000",
        action_id="test_read",
        payload={"goal": _goal("First event goal")},
        registry=registry,
    )
    session.commit()
    second = submit_action(
        session,
        external_user_id=TEST_USER,
        external_message_id=f"{TEST_USER}:1700000000001",
        action_id="test_read",
        payload={"goal": _goal("Second event goal")},
        registry=registry,
    )
    session.commit()

    assert first.status == INTAKE_ACCEPTED
    assert second.status == INTAKE_ACCEPTED
    assert first.task_id != second.task_id
    assert _count_tasks(session) == 2


# --- bridge action path --------------------------------------------------------


class _NullSdk:
    def on_message(self, handler: Any) -> None:  # pragma: no cover - unused
        return None

    async def reply(self, message: Any, text: str) -> Any:  # pragma: no cover
        return None

    async def start(self) -> None:  # pragma: no cover
        return None

    def stop(self) -> None:  # pragma: no cover
        return None


def test_bridge_handle_action_uses_task_intake_dedupe(
    session: Session,
    engine: Engine,
    org: Org,
    policy: PolicyContext,
) -> None:
    from dataclasses import dataclass

    _bind(engine, employee_id=org.employee.id)
    config = WeChatBridgeConfig(
        credential_path=Path("/var/lib/ajaimgr-wechat/credentials.json"),
        company_id=org.company.id,
        action_registry=_registry(policy),
    )
    bridge = WeChatBridge(
        config=config,
        session_factory=lambda: Session(engine),
        sdk=_NullSdk(),  # type: ignore[arg-type]
    )

    @dataclass
    class Message:
        raw: dict[str, Any]
        text: str = "/action"

    message = Message(
        raw={"from_user_id": TEST_USER, "create_time_ms": 1_700_000_000_000}
    )
    same_goal = _goal("Bridge goal")
    first = bridge.handle_action(message, "test_read", payload={"goal": same_goal})
    second = bridge.handle_action(message, "test_read", payload={"goal": same_goal})

    assert first.status == INTAKE_ACCEPTED
    assert second.status == INTAKE_DUPLICATE
    assert second.task_id == first.task_id
    assert _count_tasks(session) == 1
