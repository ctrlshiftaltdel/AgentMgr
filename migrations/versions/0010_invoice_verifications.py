"""invoice verification records

Revision ID: 0010_invoice_verifications
Revises: 0009_perm_alignment
Create Date: 2026-10-16

Creates ``invoice_verifications`` — the persisted invoice-verification record
required by ``templates/PLAN.md`` §7.2 (fields and the seven result states) and
§8 (``invoice_verifications(id, source_file_id, provider_type, provider_ref,
status, verified_at, verified_by, evidence_ref)`` plus every §7.2 field).  Field
provenance is documented in ``docs/research/W0-4-invoice-verification.md`` §8.

Key invariants enforced here:

* BIGINT identity primary key, ``created_at``/``updated_at`` as
  ``timestamptz NOT NULL DEFAULT now()``;
* **money is fixed-point decimal**: ``amount_without_tax`` / ``tax_amount`` /
  ``amount_with_tax`` are ``NUMERIC(18, 2)`` and nullable (PLAN §8「金额采用固定
  精度 decimal，不用浮点数保存财务金额」) — they stay NULL when the channel
  could not return the invoice face, because inventing an amount is forbidden;
* ``duplicate_key`` carries a **UNIQUE** constraint so repeated verification of
  the same invoice can never create a second row (idempotency);
* ``status`` / ``provider_type`` / ``duplicate_check_result`` use ``TEXT`` +
  ``CHECK`` (no database ENUM, so the sets can evolve);
* ``evidence_ref`` references the sanitized evidence blob stored alongside in
  ``evidence_json`` (tax numbers / invoice numbers are masked before storage;
  see ``app.integrations.invoice_verification.sanitize_evidence``);
* ``verified_by`` references ``employees.id`` (operator must be a real person);
* ``seller_tax_code`` / ``buyer_tax_code`` are permission-protected sensitive
  fields (W0-5 §3.1 class B).

``downgrade`` drops the table (and its indexes/constraints), returning cleanly
to the ``0009_perm_alignment`` state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0010_invoice_verifications"
down_revision = "0009_perm_alignment"
branch_labels = None
depends_on = None

_STATUS_VALUES = (
    "pending_identification",
    "pending_manual_confirmation",
    "pending_verification",
    "verified_pass",
    "verified_fail",
    "unable_to_verify",
    "suspected_duplicate",
)
_PROVIDER_TYPE_VALUES = ("manual_assisted", "authorized_provider", "official_api")
_DUPLICATE_CHECK_VALUES = ("unique", "suspected_duplicate")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


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
        "invoice_verifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column("invoice_code", sa.Text(), nullable=True),
        sa.Column("invoice_no", sa.Text(), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("seller_tax_code", sa.Text(), nullable=True),
        sa.Column("buyer_tax_code", sa.Text(), nullable=True),
        sa.Column("amount_without_tax", sa.Numeric(18, 2), nullable=True),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("amount_with_tax", sa.Numeric(18, 2), nullable=True),
        sa.Column(
            "currency",
            sa.Text(),
            server_default=sa.text("'CNY'"),
            nullable=False,
        ),
        sa.Column("invoice_type", sa.Text(), nullable=True),
        sa.Column("duplicate_key", sa.Text(), nullable=False),
        sa.Column(
            "duplicate_check_result",
            sa.Text(),
            server_default=sa.text("'unique'"),
            nullable=False,
        ),
        sa.Column("provider_type", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=True),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "verify_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "daily_verify_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_verify_date", sa.Date(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["source_files.id"],
            name="fk_invoice_verifications_source_file_id_source_files",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["employees.id"],
            name="fk_invoice_verifications_verified_by_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoice_verifications"),
        sa.UniqueConstraint(
            "duplicate_key", name="uq_invoice_verifications_duplicate_key"
        ),
        sa.CheckConstraint(
            _in_list("status", _STATUS_VALUES), name="status_allowed"
        ),
        sa.CheckConstraint(
            _in_list("provider_type", _PROVIDER_TYPE_VALUES),
            name="provider_type_allowed",
        ),
        sa.CheckConstraint(
            _in_list("duplicate_check_result", _DUPLICATE_CHECK_VALUES),
            name="duplicate_check_result_allowed",
        ),
        sa.CheckConstraint(
            "amount_without_tax IS NULL OR amount_without_tax >= 0",
            name="amount_without_tax_non_negative",
        ),
        sa.CheckConstraint(
            "tax_amount IS NULL OR tax_amount >= 0",
            name="tax_amount_non_negative",
        ),
        sa.CheckConstraint(
            "amount_with_tax IS NULL OR amount_with_tax >= 0",
            name="amount_with_tax_non_negative",
        ),
        sa.CheckConstraint(
            "verify_attempt_count >= 0", name="verify_attempt_count_non_negative"
        ),
        sa.CheckConstraint(
            "daily_verify_count >= 0", name="daily_verify_count_non_negative"
        ),
    )
    op.create_index(
        "ix_invoice_verifications_invoice_no",
        "invoice_verifications",
        ["invoice_no"],
    )
    op.create_index(
        "ix_invoice_verifications_source_file_id",
        "invoice_verifications",
        ["source_file_id"],
    )
    op.create_index(
        "ix_invoice_verifications_status",
        "invoice_verifications",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invoice_verifications_status", table_name="invoice_verifications"
    )
    op.drop_index(
        "ix_invoice_verifications_source_file_id",
        table_name="invoice_verifications",
    )
    op.drop_index(
        "ix_invoice_verifications_invoice_no", table_name="invoice_verifications"
    )
    op.drop_table("invoice_verifications")
