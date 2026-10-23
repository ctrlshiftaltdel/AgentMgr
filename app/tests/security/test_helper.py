"""Tests for the production root helper and deployment templates (#82).

The helper is a production template: #82's live tests use ``SudoSystemdOps``
and never install it.  Everything here runs unprivileged against a temporary
runtime root, a recording runner and the template files themselves, so no
sudo, systemd, ``/run`` or ``/var/lib`` access happens.

The duplicated constants in ``ops/security/agentmgr_exec.py`` are pinned to
the product builders: the property list, the unit derivation and the fixed
``ExecStart`` are compared directly with ``app.security.systemd``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.security.boundary import ManagedTaskSpec, unit_name_for_task
from app.security.systemd import SystemdUnitConfig, build_systemd_run_argv

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS = REPO_ROOT / "ops" / "security"
TASK_ID = "task_synthetic_0001"
GRANT_ID = "grnt_synthetic_0001"
CAPABILITY = "resource.read@1.0.0"
UNIT = unit_name_for_task(TASK_ID)
TOKEN = "agx1.c3ludGhldGlj.c2lnbmF0dXJl"
BAD_UNITS = (
    "",
    "agentmgr-task-0123456789ab\n",
    "agentmgr-task-0123456789AB",
    "agentmgr-task-0123456789a",
    "agentmgr-task-0123456789abc",
    "agentmgr-task-0123456789ag",
    "agentmgr-task-0123456789ab.service",
    "prefix-agentmgr-task-0123456789ab",
    "../agentmgr-task-0123456789ab",
    None,
    42,
    ["agentmgr-task-0123456789ab"],
)


def _load_helper():
    path = OPS / "agentmgr_exec.py"
    spec = importlib.util.spec_from_file_location("agentmgr_exec", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _spec_dict() -> dict[str, object]:
    return {
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


def _launch_request() -> str:
    return json.dumps(
        {"command": "launch", "spec": _spec_dict(), "credential": TOKEN},
        sort_keys=True,
    )


class RecordingRunner:
    """Capture argv instead of executing a privileged command."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, argv) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, list)
        assert all(isinstance(item, str) for item in argv)
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point every helper path at a short, private temporary tree."""

    runtime = tmp_path / "run"
    state = tmp_path / "state"
    link = tmp_path / "link"
    for directory in (runtime, state, link):
        directory.mkdir(mode=0o700)

    workload = runtime / "workload.py"
    workload.write_text("# synthetic workload\n", encoding="utf-8")
    workload.chmod(0o555)

    socket_path = runtime / "gateway.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
    finally:
        listener.close()

    monkeypatch.setattr(helper, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(helper, "STATE_ROOT", state)
    monkeypatch.setattr(helper, "STATE_LINK_ROOT", link)
    monkeypatch.setattr(helper, "WORKLOAD_PATH", workload)
    monkeypatch.setattr(helper, "GATEWAY_SOCKET_PATH", socket_path)

    return SimpleNamespace(
        runtime=runtime,
        state=state,
        link=link,
        workload=workload,
        socket=socket_path,
        specs=runtime / "specs",
        credentials=runtime / "credentials",
        spec_path=runtime / "specs" / f"{UNIT}.json",
        credential_path=runtime / "credentials" / f"{UNIT}.json",
    )


def _read_template(name: str) -> str:
    return (OPS / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Unit and payload validation
# --------------------------------------------------------------------------


def test_validate_unit_rejects_anything_but_agentmgr_task_hex() -> None:
    for bad in BAD_UNITS:
        with pytest.raises(helper.HelperError):
            helper.validate_unit(bad)

    helper.validate_unit("agentmgr-task-0123456789ab")


def test_unit_derivation_matches_product_boundary() -> None:
    for task_id in (TASK_ID, "task_abc", "task_Z9-_", "task_" + "a" * 20):
        assert helper.unit_for_task(task_id) == unit_name_for_task(task_id)

    for bad in ("task", "task_a", "task_ space", "../task_abc", "task_" + "a" * 97):
        with pytest.raises(helper.HelperError):
            helper.unit_for_task(bad)


def test_launch_payload_rejects_unknown_keys_and_token_in_argv(
    sandbox: SimpleNamespace,
) -> None:
    runner = RecordingRunner()
    base = json.loads(_launch_request())

    # Top-level fields outside the request envelope are rejected.
    for key, value in (
        ("User", "root"),
        ("ExecStart", "/bin/sh -c id"),
        ("Environment", "TOKEN=leak"),
        ("BindPaths", "/:/host"),
        ("unit", UNIT),
    ):
        payload = dict(base)
        payload[key] = value
        with pytest.raises(helper.HelperError):
            helper.launch_from_stdin(json.dumps(payload), runner=runner)

    # Missing and duplicate-shape fields are rejected too.
    for payload in (
        {"command": "launch", "spec": base["spec"]},
        {"command": "launch", "credential": TOKEN},
        {"command": "shell", "unit": UNIT},
        {"command": "launch", "spec": base["spec"], "credential": TOKEN, "x": 1},
    ):
        with pytest.raises(helper.HelperError):
            helper.launch_from_stdin(json.dumps(payload), runner=runner)

    # A non-object or oversized stdin body is rejected before any command.
    with pytest.raises(helper.HelperError):
        helper.launch_from_stdin("[]", runner=runner)
    with pytest.raises(helper.HelperError):
        helper.launch_from_stdin("x" * (helper.MAX_STDIN_BYTES + 1), runner=runner)

    assert runner.calls == []
    assert not sandbox.specs.exists()
    assert not sandbox.credentials.exists()

    # Spec-level property injection is rejected (exact-key whitelist).
    for key, value in (
        ("ExecStart", "/bin/sh -c id"),
        ("User", "root"),
        ("Environment", "TOKEN=leak"),
        ("BindPaths", "/:/host"),
        ("BindReadOnlyPaths", "/:/host"),
        ("LoadCredential", "task-credential:/etc/shadow"),
    ):
        spec = _spec_dict()
        spec[key] = value
        with pytest.raises(helper.HelperError):
            helper.validate_spec_payload(spec)

    # The accepted request carries the token only through stdin; argv keeps
    # the fixed executable and the fixed workload only.
    helper.launch_from_stdin(_launch_request(), runner=runner)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert all(TOKEN not in part for part in argv)
    assert argv[0] == "/usr/bin/systemd-run"
    assert argv[-2:] == ["/usr/bin/python3", "/workload.py"]
    assert "sh" not in argv and "-c" not in argv


def test_launch_rejects_bad_spec_values() -> None:
    for key, value in (
        ("task_id", "task_x"),
        ("grant_id", "grant_synthetic_0001"),
        ("execution_mode", "direct_execution"),
        ("ttl_seconds", 0),
        ("ttl_seconds", True),
        ("cpu_quota_percent", 101),
        ("memory_max_mb", 31),
        ("tasks_max", 65),
        ("capabilities", []),
        ("backend", "database"),
    ):
        spec = _spec_dict()
        spec[key] = value
        with pytest.raises(helper.HelperError):
            helper.validate_spec_payload(spec)

    spec = _spec_dict()
    spec["capabilities"] = ["Not A Capability"]
    with pytest.raises(helper.HelperError):
        helper.validate_spec_payload(spec)

    spec = _spec_dict()
    spec["resource_scope"] = {**_spec_dict()["resource_scope"], "scope_type": "all"}
    with pytest.raises(helper.HelperError):
        helper.validate_spec_payload(spec)


def test_run_checked_uses_clean_environment_and_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    helper.run_checked(["/usr/bin/systemctl", "show", "agentmgr-task-0123456789ab"])

    assert captured["shell"] is False
    assert captured["env"] == helper.CLEAN_ENV
    assert "HOME" not in captured["env"]
    assert "PATH" in captured["env"]


# --------------------------------------------------------------------------
# Source files and privilege boundary
# --------------------------------------------------------------------------


def test_launch_writes_spec_and_credential_root_only(
    sandbox: SimpleNamespace,
) -> None:
    runner = RecordingRunner()

    result = helper.launch_from_stdin(_launch_request(), runner=runner)

    assert result == {"ok": True, "unit": UNIT}
    assert stat.S_IMODE(sandbox.specs.stat().st_mode) == 0o700
    assert stat.S_IMODE(sandbox.credentials.stat().st_mode) == 0o700
    assert stat.S_IMODE(sandbox.spec_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(sandbox.credential_path.stat().st_mode) == 0o600
    assert sandbox.credential_path.read_text(encoding="utf-8") == TOKEN
    assert json.loads(sandbox.spec_path.read_text(encoding="utf-8")) == _spec_dict()

    # O_EXCL: a second launch never overwrites an existing source file.
    before = sandbox.credential_path.read_text(encoding="utf-8")
    with pytest.raises(helper.HelperError):
        helper.launch_from_stdin(_launch_request(), runner=runner)
    assert sandbox.credential_path.read_text(encoding="utf-8") == before

    # A symlinked credential source is rejected and its target is untouched.
    sandbox.spec_path.unlink()
    sandbox.credential_path.unlink()
    target = sandbox.runtime / "target"
    target.write_text("untouched", encoding="utf-8")
    sandbox.credential_path.symlink_to(target)
    with pytest.raises(helper.HelperError):
        helper.launch_from_stdin(_launch_request(), runner=runner)
    assert target.read_text(encoding="utf-8") == "untouched"

    # A permissive source directory is rejected instead of being repaired.
    sandbox.credential_path.unlink()
    os.chmod(sandbox.credentials, 0o755)
    with pytest.raises(helper.HelperError):
        helper.launch_from_stdin(_launch_request(), runner=runner)

    # A symlinked workload is not bind-mounted.
    os.chmod(sandbox.credentials, 0o700)
    sandbox.workload.unlink()
    sandbox.workload.symlink_to(target)
    with pytest.raises(helper.HelperError):
        helper.launch_from_stdin(_launch_request(), runner=runner)
    assert target.read_text(encoding="utf-8") == "untouched"


def test_credential_shape_is_validated(sandbox: SimpleNamespace) -> None:
    for bad in ("", "not-a-token", "agx1..", "agx1.a.b.c", "agx1.a.b\n", None, 7):
        payload = {
            "command": "launch",
            "spec": _spec_dict(),
            "credential": bad,
        }
        with pytest.raises(helper.HelperError):
            helper.launch_from_stdin(json.dumps(payload), runner=RecordingRunner())


# --------------------------------------------------------------------------
# Fixed subcommands and cleanup scope
# --------------------------------------------------------------------------


def test_stop_inspect_cleanup_reject_bad_units(sandbox: SimpleNamespace) -> None:
    runner = RecordingRunner()
    for bad in BAD_UNITS:
        for operation, kwargs in (
            (helper.stop, {"runner": runner}),
            (helper.kill, {"runner": runner}),
            (helper.inspect, {"runner": runner}),
            (helper.reset_failed, {"runner": runner}),
            (helper.remove_sources, {}),
            (helper.cleanup, {"runner": runner}),
        ):
            with pytest.raises(helper.HelperError):
                operation(bad, **kwargs)
    assert runner.calls == []


def test_commands_use_only_fixed_argv(sandbox: SimpleNamespace) -> None:
    runner = RecordingRunner()
    helper.stop(UNIT, runner=runner)
    helper.kill(UNIT, runner=runner)
    helper.reset_failed(UNIT, runner=runner)

    assert runner.calls == [
        ["/usr/bin/systemctl", "stop", UNIT],
        [
            "/usr/bin/systemctl",
            "kill",
            "--signal=SIGKILL",
            "--kill-whom=all",
            UNIT,
        ],
        ["/usr/bin/systemctl", "reset-failed", UNIT],
    ]

    show = "ActiveState=active\nMainPID=61000\nResult=success\n"
    inspection = helper.inspect(UNIT, runner=RecordingRunner(stdout=show))
    assert inspection == {
        "ActiveState": "active",
        "MainPID": "61000",
        "Result": "success",
    }


def test_run_request_accepts_only_the_documented_commands(
    sandbox: SimpleNamespace,
) -> None:
    runner = RecordingRunner()
    assert helper.run_request(
        {"command": "stop", "unit": UNIT}, runner=runner
    ) == {"ok": True, "unit": UNIT}
    assert runner.calls == [["/usr/bin/systemctl", "stop", UNIT]]

    for payload in (
        {"command": "systemctl", "unit": UNIT},
        {"command": "stop", "unit": UNIT, "args": ["--force"]},
        {"command": "stop", "unit": UNIT, "spec": {}},
        {"command": "launch", "unit": UNIT},
        {"unit": UNIT},
        [],
    ):
        with pytest.raises(helper.HelperError):
            helper.run_request(payload, runner=runner)


def test_main_rejects_argv_and_reports_structured_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert helper.main(["--unit=agentmgr-task-0123456789ab"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "arguments_not_accepted"}

    monkeypatch.setattr("sys.stdin", io.StringIO('{"command": "launch"}'))
    assert helper.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "invalid_request"

    monkeypatch.setattr("sys.stdin", io.StringIO("x" * (helper.MAX_STDIN_BYTES + 1)))
    assert helper.main([]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "payload_too_large"


def test_cleanup_removes_only_unit_derived_paths(sandbox: SimpleNamespace) -> None:
    helper.launch_from_stdin(_launch_request(), runner=RecordingRunner())

    state_dir = sandbox.state / UNIT
    state_dir.mkdir()
    (state_dir / "result.json").write_text("{}", encoding="utf-8")
    link = sandbox.link / UNIT
    link.symlink_to(state_dir)
    victim = sandbox.state / "agentmgr-task-000000000000"
    victim.mkdir()
    (victim / "keep").write_text("keep", encoding="utf-8")

    runner = RecordingRunner()
    helper.cleanup(UNIT, runner=runner)

    assert not state_dir.exists()
    assert not os.path.lexists(link)
    assert victim.exists()
    assert (victim / "keep").read_text(encoding="utf-8") == "keep"
    assert not sandbox.spec_path.exists()
    assert not sandbox.credential_path.exists()
    assert runner.calls == [["/usr/bin/systemctl", "reset-failed", UNIT]]

    # A symlink in the unit's state-directory slot is never followed.
    state_dir.symlink_to(victim)
    with pytest.raises(helper.HelperError):
        helper.cleanup(UNIT, runner=runner)
    assert (victim / "keep").read_text(encoding="utf-8") == "keep"


def test_remove_sources_rejects_wrong_mode_and_symlinks(
    sandbox: SimpleNamespace,
) -> None:
    helper.launch_from_stdin(_launch_request(), runner=RecordingRunner())

    os.chmod(sandbox.credential_path, 0o644)
    with pytest.raises(helper.HelperError):
        helper.remove_sources(UNIT)
    assert sandbox.credential_path.exists()

    os.chmod(sandbox.credential_path, 0o600)
    sandbox.credential_path.unlink()
    target = sandbox.runtime / "target"
    target.write_text("untouched", encoding="utf-8")
    sandbox.credential_path.symlink_to(target)
    with pytest.raises(helper.HelperError):
        helper.remove_sources(UNIT)
    assert target.read_text(encoding="utf-8") == "untouched"
    assert sandbox.spec_path.exists()


# --------------------------------------------------------------------------
# Deployment templates
# --------------------------------------------------------------------------


def test_wrapper_execs_the_fixed_helper() -> None:
    lines = [
        line.strip()
        for line in _read_template("agentmgr-exec").splitlines()
        if line.strip()
    ]
    assert lines[0] == "#!/bin/sh"
    assert lines[-1] == (
        'exec /usr/bin/python3 /usr/local/libexec/agentmgr_exec.py "$@"'
    )
    executable_lines = [line for line in lines[1:] if not line.startswith("#")]
    assert executable_lines == [lines[-1]]


def test_sudoers_template_is_exact_fixed_executable() -> None:
    text = _read_template("agentmgr-exec.sudoers")
    rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert rules == ["agentmgr ALL=(root) NOPASSWD: /usr/local/libexec/agentmgr-exec"]
    lowered = text.lower()
    assert "systemctl" not in lowered
    assert "systemd-run" not in lowered
    assert "*" not in text
    assert "<controller-user>" in text
    assert "placeholder" in lowered
    assert "validate" in lowered


def test_service_templates_use_hardening_and_unix_socket() -> None:
    gateway = _read_template("agentmgr-pep-gateway.service")
    for directive in (
        "Type=simple",
        "User=<controller-user>",
        "Group=<controller-group>",
        "RuntimeDirectory=agentmgr-exec",
        "RuntimeDirectoryMode=0700",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "ExecStart=/usr/bin/python3 -m app.security.gateway "
        "--socket /run/agentmgr-exec/gateway.sock",
    ):
        assert directive in gateway
    assert "\nUser=root" not in gateway

    task = _read_template("agentmgr-task@.service")
    for directive in (
        "ExecStart=/usr/bin/python3 /workload.py",
        "DynamicUser=yes",
        "StateDirectory=agentmgr-exec/agentmgr-task-%i",
        "StateDirectoryMode=0700",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "CapabilityBoundingSet=",
        "InaccessiblePaths=-/etc/agentmgr",
        "TemporaryFileSystem=/run/systemd",
        "TemporaryFileSystem=/run/dbus",
        "LoadCredential=task-credential:"
        "/run/agentmgr-exec/credentials/agentmgr-task-%i.json",
        "BindReadOnlyPaths=/run/agentmgr-exec/gateway.sock:"
        "/run/agentmgr/gateway.sock",
        "BindReadOnlyPaths=/run/agentmgr-exec/workload.py:/workload.py",
    ):
        assert directive in task
    assert "\nUser=" not in task
    assert "transient" in task.lower()
    assert "InaccessiblePaths=-/run/systemd" not in task
    assert "InaccessiblePaths=-/run/dbus" not in task


# --------------------------------------------------------------------------
# The duplicated template must stay pinned to the product builder
# --------------------------------------------------------------------------


def test_helper_argv_matches_product_builder() -> None:
    spec = ManagedTaskSpec.from_mapping(_spec_dict())
    config = SystemdUnitConfig(
        unit=UNIT,
        spec=spec,
        credential_source=helper.RUNTIME_ROOT / "credentials" / f"{UNIT}.json",
        workload_source=helper.WORKLOAD_PATH,
        gateway_socket_source=helper.GATEWAY_SOCKET_PATH,
    )
    product = build_systemd_run_argv(config)
    ours = helper.build_launch_argv(UNIT, _spec_dict())

    assert [arg for arg in ours if arg.startswith("--property=")] == [
        arg for arg in product if arg.startswith("--property=")
    ]
    assert ours[1:3] == product[1:3]
    assert ours[-2:] == product[-2:]
    # The duplicated helper must emit the tmpfs mask, never the whole-dir
    # InaccessiblePaths that breaks systemd namespace setup on the approved host.
    assert "--property=TemporaryFileSystem=/run/systemd" in ours
    assert "--property=TemporaryFileSystem=/run/dbus" in ours
    assert "--property=InaccessiblePaths=-/run/systemd" not in ours
    assert "--property=InaccessiblePaths=-/run/dbus" not in ours
    # Transient argv uses the explicit unit name; specifiers are not expanded
    # by `systemd-run -p` and would create a literal `%N`/`%i` directory.
    assert f"--property=StateDirectory=agentmgr-exec/{UNIT}" in ours
    assert not any("%N" in arg or "%i" in arg for arg in ours)
