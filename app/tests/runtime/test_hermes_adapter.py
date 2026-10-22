"""Mock-server contract tests for :class:`HermesRuntimeAdapter`.

These tests prove the adapter/contract mapping only.  They are explicitly not
runtime compatibility evidence; the real-runtime evidence lives in
``test_hermes_integration.py`` and
``docs/research/runtime-compatibility/hermes-0.22.1.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.runtime.contract import (
    CancelOutcome,
    CapabilityStatus,
    ErrorCode,
    HealthStatus,
    InputOutcome,
    ResultOutcome,
    RuntimeCapability,
    RuntimeState,
    SubmitOutcome,
    VersionSource,
)
from app.runtime.errors import (
    RuntimeAuthError,
    RuntimeConflict,
    RuntimeNotFound,
    RuntimeProtocolError,
    RuntimeRateLimited,
    RuntimeTimeout,
    RuntimeUnavailable,
    RuntimeUnknown,
)
from app.runtime.hermes import HermesConfig, HermesRuntimeAdapter
from app.runtime.http import HttpResponse

# Placeholder only; this value is not a real credential and is never used by a
# real runtime in this module.
TEST_KEY = "fake-api-key-0123456789abcdef"
WRONG_KEY = "wrong-api-key-0123456789"


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _payload_hash(body: object) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _default_events() -> list[tuple[str, str, str]]:
    return [
        ("1", "message.delta", '{"type":"message.delta","text":"O"}'),
        (
            "2",
            "tool",
            '{"type":"tool","name":"terminal","input":{"cmd":"echo hi"},'
            '"api_key":"must-not-leak","authorization":"Bearer must-not-leak"}',
        ),
        (
            "3",
            "run.completed",
            '{"type":"run.completed","output":"OK","usage":{"input_tokens":3},'
            '"occurred_at":"2026-01-01T00:00:00Z"}',
        ),
        ("4", "message.delta", '{"type":"message.delta",\n"text":"K"}'),
    ]


class MockHermes:
    """Tiny stateful Hermes API double used only by these tests."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.key = TEST_KEY
        self.api_url = ""
        self.runs: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[str, tuple[str, str]] = {}
        self.event_scripts: dict[str, list[tuple[str, str, str]]] = {}
        self.requests: list[dict[str, Any]] = []
        self.counter = 0

        self.health_status: int | None = 200
        self.capabilities_status: int | None = 200
        self.submit_status: int | None = None
        self.status_status: int | None = None
        self.events_status: int | None = None
        self.stop_status: int | None = None
        self.approval_status: int | None = None
        self.stop_polls = 1

    def create_run(
        self,
        *,
        fixed_status: str | None = None,
        sequence: list[str] | None = None,
        output: str = "OK",
        usage: dict[str, int] | None = None,
        session_id: str = "session-test",
        events: list[tuple[str, str, str]] | None = None,
    ) -> str:
        with self.lock:
            self.counter += 1
            run_id = f"run_{self.counter:04d}"
            self.runs[run_id] = {
                "run_id": run_id,
                "fixed_status": fixed_status,
                "sequence": (
                    None
                    if fixed_status is not None
                    else (sequence or ["queued", "running", "completed"])
                ),
                "status": "queued",
                "stopping_polls": None,
                "output": output,
                "partial_output": None,
                "usage": usage
                or {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                "runtime": {"name": "hermes", "version": "0.22.1+72.g450ad50"},
                "session_id": session_id,
                "body": None,
                "payload_hash": None,
            }
            if events is not None:
                self.event_scripts[run_id] = list(events)
            return run_id

    def current_status(self, run: dict[str, Any]) -> str:
        fixed = run.get("fixed_status")
        if fixed:
            return str(fixed)
        polls = run.get("stopping_polls")
        if polls is not None:
            if polls < 0:
                return "stopping"
            if polls == 0:
                run["fixed_status"] = "cancelled"
                return "cancelled"
            run["stopping_polls"] = polls - 1
            return "stopping"
        sequence = run.get("sequence")
        if sequence:
            status = sequence[0]
            if len(sequence) > 1:
                sequence.pop(0)
            return str(status)
        return str(run.get("status") or "unknown")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def mock(self) -> MockHermes:
        return self.server.mock  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Keep test output clean; never log headers or bodies here either.
        return

    def _send_json(self, status: int, payload: object) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> object | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _require_auth(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.mock.key}":
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def _record(self, body: object | None) -> None:
        with self.mock.lock:
            self.mock.requests.append(
                {
                    "method": self.command,
                    "path": urlsplit(self.path).path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")

        if path == "/health":
            if self.mock.health_status is not None and self.mock.health_status != 200:
                self._send_json(self.mock.health_status, {"error": "injected"})
            else:
                self._send_json(200, {"status": "ok"})
            return

        if parts == ["v1", "capabilities"]:
            if not self._require_auth():
                return
            if (
                self.mock.capabilities_status is not None
                and self.mock.capabilities_status != 200
            ):
                self._send_json(self.mock.capabilities_status, {"error": "injected"})
                return
            self._send_json(
                200,
                {
                    "runtime": {
                        "name": "hermes",
                        "version": "0.22.1+72.g450ad50",
                    },
                    "runtime_mode": "server_agent",
                    "tool_execution": "server",
                    "split_runtime": False,
                },
            )
            return

        if len(parts) == 3 and parts[:2] == ["v1", "runs"]:
            if not self._require_auth():
                return
            run_id = parts[2]
            with self.mock.lock:
                run = self.mock.runs.get(run_id)
                if run is None:
                    self._send_json(404, {"error": "not_found"})
                    return
                if self.mock.status_status is not None and self.mock.status_status != 200:
                    self._send_json(self.mock.status_status, {"error": "injected"})
                    return
                status = self.mock.current_status(run)
                response = {
                    "run_id": run_id,
                    "status": status,
                    "output": run["output"] if status == "completed" else None,
                    "usage": run["usage"],
                    "runtime": run["runtime"],
                    "session_id": run["session_id"],
                    "heartbeat_at": "2026-01-01T00:00:00Z",
                    "pending_controls": ["approval"],
                    "admission_id": "adm_internal",
                    "execution_generation": 7,
                }
            self._send_json(200, response)
            return

        if len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "events":
            if not self._require_auth():
                return
            run_id = parts[2]
            with self.mock.lock:
                if run_id not in self.mock.runs:
                    self._send_json(404, {"error": "not_found"})
                    return
                if self.mock.events_status is not None and self.mock.events_status != 200:
                    self._send_json(self.mock.events_status, {"error": "injected"})
                    return
                script = self.mock.event_scripts.get(run_id, _default_events())

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                for event_id, event_type, data in script:
                    lines = [f"id: {event_id}", f"event: {event_type}"]
                    lines.extend(f"data: {line}" for line in data.split("\n"))
                    self.wfile.write(("\n".join(lines) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        body = self._read_json()
        self._record(body)
        if body is None:
            self._send_json(400, {"error": "invalid_json"})
            return

        if parts == ["v1", "runs"]:
            if not self._require_auth():
                return
            if self.mock.submit_status is not None and self.mock.submit_status != 200:
                self._send_json(self.mock.submit_status, {"error": "injected"})
                return
            idempotency_key = self.headers.get("Idempotency-Key")
            if not idempotency_key:
                self._send_json(400, {"error": "idempotency_key_required"})
                return
            payload_hash = _payload_hash(body)
            with self.mock.lock:
                existing = self.mock.idempotency.get(idempotency_key)
                if existing is not None:
                    old_hash, old_run_id = existing
                    if old_hash != payload_hash:
                        self._send_json(409, {"error": "idempotency_conflict"})
                        return
                    self._send_json(
                        202,
                        {
                            "run_id": old_run_id,
                            "status": "started",
                            "replayed": True,
                        },
                    )
                    return
                run_id = self.mock.create_run(
                    session_id=str(body.get("session_id") or "session-test")
                )
                self.mock.runs[run_id]["body"] = body
                self.mock.runs[run_id]["payload_hash"] = payload_hash
                self.mock.idempotency[idempotency_key] = (payload_hash, run_id)
            self._send_json(
                202,
                {"run_id": run_id, "status": "started", "replayed": False},
            )
            return

        if len(parts) == 4 and parts[:2] == ["v1", "runs"]:
            if not self._require_auth():
                return
            run_id = parts[2]
            action = parts[3]
            with self.mock.lock:
                run = self.mock.runs.get(run_id)
                if run is None:
                    self._send_json(404, {"error": "not_found"})
                    return
                if action == "stop":
                    if self.mock.stop_status is not None and self.mock.stop_status != 200:
                        self._send_json(self.mock.stop_status, {"error": "injected"})
                        return
                    fixed = run.get("fixed_status")
                    if fixed in {"completed", "failed", "cancelled"}:
                        self._send_json(200, {"run_id": run_id, "status": fixed})
                        return
                    run["sequence"] = None
                    run["stopping_polls"] = self.mock.stop_polls
                    self._send_json(
                        200,
                        {"run_id": run_id, "status": "stopping"},
                    )
                    return
                if action == "approval":
                    if (
                        self.mock.approval_status is not None
                        and self.mock.approval_status != 200
                    ):
                        self._send_json(self.mock.approval_status, {"error": "injected"})
                        return
                    self._send_json(200, {"run_id": run_id, "status": "running"})
                    return
                if action == "resolve-unknown":
                    self._send_json(200, {"run_id": run_id, "status": "running"})
                    return

        self._send_json(404, {"error": "not_found"})


@pytest.fixture()
def mock_server() -> Any:
    mock = MockHermes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.mock = mock  # type: ignore[attr-defined]
    mock.api_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield mock
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _config(api_url: str, *, key: str = TEST_KEY, attempts: int = 2) -> HermesConfig:
    return HermesConfig(
        api_url=api_url,
        api_key=key,
        request_timeout=2.0,
        health_timeout=2.0,
        cancel_poll_timeout=1.0,
        cancel_poll_interval=0.01,
        submit_attempts=attempts,
    )


@pytest.fixture()
def adapter(mock_server: MockHermes) -> HermesRuntimeAdapter:
    return HermesRuntimeAdapter(_config(mock_server.api_url))


class _RecordingTransport:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
            }
        )
        raise self.error

    def stream(self, url, *, headers, timeout):
        self.calls.append(
            {"method": "GET", "url": url, "headers": dict(headers), "body": None}
        )
        raise self.error


class _StaticTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0

    def request(self, method, url, *, headers, body, timeout):
        self.calls += 1
        return self.response

    def stream(self, url, *, headers, timeout):
        raise RuntimeTimeout("not used")


class _StreamTimeoutTransport:
    def request(self, method, url, *, headers, body, timeout):
        raise RuntimeTimeout("not used")

    def stream(self, url, *, headers, timeout):
        raise RuntimeTimeout("runtime event stream timed out")


# --------------------------------------------------------------------------- #
# Configuration and secret hygiene
# --------------------------------------------------------------------------- #


def test_config_from_env_fails_closed() -> None:
    with pytest.raises(RuntimeUnavailable):
        HermesConfig.from_env({})
    with pytest.raises(RuntimeUnavailable):
        HermesConfig.from_env(
            {
                "HERMES_API_URL": "http://127.0.0.1:8080",
                "HERMES_API_KEY": "short",
            }
        )

    config = HermesConfig.from_env(
        {
            "HERMES_API_URL": "http://127.0.0.1:8080/",
            "HERMES_API_KEY": TEST_KEY,
        }
    )
    assert config.api_url == "http://127.0.0.1:8080"


def test_config_rejects_embedded_credentials() -> None:
    with pytest.raises(RuntimeUnavailable):
        HermesConfig(api_url="http://user:pass@localhost:8080", api_key=TEST_KEY)


def test_repr_and_str_never_contain_the_api_key(adapter: HermesRuntimeAdapter) -> None:
    assert TEST_KEY not in repr(adapter)
    assert TEST_KEY not in str(adapter)
    assert TEST_KEY not in repr(adapter.config)
    assert TEST_KEY not in str(adapter.config)
    assert "<redacted>" in repr(adapter)


def test_logs_never_contain_the_api_key(
    adapter: HermesRuntimeAdapter,
    mock_server: MockHermes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter.capabilities()
    adapter.health()
    adapter.submit_task("task_log", "hello", "idem-log")
    assert TEST_KEY not in caplog.text


# --------------------------------------------------------------------------- #
# Capabilities and health
# --------------------------------------------------------------------------- #


def test_static_capabilities_are_labelled(adapter: HermesRuntimeAdapter) -> None:
    snapshot = adapter.capabilities()
    assert snapshot.adapter_contract_version == "1.0.0"
    assert snapshot.boundary == "b3_runtime_adapter"
    assert snapshot.runtime_version is None
    assert snapshot.version_source is VersionSource.UNKNOWN

    names = [item.name for item in snapshot.capabilities]
    assert len(names) == len(set(names))
    assert snapshot.capability(RuntimeCapability.TASK_SUBMIT).status is (
        CapabilityStatus.VERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_STATUS).status is (
        CapabilityStatus.VERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_EVENTS_POLL).status is (
        CapabilityStatus.VERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_CANCEL).status is (
        CapabilityStatus.VERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_RESULT).status is (
        CapabilityStatus.VERIFIED
    )
    assert snapshot.capability(RuntimeCapability.RUNTIME_HEALTH).status is (
        CapabilityStatus.VERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_INPUT_PROVIDE).status is (
        CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability(RuntimeCapability.RUNTIME_ISOLATION).status is (
        CapabilityStatus.PARTIALLY_VERIFIED
    )
    assert snapshot.capability(
        RuntimeCapability.RUNTIME_TASK_SCOPED_CREDENTIALS
    ).status is CapabilityStatus.UNVERIFIED
    assert snapshot.capability(RuntimeCapability.RUNTIME_MANAGED_EXECUTION).status is (
        CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_APPROVAL_RESOLVE).status is (
        CapabilityStatus.PARTIALLY_VERIFIED
    )


def test_capabilities_probe_reads_runtime_version(
    adapter: HermesRuntimeAdapter,
) -> None:
    snapshot = adapter.capabilities(probe=True)
    assert snapshot.runtime_version == "0.22.1+72.g450ad50"
    assert snapshot.version_source is VersionSource.RUNTIME_SELF_REPORTED


def test_capabilities_probe_auth_failure(mock_server: MockHermes) -> None:
    adapter = HermesRuntimeAdapter(_config(mock_server.api_url, key=WRONG_KEY))
    with pytest.raises(RuntimeAuthError):
        adapter.capabilities(probe=True)


def test_health_healthy(adapter: HermesRuntimeAdapter) -> None:
    result = adapter.health()
    assert result.status is HealthStatus.HEALTHY
    assert result.details["api_ok"] is True
    assert result.details["auth_ok"] is True


def test_health_auth_failure_is_unavailable(
    mock_server: MockHermes,
) -> None:
    adapter = HermesRuntimeAdapter(_config(mock_server.api_url, key=WRONG_KEY))
    result = adapter.health()
    assert result.status is HealthStatus.UNAVAILABLE
    assert result.details["auth_ok"] is False
    assert TEST_KEY not in json.dumps(result.details, default=str)
    assert WRONG_KEY not in json.dumps(result.details, default=str)


def test_health_503_is_unavailable(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    mock_server.health_status = 503
    assert adapter.health().status is HealthStatus.UNAVAILABLE


def test_health_429_is_degraded(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    mock_server.capabilities_status = 429
    result = adapter.health()
    assert result.status is HealthStatus.DEGRADED
    assert result.details["reason"] == "rate_limited"


# --------------------------------------------------------------------------- #
# Submit and idempotency
# --------------------------------------------------------------------------- #


def test_submit_accepted_and_body(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    result = adapter.submit_task(
        "task_abc",
        "hello",
        "idem-1",
        instructions="be brief",
    )
    assert result.outcome is SubmitOutcome.ACCEPTED
    assert result.runtime_task_id
    assert result.reported_state == "started"
    assert result.replayed is False

    run = mock_server.runs[result.runtime_task_id]
    assert run["body"] == {
        "input": "hello",
        "session_id": "task_abc",
        "instructions": "be brief",
    }
    request = next(
        item
        for item in mock_server.requests
        if item["method"] == "POST" and item["path"] == "/v1/runs"
    )
    assert request["headers"]["Idempotency-Key"] == "idem-1"
    assert request["headers"]["Authorization"] == f"Bearer {TEST_KEY}"


def test_submit_idempotent_replay_and_conflict(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    first = adapter.submit_task("task_1", "same", "idem-same")
    second = adapter.submit_task("task_1", "same", "idem-same")
    assert first.runtime_task_id == second.runtime_task_id
    assert second.replayed is True
    assert len(mock_server.runs) == 1

    with pytest.raises(RuntimeConflict):
        adapter.submit_task("task_1", "different", "idem-same")
    assert len(mock_server.runs) == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, RuntimeAuthError),
        (403, RuntimeAuthError),
        (429, RuntimeRateLimited),
        (503, RuntimeUnavailable),
    ],
)
def test_submit_http_errors_are_mapped(
    adapter: HermesRuntimeAdapter,
    mock_server: MockHermes,
    status: int,
    error_type: type[Exception],
) -> None:
    mock_server.submit_status = status
    with pytest.raises(error_type):
        adapter.submit_task("task_1", "hello", "idem-error")


def test_submit_timeout_retries_same_key_and_returns_timeout(
    mock_server: MockHermes,
) -> None:
    transport = _RecordingTransport(RuntimeTimeout("timed out"))
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url, attempts=2),
        transport=transport,
    )
    result = adapter.submit_task("task_1", "hello", "idem-timeout")
    assert result.outcome is SubmitOutcome.TIMEOUT
    assert result.runtime_task_id is None
    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT
    assert len(transport.calls) == 2
    assert all(
        call["headers"]["Idempotency-Key"] == "idem-timeout"
        for call in transport.calls
    )


def test_submit_unknown_returns_unknown(mock_server: MockHermes) -> None:
    transport = _RecordingTransport(RuntimeUnknown("connection reset"))
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url, attempts=2),
        transport=transport,
    )
    result = adapter.submit_task("task_1", "hello", "idem-unknown")
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN


def test_submit_transport_unavailable_retries_then_unknown(
    mock_server: MockHermes,
) -> None:
    transport = _RecordingTransport(RuntimeUnavailable("connection refused"))
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url, attempts=2),
        transport=transport,
    )
    result = adapter.submit_task("task_1", "hello", "idem-unavailable")
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert len(transport.calls) == 2


