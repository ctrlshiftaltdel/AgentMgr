#!/usr/bin/env python3
"""Root-owned narrow helper for managed execution (Issue #82).

Installed by an operator as ``/usr/local/libexec/agentmgr_exec.py`` and reached
only through ``/usr/local/libexec/agentmgr-exec`` (see ``README.md`` and
``agentmgr-exec.sudoers``).  The controller user invokes it with ``sudo -n``
and writes exactly one JSON request to stdin; **argv is never accepted**.

Security properties
-------------------

* The unit name must match ``^agentmgr-task-[0-9a-f]{12}$``; the unit is
  re-derived from the validated task ID and cross-checked.
* The launch spec is an exact-key whitelist.  ``ExecStart``, ``User``,
  ``Environment``, ``BindPaths`` and every other systemd property are
  impossible to express.
* The bearer token arrives only on stdin.  It is written to a root-only
  ``0600`` file with ``O_CREAT|O_EXCL|O_NOFOLLOW``; it never reaches argv,
  the environment or output.
* Every child command runs from a fixed absolute path, as an argv array
  (``shell=False``), with a fixed minimal environment (``env -i`` equivalent).
* Cleanup only touches paths derived from the validated unit name under the
  documented runtime/state roots; symlinked or wrongly-owned paths fail closed.

This module is deliberately standard-library only and must not import
``app.security`` at runtime: it is installed outside the project environment.
The duplicated constants are pinned to the product builders by
``app/tests/security/test_helper.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

__all__ = [
    "BASE_INACCESSIBLE_PATHS",
    "CLEAN_ENV",
    "GATEWAY_SOCKET_PATH",
    "HelperError",
    "MAX_STDIN_BYTES",
    "RUNTIME_ROOT",
    "SHOW_PROPERTIES",
    "STATE_LINK_ROOT",
    "STATE_ROOT",
    "SYSTEMCTL",
    "SYSTEMD_RUN",
    "TEMPORARY_FILESYSTEM_PATHS",
    "WORKLOAD_PATH",
    "build_launch_argv",
    "cleanup",
    "inspect",
    "kill",
    "launch_from_stdin",
    "main",
    "remove_sources",
    "reset_failed",
    "run_checked",
    "run_request",
    "stop",
    "unit_for_task",
    "validate_spec_payload",
    "validate_unit",
]

#: Fixed deployment layout (design §7.3/§8.3).  Overridable only for tests.
RUNTIME_ROOT = Path("/run/agentmgr-exec")
STATE_ROOT = Path("/var/lib/private/agentmgr-exec")
STATE_LINK_ROOT = Path("/var/lib/agentmgr-exec")
WORKLOAD_PATH = RUNTIME_ROOT / "workload.py"
GATEWAY_SOCKET_PATH = RUNTIME_ROOT / "gateway.sock"


def _credentials_dir() -> Path:
    return RUNTIME_ROOT / "credentials"


def _specs_dir() -> Path:
    return RUNTIME_ROOT / "specs"

#: Absolute executables: the helper must never consult the caller's PATH.
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
PYTHON_EXECUTABLE = "/usr/bin/python3"
WORKLOAD_UNIT_PATH = "/workload.py"
GATEWAY_UNIT_PATH = "/run/agentmgr/gateway.sock"
DESCRIPTION = "agentmgr-exec"
CAPABILITY_NAME = "task-credential"

#: ``env -i``-like environment for every child command.
CLEAN_ENV: Mapping[str, str] = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
}

RUN_TIMEOUT_SECONDS = 15
MAX_STDIN_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096

_UNIT_PREFIX = "agentmgr-task-"
_UNIT_DOMAIN = "agentmgr-task:v1:"
_UNIT_PATTERN = re.compile(r"^agentmgr-task-[0-9a-f]{12}$")
_TASK_ID_PATTERN = re.compile(r"^task_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_GRANT_ID_PATTERN = re.compile(r"^grnt_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*@\d+\.\d+\.\d+$")
_TOKEN_PATTERN = re.compile(r"^agx1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

SHOW_PROPERTIES = (
    "ActiveState,SubState,MainPID,Result,ExecMainStatus,LoadState,"
    "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp"
)

#: Inaccessible control-plane/data paths (kept byte-identical to
#: ``app.security.systemd._BASE_INACCESSIBLE_PATHS``).
#:
#: ``/run/systemd`` and ``/run/dbus`` are deliberately absent: a whole-dir
#: ``InaccessiblePaths`` over ``/run/systemd`` fails namespace setup on the
#: approved host (226/NAMESPACE).  They are masked with ``TemporaryFileSystem``
#: below in both profiles.
BASE_INACCESSIBLE_PATHS: tuple[str, ...] = (
    "-/etc/agentmgr",
    "-/var/lib/agentmgr",
    "-/var/lib/postgresql",
    "-/srv",
    "-/opt",
)

#: Private tmpfs mounts that mask the host systemd/D-Bus socket directories in
#: both profiles (kept byte-identical to the product builder).
TEMPORARY_FILESYSTEM_PATHS: tuple[str, ...] = ("/run/systemd", "/run/dbus")

_SPEC_FIELDS = frozenset(
    {
        "task_id",
        "grant_id",
        "execution_mode",
        "ttl_seconds",
        "cpu_quota_percent",
        "memory_max_mb",
        "tasks_max",
        "capabilities",
        "resource_scope",
        "backend",
    }
)
_SCOPE_FIELDS = frozenset(
    {
        "scope_type",
        "resource_ids",
        "connector_ids",
        "data_classifications",
        "exclude_resource_ids",
    }
)
#: Explicit denies; the exact-key check already rejects them.  Naming them
#: keeps the intent reviewable and gives a distinct fail-closed error.
_FORBIDDEN_FIELDS = frozenset(
    {
        "BindPaths",
        "BindReadOnlyPaths",
        "DynamicUser",
        "Environment",
        "EnvironmentFile",
        "ExecStart",
        "ExecStartPre",
        "Group",
        "KillMode",
        "LoadCredential",
        "PrivateNetwork",
        "ProtectHome",
        "ProtectSystem",
        "RuntimeMaxSec",
        "StateDirectory",
        "User",
    }
)

_MIN_TTL_SECONDS = 1
_MAX_TTL_SECONDS = 300
_MIN_CPU_QUOTA_PERCENT = 1
_MAX_CPU_QUOTA_PERCENT = 100
_MIN_MEMORY_MAX_MB = 32
_MAX_MEMORY_MAX_MB = 1024
_MIN_TASKS_MAX = 1
_MAX_TASKS_MAX = 64
_MAX_CAPABILITIES = 16

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class HelperError(Exception):
    """A fail-closed rejection with a stable, path-free code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code:
            code = "internal_error"
        self.code = code
        super().__init__(code)


