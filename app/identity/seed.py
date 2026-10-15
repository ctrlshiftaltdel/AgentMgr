"""权限目录的幂等种子与反向清理（任务 W1-3）。

``seed_permission_catalog`` / ``clear_permission_catalog`` 都接收调用方传入的
``Session``，**不自行 commit/rollback**：事务边界由调用方（迁移或测试）掌握，
这样迁移可以把写入并入 Alembic 的迁移事务，测试也可以在一次会话里重复调用。

幂等策略是「先查后插」而不是 ``ON CONFLICT DO NOTHING``：

* 先按稳定业务键（角色 ``name``、权限 ``(resource, action, scope)``）读出已有
  行，只插入缺失的行；重复调用不会产生重复行，也不会改写已有行的 ``id``。
* ``role_permissions`` 以 ``(role_id, permission_id)`` 复合主键去重。

反向清理只删除**目录定义范围内**的种子行（角色名、权限三元组及其关联），
不误删其它业务数据，因此 ``0003`` 的 downgrade 能干净回到 ``0002``。
"""

from __future__ import annotations

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from app.identity.models import Permission, Role, RolePermission
from app.identity.permissions import (
    PERMISSIONS,
    ROLES,
    ROLE_PERMISSION_NUMBERS,
    permission_by_number,
    validate_catalog,
)

__all__ = ["clear_permission_catalog", "seed_permission_catalog"]


def seed_permission_catalog(session: Session) -> dict[str, int]:
    """幂等写入 7 角色 + 20 权限 + 32 映射，返回本次新建的行数。

    重复调用不会产生重复行：第二次调用返回全 0 且三表行数不变。目录在写入前
    先经 ``validate_catalog()`` 自检，任何违反 PLAN §9 强制规则的编辑都会在
    此处直接失败，而不是静默落库。
    """
    validate_catalog()

    created = {"roles": 0, "permissions": 0, "role_permissions": 0}

    # 1) 角色：按 name 先查后插。
    existing_roles: dict[str, Role] = {
        role.name: role for role in session.scalars(select(Role))
    }
    for spec in ROLES:
        if spec.name not in existing_roles:
            role = Role(name=spec.name, description=spec.description)
            session.add(role)
            existing_roles[spec.name] = role
            created["roles"] += 1
    session.flush()

    # 2) 权限：按 (resource, action, scope) 先查后插。
    existing_permissions: dict[tuple[str, str, str], Permission] = {
        (perm.resource, perm.action, perm.scope): perm
        for perm in session.scalars(select(Permission))
    }
    for spec in PERMISSIONS:
        if spec.key not in existing_permissions:
            perm = Permission(
                resource=spec.resource,
                action=spec.action,
                scope=spec.scope,
            )
            session.add(perm)
            existing_permissions[spec.key] = perm
            created["permissions"] += 1
    session.flush()

    # 3) 角色-权限映射：按 (role_id, permission_id) 去重。
    existing_links: set[tuple[int, int]] = {
        (role_id, permission_id)
        for role_id, permission_id in session.execute(
            select(RolePermission.role_id, RolePermission.permission_id)
        )
    }
    for role_name, numbers in ROLE_PERMISSION_NUMBERS.items():
        role = existing_roles[role_name]
        for number in numbers:
            perm = existing_permissions[permission_by_number(number).key]
            link = (role.id, perm.id)
            if link not in existing_links:
                session.add(
                    RolePermission(role_id=role.id, permission_id=perm.id)
                )
                existing_links.add(link)
                created["role_permissions"] += 1
    session.flush()

    return created


def clear_permission_catalog(session: Session) -> dict[str, int]:
    """删除目录定义范围内的种子行，返回删除的行数。

    顺序固定为「先 role_permissions，再 permissions 与 roles」，避免外键悬空。
    只匹配种子定义的键，不触碰其它业务数据（如 ``employee_channel_bindings``）。
    """
    role_names = [spec.name for spec in ROLES]
    permission_keys = [spec.key for spec in PERMISSIONS]

    role_ids = list(
        session.scalars(select(Role.id).where(Role.name.in_(role_names)))
    )
    permission_ids = list(
        session.scalars(
            select(Permission.id).where(
                tuple_(Permission.resource, Permission.action, Permission.scope).in_(
                    permission_keys
                )
            )
        )
    )

    deleted = {"roles": 0, "permissions": 0, "role_permissions": 0}

    # 1) 先删关联行（任一端的种子行都要删，避免悬空外键）。
    if role_ids or permission_ids:
        result = session.execute(
            delete(RolePermission).where(
                RolePermission.role_id.in_(role_ids or [-1])
                | RolePermission.permission_id.in_(permission_ids or [-1])
            )
        )
        deleted["role_permissions"] = result.rowcount or 0

    # 2) 再删权限与角色（顺序：permissions 先于 roles）。
    if permission_ids:
        result = session.execute(
            delete(Permission).where(Permission.id.in_(permission_ids))
        )
        deleted["permissions"] = result.rowcount or 0

    if role_ids:
        result = session.execute(delete(Role).where(Role.id.in_(role_ids)))
        deleted["roles"] = result.rowcount or 0

    session.flush()
    return deleted
