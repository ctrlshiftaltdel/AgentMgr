"""Mock-server contract tests for :class:`HermesRuntimeAdapter`.

These tests prove the adapter/contract mapping only.  They are explicitly not
runtime compatibility evidence; the real-runtime evidence lives in
``test_hermes_integration.py`` and
``docs/research/runtime-compatibility/hermes-0.22.1.md``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    LookupOutcome,
    ResultOutcome,
    RuntimeCapability,
    RuntimeGrant,
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
    RuntimeUnsupported,
)
from app.runtime.hermes import (
    HermesConfig,
    HermesRuntimeAdapter,
    _payload_hash as adapter_payload_hash,
)
from app.runtime.http import HttpResponse, UrllibTransport

# Placeholder only; this value is not a real credential and is never used by a
# real runtime in this module.
TEST_KEY = "fake-api-key-0123456789abcdef"
WRONG_KEY = "wrong-api-key-0123456789"


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _payload_hash(body: object) -> str:
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _grant(
    task_id: str,
    *,
    grant_id: str = "grnt_test_0001",
    execution_mode: str = "advisory",
    capabilities: tuple[str, ...] = ("expense.read@1.0.0",),
    scope_type: str = "self",
    data_classifications: tuple[str, ...] = ("internal",),
    expires_at: datetime | None = None,
    task_revision: int = 1,
) -> RuntimeGrant:
    return RuntimeGrant(
        grant_id=grant_id,
        task_id=task_id,
        task_revision=task_revision,
        execution_mode=execution_mode,
        capabilities=capabilities,
        resource_scope={
            "scope_type": scope_type,
            "resource_ids": ["res_0001"],
            "data_classifications": list(data_classifications),
        },
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=1)),
    )


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
        self.event_requests: list[dict[str, Any]] = []
        self.counter = 0

        self.runtime_name = "hermes"
        self.runtime_version = "0.22.1+72.g450ad50"
        self.redirect_to: str | None = None
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
                "runtime": {"name": self.runtime_name, "version": self.runtime_version},
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

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

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

        if path == "/large":
            body = b"x" * 200
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"0123456789")
            self.wfile.flush()
            self.close_connection = True
            return

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
                        "name": self.mock.runtime_name,
                        "version": self.mock.runtime_version,
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
                self.mock.event_requests.append(
                    {"path": path, "headers": dict(self.headers)}
                )
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
            if self.mock.redirect_to is not None:
                self._send_redirect(self.mock.redirect_to)
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


def _start_mock_server() -> tuple[MockHermes, ThreadingHTTPServer, threading.Thread]:
    mock = MockHermes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.mock = mock  # type: ignore[attr-defined]
    mock.api_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return mock, server, thread


def _stop_mock_server(
    server: ThreadingHTTPServer, thread: threading.Thread
) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture()
def mock_server() -> Any:
    mock, server, thread = _start_mock_server()
    try:
        yield mock
    finally:
        _stop_mock_server(server, thread)


def _config(
    api_url: str,
    *,
    key: str = TEST_KEY,
    attempts: int = 2,
) -> HermesConfig:
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


class _SequencedTransport:
    """Returns/raises a scripted sequence of outcomes and records requests."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def _next(self, method: str, url: str, headers: Mapping[str, str], body):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
            }
        )
        if not self.script:
            raise RuntimeTimeout("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, method, url, *, headers, body, timeout):
        return self._next(method, url, headers, body)

    def stream(self, url, *, headers, timeout):
        item = self._next("GET", url, headers, None)
        if isinstance(item, Exception):
            raise item
        return iter(item)


class _KeepaliveOnlyTransport:
    """Streams only keepalive comments, slowly enough to outlive the deadline."""

    def request(self, method, url, *, headers, body, timeout):
        raise RuntimeTimeout("not used")

    def stream(self, url, *, headers, timeout):
        for _ in range(1000):
            time.sleep(0.02)
            yield ": keepalive"


class _GenericErrorTransport:
    def request(self, method, url, *, headers, body, timeout):
        raise RuntimeError("boom")

    def stream(self, url, *, headers, timeout):
        raise RuntimeError("boom")


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
    adapter.submit_task("task_log", "hello", "idem-log", _grant("task_log"))
    assert TEST_KEY not in caplog.text


# --------------------------------------------------------------------------- #
# Capabilities and health
# --------------------------------------------------------------------------- #


def test_static_capabilities_are_labelled(adapter: HermesRuntimeAdapter) -> None:
    snapshot = adapter.capabilities()
    assert snapshot.adapter_contract_version == "1.0.0"
    assert snapshot.boundary == "b3_runtime_adapter"
    assert snapshot.runtime_version is None
    assert snapshot.runtime_name is None
    assert snapshot.version_source is VersionSource.UNKNOWN

    names = [item.name for item in snapshot.capabilities]
    assert len(names) == len(set(names))
    # No capability may claim ``verified`` without a recorded real suite.
    assert all(
        item.status is not CapabilityStatus.VERIFIED for item in snapshot.capabilities
    )
    for name in (
        RuntimeCapability.TASK_SUBMIT,
        RuntimeCapability.TASK_STATUS,
        RuntimeCapability.TASK_EVENTS_POLL,
        RuntimeCapability.TASK_CANCEL,
        RuntimeCapability.TASK_RESULT,
        RuntimeCapability.RUNTIME_HEALTH,
    ):
        assert snapshot.capability(name).status is CapabilityStatus.PARTIALLY_VERIFIED
    assert snapshot.capability(RuntimeCapability.TASK_INPUT_PROVIDE).status is (
        CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability(RuntimeCapability.RUNTIME_ISOLATION).status is (
        CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability(
        RuntimeCapability.RUNTIME_TASK_SCOPED_CREDENTIALS
    ).status is CapabilityStatus.UNVERIFIED
    assert snapshot.capability(RuntimeCapability.RUNTIME_TOOL_PERMISSIONS).status is (
        CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability(RuntimeCapability.RUNTIME_MANAGED_EXECUTION).status is (
        CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_APPROVAL_RESOLVE).status is (
        CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability(RuntimeCapability.TASK_UNKNOWN_RESOLVE).status is (
        CapabilityStatus.UNVERIFIED
    )


def test_capabilities_probe_reads_runtime_version_and_name(
    adapter: HermesRuntimeAdapter,
) -> None:
    snapshot = adapter.capabilities(probe=True)
    assert snapshot.runtime_version == "0.22.1+72.g450ad50"
    assert snapshot.runtime_name == "hermes"
    assert snapshot.version_source is VersionSource.RUNTIME_SELF_REPORTED


def test_capabilities_probe_name_mismatch_downgrades_to_unverified(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    mock_server.runtime_name = "other-agent"
    snapshot = adapter.capabilities(probe=True)
    assert all(
        item.status is CapabilityStatus.UNVERIFIED for item in snapshot.capabilities
    )
    submit = snapshot.capability(RuntimeCapability.TASK_SUBMIT)
    assert submit is not None
    assert "other-agent" in (submit.limitations or "")


def test_capabilities_probe_version_mismatch_adds_limitation(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    mock_server.runtime_version = "9.9.9"
    snapshot = adapter.capabilities(probe=True)
    submit = snapshot.capability(RuntimeCapability.TASK_SUBMIT)
    assert submit is not None
    assert submit.status is CapabilityStatus.PARTIALLY_VERIFIED
    assert "9.9.9" in (submit.limitations or "")


def test_capabilities_version_compares_semver_components(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    # ``0.22.10`` must not be treated as covered by ``0.22.1`` just because the
    # string starts with it.  Every numeric component is compared.
    mock_server.runtime_version = "0.22.10"
    snapshot = adapter.capabilities(probe=True)
    submit = snapshot.capability(RuntimeCapability.TASK_SUBMIT)
    assert submit is not None
    assert submit.status is CapabilityStatus.PARTIALLY_VERIFIED
    assert "0.22.10" in (submit.limitations or "")
    assert "Live probe reported version" in (submit.limitations or "")


def test_capabilities_version_build_metadata_still_covered(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    # The default mock reports ``0.22.1+72.g450ad50``; build metadata does not
    # turn the spike evidence into a mismatch.
    snapshot = adapter.capabilities(probe=True)
    submit = snapshot.capability(RuntimeCapability.TASK_SUBMIT)
    assert submit is not None
    assert submit.status is CapabilityStatus.PARTIALLY_VERIFIED
    assert "Live probe reported version" not in (submit.limitations or "")


def test_capability_limitations_reflect_the_recorded_real_suite(
    adapter: HermesRuntimeAdapter,
) -> None:
    snapshot = adapter.capabilities()
    for item in snapshot.capabilities:
        text = (item.limitations or "").lower()
        assert "has not been run" not in text
        assert "has not run" not in text
    submit = snapshot.capability(RuntimeCapability.TASK_SUBMIT)
    status = snapshot.capability(RuntimeCapability.TASK_STATUS)
    cancel = snapshot.capability(RuntimeCapability.TASK_CANCEL)
    health = snapshot.capability(RuntimeCapability.RUNTIME_HEALTH)
    for item in (submit, status, cancel, health):
        assert item is not None
        assert item.status is CapabilityStatus.PARTIALLY_VERIFIED
        assert "mock-only" in (item.limitations or "")
        assert "real suite" in (item.limitations or "")


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


def test_health_generic_error_returns_stable_unavailable(
    mock_server: MockHermes,
) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_GenericErrorTransport(),
    )
    result = adapter.health()
    assert result.status is HealthStatus.UNAVAILABLE
    assert result.details["reason"] == "unknown"


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
        _grant("task_abc"),
        instructions="be brief",
    )
    assert result.outcome is SubmitOutcome.ACCEPTED
    assert result.runtime_task_id
    assert result.reported_state == "started"
    assert result.replayed is False
    assert result.accepted_at is None  # the mock omits accepted_at

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


def test_payload_hash_uses_exact_utf8_canonical_bytes() -> None:
    body = {
        "input": "请总结这份合同",
        "session_id": "task_cn",
        "instructions": "用简体中文回答",
    }
    expected_bytes = (
        '{"input":"请总结这份合同","instructions":"用简体中文回答",'
        '"session_id":"task_cn"}'
    ).encode("utf-8")
    assert adapter_payload_hash(body) == (
        "sha256:" + hashlib.sha256(expected_bytes).hexdigest()
    )
    # The ASCII-escaped form (the old behaviour) is a different byte string and
    # therefore a different digest.
    escaped = json.dumps(
        body, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert escaped != expected_bytes
    assert adapter_payload_hash(body) != (
        "sha256:" + hashlib.sha256(escaped).hexdigest()
    )


def test_submit_sends_and_hashes_canonical_utf8_bytes(
    mock_server: MockHermes,
) -> None:
    submit_response = HttpResponse(
        202, body=_json_bytes({"run_id": "run_cn", "status": "started"})
    )
    replay_response = HttpResponse(
        202, body=_json_bytes({"run_id": "run_cn", "status": "started"})
    )
    transport = _SequencedTransport([submit_response, replay_response])
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url, attempts=1),
        transport=transport,
    )
    prompt = "请用一句话概括这份报告"
    instructions = "使用简体中文"
    result = adapter.submit_task(
        "task_cn", prompt, "idem-cn", _grant("task_cn"), instructions=instructions
    )
    assert result.outcome is SubmitOutcome.ACCEPTED

    expected_bytes = (
        '{"input":"请用一句话概括这份报告","instructions":"使用简体中文",'
        '"session_id":"task_cn"}'
    ).encode("utf-8")
    assert transport.calls[0]["body"] == expected_bytes

    found = adapter.find_by_idempotency_key(
        "task_cn", "grnt_test_0001", "idem-cn"
    )
    assert found.outcome is LookupOutcome.FOUND
    assert found.payload_hash == (
        "sha256:" + hashlib.sha256(expected_bytes).hexdigest()
    )
    # The lookup replay sends the identical canonical bytes, so the runtime
    # recognises the same idempotent submission.
    assert transport.calls[1]["body"] == expected_bytes


def test_submit_idempotent_replay_and_local_conflict(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    first = adapter.submit_task("task_1", "same", "idem-same", _grant("task_1"))
    second = adapter.submit_task("task_1", "same", "idem-same", _grant("task_1"))
    assert first.runtime_task_id == second.runtime_task_id
    assert second.replayed is True
    assert len(mock_server.runs) == 1

    conflict = adapter.submit_task(
        "task_1", "different", "idem-same", _grant("task_1")
    )
    assert conflict.outcome is SubmitOutcome.CONFLICT
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
        adapter.submit_task("task_1", "hello", "idem-error", _grant("task_1"))


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (400, SubmitOutcome.REJECTED),
        (415, SubmitOutcome.REJECTED),
        (422, SubmitOutcome.REJECTED),
        (409, SubmitOutcome.CONFLICT),
        (501, SubmitOutcome.UNSUPPORTED),
    ],
)
def test_submit_definite_outcomes_are_returned(
    adapter: HermesRuntimeAdapter,
    mock_server: MockHermes,
    status: int,
    outcome: SubmitOutcome,
) -> None:
    mock_server.submit_status = status
    result = adapter.submit_task("task_1", "hello", "idem-definite", _grant("task_1"))
    assert result.outcome is outcome
    assert result.error is not None


def test_submit_404_on_create_route_is_unknown(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    mock_server.submit_status = 404
    result = adapter.submit_task("task_1", "hello", "idem-404", _grant("task_1"))
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.NOT_FOUND
    assert mock_server.runs == {}


@pytest.mark.parametrize("status", [502])
def test_submit_502_is_ambiguous_unknown(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes, status: int
) -> None:
    mock_server.submit_status = status
    result = adapter.submit_task("task_1", "hello", "idem-502", _grant("task_1"))
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN
    keys = [
        item["headers"].get("Idempotency-Key")
        for item in mock_server.requests
        if item["method"] == "POST" and item["path"] == "/v1/runs"
    ]
    assert keys and all(key == "idem-502" for key in keys)


@pytest.mark.parametrize("status", [408, 504])
def test_submit_408_504_is_timeout_with_key_preserved(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes, status: int
) -> None:
    mock_server.submit_status = status
    result = adapter.submit_task("task_1", "hello", "idem-408", _grant("task_1"))
    assert result.outcome in {SubmitOutcome.TIMEOUT, SubmitOutcome.UNKNOWN}
    assert result.error is not None
    assert len(mock_server.runs) == 0
    keys = [
        item["headers"].get("Idempotency-Key")
        for item in mock_server.requests
        if item["method"] == "POST" and item["path"] == "/v1/runs"
    ]
    assert keys and all(key == "idem-408" for key in keys)


def test_submit_timeout_retries_same_key_and_returns_timeout(
    mock_server: MockHermes,
) -> None:
    transport = _RecordingTransport(RuntimeTimeout("timed out"))
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url, attempts=2),
        transport=transport,
    )
    result = adapter.submit_task("task_1", "hello", "idem-timeout", _grant("task_1"))
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
    result = adapter.submit_task("task_1", "hello", "idem-unknown", _grant("task_1"))
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
    result = adapter.submit_task(
        "task_1", "hello", "idem-unavailable", _grant("task_1")
    )
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert len(transport.calls) == 2


def test_submit_malformed_json_maps_to_unknown(mock_server: MockHermes) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StaticTransport(HttpResponse(202, body=b"{not json")),
    )
    result = adapter.submit_task("task_1", "hello", "idem-bad", _grant("task_1"))
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.error is not None


def test_submit_non_object_json_maps_to_unknown(mock_server: MockHermes) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StaticTransport(HttpResponse(202, body=b"[1, 2, 3]")),
    )
    result = adapter.submit_task("task_1", "hello", "idem-list", _grant("task_1"))
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.error is not None


def test_submit_missing_run_id_maps_to_unknown(mock_server: MockHermes) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StaticTransport(HttpResponse(202, body=b"{}")),
    )
    result = adapter.submit_task(
        "task_1", "hello", "idem-missing", _grant("task_1")
    )
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.error is not None


# --------------------------------------------------------------------------- #
# Grant validation
# --------------------------------------------------------------------------- #


def test_submit_rejects_expired_grant(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    grant = _grant("task_1", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    with pytest.raises(RuntimeAuthError):
        adapter.submit_task("task_1", "hello", "idem-expired", grant)
    assert mock_server.runs == {}


def test_submit_rejects_task_mismatch(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    with pytest.raises(RuntimeAuthError):
        adapter.submit_task("task_1", "hello", "idem-mismatch", _grant("task_2"))
    assert mock_server.runs == {}


def test_submit_rejects_governed_resource_access_mode(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    grant = _grant("task_1", execution_mode="governed_resource_access")
    with pytest.raises(RuntimeAuthError):
        adapter.submit_task("task_1", "hello", "idem-mode", grant)


def test_managed_execution_is_always_unsupported(
    mock_server: MockHermes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The removed test-only opt-in must not resurrect via the environment.
    monkeypatch.setenv("HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION", "1")
    adapter = HermesRuntimeAdapter(_config(mock_server.api_url))
    result = adapter.submit_task(
        "task_1",
        "hello",
        "idem-managed",
        _grant("task_1", execution_mode="managed_execution"),
    )
    assert result.outcome is SubmitOutcome.UNSUPPORTED
    assert result.runtime_task_id is None
    assert result.error is not None
    assert result.error.code is ErrorCode.UNSUPPORTED
    assert result.error.context.get("reason") == "managed_execution_unverified"
    assert mock_server.runs == {}


def test_submit_rejects_inconsistent_grant_shape(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    duplicate = _grant(
        "task_1", capabilities=("expense.read@1.0.0", "expense.read@1.0.0")
    )
    with pytest.raises(RuntimeAuthError):
        adapter.submit_task("task_1", "hello", "idem-dupe", duplicate)

    bad_scope = _grant("task_1", scope_type="none")
    with pytest.raises(RuntimeAuthError):
        adapter.submit_task("task_1", "hello", "idem-scope", bad_scope)

    unsupported = _grant(
        "task_1", capabilities=("runtime.managed_execution@1.0.0",)
    )
    result = adapter.submit_task("task_1", "hello", "idem-unsupported", unsupported)
    assert result.outcome is SubmitOutcome.UNSUPPORTED


def test_submit_rejects_boolean_task_revision(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    # ``bool`` is an ``int`` subclass; ``True`` must not pass as revision 1.
    grant = _grant("task_1", task_revision=True)  # type: ignore[arg-type]
    with pytest.raises(RuntimeAuthError) as excinfo:
        adapter.submit_task("task_1", "hello", "idem-bool-rev", grant)
    assert excinfo.value.context.get("reason") == "invalid_task_revision"
    assert mock_server.runs == {}


def test_submit_rejects_non_datetime_expiry(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    grant = _grant("task_1")
    malformed = replace(grant, expires_at="2026-01-01T00:00:00Z")  # type: ignore[arg-type]
    with pytest.raises(RuntimeAuthError) as excinfo:
        adapter.submit_task("task_1", "hello", "idem-bad-expiry", malformed)
    assert excinfo.value.context.get("reason") == "invalid_expiry"
    assert mock_server.runs == {}


# --------------------------------------------------------------------------- #
# Lookup by idempotency key
# --------------------------------------------------------------------------- #


def test_find_by_idempotency_key_found_after_ambiguous_submit(
    mock_server: MockHermes,
) -> None:
    response = HttpResponse(
        202,
        body=_json_bytes({"run_id": "run_lookup", "status": "started"}),
    )
    transport = _SequencedTransport(
        [RuntimeTimeout("lost response"), response]
    )
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url, attempts=1),
        transport=transport,
    )
    submit = adapter.submit_task(
        "task_1", "hello", "idem-lookup", _grant("task_1")
    )
    assert submit.outcome is SubmitOutcome.TIMEOUT

    found = adapter.find_by_idempotency_key("task_1", "grnt_test_0001", "idem-lookup")
    assert found.outcome is LookupOutcome.FOUND
    assert found.runtime_task_id == "run_lookup"
    assert found.payload_hash is not None
    assert found.payload_hash.startswith("sha256:")

    replay = transport.calls[-1]
    assert replay["headers"]["Idempotency-Key"] == "idem-lookup"
    assert replay["body"] == transport.calls[0]["body"]


def test_find_by_idempotency_key_unknown_without_local_record(
    adapter: HermesRuntimeAdapter,
) -> None:
    result = adapter.find_by_idempotency_key("task_1", "grnt_x", "idem-absent")
    assert result.outcome is LookupOutcome.UNKNOWN
    assert result.error is not None


def test_find_by_idempotency_key_conflict_on_payload_hash(
    adapter: HermesRuntimeAdapter,
) -> None:
    adapter.submit_task("task_1", "hello", "idem-conflict", _grant("task_1"))
    result = adapter.find_by_idempotency_key(
        "task_1", "grnt_test_0001", "idem-conflict", "sha256:" + "0" * 64
    )
    assert result.outcome is LookupOutcome.CONFLICT


def test_find_by_idempotency_key_conflict_on_server_409(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    adapter.submit_task("task_1", "hello", "idem-409", _grant("task_1"))
    mock_server.submit_status = 409
    result = adapter.find_by_idempotency_key("task_1", "grnt_test_0001", "idem-409")
    assert result.outcome is LookupOutcome.CONFLICT


def test_find_by_idempotency_key_404_on_create_route_is_unknown(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    adapter.submit_task("task_1", "hello", "idem-404", _grant("task_1"))
    mock_server.submit_status = 404
    result = adapter.find_by_idempotency_key("task_1", "grnt_test_0001", "idem-404")
    # A 404 on the create route does not prove that no run exists.
    assert result.outcome is LookupOutcome.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("status", "submit_outcome", "lookup_outcome", "error_code"),
    [
        (422, SubmitOutcome.REJECTED, LookupOutcome.NOT_FOUND, ErrorCode.INVALID_REQUEST),
        (501, SubmitOutcome.UNSUPPORTED, LookupOutcome.NOT_FOUND, ErrorCode.UNSUPPORTED),
        (409, SubmitOutcome.CONFLICT, LookupOutcome.CONFLICT, ErrorCode.CONFLICT),
    ],
)
def test_find_by_idempotency_key_returns_recorded_terminal_fact(
    adapter: HermesRuntimeAdapter,
    mock_server: MockHermes,
    status: int,
    submit_outcome: SubmitOutcome,
    lookup_outcome: LookupOutcome,
    error_code: ErrorCode,
) -> None:
    key = f"idem-terminal-{status}"
    mock_server.submit_status = status
    submitted = adapter.submit_task("task_1", "hello", key, _grant("task_1"))
    assert submitted.outcome is submit_outcome

    def _create_posts() -> list[dict[str, Any]]:
        return [
            item
            for item in mock_server.requests
            if item["method"] == "POST" and item["path"] == "/v1/runs"
        ]

    before = len(_create_posts())
    # Even if the runtime would now accept the request, the recorded definite
    # outcome is returned without replaying the create POST.
    mock_server.submit_status = None
    result = adapter.find_by_idempotency_key("task_1", "grnt_test_0001", key)
    assert result.outcome is lookup_outcome
    assert result.error is not None
    assert result.error.code is error_code
    assert len(_create_posts()) == before


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
        ("completed", RuntimeState.COMPLETED, False, False),
        ("failed", RuntimeState.FAILED, False, False),
        ("cancelled", RuntimeState.STOPPED, True, False),
        ("timed_out", RuntimeState.TIMED_OUT, True, False),
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
    result = adapter.get_status(run_id, task_id="session-test")
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


def test_status_hides_unverified_session_without_binding(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="running")
    result = adapter.get_status(run_id)
    assert result.session_id is None


def test_status_rejects_cross_task_handle(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(session_id="task_a")
    with pytest.raises(RuntimeAuthError):
        adapter.get_status(run_id, task_id="task_b")


def test_status_ambiguous_json_maps_to_unknown(mock_server: MockHermes) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StaticTransport(HttpResponse(200, body=b"{not json")),
    )
    result = adapter.get_status("run_1")
    assert result.state is RuntimeState.UNKNOWN
    assert result.error is not None


@pytest.mark.parametrize("status", [408, 504])
def test_status_408_504_maps_to_unknown(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes, status: int
) -> None:
    run_id = mock_server.create_run(fixed_status="running")
    mock_server.status_status = status
    result = adapter.get_status(run_id)
    assert result.state is RuntimeState.UNKNOWN
    assert result.error is not None


def test_status_502_maps_to_unknown(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="running")
    mock_server.status_status = 502
    result = adapter.get_status(run_id)
    # 502 is ambiguous, not a definite unavailability answer.
    assert result.state is RuntimeState.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN


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
    result = adapter.get_status(run_id, task_id="session-test")
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
    assert first.events[0].redacted is False
    assert first.events[1].redacted is True
    assert first.events[1].payload["api_key"] == "[redacted]"
    assert first.events[1].payload["authorization"] == "[redacted]"
    assert first.events[1].payload["name"] == "terminal"

    second = adapter.poll_events(run_id, cursor=first.next_cursor, max_events=10)
    assert [event.event_id for event in second.events] == ["3", "4"]
    assert second.next_cursor == "4"
    assert second.has_more is False
    assert second.events[0].type == "run.completed"
    assert second.events[0].occurred_at is not None
    assert second.events[0].occurred_at.year == 2026
    assert second.events[1].type == "message.delta"
    assert second.events[1].payload["text"] == "K"
    assert second.events[1].occurred_at is None  # never fabricated
    headers = {
        key.lower(): value
        for key, value in mock_server.event_requests[-1]["headers"].items()
    }
    assert headers.get("last-event-id") == "2"

    # Replaying the same cursor is stable: events 3 and 4 again, never 1/2.
    replay = adapter.poll_events(run_id, cursor="2", max_events=10)
    assert [event.event_id for event in replay.events] == ["3", "4"]


def test_events_has_more_is_false_when_exact_batch(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(events=_default_events()[:2])
    batch = adapter.poll_events(run_id, max_events=2)
    assert [event.event_id for event in batch.events] == ["1", "2"]
    assert batch.has_more is False


def test_events_keepalive_only_respects_deadline(mock_server: MockHermes) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_KeepaliveOnlyTransport(),
    )
    started = time.monotonic()
    batch = adapter.poll_events("run_1", timeout=0.1)
    elapsed = time.monotonic() - started
    assert batch.events == ()
    assert batch.error is None
    assert elapsed < 1.0


def test_events_rejects_cross_task_handle(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(session_id="task_a")
    with pytest.raises(RuntimeAuthError):
        adapter.poll_events(run_id, task_id="task_b")


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


def test_events_rejects_invalid_cursor_and_argument_types(
    adapter: HermesRuntimeAdapter,
) -> None:
    with pytest.raises(ValueError):
        adapter.poll_events("run_1", cursor=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        adapter.poll_events("run_1", cursor="bad\ncursor")
    with pytest.raises(ValueError):
        adapter.poll_events("run_1", max_events=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        adapter.poll_events("run_1", timeout="1")  # type: ignore[arg-type]


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


def test_cancel_sends_idempotency_key(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    mock_server.stop_polls = 1
    adapter.cancel_task(
        run_id,
        idempotency_key="idem-cancel",
        poll_timeout=1.0,
        poll_interval=0.01,
    )
    request = next(
        item
        for item in mock_server.requests
        if item["method"] == "POST" and item["path"] == f"/v1/runs/{run_id}/stop"
    )
    assert request["headers"]["Idempotency-Key"] == "idem-cancel"


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


def test_cancel_poll_error_keeps_accepted_request(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    mock_server.stop_polls = -1
    mock_server.status_status = 503  # stop POST succeeds, confirmation poll fails
    result = adapter.cancel_task(run_id, poll_timeout=0.5, poll_interval=0.01)
    assert result.outcome is CancelOutcome.REQUESTED
    assert result.stop_confirmed is False
    assert result.error is not None


def test_cancel_stop_success_then_poll_timeout(mock_server: MockHermes) -> None:
    stop_response = HttpResponse(
        200,
        body=_json_bytes({"run_id": "run_1", "status": "stopping"}),
    )
    transport = _SequencedTransport(
        [stop_response, RuntimeTimeout("confirmation poll timed out")]
    )
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=transport,
    )
    result = adapter.cancel_task("run_1", poll_timeout=0.5, poll_interval=0.01)
    assert result.outcome in {CancelOutcome.REQUESTED, CancelOutcome.UNKNOWN}
    assert result.stop_confirmed is False
    assert result.error is not None


def test_cancel_409_queries_status(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="completed")
    mock_server.stop_status = 409
    result = adapter.cancel_task(run_id, poll_timeout=0.01, poll_interval=0.0)
    assert result.outcome is CancelOutcome.ALREADY_TERMINAL
    assert result.stop_confirmed is False


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


def test_cancel_already_terminal_does_not_confirm_a_stop(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="completed")
    result = adapter.cancel_task(run_id, poll_timeout=0.01, poll_interval=0.0)
    assert result.outcome is CancelOutcome.ALREADY_TERMINAL
    assert result.stop_confirmed is False


def test_cancel_unknown_status_is_not_reported_as_requested(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="unknown")
    result = adapter.cancel_task(run_id, poll_timeout=0.5, poll_interval=0.01)
    assert result.outcome is CancelOutcome.UNKNOWN
    assert result.stop_confirmed is False
    assert result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN


def test_cancel_rejects_cross_task_handle(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(session_id="task_a")
    with pytest.raises(RuntimeAuthError):
        adapter.cancel_task(run_id, task_id="task_b")
    assert not any(
        item["method"] == "POST" and item["path"].endswith("/stop")
        for item in mock_server.requests
    )


def test_cancel_rejects_invalid_reason_and_poll_types(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run()
    with pytest.raises(ValueError):
        adapter.cancel_task(run_id, reason=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        adapter.cancel_task(run_id, reason="x" * 100_001)
    with pytest.raises(ValueError):
        adapter.cancel_task(run_id, poll_timeout="1")  # type: ignore[arg-type]
    assert not any(
        item["method"] == "POST" and item["path"].endswith("/stop")
        for item in mock_server.requests
    )


def test_cancel_missing_run_raises(adapter: HermesRuntimeAdapter) -> None:
    with pytest.raises(RuntimeNotFound):
        adapter.cancel_task("run_missing")


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


def test_result_completed_is_partial_without_evidence(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="completed")
    result = adapter.get_result(run_id)
    assert result.outcome is ResultOutcome.PARTIAL_SUCCESS
    assert result.structured_result is False
    assert result.structured_payload is None
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert result.summary == "OK"
    assert result.output == "OK"
    assert result.usage is not None
    assert result.runtime == "hermes"
    assert result.stop_confirmed is False
    assert result.completed_at is None  # the runtime omitted it; do not fabricate
    assert result.result_ref == f"runtime-result:{run_id}"
    assert result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN


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
    assert failed.stop_confirmed is False
    assert cancelled.outcome is ResultOutcome.CANCELLED
    assert cancelled.stop_confirmed is True


def test_result_timed_out_confirms_stop(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    result = adapter.get_result(mock_server.create_run(fixed_status="timed_out"))
    assert result.outcome is ResultOutcome.TIMED_OUT
    assert result.stop_confirmed is True


def test_result_output_is_redacted(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(
        fixed_status="completed",
        output="Authorization: Bearer supersecret token=abc",
    )
    result = adapter.get_result(run_id)
    assert result.output is not None
    assert "supersecret" not in result.output
    assert "token=abc" not in result.output
    assert "[redacted]" in result.output


def test_result_rejects_cross_task_handle(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(fixed_status="completed", session_id="task_a")
    with pytest.raises(RuntimeAuthError):
        adapter.get_result(run_id, task_id="task_b")


def test_result_ambiguous_json_maps_to_unknown(mock_server: MockHermes) -> None:
    adapter = HermesRuntimeAdapter(
        _config(mock_server.api_url),
        transport=_StaticTransport(HttpResponse(200, body=b"{not json")),
    )
    result = adapter.get_result("run_1")
    assert result.outcome is ResultOutcome.UNKNOWN
    assert result.error is not None


@pytest.mark.parametrize("status", [408, 504])
def test_result_408_504_maps_to_unknown(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes, status: int
) -> None:
    run_id = mock_server.create_run(fixed_status="completed")
    mock_server.status_status = status
    result = adapter.get_result(run_id)
    assert result.outcome is ResultOutcome.UNKNOWN
    assert result.error is not None


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


def test_control_endpoints_accept_core_resolved_refs(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(session_id="task_control")
    approval = adapter.resolve_approval(
        run_id, "approval_1", task_id="task_control"
    )
    assert approval.accepted is True
    assert approval.status == "running"
    resolved = adapter.resolve_unknown(
        run_id,
        "resolution_1",
        task_id="task_control",
        reason="operator reviewed",
    )
    assert resolved.accepted is True


def test_control_endpoints_reject_cross_task_refs(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(session_id="task_a")
    with pytest.raises(RuntimeAuthError):
        adapter.resolve_approval(run_id, "approval_1", task_id="task_b")


def test_control_endpoints_reject_non_opaque_refs(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    run_id = mock_server.create_run(session_id="task_control")
    with pytest.raises(ValueError):
        adapter.resolve_approval(
            run_id, "../arbitrary payload", task_id="task_control"
        )


# --------------------------------------------------------------------------- #
# Transport safety, configuration and identifier hygiene
# --------------------------------------------------------------------------- #


def test_redirect_is_refused_and_bearer_is_not_forwarded() -> None:
    mock_a, server_a, thread_a = _start_mock_server()
    mock_b, server_b, thread_b = _start_mock_server()
    try:
        mock_a.redirect_to = f"{mock_b.api_url}/v1/runs"
        adapter = HermesRuntimeAdapter(_config(mock_a.api_url))
        result = adapter.submit_task(
            "task_redirect", "hello", "idem-redirect", _grant("task_redirect")
        )
        assert result.outcome is SubmitOutcome.UNKNOWN
        # The second server was never contacted, so the Authorization header
        # could not have been forwarded to the redirect target.
        assert mock_b.requests == []
        assert mock_b.runs == {}
    finally:
        _stop_mock_server(server_b, thread_b)
        _stop_mock_server(server_a, thread_a)


def test_transport_refuses_oversized_body(mock_server: MockHermes) -> None:
    transport = UrllibTransport(max_response_bytes=64)
    with pytest.raises(RuntimeProtocolError):
        transport.request(
            "GET",
            f"{mock_server.api_url}/large",
            headers={},
            body=None,
            timeout=2.0,
        )


def test_transport_maps_truncated_body_to_unknown(mock_server: MockHermes) -> None:
    transport = UrllibTransport(max_response_bytes=1024)
    with pytest.raises(RuntimeUnknown):
        transport.request(
            "GET",
            f"{mock_server.api_url}/truncated",
            headers={},
            body=None,
            timeout=2.0,
        )


def test_config_strips_key_whitespace_and_rejects_internal_spaces() -> None:
    config = HermesConfig(
        api_url="  http://127.0.0.1:8080  ",
        api_key=f"  {TEST_KEY}  ",
    )
    assert config.api_url == "http://127.0.0.1:8080"
    assert config.api_key == TEST_KEY
    with pytest.raises(RuntimeUnavailable):
        HermesConfig(
            api_url="http://127.0.0.1:8080",
            api_key="fake api key with spaces",
        )


def test_config_has_no_managed_execution_opt_in() -> None:
    base = {
        "HERMES_API_URL": "http://127.0.0.1:8080",
        "HERMES_API_KEY": TEST_KEY,
    }
    config = HermesConfig.from_env(base)
    assert not hasattr(config, "allow_unverified_managed_execution")
    # A leftover legacy switch in the environment must be ignored.
    legacy = HermesConfig.from_env(
        {**base, "HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION": "1"}
    )
    assert legacy == config


def test_run_path_rejects_control_and_dot_segments(
    adapter: HermesRuntimeAdapter,
) -> None:
    for bad in ("../run", "run/other", "run\\other", ".", "..", "run\x00id"):
        with pytest.raises(ValueError):
            adapter.get_status(bad)


def test_event_redaction_covers_normalised_key_names(
    adapter: HermesRuntimeAdapter, mock_server: MockHermes
) -> None:
    payload = json.dumps(
        {
            "type": "tool",
            "x-api-key": "must-not-leak",
            "api-key": "must-not-leak",
            "Cookie": "session=must-not-leak",
            "jwt": "must-not-leak",
            "text": "Authorization: Bearer must-not-leak token=must-not-leak",
            "session_id": "kept",
        }
    )
    run_id = mock_server.create_run(events=[("1", "tool", payload)])
    batch = adapter.poll_events(run_id, max_events=1)
    event = batch.events[0]
    assert event.redacted is True
    assert event.payload["x-api-key"] == "[redacted]"
    assert event.payload["api-key"] == "[redacted]"
    assert event.payload["Cookie"] == "[redacted]"
    assert event.payload["jwt"] == "[redacted]"
    assert "must-not-leak" not in event.payload["text"]
    assert event.payload["session_id"] == "kept"
