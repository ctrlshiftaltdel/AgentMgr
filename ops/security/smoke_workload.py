#!/usr/bin/env python3
"""Synthetic managed-task workload and negative probes (Issue #82).

This file is deployed read-only into the transient unit as ``/workload.py`` and
is started by systemd as ``/usr/bin/python3 /workload.py`` with no arguments.
It is deliberately standard-library only: the unit's sandbox exposes nothing
but its credential, its state directory, a private ``/tmp`` and the one bound
gateway socket.

Behaviour
---------

1. Wait for the harness to drop ``$STATE_DIRECTORY/plan.json`` (the state
   directory is created by systemd, so the plan cannot exist before start).
2. Run the probes named by the plan.  Probe ids, labels and errno names are the
   only things recorded; the bearer token, real paths, IP addresses and
   exception text never reach the result file.
3. Write ``$STATE_DIRECTORY/result.json`` and exit.  In ``matrix`` mode the
   process then holds (interruptible by SIGTERM with a short, real shutdown
   grace period) so the harness can drive controller-side probes against the
   still-running unit.

Modes (``plan["mode"]``)
------------------------

``connect``  minimal gateway ``connect()`` only; used once per network profile.
``matrix``   the negative probe matrix plus the allowed gateway read.
``sleep``    a plain sleeping process; used for the TTL/RuntimeMaxSec probe.

Exit codes: ``0`` all expectations matched, ``3`` a probe or the gateway connect
failed, ``4`` plan/state unavailable, ``5`` credential invalid.  The harness
records the real code and never needs the process to explain itself.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import sys
import time

__all__ = [
    "EXIT_CONFIG",
    "EXIT_OK",
    "EXIT_PROBE_FAILED",
    "EXIT_UNAVAILABLE",
    "GATEWAY_SOCKET",
    "PROTOCOL_VERSION",
    "READ_CAPABILITY",
    "WorkloadError",
    "gateway_call",
    "main",
    "probe_filesystem",
    "probe_network",
    "probe_privileges",
    "read_credential",
    "write_result",
]

#: Fixed in-unit bind mount target of the gateway socket (design §8.1).
GATEWAY_SOCKET = "/run/agentmgr/gateway.sock"
PROTOCOL_VERSION = "1.0.0"
READ_CAPABILITY = "resource.read@1.0.0"

CREDENTIAL_DIRECTORY_ENV = "CREDENTIALS_DIRECTORY"
CREDENTIAL_NAME = "task-credential"
STATE_DIRECTORY_ENV = "STATE_DIRECTORY"
PLAN_NAME = "plan.json"
RESULT_NAME = "result.json"

PLAN_WAIT_SECONDS = 25.0
HOLD_SECONDS = 90.0
STOP_GRACE_SECONDS = 1.3
SOCKET_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 65536

EXIT_OK = 0
EXIT_PROBE_FAILED = 3
EXIT_CONFIG = 4
EXIT_UNAVAILABLE = 5

_DENIED_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.ENOENT,
        errno.EPERM,
        errno.EROFS,
        errno.ENOTDIR,
        errno.ELOOP,
    }
)
_NETWORK_DENIED_ERRNOS = frozenset(
    {errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES, errno.EINVAL}
)

_stop_signal: int | None = None


class WorkloadError(Exception):
    """A stable failure token; never carries the token, a path or errno text."""


def _errno_name(exc: OSError) -> str:
    code = exc.errno
    if isinstance(code, int):
        return errno.errorcode.get(code, f"errno_{code}")
    return type(exc).__name__


def read_credential() -> str:
    """Return the task credential from ``$CREDENTIALS_DIRECTORY``.

    The value is never logged, stored in the result or interpolated into an
    exception: failures raise ``WorkloadError`` with a stable token only.
    """

    directory = os.environ.get(CREDENTIAL_DIRECTORY_ENV)
    if not directory:
        raise WorkloadError("credential_directory_missing")
    path = os.path.join(directory, CREDENTIAL_NAME)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4097)
    except OSError as exc:
        raise WorkloadError("credential_unreadable") from None
    if len(raw) > 4096:
        raise WorkloadError("credential_invalid")
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise WorkloadError("credential_invalid") from None
    if not token.startswith("agx1.") or len(token) > 4096:
        raise WorkloadError("credential_invalid")
    return token


def gateway_call(
    capability: str,
    resource: str,
    payload: dict[str, object] | None = None,
    *,
    token: str | None = None,
    socket_path: str | os.PathLike[str] = GATEWAY_SOCKET,
    timeout: float = SOCKET_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Send one NDJSON request and return the parsed response.

    ``token`` defaults to the unit credential; it is only ever placed inside the
    request line, never in an error message.
    """

    if not capability or not resource:
        raise WorkloadError("request_invalid")
    bearer = read_credential() if token is None else token
    request = {
        "version": PROTOCOL_VERSION,
        "token": bearer,
        "capability": capability,
        "resource": resource,
        "payload": {} if payload is None else payload,
    }
    line = json.dumps(request, separators=(",", ":"), ensure_ascii=True)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(os.fspath(socket_path))
        connection.sendall(line.encode("ascii") + b"\n")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise WorkloadError("response_too_large")
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
    except TimeoutError:
        raise WorkloadError("gateway_timeout") from None
    except OSError as exc:
        raise WorkloadError(f"gateway_{_errno_name(exc).lower()}") from None
    finally:
        connection.close()

    raw = b"".join(chunks)
    if not raw:
        raise WorkloadError("gateway_empty")
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WorkloadError("gateway_bad_response") from None
    if not isinstance(response, dict):
        raise WorkloadError("gateway_bad_response")
    return response


