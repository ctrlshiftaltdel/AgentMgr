"""Unit tests for the Hermes sandbox workload and its local model proxy (#97).

The workload in ``ops/security/agentmgr_hermes_workload.py`` is a production
template that runs *inside* a transient sandbox unit.  Nothing here needs a
unit, ``sudo``, ``systemd-run`` or a real model: the tests drive the exported
functions directly, and the one ``run_workload`` integration test writes a
**fake** ``hermes`` executable into a temporary runtime root.

Properties pinned here:

* the prompt is only reachable through ``--query-file /task/prompt.txt`` and the
  task egress token only through the child environment / the proxy header;
* the loopback proxy forwards an allowlisted method/path/body to the Unix
  socket, strips every client credential and injects the task token;
* the workload writes its own status markers atomically, bounds stdout/stderr,
  kills the whole child process group on timeout/cancel and never creates or
  modifies ``out/result.txt``.
"""

from __future__ import annotations

import http.client
import hashlib
import importlib.util
import json
import os
import re
import signal
import socketserver
import stat
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS = REPO_ROOT / "ops" / "security"
WORKLOAD_PATH = OPS / "agentmgr_hermes_workload.py"

PROMPT_CANARY = "PROMPT-CANARY write out/result.txt"
TOKEN_CANARY = "TOKEN-CANARY.egt.canary"
#: A second canary that only ever appears as a client-supplied credential.
CLIENT_CANARY = "CLIENT-AUTH-CANARY"

RUNTIME_ROOT = Path("/var/lib/agentmgr-hermes/runtime")
MODEL = "model-x"
BASE_URL = "http://127.0.0.1:1234/v1"

UNIT = "agentmgr-hermes-0123456789abcdef"
TASK_ID = "task_synthetic_0001"
HANDLE = "rtask_hermes_" + "a" * 32

PROFILE_TEMPLATE = textwrap.dedent(
    """\
    model:
      provider: custom
      base_url: "http://127.0.0.1:__PORT__/v1"
      name: __MODEL__
    """
)


def _load_workload():
    spec = importlib.util.spec_from_file_location(
        "agentmgr_hermes_workload", WORKLOAD_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


workload = _load_workload()


# ---------------------------------------------------------------------------
# task-directory fixtures and fake runtime
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": "agentmgr.hermes.manifest.v1",
        "unit": UNIT,
        "task_id": TASK_ID,
        "runtime_handle": HANDLE,
        "runtime_max_sec": 60,
        "model": MODEL,
        "prompt_sha256": _sha256(PROMPT_CANARY.encode("utf-8")),
        "profile_template_sha256": _sha256(PROFILE_TEMPLATE.encode("utf-8")),
        "egress_token_sha256": _sha256(TOKEN_CANARY.encode("ascii")),
    }
    document.update(overrides)
    return document


def _write(path: Path, data: bytes | str, mode: int = 0o640) -> Path:
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    path.chmod(mode)
    return path


def _make_task(
    root: Path,
    *,
    prompt: bytes = PROMPT_CANARY.encode("utf-8"),
    template: bytes = PROFILE_TEMPLATE.encode("utf-8"),
    token: bytes = TOKEN_CANARY.encode("ascii"),
    manifest_overrides: dict[str, object] | None = None,
    result: bytes | None = None,
) -> Path:
    task = root / "task"
    task.mkdir()
    (task / "out").mkdir()
    (task / "profile").mkdir()
    manifest = _manifest(
        prompt_sha256=_sha256(prompt),
        profile_template_sha256=_sha256(template),
        egress_token_sha256=_sha256(token),
        **(manifest_overrides or {}),
    )
    _write(task / "manifest.json", json.dumps(manifest))
    _write(task / "prompt.txt", prompt)
    _write(task / "profile.template.yaml", template)
    _write(task / "egress.token", token)
    if result is not None:
        (task / "out" / "result.txt").write_bytes(result)
    return task


def _fake_runtime(root: Path, script: str) -> Path:
    executable = root / "runtime" / "hermes-agent" / ".hermes" / "bin" / "hermes"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script))
    executable.chmod(0o755)
    return root / "runtime"


# ---------------------------------------------------------------------------
# fake host egress broker (Unix socket, HTTP over AF_UNIX)
# ---------------------------------------------------------------------------


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "test-egress"
    sys_version = ""

    def log_message(self, *_args: object) -> None:  # noqa: D102 - silence
        return

    def _handle(self) -> None:
        server = self.server
        assert isinstance(server, _Upstream)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server.records.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        if server.stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: one\n\n")
            self.wfile.flush()
            server.release.wait(5.0)
            self.wfile.write(b"data: two\n\n")
            self.wfile.flush()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for name, value in server.response_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(server.response_body)))
        self.end_headers()
        self.wfile.write(server.response_body)

    do_POST = _handle
    do_GET = _handle


class _Upstream(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        path: Path,
        *,
        stream: bool = False,
        response_body: bytes = b'{"ok":true}',
        response_headers: dict[str, str] | None = None,
    ) -> None:
        self.records: list[dict[str, object]] = []
        self.stream = stream
        self.response_body = response_body
        self.response_headers = dict(response_headers or {})
        self.release = threading.Event()
        super().__init__(str(path), _UpstreamHandler)


@contextmanager
def _upstream(path: Path, **kwargs: object):
    server = _Upstream(path, **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5.0)


