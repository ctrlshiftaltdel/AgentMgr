#!/usr/bin/env python3
"""Root-owned narrow helper for the Hermes managed sandbox (Issue #97).

Installed by an operator as ``/usr/local/libexec/agentmgr_hermes_exec.py`` and
reached only through ``/usr/local/libexec/agentmgr-hermes-exec`` (see
``README.md`` and ``agentmgr-hermes.sudoers``).  The trusted adapter user runs
the wrapper through ``sudo -n`` and writes exactly one JSON request to stdin;
**argv is never accepted** and every path, user and systemd property is derived
from the root-only install config below.

Protocol
--------

One request object with exactly these keys::

    operation, task_id, grant_id, unit, runtime_handle, idempotency_key,
    prompt_sha256, prompt_bytes, profile_template_sha256, egress_token_sha256,
    ttl_seconds, runtime_max_sec, memory_max_mb, cpu_quota_percent, tasks_max

``operation`` is one of ``prepare``, ``launch``, ``status``, ``stop``,
``cleanup``, ``attest``.  ``cleanup`` accepts exactly one extra key,
``result_read`` (``true``), because a task directory is only deleted after the
adapter declares the artifact was read.  No other extra key is accepted, so a
caller can never express ``ExecStart``, ``Environment``, ``BindPaths``,
``User``, ``Group`` or ``PrivateNetwork``.

Responses are one compact JSON object on stdout.  Failures are one compact
``{"error": "<stable-code>"}`` object on stderr with exit status 1; the code is
a fixed token and never contains a path, digest or payload.

Deployment invariants
---------------------

* ``/etc/agentmgr/hermes-exec.json`` (root-owned, ``0600``/``0640``) fixes the
  task root, runtime root and trusted runtime root hash, broker socket,
  workload path, adapter user and the adapter's **private** group.  The sandbox
  group is the fixed ``agentmgr-hermes`` and is never taken from the config.
  The adapter user must be a member of ``agentmgr-hermes`` so it can read the
  task directories (verified via NSS when no numeric ids are configured).
* The attestation HMAC key is root-owned and must live under ``/etc/agentmgr``
  (masked in every unit) and never under the task root.  Accepted modes are
  ``0600``/``0400`` (owner-only) and ``0440`` whose group is exactly the
  adapter's private group; ``0440`` with the sandbox group, any other group, or
  any other mode (e.g. ``0640``) is rejected.  The helper signs with the key and
  the trusted adapter verifies with the same key; a sandbox unit never sees it.
* The task directory is ``<task-root>/<unit>`` with ``out/`` at ``0770``,
  owned by the trusted adapter user and the fixed sandbox group
  ``agentmgr-hermes``.  Existing paths are never repaired: a symlink, wrong
  owner/group or wrong mode fails closed.
  ``prompt.txt``/``profile.template.yaml`` are accepted exactly at ``0440`` or
  ``0640`` (group-read is required by the unit) and ``egress.token`` exactly at
  ``0640``.
* ``systemd-run`` receives a fixed transient unit: private ``/run`` and
  ``/tmp``, isolated loopback only, read-only host minus the task directory, an
  empty capability set, ``@system-service`` and no caller supplied property.
  ``Group=agentmgr-hermes`` is the unit's static primary group (not
  ``SupplementaryGroups``), so unit-written status/artifact files inherit the
  group and the adapter can read them without setgid.  ``PrivateDevices=yes``
  and ``InaccessiblePaths=-<task-root>`` hide sibling task directories/tokens
  while the task directory itself stays available through the single ``/task``
  bind.  ``/run`` is replaced with a private tmpfs and ``/tmp`` is private, so
  the bind sources (task root, runtime root, broker socket, workload) must live
  on a persistent host path: the install config rejects the masked prefixes and
  ``/tmp``/``/var/tmp``.
* ``launch`` verifies the prompt/profile-template/egress-token file sizes and
  SHA-256 digests before starting, then waits for ``ActiveState=active`` and
  the workload's ``status/started.json`` marker, and binds the unit's real UID
  read from ``/proc/<MainPID>/status``.
* ``status`` reads the task directory and its ``status/`` child through
  ``O_DIRECTORY|O_NOFOLLOW`` directory fds, and markers with
  ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK`` plus a regular-file ``fstat`` before any
  read, so a symlinked parent or a unit-created FIFO/socket/device cannot block
  or redirect the helper; a ``not-found`` unit reports ``result``/``exit_status``
  as ``None`` instead of systemd's default ``success``/``0``.
* ``attest`` inspects the unit first: a stopped/never-started/cleaned-up task
  yields a runtime attestation with ``task_attestation=None``; strict task-file
  verification and a signed task attestation only happen while the unit is
  active with a readable UID.
* ``stop_confirmed`` is only ever true when systemd and the cgroup hold no
  process; ``systemctl show`` strings never substitute for that fact.
* The manifest written by ``prepare`` persists the idempotency key and a
  canonical digest of the bound payload (design §12.3).

Attestation documents are byte-compatible with
``app/runtime/hermes_managed.py``: canonical JSON (sorted keys, compact
separators, ASCII), HMAC-SHA256 hex without the ``hmac`` member, and
timezone-aware UTC ISO-8601 timestamps.

This module is standard-library only and must not import ``app.*``: it is
installed outside the project environment.
"""

from __future__ import annotations

import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "CLEAN_ENV",
    "DEFAULT_INSTALL_CONFIG",
    "GROUP_NAME",
    "HELPER_VERSION",
    "INACCESSIBLE_PATHS",
    "InstallConfig",
    "MAX_EGRESS_TOKEN_BYTES",
    "MAX_PROFILE_TEMPLATE_BYTES",
    "MAX_PROMPT_BYTES",
    "MAX_STDIN_BYTES",
    "RUNTIME_ATTESTATION_SCHEMA",
    "SYSTEMCTL",
    "SYSTEMD_RUN",
    "SandboxRequest",
    "TASK_ATTESTATION_SCHEMA",
    "UnitState",
    "WORKLOAD_ARGV",
    "attest",
    "build_systemd_run_argv",
    "cleanup",
    "inspect_unit",
    "launch",
    "load_install_config",
    "main",
    "parse_systemctl_show",
    "prepare",
    "read_process_uid",
    "run_request",
    "runtime_attestation",
    "runtime_handle_for",
    "sandbox_facts",
    "sign_attestation",
    "status",
    "stop",
    "task_attestation",
    "unit_for_task",
    "unit_state_document",
    "validate_request",
    "validate_unit",
]

# ---------------------------------------------------------------------------
# fixed deployment surface
# ---------------------------------------------------------------------------

HELPER_VERSION = "1"
DEFAULT_INSTALL_CONFIG = Path("/etc/agentmgr/hermes-exec.json")

SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
PYTHON_EXECUTABLE = "/usr/bin/python3"
WORKLOAD_UNIT_PATH = "/workload.py"
WORKLOAD_ARGV: tuple[str, ...] = (PYTHON_EXECUTABLE, WORKLOAD_UNIT_PATH)
TASK_UNIT_PATH = "/task"
RUNTIME_UNIT_PATH = "/runtime"
EGRESS_UNIT_PATH = "/task/egress.sock"
DESCRIPTION = "agentmgr-hermes-sandbox"

#: The fixed sandbox group: primary group of every unit and group of the task
#: directory.  It is *not* the trusted adapter's private group.
GROUP_NAME = "agentmgr-hermes"

#: Unix group names accepted for the trusted adapter's private group.
GROUP_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")

#: Fixed derivation, identical to ``app.runtime.hermes_managed``.
UNIT_PREFIX = "agentmgr-hermes-"
UNIT_HASH_DOMAIN = "agentmgr-hermes:v1:"
RUNTIME_HANDLE_PREFIX = "rtask_hermes_"
HANDLE_HASH_DOMAIN = "rtask_hermes:v1:"

UNIT_PATTERN = re.compile(r"^agentmgr-hermes-[0-9a-f]{16}$")
HANDLE_PATTERN = re.compile(r"^rtask_hermes_[A-Za-z0-9_-]{4,63}$")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SHA256_REF_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

TASK_ATTESTATION_SCHEMA = "agentmgr.hermes.sandbox.v1"
RUNTIME_ATTESTATION_SCHEMA = "agentmgr.hermes.runtime.v1"
MANIFEST_SCHEMA = "agentmgr.hermes.manifest.v1"

#: ``env -i``-like environment for every child command.
CLEAN_ENV: Mapping[str, str] = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
}

#: Host control-plane and container paths that no task unit may see.
INACCESSIBLE_PATHS: tuple[str, ...] = (
    "-/etc/agentmgr",
    "-/var/lib/agentmgr",
    "-/var/lib/postgresql",
    "-/srv",
    "-/var/lib/docker",
    "-/var/lib/containers",
    "-/var/lib/containerd",
)

