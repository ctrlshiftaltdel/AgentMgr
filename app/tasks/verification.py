"""Deterministic completion verification and payload canonicalisation.

A task only reaches ``succeeded`` through ``verifying`` **and** a structured
verification result.  Nothing here trusts an agent's "I finished": the caller
submits evidence records, and this module re-computes each completion criterion
against the verifier named by the goal.  Free text is never a completion
definition.

The payload/confirmation hashes reuse the RFC 8785 JCS implementation that
already ships with the governance contracts
(:func:`app.governance.policies.canonical_json` /
:func:`app.governance.policies.payload_hash`) instead of adding a second
canonicalizer.  The frozen test vectors are covered by
:mod:`app.tests.tasks.test_verification`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.governance.policies import canonical_json, payload_hash, sha256_digest
from app.tasks.errors import TaskError

__all__ = [
    "ALLOWED_EVIDENCE_SOURCES",
    "ALLOWED_VERIFICATION_RESULTS",
    "EVIDENCE_REF_PATTERN",
    "CriterionResult",
    "EvidenceRecord",
    "VerificationOutcome",
    "canonicalize",
    "hash_payload",
    "validate_goal",
    "validate_result_refs",
    "verify_completion",
]

CRITERION_ID_PATTERN = re.compile(r"^crit_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
VERIFIER_ID_PATTERN = re.compile(r"^ver_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
RESOURCE_REF_PATTERN = re.compile(r"^res_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
EVIDENCE_REF_PATTERN = re.compile(r"^evidence:[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
RESULT_REF_PATTERN = re.compile(r"^result_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")

CRITERION_TYPES = frozenset(
    {"business_state_equals", "resource_exists", "artifact_hash", "verifier_result"}
)
ALLOWED_VERIFICATION_RESULTS = frozenset(
    {"pending", "passed", "partial", "failed", "inconclusive"}
)

#: Evidence must come from an independent, deterministic source.  A runtime
#: report or a model statement is an *input*, not a verification result.
ALLOWED_EVIDENCE_SOURCES = frozenset(
    {"verifier", "business_state", "artifact_store", "connector", "human_verifier"}
)
_SELF_REPORT_SOURCES = frozenset(
    {"agent", "agent_instance", "model", "runtime", "self_report", "executor"}
)


def canonicalize(value: Any) -> str:
    """Return the RFC 8785 JCS text for ``value``."""
    return canonical_json(value)


def hash_payload(value: Any) -> str:
    """Return ``sha256:<hex>`` over the JCS form of ``value``."""
    return payload_hash(value)


def _require(condition: bool, code: str, message: str, **context: Any) -> None:
    if not condition:
        raise TaskError(code, message, context=context or None)


def validate_goal(goal: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and normalise ``goal`` against the frozen criterion shape.

    Returns a JSON-safe copy.  Missing summary/criteria or a free-text criterion
    is a deterministic rejection: a task with no machine-judgeable completion
    definition can never be queued (F31).
    """
    _require(isinstance(goal, Mapping), "goal_missing", "goal must be an object")
    summary = goal.get("summary")
    _require(
        isinstance(summary, str) and 1 <= len(summary) <= 500,
        "goal_missing",
        "goal.summary is required (1..500 characters)",
        field="summary",
    )
    criteria = goal.get("completion_criteria")
    _require(
        isinstance(criteria, Sequence)
        and not isinstance(criteria, (str, bytes))
        and 1 <= len(criteria) <= 20,
        "task_completion_criteria_missing",
        "goal.completion_criteria must contain 1..20 structured criteria",
        field="completion_criteria",
    )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in criteria:
        normalized.append(_validate_criterion(raw, seen))
    out = dict(goal)
    out["completion_criteria"] = normalized
    for key in ("required_inputs", "out_of_scope"):
        if key in out:
            value = out[key]
            _require(
                isinstance(value, list)
                and all(isinstance(item, str) and item for item in value),
                "required_input_missing",
                f"goal.{key} must be a list of non-empty strings",
                field=key,
            )
    return out


