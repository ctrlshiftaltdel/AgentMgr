"""Minimal standard-library HTTP and SSE transport.

The adapter uses this module instead of a third-party client (no ``httpx`` or
``requests`` dependency).  It is intentionally small:

* :class:`HttpTransport` is the injectable seam used by tests;
* :class:`UrllibTransport` is the real stdlib implementation;
* :func:`iter_sse` parses the ``text/event-stream`` framing used by Hermes.

Security notes: requests are built with an explicit header mapping.  The
transport never logs or serialises headers, and errors carry only the HTTP
status -- never a body, URL or credential.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.runtime.errors import RuntimeTimeout, RuntimeUnavailable

__all__ = [
    "HttpResponse",
    "HttpStatusError",
    "HttpTransport",
    "SseEvent",
    "UrllibTransport",
    "iter_sse",
]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A fully read HTTP response, including non-2xx responses."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class HttpStatusError(Exception):
    """Raised by the streaming transport for a non-2xx HTTP status."""

    def __init__(
        self,
        status: int,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.body = body
        super().__init__(f"HTTP status {status}")

    def __repr__(self) -> str:
        return f"HttpStatusError(status={self.status!r})"


class HttpTransport(Protocol):
    """Injectable transport used by :class:`HermesRuntimeAdapter`."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        ...

    def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Iterator[str]:
        """Yield decoded lines from a streaming GET response."""
        ...


class UrllibTransport:
    """Standard-library transport with deterministic error translation."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            # HTTP errors are normal protocol signals; the adapter maps them.
            try:
                error_body = exc.read()
            except OSError:
                error_body = b""
            return HttpResponse(
                status=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=error_body,
            )
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise RuntimeTimeout(
                    "runtime request timed out",
                    context={"operation": method},
                ) from None
            raise RuntimeUnavailable(
                "runtime connection failed",
                context={"operation": method},
            ) from None
        except (TimeoutError, socket.timeout):
            raise RuntimeTimeout(
                "runtime request timed out",
                context={"operation": method},
            ) from None
        except OSError:
            raise RuntimeUnavailable(
                "runtime connection failed",
                context={"operation": method},
            ) from None

    def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Iterator[str]:
        request = urllib.request.Request(
            url,
            headers=dict(headers),
            method="GET",
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read(65536)
            except OSError:
                error_body = b""
            raise HttpStatusError(
                int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=error_body,
            ) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise RuntimeTimeout(
                    "runtime event stream timed out",
                    context={"operation": "GET"},
                ) from None
            raise RuntimeUnavailable(
                "runtime event stream connection failed",
                context={"operation": "GET"},
            ) from None
        except (TimeoutError, socket.timeout):
            raise RuntimeTimeout(
                "runtime event stream timed out",
                context={"operation": "GET"},
            ) from None

        try:
            for raw in response:
                yield raw.decode("utf-8", "replace").rstrip("\r\n")
        except (TimeoutError, socket.timeout):
            raise RuntimeTimeout(
                "runtime event stream timed out",
                context={"operation": "GET"},
            ) from None
        except OSError:
            raise RuntimeUnavailable(
                "runtime event stream connection failed",
                context={"operation": "GET"},
            ) from None
        finally:
            response.close()


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One ``text/event-stream`` event."""

    event_id: str | None
    event: str | None
    data: str
    retry: int | None = None


def iter_sse(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Parse SSE lines into events, ignoring keepalive comments.

    Supports multi-line ``data:`` fields, ``id:`` sequence numbers, named
    events and the optional ``retry:`` field.  A final event without a trailing
    blank line is flushed at end of stream.
    """

    event_id: str | None = None
    event_name: str | None = None
    data_lines: list[str] = []
    retry: int | None = None

    def _flush() -> SseEvent | None:
        nonlocal event_id, event_name, data_lines, retry
        if not data_lines:
            event_id = None
            event_name = None
            retry = None
            return None
        payload = "\n".join(data_lines)
        item = SseEvent(
            event_id=event_id or None,
            event=event_name or None,
            data=payload,
            retry=retry,
        )
        event_id = None
        event_name = None
        data_lines = []
        retry = None
        return item

    for line in lines:
        if line == "":
            item = _flush()
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            # Keepalive comment (for example ``: ping``).
            continue
        field_name, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field_name == "id":
            event_id = value.strip() or None
        elif field_name == "event":
            event_name = value.strip() or None
        elif field_name == "data":
            data_lines.append(value)
        elif field_name == "retry":
            try:
                retry = int(value.strip())
            except ValueError:
                retry = None
        # Unknown fields are ignored per the SSE specification.

    item = _flush()
    if item is not None:
        yield item
