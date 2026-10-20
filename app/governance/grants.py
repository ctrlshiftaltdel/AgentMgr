"""Execution Grant issuance, lookup, consumption and revocation.

A grant is a **task-bound authorization snapshot**.  It is issued only from an
authorizing PDP decision (``allow`` / ``managed_execution_required``) and can
never exceed that decision's capabilities, resource scope, execution mode, TTL,
budget or gates.  Every sensitive call must re-read the grant; ``usable`` is a
cached observation, not an authority.

Consumption is atomic: the grant row is locked with ``SELECT ... FOR UPDATE``
before the usage counter or terminal status changes, so concurrent callers
cannot over-consume a single-use or budgeted grant.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.governance.errors import (
    GRANT_REVOCATION_REASONS,
    GovernanceError,
)
from app.governance.models import ExecutionGrant, GovernanceDecisionRecord
from app.governance.pdp import (
    GovernanceDecision,
    validate_approval_ref,
    validate_confirmation_ref,
)
from app.governance.policies import (
    EXECUTION_MODES,
    get_active,
    scope_is_subset,
    validate_resource_scope,
)
from app.governance.principal import GovernancePrincipal

__all__ = [
    "consume_grant",
    "get_grant",
    "grant_remaining_calls",
    "issue_grant",
    "revoke_grant",
]

_AUTHORIZING_DECISIONS = frozenset({"allow", "managed_execution_required"})
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_IDEM_KEY = re.compile(r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _normalize_idempotency_key(value: str | None) -> str:
    if value is None:
        return _new_id("idem_")
    if not isinstance(value, str) or not _IDEM_KEY.fullmatch(value):
        raise GovernanceError("required_input_missing", context={"field": "idempotency_key"})
    return value


def _resolve_decision(
    session: Session,
    *,
    decision: GovernanceDecision | GovernanceDecisionRecord | None,
    decision_id: str | None,
) -> GovernanceDecisionRecord:
    if decision is not None:
        if isinstance(decision, GovernanceDecisionRecord):
            return decision
        if decision_id is None:
            decision_id = decision.decision_id
    if not decision_id:
        raise GovernanceError("grant_missing", context={"field": "decision"})
    row = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == decision_id
        )
    )
    if row is None:
        raise GovernanceError("grant_missing", context={"decision_id": decision_id})
    return row


def _capabilities(value: Sequence[str] | None, constraints: Mapping[str, Any]) -> list[str]:
    if value is None:
        value = constraints.get("allowed_capabilities", [])
    if not isinstance(value, (list, tuple)) or not value:
        raise GovernanceError("capability_not_granted")
    capabilities: list[str] = []
    for item in value:
        if not isinstance(item, str) or item in capabilities:
            raise GovernanceError("capability_not_granted")
        capabilities.append(item)
    allowed = set(constraints.get("allowed_capabilities", []))
    denied = set(constraints.get("denied_capabilities", []))
    if any(item in denied for item in capabilities):
        raise GovernanceError("capability_not_granted")
    if not set(capabilities).issubset(allowed):
        raise GovernanceError("capability_not_granted")
    return capabilities


def _normalize_budget(
    budget: Mapping[str, Any] | None, constraints: Mapping[str, Any], ttl_seconds: int
) -> dict[str, Any]:
    raw = dict(budget or {})
    unknown = set(raw) - {
        "max_calls",
        "max_cost_units",
        "max_runtime_seconds",
        "currency",
    }
    if unknown:
        raise GovernanceError("budget_exceeded", context={"unknown": sorted(unknown)})

    max_calls = raw.get("max_calls", constraints.get("max_calls", 0))
    if (
        not isinstance(max_calls, int)
        or isinstance(max_calls, bool)
        or max_calls < 1
        or max_calls > constraints.get("max_calls", 0)
    ):
        raise GovernanceError("budget_exceeded", context={"field": "max_calls"})

    decision_max_cost = constraints.get("max_cost_units")
    max_cost_units = raw.get("max_cost_units", decision_max_cost)
    if max_cost_units is not None:
        if isinstance(max_cost_units, bool) or not isinstance(
            max_cost_units, (int, float)
        ):
            raise GovernanceError("budget_exceeded", context={"field": "max_cost_units"})
        if max_cost_units < 0 or (
            decision_max_cost is not None and max_cost_units > decision_max_cost
        ):
            raise GovernanceError("budget_exceeded", context={"field": "max_cost_units"})

    decision_max_runtime = constraints.get("max_runtime_seconds")
    if "max_runtime_seconds" in raw:
        max_runtime_seconds = raw.get("max_runtime_seconds")
    elif decision_max_runtime is not None:
        max_runtime_seconds = min(decision_max_runtime, ttl_seconds)
    else:
        max_runtime_seconds = None
    if max_runtime_seconds is not None:
        if (
            not isinstance(max_runtime_seconds, int)
            or isinstance(max_runtime_seconds, bool)
            or max_runtime_seconds < 1
        ):
            raise GovernanceError(
                "budget_exceeded", context={"field": "max_runtime_seconds"}
            )
        if max_runtime_seconds > ttl_seconds or (
            decision_max_runtime is not None
            and max_runtime_seconds > decision_max_runtime
        ):
            raise GovernanceError(
                "budget_exceeded", context={"field": "max_runtime_seconds"}
            )

    currency = raw.get("currency", "NONE" if max_cost_units is None else "CNY")
    if currency not in {"CNY", "USD", "NONE"}:
        raise GovernanceError("budget_exceeded", context={"field": "currency"})

    return {
        "max_calls": max_calls,
        "max_cost_units": max_cost_units,
        "max_runtime_seconds": max_runtime_seconds,
        "currency": currency,
    }


def _default_usage() -> dict[str, Any]:
    return {
        "calls_consumed": 0,
        "cost_consumed": None,
        "last_call_at": None,
        "concurrent_calls": 0,
    }


def _audit_grant(
    session: Session,
    *,
    actor_id: int | None,
    action: str,
    grant: ExecutionGrant,
    extra: Mapping[str, Any] | None = None,
) -> None:
    details: dict[str, Any] = {
        "grant_id": grant.grant_id,
        "decision_id": grant.decision_id,
        "task_id": grant.task_id,
        "task_revision": grant.task_revision,
        "subject_id": grant.subject_id,
        "execution_mode": grant.execution_mode,
        "capabilities": grant.capabilities,
        "policy_set_id": grant.policy_set_id,
        "policy_version": grant.policy_version,
        "policy_content_hash": grant.policy_content_hash,
        "tool_contract_version": grant.tool_contract_version,
        "status": grant.status,
    }
    if extra:
        details.update(extra)
    record_event(
        session,
        actor_id=actor_id,
        action=action,
        resource_type="execution_grant",
        resource_id=grant.grant_id,
        request_id=grant.correlation_id,
        details=details,
    )


def issue_grant(
    session: Session,
    *,
    principal: GovernancePrincipal,
    decision: GovernanceDecision | GovernanceDecisionRecord | None = None,
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
    """Issue a grant from an authorizing decision, refusing any expansion."""
    issued_at = now or _utcnow()
    principal.require_active()
    record = _resolve_decision(session, decision=decision, decision_id=decision_id)

    if record.decision_type not in _AUTHORIZING_DECISIONS:
        raise GovernanceError(
            "policy_denied", context={"decision_type": record.decision_type}
        )
    if record.principal_id != principal.principal_id or record.principal_type != principal.principal_type:
        raise GovernanceError("grant_scope_mismatch", context={"field": "subject"})
    if record.expires_at <= issued_at:
        raise GovernanceError("decision_expired", context={"decision_id": record.decision_id})
    if not record.task_id or record.task_revision is None:
        raise GovernanceError("task_not_created", context={"decision_id": record.decision_id})
    if task_id is None:
        task_id = record.task_id
    if task_revision is None:
        task_revision = record.task_revision
    if task_id != record.task_id or task_revision != record.task_revision:
        raise GovernanceError("grant_scope_mismatch", context={"field": "task"})
    if not isinstance(task_revision, int) or isinstance(task_revision, bool) or task_revision < 1:
        raise GovernanceError("task_not_created")

    constraints = record.constraints
    decision_mode = constraints.get("execution_mode")
    if decision_mode not in EXECUTION_MODES:
        raise GovernanceError("execution_mode_mismatch")
    mode = execution_mode or decision_mode
    if mode != decision_mode:
        raise GovernanceError("execution_mode_mismatch")

    granted_capabilities = _capabilities(capabilities, constraints)
    scope = validate_resource_scope(
        resource_scope if resource_scope is not None else constraints.get("resource_scope"),
        require_concrete=True,
    )
    if not scope_is_subset(scope, constraints.get("resource_scope", {})):
        raise GovernanceError("grant_scope_mismatch", context={"field": "resource_scope"})

    decision_ttl = constraints.get("ttl_seconds", 0)
    remaining_seconds = int((record.expires_at - issued_at).total_seconds())
    if ttl_seconds is None:
        # Inherit the decision TTL, but never outlive the decision when some
        # time has elapsed between evaluation and issuance.
        ttl = min(decision_ttl, remaining_seconds)
    else:
        ttl = ttl_seconds
    if (
        not isinstance(ttl, int)
        or isinstance(ttl, bool)
        or ttl < 1
        or ttl > decision_ttl
        or ttl > remaining_seconds
    ):
        raise GovernanceError("grant_outlives_decision", context={"field": "ttl_seconds"})
    expires_at = issued_at + timedelta(seconds=ttl)
    if expires_at > record.expires_at:
        raise GovernanceError("grant_outlives_decision")

    normalized_budget = _normalize_budget(budget, constraints, ttl)
    if not isinstance(tool_contract_version, str) or not _SEMVER.fullmatch(
        tool_contract_version
    ):
        raise GovernanceError("tool_contract_mismatch")

    if consumption_mode not in {"single_use", "multi_use"}:
        raise GovernanceError("unsupported_execution_mode", context={"field": "consumption_mode"})
    required_confirmation = bool(constraints.get("required_confirmation"))
    required_approval = bool(constraints.get("required_approval"))
    if required_confirmation and consumption_mode != "single_use":
        raise GovernanceError("grant_scope_mismatch", context={"field": "consumption_mode"})

    if record.policy_set_id is None or record.policy_version is None:
        raise GovernanceError("policy_version_invalid")
    policy_row = get_active(session, record.policy_set_id, record.policy_version)
    if policy_row is None:
        raise GovernanceError("policy_version_invalid")
    if (
        record.policy_content_hash is not None
        and record.policy_content_hash != policy_row.content_hash
    ):
        raise GovernanceError("policy_version_invalid")

    if required_confirmation:
        if confirmation_ref is None:
            raise GovernanceError("confirmation_missing")
        confirmation_ref = validate_confirmation_ref(confirmation_ref, now=issued_at)
    elif confirmation_ref is not None:
        confirmation_ref = validate_confirmation_ref(confirmation_ref, now=issued_at)
    if required_approval:
        if approval_ref is None:
            raise GovernanceError("approval_missing")
        approval_ref = validate_approval_ref(
            approval_ref, principal_id=principal.principal_id, now=issued_at
        )
    elif approval_ref is not None:
        approval_ref = validate_approval_ref(
            approval_ref, principal_id=principal.principal_id, now=issued_at
        )

    key = _normalize_idempotency_key(idempotency_key)
    existing = session.scalar(
        select(ExecutionGrant).where(ExecutionGrant.idempotency_key == key)
    )
    if existing is not None:
        same = (
            existing.decision_id == record.decision_id
            and existing.task_id == task_id
            and existing.task_revision == task_revision
            and existing.subject_id == principal.principal_id
            and existing.subject_type == principal.principal_type
            and existing.capabilities == granted_capabilities
            and existing.resource_scope == scope
            and existing.execution_mode == mode
            and existing.consumption_mode == consumption_mode
            and existing.tool_contract_version == tool_contract_version
            and existing.budget == normalized_budget
        )
        if not same:
            raise GovernanceError("replay_detected", context={"idempotency_key": key})
        return existing

    grant = ExecutionGrant(
        grant_id=_new_id("grnt_"),
        idempotency_key=key,
        decision_id=record.decision_id,
        decision_revision=record.decision_revision,
        decision_hash=record.decision_hash,
        decision_purpose=record.decision_purpose,
        correlation_id=record.correlation_id,
        task_id=task_id,
        task_revision=task_revision,
        subject_type=principal.principal_type,
        subject_id=principal.principal_id,
        subject_service_identity_id=principal.service_identity_id,
        delegation_ref=principal.delegation_ref,
        execution_mode=mode,
        capabilities=granted_capabilities,
        resource_scope=scope,
        policy_set_id=policy_row.policy_set_id,
        policy_version=policy_row.version,
        policy_content_hash=policy_row.content_hash,
        tool_contract_version=tool_contract_version,
        tool_contract_id=tool_contract_id,
        tool_contract_hash=tool_contract_hash,
        issued_at=issued_at,
        expires_at=expires_at,
        budget=normalized_budget,
        usage=_default_usage(),
        consumption_mode=consumption_mode,
        confirmation_ref=confirmation_ref,
        approval_ref=approval_ref,
        status="active",
        usable=True,
    )
    session.add(grant)
    session.flush()
    _audit_grant(
        session,
        actor_id=actor_id if actor_id is not None else principal.employee_id,
        action="governance.grant.issued",
        grant=grant,
    )
    return grant


def get_grant(
    session: Session, grant_id: str, *, for_update: bool = False
) -> ExecutionGrant | None:
    """Read a grant, optionally taking a row lock for atomic consumption."""
    stmt = select(ExecutionGrant).where(ExecutionGrant.grant_id == grant_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def grant_remaining_calls(grant: ExecutionGrant) -> int:
    """Return remaining calls computed from the persisted budget/usage."""
    max_calls = int(grant.budget.get("max_calls", 0))
    consumed = int(grant.usage.get("calls_consumed", 0))
    return max(0, max_calls - consumed)


def _deny_grant(
    session: Session,
    *,
    grant: ExecutionGrant | None,
    reason_code: str,
    actor_id: int | None,
    context: Mapping[str, Any] | None = None,
) -> None:
    details = dict(context or {})
    details["reason_code"] = reason_code
    if grant is not None:
        record_event(
            session,
            actor_id=actor_id,
            action="governance.grant.denied",
            resource_type="execution_grant",
            resource_id=grant.grant_id,
            request_id=grant.correlation_id,
            details={
                "grant_id": grant.grant_id,
                "decision_id": grant.decision_id,
                "task_id": grant.task_id,
                "task_revision": grant.task_revision,
                "status": grant.status,
                "policy_set_id": grant.policy_set_id,
                "policy_version": grant.policy_version,
                **details,
            },
        )
    raise GovernanceError(reason_code, context=context)


def _validate_subject_and_binding(
    grant: ExecutionGrant,
    *,
    principal: GovernancePrincipal,
    task_id: str,
    task_revision: int,
    capability: str,
    resource_scope: Mapping[str, Any] | None,
    tool_contract_version: str | None,
) -> dict[str, Any]:
    if grant.subject_id != principal.principal_id or grant.subject_type != principal.principal_type:
        raise GovernanceError("grant_scope_mismatch", context={"field": "subject"})
    if grant.subject_service_identity_id != principal.service_identity_id:
        raise GovernanceError("grant_scope_mismatch", context={"field": "service_identity"})
    if grant.task_id != task_id or grant.task_revision != task_revision:
        raise GovernanceError("grant_scope_mismatch", context={"field": "task"})
    if capability not in grant.capabilities:
        raise GovernanceError("capability_not_granted")
    if resource_scope is not None:
        try:
            requested = validate_resource_scope(resource_scope, require_concrete=True)
        except GovernanceError as exc:
            raise GovernanceError("grant_scope_mismatch", context={"field": "resource_scope"}) from exc
        if not scope_is_subset(requested, grant.resource_scope):
            raise GovernanceError("grant_scope_mismatch", context={"field": "resource_scope"})
        normalized = requested
    else:
        normalized = dict(grant.resource_scope)
    if tool_contract_version is not None and tool_contract_version != grant.tool_contract_version:
        raise GovernanceError("tool_contract_mismatch")
    return normalized


def consume_grant(
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
    now: datetime | None = None,
) -> ExecutionGrant:
    """Atomically consume one call from a grant, or fail closed.

    Single-use grants move to ``consumed`` before the caller performs a side
    effect; multi-use grants decrement the remaining budget.  The row lock makes
    concurrent consumption safe.
    """
    timestamp = now or _utcnow()
    actor_id = principal.employee_id
    grant = get_grant(session, grant_id, for_update=True)
    if grant is None:
        _deny_grant(
            session,
            grant=None,
            reason_code="grant_missing",
            actor_id=actor_id,
            context={"grant_id": grant_id},
        )

    if grant.expires_at <= timestamp or grant.status == "expired":
        grant.status = "expired"
        grant.usable = False
        session.flush()
        _deny_grant(
            session,
            grant=grant,
            reason_code="grant_expired",
            actor_id=actor_id,
            context={"call_id": call_id},
        )
    if grant.status == "revoked":
        _deny_grant(
            session,
            grant=grant,
            reason_code="grant_revoked",
            actor_id=actor_id,
            context={"call_id": call_id},
        )
    if grant.status == "consumed":
        reason = "replay_detected" if grant.consumption_mode == "single_use" else "grant_consumed"
        _deny_grant(
            session,
            grant=grant,
            reason_code=reason,
            actor_id=actor_id,
            context={"call_id": call_id},
        )
    if grant.status != "active" or not grant.usable:
        _deny_grant(
            session,
            grant=grant,
            reason_code="grant_revoked",
            actor_id=actor_id,
            context={"call_id": call_id, "status": grant.status},
        )
    try:
        _validate_subject_and_binding(
            grant,
            principal=principal,
            task_id=task_id,
            task_revision=task_revision,
            capability=capability,
            resource_scope=resource_scope,
            tool_contract_version=tool_contract_version,
        )
    except GovernanceError as exc:
        _deny_grant(
            session,
            grant=grant,
            reason_code=exc.reason_code,
            actor_id=actor_id,
            context=exc.context,
        )

    if grant_remaining_calls(grant) < 1:
        _deny_grant(
            session,
            grant=grant,
            reason_code="budget_exceeded",
            actor_id=actor_id,
            context={"call_id": call_id},
        )

    # Revalidate gates against the current state (they may have been consumed
    # elsewhere or revoked since issuance).
    if grant.confirmation_ref is not None:
        try:
            validate_confirmation_ref(grant.confirmation_ref, payload_hash=payload_hash, now=timestamp)
        except GovernanceError as exc:
            _deny_grant(
                session,
                grant=grant,
                reason_code=exc.reason_code,
                actor_id=actor_id,
                context={"call_id": call_id, "gate": "confirmation"},
            )
    if grant.approval_ref is not None:
        try:
            validate_approval_ref(
                grant.approval_ref,
                principal_id=grant.subject_id,
                payload_hash=payload_hash,
                now=timestamp,
            )
        except GovernanceError as exc:
            _deny_grant(
                session,
                grant=grant,
                reason_code=exc.reason_code,
                actor_id=actor_id,
                context={"call_id": call_id, "gate": "approval"},
            )

    usage = dict(grant.usage)
    usage["calls_consumed"] = int(usage.get("calls_consumed", 0)) + 1
    usage["last_call_at"] = timestamp.isoformat().replace("+00:00", "Z")
    usage["concurrent_calls"] = 0
    grant.usage = usage

    if grant.consumption_mode == "single_use":
        grant.status = "consumed"
        grant.usable = False
        grant.consumed_at = timestamp
        grant.consumed_by_call_id = call_id
    elif int(usage["calls_consumed"]) >= int(grant.budget.get("max_calls", 0)):
        grant.status = "consumed"
        grant.usable = False
        grant.consumed_at = timestamp
        grant.consumed_by_call_id = call_id

    if grant.confirmation_ref is not None:
        confirmation = dict(grant.confirmation_ref)
        confirmation["status"] = "consumed"
        confirmation["consumed_at"] = timestamp.isoformat().replace("+00:00", "Z")
        confirmation["consumed_by_call_id"] = call_id
        grant.confirmation_ref = confirmation

    session.flush()
    _audit_grant(
        session,
        actor_id=actor_id,
        action="governance.grant.consumed",
        grant=grant,
        extra={
            "call_id": call_id,
            "capability": capability,
            "remaining_calls": grant_remaining_calls(grant),
        },
    )
    return grant


def revoke_grant(
    session: Session,
    *,
    grant_id: str,
    reason_code: str,
    revoked_by_principal_id: str | None = None,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> ExecutionGrant:
    """Revoke a grant.  Revocation is terminal; re-issuance needs a new grant."""
    if reason_code not in GRANT_REVOCATION_REASONS:
        raise GovernanceError(
            "required_input_missing", context={"field": "revocation_reason"}
        )
    timestamp = now or _utcnow()
    grant = get_grant(session, grant_id, for_update=True)
    if grant is None:
        raise GovernanceError("grant_missing", context={"grant_id": grant_id})
    if grant.status == "revoked":
        return grant
    if grant.status == "consumed":
        raise GovernanceError("grant_consumed", context={"grant_id": grant_id})
    if grant.status == "expired":
        raise GovernanceError("grant_expired", context={"grant_id": grant_id})

    grant.status = "revoked"
    grant.usable = False
    grant.revoked_at = timestamp
    grant.revoked_by_principal_id = revoked_by_principal_id
    grant.revocation_reason = reason_code
    session.flush()
    _audit_grant(
        session,
        actor_id=actor_id,
        action="governance.grant.revoked",
        grant=grant,
        extra={"reason_code": reason_code},
    )
    return grant
