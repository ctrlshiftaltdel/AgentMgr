"""End-to-end dispatcher tests for the Hermes managed adapter (Issue #97).

These tests are the only place where the real Task 5
:class:`~app.runtime.hermes_managed.HermesManagedAdapter`, the real #96
:class:`~app.tasks.execution.RuntimeDispatcher`, the real PostgreSQL task
fixtures (task service, Grant, lease and fencing) and a scripted
:class:`~app.tests.tasks.fake_hermes_supervisor.FakeHermesSupervisor` meet.

The fake replaces only the privileged root helper.  Every state transition,
Grant check, lease/fencing token, attempt row and completion criterion is the
production code path.

What is pinned here:

* an artifact-backed managed run stops at ``verifying`` -- never ``succeeded``
  -- and only an independent ``complete_task`` with an ``artifact_store``
  evidence record moves it to ``succeeded``;
* a confirmed runtime stop while a core cancel is pending escalates to
  ``needs_human`` without claiming side-effect reconciliation;
* an ambiguous launch is ``unknown`` and is never replayed;
* a duplicate submit with the same key/payload reuses the single unit;
* a timed-out run is recorded as ``timed_out`` and never as success;
* an expired or retargeted Grant fails closed before any sandbox call;
* a restarted adapter re-discovers the durable manifest/run;
* a degraded adapter health check is ``adapter_unavailable`` and prepares
  nothing;
* the dispatcher never calls ``complete_task``.
"""

from __future__ import annotations

import hashlib
import socket
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.governance.models import ExecutionGrant
from app.governance.principal import (
    GovernancePrincipal,
    resolve_service_principal,
)
from app.runtime.contract import (
    LookupOutcome,
    ResultOutcome,
    RuntimeState,
    SubmitOutcome,
)
from app.runtime.hermes_managed import (
    HermesManagedAdapter,
    HermesManagedConfig,
    SupervisorError,
)
from app.runtime.projection import project_runtime_grant
from app.tasks import service as task_service
from app.tasks import worker
from app.tasks.errors import TaskError
from app.tasks.execution import (
    RUNTIME_STOP_CONFIRMED,
    DispatcherConfig,
    RuntimeDispatcher,
    build_runtime_idempotency_key,
    build_runtime_prompt,
)
from app.tasks.models import Task, TaskLease
from app.tasks.verification import EvidenceRecord
from app.tests.governance.helpers import make_service_identity
from app.tests.tasks.conftest import TaskWorld
from app.tests.tasks.fake_hermes_supervisor import (
    NEVER_STARTED,
    RUNNING,
    STOPPED,
    TIMED_OUT_GONE,
    UNLOADED_SUCCESS,
    FakeHermesSupervisor,
)
from app.tests.tasks.helpers import submit_managed, utcnow

WORKER_ID = "dispatcher-hermes-test-1"

ATTESTATION_KEY = b"a" * 32
EGRESS_KEY = b"e" * 32
RUNTIME_ROOT_HASH = "sha256:" + "1" * 64
RUNTIME_EVIDENCE_REF = "evidence:hermes:managed:dispatch-test-0001"

#: The artifact the fake workload writes and the host adapter hashes.
ARTIFACT_BYTES = b"OK"
ARTIFACT_DIGEST = "sha256:" + hashlib.sha256(ARTIFACT_BYTES).hexdigest()
ARTIFACT_HEX = ARTIFACT_DIGEST.removeprefix("sha256:")
ARTIFACT_RESULT_REF = f"result_{ARTIFACT_HEX[:32]}"
ARTIFACT_EVIDENCE_REF = f"evidence:hermes_artifact_{ARTIFACT_HEX[:32]}"

