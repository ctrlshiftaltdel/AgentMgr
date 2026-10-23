"""Task-scoped ``agx1`` credentials, JCS canonicalisation and revocation.

The credential layer is the only place that signs or verifies a token.  It is
deliberately independent of the database and of the business modules: the
canonicaliser is a stdlib-only RFC 8785 subset, so the security unit tests stay
importable without SQLAlchemy.

Token format (design §9.2)::

    agx1.<base64url(JCS(claims))>.<base64url(HMAC-SHA256(key, b"agx1."+payload))>

base64url is unpadded.  Verification recomputes the MAC over the *encoded*
payload segment and compares with :func:`hmac.compare_digest`; it never
re-serialises the parsed object.

Safety rules
------------

* The token, its payload segment and any key material never appear in
  ``__repr__``, exception text or error details.  Every failure is a
  :class:`SecurityError` with a stable code and a short ``[a-z0-9_]`` detail.
* Key material comes only from an explicitly injected keyring or from the two
  ``AGENTMGR_EXEC_CREDENTIAL_*`` environment variables loaded by
  :func:`load_keyring_from_env`; there is no built-in default.  A missing,
  short, undecodable or unknown-active key fails closed.
* Claim keys are an exact set.  Unknown or missing keys, wrong JSON types,
  unsorted capabilities and out-of-range times are rejected.
* :meth:`CredentialValidator.validate_call` re-reads the Grant projection passed
  by the caller (the caller is responsible for it being live) and checks task,
  grant and subject identity, Grant status/usability/expiry, execution mode,
  capability and resource scope including ``exclude_resource_ids``.
* The filesystem revocation store writes one ``0600`` record per ``jti`` under a
  ``0700`` directory using a temp file, ``fsync`` and ``os.replace``; a partial
  ``*.tmp`` file is never read and is removed by :meth:`prune`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.security.boundary import (
    EXECUTION_MODE_MANAGED,
    MAX_CAPABILITIES,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    GrantStatus,
    ResourceScope,
)
from app.security.errors import SecurityError

__all__ = [
    "ACTIVE_KID_ENV",
    "KEYRING_ENV",
    "MAX_SAFE_INTEGER",
    "MAX_TOKEN_BYTES",
    "MIN_KEY_BYTES",
    "CredentialClaims",
    "CredentialIssuer",
    "CredentialValidator",
    "FileRevocationStore",
    "InMemoryRevocationStore",
    "IssuedCredential",
    "RevocationRecord",
    "RevocationStore",
    "canonicalize_claims",
    "load_keyring_from_env",
]

#: Token prefix; also the MAC domain separator.
TOKEN_PREFIX = "agx1"
_TOKEN_PREFIX_BYTES = b"agx1."
MAX_TOKEN_BYTES = 4096

#: RFC 8785: integers are exact only up to 2**53-1.
MAX_SAFE_INTEGER = 2**53 - 1

#: Each HMAC key must be at least 256 bit.
MIN_KEY_BYTES = 32

#: Allowed `nbf` clock skew in the past (design §9.1).
CLOCK_SKEW_SECONDS = 5

#: Fixed claim values (design §9.1).
CLAIM_VERSION = 1
CLAIM_ISSUER = "agentmgr-exec"
CLAIM_AUDIENCE = "pep-gateway"

KEYRING_ENV = "AGENTMGR_EXEC_CREDENTIAL_KEYRING"
ACTIVE_KID_ENV = "AGENTMGR_EXEC_CREDENTIAL_ACTIVE_KID"

#: Revocation records outlive the maximum credential lifetime twice over.
_RETENTION_SECONDS = 2 * MAX_TTL_SECONDS

_CLAIM_FIELDS = frozenset(
    {
        "v",
        "iss",
        "aud",
        "kid",
        "jti",
        "task_id",
        "grant_id",
        "subject_id",
        "capabilities",
        "resource_scope",
        "iat",
        "nbf",
        "exp",
        "nonce",
    }
)
_INT_CLAIM_FIELDS = ("v", "iat", "nbf", "exp")
_STR_CLAIM_FIELDS = (
    "iss",
    "aud",
    "kid",
    "jti",
    "task_id",
    "grant_id",
    "subject_id",
    "nonce",
)

_KID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,15}$")
_JTI_PATTERN = re.compile(r"^cred_[0-9a-f]{32}$")
_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TASK_ID_PATTERN = re.compile(r"^task_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_GRANT_ID_PATTERN = re.compile(r"^grnt_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_SUBJECT_PATTERN = re.compile(r"^(prin|svc|rt)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*@\d+\.\d+\.\d+$")
_RESOURCE_ID_PATTERN = re.compile(r"^res_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_B64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_RECORD_SUFFIX = ".json"
_TMP_SUFFIX = ".tmp"
_RECORD_FIELDS = frozenset({"jti", "reason", "revoked_at"})

#: Serialises writers/readers of one filesystem store within this process.
_FILE_STORE_LOCK = threading.Lock()


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _aware(moment: object, name: str) -> datetime:
    """Return a timezone-aware datetime or raise a plain programmer error."""

    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return moment


def _epoch_seconds(moment: datetime) -> int:
    return int(moment.timestamp())


def _resolve_now(
    clock: Callable[[], datetime] | None, now: datetime | None
) -> datetime:
    if now is None:
        if clock is None:
            raise ValueError("now is required when no clock was supplied")
        now = clock()
    return _aware(now, "now")


def _utf16_sort_key(key: str) -> bytes:
    """RFC 8785 sorts object keys by their UTF-16 code units."""

    return key.encode("utf-16-be")


def _encode_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SecurityError("invalid_claim", "claim_string_invalid") from exc
    return json.dumps(value, ensure_ascii=False)


def _encode_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, bool):
        # Nothing in the claim schema is boolean; refuse the int subclass.
        raise SecurityError("invalid_claim", "claim_type")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise SecurityError("invalid_claim", "integer_out_of_range")
        return str(value)
    if isinstance(value, float):
        raise SecurityError("invalid_claim", "float_not_allowed")
    if isinstance(value, Mapping):
        keys = list(value)
        for key in keys:
            if not isinstance(key, str):
                raise SecurityError("invalid_claim", "claim_key_invalid")
            _encode_string(key)
        rendered = []
        for key in sorted(keys, key=_utf16_sort_key):
            rendered.append(_encode_string(key) + ":" + _encode_value(value[key]))
        return "{" + ",".join(rendered) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_value(item) for item in value) + "]"
    raise SecurityError("invalid_claim", "claim_type")


def canonicalize_claims(value: Mapping[str, object]) -> bytes:
    """Serialise claims as the RFC 8785 subset used by ``agx1`` tokens.

    Object keys must be strings and are sorted by UTF-16 code units; output is
    compact (``,``/``:`` separators), keeps non-ASCII characters as UTF-8 and
    contains integers only.  Floats, NaN/Infinity, integers outside
    ``±(2**53-1)`` and non-JSON types are rejected with
    :class:`SecurityError("invalid_claim")`.
    """

    if not isinstance(value, Mapping):
        raise SecurityError("invalid_claim", "claim_type")
    return _encode_value(value).encode("utf-8")


def _normalise_capabilities(capabilities: object) -> tuple[str, ...]:
    if isinstance(capabilities, (str, bytes)) or not isinstance(
        capabilities, (list, tuple, set, frozenset)
    ):
        raise SecurityError("invalid_claim", "capability_invalid")
    items = tuple(capabilities)
    if not items:
        raise SecurityError("invalid_claim", "capability_empty")
    for item in items:
        if not _matches(item, _CAPABILITY_PATTERN):
            raise SecurityError("invalid_claim", "capability_invalid")
    unique = tuple(sorted(set(items)))
    if len(unique) > MAX_CAPABILITIES:
        raise SecurityError("invalid_claim", "capability_too_many")
    return unique


@dataclass(frozen=True, slots=True)
class CredentialClaims:
    """The exact ``agx1`` claim set (design §9.1).

    Construction validates every field, so a malformed claims object cannot be
    signed.  Parsed tokens are rebuilt through the same constructor; the
    validator turns any rejection into ``token_invalid``.
    """

    v: int
    iss: str
    aud: str
    kid: str
    jti: str
    task_id: str
    grant_id: str
    subject_id: str
    capabilities: tuple[str, ...]
    resource_scope: ResourceScope
    iat: int
    nbf: int
    exp: int
    nonce: str

    def __post_init__(self) -> None:
        if type(self.v) is not int or self.v != CLAIM_VERSION:
            raise SecurityError("invalid_claim", "v_invalid")
        if self.iss != CLAIM_ISSUER:
            raise SecurityError("invalid_claim", "iss_invalid")
        if self.aud != CLAIM_AUDIENCE:
            raise SecurityError("invalid_claim", "aud_invalid")
        if not _matches(self.kid, _KID_PATTERN):
            raise SecurityError("invalid_claim", "kid_invalid")
        if not _matches(self.jti, _JTI_PATTERN):
            raise SecurityError("invalid_claim", "jti_invalid")
        if not _matches(self.task_id, _TASK_ID_PATTERN):
            raise SecurityError("invalid_claim", "task_id_invalid")
        if not _matches(self.grant_id, _GRANT_ID_PATTERN):
            raise SecurityError("invalid_claim", "grant_id_invalid")
        if not _matches(self.subject_id, _SUBJECT_PATTERN):
            raise SecurityError("invalid_claim", "subject_id_invalid")
        if not _matches(self.nonce, _NONCE_PATTERN):
            raise SecurityError("invalid_claim", "nonce_invalid")

        if not isinstance(self.capabilities, tuple):
            raise SecurityError("invalid_claim", "capability_invalid")
        if not self.capabilities:
            raise SecurityError("invalid_claim", "capability_empty")
        if len(self.capabilities) > MAX_CAPABILITIES:
            raise SecurityError("invalid_claim", "capability_too_many")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise SecurityError("invalid_claim", "capability_duplicate")
        if tuple(sorted(self.capabilities)) != self.capabilities:
            raise SecurityError("invalid_claim", "capability_unsorted")
        for capability in self.capabilities:
            if not _matches(capability, _CAPABILITY_PATTERN):
                raise SecurityError("invalid_claim", "capability_invalid")

        if not isinstance(self.resource_scope, ResourceScope):
            raise SecurityError("invalid_claim", "resource_scope_invalid")

        for name in ("iat", "nbf", "exp"):
            if type(getattr(self, name)) is not int:
                raise SecurityError("invalid_claim", "claim_type")
        if self.nbf != self.iat - CLOCK_SKEW_SECONDS:
            raise SecurityError("invalid_claim", "nbf_invalid")
        if not MIN_TTL_SECONDS <= self.exp - self.iat <= MAX_TTL_SECONDS:
            raise SecurityError("invalid_claim", "exp_range_invalid")

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready projection in the design's field order."""

        return {
            "v": self.v,
            "iss": self.iss,
            "aud": self.aud,
            "kid": self.kid,
            "jti": self.jti,
            "task_id": self.task_id,
            "grant_id": self.grant_id,
            "subject_id": self.subject_id,
            "capabilities": list(self.capabilities),
            "resource_scope": self.resource_scope.as_dict(),
            "iat": self.iat,
            "nbf": self.nbf,
            "exp": self.exp,
            "nonce": self.nonce,
        }


