"""Stable Runtime Adapter errors.

The codes mirror ``docs/contracts/runtime-adapter.md`` section 3.10.  Error
messages are short and structural: they never interpolate the API key, an
Authorization header, a request body, a response body or a vendor filesystem
path.  Detailed, non-sensitive context (HTTP status, operation name) lives in
:attr:`RuntimeAdapterError.context`.

The adapter distinguishes three situations:

* definite failures (401/403/404/409/429/503, malformed protocol) raise one of
  the subclasses below;
* 408/504 (timeout) and 502 (gateway error) are ambiguous: the request may have
  been accepted, so they are reported as ``timeout``/``unknown`` rather than as
  a definite rejection or unavailability;
* ambiguous submission outcomes are returned as
  :class:`app.runtime.contract.SubmitResult` with ``timeout``/``unknown``;
* a health probe degrades to :class:`app.runtime.contract.HealthResult` instead
  of raising, because health is itself the availability signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.runtime.contract import ErrorCode

__all__ = [
    "RuntimeAdapterError",
    "RuntimeAuthError",
    "RuntimeConflict",
    "RuntimeNotFound",
    "RuntimeProtocolError",
    "RuntimeRateLimited",
    "RuntimeTimeout",
    "RuntimeUnavailable",
    "RuntimeUnknown",
    "RuntimeUnsupported",
    "error_code_for_http_status",
]


def error_code_for_http_status(status: int) -> ErrorCode:
    """Map an HTTP status to the stable contract error code."""

    if status in (401,):
        return ErrorCode.UNAUTHORIZED
    if status in (403,):
        return ErrorCode.FORBIDDEN
    if status == 404:
        return ErrorCode.NOT_FOUND
    if status == 408 or status == 504:
        # Request/gateway timeout: the request may have reached the runtime.
        # Callers must treat acceptance as ambiguous, never as a definite reject.
        return ErrorCode.TIMEOUT
    if status == 409:
        return ErrorCode.CONFLICT
    if status == 429:
        return ErrorCode.RATE_LIMITED
    if status in (400, 415, 422):
        return ErrorCode.INVALID_REQUEST
    if status in (405, 501):
        return ErrorCode.UNSUPPORTED
    if status == 502:
        # Bad gateway: the request may have reached the upstream runtime before
        # the proxy failed.  Treat it as an ambiguous/unknown outcome, not as a
        # definite unavailability answer.
        return ErrorCode.UNKNOWN
    if status == 503:
        return ErrorCode.UNAVAILABLE
    if 500 <= status < 600:
        return ErrorCode.UNAVAILABLE
    return ErrorCode.UNKNOWN


class RuntimeAdapterError(Exception):
    """Base class for definite adapter failures.

    ``code`` is always a stable :class:`ErrorCode`.  ``http_status`` is optional
    and only meaningful for transport-backed adapters.
    """

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        http_status: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        self.http_status = http_status
        self.context: dict[str, Any] = dict(context or {})
        if message is None:
            message = f"runtime adapter error: {self.code.value}"
        # Deliberately short and derived from fixed text; no secrets, bodies or
        # vendor paths are interpolated here.
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "http_status": self.http_status,
        }

    def __repr__(self) -> str:
        # Exclude message/context from repr so a caller cannot accidentally log
        # a response body that was attached to context.
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"http_status={self.http_status!r})"
        )


class RuntimeAuthError(RuntimeAdapterError):
    """Credentials missing, rejected or insufficient (401/403)."""

    def __init__(
        self,
        message: str = "runtime authentication failed",
        *,
        code: ErrorCode = ErrorCode.UNAUTHORIZED,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)


class RuntimeNotFound(RuntimeAdapterError):
    """The runtime explicitly reports that the run does not exist."""

    def __init__(
        self,
        message: str = "runtime task not found",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=ErrorCode.NOT_FOUND, **kwargs)


class RuntimeConflict(RuntimeAdapterError):
    """Idempotency or execution-state conflict (409)."""

    def __init__(
        self,
        message: str = "runtime conflict",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=ErrorCode.CONFLICT, **kwargs)


class RuntimeRateLimited(RuntimeAdapterError):
    """Concurrent-run or quota limit reached (429)."""

    def __init__(
        self,
        message: str = "runtime rate limit reached",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=ErrorCode.RATE_LIMITED, **kwargs)


class RuntimeUnavailable(RuntimeAdapterError):
    """Configuration, connection or drain/storage unavailability (503)."""

    def __init__(
        self,
        message: str = "runtime is unavailable",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=ErrorCode.UNAVAILABLE, **kwargs)


class RuntimeTimeout(RuntimeAdapterError):
    """The transport timed out; acceptance may be unknown."""

    def __init__(
        self,
        message: str = "runtime request timed out",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=ErrorCode.TIMEOUT, **kwargs)


class RuntimeProtocolError(RuntimeAdapterError):
    """A response was malformed or violated the expected stable shape."""

    def __init__(
        self,
        message: str = "runtime protocol error",
        *,
        code: ErrorCode = ErrorCode.INVALID_REQUEST,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)


class RuntimeUnknown(RuntimeAdapterError):
    """The adapter cannot determine what happened."""

    def __init__(
        self,
        message: str = "runtime outcome is unknown",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=ErrorCode.UNKNOWN, **kwargs)


class RuntimeUnsupported(RuntimeAdapterError):
    """The runtime or adapter does not support the requested operation."""

    def __init__(
        self,
        message: str = "runtime operation is unsupported",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=ErrorCode.UNSUPPORTED, **kwargs)
