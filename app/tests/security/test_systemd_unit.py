"""systemd argv, show parsing and privileged adapter tests (#82).

Everything here is in-memory: each privileged command goes through a fake
runner, so no sudo, systemd unit or real host path is touched.  The tests pin
the exact sandbox property set of design §8.1, the launch/stop/cleanup argv
shapes, and the observation table of design §11.1.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.security.boundary import ManagedTaskSpec, unit_name_for_task
from app.security.errors import SecurityError
from app.security.systemd import (
    HelperSystemdOps,
    NetworkProfile,
    SudoSystemdOps,
    SystemdUnitConfig,
    build_systemctl_show_argv,
    build_systemd_run_argv,
    observation_from_show,
    parse_systemctl_show,
    parse_systemd_timestamp,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TASK_ID = "task_synthetic_0001"
GRANT_ID = "grnt_synthetic_0001"
CAPABILITY = "resource.read@1.0.0"
UNIT = unit_name_for_task(TASK_ID)
TOKEN = "agx1.c3ludGhldGlj.c2lnbmF0dXJl"
HELPER = Path("/opt/example/agentmgr-exec")


def _spec(**overrides: object) -> ManagedTaskSpec:
    value: dict[str, object] = {
        "task_id": TASK_ID,
        "grant_id": GRANT_ID,
        "execution_mode": "managed_execution",
        "ttl_seconds": 60,
        "cpu_quota_percent": 50,
        "memory_max_mb": 256,
        "tasks_max": 32,
        "capabilities": [CAPABILITY],
        "resource_scope": {
            "scope_type": "self",
            "resource_ids": ["res_synthetic_0001"],
            "connector_ids": [],
            "data_classifications": ["internal"],
            "exclude_resource_ids": [],
        },
        "backend": "file",
    }
    value.update(overrides)
    spec = ManagedTaskSpec.from_mapping(value)
    spec.validate(NOW)
    return spec


def _config(
    root: Path,
    *,
    spec: ManagedTaskSpec | None = None,
    network_profile: NetworkProfile = "private_netns",
) -> SystemdUnitConfig:
    resolved = spec if spec is not None else _spec()
    unit = unit_name_for_task(resolved.task_id)
    return SystemdUnitConfig(
        unit=unit,
        spec=resolved,
        credential_source=root / "credentials" / f"{unit}.json",
        workload_source=root / "workload.py",
        gateway_socket_source=root / "gateway.sock",
        network_profile=network_profile,
    )


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%a %Y-%m-%d %H:%M:%S UTC")


def _timestamp_precise(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%a %Y-%m-%d %H:%M:%S.%f UTC")


class FakeRunner:
    """Capture every argv/stdin pair and return a successful process."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.stdout = stdout

    def __call__(
        self, argv: list[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), input_text))
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


def _expected_properties(config: SystemdUnitConfig) -> list[str]:
    credential = str(config.credential_source)
    gateway = str(config.gateway_socket_source)
    workload = str(config.workload_source)
    # Optional-path syntax: ``-`` lets systemd ignore an absent path while a
    # present path remains inaccessible.
    inaccessible = [
        "-/etc/agentmgr",
        "-/var/lib/agentmgr",
        "-/var/lib/postgresql",
        "-/srv",
        "-/opt",
    ]
    private_netns = config.network_profile == "private_netns"
    return [
        "--property=DynamicUser=yes",
        f"--property=StateDirectory=agentmgr-exec/{config.unit}",
        "--property=StateDirectoryMode=0700",
        "--property=RemainAfterExit=yes",
        f"--property=RuntimeMaxSec={config.spec.ttl_seconds}s",
        "--property=TimeoutStopSec=5",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=ExitType=cgroup",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        *(
            f"--property=InaccessiblePaths={path}"
            for path in inaccessible
        ),
        "--property=TemporaryFileSystem=/run/systemd",
        "--property=TemporaryFileSystem=/run/dbus",
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
        f"--property=MemoryMax={config.spec.memory_max_mb}M",
        f"--property=CPUQuota={config.spec.cpu_quota_percent}%",
        f"--property=TasksMax={config.spec.tasks_max}",
        "--property=LimitNOFILE=256",
        f"--property=LoadCredential=task-credential:{credential}",
        f"--property=BindReadOnlyPaths={gateway}:/run/agentmgr/gateway.sock",
        f"--property=BindReadOnlyPaths={workload}:/workload.py",
    ]


