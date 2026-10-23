"""systemd argv, ``systemctl show`` parsing and privileged ops adapters (#82).

This module is the only place that turns a validated :class:`ManagedTaskSpec`
into a systemd invocation.  It performs no I/O until an ops adapter is called;
the argv builders and observation mapping are pure functions.

Safety rules
------------

* Unit names are derived by :func:`unit_name_for_task`, re-checked against
  ``^agentmgr-task-[0-9a-f]{12}$`` and cross-checked against the spec's task ID.
  No caller-supplied string reaches an argv slot that is not a fixed constant,
  a validated integer or an already validated unit name.
* Every command is an argv list passed to :func:`subprocess.run` with
  ``shell=False`` (default); there is no ``sh -c``, no string concatenation and
  no host shell.
* The credential token is only ever written to a source file or sent over
  stdin.  It never appears in argv, the environment, an error detail or a
  ``repr``.
* Cleanup only addresses the four paths derived from a validated unit name:
  ``credentials/<unit>.json``, ``specs/<unit>.json``, the private state
  directory and its ``/var/lib`` compatibility symlink.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal, Protocol

from app.security.boundary import (
    MAX_CPU_QUOTA_PERCENT,
    MAX_MEMORY_MAX_MB,
    MAX_TASKS_MAX,
    MAX_TTL_SECONDS,
    MIN_CPU_QUOTA_PERCENT,
    MIN_MEMORY_MAX_MB,
    MIN_TASKS_MAX,
    MIN_TTL_SECONDS,
    NETWORK_PROFILE_PRIVATE_NETNS,
    NETWORK_PROFILES,
    ManagedTaskSpec,
    RuntimeObservation,
    unit_name_for_task,
)
from app.security.errors import SecurityError

__all__ = [
    "DEFAULT_RUNTIME_ROOT",
    "DEFAULT_STATE_LINK_ROOT",
    "DEFAULT_STATE_ROOT",
    "RUN_TIMEOUT_SECONDS",
    "HelperSystemdOps",
    "NetworkProfile",
    "SudoSystemdOps",
    "SystemdOps",
    "SystemdUnitConfig",
    "build_systemctl_show_argv",
    "build_systemd_run_argv",
    "observation_from_show",
    "parse_systemctl_show",
    "parse_systemd_timestamp",
    "run_checked",
]

#: Fixed command timeout; a hung privileged call must not block the supervisor.
RUN_TIMEOUT_SECONDS = 15

#: Runtime, state and compatibility-link roots (design §7.3/§8.3).
DEFAULT_RUNTIME_ROOT = Path("/run/agentmgr-exec")
DEFAULT_STATE_ROOT = Path("/var/lib/private/agentmgr-exec")
DEFAULT_STATE_LINK_ROOT = Path("/var/lib/agentmgr-exec")

_CAPABILITY_NAME = "task-credential"
_GATEWAY_UNIT_PATH = "/run/agentmgr/gateway.sock"
_WORKLOAD_UNIT_PATH = "/workload.py"
_PYTHON_EXECUTABLE = "/usr/bin/python3"
_DESCRIPTION = "agentmgr-exec"
_UNIT_PATTERN = re.compile(r"^agentmgr-task-[0-9a-f]{12}$")
_SHOW_PROPERTIES = (
    "ActiveState,SubState,MainPID,Result,ExecMainStatus,LoadState,"
    "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp"
)

#: Inaccessible control-plane/data paths common to both network profiles.
#: ``/run/systemd`` and ``/run/dbus`` close the host systemd private socket and
#: D-Bus to ``AF_UNIX`` connects in both profiles (design §8.1/§15.1).
_BASE_INACCESSIBLE_PATHS: tuple[str, ...] = (
    "/etc/agentmgr",
    "/var/lib/agentmgr",
    "/var/lib/postgresql",
    "/srv",
    "/opt",
    "/run/systemd",
    "/run/dbus",
)

NetworkProfile = Literal["private_netns", "af_unix_only_fallback"]

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


def _validate_unit(unit: object) -> str:
    """Return a safe unit name or fail closed before any argv is built."""

    if not isinstance(unit, str) or _UNIT_PATTERN.fullmatch(unit) is None:
        raise SecurityError("invalid_spec", "unit_name_invalid")
    return unit


def _validated_int(value: object, *, minimum: int, maximum: int) -> int:
    """Reject ``bool``/non-int and out-of-range limits (design §7.2)."""

    if type(value) is not int or not minimum <= value <= maximum:
        raise SecurityError("invalid_spec", "limit_out_of_range")
    return value


def _validated_path(value: object) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or "\x00" in str(value)
    ):
        raise SecurityError("invalid_spec", "source_path_invalid")
    return value


@dataclass(frozen=True, slots=True)
class SystemdUnitConfig:
    """Everything needed to start one unit, with no untrusted path field.

    ``network_profile`` selects design §8.4's default private netns or the
    AF_UNIX-only fallback.  Both keep the same filesystem, capability and
    syscall sandbox; only the network properties differ.
    """

    unit: str
    spec: ManagedTaskSpec
    credential_source: Path
    workload_source: Path
    gateway_socket_source: Path
    network_profile: NetworkProfile = NETWORK_PROFILE_PRIVATE_NETNS

    def __post_init__(self) -> None:
        unit = _validate_unit(self.unit)
        if not isinstance(self.spec, ManagedTaskSpec):
            raise SecurityError("invalid_spec", "spec_invalid")
        if unit_name_for_task(self.spec.task_id) != unit:
            raise SecurityError("invalid_spec", "unit_name_mismatch")
        if self.network_profile not in NETWORK_PROFILES:
            raise SecurityError("invalid_spec", "network_profile_invalid")
        _validated_path(self.credential_source)
        _validated_path(self.workload_source)
        _validated_path(self.gateway_socket_source)


def build_systemd_run_argv(config: SystemdUnitConfig) -> list[str]:
    """Return the fixed ``systemd-run`` argv for ``config`` (design §8.1)."""

    if not isinstance(config, SystemdUnitConfig):
        raise SecurityError("invalid_spec", "config_invalid")
    unit = _validate_unit(config.unit)
    if unit_name_for_task(config.spec.task_id) != unit:
        raise SecurityError("invalid_spec", "unit_name_mismatch")

    ttl = _validated_int(
        config.spec.ttl_seconds,
        minimum=MIN_TTL_SECONDS,
        maximum=MAX_TTL_SECONDS,
    )
    memory = _validated_int(
        config.spec.memory_max_mb,
        minimum=MIN_MEMORY_MAX_MB,
        maximum=MAX_MEMORY_MAX_MB,
    )
    cpu = _validated_int(
        config.spec.cpu_quota_percent,
        minimum=MIN_CPU_QUOTA_PERCENT,
        maximum=MAX_CPU_QUOTA_PERCENT,
    )
    tasks = _validated_int(
        config.spec.tasks_max,
        minimum=MIN_TASKS_MAX,
        maximum=MAX_TASKS_MAX,
    )
    credential = _validated_path(config.credential_source)
    workload = _validated_path(config.workload_source)
    gateway = _validated_path(config.gateway_socket_source)
    private_netns = config.network_profile == NETWORK_PROFILE_PRIVATE_NETNS
    inaccessible = [*_BASE_INACCESSIBLE_PATHS]

    properties = [
        "--property=DynamicUser=yes",
        "--property=StateDirectory=agentmgr-exec/%N",
        "--property=StateDirectoryMode=0700",
        f"--property=RuntimeMaxSec={ttl}s",
        "--property=TimeoutStopSec=5",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        *(
            f"--property=InaccessiblePaths={path}"
            for path in inaccessible
        ),
        "--property=PrivateTmp=yes",
        "--property=PrivateDevices=yes",
        *(["--property=PrivateNetwork=yes"] if private_netns else []),
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=IPAddressDeny=any",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=",
        "--property=AmbientCapabilities=",
        "--property=SystemCallFilter=@system-service",
        "--property=SystemCallArchitectures=native",
        "--property=SystemCallErrorNumber=EPERM",
        "--property=RestrictNamespaces=yes",
        "--property=LockPersonality=yes",
        "--property=MemoryDenyWriteExecute=yes",
        "--property=RestrictRealtime=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=RemoveIPC=yes",
        "--property=ProtectClock=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectHostname=yes",
        "--property=ProtectProc=invisible",
        "--property=ProcSubset=pid",
        "--property=UMask=0077",
        f"--property=MemoryMax={memory}M",
        f"--property=CPUQuota={cpu}%",
        f"--property=TasksMax={tasks}",
        "--property=LimitNOFILE=256",
        f"--property=LoadCredential={_CAPABILITY_NAME}:{credential}",
        (
            f"--property=BindReadOnlyPaths={gateway}:"
            f"{_GATEWAY_UNIT_PATH}"
        ),
        f"--property=BindReadOnlyPaths={workload}:{_WORKLOAD_UNIT_PATH}",
    ]
    return [
        "systemd-run",
        f"--unit={unit}",
        f"--description={_DESCRIPTION}",
        *properties,
        _PYTHON_EXECUTABLE,
        _WORKLOAD_UNIT_PATH,
    ]


def build_systemctl_show_argv(unit: str) -> list[str]:
    """Return the fixed ``systemctl show`` argv used for evidence."""

    return ["systemctl", "show", "-p", _SHOW_PROPERTIES, _validate_unit(unit)]


def parse_systemctl_show(text: str) -> dict[str, str]:
    """Parse ``Key=Value`` lines from ``systemctl show`` output.

    Blank and malformed lines are ignored; a value may itself contain ``=``
    because only the first separator is significant.
    """

    if not isinstance(text, str):
        raise ValueError("text must be a string")
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            values[key] = value
    return values


def _local_zone(zone: str) -> tzinfo | None:
    """Map a systemd zone token to a concrete tzinfo, or ``None``."""

    if zone.upper() in {"UTC", "GMT", "Z"}:
        return UTC
    if zone in time.tzname:
        return datetime.now().astimezone().tzinfo
    return None


def parse_systemd_timestamp(value: str) -> datetime | None:
    """Parse a systemd timestamp into an aware UTC datetime.

    Accepts the classic ``Fri 2026-01-02 03:04:05 UTC`` form, ``@<epoch>`` and
    ISO-8601.  ``n/a``, empty and unparseable values return ``None`` so the
    caller reports missing evidence instead of inventing a time.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"n/a", "-", "never"}:
        return None

    if text.startswith("@"):
        raw = text[1:]
        if not raw:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            return None
        if not math.isfinite(seconds):
            return None
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None

    iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    head, separator, zone = text.rpartition(" ")
    if not separator:
        return None
    tzinfo = _local_zone(zone)
    if tzinfo is None:
        return None
    for shape in ("%a %Y-%m-%d %H:%M:%S", "%a %Y-%m-%d %H:%M:%S.%f"):
        try:
            parsed = datetime.strptime(head, shape)
        except ValueError:
            continue
        return parsed.replace(tzinfo=tzinfo).astimezone(UTC)
    return None


