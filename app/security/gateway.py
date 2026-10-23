"""Unix-socket policy enforcement point for managed execution (Issue #82).

The gateway is the only external entry a managed task may use.  It speaks
newline-delimited JSON over ``AF_UNIX``, serves exactly one request per
connection and answers with ``{"ok":true,"result":...}`` or
``{"ok":false,"reason":<stable code>}``.  It never listens on an IP port.

Validation order (design §10.4)
-------------------------------
Admission and rate limits run in the socket handler *before* a byte of the
request is read: per-peer-UID connections first, then the global ceiling, then
the same order for requests.  :meth:`GatewayServer.handle_request` then runs
the pure chain: line length and JSON structure, ``version``, token MAC/claims,
``nbf``/``exp``, the revocation store, the live Grant, task/grant/subject
consistency, capability, resource scope and finally the backend.

Every call re-reads the revocation store and the Grant provider: nothing about
Grant state, revocation or an earlier decision is cached.  A failing store or
provider denies, it never allows.

Audit and redaction (design §10.6)
----------------------------------
Only whitelisted fields (:data:`AUDIT_FIELDS`) are recorded.  The token, its
segments, the request payload, exception text, stack traces, paths and host
names never are.  If the audit write fails the call is denied: the gateway
never performs an unaudited execution.

Residual (documented, not fixed here)
-------------------------------------
``FileRevocationStore`` creates its root if it is missing, so constructing one
does not prove that the run directory is a pre-existing, readable mount.  The
server therefore performs a startup probe (one ``lookup`` that must answer
``None``) and fails closed if the store cannot be read *now*.  Production
wiring (#83) must additionally create and verify the mount before constructing
the store; the probe is a liveness check, not a mount guarantee.
"""

from __future__ import annotations

import json
import os
import re
import socket
import socketserver
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.security.boundary import GrantStatus, GrantStatusProvider, ResourceScope
from app.security.credentials import (
    CredentialClaims,
    CredentialValidator,
    RevocationStore,
)
from app.security.errors import SECURITY_ERROR_CODES, SecurityError

__all__ = [
    "AUDIT_EVENT_ALLOW",
    "AUDIT_EVENT_DENY",
    "AUDIT_FIELDS",
    "GATEWAY_PROTOCOL_VERSION",
    "LIMIT_GLOBAL_CONNECTIONS",
    "LIMIT_GLOBAL_RPS",
    "LIMIT_NAMES",
    "LIMIT_PER_TASK_CONNECTIONS",
    "LIMIT_PER_TASK_RPS",
    "MAX_RESOURCE_BYTES",
    "AuditSink",
    "FileResourceBackend",
    "GatewayConfig",
    "GatewayLimits",
    "GatewayResponse",
    "GatewayServer",
    "LimitDecision",
    "RateLimiter",
    "ResourceBackend",
]

#: Wire protocol version carried in every request (design §10.2).
GATEWAY_PROTOCOL_VERSION = "1.0.0"

#: One resource file may not exceed this size (design §10.5).
MAX_RESOURCE_BYTES = 1024 * 1024

AUDIT_EVENT_ALLOW = "security.exec.gateway.allow"
AUDIT_EVENT_DENY = "security.exec.gateway.deny"

#: The complete audit whitelist (design §10.6); nothing else may be recorded.
AUDIT_FIELDS: frozenset[str] = frozenset(
    {
        "ts",
        "event",
        "task_id",
        "grant_id",
        "credential_jti",
        "capability",
        "resource",
        "decision",
        "reason",
        "peer_uid",
        "limit",
    }
)

LIMIT_PER_TASK_CONNECTIONS = "per_task_connections"
LIMIT_GLOBAL_CONNECTIONS = "global_connections"
LIMIT_PER_TASK_RPS = "per_task_rps"
LIMIT_GLOBAL_RPS = "global_rps"
LIMIT_NAMES: frozenset[str] = frozenset(
    {
        LIMIT_PER_TASK_CONNECTIONS,
        LIMIT_GLOBAL_CONNECTIONS,
        LIMIT_PER_TASK_RPS,
        LIMIT_GLOBAL_RPS,
    }
)

