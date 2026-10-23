"""Trusted boundary model for managed execution (Issue #82).

This module is the Python projection of design §7 and §11.  It is deliberately
**inert**: no process, filesystem or network operation happens here.  It only

* validates and normalises a server-resolved :class:`ManagedTaskSpec`,
* derives the per-task unit name (no user-controlled unit names),
* projects a live :class:`GrantStatus` from the authoritative Grant source,
* carries a truthful :class:`BoundaryClaim` of what the substrate enforces,
  cannot enforce, or has not yet verified,
* describes launch/stop/cleanup evidence without ever conflating
  ``process_gone``, ``stop_confirmed`` and ``never_started``.

Safety rules
------------

* ``ManagedTaskSpec.from_mapping`` accepts **exactly** the documented keys and
  rejects unknown or missing ones; there is no path/workspace/state field and
  no caller-supplied Grant expiry.
* Error text comes only from :class:`SecurityError` with a stable detail token;
  no path, token or host value is interpolated.
* :data:`ENFORCED_BOUNDARY`, :data:`BYPASSABLE_BOUNDARY` and
  :data:`DEFERRED_BOUNDARY` are module constants derived from design §15.1–§15.3.
  They are not derived from task input.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.security.errors import SecurityError

__all__ = [
    "BACKEND_FILE",
    "BYPASSABLE_BOUNDARY",
    "CONTRACT_VERSION",
    "DEFERRED_BOUNDARY",
    "ENFORCED_BOUNDARY",
    "EXECUTION_MODE_MANAGED",
    "MAX_CAPABILITIES",
    "MAX_CPU_QUOTA_PERCENT",
    "MAX_MEMORY_MAX_MB",
    "MAX_TASKS_MAX",
    "MAX_TTL_SECONDS",
    "MIN_CPU_QUOTA_PERCENT",
    "MIN_MEMORY_MAX_MB",
    "MIN_TASKS_MAX",
    "MIN_TTL_SECONDS",
    "NETWORK_PROFILES",
    "NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK",
    "NETWORK_PROFILE_PRIVATE_NETNS",
    "PRODUCT_CLAIM",
    "SUBSTRATE",
    "BoundaryClaim",
    "CleanupResult",
    "GrantStatus",
    "GrantStatusProvider",
    "LaunchHandle",
    "ManagedTaskSpec",
    "ResourceScope",
    "RuntimeObservation",
    "StopResult",
    "unit_name_for_task",
]

#: Version of the boundary declaration carried on every launch.
CONTRACT_VERSION = "1.0.0"

#: The only substrate this claim is made for.
SUBSTRATE = "systemd-transient-dynamicuser"

NETWORK_PROFILE_PRIVATE_NETNS = "private_netns"
NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK = "af_unix_only_fallback"
NETWORK_PROFILES: frozenset[str] = frozenset(
    {NETWORK_PROFILE_PRIVATE_NETNS, NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK}
)

#: Fixed product statement until #83 ships and is verified with real evidence.
PRODUCT_CLAIM = "synthetic_substrate_only"

EXECUTION_MODE_MANAGED = "managed_execution"
BACKEND_FILE = "file"

MIN_TTL_SECONDS = 1
MAX_TTL_SECONDS = 300
MIN_CPU_QUOTA_PERCENT = 1
MAX_CPU_QUOTA_PERCENT = 100
MIN_MEMORY_MAX_MB = 32
MAX_MEMORY_MAX_MB = 1024
MIN_TASKS_MAX = 1
MAX_TASKS_MAX = 64
MAX_CAPABILITIES = 16

#: Design §15.1: properties this substrate enforces.
ENFORCED_BOUNDARY: tuple[str, ...] = (
    "per_task_dynamic_uid",
    "filesystem_read_only",
    "no_host_home_access",
    "separate_private_tmp",
    "no_capabilities",
    "no_new_privileged_namespaces",
    "no_ip_network",
    "gateway_socket_only_external_entry",
    "resource_and_time_limits",
    "cross_task_state_isolation",
    "task_bound_revocable_credential",
    "stop_and_exit_evidence_separated",
    "gateway_admission_limits",
    "execution_mode_cross_checked",
)

#: Design §15.2: paths this substrate cannot eliminate.
BYPASSABLE_BOUNDARY: tuple[str, ...] = (
    "host_root_or_controller_sudo",
    "systemd_or_kernel_vulnerability",
    "side_channels",
    "physical_access",
    "gateway_backend_defect",
    "resource_path_without_gateway",
    "same_host_user_gateway_socket_connect",
    "same_uid_child_processes",
    "uid_reuse_window",
)

#: Design §15.3: capabilities deferred to #83 or otherwise unverified.
DEFERRED_BOUNDARY: tuple[str, ...] = (
    "hermes_internal_tools_in_sandbox",
    "model_provider_egress",
    "browser_and_terminal_governance",
    "advisory_governed_client_local_tools",
    "real_database_or_object_store_backend",
    "multi_host_or_multi_tenant_scheduling",
)

_UNIT_PREFIX = "agentmgr-task-"
_UNIT_DOMAIN = "agentmgr-task:v1:"
_UNIT_PATTERN = re.compile(r"^agentmgr-task-[0-9a-f]{12}$")
_TASK_ID_PATTERN = re.compile(r"^task_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_GRANT_ID_PATTERN = re.compile(r"^grnt_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*@\d+\.\d+\.\d+$")
_SUBJECT_PATTERN = re.compile(r"^(prin|svc|rt)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

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
_GRANT_STATUSES = frozenset({"active", "revoked", "consumed"})


def _safe_text(value: object, detail: str) -> str:
    """Return a non-empty, control-character-free string or fail closed."""

    if not isinstance(value, str) or not value or _CONTROL_CHARS.search(value):
        raise SecurityError("invalid_spec", detail)
    return value


def _exact_int(value: object, detail: str) -> int:
    """Return an int, rejecting ``bool`` (a subclass of ``int``)."""

    if type(value) is not int:
        raise SecurityError("invalid_spec", detail)
    return value


def _string_tuple(
    value: object, detail: str, *, unique: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise SecurityError("invalid_spec", detail)
    items = tuple(_safe_text(item, detail) for item in value)
    if unique and len(set(items)) != len(items):
        raise SecurityError("invalid_spec", detail)
    return items


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """Frozen projection of the Grant ``resource_scope`` object.

    An empty collection means "no resources of this kind", never "all".  The MVP
    only accepts ``scope_type="self"`` with no connectors and ``internal`` data
    (enforced by :meth:`ManagedTaskSpec.validate`).
    """

    scope_type: str
    resource_ids: tuple[str, ...]
    connector_ids: tuple[str, ...]
    data_classifications: tuple[str, ...]
    exclude_resource_ids: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResourceScope:
        """Build a scope from an exact-key mapping; unknown keys are rejected."""

        if not isinstance(value, Mapping):
            raise SecurityError("invalid_spec", "resource_scope_invalid")
        keys = set(value)
        if keys - _SCOPE_FIELDS:
            raise SecurityError("invalid_spec", "unknown_field")
        if _SCOPE_FIELDS - keys:
            raise SecurityError("invalid_spec", "missing_field")
        return cls(
            scope_type=_safe_text(value["scope_type"], "resource_scope_invalid"),
            resource_ids=_string_tuple(
                value["resource_ids"], "resource_scope_invalid", unique=True
            ),
            connector_ids=_string_tuple(
                value["connector_ids"], "resource_scope_invalid", unique=True
            ),
            data_classifications=_string_tuple(
                value["data_classifications"], "resource_scope_invalid", unique=True
            ),
            exclude_resource_ids=_string_tuple(
                value["exclude_resource_ids"], "resource_scope_invalid", unique=True
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready projection in the Grant schema's field order."""

        return {
            "scope_type": self.scope_type,
            "resource_ids": list(self.resource_ids),
            "connector_ids": list(self.connector_ids),
            "data_classifications": list(self.data_classifications),
            "exclude_resource_ids": list(self.exclude_resource_ids),
        }

    def is_subset_of(self, other: ResourceScope) -> bool:
        """Return whether this scope widens nothing relative to ``other``.

        ``scope_type`` must match, resource/connector/classification sets may
        shrink, and the exclusion set may only grow: dropping an exclusion is a
        scope expansion (design §7.2).
        """

        if not isinstance(other, ResourceScope):
            return False
        return (
            self.scope_type == other.scope_type
            and set(self.resource_ids) <= set(other.resource_ids)
            and set(self.connector_ids) <= set(other.connector_ids)
            and set(self.data_classifications) <= set(other.data_classifications)
            and set(other.exclude_resource_ids) <= set(self.exclude_resource_ids)
        )