def _claims_from_payload(payload: Mapping[str, object]) -> CredentialClaims:
    """Rebuild claims from parsed JSON, rejecting anything off-schema."""

    if not isinstance(payload, Mapping):
        raise SecurityError("invalid_claim", "claim_type")
    if set(payload) != _CLAIM_FIELDS:
        raise SecurityError("invalid_claim", "claim_key_set")
    for name in _INT_CLAIM_FIELDS:
        if type(payload[name]) is not int:
            raise SecurityError("invalid_claim", "claim_type")
    for name in _STR_CLAIM_FIELDS:
        if not isinstance(payload[name], str):
            raise SecurityError("invalid_claim", "claim_type")

    capabilities = payload["capabilities"]
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        raise SecurityError("invalid_claim", "capability_invalid")

    scope = payload["resource_scope"]
    if not isinstance(scope, Mapping):
        raise SecurityError("invalid_claim", "resource_scope_invalid")

    return CredentialClaims(
        v=payload["v"],
        iss=payload["iss"],
        aud=payload["aud"],
        kid=payload["kid"],
        jti=payload["jti"],
        task_id=payload["task_id"],
        grant_id=payload["grant_id"],
        subject_id=payload["subject_id"],
        capabilities=tuple(capabilities),
        resource_scope=ResourceScope.from_mapping(scope),
        iat=payload["iat"],
        nbf=payload["nbf"],
        exp=payload["exp"],
        nonce=payload["nonce"],
    )


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """A signed token and the claims it carries.

    ``token`` is excluded from ``repr`` so accidental logging of the record does
    not disclose the bearer secret.
    """

    token: str = field(repr=False)
    claims: CredentialClaims


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    if _B64URL_PATTERN.fullmatch(segment) is None:
        raise ValueError("not base64url")
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64url") from exc


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError("non-finite JSON number")


