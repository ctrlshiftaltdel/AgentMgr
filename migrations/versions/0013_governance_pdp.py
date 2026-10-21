"""governance: service identities, versioned policy, PDP decisions and grants

Revision ID: 0013_governance_pdp
Revises: 0012_inbound_messages
Create Date: 2026-10-20

Issue #78 adds the service-layer identity/governance substrate on top of the
existing RBAC catalog (Issue #77 contracts, ADR-001):

* ``service_identities`` -- non-human connector identities with a human owner;
* ``service_identity_roles`` -- reuse the existing ``roles`` table, no second
  role/permission model;
* ``governance_policy_versions`` -- immutable versioned policy documents with a
  single published version per policy set (partial unique index);
* ``governance_delegations`` -- scoped, time-bounded, revocable delegation;
* ``governance_decisions`` -- persisted PDP decisions (audit/idempotency anchor);
* ``execution_grants`` -- task-bound authorization snapshots consumed by the PEP.

All enums are ``TEXT`` + ``CHECK``; structured documents are JSONB.  The
migration is reversible and does not rewrite any existing revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0013_governance_pdp"
down_revision = "0012_inbound_messages"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "service_identities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_employee_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'active'"), nullable=False
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["owner_employee_id"],
            ["employees.id"],
            name="fk_service_identities_owner_employee_id_employees",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_service_identities"),
        sa.UniqueConstraint("name", name="uq_service_identities_name"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="status_allowed"),
    )
    op.create_index(
        "ix_service_identities_owner_employee_id",
        "service_identities",
        ["owner_employee_id"],
    )

    op.create_table(
        "service_identity_roles",
        sa.Column("service_identity_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_identity_id"],
            ["service_identities.id"],
            name="fk_service_identity_roles_service_identity_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_service_identity_roles_role_id_roles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "service_identity_id", "role_id", name="pk_service_identity_roles"
        ),
    )

    op.create_table(
        "governance_policy_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("policy_set_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column(
            "document", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_by_employee_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_employee_id"],
            ["employees.id"],
            name="fk_governance_policy_versions_created_by_employee_id_employees",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["governance_policy_versions.id"],
            name="fk_governance_policy_versions_supersedes_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_governance_policy_versions"),
        sa.UniqueConstraint(
            "policy_set_id",
            "version",
            name="uq_governance_policy_versions_policy_set_version",
        ),
        sa.CheckConstraint(
            "policy_set_id ~ '^polset_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="policy_set_id_format",
        ),
        sa.CheckConstraint(
            "version ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$'", name="version_format"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'retired')", name="status_allowed"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash_format"
        ),
    )
    op.create_index(
        "ix_governance_policy_versions_policy_set_status",
        "governance_policy_versions",
        ["policy_set_id", "status"],
    )
    op.create_index(
        "uq_governance_policy_versions_published_policy_set",
        "governance_policy_versions",
        ["policy_set_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )

    op.create_table(
        "governance_delegations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("delegation_id", sa.Text(), nullable=True),
        sa.Column("delegator_employee_id", sa.BigInteger(), nullable=False),
        sa.Column("delegatee_employee_id", sa.BigInteger(), nullable=True),
        sa.Column("delegatee_service_identity_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "resource_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("policy_set_id", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "starts_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["delegator_employee_id"],
            ["employees.id"],
            name="fk_governance_delegations_delegator_employee_id_employees",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delegatee_employee_id"],
            ["employees.id"],
            name="fk_governance_delegations_delegatee_employee_id_employees",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["delegatee_service_identity_id"],
            ["service_identities.id"],
            name="fk_governance_delegations_delegatee_service_identity_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_governance_delegations"),
        sa.UniqueConstraint(
            "delegation_id", name="uq_governance_delegations_delegation_id"
        ),
        sa.CheckConstraint(
            "delegation_id ~ '^dlg_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="delegation_id_format",
        ),
        sa.CheckConstraint(
            "(delegatee_employee_id IS NOT NULL) <> "
            "(delegatee_service_identity_id IS NOT NULL)",
            name="exactly_one_delegatee",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'expired')", name="status_allowed"
        ),
        sa.CheckConstraint("starts_at < expires_at", name="starts_before_expires"),
        sa.CheckConstraint(
            "policy_version ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$'", name="version_format"
        ),
    )
    op.create_index(
        "ix_governance_delegations_delegator_employee_id",
        "governance_delegations",
        ["delegator_employee_id"],
    )
    op.create_index(
        "ix_governance_delegations_delegatee_employee_id",
        "governance_delegations",
        ["delegatee_employee_id"],
    )
    op.create_index(
        "ix_governance_delegations_delegatee_service_identity_id",
        "governance_delegations",
        ["delegatee_service_identity_id"],
    )

    op.create_table(
        "governance_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("decision_id", sa.Text(), nullable=False),
        sa.Column(
            "decision_revision",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("decision_hash", sa.Text(), nullable=False),
        sa.Column(
            "decision_purpose",
            sa.Text(),
            server_default=sa.text("'initial_execution'"),
            nullable=False,
        ),
        sa.Column("supersedes_decision_id", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("task_revision", sa.Integer(), nullable=True),
        sa.Column("principal_type", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("decision_type", sa.Text(), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("policy_set_id", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=True),
        sa.Column("policy_content_hash", sa.Text(), nullable=True),
        sa.Column(
            "constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "document",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_governance_decisions"),
        sa.UniqueConstraint("decision_id", name="uq_governance_decisions_decision_id"),
        sa.UniqueConstraint(
            "task_id",
            "principal_id",
            "decision_revision",
            name="uq_governance_decisions_task_principal_revision",
        ),
        sa.CheckConstraint(
            "decision_id ~ '^gdec_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="decision_id_format",
        ),
        sa.CheckConstraint(
            "decision_type IN ('allow', 'deny', 'needs_clarification', "
            "'needs_confirmation', 'needs_approval', "
            "'managed_execution_required', 'pre_evaluation')",
            name="decision_type_allowed",
        ),
        sa.CheckConstraint(
            "decision_purpose IN ('initial_execution', 'retry')",
            name="decision_purpose_allowed",
        ),
        sa.CheckConstraint(
            "principal_type IN ('person', 'service', 'runtime', 'agent_instance')",
            name="principal_type_allowed",
        ),
        sa.CheckConstraint(
            "decision_hash ~ '^sha256:[a-f0-9]{64}$'", name="decision_hash_format"
        ),
        sa.CheckConstraint(
            "request_hash ~ '^sha256:[a-f0-9]{64}$'", name="request_hash_format"
        ),
        sa.CheckConstraint(
            "policy_content_hash IS NULL OR "
            "policy_content_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="policy_content_hash_format",
        ),
        sa.CheckConstraint("decision_revision >= 1", name="revision_positive"),
    )
    op.create_index(
        "ix_governance_decisions_task_id", "governance_decisions", ["task_id"]
    )
    op.create_index(
        "ix_governance_decisions_task_principal",
        "governance_decisions",
        ["task_id", "principal_id", "decision_revision"],
    )
    op.create_index(
        "ix_governance_decisions_created_at", "governance_decisions", ["created_at"]
    )

    op.create_table(
        "execution_grants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("grant_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("decision_id", sa.Text(), nullable=False),
        sa.Column("decision_revision", sa.Integer(), nullable=False),
        sa.Column("decision_hash", sa.Text(), nullable=False),
        sa.Column(
            "decision_purpose",
            sa.Text(),
            server_default=sa.text("'initial_execution'"),
            nullable=False,
        ),
        sa.Column(
            "decision_type", sa.Text(), server_default=sa.text("'allow'"), nullable=False
        ),
        sa.Column("decision_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("task_idempotency_key", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("subject_employee_id", sa.BigInteger(), nullable=True),
        sa.Column("subject_service_identity_id", sa.Text(), nullable=True),
        sa.Column("issued_by_service_identity_id", sa.Text(), nullable=True),
        sa.Column("delegation_ref", sa.Text(), nullable=True),
        sa.Column("execution_mode", sa.Text(), nullable=False),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "resource_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("policy_set_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("policy_content_hash", sa.Text(), nullable=False),
        sa.Column("tool_contract_version", sa.Text(), nullable=False),
        sa.Column("tool_contract_id", sa.Text(), nullable=True),
        sa.Column("tool_contract_hash", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "budget",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "consumption_mode",
            sa.Text(),
            server_default=sa.text("'single_use'"),
            nullable=False,
        ),
        sa.Column(
            "confirmation_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "approval_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column(
            "usable", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_call_id", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_principal_id", sa.Text(), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["governance_decisions.decision_id"],
            name="fk_execution_grants_decision_id_governance_decisions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_grants"),
        sa.UniqueConstraint("grant_id", name="uq_execution_grants_grant_id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_execution_grants_idempotency_key"
        ),
        sa.CheckConstraint(
            "grant_id ~ '^grnt_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="grant_id_format",
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="idempotency_key_format",
        ),
        sa.CheckConstraint(
            "task_id ~ '^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="task_id_format",
        ),
        sa.CheckConstraint(
            "task_idempotency_key ~ '^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="task_idempotency_key_format",
        ),
        sa.CheckConstraint(
            "issued_by_service_identity_id IS NULL OR "
            "issued_by_service_identity_id ~ "
            "'^svc_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="issued_by_service_identity_id_format",
        ),
        sa.CheckConstraint(
            "decision_hash ~ '^sha256:[a-f0-9]{64}$'", name="decision_hash_format"
        ),
        sa.CheckConstraint(
            "policy_content_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="policy_content_hash_format",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('advisory', 'governed_resource_access', "
            "'managed_execution')",
            name="execution_mode_allowed",
        ),
        sa.CheckConstraint(
            "subject_type IN ('person', 'service', 'runtime', 'agent_instance')",
            name="subject_type_allowed",
        ),
        sa.CheckConstraint(
            "subject_id ~ '^(prin|rt|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="subject_id_format",
        ),
        sa.CheckConstraint(
            "delegation_ref IS NULL OR "
            "delegation_ref ~ '^dlg_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="delegation_ref_format",
        ),
        sa.CheckConstraint(
            "NOT (consumption_mode = 'single_use' AND "
            "(budget ->> 'max_calls')::int <> 1)",
            name="single_use_budget_is_one",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'consumed', 'revoked', 'expired')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "consumption_mode IN ('single_use', 'multi_use')",
            name="consumption_mode_allowed",
        ),
        sa.CheckConstraint(
            "decision_purpose IN ('initial_execution', 'retry')",
            name="decision_purpose_allowed",
        ),
        sa.CheckConstraint("expires_at > issued_at", name="expires_after_issued"),
        sa.CheckConstraint("task_revision >= 1", name="task_revision_positive"),
        sa.CheckConstraint(
            "decision_revision >= 1", name="decision_revision_positive"
        ),
    )
    op.create_index(
        "ix_execution_grants_decision_id", "execution_grants", ["decision_id"]
    )
    op.create_index("ix_execution_grants_task_id", "execution_grants", ["task_id"])
    op.create_index("ix_execution_grants_status", "execution_grants", ["status"])
    op.create_index(
        "ix_execution_grants_expires_at", "execution_grants", ["expires_at"]
    )
    op.create_index(
        "ix_execution_grants_subject",
        "execution_grants",
        ["subject_type", "subject_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_grants_subject", table_name="execution_grants")
    op.drop_index("ix_execution_grants_expires_at", table_name="execution_grants")
    op.drop_index("ix_execution_grants_status", table_name="execution_grants")
    op.drop_index("ix_execution_grants_task_id", table_name="execution_grants")
    op.drop_index("ix_execution_grants_decision_id", table_name="execution_grants")
    op.drop_table("execution_grants")

    op.drop_index(
        "ix_governance_decisions_created_at", table_name="governance_decisions"
    )
    op.drop_index(
        "ix_governance_decisions_task_principal", table_name="governance_decisions"
    )
    op.drop_index(
        "ix_governance_decisions_task_id", table_name="governance_decisions"
    )
    op.drop_table("governance_decisions")

    op.drop_index(
        "ix_governance_delegations_delegatee_service_identity_id",
        table_name="governance_delegations",
    )
    op.drop_index(
        "ix_governance_delegations_delegatee_employee_id",
        table_name="governance_delegations",
    )
    op.drop_index(
        "ix_governance_delegations_delegator_employee_id",
        table_name="governance_delegations",
    )
    op.drop_table("governance_delegations")

    op.drop_index(
        "uq_governance_policy_versions_published_policy_set",
        table_name="governance_policy_versions",
    )
    op.drop_index(
        "ix_governance_policy_versions_policy_set_status",
        table_name="governance_policy_versions",
    )
    op.drop_table("governance_policy_versions")

    op.drop_table("service_identity_roles")

    op.drop_index(
        "ix_service_identities_owner_employee_id",
        table_name="service_identities",
    )
    op.drop_table("service_identities")