def _http(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# argv / environment / profile
# ---------------------------------------------------------------------------


def test_hermes_argv_matches_the_supported_one_shot_command_exactly():
    argv = workload.build_hermes_argv(
        RUNTIME_ROOT, model=MODEL, base_url=BASE_URL
    )
    assert argv[0] == str(
        RUNTIME_ROOT / "hermes-agent" / ".hermes" / "bin" / "hermes"
    )
    # Exact flag list: the installed `chat` client rejects `--run-budget`
    # ("Unsupported gateway CLI options", live Task 7), and neither the prompt
    # nor a credential may ever appear in argv.
    assert argv[1:] == [
        "chat",
        "--query-file",
        "/task/prompt.txt",
        "--oneshot",
        "-Q",
        "--format",
        "text",
        "-t",
        "terminal",
        "--provider",
        "custom",
        "--base-url",
        BASE_URL,
        "--model",
        MODEL,
        "--safe-mode",
    ]
    assert not any("run-budget" in item for item in argv)
    assert "--api-key" not in argv
    assert "--yolo" not in argv
    assert not any(
        PROMPT_CANARY in item or TOKEN_CANARY in item for item in argv
    )


def test_hermes_argv_has_no_run_budget_flag():
    # Regression for the live Task 7 finding: the CLI rejects the flag before
    # any model call, so the workload deadline/systemd must be the only bound.
    argv = workload.build_hermes_argv(
        RUNTIME_ROOT, model=MODEL, base_url=BASE_URL
    )
    assert "--run-budget" not in argv
    assert not any("run-budget" in item for item in argv)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"model": "", "base_url": BASE_URL},
            "model_invalid",
        ),
        (
            {"model": "--yolo", "base_url": BASE_URL},
            "model_invalid",
        ),
        (
            {"model": "a\nb", "base_url": BASE_URL},
            "model_invalid",
        ),
        (
            {"model": "a\tb", "base_url": BASE_URL},
            "model_invalid",
        ),
        (
            {"model": MODEL, "base_url": "http://example.com/v1"},
            "base_url_invalid",
        ),
        (
            {"model": MODEL, "base_url": "https://127.0.0.1:1/v1"},
            "base_url_invalid",
        ),
        (
            {"model": MODEL, "base_url": "http://127.0.0.1:1/other"},
            "base_url_invalid",
        ),
    ],
)
def test_hermes_argv_rejects_unsafe_input(kwargs, code):
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.build_hermes_argv(RUNTIME_ROOT, **kwargs)
    assert excinfo.value.code == code


@pytest.mark.parametrize("budget", [0, -1, 1.5, True])
def test_run_workload_rejects_an_invalid_internal_budget(tmp_path, budget):
    # The budget is no longer an argv flag but still bounds the deadline.
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.run_workload(
            task_dir=tmp_path,
            runtime_root=tmp_path / "runtime",
            model=MODEL,
            run_budget_sec=budget,
            proxy_socket=tmp_path / "e.sock",
        )
    assert excinfo.value.code == "budget_invalid"


@pytest.mark.parametrize(
    "model",
    [
        "a" * workload.MAX_MODEL_BYTES,
        "provider/model-x",
        "model v1",
        "a.b:c+d_e@f",
    ],
)
def test_model_validation_accepts_task2_compatible_names(model):
    argv = workload.build_hermes_argv(RUNTIME_ROOT, model=model, base_url=BASE_URL)
    assert argv[argv.index("--model") + 1] == model


def test_model_validation_rejects_an_overlong_name():
    model = "a" * (workload.MAX_MODEL_BYTES + 1)
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.build_hermes_argv(RUNTIME_ROOT, model=model, base_url=BASE_URL)
    assert excinfo.value.code == "model_invalid"


def test_hermes_argv_rejects_relative_runtime_root():
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.build_hermes_argv(
            Path("runtime"), model=MODEL, base_url=BASE_URL
        )
    assert excinfo.value.code == "runtime_root_invalid"


def test_child_env_clears_host_secrets_and_sets_hermes_home(tmp_path):
    env = workload.build_child_env(tmp_path, 1234)
    assert env["HOME"] == str(tmp_path / "home")
    assert env["HERMES_HOME"] == str(tmp_path / "profile")
    assert env["XDG_STATE_HOME"] == str(tmp_path / "state")
    assert env["HERMES_GATEWAY_LOCK_DIR"] == str(tmp_path / "locks")
    assert env["TMPDIR"] == str(tmp_path / "tmp")
    assert env["PATH"] == workload.CHILD_PATH
    assert "AGENTMGR_DB_DSN" not in env
    assert "API_KEY" not in " ".join(env)


def test_child_env_does_not_inherit_any_host_variable(tmp_path, monkeypatch):
    for name in (
        "AGENTMGR_DB_DSN",
        "DATABASE_URL",
        "API_KEY",
        "OPENAI_API_KEY",
        "HERMES_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "HERMES_HOME",
    ):
        monkeypatch.setenv(name, f"HOST-CANARY-{name}")
    env = workload.build_child_env(tmp_path, 1234)
    joined = "\n".join(f"{key}={value}" for key, value in env.items())
    assert "HOST-CANARY" not in joined
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM_API_KEY" not in env
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:1234/v1"


def test_child_env_exposes_the_task_token_only_as_api_key_env(tmp_path):
    env = workload.build_child_env(tmp_path, 4321, token=TOKEN_CANARY)
    assert env["CUSTOM_API_KEY"] == TOKEN_CANARY
    assert env["OPENAI_API_KEY"] == TOKEN_CANARY
    assert not any(
        TOKEN_CANARY in value for key, value in env.items()
        if key not in {"CUSTOM_API_KEY", "OPENAI_API_KEY"}
    )