_REQUEST_FIELDS = frozenset(
    {"version", "token", "capability", "resource", "payload"}
)

#: Token errors that may be reported to the caller verbatim.
_TOKEN_REASONS: frozenset[str] = frozenset(
    {"token_invalid", "credential_expired", "token_not_yet_valid"}
)

#: Grant/capability/scope errors that may be reported to the caller verbatim.
_CALL_REASONS: frozenset[str] = frozenset(
    {
        "grant_revoked",
        "grant_expired",
        "execution_mode_mismatch",
        "cross_task_denied",
        "capability_not_granted",
        "resource_scope_denied",
    }
)

#: Backend errors that map to their own reason code; all others are internal.
_BACKEND_REASONS: frozenset[str] = frozenset(
    {"resource_denied", "resource_scope_denied"}
)

_RESOURCE_ID_PATTERN = re.compile(r"^res_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_RESOURCE_SUFFIX = ".json"
_READ_CAPABILITY_NAME = "resource.read"

#: Idle per-task buckets are dropped after this many seconds without activity.
_IDLE_SECONDS = 300.0

#: Probe ``jti`` used for the fail-closed revocation-store liveness check.
_PROBE_JTI = "cred_" + "0" * 32


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError("non-finite JSON number")


def _load_json(raw: bytes) -> object:
    """Parse strict JSON: no duplicate keys, no NaN/Infinity."""

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_pairs_without_duplicates,
        parse_constant=_reject_json_constant,
    )


def _encode_response(response: GatewayResponse) -> bytes:
    return (
        json.dumps(
            response.as_dict(), separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        + b"\n"
    )


def _peer_uid(sock: socket.socket) -> int | None:
    """Return the peer UID from ``SO_PEERCRED`` (Linux) or ``None``.

    The UID is only ever used for admission counting and audit; it never
    participates in authorisation.
    """

    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is None:
        return None
    size = struct.calcsize("3i")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, peercred, size)
    except OSError:
        return None
    if len(raw) != size:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid if uid >= 0 else None


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    """Deployment-configurable admission limits (design §10.1).

    Every value must be a positive integer (``timeout_seconds`` may be a
    positive float).  Invalid limits are a configuration error and fail at
    construction: the gateway must never start unlimited.
    """

    max_connections_per_task: int = 8
    max_connections_global: int = 64
    max_rps_per_task: int = 16
    max_rps_global: int = 200
    max_request_bytes: int = 65536
    timeout_seconds: float = 5

    def __post_init__(self) -> None:
        for name in (
            "max_connections_per_task",
            "max_connections_global",
            "max_rps_per_task",
            "max_rps_global",
            "max_request_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive int")
        timeout = self.timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ValueError("timeout_seconds must be a positive number")


@dataclass(frozen=True, slots=True)
class LimitDecision:
    """Outcome of one admission or rate check.

    ``limit`` names the ceiling that rejected the call so the audit event can
    carry the stable ``limit`` field; it is ``None`` when the call was allowed.
    """

    allowed: bool
    limit: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be a bool")
        if self.limit is not None and self.limit not in LIMIT_NAMES:
            raise ValueError("unknown limit name")
        if self.allowed and self.limit is not None:
            raise ValueError("an allowed decision has no limit")


class _Bucket:
    """Token bucket for one peer UID (or the global ceiling)."""

    __slots__ = ("last", "tokens")

    def __init__(self, capacity: float, now: float) -> None:
        self.tokens = capacity
        self.last = now

    def take(self, rate: float, capacity: float, now: float) -> bool:
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(capacity, self.tokens + elapsed * rate)
        self.last = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def refund(self, capacity: float) -> None:
        self.tokens = min(capacity, self.tokens + 1.0)

    def idle_since(self, now: float) -> float:
        return now - self.last


