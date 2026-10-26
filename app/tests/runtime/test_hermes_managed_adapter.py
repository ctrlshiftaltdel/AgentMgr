"""Adapter and helper-client tests for Hermes managed execution (Issue #97).

Everything here is DB-free, network-free and privilege-free.  The tests pin the
behaviour the #96 dispatcher relies on:

* a handle is returned only after the unit is active *and* the started marker
  exists, with a valid Task 1 task attestation;
* ambiguous launches stop and never create a second unit;
* idempotency is durable (helper manifest) and same-key/different-payload is a
  conflict;
* status/result/cancel map only real facts, and an artifact is host-hashed
  evidence for ``verifying`` -- never business success;
* the helper client uses a fixed ``sudo`` command, one JSON request on stdin and
  strict responses, with no prompt, token or caller path in argv/errors.

The fake supervisor scripts facts; it is test-only evidence and never upgrades a
real capability label.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.runtime.contract import (
    CancelOutcome,
    CapabilityStatus,
    ErrorCode,
    HealthStatus,
    InputOutcome,
    LookupOutcome,
    ResultOutcome,
    RuntimeGrant,
    RuntimeState,
    SubmitOutcome,
)
from app.runtime.hermes_egress import verify_egress_token
from app.runtime.hermes_managed import (
    MANAGED_INSTRUCTION,
    MIN_RUNTIME_MAX_SEC,
    RUNTIME_ATTESTATION_SCHEMA,
    TASK_ATTESTATION_SCHEMA,
    HelperSandboxSupervisor,
    HermesManagedAdapter,
    HermesManagedConfig,
    RuntimeAttestation,
    SandboxSpec,
    SandboxStatus,
    SandboxStop,
    SupervisorError,
    TaskAttestation,
    build_sandbox_spec,
    read_manifest,
    runtime_handle_for,
    unit_name_for,
)

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
KEY = b"a" * 32
OTHER_KEY = b"b" * 32
EGRESS_KEY = b"e" * 32
RUNTIME_ROOT_HASH = "sha256:" + "1" * 64
EVIDENCE_REF = "evidence:hermes:managed:test-0001"
SANDBOX_FACTS = {
    "private_network": True,
    "protect_home": True,
    "protect_system": "strict",
    "no_new_privileges": True,
    "capability_bounding_set": "empty",
    "toolsets": ["terminal"],
    "external_ip": False,
}


# ---------------------------------------------------------------------------
# fixtures / scripted fakes
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _sign(document: dict[str, object], *, key: bytes = KEY) -> str:
    return hmac.new(key, _canonical(document), hashlib.sha256).hexdigest()


def make_config(tmp_path: Path, **overrides: object) -> HermesManagedConfig:
    helper = tmp_path / "agentmgr-hermes-exec"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    (tmp_path / "revoked").mkdir(exist_ok=True)
    (tmp_path / "attestation.key").write_bytes(KEY)
    (tmp_path / "egress.key").write_bytes(EGRESS_KEY)
    config = HermesManagedConfig(
        helper_path=helper,
        runtime_root=runtime,
        runtime_root_hash=RUNTIME_ROOT_HASH,
        task_root=tmp_path / "tasks",
        broker_socket=tmp_path / "egress.sock",
        attestation_key_path=tmp_path / "attestation.key",
        egress_token_key_path=tmp_path / "egress.key",
        runtime_attestation_path=tmp_path / "runtime-attestation.json",
        evidence_ref=EVIDENCE_REF,
        runtime_name="hermes",
        runtime_version="0.22.1",
        model_name="model-x",
        egress_provider_host="provider.example",
        worker_id="worker-1",
        revocation_dir=tmp_path / "revoked",
    )
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return config


def runtime_document(config: HermesManagedConfig, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": RUNTIME_ATTESTATION_SCHEMA,
        "helper_version": "1.0.0",
        "runtime_name": config.runtime_name,
        "runtime_version": config.runtime_version,
        "runtime_root_hash": config.runtime_root_hash,
        "sandbox_facts": dict(SANDBOX_FACTS),
        "broker_socket_ok": True,
        "evidence_ref": config.evidence_ref,
        "issued_at": _iso(NOW - timedelta(seconds=60)),
        "expires_at": _iso(NOW + timedelta(seconds=600)),
        "nonce": "m" * 32,
    }
    document.update(overrides)
    return document


def runtime_attestation(
    config: HermesManagedConfig, *, key: bytes = KEY, **overrides: object
) -> RuntimeAttestation:
    document = runtime_document(config, **overrides)
    return RuntimeAttestation.from_document(
        {**document, "hmac": _sign(document, key=key)}
    )


def task_document(spec: SandboxSpec, *, key: bytes = KEY, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": TASK_ATTESTATION_SCHEMA,
        "unit": spec.unit,
        "task_id": spec.task_id,
        "grant_id": spec.grant_id,
        "runtime_handle": spec.runtime_handle,
        "uid": 12345,
        "runtime_root_hash": spec_manifest_root_hash(spec),
        "profile_template_sha256": spec.profile_template_sha256,
        "prompt_sha256": spec.prompt_sha256,
        "sandbox_facts": dict(SANDBOX_FACTS),
        "issued_at": _iso(NOW - timedelta(seconds=60)),
        "expires_at": _iso(NOW + timedelta(seconds=240)),
        "nonce": "n" * 32,
    }
    document.update(overrides)
    return document


def spec_manifest_root_hash(spec: SandboxSpec) -> str:
    # The fake supervisor always runs against the fixture's trusted root hash.
    return RUNTIME_ROOT_HASH


def task_attestation(
    spec: SandboxSpec, *, key: bytes = KEY, **overrides: object
) -> TaskAttestation:
    document = task_document(spec, key=key, **overrides)
    return TaskAttestation.from_document(
        {**document, "hmac": _sign(document, key=key)}
    )


def managed_grant(**overrides: object) -> RuntimeGrant:
    values: dict[str, object] = {
        "grant_id": "grnt_1",
        "task_id": "task_1",
        "task_revision": 1,
        "execution_mode": "managed_execution",
        "capabilities": ("expense.read@1.0.0",),
        "resource_scope": {"scope_type": "expense_claim"},
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return RuntimeGrant(**values)  # type: ignore[arg-type]


def write_fake_manifest(
    config: HermesManagedConfig,
    spec: SandboxSpec,
    *,
    created_at: datetime = NOW,
    model: str | None = None,
) -> Path:
    """Write the exact helper manifest shape (including its payload digest)."""

    binding: dict[str, object] = {
        "schema": "agentmgr.hermes.manifest.v1",
        "unit": spec.unit,
        "task_id": spec.task_id,
        "grant_id": spec.grant_id,
        "runtime_handle": spec.runtime_handle,
        "idempotency_key": spec.idempotency_key,
        "runtime_root_hash": config.runtime_root_hash,
        "prompt_sha256": spec.prompt_sha256,
        "prompt_bytes": spec.prompt_size,
        "profile_template_sha256": spec.profile_template_sha256,
        "egress_token_sha256": spec.egress_token_sha256,
        "ttl_seconds": spec.ttl_seconds,
        "runtime_max_sec": spec.runtime_max_sec,
        "memory_max_mb": spec.memory_max_mb,
        "cpu_quota_percent": spec.cpu_quota_percent,
        "tasks_max": spec.tasks_max,
        "egress_socket": "/task/egress.sock",
        "toolset": "terminal",
    }
    document = dict(binding)
    document["payload_sha256"] = hashlib.sha256(_canonical(binding)).hexdigest()
    document["created_at"] = _iso(created_at)
    if model is None:
        model = config.model_name
    if model:
        document["model"] = model
    task_dir = config.task_root / spec.unit
    (task_dir / "out").mkdir(parents=True, exist_ok=True)
    path = task_dir / "manifest.json"
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    path.chmod(0o640)
    return path


class FakeSupervisor:
    """Scripted ``SandboxSupervisor`` with call counters and task-root state."""

    def __init__(self, config: HermesManagedConfig, *, attestation_key: bytes = KEY):
        self.config = config
        self.attestation_key = attestation_key
        self.prepare_calls = 0
        self.launch_calls = 0
        self.status_calls = 0
        self.stop_calls = 0
        self.cleanup_calls: list[tuple[str, bool]] = []
        self.attestation_calls = 0
        self.last_spec: SandboxSpec | None = None
        self.launched = False

        self.prepare_error: BaseException | None = None
        self.launch_error: BaseException | None = None
        self.status_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.attestation_error: BaseException | None = None
        self.write_manifest = True

        self.active_state = "active"
        self.sub_state: str | None = "running"
        self.result: str | None = None
        self.exit_status: int | None = None
        self.started = True
        self.heartbeat_at: datetime | None = NOW - timedelta(seconds=5)
        self.exit_code: int | None = None
        self.timed_out = False
        self.cancelled = False
        self.process_gone = False
        self.stop_process_gone = True
        self.status_override: SandboxStatus | None = None
        self.stop_override: SandboxStop | None = None
        self.attestation_value = runtime_attestation(config)

    def prepare(self, spec: SandboxSpec) -> None:
        self.prepare_calls += 1
        self.last_spec = spec
        if self.prepare_error is not None:
            raise self.prepare_error
        spec.task_dir.mkdir(parents=True, exist_ok=False)
        (spec.task_dir / "out").mkdir()
        if self.write_manifest:
            write_fake_manifest(self.config, spec)

    def launch(self, spec: SandboxSpec) -> TaskAttestation:
        self.launch_calls += 1
        if self.launch_error is not None:
            raise self.launch_error
        self.launched = True
        return task_attestation(spec, key=self.attestation_key)

    def status(self, unit: str) -> SandboxStatus:
        self.status_calls += 1
        if self.status_error is not None:
            raise self.status_error
        if self.status_override is not None:
            return self.status_override
        manifest = read_manifest(self.config, unit)
        assert manifest is not None
        if not self.launched:
            return SandboxStatus(
                unit=unit,
                task_id=manifest.task_id,
                runtime_handle=manifest.runtime_handle,
                active_state="not-found",
                sub_state=None,
                main_pid=None,
                result=None,
                exit_status=None,
                started=False,
                heartbeat_at=None,
                exit_code=None,
                timed_out=False,
                cancelled=False,
                process_gone=True,
                observed_at=NOW,
            )
        process_gone = self.process_gone or self.active_state not in {
            "active",
            "activating",
            "reloading",
            "deactivating",
        }
        return SandboxStatus(
            unit=unit,
            task_id=manifest.task_id,
            runtime_handle=manifest.runtime_handle,
            active_state=self.active_state,
            sub_state=self.sub_state,
            main_pid=None if process_gone else 1234,
            result=self.result,
            exit_status=self.exit_status,
            started=self.started,
            heartbeat_at=self.heartbeat_at,
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            cancelled=self.cancelled,
            process_gone=process_gone,
            observed_at=NOW,
        )

    def stop(self, unit: str, *, reason: str) -> SandboxStop:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        if self.stop_override is not None:
            return self.stop_override
        if self.stop_process_gone:
            self.active_state = "inactive"
            self.sub_state = "dead"
            self.process_gone = True
        return SandboxStop(
            unit=unit,
            active_state=self.active_state,
            result=self.result,
            exit_status=self.exit_status,
            main_pid=None if self.stop_process_gone else 1234,
            process_gone=self.stop_process_gone,
            stop_confirmed=self.stop_process_gone,
            observed_at=NOW,
        )

    def cleanup(self, unit: str, *, result_read: bool = False) -> None:
        self.cleanup_calls.append((unit, result_read))
        task_dir = self.config.task_root / unit
        if result_read and task_dir.exists():
            shutil.rmtree(task_dir)

    def runtime_attestation(self) -> RuntimeAttestation:
        self.attestation_calls += 1
        if self.attestation_error is not None:
            raise self.attestation_error
        return self.attestation_value


def build_adapter(tmp_path: Path, **overrides: object):
    config = make_config(tmp_path, **overrides)
    supervisor = FakeSupervisor(config)
    adapter = HermesManagedAdapter(config, supervisor, now=lambda: NOW)
    return config, supervisor, adapter


def accepted_submission(tmp_path: Path, *, prompt: str = "PROMPT-CANARY"):
    config, supervisor, adapter = build_adapter(tmp_path)
    result = adapter.submit_task("task_1", prompt, "idem_1", managed_grant())
    assert result.outcome is SubmitOutcome.ACCEPTED
    assert result.runtime_task_id is not None
    return config, supervisor, adapter, result


@contextmanager
def _alarm(seconds: int):
    def _handler(signum, frame):
        raise TimeoutError(f"blocked for more than {seconds}s")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _write_corrupt_sibling(
    config: HermesManagedConfig,
    name: str = "agentmgr-hermes-ffffffffffffffff",
) -> str:
    task_dir = config.task_root / name
    (task_dir / "out").mkdir(parents=True, exist_ok=True)
    manifest = task_dir / "manifest.json"
    manifest.write_text("{not-json", encoding="utf-8")
    manifest.chmod(0o640)
    return name


class _OrderedScandir:
    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc_info):
        return False


class _UnstattableEntry:
    """A task-root entry whose metadata cannot be read."""

    name = "agentmgr-hermes-eeeeeeeeeeeeeeee"

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        raise OSError(5, "input/output error")


def _scandir_with_first(config: HermesManagedConfig, first, monkeypatch):
    """Place ``first`` (a name of a real entry, or a fake entry) first in the scan."""

    real_scandir = os.scandir

    def fake_scandir(path):
        handle = real_scandir(path)
        try:
            entries = list(handle)
        finally:
            handle.close()
        if Path(path) == config.task_root:
            if isinstance(first, str):
                entries.sort(key=lambda entry: 0 if entry.name == first else 1)
            else:
                entries.insert(0, first)
        return _OrderedScandir(entries)

    monkeypatch.setattr(
        "app.runtime.hermes_managed.os.scandir", fake_scandir
    )


# ---------------------------------------------------------------------------
# capabilities gating
# ---------------------------------------------------------------------------


def test_capabilities_do_not_claim_managed_without_runtime_attestation(tmp_path):
    config, supervisor, adapter = build_adapter(tmp_path)
    supervisor.attestation_value = RuntimeAttestation.from_document(
        {**runtime_document(config), "hmac": "f" * 64}
    )

    capabilities = adapter.capabilities()

    assert (
        capabilities.capability("runtime.managed_execution").status
        is CapabilityStatus.UNVERIFIED
    )
    assert capabilities.capability("task.submit").status is CapabilityStatus.UNVERIFIED


def test_capabilities_are_unverified_when_the_helper_is_unavailable(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    supervisor.attestation_error = SupervisorError("helper_unavailable")

    capabilities = adapter.capabilities(probe=True)

    assert capabilities.capability("task.submit").status is CapabilityStatus.UNVERIFIED


def test_capabilities_verified_with_a_signed_runtime_attestation(tmp_path):
    _, _, adapter = build_adapter(tmp_path)

    capabilities = adapter.capabilities()

    assert (
        capabilities.capability("runtime.managed_execution").status
        is CapabilityStatus.VERIFIED
    )
    assert capabilities.capability("task.submit").status is CapabilityStatus.VERIFIED
    assert (
        capabilities.capability("task.events.poll").status
        is CapabilityStatus.UNVERIFIED
    )


# ---------------------------------------------------------------------------
# submit acceptance / files / attestation
# ---------------------------------------------------------------------------


def test_submit_accepts_only_after_started_and_returns_stable_handle(tmp_path):
    config, supervisor, adapter = build_adapter(tmp_path)

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert result.outcome is SubmitOutcome.ACCEPTED
    assert result.runtime_task_id is not None
    assert result.runtime_task_id.startswith("rtask_hermes_")
    assert result.replayed is False
    assert supervisor.launch_calls == 1
    assert supervisor.prepare_calls == 1

    unit = unit_name_for("task_1", "idem_1")
    assert result.runtime_task_id == runtime_handle_for(unit)
    manifest = read_manifest(config, unit)
    assert manifest is not None
    assert manifest.runtime_handle == result.runtime_task_id

    task_dir = config.task_root / unit
    prompt_bytes = (task_dir / "prompt.txt").read_bytes()
    assert prompt_bytes.startswith(MANAGED_INSTRUCTION.encode("utf-8"))
    assert b"PROMPT-CANARY" in prompt_bytes
    profile = (task_dir / "profile.template.yaml").read_text(encoding="utf-8")
    assert "__PORT__" in profile
    assert "__MODEL__" in profile
    assert "api_key" not in profile.lower()

    token = (task_dir / "egress.token").read_text(encoding="ascii")
    claims = verify_egress_token(token, key=EGRESS_KEY, now=NOW)
    assert claims.task_id == "task_1"
    assert claims.grant_id == "grnt_1"
    assert claims.unit == unit
    assert claims.model == "model-x"
    assert claims.provider_host == "provider.example"
    assert claims.path_prefix == "/v1"

    for name in ("prompt.txt", "profile.template.yaml", "egress.token"):
        assert stat.S_IMODE((task_dir / name).stat().st_mode) == 0o640

    assert supervisor.last_spec is not None
    assert "PROMPT-CANARY" not in repr(supervisor.last_spec)
    assert token not in repr(supervisor.last_spec)


def test_missing_started_marker_returns_unknown_and_stops(tmp_path):
    config, supervisor, adapter = build_adapter(tmp_path)
    supervisor.launch_error = SupervisorError(
        "started_marker_timeout", ambiguous=True
    )

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.runtime_task_id is None
    assert supervisor.launch_calls == 1
    assert supervisor.stop_calls == 1
    assert supervisor.cleanup_calls == [(unit_name_for("task_1", "idem_1"), False)]
    assert "PROMPT-CANARY" not in str(result.error)


def test_launch_failure_before_started_is_rejected_without_stop(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    supervisor.launch_error = SupervisorError("prompt_hash_mismatch", ambiguous=False)

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert result.outcome is SubmitOutcome.REJECTED
    assert supervisor.launch_calls == 1
    assert supervisor.stop_calls == 0
    assert result.error is not None
    assert result.error.code is ErrorCode.UNAVAILABLE


def test_invalid_task_attestation_is_unknown_and_never_selects_success(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    supervisor.attestation_key = OTHER_KEY

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert supervisor.launch_calls == 1
    assert supervisor.stop_calls == 1


def test_manifest_missing_after_prepare_is_unknown_and_does_not_launch(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    supervisor.write_manifest = False

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert supervisor.launch_calls == 0


def test_submit_requires_a_verified_runtime_attestation_before_prepare(tmp_path):
    config, supervisor, adapter = build_adapter(tmp_path)
    supervisor.attestation_value = RuntimeAttestation.from_document(
        {**runtime_document(config), "hmac": "f" * 64}
    )

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert result.outcome is SubmitOutcome.UNSUPPORTED
    assert result.error is not None
    assert result.error.code is ErrorCode.UNSUPPORTED
    assert supervisor.prepare_calls == 0
    assert supervisor.launch_calls == 0

    supervisor.attestation_value = runtime_attestation(config)
    supervisor.attestation_error = SupervisorError("helper_unavailable")
    unavailable = adapter.submit_task(
        "task_1", "PROMPT-CANARY", "idem_1", managed_grant()
    )
    assert unavailable.outcome is SubmitOutcome.UNSUPPORTED
    assert supervisor.prepare_calls == 0
    assert supervisor.launch_calls == 0


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_same_key_same_payload_reuses_unit_and_different_payload_conflicts(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)

    first = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())
    second = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())
    conflict = adapter.submit_task(
        "task_1", "DIFFERENT-PROMPT", "idem_1", managed_grant()
    )

    assert first.outcome is SubmitOutcome.ACCEPTED
    assert second.outcome is SubmitOutcome.ACCEPTED
    assert second.replayed is True
    assert first.runtime_task_id == second.runtime_task_id
    assert supervisor.launch_calls == 1
    assert supervisor.prepare_calls == 1
    assert conflict.outcome is SubmitOutcome.CONFLICT
    assert conflict.error is not None
    assert conflict.error.code is ErrorCode.CONFLICT


def test_idempotent_replay_after_adapter_restart_uses_the_manifest(tmp_path):
    config, supervisor, adapter, first = accepted_submission(tmp_path)

    restarted = HermesManagedAdapter(
        config, supervisor, now=lambda: NOW
    )
    second = restarted.submit_task(
        "task_1", "PROMPT-CANARY", "idem_1", managed_grant()
    )

    assert second.outcome is SubmitOutcome.ACCEPTED
    assert second.replayed is True
    assert second.runtime_task_id == first.runtime_task_id
    assert supervisor.launch_calls == 1


def test_replay_is_unknown_before_the_started_marker(tmp_path):
    _, supervisor, adapter, first = accepted_submission(tmp_path)
    supervisor.started = False
    supervisor.active_state = "active"

    second = adapter.submit_task(
        "task_1", "PROMPT-CANARY", "idem_1", managed_grant()
    )

    assert first.outcome is SubmitOutcome.ACCEPTED
    assert second.outcome is SubmitOutcome.UNKNOWN
    assert supervisor.launch_calls == 1
    assert supervisor.prepare_calls == 1


def test_ambiguous_submission_is_never_relaunched(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    supervisor.launch_error = SupervisorError(
        "started_marker_timeout", ambiguous=True
    )
    first = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())
    supervisor.launch_error = None

    second = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert first.outcome is SubmitOutcome.UNKNOWN
    assert second.outcome is SubmitOutcome.UNKNOWN
    assert supervisor.launch_calls == 1
    assert supervisor.prepare_calls == 1


def test_accepted_intent_without_manifest_is_never_relaunched(tmp_path):
    config, supervisor, adapter, first = accepted_submission(tmp_path)
    shutil.rmtree(config.task_root / unit_name_for("task_1", "idem_1"))

    lookup = adapter.find_by_idempotency_key("task_1", "grnt_1", "idem_1")
    second = adapter.submit_task(
        "task_1", "PROMPT-CANARY", "idem_1", managed_grant()
    )

    assert first.outcome is SubmitOutcome.ACCEPTED
    assert lookup.outcome is LookupOutcome.UNKNOWN
    assert second.outcome is SubmitOutcome.UNKNOWN
    assert supervisor.launch_calls == 1
    assert supervisor.prepare_calls == 1


def test_find_by_idempotency_key_reports_durable_facts(tmp_path):
    config, _, adapter, result = accepted_submission(tmp_path)
    manifest = read_manifest(config, unit_name_for("task_1", "idem_1"))
    assert manifest is not None

    found = adapter.find_by_idempotency_key("task_1", "grnt_1", "idem_1")
    conflict = adapter.find_by_idempotency_key(
        "task_1", "grnt_1", "idem_1", payload_hash="0" * 64
    )
    unknown = adapter.find_by_idempotency_key("task_9", "grnt_9", "idem_9")

    assert found.outcome is LookupOutcome.FOUND
    assert found.runtime_task_id == result.runtime_task_id
    assert found.payload_hash == manifest.payload_sha256
    assert conflict.outcome is LookupOutcome.CONFLICT
    assert unknown.outcome is LookupOutcome.UNKNOWN


def test_find_returns_not_found_only_after_a_definite_prelaunch_rejection(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    supervisor.prepare_error = SupervisorError("invalid_digest", ambiguous=False)

    rejected = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())
    lookup = adapter.find_by_idempotency_key("task_1", "grnt_1", "idem_1")

    assert rejected.outcome is SubmitOutcome.REJECTED
    assert lookup.outcome is LookupOutcome.NOT_FOUND


def test_find_reports_unknown_for_a_prepared_task_without_a_unit(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    supervisor.launch_error = SupervisorError("prompt_hash_mismatch", ambiguous=False)

    rejected = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())
    lookup = adapter.find_by_idempotency_key("task_1", "grnt_1", "idem_1")

    assert rejected.outcome is SubmitOutcome.REJECTED
    assert lookup.outcome is LookupOutcome.UNKNOWN


def test_find_reports_unknown_when_status_is_unreadable(tmp_path):
    _, supervisor, adapter, _ = accepted_submission(tmp_path)
    supervisor.status_error = SupervisorError("helper_unavailable")

    lookup = adapter.find_by_idempotency_key("task_1", "grnt_1", "idem_1")

    assert lookup.outcome is LookupOutcome.UNKNOWN


# ---------------------------------------------------------------------------
# grant / config validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"task_id": "task_other"}, ErrorCode.FORBIDDEN),
        ({"execution_mode": "advisory"}, ErrorCode.FORBIDDEN),
        ({"expires_at": NOW - timedelta(seconds=1)}, ErrorCode.FORBIDDEN),
        ({"capabilities": ()}, ErrorCode.INVALID_REQUEST),
        ({"capabilities": (" ",)}, ErrorCode.INVALID_REQUEST),
        ({"task_revision": 0}, ErrorCode.INVALID_REQUEST),
        ({"task_revision": True}, ErrorCode.INVALID_REQUEST),
    ],
)
def test_invalid_grant_is_rejected_before_any_helper_call(
    tmp_path, overrides, expected_code
):
    _, supervisor, adapter = build_adapter(tmp_path)

    result = adapter.submit_task(
        "task_1", "PROMPT-CANARY", "idem_1", managed_grant(**overrides)
    )

    assert result.outcome is SubmitOutcome.REJECTED
    assert result.error is not None
    assert result.error.code is expected_code
    assert supervisor.prepare_calls == 0
    assert supervisor.launch_calls == 0


def test_unsafe_scope_is_rejected_before_any_helper_call(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    grant = dataclasses.replace(managed_grant(), resource_scope=["not", "a", "mapping"])

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", grant)

    assert result.outcome is SubmitOutcome.REJECTED
    assert supervisor.prepare_calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"runtime_max_sec": MIN_RUNTIME_MAX_SEC - 1},
        {"model_name": "../../model"},
        {"local_proxy_base_url_template": "http://example.com:{port}/v1"},
        {"egress_token_ttl_seconds": 0},
        {"egress_token_ttl_seconds": 301},
        {"egress_provider_host": "not a host"},
        {"egress_path_prefix": "/"},
    ],
)
def test_unsafe_config_is_rejected_before_launch(tmp_path, overrides):
    _, supervisor, adapter = build_adapter(tmp_path, **overrides)

    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())

    assert result.outcome is SubmitOutcome.REJECTED
    assert supervisor.prepare_calls == 0
    assert supervisor.launch_calls == 0


def test_blank_and_oversized_prompts_are_rejected_before_launch(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path, prompt_max_bytes=64)

    blank = adapter.submit_task("task_1", "   ", "idem_1", managed_grant())
    oversized = adapter.submit_task("task_1", "x" * 128, "idem_1", managed_grant())

    assert blank.outcome is SubmitOutcome.REJECTED
    assert oversized.outcome is SubmitOutcome.REJECTED
    assert supervisor.prepare_calls == 0
    assert "x" * 128 not in str(oversized.error)


# ---------------------------------------------------------------------------
# status mapping
# ---------------------------------------------------------------------------


def test_status_maps_running_and_completed(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    handle = result.runtime_task_id

    running = adapter.get_status(handle, task_id="task_1")
    assert running.state is RuntimeState.RUNNING
    assert running.stop_confirmed is False

    supervisor.active_state = "inactive"
    supervisor.sub_state = "dead"
    supervisor.process_gone = True
    supervisor.exit_code = 0
    completed = adapter.get_status(handle, task_id="task_1")

    assert completed.state is RuntimeState.COMPLETED
    assert completed.stop_confirmed is True
    assert completed.external_state == "inactive:dead"


def test_status_never_turns_a_missing_exit_marker_into_success(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.active_state = "inactive"
    supervisor.process_gone = True
    supervisor.started = True
    supervisor.exit_status = 0
    supervisor.exit_code = None

    status = adapter.get_status(result.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.UNKNOWN
    assert status.state is not RuntimeState.COMPLETED


def test_status_maps_nonzero_exit_to_failed(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.active_state = "inactive"
    supervisor.process_gone = True
    supervisor.exit_status = 1
    supervisor.exit_code = None

    status = adapter.get_status(result.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.FAILED


def test_nonzero_exit_while_the_unit_is_alive_is_running(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.active_state = "active"
    supervisor.process_gone = False
    supervisor.started = True
    supervisor.exit_code = 1

    status = adapter.get_status(result.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.RUNNING
    assert status.stop_confirmed is False


def test_unloaded_nonzero_exit_is_failed(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.active_state = "not-found"
    supervisor.sub_state = None
    supervisor.process_gone = True
    supervisor.started = True
    supervisor.exit_code = 1

    status = adapter.get_status(result.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.FAILED


def test_unloaded_without_an_exit_marker_is_unknown(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.active_state = "not-found"
    supervisor.sub_state = None
    supervisor.process_gone = True
    supervisor.started = True
    supervisor.exit_code = None
    supervisor.exit_status = None

    status = adapter.get_status(result.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.UNKNOWN
    assert status.stop_confirmed is True


def test_status_maps_timeout_and_cancelled(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.active_state = "failed"
    supervisor.process_gone = True
    supervisor.timed_out = True

    timed_out = adapter.get_status(result.runtime_task_id, task_id="task_1")
    assert timed_out.state is RuntimeState.TIMED_OUT

    supervisor.timed_out = False
    supervisor.cancelled = True
    stopped = adapter.get_status(result.runtime_task_id, task_id="task_1")
    assert stopped.state is RuntimeState.STOPPED


def test_status_reports_not_found_for_a_missing_unit(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.active_state = "not-found"
    supervisor.process_gone = True
    supervisor.started = False

    status = adapter.get_status(result.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.NOT_FOUND
    assert status.error is not None
    assert status.error.code is ErrorCode.NOT_FOUND


def test_status_rejects_cross_task_and_unknown_handles(tmp_path):
    _, _, adapter, result = accepted_submission(tmp_path)

    cross = adapter.get_status(result.runtime_task_id, task_id="task_2")
    unknown = adapter.get_status("rtask_hermes_" + "0" * 32, task_id="task_1")

    assert cross.state is RuntimeState.NOT_FOUND
    assert unknown.state is RuntimeState.NOT_FOUND


def test_status_unreadable_is_unknown_without_guessing(tmp_path):
    _, supervisor, adapter, result = accepted_submission(tmp_path)
    supervisor.status_error = SupervisorError("io_error")

    status = adapter.get_status(result.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.UNKNOWN
    assert status.stop_confirmed is False


def test_unreadable_or_tampered_manifest_is_unknown_not_not_found(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    manifest = config.task_root / unit / "manifest.json"
    good = manifest.read_bytes()

    manifest.write_text("{not-json", encoding="utf-8")
    corrupted = adapter.get_status(submitted.runtime_task_id, task_id="task_1")
    restarted = HermesManagedAdapter(config, supervisor, now=lambda: NOW)
    restarted_corrupted = restarted.get_status(
        submitted.runtime_task_id, task_id="task_1"
    )
    corrupted_result = adapter.get_result(
        submitted.runtime_task_id, task_id="task_1"
    )
    corrupted_cancel = adapter.cancel_task(
        submitted.runtime_task_id, task_id="task_1"
    )

    assert corrupted.state is RuntimeState.UNKNOWN
    assert restarted_corrupted.state is RuntimeState.UNKNOWN
    assert corrupted.state is not RuntimeState.NOT_FOUND
    assert corrupted_result.outcome is ResultOutcome.UNKNOWN
    assert corrupted_cancel.outcome is CancelOutcome.UNKNOWN
    assert supervisor.stop_calls == 0

    manifest.write_bytes(good)
    manifest.chmod(0o644)
    wrong_mode = adapter.get_status(submitted.runtime_task_id, task_id="task_1")
    assert wrong_mode.state is RuntimeState.UNKNOWN

    manifest.chmod(0o640)
    outside = tmp_path / "outside-manifest.json"
    outside.write_bytes(good)
    manifest.unlink()
    manifest.symlink_to(outside)
    symlinked = adapter.get_status(submitted.runtime_task_id, task_id="task_1")
    assert symlinked.state is RuntimeState.UNKNOWN


def test_cold_scan_skips_a_corrupt_sibling_and_finds_a_later_manifest(
    tmp_path, monkeypatch
):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    _complete_with_artifact(config, supervisor, unit, b"cold-scan-success")
    supervisor.active_state = "not-found"
    supervisor.sub_state = None
    supervisor.started = True
    supervisor.exit_code = 0
    supervisor.process_gone = True
    corrupt = _write_corrupt_sibling(config)
    _scandir_with_first(config, corrupt, monkeypatch)

    restarted = HermesManagedAdapter(config, supervisor, now=lambda: NOW)
    status = restarted.get_status(submitted.runtime_task_id, task_id="task_1")
    result = restarted.get_result(submitted.runtime_task_id, task_id="task_1")
    stop_before = supervisor.stop_calls
    cancelled = restarted.cancel_task(submitted.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.COMPLETED
    assert result.outcome is ResultOutcome.SUCCEEDED
    assert cancelled.outcome is CancelOutcome.ALREADY_TERMINAL
    assert supervisor.stop_calls == stop_before + 1


def test_cold_scan_skips_an_unreadable_sibling_and_finds_a_later_manifest(
    tmp_path, monkeypatch
):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    _complete_with_artifact(config, supervisor, unit, b"cold-scan-unreadable")
    supervisor.active_state = "not-found"
    supervisor.sub_state = None
    supervisor.started = True
    supervisor.exit_code = 0
    supervisor.process_gone = True
    _scandir_with_first(config, _UnstattableEntry(), monkeypatch)

    restarted = HermesManagedAdapter(config, supervisor, now=lambda: NOW)
    status = restarted.get_status(submitted.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.COMPLETED
    assert status.state is not RuntimeState.UNKNOWN


def test_cold_scan_without_a_match_and_an_unreadable_sibling_is_unknown(
    tmp_path, monkeypatch
):
    config, supervisor, _, _ = accepted_submission(tmp_path)
    _scandir_with_first(config, _UnstattableEntry(), monkeypatch)
    restarted = HermesManagedAdapter(config, supervisor, now=lambda: NOW)

    status = restarted.get_status("rtask_hermes_" + "0" * 32, task_id="task_1")

    assert status.state is RuntimeState.UNKNOWN
    assert status.state is not RuntimeState.NOT_FOUND


def test_cold_scan_without_a_match_and_a_corrupt_sibling_is_unknown(tmp_path):
    config, supervisor, _ = build_adapter(tmp_path)
    _write_corrupt_sibling(config)
    restarted = HermesManagedAdapter(config, supervisor, now=lambda: NOW)

    status = restarted.get_status("rtask_hermes_" + "0" * 32, task_id="task_1")

    assert status.state is RuntimeState.UNKNOWN
    assert status.state is not RuntimeState.NOT_FOUND


def test_cross_task_cancel_is_unknown_and_never_stops(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)

    cancelled = adapter.cancel_task(submitted.runtime_task_id, task_id="task_2")

    assert cancelled.outcome is CancelOutcome.UNKNOWN
    assert cancelled.stop_confirmed is False
    assert supervisor.stop_calls == 0
    assert list(config.revocation_dir.iterdir()) == []  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def _complete_with_artifact(
    config: HermesManagedConfig,
    supervisor: FakeSupervisor,
    unit: str,
    data: bytes,
) -> Path:
    supervisor.active_state = "inactive"
    supervisor.sub_state = "dead"
    supervisor.process_gone = True
    supervisor.exit_code = 0
    out = config.task_root / unit / "out"
    out.mkdir(parents=True, exist_ok=True)
    result = out / "result.txt"
    result.write_bytes(data)
    return result


def test_completed_artifact_becomes_verifying_evidence_not_success(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    _complete_with_artifact(config, supervisor, unit, b"artifact-body")

    status = adapter.get_status(submitted.runtime_task_id, task_id="task_1")
    result = adapter.get_result(submitted.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.COMPLETED
    assert result.outcome is ResultOutcome.SUCCEEDED
    assert result.result_ref is not None
    assert result.result_ref.startswith("result_")
    assert result.evidence_refs[0].startswith("evidence:hermes_artifact_")
    assert result.structured_result is True
    assert result.structured_payload == {
        "relative_path": "out/result.txt",
        "sha256": "sha256:" + hashlib.sha256(b"artifact-body").hexdigest(),
        "size": len(b"artifact-body"),
    }
    assert result.summary is None
    assert result.runtime == "hermes"

    manifest = read_manifest(config, unit)
    assert manifest is not None
    markers = list((config.revocation_dir or tmp_path / "none").iterdir())
    names = {path.name for path in markers if path.is_file()}
    assert f"token.{manifest.egress_token_sha256}" in names
    assert f"unit.{unit}" in names
    assert f"task.task_1" in names
    assert f"grant.grnt_1" in names


def test_unloaded_success_is_completed_and_verifying_evidence(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    _complete_with_artifact(config, supervisor, unit, b"unloaded-success")
    # Normal systemd behaviour: a successful transient unit is unloaded, so the
    # unit is gone even though the task markers/artifact remain.
    supervisor.active_state = "not-found"
    supervisor.sub_state = None
    supervisor.started = True
    supervisor.exit_code = 0
    supervisor.process_gone = True

    status = adapter.get_status(submitted.runtime_task_id, task_id="task_1")
    lookup = adapter.find_by_idempotency_key("task_1", "grnt_1", "idem_1")
    result = adapter.get_result(submitted.runtime_task_id, task_id="task_1")

    assert status.state is RuntimeState.COMPLETED
    assert status.stop_confirmed is True
    assert lookup.outcome is LookupOutcome.FOUND
    assert lookup.runtime_task_id == submitted.runtime_task_id
    assert lookup.reported_state == RuntimeState.COMPLETED.value
    assert result.outcome is ResultOutcome.SUCCEEDED
    assert result.result_ref is not None
    assert result.evidence_refs[0].startswith("evidence:hermes_artifact_")


def test_missing_artifact_needs_human_and_never_synthesizes(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    supervisor.active_state = "inactive"
    supervisor.process_gone = True
    supervisor.exit_code = 0

    result = adapter.get_result(submitted.runtime_task_id, task_id="task_1")

    assert result.outcome in {ResultOutcome.UNKNOWN, ResultOutcome.NEEDS_HUMAN}
    assert result.result_ref is None
    assert result.evidence_refs == ()
    assert result.structured_result is False
    assert (config.task_root / unit / "out").exists()


def test_symlinked_and_fifo_artifacts_fail_closed(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    supervisor.active_state = "inactive"
    supervisor.process_gone = True
    supervisor.exit_code = 0
    out = config.task_root / unit / "out"
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (out / "result.txt").symlink_to(target)

    symlinked = adapter.get_result(submitted.runtime_task_id, task_id="task_1")
    assert symlinked.outcome in {
        ResultOutcome.UNKNOWN,
        ResultOutcome.NEEDS_HUMAN,
    }

    (out / "result.txt").unlink()
    os.mkfifo(out / "result.txt")
    with _alarm(5):
        fifo = adapter.get_result(submitted.runtime_task_id, task_id="task_1")
    assert fifo.outcome in {ResultOutcome.UNKNOWN, ResultOutcome.NEEDS_HUMAN}


def test_result_is_not_read_while_the_unit_is_alive(tmp_path):
    config, _, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    (config.task_root / unit / "out" / "result.txt").write_text(
        "too-early", encoding="utf-8"
    )

    result = adapter.get_result(submitted.runtime_task_id, task_id="task_1")

    assert result.outcome is ResultOutcome.UNKNOWN
    assert result.result_ref is None


def test_result_for_an_unknown_handle_is_unknown(tmp_path):
    _, _, adapter = build_adapter(tmp_path)

    result = adapter.get_result("rtask_hermes_" + "0" * 32, task_id="task_1")

    assert result.outcome is ResultOutcome.UNKNOWN
    assert result.result_ref is None


def test_result_unreadable_status_is_unknown(tmp_path):
    _, supervisor, adapter, submitted = accepted_submission(tmp_path)
    supervisor.status_error = SupervisorError("io_error")

    result = adapter.get_result(submitted.runtime_task_id, task_id="task_1")

    assert result.outcome is ResultOutcome.UNKNOWN


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_requested_is_not_stopped_and_cgroup_gone_confirms(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)

    supervisor.stop_process_gone = False
    requested = adapter.cancel_task(submitted.runtime_task_id, task_id="task_1")
    assert requested.outcome is CancelOutcome.REQUESTED
    assert requested.stop_confirmed is False
    assert list(config.revocation_dir.iterdir()) == []  # type: ignore[union-attr]

    supervisor.stop_process_gone = True
    confirmed = adapter.cancel_task(
        submitted.runtime_task_id, task_id="task_1", reason="operator request"
    )
    assert confirmed.outcome is CancelOutcome.STOPPED
    assert confirmed.stop_confirmed is True
    assert any(config.revocation_dir.iterdir())  # type: ignore[union-attr]


def test_cancel_does_not_swallow_a_completed_artifact(tmp_path):
    config, supervisor, adapter, submitted = accepted_submission(tmp_path)
    unit = unit_name_for("task_1", "idem_1")
    artifact = _complete_with_artifact(config, supervisor, unit, b"done")

    cancelled = adapter.cancel_task(submitted.runtime_task_id, task_id="task_1")

    assert cancelled.outcome is CancelOutcome.ALREADY_TERMINAL
    assert cancelled.stop_confirmed is True
    assert artifact.exists()
    result = adapter.get_result(submitted.runtime_task_id, task_id="task_1")
    assert result.outcome is ResultOutcome.SUCCEEDED


def test_cancel_stop_error_is_unknown_and_never_claims_stop(tmp_path):
    _, supervisor, adapter, submitted = accepted_submission(tmp_path)
    supervisor.stop_error = SupervisorError("helper_unavailable")

    cancelled = adapter.cancel_task(submitted.runtime_task_id, task_id="task_1")

    assert cancelled.outcome is CancelOutcome.UNKNOWN
    assert cancelled.stop_confirmed is False


def test_cancel_unknown_handle_is_unknown(tmp_path):
    _, _, adapter = build_adapter(tmp_path)

    cancelled = adapter.cancel_task("rtask_hermes_" + "0" * 32, task_id="task_1")

    assert cancelled.outcome is CancelOutcome.UNKNOWN
    assert cancelled.stop_confirmed is False


# ---------------------------------------------------------------------------
# health / events / input
# ---------------------------------------------------------------------------


def _bind_unix_socket(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    return listener


def test_health_is_healthy_with_helper_runtime_socket_and_attestation(tmp_path):
    config, _, adapter = build_adapter(tmp_path)
    listener = _bind_unix_socket(config.broker_socket)
    try:
        health = adapter.health()
    finally:
        listener.close()
        config.broker_socket.unlink()

    assert health.status is HealthStatus.HEALTHY
    assert health.details["helper"] == "available"
    assert health.details["runtime_root"] == "available"
    assert health.details["broker_socket"] == "available"
    assert health.details["attestation"] == "verified"


def test_health_degrades_without_a_broker_socket_or_attestation(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)

    without_socket = adapter.health()
    assert without_socket.status is HealthStatus.DEGRADED

    listener = _bind_unix_socket(adapter._config.broker_socket)  # type: ignore[attr-defined]
    supervisor.attestation_value = RuntimeAttestation.from_document(
        {**runtime_document(adapter._config), "hmac": "a" * 64}  # type: ignore[attr-defined]
    )
    try:
        invalid_attestation = adapter.health()
    finally:
        listener.close()
        adapter._config.broker_socket.unlink()  # type: ignore[attr-defined]

    assert invalid_attestation.status is HealthStatus.DEGRADED


def test_health_is_unavailable_without_helper_or_runtime_root(tmp_path):
    config, _, adapter = build_adapter(tmp_path)
    config.helper_path.unlink()
    listener = _bind_unix_socket(config.broker_socket)
    try:
        health = adapter.health()
    finally:
        listener.close()
        config.broker_socket.unlink()

    assert health.status is HealthStatus.UNAVAILABLE

    config.runtime_root.rmdir()
    health = adapter.health()
    assert health.status is HealthStatus.UNAVAILABLE


def test_health_does_not_call_a_model(tmp_path):
    _, supervisor, adapter = build_adapter(tmp_path)
    listener = _bind_unix_socket(adapter._config.broker_socket)  # type: ignore[attr-defined]
    try:
        adapter.health()
    finally:
        listener.close()
        adapter._config.broker_socket.unlink()  # type: ignore[attr-defined]

    assert supervisor.launch_calls == 0
    assert supervisor.prepare_calls == 0


def test_events_and_input_are_explicitly_unsupported(tmp_path):
    _, _, adapter, submitted = accepted_submission(tmp_path)

    events = adapter.poll_events(submitted.runtime_task_id, task_id="task_1")
    provided = adapter.provide_input(
        submitted.runtime_task_id, "input_1", "0" * 64, {"answer": "x"}
    )

    assert events.events == ()
    assert events.next_cursor is None
    assert events.error is not None
    assert events.error.code is ErrorCode.UNSUPPORTED
    assert provided.outcome is InputOutcome.UNSUPPORTED


# ---------------------------------------------------------------------------
# helper client
# ---------------------------------------------------------------------------


class Runner:
    """Record calls and replay scripted CompletedProcess/exception responses."""

    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object):
        self.calls.append((argv, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return subprocess.CompletedProcess(argv, 0, response, "")
        if isinstance(response, dict):
            return subprocess.CompletedProcess(
                argv,
                int(response.get("returncode", 0)),
                str(response.get("stdout", "")),
                str(response.get("stderr", "")),
            )
        raise AssertionError(f"unexpected scripted response: {response!r}")


def helper_config(tmp_path: Path) -> HermesManagedConfig:
    config = make_config(tmp_path)
    config.task_root.mkdir(parents=True, exist_ok=True)
    return config


def helper_spec(config: HermesManagedConfig, *, prompt: str = "PROMPT-CANARY") -> SandboxSpec:
    return build_sandbox_spec(
        config,
        task_id="task_1",
        grant_id="grnt_1",
        idempotency_key="idem_1",
        prompt=prompt,
        profile_template="PROFILE-TEMPLATE",
        egress_token="egt.canary",
    )


def prepare_response(spec: SandboxSpec) -> dict[str, object]:
    return {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "prepare",
                "unit": spec.unit,
                "prepared": True,
                "created": True,
                "task_dir_ready": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def test_helper_prepare_uses_fixed_sudo_argv_and_stdin_json(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    runner = Runner([prepare_response(spec)])
    supervisor = HelperSandboxSupervisor(config, runner=runner)

    supervisor.prepare(spec)

    assert len(runner.calls) == 1
    argv, kwargs = runner.calls[0]
    assert argv == ["sudo", "-n", str(config.helper_path)]
    assert kwargs["shell"] is False
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert isinstance(kwargs["timeout"], float) and kwargs["timeout"] > 0
    request = json.loads(str(kwargs["input"]))
    assert set(request) == {
        "operation",
        "task_id",
        "grant_id",
        "unit",
        "runtime_handle",
        "idempotency_key",
        "prompt_sha256",
        "prompt_bytes",
        "profile_template_sha256",
        "egress_token_sha256",
        "ttl_seconds",
        "runtime_max_sec",
        "memory_max_mb",
        "cpu_quota_percent",
        "tasks_max",
    }
    assert request["operation"] == "prepare"
    assert request["idempotency_key"] == "idem_1"
    encoded = str(kwargs["input"])
    assert "PROMPT-CANARY" not in encoded
    assert "egt.canary" not in encoded
    assert str(spec.task_dir) not in encoded


def test_helper_launch_requires_started_evidence(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    launched = {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "launch",
                "unit": spec.unit,
                "active_state": "active",
                "started": True,
                "uid": 12345,
                "main_pid": 1234,
                "attestation": task_attestation(spec).to_document(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    supervisor = HelperSandboxSupervisor(config, runner=Runner([launched]))

    attestation = supervisor.launch(spec)

    assert isinstance(attestation, TaskAttestation)
    assert attestation.unit == spec.unit

    not_started = {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "launch",
                "unit": spec.unit,
                "active_state": "active",
                "started": False,
                "uid": 12345,
                "main_pid": 1234,
                "attestation": task_attestation(spec).to_document(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    supervisor = HelperSandboxSupervisor(config, runner=Runner([not_started]))
    with pytest.raises(SupervisorError) as excinfo:
        supervisor.launch(spec)
    assert excinfo.value.code == "started_not_confirmed"
    assert excinfo.value.ambiguous is True


@pytest.mark.parametrize(
    "code",
    [
        "prompt_missing",
        "prompt_invalid",
        "prompt_owner_invalid",
        "prompt_group_invalid",
        "prompt_mode_invalid",
        "prompt_size_mismatch",
        "prompt_hash_mismatch",
        "profile_missing",
        "profile_invalid",
        "profile_owner_invalid",
        "profile_group_invalid",
        "profile_mode_invalid",
        "profile_size_mismatch",
        "profile_hash_mismatch",
        "token_missing",
        "token_invalid",
        "token_owner_invalid",
        "token_group_invalid",
        "token_mode_invalid",
        "token_size_mismatch",
        "token_hash_mismatch",
        "out_dir_owner_invalid",
        "masked_source",
        "private_tmp_source",
        "user_unavailable",
        "group_unavailable",
        "adapter_sandbox_group_missing",
        "path_unavailable",
        "runtime_root_mode_invalid",
        "broker_socket_invalid",
        "workload_invalid",
    ],
)
def test_launch_pre_start_helper_codes_are_definite(tmp_path, code):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    runner = Runner(
        [{"returncode": 1, "stdout": "", "stderr": json.dumps({"error": code})}]
    )

    with pytest.raises(SupervisorError) as excinfo:
        HelperSandboxSupervisor(config, runner=runner).launch(spec)

    assert excinfo.value.code == code
    assert excinfo.value.ambiguous is False


@pytest.mark.parametrize(
    "code",
    [
        "unit_already_exists",
        "unit_not_found",
        "unit_failed",
        "unit_not_active",
        "unit_start_timeout",
        "started_marker_timeout",
        "started_marker_invalid",
        "process_uid_unavailable",
        "io_error",
        "internal_error",
    ],
)
def test_launch_post_start_helper_codes_are_ambiguous(tmp_path, code):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    runner = Runner(
        [{"returncode": 1, "stdout": "", "stderr": json.dumps({"error": code})}]
    )

    with pytest.raises(SupervisorError) as excinfo:
        HelperSandboxSupervisor(config, runner=runner).launch(spec)

    assert excinfo.value.code == code
    assert excinfo.value.ambiguous is True


def test_helper_errors_are_stable_and_do_not_leak_output(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    error_runner = Runner(
        [{"returncode": 1, "stdout": "", "stderr": '{"error":"invalid_digest"}'}]
    )
    supervisor = HelperSandboxSupervisor(config, runner=error_runner)

    with pytest.raises(SupervisorError) as excinfo:
        supervisor.prepare(spec)
    assert excinfo.value.code == "invalid_digest"
    assert excinfo.value.ambiguous is False
    assert "PROMPT-CANARY" not in str(excinfo.value)

    ambiguous_runner = Runner(
        [
            {
                "returncode": 1,
                "stdout": "",
                "stderr": '{"error":"started_marker_timeout"}',
            }
        ]
    )
    supervisor = HelperSandboxSupervisor(config, runner=ambiguous_runner)
    with pytest.raises(SupervisorError) as excinfo:
        supervisor.launch(spec)
    assert excinfo.value.code == "started_marker_timeout"
    assert excinfo.value.ambiguous is True


def test_helper_rejects_malformed_timeout_and_duplicate_json(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)

    with pytest.raises(SupervisorError) as excinfo:
        HelperSandboxSupervisor(config, runner=Runner(["not-json"])).prepare(spec)
    assert excinfo.value.code == "helper_response_invalid"

    duplicate = '{"ok":true,"ok":true}'
    with pytest.raises(SupervisorError) as excinfo:
        HelperSandboxSupervisor(config, runner=Runner([duplicate])).prepare(spec)
    assert excinfo.value.code == "helper_response_invalid"

    timeout = subprocess.TimeoutExpired(cmd=["sudo"], timeout=1)
    with pytest.raises(SupervisorError) as excinfo:
        HelperSandboxSupervisor(config, runner=Runner([timeout])).prepare(spec)
    assert excinfo.value.code == "helper_timeout"
    assert excinfo.value.ambiguous is True


def test_helper_status_builds_the_request_from_the_manifest(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    write_fake_manifest(config, spec)
    response = {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "status",
                "unit": spec.unit,
                "uid": 12345,
                "unit_state": {
                    "load_state": "loaded",
                    "active_state": "active",
                    "sub_state": "running",
                    "main_pid": 4321,
                    "result": None,
                    "exit_status": None,
                    "process_gone": False,
                },
                "task": {
                    "started": True,
                    "heartbeat_at": _iso(NOW),
                    "exit_code": None,
                    "timed_out": False,
                    "cancelled": False,
                    "result_present": False,
                },
                "observed_at": _iso(NOW),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    runner = Runner([response])
    supervisor = HelperSandboxSupervisor(config, runner=runner)

    status = supervisor.status(spec.unit)

    assert status.unit == spec.unit
    assert status.task_id == "task_1"
    assert status.runtime_handle == spec.runtime_handle
    assert status.active_state == "active"
    assert status.started is True
    assert status.process_gone is False
    assert status.heartbeat_at == NOW
    request = json.loads(str(runner.calls[0][1]["input"]))
    assert request["operation"] == "status"
    assert request["idempotency_key"] == "idem_1"
    assert request["prompt_sha256"] == spec.prompt_sha256


def test_helper_stop_and_cleanup_map_process_gone(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    write_fake_manifest(config, spec)
    stop_response = {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "stop",
                "unit": spec.unit,
                "unit_state": {
                    "load_state": "loaded",
                    "active_state": "inactive",
                    "sub_state": "dead",
                    "main_pid": 0,
                    "result": "success",
                    "exit_status": 0,
                    "process_gone": True,
                },
                "process_gone": True,
                "stop_confirmed": True,
                "observed_at": _iso(NOW),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    cleanup_response = {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "cleanup",
                "unit": spec.unit,
                "removed": False,
                "process_gone": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    runner = Runner([stop_response, cleanup_response])
    supervisor = HelperSandboxSupervisor(config, runner=runner)

    stopped = supervisor.stop(spec.unit, reason="test")
    supervisor.cleanup(spec.unit, result_read=False)
    supervisor.cleanup(spec.unit, result_read=True)

    assert stopped.process_gone is True
    assert stopped.stop_confirmed is True
    assert len(runner.calls) == 2
    cleanup_request = json.loads(str(runner.calls[1][1]["input"]))
    assert cleanup_request["operation"] == "cleanup"
    assert cleanup_request["result_read"] is True


@pytest.mark.parametrize("removed", [True, False])
def test_helper_cleanup_accepts_both_idempotent_removed_values(tmp_path, removed):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    write_fake_manifest(config, spec)
    runner = Runner(
        [
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "ok": True,
                        "operation": "cleanup",
                        "unit": spec.unit,
                        "removed": removed,
                        "process_gone": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        ]
    )

    HelperSandboxSupervisor(config, runner=runner).cleanup(spec.unit, result_read=True)


def test_helper_cleanup_unconfirmed_or_malformed_fails_closed(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    write_fake_manifest(config, spec)
    unconfirmed = {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "cleanup",
                "unit": spec.unit,
                "removed": True,
                "process_gone": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    with pytest.raises(SupervisorError) as excinfo:
        HelperSandboxSupervisor(config, runner=Runner([unconfirmed])).cleanup(
            spec.unit, result_read=True
        )
    assert excinfo.value.code == "cleanup_unconfirmed"

    malformed = {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "ok": True,
                "operation": "cleanup",
                "unit": spec.unit,
                "removed": "yes",
                "process_gone": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    with pytest.raises(SupervisorError) as excinfo:
        HelperSandboxSupervisor(config, runner=Runner([malformed])).cleanup(
            spec.unit, result_read=True
        )
    assert excinfo.value.code == "helper_response_invalid"


def test_helper_runtime_attestation_uses_a_task_free_sentinel(tmp_path):
    config = helper_config(tmp_path)
    runner = Runner(
        [
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "ok": True,
                        "operation": "attest",
                        "unit": unit_name_for(
                            "agentmgr-runtime-attestation",
                            "agentmgr-runtime-attestation",
                        ),
                        "runtime_attestation": {
                            **runtime_document(config),
                            "hmac": _sign(runtime_document(config)),
                        },
                        "task_attestation": None,
                        "observed_at": _iso(NOW),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        ]
    )
    supervisor = HelperSandboxSupervisor(config, runner=runner)

    attestation = supervisor.runtime_attestation()

    assert isinstance(attestation, RuntimeAttestation)
    assert attestation.runtime_name == config.runtime_name
    request = json.loads(str(runner.calls[0][1]["input"]))
    assert request["operation"] == "attest"
    assert request["task_id"] == "agentmgr-runtime-attestation"


def test_helper_prepare_rejects_a_non_fresh_directory(tmp_path):
    config = helper_config(tmp_path)
    spec = helper_spec(config)
    response = prepare_response(spec)
    payload = json.loads(response["stdout"])  # type: ignore[arg-type]
    payload["created"] = False
    response["stdout"] = json.dumps(payload)  # type: ignore[index]
    supervisor = HelperSandboxSupervisor(config, runner=Runner([response]))

    with pytest.raises(SupervisorError) as excinfo:
        supervisor.prepare(spec)
    assert excinfo.value.code == "task_dir_not_fresh"


# ---------------------------------------------------------------------------
# no-overclaim guards
# ---------------------------------------------------------------------------


def test_adapter_never_calls_a_model_or_completion_path(tmp_path):
    _, supervisor, adapter, _ = accepted_submission(tmp_path)

    # The adapter has no model client and no task-state dependency; the only
    # useful assertion here is that no unexpected supervisor call happened.
    assert supervisor.launch_calls == 1
    assert supervisor.stop_calls == 0
    assert supervisor.cleanup_calls == []


def test_error_strings_never_contain_prompt_or_token(tmp_path):
    config, supervisor, adapter = build_adapter(tmp_path)
    supervisor.launch_error = SupervisorError("started_not_confirmed", ambiguous=True)

    result = adapter.submit_task(
        "task_1", "PROMPT-CANARY-SECRET", "idem_1", managed_grant()
    )

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.error is not None
    assert "PROMPT-CANARY-SECRET" not in str(result.error)
    token_path = config.task_root / unit_name_for("task_1", "idem_1") / "egress.token"
    if token_path.exists():
        token = token_path.read_text(encoding="ascii")
        assert token not in str(result.error)


def test_adapter_is_a_runtime_adapter(tmp_path):
    from app.runtime.contract import RuntimeAdapter

    _, _, adapter = build_adapter(tmp_path)

    assert isinstance(adapter, RuntimeAdapter)