#: Mount sources inside these prefixes are rejected: ``/run`` is replaced with a
#: private tmpfs (and the other prefixes are masked), so a bind mount from them
#: cannot be resolved inside the unit namespace.
MASKED_SOURCE_PREFIXES: tuple[Path, ...] = (
    Path("/run"),
    Path("/etc/agentmgr"),
    Path("/var/lib/agentmgr"),
    Path("/var/lib/postgresql"),
    Path("/srv"),
    Path("/var/lib/docker"),
    Path("/var/lib/containers"),
    Path("/var/lib/containerd"),
)

#: The attestation HMAC key must live in a root-only, unit-invisible directory:
#: ``/etc/agentmgr`` is masked by ``InaccessiblePaths`` in every sandbox unit, so
#: neither a task process nor a sibling group member can read it.
ATTESTATION_KEY_ROOT = Path("/etc/agentmgr")

#: Expected owner of the attestation key.  Production is always root; the
#: unprivileged unit-test fixture overrides it to its own uid (a unit test
#: cannot create a root-owned file).  The default is pinned by a test.
ATTESTATION_KEY_OWNER_UID = 0

#: ``PrivateTmp=yes`` replaces these with private tmpfs mounts, so they can
#: never be bind sources.  ``InstallConfig`` rejects them; the module-level
#: ``ALLOW_PRIVATE_TMP_SOURCES`` is a unit-test-only seam (never set in a
#: deployment) because the unprivileged test fixture must live under ``/tmp``.
PRIVATE_TMP_PREFIXES: tuple[Path, ...] = (Path("/tmp"), Path("/var/tmp"))
ALLOW_PRIVATE_TMP_SOURCES = False

SHOW_PROPERTIES = (
    "Id,LoadState,ActiveState,SubState,MainPID,Result,ExecMainStatus,"
    "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp,"
    "ControlGroup"
)

MAX_STDIN_BYTES = 64 * 1024
MAX_INSTALL_CONFIG_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 1_048_576
MAX_PROFILE_TEMPLATE_BYTES = 65_536
MAX_EGRESS_TOKEN_BYTES = 4_096
MAX_MARKER_BYTES = 16 * 1024
MAX_ATTESTATION_KEY_BYTES = 4_096
MIN_ATTESTATION_KEY_BYTES = 32
MAX_TEXT_BYTES = 256

MIN_EGRESS_TTL_SECONDS = 1
MAX_EGRESS_TTL_SECONDS = 300
MIN_RUNTIME_MAX_SEC = 1
MAX_RUNTIME_MAX_SEC = 86_400
MIN_MEMORY_MAX_MB = 1
MAX_MEMORY_MAX_MB = 1_048_576
MIN_CPU_QUOTA_PERCENT = 1
MAX_CPU_QUOTA_PERCENT = 6_400
MIN_TASKS_MAX = 1
MAX_TASKS_MAX = 65_536
MIN_TIMEOUT_SEC = 1
MAX_TIMEOUT_SEC = 300

ATTESTATION_TTL_SECONDS = 300
RUN_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.2

#: Fixed ``TimeoutStopSec`` of the transient unit; the helper's own SIGTERM /
#: SIGKILL wait uses ``InstallConfig.stop_timeout_sec``.
SANDBOX_TIMEOUT_STOP_SEC = 30

TASK_DIR_MODE = 0o770
MANIFEST_MODE = 0o640
TOKEN_FILE_MODE = 0o640
PROTECTED_MODE_MASK = 0o007

_SHA256_CHUNK_BYTES = 65_536

