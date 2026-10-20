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


def test_deny_rule_default_reason_and_effect_validation() -> None:
    policy_set_id = unique_policy_set()
    document = policy_document(
        policy_set_id,
        rules=[
            {
                "rule_id": "deny_without_reason",
                "capability": "expense.delete@1.0.0",
                "effect": "deny",
            }
        ],
    )
    parsed = parse_policy_document(document)
    rule = parsed.rule_for("expense.delete@1.0.0")
    assert rule is not None
    assert rule.effect == "deny"
    assert rule.reason_code == "policy_denied"

    # A deny effect may not carry an allow-only reason code.
    bad = policy_document(
        policy_set_id,
        rules=[
            {
                "rule_id": "bad_reason",
                "capability": "expense.delete@1.0.0",
                "effect": "deny",
                "reason_code": "policy_allowed",
            }
        ],
    )
    with pytest.raises(PolicyError):
        parse_policy_document(bad)


def test_deny_rule_wins_regardless_of_document_order() -> None:
    policy_set_id = unique_policy_set()
    document = policy_document(
        policy_set_id,
        rules=[
            {
                "rule_id": "allow_first",
                "capability": "expense.delete@1.0.0",
                "effect": "allow",
            },
            {
                "rule_id": "deny_second",
                "capability": "expense.delete@1.0.0",
                "effect": "deny",
            },
        ],
    )
    parsed = parse_policy_document(document)
    rule = parsed.rule_for("expense.delete@1.0.0")
    assert rule is not None
    assert rule.effect == "deny"


def test_rbac_scope_must_map_to_resource_scope() -> None:
    policy_set_id = unique_policy_set()
    bad = policy_document(
        policy_set_id,
        rules=[
            {
                "rule_id": "own_cannot_be_company",
                "capability": "expense.read@1.0.0",
                "required_permissions": [["expense_claim", "read", "own"]],
                "allowed_scope_types": ["company"],
            }
        ],
    )
    with pytest.raises(PolicyError) as excinfo:
        parse_policy_document(bad)
    assert excinfo.value.reason_code == "policy_version_invalid"

    good = policy_document(
        policy_set_id,
        rules=[
            {
                "rule_id": "team_maps_department",
                "capability": "expense.read@1.0.0",
                "required_permissions": [["expense_claim", "read", "team"]],
                "allowed_scope_types": ["department"],
            }
        ],
    )
    assert parse_policy_document(good).rule_for("expense.read@1.0.0") is not None


def test_max_cost_units_upper_bound() -> None:
    policy_set_id = unique_policy_set()
    document = policy_document(
        policy_set_id,
        rules=[
            {
                "rule_id": "too_costly",
                "capability": "expense.read@1.0.0",
                "max_cost_units": 100_000_001,
            }
        ],
    )
    with pytest.raises(PolicyError):
        parse_policy_document(document)


def test_jcs_small_and_large_numbers() -> None:
    assert canonical_json(1e-7) == "1e-7"
    assert canonical_json(1e-6) == "0.000001"
    assert canonical_json(1e21) == "1e+21"
    assert canonical_json(9007199254740991) == "9007199254740991"
    assert canonical_json(-0.0) == "0"
    with pytest.raises(ValueError):
        canonical_json(9007199254740992)


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
