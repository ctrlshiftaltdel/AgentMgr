"""Runtime Adapter contract for the B3 execution boundary.

This module is the Python projection of the frozen, runtime-neutral contract in
``docs/contracts/runtime-adapter.md`` (version 1.0.0).  It deliberately contains
**no** Hermes (or any other vendor) object: core code depends only on the
dataclasses, enums and :class:`RuntimeAdapter` protocol defined here.

Two rules from the frozen contract shape the types:

* ``runtime_task_id`` is an opaque handle.  Core code must not parse it, derive
  state from its shape, or use it as a filesystem path.
* Capability status is one of ``verified`` / ``partially_verified`` /
  ``unverified``.  Unsupported features are reported as ``unverified`` with an
  explicit ``limitations`` entry; there is no fourth "supported" state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "BOUNDARY",
    "CancelOutcome",
    "CancelResult",
    "Capability",
    "CapabilityStatus",
    "ControlResult",
    "ErrorCode",
    "EventBatch",
    "HealthResult",
    "HealthStatus",
    "InputOutcome",
    "InputResult",
    "ResultOutcome",
    "RuntimeAdapter",
    "RuntimeCapabilities",
    "RuntimeCapability",
    "RuntimeErrorInfo",
    "RuntimeEvent",
    "RuntimeResult",
    "RuntimeState",
    "RuntimeType",
    "StatusResult",
    "SubmitOutcome",
    "SubmitResult",
    "VersionSource",
]

#: Version of the frozen adapter contract this module implements.
ADAPTER_CONTRACT_VERSION = "1.0.0"

#: Fixed trust-boundary label used on every capability snapshot and event.
BOUNDARY = "b3_runtime_adapter"


class CapabilityStatus(str, Enum):
    """Contract section 2.2 capability states."""

    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIED = "unverified"


class RuntimeType(str, Enum):
    """Runtime families named by the frozen contract."""

    HERMES = "hermes"
    RAKAZO = "rakazo"
    OTHER = "other"


class VersionSource(str, Enum):
    """Where ``RuntimeCapabilities.runtime_version`` came from."""

    RUNTIME_SELF_REPORTED = "runtime_self_reported"
    OPERATOR_VERIFIED = "operator_verified"
    UNKNOWN = "unknown"


class ErrorCode(str, Enum):
    """Stable error codes from contract section 3.10."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED = "unsupported"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class RuntimeCapability(str, Enum):
    """Capability names from contract section 2.3 (plus additive adapter names)."""

    TASK_SUBMIT = "task.submit"
    TASK_LOOKUP = "task.lookup"
    TASK_STATUS = "task.status"
    TASK_EVENTS_POLL = "task.events.poll"
    TASK_EVENTS_SUBSCRIBE = "task.events.subscribe"
    TASK_INPUT_PROVIDE = "task.input.provide"
    TASK_CANCEL = "task.cancel"
    TASK_RESULT = "task.result"
    TASK_APPROVAL_RESOLVE = "task.approval.resolve"
    TASK_UNKNOWN_RESOLVE = "task.unknown.resolve"
    RUNTIME_HEALTH = "runtime.health"
    RUNTIME_ISOLATION = "runtime.isolation"
    RUNTIME_TASK_SCOPED_CREDENTIALS = "runtime.task_scoped_credentials"
    RUNTIME_TOOL_PERMISSIONS = "runtime.tool_permissions"
    RUNTIME_MANAGED_EXECUTION = "runtime.managed_execution"
    RUNTIME_STRUCTURED_EVENTS = "runtime.structured_events"