def test_submit_malformed_json_is_a_protocol_error(
    mock_server: MockHermes,
) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StaticTransport(HttpResponse(202, body=b"{not json")),
    )
    with pytest.raises(RuntimeProtocolError):
        adapter.submit_task("task_1", "hello", "idem-bad")


def test_submit_missing_run_id_is_a_protocol_error(
    mock_server: MockHermes,
) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StaticTransport(HttpResponse(202, body=b"{}")),
    )
    with pytest.raises(RuntimeProtocolError):
        adapter.submit_task("task_1", "hello", "idem-missing")


# --------------------------------------------------------------------------- #
# Status mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected", "stop_confirmed", "input_required"),
    [
        ("queued", RuntimeState.ACCEPTED, False, False),
        ("running", RuntimeState.RUNNING, False, False),
        ("waiting_for_approval", RuntimeState.WAITING_INPUT, False, True),
        ("stopping", RuntimeState.RUNNING, False, False),
        ("completed", RuntimeState.COMPLETED, True, False),
        ("failed", RuntimeState.FAILED, True, False),
        ("cancelled", RuntimeState.STOPPED, True, False),
        ("interrupted", RuntimeState.UNKNOWN, False, False),
        ("unknown", RuntimeState.UNKNOWN, False, False),
        ("not-a-hermes-status", RuntimeState.UNKNOWN, False, False),
    ],
)
def test_status_mapping(
    adapter: HermesRuntimeAdapter,
    mock_server: MockHermes,
    raw: str,
    expected: RuntimeState,
    stop_confirmed: bool,
    input_required: bool,
) -> None:
    run_id = mock_server.create_run(fixed_status=raw)
    result = adapter.get_status(run_id)
    assert result.state is expected
    assert result.external_state == raw
    assert result.stop_confirmed is stop_confirmed
    assert result.input_required is input_required
    assert result.runtime == "hermes"
    assert result.session_id == "session-test"
    if expected is RuntimeState.UNKNOWN:
        assert result.error is not None
        assert result.error.code is ErrorCode.UNKNOWN
    else:
        assert result.error is None


