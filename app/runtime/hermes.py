"""Hermes Runtime Adapter (B3) over the ``/v1/runs`` API.

Design basis
============
* ``docs/contracts/runtime-adapter.md`` -- frozen, runtime-neutral contract.
* ``docs/research/runtime-compatibility/hermes-0.22.1.md`` -- **partial** spike
  evidence for the endpoints, statuses, idempotency and SSE framing below.

The adapter talks to the Hermes API server with the standard library only.  It
does not import Hermes modules, read its profile, database or session files, or
assume any host path.  ``runtime_task_id`` values are opaque.  The AgentMgr task
id is sent as the Hermes ``session_id`` and is the only task binding Hermes can
provide; it is a logical association, not an OS identity.

Honesty rules
=============
* No capability is ``verified``: a committed contract test suite has not been
  run against a real runtime and recorded with command/exit code/version/SHA.
  The labels are ``partially_verified`` or ``unverified``.
* Hermes cannot enforce per-tool capability or resource scope, task-scoped
  credentials, or OS isolation.  Its internal tool calls do **not** pass through
  the AgentMgr PEP until Issue #82 provides a controlled execution environment.
* ``submit_task`` requires a server-resolved :class:`RuntimeGrant`.  It rejects
  expired/task-mismatched/inconsistent grants.  ``managed_execution`` is
  unverified and is therefore rejected by default unless an explicit,
  test-only compatibility opt-in is set.

Ambiguity policy
================
* A submit timeout or connection drop may mean "accepted but response lost".
  The adapter stores the original key/body, retries the *same* POST with the
  *same* ``Idempotency-Key`` and payload (Hermes replays the original
  ``run_id``), and after the bounded retry budget returns
  ``SubmitResult.outcome=timeout|unknown``.  It never generates a new key and
  never claims rejection.
* ``find_by_idempotency_key`` can only answer from the process-local submission
  map because Hermes has no lookup endpoint.  It replays the exact POST with the
  stored key.  The map is not durable across restarts; this limitation is why
  ``task.lookup`` stays ``unverified``.
* ``POST /stop`` returning ``stopping`` is **not** a stop.  ``cancel_task`` polls
  until the runtime reports ``cancelled``/``timed_out`` before setting
  ``stop_confirmed=true``; ``completed``/``failed`` are terminal but are not a
  confirmed *stop*.
* A malformed 2xx response is ambiguous, not a definite failure: submit maps to
  ``unknown``, status/result to ``unknown``, and cancel to ``unknown``.
* Redirects are refused by the transport so the bearer key never follows a
  ``Location`` header to another origin.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    CancelOutcome,
    CancelResult,
    Capability,
    CapabilityStatus,
    ControlResult,
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
    RuntimeEvent,
    RuntimeGrant,
    RuntimeResult,
    RuntimeState,
    RuntimeType,
    StatusResult,
    SubmitOutcome,
    SubmitResult,
    VersionSource,
)
from app.runtime.errors import (
    RuntimeAdapterError,
    RuntimeAuthError,
    RuntimeConflict,
    RuntimeNotFound,
    RuntimeProtocolError,
    RuntimeRateLimited,
    RuntimeTimeout,
    RuntimeUnavailable,
    RuntimeUnknown,
    RuntimeUnsupported,
    error_code_for_http_status,
)
from app.runtime.http import (
    HttpStatusError,
    HttpTransport,
    HttpResponse,
    UrllibTransport,
    iter_sse,
)

__all__ = [
    "HermesConfig",
    "HermesRuntimeAdapter",
    "HERMES_ALLOW_UNVERIFIED_EXECUTION_ENV",
    "HERMES_API_KEY_ENV",
    "HERMES_API_URL_ENV",
]

#: Environment variables read by :meth:`HermesConfig.from_env`.
HERMES_API_URL_ENV = "HERMES_API_URL"
HERMES_API_KEY_ENV = "HERMES_API_KEY"
HERMES_ALLOW_UNVERIFIED_EXECUTION_ENV = "HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION"

#: Evidence document for the real-runtime labels below (partial evidence only).
_SPIKE_EVIDENCE = "docs/research/runtime-compatibility/hermes-0.22.1.md"

#: Runtime identity/version covered by the manual spike.  A probe that reports a
#: different value must downgrade the static labels.
_SPIKE_RUNTIME_NAME = "hermes"
_SPIKE_RUNTIME_VERSION = "0.22.1"

#: Default opaque AgentMgr-side runtime identifier (never a hostname or path).
_DEFAULT_RUNTIME_ID = "rt_hermes_candidate_0001"

#: The only execution mode the B3 adapter can serve.
_MANAGED_EXECUTION = "managed_execution"
_KNOWN_EXECUTION_MODES = frozenset(
    {"advisory", "governed_resource_access", "managed_execution"}
)
_SCOPE_TYPES = frozenset(
    {
        "self",
        "department",
        "company",
        "approval_chain",
        "project",
        "record_set",
        "connector",
    }
)

#: Capabilities that describe adapter/runtime enforcement, not a business tool.
#: A Grant naming one of these asks Hermes to enforce something it cannot.
_UNENFORCEABLE_CAPABILITIES = frozenset(
    {
        "runtime.managed_execution",
        "runtime.tool_permissions",
        "runtime.isolation",
        "runtime.task_scoped_credentials",
    }
)

#: Bodies/strings above this size are truncated when they leave the adapter.
_MAX_TEXT = 100_000
_MAX_EXTERNAL_STATE = 64
_MAX_IDENTIFIER = 256

_CAPABILITY_RE = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*@[0-9]+\.[0-9]+\.[0-9]+$"
)

#: Exact (normalised) key names that always carry a credential or session value.
_SENSITIVE_KEY_EXACT = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "auth",
        "cookie",
        "set_cookie",
        "jwt",
        "session",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "private_key",
        "access_key",
        "credential",
        "credentials",
    }
)

#: Substrings that mark a key as sensitive.  ``session`` is deliberately not a
#: substring so that ``session_id`` survives in event payloads; a bare
#: ``session`` key is covered by the exact set above.
_SENSITIVE_KEY_PARTS = (
    "token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "access_key",
    "cookie",
    "jwt",
)

#: Usage counters contain "token" but are not credentials; keep them readable.
_NON_SECRET_TOKEN_KEYS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "token_count", "tokens"}
)

_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(authorization\s*:\s*)(?:bearer|basic)?\s*[^\s,;\"']+"
)
_TOKEN_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|token|api[_-]?key|apikey|"
    r"secret|password|jwt)\s*=\s*([^&\s;,\"']+)"
)


class _DeadlineReached(Exception):
    """Internal control-flow signal for the polling deadline."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_str(value: object, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str):
            return text[:_MAX_TEXT]
    return None


def _as_mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _runtime_label(value: object) -> str | None:
    if isinstance(value, str):
        return _optional_str(value, limit=200)
    if isinstance(value, Mapping):
        for key in ("name", "id", "runtime"):
            label = value.get(key)
            if isinstance(label, str) and label.strip():
                return label.strip()[:200]
    return None


def _runtime_name_from(data: Mapping[str, Any]) -> str | None:
    direct = _optional_str(data.get("runtime_name"), limit=200)
    if direct:
        return direct
    direct = _optional_str(data.get("name"), limit=200)
    if direct:
        return direct
    runtime = data.get("runtime")
    if isinstance(runtime, Mapping):
        return _optional_str(runtime.get("name"), limit=200)
    return None


def _extract_runtime_version(data: Mapping[str, Any]) -> str | None:
    direct = _optional_str(data.get("runtime_version"), limit=200)
    if direct:
        return direct
    direct = _optional_str(data.get("version"), limit=200)
    if direct:
        return direct
    runtime = data.get("runtime")
    if isinstance(runtime, Mapping):
        return _optional_str(runtime.get("version"), limit=200)
    return None