@dataclass(frozen=True, slots=True)
class ManagedTaskSpec:
    """Validated, server-resolved request to start one managed task.

    The spec carries no path, workspace, state directory or Grant expiry.  The
    unit's only writable location is systemd's injected ``$STATE_DIRECTORY`` and
    the authoritative expiry comes from :class:`GrantStatusProvider`.
    """

    task_id: str
    grant_id: str
    execution_mode: str
    ttl_seconds: int
    cpu_quota_percent: int
    memory_max_mb: int
    tasks_max: int
    capabilities: tuple[str, ...]
    resource_scope: ResourceScope
    backend: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ManagedTaskSpec:
        """Build a spec from an exact-key mapping (structure only).

        Value rules that depend on time or Grant state live in
        :meth:`validate`; this method enforces shape, exact keys, ID patterns
        and string safety so an invalid spec never reaches the launch path.
        """

        if not isinstance(value, Mapping):
            raise SecurityError("invalid_spec", "spec_invalid")
        keys = set(value)
        if keys - _SPEC_FIELDS:
            raise SecurityError("invalid_spec", "unknown_field")
        if _SPEC_FIELDS - keys:
            raise SecurityError("invalid_spec", "missing_field")

        task_id = _safe_text(value["task_id"], "invalid_value")
        if _TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise SecurityError("invalid_spec", "task_id_invalid")
        grant_id = _safe_text(value["grant_id"], "invalid_value")
        if _GRANT_ID_PATTERN.fullmatch(grant_id) is None:
            raise SecurityError("invalid_spec", "grant_id_invalid")

        scope_value = value["resource_scope"]
        if isinstance(scope_value, ResourceScope):
            scope = scope_value
        elif isinstance(scope_value, Mapping):
            scope = ResourceScope.from_mapping(scope_value)
        else:
            raise SecurityError("invalid_spec", "invalid_value")

        return cls(
            task_id=task_id,
            grant_id=grant_id,
            execution_mode=_safe_text(value["execution_mode"], "invalid_value"),
            ttl_seconds=_exact_int(value["ttl_seconds"], "invalid_type"),
            cpu_quota_percent=_exact_int(
                value["cpu_quota_percent"], "invalid_type"
            ),
            memory_max_mb=_exact_int(value["memory_max_mb"], "invalid_type"),
            tasks_max=_exact_int(value["tasks_max"], "invalid_type"),
            capabilities=_string_tuple(value["capabilities"], "invalid_value"),
            resource_scope=scope,
            backend=_safe_text(value["backend"], "invalid_value"),
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready projection that round-trips via ``from_mapping``."""

        return {
            "task_id": self.task_id,
            "grant_id": self.grant_id,
            "execution_mode": self.execution_mode,
            "ttl_seconds": self.ttl_seconds,
            "cpu_quota_percent": self.cpu_quota_percent,
            "memory_max_mb": self.memory_max_mb,
            "tasks_max": self.tasks_max,
            "capabilities": list(self.capabilities),
            "resource_scope": self.resource_scope.as_dict(),
            "backend": self.backend,
        }

    def validate(self, now: datetime) -> None:
        """Enforce mode, time, limits, capability and MVP scope invariants.

        ``now`` must be timezone-aware so later TTL arithmetic cannot mix naive
        and aware datetimes.  This method has no side effects and never mutates
        the spec; on failure it raises :class:`SecurityError` before a
        credential is issued or a unit is started.
        """

        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")

        if self.execution_mode != EXECUTION_MODE_MANAGED:
            raise SecurityError(
                "execution_mode_mismatch", "managed_execution_required"
            )
        if not MIN_TTL_SECONDS <= self.ttl_seconds <= MAX_TTL_SECONDS:
            raise SecurityError("invalid_spec", "ttl_seconds_out_of_range")
        if not MIN_CPU_QUOTA_PERCENT <= self.cpu_quota_percent <= MAX_CPU_QUOTA_PERCENT:
            raise SecurityError("invalid_spec", "cpu_quota_percent_out_of_range")
        if not MIN_MEMORY_MAX_MB <= self.memory_max_mb <= MAX_MEMORY_MAX_MB:
            raise SecurityError("invalid_spec", "memory_max_mb_out_of_range")
        if not MIN_TASKS_MAX <= self.tasks_max <= MAX_TASKS_MAX:
            raise SecurityError("invalid_spec", "tasks_max_out_of_range")

        capabilities = self.capabilities
        if not capabilities:
            raise SecurityError("capability_not_granted", "capability_empty")
        if len(capabilities) > MAX_CAPABILITIES:
            raise SecurityError("capability_not_granted", "capability_too_many")
        if len(set(capabilities)) != len(capabilities):
            raise SecurityError("capability_not_granted", "capability_duplicate")
        for capability in capabilities:
            if (
                not isinstance(capability, str)
                or _CAPABILITY_PATTERN.fullmatch(capability) is None
            ):
                raise SecurityError("capability_not_granted", "capability_invalid")

        if self.backend != BACKEND_FILE:
            raise SecurityError("invalid_spec", "backend_not_allowed")

        scope = self.resource_scope
        if scope.scope_type != "self":
            raise SecurityError("resource_scope_denied", "scope_type_denied")
        if scope.connector_ids:
            raise SecurityError("resource_scope_denied", "connector_scope_denied")
        if not set(scope.data_classifications) <= {"internal"}:
            raise SecurityError(
                "resource_scope_denied", "classification_denied"
            )


@dataclass(frozen=True, slots=True)
class BoundaryClaim:
    """What this host's substrate actually enforces at launch time.

    ``enforced``/``bypassable``/``deferred`` come from module constants and are
    not overridable by task input.  ``product_claim`` is fixed until #83 is
    verified with real evidence.
    """

    contract_version: str
    substrate: str
    network_profile: str
    product_claim: str
    enforced: tuple[str, ...]
    bypassable: tuple[str, ...]
    deferred: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.product_claim != PRODUCT_CLAIM:
            raise ValueError(f"product_claim must be {PRODUCT_CLAIM!r}")
        if self.network_profile not in NETWORK_PROFILES:
            raise ValueError(f"unknown network profile: {self.network_profile!r}")
        for name in ("enforced", "bypassable", "deferred"):
            section = getattr(self, name)
            if (
                not isinstance(section, tuple)
                or not section
                or any(not isinstance(item, str) or not item for item in section)
            ):
                raise ValueError(f"{name} must be a non-empty tuple of strings")

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready claim for launch audit and evidence files."""

        return {
            "contract_version": self.contract_version,
            "substrate": self.substrate,
            "network_profile": self.network_profile,
            "product_claim": self.product_claim,
            "enforced": list(self.enforced),
            "bypassable": list(self.bypassable),
            "deferred": list(self.deferred),
        }


@dataclass(frozen=True, slots=True)
class GrantStatus:
    """Live, read-only projection of the authoritative Grant.

    ``subject_id`` is the server-resolved ``prin_``/``svc_``/``rt_`` subject used
    in the credential.  It is never accepted from task input.
    """

    grant_id: str
    task_id: str
    subject_id: str
    execution_mode: str
    status: str
    usable: bool
    expires_at: datetime
    revocation_reason: str | None
    capabilities: tuple[str, ...]
    resource_scope: ResourceScope

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subject_id, str)
            or _SUBJECT_PATTERN.fullmatch(self.subject_id) is None
        ):
            raise SecurityError("invalid_spec", "subject_id_invalid")
        if self.status not in _GRANT_STATUSES:
            raise SecurityError("invalid_spec", "grant_status_invalid")
        if not isinstance(self.usable, bool):
            raise SecurityError("invalid_spec", "grant_usable_invalid")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.utcoffset() is None
        ):
            raise SecurityError("invalid_spec", "grant_expiry_invalid")
        if not isinstance(self.resource_scope, ResourceScope):
            raise SecurityError("invalid_spec", "resource_scope_invalid")


