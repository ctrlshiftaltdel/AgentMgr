"""Managed dispatcher: preflight, submit, binding and status/result mapping.

These tests use the real PostgreSQL task fixtures and the scripted
:class:`FakeRuntimeAdapter`.  The fake is test-only evidence; nothing here
claims that a real runtime supports Managed Execution.

Scope of this file in Task 6: the accepted submit path binds ``rtask_*``
through ``start_attempt`` and the same pass drives the bound run through
``get_status``/``get_result`` under the held lease/fencing token.  ``running``
is recorded with a hashed start-evidence ref, a completed run with a
core-compatible ``result_*``/``evidence:*`` pair reaches ``verifying`` only,
confirmed failures are recorded, and anything the core cannot map honestly
fails closed to ``unknown`` with the Grant revoked.  The dispatcher never calls
``complete_task``.  Lease recovery and core-driven cancellation arrive in
Task 7.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.errors import PrincipalResolutionError
from app.governance.grants import revoke_grant
from app.governance.models import ExecutionGrant
from app.governance.policies import canonical_json
from app.governance.principal import (
    GovernancePrincipal,
    resolve_service_principal,
)
from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    Capability,
    CapabilityStatus,
    HealthStatus,
    LookupOutcome,
    LookupResult,
    ResultOutcome,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeGrant,
    RuntimeResult,
    RuntimeState,
    RuntimeType,
    StatusResult,
    SubmitOutcome,
    SubmitResult,
    VersionSource,
)
from app.tasks import execution as task_execution
from app.tasks import service as task_service
from app.tasks.errors import TaskError
from app.tasks.execution import (
    DECISION_BINDING_MISMATCH,
    GRANT_DOCUMENT_INVALID,
    MAX_RUNTIME_PROMPT_BYTES,
    MAX_STATUS_POLLS_PER_PASS,
    RUNTIME_BIND_FAILED,
    RUNTIME_EXECUTION_FAILED,
    RUNTIME_EXECUTION_LOST,
    RUNTIME_EXECUTION_TIMED_OUT,
    RUNTIME_FACT_REFUSED,
    RUNTIME_PROMPT_INVALID,
    RUNTIME_PROMPT_TOO_LARGE,
    RUNTIME_RESULT_UNREADABLE,
    RUNTIME_RESULT_UNUSABLE,
    RUNTIME_STATUS_UNREADABLE,
    RUNTIME_STOP_UNCONFIRMED,
    RUNTIME_SUBMIT_HANDLE_INVALID,
    RUNTIME_WAITING_INPUT,
    DispatcherConfig,
    DispatcherResult,
    RuntimeDispatcher,
    RuntimePromptTooLarge,
    build_runtime_idempotency_key,
    build_runtime_prompt,
)
from app.tasks.models import Task, TaskAttempt, TaskEvent, TaskLease
from app.tests.governance.helpers import make_service_identity
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.fake_runtime import FakeRuntimeAdapter
from app.tests.tasks.helpers import (
    evidence,
    submit_managed,
    submit_read,
    utcnow,
)

WORKER_ID = "dispatcher-test-1"


def _service_principal(
    session: Session, world: TaskWorld
) -> GovernancePrincipal:
    """Resolve a real service identity; the dispatcher must never act as a person."""
    identity = make_service_identity(session, owner=world.org.admin)
    return resolve_service_principal(session, identity.id)


def _dispatcher(
    session: Session,
    world: TaskWorld,
    *,
    adapter: FakeRuntimeAdapter | None = None,
    principal: GovernancePrincipal | None = None,
) -> RuntimeDispatcher:
    return RuntimeDispatcher(
        FakeRuntimeAdapter() if adapter is None else adapter,
        DispatcherConfig(worker_id=WORKER_ID),
        service_principal=(
            _service_principal(session, world) if principal is None else principal
        ),
        session=session,
    )


def _leases(session: Session, task: Task) -> list[TaskLease]:
    return list(
        session.scalars(
            select(TaskLease)
            .where(TaskLease.task_id == task.task_id)
            .order_by(TaskLease.id)
        )
    )


def _assert_lease_released(session: Session, task: Task) -> None:
    leases = _leases(session, task)
    assert leases, "the dispatcher must claim before it can release"
    assert all(lease.released_at is not None for lease in leases)


def _grant(session: Session, task: Task) -> ExecutionGrant:
    grant = session.scalar(
        select(ExecutionGrant).where(
            ExecutionGrant.grant_id == task.grant_id
        )
    )
    assert grant is not None
    return grant


def _attempt(session: Session, task: Task) -> TaskAttempt:
    attempt = session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.task_id)
    )
    assert attempt is not None
    return attempt


def _runtime_grant() -> RuntimeGrant:
    return RuntimeGrant(
        grant_id="grnt_fake_0001",
        task_id="task_fake_0001",
        task_revision=1,
        execution_mode="managed_execution",
        capabilities=("expense.read@1.0.0",),
        resource_scope={"scope_type": "expense_claim"},
        expires_at=utcnow() + timedelta(hours=1),
    )


def _assert_grant_document_rejected(
    session: Session,
    task: Task,
    result: DispatcherResult,
    adapter: FakeRuntimeAdapter,
    *,
    reason: str,
) -> None:
    assert result.outcome == "unavailable"
    assert result.task_id == task.task_id
    assert result.detail == f"{GRANT_DOCUMENT_INVALID}:{reason}"
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []
    assert task.state == "failed"
    assert task.result["outcome"] == "unavailable"
    assert task.result["error_code"] == GRANT_DOCUMENT_INVALID
    assert task.result["no_side_effects"] is True
    assert task.grant_status == "revoked"
    _assert_lease_released(session, task)


def _capabilities(
    *,
    submit_status: CapabilityStatus = CapabilityStatus.VERIFIED,
    status_status: CapabilityStatus = CapabilityStatus.VERIFIED,
) -> RuntimeCapabilities:
    """A minimal snapshot so one capability can be unverified in isolation."""

    def capability(name: RuntimeCapability, status: CapabilityStatus) -> Capability:
        return Capability(
            name=name.value,
            version="1.0.0",
            status=status,
            evidence_ref=None,
        )

    return RuntimeCapabilities(
        adapter_contract_version=ADAPTER_CONTRACT_VERSION,
        boundary=BOUNDARY,
        runtime_id="rt_fake_0001",
        runtime_type=RuntimeType.OTHER,
        runtime_version=None,
        version_source=VersionSource.UNKNOWN,
        capabilities=(
            capability(RuntimeCapability.TASK_SUBMIT, submit_status),
            capability(RuntimeCapability.TASK_STATUS, status_status),
        ),
        observed_at=utcnow(),
    )


# -- runtime fact builders ----------------------------------------------------

FAKE_RESULT_REF = "result_fake_0001"
FAKE_EVIDENCE_REF = "evidence:fake_state_0001"


def _status(
    state: RuntimeState,
    *,
    stop_confirmed: bool = False,
    runtime_task_id: str = "rtask_fake_0001",
    observed_at: datetime | None = None,
) -> StatusResult:
    """One scripted adapter status fact."""
    return StatusResult(
        runtime_task_id=runtime_task_id,
        state=state,
        external_state=state.value,
        observed_at=observed_at or utcnow(),
        stop_confirmed=stop_confirmed,
    )


def _result(
    *,
    outcome: ResultOutcome = ResultOutcome.SUCCEEDED,
    result_ref: str | None = FAKE_RESULT_REF,
    evidence_refs: tuple[str, ...] = (FAKE_EVIDENCE_REF,),
    stop_confirmed: bool = False,
) -> RuntimeResult:
    """One scripted adapter result fact."""
    return RuntimeResult(
        outcome=outcome,
        result_ref=result_ref,
        summary="synthetic fake runtime result",
        structured_payload={"state": "submitted"},
        artifacts=(),
        evidence_refs=evidence_refs,
        verification_hints=(),
        stop_confirmed=stop_confirmed,
        completed_at=utcnow(),
    )


def _task_events(session: Session, task: Task) -> list[TaskEvent]:
    return list(
        session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.task_id)
            .order_by(TaskEvent.id)
        )
    )


def _event_types(session: Session, task: Task) -> list[str]:
    return [event.event_type for event in _task_events(session, task)]


# -- idle and preflight -------------------------------------------------------


def test_run_once_returns_idle_without_a_managed_task(
    session: Session, world: TaskWorld
) -> None:
    submit_read(session, world, key="idem_dispatch_idle_advisory")
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result == DispatcherResult(outcome="idle")
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []
    assert session.scalars(select(TaskLease)).all() == []


def test_accepted_submit_binds_runtime_handle(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    grant = _grant(session, task)
    expected_key = build_runtime_idempotency_key(task, grant, 1)
    expected_prompt = build_runtime_prompt(task)
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(
            outcome=SubmitOutcome.ACCEPTED,
            runtime_task_id="rtask_fake_0001",
        )
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "accepted"
    assert result.task_id == task.task_id
    assert result.runtime_task_id == "rtask_fake_0001"
    assert task.state == "dispatched"
    assert task.runtime_task_id == "rtask_fake_0001"
    # Managed execution never carries an ``exec_*`` handle.
    assert task.execution_ref is None
    assert task.attempt == 1
    assert len(adapter.submit_calls) == 1
    submitted_task_id, submitted_key, submitted_grant_id, submitted_prompt = (
        adapter.submit_calls[0]
    )
    assert submitted_task_id == task.task_id
    assert submitted_grant_id == task.grant_id
    assert submitted_key == expected_key
    assert submitted_prompt == expected_prompt
    attempt = _attempt(session, task)
    assert attempt.attempt_number == 1
    assert attempt.runtime_ref == "rtask_fake_0001"
    assert attempt.execution_ref is None
    assert attempt.idempotency_key == expected_key
    # The same pass observes the bound run.  A runtime that stays ``accepted``
    # advances nothing, and the bounded poll loop returns instead of spinning.
    assert len(adapter.status_calls) == MAX_STATUS_POLLS_PER_PASS
    assert set(adapter.status_calls) == {
        ("rtask_fake_0001", task.task_id)
    }
    assert adapter.result_calls == []
    assert "outcome" not in task.result
    # The lease stays held so the status/result work can keep writing under
    # the same fencing token.
    assert _leases(session, task)[-1].released_at is None


# -- key and prompt -----------------------------------------------------------


def test_runtime_key_is_stable_per_attempt_and_changes_with_grant() -> None:
    task = SimpleNamespace(task_id="task_key_fixture_0001")
    grant = SimpleNamespace(grant_id="grnt_key_fixture_0001")

    first = build_runtime_idempotency_key(task, grant, 1)
    assert first == build_runtime_idempotency_key(task, grant, 1)
    assert first.startswith("idem_")
    assert len(first) <= 64
    # A retry of the same attempt reuses the key; a new attempt does not.
    assert first != build_runtime_idempotency_key(task, grant, 2)
    # A fresh Grant changes the key even for the same attempt number.
    assert first != build_runtime_idempotency_key(
        task, SimpleNamespace(grant_id="grnt_key_fixture_0002"), 1
    )
    # A different task never shares a runtime key.
    assert first != build_runtime_idempotency_key(
        SimpleNamespace(task_id="task_key_fixture_0002"), grant, 1
    )


def test_same_runtime_key_reuses_a_single_runtime_task() -> None:
    adapter = FakeRuntimeAdapter()
    grant = _runtime_grant()
    key = build_runtime_idempotency_key(
        SimpleNamespace(task_id="task_fake_0001"),
        SimpleNamespace(grant_id="grnt_fake_0001"),
        1,
    )

    first = adapter.submit_task("task_fake_0001", "{}", key, grant)
    second = adapter.submit_task("task_fake_0001", "{}", key, grant)

    assert first.outcome is SubmitOutcome.ACCEPTED
    assert second.outcome is SubmitOutcome.ACCEPTED
    assert second.runtime_task_id == first.runtime_task_id
    assert second.replayed is True
    assert adapter.created_runtime_task_ids == [first.runtime_task_id]
    assert [call[1] for call in adapter.submit_calls] == [key, key]


def test_runtime_prompt_is_canonical_and_bounded() -> None:
    task = SimpleNamespace(
        goal={
            "summary": "Read one synthetic managed expense",
            "completion_criteria": [
                {"criterion_id": "crit_fixture_0001", "expected": "submitted"}
            ],
        },
        payload={"expense_id": "exp_fixture_0001", "amount": "12.00"},
        principal_id="prin_should_never_leak",
        grant_id="grnt_should_never_leak",
    )

    prompt = build_runtime_prompt(task)

    assert prompt == canonical_json(
        {"goal": task.goal, "payload": task.payload}
    )
    assert build_runtime_prompt(task) == prompt
    assert len(prompt.encode("utf-8")) <= MAX_RUNTIME_PROMPT_BYTES
    # Identity/authority columns are not part of the prompt.
    assert "prin_should_never_leak" not in prompt
    assert "grnt_should_never_leak" not in prompt


def test_runtime_prompt_never_includes_credentials() -> None:
    task = SimpleNamespace(
        goal={"summary": "Read one synthetic managed expense"},
        payload={
            "expense_id": "exp_fixture_0001",
            "api_key": "sk-live-do-not-leak",
            "nested": {"client_secret": "nested-secret", "amount": "12.00"},
            "items": [{"access_token": "token-secret", "sku": "SKU-1"}],
        },
    )

    prompt = build_runtime_prompt(task)

    for leaked in (
        "sk-live-do-not-leak",
        "nested-secret",
        "token-secret",
        "api_key",
        "client_secret",
        "access_token",
    ):
        assert leaked not in prompt
    # Non-credential governed fields survive the redaction.
    assert "exp_fixture_0001" in prompt
    assert "SKU-1" in prompt


def test_runtime_prompt_refuses_oversize_governed_input() -> None:
    task = SimpleNamespace(
        goal={"summary": "oversize fixture"},
        payload={"blob": "x" * MAX_RUNTIME_PROMPT_BYTES},
    )

    with pytest.raises(RuntimePromptTooLarge):
        build_runtime_prompt(task)


# -- capabilities -------------------------------------------------------------


def test_run_once_escalates_unverified_capability(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(capabilities_verified=False)

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unsupported"
    assert result.task_id == task.task_id
    assert result.detail.startswith("runtime_capability_unverified")
    assert "task.submit" in result.detail
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"
    assert task.result["outcome"] == "unsupported"
    assert adapter.submit_calls == []
    # The capability gap is found before any health probe or submission.
    assert adapter.health_calls == 0
    _assert_lease_released(session, task)


@pytest.mark.parametrize(
    ("submit_status", "status_status"),
    [
        (CapabilityStatus.UNVERIFIED, CapabilityStatus.VERIFIED),
        (CapabilityStatus.VERIFIED, CapabilityStatus.UNVERIFIED),
        (CapabilityStatus.PARTIALLY_VERIFIED, CapabilityStatus.VERIFIED),
    ],
    ids=["submit-unverified", "status-unverified", "submit-partial"],
)
def test_run_once_requires_both_submit_and_status_verified(
    session: Session,
    world: TaskWorld,
    submit_status: CapabilityStatus,
    status_status: CapabilityStatus,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        capabilities=_capabilities(
            submit_status=submit_status, status_status=status_status
        )
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unsupported"
    assert task.state == "needs_human"
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- health -------------------------------------------------------------------


@pytest.mark.parametrize(
    "health",
    [HealthStatus.UNAVAILABLE, HealthStatus.DEGRADED],
    ids=["unavailable", "degraded"],
)
def test_unhealthy_runtime_releases_lease_and_keeps_queued(
    session: Session, world: TaskWorld, health: HealthStatus
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(health=health)

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "adapter_unavailable"
    assert result.task_id == task.task_id
    assert result.detail == health.value
    # The lease is released and the task stays claimable for the next pass.
    assert task.state == "queued"
    assert task.attempt == 0
    # No dispatch outcome was recorded; only the submit-time capability check.
    assert "outcome" not in task.result
    assert task.result["runtime_capabilities_verified"] is True
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- grant rejection ----------------------------------------------------------


def test_revoked_grant_never_reaches_the_adapter(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    revoke_grant(
        session,
        grant_id=task.grant_id,
        reason_code="policy_changed",
        principal=world.actors.executor,
    )
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unavailable"
    assert result.task_id == task.task_id
    assert result.detail == "grant_not_active"
    assert task.state == "failed"
    assert task.result["outcome"] == "unavailable"
    assert task.result["error_code"] == "grant_not_active"
    assert task.grant_status == "revoked"
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


def test_expired_grant_never_reaches_the_adapter(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once(
        now=utcnow() + timedelta(hours=1)
    )

    assert result.outcome == "unavailable"
    assert result.detail == "grant_expired"
    assert task.state == "failed"
    assert task.result["outcome"] == "unavailable"
    assert task.grant_status == "revoked"
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- decision binding ---------------------------------------------------------


def test_grant_decision_binding_mismatch_never_submits(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    grant = _grant(session, task)
    # Keep the row structurally valid but no longer the persisted decision's
    # hash.  The projection alone cannot see this; the dispatcher must.
    grant.decision_hash = "sha256:" + "0" * 64
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unavailable"
    assert result.task_id == task.task_id
    assert result.detail == (
        f"{DECISION_BINDING_MISMATCH}:grant_decision_hash_mismatch"
    )
    assert task.state == "failed"
    assert task.result["outcome"] == "unavailable"
    assert task.result["error_code"] == DECISION_BINDING_MISMATCH
    # The binding is proven before capability, health, key/prompt or submit.
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- grant re-verification ----------------------------------------------------


def test_tampered_grant_capabilities_never_reach_the_adapter(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    grant = _grant(session, task)
    grant.capabilities = [*grant.capabilities, "expense.admin@1.0.0"]
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    _assert_grant_document_rejected(
        session,
        task,
        result,
        adapter,
        reason="capability_not_granted",
    )


def test_tampered_grant_resource_scope_never_reaches_the_adapter(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    grant = _grant(session, task)
    widened = dict(grant.resource_scope)
    widened["resource_ids"] = [
        *widened.get("resource_ids", []),
        "res_out_of_scope_9999",
    ]
    grant.resource_scope = widened
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    _assert_grant_document_rejected(
        session,
        task,
        result,
        adapter,
        reason="grant_scope_mismatch",
    )


# -- post-accept binding failure ---------------------------------------------


def test_binding_failure_cancels_and_enters_unknown(
    session: Session, world: TaskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(
            outcome=SubmitOutcome.ACCEPTED,
            runtime_task_id="rtask_fake_0001",
        )
    )

    def _reject_binding(*args: object, **kwargs: object) -> Task:
        raise TaskError(
            "task_fencing_token_stale", "the attempt binding was rejected"
        )

    monkeypatch.setattr(
        task_service, "start_attempt", _reject_binding
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unknown"
    assert result.task_id == task.task_id
    assert result.runtime_task_id == "rtask_fake_0001"
    assert result.detail == RUNTIME_BIND_FAILED
    assert task.state == "unknown"
    assert task.attempt == 0
    assert task.runtime_task_id is None
    assert task.result["outcome"] == "unknown"
    assert task.result["error_code"] == RUNTIME_BIND_FAILED
    assert task.grant_status == "revoked"
    # Exactly one submission; the accepted handle is cancelled best-effort.
    assert len(adapter.submit_calls) == 1
    submitted_key = adapter.submit_calls[0][1]
    assert adapter.cancel_calls == [
        {
            "runtime_task_id": "rtask_fake_0001",
            "reason": RUNTIME_BIND_FAILED,
            "idempotency_key": submitted_key,
            "task_id": task.task_id,
        }
    ]
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["runtime_task_id"] == "rtask_fake_0001"
    assert reconciliation["runtime_key"] == submitted_key
    assert reconciliation["reason"] == RUNTIME_BIND_FAILED
    assert reconciliation["cancel_requested_at"] is not None
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_confirmed"] is False
    _assert_lease_released(session, task)


def test_malformed_runtime_handle_is_cancelled_and_reconciled(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    malformed = "rtask_bad handle!"
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(
            outcome=SubmitOutcome.ACCEPTED,
            runtime_task_id=malformed,
        )
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_SUBMIT_HANDLE_INVALID
    assert task.state == "unknown"
    assert task.attempt == 0
    # The malformed handle never reaches the task row (no IntegrityError).
    assert task.runtime_task_id is None
    assert task.result["outcome"] == "unknown"
    assert task.result["error_code"] == RUNTIME_SUBMIT_HANDLE_INVALID
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    submitted_key = adapter.submit_calls[0][1]
    assert adapter.cancel_calls == [
        {
            "runtime_task_id": malformed,
            "reason": RUNTIME_BIND_FAILED,
            "idempotency_key": submitted_key,
            "task_id": task.task_id,
        }
    ]
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["runtime_task_id"] == malformed
    assert reconciliation["runtime_key"] == submitted_key
    assert reconciliation["reason"] == RUNTIME_SUBMIT_HANDLE_INVALID
    assert reconciliation["cancel_requested_at"] is not None
    _assert_lease_released(session, task)


def test_prompt_canonicalization_error_is_rejected_without_submitting(
    session: Session, world: TaskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter()

    def _not_json(_task: object) -> str:
        raise TypeError("governed document is not canonical JSON")

    monkeypatch.setattr(
        task_execution, "build_runtime_prompt", _not_json
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "rejected"
    assert result.detail == RUNTIME_PROMPT_INVALID
    assert task.state == "failed"
    assert task.result["error_code"] == RUNTIME_PROMPT_INVALID
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


def test_oversize_prompt_is_rejected_without_submitting(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(
        session,
        world,
        key="idem_task_managed_oversize_0001",
        payload={"blob": "x" * MAX_RUNTIME_PROMPT_BYTES},
    )
    session.commit()
    adapter = FakeRuntimeAdapter()

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "rejected"
    assert result.detail == RUNTIME_PROMPT_TOO_LARGE
    assert task.state == "failed"
    assert task.result["error_code"] == RUNTIME_PROMPT_TOO_LARGE
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


# -- runtime status and result mapping ----------------------------------------


def test_running_then_completed_reaches_verifying(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A completed run reaches ``verifying`` -- never a terminal success.
    assert result.outcome == "verifying"
    assert result.task_id == task.task_id
    assert result.runtime_task_id == "rtask_fake_0001"
    assert task.state == "verifying"
    assert task.state != "succeeded"
    assert _event_types(session, task) == [
        "task.created",
        "task.evaluated",
        "task.queued",
        "dispatch.accepted",
        "execution.started",
        "execution.completed",
    ]
    # The runtime was observed exactly as scripted, under the task binding.
    assert adapter.status_calls == [
        ("rtask_fake_0001", task.task_id),
        ("rtask_fake_0001", task.task_id),
    ]
    assert adapter.result_calls == [("rtask_fake_0001", task.task_id)]
    assert len(adapter.submit_calls) == 1
    # The start evidence is a hashed safe ref, never the handle or the prompt.
    started = next(
        event
        for event in _task_events(session, task)
        if event.event_type == "execution.started"
    )
    start_ref = started.payload["evidence_ref"]
    assert start_ref.startswith("evidence:runtime_start_")
    assert "rtask_" not in start_ref
    # Persisted result facts come from the adapter, unmodified.
    assert task.result["outcome"] == "completed"
    assert task.result["result_ref"] == FAKE_RESULT_REF
    assert task.result["evidence_refs"] == [FAKE_EVIDENCE_REF]
    assert task.result["verification"]["result"] == "pending"
    attempt = _attempt(session, task)
    assert attempt.state == "verifying"
    assert attempt.result_ref == FAKE_RESULT_REF
    assert list(attempt.evidence_refs) == [FAKE_EVIDENCE_REF]
    # The drive owns the same lease/fencing token and releases it once the task
    # leaves the in-progress set.
    lease = _leases(session, task)[-1]
    assert lease.worker_id == WORKER_ID
    assert lease.released_at is not None


def test_completed_status_without_a_running_observation_still_verifies(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[_status(RuntimeState.COMPLETED)],
        result=_result(),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A terminal observation proves execution started, so the running fact is
    # recorded first instead of writing an illegal dispatched -> verifying edge.
    assert result.outcome == "verifying"
    assert task.state == "verifying"
    assert "execution.started" in _event_types(session, task)
    assert len(adapter.status_calls) == 1
    assert len(adapter.result_calls) == 1


def test_completed_without_result_evidence_is_unknown(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(
            outcome=ResultOutcome.SUCCEEDED,
            result_ref=None,
            evidence_refs=(),
        ),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # Nothing is synthesized to make the run look verifiable.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_RESULT_UNUSABLE
    assert task.state == "unknown"
    assert task.state != "succeeded"
    assert task.result["outcome"] == "lost"
    assert task.result["result_ref"] is None
    assert task.result["evidence_refs"] == []
    assert task.grant_status == "revoked"
    assert _grant(session, task).status == "revoked"
    assert len(adapter.result_calls) == 1
    assert "execution.completed" not in _event_types(session, task)
    _assert_lease_released(session, task)


@pytest.mark.parametrize(
    "scripted",
    [
        _result(result_ref=None, evidence_refs=(FAKE_EVIDENCE_REF,)),
        _result(result_ref=FAKE_RESULT_REF, evidence_refs=()),
        _result(result_ref="rtask_not_a_result_ref"),
        _result(evidence_refs=("runtime-said-done",)),
    ],
    ids=["no-result", "no-evidence", "bad-result-prefix", "bad-evidence-prefix"],
)
def test_incompatible_result_refs_fail_closed(
    session: Session, world: TaskWorld, scripted: RuntimeResult
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[_status(RuntimeState.COMPLETED)],
        result=scripted,
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_RESULT_UNUSABLE
    assert task.state == "unknown"
    # Nothing from the rejected result is persisted.
    assert task.result["result_ref"] is None
    assert task.result["evidence_refs"] == []
    assert task.grant_status == "revoked"


def test_refused_completion_fact_fails_closed(
    session: Session, world: TaskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )
    original_record = task_service.record_attempt_result

    def _refuse_completed(session: Session, **kwargs: object) -> Task:
        if kwargs.get("outcome") == "completed":
            # e.g. the Grant expired while the run was executing.
            raise TaskError("grant_expired", "the grant expired")
        return original_record(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(task_service, "record_attempt_result", _refuse_completed)

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # The dispatcher cannot overrule the authority check, and must not roll the
    # bind back into an endless replay: the attempt fails closed to unknown.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_FACT_REFUSED
    assert task.state == "unknown"
    assert task.state != "succeeded"
    assert task.result["outcome"] == "lost"
    assert task.result["error_code"] == RUNTIME_FACT_REFUSED
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    _assert_lease_released(session, task)


def test_dispatcher_never_calls_complete_task(
    session: Session, world: TaskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = submit_managed(session, world)
    session.commit()
    calls: list[tuple[object, ...]] = []

    def _forbidden_complete(*args: object, **kwargs: object) -> Task:
        calls.append(args)
        raise AssertionError("the dispatcher must never call complete_task")

    monkeypatch.setattr(task_service, "complete_task", _forbidden_complete)
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "verifying"
    assert task.state == "verifying"
    assert task.state != "succeeded"
    assert calls == []
    assert "verification.passed" not in _event_types(session, task)


def test_runtime_self_report_cannot_complete_the_task(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )
    assert _dispatcher(session, world, adapter=adapter).run_once().outcome == (
        "verifying"
    )

    # Independent verification is the only path out of ``verifying`` and it
    # rejects runtime/agent self-report outright.
    with pytest.raises(TaskError) as excinfo:
        task_service.complete_task(
            session,
            task_id=task.task_id,
            actor=world.actors.owner,
            result_ref=FAKE_RESULT_REF,
            evidence_refs=(FAKE_EVIDENCE_REF,),
            evidence=[evidence(source="runtime")],
        )

    assert excinfo.value.reason_code == "task_verification_self_report"
    assert task.state == "verifying"


@pytest.mark.parametrize(
    ("state", "expected_outcome", "expected_state", "expected_code"),
    [
        (RuntimeState.FAILED, "failed", "failed", RUNTIME_EXECUTION_FAILED),
        (
            RuntimeState.TIMED_OUT,
            "timed_out",
            "timed_out",
            RUNTIME_EXECUTION_TIMED_OUT,
        ),
    ],
    ids=["failed", "timed-out"],
)
def test_confirmed_terminal_stop_is_recorded(
    session: Session,
    world: TaskWorld,
    state: RuntimeState,
    expected_outcome: str,
    expected_state: str,
    expected_code: str,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(state, stop_confirmed=True),
        ],
        result=_result(
            outcome=(
                ResultOutcome.FAILED
                if state is RuntimeState.FAILED
                else ResultOutcome.TIMED_OUT
            ),
            result_ref=None,
            evidence_refs=("evidence:fake_stop_0001",),
            stop_confirmed=True,
        ),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == expected_outcome
    assert task.state == expected_state
    assert task.stop_confirmed is True
    assert task.result["outcome"] == expected_outcome
    assert task.result["error_code"] == expected_code
    assert task.result["stop_confirmed"] is True
    assert task.result["stop_evidence_refs"] == ["evidence:fake_stop_0001"]
    attempt = _attempt(session, task)
    assert attempt.state == expected_state
    assert attempt.stop_confirmed is True
    _assert_lease_released(session, task)


@pytest.mark.parametrize("state", [RuntimeState.FAILED, RuntimeState.TIMED_OUT])
def test_unconfirmed_stop_fails_closed(
    session: Session, world: TaskWorld, state: RuntimeState
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(state, stop_confirmed=False),
        ],
        result=_result(
            result_ref=None,
            evidence_refs=("evidence:fake_stop_0001",),
        ),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A stop is never fabricated from a failure state alone.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_STOP_UNCONFIRMED
    assert task.state == "unknown"
    assert task.state != state.value
    assert task.stop_confirmed is False
    assert task.result["outcome"] == "lost"
    assert task.result["stop_confirmed"] is False
    assert task.grant_status == "revoked"
    # The result was never read: the stop check comes first.
    assert adapter.result_calls == []
    _assert_lease_released(session, task)


@pytest.mark.parametrize(
    "state",
    [RuntimeState.UNKNOWN, RuntimeState.NOT_FOUND],
    ids=["unknown", "not-found"],
)
def test_lost_run_enters_unknown_and_revokes_the_grant(
    session: Session, world: TaskWorld, state: RuntimeState
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(state),
        ]
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    result = dispatcher.run_once()

    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_EXECUTION_LOST
    assert task.state == "unknown"
    assert task.result["outcome"] == "lost"
    assert task.result["error_code"] == RUNTIME_EXECUTION_LOST
    assert task.grant_status == "revoked"
    assert _grant(session, task).status == "revoked"
    assert adapter.result_calls == []
    _assert_lease_released(session, task)

    # No resume/replay: a lost run is never claimed or resubmitted again.
    assert dispatcher.run_once().outcome == "idle"
    assert len(adapter.submit_calls) == 1


def test_waiting_input_cancels_and_fails_closed(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.WAITING_INPUT),
        ]
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # #96 has no input channel: stop the run best-effort, then fail closed.  A
    # requested cancel is not a stop, so the task cannot claim "cancelled".
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_WAITING_INPUT
    assert task.state == "unknown"
    assert task.result["outcome"] == "lost"
    assert task.grant_status == "revoked"
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_WAITING_INPUT
    assert adapter.cancel_calls[0]["runtime_task_id"] == "rtask_fake_0001"
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["reason"] == RUNTIME_WAITING_INPUT
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_confirmed"] is False
    _assert_lease_released(session, task)


def test_unreadable_status_fails_closed(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(status_error=RuntimeError("status exploded"))

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # The submission is already accepted; rolling the pass back would make the
    # run look unsubmitted.  The unreadable fact becomes an honest unknown.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_STATUS_UNREADABLE
    assert task.state == "unknown"
    assert task.runtime_task_id == "rtask_fake_0001"
    assert task.attempt == 1
    assert task.result["outcome"] == "lost"
    assert task.grant_status == "revoked"
    assert len(adapter.status_calls) == 1
    assert adapter.result_calls == []
    _assert_lease_released(session, task)


def test_unreadable_result_fails_closed(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[_status(RuntimeState.COMPLETED)],
        result_error=RuntimeError("result exploded"),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_RESULT_UNREADABLE
    assert task.state == "unknown"
    assert task.result["outcome"] == "lost"
    assert task.grant_status == "revoked"
    assert len(adapter.result_calls) == 1
    _assert_lease_released(session, task)


def test_persisted_result_never_contains_the_prompt_or_a_token(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )

    assert _dispatcher(session, world, adapter=adapter).run_once().outcome == (
        "verifying"
    )

    prompt = adapter.submit_calls[0][3]
    serialised = canonical_json(task.result)
    assert prompt not in serialised
    assert "completion_criteria" not in serialised
    assert "Read one synthetic managed expense" not in serialised
    # Only opaque adapter facts and task-service facts are persisted.
    assert task.result["result_ref"] == FAKE_RESULT_REF
    assert task.result["evidence_refs"] == [FAKE_EVIDENCE_REF]


# -- submit failure matrix ----------------------------------------------------


def _lookup_found(
    runtime_task_id: str = "rtask_fake_adopted_0001",
) -> LookupResult:
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        runtime_task_id=runtime_task_id,
        reported_state="running",
        payload_hash="sha256:" + "1" * 64,
        accepted_at=utcnow(),
        observed_at=utcnow(),
    )


def test_rejected_submit_records_failed_without_retry(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.REJECTED)
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    result = dispatcher.run_once()

    assert result.outcome == "rejected"
    assert result.task_id == task.task_id
    assert result.detail == "runtime_submit_rejected"
    assert task.state == "failed"
    assert task.attempt == 0
    assert task.runtime_task_id is None
    assert task.result["outcome"] == "rejected"
    assert task.result["error_code"] == "runtime_submit_rejected"
    # A definite rejection confirms no side effect and needs no reconciliation.
    assert task.result["no_side_effects"] is True
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    assert adapter.lookup_calls == []
    _assert_lease_released(session, task)

    # A failed task is never claimed again: no second submit, no retry.
    assert dispatcher.run_once().outcome == "idle"
    assert len(adapter.submit_calls) == 1


def test_unsupported_submit_enters_needs_human(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.UNSUPPORTED)
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    result = dispatcher.run_once()

    assert result.outcome == "unsupported"
    assert result.task_id == task.task_id
    assert result.detail == "runtime_submit_unsupported"
    assert task.state == "needs_human"
    assert task.needs_human_origin == "queued"
    assert task.attempt == 0
    assert task.result["outcome"] == "unsupported"
    assert task.result["error_code"] == "runtime_submit_unsupported"
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    # A capability gap is not ambiguous: no lookup and no retry.
    assert adapter.lookup_calls == []
    _assert_lease_released(session, task)

    assert dispatcher.run_once().outcome == "idle"
    assert len(adapter.submit_calls) == 1


def test_timeout_lookup_found_adopts_existing_run(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    grant = _grant(session, task)
    expected_key = build_runtime_idempotency_key(task, grant, 1)
    adopted = "rtask_fake_adopted_0001"
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.TIMEOUT),
        lookup=_lookup_found(adopted),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # The lost accepted reply is recovered from the lookup, then bound exactly
    # like a direct acceptance: same attempt number, key, worker and fencing.
    assert result.outcome == "accepted"
    assert result.task_id == task.task_id
    assert result.runtime_task_id == adopted
    assert task.state == "dispatched"
    assert task.attempt == 1
    assert task.runtime_task_id == adopted
    assert task.execution_ref is None
    attempt = _attempt(session, task)
    assert attempt.runtime_ref == adopted
    assert attempt.idempotency_key == expected_key
    assert attempt.execution_ref is None
    assert _leases(session, task)[-1].released_at is None
    # Exactly one submission and one lookup; adoption created no second run.
    assert len(adapter.submit_calls) == 1
    assert adapter.submit_calls[0][1] == expected_key
    assert adapter.lookup_calls == [
        (task.task_id, task.grant_id, expected_key, None)
    ]
    assert adapter.created_runtime_task_ids == []
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["lookup_outcome"] == "found"
    assert reconciliation["submit_outcome"] == "timeout"
    assert reconciliation["runtime_task_id"] == adopted
    assert reconciliation["runtime_key"] == expected_key


def test_timeout_lookup_not_found_enters_unknown_without_second_submit(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.TIMEOUT),
        lookup=LookupResult(outcome=LookupOutcome.NOT_FOUND),
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    result = dispatcher.run_once()

    # not_found is a definite query answer, but the contract has no
    # "no side effect" guarantee field: record unknown/timeout and never retry.
    assert result.outcome == "timeout"
    assert result.task_id == task.task_id
    assert result.detail == "runtime_submit_timeout"
    assert task.state == "unknown"
    assert task.attempt == 0
    assert task.runtime_task_id is None
    assert task.result["outcome"] == "timeout"
    assert task.result["error_code"] == "runtime_submit_timeout"
    assert task.result["no_side_effects"] is False
    assert task.grant_status == "revoked"
    assert _grant(session, task).status == "revoked"
    assert len(adapter.submit_calls) == 1
    assert len(adapter.lookup_calls) == 1
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["lookup_outcome"] == "not_found"
    assert reconciliation["submit_outcome"] == "timeout"
    assert reconciliation["runtime_task_id"] is None
    _assert_lease_released(session, task)

    assert dispatcher.run_once().outcome == "idle"
    assert len(adapter.submit_calls) == 1


def test_timeout_lookup_unknown_enters_unknown(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.TIMEOUT),
        lookup=LookupResult(outcome=LookupOutcome.UNKNOWN),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "timeout"
    assert result.detail == "runtime_submit_timeout"
    assert task.state == "unknown"
    assert task.attempt == 0
    assert task.runtime_task_id is None
    assert task.result["outcome"] == "timeout"
    assert task.result["error_code"] == "runtime_submit_timeout"
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    assert len(adapter.lookup_calls) == 1
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["lookup_outcome"] == "unknown"
    assert reconciliation["submit_outcome"] == "timeout"
    _assert_lease_released(session, task)


def test_lookup_error_is_treated_as_unknown_without_second_submit(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.UNKNOWN),
        lookup_error=RuntimeError("lookup exploded"),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # An adapter that cannot answer proves nothing: enter unknown instead of
    # rolling the claim back and resubmitting on the next pass.
    assert result.outcome == "unknown"
    assert result.detail == "runtime_submit_unknown"
    assert task.state == "unknown"
    assert task.attempt == 0
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    assert len(adapter.lookup_calls) == 1
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["lookup_outcome"] == "unknown"
    assert reconciliation["submit_outcome"] == "unknown"
    _assert_lease_released(session, task)


def test_timeout_lookup_found_with_unusable_handle_stays_unknown(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    unusable = "rtask_bad handle!"
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.TIMEOUT),
        lookup=_lookup_found(unusable),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A found run whose handle cannot be stored is still not bindable: the
    # dispatcher records an honest unknown and never submits a second time.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_SUBMIT_HANDLE_INVALID
    assert task.state == "unknown"
    assert task.attempt == 0
    assert task.runtime_task_id is None
    assert task.result["error_code"] == RUNTIME_SUBMIT_HANDLE_INVALID
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    assert len(adapter.lookup_calls) == 1
    assert len(adapter.cancel_calls) == 1
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["lookup_outcome"] == "found"
    assert reconciliation["runtime_task_id"] == unusable
    _assert_lease_released(session, task)


def test_conflict_never_creates_second_run(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    found = "rtask_fake_adopted_0001"
    adapter = FakeRuntimeAdapter(
        submit=SubmitResult(outcome=SubmitOutcome.CONFLICT),
        lookup=_lookup_found(found),
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    result = dispatcher.run_once()

    # The runtime says this key is bound to a different payload.  Binding the
    # found run would attest to effects of the wrong input, so the task enters
    # unknown with the handle kept for human reconciliation.
    assert result.outcome == "unknown"
    assert result.detail == "runtime_submit_conflict"
    assert task.state == "unknown"
    assert task.attempt == 0
    assert task.runtime_task_id is None
    assert task.result["outcome"] == "unknown"
    assert task.result["error_code"] == "runtime_submit_conflict"
    assert task.grant_status == "revoked"
    assert len(adapter.submit_calls) == 1
    assert len(adapter.lookup_calls) == 1
    assert adapter.created_runtime_task_ids == []
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["lookup_outcome"] == "found"
    assert reconciliation["submit_outcome"] == "conflict"
    assert reconciliation["runtime_task_id"] == found
    _assert_lease_released(session, task)

    assert dispatcher.run_once().outcome == "idle"
    assert len(adapter.submit_calls) == 1


# -- service principal --------------------------------------------------------


def test_dispatcher_rejects_a_person_as_service_principal(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter()

    with pytest.raises(TaskError) as excinfo:
        _dispatcher(
            session, world, adapter=adapter, principal=world.actors.executor
        ).run_once()

    assert excinfo.value.reason_code == "task_unauthorized_actor"
    assert session.scalars(select(TaskLease)).all() == []
    assert session.scalar(
        select(Task).where(Task.task_id == task.task_id)
    ).state == "queued"
    assert adapter.capability_probes == []
    assert adapter.health_calls == 0
    assert adapter.submit_calls == []


def test_dispatcher_rejects_an_inactive_service_principal(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    disabled = replace(
        _service_principal(session, world), account_status="disabled"
    )

    with pytest.raises(PrincipalResolutionError) as excinfo:
        _dispatcher(
            session, world, principal=disabled
        ).run_once()

    assert excinfo.value.reason_code == "principal_disabled"
    assert session.scalars(select(TaskLease)).all() == []
    assert session.scalar(
        select(Task).where(Task.task_id == task.task_id)
    ).state == "queued"


def test_dispatcher_accepts_an_active_runtime_principal(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    runtime_principal = replace(
        _service_principal(session, world),
        principal_type="runtime",
        service_identity_id=None,
    )

    result = _dispatcher(
        session, world, principal=runtime_principal
    ).run_once()

    assert result.outcome == "accepted"
    assert result.task_id == task.task_id
    assert _leases(session, task)[-1].released_at is None


# -- transaction boundary and loop -------------------------------------------


def test_unexpected_adapter_error_rolls_back_the_claim(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(health=RuntimeError("probe exploded"))

    with pytest.raises(RuntimeError, match="probe exploded"):
        _dispatcher(session, world, adapter=adapter).run_once()

    # The claim, its audit row and any flush are rolled back; nothing is left
    # half-claimed for a recovery pass to trip over.
    assert session.scalars(select(TaskLease)).all() == []
    assert session.scalar(
        select(Task).where(Task.task_id == task.task_id)
    ).state == "queued"


def test_run_forever_returns_immediately_when_the_stop_event_is_set(
    session: Session, world: TaskWorld
) -> None:
    stop_event = threading.Event()
    stop_event.set()
    opened: list[bool] = []

    def session_factory() -> Session:  # pragma: no cover - must not be called
        opened.append(True)
        raise AssertionError("run_forever must not open a session after stop")

    _dispatcher(session, world).run_forever(
        stop_event, session_factory=session_factory
    )

    assert opened == []