def _payload_hash(body: Mapping[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _validate_text(value: object, name: str, *, limit: int = _MAX_IDENTIFIER) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{name} is too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _validate_runtime_task_id(value: object) -> str:
    text = _validate_text(value, "runtime_task_id")
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError("runtime_task_id must be a single opaque path segment")
    return text


def _validate_control_ref(value: object, name: str) -> str:
    text = _validate_text(value, name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", text):
        raise ValueError(f"{name} must be an opaque reference")
    return text


def _is_ambiguous_error(exc: RuntimeAdapterError) -> bool:
    """True when a sent request may have succeeded without a usable response."""

    status = exc.http_status
    if status is None:
        return True
    if 200 <= status < 400:
        return True
    return status in (408, 504)


def _is_sensitive_key(key: str) -> bool:
    normalised = key.strip().lower().replace("-", "_")
    if normalised in _NON_SECRET_TOKEN_KEYS:
        return False
    if normalised in _SENSITIVE_KEY_EXACT:
        return True
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


def _redact_text(value: str) -> tuple[str, bool]:
    """Redact embedded credentials and truncate, reporting whether it changed."""

    changed = False
    text = _AUTHORIZATION_RE.sub(lambda match: f"{match.group(1)}[redacted]", value)
    changed = changed or text != value
    updated = _TOKEN_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    changed = changed or updated != text
    text = updated
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "...[truncated]"
        changed = True
    return text, changed


def _redact(value: object, *, depth: int = 0) -> tuple[object, bool]:
    """Recursively remove credential keys/strings; return ``(value, redacted)``."""

    if depth > 6:
        return "[truncated]", True
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        redacted = False
        for raw_key, raw_value in list(value.items())[:200]:
            key = str(raw_key)
            if _is_sensitive_key(key):
                result[key] = "[redacted]"
                redacted = True
                continue
            item, item_redacted = _redact(raw_value, depth=depth + 1)
            result[key] = item
            redacted = redacted or item_redacted
        return result, redacted
    if isinstance(value, (list, tuple)):
        items = []
        redacted = False
        for raw_item in list(value)[:200]:
            item, item_redacted = _redact(raw_item, depth=depth + 1)
            items.append(item)
            redacted = redacted or item_redacted
        return items, redacted
    if isinstance(value, str):
        if value.lower().startswith(("bearer ", "basic ")):
            return "[redacted]", True
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    text, changed = _redact_text(str(value))
    return text, changed or text != str(value)


def _error_info(exc: RuntimeAdapterError) -> RuntimeErrorInfo:
    return RuntimeErrorInfo(
        code=exc.code,
        message=str(exc),
        context=dict(exc.context),
    )


def _append_limitation(cap: Capability, extra: str) -> Capability:
    limitations = extra if not cap.limitations else f"{cap.limitations} {extra}"
    return replace(cap, limitations=limitations)


def _static_capabilities() -> tuple[Capability, ...]:
    """Labels from the partial Hermes 0.22.1 spike plus mock contract tests.

    ``verified`` is deliberately absent: the committed real-runtime contract
    suite has not been run and recorded (command, exit code, version, SHA), so
    every capability is ``partially_verified`` or ``unverified``.  Mock tests are
    adapter-mapping evidence only and can never upgrade a label.
    """

    def cap(
        name: RuntimeCapability,
        status: CapabilityStatus,
        limitations: str | None = None,
        evidence: str | None = _SPIKE_EVIDENCE,
    ) -> Capability:
        return Capability(
            name=name.value,
            version=ADAPTER_CONTRACT_VERSION,
            status=status,
            evidence_ref=evidence,
            limitations=limitations,
        )

    return (
        cap(
            RuntimeCapability.TASK_SUBMIT,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "Only the isolated manual spike and mock tests exist; the committed "
            "real contract suite has not been run. Submission requires a "
            "server-resolved Grant; managed_execution stays unverified.",
        ),
        cap(
            RuntimeCapability.TASK_LOOKUP,
            CapabilityStatus.UNVERIFIED,
            "Hermes has no lookup-by-idempotency-key endpoint. The adapter keeps "
            "a process-local submission map and replays the exact POST with the "
            "same Idempotency-Key; the map is not durable across restarts.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.TASK_STATUS,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "Only the isolated manual spike and mock tests exist; the committed "
            "real contract suite has not been run.",
        ),
        cap(
            RuntimeCapability.TASK_EVENTS_POLL,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "SSE buffers expire after 5 minutes; Hermes did not confirm "
            "Last-Event-ID support, so reconnect uses client-side filtering and "
            "can miss events. The committed real contract suite has not run.",
        ),
        cap(
            RuntimeCapability.TASK_EVENTS_SUBSCRIBE,
            CapabilityStatus.UNVERIFIED,
            "The adapter performs bounded SSE polling; no durable subscription "
            "or cross-restart resume is claimed.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.TASK_INPUT_PROVIDE,
            CapabilityStatus.UNVERIFIED,
            "The API server has approval/clarify endpoints but no generic "
            "provide_input contract; unsupported.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.TASK_CANCEL,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "POST /stop only returns stopping; stop_confirmed is set only after "
            "a later cancelled/timed_out status. The committed real contract "
            "suite has not been run.",
        ),
        cap(
            RuntimeCapability.TASK_RESULT,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "Text output, usage and runtime labels only; no independent evidence "
            "artifacts, so a completed run is reported as partial_success. The "
            "committed real contract suite has not been run.",
        ),
        cap(
            RuntimeCapability.TASK_APPROVAL_RESOLVE,
            CapabilityStatus.UNVERIFIED,
            "The endpoint exists, but the request/response contract was not "
            "triggered end-to-end in the 0.22.1 spike. Only core-resolved "
            "references are accepted.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.TASK_UNKNOWN_RESOLVE,
            CapabilityStatus.UNVERIFIED,
            "The endpoint exists, but the request/response contract was not "
            "triggered end-to-end in the 0.22.1 spike. Only core-resolved "
            "references are accepted.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.RUNTIME_HEALTH,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "Only the isolated manual spike and mock tests exist; the committed "
            "real contract suite has not been run.",
        ),
        cap(
            RuntimeCapability.RUNTIME_ISOLATION,
            CapabilityStatus.UNVERIFIED,
            "Logical session isolation only (session_id=AgentMgr task id); no "
            "OS-level UID, filesystem, network or process isolation evidence.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.RUNTIME_TASK_SCOPED_CREDENTIALS,
            CapabilityStatus.UNVERIFIED,
            "No per-run credential injection; the runtime uses its own "
            "configured credentials.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.RUNTIME_TOOL_PERMISSIONS,
            CapabilityStatus.UNVERIFIED,
            "No per-run tool allowlist; the runtime executes its full configured "
            "toolset. Hermes-internal tool calls do not pass through the "
            "AgentMgr PEP until Issue #82/controlled execution.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.RUNTIME_MANAGED_EXECUTION,
            CapabilityStatus.UNVERIFIED,
            "Not established: full host toolset, no task-scoped credentials and "
            "no OS isolation. Submissions using managed_execution are rejected "
            "unless an explicit test-only compatibility opt-in is set.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.RUNTIME_STRUCTURED_EVENTS,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "SSE events are transport-structured, but the payload is not a "
            "stable business event schema; payloads are redacted and passed "
            "through as opaque JSON.",
        ),
    )


def _bind_capabilities(
    capabilities: tuple[Capability, ...],
    runtime_name: str | None,
    runtime_version: str | None,
) -> tuple[Capability, ...]:
    """Downgrade labels when a live probe reports a different runtime."""

    if runtime_name is not None and runtime_name.lower() != _SPIKE_RUNTIME_NAME:
        return tuple(
            replace(
                cap,
                status=CapabilityStatus.UNVERIFIED,
                limitations=(
                    f"{cap.limitations} " if cap.limitations else ""
                )
                + f"Live probe reported runtime {runtime_name!r}; evidence only "
                f"covers {_SPIKE_RUNTIME_NAME!r}.",
            )
            for cap in capabilities
        )
    if runtime_version is not None and not runtime_version.startswith(
        _SPIKE_RUNTIME_VERSION
    ):
        return tuple(
            cap
            if cap.status is CapabilityStatus.UNVERIFIED
            else _append_limitation(
                cap,
                f"Live probe reported version {runtime_version!r}; evidence only "
                f"covers {_SPIKE_RUNTIME_VERSION}.x.",
            )
            for cap in capabilities
        )
    return capabilities


@dataclass(frozen=True, slots=True)
class HermesConfig:
    """Connection settings for one Hermes API server.

    ``api_key`` is excluded from ``repr``; the adapter never logs headers or
    request bodies.  ``api_url`` must not embed credentials (userinfo).

    ``allow_unverified_managed_execution`` is a **test-only compatibility
    opt-in**.  It must stay false in every real deployment until Issue #82
    provides controlled execution: Hermes cannot enforce tool scope, task-scoped
    credentials or OS isolation.
    """

    api_url: str
    api_key: str = field(repr=False)
    runtime_id: str = _DEFAULT_RUNTIME_ID
    request_timeout: float = 30.0
    submit_attempts: int = 2
    cancel_poll_timeout: float = 10.0
    cancel_poll_interval: float = 0.5
    health_timeout: float = 5.0
    allow_unverified_managed_execution: bool = False

    def __post_init__(self) -> None:
        url = self.api_url.strip().rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeUnavailable(
                "HERMES_API_URL must be an absolute http(s) URL without query/fragment",
                context={"variable": HERMES_API_URL_ENV, "reason": "invalid_url"},
            )
        if parsed.username or parsed.password:
            raise RuntimeUnavailable(
                "HERMES_API_URL must not embed credentials",
                context={"variable": HERMES_API_URL_ENV, "reason": "embedded_credentials"},
            )
        api_key = self.api_key.strip()
        if len(api_key) < 16:
            raise RuntimeUnavailable(
                "HERMES_API_KEY must be at least 16 characters",
                context={"variable": HERMES_API_KEY_ENV, "reason": "too_short"},
            )
        if any(char.isspace() for char in api_key):
            raise RuntimeUnavailable(
                "HERMES_API_KEY contains whitespace",
                context={"variable": HERMES_API_KEY_ENV, "reason": "whitespace"},
            )
        if not self.runtime_id.strip():
            raise RuntimeUnavailable(
                "runtime_id must not be blank",
                context={"reason": "invalid_runtime_id"},
            )
        if self.request_timeout <= 0 or self.health_timeout <= 0:
            raise ValueError("Hermes timeouts must be positive")
        if self.submit_attempts < 1:
            raise ValueError("submit_attempts must be at least 1")
        if self.cancel_poll_timeout < 0 or self.cancel_poll_interval < 0:
            raise ValueError("cancel poll values must not be negative")
        object.__setattr__(self, "api_url", url)
        object.__setattr__(self, "api_key", api_key)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HermesConfig":
        """Load and validate configuration; fail closed when either var is absent."""

        source = os.environ if environ is None else environ
        api_url = (source.get(HERMES_API_URL_ENV) or "").strip()
        api_key = (source.get(HERMES_API_KEY_ENV) or "").strip()
        if not api_url:
            raise RuntimeUnavailable(
                "HERMES_API_URL is not configured",
                context={"variable": HERMES_API_URL_ENV, "reason": "missing"},
            )
        if not api_key:
            raise RuntimeUnavailable(
                "HERMES_API_KEY is not configured",
                context={"variable": HERMES_API_KEY_ENV, "reason": "missing"},
            )
        opt_in = (source.get(HERMES_ALLOW_UNVERIFIED_EXECUTION_ENV) or "").strip()
        return cls(
            api_url=api_url,
            api_key=api_key,
            allow_unverified_managed_execution=opt_in == "1",
        )


@dataclass
class _SubmissionRecord:
    """Process-local record of one submit, used by the lookup fallback."""

    task_id: str
    grant_id: str
    idempotency_key: str
    payload_hash: str
    body: dict[str, Any]
    run_id: str | None = None
    reported_state: str | None = None
    accepted_at: datetime | None = None


class HermesRuntimeAdapter:
    """Runtime Adapter implementation for the Hermes API server.

    The constructor fails closed when ``HERMES_API_URL`` / ``HERMES_API_KEY`` are
    missing or invalid.  A transport may be injected in tests; the default is
    :class:`app.runtime.http.UrllibTransport`.
    """

    def __init__(
        self,
        config: HermesConfig | None = None,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        self._config = config if config is not None else HermesConfig.from_env()
        self._transport: HttpTransport = transport or UrllibTransport()
        self._static_capabilities = _static_capabilities()
        self._submissions: dict[tuple[str, str, str], _SubmissionRecord] = {}
        self._submissions_lock = threading.Lock()

    # -- identity / safety -------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"HermesRuntimeAdapter(api_url={self._config.api_url!r}, "
            f"runtime_id={self._config.runtime_id!r}, api_key='<redacted>')"
        )

    __str__ = __repr__

    @property
    def config(self) -> HermesConfig:
        """Return the effective config.  Its ``repr`` masks the API key."""

        return self._config

    # -- URL / headers -----------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._config.api_url}{path}"

    def _headers(
        self,
        *,
        idempotency_key: str | None = None,
        accept: str = "application/json",
        cursor: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Accept": accept,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if cursor is not None:
            # Standard SSE reconnect header.  Hermes did not confirm support in
            # the spike, so the adapter still filters client-side.
            headers["Last-Event-ID"] = cursor
        return headers

    @staticmethod
    def _run_path(runtime_task_id: str, suffix: str = "") -> str:
        runtime_task_id = _validate_runtime_task_id(runtime_task_id)
        quoted = urllib.parse.quote(runtime_task_id, safe="")
        return f"/v1/runs/{quoted}{suffix}"

    # -- HTTP helpers ------------------------------------------------------

    @staticmethod
    def _raise_for_status(status: int, *, operation: str) -> None:
        code = error_code_for_http_status(status)
        kwargs: dict[str, Any] = {
            "http_status": status,
            "context": {"operation": operation, "http_status": status},
        }
        if code is ErrorCode.UNAUTHORIZED:
            raise RuntimeAuthError(
                "runtime authentication failed",
                code=ErrorCode.UNAUTHORIZED,
                **kwargs,
            )
        if code is ErrorCode.FORBIDDEN:
            raise RuntimeAuthError(
                "runtime authorization failed",
                code=ErrorCode.FORBIDDEN,
                **kwargs,
            )
        if code is ErrorCode.NOT_FOUND:
            raise RuntimeNotFound("runtime task not found", **kwargs)
        if code is ErrorCode.CONFLICT:
            raise RuntimeConflict("runtime conflict", **kwargs)
        if code is ErrorCode.RATE_LIMITED:
            raise RuntimeRateLimited("runtime rate limit reached", **kwargs)
        if code is ErrorCode.UNSUPPORTED:
            raise RuntimeUnsupported("runtime operation is unsupported", **kwargs)
        if code is ErrorCode.INVALID_REQUEST:
            raise RuntimeProtocolError(
                "runtime rejected the request as invalid",
                code=ErrorCode.INVALID_REQUEST,
                **kwargs,
            )
        if code is ErrorCode.TIMEOUT:
            raise RuntimeTimeout("runtime request timed out", **kwargs)
        if code is ErrorCode.UNAVAILABLE:
            raise RuntimeUnavailable("runtime is unavailable", **kwargs)
        raise RuntimeUnknown("runtime outcome is unknown", **kwargs)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
        accept: str = "application/json",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        headers = self._headers(
            idempotency_key=idempotency_key,
            accept=accept,
            cursor=cursor,
        )
        raw_body: bytes | None = None
        if body is not None:
            raw_body = json.dumps(
                dict(body), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._transport.request(
            method,
            self._url(path),
            headers=headers,
            body=raw_body,
            timeout=self._config.request_timeout if timeout is None else timeout,
        )
        if not 200 <= response.status < 300:
            self._raise_for_status(response.status, operation=operation)
        if not response.body:
            return {}
        try:
            data = json.loads(response.text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeProtocolError(
                "runtime returned invalid JSON",
                http_status=response.status,
                context={"operation": operation},
            ) from None
        if not isinstance(data, dict):
            raise RuntimeProtocolError(
                "runtime returned a non-object JSON document",
                http_status=response.status,
                context={"operation": operation},
            ) from None
        return data

    def _get_run_data(self, runtime_task_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            self._run_path(runtime_task_id),
            operation="status",
        )

    # -- capabilities ------------------------------------------------------

    def capabilities(self, *, probe: bool = False) -> RuntimeCapabilities:
        """Return static labels, optionally enriched by ``GET /v1/capabilities``.

        ``probe=False`` is deterministic and never touches the network.  With
        ``probe=True`` a live probe supplies ``runtime_name``/``runtime_version``
        and sets ``version_source=runtime_self_reported``.  If the probe reports
        a different runtime name, every label is downgraded to ``unverified``; a
        different version adds a limitation.  Probe failures raise a stable
        adapter error so the caller can fail closed.
        """

        runtime_name: str | None = None
        runtime_version: str | None = None
        version_source = VersionSource.UNKNOWN
        capabilities = self._static_capabilities
        if probe:
            data = self._request_json(
                "GET",
                "/v1/capabilities",
                operation="capabilities",
                timeout=self._config.health_timeout,
            )
            runtime_name = _runtime_name_from(data)
            runtime_version = _extract_runtime_version(data)
            if runtime_version is not None:
                version_source = VersionSource.RUNTIME_SELF_REPORTED
            capabilities = _bind_capabilities(
                self._static_capabilities,
                runtime_name,
                runtime_version,
            )
        return RuntimeCapabilities(
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            boundary=BOUNDARY,
            runtime_id=self._config.runtime_id,
            runtime_type=RuntimeType.HERMES,
            runtime_version=runtime_version,
            version_source=version_source,
            capabilities=capabilities,
            observed_at=_utcnow(),
            runtime_name=runtime_name,
        )

    # -- Grant validation --------------------------------------------------

    def _check_grant(self, grant: RuntimeGrant, task_id: str) -> None:
        """Validate the server-resolved projection; never resolve arbitrary refs."""

        if not isinstance(grant, RuntimeGrant):
            raise ValueError("grant must be a RuntimeGrant projection")
        _validate_text(grant.grant_id, "grant_id")
        _validate_text(grant.task_id, "grant.task_id")
        if grant.task_id != task_id:
            raise RuntimeAuthError(
                "grant is bound to a different task",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "task_binding_mismatch"},
            )
        if not isinstance(grant.task_revision, int) or grant.task_revision < 1:
            raise RuntimeAuthError(
                "grant task_revision must be a positive integer",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "invalid_task_revision"},
            )
        if grant.execution_mode not in _KNOWN_EXECUTION_MODES:
            raise RuntimeAuthError(
                "grant execution_mode is not a known mode",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "unsupported_execution_mode"},
            )
        if grant.execution_mode != _MANAGED_EXECUTION:
            raise RuntimeAuthError(
                "the B3 runtime adapter only serves managed_execution",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "execution_mode_mismatch"},
            )
        if grant.expires_at is None:
            raise RuntimeAuthError(
                "grant has no expiry",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "grant_expired"},
            )
        if grant.is_expired():
            raise RuntimeAuthError(
                "grant has expired",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "grant_expired"},
            )
        if not isinstance(grant.capabilities, tuple) or not grant.capabilities:
            raise RuntimeAuthError(
                "grant must declare at least one capability",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "inconsistent_capabilities"},
            )
        seen: set[str] = set()
        for capability in grant.capabilities:
            if not isinstance(capability, str) or not _CAPABILITY_RE.fullmatch(
                capability
            ):
                raise RuntimeAuthError(
                    "grant capability is malformed",
                    code=ErrorCode.FORBIDDEN,
                    context={"reason": "inconsistent_capabilities"},
                )
            if capability in seen:
                raise RuntimeAuthError(
                    "grant capabilities contain duplicates",
                    code=ErrorCode.FORBIDDEN,
                    context={"reason": "inconsistent_capabilities"},
                )
            seen.add(capability)
            if capability.split("@", 1)[0] in _UNENFORCEABLE_CAPABILITIES:
                raise RuntimeUnsupported(
                    "Hermes cannot enforce runtime-level capability "
                    f"{capability.split('@', 1)[0]}",
                    context={"reason": "unenforceable_capability"},
                )
        if not isinstance(grant.resource_scope, Mapping):
            raise RuntimeAuthError(
                "grant resource_scope must be an object",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "inconsistent_scope"},
            )
        scope_type = grant.resource_scope.get("scope_type")
        if scope_type not in _SCOPE_TYPES:
            raise RuntimeAuthError(
                "grant resource_scope.scope_type is missing or not authorising",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "inconsistent_scope"},
            )
        classifications = grant.resource_scope.get("data_classifications")
        if not isinstance(classifications, (list, tuple)) or not classifications:
            raise RuntimeAuthError(
                "grant resource_scope must declare data_classifications",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "inconsistent_scope"},
            )
        resource_ids = grant.resource_scope.get("resource_ids", [])
        if not isinstance(resource_ids, (list, tuple)):
            raise RuntimeAuthError(
                "grant resource_scope.resource_ids must be an array",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "inconsistent_scope"},
            )
        if (
            grant.execution_mode == _MANAGED_EXECUTION
            and not self._config.allow_unverified_managed_execution
        ):
            raise RuntimeUnsupported(
                "managed_execution is unverified for Hermes: per-tool "
                "capability/scope, task-scoped credentials and OS isolation "
                "cannot be enforced before Issue #82",
                context={"reason": "managed_execution_unverified"},
            )

    # -- submit ------------------------------------------------------------

    def submit_task(
        self,
        task_id: str,
        prompt: str,
        idempotency_key: str,
        grant: RuntimeGrant,
        instructions: str | None = None,
    ) -> SubmitResult:
        """Submit one task under a server-resolved Grant.

        Definite ``rejected``/``unsupported``/``conflict`` answers are returned
        on :class:`SubmitResult`.  Auth/rate-limit/unavailable failures raise a
        stable adapter error.  A timeout, connection drop or malformed 2xx is
        ambiguous: the same POST/key/body is replayed up to ``submit_attempts``
        and then reported as ``timeout``/``unknown`` with the key preserved.
        """

        _validate_text(task_id, "task_id")
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        _validate_text(idempotency_key, "idempotency_key")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("instructions must be a string or None")

        try:
            self._check_grant(grant, task_id)
        except RuntimeUnsupported as exc:
            return SubmitResult(
                outcome=SubmitOutcome.UNSUPPORTED,
                error=_error_info(exc),
            )

        body: dict[str, Any] = {"input": prompt, "session_id": task_id}
        if instructions is not None:
            body["instructions"] = instructions
        payload_hash = _payload_hash(body)
        record_key = (task_id, grant.grant_id, idempotency_key)
        with self._submissions_lock:
            existing = self._submissions.get(record_key)
            if existing is not None and existing.payload_hash != payload_hash:
                return SubmitResult(
                    outcome=SubmitOutcome.CONFLICT,
                    runtime_task_id=existing.run_id,
                    error=RuntimeErrorInfo(
                        code=ErrorCode.CONFLICT,
                        message=(
                            "same idempotency key was used with a different payload"
                        ),
                    ),
                )
            if existing is None:
                existing = _SubmissionRecord(
                    task_id=task_id,
                    grant_id=grant.grant_id,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    body=dict(body),
                )
                self._submissions[record_key] = existing

        attempts = max(1, self._config.submit_attempts)
        last_error: RuntimeAdapterError | None = None
        for attempt in range(attempts):
            try:
                data = self._request_json(
                    "POST",
                    "/v1/runs",
                    operation="submit",
                    body=body,
                    idempotency_key=idempotency_key,
                )
            except RuntimeConflict as exc:
                return SubmitResult(
                    outcome=SubmitOutcome.CONFLICT,
                    error=_error_info(exc),
                )
            except RuntimeUnsupported as exc:
                return SubmitResult(
                    outcome=SubmitOutcome.UNSUPPORTED,
                    error=_error_info(exc),
                )
            except RuntimeProtocolError as exc:
                if exc.http_status in (400, 415, 422):
                    return SubmitResult(
                        outcome=SubmitOutcome.REJECTED,
                        error=_error_info(exc),
                    )
                if not _is_ambiguous_error(exc):
                    raise
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                return SubmitResult(
                    outcome=SubmitOutcome.UNKNOWN,
                    error=_error_info(exc),
                )
            except RuntimeTimeout as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                return SubmitResult(
                    outcome=SubmitOutcome.TIMEOUT,
                    error=_error_info(exc),
                )
            except RuntimeUnknown as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                return SubmitResult(
                    outcome=SubmitOutcome.UNKNOWN,
                    error=_error_info(exc),
                )
            except RuntimeUnavailable as exc:
                # A server-sent 503 is a definite answer; only a transport-level
                # failure is safe to retry idempotently.
                if exc.http_status is not None:
                    raise
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                return SubmitResult(
                    outcome=SubmitOutcome.UNKNOWN,
                    error=_error_info(exc),
                )

            run_id = data.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                last_error = RuntimeProtocolError(
                    "runtime submit response is missing run_id",
                    http_status=202,
                    context={"operation": "submit"},
                )
                if attempt + 1 < attempts:
                    continue
                return SubmitResult(
                    outcome=SubmitOutcome.UNKNOWN,
                    error=_error_info(last_error),
                )
            accepted_at = _parse_datetime(data.get("accepted_at"))
            reported_state = _optional_str(data.get("status"))
            with self._submissions_lock:
                existing.run_id = run_id
                existing.reported_state = reported_state
                existing.accepted_at = accepted_at
            return SubmitResult(
                outcome=SubmitOutcome.ACCEPTED,
                runtime_task_id=run_id,
                accepted_at=accepted_at,
                reported_state=reported_state,
                replayed=data.get("replayed") is True,
            )

        # Defensive: the loop always returns or raises.
        assert last_error is not None
        return SubmitResult(
            outcome=SubmitOutcome.UNKNOWN,
            error=_error_info(last_error),
        )

    # -- lookup ------------------------------------------------------------

    def find_by_idempotency_key(
        self,
        task_id: str,
        grant_id: str,
        idempotency_key: str,
        payload_hash: str | None = None,
    ) -> LookupResult:
        """Look up an earlier submission by replaying the exact stored POST.

        Hermes has no independent lookup endpoint, so this method can only use
        the **process-local** submission map.  It never sends a request for a key
        it has not seen (that would create a new run in Hermes).  Without a local
        record it returns ``unknown``, never ``not_found``: absence of a local
        record is not proof that no run exists.
        """

        _validate_text(task_id, "task_id")
        _validate_text(grant_id, "grant_id")
        _validate_text(idempotency_key, "idempotency_key")
        if payload_hash is not None:
            _validate_text(payload_hash, "payload_hash", limit=128)

        record_key = (task_id, grant_id, idempotency_key)
        with self._submissions_lock:
            record = self._submissions.get(record_key)
        observed_at = _utcnow()
        if record is None:
            return LookupResult(
                outcome=LookupOutcome.UNKNOWN,
                observed_at=observed_at,
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message=(
                        "no process-local submission record; Hermes has no "
                        "lookup-by-idempotency-key endpoint"
                    ),
                ),
            )
        if payload_hash is not None and payload_hash != record.payload_hash:
            return LookupResult(
                outcome=LookupOutcome.CONFLICT,
                runtime_task_id=record.run_id,
                payload_hash=record.payload_hash,
                accepted_at=record.accepted_at,
                observed_at=observed_at,
                error=RuntimeErrorInfo(
                    code=ErrorCode.CONFLICT,
                    message="idempotency key is bound to a different payload hash",
                ),
            )

        try:
            data = self._request_json(
                "POST",
                "/v1/runs",
                operation="lookup",
                body=record.body,
                idempotency_key=record.idempotency_key,
            )
        except RuntimeConflict as exc:
            return LookupResult(
                outcome=LookupOutcome.CONFLICT,
                runtime_task_id=record.run_id,
                payload_hash=record.payload_hash,
                accepted_at=record.accepted_at,
                observed_at=observed_at,
                error=_error_info(exc),
            )
        except RuntimeNotFound as exc:
            return LookupResult(
                outcome=LookupOutcome.NOT_FOUND,
                payload_hash=record.payload_hash,
                observed_at=observed_at,
                error=_error_info(exc),
            )
        except RuntimeProtocolError as exc:
            if not _is_ambiguous_error(exc):
                raise
            return LookupResult(
                outcome=LookupOutcome.UNKNOWN,
                payload_hash=record.payload_hash,
                observed_at=observed_at,
                error=_error_info(exc),
            )
        except RuntimeTimeout as exc:
            return LookupResult(
                outcome=LookupOutcome.UNKNOWN,
                payload_hash=record.payload_hash,
                observed_at=observed_at,
                error=_error_info(exc),
            )
        except RuntimeUnavailable as exc:
            if exc.http_status is not None:
                raise
            return LookupResult(
                outcome=LookupOutcome.UNKNOWN,
                payload_hash=record.payload_hash,
                observed_at=observed_at,
                error=_error_info(exc),
            )
        except RuntimeUnknown as exc:
            return LookupResult(
                outcome=LookupOutcome.UNKNOWN,
                payload_hash=record.payload_hash,
                observed_at=observed_at,
                error=_error_info(exc),
            )

        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            return LookupResult(
                outcome=LookupOutcome.UNKNOWN,
                payload_hash=record.payload_hash,
                observed_at=observed_at,
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message="lookup replay response is missing run_id",
                ),
            )
        with self._submissions_lock:
            if record.run_id is not None and record.run_id != run_id:
                return LookupResult(
                    outcome=LookupOutcome.CONFLICT,
                    runtime_task_id=record.run_id,
                    payload_hash=record.payload_hash,
                    observed_at=observed_at,
                    error=RuntimeErrorInfo(
                        code=ErrorCode.CONFLICT,
                        message="lookup replay returned a different run_id",
                    ),
                )
            record.run_id = run_id
            record.reported_state = _optional_str(data.get("status"))
        return LookupResult(
            outcome=LookupOutcome.FOUND,
            runtime_task_id=run_id,
            reported_state=record.reported_state,
            payload_hash=record.payload_hash,
            accepted_at=record.accepted_at,
            observed_at=observed_at,
        )

    # -- status ------------------------------------------------------------

    @staticmethod
    def _map_state(raw: str | None) -> tuple[RuntimeState, bool, bool]:
        if raw == "queued":
            return RuntimeState.ACCEPTED, False, False
        if raw == "running":
            return RuntimeState.RUNNING, False, False
        if raw == "waiting_for_approval":
            return RuntimeState.WAITING_INPUT, False, True
        if raw == "stopping":
            # The frozen state enum has no intermediate "stopping" value.
            # Report it as still running/not stopped; the core keeps polling and
            # must not treat this as a confirmed cancellation.
            return RuntimeState.RUNNING, False, False
        if raw == "completed":
            # Terminal, but not a confirmed *stop*.  ``stop_confirmed`` is only
            # for cancelled/timed_out with explicit runtime evidence.
            return RuntimeState.COMPLETED, False, False
        if raw == "failed":
            return RuntimeState.FAILED, False, False
        if raw == "cancelled":
            return RuntimeState.STOPPED, True, False
        if raw == "timed_out":
            return RuntimeState.TIMED_OUT, True, False
        if raw == "interrupted":
            # Terminal in Hermes but ambiguous for the neutral contract; fail
            # closed instead of claiming a stop or a failure.
            return RuntimeState.UNKNOWN, False, False
        return RuntimeState.UNKNOWN, False, False

    def _binding_session_id(
        self,
        data: Mapping[str, Any],
        *,
        runtime_task_id: str,
        task_id: str | None,
    ) -> str | None:
        """Return the verified session id, or reject a cross-task handle."""

        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id and run_id != runtime_task_id:
            raise RuntimeProtocolError(
                "runtime status identifies a different run",
                http_status=200,
                context={"reason": "run_id_mismatch"},
            )
        session_id = _optional_str(data.get("session_id"))
        if task_id is None:
            # Do not surface an unverified binding to the core.
            return None
        if session_id is None:
            raise RuntimeAuthError(
                "runtime handle has no task binding",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "task_binding_unverifiable"},
            )
        if session_id != task_id:
            raise RuntimeAuthError(
                "runtime handle is bound to a different task",
                code=ErrorCode.FORBIDDEN,
                context={"reason": "task_binding_mismatch"},
            )
        return session_id

    def get_status(
        self,
        runtime_task_id: str,
        *,
        task_id: str | None = None,
    ) -> StatusResult:
        """Return mapped status and safe runtime facts.

        A 404 is a definitive ``not_found`` fact.  A malformed/ambiguous 2xx is
        ``unknown``, not an exception.  When ``task_id`` is supplied, the run's
        ``session_id`` must match it; cross-task handles are rejected.  Without a
        binding, ``session_id`` is not returned.
        """

        runtime_task_id = _validate_runtime_task_id(runtime_task_id)
        observed_at = _utcnow()
        try:
            data = self._get_run_data(runtime_task_id)
        except RuntimeNotFound as exc:
            return StatusResult(
                runtime_task_id=runtime_task_id,
                state=RuntimeState.NOT_FOUND,
                external_state="not_found",
                observed_at=observed_at,
                error=_error_info(exc),
            )
        except (RuntimeProtocolError, RuntimeTimeout, RuntimeUnknown) as exc:
            if not _is_ambiguous_error(exc):
                raise
            return StatusResult(
                runtime_task_id=runtime_task_id,
                state=RuntimeState.UNKNOWN,
                external_state=None,
                observed_at=observed_at,
                error=_error_info(exc),
            )

        session_id = self._binding_session_id(
            data,
            runtime_task_id=runtime_task_id,
            task_id=task_id,
        )
        raw = _optional_str(data.get("status"), limit=_MAX_EXTERNAL_STATE)
        state, stop_confirmed, input_required = self._map_state(raw)
        error: RuntimeErrorInfo | None = None
        if raw is None or state is RuntimeState.UNKNOWN:
            error = RuntimeErrorInfo(
                code=ErrorCode.UNKNOWN,
                message="runtime status is unknown or not mapped",
                context={"external_state": raw},
            )
        output, _ = _redact(_as_text(data.get("output")))
        progress, _ = _redact(_as_mapping(data.get("progress")))
        usage, _ = _redact(_as_mapping(data.get("usage")))
        return StatusResult(
            runtime_task_id=runtime_task_id,
            state=state,
            external_state=raw,
            observed_at=observed_at,
            heartbeat_at=_parse_datetime(
                data.get("heartbeat_at") or data.get("updated_at")
            ),
            progress=progress if isinstance(progress, Mapping) else None,
            stop_confirmed=stop_confirmed,
            input_required=input_required,
            output=output if isinstance(output, str) else None,
            usage=usage if isinstance(usage, Mapping) else None,
            runtime=_runtime_label(data.get("runtime")),
            session_id=session_id,
            error=error,
        )

    # -- events ------------------------------------------------------------

    @staticmethod
    def _is_at_or_before(event_id: str | None, cursor: str | None) -> bool:
        if event_id is None or cursor is None:
            return False
        if event_id == cursor:
            return True
        try:
            return int(event_id) <= int(cursor)
        except (TypeError, ValueError):
            return False

    def _event_from_sse(
        self,
        sse: Any,
        *,
        cursor: str | None,
    ) -> RuntimeEvent | None:
        if not sse.data:
            return None
        try:
            decoded = json.loads(sse.data)
        except (json.JSONDecodeError, TypeError):
            decoded = None

        payload_obj: Mapping[str, Any]
        if isinstance(decoded, Mapping):
            payload_obj = decoded
        else:
            payload_obj = {"data": decoded if decoded is not None else sse.data}

        event_id = sse.event_id
        if event_id is None:
            for key in ("id", "seq"):
                candidate = payload_obj.get(key)
                if isinstance(candidate, (str, int)) and str(candidate):
                    event_id = str(candidate)
                    break
        if self._is_at_or_before(event_id, cursor):
            return None

        declared_type = payload_obj.get("type")
        event_type = (
            _optional_str(declared_type, limit=200)
            or _optional_str(sse.event, limit=200)
            or "message"
        )
        occurred_at = _parse_datetime(
            payload_obj.get("occurred_at")
        ) or _parse_datetime(payload_obj.get("timestamp"))
        payload, redacted = _redact(payload_obj)
        if not isinstance(payload, Mapping):
            payload = {"data": payload}
        return RuntimeEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            type=event_type,
            payload=payload,
            redacted=redacted,
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
        """Poll the SSE stream, filtering client-side by sequence id.

        ``Last-Event-ID`` is sent when a cursor is supplied, but Hermes support is
        unconfirmed, so client-side filtering remains authoritative.  Keepalive
        comments also advance the poll deadline: a stream that only sends
        keepalives cannot hang past ``timeout``.  ``has_more`` is true only when
        an additional event was actually observed beyond ``max_events``.
        """

        runtime_task_id = _validate_runtime_task_id(runtime_task_id)
        if not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        if task_id is not None:
            bound = self.get_status(runtime_task_id, task_id=task_id)
            if bound.state is RuntimeState.NOT_FOUND:
                raise RuntimeNotFound("runtime task not found")

        socket_timeout = max(timeout, 0.001)
        headers = self._headers(accept="text/event-stream", cursor=cursor)
        url = self._url(self._run_path(runtime_task_id, "/events"))
        deadline = time.monotonic() + timeout

        collected: list[RuntimeEvent] = []
        next_cursor = cursor
        has_more = False
        error: RuntimeErrorInfo | None = None
        iterator: Iterator[str] | None = None

        def _enforce_deadline() -> None:
            if time.monotonic() >= deadline:
                raise _DeadlineReached

        try:
            iterator = self._transport.stream(
                url,
                headers=headers,
                timeout=min(socket_timeout, max(deadline - time.monotonic(), 0.001)),
            )
            for sse in iter_sse(iterator, on_keepalive=_enforce_deadline):
                event = self._event_from_sse(sse, cursor=cursor)
                if event is None:
                    _enforce_deadline()
                    continue
                collected.append(event)
                if event.event_id is not None:
                    next_cursor = event.event_id
                if len(collected) > max_events:
                    # One extra event proves there is more; it is not returned
                    # and will be re-read on the next poll from the cursor.
                    collected = collected[:max_events]
                    next_cursor = collected[-1].event_id or next_cursor
                    has_more = True
                    break
                _enforce_deadline()
        except _DeadlineReached:
            pass
        except HttpStatusError as exc:
            self._raise_for_status(exc.status, operation="events")
        except RuntimeTimeout as exc:
            error = _error_info(exc)
        except RuntimeAdapterError:
            raise
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

        return EventBatch(
            events=tuple(collected),
            next_cursor=next_cursor,
            has_more=has_more,
            error=error,
        )

    # -- cancel ------------------------------------------------------------

    def _cancel_from_status(self, status: StatusResult) -> CancelResult:
        if status.state is RuntimeState.STOPPED:
            return CancelResult(
                outcome=CancelOutcome.STOPPED,
                stop_confirmed=True,
                observed_at=_utcnow(),
            )
        if status.state is RuntimeState.TIMED_OUT:
            return CancelResult(
                outcome=CancelOutcome.ALREADY_TERMINAL,
                stop_confirmed=True,
                observed_at=_utcnow(),
                error=status.error,
            )
        if status.state in {RuntimeState.COMPLETED, RuntimeState.FAILED}:
            # Terminal, but not a confirmed stop.
            return CancelResult(
                outcome=CancelOutcome.ALREADY_TERMINAL,
                stop_confirmed=False,
                observed_at=_utcnow(),
                error=status.error,
            )
        return CancelResult(
            outcome=CancelOutcome.UNKNOWN,
            stop_confirmed=False,
            observed_at=_utcnow(),
            error=status.error,
        )

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
        """Request cancellation and only confirm a stop after terminal evidence.

        ``POST /v1/runs/{id}/stop`` returns ``stopping``; that is not a stop.
        ``stop_confirmed`` requires a later ``cancelled``/``timed_out`` status.
        Once the stop request has been accepted, a status poll error never
        discards the request: the result is ``requested`` with the error attached.
        """

        runtime_task_id = _validate_runtime_task_id(runtime_task_id)
        if idempotency_key is not None:
            _validate_text(idempotency_key, "idempotency_key")
        poll_timeout = (
            self._config.cancel_poll_timeout if poll_timeout is None else poll_timeout
        )
        poll_interval = (
            self._config.cancel_poll_interval
            if poll_interval is None
            else poll_interval
        )
        if poll_timeout < 0 or poll_interval < 0:
            raise ValueError("poll_timeout and poll_interval must not be negative")
        if task_id is not None:
            bound = self.get_status(runtime_task_id, task_id=task_id)
            if bound.state is RuntimeState.NOT_FOUND:
                raise RuntimeNotFound("runtime task not found")

        observed_at = _utcnow()
        body: dict[str, Any] = {} if reason is None else {"reason": reason}
        try:
            data = self._request_json(
                "POST",
                self._run_path(runtime_task_id, "/stop"),
                operation="cancel",
                body=body,
                idempotency_key=idempotency_key,
            )
        except RuntimeUnsupported as exc:
            return CancelResult(
                outcome=CancelOutcome.UNSUPPORTED,
                stop_confirmed=False,
                observed_at=observed_at,
                error=_error_info(exc),
            )
        except RuntimeNotFound:
            raise
        except RuntimeConflict as exc:
            # A 409 usually means completion raced the stop.  Query the current
            # status instead of guessing that the stop failed.
            try:
                status = self.get_status(runtime_task_id, task_id=task_id)
            except RuntimeAdapterError:
                return CancelResult(
                    outcome=CancelOutcome.REQUESTED,
                    stop_confirmed=False,
                    observed_at=_utcnow(),
                    error=_error_info(exc),
                )
            if status.state in {
                RuntimeState.STOPPED,
                RuntimeState.TIMED_OUT,
                RuntimeState.COMPLETED,
                RuntimeState.FAILED,
            }:
                return self._cancel_from_status(status)
            return CancelResult(
                outcome=CancelOutcome.REQUESTED,
                stop_confirmed=False,
                observed_at=_utcnow(),
                error=_error_info(exc),
            )
        except RuntimeProtocolError as exc:
            if not _is_ambiguous_error(exc):
                raise
            return CancelResult(
                outcome=CancelOutcome.UNKNOWN,
                stop_confirmed=False,
                observed_at=_utcnow(),
                error=_error_info(exc),
            )
        except (RuntimeTimeout, RuntimeUnknown) as exc:
            return CancelResult(
                outcome=CancelOutcome.UNKNOWN,
                stop_confirmed=False,
                observed_at=_utcnow(),
                error=_error_info(exc),
            )
        except RuntimeUnavailable as exc:
            if exc.http_status is not None:
                raise
            # A transport-level failure means the stop may or may not have been
            # delivered; never claim it was requested.
            return CancelResult(
                outcome=CancelOutcome.UNKNOWN,
                stop_confirmed=False,
                observed_at=_utcnow(),
                error=_error_info(exc),
            )

        raw = _optional_str(data.get("status"), limit=_MAX_EXTERNAL_STATE)
        if raw == "cancelled":
            return CancelResult(
                outcome=CancelOutcome.STOPPED,
                stop_confirmed=True,
                observed_at=_utcnow(),
            )
        if raw == "timed_out":
            return CancelResult(
                outcome=CancelOutcome.ALREADY_TERMINAL,
                stop_confirmed=True,
                observed_at=_utcnow(),
            )
        if raw in {"completed", "failed"}:
            return CancelResult(
                outcome=CancelOutcome.ALREADY_TERMINAL,
                stop_confirmed=False,
                observed_at=_utcnow(),
            )

        deadline = time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            try:
                status = self.get_status(runtime_task_id, task_id=task_id)
            except RuntimeAdapterError as exc:
                # The stop request was accepted; do not lose that fact because
                # the confirmation poll failed.
                return CancelResult(
                    outcome=CancelOutcome.REQUESTED,
                    stop_confirmed=False,
                    observed_at=_utcnow(),
                    error=_error_info(exc),
                )
            if status.state in {
                RuntimeState.STOPPED,
                RuntimeState.TIMED_OUT,
                RuntimeState.COMPLETED,
                RuntimeState.FAILED,
            }:
                return self._cancel_from_status(status)
            if status.state in {RuntimeState.NOT_FOUND, RuntimeState.UNKNOWN}:
                return CancelResult(
                    outcome=CancelOutcome.UNKNOWN,
                    stop_confirmed=False,
                    observed_at=_utcnow(),
                    error=status.error,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if poll_interval > 0:
                time.sleep(min(poll_interval, remaining))

        timeout_error = RuntimeErrorInfo(
            code=ErrorCode.TIMEOUT,
            message="stop was requested but not confirmed before the poll deadline",
        )
        return CancelResult(
            outcome=CancelOutcome.REQUESTED,
            stop_confirmed=False,
            observed_at=_utcnow(),
            error=timeout_error,
        )

    # -- result ------------------------------------------------------------

    def get_result(
        self,
        runtime_task_id: str,
        *,
        task_id: str | None = None,
    ) -> RuntimeResult:
        """Return the runtime result without claiming business verification.

        Hermes returns free text plus usage/runtime labels.  Therefore
        ``structured_result=false``, ``structured_payload`` is null, and no
        independent ``evidence_refs`` are fabricated.  A completed run without
        evidence is reported as ``partial_success``, not ``succeeded``.
        ``completed_at`` is ``None`` when the runtime omitted it.
        """

        runtime_task_id = _validate_runtime_task_id(runtime_task_id)
        try:
            data = self._get_run_data(runtime_task_id)
        except RuntimeProtocolError as exc:
            if not _is_ambiguous_error(exc):
                raise
            return self._unknown_result(exc)
        except (RuntimeTimeout, RuntimeUnknown) as exc:
            return self._unknown_result(exc)

        self._binding_session_id(
            data,
            runtime_task_id=runtime_task_id,
            task_id=task_id,
        )
        raw = _optional_str(data.get("status"), limit=_MAX_EXTERNAL_STATE)
        raw_output = _as_text(data.get("output"))
        output, _ = _redact(raw_output) if raw_output is not None else (None, False)
        usage, _ = _redact(_as_mapping(data.get("usage")))
        runtime = _runtime_label(data.get("runtime"))
        completed_at = _parse_datetime(data.get("completed_at"))
        result_ref = f"runtime-result:{runtime_task_id}"

        def _result(
            outcome: ResultOutcome,
            *,
            stop_confirmed: bool,
            summary: str | None,
            error: RuntimeErrorInfo | None = None,
        ) -> RuntimeResult:
            keeps_reference = outcome in {
                ResultOutcome.PARTIAL_SUCCESS,
                ResultOutcome.CANCELLED,
                ResultOutcome.TIMED_OUT,
            }
            return RuntimeResult(
                outcome=outcome,
                result_ref=result_ref if keeps_reference else None,
                summary=summary,
                structured_payload=None,
                artifacts=(),
                evidence_refs=(),
                verification_hints=(),
                stop_confirmed=stop_confirmed,
                completed_at=completed_at,
                structured_result=False,
                output=output if isinstance(output, str) else None,
                usage=usage if isinstance(usage, Mapping) else None,
                runtime=runtime,
                error=error,
            )

        if raw == "completed":
            return _result(
                ResultOutcome.PARTIAL_SUCCESS,
                stop_confirmed=False,
                summary=output if isinstance(output, str) else None,
                error=RuntimeErrorInfo(
                    code=ErrorCode.UNKNOWN,
                    message=(
                        "runtime completed without independent evidence_refs; "
                        "the result is not verifiable at the adapter layer"
                    ),
                ),
            )
        if raw == "failed":
            return _result(
                ResultOutcome.FAILED,
                stop_confirmed=False,
                summary=None,
            )
        if raw == "cancelled":
            return _result(
                ResultOutcome.CANCELLED,
                stop_confirmed=True,
                summary=None,
            )
        if raw == "timed_out":
            return _result(
                ResultOutcome.TIMED_OUT,
                stop_confirmed=True,
                summary=None,
            )

        reason = (
            "runtime reported interrupted execution"
            if raw == "interrupted"
            else "runtime result is not available for the current status"
        )
        return _result(
            ResultOutcome.UNKNOWN,
            stop_confirmed=False,
            summary=None,
            error=RuntimeErrorInfo(
                code=ErrorCode.UNKNOWN,
                message=reason,
                context={"external_state": raw},
            ),
        )

    @staticmethod
    def _unknown_result(exc: RuntimeAdapterError) -> RuntimeResult:
        return RuntimeResult(
            outcome=ResultOutcome.UNKNOWN,
            result_ref=None,
            summary=None,
            structured_payload=None,
            artifacts=(),
            evidence_refs=(),
            verification_hints=(),
            stop_confirmed=False,
            completed_at=None,
            structured_result=False,
            error=_error_info(exc),
        )

    # -- optional input / control -----------------------------------------

    def provide_input(
        self,
        runtime_task_id: str,
        input_request_id: str,
        payload_hash: str,
        payload: Mapping[str, Any],
    ) -> InputResult:
        """Report Hermes 0.22.1's lack of a generic provide_input contract.

        The runtime has approval/clarify endpoints, but they are not a generic
        input contract with ``input_request_id`` + ``payload_hash`` idempotency.
        The adapter therefore reports ``unsupported`` and never guesses.
        """

        del runtime_task_id, input_request_id, payload_hash, payload
        return InputResult(
            outcome=InputOutcome.UNSUPPORTED,
            observed_at=_utcnow(),
            error=RuntimeErrorInfo(
                code=ErrorCode.UNSUPPORTED,
                message="Hermes has no generic provide_input endpoint",
            ),
        )

    def _assert_control_binding(self, runtime_task_id: str, task_id: str) -> None:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        status = self.get_status(runtime_task_id, task_id=task_id)
        if status.state is RuntimeState.NOT_FOUND:
            raise RuntimeNotFound("runtime task not found")

    def resolve_approval(
        self,
        runtime_task_id: str,
        approval_ref: str,
        *,
        task_id: str,
        decision: str = "approve",
    ) -> ControlResult:
        """Optional, unverified approval control using a core-resolved reference.

        The endpoint exists in the spike but its request/response schema was not
        exercised end-to-end.  Only an opaque reference and a decision are sent;
        the adapter does not forward an arbitrary caller payload.
        """

        runtime_task_id = _validate_runtime_task_id(runtime_task_id)
        approval_ref = _validate_control_ref(approval_ref, "approval_ref")
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be 'approve' or 'reject'")
        self._assert_control_binding(runtime_task_id, task_id)
        return self._control_post(
            runtime_task_id,
            "/approval",
            {"approval_id": approval_ref, "decision": decision},
            "approval",
        )

    def resolve_unknown(
        self,
        runtime_task_id: str,
        resolution_ref: str,
        *,
        task_id: str,
        reason: str | None = None,
    ) -> ControlResult:
        """Optional, unverified unknown-resolution control."""

        runtime_task_id = _validate_runtime_task_id(runtime_task_id)
        resolution_ref = _validate_control_ref(resolution_ref, "resolution_ref")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string or None")
        self._assert_control_binding(runtime_task_id, task_id)
        body: dict[str, Any] = {"resolution_id": resolution_ref}
        if reason is not None:
            body["reason"] = reason[:_MAX_TEXT]
        return self._control_post(
            runtime_task_id,
            "/resolve-unknown",
            body,
            "resolve_unknown",
        )

    def _control_post(
        self,
        runtime_task_id: str,
        suffix: str,
        body: dict[str, Any],
        operation: str,
    ) -> ControlResult:
        observed_at = _utcnow()
        try:
            data = self._request_json(
                "POST",
                self._run_path(runtime_task_id, suffix),
                operation=operation,
                body=body,
            )
        except RuntimeUnsupported as exc:
            return ControlResult(
                accepted=False,
                status=None,
                observed_at=observed_at,
                error=_error_info(exc),
            )
        return ControlResult(
            accepted=True,
            status=_optional_str(data.get("status"), limit=_MAX_EXTERNAL_STATE),
            observed_at=observed_at,
        )

    # -- health ------------------------------------------------------------

    def health(self) -> HealthResult:
        """Check ``GET /health`` and then authenticated ``GET /v1/capabilities``.

        Health never raises for runtime failures and never includes the API key,
        headers or response body in ``details``.  A 429 on capabilities is
        ``degraded`` (the process is alive but saturated); auth/5xx/transport
        failures are ``unavailable`` with a stable reason code.
        """

        checked_at = _utcnow()
        api_ok = False
        auth_ok = False
        http_status: int | None = None
        capabilities_status: int | None = None
        reason: str | None = None

        try:
            response: HttpResponse = self._transport.request(
                "GET",
                self._url("/health"),
                headers={"Accept": "application/json"},
                body=None,
                timeout=self._config.health_timeout,
            )
            http_status = response.status
            api_ok = 200 <= response.status < 300
            if not api_ok:
                reason = error_code_for_http_status(response.status).value
        except RuntimeAdapterError as exc:
            reason = exc.code.value
        except Exception:  # pragma: no cover - defensive, never expose details
            reason = ErrorCode.UNKNOWN.value

        if api_ok:
            try:
                response = self._transport.request(
                    "GET",
                    self._url("/v1/capabilities"),
                    headers=self._headers(),
                    body=None,
                    timeout=self._config.health_timeout,
                )
                capabilities_status = response.status
                auth_ok = 200 <= response.status < 300
                if not auth_ok:
                    reason = error_code_for_http_status(response.status).value
            except RuntimeAdapterError as exc:
                reason = exc.code.value
            except Exception:  # pragma: no cover - defensive
                reason = ErrorCode.UNKNOWN.value

        if api_ok and auth_ok:
            status = HealthStatus.HEALTHY
            reason = None
        elif api_ok and reason == ErrorCode.RATE_LIMITED.value:
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.UNAVAILABLE

        return HealthResult(
            status=status,
            checked_at=checked_at,
            details={
                "api_ok": api_ok,
                "auth_ok": auth_ok,
                "http_status": http_status,
                "capabilities_status": capabilities_status,
                "reason": reason,
            },
        )
