"""PEP gateway, admission limits, audit redaction and file backend tests (#82).

The gateway is the only external entry a managed task may reach.  These tests
pin the validation order and stable reason codes of design §10.4, the admission
and rate limits of §10.1, the fail-closed audit rule of §10.6 and the
filesystem backend's traversal defences of §10.5.

Everything runs against ``tmp_path``, an in-memory Grant provider and at most
one temporary Unix socket: no database, no privileged host and no IP listener.
The bearer token never reaches an audit event; several tests assert that.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.security.boundary import GrantStatus, ResourceScope
from app.security.credentials import (
    CredentialIssuer,
    CredentialValidator,
    InMemoryRevocationStore,
    IssuedCredential,
    canonicalize_claims,
)
from app.security.errors import SecurityError
from app.security.gateway import (
    AUDIT_FIELDS,
    GATEWAY_PROTOCOL_VERSION,
    MAX_RESOURCE_BYTES,
    FileResourceBackend,
    GatewayConfig,
    GatewayLimits,
    GatewayResponse,
    GatewayServer,
    RateLimiter,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
KID = "k1"
KEY = bytes(range(32))
TASK_ID = "task_synthetic_0001"
OTHER_TASK_ID = "task_synthetic_0002"
GRANT_ID = "grnt_synthetic_0001"
OTHER_GRANT_ID = "grnt_synthetic_0002"
SUBJECT_ID = "prin_synthetic_0001"
OTHER_SUBJECT_ID = "svc_synthetic_0002"
RESOURCE_ID = "res_synthetic_0001"
OTHER_RESOURCE_ID = "res_synthetic_0002"
CAPABILITY = "resource.read@1.0.0"
OTHER_CAPABILITY = "resource.write@1.0.0"
PEER_UID = 1000

RESOURCE_BODY = {"kind": "synthetic", "value": "alpha"}


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


def _grant(**overrides: object) -> GrantStatus:
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


def _request(token: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "version": GATEWAY_PROTOCOL_VERSION,
        "token": token,
        "capability": CAPABILITY,
        "resource": RESOURCE_ID,
        "payload": {},
    }
    value.update(overrides)
    return value


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(payload: dict[str, object]) -> str:
    """Build a token exactly as the issuer would, for forged-claim tests."""

    segment = _b64url(canonicalize_claims(payload))
    mac = hmac.new(KEY, b"agx1." + segment.encode("ascii"), hashlib.sha256).digest()
    return f"agx1.{segment}.{_b64url(mac)}"


class _Provider:
    """Live Grant provider whose answer the test mutates between calls."""

    def __init__(self, grant: GrantStatus | None, *, ignore_lookup: bool = False):
        self.grant = grant
        self.calls = 0
        self._ignore_lookup = ignore_lookup

    def status(self, grant_id: str, *, task_id: str) -> GrantStatus | None:
        self.calls += 1
        if self._ignore_lookup:
            return self.grant
        if self.grant is None or self.grant.grant_id != grant_id:
            return None
        return self.grant


class _RaisingProvider(_Provider):
    """Provider whose backing source is unavailable."""

    def __init__(self) -> None:
        super().__init__(None)

    def status(self, grant_id: str, *, task_id: str) -> GrantStatus | None:
        raise SecurityError("unknown", "grant_source_unavailable")


class _Recorder:
    """Audit sink that keeps a copy of every event."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(self, event: object) -> None:
        assert isinstance(event, dict)
        self.events.append(dict(event))


class _RaisingSink:
    def record(self, event: object) -> None:
        raise RuntimeError("audit sink unavailable")


@dataclass(frozen=True)
class _Harness:
    config: GatewayConfig
    server: GatewayServer
    provider: _Provider
    sink: _Recorder
    issued: IssuedCredential
    resources_root: Path

    @property
    def token(self) -> str:
        return self.issued.token

    def call(
        self, request: object, *, peer_uid: int | None = PEER_UID
    ) -> GatewayResponse:
        return self.server.handle_request(  # type: ignore[arg-type]
            request, peer_uid=peer_uid
        )