_REQUEST_FIELDS = frozenset(
    {
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
)

_OPERATIONS = frozenset(
    {"prepare", "launch", "status", "stop", "cleanup", "attest"}
)

#: Explicit denies; the exact-key check already rejects them.  Naming them
#: keeps the boundary reviewable and gives a distinct stable error.
_FORBIDDEN_FIELDS = frozenset(
    {
        "AmbientCapabilities",
        "BindPaths",
        "BindReadOnlyPaths",
        "CapabilityBoundingSet",
        "DynamicUser",
        "Environment",
        "EnvironmentFile",
        "ExecStart",
        "ExecStartPost",
        "ExecStartPre",
        "ExecStop",
        "ExecStopPost",
        "Group",
        "KillMode",
        "LoadCredential",
        "PrivateNetwork",
        "ProtectHome",
        "ProtectSystem",
        "RuntimeMaxSec",
        "StateDirectory",
        "SupplementaryGroups",
        "SystemCallFilter",
        "TemporaryFileSystem",
        "TimeoutStopSec",
        "User",
        "WorkingDirectory",
    }
)

_INSTALL_REQUIRED_FIELDS = frozenset(
    {
        "task_root",
        "runtime_root",
        "runtime_root_hash",
        "broker_socket",
        "workload",
        "adapter_user",
        "adapter_group",
        "attestation_key_path",
        "evidence_ref",
        "runtime_name",
        "runtime_version",
    }
)

_INSTALL_OPTIONAL_FIELDS = frozenset(
    {
        "helper_version",
        "stop_timeout_sec",
        "start_timeout_sec",
        "model_name",
        "adapter_uid",
        "adapter_gid",
    }
)

_HASH_FIELDS = ("prompt_sha256", "profile_template_sha256", "egress_token_sha256")

_LIMITS = {
    "ttl_seconds": (MIN_EGRESS_TTL_SECONDS, MAX_EGRESS_TTL_SECONDS),
    "runtime_max_sec": (MIN_RUNTIME_MAX_SEC, MAX_RUNTIME_MAX_SEC),
    "memory_max_mb": (MIN_MEMORY_MAX_MB, MAX_MEMORY_MAX_MB),
    "cpu_quota_percent": (MIN_CPU_QUOTA_PERCENT, MAX_CPU_QUOTA_PERCENT),
    "tasks_max": (MIN_TASKS_MAX, MAX_TASKS_MAX),
}

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class HelperError(Exception):
    """A fail-closed rejection carrying only a stable, path-free code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code or CONTROL_CHARACTERS.search(code):
            code = "internal_error"
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------------------
# immutable contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """One validated request; hashes and sizes only, never payload content."""

    operation: str
    task_id: str
    grant_id: str
    unit: str
    runtime_handle: str
    idempotency_key: str
    prompt_sha256: str
    prompt_bytes: int
    profile_template_sha256: str
    egress_token_sha256: str
    ttl_seconds: int
    runtime_max_sec: int
    memory_max_mb: int
    cpu_quota_percent: int
    tasks_max: int
    result_read: bool = False


@dataclass(frozen=True, slots=True)
class InstallConfig:
    """Root-only, secret-free deployment configuration."""

    task_root: Path
    runtime_root: Path
    runtime_root_hash: str
    broker_socket: Path
    workload: Path
    adapter_user: str
    adapter_group: str
    attestation_key_path: Path
    evidence_ref: str
    runtime_name: str
    runtime_version: str
    helper_version: str = HELPER_VERSION
    stop_timeout_sec: int = 30
    start_timeout_sec: int = 30
    model_name: str = ""
    adapter_uid: int | None = None
    adapter_gid: int | None = None
    #: Test/deployment-only numeric override for the fixed sandbox group; never
    #: read from the install-config JSON (``GROUP_NAME`` remains authoritative).
    sandbox_gid: int | None = None


@dataclass(frozen=True, slots=True)
class UnitState:
    """Whitelisted ``systemctl show`` facts for one unit."""

    unit: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int
    result: str
    exec_main_status: int | None
    active_enter: str | None
    exec_main_start: str | None
    exec_main_exit: str | None
    control_group: str

    @property
    def process_gone(self) -> bool:
        """Systemd facts only; :func:`_process_gone` also reads the cgroup."""

        if self.load_state == "not-found":
            return True
        return self.active_state in {"inactive", "failed"} and self.main_pid == 0


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------


def unit_for_task(task_id: object, idempotency_key: object) -> str:
    """Return ``agentmgr-hermes-<16 hex>`` exactly like Task 1."""

    _require_text("task_id", task_id)
    _require_text("idempotency_key", idempotency_key)
    material = f"{UNIT_HASH_DOMAIN}{task_id}\n{idempotency_key}".encode("utf-8")
    return UNIT_PREFIX + hashlib.sha256(material).hexdigest()[:16]


def runtime_handle_for(unit: object) -> str:
    """Return the opaque ``rtask_hermes_<32 hex>`` handle exactly like Task 1."""

    validate_unit(unit)
    assert isinstance(unit, str)
    material = f"{HANDLE_HASH_DOMAIN}{unit}".encode("utf-8")
    return RUNTIME_HANDLE_PREFIX + hashlib.sha256(material).hexdigest()[:32]


def validate_unit(unit: object) -> None:
    """Reject anything that is not a derived managed-sandbox unit name."""

    if not isinstance(unit, str) or UNIT_PATTERN.fullmatch(unit) is None:
        raise HelperError("invalid_unit")


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------


def validate_request(raw: Mapping[str, object]) -> SandboxRequest:
    """Validate the exact request schema and the task/grant/unit binding."""

    if not isinstance(raw, Mapping):
        raise HelperError("invalid_request")
    keys = set(raw)
    if keys & _FORBIDDEN_FIELDS:
        raise HelperError("forbidden_field")
    extra = keys - _REQUEST_FIELDS
    result_read = False
    if extra:
        if raw.get("operation") == "cleanup" and extra == {"result_read"}:
            if raw.get("result_read") is not True:
                raise HelperError("result_not_read")
            result_read = True
        else:
            raise HelperError("unknown_field")
    missing = _REQUEST_FIELDS - keys
    if missing:
        raise HelperError("missing_field")

    operation = raw["operation"]
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise HelperError("invalid_operation")
    if operation == "cleanup" and not result_read:
        raise HelperError("missing_field")

    request = SandboxRequest(
        operation=operation,
        task_id=_require_text("task_id", raw["task_id"]),
        grant_id=_require_text("grant_id", raw["grant_id"]),
        unit=raw["unit"],  # type: ignore[arg-type]
        runtime_handle=raw["runtime_handle"],  # type: ignore[arg-type]
        idempotency_key=_require_text("idempotency_key", raw["idempotency_key"]),
        prompt_sha256=raw["prompt_sha256"],  # type: ignore[arg-type]
        prompt_bytes=raw["prompt_bytes"],  # type: ignore[arg-type]
        profile_template_sha256=raw[  # type: ignore[arg-type]
            "profile_template_sha256"
        ],
        egress_token_sha256=raw["egress_token_sha256"],  # type: ignore[arg-type]
        ttl_seconds=raw["ttl_seconds"],  # type: ignore[arg-type]
        runtime_max_sec=raw["runtime_max_sec"],  # type: ignore[arg-type]
        memory_max_mb=raw["memory_max_mb"],  # type: ignore[arg-type]
        cpu_quota_percent=raw["cpu_quota_percent"],  # type: ignore[arg-type]
        tasks_max=raw["tasks_max"],  # type: ignore[arg-type]
        result_read=result_read,
    )
    return _validate_request_values(request)


def _validate_request_values(request: SandboxRequest) -> SandboxRequest:
    """Re-validate a possibly hand-built request (builder and every operation)."""

    if not isinstance(request, SandboxRequest):
        raise HelperError("invalid_request")
    if request.operation not in _OPERATIONS:
        raise HelperError("invalid_operation")
    if request.result_read and request.operation != "cleanup":
        raise HelperError("invalid_request")

    task_id = _require_text("task_id", request.task_id)
    _require_text("grant_id", request.grant_id)
    idempotency_key = _require_text("idempotency_key", request.idempotency_key)

    validate_unit(request.unit)
    if request.unit != unit_for_task(task_id, idempotency_key):
        raise HelperError("unit_name_mismatch")

    handle = request.runtime_handle
    if not isinstance(handle, str) or HANDLE_PATTERN.fullmatch(handle) is None:
        raise HelperError("invalid_request")
    if handle != runtime_handle_for(request.unit):
        raise HelperError("runtime_handle_mismatch")

    for field in _HASH_FIELDS:
        value = getattr(request, field)
        if not isinstance(value, str) or SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise HelperError("invalid_digest")

    if (
        type(request.prompt_bytes) is not int
        or not 1 <= request.prompt_bytes <= MAX_PROMPT_BYTES
    ):
        raise HelperError("invalid_limit")
    for field, (minimum, maximum) in _LIMITS.items():
        value = getattr(request, field)
        if type(value) is not int or not minimum <= value <= maximum:
            raise HelperError("invalid_limit")

    return request


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise HelperError("invalid_request")
    if len(value) > MAX_TEXT_BYTES or CONTROL_CHARACTERS.search(value):
        raise HelperError("invalid_request")
    return value


# ---------------------------------------------------------------------------
# install config
# ---------------------------------------------------------------------------


def load_install_config(path: Path) -> InstallConfig:
    """Read the root-owned install config and validate every fixed value."""

    if not isinstance(path, (str, os.PathLike)):
        raise HelperError("install_config_invalid")
    config_path = Path(path)
    info = _lstat(config_path)
    if info is None:
        raise HelperError("install_config_unavailable")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise HelperError("install_config_invalid")
    if info.st_uid != os.geteuid():
        raise HelperError("install_config_owner_invalid")
    if info.st_size <= 0 or info.st_size > MAX_INSTALL_CONFIG_BYTES:
        raise HelperError("install_config_invalid")
    if stat.S_IMODE(info.st_mode) & 0o077 not in (0, 0o040):
        raise HelperError("install_config_mode_invalid")

    raw = _read_file(config_path, MAX_INSTALL_CONFIG_BYTES, "install_config")
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        raise HelperError("install_config_invalid") from None
    if not isinstance(document, Mapping):
        raise HelperError("install_config_invalid")

    keys = set(document)
    if keys - _INSTALL_REQUIRED_FIELDS - _INSTALL_OPTIONAL_FIELDS:
        raise HelperError("install_config_unknown_field")
    missing = _INSTALL_REQUIRED_FIELDS - keys
    if missing:
        raise HelperError("install_config_missing_field")

    config = InstallConfig(
        task_root=_config_path(document["task_root"]),
        runtime_root=_config_path(document["runtime_root"]),
        runtime_root_hash=_config_text(document["runtime_root_hash"]),
        broker_socket=_config_path(document["broker_socket"]),
        workload=_config_path(document["workload"]),
        adapter_user=_config_text(document["adapter_user"]),
        adapter_group=_config_text(document["adapter_group"]),
        attestation_key_path=_config_path(document["attestation_key_path"]),
        evidence_ref=_config_text(document["evidence_ref"]),
        runtime_name=_config_text(document["runtime_name"]),
        runtime_version=_config_text(document["runtime_version"]),
        helper_version=_config_text(document.get("helper_version", HELPER_VERSION)),
        stop_timeout_sec=_config_timeout(
            document.get("stop_timeout_sec", 30), "stop_timeout_sec"
        ),
        start_timeout_sec=_config_timeout(
            document.get("start_timeout_sec", 30), "start_timeout_sec"
        ),
        model_name=_config_text(document.get("model_name", ""), allow_empty=True),
        adapter_uid=_config_id(document.get("adapter_uid")),
        adapter_gid=_config_id(document.get("adapter_gid")),
    )
    _validate_install_config(config)
    return config


def _config_text(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise HelperError("install_config_invalid")
    if not value and not allow_empty:
        raise HelperError("install_config_invalid")
    if len(value) > MAX_TEXT_BYTES or CONTROL_CHARACTERS.search(value):
        raise HelperError("install_config_invalid")
    return value


def _config_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise HelperError("install_config_invalid")
    path = _normalise_absolute(value)
    if path is None:
        raise HelperError("install_config_invalid")
    return path


def _config_timeout(value: object, name: str) -> int:
    if type(value) is not int or not MIN_TIMEOUT_SEC <= value <= MAX_TIMEOUT_SEC:
        raise HelperError("install_config_invalid")
    assert isinstance(value, int)
    return value


def _config_id(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise HelperError("install_config_invalid")
    assert isinstance(value, int)
    return value


def _normalise_absolute(value: str) -> Path | None:
    """Return a clean absolute path, or ``None`` for relative/``..`` input."""

    if CONTROL_CHARACTERS.search(value):
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        return None
    return path


def _validate_install_config(config: object) -> InstallConfig:
    if not isinstance(config, InstallConfig):
        raise HelperError("install_config_invalid")
    _config_text(config.adapter_user)
    _config_text(config.evidence_ref)
    _config_text(config.runtime_name)
    _config_text(config.runtime_version)
    _config_text(config.helper_version)
    _config_timeout(config.stop_timeout_sec, "stop_timeout_sec")
    _config_timeout(config.start_timeout_sec, "start_timeout_sec")
    # ``adapter_group`` is the trusted adapter's *private* group (used only for
    # attestation-key access).  It must be a valid group name and must differ
    # from the fixed sandbox group, otherwise the sandbox could read the key.
    adapter_group = _config_text(config.adapter_group)
    if (
        GROUP_NAME_PATTERN.fullmatch(adapter_group) is None
        or adapter_group == GROUP_NAME
    ):
        raise HelperError("install_config_invalid")
    if SHA256_REF_PATTERN.fullmatch(config.runtime_root_hash or "") is None:
        raise HelperError("install_config_invalid")
    if config.adapter_uid is not None and (
        type(config.adapter_uid) is not int or config.adapter_uid <= 0
    ):
        raise HelperError("install_config_invalid")
    if config.adapter_gid is not None and (
        type(config.adapter_gid) is not int or config.adapter_gid <= 0
    ):
        raise HelperError("install_config_invalid")
    if config.sandbox_gid is not None and (
        type(config.sandbox_gid) is not int or config.sandbox_gid <= 0
    ):
        raise HelperError("install_config_invalid")

    for name in ("task_root", "runtime_root", "broker_socket", "workload"):
        path = getattr(config, name)
        if not isinstance(path, Path) or _normalise_absolute(str(path)) is None:
            raise HelperError("install_config_invalid")
        if path == Path("/"):
            raise HelperError("install_config_invalid")
        if _is_masked_source(path):
            raise HelperError("install_config_masked_source")
        if not ALLOW_PRIVATE_TMP_SOURCES and _is_under_any(
            path, PRIVATE_TMP_PREFIXES
        ):
            raise HelperError("install_config_private_tmp_source")

    key_path = config.attestation_key_path
    if not isinstance(key_path, Path):
        raise HelperError("install_config_invalid")
    if _normalise_absolute(str(key_path)) is None:
        raise HelperError("install_config_invalid")
    # The HMAC key must be invisible to every sandbox unit and to the shared
    # ``agentmgr-hermes`` group: only ``/etc/agentmgr`` (masked with
    # ``InaccessiblePaths`` and never bind-mounted) qualifies.
    if not _is_under(key_path, ATTESTATION_KEY_ROOT):
        raise HelperError("attestation_key_path_invalid")
    if _is_under(key_path, config.task_root):
        raise HelperError("attestation_key_path_invalid")
    return config


def _adapter_uid(config: InstallConfig) -> int:
    """Resolve the trusted adapter UID without the group-membership probe."""

    uid = config.adapter_uid
    if uid is None:
        try:
            uid = pwd.getpwnam(config.adapter_user).pw_uid
        except KeyError:
            raise HelperError("user_unavailable") from None
    if type(uid) is not int or uid <= 0:
        raise HelperError("install_config_invalid")
    return uid


def _adapter_ids(config: InstallConfig) -> tuple[int, int]:
    """Return the trusted adapter UID and its *private* group GID."""

    uid = _adapter_uid(config)
    gid = config.adapter_gid
    if gid is None:
        try:
            gid = grp.getgrnam(config.adapter_group).gr_gid
        except KeyError:
            raise HelperError("group_unavailable") from None
    if type(gid) is not int or gid <= 0:
        raise HelperError("install_config_invalid")
    if config.adapter_uid is None and config.adapter_gid is None:
        # NSS was consulted, so verify the real membership the task-dir access
        # depends on.  Numeric overrides (tests / NSS-less deployments) cannot
        # be checked and are trusted as root-only configuration.
        _require_adapter_sandbox_membership(
            config.adapter_user, gid, _sandbox_gid(config)
        )
    return uid, gid


def _sandbox_gid(config: InstallConfig) -> int:
    """Return the fixed ``agentmgr-hermes`` GID (or the test-only override)."""

    if config.sandbox_gid is not None:
        if type(config.sandbox_gid) is not int or config.sandbox_gid <= 0:
            raise HelperError("install_config_invalid")
        return config.sandbox_gid
    try:
        gid = grp.getgrnam(GROUP_NAME).gr_gid
    except KeyError:
        raise HelperError("group_unavailable") from None
    if type(gid) is not int or gid <= 0:
        raise HelperError("install_config_invalid")
    return gid


def _require_adapter_sandbox_membership(
    adapter_user: str, adapter_gid: int, sandbox_gid: int
) -> None:
    """Fail closed unless the adapter user can reach task dirs via the group."""

    try:
        groups = os.getgrouplist(adapter_user, adapter_gid)
    except OSError:
        raise HelperError("user_unavailable") from None
    if sandbox_gid not in groups:
        raise HelperError("adapter_sandbox_group_missing")


def _is_masked_source(path: Path) -> bool:
    return _is_under_any(path, MASKED_SOURCE_PREFIXES)


def _is_under(path: Path, root: Path) -> bool:
    """Boundary-aware containment: ``/a/b`` is under ``/a`` but not ``/ab``."""

    return path == root or root in path.parents


def _is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(_is_under(path, root) for root in roots)


# ---------------------------------------------------------------------------
# filesystem helpers
# ---------------------------------------------------------------------------


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise HelperError("path_unavailable") from None


def _read_file(path: Path, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise HelperError(f"{label}_unavailable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HelperError(f"{label}_invalid")
        if info.st_size > limit:
            raise HelperError(f"{label}_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _SHA256_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HelperError(f"{label}_invalid")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _require_fixed_directory(
    path: Path,
    *,
    code: str,
    mode: int | None = None,
    check_owner: bool = True,
    group_id: int | None = None,
) -> os.stat_result:
    info = _lstat(path)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HelperError(f"{code}_invalid")
    if check_owner and info.st_uid != os.geteuid():
        raise HelperError(f"{code}_owner_invalid")
    if group_id is not None and info.st_gid != group_id:
        raise HelperError(f"{code}_group_invalid")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise HelperError(f"{code}_mode_invalid")
    return info


def _mkdir_owned(
    path: Path, *, mode: int, uid: int, gid: int, code: str
) -> None:
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        return
    except OSError:
        raise HelperError(f"{code}_create_failed") from None
    try:
        os.chmod(path, mode)
        if (uid, gid) != (os.geteuid(), os.getegid()):
            if os.geteuid() != 0:
                raise HelperError(f"{code}_owner_failed")
            os.chown(path, uid, gid)
    except OSError:
        raise HelperError(f"{code}_owner_failed") from None
    except HelperError:
        try:
            os.rmdir(path)
        except OSError:
            pass
        raise
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise HelperError(f"{code}_invalid")
    if info.st_uid != uid or info.st_gid != gid:
        raise HelperError(f"{code}_owner_failed")


def _ensure_task_dir(config: InstallConfig, unit: str) -> tuple[Path, bool]:
    uid, _ = _adapter_ids(config)
    sandbox_gid = _sandbox_gid(config)
    task_dir = config.task_root / unit
    created = False
    info = _lstat(task_dir)
    if info is None:
        _mkdir_owned(
            task_dir, mode=TASK_DIR_MODE, uid=uid, gid=sandbox_gid, code="task_dir"
        )
        created = True
    else:
        _require_fixed_directory(
            task_dir, code="task_dir", mode=TASK_DIR_MODE, check_owner=False,
            group_id=sandbox_gid,
        )
        if info.st_uid != uid:
            raise HelperError("task_dir_owner_invalid")

    out_dir = task_dir / "out"
    info = _lstat(out_dir)
    if info is None:
        _mkdir_owned(
            out_dir, mode=TASK_DIR_MODE, uid=uid, gid=sandbox_gid, code="out_dir"
        )
        created = True
    else:
        _require_fixed_directory(
            out_dir, code="out_dir", mode=TASK_DIR_MODE, check_owner=False,
            group_id=sandbox_gid,
        )
        if info.st_uid != uid:
            raise HelperError("out_dir_owner_invalid")
    return task_dir, created


def _require_task_dir(config: InstallConfig, unit: str) -> Path:
    uid, _ = _adapter_ids(config)
    sandbox_gid = _sandbox_gid(config)
    task_dir = config.task_root / unit
    info = _lstat(task_dir)
    if info is None:
        raise HelperError("task_dir_missing")
    _require_fixed_directory(
        task_dir, code="task_dir", mode=TASK_DIR_MODE, check_owner=False,
        group_id=sandbox_gid,
    )
    if info.st_uid != uid:
        raise HelperError("task_dir_owner_invalid")
    _require_fixed_directory(
        task_dir / "out", code="out_dir", mode=TASK_DIR_MODE, check_owner=False,
        group_id=sandbox_gid,
    )
    return task_dir


def _require_task_root(config: InstallConfig) -> None:
    gid = _sandbox_gid(config)
    info = _require_fixed_directory(config.task_root, code="task_root",
                                    group_id=gid)
    if stat.S_IMODE(info.st_mode) & PROTECTED_MODE_MASK:
        raise HelperError("task_root_mode_invalid")


def _write_task_file(
    path: Path, data: bytes, *, mode: int, uid: int, gid: int, label: str
) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(temporary, flags, mode)
    except OSError:
        raise HelperError(f"{label}_write_failed") from None
    try:
        try:
            os.fchmod(descriptor, mode)
            if (uid, gid) != (os.geteuid(), os.getegid()):
                if os.geteuid() != 0:
                    raise HelperError(f"{label}_write_failed")
                os.fchown(descriptor, uid, gid)
            remaining = data
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise HelperError(f"{label}_write_failed")
                remaining = remaining[written:]
            os.fsync(descriptor)
        except OSError:
            raise HelperError(f"{label}_write_failed") from None
    except BaseException:
        os.close(descriptor)
        _discard(temporary)
        raise
    os.close(descriptor)
    try:
        os.replace(temporary, path)
    except OSError:
        _discard(temporary)
        raise HelperError(f"{label}_write_failed") from None

    info = _lstat(path)
    if (
        info is None
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_gid != gid
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise HelperError(f"{label}_write_failed")


def _discard(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _verify_task_file(
    path: Path,
    *,
    label: str,
    expected_sha256: str,
    expected_size: int | None,
    max_size: int,
    uid: int,
    gid: int,
    exact_mode: int | None = None,
) -> None:
    info = _lstat(path)
    if info is None:
        raise HelperError(f"{label}_missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HelperError(f"{label}_invalid")
    if info.st_uid != uid:
        raise HelperError(f"{label}_owner_invalid")
    if info.st_gid != gid:
        raise HelperError(f"{label}_group_invalid")
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None:
        if mode != exact_mode:
            raise HelperError(f"{label}_mode_invalid")
    elif mode not in {0o440, 0o640}:
        # The dynamic unit reads these through its static ``agentmgr-hermes``
        # primary group, so group-read is mandatory.  Owner-only 0400/0600,
        # group write/execute and every "other" bit are rejected.
        raise HelperError(f"{label}_mode_invalid")
    if info.st_size <= 0 or info.st_size > max_size:
        raise HelperError(f"{label}_size_mismatch")
    if expected_size is not None and info.st_size != expected_size:
        raise HelperError(f"{label}_size_mismatch")

    flags = _FILE_FLAGS
    try:
        descriptor = os.open(path, flags)
    except BlockingIOError:
        raise HelperError(f"{label}_invalid") from None
    except OSError:
        raise HelperError(f"{label}_invalid") from None
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            # A unit can race a FIFO/socket/device into prompt/profile after the
            # lstat; ``O_NONBLOCK`` prevents the root helper from blocking and
            # the regular-file check rejects the substitution.
            raise HelperError(f"{label}_invalid")
        while True:
            try:
                chunk = os.read(descriptor, _SHA256_CHUNK_BYTES)
            except BlockingIOError:
                raise HelperError(f"{label}_invalid") from None
            except OSError:
                raise HelperError(f"{label}_invalid") from None
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HelperError(f"{label}_size_mismatch")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if total != info.st_size or digest.hexdigest() != expected_sha256:
        raise HelperError(f"{label}_hash_mismatch")


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _open_child_directory(parent_fd: int, name: str, label: str) -> int | None:
    """Open a real child directory (never a symlink) relative to ``parent_fd``."""

    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise HelperError(f"{label}_invalid") from None


def _read_fd(descriptor: int, limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, _SHA256_CHUNK_BYTES)
        except BlockingIOError:
            # ``O_NONBLOCK`` never blocks the root helper on a unit-writable
            # FIFO/socket; an unexpected EAGAIN is a malformed marker.
            raise HelperError(f"{label}_invalid") from None
        except OSError:
            raise HelperError(f"{label}_invalid") from None
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HelperError(f"{label}_invalid")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_json_at(dir_fd: int, name: str, label: str) -> dict[str, object] | None:
    """Read one bounded JSON object relative to a verified directory fd.

    ``_FILE_FLAGS`` includes ``O_NONBLOCK`` and ``O_NOFOLLOW``: a sandbox unit
    can create a FIFO/socket/device named like a marker, and the root helper
    must never block on it.  ``fstat`` runs before any read, so only a regular,
    non-empty, size-bounded file is accepted; EAGAIN, an empty file or invalid
    JSON is a stable ``<label>_invalid``.
    """

    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise HelperError(f"{label}_invalid") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_MARKER_BYTES:
            raise HelperError(f"{label}_invalid")
        raw = _read_fd(descriptor, MAX_MARKER_BYTES, label)
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        raise HelperError(f"{label}_invalid") from None
    if not isinstance(document, dict):
        raise HelperError(f"{label}_invalid")
    return document


def _open_task_root(task_root: Path) -> int | None:
    """Open the configured task root without following a symlinked root."""

    try:
        return os.open(task_root, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return None
    except OSError:
        raise HelperError("task_root_invalid") from None


def _open_unit_directory(task_root: Path, unit: str) -> int | None:
    """Open ``<task_root>/<unit>`` through real, non-symlinked directory fds."""

    root_fd = _open_task_root(task_root)
    if root_fd is None:
        return None
    try:
        return _open_child_directory(root_fd, unit, "task_dir")
    finally:
        os.close(root_fd)


def _read_task_marker(
    task_root: Path, unit: str, name: str, label: str
) -> dict[str, object] | None:
    """Read ``<task_root>/<unit>/status/<name>`` without following symlinks."""

    task_fd = _open_unit_directory(task_root, unit)
    if task_fd is None:
        return None
    try:
        status_fd = _open_child_directory(task_fd, "status", "status_dir")
        if status_fd is None:
            return None
        try:
            return _read_json_at(status_fd, name, label)
        finally:
            os.close(status_fd)
    finally:
        os.close(task_fd)


def _validate_started_document(
    document: Mapping[str, object], request: SandboxRequest
) -> None:
    if (
        document.get("unit") != request.unit
        or document.get("task_id") != request.task_id
        or document.get("runtime_handle") != request.runtime_handle
    ):
        raise HelperError("started_marker_invalid")


def _read_started_marker(
    task_root: Path, request: SandboxRequest
) -> dict[str, object] | None:
    document = _read_task_marker(
        task_root, request.unit, "started.json", "started_marker"
    )
    if document is None:
        return None
    _validate_started_document(document, request)
    return document


def read_process_uid(pid: object) -> int:
    """Return the real UID of ``pid`` from ``/proc/<pid>/status``."""

    if type(pid) is not int or pid <= 0:
        raise HelperError("process_uid_unavailable")
    path = Path("/proc") / str(pid) / "status"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise HelperError("process_uid_unavailable") from None
    for line in raw.splitlines():
        if not line.startswith("Uid:"):
            continue
        fields = line.split()
        if len(fields) < 2 or not fields[1].isdigit():
            raise HelperError("process_uid_unavailable")
        uid = int(fields[1])
        if uid <= 0:
            raise HelperError("process_uid_unavailable")
        return uid
    raise HelperError("process_uid_unavailable")


# ---------------------------------------------------------------------------
# delegation
# ---------------------------------------------------------------------------


def run_checked(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one fixed argv with a clean environment and no shell."""

    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
        shell=False,
        env=dict(CLEAN_ENV),
    )


