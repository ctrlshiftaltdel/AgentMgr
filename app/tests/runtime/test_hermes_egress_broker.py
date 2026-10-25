"""Contract tests for the host egress broker (design §9.3).

The broker is the only path from a sandbox to the model provider.  These tests
are DB-free and provider-free: the upstream is a loopback :class:`http.server`
instance, and the broker's Unix socket is only started in the two socket tests.
They pin:

* task-token validation (missing / invalid / expired / revoked / cross-task /
  cross-unit / wrong provider / wrong path) before any upstream byte is sent;
* the fixed method/path allowlist and the fixed client-header allowlist, with the
  real provider key injected and the task token never forwarded;
* request/response size caps, upstream timeouts, refusal to follow redirects and
  stable upstream error mapping with no upstream body echo;
* SSE/chunked streaming (the first chunk must reach the caller before the
  upstream finishes) and client-socket framing/mode;
* "no secret ever reaches a log line or an error body".
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import json
import logging
import socket
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.runtime.hermes_egress import (
    EXIT_CONFIG,
    EXIT_UNAVAILABLE,
    BrokerConfig,
    BrokerConfigError,
    BrokerError,
    BrokerResponse,
    EgressBroker,
    EgressClaims,
    egress_token_hash,
    load_broker_config,
    mint_egress_token,
    run_broker,
)

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
KEY = b"t" * 32
OTHER_KEY = b"z" * 32
REAL_KEY = b"sk-real-provider-key-canary"
PROVIDER_HOST = "provider.example"
UNIT = "agentmgr-hermes-0123456789abcdef"
OTHER_UNIT = "agentmgr-hermes-fedcba9876543210"
TOKEN_HEADER = "X-AgentMgr-Egress-Token"
BODY = b'{"model":"model-x","messages":[{"role":"user","content":"PROMPT-CANARY"}]}'


# ---------------------------------------------------------------------------
# tokens / task binding
# ---------------------------------------------------------------------------


def claims(**overrides: object) -> EgressClaims:
    values: dict[str, object] = {
        "version": 1,
        "task_id": "task_1",
        "grant_id": "grnt_1",
        "unit": UNIT,
        "model": "model-x",
        "provider_host": PROVIDER_HOST,
        "path_prefix": "/v1",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
        "nonce": "n" * 32,
    }
    values.update(overrides)
    return EgressClaims(**values)  # type: ignore[arg-type]


def token_for(**overrides: object) -> str:
    return mint_egress_token(claims(**overrides), key=KEY, now=NOW)


def write_task(
    task_root: Path,
    token: str,
    *,
    unit: str = UNIT,
    task_id: str = "task_1",
    grant_id: str = "grnt_1",
) -> Path:
    """Write the secret-free manifest the helper's ``prepare`` produces."""

    directory = task_root / unit
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "agentmgr.hermes.manifest.v1",
        "unit": unit,
        "task_id": task_id,
        "grant_id": grant_id,
        "egress_token_sha256": egress_token_hash(token),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# loopback upstream
# ---------------------------------------------------------------------------


@dataclass
class UpstreamState:
    requests: list[dict[str, object]]
    status: int = 200
    body: bytes = b'{"ok":true}'
    headers: dict[str, str] | None = None
    stream: bool = False
    stream_chunks: tuple[bytes, ...] = (b"data: one\n\n", b"data: two\n\n")
    wait_seconds: float = 5.0

    def __post_init__(self) -> None:
        self.headers = dict(self.headers or {})
        self.release = threading.Event()


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "test-upstream"
    sys_version = ""

    def log_message(self, _format: str, *args: object) -> None:
        return

    def log_error(self, _format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._handle()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._handle()

    def _handle(self) -> None:
        state: UpstreamState = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        if state.stream:
            self.protocol_version = "HTTP/1.0"
            self.send_response_only(state.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            for index, chunk in enumerate(state.stream_chunks):
                self.wfile.write(chunk)
                self.wfile.flush()
                if index == 0:
                    state.release.wait(state.wait_seconds)
            return
        self.send_response_only(state.status)
        for name, value in state.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(state.body)))
        self.end_headers()
        self.wfile.write(state.body)


