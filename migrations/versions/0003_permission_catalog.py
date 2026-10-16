"""seed permission catalog and role matrix (roles, permissions, role_permissions)

Revision ID: 0003_permission_catalog
Revises: 0002_identity_rbac
Create Date: 2026-10-15

Data migration for task W1-3: writes the fixed permission catalog and the
role -> permission matrix derived from ``templates/PLAN.md`` §9 and
``docs/PROJECT-CHARTER.md`` (administrators do not automatically gain financial
approval rights).

Frozen snapshot (task W1-16)
----------------------------
This revision originally seeded the rows through
``app.identity.seed.seed_permission_catalog``, i.e. it read the **live**
``app.identity.permissions`` catalog at migration time.  That is not
reproducible: replaying the chain on a fresh database would write whatever the
catalog happened to contain when the replay ran, so the intermediate state at
this revision depended on the working tree rather than on this revision's
history, and any future catalog edit would retroactively change this migration's
behaviour.  The full impact assessment and the decision to freeze the snapshot
are recorded in ``docs/MIGRATION-REPRODUCIBILITY.md``.

This revision therefore carries its **own frozen copy** of the catalog as it
stood when the revision was authored (commit ``b3aa87d``: 7 roles, 19
permissions, 31 mappings, i.e. the state before W1-13's ``0009_perm_alignment``
added ``(audit, read, company)`` for ``finance_lead``).  It imports nothing from
``app`` -- only ``alembic`` and ``sqlalchemy`` -- so it no longer reads active
code.

Replaying the chain on a fresh database always writes the same 19/31 rows here;
``0009_perm_alignment`` then lands the 20th permission and the 32nd mapping, so
every database -- freshly migrated or historically migrated -- reaches the same
20/32 head state.  Existing databases are unaffected: their ``alembic_version``
is already past this revision, so it never re-runs.

``upgrade`` and ``downgrade`` are idempotent (look-up-then-insert, and deletion
of exactly this frozen set), so the upgrade/downgrade round-trip is repeatable
and ``downgrade`` returns cleanly to the ``0002_identity_rbac`` state.

The statements run on Alembic's migration connection (``op.get_bind()``), so
they join the surrounding migration transaction and need no explicit commit.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_permission_catalog"
down_revision = "0002_identity_rbac"
branch_labels = None
depends_on = None

#: Frozen roles ``(name, description)`` -- the 7 roles of PLAN §9.
_FROZEN_ROLES: tuple[tuple[str, str], ...] = (
    ("employee", "普通员工"),
    ("manager", "主管"),
    ("finance_operator", "财务经办"),
    ("finance_lead", "财务主管"),
    ("warehouse", "仓储/采购"),
    ("sysadmin", "系统管理员"),
    ("auditor", "审计人员"),
)

#: Frozen permissions ``(number, resource, action, scope)`` -- numbers 1..19.
_FROZEN_PERMISSIONS: tuple[tuple[int, str, str, str], ...] = (
    (1, "expense_claim", "create", "own"),
    (2, "expense_claim", "read", "own"),
    (3, "expense_claim", "read", "team"),
    (4, "expense_claim", "read", "company"),
    (5, "expense_claim", "read", "authorized"),
    (6, "expense_claim", "submit", "own"),
    (7, "expense_claim", "withdraw", "own"),
    (8, "expense_claim", "approve", "team"),
    (9, "expense_claim", "finance_review", "company"),
    (10, "invoice", "verify", "company"),
    (11, "invoice", "read", "company"),
    (12, "invoice", "read", "authorized"),
    (13, "payment", "record", "company"),
    (14, "inventory", "read", "company"),
    (15, "inventory", "execute", "company"),
    (16, "inventory", "read", "authorized"),
    (17, "audit", "read", "authorized"),
    (18, "identity", "manage", "company"),
    (19, "channel_binding", "manage", "company"),
)

#: Frozen role -> permission-number matrix (31 mappings).
_FROZEN_ROLE_PERMISSION_NUMBERS: dict[str, tuple[int, ...]] = {
    "employee": (1, 2, 6, 7),
    "manager": (1, 2, 3, 6, 7, 8),
    "finance_operator": (4, 9, 10, 11),
    "finance_lead": (4, 9, 10, 11, 13),
    "warehouse": (1, 2, 6, 7, 14, 15),
    "sysadmin": (18, 19),
    "auditor": (5, 12, 16, 17),
}

#: Permission number -> ``(resource, action, scope)`` lookup for the matrix.
_PERMISSION_KEY_BY_NUMBER: dict[int, tuple[str, str, str]] = {
    number: (resource, action, scope)
    for number, resource, action, scope in _FROZEN_PERMISSIONS
}


def upgrade() -> None:
    bind = op.get_bind()

    # 1) roles: look up by the unique ``name``, insert only the missing ones.
    existing_roles: set[str] = set(
        bind.execute(sa.text("SELECT name FROM roles")).scalars()
    )
    for name, description in _FROZEN_ROLES:
        if name not in existing_roles:
            bind.execute(
                sa.text(
                    "INSERT INTO roles (name, description) "
                    "VALUES (:name, :description)"
                ),
                {"name": name, "description": description},
            )

    # 2) permissions: look up by the unique ``(resource, action, scope)`` key.
    existing_permissions: set[tuple[str, str, str]] = {
        (resource, action, scope)
        for resource, action, scope in bind.execute(
            sa.text("SELECT resource, action, scope FROM permissions")
        )
    }
    for _number, resource, action, scope in _FROZEN_PERMISSIONS:
        if (resource, action, scope) not in existing_permissions:
            bind.execute(
                sa.text(
                    "INSERT INTO permissions (resource, action, scope) "
                    "VALUES (:resource, :action, :scope)"
                ),
                {"resource": resource, "action": action, "scope": scope},
            )

    # 3) role -> permission links: resolve ids, insert only the missing pairs.
    role_ids: dict[str, int] = {
        name: role_id
        for name, role_id in bind.execute(sa.text("SELECT name, id FROM roles"))
    }
    permission_ids: dict[tuple[str, str, str], int] = {
        (resource, action, scope): permission_id
        for resource, action, scope, permission_id in bind.execute(
            sa.text("SELECT resource, action, scope, id FROM permissions")
        )
    }
    existing_links: set[tuple[int, int]] = {
        (role_id, permission_id)
        for role_id, permission_id in bind.execute(
            sa.text("SELECT role_id, permission_id FROM role_permissions")
        )
    }
    for role_name, numbers in _FROZEN_ROLE_PERMISSION_NUMBERS.items():
        role_id = role_ids[role_name]
        for number in numbers:
            permission_id = permission_ids[_PERMISSION_KEY_BY_NUMBER[number]]
            if (role_id, permission_id) not in existing_links:
                bind.execute(
                    sa.text(
                        "INSERT INTO role_permissions (role_id, permission_id) "
                        "VALUES (:role_id, :permission_id)"
                    ),
                    {"role_id": role_id, "permission_id": permission_id},
                )


def downgrade() -> None:
    bind = op.get_bind()

    role_names = [name for name, _description in _FROZEN_ROLES]
    frozen_keys = {
        (resource, action, scope)
        for _number, resource, action, scope in _FROZEN_PERMISSIONS
    }

    role_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT id FROM roles WHERE name IN :names").bindparams(
                sa.bindparam("names", expanding=True)
            ),
            {"names": role_names},
        )
    ]
    permission_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT id, resource, action, scope FROM permissions")
        )
        if (row[1], row[2], row[3]) in frozen_keys
    ]

    # 1) links first (either endpoint), so no dangling foreign key remains.
    if role_ids or permission_ids:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions "
                "WHERE role_id IN :role_ids "
                "OR permission_id IN :permission_ids"
            ).bindparams(
                sa.bindparam("role_ids", expanding=True),
                sa.bindparam("permission_ids", expanding=True),
            ),
            {
                "role_ids": role_ids or [-1],
                "permission_ids": permission_ids or [-1],
            },
        )

    # 2) permissions, then roles (children before parents).
    if permission_ids:
        bind.execute(
            sa.text("DELETE FROM permissions WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": permission_ids},
        )
    if role_ids:
        bind.execute(
            sa.text("DELETE FROM roles WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": role_ids},
        )