def _run(
    argv: list[str], *, runner: Runner, allow_failure: bool = False
) -> subprocess.CompletedProcess[str]:
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
    ):
        raise HelperError("internal_error")
    try:
        completed = runner(argv)
    except subprocess.TimeoutExpired:
        raise HelperError("command_timeout") from None
    except subprocess.CalledProcessError:
        raise HelperError("command_failed") from None
    except OSError:
        raise HelperError("command_unavailable") from None
    if not isinstance(completed, subprocess.CompletedProcess):
        raise HelperError("command_failed")
    if completed.returncode != 0 and not allow_failure:
        raise HelperError("command_failed")
    return completed


def _run_quiet(argv: list[str], *, runner: Runner) -> None:
    try:
        _run(argv, runner=runner, allow_failure=True)
    except HelperError:
        pass


def inspect_unit(unit: str, *, runner: Runner = run_checked) -> UnitState:
    """Read the whitelisted facts of one unit."""

    validate_unit(unit)
    completed = _run(
        [SYSTEMCTL, "show", "--timestamp=us", "-p", SHOW_PROPERTIES, unit],
        runner=runner,
    )
    return parse_systemctl_show(completed.stdout or "")


def parse_systemctl_show(text: str) -> UnitState:
    """Parse ``systemctl show`` output into a whitelisted :class:`UnitState`."""

    values: dict[str, str] = {}
    if isinstance(text, str):
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key:
                values[key] = value

    unit = values.get("Id", "")
    if unit.endswith(".service"):
        unit = unit[: -len(".service")]

    def _optional(name: str) -> str | None:
        value = values.get(name, "")
        return value or None

    return UnitState(
        unit=unit,
        load_state=values.get("LoadState", ""),
        active_state=values.get("ActiveState", ""),
        sub_state=values.get("SubState", ""),
        main_pid=_parse_int(values.get("MainPID")),
        result=values.get("Result", ""),
        exec_main_status=_parse_optional_int(values.get("ExecMainStatus")),
        active_enter=_optional("ActiveEnterTimestamp"),
        exec_main_start=_optional("ExecMainStartTimestamp"),
        exec_main_exit=_optional("ExecMainExitTimestamp"),
        control_group=values.get("ControlGroup", ""),
    )


