"""organization core tables (companies, departments, employees)

Revision ID: 0001_org_core
Revises:
Create Date: 2026-10-15

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_org_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "country_code",
            sa.Text(),
            server_default=sa.text("'CN'"),
            nullable=False,
        ),
        sa.Column(
            "base_currency",
            sa.Text(),
            server_default=sa.text("'CNY'"),
            nullable=False,
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
        # Bare name: the metadata naming convention expands it to
        # ck_companies_status_allowed, matching app.db.models.
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'closed')",
            name="status_allowed",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_companies"),
    )

    op.create_table(
        "departments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
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
            ["company_id"],
            ["companies.id"],
            name="fk_departments_company_id_companies",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["departments.id"],
            name="fk_departments_parent_id_departments",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_departments"),
        sa.UniqueConstraint(
            "company_id",
            "parent_id",
            "name",
            name="uq_departments_company_parent_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "employees",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("department_id", sa.BigInteger(), nullable=True),
        sa.Column("manager_id", sa.BigInteger(), nullable=True),
        sa.Column("employee_no", sa.Text(), nullable=False),
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
            ["company_id"],
            ["companies.id"],
            name="fk_employees_company_id_companies",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_employees_department_id_departments",
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["employees.id"],
            name="fk_employees_manager_id_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_employees"),
        sa.UniqueConstraint(
            "company_id",
            "employee_no",
            name="uq_employees_company_no",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'left')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "manager_id IS NULL OR manager_id <> id",
            name="manager_not_self",
        ),
    )


def downgrade() -> None:
    op.drop_table("employees")
    op.drop_table("departments")
    op.drop_table("companies")
