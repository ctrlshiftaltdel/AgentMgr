"""Deterministic PDP behaviour (Issue #78).

Covers all decision types, fail-closed policy handling, priority non-escalation,
resource-scope containment, RBAC reuse, gates and audit/decision persistence.
"""

from __future__ import annotations

from sqlalchemy import select

from app.audit.models import AuditEvent
from app.governance.models import GovernanceDecisionRecord
from app.governance.pdp import EvaluationRequest, evaluate
from app.governance.policies import (
    canonical_json,
    create_draft,
    sha256_digest,
)
from app.governance.principal import principal_ref, resolve_employee_principal
from app.tests.governance.helpers import (
    PolicyContext,
    approval_ref,
    confirmation_ref,
    scope,
    unique_policy_set,
)

TASK_ID = "task_expense_0001"
TASK_REVISION = 1


def _request(session, org, policy: PolicyContext, **overrides):
    principal = resolve_employee_principal(session, employee_id=org.employee.id)
    payload = {
        "principal": principal,
        "capability": "expense.read@1.0.0",
        "task_id": TASK_ID,
        "task_revision": TASK_REVISION,
        "resource_scope": scope(),
        "policy_set_id": policy.policy_set_id,
    }
    payload.update(overrides)
    return EvaluationRequest(**payload)


def test_allow_persists_decision_and_audit(session, org, policy: PolicyContext) -> None:
    request = _request(session, org, policy)
    decision = evaluate(session, request)

    assert decision.decision_type == "allow"
    assert decision.reason_codes == ("policy_allowed",)
    assert decision.is_authorizing
    assert decision.policy_version["policy_set_id"] == policy.policy_set_id
    assert decision.policy_version["version"] == "1.0.0"
    assert decision.constraints["execution_mode"] == "advisory"
    assert decision.constraints["allowed_capabilities"] == ["expense.read@1.0.0"]

    # The hash is the canonical hash of the document without decision_hash.
    document = decision.to_document()
    stored_hash = document.pop("decision_hash")
    assert sha256_digest(canonical_json(document).encode("utf-8")) == stored_hash

    record = session.scalar(
        select(GovernanceDecisionRecord).where(
            GovernanceDecisionRecord.decision_id == decision.decision_id
        )
    )
    assert record is not None
    assert record.decision_type == "allow"
    assert record.policy_version == "1.0.0"

    event = session.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_type == "governance_decision",
            AuditEvent.resource_id == decision.decision_id,
        )
    )
    assert event is not None
    assert event.action == "governance.allowed"
    assert event.actor_id == org.employee.id
    assert event.details["reason_codes"] == ["policy_allowed"]
    assert event.details["policy_version"] == "1.0.0"


def test_deny_unknown_capability_and_malformed_capability(session, org, policy) -> None:
    unknown = evaluate(
        session,
        _request(session, org, policy, capability="unknown.capability@1.0.0"),
    )
    assert unknown.decision_type == "deny"
    assert "capability_not_granted" in unknown.reason_codes

    malformed = evaluate(
        session, _request(session, org, policy, capability="shell.exec")
    )
    assert malformed.decision_type == "needs_clarification"
    assert "capability_ambiguous" in malformed.reason_codes


def test_hard_deny_cannot_be_overridden_by_priority(session, org, policy) -> None:
    principal = resolve_employee_principal(session, employee_id=org.finance.id)
    base = {
        "principal": principal,
        "capability": "payment.execute@1.0.0",
        "task_id": TASK_ID,
        "task_revision": TASK_REVISION,
        "resource_scope": scope(
            "company", resource_ids=("res_payment_0001",), classifications=("restricted",)
        ),
        "policy_set_id": policy.policy_set_id,
    }

    normal = evaluate(session, EvaluationRequest(**base, priority="normal"))
    urgent = evaluate(session, EvaluationRequest(**base, priority="urgent"))

    assert normal.decision_type == "deny"
    assert normal.reason_codes == ("policy_denied",)
    assert urgent.decision_type == "deny"
    assert "policy_denied" in urgent.reason_codes
    assert "priority_cannot_override_deny" in urgent.reason_codes
    assert urgent.constraints["execution_mode"] == "none"


def test_missing_policy_fails_closed(session, org) -> None:
    nonexistent = unique_policy_set()
    request = _request(
        session,
        org,
        PolicyContext(policy_set_id=nonexistent, row=None),  # type: ignore[arg-type]
    )
    decision = evaluate(session, request)
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("policy_unavailable",)

    requested_version = evaluate(
        session, _request(session, org, PolicyContext(nonexistent, None), policy_version="9.9.9")  # type: ignore[arg-type]
    )
    assert requested_version.decision_type == "deny"
    assert requested_version.reason_codes == ("policy_version_invalid",)


def test_unpublished_draft_is_not_usable(session, org) -> None:
    policy_set_id = unique_policy_set()
    draft = create_draft(
        session,
        document={
            "policy_set_id": policy_set_id,
            "version": "1.0.0",
            "default_effect": "deny",
            "hard_denies": [],
            "rules": [],
        },
        created_by_employee_id=org.finance.id,
    )
    assert draft.status == "draft"
    decision = evaluate(
        session,
        _request(session, org, PolicyContext(policy_set_id, draft)),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("policy_unavailable",)


def test_resource_scope_escape_and_ambiguity(session, org, policy) -> None:
    wrong_type = evaluate(
        session,
        _request(session, org, policy, resource_scope=scope("company")),
    )
    assert wrong_type.decision_type == "deny"
    assert "resource_scope_denied" in wrong_type.reason_codes

    no_members = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            resource_scope=scope("self", resource_ids=()),
        ),
    )
    assert no_members.decision_type == "needs_clarification"
    assert "resource_scope_ambiguous" in no_members.reason_codes

    unknown_type = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            resource_scope=scope("galaxy"),
        ),
    )
    assert unknown_type.decision_type == "deny"
    assert "resource_scope_denied" in unknown_type.reason_codes


