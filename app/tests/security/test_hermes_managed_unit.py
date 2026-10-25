"""Unit tests for the Hermes managed sandbox root helper (#97).

The helper in ``ops/security/agentmgr_hermes_exec.py`` is a production template:
nothing here runs ``sudo``, ``systemd-run`` or touches a real unit.  Every
privileged command goes through a scripted runner, and the task directory lives
in a short temporary tree so ownership/mode checks run unprivileged.

Two properties are pinned here rather than in the adapter tests:

* the runtime contract parity with ``app.runtime.hermes_managed`` (unit name,
  runtime handle and the signed attestation documents are verified by the Task 1
  verifiers), and
* the fixed deployment surface (wrapper text, sudoers rule, no ``app.*`` import,
  no caller controlled systemd property).
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import importlib.util
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.hermes_managed import (
    RUNTIME_ATTESTATION_SCHEMA,
    TASK_ATTESTATION_SCHEMA,
    AttestationError,
    HermesManagedConfig,
    RuntimeAttestation,
    TaskAttestation,
    build_sandbox_spec,
    runtime_handle_for,
    unit_name_for,
    verify_runtime_attestation,
    verify_task_attestation,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS = REPO_ROOT / "ops" / "security"
WRAPPER = OPS / "agentmgr-hermes-exec"
SUDOERS = OPS / "agentmgr-hermes.sudoers"

TASK_ID = "task_synthetic_0001"
GRANT_ID = "grnt_synthetic_0001"
IDEMPOTENCY_KEY = "idem_synthetic_0001"
PROMPT = b"PROMPT-CANARY write out/result.txt"
PROFILE_TEMPLATE = b"model:\n  base_url: http://127.0.0.1:__PORT__/v1\n"
TOKEN = "egt.synthetic.canary.token"
KEY = b"k" * 32
RUNTIME_ROOT_HASH = "sha256:" + "a" * 64
EVIDENCE_REF = "evidence:hermes:unit-vector"

#: Fixed shared vectors: both the helper and Task 1 must derive exactly these.
PINNED_UNIT = "agentmgr-hermes-246feaffb6802118"
PINNED_HANDLE = "rtask_hermes_b8a8d68eaf775211c33654c5d19d8b77"

TASK_DIR = Path("/var/lib/agentmgr-hermes/tasks") / unit_name_for(TASK_ID, IDEMPOTENCY_KEY)
RUNTIME_ROOT = Path("/var/lib/agentmgr-hermes/runtime")
BROKER_SOCKET = Path("/var/lib/agentmgr-hermes/egress.sock")
WORKLOAD = Path("/var/lib/agentmgr-hermes/workload.py")

FORBIDDEN_FIELDS = (
    "ExecStart",
    "ExecStartPre",
    "Environment",
    "EnvironmentFile",
    "BindPaths",
    "BindReadOnlyPaths",
    "User",
    "Group",
    "DynamicUser",
    "PrivateNetwork",
    "ProtectHome",
    "ProtectSystem",
    "KillMode",
    "RuntimeMaxSec",
    "SupplementaryGroups",
    "CapabilityBoundingSet",
    "SystemCallFilter",
    "TemporaryFileSystem",
    "StateDirectory",
    "LoadCredential",
)


def _load_helper():
    path = OPS / "agentmgr_hermes_exec.py"
    spec = importlib.util.spec_from_file_location("agentmgr_hermes_exec", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclass(slots=True)`` resolves type names through ``sys.modules``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()
UNIT = helper.unit_for_task(TASK_ID, IDEMPOTENCY_KEY)
HANDLE = helper.runtime_handle_for(UNIT)


# ---------------------------------------------------------------------------
# synthetic requests / runners
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def base_request(**overrides: object) -> dict[str, object]:
    """A launch request with the exact 15 field names of the helper contract."""

    request: dict[str, object] = {
        "operation": "launch",
        "task_id": TASK_ID,
        "grant_id": GRANT_ID,
        "unit": UNIT,
        "runtime_handle": HANDLE,
        "idempotency_key": IDEMPOTENCY_KEY,
        "prompt_sha256": _sha256(PROMPT),
        "prompt_bytes": len(PROMPT),
        "profile_template_sha256": _sha256(PROFILE_TEMPLATE),
        "egress_token_sha256": _sha256(TOKEN.encode("utf-8")),
        "ttl_seconds": 300,
        "runtime_max_sec": 60,
        "memory_max_mb": 512,
        "cpu_quota_percent": 100,
        "tasks_max": 64,
    }
    request.update(overrides)
    return request


def valid_request(**overrides: object) -> object:
    return helper.validate_request(base_request(**overrides))


def _show(
    *,
    load_state: str = "loaded",
    active_state: str = "inactive",
    sub_state: str = "dead",
    main_pid: int = 0,
    result: str = "success",
    exit_status: str = "0",
    control_group: str = "",
    unit: str = UNIT,
) -> str:
    return "\n".join(
        (
            f"Id={unit}.service",
            f"LoadState={load_state}",
            f"ActiveState={active_state}",
            f"SubState={sub_state}",
            f"MainPID={main_pid}",
            f"Result={result}",
            f"ExecMainStatus={exit_status}",
            "ActiveEnterTimestamp=Sun 2026-10-25 12:00:00 UTC",
            "ExecMainStartTimestamp=Sun 2026-10-25 12:00:00 UTC",
            "ExecMainExitTimestamp=",
            f"ControlGroup={control_group}",
            "",
        )
    )


SHOW_NOT_FOUND = _show(load_state="not-found", active_state="inactive")
SHOW_ACTIVE = _show(active_state="active", sub_state="running", main_pid=os.getpid())
SHOW_ACTIVATING = _show(active_state="activating", sub_state="start-pre")
SHOW_FAILED = _show(active_state="failed", sub_state="failed", result="exit-code")
SHOW_INACTIVE = _show(active_state="inactive")


class ScriptedRunner:
    """Record argv and answer ``systemctl show`` from a scripted list."""

    def __init__(self, show_outputs: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._show_outputs = list(show_outputs or [])
        self.last_show: str = ""

    def systemctl_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == helper.SYSTEMCTL]

    def launch_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == helper.SYSTEMD_RUN]

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, list)
        assert all(isinstance(item, str) for item in argv)
        self.calls.append(list(argv))
        stdout = ""
        if argv[:2] == [helper.SYSTEMCTL, "show"]:
            if self._show_outputs:
                stdout = self._show_outputs.pop(0)
            else:
                stdout = self.last_show or SHOW_NOT_FOUND
            self.last_show = stdout
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class FailingRunner(ScriptedRunner):
    """Fail the ``systemd-run`` call itself."""

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv and argv[0] == helper.SYSTEMD_RUN:
            self.calls.append(list(argv))
            raise subprocess.CalledProcessError(1, argv)
        return super().__call__(argv)


class KillAwareRunner(ScriptedRunner):
    """Stay active until a ``SIGKILL`` is sent, then report the unit gone."""

    def __init__(self) -> None:
        super().__init__()
        self.terminated = False
        self.killed = False

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "--signal=SIGTERM" in argv:
            self.terminated = True
        if "--signal=SIGKILL" in argv:
            self.killed = True
        assert isinstance(argv, list)
        assert all(isinstance(item, str) for item in argv)
        self.calls.append(list(argv))
        stdout = ""
        if argv[:2] == [helper.SYSTEMCTL, "show"]:
            stdout = SHOW_INACTIVE if self.killed else SHOW_ACTIVE
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class FakeClock:
    """Deterministic monotonic clock so stop escalation does not sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch):
    if os.geteuid() == 0:
        pytest.skip("ownership checks require an unprivileged test user")
    root = Path(tempfile.mkdtemp(prefix="hermes-unit-"))
    try:
        # Unit tests must live under /tmp, which the production helper rejects
        # as a bind source (PrivateTmp). The key root is redirected to a
        # synthetic ``etc/agentmgr`` so the "unit-invisible root" rule can be
        # exercised without root privileges.
        key_root = root / "etc" / "agentmgr"
        key_root.mkdir(parents=True)
        monkeypatch.setattr(helper, "ATTESTATION_KEY_ROOT", key_root)
        monkeypatch.setattr(helper, "ALLOW_PRIVATE_TMP_SOURCES", True)

        task_root = root / "tasks"
        task_root.mkdir()
        task_root.chmod(0o770)
        runtime_root = root / "runtime"
        runtime_root.mkdir()
        runtime_root.chmod(0o755)
        workload = root / "workload.py"
        workload.write_text("# synthetic workload\n", encoding="utf-8")
        workload.chmod(0o555)
        broker_socket = root / "s.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(broker_socket))
        key_path = key_root / "attestation.key"
        key_path.write_bytes(KEY)
        key_path.chmod(0o600)

        uid = os.geteuid()
        gid = os.getegid()
        config = helper.InstallConfig(
            task_root=task_root,
            runtime_root=runtime_root,
            runtime_root_hash=RUNTIME_ROOT_HASH,
            broker_socket=broker_socket,
            workload=workload,
            adapter_user=pwd.getpwuid(uid).pw_name,
            adapter_group=helper.GROUP_NAME,
            attestation_key_path=key_path,
            evidence_ref=EVIDENCE_REF,
            runtime_name="hermes-agent",
            runtime_version="0.22.1",
            adapter_uid=uid,
            adapter_gid=gid,
        )
        yield SimpleNamespace(
            root=root,
            key_root=key_root,
            task_root=task_root,
            runtime_root=runtime_root,
            workload=workload,
            broker_socket=broker_socket,
            key_path=key_path,
            config=config,
            uid=uid,
            gid=gid,
            task_dir=task_root / UNIT,
        )
        listener.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _prepare(sandbox: SimpleNamespace) -> object:
    return helper.prepare(valid_request(operation="prepare"), sandbox.config)


