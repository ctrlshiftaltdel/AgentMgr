"""expense core tables (categories, cost centers, claims, lines, payments)

Revision ID: 0005_expense_core
Revises: 0004_audit_events
Create Date: 2026-10-16

Creates the reimbursement-domain tables fixed by the task (Master-defined
columns, aligned with ``templates/PLAN.md`` §5.2/§8 and
``docs/PROJECT-CHARTER.md`` principle 6).

Key invariants enforced here:

* **All money columns are ``NUMERIC(18, 2)``** (fixed-point), never
  ``Float``/``REAL``/``DOUBLE`` -- PLAN §8 "金额采用固定精度 decimal，不用浮点数
  保存财务金额";
* BIGINT identity primary keys, ``created_at``/``updated_at`` as
  ``timestamptz NOT NULL DEFAULT now()``;
* ``status`` uses ``TEXT`` + ``CHECK`` (no database ENUM, to keep evolution
  cheap);
* ``expense_categories``/``cost_centers`` unique on ``(company_id, code)``;
* ``expense_claims.claim_no`` globally unique; ``total_amount >= 0``;
* ``expense_lines.amount >= 0`` / ``tax_amount >= 0``, cascade-deleted with the
  claim; ``reimbursement_payments.amount > 0``.

``downgrade`` drops the five tables in reverse dependency order, returning
cleanly to the ``0004_audit_events`` state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_expense_core"
down_revision = "0004_audit_events"
branch_labels = None
depends_on = None


def _money() -> sa.Numeric:
    """Fixed-point money type shared by every amount column."""
    return sa.Numeric(18, 2)


def _timestamps() -> list[sa.Column]:
    """The mandatory audit timestamps shared by every core table."""
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "expense_categories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_expense_categories_company_id_companies",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_categories"),
        sa.UniqueConstraint(
            "company_id", "code", name="uq_expense_categories_company_code"
        ),
        # Bare name: the metadata naming convention expands it to
        # ck_expense_categories_status_allowed, matching app.expense.models.
        sa.CheckConstraint("status IN ('active', 'inactive')", name="status_allowed"),
    )

    op.create_table(
        "cost_centers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_cost_centers_company_id_companies",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cost_centers"),
        sa.UniqueConstraint("company_id", "code", name="uq_cost_centers_company_code"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="status_allowed"),
    )

    op.create_table(
        "expense_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("claim_no", sa.Text(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.Text(),
            server_default=sa.text("'CNY'"),
            nullable=False,
        ),
        sa.Column(
            "total_amount",
            _money(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_expense_claims_employee_id_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_claims"),
        sa.UniqueConstraint("claim_no", name="uq_expense_claims_claim_no"),
        sa.CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'withdrawn', 'approved', "
            "'finance_reviewed', 'paid', 'rejected')",
            name="status_allowed",
        ),
    )

    op.create_table(
        "expense_lines",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount", _money(), nullable=False),
        sa.Column(
            "tax_amount",
            _money(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("cost_center_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["expense_claims.id"],
            name="fk_expense_lines_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["expense_categories.id"],
            name="fk_expense_lines_category_id_expense_categories",
        ),
        sa.ForeignKeyConstraint(
            ["cost_center_id"],
            ["cost_centers.id"],
            name="fk_expense_lines_cost_center_id_cost_centers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_lines"),
        sa.CheckConstraint("amount >= 0", name="amount_non_negative"),
        sa.CheckConstraint("tax_amount >= 0", name="tax_amount_non_negative"),
    )

    op.create_table(
        "reimbursement_payments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", _money(), nullable=False),
        sa.Column(
            "currency",
            sa.Text(),
            server_default=sa.text("'CNY'"),
            nullable=False,
        ),
        sa.Column("payment_ref", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["expense_claims.id"],
            name="fk_reimbursement_payments_claim_id_expense_claims",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reimbursement_payments"),
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'failed')", name="status_allowed"
        ),
    )


def downgrade() -> None:
    # Reverse dependency order: children before parents.
    op.drop_table("reimbursement_payments")
    op.drop_table("expense_lines")
    op.drop_table("expense_claims")
    op.drop_table("cost_centers")
    op.drop_table("expense_categories")
