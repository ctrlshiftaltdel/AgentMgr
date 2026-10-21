"""Execution Grant issuance, forgery refusal and atomic consumption."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.governance.errors import GovernanceError
from app.governance.grants import (
    get_grant,
    grant_document,
    grant_remaining_calls,
    issue_grant,
    revoke_grant,
)
from app.governance.models import ExecutionGrant, GovernanceDecisionRecord
from app.governance.pdp import EvaluationRequest, evaluate
from app.governance.pep import enforce_tool_call
from app.governance.principal import principal_ref, resolve_employee_principal
from app.tests.governance.helpers import (
    PolicyContext,
    FakeEvidenceVerifier,
    approval_ref,
    confirmation_ref,
    scope,
)

TASK_ID = "task_expense_0001"
TOOL_HASH = "sha256:" + "a" * 64


def _principal(session, org):
    return resolve_employee_principal(session, employee_id=org.employee.id)


def _allow(session, org, policy: PolicyContext, *, now=None, **overrides):
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
    return evaluate(
        session, EvaluationRequest(**payload), now=now, evidence_verifier=verifier
    )


def _issue(session, org, decision, **overrides):
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
        "call_id": "call_gov_0001",
        "resource_scope": scope(),
        "tool_contract_version": "1.0.0",
    }
    payload.update(overrides)
    return enforce_tool_call(session, **payload)


def test_issue_grant_binds_decision_task_policy_and_scope(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision)

    assert grant.decision_id == decision.decision_id
    assert grant.decision_hash == decision.decision_hash
    assert grant.decision_revision == decision.decision_revision
    assert grant.task_id == TASK_ID
    assert grant.task_revision == 1
    assert grant.task_idempotency_key == decision.task_ref["idempotency_key"]
    assert grant.subject_id == principal_ref("prin", org.employee.id)
    assert grant.subject_type == "person"
    assert grant.execution_mode == "advisory"
    assert grant.capabilities == ["expense.read@1.0.0"]
    assert grant.resource_scope["resource_ids"] == ["res_expense_0001"]
    assert grant.policy_set_id == policy.policy_set_id
    assert grant.policy_version == "1.0.0"
    assert grant.policy_content_hash == policy.content_hash
    assert grant.status == "active"
    assert grant.usable is True
    assert grant.expires_at > grant.issued_at
    # I6: a single-use grant is exactly one call, never the decision's 5.
    assert grant.budget["max_calls"] == 1
    assert grant_remaining_calls(grant) == 1


def test_issue_only_from_authorizing_decision(session, org, policy) -> None:
    denied = _allow(session, org, policy, capability="unknown.capability@1.0.0")
    assert denied.decision_type == "deny"
    try:
        _issue(session, org, denied)
    except GovernanceError as exc:
        assert exc.reason_code == "policy_denied"
    else:  # pragma: no cover - defensive
        raise AssertionError("a deny decision must not issue a grant")


def test_grant_rejects_client_asserted_identity(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    try:
        _issue(
            session,
            org,
            decision,
            client_payload={"principal_id": "prin_admin_0001"},
        )
    except GovernanceError as exc:
        assert exc.reason_code == "identity_unverified"
    else:  # pragma: no cover
        raise AssertionError("client-asserted identity must be rejected")


def test_grant_refuses_capability_expansion(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    try:
        _issue(session, org, decision, capabilities=["expense.approve@1.0.0"])
    except GovernanceError as exc:
        assert exc.reason_code == "capability_not_granted"
    else:  # pragma: no cover
        raise AssertionError("capability expansion must be refused")


def test_grant_refuses_scope_expansion(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    try:
        _issue(
            session,
            org,
            decision,
            resource_scope=scope(
                "self", resource_ids=("res_expense_0001", "res_expense_9999")
            ),
        )
    except GovernanceError as exc:
        assert exc.reason_code == "grant_scope_mismatch"
    else:  # pragma: no cover
        raise AssertionError("scope expansion must be refused")


def test_grant_refuses_ttl_budget_and_mode_expansion(session, org, policy) -> None:
    decision = _allow(session, org, policy)

    for overrides, expected in (
        ({"ttl_seconds": 1000}, "grant_outlives_decision"),
        ({"budget": {"max_calls": 100}}, "budget_exceeded"),
        ({"execution_mode": "managed_execution"}, "execution_mode_mismatch"),
        ({"task_id": "task_expense_9999"}, "grant_scope_mismatch"),
    ):
        try:
            _issue(session, org, decision, **overrides)
        except GovernanceError as exc:
            assert exc.reason_code == expected
        else:  # pragma: no cover
            raise AssertionError(f"expected {expected} for {overrides}")


def test_grant_from_expired_decision_is_refused(session, org, policy) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    decision = _allow(session, org, policy, now=past)
    try:
        _issue(session, org, decision)
    except GovernanceError as exc:
        assert exc.reason_code == "decision_expired"
    else:  # pragma: no cover
        raise AssertionError("expired decisions must not issue grants")


def test_get_grant_and_idempotent_issue(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision, idempotency_key="idem_gov_same_0001")

    fetched = get_grant(session, grant.grant_id)
    assert fetched is not None
    assert fetched.grant_id == grant.grant_id

    again = _issue(session, org, decision, idempotency_key="idem_gov_same_0001")
    assert again.grant_id == grant.grant_id

    try:
        _issue(
            session,
            org,
            decision,
            idempotency_key="idem_gov_same_0001",
            tool_contract_version="2.0.0",
        )
    except GovernanceError as exc:
        assert exc.reason_code == "replay_detected"
    else:  # pragma: no cover
        raise AssertionError("idempotency key reuse with a different request must fail")


def test_single_use_consumption_and_replay(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision)

    consumed = _enforce(session, org, grant).grant
    assert consumed.status == "consumed"
    assert consumed.usable is False
    assert consumed.consumed_by_call_id == "call_gov_0001"
    assert consumed.usage["calls_consumed"] == 1
    assert grant_remaining_calls(consumed) == 0

    try:
        _enforce(session, org, grant, call_id="call_gov_0002")
    except GovernanceError as exc:
        assert exc.reason_code == "replay_detected"
    else:  # pragma: no cover
        raise AssertionError("single-use grant must reject replay")


def test_multi_use_budget_is_enforced(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(
        session,
        org,
        decision,
        consumption_mode="multi_use",
        budget={"max_calls": 2},
    )

    first = _enforce(session, org, grant, call_id="call_gov_m1").grant
    assert first.status == "active"
    assert first.usage["calls_consumed"] == 1
    assert grant_remaining_calls(first) == 1

    second = _enforce(session, org, grant, call_id="call_gov_m2").grant
    assert second.status == "consumed"
    assert second.usage["calls_consumed"] == 2
    assert grant_remaining_calls(second) == 0

    try:
        _enforce(session, org, grant, call_id="call_gov_m3")
    except GovernanceError as exc:
        assert exc.reason_code == "grant_consumed"
    else:  # pragma: no cover
        raise AssertionError("exhausted multi-use grant must reject further calls")


def test_revocation_requires_subject_or_admin(session, org, policy) -> None:
    grant = _issue(session, org, _allow(session, org, policy))
    manager = resolve_employee_principal(session, employee_id=org.manager.id)
    try:
        revoke_grant(
            session,
            grant_id=grant.grant_id,
            reason_code="manual_revoke",
            principal=manager,
        )
    except GovernanceError as exc:
        assert exc.reason_code == "capability_not_granted"
    else:  # pragma: no cover
        raise AssertionError("an unrelated principal must not revoke a grant")

    admin = resolve_employee_principal(session, employee_id=org.admin.id)
    revoked = revoke_grant(
        session,
        grant_id=grant.grant_id,
        reason_code="manual_revoke",
        principal=admin,
    )
    assert revoked.status == "revoked"
    assert revoked.revoked_by_principal_id == admin.principal_id


def test_terminal_states_are_not_relabelled_expired(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    principal = _principal(session, org)

    revoked = _issue(session, org, decision)
    revoke_grant(
        session,
        grant_id=revoked.grant_id,
        reason_code="manual_revoke",
        principal=principal,
    )
    future = datetime.now(UTC) + timedelta(hours=2)
    try:
        _enforce(session, org, revoked, now=future)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_revoked"
    else:  # pragma: no cover
        raise AssertionError("revoked grant must reject calls")
    refreshed = get_grant(session, revoked.grant_id)
    assert refreshed.status == "revoked"
    assert refreshed.revocation_reason == "manual_revoke"
    assert refreshed.revoked_at is not None

    consumed = _issue(session, org, decision)
    _enforce(session, org, consumed, call_id="call_gov_terminal")
    try:
        _enforce(session, org, consumed, call_id="call_gov_terminal_2", now=future)
    except GovernanceError as exc:
        assert exc.reason_code == "replay_detected"
    else:  # pragma: no cover
        raise AssertionError("consumed grant must not be relabelled expired")
    assert get_grant(session, consumed.grant_id).status == "consumed"


def test_revoked_and_expired_grants_are_rejected(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    principal = _principal(session, org)

    revoked_grant = _issue(session, org, decision)
    revoke_grant(
        session,
        grant_id=revoked_grant.grant_id,
        reason_code="manual_revoke",
        principal=principal,
    )
    try:
        _enforce(session, org, revoked_grant, call_id="call_gov_revoked")
    except GovernanceError as exc:
        assert exc.reason_code == "grant_revoked"
    else:  # pragma: no cover
        raise AssertionError("revoked grant must reject calls")

    short = _issue(session, org, decision, ttl_seconds=1)
    future = datetime.now(UTC) + timedelta(seconds=10)
    try:
        _enforce(session, org, short, call_id="call_gov_expired", now=future)
    except GovernanceError as exc:
        assert exc.reason_code == "grant_expired"
    else:  # pragma: no cover
        raise AssertionError("expired grant must reject calls")
    assert get_grant(session, short.grant_id).status == "expired"


def test_grant_rejects_subject_task_and_scope_mismatch(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision)

    for overrides, expected in (
        ({"task_revision": 2}, "grant_scope_mismatch"),
        ({"capability": "expense.submit@1.0.0"}, "capability_not_granted"),
        (
            {
                "resource_scope": scope(
                    "self", resource_ids=("res_expense_9999",)
                )
            },
            "grant_scope_mismatch",
        ),
    ):
        try:
            _enforce(session, org, grant, **overrides)
        except GovernanceError as exc:
            assert exc.reason_code == expected
        else:  # pragma: no cover
            raise AssertionError(f"expected {expected} for {overrides}")
        # The refusal does not consume or mutate the grant.
        refreshed = get_grant(session, grant.grant_id)
        assert refreshed is not None
        assert refreshed.status == "active"


def test_concurrent_single_use_consumption_is_safe(session, org, policy, engine) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision)
    employee_id = org.employee.id
    task_key = grant.task_idempotency_key
    session.commit()
    grant_id = grant.grant_id

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        with Session(engine) as worker_session:
            barrier.wait(timeout=10)
            try:
                enforce_tool_call(
                    worker_session,
                    grant_id=grant_id,
                    principal=resolve_employee_principal(
                        worker_session, employee_id=employee_id
                    ),
                    task_id=TASK_ID,
                    task_revision=1,
                    task_idempotency_key=task_key,
                    capability="expense.read@1.0.0",
                    call_id="call_gov_concurrent",
                    resource_scope=scope(),
                    tool_contract_version="1.0.0",
                )
                worker_session.commit()
                results.append("ok")
            except GovernanceError as exc:
                worker_session.rollback()
                results.append(exc.reason_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(results) == ["ok", "replay_detected"]


# ---------------------------------------------------------------------------
# Critical C1: a forged in-memory decision cannot expand a real decision
# ---------------------------------------------------------------------------


def test_transient_decision_record_is_rejected(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    persisted = session.query(GovernanceDecisionRecord).filter_by(
        decision_id=decision.decision_id
    ).one()
    forged = GovernanceDecisionRecord(
        decision_id=persisted.decision_id,
        decision_revision=persisted.decision_revision,
        decision_hash=persisted.decision_hash,
        decision_purpose=persisted.decision_purpose,
        correlation_id=persisted.correlation_id,
        task_id=persisted.task_id,
        task_revision=persisted.task_revision,
        principal_type=persisted.principal_type,
        principal_id=persisted.principal_id,
        decision_type=persisted.decision_type,
        reason_codes=list(persisted.reason_codes),
        policy_set_id=persisted.policy_set_id,
        policy_version=persisted.policy_version,
        policy_content_hash=persisted.policy_content_hash,
        constraints={
            **persisted.constraints,
            "allowed_capabilities": ["expense.approve@1.0.0"],
            "ttl_seconds": 86400,
        },
        document=persisted.document,
        request_hash=persisted.request_hash,
        evaluated_at=persisted.evaluated_at,
        expires_at=persisted.expires_at,
    )
    try:
        _issue(session, org, forged, capabilities=["expense.approve@1.0.0"])
    except GovernanceError as exc:
        assert exc.reason_code == "grant_missing"
    else:  # pragma: no cover
        raise AssertionError("a transient decision record must never issue a grant")


def test_corrupted_decision_document_is_rejected(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    persisted = session.query(GovernanceDecisionRecord).filter_by(
        decision_id=decision.decision_id
    ).one()
    persisted.document = {
        **persisted.document,
        "constraints": {
            **persisted.constraints,
            "allowed_capabilities": ["expense.approve@1.0.0"],
        },
    }
    session.flush()

    try:
        _issue(session, org, persisted)
    except GovernanceError as exc:
        assert exc.reason_code == "policy_version_invalid"
    else:  # pragma: no cover
        raise AssertionError("a decision document with a broken hash must be rejected")


def test_in_memory_mutation_cannot_expand_persisted_decision(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    persisted = session.query(GovernanceDecisionRecord).filter_by(
        decision_id=decision.decision_id
    ).one()
    persisted.constraints = {
        **persisted.constraints,
        "allowed_capabilities": ["expense.approve@1.0.0"],
        "resource_scope": scope("company"),
        "ttl_seconds": 86400,
        "max_calls": 100,
    }
    persisted.document = {
        **persisted.document,
        "constraints": persisted.constraints,
    }

    # Issuing for the forged capability must fail: the persisted decision is
    # re-read, and it never allowed it.
    try:
        _issue(session, org, persisted, capabilities=["expense.approve@1.0.0"])
    except GovernanceError as exc:
        assert exc.reason_code in {"capability_not_granted", "policy_version_invalid"}
    else:  # pragma: no cover
        raise AssertionError("forged in-memory constraints must not expand a grant")

    # No grant was issued from the forged object.
    assert (
        session.scalar(
            select(func.count()).select_from(ExecutionGrant)
        )
        == 0
    )


# ---------------------------------------------------------------------------
# I1/I2: revision freshness and retry chain
# ---------------------------------------------------------------------------


def test_stale_decision_cannot_issue_grant(session, org, policy) -> None:
    first = _allow(session, org, policy)
    assert first.decision_revision == 1
    second = _allow(session, org, policy)
    assert second.decision_revision == 2

    try:
        _issue(session, org, first)
    except GovernanceError as exc:
        assert exc.reason_code == "decision_revision_stale"
    else:  # pragma: no cover
        raise AssertionError("a superseded decision revision must not issue a grant")


def test_retry_requires_history_and_supersedes_latest(session, org, policy) -> None:
    no_history = _allow(
        session,
        org,
        policy,
        task_id="task_expense_no_history",
        decision_purpose="retry",
        supersedes_decision_id="gdec_missing_0001",
    )
    assert no_history.decision_type == "deny"
    assert no_history.reason_codes == ("decision_revision_stale",)

    first = _allow(session, org, policy)
    retry = _allow(session, org, policy, decision_purpose="retry")
    assert retry.decision_type == "allow"
    assert retry.decision_revision == 2
    assert retry.supersedes_decision_id == first.decision_id

    wrong = _allow(
        session,
        org,
        policy,
        decision_purpose="retry",
        supersedes_decision_id="gdec_missing_0001",
    )
    assert wrong.decision_type == "deny"
    assert wrong.reason_codes == ("decision_revision_stale",)


# ---------------------------------------------------------------------------
# I3: grant gate references are bound to the decision
# ---------------------------------------------------------------------------


def test_grant_gate_refs_must_match_decision(session, org, policy) -> None:
    principal_id = principal_ref("prin", org.employee.id)
    payload_hash = "sha256:" + "1" * 64
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

    # A different confirmation is unsolicited and must be refused.
    try:
        _issue(
            session,
            org,
            decision,
            confirmation_ref=confirmation_ref(
                principal_id=principal_id, payload_hash="sha256:" + "2" * 64
            ),
        )
    except GovernanceError as exc:
        assert exc.reason_code == "confirmation_missing"
    else:  # pragma: no cover
        raise AssertionError("unsolicited confirmation must be refused")

    grant = _issue(session, org, decision)
    assert grant.confirmation_ref["payload_hash"] == payload_hash
    assert grant.approval_ref["approver_principal_id"] == "role_gov_approver"
    assert grant.consumption_mode == "single_use"


# ---------------------------------------------------------------------------
# I9: serializer
# ---------------------------------------------------------------------------


def test_grant_document_serializes_derivable_blocks(session, org, policy) -> None:
    grant = _issue(session, org, _allow(session, org, policy))
    document = grant_document(grant)

    assert document["grant_id"] == grant.grant_id
    assert document["task_ref"]["task_id"] == TASK_ID
    assert document["task_ref"]["idempotency_key"] == grant.task_idempotency_key
    assert document["subject"]["principal_id"] == grant.subject_id
    assert document["capabilities"]["allowed"] == ["expense.read@1.0.0"]
    assert document["policy_version"]["content_hash"] == policy.content_hash
    assert document["tool_contract"]["catalog_id"] == "cat_gov_tools"
    assert document["gates"] == {
        "confirmation_required": False,
        "approval_required": False,
    }
    assert document["revalidation"]["required"] is True
    assert document["binding"]["decision_hash_bound"] is True
    assert document["decision"]["decision_id"] == grant.decision_id
    assert document["decision"]["decision_hash"] == grant.decision_hash
    # No issuing service-identity registry exists in #78; the block is omitted
    # rather than fabricated.  Attempt/task aggregate blocks are deferred to
    # Issue #79.  This is why the serializer is not claimed as full schema
    # conformance.
    assert "attempt" not in document
    assert "issued_by" not in document
