"""seed permission catalog and role matrix (roles, permissions, role_permissions)

Revision ID: 0003_permission_catalog
Revises: 0002_identity_rbac
Create Date: 2026-10-15

Data migration for task W1-3: writes the fixed permission catalog and the
role -> permission matrix derived from ``templates/PLAN.md`` §9 and
``docs/PROJECT-CHARTER.md`` (administrators do not automatically gain financial
approval rights).

The rows come from ``app.identity.permissions`` (single source of truth) and are
written through the idempotent ``app.identity.seed.seed_permission_catalog``
helper, so re-running the seed never duplicates rows.  ``downgrade`` calls the
matching ``clear_permission_catalog`` and removes exactly those seed rows,
returning the schema/data to the ``0002_identity_rbac`` state.

The seed session joins Alembic's migration transaction via a savepoint
(``join_transaction_mode="create_savepoint"``) so committing the seed does not
prematurely commit the surrounding migration transaction.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy.orm import Session

from app.identity.seed import clear_permission_catalog, seed_permission_catalog

# revision identifiers, used by Alembic.
revision = "0003_permission_catalog"
down_revision = "0002_identity_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    session = Session(bind=op.get_bind(), join_transaction_mode="create_savepoint")
    try:
        seed_permission_catalog(session)
        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    session = Session(bind=op.get_bind(), join_transaction_mode="create_savepoint")
    try:
        clear_permission_catalog(session)
        session.commit()
    finally:
        session.close()