class _Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: UpstreamState) -> None:
        self.state = state
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        state.port = int(self.server_address[1])

    def handle_error(self, request: object, client_address: object) -> None:
        return


@contextlib.contextmanager
def upstream(**options: object) -> Iterator[UpstreamState]:
    state = UpstreamState(requests=[], **options)  # type: ignore[arg-type]
    server = _Upstream(state)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5.0)


# ---------------------------------------------------------------------------
# broker harness
# ---------------------------------------------------------------------------


def build_config(
    tmp_path: Path,
    state: UpstreamState,
    *,
    clock: object = None,
    **overrides: object,
) -> BrokerConfig:
    revocation = tmp_path / "revoked"
    revocation.mkdir(parents=True, exist_ok=True)
    task_root = tmp_path / "tasks"
    task_root.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "socket_path": tmp_path / "egress.sock",
        "upstream_base_url": f"http://127.0.0.1:{state.port}",
        "provider_host": PROVIDER_HOST,
        "token_key": KEY,
        "provider_key": REAL_KEY,
        "revocation_dir": revocation,
        "task_root": task_root,
        "allow_insecure_upstream": True,
        "socket_group": None,
        "clock": clock or (lambda: NOW + timedelta(seconds=1)),
    }
    values.update(overrides)
    return BrokerConfig(**values)  # type: ignore[arg-type]


@dataclass
class Harness:
    broker: EgressBroker
    config: BrokerConfig
    state: UpstreamState
    task_root: Path
    revocation_dir: Path


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    with upstream() as state:
        config = build_config(tmp_path, state)
        assert config.task_root is not None
        assert config.revocation_dir is not None
        write_task(config.task_root, token_for())
        yield Harness(
            broker=EgressBroker(config),
            config=config,
            state=state,
            task_root=config.task_root,
            revocation_dir=config.revocation_dir,
        )


def call(
    broker: EgressBroker,
    token: str | None,
    *,
    method: str = "POST",
    path: str = "/v1/chat/completions",
    headers: dict[str, str] | None = None,
    body: bytes = BODY,
) -> BrokerResponse:
    request_headers = dict(headers or {})
    if token is not None:
        request_headers.setdefault(TOKEN_HEADER, token)
    return broker.handle(method, path, request_headers, body)


def error_code(response: BrokerResponse) -> str:
    document = json.loads(response.read_body())
    return str(document["error"])


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_valid_token_injects_real_key_and_forwards_path(harness: Harness) -> None:
    token = token_for()

    response = call(
        harness.broker,
        token,
        headers={
            "Authorization": "Bearer task-token",
            "X-Api-Key": "task-api-key",
            "Content-Type": "application/json",
        },
    )

    assert response.status == 200
    assert response.read_body() == b'{"ok":true}'
    assert len(harness.state.requests) == 1
    request = harness.state.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/chat/completions"
    assert request["body"] == BODY
    forwarded = request["headers"]
    assert isinstance(forwarded, dict)
    assert forwarded["Authorization"] == f"Bearer {REAL_KEY.decode()}"
    assert forwarded["Content-Type"] == "application/json"
    assert TOKEN_HEADER not in forwarded
    assert "X-Api-Key" not in forwarded
    assert REAL_KEY.decode().encode() not in response.read_body()


def test_forwarded_path_keeps_query_and_uses_configured_base_path(
    tmp_path: Path,
) -> None:
    with upstream() as state:
        config = build_config(
            tmp_path,
            state,
            upstream_base_url=f"http://127.0.0.1:{state.port}/openai",
        )
        assert config.task_root is not None
        write_task(config.task_root, token_for())
        response = call(
            EgressBroker(config), token_for(), path="/v1/chat/completions?stream=true"
        )

    assert response.status == 200
    assert state.requests[0]["path"] == "/openai/v1/chat/completions?stream=true"


