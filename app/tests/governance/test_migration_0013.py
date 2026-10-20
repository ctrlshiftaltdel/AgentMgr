"""Migration 0013 creates and drops the governance tables (Issue #78)."""

from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, inspect

from app.tests.governance.helpers import alembic_config

GOVERNANCE_TABLES = frozenset(
    {
        "service_identities",
        "service_identity_roles",
        "governance_policy_versions",
        "governance_delegations",
        "governance_decisions",
        "execution_grants",
    }
)


def test_0013_creates_and_drops_governance_tables(test_db_url: str) -> None:
    engine = create_engine(test_db_url)
    config = alembic_config()
    try:
        command.downgrade(config, "0012_inbound_messages")
        before = set(inspect(engine).get_table_names())
        assert GOVERNANCE_TABLES.isdisjoint(before)

        command.upgrade(config, "head")
        after = set(inspect(engine).get_table_names())
        assert GOVERNANCE_TABLES.issubset(after)

        indexes = {
            index["name"]
            for index in inspect(engine).get_indexes("governance_policy_versions")
        }
        assert "uq_governance_policy_versions_published_policy_set" in indexes
        assert "ix_governance_policy_versions_policy_set_status" in indexes

        decision_columns = {
            column["name"]
            for column in inspect(engine).get_columns("governance_decisions")
        }
        assert {
            "decision_id",
            "decision_revision",
            "decision_hash",
            "reason_codes",
            "policy_set_id",
            "policy_version",
            "constraints",
            "document",
            "request_hash",
        }.issubset(decision_columns)
        decision_constraints = {
            constraint["name"]
            for constraint in inspect(engine).get_unique_constraints(
                "governance_decisions"
            )
        }
        assert (
            "uq_governance_decisions_task_principal_revision"
            in decision_constraints
        )

        grant_columns = {
            column["name"] for column in inspect(engine).get_columns("execution_grants")
        }
        assert {
            "grant_id",
            "decision_id",
            "decision_type",
            "decision_evaluated_at",
            "decision_expires_at",
            "task_id",
            "task_revision",
            "task_idempotency_key",
            "subject_id",
            "subject_employee_id",
            "issued_by_service_identity_id",
            "execution_mode",
            "capabilities",
            "resource_scope",
            "policy_version",
            "tool_contract_version",
            "expires_at",
            "budget",
            "usage",
            "confirmation_ref",
            "approval_ref",
            "status",
            "usable",
        }.issubset(grant_columns)
    finally:
        # Leave the database at head for the rest of the suite.
        command.upgrade(config, "head")
        engine.dispose()
