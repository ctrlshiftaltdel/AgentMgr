"""Execution Grant issuance, expansion refusal and atomic consumption."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.governance.errors import GovernanceError
from app.governance.grants import (
    consume_grant,
    get_grant,
    issue_grant,
    revoke_grant,
)
from app.governance.pdp import EvaluationRequest, evaluate
from app.governance.principal import principal_ref, resolve_employee_principal
from app.tests.governance.helpers import PolicyContext, scope

TASK_ID = "task_expense_0001"


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
    return evaluate(session, EvaluationRequest(**payload), now=now)


def _issue(session, org, decision, **overrides):
    payload = {
        "principal": _principal(session, org),
        "decision": decision,
        "tool_contract_version": "1.0.0",
    }
    payload.update(overrides)
    return issue_grant(session, **payload)


def test_issue_grant_binds_decision_task_policy_and_scope(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision)

    assert grant.decision_id == decision.decision_id
    assert grant.decision_hash == decision.decision_hash
    assert grant.decision_revision == decision.decision_revision
    assert grant.task_id == TASK_ID
    assert grant.task_revision == 1
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
    assert grant.budget["max_calls"] == 5


def test_issue_only_from_authorizing_decision(session, org, policy) -> None:
    denied = _allow(session, org, policy, capability="unknown.capability@1.0.0")
    assert denied.decision_type == "deny"
    try:
        _issue(session, org, denied)
    except GovernanceError as exc:
        assert exc.reason_code == "policy_denied"
    else:  # pragma: no cover - defensive
        raise AssertionError("a deny decision must not issue a grant")


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
    past = datetime.now(timezone.utc) - timedelta(hours=2)
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

    # Same key, different request (tool contract version) -> replay/conflict,
    # not a second grant.
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
    principal = _principal(session, org)

    consumed = consume_grant(
        session,
        grant_id=grant.grant_id,
        principal=principal,
        task_id=TASK_ID,
        task_revision=1,
        capability="expense.read@1.0.0",
        call_id="call_gov_0001",
        resource_scope=scope(),
    )
    assert consumed.status == "consumed"
    assert consumed.usable is False
    assert consumed.consumed_by_call_id == "call_gov_0001"
    assert consumed.usage["calls_consumed"] == 1

    try:
        consume_grant(
            session,
            grant_id=grant.grant_id,
            principal=principal,
            task_id=TASK_ID,
            task_revision=1,
            capability="expense.read@1.0.0",
            call_id="call_gov_0002",
            resource_scope=scope(),
        )
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
    principal = _principal(session, org)

    first = consume_grant(
        session,
        grant_id=grant.grant_id,
        principal=principal,
        task_id=TASK_ID,
        task_revision=1,
        capability="expense.read@1.0.0",
        call_id="call_gov_m1",
        resource_scope=scope(),
    )
    assert first.status == "active"
    assert first.usage["calls_consumed"] == 1

    second = consume_grant(
        session,
        grant_id=grant.grant_id,
        principal=principal,
        task_id=TASK_ID,
        task_revision=1,
        capability="expense.read@1.0.0",
        call_id="call_gov_m2",
        resource_scope=scope(),
    )
    assert second.status == "consumed"
    assert second.usage["calls_consumed"] == 2

    try:
        consume_grant(
            session,
            grant_id=grant.grant_id,
            principal=principal,
            task_id=TASK_ID,
            task_revision=1,
            capability="expense.read@1.0.0",
            call_id="call_gov_m3",
            resource_scope=scope(),
        )
    except GovernanceError as exc:
        assert exc.reason_code == "grant_consumed"
    else:  # pragma: no cover
        raise AssertionError("exhausted multi-use grant must reject further calls")


def test_revoked_and_expired_grants_are_rejected(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    principal = _principal(session, org)

    revoked_grant = _issue(session, org, decision)
    revoke_grant(
        session,
        grant_id=revoked_grant.grant_id,
        reason_code="manual_revoke",
        revoked_by_principal_id=principal.principal_id,
    )
    try:
        consume_grant(
            session,
            grant_id=revoked_grant.grant_id,
            principal=principal,
            task_id=TASK_ID,
            task_revision=1,
            capability="expense.read@1.0.0",
            call_id="call_gov_revoked",
            resource_scope=scope(),
        )
    except GovernanceError as exc:
        assert exc.reason_code == "grant_revoked"
    else:  # pragma: no cover
        raise AssertionError("revoked grant must reject calls")

    short = _issue(session, org, decision, ttl_seconds=1)
    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    try:
        consume_grant(
            session,
            grant_id=short.grant_id,
            principal=principal,
            task_id=TASK_ID,
            task_revision=1,
            capability="expense.read@1.0.0",
            call_id="call_gov_expired",
            resource_scope=scope(),
            now=future,
        )
    except GovernanceError as exc:
        assert exc.reason_code == "grant_expired"
    else:  # pragma: no cover
        raise AssertionError("expired grant must reject calls")


def test_grant_rejects_subject_task_and_scope_mismatch(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision)
    principal = _principal(session, org)

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
        payload = {
            "grant_id": grant.grant_id,
            "principal": principal,
            "task_id": TASK_ID,
            "task_revision": 1,
            "capability": "expense.read@1.0.0",
            "call_id": "call_gov_mismatch",
            "resource_scope": scope(),
        }
        payload.update(overrides)
        try:
            consume_grant(session, **payload)
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
    principal = _principal(session, org)
    session.commit()
    grant_id = grant.grant_id

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        with Session(engine) as worker_session:
            barrier.wait(timeout=10)
            try:
                consume_grant(
                    worker_session,
                    grant_id=grant_id,
                    principal=principal,
                    task_id=TASK_ID,
                    task_revision=1,
                    capability="expense.read@1.0.0",
                    call_id="call_gov_concurrent",
                    resource_scope=scope(),
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
