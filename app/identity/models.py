"""身份与权限（RBAC）数据模型：roles、permissions 与员工绑定。

结构由任务 W1-2 固定，并对齐：

* ``templates/PLAN.md`` §8「身份、Profile 与权限」与 §9「权限模型」；
* ``docs/research/W0-1-weixin-ilink.md`` §6.5（(渠道, 渠道用户 ID) 复合身份键）；
* ``docs/research/W0-2-profile-isolation.md`` §4（身份路由协议草案）。

关键不变量：

* ``employee_channel_bindings`` 的唯一键是复合键 ``(channel_type,
  external_user_id)``（W0-1 §6.5）：渠道用户 ID 绝不跨渠道复用，因此
  **不得**退化为只对 ``external_user_id`` 唯一。
* ``permissions.scope`` 限定为 ``own``/``team``/``company``/``authorized``，
  对应 PLAN §9 的资源/数据范围控制维度。
* 授权默认拒绝、最小权限（PLAN §9 强制规则），本层只承载数据，判定在后端。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin


class Role(TimestampMixin, Base):
    """角色：权限集合的命名分组（PLAN §8 ``roles(id, name)``）。"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Permission(TimestampMixin, Base):
    """权限：``(resource, action, scope)`` 三元组，scope 表达数据范围。"""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'own'")
    )

    __table_args__ = (
        UniqueConstraint(
            "resource",
            "action",
            "scope",
            name="uq_permissions_resource_action_scope",
        ),
        CheckConstraint(
            "scope IN ('own', 'team', 'company', 'authorized')",
            name="scope_allowed",
        ),
    )


class EmployeeRole(Base):
    """员工-角色授予关系；``granted_by`` 记录授权人（PLAN §9 需审计）。"""

    __tablename__ = "employee_roles"

    employee_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    granted_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )


class RolePermission(Base):
    """角色-权限关联（纯连接表，复合主键）。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )


class EmployeeChannelBinding(TimestampMixin, Base):
    """渠道身份绑定：``(channel_type, external_user_id)`` → 内部员工 ID。

    复合唯一键来自 W0-1 §6.5 / W0-2 §4.2：渠道用户 ID 跨渠道不复用，路由以
    「(渠道, 渠道用户 ID)」为身份真源；未绑定/已解绑默认拒绝（status）。
    """

    __tablename__ = "employee_channel_bindings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )

    __table_args__ = (
        # 复合身份键：绝不可退化为仅 external_user_id 唯一（W0-1 §6.5）。
        UniqueConstraint(
            "channel_type",
            "external_user_id",
            name="uq_employee_channel_bindings_channel_type",
        ),
        CheckConstraint("status IN ('active', 'revoked')", name="status_allowed"),
        Index("ix_employee_channel_bindings_employee_id", "employee_id"),
    )


__all__ = [
    "EmployeeChannelBinding",
    "EmployeeRole",
    "Permission",
    "Role",
    "RolePermission",
]
