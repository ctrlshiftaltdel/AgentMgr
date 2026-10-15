"""intake jobs, extraction drafts, source files

Revision ID: 0008_intake_files
Revises: 0007_workflow_outbox
Create Date: 2026-10-16

Creates the intake / document tables fixed by the task W1-9 (Master-defined
columns, aligned with ``templates/PLAN.md`` §4.2, §8 and
``docs/research/W0-5-security-compliance.md`` §4.3/§5.5).

Tables:

* ``intake_jobs`` -- one received-material job per channel/input type;
* ``source_files`` -- content-addressed storage metadata for an uploaded file;
* ``extraction_drafts`` -- parsed draft (extracted JSON + confidence + source
  reference) that never writes into a formal business table.

Key invariants enforced here:

* BIGINT identity primary keys, ``created_at``/``updated_at`` as
  ``timestamptz NOT NULL DEFAULT now()``;
* ``status`` / ``input_type`` / ``review_status`` / ``scan_status`` use ``TEXT``
  + ``CHECK`` (no database ENUM);
* ``extraction_drafts.intake_job_id`` cascades with its job; ``source_file_id``
  is nullable and references ``source_files`` (original-file reference -- the
  raw file is never overwritten by the parse result, PLAN §4.2);
* ``source_files.storage_key`` is deliberately **not** unique: identical content
  uploaded by different employees shares one physical blob (content addressing),
  while the database keeps one row per upload (owner metadata differs).  Dedup
  happens in the storage layer (``app.documents.storage``), not in the database;
* ``source_files.size_bytes >= 0``; indexes on ``storage_key`` and ``sha256``.

``downgrade`` drops the three tables in reverse dependency order, returning
cleanly to the ``0007_workflow_outbox`` state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0008_intake_files"
down_revision = "0007_workflow_outbox"
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
        "intake_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("submitted_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "channel_type",
            sa.Text(),
            server_default=sa.text("'weixin'"),
            nullable=False,
        ),
        sa.Column("input_type", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'received'"),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["submitted_by"],
            ["employees.id"],
            name="fk_intake_jobs_submitted_by_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_intake_jobs"),
        sa.CheckConstraint(
            "input_type IN ('text', 'image', 'file', 'voice', 'spreadsheet')",
            name="input_type_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('received', 'parsed', 'needs_review', 'failed')",
            name="status_allowed",
        ),
    )

    op.create_table(
        "source_files",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=True),
        sa.Column(
            "scan_status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["employees.id"],
            name="fk_source_files_owner_id_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_files"),
        sa.CheckConstraint(
            "size_bytes >= 0", name="size_bytes_non_negative"
        ),
        sa.CheckConstraint(
            "scan_status IN ('pending', 'clean', 'infected', 'failed')",
            name="scan_status_allowed",
        ),
    )
    op.create_index(
        "ix_source_files_storage_key", "source_files", ["storage_key"]
    )
    op.create_index("ix_source_files_sha256", "source_files", ["sha256"])

    op.create_table(
        "extraction_drafts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("intake_job_id", sa.BigInteger(), nullable=False),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column("business_type", sa.Text(), nullable=False),
        sa.Column(
            "extracted_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "confidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "review_status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["intake_job_id"],
            ["intake_jobs.id"],
            name="fk_extraction_drafts_intake_job_id_intake_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["source_files.id"],
            name="fk_extraction_drafts_source_file_id_source_files",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_drafts"),
        sa.CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'rejected')",
            name="review_status_allowed",
        ),
    )


def downgrade() -> None:
    # Reverse dependency order: children before parents.
    op.drop_table("extraction_drafts")
    op.drop_index("ix_source_files_sha256", table_name="source_files")
    op.drop_index("ix_source_files_storage_key", table_name="source_files")
    op.drop_table("source_files")
    op.drop_table("intake_jobs")