VERIFIER_ID = "ver_hermes_artifact_0001"
VERIFIER_VERSION = "1.0.0"
ARTIFACT_CRITERION_ID = "crit_hermes_artifact_0001"


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture()
def broker_socket(tmp_path: Path) -> Iterator[Path]:
    """A real Unix socket file so ``HermesManagedAdapter.health`` is healthy."""

    path = tmp_path / "egress.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    try:
        yield path
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def managed_config(tmp_path: Path) -> HermesManagedConfig:
    """Deployment-shaped, secret-free configuration rooted in ``tmp_path``."""

    helper = tmp_path / "bin" / "agentmgr-hermes-exec"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(exist_ok=True)
    (tmp_path / "attestation.key").write_bytes(ATTESTATION_KEY)
    (tmp_path / "egress.key").write_bytes(EGRESS_KEY)
    (tmp_path / "revoked").mkdir(exist_ok=True)
    return HermesManagedConfig(
        helper_path=helper,
        runtime_root=runtime_root,
        runtime_root_hash=RUNTIME_ROOT_HASH,
        task_root=tmp_path / "tasks",
        broker_socket=tmp_path / "egress.sock",
        attestation_key_path=tmp_path / "attestation.key",
        egress_token_key_path=tmp_path / "egress.key",
        runtime_attestation_path=tmp_path / "runtime-attestation.json",
        evidence_ref=RUNTIME_EVIDENCE_REF,
        runtime_name="hermes",
        runtime_version="0.22.1",
        model_name="model-x",
        egress_provider_host="provider.example",
        worker_id=WORKER_ID,
        revocation_dir=tmp_path / "revoked",
    )


def artifact_goal() -> dict[str, object]:
    """A goal whose only criterion is the host-hashed managed artifact."""

    return {
        "summary": "Write one managed Hermes artifact",
        "completion_criteria": [
            {
                "criterion_id": ARTIFACT_CRITERION_ID,
                "type": "artifact_hash",
                "verifier": {
                    "verifier_id": VERIFIER_ID,
                    "version": VERIFIER_VERSION,
                },
                "resource_ref": "res_hermes_artifact_0001",
                "expected": ARTIFACT_DIGEST,
                "evidence_ref": ARTIFACT_EVIDENCE_REF,
                "evidence_hash": ARTIFACT_DIGEST,
            }
        ],
        "required_inputs": [],
        "out_of_scope": ["No host access."],
    }


def artifact_evidence() -> EvidenceRecord:
    """The independent verifier's record for ``out/result.txt``."""

    return EvidenceRecord(
        criterion_id=ARTIFACT_CRITERION_ID,
        verifier_id=VERIFIER_ID,
        verifier_version=VERIFIER_VERSION,
        evidence_ref=ARTIFACT_EVIDENCE_REF,
        observed=ARTIFACT_DIGEST,
        evidence_hash=ARTIFACT_DIGEST,
        source="artifact_store",
    )


def service_principal(
    session: Session, world: TaskWorld
) -> GovernancePrincipal:
    """A synthetic but real active service identity (never a person)."""

    identity = make_service_identity(session, owner=world.org.admin)
    return resolve_service_principal(session, identity.id)


def dispatcher_for(
    session: Session,
    world: TaskWorld,
    adapter: HermesManagedAdapter,
) -> RuntimeDispatcher:
    return RuntimeDispatcher(
        adapter,
        DispatcherConfig(
            worker_id=WORKER_ID,
            poll_interval=0.0,
            max_status_observations=8,
        ),
        service_principal=service_principal(session, world),
        session=session,
    )


def persisted_grant(session: Session, task: Task) -> ExecutionGrant:
    grant = session.scalar(
        select(ExecutionGrant).where(ExecutionGrant.grant_id == task.grant_id)
    )
    assert grant is not None
    return grant


def request_cancel_on_another_connection(
    engine: Engine,
    *,
    task_id: str,
    actor: GovernancePrincipal,
) -> None:
    """Commit a real cancel request on a second connection.

    The drive commits after every observation, so the task row must not be
    locked while the adapter reports status.  A thread with its own
    ``Session``/connection proves that property (a same-session mutation would
    say nothing).
    """

    errors: list[BaseException] = []

    def _run() -> None:
        try:
            other = Session(engine)
            try:
                task_service.request_cancel(other, task_id=task_id, actor=actor)
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
    assert not thread.is_alive(), "the drive held the task row lock across status"


