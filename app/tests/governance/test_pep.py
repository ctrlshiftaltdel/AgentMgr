"""PEP revalidation, fail-closed context and audit behaviour (Issue #78)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.governance.errors import GovernanceError
from app.governance.grants import issue_grant, revoke_grant
from app.governance.models import ExecutionGrant
from app.governance.pdp import EvaluationRequest, evaluate
from app.governance.pep import enforce_tool_call
from app.governance.principal import (
    principal_ref,
    resolve_employee_principal,
    resolve_service_principal,
)
from app.identity.models import EmployeeRole
from app.tests.governance.helpers import (
    PolicyContext,
    approval_ref,
    confirmation_ref,
    make_service_identity,
    publish_policy,
    scope,
)

TASK_ID = "task_expense_0001"
TOOL_HASH = "sha256:" + "a" * 64


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
    verifier = payload.pop("evidence_verifier", None)
    return evaluate(session, EvaluationRequest(**payload), evidence_verifier=verifier)


def _grant(session, org, decision, **overrides):
    payload = {
        "principal": _principal(session, org),
        "decision": decision,
        "tool_contract_version": "1.0.0",
        "tool_contract_id": "cat_gov_tools",
        "tool_contract_hash": TOOL_HASH,
    }
    payload.update(overrides)
    return issue_grant(session, **payload)


def _enforce(session, org, grant, **overrides):
    payload = {
        "grant_id": grant.grant_id,
        "principal": _principal(session, org),
        "task_id": TASK_ID,
        "task_revision": 1,
        "task_idempotency_key": grant.task_idempotency_key,
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

    assert result.grant.grant_id == grant.grant_id
    assert result.capability == "expense.read@1.0.0"
    assert result.decision_id == grant.decision_id
    assert result.policy_version == "1.0.0"
    assert result.remaining_calls == 0

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


def test_denial_audit_is_durable_when_the_caller_commits(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    grant_id = grant.grant_id
    session.commit()

    try:
        _enforce(session, org, grant, capability="expense.submit@1.0.0")
    except GovernanceError as exc:
        assert exc.reason_code == "capability_not_granted"
    session.commit()

    event = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.action == "governance.pep.denied",
            AuditEvent.resource_id == grant_id,
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )
    assert event is not None
    assert event.details["reason_code"] == "capability_not_granted"


def test_pep_rejects_revoked_expired_and_consumed_grants(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    revoke_grant(
        session,
        grant_id=grant.grant_id,
        reason_code="manual_revoke",
        principal=_principal(session, org),
    )
    try:
        _enforce(session, org, grant)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_revoked"
    else:  # pragma: no cover
        raise AssertionError("revoked grant must be denied")
    assert _last_event(session, "governance.pep.denied").details["reason_code"] == "grant_revoked"

    short = _grant(session, org, _allow(session, org, policy), ttl_seconds=1)
    future = datetime.now(UTC) + timedelta(seconds=10)
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
        assert exc.reason_code in {"grant_scope_mismatch", "identity_unverified"}
    else:  # pragma: no cover
        raise AssertionError("subject mismatch must be denied")


def test_pep_requires_resource_scope_and_tool_version(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))

    try:
        _enforce(session, org, grant, resource_scope=None)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_scope_mismatch"
    else:  # pragma: no cover
        raise AssertionError("missing resource_scope must fail closed")

    try:
        _enforce(session, org, grant, tool_contract_version=None)
    except GovernanceError as exc:
        assert exc.reason_code == "tool_contract_mismatch"
    else:  # pragma: no cover
        raise AssertionError("missing tool_contract_version must fail closed")


def test_pep_rejects_client_asserted_identity(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    try:
        _enforce(
            session,
            org,
            grant,
            client_payload={"principal_id": "prin_admin_0001", "role_ids": ["role_sysadmin"]},
        )
    except GovernanceError as exc:
        assert exc.reason_code == "identity_unverified"
    else:  # pragma: no cover
        raise AssertionError("client-asserted identity must be rejected")


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
    payload_hash = "sha256:" + "1" * 64
    from app.tests.governance.helpers import FakeEvidenceVerifier

    verifier = FakeEvidenceVerifier(
        role_resolutions={"role_gov_approver": principal_ref("prin", org.manager.id)}
    )
    decision = _allow(
        session,
        org,
        policy,
        capability="expense.submit@1.0.0",
        confirmation_ref=confirmation_ref(
            principal_id=principal_id, payload_hash=payload_hash
        ),
        approval_ref=approval_ref(
            approver_principal_id="role_gov_approver", payload_hash=payload_hash
        ),
        payload_hash=payload_hash,
        evidence_verifier=verifier,
    )
    assert decision.decision_type == "allow"
    grant = _grant(session, org, decision)

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

    # Gated grants require the payload hash; omission is not a skip.
    try:
        _enforce(session, org, grant, capability="expense.submit@1.0.0", payload_hash=None)
    except GovernanceError as exc:
        assert exc.reason_code == "payload_hash_mismatch"
    else:  # pragma: no cover
        raise AssertionError("missing payload hash must fail closed")


def test_gated_grant_requires_a_trusted_verifier_at_the_pep(session, org, policy) -> None:
    principal_id = principal_ref("prin", org.employee.id)
    payload_hash = "sha256:" + "1" * 64
    from app.tests.governance.helpers import FakeEvidenceVerifier

    verifier = FakeEvidenceVerifier(
        role_resolutions={"role_gov_approver": principal_ref("prin", org.manager.id)}
    )
    decision = _allow(
        session,
        org,
        policy,
        capability="expense.submit@1.0.0",
        confirmation_ref=confirmation_ref(
            principal_id=principal_id, payload_hash=payload_hash
        ),
        approval_ref=approval_ref(
            approver_principal_id="role_gov_approver", payload_hash=payload_hash
        ),
        payload_hash=payload_hash,
        evidence_verifier=verifier,
    )
    grant = _grant(session, org, decision)

    # Default deny verifier: the stored reference alone is not evidence.
    try:
        _enforce(
            session,
            org,
            grant,
            capability="expense.submit@1.0.0",
            payload_hash=payload_hash,
        )
    except GovernanceError as exc:
        assert exc.reason_code in {"confirmation_missing", "approval_missing"}
    else:  # pragma: no cover
        raise AssertionError("caller-asserted gate evidence must not satisfy the PEP")

    result = _enforce(
        session,
        org,
        grant,
        capability="expense.submit@1.0.0",
        payload_hash=payload_hash,
        evidence_verifier=verifier,
    )
    assert result.grant.status == "consumed"
    assert result.grant.confirmation_ref["status"] == "consumed"


def test_pep_rejects_consumed_confirmation(session, org, policy) -> None:
    principal_id = principal_ref("prin", org.employee.id)
    payload_hash = "sha256:" + "1" * 64
    from app.tests.governance.helpers import FakeEvidenceVerifier

    verifier = FakeEvidenceVerifier()
    decision = _allow(
        session,
        org,
        policy,
        capability="expense.submit@1.0.0",
        confirmation_ref=confirmation_ref(
            principal_id=principal_id, payload_hash=payload_hash
        ),
        approval_ref=approval_ref(payload_hash=payload_hash),
        payload_hash=payload_hash,
        evidence_verifier=verifier,
    )
    grant = _grant(session, org, decision)
    # Simulate the confirmation having been consumed elsewhere after issuance.
    consumed = dict(grant.confirmation_ref)
    consumed["status"] = "consumed"
    consumed["consumed_at"] = "2026-01-01T00:00:00Z"
    consumed["consumed_by_call_id"] = "call_other_0001"
    grant.confirmation_ref = consumed
    session.flush()

    try:
        _enforce(
            session,
            org,
            grant,
            capability="expense.submit@1.0.0",
            payload_hash=payload_hash,
            evidence_verifier=verifier,
        )
    except GovernanceError as exc:
        assert exc.reason_code == "replay_detected"
    else:  # pragma: no cover
        raise AssertionError("consumed confirmation must fail closed")


def test_pep_uses_service_identity_subject(session, org, policy) -> None:
    identity = make_service_identity(session, owner=org.finance, role_names=("finance_lead",))
    principal = resolve_service_principal(session, service_identity_id=identity.id)
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
    grant = _grant(session, org, decision2, principal=principal2)
    result = _enforce(
        session,
        org,
        grant,
        principal=principal2,
        call_id="call_gov_service",
    )
    assert result.grant.subject_id == principal_ref("svc", identity2.id)
    assert result.grant.subject_type == "service"


# ---------------------------------------------------------------------------
# I4: subject freshness and current RBAC authority
# ---------------------------------------------------------------------------


def test_pep_rejects_disabled_subject_after_issuance(session, org, policy) -> None:
    principal = _principal(session, org)
    grant = _grant(session, org, _allow(session, org, policy))
    org.employee.status = "inactive"
    session.flush()

    try:
        enforce_tool_call(
            session,
            grant_id=grant.grant_id,
            principal=principal,
            task_id=TASK_ID,
            task_revision=1,
            task_idempotency_key=grant.task_idempotency_key,
            capability="expense.read@1.0.0",
            call_id="call_gov_disabled",
            resource_scope=scope(),
            tool_contract_version="1.0.0",
        )
    except GovernanceError as exc:
        assert exc.reason_code == "principal_disabled"
    else:  # pragma: no cover
        raise AssertionError("a disabled subject must invalidate the grant")


def test_pep_rejects_removed_rbac_permission(session, org, policy) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    session.execute(
        delete(EmployeeRole).where(EmployeeRole.employee_id == org.employee.id)
    )
    session.flush()

    try:
        _enforce(session, org, grant)
    except GovernanceError as exc:
        assert exc.reason_code == "capability_not_granted"
    else:  # pragma: no cover
        raise AssertionError("removed RBAC permission must invalidate the grant")


# ---------------------------------------------------------------------------
# C4: stale identity-map reads cannot hide a revocation/consumption
# ---------------------------------------------------------------------------


def test_pep_sees_revocation_committed_by_another_session(session, org, policy, engine) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    employee_id = org.employee.id
    grant_id = grant.grant_id
    session.commit()

    # Session A preloads the grant into its identity map.
    preloaded = session.scalar(
        select(ExecutionGrant).where(ExecutionGrant.grant_id == grant_id)
    )
    assert preloaded is not None and preloaded.status == "active"

    with Session(engine) as other:
        other_grant = other.scalar(
            select(ExecutionGrant).where(ExecutionGrant.grant_id == grant_id)
        )
        revoke_grant(
            other,
            grant_id=other_grant.grant_id,
            reason_code="manual_revoke",
            principal=resolve_employee_principal(other, employee_id=employee_id),
        )
        other.commit()

    try:
        _enforce(session, org, preloaded)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_revoked"
    else:  # pragma: no cover
        raise AssertionError("a preloaded active object must not hide a revocation")


def test_pep_sees_consumption_committed_by_another_session(session, org, policy, engine) -> None:
    grant = _grant(session, org, _allow(session, org, policy))
    grant_id = grant.grant_id
    task_key = grant.task_idempotency_key
    session.commit()

    preloaded = session.scalar(
        select(ExecutionGrant).where(ExecutionGrant.grant_id == grant_id)
    )
    assert preloaded is not None and preloaded.status == "active"

    with Session(engine) as other:
        other_principal = resolve_employee_principal(
            other, employee_id=org.employee.id
        )
        enforce_tool_call(
            other,
            grant_id=grant_id,
            principal=other_principal,
            task_id=TASK_ID,
            task_revision=1,
            task_idempotency_key=task_key,
            capability="expense.read@1.0.0",
            call_id="call_gov_other",
            resource_scope=scope(),
            tool_contract_version="1.0.0",
        )
        other.commit()

    try:
        _enforce(session, org, preloaded, call_id="call_gov_after")
    except GovernanceError as exc:
        assert exc.reason_code == "replay_detected"
    else:  # pragma: no cover
        raise AssertionError("a preloaded active object must not hide consumption")