def _write_task_files(
    sandbox: SimpleNamespace,
    *,
    prompt: bytes = PROMPT,
    profile: bytes = PROFILE_TEMPLATE,
    token: str = TOKEN,
    token_mode: int = 0o640,
    prompt_mode: int = 0o640,
) -> None:
    task_dir = sandbox.task_dir
    (task_dir / "prompt.txt").write_bytes(prompt)
    (task_dir / "prompt.txt").chmod(prompt_mode)
    (task_dir / "profile.template.yaml").write_bytes(profile)
    (task_dir / "profile.template.yaml").chmod(0o640)
    (task_dir / "egress.token").write_text(token, encoding="utf-8")
    (task_dir / "egress.token").chmod(token_mode)
    for name in ("prompt.txt", "profile.template.yaml", "egress.token"):
        os.chown(task_dir / name, sandbox.uid, sandbox.gid)


def _write_started(sandbox: SimpleNamespace, *, unit: str = UNIT) -> None:
    status_dir = sandbox.task_dir / "status"
    status_dir.mkdir(exist_ok=True)
    payload = {
        "unit": unit,
        "task_id": TASK_ID,
        "runtime_handle": HANDLE,
        "started_at": "2026-10-25T12:00:00Z",
    }
    (status_dir / "started.json").write_text(json.dumps(payload), encoding="utf-8")


def _launch_ready(sandbox: SimpleNamespace, runner: ScriptedRunner) -> object:
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    return helper.launch(valid_request(), sandbox.config, runner=runner)


def _managed_config(sandbox: SimpleNamespace) -> HermesManagedConfig:
    return HermesManagedConfig(
        helper_path=WRAPPER,
        runtime_root=sandbox.runtime_root,
        runtime_root_hash=RUNTIME_ROOT_HASH,
        task_root=sandbox.task_root,
        broker_socket=sandbox.broker_socket,
        attestation_key_path=sandbox.key_path,
        egress_token_key_path=sandbox.key_path,
        runtime_attestation_path=sandbox.root / "runtime-attestation.json",
        evidence_ref=EVIDENCE_REF,
        runtime_name="hermes-agent",
        runtime_version="0.22.1",
        model_name="model-example",
        egress_provider_host="provider.example",
        worker_id="worker-example",
    )


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------


def test_request_rejects_unknown_or_forbidden_fields():
    for field in FORBIDDEN_FIELDS:
        with pytest.raises(helper.HelperError) as excinfo:
            helper.validate_request({**base_request(), field: "x"})
        assert excinfo.value.code == "forbidden_field"

    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request({**base_request(), "extra": 1})
    assert excinfo.value.code == "unknown_field"

    missing = base_request()
    missing.pop("tasks_max")
    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(missing)
    assert excinfo.value.code == "missing_field"

    with pytest.raises(helper.HelperError):
        helper.validate_request([])
    with pytest.raises(helper.HelperError):
        helper.validate_request({"operation": "launch"})


@pytest.mark.parametrize(
    "operation", ["prepare", "launch", "status", "stop", "cleanup", "attest"]
)
def test_every_operation_accepts_the_same_exact_request_shape(operation: str):
    overrides = {"result_read": True} if operation == "cleanup" else {}
    request = valid_request(operation=operation, **overrides)
    assert request.operation == operation
    assert request.unit == UNIT


def test_request_rejects_unknown_operation():
    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(base_request(operation="shell"))
    assert excinfo.value.code == "invalid_operation"


def test_cleanup_requires_explicit_result_read_and_nothing_else_extra():
    helper.validate_request(base_request(operation="cleanup", result_read=True))
    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(base_request(operation="cleanup"))
    assert excinfo.value.code == "missing_field"
    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(base_request(operation="cleanup", result_read=False))
    assert excinfo.value.code == "result_not_read"
    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(base_request(result_read=True))
    assert excinfo.value.code == "unknown_field"


