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

This module deliberately contains **no** broker server, socket handling, HTTP or
filesystem access.  The broker (Task 4) and the managed adapter (Task 5) build on
these pure helpers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "MAX_EGRESS_TOKEN_LENGTH",
    "MAX_EGRESS_TTL_SECONDS",
    "TOKEN_PREFIX",
    "EgressClaims",
    "EgressTokenError",
    "egress_token_hash",
    "mint_egress_token",
    "verify_egress_token",
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
        or ".." in value
        or "?" in value
        or "#" in value
    ):
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