@pytest.mark.parametrize(
    "profile", ["private_netns", "af_unix_only_fallback"]
)
def test_systemd_argv_exact_properties(
    tmp_path: Path, profile: NetworkProfile
) -> None:
    config = _config(tmp_path / "run", network_profile=profile)

    argv = build_systemd_run_argv(config)

    assert argv[0] == "systemd-run"
    assert f"--unit={UNIT}" in argv
    assert "--description=agentmgr-exec" in argv
    properties = [item for item in argv if item.startswith("--property=")]
    assert properties == _expected_properties(config)
    assert not any("%N" in item or "%i" in item for item in properties)
    assert "--property=ExitType=cgroup" in properties
    assert "--property=CollectMode=inactive" not in properties
    # Both profiles mask the host systemd private socket and D-Bus with a
    # private tmpfs so the bound gateway socket remains the only AF_UNIX entry.
    assert "--property=TemporaryFileSystem=/run/systemd" in properties
    assert "--property=TemporaryFileSystem=/run/dbus" in properties
    assert "--property=InaccessiblePaths=-/run/systemd" not in properties
    assert "--property=InaccessiblePaths=-/run/dbus" not in properties
    prefix = "--property=InaccessiblePaths="
    inaccessible = [
        item[len(prefix) :] for item in properties if item.startswith(prefix)
    ]
    assert inaccessible
    # Every entry uses optional-path syntax; none may be an unconditional path
    # that would make systemd refuse to start on a host without that directory.
    assert all(path.startswith("-") and len(path) > 1 for path in inaccessible)

    if profile == "private_netns":
        assert "--property=PrivateNetwork=yes" in properties
    else:
        assert "--property=PrivateNetwork=yes" not in properties
        # The AF_UNIX-only fallback must keep the no-IP invariants.
        assert "--property=RestrictAddressFamilies=AF_UNIX" in properties
        assert "--property=IPAddressDeny=any" in properties
    # Fixed ExecStart, no shell and no caller-supplied command.
    assert argv[-2:] == ["/usr/bin/python3", "/workload.py"]


def test_whole_directory_inaccessible_paths_are_not_emitted(
    tmp_path: Path,
) -> None:
    for profile in ("private_netns", "af_unix_only_fallback"):
        config = _config(tmp_path / "run" / profile, network_profile=profile)
        properties = {
            item
            for item in build_systemd_run_argv(config)
            if item.startswith("--property=")
        }

        assert "--property=InaccessiblePaths=-/run/systemd" not in properties
        assert "--property=InaccessiblePaths=-/run/dbus" not in properties
        assert "--property=TemporaryFileSystem=/run/systemd" in properties
        assert "--property=TemporaryFileSystem=/run/dbus" in properties


def test_state_directory_uses_exact_unit_name(tmp_path: Path) -> None:
    first = _config(tmp_path / "first")
    second = _config(
        tmp_path / "second", spec=_spec(task_id="task_synthetic_0002")
    )

    first_properties = set(build_systemd_run_argv(first))
    second_properties = set(build_systemd_run_argv(second))

    assert f"--property=StateDirectory=agentmgr-exec/{first.unit}" in first_properties
    assert (
        f"--property=StateDirectory=agentmgr-exec/{second.unit}"
        in second_properties
    )
    assert f"--property=StateDirectory=agentmgr-exec/{UNIT}" in first_properties
    assert not any(
        "%N" in item or "%i" in item for item in first_properties | second_properties
    )


def test_unknown_network_profile_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SecurityError) as excinfo:
        _config(tmp_path / "run", network_profile="open_netns")  # type: ignore[arg-type]

    assert excinfo.value.code == "invalid_spec"
    assert excinfo.value.detail == "network_profile_invalid"