def test_status_404_is_not_found(adapter: HermesRuntimeAdapter) -> None:
    result = adapter.get_status("run_missing")
    assert result.state is RuntimeState.NOT_FOUND
    assert result.external_state == "not_found"
    assert result.stop_confirmed is False
    assert result.error is not None
    assert result.error.code is ErrorCode.NOT_FOUND


def test_status_does_not_expose_internal_fields(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="completed")
    result = adapter.get_status(run_id)
    assert not hasattr(result, "pending_controls")
    assert not hasattr(result, "admission_id")
    assert not hasattr(result, "execution_generation")
    assert result.output == "OK"
    assert result.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def test_events_cursor_keepalive_and_redaction(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(events=_default_events())
    first = adapter.poll_events(run_id, max_events=2)
    assert [event.event_id for event in first.events] == ["1", "2"]
    assert first.next_cursor == "2"
    assert first.has_more is True
    assert first.error is None
    assert first.events[1].payload["api_key"] == "[redacted]"
    assert first.events[1].payload["authorization"] == "[redacted]"
    assert first.events[1].payload["name"] == "terminal"

    second = adapter.poll_events(run_id, cursor=first.next_cursor, max_events=10)
    assert [event.event_id for event in second.events] == ["3", "4"]
    assert second.next_cursor == "4"
    assert second.has_more is False
    assert second.events[0].type == "run.completed"
    assert second.events[0].occurred_at.year == 2026
    assert second.events[1].type == "message.delta"
    assert second.events[1].payload["text"] == "K"

    # Replaying the same cursor is stable: events 3 and 4 again, never 1/2.
    replay = adapter.poll_events(run_id, cursor="2", max_events=10)
    assert [event.event_id for event in replay.events] == ["3", "4"]


def test_events_missing_run_raises_not_found(adapter: HermesRuntimeAdapter) -> None:
    with pytest.raises(RuntimeNotFound):
        adapter.poll_events("run_missing")


def test_events_timeout_returns_empty_batch_with_error(
    mock_server: MockHermes,
) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StreamTimeoutTransport(),
    )
    batch = adapter.poll_events("run_1", timeout=0.01)
    assert batch.events == ()
    assert batch.next_cursor is None
    assert batch.error is not None
    assert batch.error.code is ErrorCode.TIMEOUT


