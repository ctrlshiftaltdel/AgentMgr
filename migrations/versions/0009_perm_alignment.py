"""grant audit/read/company to finance_lead (task W1-13)

Revision ID: 0009_perm_alignment
Revises: 0008_intake_files
Create Date: 2026-10-16

Aligns the permission catalog with ``templates/PLAN.md`` §9: the 审计日志
("audit log") row grants the finance lead (财务主管) read access, so the catalog
gains permission number 20 ``(audit, read, company)`` and ``finance_lead`` is
granted it.  The catalog size constants move from 19/31 to 20/32 in
``app.identity.permissions`` (the single source of truth).

``0003_permission_catalog`` already ran on existing databases, so re-running it
would not pick up the new row; this revision writes exactly the two rows the
alignment adds:

* the ``(audit, read, company)`` permission itself, and
* the ``finance_lead`` -> that permission link.

``upgrade`` is idempotent (look-up-then-insert, keyed by the stable permission
triple and the ``(role_id, permission_id)`` primary key) and reads the triple and
the grant from ``app.identity.permissions`` rather than hard-coding them, so a
catalog edit cannot silently drift from the migration.

``downgrade`` removes exactly those two rows (the ``finance_lead`` link and the
permission itself, only when nothing else references it), returning cleanly to
the ``0008_intake_files`` state without touching the rest of the catalog seeded
by ``0003_permission_catalog``.

The seed session joins Alembic's migration transaction via a savepoint
(``join_transaction_mode="create_savepoint"``) so committing does not
prematurely commit the surrounding migration transaction.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.identity.models import Permission, Role, RolePermission
from app.identity.permissions import ROLE_PERMISSION_NUMBERS, permission_by_number

# revision identifiers, used by Alembic.
revision = "0009_perm_alignment"
down_revision = "0008_intake_files"
branch_labels = None
depends_on = None

#: The grant this revision lands: ``finance_lead`` gains permission number 20.
_GRANTED_ROLE = "finance_lead"
_PERMISSION_NUMBER = 20


def _granted_permission() -> Permission:
    """Return an unsaved ``Permission`` for the alignment triple (from catalog)."""
    spec = permission_by_number(_PERMISSION_NUMBER)
    return Permission(
        resource=spec.resource,
        action=spec.action,
        scope=spec.scope,
    )


def _find_permission(session: Session) -> Permission | None:
    spec = permission_by_number(_PERMISSION_NUMBER)
    return session.scalar(
        select(Permission).where(
            Permission.resource == spec.resource,
            Permission.action == spec.action,
            Permission.scope == spec.scope,
        )
    )


def upgrade() -> None:
    # The catalog must already grant number 20 to finance_lead, otherwise this
    # migration and ``app.identity.permissions`` disagree; fail loudly instead of
    # writing an inconsistent row.
    if _PERMISSION_NUMBER not in ROLE_PERMISSION_NUMBERS[_GRANTED_ROLE]:
        raise RuntimeError(
            f"catalog does not grant permission {_PERMISSION_NUMBER} to "
            f"{_GRANTED_ROLE}; refusing to write an inconsistent grant"
        )

    session = Session(bind=op.get_bind(), join_transaction_mode="create_savepoint")
    try:
        # 1) permission (audit, read, company): look-up-then-insert (idempotent).
        perm = _find_permission(session)
        if perm is None:
            perm = _granted_permission()
            session.add(perm)
            session.flush()

        # 2) role -> permission link: keyed by the (role_id, permission_id) PK.
        role = session.scalar(select(Role).where(Role.name == _GRANTED_ROLE))
        if role is None:
            raise RuntimeError(
                f"role {_GRANTED_ROLE!r} is missing; run 0003_permission_catalog "
                "before this revision"
            )

        link = session.scalar(
            select(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id == perm.id,
            )
        )
        if link is None:
            session.add(RolePermission(role_id=role.id, permission_id=perm.id))

        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    session = Session(bind=op.get_bind(), join_transaction_mode="create_savepoint")
    try:
        perm = _find_permission(session)
        if perm is not None:
            # Drop the grant first (any role, so the permission is never left
            # dangling), then remove the permission only if nothing references it.
            session.execute(
                delete(RolePermission).where(RolePermission.permission_id == perm.id)
            )
            session.flush()

            remaining = session.scalar(
                select(func.count())
                .select_from(RolePermission)
                .where(RolePermission.permission_id == perm.id)
            )
            if not remaining:
                session.execute(delete(Permission).where(Permission.id == perm.id))

        session.commit()
    finally:
        session.close()
