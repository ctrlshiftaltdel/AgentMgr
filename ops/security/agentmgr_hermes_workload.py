#!/usr/bin/env python3
"""Hermes sandbox workload and local model proxy (Issue #97).

Installed read-only as ``/workload.py`` and started by systemd as
``/usr/bin/python3 /workload.py`` with no arguments; everything it needs is
bound into the transient unit at a fixed path (design section 6.3 and 8.1)::

    /task/                          task directory (the only writable area)
    /task/manifest.json             adapter/helper binding facts (no secrets)
    /task/prompt.txt                governed prompt, bounded
    /task/profile.template.yaml     minimal model profile with ``__PORT__``
    /task/profile/config.yaml       rendered profile (no key)
    /task/egress.token              short-lived task egress token, mode 0640
    /task/status/{started,heartbeat,exit}.json
    /task/stdout.txt, /task/stderr.txt
    /task/out/result.txt            the only allowed result artifact
    /task/egress.sock               bind-mounted host broker socket
    /runtime/                       read-only runtime root (host mount)
    /task/{home,state,locks,tmp,profile}/

Behaviour
---------

1. Validate the task directory and its files (regular files, no symlinks,
   bounded sizes, expected modes), verify the manifest's prompt, profile
   template and egress-token SHA-256 digests against the files, and read the
   model/binding facts from ``manifest.json``.  The prompt is never read into
   argv and never logged.
2. Start a loopback HTTP proxy on ``127.0.0.1:0``.  It accepts the allowlisted
   OpenAI-compatible method/path pairs, removes every client credential
   (``Authorization``, ``X-Api-Key`` and any client-supplied egress token),
   adds ``X-AgentMgr-Egress-Token`` from the task token file and streams the
   response from the Unix-socket broker ``/task/egress.sock``.
3. Render ``profile/config.yaml`` with the actual port and write it atomically.
4. Spawn the fixed Hermes one-shot command in its own process group::

       <runtime>/hermes-agent/.hermes/bin/hermes chat \\
         --query-file /task/prompt.txt --oneshot -Q --format text \\
         -t terminal --provider custom \\
         --base-url http://127.0.0.1:<port>/v1 --model <name> \\
         --safe-mode --run-budget <seconds>

   The child receives a fresh allowlisted environment (no host credential, DB
   DSN, path or profile variable).  The task egress token is exposed only as
   ``CUSTOM_API_KEY``/``OPENAI_API_KEY``; it is never a command-line argument.
5. Write ``status/started.json`` and ``status/heartbeat.json`` atomically,
   capture stdout/stderr with the prompt and token redacted, enforce the run
   budget/deadline, and on timeout, stop signal or any post-spawn failure send
   SIGTERM to the whole process group, then SIGKILL after the grace period.
   ``status/exit.json`` records exit code, signal, duration and the
   timeout/cancel flags plus a stable error code when the workload itself
   failed.
6. Never create, modify or read ``out/result.txt``: the host adapter reads and
   hashes it after the unit stopped.

This module is standard-library only and must not import ``app.*``: it is
installed outside the project environment and runs inside the sandbox.
"""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

__all__ = [
    "ALLOWED_PATHS",
    "ALLOWED_REQUESTS",
    "CHILD_PATH",
    "DEADLINE_MARGIN_SECONDS",
    "EGRESS_TOKEN_HEADER",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_TASK_INVALID",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "EXIT_WRITE_RESERVE_SECONDS",
    "EgressProxy",
    "HEARTBEAT_INTERVAL_SECONDS",
    "MAX_EGRESS_TOKEN_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_MODEL_BYTES",
    "MAX_PROFILE_TEMPLATE_BYTES",
    "MAX_PROMPT_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "POLL_INTERVAL_SECONDS",
    "ProxyHandle",
    "TERMINATE_GRACE_SECONDS",
    "TaskManifest",
    "UNIT_EGRESS_SOCKET_PATH",
    "UNIT_EGRESS_TOKEN_PATH",
    "UNIT_EXIT_PATH",
    "UNIT_HEARTBEAT_PATH",
    "UNIT_OUTPUT_PATH",
    "UNIT_PROFILE_CONFIG_PATH",
    "UNIT_PROFILE_TEMPLATE_PATH",
    "UNIT_PROMPT_PATH",
    "UNIT_RUNTIME_ROOT",
    "UNIT_STARTED_PATH",
    "UNIT_TASK_DIR",
    "WorkloadError",
    "build_child_env",
    "build_hermes_argv",
    "main",
    "read_manifest",
    "render_profile",
    "run_workload",
    "start_egress_proxy",
    "unit_run_parameters",
    "write_status_atomic",
]

# ---------------------------------------------------------------------------
# fixed unit layout
# ---------------------------------------------------------------------------

UNIT_TASK_DIR = Path("/task")
UNIT_RUNTIME_ROOT = Path("/runtime")
UNIT_PROMPT_PATH = Path("/task/prompt.txt")
UNIT_PROFILE_TEMPLATE_PATH = Path("/task/profile.template.yaml")
UNIT_PROFILE_CONFIG_PATH = Path("/task/profile/config.yaml")
UNIT_EGRESS_TOKEN_PATH = Path("/task/egress.token")
UNIT_EGRESS_SOCKET_PATH = Path("/task/egress.sock")
UNIT_STATUS_DIR = Path("/task/status")
UNIT_STARTED_PATH = Path("/task/status/started.json")
UNIT_HEARTBEAT_PATH = Path("/task/status/heartbeat.json")
UNIT_EXIT_PATH = Path("/task/status/exit.json")
UNIT_STDOUT_PATH = Path("/task/stdout.txt")
UNIT_STDERR_PATH = Path("/task/stderr.txt")
UNIT_OUTPUT_PATH = Path("/task/out/result.txt")

#: Sub-directories of the task directory the child may write to.
WORKLOAD_DIRECTORIES: tuple[str, ...] = (
    "home",
    "state",
    "locks",
    "tmp",
    "profile",
    "status",
)

