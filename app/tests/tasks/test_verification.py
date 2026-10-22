"""Deterministic completion verification: no free text, no self-report."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.governance.principal import resolve_employee_principal
from app.tasks import verification
from app.tasks.errors import TaskError
from app.tasks.service import complete_task
from app.tasks.verification import EvidenceRecord
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.helpers import (
    CRITERION_ID,
    EVIDENCE_REF,
    VERIFIER_ID,
    VERIFIER_VERSION,
    evidence,
    goal,
    move_to_verifying,
    submit_read,
)


def test_jcs_hash_matches_the_frozen_test_vectors() -> None:
    vector_1 = {
        "amount": "100.00",
        "currency": "CNY",
        "payee": "res_vendor_0001",
        "purpose": "expense.submit",
    }
    assert verification.hash_payload(vector_1) == (
        "sha256:f0470abd268f709bf23a3d2d93c51e801fb59c997ce072d60f8fa29e9d66a9fa"
    )
    vector_2 = {
        "confirmation_id": "conf_expense_0001",
        "payload": {
            "amount": 100.5,
            "note": "café",
            "resource_ids": ["res_expense_0001", "res_expense_0002"],
        },
        "single_use": True,
    }
    assert verification.hash_payload(vector_2) == (
        "sha256:8f624db9d2c9cea16272730704c4e0e5d22ed37d43afecf4115860c877fda80d"
    )


def test_goal_requires_structured_criteria() -> None:
    with pytest.raises(TaskError) as excinfo:
        verification.validate_goal({"summary": "done", "completion_criteria": []})
    assert excinfo.value.reason_code == "task_completion_criteria_missing"

    with pytest.raises(TaskError) as excinfo:
        verification.validate_goal(
            {
                "summary": "done",
                "completion_criteria": [
                    {
                        "criterion_id": "crit_free_text_0001",
                        "type": "free_text",
                        "verifier": {
                            "verifier_id": VERIFIER_ID,
                            "version": VERIFIER_VERSION,
                        },
                        "resource_ref": "res_expense_0001",
                        "expected": "looks fine",
                        "evidence_ref": EVIDENCE_REF,
                        "evidence_hash": None,
                    }
                ],
            }
        )
    assert excinfo.value.reason_code == "task_completion_criteria_missing"


def test_verify_completion_outcomes() -> None:
    passed = verification.verify_completion(goal(), [evidence()])
    assert passed.result == "passed"
    assert passed.all_passed
    assert passed.passed_criteria == (CRITERION_ID,)

    mismatch = verification.verify_completion(goal(), [evidence(observed="draft")])
    assert mismatch.result == "failed"
    assert mismatch.failed_criteria == (CRITERION_ID,)

    missing = verification.verify_completion(goal(), [])
    assert missing.result == "inconclusive"

    partial = verification.verify_completion(
        goal(), [evidence(observed="draft")], allow_partial=True
    )
    assert partial.result in {"failed", "inconclusive"}

    wrong_verifier = verification.verify_completion(
        goal(), [evidence(verifier_id="ver_other_0001")]
    )
    assert wrong_verifier.result in {"failed", "inconclusive"}


def test_agent_self_report_is_not_evidence() -> None:
    with pytest.raises(TaskError) as excinfo:
        verification.verify_completion(goal(), [evidence(source="agent")])
    assert excinfo.value.reason_code == "task_verification_self_report"

    with pytest.raises(TaskError) as excinfo:
        verification.verify_completion(goal(), [evidence(source="runtime")])
    assert excinfo.value.reason_code == "task_verification_self_report"


def test_completion_requires_verifying_and_passes_with_real_evidence(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    move_to_verifying(session, world, task)
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref="result_verify_0001",
        evidence_refs=[EVIDENCE_REF],
        evidence=[evidence()],
    )
    session.commit()
    assert task.state == "succeeded"
    assert task.result["verification"]["result"] == "passed"
    assert task.result["all_criteria_passed"] is True
    assert task.result["verification"]["verifier_id"] == VERIFIER_ID


def test_mismatched_evidence_fails_verification(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    move_to_verifying(session, world, task)
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref="result_verify_0002",
        evidence_refs=[EVIDENCE_REF],
        evidence=[evidence(observed="still_draft")],
    )
    session.commit()
    assert task.state == "failed"
    assert task.result["verification"]["result"] == "failed"
    assert task.result["failed_criteria"] == [CRITERION_ID]


def test_missing_evidence_is_inconclusive_and_goes_to_human(
    session: Session, world: TaskWorld
) -> None:
    task = submit_read(session, world)
    session.commit()
    move_to_verifying(session, world, task)
    complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref="result_verify_0003",
        evidence_refs=["evidence:claimed_without_verifier_0001"],
        evidence=[],
    )
    session.commit()
    assert task.state == "needs_human"
    assert task.result["verification"]["result"] == "inconclusive"


def test_agent_cannot_certify_its_own_completion(
    session: Session, world: TaskWorld
) -> None:
    agent = resolve_employee_principal(
        session,
        employee_id=world.actors.executor.employee_id,
        principal_type="agent_instance",
    )
    task = submit_read(session, world, executor=agent)
    session.commit()
    move_to_verifying(session, world, task)

    with pytest.raises(TaskError) as excinfo:
        complete_task(
            session,
            task_id=task.task_id,
            actor=agent,
            result_ref="result_agent_0001",
            evidence_refs=[EVIDENCE_REF],
            evidence=[evidence()],
        )
    assert excinfo.value.reason_code == "task_verification_self_report"


def test_artifact_hash_criterion_requires_the_real_hash() -> None:
    artifact = "sha256:" + "a" * 64
    goal_doc = goal(
        criterion_type="artifact_hash",
        expected=artifact,
        evidence_hash=artifact,
    )
    good = EvidenceRecord(
        criterion_id=CRITERION_ID,
        verifier_id=VERIFIER_ID,
        verifier_version=VERIFIER_VERSION,
        evidence_ref=EVIDENCE_REF,
        observed=artifact,
        evidence_hash=artifact,
        source="artifact_store",
    )
    assert verification.verify_completion(goal_doc, [good]).result == "passed"

    bad = EvidenceRecord(
        criterion_id=CRITERION_ID,
        verifier_id=VERIFIER_ID,
        verifier_version=VERIFIER_VERSION,
        evidence_ref=EVIDENCE_REF,
        observed="sha256:" + "b" * 64,
        evidence_hash="sha256:" + "b" * 64,
        source="artifact_store",
    )
    assert verification.verify_completion(goal_doc, [bad]).result == "failed"
