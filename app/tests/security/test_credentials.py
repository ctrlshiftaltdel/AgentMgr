"""Credential, JCS canonicalisation and revocation tests (Issue #82).

Pure in-memory unit tests: no database, no privileged host, no filesystem
outside ``tmp_path``.  They pin the wire format of the ``agx1`` token (JCS
bytes, exact claim key set, HMAC-SHA256, base64url without padding), the
fail-closed rules for keys and time, the call-time Grant/capability/scope
checks, and the revocation stores' semantics.

The token itself is never interpolated into an error or a ``repr``; several
tests assert that directly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.security.boundary import GrantStatus, ResourceScope
from app.security.credentials import (
    CredentialIssuer,
    CredentialValidator,
    FileRevocationStore,
    InMemoryRevocationStore,
    IssuedCredential,
    canonicalize_claims,
    load_keyring_from_env,
)
from app.security.errors import SecurityError

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
KID = "k1"
KEY = bytes(range(32))
OTHER_KID = "k2"
TASK_ID = "task_synthetic_0001"
GRANT_ID = "grnt_synthetic_0001"
SUBJECT_ID = "prin_synthetic_0001"
RESOURCE_ID = "res_synthetic_0001"
OTHER_RESOURCE_ID = "res_synthetic_0002"
CAPABILITY = "resource.read@1.0.0"
OTHER_CAPABILITY = "resource.write@1.0.0"

KEYRING_ENV = "AGENTMGR_EXEC_CREDENTIAL_KEYRING"
ACTIVE_KID_ENV = "AGENTMGR_EXEC_CREDENTIAL_ACTIVE_KID"

JTI_1 = "cred_" + "a" * 32
JTI_2 = "cred_" + "b" * 32
JTI_3 = "cred_" + "c" * 32


def _clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _scope(
    resource_ids: tuple[str, ...] = (RESOURCE_ID,),
    exclude: tuple[str, ...] = (),
) -> ResourceScope:
    return ResourceScope(
        scope_type="self",
        resource_ids=resource_ids,
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=exclude,
    )


def _grant_status(**overrides: object) -> GrantStatus:
    value: dict[str, object] = {
        "grant_id": GRANT_ID,
        "task_id": TASK_ID,
        "subject_id": SUBJECT_ID,
        "execution_mode": "managed_execution",
        "status": "active",
        "usable": True,
        "expires_at": NOW + timedelta(seconds=60),
        "revocation_reason": None,
        "capabilities": (CAPABILITY,),
        "resource_scope": _scope(),
    }
    value.update(overrides)
    return GrantStatus(**value)  # type: ignore[arg-type]


def _issuer(clock: Callable[[], datetime] | None = None) -> CredentialIssuer:
    return CredentialIssuer({KID: KEY}, KID, clock or _clock(NOW))


def _validator(clock: Callable[[], datetime] | None = None) -> CredentialValidator:
    return CredentialValidator({KID: KEY}, clock or _clock(NOW))


def _issue(**overrides: object) -> IssuedCredential:
    value: dict[str, object] = {
        "task_id": TASK_ID,
        "grant_id": GRANT_ID,
        "subject_id": SUBJECT_ID,
        "capabilities": [CAPABILITY],
        "resource_scope": _scope(),
        "ttl_seconds": 60,
    }
    value.update(overrides)
    return _issuer().issue(**value)  # type: ignore[arg-type]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(payload: dict[str, object], *, key: bytes = KEY) -> str:
    """Build a token exactly as the issuer would, for forged-claim tests."""

    segment = _b64url(canonicalize_claims(payload))
    mac = hmac.new(key, b"agx1." + segment.encode("ascii"), hashlib.sha256).digest()
    return f"agx1.{segment}.{_b64url(mac)}"


def _payload_segment(token: str) -> str:
    return token.split(".")[1]


def test_jcs_utf8_key_order_and_integers() -> None:
    expected = '{"a":[1,2],"z":1,"ä":"café"}'.encode("utf-8")

    assert canonicalize_claims({"z": 1, "ä": "café", "a": [1, 2]}) == expected
    assert canonicalize_claims({"n": -0}) == b'{"n":0}'
    assert canonicalize_claims({"s": "a\nb"}) == b'{"s":"a\\nb"}'
    assert canonicalize_claims({"b": {"d": 2, "c": 1}}) == b'{"b":{"c":1,"d":2}}'

    # RFC 8785 orders object keys by UTF-16 code units, not by code point:
    # U+1F600 is the surrogate pair D83D DE00 and therefore sorts before U+FFFF.
    assert (
        canonicalize_claims({"\uffff": 2, "\U0001f600": 1})
        == '{"\U0001f600":1,"\uffff":2}'.encode("utf-8")
    )

    # Integers are exact only up to 2**53-1.
    assert canonicalize_claims({"n": 9007199254740991}) == b'{"n":9007199254740991}'
    assert canonicalize_claims({"n": -9007199254740991}) == b'{"n":-9007199254740991}'

    for invalid in (
        {"n": 1.5},
        {"n": 9007199254740992},
        {"n": -9007199254740992},
        {"n": float("nan")},
        {"n": float("inf")},
        {1: "value"},
        {"n": b"bytes"},
        {"n": datetime(2026, 1, 1, tzinfo=UTC)},
        {"n": ["\ud800"]},
    ):
        with pytest.raises(SecurityError) as raised:
            canonicalize_claims(invalid)  # type: ignore[arg-type]
        assert raised.value.code == "invalid_claim"


def test_issue_and_validate_round_trip() -> None:
    issued = _issue(capabilities=[OTHER_CAPABILITY, CAPABILITY])

    assert issued.token.startswith("agx1.")
    assert issued.token.count(".") == 2
    assert "=" not in issued.token
    assert len(issued.token.encode("ascii")) <= 4096

    claims = issued.claims
    assert claims.v == 1
    assert claims.iss == "agentmgr-exec"
    assert claims.aud == "pep-gateway"
    assert claims.kid == KID
    assert re.fullmatch(r"^cred_[0-9a-f]{32}$", claims.jti)
    assert claims.task_id == TASK_ID
    assert claims.grant_id == GRANT_ID
    assert claims.subject_id == SUBJECT_ID
    assert claims.capabilities == (CAPABILITY, OTHER_CAPABILITY)
    assert re.fullmatch(r"^[0-9a-f]{32}$", claims.nonce)
    assert claims.iat == int(NOW.timestamp())
    assert claims.nbf == claims.iat - 5
    assert claims.exp - claims.iat == 60
    assert claims.exp - claims.iat <= 300
    assert set(claims.as_dict()) == {
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

    validated = _validator().validate(issued.token)
    assert validated == claims
    assert _validator().validate(
        issued.token, expected_task_id=TASK_ID, expected_grant_id=GRANT_ID
    ) == claims

    maximum = _issue(ttl_seconds=300)
    assert maximum.claims.exp - maximum.claims.iat == 300
    assert _validator().validate(maximum.token) == maximum.claims

    for ttl in (0, 301):
        with pytest.raises(SecurityError) as raised:
            _issue(ttl_seconds=ttl)
        assert raised.value.code == "invalid_claim"


def test_token_tamper_wrong_kid_and_short_key_fail_closed() -> None:
    issued = _issue()
    prefix, segment, mac = issued.token.split(".")
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    tampered_payload = _b64url(bytes([raw[0] ^ 0x01]) + raw[1:])
    tampered = f"{prefix}.{tampered_payload}.{mac}"

    for bad_token in (tampered, issued.token[:-1], "", "agx1.only-two-parts", 123):
        with pytest.raises(SecurityError) as raised:
            _validator().validate(bad_token)  # type: ignore[arg-type]
        assert raised.value.code == "token_invalid"

    unknown_kid = CredentialValidator({OTHER_KID: KEY}, _clock(NOW))
    with pytest.raises(SecurityError) as raised:
        unknown_kid.validate(issued.token)
    assert raised.value.code == "token_invalid"

    short_key = b"0123456789abcdef"
    with pytest.raises(SecurityError) as raised:
        CredentialValidator({KID: short_key}, _clock(NOW))
    assert raised.value.code == "credential_key_invalid"
    with pytest.raises(SecurityError) as raised:
        CredentialIssuer({KID: short_key}, KID, _clock(NOW))
    assert raised.value.code == "credential_key_invalid"
    with pytest.raises(SecurityError) as raised:
        CredentialIssuer({KID: KEY}, OTHER_KID, _clock(NOW))
    assert raised.value.code == "credential_key_invalid"


def test_expiry_and_not_before() -> None:
    issued = _issue(ttl_seconds=60)

    expired = CredentialValidator(
        {KID: KEY}, _clock(NOW + timedelta(seconds=60))
    )
    with pytest.raises(SecurityError) as raised:
        expired.validate(issued.token)
    assert raised.value.code == "credential_expired"

    early = CredentialValidator(
        {KID: KEY}, _clock(NOW - timedelta(seconds=6))
    )
    with pytest.raises(SecurityError) as raised:
        early.validate(issued.token)
    assert raised.value.code == "token_not_yet_valid"

    at_nbf = CredentialValidator({KID: KEY}, _clock(NOW - timedelta(seconds=5)))
    assert at_nbf.validate(issued.token) == issued.claims


def test_claim_key_set_is_exact() -> None:
    claims = _issue(capabilities=[OTHER_CAPABILITY, CAPABILITY]).claims
    extra = claims.as_dict()
    extra["role"] = "admin"
    missing = claims.as_dict()
    del missing["nonce"]
    wrong_type = claims.as_dict()
    wrong_type["iat"] = str(wrong_type["iat"])
    unsorted = claims.as_dict()
    unsorted["capabilities"] = [OTHER_CAPABILITY, CAPABILITY]

    for payload in (extra, missing, wrong_type, unsorted):
        with pytest.raises(SecurityError) as raised:
            _validator().validate(_sign(payload))
        assert raised.value.code == "token_invalid"
        assert raised.value.detail == "claim_invalid"

    # A validly signed token whose claim values are out of range also fails.
    with pytest.raises(SecurityError) as raised:
        _validator().validate(_sign({**claims.as_dict(), "exp": claims.iat + 400}))
    assert raised.value.code == "token_invalid"

    assert _validator().validate(_sign(claims.as_dict())) == claims


def test_validate_call_rejects_cross_task_scope_and_capability() -> None:
    claims = _issue().claims
    validator = _validator()

    assert (
        validator.validate_call(
            claims,
            grant_status=_grant_status(),
            capability=CAPABILITY,
            resource=RESOURCE_ID,
        )
        is None
    )

    for grant in (
        _grant_status(task_id="task_synthetic_0002"),
        _grant_status(grant_id="grnt_synthetic_0002"),
        _grant_status(subject_id="svc_synthetic_0002"),
    ):
        with pytest.raises(SecurityError) as raised:
            validator.validate_call(
                claims,
                grant_status=grant,
                capability=CAPABILITY,
                resource=RESOURCE_ID,
            )
        assert raised.value.code == "cross_task_denied"

    for grant in (
        _grant_status(status="revoked", revocation_reason="grant_revoked"),
        _grant_status(status="consumed"),
        _grant_status(usable=False),
    ):
        with pytest.raises(SecurityError) as raised:
            validator.validate_call(
                claims,
                grant_status=grant,
                capability=CAPABILITY,
                resource=RESOURCE_ID,
            )
        assert raised.value.code == "grant_revoked"

    with pytest.raises(SecurityError) as raised:
        validator.validate_call(
            claims,
            grant_status=_grant_status(expires_at=NOW - timedelta(seconds=1)),
            capability=CAPABILITY,
            resource=RESOURCE_ID,
        )
    assert raised.value.code == "grant_expired"

    with pytest.raises(SecurityError) as raised:
        validator.validate_call(
            claims,
            grant_status=_grant_status(execution_mode="governed_auto"),
            capability=CAPABILITY,
            resource=RESOURCE_ID,
        )
    assert raised.value.code == "execution_mode_mismatch"

    with pytest.raises(SecurityError) as raised:
        validator.validate_call(
            claims,
            grant_status=_grant_status(),
            capability=OTHER_CAPABILITY,
            resource=RESOURCE_ID,
        )
    assert raised.value.code == "capability_not_granted"

    with pytest.raises(SecurityError) as raised:
        validator.validate_call(
            claims,
            grant_status=_grant_status(),
            capability=CAPABILITY,
            resource=OTHER_RESOURCE_ID,
        )
    assert raised.value.code == "resource_scope_denied"

    excluded = _issue(
        resource_scope=_scope(
            resource_ids=(RESOURCE_ID, OTHER_RESOURCE_ID),
            exclude=(OTHER_RESOURCE_ID,),
        )
    ).claims
    with pytest.raises(SecurityError) as raised:
        validator.validate_call(
            excluded,
            grant_status=_grant_status(
                resource_scope=_scope(
                    resource_ids=(RESOURCE_ID, OTHER_RESOURCE_ID),
                    exclude=(OTHER_RESOURCE_ID,),
                )
            ),
            capability=CAPABILITY,
            resource=OTHER_RESOURCE_ID,
        )
    assert raised.value.code == "resource_scope_denied"

    # The Grant is authoritative: a resource dropped from the live Grant scope
    # is denied even though the token was issued with it.
    narrowed = _issue(
        resource_scope=_scope(resource_ids=(RESOURCE_ID, OTHER_RESOURCE_ID))
    ).claims
    with pytest.raises(SecurityError) as raised:
        validator.validate_call(
            narrowed,
            grant_status=_grant_status(),
            capability=CAPABILITY,
            resource=OTHER_RESOURCE_ID,
        )
    assert raised.value.code == "resource_scope_denied"


def test_revocation_store_memory_and_file(tmp_path: Path) -> None:
    memory = InMemoryRevocationStore()
    assert memory.lookup(JTI_1) is None

    memory.revoke(JTI_1, reason="grant_revoked", revoked_at=NOW)
    record = memory.lookup(JTI_1)
    assert record is not None
    assert record.jti == JTI_1
    assert record.reason == "grant_revoked"
    assert record.revoked_at == NOW

    memory.revoke(JTI_1, reason="other_reason", revoked_at=NOW + timedelta(seconds=1))
    assert memory.lookup(JTI_1) == record

    assert memory.prune(NOW + timedelta(seconds=599)) == 0
    assert memory.prune(NOW + timedelta(seconds=600)) == 1
    assert memory.lookup(JTI_1) is None

    root = tmp_path / "revoked"
    store = FileRevocationStore(root, _clock(NOW))
    store.revoke(JTI_1, reason="grant_revoked", revoked_at=NOW)

    record_path = root / f"{JTI_1}.json"
    assert record_path.is_file()
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700

    reloaded = FileRevocationStore(root, _clock(NOW))
    assert reloaded.lookup(JTI_1) == store.lookup(JTI_1) == record

    reloaded.revoke(JTI_1, reason="other_reason", revoked_at=NOW + timedelta(seconds=1))
    assert reloaded.lookup(JTI_1) == record

    assert reloaded.lookup(JTI_2) is None
    (root / f"{JTI_2}.json.tmp").write_text("half-written", encoding="utf-8")
    assert reloaded.lookup(JTI_2) is None
    assert reloaded.prune(NOW) == 0
    assert not (root / f"{JTI_2}.json.tmp").exists()

    assert reloaded.prune(NOW + timedelta(seconds=599)) == 0
    assert reloaded.prune(NOW + timedelta(seconds=600)) == 1
    assert reloaded.lookup(JTI_1) is None

    for invalid in ("../escape", "cred_nothex", "", JTI_3 + "\x00"):
        with pytest.raises(SecurityError) as raised:
            reloaded.lookup(invalid)
        assert raised.value.code == "invalid_claim"
        with pytest.raises(SecurityError):
            reloaded.revoke(invalid, reason="grant_revoked", revoked_at=NOW)
    assert not (tmp_path / "escape.json").exists()

    (root / f"{JTI_3}.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SecurityError) as raised:
        reloaded.lookup(JTI_3)
    assert raised.value.code == "unknown"


def test_file_revocation_store_fails_closed_when_root_disappears(
    tmp_path: Path,
) -> None:
    root = tmp_path / "revoked"
    store = FileRevocationStore(root, _clock(NOW))
    store.revoke(JTI_1, reason="grant_revoked", revoked_at=NOW)
    assert store.lookup(JTI_1) is not None

    shutil.rmtree(root)

    with pytest.raises(SecurityError) as raised:
        store.lookup(JTI_1)
    assert raised.value.code == "unknown"
    assert raised.value.detail == "revocation_store_unreadable"

    # A missing record under a readable root is still a legitimate absence.
    store = FileRevocationStore(root, _clock(NOW))
    assert store.lookup(JTI_2) is None


def test_load_keyring_from_env_fails_closed() -> None:
    valid = {
        KEYRING_ENV: json.dumps({KID: base64.b64encode(KEY).decode("ascii")}),
        ACTIVE_KID_ENV: KID,
    }
    keyring, active = load_keyring_from_env(valid)
    assert active == KID
    assert dict(keyring) == {KID: KEY}

    short = {
        KEYRING_ENV: json.dumps(
            {KID: base64.b64encode(b"0123456789abcdef").decode("ascii")}
        ),
        ACTIVE_KID_ENV: KID,
    }
    cases = (
        {ACTIVE_KID_ENV: KID},
        {KEYRING_ENV: valid[KEYRING_ENV]},
        {KEYRING_ENV: "not json", ACTIVE_KID_ENV: KID},
        {KEYRING_ENV: json.dumps({}), ACTIVE_KID_ENV: KID},
        {KEYRING_ENV: json.dumps({KID: "not base64!!"}), ACTIVE_KID_ENV: KID},
        short,
        {KEYRING_ENV: valid[KEYRING_ENV], ACTIVE_KID_ENV: OTHER_KID},
        {KEYRING_ENV: json.dumps({KID: 1}), ACTIVE_KID_ENV: KID},
    )
    for env in cases:
        with pytest.raises(SecurityError) as raised:
            load_keyring_from_env(env)
        assert raised.value.code == "credential_key_invalid"


def test_token_never_repr_or_error_leaks_secret() -> None:
    issued = _issue()

    for rendered in (repr(issued), repr(issued.claims), str(issued.claims.as_dict())):
        assert issued.token not in rendered
        assert _payload_segment(issued.token) not in rendered
    assert issued.token not in json.dumps(issued.claims.as_dict())

    expired = CredentialValidator(
        {KID: KEY}, _clock(NOW + timedelta(seconds=3600))
    )
    with pytest.raises(SecurityError) as raised:
        expired.validate(issued.token)
    for rendered in (str(raised.value), repr(raised.value)):
        assert issued.token not in rendered
        assert _payload_segment(issued.token) not in rendered

    with pytest.raises(SecurityError) as raised:
        _validator().validate(issued.token[:-2] + "aa")
    assert issued.token not in str(raised.value)
