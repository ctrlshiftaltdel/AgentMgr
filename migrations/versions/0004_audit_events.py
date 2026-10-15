"""audit_events table + database-level append-only enforcement

Revision ID: 0004_audit_events
Revises: 0003_permission_catalog
Create Date: 2026-10-16

Creates the append-only audit table fixed by the task (Master-defined columns,
aligned with ``templates/PLAN.md`` §8/§9 and
``docs/research/W0-5-security-compliance.md`` §3.2/§7.3).

Key invariants enforced here:

* ``actor_id`` references ``employees(id)`` with **ON DELETE SET NULL** -- audit
  must outlive the person record, so ``CASCADE`` is explicitly forbidden;
* ``resource_id`` is ``TEXT`` (resource ids are not necessarily numeric);
* append-only is enforced at the **database layer**, not by application
  convention: a ``plpgsql`` function ``public.audit_events_reject_mutation()``
  plus a ``BEFORE UPDATE OR DELETE`` row trigger raise an exception on any
  row-level mutation or deletion.

``downgrade`` drops, in order, the trigger, the function, the indexes and the
table, returning cleanly to the ``0003_permission_catalog`` state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0004_audit_events"
down_revision = "0003_permission_catalog"
branch_labels = None
depends_on = None

# Append-only guard: any row-level UPDATE or DELETE is rejected outright.
#
# PostgreSQL implements a foreign key's ``ON DELETE SET NULL`` referential action
# as an internal ``UPDATE`` on the referencing table (audit_events), which would
# otherwise be caught by this very trigger.  That nested referential action runs
# at trigger depth 2, whereas a direct application statement runs at depth 1, so
# the guard allows *only* the FK action -- and only when it does nothing but null
# ``actor_id`` while every other column is untouched.  Everything else raises.
_CREATE_REJECT_FUNCTION = """
CREATE OR REPLACE FUNCTION public.audit_events_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF pg_trigger_depth() > 1
       AND TG_OP = 'UPDATE'
       AND OLD.actor_id IS NOT NULL
       AND NEW.actor_id IS NULL
       AND NEW.id            IS NOT DISTINCT FROM OLD.id
       AND NEW.action        IS NOT DISTINCT FROM OLD.action
       AND NEW.resource_type IS NOT DISTINCT FROM OLD.resource_type
       AND NEW.resource_id   IS NOT DISTINCT FROM OLD.resource_id
       AND NEW.request_id    IS NOT DISTINCT FROM OLD.request_id
       AND NEW.details       IS NOT DISTINCT FROM OLD.details
       AND NEW.created_at    IS NOT DISTINCT FROM OLD.created_at
    THEN
        -- Legitimate: the employees FK's ON DELETE SET NULL action.
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'audit_events is append-only (only INSERT is permitted): % is rejected',
        TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

_CREATE_REJECT_TRIGGER = """
CREATE TRIGGER trg_audit_events_reject_mutation
BEFORE UPDATE OR DELETE ON public.audit_events
FOR EACH ROW EXECUTE FUNCTION public.audit_events_reject_mutation();
"""

_DROP_REJECT_TRIGGER = (
    "DROP TRIGGER IF EXISTS trg_audit_events_reject_mutation "
    "ON public.audit_events;"
)

_DROP_REJECT_FUNCTION = (
    "DROP FUNCTION IF EXISTS public.audit_events_reject_mutation();"
)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        # TEXT: resource ids are not necessarily numeric.
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Audit must outlive the person record: SET NULL, never CASCADE.
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["employees.id"],
            name="fk_audit_events_actor_id_employees",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )

    op.create_index(
        "ix_audit_events_created_at",
        "audit_events",
        ["created_at"],
    )
    op.create_index(
        "ix_audit_events_resource_type_resource_id",
        "audit_events",
        ["resource_type", "resource_id"],
    )
    op.create_index(
        "ix_audit_events_request_id",
        "audit_events",
        ["request_id"],
    )
    op.create_index(
        "ix_audit_events_actor_id",
        "audit_events",
        ["actor_id"],
    )

    # Database-level append-only enforcement (not application-layer trust).
    op.execute(_CREATE_REJECT_FUNCTION)
    op.execute(_CREATE_REJECT_TRIGGER)


def downgrade() -> None:
    # Order matters: trigger -> function -> indexes -> table.
    op.execute(_DROP_REJECT_TRIGGER)
    op.execute(_DROP_REJECT_FUNCTION)
    op.drop_index("ix_audit_events_actor_id", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index(
        "ix_audit_events_resource_type_resource_id",
        table_name="audit_events",
    )
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
