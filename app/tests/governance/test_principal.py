"""Trusted principal resolution (Issue #78).

Negative coverage: unknown, disabled, unbound, forged request-body identity and
cross-company service delegation.  Positive coverage: the directory is the only
source of identity and a service identity never exceeds its requester.
"""

from __future__ import annotations

import pytest

from app.db.models.organization import Company, Employee
from app.governance.errors import PrincipalResolutionError
from app.governance.principal import (
    principal_ref,
    reject_client_asserted_identity,
    resolve_employee_principal,
    resolve_principal,
    resolve_service_principal,
)
from app.tests.governance.helpers import Org, make_service_identity


def test_resolve_employee_principal_reads_directory(session, org: Org) -> None:
    principal = resolve_employee_principal(session, employee_id=org.employee.id)

    assert principal.principal_id == principal_ref("prin", org.employee.id)
    assert principal.principal_type == "person"
    assert principal.company_id == org.company.id
    assert principal.account_status == "active"
    assert ("expense_claim", "read", "own") in principal.effective_permissions
    assert principal.subject_document()["resolution"] == "server_resolved"


def test_unknown_employee_is_unbound(session) -> None:
    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_employee_principal(session, employee_id=999_999_999)
    assert excinfo.value.reason_code == "principal_unbound"


def test_disabled_employee_is_rejected(session, org: Org) -> None:
    org.employee.status = "inactive"
    session.flush()
    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_employee_principal(session, employee_id=org.employee.id)
    assert excinfo.value.reason_code == "principal_disabled"


def test_unverified_assurance_is_rejected(session, org: Org) -> None:
    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_employee_principal(
            session,
            employee_id=org.employee.id,
            identity_assurance="unverified",
        )
    assert excinfo.value.reason_code == "identity_unverified"


def test_client_asserted_identity_is_rejected(session) -> None:
    with pytest.raises(PrincipalResolutionError) as excinfo:
        reject_client_asserted_identity(
            {"principal_id": "prin_admin_0001", "role_ids": ["role_sysadmin"]}
        )
    assert excinfo.value.reason_code == "identity_unverified"

    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_principal(session, principal_id="role_sysadmin")
    assert excinfo.value.reason_code == "identity_unverified"

    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_principal(session, principal_id="rt_runtime_0001")
    assert excinfo.value.reason_code == "identity_unverified"


def test_service_identity_permissions_are_bounded_by_requester(session, org: Org) -> None:
    identity = make_service_identity(
        session, owner=org.finance, role_names=("manager",)
    )

    service_alone = resolve_service_principal(
        session, service_identity_id=identity.id
    )
    assert ("expense_claim", "read", "team") in service_alone.effective_permissions
    assert ("expense_claim", "approve", "team") in service_alone.effective_permissions

    requester = resolve_employee_principal(session, employee_id=org.employee.id)
    acting = resolve_service_principal(
        session, service_identity_id=identity.id, requester=requester
    )

    # The employee role only has own-scope expense permissions.  The service
    # identity must not keep its wider manager permissions.
    assert ("expense_claim", "read", "own") in acting.effective_permissions
    assert ("expense_claim", "read", "team") not in acting.effective_permissions
    assert ("expense_claim", "approve", "team") not in acting.effective_permissions
    assert acting.effective_permissions.issubset(requester.effective_permissions)
    assert acting.principal_id == principal_ref("svc", identity.id)
    assert acting.employee_id == org.employee.id
    assert acting.service_identity_id == principal_ref("svc", identity.id)


def test_revoked_service_identity_or_disabled_owner_is_rejected(session, org: Org) -> None:
    identity = make_service_identity(session, owner=org.finance)
    identity.status = "revoked"
    session.flush()
    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_service_principal(session, service_identity_id=identity.id)
    assert excinfo.value.reason_code == "principal_disabled"

    identity.status = "active"
    org.finance.status = "left"
    session.flush()
    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_service_principal(session, service_identity_id=identity.id)
    assert excinfo.value.reason_code == "principal_disabled"


def test_service_identity_cannot_act_for_another_company(session, org: Org) -> None:
    identity = make_service_identity(session, owner=org.finance)
    other_company = Company(name="GOV other")
    session.add(other_company)
    session.flush()
    other = Employee(company_id=other_company.id, employee_no="E-other")
    session.add(other)
    session.flush()
    requester = resolve_employee_principal(session, employee_id=other.id)

    with pytest.raises(PrincipalResolutionError) as excinfo:
        resolve_service_principal(
            session, service_identity_id=identity.id, requester=requester
        )
    assert excinfo.value.reason_code == "principal_unbound"
