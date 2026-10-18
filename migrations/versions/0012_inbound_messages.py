"""inbound channel messages (ingest idempotency anchor)

Revision ID: 0012_inbound_messages
Revises: 0011_expense_invoices
Create Date: 2026-10-18

Creates ``inbound_messages`` -- the per-message idempotency anchor for channel
ingestion (Issue #21), aligned with
``docs/research/W0-2-profile-isolation.md`` §4.3/§4.4:

* the **inbound dedup key** is ``(channel_type, external_message_id)``; the
  database enforces it with a UNIQUE constraint so a concurrent double delivery
  can never create two intake jobs (the service layer catches the
  ``IntegrityError`` and re-reads the existing row -- the pre-check is only a
  fast path, never the guarantee);
* ``external_user_id`` is the channel-side sender key, stored as plain ``TEXT``
  on purpose: it is a *lookup key* (composite identity key with
  ``channel_type``, W0-1 §6.5), not a credential.  Authorization remains solely
  ``employee_channel_bindings``; this table stores no authorization outcome;
* the message body / attachments are deliberately **not** stored -- only the
  keys needed for dedup plus the intake-job reference;
* ``received_at`` is the **local** receipt time (channel-side timestamps are
  untrusted); ``intake_job_id`` references ``intake_jobs.id`` with
  ``ON DELETE CASCADE`` (same convention as ``extraction_drafts``);
* ``status`` uses ``TEXT`` + ``CHECK`` restricted to ``accepted`` / ``rejected``
  (no database ENUM).  The service currently writes only ``accepted`` rows: a
  rejection must never become a permanent dedup anchor that would block a later,
  legitimately bound delivery;
* index on ``intake_job_id`` for the reverse lookup (job -> message).

``downgrade`` drops the index and the table, returning cleanly to the
``0011_expense_invoices`` state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_inbound_messages"
down_revision = "0011_expense_invoices"
branch_labels = None
depends_on = None


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
        "inbound_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_message_id", sa.Text(), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("intake_job_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'accepted'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["intake_job_id"],
            ["intake_jobs.id"],
            name="fk_inbound_messages_intake_job_id_intake_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inbound_messages"),
        sa.UniqueConstraint(
            "channel_type",
            "external_message_id",
            name="uq_inbound_messages_channel_external_message",
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'rejected')", name="status_allowed"
        ),
    )
    op.create_index(
        "ix_inbound_messages_intake_job_id",
        "inbound_messages",
        ["intake_job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbound_messages_intake_job_id", table_name="inbound_messages")
    op.drop_table("inbound_messages")