def test_request_binds_unit_handle_task_and_grant():
    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(base_request(unit="agentmgr-hermes-0000000000000000"))
    assert excinfo.value.code == "unit_name_mismatch"

    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(base_request(runtime_handle="rtask_hermes_" + "0" * 32))
    assert excinfo.value.code == "runtime_handle_mismatch"

    for field in ("task_id", "grant_id", "idempotency_key"):
        with pytest.raises(helper.HelperError) as excinfo:
            helper.validate_request(base_request(**{field: ""}))
        assert excinfo.value.code == "invalid_request"

    for field in ("task_id", "grant_id", "runtime_handle"):
        with pytest.raises(helper.HelperError):
            helper.validate_request(base_request(**{field: "bad\nvalue"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ttl_seconds", 0),
        ("ttl_seconds", 301),
        ("ttl_seconds", True),
        ("prompt_bytes", 0),
        ("prompt_bytes", helper.MAX_PROMPT_BYTES + 1),
        ("runtime_max_sec", 0),
        ("runtime_max_sec", helper.MAX_RUNTIME_MAX_SEC + 1),
        ("memory_max_mb", 0),
        ("memory_max_mb", helper.MAX_MEMORY_MAX_MB + 1),
        ("cpu_quota_percent", 0),
        ("cpu_quota_percent", helper.MAX_CPU_QUOTA_PERCENT + 1),
        ("tasks_max", 0),
        ("tasks_max", helper.MAX_TASKS_MAX + 1),
        ("ttl_seconds", "300"),
        ("prompt_sha256", "0" * 63),
        ("profile_template_sha256", "A" * 64),
        ("egress_token_sha256", "../" + "0" * 61),
    ],
)
def test_request_rejects_out_of_range_numbers_and_bad_hashes(field: str, value: object):
    with pytest.raises(helper.HelperError) as excinfo:
        helper.validate_request(base_request(**{field: value}))
    assert excinfo.value.code in {
        "invalid_request",
        "invalid_limit",
        "invalid_digest",
    }


# ---------------------------------------------------------------------------
# derivation parity with Task 1
# ---------------------------------------------------------------------------


def test_unit_name_matches_task1_contract_and_shared_vector():
    assert helper.unit_for_task("task_abc", "idem_abc") == PINNED_UNIT
    assert unit_name_for("task_abc", "idem_abc") == PINNED_UNIT
    assert helper.unit_for_task("task_abc", "idem_abc") == unit_name_for(
        "task_abc", "idem_abc"
    )
    assert helper.unit_for_task("task_abc", "idem_other") == unit_name_for(
        "task_abc", "idem_other"
    )
    assert helper.unit_for_task("task_abc", "idem_abc") != helper.unit_for_task(
        "task_abc", "idem_other"
    )
    assert helper.UNIT_PATTERN.fullmatch(UNIT)


@pytest.mark.parametrize(
    ("task_id", "idempotency_key"),
    [
        ("task_a\nidem_b", "c"),
        ("task_a", "idem_b\nc"),
        ("task_a\x00idem_b", "c"),
        ("task_a", "idem_b\x7fc"),
    ],
)
def test_unit_name_rejects_control_characters_like_task1(
    task_id: str, idempotency_key: str
):
    with pytest.raises(helper.HelperError):
        helper.unit_for_task(task_id, idempotency_key)


def test_runtime_handle_matches_task1_contract():
    assert helper.runtime_handle_for(PINNED_UNIT) == PINNED_HANDLE
    assert runtime_handle_for(PINNED_UNIT) == PINNED_HANDLE
    assert helper.runtime_handle_for(UNIT) == runtime_handle_for(UNIT)
    with pytest.raises(helper.HelperError):
        helper.runtime_handle_for("agentmgr-hermes-abc")
    with pytest.raises(helper.HelperError):
        helper.runtime_handle_for("")


# ---------------------------------------------------------------------------
# systemd argv
# ---------------------------------------------------------------------------


def test_systemd_argv_has_fixed_sandbox_properties():
    argv = helper.build_systemd_run_argv(
        valid_request(),
        task_dir=TASK_DIR,
        runtime_root=RUNTIME_ROOT,
        broker_socket=BROKER_SOCKET,
        workload=WORKLOAD,
    )
    assert argv[0] == helper.SYSTEMD_RUN
    assert Path(argv[0]).name == "systemd-run"
    assert f"--unit={UNIT}" in argv

    for prop in (
        "DynamicUser=yes",
        f"Group={helper.GROUP_NAME}",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "TemporaryFileSystem=/run",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "SystemCallFilter=@system-service",
        "SystemCallArchitectures=native",
        "SystemCallErrorNumber=EPERM",
        "RestrictNamespaces=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "RemoveIPC=yes",
        "ProtectClock=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "UMask=0007",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "ExitType=cgroup",
        "WorkingDirectory=/task",
        f"BindPaths={TASK_DIR}:/task",
        f"BindReadOnlyPaths={RUNTIME_ROOT}:/runtime",
        f"BindReadOnlyPaths={BROKER_SOCKET}:/task/egress.sock",
        f"BindReadOnlyPaths={WORKLOAD}:/workload.py",
        "MemoryMax=512M",
        "CPUQuota=100%",
        "TasksMax=64",
        "RuntimeMaxSec=60s",
    ):
        assert f"--property={prop}" in argv

    for masked in (
        "InaccessiblePaths=-/etc/agentmgr",
        "InaccessiblePaths=-/var/lib/agentmgr",
        "InaccessiblePaths=-/var/lib/postgresql",
        "InaccessiblePaths=-/srv",
        "InaccessiblePaths=-/var/lib/docker",
        "InaccessiblePaths=-/var/lib/containers",
        "InaccessiblePaths=-/var/lib/containerd",
        f"InaccessiblePaths=-{TASK_DIR.parent}",
    ):
        assert f"--property={masked}" in argv

    # The task root is hidden, but its own task directory stays bind-mounted.
    assert f"--property=BindPaths={TASK_DIR}:/task" in argv
    assert not any("SupplementaryGroups=" in item for item in argv)
    assert "--property=Group=agentmgr-hermes" in argv
    assert "--property=PrivateDevices=yes" in argv

    worklaod_argv = list(helper.WORKLOAD_ARGV)
    assert argv[-len(worklaod_argv) :] == worklaod_argv
    assert not any(item.startswith("--property=ExecStart") for item in argv)
    assert not any(item.startswith("--property=Environment=") for item in argv)
    assert not any(item.startswith("--property=User=") for item in argv)
    # ``Group`` is fixed by the helper (static sandbox group); a request can
    # never express it because ``Group`` is a forbidden request field.
    assert [item for item in argv if item.startswith("--property=Group=")] == [
        "--property=Group=agentmgr-hermes"
    ]
    assert not any(item.startswith("--property=PrivateNetwork=no") for item in argv)
    assert "--property=RuntimeMaxSec=60s" in argv
    assert "--property=RestrictAddressFamilies=AF_UNIX" not in argv


def test_systemd_argv_is_deterministic_and_free_of_payload():
    first = helper.build_systemd_run_argv(
        valid_request(),
        task_dir=TASK_DIR,
        runtime_root=RUNTIME_ROOT,
        broker_socket=BROKER_SOCKET,
        workload=WORKLOAD,
    )
    second = helper.build_systemd_run_argv(
        valid_request(),
        task_dir=TASK_DIR,
        runtime_root=RUNTIME_ROOT,
        broker_socket=BROKER_SOCKET,
        workload=WORKLOAD,
    )
    assert first == second
    flattened = "\n".join(first)
    for canary in (PROMPT, PROFILE_TEMPLATE, TOKEN, KEY):
        text = canary.decode("utf-8") if isinstance(canary, bytes) else canary
        assert text not in flattened


def test_systemd_argv_uses_the_configured_stop_timeout():
    argv = helper.build_systemd_run_argv(
        valid_request(),
        task_dir=TASK_DIR,
        runtime_root=RUNTIME_ROOT,
        broker_socket=BROKER_SOCKET,
        workload=WORKLOAD,
        stop_timeout_sec=7,
    )
    assert "--property=TimeoutStopSec=7" in argv
    with pytest.raises(helper.HelperError):
        helper.build_systemd_run_argv(
            valid_request(),
            task_dir=TASK_DIR,
            runtime_root=RUNTIME_ROOT,
            broker_socket=BROKER_SOCKET,
            workload=WORKLOAD,
            stop_timeout_sec=0,
        )


def test_systemd_argv_rejects_unsafe_or_unfixed_paths():
    with pytest.raises(helper.HelperError):
        helper.build_systemd_run_argv(
            valid_request(),
            task_dir=Path("/var/lib/agentmgr-hermes/tasks/agentmgr-hermes-0000000000000000"),
            runtime_root=RUNTIME_ROOT,
            broker_socket=BROKER_SOCKET,
            workload=WORKLOAD,
        )
    for kwargs in (
        {"task_dir": Path("relative/task"), "runtime_root": RUNTIME_ROOT},
        {"task_dir": TASK_DIR / ".." / UNIT, "runtime_root": RUNTIME_ROOT},
        {"task_dir": TASK_DIR, "runtime_root": Path("/run/hermes-runtime")},
        {"task_dir": TASK_DIR, "runtime_root": RUNTIME_ROOT, "broker_socket": Path("/run/s.sock")},
        {"task_dir": TASK_DIR, "runtime_root": RUNTIME_ROOT, "broker_socket": BROKER_SOCKET, "workload": Path("/var/lib/agentmgr/workload.py")},
    ):
        arguments = {
            "task_dir": TASK_DIR,
            "runtime_root": RUNTIME_ROOT,
            "broker_socket": BROKER_SOCKET,
            "workload": WORKLOAD,
        }
        arguments.update(kwargs)
        with pytest.raises(helper.HelperError):
            helper.build_systemd_run_argv(valid_request(), **arguments)

    # A task directory directly under "/" would mask the whole root.
    with pytest.raises(helper.HelperError):
        helper.build_systemd_run_argv(
            valid_request(),
            task_dir=Path("/") / UNIT,
            runtime_root=RUNTIME_ROOT,
            broker_socket=BROKER_SOCKET,
            workload=WORKLOAD,
        )


def test_systemd_argv_revalidates_hand_built_request_limits():
    for overrides in (
        {"ttl_seconds": 0},
        {"ttl_seconds": 301},
        {"prompt_bytes": 0},
        {"runtime_max_sec": 0},
        {"memory_max_mb": 0},
        {"cpu_quota_percent": 0},
        {"tasks_max": 0},
        {"prompt_sha256": "0" * 63},
        {"runtime_handle": "rtask_hermes_" + "0" * 32},
        {"grant_id": ""},
    ):
        hand_built = dataclasses.replace(valid_request(), **overrides)
        with pytest.raises(helper.HelperError):
            helper.build_systemd_run_argv(
                hand_built,
                task_dir=TASK_DIR,
                runtime_root=RUNTIME_ROOT,
                broker_socket=BROKER_SOCKET,
                workload=WORKLOAD,
            )


# ---------------------------------------------------------------------------
# systemctl show parsing
# ---------------------------------------------------------------------------


def test_parse_systemctl_show_maps_the_whitelist():
    state = helper.parse_systemctl_show(
        "\n".join(
            (
                f"Id={UNIT}.service",
                "LoadState=loaded",
                "ActiveState=active",
                "SubState=running",
                "MainPID=4242",
                "Result=success",
                "ExecMainStatus=0",
                "ActiveEnterTimestamp=Sun 2026-10-25 12:00:00 UTC",
                "ExecMainStartTimestamp=Sun 2026-10-25 12:00:01 UTC",
                "ExecMainExitTimestamp=",
                "ControlGroup=/system.slice/ignored.service",
                "Noise=ignored",
                "garbage-line",
                "",
            )
        )
    )
    assert state.unit == UNIT
    assert state.active_state == "active"
    assert state.sub_state == "running"
    assert state.main_pid == 4242
    assert state.result == "success"
    assert state.exec_main_status == 0
    assert state.exec_main_start == "Sun 2026-10-25 12:00:01 UTC"
    assert state.exec_main_exit is None
    assert state.process_gone is False
    assert helper.unit_state_document(state) == {
        "load_state": "loaded",
        "active_state": "active",
        "sub_state": "running",
        "main_pid": 4242,
        "result": "success",
        "exit_status": 0,
        "active_enter": "Sun 2026-10-25 12:00:00 UTC",
        "exec_main_start": "Sun 2026-10-25 12:00:01 UTC",
        "exec_main_exit": None,
        "process_gone": False,
    }
    assert "ControlGroup" not in json.dumps(helper.unit_state_document(state))


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        (SHOW_INACTIVE, True),
        (SHOW_FAILED, True),
        (SHOW_NOT_FOUND, True),
        (SHOW_ACTIVE, False),
        (SHOW_ACTIVATING, False),
        ("", False),
    ],
)
def test_parse_systemctl_show_process_gone(text: str, gone: bool):
    assert helper.parse_systemctl_show(text).process_gone is gone


