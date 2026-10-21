"""Trusted principal resolution for governance decisions.

A :class:`GovernancePrincipal` is built **only** from already-verified context
(an authenticated session, mTLS workload identity or signed token) plus records
read from the identity directory.  Request-body or model-supplied identity
fields are never accepted: :func:`reject_client_asserted_identity` makes that
rule executable, and every resolver re-reads the employee/service identity row
before returning.

Service identities are deliberately *not* super-users:

* their permissions are the union of their assigned **existing** roles;
* when they act for a requester, the effective permission set is intersected
  with the requester's own permissions, so a service identity can never inherit
  an excess the requester does not hold;
* a disabled/revoked service identity or a disabled owner fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.db.models.organization import Employee
from app.governance.errors import PrincipalResolutionError
from app.governance.models import ServiceIdentity, ServiceIdentityRole
from app.identity.authorization import (
    effective_permissions_for_role_ids,
    role_ids_for_employee,
)

__all__ = [
    "CLIENT_ASSERTED_IDENTITY_FIELDS",
    "GovernancePrincipal",
    "audit_actor_id",
    "principal_ref",
    "reject_client_asserted_identity",
    "require_management_permission",
    "resolve_employee_principal",
    "resolve_principal",
    "resolve_service_principal",
]

VERIFIED_ASSURANCES = frozenset(
    {"verified_session", "verified_mtls", "verified_signed_token"}
)


def principal_ref(prefix: str, value: int | str) -> str:
    """Build a contract-valid ``<prefix>_<id>`` reference.

    The frozen schemas require at least four characters after the prefix
    (``prin_``/``svc_``/``role_``/``dept_``).  Numeric IDs are zero-padded to
    four digits so small development/test IDs remain schema-valid.
    """
    text = str(value)
    if text.isdigit():
        text = text.zfill(4)
    return f"{prefix}_{text}"

def _audit_resolution_failure(
    session: Session,
    *,
    reason_code: str,
    resource_id: str | None,
    context: Mapping[str, Any],
) -> None:
    """Record a resolution failure before raising.

    Resolution may happen before a PDP evaluation, so without this event a
    failed login/identity binding would be invisible to the audit trail.
    """
    record_event(
        session,
        actor_id=None,
        action="governance.principal.resolution_denied",
        resource_type="principal",
        resource_id=resource_id,
        details={"reason_code": reason_code, **dict(context)},
    )


def audit_actor_id(principal: GovernancePrincipal) -> int | None:
    """Return the human employee id an audit event may name.

    ``record_event`` requires an actual person.  A service identity acting for
    a requester is audited as that requester; otherwise its accountable owner
    is the person.  Never the caller-supplied actor id.
    """
    if principal.employee_id is not None:
        return principal.employee_id
    return principal.owner_employee_id


def require_management_permission(
    principal: GovernancePrincipal,
    *,
    resource: str = "identity",
    action: str = "manage",
    scope: str = "company",
) -> GovernancePrincipal:
    """Require an active principal holding an existing RBAC management permission.

    Policy publication and grant revocation are management operations, not
    subject to the caller's self-asserted actor id.
    """
    principal.require_active()
    if (resource, action, scope) not in principal.effective_permissions:
        raise PrincipalResolutionError(
            "capability_not_granted",
            context={
                "principal_id": principal.principal_id,
                "required_permission": [resource, action, scope],
            },
        )
    return principal


#: Fields a client or model must never be allowed to assert.  They may appear in
#: a tool payload for explanation, but the governance layer rejects them rather
#: than risk treating a self-declared identity as authority.
CLIENT_ASSERTED_IDENTITY_FIELDS = frozenset(
    {
        "principal_id",
        "principal_type",
        "employee_id",
        "service_identity_id",
        "role_ids",
        "roles",
        "account_status",
        "identity_assurance",
        "resolution",
        "permissions",
    }
)


@dataclass(frozen=True)
class GovernancePrincipal:
    """A resolved, trusted actor for one governance evaluation."""

    principal_id: str
    principal_type: str
    employee_id: int | None
    service_identity_id: str | None
    company_id: int | None
    role_ids: tuple[int, ...]
    account_status: str
    identity_assurance: str
    effective_permissions: frozenset[tuple[str, str, str]] = field(
        default_factory=frozenset
    )
    delegation_ref: str | None = None
    department_id: int | None = None
    owner_employee_id: int | None = None
    service_identity_pk: int | None = None

    @property
    def is_active(self) -> bool:
        return self.account_status == "active"

    @property
    def is_service(self) -> bool:
        return self.principal_type == "service"

    def subject_document(self) -> dict[str, Any]:
        """Return the contract-shaped ``subject`` object for a decision."""
        subject: dict[str, Any] = {
            "principal_id": self.principal_id,
            "principal_type": self.principal_type,
            "account_status": self.account_status,
            "identity_assurance": self.identity_assurance,
            "resolution": "server_resolved",
            "role_ids": [principal_ref("role", role_id) for role_id in self.role_ids],
        }
        if self.department_id is not None:
            subject["department_id"] = principal_ref("dept", self.department_id)
        if self.service_identity_id is not None:
            subject["service_identity_id"] = self.service_identity_id
        if self.delegation_ref is not None:
            subject["delegation_ref"] = self.delegation_ref
        return subject

    def require_active(self) -> GovernancePrincipal:
        if not self.is_active:
            raise PrincipalResolutionError(
                "principal_disabled",
                context={
                    "principal_id": self.principal_id,
                    "account_status": self.account_status,
                },
            )
        return self


def reject_client_asserted_identity(payload: Mapping[str, Any] | None) -> None:
    """Reject a payload that tries to assert identity/authority fields.

    This is intentionally strict: a client or model can describe a request, but
    it cannot declare who it is or which roles it holds.  The server resolves
    those values from the authenticated session and the directory.
    """
    if not payload:
        return
    found = sorted(CLIENT_ASSERTED_IDENTITY_FIELDS.intersection(payload))
    if found:
        raise PrincipalResolutionError(
            "identity_unverified", context={"client_asserted_fields": found}
        )


def _assurance(value: str) -> str:
    if value not in VERIFIED_ASSURANCES:
        raise PrincipalResolutionError(
            "identity_unverified", context={"identity_assurance": value}
        )
    return value


def _assurance_with_audit(session: Session, value: str) -> str:
    try:
        return _assurance(value)
    except PrincipalResolutionError as exc:
        _audit_resolution_failure(
            session,
            reason_code=exc.reason_code,
            resource_id=None,
            context=exc.context,
        )
        raise


def _employee_roles(session: Session, employee_id: int) -> tuple[int, ...]:
    return role_ids_for_employee(session, employee_id)


def resolve_employee_principal(
    session: Session,
    employee_id: int,
    *,
    identity_assurance: str = "verified_session",
    principal_type: str = "person",
) -> GovernancePrincipal:
    """Resolve a person/agent-instance principal from a verified context.

    ``employee_id`` must come from the authenticated session (or an equivalent
    verified assertion), never from a request body.  Missing, non-active or
    unbound employees fail closed.
    """
    _assurance_with_audit(session, identity_assurance)
    if principal_type not in {"person", "agent_instance"}:
        context = {"principal_type": principal_type}
        _audit_resolution_failure(
            session,
            reason_code="identity_unverified",
            resource_id=None,
            context=context,
        )
        raise PrincipalResolutionError("identity_unverified", context=context)

    employee = session.get(Employee, employee_id)
    if employee is None:
        context = {"employee_id": employee_id}
        _audit_resolution_failure(
            session,
            reason_code="principal_unbound",
            resource_id=principal_ref("prin", employee_id),
            context=context,
        )
        raise PrincipalResolutionError("principal_unbound", context=context)
    if employee.status != "active":
        context = {"employee_id": employee_id, "status": employee.status}
        _audit_resolution_failure(
            session,
            reason_code="principal_disabled",
            resource_id=principal_ref("prin", employee.id),
            context=context,
        )
        raise PrincipalResolutionError("principal_disabled", context=context)

    role_ids = _employee_roles(session, employee.id)
    permissions = effective_permissions_for_role_ids(session, role_ids)
    return GovernancePrincipal(
        principal_id=principal_ref("prin", employee.id),
        principal_type=principal_type,
        employee_id=employee.id,
        service_identity_id=None,
        company_id=employee.company_id,
        role_ids=role_ids,
        account_status="active",
        identity_assurance=identity_assurance,
        effective_permissions=permissions,
        department_id=employee.department_id,
    )


def _service_identity_pk(value: int | str) -> int:
    if isinstance(value, int):
        return value
    prefix = "svc_"
    if isinstance(value, str) and value.startswith(prefix):
        try:
            return int(value[len(prefix) :])
        except ValueError as exc:
            raise PrincipalResolutionError(
                "principal_unbound", context={"service_identity_id": value}
            ) from exc
    raise PrincipalResolutionError(
        "principal_unbound", context={"service_identity_id": value}
    )


def _service_role_ids(session: Session, service_identity_id: int) -> tuple[int, ...]:
    rows = session.scalars(
        select(ServiceIdentityRole.role_id)
        .where(ServiceIdentityRole.service_identity_id == service_identity_id)
        .order_by(ServiceIdentityRole.role_id)
    ).all()
    return tuple(rows)


def resolve_service_principal(
    session: Session,
    service_identity_id: int | str,
    *,
    requester: GovernancePrincipal | None = None,
    identity_assurance: str = "verified_mtls",
    delegation_ref: str | None = None,
) -> GovernancePrincipal:
    """Resolve a service identity, optionally acting for a requester.

    The service identity's own owner must be an active employee.  When
    ``requester`` is supplied, the service identity must belong to the same
    company and its effective permissions are intersected with the requester's;
    an empty intersection is a valid (deny-by-default) result, not an error.
    """
    _assurance_with_audit(session, identity_assurance)
    try:
        pk = _service_identity_pk(service_identity_id)
    except PrincipalResolutionError as exc:
        _audit_resolution_failure(
            session,
            reason_code=exc.reason_code,
            resource_id=None,
            context={**exc.context, "field": "service_identity_id"},
        )
        raise
    row = session.get(ServiceIdentity, pk)
    if row is None:
        context = {"service_identity_id": str(service_identity_id)}
        _audit_resolution_failure(
            session,
            reason_code="principal_unbound",
            resource_id=None,
            context=context,
        )
        raise PrincipalResolutionError("principal_unbound", context=context)
    if row.status != "active":
        context = {
            "service_identity_id": principal_ref("svc", row.id),
            "status": row.status,
        }
        _audit_resolution_failure(
            session,
            reason_code="principal_disabled",
            resource_id=principal_ref("svc", row.id),
            context=context,
        )
        raise PrincipalResolutionError("principal_disabled", context=context)

    owner = session.get(Employee, row.owner_employee_id)
    if owner is None:
        context = {
            "service_identity_id": principal_ref("svc", row.id),
            "owner_missing": True,
        }
        _audit_resolution_failure(
            session,
            reason_code="principal_unbound",
            resource_id=principal_ref("svc", row.id),
            context=context,
        )
        raise PrincipalResolutionError("principal_unbound", context=context)
    if owner.status != "active":
        context = {
            "service_identity_id": principal_ref("svc", row.id),
            "owner_status": owner.status,
        }
        _audit_resolution_failure(
            session,
            reason_code="principal_disabled",
            resource_id=principal_ref("svc", row.id),
            context=context,
        )
        raise PrincipalResolutionError("principal_disabled", context=context)

    service_role_ids = _service_role_ids(session, row.id)
    service_permissions = effective_permissions_for_role_ids(session, service_role_ids)

    employee_id: int | None = None
    company_id: int | None = owner.company_id
    permissions = service_permissions
    if requester is not None:
        requester.require_active()
        if requester.company_id != owner.company_id:
            context = {
                "service_identity_id": principal_ref("svc", row.id),
                "reason": "company_mismatch",
            }
            _audit_resolution_failure(
                session,
                reason_code="principal_unbound",
                resource_id=principal_ref("svc", row.id),
                context=context,
            )
            raise PrincipalResolutionError("principal_unbound", context=context)
        employee_id = requester.employee_id
        company_id = requester.company_id
        # No inherited excess: service authority is bounded by the requester.
        permissions = service_permissions.intersection(
            requester.effective_permissions
        )
        if requester.delegation_ref is not None:
            delegation_ref = delegation_ref or requester.delegation_ref

    return GovernancePrincipal(
        principal_id=principal_ref("svc", row.id),
        principal_type="service",
        employee_id=employee_id,
        service_identity_id=principal_ref("svc", row.id),
        company_id=company_id,
        role_ids=service_role_ids,
        account_status="active",
        identity_assurance=identity_assurance,
        effective_permissions=permissions,
        delegation_ref=delegation_ref,
        department_id=requester.department_id if requester is not None else None,
        owner_employee_id=owner.id,
        service_identity_pk=row.id,
    )


def resolve_principal(
    session: Session,
    principal_id: str,
    *,
    identity_assurance: str = "verified_session",
    requester: GovernancePrincipal | None = None,
    delegation_ref: str | None = None,
    principal_type: str = "person",
) -> GovernancePrincipal:
    """Resolve a verified principal ID from an authentication context.

    ``principal_id`` is accepted here only because the caller has already
    authenticated it.  Prefix/type mismatches fail closed.  Runtime principals
    (``rt_``) require a runtime identity registry, which Issue #78 does not
    create; they are therefore rejected rather than trusted by assertion.
    """
    _assurance_with_audit(session, identity_assurance)
    if not isinstance(principal_id, str):
        _audit_resolution_failure(
            session,
            reason_code="identity_unverified",
            resource_id=None,
            context={"reason": "principal_id_not_a_string"},
        )
        raise PrincipalResolutionError("identity_unverified")
    if principal_id.startswith("prin_"):
        try:
            employee_id = int(principal_id[len("prin_") :])
        except ValueError:
            _audit_resolution_failure(
                session,
                reason_code="identity_unverified",
                resource_id=principal_id,
                context={"reason": "malformed_principal_id"},
            )
            raise PrincipalResolutionError("identity_unverified") from None
        principal = resolve_employee_principal(
            session,
            employee_id=employee_id,
            identity_assurance=identity_assurance,
            principal_type=principal_type,
        )
        if delegation_ref is not None:
            principal = replace(principal, delegation_ref=delegation_ref)
        return principal
    if principal_id.startswith("svc_"):
        try:
            pk = int(principal_id[len("svc_") :])
        except ValueError:
            _audit_resolution_failure(
                session,
                reason_code="identity_unverified",
                resource_id=principal_id,
                context={"reason": "malformed_principal_id"},
            )
            raise PrincipalResolutionError("identity_unverified") from None
        return resolve_service_principal(
            session,
            service_identity_id=pk,
            requester=requester,
            identity_assurance=identity_assurance,
            delegation_ref=delegation_ref,
        )
    context = {"principal_id": principal_id, "reason": "unsupported_prefix"}
    _audit_resolution_failure(
        session,
        reason_code="identity_unverified",
        resource_id=principal_id if isinstance(principal_id, str) else None,
        context=context,
    )
    raise PrincipalResolutionError("identity_unverified", context=context)
