"""Trusted launcher for Hermes managed execution (Issue #97, Task 9).

The launcher is the production entry point for the #96
:class:`~app.tasks.execution.RuntimeDispatcher` with the real Hermes managed
adapter.  It:

* reads one **trusted, secret-free** JSON configuration supplied by the operator
  with ``--config`` (there is no default and no discovery);
* validates the fixed deployment surface (the sudoers wrapper path, the control
  socket directory, the unit-invisible key root) and every trusted value before
  anything is constructed;
* requires a provisioned runtime-attestation record, the two HMAC key files and
  the service principal to be present, and fails closed with a stable code when
  they are not;
* resolves a *service* principal through the existing
  :mod:`app.governance.principal` lookup (never a person, task payload or
  client-supplied identity);
* builds :class:`~app.runtime.hermes_managed.HermesManagedAdapter` with the
  fixed root-helper supervisor and reuses #96's dispatcher semantics unchanged;
* owns the session lifecycle and runs either one pass (``--once``) or a loop
  (``--loop``) with a fresh session per iteration until SIGTERM/SIGINT.

There is exactly one runtime: the managed Hermes sandbox.  The advisory
``/v1/runs`` adapter is not imported, and no other runtime is substituted when
configuration is missing or invalid.  Only stable codes, a
``DispatcherResult`` outcome or an exception *type* is printed; the config path,
prompt, token, key, task payload and runtime paths never reach stdout/stderr.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import re
import signal
import stat
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from app.runtime.hermes_egress import MAX_EGRESS_TTL_SECONDS
from app.runtime.hermes_managed import (
    MAX_CPU_QUOTA_PERCENT,
    MAX_MEMORY_MAX_MB,
    MAX_PROMPT_MAX_BYTES,
    MAX_RUNTIME_MAX_SEC,
    MAX_TASKS_MAX,
    MIN_RUNTIME_MAX_SEC,
    MODEL_NAME_PATTERN,
    RUNTIME_ATTESTATION_SCHEMA,
    AttestationError,
    HelperSandboxSupervisor,
    HermesManagedAdapter,
    HermesManagedConfig,
    RuntimeAttestation,
)
from app.tasks.execution import DispatcherConfig, RuntimeDispatcher

__all__ = [
    "ATTESTATION_KEY_ROOT",
    "BROKER_SOCKET",
    "CONFIG_OWNER_UID",
    "CONFIG_SCHEMA",
    "CONTROL_DIR",
    "HELPER_OWNER_UID",
    "HermesCliError",
    "KEY_OWNER_UID",
    "LauncherConfig",
    "REVOCATION_DIR",
    "RUNTIME_ROOT",
    "SUDOERS_WRAPPER",
    "TASK_ROOT",
    "load_config",
    "load_launcher_config",
    "main",
]

# ---------------------------------------------------------------------------
# fixed deployment surface (mirrored by provision/verify scripts)
# ---------------------------------------------------------------------------

#: Schema of the trusted launcher JSON document.
CONFIG_SCHEMA = "agentmgr.hermes.managed-cli.v1"

#: The only helper path the sudoers rule authorises.  The launcher never accepts
#: an operator-supplied helper path.
SUDOERS_WRAPPER = Path("/usr/local/libexec/agentmgr-hermes-exec")

#: Root-only directory that every sandbox unit masks with ``InaccessiblePaths``.
#: Both HMAC key files and the runtime-attestation record must live below it.
ATTESTATION_KEY_ROOT = Path("/etc/agentmgr")

#: The trusted launcher configuration must be owned by root (installed as
#: ``root:<adapter-private-group> 0440``).  The unprivileged unit-test fixture
#: overrides this to its own uid, exactly like the root-helper tests; production
#: never changes it.
CONFIG_OWNER_UID = 0

#: The fixed deployment roots.  The launcher accepts no other absolute path for
#: any of them: a task root, runtime root, revocation store or control socket
#: that differs from provisioning is a deployment error, not an operator choice.
RUNTIME_ROOT = Path("/var/lib/agentmgr-hermes-runtime")
TASK_ROOT = Path("/var/lib/agentmgr-hermes-tasks")
REVOCATION_DIR = Path("/var/lib/agentmgr-hermes-revoked")

#: Host-visible broker control directory and socket (design §9.1).  The socket
#: cannot live under ``/run``, ``/tmp`` or ``/var/tmp`` because a task unit
#: replaces ``/run`` and ``/tmp`` and cannot bind a source from them.
CONTROL_DIR = Path("/var/lib/agentmgr-hermes-control")
BROKER_SOCKET = CONTROL_DIR / "egress.sock"

#: Ownership seams for the unprivileged unit-test fixture.  Production requires
#: a root-owned sudoers wrapper and root-owned HMAC key files; the fixture moves
#: these to its own uid exactly like the root-helper tests.
HELPER_OWNER_UID = 0
KEY_OWNER_UID = 0

#: Fixed local loopback proxy template rendered into the per-task profile.
_DEFAULT_PROXY_TEMPLATE = "http://127.0.0.1:{port}/v1"
_RENDERED_PROXY_TEMPLATE = "http://127.0.0.1:__PORT__/v1"

#: Only the ``/v1`` OpenAI-compatible prefix is allowlisted by the broker.
_DEFAULT_PATH_PREFIX = "/v1"

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_ATTESTATION_BYTES = 64 * 1024
_MIN_KEY_BYTES = 32
_MAX_KEY_BYTES = 4_096
_MAX_TIMEOUT_SEC = 300
_MAX_POLL_SECONDS = 300.0
_MAX_BACKOFF_SECONDS = 3_600.0
_MAX_LEASE_SECONDS = 86_400

_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_REF_RE = re.compile(r"^evidence:[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$")
_RUNTIME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUNTIME_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SERVICE_IDENTITY_RE = re.compile(r"^svc_[1-9][0-9]{0,18}$")
_PROVIDER_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$"
)
_PATH_PREFIX_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,127}$")
_CONTROL_CHARACTERS_RE = re.compile(r"[\x00-\x1f\x7f]")

#: A field with one of these names would carry a secret into the launcher
#: configuration.  The provider credential belongs to the host broker only.
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "credential",
        "credentials",
        "password",
        "passwd",
        "provider_key",
        "secret",
        "token",
    }
)

_CONFIG_REQUIRED = frozenset(
    {
        "schema",
        "service_identity_id",
        "helper_path",
        "runtime_root",
        "runtime_root_hash",
        "task_root",
        "control_dir",
        "broker_socket",
        "revocation_dir",
        "attestation_key_path",
        "egress_token_key_path",
        "runtime_attestation_path",
        "evidence_ref",
        "runtime_name",
        "runtime_version",
        "model_name",
        "egress_provider_host",
        "worker_id",
    }
)
_CONFIG_OPTIONAL = frozenset(
    {
        "local_proxy_base_url_template",
        "egress_path_prefix",
        "runtime_max_sec",
        "memory_max_mb",
        "cpu_quota_percent",
        "tasks_max",
        "stop_timeout_sec",
        "prompt_max_bytes",
        "result_max_bytes",
        "egress_token_ttl_seconds",
        "lease_seconds",
        "poll_interval",
        "health_backoff_seconds",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Defaults are read from :class:`HermesManagedConfig` itself so the loader and
#: the adapter cannot drift.
_CONFIG_DEFAULTS = MappingProxyType(
    {
        field.name: field.default
        for field in fields(HermesManagedConfig)
        if field.default is not MISSING
    }
)

#: Stable stderr prefixes.  They contain no path, secret or runtime value.
NO_CONFIGURATION = "#97 managed execution configuration not supplied (use --config)"
CONFIGURATION_REJECTED = "managed execution configuration rejected"
MODE_REQUIRED = "managed execution mode must be exactly one of --once or --loop"


class HermesCliError(ValueError):
    """A fail-closed launcher error carrying only a stable reason code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if (
            not isinstance(code, str)
            or not code
            or _CONTROL_CHARACTERS_RE.search(code)
            or " " in code
        ):
            code = "launcher_invalid"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LauncherConfig:
    """The validated trusted configuration plus the service identity.

    :class:`HermesManagedConfig` is the frozen adapter contract and has no
    service-identity field; the launcher keeps the resolved identity shape next
    to it instead of widening the Task 5 dataclass.
    """

    managed: HermesManagedConfig
    service_identity_id: str