def test_render_profile_replaces_the_port_with_the_minimal_model_block():
    rendered = workload.render_profile(
        PROFILE_TEMPLATE, proxy_port=23456, model=MODEL
    )
    assert "__PORT__" not in rendered
    assert "__MODEL__" not in rendered
    assert "http://127.0.0.1:23456/v1" in rendered
    document = rendered.splitlines()
    assert document[0] == "model:"
    assert "  provider: custom" in document
    assert f'  name: "{MODEL}"' in document
    lowered = rendered.lower()
    for forbidden in ("api_key", "api-key", "token", "secret", "password"):
        assert forbidden not in lowered


def test_render_profile_rejects_a_foreign_endpoint_or_extra_sections():
    other_host = PROFILE_TEMPLATE.replace(
        "http://127.0.0.1:__PORT__/v1", "http://example.com/v1"
    )
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.render_profile(other_host, proxy_port=1, model=MODEL)
    assert excinfo.value.code == "profile_template_invalid"

    with_key = PROFILE_TEMPLATE.replace(
        "  name: __MODEL__", "  name: __MODEL__\n  api_key: should-not-be-here"
    )
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.render_profile(with_key, proxy_port=1, model=MODEL)
    assert excinfo.value.code == "profile_template_invalid"

    with_section = PROFILE_TEMPLATE + "plugins:\n  enabled: true\n"
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.render_profile(with_section, proxy_port=1, model=MODEL)
    assert excinfo.value.code == "profile_template_invalid"


@pytest.mark.parametrize("port", [0, -1, 65536, "1234"])
def test_render_profile_rejects_an_invalid_port(port):
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.render_profile(PROFILE_TEMPLATE, proxy_port=port, model=MODEL)
    assert excinfo.value.code == "proxy_port_invalid"


# ---------------------------------------------------------------------------
# runtime "installs" exposure in HERMES_HOME
# ---------------------------------------------------------------------------


def test_link_runtime_installs_creates_a_symlink_to_the_given_target(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    runtime_installs = tmp_path / "runtime" / "installs"
    runtime_installs.mkdir(parents=True)

    workload.link_runtime_installs(profile, runtime_installs)

    link = profile / "installs"
    assert link.is_symlink()
    assert os.readlink(link) == str(runtime_installs)


def test_link_runtime_installs_is_idempotent_for_the_same_target(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    runtime_installs = tmp_path / "runtime" / "installs"
    runtime_installs.mkdir(parents=True)

    workload.link_runtime_installs(profile, runtime_installs)
    workload.link_runtime_installs(profile, runtime_installs)

    link = profile / "installs"
    assert link.is_symlink()
    assert os.readlink(link) == str(runtime_installs)
    assert sorted(item.name for item in profile.iterdir()) == ["installs"]


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("file", "profile_installs_invalid"),
        ("directory", "profile_installs_invalid"),
        ("foreign-symlink", "profile_installs_mismatch"),
    ],
)
def test_link_runtime_installs_rejects_an_existing_unsafe_entry(
    tmp_path, kind, code
):
    profile = tmp_path / "profile"
    profile.mkdir()
    runtime_installs = tmp_path / "runtime" / "installs"
    runtime_installs.mkdir(parents=True)
    entry = profile / "installs"
    if kind == "file":
        entry.write_text("not-a-link")
    elif kind == "directory":
        entry.mkdir()
    else:
        other = tmp_path / "other-installs"
        other.mkdir()
        entry.symlink_to(other, target_is_directory=True)

    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.link_runtime_installs(profile, runtime_installs)
    assert excinfo.value.code == code
    # The unsafe entry is never silently replaced or removed.
    if kind == "file":
        assert entry.read_text() == "not-a-link"
    elif kind == "directory":
        assert entry.is_dir() and not entry.is_symlink()
    else:
        assert entry.is_symlink()
        assert os.readlink(entry) != str(runtime_installs)


@pytest.mark.parametrize("target", ["missing", "file"])
def test_link_runtime_installs_leaves_an_absent_target_absent(tmp_path, target):
    profile = tmp_path / "profile"
    profile.mkdir()
    if target == "missing":
        runtime_installs = tmp_path / "runtime" / "installs"
    else:
        runtime_installs = tmp_path / "runtime" / "installs"
        runtime_installs.parent.mkdir(parents=True)
        runtime_installs.write_text("not-a-directory")

    workload.link_runtime_installs(profile, runtime_installs)

    assert not (profile / "installs").exists()
    assert not (profile / "installs").is_symlink()
    assert list(profile.iterdir()) == []


def test_link_runtime_installs_rejects_a_symlinked_profile_dir(tmp_path):
    real = tmp_path / "real-profile"
    real.mkdir()
    profile = tmp_path / "profile"
    profile.symlink_to(real, target_is_directory=True)
    runtime_installs = tmp_path / "runtime" / "installs"
    runtime_installs.mkdir(parents=True)

    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.link_runtime_installs(profile, runtime_installs)
    assert excinfo.value.code == "profile_dir_invalid"


# ---------------------------------------------------------------------------
# local model proxy
# ---------------------------------------------------------------------------


def _start_proxy(tmp_path: Path, *, token: str | None = TOKEN_CANARY, **kwargs):
    socket_path = tmp_path / "e.sock"
    token_path = tmp_path / "egress.token"
    if token is not None:
        _write(token_path, token.encode("ascii"))
    if token is None and token_path.exists():
        token_path.unlink()
    handle = workload.start_egress_proxy(
        socket_path, token_path=token_path, **kwargs
    )
    return handle


