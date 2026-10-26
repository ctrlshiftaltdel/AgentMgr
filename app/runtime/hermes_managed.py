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
* :class:`SandboxStatus` / :class:`SandboxStop` -- supervisor result contracts;
* :class:`HelperSandboxSupervisor` -- the stdin-JSON root-helper client;
* :class:`HermesManagedAdapter` -- the complete :class:`RuntimeAdapter`
  implementation (submit/lookup/status/result/cancel/health).

The contract half of this module is pure: no database, HTTP or systemd access.
The supervisor client runs the fixed helper command and the adapter writes only
the three task input files (``prompt.txt``, ``profile.template.yaml``,
``egress.token``) and the broker revocation markers.  Neither ever writes task
state or calls ``complete_task``.

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
import grp
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, runtime_checkable

from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    CancelOutcome,
    CancelResult,
    Capability,
    CapabilityStatus,
    ErrorCode,
    EventBatch,
    HealthResult,
    HealthStatus,
    InputOutcome,
    InputResult,
    LookupOutcome,
    LookupResult,
    ResultOutcome,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeErrorInfo,
    RuntimeGrant,
    RuntimeResult,
    RuntimeState,
    RuntimeType,
    StatusResult,
    SubmitOutcome,
    SubmitResult,
    VersionSource,
)
from app.runtime.hermes_egress import (
    MAX_EGRESS_TOKEN_LENGTH,
    MAX_EGRESS_TTL_SECONDS,
    EgressClaims,
    EgressTokenError,
    egress_token_hash,
    mint_egress_token,
    write_revocation_markers,
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
    "MIN_RUNTIME_MAX_SEC",
    "MODEL_NAME_PATTERN",
    "RUNTIME_ATTESTATION_SCHEMA",
    "RUNTIME_HANDLE_PREFIX",
    "SANDBOX_GROUP",
    "TASK_ATTESTATION_SCHEMA",
    "UNIT_PREFIX",
    "ArtifactError",
    "ArtifactResult",
    "AttestationError",
    "HelperSandboxSupervisor",
    "HermesManagedAdapter",
    "HermesManagedConfig",
    "HermesManagedError",
    "PromptError",
    "RuntimeAttestation",
    "SandboxManifest",
    "SandboxSpec",
    "SandboxSpecError",
    "SandboxStatus",
    "SandboxStop",
    "SandboxSupervisor",
    "SupervisorError",
    "TaskAttestation",
    "VerifiedRuntimeAttestation",
    "build_managed_prompt",
    "build_sandbox_spec",
    "managed_capabilities",
    "read_artifact",
    "read_manifest",
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

#: The workload rejects a budget below one second, but a Hermes one-shot run
#: needs a usable window; the deployment loader and the adapter both require at
#: least this many seconds (plan Tasks 5 and 9).
MIN_RUNTIME_MAX_SEC = 9

#: Workload-compatible model-name shape.  A leading ``-`` is impossible, so a
#: model name can never be parsed as a command-line option.
MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:+/@-]{0,255}$")

#: Fixed sandbox group used by the helper for task files.
SANDBOX_GROUP = "agentmgr-hermes"

#: Helper manifest schema and fixed launch paths (Task 2 contract).
MANIFEST_SCHEMA = "agentmgr.hermes.manifest.v1"
UNIT_EGRESS_SOCKET = "/task/egress.sock"
UNIT_TOOLSET = "terminal"

_PROMPT_FILE_MODE = 0o640
_PROFILE_FILE_MODE = 0o640
_TOKEN_FILE_MODE = 0o640
_MANIFEST_MAX_BYTES = 65_536
_HELPER_STDOUT_MAX_BYTES = 1_048_576
_MIN_KEY_BYTES = 32
_MAX_KEY_BYTES = 4_096
_MAX_ID_TEXT = 256
_MAX_REASON_TEXT = 256
_HELPER_TIMEOUT_FLOOR = 60.0
_MAX_TASK_SCAN = 4_096

_SAFE_HANDLE_RE = re.compile(r"^rtask_hermes_[A-Za-z0-9_-]{4,63}$")
_SAFE_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_SAFE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_PROFILE_PORT_TOKEN = "__PORT__"

_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "unit",
        "task_id",
        "grant_id",
        "runtime_handle",
        "idempotency_key",
        "runtime_root_hash",
        "prompt_sha256",
        "prompt_bytes",
        "profile_template_sha256",
        "egress_token_sha256",
        "ttl_seconds",
        "runtime_max_sec",
        "memory_max_mb",
        "cpu_quota_percent",
        "tasks_max",
        "egress_socket",
        "toolset",
        "payload_sha256",
        "created_at",
        "model",
    }
)

#: Helper launch failures that provably happen before ``systemd-run`` starts a
#: unit.  Everything else is ambiguous: the adapter must stop and report
#: unknown rather than assume no side effect.
_DEFINITE_LAUNCH_PREFIXES = (
    "install_config_",
    "task_root_",
    "runtime_root_",
    "broker_socket_",
    "workload_",
    "task_dir_",
    "task_file_",
    "prompt_",
    "profile_",
    "token_",
    "out_dir_",
    "egress_token_",
    "attestation_key_",
)
_DEFINITE_LAUNCH_CODES = frozenset(
    {
        "adapter_sandbox_group_missing",
        "arguments_not_accepted",
        "forbidden_field",
        "group_unavailable",
        "invalid_digest",
        "invalid_limit",
        "invalid_operation",
        "invalid_request",
        "invalid_unit",
        "masked_source",
        "missing_field",
        "path_unavailable",
        "payload_too_large",
        "private_tmp_source",
        "runtime_handle_mismatch",
        "unit_name_mismatch",
        "unknown_field",
        "user_unavailable",
    }
)

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
    #: Broker revocation store.  The managed adapter writes Task 4 markers here
    #: on a confirmed stop or after reading the result artifact.  Provisioning
    #: (Task 9) points this at the broker configuration's ``revocation_dir``.
    revocation_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """Launch contract shared with the root helper.

    It carries hashes, sizes and server-derived identifiers only: the raw
    prompt, profile template and egress token never leave the trusted adapter.
    ``idempotency_key`` is the dispatcher-supplied attempt key (its digest only
    reaches the unit name); it is included because the root helper re-derives
    and verifies the unit from it.  ``task_dir`` is still a host filesystem
    path, so redact it before writing this object to an external log or
    evidence store.
    """

    task_id: str
    grant_id: str
    unit: str
    runtime_handle: str
    idempotency_key: str
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
        idempotency_key=idempotency_key,
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
# durable task manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxManifest:
    """The helper-written manifest binding one unit to one submission.

    The adapter never writes this document; it is produced by ``prepare`` in the
    root helper.  Reading it back is how the adapter recovers the unit/handle
    mapping after a restart without trusting caller input or using the opaque
    runtime handle as a path.
    """

    schema: str
    unit: str
    task_id: str
    grant_id: str
    runtime_handle: str
    idempotency_key: str
    runtime_root_hash: str
    prompt_sha256: str
    prompt_bytes: int
    profile_template_sha256: str
    egress_token_sha256: str
    ttl_seconds: int
    runtime_max_sec: int
    memory_max_mb: int
    cpu_quota_percent: int
    tasks_max: int
    egress_socket: str
    toolset: str
    payload_sha256: str
    created_at: datetime
    model: str | None = None

    def binding_document(self) -> dict[str, object]:
        """Return the exact helper binding whose digest is ``payload_sha256``."""

        return {
            "schema": self.schema,
            "unit": self.unit,
            "task_id": self.task_id,
            "grant_id": self.grant_id,
            "runtime_handle": self.runtime_handle,
            "idempotency_key": self.idempotency_key,
            "runtime_root_hash": self.runtime_root_hash,
            "prompt_sha256": self.prompt_sha256,
            "prompt_bytes": self.prompt_bytes,
            "profile_template_sha256": self.profile_template_sha256,
            "egress_token_sha256": self.egress_token_sha256,
            "ttl_seconds": self.ttl_seconds,
            "runtime_max_sec": self.runtime_max_sec,
            "memory_max_mb": self.memory_max_mb,
            "cpu_quota_percent": self.cpu_quota_percent,
            "tasks_max": self.tasks_max,
            "egress_socket": self.egress_socket,
            "toolset": self.toolset,
        }

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, object],
        *,
        expected_unit: str | None = None,
        expected_runtime_root_hash: str | None = None,
    ) -> SandboxManifest:
        if not isinstance(document, Mapping):
            raise SandboxSpecError("task manifest is not an object")
        data = dict(document)
        if set(data) - _MANIFEST_KEYS:
            raise SandboxSpecError("task manifest has unexpected fields")
        required = _MANIFEST_KEYS - {"model"}
        if required - set(data):
            raise SandboxSpecError("task manifest has missing fields")
        if data["schema"] != MANIFEST_SCHEMA:
            raise SandboxSpecError("task manifest schema is not supported")

        unit = _manifest_text(data, "unit", pattern=_UNIT_RE)
        if expected_unit is not None and unit != expected_unit:
            raise SandboxSpecError("task manifest is bound to a different unit")
        runtime_root_hash = _manifest_text(data, "runtime_root_hash", pattern=_SHA256_REF_RE)
        if (
            expected_runtime_root_hash is not None
            and runtime_root_hash != expected_runtime_root_hash
        ):
            raise SandboxSpecError(
                "task manifest runtime root hash does not match the trusted value"
            )
        egress_socket = _manifest_text(data, "egress_socket")
        if egress_socket != UNIT_EGRESS_SOCKET:
            raise SandboxSpecError("task manifest egress socket is not the fixed path")
        toolset = _manifest_text(data, "toolset")
        if toolset != UNIT_TOOLSET:
            raise SandboxSpecError("task manifest toolset is not terminal-only")

        manifest = cls(
            schema=MANIFEST_SCHEMA,
            unit=unit,
            task_id=_manifest_text(data, "task_id"),
            grant_id=_manifest_text(data, "grant_id"),
            runtime_handle=_manifest_text(data, "runtime_handle", pattern=_SAFE_HANDLE_RE),
            idempotency_key=_manifest_text(data, "idempotency_key"),
            runtime_root_hash=runtime_root_hash,
            prompt_sha256=_manifest_text(data, "prompt_sha256", pattern=_SHA256_HEX_RE),
            prompt_bytes=_manifest_int(data, "prompt_bytes", 1, MAX_PROMPT_MAX_BYTES),
            profile_template_sha256=_manifest_text(
                data, "profile_template_sha256", pattern=_SHA256_HEX_RE
            ),
            egress_token_sha256=_manifest_text(
                data, "egress_token_sha256", pattern=_SHA256_HEX_RE
            ),
            ttl_seconds=_manifest_int(data, "ttl_seconds", 1, MAX_EGRESS_TTL_SECONDS),
            runtime_max_sec=_manifest_int(
                data, "runtime_max_sec", 1, MAX_RUNTIME_MAX_SEC
            ),
            memory_max_mb=_manifest_int(
                data, "memory_max_mb", 1, MAX_MEMORY_MAX_MB
            ),
            cpu_quota_percent=_manifest_int(
                data, "cpu_quota_percent", 1, MAX_CPU_QUOTA_PERCENT
            ),
            tasks_max=_manifest_int(data, "tasks_max", 1, MAX_TASKS_MAX),
            egress_socket=egress_socket,
            toolset=toolset,
            payload_sha256=_manifest_text(
                data, "payload_sha256", pattern=_SHA256_HEX_RE
            ),
            created_at=_parse_iso(data.get("created_at"), "task manifest created_at"),
            model=(
                None
                if "model" not in data
                else _manifest_text(data, "model", max_length=256)
            ),
        )
        expected_digest = hashlib.sha256(
            _canonical_json(manifest.binding_document())
        ).hexdigest()
        if manifest.payload_sha256 != expected_digest:
            raise SandboxSpecError("task manifest payload digest does not match")
        return manifest