def test_unit_state_document_nulls_not_found_result_and_exit_status():
    state = helper.parse_systemctl_show(SHOW_NOT_FOUND)
    assert state.result == "success"
    assert state.exec_main_status == 0

    document = helper.unit_state_document(state)
    assert document["load_state"] == "not-found"
    assert document["result"] is None
    assert document["exit_status"] is None
    assert document["process_gone"] is True


# ---------------------------------------------------------------------------
# attestation signing
# ---------------------------------------------------------------------------


def test_sign_attestation_is_canonical_hmac_sha256():
    payload = {"b": 2, "a": "x", "nested": {"z": [1, 2]}}
    signed = helper.sign_attestation(payload, KEY)

    expected = hmac.new(
        KEY, b'{"a":"x","b":2,"nested":{"z":[1,2]}}', hashlib.sha256
    ).hexdigest()
    assert signed["hmac"] == expected
    assert "hmac" not in payload
    assert set(signed) == {"a", "b", "nested", "hmac"}

    tampered = dict(signed, a="y")
    with pytest.raises(helper.HelperError):
        helper.sign_attestation(tampered, KEY)
    with pytest.raises(helper.HelperError):
        helper.sign_attestation(payload, b"too-short")


def test_signed_task_attestation_is_accepted_by_task1_verifier(sandbox):
    runner = ScriptedRunner([SHOW_NOT_FOUND, SHOW_ACTIVE])
    response = _launch_ready(sandbox, runner)
    document = response["attestation"]
    assert document["schema"] == TASK_ATTESTATION_SCHEMA
    assert document["runtime_root_hash"] == RUNTIME_ROOT_HASH
    assert document["uid"] == sandbox.uid

    spec = build_sandbox_spec(
        _managed_config(sandbox),
        task_id=TASK_ID,
        grant_id=GRANT_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        prompt=PROMPT.decode("utf-8"),
        profile_template=PROFILE_TEMPLATE.decode("utf-8"),
        egress_token=TOKEN,
    )
    attestation = TaskAttestation.from_document(document)
    verify_task_attestation(
        attestation,
        key=KEY,
        spec=spec,
        expected_runtime_root_hash=RUNTIME_ROOT_HASH,
        now=datetime.now(UTC),
    )

    tampered = dict(document, prompt_sha256="0" * 64)
    with pytest.raises(AttestationError):
        verify_task_attestation(
            TaskAttestation.from_document(tampered),
            key=KEY,
            spec=spec,
            expected_runtime_root_hash=RUNTIME_ROOT_HASH,
            now=datetime.now(UTC),
        )
    with pytest.raises(AttestationError):
        verify_task_attestation(
            attestation,
            key=b"other" * 8,
            spec=spec,
            expected_runtime_root_hash=RUNTIME_ROOT_HASH,
            now=datetime.now(UTC),
        )


