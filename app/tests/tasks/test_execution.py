"""Managed dispatcher: preflight, submit, binding and status/result driving.

These tests use the real PostgreSQL task fixtures and the scripted
:class:`FakeRuntimeAdapter`.  The fake is test-only evidence; nothing here
claims that a real runtime supports Managed Execution.

Scope of this file: the accepted submit path binds ``rtask_*`` through
``start_attempt`` and the same pass drives the bound run through
``get_status``/``get_result`` under the held lease/fencing token.  The drive is
bounded by the run's own runtime budget (``poll_interval`` between observations,
lease renewal at half life) and never returns while a dispatched/running task
still holds a live lease: ``running`` is recorded with a hashed start-evidence
ref, a completed run with a core-compatible ``result_*``/``evidence:*`` pair
reaches ``verifying`` only, confirmed failures/stops are recorded, a confirmed
stop while waiting for input escalates to ``needs_human``, and everything the
core cannot map honestly fails closed to ``unknown`` with the Grant revoked.  The
dispatcher never calls ``complete_task``.

Task 7 adds the reconciliation loops that surround the pass.  The drive commits
after the bind and after every observation, so a *real second connection* can
request a cancel while the run is being driven (tested with a background
connection, not a same-session mutation).  A runtime ``stop_confirmed`` is
evidence, never business reconciliation: the hashed evidence is saved and the
task is escalated ``needs_human``; the dispatcher never writes
``cancelled``/``side_effects_reconciled``.  ``RuntimeRecoveryChecker`` feeds
``worker.recover_expired_leases`` (managed tasks only, never replays a live
runtime), a separate pass reconciles stranded ``cancel_requested`` rows, and
``unknown`` tasks that still carry a handle get a fenced best-effort stop.

Task 8 adds the fail-closed CLI: ``main`` has no default production adapter,
rejects unknown adapter names, and needs a trusted ``dispatcher_factory``;
``--once``/``--loop`` are exercised with injected fake dispatchers.  The
hardening cases pin recovery paging, the loop's bounded error backoff, and the
attempt-scoped reconciliation facts.  Nothing here is a real runtime claim: the
Fake adapter remains test-only evidence and #97 owns the Hermes configuration.
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
    CancelOutcome,
    CancelResult,
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
from app.tasks import worker
from app.tasks.errors import TaskError
from app.tasks.execution import (
    DECISION_BINDING_MISMATCH,
    GRANT_DOCUMENT_INVALID,
    MAX_RUNTIME_PROMPT_BYTES,
    RUNTIME_BIND_FAILED,
    RUNTIME_CANCEL_RECONCILE,
    RUNTIME_CANCEL_REQUESTED,
    RUNTIME_DRIVE_DEADLINE,
    RUNTIME_DRIVE_OBSERVATION_CAP,
    RUNTIME_EXECUTION_FAILED,
    RUNTIME_EXECUTION_LOST,
    RUNTIME_EXECUTION_TIMED_OUT,
    RUNTIME_FACT_REFUSED,
    RUNTIME_HANDLE_MISMATCH,
    RUNTIME_PROMPT_INVALID,
    RUNTIME_PROMPT_TOO_LARGE,
    RUNTIME_RESULT_UNREADABLE,
    RUNTIME_RESULT_UNUSABLE,
    RUNTIME_STATUS_UNREADABLE,
    RUNTIME_STOP_CONFIRMED,
    RUNTIME_STOP_UNCONFIRMED,
    RUNTIME_STOP_UNREQUESTED,
    RUNTIME_SUBMIT_HANDLE_INVALID,
    RUNTIME_WAITING_INPUT,
    DispatcherConfig,
    DispatcherResult,
    RuntimeDispatcher,
    RuntimePromptTooLarge,
    RuntimeRecoveryChecker,
    build_runtime_idempotency_key,
    build_runtime_prompt,
    main,
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

#: Test-only observation bound.  Terminal scripts finish in a handful of
#: observations; a mistakenly non-terminal script fails closed quickly instead
#: of sleeping out a 300 s runtime budget.  Production never sets this.
TEST_OBSERVATION_LIMIT = 16


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
    config: DispatcherConfig | None = None,
) -> RuntimeDispatcher:
    return RuntimeDispatcher(
        FakeRuntimeAdapter() if adapter is None else adapter,
        (
            DispatcherConfig(
                worker_id=WORKER_ID,
                poll_interval=0.0,
                max_status_observations=TEST_OBSERVATION_LIMIT,
            )
            if config is None
            else config
        ),
        service_principal=(
            _service_principal(session, world) if principal is None else principal
        ),
        session=session,
    )


class _ExternalCancelAdapter(FakeRuntimeAdapter):
    """Fake adapter that lets a *second connection* request the core cancel.

    ``task_service.request_cancel`` runs in a real thread with its own
    ``Session``/connection.  It can only commit if the drive is **not** holding
    the task row lock while it observes the runtime, which is exactly the
    production property under test (a same-session mutation would prove
    nothing).  The trigger can be a status call or a result call, so the
    "cancel commits while the drive is reading the result" race is testable.
    The adapter itself only reports facts.
    """

    def __init__(
        self,
        *,
        engine: object,
        actor: GovernancePrincipal,
        task_id: str,
        after_status_calls: int | None = None,
        after_result_calls: int | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._engine = engine
        self._actor = actor
        self._task_id = task_id
        self._after_status_calls = after_status_calls
        self._after_result_calls = after_result_calls
        self.cancel_requests = 0
        self.cancel_committed = False

    def get_status(
        self, runtime_task_id: str, *, task_id: str | None = None
    ) -> StatusResult:
        status = super().get_status(runtime_task_id, task_id=task_id)
        if (
            self._after_status_calls is not None
            and len(self.status_calls) >= self._after_status_calls
        ):
            self._maybe_request_cancel()
        return status

    def get_result(
        self, runtime_task_id: str, *, task_id: str | None = None
    ) -> RuntimeResult:
        result = super().get_result(runtime_task_id, task_id=task_id)
        if (
            self._after_result_calls is not None
            and len(self.result_calls) >= self._after_result_calls
        ):
            self._maybe_request_cancel()
        return result

    def _maybe_request_cancel(self) -> None:
        if self.cancel_requests:
            return
        self.cancel_requests += 1
        self._request_cancel_on_another_connection()

    def _request_cancel_on_another_connection(self) -> None:
        errors: list[BaseException] = []

        def _run() -> None:
            try:
                other = Session(self._engine)  # type: ignore[arg-type]
                try:
                    task_service.request_cancel(
                        other,
                        task_id=self._task_id,
                        actor=self._actor,
                    )
                    other.commit()
                finally:
                    other.close()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                errors.append(exc)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=10)
        if errors:
            raise errors[0]
        if thread.is_alive():
            raise AssertionError(
                "the drive held the task row lock across an observation"
            )
        self.cancel_committed = True


class _StopOnStatusAdapter(FakeRuntimeAdapter):
    """Fake adapter that signals the dispatcher's stop event mid-drive."""

    def __init__(
        self, stop_event: threading.Event, **kwargs: object
    ) -> None:
        super().__init__(**kwargs)
        self._stop_event = stop_event

    def get_status(
        self, runtime_task_id: str, *, task_id: str | None = None
    ) -> StatusResult:
        status = super().get_status(runtime_task_id, task_id=task_id)
        self._stop_event.set()
        return status


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
        ),
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # The bind facts are recorded first, then the same pass drives the run.
    assert result.outcome == "verifying"
    assert result.task_id == task.task_id
    assert result.runtime_task_id == "rtask_fake_0001"
    assert task.state == "verifying"
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
    # The run was observed under the task binding and reached a terminal fact.
    assert adapter.status_calls == [
        ("rtask_fake_0001", task.task_id),
        ("rtask_fake_0001", task.task_id),
    ]
    assert adapter.result_calls == [("rtask_fake_0001", task.task_id)]
    assert task.result["outcome"] == "completed"
    assert task.result["result_ref"] == FAKE_RESULT_REF
    # No in-progress run is left behind: the drive released its lease.
    assert _leases(session, task)[-1].released_at is not None


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