# ---------------------------------------------------------------------------
# token validation
# ---------------------------------------------------------------------------


def test_missing_token_is_rejected_before_the_upstream(harness: Harness) -> None:
    response = call(harness.broker, None)

    assert response.status == 401
    assert error_code(response) == "token_required"
    assert harness.state.requests == []


@pytest.mark.parametrize(
    "token",
    [
        "not-a-token",
        "egt.a.b",
        mint_egress_token(claims(), key=OTHER_KEY, now=NOW),
    ],
    ids=["garbage", "malformed", "wrong-key"],
)
def test_invalid_tokens_are_rejected_without_echo(harness: Harness, token: str) -> None:
    response = call(harness.broker, token)

    assert response.status == 401
    body = response.read_body()
    assert error_code(response) == "token_invalid"
    assert token.encode() not in body
    assert harness.state.requests == []


def test_expired_and_not_yet_valid_tokens_are_rejected(tmp_path: Path) -> None:
    token = token_for()
    with upstream() as state:
        expired = build_config(
            tmp_path / "expired", state, clock=lambda: NOW + timedelta(seconds=61)
        )
        assert expired.task_root is not None
        write_task(expired.task_root, token)

        early = build_config(
            tmp_path / "early", state, clock=lambda: NOW - timedelta(seconds=1)
        )
        assert early.task_root is not None
        write_task(early.task_root, token)

        expired_response = call(EgressBroker(expired), token)
        early_response = call(EgressBroker(early), token)

    assert expired_response.status == 401
    assert error_code(expired_response) == "token_expired"
    assert early_response.status in {401, 403}
    assert state.requests == []


def test_revoked_token_and_revoked_unit_are_rejected_but_siblings_pass(
    harness: Harness,
) -> None:
    assert call(harness.broker, token_for()).status == 200

    token_hash = egress_token_hash(token_for())
    (harness.revocation_dir / f"token.{token_hash}").write_text("", encoding="utf-8")
    revoked_response = call(harness.broker, token_for())
    assert revoked_response.status == 401
    assert error_code(revoked_response) == "token_revoked"

    (harness.revocation_dir / f"unit.{UNIT}").write_text("", encoding="utf-8")
    assert call(harness.broker, token_for()).status == 401

    # A revocation marker for one unit must not block another task's token.
    other_token = token_for(unit=OTHER_UNIT, task_id="task_2", grant_id="grnt_2")
    write_task(
        harness.task_root, other_token, unit=OTHER_UNIT, task_id="task_2", grant_id="grnt_2"
    )
    assert call(harness.broker, other_token).status == 200


def test_missing_revocation_store_fails_closed(harness: Harness) -> None:
    harness.revocation_dir.rmdir()

    response = call(harness.broker, token_for())

    assert response.status == 503
    assert error_code(response) == "revocation_unavailable"
    assert harness.state.requests == []


def test_token_bound_to_another_task_or_unit_is_rejected(harness: Harness) -> None:
    # The manifest says task_1 but the (validly signed) token claims task_2:
    # a cross-task token cannot ride another task's manifest.
    cross_task = token_for(task_id="task_2")
    cross_task_response = call(harness.broker, cross_task)

    assert cross_task_response.status == 403
    assert error_code(cross_task_response) == "token_binding_mismatch"

    # A token whose unit has no prepared task directory (cross-unit replay).
    cross_unit = token_for(unit=OTHER_UNIT)
    cross_unit_response = call(harness.broker, cross_unit)

    assert cross_unit_response.status == 403
    assert error_code(cross_unit_response) == "token_binding_mismatch"
    assert harness.state.requests == []


def test_cleaned_up_task_directory_revokes_the_token(harness: Harness) -> None:
    token = token_for()
    assert call(harness.broker, token).status == 200

    for child in (harness.task_root / UNIT).iterdir():
        child.unlink()
    (harness.task_root / UNIT).rmdir()

    response = call(harness.broker, token)
    assert response.status == 403
    assert error_code(response) == "token_binding_mismatch"


