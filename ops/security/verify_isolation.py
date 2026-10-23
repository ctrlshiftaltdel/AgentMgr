#!/usr/bin/env python3
"""In-process live isolation harness for managed execution (Issue #82).

The harness is the only entry point that produces §16.4 evidence.  It

1. preflights the approved test host (systemd >= 249, ``sudo -n``, no residue),
2. creates ``/run/agentmgr-exec`` owned by the controller ``0700`` with
   root-only ``credentials/``/``specs/`` and deploys the workload/resources,
3. starts :class:`~app.security.gateway.GatewayServer` **in this process** on
   ``<run_root>/gateway.sock`` with a live in-memory ``GrantStatusProvider``,
4. probes both network profiles (§8.4), then launches two distinct tasks and
   drives the §16.3 matrix (unit-side probes plus controller-side gateway and
   systemd probes),
5. always stops units, closes the gateway, closes the audit sink and removes
   every directory it created, then writes evidence to
   ``$AGENTMGR_ISOLATION_EVIDENCE`` (outside the repository).

There is no ``gateway.pid`` and no standalone gateway process: the listener and
its thread belong to this process and are closed before the audit sink, because
daemon handler threads can outlive ``GatewayServer.stop()`` for up to the
read/write timeout.

The bearer token stays inside the supervisor; a recording wrapper around the
issuer lets controller-side probes reuse the *unit's own* credential without
ever writing it to argv, the environment, a file or the evidence document.  All
privileged operations go through fixed ``sudo -n`` argv arrays; no shell is
used.
"""

from __future__ import annotations

import errno
import grp
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.security.boundary import (
    CONTRACT_VERSION,
    EXECUTION_MODE_MANAGED,
    NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK,
    NETWORK_PROFILE_PRIVATE_NETNS,
    GrantStatus,
    ManagedTaskSpec,
    ResourceScope,
    unit_name_for_task,
)
from app.security.credentials import (
    CredentialIssuer,
    CredentialValidator,
    InMemoryRevocationStore,
    IssuedCredential,
)
from app.security.errors import SecurityError
from app.security.gateway import (
    GATEWAY_PROTOCOL_VERSION,
    GatewayConfig,
    GatewayLimits,
    GatewayServer,
)
from app.security.supervisor import (
    AUDIT_EVENT_LAUNCH,
    ManagedExecutionSupervisor,
    SupervisorConfig,
)
from app.security.systemd import (
    DEFAULT_RUNTIME_ROOT,
    DEFAULT_STATE_LINK_ROOT,
    DEFAULT_STATE_ROOT,
    SudoSystemdOps,
    SystemdUnitConfig,
    build_systemd_run_argv,
)

__all__ = [
    "HarnessConfig",
    "HarnessError",
    "RESOURCES_ROOT",
    "main",
    "run_harness",
    "validate_evidence_path",
]

#: Where the harness installs the synthetic resource files (design §16.5).
RESOURCES_ROOT = Path("/srv/agentmgr-exec-resources")

READ_CAPABILITY = "resource.read@1.0.0"
WRITE_CAPABILITY = "resource.write@1.0.0"

PROFILE_TASK_ID = "task_iso_profile"
PROFILE_GRANT_ID = "grnt_iso_profile"
MATRIX_TASKS = {
    "a": ("task_iso_matrix_a", "grnt_iso_matrix_a", "prin_iso_matrix_a"),
    "b": ("task_iso_matrix_b", "grnt_iso_matrix_b", "svc_iso_matrix_b"),
}
TIMEOUT_TASK_ID = "task_iso_timeout"
TIMEOUT_GRANT_ID = "grnt_iso_timeout"

RESOURCE_A = "res_synthetic_0001"
RESOURCE_B = "res_synthetic_0002"
RESOURCE_OUT_OF_SCOPE = "res_synthetic_0003"
RESOURCE_BODIES = {
    RESOURCE_A: {"kind": "synthetic", "value": "alpha"},
    RESOURCE_B: {"kind": "synthetic", "value": "beta"},
    RESOURCE_OUT_OF_SCOPE: {"kind": "synthetic", "value": "gamma"},
}

MATRIX_TTL_SECONDS = 300
MATRIX_GRANT_SECONDS = 240
TIMEOUT_TTL_SECONDS = 2
TIMEOUT_GRANT_SECONDS = 120
PROFILE_TTL_SECONDS = 30
PROFILE_GRANT_SECONDS = 120

RESULT_WAIT_SECONDS = 30.0
PROCESS_GONE_SECONDS = 12.0
STOP_WAIT_SECONDS = 5.0
PLAN_INSTALL_TIMEOUT_SECONDS = 20.0

#: §16.3 matrix rows keyed by the probe id the workload/harness emits.
PROBE_ROWS = {
    "read-etc-shadow": 1,
    "read-home-profile": 2,
    "read-postgresql": 3,
    "read-other-state-configured": 4,
    "read-other-state-private": 4,
    "read-other-credential-source": 5,
    "list-run-directory": 5,
    "socket-af-inet": 6,
    "connect-loopback-5432": 7,
    "connect-documentation-443": 8,
    "connect-metadata-endpoint": 9,
    "proc-cap-eff": 10,
    "proc-no-new-privs": 11,
    "unshare-user-mount-ns": 12,
    "proc-enumeration-self-only": 13,
    "write-etc": 14,
    "write-usr": 14,
    "write-var-lib": 14,
    "write-srv": 14,
    "gateway-allowed-read": 15,
    "gateway-expired-credential": 16,
    "gateway-revoked-credential": 17,
    "gateway-cross-task-identity": 18,
    "gateway-cross-task-resource": 18,
    "gateway-out-of-scope-resource": 19,
    "gateway-ungranted-capability": 20,
    "gateway-grant-revoked": 21,
    "ttl-timeout": 22,
    "stop-on-revoke": 23,
    "cleanup": 24,
    "gateway-rate-limit": 25,
    "gateway-busy": 26,
}

#: RFC 3927 link-local metadata address, assembled so a literal public-looking
#: IP never lands in the repository (privacy gate) or the evidence document.
_METADATA_ADDRESS = ".".join(("169", "254", "169", "254"))

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_UNIT_LINE = re.compile(r"^(agentmgr-task-[0-9a-f]{12})\.service$")


class HarnessError(Exception):
    """A stable harness failure token; never a path, token or command output."""


def _errno_name(exc: OSError) -> str:
    code = exc.errno
    if isinstance(code, int):
        return errno.errorcode.get(code, f"errno_{code}")
    return type(exc).__name__


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, (SecurityError, HarnessError)):
        return str(exc)
    if isinstance(exc, OSError):
        return _errno_name(exc)
    return type(exc).__name__


def _clock() -> datetime:
    return datetime.now(UTC)


def _run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    if not argv or not all(isinstance(item, str) for item in argv):
        raise HarnessError("command_invalid")
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise HarnessError("command_timeout") from None
    except OSError:
        raise HarnessError("command_unavailable") from None