def test_waiting_input_with_confirmed_stop_escalates(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.WAITING_INPUT),
        ],
        cancel=CancelResult(
            outcome=CancelOutcome.STOPPED,
            stop_confirmed=True,
            observed_at=utcnow(),
        ),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A confirmed stop is a real fact: escalate to a human instead of throwing
    # it away as "lost".
    assert result.outcome == "needs_human"
    assert result.detail == RUNTIME_WAITING_INPUT
    assert task.state == "needs_human"
    assert task.state != "unknown"
    assert task.needs_human_origin == "running"
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_WAITING_INPUT
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == "stopped"
    assert reconciliation["stop_confirmed"] is True
    stop_refs = reconciliation["stop_evidence_refs"]
    assert len(stop_refs) == 1
    assert stop_refs[0].startswith("evidence:runtime_stop_")
    escalation = next(
        event
        for event in _task_events(session, task)
        if event.event_type == "human.escalation_required"
    )
    assert escalation.payload["evidence_refs"] == stop_refs
    # The core stop flag is never fabricated here: the core never requested
    # this stop, so needs_human keeps the attempt open exactly as the service
    # defines (a core `cancel_requested` is the Task 7 path).
    assert task.stop_confirmed is False
    attempt = _attempt(session, task)
    assert attempt.state == "running"
    assert attempt.ended_at is None
    assert task.grant_status == "active"
    _assert_lease_released(session, task)


def test_waiting_input_confirmed_by_terminal_status_escalates(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.WAITING_INPUT),
            # The cancel only reported "requested"; the follow-up status is the
            # equivalent confirmed stop the dispatcher must not discard.
            _status(RuntimeState.STOPPED, stop_confirmed=True),
        ],
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "needs_human"
    assert task.state == "needs_human"
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_evidence_refs"][0].startswith(
        "evidence:runtime_stop_"
    )
    _assert_lease_released(session, task)


# -- drive bounds, lease renewal and handle attribution -----------------------


def test_deadline_abandons_a_running_run(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world, max_runtime_seconds=1)
    session.commit()
    adapter = FakeRuntimeAdapter(status=_status(RuntimeState.RUNNING))
    dispatcher = _dispatcher(
        session,
        world,
        adapter=adapter,
        config=DispatcherConfig(worker_id=WORKER_ID, poll_interval=0.02),
    )

    result = dispatcher.run_once()

    # The run outlived its runtime budget: cancel best-effort, keep the handle
    # and fail closed.  It is never left running with a live lease.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_DRIVE_DEADLINE
    assert task.state == "unknown"
    assert task.result["outcome"] == "lost"
    assert task.grant_status == "revoked"
    assert len(adapter.status_calls) > 1
    assert len(adapter.submit_calls) == 1
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_DRIVE_DEADLINE
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["reason"] == RUNTIME_DRIVE_DEADLINE
    assert reconciliation["runtime_task_id"] == "rtask_fake_0001"
    assert reconciliation["runtime_key"] == adapter.submit_calls[0][1]
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_confirmed"] is False
    _assert_lease_released(session, task)

    # No replay: the task is no longer claimable.
    assert dispatcher.run_once().outcome == "idle"
    assert len(adapter.submit_calls) == 1


