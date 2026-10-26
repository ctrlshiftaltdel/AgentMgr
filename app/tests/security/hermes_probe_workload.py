#!/usr/bin/env python3
"""Test-only sandbox probe workload for the #97 live matrix.

This is **not** a production workload.  ``test_hermes_sandbox_live.py`` binds it
into a transient ``agentmgr-hermes-*`` unit with the production helper's pure
``build_systemd_run_argv`` (exactly the same sandbox property profile as the
real workload) and asks one question: what can a task unit reach?

It answers that question with a fixed, read-only checklist, writes the evidence
to the task directory, and then waits for a release file so the caller can read
``systemctl show`` *while the unit is still active*.  It never creates, reads or
modifies ``out/result.txt`` and never touches a real runtime.

Input (written by the test into the task directory, the only writable area)::

    /task/probe-config.json    the checklist: paths, labels, external target
    /task/probe.release        created by the test to let the probe exit

Output::

    /task/probe.json           per-probe expected/observed evidence

The output carries fixed probe labels and ``errno``/exception class names only.
It never contains a host path, prompt, token or canary value, so it is safe to
print on a live test host.

This module is standard-library only and must not import ``app.*``: the sandbox
unit runs it with ``/usr/bin/python3 /workload.py`` like the production workload.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

__all__ = [
    "DEFAULT_DENIED_PATHS",
    "PROBE_VERSION",
    "collect_report",
    "probe_denied_path",
    "probe_external",
    "probe_loopback",
    "probe_task_dir_write",
]

PROBE_VERSION = 1

#: Fixed host paths no task unit may reach.  Labels are generic and stable; the
#: values exist only in the task directory configuration, never in the report.
DEFAULT_DENIED_PATHS: dict[str, str] = {
    "etc_shadow": "/etc/shadow",
    "host_home": "/home",
    "postgresql_data": "/var/lib/postgresql",
    "host_systemd_run": "/run/systemd",
    "host_dbus": "/run/dbus",
    "docker_socket": "/var/run/docker.sock",
    "containerd_socket": "/run/containerd/containerd.sock",
}

DEFAULT_TASK_DIR = Path("/task")
CONFIG_NAME = "probe-config.json"
DEFAULT_OUT_NAME = "probe.json"
DEFAULT_RELEASE_NAME = "probe.release"
DEFAULT_EXTERNAL = ("example.com", 443)

SOCKET_TIMEOUT_SECONDS = 5.0
LINGER_POLL_SECONDS = 0.2
MAX_LINGER_SECONDS = 600

EXIT_OK = 0
EXIT_CONFIG_ERROR = 4
EXIT_MISMATCH = 3


# ---------------------------------------------------------------------------
# individual probes
# ---------------------------------------------------------------------------


def _error_detail(error: OSError) -> str:
    """Return an errno name or exception class; never the message text."""

    if error.errno is not None:
        name = errno.errorcode.get(error.errno)
        if name:
            return name
    return type(error).__name__


def _probe_unix_socket(path: str) -> tuple[str, str]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(SOCKET_TIMEOUT_SECONDS)
    try:
        client.connect(path)
    except OSError as error:
        return "denied", _error_detail(error)
    finally:
        client.close()
    return "reached", "connected"


def probe_denied_path(path: str) -> tuple[str, str]:
    """Return ``(observed, detail)`` for a path that must not be reachable.

    ``observed`` is ``denied`` when the path is absent, unreadable, an empty
    directory or an unconnectable socket, and ``reached`` when any content or
    connection was available.
    """

    try:
        info = os.lstat(path)
    except OSError as error:
        return "denied", _error_detail(error)

    if stat.S_ISDIR(info.st_mode):
        try:
            entries = os.listdir(path)
        except OSError as error:
            return "denied", _error_detail(error)
        return ("denied", "empty_directory") if not entries else ("reached", "entries")

    if stat.S_ISSOCK(info.st_mode):
        return _probe_unix_socket(path)

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        return "denied", _error_detail(error)
    try:
        os.read(descriptor, 16)
    except OSError as error:
        return "denied", _error_detail(error)
    finally:
        os.close(descriptor)
    return "reached", "readable"


def probe_loopback() -> tuple[str, str]:
    """Bind and connect an isolated loopback socket; ``PrivateNetwork`` keeps it."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.settimeout(SOCKET_TIMEOUT_SECONDS)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()[:2]
        with socket.create_connection(
            (host, port), timeout=SOCKET_TIMEOUT_SECONDS
        ) as client:
            connection, _ = listener.accept()
            try:
                client.sendall(b"ping")
                if connection.recv(4) != b"ping":
                    return "error", "no_payload"
            finally:
                connection.close()
    except OSError as error:
        return "error", _error_detail(error)
    finally:
        listener.close()
    return "ok", "connected"