def _text(show: Mapping[str, str], key: str) -> str:
    value = show.get(key)
    return value if isinstance(value, str) else ""


def _int_or_none(value: str) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value, 10)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _observation(
    state: str,
    process_gone: bool,
    stop_confirmed: bool,
    never_started: bool,
    main_pid: int,
    result: str,
    exec_main_status: int | None,
) -> RuntimeObservation:
    return RuntimeObservation(
        state=state,
        process_gone=process_gone,
        stop_confirmed=stop_confirmed,
        never_started=never_started,
        main_pid=main_pid,
        result=result,
        exec_main_status=exec_main_status,
    )


def observation_from_show(
    show: Mapping[str, str],
    *,
    stop_requested_at: datetime | None = None,
    had_start_evidence: bool = False,
) -> RuntimeObservation:
    """Map one ``systemctl show`` projection to design §11.1 facts.

    ``process_gone``, ``stop_confirmed`` and ``never_started`` are independent.
    A natural ``completed``/``failed`` is never reported as a confirmed stop;
    ``not_started`` is never reported as gone.  Missing or unparseable fields
    degrade to ``unknown``/``False`` rather than to a guess.
    """

    if not isinstance(show, Mapping):
        raise ValueError("show must be a mapping")

    if stop_requested_at is not None:
        if (
            not isinstance(stop_requested_at, datetime)
            or stop_requested_at.utcoffset() is None
        ):
            raise SecurityError("invalid_spec", "stop_requested_invalid")
        stop_requested_at = stop_requested_at.astimezone(UTC)

    active_state = _text(show, "ActiveState")
    load_state = _text(show, "LoadState")
    result = _text(show, "Result") or "unknown"
    main_pid_value = _int_or_none(_text(show, "MainPID"))
    main_pid = main_pid_value if main_pid_value is not None else 0
    exec_main_status = _int_or_none(_text(show, "ExecMainStatus"))

    exit_text = _text(show, "ExecMainExitTimestamp")
    exit_at = parse_systemd_timestamp(exit_text)
    start_evidence = had_start_evidence or any(
        moment is not None
        for moment in (
            parse_systemd_timestamp(_text(show, "ActiveEnterTimestamp")),
            parse_systemd_timestamp(_text(show, "ExecMainStartTimestamp")),
            exit_at,
        )
    )

    if load_state == "not-found":
        if start_evidence:
            return _observation(
                "unknown", True, False, False, main_pid, result, exec_main_status
            )
        return _observation(
            "not_started", False, False, True, main_pid, result, exec_main_status
        )

    if active_state == "activating":
        return _observation(
            "starting", False, False, False, main_pid, result, exec_main_status
        )
    if active_state == "active":
        return _observation(
            "running", False, False, False, main_pid, result, exec_main_status
        )
    if active_state == "deactivating":
        return _observation(
            "stopping", False, False, False, main_pid, result, exec_main_status
        )

    if active_state not in {"inactive", "failed"} or main_pid_value != 0:
        return _observation(
            "unknown", False, False, False, main_pid, result, exec_main_status
        )

    # An inactive/dead unit with no start or exit timestamp is indistinguishable
    # from a unit that never started; do not claim it ran (and do not report
    # ``not_started`` for a loaded unit).
    if not start_evidence:
        return _observation(
            "unknown", False, False, False, main_pid, result, exec_main_status
        )

    if result == "timeout":
        return _observation(
            "timed_out", True, True, False, main_pid, result, exec_main_status
        )

    # A requested stop can only be ordered against a real exit timestamp.  With
    # no timestamp, claiming a confirmed stop could downgrade a natural end, so
    # the evidence is reported as unknown rather than guessed (design §11.5).
    if stop_requested_at is not None and exit_at is None:
        return _observation(
            "unknown", True, False, False, main_pid, result, exec_main_status
        )

    ambiguous = False
    if stop_requested_at is not None and exit_at is not None:
        same_second = exit_at.replace(microsecond=0) == stop_requested_at.replace(
            microsecond=0
        )
        # Classic systemd timestamps are whole-second.  If the stop landed in
        # that same second, the true order is unknowable; an exact tie is
        # equally ambiguous.  Never guess either way.
        exit_has_subsecond = "." in exit_text
        ambiguous = same_second and (
            not exit_has_subsecond or exit_at == stop_requested_at
        )

    if ambiguous:
        return _observation(
            "unknown", True, False, False, main_pid, result, exec_main_status
        )

    natural_end = (
        exit_at is not None
        and stop_requested_at is not None
        and exit_at < stop_requested_at
    )
    stop_observed = stop_requested_at is not None and not natural_end

    if result == "success" and active_state == "inactive":
        if exec_main_status == 0:
            if stop_observed:
                return _observation(
                    "cancelled", True, True, False, main_pid, result, exec_main_status
                )
            return _observation(
                "completed", True, False, False, main_pid, result, exec_main_status
            )
        # success without a zero exit status is missing/malformed evidence.
        return _observation(
            "unknown", True, False, False, main_pid, result, exec_main_status
        )

    if result == "unknown":
        return _observation(
            "unknown", True, False, False, main_pid, result, exec_main_status
        )

    if stop_observed:
        return _observation(
            "cancelled", True, True, False, main_pid, result, exec_main_status
        )
    return _observation(
        "failed", True, False, False, main_pid, result, exec_main_status
    )


