"""expense invoice attachments (claim <-> verified invoice linkage)

Revision ID: 0011_expense_invoices
Revises: 0010_invoice_verifications
Create Date: 2026-10-16

Creates ``expense_invoices`` — the linkage required by ``templates/PLAN.md`` §8
(``expense_invoices(id, claim_id, source_file_id, invoice_fields_json,
verification_status, duplicate_key)``) so that invoice-verification records
(``invoice_verifications``, revision ``0010``) can be attached to an expense
claim.  ``suspected_duplicate`` is the Master-authorized additional column
carrying the cross-claim duplicate marker (acceptance item 2).

Key invariants enforced here:

* BIGINT identity primary key, ``created_at``/``updated_at`` as
  ``timestamptz NOT NULL DEFAULT now()``;
* ``claim_id`` references ``expense_claims.id`` with ``ON DELETE CASCADE``
  (same convention as ``expense_lines``); ``source_file_id`` references
  ``source_files.id`` and stays nullable (same convention as
  ``invoice_verifications.source_file_id``);
* ``invoice_fields_json`` is ``JSONB NOT NULL``: it holds the caller-submitted
  **invoice face snapshot** (code / number / ISO date / kind / fixed-point
  amount string, no tax numbers) -- never a floating-point amount;
* ``verification_status`` uses ``TEXT`` + ``CHECK`` restricted to the **seven**
  verification statuses of ``app.integrations.models.STATUS_VALUES`` (no
  database ENUM; the business layer copies the value verbatim from
  ``invoice_verifications.status`` and must never invent a value);
* ``duplicate_key`` holds the one-way invoice-identity digest
  (``compute_duplicate_key``); ``(claim_id, duplicate_key)`` is UNIQUE so the
  same invoice can only be attached once per claim (the concurrent race is
  caught by the constraint even if the service-layer pre-check is bypassed);
* ``suspected_duplicate`` is ``BOOLEAN NOT NULL DEFAULT false``: set on every
  affected row (this one and the ones on other claims) when the same invoice
  appears on more than one claim;
* indexes on ``duplicate_key`` (cross-claim lookup) and ``claim_id``
  (per-claim listing).

``downgrade`` drops the table with its indexes/constraints, returning cleanly
to the ``0010_invoice_verifications`` state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0011_expense_invoices"
down_revision = "0010_invoice_verifications"
branch_labels = None
depends_on = None

#: The seven verification statuses, identical to
#: ``app.integrations.models.STATUS_VALUES`` (migrations repeat literals on
#: purpose: they must not import application code).  Do not add values here
#: without changing the contract layer first.
_VERIFICATION_STATUS_VALUES = (
    "pending_identification",
    "pending_manual_confirmation",
    "pending_verification",
    "verified_pass",
    "verified_fail",
    "unable_to_verify",
    "suspected_duplicate",
)


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
        "expense_invoices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "invoice_fields_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("duplicate_key", sa.Text(), nullable=False),
        sa.Column(
            "suspected_duplicate",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["expense_claims.id"],
            name="fk_expense_invoices_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["source_files.id"],
            name="fk_expense_invoices_source_file_id_source_files",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_invoices"),
        sa.UniqueConstraint(
            "claim_id", "duplicate_key", name="uq_expense_invoices_claim_duplicate"
        ),
        # Bare name: the metadata naming convention expands it to
        # ck_expense_invoices_verification_status_allowed, matching
        # app.expense.models.
        sa.CheckConstraint(
            _in_list("verification_status", _VERIFICATION_STATUS_VALUES),
            name="verification_status_allowed",
        ),
    )
    op.create_index(
        "ix_expense_invoices_duplicate_key",
        "expense_invoices",
        ["duplicate_key"],
    )
    op.create_index(
        "ix_expense_invoices_claim_id",
        "expense_invoices",
        ["claim_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_expense_invoices_claim_id", table_name="expense_invoices")
    op.drop_index("ix_expense_invoices_duplicate_key", table_name="expense_invoices")
    op.drop_table("expense_invoices")