def test_wrong_provider_or_path_claims_are_rejected(harness: Harness) -> None:
    wrong_provider = token_for(provider_host="other.example")
    wrong_prefix = token_for(path_prefix="/v2")

    provider_response = call(harness.broker, wrong_provider)
    prefix_response = call(harness.broker, wrong_prefix)

    assert provider_response.status == 403
    assert error_code(provider_response) == "target_not_allowed"
    assert prefix_response.status == 403
    assert error_code(prefix_response) == "path_not_allowed"
    assert harness.state.requests == []


def test_token_for_another_model_is_accepted_for_the_path(harness: Harness) -> None:
    # The broker never parses the body; model selection is a profile fact.  This
    # pins that a prompt-bearing body is forwarded opaquely and never logged.
    other_body = b'{"model":"another-model","messages":[{"role":"user","content":"PROMPT-CANARY"}]}'
    assert call(harness.broker, token_for(), body=other_body).status == 200
    assert harness.state.requests[0]["body"] == other_body


# ---------------------------------------------------------------------------
# allowlist and target pinning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/v1/chat/completions", 405, "method_not_allowed"),
        ("POST", "/v1/models", 405, "method_not_allowed"),
        ("DELETE", "/v1/chat/completions", 405, "method_not_allowed"),
        ("HEAD", "/v1/chat/completions", 405, "method_not_allowed"),
        ("POST", "/admin", 404, "path_not_allowed"),
        ("POST", "/v1/chat/completions/../../admin", 404, "path_not_allowed"),
        ("POST", "http://evil.example/v1/chat/completions", 404, "path_not_allowed"),
    ],
    ids=[
        "get-on-post-route",
        "post-on-get-route",
        "delete",
        "head",
        "unknown-path",
        "traversal",
        "absolute-form",
    ],
)
def test_method_and_path_allowlist(
    harness: Harness, method: str, path: str, status: int, code: str
) -> None:
    response = call(harness.broker, token_for(), method=method, path=path)

    assert response.status == status
    assert error_code(response) == code
    assert harness.state.requests == []


def test_get_models_is_forwarded_without_a_body(harness: Harness) -> None:
    response = call(
        harness.broker, token_for(), method="GET", path="/v1/models", body=b""
    )

    assert response.status == 200
    assert harness.state.requests[0]["method"] == "GET"
    assert harness.state.requests[0]["body"] == b""


def test_oversized_request_is_rejected_before_the_upstream(tmp_path: Path) -> None:
    with upstream() as state:
        config = build_config(tmp_path, state, max_request_bytes=32)
        assert config.task_root is not None
        write_task(config.task_root, token_for())
        response = call(EgressBroker(config), token_for(), body=b"x" * 33)

    assert response.status == 413
    assert error_code(response) == "request_too_large"
    assert state.requests == []


# ---------------------------------------------------------------------------
# upstream behaviour
# ---------------------------------------------------------------------------


def test_redirects_are_refused_and_never_followed(harness: Harness) -> None:
    harness.state.status = 302
    harness.state.headers = {"Location": "http://127.0.0.1:1/steal"}
    harness.state.body = b""

    response = call(harness.broker, token_for())

    assert response.status == 502
    assert error_code(response) == "upstream_redirect_refused"
    assert all(name != "Location" for name, _ in response.headers)
    assert len(harness.state.requests) == 1


@pytest.mark.parametrize(
    ("upstream_status", "status", "code"),
    [
        (401, 502, "upstream_auth_failed"),
        (403, 502, "upstream_auth_failed"),
        (429, 429, "upstream_rate_limited"),
        (500, 502, "upstream_error"),
        (503, 503, "upstream_unavailable"),
    ],
)
def test_upstream_failures_map_to_stable_codes_without_echo(
    harness: Harness, upstream_status: int, status: int, code: str
) -> None:
    harness.state.status = upstream_status
    harness.state.body = b'{"error":"upstream leaks real key ' + REAL_KEY + b'"}'
    harness.state.headers = {"Retry-After": "7"} if upstream_status == 429 else {}

    response = call(harness.broker, token_for())
    body = response.read_body()

    assert response.status == status
    assert json.loads(body)["error"] == code
    assert REAL_KEY not in body
    assert b"upstream leaks" not in body
    if upstream_status == 429:
        assert dict(response.headers)["Retry-After"] == "7"


