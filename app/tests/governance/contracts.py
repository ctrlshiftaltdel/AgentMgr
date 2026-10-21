"""Frozen-contract validation helpers for governance documents.

The schemas live in ``docs/contracts`` and are part of the Phase 0 frozen
bundle.  Validation uses JSON Schema Draft 2020-12 with ``FormatChecker`` so a
``format: date-time`` violation is a failure, not a comment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = REPO_ROOT / "docs" / "contracts"


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


DECISION_SCHEMA = _load("governance-decision.schema.json")
GRANT_SCHEMA = _load("execution-grant.schema.json")

_DECISION_VALIDATOR = Draft202012Validator(
    DECISION_SCHEMA, format_checker=FormatChecker()
)


def decision_errors(document: dict[str, Any]) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"{error.message}"
        for error in _DECISION_VALIDATOR.iter_errors(document)
    ]


def assert_decision_document(document: dict[str, Any]) -> None:
    errors = decision_errors(document)
    assert not errors, "decision does not match governance-decision.schema.json:\n" + "\n".join(
        errors
    )


def grant_definition_validator(definition: str) -> Draft202012Validator:
    """Validator for one Grant ``$defs`` block.

    The full Grant document is not claimed as conformant in Issue #78: the
    ``attempt`` block belongs to Issue #79 and ``issued_by`` needs a real
    service-identity registry.  The serializer's derivable blocks are still
    checked individually against the frozen schema.
    """
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": GRANT_SCHEMA["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    return Draft202012Validator(schema, format_checker=FormatChecker())


def assert_grant_definition(definition: str, value: Any) -> None:
    errors = [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"{error.message}"
        for error in grant_definition_validator(definition).iter_errors(value)
    ]
    assert not errors, (
        f"grant block {definition!r} does not match execution-grant.schema.json:\n"
        + "\n".join(errors)
    )