def test_proxy_forwards_method_path_body_and_replaces_client_credentials(tmp_path):
    with _upstream(tmp_path / "e.sock") as upstream:
        handle = _start_proxy(tmp_path)
        try:
            status, _headers, body = _http(
                handle.port,
                "POST",
                "/v1/chat/completions",
                body=b'{"model":"m"}',
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {CLIENT_CANARY}",
                    "X-AgentMgr-Egress-Token": CLIENT_CANARY,
                    "X-Api-Key": CLIENT_CANARY,
                },
            )
        finally:
            handle.close()

    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert len(upstream.records) == 1
    record = upstream.records[0]
    assert record["method"] == "POST"
    assert record["path"] == "/v1/chat/completions"
    assert record["body"] == b'{"model":"m"}'
    headers = record["headers"]
    assert headers["x-agentmgr-egress-token"] == TOKEN_CANARY
    assert headers["content-type"] == "application/json"
    assert "authorization" not in headers
    assert "x-api-key" not in headers
    assert CLIENT_CANARY not in json.dumps(headers)


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/v1/chat/completions", 405),
        ("DELETE", "/v1/models", 405),
        ("POST", "/v1/admin", 404),
        ("POST", "/v1/../admin", 404),
        ("POST", "/v1/chat/completions/extra", 404),
        ("POST", "http://example.com/v1/chat/completions", 404),
    ],
)
def test_proxy_rejects_methods_and_paths_outside_the_allowlist(
    tmp_path, method, path, expected
):
    with _upstream(tmp_path / "e.sock") as upstream:
        handle = _start_proxy(tmp_path)
        try:
            status, _headers, body = _http(handle.port, method, path, body=b"{}")
        finally:
            handle.close()
    assert status == expected
    assert json.loads(body)["error"] in {"method_not_allowed", "path_not_allowed"}
    assert upstream.records == []


def test_proxy_rejects_an_oversized_or_chunked_request_body(tmp_path):
    with _upstream(tmp_path / "e.sock") as upstream:
        handle = _start_proxy(tmp_path, max_request_bytes=64)
        try:
            status, _headers, body = _http(
                handle.port, "POST", "/v1/chat/completions", body=b"x" * 200
            )
            assert status == 413
            assert json.loads(body)["error"] == "request_too_large"
            assert upstream.records == []

            connection = http.client.HTTPConnection(
                "127.0.0.1", handle.port, timeout=5
            )
            try:
                connection.putrequest("POST", "/v1/chat/completions")
                connection.putheader("Transfer-Encoding", "chunked")
                connection.endheaders()
                response = connection.getresponse()
                assert response.status == 400
                assert json.loads(response.read())["error"] == "request_invalid"
            finally:
                connection.close()
        finally:
            handle.close()
    assert upstream.records == []


def test_proxy_streams_the_upstream_response_without_buffering(tmp_path):
    with _upstream(tmp_path / "e.sock", stream=True) as upstream:
        handle = _start_proxy(tmp_path)
        connection = http.client.HTTPConnection(
            "127.0.0.1", handle.port, timeout=5
        )
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 200
            first = response.read1(64)
            assert first == b"data: one\n\n"
            upstream.release.set()
            rest = response.read()
        finally:
            connection.close()
            handle.close()
    assert rest == b"data: two\n\n"


def test_proxy_fails_closed_without_a_token_or_upstream(tmp_path):
    handle = _start_proxy(tmp_path, token=None)
    try:
        status, _headers, body = _http(
            handle.port, "POST", "/v1/chat/completions", body=b"{}"
        )
    finally:
        handle.close()
    assert status == 503
    assert json.loads(body)["error"] == "egress_token_unavailable"

    handle = _start_proxy(tmp_path)
    try:
        status, _headers, body = _http(
            handle.port, "POST", "/v1/chat/completions", body=b"{}"
        )
    finally:
        handle.close()
    assert status == 502
    assert json.loads(body)["error"] == "egress_unavailable"


def test_proxy_handler_never_logs_the_token(tmp_path, capsys):
    with _upstream(tmp_path / "e.sock") as upstream:
        handle = _start_proxy(tmp_path)
        try:
            _http(
                handle.port,
                "POST",
                "/v1/chat/completions",
                body=b"{}",
                headers={"Authorization": f"Bearer {CLIENT_CANARY}"},
            )
        finally:
            handle.close()
    captured = capsys.readouterr()
    assert TOKEN_CANARY not in captured.out + captured.err
    assert CLIENT_CANARY not in captured.out + captured.err
    assert upstream.records


def test_proxy_forwards_content_encoding_and_never_requests_compression(tmp_path):
    with _upstream(
        tmp_path / "e.sock", response_headers={"Content-Encoding": "gzip"}
    ) as upstream:
        handle = _start_proxy(tmp_path)
        try:
            status, headers, _body = _http(
                handle.port,
                "POST",
                "/v1/chat/completions",
                body=b'{"model":"m"}',
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "identity",
                },
            )
        finally:
            handle.close()
    assert status == 200
    assert headers.get("Content-Encoding") == "gzip"
    record = upstream.records[0]
    assert record["headers"]["content-encoding"] == "identity"
    # Identity responses are guaranteed: the proxy never asks for compression.
    assert "accept-encoding" not in record["headers"]


def test_proxy_rejects_a_declared_response_body_over_the_cap(tmp_path):
    with _upstream(tmp_path / "e.sock", response_body=b"x" * 100):
        handle = _start_proxy(tmp_path, max_response_bytes=32)
        try:
            status, _headers, body = _http(
                handle.port, "POST", "/v1/chat/completions", body=b"{}"
            )
        finally:
            handle.close()
    assert status == 502
    assert json.loads(body)["error"] == "response_too_large"


