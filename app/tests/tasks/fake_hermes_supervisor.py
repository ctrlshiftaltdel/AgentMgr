"""Scripted ``SandboxSupervisor`` for the #97 dispatcher end-to-end tests.

The fake stands in for the root helper (``ops/security/agentmgr_hermes_exec.py``)
while the real :class:`~app.runtime.hermes_managed.HermesManagedAdapter`, the
real PostgreSQL task fixtures and the real #96 :class:`RuntimeDispatcher` do all
the work.  It only reports facts:

* :meth:`prepare` creates ``<task_dir>/out`` and writes the helper-shaped
  ``manifest.json`` (including ``payload_sha256`` over the exact binding
  document the adapter's :func:`read_manifest` verifies);
* :meth:`launch` writes the helper/workload-shaped ``status/started.json`` and
  returns a canonically signed task attestation;
* :meth:`status` / :meth:`stop` replay a scripted unit state, including the
  normal systemd "unloaded after success" state
  (``active_state="not-found", started=True, exit_code=0, process_gone=True``);
* :meth:`runtime_attestation` returns a freshly signed runtime attestation so
  the adapter's managed capabilities are ``verified`` in the test;
* :meth:`cleanup` records the ``result_read`` argument.

Nothing here schedules a real unit, reads the database or writes task state.
This is test-only evidence and never upgrades a real runtime's capability
labels.
"""

from __future__ import annotations

import grp
import hashlib
import hmac
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.runtime.hermes_managed import (
    MANIFEST_SCHEMA,
    RUNTIME_ATTESTATION_SCHEMA,
    TASK_ATTESTATION_SCHEMA,
    HermesManagedConfig,
    RuntimeAttestation,
    SandboxSpec,
    SandboxSpecError,
    SandboxStatus,
    SandboxStop,
    SupervisorError,
    TaskAttestation,
    read_manifest,
)

__all__ = [
    "ACCEPTED",
    "DEFAULT_UID",
    "FAILED",
    "NEVER_STARTED",
    "RUNNING",
    "SANDBOX_FACTS",
    "STOPPED",
    "TIMED_OUT_GONE",
    "UNKNOWN_EXIT",
    "UNLOADED_SUCCESS",
    "FakeHermesSupervisor",
]

#: The unit UID the helper binds from the real process.  Any positive int is
#: accepted by the attestation parser; the fake never spawns a process.
DEFAULT_UID = 12_345

#: Sandbox facts the helper guarantees and the adapter re-verifies.
SANDBOX_FACTS: Mapping[str, object] = {
    "private_network": True,
    "protect_home": True,
    "protect_system": "strict",
    "no_new_privileges": True,
    "capability_bounding_set": "empty",
    "external_ip": False,
    "toolsets": ["terminal"],
}

#: One unit-state fact set.  ``heartbeat_seconds_ago`` is a script-local key;
#: the fake turns it into a concrete ``heartbeat_at`` at observation time.
RUNNING = {
    "active_state": "active",
    "sub_state": "running",
    "main_pid": 4_321,
    "result": None,
    "exit_status": None,
    "started": True,
    "heartbeat_seconds_ago": 5,
    "exit_code": None,
    "timed_out": False,
    "cancelled": False,
    "process_gone": False,
}

#: A submitted unit that systemd reports as still starting (``accepted``).
ACCEPTED = {
    "active_state": "activating",
    "sub_state": "start-pre",
    "main_pid": None,
    "result": None,
    "exit_status": None,
    "started": False,
    "heartbeat_seconds_ago": None,
    "exit_code": None,
    "timed_out": False,
    "cancelled": False,
    "process_gone": False,
}

#: The normal systemd success end state: the transient unit was unloaded, so
#: ``active_state`` is ``not-found`` while the workload exit marker says 0.
UNLOADED_SUCCESS = {
    "active_state": "not-found",
    "sub_state": None,
    "main_pid": None,
    "result": "success",
    "exit_status": 0,
    "started": True,
    "heartbeat_seconds_ago": None,
    "exit_code": 0,
    "timed_out": False,
    "cancelled": False,
    "process_gone": True,
}

#: A live run that the workload declared an execution timeout and exited.
TIMED_OUT_GONE = {
    "active_state": "failed",
    "sub_state": "failed",
    "main_pid": None,
    "result": "timeout",
    "exit_status": None,
    "started": True,
    "heartbeat_seconds_ago": None,
    "exit_code": None,
    "timed_out": True,
    "cancelled": False,
    "process_gone": True,
}

