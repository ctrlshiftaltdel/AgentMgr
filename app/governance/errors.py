"""Stable governance errors and reason codes.

The reason-code set is copied from the frozen Phase 0 contract
``docs/contracts/governance-decision.schema.json`` (``$defs.reasonCode``).  New
codes are backwards-compatible additions; removing or redefining one is a major
contract change and must not be done here.

Design rules
------------

* The exception message is **not** an authorization signal.  It is a stable,
  short, machine-oriented string derived from the reason code; no identity,
  credential, payload or other sensitive value is ever interpolated into it.
* Detailed context (task id, capability, principal id) is kept in
  :attr:`GovernanceError.context` for audit/debugging, never in ``str(exc)``.
* Unknown reason codes are rejected at construction time so a typo cannot
  silently become an unrecognised security event.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

__all__ = [
    "ALLOW_REASON_CODES",
    "DECISION_REASON_CODES",
    "DECISION_TYPES",
    "DENY_REASON_CODES",
    "GRANT_REVOCATION_REASONS",
    "MANAGED_EXECUTION_REASON_CODES",
    "NEEDS_APPROVAL_REASON_CODES",
    "NEEDS_CLARIFICATION_REASON_CODES",
    "NEEDS_CONFIRMATION_REASON_CODES",
    "NEEDS_REASON_CODES",
    "PRE_EVALUATION_REASON_CODES",
    "REASON_CODES",
    "DelegationError",
    "GovernanceError",
    "GrantError",
    "PolicyError",
    "PrincipalResolutionError",
    "ToolCallDeniedError",
    "deny",
]

#: Every reason code defined by the frozen governance-decision contract.
REASON_CODES: frozenset[str] = frozenset(
    {
        "policy_allowed",
        "delegated_authority_valid",
        "approval_satisfied",
        "confirmation_satisfied",
        "managed_execution_boundary_verified",
        "pre_evaluation_only",
        "task_not_created",
        "identity_unverified",
        "principal_disabled",
        "principal_unbound",
        "delegation_expired",
        "capability_not_granted",
        "resource_scope_denied",
        "data_classification_denied",
        "policy_denied",
        "policy_unavailable",
        "policy_version_invalid",
        "tool_contract_mismatch",
        "separation_of_duties_violation",
        "self_approval_forbidden",
        "accountable_owner_missing",
        "approval_missing",
        "confirmation_missing",
        "grant_missing",
        "grant_expired",
        "grant_revoked",
        "grant_consumed",
        "grant_scope_mismatch",
        "grant_outlives_decision",
        "budget_exceeded",
        "payload_hash_mismatch",
        "replay_detected",
        "decision_expired",
        "decision_revision_stale",
        "execution_mode_mismatch",
        "priority_cannot_override_deny",
        "unsupported_execution_mode",
        "runtime_capability_unverified",
        "retry_budget_exhausted",
        "risk_above_ceiling",
        "goal_missing",
        "required_input_missing",
        "resource_scope_ambiguous",
        "accountable_owner_unresolved",
        "due_date_ambiguous",
        "capability_ambiguous",
        "confirmation_required",
        "payload_changed",
        "high_impact_action",
        "approval_required",
        "risk_threshold_exceeded",
        "amount_threshold_exceeded",
        "separation_of_duties_required",
        "client_local_control_insufficient",
        "managed_execution_required",
        "capability_requires_pep",
        "runtime_isolation_required",
        "evidence_missing",
        "stop_not_confirmed",
    }
)

#: Reason codes that may appear on an ``allow`` decision.
#: Exactly the frozen ``governance-decision.schema.json`` enum for
#: ``decision_type = allow``.
ALLOW_REASON_CODES: frozenset[str] = frozenset(
    {
        "policy_allowed",
        "delegated_authority_valid",
        "approval_satisfied",
        "confirmation_satisfied",
        "managed_execution_boundary_verified",
    }
)

#: Reason codes that may appear on a ``managed_execution_required`` decision.
MANAGED_EXECUTION_REASON_CODES: frozenset[str] = frozenset(
    {
        "client_local_control_insufficient",
        "managed_execution_required",
        "capability_requires_pep",
        "runtime_isolation_required",
        "managed_execution_boundary_verified",
    }
)

#: Reason codes that may appear on a deny decision: exactly the frozen schema
#: enum for ``decision_type = deny``.
DENY_REASON_CODES: frozenset[str] = frozenset(
    {
        "identity_unverified",
        "principal_disabled",
        "principal_unbound",
        "delegation_expired",
        "capability_not_granted",
        "resource_scope_denied",
        "data_classification_denied",
        "policy_denied",
        "policy_unavailable",
        "policy_version_invalid",
        "tool_contract_mismatch",
        "separation_of_duties_violation",
        "self_approval_forbidden",
        "accountable_owner_missing",
        "approval_missing",
        "confirmation_missing",
        "grant_missing",
        "grant_expired",
        "grant_revoked",
        "grant_consumed",
        "grant_scope_mismatch",
        "grant_outlives_decision",
        "budget_exceeded",
        "payload_hash_mismatch",
        "replay_detected",
        "decision_expired",
        "decision_revision_stale",
        "execution_mode_mismatch",
        "priority_cannot_override_deny",
        "unsupported_execution_mode",
        "runtime_capability_unverified",
        "retry_budget_exhausted",
        "risk_above_ceiling",
        "evidence_missing",
        "stop_not_confirmed",
    }
)

#: Reason codes allowed on ``needs_clarification`` (frozen schema enum).
NEEDS_CLARIFICATION_REASON_CODES: frozenset[str] = frozenset(
    {
        "goal_missing",
        "required_input_missing",
        "resource_scope_ambiguous",
        "accountable_owner_unresolved",
        "due_date_ambiguous",
        "capability_ambiguous",
    }
)

#: Reason codes allowed on ``needs_confirmation`` (frozen schema enum).
NEEDS_CONFIRMATION_REASON_CODES: frozenset[str] = frozenset(
    {"confirmation_required", "payload_changed", "high_impact_action"}
)

#: Reason codes allowed on ``needs_approval`` (frozen schema enum).
NEEDS_APPROVAL_REASON_CODES: frozenset[str] = frozenset(
    {
        "approval_required",
        "risk_threshold_exceeded",
        "amount_threshold_exceeded",
        "separation_of_duties_required",
    }
)

#: Reason codes allowed on ``pre_evaluation`` (frozen schema enum).
PRE_EVALUATION_REASON_CODES: frozenset[str] = frozenset(
    {
        "pre_evaluation_only",
        "task_not_created",
        "policy_denied",
        "required_input_missing",
        "client_local_control_insufficient",
    }
)

#: Union of the non-authorizing ``needs_*`` sets (compatibility export).
NEEDS_REASON_CODES: frozenset[str] = (
    NEEDS_CLARIFICATION_REASON_CODES
    | NEEDS_CONFIRMATION_REASON_CODES
    | NEEDS_APPROVAL_REASON_CODES
)

#: The exact per-decision-type reason-code vocabulary.  A decision may only
#: carry codes from its own set; the builder enforces this at construction time.
DECISION_REASON_CODES: dict[str, frozenset[str]] = {
    "allow": ALLOW_REASON_CODES,
    "managed_execution_required": MANAGED_EXECUTION_REASON_CODES,
    "deny": DENY_REASON_CODES,
    "needs_clarification": NEEDS_CLARIFICATION_REASON_CODES,
    "needs_confirmation": NEEDS_CONFIRMATION_REASON_CODES,
    "needs_approval": NEEDS_APPROVAL_REASON_CODES,
    "pre_evaluation": PRE_EVALUATION_REASON_CODES,
}

#: The seven decision types frozen by the contract.
DECISION_TYPES: frozenset[str] = frozenset(
    {
        "allow",
        "deny",
        "needs_clarification",
        "needs_confirmation",
        "needs_approval",
        "managed_execution_required",
        "pre_evaluation",
    }
)

#: Revocation reasons allowed by ``execution-grant.schema.json``.
GRANT_REVOCATION_REASONS: frozenset[str] = frozenset(
    {
        "policy_changed",
        "task_cancelled",
        "task_terminal",
        "attempt_failed",
        "execution_evidence_lost",
        "identity_disabled",
        "security_incident",
        "manual_revoke",
        "approval_revoked",
        "runtime_compromised",
    }
)


class GovernanceError(Exception):
    """A fail-closed governance denial.

    ``reason_code`` is always one of :data:`REASON_CODES`.  ``context`` is an
    internal mapping used for audit details and tests; it is never rendered into
    the exception string.
    """

    def __init__(
        self,
        reason_code: str,
        *,
        message: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        if reason_code not in REASON_CODES:
            raise ValueError(f"unknown governance reason code: {reason_code!r}")
        self.reason_code = reason_code
        self.context: dict[str, Any] = dict(context or {})
        #: Set by a layer that already wrote the denial audit event, so an
        #: outer layer does not append a duplicate.
        self.audited = False
        super().__init__(message or f"governance denied: {reason_code}")

    def audit_details(self) -> dict[str, Any]:
        """Return non-sensitive structured details suitable for an audit row."""
        return {"reason_code": self.reason_code, **self.context}


class PrincipalResolutionError(GovernanceError):
    """A principal could not be trusted from the verified context."""


class PolicyError(GovernanceError):
    """Policy storage, validation or publication failed closed."""


class DelegationError(GovernanceError):
    """A delegation reference is missing, expired, revoked or too broad."""


class GrantError(GovernanceError):
    """An execution grant is missing, invalid, expanded or no longer usable."""


class ToolCallDeniedError(GovernanceError):
    """The PEP refused a tool call."""


def deny(reason_code: str, *, context: Mapping[str, Any] | None = None) -> NoReturn:
    """Raise :class:`GovernanceError` with a stable reason code."""
    raise GovernanceError(reason_code, context=context)