def _parse_int(value: object) -> int:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _parse_optional_int(value: object) -> int | None:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def unit_state_document(state: UnitState) -> dict[str, object]:
    """Render only the whitelisted, path-free unit facts.

    ``systemctl show`` reports ``Result=success``/``ExecMainStatus=0`` even for
    a unit that does not exist, so a ``not-found`` unit is normalised to
    ``None`` instead of claiming a successful exit.
    """

    not_found = state.load_state == "not-found"
    return {
        "load_state": state.load_state,
        "active_state": state.active_state,
        "sub_state": state.sub_state,
        "main_pid": state.main_pid,
        "result": None if not_found else state.result,
        "exit_status": None if not_found else state.exec_main_status,
        "active_enter": state.active_enter,
        "exec_main_start": state.exec_main_start,
        "exec_main_exit": state.exec_main_exit,
        "process_gone": _process_gone(state),
    }


def _cgroup_procs_path(control_group: str) -> Path | None:
    """Return ``cgroup.procs`` for a unit cgroup, or ``None`` when unusable."""

    if not control_group or not control_group.startswith("/"):
        return None
    return Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs"


def _read_cgroup_pids(control_group: str) -> list[int] | None:
    """Return the cgroup's PIDs, or ``None`` when it cannot be inspected."""

    path = _cgroup_procs_path(control_group)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    return [int(item) for item in text.split() if item.isdigit()]


