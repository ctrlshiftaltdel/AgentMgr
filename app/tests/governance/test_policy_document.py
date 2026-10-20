"""Pure policy/canonicalization tests (no database required)."""

from __future__ import annotations

import pytest

from app.governance.errors import PolicyError
from app.governance.policies import (
    canonical_json,
    capability_matches,
    content_hash,
    parse_policy_document,
    payload_hash,
    scope_is_subset,
)
from app.tests.governance.helpers import policy_document, scope, unique_policy_set


def test_jcs_test_vectors() -> None:
    vector1 = {
        "amount": "100.00",
        "currency": "CNY",
        "payee": "res_vendor_0001",
        "purpose": "expense.submit",
    }
    assert canonical_json(vector1) == (
        '{"amount":"100.00","currency":"CNY","payee":"res_vendor_0001",'
        '"purpose":"expense.submit"}'
    )
    assert payload_hash(vector1) == (
        "sha256:f0470abd268f709bf23a3d2d93c51e801fb59c997ce072d60f8fa29e9d66a9fa"
    )

    vector2 = {
        "confirmation_id": "conf_expense_0001",
        "payload": {
            "amount": 100.5,
            "note": "café",
            "resource_ids": ["res_expense_0001", "res_expense_0002"],
        },
        "single_use": True,
    }
    assert canonical_json(vector2) == (
        '{"confirmation_id":"conf_expense_0001","payload":{"amount":100.5,'
        '"note":"café","resource_ids":["res_expense_0001","res_expense_0002"]},'
        '"single_use":true}'
    )
    assert payload_hash(vector2) == (
        "sha256:8f624db9d2c9cea16272730704c4e0e5d22ed37d43afecf4115860c877fda80d"
    )


def test_canonical_hash_is_order_independent() -> None:
    left = {"b": 1, "a": {"y": [2, 1], "x": None}}
    right = {"a": {"x": None, "y": [2, 1]}, "b": 1}
    assert canonical_json(left) == canonical_json(right)
    assert content_hash(left) == content_hash(right)


def test_capability_patterns() -> None:
    assert capability_matches("expense.read@1.0.0", "expense.read@1.0.0")
    assert capability_matches("expense.*@1.0.0", "expense.read@1.0.0")
    assert capability_matches("expense.read@*", "expense.read@2.3.4")
    assert not capability_matches("expense.read@1.0.0", "expense.read@2.0.0")
    assert not capability_matches("expense", "expense.read@1.0.0")


def test_policy_document_validation() -> None:
    policy_set_id = unique_policy_set()
    document = policy_document(policy_set_id)
    parsed = parse_policy_document(document)
    assert parsed.default_effect == "deny"
    assert parsed.rule_for("expense.read@1.0.0") is not None
    assert parsed.hard_deny_for("payment.execute@1.0.0") is not None
    assert parsed.rule_for("unknown.capability@1.0.0") is None

    missing_default = dict(document)
    missing_default.pop("default_effect")
    with pytest.raises(PolicyError) as excinfo:
        parse_policy_document(missing_default)
    assert excinfo.value.reason_code == "policy_version_invalid"

    unknown_field = {**document, "allow_everything": True}
    with pytest.raises(PolicyError):
        parse_policy_document(unknown_field)

    bad_rule = policy_document(
        policy_set_id,
        rules=[{"rule_id": "bad", "capability": "shell.exec"}],
    )
    with pytest.raises(PolicyError):
        parse_policy_document(bad_rule)


def test_scope_subset_rules() -> None:
    allowed = scope(
        "self",
        resource_ids=("res_expense_0001", "res_expense_0002"),
        classifications=("public", "internal"),
    )
    narrower = scope(
        "self",
        resource_ids=("res_expense_0001",),
        classifications=("internal",),
    )
    wider = scope(
        "self",
        resource_ids=("res_expense_0001", "res_expense_9999"),
        classifications=("internal",),
    )
    assert scope_is_subset(narrower, allowed)
    assert not scope_is_subset(wider, allowed)
    assert not scope_is_subset(scope("company"), allowed)