def validate_unit(unit: object) -> None:
    """Reject anything that is not an exact derived task unit name."""

    if not isinstance(unit, str) or _UNIT_PATTERN.fullmatch(unit) is None:
        raise HelperError("invalid_unit")


def unit_for_task(task_id: object) -> str:
    """Return the fixed unit name for ``task_id`` (product-compatible)."""

    if not isinstance(task_id, str) or _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise HelperError("invalid_spec")
    digest = hashlib.sha256((_UNIT_DOMAIN + task_id).encode("utf-8")).hexdigest()
    unit = _UNIT_PREFIX + digest[:12]
    validate_unit(unit)
    return unit


def _safe_text(value: object, pattern: re.Pattern[str] | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or _CONTROL_CHARS.search(value)
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise HelperError("invalid_spec")
    return value


def _exact_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise HelperError("invalid_spec")
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise HelperError("invalid_spec")
    return [_safe_text(item) for item in value]


def _exact_keys(value: Mapping[str, object], expected: frozenset[str]) -> None:
    keys = set(value)
    if keys & _FORBIDDEN_FIELDS:
        raise HelperError("forbidden_field")
    if keys - expected:
        raise HelperError("unknown_field")
    if expected - keys:
        raise HelperError("missing_field")


def validate_spec_payload(payload: object) -> None:
    """Validate one launch spec against the exact §7.1 field set.

    This is a structural/range gate only; the authoritative Grant check stays
    in the controller.  It runs before any root-owned file is created.
    """

    if not isinstance(payload, Mapping):
        raise HelperError("invalid_spec")
    _exact_keys(payload, _SPEC_FIELDS)

    _safe_text(payload["task_id"], _TASK_ID_PATTERN)
    _safe_text(payload["grant_id"], _GRANT_ID_PATTERN)
    if payload["execution_mode"] != "managed_execution":
        raise HelperError("invalid_spec")
    _exact_int(
        payload["ttl_seconds"],
        minimum=_MIN_TTL_SECONDS,
        maximum=_MAX_TTL_SECONDS,
    )
    _exact_int(
        payload["cpu_quota_percent"],
        minimum=_MIN_CPU_QUOTA_PERCENT,
        maximum=_MAX_CPU_QUOTA_PERCENT,
    )
    _exact_int(
        payload["memory_max_mb"],
        minimum=_MIN_MEMORY_MAX_MB,
        maximum=_MAX_MEMORY_MAX_MB,
    )
    _exact_int(payload["tasks_max"], minimum=_MIN_TASKS_MAX, maximum=_MAX_TASKS_MAX)

    capabilities = _string_list(payload["capabilities"])
    if (
        not capabilities
        or len(capabilities) > _MAX_CAPABILITIES
        or len(set(capabilities)) != len(capabilities)
    ):
        raise HelperError("invalid_spec")
    for capability in capabilities:
        _safe_text(capability, _CAPABILITY_PATTERN)

    scope = payload["resource_scope"]
    if not isinstance(scope, Mapping):
        raise HelperError("invalid_spec")
    _exact_keys(scope, _SCOPE_FIELDS)
    if scope["scope_type"] != "self":
        raise HelperError("invalid_spec")
    for field in (
        "resource_ids",
        "connector_ids",
        "data_classifications",
        "exclude_resource_ids",
    ):
        values = _string_list(scope[field])
        if len(set(values)) != len(values):
            raise HelperError("invalid_spec")
    if "connector_ids" in scope and scope["connector_ids"]:
        raise HelperError("invalid_spec")
    if not set(scope["data_classifications"]) <= {"internal"}:
        raise HelperError("invalid_spec")

    if payload["backend"] != "file":
        raise HelperError("invalid_spec")


def _validate_credential(credential: object) -> str:
    if (
        not isinstance(credential, str)
        or not credential
        or len(credential) > MAX_TOKEN_BYTES
        or _TOKEN_PATTERN.fullmatch(credential) is None
    ):
        raise HelperError("invalid_credential")
    return credential


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise HelperError("path_unavailable") from None


def _require_root_dir(path: Path, *, code: str) -> os.stat_result:
    """Require an existing directory that is not a symlink.

    Ownership is deliberately not pinned to the helper's uid here: the
    runtime root is owned by the controller user (design §7.3), and root may
    create the private subdirectories inside it.
    """

    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise HelperError(code)
    return info


def _require_private_dir(
    path: Path, *, create: bool, mode: int | None = None
) -> None:
    """Require a helper-owned ``0700`` directory; optionally create it."""

    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise HelperError("directory_unavailable") from None
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise HelperError("directory_invalid")
    if info.st_uid != os.geteuid():
        raise HelperError("directory_owner_invalid")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise HelperError("directory_mode_invalid")


def _check_workload_and_socket() -> None:
    workload = _lstat(WORKLOAD_PATH)
    if workload is None or not stat.S_ISREG(workload.st_mode):
        raise HelperError("workload_invalid")
    if workload.st_uid != os.geteuid():
        raise HelperError("workload_owner_invalid")
    gateway = _lstat(GATEWAY_SOCKET_PATH)
    if gateway is None or not stat.S_ISSOCK(gateway.st_mode):
        raise HelperError("gateway_socket_invalid")


def _write_private_file(path: Path, text: str) -> None:
    """Create ``path`` exactly once, ``0600``, without following symlinks."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise HelperError("source_exists") from None
    except OSError:
        raise HelperError("source_write_failed") from None

    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            raise HelperError("source_mode_failed") from None
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise HelperError("source_mode_invalid")
        data = text.encode("utf-8")
        while data:
            try:
                written = os.write(descriptor, data)
            except OSError:
                raise HelperError("source_write_failed") from None
            if written <= 0:
                raise HelperError("source_write_failed")
            data = data[written:]
        try:
            os.fsync(descriptor)
        except OSError:
            raise HelperError("source_write_failed") from None
    finally:
        os.close(descriptor)


def _discard(path: Path) -> None:
    """Remove a file this helper just created; never follow a symlink."""

    try:
        info = os.lstat(path)
    except OSError:
        return
    if stat.S_ISREG(info.st_mode):
        try:
            os.unlink(path)
        except OSError:
            pass


def build_launch_argv(unit: str, spec: object) -> list[str]:
    """Return the fixed ``systemd-run`` argv for one validated spec."""

    validate_unit(unit)
    validate_spec_payload(spec)
    assert isinstance(spec, Mapping)  # validated above
    ttl = _exact_int(
        spec["ttl_seconds"], minimum=_MIN_TTL_SECONDS, maximum=_MAX_TTL_SECONDS
    )
    cpu = _exact_int(
        spec["cpu_quota_percent"],
        minimum=_MIN_CPU_QUOTA_PERCENT,
        maximum=_MAX_CPU_QUOTA_PERCENT,
    )
    memory = _exact_int(
        spec["memory_max_mb"],
        minimum=_MIN_MEMORY_MAX_MB,
        maximum=_MAX_MEMORY_MAX_MB,
    )
    tasks = _exact_int(
        spec["tasks_max"], minimum=_MIN_TASKS_MAX, maximum=_MAX_TASKS_MAX
    )
    if unit_for_task(spec["task_id"]) != unit:
        raise HelperError("unit_name_mismatch")

    credential = _credentials_dir() / f"{unit}.json"
    # `systemd-run -p` does not expand `%N`/`%i`; use the validated unit name so
    # the state directory is `agentmgr-exec/<unit>`, not a literal `%N` path.
    properties = [
        "--property=DynamicUser=yes",
        f"--property=StateDirectory=agentmgr-exec/{unit}",
        "--property=StateDirectoryMode=0700",
        f"--property=RuntimeMaxSec={ttl}s",
        "--property=TimeoutStopSec=5",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        *(
            f"--property=InaccessiblePaths={path}"
            for path in BASE_INACCESSIBLE_PATHS
        ),
        *(
            f"--property=TemporaryFileSystem={path}"
            for path in TEMPORARY_FILESYSTEM_PATHS
        ),
        "--property=PrivateTmp=yes",
        "--property=PrivateDevices=yes",
        "--property=PrivateNetwork=yes",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=IPAddressDeny=any",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=",
        "--property=AmbientCapabilities=",
        "--property=SystemCallFilter=@system-service",
        "--property=SystemCallArchitectures=native",
        "--property=SystemCallErrorNumber=EPERM",
        "--property=RestrictNamespaces=yes",
        "--property=LockPersonality=yes",
        "--property=MemoryDenyWriteExecute=yes",
        "--property=RestrictRealtime=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=RemoveIPC=yes",
        "--property=ProtectClock=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectHostname=yes",
        "--property=ProtectProc=invisible",
        "--property=ProcSubset=pid",
        "--property=UMask=0077",
        f"--property=MemoryMax={memory}M",
        f"--property=CPUQuota={cpu}%",
        f"--property=TasksMax={tasks}",
        "--property=LimitNOFILE=256",
        f"--property=LoadCredential={CAPABILITY_NAME}:{credential}",
        f"--property=BindReadOnlyPaths={GATEWAY_SOCKET_PATH}:{GATEWAY_UNIT_PATH}",
        f"--property=BindReadOnlyPaths={WORKLOAD_PATH}:{WORKLOAD_UNIT_PATH}",
    ]
    return [
        SYSTEMD_RUN,
        f"--unit={unit}",
        f"--description={DESCRIPTION}",
        *properties,
        PYTHON_EXECUTABLE,
        WORKLOAD_UNIT_PATH,
    ]


def run_checked(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one fixed argv with a clean environment and no shell."""

    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
    ):
        raise ValueError("argv must be a non-empty list of strings")
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=True,
        shell=False,
        env=dict(CLEAN_ENV),
    )


