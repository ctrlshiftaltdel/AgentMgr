"""Managed dispatcher: preflight, submit and runtime-handle binding.

These tests use the real PostgreSQL task fixtures and the scripted
:class:`FakeRuntimeAdapter`.  The fake is test-only evidence; nothing here
claims that a real runtime supports Managed Execution.

Scope of this file in Task 4: the accepted submit path binds ``rtask_*`` through
``start_attempt``; the decision binding is verified before any adapter call; a
post-accept binding failure cancels and enters ``unknown``.  Status/result
mapping and the ambiguous-submit lookup matrix arrive in Tasks 5-6.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
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
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeGrant,
    RuntimeType,
    SubmitOutcome,
    SubmitResult,
    VersionSource,
)
from app.tasks import service as task_service
from app.tasks.errors import TaskError
from app.tasks.execution import (
    DECISION_BINDING_MISMATCH,
    MAX_RUNTIME_PROMPT_BYTES,
    RUNTIME_BIND_FAILED,
    RUNTIME_PROMPT_TOO_LARGE,
    DispatcherConfig,
    DispatcherResult,
    RuntimeDispatcher,
    RuntimePromptTooLarge,
    build_runtime_idempotency_key,
    build_runtime_prompt,
)
from app.tasks.models import Task, TaskAttempt, TaskLease
from app.tests.governance.helpers import make_service_identity
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.fake_runtime import FakeRuntimeAdapter
from app.tests.tasks.helpers import submit_managed, submit_read, utcnow

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
