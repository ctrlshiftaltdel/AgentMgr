"""审计事件模型：``audit_events``（只增不改）。

列定义由 Master 统一固定，对齐 ``templates/PLAN.md`` §8 与 §9：

* ``id`` BIGINT 自增主键；
* ``actor_id`` 是**普通 ``BIGINT NULL``，不带外键**。审计是一本独立的追加式账本，
  必须记录**实际操作人**的员工 ID，因此绝不能因为人员记录变动而改写它：
  - 员工按 ``status = 'left'`` **软删除**，员工行仍在，``actor_id`` 在实践中仍可解析；
  - 硬删除员工时，审计行**保留原 ``actor_id`` 不变**，允许出现悬空引用——这是有意
    的取舍：宁可留下一个指向已不存在员工的 ID，也不能丢失「谁做了这件事」的审计证据。
  这也是本表**不声明** ``ON DELETE SET NULL`` / ``CASCADE`` 外键的原因：任何会在员工
  被删除时改写 ``actor_id`` 的引用动作都会抹掉实际操作人；
* ``action`` / ``resource_type`` 必填；``resource_id`` 用 ``TEXT``（资源 ID 可能
  不是数字）；``request_id`` 关联同一次调用；``details`` 为结构化 JSONB；
* ``created_at`` 为 ``timestamptz NOT NULL DEFAULT now()``。

只增不改由**数据库层**强制（迁移 ``0004_audit_events`` 的 BEFORE UPDATE OR DELETE
触发器无条件拒绝任何 UPDATE / DELETE），不依赖应用层自觉；本模型不提供任何更新/
删除路径。

部署前提（防护有效性的必要条件，详见 ``app.audit.service`` 模块 docstring）：应用
数据库角色**不得拥有 DDL 权限**，否则它可 ``ALTER TABLE ... DISABLE TRIGGER`` 绕过
本触发器；迁移应由独立的 DDL 角色执行。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditEvent(Base):
    """一条不可变、仅追加的审计事件。"""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 无外键：审计是独立追加式账本，actor_id 永久保留实际操作人的员工 ID。
    # 员工软删除（status='left'）时该 ID 仍可解析；硬删除时审计行保留原值，
    # 允许悬空引用——绝不因人员记录变动而改写审计证据。
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