def _build(
    tmp_path: Path,
    *,
    now: datetime = NOW,
    token_now: datetime = NOW,
    provider: _Provider | None = None,
    sink: object | None = None,
    revocation: object | None = None,
    limits: GatewayLimits | None = None,
) -> _Harness:
    root = tmp_path / "resources"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{RESOURCE_ID}.json").write_text(
        json.dumps(RESOURCE_BODY), encoding="utf-8"
    )
    (root / f"{OTHER_RESOURCE_ID}.json").write_text(
        json.dumps({"kind": "synthetic", "value": "beta"}), encoding="utf-8"
    )

    issued = CredentialIssuer({KID: KEY}, KID, _clock(token_now)).issue(
        task_id=TASK_ID,
        grant_id=GRANT_ID,
        subject_id=SUBJECT_ID,
        capabilities=[CAPABILITY],
        resource_scope=_scope(),
        ttl_seconds=60,
    )
    recorder = sink if sink is not None else _Recorder()
    live_provider = provider if provider is not None else _Provider(_grant())
    config = GatewayConfig(
        socket_path=tmp_path / "gateway.sock",
        resources_root=root,
        validator=CredentialValidator({KID: KEY}, _clock(now)),
        revocation_store=(
            revocation
            if revocation is not None
            else InMemoryRevocationStore(_clock(now))
        ),
        grant_provider=live_provider,
        audit_sink=recorder,  # type: ignore[arg-type]
        limits=limits if limits is not None else GatewayLimits(),
        clock=_clock(now),
    )
    return _Harness(
        config=config,
        server=GatewayServer(config),
        provider=live_provider,
        sink=recorder,  # type: ignore[arg-type]
        issued=issued,
        resources_root=root,
    )


def test_allowed_read_returns_scoped_resource(tmp_path: Path) -> None:
    harness = _build(tmp_path)

    response = harness.call(_request(harness.token))

    assert response.ok is True
    assert response.reason is None
    assert response.result == {
        "resource_id": RESOURCE_ID,
        "media_type": "application/json",
        "body": RESOURCE_BODY,
    }
    assert json.dumps(response.result, sort_keys=True).count(RESOURCE_ID) == 1
    assert len(harness.sink.events) == 1
    assert harness.sink.events[0]["decision"] == "allow"


