"""Task-scoped model egress tokens for the Hermes managed execution broker.

Design basis: ``docs/security/hermes-managed-execution-design.md`` §9.2.  The
token is the *only* model credential a sandbox holds; the real provider key stays
in the host broker.  The wire format is::

    egt.<base64url(canonical payload JSON)>.<base64url(HMAC-SHA256)>

with base64url **without** padding, payload JSON serialized with sorted keys and
compact separators, and a tag that is the HMAC-SHA256 of the exact payload bytes.

Guarantees enforced here:

* claims are structurally validated (exact key set, non-empty bounded ids,
  provider host/path shape, timezone-aware timestamps, TTL 1..300 seconds);
* verification is constant time (``hmac.compare_digest``), rejects tampering,
  wrong keys, malformed/non-canonical encodings, expiry and use before issue;
* the raw token and the signing key never appear in an error message so callers
  can log ``str(exc)`` safely.

This module holds two layers:

* the pure token helpers above (:func:`mint_egress_token`,
  :func:`verify_egress_token`, :func:`egress_token_hash`) plus the revocation
  marker contract (:func:`revocation_markers`,
  :func:`write_revocation_markers`), and
* the host egress broker (Task 4, design §9.3): a Unix-socket HTTP bridge that
  validates a task token, checks revocation and the required task/manifest
  binding, enforces the token's ``model`` claim for POST model routes, injects
  the real provider key and streams the allowlisted OpenAI-compatible call
  upstream.  The sandbox never sees the provider key.

The broker speaks HTTP over ``AF_UNIX`` only.  It never listens on an IP port,
never follows upstream redirects, never parses a prompt, never caches a body and
never writes a token, key, prompt or upstream body to a log line.  Concurrent
connections are bounded (``max_connections``, 503 on overflow) and every request
has an overall wall-clock deadline in addition to the per-I/O timeout.

Revocation contract for the managed adapter (Task 5): after a task is stopped,
cleaned up, or its result has been read, call
:func:`write_revocation_markers` with the task's claims and token digest.  The
broker re-reads the directory on every request, so a token cannot outlive the
task even if an earlier copy leaked into the sandbox.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import grp
import hashlib
import hmac
import http.client
import http.server
import json
import logging
import os
import re
import signal
import socket
import socketserver
import ssl
import stat
import threading
import time
import types
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "BROKER_MAX_CONNECTIONS",
    "BROKER_REQUEST_TIMEOUT_SECONDS",
    "BROKER_SOCKET_GROUP",
    "BROKER_SOCKET_MODE",
    "DEFAULT_ALLOWED_ROUTES",
    "EGRESS_TOKEN_HEADER",
    "MAX_EGRESS_TOKEN_LENGTH",
    "MAX_EGRESS_TTL_SECONDS",
    "REVOCATION_MARKER_KINDS",
    "TOKEN_PREFIX",
    "VOLATILE_SOCKET_ROOTS",
    "BrokerConfig",
    "BrokerConfigError",
    "BrokerError",
    "BrokerResponse",
    "EgressBroker",
    "EgressClaims",
    "EgressTokenError",
    "egress_token_hash",
    "load_broker_config",
    "main",
    "mint_egress_token",
    "revocation_markers",
    "run_broker",
    "verify_egress_token",
    "write_revocation_markers",
]

#: Fixed token prefix; also the first dot-separated segment of every token.
TOKEN_PREFIX = "egt"

#: Hard upper bound for a task-scoped egress token lifetime.
MAX_EGRESS_TTL_SECONDS = 300

#: Defensive bound so a hostile string cannot drive large decoding work.
MAX_EGRESS_TOKEN_LENGTH = 4096

#: Minimum HMAC key length in bytes (256-bit keys only).
_MIN_KEY_BYTES = 32

_PAYLOAD_KEYS = frozenset(
    {
        "v",
        "task_id",
        "grant_id",
        "unit",
        "model",
        "provider_host",
        "path_prefix",
        "issued_at",
        "expires_at",
        "nonce",
    }
)

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$"
)

_MAX_ID_LENGTH = 200
_MAX_MODEL_LENGTH = 200
_MAX_HOST_LENGTH = 255
_MAX_PATH_PREFIX_LENGTH = 128


class EgressTokenError(Exception):
    """Stable egress token failure.

    ``reason`` is a short constant code suitable for logs.  Neither the raw
    token nor the signing key is ever interpolated into it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EgressClaims:
    """One task-scoped egress grant.

    This object never holds the token itself, so it is safe to log or include in
    structured errors.  ``provider_host``/``path_prefix`` pin the only upstream
    destination the token authorises; ``unit``/``task_id``/``grant_id`` bind it
    to a single sandbox and attempt.
    """

    version: int
    task_id: str
    grant_id: str
    unit: str
    model: str
    provider_host: str
    path_prefix: str
    issued_at: datetime
    expires_at: datetime
    nonce: str

    @property
    def ttl_seconds(self) -> int:
        """Whole-second lifetime; claims shorter than one second are invalid."""

        return int((self.expires_at - self.issued_at).total_seconds())


def mint_egress_token(
    claims: EgressClaims, *, key: bytes, now: datetime | None = None
) -> str:
    """Serialize and sign ``claims``.

    ``now`` (UTC) is used only to refuse minting a token that is already expired
    or not yet valid; it never widens the structural TTL bound.
    """

    signing_key = _require_key(key)
    _validate_claims(claims)
    current = _require_now(now)
    if claims.expires_at <= current:
        raise EgressTokenError("egress claims are already expired")
    if claims.issued_at > current:
        raise EgressTokenError("egress claims are not valid yet")

    payload = _claims_document(claims)
    raw_payload = _canonical_json(payload)
    signature = hmac.new(signing_key, raw_payload, hashlib.sha256).digest()
    token = (
        f"{TOKEN_PREFIX}.{_b64encode(raw_payload)}.{_b64encode(signature)}"
    )
    if len(token) > MAX_EGRESS_TOKEN_LENGTH:
        raise EgressTokenError("egress token is too long")
    return token


def verify_egress_token(
    token: str, *, key: bytes, now: datetime | None = None
) -> EgressClaims:
    """Verify a token and return its claims, or raise :class:`EgressTokenError`.

    The signature is compared in constant time before the payload is parsed; a
    bad key, a tampered segment, a non-canonical encoding, expiry and use before
    issue are all rejected with a stable, secret-free reason.
    """

    signing_key = _require_key(key)
    current = _require_now(now)
    raw_payload, provided_signature = _split_token(token)

    expected_signature = hmac.new(signing_key, raw_payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise EgressTokenError("egress token signature mismatch")

    claims = _claims_from_document(_parse_payload(raw_payload))
    if claims.expires_at <= current:
        raise EgressTokenError("egress token expired")
    if claims.issued_at > current:
        raise EgressTokenError("egress token is not valid yet")
    return claims


def egress_token_hash(token: str) -> str:
    """Return the hex SHA-256 of a token for manifests/evidence.

    The token is opaque to callers; only this digest belongs in a sandbox spec,
    manifest, log line or evidence record.
    """

    if not isinstance(token, str) or not token:
        raise EgressTokenError("egress token hash input is invalid")
    if len(token) > MAX_EGRESS_TOKEN_LENGTH:
        raise EgressTokenError("egress token hash input is invalid")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _require_key(key: object) -> bytes:
    if not isinstance(key, bytes) or len(key) < _MIN_KEY_BYTES:
        raise EgressTokenError(
            f"egress signing key must be at least {_MIN_KEY_BYTES} bytes"
        )
    return key


def _require_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise EgressTokenError("verification time must be timezone-aware")
    return now.astimezone(UTC)


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(segment: str, reason: str) -> bytes:
    if not segment or not _B64URL_RE.fullmatch(segment):
        raise EgressTokenError(reason)
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError):
        raise EgressTokenError(reason) from None
    # Reject non-canonical trailing bits so two different strings can never
    # decode to the same signature bytes.
    if _b64encode(raw) != segment:
        raise EgressTokenError(reason)
    return raw


