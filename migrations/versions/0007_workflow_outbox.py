"""workflow instances, approval steps, transactional outbox, notifications

Revision ID: 0007_workflow_outbox
Revises: 0006_expense_claim_no_seq
Create Date: 2026-10-16

Creates the workflow / outbox / notification tables fixed by the task W1-8
(Master-defined columns, aligned with ``templates/PLAN.md`` §8 and
``docs/PROJECT-CHARTER.md`` D5).

Key invariants enforced here:

* BIGINT identity primary keys, ``created_at``/``updated_at`` as
  ``timestamptz NOT NULL DEFAULT now()``;
* ``status`` uses ``TEXT`` + ``CHECK`` (no database ENUM);
* ``workflow_instances`` unique on
  ``(business_type, business_id, workflow_type)`` -- one instance per business
  object per workflow type, so a repeated request never creates duplicate
  approval tasks (PLAN §12);
* ``approval_steps`` unique on ``(instance_id, step_no)`` and cascade-deleted
  with the instance; ``approver_id`` references ``employees``;
* ``outbox_events.idempotency_key`` globally unique (idempotent enqueue);
  ``payload`` is ``JSONB NOT NULL DEFAULT '{}'::jsonb``; ``attempt_count >= 0``;
  index ``(status, available_at)`` serves the worker's batch claim query;
* ``notifications.dedup_key`` globally unique (no duplicate notifications);
  ``recipient_id`` references ``employees``; ``attempt_count >= 0``.

``downgrade`` drops the four tables in reverse dependency order, returning
cleanly to the ``0006_expense_claim_no_seq`` state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0007_workflow_outbox"
down_revision = "0006_expense_claim_no_seq"
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
        "workflow_instances",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("business_type", sa.Text(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("workflow_type", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_instances"),
        sa.UniqueConstraint(
            "business_type",
            "business_id",
            "workflow_type",
            name="uq_workflow_instances_business",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'approved', 'rejected', "
            "'canceled')",
            name="status_allowed",
        ),
    )

    op.create_table(
        "approval_steps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("approver_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instances.id"],
            name="fk_approval_steps_instance_id_workflow_instances",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approver_id"],
            ["employees.id"],
            name="fk_approval_steps_approver_id_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_steps"),
        sa.UniqueConstraint(
            "instance_id", "step_no", name="uq_approval_steps_instance_step_no"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'returned', 'skipped')",
            name="status_allowed",
        ),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_outbox_events_idempotency_key"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'failed')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="attempt_count_non_negative"
        ),
    )

    op.create_index(
        "ix_outbox_events_status_available_at",
        "outbox_events",
        ["status", "available_at"],
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "channel_type",
            sa.Text(),
            server_default=sa.text("'weixin'"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "delivery_status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["employees.id"],
            name="fk_notifications_recipient_id_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint("dedup_key", name="uq_notifications_dedup_key"),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'sent', 'failed')",
            name="delivery_status_allowed",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="attempt_count_non_negative"
        ),
    )


def downgrade() -> None:
    # Reverse dependency order: children before parents.
    op.drop_table("notifications")
    op.drop_index(
        "ix_outbox_events_status_available_at", table_name="outbox_events"
    )
    op.drop_table("outbox_events")
    op.drop_table("approval_steps")
    op.drop_table("workflow_instances")