def _process_gone(state: UnitState) -> bool:
    """True only when systemd and (when readable) the cgroup hold no process."""

    if state.load_state == "not-found":
        return True
    pids = _read_cgroup_pids(state.control_group)
    if pids:
        return False
    return state.process_gone


# ---------------------------------------------------------------------------
# systemd argv
# ---------------------------------------------------------------------------


def build_systemd_run_argv(
    request: SandboxRequest,
    *,
    task_dir: Path,
    runtime_root: Path,
    broker_socket: Path,
    workload: Path,
    stop_timeout_sec: int = SANDBOX_TIMEOUT_STOP_SEC,
) -> list[str]:
    """Return the fixed transient-unit argv for one validated request."""

    request = _require_sandbox_request(request)
    if (
        type(stop_timeout_sec) is not int
        or not MIN_TIMEOUT_SEC <= stop_timeout_sec <= MAX_TIMEOUT_SEC
    ):
        raise HelperError("invalid_limit")

    task_dir = _require_argv_path(task_dir, "task_dir")
    runtime_root = _require_argv_path(runtime_root, "runtime_root")
    broker_socket = _require_argv_path(broker_socket, "broker_socket")
    workload = _require_argv_path(workload, "workload")
    if task_dir.name != request.unit:
        raise HelperError("task_dir_invalid")
    task_root = task_dir.parent
    if task_root == Path("/"):
        raise HelperError("task_root_invalid")
    for path in (task_dir, runtime_root, broker_socket, workload):
        if _is_masked_source(path):
            raise HelperError("masked_source")
        if not ALLOW_PRIVATE_TMP_SOURCES and _is_under_any(
            path, PRIVATE_TMP_PREFIXES
        ):
            # ``PrivateTmp=yes`` replaces /tmp and /var/tmp inside the unit, so
            # a bind source there cannot be resolved by systemd.
            raise HelperError("private_tmp_source")

    properties = [
        "--property=DynamicUser=yes",
        # A static primary group (instead of SupplementaryGroups) means files
        # the unit writes inherit ``agentmgr-hermes`` and the adapter can read
        # them even though the unit UID is dynamic.
        f"--property=Group={GROUP_NAME}",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=PrivateTmp=yes",
        "--property=PrivateDevices=yes",
        "--property=TemporaryFileSystem=/run",
        "--property=PrivateNetwork=yes",
        "--property=RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
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
        *(f"--property=InaccessiblePaths={item}" for item in INACCESSIBLE_PATHS),
        # Hide the task root itself so a unit cannot list or read sibling task
        # directories/tokens, while its own task dir stays available through
        # the explicit bind below (verified on the approved host).
        f"--property=InaccessiblePaths=-{task_root}",
        "--property=UMask=0007",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=ExitType=cgroup",
        f"--property=TimeoutStopSec={stop_timeout_sec}",
        f"--property=RuntimeMaxSec={request.runtime_max_sec}s",
        f"--property=MemoryMax={request.memory_max_mb}M",
        f"--property=CPUQuota={request.cpu_quota_percent}%",
        f"--property=TasksMax={request.tasks_max}",
        "--property=LimitNOFILE=1024",
        f"--property=WorkingDirectory={TASK_UNIT_PATH}",
        f"--property=BindPaths={task_dir}:{TASK_UNIT_PATH}",
        f"--property=BindReadOnlyPaths={runtime_root}:{RUNTIME_UNIT_PATH}",
        f"--property=BindReadOnlyPaths={broker_socket}:{EGRESS_UNIT_PATH}",
        f"--property=BindReadOnlyPaths={workload}:{WORKLOAD_UNIT_PATH}",
    ]
    return [
        SYSTEMD_RUN,
        f"--unit={request.unit}",
        f"--description={DESCRIPTION}",
        *properties,
        *WORKLOAD_ARGV,
    ]


def _require_argv_path(value: object, label: str) -> Path:
    if not isinstance(value, Path):
        raise HelperError(f"{label}_invalid")
    path = _normalise_absolute(str(value))
    if path is None or path != value:
        raise HelperError(f"{label}_invalid")
    return path


# ---------------------------------------------------------------------------
# attestation
# ---------------------------------------------------------------------------


def sandbox_facts() -> dict[str, object]:
    """Facts the unit profile guarantees; verified again by the adapter."""

    return {
        "private_network": True,
        "protect_home": True,
        "protect_system": "strict",
        "no_new_privileges": True,
        "capability_bounding_set": "empty",
        "external_ip": False,
        "toolsets": ["terminal"],
    }


def sign_attestation(
    payload: Mapping[str, object], key: bytes
) -> dict[str, object]:
    """Return ``payload`` plus the canonical-JSON HMAC-SHA256 hex signature."""

    if not isinstance(payload, Mapping) or "hmac" in payload:
        raise HelperError("attestation_payload_invalid")
    if not isinstance(key, bytes) or len(key) < MIN_ATTESTATION_KEY_BYTES:
        raise HelperError("attestation_key_invalid")
    document = dict(payload)
    signature = hmac.new(key, _canonical_json(document), hashlib.sha256).hexdigest()
    document["hmac"] = signature
    return document


