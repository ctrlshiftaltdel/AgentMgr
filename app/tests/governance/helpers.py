"""Shared builders for governance tests.

The tests deliberately use the real PostgreSQL test database and the existing
identity fixtures/patterns (see ``app/tests/identity``).  Nothing here mocks the
policy engine, the database or the audit service.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Employee
from app.governance.models import (
    GovernanceDelegation,
    GovernancePolicyVersion,
    ServiceIdentity,
    ServiceIdentityRole,
)
from app.governance.policies import create_draft, publish
from app.governance.principal import (
    GovernancePrincipal,
    resolve_employee_principal,
)
from app.identity.models import EmployeeRole, Role
from app.identity.seed import seed_permission_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]


def alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@dataclass(frozen=True)
class Org:
    company: Company
    employee: Employee
    manager: Employee
    finance: Employee
    auditor: Employee
    admin: Employee


@dataclass(frozen=True)
class PolicyContext:
    policy_set_id: str
    row: GovernancePolicyVersion

    @property
    def version(self) -> str:
        return self.row.version

    @property
    def content_hash(self) -> str:
        return self.row.content_hash


def unique_policy_set(prefix: str = "polset_gov") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def make_org(session: Session) -> Org:
    seed_permission_catalog(session)
    company = Company(name=f"GOV {uuid.uuid4().hex[:8]}")
    session.add(company)
    session.flush()

    def employee(number: str) -> Employee:
        row = Employee(company_id=company.id, employee_no=number)
        session.add(row)
        session.flush()
        return row

    employee_row = employee("E-gov-employee")
    manager = employee("E-gov-manager")
    finance = employee("E-gov-finance")
    auditor = employee("E-gov-auditor")
    admin = employee("E-gov-admin")
    manager.manager_id = None
    session.flush()

    roles = {
        role.name: role
        for role in session.scalars(
            select(Role).where(
                Role.name.in_(
                    ["employee", "manager", "finance_lead", "auditor", "sysadmin"]
                )
            )
        )
    }
    session.add_all(
        [
            EmployeeRole(employee_id=employee_row.id, role_id=roles["employee"].id),
            EmployeeRole(employee_id=manager.id, role_id=roles["manager"].id),
            EmployeeRole(employee_id=finance.id, role_id=roles["finance_lead"].id),
            EmployeeRole(employee_id=auditor.id, role_id=roles["auditor"].id),
            EmployeeRole(employee_id=admin.id, role_id=roles["sysadmin"].id),
        ]
    )
    session.flush()
    return Org(
        company=company,
        employee=employee_row,
        manager=manager,
        finance=finance,
        auditor=auditor,
        admin=admin,
    )


def admin_principal(session: Session, org: Org) -> GovernancePrincipal:
    """Resolve the sysadmin employee used for policy/grant management tests."""
    return resolve_employee_principal(session, employee_id=org.admin.id)


class FakeEvidenceVerifier:
    """A test double standing in for a trusted approval/confirmation store.

    Production code defaults to :class:`DenyAllApprovalEvidenceVerifier`; this
    class exists only to prove that *with* a trusted verifier the gate logic
    resolves roles to people and still rejects self-approval.
    """

    def __init__(self, *, role_resolutions: dict[str, str] | None = None) -> None:
        self.role_resolutions = dict(role_resolutions or {})
        self.confirmation_calls = 0
        self.approval_calls = 0

    def verify_confirmation(
        self,
        ref: dict[str, Any],
        *,
        principal_id: str,
        payload_hash: str | None,
        now: datetime,
    ) -> bool:
        self.confirmation_calls += 1
        return (
            ref.get("status") == "satisfied"
            and ref.get("consumed_at") is None
            and ref.get("payload_hash") == payload_hash
        )

    def verify_approval(
        self,
        ref: dict[str, Any],
        *,
        requester_principal_id: str,
        owner_principal_ids: frozenset[str],
        payload_hash: str | None,
        now: datetime,
    ) -> str | None:
        self.approval_calls += 1
        if ref.get("decision") != "approved" or ref.get("payload_hash") != payload_hash:
            return None
        approver = ref.get("approver_principal_id")
        if isinstance(approver, str) and approver.startswith("role_"):
            return self.role_resolutions.get(approver)
        if isinstance(approver, str) and approver.startswith("prin_"):
            return approver
        return None


def scope(
    scope_type: str = "self",
    *,
    resource_ids: tuple[str, ...] = ("res_expense_0001",),
    connector_ids: tuple[str, ...] = (),
    classifications: tuple[str, ...] = ("internal",),
    exclude_resource_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "scope_type": scope_type,
        "resource_ids": list(resource_ids),
        "connector_ids": list(connector_ids),
        "data_classifications": list(classifications),
        "exclude_resource_ids": list(exclude_resource_ids),
    }


def confirmation_ref(
    *,
    principal_id: str,
    payload_hash: str = "sha256:" + "1" * 64,
    expires_in: int = 3600,
    status: str = "satisfied",
    consumed_at: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "confirmation_id": "conf_gov_0001",
        "payload_hash": payload_hash,
        "canonicalization": "rfc8785_jcs",
        "purpose": "high_impact_action",
        "confirmed_by_principal_id": principal_id,
        "status": status,
        "satisfied_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=expires_in))
        .isoformat()
        .replace("+00:00", "Z"),
        "consumed_at": consumed_at,
        "consumed_by_call_id": None,
        "single_use": True,
    }


def approval_ref(
    *,
    approver_principal_id: str = "prin_approver_0001",
    payload_hash: str = "sha256:" + "1" * 64,
    expires_in: int = 3600,
    decision: str = "approved",
    separation_check: str = "passed",
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "approval_id": "appr_gov_0001",
        "approver_principal_id": approver_principal_id,
        "payload_hash": payload_hash,
        "canonicalization": "rfc8785_jcs",
        "decision": decision,
        "separation_check": separation_check,
        "decided_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=expires_in))
        .isoformat()
        .replace("+00:00", "Z"),
    }


DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "read_self",
        "capability": "expense.read@1.0.0",
        "required_permissions": [["expense_claim", "read", "own"]],
        "allowed_scope_types": ["self"],
        "risk_levels": ["low", "medium"],
        "delegation_allowed": True,
        "ttl_seconds": 900,
        "max_calls": 5,
        "max_runtime_seconds": 300,
    },
    {
        "rule_id": "submit_self",
        "capability": "expense.submit@1.0.0",
        "required_permissions": [["expense_claim", "submit", "own"]],
        "allowed_scope_types": ["self"],
        "required_confirmation": True,
        "required_approval": True,
        "ttl_seconds": 900,
        "max_calls": 1,
        "max_runtime_seconds": 300,
    },
    {
        "rule_id": "inventory_company",
        "capability": "inventory.read@1.0.0",
        "required_permissions": [["inventory", "read", "company"]],
        "allowed_scope_types": ["company"],
        "ttl_seconds": 900,
        "max_calls": 5,
        "max_runtime_seconds": 300,
    },
    {
        "rule_id": "risk_low_only",
        "capability": "risk.high@1.0.0",
        "required_permissions": [],
        "allowed_scope_types": ["self"],
        "risk_levels": ["low"],
        "ttl_seconds": 900,
        "max_calls": 5,
        "max_runtime_seconds": 300,
    },
    {
        "rule_id": "managed_task",
        "capability": "managed.task@1.0.0",
        "required_permissions": [],
        "allowed_scope_types": ["self"],
        "requires_managed_execution": True,
        "ttl_seconds": 900,
        "max_calls": 5,
        "max_runtime_seconds": 300,
    },
    {
        "rule_id": "delegated_read",
        "capability": "delegated.read@1.0.0",
        "required_permissions": [["expense_claim", "read", "own"]],
        "allowed_scope_types": ["self"],
        "delegation_allowed": True,
        "ttl_seconds": 900,
        "max_calls": 5,
        "max_runtime_seconds": 300,
    },
]

DEFAULT_HARD_DENIES: list[dict[str, Any]] = [
    {
        "rule_id": "hard_payment",
        "capability": "payment.execute@1.0.0",
        "reason_code": "policy_denied",
    }
]


def policy_document(
    policy_set_id: str,
    *,
    version: str = "1.0.0",
    rules: list[dict[str, Any]] | None = None,
    hard_denies: list[dict[str, Any]] | None = None,
    decision_ttl_seconds: int = 900,
) -> dict[str, Any]:
    return {
        "policy_set_id": policy_set_id,
        "version": version,
        "default_effect": "deny",
        "hard_denies": copy.deepcopy(
            DEFAULT_HARD_DENIES if hard_denies is None else hard_denies
        ),
        "rules": copy.deepcopy(DEFAULT_RULES if rules is None else rules),
        "decision_ttl_seconds": decision_ttl_seconds,
    }


def publish_policy(
    session: Session,
    org: Org,
    *,
    policy_set_id: str | None = None,
    version: str = "1.0.0",
    rules: list[dict[str, Any]] | None = None,
    hard_denies: list[dict[str, Any]] | None = None,
    decision_ttl_seconds: int = 900,
    principal: GovernancePrincipal | None = None,
) -> PolicyContext:
    policy_set_id = policy_set_id or unique_policy_set()
    document = policy_document(
        policy_set_id,
        version=version,
        rules=rules,
        hard_denies=hard_denies,
        decision_ttl_seconds=decision_ttl_seconds,
    )
    actor = principal or admin_principal(session, org)
    draft = create_draft(
        session,
        document=document,
        principal=actor,
        created_by_employee_id=org.finance.id,
        actor_id=org.finance.id,
    )
    row = publish(
        session,
        policy_version_id=draft.id,
        principal=actor,
        published_by_employee_id=org.finance.id,
        actor_id=org.finance.id,
    )
    return PolicyContext(policy_set_id=policy_set_id, row=row)


def make_service_identity(
    session: Session,
    *,
    owner: Employee,
    role_names: tuple[str, ...] = ("employee",),
) -> ServiceIdentity:
    identity = ServiceIdentity(
        name=f"svc-{uuid.uuid4().hex[:10]}",
        owner_employee_id=owner.id,
        status="active",
    )
    session.add(identity)
    session.flush()
    roles = {
        role.name: role
        for role in session.scalars(select(Role).where(Role.name.in_(role_names)))
    }
    session.add_all(
        [
            ServiceIdentityRole(
                service_identity_id=identity.id, role_id=roles[name].id
            )
            for name in role_names
        ]
    )
    session.flush()
    return identity


def make_delegation(
    session: Session,
    *,
    delegator: Employee,
    delegatee: Employee | None = None,
    service_identity: ServiceIdentity | None = None,
    capabilities: tuple[str, ...] = ("delegated.read@1.0.0",),
    resource_scope: dict[str, Any] | None = None,
    policy_version: str = "1.0.0",
    policy_set_id: str | None = None,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    status: str = "active",
) -> GovernanceDelegation:
    now = datetime.now(UTC)
    delegation = GovernanceDelegation(
        delegation_id=f"dlg_{uuid.uuid4().hex[:16]}",
        delegator_employee_id=delegator.id,
        delegatee_employee_id=delegatee.id if delegatee is not None else None,
        delegatee_service_identity_id=(
            service_identity.id if service_identity is not None else None
        ),
        capabilities=list(capabilities),
        resource_scope=resource_scope or scope(),
        policy_set_id=policy_set_id,
        policy_version=policy_version,
        starts_at=starts_at or (now - timedelta(minutes=5)),
        expires_at=expires_at or (now + timedelta(hours=1)),
        status=status,
    )
    session.add(delegation)
    session.flush()
    return delegation
