"""Deterministic Policy Decision Point (PDP).

Evaluation order is fixed and fail-closed:

1. trusted principal status/assurance;
2. active policy version (missing policy -> deny);
3. hard deny (never overridden by priority, scope or gates);
4. policy rule match and its declared effect;
5. existing RBAC required-permission triples;
6. resource-scope validation and containment;
7. delegation validity/containment;
8. confirmation and approval gates;
9. task binding for authorizing decisions.

Priority is scheduling-only.  It is recorded for audit and can add the stable
``priority_cannot_override_deny`` reason code when a hard denial is attempted,
but it never changes the outcome.

Every decision is persisted to ``governance_decisions`` and emits an audit event
with the actor, policy version and reason codes.  Callers own the transaction
boundary; this module only flushes.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.db.models.organization import Employee
from app.governance.errors import (
    ALLOW_REASON_CODES,
    DENY_REASON_CODES,
    NEEDS_REASON_CODES,
    REASON_CODES,
    GovernanceError,
    PolicyError,
)
from app.governance.models import (
    GovernanceDecisionRecord,
    GovernanceDelegation,
    GovernancePolicyVersion,
)
from app.governance.policies import (
    CAPABILITY_PATTERN,
    DEFAULT_POLICY_SET_ID,
    EXECUTION_MODES,
    RISK_LEVELS,
    PolicyRule,
    canonical_json,
    capability_matches,
    content_hash,
    get_active,
    parse_policy_document,
    scope_is_subset,
    sha256_digest,
    validate_resource_scope,
)
from app.governance.principal import (
    VERIFIED_ASSURANCES,
    GovernancePrincipal,
    principal_ref,
)
from app.identity.authorization import effective_permissions

__all__ = [
    "PRIORITY_LEVELS",
    "ApprovalEvidenceVerifier",
    "DenyAllApprovalEvidenceVerifier",
    "EvaluationRequest",
    "GovernanceDecision",
    "decision_document_hash",
    "evaluate",
    "validate_approval_ref",
    "validate_confirmation_ref",
    "verify_decision_document",
]

PRIORITY_LEVELS = frozenset({"low", "normal", "high", "urgent"})
_ESCALATING_PRIORITIES = frozenset({"high", "urgent"})
_ZERO_HASH = "sha256:" + "0" * 64
_POLICY_SET_ID = re.compile(r"^polset_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_NON_AUTHORIZING_TTL_SECONDS = 300
_TASK_ID = re.compile(r"^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_IDEM_KEY = re.compile(r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_CORRELATION_ID = re.compile(r"^corr_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_APPROVER_ID = re.compile(r"^(prin|role)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_RESOLVED_PERSON = re.compile(r"^prin_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


class ApprovalEvidenceVerifier(Protocol):
    """Trusted resolver for confirmation/approval evidence.

    Issue #78 has no approval store, so the only safe default is
    :class:`DenyAllApprovalEvidenceVerifier`.  A deployment that owns a
    trustworthy store injects a verifier that re-reads the evidence and, for a
    ``role_`` approver, returns the actual person.  A caller-asserted reference
    is never sufficient by itself.
    """

    def verify_confirmation(
        self,
        ref: Mapping[str, Any],
        *,
        principal_id: str,
        payload_hash: str | None,
        now: datetime,
    ) -> bool:
        """Return True only if the confirmation is authentic and unconsumed."""

    def verify_approval(
        self,
        ref: Mapping[str, Any],
        *,
        requester_principal_id: str,
        owner_principal_ids: frozenset[str],
        payload_hash: str | None,
        now: datetime,
    ) -> str | None:
        """Return the resolved person (``prin_*``) or ``None`` if untrusted."""


class DenyAllApprovalEvidenceVerifier:
    """Default verifier: no evidence store exists, so nothing is satisfied."""

    def verify_confirmation(
        self,
        ref: Mapping[str, Any],
        *,
        principal_id: str,
        payload_hash: str | None,
        now: datetime,
    ) -> bool:
        return False

    def verify_approval(
        self,
        ref: Mapping[str, Any],
        *,
        requester_principal_id: str,
        owner_principal_ids: frozenset[str],
        payload_hash: str | None,
        now: datetime,
    ) -> str | None:
        return None


DENY_ALL_EVIDENCE = DenyAllApprovalEvidenceVerifier()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _rfc3339(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


@dataclass(frozen=True)
class EvaluationRequest:
    """Input to :func:`evaluate`.

    ``principal`` is always the result of trusted resolution.  The remaining
    fields describe the requested action; none of them is an authority claim.
    """

    principal: GovernancePrincipal
    capability: str
    task_id: str | None = None
    task_revision: int | None = None
    task_idempotency_key: str | None = None
    resource_scope: Mapping[str, Any] | None = None
    priority: str = "normal"
    execution_mode: str = "advisory"
    risk: str = "low"
    delegation_ref: str | None = None
    confirmation_ref: Mapping[str, Any] | None = None
    approval_ref: Mapping[str, Any] | None = None
    payload_hash: str | None = None
    policy_set_id: str = DEFAULT_POLICY_SET_ID
    policy_version: str | None = None
    correlation_id: str | None = None
    decision_purpose: str = "initial_execution"
    supersedes_decision_id: str | None = None
    pre_evaluation: bool = False


@dataclass(frozen=True)
class GovernanceDecision:
    """A contract-shaped governance decision."""

    schema_version: str
    decision_id: str
    decision_revision: int
    decision_hash: str
    canonicalization: str
    decision_type: str
    decision_purpose: str
    reason_codes: tuple[str, ...]
    policy_version: dict[str, Any]
    subject: dict[str, Any]
    task_ref: dict[str, Any]
    evaluated_at: str
    expires_at: str
    fail_closed: bool
    constraints: dict[str, Any]
    obligations: tuple[str, ...]
    supersedes_decision_id: str | None
    correlation_id: str
    explanation: str | None = None

    @property
    def is_authorizing(self) -> bool:
        return self.decision_type in {"allow", "managed_execution_required"}

    def to_document(self) -> dict[str, Any]:
        """Return the full decision document (including ``decision_hash``)."""
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "decision_revision": self.decision_revision,
            "decision_hash": self.decision_hash,
            "canonicalization": self.canonicalization,
            "decision_type": self.decision_type,
            "decision_purpose": self.decision_purpose,
            "reason_codes": list(self.reason_codes),
            "policy_version": self.policy_version,
            "subject": self.subject,
            "task_ref": self.task_ref,
            "evaluated_at": self.evaluated_at,
            "expires_at": self.expires_at,
            "fail_closed": self.fail_closed,
            "constraints": self.constraints,
            "obligations": list(self.obligations),
            "supersedes_decision_id": self.supersedes_decision_id,
            "correlation_id": self.correlation_id,
            "explanation": self.explanation,
        }


# ---------------------------------------------------------------------------
# Gate validators (also used by grants/PEP)
# ---------------------------------------------------------------------------


def validate_confirmation_ref(
    ref: Mapping[str, Any],
    *,
    payload_hash: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Structurally validate a satisfied, unconsumed, single-use confirmation.

    This only proves the reference is well formed.  Whether it is *authentic*
    is decided by :class:`ApprovalEvidenceVerifier`; shape validation alone
    never satisfies a gate.
    """
    timestamp = now or _utcnow()
    if not isinstance(ref, Mapping):
        raise GovernanceError("confirmation_missing")
    required = (
        "confirmation_id",
        "payload_hash",
        "status",
        "single_use",
        "satisfied_at",
        "consumed_at",
        "expires_at",
        "confirmed_by_principal_id",
    )
    missing = [key for key in required if key not in ref]
    if missing:
        raise GovernanceError("confirmation_missing", context={"missing": missing})

    if not isinstance(ref.get("confirmation_id"), str) or not re.fullmatch(
        r"conf_[A-Za-z0-9][A-Za-z0-9_-]{3,63}", ref["confirmation_id"]
    ):
        raise GovernanceError("confirmation_missing", context={"field": "confirmation_id"})
    if ref.get("canonicalization") not in {None, "rfc8785_jcs"}:
        raise GovernanceError("confirmation_missing", context={"field": "canonicalization"})
    if ref.get("status") != "satisfied":
        if ref.get("status") == "consumed":
            raise GovernanceError("replay_detected")
        raise GovernanceError(
            "confirmation_missing", context={"status": ref.get("status")}
        )
    if ref.get("consumed_at") is not None:
        raise GovernanceError("replay_detected")
    if ref.get("single_use") is not True:
        raise GovernanceError("confirmation_missing", context={"field": "single_use"})
    confirmer = ref.get("confirmed_by_principal_id")
    if not isinstance(confirmer, str) or not re.fullmatch(
        r"^(prin|rt|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$", confirmer
    ):
        raise GovernanceError(
            "confirmation_missing", context={"field": "confirmed_by_principal_id"}
        )
    expires_at = _parse_rfc3339(ref.get("expires_at"))
    if expires_at is None or expires_at <= timestamp:
        raise GovernanceError("confirmation_missing", context={"field": "expires_at"})
    ref_hash = ref.get("payload_hash")
    if not isinstance(ref_hash, str) or not _SHA256.fullmatch(ref_hash):
        raise GovernanceError("payload_hash_mismatch")
    # The payload hash is mandatory: omitting it must not skip binding.
    if not isinstance(payload_hash, str) or not _SHA256.fullmatch(payload_hash):
        raise GovernanceError(
            "payload_hash_mismatch", context={"field": "payload_hash", "missing": True}
        )
    if ref_hash != payload_hash:
        raise GovernanceError("payload_hash_mismatch")
    return dict(ref)