class _Runner(Protocol):
    """The narrow surface the launcher drives (satisfied by #96's dispatcher)."""

    def run_once(self) -> Any: ...

    def run_forever(self, stop_event: threading.Event) -> None: ...


DispatcherFactory = Callable[[HermesManagedConfig], _Runner | None]


# ---------------------------------------------------------------------------
# trusted configuration
# ---------------------------------------------------------------------------


def _is_inside(path: Path, root: Path) -> bool:
    if path == root:
        return True
    try:
        return path.is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or _is_inside(first, second) or _is_inside(second, first)


def _safe_path(value: object, code: str) -> Path:
    """Return a clean absolute path outside the repository, or raise ``code``."""

    if not isinstance(value, str) or not value:
        raise HermesCliError(code)
    if _CONTROL_CHARACTERS_RE.search(value):
        raise HermesCliError(code)
    path = Path(value)
    if not path.is_absolute() or path == Path("/"):
        raise HermesCliError(code)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise HermesCliError(code)
    if _is_inside(path, _REPO_ROOT):
        # Deployment state never lives inside a checkout: a worktree path is not
        # a durable, root-owned location.
        raise HermesCliError(code)
    return path


def _read_bounded_file(path: Path, *, limit: int, code: str) -> bytes:
    """Open a regular file without following symlinks and return its bytes."""

    try:
        info = os.lstat(path)
    except OSError:
        raise HermesCliError(code) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HermesCliError(code)
    if info.st_size <= 0 or info.st_size > limit:
        raise HermesCliError(code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        raise HermesCliError(code) from None
    try:
        try:
            current = os.fstat(descriptor)
        except OSError:  # pragma: no cover - race guard
            raise HermesCliError(code) from None
        if not stat.S_ISREG(current.st_mode):
            raise HermesCliError(code)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            try:
                chunk = os.read(descriptor, min(65_536, remaining))
            except OSError:
                raise HermesCliError(code) from None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > limit:
        raise HermesCliError(code)
    return raw


def _read_config_document(path: Path) -> dict[str, object]:
    """Read the trusted config, enforcing the private root-owned file shape."""

    config_path = Path(path)
    try:
        info = os.lstat(config_path)
    except OSError:
        raise HermesCliError("config_unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HermesCliError("config_invalid")
    if info.st_uid != CONFIG_OWNER_UID:
        raise HermesCliError("config_owner_invalid")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o007 or mode & 0o020:
        # The trusted configuration is never world-accessible and never
        # group-writable; a read-only group variant (0440/0640) is allowed.
        raise HermesCliError("config_mode_invalid")
    if info.st_size <= 0 or info.st_size > _MAX_CONFIG_BYTES:
        raise HermesCliError("config_invalid")

    raw = _read_bounded_file(
        config_path, limit=_MAX_CONFIG_BYTES, code="config_unavailable"
    )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HermesCliError("config_invalid") from None
    if not isinstance(document, dict):
        raise HermesCliError("config_invalid")
    return document


def _require_text(
    document: dict[str, object], name: str, *, code: str, pattern: re.Pattern[str]
) -> str:
    value = document[name]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise HermesCliError(code)
    return value


def _require_int(
    document: dict[str, object],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = document.get(name, _CONFIG_DEFAULTS[name])
    if type(value) is not int or not minimum <= value <= maximum:
        raise HermesCliError("config_limit_invalid")
    return value


def _require_number(
    document: dict[str, object],
    name: str,
    *,
    minimum: float,
    maximum: float,
    allow_zero: bool = False,
) -> float:
    value = document.get(name, _CONFIG_DEFAULTS[name])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HermesCliError("config_limit_invalid")
    number = float(value)
    if number < minimum or number > maximum:
        raise HermesCliError("config_limit_invalid")
    if not allow_zero and number == 0:
        raise HermesCliError("config_limit_invalid")
    return number


def _validate_key_path(value: object) -> Path:
    path = _safe_path(value, "config_key_path_invalid")
    if not _is_inside(path, ATTESTATION_KEY_ROOT) or path == ATTESTATION_KEY_ROOT:
        # The HMAC keys and the attestation record must stay in the masked,
        # root-only directory that every sandbox unit cannot see.
        raise HermesCliError("config_key_path_invalid")
    return path


def load_launcher_config(path: Path) -> LauncherConfig:
    """Load and validate the trusted launcher document.

    Every failure is raised as :class:`HermesCliError` with a stable code and no
    value interpolation: the caller can print the code without leaking a path,
    secret or payload.
    """

    document = _read_config_document(Path(path))

    secret_fields = set(document) & _SECRET_FIELD_NAMES
    if secret_fields:
        raise HermesCliError("config_secret_field")
    if set(document) - _CONFIG_REQUIRED - _CONFIG_OPTIONAL:
        raise HermesCliError("config_unknown_field")
    if _CONFIG_REQUIRED - set(document):
        raise HermesCliError("config_missing_field")
    if document["schema"] != CONFIG_SCHEMA:
        raise HermesCliError("config_invalid")

    service_identity_id = _require_text(
        document,
        "service_identity_id",
        code="config_service_identity_invalid",
        pattern=_SERVICE_IDENTITY_RE,
    )

    if document["helper_path"] != str(SUDOERS_WRAPPER):
        # The sudoers rule authorises exactly this path.  Any other path would
        # either bypass the rule or run an unprivileged helper.
        raise HermesCliError("config_helper_path_invalid")

    runtime_root = _safe_path(document["runtime_root"], "config_unsafe_path")
    task_root = _safe_path(document["task_root"], "config_unsafe_path")
    revocation_dir = _safe_path(document["revocation_dir"], "config_unsafe_path")
    if (
        runtime_root != RUNTIME_ROOT
        or task_root != TASK_ROOT
        or revocation_dir != REVOCATION_DIR
    ):
        raise HermesCliError("config_unsafe_path")

    runtime_root_hash = document["runtime_root_hash"]
    if (
        not isinstance(runtime_root_hash, str)
        or _SHA256_REF_RE.fullmatch(runtime_root_hash) is None
    ):
        raise HermesCliError("config_runtime_hash_invalid")

    control_dir = document["control_dir"]
    broker_socket = document["broker_socket"]
    if control_dir != str(CONTROL_DIR) or broker_socket != str(BROKER_SOCKET):
        raise HermesCliError("config_control_path_invalid")

    attestation_key_path = _validate_key_path(document["attestation_key_path"])
    egress_token_key_path = _validate_key_path(document["egress_token_key_path"])
    runtime_attestation_path = _validate_key_path(
        document["runtime_attestation_path"]
    )
    if len(
        {
            attestation_key_path,
            egress_token_key_path,
            runtime_attestation_path,
        }
    ) != 3:
        raise HermesCliError("config_key_path_invalid")

    evidence_ref = _require_text(
        document,
        "evidence_ref",
        code="config_evidence_ref_invalid",
        pattern=_EVIDENCE_REF_RE,
    )
    runtime_name = _require_text(
        document,
        "runtime_name",
        code="config_runtime_invalid",
        pattern=_RUNTIME_NAME_RE,
    )
    runtime_version = _require_text(
        document,
        "runtime_version",
        code="config_runtime_invalid",
        pattern=_RUNTIME_VERSION_RE,
    )
    model_name = _require_text(
        document,
        "model_name",
        code="config_model_invalid",
        pattern=MODEL_NAME_PATTERN,
    )
    provider_host = _require_text(
        document,
        "egress_provider_host",
        code="config_provider_invalid",
        pattern=_PROVIDER_HOST_RE,
    )
    worker_id = _require_text(
        document,
        "worker_id",
        code="config_worker_invalid",
        pattern=_WORKER_ID_RE,
    )

    proxy_template = document.get(
        "local_proxy_base_url_template", _DEFAULT_PROXY_TEMPLATE
    )
    if (
        not isinstance(proxy_template, str)
        or proxy_template.count("{port}") != 1
        or proxy_template.replace("{port}", "") != _RENDERED_PROXY_TEMPLATE.replace(
            "__PORT__", ""
        )
    ):
        raise HermesCliError("config_proxy_invalid")

    path_prefix = document.get("egress_path_prefix", _DEFAULT_PATH_PREFIX)
    if (
        not isinstance(path_prefix, str)
        or path_prefix != _DEFAULT_PATH_PREFIX
        or _PATH_PREFIX_RE.fullmatch(path_prefix) is None
    ):
        raise HermesCliError("config_path_prefix_invalid")

    # The four fixed roots must remain distinct even if the constants are
    # changed together (defence in depth for a future deployment layout).
    for value in (task_root, CONTROL_DIR, revocation_dir):
        if _overlaps(runtime_root, value):
            raise HermesCliError("config_unsafe_path")
    if _overlaps(task_root, CONTROL_DIR) or _overlaps(task_root, revocation_dir):
        raise HermesCliError("config_unsafe_path")
    if _overlaps(revocation_dir, CONTROL_DIR):
        raise HermesCliError("config_unsafe_path")

    runtime_max_sec = _require_int(
        document,
        "runtime_max_sec",
        minimum=MIN_RUNTIME_MAX_SEC,
        maximum=MAX_RUNTIME_MAX_SEC,
    )
    memory_max_mb = _require_int(
        document, "memory_max_mb", minimum=1, maximum=MAX_MEMORY_MAX_MB
    )
    cpu_quota_percent = _require_int(
        document, "cpu_quota_percent", minimum=1, maximum=MAX_CPU_QUOTA_PERCENT
    )
    tasks_max = _require_int(
        document, "tasks_max", minimum=1, maximum=MAX_TASKS_MAX
    )
    stop_timeout_sec = _require_int(
        document, "stop_timeout_sec", minimum=1, maximum=_MAX_TIMEOUT_SEC
    )
    prompt_max_bytes = _require_int(
        document, "prompt_max_bytes", minimum=1, maximum=MAX_PROMPT_MAX_BYTES
    )
    result_max_bytes = _require_int(
        document, "result_max_bytes", minimum=1, maximum=MAX_PROMPT_MAX_BYTES
    )
    egress_token_ttl_seconds = _require_int(
        document,
        "egress_token_ttl_seconds",
        minimum=1,
        maximum=MAX_EGRESS_TTL_SECONDS,
    )
    lease_seconds = _require_int(
        document, "lease_seconds", minimum=1, maximum=_MAX_LEASE_SECONDS
    )
    poll_interval = _require_number(
        document, "poll_interval", minimum=0.0, maximum=_MAX_POLL_SECONDS
    )
    health_backoff_seconds = _require_number(
        document,
        "health_backoff_seconds",
        minimum=0.0,
        maximum=_MAX_BACKOFF_SECONDS,
        allow_zero=True,
    )

    managed = HermesManagedConfig(
        helper_path=SUDOERS_WRAPPER,
        runtime_root=runtime_root,
        runtime_root_hash=runtime_root_hash,
        task_root=task_root,
        broker_socket=BROKER_SOCKET,
        attestation_key_path=attestation_key_path,
        egress_token_key_path=egress_token_key_path,
        runtime_attestation_path=runtime_attestation_path,
        evidence_ref=evidence_ref,
        runtime_name=runtime_name,
        runtime_version=runtime_version,
        model_name=model_name,
        egress_provider_host=provider_host,
        worker_id=worker_id,
        local_proxy_base_url_template=_DEFAULT_PROXY_TEMPLATE,
        egress_path_prefix=_DEFAULT_PATH_PREFIX,
        runtime_max_sec=runtime_max_sec,
        memory_max_mb=memory_max_mb,
        cpu_quota_percent=cpu_quota_percent,
        tasks_max=tasks_max,
        stop_timeout_sec=stop_timeout_sec,
        prompt_max_bytes=prompt_max_bytes,
        result_max_bytes=result_max_bytes,
        egress_token_ttl_seconds=egress_token_ttl_seconds,
        lease_seconds=lease_seconds,
        poll_interval=poll_interval,
        health_backoff_seconds=health_backoff_seconds,
        revocation_dir=revocation_dir,
    )
    return LauncherConfig(managed=managed, service_identity_id=service_identity_id)


def load_config(path: Path) -> HermesManagedConfig:
    """Load the trusted adapter configuration (the service id is validated too)."""

    return load_launcher_config(path).managed


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def _require_directory(path: Path, code: str) -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise HermesCliError(code) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HermesCliError(code)
    if not os.access(path, os.R_OK | os.X_OK):
        raise HermesCliError(code)


def _sandbox_gid() -> int | None:
    """Return the fixed sandbox group's gid, or ``None`` when it is absent."""

    try:
        return grp.getgrnam("agentmgr-hermes").gr_gid
    except KeyError:
        return None


def _require_key_file(path: Path, code: str) -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise HermesCliError(code) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HermesCliError(code)
    if info.st_uid != KEY_OWNER_UID:
        # The HMAC keys are root-owned; the adapter reads them through its
        # private group, never by owning them.
        raise HermesCliError(code)
    sandbox_gid = _sandbox_gid()
    if sandbox_gid is not None and info.st_gid == sandbox_gid:
        # A key readable by the sandbox group would let every task forge
        # attestations or egress tokens.
        raise HermesCliError(code)
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o007 or mode & 0o020:
        # A world-readable or group-writable HMAC key is a deployment error.
        raise HermesCliError(code)
    if not _MIN_KEY_BYTES <= info.st_size <= _MAX_KEY_BYTES:
        raise HermesCliError(code)
    if not os.access(path, os.R_OK):
        raise HermesCliError(code)


def _helper_is_executable(config: HermesManagedConfig) -> bool:
    """Return whether the fixed root-owned sudoers wrapper is installed.

    A group- or world-writable wrapper could be replaced by the adapter (or any
    local process in that group) and would make the sudoers rule meaningless, so
    it fails closed even when it is executable.
    """

    path = Path(config.helper_path)
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != HELPER_OWNER_UID or mode & 0o022 or not mode & 0o111:
        return False
    return os.access(path, os.X_OK)


def _verify_attestation_record(config: HermesManagedConfig) -> None:
    """Check the provisioned runtime-attestation record binds this config.

    The record is the operator-visible provisioning snapshot.  Its HMAC and
    validity window are *not* checked here: the live helper attestation is
    verification authority and :class:`HermesManagedAdapter` re-verifies it
    (signature, expiry and sandbox facts) before any capability or submit.  This
    preflight only proves the deployment wrote a record for the same runtime
    name/version, runtime root hash and evidence reference, so a stale or
    foreign record cannot be paired with this configuration.
    """

    try:
        raw = _read_bounded_file(
            Path(config.runtime_attestation_path),
            limit=_MAX_ATTESTATION_BYTES,
            code="attestation_unavailable",
        )
    except HermesCliError:
        raise HermesCliError("attestation_unavailable") from None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HermesCliError("attestation_invalid") from None
    if not isinstance(document, dict):
        raise HermesCliError("attestation_invalid")
    try:
        attestation = RuntimeAttestation.from_document(document)
    except (AttestationError, TypeError, ValueError):
        raise HermesCliError("attestation_invalid") from None
    if (
        attestation.schema != RUNTIME_ATTESTATION_SCHEMA
        or attestation.runtime_name != config.runtime_name
        or attestation.runtime_version != config.runtime_version
        or attestation.runtime_root_hash != config.runtime_root_hash
        or attestation.evidence_ref != config.evidence_ref
    ):
        raise HermesCliError("attestation_invalid")


def _preflight(config: HermesManagedConfig, *, require_helper: bool) -> None:
    """Validate the provisioned host state before any dispatcher is built."""

    _verify_attestation_record(config)
    _require_key_file(config.attestation_key_path, "attestation_key_unavailable")
    _require_key_file(config.egress_token_key_path, "egress_token_key_unavailable")
    _require_directory(config.runtime_root, "runtime_root_unavailable")
    _require_directory(config.task_root, "task_root_unavailable")
    _require_directory(BROKER_SOCKET.parent, "control_dir_unavailable")
    if config.revocation_dir is not None:
        _require_directory(config.revocation_dir, "revocation_dir_unavailable")
    # An injected trusted dispatcher brings its own adapter; the production path
    # additionally requires the installed sudoers wrapper before touching the DB.
    if require_helper and not _helper_is_executable(config):
        raise HermesCliError("helper_unavailable")


# ---------------------------------------------------------------------------
# production dispatcher construction
# ---------------------------------------------------------------------------


def _session_factory() -> Any:
    """Open one fresh SQLAlchemy session (trusted launcher value, never input)."""

    from app.db.session import SessionLocal

    return SessionLocal()


def _resolve_service(session: Any, service_identity_id: str) -> Any:
    """Resolve the dedicated *service* principal through the existing lookup."""

    from app.governance.principal import resolve_service_principal

    return resolve_service_principal(session, service_identity_id)


class _DispatcherRunner:
    """Own the dispatcher and its initial session for one CLI invocation."""

    def __init__(self, dispatcher: RuntimeDispatcher, session: Any) -> None:
        self._dispatcher = dispatcher
        self._session = session

    def run_once(self) -> Any:
        return self._dispatcher.run_once()

    def run_forever(self, stop_event: threading.Event) -> None:
        self._dispatcher.run_forever(stop_event, session_factory=_session_factory)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - shutdown must not mask the outcome
            pass


def _build_runner(launcher: LauncherConfig) -> _DispatcherRunner:
    """Build the managed adapter and #96 dispatcher with a service principal."""

    config = launcher.managed
    session = _session_factory()
    try:
        principal = _resolve_service(session, launcher.service_identity_id)
        require_active = getattr(principal, "require_active", None)
        if callable(require_active):
            principal = require_active()
        if not getattr(principal, "is_service", False):
            raise HermesCliError("service_principal_invalid")

        adapter = HermesManagedAdapter(config, HelperSandboxSupervisor(config))
        dispatcher_config = DispatcherConfig(
            worker_id=config.worker_id,
            lease_seconds=config.lease_seconds,
            poll_interval=config.poll_interval,
            health_backoff_seconds=config.health_backoff_seconds,
        )
        dispatcher = RuntimeDispatcher(
            adapter,
            dispatcher_config,
            service_principal=principal,
            session=session,
            session_factory=_session_factory,
        )
        return _DispatcherRunner(dispatcher, session)
    except BaseException:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - preserve the original failure
            pass
        raise


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.runtime.hermes_managed_cli",
        description=(
            "Hermes managed execution launcher.  Requires a trusted JSON "
            "configuration; there is no default configuration and no other "
            "runtime is substituted when it is missing."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one claim/submit/drive pass and exit",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="poll until SIGTERM/SIGINT (one task at a time)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="trusted, secret-free launcher configuration",
    )
    return parser


def _install_stop_handlers(stop_event: threading.Event) -> Callable[[], None]:
    """Translate SIGTERM/SIGINT into ``stop_event``; restore handlers on exit."""

    previous: dict[int, Any] = {}

    def _stop(signum: int, frame: object) -> None:
        del signum, frame
        stop_event.set()

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            previous.pop(signum, None)

    def _restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - thread/OS guard
                pass

    return _restore


def _safe_exception_code(exc: BaseException) -> str:
    """Return a stable, non-sensitive code for an unexpected failure.

    Only a governance reason code or the exception type is rendered: never the
    message, config value, path, prompt or token.
    """

    from app.governance.errors import GovernanceError

    if isinstance(exc, GovernanceError):
        return exc.reason_code
    return type(exc).__name__


def main(
    argv: Sequence[str] | None = None,
    *,
    dispatcher_factory: DispatcherFactory | None = None,
) -> int:
    """Run the launcher and return a process exit code.

    ``dispatcher_factory`` is a **trusted** launcher/test injection: it receives
    the validated :class:`HermesManagedConfig` and returns a runner, bypassing
    the database, service-principal lookup and root-helper preflight.  It is the
    only seam; without it the production path is built.  A missing or invalid
    configuration, attestation record, key file, helper or service principal
    exits ``2`` with a stable code; an unexpected pass/loop failure exits ``1``
    with the exception type only.
    """

    try:
        args = _build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # pragma: no cover - argparse already printed
        code = exc.code
        return code if isinstance(code, int) else 2

    if bool(args.once) == bool(args.loop):
        print(MODE_REQUIRED, file=sys.stderr)
        return 2
    if not args.config:
        print(NO_CONFIGURATION, file=sys.stderr)
        return 2

    try:
        launcher = load_launcher_config(Path(args.config))
    except HermesCliError as exc:
        print(f"{CONFIGURATION_REJECTED}: {exc.code}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never print the message
        print(
            f"{CONFIGURATION_REJECTED}: {_safe_exception_code(exc)}",
            file=sys.stderr,
        )
        return 2

    try:
        _preflight(launcher.managed, require_helper=dispatcher_factory is None)
    except HermesCliError as exc:
        print(f"{CONFIGURATION_REJECTED}: {exc.code}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never print the message
        print(
            f"{CONFIGURATION_REJECTED}: {_safe_exception_code(exc)}",
            file=sys.stderr,
        )
        return 2

    runner: _Runner | _DispatcherRunner | None = None
    try:
        if dispatcher_factory is not None:
            runner = dispatcher_factory(launcher.managed)
            if runner is None:
                print(
                    f"{CONFIGURATION_REJECTED}: dispatcher_unavailable",
                    file=sys.stderr,
                )
                return 2
        else:
            runner = _build_runner(launcher)
    except HermesCliError as exc:
        print(f"{CONFIGURATION_REJECTED}: {exc.code}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - stable reason only
        print(
            f"{CONFIGURATION_REJECTED}: {_safe_exception_code(exc)}",
            file=sys.stderr,
        )
        return 2

    try:
        if args.once:
            try:
                result = runner.run_once()
            except Exception as exc:  # noqa: BLE001 - stable reason only
                print(
                    f"dispatch pass failed: {_safe_exception_code(exc)}",
                    file=sys.stderr,
                )
                return 1
            print(result.outcome)
            return 0

        stop_event = threading.Event()
        restore_handlers = _install_stop_handlers(stop_event)
        try:
            runner.run_forever(stop_event)
        except Exception as exc:  # noqa: BLE001 - stable reason only
            print(
                f"dispatch loop failed: {_safe_exception_code(exc)}",
                file=sys.stderr,
            )
            return 1
        finally:
            restore_handlers()
        return 0
    finally:
        close = getattr(runner, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