# --------------------------------------------------------------------------- #
# Cancel semantics
# --------------------------------------------------------------------------- #


def test_cancel_waits_for_cancelled(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    mock_server.stop_polls = 1
    result = adapter.cancel_task(run_id, poll_timeout=1.0, poll_interval=0.01)
    assert result.outcome is CancelOutcome.STOPPED
    assert result.stop_confirmed is True
    assert result.error is None


def test_cancel_never_claims_stopped_before_terminal(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    mock_server.stop_polls = -1
    result = adapter.cancel_task(run_id, poll_timeout=0.05, poll_interval=0.01)
    assert result.outcome is CancelOutcome.REQUESTED
    assert result.stop_confirmed is False
    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT


def test_cancel_unsupported_is_distinguished(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    mock_server.stop_status = 501
    result = adapter.cancel_task(run_id, poll_timeout=0.01, poll_interval=0.0)
    assert result.outcome is CancelOutcome.UNSUPPORTED
    assert result.stop_confirmed is False
    assert result.error is not None
    assert result.error.code is ErrorCode.UNSUPPORTED


def test_cancel_already_terminal(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="completed")
    result = adapter.cancel_task(run_id, poll_timeout=0.01, poll_interval=0.0)
    assert result.outcome is CancelOutcome.ALREADY_TERMINAL
    assert result.stop_confirmed is True


def test_cancel_missing_run_raises(adapter: HermesRuntimeAdapter) -> None:
    with pytest.raises(RuntimeNotFound):
        adapter.cancel_task("run_missing")


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


def test_result_completed_is_text_only(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="completed")
    result = adapter.get_result(run_id)
    assert result.outcome is ResultOutcome.SUCCEEDED
    assert result.structured_result is False
    assert result.structured_payload is None
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert result.summary == "OK"
    assert result.output == "OK"
    assert result.usage is not None
    assert result.runtime == "hermes"
    assert result.stop_confirmed is True
    assert result.result_ref == f"runtime-result:{run_id}"


def test_result_running_is_not_available(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="running")
    result = adapter.get_result(run_id)
    assert result.outcome is ResultOutcome.UNKNOWN
    assert result.result_ref is None
    assert result.stop_confirmed is False
    assert result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN


def test_result_failed_and_cancelled_are_terminal(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    failed = adapter.get_result(mock_server.create_run(fixed_status="failed"))
    cancelled = adapter.get_result(mock_server.create_run(fixed_status="cancelled"))
    assert failed.outcome is ResultOutcome.FAILED
    assert failed.stop_confirmed is True
    assert cancelled.outcome is ResultOutcome.CANCELLED
    assert cancelled.stop_confirmed is True


def test_result_missing_run_raises(adapter: HermesRuntimeAdapter) -> None:
    with pytest.raises(RuntimeNotFound):
        adapter.get_result("run_missing")


# --------------------------------------------------------------------------- #
# Unsupported input and partial control endpoints
# --------------------------------------------------------------------------- #


def test_provide_input_is_unsupported(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    result = adapter.provide_input(run_id, "input_1", "sha256:abc", {"answer": "yes"})
    assert result.outcome is InputOutcome.UNSUPPORTED
    assert result.error is not None
    assert result.error.code is ErrorCode.UNSUPPORTED


def test_partial_control_endpoints(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    approval = adapter.resolve_approval(
        run_id, {"approval_id": "approval_1", "decision": "approve"}
    )
    assert approval.accepted is True
    assert approval.status == "running"
    resolved = adapter.resolve_unknown(run_id, {"reason": "operator reviewed"})
    assert resolved.accepted is True
