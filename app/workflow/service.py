"""流程审批服务层：幂等发起、事务性 Outbox、SKIP LOCKED 抢占、失败退避与事件处理。

本模块把 ``templates/PLAN.md`` §8/§12 与 ``docs/PROJECT-CHARTER.md`` D5
（事务性 Outbox + Worker）落成**确定性的后端服务**：

* **事务性 Outbox**：业务变更（发起审批、审批决定）与 Outbox 事件在**同一
  ``session``** 内写入，调用方一次 ``commit`` 原子落库——不会出现「业务成功但事件
  丢失」或反之（D5；PLAN §8「审批、财务同步和通知通过事务及 Outbox 保证状态一致
  性」）；
* **幂等入队**：``enqueue_event`` 以 ``idempotency_key`` 去重，已存在则**直接返回
  既有行**，绝不新建（PLAN §12「重复事件、断线和重试不会重复创建业务记录」）；
* **并发抢占**：``claim_batch`` 用 ``SELECT ... FOR UPDATE SKIP LOCKED`` 抢占
  ``pending`` 且 ``available_at <= now()`` 的事件，多 Worker 并发互不阻塞、也不
  重复消费同一条；
* **失败退避**：``mark_failed`` 递增 ``attempt_count``，未达 ``MAX_ATTEMPTS`` 时
  回到 ``pending`` 并把 ``available_at`` 推到未来（指数退避，上限 300 秒），达到
  上限则置 ``failed``；
* **幂等处理**：``process_event`` 对 ``approval.requested`` 以
  ``dedup_key = f"{event_type}:{idempotency_key}"`` 去重创建通知，重复处理不产生
  第二条。

事务边界由调用方控制：本模块所有函数只 ``flush``，**绝不 ``commit``**（与
``app.audit.service.record_event``、``app.expense.service`` 一致），使业务变更、
审计与 Outbox 写入处于同一事务。

幂等键约定：

* 发起审批的步骤事件：``f"{workflow_type}:{business_type}:{business_id}:step1"``；
* 审批决定事件：``f"approval.decided:{step_id}:{decision}"``；
* 通知去重键：``f"{event.event_type}:{event.idempotency_key}"``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workflow.models import (
    ApprovalStep,
    Notification,
    OutboxEvent,
    WorkflowInstance,
)

__all__ = [
    "InvalidDecisionError",
    "MAX_ATTEMPTS",
    "StepAlreadyDecidedError",
    "WorkflowError",
    "WorkflowInstanceNotFoundError",
    "claim_batch",
    "enqueue_event",
    "mark_failed",
    "mark_published",
    "process_event",
    "record_decision",
    "start_approval",
]

#: 事件处理的最大尝试次数；达到后 ``mark_failed`` 置 ``failed``（不再重试）。
MAX_ATTEMPTS = 5

#: 退避上限（秒）：``available_at = now() + min(2**attempt_count, 300)``。
MAX_BACKOFF_SECONDS = 300

#: 审批决定允许的取值。
ALLOWED_DECISIONS = ("approve", "reject", "return")

#: 决定 → 步骤状态。
_DECISION_STEP_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "return": "returned",
}

#: 决定 → 实例状态。
_DECISION_INSTANCE_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "return": "in_progress",
}


class WorkflowError(Exception):
    """流程审批服务层错误基类。"""


class WorkflowInstanceNotFoundError(WorkflowError):
    """指定的审批实例不存在。"""

    def __init__(self, instance_id: int) -> None:
        self.instance_id = instance_id
        super().__init__(f"workflow instance {instance_id!r} not found")


class InvalidDecisionError(WorkflowError):
    """``decision`` 不在允许集合（``approve`` / ``reject`` / ``return``）内。"""

    def __init__(self, decision: str) -> None:
        self.decision = decision
        super().__init__(f"invalid decision {decision!r}")


class StepAlreadyDecidedError(WorkflowError):
    """当前实例已无 ``pending`` 步骤：不得对已决定的步骤重复决定。"""

    def __init__(self, instance_id: int) -> None:
        self.instance_id = instance_id
        super().__init__(
            f"workflow instance {instance_id!r} has no pending step to decide"
        )


# --- 内部辅助 -----------------------------------------------------------------


def _existing_event(session: Session, idempotency_key: str) -> OutboxEvent | None:
    """按 ``idempotency_key`` 查既有事件行；无则返回 ``None``。"""
    return session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.idempotency_key == idempotency_key
        )
    )


# --- Outbox：入队 / 抢占 / 标记 -------------------------------------------------


def enqueue_event(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    """幂等写入一条 Outbox 事件并返回它（不 commit）。

    **幂等**：若 ``idempotency_key`` 已存在，**直接返回既有行**，不新建、不覆盖
    （PLAN §12「重复事件、断线和重试不会重复创建业务记录」）。
    """
    existing = _existing_event(session, idempotency_key)
    if existing is not None:
        return existing

    event = OutboxEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        idempotency_key=idempotency_key,
        payload=payload if payload is not None else {},
        status="pending",
    )
    session.add(event)
    session.flush()
    return event


def claim_batch(session: Session, *, limit: int = 10) -> list[OutboxEvent]:
    """抢占一批可处理事件：``pending`` 且 ``available_at <= now()``。

    用 ``ORDER BY id`` + ``FOR UPDATE SKIP LOCKED`` 选取 ``limit`` 条，并**在同一
    事务内**把它们置为 ``processing`` 后 ``flush``；返回被抢占的事件列表。

    ``SKIP LOCKED`` 让并发的多个 Worker 跳过已被他人锁定的行，互不阻塞、也不会
    重复消费同一条事件。锁在调用方 ``commit``/``rollback`` 时释放。
    """
    stmt = (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == "pending",
            OutboxEvent.available_at <= datetime.now(UTC),
        )
        .order_by(OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    events = list(session.scalars(stmt))
    for event in events:
        event.status = "processing"
    session.flush()
    return events


def mark_published(session: Session, event: OutboxEvent) -> None:
    """标记事件已成功发布：``status='published'``、``published_at=now()``。"""
    event.status = "published"
    event.published_at = datetime.now(UTC)
    session.flush()


def mark_failed(session: Session, event: OutboxEvent, error: str) -> None:
    """标记一次处理失败（不 commit）。

    递增 ``attempt_count``，写入 ``last_error``：

    * 未达 ``MAX_ATTEMPTS`` → ``status='pending'``，``available_at =
      now() + min(2**attempt_count, 300)`` 秒（指数退避，供 Worker 稍后重试）；
    * 达到 ``MAX_ATTEMPTS`` → ``status='failed'``（不再重试）。
    """
    event.attempt_count = event.attempt_count + 1
    event.last_error = error

    if event.attempt_count >= MAX_ATTEMPTS:
        event.status = "failed"
    else:
        event.status = "pending"
        backoff = min(2 ** event.attempt_count, MAX_BACKOFF_SECONDS)
        event.available_at = datetime.now(UTC) + timedelta(seconds=backoff)
    session.flush()


def process_event(session: Session, event: OutboxEvent) -> Notification | None:
    """处理一条 Outbox 事件，返回产生的通知（无则 ``None``）。

    目前只处理 ``event_type='approval.requested'``：取 ``payload['approver_id']``
    作为收件人，创建一条 ``channel_type='weixin'`` 的通知，
    ``dedup_key = f"{event.event_type}:{event.idempotency_key}"``。

    **幂等**：若该 ``dedup_key`` 已存在，**直接返回既有通知**，不重复创建（PLAN
    §12）。其它 ``event_type`` 返回 ``None``。
    """
    if event.event_type != "approval.requested":
        return None

    approver_id = event.payload["approver_id"]
    dedup_key = f"{event.event_type}:{event.idempotency_key}"

    existing = session.scalar(
        select(Notification).where(Notification.dedup_key == dedup_key)
    )
    if existing is not None:
        return existing

    notification = Notification(
        recipient_id=approver_id,
        channel_type="weixin",
        event_type=event.event_type,
        dedup_key=dedup_key,
        payload=dict(event.payload),
        delivery_status="pending",
    )
    session.add(notification)
    session.flush()
    return notification


# --- 审批流程：发起 / 决定 ------------------------------------------------------


def start_approval(
    session: Session,
    *,
    business_type: str,
    business_id: int,
    workflow_type: str,
    approver_id: int,
) -> WorkflowInstance:
    """幂等发起一条审批流程（不 commit）。

    若 ``(business_type, business_id, workflow_type)`` 已存在实例，**直接返回既有
    实例**，不重复创建审批步骤、也不重复入队事件（PLAN §12「重复消息不会重复创建
    报销单/审批任务」）。

    否则创建：实例（``status='in_progress'``）+ 第 1 步（``pending``，指定
    ``approver_id``）+ 一条 ``approval.requested`` 事件（``idempotency_key =
    f"{workflow_type}:{business_type}:{business_id}:step1"``，``payload`` 含
    ``approver_id``）。
    """
    existing = session.scalar(
        select(WorkflowInstance).where(
            WorkflowInstance.business_type == business_type,
            WorkflowInstance.business_id == business_id,
            WorkflowInstance.workflow_type == workflow_type,
        )
    )
    if existing is not None:
        return existing

    instance = WorkflowInstance(
        business_type=business_type,
        business_id=business_id,
        workflow_type=workflow_type,
        status="in_progress",
    )
    session.add(instance)
    session.flush()

    step = ApprovalStep(
        instance_id=instance.id,
        step_no=1,
        approver_id=approver_id,
        status="pending",
    )
    session.add(step)
    session.flush()

    enqueue_event(
        session,
        event_type="approval.requested",
        aggregate_type="workflow_instance",
        aggregate_id=str(instance.id),
        idempotency_key=(
            f"{workflow_type}:{business_type}:{business_id}:step1"
        ),
        payload={"approver_id": approver_id, "step_id": step.id},
    )
    return instance


def record_decision(
    session: Session,
    *,
    instance_id: int,
    decision: str,
    actor_id: int,
) -> WorkflowInstance:
    """记录一次审批决定并推进实例状态（不 commit）。

    ``decision`` 限 ``approve`` / ``reject`` / ``return``。只处理**当前 pending
    的那一步**（``step_no`` 最小且 ``status='pending'``）：更新步骤状态
    （``approved`` / ``rejected`` / ``returned``）与 ``acted_at``；实例状态
    ``approve→approved``、``reject→rejected``、``return→in_progress``；入队一条
    ``approval.decided`` 事件（``idempotency_key`` 含 ``step_id`` 与 ``decision``）。

    **不得重复决定**：若当前已无 ``pending`` 步骤（该步已非 pending），抛
    ``StepAlreadyDecidedError``，且不产生任何写入。
    """
    if decision not in ALLOWED_DECISIONS:
        raise InvalidDecisionError(decision)

    instance = session.get(WorkflowInstance, instance_id)
    if instance is None:
        raise WorkflowInstanceNotFoundError(instance_id)

    step = session.scalar(
        select(ApprovalStep)
        .where(
            ApprovalStep.instance_id == instance_id,
            ApprovalStep.status == "pending",
        )
        .order_by(ApprovalStep.step_no)
        .limit(1)
    )
    if step is None:
        raise StepAlreadyDecidedError(instance_id)

    step.status = _DECISION_STEP_STATUS[decision]
    step.acted_at = datetime.now(UTC)
    instance.status = _DECISION_INSTANCE_STATUS[decision]

    enqueue_event(
        session,
        event_type="approval.decided",
        aggregate_type="workflow_instance",
        aggregate_id=str(instance.id),
        idempotency_key=f"approval.decided:{step.id}:{decision}",
        payload={
            "instance_id": instance.id,
            "step_id": step.id,
            "decision": decision,
            "actor_id": actor_id,
        },
    )
    session.flush()
    return instance
