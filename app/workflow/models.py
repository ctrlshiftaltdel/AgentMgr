"""流程审批域核心数据模型：审批实例、审批步骤、事务性 Outbox 与通知队列。

列结构由 Master 在任务 W1-8 中统一固定，并对齐：

* ``templates/PLAN.md`` §8「工作流、收件和文件」表清单——
  ``workflow_instances`` / ``approval_steps`` / ``outbox_events`` /
  ``notifications``；
* ``templates/PLAN.md`` §8「数据完整性要求」——
  **「关键业务号、外部映射和幂等键设唯一约束」**、**「审批、财务同步和通知通过
  事务及 Outbox 保证状态一致性」**；
* ``templates/PLAN.md`` §12 全局发布验收清单——
  **「重复事件、断线和重试不会重复创建业务记录」**；
* ``docs/PROJECT-CHARTER.md`` D5——**事务性 Outbox 与 Worker 处理通知和外部
  同步（替代：直接同步调用 —— 会丢事件或产生重复）**。

关键不变量：

* 主键均为 ``BIGINT`` 自增，``created_at``/``updated_at`` 为
  ``timestamptz NOT NULL DEFAULT now()``；
* ``status`` 用 ``TEXT`` + ``CHECK`` 限定取值集合（不用数据库 ENUM，便于演进）；
* ``workflow_instances`` 以 ``(business_type, business_id, workflow_type)`` 唯一——
  同一业务的同一流程只有一份实例，重复发起不会重复创建审批任务
  （PLAN §12「重复消息不会重复创建报销单/审批任务」）；
* ``approval_steps`` 以 ``(instance_id, step_no)`` 唯一，随实例
  ``ON DELETE CASCADE`` 删除；``approver_id`` 外键指向 ``employees``；
* ``outbox_events.idempotency_key`` 全局唯一——幂等入队的落点；``payload`` 为
  ``JSONB NOT NULL DEFAULT '{}'::jsonb``；``attempt_count >= 0``；
* ``notifications.dedup_key`` 全局唯一——重复事件不会产生第二条通知；
  ``recipient_id`` 外键指向 ``employees``；``attempt_count >= 0``。

本层只承载数据与约束；事务边界、幂等、抢占与退避由 ``app.workflow.service``
以确定性程序驱动（PROJECT-CHARTER 强制原则 6）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

__all__ = [
    "ApprovalStep",
    "Notification",
    "OutboxEvent",
    "WorkflowInstance",
]


class WorkflowInstance(TimestampMixin, Base):
    """一条审批流程实例：同一业务的同一流程类型只有一份（幂等发起）。

    ``status`` 取值集合固定为 ``pending`` / ``in_progress`` / ``approved`` /
    ``rejected`` / ``canceled``；状态迁移由 ``app.workflow.service`` 的
    ``record_decision`` 驱动（确定性程序，强制原则 6）。
    """

    __tablename__ = "workflow_instances"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 业务对象类型（如 ``expense_claim``）；与 business_id 组成业务引用。
    business_type: Mapped[str] = mapped_column(Text, nullable=False)
    business_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workflow_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )

    __table_args__ = (
        # 幂等发起：同一业务对象的同一流程只有一份实例。
        UniqueConstraint(
            "business_type",
            "business_id",
            "workflow_type",
            name="uq_workflow_instances_business",
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'approved', 'rejected', "
            "'canceled')",
            name="status_allowed",
        ),
    )


class ApprovalStep(TimestampMixin, Base):
    """一条审批步骤：审批人、序号与决定状态。

    ``status`` 取值集合固定为 ``pending`` / ``approved`` / ``rejected`` /
    ``returned`` / ``skipped``；``(instance_id, step_no)`` 唯一；随实例
    ``ON DELETE CASCADE`` 删除。
    """

    __tablename__ = "approval_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflow_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "instance_id", "step_no", name="uq_approval_steps_instance_step_no"
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'returned', 'skipped')",
            name="status_allowed",
        ),
    )


class OutboxEvent(TimestampMixin, Base):
    """一条事务性 Outbox 事件：与业务变更同事务写入，由 Worker 异步消费。

    ``idempotency_key`` 全局唯一——重复入队直接返回既有行（PLAN §12 幂等）；
    ``status`` 取值集合固定为 ``pending`` / ``processing`` / ``published`` /
    ``failed``；``available_at`` 控制可抢占时间（失败退避）；索引
    ``(status, available_at)`` 服务 Worker 的批量抢占查询。
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    # TEXT：聚合 ID 不一定是数字（与 audit_events.resource_id 口径一致）。
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'failed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="attempt_count_non_negative"
        ),
        # Worker 批量抢占：按 (status, available_at) 过滤。
        Index(
            "ix_outbox_events_status_available_at",
            "status",
            "available_at",
        ),
    )


class Notification(TimestampMixin, Base):
    """一条待发送通知：由 Outbox 事件处理产生，``dedup_key`` 去重。

    ``dedup_key`` 全局唯一——同一事件重复处理不会产生第二条通知；
    ``delivery_status`` 取值集合固定为 ``pending`` / ``sent`` / ``failed``。
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recipient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'weixin'")
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    delivery_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "delivery_status IN ('pending', 'sent', 'failed')",
            name="delivery_status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="attempt_count_non_negative"
        ),
    )