#: Fixed minimal ``PATH``: no host profile, no per-user bin directory.
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"

#: Header the local proxy uses to present the task egress token to the broker.
EGRESS_TOKEN_HEADER = "X-AgentMgr-Egress-Token"

# ---------------------------------------------------------------------------
# limits, modes and intervals
# ---------------------------------------------------------------------------

MAX_PROMPT_BYTES = 1_048_576
MAX_PROFILE_TEMPLATE_BYTES = 65_536
MAX_EGRESS_TOKEN_BYTES = 4_096
MAX_MANIFEST_BYTES = 65_536
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_STDOUT_BYTES = 1_048_576
MAX_STDERR_BYTES = 262_144
#: Replacement written where a prompt or token occurrence was redacted.
REDACTED = b"[redacted]"

MIN_RUN_BUDGET_SECONDS = 1
MAX_RUN_BUDGET_SECONDS = 86_400
#: Seconds kept between the workload's internal deadline (plus the SIGTERM
#: grace and the exit-marker write) and systemd ``RuntimeMaxSec``.
EXIT_WRITE_RESERVE_SECONDS = 2

HEARTBEAT_INTERVAL_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.2
#: Extra time after ``--run-budget`` before the workload's own deadline fires;
#: Hermes normally stops itself first, this is the backstop.
DEADLINE_MARGIN_SECONDS = 5.0
#: Grace between SIGTERM and SIGKILL for the child process group.
TERMINATE_GRACE_SECONDS = 5.0
UPSTREAM_TIMEOUT_SECONDS = 120.0

TASK_DIR_MODE = 0o770
WORKLOAD_FILE_MODE = 0o640
STATUS_FILE_MODE = 0o640
TOKEN_FILE_MODE = 0o640
PROTECTED_FILE_MODES = frozenset({0o440, 0o640})

#: Request methods/paths the proxy is allowed to forward to the broker.
ALLOWED_REQUESTS: Mapping[str, frozenset[str]] = {
    "POST": frozenset(
        {
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/embeddings",
        }
    ),
    "GET": frozenset({"/v1/models"}),
}
ALLOWED_PATHS: frozenset[str] = frozenset().union(*ALLOWED_REQUESTS.values())
#: Client request headers copied upstream.  Everything else, in particular
#: ``Authorization``, cookies and API keys, is dropped.  ``Accept-Encoding`` is
#: never sent, so the broker/upstream answers identity-encoded (a client may
#: still send a ``Content-Encoding`` for its own request body).
FORWARDED_REQUEST_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "Content-Encoding",
    "Accept",
)
#: Upstream response headers copied to Hermes.  Cookies and authentication
#: challenges are never replayed into the sandbox.
FORWARDED_RESPONSE_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "Content-Encoding",
    "Cache-Control",
    "Retry-After",
    "X-Request-Id",
)

TRUNCATION_MARKER = b"\n[truncated]\n"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TASK_INVALID = 3
EXIT_UNAVAILABLE = 4
EXIT_INTERNAL = 5

UNIT_PATTERN = re.compile(r"^agentmgr-hermes-[0-9a-f]{16}$")
HANDLE_PATTERN = re.compile(r"^rtask_hermes_[A-Za-z0-9_-]{4,63}$")
#: Task 2 treats ``model_name`` as trusted text of at most 256 characters with
#: no control characters.  The workload keeps that length limit, rejects a
#: leading ``-`` (option injection) and other characters that cannot be part of
#: a provider model identifier.
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:+/@-]{0,255}$")
MAX_MODEL_BYTES = 256
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{8,4096}$")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BASE_URL_PATTERN = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})/v1$")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | getattr(os, "O_CREAT", 0)
    | getattr(os, "O_EXCL", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

_READ_CHUNK_BYTES = 65_536

#: Set by the SIGTERM/SIGINT handler; only ever read by the wait loop.
_stop_signal: int | None = None


class WorkloadError(Exception):
    """A fail-closed rejection carrying only a stable, path-free code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code or CONTROL_CHARACTERS.search(code):
            code = "internal_error"
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------------------
# immutable contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskManifest:
    """The secret-free binding facts the helper wrote before launch."""

    unit: str
    task_id: str
    runtime_handle: str
    model: str
    runtime_max_sec: int
    prompt_sha256: str
    profile_template_sha256: str
    egress_token_sha256: str


class ProxyHandle:
    """A running loopback proxy plus the thread serving it."""

    __slots__ = ("port", "thread", "_server", "_lock", "_closed")

    def __init__(
        self, port: int, thread: threading.Thread, server: ThreadingHTTPServer
    ) -> None:
        self.port = port
        self.thread = thread
        self._server = server
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """Stop serving and release the loopback port exactly once."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._server.shutdown()
        except OSError:
            pass
        finally:
            try:
                self._server.server_close()
            except OSError:
                pass
            if self.thread.is_alive():
                self.thread.join(5.0)

    def __enter__(self) -> ProxyHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# small filesystem helpers
# ---------------------------------------------------------------------------


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise WorkloadError("path_unavailable") from None


def _require_absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, Path):
        raise WorkloadError(f"{label}_invalid")
    if not value.is_absolute() or value == Path("/"):
        raise WorkloadError(f"{label}_invalid")
    if any(part in {"", ".", ".."} for part in value.parts[1:]):
        raise WorkloadError(f"{label}_invalid")
    if CONTROL_CHARACTERS.search(str(value)):
        raise WorkloadError(f"{label}_invalid")
    return value


