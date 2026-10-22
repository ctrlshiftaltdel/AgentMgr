"""Trusted principal adapter for the client governance surface (B1).

The only accepted identity sources are the existing internal token and the
verified employee header consumed by :func:`app.api.deps.get_principal`.  This
module does not add a second authentication scheme; it adapts that verified
employee identity into a :class:`GovernancePrincipal` by re-reading the
directory (roles, permissions, account status).  A model/tool argument can
never name a principal, owner, approver, role or priority.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.api.deps import get_principal
from app.governance.principal import (
    GovernancePrincipal,
    resolve_employee_principal,
)
from app.identity.authorization import Principal

__all__ = [
    "TrustedActor",
    "actor_from_identity",
    "resolve_actor_from_headers",
]


@dataclass(frozen=True)
class TrustedActor:
    """The verified human identity plus its governance projection."""

    identity: Principal
    governance: GovernancePrincipal

    @property
    def principal_id(self) -> str:
        return self.governance.principal_id

    @property
    def employee_id(self) -> int:
        return self.identity.employee_id


def actor_from_identity(session: Session, identity: Principal) -> TrustedActor:
    """Re-read the employee directory and return a trusted governance actor.

    ``get_principal`` has already validated the internal token and the employee
    header; this second read is deliberate defence in depth (a disabled or
    unbound employee fails closed even if the caller resolved it earlier).
    """
    governance = resolve_employee_principal(
        session,
        employee_id=identity.employee_id,
        identity_assurance="verified_session",
        principal_type="person",
    )
    governance.require_active()
    return TrustedActor(identity=identity, governance=governance)


def resolve_actor_from_headers(
    session: Session,
    *,
    authorization: str | None,
    employee_header: str | None,
) -> TrustedActor:
    """MCP counterpart of the FastAPI dependency.

    ``get_principal`` is reused as a plain function so the token comparison,
    header parsing and active-employee check are exactly the same code path as
    every existing REST endpoint.  It raises the existing ``ApiError`` subclasses
    (401/403/503), which the MCP adapter maps to stable tool errors.
    """
    identity = get_principal(
        session=session,
        authorization=authorization,
        x_employee_id=employee_header,
    )
    return actor_from_identity(session, identity)