class GrantStatusProvider(Protocol):
    """Live, read-only Grant adapter used by the gateway and supervisor.

    Implementations must read the authoritative Grant on every call: no cache,
    no reuse of a previous answer, and no acceptance of a spec-supplied expiry.
    """

    def status(self, grant_id: str, *, task_id: str) -> GrantStatus | None:
        """Return the current Grant projection, or ``None`` when missing."""
        ...


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """One factual observation of a unit; it does not write task state.

    ``process_gone``, ``stop_confirmed`` and ``never_started`` are distinct
    facts and must not be substituted for one another.
    """

    state: str
    process_gone: bool
    stop_confirmed: bool
    never_started: bool
    main_pid: int
    result: str
    exec_main_status: int | None


@dataclass(frozen=True, slots=True)
class LaunchHandle:
    """Trusted handle returned by a successful launch."""

    unit: str
    task_id: str
    grant_id: str
    jti: str
    expires_at: datetime
    network_profile: str
    boundary_claim: BoundaryClaim
    launched_at: datetime


@dataclass(frozen=True, slots=True)
class StopResult:
    """Outcome of a requested stop, with evidence kept separate from outcome."""

    stop_confirmed: bool
    process_gone: bool
    result: str
    exec_main_status: int | None


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Evidence that cleanup removed the unit's residue."""

    units_remaining: int
    state_dir_removed: bool
    credential_removed: bool


def unit_name_for_task(task_id: str) -> str:
    """Derive the fixed, non-user-controlled unit name for ``task_id``.

    ``agentmgr-task-`` plus the first 12 lowercase hex characters of
    ``sha256("agentmgr-task:v1:" + task_id)``.  Invalid task IDs fail closed.
    """

    if (
        not isinstance(task_id, str)
        or _TASK_ID_PATTERN.fullmatch(task_id) is None
    ):
        raise SecurityError("invalid_spec", "task_id_invalid")

    digest = hashlib.sha256((_UNIT_DOMAIN + task_id).encode("utf-8")).hexdigest()
    unit = _UNIT_PREFIX + digest[:12]
    if _UNIT_PATTERN.fullmatch(unit) is None:
        # Defence in depth: the prefix/hex construction makes this unreachable.
        raise SecurityError("invalid_spec", "unit_name_invalid")
    return unit