def probe_filesystem(
    targets: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Run read/list/write probes; every entry records an id and an errno name.

    ``targets`` entries are ``{"id", "label", "path", "op"?}``.  Only the
    label/id/errno are copied to the result: a real path (for example the host
    home directory) never leaves the unit.
    """

    results: list[dict[str, object]] = []
    for target in targets:
        probe_id = target.get("id")
        label = target.get("label") or probe_id
        path = target.get("path")
        operation = target.get("op", "read")
        entry: dict[str, object] = {
            "id": probe_id,
            "label": label,
            "expected": "denied",
            "observed": None,
            "errno": None,
            "passed": False,
        }
        if not isinstance(path, str) or not path:
            entry["observed"] = "config_invalid"
            results.append(entry)
            continue
        try:
            if operation == "read":
                with open(path, "rb") as handle:
                    handle.read(1)
            elif operation == "list":
                os.listdir(path)
            elif operation == "write":
                fd = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    os.write(fd, b"probe\n")
                finally:
                    os.close(fd)
                try:
                    os.unlink(path)
                except OSError:
                    pass
            else:
                entry["observed"] = "config_invalid"
                results.append(entry)
                continue
        except OSError as exc:
            entry["observed"] = _errno_name(exc)
            entry["errno"] = exc.errno
            entry["passed"] = exc.errno in _DENIED_ERRNOS
        else:
            entry["observed"] = f"{operation}_allowed"
        results.append(entry)
    return results


def probe_network(
    targets: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Probe ``AF_INET`` creation and IP connects; the address is not recorded."""

    results: list[dict[str, object]] = []

    socket_entry: dict[str, object] = {
        "id": "socket-af-inet",
        "label": "socket_af_inet",
        "expected": "network_denied",
        "observed": None,
        "errno": None,
        "passed": False,
    }
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        socket_entry["observed"] = _errno_name(exc)
        socket_entry["errno"] = exc.errno
        socket_entry["passed"] = exc.errno in _NETWORK_DENIED_ERRNOS
    else:
        probe.close()
        socket_entry["observed"] = "socket_created"
    results.append(socket_entry)

    for target in targets:
        entry: dict[str, object] = {
            "id": target.get("id"),
            "label": target.get("label") or target.get("id"),
            "expected": "network_denied",
            "observed": None,
            "errno": None,
            "passed": False,
        }
        host = target.get("host")
        port = target.get("port")
        if not isinstance(host, str) or not isinstance(port, int):
            entry["observed"] = "config_invalid"
            results.append(entry)
            continue
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                connection.settimeout(1.0)
                connection.connect((host, port))
            finally:
                connection.close()
        except OSError as exc:
            entry["observed"] = _errno_name(exc)
            entry["errno"] = exc.errno
            entry["passed"] = exc.errno in _NETWORK_DENIED_ERRNOS
        else:
            entry["observed"] = "connected"
        results.append(entry)
    return results


def _proc_status() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if separator:
                    values[key.strip()] = value.strip()
    except OSError:
        return values
    return values


def _visible_process_uids() -> list[int] | None:
    uids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", "r", encoding="utf-8") as handle:
                for line in handle:
                    key, separator, value = line.partition(":")
                    if separator and key.strip() == "Uid":
                        uids.append(int(value.split()[0]))
                        break
        except (OSError, ValueError, IndexError):
            continue
    return uids


def probe_privileges() -> dict[str, object]:
    """Read capability/no-new-privs evidence and attempt a namespace escape."""

    status = _proc_status()
    cap_eff = status.get("CapEff", "")
    no_new_privs = status.get("NoNewPrivs", "")
    uid = os.getuid()

    uids = _visible_process_uids()
    only_self = bool(uids) and all(item == uid for item in uids)

    unshare: dict[str, object] = {
        "expected": "EPERM",
        "observed": None,
        "errno": None,
        "passed": False,
    }
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
    except OSError as exc:
        unshare["observed"] = _errno_name(exc)
        unshare["errno"] = exc.errno
        unshare["passed"] = exc.errno == errno.EPERM
    except AttributeError:
        unshare["observed"] = "unavailable"
    else:
        unshare["observed"] = "namespace_created"

    return {
        "cap_eff": cap_eff if cap_eff else "unavailable",
        "no_new_privs": no_new_privs if no_new_privs else "unavailable",
        "uid": uid,
        "visible_process_uids": uids if uids is not None else [],
        "proc_only_self": only_self,
        "unshare": unshare,
    }


def write_result(state_dir: str, result: dict[str, object]) -> str:
    """Write ``result.json`` atomically enough for one reader, mode ``0600``."""

    path = os.path.join(state_dir, RESULT_NAME)
    raw = json.dumps(result, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd != -1:
            os.close(fd)
    return path


def _load_plan(state_dir: str) -> dict[str, object] | None:
    path = os.path.join(state_dir, PLAN_NAME)
    deadline = time.monotonic() + PLAN_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except OSError:
            time.sleep(0.1)
            continue
        except ValueError:
            # ``install`` can expose a partially copied file for a moment.
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)
            continue
        if isinstance(value, dict):
            return value
        return None
    return None


def _install_stop_handler() -> None:
    def _handle(signum: int, _frame: object) -> None:
        global _stop_signal
        _stop_signal = signum

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _handle)
        except (OSError, ValueError):
            pass


