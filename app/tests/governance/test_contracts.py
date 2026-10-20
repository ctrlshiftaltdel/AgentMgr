"""Every emitted decision validates against the frozen Phase 0 schema."""

from __future__ import annotations

from app.governance.grants import grant_document, issue_grant
from app.governance.pdp import EvaluationRequest, evaluate
from app.governance.principal import principal_ref, resolve_employee_principal
from app.tests.governance.contracts import (
    assert_decision_document,
    assert_grant_definition,
)
from app.tests.governance.helpers import (
    PolicyContext,
    FakeEvidenceVerifier,
    confirmation_ref,
    scope,
)

TASK_ID = "task_expense_0001"
TOOL_HASH = "sha256:" + "a" * 64


def _request(session, org, policy: PolicyContext, **overrides) -> EvaluationRequest:
    payload = {
        "principal": resolve_employee_principal(session, employee_id=org.employee.id),
        "capability": "expense.read@1.0.0",
        "task_id": TASK_ID,
        "task_revision": 1,
        "resource_scope": scope(),
        "policy_set_id": policy.policy_set_id,
    }
    payload.update(overrides)
    return EvaluationRequest(**payload)


def test_emitted_decisions_match_the_frozen_schema(session, org, policy) -> None:
    principal_id = principal_ref("prin", org.employee.id)
    payload_hash = "sha256:" + "1" * 64
    verifier = FakeEvidenceVerifier()

    documents = [
        evaluate(session, _request(session, org, policy)),
        evaluate(
            session,
            _request(session, org, policy, capability="unknown.capability@1.0.0"),
        ),
        evaluate(
            session,
            _request(session, org, policy, capability="shell.exec"),
        ),
        evaluate(
            session,
            _request(session, org, policy, capability="expense.submit@1.0.0"),
        ),
        evaluate(
            session,
            _request(
                session,
                org,
                policy,
                capability="expense.submit@1.0.0",
                confirmation_ref=confirmation_ref(
                    principal_id=principal_id, payload_hash=payload_hash
                ),
                payload_hash=payload_hash,
            ),
            evidence_verifier=verifier,
        ),
        evaluate(
            session,
            _request(session, org, policy, capability="managed.task@1.0.0"),
        ),
        evaluate(
            session,
            _request(
                session,
                org,
                policy,
                task_id=None,
                task_revision=None,
                pre_evaluation=True,
            ),
        ),
    ]
    assert [document.decision_type for document in documents] == [
        "allow",
        "deny",
        "needs_clarification",
        "needs_confirmation",
        "needs_approval",
        "managed_execution_required",
        "pre_evaluation",
    ]
    for document in documents:
        assert_decision_document(document.to_document())


def test_deny_rule_without_reason_is_schema_valid(session, org, policy) -> None:
    from app.governance.pdp import evaluate as evaluate_decision
    from app.tests.governance.helpers import (
        publish_policy,
        unique_policy_set,
    )

    policy_set_id = unique_policy_set()
    custom = publish_policy(
        session,
        org,
        policy_set_id=policy_set_id,
        rules=[
            {
                "rule_id": "deny_without_reason",
                "capability": "expense.delete@1.0.0",
                "effect": "deny",
            }
        ],
    )
    decision = evaluate_decision(
        session,
        _request(
            session,
            org,
            custom,
            capability="expense.delete@1.0.0",
        ),
    )
    assert decision.decision_type == "deny"
    assert decision.reason_codes == ("policy_denied",)
    assert_decision_document(decision.to_document())


def test_grant_serializer_blocks_match_the_frozen_schema(session, org, policy) -> None:
    decision = evaluate(session, _request(session, org, policy))
    grant = issue_grant(
        session,
        principal=resolve_employee_principal(session, employee_id=org.employee.id),
        decision=decision,
        tool_contract_version="1.0.0",
        tool_contract_id="cat_gov_tools",
        tool_contract_hash=TOOL_HASH,
    )
    document = grant_document(grant)

    for definition, value in (
        ("taskRef", document["task_ref"]),
        ("grantSubject", document["subject"]),
        ("capabilities", document["capabilities"]),
        ("resourceScope", document["resource_scope"]),
        ("policyVersion", document["policy_version"]),
        ("toolContract", document["tool_contract"]),
        ("budget", document["budget"]),
        ("usage", document["usage"]),
        ("revocation", document["revocation"]),
        ("consumption", document["consumption"]),
        ("revalidation", document["revalidation"]),
        ("binding", document["binding"]),
        ("decision", document["decision"]),
    ):
        assert_grant_definition(definition, value)
    # ``gates`` is defined inline in the schema, not under $defs.
    assert set(document["gates"]) == {"confirmation_required", "approval_required"}
    # Full Grant schema conformance is intentionally not claimed: Issue #79
    # owns ``attempt`` and #78 has no issuing service-identity registry.
    assert "attempt" not in document
    assert "issued_by" not in document
