"""governance: make ``single_use_budget_is_one`` NULL-safe

Revision ID: 0014_gov_grant_constraints
Revises: 0013_governance_pdp
Create Date: 2026-10-21

Issue #78 adds a grant-level backstop: a ``single_use`` grant must budget
exactly one call.  The 0013 expression ``NOT (consumption_mode = 'single_use'
AND (budget ->> 'max_calls')::int <> 1)`` evaluates to ``NULL`` when
``max_calls`` is missing, and a ``CHECK`` constraint treats ``NULL`` as
satisfied -- so a single-use grant without a budget silently passed.

The replacement states the invariant positively and maps a missing key to
``-1``, so ``NULL`` fails instead of passing.  Downgrade restores the exact
0013 expression; no data or historical semantics are rewritten.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_gov_grant_constraints"
down_revision = "0013_governance_pdp"
branch_labels = None
depends_on = None

_OLD_EXPRESSION = (
    "NOT (consumption_mode = 'single_use' AND "
    "(budget ->> 'max_calls')::int <> 1)"
)
_NEW_EXPRESSION = (
    "consumption_mode <> 'single_use' OR "
    "COALESCE((budget ->> 'max_calls')::int, -1) = 1"
)


def upgrade() -> None:
    op.drop_constraint(
        "single_use_budget_is_one", "execution_grants", type_="check"
    )
    op.create_check_constraint(
        "single_use_budget_is_one", "execution_grants", _NEW_EXPRESSION
    )


def downgrade() -> None:
    op.drop_constraint(
        "single_use_budget_is_one", "execution_grants", type_="check"
    )
    op.create_check_constraint(
        "single_use_budget_is_one", "execution_grants", _OLD_EXPRESSION
    )