def test_proxy_returns_a_stable_error_for_a_malformed_request_target(
    tmp_path, monkeypatch
):
    # ``http.server`` normalises a leading ``//`` before the handler sees it, so
    # the defensive ``urlsplit`` branch is exercised directly with a malformed
    # target: it must answer a stable 400 instead of raising in the thread.
    def broken_urlsplit(_value):
        raise ValueError("malformed request target")

    with _upstream(tmp_path / "e.sock") as upstream:
        handle = _start_proxy(tmp_path)
        try:
            monkeypatch.setattr(workload, "urlsplit", broken_urlsplit)
            status, _headers, body = _http(
                handle.port, "POST", "/v1/chat/completions", body=b"{}"
            )
        finally:
            handle.close()
            monkeypatch.undo()
    assert status == 400
    assert json.loads(body)["error"] == "request_invalid"
    assert upstream.records == []


# ---------------------------------------------------------------------------
# status markers
# ---------------------------------------------------------------------------


def test_write_status_atomic_replaces_the_file_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "started.json"
    workload.write_status_atomic(path, {"unit": UNIT, "started": True})
    workload.write_status_atomic(path, {"unit": UNIT, "started": True, "pid": 7})
    assert _json(path) == {"unit": UNIT, "started": True, "pid": 7}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert sorted(item.name for item in tmp_path.iterdir()) == ["started.json"]


