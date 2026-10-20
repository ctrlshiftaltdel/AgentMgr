"""Policy Enforcement Point (PEP) for controlled tool/API calls.

The PEP never trusts a prompt, a model, a client-supplied grant or a cached
``usable`` flag.  It re-reads the grant and the active policy version, verifies
the subject/task/revision/capability/scope/tool-version/gates, re-resolves the
subject against the identity directory, and consumes one call atomically.
Both allow and deny paths write an audit event with the actor, policy version
and reason code.

Transaction contract
--------------------
The PEP does not commit.  A denial audit event is written in the caller's
transaction **before** :class:`GovernanceError` is raised; callers that need the
denial to survive must commit the transaction even when enforcement raises.
``session.commit()`` after catching the error is the mandatory durable path and
is covered by a test.  A rollback discards the denial audit by design.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.governance.errors import GovernanceError, PrincipalResolutionError
from app.governance.grants import (
    _consume_grant,
    _current_revision,
    _load_decision,
    get_grant,
    grant_remaining_calls,
)
from app.governance.models import ExecutionGrant
from app.governance.pdp import (
    ApprovalEvidenceVerifier,
    _validate_delegation,
)
from app.governance.policies import (
    CAPABILITY_PATTERN,
    get_active,
    parse_policy_document,
    scope_is_subset,
)
from app.governance.principal import (
    GovernancePrincipal,
    reject_client_asserted_identity,
    resolve_employee_principal,
    resolve_service_principal,
)

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


def _refresh_principal(
    session: Session, principal: GovernancePrincipal
) -> GovernancePrincipal:
    """Re-resolve the subject from the directory, refusing stale authority."""
    if principal.principal_type in {"person", "agent_instance"}:
        if principal.employee_id is None:
            raise PrincipalResolutionError(
                "principal_unbound", context={"field": "employee_id"}
            )
        refreshed = resolve_employee_principal(
            session,
            employee_id=principal.employee_id,
            identity_assurance=principal.identity_assurance,
            principal_type=principal.principal_type,
        )
        return replace(refreshed, delegation_ref=principal.delegation_ref)

    if principal.principal_type == "service":
        if principal.service_identity_pk is None and principal.service_identity_id is None:
            raise PrincipalResolutionError(
                "principal_unbound", context={"field": "service_identity_id"}
            )
        requester = None
        if principal.employee_id is not None:
            requester = resolve_employee_principal(
                session,
                employee_id=principal.employee_id,
                identity_assurance=principal.identity_assurance,
            )
        return resolve_service_principal(
            session,
            service_identity_id=(
                principal.service_identity_pk
                if principal.service_identity_pk is not None
                else principal.service_identity_id
            ),
            requester=requester,
            identity_assurance=principal.identity_assurance,
            delegation_ref=principal.delegation_ref,
        )

    raise PrincipalResolutionError(
        "identity_unverified", context={"principal_type": principal.principal_type}
    )


def _verify_decision_binding(
    session: Session,
    *,
    grant: ExecutionGrant,
    principal: GovernancePrincipal,
    policy,
    policy_row,
) -> None:
    """Re-read the decision and verify every grant binding against it."""
    decision = _load_decision(session, grant.decision_id)
    document = decision.document
    if decision.decision_type not in {"allow", "managed_execution_required"}:
        raise GovernanceError("policy_denied", context={"field": "decision_type"})
    if (
        decision.decision_revision != grant.decision_revision
        or decision.decision_hash != grant.decision_hash
        or decision.decision_type != grant.decision_type
        or decision.decision_purpose != grant.decision_purpose
        or decision.principal_id != grant.subject_id
        or decision.principal_type != grant.subject_type
        or decision.task_id != grant.task_id
        or decision.task_revision != grant.task_revision
    ):
        raise GovernanceError(
            "grant_scope_mismatch", context={"field": "decision_binding"}
        )
    decision_delegation = (document.get("subject") or {}).get("delegation_ref")
    if (decision_delegation or None) != (grant.delegation_ref or None):
        raise GovernanceError(
            "grant_scope_mismatch", context={"field": "delegation_ref"}
        )
    if decision.policy_set_id != grant.policy_set_id or decision.policy_version != grant.policy_version:
        raise GovernanceError("policy_version_invalid")
    if decision.policy_content_hash != grant.policy_content_hash:
        raise GovernanceError(
            "policy_version_invalid", context={"reason": "policy_content_hash"}
        )
    if _current_revision(
        session, task_id=grant.task_id, principal_id=grant.subject_id
    ) > grant.decision_revision:
        raise GovernanceError(
            "decision_revision_stale",
            context={"decision_revision": grant.decision_revision},
        )
    if grant.expires_at > decision.expires_at:
        raise GovernanceError("grant_outlives_decision")

    constraints = document.get("constraints") or {}
    if grant.execution_mode != constraints.get("execution_mode"):
        raise GovernanceError("execution_mode_mismatch")
    allowed = set(constraints.get("allowed_capabilities") or [])
    denied = set(constraints.get("denied_capabilities") or [])
    if not set(grant.capabilities).issubset(allowed) or set(grant.capabilities) & denied:
        raise GovernanceError("capability_not_granted")
    if not scope_is_subset(grant.resource_scope, constraints.get("resource_scope") or {}):
        raise GovernanceError(
            "grant_scope_mismatch", context={"field": "resource_scope"}
        )
    if bool(constraints.get("required_confirmation")) != (
        grant.confirmation_ref is not None
    ):
        raise GovernanceError("confirmation_missing", context={"field": "gates"})
    if bool(constraints.get("required_approval")) != (grant.approval_ref is not None):
        raise GovernanceError("approval_missing", context={"field": "gates"})
    if grant.confirmation_ref is not None:
        if dict(grant.confirmation_ref) != dict(constraints.get("confirmation_ref") or {}):
            # A consumed confirmation legitimately differs from the decision
            # snapshot; only the identity and payload binding must persist.
            if (
                grant.confirmation_ref.get("confirmation_id")
                != (constraints.get("confirmation_ref") or {}).get("confirmation_id")
                or grant.confirmation_ref.get("payload_hash")
                != (constraints.get("confirmation_ref") or {}).get("payload_hash")
            ):
                raise GovernanceError("confirmation_missing")
    if grant.approval_ref is not None:
        if (
            grant.approval_ref.get("approval_id")
            != (constraints.get("approval_ref") or {}).get("approval_id")
            or grant.approval_ref.get("payload_hash")
            != (constraints.get("approval_ref") or {}).get("payload_hash")
        ):
            raise GovernanceError("approval_missing")

    # Delegation provenance must still be live.
    if grant.delegation_ref is not None:
        rule = policy.rule_for(grant.capabilities[0])
        if rule is None:
            raise GovernanceError("capability_not_granted")
        _validate_delegation(
            session,
            reference=grant.delegation_ref,
            principal=principal,
            rule=rule,
            policy_row=policy_row,
            capability=grant.capabilities[0],
            scope=grant.resource_scope,
            now=datetime.now(UTC),
        )


def _recheck_rbac(
    *,
    principal: GovernancePrincipal,
    policy,
    capability: str,
    resource_scope: Mapping[str, Any],
) -> None:
    """Re-check the existing RBAC permission triples for the grant's rule."""
    rule = policy.rule_for(capability)
    if rule is None or rule.effect != "allow":
        raise GovernanceError("capability_not_granted")
    if rule.required_permissions and not set(rule.required_permissions).issubset(
        principal.effective_permissions
    ):
        raise GovernanceError(
            "capability_not_granted", context={"field": "rbac_permission"}
        )
    if resource_scope.get("scope_type") not in rule.allowed_scope_types:
        raise GovernanceError("resource_scope_denied")


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
    task_idempotency_key: str | None = None,
    cost_units: float = 0.0,
    policy_set_id: str | None = None,
    policy_version: str | None = None,
    policy_content_hash: str | None = None,
    evidence_verifier: ApprovalEvidenceVerifier | None = None,
    client_payload: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> EnforcementResult:
    """Enforce one controlled call, or fail closed with an audit event.

    ``resource_scope``, ``tool_contract_version`` and (for gated grants)
    ``payload_hash`` are mandatory.  Passing ``None`` is a denial, never a
    skipped check.
    """
    try:
        principal.require_active()
        reject_client_asserted_identity(client_payload)
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

    # Mandatory context: omission fails closed.
    if resource_scope is None:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="grant_scope_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "resource_scope", "reason": "missing_context"},
        )
    if tool_contract_version is None:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="tool_contract_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "tool_contract_version", "reason": "missing_context"},
        )
    if (
        (grant.confirmation_ref is not None or grant.approval_ref is not None)
        and payload_hash is None
    ):
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="payload_hash_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "payload_hash", "reason": "missing_context"},
        )
    if not isinstance(capability, str) or not CAPABILITY_PATTERN.fullmatch(capability):
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="capability_not_granted",
            call_id=call_id,
            capability=capability,
            context={"field": "capability"},
        )

    # Re-resolve the subject; a disabled employee, revoked service identity or
    # removed role invalidates the grant even if the row still says active.
    try:
        refreshed = _refresh_principal(session, principal)
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
    if refreshed.principal_id != principal.principal_id:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="grant_scope_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "subject_identity"},
        )
    if refreshed.service_identity_id != grant.subject_service_identity_id:
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
    if refreshed.employee_id != grant.subject_employee_id:
        _deny(
            session,
            grant=grant,
            grant_id=grant_id,
            principal=principal,
            reason_code="grant_scope_mismatch",
            call_id=call_id,
            capability=capability,
            context={"field": "requester_identity"},
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
    if tool_contract_version != grant.tool_contract_version:
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

    # Decision document + binding + current RBAC authority.
    try:
        policy = parse_policy_document(active_policy.document)
        _verify_decision_binding(
            session,
            grant=grant,
            principal=refreshed,
            policy=policy,
            policy_row=active_policy,
        )
        _recheck_rbac(
            principal=refreshed,
            policy=policy,
            capability=capability,
            resource_scope=resource_scope,
        )
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

    try:
        consumed = _consume_grant(
            session,
            grant_id=grant_id,
            principal=refreshed,
            task_id=task_id,
            task_revision=task_revision,
            task_idempotency_key=task_idempotency_key,
            capability=capability,
            call_id=call_id,
            resource_scope=resource_scope,
            payload_hash=payload_hash,
            tool_contract_version=tool_contract_version,
            cost_units=cost_units,
            evidence_verifier=evidence_verifier,
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
