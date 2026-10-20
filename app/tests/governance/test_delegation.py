"""Delegation containment (Issue #78).

A delegation must be active, in time, for the right delegatee, within its
capability and resource scope, and never wider than the delegator's own
permissions or the policy rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.governance.pdp import EvaluationRequest, evaluate
from app.governance.principal import resolve_employee_principal
from app.tests.governance.helpers import (
    PolicyContext,
    make_delegation,
    scope,
)

TASK_ID = "task_expense_0001"


def _request(session, org, policy: PolicyContext, **overrides):
    principal = resolve_employee_principal(session, employee_id=org.employee.id)
    payload = {
        "principal": principal,
        "capability": "delegated.read@1.0.0",
        "task_id": TASK_ID,
        "task_revision": 1,
        "resource_scope": scope(),
        "policy_set_id": policy.policy_set_id,
    }
    payload.update(overrides)
    return EvaluationRequest(**payload)


def test_active_delegation_within_scope_is_allowed(session, org, policy) -> None:
    delegation = make_delegation(
        session,
        delegator=org.manager,
        delegatee=org.employee,
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
    )
    decision = evaluate(
        session,
        _request(session, org, policy, delegation_ref=delegation.delegation_id),
    )
    assert decision.decision_type == "allow"
    assert "delegated_authority_valid" in decision.reason_codes


def test_expired_delegation_is_denied(session, org, policy) -> None:
    now = datetime.now(timezone.utc)
    delegation = make_delegation(
        session,
        delegator=org.manager,
        delegatee=org.employee,
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
        starts_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    decision = evaluate(
        session,
        _request(session, org, policy, delegation_ref=delegation.delegation_id),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("delegation_expired",)


def test_revoked_delegation_is_denied(session, org, policy) -> None:
    delegation = make_delegation(
        session,
        delegator=org.manager,
        delegatee=org.employee,
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
        status="revoked",
    )
    decision = evaluate(
        session,
        _request(session, org, policy, delegation_ref=delegation.delegation_id),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("delegation_expired",)


def test_delegation_capability_cannot_expand(session, org, policy) -> None:
    delegation = make_delegation(
        session,
        delegator=org.manager,
        delegatee=org.employee,
        capabilities=("some.other@1.0.0",),
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
    )
    decision = evaluate(
        session,
        _request(session, org, policy, delegation_ref=delegation.delegation_id),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("capability_not_granted",)


def test_delegation_scope_cannot_expand(session, org, policy) -> None:
    delegation = make_delegation(
        session,
        delegator=org.manager,
        delegatee=org.employee,
        resource_scope=scope("self", resource_ids=("res_expense_9999",)),
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
    )
    decision = evaluate(
        session,
        _request(session, org, policy, delegation_ref=delegation.delegation_id),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("resource_scope_denied",)


def test_wrong_delegatee_is_denied(session, org, policy) -> None:
    delegation = make_delegation(
        session,
        delegator=org.manager,
        delegatee=org.finance,
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
    )
    decision = evaluate(
        session,
        _request(session, org, policy, delegation_ref=delegation.delegation_id),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("delegation_expired",)


def test_rule_must_allow_delegation(session, org, policy) -> None:
    delegation = make_delegation(
        session,
        delegator=org.manager,
        delegatee=org.employee,
        capabilities=("risk.high@1.0.0",),
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
    )
    decision = evaluate(
        session,
        _request(
            session,
            org,
            policy,
            capability="risk.high@1.0.0",
            risk="low",
            delegation_ref=delegation.delegation_id,
        ),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("capability_not_granted",)


def test_delegator_cannot_delegate_permissions_it_lacks(session, org, policy) -> None:
    delegation = make_delegation(
        session,
        delegator=org.auditor,
        delegatee=org.employee,
        policy_set_id=policy.policy_set_id,
        policy_version=policy.version,
    )
    decision = evaluate(
        session,
        _request(session, org, policy, delegation_ref=delegation.delegation_id),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("capability_not_granted",)
