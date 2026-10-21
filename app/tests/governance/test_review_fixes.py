"""Review-fix round 2 regressions (PR #87 / Issue #78).

Focused security tests added after the Master review of head ``df5ba0b``:

* B1: a grant may never widen the decision's cost/runtime budget, and the PEP
  re-checks the stored budget against the persisted decision on every call.
* I-A: a retry without a task produces a schema-valid clarification instead of
  an invalid ``deny(task_not_created)``.
* I-B: per-decision-type reason sets exactly match the frozen schema.
* I-C: malformed request fields fail closed before hashing/database casts.
* I-D: gate references require the full frozen shape.
* I-E: audit actors come from the resolved principal; resolution failures audit.
* I-F: revocation uses the service owner; ``issued_by`` is not self-declared.
* I-G: facade forwarding, idempotency races, denial audit de-duplication.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.governance import grants as grants_module
from app.governance.errors import (
    ALLOW_REASON_CODES,
    DECISION_REASON_CODES,
    DENY_REASON_CODES,
    REASON_CODES,
    GovernanceError,
    PolicyError,
)
from app.governance.grants import (
    get_grant,
    grant_document,
    issue_grant,
    revoke_grant,
)
from app.governance.models import ExecutionGrant
from app.governance.pdp import (
    EvaluationRequest,
    evaluate,
    validate_approval_ref,
    validate_confirmation_ref,
)
from app.governance.pep import enforce_tool_call
from app.governance.policies import (
    create_draft,
    parse_policy_document,
    publish,
    rollback,
)
from app.governance.principal import (
    principal_ref,
    resolve_employee_principal,
    resolve_principal,
    resolve_service_principal,
)
from app.governance.service import GovernanceService
from app.tests.governance.contracts import (
    DECISION_SCHEMA,
    assert_decision_document,
)
from app.tests.governance.helpers import (
    FakeEvidenceVerifier,
    PolicyContext,
    admin_principal,
    approval_ref,
    confirmation_ref,
    make_service_identity,
    policy_document,
    publish_policy,
    scope,
    unique_policy_set,
)

TASK_ID = "task_expense_0001"
TOOL_HASH = "sha256:" + "a" * 64
PAYLOAD_HASH = "sha256:" + "1" * 64

COST_RULE = {
    "rule_id": "cost_limited",
    "capability": "expense.read@1.0.0",
    "required_permissions": [["expense_claim", "read", "own"]],
    "allowed_scope_types": ["self"],
    "ttl_seconds": 900,
    "max_calls": 5,
    "max_cost_units": 10,
    "max_runtime_seconds": 60,
}

NO_COST_RULE = {
    "rule_id": "no_cost",
    "capability": "expense.read@1.0.0",
    "required_permissions": [["expense_claim", "read", "own"]],
    "allowed_scope_types": ["self"],
    "ttl_seconds": 900,
    "max_calls": 5,
    "max_cost_units": None,
    "max_runtime_seconds": None,
}


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
        "task_idempotency_key": getattr(grant, "task_idempotency_key", None),
        "capability": "expense.read@1.0.0",
        "call_id": "call_gov_fix_0001",
        "resource_scope": scope(),
        "tool_contract_version": "1.0.0",
    }
    payload.update(overrides)
    return enforce_tool_call(session, **payload)


def _cost_policy(session, org) -> PolicyContext:
    return publish_policy(session, org, rules=[COST_RULE])


def _no_cost_policy(session, org) -> PolicyContext:
    return publish_policy(session, org, rules=[NO_COST_RULE])


def _expect(exception_type, reason_code, call):
    with pytest.raises(exception_type) as excinfo:
        call()
    assert excinfo.value.reason_code == reason_code, excinfo.value.context


# ---------------------------------------------------------------------------
# B1: grant budget can never widen the decision budget
# ---------------------------------------------------------------------------


def test_grant_rejects_positive_budget_when_decision_authorizes_none(
    session, org
) -> None:
    policy = _no_cost_policy(session, org)
    decision = _allow(session, org, policy)
    assert decision.constraints["max_cost_units"] is None
    assert decision.constraints["max_runtime_seconds"] is None

    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _issue(session, org, decision, budget={"max_cost_units": 5}),
    )
    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _issue(session, org, decision, budget={"max_runtime_seconds": 30}),
    )
    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _issue(
            session, org, decision, budget={"max_cost_units": 0, "currency": "CNY"}
        ),
    )
    # A zero cost budget is a narrowing, not an expansion.
    narrowed = _issue(
        session, org, decision, budget={"max_cost_units": 0, "currency": "NONE"}
    )
    assert narrowed.budget["max_cost_units"] is None


def test_grant_rejects_budget_above_decision_limit(session, org) -> None:
    policy = _cost_policy(session, org)
    decision = _allow(session, org, policy)

    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _issue(session, org, decision, budget={"max_cost_units": 100}),
    )
    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _issue(session, org, decision, budget={"max_runtime_seconds": 61}),
    )
    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _issue(session, org, decision, budget={"max_calls": 6}),
    )

    grant = _issue(session, org, decision, budget={"max_cost_units": 10})
    assert grant.budget["max_cost_units"] == 10
    assert grant.budget["max_runtime_seconds"] == 60

    # Narrowing the cost budget to zero is allowed and still PEP-valid.
    zeroed = _issue(session, org, decision, budget={"max_cost_units": None})
    assert zeroed.budget["max_cost_units"] is None
    assert _enforce(session, org, zeroed).grant.status == "consumed"


def test_pep_rejects_stored_grant_that_exceeds_decision_budget(session, org) -> None:
    policy = _cost_policy(session, org)
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision, budget={"max_cost_units": 5})
    grant_id = grant.grant_id

    expanded = dict(grant.budget)
    expanded["max_cost_units"] = 100
    session.execute(
        update(ExecutionGrant)
        .where(ExecutionGrant.grant_id == grant_id)
        .values(budget=expanded)
    )
    session.flush()
    assert get_grant(session, grant_id).budget["max_cost_units"] == 100

    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _enforce(session, org, grant),
    )

    # Same for an expanded runtime budget.
    runtime_expanded = dict(grant.budget)
    runtime_expanded["max_cost_units"] = 5
    runtime_expanded["max_runtime_seconds"] = 1000
    session.execute(
        update(ExecutionGrant)
        .where(ExecutionGrant.grant_id == grant_id)
        .values(budget=runtime_expanded)
    )
    session.flush()
    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: _enforce(session, org, grant),
    )


def test_grant_document_computes_budget_binding(session, org) -> None:
    policy = _cost_policy(session, org)
    grant = _issue(session, org, _allow(session, org, policy), budget={"max_cost_units": 10})
    assert grant_document(grant)["binding"]["budget_within_decision"] is True

    expanded = dict(grant.budget)
    expanded["max_cost_units"] = 11
    session.execute(
        update(ExecutionGrant)
        .where(ExecutionGrant.grant_id == grant.grant_id)
        .values(budget=expanded)
    )
    session.flush()
    _expect(
        GovernanceError,
        "budget_exceeded",
        lambda: grant_document(get_grant(session, grant.grant_id)),
    )


# ---------------------------------------------------------------------------
# I-A: retry without a task never emits an invalid deny reason
# ---------------------------------------------------------------------------


def test_retry_without_task_is_a_schema_valid_clarification(
    session, org, policy
) -> None:
    decision = _allow(
        session,
        org,
        policy,
        task_id=None,
        task_revision=None,
        decision_purpose="retry",
    )
    assert decision.decision_type == "needs_clarification"
    assert decision.reason_codes == ("required_input_missing",)
    assert_decision_document(decision.to_document())


# ---------------------------------------------------------------------------
# I-B: exact per-decision-type reason sets and malformed input
# ---------------------------------------------------------------------------


def _schema_reason_sets() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for clause in DECISION_SCHEMA["allOf"]:
        decision_type = clause.get("if", {}).get("properties", {}).get(
            "decision_type"
        )
        if not decision_type or "const" not in decision_type:
            continue
        enum = (
            clause.get("then", {})
            .get("properties", {})
            .get("reason_codes", {})
            .get("items", {})
            .get("enum")
        )
        if enum is not None:
            result[decision_type["const"]] = set(enum)
    return result


def test_reason_code_sets_exactly_match_the_frozen_schema() -> None:
    schema_sets = _schema_reason_sets()
    assert schema_sets  # guards against a fixture/schema drift
    assert set(schema_sets) == set(DECISION_REASON_CODES)
    for decision_type, expected in schema_sets.items():
        assert DECISION_REASON_CODES[decision_type] == expected, decision_type

    frozen_all = set(DECISION_SCHEMA["$defs"]["reasonCode"]["enum"])
    assert set(REASON_CODES) == frozen_all
    # A deny decision may never borrow an allow/needs-only reason.
    assert DENY_REASON_CODES.isdisjoint(ALLOW_REASON_CODES)


@pytest.mark.parametrize(
    ("overrides", "expected_type"),
    [
        ({"task_revision": 2**60}, "needs_clarification"),
        ({"task_revision": True}, "needs_clarification"),
        ({"task_idempotency_key": "../../etc/passwd"}, "needs_clarification"),
        ({"decision_purpose": "replay"}, "needs_clarification"),
        ({"supersedes_decision_id": "not-a-decision"}, "needs_clarification"),
        ({"policy_set_id": 123}, "deny"),
        ({"policy_version": "latest"}, "deny"),
        ({"execution_mode": ["advisory"]}, "deny"),
        ({"risk": {"level": "low"}}, "deny"),
        ({"priority": ["urgent"]}, "needs_clarification"),
        ({"resource_scope": ["res_expense_0001"]}, "needs_clarification"),
        ({"confirmation_ref": ["conf_1"]}, "needs_clarification"),
        ({"approval_ref": "appr_1"}, "needs_clarification"),
        ({"payload_hash": 12345}, "deny"),
        ({"delegation_ref": 42}, "needs_clarification"),
        ({"correlation_id": ["corr_x"]}, "needs_clarification"),
        ({"pre_evaluation": "yes"}, "needs_clarification"),
    ],
)
def test_malformed_request_fields_fail_closed(
    session, org, policy, overrides, expected_type
) -> None:
    decision = _allow(session, org, policy, **overrides)
    assert decision.decision_type == expected_type, decision.reason_codes
    assert set(decision.reason_codes).issubset(
        DECISION_REASON_CODES[decision.decision_type]
    )
    assert_decision_document(decision.to_document())


def test_initial_decision_never_persists_a_supersedes_reference(
    session, org, policy
) -> None:
    decision = _allow(
        session,
        org,
        policy,
        supersedes_decision_id="gdec_other_0001",
    )
    assert decision.decision_type == "allow"
    assert decision.supersedes_decision_id is None


def test_deny_rule_cannot_use_an_allow_only_reason(session, org) -> None:
    policy_set_id = unique_policy_set()
    document = policy_document(
        policy_set_id,
        rules=[
            {
                "rule_id": "bad_deny_reason",
                "capability": "expense.delete@1.0.0",
                "effect": "deny",
                "reason_code": "policy_allowed",
            }
        ],
    )
    _expect(
        PolicyError,
        "policy_version_invalid",
        lambda: parse_policy_document(document),
    )

    # A valid deny rule emits only deny-valid codes, and the result validates.
    valid_set = unique_policy_set()
    valid = publish_policy(
        session,
        org,
        policy_set_id=valid_set,
        rules=[
            {
                "rule_id": "deny_reason",
                "capability": "expense.delete@1.0.0",
                "effect": "deny",
                "reason_code": "policy_denied",
            }
        ],
    )
    decision = evaluate(
        session,
        EvaluationRequest(
            principal=_principal(session, org),
            capability="expense.delete@1.0.0",
            task_id=TASK_ID,
            task_revision=1,
            resource_scope=scope(),
            policy_set_id=valid.policy_set_id,
        ),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("policy_denied",)
    assert_decision_document(decision.to_document())


# ---------------------------------------------------------------------------
# I-D: gate references must have the full frozen shape
# ---------------------------------------------------------------------------


def _minimal_legacy_confirmation() -> dict:
    now = datetime.now(UTC)
    return {
        "confirmation_id": "conf_legacy_0001",
        "payload_hash": PAYLOAD_HASH,
        "status": "satisfied",
        "single_use": True,
        "satisfied_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "consumed_at": None,
        "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "confirmed_by_principal_id": "prin_0001",
    }


def test_confirmation_ref_requires_purpose_and_canonicalization() -> None:
    legacy = _minimal_legacy_confirmation()
    _expect(
        GovernanceError,
        "confirmation_missing",
        lambda: validate_confirmation_ref(legacy, payload_hash=PAYLOAD_HASH),
    )

    full = confirmation_ref(principal_id="prin_0001")
    assert validate_confirmation_ref(full, payload_hash=PAYLOAD_HASH)

    for mutation in (
        {"purpose": "anything"},
        {"canonicalization": "json"},
        {"purpose": None},
        {"expires_at": "tomorrow"},
        {"satisfied_at": None},
    ):
        tampered = {**full, **mutation}
        _expect(
            GovernanceError,
            "confirmation_missing",
            lambda ref=tampered: validate_confirmation_ref(
                ref, payload_hash=PAYLOAD_HASH
            ),
        )

    unknown = {**full, "unexpected": True}
    _expect(
        GovernanceError,
        "confirmation_missing",
        lambda: validate_confirmation_ref(unknown, payload_hash=PAYLOAD_HASH),
    )


def test_approval_ref_requires_canonicalization_and_no_unknown_keys() -> None:
    ref = approval_ref(payload_hash=PAYLOAD_HASH)
    assert validate_approval_ref(
        ref, principal_id="prin_requester_0001", payload_hash=PAYLOAD_HASH
    )
    for mutation in (
        {"canonicalization": None},
        {"decided_at": "not-a-date"},
        {"decided_at": None},
        {"unknown_key": 1},
    ):
        tampered = {**ref, **mutation}
        _expect(
            GovernanceError,
            "approval_missing",
            lambda candidate=tampered: validate_approval_ref(
                candidate,
                principal_id="prin_requester_0001",
                payload_hash=PAYLOAD_HASH,
            ),
        )


def test_legacy_gate_reference_cannot_authorize_a_decision(session, org, policy) -> None:
    verifier = FakeEvidenceVerifier(
        role_resolutions={"role_gov_approver": principal_ref("prin", org.manager.id)}
    )
    decision = _allow(
        session,
        org,
        policy,
        capability="expense.submit@1.0.0",
        confirmation_ref=_minimal_legacy_confirmation(),
        approval_ref=approval_ref(payload_hash=PAYLOAD_HASH),
        payload_hash=PAYLOAD_HASH,
        evidence_verifier=verifier,
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("confirmation_missing",)
    assert_decision_document(decision.to_document())


# ---------------------------------------------------------------------------
# I-E: audit actor and resolution-failure audits
# ---------------------------------------------------------------------------


def _last_audit(session, action: str, *, resource_id: str | None = None):
    stmt = select(AuditEvent).where(AuditEvent.action == action)
    if resource_id is not None:
        stmt = stmt.where(AuditEvent.resource_id == resource_id)
    return session.scalar(stmt.order_by(AuditEvent.id.desc()).limit(1))


def test_policy_audit_actor_is_the_resolved_principal(session, org) -> None:
    admin = admin_principal(session, org)
    policy_set_id = unique_policy_set()
    document = policy_document(policy_set_id)

    draft = create_draft(session, document=document, principal=admin)
    assert draft.created_by_employee_id == org.admin.id
    created = _last_audit(
        session, "governance.policy.draft_created", resource_id=f"{policy_set_id}@1.0.0"
    )
    assert created is not None and created.actor_id == org.admin.id

    published = publish(session, policy_version_id=draft.id, principal=admin)
    published_event = _last_audit(
        session,
        "governance.policy.published",
        resource_id=f"{policy_set_id}@{published.version}",
    )
    assert published_event is not None and published_event.actor_id == org.admin.id

    rolled_back = rollback(
        session,
        principal=admin,
        policy_set_id=policy_set_id,
        target_version="1.0.0",
    )
    rollback_event = _last_audit(
        session,
        "governance.policy.rolled_back",
        resource_id=f"{policy_set_id}@{rolled_back.version}",
    )
    assert rollback_event is not None and rollback_event.actor_id == org.admin.id
    assert rolled_back.created_by_employee_id == org.admin.id


def test_principal_resolution_failure_paths_are_audited(session, org) -> None:
    attempts = (
        lambda: resolve_employee_principal(
            session, employee_id=org.employee.id, principal_type="robot"
        ),
        lambda: resolve_service_principal(session, service_identity_id="svc_not_a_number"),
        lambda: resolve_principal(session, principal_id="prin_not_a_number"),
        lambda: resolve_principal(session, principal_id=12345),
    )
    for attempt in attempts:
        before = session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "governance.principal.resolution_denied"
            )
        )
        with pytest.raises(GovernanceError):
            attempt()
        after = session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "governance.principal.resolution_denied"
            )
        )
        assert after == before + 1


# ---------------------------------------------------------------------------
# I-F: revocation authority and issued_by
# ---------------------------------------------------------------------------


def test_service_grant_revocation_uses_the_service_owner(session, org, policy) -> None:
    identity = make_service_identity(session, owner=org.finance, role_names=("employee",))
    service = resolve_service_principal(session, service_identity_id=identity.id)
    decision = evaluate(
        session,
        EvaluationRequest(
            principal=service,
            capability="expense.read@1.0.0",
            task_id=TASK_ID,
            task_revision=1,
            resource_scope=scope(),
            policy_set_id=policy.policy_set_id,
        ),
    )
    assert decision.decision_type == "allow"
    grant = issue_grant(
        session,
        principal=service,
        decision=decision,
        tool_contract_version="1.0.0",
        tool_contract_id="cat_gov_tools",
        tool_contract_hash=TOOL_HASH,
    )
    assert grant.subject_service_identity_id == principal_ref("svc", identity.id)
    assert grant.subject_employee_id is None

    unrelated = _principal(session, org)
    _expect(
        GovernanceError,
        "capability_not_granted",
        lambda: revoke_grant(
            session,
            grant_id=grant.grant_id,
            reason_code="manual_revoke",
            principal=unrelated,
        ),
    )

    owner = resolve_employee_principal(session, employee_id=org.finance.id)
    revoked = revoke_grant(
        session,
        grant_id=grant.grant_id,
        reason_code="manual_revoke",
        principal=owner,
    )
    assert revoked.status == "revoked"
    assert revoked.revoked_by_principal_id == owner.principal_id


def test_issued_by_service_identity_is_not_caller_supplied(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    with pytest.raises(TypeError):
        issue_grant(
            session,
            principal=_principal(session, org),
            decision=decision,
            tool_contract_version="1.0.0",
            tool_contract_id="cat_gov_tools",
            tool_contract_hash=TOOL_HASH,
            issued_by_service_identity_id="svc_0001",
        )
    grant = _issue(session, org, decision)
    assert grant.issued_by_service_identity_id is None
    assert "issued_by" not in grant_document(grant)


# ---------------------------------------------------------------------------
# I-G: facade, idempotency race, single denial audit, DB constraint
# ---------------------------------------------------------------------------


def test_service_facade_rollback_uses_the_resolved_principal(session, org) -> None:
    service = GovernanceService(session)
    admin = admin_principal(session, org)
    policy_set_id = unique_policy_set()
    draft = service.create_policy_draft(policy_document(policy_set_id), principal=admin)
    first = service.publish_policy(draft.id, principal=admin)

    second_draft = service.create_policy_draft(
        policy_document(policy_set_id, version="2.0.0"), principal=admin
    )
    service.publish_policy(second_draft.id, principal=admin)

    rolled_back = service.rollback(
        principal=admin,
        policy_set_id=policy_set_id,
        target_version=first.version,
    )
    assert rolled_back.status == "published"
    assert rolled_back.version == "2.0.1"
    assert service.get_active_policy(policy_set_id).id == rolled_back.id


def test_service_facade_forwards_the_evidence_verifier(session, org, policy) -> None:
    service = GovernanceService(session)
    verifier = FakeEvidenceVerifier()
    request_kwargs = {
        "principal": _principal(session, org),
        "capability": "expense.submit@1.0.0",
        "task_id": TASK_ID,
        "task_revision": 1,
        "resource_scope": scope(),
        "policy_set_id": policy.policy_set_id,
        "confirmation_ref": confirmation_ref(
            principal_id=principal_ref("prin", org.employee.id),
            payload_hash=PAYLOAD_HASH,
        ),
        "payload_hash": PAYLOAD_HASH,
    }

    unverified = service.evaluate(EvaluationRequest(**request_kwargs))
    assert unverified.decision_type == "needs_confirmation"

    verified = service.evaluate(
        EvaluationRequest(**request_kwargs), evidence_verifier=verifier
    )
    assert verified.decision_type == "needs_approval"


def test_grant_idempotency_insert_race_returns_the_winner(
    session, org, policy, monkeypatch
) -> None:
    decision = _allow(session, org, policy)
    key = "idem_gov_race_0001"
    first = _issue(session, org, decision, idempotency_key=key)
    session.commit()

    original = grants_module._find_grant_by_idempotency_key
    calls = {"count": 0}

    def race_reader(session_, key_):
        calls["count"] += 1
        if calls["count"] == 1:
            # Simulate losing the lookup race: the winner committed after our
            # read but before our insert tried to flush.
            return None
        return original(session_, key_)

    monkeypatch.setattr(grants_module, "_find_grant_by_idempotency_key", race_reader)

    replay = _issue(session, org, decision, idempotency_key=key)
    assert replay.grant_id == first.grant_id
    assert session.scalar(
        select(func.count()).select_from(ExecutionGrant)
    ) == 1


def test_pep_writes_exactly_one_denial_audit(session, org, policy) -> None:
    grant = _issue(session, org, _allow(session, org, policy))
    _expect(
        GovernanceError,
        "capability_not_granted",
        lambda: _enforce(session, org, grant, capability="expense.submit@1.0.0"),
    )
    rows = session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "governance.pep.denied",
            AuditEvent.resource_id == grant.grant_id,
        )
    ).all()
    matching = [
        event
        for event in rows
        if (event.details or {}).get("call_id") == "call_gov_fix_0001"
    ]
    assert len(matching) == 1
    assert matching[0].details["reason_code"] == "capability_not_granted"


def test_pep_rejects_non_mapping_resource_scope_with_audit(session, org, policy) -> None:
    grant = _issue(session, org, _allow(session, org, policy))
    _expect(
        GovernanceError,
        "grant_scope_mismatch",
        lambda: _enforce(session, org, grant, resource_scope=["res_expense_0001"]),
    )
    event = _last_audit(session, "governance.pep.denied", resource_id=grant.grant_id)
    assert event is not None
    assert event.details["reason_code"] == "grant_scope_mismatch"
    assert event.details["call_id"] == "call_gov_fix_0001"


def test_grant_audit_actor_comes_from_the_resolved_principal(session, org, policy) -> None:
    decision = _allow(session, org, policy)
    grant = _issue(session, org, decision)
    issued = _last_audit(
        session, "governance.grant.issued", resource_id=grant.grant_id
    )
    assert issued is not None and issued.actor_id == org.employee.id

    # A service identity without a requester is audited as its accountable
    # owner, never as ``None`` or a caller-supplied id.
    identity = make_service_identity(session, owner=org.finance, role_names=("employee",))
    service = resolve_service_principal(session, service_identity_id=identity.id)
    service_decision = evaluate(
        session,
        EvaluationRequest(
            principal=service,
            capability="expense.read@1.0.0",
            task_id=TASK_ID,
            task_revision=1,
            resource_scope=scope(),
            policy_set_id=policy.policy_set_id,
        ),
    )
    service_grant = issue_grant(
        session,
        principal=service,
        decision=service_decision,
        tool_contract_version="1.0.0",
        tool_contract_id="cat_gov_tools",
        tool_contract_hash=TOOL_HASH,
    )
    service_issued = _last_audit(
        session, "governance.grant.issued", resource_id=service_grant.grant_id
    )
    assert service_issued is not None
    assert service_issued.actor_id == org.finance.id


def test_grant_document_refuses_a_reused_decision(session, org, policy) -> None:
    grant = _issue(session, org, _allow(session, org, policy))
    grant.decision_purpose = "retry"
    session.flush()
    _expect(
        GovernanceError,
        "decision_revision_stale",
        lambda: grant_document(get_grant(session, grant.grant_id)),
    )


def test_canonical_json_rejects_non_string_object_keys() -> None:
    from app.governance.policies import canonical_json

    with pytest.raises(TypeError):
        canonical_json({1: "one"})
    assert canonical_json({"1": "one"}) == '{"1":"one"}'


def test_single_use_grant_rejects_a_null_call_budget_in_the_database(
    session, org, policy, engine
) -> None:
    grant = _issue(session, org, _allow(session, org, policy))
    grant_id = grant.grant_id
    session.commit()

    with Session(engine) as other:
        with pytest.raises(IntegrityError):
            other.execute(
                text(
                    "UPDATE execution_grants "
                    "SET budget = jsonb_set(budget, '{max_calls}', 'null') "
                    "WHERE grant_id = :grant_id"
                ),
                {"grant_id": grant_id},
            )
            other.flush()
        other.rollback()

    # Sanity: the stored budget is unchanged.
    assert _issue(session, org, _allow(session, org, policy)).budget["max_calls"] == 1