def _split_token(token: object) -> tuple[bytes, bytes]:
    if not isinstance(token, str) or not token:
        raise EgressTokenError("egress token is required")
    if len(token) > MAX_EGRESS_TOKEN_LENGTH:
        raise EgressTokenError("egress token is too long")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise EgressTokenError("malformed egress token")
    raw_payload = _b64decode(parts[1], "malformed egress token")
    signature = _b64decode(parts[2], "malformed egress token")
    if len(signature) != hashlib.sha256().digest_size:
        raise EgressTokenError("malformed egress token")
    return raw_payload, signature


def _parse_payload(raw_payload: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EgressTokenError("egress token payload is malformed") from None
    if not isinstance(payload, dict):
        raise EgressTokenError("egress token payload is malformed")
    if set(payload) != _PAYLOAD_KEYS:
        raise EgressTokenError("egress token claims are invalid")
    if payload["v"] not in (1,) or type(payload["v"]) is not int:
        raise EgressTokenError("egress token claims are invalid")
    if _canonical_json(payload) != raw_payload:
        raise EgressTokenError("egress token payload is not canonical")
    return payload


def _claims_document(claims: EgressClaims) -> dict[str, object]:
    return {
        "v": claims.version,
        "task_id": claims.task_id,
        "grant_id": claims.grant_id,
        "unit": claims.unit,
        "model": claims.model,
        "provider_host": claims.provider_host,
        "path_prefix": claims.path_prefix,
        "issued_at": _iso_utc(claims.issued_at),
        "expires_at": _iso_utc(claims.expires_at),
        "nonce": claims.nonce,
    }


def _claims_from_document(document: dict[str, object]) -> EgressClaims:
    claims = EgressClaims(
        version=document["v"],  # type: ignore[arg-type]
        task_id=document["task_id"],  # type: ignore[arg-type]
        grant_id=document["grant_id"],  # type: ignore[arg-type]
        unit=document["unit"],  # type: ignore[arg-type]
        model=document["model"],  # type: ignore[arg-type]
        provider_host=document["provider_host"],  # type: ignore[arg-type]
        path_prefix=document["path_prefix"],  # type: ignore[arg-type]
        issued_at=_parse_timestamp(document["issued_at"]),
        expires_at=_parse_timestamp(document["expires_at"]),
        nonce=document["nonce"],  # type: ignore[arg-type]
    )
    _validate_claims(claims)
    return claims


def _validate_claims(claims: object) -> None:
    if not isinstance(claims, EgressClaims):
        raise EgressTokenError("egress claims are invalid")
    if type(claims.version) is not int or claims.version != 1:
        raise EgressTokenError("egress claims are invalid")
    _require_identifier(claims.task_id, "egress claims are invalid")
    _require_identifier(claims.grant_id, "egress claims are invalid")
    _require_identifier(claims.unit, "egress claims are invalid")
    _require_text(claims.model, _MAX_MODEL_LENGTH, "egress claims are invalid")
    _require_host(claims.provider_host)
    _require_path_prefix(claims.path_prefix)
    _require_nonce(claims.nonce)
    _require_aware(claims.issued_at, "egress claims are invalid")
    _require_aware(claims.expires_at, "egress claims are invalid")
    if claims.expires_at <= claims.issued_at:
        raise EgressTokenError("egress claims are invalid")
    ttl = (claims.expires_at - claims.issued_at).total_seconds()
    if ttl < 1 or ttl > MAX_EGRESS_TTL_SECONDS:
        raise EgressTokenError("egress claims are invalid")


def _require_aware(value: object, reason: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise EgressTokenError(reason)


def _require_text(value: object, max_length: int, reason: str) -> None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise EgressTokenError(reason)
    if value != value.strip():
        raise EgressTokenError(reason)
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise EgressTokenError(reason)


def _require_identifier(value: object, reason: str) -> None:
    _require_text(value, _MAX_ID_LENGTH, reason)


def _require_host(value: object) -> None:
    reason = "egress claims are invalid"
    _require_text(value, _MAX_HOST_LENGTH, reason)
    if not _HOST_RE.fullmatch(value):  # type: ignore[arg-type]
        raise EgressTokenError(reason)


def _require_path_prefix(value: object) -> None:
    reason = "egress claims are invalid"
    _require_text(value, _MAX_PATH_PREFIX_LENGTH, reason)
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.rstrip("/") == ""
        or ".." in value
        or "?" in value
        or "#" in value
    ):
        # "/" (or "///") would authorise every allowlisted route; the token must
        # pin a concrete prefix such as "/v1".
        raise EgressTokenError(reason)


def _require_nonce(value: object) -> None:
    if not isinstance(value, str) or not _NONCE_RE.fullmatch(value):
        raise EgressTokenError("egress claims are invalid")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EgressTokenError("egress token claims are invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise EgressTokenError("egress token claims are invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EgressTokenError("egress token claims are invalid")
    parsed = parsed.astimezone(UTC)
    if _iso_utc(parsed) != value:
        raise EgressTokenError("egress token claims are invalid")
    return parsed


# ---------------------------------------------------------------------------
# host egress broker (design §9.3)
# ---------------------------------------------------------------------------

#: Header the sandbox proxy uses to present the task egress token.
EGRESS_TOKEN_HEADER = "X-AgentMgr-Egress-Token"
_TOKEN_HEADER_LOWER = EGRESS_TOKEN_HEADER.lower()

#: Fixed socket contract from design §9.1 (group-shared, never world-accessible).
BROKER_SOCKET_GROUP = "agentmgr-hermes"
BROKER_SOCKET_MODE = 0o660

#: The only upstream routes a task token may reach.  This is exactly the
#: sandbox proxy's ``ALLOWED_REQUESTS`` (``ops/security/agentmgr_hermes_workload.py``);
#: both sides must agree or traffic is dropped before it reaches the broker.
#: ``/v1/responses`` is deliberately absent: the pinned Hermes client only calls
#: ``/v1/chat/completions`` and widening the allowlist here would not be reachable
#: through the proxy anyway.
DEFAULT_ALLOWED_ROUTES: Mapping[str, frozenset[str]] = types.MappingProxyType(
    {
        "POST": frozenset(
            {
                "/v1/chat/completions",
                "/v1/completions",
                "/v1/embeddings",
            }
        ),
        "GET": frozenset({"/v1/models"}),
    }
)

#: Revocation marker file-name prefixes (``<kind>.<value>``) understood by
#: :func:`revocation_markers` and checked by :meth:`EgressBroker._is_revoked`.
REVOCATION_MARKER_KINDS: tuple[str, ...] = ("token", "unit", "task", "grant")

#: The control-plane socket may not live in a volatile/masked filesystem: the
#: task unit binds it read-only and Task 2 masks the control directory.  The
#: production path is ``/var/lib/agentmgr-hermes-control/egress.sock``.
VOLATILE_SOCKET_ROOTS: tuple[Path, ...] = (
    Path("/run"),
    Path("/tmp"),
    Path("/var/tmp"),
)

#: Client headers copied upstream.  Everything else -- in particular
#: ``Authorization``, ``X-Api-Key``, cookies and the task token -- is dropped.
_FORWARDED_REQUEST_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "Content-Encoding",
    "Accept",
)
#: Upstream response headers copied back.  Authentication challenges,
#: ``Set-Cookie``, ``Location`` and hop-by-hop headers are never replayed.
_FORWARDED_RESPONSE_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "Content-Encoding",
    "Cache-Control",
    "Retry-After",
    "X-Request-Id",
)

#: Parser-level failures reported by ``BaseHTTPRequestHandler.send_error``.
_PROTOCOL_ERROR_CODES: Mapping[int, str] = {
    400: "request_invalid",
    408: "request_timeout",
    414: "path_too_long",
    431: "request_invalid",
    501: "method_not_allowed",
    505: "request_invalid",
}