def _require_task_directory(task_dir: object) -> Path:
    path = _require_absolute_path(task_dir, "task_dir")
    info = _lstat(path)
    if info is None:
        raise WorkloadError("task_dir_missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkloadError("task_dir_invalid")
    return path


def _ensure_directory(parent: Path, name: str) -> Path:
    path = parent / name
    info = _lstat(path)
    if info is None:
        try:
            os.mkdir(path, TASK_DIR_MODE)
            os.chmod(path, TASK_DIR_MODE)
        except OSError:
            raise WorkloadError("workload_dir_unavailable") from None
        return path
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkloadError("workload_dir_invalid")
    return path


def _read_task_file(
    path: Path,
    *,
    label: str,
    limit: int,
    allowed_modes: frozenset[int] | None = None,
    exact_mode: int | None = None,
) -> bytes:
    """Read one bounded, regular, non-symlinked task file."""

    info = _lstat(path)
    if info is None:
        raise WorkloadError(f"{label}_missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkloadError(f"{label}_invalid")
    if info.st_size <= 0 or info.st_size > limit:
        raise WorkloadError(f"{label}_invalid")
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None and mode != exact_mode:
        raise WorkloadError(f"{label}_mode_invalid")
    if allowed_modes is not None and mode not in allowed_modes:
        raise WorkloadError(f"{label}_mode_invalid")

    try:
        descriptor = os.open(path, _READ_FLAGS)
    except BlockingIOError:
        raise WorkloadError(f"{label}_invalid") from None
    except OSError:
        raise WorkloadError(f"{label}_invalid") from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise WorkloadError(f"{label}_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            except BlockingIOError:
                raise WorkloadError(f"{label}_invalid") from None
            except OSError:
                raise WorkloadError(f"{label}_invalid") from None
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise WorkloadError(f"{label}_invalid")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    if total != info.st_size:
        raise WorkloadError(f"{label}_invalid")
    return b"".join(chunks)


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = data
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise WorkloadError("status_write_failed")
        remaining = remaining[written:]


def _write_atomic(path: Path, data: bytes, *, label: str = "status") -> None:
    """Replace ``path`` with ``data`` atomically, refusing symlinked targets."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise WorkloadError(f"{label}_invalid")
    parent = path.parent
    info = _lstat(parent)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkloadError(f"{label}_invalid")
    info = _lstat(path)
    if info is not None and (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
    ):
        raise WorkloadError(f"{label}_invalid")

    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(temporary, _WRITE_FLAGS, STATUS_FILE_MODE)
    except OSError:
        raise WorkloadError(f"{label}_write_failed") from None
    try:
        os.fchmod(descriptor, STATUS_FILE_MODE)
        _write_all(descriptor, data)
        os.fsync(descriptor)
    except OSError:
        os.close(descriptor)
        _discard(temporary)
        raise WorkloadError(f"{label}_write_failed") from None
    except WorkloadError:
        os.close(descriptor)
        _discard(temporary)
        raise
    os.close(descriptor)
    try:
        os.replace(temporary, path)
    except OSError:
        _discard(temporary)
        raise WorkloadError(f"{label}_write_failed") from None


def _discard(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def write_status_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Write one compact JSON status document atomically (mode 0640)."""

    if not isinstance(payload, Mapping):
        raise WorkloadError("status_invalid")
    try:
        data = (
            json.dumps(
                dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        raise WorkloadError("status_invalid") from None
    _write_atomic(path, data)


def _write_profile(path: Path, text: str) -> None:
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise WorkloadError("profile_template_invalid") from None
    _write_atomic(path, data, label="profile")
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise WorkloadError("profile_write_failed")


# ---------------------------------------------------------------------------
# manifest and task input
# ---------------------------------------------------------------------------


def read_manifest(task_dir: Path) -> TaskManifest:
    """Read the secret-free manifest written by the helper's ``prepare``."""

    directory = _require_task_directory(task_dir)
    raw = _read_task_file(
        directory / "manifest.json",
        label="manifest",
        limit=MAX_MANIFEST_BYTES,
        allowed_modes=PROTECTED_FILE_MODES,
    )
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        raise WorkloadError("manifest_invalid") from None
    if not isinstance(document, Mapping):
        raise WorkloadError("manifest_invalid")

    unit = document.get("unit")
    if not isinstance(unit, str) or UNIT_PATTERN.fullmatch(unit) is None:
        raise WorkloadError("manifest_invalid")
    task_id = document.get("task_id")
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise WorkloadError("manifest_invalid")
    if CONTROL_CHARACTERS.search(task_id):
        raise WorkloadError("manifest_invalid")
    handle = document.get("runtime_handle")
    if not isinstance(handle, str) or HANDLE_PATTERN.fullmatch(handle) is None:
        raise WorkloadError("manifest_invalid")
    model = document.get("model", "")
    if not isinstance(model, str):
        raise WorkloadError("manifest_invalid")
    if model:
        _validate_model(model)
    runtime_max_sec = document.get("runtime_max_sec")
    if (
        type(runtime_max_sec) is not int
        or not MIN_RUN_BUDGET_SECONDS <= runtime_max_sec <= MAX_RUN_BUDGET_SECONDS
    ):
        raise WorkloadError("manifest_invalid")
    hashes: dict[str, str] = {}
    for field in (
        "prompt_sha256",
        "profile_template_sha256",
        "egress_token_sha256",
    ):
        value = document.get(field)
        if not isinstance(value, str) or SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise WorkloadError("manifest_invalid")
        hashes[field] = value
    return TaskManifest(
        unit=unit,
        task_id=task_id,
        runtime_handle=handle,
        model=model,
        runtime_max_sec=runtime_max_sec,
        prompt_sha256=hashes["prompt_sha256"],
        profile_template_sha256=hashes["profile_template_sha256"],
        egress_token_sha256=hashes["egress_token_sha256"],
    )


def _read_egress_token(path: Path) -> str:
    """Read the task token for the proxy; failures carry a stable code only."""

    info = _lstat(path)
    if info is None:
        raise WorkloadError("egress_token_unavailable")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkloadError("egress_token_invalid")
    if info.st_size <= 0 or info.st_size > MAX_EGRESS_TOKEN_BYTES:
        raise WorkloadError("egress_token_invalid")
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError:
        raise WorkloadError("egress_token_invalid") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkloadError("egress_token_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, MAX_EGRESS_TOKEN_BYTES + 1)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EGRESS_TOKEN_BYTES:
                raise WorkloadError("egress_token_invalid")
            chunks.append(chunk)
    except OSError:
        raise WorkloadError("egress_token_invalid") from None
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise WorkloadError("egress_token_invalid") from None
    return _validate_egress_token(token)


def _validate_egress_token(token: object) -> str:
    if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
        raise WorkloadError("egress_token_invalid")
    return token


# ---------------------------------------------------------------------------
# validated inputs
# ---------------------------------------------------------------------------


def _validate_port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65_535:
        raise WorkloadError("proxy_port_invalid")
    return value


def _validate_model(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_MODEL_BYTES
        or MODEL_PATTERN.fullmatch(value) is None
    ):
        raise WorkloadError("model_invalid")
    return value


def _validate_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise WorkloadError("base_url_invalid")
    match = BASE_URL_PATTERN.fullmatch(value)
    if match is None or not 1 <= int(match.group(1)) <= 65_535:
        raise WorkloadError("base_url_invalid")
    return value


def _validate_budget(value: object) -> int:
    if (
        type(value) is not int
        or not MIN_RUN_BUDGET_SECONDS <= value <= MAX_RUN_BUDGET_SECONDS
    ):
        raise WorkloadError("budget_invalid")
    return value


def _validate_interval(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WorkloadError("interval_invalid")
    number = float(value)
    if number < 0 or number > 3600:
        raise WorkloadError("interval_invalid")
    return number


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1"


# ---------------------------------------------------------------------------
# child argv / environment / profile
# ---------------------------------------------------------------------------


def build_hermes_argv(
    runtime_root: Path,
    *,
    model: str,
    base_url: str,
    run_budget_sec: int,
) -> list[str]:
    """Return the fixed one-shot Hermes argv.

    The prompt is referenced only as the fixed in-unit ``/task/prompt.txt``
    path; the workload never puts prompt text or a credential in argv.
    """

    root = _require_absolute_path(runtime_root, "runtime_root")
    name = _validate_model(model)
    url = _validate_base_url(base_url)
    budget = _validate_budget(run_budget_sec)
    executable = root / "hermes-agent" / ".hermes" / "bin" / "hermes"
    return [
        str(executable),
        "chat",
        "--query-file",
        str(UNIT_PROMPT_PATH),
        "--oneshot",
        "-Q",
        "--format",
        "text",
        "-t",
        "terminal",
        "--provider",
        "custom",
        "--base-url",
        url,
        "--model",
        name,
        "--safe-mode",
        "--run-budget",
        str(budget),
    ]


def build_child_env(
    task_dir: Path, proxy_port: int, *, token: str | None = None
) -> dict[str, str]:
    """Return a fresh allowlist environment for the Hermes child.

    Nothing is inherited from the host.  The task egress token, when given, is
    exposed only as ``CUSTOM_API_KEY``/``OPENAI_API_KEY`` for the loopback
    proxy; it is never an argv entry.
    """

    directory = _require_absolute_path(task_dir, "task_dir")
    port = _validate_port(proxy_port)
    env = {
        "PATH": CHILD_PATH,
        "LC_ALL": "C",
        "HOME": str(directory / "home"),
        "HERMES_HOME": str(directory / "profile"),
        "XDG_STATE_HOME": str(directory / "state"),
        "HERMES_GATEWAY_LOCK_DIR": str(directory / "locks"),
        "TMPDIR": str(directory / "tmp"),
        "OPENAI_BASE_URL": _base_url(port),
        "CUSTOM_API_BASE_URL": _base_url(port),
    }
    if token is not None:
        value = _validate_egress_token(token)
        env["CUSTOM_API_KEY"] = value
        env["OPENAI_API_KEY"] = value
    return env


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def render_profile(template: str, *, proxy_port: int, model: str) -> str:
    """Render the minimal, key-free model profile for the loopback proxy.

    Only the ``model`` block with ``provider``/``base_url``/``name`` is
    accepted; any other section, unknown key or credential-looking text is
    rejected.  ``__PORT__`` and ``__MODEL__`` placeholders are substituted and
    the result is emitted canonically so the file is deterministic.
    """

    port = _validate_port(proxy_port)
    name = _validate_model(model)
    if not isinstance(template, str) or not template.strip():
        raise WorkloadError("profile_template_invalid")
    try:
        encoded = template.encode("utf-8")
    except UnicodeEncodeError:
        raise WorkloadError("profile_template_invalid") from None
    if len(encoded) > MAX_PROFILE_TEMPLATE_BYTES:
        raise WorkloadError("profile_template_invalid")

    lowered = template.lower()
    for forbidden in (
        "api_key",
        "api-key",
        "apikey",
        "password",
        "secret",
        "authorization",
        "bearer ",
        "token",
    ):
        if forbidden in lowered:
            raise WorkloadError("profile_template_invalid")

    values: dict[str, str] = {}
    seen_model = False
    for raw_line in template.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            if line != "model:" or seen_model:
                raise WorkloadError("profile_template_invalid")
            seen_model = True
            continue
        if not seen_model or not line.startswith("  ") or line.startswith("   "):
            raise WorkloadError("profile_template_invalid")
        key, separator, value = line.strip().partition(":")
        if not separator:
            raise WorkloadError("profile_template_invalid")
        key = key.strip()
        if key not in {"provider", "base_url", "name"} or key in values:
            raise WorkloadError("profile_template_invalid")
        values[key] = _unquote(value.strip())
    if not seen_model:
        raise WorkloadError("profile_template_invalid")

    provider = values.get("provider", "custom")
    if provider != "custom":
        raise WorkloadError("profile_template_invalid")
    expected = _base_url(port)
    base_url = values.get("base_url", "http://127.0.0.1:__PORT__/v1")
    base_url = base_url.replace("__PORT__", str(port)).replace("__MODEL__", name)
    if base_url != expected:
        raise WorkloadError("profile_template_invalid")

    return (
        "model:\n"
        "  provider: custom\n"
        f'  base_url: "{expected}"\n'
        f'  name: "{name}"\n'
    )


# ---------------------------------------------------------------------------
# local model proxy
# ---------------------------------------------------------------------------


class _UnixHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` that dials a Unix domain socket."""

    def __init__(self, socket_path: str, *, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self._socket_path)
        except BaseException:
            connection.close()
            raise
        self.sock = connection


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], proxy: EgressProxy) -> None:
        self.proxy = proxy
        super().__init__(address, _ProxyHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        """Never print a handler traceback into the unit's stderr."""

        return


class _ProxyHandler(BaseHTTPRequestHandler):
    """Forward allowlisted OpenAI-compatible calls to the host broker."""

    protocol_version = "HTTP/1.0"
    server_version = "agentmgr-egress-proxy"
    sys_version = ""
    #: ``StreamRequestHandler`` applies this to the accepted connection so a
    #: stalled client cannot pin a worker thread forever.
    timeout = UPSTREAM_TIMEOUT_SECONDS

    #: BaseHTTPRequestHandler logs every request to stderr; a sandbox log must
    #: never contain a URL, a header or a credential.
    def log_message(self, _format: str, *args: object) -> None:
        return

    def log_error(self, _format: str, *args: object) -> None:
        return

    @property
    def _proxy(self) -> EgressProxy:
        server = self.server
        assert isinstance(server, _ProxyServer)
        return server.proxy

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("POST")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("GET")

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject_method()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject_method()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject_method()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject_method()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject_method()

    def _reject_method(self) -> None:
        self._send_json(405, "method_not_allowed")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            # A malformed request target (for example a broken IPv6 literal)
            # must fail closed instead of raising inside the handler thread.
            self._send_json(400, "request_invalid")
            return
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            self._send_json(404, "path_not_allowed")
            return
        allowed_paths = ALLOWED_PATHS
        if parsed.path not in allowed_paths:
            self._send_json(404, "path_not_allowed")
            return
        if parsed.path not in ALLOWED_REQUESTS.get(method, frozenset()):
            self._send_json(405, "method_not_allowed")
            return

        proxy = self._proxy
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_json(400, "request_invalid")
            return
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            length = 0
        else:
            try:
                length = int(length_header)
            except (TypeError, ValueError):
                self._send_json(400, "request_invalid")
                return
        if length < 0 or length > proxy.max_request_bytes:
            self._send_json(413, "request_too_large")
            return
        try:
            body = self.rfile.read(length) if length else b""
        except OSError:
            self._send_json(400, "request_invalid")
            return
        if len(body) != length:
            self._send_json(400, "request_invalid")
            return

        try:
            token = _read_egress_token(proxy.token_path)
        except WorkloadError as error:
            code = (
                "egress_token_unavailable"
                if error.code == "egress_token_unavailable"
                else "egress_token_invalid"
            )
            self._send_json(503, code)
            return

        target = parsed.path
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection = _UnixHTTPConnection(
            str(proxy.upstream_socket), timeout=proxy.upstream_timeout_sec
        )
        try:
            connection.putrequest(
                method, target, skip_host=True, skip_accept_encoding=True
            )
            connection.putheader("Host", "localhost")
            for name in FORWARDED_REQUEST_HEADERS:
                value = self.headers.get(name)
                if value:
                    connection.putheader(name, value)
            if body:
                connection.putheader("Content-Length", str(len(body)))
            connection.putheader(EGRESS_TOKEN_HEADER, token)
            connection.endheaders()
            if body:
                connection.send(body)
            response = connection.getresponse()
        except OSError:
            connection.close()
            self._send_json(502, "egress_unavailable")
            return
        except (ValueError, http.client.HTTPException):
            connection.close()
            self._send_json(502, "egress_unavailable")
            return

        try:
            length_value = response.getheader("Content-Length")
            if length_value:
                try:
                    declared_size = int(length_value)
                except ValueError:
                    declared_size = None
                if declared_size is not None and (
                    declared_size < 0 or declared_size > proxy.max_response_bytes
                ):
                    # Refuse before any header is sent, so the client sees a
                    # stable 502 instead of a silently truncated body.
                    self._send_json(502, "response_too_large")
                    return
            self.send_response_only(response.status, response.reason)
            for name in FORWARDED_RESPONSE_HEADERS:
                value = response.getheader(name)
                if value:
                    self.send_header(name, value)
            if length_value:
                self.send_header("Content-Length", length_value)
            # ``HTTP/1.0`` plus an explicit close frames a streamed body that
            # has no content length (SSE): each chunk is flushed as it arrives.
            self.close_connection = True
            self.end_headers()
            total = 0
            while True:
                try:
                    chunk = response.read1(65_536)
                except (OSError, http.client.HTTPException):
                    break
                if not chunk:
                    break
                total += len(chunk)
                if total > proxy.max_response_bytes:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            response.close()
            connection.close()

    def _send_json(self, status: int, code: str) -> None:
        body = json.dumps({"error": code}, separators=(",", ":")).encode("ascii")
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class EgressProxy:
    """Loopback HTTP model proxy backed by the host broker's Unix socket.

    The token file is re-read for every forwarded request, so deleting or
    replacing ``/task/egress.token`` takes effect immediately and the proxy
    never needs to keep a credential in memory after a request.
    """

    def __init__(
        self,
        upstream_socket: Path,
        *,
        token_path: Path = UNIT_EGRESS_TOKEN_PATH,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        upstream_timeout_sec: float = UPSTREAM_TIMEOUT_SECONDS,
    ) -> None:
        self.upstream_socket = _require_absolute_path(
            upstream_socket, "egress_socket"
        )
        self.token_path = _require_absolute_path(token_path, "egress_token")
        if bind_host != "127.0.0.1":
            raise WorkloadError("proxy_bind_invalid")
        if type(bind_port) is not int or not 0 <= bind_port <= 65_535:
            raise WorkloadError("proxy_bind_invalid")
        if type(max_request_bytes) is not int or max_request_bytes <= 0:
            raise WorkloadError("proxy_limit_invalid")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise WorkloadError("proxy_limit_invalid")
        if (
            not isinstance(upstream_timeout_sec, (int, float))
            or upstream_timeout_sec <= 0
        ):
            raise WorkloadError("proxy_limit_invalid")
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.upstream_timeout_sec = float(upstream_timeout_sec)

    def start(self) -> ProxyHandle:
        """Bind the loopback port and serve it from a daemon thread."""

        try:
            server = _ProxyServer((self.bind_host, self.bind_port), self)
        except OSError:
            raise WorkloadError("proxy_unavailable") from None
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="agentmgr-egress-proxy",
            daemon=True,
        )
        thread.start()
        port = server.server_address[1]
        return ProxyHandle(int(port), thread, server)


def start_egress_proxy(
    socket_path: Path,
    *,
    token_path: Path | None = None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    upstream_timeout_sec: float = UPSTREAM_TIMEOUT_SECONDS,
) -> ProxyHandle:
    """Start the loopback proxy that forwards to ``socket_path``."""

    upstream = _require_absolute_path(socket_path, "egress_socket")
    token = UNIT_EGRESS_TOKEN_PATH if token_path is None else token_path
    proxy = EgressProxy(
        upstream,
        token_path=token,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        upstream_timeout_sec=upstream_timeout_sec,
    )
    return proxy.start()


# ---------------------------------------------------------------------------
# child process handling
# ---------------------------------------------------------------------------


def _install_signal_handlers() -> dict[int, object]:
    previous: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, _handle_stop)
        except (OSError, ValueError):
            continue
    return previous


def _restore_signal_handlers(previous: Mapping[int, object]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)  # type: ignore[arg-type]
        except (OSError, ValueError):
            continue


def _handle_stop(signum: int, _frame: object) -> None:
    global _stop_signal
    _stop_signal = signum


def _open_output(path: Path) -> BinaryIO:
    info = _lstat(path)
    if info is not None and (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
    ):
        raise WorkloadError("log_invalid")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, WORKLOAD_FILE_MODE)
    except OSError:
        raise WorkloadError("log_unavailable") from None
    try:
        os.fchmod(descriptor, WORKLOAD_FILE_MODE)
    except OSError:
        os.close(descriptor)
        raise WorkloadError("log_unavailable") from None
    return os.fdopen(descriptor, "wb")


class _Redactor:
    """Streaming byte redactor for the task prompt and egress token.

    Complete occurrences are replaced; the last ``longest_needle - 1`` bytes are
    held back so an occurrence split across two pipe reads is still caught.  It
    never guesses: a held tail is flushed verbatim (after replacement) at EOF.
    """

    __slots__ = ("_needles", "_hold", "_pending")

    def __init__(self, needles: tuple[bytes, ...]) -> None:
        self._needles = tuple(
            dict.fromkeys(item for item in needles if isinstance(item, bytes) and item)
        )
        self._hold = max((len(item) for item in self._needles), default=1) - 1
        self._pending = b""

    def feed(self, chunk: bytes) -> bytes:
        data = self._pending + chunk
        for needle in self._needles:
            data = data.replace(needle, REDACTED)
        if self._hold and len(data) > self._hold:
            self._pending = data[-self._hold :]
            return data[: -self._hold]
        if self._hold:
            self._pending = data
            return b""
        self._pending = b""
        return data

    def flush(self) -> bytes:
        data = self._pending
        self._pending = b""
        return data


def _write_bounded(
    handle: BinaryIO, data: bytes, total: int, limit: int
) -> tuple[int, bool]:
    """Write at most ``limit`` bytes; return ``(total_written, truncated)``."""

    if not data:
        return total, False
    if total < limit:
        room = limit - total
        piece = data[:room]
        handle.write(piece)
        total += len(piece)
        if len(piece) < len(data):
            handle.write(TRUNCATION_MARKER)
            return total, True
        return total, False
    handle.write(TRUNCATION_MARKER)
    return total, True


def _drain(
    stream: BinaryIO, handle: BinaryIO, limit: int, redactor: _Redactor
) -> None:
    """Copy at most ``limit`` redacted bytes, then keep draining the pipe."""

    total = 0
    truncated = False
    try:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                break
            if truncated:
                # Keep reading so the child never blocks on a full pipe, but
                # never write past the cap.
                continue
            total, truncated = _write_bounded(
                handle, redactor.feed(chunk), total, limit
            )
        if not truncated:
            _write_bounded(handle, redactor.flush(), total, limit)
    except (OSError, ValueError):
        pass
    finally:
        try:
            handle.flush()
        except (OSError, ValueError):
            pass
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _signal_group(process: subprocess.Popen[bytes], signum: int) -> None:
    if process.poll() is not None:
        return
    _kill_group(process.pid, signum)


def _kill_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _terminate_process_group(
    process: subprocess.Popen[bytes], grace_sec: float
) -> None:
    """SIGTERM the child process group, then SIGKILL after the grace period.

    The whole group is considered, not just the direct child: a grandchild that
    ignores SIGTERM keeps the group alive and must be SIGKILLed as well.
    """

    if process.poll() is not None and not _group_alive(process.pid):
        return
    _kill_group(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_sec)
    while time.monotonic() < deadline:
        if process.poll() is not None and not _group_alive(process.pid):
            return
        time.sleep(0.05)
    _kill_group(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _signal_name(returncode: int | None) -> str | None:
    if type(returncode) is not int or returncode >= 0:
        return None
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"signal_{-returncode}"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _spawn(
    argv: list[str],
    *,
    task_dir: Path,
    env: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            argv,
            cwd=str(task_dir),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            shell=False,
        )
    except (OSError, ValueError):
        raise WorkloadError("hermes_unavailable") from None


def _run_child(
    argv: list[str],
    *,
    task_dir: Path,
    env: Mapping[str, str],
    manifest: TaskManifest,
    deadline_sec: float,
    heartbeat_interval_sec: float,
    poll_interval_sec: float,
    terminate_grace_sec: float,
    stdout_limit: int,
    stderr_limit: int,
    redact_needles: tuple[bytes, ...] = (),
) -> int:
    """Spawn Hermes, drive the status markers and enforce the deadline.

    Every post-spawn step (started rewrite, heartbeat, wait loop, exit write)
    runs under ``except BaseException``: if a status write or anything else
    fails, the child process group is SIGTERMed, SIGKILLed after the grace and
    a best-effort ``exit.json`` is written before the error is re-raised.  A
    sandbox must never leave an orphan Hermes running until ``RuntimeMaxSec``.
    """

    global _stop_signal

    status_dir = _ensure_directory(task_dir, "status")
    stdout_handle = _open_output(task_dir / "stdout.txt")
    stderr_handle = _open_output(task_dir / "stderr.txt")
    stdout_redactor = _Redactor(redact_needles)
    stderr_redactor = _Redactor(redact_needles)
    readers: list[threading.Thread] = []
    previous: dict[int, object] = {}
    process: subprocess.Popen[bytes] | None = None
    forced: str | None = None
    _stop_signal = None
    started_monotonic = time.monotonic()

    def _write_exit_safely(error_code: str | None = None) -> None:
        returncode = None if process is None else process.returncode
        payload: dict[str, object] = {
            "exit_code": returncode,
            "signal": _signal_name(returncode),
            "duration_sec": round(time.monotonic() - started_monotonic, 3),
            "timed_out": forced == "timed_out",
            "cancelled": forced == "cancelled",
            "finished_at": _utcnow(),
        }
        if process is not None:
            payload["child_pid"] = process.pid
        if error_code is not None:
            payload["error"] = error_code
        try:
            write_status_atomic(status_dir / "exit.json", payload)
        except Exception:
            pass

    try:
        previous = _install_signal_handlers()
        # Design 8.1: the started marker is written before Hermes is spawned so
        # the helper's launch wait has one stable signal.  The child pid is
        # added by an atomic rewrite as soon as the process exists; the marker
        # never carries a prompt, token or host environment value.
        started_payload: dict[str, object] = {
            "unit": manifest.unit,
            "task_id": manifest.task_id,
            "runtime_handle": manifest.runtime_handle,
            "workload_pid": os.getpid(),
            "child_pid": None,
            "started_at": _utcnow(),
        }
        write_status_atomic(status_dir / "started.json", started_payload)

        process = _spawn(argv, task_dir=task_dir, env=env)
        write_status_atomic(
            status_dir / "started.json",
            {**started_payload, "child_pid": process.pid},
        )
        assert process.stdout is not None and process.stderr is not None
        for stream, handle, limit, redactor, name in (
            (
                process.stdout,
                stdout_handle,
                stdout_limit,
                stdout_redactor,
                "workload-stdout",
            ),
            (
                process.stderr,
                stderr_handle,
                stderr_limit,
                stderr_redactor,
                "workload-stderr",
            ),
        ):
            reader = threading.Thread(
                target=_drain,
                args=(stream, handle, limit, redactor),
                name=name,
                daemon=True,
            )
            reader.start()
            readers.append(reader)

        write_status_atomic(
            status_dir / "heartbeat.json",
            {"heartbeat_at": _utcnow(), "child_pid": process.pid},
        )

        deadline = started_monotonic + deadline_sec
        next_heartbeat = started_monotonic + heartbeat_interval_sec
        terminate_deadline = 0.0
        sigkill_sent = False
        while process.poll() is None:
            now = time.monotonic()
            if forced is None:
                if _stop_signal is not None:
                    forced = "cancelled"
                elif now >= deadline:
                    forced = "timed_out"
                if forced is not None:
                    _signal_group(process, signal.SIGTERM)
                    terminate_deadline = now + terminate_grace_sec
            elif not sigkill_sent and now >= terminate_deadline:
                _signal_group(process, signal.SIGKILL)
                sigkill_sent = True
            if now >= next_heartbeat:
                # A failing status write is fatal: the supervisor can no longer
                # observe the task, so fail closed and take the child down.
                write_status_atomic(
                    status_dir / "heartbeat.json",
                    {"heartbeat_at": _utcnow(), "child_pid": process.pid},
                )
                next_heartbeat = now + heartbeat_interval_sec
            time.sleep(poll_interval_sec)

        process.wait()
        for reader in readers:
            reader.join(5.0)
        returncode = process.returncode
        _write_exit_safely()
        if type(returncode) is not int:
            return EXIT_INTERNAL
        if returncode < 0:
            return 128 + (-returncode)
        return returncode
    except BaseException as error:
        if process is not None:
            try:
                _terminate_process_group(process, terminate_grace_sec)
            except Exception:
                pass
            for reader in readers:
                if reader.is_alive():
                    reader.join(2.0)
        code = error.code if isinstance(error, WorkloadError) else "workload_failed"
        _write_exit_safely(code)
        raise
    finally:
        _restore_signal_handlers(previous)
        for reader in readers:
            if reader.is_alive():
                reader.join(1.0)
        for handle in (stdout_handle, stderr_handle):
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        _stop_signal = None


# ---------------------------------------------------------------------------
# workload entry point
# ---------------------------------------------------------------------------


def run_workload(
    *,
    task_dir: Path,
    runtime_root: Path,
    model: str,
    run_budget_sec: int,
    proxy_socket: Path,
    runtime_max_sec: int | None = None,
    heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SECONDS,
    poll_interval_sec: float = POLL_INTERVAL_SECONDS,
    deadline_margin_sec: float = DEADLINE_MARGIN_SECONDS,
    terminate_grace_sec: float = TERMINATE_GRACE_SECONDS,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
) -> int:
    """Run one Hermes task inside the sandbox and return its exit code.

    ``runtime_max_sec`` (when given) is the unit's systemd ``RuntimeMaxSec``;
    the internal deadline is clamped so that deadline + SIGTERM grace + the
    exit-marker reserve always stays below it.  A limit too small to honour
    that contract fails closed with ``runtime_too_short``.
    """

    task = _require_task_directory(task_dir)
    root = _require_absolute_path(runtime_root, "runtime_root")
    name = _validate_model(model)
    budget = _validate_budget(run_budget_sec)
    upstream = _require_absolute_path(proxy_socket, "egress_socket")
    heartbeat_interval = _validate_interval(heartbeat_interval_sec)
    poll_interval = _validate_interval(poll_interval_sec)
    deadline_margin = _validate_interval(deadline_margin_sec)
    grace = _validate_interval(terminate_grace_sec)
    if type(stdout_limit) is not int or stdout_limit <= 0:
        raise WorkloadError("stdout_limit_invalid")
    if type(stderr_limit) is not int or stderr_limit <= 0:
        raise WorkloadError("stderr_limit_invalid")

    deadline_sec = budget + deadline_margin
    if runtime_max_sec is not None:
        if (
            type(runtime_max_sec) is not int
            or not MIN_RUN_BUDGET_SECONDS <= runtime_max_sec <= MAX_RUN_BUDGET_SECONDS
        ):
            raise WorkloadError("runtime_max_invalid")
        maximum = _maximum_deadline(runtime_max_sec, grace)
        deadline_sec = min(deadline_sec, maximum)

    manifest = read_manifest(task)
    if manifest.model and manifest.model != name:
        raise WorkloadError("manifest_model_mismatch")

    # Design 8.1 step 1: the manifest promises the digests of the exact files
    # the helper launched; verify them again before reading any of them.
    prompt_bytes = _read_task_file(
        task / "prompt.txt",
        label="prompt",
        limit=MAX_PROMPT_BYTES,
        allowed_modes=PROTECTED_FILE_MODES,
    )
    if hashlib.sha256(prompt_bytes).hexdigest() != manifest.prompt_sha256:
        raise WorkloadError("prompt_hash_mismatch")
    template_bytes = _read_task_file(
        task / "profile.template.yaml",
        label="profile_template",
        limit=MAX_PROFILE_TEMPLATE_BYTES,
        allowed_modes=PROTECTED_FILE_MODES,
    )
    if (
        hashlib.sha256(template_bytes).hexdigest()
        != manifest.profile_template_sha256
    ):
        raise WorkloadError("profile_template_hash_mismatch")
    try:
        template = template_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkloadError("profile_template_invalid") from None
    token_bytes = _read_task_file(
        task / "egress.token",
        label="egress_token",
        limit=MAX_EGRESS_TOKEN_BYTES,
        exact_mode=TOKEN_FILE_MODE,
    )
    if hashlib.sha256(token_bytes).hexdigest() != manifest.egress_token_sha256:
        raise WorkloadError("egress_token_hash_mismatch")
    try:
        token = _validate_egress_token(token_bytes.decode("ascii").strip())
    except UnicodeDecodeError:
        raise WorkloadError("egress_token_invalid") from None

    for directory in WORKLOAD_DIRECTORIES:
        _ensure_directory(task, directory)

    handle = start_egress_proxy(upstream, token_path=task / "egress.token")
    try:
        profile = render_profile(template, proxy_port=handle.port, model=name)
        _write_profile(task / "profile" / "config.yaml", profile)
        argv = build_hermes_argv(
            root,
            model=name,
            base_url=_base_url(handle.port),
            run_budget_sec=budget,
        )
        env = build_child_env(task, handle.port, token=token)
        return _run_child(
            argv,
            task_dir=task,
            env=env,
            manifest=manifest,
            deadline_sec=deadline_sec,
            heartbeat_interval_sec=heartbeat_interval,
            poll_interval_sec=poll_interval,
            terminate_grace_sec=grace,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            redact_needles=(prompt_bytes, token.encode("ascii")),
        )
    finally:
        handle.close()


def _maximum_deadline(runtime_max_sec: int, grace_sec: float) -> float:
    """Return the largest deadline that leaves grace and exit-write room."""

    maximum = runtime_max_sec - grace_sec - EXIT_WRITE_RESERVE_SECONDS - 1
    if maximum < 1:
        raise WorkloadError("runtime_too_short")
    return float(maximum)


def unit_run_parameters(runtime_max_sec: int) -> tuple[int, float]:
    """Return ``(hermes --run-budget, workload deadline)`` for a unit limit.

    The deadline plus the default SIGTERM grace plus the exit-marker reserve is
    strictly below ``runtime_max_sec``; the Hermes budget stops first so it can
    exit cleanly.  A limit below the minimum contract fails closed.
    """

    if (
        type(runtime_max_sec) is not int
        or not MIN_RUN_BUDGET_SECONDS <= runtime_max_sec <= MAX_RUN_BUDGET_SECONDS
    ):
        raise WorkloadError("runtime_max_invalid")
    maximum = _maximum_deadline(runtime_max_sec, TERMINATE_GRACE_SECONDS)
    budget = max(MIN_RUN_BUDGET_SECONDS, int(maximum - DEADLINE_MARGIN_SECONDS))
    budget = min(budget, int(maximum))
    deadline = min(budget + DEADLINE_MARGIN_SECONDS, maximum)
    return budget, float(deadline)


def main(argv: list[str] | None = None) -> int:
    """Run the fixed unit workload; argv is never accepted."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        return EXIT_USAGE
    try:
        manifest = read_manifest(UNIT_TASK_DIR)
    except WorkloadError:
        return EXIT_TASK_INVALID
    if not manifest.model:
        return EXIT_TASK_INVALID
    try:
        budget, _deadline = unit_run_parameters(manifest.runtime_max_sec)
    except WorkloadError:
        return EXIT_TASK_INVALID
    try:
        return run_workload(
            task_dir=UNIT_TASK_DIR,
            runtime_root=UNIT_RUNTIME_ROOT,
            model=manifest.model,
            run_budget_sec=budget,
            proxy_socket=UNIT_EGRESS_SOCKET_PATH,
            runtime_max_sec=manifest.runtime_max_sec,
        )
    except WorkloadError as error:
        if error.code.endswith("_unavailable"):
            return EXIT_UNAVAILABLE
        return EXIT_TASK_INVALID
    except Exception:
        # Never print a traceback: it can contain task paths or prompt content.
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
