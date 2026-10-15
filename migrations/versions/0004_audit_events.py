"""audit_events table + database-level append-only enforcement

Revision ID: 0004_audit_events
Revises: 0003_permission_catalog
Create Date: 2026-10-16

Creates the append-only audit table fixed by the task (Master-defined columns,
aligned with ``templates/PLAN.md`` §8/§9 and
``docs/research/W0-5-security-compliance.md`` §3.2/§7.3).

Key invariants enforced here:

* ``actor_id`` is a plain ``BIGINT NULL`` with **no foreign key**.  Audit is an
  independent append-only ledger: it must keep the *actual* operator's employee
  id, so it deliberately does **not** declare ``ON DELETE SET NULL`` / ``CASCADE``
  -- any referential action that rewrote ``actor_id`` when an employee was
  deleted would erase the operator from the record.  Employees are soft-deleted
  (``status = 'left'``) so the id stays resolvable in practice; on a hard delete
  the audit row keeps its original ``actor_id`` (dangling references are allowed
  on purpose -- audit evidence must not be lost);
* ``resource_id`` is ``TEXT`` (resource ids are not necessarily numeric);
* append-only is enforced at the **database layer**, not by application
  convention: a ``plpgsql`` function ``public.audit_events_reject_mutation()``
  plus a ``BEFORE UPDATE OR DELETE`` row trigger **unconditionally** raise an
  exception (``ERRCODE = 'restrict_violation'``) on any row-level UPDATE or
  DELETE.  There is **no** ``pg_trigger_depth()`` escape hatch and **no** column
  shape check: an earlier revision tried to whitelist the ``ON DELETE SET NULL``
  referential action via ``pg_trigger_depth() > 1``, which any role able to
  create a trigger could abuse (at depth 2) to null out ``actor_id``.  Dropping
  the FK and rejecting unconditionally closes that hole.

Deployment prerequisite for this guard to hold: the application database role
must **not** own DDL privileges (otherwise it could
``ALTER TABLE ... DISABLE TRIGGER``); migrations run under a separate DDL role.
See ``app/audit/service.py`` for the full statement.

This revision is in its **final form** (``0004`` is not yet merged, so it is
amended in place; no ``0005`` is added).

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
# No ``pg_trigger_depth()`` escape hatch and no column-shape whitelist: those
# existed only to let the ``employees`` FK's ``ON DELETE SET NULL`` referential
# action (which Postgres runs as an internal UPDATE at trigger depth 2) through,
# and any role able to create a trigger could exploit the same depth-2 path to
# null out ``actor_id``.  The FK is gone (``actor_id`` is a plain BIGINT), so the
# guard rejects unconditionally.
_CREATE_REJECT_FUNCTION = """
CREATE OR REPLACE FUNCTION public.audit_events_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
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
        # Plain BIGINT NULL, no FK: audit keeps the actual operator's employee
        # id and is never rewritten when an employee row is deleted.
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