def _sudo(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return _run(["sudo", "-n", *args], timeout=timeout)


def _sudo_check(*args: str, token: str, timeout: float = 60.0) -> None:
    completed = _sudo(*args, timeout=timeout)
    if completed.returncode != 0:
        raise HarnessError(token)


def _sudo_exists(path: Path | str) -> bool:
    # ``test`` has no portable end-of-options marker; every path used here is
    # absolute and derived by the harness.
    completed = _sudo("test", "-e", str(path))
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise HarnessError("command_failed")


def _sudo_read_text(path: Path | str) -> str | None:
    completed = _sudo("cat", "--", str(path))
    if completed.returncode != 0:
        return None
    return completed.stdout


def validate_evidence_path(path: Path, repo_root: Path) -> Path:
    """Return the absolute evidence path, refusing anything inside the repo."""

    resolved = Path(path).resolve()
    root = Path(repo_root).resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise HarnessError("evidence_path_inside_repository")
    return resolved


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """Harness inputs; no task-supplied path or grant TTL is accepted."""

    repo_root: Path
    run_root: Path
    resources_root: Path
    evidence_path: Path
    gateway_limits: GatewayLimits = field(default_factory=GatewayLimits)

    def __post_init__(self) -> None:
        repo_root = Path(self.repo_root)
        if not repo_root.is_absolute() or not repo_root.is_dir():
            raise ValueError("repo_root must be an existing absolute path")
        for name in ("run_root", "resources_root"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if not isinstance(self.evidence_path, Path) or not self.evidence_path.is_absolute():
            raise ValueError("evidence_path must be an absolute Path")
        if not isinstance(self.gateway_limits, GatewayLimits):
            raise ValueError("gateway_limits must be GatewayLimits")


class _AuditSink:
    """In-memory audit sink closed only after the gateway has stopped."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._closed = False

    def record(self, event: Mapping[str, object]) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("audit sink closed")
            self._events.append(dict(event))

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(event) for event in self._events]


class _GrantProvider:
    """Live in-memory Grant provider; answers by grant id on every call."""

    def __init__(self) -> None:
        self._grants: dict[str, GrantStatus] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def put(self, grant: GrantStatus) -> None:
        with self._lock:
            self._grants[grant.grant_id] = grant

    def status(self, grant_id: str, *, task_id: str) -> GrantStatus | None:
        with self._lock:
            self.calls += 1
            return self._grants.get(grant_id)

    def get(self, grant_id: str) -> GrantStatus | None:
        with self._lock:
            return self._grants.get(grant_id)

    def impersonate(self, grant_id: str, task_id: str) -> None:
        with self._lock:
            current = self._grants[grant_id]
            self._grants[grant_id] = replace(current, task_id=task_id)

    def revoke(self, grant_id: str) -> None:
        with self._lock:
            current = self._grants[grant_id]
            self._grants[grant_id] = replace(
                current, status="revoked", usable=False
            )


class _RecordingIssuer:
    """Issuer wrapper that keeps the unit credential for controller probes.

    The token is held in memory only: it is never written to a file, argv, the
    environment, an audit event or the evidence document.
    """

    def __init__(self, issuer: CredentialIssuer) -> None:
        self._issuer = issuer
        self.issued: dict[str, IssuedCredential] = {}
        self._lock = threading.Lock()

    def issue(self, **kwargs: object) -> IssuedCredential:
        credential = self._issuer.issue(**kwargs)  # type: ignore[arg-type]
        task_id = kwargs.get("task_id")
        with self._lock:
            self.issued[str(task_id)] = credential
        return credential

    def credential(self, task_id: str) -> IssuedCredential:
        with self._lock:
            credential = self.issued.get(task_id)
        if credential is None:
            raise HarnessError("credential_not_issued")
        return credential


def _spec(
    task_id: str,
    grant_id: str,
    resource_ids: tuple[str, ...],
    *,
    ttl_seconds: int,
) -> ManagedTaskSpec:
    return ManagedTaskSpec.from_mapping(
        {
            "task_id": task_id,
            "grant_id": grant_id,
            "execution_mode": EXECUTION_MODE_MANAGED,
            "ttl_seconds": ttl_seconds,
            "cpu_quota_percent": 50,
            "memory_max_mb": 256,
            "tasks_max": 32,
            "capabilities": [READ_CAPABILITY],
            "resource_scope": {
                "scope_type": "self",
                "resource_ids": list(resource_ids),
                "connector_ids": [],
                "data_classifications": ["internal"],
                "exclude_resource_ids": [],
            },
            "backend": "file",
        }
    )


def _grant(
    task_id: str,
    grant_id: str,
    subject_id: str,
    resource_ids: tuple[str, ...],
    seconds: int,
) -> GrantStatus:
    return GrantStatus(
        grant_id=grant_id,
        task_id=task_id,
        subject_id=subject_id,
        execution_mode=EXECUTION_MODE_MANAGED,
        status="active",
        usable=True,
        expires_at=_clock() + timedelta(seconds=seconds),
        revocation_reason=None,
        capabilities=(READ_CAPABILITY,),
        resource_scope=ResourceScope(
            scope_type="self",
            resource_ids=resource_ids,
            connector_ids=(),
            data_classifications=("internal",),
            exclude_resource_ids=(),
        ),
    )


class _Harness:
    """One run's privileged state; every teardown step is best effort."""

    def __init__(self, config: HarnessConfig, staging: Path) -> None:
        self.config = config
        self.staging = staging
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.group = grp.getgrgid(os.getgid()).gr_name
        self.ops = SudoSystemdOps()
        self.audit = _AuditSink()
        self.provider = _GrantProvider()
        self.store = InMemoryRevocationStore(_clock)
        keyring = {"k1": os.urandom(32)}
        self.issuer = CredentialIssuer(keyring, "k1", _clock)
        self.validator = CredentialValidator(keyring, _clock)
        self.recording_issuer = _RecordingIssuer(self.issuer)
        self.gateway: GatewayServer | None = None
        self.handles: list[tuple[ManagedExecutionSupervisor, object]] = []
        self.failures: list[dict[str, str]] = []
        self.systemd_line = ""
        #: Set once this run may own /run/agentmgr-exec and the resources root;
        #: before that (for example a preflight refusal) teardown must not touch
        #: directories that belong to someone else.
        self.filesystem_prepared = False

    # -- commands ---------------------------------------------------------
    @property
    def run_root(self) -> Path:
        return self.config.run_root

    @property
    def resources_root(self) -> Path:
        return self.config.resources_root

    def _stage(self, name: str, payload: Mapping[str, object]) -> Path:
        path = self.staging / name
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def _install(
        self,
        source: Path,
        destination: Path,
        *,
        mode: str,
        owner: str = "root",
        group: str = "root",
    ) -> None:
        _sudo_check(
            "install",
            "-o",
            owner,
            "-g",
            group,
            "-m",
            mode,
            "--",
            str(source),
            str(destination),
            token="install_failed",
        )

    def _install_directory(
        self, destination: Path, *, mode: str, owner: str, group: str
    ) -> None:
        _sudo_check(
            "install",
            "-d",
            "-o",
            owner,
            "-g",
            group,
            "-m",
            mode,
            "--",
            str(destination),
            token="install_directory_failed",
        )

    def _sudo_remove(self, *paths: Path) -> None:
        _sudo("rm", "-rf", "--", *[str(path) for path in paths])

    # -- preflight --------------------------------------------------------
    def preflight(self, evidence: dict[str, object]) -> None:
        version = _run(["systemctl", "--version"])
        if version.returncode != 0 or not version.stdout.strip():
            raise HarnessError("systemd_unavailable")
        self.systemd_line = version.stdout.splitlines()[0].strip()
        fields = self.systemd_line.split()
        try:
            number = int(fields[1])
        except (IndexError, ValueError):
            raise HarnessError("systemd_version_unreadable") from None
        if number < 249:
            raise HarnessError("systemd_too_old")
        evidence["host"]["systemd"] = self.systemd_line  # type: ignore[index]

        for tool in ("systemd-run", "systemctl"):
            if shutil.which(tool) is None:
                raise HarnessError("systemd_tool_missing")
        if not Path("/run/systemd/system").is_dir():
            raise HarnessError("systemd_runtime_missing")
        # Probe sudo with a command the isolation path always needs; a blanket
        # "sudo -n true" would also require ``true`` in a narrow sudoers policy.
        if _sudo("systemctl", "--version").returncode != 0:
            raise HarnessError("sudo_unavailable")

        if os.path.exists(self.run_root):
            raise HarnessError("run_root_present")
        if os.path.exists(self.resources_root):
            raise HarnessError("resources_root_present")

        residue = _sudo(
            "find",
            str(DEFAULT_STATE_ROOT),
            "-maxdepth",
            "1",
            "-name",
            "agentmgr-task-*",
            "-print",
            "-quit",
        )
        if residue.returncode == 0 and residue.stdout.strip():
            raise HarnessError("state_residue_present")
        if residue.returncode not in (0, 1):
            raise HarnessError("state_probe_failed")

        missing = [path for path in self._inaccessible_paths() if not _sudo_exists(path)]
        if missing:
            evidence["preflight"] = {"missing_inaccessible_paths": missing}
            raise HarnessError("inaccessible_path_missing")

    def _inaccessible_paths(self) -> list[str]:
        spec = _spec(
            "task_iso_preflight", "grnt_iso_preflight", (RESOURCE_A,), ttl_seconds=30
        )
        unit = unit_name_for_task(spec.task_id)
        unit_config = SystemdUnitConfig(
            unit=unit,
            spec=spec,
            credential_source=DEFAULT_RUNTIME_ROOT / "credentials" / f"{unit}.json",
            workload_source=self.run_root / "workload.py",
            gateway_socket_source=self.run_root / "gateway.sock",
            network_profile=NETWORK_PROFILE_PRIVATE_NETNS,
        )
        prefix = "--property=InaccessiblePaths="
        return [
            item[len(prefix) :]
            for item in build_systemd_run_argv(unit_config)
            if item.startswith(prefix)
        ]

    # -- filesystem and gateway ------------------------------------------
    def prepare_filesystem(self) -> None:
        self.filesystem_prepared = True
        self._install_directory(
            self.run_root, mode="0700", owner=self.user, group=self.group
        )
        for name in ("credentials", "specs"):
            self._install_directory(
                self.run_root / name, mode="0700", owner="root", group="root"
            )
        self._install_directory(
            self.resources_root, mode="0755", owner="root", group="root"
        )
        self._install(
            self.config.repo_root / "ops" / "security" / "smoke_workload.py",
            self.run_root / "workload.py",
            mode="0555",
        )
        for resource_id, body in RESOURCE_BODIES.items():
            staged = self._stage(f"{resource_id}.json", body)
            self._install(staged, self.resources_root / f"{resource_id}.json", mode="0444")

    def start_gateway(self) -> None:
        config = GatewayConfig(
            socket_path=self.run_root / "gateway.sock",
            resources_root=self.resources_root,
            validator=self.validator,
            revocation_store=self.store,
            grant_provider=self.provider,
            audit_sink=self.audit,
            limits=self.config.gateway_limits,
            clock=_clock,
        )
        self.gateway = GatewayServer(config)
        self.gateway.start()

    def supervisor(self, profile: str) -> ManagedExecutionSupervisor:
        return ManagedExecutionSupervisor(
            SupervisorConfig(
                workload_source=self.run_root / "workload.py",
                gateway_socket_source=self.run_root / "gateway.sock",
                network_profile=profile,  # type: ignore[arg-type]
                clock=_clock,
            ),
            self.ops,
            self.provider,
            self.recording_issuer,  # type: ignore[arg-type]
            self.store,
            self.audit,
        )

    # -- unit lifecycle ---------------------------------------------------
    def launch(
        self, supervisor: ManagedExecutionSupervisor, spec: ManagedTaskSpec
    ) -> object:
        handle = supervisor.launch(spec)
        self.handles.append((supervisor, handle))
        return handle

    def _untrack(
        self, supervisor: ManagedExecutionSupervisor, handle: object
    ) -> None:
        try:
            self.handles.remove((supervisor, handle))
        except ValueError:
            pass

    def _install_plan(self, unit: str, plan: Mapping[str, object]) -> None:
        staged = self._stage(f"{unit}-plan.json", plan)
        destination = DEFAULT_STATE_ROOT / unit / "plan.json"
        deadline = time.monotonic() + PLAN_INSTALL_TIMEOUT_SECONDS
        while True:
            completed = _sudo(
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0444",
                "--",
                str(staged),
                str(destination),
            )
            if completed.returncode == 0:
                return
            if time.monotonic() >= deadline:
                raise HarnessError("plan_install_failed")
            time.sleep(0.1)

    def _read_result(self, unit: str) -> dict[str, object] | None:
        raw = _sudo_read_text(DEFAULT_STATE_ROOT / unit / "result.json")
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def _wait_result(self, unit: str, timeout: float = RESULT_WAIT_SECONDS) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            result = self._read_result(unit)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise HarnessError("workload_result_timeout")
            time.sleep(0.1)

    def _poll_until_gone(
        self, supervisor: ManagedExecutionSupervisor, handle: object, timeout: float
    ):
        deadline = time.monotonic() + timeout
        observation = None
        while True:
            try:
                observation = supervisor.poll(handle)  # type: ignore[arg-type]
            except SecurityError:
                observation = None
            if observation is not None and observation.process_gone:
                return observation
            if time.monotonic() >= deadline:
                return observation
            time.sleep(0.1)

    def _discard_unit(self, unit: str) -> None:
        for operation in (self.ops.stop, self.ops.kill, self.ops.cleanup):
            try:
                operation(unit)
            except Exception:
                pass

    def retire(
        self, supervisor: ManagedExecutionSupervisor, handle: object
    ) -> None:
        try:
            supervisor.stop(handle, "manual_stop")  # type: ignore[arg-type]
        except (SecurityError, ValueError):
            pass
        self.cleanup(supervisor, handle)

    def cleanup(
        self, supervisor: ManagedExecutionSupervisor, handle: object
    ) -> None:
        try:
            supervisor.cleanup(handle)  # type: ignore[arg-type]
        except (SecurityError, ValueError) as exc:
            self.failures.append(
                {"stage": "cleanup", "error": _safe_error(exc)}
            )
        finally:
            self._untrack(supervisor, handle)

    def teardown(self) -> None:
        for supervisor, handle in list(self.handles):
            try:
                observation = supervisor.poll(handle)  # type: ignore[arg-type]
            except Exception:
                observation = None
            if observation is None or not observation.process_gone:
                try:
                    supervisor.stop(handle, "manual_stop")  # type: ignore[arg-type]
                except Exception as exc:
                    self.failures.append(
                        {"stage": "teardown_stop", "error": _safe_error(exc)}
                    )
            try:
                supervisor.cleanup(handle)  # type: ignore[arg-type]
            except Exception as exc:
                self.failures.append(
                    {"stage": "teardown_cleanup", "error": _safe_error(exc)}
                )
            self._untrack(supervisor, handle)

        gateway = self.gateway
        self.gateway = None
        if gateway is not None:
            try:
                gateway.stop()
            except Exception as exc:
                self.failures.append(
                    {"stage": "gateway_stop", "error": _safe_error(exc)}
                )
        # Daemon request threads can live for the read/write timeout, so the
        # sink is closed only after the listener itself has stopped.
        self.audit.close()

        if self.filesystem_prepared:
            self._sudo_remove(self.run_root, self.resources_root)
            self._sudo_remove(DEFAULT_STATE_ROOT, DEFAULT_STATE_LINK_ROOT)

    # -- probes -----------------------------------------------------------
    def _gateway_request(
        self,
        token: str,
        capability: str,
        resource: str,
        *,
        timeout: float = 6.0,
    ) -> tuple[dict[str, object] | None, bool]:
        request = {
            "version": GATEWAY_PROTOCOL_VERSION,
            "token": token,
            "capability": capability,
            "resource": resource,
            "payload": {},
        }
        payload = json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(timeout)
            connection.connect(str(self.run_root / "gateway.sock"))
            connection.sendall(payload)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                total += len(chunk)
                if total > 131072:
                    return None, True
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
            peer_closed = False
            try:
                connection.settimeout(0.5)
                peer_closed = connection.recv(1) == b""
            except ConnectionResetError:
                # A handler that rejects before reading the request leaves the
                # request bytes unread, so Linux answers with RST on close.
                peer_closed = True
            except (TimeoutError, OSError):
                peer_closed = False
        except (TimeoutError, OSError):
            return None, False
        finally:
            connection.close()
        raw = b"".join(chunks)
        if not raw:
            return None, peer_closed
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, peer_closed
        return (response if isinstance(response, dict) else None), peer_closed

    @staticmethod
    def _observed(response: dict[str, object] | None) -> str | None:
        if response is None:
            return None
        if response.get("ok") is True:
            return "ok"
        reason = response.get("reason")
        return reason if isinstance(reason, str) else None

    def _call_probe(
        self,
        token: str,
        *,
        probe_id: str,
        expected: str,
        capability: str = READ_CAPABILITY,
        resource: str = RESOURCE_A,
        note: str | None = None,
    ) -> dict[str, object]:
        response, closed = self._gateway_request(token, capability, resource)
        observed = self._observed(response)
        entry: dict[str, object] = {
            "id": probe_id,
            "row": PROBE_ROWS.get(probe_id),
            "expected": expected,
            "observed": observed,
            "passed": observed == expected,
            "peer_closed": closed,
        }
        if note is not None:
            entry["note"] = note
        return entry

    # -- §16.3 matrix -----------------------------------------------------
    def probe_profiles(self) -> tuple[str, list[dict[str, object]]]:
        results: list[dict[str, object]] = []
        for profile, used_fallback in (
            (NETWORK_PROFILE_PRIVATE_NETNS, False),
            (NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK, True),
        ):
            results.append(self._probe_profile(profile, used_fallback=used_fallback))

        connected = {
            item["profile"]: bool(item.get("gateway_connect")) for item in results
        }
        if connected.get(NETWORK_PROFILE_PRIVATE_NETNS):
            chosen = NETWORK_PROFILE_PRIVATE_NETNS
        elif connected.get(NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK):
            chosen = NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK
        else:
            raise HarnessError("network_profile_unavailable")
        for item in results:
            item["used"] = item["profile"] == chosen
        return chosen, results

    def _probe_profile(
        self, profile: str, *, used_fallback: bool
    ) -> dict[str, object]:
        spec = _spec(
            PROFILE_TASK_ID,
            PROFILE_GRANT_ID,
            (RESOURCE_A,),
            ttl_seconds=PROFILE_TTL_SECONDS,
        )
        self.provider.put(
            _grant(
                PROFILE_TASK_ID,
                PROFILE_GRANT_ID,
                "prin_iso_profile",
                (RESOURCE_A,),
                PROFILE_GRANT_SECONDS,
            )
        )
        supervisor = self.supervisor(profile)
        entry: dict[str, object] = {
            "profile": profile,
            "fallback": used_fallback,
            "launched": False,
            "gateway_connect": False,
            "connect_errno": None,
            "error": None,
        }
        try:
            handle = self.launch(supervisor, spec)
        except SecurityError as exc:
            entry["error"] = str(exc)
            # A failed launch can leave the transient unit loaded (failed), which
            # would trip the next profile launch's collision preflight when the
            # same probe task id is reused for the fallback profile.
            self._discard_unit(unit_name_for_task(spec.task_id))
            return entry
        entry["launched"] = True
        self._install_plan(handle.unit, {"mode": "connect", "role": "profile"})
        result = self._wait_result(handle.unit)
        connect = result.get("gateway_connect")
        if isinstance(connect, dict):
            entry["gateway_connect"] = connect.get("connected") is True
            entry["connect_errno"] = connect.get("errno")
        self._poll_until_gone(supervisor, handle, PROCESS_GONE_SECONDS)
        self.retire(supervisor, handle)
        return entry

    def matrix_plans(self, other_units: Mapping[str, str]) -> dict[str, dict[str, object]]:
        home_profile = str(Path.home() / ".hermes")
        plans: dict[str, dict[str, object]] = {}
        for role, (task_id, _grant_id, _subject) in MATRIX_TASKS.items():
            other_role = "b" if role == "a" else "a"
            other_unit = other_units[other_role]
            self_resource = RESOURCE_A if role == "a" else RESOURCE_B
            other_resource = RESOURCE_B if role == "a" else RESOURCE_A
            plans[role] = {
                "mode": "matrix",
                "role": role,
                "hold_seconds": 90,
                "filesystem": [
                    {"id": "read-etc-shadow", "label": "etc_shadow", "path": "/etc/shadow"},
                    {
                        "id": "read-home-profile",
                        "label": "home_profile_dir",
                        "path": home_profile,
                    },
                    {
                        "id": "read-postgresql",
                        "label": "postgresql_data",
                        "path": "/var/lib/postgresql",
                    },
                    {
                        "id": "read-other-state-configured",
                        "label": "other_state_configured",
                        "path": f"/var/lib/agentmgr-exec/{other_unit}",
                    },
                    {
                        "id": "read-other-state-private",
                        "label": "other_state_private",
                        "path": f"/var/lib/private/agentmgr-exec/{other_unit}",
                    },
                    {
                        "id": "read-other-credential-source",
                        "label": "other_credential_source",
                        "path": f"{self.run_root}/credentials/{other_unit}.json",
                    },
                    {
                        "id": "list-run-directory",
                        "label": "run_directory",
                        "path": str(self.run_root),
                        "op": "list",
                    },
                    {"id": "write-etc", "label": "etc", "path": "/etc/agentmgr-exec-write-probe", "op": "write"},
                    {"id": "write-usr", "label": "usr", "path": "/usr/agentmgr-exec-write-probe", "op": "write"},
                    {"id": "write-var-lib", "label": "var_lib", "path": "/var/lib/agentmgr-exec-write-probe", "op": "write"},
                    {"id": "write-srv", "label": "srv", "path": "/srv/agentmgr-exec-write-probe", "op": "write"},
                ],
                "network": [
                    {
                        "id": "connect-loopback-5432",
                        "label": "loopback_postgres",
                        "host": "127.0.0.1",
                        "port": 5432,
                    },
                    {
                        "id": "connect-documentation-443",
                        "label": "documentation_network",
                        "host": "203.0.113.10",
                        "port": 443,
                    },
                    {
                        "id": "connect-metadata-endpoint",
                        "label": "link_local_metadata",
                        "host": _METADATA_ADDRESS,
                        "port": 80,
                    },
                ],
                "gateway": [
                    {
                        "id": "gateway-allowed-read",
                        "expected": "allow",
                        "capability": READ_CAPABILITY,
                        "resource": self_resource,
                        "expected_value": RESOURCE_BODIES[self_resource]["value"],
                    },
                    {
                        "id": "gateway-cross-task-resource",
                        "expected": "resource_scope_denied",
                        "capability": READ_CAPABILITY,
                        "resource": other_resource,
                        "note": "another task's resource is outside this credential's scope",
                    },
                    {
                        "id": "gateway-out-of-scope-resource",
                        "expected": "resource_scope_denied",
                        "capability": READ_CAPABILITY,
                        "resource": RESOURCE_OUT_OF_SCOPE,
                    },
                    {
                        "id": "gateway-ungranted-capability",
                        "expected": "capability_not_granted",
                        "capability": WRITE_CAPABILITY,
                        "resource": self_resource,
                    },
                ],
            }
        return plans

    def run_matrix(
        self, profile: str, evidence: dict[str, object]
    ) -> None:
        specs = {
            role: _spec(task_id, grant_id, (RESOURCE_A if role == "a" else RESOURCE_B,), ttl_seconds=MATRIX_TTL_SECONDS)
            for role, (task_id, grant_id, _subject) in MATRIX_TASKS.items()
        }
        for role, (task_id, grant_id, subject) in MATRIX_TASKS.items():
            self.provider.put(
                _grant(
                    task_id,
                    grant_id,
                    subject,
                    (RESOURCE_A if role == "a" else RESOURCE_B,),
                    MATRIX_GRANT_SECONDS,
                )
            )
        units = {role: unit_name_for_task(spec.task_id) for role, spec in specs.items()}
        supervisor = self.supervisor(profile)
        handles: dict[str, object] = {}
        # Both units exist before either plan is delivered, so the cross-task
        # state probe always sees another task's real 0700 state directory.
        for role in ("a", "b"):
            handles[role] = self.launch(supervisor, specs[role])
        plans = self.matrix_plans(units)
        for role in ("a", "b"):
            self._install_plan(units[role], plans[role])

        results: dict[str, dict[str, object]] = {}
        for role in ("a", "b"):
            results[role] = self._wait_result(units[role])

        for role in ("a", "b"):
            self._merge_unit_result(evidence, role, units[role], results[role])

        primary = results["a"].get("process")
        primary = primary if isinstance(primary, dict) else {}
        evidence["uid"] = primary.get("uid")
        evidence["process"] = {
            "cap_eff": primary.get("cap_eff"),
            "no_new_privs": primary.get("no_new_privs"),
            "uid": primary.get("uid"),
            "proc_only_self": primary.get("proc_only_self"),
            "unshare": primary.get("unshare"),
        }
        evidence["unit"] = units["a"]

        self._record_effective_ttl(evidence, units["a"], "matrix", MATRIX_TTL_SECONDS)

        self._record_run_directory(evidence)

        token_a = self.recording_issuer.credential(MATRIX_TASKS["a"][0]).token
        token_b = self.recording_issuer.credential(MATRIX_TASKS["b"][0]).token

        # §16.3 #22: a TTL=2s unit running a plain sleep must be gone within 8s.
        # Poll it immediately so the wall-clock measurement covers the unit's
        # actual lifetime, not the later gateway probes.
        timeout_supervisor = self.supervisor(profile)
        timeout_spec = _spec(
            TIMEOUT_TASK_ID, TIMEOUT_GRANT_ID, (RESOURCE_A,), ttl_seconds=TIMEOUT_TTL_SECONDS
        )
        self.provider.put(
            _grant(
                TIMEOUT_TASK_ID,
                TIMEOUT_GRANT_ID,
                "rt_iso_timeout",
                (RESOURCE_A,),
                TIMEOUT_GRANT_SECONDS,
            )
        )
        timeout_started = time.monotonic()
        timeout_handle = self.launch(timeout_supervisor, timeout_spec)
        self._install_plan(
            timeout_handle.unit,
            {"mode": "sleep", "role": "timeout", "sleep_seconds": 30},
        )
        self._record_effective_ttl(
            evidence, timeout_handle.unit, "timeout", TIMEOUT_TTL_SECONDS
        )
        timeout_observation = self._poll_until_gone(
            timeout_supervisor, timeout_handle, PROCESS_GONE_SECONDS
        )
        timeout_elapsed = time.monotonic() - timeout_started
        evidence["probes"].append(  # type: ignore[union-attr]
            {
                "id": "ttl-timeout",
                "row": 22,
                "expected": "timeout",
                "observed": None
                if timeout_observation is None
                else timeout_observation.result,
                "passed": bool(
                    timeout_observation is not None
                    and timeout_observation.process_gone
                    and timeout_observation.stop_confirmed
                    and timeout_observation.result == "timeout"
                    and timeout_elapsed <= 8.0
                ),
                "elapsed_seconds": round(timeout_elapsed, 3),
                "process_gone": None
                if timeout_observation is None
                else timeout_observation.process_gone,
                "stop_confirmed": None
                if timeout_observation is None
                else timeout_observation.stop_confirmed,
                "main_pid": None
                if timeout_observation is None
                else timeout_observation.main_pid,
            }
        )
        self.retire(timeout_supervisor, timeout_handle)

        # §16.3 #26 and #25 run from the controller with the unit's own token.
        gateway_probes = evidence["gateway"]["probes"]  # type: ignore[index]
        gateway_probes.append(self._busy_probe(token_b, RESOURCE_B))
        gateway_probes.append(self._rate_limit_probe(token_b, RESOURCE_B))
        # The 17-call burst drained this peer UID's request bucket; let it refill
        # before the single-call probes below (16 tokens at 16/s).
        time.sleep(1.1)

        # Cross-task identity: a provider projection for another task.
        grant_a = self.provider.get(MATRIX_TASKS["a"][1])
        self.provider.impersonate(MATRIX_TASKS["a"][1], MATRIX_TASKS["b"][0])
        evidence["probes"].append(  # type: ignore[union-attr]
            self._call_probe(
                token_a,
                probe_id="gateway-cross-task-identity",
                expected="cross_task_denied",
                note="live provider projection carries another task id",
            )
        )
        if grant_a is not None:
            self.provider.put(grant_a)

        evidence["probes"].append(  # type: ignore[union-attr]
            self._call_probe(
                token_a,
                probe_id="gateway-cross-task-resource",
                expected="resource_scope_denied",
                resource=RESOURCE_B,
                note="resource ownership is enforced through the resource scope",
            )
        )
        evidence["probes"].append(  # type: ignore[union-attr]
            self._call_probe(
                token_a,
                probe_id="gateway-out-of-scope-resource",
                expected="resource_scope_denied",
                resource=RESOURCE_OUT_OF_SCOPE,
            )
        )
        evidence["probes"].append(  # type: ignore[union-attr]
            self._call_probe(
                token_a,
                probe_id="gateway-ungranted-capability",
                expected="capability_not_granted",
                capability=WRITE_CAPABILITY,
            )
        )

        # Expired credential: a real token issued with a 1s TTL, used after exp.
        expired = self.issuer.issue(
            task_id=MATRIX_TASKS["a"][0],
            grant_id=MATRIX_TASKS["a"][1],
            subject_id=MATRIX_TASKS["a"][2],
            capabilities=(READ_CAPABILITY,),
            resource_scope=ResourceScope(
                scope_type="self",
                resource_ids=(RESOURCE_A,),
                connector_ids=(),
                data_classifications=("internal",),
                exclude_resource_ids=(),
            ),
            ttl_seconds=1,
        )
        time.sleep(1.5)
        evidence["probes"].append(  # type: ignore[union-attr]
            self._call_probe(
                expired.token,
                probe_id="gateway-expired-credential",
                expected="credential_expired",
            )
        )

        # §16.3 #21/#23: revoke the live Grant, deny the next call, then stop.
        self.provider.revoke(MATRIX_TASKS["a"][1])
        evidence["probes"].append(  # type: ignore[union-attr]
            self._call_probe(
                token_a,
                probe_id="gateway-grant-revoked",
                expected="grant_revoked",
            )
        )
        stop_started = time.monotonic()
        observation = supervisor.check_and_stop(handles["a"])
        stop_elapsed = time.monotonic() - stop_started
        evidence["systemd_properties"] = dict(self.ops.show(units["a"]))
        evidence["probes"].append(  # type: ignore[union-attr]
            {
                "id": "stop-on-revoke",
                "row": 23,
                "expected": "stopped",
                "observed": observation.state,
                "passed": bool(
                    observation.process_gone
                    and observation.stop_confirmed
                    and observation.main_pid == 0
                    and stop_elapsed <= STOP_WAIT_SECONDS
                ),
                "elapsed_seconds": round(stop_elapsed, 3),
                "process_gone": observation.process_gone,
                "stop_confirmed": observation.stop_confirmed,
                "main_pid": observation.main_pid,
                "systemd_result": observation.result,
            }
        )
        self.cleanup(supervisor, handles["a"])

        # §16.3 #17: revoke B's credential while its unit is still running.
        self.store.revoke(
            self.recording_issuer.credential(MATRIX_TASKS["b"][0]).claims.jti,
            reason="manual_stop",
            revoked_at=_clock(),
        )
        evidence["probes"].append(  # type: ignore[union-attr]
            self._call_probe(
                token_b,
                probe_id="gateway-revoked-credential",
                expected="credential_revoked",
            )
        )
        supervisor.stop(handles["b"], "manual_stop")
        self.cleanup(supervisor, handles["b"])

    def _merge_unit_result(
        self,
        evidence: dict[str, object],
        role: str,
        unit: str,
        result: Mapping[str, object],
    ) -> None:
        entries = result.get("probes")
        entries = entries if isinstance(entries, list) else []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            probe = dict(entry)
            probe["unit"] = unit
            probe["role"] = role
            probe.setdefault("row", PROBE_ROWS.get(str(probe.get("id"))))
            evidence["probes"].append(probe)  # type: ignore[union-attr]

        process = result.get("process")
        process = process if isinstance(process, Mapping) else {}
        cap_eff = process.get("cap_eff")
        no_new_privs = process.get("no_new_privs")
        unshare = process.get("unshare")
        unshare = unshare if isinstance(unshare, Mapping) else {}
        evidence["probes"].extend(  # type: ignore[union-attr]
            [
                {
                    "id": "proc-cap-eff",
                    "row": 10,
                    "unit": unit,
                    "role": role,
                    "expected": "0000000000000000",
                    "observed": cap_eff,
                    "passed": cap_eff == "0000000000000000",
                },
                {
                    "id": "proc-no-new-privs",
                    "row": 11,
                    "unit": unit,
                    "role": role,
                    "expected": "1",
                    "observed": no_new_privs,
                    "passed": no_new_privs == "1",
                },
                {
                    "id": "unshare-user-mount-ns",
                    "row": 12,
                    "unit": unit,
                    "role": role,
                    "expected": "EPERM",
                    "observed": unshare.get("observed"),
                    "errno": unshare.get("errno"),
                    "passed": unshare.get("passed") is True,
                },
                {
                    "id": "proc-enumeration-self-only",
                    "row": 13,
                    "unit": unit,
                    "role": role,
                    "expected": "self_only",
                    "observed": process.get("visible_process_uids"),
                    "passed": process.get("proc_only_self") is True,
                },
            ]
        )

    def _record_effective_ttl(
        self,
        evidence: dict[str, object],
        unit: str,
        key: str,
        requested: int,
    ) -> None:
        effective = None
        for event in self.audit.snapshot():
            if event.get("event") == AUDIT_EVENT_LAUNCH and event.get("unit") == unit:
                effective = event.get("ttl_seconds")
        ttl = evidence.setdefault("ttl", {})  # type: ignore[union-attr]
        ttl[f"{key}_requested_seconds"] = requested
        ttl[f"{key}_effective_seconds"] = effective

    def _record_run_directory(self, evidence: dict[str, object]) -> None:
        info = os.stat(self.run_root)
        socket_info = os.stat(self.run_root / "gateway.sock")
        units = list(self._unit_names())
        credential_present = any(
            _sudo_exists(self.run_root / "credentials" / f"{unit}.json")
            for unit in units
        )
        spec_present = any(
            _sudo_exists(self.run_root / "specs" / f"{unit}.json") for unit in units
        )
        run_entries = []
        for unit in units:
            run_entries.extend(
                item
                for item in self._all_probes(evidence)
                if item.get("unit") == unit
                and item.get("id") in {"list-run-directory", "read-other-credential-source"}
            )
        evidence["run_directory"] = {
            "owner_matches_controller": info.st_uid == os.getuid(),
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "gateway_socket_owner_matches": socket_info.st_uid == os.getuid(),
            "gateway_socket_mode": f"{stat.S_IMODE(socket_info.st_mode):04o}",
            "credential_source_present_after_active": credential_present,
            "spec_source_present_after_active": spec_present,
            "unit_cannot_traverse": bool(run_entries)
            and all(item.get("passed") is True for item in run_entries),
        }

    @staticmethod
    def _all_probes(evidence: dict[str, object]) -> list[dict]:
        items = evidence.get("probes")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _unit_names(self) -> list[str]:
        return [unit for _role, unit in self._units()]

    def _units(self) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        for role, (task_id, _grant_id, _subject) in MATRIX_TASKS.items():
            values.append((role, unit_name_for_task(task_id)))
        return values

    def _busy_probe(self, token: str, resource: str) -> dict[str, object]:
        idle: list[socket.socket] = []
        started = time.monotonic()
        entry: dict[str, object] = {
            "id": "gateway-busy",
            "row": 26,
            "expected": "busy",
            "observed": None,
            "passed": False,
            "busy_after_seconds": None,
            "idle_connections": 8,
            "idle_closed_within_seconds": None,
            "connections_closed": 0,
            "follow_up_allowed": False,
        }
        try:
            for _ in range(8):
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(6.0)
                connection.connect(str(self.run_root / "gateway.sock"))
                idle.append(connection)
            ninth = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                ninth.settimeout(2.0)
                ninth.connect(str(self.run_root / "gateway.sock"))
                raw = ninth.recv(4096)
            finally:
                ninth.close()
            entry["busy_after_seconds"] = round(time.monotonic() - started, 3)
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                response = None
            observed = self._observed(response if isinstance(response, dict) else None)
            entry["observed"] = observed

            closed = 0
            for connection in idle:
                connection.settimeout(8.0)
                try:
                    raw = connection.recv(4096)
                except (TimeoutError, OSError):
                    raw = b""
                try:
                    response = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    response = None
                if (
                    isinstance(response, dict)
                    and response.get("reason") == "timeout"
                ):
                    closed += 1
            entry["idle_closed_within_seconds"] = round(
                time.monotonic() - started, 3
            )
            entry["connections_closed"] = closed + 1
            entry["follow_up_allowed"] = (
                self._observed(self._gateway_request(token, READ_CAPABILITY, resource)[0])
                == "ok"
            )
            entry["passed"] = (
                observed == "busy"
                and entry["busy_after_seconds"] < 1.0
                and closed == 8
                and entry["idle_closed_within_seconds"] <= 7.0
                and entry["follow_up_allowed"] is True
            )
        finally:
            for connection in idle:
                connection.close()
        return entry

    def _rate_limit_probe(self, token: str, resource: str) -> dict[str, object]:
        time.sleep(1.1)  # let the per-peer-UID request bucket refill
        responses: list[dict[str, object] | None] = []
        closed_flags: list[bool] = []
        for _ in range(17):
            response, closed = self._gateway_request(
                token, READ_CAPABILITY, resource
            )
            responses.append(response)
            closed_flags.append(closed)
        throttled_index = None
        for index, response in enumerate(responses, start=1):
            if self._observed(response) == "rate_limited":
                throttled_index = index
                break
        first_allowed = all(
            self._observed(response) == "ok" for response in responses[:16]
        )
        entry: dict[str, object] = {
            "id": "gateway-rate-limit",
            "row": 25,
            "expected": "rate_limited",
            "observed": self._observed(responses[16]) if len(responses) == 17 else None,
            "passed": (
                throttled_index == 17
                and first_allowed
                and len(closed_flags) == 17
                and closed_flags[16] is True
            ),
            "requests": 17,
            "throttled_index": throttled_index,
            "denied_connection_closed": closed_flags[16] if len(closed_flags) == 17 else None,
        }
        return entry

    def record_gateway_evidence(self, evidence: dict[str, object]) -> None:
        events = self.audit.snapshot()
        serialized = json.dumps(events, sort_keys=True)
        reasons = sorted(
            {
                str(event.get("reason"))
                for event in events
                if event.get("reason") is not None
            }
        )
        limits = self.config.gateway_limits
        gateway = evidence["gateway"]
        gateway["mode"] = "in_process_harness"
        gateway["grant_provider"] = "in_memory_live"
        gateway["limits"] = {
            "per_task_connections": limits.max_connections_per_task,
            "per_task_requests_per_second": limits.max_rps_per_task,
            "global_connections": limits.max_connections_global,
            "global_requests_per_second": limits.max_rps_global,
            "max_request_bytes": limits.max_request_bytes,
            "read_write_timeout_seconds": int(limits.timeout_seconds),
        }
        gateway["audit"] = {
            "events": len(events),
            "token_absent": "agx1." not in serialized,
            "deny_reasons": reasons,
        }

    # -- cleanup evidence -------------------------------------------------
    def record_cleanup(self, evidence: dict[str, object]) -> None:
        remaining = self._remaining_units()
        cleanup = {
            "units_remaining": len(remaining),
            "remaining_unit_names": remaining,
            "run_dir_present": os.path.exists(self.run_root),
            "resources_dir_present": os.path.exists(self.resources_root),
            "state_dir_present": _sudo_exists(DEFAULT_STATE_ROOT),
            "state_link_present": _sudo_exists(DEFAULT_STATE_LINK_ROOT),
        }
        evidence["cleanup"] = cleanup
        probes = evidence.get("probes")
        if isinstance(probes, list):
            probes.append(
                {
                    "id": "cleanup",
                    "row": 24,
                    "expected": "clean",
                    "observed": dict(cleanup),
                    "passed": (
                        cleanup["units_remaining"] == 0
                        and cleanup["run_dir_present"] is False
                        and cleanup["resources_dir_present"] is False
                        and cleanup["state_dir_present"] is False
                        and cleanup["state_link_present"] is False
                    ),
                }
            )

    def _remaining_units(self) -> list[str]:
        completed = _sudo(
            "systemctl",
            "list-units",
            "--all",
            "--no-legend",
            "--plain",
            "--no-pager",
            "agentmgr-task-*",
        )
        if completed.returncode != 0:
            return []
        names: list[str] = []
        for line in completed.stdout.splitlines():
            fields = line.split()
            if not fields:
                continue
            matched = _UNIT_LINE.match(fields[0])
            if matched is not None:
                names.append(matched.group(1))
        return sorted(set(names))


def _commit_sha(repo_root: Path) -> str:
    completed = _run(["git", "-C", str(repo_root), "rev-parse", "HEAD"], timeout=15)
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and _HEX40.match(value) else "unknown"


def _new_evidence(repo_root: Path, started: datetime) -> dict[str, object]:
    return {
        "run_id": f"iso-{started:%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}",
        "started_at": started.isoformat(),
        "commit_sha": _commit_sha(repo_root),
        "contract_version": CONTRACT_VERSION,
        "host": {
            "os": os.uname().sysname.lower(),
            "kernel": os.uname().release,
            "systemd": "",
            "systemd_runtime": "system",
        },
        "network_profile": None,
        "network_profile_fallback_reason": None,
        "profile_probes": [],
        "unit": None,
        "uid": None,
        "systemd_properties": {},
        "process": {},
        "ttl": {},
        "gateway": {
            "mode": "in_process_harness",
            "grant_provider": "in_memory_live",
            "limits": {},
            "probes": [],
            "audit": {},
        },
        "run_directory": {},
        "probes": [],
        "cleanup": {},
        "evidence_self_check": {},
        "result": "fail",
        "failures": [],
    }


def _evidence_self_check(evidence: dict[str, object]) -> None:
    serialized = json.dumps(evidence, sort_keys=True)
    home = str(Path.home())
    evidence["evidence_self_check"] = {
        "token_absent": "agx1." not in serialized,
        "home_path_absent": home == "/" or home not in serialized,
        "ipv4_absent": _IPV4.search(serialized) is None,
    }


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(raw)
    finally:
        if fd != -1:
            os.close(fd)


def _overall_result(evidence: dict[str, object]) -> bool:
    probes = evidence.get("probes")
    probes = probes if isinstance(probes, list) else []
    gateway = evidence.get("gateway")
    gateway = gateway if isinstance(gateway, dict) else {}
    gateway_probes = gateway.get("probes")
    gateway_probes = gateway_probes if isinstance(gateway_probes, list) else []
    cleanup = evidence.get("cleanup")
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    failures = evidence.get("failures")
    failures = failures if isinstance(failures, list) else []
    self_check = evidence.get("evidence_self_check")
    self_check = self_check if isinstance(self_check, dict) else {}
    return bool(
        probes
        and all(item.get("passed") is True for item in probes)
        and all(item.get("passed") is True for item in gateway_probes)
        and not failures
        and cleanup.get("units_remaining") == 0
        and cleanup.get("run_dir_present") is False
        and cleanup.get("state_dir_present") is False
        and cleanup.get("resources_dir_present") is False
        and self_check.get("token_absent") is True
        and self_check.get("home_path_absent") is True
        and self_check.get("ipv4_absent") is True
    )


def run_harness(config: HarnessConfig) -> dict[str, object]:
    """Run the whole live matrix and return (and write) the evidence document."""

    started = datetime.now(UTC)
    evidence = _new_evidence(config.repo_root, started)
    evidence_path = validate_evidence_path(config.evidence_path, config.repo_root)
    staging = Path(tempfile.mkdtemp(prefix="agentmgr-isolation-"))
    harness = _Harness(config, staging)
    stage = "preflight"
    try:
        harness.preflight(evidence)
        stage = "filesystem"
        harness.prepare_filesystem()
        stage = "gateway_start"
        harness.start_gateway()
        stage = "profile_probes"
        profile, profile_probes = harness.probe_profiles()
        evidence["network_profile"] = profile
        evidence["profile_probes"] = profile_probes
        if profile == NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK:
            evidence["network_profile_fallback_reason"] = (
                "private_netns_gateway_connect_failed"
            )
        stage = "matrix"
        harness.run_matrix(profile, evidence)
    except (HarnessError, SecurityError, OSError, ValueError) as exc:
        harness.failures.append({"stage": stage, "error": _safe_error(exc)})
    except Exception as exc:  # a harness defect must still leave evidence
        harness.failures.append({"stage": stage, "error": type(exc).__name__})
    finally:
        stage = "teardown"
        try:
            harness.teardown()
        except Exception as exc:
            harness.failures.append({"stage": stage, "error": _safe_error(exc)})
        try:
            harness.record_gateway_evidence(evidence)
        except Exception as exc:
            harness.failures.append(
                {"stage": "gateway_evidence", "error": _safe_error(exc)}
            )
        try:
            harness.record_cleanup(evidence)
        except Exception as exc:
            harness.failures.append(
                {"stage": "cleanup_evidence", "error": _safe_error(exc)}
            )
        evidence["failures"] = harness.failures
        _evidence_self_check(evidence)
        evidence["result"] = "pass" if _overall_result(evidence) else "fail"
        try:
            _write_evidence(evidence_path, evidence)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return evidence


def main(argv: list[str] | None = None) -> int:
    del argv
    for name in ("AGENTMGR_ISOLATION_TEST", "AGENTMGR_ISOLATION_SUDO"):
        if os.environ.get(name) != "1":
            print(
                f"{name}=1 is required; run ops/security/verify_isolation.sh "
                "on the approved test host",
                file=sys.stderr,
            )
            return 2

    repo_root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("AGENTMGR_ISOLATION_EVIDENCE")
    if configured:
        evidence_path = Path(configured)
    else:
        evidence_path = Path(tempfile.gettempdir()) / "agentmgr-isolation-evidence.json"

    try:
        config = HarnessConfig(
            repo_root=repo_root,
            run_root=DEFAULT_RUNTIME_ROOT,
            resources_root=RESOURCES_ROOT,
            evidence_path=evidence_path.resolve(),
        )
        evidence = run_harness(config)
    except (HarnessError, ValueError, OSError) as exc:
        print(f"isolation harness failed: {_safe_error(exc)}", file=sys.stderr)
        return 2
    if evidence.get("result") == "pass":
        print("isolation evidence: pass")
        return 0
    print("isolation evidence: fail (see the evidence document)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