def _validate_criterion(
    raw: Any, seen: set[str]
) -> dict[str, Any]:
    _require(
        isinstance(raw, Mapping),
        "task_completion_criteria_missing",
        "each completion criterion must be an object",
    )
    criterion_id = raw.get("criterion_id")
    _require(
        isinstance(criterion_id, str) and bool(CRITERION_ID_PATTERN.fullmatch(criterion_id)),
        "task_completion_criteria_missing",
        "criterion_id must match crit_*",
        field="criterion_id",
    )
    _require(
        criterion_id not in seen,
        "task_completion_criteria_missing",
        "duplicate criterion_id",
        criterion_id=criterion_id,
    )
    seen.add(criterion_id)

    criterion_type = raw.get("type")
    _require(
        criterion_type in CRITERION_TYPES,
        "task_completion_criteria_missing",
        "criterion type must be one of the four machine-judgeable types",
        field="type",
    )
    verifier = raw.get("verifier")
    _require(
        isinstance(verifier, Mapping)
        and isinstance(verifier.get("verifier_id"), str)
        and bool(VERIFIER_ID_PATTERN.fullmatch(verifier["verifier_id"]))
        and isinstance(verifier.get("version"), str)
        and bool(SEMVER_PATTERN.fullmatch(verifier["version"])),
        "task_completion_criteria_missing",
        "criterion.verifier requires verifier_id (ver_*) and version",
        field="verifier",
    )
    resource_ref = raw.get("resource_ref")
    if resource_ref is not None:
        _require(
            isinstance(resource_ref, str)
            and bool(RESOURCE_REF_PATTERN.fullmatch(resource_ref)),
            "task_completion_criteria_missing",
            "resource_ref must match res_*",
            field="resource_ref",
        )
    expected = raw.get("expected")
    _require(
        isinstance(expected, str) and 1 <= len(expected) <= 200,
        "task_completion_criteria_missing",
        "criterion.expected is required",
        field="expected",
    )
    evidence_ref = raw.get("evidence_ref")
    if evidence_ref is not None:
        _require(
            isinstance(evidence_ref, str)
            and bool(EVIDENCE_REF_PATTERN.fullmatch(evidence_ref)),
            "task_completion_criteria_missing",
            "evidence_ref must match evidence:*",
            field="evidence_ref",
        )
    evidence_hash = raw.get("evidence_hash")
    if evidence_hash is not None:
        _require(
            isinstance(evidence_hash, str)
            and bool(SHA256_PATTERN.fullmatch(evidence_hash)),
            "task_completion_criteria_missing",
            "evidence_hash must be a sha256:<hex> value",
            field="evidence_hash",
        )

    if criterion_type == "artifact_hash":
        _require(
            isinstance(resource_ref, str)
            and isinstance(expected, str)
            and bool(SHA256_PATTERN.fullmatch(expected))
            and isinstance(evidence_ref, str)
            and isinstance(evidence_hash, str)
            and bool(SHA256_PATTERN.fullmatch(evidence_hash)),
            "task_completion_criteria_missing",
            "artifact_hash requires resource_ref, sha256 expected, evidence_ref "
            "and evidence_hash",
            field="artifact_hash",
        )
    elif criterion_type == "resource_exists":
        _require(
            isinstance(resource_ref, str)
            and expected == "exists"
            and isinstance(evidence_ref, str),
            "task_completion_criteria_missing",
            "resource_exists requires resource_ref, expected=exists and "
            "evidence_ref",
            field="resource_exists",
        )
    elif criterion_type == "business_state_equals":
        _require(
            isinstance(resource_ref, str) and isinstance(evidence_ref, str),
            "task_completion_criteria_missing",
            "business_state_equals requires resource_ref and evidence_ref",
            field="business_state_equals",
        )
    elif criterion_type == "verifier_result":
        _require(
            expected == "passed" and isinstance(evidence_ref, str),
            "task_completion_criteria_missing",
            "verifier_result requires expected=passed and evidence_ref",
            field="verifier_result",
        )
    return dict(raw)