def test_unknown_fields_bad_json_and_oversize(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    oversized = b"x" * (harness.config.limits.max_request_bytes + 1)

    denials = {
        "extra_field": harness.call({**_request(harness.token), "extra": 1}),
        "missing_fields": harness.call({"version": GATEWAY_PROTOCOL_VERSION}),
        "not_an_object": harness.call(b"[]\n"),
        "invalid_json": harness.call(b"{not json\n"),
        "blank_line": harness.call(b"\n"),
        "empty": harness.call(b""),
        "duplicate_key": harness.call(
            b'{"version":"1.0.0","version":"1.0.0"}\n'
        ),
        "two_requests": harness.call(
            json.dumps(_request(harness.token)).encode("utf-8") + b"\n{}\n"
        ),
        "payload_not_empty": harness.call(
            _request(harness.token, payload={"anything": 1})
        ),
        "payload_not_object": harness.call(_request(harness.token, payload=[])),
        "token_not_string": harness.call(
            {**_request(harness.token), "token": 7}
        ),
        "capability_not_string": harness.call(
            _request(harness.token, capability=None)
        ),
        "wrong_version": harness.call(_request(harness.token, version="2.0.0")),
        "oversized": harness.call(oversized),
    }

    for name, response in denials.items():
        assert response.ok is False, name
    for name in denials.keys() - {"oversized", "wrong_version"}:
        assert denials[name].reason == "bad_request", name
    assert denials["wrong_version"].reason == "unsupported_version"
    assert denials["oversized"].reason == "payload_too_large"

    # The limit counts the newline and is inclusive: a 64 KiB line reaches the
    # parser (and fails as JSON) instead of being reported as too large.
    at_limit = b" " * (harness.config.limits.max_request_bytes - 1) + b"\n"
    assert harness.call(at_limit).reason == "bad_request"

    assert all("token" not in event for event in harness.sink.events)


def test_token_expired_revoked_and_wrong_audience(tmp_path: Path) -> None:
    expired = _build(
        tmp_path / "expired", now=NOW + timedelta(seconds=120), token_now=NOW
    )
    assert expired.call(_request(expired.token)).reason == "credential_expired"

    not_yet = _build(tmp_path / "early", now=NOW - timedelta(seconds=10))
    assert not_yet.call(_request(not_yet.token)).reason == "token_not_yet_valid"

    store = InMemoryRevocationStore(_clock(NOW))
    revoked = _build(tmp_path / "revoked", revocation=store)
    store.revoke(revoked.issued.claims.jti, reason="task_stopped", revoked_at=NOW)
    assert revoked.call(_request(revoked.token)).reason == "credential_revoked"

    wrong_audience = _build(tmp_path / "aud")
    payload = dict(wrong_audience.issued.claims.as_dict())
    payload["aud"] = "other-gateway"
    assert wrong_audience.call(_request(_sign(payload))).reason == "token_invalid"

    tampered = _build(tmp_path / "tampered")
    prefix, segment, mac = tampered.token.split(".")
    replacement = "A" if mac[-1] != "A" else "B"
    broken = f"{prefix}.{segment}.{mac[:-1]}{replacement}"
    assert tampered.call(_request(broken)).reason == "token_invalid"


def test_grant_revoked_and_mode_mismatch(tmp_path: Path) -> None:
    provider = _Provider(_grant())
    harness = _build(tmp_path, provider=provider)

    assert harness.call(_request(harness.token)).ok is True

    for grant, reason in (
        (_grant(status="revoked"), "grant_revoked"),
        (_grant(usable=False), "grant_revoked"),
        (_grant(status="consumed"), "grant_revoked"),
        (_grant(execution_mode="legacy_shell"), "execution_mode_mismatch"),
        (_grant(expires_at=NOW - timedelta(seconds=1)), "grant_expired"),
    ):
        provider.grant = grant
        assert harness.call(_request(harness.token)).reason == reason

    # Every call re-reads the live provider: restoring it allows the call
    # again and no earlier answer is cached.
    provider.grant = _grant()
    calls_before = provider.calls
    assert harness.call(_request(harness.token)).ok is True
    assert provider.calls == calls_before + 1

    provider.grant = None
    assert harness.call(_request(harness.token)).reason == "grant_missing"

    raising = _build(tmp_path / "raising", provider=_RaisingProvider())
    assert raising.call(_request(raising.token)).reason == "unknown"


def test_cross_task_capability_and_scope_denied(tmp_path: Path) -> None:
    provider = _Provider(_grant())
    harness = _build(tmp_path, provider=provider)

    for grant in (
        _grant(task_id=OTHER_TASK_ID),
        _grant(subject_id=OTHER_SUBJECT_ID),
    ):
        provider.grant = grant
        assert harness.call(_request(harness.token)).reason == "cross_task_denied"

    # A provider that answers for the wrong Grant id is also cross-task denied.
    ignoring = _build(
        tmp_path / "ignoring",
        provider=_Provider(_grant(grant_id=OTHER_GRANT_ID), ignore_lookup=True),
    )
    assert ignoring.call(_request(ignoring.token)).reason == "cross_task_denied"

    provider.grant = _grant()
    assert (
        harness.call(_request(harness.token, capability=OTHER_CAPABILITY)).reason
        == "capability_not_granted"
    )
    assert (
        harness.call(_request(harness.token, resource=OTHER_RESOURCE_ID)).reason
        == "resource_scope_denied"
    )
    assert (
        harness.call(_request(harness.token, resource="res_")).reason
        == "resource_scope_denied"
    )

    provider.grant = _grant(resource_scope=_scope(exclude=(RESOURCE_ID,)))
    assert harness.call(_request(harness.token)).reason == "resource_scope_denied"


def test_backend_rejects_traversal_symlink_and_non_regular(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resources"
    root.mkdir()
    (root / f"{RESOURCE_ID}.json").write_text(
        json.dumps(RESOURCE_BODY), encoding="utf-8"
    )
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"secret": True}), encoding="utf-8")

    symlink_id = "res_synthetic_0003"
    (root / f"{symlink_id}.json").symlink_to(outside)
    inner_symlink_id = "res_synthetic_0006"
    (root / f"{inner_symlink_id}.json").symlink_to(root / f"{RESOURCE_ID}.json")
    directory_id = "res_synthetic_0004"
    (root / f"{directory_id}.json").mkdir()
    huge_id = "res_synthetic_0005"
    (root / f"{huge_id}.json").write_bytes(
        b'{"value":"' + b"x" * MAX_RESOURCE_BYTES + b'"}'
    )

    backend = FileResourceBackend(root)

    def read(
        resource_id: str,
        scope: ResourceScope,
        capability: str = CAPABILITY,
    ) -> dict[str, object]:
        return backend.read(resource_id, scope=scope, capability=capability)

    scope = _scope(
        (
            RESOURCE_ID,
            OTHER_RESOURCE_ID,
            symlink_id,
            inner_symlink_id,
            directory_id,
            huge_id,
            "res_synthetic_9999",
            "../x",
            "/etc/passwd",
            "res_synthetic_0001/sub",
            "res_\x00nul",
        )
    )

    for resource_id in (
        "../x",
        "/etc/passwd",
        "res_synthetic_0001/sub",
        "res_\x00nul",
        symlink_id,
        inner_symlink_id,
        directory_id,
        huge_id,
        "res_synthetic_9999",
    ):
        with pytest.raises(SecurityError) as raised:
            read(resource_id, scope)
        assert raised.value.code == "resource_denied", resource_id

    # Out-of-scope reads are refused before the filesystem is touched.
    with pytest.raises(SecurityError) as raised:
        read(OTHER_RESOURCE_ID, _scope((RESOURCE_ID,)))
    assert raised.value.code == "resource_scope_denied"

    with pytest.raises(SecurityError) as raised:
        read(RESOURCE_ID, _scope((RESOURCE_ID,), exclude=(RESOURCE_ID,)))
    assert raised.value.code == "resource_scope_denied"

    # Only a read capability may use the backend.
    with pytest.raises(SecurityError) as raised:
        read(RESOURCE_ID, scope, capability=OTHER_CAPABILITY)
    assert raised.value.code == "resource_denied"

    assert read(RESOURCE_ID, scope) == {
        "resource_id": RESOURCE_ID,
        "media_type": "application/json",
        "body": RESOURCE_BODY,
    }