def test_argv_contains_no_user_string_command_or_shell(tmp_path: Path) -> None:
    config = _config(tmp_path / "run")

    argv = build_systemd_run_argv(config)
    joined = "\0".join(argv)

    assert "sh" not in argv
    assert "bash" not in argv
    assert "-c" not in argv
    assert not any(item.startswith("--property=Environment=") for item in argv)
    assert not any(item.startswith("--property=User=") for item in argv)
    assert not any("ExecStart" in item for item in argv)
    assert TASK_ID not in joined
    assert GRANT_ID not in joined
    assert CAPABILITY not in joined
    assert TOKEN not in joined
    assert re.fullmatch(r"agentmgr-task-[0-9a-f]{12}", config.unit)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ttl_seconds", 0),
        ("ttl_seconds", 301),
        ("cpu_quota_percent", 0),
        ("cpu_quota_percent", 101),
        ("memory_max_mb", 31),
        ("memory_max_mb", 1025),
        ("tasks_max", 0),
        ("tasks_max", 65),
        ("tasks_max", True),
    ],
)
def test_builder_rejects_out_of_range_limits(
    tmp_path: Path, field: str, value: object
) -> None:
    spec = replace(_spec(), **{field: value})
    config = replace(_config(tmp_path / "run"), spec=spec)

    with pytest.raises(SecurityError) as excinfo:
        build_systemd_run_argv(config)

    assert excinfo.value.code == "invalid_spec"


def test_builder_rejects_invalid_or_mismatched_unit(tmp_path: Path) -> None:
    with pytest.raises(SecurityError):
        replace(_config(tmp_path / "run"), unit="agentmgr-task-NOTHEX")

    other = _spec(task_id="task_synthetic_0002")
    with pytest.raises(SecurityError):
        replace(_config(tmp_path / "run"), unit=UNIT, spec=other)


def test_show_argv_is_fixed_and_unit_validated() -> None:
    argv = build_systemctl_show_argv(UNIT)

    assert argv == [
        "systemctl",
        "show",
        "-p",
        (
            "ActiveState,SubState,MainPID,Result,ExecMainStatus,LoadState,"
            "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp"
        ),
        UNIT,
    ]
    with pytest.raises(SecurityError):
        build_systemctl_show_argv("agentmgr-task-ABC")


def test_show_parser_keeps_first_equals_and_skips_malformed_lines() -> None:
    text = (
        "ActiveState=active\n"
        "SubState=running\n"
        "MainPID=4242\n"
        "\n"
        "Description=agentmgr=exec\n"
        "not a property line\n"
    )

    assert parse_systemctl_show(text) == {
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "4242",
        "Description": "agentmgr=exec",
    }
    assert parse_systemctl_show("") == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"{_timestamp(NOW)}", NOW),
        ("@1700000000", datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        ("@1700000000.500000", datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=UTC)),
        ("2026-01-02T03:04:05+00:00", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
    ],
)
def test_parse_systemd_timestamp_accepts_systemd_forms(
    value: str, expected: datetime
) -> None:
    assert parse_systemd_timestamp(value) == expected


@pytest.mark.parametrize("value", ["", "n/a", "-", "not-a-timestamp", "@", "@NaN"])
def test_parse_systemd_timestamp_fails_closed(value: str) -> None:
    assert parse_systemd_timestamp(value) is None


def _observation_row(
    show: dict[str, str],
    *,
    stop_requested_at: datetime | None = None,
    had_start_evidence: bool = False,
) -> tuple[str, bool, bool, bool]:
    observation = observation_from_show(
        show,
        stop_requested_at=stop_requested_at,
        had_start_evidence=had_start_evidence,
    )
    return (
        observation.state,
        observation.process_gone,
        observation.stop_confirmed,
        observation.never_started,
    )