def test_runtime_attestation_is_accepted_by_task1_verifier(sandbox):
    _launch_ready(sandbox, ScriptedRunner([SHOW_NOT_FOUND, SHOW_ACTIVE]))
    response = helper.attest(
        valid_request(operation="attest"),
        sandbox.config,
        runner=ScriptedRunner([SHOW_ACTIVE]),
    )
    document = response["runtime_attestation"]
    assert document["schema"] == RUNTIME_ATTESTATION_SCHEMA
    assert document["runtime_root_hash"] == RUNTIME_ROOT_HASH
    assert document["evidence_ref"] == EVIDENCE_REF
    assert document["broker_socket_ok"] is True
    assert document["helper_version"] == helper.HELPER_VERSION
    assert set(document) == set(RuntimeAttestation.from_document(document).to_document())

    verified = verify_runtime_attestation(
        RuntimeAttestation.from_document(document),
        key=KEY,
        config=_managed_config(sandbox),
        now=datetime.now(UTC),
    )
    assert verified.attestation.runtime_root_hash == RUNTIME_ROOT_HASH

    with pytest.raises(AttestationError):
        verify_runtime_attestation(
            RuntimeAttestation.from_document(dict(document, broker_socket_ok=False)),
            key=KEY,
            config=_managed_config(sandbox),
            now=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# install config
# ---------------------------------------------------------------------------


def _install_config_document(sandbox: SimpleNamespace, **overrides: object) -> dict:
    document: dict[str, object] = {
        "task_root": str(sandbox.task_root),
        "runtime_root": str(sandbox.runtime_root),
        "runtime_root_hash": RUNTIME_ROOT_HASH,
        "broker_socket": str(sandbox.broker_socket),
        "workload": str(sandbox.workload),
        "adapter_user": pwd.getpwuid(sandbox.uid).pw_name,
        "adapter_group": helper.GROUP_NAME,
        "attestation_key_path": str(sandbox.key_path),
        "evidence_ref": EVIDENCE_REF,
        "runtime_name": "hermes-agent",
        "runtime_version": "0.22.1",
        "adapter_uid": sandbox.uid,
        "adapter_gid": sandbox.gid,
    }
    document.update(overrides)
    return document


def test_load_install_config_roundtrip(sandbox):
    path = sandbox.root / "install.json"
    path.write_text(json.dumps(_install_config_document(sandbox)), encoding="utf-8")
    path.chmod(0o600)

    config = helper.load_install_config(path)
    assert config.task_root == sandbox.task_root
    assert config.runtime_root_hash == RUNTIME_ROOT_HASH
    assert config.adapter_group == helper.GROUP_NAME
    assert config.helper_version == helper.HELPER_VERSION
    assert config.stop_timeout_sec >= 1
    assert config.start_timeout_sec >= 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown": "x"},
        {"adapter_group": "sudo"},
        {"runtime_root_hash": "sha256:" + "A" * 64},
        {"runtime_root_hash": "not-a-digest"},
        {"task_root": "relative/tasks"},
        {"broker_socket": "/run/agentmgr-hermes/egress.sock"},
        {"runtime_root": "/run/hermes-runtime"},
        {"workload": "/var/lib/agentmgr/workload.py"},
        {"task_root": "/var/lib/agentmgr/tasks"},
        {"adapter_uid": 0},
        {"stop_timeout_sec": 0},
    ],
)
def test_load_install_config_fails_closed(sandbox, overrides):
    path = sandbox.root / "install.json"
    document = _install_config_document(sandbox, **overrides)
    if overrides.get("unknown"):
        document["unknown"] = "x"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(helper.HelperError):
        helper.load_install_config(path)


def test_load_install_config_rejects_missing_unsafe_or_symlinked_file(sandbox):
    missing = sandbox.root / "missing.json"
    with pytest.raises(helper.HelperError) as excinfo:
        helper.load_install_config(missing)
    assert excinfo.value.code == "install_config_unavailable"

    path = sandbox.root / "install.json"
    path.write_text(json.dumps(_install_config_document(sandbox)), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.load_install_config(path)
    assert excinfo.value.code == "install_config_mode_invalid"

    path.chmod(0o600)
    target = sandbox.root / "install-target.json"
    os.replace(path, target)
    path.symlink_to(target)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.load_install_config(path)
    assert excinfo.value.code == "install_config_invalid"


def test_load_install_config_rejects_missing_required_field(sandbox):
    path = sandbox.root / "install.json"
    document = _install_config_document(sandbox)
    document.pop("evidence_ref")
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.load_install_config(path)
    assert excinfo.value.code == "install_config_missing_field"


def test_load_install_config_accepts_read_only_group_variant(sandbox):
    path = sandbox.root / "install.json"
    path.write_text(json.dumps(_install_config_document(sandbox)), encoding="utf-8")
    path.chmod(0o640)
    config = helper.load_install_config(path)
    assert config.adapter_group == helper.GROUP_NAME


def test_attestation_key_root_defaults_to_the_unit_invisible_directory():
    assert helper.ATTESTATION_KEY_ROOT == Path("/etc/agentmgr")
    assert "-/etc/agentmgr" in helper.INACCESSIBLE_PATHS


def test_install_config_rejects_private_tmp_bind_sources(sandbox, monkeypatch):
    monkeypatch.setattr(helper, "ALLOW_PRIVATE_TMP_SOURCES", False)

    path = sandbox.root / "install.json"
    path.write_text(json.dumps(_install_config_document(sandbox)), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.load_install_config(path)
    assert excinfo.value.code == "install_config_private_tmp_source"

    with pytest.raises(helper.HelperError) as excinfo:
        helper.prepare(valid_request(operation="prepare"), sandbox.config)
    assert excinfo.value.code == "install_config_private_tmp_source"

    monkeypatch.setattr(helper, "ALLOW_PRIVATE_TMP_SOURCES", True)
    assert helper.prepare(valid_request(operation="prepare"), sandbox.config)[
        "prepared"
    ] is True


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_creates_task_and_out_dirs_with_fixed_owner_group_and_mode(sandbox):
    response = _prepare(sandbox)

    assert response["prepared"] is True
    assert response["created"] is True
    assert response["unit"] == UNIT
    task_dir = sandbox.task_dir
    out_dir = task_dir / "out"
    assert task_dir.is_dir() and not task_dir.is_symlink()
    assert out_dir.is_dir() and not out_dir.is_symlink()
    for path in (task_dir, out_dir):
        info = os.lstat(path)
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o770
        assert info.st_uid == sandbox.uid
        assert info.st_gid == sandbox.gid

    manifest = task_dir / "manifest.json"
    info = os.lstat(manifest)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o640
    assert info.st_uid == sandbox.uid
    assert info.st_gid == sandbox.gid
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["unit"] == UNIT
    assert document["task_id"] == TASK_ID
    assert document["grant_id"] == GRANT_ID
    assert document["runtime_handle"] == HANDLE
    assert document["runtime_root_hash"] == RUNTIME_ROOT_HASH
    assert document["idempotency_key"] == IDEMPOTENCY_KEY
    assert re.fullmatch(r"[0-9a-f]{64}", str(document["payload_sha256"]))
    assert len(str(document["payload_sha256"])) == 64
    assert document["prompt_sha256"] == _sha256(PROMPT)
    assert document["toolset"] == "terminal"
    assert document["egress_socket"] == "/task/egress.sock"
    assert TOKEN not in manifest.read_text(encoding="utf-8")
    assert PROMPT.decode("utf-8") not in manifest.read_text(encoding="utf-8")

    digest = document["payload_sha256"]
    again = helper.prepare(valid_request(operation="prepare"), sandbox.config)
    assert again["created"] is False
    # The payload digest is stable across idempotent re-prepares.
    assert json.loads(manifest.read_text(encoding="utf-8"))["payload_sha256"] == digest


def test_prepare_rejects_symlinked_task_root(sandbox):
    outside = sandbox.root / "outside"
    outside.mkdir()
    link_root = sandbox.root / "linked-tasks"
    link_root.symlink_to(outside, target_is_directory=True)
    config = dataclasses.replace(sandbox.config, task_root=link_root)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.prepare(valid_request(operation="prepare"), config)
    assert excinfo.value.code == "task_root_invalid"
    assert list(outside.iterdir()) == []


def test_prepare_rejects_symlinked_task_dir(sandbox):
    outside = sandbox.root / "outside"
    outside.mkdir()
    (sandbox.task_root / UNIT).symlink_to(outside, target_is_directory=True)
    with pytest.raises(helper.HelperError) as excinfo:
        _prepare(sandbox)
    assert excinfo.value.code == "task_dir_invalid"
    assert list(outside.iterdir()) == []


def test_prepare_rejects_wrong_owner_and_wrong_mode(sandbox):
    task_dir = sandbox.task_dir
    task_dir.mkdir()
    task_dir.chmod(0o770)

    wrong_owner = helper.InstallConfig(
        task_root=sandbox.config.task_root,
        runtime_root=sandbox.config.runtime_root,
        runtime_root_hash=sandbox.config.runtime_root_hash,
        broker_socket=sandbox.config.broker_socket,
        workload=sandbox.config.workload,
        adapter_user=sandbox.config.adapter_user,
        adapter_group=sandbox.config.adapter_group,
        attestation_key_path=sandbox.config.attestation_key_path,
        evidence_ref=sandbox.config.evidence_ref,
        runtime_name=sandbox.config.runtime_name,
        runtime_version=sandbox.config.runtime_version,
        adapter_uid=sandbox.uid + 1,
        adapter_gid=sandbox.gid,
    )
    with pytest.raises(helper.HelperError) as excinfo:
        helper.prepare(valid_request(operation="prepare"), wrong_owner)
    assert excinfo.value.code == "task_dir_owner_invalid"

    task_dir.chmod(0o777)
    with pytest.raises(helper.HelperError) as excinfo:
        _prepare(sandbox)
    assert excinfo.value.code == "task_dir_mode_invalid"


def test_prepare_rejects_wrong_group(sandbox):
    config = dataclasses.replace(sandbox.config, adapter_gid=sandbox.gid + 1)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.prepare(valid_request(operation="prepare"), config)
    assert excinfo.value.code in {"task_root_group_invalid", "task_dir_group_invalid"}


def test_prepare_rejects_missing_roots(sandbox):
    with pytest.raises(helper.HelperError):
        helper.prepare(
            valid_request(operation="prepare"),
            helper.InstallConfig(
                task_root=sandbox.root / "missing",
                runtime_root=sandbox.config.runtime_root,
                runtime_root_hash=sandbox.config.runtime_root_hash,
                broker_socket=sandbox.config.broker_socket,
                workload=sandbox.config.workload,
                adapter_user=sandbox.config.adapter_user,
                adapter_group=sandbox.config.adapter_group,
                attestation_key_path=sandbox.config.attestation_key_path,
                evidence_ref=sandbox.config.evidence_ref,
                runtime_name=sandbox.config.runtime_name,
                runtime_version=sandbox.config.runtime_version,
                adapter_uid=sandbox.uid,
                adapter_gid=sandbox.gid,
            ),
        )


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


def test_launch_verifies_active_unit_started_marker_and_uid(sandbox):
    runner = ScriptedRunner([SHOW_NOT_FOUND, SHOW_ACTIVE])
    response = _launch_ready(sandbox, runner)

    assert response["unit"] == UNIT
    assert response["active_state"] == "active"
    assert response["started"] is True
    assert response["uid"] == sandbox.uid
    assert response["main_pid"] == os.getpid()

    launches = runner.launch_calls()
    assert len(launches) == 1
    assert launches[0][0] == helper.SYSTEMD_RUN
    assert f"--unit={UNIT}" in launches[0]

    serialized = json.dumps(response)
    for canary in (PROMPT.decode("utf-8"), TOKEN, str(sandbox.task_dir)):
        assert canary not in serialized


def test_launch_waits_through_activating(sandbox):
    runner = ScriptedRunner([SHOW_NOT_FOUND, SHOW_ACTIVATING, SHOW_ACTIVE])
    _launch_ready(sandbox, runner)
    assert len(runner.launch_calls()) == 1
    assert [call[0] for call in runner.calls] == [
        helper.SYSTEMCTL,
        helper.SYSTEMD_RUN,
        helper.SYSTEMCTL,
        helper.SYSTEMCTL,
    ]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda sandbox: (sandbox.task_dir / "prompt.txt").write_bytes(b"other"), "prompt_size_mismatch"),
        (lambda sandbox: (sandbox.task_dir / "prompt.txt").write_bytes(b"x" * len(PROMPT)), "prompt_hash_mismatch"),
        (lambda sandbox: (sandbox.task_dir / "profile.template.yaml").write_bytes(b"other"), "profile_hash_mismatch"),
        (lambda sandbox: (sandbox.task_dir / "egress.token").chmod(0o644), "token_mode_invalid"),
        (lambda sandbox: (sandbox.task_dir / "egress.token").chmod(0o660), "token_mode_invalid"),
        (lambda sandbox: (sandbox.task_dir / "prompt.txt").chmod(0o604), "prompt_mode_invalid"),
        (lambda sandbox: (sandbox.task_dir / "prompt.txt").chmod(0o660), "prompt_mode_invalid"),
        (lambda sandbox: (sandbox.task_dir / "profile.template.yaml").chmod(0o666), "profile_mode_invalid"),
    ],
)
def test_launch_rejects_modified_task_files_before_starting(sandbox, mutate, code):
    _prepare(sandbox)
    _write_task_files(sandbox)
    mutate(sandbox)
    runner = ScriptedRunner([SHOW_NOT_FOUND])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == code
    assert runner.launch_calls() == []
    assert TOKEN not in str(excinfo.value)