def run_checked(
    argv: list[str], input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one argv array without a shell, with a fixed timeout.

    The input is written to stdin (never argv/environment) and the command's
    output is captured.  A non-zero exit raises ``CalledProcessError``; the ops
    adapters translate that into a stable :class:`SecurityError`.
    """

    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) for item in argv
    ):
        raise ValueError("argv must be a non-empty list of strings")
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=True,
        shell=False,
    )


def _invoke(
    runner: Runner,
    argv: list[str],
    input_text: str | None,
    *,
    code: str,
    detail: str,
) -> subprocess.CompletedProcess[str]:
    """Run through ``runner`` and translate failures into stable errors.

    The original exception is deliberately **not** chained: ``CalledProcessError``
    and ``TimeoutExpired`` carry ``cmd``/``stdout``/``stderr``, which can contain
    root paths and (for ``tee``) the echoed credential.  The raise happens after
    the ``except`` block so neither ``__cause__`` nor ``__context__`` retains it.
    """

    failure: tuple[str, str] | None = None
    completed: subprocess.CompletedProcess[str] | None = None
    try:
        completed = runner(argv, input_text)
    except subprocess.TimeoutExpired:
        failure = ("timeout", "command_timeout")
    except subprocess.CalledProcessError:
        failure = (code, detail)
    except OSError:
        failure = ("unknown", "command_unavailable")
    if failure is not None:
        raise SecurityError(*failure) from None
    if not isinstance(completed, subprocess.CompletedProcess):
        raise SecurityError("unknown", "command_invalid")
    if completed.returncode != 0:
        raise SecurityError(code, detail) from None
    return completed


class SystemdOps(Protocol):
    """Privileged systemd operations used by the supervisor."""

    def launch(
        self,
        config: SystemdUnitConfig,
        *,
        spec_text: str,
        credential_text: str,
    ) -> None: ...

    def stop(self, unit: str) -> None: ...

    def kill(self, unit: str) -> None: ...

    def show(self, unit: str) -> Mapping[str, str]: ...

    def reset_failed(self, unit: str) -> None: ...

    def remove_sources(self, unit: str) -> None: ...

    def cleanup(self, unit: str) -> None: ...


class SudoSystemdOps:
    """Direct fixed-argv ``sudo -n`` adapter for the approved test host.

    Source files are written with ``sudo -n tee`` (token via stdin) and then
    restricted to ``0600``.  The directories are expected to be pre-created
    ``root:root 0700``; this adapter never widens or creates them.  Cleanup is
    limited to paths derived from a validated unit name.
    """

    def __init__(
        self,
        runner: Runner = run_checked,
        *,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        state_root: Path = DEFAULT_STATE_ROOT,
        state_link_root: Path = DEFAULT_STATE_LINK_ROOT,
    ) -> None:
        self._runner = runner
        self._runtime_root = self._root(runtime_root)
        self._state_root = self._root(state_root)
        self._state_link_root = self._root(state_link_root)

    @staticmethod
    def _root(value: object) -> Path:
        path = Path(value)  # type: ignore[arg-type]
        if not path.is_absolute() or "\x00" in str(path):
            raise ValueError("ops roots must be absolute paths")
        return path

    @property
    def _credentials_dir(self) -> Path:
        return self._runtime_root / "credentials"

    @property
    def _specs_dir(self) -> Path:
        return self._runtime_root / "specs"

    def _write_source(self, path: Path, text: str, detail: str) -> None:
        completed = _invoke(
            self._runner,
            ["sudo", "-n", "tee", "--", str(path)],
            text,
            code="privilege_required",
            detail=detail,
        )
        # ``tee`` echoes its stdin to stdout; for credential writes that is the
        # token.  Never retain it beyond this call.
        completed.stdout = ""
        completed.stderr = ""
        _invoke(
            self._runner,
            ["sudo", "-n", "chmod", "600", "--", str(path)],
            None,
            code="privilege_required",
            detail="source_chmod_failed",
        )

    def launch(
        self,
        config: SystemdUnitConfig,
        *,
        spec_text: str,
        credential_text: str,
    ) -> None:
        if not isinstance(config, SystemdUnitConfig):
            raise SecurityError("invalid_spec", "config_invalid")
        unit = _validate_unit(config.unit)
        if not isinstance(spec_text, str) or not spec_text:
            raise SecurityError("invalid_spec", "spec_text_invalid")
        if (
            not isinstance(credential_text, str)
            or not credential_text
            or "\x00" in credential_text
        ):
            raise SecurityError("invalid_spec", "credential_text_invalid")

        expected_credential = self._credentials_dir / f"{unit}.json"
        if Path(config.credential_source) != expected_credential:
            raise SecurityError("invalid_spec", "credential_source_invalid")
        # Build (and therefore validate) before writing any source file.
        argv = ["sudo", "-n", *build_systemd_run_argv(config)]

        self._write_source(
            self._specs_dir / f"{unit}.json", spec_text, "spec_write_failed"
        )
        self._write_source(
            expected_credential, credential_text, "credential_write_failed"
        )
        _invoke(
            self._runner,
            argv,
            None,
            code="unit_start_failed",
            detail="systemd_run_failed",
        )

    def stop(self, unit: str) -> None:
        _invoke(
            self._runner,
            ["sudo", "-n", "systemctl", "stop", _validate_unit(unit)],
            None,
            code="unit_stop_unconfirmed",
            detail="stop_failed",
        )

    def kill(self, unit: str) -> None:
        _invoke(
            self._runner,
            [
                "sudo",
                "-n",
                "systemctl",
                "kill",
                "--signal=SIGKILL",
                "--kill-whom=all",
                _validate_unit(unit),
            ],
            None,
            code="unit_stop_unconfirmed",
            detail="kill_failed",
        )

    def show(self, unit: str) -> Mapping[str, str]:
        completed = _invoke(
            self._runner,
            ["sudo", "-n", *build_systemctl_show_argv(unit)],
            None,
            code="unknown",
            detail="show_failed",
        )
        return parse_systemctl_show(completed.stdout or "")

    def reset_failed(self, unit: str) -> None:
        _invoke(
            self._runner,
            ["sudo", "-n", "systemctl", "reset-failed", _validate_unit(unit)],
            None,
            code="unknown",
            detail="reset_failed_failed",
        )

    def remove_sources(self, unit: str) -> None:
        resolved = _validate_unit(unit)
        _invoke(
            self._runner,
            [
                "sudo",
                "-n",
                "rm",
                "-f",
                "--",
                str(self._credentials_dir / f"{resolved}.json"),
                str(self._specs_dir / f"{resolved}.json"),
            ],
            None,
            code="unknown",
            detail="remove_sources_failed",
        )

    def cleanup(self, unit: str) -> None:
        resolved = _validate_unit(unit)
        _invoke(
            self._runner,
            ["sudo", "-n", "systemctl", "reset-failed", resolved],
            None,
            code="unknown",
            detail="reset_failed_failed",
        )
        _invoke(
            self._runner,
            [
                "sudo",
                "-n",
                "rm",
                "-rf",
                "--",
                str(self._credentials_dir / f"{resolved}.json"),
                str(self._specs_dir / f"{resolved}.json"),
                str(self._state_root / resolved),
                str(self._state_link_root / resolved),
            ],
            None,
            code="unknown",
            detail="cleanup_failed",
        )


class HelperSystemdOps:
    """Production adapter that delegates every action to the root helper.

    The helper is invoked through ``sudo -n`` and receives one JSON request on
    stdin.  Nothing but the fixed helper path and ``sudo`` appears in argv, so
    the credential and spec never reach the process table.
    """

    def __init__(
        self, helper_path: Path, runner: Runner = run_checked
    ) -> None:
        path = Path(helper_path)
        if not path.is_absolute() or "\x00" in str(path):
            raise ValueError("helper_path must be an absolute path")
        self._helper_path = path
        self._runner = runner

    def _call(
        self,
        payload: Mapping[str, object],
        *,
        code: str,
        detail: str,
    ) -> subprocess.CompletedProcess[str]:
        request = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
        return _invoke(
            self._runner,
            ["sudo", "-n", str(self._helper_path)],
            request,
            code=code,
            detail=detail,
        )

    def launch(
        self,
        config: SystemdUnitConfig,
        *,
        spec_text: str,
        credential_text: str,
    ) -> None:
        if not isinstance(config, SystemdUnitConfig):
            raise SecurityError("invalid_spec", "config_invalid")
        unit = _validate_unit(config.unit)
        if unit_name_for_task(config.spec.task_id) != unit:
            raise SecurityError("invalid_spec", "unit_name_mismatch")
        try:
            spec = json.loads(spec_text)
        except (TypeError, ValueError) as exc:
            raise SecurityError("invalid_spec", "spec_text_invalid") from exc
        if not isinstance(spec, Mapping):
            raise SecurityError("invalid_spec", "spec_text_invalid")
        if (
            not isinstance(credential_text, str)
            or not credential_text
            or "\x00" in credential_text
        ):
            raise SecurityError("invalid_spec", "credential_text_invalid")

        self._call(
            {
                "command": "launch",
                "spec": dict(spec),
                "credential": credential_text,
            },
            code="unit_start_failed",
            detail="helper_launch_failed",
        )

    def stop(self, unit: str) -> None:
        self._call(
            {"command": "stop", "unit": _validate_unit(unit)},
            code="unit_stop_unconfirmed",
            detail="helper_stop_failed",
        )

    def kill(self, unit: str) -> None:
        self._call(
            {"command": "kill", "unit": _validate_unit(unit)},
            code="unit_stop_unconfirmed",
            detail="helper_kill_failed",
        )

    def show(self, unit: str) -> Mapping[str, str]:
        completed = self._call(
            {"command": "inspect", "unit": _validate_unit(unit)},
            code="unknown",
            detail="helper_inspect_failed",
        )
        try:
            parsed = json.loads(completed.stdout or "")
        except (TypeError, ValueError) as exc:
            raise SecurityError("unknown", "helper_output_invalid") from exc
        if isinstance(parsed, Mapping) and isinstance(parsed.get("show"), Mapping):
            parsed = parsed["show"]
        if not isinstance(parsed, Mapping):
            raise SecurityError("unknown", "helper_output_invalid")

        values: dict[str, str] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                raise SecurityError("unknown", "helper_output_invalid")
            if isinstance(value, str):
                values[key] = value
            elif isinstance(value, int) and not isinstance(value, bool):
                values[key] = str(value)
            elif value is None:
                values[key] = ""
            else:
                raise SecurityError("unknown", "helper_output_invalid")
        return values

    def reset_failed(self, unit: str) -> None:
        self._call(
            {"command": "reset-failed", "unit": _validate_unit(unit)},
            code="unknown",
            detail="helper_reset_failed",
        )

    def remove_sources(self, unit: str) -> None:
        self._call(
            {"command": "remove-sources", "unit": _validate_unit(unit)},
            code="unknown",
            detail="helper_remove_sources_failed",
        )

    def cleanup(self, unit: str) -> None:
        self._call(
            {"command": "cleanup", "unit": _validate_unit(unit)},
            code="unknown",
            detail="helper_cleanup_failed",
        )
