"""Hermes Runtime Adapter (B3) over the verified ``/v1/runs`` API.

Design basis
============
* ``docs/contracts/runtime-adapter.md`` -- frozen, runtime-neutral contract.
* ``docs/research/runtime-compatibility/hermes-0.22.1.md`` -- spike evidence
  for the endpoints, statuses, idempotency and SSE framing used below.

The adapter talks to the Hermes API server with the standard library only.  It
does not import Hermes modules, read its profile, database or session files, or
assume any host path.  ``runtime_task_id`` values are opaque; the only stable
AgentMgr-side association is the ``session_id`` sent at submission time (the
AgentMgr task id).

Ambiguity policy
================
* A submit timeout or connection drop may mean "accepted but response lost".
  The adapter retries the *same* POST with the *same* ``Idempotency-Key`` and
  payload (Hermes replays the original ``run_id``), and after the bounded retry
  budget returns ``SubmitResult.outcome=timeout|unknown``.  It never generates
  a new key and never claims rejection.
* ``POST /stop`` returning ``stopping`` is **not** a stop.  ``cancel_task``
  polls until the runtime reports ``cancelled`` (or another terminal status)
  before setting ``stop_confirmed=true``; otherwise it returns ``requested``.
* The runtime's "completed" is reported as ``succeeded`` at the adapter layer
  only in the sense of "execution finished"; ``structured_result=false`` tells
  the core that no business result schema or independent evidence exists.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
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
    ResultOutcome,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeErrorInfo,
    RuntimeEvent,
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
    "HERMES_API_KEY_ENV",
    "HERMES_API_URL_ENV",
]

#: Environment variables read by :meth:`HermesConfig.from_env`.
HERMES_API_URL_ENV = "HERMES_API_URL"
HERMES_API_KEY_ENV = "HERMES_API_KEY"

#: Evidence document for the real-runtime labels below.
_SPIKE_EVIDENCE = "docs/research/runtime-compatibility/hermes-0.22.1.md"

#: Default opaque AgentMgr-side runtime identifier (never a hostname or path).
_DEFAULT_RUNTIME_ID = "rt_hermes_candidate_0001"

#: Bodies/strings above this size are truncated when they leave the adapter.
_MAX_TEXT = 100_000
_MAX_EXTERNAL_STATE = 64

#: Keys whose values are removed from event payloads before the core sees them.
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
)


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


def _redact(value: object, *, depth: int = 0) -> object:
    """Recursively remove obvious credential keys from an event payload."""

    if depth > 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:200]:
            key = str(raw_key)
            lowered = key.lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                result[key] = "[redacted]"
            else:
                result[key] = _redact(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth=depth + 1) for item in list(value)[:200]]
    if isinstance(value, str):
        if value.lower().startswith(("bearer ", "basic ")):
            return "[redacted]"
        if len(value) > _MAX_TEXT:
            return value[:_MAX_TEXT] + "...[truncated]"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_TEXT]


def _error_info(exc: RuntimeAdapterError) -> RuntimeErrorInfo:
    return RuntimeErrorInfo(
        code=exc.code,
        message=str(exc),
        context=dict(exc.context),
    )


def _static_capabilities() -> tuple[Capability, ...]:
    """Static labels from the Hermes 0.22.1 spike plus mock contract tests.

    ``verified`` means the success and negative paths are covered by the
    committed tests and the spike evidence; ``partially_verified`` means a real
    path exists but a documented branch/limitation remains; ``unverified`` means
    the runtime does not provide the capability (or no real evidence exists).
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
        cap(RuntimeCapability.TASK_SUBMIT, CapabilityStatus.VERIFIED),
        cap(
            RuntimeCapability.TASK_LOOKUP,
            CapabilityStatus.UNVERIFIED,
            "Hermes has no lookup-by-idempotency-key endpoint. Retry the same "
            "POST /v1/runs with the same Idempotency-Key and payload; replay "
            "returns the original run_id.",
            evidence=None,
        ),
        cap(RuntimeCapability.TASK_STATUS, CapabilityStatus.VERIFIED),
        cap(
            RuntimeCapability.TASK_EVENTS_POLL,
            CapabilityStatus.VERIFIED,
            "SSE event buffers expire after 5 minutes; reconnect from the last "
            "sequence id before expiry.",
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
            CapabilityStatus.VERIFIED,
            "POST /stop only returns stopping; stop_confirmed is set only after "
            "a later cancelled status.",
        ),
        cap(
            RuntimeCapability.TASK_RESULT,
            CapabilityStatus.VERIFIED,
            "Text output, usage and runtime labels only; structured_result is "
            "false and there are no independent evidence artifacts.",
        ),
        cap(
            RuntimeCapability.TASK_APPROVAL_RESOLVE,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "The endpoint exists, but the request/response contract was not "
            "triggered end-to-end in the 0.22.1 spike.",
        ),
        cap(
            RuntimeCapability.TASK_UNKNOWN_RESOLVE,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "The endpoint exists, but the request/response contract was not "
            "triggered end-to-end in the 0.22.1 spike.",
        ),
        cap(RuntimeCapability.RUNTIME_HEALTH, CapabilityStatus.VERIFIED),
        cap(
            RuntimeCapability.RUNTIME_ISOLATION,
            CapabilityStatus.PARTIALLY_VERIFIED,
            "Logical session isolation only (session_id=AgentMgr task id); no "
            "OS-level UID, filesystem, network or process isolation.",
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
            "No per-run tool allowlist; the runtime exposes its full configured "
            "toolset, including terminal access.",
            evidence=None,
        ),
        cap(
            RuntimeCapability.RUNTIME_MANAGED_EXECUTION,
            CapabilityStatus.UNVERIFIED,
            "Not established: full host toolset, no task-scoped credentials and "
            "no OS isolation.",
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


@dataclass(frozen=True, slots=True)
class HermesConfig:
    """Connection settings for one Hermes API server.

    ``api_key`` is excluded from ``repr``; the adapter never logs headers or
    request bodies.  ``api_url`` must not embed credentials (userinfo).
    """

    api_url: str
    api_key: str = field(repr=False)
    runtime_id: str = _DEFAULT_RUNTIME_ID
    request_timeout: float = 30.0
    submit_attempts: int = 2
    cancel_poll_timeout: float = 10.0
    cancel_poll_interval: float = 0.5
    health_timeout: float = 5.0

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
        if len(self.api_key) < 16:
            raise RuntimeUnavailable(
                "HERMES_API_KEY must be at least 16 characters",
                context={"variable": HERMES_API_KEY_ENV, "reason": "too_short"},
            )
        if "\r" in self.api_key or "\n" in self.api_key:
            raise RuntimeUnavailable(
                "HERMES_API_KEY contains an invalid control character",
                context={"variable": HERMES_API_KEY_ENV, "reason": "invalid_character"},
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
        return cls(api_url=api_url, api_key=api_key)


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
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Accept": accept,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    @staticmethod
    def _run_path(runtime_task_id: str, suffix: str = "") -> str:
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
    ) -> dict[str, Any]:
        headers = self._headers(idempotency_key=idempotency_key, accept=accept)
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
            )
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
        ``probe=True`` a live probe supplies ``runtime_version`` and sets
        ``version_source=runtime_self_reported``; probe failures raise a stable
        adapter error so the caller can fail closed.
        """

        runtime_version: str | None = None
        version_source = VersionSource.UNKNOWN
        if probe:
            data = self._request_json(
                "GET",
                "/v1/capabilities",
                operation="capabilities",
                timeout=self._config.health_timeout,
            )
            runtime_version = _extract_runtime_version(data)
            if runtime_version is not None:
                version_source = VersionSource.RUNTIME_SELF_REPORTED
        return RuntimeCapabilities(
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            boundary=BOUNDARY,
            runtime_id=self._config.runtime_id,
            runtime_type=RuntimeType.HERMES,
            runtime_version=runtime_version,
            version_source=version_source,
            capabilities=self._static_capabilities,
            observed_at=_utcnow(),
        )

    # -- submit ------------------------------------------------------------

    def submit_task(
        self,
        task_id: str,
        prompt: str,
        idempotency_key: str,
        instructions: str | None = None,
    ) -> SubmitResult:
        """Submit one task with a stable idempotency key.

        On timeout or a pre-response connection failure the exact same POST is
        retried (same key, same body).  After the bounded attempts the result is
        ``timeout``/``unknown`` -- never ``accepted`` without a ``run_id`` and
        never a new idempotency key.
        """

        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        if "\r" in idempotency_key or "\n" in idempotency_key:
            raise ValueError("idempotency_key must not contain control characters")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("instructions must be a string or None")

        body: dict[str, Any] = {"input": prompt, "session_id": task_id}
        if instructions is not None:
            body["instructions"] = instructions

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
            except RuntimeTimeout as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                return SubmitResult(
                    outcome=SubmitOutcome.TIMEOUT,
                    error=_error_info(exc),
                )
            except RuntimeUnknown as exc:
                # A definite HTTP response that maps to "unknown" is not an
                # ambiguous submission; do not turn it into an accepted retry.
                if exc.http_status is not None:
                    raise
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                return SubmitResult(
                    outcome=SubmitOutcome.UNKNOWN,
                    error=_error_info(exc),
                )
            except RuntimeUnavailable as exc:
                # A server-sent 503 is a definite answer; only a transport-level
                # failure (http_status is None) is safe to retry idempotently.
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
                raise RuntimeProtocolError(
                    "runtime submit response is missing run_id",
                    http_status=202,
                    context={"operation": "submit"},
                )
            return SubmitResult(
                outcome=SubmitOutcome.ACCEPTED,
                runtime_task_id=run_id,
                accepted_at=_parse_datetime(data.get("accepted_at")) or _utcnow(),
                reported_state=_optional_str(data.get("status")),
                replayed=data.get("replayed") is True,
            )

        # Defensive: the loop always returns or raises.
        assert last_error is not None
        return SubmitResult(
            outcome=SubmitOutcome.UNKNOWN,
            error=_error_info(last_error),
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
            return RuntimeState.COMPLETED, True, False
        if raw == "failed":
            return RuntimeState.FAILED, True, False
        if raw == "cancelled":
            return RuntimeState.STOPPED, True, False
        if raw == "interrupted":
            # Terminal in Hermes but ambiguous for the neutral contract; fail
            # closed instead of claiming a stop or a failure.
            return RuntimeState.UNKNOWN, False, False
        return RuntimeState.UNKNOWN, False, False

    def get_status(self, runtime_task_id: str) -> StatusResult:
        """Return mapped status and safe runtime facts.

        A 404 is a definitive ``not_found`` fact and is returned as
        ``RuntimeState.NOT_FOUND``; the core treats it as ``unknown`` and starts
        reconciliation.  Other HTTP/transport failures raise.
        """

        if not isinstance(runtime_task_id, str) or not runtime_task_id:
            raise ValueError("runtime_task_id must be a non-empty string")
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

        raw = _optional_str(data.get("status"), limit=_MAX_EXTERNAL_STATE)
        state, stop_confirmed, input_required = self._map_state(raw)
        error: RuntimeErrorInfo | None = None
        if raw is None or state is RuntimeState.UNKNOWN:
            error = RuntimeErrorInfo(
                code=ErrorCode.UNKNOWN,
                message="runtime status is unknown or not mapped",
                context={"external_state": raw},
            )
        return StatusResult(
            runtime_task_id=runtime_task_id,
            state=state,
            external_state=raw,
            observed_at=observed_at,
            heartbeat_at=_parse_datetime(
                data.get("heartbeat_at") or data.get("updated_at")
            ),
            progress=_as_mapping(data.get("progress")),
            stop_confirmed=stop_confirmed,
            input_required=input_required,
            output=_as_text(data.get("output")),
            usage=_as_mapping(data.get("usage")),
            runtime=_runtime_label(data.get("runtime")),
            session_id=_optional_str(data.get("session_id")),
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
        occurred_at = (
            _parse_datetime(payload_obj.get("occurred_at"))
            or _parse_datetime(payload_obj.get("timestamp"))
            or _utcnow()
        )
        payload = _redact(payload_obj)
        if not isinstance(payload, Mapping):
            payload = {"data": payload}
        return RuntimeEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            type=event_type,
            payload=payload,
            redacted=True,
        )

    def poll_events(
        self,
        runtime_task_id: str,
        cursor: str | None = None,
        max_events: int = 100,
        timeout: float = 30.0,
    ) -> EventBatch:
        """Poll the SSE stream, filtering client-side by sequence id.

        Keepalive comments are ignored.  ``next_cursor`` is the highest sequence
        id observed (or the input cursor when nothing new arrived).  Event
        buffers expire after five minutes on Hermes; a reconnect must happen
        before then.  A read timeout with no events is reported as an empty
        batch with ``error.code=timeout`` rather than a fake success.
        """

        if not isinstance(runtime_task_id, str) or not runtime_task_id:
            raise ValueError("runtime_task_id must be a non-empty string")
        if not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        if timeout < 0:
            raise ValueError("timeout must not be negative")

        socket_timeout = max(timeout, 0.001)
        headers = self._headers(accept="text/event-stream")
        url = self._url(self._run_path(runtime_task_id, "/events"))
        deadline = time.monotonic() + timeout

        collected: list[RuntimeEvent] = []
        next_cursor = cursor
        has_more = False
        error: RuntimeErrorInfo | None = None
        iterator: Iterator[str] | None = None
        try:
            iterator = self._transport.stream(
                url,
                headers=headers,
                timeout=socket_timeout,
            )
            for sse in iter_sse(iterator):
                event = self._event_from_sse(sse, cursor=cursor)
                if event is None:
                    continue
                collected.append(event)
                if event.event_id is not None:
                    next_cursor = event.event_id
                if len(collected) >= max_events:
                    has_more = True
                    break
                if time.monotonic() >= deadline:
                    has_more = True
                    break
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

    def cancel_task(
        self,
        runtime_task_id: str,
        *,
        reason: str | None = None,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> CancelResult:
        """Request cancellation and only confirm a stop after a terminal status.

        ``POST /v1/runs/{id}/stop`` returns ``status=stopping``; that is not a
        stop.  The method polls ``GET /v1/runs/{id}`` until ``cancelled`` (or
        another terminal status) or the bounded poll deadline.  On deadline it
        returns ``requested`` with ``stop_confirmed=false``.
        """

        if not isinstance(runtime_task_id, str) or not runtime_task_id:
            raise ValueError("runtime_task_id must be a non-empty string")
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

        observed_at = _utcnow()
        body: dict[str, Any] = {} if reason is None else {"reason": reason}
        try:
            data = self._request_json(
                "POST",
                self._run_path(runtime_task_id, "/stop"),
                operation="cancel",
                body=body,
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

        raw = _optional_str(data.get("status"), limit=_MAX_EXTERNAL_STATE)
        if raw == "cancelled":
            return CancelResult(
                outcome=CancelOutcome.STOPPED,
                stop_confirmed=True,
                observed_at=_utcnow(),
            )
        if raw in {"completed", "failed"}:
            return CancelResult(
                outcome=CancelOutcome.ALREADY_TERMINAL,
                stop_confirmed=True,
                observed_at=_utcnow(),
            )

        deadline = time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            status = self.get_status(runtime_task_id)
            if status.state is RuntimeState.STOPPED:
                return CancelResult(
                    outcome=CancelOutcome.STOPPED,
                    stop_confirmed=True,
                    observed_at=_utcnow(),
                )
            if status.state in {RuntimeState.COMPLETED, RuntimeState.FAILED}:
                return CancelResult(
                    outcome=CancelOutcome.ALREADY_TERMINAL,
                    stop_confirmed=True,
                    observed_at=_utcnow(),
                )
            if status.state is RuntimeState.NOT_FOUND:
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

    def get_result(self, runtime_task_id: str) -> RuntimeResult:
        """Return the runtime result without claiming business verification.

        Hermes returns free text plus usage/runtime labels.  Therefore
        ``structured_result=false``, ``structured_payload`` is null, and no
        independent ``evidence_refs`` are fabricated.  ``result_ref`` is an
        opaque handle to the runtime run, not a verified artifact.
        """

        if not isinstance(runtime_task_id, str) or not runtime_task_id:
            raise ValueError("runtime_task_id must be a non-empty string")
        data = self._get_run_data(runtime_task_id)
        raw = _optional_str(data.get("status"), limit=_MAX_EXTERNAL_STATE)
        output = _as_text(data.get("output"))
        usage = _as_mapping(data.get("usage"))
        runtime = _runtime_label(data.get("runtime"))
        now = _utcnow()
        completed_at = _parse_datetime(data.get("completed_at"))

        if raw == "completed":
            return RuntimeResult(
                outcome=ResultOutcome.SUCCEEDED,
                result_ref=f"runtime-result:{runtime_task_id}",
                summary=output,
                structured_payload=None,
                artifacts=(),
                evidence_refs=(),
                verification_hints=(),
                stop_confirmed=True,
                completed_at=completed_at or now,
                structured_result=False,
                output=output,
                usage=usage,
                runtime=runtime,
            )
        if raw == "failed":
            return RuntimeResult(
                outcome=ResultOutcome.FAILED,
                result_ref=None,
                summary=None,
                structured_payload=None,
                artifacts=(),
                evidence_refs=(),
                verification_hints=(),
                stop_confirmed=True,
                completed_at=completed_at or now,
                structured_result=False,
                output=output,
                usage=usage,
                runtime=runtime,
            )
        if raw == "cancelled":
            return RuntimeResult(
                outcome=ResultOutcome.CANCELLED,
                result_ref=None,
                summary=None,
                structured_payload=None,
                artifacts=(),
                evidence_refs=(),
                verification_hints=(),
                stop_confirmed=True,
                completed_at=completed_at or now,
                structured_result=False,
                output=output,
                usage=usage,
                runtime=runtime,
            )

        reason = (
            "runtime reported interrupted execution"
            if raw == "interrupted"
            else "runtime result is not available for the current status"
        )
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
            output=output,
            usage=usage,
            runtime=runtime,
            error=RuntimeErrorInfo(
                code=ErrorCode.UNKNOWN,
                message=reason,
                context={"external_state": raw},
            ),
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

    def resolve_approval(
        self,
        runtime_task_id: str,
        payload: Mapping[str, Any],
    ) -> ControlResult:
        """Optional, partially verified approval control (no core claim).

        The endpoint exists in the spike but its request/response schema was not
        exercised end-to-end.  The caller must supply the runtime payload; the
        adapter passes it through without inventing a core authorization claim.
        """

        return self._control_post(runtime_task_id, "/approval", payload, "approval")

    def resolve_unknown(
        self,
        runtime_task_id: str,
        payload: Mapping[str, Any],
    ) -> ControlResult:
        """Optional, partially verified unknown-resolution control."""

        return self._control_post(
            runtime_task_id, "/resolve-unknown", payload, "resolve_unknown"
        )

    def _control_post(
        self,
        runtime_task_id: str,
        suffix: str,
        payload: Mapping[str, Any],
        operation: str,
    ) -> ControlResult:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        observed_at = _utcnow()
        try:
            data = self._request_json(
                "POST",
                self._run_path(runtime_task_id, suffix),
                operation=operation,
                body=dict(payload),
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
        failures are ``unavailable``.
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