def test_show_parser_and_state_table() -> None:
    stop_at = NOW
    completed_show = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
        "ExecMainExitTimestamp": _timestamp(NOW - timedelta(seconds=1)),
    }
    rows = [
        (
            {
                "ActiveState": "activating",
                "SubState": "start",
                "MainPID": "1234",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "loaded",
            },
            {},
            ("starting", False, False, False),
        ),
        (
            {
                "ActiveState": "active",
                "SubState": "running",
                "MainPID": "1234",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "loaded",
            },
            {},
            ("running", False, False, False),
        ),
        (
            {
                "ActiveState": "active",
                "SubState": "exited",
                "MainPID": "0",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "loaded",
                "ExecMainExitTimestamp": _timestamp(NOW - timedelta(seconds=1)),
            },
            {},
            ("completed", True, False, False),
        ),
        (
            {
                "ActiveState": "active",
                "SubState": "exited",
                "MainPID": "0",
                "Result": "success",
                "ExecMainStatus": "15",
                "LoadState": "loaded",
                "ExecMainExitTimestamp": _timestamp(NOW + timedelta(seconds=1)),
            },
            {"stop_requested_at": stop_at},
            ("cancelled", True, True, False),
        ),
        (
            {
                "ActiveState": "active",
                "SubState": "exited",
                "MainPID": "0",
                "Result": "success",
                "ExecMainStatus": "1",
                "LoadState": "loaded",
                "ExecMainExitTimestamp": _timestamp(NOW - timedelta(seconds=1)),
            },
            {},
            ("unknown", True, False, False),
        ),
        (
            {
                "ActiveState": "active",
                "SubState": "exited",
                "MainPID": "0",
                "Result": "signal",
                "ExecMainStatus": "15",
                "LoadState": "loaded",
                "ExecMainExitTimestamp": _timestamp(NOW - timedelta(seconds=1)),
            },
            {},
            ("unknown", True, False, False),
        ),
        (
            {
                "ActiveState": "deactivating",
                "SubState": "stop-sigterm",
                "MainPID": "1234",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "loaded",
            },
            {},
            ("stopping", False, False, False),
        ),
        (completed_show, {}, ("completed", True, False, False)),
        (
            {
                key: value
                for key, value in completed_show.items()
                if key != "ExecMainExitTimestamp"
            },
            {"stop_requested_at": stop_at},
            ("unknown", False, False, False),
        ),
        (
            {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "loaded",
            },
            {},
            ("unknown", False, False, False),
        ),
        (
            {
                "ActiveState": "failed",
                "SubState": "failed",
                "MainPID": "0",
                "Result": "timeout",
                "ExecMainStatus": "0",
                "LoadState": "loaded",
                "ExecMainExitTimestamp": _timestamp(NOW),
            },
            {},
            ("timed_out", True, True, False),
        ),
        (
            {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "Result": "signal",
                "ExecMainStatus": "15",
                "LoadState": "loaded",
                "ExecMainExitTimestamp": _timestamp(NOW + timedelta(seconds=1)),
            },
            {"stop_requested_at": stop_at},
            ("cancelled", True, True, False),
        ),
        (
            {
                "ActiveState": "failed",
                "SubState": "failed",
                "MainPID": "0",
                "Result": "exit-code",
                "ExecMainStatus": "1",
                "LoadState": "loaded",
                "ExecMainExitTimestamp": _timestamp(NOW),
            },
            {},
            ("failed", True, False, False),
        ),
        (
            {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "not-found",
            },
            {},
            ("not_started", False, False, True),
        ),
        (
            {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "not-found",
            },
            {"had_start_evidence": True},
            ("unknown", True, False, False),
        ),
        (
            {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "not-found",
                "ExecMainStartTimestamp": _timestamp(NOW - timedelta(seconds=1)),
            },
            {},
            ("unknown", True, False, False),
        ),
        ({"ActiveState": "unknown", "LoadState": "loaded"}, {}, ("unknown", False, False, False)),
        ({}, {}, ("unknown", False, False, False)),
        (
            {
                "ActiveState": "inactive",
                "MainPID": "1234",
                "Result": "success",
                "ExecMainStatus": "0",
                "LoadState": "loaded",
            },
            {},
            ("unknown", False, False, False),
        ),
    ]

    for show, kwargs, expected in rows:
        assert _observation_row(show, **kwargs) == expected, show


def test_active_exited_keeps_main_pid_zero_and_exit_evidence() -> None:
    observation = observation_from_show(
        {
            "ActiveState": "active",
            "SubState": "exited",
            "MainPID": "0",
            "Result": "success",
            "ExecMainStatus": "0",
            "LoadState": "loaded",
            "ExecMainExitTimestamp": _timestamp(NOW - timedelta(seconds=1)),
        }
    )

    assert observation.state == "completed"
    assert observation.process_gone is True
    assert observation.stop_confirmed is False
    assert observation.main_pid == 0
    assert observation.exec_main_status == 0


def test_natural_completion_is_not_stop_confirmed() -> None:
    stop_at = NOW
    show = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
        "ExecMainExitTimestamp": _timestamp(stop_at - timedelta(seconds=1)),
    }

    observation = observation_from_show(show, stop_requested_at=stop_at)

    assert observation.state == "completed"
    assert observation.process_gone is True
    assert observation.stop_confirmed is False
    assert observation.never_started is False
    assert observation.result == "success"
    assert observation.exec_main_status == 0
    assert observation.main_pid == 0


