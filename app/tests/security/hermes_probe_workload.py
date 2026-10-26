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

Denial oracles
--------------

Every denied path carries the observation that *proves* the denial kind, so a
bare permission error cannot be mistaken for isolation:

``permission``
    Ordinary DAC denial (``EACCES``) with no inaccessible overmount on the path
    or an ancestor.  Used only for ``/etc/shadow`` as a permission baseline.

``hidden``
    The path is an inaccessible overmount (mode ``0000``, uid 0 -- what systemd's
    ``InaccessiblePaths=``/``ProtectHome=yes`` mount over a target) or is
    unreachable because such an overmount sits on an ancestor.  Reported as
    ``hidden`` with ``mask``/``ancestor_mask``.  ``ENOENT`` is also reported as
    ``absent``; the caller decides whether that is the expected proof.

``absent``
    ``ENOENT`` because the path lives on a filesystem the unit replaced (private
    ``/run`` tmpfs).  This is the expected observation for ``/run/*`` oracles.

Input (written by the test into the task directory, the only writable area)::

    /task/probe-config.json    extra oracles, external target, output names
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
import ipaddress
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

__all__ = [
    "DEFAULT_DENIED",
    "ORACLE_KINDS",
    "PROBE_VERSION",
    "collect_report",
    "probe_denied",
    "probe_external",
    "probe_loopback",
    "probe_task_dir_write",
]

PROBE_VERSION = 2

#: Fixed denial oracles: label -> (host path, expected observation).  The test
#: pre-checks each path on the host and supplies its own dynamic labels.
DEFAULT_DENIED: dict[str, tuple[str, str]] = {
    "etc_shadow": ("/etc/shadow", "permission"),
    "host_home": ("/home", "hidden"),
    "postgresql_data": ("/var/lib/postgresql", "hidden"),
    "host_systemd_run": ("/run/systemd", "absent"),
    "host_dbus": ("/run/dbus", "absent"),
    "docker_socket": ("/var/run/docker.sock", "absent"),
    "containerd_socket": ("/run/containerd/containerd.sock", "absent"),
}

ORACLE_KINDS = frozenset({"permission", "hidden", "absent"})

DEFAULT_TASK_DIR = Path("/task")
CONFIG_NAME = "probe-config.json"
DEFAULT_OUT_NAME = "probe.json"
DEFAULT_RELEASE_NAME = "probe.release"
DEFAULT_EXTERNAL = ("203.0.113.1", 443)

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


def _is_mask(info: os.stat_result) -> bool:
    """True for a systemd inaccessible overmount (mode 0000, owned by root)."""

    return stat.S_IMODE(info.st_mode) == 0 and info.st_uid == 0


def _hidden_by_mask(path: str) -> bool:
    """True when an inaccessible overmount sits on the path or an ancestor."""

    for candidate in (Path(path), *Path(path).parents):
        try:
            info = os.lstat(candidate)
        except OSError:
            # An unreadable or absent ancestor cannot be the mask proof itself;
            # keep walking up (``/home/<user>`` is unreachable *because* ``/home``
            # is an inaccessible mount).
            continue
        if _is_mask(info):
            return True
    return False


def _denied_by_permission(path: str) -> tuple[str, str]:
    if _hidden_by_mask(path):
        return "hidden", "ancestor_mask"
    return "permission", "EACCES"


def _probe_unix_socket(path: str) -> tuple[str, str]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(SOCKET_TIMEOUT_SECONDS)
    try:
        client.connect(path)
    except FileNotFoundError:
        return "absent", "ENOENT"
    except PermissionError:
        return _denied_by_permission(path)
    except OSError as error:
        return "error", _error_detail(error)
    finally:
        client.close()
    return "reached", "connected"


def probe_denied(path: str) -> tuple[str, str]:
    """Return ``(observed, detail)`` for a path that must not be reachable.

    ``observed`` is ``permission`` (ordinary DAC denial), ``hidden``
    (inaccessible overmount on the path or an ancestor), ``absent`` (ENOENT),
    ``reached`` (content or a connection was available) or ``error``.
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent", "ENOENT"
    except NotADirectoryError:
        return "absent", "ENOTDIR"
    except PermissionError:
        return _denied_by_permission(path)
    except OSError as error:
        return "error", _error_detail(error)

    if _is_mask(info):
        return "hidden", "mask"

    if stat.S_ISDIR(info.st_mode):
        try:
            entries = os.listdir(path)
        except PermissionError:
            return _denied_by_permission(path)
        except OSError as error:
            return "error", _error_detail(error)
        return "reached", f"{len(entries)}_entries"

    if stat.S_ISSOCK(info.st_mode):
        return _probe_unix_socket(path)

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except PermissionError:
        return _denied_by_permission(path)
    except OSError as error:
        return "error", _error_detail(error)
    try:
        os.read(descriptor, 16)
    except OSError as error:
        return "error", _error_detail(error)
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


def probe_external(address: str, port: int) -> tuple[str, str]:
    """Attempt one direct outbound connection to an IP literal.

    Only ``ENETUNREACH`` proves network isolation: a timeout, a permission
    error or any other failure is reported as ``error`` (DNS cannot happen
    because the caller must pass a literal address).
    """

    try:
        with socket.create_connection(
            (address, port), timeout=SOCKET_TIMEOUT_SECONDS
        ):
            return "reached", "connected"
    except OSError as error:
        detail = _error_detail(error)
        if error.errno == errno.ENETUNREACH:
            return "isolated", detail
        return "error", detail


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
    denied: dict[str, tuple[str, str]],
    external: tuple[str, int] | None,
    oracles_absent: tuple[str, ...] = (),
) -> dict[str, object]:
    """Run the whole checklist and return the evidence document."""

    probes: dict[str, dict[str, object]] = {}
    for label in sorted(denied):
        path, expected = denied[label]
        observed, detail = probe_denied(path)
        probes[label] = _record(expected, observed, detail)

    observed, detail = probe_loopback()
    probes["loopback"] = _record("ok", observed, detail)

    if external is not None:
        observed, detail = probe_external(*external)
        probes["external"] = _record("isolated", observed, detail)

    observed, detail = probe_task_dir_write(task_dir)
    probes["task_dir_write"] = _record("ok", observed, detail)

    failed = sorted(name for name, item in probes.items() if not item["passed"])
    return {
        "version": PROBE_VERSION,
        "probes": probes,
        "oracles_absent": sorted(oracles_absent),
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


def _require_label(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _load_denied(raw: object) -> dict[str, tuple[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("denied must be an object")
    denied: dict[str, tuple[str, str]] = {}
    for label, entry in raw.items():
        name = _require_label(label, "denied path label")
        if not isinstance(entry, dict):
            raise ValueError(f"denied path {name} must be an object")
        path = _require_absolute(entry.get("path"), f"denied path {name}")
        expect = entry.get("expect")
        if expect not in ORACLE_KINDS:
            raise ValueError(f"denied path {name} has an unknown expectation")
        denied[name] = (path, str(expect))
    return denied


def _load_external(raw: object) -> tuple[str, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("external must be an object or absent")
    address = raw.get("address")
    port = raw.get("port")
    if not isinstance(address, str):
        raise ValueError("external address must be an IP literal")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise ValueError("external address must be an IP literal") from None
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("external port must be an integer port")
    return address, port


def _load_config(task_dir: Path) -> dict[str, object]:
    document = json.loads((task_dir / CONFIG_NAME).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("probe config must be an object")

    raw_absent = document.get("oracles_absent", [])
    if not isinstance(raw_absent, list):
        raise ValueError("oracles_absent must be a list")
    absent = tuple(_require_label(item, "oracle label") for item in raw_absent)

    linger = document.get("linger_seconds", 60)
    if type(linger) is not int or not 0 <= linger <= MAX_LINGER_SECONDS:
        raise ValueError("linger_seconds out of range")

    return {
        "denied": _load_denied(document.get("denied")),
        "external": _load_external(document.get("external")),
        "oracles_absent": absent,
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
    except (OSError, ValueError, TypeError):
        return EXIT_CONFIG_ERROR

    denied = dict(DEFAULT_DENIED)
    extra = config["denied"]
    if isinstance(extra, dict):
        denied.update(extra)

    external = config["external"]
    absent = config["oracles_absent"]
    report = collect_report(
        task_dir=task_dir,
        denied=denied,
        external=external if isinstance(external, tuple) else None,
        oracles_absent=absent if isinstance(absent, tuple) else (),
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