def _sleep_interruptible(seconds: float) -> str:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if _stop_signal is not None:
            time.sleep(STOP_GRACE_SECONDS)
            return "sigterm"
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return "timer"


def _state_dir_writable(state_dir: str) -> bool:
    path = os.path.join(state_dir, ".write-probe")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"ok\n")
        finally:
            os.close(fd)
        os.unlink(path)
        return True
    except OSError:
        return False


def _probe_gateway(
    plan: dict[str, object], token: str
) -> tuple[list[dict[str, object]], dict[str, object]]:
    results: list[dict[str, object]] = []
    allowed: dict[str, object] = {"ok": False, "resource_id": None, "value_matches": False}
    entries = plan.get("gateway")
    if not isinstance(entries, list):
        entries = []
    for spec in entries:
        if not isinstance(spec, dict):
            continue
        probe_id = spec.get("id")
        entry: dict[str, object] = {
            "id": probe_id,
            "label": spec.get("label") or probe_id,
            "expected": spec.get("expected"),
            "observed": None,
            "passed": False,
        }
        capability = spec.get("capability")
        resource = spec.get("resource")
        if not isinstance(capability, str) or not isinstance(resource, str):
            entry["observed"] = "config_invalid"
            results.append(entry)
            continue
        try:
            response = gateway_call(capability, resource, token=token)
        except WorkloadError as exc:
            entry["observed"] = f"client_error:{exc}"
            results.append(entry)
            continue
        if spec.get("expected") == "allow":
            body = response.get("result")
            body = body if isinstance(body, dict) else {}
            content = body.get("body")
            content = content if isinstance(content, dict) else {}
            matched = (
                response.get("ok") is True
                and body.get("resource_id") == resource
                and content.get("value") == spec.get("expected_value")
            )
            entry["observed"] = "ok" if response.get("ok") is True else response.get(
                "reason"
            )
            entry["passed"] = matched
            allowed = {
                "ok": response.get("ok") is True,
                "resource_id": body.get("resource_id"),
                "value_matches": content.get("value") == spec.get("expected_value"),
            }
        else:
            observed = (
                "ok" if response.get("ok") is True else response.get("reason")
            )
            entry["observed"] = observed
            entry["passed"] = observed == spec.get("expected")
        results.append(entry)
    return results, allowed