#: A run that exited non-zero after writing its own exit marker.
FAILED = {
    "active_state": "failed",
    "sub_state": "failed",
    "main_pid": None,
    "result": "exit-code",
    "exit_status": 1,
    "started": True,
    "heartbeat_seconds_ago": None,
    "exit_code": 1,
    "timed_out": False,
    "cancelled": False,
    "process_gone": True,
}

#: A gone run without a workload exit marker: systemd exit is not proof.
UNKNOWN_EXIT = {
    "active_state": "inactive",
    "sub_state": "dead",
    "main_pid": None,
    "result": None,
    "exit_status": 0,
    "started": True,
    "heartbeat_seconds_ago": None,
    "exit_code": None,
    "timed_out": False,
    "cancelled": False,
    "process_gone": True,
}

#: A confirmed stop (cancel/stop request observed by the workload).
STOPPED = {
    "active_state": "inactive",
    "sub_state": "dead",
    "main_pid": None,
    "result": "success",
    "exit_status": 0,
    "started": True,
    "heartbeat_seconds_ago": None,
    "exit_code": None,
    "timed_out": False,
    "cancelled": True,
    "process_gone": True,
}

#: A prepared task whose unit never started.
NEVER_STARTED = {
    "active_state": "not-found",
    "sub_state": None,
    "main_pid": None,
    "result": None,
    "exit_status": None,
    "started": False,
    "heartbeat_seconds_ago": None,
    "exit_code": None,
    "timed_out": False,
    "cancelled": False,
    "process_gone": True,
}

