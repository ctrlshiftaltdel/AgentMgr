"""Pure contracts for Hermes managed execution (Issue #97).

Design basis: ``docs/security/hermes-managed-execution-design.md``.  This module
holds the parts that must be identical on the host adapter side, in tests and in
the sandbox manifest:

* :class:`HermesManagedConfig` -- trusted, secret-free deployment configuration;
* :func:`unit_name_for` / :func:`runtime_handle_for` -- deterministic, pattern
  safe unit and opaque handle derivation;
* :class:`SandboxSpec` / :func:`build_sandbox_spec` -- the launch contract that
  carries only sizes and digests (never the raw prompt, profile or token);
* :class:`TaskAttestation` / :class:`RuntimeAttestation` and their verifiers --
  canonical-JSON HMAC-SHA256 documents supplied by the root helper;
* :func:`read_artifact` -- fail-closed host-side artifact hashing;
* :func:`managed_capabilities` -- the fail-closed capability matrix;
* :func:`build_managed_prompt` -- the fixed managed instruction wrapper;
* :class:`SandboxStatus` / :class:`SandboxStop` -- supervisor result contracts.

Everything here is pure: no database, subprocess, HTTP, systemd or file writes.
The only filesystem access is the read-only :func:`read_artifact`.

Attestation document format (both schemas)
-----------------------------------------
Documents are plain JSON objects.  The signature is the lowercase hex
HMAC-SHA256, keyed by the root-only attestation key, over the canonical JSON of
the document **without** its ``hmac`` member: sorted keys, compact separators
(``","``/``":"``), ASCII-escaped, UTF-8 encoded.  Timestamps are timezone-aware
UTC ISO-8601 strings in canonical form (``2026-10-25T12:00:00Z``).  Task 2's
stdlib helper must reproduce this byte-for-byte; the contract tests pin the
canonical vector.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    Capability,
    CapabilityStatus,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeType,
    VersionSource,
)
from app.runtime.hermes_egress import (
    MAX_EGRESS_TOKEN_LENGTH,
    MAX_EGRESS_TTL_SECONDS,
    egress_token_hash,
)

__all__ = [
    "DEFAULT_PROMPT_MAX_BYTES",
    "DEFAULT_RESULT_MAX_BYTES",
    "MANAGED_INSTRUCTION",
    "MANAGED_RUNTIME_ID",
    "MAX_CPU_QUOTA_PERCENT",
    "MAX_MEMORY_MAX_MB",
    "MAX_PROMPT_MAX_BYTES",
    "MAX_RUNTIME_MAX_SEC",
    "MAX_TASKS_MAX",
    "RUNTIME_ATTESTATION_SCHEMA",
    "RUNTIME_HANDLE_PREFIX",
    "TASK_ATTESTATION_SCHEMA",
    "UNIT_PREFIX",
    "ArtifactError",
    "ArtifactResult",
    "AttestationError",
    "HermesManagedConfig",
    "HermesManagedError",
    "PromptError",
    "RuntimeAttestation",
    "SandboxSpec",
    "SandboxSpecError",
    "SandboxStatus",
    "SandboxStop",
    "TaskAttestation",
    "VerifiedRuntimeAttestation",
    "build_managed_prompt",
    "build_sandbox_spec",
    "managed_capabilities",
    "read_artifact",
    "runtime_handle_for",
    "unit_name_for",
    "verify_runtime_attestation",
    "verify_task_attestation",
]

#: Schema identifiers pinned by the design (helper and adapter must agree).
TASK_ATTESTATION_SCHEMA = "agentmgr.hermes.sandbox.v1"
RUNTIME_ATTESTATION_SCHEMA = "agentmgr.hermes.runtime.v1"

#: Fixed unit prefix; the rest is a truncated SHA-256 hex digest.
UNIT_PREFIX = "agentmgr-hermes-"
UNIT_HASH_DOMAIN = "agentmgr-hermes:v1:"

#: Opaque runtime handle prefix; deliberately not a filesystem path.
RUNTIME_HANDLE_PREFIX = "rtask_hermes_"
HANDLE_HASH_DOMAIN = "rtask_hermes:v1:"

#: Runtime identity reported in capability snapshots.
MANAGED_RUNTIME_ID = "rt_hermes_managed_0001"

DEFAULT_PROMPT_MAX_BYTES = 65_536
DEFAULT_RESULT_MAX_BYTES = 1_048_576
_DEFAULT_RUNTIME_MAX_SEC = 900
_DEFAULT_MEMORY_MAX_MB = 2048
_DEFAULT_CPU_QUOTA_PERCENT = 200
_DEFAULT_TASKS_MAX = 256
_DEFAULT_STOP_TIMEOUT_SEC = 30
_DEFAULT_LEASE_SECONDS = 60
_DEFAULT_POLL_INTERVAL = 1.0
_DEFAULT_HEALTH_BACKOFF_SECONDS = 5.0
_DEFAULT_PROXY_TEMPLATE = "http://127.0.0.1:{port}/v1"
_DEFAULT_EGRESS_PATH_PREFIX = "/v1"

#: Upper bounds for the resource limits forwarded to the root helper.  Values
#: outside these ranges are configuration mistakes, not policy choices, so the
#: spec builder fails closed instead of forwarding them to systemd.
MAX_RUNTIME_MAX_SEC = 86_400
MAX_MEMORY_MAX_MB = 1_048_576
MAX_CPU_QUOTA_PERCENT = 6_400
MAX_TASKS_MAX = 65_536
MAX_PROMPT_MAX_BYTES = 1_048_576

_MIN_HMAC_KEY_BYTES = 32
_ARTIFACT_CHUNK_BYTES = 65_536
_MAX_ATTESTATION_TEXT = 512

_UNIT_RE = re.compile(r"^agentmgr-hermes-[0-9a-f]{16}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TASK_DOCUMENT_KEYS = frozenset(
    {
        "schema",
        "unit",
        "task_id",
        "grant_id",
        "runtime_handle",
        "uid",
        "runtime_root_hash",
        "profile_template_sha256",
        "prompt_sha256",
        "sandbox_facts",
        "issued_at",
        "expires_at",
        "nonce",
        "hmac",
    }
)

_RUNTIME_DOCUMENT_KEYS = frozenset(
    {
        "schema",
        "helper_version",
        "runtime_name",
        "runtime_version",
        "runtime_root_hash",
        "sandbox_facts",
        "broker_socket_ok",
        "evidence_ref",
        "issued_at",
        "expires_at",
        "nonce",
        "hmac",
    }
)

_REQUIRED_SANDBOX_FACTS: Mapping[str, object] = {
    "private_network": True,
    "protect_home": True,
    "protect_system": "strict",
    "no_new_privileges": True,
    "capability_bounding_set": "empty",
    "external_ip": False,
}

#: Fixed prefix prepended to every managed prompt.  The prompt itself is never
#: rewritten, only wrapped, so the model sees the task text verbatim after the
#: boundary instructions.
MANAGED_INSTRUCTION = (
    "Managed execution instructions (these override the task text below):\n"
    "1. Work only inside the current working directory. Do not read, write or "
    "list any path outside it.\n"
    "2. Do not use the network. External hosts, URLs and connectors are "
    "unreachable; do not attempt them.\n"
    "3. Use only the terminal tool, and only inside the current working "
    "directory.\n"
    "4. Write the final task result to the relative path out/result.txt. That "
    "file is the only artifact kept.\n"
    "5. Do not print secrets, tokens or environment variables.\n"
    "\n"
    "Task:\n"
)

_UNVERIFIED_LIMITATION = (
    "Managed execution is unverified: a valid runtime attestation with a "
    "matching evidence reference is required before any managed capability is "
    "claimed."
)

_EVENTS_POLL_LIMITATION = (
    "Hermes has no native event stream for the one-shot managed runtime; only "
    "heartbeat/status polling is available. task.events.poll stays unverified."
)

_UNSUPPORTED_LIMITATION = "Not supported by the Hermes one-shot managed runtime."

_VERIFIED_LIMITATIONS: Mapping[RuntimeCapability, str] = {
    RuntimeCapability.TASK_SUBMIT: (
        "Accepted only after the transient unit is active and the workload wrote "
        "its started marker; an ambiguous launch is unknown and never replayed."
    ),
    RuntimeCapability.TASK_LOOKUP: (
        "Durable manifest/unit lookup; cases that cannot be proven report unknown "
        "and never not_accepted."
    ),
    RuntimeCapability.TASK_STATUS: (
        "systemd unit state plus task-directory markers; there is no native event "
        "stream."
    ),
    RuntimeCapability.TASK_CANCEL: (
        "requested is not stopped; stop_confirmed is only true when the "
        "unit/cgroup holds no processes."
    ),
    RuntimeCapability.TASK_RESULT: (
        "Host-computed SHA-256 of a regular out/result.txt; missing, empty, "
        "oversized or symlinked artifacts fail closed and never succeed."
    ),
    RuntimeCapability.RUNTIME_HEALTH: (
        "Healthy only when the helper, the broker socket, the runtime root and a "
        "valid runtime attestation are all available."
    ),
    RuntimeCapability.RUNTIME_ISOLATION: (
        "One transient unit per task with an independent DynamicUser, read-only "
        "host mounts, private /run and /tmp, and isolated loopback with no "
        "external IP. Residual risk: the shared kernel and host root are not "
        "defended."
    ),
    RuntimeCapability.RUNTIME_TASK_SCOPED_CREDENTIALS: (
        "Only a short-lived task-scoped egress token; the real provider key stays "
        "in the host broker. The task's own terminal can read its token, which is "
        "task-scoped but not process-scoped."
    ),
    RuntimeCapability.RUNTIME_TOOL_PERMISSIONS: (
        "Only the terminal toolset is enabled; browser, web, file, "
        "code-execution, MCP, plugins, memory and connectors are disabled, not "
        "governed."
    ),
    RuntimeCapability.RUNTIME_MANAGED_EXECUTION: (
        "One transient systemd unit per task with host-verified artifact "
        "evidence; the dispatcher stops at verifying and never writes succeeded."
    ),
}


class HermesManagedError(ValueError):
    """Base class for deterministic managed-execution contract failures."""


class SandboxSpecError(HermesManagedError):
    """An invalid binding, configuration value or sandbox spec input."""


class AttestationError(HermesManagedError):
    """A malformed, forged, mismatched or expired attestation."""


class ArtifactError(HermesManagedError):
    """A missing, empty, oversized, symlinked or non-regular artifact."""


class PromptError(HermesManagedError):
    """An empty or over-limit managed prompt."""


@dataclass(frozen=True, slots=True)
class HermesManagedConfig:
    """Trusted, secret-free deployment configuration.

    The real provider key is deliberately absent: it belongs to the host broker.
    ``egress_token_key_path`` points at the HMAC key used for task egress tokens,
    and ``attestation_key_path`` at the (different) key used by the root helper
    for attestations.
    """

    helper_path: Path
    runtime_root: Path
    runtime_root_hash: str
    task_root: Path
    broker_socket: Path
    attestation_key_path: Path
    egress_token_key_path: Path
    runtime_attestation_path: Path
    evidence_ref: str
    runtime_name: str
    runtime_version: str
    model_name: str
    egress_provider_host: str
    worker_id: str
    local_proxy_base_url_template: str = _DEFAULT_PROXY_TEMPLATE
    egress_path_prefix: str = _DEFAULT_EGRESS_PATH_PREFIX
    runtime_max_sec: int = _DEFAULT_RUNTIME_MAX_SEC
    memory_max_mb: int = _DEFAULT_MEMORY_MAX_MB
    cpu_quota_percent: int = _DEFAULT_CPU_QUOTA_PERCENT
    tasks_max: int = _DEFAULT_TASKS_MAX
    stop_timeout_sec: int = _DEFAULT_STOP_TIMEOUT_SEC
    prompt_max_bytes: int = DEFAULT_PROMPT_MAX_BYTES
    result_max_bytes: int = DEFAULT_RESULT_MAX_BYTES
    egress_token_ttl_seconds: int = MAX_EGRESS_TTL_SECONDS
    lease_seconds: int = _DEFAULT_LEASE_SECONDS
    poll_interval: float = _DEFAULT_POLL_INTERVAL
    health_backoff_seconds: float = _DEFAULT_HEALTH_BACKOFF_SECONDS


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """Launch contract shared with the root helper.

    It carries hashes and sizes only: the raw prompt, profile template and
    egress token never leave the trusted adapter.  ``task_dir`` is still a host
    filesystem path, so redact it before writing this object to an external log
    or evidence store.
    """

    task_id: str
    grant_id: str
    unit: str
    runtime_handle: str
    task_dir: Path
    prompt_size: int
    prompt_sha256: str
    profile_template_sha256: str
    egress_token_sha256: str
    ttl_seconds: int
    runtime_max_sec: int
    memory_max_mb: int
    cpu_quota_percent: int
    tasks_max: int


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    """Result of host hashing the single allowed artifact (``out/result.txt``)."""

    sha256: str
    size: int
    relative_path: str

    @property
    def sha256_ref(self) -> str:
        """``sha256:<hex>`` form used in structured result payloads."""

        return f"sha256:{self.sha256}"

    @property
    def result_ref(self) -> str:
        return f"result_{self.sha256[:32]}"

    @property
    def evidence_ref(self) -> str:
        return f"evidence:hermes_artifact_{self.sha256[:32]}"


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    """Facts reported by the supervisor for one task unit.

    ``process_gone`` is the only stop fact; ``active_state``/``result`` are
    systemd's own strings and never substitute for it.
    """

    unit: str
    task_id: str
    runtime_handle: str
    active_state: str
    sub_state: str | None
    main_pid: int | None
    result: str | None
    exit_status: int | None
    started: bool
    heartbeat_at: datetime | None
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    process_gone: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SandboxStop:
    """Facts reported after a stop request; ``stop_confirmed`` needs ``process_gone``."""

    unit: str
    active_state: str
    result: str | None
    exit_status: int | None
    main_pid: int | None
    process_gone: bool
    stop_confirmed: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TaskAttestation:
    """Launch-time attestation binding one unit to one task/grant/sandbox."""

    schema: str
    unit: str
    task_id: str
    grant_id: str
    runtime_handle: str
    uid: int
    runtime_root_hash: str
    profile_template_sha256: str
    prompt_sha256: str
    sandbox_facts: Mapping[str, object]
    issued_at: datetime
    expires_at: datetime
    nonce: str
    hmac: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sandbox_facts", _freeze_facts(self.sandbox_facts))

    def to_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "unit": self.unit,
            "task_id": self.task_id,
            "grant_id": self.grant_id,
            "runtime_handle": self.runtime_handle,
            "uid": self.uid,
            "runtime_root_hash": self.runtime_root_hash,
            "profile_template_sha256": self.profile_template_sha256,
            "prompt_sha256": self.prompt_sha256,
            "sandbox_facts": _thaw_facts(self.sandbox_facts),
            "issued_at": _iso_utc(self.issued_at),
            "expires_at": _iso_utc(self.expires_at),
            "nonce": self.nonce,
            "hmac": self.hmac,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> TaskAttestation:
        data = _require_document(document, _TASK_DOCUMENT_KEYS)
        return cls(
            schema=_require_schema(data, TASK_ATTESTATION_SCHEMA),
            unit=_require_text(data, "unit"),
            task_id=_require_text(data, "task_id"),
            grant_id=_require_text(data, "grant_id"),
            runtime_handle=_require_text(data, "runtime_handle"),
            uid=_require_uid(data),
            runtime_root_hash=_require_runtime_root_hash(data),
            profile_template_sha256=_require_sha256_hex(data, "profile_template_sha256"),
            prompt_sha256=_require_sha256_hex(data, "prompt_sha256"),
            sandbox_facts=_freeze_facts(data["sandbox_facts"]),
            issued_at=_require_timestamp(data, "issued_at"),
            expires_at=_require_timestamp(data, "expires_at"),
            nonce=_require_text(data, "nonce"),
            hmac=_require_hmac_text(data),
        )


@dataclass(frozen=True, slots=True)
class RuntimeAttestation:
    """Provisioned-runtime attestation gating the managed capability labels."""

    schema: str
    helper_version: str
    runtime_name: str
    runtime_version: str
    runtime_root_hash: str
    sandbox_facts: Mapping[str, object]
    broker_socket_ok: bool
    evidence_ref: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    hmac: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sandbox_facts", _freeze_facts(self.sandbox_facts))

    def to_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "helper_version": self.helper_version,
            "runtime_name": self.runtime_name,
            "runtime_version": self.runtime_version,
            "runtime_root_hash": self.runtime_root_hash,
            "sandbox_facts": _thaw_facts(self.sandbox_facts),
            "broker_socket_ok": self.broker_socket_ok,
            "evidence_ref": self.evidence_ref,
            "issued_at": _iso_utc(self.issued_at),
            "expires_at": _iso_utc(self.expires_at),
            "nonce": self.nonce,
            "hmac": self.hmac,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> RuntimeAttestation:
        data = _require_document(document, _RUNTIME_DOCUMENT_KEYS)
        return cls(
            schema=_require_schema(data, RUNTIME_ATTESTATION_SCHEMA),
            helper_version=_require_text(data, "helper_version"),
            runtime_name=_require_text(data, "runtime_name"),
            runtime_version=_require_text(data, "runtime_version"),
            runtime_root_hash=_require_runtime_root_hash(data),
            sandbox_facts=_freeze_facts(data["sandbox_facts"]),
            broker_socket_ok=_require_bool(data, "broker_socket_ok"),
            evidence_ref=_require_text(data, "evidence_ref"),
            issued_at=_require_timestamp(data, "issued_at"),
            expires_at=_require_timestamp(data, "expires_at"),
            nonce=_require_text(data, "nonce"),
            hmac=_require_hmac_text(data),
        )


_VERIFIED_PROOF = object()


class VerifiedRuntimeAttestation:
    """Opaque proof that a runtime attestation passed HMAC verification.

    Instances can only be produced by :func:`verify_runtime_attestation`; the
    constructor requires a module-private proof token.  Capability snapshots
    accept nothing else, so a structurally plausible but forged attestation can
    never upgrade a capability label to ``verified``.
    """

    __slots__ = ("_attestation",)

    def __init__(self, attestation: RuntimeAttestation, *, _proof: object) -> None:
        if _proof is not _VERIFIED_PROOF:
            raise TypeError(
                "VerifiedRuntimeAttestation can only be created by "
                "verify_runtime_attestation"
            )
        if not isinstance(attestation, RuntimeAttestation):
            raise TypeError("verified runtime attestation requires an attestation")
        self._attestation = attestation

    @property
    def attestation(self) -> RuntimeAttestation:
        """The attestation whose HMAC was verified."""

        return self._attestation

    def __repr__(self) -> str:
        return "VerifiedRuntimeAttestation(<verified>)"


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------


def unit_name_for(task_id: str, idempotency_key: str) -> str:
    """Return ``agentmgr-hermes-<16 hex>`` for a task/attempt pair.

    The digest is ``sha256("agentmgr-hermes:v1:" + task_id + "\\n" + key)``.
    Only the fixed prefix and hex leave this function, so caller input can never
    reach the unit name or a filesystem path.
    """

    _require_binding("task_id", task_id)
    _require_binding("idempotency_key", idempotency_key)
    material = f"{UNIT_HASH_DOMAIN}{task_id}\n{idempotency_key}".encode("utf-8")
    return UNIT_PREFIX + hashlib.sha256(material).hexdigest()[:16]


def runtime_handle_for(unit: str) -> str:
    """Return the opaque ``rtask_hermes_<32 hex>`` handle for a derived unit."""

    if not isinstance(unit, str) or not _UNIT_RE.fullmatch(unit):
        raise SandboxSpecError("runtime handle requires a derived unit name")
    material = f"{HANDLE_HASH_DOMAIN}{unit}".encode("utf-8")
    return RUNTIME_HANDLE_PREFIX + hashlib.sha256(material).hexdigest()[:32]


# ---------------------------------------------------------------------------
# sandbox spec
# ---------------------------------------------------------------------------


def build_sandbox_spec(
    config: HermesManagedConfig,
    *,
    task_id: str,
    grant_id: str,
    idempotency_key: str,
    prompt: str,
    profile_template: str,
    egress_token: str,
) -> SandboxSpec:
    """Build the hash-only launch spec for one task attempt.

    ``prompt`` must be the **final** prompt bytes that will be written to
    ``prompt.txt`` (call :func:`build_managed_prompt` first when a wrapped prompt
    is used), so ``prompt_size``/``prompt_sha256`` match the file the helper
    verifies.
    """

    if not isinstance(config, HermesManagedConfig):
        raise SandboxSpecError("sandbox spec requires a HermesManagedConfig")

    unit = unit_name_for(task_id, idempotency_key)
    _require_text_value(grant_id, "grant_id")
    _validate_resource_limits(config)
    prompt_bytes = _require_payload("prompt", prompt)
    if len(prompt_bytes) > config.prompt_max_bytes:
        raise SandboxSpecError("prompt exceeds the configured byte limit")
    profile_bytes = _require_payload("profile template", profile_template)
    token = _require_egress_token(egress_token)

    ttl_seconds = config.egress_token_ttl_seconds
    if (
        type(ttl_seconds) is not int
        or ttl_seconds < 1
        or ttl_seconds > MAX_EGRESS_TTL_SECONDS
    ):
        raise SandboxSpecError("egress token TTL must be 1..300 seconds")

    return SandboxSpec(
        task_id=task_id,
        grant_id=grant_id,
        unit=unit,
        runtime_handle=runtime_handle_for(unit),
        task_dir=Path(config.task_root) / unit,
        prompt_size=len(prompt_bytes),
        prompt_sha256=_sha256_hex(prompt_bytes),
        profile_template_sha256=_sha256_hex(profile_bytes),
        egress_token_sha256=egress_token_hash(token),
        ttl_seconds=ttl_seconds,
        runtime_max_sec=config.runtime_max_sec,
        memory_max_mb=config.memory_max_mb,
        cpu_quota_percent=config.cpu_quota_percent,
        tasks_max=config.tasks_max,
    )


# ---------------------------------------------------------------------------
# managed prompt
# ---------------------------------------------------------------------------


def build_managed_prompt(prompt: str, *, max_bytes: int | None = None) -> str:
    """Prepend the fixed managed instruction and enforce the byte ceiling.

    The bound applies to the final wrapped bytes that are written to
    ``prompt.txt``; ``max_bytes=None`` uses :data:`DEFAULT_PROMPT_MAX_BYTES`.
    """

    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptError("managed prompt must be non-empty text")
    limit = DEFAULT_PROMPT_MAX_BYTES if max_bytes is None else max_bytes
    if type(limit) is not int or limit <= 0:
        raise PromptError("managed prompt byte limit must be a positive integer")
    wrapped = MANAGED_INSTRUCTION + prompt
    if len(wrapped.encode("utf-8")) > limit:
        raise PromptError("managed prompt exceeds the configured byte limit")
    return wrapped


# ---------------------------------------------------------------------------
# artifact
# ---------------------------------------------------------------------------


def read_artifact(task_dir: Path, *, max_bytes: int) -> ArtifactResult:
    """Hash ``<task_dir>/out/result.txt`` without following symlinks.

    The only artifact ever read is ``out/result.txt``.  ``task_dir`` and ``out``
    are opened with ``O_DIRECTORY | O_NOFOLLOW`` and the artifact itself with
    ``O_NOFOLLOW | O_NONBLOCK`` relative to the ``out`` descriptor, so a
    symlinked parent, a symlinked artifact, a FIFO (which would otherwise block
    on ``open``), a socket/device or a directory all fail closed before any byte
    is read.  The digest is streamed and bounded by ``max_bytes``.
    """

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ArtifactError("artifact size limit must be a positive integer")
    if not isinstance(task_dir, (str, os.PathLike)):
        raise ArtifactError("artifact task directory is invalid")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ArtifactError("artifact symlink protection is unavailable on this host")

    root_fd = _open_directory(Path(task_dir), None)
    try:
        out_fd = _open_directory(Path("out"), root_fd)
        try:
            flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open("result.txt", flags, dir_fd=out_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ArtifactError(
                        "artifact path is a symbolic link or not a file"
                    ) from None
                raise ArtifactError("artifact is unavailable") from None

            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ArtifactError("artifact is not a regular file")
                expected_size = info.st_size
                if expected_size <= 0:
                    raise ArtifactError("artifact is empty")
                if expected_size > max_bytes:
                    raise ArtifactError("artifact exceeds the size limit")

                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(descriptor, _ARTIFACT_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArtifactError("artifact exceeds the size limit")
                    digest.update(chunk)
            finally:
                os.close(descriptor)
        finally:
            os.close(out_fd)
    finally:
        os.close(root_fd)

    if total != expected_size:
        raise ArtifactError("artifact changed while it was read")
    return ArtifactResult(
        sha256=digest.hexdigest(),
        size=total,
        relative_path="out/result.txt",
    )


def _open_directory(path: Path, dir_fd: int | None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ArtifactError("artifact directory is a symbolic link") from None
        if exc.errno == errno.ENOTDIR:
            raise ArtifactError("artifact path is not a directory") from None
        raise ArtifactError("artifact directory is unavailable") from None


# ---------------------------------------------------------------------------
# attestation verification
# ---------------------------------------------------------------------------


def verify_task_attestation(
    attestation: TaskAttestation,
    *,
    key: bytes,
    spec: SandboxSpec,
    expected_runtime_root_hash: str,
    now: datetime,
) -> None:
    """Raise :class:`AttestationError` unless the task attestation is trusted.

    Checks the HMAC, schema, task/grant/unit/handle binding, the derived task
    directory, prompt and profile-template digests, the runtime root hash
    against the trusted operator value, the required sandbox facts and the
    validity window.
    """

    signing_key = _require_key(key)
    current = _require_now(now)
    _require_runtime_root_hash_value(expected_runtime_root_hash)
    if not isinstance(attestation, TaskAttestation):
        raise AttestationError("task attestation is missing")
    _verify_signature(attestation.to_document(), attestation.hmac, signing_key)

    if attestation.schema != TASK_ATTESTATION_SCHEMA:
        raise AttestationError("task attestation schema is not supported")
    if attestation.runtime_root_hash != expected_runtime_root_hash:
        raise AttestationError(
            "task attestation runtime root hash does not match the trusted value"
        )

    bindings = {
        "unit": (attestation.unit, getattr(spec, "unit", None)),
        "task_id": (attestation.task_id, getattr(spec, "task_id", None)),
        "grant_id": (attestation.grant_id, getattr(spec, "grant_id", None)),
        "runtime_handle": (
            attestation.runtime_handle,
            getattr(spec, "runtime_handle", None),
        ),
        "prompt_sha256": (attestation.prompt_sha256, getattr(spec, "prompt_sha256", None)),
        "profile_template_sha256": (
            attestation.profile_template_sha256,
            getattr(spec, "profile_template_sha256", None),
        ),
    }
    for field_name, (attested, expected) in bindings.items():
        if not isinstance(expected, str) or attested != expected:
            raise AttestationError(f"task attestation is not bound to the spec {field_name}")

    task_dir = getattr(spec, "task_dir", None)
    if not isinstance(task_dir, Path) or task_dir.name != attestation.unit:
        raise AttestationError("task attestation is not bound to the derived task directory")

    _require_runtime_root_hash_value(attestation.runtime_root_hash)
    _verify_sandbox_facts(attestation.sandbox_facts, "task attestation")
    _verify_window(attestation.issued_at, attestation.expires_at, current, "task attestation")


def verify_runtime_attestation(
    attestation: RuntimeAttestation,
    *,
    key: bytes,
    config: HermesManagedConfig,
    now: datetime,
) -> VerifiedRuntimeAttestation:
    """Verify a runtime attestation and return an opaque proof marker.

    Checks the HMAC, schema, helper version, runtime name/version and runtime
    root hash against the trusted configuration, evidence reference, broker
    socket health, sandbox facts and validity window.  Capability labels only
    follow from this marker plus a matching ``evidence_ref``.
    """

    signing_key = _require_key(key)
    current = _require_now(now)
    if not isinstance(attestation, RuntimeAttestation):
        raise AttestationError("runtime attestation is missing")
    _verify_signature(attestation.to_document(), attestation.hmac, signing_key)

    if attestation.schema != RUNTIME_ATTESTATION_SCHEMA:
        raise AttestationError("runtime attestation schema is not supported")
    if not attestation.helper_version:
        raise AttestationError("runtime attestation helper version is missing")
    if attestation.runtime_name != getattr(config, "runtime_name", None):
        raise AttestationError("runtime attestation name does not match the configuration")
    if attestation.runtime_version != getattr(config, "runtime_version", None):
        raise AttestationError("runtime attestation version does not match the configuration")
    if attestation.evidence_ref != getattr(config, "evidence_ref", None):
        raise AttestationError("runtime attestation evidence does not match the configuration")
    expected_root_hash = getattr(config, "runtime_root_hash", None)
    _require_runtime_root_hash_value(expected_root_hash)
    if attestation.runtime_root_hash != expected_root_hash:
        raise AttestationError(
            "runtime attestation runtime root hash does not match the trusted value"
        )
    if attestation.broker_socket_ok is not True:
        raise AttestationError("runtime attestation reports the egress broker unavailable")

    _verify_sandbox_facts(attestation.sandbox_facts, "runtime attestation")
    _verify_window(attestation.issued_at, attestation.expires_at, current, "runtime attestation")
    return VerifiedRuntimeAttestation(attestation, _proof=_VERIFIED_PROOF)


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def managed_capabilities(
    config: HermesManagedConfig,
    verified_runtime_attestation: VerifiedRuntimeAttestation | None,
    *,
    now: datetime | None = None,
) -> RuntimeCapabilities:
    """Build the fail-closed capability snapshot for the managed Hermes runtime.

    Only a :class:`VerifiedRuntimeAttestation` produced by
    :func:`verify_runtime_attestation` can upgrade a managed label.  A raw
    :class:`RuntimeAttestation` — even one with a well-formed 64-hex signature —
    is a ``TypeError``, and ``None`` leaves every managed entry ``unverified``.
    The verified attestation is re-checked against the trusted configuration and
    the requested ``now`` so a later config change or expiry also fails closed.
    """

    current = _require_now(now)
    if verified_runtime_attestation is None:
        trusted = False
    elif isinstance(verified_runtime_attestation, VerifiedRuntimeAttestation):
        trusted = (
            _runtime_attestation_problem(
                config, verified_runtime_attestation.attestation, current
            )
            is None
        )
    else:
        raise TypeError(
            "managed capabilities require a VerifiedRuntimeAttestation or None"
        )

    capabilities: list[Capability] = []
    for name in RuntimeCapability:
        if name in _VERIFIED_LIMITATIONS:
            if trusted:
                capabilities.append(
                    _capability(
                        name,
                        CapabilityStatus.VERIFIED,
                        _VERIFIED_LIMITATIONS[name],
                        getattr(config, "evidence_ref", None),
                    )
                )
            else:
                capabilities.append(
                    _capability(
                        name,
                        CapabilityStatus.UNVERIFIED,
                        _UNVERIFIED_LIMITATION,
                        None,
                    )
                )
            continue

        limitation = (
            _EVENTS_POLL_LIMITATION
            if name is RuntimeCapability.TASK_EVENTS_POLL
            else _UNSUPPORTED_LIMITATION
        )
        capabilities.append(
            _capability(name, CapabilityStatus.UNVERIFIED, limitation, None)
        )

    return RuntimeCapabilities(
        adapter_contract_version=ADAPTER_CONTRACT_VERSION,
        boundary=BOUNDARY,
        runtime_id=MANAGED_RUNTIME_ID,
        runtime_type=RuntimeType.HERMES,
        runtime_version=getattr(config, "runtime_version", None),
        version_source=VersionSource.OPERATOR_VERIFIED,
        capabilities=tuple(capabilities),
        observed_at=current,
        runtime_name=getattr(config, "runtime_name", None),
    )


def _capability(
    name: RuntimeCapability,
    status: CapabilityStatus,
    limitations: str,
    evidence_ref: str | None,
) -> Capability:
    return Capability(
        name=name.value,
        version=ADAPTER_CONTRACT_VERSION,
        status=status,
        evidence_ref=evidence_ref,
        limitations=limitations,
    )


def _runtime_attestation_problem(
    config: HermesManagedConfig,
    attestation: object,
    now: datetime,
) -> str | None:
    """Return a stable reason when the attestation cannot be trusted structurally."""

    if not isinstance(attestation, RuntimeAttestation):
        return "runtime attestation is missing"
    if attestation.schema != RUNTIME_ATTESTATION_SCHEMA:
        return "runtime attestation schema is not supported"
    if not _SHA256_HEX_RE.fullmatch(attestation.hmac or ""):
        return "runtime attestation signature is missing"
    if attestation.runtime_name != getattr(config, "runtime_name", None):
        return "runtime attestation name does not match the configuration"
    if attestation.runtime_version != getattr(config, "runtime_version", None):
        return "runtime attestation version does not match the configuration"
    if attestation.evidence_ref != getattr(config, "evidence_ref", None):
        return "runtime attestation evidence does not match the configuration"
    if not isinstance(attestation.helper_version, str) or not attestation.helper_version:
        return "runtime attestation helper version is missing"
    if attestation.broker_socket_ok is not True:
        return "runtime attestation reports the egress broker unavailable"
    if not _SHA256_REF_RE.fullmatch(attestation.runtime_root_hash or ""):
        return "runtime attestation runtime root hash is malformed"
    if attestation.runtime_root_hash != getattr(config, "runtime_root_hash", None):
        return "runtime attestation runtime root hash does not match the trusted value"
    try:
        _verify_sandbox_facts(attestation.sandbox_facts, "runtime attestation")
        _verify_window(
            attestation.issued_at, attestation.expires_at, now, "runtime attestation"
        )
    except AttestationError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _require_binding(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or not value.strip():
        raise SandboxSpecError(f"{name} must be non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise SandboxSpecError(f"{name} must not contain control characters")


def _require_text_value(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or not value.strip():
        raise SandboxSpecError(f"{name} must be non-empty text")


_RESOURCE_LIMITS = (
    ("runtime_max_sec", MAX_RUNTIME_MAX_SEC),
    ("memory_max_mb", MAX_MEMORY_MAX_MB),
    ("cpu_quota_percent", MAX_CPU_QUOTA_PERCENT),
    ("tasks_max", MAX_TASKS_MAX),
    ("prompt_max_bytes", MAX_PROMPT_MAX_BYTES),
)


def _validate_resource_limits(config: HermesManagedConfig) -> None:
    for name, upper in _RESOURCE_LIMITS:
        value = getattr(config, name, None)
        if type(value) is not int or value < 1 or value > upper:
            raise SandboxSpecError(f"{name} must be an integer in 1..{upper}")


def _require_payload(name: str, value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise SandboxSpecError(f"{name} must be non-empty text")
    return value.encode("utf-8")


def _require_egress_token(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SandboxSpecError("egress token must be non-empty text")
    if len(value) > MAX_EGRESS_TOKEN_LENGTH:
        raise SandboxSpecError("egress token is too long")
    return value


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()



def _require_key(key: object) -> bytes:
    if not isinstance(key, bytes) or len(key) < _MIN_HMAC_KEY_BYTES:
        raise AttestationError(
            f"attestation key must be at least {_MIN_HMAC_KEY_BYTES} bytes"
        )
    return key


def _require_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise HermesManagedError("time must be timezone-aware")
    return now.astimezone(UTC)


def _canonical_json(document: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(document), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _verify_signature(
    document: Mapping[str, object], provided: object, key: bytes
) -> None:
    if not isinstance(provided, str) or not _SHA256_HEX_RE.fullmatch(provided):
        raise AttestationError("attestation signature is malformed")
    payload = {name: value for name, value in document.items() if name != "hmac"}
    expected = hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise AttestationError("attestation signature mismatch")


def _verify_window(
    issued_at: datetime,
    expires_at: datetime,
    now: datetime,
    label: str,
) -> None:
    if expires_at <= now:
        raise AttestationError(f"{label} is expired")
    if issued_at > now:
        raise AttestationError(f"{label} is not valid yet")


def _verify_sandbox_facts(facts: Mapping[str, object], label: str) -> None:
    for name, expected in _REQUIRED_SANDBOX_FACTS.items():
        actual = facts.get(name)
        # Type-exact comparison: JSON ``true`` must not be satisfied by ``1``.
        if type(actual) is not type(expected) or actual != expected:
            raise AttestationError(f"{label} reports an unsafe sandbox fact {name}")
    toolsets = facts.get("toolsets")
    if not isinstance(toolsets, (list, tuple)) or tuple(toolsets) != ("terminal",):
        raise AttestationError(f"{label} reports an unsafe sandbox toolset")


def _require_document(
    document: Mapping[str, object], expected_keys: frozenset[str]
) -> dict[str, object]:
    if not isinstance(document, Mapping):
        raise AttestationError("attestation document must be a mapping")
    data = dict(document)
    if set(data) != expected_keys:
        raise AttestationError("attestation document has unexpected or missing fields")
    return data


def _require_schema(data: Mapping[str, object], expected: str) -> str:
    value = data["schema"]
    if value != expected:
        raise AttestationError("attestation schema is not supported")
    return expected


def _require_text(
    data: Mapping[str, object], name: str, *, max_length: int = _MAX_ATTESTATION_TEXT
) -> str:
    value = data[name]
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise AttestationError(f"attestation field {name} is invalid")
    if value != value.strip():
        raise AttestationError(f"attestation field {name} is invalid")
    if any(character.isspace() for character in value):
        raise AttestationError(f"attestation field {name} is invalid")
    return value


def _require_uid(data: Mapping[str, object]) -> int:
    value = data["uid"]
    if type(value) is not int or value <= 0:
        raise AttestationError("attestation field uid is invalid")
    return value


def _require_bool(data: Mapping[str, object], name: str) -> bool:
    value = data[name]
    if type(value) is not bool:
        raise AttestationError(f"attestation field {name} is invalid")
    return value


def _require_runtime_root_hash(data: Mapping[str, object]) -> str:
    value = data["runtime_root_hash"]
    _require_runtime_root_hash_value(value)
    return value  # type: ignore[return-value]


def _require_runtime_root_hash_value(value: object) -> None:
    if not isinstance(value, str) or not _SHA256_REF_RE.fullmatch(value):
        raise AttestationError("attestation runtime root hash is malformed")


def _require_sha256_hex(data: Mapping[str, object], name: str) -> str:
    value = data[name]
    if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value):
        raise AttestationError(f"attestation field {name} is invalid")
    return value


def _require_hmac_text(data: Mapping[str, object]) -> str:
    value = data["hmac"]
    if not isinstance(value, str):
        raise AttestationError("attestation signature is malformed")
    return value


def _require_timestamp(data: Mapping[str, object], name: str) -> datetime:
    value = data[name]
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AttestationError(f"attestation field {name} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise AttestationError(f"attestation field {name} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AttestationError(f"attestation field {name} is invalid")
    parsed = parsed.astimezone(UTC)
    if _iso_utc(parsed) != value:
        raise AttestationError(f"attestation field {name} is invalid")
    return parsed


def _freeze_facts(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AttestationError("attestation sandbox facts must be a mapping")
    return MappingProxyType(
        {str(name): _freeze_fact_value(item) for name, item in value.items()}
    )


def _freeze_fact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_facts(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_fact_value(item) for item in value)
    return value


def _thaw_facts(value: object) -> object:
    """Return a JSON-serialisable copy of frozen sandbox facts."""

    if isinstance(value, Mapping):
        return {str(name): _thaw_facts(item) for name, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_facts(item) for item in value]
    return value


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
