"""identity and RBAC tables (roles, permissions, employee bindings)

Revision ID: 0002_identity_rbac
Revises: 0001_org_core
Create Date: 2026-10-15

Creates the five identity/RBAC tables fixed by task W1-2:

* ``roles`` / ``permissions`` -- RBAC catalog (PLAN §8, §9);
* ``employee_roles`` / ``role_permissions`` -- grant and role-permission links;
* ``employee_channel_bindings`` -- the ``(channel_type, external_user_id)``
  composite identity key required by W0-1 §6.5 / W0-2 §4.2.

CHECK constraints use bare names so the shared metadata naming convention
expands them to ``ck_<table>_<name>``, matching ``app.identity.models``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_identity_rbac"
down_revision = "0001_org_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    op.create_table(
        "permissions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "scope",
            sa.Text(),
            server_default=sa.text("'own'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_permissions"),
        sa.UniqueConstraint(
            "resource",
            "action",
            "scope",
            name="uq_permissions_resource_action_scope",
        ),
        # Bare name: the metadata naming convention expands it to
        # ck_permissions_scope_allowed, matching app.identity.models.
        sa.CheckConstraint(
            "scope IN ('own', 'team', 'company', 'authorized')",
            name="scope_allowed",
        ),
    )

    op.create_table(
        "employee_roles",
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("granted_by", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_employee_roles_employee_id_employees",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_employee_roles_role_id_roles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["employees.id"],
            name="fk_employee_roles_granted_by_employees",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("employee_id", "role_id", name="pk_employee_roles"),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("permission_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_role_permissions_role_id_roles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name="fk_role_permissions_permission_id_permissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name="pk_role_permissions"),
    )

    op.create_table(
        "employee_channel_bindings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=True),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_employee_channel_bindings_employee_id_employees",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_employee_channel_bindings"),
        # Composite identity key (W0-1 §6.5): never unique on external_user_id alone.
        sa.UniqueConstraint(
            "channel_type",
            "external_user_id",
            name="uq_employee_channel_bindings_channel_type",
        ),
        # Bare name -> ck_employee_channel_bindings_status_allowed.
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="status_allowed",
        ),
    )

    op.create_index(
        "ix_employee_channel_bindings_employee_id",
        "employee_channel_bindings",
        ["employee_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_employee_channel_bindings_employee_id",
        table_name="employee_channel_bindings",
    )
    op.drop_table("employee_channel_bindings")
    op.drop_table("role_permissions")
    op.drop_table("employee_roles")
    op.drop_table("permissions")
    op.drop_table("roles")