#: Stable upstream error mapping (design §9.3: never echo an upstream body).
_UPSTREAM_ERRORS: Mapping[int, tuple[int, str]] = {
    401: (502, "upstream_auth_failed"),
    403: (502, "upstream_auth_failed"),
    404: (502, "upstream_error"),
    408: (504, "upstream_timeout"),
    429: (429, "upstream_rate_limited"),
    500: (502, "upstream_error"),
    502: (502, "upstream_error"),
    503: (503, "upstream_unavailable"),
    504: (504, "upstream_timeout"),
}

BROKER_MAX_REQUEST_BYTES = 8 * 1024 * 1024
BROKER_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
BROKER_UPSTREAM_TIMEOUT_SECONDS = 120.0
#: Overall wall-clock budget for one request, including every I/O operation.
BROKER_REQUEST_TIMEOUT_SECONDS = 300.0
#: Concurrent Unix connections served before new ones get a stable 503.
BROKER_MAX_CONNECTIONS = 32

_MAX_CONNECTIONS_LIMIT = 512
_MAX_REQUEST_BYTES_LIMIT = 64 * 1024 * 1024
_MAX_RESPONSE_BYTES_LIMIT = 128 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 3600.0
_MAX_PATH_CHARS = 2_048
_MAX_QUERY_CHARS = 4_096
_MAX_SECRET_BYTES = 4_096
_MAX_CONFIG_BYTES = 65_536
_MAX_MANIFEST_BYTES = 65_536
_STREAM_CHUNK_BYTES = 65_536
_BUSY_RESPONSE_TIMEOUT_SECONDS = 1.0
_BUSY_DRAIN_SECONDS = 0.2

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_SOCKET = 3
EXIT_UNAVAILABLE = 4

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_METHOD_RE = re.compile(r"^[A-Z]{1,10}$")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,400}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_GROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_OCTAL_RE = re.compile(r"^0[0-7]+$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

logger = logging.getLogger("agentmgr.hermes.egress")


class BrokerError(Exception):
    """Stable broker failure carrying only a short reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BrokerConfigError(BrokerError):
    """The broker configuration is incomplete, unsafe or inconsistent."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _UpstreamTarget:
    """A validated upstream endpoint; never contains credentials."""

    scheme: str
    host: str
    port: int
    base_path: str
    host_header: str


def _parse_upstream(base_url: object, *, allow_insecure: bool) -> _UpstreamTarget:
    if not isinstance(base_url, str) or not base_url:
        raise BrokerConfigError("config_upstream_invalid")
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        raise BrokerConfigError("config_upstream_invalid") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "http"}:
        raise BrokerConfigError("config_upstream_invalid")
    if parsed.username is not None or parsed.password is not None:
        raise BrokerConfigError("config_upstream_invalid")
    if parsed.query or parsed.fragment:
        raise BrokerConfigError("config_upstream_invalid")
    host = parsed.hostname
    if not host or not _HOSTNAME_RE.fullmatch(host):
        raise BrokerConfigError("config_upstream_invalid")
    try:
        port = parsed.port
    except ValueError:
        raise BrokerConfigError("config_upstream_invalid") from None
    default_port = 443 if scheme == "https" else 80
    if port is None:
        port = default_port
    if not 1 <= port <= 65_535:
        raise BrokerConfigError("config_upstream_invalid")
    if scheme == "http":
        # Plain HTTP is only ever allowed for a loopback test upstream.  Every
        # production provider call must be TLS with a verified certificate.
        if not allow_insecure:
            raise BrokerConfigError("config_upstream_invalid")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise BrokerConfigError("config_upstream_invalid")
    raw_path = parsed.path or ""
    if raw_path in {"", "/"}:
        base_path = ""
    elif not raw_path.startswith("/") or ".." in raw_path or "//" in raw_path:
        raise BrokerConfigError("config_upstream_invalid")
    else:
        base_path = raw_path.rstrip("/")
    host_header = host if port == default_port else f"{host}:{port}"
    return _UpstreamTarget(
        scheme=scheme,
        host=host,
        port=port,
        base_path=base_path,
        host_header=host_header,
    )