def main(argv: list[str] | None = None) -> int:
    del argv  # The unit argv is fixed; all input arrives through files/env.
    state_dir = os.environ.get(STATE_DIRECTORY_ENV, "")
    if not state_dir or not os.path.isdir(state_dir):
        return EXIT_CONFIG

    plan = _load_plan(state_dir)
    if plan is None:
        try:
            write_result(state_dir, {"error": "plan_missing", "probes": []})
        except OSError:
            pass
        return EXIT_CONFIG

    mode = plan.get("mode")
    if mode == "connect":
        result: dict[str, object] = {
            "role": plan.get("role", "profile"),
            "probes": [],
            "gateway_connect": {"connected": False, "errno": None},
        }
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(SOCKET_TIMEOUT_SECONDS)
            connection.connect(GATEWAY_SOCKET)
        except OSError as exc:
            result["gateway_connect"] = {
                "connected": False,
                "errno": _errno_name(exc),
            }
        else:
            result["gateway_connect"] = {"connected": True, "errno": None}
        finally:
            connection.close()
        try:
            write_result(state_dir, result)
        except OSError:
            return EXIT_CONFIG
        return (
            EXIT_OK
            if result["gateway_connect"]["connected"] is True
            else EXIT_PROBE_FAILED
        )

    if mode == "sleep":
        # No SIGTERM handler here on purpose: the unit is killed by the default
        # disposition, so systemd records the RuntimeMaxSec cause (Result=timeout)
        # instead of a clean handler exit.
        seconds = plan.get("sleep_seconds", 30)
        time.sleep(float(seconds) if isinstance(seconds, (int, float)) else 30.0)
        try:
            write_result(state_dir, {"role": plan.get("role", "sleep"), "probes": []})
        except OSError:
            return EXIT_CONFIG
        return EXIT_OK

    if mode != "matrix":
        try:
            write_result(state_dir, {"error": "mode_invalid", "probes": []})
        except OSError:
            pass
        return EXIT_CONFIG

    # matrix mode: the stop handler gives systemd an orderly shutdown whose exit
    # timestamp is at least one second after the stop request, so the supervisor
    # can order it against the stop without same-second ambiguity.
    _install_stop_handler()
    try:
        token = read_credential()
    except WorkloadError as exc:
        try:
            write_result(
                state_dir, {"error": str(exc), "probes": []}
            )
        except OSError:
            pass
        return EXIT_UNAVAILABLE

    result = {
        "role": plan.get("role"),
        "probes": [],
        "process": {},
        "gateway_allowed": {},
        "state_dir_writable": False,
    }
    filesystem = plan.get("filesystem")
    if isinstance(filesystem, list):
        result["probes"].extend(probe_filesystem(filesystem))
    network = plan.get("network")
    if isinstance(network, list):
        result["probes"].extend(probe_network(network))
    gateway_probes, allowed = _probe_gateway(plan, token)
    result["probes"].extend(gateway_probes)
    result["gateway_allowed"] = allowed
    result["process"] = probe_privileges()
    result["state_dir_writable"] = _state_dir_writable(state_dir)

    try:
        write_result(state_dir, result)
    except OSError:
        return EXIT_CONFIG

    hold = plan.get("hold_seconds", HOLD_SECONDS)
    hold_seconds = float(hold) if isinstance(hold, (int, float)) else HOLD_SECONDS
    _sleep_interruptible(max(0.0, hold_seconds))
    return EXIT_OK if all(
        entry.get("passed") is True for entry in result["probes"]
    ) else EXIT_PROBE_FAILED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