def test_stop_request_before_exit_is_cancelled() -> None:
    stop_at = NOW
    show = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
        "ExecMainExitTimestamp": _timestamp(stop_at + timedelta(seconds=1)),
    }

    observation = observation_from_show(show, stop_requested_at=stop_at)

    assert observation.state == "cancelled"
    assert observation.process_gone is True
    assert observation.stop_confirmed is True
    assert observation.never_started is False


def test_missing_fields_do_not_claim_completion_or_stop() -> None:
    observation = observation_from_show(
        {
            "ActiveState": "inactive",
            "MainPID": "0",
            "LoadState": "loaded",
        },
        stop_requested_at=NOW,
    )

    assert observation.state == "unknown"
    assert observation.process_gone is False
    assert observation.stop_confirmed is False
    assert observation.never_started is False
    assert observation.exec_main_status is None


def test_loaded_unit_without_start_evidence_is_not_completed() -> None:
    observation = observation_from_show(
        {
            "ActiveState": "inactive",
            "SubState": "dead",
            "MainPID": "0",
            "Result": "success",
            "ExecMainStatus": "0",
            "LoadState": "loaded",
        }
    )

    assert observation.state == "unknown"
    assert observation.process_gone is False
    assert observation.stop_confirmed is False
    assert observation.never_started is False


def test_same_second_whole_second_exit_is_ambiguous() -> None:
    stop_at = NOW.replace(microsecond=100_000)
    show = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
        "ExecMainExitTimestamp": _timestamp(stop_at),
    }

    observation = observation_from_show(show, stop_requested_at=stop_at)

    assert observation.state == "unknown"
    assert observation.process_gone is True
    assert observation.stop_confirmed is False


def test_same_second_precise_stop_first_is_cancelled() -> None:
    stop_at = NOW.replace(microsecond=100_000)
    show = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
        "ExecMainExitTimestamp": _timestamp_precise(
            stop_at.replace(microsecond=200_000)
        ),
    }

    observation = observation_from_show(show, stop_requested_at=stop_at)

    assert observation.state == "cancelled"
    assert observation.process_gone is True
    assert observation.stop_confirmed is True


def test_same_second_precise_natural_exit_is_completed() -> None:
    stop_at = NOW.replace(microsecond=900_000)
    show = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
        "ExecMainExitTimestamp": _timestamp_precise(
            stop_at.replace(microsecond=100_000)
        ),
    }

    observation = observation_from_show(show, stop_requested_at=stop_at)

    assert observation.state == "completed"
    assert observation.process_gone is True
    assert observation.stop_confirmed is False


def test_naive_stop_request_is_rejected() -> None:
    show = {
        "ActiveState": "inactive",
        "MainPID": "0",
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
        "ExecMainExitTimestamp": _timestamp(NOW),
    }

    with pytest.raises(SecurityError) as excinfo:
        observation_from_show(
            show, stop_requested_at=datetime(2026, 1, 1, 12, 0)
        )

    assert excinfo.value.code == "invalid_spec"
    assert excinfo.value.detail == "stop_requested_invalid"


def test_helper_ops_sends_stdin_and_never_token_argv(tmp_path: Path) -> None:
    runner = FakeRunner()
    ops = HelperSystemdOps(HELPER, runner=runner)
    config = _config(tmp_path / "run")
    spec_text = json.dumps(config.spec.as_dict(), sort_keys=True)

    ops.launch(config, spec_text=spec_text, credential_text=TOKEN)

    assert len(runner.calls) == 1
    argv, input_text = runner.calls[0]
    assert argv == ["sudo", "-n", str(HELPER)]
    assert input_text is not None
    assert TOKEN in input_text
    assert all(TOKEN not in part for part in argv)
    payload = json.loads(input_text)
    assert payload == {
        "command": "launch",
        "spec": json.loads(spec_text),
        "credential": TOKEN,
    }

    with pytest.raises(SecurityError) as excinfo:
        ops.stop("agentmgr-task-not-a-real-unit")
    assert excinfo.value.code == "invalid_spec"
    assert len(runner.calls) == 1

    with pytest.raises(ValueError):
        HelperSystemdOps(Path("relative/agentmgr-exec"))