def _validated_keyring(
    keyring: Mapping[str, bytes], active_kid: str | None = None
) -> dict[str, bytes]:
    """Return a private copy of the keyring, failing closed on any defect."""

    if not isinstance(keyring, Mapping) or not keyring:
        raise SecurityError("credential_key_invalid", "keyring_invalid")
    validated: dict[str, bytes] = {}
    for kid, value in keyring.items():
        if not _matches(kid, _KID_PATTERN):
            raise SecurityError("credential_key_invalid", "kid_invalid")
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise SecurityError("credential_key_invalid", "key_type")
        key = bytes(value)
        if len(key) < MIN_KEY_BYTES:
            raise SecurityError("credential_key_invalid", "key_short")
        validated[kid] = key
    if active_kid is not None and active_kid not in validated:
        raise SecurityError("credential_key_invalid", "active_kid_unknown")
    return validated


def _sign_claims(claims: CredentialClaims, key: bytes) -> str:
    payload_segment = _b64url_encode(canonicalize_claims(claims.as_dict()))
    mac = hmac.new(
        key, _TOKEN_PREFIX_BYTES + payload_segment.encode("ascii"), hashlib.sha256
    ).digest()
    token = f"{TOKEN_PREFIX}.{payload_segment}.{_b64url_encode(mac)}"
    if len(token) > MAX_TOKEN_BYTES:
        raise SecurityError("invalid_claim", "token_too_large")
    return token