def release_lease_on_another_connection(
    engine: Engine,
    *,
    task_id: str,
) -> None:
    """Release the live task lease from a second connection.

    Used to prove a stale pass cannot write a managed fact: the real fencing
    token check rejects the write instead of the test mutating the row behind
    the dispatcher.
    """

    errors: list[BaseException] = []

    def _run() -> None:
        try:
            other = Session(engine)
            try:
                lease = other.scalar(
                    select(TaskLease)
                    .where(
                        TaskLease.task_id == task_id,
                        TaskLease.released_at.is_(None),
                    )
                    .order_by(TaskLease.id.desc())
                )
                assert lease is not None
                worker.release_lease(
                    other,
                    task_id=task_id,
                    worker_id=lease.worker_id,
                    fencing_token=lease.fencing_token,
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
    assert not thread.is_alive(), "the drive held the lease row lock across status"


def drive_success(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    *,
    result_bytes: bytes = ARTIFACT_BYTES,
) -> tuple[Task, HermesManagedConfig, FakeHermesSupervisor, HermesManagedAdapter]:
    """Submit one managed task and drive the scripted unloaded-success unit."""

    task = submit_managed(session, world, goal=artifact_goal())
    session.commit()
    config = managed_config(tmp_path)
    supervisor = FakeHermesSupervisor(
        config, result_bytes=result_bytes, status_script=[UNLOADED_SUCCESS]
    )
    adapter = HermesManagedAdapter(config, supervisor)
    result = dispatcher_for(session, world, adapter).run_once()

    session.refresh(task)
    assert result.outcome == "verifying", result.detail
    assert task.state == "verifying"
    return task, config, supervisor, adapter


# ---------------------------------------------------------------------------
# success path and independent verification
# ---------------------------------------------------------------------------


def test_dispatcher_drives_managed_task_to_verifying_and_verifier_completes(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task, config, supervisor, _ = drive_success(session, world, tmp_path)

    # The unit ended in the normal systemd unload-after-success state
    # (active_state="not-found" while started=True, exit_code=0,
    # process_gone=True); the adapter maps that to COMPLETED and hashes the
    # artifact host-side.
    assert supervisor.launch_calls == 1
    assert supervisor.prepare_calls == 1
    assert supervisor.status_calls >= 1
    assert len(supervisor.unit_dirs()) == 1
    assert task.state != "succeeded"
    assert task.runtime_task_id is not None
    assert task.runtime_task_id.startswith("rtask_hermes_")

    document = dict(task.result or {})
    assert document["outcome"] == "completed"
    assert document["result_ref"] == ARTIFACT_RESULT_REF
    assert document["evidence_refs"] == [ARTIFACT_EVIDENCE_REF]
    assert document["verification"]["result"] == "pending"

    # The artifact really exists under the trusted task root and has the
    # digest the adapter reported.
    unit_dir = supervisor.unit_dirs()[0]
    artifact = unit_dir / "out" / "result.txt"
    assert artifact.read_bytes() == ARTIFACT_BYTES
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == ARTIFACT_HEX

    # The dispatcher must not be the entity that certifies completion.
    assert task.state == "verifying"

    task_service.complete_task(
        session,
        task_id=task.task_id,
        actor=world.actors.owner,
        result_ref=document["result_ref"],
        evidence_refs=list(document["evidence_refs"]),
        evidence=[artifact_evidence()],
    )
    session.commit()
    session.refresh(task)

    assert task.state == "succeeded"
    assert task.result["outcome"] == "passed"
    assert task.result["all_criteria_passed"] is True


# ---------------------------------------------------------------------------
# cancel / stop
# ---------------------------------------------------------------------------


def test_dispatcher_cancel_confirmed_routes_to_needs_human(
    session: Session,
    world: TaskWorld,
    engine: Engine,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    config = managed_config(tmp_path)
    supervisor = FakeHermesSupervisor(
        config, status_script=[RUNNING, RUNNING, STOPPED]
    )
    cancelled: dict[str, bool] = {"requested": False}

    def _hook(unit: str, call: int) -> None:
        if call == 1 and not cancelled["requested"]:
            cancelled["requested"] = True
            request_cancel_on_another_connection(
                engine, task_id=task.task_id, actor=world.actors.requester
            )

    supervisor.status_hook = _hook
    adapter = HermesManagedAdapter(config, supervisor)

    result = dispatcher_for(session, world, adapter).run_once()
    session.refresh(task)

    assert cancelled["requested"] is True
    # A runtime-confirmed stop is evidence, not business reconciliation.
    assert result.outcome == "needs_human"
    assert result.detail == RUNTIME_STOP_CONFIRMED
    assert task.state == "needs_human"
    assert task.needs_human_origin == "cancel_requested"
    assert task.stop_confirmed is False
    assert (task.result or {}).get("side_effects_reconciled") is not True
    assert (task.result or {}).get("outcome") != "cancelled"
    assert supervisor.stop_calls == 1
    assert supervisor.stop_reasons == ["runtime_cancel_requested"]
    # The unit was stopped exactly once and never re-launched.
    assert supervisor.launch_calls == 1


# ---------------------------------------------------------------------------
# ambiguous launch / duplicate submit
# ---------------------------------------------------------------------------


def test_dispatcher_ambiguous_launch_does_not_replay(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    config = managed_config(tmp_path)
    supervisor = FakeHermesSupervisor(config, status_script=[NEVER_STARTED])
    supervisor.launch_error = SupervisorError(
        "started_not_confirmed", ambiguous=True
    )
    adapter = HermesManagedAdapter(config, supervisor)

    dispatcher = dispatcher_for(session, world, adapter)
    result = dispatcher.run_once()
    session.refresh(task)

    assert result.outcome == "unknown"
    assert result.detail == "runtime_submit_unknown"
    assert task.state == "unknown"
    assert task.state != "succeeded"
    # The launch may already have created a unit: one launch, best-effort stop,
    # no replay, and the durable manifest still describes exactly one unit.
    assert supervisor.launch_calls == 1
    assert supervisor.prepare_calls == 1
    assert supervisor.stop_calls == 1
    assert supervisor.cleanup_calls == [(supervisor.launch_specs[0].unit, False)]
    assert len(supervisor.unit_dirs()) == 1

    # A second pass cannot claim the task again.
    assert dispatcher.run_once().outcome == "idle"
    assert supervisor.launch_calls == 1


def test_dispatcher_duplicate_submit_reuses_one_unit(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task, config, supervisor, adapter = drive_success(session, world, tmp_path)
    grant = project_runtime_grant(
        task, persisted_grant(session, task), now=utcnow()
    )
    prompt = build_runtime_prompt(task)
    key = build_runtime_idempotency_key(task, grant, 1)

    replayed = adapter.submit_task(task.task_id, prompt, key, grant)
    session.commit()

    # Same key and payload: the durable helper manifest is the idempotency
    # record, so no second prepare/launch/unit happens.
    assert replayed.outcome is SubmitOutcome.ACCEPTED
    assert replayed.replayed is True
    assert replayed.runtime_task_id == task.runtime_task_id
    assert supervisor.prepare_calls == 1
    assert supervisor.launch_calls == 1
    assert len(supervisor.unit_dirs()) == 1


# ---------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------


def test_dispatcher_timed_out_run_never_succeeds(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task = submit_managed(session, world, goal=artifact_goal())
    session.commit()
    config = managed_config(tmp_path)
    # A workload that wrote a partial artifact and then declared its own
    # timeout: the real adapter returns the hashed artifact as evidence, but
    # the dispatcher records the confirmed timeout, not success.
    supervisor = FakeHermesSupervisor(
        config,
        result_bytes=b"partial",
        status_script=[TIMED_OUT_GONE],
    )
    adapter = HermesManagedAdapter(config, supervisor)

    result = dispatcher_for(session, world, adapter).run_once()
    session.refresh(task)

    assert result.outcome == "timed_out"
    assert task.state == "timed_out"
    assert task.state != "succeeded"
    assert task.result["outcome"] == "timed_out"
    assert task.result["stop_confirmed"] is True
    assert supervisor.launch_calls == 1


# ---------------------------------------------------------------------------
# fail-closed Grant checks
# ---------------------------------------------------------------------------


def test_expired_grant_never_reaches_the_sandbox(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    config = managed_config(tmp_path)
    supervisor = FakeHermesSupervisor(config)
    adapter = HermesManagedAdapter(config, supervisor)

    result = dispatcher_for(session, world, adapter).run_once(
        now=utcnow() + timedelta(hours=1)
    )
    session.refresh(task)

    assert result.outcome == "unavailable"
    assert result.detail == "grant_expired"
    assert task.state == "failed"
    assert task.grant_status == "revoked"
    assert supervisor.prepare_calls == 0
    assert supervisor.launch_calls == 0
    assert supervisor.unit_dirs() == []


def test_grant_task_mismatch_never_reaches_the_sandbox(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    grant = persisted_grant(session, task)
    # Keep the row structurally valid but no longer bound to this task.
    grant.task_id = "task_hermes_other_0001"
    session.commit()
    config = managed_config(tmp_path)
    supervisor = FakeHermesSupervisor(config)
    adapter = HermesManagedAdapter(config, supervisor)

    result = dispatcher_for(session, world, adapter).run_once()
    session.refresh(task)

    assert result.outcome == "unavailable"
    assert result.detail == "grant_task_mismatch"
    assert task.state == "failed"
    assert task.grant_status == "revoked"
    assert supervisor.prepare_calls == 0
    assert supervisor.launch_calls == 0
    assert supervisor.unit_dirs() == []


# ---------------------------------------------------------------------------
# stale lease / fencing
# ---------------------------------------------------------------------------


def test_stale_lease_cannot_write_a_managed_fact(
    session: Session,
    world: TaskWorld,
    engine: Engine,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task = submit_managed(session, world)
    session.commit()
    config = managed_config(tmp_path)
    supervisor = FakeHermesSupervisor(config, status_script=[RUNNING])
    released: dict[str, bool] = {"done": False}

    def _hook(unit: str, call: int) -> None:
        if call == 1 and not released["done"]:
            released["done"] = True
            release_lease_on_another_connection(engine, task_id=task.task_id)

    supervisor.status_hook = _hook
    adapter = HermesManagedAdapter(config, supervisor)

    # The lease was released on another connection while the status was being
    # observed.  The real fencing token now rejects the managed write instead of
    # recording a stale fact; the pass rolls back.
    with pytest.raises(TaskError):
        dispatcher_for(session, world, adapter).run_once()

    session.refresh(task)
    assert released["done"] is True
    assert task.state == "dispatched"
    assert task.stop_confirmed is False
    assert (task.result or {}).get("runtime_reconciliation") is None
    assert supervisor.launch_calls == 1


# ---------------------------------------------------------------------------
# restart / lookup
# ---------------------------------------------------------------------------


def test_restarted_adapter_finds_the_durable_run(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
) -> None:
    task, config, supervisor, _ = drive_success(session, world, tmp_path)
    grant = persisted_grant(session, task)
    handle = task.runtime_task_id
    assert handle is not None
    key = build_runtime_idempotency_key(
        task, project_runtime_grant(task, grant, now=utcnow()), 1
    )

    # A fresh adapter has no in-memory handle map.  It must recover the run
    # from the helper manifest alone (task-root scan), not from caller input.
    restarted = HermesManagedAdapter(config, supervisor)
    status = restarted.get_status(handle, task_id=task.task_id)
    assert status.state is RuntimeState.COMPLETED
    assert status.stop_confirmed is True

    lookup = restarted.find_by_idempotency_key(
        task.task_id, grant.grant_id, key
    )
    assert lookup.outcome is LookupOutcome.FOUND
    assert lookup.runtime_task_id == handle

    result = restarted.get_result(handle, task_id=task.task_id)
    assert result.outcome is ResultOutcome.SUCCEEDED
    assert result.result_ref == ARTIFACT_RESULT_REF
    assert result.evidence_refs == (ARTIFACT_EVIDENCE_REF,)
    assert supervisor.launch_calls == 1


# ---------------------------------------------------------------------------
# health degradation
# ---------------------------------------------------------------------------


def test_degraded_adapter_prepares_nothing(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
) -> None:
    # No broker socket is bound in this test: health is DEGRADED.
    task = submit_managed(session, world)
    session.commit()
    config = managed_config(tmp_path)
    supervisor = FakeHermesSupervisor(config)
    adapter = HermesManagedAdapter(config, supervisor)

    result = dispatcher_for(session, world, adapter).run_once()
    session.refresh(task)

    assert result.outcome == "adapter_unavailable"
    assert result.detail == "degraded"
    assert task.state == "queued"
    assert task.attempt == 0
    assert supervisor.prepare_calls == 0
    assert supervisor.launch_calls == 0
    assert supervisor.unit_dirs() == []


# ---------------------------------------------------------------------------
# dispatcher never certifies completion
# ---------------------------------------------------------------------------


def test_dispatcher_never_calls_complete_task(
    session: Session,
    world: TaskWorld,
    tmp_path: Path,
    broker_socket: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _tripwire(*args: object, **kwargs: object) -> None:
        calls.append(kwargs)
        raise AssertionError("the dispatcher must never call complete_task")

    monkeypatch.setattr(task_service, "complete_task", _tripwire)

    task, _, _, _ = drive_success(session, world, tmp_path)

    assert calls == []
    assert task.state == "verifying"
    assert task.state != "succeeded"