def test_helper_ops_show_parses_structured_output(tmp_path: Path) -> None:
    runner = FakeRunner(
        stdout='{"ActiveState":"active","SubState":"running","MainPID":"77"}'
    )
    ops = HelperSystemdOps(HELPER, runner=runner)

    show = ops.show(UNIT)

    assert show == {"ActiveState": "active", "SubState": "running", "MainPID": "77"}
    assert observation_from_show(show).state == "running"
    assert runner.calls[0] == (
        ["sudo", "-n", str(HELPER)],
        json.dumps({"command": "inspect", "unit": UNIT}, sort_keys=True, separators=(",", ":")),
    )


def test_helper_ops_terminate_sends_fixed_command() -> None:
    runner = FakeRunner()
    ops = HelperSystemdOps(HELPER, runner=runner)

    ops.terminate(UNIT)

    assert runner.calls == [
        (
            ["sudo", "-n", str(HELPER)],
            json.dumps(
                {"command": "terminate", "unit": UNIT},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    ]


def test_sudo_ops_writes_root_only_sources_without_token_argv(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    runtime = tmp_path / "run"
    ops = SudoSystemdOps(
        runner=runner,
        runtime_root=runtime,
        state_root=tmp_path / "state",
        state_link_root=tmp_path / "state-link",
    )
    config = _config(runtime)
    spec_text = json.dumps(config.spec.as_dict(), sort_keys=True)

    ops.launch(config, spec_text=spec_text, credential_text=TOKEN)

    spec_source = runtime / "specs" / f"{UNIT}.json"
    credential_source = runtime / "credentials" / f"{UNIT}.json"
    assert [argv for argv, _ in runner.calls[:4]] == [
        ["sudo", "-n", "tee", "--", str(spec_source)],
        ["sudo", "-n", "chmod", "600", "--", str(spec_source)],
        ["sudo", "-n", "tee", "--", str(credential_source)],
        ["sudo", "-n", "chmod", "600", "--", str(credential_source)],
    ]
    assert runner.calls[0][1] == spec_text
    assert runner.calls[2][1] == TOKEN
    assert all(TOKEN not in part for argv, _ in runner.calls for part in argv)
    assert runner.calls[4] == (
        ["sudo", "-n", *build_systemd_run_argv(config)],
        None,
    )


def test_sudo_ops_discards_tee_echo_for_credential(tmp_path: Path) -> None:
    class EchoingRunner:
        def __init__(self) -> None:
            self.records: list[subprocess.CompletedProcess[str]] = []

        def __call__(
            self, argv: list[str], input_text: str | None = None
        ) -> subprocess.CompletedProcess[str]:
            completed = subprocess.CompletedProcess(
                argv, 0, input_text or "", ""
            )
            self.records.append(completed)
            return completed

    runner = EchoingRunner()
    runtime = tmp_path / "run"
    ops = SudoSystemdOps(
        runner=runner,
        runtime_root=runtime,
        state_root=tmp_path / "state",
        state_link_root=tmp_path / "state-link",
    )
    config = _config(runtime)

    ops.launch(config, spec_text="{}", credential_text=TOKEN)

    assert all(TOKEN not in (record.stdout or "") for record in runner.records)


def test_runner_failure_does_not_chain_token_or_paths(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    config = _config(runtime)
    spec_text = json.dumps(config.spec.as_dict(), sort_keys=True)

    class FailingRunner:
        def __call__(
            self, argv: list[str], input_text: str | None = None
        ) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(
                1,
                argv,
                output=TOKEN,
                stderr=f"tee failed {TOKEN} {argv[-1]}",
            )

    ops = SudoSystemdOps(
        runner=FailingRunner(),
        runtime_root=runtime,
        state_root=tmp_path / "state",
        state_link_root=tmp_path / "state-link",
    )

    with pytest.raises(SecurityError) as excinfo:
        ops.launch(config, spec_text=spec_text, credential_text=TOKEN)

    error = excinfo.value
    assert TOKEN not in str(error)
    assert str(runtime) not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True


def test_sudo_ops_uses_fixed_systemctl_argv(tmp_path: Path) -> None:
    runner = FakeRunner(stdout="ActiveState=active\n")
    ops = SudoSystemdOps(
        runner=runner,
        runtime_root=tmp_path / "run",
        state_root=tmp_path / "state",
        state_link_root=tmp_path / "state-link",
    )

    ops.stop(UNIT)
    ops.terminate(UNIT)
    ops.kill(UNIT)
    show = ops.show(UNIT)
    ops.reset_failed(UNIT)
    ops.remove_sources(UNIT)

    assert show == {"ActiveState": "active"}
    assert [argv for argv, _ in runner.calls] == [
        ["sudo", "-n", "systemctl", "stop", UNIT],
        [
            "sudo",
            "-n",
            "systemctl",
            "kill",
            "--signal=SIGTERM",
            "--kill-whom=all",
            UNIT,
        ],
        [
            "sudo",
            "-n",
            "systemctl",
            "kill",
            "--signal=SIGKILL",
            "--kill-whom=all",
            UNIT,
        ],
        ["sudo", "-n", *build_systemctl_show_argv(UNIT)],
        ["sudo", "-n", "systemctl", "reset-failed", UNIT],
        [
            "sudo",
            "-n",
            "rm",
            "-f",
            "--",
            str(tmp_path / "run" / "credentials" / f"{UNIT}.json"),
            str(tmp_path / "run" / "specs" / f"{UNIT}.json"),
        ],
    ]

    with pytest.raises(SecurityError):
        ops.stop("agentmgr-task-XYZ")
    assert len(runner.calls) == 6


def test_sudo_reset_failed_is_noop_for_collected_unit(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(
        argv: list[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if "reset-failed" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "LoadState=not-found\n", "")

    ops = SudoSystemdOps(
        runner=runner,
        runtime_root=tmp_path / "run",
        state_root=tmp_path / "state",
        state_link_root=tmp_path / "state-link",
    )

    ops.reset_failed(UNIT)

    assert calls[0] == ["sudo", "-n", "systemctl", "reset-failed", UNIT]
    assert calls[1][:3] == ["sudo", "-n", "systemctl"]
    assert any("LoadState" in part for part in calls[1])


def test_sudo_reset_failed_still_raises_for_loaded_unit(tmp_path: Path) -> None:
    def runner(
        argv: list[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        if "reset-failed" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "LoadState=loaded\n", "")

    ops = SudoSystemdOps(
        runner=runner,
        runtime_root=tmp_path / "run",
        state_root=tmp_path / "state",
        state_link_root=tmp_path / "state-link",
    )

    with pytest.raises(SecurityError) as excinfo:
        ops.reset_failed(UNIT)

    assert excinfo.value.detail == "reset_failed_failed"


def test_sudo_cleanup_stops_loaded_active_exited_unit_first(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(
        argv: list[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            "ActiveState=active\nSubState=exited\nMainPID=0\nLoadState=loaded\n",
            "",
        )

    ops = SudoSystemdOps(
        runner=runner,
        runtime_root=tmp_path / "run",
        state_root=tmp_path / "state",
        state_link_root=tmp_path / "state-link",
    )

    ops.cleanup(UNIT)

    assert calls[0][:4] == ["sudo", "-n", "systemctl", "stop"]
    assert calls[1][:4] == ["sudo", "-n", "systemctl", "reset-failed"]
    assert calls[2][:5] == ["sudo", "-n", "rm", "-rf", "--"]


def test_sudo_ops_cleanup_only_removes_validated_unit_paths(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    runtime = tmp_path / "run"
    state = tmp_path / "state"
    state_link = tmp_path / "state-link"
    ops = SudoSystemdOps(
        runner=runner,
        runtime_root=runtime,
        state_root=state,
        state_link_root=state_link,
    )

    ops.cleanup(UNIT)

    assert runner.calls[0] == (
        ["sudo", "-n", "systemctl", "stop", UNIT],
        None,
    )
    assert runner.calls[1] == (
        ["sudo", "-n", "systemctl", "reset-failed", UNIT],
        None,
    )
    assert len(runner.calls) == 3
    rm_argv, rm_input = runner.calls[2]
    assert rm_argv[:5] == ["sudo", "-n", "rm", "-rf", "--"]
    assert rm_input is None
    assert set(rm_argv[5:]) == {
        str(runtime / "credentials" / f"{UNIT}.json"),
        str(runtime / "specs" / f"{UNIT}.json"),
        str(state / UNIT),
        str(state_link / UNIT),
    }

    before = len(runner.calls)
    with pytest.raises(SecurityError):
        ops.cleanup("agentmgr-task-XYZ")
    assert len(runner.calls) == before