class CredentialIssuer:
    """Signs short-lived, task-bound ``agx1`` credentials."""

    def __init__(
        self,
        keyring: Mapping[str, bytes],
        active_kid: str,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._keyring = _validated_keyring(keyring, active_kid)
        self._active_kid = active_kid
        self._clock = clock

    def issue(
        self,
        *,
        task_id: str,
        grant_id: str,
        subject_id: str,
        capabilities: Sequence[str] | set[str] | frozenset[str],
        resource_scope: ResourceScope,
        ttl_seconds: int,
    ) -> IssuedCredential:
        """Issue a token whose ``exp - iat`` is ``ttl_seconds`` (1..300)."""

        if type(ttl_seconds) is not int or not (
            MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
        ):
            raise SecurityError("invalid_claim", "ttl_seconds_out_of_range")
        if not isinstance(resource_scope, ResourceScope):
            raise SecurityError("invalid_claim", "resource_scope_invalid")

        now = _aware(self._clock(), "clock")
        iat = _epoch_seconds(now)
        claims = CredentialClaims(
            v=CLAIM_VERSION,
            iss=CLAIM_ISSUER,
            aud=CLAIM_AUDIENCE,
            kid=self._active_kid,
            jti="cred_" + secrets.token_hex(16),
            task_id=task_id,
            grant_id=grant_id,
            subject_id=subject_id,
            capabilities=_normalise_capabilities(capabilities),
            resource_scope=resource_scope,
            iat=iat,
            nbf=iat - CLOCK_SKEW_SECONDS,
            exp=iat + ttl_seconds,
            nonce=secrets.token_hex(16),
        )
        return IssuedCredential(
            token=_sign_claims(claims, self._keyring[self._active_kid]),
            claims=claims,
        )


class CredentialValidator:
    """Verifies ``agx1`` tokens and authorises one call against a live Grant."""

    def __init__(
        self, keyring: Mapping[str, bytes], clock: Callable[[], datetime]
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._keyring = _validated_keyring(keyring)
        self._clock = clock

    def validate(
        self,
        token: str,
        *,
        expected_task_id: str | None = None,
        expected_grant_id: str | None = None,
    ) -> CredentialClaims:
        """Return the verified claims or raise a stable :class:`SecurityError`."""

        claims = self._parse_token(token)
        now_seconds = _epoch_seconds(_aware(self._clock(), "clock"))
        if now_seconds >= claims.exp:
            raise SecurityError("credential_expired", "exp_passed")
        if now_seconds < claims.nbf:
            raise SecurityError("token_not_yet_valid", "nbf_not_reached")
        if expected_task_id is not None and claims.task_id != expected_task_id:
            raise SecurityError("cross_task_denied", "task_id_mismatch")
        if expected_grant_id is not None and claims.grant_id != expected_grant_id:
            raise SecurityError("cross_task_denied", "grant_id_mismatch")
        return claims

    def validate_call(
        self,
        claims: CredentialClaims,
        *,
        grant_status: GrantStatus,
        capability: str,
        resource: str,
    ) -> None:
        """Authorise one gateway call; raise on any failed check.

        The caller supplies the *live* :class:`GrantStatus` projection (no
        caching, design §9.6).  Checks run in the gateway's documented order:
        Grant status/usability/expiry, execution mode, identity, capability and
        finally resource scope including ``exclude_resource_ids``.
        """

        if not isinstance(claims, CredentialClaims):
            raise ValueError("claims must be CredentialClaims")
        if not isinstance(grant_status, GrantStatus):
            raise ValueError("grant_status must be GrantStatus")

        if grant_status.status != "active":
            raise SecurityError("grant_revoked", "grant_not_active")
        if not grant_status.usable:
            raise SecurityError("grant_revoked", "grant_unusable")

        now = _aware(self._clock(), "clock")
        if _epoch_seconds(now) >= _epoch_seconds(grant_status.expires_at):
            raise SecurityError("grant_expired", "grant_expiry_passed")
        if grant_status.execution_mode != EXECUTION_MODE_MANAGED:
            raise SecurityError(
                "execution_mode_mismatch", "managed_execution_required"
            )

        if claims.task_id != grant_status.task_id:
            raise SecurityError("cross_task_denied", "task_id_mismatch")
        if claims.grant_id != grant_status.grant_id:
            raise SecurityError("cross_task_denied", "grant_id_mismatch")
        if claims.subject_id != grant_status.subject_id:
            raise SecurityError("cross_task_denied", "subject_id_mismatch")

        if not _matches(capability, _CAPABILITY_PATTERN):
            raise SecurityError("capability_not_granted", "capability_invalid")
        if capability not in claims.capabilities:
            raise SecurityError(
                "capability_not_granted", "capability_not_in_credential"
            )
        if capability not in grant_status.capabilities:
            raise SecurityError("capability_not_granted", "capability_not_in_grant")

        if not _matches(resource, _RESOURCE_ID_PATTERN):
            raise SecurityError("resource_scope_denied", "resource_invalid")
        scope = claims.resource_scope
        if resource not in scope.resource_ids:
            raise SecurityError("resource_scope_denied", "resource_out_of_scope")
        if resource in scope.exclude_resource_ids:
            raise SecurityError("resource_scope_denied", "resource_excluded")
        grant_scope = grant_status.resource_scope
        if resource not in grant_scope.resource_ids:
            raise SecurityError(
                "resource_scope_denied", "resource_not_in_grant_scope"
            )
        if resource in grant_scope.exclude_resource_ids:
            raise SecurityError("resource_scope_denied", "resource_excluded")

    def _parse_token(self, token: str) -> CredentialClaims:
        if not isinstance(token, str) or not token:
            raise SecurityError("token_invalid", "token_type")
        try:
            encoded = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SecurityError("token_invalid", "token_encoding") from exc
        if len(encoded) > MAX_TOKEN_BYTES:
            raise SecurityError("token_invalid", "token_too_large")

        parts = token.split(".")
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1] or not parts[2]:
            raise SecurityError("token_invalid", "token_structure")

        try:
            payload_bytes = _b64url_decode(parts[1])
        except ValueError as exc:
            raise SecurityError("token_invalid", "payload_encoding") from exc

        try:
            payload = json.loads(
                payload_bytes.decode("utf-8"),
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise SecurityError("token_invalid", "payload_invalid") from exc
        if not isinstance(payload, Mapping):
            raise SecurityError("token_invalid", "payload_invalid")

        # `kid` is read from the unverified payload only to select a key; the
        # MAC still covers the whole encoded segment.
        kid = payload.get("kid")
        if not isinstance(kid, str) or _KID_PATTERN.fullmatch(kid) is None:
            raise SecurityError("token_invalid", "kid_invalid")
        key = self._keyring.get(kid)
        if key is None:
            raise SecurityError("token_invalid", "kid_unknown")

        try:
            supplied_mac = _b64url_decode(parts[2])
        except ValueError as exc:
            raise SecurityError("token_invalid", "mac_encoding") from exc

        expected_mac = hmac.new(
            key, _TOKEN_PREFIX_BYTES + parts[1].encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected_mac, supplied_mac):
            raise SecurityError("token_invalid", "mac_mismatch")

        try:
            return _claims_from_payload(payload)
        except SecurityError as exc:
            raise SecurityError("token_invalid", "claim_invalid") from exc


@dataclass(frozen=True, slots=True)
class RevocationRecord:
    """One revoked credential ``jti``; ``revoke`` keeps the first record."""

    jti: str
    reason: str
    revoked_at: datetime

    def __post_init__(self) -> None:
        if not _matches(self.jti, _JTI_PATTERN):
            raise SecurityError("invalid_claim", "jti_invalid")
        if not _matches(self.reason, _REASON_PATTERN):
            raise SecurityError("invalid_claim", "reason_invalid")
        _aware(self.revoked_at, "revoked_at")

    def as_dict(self) -> dict[str, str]:
        return {
            "jti": self.jti,
            "reason": self.reason,
            "revoked_at": self.revoked_at.isoformat(),
        }


class RevocationStore(Protocol):
    """Revocation lookup used by the gateway on every call (design §9.5)."""

    def revoke(
        self, jti: str, *, reason: str, revoked_at: datetime
    ) -> None: ...

    def lookup(self, jti: str) -> RevocationRecord | None: ...

    def prune(self, now: datetime | None = None) -> int: ...


class InMemoryRevocationStore:
    """Same-process store; records are lost on restart (design §9.5)."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock
        self._records: dict[str, RevocationRecord] = {}
        self._lock = threading.Lock()

    def revoke(self, jti: str, *, reason: str, revoked_at: datetime) -> None:
        record = RevocationRecord(jti=jti, reason=reason, revoked_at=revoked_at)
        with self._lock:
            self._records.setdefault(record.jti, record)

    def lookup(self, jti: str) -> RevocationRecord | None:
        if not _matches(jti, _JTI_PATTERN):
            raise SecurityError("invalid_claim", "jti_invalid")
        with self._lock:
            return self._records.get(jti)

    def prune(self, now: datetime | None = None) -> int:
        moment = _resolve_now(self._clock, now)
        with self._lock:
            expired = [
                jti
                for jti, record in self._records.items()
                if record.revoked_at + timedelta(seconds=_RETENTION_SECONDS) <= moment
            ]
            for jti in expired:
                del self._records[jti]
        return len(expired)


def _ensure_private_directory(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir():
            raise SecurityError("unknown", "revocation_root_invalid")
        # Tighten only; never widen the owner-only directory mode.
        os.chmod(root, 0o700)
    except SecurityError:
        raise
    except OSError as exc:
        raise SecurityError("unknown", "revocation_root_invalid") from exc


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _record_from_bytes(raw: bytes, *, expected_jti: str) -> RevocationRecord:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise SecurityError("unknown", "revocation_store_corrupt") from exc
    if not isinstance(value, Mapping) or set(value) != _RECORD_FIELDS:
        raise SecurityError("unknown", "revocation_store_corrupt")
    jti = value["jti"]
    reason = value["reason"]
    revoked_at = value["revoked_at"]
    if not isinstance(jti, str) or not isinstance(reason, str) or not isinstance(
        revoked_at, str
    ):
        raise SecurityError("unknown", "revocation_store_corrupt")
    try:
        record = RevocationRecord(
            jti=jti,
            reason=reason,
            revoked_at=datetime.fromisoformat(revoked_at),
        )
    except (SecurityError, ValueError) as exc:
        raise SecurityError("unknown", "revocation_store_corrupt") from exc
    if record.jti != expected_jti:
        raise SecurityError("unknown", "revocation_store_corrupt")
    return record


class FileRevocationStore:
    """Atomic, cross-process revocation store (design §9.5).

    Each record lives at ``<root>/<jti>.json`` (mode ``0600``) under a ``0700``
    directory.  Writes go to an exclusive ``*.tmp`` file, are ``fsync``-ed, then
    ``os.replace``-d into place; the directory is ``fsync``-ed afterwards.
    Readers never look at ``*.tmp``.  A corrupt record fails closed with
    ``unknown`` rather than being treated as "not revoked".
    """

    def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._root = Path(root)
        self._clock = clock
        _ensure_private_directory(self._root)

    def revoke(self, jti: str, *, reason: str, revoked_at: datetime) -> None:
        record = RevocationRecord(jti=jti, reason=reason, revoked_at=revoked_at)
        with _FILE_STORE_LOCK:
            if self._read(record.jti) is not None:
                # Idempotent: keep the first reason and timestamp.
                return
            self._write(record)

    def lookup(self, jti: str) -> RevocationRecord | None:
        if not _matches(jti, _JTI_PATTERN):
            raise SecurityError("invalid_claim", "jti_invalid")
        return self._read(jti)

    def prune(self, now: datetime | None = None) -> int:
        moment = _resolve_now(self._clock, now)
        removed = 0
        with _FILE_STORE_LOCK:
            try:
                entries = sorted(self._root.iterdir())
            except OSError as exc:
                raise SecurityError(
                    "unknown", "revocation_store_unreadable"
                ) from exc
            for entry in entries:
                if entry.name.endswith(_TMP_SUFFIX):
                    # A half-written record is never read; clean it up.
                    _unlink_quietly(entry)
                    continue
                if not entry.name.endswith(_RECORD_SUFFIX):
                    continue
                jti = entry.name[: -len(_RECORD_SUFFIX)]
                if _JTI_PATTERN.fullmatch(jti) is None:
                    continue
                record = self._read(jti)
                if record is None:
                    continue
                if (
                    record.revoked_at + timedelta(seconds=_RETENTION_SECONDS)
                    <= moment
                ):
                    try:
                        entry.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise SecurityError(
                            "unknown", "revocation_store_unwritable"
                        ) from exc
                    removed += 1
        return removed

    def _read(self, jti: str) -> RevocationRecord | None:
        path = self._root / f"{jti}{_RECORD_SUFFIX}"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecurityError("unknown", "revocation_store_unreadable") from exc
        return _record_from_bytes(raw, expected_jti=jti)

    def _write(self, record: RevocationRecord) -> None:
        final = self._root / f"{record.jti}{_RECORD_SUFFIX}"
        tmp = self._root / (
            f"{record.jti}.{os.getpid()}.{secrets.token_hex(8)}{_TMP_SUFFIX}"
        )
        payload = (
            json.dumps(
                record.as_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )

        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise SecurityError("unknown", "revocation_store_unwritable") from exc
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            _unlink_quietly(tmp)
            raise SecurityError("unknown", "revocation_store_unwritable") from exc
        finally:
            if fd != -1:
                os.close(fd)

        try:
            os.replace(tmp, final)
        except OSError as exc:
            _unlink_quietly(tmp)
            raise SecurityError("unknown", "revocation_store_unwritable") from exc

        try:
            dir_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise SecurityError("unknown", "revocation_store_unwritable") from exc


def load_keyring_from_env(
    env: Mapping[str, str],
) -> tuple[Mapping[str, bytes], str]:
    """Load ``AGENTMGR_EXEC_CREDENTIAL_KEYRING``/``_ACTIVE_KID`` fail-closed.

    The value is a JSON object of ``{"kid": "<standard base64>"}``.  Every
    decoded key must be at least 32 bytes and the active kid must name one of
    them; otherwise :class:`SecurityError("credential_key_invalid")` is raised
    and the caller must not start.
    """

    if not isinstance(env, Mapping):
        raise ValueError("env must be a mapping")

    raw = env.get(KEYRING_ENV)
    if not isinstance(raw, str) or not raw.strip():
        raise SecurityError("credential_key_invalid", "keyring_missing")
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except ValueError as exc:
        raise SecurityError("credential_key_invalid", "keyring_invalid") from exc
    if not isinstance(parsed, Mapping) or not parsed:
        raise SecurityError("credential_key_invalid", "keyring_invalid")

    decoded: dict[str, bytes] = {}
    for kid, encoded in parsed.items():
        if not isinstance(encoded, str):
            raise SecurityError("credential_key_invalid", "key_invalid")
        try:
            decoded[kid] = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SecurityError("credential_key_invalid", "key_invalid") from exc

    active_kid = env.get(ACTIVE_KID_ENV)
    if not isinstance(active_kid, str) or not active_kid:
        raise SecurityError("credential_key_invalid", "active_kid_missing")

    return _validated_keyring(decoded, active_kid), active_kid