def test_write_status_atomic_rejects_a_symlinked_target(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    path = tmp_path / "started.json"
    path.symlink_to(target)
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.write_status_atomic(path, {"unit": UNIT})
    assert excinfo.value.code == "status_invalid"
    assert target.read_text() == "{}"


# ---------------------------------------------------------------------------
# run_workload: fake hermes in a temporary runtime root
# ---------------------------------------------------------------------------


WIRING_SCRIPT = """\
import json
import os
import sys
import urllib.request

report = {
    "argv": sys.argv[1:],
    "env": {
        name: os.environ.get(name)
        for name in (
            "HOME",
            "HERMES_HOME",
            "XDG_STATE_HOME",
            "HERMES_GATEWAY_LOCK_DIR",
            "TMPDIR",
            "PATH",
            "CUSTOM_API_KEY",
            "OPENAI_API_KEY",
        )
    },
}
base_url = sys.argv[sys.argv.index("--base-url") + 1]
request = urllib.request.Request(
    base_url + "/chat/completions",
    data=b'{"model":"m","messages":[]}',
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer CLIENT-AUTH-CANARY",
    },
)
with urllib.request.urlopen(request, timeout=10) as response:
    report["status"] = response.status
    report["body"] = response.read().decode("utf-8")
with open("fake-report.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle)
"""


def test_run_workload_wires_env_argv_profile_and_proxy(tmp_path):
    runtime = _fake_runtime(tmp_path, WIRING_SCRIPT)
    task = _make_task(tmp_path)
    with _upstream(tmp_path / "e.sock") as upstream:
        code = workload.run_workload(
            task_dir=task,
            runtime_root=runtime,
            model=MODEL,
            run_budget_sec=30,
            proxy_socket=tmp_path / "e.sock",
            heartbeat_interval_sec=30.0,
        )

    assert code == 0
    report = _json(task / "fake-report.json")
    assert report["status"] == 200
    argv = report["argv"]
    assert "--query-file" in argv
    assert argv[argv.index("--query-file") + 1] == "/task/prompt.txt"
    assert "--api-key" not in argv
    assert TOKEN_CANARY not in json.dumps(argv)
    assert PROMPT_CANARY not in json.dumps(argv)

    base_url = argv[argv.index("--base-url") + 1]
    assert re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/v1", base_url)
    assert report["env"]["CUSTOM_API_KEY"] == TOKEN_CANARY
    assert report["env"]["OPENAI_API_KEY"] == TOKEN_CANARY
    assert report["env"]["HOME"] == str(task / "home")
    assert report["env"]["HERMES_HOME"] == str(task / "profile")
    assert report["env"]["XDG_STATE_HOME"] == str(task / "state")
    assert report["env"]["HERMES_GATEWAY_LOCK_DIR"] == str(task / "locks")
    assert report["env"]["TMPDIR"] == str(task / "tmp")

    profile = (task / "profile" / "config.yaml").read_text(encoding="utf-8")
    assert "__PORT__" not in profile
    assert base_url in profile
    assert f'name: "{MODEL}"' in profile
    assert TOKEN_CANARY not in profile

    assert len(upstream.records) == 1
    headers = upstream.records[0]["headers"]
    assert headers["x-agentmgr-egress-token"] == TOKEN_CANARY
    assert "authorization" not in headers

    started = _json(task / "status" / "started.json")
    assert started["unit"] == UNIT
    assert started["task_id"] == TASK_ID
    assert started["runtime_handle"] == HANDLE
    assert isinstance(started["child_pid"], int)

    heartbeat = _json(task / "status" / "heartbeat.json")
    assert isinstance(heartbeat["heartbeat_at"], str)

    exit_document = _json(task / "status" / "exit.json")
    assert exit_document["exit_code"] == 0
    assert exit_document["timed_out"] is False
    assert exit_document["cancelled"] is False
    assert exit_document["duration_sec"] >= 0

    for directory in ("home", "state", "locks", "tmp", "status"):
        assert (task / directory).is_dir()
    assert not (task / "out" / "result.txt").exists()

    for path in (task / "status").iterdir():
        text = path.read_text(encoding="utf-8")
        assert PROMPT_CANARY not in text
        assert TOKEN_CANARY not in text
    for name in ("stdout.txt", "stderr.txt"):
        text = (task / name).read_text(encoding="utf-8", errors="replace")
        assert PROMPT_CANARY not in text
        assert TOKEN_CANARY not in text


def test_run_workload_exposes_runtime_installs_before_spawning(tmp_path, monkeypatch):
    runtime = _fake_runtime(tmp_path, "import sys\nsys.exit(0)\n")
    task = _make_task(tmp_path)
    captured: dict[str, object] = {}

    def fake_link(profile_dir, runtime_installs):
        captured["profile"] = profile_dir
        captured["installs"] = runtime_installs
        captured["config_exists"] = (profile_dir / "config.yaml").is_file()

    monkeypatch.setattr(workload, "link_runtime_installs", fake_link)
    code = workload.run_workload(
        task_dir=task,
        runtime_root=runtime,
        model=MODEL,
        run_budget_sec=30,
        proxy_socket=tmp_path / "e.sock",
        heartbeat_interval_sec=30.0,
    )
    assert code == 0
    assert captured["profile"] == task / "profile"
    # The unit sees the runtime root mounted at "/runtime", so the target must
    # be the runtime-visible path, not the host path passed as runtime_root.
    assert captured["installs"] == Path("/runtime/installs")
    assert captured["config_exists"] is True


def test_run_workload_preserves_an_existing_result_artifact(tmp_path):
    runtime = _fake_runtime(tmp_path, "import sys\nsys.exit(0)\n")
    task = _make_task(tmp_path, result=b"existing-artifact")
    socket_path = tmp_path / "e.sock"
    with _upstream(socket_path):
        workload.run_workload(
            task_dir=task,
            runtime_root=runtime,
            model=MODEL,
            run_budget_sec=10,
            proxy_socket=socket_path,
            heartbeat_interval_sec=30.0,
        )
    assert (task / "out" / "result.txt").read_bytes() == b"existing-artifact"


TIMEOUT_SCRIPT = """\
import json
import os
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
    ]
)
with open("pids.json", "w", encoding="utf-8") as handle:
    json.dump({"pid": os.getpid(), "child": child.pid}, handle)
time.sleep(60)
"""


def test_run_workload_timeout_kills_the_whole_process_group(tmp_path):
    runtime = _fake_runtime(tmp_path, TIMEOUT_SCRIPT)
    task = _make_task(tmp_path)
    socket_path = tmp_path / "e.sock"
    with _upstream(socket_path):
        code = workload.run_workload(
            task_dir=task,
            runtime_root=runtime,
            model=MODEL,
            run_budget_sec=1,
            proxy_socket=socket_path,
            heartbeat_interval_sec=0.2,
            poll_interval_sec=0.05,
            deadline_margin_sec=0.3,
            terminate_grace_sec=0.3,
        )

    exit_document = _json(task / "status" / "exit.json")
    assert exit_document["timed_out"] is True
    assert exit_document["cancelled"] is False
    assert exit_document["exit_code"] == -signal.SIGKILL
    assert exit_document["signal"] == "SIGKILL"
    assert code == 128 + signal.SIGKILL

    pids = _json(task / "pids.json")
    assert _wait_gone(pids["pid"])
    assert _wait_gone(pids["child"])


CANCEL_SCRIPT = """\
import json
import os
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open("pids.json", "w", encoding="utf-8") as handle:
    json.dump({"pid": os.getpid(), "child": child.pid}, handle)
time.sleep(60)
"""


def test_run_workload_forwards_sigterm_and_records_cancellation(tmp_path):
    runtime = _fake_runtime(tmp_path, CANCEL_SCRIPT)
    task = _make_task(tmp_path)
    socket_path = tmp_path / "e.sock"

    previous = signal.getsignal(signal.SIGTERM)
    with _upstream(socket_path):
        try:
            def _cancel() -> None:
                time.sleep(0.5)
                os.kill(os.getpid(), signal.SIGTERM)

            threading.Thread(target=_cancel, daemon=True).start()
            code = workload.run_workload(
                task_dir=task,
                runtime_root=runtime,
                model=MODEL,
                run_budget_sec=30,
                proxy_socket=socket_path,
                heartbeat_interval_sec=0.2,
                poll_interval_sec=0.05,
                terminate_grace_sec=2.0,
            )
        finally:
            signal.signal(signal.SIGTERM, previous)

    exit_document = _json(task / "status" / "exit.json")
    assert exit_document["cancelled"] is True
    assert exit_document["timed_out"] is False
    assert exit_document["exit_code"] == -signal.SIGTERM
    assert exit_document["signal"] == "SIGTERM"
    assert code == 128 + signal.SIGTERM
    pids = _json(task / "pids.json")
    assert _wait_gone(pids["child"])


BOUNDED_SCRIPT = """\
import sys

sys.stdout.write("O" * 5000)
sys.stderr.write("E" * 5000)
sys.stdout.flush()
sys.stderr.flush()
sys.exit(3)
"""


def test_run_workload_bounds_stdout_and_stderr(tmp_path):
    runtime = _fake_runtime(tmp_path, BOUNDED_SCRIPT)
    task = _make_task(tmp_path)
    socket_path = tmp_path / "e.sock"
    with _upstream(socket_path):
        code = workload.run_workload(
            task_dir=task,
            runtime_root=runtime,
            model=MODEL,
            run_budget_sec=30,
            proxy_socket=socket_path,
            heartbeat_interval_sec=30.0,
            stdout_limit=64,
            stderr_limit=32,
        )
    assert code == 3
    stdout = (task / "stdout.txt").read_bytes()
    stderr = (task / "stderr.txt").read_bytes()
    assert stdout.startswith(b"O" * 64)
    assert stderr.startswith(b"E" * 32)
    assert len(stdout) <= 64 + 32
    assert len(stderr) <= 32 + 32
    assert b"[truncated]" in stdout
    assert b"[truncated]" in stderr
    assert _json(task / "status" / "exit.json")["exit_code"] == 3


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda task: (task / "prompt.txt").unlink(), "prompt_missing"),
        (
            lambda task: (
                (task / "prompt.txt").unlink(),
                (task / "prompt.txt").symlink_to(task / "manifest.json"),
            ),
            "prompt_invalid",
        ),
        (
            lambda task: (task / "prompt.txt").chmod(0o644),
            "prompt_mode_invalid",
        ),
        (
            lambda task: (
                (task / "profile.template.yaml").unlink(),
                (task / "profile.template.yaml").symlink_to(task / "manifest.json"),
            ),
            "profile_template_invalid",
        ),
        (
            lambda task: (task / "egress.token").chmod(0o644),
            "egress_token_mode_invalid",
        ),
        (lambda task: (task / "egress.token").unlink(), "egress_token_missing"),
        (lambda task: (task / "manifest.json").unlink(), "manifest_missing"),
        (
            lambda task: _write(
                task / "manifest.json", json.dumps(_manifest(unit="not-a-unit"))
            ),
            "manifest_invalid",
        ),
        (
            lambda task: _write(
                task / "manifest.json", json.dumps(_manifest(model="other-model"))
            ),
            "manifest_model_mismatch",
        ),
        (
            lambda task: _write(
                task / "manifest.json",
                json.dumps(_manifest(prompt_sha256="0" * 64)),
            ),
            "prompt_hash_mismatch",
        ),
        (
            lambda task: _write(
                task / "manifest.json",
                json.dumps(_manifest(profile_template_sha256="0" * 64)),
            ),
            "profile_template_hash_mismatch",
        ),
        (
            lambda task: _write(
                task / "manifest.json",
                json.dumps(_manifest(egress_token_sha256="0" * 64)),
            ),
            "egress_token_hash_mismatch",
        ),
    ],
)
def test_run_workload_validates_task_files_before_starting(tmp_path, mutate, code):
    runtime = _fake_runtime(tmp_path, "import sys\nsys.exit(0)\n")
    task = _make_task(tmp_path)
    mutate(task)
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.run_workload(
            task_dir=task,
            runtime_root=runtime,
            model=MODEL,
            run_budget_sec=10,
            proxy_socket=tmp_path / "e.sock",
        )
    assert excinfo.value.code == code
    assert not (task / "status" / "started.json").exists()