def validate_approval_ref(
    ref: Mapping[str, Any],
    *,
    principal_id: str,
    owner_principal_ids: frozenset[str] | set[str] | None = None,
    payload_hash: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Structurally validate an approved, separation-checked, unexpired approval.

    ``role_`` approvers pass shape validation but must still be resolved to a
    person by the trusted verifier.  Self-approval is rejected here when the
    approver is already a concrete ``prin_`` id; the verified person is checked
    again after resolution.
    """
    timestamp = now or _utcnow()
    if not isinstance(ref, Mapping):
        raise GovernanceError("approval_missing")
    required = (
        "approval_id",
        "approver_principal_id",
        "payload_hash",
        "decision",
        "separation_check",
        "decided_at",
        "expires_at",
    )
    missing = [key for key in required if key not in ref]
    if missing:
        raise GovernanceError("approval_missing", context={"missing": missing})

    if not isinstance(ref.get("approval_id"), str) or not re.fullmatch(
        r"appr_[A-Za-z0-9][A-Za-z0-9_-]{3,63}", ref["approval_id"]
    ):
        raise GovernanceError("approval_missing", context={"field": "approval_id"})
    if ref.get("canonicalization") not in {None, "rfc8785_jcs"}:
        raise GovernanceError("approval_missing", context={"field": "canonicalization"})
    if ref.get("decision") != "approved":
        raise GovernanceError(
            "approval_missing", context={"decision": ref.get("decision")}
        )
    separation = ref.get("separation_check")
    if separation == "failed":
        raise GovernanceError("separation_of_duties_violation")
    if separation != "passed":
        raise GovernanceError("approval_missing", context={"field": "separation_check"})

    approver = ref.get("approver_principal_id")
    if not isinstance(approver, str) or not _APPROVER_ID.fullmatch(approver):
        raise GovernanceError("approval_missing", context={"field": "approver_principal_id"})
    owners = set(owner_principal_ids or ())
    owners.add(principal_id)
    if approver.startswith("prin_") and approver in owners:
        raise GovernanceError("self_approval_forbidden")

    decided_at = _parse_rfc3339(ref.get("decided_at"))
    expires_at = _parse_rfc3339(ref.get("expires_at"))
    if decided_at is None or expires_at is None or expires_at <= timestamp:
        raise GovernanceError("approval_missing", context={"field": "expires_at"})

    ref_hash = ref.get("payload_hash")
    if not isinstance(ref_hash, str) or not _SHA256.fullmatch(ref_hash):
        raise GovernanceError("payload_hash_mismatch")
    if not isinstance(payload_hash, str) or not _SHA256.fullmatch(payload_hash):
        raise GovernanceError(
            "payload_hash_mismatch", context={"field": "payload_hash", "missing": True}
        )
    if ref_hash != payload_hash:
        raise GovernanceError("payload_hash_mismatch")
    return dict(ref)


def _owner_principal_ids(principal: GovernancePrincipal) -> frozenset[str]:
    owners = {principal.principal_id}
    if principal.employee_id is not None:
        owners.add(principal_ref("prin", principal.employee_id))
    if principal.owner_employee_id is not None:
        owners.add(principal_ref("prin", principal.owner_employee_id))
    return frozenset(owners)


def _verify_approval_evidence(
    verifier: ApprovalEvidenceVerifier,
    ref: Mapping[str, Any],
    *,
    principal: GovernancePrincipal,
    payload_hash: str | None,
    now: datetime,
) -> str | None:
    """Return the resolved approver, or ``None`` when evidence is untrusted.

    A verifier may not resolve a ``role_`` to another role: only an actual
    person (``prin_*``) satisfies an approval.
    """
    resolved = verifier.verify_approval(
        ref,
        requester_principal_id=principal.principal_id,
        owner_principal_ids=_owner_principal_ids(principal),
        payload_hash=payload_hash,
        now=now,
    )
    if resolved is None:
        return None
    if not isinstance(resolved, str) or not _RESOLVED_PERSON.fullmatch(resolved):
        return None
    if resolved in _owner_principal_ids(principal):
        raise GovernanceError(
            "self_approval_forbidden", context={"approver": resolved}
        )
    return resolved


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------


def _safe_scope(scope: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        return validate_resource_scope(scope, require_concrete=False)
    except GovernanceError:
        return {
            "scope_type": "undetermined",
            "resource_ids": [],
            "connector_ids": [],
            "data_classifications": [],
            "exclude_resource_ids": [],
        }


def _constraints(
    *,
    execution_mode: str,
    allowed_capabilities: Sequence[str],
    denied_capabilities: Sequence[str],
    resource_scope: Mapping[str, Any],
    ttl_seconds: int,
    max_calls: int,
    max_cost_units: float | None,
    max_runtime_seconds: int | None,
    required_confirmation: bool,
    required_approval: bool,
    confirmation_ref: Mapping[str, Any] | None,
    approval_ref: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "execution_mode": execution_mode,
        "allowed_capabilities": list(allowed_capabilities),
        "denied_capabilities": list(denied_capabilities),
        "resource_scope": dict(resource_scope),
        "ttl_seconds": ttl_seconds,
        "max_calls": max_calls,
        "max_cost_units": max_cost_units,
        "max_runtime_seconds": max_runtime_seconds,
        "required_confirmation": required_confirmation,
        "required_approval": required_approval,
        "confirmation_ref": dict(confirmation_ref) if confirmation_ref else None,
        "approval_ref": dict(approval_ref) if approval_ref else None,
    }


def _policy_version_document(
    row: GovernancePolicyVersion | None,
    *,
    policy_set_id: str,
    version: str | None,
) -> dict[str, Any]:
    if row is None:
        if not isinstance(policy_set_id, str) or not _POLICY_SET_ID.fullmatch(
            policy_set_id
        ):
            policy_set_id = DEFAULT_POLICY_SET_ID
        if not isinstance(version, str) or not _SEMVER.fullmatch(version):
            version = "0.0.0"
        return {
            "policy_set_id": policy_set_id,
            "version": version,
            "content_hash": _ZERO_HASH,
        }
    document: dict[str, Any] = {
        "policy_set_id": row.policy_set_id,
        "version": row.version,
        "content_hash": row.content_hash,
    }
    if row.published_at is not None:
        document["effective_at"] = _rfc3339(row.published_at)
    return document


def _request_hash(request: EvaluationRequest) -> str:
    payload = {
        "principal_id": request.principal.principal_id,
        "principal_type": request.principal.principal_type,
        "service_identity_id": request.principal.service_identity_id,
        "employee_id": request.principal.employee_id,
        "capability": request.capability,
        "task_id": request.task_id,
        "task_revision": request.task_revision,
        "task_idempotency_key": request.task_idempotency_key,
        "resource_scope": _safe_scope(request.resource_scope),
        "priority": request.priority,
        "execution_mode": request.execution_mode,
        "risk": request.risk,
        "delegation_ref": request.delegation_ref or request.principal.delegation_ref,
        "confirmation_ref": request.confirmation_ref,
        "approval_ref": request.approval_ref,
        "policy_set_id": request.policy_set_id,
        "policy_version": request.policy_version,
        "decision_purpose": request.decision_purpose,
        "pre_evaluation": request.pre_evaluation,
    }
    return sha256_digest(canonical_json(payload).encode("utf-8"))


def _next_revision(
    session: Session, *, task_id: str | None, principal_id: str
) -> int:
    if not task_id:
        return 1
    current = session.scalar(
        select(func.max(GovernanceDecisionRecord.decision_revision)).where(
            GovernanceDecisionRecord.task_id == task_id,
            GovernanceDecisionRecord.principal_id == principal_id,
        )
    )
    return int(current or 0) + 1


def _build_decision(
    *,
    session: Session,
    request: EvaluationRequest,
    decision_type: str,
    reason_codes: Sequence[str],
    policy_row: GovernancePolicyVersion | None,
    constraints: dict[str, Any],
    evaluated_at: datetime,
    expires_at: datetime,
    decision_purpose: str,
    supersedes_decision_id: str | None,
    explanation: str | None = None,
) -> GovernanceDecision:
    unique_reasons = tuple(dict.fromkeys(reason_codes))
    if not unique_reasons:
        unique_reasons = ("policy_denied",)
    if len(unique_reasons) > 8:
        unique_reasons = unique_reasons[:8]
    unknown = set(unique_reasons) - REASON_CODES
    if unknown:
        raise ValueError(f"unknown reason codes: {sorted(unknown)}")
    if decision_type in {"allow", "managed_execution_required"}:
        allowed_reasons = ALLOW_REASON_CODES
    elif decision_type == "deny":
        allowed_reasons = DENY_REASON_CODES
    else:
        allowed_reasons = NEEDS_REASON_CODES
    invalid = set(unique_reasons) - allowed_reasons
    if invalid:
        raise ValueError(
            f"reason codes not valid for {decision_type}: {sorted(invalid)}"
        )

    decision_id = _new_id("gdec_")
    correlation_id = (
        request.correlation_id
        if isinstance(request.correlation_id, str)
        and _CORRELATION_ID.fullmatch(request.correlation_id)
        else _new_id("corr_")
    )
    revision = _next_revision(
        session,
        task_id=request.task_id,
        principal_id=request.principal.principal_id,
    )

    policy_version = _policy_version_document(
        policy_row,
        policy_set_id=request.policy_set_id,
        version=request.policy_version,
    )
    safe_task_id = (
        request.task_id
        if isinstance(request.task_id, str) and _TASK_ID.fullmatch(request.task_id)
        else None
    )
    safe_idempotency = (
        request.task_idempotency_key
        if isinstance(request.task_idempotency_key, str)
        and _IDEM_KEY.fullmatch(request.task_idempotency_key)
        else None
    )
    if safe_idempotency is None and safe_task_id is not None:
        # Reuse the task submission key from the previous decision so a retry
        # stays bound to the same task identity instead of inventing a new one.
        previous_key = session.scalar(
            select(GovernanceDecisionRecord)
            .where(
                GovernanceDecisionRecord.task_id == safe_task_id,
                GovernanceDecisionRecord.principal_id
                == request.principal.principal_id,
            )
            .order_by(GovernanceDecisionRecord.decision_revision.desc())
            .limit(1)
            .with_only_columns(GovernanceDecisionRecord.document)
        )
        if isinstance(previous_key, Mapping):
            candidate = (previous_key.get("task_ref") or {}).get("idempotency_key")
            if isinstance(candidate, str) and _IDEM_KEY.fullmatch(candidate):
                safe_idempotency = candidate
    if safe_idempotency is None:
        safe_idempotency = _new_id("idem_")
    safe_task_revision = (
        request.task_revision
        if isinstance(request.task_revision, int)
        and not isinstance(request.task_revision, bool)
        and request.task_revision >= 0
        else None
    )
    task_ref = {
        "task_id": None if decision_type == "pre_evaluation" else safe_task_id,
        "idempotency_key": safe_idempotency,
        "task_revision": (
            None if decision_type == "pre_evaluation" else safe_task_revision
        ),
    }
    body: dict[str, Any] = {
        "schema_version": "1.0.0",
        "decision_id": decision_id,
        "decision_revision": revision,
        "canonicalization": "rfc8785_jcs",
        "decision_type": decision_type,
        "decision_purpose": decision_purpose,
        "reason_codes": list(unique_reasons),
        "policy_version": policy_version,
        "subject": request.principal.subject_document(),
        "task_ref": task_ref,
        "evaluated_at": _rfc3339(evaluated_at),
        "expires_at": _rfc3339(expires_at),
        "fail_closed": True,
        "constraints": constraints,
        "obligations": _obligations(decision_type, request, constraints),
        "supersedes_decision_id": supersedes_decision_id,
        "correlation_id": correlation_id,
        "explanation": explanation,
    }
    decision_hash = sha256_digest(canonical_json(body).encode("utf-8"))

    return GovernanceDecision(
        schema_version="1.0.0",
        decision_id=decision_id,
        decision_revision=revision,
        decision_hash=decision_hash,
        canonicalization="rfc8785_jcs",
        decision_type=decision_type,
        decision_purpose=decision_purpose,
        reason_codes=unique_reasons,
        policy_version=policy_version,
        subject=request.principal.subject_document(),
        task_ref=task_ref,
        evaluated_at=body["evaluated_at"],
        expires_at=body["expires_at"],
        fail_closed=True,
        constraints=constraints,
        obligations=body["obligations"],
        supersedes_decision_id=supersedes_decision_id,
        correlation_id=correlation_id,
        explanation=explanation,
    )


def _obligations(
    decision_type: str,
    request: EvaluationRequest,
    constraints: Mapping[str, Any],
) -> tuple[str, ...]:
    obligations: list[str] = ["audit_decision"]
    if decision_type in {"allow", "managed_execution_required"}:
        obligations.append("revalidate_each_call")
    if decision_type == "managed_execution_required":
        obligations.append("require_managed_execution")
    if constraints.get("required_confirmation") or constraints.get(
        "required_approval"
    ):
        obligations.append("record_evidence")
    if request.principal.is_service:
        obligations.append("notify_owner")
    # Preserve first-seen order and remove duplicates.
    return tuple(dict.fromkeys(obligations))


def decision_document_hash(document: Mapping[str, Any]) -> str:
    """Return the contract hash: JCS of the document without ``decision_hash``."""
    body = dict(document)
    body.pop("decision_hash", None)
    return sha256_digest(canonical_json(body).encode("utf-8"))


def verify_decision_document(record: GovernanceDecisionRecord) -> dict[str, Any]:
    """Verify a persisted decision document and return it, else fail closed.

    Grants and the PEP derive every field from this document, so a mismatch
    between the stored hash, the stored document and the row must never be
    tolerated.
    """
    document = record.document
    if not isinstance(document, Mapping):
        raise GovernanceError(
            "policy_version_invalid", context={"reason": "decision_document_missing"}
        )
    if document.get("decision_id") != record.decision_id:
        raise GovernanceError(
            "policy_version_invalid", context={"reason": "decision_id_mismatch"}
        )
    if document.get("decision_type") != record.decision_type:
        raise GovernanceError(
            "policy_version_invalid", context={"reason": "decision_type_mismatch"}
        )
    if document.get("decision_revision") != record.decision_revision:
        raise GovernanceError(
            "policy_version_invalid", context={"reason": "decision_revision_mismatch"}
        )
    computed = decision_document_hash(document)
    stored_document_hash = document.get("decision_hash")
    if (
        computed != record.decision_hash
        or stored_document_hash != record.decision_hash
    ):
        raise GovernanceError(
            "policy_version_invalid", context={"reason": "decision_hash_mismatch"}
        )
    if document.get("constraints") != record.constraints:
        raise GovernanceError(
            "policy_version_invalid", context={"reason": "constraints_mismatch"}
        )
    return dict(document)


def _persist(
    session: Session,
    *,
    request: EvaluationRequest,
    decision: GovernanceDecision,
    evaluated_at: datetime,
    request_hash: str,
    policy_row: GovernancePolicyVersion | None,
) -> GovernanceDecision:
    task_ref = decision.task_ref
    record = GovernanceDecisionRecord(
        decision_id=decision.decision_id,
        decision_revision=decision.decision_revision,
        decision_hash=decision.decision_hash,
        decision_purpose=decision.decision_purpose,
        supersedes_decision_id=decision.supersedes_decision_id,
        correlation_id=decision.correlation_id,
        task_id=task_ref.get("task_id"),
        task_revision=task_ref.get("task_revision"),
        principal_type=request.principal.principal_type,
        principal_id=request.principal.principal_id,
        decision_type=decision.decision_type,
        reason_codes=list(decision.reason_codes),
        policy_set_id=decision.policy_version["policy_set_id"],
        policy_version=decision.policy_version["version"],
        policy_content_hash=(
            policy_row.content_hash if policy_row is not None else None
        ),
        constraints=decision.constraints,
        document=decision.to_document(),
        request_hash=request_hash,
        evaluated_at=evaluated_at,
        expires_at=_parse_rfc3339(decision.expires_at) or evaluated_at,
    )
    # The unique (task_id, principal_id, decision_revision) constraint is the
    # concurrency guard: two evaluators racing for the same revision produce
    # one row, and the loser gets a contract-shaped stale-revision error
    # instead of a raw IntegrityError.  A savepoint keeps the outer
    # transaction usable after the conflict.
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError as exc:
        raise GovernanceError(
            "decision_revision_stale",
            context={
                "task_id": request.task_id,
                "principal_id": request.principal.principal_id,
                "decision_revision": decision.decision_revision,
            },
        ) from exc

    actor_id = request.principal.employee_id
    if decision.decision_type in {"allow", "managed_execution_required"}:
        action = "governance.allowed"
    elif decision.decision_type == "deny":
        action = "governance.denied"
    else:
        action = "governance.evaluated"
    record_event(
        session,
        actor_id=actor_id,
        action=action,
        resource_type="governance_decision",
        resource_id=decision.decision_id,
        request_id=decision.correlation_id,
        details={
            "decision_id": decision.decision_id,
            "decision_type": decision.decision_type,
            "decision_revision": decision.decision_revision,
            "reason_codes": list(decision.reason_codes),
            "policy_set_id": decision.policy_version["policy_set_id"],
            "policy_version": decision.policy_version["version"],
            "policy_content_hash": decision.policy_version["content_hash"],
            "capability": request.capability,
            "task_id": request.task_id,
            "task_revision": request.task_revision,
            "execution_mode": request.execution_mode,
            "priority": request.priority,
            "risk": request.risk,
            "principal_id": request.principal.principal_id,
            "service_identity_id": request.principal.service_identity_id,
        },
    )
    return decision


def _deny(
    session: Session,
    *,
    request: EvaluationRequest,
    reason_codes: Sequence[str],
    policy_row: GovernancePolicyVersion | None,
    evaluated_at: datetime,
    request_hash: str,
    decision_purpose: str,
    supersedes_decision_id: str | None,
    scope: Mapping[str, Any] | None = None,
) -> GovernanceDecision:
    constraints = _constraints(
        execution_mode="none",
        allowed_capabilities=[],
        denied_capabilities=(
            [request.capability]
            if isinstance(request.capability, str)
            and CAPABILITY_PATTERN.fullmatch(request.capability)
            else []
        ),
        resource_scope=_safe_scope(scope if scope is not None else request.resource_scope),
        ttl_seconds=0,
        max_calls=0,
        max_cost_units=None,
        max_runtime_seconds=None,
        required_confirmation=False,
        required_approval=False,
        confirmation_ref=None,
        approval_ref=None,
    )
    decision = _build_decision(
        session=session,
        request=request,
        decision_type="deny",
        reason_codes=reason_codes,
        policy_row=policy_row,
        constraints=constraints,
        evaluated_at=evaluated_at,
        expires_at=evaluated_at + timedelta(seconds=_NON_AUTHORIZING_TTL_SECONDS),
        decision_purpose=decision_purpose,
        supersedes_decision_id=supersedes_decision_id,
    )
    return _persist(
        session,
        request=request,
        decision=decision,
        evaluated_at=evaluated_at,
        request_hash=request_hash,
        policy_row=policy_row,
    )


def _non_authorizing(
    session: Session,
    *,
    request: EvaluationRequest,
    decision_type: str,
    reason_codes: Sequence[str],
    policy_row: GovernancePolicyVersion | None,
    evaluated_at: datetime,
    request_hash: str,
    decision_purpose: str,
    supersedes_decision_id: str | None,
    scope: Mapping[str, Any] | None = None,
    execution_mode: str = "none",
    required_confirmation: bool = False,
    required_approval: bool = False,
) -> GovernanceDecision:
    constraints = _constraints(
        execution_mode=execution_mode,
        allowed_capabilities=[],
        denied_capabilities=[],
        resource_scope=_safe_scope(scope if scope is not None else request.resource_scope),
        ttl_seconds=0,
        max_calls=0,
        max_cost_units=None,
        max_runtime_seconds=None,
        required_confirmation=required_confirmation,
        required_approval=required_approval,
        confirmation_ref=None,
        approval_ref=None,
    )
    decision = _build_decision(
        session=session,
        request=request,
        decision_type=decision_type,
        reason_codes=reason_codes,
        policy_row=policy_row,
        constraints=constraints,
        evaluated_at=evaluated_at,
        expires_at=evaluated_at + timedelta(seconds=_NON_AUTHORIZING_TTL_SECONDS),
        decision_purpose=decision_purpose,
        supersedes_decision_id=supersedes_decision_id,
    )
    return _persist(
        session,
        request=request,
        decision=decision,
        evaluated_at=evaluated_at,
        request_hash=request_hash,
        policy_row=policy_row,
    )


def _find_delegation(session: Session, reference: str) -> GovernanceDelegation | None:
    """Resolve a delegation reference.

    Accepts the explicit public ``delegation_id`` and, for compatibility with
    rows created from the minimal model shape, ``dlg_<numeric row id>``.
    """
    row = session.scalar(
        select(GovernanceDelegation).where(
            GovernanceDelegation.delegation_id == reference
        )
    )
    if row is not None:
        return row
    if reference.startswith("dlg_"):
        try:
            row_id = int(reference[len("dlg_") :])
        except ValueError:
            return None
        return session.get(GovernanceDelegation, row_id)
    return None


def _validate_delegation(
    session: Session,
    *,
    reference: str,
    principal: GovernancePrincipal,
    rule: PolicyRule,
    policy_row: GovernancePolicyVersion,
    capability: str,
    scope: Mapping[str, Any],
    now: datetime,
) -> GovernanceDelegation:
    if not rule.delegation_allowed:
        raise GovernanceError("capability_not_granted", context={"delegation": "not_allowed"})
    row = _find_delegation(session, reference)
    if row is None:
        raise GovernanceError("delegation_expired", context={"delegation_ref": reference})
    if row.status != "active":
        raise GovernanceError("delegation_expired", context={"status": row.status})
    if not (row.starts_at <= now < row.expires_at):
        raise GovernanceError("delegation_expired", context={"delegation_ref": reference})
    delegator = session.get(Employee, row.delegator_employee_id)
    if delegator is None or delegator.status != "active":
        raise GovernanceError(
            "delegation_expired",
            context={
                "delegation_ref": reference,
                "reason": "delegator_inactive",
            },
        )
    if row.policy_set_id is not None and row.policy_set_id != policy_row.policy_set_id:
        raise GovernanceError("policy_version_invalid", context={"delegation_ref": reference})
    if row.policy_version != policy_row.version:
        raise GovernanceError("policy_version_invalid", context={"delegation_ref": reference})

    if principal.service_identity_pk is not None:
        if row.delegatee_service_identity_id != principal.service_identity_pk:
            raise GovernanceError("delegation_expired", context={"reason": "wrong_delegatee"})
    elif row.delegatee_employee_id != principal.employee_id:
        raise GovernanceError("delegation_expired", context={"reason": "wrong_delegatee"})

    if not any(
        capability_matches(pattern, capability) for pattern in row.capabilities
    ):
        raise GovernanceError("capability_not_granted", context={"delegation_ref": reference})

    if not scope_is_subset(scope, row.resource_scope):
        raise GovernanceError("resource_scope_denied", context={"delegation_ref": reference})

    # A delegation can never exceed the delegator's own permissions.
    delegator_permissions = effective_permissions(session, row.delegator_employee_id)
    missing = [
        triple for triple in rule.required_permissions if triple not in delegator_permissions
    ]
    if missing:
        raise GovernanceError(
            "capability_not_granted", context={"delegation_ref": reference}
        )
    return row


def evaluate(
    session: Session,
    request: EvaluationRequest | GovernancePrincipal | None = None,
    *,
    principal: GovernancePrincipal | None = None,
    now: datetime | None = None,
    evidence_verifier: ApprovalEvidenceVerifier | None = None,
    **request_fields: Any,
) -> GovernanceDecision:
    """Evaluate one governance request deterministically and persist the decision.

    The canonical form is ``evaluate(session, EvaluationRequest(...))``.  For
    caller convenience the same function accepts ``evaluate(session, principal,
    capability=..., task_id=..., ...)``; the principal must still have been
    resolved from a trusted context.
    """
    if isinstance(request, GovernancePrincipal):
        if principal is None:
            principal = request
        request = None
    if request is None:
        if principal is None:
            raise TypeError("evaluate requires an EvaluationRequest or principal=")
        if "capability" not in request_fields:
            raise TypeError("evaluate requires capability=")
        request = EvaluationRequest(principal=principal, **request_fields)
    elif request_fields:
        raise TypeError(
            "do not combine an EvaluationRequest with keyword request fields"
        )

    evaluated_at = now or _utcnow()
    request_hash = _request_hash(request)
    verifier = evidence_verifier or DENY_ALL_EVIDENCE
    decision_purpose = request.decision_purpose
    if decision_purpose not in {"initial_execution", "retry"}:
        decision_purpose = "initial_execution"
    supersedes = request.supersedes_decision_id

    def deny(reason_codes: Sequence[str], *, policy_row=None, scope=None):
        return _deny(
            session,
            request=request,
            reason_codes=reason_codes,
            policy_row=policy_row,
            evaluated_at=evaluated_at,
            request_hash=request_hash,
            decision_purpose=decision_purpose,
            supersedes_decision_id=supersedes,
            scope=scope,
        )

    # 1. Trusted principal.
    if request.principal.identity_assurance not in VERIFIED_ASSURANCES:
        return deny(["identity_unverified"])
    if not request.principal.is_active:
        return deny(["principal_disabled"])

    # 1b. Request-shape validation: reject malformed identifiers before they
    # reach a database cast or become a silently-ignored field.
    if request.task_id is not None and (
        not isinstance(request.task_id, str)
        or not _TASK_ID.fullmatch(request.task_id)
    ):
        return deny(["required_input_missing"], scope=request.resource_scope)
    if request.task_revision is not None and (
        not isinstance(request.task_revision, int)
        or isinstance(request.task_revision, bool)
        or request.task_revision < 0
        or request.task_revision > 2**53 - 1
    ):
        return deny(["required_input_missing"], scope=request.resource_scope)
    if request.task_idempotency_key is not None and (
        not isinstance(request.task_idempotency_key, str)
        or not _IDEM_KEY.fullmatch(request.task_idempotency_key)
    ):
        return deny(["required_input_missing"], scope=request.resource_scope)
    if request.payload_hash is not None and (
        not isinstance(request.payload_hash, str)
        or not _SHA256.fullmatch(request.payload_hash)
    ):
        return deny(["payload_hash_mismatch"], scope=request.resource_scope)
    if request.correlation_id is not None and (
        not isinstance(request.correlation_id, str)
        or not _CORRELATION_ID.fullmatch(request.correlation_id)
    ):
        return deny(["required_input_missing"], scope=request.resource_scope)
    if request.delegation_ref is not None and (
        not isinstance(request.delegation_ref, str)
        or not re.fullmatch(
            r"^dlg_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$", request.delegation_ref
        )
    ):
        return deny(["required_input_missing"], scope=request.resource_scope)

    # 1c. A retry must supersede the current decision of the same task and
    # principal.  There is no synthetic revision 2 without history.
    if decision_purpose == "retry":
        if not request.task_id:
            return deny(["task_not_created"], scope=request.resource_scope)
        latest = session.scalar(
            select(GovernanceDecisionRecord)
            .where(
                GovernanceDecisionRecord.task_id == request.task_id,
                GovernanceDecisionRecord.principal_id
                == request.principal.principal_id,
            )
            .order_by(GovernanceDecisionRecord.decision_revision.desc())
            .limit(1)
        )
        if latest is None:
            # There is no retry chain to join; record a plain denial instead of
            # fabricating revision 2 or a supersedes reference.
            decision_purpose = "initial_execution"
            supersedes = None
            return deny(["decision_revision_stale"], scope=request.resource_scope)
        if supersedes is not None:
            superseded = session.scalar(
                select(GovernanceDecisionRecord).where(
                    GovernanceDecisionRecord.decision_id == supersedes
                )
            )
            if (
                superseded is None
                or superseded.task_id != request.task_id
                or superseded.principal_id != request.principal.principal_id
                or superseded.decision_revision != latest.decision_revision
            ):
                # Keep the deny on the real chain rather than referencing a
                # decision that does not exist or belongs elsewhere.
                supersedes = latest.decision_id
                return deny(
                    ["decision_revision_stale"], scope=request.resource_scope
                )
        else:
            supersedes = latest.decision_id

    # 2. Policy must exist and be the published version (fail closed).
    try:
        policy_row = get_active(
            session, request.policy_set_id, request.policy_version
        )
    except PolicyError as exc:
        return deny([exc.reason_code])
    if policy_row is None:
        reason = "policy_version_invalid" if request.policy_version else "policy_unavailable"
        return deny([reason])
    try:
        policy = parse_policy_document(policy_row.document)
    except GovernanceError as exc:
        return deny([exc.reason_code], policy_row=policy_row)
    if content_hash(policy_row.document) != policy_row.content_hash:
        return deny(["policy_version_invalid"], policy_row=policy_row)

    # 3. A malformed capability is not a usable request; a well-formed but
    # unknown capability falls through to the rule match below.
    if not isinstance(request.capability, str) or not CAPABILITY_PATTERN.fullmatch(
        request.capability
    ):
        return _non_authorizing(
            session,
            request=request,
            decision_type="needs_clarification",
            reason_codes=["capability_ambiguous"],
            policy_row=policy_row,
            evaluated_at=evaluated_at,
            request_hash=request_hash,
            decision_purpose=decision_purpose,
            supersedes_decision_id=supersedes,
            scope=request.resource_scope,
        )

    # 4. Hard deny wins over everything except identity/policy availability.
    hard_deny = policy.hard_deny_for(request.capability)
    if hard_deny is not None:
        reasons = [hard_deny.reason_code or "policy_denied"]
        if request.priority in _ESCALATING_PRIORITIES:
            reasons.append("priority_cannot_override_deny")
        return deny(reasons, policy_row=policy_row)

    # 5. Rule match.
    rule = policy.rule_for(request.capability)
    if rule is None:
        return deny(["capability_not_granted"], policy_row=policy_row)
    if rule.effect == "deny":
        reasons = [rule.reason_code or "policy_denied"]
        if request.priority in _ESCALATING_PRIORITIES:
            reasons.append("priority_cannot_override_deny")
        return deny(reasons, policy_row=policy_row)

    # 6. Risk and execution mode.
    if request.risk not in RISK_LEVELS or request.risk not in rule.risk_levels:
        return deny(["risk_above_ceiling"], policy_row=policy_row)
    if request.execution_mode not in EXECUTION_MODES:
        return deny(["unsupported_execution_mode"], policy_row=policy_row)
    if request.execution_mode not in rule.allowed_execution_modes:
        return deny(["unsupported_execution_mode"], policy_row=policy_row)

    # 7. Existing RBAC authority (no second permission model).
    missing_permissions = [
        triple
        for triple in rule.required_permissions
        if triple not in request.principal.effective_permissions
    ]
    if missing_permissions:
        return deny(
            ["capability_not_granted"],
            policy_row=policy_row,
            scope=request.resource_scope,
        )

    # 8. Resource scope: missing/undetermined is ambiguous, not "all".
    raw_scope = request.resource_scope
    if raw_scope is None or (
        isinstance(raw_scope, Mapping)
        and raw_scope.get("scope_type") in {None, "undetermined"}
    ):
        return _non_authorizing(
            session,
            request=request,
            decision_type="needs_clarification",
            reason_codes=["resource_scope_ambiguous"],
            policy_row=policy_row,
            evaluated_at=evaluated_at,
            request_hash=request_hash,
            decision_purpose=decision_purpose,
            supersedes_decision_id=supersedes,
            scope=raw_scope,
        )
    try:
        scope = validate_resource_scope(raw_scope, require_concrete=True)
    except GovernanceError as exc:
        if exc.reason_code == "resource_scope_ambiguous":
            return _non_authorizing(
                session,
                request=request,
                decision_type="needs_clarification",
                reason_codes=["resource_scope_ambiguous"],
                policy_row=policy_row,
                evaluated_at=evaluated_at,
                request_hash=request_hash,
                decision_purpose=decision_purpose,
                supersedes_decision_id=supersedes,
                scope=raw_scope,
            )
        return deny([exc.reason_code], policy_row=policy_row, scope=raw_scope)

    if scope["scope_type"] not in rule.allowed_scope_types:
        return deny(["resource_scope_denied"], policy_row=policy_row, scope=scope)
    if rule.allowed_resource_ids is not None and not set(
        scope["resource_ids"]
    ).issubset(rule.allowed_resource_ids):
        return deny(["resource_scope_denied"], policy_row=policy_row, scope=scope)
    if rule.allowed_connector_ids is not None and not set(
        scope["connector_ids"]
    ).issubset(rule.allowed_connector_ids):
        return deny(["resource_scope_denied"], policy_row=policy_row, scope=scope)
    if not set(scope["data_classifications"]).issubset(
        rule.allowed_data_classifications
    ):
        return deny(["data_classification_denied"], policy_row=policy_row, scope=scope)

    # 9. Delegation, if the caller relies on one.
    delegation_reference = request.delegation_ref or request.principal.delegation_ref
    if delegation_reference is not None:
        try:
            _validate_delegation(
                session,
                reference=delegation_reference,
                principal=request.principal,
                rule=rule,
                policy_row=policy_row,
                capability=request.capability,
                scope=scope,
                now=evaluated_at,
            )
        except GovernanceError as exc:
            return deny([exc.reason_code], policy_row=policy_row, scope=scope)
        # Persist the delegation that actually authorized this decision on the
        # decision subject, so the grant can bind and revalidate it.
        if request.principal.delegation_ref != delegation_reference:
            request = replace(
                request,
                principal=replace(
                    request.principal, delegation_ref=delegation_reference
                ),
            )

    # 10. Confirmation and approval gates.
    owed_reasons: list[str] = []
    confirmation_ref: dict[str, Any] | None = None
    approval_ref: dict[str, Any] | None = None
    if rule.required_confirmation:
        if request.confirmation_ref is None:
            return _non_authorizing(
                session,
                request=request,
                decision_type="needs_confirmation",
                reason_codes=["confirmation_required"],
                policy_row=policy_row,
                evaluated_at=evaluated_at,
                request_hash=request_hash,
                decision_purpose=decision_purpose,
                supersedes_decision_id=supersedes,
                scope=scope,
                required_confirmation=True,
                required_approval=rule.required_approval,
            )
        try:
            confirmation_ref = validate_confirmation_ref(
                request.confirmation_ref,
                payload_hash=request.payload_hash,
                now=evaluated_at,
            )
        except GovernanceError as exc:
            return deny([exc.reason_code], policy_row=policy_row, scope=scope)
        try:
            verified = verifier.verify_confirmation(
                confirmation_ref,
                principal_id=request.principal.principal_id,
                payload_hash=request.payload_hash,
                now=evaluated_at,
            )
        except Exception:  # noqa: BLE001 - untrusted verifier: fail closed
            verified = False
        if not verified:
            # A caller-asserted reference is not evidence.
            return _non_authorizing(
                session,
                request=request,
                decision_type="needs_confirmation",
                reason_codes=["confirmation_required"],
                policy_row=policy_row,
                evaluated_at=evaluated_at,
                request_hash=request_hash,
                decision_purpose=decision_purpose,
                supersedes_decision_id=supersedes,
                scope=scope,
                required_confirmation=True,
                required_approval=rule.required_approval,
            )
        owed_reasons.append("confirmation_satisfied")
    if rule.required_approval:
        if request.approval_ref is None:
            return _non_authorizing(
                session,
                request=request,
                decision_type="needs_approval",
                reason_codes=["approval_required"],
                policy_row=policy_row,
                evaluated_at=evaluated_at,
                request_hash=request_hash,
                decision_purpose=decision_purpose,
                supersedes_decision_id=supersedes,
                scope=scope,
                required_confirmation=rule.required_confirmation,
                required_approval=True,
            )
        try:
            approval_ref = validate_approval_ref(
                request.approval_ref,
                principal_id=request.principal.principal_id,
                owner_principal_ids=_owner_principal_ids(request.principal),
                payload_hash=request.payload_hash,
                now=evaluated_at,
            )
            resolved_approver = _verify_approval_evidence(
                verifier,
                approval_ref,
                principal=request.principal,
                payload_hash=request.payload_hash,
                now=evaluated_at,
            )
        except GovernanceError as exc:
            return deny([exc.reason_code], policy_row=policy_row, scope=scope)
        except Exception:  # noqa: BLE001 - untrusted verifier: fail closed
            resolved_approver = None
        if resolved_approver is None:
            return _non_authorizing(
                session,
                request=request,
                decision_type="needs_approval",
                reason_codes=["approval_required"],
                policy_row=policy_row,
                evaluated_at=evaluated_at,
                request_hash=request_hash,
                decision_purpose=decision_purpose,
                supersedes_decision_id=supersedes,
                scope=scope,
                required_confirmation=rule.required_confirmation,
                required_approval=True,
            )
        owed_reasons.append("approval_satisfied")

    # 11. Pre-evaluation is never authorizing.
    if request.pre_evaluation:
        return _non_authorizing(
            session,
            request=request,
            decision_type="pre_evaluation",
            reason_codes=["pre_evaluation_only"],
            policy_row=policy_row,
            evaluated_at=evaluated_at,
            request_hash=request_hash,
            decision_purpose=decision_purpose,
            supersedes_decision_id=supersedes,
            scope=scope,
            execution_mode="none",
        )

    # 12. Authorizing decisions must bind an existing task revision.  A request
    # before task creation can only be a non-authorizing pre-evaluation.
    if not request.task_id:
        return _non_authorizing(
            session,
            request=request,
            decision_type="pre_evaluation",
            reason_codes=["task_not_created"],
            policy_row=policy_row,
            evaluated_at=evaluated_at,
            request_hash=request_hash,
            decision_purpose=decision_purpose,
            supersedes_decision_id=supersedes,
            scope=scope,
        )
    if (
        not isinstance(request.task_revision, int)
        or isinstance(request.task_revision, bool)
        or request.task_revision < 1
    ):
        return _non_authorizing(
            session,
            request=request,
            decision_type="needs_clarification",
            reason_codes=["required_input_missing"],
            policy_row=policy_row,
            evaluated_at=evaluated_at,
            request_hash=request_hash,
            decision_purpose=decision_purpose,
            supersedes_decision_id=supersedes,
            scope=scope,
        )

    managed_required = (
        request.execution_mode == "managed_execution"
        or rule.requires_managed_execution
    )
    decision_type = (
        "managed_execution_required" if managed_required else "allow"
    )
    execution_mode = "managed_execution" if managed_required else request.execution_mode
    ttl_seconds = min(rule.ttl_seconds, policy.decision_ttl_seconds)
    if ttl_seconds < 1:
        return deny(["policy_version_invalid"], policy_row=policy_row, scope=scope)
    expires_at = evaluated_at + timedelta(seconds=ttl_seconds)

    # Managed decisions have a narrower reason-code vocabulary than allow; the
    # gate/delegation evidence lives in constraints and is re-checked by the PEP.
    if decision_type == "managed_execution_required":
        reasons = ["managed_execution_required"]
    else:
        reasons = ["policy_allowed", *owed_reasons]
        if delegation_reference is not None:
            reasons.append("delegated_authority_valid")

    constraints = _constraints(
        execution_mode=execution_mode,
        allowed_capabilities=[request.capability],
        denied_capabilities=[],
        resource_scope=scope,
        ttl_seconds=ttl_seconds,
        max_calls=rule.max_calls,
        max_cost_units=rule.max_cost_units,
        max_runtime_seconds=rule.max_runtime_seconds,
        required_confirmation=rule.required_confirmation,
        required_approval=rule.required_approval,
        confirmation_ref=confirmation_ref,
        approval_ref=approval_ref,
    )
    decision = _build_decision(
        session=session,
        request=request,
        decision_type=decision_type,
        reason_codes=reasons,
        policy_row=policy_row,
        constraints=constraints,
        evaluated_at=evaluated_at,
        expires_at=expires_at,
        decision_purpose=decision_purpose,
        supersedes_decision_id=supersedes,
    )
    return _persist(
        session,
        request=request,
        decision=decision,
        evaluated_at=evaluated_at,
        request_hash=request_hash,
        policy_row=policy_row,
    )