def test_declared_oversized_response_is_refused_without_reading(tmp_path: Path) -> None:
    with upstream(body=b"y" * 4096) as state:
        config = build_config(tmp_path, state, max_response_bytes=1024)
        assert config.task_root is not None
        write_task(config.task_root, token_for())
        response = call(EgressBroker(config), token_for())

    assert response.status == 502
    assert error_code(response) == "response_too_large"
    assert b"y" not in response.read_body()


def test_streamed_body_is_cut_at_the_response_cap(tmp_path: Path) -> None:
    with upstream(
        stream=True, stream_chunks=(b"x" * 2048, b"tail"), wait_seconds=2.0
    ) as state:
        config = build_config(tmp_path, state, max_response_bytes=1024)
        assert config.task_root is not None
        write_task(config.task_root, token_for())
        response = call(EgressBroker(config), token_for())
        state.release.set()
        body = response.read_body()

    assert response.status == 200
    assert len(body) == 1024
    assert body == b"x" * 1024


def test_sse_stream_is_forwarded_before_the_upstream_finishes(harness: Harness) -> None:
    harness.state.stream = True
    response = call(harness.broker, token_for())

    assert response.status == 200
    assert dict(response.headers)["Content-Type"] == "text/event-stream"
    assert not isinstance(response.body, bytes), "an SSE body must not be buffered"
    chunks = response.iter_body()
    try:
        first = next(chunks)
        assert b"data: one" in first
        harness.state.release.set()
        rest = b"".join(chunks)
    finally:
        response.close()

    assert b"data: two" in rest
    assert harness.state.release.is_set()


def test_upstream_timeout_is_stable(tmp_path: Path) -> None:
    with upstream(stream=True, wait_seconds=1.0) as state:
        config = build_config(tmp_path, state, upstream_timeout_sec=0.2)
        assert config.task_root is not None
        write_task(config.task_root, token_for())
        broker = EgressBroker(config)
        response = call(broker, token_for())
        # Headers arrived; the body stalls past the read timeout, so the stream
        # ends without inventing content and without leaking an exception.
        timed_out = b"".join(response.iter_body())
        state.release.set()

    assert timed_out in {b"", b"data: one\n\n"}


# ---------------------------------------------------------------------------
# secrets in logs
# ---------------------------------------------------------------------------


def test_neither_key_nor_token_nor_prompt_reaches_the_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token = token_for()

    assert call(harness.broker, token).status == 200
    assert call(harness.broker, "bogus-token").status == 401

    harness.state.status = 500
    harness.state.body = b'{"error":"secret body"}'
    assert call(harness.broker, token).status == 502

    logged = caplog.text
    assert "egress" in logged, "the denial/allow path must actually be logged"
    assert token not in logged
    assert REAL_KEY.decode() not in logged
    assert "PROMPT-CANARY" not in logged
    assert "secret body" not in logged


# ---------------------------------------------------------------------------
# Unix socket
# ---------------------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, *, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self._path)
        except BaseException:
            connection.close()
            raise
        self.sock = connection