def test_run_workload_reports_a_missing_runtime_binary(tmp_path):
    task = _make_task(tmp_path)
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.run_workload(
            task_dir=task,
            runtime_root=tmp_path / "runtime",
            model=MODEL,
            run_budget_sec=10,
            proxy_socket=tmp_path / "e.sock",
            heartbeat_interval_sec=30.0,
        )
    assert excinfo.value.code == "hermes_unavailable"
    started = _json(task / "status" / "started.json")
    assert started["child_pid"] is None
    exit_document = _json(task / "status" / "exit.json")
    assert exit_document["exit_code"] is None
    assert exit_document["timed_out"] is False
    assert exit_document["cancelled"] is False
    assert exit_document["error"] == "hermes_unavailable"
    assert not (task / "status" / "heartbeat.json").exists()


def test_run_workload_kills_the_child_when_a_status_write_fails(
    tmp_path, monkeypatch
):
    """A failing heartbeat must not leave Hermes running until RuntimeMaxSec."""

    runtime = _fake_runtime(tmp_path, TIMEOUT_SCRIPT)
    task = _make_task(tmp_path)
    socket_path = tmp_path / "e.sock"
    real_write = workload.write_status_atomic

    def flaky_write(path, payload):
        # The first heartbeat happens immediately after the spawn; fail a later
        # one so the fake child has recorded its process group first.
        if path.name == "heartbeat.json" and (task / "pids.json").exists():
            raise workload.WorkloadError("status_write_failed")
        real_write(path, payload)

    monkeypatch.setattr(workload, "write_status_atomic", flaky_write)
    with _upstream(socket_path):
        with pytest.raises(workload.WorkloadError) as excinfo:
            workload.run_workload(
                task_dir=task,
                runtime_root=runtime,
                model=MODEL,
                run_budget_sec=30,
                proxy_socket=socket_path,
                heartbeat_interval_sec=0.2,
                poll_interval_sec=0.05,
                terminate_grace_sec=0.3,
            )
    assert excinfo.value.code == "status_write_failed"
    pids = _json(task / "pids.json")
    assert _wait_gone(pids["pid"])
    assert _wait_gone(pids["child"])
    exit_document = _json(task / "status" / "exit.json")
    assert exit_document["error"] == "status_write_failed"
    assert exit_document["timed_out"] is False
    assert exit_document["cancelled"] is False


def test_run_workload_redacts_prompt_and_token_from_captured_output(tmp_path):
    leak = f"{PROMPT_CANARY} {TOKEN_CANARY}"
    script = (
        "import sys\n"
        f"leak = {leak!r}\n"
        "sys.stdout.write(leak + chr(10))\n"
        "sys.stderr.write(leak + chr(10))\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
    )
    runtime = _fake_runtime(tmp_path, script)
    task = _make_task(tmp_path)
    socket_path = tmp_path / "e.sock"
    with _upstream(socket_path):
        code = workload.run_workload(
            task_dir=task,
            runtime_root=runtime,
            model=MODEL,
            run_budget_sec=30,
            proxy_socket=socket_path,
            heartbeat_interval_sec=30.0,
        )
    assert code == 0
    stdout = (task / "stdout.txt").read_text(encoding="utf-8")
    stderr = (task / "stderr.txt").read_text(encoding="utf-8")
    for text in (stdout, stderr):
        assert PROMPT_CANARY not in text
        assert TOKEN_CANARY not in text
        assert "[redacted]" in text


