"""PEP revalidation and audit behaviour (Issue #78)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.audit.models import AuditEvent
from app.governance.errors import GovernanceError
from app.governance.grants import issue_grant, revoke_grant
from app.governance.pdp import EvaluationRequest, evaluate
from app.governance.pep import enforce_tool_call
from app.governance.principal import (
    principal_ref,
    resolve_employee_principal,
    resolve_service_principal,
)
from app.tests.governance.helpers import (
    PolicyContext,
    approval_ref,
    confirmation_ref,
    make_service_identity,
    publish_policy,
    scope,
)

TASK_ID = "task_expense_0001"


def _principal(session, org):
    return resolve_employee_principal(session, employee_id=org.employee.id)


def _allow(session, org, policy: PolicyContext, **overrides):
    payload = {
        "principal": _principal(session, org),
        "capability": "expense.read@1.0.0",
        "task_id": TASK_ID,
        "task_revision": 1,
        "resource_scope": scope(),
        "policy_set_id": policy.policy_set_id,
    }
    payload.update(overrides)
    return evaluate(session, EvaluationRequest(**payload))


def _grant(session, org, decision, **overrides):
    payload = {
        "principal": _principal(session, org),
        "decision": decision,
        "tool_contract_version": "1.0.0",
    }
    payload.update(overrides)
    return issue_grant(session, **payload)


def _enforce(session, org, grant, **overrides):
    payload = {
        "grant_id": grant.grant_id,
        "principal": _principal(session, org),
        "task_id": TASK_ID,
        "task_revision": 1,
        "capability": "expense.read@1.0.0",
        "call_id": "call_gov_pep_0001",
        "resource_scope": scope(),
        "tool_contract_version": "1.0.0",
    }
    payload.update(overrides)
    return enforce_tool_call(session, **payload)


def _last_event(session, action: str) -> AuditEvent | None:
    return session.scalar(
        select(AuditEvent)
        .where(AuditEvent.action == action)
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )


def test_pep_allows_and_audits(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    result = _enforce(session, org, grant)

    assert result.grant_id if hasattr(result, "grant_id") else result.grant.grant_id
    assert result.capability == "expense.read@1.0.0"
    assert result.decision_id == grant.decision_id
    assert result.policy_version == "1.0.0"
    assert result.remaining_calls == 4

    allowed = _last_event(session, "governance.pep.allowed")
    assert allowed is not None
    assert allowed.actor_id == org.employee.id
    assert allowed.resource_id == grant.grant_id
    assert allowed.details["policy_version"] == "1.0.0"
    assert allowed.details["capability"] == "expense.read@1.0.0"

    consumed = _last_event(session, "governance.grant.consumed")
    assert consumed is not None
    assert consumed.details["call_id"] == "call_gov_pep_0001"


def test_pep_missing_grant_denies_and_audits(session, org) -> None:
    try:
        _enforce(session, org, type("G", (), {"grant_id": "grnt_missing_0001"})())
    except GovernanceError as exc:
        assert exc.reason_code == "grant_missing"
    else:  # pragma: no cover
        raise AssertionError("missing grant must be denied")

    event = _last_event(session, "governance.pep.denied")
    assert event is not None
    assert event.details["reason_code"] == "grant_missing"
    assert event.resource_id == "grnt_missing_0001"


def test_pep_rejects_revoked_expired_and_consumed_grants(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    revoke_grant(
        session,
        grant_id=grant.grant_id,
        reason_code="manual_revoke",
        revoked_by_principal_id=principal_ref("prin", org.employee.id),
    )
    try:
        _enforce(session, org, grant)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_revoked"
    else:  # pragma: no cover
        raise AssertionError("revoked grant must be denied")
    assert _last_event(session, "governance.pep.denied").details["reason_code"] == "grant_revoked"

    short = _grant(session, org, _allow(session, org, policy), ttl_seconds=1)
    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    try:
        _enforce(session, org, short, now=future)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_expired"
    else:  # pragma: no cover
        raise AssertionError("expired grant must be denied")

    fresh = _grant(session, org, _allow(session, org, policy))
    _enforce(session, org, fresh)
    try:
        _enforce(session, org, fresh, call_id="call_gov_pep_replay")
    except GovernanceError as exc:
        assert exc.reason_code == "replay_detected"
    else:  # pragma: no cover
        raise AssertionError("consumed single-use grant must be denied")


def test_pep_rejects_scope_capability_task_and_subject_mismatch(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))

    cases = (
        ({"capability": "expense.submit@1.0.0"}, "capability_not_granted"),
        (
            {"resource_scope": scope("self", resource_ids=("res_expense_9999",))},
            "grant_scope_mismatch",
        ),
        ({"task_revision": 2}, "grant_scope_mismatch"),
    )
    for overrides, expected in cases:
        try:
            _enforce(session, org, grant, **overrides)
        except GovernanceError as exc:
            assert exc.reason_code == expected
        else:  # pragma: no cover
            raise AssertionError(f"expected {expected} for {overrides}")

    other_service = make_service_identity(session, owner=org.finance)
    other_principal = resolve_service_principal(
        session, service_identity_id=other_service.id
    )
    try:
        _enforce(session, org, grant, principal=other_principal)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_scope_mismatch"
    else:  # pragma: no cover
        raise AssertionError("subject mismatch must be denied")


def test_pep_rejects_tool_contract_and_policy_version_changes(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    try:
        _enforce(session, org, grant, tool_contract_version="2.0.0")
    except GovernanceError as exc:
        assert exc.reason_code == "tool_contract_mismatch"
    else:  # pragma: no cover
        raise AssertionError("tool contract mismatch must be denied")

    # Publishing a new version retires the old one; the grant is no longer valid.
    publish_policy(
        session,
        org,
        policy_set_id=policy.policy_set_id,
        version="2.0.0",
    )
    try:
        _enforce(session, org, grant)
    except GovernanceError as exc:
        assert exc.reason_code == "policy_version_invalid"
    else:  # pragma: no cover
        raise AssertionError("policy version change must invalidate the grant")


def test_pep_rejects_payload_hash_mismatch(session, org, policy) -> None:
    principal_id = principal_ref("prin", org.employee.id)
    decision = _allow(
        session,
        org,
        policy,
        capability="expense.submit@1.0.0",
        confirmation_ref=confirmation_ref(principal_id=principal_id),
        approval_ref=approval_ref(),
        payload_hash="sha256:" + "1" * 64,
    )
    assert decision.decision_type == "allow"
    grant = _grant(
        session,
        org,
        decision,
        confirmation_ref=confirmation_ref(
            principal_id=principal_id, payload_hash="sha256:" + "1" * 64
        ),
        approval_ref=approval_ref(payload_hash="sha256:" + "1" * 64),
    )

    try:
        _enforce(
            session,
            org,
            grant,
            capability="expense.submit@1.0.0",
            payload_hash="sha256:" + "2" * 64,
        )
    except GovernanceError as exc:
        assert exc.reason_code == "payload_hash_mismatch"
    else:  # pragma: no cover
        raise AssertionError("payload hash mismatch must be denied")


def test_pep_uses_service_identity_subject(session, org, policy) -> None:
    identity = make_service_identity(session, owner=org.finance, role_names=("finance_lead",))
    principal = resolve_service_principal(session, service_identity_id=identity.id)
    # finance_lead has read company; use a company-scope rule that the service
    # identity can satisfy, while still exercising the PEP subject binding.
    decision = evaluate(
        session,
        EvaluationRequest(
            principal=principal,
            capability="expense.read@1.0.0",
            task_id=TASK_ID,
            task_revision=1,
            resource_scope=scope("self"),
            policy_set_id=policy.policy_set_id,
        ),
    )
    assert decision.decision_type == "deny"  # service lacks own-scope read

    # Use the employee role service identity instead (own scope).
    identity2 = make_service_identity(session, owner=org.finance, role_names=("employee",))
    principal2 = resolve_service_principal(session, service_identity_id=identity2.id)
    decision2 = evaluate(
        session,
        EvaluationRequest(
            principal=principal2,
            capability="expense.read@1.0.0",
            task_id=TASK_ID,
            task_revision=1,
            resource_scope=scope("self"),
            policy_set_id=policy.policy_set_id,
        ),
    )
    assert decision2.decision_type == "allow"
    grant = issue_grant(
        session,
        principal=principal2,
        decision=decision2,
        tool_contract_version="1.0.0",
    )
    result = enforce_tool_call(
        session,
        grant_id=grant.grant_id,
        principal=principal2,
        task_id=TASK_ID,
        task_revision=1,
        capability="expense.read@1.0.0",
        call_id="call_gov_service",
        resource_scope=scope(),
        tool_contract_version="1.0.0",
    )
    assert result.grant.subject_id == principal_ref("svc", identity2.id)
    assert result.grant.subject_type == "service"
