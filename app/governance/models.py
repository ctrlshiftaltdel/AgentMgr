"""Governance data model: service identities, policy versions, delegations,
decision records and execution grants.

The model reuses the existing identity domain (``employees``, ``roles``,
``permissions``, ``employee_roles``, ``role_permissions``) and the append-only
``audit_events`` table.  It does **not** introduce a second employee, role,
permission or approval model.

Schema conventions follow the rest of the repository:

* ``BIGINT`` identity primary keys and explicit FK/UNIQUE/CHECK names, so the
  project-wide naming convention in :mod:`app.db.base` stays deterministic;
* enums are ``TEXT`` + ``CHECK`` (no database ENUM type);
* structured documents use PostgreSQL ``JSONB``;
* every ``*_at`` business timestamp is ``timestamptz``.

``task_id`` is an opaque string on purpose: Issue #79 owns the task aggregate.
Issue #78 must be able to bind a grant and a decision to a task without
creating a task table of its own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

# Import the identity models so ``service_identity_roles.role_id -> roles.id``
# resolves when this module is imported on its own (the model-import
# self-sufficiency test runs each module in a fresh interpreter).
import app.identity.models  # noqa: F401,E402

__all__ = [
    "ExecutionGrant",
    "GovernanceDecisionRecord",
    "GovernanceDelegation",
    "GovernancePolicyVersion",
    "ServiceIdentity",
    "ServiceIdentityRole",
]

_POLICY_SET_ID_PATTERN = r"^polset_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_SEMVER_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"
_SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
_PRINCIPAL_PATTERN = r"^(prin|rt|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CAPABILITY_PATTERN = (
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*@[0-9]+\.[0-9]+\.[0-9]+$"
)


class ServiceIdentity(TimestampMixin, Base):
    """A non-human connector/runtime identity with a human accountable owner.

    Service identities receive permissions only through reusable ``roles``.
    They are never administrators by default and never inherit permissions the
    acting requester does not already hold (enforced in
    :mod:`app.governance.principal`).
    """

    __tablename__ = "service_identities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_employee_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'revoked')", name="status_allowed"),
        Index("ix_service_identities_owner_employee_id", "owner_employee_id"),
    )


class ServiceIdentityRole(Base):
    """Service-identity to existing-role grant (composite primary key)."""

    __tablename__ = "service_identity_roles"

    service_identity_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "service_identities.id",
            ondelete="CASCADE",
            name="fk_service_identity_roles_service_identity_id",
        ),
        primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )


class GovernancePolicyVersion(Base):
    """One immutable policy document version.

    A policy set has at most one ``published`` row at a time (partial unique
    index).  Publishing retires the previous row; rollback creates a **new**
    version with ``supersedes_id`` instead of mutating history.
    """

    __tablename__ = "governance_policy_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_set_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'")
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_employee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supersedes_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "governance_policy_versions.id",
            ondelete="SET NULL",
            name="fk_governance_policy_versions_supersedes_id",
        ),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "policy_set_id",
            "version",
            name="uq_governance_policy_versions_policy_set_version",
        ),
        CheckConstraint(
            f"policy_set_id ~ '{_POLICY_SET_ID_PATTERN}'",
            name="policy_set_id_format",
        ),
        CheckConstraint(f"version ~ '{_SEMVER_PATTERN}'", name="version_format"),
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')", name="status_allowed"
        ),
        CheckConstraint(
            f"content_hash ~ '{_SHA256_PATTERN}'", name="content_hash_format"
        ),
        Index(
            "ix_governance_policy_versions_policy_set_status",
            "policy_set_id",
            "status",
        ),
        Index(
            "uq_governance_policy_versions_published_policy_set",
            "policy_set_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )


class GovernanceDelegation(TimestampMixin, Base):
    """A scoped, time-bounded, revocable delegation of capability and scope.

    Exactly one delegatee is allowed: an employee (``delegatee_employee_id``)
    or a service identity (``delegatee_service_identity_id``).  A delegation can
    never exceed the delegator's own permissions or the policy rule's scope.
    """

    __tablename__ = "governance_delegations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Optional public reference.  The contract uses ``dlg_*``; when omitted the
    # service also accepts ``dlg_<row id>`` so callers cannot be forced to
    # generate a second identifier just to reference a delegation.
    delegation_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    delegator_employee_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    delegatee_employee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=True,
    )
    delegatee_service_identity_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "service_identities.id",
            ondelete="CASCADE",
            name="fk_governance_delegations_delegatee_service_identity_id",
        ),
        nullable=True,
    )
    capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    resource_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    policy_set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "delegation_id ~ '^dlg_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="delegation_id_format",
        ),
        CheckConstraint(
            "(delegatee_employee_id IS NOT NULL) <> "
            "(delegatee_service_identity_id IS NOT NULL)",
            name="exactly_one_delegatee",
        ),
        CheckConstraint("status IN ('active', 'revoked', 'expired')", name="status_allowed"),
        CheckConstraint("starts_at < expires_at", name="starts_before_expires"),
        CheckConstraint(f"policy_version ~ '{_SEMVER_PATTERN}'", name="version_format"),
        Index(
            "ix_governance_delegations_delegator_employee_id",
            "delegator_employee_id",
        ),
        Index(
            "ix_governance_delegations_delegatee_employee_id",
            "delegatee_employee_id",
        ),
        Index(
            "ix_governance_delegations_delegatee_service_identity_id",
            "delegatee_service_identity_id",
        ),
    )


class GovernanceDecisionRecord(Base):
    """A persisted, immutable PDP decision (audit and idempotency anchor).

    The full decision document is reproducible from these fields plus the
    canonical hash.  Decisions are never mutated; a retry creates a new decision
    with a higher ``decision_revision`` and ``supersedes_decision_id``.
    """

    __tablename__ = "governance_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    decision_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    decision_hash: Mapped[str] = mapped_column(Text, nullable=False)
    decision_purpose: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'initial_execution'")
    )
    supersedes_decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    principal_type: Mapped[str] = mapped_column(Text, nullable=False)
    principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision_type: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    policy_set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "decision_id ~ '^gdec_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="decision_id_format",
        ),
        CheckConstraint(
            "decision_type IN ('allow', 'deny', 'needs_clarification', "
            "'needs_confirmation', 'needs_approval', "
            "'managed_execution_required', 'pre_evaluation')",
            name="decision_type_allowed",
        ),
        CheckConstraint(
            "decision_purpose IN ('initial_execution', 'retry')",
            name="decision_purpose_allowed",
        ),
        CheckConstraint(
            "principal_type IN ('person', 'service', 'runtime', 'agent_instance')",
            name="principal_type_allowed",
        ),
        CheckConstraint(
            f"decision_hash ~ '{_SHA256_PATTERN}'", name="decision_hash_format"
        ),
        CheckConstraint(
            f"request_hash ~ '{_SHA256_PATTERN}'", name="request_hash_format"
        ),
        CheckConstraint(
            "policy_content_hash IS NULL OR "
            f"policy_content_hash ~ '{_SHA256_PATTERN}'",
            name="policy_content_hash_format",
        ),
        CheckConstraint("decision_revision >= 1", name="revision_positive"),
        Index("ix_governance_decisions_task_id", "task_id"),
        Index(
            "ix_governance_decisions_task_principal",
            "task_id",
            "principal_id",
            "decision_revision",
        ),
        Index("ix_governance_decisions_created_at", "created_at"),
    )


class ExecutionGrant(TimestampMixin, Base):
    """A task-bound authorization snapshot issued from an authorizing decision.

    Every sensitive call must re-read the grant and re-check subject, task,
    capability, scope, policy/tool version, gates, expiry, revocation and
    remaining budget.  ``usable`` is only a cached observation, never a
    substitute for the per-call check.
    """

    __tablename__ = "execution_grants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    grant_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    decision_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("governance_decisions.decision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_hash: Mapped[str] = mapped_column(Text, nullable=False)
    decision_purpose: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'initial_execution'")
    )
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_service_identity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    delegation_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_mode: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    resource_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_set_id: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    tool_contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    tool_contract_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_contract_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    consumption_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'single_use'")
    )
    confirmation_ref: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    approval_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    usable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_principal_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "grant_id ~ '^grnt_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="grant_id_format",
        ),
        CheckConstraint(
            "idempotency_key ~ '^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="idempotency_key_format",
        ),
        CheckConstraint(
            "decision_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="decision_hash_format",
        ),
        CheckConstraint(
            f"policy_content_hash ~ '{_SHA256_PATTERN}'",
            name="policy_content_hash_format",
        ),
        CheckConstraint(
            "execution_mode IN ('advisory', 'governed_resource_access', "
            "'managed_execution')",
            name="execution_mode_allowed",
        ),
        CheckConstraint(
            "subject_type IN ('person', 'service', 'runtime', 'agent_instance')",
            name="subject_type_allowed",
        ),
        CheckConstraint(
            "status IN ('active', 'consumed', 'revoked', 'expired')",
            name="status_allowed",
        ),
        CheckConstraint(
            "consumption_mode IN ('single_use', 'multi_use')",
            name="consumption_mode_allowed",
        ),
        CheckConstraint(
            "decision_purpose IN ('initial_execution', 'retry')",
            name="decision_purpose_allowed",
        ),
        CheckConstraint("expires_at > issued_at", name="expires_after_issued"),
        CheckConstraint("task_revision >= 1", name="task_revision_positive"),
        CheckConstraint("decision_revision >= 1", name="decision_revision_positive"),
        Index("ix_execution_grants_decision_id", "decision_id"),
        Index("ix_execution_grants_task_id", "task_id"),
        Index("ix_execution_grants_status", "status"),
        Index("ix_execution_grants_expires_at", "expires_at"),
        Index(
            "ix_execution_grants_subject",
            "subject_type",
            "subject_id",
        ),
    )
