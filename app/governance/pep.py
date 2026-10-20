"""Policy Enforcement Point (PEP) for controlled tool/API calls.

The PEP never trusts a prompt, a model, a client-supplied grant or a cached
``usable`` flag.  It re-reads the grant and the active policy version, verifies
the subject/task/revision/capability/scope/tool-version/gates, locks the grant
row and consumes one call atomically.  Both allow and deny paths write an audit
event with the actor, policy version and reason code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.governance.errors import GovernanceError
from app.governance.grants import consume_grant, get_grant, grant_remaining_calls
from app.governance.models import ExecutionGrant
from app.governance.policies import get_active
from app.governance.principal import GovernancePrincipal

__all__ = ["EnforcementResult", "enforce_tool_call"]


@dataclass(frozen=True)
class EnforcementResult:
    """The result of a successful PEP check."""

    grant: ExecutionGrant
    decision_id: str
    task_id: str
    task_revision: int
    capability: str
    policy_set_id: str
    policy_version: str
    remaining_calls: int


def _audit(
    session: Session,
    *,
    action: str,
    grant_id: str,
    actor_id: int | None,
    principal: GovernancePrincipal,
    details: Mapping[str, Any],
) -> None:
    record_event(
        session,
        actor_id=actor_id,
        action=action,
        resource_type="execution_grant",
        resource_id=grant_id,
        details={
            "grant_id": grant_id,
            "principal_id": principal.principal_id,
            "service_identity_id": principal.service_identity_id,
            **dict(details),
        },
    )


def _deny(
    session: Session,
    *,
    grant: ExecutionGrant | None,
    grant_id: str,
    principal: GovernancePrincipal,
    reason_code: str,
    call_id: str,
    capability: str,
    context: Mapping[str, Any] | None = None,
) -> None:
    details: dict[str, Any] = {
        "reason_code": reason_code,
        "call_id": call_id,
        "capability": capability,
    }
    if grant is not None:
        details.update(
            {
                "decision_id": grant.decision_id,
                "task_id": grant.task_id,
                "task_revision": grant.task_revision,
                "status": grant.status,
                "policy_set_id": grant.policy_set_id,
                "policy_version": grant.policy_version,
                "policy_content_hash": grant.policy_content_hash,
                "tool_contract_version": grant.tool_contract_version,
            }
        )
    if context:
        details.update(context)
    _audit(
        session,
        action="governance.pep.denied",
        grant_id=grant_id,
        actor_id=principal.employee_id,
        principal=principal,
        details=details,
    )
    raise GovernanceError(reason_code, context=context)


def enforce_tool_call(
    session: Session,
    *,
    grant_id: str,
    principal: GovernancePrincipal,
    task_id: str,
    task_revision: int,
    capability: str,
    call_id: str,
    resource_scope: Mapping[str, Any] | None = None,
    payload_hash: str | None = None,
    tool_contract_version: str | None = None,
    policy_set_id: str | None = None,
    policy_version: str | None = None,
    policy_content_hash: str | None = None,
    now: datetime | None = None,
) -> EnforcementResult:
    """Enforce one controlled call, or fail closed with an audit event."""
    try:
        principal.require_active()
    except GovernanceError as exc:
        _audit(
            session,
            action="governance.pep.denied",
            grant_id=grant_id,
            actor_id=principal.employee_id,
            principal=principal,
            details={
                "reason_code": exc.reason_code,
                "call_id": call_id,
                "capability": capability,
                **exc.context,
            },
        )
        raise
    grant = get_grant(session, grant_id, for_update=True)
    if grant is None:
        _deny(
            session,
            grant=None,
            grant_id=grant_id,
            principal=principal,
            reason_code="grant_missing",
            call_id=call_id,
            capability=capability,
        )

    # Policy version/content must still be the published one that issued the
    # grant.  A retired or replaced version invalidates the grant.
    resolved_set = policy_set_id or grant.policy_set_id
    resolved_version = policy_version or grant.policy_version
    if resolved_set != grant.policy_set_id or resolved_version != grant.policy_version:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="policy_version_invalid",
            call_id=call_id,
            capability=capability,
            context={"requested_policy_version": resolved_version},
        )
    try:
        active_policy = get_active(session, grant.policy_set_id, grant.policy_version)
    except GovernanceError as exc:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code=exc.reason_code,
            call_id=call_id,
            capability=capability,
            context=exc.context,
        )
    if active_policy is None:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="policy_version_invalid",
            call_id=call_id,
            capability=capability,
        )
    if active_policy.content_hash != grant.policy_content_hash:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="policy_version_invalid",
            call_id=call_id,
            capability=capability,
            context={"policy_content_hash_mismatch": True},
        )
    if policy_content_hash is not None and policy_content_hash != grant.policy_content_hash:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="policy_version_invalid",
            call_id=call_id,
            capability=capability,
            context={"policy_content_hash": policy_content_hash},
        )
    if (
        tool_contract_version is not None
        and tool_contract_version != grant.tool_contract_version
    ):
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="tool_contract_mismatch",
            call_id=call_id,
            capability=capability,
            context={"tool_contract_version": tool_contract_version},
        )

    if grant.subject_id != principal.principal_id or grant.subject_type != principal.principal_type:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="grant_scope_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "subject"},
        )
    if grant.subject_service_identity_id != principal.service_identity_id:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="grant_scope_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "service_identity"},
        )
    if grant.task_id != task_id or grant.task_revision != task_revision:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="grant_scope_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "task"},
        )

    try:
        consumed = consume_grant(
            session,
            grant_id=grant_id,
            principal=principal,
            task_id=task_id,
            task_revision=task_revision,
            capability=capability,
            call_id=call_id,
            resource_scope=resource_scope,
            payload_hash=payload_hash,
            tool_contract_version=tool_contract_version,
            now=now,
        )
    except GovernanceError as exc:
        _audit(
            session,
            action="governance.pep.denied",
            grant_id=grant_id,
            actor_id=principal.employee_id,
            principal=principal,
            details={
                "reason_code": exc.reason_code,
                "call_id": call_id,
                "capability": capability,
                "decision_id": grant.decision_id,
                "task_id": grant.task_id,
                "task_revision": grant.task_revision,
                "policy_set_id": grant.policy_set_id,
                "policy_version": grant.policy_version,
                "policy_content_hash": grant.policy_content_hash,
                **exc.context,
            },
        )
        raise

    _audit(
        session,
        action="governance.pep.allowed",
        grant_id=grant_id,
        actor_id=principal.employee_id,
        principal=principal,
        details={
            "call_id": call_id,
            "capability": capability,
            "decision_id": consumed.decision_id,
            "task_id": consumed.task_id,
            "task_revision": consumed.task_revision,
            "policy_set_id": consumed.policy_set_id,
            "policy_version": consumed.policy_version,
            "policy_content_hash": consumed.policy_content_hash,
            "tool_contract_version": consumed.tool_contract_version,
            "remaining_calls": grant_remaining_calls(consumed),
        },
    )
    return EnforcementResult(
        grant=consumed,
        decision_id=consumed.decision_id,
        task_id=consumed.task_id,
        task_revision=consumed.task_revision,
        capability=capability,
        policy_set_id=consumed.policy_set_id,
        policy_version=consumed.policy_version,
        remaining_calls=grant_remaining_calls(consumed),
    )