def _require_absolute_path(value: object, code: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise BrokerConfigError(code)
    if any(part == ".." for part in value.parts):
        raise BrokerConfigError(code)
    return value


def _require_secret(value: object, *, minimum: int, code: str) -> bytes:
    if not isinstance(value, bytes) or not minimum <= len(value) <= _MAX_SECRET_BYTES:
        raise BrokerConfigError(code)
    if any(byte < 0x21 or byte > 0x7E for byte in value):
        raise BrokerConfigError(code)
    return value


def _require_limit(value: object, *, maximum: int, code: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise BrokerConfigError(code)
    return value


def _require_timeout(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrokerConfigError(code)
    timeout = float(value)
    if not 0.05 <= timeout <= _MAX_TIMEOUT_SECONDS:
        raise BrokerConfigError(code)
    return timeout


def _require_auth_prefix(value: object) -> str:
    """Validate an injected credential prefix.

    Accepted: ``""`` (only for a non-``Authorization`` header such as
    ``x-api-key``), ``"Bearer"`` or ``"Bearer "`` (exactly one separator
    space).  Leading whitespace, double spaces and embedded whitespace are
    rejected so the header cannot be smuggled or malformed.
    """

    code = "config_provider_header_invalid"
    if not isinstance(value, str) or len(value) > 256:
        raise BrokerConfigError(code)
    if any(
        character in "\r\n\x00" or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise BrokerConfigError(code)
    if value == "":
        return value
    core = value.rstrip(" ")
    if (
        core == ""
        or value != value.lstrip()
        or any(character.isspace() for character in core)
        or value not in {core, core + " "}
    ):
        raise BrokerConfigError(code)
    return value


def _freeze_routes(
    routes: object,
) -> Mapping[str, frozenset[str]]:
    if not isinstance(routes, Mapping) or not routes:
        raise BrokerConfigError("config_routes_invalid")
    frozen: dict[str, frozenset[str]] = {}
    for method, paths in routes.items():
        if not isinstance(method, str) or not _METHOD_RE.fullmatch(method):
            raise BrokerConfigError("config_routes_invalid")
        if not isinstance(paths, Iterable) or isinstance(paths, (str, bytes)):
            raise BrokerConfigError("config_routes_invalid")
        selected = frozenset(paths)
        if not selected:
            raise BrokerConfigError("config_routes_invalid")
        for path in selected:
            if (
                not isinstance(path, str)
                or not _ROUTE_RE.fullmatch(path)
                or ".." in path
                or "//" in path
                or "?" in path
                or "#" in path
            ):
                raise BrokerConfigError("config_routes_invalid")
        frozen[method] = selected
    return types.MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    """Everything the egress broker needs; supplied by the service loader.

    ``token_key`` and ``provider_key`` are the only secrets.  ``provider_host``
    is the canonical value a token must claim (it may differ from the physical
    upstream host only for the loopback test upstream), and ``task_root`` is
    mandatory: every request must bind to the prepared task manifest, so a
    cleaned-up or cross-task token fails closed.
    """

    socket_path: Path
    upstream_base_url: str
    token_key: bytes
    provider_key: bytes
    revocation_dir: Path
    task_root: Path
    provider_host: str
    max_request_bytes: int = BROKER_MAX_REQUEST_BYTES
    max_response_bytes: int = BROKER_MAX_RESPONSE_BYTES
    upstream_timeout_sec: float = BROKER_UPSTREAM_TIMEOUT_SECONDS
    request_timeout_sec: float = BROKER_REQUEST_TIMEOUT_SECONDS
    max_connections: int = BROKER_MAX_CONNECTIONS
    allowed_routes: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: DEFAULT_ALLOWED_ROUTES
    )
    allow_insecure_upstream: bool = False
    socket_group: str | None = BROKER_SOCKET_GROUP
    socket_mode: int = BROKER_SOCKET_MODE
    provider_key_header: str = "Authorization"
    provider_key_prefix: str = "Bearer "
    clock: Callable[[], datetime] = _utcnow

    def __post_init__(self) -> None:
        _require_absolute_path(self.socket_path, "config_socket_path_invalid")
        _require_absolute_path(self.revocation_dir, "config_revocation_dir_invalid")
        _require_absolute_path(self.task_root, "config_task_root_invalid")
        if type(self.allow_insecure_upstream) is not bool:
            raise BrokerConfigError("config_upstream_invalid")
        # Parsing validates the scheme, TLS requirement and the base path.
        _parse_upstream(
            self.upstream_base_url, allow_insecure=self.allow_insecure_upstream
        )
        if (
            not isinstance(self.provider_host, str)
            or not _HOSTNAME_RE.fullmatch(self.provider_host.split(":", 1)[0])
            or len(self.provider_host) > 255
        ):
            raise BrokerConfigError("config_provider_host_invalid")
        _require_secret(self.token_key, minimum=32, code="config_token_key_invalid")
        _require_secret(self.provider_key, minimum=1, code="config_provider_key_invalid")
        _require_limit(
            self.max_request_bytes,
            maximum=_MAX_REQUEST_BYTES_LIMIT,
            code="config_request_limit_invalid",
        )
        _require_limit(
            self.max_response_bytes,
            maximum=_MAX_RESPONSE_BYTES_LIMIT,
            code="config_response_limit_invalid",
        )
        _require_timeout(self.upstream_timeout_sec, "config_timeout_invalid")
        _require_timeout(self.request_timeout_sec, "config_request_timeout_invalid")
        _require_limit(
            self.max_connections,
            maximum=_MAX_CONNECTIONS_LIMIT,
            code="config_connections_invalid",
        )
        object.__setattr__(self, "allowed_routes", _freeze_routes(self.allowed_routes))
        if self.socket_group is not None and (
            not isinstance(self.socket_group, str)
            or not _GROUP_RE.fullmatch(self.socket_group)
        ):
            raise BrokerConfigError("config_socket_group_invalid")
        if (
            type(self.socket_mode) is not int
            or not 0 <= self.socket_mode <= 0o777
            or self.socket_mode & 0o007
        ):
            # A world-accessible broker socket would let any local process spend
            # the provider key.
            raise BrokerConfigError("config_socket_mode_invalid")
        if (
            not isinstance(self.provider_key_header, str)
            or not _HEADER_NAME_RE.fullmatch(self.provider_key_header)
            or self.provider_key_header.lower() == _TOKEN_HEADER_LOWER
        ):
            raise BrokerConfigError("config_provider_header_invalid")
        prefix = _require_auth_prefix(self.provider_key_prefix)
        if self.provider_key_header.lower() == "authorization" and not prefix:
            # ``Authorization: <bare key>`` is never a valid provider credential.
            raise BrokerConfigError("config_provider_header_invalid")
        if not callable(self.clock):
            raise BrokerConfigError("config_clock_invalid")

    def __repr__(self) -> str:
        return (
            "BrokerConfig(socket_path={!r}, upstream_base_url={!r}, "
            "provider_host={!r}, revocation_dir={!r}, task_root={!r}, "
            "token_key=<redacted>, provider_key=<redacted>, "
            "max_request_bytes={!r}, max_response_bytes={!r}, "
            "upstream_timeout_sec={!r}, request_timeout_sec={!r}, "
            "max_connections={!r}, socket_group={!r}, socket_mode={!r})"
        ).format(
            self.socket_path,
            self.upstream_base_url,
            self.provider_host,
            self.revocation_dir,
            self.task_root,
            self.max_request_bytes,
            self.max_response_bytes,
            self.upstream_timeout_sec,
            self.request_timeout_sec,
            self.max_connections,
            self.socket_group,
            self.socket_mode,
        )


@dataclass(frozen=True, slots=True)
class BrokerResponse:
    """One broker response: status, response headers and a byte/stream body.

    ``body`` is ``bytes`` for a bounded response (including every error) and an
    iterator of chunks for a streamed upstream body (SSE/chunked).  A stream is
    single-use; call :meth:`close` if it will not be consumed.  A buffered body
    never carries ``Content-Length`` here: the socket handler adds exactly one.
    """

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | Iterator[bytes] = b""

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("broker response status is invalid")
        if not isinstance(self.headers, tuple):
            raise ValueError("broker response headers must be a tuple")
        for name, value in self.headers:
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not _HEADER_NAME_RE.fullmatch(name)
                or any(
                    character in "\r\n\x00" or ord(character) < 0x20
                    for character in value
                )
            ):
                raise ValueError("broker response header is invalid")
        if isinstance(self.body, (bytes, bytearray)):
            object.__setattr__(self, "body", bytes(self.body))
        elif not isinstance(self.body, Iterator):
            raise ValueError("broker response body is invalid")

    @classmethod
    def failure(
        cls,
        status: int,
        code: str,
        *,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> "BrokerResponse":
        if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
            raise ValueError("broker error code is invalid")
        body = json.dumps({"error": code}, separators=(",", ":")).encode("ascii")
        # Every stable error is JSON; callers may add Retry-After but not a
        # second/conflicting content type.
        return cls(
            status=status,
            headers=(("Content-Type", "application/json"),) + headers,
            body=body,
        )

    def iter_body(self) -> Iterator[bytes]:
        if isinstance(self.body, bytes):
            yield self.body
            return
        yield from self.body

    def read_body(self) -> bytes:
        return b"".join(self.iter_body())

    def close(self) -> None:
        body = self.body
        if isinstance(body, bytes):
            return
        close = getattr(body, "close", None)
        if callable(close):
            close()


def _normalize_request_headers(headers: object) -> dict[str, tuple[str, ...]]:
    """Lower-case and bound caller headers; reject anything that cannot be sent.

    Only an allowlist is copied upstream, so ``Authorization``, ``X-Api-Key``,
    the task token and unknown headers are dropped rather than rewritten.
    """

    if not isinstance(headers, Mapping):
        raise BrokerError("request_invalid")
    normalized: dict[str, list[str]] = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not _HEADER_NAME_RE.fullmatch(name)
        ):
            raise BrokerError("request_invalid")
        if any(
            character in "\r\n\x00" or ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        ):
            raise BrokerError("request_invalid")
        normalized.setdefault(name.lower(), []).append(value)
    return {name: tuple(values) for name, values in normalized.items()}


def _parse_route(path: str) -> tuple[str, str] | None:
    """Split a request target into an allowlistable route and a safe query."""

    if not isinstance(path, str) or not path or len(path) > _MAX_PATH_CHARS:
        return None
    try:
        parsed = urlsplit(path)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    route = parsed.path
    if (
        not route.startswith("/")
        or not _ROUTE_RE.fullmatch(route)
        or ".." in route
        or "//" in route
    ):
        return None
    query = parsed.query
    if len(query) > _MAX_QUERY_CHARS or any(
        character.isspace() or ord(character) < 0x21 or ord(character) == 0x7F
        for character in query
    ):
        return None
    return route, query


def _marker_path(directory: str, name: str) -> str:
    return os.path.join(directory, name)


def revocation_markers(claims: EgressClaims, token_hash: str) -> tuple[str, ...]:
    """Return the revocation marker names for one task token.

    Contract for the managed adapter (Task 5): on stop, cleanup or a completed
    result read, write every returned name as a zero-byte file into the broker's
    ``revocation_dir`` (see :func:`write_revocation_markers`).  The broker treats
    any existing entry — token digest, unit, task or grant — as revoked.
    """

    if (
        not isinstance(claims, EgressClaims)
        or not isinstance(token_hash, str)
        or not _SHA256_HEX_RE.fullmatch(token_hash)
        or not _SAFE_IDENTIFIER_RE.fullmatch(claims.unit)
        or not _SAFE_IDENTIFIER_RE.fullmatch(claims.task_id)
        or not _SAFE_IDENTIFIER_RE.fullmatch(claims.grant_id)
    ):
        raise ValueError("revocation marker input is invalid")
    values = {
        "token": token_hash,
        "unit": claims.unit,
        "task": claims.task_id,
        "grant": claims.grant_id,
    }
    return tuple(f"{kind}.{values[kind]}" for kind in REVOCATION_MARKER_KINDS)


def write_revocation_markers(
    directory: Path, claims: EgressClaims, token_hash: str
) -> tuple[str, ...]:
    """Write every revocation marker for one token (adapter side, Task 5).

    The broker only reads revocation state; the adapter calls this on stop,
    cleanup and after reading a task result so a short-lived token cannot outlive
    the task even if an earlier copy leaked into the sandbox.
    """

    names = revocation_markers(claims, token_hash)
    if not isinstance(directory, Path):
        raise ValueError("revocation directory is invalid")
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError("revocation directory is invalid")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for name in names:
        descriptor = os.open(_marker_path(str(directory), name), flags, 0o600)
        os.close(descriptor)
    return names


class EgressBroker:
    """Validate one task token, then forward one allowlisted call upstream.

    :meth:`handle` is pure apart from the revocation/manifest reads and the
    upstream call, so every decision is testable without a socket.  The socket
    handler (:class:`_BrokerRequestHandler`) only frames HTTP around it.

    The production entry point (:func:`run_broker`) additionally requires the
    socket to sit outside ``/run``, ``/tmp`` and ``/var/tmp``; an embedder that
    constructs this class directly owns that placement decision.
    """

    def __init__(self, config: BrokerConfig) -> None:
        if not isinstance(config, BrokerConfig):
            raise BrokerConfigError("config_invalid")
        self._config = config
        self._target = _parse_upstream(
            config.upstream_base_url, allow_insecure=config.allow_insecure_upstream
        )
        self._lock = threading.Lock()
        self._server: _BrokerServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def config(self) -> BrokerConfig:
        return self._config

    # ------------------------------------------------------------------
    # request handling
    # ------------------------------------------------------------------
    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> BrokerResponse:
        """Validate one request and return its response.

        Denials happen before any upstream byte is written, and every error body
        is a stable ``{"error": <code>}`` document.
        """

        config = self._config
        deadline = time.monotonic() + config.request_timeout_sec
        if not isinstance(method, str) or not isinstance(path, str):
            return self._deny(400, "request_invalid")
        if not isinstance(body, (bytes, bytearray)):
            return self._deny(400, "request_invalid")
        payload = bytes(body)
        if len(payload) > config.max_request_bytes:
            # The size cap is checked before token work so a hostile caller
            # cannot force HMAC or manifest reads with a huge body.
            return self._deny(413, "request_too_large")
        try:
            normalized = _normalize_request_headers(headers)
        except BrokerError:
            return self._deny(400, "request_invalid")

        tokens = normalized.get(_TOKEN_HEADER_LOWER, ())
        if not tokens:
            return self._deny(401, "token_required")
        if len(tokens) != 1:
            return self._deny(401, "token_invalid")
        token = tokens[0]

        try:
            claims = verify_egress_token(
                token, key=config.token_key, now=config.clock()
            )
        except EgressTokenError as exc:
            code = (
                "token_expired"
                if exc.reason == "egress token expired"
                else "token_invalid"
            )
            return self._deny(401, code)

        if not (
            _SAFE_IDENTIFIER_RE.fullmatch(claims.task_id)
            and _SAFE_IDENTIFIER_RE.fullmatch(claims.grant_id)
            and _SAFE_IDENTIFIER_RE.fullmatch(claims.unit)
        ):
            # Identifiers are also path components below, so reject anything
            # that could escape the task root before touching the filesystem.
            return self._deny(403, "claims_invalid")

        try:
            if self._is_revoked(claims, egress_token_hash(token)):
                return self._deny(401, "token_revoked")
        except BrokerError:
            return self._deny(503, "revocation_unavailable")

        parsed_route = _parse_route(path)
        if parsed_route is None:
            return self._deny(404, "path_not_allowed")
        route, query = parsed_route
        method_name = method.upper()
        if not _METHOD_RE.fullmatch(method_name):
            return self._deny(405, "method_not_allowed")
        allowed = config.allowed_routes.get(method_name, frozenset())
        if route not in allowed:
            if any(route in paths for paths in config.allowed_routes.values()):
                return self._deny(405, "method_not_allowed")
            return self._deny(404, "path_not_allowed")

        if claims.provider_host.lower() != config.provider_host.lower():
            return self._deny(403, "target_not_allowed")
        prefix = claims.path_prefix.rstrip("/")
        if prefix and not (route == prefix or route.startswith(prefix + "/")):
            return self._deny(403, "path_not_allowed")

        if method_name == "POST":
            # Every allowlisted POST route is an OpenAI-compatible model route.
            # Model choice is a trusted profile fact: the token names the model
            # and the body may not silently select another one.  Only the
            # top-level ``model`` field is read; messages/prompt are never parsed
            # or logged.
            try:
                document = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._deny(400, "request_invalid")
            if not isinstance(document, dict):
                return self._deny(400, "request_invalid")
            model = document.get("model")
            if not isinstance(model, str) or model != claims.model:
                return self._deny(403, "model_not_allowed")

        if not self._binding_matches(claims, token):
            # Filesystem work (manifest read) runs last, after every cheap
            # claim/route check has already passed.
            return self._deny(403, "token_binding_mismatch")

        logger.debug("egress allowed unit=%s route=%s", claims.unit, route)
        return self._forward(method_name, route, query, normalized, payload, deadline)

    def _deny(self, status: int, code: str) -> BrokerResponse:
        logger.debug("egress denied code=%s", code)
        return BrokerResponse.failure(status, code)

    # ------------------------------------------------------------------
    # revocation and task binding
    # ------------------------------------------------------------------
    def _is_revoked(self, claims: EgressClaims, token_hash: str) -> bool:
        """Check the revocation directory once per request.

        Marker names come from :func:`revocation_markers` (token digest, unit,
        task and grant), so the adapter can revoke at the finest or coarsest
        granularity it has.  A missing or non-directory store fails closed.
        """

        directory = self._config.revocation_dir
        try:
            info = os.lstat(directory)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BrokerError("revocation_unavailable")
            for name in revocation_markers(claims, token_hash):
                try:
                    os.lstat(_marker_path(str(directory), name))
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise BrokerError("revocation_unavailable") from exc
                return True
        except BrokerError:
            raise
        except OSError as exc:
            raise BrokerError("revocation_unavailable") from exc
        return False

    def _binding_matches(self, claims: EgressClaims, token: str) -> bool:
        """Confirm the token is the one bound to its prepared task directory.

        The helper writes ``manifest.json`` (mode 0640, group
        ``agentmgr-hermes``) with the unit/task/grant ids and the egress-token
        SHA-256.  A token whose unit has no manifest (never prepared, already
        cleaned up, or belonging to another unit) and a token whose claims were
        rewritten cannot pass.  Any read or parse failure is a mismatch.
        """

        manifest = self._read_manifest(self._config.task_root, claims.unit)
        if manifest is None:
            return False
        expected = {
            "unit": claims.unit,
            "task_id": claims.task_id,
            "grant_id": claims.grant_id,
            "egress_token_sha256": egress_token_hash(token),
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                return False
        return True

    def _read_manifest(self, task_root: Path, unit: str) -> dict[str, object] | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
        root_fd = -1
        unit_fd = -1
        manifest_fd = -1
        try:
            root_fd = os.open(str(task_root), directory_flags)
            unit_fd = os.open(unit, directory_flags, dir_fd=root_fd)
            manifest_fd = os.open("manifest.json", flags, dir_fd=unit_fd)
            info = os.fstat(manifest_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or not 0 < info.st_size <= _MAX_MANIFEST_BYTES
            ):
                return None
            raw = _read_bounded(manifest_fd, _MAX_MANIFEST_BYTES)
        except (OSError, ValueError):
            return None
        finally:
            for descriptor in (manifest_fd, unit_fd, root_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        if raw is None:
            return None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        return document

    # ------------------------------------------------------------------
    # upstream forwarding
    # ------------------------------------------------------------------
    def _target_path(self, route: str, query: str) -> str:
        target = f"{self._target.base_path}{route}"
        if query:
            target = f"{target}?{query}"
        return target

    def _connect(self, deadline: float) -> http.client.HTTPConnection:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerError("upstream_timeout")
        timeout = min(self._config.upstream_timeout_sec, remaining)
        try:
            if self._target.scheme == "https":
                return http.client.HTTPSConnection(
                    self._target.host,
                    self._target.port,
                    timeout=timeout,
                    context=ssl.create_default_context(),
                )
            return http.client.HTTPConnection(
                self._target.host, self._target.port, timeout=timeout
            )
        except (OSError, ValueError) as exc:
            raise BrokerError("upstream_unavailable") from exc

    def _arm(self, sock: socket.socket | None, deadline: float) -> bool:
        """Apply the remaining overall deadline to the upstream socket.

        Returns ``False`` when the request budget is exhausted; the caller then
        fails with the stable timeout code instead of continuing I/O.  ``sock``
        may be ``None`` for an exotic response object; the per-I/O timeout set
        at connect time still applies and the streaming loop checks the deadline
        on every iteration.
        """

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if sock is not None:
            try:
                sock.settimeout(min(self._config.upstream_timeout_sec, remaining))
            except OSError:
                return False
        return True

    def _forward(
        self,
        method: str,
        route: str,
        query: str,
        headers: Mapping[str, tuple[str, ...]],
        body: bytes,
        deadline: float,
    ) -> BrokerResponse:
        try:
            connection = self._connect(deadline)
        except BrokerError as exc:
            if exc.code == "upstream_timeout":
                return self._upstream_failure(504, "upstream_timeout")
            return self._upstream_failure(502, "upstream_unavailable")
        try:
            # Connect explicitly so the overall deadline bounds the TCP/TLS
            # handshake as well as every later read and write.
            connection.connect()
            connection.putrequest(
                method,
                self._target_path(route, query),
                skip_host=True,
                skip_accept_encoding=True,
            )
            connection.putheader("Host", self._target.host_header)
            for name in _FORWARDED_REQUEST_HEADERS:
                values = headers.get(name.lower(), ())
                if values:
                    connection.putheader(name, values[0])
            connection.putheader(
                self._config.provider_key_header,
                self._config.provider_key_prefix
                + self._config.provider_key.decode("ascii"),
            )
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders()
            if body:
                if not self._arm(connection.sock, deadline):
                    connection.close()
                    return self._upstream_failure(504, "upstream_timeout")
                connection.send(body)
            if not self._arm(connection.sock, deadline):
                connection.close()
                return self._upstream_failure(504, "upstream_timeout")
            response = connection.getresponse()
        except TimeoutError:
            connection.close()
            return self._upstream_failure(504, "upstream_timeout")
        except (OSError, http.client.HTTPException, ValueError):
            connection.close()
            return self._upstream_failure(502, "upstream_unavailable")
        try:
            return self._translate(response, connection, deadline)
        except TimeoutError:
            response.close()
            connection.close()
            return self._upstream_failure(504, "upstream_timeout")
        except (OSError, http.client.HTTPException, ValueError):
            response.close()
            connection.close()
            return self._upstream_failure(502, "upstream_error")
        except BaseException:
            response.close()
            connection.close()
            raise

    def _translate(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> BrokerResponse:
        status = response.status
        if 300 <= status < 400:
            # Never follow or expose a provider redirect: the token pins one
            # destination and a 3xx would otherwise move the key elsewhere.
            response.close()
            connection.close()
            return self._upstream_failure(502, "upstream_redirect_refused")
        if not 200 <= status < 300:
            mapped, code = _UPSTREAM_ERRORS.get(status, (502, "upstream_error"))
            extra: tuple[tuple[str, str], ...] = ()
            if mapped == 429:
                retry_after = response.getheader("Retry-After")
                if retry_after and retry_after.isdigit() and len(retry_after) <= 10:
                    extra = (("Retry-After", retry_after),)
            response.close()
            connection.close()
            return self._upstream_failure(mapped, code, headers=extra)

        out_headers: list[tuple[str, str]] = []
        for name in _FORWARDED_RESPONSE_HEADERS:
            value = response.getheader(name)
            if value:
                out_headers.append((name, value))

        declared = response.getheader("Content-Length")
        length: int | None
        if declared is None:
            length = None
        else:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length is not None and length < 0:
                length = None
            elif length is not None and length > self._config.max_response_bytes:
                response.close()
                connection.close()
                return self._upstream_failure(502, "response_too_large")

        if length is not None:
            if not self._arm(_response_socket(response), deadline):
                response.close()
                connection.close()
                return self._upstream_failure(504, "upstream_timeout")
            data = response.read(length)
            if len(data) != length:
                response.close()
                connection.close()
                return self._upstream_failure(502, "upstream_error")
            response.close()
            connection.close()
            # The handler sets exactly one Content-Length for a buffered body.
            return BrokerResponse(
                status=status, headers=tuple(out_headers), body=data
            )

        return BrokerResponse(
            status=status,
            headers=tuple(out_headers),
            body=self._stream(response, connection, deadline),
        )

    def _stream(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> Iterator[bytes]:
        """Copy an SSE/chunked body chunk by chunk under the response cap.

        The ``finally`` closes the upstream response and connection, so a client
        disconnect (the server handler closes this generator) also ends the
        upstream call instead of buffering it.
        """

        remaining = self._config.max_response_bytes
        try:
            while remaining > 0:
                if not self._arm(_response_socket(response), deadline):
                    return
                try:
                    chunk = response.read1(min(_STREAM_CHUNK_BYTES, remaining))
                except (OSError, http.client.HTTPException):
                    return
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk
        finally:
            response.close()
            connection.close()

    def _upstream_failure(
        self,
        status: int,
        code: str,
        *,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> BrokerResponse:
        logger.debug("egress upstream failure code=%s", code)
        return BrokerResponse.failure(status, code, headers=headers)

    # ------------------------------------------------------------------
    # socket lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Bind the Unix socket in a background thread (tests and ``run_broker``)."""

        with self._lock:
            if self._server is not None:
                raise BrokerError("already_started")
            server = self._bind()
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="agentmgr-hermes-egress",
                daemon=True,
            )
            try:
                thread.start()
            except BaseException:
                server.server_close()
                _safe_unlink(self._config.socket_path)
                raise
            self._server = server
            self._thread = thread

    def serve_forever(self) -> None:
        """Bind the Unix socket and serve in the calling thread."""

        with self._lock:
            if self._server is not None:
                raise BrokerError("already_started")
            server = self._bind()
            self._server = server
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            with self._lock:
                self._server = None
                self._thread = None
            server.server_close()
            _safe_unlink(self._config.socket_path)

    def stop(self) -> None:
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
            thread.join(timeout=5.0)
        _safe_unlink(self._config.socket_path)

    @property
    def serving(self) -> bool:
        """True once the Unix listener is bound, activated and accepting."""

        return self._server is not None

    @property
    def active_connections(self) -> int:
        """Connections currently being served (bounded by ``max_connections``)."""

        server = self._server
        return server.active_connections if server is not None else 0

    def _bind(self) -> "_BrokerServer":
        path = self._config.socket_path
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise BrokerError("socket_unavailable") from exc
        if info is not None:
            # A leftover socket from a previous instance is ours to replace, but
            # only after proving nothing is listening: never clobber a live
            # socket, a regular file or another user's path.
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != getattr(os, "geteuid", lambda: -1)()
            ):
                raise BrokerError("socket_path_exists")
            if _socket_is_live(path):
                raise BrokerError("socket_in_use")
            try:
                os.unlink(path)
            except OSError as exc:
                raise BrokerError("socket_unavailable") from exc
        try:
            server = _BrokerServer(str(path), self)
        except OSError as exc:
            raise BrokerError("socket_unavailable") from exc
        try:
            os.chmod(path, self._config.socket_mode)
            if self._config.socket_group is not None:
                group = grp.getgrnam(self._config.socket_group)
                os.chown(path, -1, group.gr_gid)
            info = os.lstat(path)
            if (
                not stat.S_ISSOCK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != self._config.socket_mode
            ):
                raise OSError("socket attributes did not stick")
        except (OSError, KeyError) as exc:
            server.server_close()
            _safe_unlink(path)
            raise BrokerError("socket_permissions_invalid") from exc
        return server


def _response_socket(
    response: http.client.HTTPResponse,
) -> socket.socket | None:
    """Return the socket behind a response for deadline re-arming.

    ``HTTPConnection.getresponse`` closes the connection object for a
    ``Connection: close`` response, but the response's buffered socket stays
    usable until the response is closed.
    """

    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if isinstance(sock, socket.socket) else None


def _read_bounded(descriptor: int, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(_STREAM_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _socket_is_live(path: Path) -> bool:
    """True when a process accepts connections on ``path`` (or the check fails)."""

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_BUSY_RESPONSE_TIMEOUT_SECONDS)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        # Unknown state: fail closed and leave the path untouched.
        return True
    else:
        return True
    finally:
        probe.close()


def _safe_unlink(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        return
    try:
        os.unlink(path)
    except OSError:
        pass


class _BrokerRequestHandler(http.server.BaseHTTPRequestHandler):
    """Frame one HTTP request over the accepted Unix connection."""

    protocol_version = "HTTP/1.1"
    server_version = "agentmgr-egress-broker"
    sys_version = ""
    #: ``StreamRequestHandler`` applies this to the accepted connection.
    timeout = BROKER_UPSTREAM_TIMEOUT_SECONDS

    # A sandbox log, the journal and the broker's own output must never contain
    # a request line, header or body.
    def log_message(self, _format: str, *args: object) -> None:
        return

    def log_error(self, _format: str, *args: object) -> None:
        return

    @property
    def _broker(self) -> EgressBroker:
        server = self.server
        assert isinstance(server, _BrokerServer)
        return server.broker

    def setup(self) -> None:
        # ``StreamRequestHandler.setup`` applies ``self.timeout`` to the socket;
        # a stalled client must never pin a worker thread past the tighter of the
        # per-I/O and overall request bounds.
        config = self._broker.config
        self.timeout = min(config.upstream_timeout_sec, config.request_timeout_sec)
        super().setup()

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace the stdlib HTML error page with a stable, detail-free code."""

        del message, explain
        stable = _PROTOCOL_ERROR_CODES.get(code, "request_invalid")
        if not 400 <= code <= 599:
            code = 400
        self._send(BrokerResponse.failure(code, stable))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("POST")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("GET")

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        broker = self._broker
        path = self.path
        if not isinstance(path, str) or len(path) > _MAX_PATH_CHARS:
            self._send(BrokerResponse.failure(414, "path_too_long"))
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send(BrokerResponse.failure(400, "request_invalid"))
            return
        if len(self.headers.get_all("Content-Length") or ()) > 1:
            # HTTP request smuggling guard: one authoritative body length only.
            self._send(BrokerResponse.failure(400, "request_invalid"))
            return
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            length = 0
        else:
            try:
                length = int(length_header)
            except (TypeError, ValueError):
                self._send(BrokerResponse.failure(400, "request_invalid"))
                return
        if length < 0:
            self._send(BrokerResponse.failure(400, "request_invalid"))
            return
        if length > broker.config.max_request_bytes:
            # Refuse before reading: an oversized body is never buffered.
            self._send(BrokerResponse.failure(413, "request_too_large"))
            return
        try:
            body = self.rfile.read(length) if length else b""
        except OSError:
            self._send(BrokerResponse.failure(400, "request_invalid"))
            return
        if len(body) != length:
            self._send(BrokerResponse.failure(400, "request_invalid"))
            return
        if len(self.headers.get_all(EGRESS_TOKEN_HEADER) or ()) > 1:
            self._send(BrokerResponse.failure(401, "token_invalid"))
            return
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            headers.setdefault(name, value)
        self._send(broker.handle(method, path, headers, body))

    def _send(self, response: BrokerResponse) -> None:
        self.close_connection = True
        try:
            self.send_response_only(response.status)
            for name, value in response.headers:
                self.send_header(name, value)
            if isinstance(response.body, bytes):
                self.send_header("Content-Length", str(len(response.body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD" and response.body:
                    self.wfile.write(response.body)
                self.wfile.flush()
                return
            # Unknown length (SSE/chunked): close-delimited streaming.
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in response.iter_body():
                    if self.command == "HEAD":
                        continue
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError:
                return
            finally:
                response.close()
        except OSError:
            response.close()


class _BrokerServer(socketserver.ThreadingUnixStreamServer):
    """Threaded ``AF_UNIX`` server; it has no IP listener by construction.

    Concurrency is bounded by ``BrokerConfig.max_connections``: an accepted
    connection beyond the cap receives a stable ``503 broker_busy`` and is closed
    instead of spawning another handler thread.  The service unit's ``TasksMax``
    is the second bound.
    """

    daemon_threads = True
    # Do not join handler threads in ``server_close``: a stalled upstream call
    # must not delay SIGTERM past the unit's TimeoutStopSec.  In-flight daemon
    # threads are aborted when the process exits.
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, socket_path: str, broker: EgressBroker) -> None:
        self.broker = broker
        self._active = 0
        self._active_lock = threading.Lock()
        super().__init__(socket_path, _BrokerRequestHandler)

    @property
    def active_connections(self) -> int:
        with self._active_lock:
            return self._active

    def process_request(self, request: socket.socket, client_address: object) -> None:
        with self._active_lock:
            if self._active >= self.broker.config.max_connections:
                overflow = True
            else:
                self._active += 1
                overflow = False
        if overflow:
            self._reject_busy(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: object
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()

    def _release(self) -> None:
        with self._active_lock:
            if self._active > 0:
                self._active -= 1

    def _reject_busy(self, request: socket.socket) -> None:
        # Consume the already-queued request first (bounded): closing a Unix
        # socket with unread data would reset the connection and the caller would
        # never see the stable 503.
        try:
            request.settimeout(_BUSY_DRAIN_SECONDS)
            request.recv(_STREAM_CHUNK_BYTES)
            request.setblocking(False)
            while request.recv(_STREAM_CHUNK_BYTES):
                pass
        except OSError:
            pass
        body = b'{"error":"broker_busy"}'
        payload = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        try:
            request.settimeout(_BUSY_RESPONSE_TIMEOUT_SECONDS)
            request.sendall(payload)
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def handle_error(self, request: object, client_address: object) -> None:
        # The default handler prints a traceback, which could disclose request
        # detail or filesystem paths.  Every decision is already stable-coded.
        return


# ---------------------------------------------------------------------------
# root-only configuration and process entry point
# ---------------------------------------------------------------------------

_CONFIG_REQUIRED = frozenset(
    {
        "socket_path",
        "upstream_base_url",
        "token_key_path",
        "provider_key_path",
        "revocation_dir",
        "task_root",
    }
)
_CONFIG_OPTIONAL = frozenset(
    {
        "provider_host",
        "socket_group",
        "socket_mode",
        "max_request_bytes",
        "max_response_bytes",
        "upstream_timeout_sec",
        "request_timeout_sec",
        "max_connections",
        "allow_insecure_upstream",
        "provider_key_header",
        "provider_key_prefix",
    }
)
_CONFIG_KEYS = _CONFIG_REQUIRED | _CONFIG_OPTIONAL


def _read_config_document(path: object) -> dict[str, object]:
    if not isinstance(path, Path):
        raise BrokerConfigError("config_path_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise BrokerConfigError("config_unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BrokerConfigError("config_invalid")
        if stat.S_IMODE(info.st_mode) & 0o022:
            # Group/world-writable configuration could redirect the provider key.
            raise BrokerConfigError("config_mode_invalid")
        if not 0 < info.st_size <= _MAX_CONFIG_BYTES:
            raise BrokerConfigError("config_invalid")
        raw = _read_bounded(descriptor, _MAX_CONFIG_BYTES)
    finally:
        os.close(descriptor)
    if raw is None:
        raise BrokerConfigError("config_invalid")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BrokerConfigError("config_invalid") from None
    if not isinstance(document, dict):
        raise BrokerConfigError("config_invalid")
    return document


def _config_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise BrokerConfigError(code)
    return value


def _config_path(value: object, code: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BrokerConfigError(code)
    return _require_absolute_path(Path(value), code)


def _config_bool(value: object, code: str) -> bool:
    if type(value) is not bool:
        raise BrokerConfigError(code)
    return value


def _config_int(value: object, code: str) -> int:
    if type(value) is not int:
        raise BrokerConfigError(code)
    return value


def _config_prefix(value: object) -> str:
    # An empty prefix is meaningful for a non-Authorization header (x-api-key);
    # BrokerConfig rejects it for Authorization and all whitespace abuse.
    if not isinstance(value, str):
        raise BrokerConfigError("config_provider_header_invalid")
    return value


def _config_mode(value: object) -> int:
    if isinstance(value, str):
        text = value.strip()
        if _OCTAL_RE.fullmatch(text):
            try:
                return int(text, 8)
            except ValueError:
                raise BrokerConfigError("config_socket_mode_invalid") from None
        raise BrokerConfigError("config_socket_mode_invalid")
    return _config_int(value, "config_socket_mode_invalid")


def _read_secret_file(path: Path, *, label: str) -> bytes:
    """Read one root-only secret file without following symlinks or echoing it."""

    code = f"config_{label}_invalid"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise BrokerConfigError(code) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BrokerConfigError(code)
        # Root-only or systemd's LoadCredential copy (0400, owned by the service
        # user): never group- or world-readable for a provider key.
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise BrokerConfigError(code)
        if not 0 < info.st_size <= _MAX_SECRET_BYTES + 2:
            raise BrokerConfigError(code)
        raw = _read_bounded(descriptor, _MAX_SECRET_BYTES + 2)
    finally:
        os.close(descriptor)
    if raw is None:
        raise BrokerConfigError(code)
    value = raw.rstrip(b"\r\n")
    if not value or len(value) > _MAX_SECRET_BYTES:
        raise BrokerConfigError(code)
    if b"\n" in value or b"\r" in value:
        raise BrokerConfigError(code)
    return value


def load_broker_config(path: Path) -> BrokerConfig:
    """Load and validate the root-only JSON broker configuration.

    Secrets are only ever referenced by file path (``token_key_path``,
    ``provider_key_path``); inline values, unknown keys and relative paths are
    rejected.  Errors carry stable codes and never include a secret or a path.
    """

    document = _read_config_document(path)
    unknown = set(document) - _CONFIG_KEYS
    if unknown or not _CONFIG_REQUIRED <= set(document):
        raise BrokerConfigError("config_invalid")

    allow_insecure = _config_bool(
        document.get("allow_insecure_upstream", False),
        "config_upstream_invalid",
    )
    upstream_base_url = _config_string(
        document["upstream_base_url"], "config_upstream_invalid"
    )
    target = _parse_upstream(upstream_base_url, allow_insecure=allow_insecure)

    provider_host = document.get("provider_host")
    if provider_host is None:
        provider_host = target.host_header
    else:
        provider_host = _config_string(provider_host, "config_provider_host_invalid")

    socket_group = document.get("socket_group", BROKER_SOCKET_GROUP)
    if socket_group is not None:
        socket_group = _config_string(socket_group, "config_socket_group_invalid")

    return BrokerConfig(
        socket_path=_config_path(document["socket_path"], "config_socket_path_invalid"),
        upstream_base_url=upstream_base_url,
        token_key=_read_secret_file(
            _config_path(document["token_key_path"], "config_token_key_invalid"),
            label="token_key",
        ),
        provider_key=_read_secret_file(
            _config_path(
                document["provider_key_path"], "config_provider_key_invalid"
            ),
            label="provider_key",
        ),
        revocation_dir=_config_path(
            document["revocation_dir"], "config_revocation_dir_invalid"
        ),
        task_root=_config_path(document["task_root"], "config_task_root_invalid"),
        provider_host=provider_host,
        max_request_bytes=_config_int(
            document.get("max_request_bytes", BROKER_MAX_REQUEST_BYTES),
            "config_request_limit_invalid",
        ),
        max_response_bytes=_config_int(
            document.get("max_response_bytes", BROKER_MAX_RESPONSE_BYTES),
            "config_response_limit_invalid",
        ),
        upstream_timeout_sec=document.get(
            "upstream_timeout_sec", BROKER_UPSTREAM_TIMEOUT_SECONDS
        ),
        request_timeout_sec=document.get(
            "request_timeout_sec", BROKER_REQUEST_TIMEOUT_SECONDS
        ),
        max_connections=_config_int(
            document.get("max_connections", BROKER_MAX_CONNECTIONS),
            "config_connections_invalid",
        ),
        allow_insecure_upstream=allow_insecure,
        socket_group=socket_group,
        socket_mode=_config_mode(document.get("socket_mode", BROKER_SOCKET_MODE)),
        provider_key_header=_config_string(
            document.get("provider_key_header", "Authorization"),
            "config_provider_header_invalid",
        ),
        provider_key_prefix=_config_prefix(
            document.get("provider_key_prefix", "Bearer ")
        ),
    )


def _require_directory(path: Path, code: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise BrokerError(code) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BrokerError(code)


def _install_stop_handlers(stop_event: threading.Event) -> dict[int, object]:
    previous: dict[int, object] = {}

    def _handler(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, _handler)
        except (ValueError, OSError):
            # Not the main thread (embedded use); the caller owns the lifecycle.
            break
    return previous


def _restore_stop_handlers(previous: Mapping[int, object]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)  # type: ignore[arg-type]
        except (ValueError, OSError):
            pass


def _require_control_socket_path(path: Path) -> None:
    """Reject a socket in a volatile/masked filesystem (design §9.1).

    ``/run`` is replaced/masked inside task units and ``/tmp``/``/var/tmp`` are
    private or temporary, so a socket there cannot be a stable host-side bind
    source.  The service unit uses ``StateDirectory=agentmgr-hermes-control``.
    """

    for root in VOLATILE_SOCKET_ROOTS:
        if path == root or root in path.parents:
            raise BrokerError("socket_path_volatile")


def run_broker(config: BrokerConfig) -> int:
    """Validate the deployment, serve until SIGTERM/SIGINT and return an exit code."""

    if not isinstance(config, BrokerConfig):
        logger.error("egress broker rejected: config_invalid")
        return EXIT_CONFIG
    try:
        _require_directory(config.revocation_dir, "revocation_unavailable")
        _require_directory(config.task_root, "task_root_unavailable")
        _require_control_socket_path(config.socket_path)
    except BrokerError as exc:
        logger.error("egress broker unavailable: %s", exc.code)
        return EXIT_CONFIG if exc.code == "socket_path_volatile" else EXIT_UNAVAILABLE

    broker = EgressBroker(config)
    stop_event = threading.Event()
    previous = _install_stop_handlers(stop_event)
    try:
        broker.start()
    except BrokerError as exc:
        logger.error("egress broker failed: %s", exc.code)
        _restore_stop_handlers(previous)
        return EXIT_SOCKET
    logger.info("hermes egress broker ready")
    try:
        stop_event.wait()
    finally:
        broker.stop()
        _restore_stop_handlers(previous)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """``python -m app.runtime.hermes_egress --config <root-only-config>``."""

    parser = argparse.ArgumentParser(
        prog="python -m app.runtime.hermes_egress",
        description="AgentMgr Hermes host egress broker (Unix socket only).",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="root-only JSON broker configuration",
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_broker_config(Path(arguments.config))
    except BrokerConfigError as exc:
        logger.error("egress broker config rejected: %s", exc.code)
        return EXIT_CONFIG
    return run_broker(config)


if __name__ == "__main__":
    raise SystemExit(main())
