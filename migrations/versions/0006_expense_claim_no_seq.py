"""expense claim number sequence (expense_claim_no_seq)

Revision ID: 0006_expense_claim_no_seq
Revises: 0005_expense_core
Create Date: 2026-10-16

Creates the PostgreSQL sequence ``expense_claim_no_seq`` used to generate
human-readable, gap-free-enough claim numbers of the form ``EC-00000001``.

Why a sequence and not ``MAX(claim_no) + 1`` or a UUID:

* ``expense_claims.claim_no`` is globally unique (see migration
  ``0005_expense_core``); a database sequence gives a single, atomic source of
  the next number so concurrent backends never collide (``templates/PLAN.md``
  §8「关键业务号...设唯一约束」and §5.2 reimbursement numbers);
* the number itself is *not* a business decision -- the deterministic backend
  owns it (``docs/PROJECT-CHARTER.md`` 强制原则 6), never the model.

The sequence is intentionally **standalone** (not ``OWNED BY`` any column):
``expense_claims.id`` is a ``BIGINT`` identity, while ``claim_no`` is the
formatted string built from this sequence by ``app.expense.service``. Keeping
it standalone also means autogenerate/``alembic check`` sees no schema drift,
because no ``Sequence`` object is attached to ``Base.metadata``.

``downgrade`` drops the sequence, returning cleanly to the ``0005_expense_core``
state (the five expense tables are untouched by this revision).
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_expense_claim_no_seq"
down_revision = "0005_expense_core"
branch_labels = None
depends_on = None

#: Name of the standalone sequence; also referenced by ``app.expense.service``.
SEQUENCE_NAME = "expense_claim_no_seq"


def upgrade() -> None:
    # START WITH 1: the first generated claim number is EC-00000001.
    # Explicit AS BIGINT / INCREMENT BY 1 documents the intended contract even
    # though they are PostgreSQL defaults.
    op.execute(
        f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME} "
        "AS BIGINT START WITH 1 INCREMENT BY 1"
    )


def downgrade() -> None:
    op.execute(f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME}")