def test_rate_limiter_busy_and_rate_limited() -> None:
    limits = GatewayLimits()
    assert (
        limits.max_connections_per_task,
        limits.max_connections_global,
        limits.max_rps_per_task,
        limits.max_rps_global,
        limits.max_request_bytes,
        limits.timeout_seconds,
    ) == (8, 64, 16, 200, 65536, 5)

    now = [0.0]
    limiter = RateLimiter(limits, monotonic=lambda: now[0])

    first_task = 1000
    for _ in range(8):
        assert limiter.allow_connection(first_task).allowed is True
    denied = limiter.allow_connection(first_task)
    assert denied.allowed is False
    assert denied.limit == "per_task_connections"
    # A second task still has its own connection quota and global capacity.
    assert limiter.allow_connection(1001).allowed is True

    limiter.release_connection(first_task)
    assert limiter.allow_connection(first_task).allowed is True
    for _ in range(9):
        limiter.release_connection(first_task)
    limiter.release_connection(1001)

    # Requests: 16/s per task, counted before the token is read.
    for _ in range(16):
        assert limiter.allow_request(first_task).allowed is True
    limited = limiter.allow_request(first_task)
    assert limited.allowed is False
    assert limited.limit == "per_task_rps"
    assert limiter.allow_request(1001).allowed is True

    # One second later the bucket has refilled.
    now[0] += 1.0
    for _ in range(16):
        assert limiter.allow_request(first_task).allowed is True

    # Global connection ceiling: 8 tasks fill 64 slots, a 65th is refused even
    # though its own quota is untouched.
    global_limiter = RateLimiter(limits, monotonic=lambda: 0.0)
    for uid in range(2000, 2008):
        for _ in range(8):
            assert global_limiter.allow_connection(uid).allowed is True
    global_denied = global_limiter.allow_connection(2008)
    assert global_denied.allowed is False
    assert global_denied.limit == "global_connections"

    # Global request ceiling: 200/s across tasks, a single task cannot spend it.
    request_limiter = RateLimiter(limits, monotonic=lambda: 0.0)
    decisions = [
        request_limiter.allow_request(3000 + index // 16) for index in range(320)
    ]
    assert sum(1 for decision in decisions if decision.allowed) == 200
    assert all(decision.limit == "global_rps" for decision in decisions[200:])


def test_audit_redacts_token(tmp_path: Path) -> None:
    harness = _build(tmp_path)

    allowed = harness.call(_request(harness.token))
    denied = harness.call(_request(harness.token, capability=OTHER_CAPABILITY))

    assert allowed.ok is True
    assert denied.reason == "capability_not_granted"
    events = harness.sink.events
    assert len(events) == 2
    allow_event, deny_event = events

    assert allow_event["event"] == "security.exec.gateway.allow"
    assert allow_event["decision"] == "allow"
    assert "reason" not in allow_event
    assert deny_event["event"] == "security.exec.gateway.deny"
    assert deny_event["decision"] == "deny"
    assert deny_event["reason"] == "capability_not_granted"

    claims = harness.issued.claims
    for event in events:
        assert set(event) <= AUDIT_FIELDS
        assert event["peer_uid"] == PEER_UID
        assert event["task_id"] == TASK_ID
        assert event["grant_id"] == GRANT_ID
        assert event["credential_jti"] == claims.jti
        assert event["capability"] == (
            CAPABILITY if event is allow_event else OTHER_CAPABILITY
        )
        assert event["resource"] == RESOURCE_ID
        assert isinstance(event["ts"], str) and event["ts"].endswith("+00:00")

        serialized = json.dumps(event, sort_keys=True)
        assert harness.token not in serialized
        for segment in harness.token.split("."):
            assert segment not in serialized
        assert "payload" not in serialized
        assert str(tmp_path) not in serialized
        assert "agx1" not in serialized


def test_audit_write_failure_denies(tmp_path: Path) -> None:
    harness = _build(tmp_path, sink=_RaisingSink())

    response = harness.call(_request(harness.token))

    assert response.ok is False
    assert response.reason in {"backend_error", "unknown"}
    assert response.result is None

    # A denial that cannot be audited is still denied, and never raises.
    denied = harness.call(_request(harness.token, capability=OTHER_CAPABILITY))
    assert denied.ok is False
    assert denied.reason == "capability_not_granted"


def test_ndjson_socket_round_trip(tmp_path: Path) -> None:
    harness = _build(tmp_path)

    harness.server.start()
    try:
        assert harness.config.socket_path.exists()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(harness.config.socket_path))
            client.sendall(
                json.dumps(_request(harness.token)).encode("utf-8") + b"\n"
            )
            data = b""
            while not data.endswith(b"\n"):
                chunk = client.recv(4096)
                assert chunk, "server closed before sending one response"
                data += chunk
            assert json.loads(data.decode("utf-8")) == {
                "ok": True,
                "result": {
                    "resource_id": RESOURCE_ID,
                    "media_type": "application/json",
                    "body": RESOURCE_BODY,
                },
            }
            # One request per connection: the server closes, so a second
            # request cannot be served on this socket.
            assert client.recv(1) == b""
    finally:
        harness.server.stop()

    assert not harness.config.socket_path.exists()
    assert len(harness.sink.events) == 1
    assert harness.sink.events[0]["decision"] == "allow"