@dataclass(frozen=True)
class EvidenceRecord:
    """One independently produced observation for one criterion."""

    criterion_id: str
    verifier_id: str
    verifier_version: str
    evidence_ref: str
    observed: str | None = None
    evidence_hash: str | None = None
    source: str = "verifier"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> EvidenceRecord:
        return cls(
            criterion_id=str(raw.get("criterion_id", "")),
            verifier_id=str(raw.get("verifier_id", "")),
            verifier_version=str(raw.get("verifier_version", "")),
            evidence_ref=str(raw.get("evidence_ref", "")),
            observed=raw.get("observed"),
            evidence_hash=raw.get("evidence_hash"),
            source=str(raw.get("source", "verifier")),
        )


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    passed: bool
    reason_code: str
    detail: str = ""


@dataclass(frozen=True)
class VerificationOutcome:
    result: str
    results: tuple[CriterionResult, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    reason_code: str = "evidence_missing"

    @property
    def passed_criteria(self) -> tuple[str, ...]:
        return tuple(r.criterion_id for r in self.results if r.passed)

    @property
    def failed_criteria(self) -> tuple[str, ...]:
        return tuple(r.criterion_id for r in self.results if not r.passed)

    @property
    def all_passed(self) -> bool:
        return self.result == "passed"


def _validate_evidence(evidence: EvidenceRecord) -> None:
    _require(
        evidence.source not in _SELF_REPORT_SOURCES,
        "task_verification_self_report",
        "an agent/runtime self-report is not completion evidence",
        source=evidence.source,
    )
    _require(
        evidence.source in ALLOWED_EVIDENCE_SOURCES,
        "task_verification_self_report",
        "evidence source is not an accepted independent verifier",
        source=evidence.source,
    )
    _require(
        bool(VERIFIER_ID_PATTERN.fullmatch(evidence.verifier_id)),
        "evidence_missing",
        "evidence must name the verifier that produced it",
        field="verifier_id",
    )
    _require(
        bool(SEMVER_PATTERN.fullmatch(evidence.verifier_version)),
        "evidence_missing",
        "evidence verifier_version is required",
        field="verifier_version",
    )
    _require(
        bool(EVIDENCE_REF_PATTERN.fullmatch(evidence.evidence_ref)),
        "evidence_missing",
        "evidence_ref must match evidence:*",
        field="evidence_ref",
    )


def _check_one(
    criterion: Mapping[str, Any], evidence: EvidenceRecord | None
) -> CriterionResult:
    criterion_id = str(criterion["criterion_id"])
    if evidence is None:
        return CriterionResult(
            criterion_id, False, "evidence_missing", "no evidence for criterion"
        )
    try:
        _validate_evidence(evidence)
    except TaskError as exc:
        return CriterionResult(criterion_id, False, exc.reason_code, str(exc))

    verifier = criterion["verifier"]
    if (
        evidence.verifier_id != verifier["verifier_id"]
        or evidence.verifier_version != verifier["version"]
    ):
        return CriterionResult(
            criterion_id,
            False,
            "task_completion_evidence_incomplete",
            "evidence was produced by a different verifier",
        )
    if evidence.evidence_ref != criterion.get("evidence_ref"):
        return CriterionResult(
            criterion_id,
            False,
            "task_completion_evidence_incomplete",
            "evidence_ref does not match the criterion",
        )
    expected_hash = criterion.get("evidence_hash")
    if expected_hash is not None and evidence.evidence_hash != expected_hash:
        return CriterionResult(
            criterion_id,
            False,
            "task_completion_evidence_incomplete",
            "evidence hash does not match the criterion",
        )

    criterion_type = criterion["type"]
    expected = criterion["expected"]
    observed = evidence.observed
    if criterion_type == "artifact_hash":
        passed = observed == expected
    elif criterion_type == "business_state_equals":
        passed = observed == expected
    elif criterion_type == "resource_exists":
        passed = observed in {"exists", "true", "True", True} and expected == "exists"
    else:  # verifier_result
        passed = observed in {"passed", "true", "True", True} and expected == "passed"
    if not passed:
        return CriterionResult(
            criterion_id,
            False,
            "task_completion_evidence_incomplete",
            "observed value does not satisfy the criterion",
        )
    return CriterionResult(criterion_id, True, "evidence_missing", "")


def verify_completion(
    goal: Mapping[str, Any],
    evidence: Iterable[EvidenceRecord | Mapping[str, Any]],
    *,
    allow_partial: bool = False,
) -> VerificationOutcome:
    """Recompute every completion criterion deterministically.

    * ``passed``      -- every criterion matched its named verifier and evidence;
    * ``partial``     -- a subset passed and policy explicitly allows partial
                         completion;
    * ``failed``      -- a mismatch was observed;
    * ``inconclusive``-- evidence is missing/self-reported or the source is
                         unavailable.

    The function never mutates the session and never accepts a bare statement.
    """
    normalized = validate_goal(goal)
    records: dict[str, EvidenceRecord] = {}
    for item in evidence:
        record = (
            item if isinstance(item, EvidenceRecord) else EvidenceRecord.from_mapping(item)
        )
        if record.criterion_id in records:
            raise TaskError(
                "task_completion_evidence_incomplete",
                "more than one evidence record for a criterion",
                context={"criterion_id": record.criterion_id},
            )
        records[record.criterion_id] = record

    # Self-report anywhere in the submission is refused outright, even if the
    # criterion itself would otherwise have failed.
    for record in records.values():
        if record.source in _SELF_REPORT_SOURCES:
            raise TaskError(
                "task_verification_self_report",
                "agent/runtime self-report cannot satisfy completion criteria",
                context={"criterion_id": record.criterion_id, "source": record.source},
            )

    results = tuple(
        _check_one(criterion, records.get(criterion["criterion_id"]))
        for criterion in normalized["completion_criteria"]
    )
    evidence_refs = tuple(
        sorted(
            {
                record.evidence_ref
                for record in records.values()
                if EVIDENCE_REF_PATTERN.fullmatch(record.evidence_ref)
            }
        )
    )
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    if len(passed) == len(results):
        return VerificationOutcome(
            "passed", results, evidence_refs, reason_code="evidence_missing"
        )
    if allow_partial and passed:
        return VerificationOutcome(
            "partial", results, evidence_refs, reason_code="evidence_missing"
        )
    if any(r.reason_code == "evidence_missing" for r in failed):
        return VerificationOutcome(
            "inconclusive", results, evidence_refs, reason_code="evidence_missing"
        )
    if not passed:
        return VerificationOutcome(
            "failed", results, evidence_refs, reason_code="task_completion_evidence_incomplete"
        )
    return VerificationOutcome(
        "failed", results, evidence_refs, reason_code="task_completion_evidence_incomplete"
    )


def validate_result_refs(
    result_ref: str | None,
    evidence_refs: Sequence[str] | None,
    *,
    require_result: bool = True,
) -> tuple[str | None, tuple[str, ...]]:
    """Validate result/evidence references before a task leaves ``running``.

    ``verifying`` requires a result reference and at least one evidence
    reference; ``failed``/``cancelled``/``timed_out`` require evidence too (the
    failure/stop evidence), but not necessarily a result.
    """
    if require_result:
        _require(
            isinstance(result_ref, str)
            and bool(RESULT_REF_PATTERN.fullmatch(result_ref)),
            "task_result_missing",
            "a structured result_ref (result_*) is required",
            field="result_ref",
        )
    elif result_ref is not None:
        _require(
            isinstance(result_ref, str) and bool(RESULT_REF_PATTERN.fullmatch(result_ref)),
            "task_result_missing",
            "result_ref must match result_*",
            field="result_ref",
        )
    refs = tuple(evidence_refs or ())
    _require(
        bool(refs),
        "evidence_missing",
        "at least one structured evidence reference is required",
        field="evidence_refs",
    )
    _require(
        len(refs) <= 100 and len(set(refs)) == len(refs),
        "evidence_missing",
        "evidence_refs must be unique and at most 100",
        field="evidence_refs",
    )
    for ref in refs:
        _require(
            isinstance(ref, str) and bool(EVIDENCE_REF_PATTERN.fullmatch(ref)),
            "evidence_missing",
            "each evidence reference must match evidence:*",
            field="evidence_refs",
        )
    return result_ref, refs