def test_observation_cap_abandons_an_in_progress_run(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(status=_status(RuntimeState.RUNNING))
    dispatcher = _dispatcher(
        session,
        world,
        adapter=adapter,
        config=DispatcherConfig(
            worker_id=WORKER_ID,
            poll_interval=0.0,
            max_status_observations=3,
        ),
    )

    result = dispatcher.run_once()

    # The test-only cap uses the same fail-closed path as the deadline, so it
    # can never mask a stranded in-progress run.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_DRIVE_OBSERVATION_CAP
    assert task.state == "unknown"
    assert len(adapter.status_calls) >= 3
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_DRIVE_OBSERVATION_CAP
    _assert_lease_released(session, task)


def test_lease_is_renewed_while_the_run_is_driven(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )
    dispatcher = _dispatcher(
        session,
        world,
        adapter=adapter,
        config=DispatcherConfig(
            worker_id=WORKER_ID, lease_seconds=1, poll_interval=0.2
        ),
    )

    result = dispatcher.run_once()

    # The run takes about as long as the lease, so the lease must have been
    # renewed or the final fact would have been rejected as stale.
    assert result.outcome == "verifying"
    assert task.state == "verifying"
    leases = _leases(session, task)
    assert len(leases) == 1
    lease = leases[0]
    assert lease.released_at is not None
    assert lease.lease_expires_at - lease.acquired_at > timedelta(seconds=1)


def test_lease_renewal_failure_rolls_back_the_unit_of_work(
    session: Session, world: TaskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(status=_status(RuntimeState.RUNNING))

    def _lost_lease(*args: object, **kwargs: object) -> None:
        raise TaskError("task_lease_not_held", "the lease was lost")

    monkeypatch.setattr(worker, "renew_lease", _lost_lease)
    dispatcher = _dispatcher(
        session,
        world,
        adapter=adapter,
        config=DispatcherConfig(
            worker_id=WORKER_ID, lease_seconds=1, poll_interval=0.2
        ),
    )

    with pytest.raises(TaskError):
        dispatcher.run_once()

    # No fact may be written under a stale fencing token.  The bind and the
    # observed ``running`` fact were already committed by design (the drive
    # commits before observing), so the failure rolls back only the current
    # unit of work: the run stays ``running`` with a still-live lease and lease
    # recovery reconciles it instead of replaying it.
    reloaded = session.scalar(
        select(Task).where(Task.task_id == task.task_id)
    )
    assert reloaded is not None
    assert reloaded.state == "running"
    assert reloaded.result.get("outcome") is None
    leases = _leases(session, task)
    assert leases
    assert leases[-1].released_at is None
    assert adapter.submit_calls and len(adapter.submit_calls) == 1


def test_status_for_another_handle_fails_closed(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=_status(
            RuntimeState.RUNNING, runtime_task_id="rtask_other_0001"
        )
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A status about another run proves nothing about this task and must not be
    # attributed to it.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_HANDLE_MISMATCH
    assert task.state == "unknown"
    assert task.result["outcome"] == "lost"
    assert task.grant_status == "revoked"
    assert "execution.started" not in _event_types(session, task)
    assert adapter.result_calls == []
    _assert_lease_released(session, task)


def test_unrequested_confirmed_stop_escalates_with_evidence(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.STOPPED, stop_confirmed=True),
        ]
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A runtime-confirmed stop is evidence that the runtime ended, not that the
    # business side effects are reconciled.  It is preserved and handed to a
    # human: never auto-written as `cancelled`, never dropped as `unknown`.
    assert result.outcome == "needs_human"
    assert result.detail == RUNTIME_STOP_UNREQUESTED
    assert task.state == "needs_human"
    assert task.needs_human_origin == "running"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    assert adapter.cancel_calls == []
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["stop_confirmed"] is True
    assert reconciliation["cancel_outcome"] == "stopped"
    assert reconciliation["runtime_outcome"] == "stopped"
    assert reconciliation["stop_evidence_refs"][0].startswith(
        "evidence:runtime_stop_"
    )
    escalation = next(
        event
        for event in _task_events(session, task)
        if event.event_type == "human.escalation_required"
    )
    assert escalation.payload["evidence_refs"] == (
        reconciliation["stop_evidence_refs"]
    )
    _assert_lease_released(session, task)


@pytest.mark.parametrize("state", [RuntimeState.FAILED, RuntimeState.TIMED_OUT])
def test_confirmed_stop_with_unreadable_result_fails_closed(
    session: Session, world: TaskWorld, state: RuntimeState
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(state, stop_confirmed=True),
        ],
        result_error=RuntimeError("result exploded"),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_RESULT_UNREADABLE
    assert task.state == "unknown"
    assert task.result["outcome"] == "lost"
    assert len(adapter.result_calls) == 1
    _assert_lease_released(session, task)


@pytest.mark.parametrize("state", [RuntimeState.FAILED, RuntimeState.TIMED_OUT])
def test_confirmed_stop_without_result_evidence_fails_closed(
    session: Session, world: TaskWorld, state: RuntimeState
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(state, stop_confirmed=True),
        ],
        result=_result(
            outcome=ResultOutcome.FAILED,
            result_ref=None,
            evidence_refs=(),
            stop_confirmed=True,
        ),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_RESULT_UNUSABLE
    assert task.state == "unknown"
    assert task.stop_confirmed is False
    assert task.result["outcome"] == "lost"
    _assert_lease_released(session, task)


# -- core cancel and stop -----------------------------------------------------


def _external_cancel_adapter(
    world: TaskWorld,
    engine: object,
    task: Task,
    **kwargs: object,
) -> _ExternalCancelAdapter:
    """Build the second-connection cancel adapter for ``task``."""

    return _ExternalCancelAdapter(
        engine=engine,
        actor=world.actors.requester,
        task_id=task.task_id,
        **kwargs,
    )


def test_cancel_requested_waits_for_requested_then_escalates_on_confirmed_stop(
    session: Session, world: TaskWorld, engine: object
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = _external_cancel_adapter(
        world,
        engine,
        task,
        after_status_calls=1,
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.STOPPED, stop_confirmed=True),
        ],
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    result = dispatcher.run_once()

    # The cancel committed on another connection while the drive was between
    # observations: the row lock is not held across the runtime call.
    assert adapter.cancel_committed is True
    # `requested` is not a stop; the confirmed stop is evidence, not business
    # reconciliation.  A human decides, nothing is auto-cancelled.
    assert result.outcome == "needs_human"
    assert result.detail == RUNTIME_STOP_CONFIRMED
    assert result.runtime_task_id == "rtask_fake_0001"
    assert task.state == "needs_human"
    assert task.needs_human_origin == "cancel_requested"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    assert task.result.get("outcome") != "cancelled"
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_confirmed"] is True
    assert reconciliation["runtime_outcome"] == "stopped"
    assert reconciliation["runtime_task_id"] == "rtask_fake_0001"
    assert reconciliation["runtime_key"] == adapter.submit_calls[0][1]
    stop_refs = reconciliation["stop_evidence_refs"]
    assert len(stop_refs) == 1
    assert stop_refs[0].startswith("evidence:runtime_stop_")
    assert "rtask_fake_0001" not in stop_refs[0]
    assert "execution.stopped" not in _event_types(session, task)
    escalation = next(
        event
        for event in _task_events(session, task)
        if event.event_type == "human.escalation_required"
    )
    assert escalation.payload["evidence_refs"] == stop_refs
    # T59 can still recover the same attempt, so the Grant stays usable.
    assert task.grant_status == "active"
    assert _attempt(session, task).ended_at is None
    # Exactly one runtime cancel, one submit, no replay.
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_CANCEL_REQUESTED
    assert adapter.cancel_calls[0]["runtime_task_id"] == "rtask_fake_0001"
    assert len(adapter.submit_calls) == 1
    _assert_lease_released(session, task)
    assert dispatcher.run_once().outcome == "idle"


def test_drive_commits_between_observations_so_cancel_is_reachable(
    session: Session, world: TaskWorld, engine: object
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = _external_cancel_adapter(
        world,
        engine,
        task,
        after_status_calls=1,
        status=_status(RuntimeState.RUNNING),
    )
    dispatcher = _dispatcher(
        session,
        world,
        adapter=adapter,
        config=DispatcherConfig(
            worker_id=WORKER_ID,
            poll_interval=0.0,
            max_status_observations=3,
        ),
    )

    result = dispatcher.run_once()

    # This is the Critical regression proof: a real second connection could
    # commit request_cancel while the pass was driving, and the drive saw it
    # (one cancel reached the adapter), not block until the pass ended.
    assert adapter.cancel_committed is True
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_CANCEL_REQUESTED
    # The requested cancel is not a stop, so the pass ends honestly.
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_DRIVE_OBSERVATION_CAP
    assert task.state == "unknown"
    assert task.state != "cancelled"
    assert task.stop_confirmed is False
    assert task.result["outcome"] == "lost"
    _assert_lease_released(session, task)


def test_cancel_requested_only_is_not_cancelled(
    session: Session, world: TaskWorld, engine: object
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = _external_cancel_adapter(
        world,
        engine,
        task,
        after_status_calls=1,
        status=_status(RuntimeState.RUNNING),
        cancel=CancelResult(
            outcome=CancelOutcome.REQUESTED,
            stop_confirmed=False,
            observed_at=utcnow(),
        ),
    )
    dispatcher = _dispatcher(
        session,
        world,
        adapter=adapter,
        config=DispatcherConfig(
            worker_id=WORKER_ID,
            poll_interval=0.0,
            max_status_observations=3,
        ),
    )

    result = dispatcher.run_once()

    # A requested cancel is not a stop.  The run never confirmed the stop, so
    # the pass fails closed instead of claiming "cancelled".
    assert result.outcome == "unknown"
    assert result.detail == RUNTIME_DRIVE_OBSERVATION_CAP
    assert task.state == "unknown"
    assert task.state != "cancelled"
    assert task.stop_confirmed is False
    assert task.result["outcome"] == "lost"
    assert task.result.get("side_effects_reconciled") is not True
    assert len(adapter.cancel_calls) == 1
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_confirmed"] is False
    _assert_lease_released(session, task)
    assert len(adapter.submit_calls) == 1


@pytest.mark.parametrize(
    "cancel_outcome",
    [CancelOutcome.UNSUPPORTED, CancelOutcome.UNKNOWN],
    ids=["unsupported", "unknown"],
)
def test_cancel_outcome_that_is_not_a_stop_never_reports_cancelled(
    session: Session,
    world: TaskWorld,
    engine: object,
    cancel_outcome: CancelOutcome,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = _external_cancel_adapter(
        world,
        engine,
        task,
        after_status_calls=1,
        status=_status(RuntimeState.RUNNING),
        cancel=CancelResult(
            outcome=cancel_outcome,
            stop_confirmed=False,
            observed_at=utcnow(),
        ),
    )
    dispatcher = _dispatcher(
        session,
        world,
        adapter=adapter,
        config=DispatcherConfig(
            worker_id=WORKER_ID,
            poll_interval=0.0,
            max_status_observations=3,
        ),
    )

    result = dispatcher.run_once()

    # Unsupported/unknown cancels are reconciled honestly and never become a
    # fabricated stop or a human escalation for a stop that did not happen.
    assert result.outcome == "unknown"
    assert task.state == "unknown"
    assert task.stop_confirmed is False
    assert len(adapter.cancel_calls) == 1
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == cancel_outcome.value
    assert reconciliation["stop_confirmed"] is False
    assert "stop_evidence_refs" not in reconciliation
    _assert_lease_released(session, task)


def test_cancel_race_completed_result_wins(
    session: Session, world: TaskWorld, engine: object
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = _external_cancel_adapter(
        world,
        engine,
        task,
        after_status_calls=1,
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # The run completed before the unconfirmed cancel took effect: the
    # completed result must survive and reach verification.
    assert adapter.cancel_committed is True
    assert result.outcome == "verifying"
    assert task.state == "verifying"
    assert task.result["outcome"] == "completed"
    assert task.result["result_ref"] == FAKE_RESULT_REF
    assert task.result["evidence_refs"] == [FAKE_EVIDENCE_REF]
    assert task.stop_confirmed is False
    assert "execution.stopped" not in _event_types(session, task)
    assert "human.escalation_required" not in _event_types(session, task)
    assert len(adapter.cancel_calls) == 1
    assert len(adapter.result_calls) == 1
    assert len(adapter.submit_calls) == 1
    _assert_lease_released(session, task)


def test_cancel_already_terminal_keeps_the_completed_result(
    session: Session, world: TaskWorld, engine: object
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = _external_cancel_adapter(
        world,
        engine,
        task,
        after_status_calls=1,
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        cancel=CancelResult(
            outcome=CancelOutcome.ALREADY_TERMINAL,
            stop_confirmed=False,
            observed_at=utcnow(),
        ),
        result=_result(),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # `already_terminal` follows the real result path instead of being treated
    # as a stop.
    assert result.outcome == "verifying"
    assert task.state == "verifying"
    assert task.result["outcome"] == "completed"
    assert task.result["result_ref"] == FAKE_RESULT_REF
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == "already_terminal"
    assert task.stop_confirmed is False
    _assert_lease_released(session, task)


def test_confirmed_stop_never_sets_side_effects_reconciled(
    session: Session,
    world: TaskWorld,
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = submit_managed(session, world)
    session.commit()

    def _forbidden_confirm_stop(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "a runtime stop must never be written as business reconciliation"
        )

    monkeypatch.setattr(task_service, "confirm_stop", _forbidden_confirm_stop)
    adapter = _external_cancel_adapter(
        world,
        engine,
        task,
        after_status_calls=1,
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.STOPPED, stop_confirmed=True),
        ],
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    assert result.outcome == "needs_human"
    assert task.state != "cancelled"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    assert task.result.get("outcome") != "cancelled"
    assert "execution.stopped" not in _event_types(session, task)
    assert "human.escalation_required" in _event_types(session, task)


def test_first_observation_timed_out_is_recorded_after_running(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=_status(RuntimeState.TIMED_OUT, stop_confirmed=True),
        result=_result(
            outcome=ResultOutcome.TIMED_OUT,
            result_ref=None,
            evidence_refs=("evidence:fake_stop_0001",),
            stop_confirmed=True,
        ),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # A runtime may time out before it ever reports `running`; the terminal
    # fact is written through the legal dispatched -> running -> timed_out
    # path instead of raising an invalid transition.
    assert result.outcome == "timed_out"
    assert task.state == "timed_out"
    assert task.stop_confirmed is True
    events = _event_types(session, task)
    assert "execution.started" in events
    assert "execution.timed_out" in events
    assert events.index("execution.started") < events.index(
        "execution.timed_out"
    )
    assert _attempt(session, task).state == "timed_out"
    _assert_lease_released(session, task)


def test_cancel_during_result_fetch_is_not_absorbed_as_terminal_failure(
    session: Session, world: TaskWorld, engine: object
) -> None:
    task = submit_managed(session, world)
    session.commit()
    # The first observation is already a confirmed timeout.  The external
    # cancel commits *while get_result is running*: the post-status refresh saw
    # `dispatched`, but the authoritative row becomes `cancel_requested` before
    # the running/failure write.
    adapter = _ExternalCancelAdapter(
        engine=engine,
        actor=world.actors.requester,
        task_id=task.task_id,
        after_result_calls=1,
        status=_status(RuntimeState.TIMED_OUT, stop_confirmed=True),
        result=_result(
            outcome=ResultOutcome.TIMED_OUT,
            result_ref=None,
            evidence_refs=("evidence:fake_stop_0001",),
            stop_confirmed=True,
        ),
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    result = dispatcher.run_once()

    assert adapter.cancel_committed is True
    # The cancel must not be silently absorbed by `mark_running` (T49) and the
    # timeout must not be written as a plain terminal fact.
    assert result.outcome == "needs_human"
    assert result.detail == RUNTIME_STOP_CONFIRMED
    assert task.state == "needs_human"
    assert task.state not in {"timed_out", "failed", "cancelled"}
    assert task.needs_human_origin == "cancel_requested"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["stop_confirmed"] is True
    assert reconciliation["runtime_outcome"] == "timed_out"
    assert reconciliation["stop_evidence_refs"][0].startswith(
        "evidence:runtime_stop_"
    )
    events = _event_types(session, task)
    assert "task.cancel_requested" in events
    # The revision guard prevented `dispatched -> running` (which would have
    # swallowed the cancel) and no terminal failure was recorded.
    assert "execution.started" not in events
    assert "execution.timed_out" not in events
    assert "execution.failed" not in events
    assert "human.escalation_required" in events
    assert len(adapter.submit_calls) == 1
    _assert_lease_released(session, task)


# -- lease recovery and unknown-run cancellation ------------------------------


def test_recovery_checker_found_and_unknown() -> None:
    task = SimpleNamespace(
        task_id="task_recovery_0001",
        grant_id="grnt_recovery_0001",
        runtime_task_id=None,
        execution_mode="managed_execution",
    )
    adapter = FakeRuntimeAdapter(
        lookup=LookupResult(
            outcome=LookupOutcome.FOUND,
            runtime_task_id="rtask_recovery_0001",
        ),
        status=_status(
            RuntimeState.RUNNING, runtime_task_id="rtask_recovery_0001"
        ),
    )
    checker = RuntimeRecoveryChecker(adapter)

    # A key lookup found the run; its status is live, so the boundary reports
    # "running" and the worker never re-dispatches it.
    assert checker(
        task=task, attempt_number=1, idempotency_key="idem_recovery_0001"
    ) == ("running", False)
    assert adapter.submit_calls == []
    assert adapter.lookup_calls == [
        (
            "task_recovery_0001",
            "grnt_recovery_0001",
            "idem_recovery_0001",
            None,
        )
    ]

    # A bound handle is authoritative; no lookup is needed.
    task.runtime_task_id = "rtask_recovery_0001"
    bound = FakeRuntimeAdapter(
        status=_status(
            RuntimeState.RUNNING, runtime_task_id="rtask_recovery_0001"
        )
    )
    assert RuntimeRecoveryChecker(bound)(
        task=task, attempt_number=1, idempotency_key=None
    ) == ("running", False)
    assert bound.lookup_calls == []
    assert bound.submit_calls == []

    # Unreadable / unknown facts are honest "unknown", never "not accepted".
    unreadable = FakeRuntimeAdapter(
        status_error=RuntimeError("status exploded")
    )
    assert RuntimeRecoveryChecker(unreadable)(
        task=task, attempt_number=1, idempotency_key="idem_recovery_0001"
    ) == ("unknown", False)
    unknown_lookup = FakeRuntimeAdapter(
        lookup=LookupResult(outcome=LookupOutcome.UNKNOWN)
    )
    unbound = SimpleNamespace(
        task_id="task_recovery_0001",
        grant_id="grnt_recovery_0001",
        runtime_task_id=None,
        execution_mode="managed_execution",
    )
    assert RuntimeRecoveryChecker(unknown_lookup)(
        task=unbound, attempt_number=1, idempotency_key="idem_recovery_0001"
    ) == ("unknown", False)
    assert unknown_lookup.submit_calls == []


def test_recovery_checker_ignores_non_managed_tasks() -> None:
    advisory = SimpleNamespace(
        task_id="task_recovery_advisory",
        grant_id="grnt_recovery_advisory",
        runtime_task_id="rtask_recovery_0001",
        execution_mode="advisory",
    )
    adapter = FakeRuntimeAdapter(
        status=_status(
            RuntimeState.RUNNING, runtime_task_id="rtask_recovery_0001"
        ),
        lookup=LookupResult(
            outcome=LookupOutcome.FOUND,
            runtime_task_id="rtask_recovery_0001",
        ),
    )
    checker = RuntimeRecoveryChecker(adapter)

    # A non-managed row is outside this dispatcher's boundary: no lookup, no
    # status, no "not accepted", no replay.
    assert checker(
        task=advisory, attempt_number=1, idempotency_key="idem_recovery_0001"
    ) == ("unknown", False)
    assert adapter.status_calls == []
    assert adapter.lookup_calls == []
    assert adapter.submit_calls == []


def test_recovery_checker_not_found_only_without_a_bound_handle() -> None:
    unbound = SimpleNamespace(
        task_id="task_recovery_0002",
        grant_id="grnt_recovery_0002",
        runtime_task_id=None,
        execution_mode="managed_execution",
    )
    not_found = FakeRuntimeAdapter(
        lookup=LookupResult(outcome=LookupOutcome.NOT_FOUND)
    )
    checker = RuntimeRecoveryChecker(not_found)

    # No handle was ever bound and the adapter gives the contract's definite
    # absence answer: this is the only "never accepted" answer.
    assert checker(
        task=unbound, attempt_number=1, idempotency_key="idem_recovery_0002"
    ) == ("not_accepted", False)
    assert not_found.submit_calls == []
    # Missing local state is not a proof of no side effects.
    assert checker(task=unbound, attempt_number=1, idempotency_key=None) == (
        "unknown",
        False,
    )
    # A raising lookup cannot prove absence.
    raising = FakeRuntimeAdapter(lookup_error=RuntimeError("lookup exploded"))
    assert RuntimeRecoveryChecker(raising)(
        task=unbound, attempt_number=1, idempotency_key="idem_recovery_0002"
    ) == ("unknown", False)
    # A found run without a usable handle cannot be observed or replayed.
    no_handle = FakeRuntimeAdapter(
        lookup=LookupResult(outcome=LookupOutcome.FOUND)
    )
    assert RuntimeRecoveryChecker(no_handle)(
        task=unbound, attempt_number=1, idempotency_key="idem_recovery_0002"
    ) == ("unknown", False)

    # A bound run that the runtime no longer knows is still never replayed.
    bound = SimpleNamespace(
        task_id="task_recovery_0002",
        grant_id="grnt_recovery_0002",
        runtime_task_id="rtask_recovery_0002",
        execution_mode="managed_execution",
    )
    missing_run = FakeRuntimeAdapter(
        lookup=LookupResult(outcome=LookupOutcome.NOT_FOUND),
        status=_status(
            RuntimeState.NOT_FOUND, runtime_task_id="rtask_recovery_0002"
        ),
    )
    assert RuntimeRecoveryChecker(missing_run)(
        task=bound, attempt_number=1, idempotency_key="idem_recovery_0002"
    ) == ("unknown", False)
    assert missing_run.submit_calls == []

    # A malformed persisted handle cannot be observed, so absence is never
    # inferred from it either.
    malformed = SimpleNamespace(
        task_id="task_recovery_0002",
        grant_id="grnt_recovery_0002",
        runtime_task_id="not-a-runtime-handle",
        execution_mode="managed_execution",
    )
    checker_adapter = FakeRuntimeAdapter(
        lookup=LookupResult(outcome=LookupOutcome.NOT_FOUND)
    )
    assert RuntimeRecoveryChecker(checker_adapter)(
        task=malformed, attempt_number=1, idempotency_key="idem_recovery_0002"
    ) == ("unknown", False)
    assert checker_adapter.lookup_calls == []
    assert checker_adapter.submit_calls == []


def test_lease_expiry_does_not_replay_running_runtime(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    principal = _service_principal(session, world)
    started = utcnow()
    claimed = worker.claim_task(
        session,
        worker_id=WORKER_ID,
        actor=principal,
        execution_mode="managed_execution",
        lease_seconds=5,
        now=started,
    )
    assert claimed is not None
    assert claimed.task.task_id == task.task_id
    task_service.start_attempt(
        session,
        task_id=task.task_id,
        actor=principal,
        worker_id=WORKER_ID,
        fencing_token=claimed.lease.fencing_token,
        runtime_ref="rtask_fake_0001",
        idempotency_key="idem_lease_expired_0001",
        now=started,
    )
    session.commit()
    adapter = FakeRuntimeAdapter(status=_status(RuntimeState.RUNNING))
    dispatcher = _dispatcher(session, world, adapter=adapter)
    expired = started + timedelta(seconds=6)

    recovered = dispatcher.recover_leases(now=expired)

    # A live runtime is never rolled back or replayed: the attempt enters
    # unknown and the handle stays for reconciliation.
    assert [(item.action, item.state) for item in recovered] == [
        ("entered_unknown", "unknown")
    ]
    assert task.state == "unknown"
    assert task.runtime_task_id == "rtask_fake_0001"
    assert task.result["outcome"] == "lost"
    assert task.grant_status == "revoked"
    assert adapter.submit_calls == []
    assert adapter.status_calls == [("rtask_fake_0001", task.task_id)]
    assert adapter.cancel_calls == []
    assert _leases(session, task)[-1].released_at is not None
    # No second recovery and no dispatch pass can pick the row up again.
    assert dispatcher.recover_leases(now=expired + timedelta(seconds=1)) == []
    assert dispatcher.run_once().outcome == "idle"
    assert adapter.submit_calls == []


def test_unknown_with_runtime_handle_best_effort_cancel(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.UNKNOWN),
        ]
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    assert dispatcher.run_once().outcome == "unknown"
    assert task.state == "unknown"
    assert task.runtime_task_id == "rtask_fake_0001"
    submit_key = adapter.submit_calls[0][1]

    results = dispatcher.cancel_unknown_runs()

    # The persisted handle is a real run: stop it best-effort.  An unconfirmed
    # stop is not reconciliation, so the task stays unknown.
    assert [item.outcome for item in results] == ["unknown"]
    assert results[0].detail == RUNTIME_CANCEL_RECONCILE
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["runtime_task_id"] == "rtask_fake_0001"
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_CANCEL_RECONCILE
    assert adapter.cancel_calls[0]["idempotency_key"] == submit_key
    assert adapter.cancel_calls[0]["task_id"] == task.task_id
    assert task.state == "unknown"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_confirmed"] is False
    assert reconciliation["runtime_task_id"] == "rtask_fake_0001"
    assert reconciliation["runtime_key"] == submit_key
    # The original unknown reason is preserved, not overwritten.
    assert reconciliation["reason"] == RUNTIME_EXECUTION_LOST
    assert len(adapter.submit_calls) == 1
    # The reconciliation write was fenced by a recovery lease, released
    # afterwards; no stale writer can claim the row.
    leases = _leases(session, task)
    assert leases[-1].released_at is not None
    # No prompt or credential is persisted by the cancel pass.
    prompt = adapter.submit_calls[0][3]
    serialised = canonical_json(task.result)
    assert prompt not in serialised
    assert "completion_criteria" not in serialised


def test_unknown_with_confirmed_stop_keeps_unknown_and_saves_evidence(
    session: Session, world: TaskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.UNKNOWN),
        ],
        cancel=CancelResult(
            outcome=CancelOutcome.STOPPED,
            stop_confirmed=True,
            observed_at=utcnow(),
        ),
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)
    assert dispatcher.run_once().outcome == "unknown"

    def _forbidden_confirm_stop(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "unknown must not be auto-reconciled to cancelled"
        )

    monkeypatch.setattr(task_service, "confirm_stop", _forbidden_confirm_stop)

    results = dispatcher.cancel_unknown_runs()

    # A runtime-confirmed stop is evidence only: the task stays unknown for a
    # human to reconcile, and the evidence is saved.
    assert [item.outcome for item in results] == ["unknown"]
    assert task.state == "unknown"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["stop_confirmed"] is True
    assert reconciliation["runtime_outcome"] == "stopped"
    assert reconciliation["stop_evidence_refs"][0].startswith(
        "evidence:runtime_stop_"
    )
    assert len(adapter.cancel_calls) == 1
    assert _leases(session, task)[-1].released_at is not None


def test_unknown_best_effort_cancel_preserves_the_confirming_status(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.UNKNOWN),
            _status(RuntimeState.FAILED, stop_confirmed=True),
        ],
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)
    assert dispatcher.run_once().outcome == "unknown"

    results = dispatcher.cancel_unknown_runs()

    # The runtime's confirming status is kept as the evidence label; it must
    # not be rewritten to "stopped" just because the cancel path produced it.
    assert [item.outcome for item in results] == ["unknown"]
    assert task.state == "unknown"
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["stop_confirmed"] is True
    assert reconciliation["runtime_outcome"] == "failed"
    assert reconciliation["stop_evidence_refs"][0].startswith(
        "evidence:runtime_stop_"
    )
    assert len(adapter.cancel_calls) == 1
    assert _leases(session, task)[-1].released_at is not None


def _prepare_cancel_requested_task(
    session: Session,
    world: TaskWorld,
    *,
    started: datetime,
    lease_seconds: int = 5,
) -> tuple[Task, GovernancePrincipal]:
    """Create a real ``cancel_requested`` row via the production services."""

    task = submit_managed(session, world)
    session.commit()
    principal = _service_principal(session, world)
    claimed = worker.claim_task(
        session,
        worker_id=WORKER_ID,
        actor=principal,
        execution_mode="managed_execution",
        lease_seconds=lease_seconds,
        now=started,
    )
    assert claimed is not None
    assert claimed.task.task_id == task.task_id
    task_service.start_attempt(
        session,
        task_id=task.task_id,
        actor=principal,
        worker_id=WORKER_ID,
        fencing_token=claimed.lease.fencing_token,
        runtime_ref="rtask_fake_0001",
        idempotency_key="idem_cancel_crash_0001",
        now=started,
    )
    task_service.request_cancel(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
        now=started,
    )
    session.commit()
    return task, principal


def test_cancel_requested_crash_pass_escalates_without_live_lease(
    session: Session, world: TaskWorld
) -> None:
    started = utcnow()
    task, _principal = _prepare_cancel_requested_task(
        session, world, started=started
    )
    assert task.state == "cancel_requested"
    expired = started + timedelta(seconds=6)
    adapter = FakeRuntimeAdapter(
        status=_status(RuntimeState.STOPPED, stop_confirmed=True),
        cancel=CancelResult(
            outcome=CancelOutcome.STOPPED,
            stop_confirmed=True,
            observed_at=utcnow(),
        ),
    )
    dispatcher = _dispatcher(session, world, adapter=adapter)

    results = dispatcher.reconcile_cancel_requests(now=expired)

    # A cancel stranded by a crashed/expired lease is reconciled under a fresh
    # recovery lease.  The confirmed stop is evidence, not reconciliation.
    assert [item.outcome for item in results] == ["needs_human"]
    assert task.state == "needs_human"
    assert task.needs_human_origin == "cancel_requested"
    assert task.stop_confirmed is False
    assert task.result.get("side_effects_reconciled") is not True
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["stop_confirmed"] is True
    assert reconciliation["runtime_outcome"] == "stopped"
    assert reconciliation["stop_evidence_refs"][0].startswith(
        "evidence:runtime_stop_"
    )
    escalation = next(
        event
        for event in _task_events(session, task)
        if event.event_type == "human.escalation_required"
    )
    assert escalation.payload["evidence_refs"] == (
        reconciliation["stop_evidence_refs"]
    )
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_calls[0]["reason"] == RUNTIME_CANCEL_REQUESTED
    assert adapter.submit_calls == []
    # The expired claim lease and the recovery lease are both released.
    _assert_lease_released(session, task)


def test_cancel_requested_crash_pass_live_runtime_escalates(
    session: Session, world: TaskWorld
) -> None:
    started = utcnow()
    task, _principal = _prepare_cancel_requested_task(
        session, world, started=started
    )
    expired = started + timedelta(seconds=6)
    adapter = FakeRuntimeAdapter(status=_status(RuntimeState.RUNNING))
    dispatcher = _dispatcher(session, world, adapter=adapter)

    results = dispatcher.reconcile_cancel_requests(now=expired)

    # The runtime is still live and the cancel unconfirmed.  The row must not
    # stay pending forever: it is handed to a human, never marked cancelled.
    assert [item.outcome for item in results] == ["needs_human"]
    assert task.state == "needs_human"
    assert task.needs_human_origin == "cancel_requested"
    assert task.stop_confirmed is False
    reconciliation = task.result["runtime_reconciliation"]
    assert reconciliation["cancel_outcome"] == "requested"
    assert reconciliation["stop_confirmed"] is False
    assert "stop_evidence_refs" not in reconciliation
    assert len(adapter.cancel_calls) == 1
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


def test_cancel_requested_crash_pass_without_handle_escalates(
    session: Session, world: TaskWorld
) -> None:
    task = submit_managed(session, world)
    session.commit()
    task_service.request_cancel(
        session,
        task_id=task.task_id,
        actor=world.actors.requester,
    )
    session.commit()
    assert task.state == "cancel_requested"
    assert task.runtime_task_id is None
    adapter = FakeRuntimeAdapter()
    dispatcher = _dispatcher(session, world, adapter=adapter)

    results = dispatcher.reconcile_cancel_requests()

    # No runtime run was ever bound: there is nothing to cancel, but the row
    # cannot stay silently pending.  A human decides.
    assert [item.outcome for item in results] == ["needs_human"]
    assert task.state == "needs_human"
    assert task.needs_human_origin == "cancel_requested"
    assert adapter.cancel_calls == []
    assert adapter.status_calls == []
    assert adapter.submit_calls == []
    _assert_lease_released(session, task)


def test_reconcile_cancel_requests_skips_a_live_lease(
    session: Session, world: TaskWorld
) -> None:
    started = utcnow()
    task, _principal = _prepare_cancel_requested_task(
        session, world, started=started
    )
    adapter = FakeRuntimeAdapter()
    dispatcher = _dispatcher(session, world, adapter=adapter)

    # The claim lease is still live: a driving pass may own the row, so the
    # reconciliation pass must not touch it.
    assert dispatcher.reconcile_cancel_requests(now=started) == []
    assert task.state == "cancel_requested"
    assert adapter.cancel_calls == []
    assert adapter.status_calls == []


def test_only_unknown_tasks_are_best_effort_cancelled(
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
    dispatcher = _dispatcher(session, world, adapter=adapter)
    assert dispatcher.run_once().outcome == "verifying"
    # A reconciliation block alone is not a reason to cancel a verifying task.
    task.result = {
        **dict(task.result or {}),
        "runtime_reconciliation": {
            "runtime_task_id": "rtask_fake_0001",
            "runtime_key": adapter.submit_calls[0][1],
            "stop_confirmed": False,
        },
    }
    session.flush()

    assert dispatcher.cancel_unknown_runs() == []

    assert task.state == "verifying"
    assert adapter.cancel_calls == []


def test_run_forever_runs_lease_recovery_and_cancel_reconciliation(
    session: Session,
    world: TaskWorld,
    engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = threading.Event()
    seen: dict[str, object] = {}

    def _recover(
        db: Session,
        *,
        actor: object = None,
        checker: object = None,
        now: object = None,
        limit: int = 50,
    ) -> list[object]:
        seen["checker"] = checker
        seen["actor"] = actor
        return []

    monkeypatch.setattr(worker, "recover_expired_leases", _recover)
    def _reconcile_cancels(
        self: RuntimeDispatcher, **kwargs: object
    ) -> list:
        seen["cancel_pass"] = True
        return []

    monkeypatch.setattr(
        RuntimeDispatcher, "reconcile_cancel_requests", _reconcile_cancels
    )
    def _cancel_unknown(self: RuntimeDispatcher, **kwargs: object) -> list:
        seen["unknown_pass"] = True
        stop_event.set()
        return []

    monkeypatch.setattr(
        RuntimeDispatcher, "cancel_unknown_runs", _cancel_unknown
    )

    _dispatcher(session, world).run_forever(
        stop_event,
        session_factory=lambda: Session(engine),  # type: ignore[arg-type]
    )

    assert isinstance(seen["checker"], RuntimeRecoveryChecker)
    assert seen["actor"] is not None
    assert seen["cancel_pass"] is True
    assert seen["unknown_pass"] is True


def test_run_forever_stops_driving_when_the_stop_event_is_set(
    session: Session, world: TaskWorld, engine: object
) -> None:
    task = submit_managed(session, world)
    session.commit()
    stop_event = threading.Event()
    adapter = _StopOnStatusAdapter(
        stop_event, status=_status(RuntimeState.RUNNING)
    )

    _dispatcher(session, world, adapter=adapter).run_forever(
        stop_event,
        session_factory=lambda: Session(engine),  # type: ignore[arg-type]
    )

    # The shutdown signal is honoured between observations: the current unit of
    # work rolls back after the committed `running` fact, so the run is not
    # replayed and its still-live lease lets recovery reconcile it.  The
    # runtime is not cancelled (shutdown is not a core cancel).
    fresh = Session(engine)  # type: ignore[arg-type]
    try:
        reloaded = fresh.scalar(
            select(Task).where(Task.task_id == task.task_id)
        )
        assert reloaded is not None
        assert reloaded.state == "running"
        leases = list(
            fresh.scalars(
                select(TaskLease).where(
                    TaskLease.task_id == task.task_id
                )
            )
        )
        assert leases
        assert leases[-1].released_at is None
    finally:
        fresh.close()
    assert len(adapter.submit_calls) == 1
    assert adapter.cancel_calls == []


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
        status=[
            _status(RuntimeState.RUNNING, runtime_task_id=adopted),
            _status(RuntimeState.COMPLETED, runtime_task_id=adopted),
        ],
        result=_result(),
    )

    result = _dispatcher(session, world, adapter=adapter).run_once()

    # The lost accepted reply is recovered from the lookup, bound exactly like a
    # direct acceptance (same attempt number, key, worker and fencing), and then
    # driven in the same pass to a terminal fact.
    assert result.outcome == "verifying"
    assert result.task_id == task.task_id
    assert result.runtime_task_id == adopted
    assert task.state == "verifying"
    assert task.attempt == 1
    assert task.runtime_task_id == adopted
    assert task.execution_ref is None
    attempt = _attempt(session, task)
    assert attempt.runtime_ref == adopted
    assert attempt.idempotency_key == expected_key
    assert attempt.execution_ref is None
    assert _leases(session, task)[-1].released_at is not None
    # Exactly one submission and one lookup; adoption created no second run.
    assert len(adapter.submit_calls) == 1
    assert adapter.submit_calls[0][1] == expected_key
    assert adapter.lookup_calls == [
        (task.task_id, task.grant_id, expected_key, None)
    ]
    assert adapter.created_runtime_task_ids == []
    assert adapter.status_calls == [
        (adopted, task.task_id),
        (adopted, task.task_id),
    ]
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
    adapter = FakeRuntimeAdapter(
        status=[
            _status(RuntimeState.RUNNING),
            _status(RuntimeState.COMPLETED),
        ],
        result=_result(),
    )

    result = _dispatcher(
        session, world, adapter=adapter, principal=runtime_principal
    ).run_once()

    assert result.outcome == "verifying"
    assert result.task_id == task.task_id
    assert task.state == "verifying"
    assert _leases(session, task)[-1].released_at is not None


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


# -- hardening: recovery paging, loop backoff, attempt-scoped facts -----------


class _StubPrincipal:
    """Minimal trusted-service actor for DB-free unit checks.

    ``recover_leases`` and ``_persist_reconciliation`` only read the principal
    type/id and call ``require_active``; no governance row is involved.
    """

    principal_type = "service"
    principal_id = "svc_dispatcher_unit"

    def require_active(self) -> None:
        return None


class _StubSession:
    """Session stand-in for helpers that only commit/rollback."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "_StubSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _unit_dispatcher(
    session: object | None = None,
    *,
    config: DispatcherConfig | None = None,
) -> RuntimeDispatcher:
    return RuntimeDispatcher(
        FakeRuntimeAdapter(),
        config or DispatcherConfig(worker_id=WORKER_ID, poll_interval=0.0),
        service_principal=_StubPrincipal(),  # type: ignore[arg-type]
        session=session or _StubSession(),  # type: ignore[arg-type]
    )


def test_recover_leases_drains_every_batch_without_a_fixed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [object() for _ in range(5)]
    limits: list[int] = []

    def _recover(
        db: object,
        *,
        actor: object = None,
        checker: object = None,
        now: object = None,
        limit: int = 50,
    ) -> list[object]:
        limits.append(limit)
        batch, pending[:] = pending[:2], pending[2:]
        return batch

    monkeypatch.setattr(worker, "recover_expired_leases", _recover)
    session = _StubSession()

    recovered = _unit_dispatcher(session).recover_leases()

    # Two full batches plus the empty probe: every expired lease is reached in
    # the same pass, and each batch is committed before the next read.
    assert len(recovered) == 5
    assert len(limits) == 4
    assert len(set(limits)) == 1
    assert limits[0] > 50
    assert session.commits == 4
    assert session.rollbacks == 0


def test_recover_leases_honours_an_explicit_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def _recover(
        db: object,
        *,
        actor: object = None,
        checker: object = None,
        now: object = None,
        limit: int = 50,
    ) -> list[object]:
        calls.append(limit)
        return [object() for _ in range(limit)]

    monkeypatch.setattr(worker, "recover_expired_leases", _recover)

    recovered = _unit_dispatcher().recover_leases(limit=3)

    assert len(recovered) == 3
    assert calls == [3]


class _RecordingStopEvent(threading.Event):
    """Stop event that records loop delays and stops after ``after`` waits."""

    def __init__(self, after: int = 1) -> None:
        super().__init__()
        self.after = after
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(0.0 if timeout is None else timeout)
        if len(self.waits) >= self.after:
            self.set()
        return self.is_set()


def test_run_forever_backs_off_on_a_recoverable_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(self: RuntimeDispatcher, **kwargs: object) -> list[object]:
        raise TaskError("task_not_found")

    monkeypatch.setattr(RuntimeDispatcher, "recover_leases", _boom)
    stop_event = _RecordingStopEvent(after=3)
    config = DispatcherConfig(
        worker_id=WORKER_ID,
        poll_interval=0.0,
        error_backoff_seconds=0.25,
        max_error_backoff_seconds=10.0,
    )

    with caplog.at_level("WARNING", logger="app.tasks.execution"):
        _unit_dispatcher(config=config).run_forever(
            stop_event,
            session_factory=lambda: _StubSession(),  # type: ignore[return-value]
        )

    # A domain/lease error is retried, not fatal: the delay doubles per
    # consecutive failing pass (no busy loop) and only a safe code is logged.
    assert stop_event.waits == [0.25, 0.5, 1.0]
    assert "runtime_dispatch_pass_error" in caplog.text
    assert "task_not_found" in caplog.text
    assert "svc_dispatcher_unit" not in caplog.text


def test_run_forever_propagates_a_programming_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(self: RuntimeDispatcher, **kwargs: object) -> list[object]:
        raise TypeError("dispatcher bug")

    monkeypatch.setattr(RuntimeDispatcher, "recover_leases", _boom)
    stop_event = _RecordingStopEvent(after=1)

    with pytest.raises(TypeError, match="dispatcher bug"):
        _unit_dispatcher().run_forever(
            stop_event,
            session_factory=lambda: _StubSession(),  # type: ignore[return-value]
        )

    assert stop_event.waits == []


def test_run_forever_requires_a_session_factory() -> None:
    with pytest.raises(ValueError, match="session_factory"):
        _unit_dispatcher().run_forever(threading.Event())


def test_reconciliation_facts_do_not_cross_attempts() -> None:
    dispatcher = _unit_dispatcher()
    task = SimpleNamespace(
        result={
            "runtime_reconciliation": {
                "runtime_task_id": "rtask_old_0001",
                "runtime_key": "idem_old_attempt",
                "stop_confirmed": True,
                "stop_evidence_refs": ["evidence:runtime_stop_old"],
                "runtime_outcome": "stopped",
                "lookup_outcome": "found",
                "submit_outcome": "timeout",
            }
        }
    )

    dispatcher._persist_reconciliation(
        task,  # type: ignore[arg-type]
        runtime_task_id="rtask_new_0001",
        idempotency_key="idem_new_attempt",
        cancel={
            "attempted": True,
            "outcome": "requested",
            "stop_confirmed": False,
        },
        reason="runtime_submit_timeout",
        now=utcnow(),
    )

    document = task.result["runtime_reconciliation"]
    assert document["runtime_task_id"] == "rtask_new_0001"
    assert document["runtime_key"] == "idem_new_attempt"
    # The previous attempt's stop/lookup facts must not make the fresh run
    # look already stopped (or already decided) before it is submitted.
    assert document["stop_confirmed"] is False
    assert "stop_evidence_refs" not in document
    assert "runtime_outcome" not in document
    assert "lookup_outcome" not in document
    assert "submit_outcome" not in document


def test_reconciliation_keeps_same_attempt_facts() -> None:
    dispatcher = _unit_dispatcher()
    task = SimpleNamespace(
        result={
            "runtime_reconciliation": {
                "runtime_task_id": "rtask_fake_0001",
                "runtime_key": "idem_same_attempt",
                "stop_confirmed": True,
                "stop_evidence_refs": ["evidence:runtime_stop_same"],
                "runtime_outcome": "failed",
                "lookup_outcome": "found",
            }
        }
    )

    dispatcher._persist_reconciliation(
        task,  # type: ignore[arg-type]
        runtime_task_id="rtask_fake_0001",
        idempotency_key="idem_same_attempt",
        cancel={"attempted": True, "outcome": "requested"},
        reason="runtime_execution_lost",
        now=utcnow(),
    )

    document = task.result["runtime_reconciliation"]
    assert document["stop_confirmed"] is True
    assert document["stop_evidence_refs"] == ["evidence:runtime_stop_same"]
    assert document["runtime_outcome"] == "failed"
    assert document["lookup_outcome"] == "found"


# -- CLI ---------------------------------------------------------------------


def test_cli_without_trusted_configuration_fails_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--once", "--adapter", "fake"]) != 0

    err = capsys.readouterr().err.lower()
    assert "#97 production dispatch configuration not supplied" in err


def test_cli_rejects_unknown_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            ["--once", "--adapter", "unknown"],
            dispatcher_factory=lambda name: None,
        )
        != 0
    )

    err = capsys.readouterr().err.lower()
    assert "unknown adapter" in err
    assert "unknown" in err


def test_cli_once_uses_injected_dispatcher(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    class _OnceDispatcher:
        def run_once(self, **kwargs: object) -> DispatcherResult:
            calls.append("run_once")
            return DispatcherResult(outcome="idle")

    def factory(name: str) -> object | None:
        return _OnceDispatcher() if name == "fake" else None

    assert (
        main(
            ["--once", "--adapter", "fake"],
            dispatcher_factory=factory,  # type: ignore[arg-type]
        )
        == 0
    )
    assert calls == ["run_once"]
    assert capsys.readouterr().err == ""


def test_cli_loop_runs_until_the_dispatcher_stops(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[bool] = []

    class _LoopDispatcher:
        def run_forever(self, stop_event: threading.Event) -> None:
            calls.append(stop_event.is_set())
            stop_event.set()

    def factory(name: str) -> object | None:
        return _LoopDispatcher() if name == "fake" else None

    assert (
        main(
            ["--loop", "--adapter", "fake"],
            dispatcher_factory=factory,  # type: ignore[arg-type]
        )
        == 0
    )
    assert calls == [False]
    assert capsys.readouterr().err == ""


def test_cli_requires_exactly_one_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory_calls: list[str] = []

    def factory(name: str) -> object | None:
        factory_calls.append(name)
        return None

    assert (
        main(["--adapter", "fake"], dispatcher_factory=factory) != 0
    )
    assert (
        main(
            ["--once", "--loop", "--adapter", "fake"],
            dispatcher_factory=factory,
        )
        != 0
    )
    # A malformed invocation never reaches the trusted launcher.
    assert factory_calls == []
    capsys.readouterr()


def test_cli_failure_reports_only_a_safe_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FailingDispatcher:
        def run_once(self, **kwargs: object) -> DispatcherResult:
            raise RuntimeError("token=secret /srv/operator/state.sqlite3")

    def factory(name: str) -> object | None:
        return _FailingDispatcher() if name == "fake" else None

    assert (
        main(
            ["--once", "--adapter", "fake"],
            dispatcher_factory=factory,  # type: ignore[arg-type]
        )
        != 0
    )

    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "secret" not in err
    assert "/srv" not in err


def test_cli_does_not_fall_back_to_a_fake_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # ``hermes`` is rejected before #97 supplies a real managed adapter: the
    # CLI has no built-in registry entry for any name.
    assert main(["--once", "--adapter", "hermes"]) != 0
    assert "#97" in capsys.readouterr().err.lower()