def _run(argv: list[str], *, runner: Runner) -> subprocess.CompletedProcess[str]:
    try:
        completed = runner(argv)
    except subprocess.TimeoutExpired:
        raise HelperError("command_timeout") from None
    except subprocess.CalledProcessError:
        raise HelperError("command_failed") from None
    except OSError:
        raise HelperError("command_unavailable") from None
    if (
        not isinstance(completed, subprocess.CompletedProcess)
        or completed.returncode != 0
    ):
        raise HelperError("command_failed")
    return completed


def _launch(
    spec: object, credential: object, *, runner: Runner
) -> dict[str, object]:
    validate_spec_payload(spec)
    assert isinstance(spec, Mapping)
    token = _validate_credential(credential)
    unit = unit_for_task(spec["task_id"])

    _require_root_dir(RUNTIME_ROOT, code="runtime_root_invalid")
    _require_private_dir(_specs_dir(), create=True, mode=0o700)
    _require_private_dir(_credentials_dir(), create=True, mode=0o700)
    _check_workload_and_socket()

    argv = build_launch_argv(unit, spec)
    spec_path = _specs_dir() / f"{unit}.json"
    credential_path = _credentials_dir() / f"{unit}.json"
    spec_text = json.dumps(
        dict(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )

    written: list[Path] = []
    try:
        _write_private_file(spec_path, spec_text)
        written.append(spec_path)
        _write_private_file(credential_path, token)
        written.append(credential_path)
        _run(argv, runner=runner)
    except BaseException:
        # Never leave a root-only source behind on a failed launch.  The
        # supervisor repeats this cleanup, which is idempotent.
        for path in reversed(written):
            _discard(path)
        raise
    return {"ok": True, "unit": unit}


def launch_from_stdin(stdin_text: str, *, runner: Runner = run_checked) -> dict:
    """Parse one launch request from stdin and start the fixed unit."""

    if not isinstance(stdin_text, str):
        raise HelperError("invalid_request")
    if len(stdin_text) > MAX_STDIN_BYTES:
        raise HelperError("payload_too_large")
    try:
        payload = json.loads(stdin_text)
    except (TypeError, ValueError):
        raise HelperError("invalid_request") from None
    if not isinstance(payload, Mapping):
        raise HelperError("invalid_request")
    if set(payload) != {"command", "spec", "credential"}:
        raise HelperError("invalid_request")
    if payload["command"] != "launch":
        raise HelperError("unknown_command")
    return _launch(payload["spec"], payload["credential"], runner=runner)


def stop(unit: object, *, runner: Runner = run_checked) -> None:
    validate_unit(unit)
    assert isinstance(unit, str)
    _run([SYSTEMCTL, "stop", unit], runner=runner)


def kill(unit: object, *, runner: Runner = run_checked) -> None:
    validate_unit(unit)
    assert isinstance(unit, str)
    _run(
        [SYSTEMCTL, "kill", "--signal=SIGKILL", "--kill-whom=all", unit],
        runner=runner,
    )


def reset_failed(unit: object, *, runner: Runner = run_checked) -> None:
    validate_unit(unit)
    assert isinstance(unit, str)
    _run([SYSTEMCTL, "reset-failed", unit], runner=runner)


def _parse_show(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            values[key] = value
    return values


def inspect(unit: object, *, runner: Runner = run_checked) -> dict[str, str]:
    validate_unit(unit)
    assert isinstance(unit, str)
    completed = _run(
        [SYSTEMCTL, "show", "-p", SHOW_PROPERTIES, unit], runner=runner
    )
    return _parse_show(completed.stdout or "")


def _remove_source_file(path: Path) -> None:
    info = _lstat(path)
    if info is None:
        return
    if not stat.S_ISREG(info.st_mode):
        raise HelperError("source_path_invalid")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise HelperError("source_mode_invalid")
    try:
        os.unlink(path)
    except OSError:
        raise HelperError("source_remove_failed") from None


def _remove_sources(unit: str) -> None:
    for directory, filename in (
        (_credentials_dir(), f"{unit}.json"),
        (_specs_dir(), f"{unit}.json"),
    ):
        info = _lstat(directory)
        if info is None:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise HelperError("directory_invalid")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise HelperError("directory_mode_invalid")
        _remove_source_file(directory / filename)


def remove_sources(unit: object) -> None:
    """Delete only ``credentials/<unit>.json`` and ``specs/<unit>.json``."""

    validate_unit(unit)
    assert isinstance(unit, str)
    _remove_sources(unit)


def _remove_state(unit: str) -> None:
    state_root = _lstat(STATE_ROOT)
    if state_root is not None:
        if not stat.S_ISDIR(state_root.st_mode) or state_root.st_uid != os.geteuid():
            raise HelperError("state_root_invalid")
        state_dir = STATE_ROOT / unit
        info = _lstat(state_dir)
        if info is not None:
            if not stat.S_ISDIR(info.st_mode):
                raise HelperError("state_path_invalid")
            try:
                shutil.rmtree(state_dir)
            except OSError:
                raise HelperError("state_remove_failed") from None

    link_root = _lstat(STATE_LINK_ROOT)
    if link_root is not None:
        if not stat.S_ISDIR(link_root.st_mode) or link_root.st_uid != os.geteuid():
            raise HelperError("state_link_root_invalid")
        link = STATE_LINK_ROOT / unit
        info = _lstat(link)
        if info is not None:
            if not stat.S_ISLNK(info.st_mode):
                raise HelperError("state_link_invalid")
            try:
                os.unlink(link)
            except OSError:
                raise HelperError("state_link_remove_failed") from None


def cleanup(unit: object, *, runner: Runner = run_checked) -> None:
    """Reset failed state, then remove only the unit's derived residue."""

    validate_unit(unit)
    assert isinstance(unit, str)
    reset_failed(unit, runner=runner)
    _remove_sources(unit)
    _remove_state(unit)


_UNIT_COMMANDS = {
    "cleanup": cleanup,
    "inspect": inspect,
    "kill": kill,
    "remove-sources": remove_sources,
    "reset-failed": reset_failed,
    "stop": stop,
}


def run_request(
    payload: object, *, runner: Runner = run_checked
) -> dict[str, object]:
    """Dispatch one decoded request object (the only accepted entry shape)."""

    if not isinstance(payload, Mapping):
        raise HelperError("invalid_request")
    command = payload.get("command")
    if not isinstance(command, str):
        raise HelperError("invalid_request")
    if command == "launch":
        if set(payload) != {"command", "spec", "credential"}:
            raise HelperError("invalid_request")
        return _launch(payload["spec"], payload["credential"], runner=runner)

    operation = _UNIT_COMMANDS.get(command)
    if operation is None:
        raise HelperError("unknown_command")
    if set(payload) != {"command", "unit"}:
        raise HelperError("invalid_request")
    unit = payload["unit"]
    validate_unit(unit)
    if command == "inspect":
        return {"show": inspect(unit, runner=runner)}
    if command == "remove-sources":
        remove_sources(unit)
    else:
        operation(unit, runner=runner)
    return {"ok": True, "unit": unit}


def _emit_error(code: str) -> int:
    sys.stderr.write(json.dumps({"error": code}, separators=(",", ":")) + "\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    """Read one JSON request from stdin; reject argv and emit safe JSON."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        return _emit_error("arguments_not_accepted")
    try:
        text = sys.stdin.read(MAX_STDIN_BYTES + 1)
        if len(text) > MAX_STDIN_BYTES:
            raise HelperError("payload_too_large")
        response = run_request(json.loads(text))
    except HelperError as error:
        return _emit_error(error.code)
    except (TypeError, ValueError):
        return _emit_error("invalid_request")
    except OSError:
        return _emit_error("io_error")
    except Exception:
        # Never leak a traceback: it can contain paths and request content.
        return _emit_error("internal_error")
    sys.stdout.write(
        json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
