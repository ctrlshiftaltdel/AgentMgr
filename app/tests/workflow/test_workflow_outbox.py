"""W1-8 流程审批与事务性 Outbox 测试（跑在专用测试库 ``ajaimgr_test`` 上）。

覆盖任务第四步要求的八项（对齐 ``templates/PLAN.md`` §8/§12 与
``docs/PROJECT-CHARTER.md`` D5）：

1. **幂等发起**：``start_approval`` 建实例 + 第 1 步 + 恰 1 条事件；同一业务重复
   调用返回同一实例且不新增步骤/事件；
2. **幂等入队**：``enqueue_event`` 同一 ``idempotency_key`` 重复入队只有 1 行，
   且不覆盖既有 ``payload``；
3. **抢占**：``claim_batch`` 抢占事件并置 ``processing``；再次 ``claim_batch`` 不
   返回已抢占的事件；``available_at`` 在未来的事件不被抢占；
4. **并发 SKIP LOCKED**：会话 A 抢占后不提交，会话 B（真实第二条连接）再抢占
   得不到同一条事件；
5. **失败退避**：``mark_failed`` 递增 ``attempt_count``、回到 ``pending``、``available_at``
   推到未来；连续失败到 ``MAX_ATTEMPTS`` 后置 ``failed``；
6. **幂等处理**：``process_event`` 创建通知；重复处理不产生第二条通知；未知
   ``event_type`` 返回 ``None``；
7. **审批决定**：``record_decision`` 成功（步骤状态 + ``acted_at`` + 实例状态 +
   事件）；对同一步重复决定抛 ``StepAlreadyDecidedError``；非法决定抛
   ``InvalidDecisionError``；
8. **CHECK 约束**：四张表的非法 ``status`` / 负数 ``attempt_count`` 被数据库拒绝。

每个测试自行造 company/department/employee，session fixture 在结束后回滚未提交的
服务写入并删除业务行（``outbox_events`` / ``notifications`` / ``approval_steps`` /
``workflow_instances`` 无只增不改触发器，可直接删除）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.workflow.models import (
    ApprovalStep,
    Notification,
    OutboxEvent,
    WorkflowInstance,
)
from app.workflow.service import (
    MAX_ATTEMPTS,
    InvalidDecisionError,
    StepAlreadyDecidedError,
    claim_batch,
    enqueue_event,
    mark_failed,
    mark_published,
    process_event,
    record_decision,
    start_approval,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head（含 0007 workflow/outbox）。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """删除这些测试创建的全部业务行（先子后父）。

    ``outbox_events`` / ``notifications`` / ``approval_steps`` /
    ``workflow_instances`` 都没有只增不改触发器，可以直接删除。
    """
    session.execute(delete(Notification))
    session.execute(delete(OutboxEvent))
    session.execute(delete(ApprovalStep))
    session.execute(delete(WorkflowInstance))
    session.execute(delete(Employee))
    session.execute(delete(Department))
    session.execute(delete(Company))


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


@dataclass
class World:
    """一套测试组织：公司、部门、发起人与审批人。"""

    company: Company
    requester: Employee
    approver: Employee


@pytest.fixture()
def world(session: Session) -> World:
    """造出并提交一套组织数据（提交后服务写入可独立回滚）。"""
    company = Company(name="W1-8 流程审批")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name="审批部")
    session.add(department)
    session.flush()
    requester = Employee(
        company_id=company.id, department_id=department.id, employee_no="E-requester"
    )
    approver = Employee(
        company_id=company.id, department_id=department.id, employee_no="E-approver"
    )
    session.add_all([requester, approver])
    session.flush()
    session.commit()
    return World(company=company, requester=requester, approver=approver)


def _events(session: Session, instance_id: int) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.aggregate_id == str(instance_id))
            .order_by(OutboxEvent.id)
        )
    )


def _steps(session: Session, instance_id: int) -> list[ApprovalStep]:
    return list(
        session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.instance_id == instance_id)
            .order_by(ApprovalStep.step_no)
        )
    )


# --- 1. 幂等发起 --------------------------------------------------------------


def test_start_approval_creates_instance_step_and_one_event(
    session: Session, world: World
) -> None:
    """发起审批建实例 + 第 1 步 + 恰 1 条事件；重复发起幂等。"""
    instance = start_approval(
        session,
        business_type="expense_claim",
        business_id=1001,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )
    assert instance.status == "in_progress"
    assert instance.business_type == "expense_claim"
    assert instance.business_id == 1001
    assert instance.workflow_type == "expense_approval"

    steps = _steps(session, instance.id)
    assert len(steps) == 1
    assert steps[0].step_no == 1
    assert steps[0].status == "pending"
    assert steps[0].approver_id == world.approver.id
    assert steps[0].acted_at is None

    events = _events(session, instance.id)
    assert len(events) == 1
    assert events[0].event_type == "approval.requested"
    assert events[0].aggregate_type == "workflow_instance"
    assert events[0].status == "pending"
    assert (
        events[0].idempotency_key
        == "expense_approval:expense_claim:1001:step1"
    )
    assert events[0].payload["approver_id"] == world.approver.id

    # 幂等：同一业务重复发起 → 同一实例，且不新增步骤/事件。
    again = start_approval(
        session,
        business_type="expense_claim",
        business_id=1001,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )
    assert again.id == instance.id
    assert len(_steps(session, instance.id)) == 1
    assert len(_events(session, instance.id)) == 1


# --- 2. 幂等入队 --------------------------------------------------------------


def test_enqueue_event_is_idempotent(session: Session, world: World) -> None:
    """同一 idempotency_key 重复入队只有 1 行，且不覆盖既有 payload。"""
    first = enqueue_event(
        session,
        event_type="demo",
        aggregate_type="thing",
        aggregate_id="1",
        idempotency_key="K-DUP",
        payload={"n": 1},
    )
    second = enqueue_event(
        session,
        event_type="demo",
        aggregate_type="thing",
        aggregate_id="1",
        idempotency_key="K-DUP",
        payload={"n": 999},
    )
    assert second.id == first.id
    assert second.payload == {"n": 1}

    count = session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.idempotency_key == "K-DUP")
    )
    assert count == 1


# --- 3. 抢占 ------------------------------------------------------------------


def test_claim_batch_claims_and_does_not_reclaim(
    session: Session, world: World
) -> None:
    """claim_batch 抢占 pending 事件；再次 claim 不返回已抢占的事件。"""
    e1 = enqueue_event(
        session,
        event_type="demo",
        aggregate_type="thing",
        aggregate_id="1",
        idempotency_key="K-CLAIM-1",
    )
    e2 = enqueue_event(
        session,
        event_type="demo",
        aggregate_type="thing",
        aggregate_id="2",
        idempotency_key="K-CLAIM-2",
    )

    batch = claim_batch(session, limit=10)
    assert {e.id for e in batch} == {e1.id, e2.id}
    assert all(e.status == "processing" for e in batch)

    again = claim_batch(session, limit=10)
    assert again == []
    assert e1.status == "processing"
    assert e2.status == "processing"


def test_claim_batch_skips_future_available_at(
    session: Session, world: World
) -> None:
    """available_at 在未来的事件不被抢占。"""
    event = enqueue_event(
        session,
        event_type="demo",
        aggregate_type="thing",
        aggregate_id="1",
        idempotency_key="K-FUTURE",
    )
    event.available_at = datetime.now(UTC) + timedelta(seconds=60)
    session.flush()

    assert claim_batch(session, limit=10) == []
    assert event.status == "pending"


# --- 4. 并发 SKIP LOCKED ------------------------------------------------------


def test_claim_batch_skip_locked_across_connections(
    engine: Engine, world: World
) -> None:
    """会话 A 抢占后不提交，会话 B（第二条真实连接）抢占不到同一条。

    ``world`` 依赖 ``session`` fixture，其 teardown 会删除本测试提交的事件行。
    """
    with Session(engine) as setup:
        e1 = enqueue_event(
            setup,
            event_type="demo",
            aggregate_type="thing",
            aggregate_id="1",
            idempotency_key="K-LOCK-1",
        )
        e2 = enqueue_event(
            setup,
            event_type="demo",
            aggregate_type="thing",
            aggregate_id="2",
            idempotency_key="K-LOCK-2",
        )
        locked_ids = {e1.id, e2.id}
        setup.commit()

    session_a = Session(engine)
    session_b = Session(engine)
    try:
        batch_a = claim_batch(session_a, limit=10)
        assert {e.id for e in batch_a} == locked_ids  # A 抢到并持有行锁

        # A 未提交：B 在自己的快照里仍看到 pending，但行已被锁 → SKIP LOCKED 跳过。
        batch_b = claim_batch(session_b, limit=10)
        assert batch_b == []
        assert locked_ids.isdisjoint({e.id for e in batch_b})
    finally:
        session_a.rollback()
        session_b.rollback()
        session_a.close()
        session_b.close()

    # A 回滚后事件回到 pending，可被重新抢占。
    with Session(engine) as verify:
        reclaimed = claim_batch(verify, limit=10)
        assert {e.id for e in reclaimed} == locked_ids
        verify.rollback()


# --- 5. 失败退避 --------------------------------------------------------------


def test_mark_failed_backs_off_then_fails_after_max_attempts(
    session: Session, world: World
) -> None:
    """mark_failed 递增 attempt_count、回 pending、available_at 在未来；到上限置 failed。"""
    event = enqueue_event(
        session,
        event_type="demo",
        aggregate_type="thing",
        aggregate_id="1",
        idempotency_key="K-FAIL",
    )
    before = datetime.now(UTC)

    mark_failed(session, event, "boom-1")
    assert event.attempt_count == 1
    assert event.status == "pending"
    assert event.last_error == "boom-1"
    assert event.available_at > before  # 退避到未来

    # 未达上限前一直是 pending 且 attempt_count 递增。
    for attempt in range(2, MAX_ATTEMPTS):
        mark_failed(session, event, f"boom-{attempt}")
        assert event.attempt_count == attempt
        assert event.status == "pending"
        assert event.available_at > datetime.now(UTC)

    # 第 MAX_ATTEMPTS 次失败 → failed（不再重试）。
    mark_failed(session, event, "boom-final")
    assert event.attempt_count == MAX_ATTEMPTS
    assert event.status == "failed"
    assert event.last_error == "boom-final"


def test_mark_published_sets_status_and_timestamp(
    session: Session, world: World
) -> None:
    """mark_published 置 published 并写 published_at。"""
    event = enqueue_event(
        session,
        event_type="demo",
        aggregate_type="thing",
        aggregate_id="1",
        idempotency_key="K-PUB",
    )
    mark_published(session, event)
    assert event.status == "published"
    assert event.published_at is not None


# --- 6. 幂等处理 --------------------------------------------------------------


def test_process_event_creates_notification_once(
    session: Session, world: World
) -> None:
    """process_event 创建通知；重复处理不产生第二条；未知类型返回 None。"""
    instance = start_approval(
        session,
        business_type="expense_claim",
        business_id=2002,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )
    event = _events(session, instance.id)[0]

    notification = process_event(session, event)
    assert notification is not None
    assert notification.recipient_id == world.approver.id
    assert notification.channel_type == "weixin"
    assert notification.event_type == "approval.requested"
    assert (
        notification.dedup_key
        == f"approval.requested:{event.idempotency_key}"
    )
    assert notification.delivery_status == "pending"

    # 重复处理同一事件 → 返回既有通知，不新建。
    again = process_event(session, event)
    assert again is not None
    assert again.id == notification.id
    count = session.scalar(select(func.count()).select_from(Notification))
    assert count == 1

    # 未知 event_type → None。
    other = enqueue_event(
        session,
        event_type="something.else",
        aggregate_type="thing",
        aggregate_id="9",
        idempotency_key="K-OTHER",
    )
    assert process_event(session, other) is None


# --- 7. 审批决定 --------------------------------------------------------------


def test_record_decision_approve_and_repeat_raises(
    session: Session, world: World
) -> None:
    """record_decision 成功推进状态；对同一步重复决定抛异常。"""
    instance = start_approval(
        session,
        business_type="expense_claim",
        business_id=3003,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )

    updated = record_decision(
        session,
        instance_id=instance.id,
        decision="approve",
        actor_id=world.approver.id,
    )
    assert updated.status == "approved"

    step = _steps(session, instance.id)[0]
    assert step.status == "approved"
    assert step.acted_at is not None

    decided = session.scalar(
        select(OutboxEvent).where(OutboxEvent.event_type == "approval.decided")
    )
    assert decided is not None
    assert str(step.id) in decided.idempotency_key
    assert "approve" in decided.idempotency_key
    assert decided.payload["decision"] == "approve"

    # 重复决定（该步已非 pending）→ 抛异常，且不产生新事件。
    with pytest.raises(StepAlreadyDecidedError):
        record_decision(
            session,
            instance_id=instance.id,
            decision="approve",
            actor_id=world.approver.id,
        )

    decided_count = session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.event_type == "approval.decided")
    )
    assert decided_count == 1

    # 非法决定值。
    with pytest.raises(InvalidDecisionError):
        record_decision(
            session,
            instance_id=instance.id,
            decision="maybe",
            actor_id=world.approver.id,
        )


def test_record_decision_reject_and_return(session: Session, world: World) -> None:
    """reject → 实例 rejected；return → 实例回到 in_progress。"""
    rejected = start_approval(
        session,
        business_type="expense_claim",
        business_id=4004,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )
    record_decision(
        session,
        instance_id=rejected.id,
        decision="reject",
        actor_id=world.approver.id,
    )
    assert rejected.status == "rejected"
    assert _steps(session, rejected.id)[0].status == "rejected"

    returned = start_approval(
        session,
        business_type="expense_claim",
        business_id=5005,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )
    record_decision(
        session,
        instance_id=returned.id,
        decision="return",
        actor_id=world.approver.id,
    )
    assert returned.status == "in_progress"
    assert _steps(session, returned.id)[0].status == "returned"


# --- 8. CHECK 约束 ------------------------------------------------------------


def test_status_check_constraints_reject_invalid(
    session: Session, world: World
) -> None:
    """四张表的非法 status / 负数 attempt_count 被数据库 CHECK 拒绝。"""
    # workflow_instances.status
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO workflow_instances "
                    "(business_type, business_id, workflow_type, status) "
                    "VALUES ('b', 1, 'w', 'bogus')"
                )
            )

    # outbox_events.status
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_type, aggregate_type, aggregate_id, "
                    "idempotency_key, status) "
                    "VALUES ('t', 'a', '1', 'CK-OUTBOX-STATUS', 'bogus')"
                )
            )

    # outbox_events.attempt_count >= 0
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_type, aggregate_type, aggregate_id, "
                    "idempotency_key, attempt_count) "
                    "VALUES ('t', 'a', '1', 'CK-OUTBOX-NEG', -1)"
                )
            )

    # approval_steps.status（需要合法 instance_id / approver_id）
    instance = start_approval(
        session,
        business_type="expense_claim",
        business_id=6006,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO approval_steps "
                    "(instance_id, step_no, approver_id, status) "
                    f"VALUES ({instance.id}, 99, {world.approver.id}, 'bogus')"
                )
            )

    # notifications.delivery_status
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO notifications "
                    "(recipient_id, event_type, dedup_key, delivery_status) "
                    f"VALUES ({world.approver.id}, 't', 'CK-NOTIF-STATUS', "
                    "'bogus')"
                )
            )


def test_unique_constraints_reject_duplicates(
    session: Session, world: World
) -> None:
    """workflow_instances 的业务唯一键与 outbox_events 的幂等键在库层唯一。"""
    start_approval(
        session,
        business_type="expense_claim",
        business_id=7007,
        workflow_type="expense_approval",
        approver_id=world.approver.id,
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO workflow_instances "
                    "(business_type, business_id, workflow_type, status) "
                    "VALUES ('expense_claim', 7007, 'expense_approval', "
                    "'in_progress')"
                )
            )

    enqueue_event(
        session,
        event_type="t",
        aggregate_type="a",
        aggregate_id="1",
        idempotency_key="K-UNIQUE-DUP",
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_type, aggregate_type, aggregate_id, "
                    "idempotency_key) "
                    "VALUES ('t', 'a', '1', 'K-UNIQUE-DUP')"
                )
            )