class RateLimiter:
    """Per-peer-UID and global admission control (design §10.1).

    Counters use a monotonic clock.  Connections are plain counters released in
    a ``finally``; requests use a token bucket whose burst equals its rate.
    The per-task ceiling is always checked first, so one task can never consume
    the global quota.
    """

    def __init__(
        self,
        limits: GatewayLimits,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(limits, GatewayLimits):
            raise ValueError("limits must be GatewayLimits")
        if monotonic is not None and not callable(monotonic):
            raise ValueError("monotonic must be callable")
        self._limits = limits
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._lock = threading.Lock()
        self._connections: dict[int | None, int] = {}
        self._connections_global = 0
        self._buckets: dict[int | None, _Bucket] = {}
        self._global_bucket: _Bucket | None = None
        self._last_prune = float("-inf")

    def allow_connection(self, peer_uid: int | None) -> LimitDecision:
        """Admit one connection, per-task first and then global."""

        with self._lock:
            count = self._connections.get(peer_uid, 0)
            if count >= self._limits.max_connections_per_task:
                return LimitDecision(False, LIMIT_PER_TASK_CONNECTIONS)
            if self._connections_global >= self._limits.max_connections_global:
                return LimitDecision(False, LIMIT_GLOBAL_CONNECTIONS)
            self._connections[peer_uid] = count + 1
            self._connections_global += 1
            return LimitDecision(True)

    def release_connection(self, peer_uid: int | None) -> None:
        """Release one admitted connection; extra releases are ignored."""

        with self._lock:
            count = self._connections.get(peer_uid, 0)
            if count <= 0:
                return
            if count == 1:
                del self._connections[peer_uid]
            else:
                self._connections[peer_uid] = count - 1
            if self._connections_global > 0:
                self._connections_global -= 1

    def allow_request(self, peer_uid: int | None) -> LimitDecision:
        """Admit one request, per-task first and then global."""

        now = self._monotonic()
        with self._lock:
            task_rate = float(self._limits.max_rps_per_task)
            bucket = self._buckets.get(peer_uid)
            if bucket is None:
                self._maybe_prune(now)
                bucket = _Bucket(task_rate, now)
                self._buckets[peer_uid] = bucket
            if not bucket.take(task_rate, task_rate, now):
                return LimitDecision(False, LIMIT_PER_TASK_RPS)

            global_bucket = self._global_bucket
            if global_bucket is None:
                global_bucket = _Bucket(
                    float(self._limits.max_rps_global), now
                )
                self._global_bucket = global_bucket
            global_rate = float(self._limits.max_rps_global)
            if not global_bucket.take(global_rate, global_rate, now):
                # A global refusal must not also drain the task's own bucket,
                # otherwise a saturated neighbour would starve this task twice.
                bucket.refund(task_rate)
                return LimitDecision(False, LIMIT_GLOBAL_RPS)
            return LimitDecision(True)

    def _maybe_prune(self, now: float) -> None:
        """Drop idle per-task buckets so the map cannot grow without bound.

        IDLE_SECONDS is the design's reclamation window; the scan itself runs
        at most once a second so a burst of new peers stays cheap.
        """

        if now - self._last_prune < 1.0:
            return
        self._last_prune = now
        for key in list(self._buckets):
            if (
                self._buckets[key].idle_since(now) > _IDLE_SECONDS
                and not self._connections.get(key, 0)
            ):
                del self._buckets[key]


class AuditSink(Protocol):
    """Destination for gateway audit events (design §10.6)."""

    def record(self, event: Mapping[str, object]) -> None: ...


class ResourceBackend(Protocol):
    """Future database/object-store backends implement the same contract."""

    def read(
        self, resource_id: str, *, scope: ResourceScope, capability: str
    ) -> dict[str, object]: ...


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


class FileResourceBackend:
    """Read-only, scope-checked reader for deployment-provided JSON resources.

    The root must already exist as a directory; it is never created.  A
    resource is one ``<resource_id>.json`` regular file under that root, no
    larger than :data:`MAX_RESOURCE_BYTES`.  The backend repeats the scope
    check so a defect in the gateway cannot turn a resource read into an
    arbitrary file read (design §10.5).

    The "allowlist" is the set of files present under the root: a deployment
    installs resource files before the gateway serves them, and a file that is
    absent, a symlink, a directory or an oversized blob is denied.
    """

    def __init__(self, root: Path) -> None:
        try:
            root_path = Path(root)
            info = os.stat(root_path)
        except (OSError, TypeError, ValueError) as exc:
            raise SecurityError("unknown", "resources_root_invalid") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise SecurityError("unknown", "resources_root_invalid")
        self._root = root_path
        self._root_real = os.path.realpath(root_path)

    def read(
        self, resource_id: str, *, scope: ResourceScope, capability: str
    ) -> dict[str, object]:
        """Return ``{"resource_id", "media_type", "body"}`` or raise."""

        if (
            not isinstance(resource_id, str)
            or _RESOURCE_ID_PATTERN.fullmatch(resource_id) is None
        ):
            raise SecurityError("resource_denied", "resource_id_invalid")
        if (
            "/" in resource_id
            or "\\" in resource_id
            or ".." in resource_id
            or "\x00" in resource_id
        ):
            raise SecurityError("resource_denied", "resource_id_invalid")
        if (
            not isinstance(capability, str)
            or capability.partition("@")[0] != _READ_CAPABILITY_NAME
        ):
            raise SecurityError("resource_denied", "capability_denied")
        if not isinstance(scope, ResourceScope):
            raise SecurityError("resource_scope_denied", "scope_invalid")
        if (
            resource_id not in scope.resource_ids
            or resource_id in scope.exclude_resource_ids
        ):
            raise SecurityError("resource_scope_denied", "resource_out_of_scope")

        path = self._root / f"{resource_id}{_RESOURCE_SUFFIX}"
        if not _is_within(os.path.realpath(path), self._root_real):
            raise SecurityError("resource_denied", "resource_outside_root")

        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            # Missing, a symlink (ELOOP) or otherwise unreadable.
            raise SecurityError("resource_denied", "resource_unavailable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SecurityError("resource_denied", "resource_not_regular")
            if info.st_size > MAX_RESOURCE_BYTES:
                raise SecurityError("resource_denied", "resource_too_large")
            raw = self._read_exactly(fd)
        except OSError as exc:
            raise SecurityError("backend_error", "resource_unreadable") from exc
        finally:
            os.close(fd)

        try:
            body = _load_json(raw)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise SecurityError("backend_error", "resource_invalid") from exc
        if not isinstance(body, dict):
            raise SecurityError("backend_error", "resource_invalid")
        return {
            "resource_id": resource_id,
            "media_type": "application/json",
            "body": body,
        }

    @staticmethod
    def _read_exactly(fd: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_RESOURCE_BYTES:
                # The file grew after fstat; refuse rather than read on.
                raise SecurityError("resource_denied", "resource_too_large")
            chunks.append(chunk)


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Everything the gateway needs; supplied by the harness or #83 wiring."""

    socket_path: Path
    resources_root: Path
    validator: CredentialValidator
    revocation_store: RevocationStore
    grant_provider: GrantStatusProvider
    audit_sink: AuditSink
    limits: GatewayLimits
    clock: Callable[[], datetime]

    def __post_init__(self) -> None:
        if not isinstance(self.socket_path, Path):
            raise ValueError("socket_path must be a Path")
        if not isinstance(self.resources_root, Path):
            raise ValueError("resources_root must be a Path")
        if not isinstance(self.limits, GatewayLimits):
            raise ValueError("limits must be GatewayLimits")
        if not callable(self.clock):
            raise ValueError("clock must be callable")
        for attribute, owner in (
            ("validate", self.validator),
            ("validate_call", self.validator),
            ("lookup", self.revocation_store),
            ("status", self.grant_provider),
            ("record", self.audit_sink),
        ):
            if not callable(getattr(owner, attribute, None)):
                raise ValueError(f"{attribute} is not callable")


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    """One NDJSON response body (design §10.2).

    A denial carries only ``ok`` and a stable reason code: never exception text,
    paths, Grant detail or a token.
    """

    ok: bool
    result: dict[str, object] | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise ValueError("ok must be a bool")
        if self.ok:
            if self.reason is not None or not isinstance(self.result, dict):
                raise ValueError("a success needs exactly one result")
        elif self.result is not None or self.reason not in SECURITY_ERROR_CODES:
            raise ValueError("a denial needs exactly one stable reason")

    def as_dict(self) -> dict[str, object]:
        if self.ok:
            return {"ok": True, "result": self.result}
        return {"ok": False, "reason": self.reason}


class GatewayServer:
    """The policy enforcement point: validation chain plus Unix-socket server.

    ``handle_request`` is pure apart from the audit write and the backend read,
    so tests can drive every branch without a socket.  ``start``/``stop`` own
    the single ``AF_UNIX`` listener and are safe to call once each.
    """

    def __init__(self, config: GatewayConfig) -> None:
        if not isinstance(config, GatewayConfig):
            raise ValueError("config must be GatewayConfig")
        self._config = config
        self._limits = config.limits
        self._limiter = RateLimiter(config.limits)
        self._backend = FileResourceBackend(config.resources_root)
        self._probe_revocation_store()
        self._lock = threading.Lock()
        self._server: _UnixGatewayServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def config(self) -> GatewayConfig:
        return self._config

    # ------------------------------------------------------------------
    # Validation chain (design §10.4, steps 3-13)
    # ------------------------------------------------------------------
    def handle_request(
        self,
        request: Mapping[str, object] | bytes | bytearray | memoryview,
        *,
        peer_uid: int | None = None,
    ) -> GatewayResponse:
        """Validate one request and return its response.

        ``request`` is either the raw NDJSON line (as the socket handler passes
        it) or an already-parsed mapping (for direct calls).  Raw input is
        length-checked before parsing; a mapping has, by construction, already
        been parsed by the caller.
        """

        parsed = self._parse_request(request)
        if isinstance(parsed, GatewayResponse):
            return self._deny(parsed.reason, peer_uid=peer_uid)

        fields: dict[str, str] = {}
        if set(parsed) != _REQUEST_FIELDS:
            return self._deny("bad_request", peer_uid=peer_uid)
        version = parsed["version"]
        token = parsed["token"]
        capability = parsed["capability"]
        resource = parsed["resource"]
        payload = parsed["payload"]
        if not all(
            isinstance(value, str) and value
            for value in (version, token, capability, resource)
        ):
            return self._deny("bad_request", peer_uid=peer_uid)
        if not isinstance(payload, Mapping) or payload:
            return self._deny("bad_request", peer_uid=peer_uid)

        fields["capability"] = capability
        fields["resource"] = resource
        if version != GATEWAY_PROTOCOL_VERSION:
            return self._deny("unsupported_version", peer_uid=peer_uid, **fields)

        claims = self._validated_claims(token, peer_uid=peer_uid, fields=fields)
        if isinstance(claims, GatewayResponse):
            return claims
        fields["task_id"] = claims.task_id
        fields["grant_id"] = claims.grant_id
        fields["credential_jti"] = claims.jti

        try:
            revoked = self._config.revocation_store.lookup(claims.jti)
        except Exception:
            return self._deny("unknown", peer_uid=peer_uid, **fields)
        if revoked is not None:
            return self._deny("credential_revoked", peer_uid=peer_uid, **fields)

        try:
            grant = self._config.grant_provider.status(
                claims.grant_id, task_id=claims.task_id
            )
        except Exception:
            return self._deny("unknown", peer_uid=peer_uid, **fields)
        if grant is None:
            return self._deny("grant_missing", peer_uid=peer_uid, **fields)
        if not isinstance(grant, GrantStatus):
            return self._deny("unknown", peer_uid=peer_uid, **fields)

        try:
            self._config.validator.validate_call(
                claims,
                grant_status=grant,
                capability=capability,
                resource=resource,
            )
        except SecurityError as exc:
            return self._deny(
                exc.code if exc.code in _CALL_REASONS else "unknown",
                peer_uid=peer_uid,
                **fields,
            )

        try:
            result = self._backend.read(
                resource, scope=claims.resource_scope, capability=capability
            )
        except SecurityError as exc:
            return self._deny(
                exc.code if exc.code in _BACKEND_REASONS else "backend_error",
                peer_uid=peer_uid,
                **fields,
            )
        except TimeoutError:
            return self._deny("timeout", peer_uid=peer_uid, **fields)
        except Exception:
            return self._deny("backend_error", peer_uid=peer_uid, **fields)

        return self._allow(result, peer_uid=peer_uid, **fields)

    def _validated_claims(
        self,
        token: str,
        *,
        peer_uid: int | None,
        fields: Mapping[str, str],
    ) -> CredentialClaims | GatewayResponse:
        try:
            return self._config.validator.validate(token)
        except SecurityError as exc:
            return self._deny(
                exc.code if exc.code in _TOKEN_REASONS else "unknown",
                peer_uid=peer_uid,
                **fields,
            )

    def _parse_request(
        self,
        request: Mapping[str, object] | bytes | bytearray | memoryview,
    ) -> Mapping[str, object] | GatewayResponse:
        if isinstance(request, (bytes, bytearray, memoryview)):
            raw = bytes(request)
            if len(raw) > self._limits.max_request_bytes:
                return GatewayResponse(ok=False, reason="payload_too_large")
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if b"\n" in raw:
                return GatewayResponse(ok=False, reason="bad_request")
            try:
                value = _load_json(raw)
            except (UnicodeDecodeError, ValueError, RecursionError):
                return GatewayResponse(ok=False, reason="bad_request")
            if not isinstance(value, Mapping):
                return GatewayResponse(ok=False, reason="bad_request")
            return value
        if isinstance(request, Mapping):
            return request
        return GatewayResponse(ok=False, reason="bad_request")

    # ------------------------------------------------------------------
    # Audit (design §10.6)
    # ------------------------------------------------------------------
    def _timestamp(self) -> str:
        moment = self._config.clock()
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return moment.astimezone(UTC).isoformat()

    def _record(
        self,
        *,
        decision: str,
        peer_uid: int | None,
        reason: str | None = None,
        limit: str | None = None,
        **fields: str | None,
    ) -> None:
        event: dict[str, object] = {
            "ts": self._timestamp(),
            "event": (
                AUDIT_EVENT_ALLOW if decision == "allow" else AUDIT_EVENT_DENY
            ),
            "decision": decision,
            "peer_uid": peer_uid,
        }
        for name in ("task_id", "grant_id", "credential_jti", "capability", "resource"):
            value = fields.get(name)
            if value is not None:
                event[name] = value
        if reason is not None:
            event["reason"] = reason
        if limit is not None:
            event["limit"] = limit
        if not set(event) <= AUDIT_FIELDS:
            raise RuntimeError("audit event outside the whitelist")
        self._config.audit_sink.record(event)

    def _allow(
        self, result: dict[str, object], *, peer_uid: int | None, **fields: str
    ) -> GatewayResponse:
        try:
            self._record(decision="allow", peer_uid=peer_uid, **fields)
        except Exception:
            # No audit, no execution: fail closed without a body.
            return GatewayResponse(ok=False, reason="unknown")
        return GatewayResponse(ok=True, result=result)

    def _deny(
        self,
        reason: str,
        *,
        peer_uid: int | None,
        limit: str | None = None,
        **fields: str,
    ) -> GatewayResponse:
        try:
            self._record(
                decision="deny",
                peer_uid=peer_uid,
                reason=reason,
                limit=limit,
                **fields,
            )
        except Exception:
            # The call is already denied; an unauditable denial stays a denial.
            pass
        return GatewayResponse(ok=False, reason=reason)

    def _audit_deny(
        self, reason: str, peer_uid: int | None, *, limit: str | None = None
    ) -> None:
        try:
            self._record(
                decision="deny", peer_uid=peer_uid, reason=reason, limit=limit
            )
        except Exception:
            # The call is already denied; an unauditable denial stays a denial.
            pass

    # ------------------------------------------------------------------
    # Startup checks and listener lifecycle
    # ------------------------------------------------------------------
    def _probe_revocation_store(self) -> None:
        """Fail closed unless the revocation store can answer right now.

        ``FileRevocationStore`` creates a missing root, so this proves the store
        is readable now; #83 must additionally verify that the mount itself
        pre-exists (see the module docstring).
        """

        try:
            record = self._config.revocation_store.lookup(_PROBE_JTI)
        except Exception as exc:
            raise SecurityError(
                "unknown", "revocation_store_unavailable"
            ) from exc
        if record is not None:
            raise SecurityError("unknown", "revocation_store_unexpected_record")

    def start(self) -> None:
        """Bind the Unix socket and serve in a background thread."""

        with self._lock:
            if self._server is not None:
                raise RuntimeError("gateway already started")
            path = self._config.socket_path
            if path.exists():
                # Never clobber a socket another process may be serving.
                raise SecurityError("unknown", "gateway_socket_exists")
            try:
                server = _UnixGatewayServer(str(path), self)
            except OSError as exc:
                raise SecurityError(
                    "unknown", "gateway_socket_unavailable"
                ) from exc
            try:
                # MVP test mode (design §10.1); production uses 0660 + a group.
                os.chmod(path, 0o666)
            except OSError as exc:
                server.server_close()
                raise SecurityError(
                    "unknown", "gateway_socket_unavailable"
                ) from exc
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="agentmgr-pep-gateway",
                daemon=True,
            )
            try:
                thread.start()
            except BaseException:
                server.server_close()
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            self._server = server
            self._thread = thread

    def stop(self) -> None:
        """Stop serving, close the listener and unlink the socket."""

        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=self._limits.timeout_seconds + 1)
        try:
            self._config.socket_path.unlink()
        except OSError:
            pass


class _GatewayRequestHandler(socketserver.StreamRequestHandler):
    """Serve exactly one NDJSON request on one admitted connection."""

    def handle(self) -> None:
        owner = self.server._owner  # type: ignore[attr-defined]
        peer_uid = _peer_uid(self.request)
        try:
            self.request.settimeout(owner._limits.timeout_seconds)
            decision = owner._limiter.allow_request(peer_uid)
            if not decision.allowed:
                owner._audit_deny(
                    "rate_limited", peer_uid, limit=decision.limit
                )
                self._send(GatewayResponse(ok=False, reason="rate_limited"))
                return

            raw = self.rfile.readline(owner._limits.max_request_bytes + 1)
            if len(raw) > owner._limits.max_request_bytes:
                owner._audit_deny("payload_too_large", peer_uid)
                self._send(GatewayResponse(ok=False, reason="payload_too_large"))
                return

            try:
                response = owner.handle_request(raw, peer_uid=peer_uid)
            except Exception:
                # A defect must not leak detail or leave the peer waiting.
                owner._audit_deny("unknown", peer_uid)
                response = GatewayResponse(ok=False, reason="unknown")
            self._send(response)
        except TimeoutError:
            owner._audit_deny("timeout", peer_uid)
            self._try_send(GatewayResponse(ok=False, reason="timeout"))
        except OSError:
            pass

    def _send(self, response: GatewayResponse) -> None:
        self.wfile.write(_encode_response(response))
        self.wfile.flush()

    def _try_send(self, response: GatewayResponse) -> None:
        try:
            self._send(response)
        except OSError:
            pass


class _UnixGatewayServer(socketserver.ThreadingUnixStreamServer):
    """Threaded Unix-socket server with admission and no error output."""

    daemon_threads = True
    allow_reuse_address = False
    # Idle connections wait for a request; accept must never queue behind them.
    request_queue_size = 32

    def __init__(self, socket_path: str, owner: GatewayServer) -> None:
        self._owner = owner
        super().__init__(socket_path, _GatewayRequestHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        # The default handler prints a traceback; that would disclose paths
        # and request detail.  Every served path already audits its outcome.
        return None

    def process_request(self, request: socket.socket, client_address: object) -> None:
        peer_uid = _peer_uid(request)
        decision = self._owner._limiter.allow_connection(peer_uid)
        if not decision.allowed:
            self._owner._audit_deny(
                "busy", peer_uid, limit=decision.limit
            )
            try:
                request.settimeout(self._owner._limits.timeout_seconds)
                request.sendall(
                    _encode_response(GatewayResponse(ok=False, reason="busy"))
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # Thread creation failed after the slot was taken.
            self._owner._limiter.release_connection(peer_uid)
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: object
    ) -> None:
        peer_uid = _peer_uid(request)
        try:
            super().process_request_thread(request, client_address)
        finally:
            # Always release, even when handler setup failed.
            self._owner._limiter.release_connection(peer_uid)