def test_undetermined_scope_needs_clarification(session, org, policy) -> None:
    decision = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            resource_scope={
                "scope_type": "undetermined",
                "resource_ids": [],
                "connector_ids": [],
                "data_classifications": [],
                "exclude_resource_ids": [],
            },
        ),
    )
    assert decision.decision_type == "needs_clarification"
    assert decision.reason_codes == ("resource_scope_ambiguous",)
    assert not decision.is_authorizing


def test_confirmation_and_approval_gates(session, org, policy) -> None:
    base = _request(
        session, org, policy, capability="expense.submit@1.0.0"
    )
    needs_confirmation = evaluate(session, base)
    assert needs_confirmation.decision_type == "needs_confirmation"
    assert needs_confirmation.reason_codes == ("confirmation_required",)

    # Priority cannot skip a gate.
    urgent = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            capability="expense.submit@1.0.0",
            priority="urgent",
        ),
    )
    assert urgent.decision_type == "needs_confirmation"

    principal_id = principal_ref("prin", org.employee.id)
    needs_approval = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            capability="expense.submit@1.0.0",
            confirmation_ref=confirmation_ref(principal_id=principal_id),
        ),
    )
    assert needs_approval.decision_type == "needs_approval"
    assert needs_approval.reason_codes == ("approval_required",)

    allowed = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            capability="expense.submit@1.0.0",
            confirmation_ref=confirmation_ref(principal_id=principal_id),
            approval_ref=approval_ref(),
        ),
    )
    assert allowed.decision_type == "allow"
    assert "confirmation_satisfied" in allowed.reason_codes
    assert "approval_satisfied" in allowed.reason_codes


def test_gate_reference_validation_fails_closed(session, org, policy) -> None:
    principal_id = principal_ref("prin", org.employee.id)
    consumed = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            capability="expense.submit@1.0.0",
            confirmation_ref=confirmation_ref(
                principal_id=principal_id,
                consumed_at="2026-01-01T00:00:00Z",
            ),
            approval_ref=approval_ref(),
        ),
    )
    assert consumed.decision_type == "deny"
    assert "replay_detected" in consumed.reason_codes

    self_approved = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            capability="expense.submit@1.0.0",
            confirmation_ref=confirmation_ref(principal_id=principal_id),
            approval_ref=approval_ref(approver_principal_id=principal_id),
        ),
    )
    assert self_approved.decision_type == "deny"
    assert "self_approval_forbidden" in self_approved.reason_codes


def test_authorizing_decision_requires_task_binding(session, org, policy) -> None:
    before_task = evaluate(
        session,
        _request(session, org, policy, task_id=None, task_revision=None),
    )
    assert before_task.decision_type == "pre_evaluation"
    assert before_task.reason_codes == ("task_not_created",)
    assert not before_task.is_authorizing
    assert before_task.task_ref["task_id"] is None
    assert before_task.task_ref["task_revision"] is None

    missing_revision = evaluate(
        session,
        _request(session, org, policy, task_revision=None),
    )
    assert missing_revision.decision_type == "needs_clarification"
    assert missing_revision.reason_codes == ("required_input_missing",)

    pre = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            task_id=None,
            task_revision=None,
            pre_evaluation=True,
        ),
    )
    assert pre.decision_type == "pre_evaluation"
    assert pre.reason_codes == ("pre_evaluation_only",)
    assert not pre.is_authorizing


def test_risk_ceiling_and_execution_mode(session, org, policy) -> None:
    risky = evaluate(
        session,
        _request(session, org, policy, capability="risk.high@1.0.0", risk="high"),
    )
    assert risky.decision_type == "deny"
    assert risky.reason_codes == ("risk_above_ceiling",)

    bad_mode = evaluate(
        session,
        _request(session, org, policy, execution_mode="magic"),
    )
    assert bad_mode.decision_type == "deny"
    assert bad_mode.reason_codes == ("unsupported_execution_mode",)


def test_managed_execution_required(session, org, policy) -> None:
    decision = evaluate(
        session,
        _request(session, org, policy, capability="managed.task@1.0.0"),
    )
    assert decision.decision_type == "managed_execution_required"
    assert decision.is_authorizing
    assert decision.constraints["execution_mode"] == "managed_execution"
    assert "managed_execution_required" in decision.reason_codes


def test_rule_requires_existing_rbac_permission(session, org, policy) -> None:
    decision = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            capability="inventory.read@1.0.0",
            resource_scope=scope("company", resource_ids=("res_stock_0001",)),
        ),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("capability_not_granted",)


def test_request_hash_and_decision_revision_are_stable(session, org, policy) -> None:
    first = evaluate(session, _request(session, org, policy))
    second = evaluate(session, _request(session, org, policy))
    assert first.decision_revision == 1
    assert second.decision_revision == 2
    assert first.decision_hash != second.decision_hash

    records = list(
        session.scalars(
            select(GovernanceDecisionRecord)
            .where(GovernanceDecisionRecord.task_id == TASK_ID)
            .order_by(GovernanceDecisionRecord.decision_revision)
        )
    )
    assert [record.decision_revision for record in records] == [1, 2]
