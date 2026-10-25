"""Contract tests for task-scoped model egress tokens.

Pure tests: no broker, no sockets, no filesystem.  They pin the token wire
format, the HMAC path, claim validation and the "never echo the raw token in an
error" rule from ``docs/security/hermes-managed-execution-design.md`` §9.2.

The token is ``egt.<base64url(canonical payload JSON)>.<base64url(hmac-sha256)>``
with no base64 padding.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.runtime import hermes_egress
from app.runtime.hermes_egress import (
    MAX_EGRESS_TOKEN_LENGTH,
    MAX_EGRESS_TTL_SECONDS,
    EgressClaims,
    EgressTokenError,
    egress_token_hash,
    mint_egress_token,
    verify_egress_token,
)

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
KEY = b"k" * 32
OTHER_KEY = b"z" * 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def claims(**overrides: object) -> EgressClaims:
    values: dict[str, object] = {
        "version": 1,
        "task_id": "task_1",
        "grant_id": "grnt_1",
        "unit": "agentmgr-hermes-abc",
        "model": "model-x",
        "provider_host": "provider.example",
        "path_prefix": "/v1",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
        "nonce": "n" * 32,
    }
    values.update(overrides)
    return EgressClaims(**values)  # type: ignore[arg-type]


def payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "v": 1,
        "task_id": "task_1",
        "grant_id": "grnt_1",
        "unit": "agentmgr-hermes-abc",
        "model": "model-x",
        "provider_host": "provider.example",
        "path_prefix": "/v1",
        "issued_at": _iso_utc(NOW),
        "expires_at": _iso_utc(NOW + timedelta(seconds=60)),
        "nonce": "n" * 32,
    }
    values.update(overrides)
    return values


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _mint_raw(document: dict[str, object], *, key: bytes = KEY) -> str:
    raw = _canonical(document)
    signature = hmac.new(key, raw, hashlib.sha256).digest()
    return f"egt.{_b64(raw)}.{_b64(signature)}"


def _decode_payload(token: str) -> dict[str, object]:
    segment = token.split(".")[1]
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    return json.loads(raw)


def test_egress_token_roundtrip_preserves_task_binding():
    original = claims()
    token = mint_egress_token(original, key=KEY, now=NOW)

    assert verify_egress_token(token, key=KEY, now=NOW) == original


def test_egress_token_wire_format_is_prefixed_and_canonical():
    token = mint_egress_token(claims(), key=KEY, now=NOW)

    assert token.startswith("egt.")
    parts = token.split(".")
    assert len(parts) == 3
    assert parts[0] == "egt"
    assert all("=" not in part for part in parts)
    assert _decode_payload(token) == payload()
    # The payload segment must be the canonical JSON, not merely equivalent.
    decoded = _decode_payload(token)
    raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    assert raw == _canonical(decoded)


def test_egress_token_accepts_maximum_ttl():
    original = claims(expires_at=NOW + timedelta(seconds=MAX_EGRESS_TTL_SECONDS))
    token = mint_egress_token(original, key=KEY, now=NOW)

    assert verify_egress_token(token, key=KEY, now=NOW) == original


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: token[:-1] + ("A" if token[-1] != "A" else "B"),
        lambda token: token[:-5] + ("A" if token[-5] != "A" else "B") + token[-4:],
        lambda token: token.rsplit(".", 1)[0] + "." + "A" * 43,
        lambda token: token[:-2],
        lambda token: token + "A",
    ],
    ids=["last-char", "middle-char", "wrong-signature", "truncated", "trailing"],
)
def test_egress_token_rejects_signature_tamper(mutate):
    token = mint_egress_token(claims(), key=KEY, now=NOW)
    tampered = mutate(token)
    assert tampered != token

    with pytest.raises(EgressTokenError) as excinfo:
        verify_egress_token(tampered, key=KEY, now=NOW)

    assert token not in str(excinfo.value)
    assert tampered not in str(excinfo.value)


def test_egress_token_rejects_payload_tamper_even_with_valid_looking_hmac():
    token = mint_egress_token(claims(), key=KEY, now=NOW)
    document = _decode_payload(token)
    document["task_id"] = "task_2"
    forged = f"egt.{_b64(_canonical(document))}.{token.split('.')[2]}"

    with pytest.raises(EgressTokenError):
        verify_egress_token(forged, key=KEY, now=NOW)


@pytest.mark.parametrize(
    ("offset", "valid"),
    [
        (timedelta(seconds=59), True),
        (timedelta(seconds=60), False),
        (timedelta(seconds=61), False),
    ],
)
def test_egress_token_enforces_expiry_boundary(offset, valid):
    token = mint_egress_token(claims(), key=KEY, now=NOW)

    if valid:
        assert verify_egress_token(token, key=KEY, now=NOW + offset) == claims()
    else:
        with pytest.raises(EgressTokenError):
            verify_egress_token(token, key=KEY, now=NOW + offset)


def test_egress_token_rejects_use_before_issue():
    token = mint_egress_token(claims(), key=KEY, now=NOW)

    with pytest.raises(EgressTokenError):
        verify_egress_token(token, key=KEY, now=NOW - timedelta(seconds=1))


def test_egress_token_rejects_cross_key_verification():
    token = mint_egress_token(claims(), key=KEY, now=NOW)

    with pytest.raises(EgressTokenError) as excinfo:
        verify_egress_token(token, key=OTHER_KEY, now=NOW)

    assert token not in str(excinfo.value)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "egt",
        "egt.",
        "egt..",
        "egt.a.b",
        "egt.a.b.c",
        "e.g.t",
        " egt.a.b",
        "egt.a.b ",
        "EGT.a.b",
        "egt.a!.b",
        "egt.a=b.b",
        "egt." + "a" * 5000 + ".b",
        "egt.not-base64-json.b",
        None,
        b"egt.a.b",
        1,
    ],
)
def test_egress_token_rejects_malformed_input_without_echoing_it(token):
    with pytest.raises(EgressTokenError) as excinfo:
        verify_egress_token(token, key=KEY, now=NOW)

    message = str(excinfo.value)
    if isinstance(token, str) and token:
        assert token not in message
    assert "egt." not in message


def test_egress_token_rejects_non_canonical_json_even_when_signed():
    raw = json.dumps(payload(), indent=2).encode("utf-8")
    signature = hmac.new(KEY, raw, hashlib.sha256).digest()
    token = f"egt.{_b64(raw)}.{_b64(signature)}"

    with pytest.raises(EgressTokenError):
        verify_egress_token(token, key=KEY, now=NOW)


@pytest.mark.parametrize(
    "document",
    [
        {key: value for key, value in payload().items() if key != "nonce"},
        {**payload(), "scope": "everything"},
        payload(v=2),
        payload(v=True),
        payload(task_id=""),
        payload(task_id="  "),
        payload(provider_host="https://provider.example"),
        payload(path_prefix="v1"),
        payload(expires_at=_iso_utc(NOW)),
        payload(issued_at=_iso_utc(NOW + timedelta(seconds=1))),
    ],
    ids=[
        "missing-key",
        "unknown-key",
        "wrong-version",
        "bool-version",
        "empty-task",
        "blank-task",
        "host-with-scheme",
        "path-without-slash",
        "zero-ttl",
        "issued-after-expiry",
    ],
)
def test_egress_token_rejects_invalid_claims_even_when_signed(document):
    token = _mint_raw(document)

    with pytest.raises(EgressTokenError):
        verify_egress_token(token, key=KEY, now=NOW)


def test_egress_token_rejects_non_object_payload_even_when_signed():
    token = _mint_raw(payload())  # sanity: valid
    assert verify_egress_token(token, key=KEY, now=NOW) == claims()

    for document in (["not", "a", "mapping"], "a string", 7):
        raw = _canonical({"value": document})
        signature = hmac.new(KEY, raw, hashlib.sha256).digest()
        token = f"egt.{_b64(raw)}.{_b64(signature)}"
        with pytest.raises(EgressTokenError):
            verify_egress_token(token, key=KEY, now=NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 2},
        {"task_id": ""},
        {"task_id": "  "},
        {"task_id": "task\n1"},
        {"task_id": "t" * 201},
        {"grant_id": ""},
        {"unit": ""},
        {"unit": "unit with space"},
        {"model": ""},
        {"model": "m" * 201},
        {"provider_host": ""},
        {"provider_host": "https://provider.example"},
        {"provider_host": "provider.example/path"},
        {"provider_host": "provider example"},
        {"path_prefix": ""},
        {"path_prefix": "v1"},
        {"path_prefix": "/v1\n"},
        {"nonce": ""},
        {"nonce": "short"},
        {"nonce": "n" * 129},
        {"nonce": "not a nonce"},
        {"issued_at": datetime(2026, 10, 25, 12, 0, 0)},
        {"expires_at": datetime(2026, 10, 25, 12, 1, 0)},
        {"expires_at": NOW},
        {"expires_at": NOW + timedelta(seconds=MAX_EGRESS_TTL_SECONDS + 1)},
        {"expires_at": NOW + timedelta(seconds=1) - timedelta(hours=1)},
    ],
    ids=[
        "version",
        "empty-task",
        "blank-task",
        "newline-task",
        "oversized-task",
        "empty-grant",
        "empty-unit",
        "unit-space",
        "empty-model",
        "oversized-model",
        "empty-host",
        "host-scheme",
        "host-path",
        "host-space",
        "empty-prefix",
        "prefix-no-slash",
        "prefix-newline",
        "empty-nonce",
        "short-nonce",
        "oversized-nonce",
        "nonce-space",
        "naive-issued",
        "naive-expires",
        "zero-ttl",
        "ttl-above-max",
        "ttl-negative",
    ],
)
def test_mint_rejects_invalid_claim_values(overrides):
    with pytest.raises(EgressTokenError) as excinfo:
        mint_egress_token(claims(**overrides), key=KEY, now=NOW)

    assert KEY.decode() not in str(excinfo.value)


def test_mint_rejects_already_expired_claims():
    original = claims(
        issued_at=NOW - timedelta(seconds=120),
        expires_at=NOW - timedelta(seconds=60),
    )

    with pytest.raises(EgressTokenError):
        mint_egress_token(original, key=KEY, now=NOW)


def test_mint_rejects_not_yet_valid_claims():
    original = claims(
        issued_at=NOW + timedelta(seconds=60),
        expires_at=NOW + timedelta(seconds=120),
    )

    with pytest.raises(EgressTokenError):
        mint_egress_token(original, key=KEY, now=NOW)


@pytest.mark.parametrize("key", [b"", b"short", "k" * 32, None, 32])
def test_mint_and_verify_reject_invalid_keys(key):
    with pytest.raises(EgressTokenError):
        mint_egress_token(claims(), key=key, now=NOW)

    token = mint_egress_token(claims(), key=KEY, now=NOW)
    with pytest.raises(EgressTokenError):
        verify_egress_token(token, key=key, now=NOW)


def test_mint_and_verify_reject_naive_now():
    with pytest.raises(EgressTokenError):
        mint_egress_token(claims(), key=KEY, now=datetime(2026, 10, 25, 12, 0, 0))

    token = mint_egress_token(claims(), key=KEY, now=NOW)
    with pytest.raises(EgressTokenError):
        verify_egress_token(token, key=KEY, now=datetime(2026, 10, 25, 12, 0, 0))


def test_verify_defaults_now_to_current_utc_clock():
    token = mint_egress_token(
        claims(
            issued_at=datetime.now(UTC) - timedelta(seconds=5),
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
        ),
        key=KEY,
    )

    assert verify_egress_token(token, key=KEY).task_id == "task_1"


def test_verify_uses_constant_time_comparison(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(hermes_egress.hmac, "compare_digest", spy)
    token = mint_egress_token(claims(), key=KEY, now=NOW)

    assert verify_egress_token(token, key=KEY, now=NOW) == claims()
    assert len(calls) == 1
    expected, provided = calls[0]
    assert len(expected) == 32
    assert len(provided) == 32


def test_egress_token_hash_is_stable_and_opaque():
    token = mint_egress_token(claims(), key=KEY, now=NOW)
    digest = egress_token_hash(token)

    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
    assert egress_token_hash(token) == digest
    assert egress_token_hash(token + "A") != digest
    assert token not in digest
    assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("token", ["", None, 1, b"egt.a.b", "t" * (MAX_EGRESS_TOKEN_LENGTH + 1)])
def test_egress_token_hash_rejects_invalid_input(token):
    with pytest.raises(EgressTokenError):
        egress_token_hash(token)


def test_egress_claims_are_frozen_and_never_hold_the_token():
    original = claims()

    with pytest.raises(dataclasses.FrozenInstanceError):
        original.task_id = "task_2"  # type: ignore[misc]

    token = mint_egress_token(original, key=KEY, now=NOW)
    assert token not in repr(original)
    assert token not in str(original)