def test_launch_rejects_symlinked_prompt_and_missing_files(sandbox):
    _prepare(sandbox)
    outside = sandbox.root / "outside.txt"
    outside.write_bytes(PROMPT)
    (sandbox.task_dir / "prompt.txt").symlink_to(outside)
    runner = ScriptedRunner([SHOW_NOT_FOUND])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "prompt_invalid"

    (sandbox.task_dir / "prompt.txt").unlink()
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "prompt_missing"


def test_launch_requires_task_dir_and_socket_and_workload(sandbox):
    runner = ScriptedRunner([SHOW_NOT_FOUND])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "task_dir_missing"

    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    sandbox.broker_socket.unlink()
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "broker_socket_invalid"
    assert runner.launch_calls() == []


def test_launch_does_not_create_a_second_unit(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    runner = ScriptedRunner([SHOW_ACTIVE])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "unit_already_exists"
    assert runner.launch_calls() == []


def test_launch_failure_returns_stable_error_and_stops_the_unit(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    runner = ScriptedRunner([SHOW_NOT_FOUND, SHOW_FAILED])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "unit_failed"
    assert "/" not in excinfo.value.code
    assert any("--signal=SIGTERM" in call for call in runner.calls)
    assert any("stop" in call for call in runner.calls)


def test_launch_command_failure_is_reported(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=FailingRunner([SHOW_NOT_FOUND]))
    assert excinfo.value.code == "command_failed"


def test_launch_times_out_without_started_marker(sandbox, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(helper, "_monotonic", clock.monotonic)
    monkeypatch.setattr(helper, "_sleep", clock.sleep)

    _prepare(sandbox)
    _write_task_files(sandbox)
    runner = ScriptedRunner([SHOW_NOT_FOUND, SHOW_ACTIVE])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "started_marker_timeout"
    assert any("--signal=SIGTERM" in call for call in runner.calls)


def test_launch_fails_closed_when_the_unit_uid_is_unreadable(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    unreachable = _show(
        active_state="active", sub_state="running", main_pid=4_000_000
    )
    runner = ScriptedRunner([SHOW_NOT_FOUND, unreachable])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "process_uid_unavailable"
    assert any("--signal=SIGTERM" in call for call in runner.calls)


def test_launch_rejects_runtime_root_and_workload_unreadable_by_the_unit(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    runner = ScriptedRunner([SHOW_NOT_FOUND])

    sandbox.runtime_root.chmod(0o700)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "runtime_root_mode_invalid"

    sandbox.runtime_root.chmod(0o755)
    sandbox.workload.chmod(0o600)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "workload_mode_invalid"
    assert runner.launch_calls() == []


def test_launch_rejects_an_unsafe_attestation_key(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    runner = ScriptedRunner([SHOW_NOT_FOUND, SHOW_ACTIVE])

    # Group can read (and it is the sandbox group): never acceptable.
    sandbox.key_path.chmod(0o640)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "attestation_key_group_readable"

    # World-readable is rejected even without a group-read bit.
    sandbox.key_path.chmod(0o604)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "attestation_key_mode_invalid"

    sandbox.key_path.write_bytes(b"short")
    sandbox.key_path.chmod(0o600)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "attestation_key_invalid"
    assert runner.launch_calls() == []


def test_install_config_rejects_an_attestation_key_outside_the_unit_invisible_root(
    sandbox,
):
    outside = sandbox.root / "outside.key"
    outside.write_bytes(KEY)
    outside.chmod(0o600)
    config = dataclasses.replace(sandbox.config, attestation_key_path=outside)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.prepare(valid_request(operation="prepare"), config)
    assert excinfo.value.code == "attestation_key_path_invalid"

    in_task_root = sandbox.task_root / "attestation.key"
    in_task_root.write_bytes(KEY)
    in_task_root.chmod(0o600)
    config = dataclasses.replace(sandbox.config, attestation_key_path=in_task_root)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.prepare(valid_request(operation="prepare"), config)
    assert excinfo.value.code == "attestation_key_path_invalid"


def test_operations_reject_a_cross_task_binding(sandbox):
    _prepare(sandbox)
    forged = dataclasses.replace(valid_request(), task_id="task_other")
    with pytest.raises(helper.HelperError) as excinfo:
        helper.status(forged, sandbox.config, runner=ScriptedRunner([SHOW_ACTIVE]))
    assert excinfo.value.code == "unit_name_mismatch"

    forged = dataclasses.replace(valid_request(), runtime_handle="rtask_hermes_" + "0" * 32)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.stop(forged, sandbox.config, runner=ScriptedRunner([SHOW_INACTIVE]))
    assert excinfo.value.code == "runtime_handle_mismatch"


def test_launch_rejects_started_marker_for_another_unit(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox, unit="agentmgr-hermes-0000000000000000")
    runner = ScriptedRunner([SHOW_NOT_FOUND, SHOW_ACTIVE])
    with pytest.raises(helper.HelperError) as excinfo:
        helper.launch(valid_request(), sandbox.config, runner=runner)
    assert excinfo.value.code == "started_marker_invalid"


# ---------------------------------------------------------------------------
# status / stop / cleanup / attest
# ---------------------------------------------------------------------------


def test_status_returns_whitelist_facts_and_no_host_paths(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    status_dir = sandbox.task_dir / "status"
    (status_dir / "heartbeat.json").write_text(
        json.dumps({"heartbeat_at": "2026-10-25T12:00:05Z"}), encoding="utf-8"
    )
    (status_dir / "exit.json").write_text(
        json.dumps({"exit_code": 0, "timed_out": False, "cancelled": False}),
        encoding="utf-8",
    )
    (sandbox.task_dir / "out" / "result.txt").write_text("OK\n", encoding="utf-8")

    runner = ScriptedRunner([SHOW_ACTIVE])
    response = helper.status(valid_request(operation="status"), sandbox.config, runner=runner)

    assert response["unit"] == UNIT
    assert response["uid"] == sandbox.uid
    assert response["unit_state"]["active_state"] == "active"
    assert response["unit_state"]["main_pid"] == os.getpid()
    assert response["task"] == {
        "started": True,
        "heartbeat_at": "2026-10-25T12:00:05Z",
        "exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "result_present": True,
    }
    serialized = json.dumps(response)
    assert str(sandbox.task_root) not in serialized
    assert str(sandbox.task_dir) not in serialized
    assert "ControlGroup" not in serialized


def test_status_of_never_started_unit_reports_gone_and_no_started(sandbox):
    _prepare(sandbox)
    runner = ScriptedRunner([SHOW_NOT_FOUND])
    response = helper.status(valid_request(operation="status"), sandbox.config, runner=runner)
    assert response["unit_state"]["process_gone"] is True
    assert response["unit_state"]["result"] is None
    assert response["unit_state"]["exit_status"] is None
    assert response["task"]["started"] is False
    assert response["uid"] is None


def test_status_rejects_a_symlinked_status_directory(sandbox):
    _prepare(sandbox)
    outside = sandbox.root / "outside-status"
    outside.mkdir()
    (outside / "started.json").write_text(
        json.dumps(
            {"unit": UNIT, "task_id": TASK_ID, "runtime_handle": HANDLE}
        ),
        encoding="utf-8",
    )
    (sandbox.task_dir / "status").symlink_to(outside, target_is_directory=True)

    with pytest.raises(helper.HelperError) as excinfo:
        helper.status(
            valid_request(operation="status"),
            sandbox.config,
            runner=ScriptedRunner([SHOW_ACTIVE]),
        )
    assert excinfo.value.code == "status_dir_invalid"


def test_status_rejects_a_symlinked_task_root(sandbox):
    outside = sandbox.root / "outside-root"
    outside.mkdir()
    (outside / UNIT).mkdir()
    (outside / UNIT / "status").mkdir()
    (outside / UNIT / "status" / "started.json").write_text(
        json.dumps({"unit": UNIT, "task_id": TASK_ID, "runtime_handle": HANDLE}),
        encoding="utf-8",
    )
    link_root = sandbox.root / "linked-tasks"
    link_root.symlink_to(outside, target_is_directory=True)
    config = dataclasses.replace(sandbox.config, task_root=link_root)

    with pytest.raises(helper.HelperError) as excinfo:
        helper.status(
            valid_request(operation="status"),
            config,
            runner=ScriptedRunner([SHOW_ACTIVE]),
        )
    assert excinfo.value.code == "task_root_invalid"


def test_stop_escalates_to_sigkill_and_confirms_only_when_gone(sandbox, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(helper, "_monotonic", clock.monotonic)
    monkeypatch.setattr(helper, "_sleep", clock.sleep)

    runner = KillAwareRunner()
    response = helper.stop(valid_request(operation="stop"), sandbox.config, runner=runner)

    assert runner.terminated is True
    assert runner.killed is True
    assert response["process_gone"] is True
    assert response["stop_confirmed"] is True
    assert response["unit_state"]["active_state"] == "inactive"


def test_stop_of_never_started_unit_is_confirmed_without_signals(sandbox):
    runner = ScriptedRunner([SHOW_NOT_FOUND])
    response = helper.stop(valid_request(operation="stop"), sandbox.config, runner=runner)
    assert response["stop_confirmed"] is True
    assert not any("--signal=SIGTERM" in call for call in runner.calls)
    assert not any("--signal=SIGKILL" in call for call in runner.calls)


def test_stop_is_not_confirmed_while_the_unit_stays_active(sandbox, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(helper, "_monotonic", clock.monotonic)
    monkeypatch.setattr(helper, "_sleep", clock.sleep)

    runner = ScriptedRunner([SHOW_ACTIVE])
    response = helper.stop(valid_request(operation="stop"), sandbox.config, runner=runner)

    assert any("--signal=SIGTERM" in call for call in runner.calls)
    assert any("--signal=SIGKILL" in call for call in runner.calls)
    assert response["process_gone"] is False
    assert response["stop_confirmed"] is False


def test_stop_is_not_confirmed_while_the_cgroup_holds_a_pid(sandbox, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(helper, "_monotonic", clock.monotonic)
    monkeypatch.setattr(helper, "_sleep", clock.sleep)

    procs = sandbox.root / "cgroup.procs"
    procs.write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(
        helper, "_cgroup_procs_path", lambda control_group: procs
    )

    show = _show(active_state="inactive", control_group="/system.slice/fake.service")
    runner = ScriptedRunner([show])
    response = helper.stop(
        valid_request(operation="stop"),
        sandbox.config,
        runner=runner,
    )
    assert any("--signal=SIGTERM" in call for call in runner.calls)
    assert any("--signal=SIGKILL" in call for call in runner.calls)
    assert response["process_gone"] is False
    assert response["stop_confirmed"] is False


def test_cleanup_requires_result_read_and_a_stopped_unit(sandbox):
    _prepare(sandbox)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.cleanup(
            valid_request(operation="cleanup", result_read=True),
            sandbox.config,
            result_read=False,
            runner=ScriptedRunner([SHOW_INACTIVE]),
        )
    assert excinfo.value.code == "result_not_read"

    with pytest.raises(helper.HelperError) as excinfo:
        helper.cleanup(
            valid_request(operation="cleanup", result_read=True),
            sandbox.config,
            result_read=True,
            runner=ScriptedRunner([SHOW_ACTIVE]),
        )
    assert excinfo.value.code == "unit_still_running"
    assert sandbox.task_dir.exists()

    response = helper.cleanup(
        valid_request(operation="cleanup", result_read=True),
        sandbox.config,
        result_read=True,
        runner=ScriptedRunner([SHOW_INACTIVE]),
    )
    assert response["removed"] is True
    assert response["process_gone"] is True
    assert not sandbox.task_dir.exists()


def test_cleanup_refuses_a_symlinked_task_dir(sandbox):
    outside = sandbox.root / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (sandbox.task_root / UNIT).symlink_to(outside, target_is_directory=True)
    with pytest.raises(helper.HelperError) as excinfo:
        helper.cleanup(
            valid_request(operation="cleanup", result_read=True),
            sandbox.config,
            result_read=True,
            runner=ScriptedRunner([SHOW_INACTIVE]),
        )
    assert excinfo.value.code == "task_dir_invalid"
    assert (outside / "keep.txt").exists()


def test_attest_without_a_task_dir_returns_runtime_only(sandbox):
    # Never prepared: attest must still issue a runtime attestation.
    response = helper.attest(
        valid_request(operation="attest"),
        sandbox.config,
        runner=ScriptedRunner([SHOW_INACTIVE]),
    )
    assert response["task_attestation"] is None
    assert response["runtime_attestation"]["schema"] == RUNTIME_ATTESTATION_SCHEMA
    assert response["runtime_attestation"]["runtime_root_hash"] == RUNTIME_ROOT_HASH
    serialized = json.dumps(response)
    assert str(sandbox.task_root) not in serialized
    assert str(sandbox.task_dir) not in serialized
    assert TOKEN not in serialized


def test_attest_after_cleanup_returns_runtime_only(sandbox):
    _prepare(sandbox)
    helper.cleanup(
        valid_request(operation="cleanup", result_read=True),
        sandbox.config,
        result_read=True,
        runner=ScriptedRunner([SHOW_INACTIVE]),
    )
    assert not sandbox.task_dir.exists()

    response = helper.attest(
        valid_request(operation="attest"),
        sandbox.config,
        runner=ScriptedRunner([SHOW_INACTIVE]),
    )
    assert response["task_attestation"] is None
    assert response["runtime_attestation"]["broker_socket_ok"] is True


def test_attest_keeps_strict_file_verification_while_active(sandbox):
    _prepare(sandbox)
    _write_task_files(sandbox)
    _write_started(sandbox)
    (sandbox.task_dir / "egress.token").unlink()

    with pytest.raises(helper.HelperError) as excinfo:
        helper.attest(
            valid_request(operation="attest"),
            sandbox.config,
            runner=ScriptedRunner([SHOW_ACTIVE]),
        )
    assert excinfo.value.code == "token_missing"


# ---------------------------------------------------------------------------
# stdin protocol
# ---------------------------------------------------------------------------


def test_run_request_dispatches_prepare(sandbox, monkeypatch):
    monkeypatch.setattr(helper, "DEFAULT_INSTALL_CONFIG", sandbox.root / "install.json")
    path = sandbox.root / "install.json"
    path.write_text(json.dumps(_install_config_document(sandbox)), encoding="utf-8")
    path.chmod(0o600)

    response = helper.run_request(base_request(operation="prepare"))
    assert response["ok"] is True
    assert response["operation"] == "prepare"
    assert (sandbox.task_dir / "out").is_dir()


def test_main_roundtrips_one_prepare_request(capsys, sandbox, monkeypatch):
    import io

    path = sandbox.root / "install.json"
    path.write_text(json.dumps(_install_config_document(sandbox)), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(helper, "DEFAULT_INSTALL_CONFIG", path)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(base_request(operation="prepare")))
    )

    assert helper.main([]) == 0
    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert response["ok"] is True
    assert response["operation"] == "prepare"
    assert response["unit"] == UNIT
    assert str(sandbox.task_root) not in captured.out
    assert captured.err == ""


def test_run_request_dispatches_cleanup(sandbox, monkeypatch):
    monkeypatch.setattr(helper, "DEFAULT_INSTALL_CONFIG", sandbox.root / "install.json")
    path = sandbox.root / "install.json"
    path.write_text(json.dumps(_install_config_document(sandbox)), encoding="utf-8")
    path.chmod(0o600)
    assert helper.run_request(base_request(operation="prepare"))["ok"] is True

    response = helper.run_request(
        base_request(operation="cleanup", result_read=True),
        runner=ScriptedRunner([SHOW_INACTIVE]),
    )
    assert response["removed"] is True
    assert not sandbox.task_dir.exists()

    with pytest.raises(helper.HelperError) as excinfo:
        helper.run_request(base_request(operation="cleanup", result_read=False))
    assert excinfo.value.code == "result_not_read"


def test_main_rejects_argv_and_blank_stdin(capsys, monkeypatch):
    assert helper.main(["--help"]) == 1
    captured = capsys.readouterr()
    assert "arguments_not_accepted" in captured.err
    assert captured.out == ""

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    assert helper.main([]) == 1
    captured = capsys.readouterr()
    assert "invalid_request" in captured.err


# ---------------------------------------------------------------------------
# deployment surface
# ---------------------------------------------------------------------------


def _read_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_wrapper_execs_the_fixed_helper_without_argv_or_shell_eval():
    text = _read_template(WRAPPER)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert lines[0] == "#!/bin/sh"
    assert lines[-1] == "exec /usr/bin/python3 /usr/local/libexec/agentmgr_hermes_exec.py"

    executable = [line for line in lines[1:] if not line.startswith("#")]
    assert executable == [lines[-1]]
    lowered = text.lower()
    assert "eval" not in lowered
    assert "sh -c" not in lowered
    assert "$@" not in text
    assert "$1" not in text


def test_sudoers_allows_only_the_fixed_no_argument_wrapper():
    text = _read_template(SUDOERS)
    rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert rules == [
        'agentmgr ALL=(root) NOPASSWD: /usr/local/libexec/agentmgr-hermes-exec ""'
    ]
    lowered = text.lower()
    assert "*" not in text
    assert "systemctl" not in lowered
    assert "systemd-run" not in lowered
    assert "<adapter-user>" in text
    assert "placeholder" in lowered


def test_helper_is_stdlib_only_and_never_imports_app():
    source = _read_template(OPS / "agentmgr_hermes_exec.py")
    lines = source.splitlines()
    assert not [
        line
        for line in lines
        if line.startswith("import app") or line.startswith("from app")
    ]
    for module in ("hashlib", "hmac", "json", "os", "re", "subprocess", "sys"):
        assert f"import {module}" in source