def test_socket_denials_are_single_ndjson_responses(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    harness.server.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(harness.config.socket_path))
            client.sendall(b'{"extra":1}\n')
            assert json.loads(client.recv(4096).decode("utf-8")) == {
                "ok": False,
                "reason": "bad_request",
            }
            assert client.recv(1) == b""
    finally:
        harness.server.stop()


def test_admission_denies_ninth_connection_with_busy(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    harness.server.start()
    idle: list[socket.socket] = []
    try:
        for _ in range(8):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            client.connect(str(harness.config.socket_path))
            idle.append(client)

        ninth = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ninth.settimeout(5)
        ninth.connect(str(harness.config.socket_path))
        try:
            assert json.loads(ninth.recv(4096).decode("utf-8")) == {
                "ok": False,
                "reason": "busy",
            }
            assert ninth.recv(1) == b""
        finally:
            ninth.close()

        busy_event = harness.sink.events[-1]
        assert busy_event["decision"] == "deny"
        assert busy_event["reason"] == "busy"
        assert busy_event["limit"] == "per_task_connections"
        assert busy_event["peer_uid"] == os.getuid()
        assert "task_id" not in busy_event
    finally:
        for client in idle:
            client.close()
        harness.server.stop()


def test_startup_fails_closed_without_readable_backends(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    with pytest.raises(SecurityError) as raised:
        FileResourceBackend(missing_root)
    assert raised.value.code == "unknown"
    assert not missing_root.exists()

    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("{}", encoding="utf-8")
    with pytest.raises(SecurityError):
        FileResourceBackend(not_a_directory)

    class _UnreadableStore:
        def lookup(self, jti: str) -> None:
            raise SecurityError("unknown", "revocation_store_unreadable")

        def revoke(self, jti: str, *, reason: str, revoked_at: datetime) -> None:
            raise AssertionError("not used")

        def prune(self, now: datetime | None = None) -> int:
            raise AssertionError("not used")

    with pytest.raises(SecurityError) as raised:
        _build(tmp_path / "store", revocation=_UnreadableStore())
    assert raised.value.code == "unknown"
    assert raised.value.detail == "revocation_store_unavailable"
