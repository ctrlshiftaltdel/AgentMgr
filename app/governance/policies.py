"""Versioned governance policy store and policy-document validation.

A policy document is a plain JSON object with an explicit policy set, semantic
version, a mandatory ``default_effect: deny``, ``hard_denies`` and ordered
``rules``.  Policy versions are immutable after publication: publishing retires
the previous published row, and rollback creates a *new* version that points at
the source version through ``supersedes_id``.

The module also hosts the RFC 8785 JCS canonicalizer used for policy content
hashes, decision hashes and payload hashes.  Keeping it here avoids a second
cross-cutting utility module while keeping the dependency direction one-way:
``policies -> models/errors``; ``pdp -> policies``; ``grants/pep -> policies``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.governance.errors import (
    ALLOW_REASON_CODES,
    DENY_REASON_CODES,
    REASON_CODES,
    PolicyError,
)
from app.governance.models import GovernancePolicyVersion
from app.governance.principal import (
    GovernancePrincipal,
    audit_actor_id,
    require_management_permission,
)

__all__ = [
    "CAPABILITY_PATTERN",
    "DEFAULT_DECISION_TTL_SECONDS",
    "DEFAULT_POLICY_SET_ID",
    "POLICY_SCOPE_TYPES",
    "PolicyDocument",
    "PolicyRule",
    "canonical_json",
    "capability_matches",
    "content_hash",
    "create_draft",
    "get_active",
    "get_version",
    "list_versions",
    "parse_policy_document",
    "publish",
    "rollback",
    "scope_is_subset",
    "sha256_digest",
    "validate_resource_scope",
]

DEFAULT_POLICY_SET_ID = "polset_default"
DEFAULT_DECISION_TTL_SECONDS = 900

#: Capability identifiers are always ``name@x.y.z`` (frozen contract).
CAPABILITY_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*@[0-9]+\.[0-9]+\.[0-9]+$"
)
_POLICY_SET_ID_PATTERN = re.compile(r"^polset_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")

#: Scope types usable by an authorizing decision/grant.  ``none`` and
#: ``undetermined`` are deliberately excluded: they never authorize execution.
POLICY_SCOPE_TYPES: frozenset[str] = frozenset(
    {
        "self",
        "department",
        "company",
        "approval_chain",
        "project",
        "record_set",
        "connector",
    }
)
ALL_SCOPE_TYPES = POLICY_SCOPE_TYPES | {"none", "undetermined"}

#: ADR-001 §3.3.1 mapping from the existing RBAC ``permissions.scope`` to the
#: governance ``resource_scope.scope_type``.  A rule that requires an RBAC
#: permission may not declare a wider resource scope type than that permission
#: actually grants (``own`` must never authorize ``company`` data).
RBAC_SCOPE_TO_RESOURCE_SCOPE: dict[str, frozenset[str]] = {
    "own": frozenset({"self"}),
    "team": frozenset({"department"}),
    "company": frozenset({"company"}),
    "authorized": frozenset({"approval_chain"}),
}
DATA_CLASSIFICATIONS = frozenset(
    {"public", "internal", "confidential", "restricted"}
)
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
EXECUTION_MODES = frozenset(
    {"advisory", "governed_resource_access", "managed_execution"}
)

_RESOURCE_ID_PATTERN = re.compile(r"^res_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_CONNECTOR_ID_PATTERN = re.compile(r"^conn_[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")


# ---------------------------------------------------------------------------
# RFC 8785 JCS canonicalization
# ---------------------------------------------------------------------------


def _utf16_sort_key(value: str) -> tuple[int, ...]:
    """Sort key using UTF-16 code units, as RFC 8785 requires."""
    encoded = value.encode("utf-16-be")
    return tuple(
        struct.unpack(">H", encoded[index : index + 2])[0]
        for index in range(0, len(encoded), 2)
    )


MAX_SAFE_INTEGER = 2**53 - 1


def _ecmascript_number(value: float) -> str:
    """Render a finite float the way ECMAScript ``Number::toString`` does.

    Python's ``repr`` and ECMAScript both print the *shortest* round-tripping
    decimal digits, so only the presentation differs: ECMAScript uses fixed
    notation for ``1e-6 <= |x| < 1e21`` and exponential notation outside that
    range, with no zero padding in the exponent (RFC 8785 JCS number rules).
    """
    if value == 0:
        # JCS maps both +0 and -0 to "0".
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))

    rendered = repr(value)
    negative = rendered.startswith("-")
    if negative:
        rendered = rendered[1:]

    if "e" in rendered or "E" in rendered:
        mantissa, _, exponent_text = rendered.lower().partition("e")
        exponent = int(exponent_text)
    else:
        mantissa, exponent = rendered, 0

    if "." in mantissa:
        whole, fraction = mantissa.split(".", 1)
    else:
        whole, fraction = mantissa, ""
    digits = (whole + fraction).lstrip("0")
    if not digits:
        return "0"
    # Exponent of the first significant digit.
    if whole.strip("0"):
        decimal_exponent = len(whole.lstrip("0")) - 1 + exponent
    else:
        first_significant = next(
            index for index, char in enumerate(fraction) if char != "0"
        )
        decimal_exponent = -(first_significant + 1) + exponent

    digits = digits.rstrip("0") or "0"
    magnitude = abs(value)
    sign = "-" if negative else ""

    if 1e-6 <= magnitude < 1e21:
        if decimal_exponent >= 0:
            if len(digits) <= decimal_exponent + 1:
                fixed = digits + "0" * (decimal_exponent + 1 - len(digits))
                return sign + fixed
            return sign + digits[: decimal_exponent + 1] + "." + digits[
                decimal_exponent + 1 :
            ]
        return sign + "0." + "0" * (-decimal_exponent - 1) + digits

    # Exponential form: d[.ddd]e±n, no leading zeros in the exponent.
    mantissa_digits = digits[0] + (
        "." + digits[1:] if len(digits) > 1 else ""
    )
    exponent_sign = "+" if decimal_exponent >= 0 else "-"
    return f"{sign}{mantissa_digits}e{exponent_sign}{abs(decimal_exponent)}"


def _canonical_number(value: float) -> str:
    if isinstance(value, bool):
        raise TypeError("bool is not a JSON number")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(
                "JCS numbers must stay within the IEEE-754 safe integer range"
            )
        return str(value)
    if not math.isfinite(value):
        raise ValueError("JCS does not allow NaN or Infinity")
    return _ecmascript_number(value)


def canonical_json(value: Any) -> str:
    """Return the RFC 8785 JCS canonical UTF-8 JSON text for ``value``.

    Only the JSON data model is accepted.  Floats are serialized using the
    ECMAScript ``Number`` form; amounts should still be strings in business
    documents, as required by the contract.  The implementation is deliberately
    small and has no third-party dependency.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _canonical_number(value)
    if isinstance(value, Mapping):
        # RFC 8785 sorts by UTF-16 code units of JSON object *names*, which are
        # always strings.  Silently coercing a non-string key (``1`` vs
        # ``"1"``) would change meaning, so refuse it instead.
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JCS object keys must be strings")
        items = sorted(value.items(), key=lambda item: _utf16_sort_key(item[0]))
        return "{" + ",".join(
            canonical_json(key) + ":" + canonical_json(item)
            for key, item in items
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    raise TypeError(f"value is not JSON-serializable under JCS: {type(value)!r}")


def sha256_digest(data: bytes) -> str:
    """Return ``sha256:<hex>`` for raw bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_hash(document: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 of a policy document."""
    return sha256_digest(canonical_json(document).encode("utf-8"))


def payload_hash(payload: Any) -> str:
    """Return the canonical SHA-256 of a confirmation/approval payload."""
    return sha256_digest(canonical_json(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# Policy document model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """One deterministic rule in a policy document."""

    rule_id: str
    capability: str
    required_permissions: tuple[tuple[str, str, str], ...] = ()
    allowed_scope_types: frozenset[str] = POLICY_SCOPE_TYPES
    required_confirmation: bool = False
    required_approval: bool = False
    risk_levels: frozenset[str] = RISK_LEVELS
    delegation_allowed: bool = False
    effect: str = "allow"
    reason_code: str = "policy_allowed"
    allowed_execution_modes: frozenset[str] = EXECUTION_MODES
    requires_managed_execution: bool = False
    ttl_seconds: int = DEFAULT_DECISION_TTL_SECONDS
    max_calls: int = 20
    max_cost_units: float | None = None
    max_runtime_seconds: int | None = 300
    allowed_resource_ids: frozenset[str] | None = None
    allowed_connector_ids: frozenset[str] | None = None
    allowed_data_classifications: frozenset[str] = DATA_CLASSIFICATIONS


@dataclass(frozen=True)
class PolicyDocument:
    """A validated, immutable view over a policy document."""

    policy_set_id: str
    version: str
    default_effect: str
    hard_denies: tuple[PolicyRule, ...] = ()
    rules: tuple[PolicyRule, ...] = ()
    decision_ttl_seconds: int = DEFAULT_DECISION_TTL_SECONDS
    raw: Mapping[str, Any] = field(default_factory=dict)

    def hard_deny_for(self, capability: str) -> PolicyRule | None:
        for rule in self.hard_denies:
            if capability_matches(rule.capability, capability):
                return rule
        return None

    def rule_for(self, capability: str) -> PolicyRule | None:
        """Return a matching rule, with deny always winning.

        A ``deny`` rule beats every ``allow`` rule for the same capability
        regardless of document order; document order only decides between
        rules of the same effect.
        """
        first_allow: PolicyRule | None = None
        for rule in self.rules:
            if not capability_matches(rule.capability, capability):
                continue
            if rule.effect == "deny":
                return rule
            if first_allow is None:
                first_allow = rule
        return first_allow


def capability_matches(pattern: str, capability: str) -> bool:
    """Match a policy capability pattern against an exact capability.

    Both sides must use the ``name@x.y.z`` shape.  Patterns may use ``*`` in
    either the name or the version (``expense.*@1.0.0``, ``expense.read@*``).
    """
    if "@" not in pattern or "@" not in capability:
        return False
    pattern_name, pattern_version = pattern.rsplit("@", 1)
    name, version = capability.rsplit("@", 1)
    return fnmatch.fnmatchcase(name, pattern_name) and fnmatch.fnmatchcase(
        version, pattern_version
    )


def _is_capability_pattern(value: str) -> bool:
    if not isinstance(value, str) or "@" not in value:
        return False
    name, version = value.rsplit("@", 1)
    if not name or not version:
        return False
    name_parts = name.split(".")
    if any(
        not re.fullmatch(r"[a-z*][a-z0-9_*]*", part) for part in name_parts
    ):
        return False
    return bool(re.fullmatch(r"[0-9*][0-9.*]*", version))


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(
            "policy_version_invalid", context={"field": field_name}
        )
    return value


def _require_int(
    value: Any, field_name: str, *, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError("policy_version_invalid", context={"field": field_name})
    if not minimum <= value <= maximum:
        raise PolicyError("policy_version_invalid", context={"field": field_name})
    return value


def _string_set(value: Any, field_name: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise PolicyError("policy_version_invalid", context={"field": field_name})
    return frozenset(value)


def _permissions(value: Any, field_name: str) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise PolicyError("policy_version_invalid", context={"field": field_name})
    triples: list[tuple[str, str, str]] = []
    for item in value:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 3
            or not all(isinstance(part, str) for part in item)
        ):
            raise PolicyError(
                "policy_version_invalid", context={"field": field_name}
            )
        resource, action, scope = item
        if scope not in {"own", "team", "company", "authorized"}:
            raise PolicyError(
                "policy_version_invalid", context={"field": field_name}
            )
        triples.append((resource, action, scope))
    return tuple(triples)


def _resource_ids(value: Any, field_name: str, pattern: re.Pattern[str]) -> frozenset[str]:
    ids = _string_set(value, field_name)
    for resource_id in ids:
        if not pattern.fullmatch(resource_id):
            raise PolicyError(
                "policy_version_invalid", context={"field": field_name}
            )
    return ids


def _parse_rule(raw: Mapping[str, Any], *, default_effect: str = "allow") -> PolicyRule:
    if not isinstance(raw, Mapping):
        raise PolicyError("policy_version_invalid", context={"field": "rules"})
    unknown = set(raw) - {
        "rule_id",
        "capability",
        "required_permissions",
        "allowed_scope_types",
        "required_confirmation",
        "required_approval",
        "risk_levels",
        "delegation_allowed",
        "effect",
        "reason_code",
        "allowed_execution_modes",
        "requires_managed_execution",
        "ttl_seconds",
        "max_calls",
        "max_cost_units",
        "max_runtime_seconds",
        "allowed_resource_ids",
        "allowed_connector_ids",
        "allowed_data_classifications",
    }
    if unknown:
        raise PolicyError(
            "policy_version_invalid", context={"field": "rules", "unknown": sorted(unknown)}
        )

    rule_id = raw.get("rule_id")
    capability = raw.get("capability")
    if rule_id is None and default_effect == "deny" and isinstance(capability, str):
        rule_id = "hard_deny_" + re.sub(r"[^A-Za-z0-9_-]", "_", capability)
    if not isinstance(rule_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", rule_id
    ):
        raise PolicyError("policy_version_invalid", context={"field": "rule_id"})
    if not isinstance(capability, str) or not _is_capability_pattern(capability):
        raise PolicyError("policy_version_invalid", context={"field": "capability"})

    effect = raw.get("effect", default_effect)
    if effect not in {"allow", "deny"}:
        raise PolicyError("policy_version_invalid", context={"field": "effect"})
    # The default reason code follows the *rule's* effect, never the document
    # default: a deny rule without a reason must emit a deny reason.
    reason_code = raw.get(
        "reason_code", "policy_denied" if effect == "deny" else "policy_allowed"
    )
    if not isinstance(reason_code, str) or reason_code not in REASON_CODES:
        raise PolicyError("policy_version_invalid", context={"field": "reason_code"})
    allowed_reasons = (
        ALLOW_REASON_CODES if effect == "allow" else DENY_REASON_CODES
    )
    if reason_code not in allowed_reasons:
        raise PolicyError(
            "policy_version_invalid",
            context={
                "field": "reason_code",
                "reason": "not_valid_for_effect",
                "effect": effect,
            },
        )

    allowed_scope_types = _string_set(
        raw.get("allowed_scope_types", sorted(POLICY_SCOPE_TYPES)),
        "allowed_scope_types",
    )
    if not allowed_scope_types.issubset(POLICY_SCOPE_TYPES):
        raise PolicyError(
            "policy_version_invalid", context={"field": "allowed_scope_types"}
        )

    required_permissions = _permissions(
        raw.get("required_permissions", []), "required_permissions"
    )
    # ADR-001 §3.3.1: RBAC scope must map onto the matching resource scope.
    # Requiring ``own`` may never authorize ``department``/``company`` data.
    for _resource, _action, rbac_scope in required_permissions:
        mapped = RBAC_SCOPE_TO_RESOURCE_SCOPE[rbac_scope]
        if not allowed_scope_types.issubset(mapped):
            raise PolicyError(
                "policy_version_invalid",
                context={
                    "field": "allowed_scope_types",
                    "reason": "rbac_scope_mismatch",
                    "rbac_scope": rbac_scope,
                },
            )

    risk_levels = _string_set(raw.get("risk_levels", sorted(RISK_LEVELS)), "risk_levels")
    if not risk_levels.issubset(RISK_LEVELS):
        raise PolicyError("policy_version_invalid", context={"field": "risk_levels"})

    allowed_modes = _string_set(
        raw.get("allowed_execution_modes", sorted(EXECUTION_MODES)),
        "allowed_execution_modes",
    )
    if not allowed_modes.issubset(EXECUTION_MODES):
        raise PolicyError(
            "policy_version_invalid", context={"field": "allowed_execution_modes"}
        )

    allowed_classes = _string_set(
        raw.get("allowed_data_classifications", sorted(DATA_CLASSIFICATIONS)),
        "allowed_data_classifications",
    )
    if not allowed_classes.issubset(DATA_CLASSIFICATIONS):
        raise PolicyError(
            "policy_version_invalid",
            context={"field": "allowed_data_classifications"},
        )

    ttl_seconds = _require_int(
        raw.get("ttl_seconds", DEFAULT_DECISION_TTL_SECONDS),
        "ttl_seconds",
        minimum=1,
        maximum=86400,
    )
    max_calls = _require_int(raw.get("max_calls", 20), "max_calls", minimum=1, maximum=100000)
    max_runtime_seconds = raw.get("max_runtime_seconds", 300)
    if max_runtime_seconds is not None:
        max_runtime_seconds = _require_int(
            max_runtime_seconds,
            "max_runtime_seconds",
            minimum=1,
            maximum=86400,
        )
        if max_runtime_seconds > ttl_seconds:
            raise PolicyError(
                "policy_version_invalid", context={"field": "max_runtime_seconds"}
            )
    max_cost_units = raw.get("max_cost_units")
    if max_cost_units is not None:
        if isinstance(max_cost_units, bool) or not isinstance(
            max_cost_units, (int, float)
        ):
            raise PolicyError(
                "policy_version_invalid", context={"field": "max_cost_units"}
            )
        if max_cost_units < 0 or max_cost_units > 100_000_000:
            raise PolicyError(
                "policy_version_invalid", context={"field": "max_cost_units"}
            )

    allowed_resource_ids = raw.get("allowed_resource_ids")
    allowed_connector_ids = raw.get("allowed_connector_ids")

    return PolicyRule(
        rule_id=rule_id,
        capability=capability,
        required_permissions=required_permissions,
        allowed_scope_types=frozenset(allowed_scope_types),
        required_confirmation=_require_bool(
            raw.get("required_confirmation", False), "required_confirmation"
        ),
        required_approval=_require_bool(
            raw.get("required_approval", False), "required_approval"
        ),
        risk_levels=frozenset(risk_levels),
        delegation_allowed=_require_bool(
            raw.get("delegation_allowed", False), "delegation_allowed"
        ),
        effect=effect,
        reason_code=reason_code,
        allowed_execution_modes=frozenset(allowed_modes),
        requires_managed_execution=_require_bool(
            raw.get("requires_managed_execution", False),
            "requires_managed_execution",
        ),
        ttl_seconds=ttl_seconds,
        max_calls=max_calls,
        max_cost_units=max_cost_units,
        max_runtime_seconds=max_runtime_seconds,
        allowed_resource_ids=(
            _resource_ids(allowed_resource_ids, "allowed_resource_ids", _RESOURCE_ID_PATTERN)
            if allowed_resource_ids is not None
            else None
        ),
        allowed_connector_ids=(
            _resource_ids(
                allowed_connector_ids, "allowed_connector_ids", _CONNECTOR_ID_PATTERN
            )
            if allowed_connector_ids is not None
            else None
        ),
        allowed_data_classifications=frozenset(allowed_classes),
    )


def parse_policy_document(document: Mapping[str, Any]) -> PolicyDocument:
    """Validate a raw policy document and return an immutable view.

    Unknown top-level or rule keys are rejected: silently ignoring a field an
    author believed was enforced is a security bug, not a convenience.
    """
    if not isinstance(document, Mapping):
        raise PolicyError("policy_version_invalid", context={"field": "document"})
    allowed_keys = {
        "policy_set_id",
        "version",
        "default_effect",
        "hard_denies",
        "rules",
        "decision_ttl_seconds",
        "description",
    }
    unknown = set(document) - allowed_keys
    if unknown:
        raise PolicyError(
            "policy_version_invalid", context={"unknown": sorted(unknown)}
        )

    policy_set_id = document.get("policy_set_id")
    version = document.get("version")
    if not isinstance(policy_set_id, str) or not _POLICY_SET_ID_PATTERN.fullmatch(
        policy_set_id
    ):
        raise PolicyError("policy_version_invalid", context={"field": "policy_set_id"})
    if not isinstance(version, str) or not _SEMVER_PATTERN.fullmatch(version):
        raise PolicyError("policy_version_invalid", context={"field": "version"})

    # Fail closed: a document that says anything other than deny-by-default is
    # not a valid AgentMgr policy.
    if document.get("default_effect") != "deny":
        raise PolicyError("policy_version_invalid", context={"field": "default_effect"})

    hard_denies_raw = document.get("hard_denies", [])
    rules_raw = document.get("rules", [])
    if not isinstance(hard_denies_raw, (list, tuple)) or not isinstance(
        rules_raw, (list, tuple)
    ):
        raise PolicyError("policy_version_invalid", context={"field": "rules"})

    hard_denies: list[PolicyRule] = []
    for index, raw in enumerate(hard_denies_raw):
        parsed = _parse_rule(raw, default_effect="deny")
        if parsed.effect != "deny":
            raise PolicyError(
                "policy_version_invalid",
                context={"field": "hard_denies", "index": index},
            )
        hard_denies.append(parsed)

    rules: list[PolicyRule] = []
    seen_rule_ids: set[str] = set()
    for raw in rules_raw:
        parsed = _parse_rule(raw)
        if parsed.rule_id in seen_rule_ids:
            raise PolicyError(
                "policy_version_invalid", context={"field": "rule_id"}
            )
        seen_rule_ids.add(parsed.rule_id)
        rules.append(parsed)

    decision_ttl_seconds = _require_int(
        document.get("decision_ttl_seconds", DEFAULT_DECISION_TTL_SECONDS),
        "decision_ttl_seconds",
        minimum=1,
        maximum=86400,
    )

    return PolicyDocument(
        policy_set_id=policy_set_id,
        version=version,
        default_effect="deny",
        hard_denies=tuple(hard_denies),
        rules=tuple(rules),
        decision_ttl_seconds=decision_ttl_seconds,
        raw=dict(document),
    )


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def validate_resource_scope(
    scope: Mapping[str, Any] | None,
    *,
    require_concrete: bool = True,
) -> dict[str, Any]:
    """Validate and normalize a resource-scope object.

    Empty sets mean "no resources of this kind", never "all resources".  An
    authorizing scope must be concrete and must list at least one resource or
    connector plus at least one data classification.
    """
    if scope is None:
        scope = {"scope_type": "undetermined"}
    if not isinstance(scope, Mapping):
        raise PolicyError(
            "resource_scope_ambiguous", context={"field": "resource_scope"}
        )
    allowed_keys = {
        "scope_type",
        "resource_ids",
        "connector_ids",
        "data_classifications",
        "exclude_resource_ids",
    }
    unknown = set(scope) - allowed_keys
    if unknown:
        raise PolicyError(
            "resource_scope_ambiguous", context={"unknown": sorted(unknown)}
        )

    scope_type = scope.get("scope_type")
    if scope_type not in ALL_SCOPE_TYPES:
        raise PolicyError(
            "resource_scope_denied", context={"field": "scope_type"}
        )

    def _ids(field_name: str, pattern: re.Pattern[str], maximum: int) -> list[str]:
        value = scope.get(field_name, [])
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            raise PolicyError(
                "resource_scope_ambiguous", context={"field": field_name}
            )
        if len(value) > maximum:
            raise PolicyError(
                "resource_scope_denied", context={"field": field_name}
            )
        if len(set(value)) != len(value):
            raise PolicyError(
                "resource_scope_ambiguous", context={"field": field_name}
            )
        for item in value:
            if not pattern.fullmatch(item):
                raise PolicyError(
                    "resource_scope_denied", context={"field": field_name}
                )
        return list(value)

    resource_ids = _ids("resource_ids", _RESOURCE_ID_PATTERN, 1000)
    connector_ids = _ids("connector_ids", _CONNECTOR_ID_PATTERN, 32)
    exclude_resource_ids = _ids(
        "exclude_resource_ids", _RESOURCE_ID_PATTERN, 1000
    )
    classifications = scope.get("data_classifications", [])
    if not isinstance(classifications, (list, tuple)) or not all(
        isinstance(item, str) for item in classifications
    ):
        raise PolicyError(
            "resource_scope_ambiguous", context={"field": "data_classifications"}
        )
    if len(set(classifications)) != len(classifications):
        raise PolicyError(
            "resource_scope_ambiguous", context={"field": "data_classifications"}
        )
    if not set(classifications).issubset(DATA_CLASSIFICATIONS):
        raise PolicyError(
            "data_classification_denied", context={"field": "data_classifications"}
        )

    if set(resource_ids) & set(exclude_resource_ids):
        raise PolicyError(
            "resource_scope_denied", context={"field": "exclude_resource_ids"}
        )

    if scope_type in {"none", "undetermined"}:
        if resource_ids or connector_ids or exclude_resource_ids or classifications:
            raise PolicyError(
                "resource_scope_denied", context={"field": "scope_type"}
            )
    elif require_concrete:
        if not resource_ids and not connector_ids:
            raise PolicyError(
                "resource_scope_ambiguous", context={"field": "resource_ids"}
            )
        if not classifications:
            raise PolicyError(
                "resource_scope_ambiguous", context={"field": "data_classifications"}
            )

    return {
        "scope_type": scope_type,
        "resource_ids": resource_ids,
        "connector_ids": connector_ids,
        "data_classifications": list(classifications),
        "exclude_resource_ids": exclude_resource_ids,
    }


def scope_is_subset(requested: Mapping[str, Any], allowed: Mapping[str, Any]) -> bool:
    """Return whether ``requested`` stays inside ``allowed``.

    Scope-type changes are refused (a grant cannot silently widen or reinterpret
    the type), resources/connectors/classifications must be subsets, and the
    exclusion set may only grow.
    """
    try:
        want = validate_resource_scope(requested, require_concrete=False)
        have = validate_resource_scope(allowed, require_concrete=False)
    except PolicyError:
        return False
    if want["scope_type"] != have["scope_type"]:
        return False
    if not set(want["resource_ids"]).issubset(have["resource_ids"]):
        return False
    if not set(want["connector_ids"]).issubset(have["connector_ids"]):
        return False
    if not set(want["data_classifications"]).issubset(
        have["data_classifications"]
    ):
        return False
    if not set(have["exclude_resource_ids"]).issubset(
        want["exclude_resource_ids"]
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Store operations
# ---------------------------------------------------------------------------


def _audit(
    session: Session,
    *,
    actor_id: int | None,
    action: str,
    row: GovernancePolicyVersion,
    extra: Mapping[str, Any] | None = None,
) -> None:
    details: dict[str, Any] = {
        "policy_set_id": row.policy_set_id,
        "version": row.version,
        "content_hash": row.content_hash,
        "status": row.status,
    }
    if extra:
        details.update(extra)
    record_event(
        session,
        actor_id=actor_id,
        action=action,
        resource_type="governance_policy_version",
        resource_id=f"{row.policy_set_id}@{row.version}",
        details=details,
    )


def create_draft(
    session: Session,
    document: Mapping[str, Any],
    *,
    principal: GovernancePrincipal,
    supersedes_id: int | None = None,
) -> GovernancePolicyVersion:
    """Validate ``document`` and persist a new ``draft`` policy version.

    A draft is a management operation: the caller must be a resolved principal
    holding the existing ``identity/manage/company`` permission.

    ``created_by_employee_id`` and the audit actor are derived from the
    resolved principal, never from a caller-supplied id: a request body cannot
    attribute a policy change to somebody else.
    """
    require_management_permission(principal)
    actor_id = audit_actor_id(principal)
    parsed = parse_policy_document(document)
    existing = session.scalar(
        select(GovernancePolicyVersion.id).where(
            GovernancePolicyVersion.policy_set_id == parsed.policy_set_id,
            GovernancePolicyVersion.version == parsed.version,
        )
    )
    if existing is not None:
        raise PolicyError(
            "policy_version_invalid",
            context={"policy_set_id": parsed.policy_set_id, "version": parsed.version},
        )
    if supersedes_id is not None:
        superseded = session.get(GovernancePolicyVersion, supersedes_id)
        if (
            superseded is None
            or superseded.policy_set_id != parsed.policy_set_id
        ):
            raise PolicyError("policy_version_invalid", context={"field": "supersedes_id"})

    row = GovernancePolicyVersion(
        policy_set_id=parsed.policy_set_id,
        version=parsed.version,
        status="draft",
        document=dict(document),
        content_hash=content_hash(document),
        created_by_employee_id=actor_id,
        supersedes_id=supersedes_id,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        actor_id=actor_id,
        action="governance.policy.draft_created",
        row=row,
    )
    return row


def publish(
    session: Session,
    policy_version_id: int,
    *,
    principal: GovernancePrincipal,
    now: datetime | None = None,
) -> GovernancePolicyVersion:
    """Publish a draft, retiring the currently published version of the set.

    The audit actor is the resolved principal, never a caller-supplied
    employee id.
    """
    require_management_permission(principal)
    actor_id = audit_actor_id(principal)
    timestamp = now or datetime.now(UTC)
    row = session.get(
        GovernancePolicyVersion,
        policy_version_id,
        with_for_update=True,
        populate_existing=True,
    )
    if row is None:
        raise PolicyError("policy_version_invalid", context={"field": "policy_version_id"})
    if row.status == "published":
        return row
    if row.status != "draft":
        raise PolicyError(
            "policy_version_invalid",
            context={"policy_set_id": row.policy_set_id, "version": row.version},
        )

    # Recompute the hash: a draft whose stored hash no longer matches its
    # document must not be promoted.
    if content_hash(row.document) != row.content_hash:
        raise PolicyError(
            "policy_version_invalid",
            context={"policy_set_id": row.policy_set_id, "version": row.version},
        )
    parse_policy_document(row.document)

    session.execute(
        update(GovernancePolicyVersion)
        .where(
            GovernancePolicyVersion.policy_set_id == row.policy_set_id,
            GovernancePolicyVersion.status == "published",
            GovernancePolicyVersion.id != row.id,
        )
        .values(status="retired", retired_at=timestamp)
    )
    session.flush()

    row.status = "published"
    row.published_at = timestamp
    row.retired_at = None
    session.flush()
    _audit(
        session,
        actor_id=actor_id,
        action="governance.policy.published",
        row=row,
    )
    return row


def get_version(
    session: Session, policy_set_id: str, version: str
) -> GovernancePolicyVersion | None:
    """Return a specific policy version regardless of status."""
    return session.scalar(
        select(GovernancePolicyVersion).where(
            GovernancePolicyVersion.policy_set_id == policy_set_id,
            GovernancePolicyVersion.version == version,
        )
    )


def get_active(
    session: Session,
    policy_set_id: str = DEFAULT_POLICY_SET_ID,
    version: str | None = None,
) -> GovernancePolicyVersion | None:
    """Return the active published version, or ``None`` (callers fail closed).

    When ``version`` is supplied it must be the *published* version; an old or
    draft version reference is never silently accepted.
    """
    stmt = (
        select(GovernancePolicyVersion)
        .where(
            GovernancePolicyVersion.policy_set_id == policy_set_id,
            GovernancePolicyVersion.status == "published",
        )
        .execution_options(populate_existing=True)
    )
    if version is not None:
        stmt = stmt.where(GovernancePolicyVersion.version == version)
    rows = session.scalars(stmt).all()
    if len(rows) > 1:
        raise PolicyError(
            "policy_version_invalid",
            context={"policy_set_id": policy_set_id, "reason": "multiple_published"},
        )
    return rows[0] if rows else None


def list_versions(
    session: Session, policy_set_id: str
) -> list[GovernancePolicyVersion]:
    """Return all versions of a policy set, newest first by semantic version."""
    rows = list(
        session.scalars(
            select(GovernancePolicyVersion).where(
                GovernancePolicyVersion.policy_set_id == policy_set_id
            )
        )
    )

    def _key(row: GovernancePolicyVersion) -> tuple[int, int, int]:
        return tuple(int(part) for part in row.version.split("."))  # type: ignore[return-value]

    return sorted(rows, key=_key, reverse=True)


def _next_version(
    session: Session, policy_set_id: str, *, base: str | None = None
) -> str:
    versions = [row.version for row in list_versions(session, policy_set_id)]
    if base is None:
        if not versions:
            return "1.0.0"
        base = versions[0]
    parts = base.split(".")
    if len(parts) != 3:
        raise PolicyError("policy_version_invalid", context={"field": "version"})
    major, minor, patch = (int(part) for part in parts)
    candidate = f"{major}.{minor}.{patch + 1}"
    while get_version(session, policy_set_id, candidate) is not None:
        patch += 1
        candidate = f"{major}.{minor}.{patch + 1}"
    return candidate


def rollback(
    session: Session,
    *,
    principal: GovernancePrincipal,
    policy_set_id: str,
    target_version: str,
    new_version: str | None = None,
    now: datetime | None = None,
) -> GovernancePolicyVersion:
    """Restore a previous policy as a **new** published version.

    The target row is read, never mutated.  A new version is created with
    ``supersedes_id`` pointing at the current published version (or the target
    if there is none), preserving the full version history.  The audit actor is
    the resolved principal throughout.
    """
    require_management_permission(principal)
    actor_id = audit_actor_id(principal)
    target = get_version(session, policy_set_id, target_version)
    if target is None:
        raise PolicyError(
            "policy_version_invalid",
            context={"policy_set_id": policy_set_id, "version": target_version},
        )
    if target.status == "draft":
        raise PolicyError(
            "policy_version_invalid",
            context={"policy_set_id": policy_set_id, "version": target_version},
        )

    current = get_active(session, policy_set_id)
    version = new_version or _next_version(session, policy_set_id)
    if get_version(session, policy_set_id, version) is not None:
        raise PolicyError(
            "policy_version_invalid",
            context={"policy_set_id": policy_set_id, "version": version},
        )

    document = dict(target.document)
    document["version"] = version
    if document.get("policy_set_id") != policy_set_id:
        raise PolicyError("policy_version_invalid", context={"field": "policy_set_id"})

    draft = create_draft(
        session,
        document=document,
        principal=principal,
        supersedes_id=(current.id if current is not None else target.id),
    )
    published = publish(
        session,
        policy_version_id=draft.id,
        principal=principal,
        now=now,
    )
    _audit(
        session,
        actor_id=actor_id,
        action="governance.policy.rolled_back",
        row=published,
        extra={"target_version": target_version},
    )
    return published