def read_manifest(config: HermesManagedConfig, unit: str) -> SandboxManifest | None:
    """Read ``<task_root>/<unit>/manifest.json`` with no symlink following.

    Returns ``None`` when the task directory or manifest does not exist; any
    malformed, tampered or unsafe document raises :class:`SandboxSpecError`.
    The task directory is always derived from the trusted root and unit, never
    from caller input or the opaque handle.
    """

    if not isinstance(config, HermesManagedConfig):
        raise SandboxSpecError("task manifest requires a managed configuration")
    if not isinstance(unit, str) or not _UNIT_RE.fullmatch(unit):
        raise SandboxSpecError("task manifest requires a derived unit name")
    task_dir = Path(config.task_root) / unit
    try:
        directory_fd = os.open(
            task_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise SandboxSpecError("task directory is unavailable") from None
    try:
        flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open("manifest.json", flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise SandboxSpecError("task manifest is unavailable") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MANIFEST_MAX_BYTES:
                raise SandboxSpecError("task manifest is not a bounded regular file")
            if info.st_uid != os.geteuid():
                raise SandboxSpecError("task manifest owner is invalid")
            sandbox_gid = _sandbox_gid()
            if sandbox_gid is not None and info.st_gid != sandbox_gid:
                raise SandboxSpecError("task manifest group is invalid")
            if stat.S_IMODE(info.st_mode) != 0o640:
                raise SandboxSpecError("task manifest mode is invalid")
            raw = _read_bounded_fd(descriptor, _MANIFEST_MAX_BYTES)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)

    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError):
        raise SandboxSpecError("task manifest is not valid JSON") from None
    return SandboxManifest.from_document(
        document,
        expected_unit=unit,
        expected_runtime_root_hash=config.runtime_root_hash,
    )


def _manifest_text(
    data: Mapping[str, object],
    name: str,
    *,
    max_length: int = _MAX_ID_TEXT,
    pattern: re.Pattern[str] | None = None,
) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise SandboxSpecError(f"task manifest field {name} is invalid")
    if value != value.strip() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise SandboxSpecError(f"task manifest field {name} is invalid")
    if pattern is not None and not pattern.fullmatch(value):
        raise SandboxSpecError(f"task manifest field {name} is invalid")
    return value


def _manifest_int(
    data: Mapping[str, object], name: str, minimum: int, maximum: int
) -> int:
    value = data.get(name)
    if type(value) is not int or not minimum <= value <= maximum:
        raise SandboxSpecError(f"task manifest field {name} is invalid")
    return value