def probe_external(host: str, port: int) -> tuple[str, str]:
    """Attempt one direct outbound connection; any failure is a denial."""

    try:
        with socket.create_connection(
            (host, port), timeout=SOCKET_TIMEOUT_SECONDS
        ):
            return "reached", "connected"
    except OSError as error:
        return "denied", _error_detail(error)


def probe_task_dir_write(task_dir: Path) -> tuple[str, str]:
    """The task directory is the one writable area; prove it writes."""

    target = task_dir / "probe-write-check"
    try:
        with open(target, "wb") as handle:
            handle.write(b"probe")
            handle.flush()
            os.fsync(handle.fileno())
        target.unlink()
    except OSError as error:
        return "error", _error_detail(error)
    return "ok", "written"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _record(expected: str, observed: str, detail: str) -> dict[str, object]:
    return {
        "expected": expected,
        "observed": observed,
        "passed": observed == expected,
        "detail": detail,
    }


def collect_report(
    *,
    task_dir: Path,
    denied_paths: dict[str, str],
    external: tuple[str, int] | None,
) -> dict[str, object]:
    """Run the whole checklist and return the evidence document."""

    probes: dict[str, dict[str, object]] = {}
    for label in sorted(denied_paths):
        path = denied_paths[label]
        observed, detail = probe_denied_path(path)
        probes[label] = _record("denied", observed, detail)

    observed, detail = probe_loopback()
    probes["loopback"] = _record("ok", observed, detail)

    if external is not None:
        observed, detail = probe_external(*external)
        probes["external_https"] = _record("denied", observed, detail)

    observed, detail = probe_task_dir_write(task_dir)
    probes["task_dir_write"] = _record("ok", observed, detail)

    failed = sorted(name for name, item in probes.items() if not item["passed"])
    return {
        "version": PROBE_VERSION,
        "probes": probes,
        "ok": not failed,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# configuration / I/O
# ---------------------------------------------------------------------------


def _require_absolute(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute path")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _require_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or value in {".", ".."}:
        raise ValueError(f"{label} must be a plain file name")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _load_config(task_dir: Path) -> dict[str, object]:
    document = json.loads((task_dir / CONFIG_NAME).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("probe config must be an object")

    raw_paths = document.get("denied_paths", {})
    if not isinstance(raw_paths, dict):
        raise ValueError("denied_paths must be an object")
    denied: dict[str, str] = {}
    for label, path in raw_paths.items():
        if not isinstance(label, str) or not label:
            raise ValueError("denied path labels must be non-empty strings")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in label):
            raise ValueError("denied path labels contain control characters")
        denied[label] = _require_absolute(path, f"denied path {label}")

    raw_external = document.get("external")
    external: tuple[str, int] | None = None
    if raw_external is None:
        external = DEFAULT_EXTERNAL
    elif isinstance(raw_external, dict):
        host = raw_external.get("host")
        port = raw_external.get("port")
        if not isinstance(host, str) or not host:
            raise ValueError("external host must be a non-empty string")
        if type(port) is not int or not 1 <= port <= 65_535:
            raise ValueError("external port must be an integer port")
        external = (host, port)
    else:
        raise ValueError("external must be an object or absent")

    linger = document.get("linger_seconds", 60)
    if type(linger) is not int or not 0 <= linger <= MAX_LINGER_SECONDS:
        raise ValueError("linger_seconds out of range")

    return {
        "denied_paths": denied,
        "external": external,
        "out": _require_name(document.get("out", DEFAULT_OUT_NAME), "out"),
        "release": _require_name(
            document.get("release", DEFAULT_RELEASE_NAME), "release"
        ),
        "linger_seconds": linger,
    }


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _linger(release: Path, seconds: int) -> None:
    """Wait for the test to release the unit so its properties stay inspectable."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if release.exists():
            return
        time.sleep(LINGER_POLL_SECONDS)


def main() -> int:
    task_dir = DEFAULT_TASK_DIR
    try:
        config = _load_config(task_dir)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return EXIT_CONFIG_ERROR

    extra_paths = config["denied_paths"]
    external = config["external"]
    denied = dict(DEFAULT_DENIED_PATHS)
    if isinstance(extra_paths, dict):
        denied.update(extra_paths)

    report = collect_report(
        task_dir=task_dir,
        denied_paths=denied,
        external=external if isinstance(external, tuple) else None,
    )
    document = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")
    try:
        _write_atomic(task_dir / str(config["out"]), document)
    except OSError:
        return EXIT_CONFIG_ERROR

    _linger(task_dir / str(config["release"]), int(config["linger_seconds"]))
    return EXIT_OK if report["ok"] else EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