def _canonical_json(document: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(document), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _read_attestation_key(config: InstallConfig) -> bytes:
    path = config.attestation_key_path
    info = _lstat(path)
    if info is None:
        raise HelperError("attestation_key_missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HelperError("attestation_key_invalid")
    if info.st_uid != ATTESTATION_KEY_OWNER_UID:
        raise HelperError("attestation_key_owner_invalid")
    _, adapter_gid = _adapter_ids(config)
    sandbox_gid = _sandbox_gid(config)
    if adapter_gid == sandbox_gid:
        # The adapter's private group must not be the sandbox group, otherwise
        # a 0440 key would be readable by every sandbox unit.
        raise HelperError("install_config_invalid")
    mode = stat.S_IMODE(info.st_mode)
    # Accepted: 0600/0400 (owner-only) and 0440 owned by the adapter's private
    # group.  Any group read with the sandbox group or an unrelated group, and
    # any other mode (e.g. 0640), is rejected: the sandbox unit must never be
    # able to read the HMAC key.
    if mode == 0o440:
        if info.st_gid == sandbox_gid:
            raise HelperError("attestation_key_sandbox_group_readable")
        if info.st_gid != adapter_gid:
            raise HelperError("attestation_key_group_invalid")
    elif mode not in {0o600, 0o400}:
        raise HelperError("attestation_key_mode_invalid")
    if not MIN_ATTESTATION_KEY_BYTES <= info.st_size <= MAX_ATTESTATION_KEY_BYTES:
        raise HelperError("attestation_key_invalid")
    key = _read_file(path, MAX_ATTESTATION_KEY_BYTES, "attestation_key")
    if len(key) < MIN_ATTESTATION_KEY_BYTES:
        raise HelperError("attestation_key_invalid")
    return key


def runtime_attestation(
    config: InstallConfig,
    *,
    key: bytes,
    now: datetime | None = None,
) -> dict[str, object]:
    """Sign the secret-free runtime attestation (``agentmgr.hermes.runtime.v1``)."""

    current = now or _utcnow()
    payload = {
        "schema": RUNTIME_ATTESTATION_SCHEMA,
        "helper_version": config.helper_version,
        "runtime_name": config.runtime_name,
        "runtime_version": config.runtime_version,
        "runtime_root_hash": config.runtime_root_hash,
        "sandbox_facts": sandbox_facts(),
        "broker_socket_ok": _broker_socket_ok(config),
        "evidence_ref": config.evidence_ref,
        "issued_at": _iso_utc(current),
        "expires_at": _iso_utc(current + timedelta(seconds=ATTESTATION_TTL_SECONDS)),
        "nonce": secrets.token_hex(16),
    }
    return sign_attestation(payload, key)


def task_attestation(
    request: SandboxRequest,
    config: InstallConfig,
    *,
    uid: int,
    key: bytes,
    now: datetime | None = None,
) -> dict[str, object]:
    """Sign the launch attestation (``agentmgr.hermes.sandbox.v1``)."""

    if type(uid) is not int or uid <= 0:
        raise HelperError("process_uid_unavailable")
    current = now or _utcnow()
    payload = {
        "schema": TASK_ATTESTATION_SCHEMA,
        "unit": request.unit,
        "task_id": request.task_id,
        "grant_id": request.grant_id,
        "runtime_handle": request.runtime_handle,
        "uid": uid,
        "runtime_root_hash": config.runtime_root_hash,
        "profile_template_sha256": request.profile_template_sha256,
        "prompt_sha256": request.prompt_sha256,
        "sandbox_facts": sandbox_facts(),
        "issued_at": _iso_utc(current),
        "expires_at": _iso_utc(current + timedelta(seconds=ATTESTATION_TTL_SECONDS)),
        "nonce": secrets.token_hex(16),
    }
    return sign_attestation(payload, key)


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


def prepare(request: object, config: object) -> dict[str, object]:
    """Create the derived task directory and ``out/`` with fixed ownership."""

    request = _require_sandbox_request(request)
    config = _validate_install_config(config)
    _require_task_root(config)
    task_dir, created = _ensure_task_dir(config, request.unit)
    uid, _ = _adapter_ids(config)
    sandbox_gid = _sandbox_gid(config)
    binding = {
        "schema": MANIFEST_SCHEMA,
        "unit": request.unit,
        "task_id": request.task_id,
        "grant_id": request.grant_id,
        "runtime_handle": request.runtime_handle,
        "idempotency_key": request.idempotency_key,
        "runtime_root_hash": config.runtime_root_hash,
        "prompt_sha256": request.prompt_sha256,
        "prompt_bytes": request.prompt_bytes,
        "profile_template_sha256": request.profile_template_sha256,
        "egress_token_sha256": request.egress_token_sha256,
        "ttl_seconds": request.ttl_seconds,
        "runtime_max_sec": request.runtime_max_sec,
        "memory_max_mb": request.memory_max_mb,
        "cpu_quota_percent": request.cpu_quota_percent,
        "tasks_max": request.tasks_max,
        "egress_socket": EGRESS_UNIT_PATH,
        "toolset": "terminal",
    }
    # Design §12.3: the manifest persists the dispatcher idempotency key and a
    # digest of the bound payload so a replay/conflict decision can be made
    # without trusting caller input. ``created_at`` is deliberately excluded.
    manifest = dict(binding)
    manifest["payload_sha256"] = hashlib.sha256(_canonical_json(binding)).hexdigest()
    manifest["created_at"] = _iso_utc(_utcnow())
    if config.model_name:
        manifest["model"] = config.model_name
    _write_task_file(
        task_dir / "manifest.json",
        _canonical_json(manifest),
        mode=MANIFEST_MODE,
        uid=uid,
        gid=sandbox_gid,
        label="manifest",
    )
    return {
        "unit": request.unit,
        "prepared": True,
        "created": created,
        "task_dir_ready": True,
    }


def launch(
    request: object, config: object, *, runner: Runner = run_checked
) -> dict[str, object]:
    """Verify the task files, start the fixed unit and sign its attestation."""

    request = _require_sandbox_request(request)
    config = _validate_install_config(config)
    task_dir = _require_task_dir_and_runtime(request, config)
    _verify_task_files(request, config, task_dir)
    key = _read_attestation_key(config)

    existing = inspect_unit(request.unit, runner=runner)
    if existing.load_state != "not-found":
        raise HelperError("unit_already_exists")

    argv = build_systemd_run_argv(
        request,
        task_dir=task_dir,
        runtime_root=config.runtime_root,
        broker_socket=config.broker_socket,
        workload=config.workload,
        stop_timeout_sec=config.stop_timeout_sec,
    )
    try:
        _run(argv, runner=runner)
        state = _wait_for_active(
            request.unit, timeout_sec=config.start_timeout_sec, runner=runner
        )
        _wait_for_started_marker(
            config.task_root, request, timeout_sec=config.start_timeout_sec
        )
        uid = read_process_uid(state.main_pid)
    except BaseException:
        _safe_stop(request.unit, runner=runner)
        raise

    return {
        "unit": request.unit,
        "active_state": state.active_state,
        "started": True,
        "uid": uid,
        "main_pid": state.main_pid,
        "attestation": task_attestation(request, config, uid=uid, key=key),
    }


def status(
    request: object, config: object, *, runner: Runner = run_checked
) -> dict[str, object]:
    """Return whitelisted unit facts plus task-directory markers."""

    request = _require_sandbox_request(request)
    config = _validate_install_config(config)
    state = inspect_unit(request.unit, runner=runner)

    uid: int | None = None
    if state.main_pid > 0:
        try:
            uid = read_process_uid(state.main_pid)
        except HelperError:
            uid = None

    return {
        "unit": request.unit,
        "uid": uid,
        "unit_state": unit_state_document(state),
        "task": _task_facts(config.task_root, request),
        "observed_at": _iso_utc(_utcnow()),
    }


def stop(
    request: object, config: object, *, runner: Runner = run_checked
) -> dict[str, object]:
    """SIGTERM, wait ``TimeoutStopSec``, SIGKILL; confirm only when gone."""

    request = _require_sandbox_request(request)
    config = _validate_install_config(config)
    state = inspect_unit(request.unit, runner=runner)

    if state.load_state == "not-found" or _process_gone(state):
        _finalize(request.unit, runner=runner)
        final = inspect_unit(request.unit, runner=runner)
        return _stop_response(request.unit, final)

    _run_quiet(
        [
            SYSTEMCTL,
            "kill",
            "--signal=SIGTERM",
            "--kill-whom=all",
            request.unit,
        ],
        runner=runner,
    )
    final = _wait_until_gone(
        request.unit, timeout_sec=config.stop_timeout_sec, runner=runner
    )
    if final is None:
        _run_quiet(
            [
                SYSTEMCTL,
                "kill",
                "--signal=SIGKILL",
                "--kill-whom=all",
                request.unit,
            ],
            runner=runner,
        )
        final = _wait_until_gone(
            request.unit, timeout_sec=config.stop_timeout_sec, runner=runner
        )
    _run_quiet([SYSTEMCTL, "stop", request.unit], runner=runner)
    if final is None:
        final = inspect_unit(request.unit, runner=runner)
    return _stop_response(request.unit, final)


def cleanup(
    request: object,
    config: object,
    *,
    result_read: bool | None = None,
    runner: Runner = run_checked,
) -> dict[str, object]:
    """Delete only the derived task directory once the unit is gone."""

    request = _require_sandbox_request(request)
    config = _validate_install_config(config)
    declared = request.result_read if result_read is None else result_read
    if declared is not True:
        raise HelperError("result_not_read")

    state = inspect_unit(request.unit, runner=runner)
    if not _process_gone(state):
        raise HelperError("unit_still_running")

    uid = _adapter_uid(config)
    task_dir = config.task_root / request.unit
    _require_task_root(config)
    info = _lstat(task_dir)
    removed = False
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise HelperError("task_dir_invalid")
        if info.st_uid != uid:
            raise HelperError("task_dir_owner_invalid")
        if task_dir.parent != config.task_root:
            raise HelperError("task_dir_invalid")
        try:
            shutil.rmtree(task_dir)
        except OSError:
            raise HelperError("task_dir_remove_failed") from None
        removed = True
    _run_quiet([SYSTEMCTL, "reset-failed", request.unit], runner=runner)
    return {
        "unit": request.unit,
        "removed": removed,
        "process_gone": True,
    }


def attest(
    request: object, config: object, *, runner: Runner = run_checked
) -> dict[str, object]:
    """Inspect the unit, then reissue runtime and (when live) task attestations.

    The runtime attestation is task independent, so ``attest`` must work after
    the token/result were deleted or the task directory was cleaned up: only an
    active unit with a readable UID triggers the strict task-file verification
    and a non-null task attestation.
    """

    request = _require_sandbox_request(request)
    config = _validate_install_config(config)
    _require_runtime_root(config)
    _require_broker_socket(config)
    _require_workload(config)

    key = _read_attestation_key(config)
    state = inspect_unit(request.unit, runner=runner)

    task_document: dict[str, object] | None = None
    if (
        state.active_state == "active"
        and state.main_pid > 0
        and not _process_gone(state)
    ):
        _require_task_root(config)
        task_dir = _require_task_dir(config, request.unit)
        _verify_task_files(request, config, task_dir)
        uid = read_process_uid(state.main_pid)
        task_document = task_attestation(request, config, uid=uid, key=key)

    return {
        "unit": request.unit,
        "runtime_attestation": runtime_attestation(config, key=key),
        "task_attestation": task_document,
        "observed_at": _iso_utc(_utcnow()),
    }


# ---------------------------------------------------------------------------
# operation internals
# ---------------------------------------------------------------------------


def _require_sandbox_request(request: object) -> SandboxRequest:
    if not isinstance(request, SandboxRequest):
        raise HelperError("invalid_request")
    return _validate_request_values(request)


def _require_task_dir_and_runtime(
    request: SandboxRequest, config: InstallConfig
) -> Path:
    _require_runtime_root(config)
    _require_broker_socket(config)
    _require_workload(config)
    _require_task_root(config)
    return _require_task_dir(config, request.unit)


def _require_runtime_root(config: InstallConfig) -> None:
    info = _require_fixed_directory(config.runtime_root, code="runtime_root")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise HelperError("runtime_root_mode_invalid")
    if not _unit_can_read(info, _sandbox_gid(config), execute=True):
        raise HelperError("runtime_root_mode_invalid")


def _require_broker_socket(config: InstallConfig) -> None:
    info = _lstat(config.broker_socket)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise HelperError("broker_socket_invalid")


def _broker_socket_ok(config: InstallConfig) -> bool:
    info = _lstat(config.broker_socket)
    return bool(
        info is not None
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_ISSOCK(info.st_mode)
    )


def _require_workload(config: InstallConfig) -> None:
    info = _lstat(config.workload)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HelperError("workload_invalid")
    if info.st_uid != os.geteuid():
        raise HelperError("workload_owner_invalid")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise HelperError("workload_mode_invalid")
    if not _unit_can_read(info, _sandbox_gid(config), execute=False):
        raise HelperError("workload_mode_invalid")


def _unit_can_read(
    info: os.stat_result, sandbox_gid: int, *, execute: bool
) -> bool:
    """Check read (and optional execute) for the dynamic unit, not for root.

    ``os.access`` is useless here because the helper runs as root, which
    bypasses mode bits.  The unit's primary group is ``agentmgr-hermes``, so
    either the "other" bits or the matching group bits grant access.
    """

    mode = stat.S_IMODE(info.st_mode)
    other = (0o004 if not execute else 0o005)
    group = (0o040 if not execute else 0o050)
    if mode & other == other:
        return True
    return info.st_gid == sandbox_gid and mode & group == group


def _verify_task_files(
    request: SandboxRequest, config: InstallConfig, task_dir: Path
) -> None:
    uid, _ = _adapter_ids(config)
    gid = _sandbox_gid(config)
    _verify_task_file(
        task_dir / "prompt.txt",
        label="prompt",
        expected_sha256=request.prompt_sha256,
        expected_size=request.prompt_bytes,
        max_size=MAX_PROMPT_BYTES,
        uid=uid,
        gid=gid,
    )
    _verify_task_file(
        task_dir / "profile.template.yaml",
        label="profile",
        expected_sha256=request.profile_template_sha256,
        expected_size=None,
        max_size=MAX_PROFILE_TEMPLATE_BYTES,
        uid=uid,
        gid=gid,
    )
    _verify_task_file(
        task_dir / "egress.token",
        label="token",
        expected_sha256=request.egress_token_sha256,
        expected_size=None,
        max_size=MAX_EGRESS_TOKEN_BYTES,
        uid=uid,
        gid=gid,
        exact_mode=TOKEN_FILE_MODE,
    )


def _wait_for_active(
    unit: str, *, timeout_sec: int, runner: Runner
) -> UnitState:
    deadline = _monotonic() + timeout_sec
    while True:
        state = inspect_unit(unit, runner=runner)
        if state.load_state == "not-found":
            raise HelperError("unit_not_found")
        if state.active_state == "active" and state.main_pid > 0:
            return state
        if state.active_state in {"failed", "inactive"}:
            raise HelperError(
                "unit_failed" if state.active_state == "failed" else "unit_not_active"
            )
        if _monotonic() >= deadline:
            raise HelperError("unit_start_timeout")
        _sleep(POLL_INTERVAL_SECONDS)


def _wait_for_started_marker(
    task_root: Path, request: SandboxRequest, *, timeout_sec: int
) -> None:
    deadline = _monotonic() + timeout_sec
    while True:
        if _read_started_marker(task_root, request) is not None:
            return
        if _monotonic() >= deadline:
            raise HelperError("started_marker_timeout")
        _sleep(POLL_INTERVAL_SECONDS)


def _wait_until_gone(
    unit: str, *, timeout_sec: int, runner: Runner
) -> UnitState | None:
    deadline = _monotonic() + timeout_sec
    while True:
        state = inspect_unit(unit, runner=runner)
        if _process_gone(state):
            return state
        if _monotonic() >= deadline:
            return None
        _sleep(POLL_INTERVAL_SECONDS)


def _safe_stop(unit: str, *, runner: Runner) -> None:
    _run_quiet(
        [SYSTEMCTL, "kill", "--signal=SIGTERM", "--kill-whom=all", unit],
        runner=runner,
    )
    _run_quiet([SYSTEMCTL, "stop", unit], runner=runner)
    _run_quiet([SYSTEMCTL, "reset-failed", unit], runner=runner)


def _finalize(unit: str, *, runner: Runner) -> None:
    _run_quiet([SYSTEMCTL, "stop", unit], runner=runner)
    _run_quiet([SYSTEMCTL, "reset-failed", unit], runner=runner)


def _stop_response(unit: str, state: UnitState) -> dict[str, object]:
    gone = _process_gone(state)
    return {
        "unit": unit,
        "unit_state": unit_state_document(state),
        "process_gone": gone,
        "stop_confirmed": gone,
        "observed_at": _iso_utc(_utcnow()),
    }


def _heartbeat_value(document: Mapping[str, object]) -> str | None:
    value = document.get("heartbeat_at", document.get("at"))
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise HelperError("heartbeat_invalid")
    if CONTROL_CHARACTERS.search(value):
        raise HelperError("heartbeat_invalid")
    return value


def _exit_values(document: Mapping[str, object]) -> tuple[int | None, bool, bool]:
    exit_code = document.get("exit_code")
    if exit_code is not None and (type(exit_code) is not int or not -256 <= exit_code <= 256):
        raise HelperError("exit_invalid")
    timed_out = document.get("timed_out", False)
    cancelled = document.get("cancelled", False)
    if type(timed_out) is not bool or type(cancelled) is not bool:
        raise HelperError("exit_invalid")
    return exit_code, timed_out, cancelled


def _result_present_fd(root_fd: int) -> bool:
    """Return whether ``out/result.txt`` is a regular file (fd-relative)."""

    out_fd = _open_child_directory(root_fd, "out", "out_dir")
    if out_fd is None:
        return False
    try:
        try:
            descriptor = os.open("result.txt", _FILE_FLAGS, dir_fd=out_fd)
        except FileNotFoundError:
            return False
        except OSError:
            # A symlink or otherwise unsafe artifact is never "present"; the
            # adapter's artifact reader also fails closed on it.
            return False
        try:
            return stat.S_ISREG(os.fstat(descriptor).st_mode)
        finally:
            os.close(descriptor)
    finally:
        os.close(out_fd)


def _task_facts(
    task_root: Path, request: SandboxRequest
) -> dict[str, object]:
    """Read the task-directory markers through verified directory fds."""

    facts: dict[str, object] = {
        "started": False,
        "heartbeat_at": None,
        "exit_code": None,
        "timed_out": False,
        "cancelled": False,
        "result_present": False,
    }
    task_fd = _open_unit_directory(task_root, request.unit)
    if task_fd is None:
        return facts
    try:
        status_fd = _open_child_directory(task_fd, "status", "status_dir")
        if status_fd is not None:
            try:
                document = _read_json_at(status_fd, "started.json", "started_marker")
                if document is not None:
                    _validate_started_document(document, request)
                    facts["started"] = True
                heartbeat = _read_json_at(status_fd, "heartbeat.json", "heartbeat")
                if heartbeat is not None:
                    facts["heartbeat_at"] = _heartbeat_value(heartbeat)
                exit_document = _read_json_at(status_fd, "exit.json", "exit")
                if exit_document is not None:
                    exit_code, timed_out, cancelled = _exit_values(exit_document)
                    facts["exit_code"] = exit_code
                    facts["timed_out"] = timed_out
                    facts["cancelled"] = cancelled
            finally:
                os.close(status_fd)
        facts["result_present"] = _result_present_fd(task_fd)
    finally:
        os.close(task_fd)
    return facts


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# stdin protocol
# ---------------------------------------------------------------------------


def run_request(
    payload: object,
    *,
    config: InstallConfig | None = None,
    runner: Runner = run_checked,
) -> dict[str, object]:
    """Dispatch one decoded request object (the only accepted entry shape)."""

    if not isinstance(payload, Mapping):
        raise HelperError("invalid_request")
    request = validate_request(payload)
    resolved = (
        load_install_config(DEFAULT_INSTALL_CONFIG) if config is None else config
    )
    operation = request.operation
    if operation == "prepare":
        result = prepare(request, resolved)
    elif operation == "launch":
        result = launch(request, resolved, runner=runner)
    elif operation == "status":
        result = status(request, resolved, runner=runner)
    elif operation == "stop":
        result = stop(request, resolved, runner=runner)
    elif operation == "cleanup":
        result = cleanup(
            request,
            resolved,
            result_read=payload.get("result_read") is True,
            runner=runner,
        )
    elif operation == "attest":
        result = attest(request, resolved, runner=runner)
    else:  # pragma: no cover - validate_request already rejected it
        raise HelperError("invalid_operation")
    return {"ok": True, "operation": operation, **result}


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
        if not text.strip():
            raise HelperError("invalid_request")
        payload = json.loads(text)
        response = run_request(payload)
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