def test_socket_is_unix_only_mode_0660_and_serves_the_broker(
    tmp_path: Path,
) -> None:
    with upstream() as state:
        config = build_config(tmp_path, state)
        assert config.task_root is not None
        write_task(config.task_root, token_for())
        broker = EgressBroker(config)
        broker.start()
        try:
            socket_path = config.socket_path
            info = socket_path.stat()
            assert stat.S_ISSOCK(info.st_mode)
            assert stat.S_IMODE(info.st_mode) == 0o660

            connection = _UnixConnection(str(socket_path))
            try:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    body=BODY,
                    headers={
                        TOKEN_HEADER: token_for(),
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                payload = response.read()
                assert response.status == 200
                assert payload == b'{"ok":true}'
            finally:
                connection.close()
        finally:
            broker.stop()

        assert not config.socket_path.exists()

    forwarded = state.requests[0]["headers"]
    assert isinstance(forwarded, dict)
    assert forwarded["Authorization"] == f"Bearer {REAL_KEY.decode()}"


def test_start_refuses_to_clobber_an_existing_path(tmp_path: Path) -> None:
    with upstream() as state:
        config = build_config(tmp_path, state)
        config.socket_path.write_text("occupied", encoding="utf-8")
        broker = EgressBroker(config)

        with pytest.raises(BrokerError) as excinfo:
            broker.start()

        assert config.socket_path.read_text(encoding="utf-8") == "occupied"
        assert REAL_KEY.decode() not in str(excinfo.value)


# ---------------------------------------------------------------------------
# configuration loading
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, **overrides: object) -> tuple[Path, Path, Path]:
    token_key = tmp_path / "token.key"
    token_key.write_bytes(KEY)
    token_key.chmod(0o600)
    provider_key = tmp_path / "provider.key"
    provider_key.write_bytes(REAL_KEY + b"\n")
    provider_key.chmod(0o600)
    revocation = tmp_path / "revoked"
    revocation.mkdir(exist_ok=True)
    task_root = tmp_path / "tasks"
    task_root.mkdir(exist_ok=True)
    document: dict[str, object] = {
        "socket_path": str(tmp_path / "egress.sock"),
        "upstream_base_url": "https://provider.example",
        "provider_host": PROVIDER_HOST,
        "token_key_path": str(token_key),
        "provider_key_path": str(provider_key),
        "revocation_dir": str(revocation),
        "task_root": str(task_root),
    }
    document.update(overrides)
    config_path = tmp_path / "broker.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path, token_key, provider_key


def test_config_loader_reads_secret_files_and_redacts_them(tmp_path: Path) -> None:
    config_path, token_key, provider_key = _write_config(tmp_path)

    config = load_broker_config(config_path)

    assert config.token_key == KEY
    assert config.provider_key == REAL_KEY
    text = repr(config)
    assert REAL_KEY.decode() not in text
    assert KEY.decode() not in text
    assert str(token_key) not in text
    assert str(provider_key) not in text
    assert config.socket_mode == 0o660


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_key": "inline-secret"},
        {"token_key": "inline-secret"},
        {"socket_path": "relative.sock"},
        {"upstream_base_url": "http://provider.example"},
        {"unknown_key": True},
    ],
    ids=["inline-provider-key", "inline-token-key", "relative-socket", "plain-http", "unknown-key"],
)
def test_config_loader_rejects_unsafe_configuration(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    config_path, _, _ = _write_config(tmp_path, **overrides)

    with pytest.raises(BrokerConfigError):
        load_broker_config(config_path)


def test_config_loader_rejects_a_group_readable_secret(tmp_path: Path) -> None:
    config_path, _, provider_key = _write_config(tmp_path)
    provider_key.chmod(0o644)

    with pytest.raises(BrokerConfigError):
        load_broker_config(config_path)


def test_run_broker_fails_closed_without_a_task_binding(tmp_path: Path) -> None:
    with upstream() as state:
        config = build_config(tmp_path, state, task_root=None)

        exit_code = run_broker(config)

    assert exit_code == EXIT_CONFIG
    assert not config.socket_path.exists()


def test_run_broker_refuses_a_missing_revocation_store(tmp_path: Path) -> None:
    with upstream() as state:
        config = build_config(tmp_path, state)
        config.revocation_dir.rmdir()

        exit_code = run_broker(config)

    assert exit_code == EXIT_UNAVAILABLE
    assert not config.socket_path.exists()