def _parse_iso(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise SandboxSpecError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise SandboxSpecError(f"{label} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SandboxSpecError(f"{label} is invalid")
    return parsed.astimezone(UTC)


def _read_bounded_fd(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(_ARTIFACT_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise SandboxSpecError("task manifest exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# supervisor protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SandboxSupervisor(Protocol):
    """Privileged sandbox operations used by :class:`HermesManagedAdapter`.

    Implementations either talk to the root helper over a fixed stdin JSON
    protocol or, in tests, script the same facts.  Every method reports facts
    only; none of them writes task/DB state.
    """

    def prepare(self, spec: SandboxSpec) -> None:
        """Create the derived task directory and its manifest."""
        ...

    def launch(self, spec: SandboxSpec) -> TaskAttestation:
        """Start the unit and return only after the started marker exists."""
        ...

    def status(self, unit: str) -> SandboxStatus:
        """Return systemd and task-directory facts for one unit."""
        ...

    def stop(self, unit: str, *, reason: str) -> SandboxStop:
        """Stop the unit; ``stop_confirmed`` requires the process/cgroup gone."""
        ...

    def cleanup(self, unit: str, *, result_read: bool = False) -> None:
        """Delete the task directory only when the result was read."""
        ...

    def runtime_attestation(self) -> RuntimeAttestation:
        """Return a freshly signed, task-independent runtime attestation."""
        ...


class SupervisorError(HermesManagedError):
    """A stable supervisor/helper failure with an ambiguity flag.

    ``ambiguous`` is true when the operation may already have created or
    modified a unit, so the adapter must report ``unknown`` and never replay.
    The message contains only the stable helper code: never a prompt, token,
    path or helper output body.
    """

    def __init__(self, code: str, *, ambiguous: bool = False) -> None:
        if not isinstance(code, str) or not _SAFE_CODE_RE.fullmatch(code):
            code = "supervisor_error"
        self.code = code
        self.ambiguous = bool(ambiguous)
        super().__init__(f"supervisor operation failed: {code}")


# ---------------------------------------------------------------------------
# helper client
# ---------------------------------------------------------------------------


class HelperSandboxSupervisor:
    """Root-helper client: fixed sudo command, one JSON request on stdin.

    ``argv`` is exactly ``["sudo", "-n", <fixed helper path>]``.  No prompt,
    token, path or caller value ever appears in argv, and no shell is used.
    Responses are strict JSON objects with an exact key set per operation; a
    malformed, oversized or inconsistent response is a stable error, never a
    guessed fact.
    """

    def __init__(
        self,
        config: HermesManagedConfig,
        *,
        runner: Callable[..., object] = subprocess.run,
    ) -> None:
        if not isinstance(config, HermesManagedConfig):
            raise TypeError("helper supervisor requires a managed configuration")
        helper = Path(config.helper_path)
        if not helper.is_absolute() or "\x00" in str(helper):
            raise ValueError("helper path must be an absolute path")
        if not callable(runner):
            raise TypeError("helper runner must be callable")
        self._config = config
        self._helper_path = helper
        self._runner = runner
        self._timeout = max(
            _HELPER_TIMEOUT_FLOOR, float(config.stop_timeout_sec) * 2.0 + 30.0
        )
        self._lock = threading.Lock()

    # -- protocol ------------------------------------------------------------

    def prepare(self, spec: SandboxSpec) -> None:
        if not isinstance(spec, SandboxSpec):
            raise SupervisorError("invalid_request")
        response = self._call("prepare", self._spec_payload(spec))
        self._require_exact(
            response, {"ok", "operation", "unit", "prepared", "created", "task_dir_ready"}
        )
        if response["unit"] != spec.unit:
            raise SupervisorError("unit_name_mismatch")
        if response["prepared"] is not True or response["task_dir_ready"] is not True:
            raise SupervisorError("task_dir_not_ready")
        if response["created"] is not True:
            raise SupervisorError("task_dir_not_fresh")

    def launch(self, spec: SandboxSpec) -> TaskAttestation:
        if not isinstance(spec, SandboxSpec):
            raise SupervisorError("invalid_request")
        response = self._call("launch", self._spec_payload(spec))
        self._require_exact(
            response,
            {
                "ok",
                "operation",
                "unit",
                "active_state",
                "started",
                "uid",
                "main_pid",
                "attestation",
            },
        )
        if response["unit"] != spec.unit:
            raise SupervisorError("unit_name_mismatch", ambiguous=True)
        if response["started"] is not True or response["active_state"] != "active":
            raise SupervisorError("started_not_confirmed", ambiguous=True)
        document = response["attestation"]
        if not isinstance(document, Mapping):
            raise SupervisorError("attestation_invalid", ambiguous=True)
        try:
            attestation = TaskAttestation.from_document(document)
        except AttestationError:
            raise SupervisorError("attestation_invalid", ambiguous=True) from None
        if attestation.unit != spec.unit:
            raise SupervisorError("attestation_unit_mismatch", ambiguous=True)
        return attestation

    def status(self, unit: str) -> SandboxStatus:
        manifest = self._manifest(unit)
        response = self._call("status", self._manifest_payload(manifest))
        self._require_exact(
            response, {"ok", "operation", "unit", "uid", "unit_state", "task", "observed_at"}
        )
        if response["unit"] != unit:
            raise SupervisorError("unit_name_mismatch")
        return _status_from_document(manifest, response)

    def stop(self, unit: str, *, reason: str) -> SandboxStop:
        manifest = self._manifest(unit)
        self._require_reason(reason)
        response = self._call("stop", self._manifest_payload(manifest))
        self._require_exact(
            response,
            {
                "ok",
                "operation",
                "unit",
                "unit_state",
                "process_gone",
                "stop_confirmed",
                "observed_at",
            },
        )
        if response["unit"] != unit:
            raise SupervisorError("unit_name_mismatch")
        return _stop_from_document(unit, response)

    def cleanup(self, unit: str, *, result_read: bool = False) -> None:
        if result_read is not True:
            # The helper refuses to delete a task directory before the adapter
            # declares the result was read.  A false call is a deliberate no-op
            # so an ambiguous launch can still request cleanup uniformly.
            return
        manifest = self._manifest(unit)
        payload = self._manifest_payload(manifest)
        payload["result_read"] = True
        response = self._call("cleanup", payload)
        self._require_exact(
            response,
            {"ok", "operation", "unit", "removed", "process_gone"},
        )
        if response["unit"] != unit:
            raise SupervisorError("unit_name_mismatch")
        if response["process_gone"] is not True:
            raise SupervisorError("cleanup_unconfirmed", ambiguous=True)
        if type(response["removed"]) is not bool:
            raise SupervisorError("helper_response_invalid", ambiguous=True)

    def runtime_attestation(self) -> RuntimeAttestation:
        response = self._call("attest", self._sentinel_payload())
        self._require_exact(
            response,
            {
                "ok",
                "operation",
                "unit",
                "runtime_attestation",
                "task_attestation",
                "observed_at",
            },
        )
        document = response["runtime_attestation"]
        if not isinstance(document, Mapping):
            raise SupervisorError("attestation_invalid")
        try:
            return RuntimeAttestation.from_document(document)
        except AttestationError:
            raise SupervisorError("attestation_invalid") from None

    # -- request construction ------------------------------------------------

    def _manifest(self, unit: str) -> SandboxManifest:
        if not isinstance(unit, str) or not _UNIT_RE.fullmatch(unit):
            raise SupervisorError("invalid_unit")
        try:
            manifest = read_manifest(self._config, unit)
        except SandboxSpecError:
            raise SupervisorError("manifest_invalid") from None
        if manifest is None:
            raise SupervisorError("manifest_unavailable")
        return manifest

    @staticmethod
    def _spec_payload(spec: SandboxSpec) -> dict[str, object]:
        return {
            "task_id": spec.task_id,
            "grant_id": spec.grant_id,
            "unit": spec.unit,
            "runtime_handle": spec.runtime_handle,
            "idempotency_key": spec.idempotency_key,
            "prompt_sha256": spec.prompt_sha256,
            "prompt_bytes": spec.prompt_size,
            "profile_template_sha256": spec.profile_template_sha256,
            "egress_token_sha256": spec.egress_token_sha256,
            "ttl_seconds": spec.ttl_seconds,
            "runtime_max_sec": spec.runtime_max_sec,
            "memory_max_mb": spec.memory_max_mb,
            "cpu_quota_percent": spec.cpu_quota_percent,
            "tasks_max": spec.tasks_max,
        }

    @staticmethod
    def _manifest_payload(manifest: SandboxManifest) -> dict[str, object]:
        return {
            "task_id": manifest.task_id,
            "grant_id": manifest.grant_id,
            "unit": manifest.unit,
            "runtime_handle": manifest.runtime_handle,
            "idempotency_key": manifest.idempotency_key,
            "prompt_sha256": manifest.prompt_sha256,
            "prompt_bytes": manifest.prompt_bytes,
            "profile_template_sha256": manifest.profile_template_sha256,
            "egress_token_sha256": manifest.egress_token_sha256,
            "ttl_seconds": manifest.ttl_seconds,
            "runtime_max_sec": manifest.runtime_max_sec,
            "memory_max_mb": manifest.memory_max_mb,
            "cpu_quota_percent": manifest.cpu_quota_percent,
            "tasks_max": manifest.tasks_max,
        }

    def _sentinel_payload(self) -> dict[str, object]:
        task_id = "agentmgr-runtime-attestation"
        idempotency_key = "agentmgr-runtime-attestation"
        unit = unit_name_for(task_id, idempotency_key)
        digest = hashlib.sha256(b"agentmgr-runtime-attestation").hexdigest()
        return {
            "task_id": task_id,
            "grant_id": "agentmgr-runtime-attestation",
            "unit": unit,
            "runtime_handle": runtime_handle_for(unit),
            "idempotency_key": idempotency_key,
            "prompt_sha256": digest,
            "prompt_bytes": 1,
            "profile_template_sha256": digest,
            "egress_token_sha256": digest,
            "ttl_seconds": 1,
            "runtime_max_sec": self._config.runtime_max_sec,
            "memory_max_mb": self._config.memory_max_mb,
            "cpu_quota_percent": self._config.cpu_quota_percent,
            "tasks_max": self._config.tasks_max,
        }

    # -- transport -----------------------------------------------------------

    def _call(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        request = dict(payload)
        request["operation"] = operation
        try:
            encoded = json.dumps(
                request, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        except (TypeError, ValueError):
            raise SupervisorError("invalid_request") from None
        argv = ["sudo", "-n", str(self._helper_path)]
        try:
            with self._lock:
                completed = self._runner(
                    argv,
                    input=encoded,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=self._timeout,
                )
        except subprocess.TimeoutExpired:
            raise SupervisorError("helper_timeout", ambiguous=True) from None
        except OSError:
            raise SupervisorError("helper_unavailable") from None
        except TypeError:
            raise SupervisorError("helper_runner_invalid") from None

        returncode = getattr(completed, "returncode", None)
        stdout = getattr(completed, "stdout", None)
        stderr = getattr(completed, "stderr", None)
        if type(returncode) is not int:
            raise SupervisorError("helper_response_invalid", ambiguous=True)
        if returncode != 0:
            code = _helper_error_code(stderr) or "helper_failed"
            raise SupervisorError(
                code, ambiguous=_launch_error_is_ambiguous(operation, code)
            )
        if not isinstance(stdout, str) or not stdout or len(stdout) > _HELPER_STDOUT_MAX_BYTES:
            raise SupervisorError("helper_response_invalid", ambiguous=True)
        try:
            response = json.loads(stdout, object_pairs_hook=_reject_duplicate_keys)
        except (TypeError, ValueError):
            raise SupervisorError("helper_response_invalid", ambiguous=True) from None
        if not isinstance(response, dict):
            raise SupervisorError("helper_response_invalid", ambiguous=True)
        if response.get("ok") is not True or response.get("operation") != operation:
            raise SupervisorError("helper_response_invalid", ambiguous=True)
        return response

    @staticmethod
    def _require_exact(response: Mapping[str, object], keys: set[str]) -> None:
        if set(response) != keys:
            raise SupervisorError("helper_response_invalid", ambiguous=True)

    @staticmethod
    def _require_reason(reason: object) -> str:
        if not isinstance(reason, str) or not reason.strip():
            return "cancelled"
        if len(reason) > _MAX_REASON_TEXT or any(
            ord(ch) < 0x20 or ord(ch) == 0x7F for ch in reason
        ):
            raise SupervisorError("invalid_request")
        return reason


def _helper_error_code(stderr: object) -> str | None:
    if not isinstance(stderr, str) or len(stderr) > 4_096:
        return None
    line = stderr.strip().splitlines()[0] if stderr.strip() else ""
    try:
        document = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict) or set(document) != {"error"}:
        return None
    code = document.get("error")
    if not isinstance(code, str) or not _SAFE_CODE_RE.fullmatch(code):
        return None
    return code


def _launch_error_is_ambiguous(operation: str, code: str) -> bool:
    """A launch failure is definite only when it happened before systemd-run."""

    if operation != "launch":
        return False
    if code in _DEFINITE_LAUNCH_CODES:
        return False
    if any(code.startswith(prefix) for prefix in _DEFINITE_LAUNCH_PREFIXES):
        return False
    return True


def _status_from_document(
    manifest: SandboxManifest, response: Mapping[str, object]
) -> SandboxStatus:
    unit_state = response.get("unit_state")
    task = response.get("task")
    if not isinstance(unit_state, Mapping) or not isinstance(task, Mapping):
        raise SupervisorError("helper_response_invalid", ambiguous=True)
    load_state = _fact_text(unit_state, "load_state")
    active_state = "not-found" if load_state == "not-found" else _fact_text(unit_state, "active_state")
    sub_state = _fact_optional_text(unit_state, "sub_state")
    main_pid = _fact_optional_int(unit_state, "main_pid", minimum=0)
    result = _fact_optional_text(unit_state, "result")
    exit_status = _fact_optional_int(unit_state, "exit_status", minimum=-256)
    process_gone = _fact_bool(unit_state, "process_gone")
    started = _fact_bool(task, "started")
    heartbeat_raw = task.get("heartbeat_at")
    heartbeat_at = (
        None
        if heartbeat_raw is None
        else _supervisor_iso(heartbeat_raw, "task heartbeat")
    )
    exit_code = _fact_optional_int(task, "exit_code", minimum=-256)
    timed_out = _fact_bool(task, "timed_out")
    cancelled = _fact_bool(task, "cancelled")
    observed_at = _supervisor_iso(response.get("observed_at"), "supervisor observation")
    return SandboxStatus(
        unit=manifest.unit,
        task_id=manifest.task_id,
        runtime_handle=manifest.runtime_handle,
        active_state=active_state,
        sub_state=sub_state,
        main_pid=main_pid,
        result=result,
        exit_status=exit_status,
        started=started,
        heartbeat_at=heartbeat_at,
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=cancelled,
        process_gone=process_gone,
        observed_at=observed_at,
    )


def _stop_from_document(unit: str, response: Mapping[str, object]) -> SandboxStop:
    unit_state = response.get("unit_state")
    if not isinstance(unit_state, Mapping):
        raise SupervisorError("helper_response_invalid", ambiguous=True)
    process_gone = _fact_bool(response, "process_gone")
    stop_confirmed = _fact_bool(response, "stop_confirmed")
    load_state = _fact_text(unit_state, "load_state")
    active_state = (
        "not-found"
        if load_state == "not-found"
        else _fact_text(unit_state, "active_state")
    )
    return SandboxStop(
        unit=unit,
        active_state=active_state,
        result=_fact_optional_text(unit_state, "result"),
        exit_status=_fact_optional_int(unit_state, "exit_status", minimum=-256),
        main_pid=_fact_optional_int(unit_state, "main_pid", minimum=0),
        process_gone=process_gone,
        stop_confirmed=bool(stop_confirmed and process_gone),
        observed_at=_supervisor_iso(
            response.get("observed_at"), "supervisor observation"
        ),
    )


def _supervisor_iso(value: object, label: str) -> datetime:
    try:
        return _parse_iso(value, label)
    except SandboxSpecError:
        raise SupervisorError("helper_response_invalid", ambiguous=True) from None


def _fact_text(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value or len(value) > 64:
        raise SupervisorError("helper_response_invalid", ambiguous=True)
    return value


def _fact_optional_text(data: Mapping[str, object], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 256:
        raise SupervisorError("helper_response_invalid", ambiguous=True)
    return value


def _fact_optional_int(
    data: Mapping[str, object], name: str, *, minimum: int
) -> int | None:
    value = data.get(name)
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise SupervisorError("helper_response_invalid", ambiguous=True)
    return value


def _fact_bool(data: Mapping[str, object], name: str) -> bool:
    value = data.get(name)
    if type(value) is not bool:
        raise SupervisorError("helper_response_invalid", ambiguous=True)
    return value


# ---------------------------------------------------------------------------
# managed adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Intent:
    """Adapter-local submission intent (never a DB record)."""

    digest: str
    state: str
    handle: str | None = None
    accepted_at: datetime | None = None


class _ManifestUnreadable(HermesManagedError):
    """A manifest exists for a known unit but fails validation.

    Distinguished from an absent manifest so status/result/cancel can report
    ``unknown`` instead of the definite ``not_found`` answer.
    """


class HermesManagedAdapter:
    """``RuntimeAdapter`` for Hermes in a transient systemd sandbox.

    The adapter is a fact reporter: it never writes task state, never calls
    ``complete_task`` and never fabricates success.  A handle is returned only
    after the helper reports an active unit *and* a started marker, and the
    task attestation has been verified against the trusted runtime root hash.
    """

    def __init__(
        self,
        config: HermesManagedConfig,
        supervisor: SandboxSupervisor,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, HermesManagedConfig):
            raise TypeError("managed adapter requires a managed configuration")
        if not isinstance(supervisor, SandboxSupervisor):
            raise TypeError("managed adapter requires a sandbox supervisor")
        self._config = config
        self._supervisor = supervisor
        self._clock = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._handles: dict[str, tuple[str, str]] = {}
        self._intents: dict[tuple[str, str], _Intent] = {}

    # -- capabilities / health ----------------------------------------------

    def capabilities(self, *, probe: bool = False) -> RuntimeCapabilities:
        """Return the fail-closed capability matrix.

        Only a runtime attestation whose HMAC verifies with the trusted key and
        whose name/version/root/evidence match the configuration upgrades a
        managed label.  Any helper/attestation/key failure leaves every managed
        entry ``unverified``; it never raises.
        """

        del probe  # a probe has the same read-only checks as the default path
        current = self._clock()
        verified = None
        try:
            attestation = self._supervisor.runtime_attestation()
            key = _read_key_file(self._config.attestation_key_path, "attestation key")
            verified = verify_runtime_attestation(
                attestation, key=key, config=self._config, now=current
            )
        except Exception:  # noqa: BLE001 - capabilities must fail closed
            verified = None
        return managed_capabilities(self._config, verified, now=current)

    def health(self) -> HealthResult:
        """Check helper, runtime root, broker socket and runtime attestation."""

        current = self._clock()
        details: dict[str, object] = {
            "helper": "unavailable",
            "runtime_root": "unavailable",
            "broker_socket": "unavailable",
            "attestation": "unverified",
        }
        status = HealthStatus.HEALTHY

        if _is_regular_executable(self._config.helper_path):
            details["helper"] = "available"
        else:
            status = HealthStatus.UNAVAILABLE

        if _is_readable_directory(self._config.runtime_root):
            details["runtime_root"] = "available"
        else:
            status = HealthStatus.UNAVAILABLE

        if _is_unix_socket(self._config.broker_socket):
            details["broker_socket"] = "available"
        elif status is HealthStatus.HEALTHY:
            status = HealthStatus.DEGRADED

        try:
            attestation = self._supervisor.runtime_attestation()
            key = _read_key_file(self._config.attestation_key_path, "attestation key")
            verify_runtime_attestation(
                attestation, key=key, config=self._config, now=current
            )
            details["attestation"] = "verified"
            details["helper_version"] = attestation.helper_version
        except SupervisorError as exc:
            details["attestation"] = exc.code
            if exc.code == "helper_unavailable" and status is HealthStatus.HEALTHY:
                status = HealthStatus.UNAVAILABLE
            elif status is HealthStatus.HEALTHY:
                status = HealthStatus.DEGRADED
        except Exception:  # noqa: BLE001 - health must return a fact
            details["attestation"] = "invalid"
            if status is HealthStatus.HEALTHY:
                status = HealthStatus.DEGRADED

        return HealthResult(status=status, checked_at=current, details=details)

    # -- submit / lookup -----------------------------------------------------

    def submit_task(
        self,
        task_id: str,
        prompt: str,
        idempotency_key: str,
        grant: RuntimeGrant,
        instructions: str | None = None,
    ) -> SubmitResult:
        """Submit one managed task and return a handle only after ``started``."""

        del instructions  # the managed prompt wrapper is fixed; extra text is ignored
        current = self._clock()
        problem = self._validate_submission(task_id, prompt, idempotency_key, grant, current)
        if problem is not None:
            reason, code = problem
            return _submit_error(SubmitOutcome.REJECTED, reason, code)
        try:
            wrapped = build_managed_prompt(
                prompt, max_bytes=self._config.prompt_max_bytes
            )
            profile_template = self._profile_template()
        except (PromptError, SandboxSpecError):
            return _submit_error(
                SubmitOutcome.REJECTED,
                "managed_prompt_invalid",
                ErrorCode.INVALID_REQUEST,
            )

        prompt_bytes = wrapped.encode("utf-8")
        prompt_sha256 = _sha256_hex(prompt_bytes)
        profile_sha256 = _sha256_hex(profile_template.encode("utf-8"))
        unit = unit_name_for(task_id, idempotency_key)
        handle = runtime_handle_for(unit)
        digest = _submission_digest(
            self._config,
            task_id=task_id,
            grant_id=grant.grant_id,
            idempotency_key=idempotency_key,
            prompt_sha256=prompt_sha256,
            prompt_bytes=len(prompt_bytes),
            profile_template_sha256=profile_sha256,
        )

        with self._lock:
            try:
                existing = read_manifest(self._config, unit)
            except SandboxSpecError:
                self._remember_intent(task_id, idempotency_key, digest, "unknown")
                return _submit_error(
                    SubmitOutcome.UNKNOWN,
                    "task_manifest_unreadable",
                    ErrorCode.UNKNOWN,
                )

            if existing is not None:
                if not self._manifest_matches(
                    existing,
                    task_id=task_id,
                    grant_id=grant.grant_id,
                    idempotency_key=idempotency_key,
                    prompt_sha256=prompt_sha256,
                    prompt_bytes=len(prompt_bytes),
                    profile_template_sha256=profile_sha256,
                ):
                    return _submit_error(
                        SubmitOutcome.CONFLICT,
                        "idempotency_key_payload_conflict",
                        ErrorCode.CONFLICT,
                    )
                return self._replay(
                    existing, task_id, idempotency_key, digest
                )

            prior = self._intents.get((task_id, idempotency_key))
            if prior is not None and prior.digest != digest:
                return _submit_error(
                    SubmitOutcome.CONFLICT,
                    "idempotency_key_payload_conflict",
                    ErrorCode.CONFLICT,
                )
            if prior is not None and prior.state != "rejected":
                # A prepared/launched or ambiguous intent already owns this
                # key.  Without the durable manifest we cannot prove whether a
                # unit exists, and a second prepare/launch is forbidden.
                return _submit_error(
                    SubmitOutcome.UNKNOWN,
                    "submission_intent_unresolved",
                    ErrorCode.UNKNOWN,
                )
            if _path_exists(_task_dir(self._config, unit)):
                self._remember_intent(task_id, idempotency_key, digest, "unknown")
                return _submit_error(
                    SubmitOutcome.UNKNOWN,
                    "task_directory_exists_without_manifest",
                    ErrorCode.UNKNOWN,
                )

            if not self._runtime_attestation_verified(current):
                return _submit_error(
                    SubmitOutcome.UNSUPPORTED,
                    "runtime_attestation_unverified",
                    ErrorCode.UNSUPPORTED,
                )

            try:
                _claims, token = self._mint_egress(unit, grant, current)
                spec = build_sandbox_spec(
                    self._config,
                    task_id=task_id,
                    grant_id=grant.grant_id,
                    idempotency_key=idempotency_key,
                    prompt=wrapped,
                    profile_template=profile_template,
                    egress_token=token,
                )
            except (HermesManagedError, EgressTokenError, ValueError):
                return _submit_error(
                    SubmitOutcome.REJECTED,
                    "managed_spec_invalid",
                    ErrorCode.UNAVAILABLE,
                )

            if spec.unit != unit or spec.runtime_handle != handle:
                return _submit_error(
                    SubmitOutcome.REJECTED,
                    "managed_spec_binding_mismatch",
                    ErrorCode.INTERNAL,
                )

            self._remember_intent(task_id, idempotency_key, digest, "preparing")

            try:
                self._supervisor.prepare(spec)
            except SupervisorError as exc:
                if exc.ambiguous:
                    self._remember_intent(task_id, idempotency_key, digest, "unknown")
                    return _submit_error(
                        SubmitOutcome.UNKNOWN, exc.code, ErrorCode.UNKNOWN
                    )
                self._remember_intent(task_id, idempotency_key, digest, "rejected")
                return _submit_error(
                    SubmitOutcome.REJECTED, exc.code, ErrorCode.UNAVAILABLE
                )

            try:
                manifest = read_manifest(self._config, spec.unit)
            except SandboxSpecError:
                manifest = None
            if manifest is None:
                self._remember_intent(task_id, idempotency_key, digest, "unknown")
                return _submit_error(
                    SubmitOutcome.UNKNOWN,
                    "task_manifest_unavailable",
                    ErrorCode.UNKNOWN,
                )

            try:
                self._write_task_files(spec, wrapped, profile_template, token)
            except (SandboxSpecError, OSError):
                self._remember_intent(task_id, idempotency_key, digest, "rejected")
                return _submit_error(
                    SubmitOutcome.REJECTED,
                    "task_files_unwritable",
                    ErrorCode.UNAVAILABLE,
                )

            try:
                attestation = self._supervisor.launch(spec)
            except SupervisorError as exc:
                if exc.ambiguous:
                    self._remember_intent(task_id, idempotency_key, digest, "unknown")
                    self._ambiguous_launch_cleanup(spec.unit)
                    return _submit_error(
                        SubmitOutcome.UNKNOWN, exc.code, ErrorCode.UNKNOWN
                    )
                self._remember_intent(task_id, idempotency_key, digest, "rejected")
                return _submit_error(
                    SubmitOutcome.REJECTED, exc.code, ErrorCode.UNAVAILABLE
                )

            try:
                key = _read_key_file(
                    self._config.attestation_key_path, "attestation key"
                )
                verify_task_attestation(
                    attestation,
                    key=key,
                    spec=spec,
                    expected_runtime_root_hash=self._config.runtime_root_hash,
                    now=self._clock(),
                )
            except Exception:  # noqa: BLE001 - any verification failure is unknown
                self._remember_intent(task_id, idempotency_key, digest, "unknown")
                self._revoke(manifest)
                self._ambiguous_launch_cleanup(spec.unit)
                return _submit_error(
                    SubmitOutcome.UNKNOWN,
                    "task_attestation_invalid",
                    ErrorCode.UNKNOWN,
                )

            self._handles[spec.runtime_handle] = (spec.unit, task_id)
            self._intents[(task_id, idempotency_key)] = _Intent(
                digest=digest,
                state="accepted",
                handle=spec.runtime_handle,
                accepted_at=current,
            )
            return SubmitResult(
                outcome=SubmitOutcome.ACCEPTED,
                runtime_task_id=spec.runtime_handle,
                accepted_at=current,
                reported_state=RuntimeState.RUNNING.value,
                replayed=False,
            )

    def find_by_idempotency_key(
        self,
        task_id: str,
        grant_id: str,
        idempotency_key: str,
        payload_hash: str | None = None,
    ) -> LookupResult:
        """Look up a durable manifest/unit or an adapter-local intent."""

        current = self._clock()
        if not _is_safe_id(task_id) or not _is_safe_id(grant_id) or not _is_safe_id(
            idempotency_key
        ):
            return _lookup_error(
                LookupOutcome.UNKNOWN, "lookup_request_invalid", current
            )
        if payload_hash is not None and (
            not isinstance(payload_hash, str) or not _SHA256_HEX_RE.fullmatch(payload_hash)
        ):
            return _lookup_error(
                LookupOutcome.UNKNOWN, "lookup_payload_hash_invalid", current
            )
        try:
            unit = unit_name_for(task_id, idempotency_key)
        except SandboxSpecError:
            return _lookup_error(LookupOutcome.UNKNOWN, "lookup_request_invalid", current)

        try:
            manifest = read_manifest(self._config, unit)
        except SandboxSpecError:
            return _lookup_error(
                LookupOutcome.UNKNOWN, "task_manifest_unreadable", current
            )

        if manifest is not None:
            if (
                manifest.task_id != task_id
                or manifest.grant_id != grant_id
                or manifest.idempotency_key != idempotency_key
            ):
                return _lookup_error(
                    LookupOutcome.CONFLICT, "idempotency_key_payload_conflict", current
                )
            if payload_hash is not None and payload_hash != manifest.payload_sha256:
                return _lookup_error(
                    LookupOutcome.CONFLICT, "idempotency_key_payload_conflict", current
                )
            try:
                status = self._supervisor.status(unit)
            except SupervisorError:
                return _lookup_error(
                    LookupOutcome.UNKNOWN, "runtime_status_unreadable", current
                )
            if status.active_state != "not-found" or status.started:
                self._handles[manifest.runtime_handle] = (unit, manifest.task_id)
                state, _ = _map_status(status)
                return LookupResult(
                    outcome=LookupOutcome.FOUND,
                    runtime_task_id=manifest.runtime_handle,
                    reported_state=state.value,
                    payload_hash=manifest.payload_sha256,
                    accepted_at=manifest.created_at,
                    observed_at=current,
                )
            return _lookup_error(
                LookupOutcome.UNKNOWN, "task_prepared_without_unit", current
            )

        with self._lock:
            intent = self._intents.get((task_id, idempotency_key))
        if intent is not None and intent.state == "rejected":
            return LookupResult(outcome=LookupOutcome.NOT_FOUND, observed_at=current)
        if intent is not None:
            # An accepted/ambiguous intent without its durable manifest cannot
            # prove the handle or the absence of side effects.  Never claim
            # found (or not_found) and never suggest a relaunch.
            return _lookup_error(
                LookupOutcome.UNKNOWN, "submission_intent_unresolved", current
            )
        return _lookup_error(LookupOutcome.UNKNOWN, "submission_intent_unproven", current)

    # -- status / result -----------------------------------------------------

    def get_status(
        self,
        runtime_task_id: str,
        *,
        task_id: str | None = None,
    ) -> StatusResult:
        """Return the mapped unit state, verifying the handle/task binding."""

        current = self._clock()
        try:
            resolved = self._resolve_handle(runtime_task_id, task_id)
        except _ManifestUnreadable:
            return StatusResult(
                runtime_task_id=runtime_task_id,
                state=RuntimeState.UNKNOWN,
                external_state=None,
                observed_at=current,
                stop_confirmed=False,
                runtime="hermes",
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message="task manifest is unreadable",
                    context={"reason": "task_manifest_unreadable"},
                ),
            )
        if resolved is None:
            return StatusResult(
                runtime_task_id=runtime_task_id,
                state=RuntimeState.NOT_FOUND,
                external_state="not_found",
                observed_at=current,
                stop_confirmed=False,
                error=RuntimeErrorInfo(
                    code=ErrorCode.NOT_FOUND,
                    message="runtime task is not known to this adapter",
                ),
            )
        unit, manifest = resolved
        try:
            status = self._supervisor.status(unit)
        except SupervisorError as exc:
            return StatusResult(
                runtime_task_id=runtime_task_id,
                state=RuntimeState.UNKNOWN,
                external_state=None,
                observed_at=current,
                stop_confirmed=False,
                runtime="hermes",
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message="runtime status is unreadable",
                    context={"reason": exc.code},
                ),
            )
        if (
            status.unit != unit
            or status.task_id != manifest.task_id
            or status.runtime_handle != runtime_task_id
        ):
            return StatusResult(
                runtime_task_id=runtime_task_id,
                state=RuntimeState.UNKNOWN,
                external_state=None,
                observed_at=current,
                stop_confirmed=False,
                runtime="hermes",
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message="runtime status is not bound to this handle",
                ),
            )
        state, error = _map_status(status)
        return StatusResult(
            runtime_task_id=runtime_task_id,
            state=state,
            external_state=_external_state(status),
            observed_at=status.observed_at,
            heartbeat_at=status.heartbeat_at,
            stop_confirmed=bool(status.process_gone),
            runtime="hermes",
            error=error,
        )

    def get_result(
        self,
        runtime_task_id: str,
        *,
        task_id: str | None = None,
    ) -> RuntimeResult:
        """Hash ``out/result.txt`` and return verifying evidence, never success."""

        current = self._clock()
        try:
            resolved = self._resolve_handle(runtime_task_id, task_id)
        except _ManifestUnreadable:
            return _result_error(
                ResultOutcome.UNKNOWN,
                stop_confirmed=False,
                code=ErrorCode.UNKNOWN,
                reason="task_manifest_unreadable",
            )
        if resolved is None:
            return _result_error(
                ResultOutcome.UNKNOWN,
                stop_confirmed=False,
                code=ErrorCode.NOT_FOUND,
                reason="runtime_task_unknown",
            )
        unit, manifest = resolved
        try:
            status = self._supervisor.status(unit)
        except SupervisorError as exc:
            return _result_error(
                ResultOutcome.UNKNOWN,
                stop_confirmed=False,
                code=ErrorCode.UNKNOWN,
                reason=exc.code,
            )

        if not status.process_gone:
            return _result_error(
                ResultOutcome.UNKNOWN,
                stop_confirmed=False,
                code=ErrorCode.UNKNOWN,
                reason="runtime_result_not_ready",
            )
        if status.active_state == "not-found" and not status.started:
            return _result_error(
                ResultOutcome.UNKNOWN,
                stop_confirmed=True,
                code=ErrorCode.UNKNOWN,
                reason="runtime_never_started",
            )

        try:
            artifact = read_artifact(
                _task_dir(self._config, unit),
                max_bytes=self._config.result_max_bytes,
            )
        except ArtifactError as exc:
            outcome = (
                ResultOutcome.NEEDS_HUMAN
                if status.process_gone
                else ResultOutcome.UNKNOWN
            )
            return _result_error(
                outcome,
                stop_confirmed=bool(status.process_gone),
                code=ErrorCode.UNKNOWN,
                reason=str(exc),
            )

        self._revoke(manifest)
        payload = {
            "relative_path": artifact.relative_path,
            "sha256": artifact.sha256_ref,
            "size": artifact.size,
        }
        return RuntimeResult(
            outcome=ResultOutcome.SUCCEEDED,
            result_ref=artifact.result_ref,
            summary=None,
            structured_payload=payload,
            artifacts=(dict(payload),),
            evidence_refs=(artifact.evidence_ref,),
            verification_hints=(
                "an independent verifier must hash out/result.txt before completion",
            ),
            stop_confirmed=bool(status.process_gone),
            completed_at=None,
            structured_result=True,
            output=None,
            usage=None,
            runtime="hermes",
        )

    # -- cancel / events / input --------------------------------------------

    def cancel_task(
        self,
        runtime_task_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> CancelResult:
        """Request a helper stop; confirmation requires the cgroup to be gone."""

        del idempotency_key, poll_timeout, poll_interval
        current = self._clock()
        try:
            resolved = self._resolve_handle(runtime_task_id, task_id)
        except _ManifestUnreadable:
            return CancelResult(
                outcome=CancelOutcome.UNKNOWN,
                stop_confirmed=False,
                observed_at=current,
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message="task manifest is unreadable",
                    context={"reason": "task_manifest_unreadable"},
                ),
            )
        if resolved is None:
            return CancelResult(
                outcome=CancelOutcome.UNKNOWN,
                stop_confirmed=False,
                observed_at=current,
                error=RuntimeErrorInfo(
                    code=ErrorCode.NOT_FOUND,
                    message="runtime task is not known to this adapter",
                ),
            )
        unit, manifest = resolved
        stop_reason = "cancelled"
        if reason is not None:
            if not isinstance(reason, str) or len(reason) > _MAX_REASON_TEXT:
                return CancelResult(
                    outcome=CancelOutcome.UNKNOWN,
                    stop_confirmed=False,
                    observed_at=current,
                    error=RuntimeErrorInfo(
                        code=ErrorCode.INVALID_REQUEST,
                        message="cancel reason is invalid",
                    ),
                )
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in reason):
                return CancelResult(
                    outcome=CancelOutcome.UNKNOWN,
                    stop_confirmed=False,
                    observed_at=current,
                    error=RuntimeErrorInfo(
                        code=ErrorCode.INVALID_REQUEST,
                        message="cancel reason is invalid",
                    ),
                )
            if reason.strip():
                stop_reason = reason

        prior_status: SandboxStatus | None = None
        try:
            prior_status = self._supervisor.status(unit)
        except SupervisorError:
            prior_status = None
        terminal_before = (
            prior_status is not None
            and _is_terminal_state(_map_status(prior_status)[0])
        )
        artifact_valid = bool(
            prior_status is not None
            and prior_status.process_gone
            and self._artifact_is_valid(manifest)
        )

        try:
            stop = self._supervisor.stop(unit, reason=stop_reason)
        except SupervisorError as exc:
            if prior_status is not None and prior_status.process_gone:
                self._revoke(manifest)
                return CancelResult(
                    outcome=CancelOutcome.ALREADY_TERMINAL,
                    stop_confirmed=True,
                    observed_at=prior_status.observed_at,
                    error=RuntimeErrorInfo(
                        code=ErrorCode.UNKNOWN,
                        message="stop was already terminal; helper stop failed",
                        context={"reason": exc.code},
                    ),
                )
            return CancelResult(
                outcome=CancelOutcome.UNKNOWN,
                stop_confirmed=False,
                observed_at=current,
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message="runtime stop outcome is unknown",
                    context={"reason": exc.code},
                ),
            )

        if stop.process_gone is not True:
            return CancelResult(
                outcome=CancelOutcome.REQUESTED,
                stop_confirmed=False,
                observed_at=stop.observed_at,
            )

        if not artifact_valid:
            artifact_valid = self._artifact_is_valid(manifest)
        self._revoke(manifest)
        outcome = (
            CancelOutcome.ALREADY_TERMINAL
            if terminal_before or artifact_valid
            else CancelOutcome.STOPPED
        )
        return CancelResult(
            outcome=outcome,
            stop_confirmed=True,
            observed_at=stop.observed_at,
        )

    def poll_events(
        self,
        runtime_task_id: str,
        cursor: str | None = None,
        max_events: int = 100,
        timeout: float = 30.0,
        *,
        task_id: str | None = None,
    ) -> EventBatch:
        """Hermes has no native event stream; return an explicit unsupported batch."""

        del runtime_task_id, cursor, max_events, timeout, task_id
        return EventBatch(
            events=(),
            next_cursor=None,
            has_more=False,
            error=RuntimeErrorInfo(
                code=ErrorCode.UNSUPPORTED,
                message="the managed Hermes runtime has no event stream",
            ),
        )

    def provide_input(
        self,
        runtime_task_id: str,
        input_request_id: str,
        payload_hash: str,
        payload: Mapping[str, object],
    ) -> InputResult:
        """The one-shot managed runtime never asks for input."""

        del runtime_task_id, input_request_id, payload_hash, payload
        return InputResult(
            outcome=InputOutcome.UNSUPPORTED,
            observed_at=self._clock(),
            error=RuntimeErrorInfo(
                code=ErrorCode.UNSUPPORTED,
                message="the managed Hermes runtime does not accept input",
            ),
        )

    # -- internal helpers ----------------------------------------------------

    def _validate_submission(
        self,
        task_id: object,
        prompt: object,
        idempotency_key: object,
        grant: object,
        now: datetime,
    ) -> tuple[str, ErrorCode] | None:
        if not _is_safe_id(task_id) or not _is_safe_id(idempotency_key):
            return "submission_binding_invalid", ErrorCode.INVALID_REQUEST
        if not isinstance(prompt, str) or not prompt.strip():
            return "managed_prompt_invalid", ErrorCode.INVALID_REQUEST
        if not isinstance(grant, RuntimeGrant):
            return "runtime_grant_invalid", ErrorCode.INVALID_REQUEST
        if not _is_safe_id(grant.grant_id):
            return "runtime_grant_invalid", ErrorCode.INVALID_REQUEST
        if grant.task_id != task_id:
            return "runtime_grant_task_mismatch", ErrorCode.FORBIDDEN
        if grant.execution_mode != "managed_execution":
            return "runtime_grant_mode_mismatch", ErrorCode.FORBIDDEN
        if (
            isinstance(grant.task_revision, bool)
            or not isinstance(grant.task_revision, int)
            or grant.task_revision < 1
        ):
            return "runtime_grant_revision_invalid", ErrorCode.INVALID_REQUEST
        if not _valid_capabilities(grant.capabilities):
            return "runtime_grant_capabilities_invalid", ErrorCode.INVALID_REQUEST
        if not isinstance(grant.resource_scope, Mapping):
            return "runtime_grant_scope_invalid", ErrorCode.INVALID_REQUEST
        expires_at = grant.expires_at
        if (
            not isinstance(expires_at, datetime)
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or expires_at <= now
        ):
            return "runtime_grant_expired", ErrorCode.FORBIDDEN
        if (
            type(self._config.runtime_max_sec) is not int
            or self._config.runtime_max_sec < MIN_RUNTIME_MAX_SEC
        ):
            return "runtime_budget_too_small", ErrorCode.INVALID_REQUEST
        if not isinstance(self._config.model_name, str) or not MODEL_NAME_PATTERN.fullmatch(
            self._config.model_name
        ):
            return "runtime_model_invalid", ErrorCode.INVALID_REQUEST
        if (
            type(self._config.egress_token_ttl_seconds) is not int
            or not 1
            <= self._config.egress_token_ttl_seconds
            <= MAX_EGRESS_TTL_SECONDS
        ):
            return "runtime_token_ttl_invalid", ErrorCode.INVALID_REQUEST
        return None

    def _runtime_attestation_verified(self, now: datetime) -> bool:
        """Verify the runtime attestation before creating any task directory."""

        try:
            attestation = self._supervisor.runtime_attestation()
            key = _read_key_file(self._config.attestation_key_path, "attestation key")
            verify_runtime_attestation(
                attestation, key=key, config=self._config, now=now
            )
        except Exception:  # noqa: BLE001 - an unverified runtime fails closed
            return False
        return True

    def _profile_template(self) -> str:
        template = self._config.local_proxy_base_url_template
        if (
            not isinstance(template, str)
            or template.count("{port}") != 1
            or "{" in template.replace("{port}", "")
            or "}" in template.replace("{port}", "")
        ):
            raise SandboxSpecError("local proxy base URL template is invalid")
        try:
            rendered = template.replace("{port}", _PROFILE_PORT_TOKEN)
        except (TypeError, ValueError):
            raise SandboxSpecError("local proxy base URL template is invalid") from None
        if rendered != f"http://127.0.0.1:{_PROFILE_PORT_TOKEN}/v1":
            raise SandboxSpecError(
                "local proxy base URL template is not the isolated loopback endpoint"
            )
        return (
            "model:\n"
            "  provider: custom\n"
            f'  base_url: "{rendered}"\n'
            '  name: "__MODEL__"\n'
        )

    def _mint_egress(
        self, unit: str, grant: RuntimeGrant, now: datetime
    ) -> tuple[EgressClaims, str]:
        key = _read_key_file(self._config.egress_token_key_path, "egress token key")
        claims = EgressClaims(
            version=1,
            task_id=grant.task_id,
            grant_id=grant.grant_id,
            unit=unit,
            model=self._config.model_name,
            provider_host=self._config.egress_provider_host,
            path_prefix=self._config.egress_path_prefix,
            issued_at=now,
            expires_at=now
            + timedelta(seconds=self._config.egress_token_ttl_seconds),
            nonce=secrets.token_hex(16),
        )
        token = mint_egress_token(claims, key=key, now=now)
        return claims, token

    def _write_task_files(
        self,
        spec: SandboxSpec,
        prompt: str,
        profile_template: str,
        egress_token: str,
    ) -> None:
        _write_task_file(
            spec.task_dir, "prompt.txt", prompt.encode("utf-8"), _PROMPT_FILE_MODE
        )
        _write_task_file(
            spec.task_dir,
            "profile.template.yaml",
            profile_template.encode("utf-8"),
            _PROFILE_FILE_MODE,
        )
        _write_task_file(
            spec.task_dir,
            "egress.token",
            egress_token.encode("ascii"),
            _TOKEN_FILE_MODE,
        )

    def _manifest_matches(
        self,
        manifest: SandboxManifest,
        *,
        task_id: str,
        grant_id: str,
        idempotency_key: str,
        prompt_sha256: str,
        prompt_bytes: int,
        profile_template_sha256: str,
    ) -> bool:
        return (
            manifest.task_id == task_id
            and manifest.grant_id == grant_id
            and manifest.idempotency_key == idempotency_key
            and manifest.runtime_root_hash == self._config.runtime_root_hash
            and manifest.prompt_sha256 == prompt_sha256
            and manifest.prompt_bytes == prompt_bytes
            and manifest.profile_template_sha256 == profile_template_sha256
            and manifest.ttl_seconds == self._config.egress_token_ttl_seconds
            and manifest.runtime_max_sec == self._config.runtime_max_sec
            and manifest.memory_max_mb == self._config.memory_max_mb
            and manifest.cpu_quota_percent == self._config.cpu_quota_percent
            and manifest.tasks_max == self._config.tasks_max
            and (manifest.model or None) == (self._config.model_name or None)
        )

    def _replay(
        self,
        manifest: SandboxManifest,
        task_id: str,
        idempotency_key: str,
        digest: str,
    ) -> SubmitResult:
        try:
            status = self._supervisor.status(manifest.unit)
        except SupervisorError as exc:
            self._remember_intent(task_id, idempotency_key, digest, "unknown")
            return _submit_error(
                SubmitOutcome.UNKNOWN, exc.code, ErrorCode.UNKNOWN
            )
        if status.started:
            self._handles[manifest.runtime_handle] = (manifest.unit, manifest.task_id)
            return SubmitResult(
                outcome=SubmitOutcome.ACCEPTED,
                runtime_task_id=manifest.runtime_handle,
                accepted_at=manifest.created_at,
                reported_state=_map_status(status)[0].value,
                replayed=True,
            )
        self._remember_intent(task_id, idempotency_key, digest, "unknown")
        return _submit_error(
            SubmitOutcome.UNKNOWN,
            "task_prepared_without_started",
            ErrorCode.UNKNOWN,
        )

    def _ambiguous_launch_cleanup(self, unit: str) -> None:
        try:
            self._supervisor.stop(unit, reason="launch_ambiguous")
        except SupervisorError:
            pass
        try:
            self._supervisor.cleanup(unit, result_read=False)
        except SupervisorError:
            pass

    def _remember_intent(
        self,
        task_id: str,
        idempotency_key: str,
        digest: str,
        state: str,
        *,
        handle: str | None = None,
        accepted_at: datetime | None = None,
    ) -> None:
        with self._lock:
            previous = self._intents.get((task_id, idempotency_key))
            if (
                previous is not None
                and previous.state == "accepted"
                and state != "accepted"
            ):
                return
            self._intents[(task_id, idempotency_key)] = _Intent(
                digest=digest,
                state=state,
                handle=handle if handle is not None else getattr(previous, "handle", None),
                accepted_at=(
                    accepted_at
                    if accepted_at is not None
                    else getattr(previous, "accepted_at", None)
                ),
            )

    def _resolve_handle(
        self, handle: object, task_id: str | None
    ) -> tuple[str, SandboxManifest] | None:
        if not isinstance(handle, str) or not _SAFE_HANDLE_RE.fullmatch(handle):
            return None
        with self._lock:
            known = self._handles.get(handle)
        if known is not None:
            unit, bound_task = known
            if task_id is not None and task_id != bound_task:
                return None
            try:
                manifest = read_manifest(self._config, unit)
            except SandboxSpecError:
                raise _ManifestUnreadable("task manifest is unreadable") from None
            if manifest is None:
                return None
            if (
                manifest.unit != unit
                or manifest.runtime_handle != handle
                or manifest.task_id != bound_task
            ):
                raise _ManifestUnreadable("task manifest is not bound to this handle")
            return unit, manifest

        try:
            truncated = False
            unreadable_sibling = False
            with os.scandir(self._config.task_root) as entries:
                seen = 0
                for entry in entries:
                    seen += 1
                    if seen > _MAX_TASK_SCAN:
                        truncated = True
                        break
                    unit = entry.name
                    if not _UNIT_RE.fullmatch(unit):
                        continue
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        unreadable_sibling = True
                        continue
                    try:
                        manifest = read_manifest(self._config, unit)
                    except SandboxSpecError:
                        # An unreadable sibling is skipped so one bad task
                        # directory cannot hide every later healthy task.  If
                        # the whole scan finds no match, the caller still gets
                        # ``unknown`` (never ``not_found``).
                        unreadable_sibling = True
                        continue
                    if manifest is None:
                        # A unit-shaped directory without a usable manifest
                        # cannot prove the handle is absent either.
                        unreadable_sibling = True
                        continue
                    if (
                        manifest.runtime_handle == handle
                        and (task_id is None or manifest.task_id == task_id)
                    ):
                        with self._lock:
                            self._handles[handle] = (unit, manifest.task_id)
                        return unit, manifest
            if truncated:
                # The scan stopped at the entry cap, so a handle in the
                # unscanned tail cannot be ruled out.
                raise _ManifestUnreadable("task root scan was truncated")
            if unreadable_sibling:
                raise _ManifestUnreadable(
                    "a task manifest in the task root is unreadable"
                )
        except (FileNotFoundError, NotADirectoryError, OSError):
            return None
        return None

    def _artifact_is_valid(self, manifest: SandboxManifest) -> bool:
        try:
            read_artifact(
                _task_dir(self._config, manifest.unit),
                max_bytes=self._config.result_max_bytes,
            )
        except ArtifactError:
            return False
        return True

    def _revoke(self, manifest: SandboxManifest) -> bool:
        directory = self._config.revocation_dir
        if directory is None:
            return True
        current = self._clock()
        claims = EgressClaims(
            version=1,
            task_id=manifest.task_id,
            grant_id=manifest.grant_id,
            unit=manifest.unit,
            model=manifest.model or self._config.model_name,
            provider_host=self._config.egress_provider_host,
            path_prefix=self._config.egress_path_prefix,
            issued_at=current,
            expires_at=current + timedelta(seconds=1),
            nonce="revocation",
        )
        try:
            write_revocation_markers(Path(directory), claims, manifest.egress_token_sha256)
        except (OSError, ValueError):
            return False
        return True


# ---------------------------------------------------------------------------
# shared helpers for the adapter/supervisor
# ---------------------------------------------------------------------------


def _task_dir(config: HermesManagedConfig, unit: str) -> Path:
    return Path(config.task_root) / unit


def _submission_digest(
    config: HermesManagedConfig,
    *,
    task_id: str,
    grant_id: str,
    idempotency_key: str,
    prompt_sha256: str,
    prompt_bytes: int,
    profile_template_sha256: str,
) -> str:
    document = {
        "task_id": task_id,
        "grant_id": grant_id,
        "idempotency_key": idempotency_key,
        "runtime_root_hash": config.runtime_root_hash,
        "prompt_sha256": prompt_sha256,
        "prompt_bytes": prompt_bytes,
        "profile_template_sha256": profile_template_sha256,
        "ttl_seconds": config.egress_token_ttl_seconds,
        "runtime_max_sec": config.runtime_max_sec,
        "memory_max_mb": config.memory_max_mb,
        "cpu_quota_percent": config.cpu_quota_percent,
        "tasks_max": config.tasks_max,
        "model": config.model_name,
        "provider_host": config.egress_provider_host,
        "path_prefix": config.egress_path_prefix,
    }
    return _sha256_hex(_canonical_json(document))


def _map_status(status: SandboxStatus) -> tuple[RuntimeState, RuntimeErrorInfo | None]:
    if status.active_state == "not-found" and not status.started:
        return RuntimeState.NOT_FOUND, RuntimeErrorInfo(
            code=ErrorCode.NOT_FOUND, message="runtime unit was not found"
        )
    if status.timed_out and status.process_gone:
        return RuntimeState.TIMED_OUT, None
    if status.cancelled and status.process_gone:
        return RuntimeState.STOPPED, None
    if status.active_state == "failed" and status.process_gone:
        return RuntimeState.FAILED, None
    if status.process_gone:
        if not status.started:
            return RuntimeState.STOPPED, None
        if status.exit_code is not None:
            if status.exit_code == 0:
                return RuntimeState.COMPLETED, None
            return RuntimeState.FAILED, None
        if status.exit_status is not None and status.exit_status != 0:
            # systemd observed a non-zero exit but the workload never wrote its
            # exit marker: never report success.
            return RuntimeState.FAILED, None
        # A missing exit marker is not proof that the run completed.  A
        # transient unit is unloaded after success, so ``not-found`` with
        # ``started=True`` and no exit marker is still unknown, not success.
        return RuntimeState.UNKNOWN, RuntimeErrorInfo(
            code=ErrorCode.UNKNOWN,
            message="runtime exited without a workload exit marker",
        )
    if status.active_state in {"inactive", "failed"} and not status.started:
        return RuntimeState.STOPPED, None
    if status.started:
        return RuntimeState.RUNNING, None
    return RuntimeState.ACCEPTED, None


def _external_state(status: SandboxStatus) -> str:
    if status.sub_state:
        return f"{status.active_state}:{status.sub_state}"
    return status.active_state


def _is_terminal_state(state: RuntimeState) -> bool:
    return state in {
        RuntimeState.COMPLETED,
        RuntimeState.FAILED,
        RuntimeState.STOPPED,
        RuntimeState.TIMED_OUT,
        RuntimeState.NOT_FOUND,
    }


def _submit_error(
    outcome: SubmitOutcome, reason: str, code: ErrorCode
) -> SubmitResult:
    return SubmitResult(
        outcome=outcome,
        error=RuntimeErrorInfo(
            code=code,
            message=_SUBMIT_ERROR_MESSAGES.get(reason, "managed submission was not accepted"),
            context={"reason": reason},
        ),
    )


def _lookup_error(
    outcome: LookupOutcome, reason: str, now: datetime
) -> LookupResult:
    return LookupResult(
        outcome=outcome,
        observed_at=now,
        error=RuntimeErrorInfo(
            code=(
                ErrorCode.CONFLICT
                if outcome is LookupOutcome.CONFLICT
                else ErrorCode.UNKNOWN
            ),
            message=_LOOKUP_ERROR_MESSAGES.get(reason, "runtime lookup is inconclusive"),
            context={"reason": reason},
        ),
    )


def _result_error(
    outcome: ResultOutcome,
    *,
    stop_confirmed: bool,
    code: ErrorCode,
    reason: str,
) -> RuntimeResult:
    return RuntimeResult(
        outcome=outcome,
        result_ref=None,
        summary=None,
        structured_payload=None,
        artifacts=(),
        evidence_refs=(),
        verification_hints=(),
        stop_confirmed=stop_confirmed,
        completed_at=None,
        structured_result=False,
        runtime="hermes",
        error=RuntimeErrorInfo(
            code=code,
            message="runtime result is not usable",
            context={"reason": reason},
        ),
    )


_SUBMIT_ERROR_MESSAGES = {
    "submission_binding_invalid": "task or idempotency binding is invalid",
    "managed_prompt_invalid": "managed prompt is invalid",
    "runtime_grant_invalid": "runtime Grant is invalid",
    "runtime_grant_task_mismatch": "runtime Grant is bound to a different task",
    "runtime_grant_mode_mismatch": "runtime Grant is not for managed execution",
    "runtime_grant_revision_invalid": "runtime Grant revision is invalid",
    "runtime_grant_capabilities_invalid": "runtime Grant capabilities are invalid",
    "runtime_grant_scope_invalid": "runtime Grant scope is invalid",
    "runtime_grant_expired": "runtime Grant has expired",
    "runtime_budget_too_small": "runtime budget is below the managed minimum",
    "runtime_model_invalid": "configured model name is invalid",
    "runtime_token_ttl_invalid": "egress token TTL is invalid",
    "managed_spec_invalid": "managed sandbox spec could not be built",
    "managed_spec_binding_mismatch": "managed sandbox spec binding is inconsistent",
    "task_manifest_unreadable": "task manifest is unreadable",
    "task_manifest_unavailable": "task manifest is unavailable after prepare",
    "task_directory_exists_without_manifest": "task directory exists without a manifest",
    "task_files_unwritable": "managed task files could not be written",
    "task_attestation_invalid": "task attestation could not be verified",
    "task_prepared_without_unit": "task was prepared but no unit/started marker exists",
    "task_prepared_without_started": "task was prepared but the started marker is missing",
    "submission_intent_unresolved": "an earlier submission intent is unresolved",
    "runtime_attestation_unverified": "runtime attestation is not verified",
    "idempotency_key_payload_conflict": "idempotency key was used with a different payload",
}
_LOOKUP_ERROR_MESSAGES = {
    "lookup_request_invalid": "lookup binding is invalid",
    "lookup_payload_hash_invalid": "lookup payload hash is invalid",
    "task_manifest_unreadable": "task manifest is unreadable",
    "runtime_status_unreadable": "runtime status is unreadable",
    "task_prepared_without_unit": "task was prepared but no unit/started marker exists",
    "submission_intent_unproven": "submission intent cannot be proven",
    "submission_intent_unresolved": "an earlier submission intent is unresolved",
    "idempotency_key_payload_conflict": "idempotency key was used with a different payload",
}


def _valid_capabilities(value: object) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        return False
    items = tuple(value)
    return bool(items) and all(
        isinstance(item, str) and bool(item.strip()) for item in items
    )


def _is_safe_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_ID_TEXT
        and value == value.strip()
        and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    )


def _path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_key_file(path: object, label: str) -> bytes:
    if not isinstance(path, (str, os.PathLike)):
        raise HermesManagedError(f"{label} is unavailable")
    candidate = Path(path)
    try:
        info = os.lstat(candidate)
    except OSError:
        raise HermesManagedError(f"{label} is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HermesManagedError(f"{label} is not a regular file")
    if not _MIN_KEY_BYTES <= info.st_size <= _MAX_KEY_BYTES:
        raise HermesManagedError(f"{label} has an invalid size")
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise HermesManagedError(f"{label} is unavailable") from None
    try:
        raw = _read_fd_bytes(descriptor, _MAX_KEY_BYTES + 1)
    finally:
        os.close(descriptor)
    if not _MIN_KEY_BYTES <= len(raw) <= _MAX_KEY_BYTES:
        raise HermesManagedError(f"{label} has an invalid size")
    return raw


def _read_fd_bytes(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = os.read(descriptor, limit - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _sandbox_gid() -> int | None:
    try:
        return grp.getgrnam(SANDBOX_GROUP).gr_gid
    except KeyError:
        # Unit tests and unprovisioned hosts have no sandbox group; Task 9
        # provisioning creates it.  The mode is still enforced exactly.
        return None


def _write_task_file(task_dir: Path, name: str, data: bytes, mode: int) -> None:
    if name not in {"prompt.txt", "profile.template.yaml", "egress.token"}:
        raise SandboxSpecError("task file name is not allowed")
    try:
        directory_fd = os.open(
            task_dir,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise SandboxSpecError("task directory is unavailable") from None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            raise SandboxSpecError("task file already exists") from None
        except OSError:
            raise SandboxSpecError("task file cannot be created") from None
        try:
            try:
                os.fchmod(descriptor, mode)
                gid = _sandbox_gid()
                if gid is not None:
                    os.fchown(descriptor, -1, gid)
            except OSError:
                raise SandboxSpecError("task file permissions cannot be set") from None
            total = 0
            while total < len(data):
                written = os.write(descriptor, data[total:])
                if written <= 0:
                    raise SandboxSpecError("task file write failed")
                total += written
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _is_regular_executable(path: object) -> bool:
    try:
        info = os.lstat(Path(path))
    except (OSError, TypeError, ValueError):
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and bool(info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    )


def _is_readable_directory(path: object) -> bool:
    try:
        info = os.lstat(Path(path))
    except (OSError, TypeError, ValueError):
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    try:
        return os.access(Path(path), os.R_OK | os.X_OK)
    except OSError:
        return False


def _is_unix_socket(path: object) -> bool:
    try:
        info = os.lstat(Path(path))
    except (OSError, TypeError, ValueError):
        return False
    return stat.S_ISSOCK(info.st_mode)


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