def test_run_workload_fails_closed_when_the_final_exit_marker_fails(
    tmp_path, monkeypatch
):
    """A missing final exit marker must never be reported as success."""

    runtime = _fake_runtime(tmp_path, "import sys\nsys.exit(0)\n")
    task = _make_task(tmp_path)
    socket_path = tmp_path / "e.sock"
    real_write = workload.write_status_atomic

    def flaky_write(path, payload):
        if path.name == "exit.json":
            raise workload.WorkloadError("status_write_failed")
        real_write(path, payload)

    monkeypatch.setattr(workload, "write_status_atomic", flaky_write)
    with _upstream(socket_path):
        with pytest.raises(workload.WorkloadError) as excinfo:
            workload.run_workload(
                task_dir=task,
                runtime_root=runtime,
                model=MODEL,
                run_budget_sec=30,
                proxy_socket=socket_path,
                heartbeat_interval_sec=30.0,
            )
    assert excinfo.value.code == "status_write_failed"
    assert not (task / "status" / "exit.json").exists()


@pytest.mark.parametrize(
    "interval",
    [float("nan"), float("inf"), float("-inf"), 0, -1.0, 3601.0],
)
def test_run_workload_rejects_non_finite_or_out_of_range_intervals(
    tmp_path, interval
):
    # A NaN interval would make every deadline comparison false and silently
    # disable the run budget.
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.run_workload(
            task_dir=tmp_path,
            runtime_root=tmp_path / "runtime",
            model=MODEL,
            run_budget_sec=10,
            proxy_socket=tmp_path / "e.sock",
            heartbeat_interval_sec=interval,
        )
    assert excinfo.value.code == "interval_invalid"


@pytest.mark.parametrize("runtime_max_sec", [10, 60, 600])
def test_unit_run_parameters_keep_deadline_and_grace_below_runtime_max(
    runtime_max_sec,
):
    budget, deadline = workload.unit_run_parameters(runtime_max_sec)
    assert 1 <= budget <= deadline
    assert deadline <= runtime_max_sec - 1
    assert (
        deadline
        + workload.TERMINATE_GRACE_SECONDS
        + workload.EXIT_WRITE_RESERVE_SECONDS
        <= runtime_max_sec - 1
    )


@pytest.mark.parametrize("runtime_max_sec", [1, 8])
def test_unit_run_parameters_fails_closed_below_the_minimum_contract(
    runtime_max_sec,
):
    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.unit_run_parameters(runtime_max_sec)
    assert excinfo.value.code == "runtime_too_short"


def test_run_workload_clamps_the_deadline_to_runtime_max(tmp_path, monkeypatch):
    runtime = _fake_runtime(tmp_path, "import sys\nsys.exit(0)\n")
    task = _make_task(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_child(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(workload, "_run_child", fake_run_child)
    code = workload.run_workload(
        task_dir=task,
        runtime_root=runtime,
        model=MODEL,
        run_budget_sec=60,
        proxy_socket=tmp_path / "e.sock",
        runtime_max_sec=10,
    )
    assert code == 0
    deadline = captured["deadline_sec"]
    assert deadline <= 10 - 1
    assert (
        deadline
        + captured["terminate_grace_sec"]
        + workload.EXIT_WRITE_RESERVE_SECONDS
        <= 10 - 1
    )

    with pytest.raises(workload.WorkloadError) as excinfo:
        workload.run_workload(
            task_dir=task,
            runtime_root=runtime,
            model=MODEL,
            run_budget_sec=1,
            proxy_socket=tmp_path / "e.sock",
            runtime_max_sec=1,
        )
    assert excinfo.value.code == "runtime_too_short"


# ---------------------------------------------------------------------------
# entry point / import boundary
# ---------------------------------------------------------------------------


def test_main_rejects_arguments():
    assert workload.main(["--task-dir", "/task"]) == workload.EXIT_USAGE


def test_main_fails_closed_without_a_task_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(workload, "UNIT_TASK_DIR", tmp_path / "missing")
    assert workload.main([]) == workload.EXIT_TASK_INVALID


def test_main_derives_the_budget_and_deadline_from_the_manifest(
    tmp_path, monkeypatch
):
    task = _make_task(tmp_path)
    monkeypatch.setattr(workload, "UNIT_TASK_DIR", task)
    captured: dict[str, object] = {}

    def fake_run_workload(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(workload, "run_workload", fake_run_workload)
    assert workload.main([]) == 0
    budget, _deadline = workload.unit_run_parameters(60)
    assert captured["task_dir"] == task
    assert captured["model"] == MODEL
    assert captured["run_budget_sec"] == budget
    assert captured["runtime_max_sec"] == 60
    assert captured["proxy_socket"] == workload.UNIT_EGRESS_SOCKET_PATH


def test_main_fails_closed_for_a_too_short_runtime_max(tmp_path, monkeypatch):
    task = _make_task(tmp_path, manifest_overrides={"runtime_max_sec": 5})
    monkeypatch.setattr(workload, "UNIT_TASK_DIR", task)

    def forbidden_run_workload(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("run_workload must not start")

    monkeypatch.setattr(workload, "run_workload", forbidden_run_workload)
    assert workload.main([]) == workload.EXIT_TASK_INVALID


def test_workload_is_stdlib_only_and_never_imports_app():
    source = WORKLOAD_PATH.read_text(encoding="utf-8")
    assert not [
        line
        for line in source.splitlines()
        if line.startswith("import app") or line.startswith("from app")
    ]
    for module in ("json", "os", "signal", "socket", "subprocess"):
        assert f"import {module}" in source
    assert "from http.server import" in source