def _canonical(document: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(document), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _share_with_sandbox_group(path: Path) -> None:
    """Give the fake manifest the production sandbox group when it exists.

    The real helper writes the manifest group-readable; on a provisioned host a
    test user in ``agentmgr-hermes`` can set that group without root.  Plain CI
    has no such group and the adapter falls back to the mode/owner checks.
    """

    try:
        gid = grp.getgrnam("agentmgr-hermes").gr_gid
    except KeyError:
        return
    os.chown(path, -1, gid)


def _sign(document: Mapping[str, object], key: bytes) -> dict[str, object]:
    """Return ``document`` plus the canonical HMAC-SHA256 hex signature."""

    signed = dict(document)
    signed["hmac"] = hmac.new(key, _canonical(document), hashlib.sha256).hexdigest()
    return signed


class FakeHermesSupervisor:
    """Scripted supervisor implementing the Task 5 ``SandboxSupervisor`` protocol.

    ``result_bytes`` simulates the workload writing ``out/result.txt`` (the only
    artifact the adapter ever hashes).  ``status_script`` is a sequence of unit
    fact sets; each :meth:`status` call consumes the next one and the final item
    repeats.  Without a script the fake reports ``running`` after a launch, the
    stopped fact after a confirmed stop and ``not-found/never started`` before a
    launch, so an unscripted run is always a live run.

    ``status_hook`` runs after each status observation; the dispatcher tests use
    it to commit a real cancel request on a second database connection.  Every
    operation increments a counter so a test can prove a fail-closed path never
    reached the sandbox.
    """

    def __init__(
        self,
        config: HermesManagedConfig,
        *,
        attestation_key: bytes | None = None,
        clock: Callable[[], datetime] | None = None,
        result_bytes: bytes | None = None,
        status_script: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        if not isinstance(config, HermesManagedConfig):
            raise TypeError("fake supervisor requires a managed configuration")
        self.config = config
        self.attestation_key = (
            Path(config.attestation_key_path).read_bytes()
            if attestation_key is None
            else attestation_key
        )
        self._clock = clock or (lambda: datetime.now(UTC))

        #: The only artifact the adapter may hash, or ``None`` for no artifact.
        self.result_bytes = result_bytes
        self.status_script: list[dict[str, object]] = [
            dict(item) for item in (status_script or ())
        ]
        self.status_index = 0
        self.status_hook: Callable[[str, int], None] | None = None

        # Scripted failures.  ``launch_error`` is normally a
        # :class:`SupervisorError` whose ``ambiguous`` flag decides replay.
        self.prepare_error: BaseException | None = None
        self.launch_error: BaseException | None = None
        self.status_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.attestation_error: BaseException | None = None
        self.write_manifest = True

        # Scripted stop confirmation.  ``False`` models a stop request the
        # workload has not honoured yet (``requested`` is not a stop).
        self.stop_process_gone = True

        self.prepare_calls = 0
        self.launch_calls = 0
        self.status_calls = 0
        self.stop_calls = 0
        self.cleanup_calls: list[tuple[str, bool]] = []
        self.attestation_calls = 0

        self.prepare_specs: list[SandboxSpec] = []
        self.launch_specs: list[SandboxSpec] = []
        self.status_units: list[str] = []
        self.stop_reasons: list[str] = []

        self._launched = False
        self._stopped = False
        self._stop_confirmed = False

    # -- protocol ------------------------------------------------------------

    def prepare(self, spec: SandboxSpec) -> None:
        self.prepare_calls += 1
        self.prepare_specs.append(spec)
        if self.prepare_error is not None:
            raise self.prepare_error
        spec.task_dir.mkdir(parents=True, exist_ok=True)
        (spec.task_dir / "out").mkdir(exist_ok=True)
        if self.write_manifest:
            self._write_manifest(spec)

    def launch(self, spec: SandboxSpec) -> TaskAttestation:
        self.launch_calls += 1
        self.launch_specs.append(spec)
        if self.launch_error is not None:
            raise self.launch_error
        if not (spec.task_dir / "manifest.json").is_file():
            # The helper refuses to start a unit without its durable manifest.
            raise SupervisorError("manifest_unavailable")
        self._launched = True
        self._write_started(spec)
        if self.result_bytes is not None:
            artifact = spec.task_dir / "out" / "result.txt"
            artifact.write_bytes(self.result_bytes)
            os.chmod(artifact, 0o640)
        return self._task_attestation(spec)

    def status(self, unit: str) -> SandboxStatus:
        self.status_calls += 1
        self.status_units.append(unit)
        if self.status_error is not None:
            raise self.status_error
        if self.status_hook is not None:
            self.status_hook(unit, self.status_calls)
        try:
            manifest = read_manifest(self.config, unit)
        except SandboxSpecError:
            raise SupervisorError("manifest_invalid") from None
        if manifest is None:
            raise SupervisorError("manifest_unavailable")
        facts = self._next_facts()
        observed_at = self._clock()
        heartbeat_at = None
        if facts.get("heartbeat_seconds_ago") is not None:
            heartbeat_at = observed_at - timedelta(
                seconds=int(facts["heartbeat_seconds_ago"])
            )
        return SandboxStatus(
            unit=manifest.unit,
            task_id=manifest.task_id,
            runtime_handle=manifest.runtime_handle,
            active_state=str(facts["active_state"]),
            sub_state=(
                None if facts.get("sub_state") is None else str(facts["sub_state"])
            ),
            main_pid=(
                None if facts.get("main_pid") is None else int(facts["main_pid"])
            ),
            result=(
                None if facts.get("result") is None else str(facts["result"])
            ),
            exit_status=(
                None
                if facts.get("exit_status") is None
                else int(facts["exit_status"])
            ),
            started=bool(facts.get("started")),
            heartbeat_at=heartbeat_at,
            exit_code=(
                None if facts.get("exit_code") is None else int(facts["exit_code"])
            ),
            timed_out=bool(facts.get("timed_out")),
            cancelled=bool(facts.get("cancelled")),
            process_gone=bool(facts.get("process_gone")),
            observed_at=observed_at,
        )

    def stop(self, unit: str, *, reason: str) -> SandboxStop:
        self.stop_calls += 1
        self.stop_reasons.append(reason)
        if self.stop_error is not None:
            raise self.stop_error
        confirmed = self.stop_process_gone
        self._stopped = True
        self._stop_confirmed = confirmed
        return SandboxStop(
            unit=unit,
            active_state="inactive" if confirmed else "active",
            result=None,
            exit_status=None,
            main_pid=None if confirmed else 4_321,
            process_gone=confirmed,
            stop_confirmed=confirmed,
            observed_at=self._clock(),
        )

    def cleanup(self, unit: str, *, result_read: bool = False) -> None:
        self.cleanup_calls.append((unit, result_read))
        if not result_read:
            return
        task_dir = self.config.task_root / unit
        if task_dir.exists():
            shutil.rmtree(task_dir)

    def runtime_attestation(self) -> RuntimeAttestation:
        self.attestation_calls += 1
        if self.attestation_error is not None:
            raise self.attestation_error
        current = self._clock()
        document = {
            "schema": RUNTIME_ATTESTATION_SCHEMA,
            "helper_version": "1.0.0",
            "runtime_name": self.config.runtime_name,
            "runtime_version": self.config.runtime_version,
            "runtime_root_hash": self.config.runtime_root_hash,
            "sandbox_facts": dict(SANDBOX_FACTS),
            "broker_socket_ok": True,
            "evidence_ref": self.config.evidence_ref,
            "issued_at": _iso_utc(current - timedelta(seconds=30)),
            "expires_at": _iso_utc(current + timedelta(seconds=300)),
            "nonce": os.urandom(16).hex(),
        }
        return RuntimeAttestation.from_document(
            _sign(document, self.attestation_key)
        )

    # -- test helpers --------------------------------------------------------

    def script(self, states: Sequence[Mapping[str, object]]) -> None:
        """Replace the status script and restart it at the first entry."""

        self.status_script = [dict(item) for item in states]
        self.status_index = 0

    def unit_dirs(self) -> list[Path]:
        """Directories currently present under the trusted task root."""

        root = Path(self.config.task_root)
        try:
            return sorted(
                entry for entry in root.iterdir() if entry.is_dir()
            )
        except FileNotFoundError:
            return []

    # -- internals -----------------------------------------------------------

    def _next_facts(self) -> dict[str, object]:
        if self.status_script:
            index = min(self.status_index, len(self.status_script) - 1)
            self.status_index += 1
            return dict(self.status_script[index])
        if self._stopped:
            if self._stop_confirmed:
                return dict(STOPPED)
            return dict(RUNNING)
        if self._launched:
            return dict(RUNNING)
        return dict(NEVER_STARTED)

    def _write_manifest(self, spec: SandboxSpec) -> None:
        """Write the exact Task 2 helper manifest shape (binding digest last)."""

        binding: dict[str, object] = {
            "schema": MANIFEST_SCHEMA,
            "unit": spec.unit,
            "task_id": spec.task_id,
            "grant_id": spec.grant_id,
            "runtime_handle": spec.runtime_handle,
            "idempotency_key": spec.idempotency_key,
            "runtime_root_hash": self.config.runtime_root_hash,
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
        # ``payload_sha256`` covers the binding document only; ``created_at``
        # and ``model`` are deliberately excluded, exactly like the helper.
        document["payload_sha256"] = hashlib.sha256(_canonical(binding)).hexdigest()
        document["created_at"] = _iso_utc(self._clock())
        if self.config.model_name:
            document["model"] = self.config.model_name
        path = spec.task_dir / "manifest.json"
        path.write_bytes(_canonical(document))
        os.chmod(path, 0o640)
        _share_with_sandbox_group(path)

    def _write_started(self, spec: SandboxSpec) -> None:
        """Write the workload-shaped ``status/started.json`` marker."""

        status_dir = spec.task_dir / "status"
        status_dir.mkdir(exist_ok=True)
        document = {
            "unit": spec.unit,
            "task_id": spec.task_id,
            "runtime_handle": spec.runtime_handle,
            "workload_pid": 4_320,
            "child_pid": 4_321,
            "started_at": _iso_utc(self._clock()),
        }
        path = status_dir / "started.json"
        path.write_bytes(_canonical(document))
        os.chmod(path, 0o640)

    def _task_attestation(self, spec: SandboxSpec) -> TaskAttestation:
        current = self._clock()
        document = {
            "schema": TASK_ATTESTATION_SCHEMA,
            "unit": spec.unit,
            "task_id": spec.task_id,
            "grant_id": spec.grant_id,
            "runtime_handle": spec.runtime_handle,
            "uid": DEFAULT_UID,
            "runtime_root_hash": self.config.runtime_root_hash,
            "profile_template_sha256": spec.profile_template_sha256,
            "prompt_sha256": spec.prompt_sha256,
            "sandbox_facts": dict(SANDBOX_FACTS),
            "issued_at": _iso_utc(current - timedelta(seconds=60)),
            "expires_at": _iso_utc(current + timedelta(seconds=300)),
            "nonce": os.urandom(16).hex(),
        }
        return TaskAttestation.from_document(_sign(document, self.attestation_key))
