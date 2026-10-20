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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.governance.errors import (
    GRANT_REVOCATION_REASONS,
    GovernanceError,
)
from app.governance.models import ExecutionGrant, GovernanceDecisionRecord
from app.governance.pdp import (
    DENY_ALL_EVIDENCE,
    ApprovalEvidenceVerifier,
    GovernanceDecision,
    _verify_approval_evidence,
    validate_approval_ref,
    validate_confirmation_ref,
    verify_decision_document,
)
from app.governance.policies import (
    CAPABILITY_PATTERN,
    EXECUTION_MODES,
    get_active,
    scope_is_subset,
    validate_resource_scope,
)
from app.governance.principal import (
    GovernancePrincipal,
    principal_ref,
    reject_client_asserted_identity,
)

__all__ = [
    "get_grant",
    "grant_document",
    "grant_remaining_calls",
    "issue_grant",
    "revoke_grant",
]

_AUTHORIZING_DECISIONS = frozenset({"allow", "managed_execution_required"})
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_IDEM_KEY = re.compile(r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _normalize_idempotency_key(value: str | None) -> str:
    if value is None:
        return _new_id("idem_")
    if not isinstance(value, str) or not _IDEM_KEY.fullmatch(value):
        raise GovernanceError("required_input_missing", context={"field": "idempotency_key"})
    return value


_TASK_ID = re.compile(r"^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_DECISION_ID = re.compile(r"^gdec_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_CALL_ID = re.compile(r"^call_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_CATALOG_ID = re.compile(r"^cat_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_PRINCIPAL_ID = re.compile(r"^(prin|rt|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_DELEGATION_ID = re.compile(r"^dlg_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


def _require_task_id(value: Any, *, field: str = "task_id") -> str:
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise GovernanceError("required_input_missing", context={"field": field})
    return value


def _require_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise GovernanceError("required_input_missing", context={"field": field})
    return value


def _load_decision(session: Session, decision_id: str) -> GovernanceDecisionRecord:
    """Always re-read the persisted decision; never trust an in-memory copy.

    ``populate_existing`` is mandatory: a caller may have a stale decision
    object in the identity map, and the persisted row is the only authority.
    """
    if not isinstance(decision_id, str) or not _DECISION_ID.fullmatch(decision_id):
        raise GovernanceError("grant_missing", context={"decision_id": decision_id})
    # No autoflush: a caller may have mutated a persistent record in memory,
    # and flushing that mutation before the re-read would launder the forgery
    # into the database.
    with session.no_autoflush:
        row = session.scalar(
            select(GovernanceDecisionRecord)
            .where(GovernanceDecisionRecord.decision_id == decision_id)
            .execution_options(populate_existing=True)
        )
        if row is not None:
            # Decisions are immutable.  Always refresh so a caller-mutated
            # identity-map object cannot supply forged constraints/document,
            # even if SQLAlchemy considers the object clean.
            session.refresh(row)
    if row is None:
        raise GovernanceError("grant_missing", context={"decision_id": decision_id})
    verify_decision_document(row)
    return row


def _resolve_decision(
    session: Session,
    *,
    decision: GovernanceDecision | GovernanceDecisionRecord | None,
    decision_id: str | None,
) -> GovernanceDecisionRecord:
    """Resolve a decision id and return the **persisted** row.

    A ``GovernanceDecision`` contributes only its id.  A
    ``GovernanceDecisionRecord`` must already be persistent; its fields are
    never accepted as authority.  Either way the row is re-read from the
    database, so a forged/transient record cannot carry expanded constraints.
    """
    if isinstance(decision, GovernanceDecisionRecord):
        state = sa_inspect(decision)
        if not state.persistent:
            raise GovernanceError(
                "grant_missing", context={"reason": "transient_decision_record"}
            )
        if decision_id is None:
            decision_id = decision.decision_id
        # Drop any local mutations without flushing them.  Decisions are
        # immutable; the caller's in-memory copy is not authority and must not
        # be laundered into the database by a later autoflush.
        session.expire(decision)
    elif decision is not None:
        if decision_id is None:
            decision_id = decision.decision_id
    if not decision_id:
        raise GovernanceError("grant_missing", context={"field": "decision"})
    return _load_decision(session, decision_id)


def _current_revision(
    session: Session, *, task_id: str | None, principal_id: str
) -> int:
    if not task_id:
        return 0
    with session.no_autoflush:
        current = session.scalar(
            select(func.max(GovernanceDecisionRecord.decision_revision)).where(
                GovernanceDecisionRecord.task_id == task_id,
                GovernanceDecisionRecord.principal_id == principal_id,
            )
        )
    return int(current or 0)


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
    budget: Mapping[str, Any] | None,
    constraints: Mapping[str, Any],
    ttl_seconds: int,
    *,
    consumption_mode: str,
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

    decision_max_calls = constraints.get("max_calls", 0)
    if not isinstance(decision_max_calls, int) or decision_max_calls < 1:
        raise GovernanceError("budget_exceeded", context={"field": "max_calls"})

    # A single-use grant is exactly one call.  It never inherits the decision's
    # multi-call budget, and a caller cannot ask for more.
    if consumption_mode == "single_use":
        requested = raw.get("max_calls", 1)
        if (
            not isinstance(requested, int)
            or isinstance(requested, bool)
            or requested != 1
        ):
            raise GovernanceError("budget_exceeded", context={"field": "max_calls"})
        max_calls = 1
    else:
        max_calls = raw.get("max_calls", decision_max_calls)
        if (
            not isinstance(max_calls, int)
            or isinstance(max_calls, bool)
            or max_calls < 1
            or max_calls > decision_max_calls
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
    # ``cost_consumed`` is numeric even when no cost budget exists, so callers
    # never have to treat ``None`` as "skip the check".
    return {
        "calls_consumed": 0,
        "cost_consumed": 0.0,
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
    issued_by_service_identity_id: str | None = None,
    idempotency_key: str | None = None,
    client_payload: Mapping[str, Any] | None = None,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> ExecutionGrant:
    """Issue a grant from an authorizing decision, refusing any expansion.

    All authority fields are derived from the **persisted** decision document;
    the caller may narrow capabilities/scope/TTL/budget but can never widen
    them.  Gate references are copied from the decision, so a grant cannot
    smuggle a different confirmation/approval.
    """
    issued_at = now or _utcnow()
    principal.require_active()
    reject_client_asserted_identity(client_payload)
    record = _resolve_decision(session, decision=decision, decision_id=decision_id)
    document = verify_decision_document(record)

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

    # A newer decision for the same task/principal supersedes this one.
    if record.decision_revision < _current_revision(
        session, task_id=record.task_id, principal_id=record.principal_id
    ):
        raise GovernanceError(
            "decision_revision_stale",
            context={
                "decision_id": record.decision_id,
                "decision_revision": record.decision_revision,
            },
        )

    if task_id is None:
        task_id = record.task_id
    if task_revision is None:
        task_revision = record.task_revision
    _require_task_id(task_id)
    if task_id != record.task_id or task_revision != record.task_revision:
        raise GovernanceError("grant_scope_mismatch", context={"field": "task"})
    if not isinstance(task_revision, int) or isinstance(task_revision, bool) or task_revision < 1:
        raise GovernanceError("task_not_created")

    task_ref = document.get("task_ref") or {}
    task_idempotency_key = task_ref.get("idempotency_key")
    if (
        not isinstance(task_idempotency_key, str)
        or not _IDEM_KEY.fullmatch(task_idempotency_key)
    ):
        raise GovernanceError(
            "required_input_missing", context={"field": "task_idempotency_key"}
        )

    constraints = document.get("constraints") or {}
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

    if consumption_mode not in {"single_use", "multi_use"}:
        raise GovernanceError("unsupported_execution_mode", context={"field": "consumption_mode"})
    required_confirmation = bool(constraints.get("required_confirmation"))
    required_approval = bool(constraints.get("required_approval"))

    decision_confirmation_ref = constraints.get("confirmation_ref")
    decision_approval_ref = constraints.get("approval_ref")
    if required_confirmation and decision_confirmation_ref is None:
        raise GovernanceError("confirmation_missing")
    if required_approval and decision_approval_ref is None:
        raise GovernanceError("approval_missing")
    if confirmation_ref is not None:
        if decision_confirmation_ref is None or dict(confirmation_ref) != dict(
            decision_confirmation_ref
        ):
            raise GovernanceError(
                "confirmation_missing", context={"reason": "unsolicited_confirmation"}
            )
    if approval_ref is not None:
        if decision_approval_ref is None or dict(approval_ref) != dict(
            decision_approval_ref
        ):
            raise GovernanceError(
                "approval_missing", context={"reason": "unsolicited_approval"}
            )

    confirmation_ref = (
        dict(decision_confirmation_ref)
        if decision_confirmation_ref is not None
        else None
    )
    approval_ref = (
        dict(decision_approval_ref) if decision_approval_ref is not None else None
    )
    if confirmation_ref is not None:
        # A confirmation is single-use by contract, so the grant must be too.
        if consumption_mode != "single_use":
            raise GovernanceError(
                "grant_scope_mismatch", context={"field": "consumption_mode"}
            )
        confirmation_ref = validate_confirmation_ref(
            confirmation_ref,
            payload_hash=confirmation_ref.get("payload_hash"),
            now=issued_at,
        )
    if approval_ref is not None:
        approval_ref = validate_approval_ref(
            approval_ref,
            principal_id=principal.principal_id,
            owner_principal_ids=frozenset(
                {
                    principal.principal_id,
                    *(
                        {principal_ref("prin", principal.employee_id)}
                        if principal.employee_id is not None
                        else set()
                    ),
                    *(
                        {principal_ref("prin", principal.owner_employee_id)}
                        if principal.owner_employee_id is not None
                        else set()
                    ),
                }
            ),
            payload_hash=approval_ref.get("payload_hash"),
            now=issued_at,
        )

    normalized_budget = _normalize_budget(
        budget, constraints, ttl, consumption_mode=consumption_mode
    )
    if not isinstance(tool_contract_version, str) or not _SEMVER.fullmatch(
        tool_contract_version
    ):
        raise GovernanceError("tool_contract_mismatch")
    if tool_contract_id is not None and (
        not isinstance(tool_contract_id, str)
        or not _CATALOG_ID.fullmatch(tool_contract_id)
    ):
        raise GovernanceError("tool_contract_mismatch", context={"field": "tool_contract_id"})
    if tool_contract_hash is not None:
        _require_sha256(tool_contract_hash, field="tool_contract_hash")
    if (tool_contract_id is None) != (tool_contract_hash is None):
        raise GovernanceError(
            "tool_contract_mismatch",
            context={"reason": "catalog_id_and_hash_must_pair"},
        )
    if issued_by_service_identity_id is not None and (
        not isinstance(issued_by_service_identity_id, str)
        or not _PRINCIPAL_ID.fullmatch(issued_by_service_identity_id)
        or not issued_by_service_identity_id.startswith("svc_")
    ):
        raise GovernanceError(
            "required_input_missing", context={"field": "issued_by_service_identity_id"}
        )

    # Delegation provenance: the decision subject is authoritative.
    decision_delegation = (document.get("subject") or {}).get("delegation_ref")
    principal_delegation = principal.delegation_ref
    if decision_delegation is not None and not isinstance(
        decision_delegation, str
    ):
        raise GovernanceError("grant_scope_mismatch", context={"field": "delegation_ref"})
    if decision_delegation is not None and not _DELEGATION_ID.fullmatch(
        decision_delegation
    ):
        raise GovernanceError("grant_scope_mismatch", context={"field": "delegation_ref"})
    if (decision_delegation or None) != (principal_delegation or None):
        raise GovernanceError("grant_scope_mismatch", context={"field": "delegation_ref"})

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

    key = _normalize_idempotency_key(idempotency_key)
    existing = session.scalar(
        select(ExecutionGrant).where(ExecutionGrant.idempotency_key == key)
    )
    if existing is not None:
        same = (
            existing.decision_id == record.decision_id
            and existing.decision_type == record.decision_type
            and existing.task_id == task_id
            and existing.task_revision == task_revision
            and existing.task_idempotency_key == task_idempotency_key
            and existing.subject_id == principal.principal_id
            and existing.subject_type == principal.principal_type
            and existing.subject_employee_id == principal.employee_id
            and existing.subject_service_identity_id == principal.service_identity_id
            and existing.delegation_ref == (decision_delegation or None)
            and existing.capabilities == granted_capabilities
            and existing.resource_scope == scope
            and existing.execution_mode == mode
            and existing.consumption_mode == consumption_mode
            and existing.tool_contract_version == tool_contract_version
            and existing.tool_contract_id == tool_contract_id
            and existing.tool_contract_hash == tool_contract_hash
            and existing.confirmation_ref == confirmation_ref
            and existing.approval_ref == approval_ref
            and existing.issued_by_service_identity_id == issued_by_service_identity_id
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
        decision_type=record.decision_type,
        decision_evaluated_at=record.evaluated_at,
        decision_expires_at=record.expires_at,
        correlation_id=record.correlation_id,
        task_id=task_id,
        task_revision=task_revision,
        task_idempotency_key=task_idempotency_key,
        subject_type=principal.principal_type,
        subject_id=principal.principal_id,
        subject_employee_id=principal.employee_id,
        subject_service_identity_id=principal.service_identity_id,
        issued_by_service_identity_id=issued_by_service_identity_id,
        delegation_ref=decision_delegation or None,
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
    stmt = (
        select(ExecutionGrant)
        .where(ExecutionGrant.grant_id == grant_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    with session.no_autoflush:
        row = session.scalar(stmt)
        if row is not None:
            # Re-read even when the identity map already holds the object; a
            # stale/locally-mutated grant must never hide a revocation or a
            # consumption committed by another session.
            session.refresh(row, with_for_update=for_update)
        return row


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
    task_idempotency_key: str | None,
    capability: str,
    resource_scope: Mapping[str, Any],
    tool_contract_version: str,
) -> dict[str, Any]:
    if grant.subject_id != principal.principal_id or grant.subject_type != principal.principal_type:
        raise GovernanceError("grant_scope_mismatch", context={"field": "subject"})
    if grant.subject_employee_id != principal.employee_id:
        raise GovernanceError(
            "grant_scope_mismatch", context={"field": "requester_identity"}
        )
    if grant.subject_service_identity_id != principal.service_identity_id:
        raise GovernanceError("grant_scope_mismatch", context={"field": "service_identity"})
    if grant.task_id != task_id or grant.task_revision != task_revision:
        raise GovernanceError("grant_scope_mismatch", context={"field": "task"})
    if (
        task_idempotency_key is not None
        and grant.task_idempotency_key != task_idempotency_key
    ):
        raise GovernanceError(
            "grant_scope_mismatch", context={"field": "task_idempotency_key"}
        )
    if capability not in grant.capabilities:
        raise GovernanceError("capability_not_granted")
    try:
        requested = validate_resource_scope(resource_scope, require_concrete=True)
    except GovernanceError as exc:
        raise GovernanceError(
            "grant_scope_mismatch", context={"field": "resource_scope"}
        ) from exc
    if not scope_is_subset(requested, grant.resource_scope):
        raise GovernanceError("grant_scope_mismatch", context={"field": "resource_scope"})
    if tool_contract_version != grant.tool_contract_version:
        raise GovernanceError("tool_contract_mismatch")
    return requested


def _consume_grant(
    session: Session,
    *,
    grant_id: str,
    principal: GovernancePrincipal,
    task_id: str,
    task_revision: int,
    task_idempotency_key: str | None,
    capability: str,
    call_id: str,
    resource_scope: Mapping[str, Any],
    tool_contract_version: str,
    payload_hash: str | None = None,
    cost_units: float = 0.0,
    evidence_verifier: ApprovalEvidenceVerifier | None = None,
    now: datetime | None = None,
) -> ExecutionGrant:
    """Atomically consume one call from a grant, or fail closed.

    Private on purpose: this is the PEP's atomic primitive.  It still enforces
    every binding itself, so even a future internal caller cannot bypass the
    resource-scope, payload-hash, tool-version or gate checks by omitting a
    keyword argument.
    """
    timestamp = now or _utcnow()
    actor_id = principal.employee_id
    if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
        raise GovernanceError("required_input_missing", context={"field": "call_id"})
    if not isinstance(capability, str) or not CAPABILITY_PATTERN.fullmatch(capability):
        raise GovernanceError("capability_not_granted", context={"field": "capability"})
    if not isinstance(tool_contract_version, str) or not _SEMVER.fullmatch(
        tool_contract_version
    ):
        raise GovernanceError(
            "tool_contract_mismatch", context={"field": "tool_contract_version"}
        )
    if resource_scope is None:
        # Missing context must never mean "skip the scope check".
        raise GovernanceError(
            "grant_scope_mismatch",
            context={"field": "resource_scope", "reason": "missing_context"},
        )
    if (
        isinstance(cost_units, bool)
        or not isinstance(cost_units, (int, float))
        or cost_units < 0
    ):
        raise GovernanceError("budget_exceeded", context={"field": "cost_units"})

    grant = get_grant(session, grant_id, for_update=True)
    if grant is None:
        _deny_grant(
            session,
            grant=None,
            reason_code="grant_missing",
            actor_id=actor_id,
            context={"grant_id": grant_id},
        )

    # Terminal states are immutable.  Revoked/consumed must keep their reason
    # and never be relabelled "expired" because time has passed.
    if grant.status == "revoked":
        _deny_grant(
            session,
            grant=grant,
            reason_code="grant_revoked",
            actor_id=actor_id,
            context={"call_id": call_id, "revocation_reason": grant.revocation_reason},
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
    if grant.status == "expired":
        _deny_grant(
            session,
            grant=grant,
            reason_code="grant_expired",
            actor_id=actor_id,
            context={"call_id": call_id},
        )
    if grant.expires_at <= timestamp:
        # Only an active grant may transition to expired.
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
            task_idempotency_key=task_idempotency_key,
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

    # Cost budget: a grant with no numeric budget allows exactly zero cost.
    max_cost = grant.budget.get("max_cost_units")
    consumed_cost = float(grant.usage.get("cost_consumed", 0.0) or 0.0)
    projected_cost = consumed_cost + float(cost_units)
    if max_cost is None:
        if projected_cost > 0:
            _deny_grant(
                session,
                grant=grant,
                reason_code="budget_exceeded",
                actor_id=actor_id,
                context={"call_id": call_id, "field": "max_cost_units"},
            )
    elif projected_cost > float(max_cost):
        _deny_grant(
            session,
            grant=grant,
            reason_code="budget_exceeded",
            actor_id=actor_id,
            context={"call_id": call_id, "field": "max_cost_units"},
        )

    # Revalidate gates against current evidence.  Without a trusted verifier
    # the default denies, so a caller-asserted reference is never sufficient.
    verifier = evidence_verifier or DENY_ALL_EVIDENCE
    if grant.confirmation_ref is not None or grant.approval_ref is not None:
        if not isinstance(payload_hash, str) or not _SHA256.fullmatch(payload_hash):
            _deny_grant(
                session,
                grant=grant,
                reason_code="payload_hash_mismatch",
                actor_id=actor_id,
                context={"call_id": call_id, "field": "payload_hash", "missing": True},
            )
    if grant.confirmation_ref is not None:
        try:
            confirmation = validate_confirmation_ref(
                grant.confirmation_ref, payload_hash=payload_hash, now=timestamp
            )
        except GovernanceError as exc:
            _deny_grant(
                session,
                grant=grant,
                reason_code=exc.reason_code,
                actor_id=actor_id,
                context={"call_id": call_id, "gate": "confirmation", **exc.context},
            )
        try:
            confirmed = verifier.verify_confirmation(
                confirmation,
                principal_id=principal.principal_id,
                payload_hash=payload_hash,
                now=timestamp,
            )
        except Exception:  # noqa: BLE001 - untrusted verifier: fail closed
            confirmed = False
        if not confirmed:
            _deny_grant(
                session,
                grant=grant,
                reason_code="confirmation_missing",
                actor_id=actor_id,
                context={"call_id": call_id, "gate": "confirmation"},
            )
    if grant.approval_ref is not None:
        try:
            approval = validate_approval_ref(
                grant.approval_ref,
                principal_id=grant.subject_id,
                owner_principal_ids=frozenset(
                    {
                        grant.subject_id,
                        *(
                            {principal_ref("prin", grant.subject_employee_id)}
                            if grant.subject_employee_id is not None
                            else set()
                        ),
                    }
                ),
                payload_hash=payload_hash,
                now=timestamp,
            )
            resolved = _verify_approval_evidence(
                verifier,
                approval,
                principal=principal,
                payload_hash=payload_hash,
                now=timestamp,
            )
        except GovernanceError as exc:
            _deny_grant(
                session,
                grant=grant,
                reason_code=exc.reason_code,
                actor_id=actor_id,
                context={"call_id": call_id, "gate": "approval", **exc.context},
            )
        except Exception:  # noqa: BLE001 - untrusted verifier: fail closed
            resolved = None
        if resolved is None:
            _deny_grant(
                session,
                grant=grant,
                reason_code="approval_missing",
                actor_id=actor_id,
                context={"call_id": call_id, "gate": "approval"},
            )

    usage = dict(grant.usage)
    usage["calls_consumed"] = int(usage.get("calls_consumed", 0)) + 1
    usage["cost_consumed"] = projected_cost
    usage["last_call_at"] = timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")
    usage["concurrent_calls"] = 0
    grant.usage = usage

    if grant.consumption_mode == "single_use" or int(usage["calls_consumed"]) >= int(grant.budget.get("max_calls", 0)):
        grant.status = "consumed"
        grant.usable = False
        grant.consumed_at = timestamp
        grant.consumed_by_call_id = call_id

    if grant.confirmation_ref is not None:
        confirmation = dict(grant.confirmation_ref)
        confirmation["status"] = "consumed"
        confirmation["consumed_at"] = timestamp.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
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
            "cost_units": float(cost_units),
        },
    )
    return grant


def revoke_grant(
    session: Session,
    *,
    grant_id: str,
    reason_code: str,
    principal: GovernancePrincipal,
    now: datetime | None = None,
) -> ExecutionGrant:
    """Revoke a grant.  Revocation is terminal; re-issuance needs a new grant.

    The caller must supply a *resolved* principal.  The grant subject (or the
    service identity's owner) may revoke its own grant; any other principal
    needs the existing ``identity/manage/company`` RBAC permission.
    """
    if reason_code not in GRANT_REVOCATION_REASONS:
        raise GovernanceError(
            "required_input_missing", context={"field": "revocation_reason"}
        )
    if principal is None:
        raise GovernanceError("identity_unverified", context={"field": "principal"})
    principal.require_active()
    timestamp = now or _utcnow()
    grant = get_grant(session, grant_id, for_update=True)
    if grant is None:
        raise GovernanceError("grant_missing", context={"grant_id": grant_id})

    is_subject = principal.principal_id == grant.subject_id
    is_owner = (
        principal.employee_id is not None
        and grant.subject_employee_id is not None
        and principal.employee_id == grant.subject_employee_id
    )
    if not (is_subject or is_owner):
        if ("identity", "manage", "company") not in principal.effective_permissions:
            raise GovernanceError(
                "capability_not_granted",
                context={
                    "field": "revocation_authority",
                    "principal_id": principal.principal_id,
                },
            )

    if grant.status == "revoked":
        return grant
    if grant.status == "consumed":
        raise GovernanceError("grant_consumed", context={"grant_id": grant_id})
    if grant.status == "expired":
        raise GovernanceError("grant_expired", context={"grant_id": grant_id})

    grant.status = "revoked"
    grant.usable = False
    grant.revoked_at = timestamp
    grant.revoked_by_principal_id = principal.principal_id
    grant.revocation_reason = reason_code
    session.flush()
    _audit_grant(
        session,
        actor_id=principal.employee_id,
        action="governance.grant.revoked",
        grant=grant,
        extra={"reason_code": reason_code, "revoked_by": principal.principal_id},
    )
    return grant


def grant_document(grant: ExecutionGrant) -> dict[str, Any]:
    """Serialize the Grant fields that are derivable from Issue #78 state.

    This is **not** a claim of full ``execution-grant.schema.json`` conformance:
    ``attempt`` (retry/task aggregate, Issue #79) and a real ``issued_by``
    service identity (service-identity registry) are deliberately omitted when
    unavailable.  All other blocks are deterministic and are the values the PEP
    actually revalidates.
    """
    document: dict[str, Any] = {
        "schema_version": "1.0.0",
        "grant_id": grant.grant_id,
        "correlation_id": grant.correlation_id,
        "task_ref": {
            "task_id": grant.task_id,
            "idempotency_key": grant.task_idempotency_key,
            "task_revision": grant.task_revision,
        },
        "subject": {
            "principal_id": grant.subject_id,
            "principal_type": grant.subject_type,
            "service_identity_id": grant.subject_service_identity_id,
            "delegation_ref": grant.delegation_ref,
            "account_status": "active" if grant.status != "revoked" else "disabled",
        },
        "execution_mode": grant.execution_mode,
        "capabilities": {
            "allowed": list(grant.capabilities),
            "denied": [],
        },
        "resource_scope": dict(grant.resource_scope),
        "policy_version": {
            "policy_set_id": grant.policy_set_id,
            "version": grant.policy_version,
            "content_hash": grant.policy_content_hash,
        },
        "gates": {
            "confirmation_required": grant.confirmation_ref is not None,
            "approval_required": grant.approval_ref is not None,
        },
        "issued_at": grant.issued_at.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "expires_at": grant.expires_at.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "ttl_seconds": max(
            1, int((grant.expires_at - grant.issued_at).total_seconds())
        ),
        "budget": dict(grant.budget),
        "usage": dict(grant.usage),
        "confirmation_ref": dict(grant.confirmation_ref)
        if grant.confirmation_ref is not None
        else None,
        "approval_ref": dict(grant.approval_ref)
        if grant.approval_ref is not None
        else None,
        "status": grant.status,
        "usable": bool(grant.usable),
        "revocation": {
            "revoked": grant.status == "revoked",
            "revoked_at": (
                grant.revoked_at.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                if grant.revoked_at is not None
                else None
            ),
            "reason_code": grant.revocation_reason,
            "revoked_by_principal_id": grant.revoked_by_principal_id,
        },
        "consumption": {
            "mode": grant.consumption_mode,
            "consumed_at": (
                grant.consumed_at.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                if grant.consumed_at is not None
                else None
            ),
            "consumed_by_call_id": grant.consumed_by_call_id,
            "remaining_calls": grant_remaining_calls(grant),
        },
        "revalidation": {
            "required": True,
            "at": "each_call",
            "policy_version_bound": True,
            "tool_version_bound": True,
            "subject_bound": True,
            "resource_scope_bound": True,
            "decision_bound": True,
            "task_revision_bound": True,
            "confirmation_bound": True,
            "approval_bound": True,
            "budget_bound": True,
            "execution_mode_bound": True,
        },
        "binding": {
            "task_revision_bound": True,
            "capabilities_subset_of_decision": True,
            "scope_subset_of_decision": True,
            "ttl_within_decision": True,
            "budget_within_decision": True,
            "expires_within_decision": True,
            "execution_mode_matches_decision": True,
            "decision_hash_bound": True,
            "gates_match_decision": True,
            "confirmation_payload_hash_bound": True,
            "runtime_within_grant_ttl": True,
        },
        "decision": {
            "decision_id": grant.decision_id,
            "decision_revision": grant.decision_revision,
            "decision_hash": grant.decision_hash,
            "decision_type": grant.decision_type,
            "decision_purpose": grant.decision_purpose,
            "freshness": "fresh_evaluation",
            "canonicalization": "rfc8785_jcs",
            "evaluated_at": (
                grant.decision_evaluated_at.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                if grant.decision_evaluated_at is not None
                else grant.issued_at.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            ),
            "expires_at": (
                grant.decision_expires_at.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                if grant.decision_expires_at is not None
                else grant.expires_at.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            ),
            "execution_mode": grant.execution_mode,
            "policy_version": {
                "policy_set_id": grant.policy_set_id,
                "version": grant.policy_version,
                "content_hash": grant.policy_content_hash,
            },
            "correlation_id": grant.correlation_id,
        },
    }
    if grant.tool_contract_id is not None and grant.tool_contract_hash is not None:
        document["tool_contract"] = {
            "catalog_id": grant.tool_contract_id,
            "version": grant.tool_contract_version,
            "content_hash": grant.tool_contract_hash,
        }
    if grant.issued_by_service_identity_id is not None:
        document["issued_by"] = {
            "service_identity_id": grant.issued_by_service_identity_id,
            "authentication_method": "internal_workload_identity",
        }
    return document
