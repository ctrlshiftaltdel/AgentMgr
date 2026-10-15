"""审计事件模型：``audit_events``（只增不改）。

列定义由 Master 统一固定，对齐 ``templates/PLAN.md`` §8 与 §9：

* ``id`` BIGINT 自增主键；
* ``actor_id`` 关联 ``employees(id)``，**ON DELETE SET NULL**：审计必须比人员记录
  活得更久，因此绝不能使用 ``CASCADE``（删除员工不得抹掉其审计历史）；
* ``action`` / ``resource_type`` 必填；``resource_id`` 用 ``TEXT``（资源 ID 可能
  不是数字）；``request_id`` 关联同一次调用；``details`` 为结构化 JSONB；
* ``created_at`` 为 ``timestamptz NOT NULL DEFAULT now()``。

只增不改由**数据库层**强制（迁移 ``0004_audit_events`` 的 BEFORE UPDATE OR DELETE
触发器），不依赖应用层自觉；本模型不提供任何更新/删除路径。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditEvent(Base):
    """一条不可变、仅追加的审计事件。"""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 审计比人员记录活得久：删除员工只把 actor_id 置 NULL，绝不级联删除审计行。
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    # TEXT 而非整数：资源 ID 可能不是数字。
    resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_audit_events_created_at", "created_at"),
        Index(
            "ix_audit_events_resource_type_resource_id",
            "resource_type",
            "resource_id",
        ),
        Index("ix_audit_events_request_id", "request_id"),
        Index("ix_audit_events_actor_id", "actor_id"),
    )


__all__ = ["AuditEvent"]