class SubmitOutcome(str, Enum):
    """Contract section 3.1 submit outcomes."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
    #: Same idempotency key with a different payload (contract section 3.1).
    CONFLICT = "conflict"


class RuntimeState(str, Enum):
    """Stable states from contract section 3.3.

    Vendor status strings never cross this boundary; they stay in
    :attr:`StatusResult.external_state` as evidence.
    """

    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


class CancelOutcome(str, Enum):
    """Contract section 3.7 cancel outcomes."""

    REQUESTED = "requested"
    ALREADY_TERMINAL = "already_terminal"
    STOPPED = "stopped"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ResultOutcome(str, Enum):
    """Contract section 3.8 result outcomes."""

    SUCCEEDED = "succeeded"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    NEEDS_HUMAN = "needs_human"
    UNKNOWN = "unknown"


class InputOutcome(str, Enum):
    """Contract section 3.6 provide-input outcomes."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class HealthStatus(str, Enum):
    """Contract section 3.9 health states."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RuntimeErrorInfo:
    """A safe, structured error attached to a normal result object.

    Definite protocol/auth/transport failures are raised as exceptions from
    :mod:`app.runtime.errors`.  Ambiguous outcomes (submit timeout, unknown
    execution, result not ready) are returned on the dataclass so the core can
    follow the ``unknown`` / reconciliation path without guessing.
    """

    code: ErrorCode
    message: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class Capability:
    """One capability label with its evidence and explicit limitations."""

    name: str
    version: str
    status: CapabilityStatus
    evidence_ref: str | None
    limitations: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Contract section 2.1 capability snapshot."""

    adapter_contract_version: str
    boundary: str
    runtime_id: str
    runtime_type: RuntimeType
    runtime_version: str | None
    version_source: VersionSource
    capabilities: tuple[Capability, ...]
    observed_at: datetime

    def capability(self, name: RuntimeCapability | str) -> Capability | None:
        wanted = name.value if isinstance(name, RuntimeCapability) else name
        for item in self.capabilities:
            if item.name == wanted:
                return item
        return None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Contract section 3.1 submit response.

    ``runtime_task_id`` is an opaque handle.  ``replayed`` is adapter evidence
    that the runtime recognised an idempotent replay; it is not a core state.
    """

    outcome: SubmitOutcome
    runtime_task_id: str | None = None
    accepted_at: datetime | None = None
    reported_state: str | None = None
    replayed: bool = False
    error: RuntimeErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Contract section 3.3 status response with safe pass-through fields."""

    runtime_task_id: str
    state: RuntimeState
    external_state: str | None
    observed_at: datetime
    heartbeat_at: datetime | None = None
    progress: Mapping[str, Any] | None = None
    stop_confirmed: bool = False
    input_required: bool = False
    output: str | None = None
    usage: Mapping[str, Any] | None = None
    runtime: str | None = None
    session_id: str | None = None
    error: RuntimeErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One typed event from contract section 3.4."""

    event_id: str | None
    occurred_at: datetime
    type: str
    payload: Mapping[str, Any]
    redacted: bool = True


@dataclass(frozen=True, slots=True)
class EventBatch:
    """Contract section 3.4 event batch with a client-side cursor."""

    events: tuple[RuntimeEvent, ...]
    next_cursor: str | None
    has_more: bool = False
    error: RuntimeErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class CancelResult:
    """Contract section 3.7 cancel response.

    ``stop_confirmed`` is only true after a terminal runtime status (normally
    ``cancelled``) proves execution has ended.  A ``requested`` outcome is never
    a stop confirmation.
    """

    outcome: CancelOutcome
    stop_confirmed: bool
    observed_at: datetime
    error: RuntimeErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Contract section 3.8 result response.

    ``structured_result`` is false for runtimes that only return free text.
    Such a result may be reported to the core, but the core must not treat it as
    a verified business result without independent completion evidence.
    """

    outcome: ResultOutcome
    result_ref: str | None
    summary: str | None
    structured_payload: Mapping[str, Any] | None
    artifacts: tuple[Mapping[str, Any], ...]
    evidence_refs: tuple[str, ...]
    verification_hints: tuple[str, ...]
    stop_confirmed: bool
    completed_at: datetime | None
    structured_result: bool = False
    output: str | None = None
    usage: Mapping[str, Any] | None = None
    runtime: str | None = None
    error: RuntimeErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class InputResult:
    """Contract section 3.6 provide-input response."""

    outcome: InputOutcome
    observed_at: datetime
    error: RuntimeErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class HealthResult:
    """Contract section 3.9 health response."""

    status: HealthStatus
    checked_at: datetime
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Optional approval / unknown-resolution control operation result.

    These operations are additive and only partially verified for Hermes; the
    core must not rely on them for a compatibility claim.
    """

    accepted: bool
    status: str | None
    observed_at: datetime
    error: RuntimeErrorInfo | None = None


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Internal B3 adapter protocol.

    Implementations report facts only.  They never write task state, never
    accept a client-supplied Grant, and never expose vendor paths, database
    objects or credentials to the core.
    """

    def capabilities(self, *, probe: bool = False) -> RuntimeCapabilities:
        """Return the capability snapshot; ``probe`` may read the live runtime."""
        ...

    def submit_task(
        self,
        task_id: str,
        prompt: str,
        idempotency_key: str,
        instructions: str | None = None,
    ) -> SubmitResult:
        """Submit one task and return an opaque runtime handle or ambiguity."""
        ...

    def get_status(self, runtime_task_id: str) -> StatusResult:
        """Return the mapped status and safe runtime facts."""
        ...

    def poll_events(
        self,
        runtime_task_id: str,
        cursor: str | None = None,
        max_events: int = 100,
        timeout: float = 30.0,
    ) -> EventBatch:
        """Poll the runtime event stream from a client-side cursor."""
        ...

    def cancel_task(
        self,
        runtime_task_id: str,
        *,
        reason: str | None = None,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> CancelResult:
        """Request cancellation and report whether a stop is confirmed."""
        ...

    def get_result(self, runtime_task_id: str) -> RuntimeResult:
        """Return the runtime result without claiming business verification."""
        ...

    def provide_input(
        self,
        runtime_task_id: str,
        input_request_id: str,
        payload_hash: str,
        payload: Mapping[str, Any],
    ) -> InputResult:
        """Answer a runtime input request, or report ``unsupported``."""
        ...

    def health(self) -> HealthResult:
        """Probe liveness and authenticated API access."""
        ...
