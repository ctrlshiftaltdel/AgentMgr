"""Service-layer facade for the governance domain.

This is intentionally a thin, transport-free facade.  It owns no sessions and
does not commit: the caller decides the transaction boundary, consistent with
``app.audit.service.record_event`` and the existing domain services.  No HTTP,
MCP or runtime-adapter code lives here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.governance import grants, pdp, pep, policies
from app.governance.models import ExecutionGrant, GovernancePolicyVersion
from app.governance.pdp import EvaluationRequest, GovernanceDecision
from app.governance.pep import EnforcementResult
from app.governance.principal import GovernancePrincipal

__all__ = [
    "GovernanceService",
    "create_policy_draft",
    "enforce_tool_call",
    "evaluate",
    "get_active_policy",
    "issue_grant",
    "publish_policy",
    "rollback_policy",
]


class GovernanceService:
    """Facade over the PDP, policy store, grant store and PEP."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- policy store -----------------------------------------------------

    def create_policy_draft(
        self,
        document: Mapping[str, Any],
        *,
        created_by_employee_id: int | None = None,
        actor_id: int | None = None,
        supersedes_id: int | None = None,
    ) -> GovernancePolicyVersion:
        return policies.create_draft(
            self.session,
            document=document,
            created_by_employee_id=created_by_employee_id,
            actor_id=actor_id,
            supersedes_id=supersedes_id,
        )

    # Short aliases matching the underlying store operations.
    def create_draft(self, document: Mapping[str, Any], **kwargs: Any) -> GovernancePolicyVersion:
        return self.create_policy_draft(document, **kwargs)

    def publish_policy(
        self,
        policy_version_id: int,
        *,
        published_by_employee_id: int | None = None,
        actor_id: int | None = None,
        now: datetime | None = None,
    ) -> GovernancePolicyVersion:
        return policies.publish(
            self.session,
            policy_version_id=policy_version_id,
            published_by_employee_id=published_by_employee_id,
            actor_id=actor_id,
            now=now,
        )

    def publish(self, policy_version_id: int, **kwargs: Any) -> GovernancePolicyVersion:
        return self.publish_policy(policy_version_id, **kwargs)

    def rollback_policy(
        self,
        *,
        policy_set_id: str,
        target_version: str,
        new_version: str | None = None,
        actor_id: int | None = None,
        now: datetime | None = None,
    ) -> GovernancePolicyVersion:
        return policies.rollback(
            self.session,
            policy_set_id=policy_set_id,
            target_version=target_version,
            new_version=new_version,
            actor_id=actor_id,
            now=now,
        )

    def get_active_policy(
        self, policy_set_id: str, version: str | None = None
    ) -> GovernancePolicyVersion | None:
        return policies.get_active(self.session, policy_set_id, version)

    def get_active(
        self, policy_set_id: str, version: str | None = None
    ) -> GovernancePolicyVersion | None:
        return self.get_active_policy(policy_set_id, version)

    def rollback(
        self,
        *,
        policy_set_id: str,
        target_version: str,
        new_version: str | None = None,
        actor_id: int | None = None,
        now: datetime | None = None,
    ) -> GovernancePolicyVersion:
        return self.rollback_policy(
            policy_set_id=policy_set_id,
            target_version=target_version,
            new_version=new_version,
            actor_id=actor_id,
            now=now,
        )

    # -- decision / grant / enforcement ----------------------------------

    def evaluate(
        self, request: EvaluationRequest, *, now: datetime | None = None
    ) -> GovernanceDecision:
        return pdp.evaluate(self.session, request, now=now)

    def issue_grant(
        self,
        *,
        principal: GovernancePrincipal,
        decision: GovernanceDecision | None = None,
        decision_id: str | None = None,
        task_id: str | None = None,
        task_revision: int | None = None,
        execution_mode: str | None = None,
        capabilities: Sequence[str] | None = None,
        resource_scope: Mapping[str, Any] | None = None,
        ttl_seconds: int | None = None,
        budget: Mapping[str, Any] | None = None,
        consumption_mode: str = "single_use",
        tool_contract_version: str = "1.0.0",
        tool_contract_id: str | None = None,
        tool_contract_hash: str | None = None,
        confirmation_ref: Mapping[str, Any] | None = None,
        approval_ref: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        actor_id: int | None = None,
        now: datetime | None = None,
    ) -> ExecutionGrant:
        return grants.issue_grant(
            self.session,
            principal=principal,
            decision=decision,
            decision_id=decision_id,
            task_id=task_id,
            task_revision=task_revision,
            execution_mode=execution_mode,
            capabilities=capabilities,
            resource_scope=resource_scope,
            ttl_seconds=ttl_seconds,
            budget=budget,
            consumption_mode=consumption_mode,
            tool_contract_version=tool_contract_version,
            tool_contract_id=tool_contract_id,
            tool_contract_hash=tool_contract_hash,
            confirmation_ref=confirmation_ref,
            approval_ref=approval_ref,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            now=now,
        )

    def consume_grant(self, **kwargs: Any) -> ExecutionGrant:
        return grants.consume_grant(self.session, **kwargs)

    def get_grant(self, grant_id: str, *, for_update: bool = False) -> ExecutionGrant | None:
        return grants.get_grant(self.session, grant_id, for_update=for_update)

    def revoke_grant(self, **kwargs: Any) -> ExecutionGrant:
        return grants.revoke_grant(self.session, **kwargs)

    def enforce_tool_call(self, **kwargs: Any) -> EnforcementResult:
        return pep.enforce_tool_call(self.session, **kwargs)


# Module-level conveniences.  They keep the same no-commit contract as the
# class: the caller owns the transaction boundary.

def evaluate(
    session: Session,
    request: EvaluationRequest | GovernancePrincipal | None = None,
    *,
    now: datetime | None = None,
    **request_fields: Any,
) -> GovernanceDecision:
    return pdp.evaluate(session, request, now=now, **request_fields)


def issue_grant(session: Session, **kwargs: Any) -> ExecutionGrant:
    return grants.issue_grant(session, **kwargs)


def enforce_tool_call(session: Session, **kwargs: Any) -> EnforcementResult:
    return pep.enforce_tool_call(session, **kwargs)


def create_policy_draft(session: Session, document: Mapping[str, Any], **kwargs: Any) -> GovernancePolicyVersion:
    return policies.create_draft(session, document, **kwargs)


def publish_policy(session: Session, policy_version_id: int, **kwargs: Any) -> GovernancePolicyVersion:
    return policies.publish(session, policy_version_id, **kwargs)


def rollback_policy(session: Session, **kwargs: Any) -> GovernancePolicyVersion:
    return policies.rollback(session, **kwargs)


def get_active_policy(
    session: Session, policy_set_id: str, version: str | None = None
) -> GovernancePolicyVersion | None:
    return policies.get_active(session, policy_set_id, version)
