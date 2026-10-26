"""Live sandbox probe and Hermes lifecycle matrix (Issue #97).

**Test-only, environment-gated, Master-run.**  Nothing in this module runs by
default: :data:`live_only` skips every live body unless *both*
``AGENTMGR_HERMES_LIVE=1`` and ``AGENTMGR_HERMES_SUDO=1`` are set, and each body
adds its own skip reason when a required path, group or helper is absent.  A
skip is never acceptance evidence; the implementer must never claim a live run.

Two independent live bodies:

``TestProbeUnit``
    Launches :mod:`hermes_probe_workload` (the fixed test-only probe next to this
    file) through the production helper's **pure** ``build_systemd_run_argv`` and
    asserts the negative matrix inside exactly the production sandbox property
    profile: host files, host home, database/container/systemd/D-Bus paths and a
    sibling task directory are denied, isolated loopback works, a direct external
    connection fails, and only the task directory is writable.

``TestManagedLifecycle``
    Drives ``HelperSandboxSupervisor`` / ``HermesManagedAdapter`` against the real
    ``agentmgr-hermes-exec`` helper with a **stub** egress broker whose loopback
    upstream returns fixed assistant text only (no tool calls).  It proves the
    unit/workload/Hermes process lifecycle, the task-file and token channel, the
    single-unit idempotency rule, stop/cgroup-gone and the honest
    "no artifact -> unknown/needs_human" mapping.  It proves neither business
    success nor model quality.

Environment (all paths must be absolute and on a persistent filesystem):

* ``AGENTMGR_HERMES_LIVE=1``, ``AGENTMGR_HERMES_SUDO=1`` -- the gates.
* ``AGENTMGR_HERMES_RUNTIME_ROOT`` -- read-only Hermes runtime root.
* ``AGENTMGR_HERMES_TASK_ROOT`` -- provisioned task root (group-shared, 0770).
* ``AGENTMGR_HERMES_EGRESS_SOCKET`` -- dedicated *test* broker socket under the
  control directory.  The lifecycle body owns it: it refuses to replace a live
  or pre-existing socket.
* ``AGENTMGR_HERMES_GROUP`` -- sandbox group, default ``agentmgr-hermes``.
* ``AGENTMGR_HERMES_PROBE_WORKLOAD`` -- optional installed copy of the probe
  workload; defaults to the file next to this module (a checkout under ``/tmp``
  cannot be a bind source because the unit has ``PrivateTmp=yes``).
* Lifecycle only: ``AGENTMGR_HERMES_HELPER``, ``AGENTMGR_HERMES_ATTESTATION_KEY``,
  ``AGENTMGR_HERMES_EGRESS_TOKEN_KEY``, ``AGENTMGR_HERMES_RUNTIME_ROOT_HASH``,
  ``AGENTMGR_HERMES_EVIDENCE_REF``, ``AGENTMGR_HERMES_MODEL``,
  ``AGENTMGR_HERMES_RUNTIME_VERSION`` and ``AGENTMGR_HERMES_PROVIDER_HOST``
  (loopback-agnostic token claim, default ``stub-provider.invalid``), plus
  ``AGENTMGR_HERMES_RUNTIME_NAME`` (default ``hermes``) and
  ``AGENTMGR_HERMES_RUNTIME_MAX_SEC`` (default 180).

Never touches the personal/default Hermes gateway, profile, auth, ``.env`` or
session; every unit and task directory it creates is removed in ``finally``.
"""

from __future__ import annotations

import contextlib
import grp
import hashlib
import http.server
import importlib.util
import json
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.runtime.contract import (
    ResultOutcome,
    RuntimeGrant,
    RuntimeState,
    SubmitOutcome,
)
from app.runtime.hermes_egress import BrokerConfig, EgressBroker
from app.runtime.hermes_managed import (
    HermesManagedAdapter,
    HermesManagedConfig,
    HelperSandboxSupervisor,
    unit_name_for,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_WORKLOAD = Path(__file__).with_name("hermes_probe_workload.py")
OPS_HELPER = REPO_ROOT / "ops" / "security" / "agentmgr_hermes_exec.py"

LIVE_ENV_VARS = ("AGENTMGR_HERMES_LIVE", "AGENTMGR_HERMES_SUDO")
LIVE_ENABLED = all(os.environ.get(name) == "1" for name in LIVE_ENV_VARS)
LIVE_SKIP_REASON = (
    "requires the owner-approved #97 live test host: set "
    "AGENTMGR_HERMES_LIVE=1 and AGENTMGR_HERMES_SUDO=1"
)

LIVE_TIMEOUT_SECONDS = 240
UNIT_START_TIMEOUT_SECONDS = 60
UNIT_STOP_TIMEOUT_SECONDS = 90
STATUS_TIMEOUT_SECONDS = 240

PROBE_TASK_ID = "task_hermes_live_probe"
PROBE_GRANT_ID = "grnt_hermes_live_probe"
PROBE_IDEMPOTENCY_KEY = "idem_hermes_live_probe"
PROBE_RUNTIME_MAX_SEC = 120
PROBE_LINGER_SECONDS = 90
PROBE_EXTERNAL = ("example.com", 443)
CANARY_DIRECTORY = "agentmgr-hermes-canary-live"
CANARY_PROMPT = "PROMPT-CANARY-hermes-live-probe"
CANARY_TOKEN = "egt.canary.live.probe.token"

MANAGED_PROMPT_CANARY = "PROMPT-CANARY-hermes-managed-live"
STUB_ASSISTANT_TEXT = "STUB-ASSISTANT-OK"
STUB_RELEASE_TIMEOUT_SECONDS = 120
STUB_PROVIDER_KEY = b"stub-provider-key-never-real"

#: Hermes argv facts asserted on the live process: the prompt is a file and the
#: toolset is exactly ``terminal``.
HERMES_ARGV_FACTS = ("--query-file", "/task/prompt.txt", "-t", "terminal")

SUDO = shutil.which("sudo") or "/usr/bin/sudo"
SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"
JOURNALCTL = shutil.which("journalctl") or "/usr/bin/journalctl"
INSTALL = shutil.which("install") or "/usr/bin/install"
RM = shutil.which("rm") or "/usr/bin/rm"
CAT = shutil.which("cat") or "/usr/bin/cat"

live_only = pytest.mark.skipif(not LIVE_ENABLED, reason=LIVE_SKIP_REASON)


# ---------------------------------------------------------------------------
# production helper module (pure argv builder + request contract)
# ---------------------------------------------------------------------------


def _load_helper() -> Any:
    """Load the root helper as a module; only its pure functions are used."""

    spec = importlib.util.spec_from_file_location(
        "agentmgr_hermes_exec_live", OPS_HELPER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclass(slots=True)`` resolves type names through ``sys.modules``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()
# A live run must never enable the unprivileged unit-test seam.
assert helper.ALLOW_PRIVATE_TMP_SOURCES is False


# ---------------------------------------------------------------------------
# environment contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveEnvironment:
    """Resolved live-test configuration; every value is optional until checked."""

    group: str
    runtime_root: Path | None
    task_root: Path | None
    egress_socket: Path | None
    helper: Path | None
    attestation_key: Path | None
    egress_token_key: Path | None
    runtime_root_hash: str | None
    evidence_ref: str | None
    runtime_name: str
    runtime_version: str | None
    model: str | None
    provider_host: str
    runtime_max_sec: int
    probe_workload: Path

    # -- readiness -----------------------------------------------------------

    def probe_skip_reason(self) -> str | None:
        """Return why the probe body cannot run, or ``None`` when it can."""

        return _skip_reason("the live sandbox probe", self._probe_problems())

    def lifecycle_skip_reason(self) -> str | None:
        """Return why the managed lifecycle body cannot run, or ``None``."""

        problems = self._probe_problems()
        problems.extend(
            [
                _executable_problem(self.helper, "AGENTMGR_HERMES_HELPER"),
                _readable_problem(
                    self.attestation_key, "AGENTMGR_HERMES_ATTESTATION_KEY"
                ),
                _readable_problem(
                    self.egress_token_key, "AGENTMGR_HERMES_EGRESS_TOKEN_KEY"
                ),
                _hash_problem(self.runtime_root_hash),
                _text_problem(self.evidence_ref, "AGENTMGR_HERMES_EVIDENCE_REF"),
                _text_problem(self.model, "AGENTMGR_HERMES_MODEL"),
                _text_problem(
                    self.runtime_version, "AGENTMGR_HERMES_RUNTIME_VERSION"
                ),
            ]
        )
        key = self.egress_token_key
        if key is not None and key.is_file() and key.stat().st_size < 32:
            problems.append(
                "AGENTMGR_HERMES_EGRESS_TOKEN_KEY must be at least 32 bytes "
                "(the broker's token-key contract)"
            )
        return _skip_reason("the managed Hermes lifecycle body", problems)

    def _probe_problems(self) -> list[str | None]:
        problems: list[str | None] = [
            _directory_problem(self.runtime_root, "AGENTMGR_HERMES_RUNTIME_ROOT"),
            _directory_problem(self.task_root, "AGENTMGR_HERMES_TASK_ROOT"),
            self._socket_problem(),
            self._group_problem(),
            _source_problem(self.probe_workload, "the probe workload"),
        ]
        if not self.probe_workload.is_file():
            problems.append(f"the probe workload {self.probe_workload} is missing")
        else:
            try:
                mode = self.probe_workload.stat().st_mode
            except OSError:
                mode = 0
            if not mode & 0o004:
                problems.append(
                    "the probe workload must be world-readable: the unit runs as "
                    "a dynamic user"
                )
        return problems

    # -- managed configuration ------------------------------------------------

    def managed_config(self, revocation_dir: Path) -> HermesManagedConfig:
        """Build the trusted adapter configuration from the live environment."""

        assert self.helper is not None
        assert self.runtime_root is not None
        assert self.runtime_root_hash is not None
        assert self.task_root is not None
        assert self.egress_socket is not None
        assert self.attestation_key is not None
        assert self.egress_token_key is not None
        assert self.evidence_ref is not None
        assert self.runtime_version is not None
        assert self.model is not None
        return HermesManagedConfig(
            helper_path=self.helper,
            runtime_root=self.runtime_root,
            runtime_root_hash=self.runtime_root_hash,
            task_root=self.task_root,
            broker_socket=self.egress_socket,
            attestation_key_path=self.attestation_key,
            egress_token_key_path=self.egress_token_key,
            runtime_attestation_path=(
                self.egress_socket.parent / "runtime-attestation.json"
            ),
            evidence_ref=self.evidence_ref,
            runtime_name=self.runtime_name,
            runtime_version=self.runtime_version,
            model_name=self.model,
            egress_provider_host=self.provider_host,
            worker_id="worker_hermes_live",
            runtime_max_sec=self.runtime_max_sec,
            revocation_dir=revocation_dir,
        )

    # -- internal checks -----------------------------------------------------

    def _socket_problem(self) -> str | None:
        path = self.egress_socket
        if path is None:
            return "AGENTMGR_HERMES_EGRESS_SOCKET is not set"
        if not path.is_absolute():
            return "AGENTMGR_HERMES_EGRESS_SOCKET must be an absolute path"
        if path.parent == Path("/"):
            return "AGENTMGR_HERMES_EGRESS_SOCKET needs a control directory"
        if not path.parent.is_dir():
            return "the parent of AGENTMGR_HERMES_EGRESS_SOCKET does not exist"
        return _source_problem(path.parent, "the egress socket control directory")

    def _group_problem(self) -> str | None:
        try:
            entry = grp.getgrnam(self.group)
        except KeyError:
            return f"the sandbox group {self.group} does not exist"
        if os.geteuid() != 0 and entry.gr_gid not in os.getgroups():
            return f"the test user is not a member of the sandbox group {self.group}"
        return None


def _text(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _path(name: str) -> Path | None:
    value = _text(name)
    return Path(value) if value is not None else None


def read_environment() -> LiveEnvironment:
    """Read the live-test environment; nothing is validated here."""

    return LiveEnvironment(
        group=_text("AGENTMGR_HERMES_GROUP") or "agentmgr-hermes",
        runtime_root=_path("AGENTMGR_HERMES_RUNTIME_ROOT"),
        task_root=_path("AGENTMGR_HERMES_TASK_ROOT"),
        egress_socket=_path("AGENTMGR_HERMES_EGRESS_SOCKET"),
        helper=_path("AGENTMGR_HERMES_HELPER"),
        attestation_key=_path("AGENTMGR_HERMES_ATTESTATION_KEY"),
        egress_token_key=_path("AGENTMGR_HERMES_EGRESS_TOKEN_KEY"),
        runtime_root_hash=_text("AGENTMGR_HERMES_RUNTIME_ROOT_HASH"),
        evidence_ref=_text("AGENTMGR_HERMES_EVIDENCE_REF"),
        runtime_name=_text("AGENTMGR_HERMES_RUNTIME_NAME") or "hermes",
        runtime_version=_text("AGENTMGR_HERMES_RUNTIME_VERSION"),
        model=_text("AGENTMGR_HERMES_MODEL"),
        provider_host=(
            _text("AGENTMGR_HERMES_PROVIDER_HOST") or "stub-provider.invalid"
        ),
        runtime_max_sec=_runtime_max_sec(),
        probe_workload=(
            _path("AGENTMGR_HERMES_PROBE_WORKLOAD") or PROBE_WORKLOAD
        ),
    )


def _runtime_max_sec() -> int:
    raw = _text("AGENTMGR_HERMES_RUNTIME_MAX_SEC")
    if raw is None:
        return 180
    try:
        value = int(raw)
    except ValueError:
        return 180
    return value if 9 <= value <= 86_400 else 180


def _skip_reason(body: str, problems: list[str | None]) -> str | None:
    present = [problem for problem in problems if problem]
    if not present:
        return None
    return f"{body} requires: " + "; ".join(present)


def _directory_problem(path: Path | None, name: str) -> str | None:
    if path is None:
        return f"{name} is not set"
    if not path.is_absolute():
        return f"{name} must be an absolute path"
    if not path.is_dir():
        return f"{name} is not an existing directory"
    return _source_problem(path, name)


def _executable_problem(path: Path | None, name: str) -> str | None:
    if path is None:
        return f"{name} is not set"
    if not path.is_absolute():
        return f"{name} must be an absolute path"
    if not os.access(path, os.X_OK):
        return f"{name} is not an executable file"
    return None


def _readable_problem(path: Path | None, name: str) -> str | None:
    if path is None:
        return f"{name} is not set"
    if not path.is_absolute():
        return f"{name} must be an absolute path"
    if not os.access(path, os.R_OK):
        return f"{name} is not readable by the test user"
    return None


def _hash_problem(value: str | None) -> str | None:
    if value is None:
        return "AGENTMGR_HERMES_RUNTIME_ROOT_HASH is not set"
    if not value.startswith("sha256:") or len(value) != 71:
        return "AGENTMGR_HERMES_RUNTIME_ROOT_HASH must be 'sha256:' + 64 lowercase hex"
    if any(character not in "0123456789abcdef" for character in value[7:]):
        return "AGENTMGR_HERMES_RUNTIME_ROOT_HASH must be 'sha256:' + 64 lowercase hex"
    return None


def _text_problem(value: str | None, name: str) -> str | None:
    return None if value else f"{name} is not set"


def _source_problem(path: Path, label: str) -> str | None:
    """Reject a path the sandbox unit cannot see as a bind source.

    The unit has ``PrivateTmp=yes`` and masks the host control prefixes, so a
    bind source under those prefixes cannot be resolved inside its namespace.
    """

    prefixes = (*helper.MASKED_SOURCE_PREFIXES, *helper.PRIVATE_TMP_PREFIXES)
    for prefix in prefixes:
        if path == prefix or prefix in path.parents:
            return (
                f"{label} ({path}) must not be under {prefix}: the sandbox unit "
                "has PrivateTmp=yes and masks that prefix"
            )
    return None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def require_sudo() -> bool:
    if not LIVE_ENABLED:
        pytest.skip(LIVE_SKIP_REASON)
    completed = _run([SUDO, "-n", "true"], timeout=30)
    if completed.returncode != 0:
        pytest.skip(
            "the live tests require passwordless sudo ('sudo -n true' failed)"
        )
    return True


@pytest.fixture(scope="module")
def live_environment(require_sudo: bool) -> LiveEnvironment:
    del require_sudo
    environment = read_environment()
    reason = environment.probe_skip_reason()
    if reason:
        pytest.skip(reason)
    return environment


# ---------------------------------------------------------------------------
# command helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    timeout: float = LIVE_TIMEOUT_SECONDS,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command without a shell; no prompt or token is ever in argv."""

    try:
        return subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            input=stdin_text,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"command timed out after {timeout}s: {argv[0]}")


def _sudo(
    argv: list[str],
    *,
    timeout: float = LIVE_TIMEOUT_SECONDS,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run([SUDO, "-n", *argv], timeout=timeout, stdin_text=stdin_text)


def _current_user() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def _property_values(dump: str, name: str) -> list[str]:
    prefix = f"{name}="
    return [
        line[len(prefix) :].strip()
        for line in dump.splitlines()
        if line.startswith(prefix)
    ]


def _property(dump: str, name: str) -> str:
    values = _property_values(dump, name)
    return values[-1] if values else ""


def _joined(dump: str, name: str) -> str:
    return " ".join(_property_values(dump, name))


def _try_show(unit: str) -> str | None:
    completed = _sudo([SYSTEMCTL, "show", "--no-pager", unit])
    if completed.returncode != 0:
        return None
    return completed.stdout


def _show(unit: str) -> str:
    dump = _try_show(unit)
    if dump is None:
        pytest.fail(f"systemctl show {unit} failed")
    return dump


def _unit_is_gone(dump: str) -> bool:
    if _property(dump, "LoadState") == "not-found":
        return True
    return (
        _property(dump, "ActiveState") == "inactive"
        and int(_property(dump, "MainPID") or 0) == 0
    )


def _wait_for_unit_gone(unit: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dump = _try_show(unit)
        if dump is not None and _unit_is_gone(dump):
            return
        time.sleep(0.5)
    pytest.fail(f"unit {unit} still holds a process after {timeout}s")


def _wait_for_active_unit(unit: str, *, timeout: float) -> str:
    """Return the ``systemctl show`` dump of an active unit with a live process."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dump = _try_show(unit)
        if dump is not None:
            active_state = _property(dump, "ActiveState")
            main_pid = int(_property(dump, "MainPID") or 0)
            if active_state == "active" and main_pid > 0:
                return dump
            if active_state in {"failed", "inactive"} and main_pid == 0:
                pytest.fail(
                    "the managed unit did not stay active: "
                    f"ActiveState={active_state} "
                    f"Result={_property(dump, 'Result')} "
                    f"ExecMainStatus={_property(dump, 'ExecMainStatus')}"
                )
        time.sleep(0.5)
    pytest.fail(f"unit {unit} was not active with a live process within {timeout}s")


def _unit_exists(unit: str) -> bool:
    dump = _try_show(unit)
    return dump is not None and _property(dump, "LoadState") != "not-found"


def _stop_unit(unit: str) -> None:
    _sudo([SYSTEMCTL, "stop", unit], timeout=UNIT_STOP_TIMEOUT_SECONDS)
    _sudo([SYSTEMCTL, "reset-failed", unit], timeout=30)


def _read_remote_text(path: Path) -> str | None:
    completed = _sudo([CAT, str(path)])
    if completed.returncode != 0:
        return None
    return completed.stdout


def _read_remote_json(path: Path) -> dict[str, object] | None:
    text = _read_remote_text(path)
    if text is None:
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _write_remote_json(
    tmp_path: Path, directory: Path, name: str, document: object
) -> None:
    source = tmp_path / name
    source.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    completed = _sudo([INSTALL, "-m", "0644", str(source), str(directory / name)])
    assert completed.returncode == 0, completed.stderr


def _write_remote_text(
    tmp_path: Path, directory: Path, name: str, text: str, mode: str
) -> None:
    source = tmp_path / f"upload-{name}"
    source.write_text(text, encoding="utf-8")
    completed = _sudo(
        [INSTALL, "-m", mode, str(source), str(directory / name)]
    )
    assert completed.returncode == 0, completed.stderr


def _make_directory(path: Path, *, owner: str, group: str, mode: str) -> None:
    completed = _sudo(
        [INSTALL, "-d", "-o", owner, "-g", group, "-m", mode, str(path)]
    )
    assert completed.returncode == 0, completed.stderr


def _remove_tree(path: Path, parent: Path) -> None:
    """Remove one derived task directory; never a caller-supplied path."""

    if path.parent != parent or not path.name.startswith("agentmgr-hermes-"):
        pytest.fail(f"refusing to remove a path that is not derived: {path.name}")
    _sudo([RM, "-rf", "--", str(path)])


@contextlib.contextmanager
def _egress_bind_source(path: Path) -> Iterator[None]:
    """Guarantee the socket bind source exists for a probe unit, then clean up.

    A live broker often already owns the path: keep it untouched.  Otherwise the
    test creates one placeholder listener because ``systemd-run`` fails closed on
    a missing ``BindReadOnlyPaths`` source.
    """

    if path.exists():
        yield
        return
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
        yield
    finally:
        listener.close()
        with contextlib.suppress(OSError):
            path.unlink()


# ---------------------------------------------------------------------------
# probe body
# ---------------------------------------------------------------------------


def _probe_request(task_dir: Path) -> Any:
    unit = task_dir.name
    prompt = CANARY_PROMPT.encode("utf-8")
    return helper.SandboxRequest(
        operation="launch",
        task_id=PROBE_TASK_ID,
        grant_id=PROBE_GRANT_ID,
        unit=unit,
        runtime_handle=helper.runtime_handle_for(unit),
        idempotency_key=PROBE_IDEMPOTENCY_KEY,
        prompt_sha256=hashlib.sha256(prompt).hexdigest(),
        prompt_bytes=len(prompt),
        profile_template_sha256=hashlib.sha256(b"probe-profile").hexdigest(),
        egress_token_sha256=hashlib.sha256(CANARY_TOKEN.encode()).hexdigest(),
        ttl_seconds=300,
        runtime_max_sec=PROBE_RUNTIME_MAX_SEC,
        memory_max_mb=256,
        cpu_quota_percent=50,
        tasks_max=64,
    )


def _personal_paths() -> dict[str, Path]:
    """Existing personal Hermes locations the unit must not see (labels only)."""

    home = Path.home()
    candidates = {
        "personal_hermes_home": home / ".hermes",
        "personal_hermes_config": home / ".config" / "hermes",
    }
    return {label: path for label, path in candidates.items() if path.exists()}


def _probe_denied_paths(
    environment: LiveEnvironment, canary_dir: Path
) -> dict[str, str]:
    assert environment.task_root is not None
    paths = {
        "sibling_prompt": str(canary_dir / "prompt.txt"),
        "sibling_token": str(canary_dir / "egress.token"),
        "host_home_dir": str(Path.home()),
    }
    for label, path in _personal_paths().items():
        paths[label] = str(path)
    if environment.attestation_key is not None:
        paths["sandbox_attestation_key"] = str(environment.attestation_key)
    return paths


def _prepare_probe_tree(
    environment: LiveEnvironment,
    task_dir: Path,
    canary_dir: Path,
    tmp_path: Path,
) -> None:
    owner = _current_user()
    assert environment.task_root is not None
    _make_directory(task_dir, owner=owner, group=environment.group, mode="0770")
    _make_directory(
        task_dir / "out", owner=owner, group=environment.group, mode="0770"
    )
    _make_directory(canary_dir, owner=owner, group=environment.group, mode="0770")
    _write_remote_text(tmp_path, canary_dir, "prompt.txt", CANARY_PROMPT, "0640")
    _write_remote_text(tmp_path, canary_dir, "egress.token", CANARY_TOKEN, "0640")
    document = {
        "denied_paths": _probe_denied_paths(environment, canary_dir),
        "external": {"host": PROBE_EXTERNAL[0], "port": PROBE_EXTERNAL[1]},
        "out": "probe.json",
        "release": "probe.release",
        "linger_seconds": PROBE_LINGER_SECONDS,
    }
    _write_remote_json(tmp_path, task_dir, "probe-config.json", document)


def _wait_for_probe_report(task_dir: Path, unit: str) -> dict[str, object]:
    deadline = time.monotonic() + UNIT_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        document = _read_remote_json(task_dir / "probe.json")
        if document is not None:
            return document
        dump = _try_show(unit)
        if dump is not None and _unit_is_gone(dump):
            pytest.fail(
                "the probe unit exited without writing probe.json "
                f"(ExecMainStatus={_property(dump, 'ExecMainStatus')})"
            )
        time.sleep(0.5)
    pytest.fail("the probe unit did not write probe.json in time")


def _release_probe(task_dir: Path) -> None:
    completed = _sudo(
        [INSTALL, "-m", "0660", "/dev/null", str(task_dir / "probe.release")]
    )
    assert completed.returncode == 0, completed.stderr


def _assert_probe_argv(
    argv: list[str], environment: LiveEnvironment, task_dir: Path
) -> None:
    assert environment.task_root is not None
    assert environment.runtime_root is not None
    assert environment.egress_socket is not None
    assert argv[0] == helper.SYSTEMD_RUN
    assert f"--property=Group={environment.group}" in argv
    assert "--property=PrivateNetwork=yes" in argv
    assert "--property=PrivateDevices=yes" in argv
    assert f"--property=InaccessiblePaths=-{environment.task_root}" in argv
    assert f"--property=BindPaths={task_dir}:/task" in argv
    workload_bind = (
        f"--property=BindReadOnlyPaths={environment.probe_workload}:/workload.py"
    )
    assert workload_bind in argv
    assert not any("SupplementaryGroups" in item for item in argv)
    assert not any(CANARY_PROMPT in item or CANARY_TOKEN in item for item in argv)


def _assert_sandbox_properties(
    dump: str, environment: LiveEnvironment, task_dir: Path
) -> None:
    assert environment.runtime_root is not None
    assert environment.task_root is not None
    assert environment.egress_socket is not None

    assert _property(dump, "ActiveState") == "active"
    assert _property(dump, "DynamicUser") == "yes"
    assert _property(dump, "Group") == environment.group
    assert _property_values(dump, "SupplementaryGroups") in ([], [""])
    assert _property(dump, "PrivateDevices") == "yes"
    assert _property(dump, "PrivateNetwork") == "yes"
    assert _property(dump, "ProtectHome") == "yes"
    assert _property(dump, "ProtectSystem") == "strict"
    assert _property(dump, "KillMode") == "control-group"
    assert _property(dump, "ExitType") == "cgroup"
    assert _property(dump, "WorkingDirectory") == "/task"

    inaccessible = _joined(dump, "InaccessiblePaths")
    assert f"-{environment.task_root}" in inaccessible
    if environment.egress_socket.parent != environment.task_root:
        assert f"-{environment.egress_socket.parent}" in inaccessible
    assert "-/etc/agentmgr" in inaccessible
    assert "-/var/lib/postgresql" in inaccessible

    bind = _joined(dump, "BindPaths")
    assert f"{task_dir}:/task" in bind
    assert f"{environment.task_root}:" not in bind
    readonly = _joined(dump, "BindReadOnlyPaths")
    assert f"{environment.runtime_root}:/runtime" in readonly
    assert f"{environment.egress_socket}:/task/egress.sock" in readonly
    assert f"{environment.probe_workload}:/workload.py" in readonly

    assert "/workload.py" in _joined(dump, "ExecStart")
    for path in _personal_paths().values():
        assert str(path) not in dump


def _assert_probe_report(
    report: dict[str, object], environment: LiveEnvironment
) -> None:
    assert environment.task_root is not None
    probes = report.get("probes")
    assert isinstance(probes, dict) and probes
    assert report.get("ok") is True, report.get("failed")
    assert report.get("failed") == []

    denied_labels = (
        "etc_shadow",
        "host_home",
        "host_home_dir",
        "postgresql_data",
        "host_systemd_run",
        "host_dbus",
        "docker_socket",
        "containerd_socket",
        "sibling_prompt",
        "sibling_token",
    )
    for label in denied_labels:
        item = probes.get(label)
        assert isinstance(item, dict), f"missing probe {label}"
        assert item.get("observed") == "denied", item
        assert item.get("passed") is True, item

    for label in ("loopback", "task_dir_write"):
        item = probes.get(label)
        assert isinstance(item, dict), f"missing probe {label}"
        assert item.get("observed") == "ok", item

    external = probes.get("external_https")
    assert isinstance(external, dict)
    assert external.get("observed") == "denied", external

    raw = json.dumps(report)
    forbidden = (
        CANARY_PROMPT,
        CANARY_TOKEN,
        str(environment.task_root),
        str(Path.home()),
    )
    for value in forbidden:
        assert value not in raw


@live_only
class TestProbeUnit:
    def test_probe_unit_negative_matrix(
        self, live_environment: LiveEnvironment, tmp_path: Path
    ) -> None:
        environment = live_environment
        assert environment.task_root is not None
        assert environment.runtime_root is not None
        assert environment.egress_socket is not None

        unit = helper.unit_for_task(PROBE_TASK_ID, PROBE_IDEMPOTENCY_KEY)
        task_dir = environment.task_root / unit
        canary_dir = environment.task_root / CANARY_DIRECTORY
        if _unit_exists(unit):
            pytest.fail(f"a stale unit {unit} already exists; clean it up first")

        try:
            _prepare_probe_tree(environment, task_dir, canary_dir, tmp_path)
            with _egress_bind_source(environment.egress_socket):
                request = _probe_request(task_dir)
                argv = helper.build_systemd_run_argv(
                    request,
                    task_dir=task_dir,
                    runtime_root=environment.runtime_root,
                    broker_socket=environment.egress_socket,
                    workload=environment.probe_workload,
                )
                _assert_probe_argv(argv, environment, task_dir)

                launched = _sudo(argv)
                assert launched.returncode == 0, launched.stderr.strip()

                report = _wait_for_probe_report(task_dir, unit)
                dump = _show(unit)
                _assert_sandbox_properties(dump, environment, task_dir)
                _assert_probe_report(report, environment)

                # The probe lingers after writing its report, so the property
                # dump above is taken from a live unit; release it now.
                _release_probe(task_dir)
                _wait_for_unit_gone(unit, timeout=UNIT_STOP_TIMEOUT_SECONDS)
        finally:
            _stop_unit(unit)
            _remove_tree(task_dir, environment.task_root)
            _remove_tree(canary_dir, environment.task_root)


# ---------------------------------------------------------------------------
# stub upstream (fixed assistant text, no tool calls)
# ---------------------------------------------------------------------------


class _StubUpstream:
    """Loopback OpenAI-compatible upstream that never calls a tool.

    It holds the first chat completion open until the test releases it, which
    keeps the sandbox unit active while ``systemctl show``/``/proc`` are read.
    """

    def __init__(self, model: str | None) -> None:
        self.model = model or "stub-model"
        self.release = threading.Event()
        self.requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler()
        )
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="stub-upstream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        state = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - handler contract
                self._handle()

            def do_GET(self) -> None:  # noqa: N802 - handler contract
                self._handle()

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with state._lock:
                    state.requests.append(
                        {"method": self.command, "path": self.path, "body": body}
                    )
                if self.command == "POST" and self.path.startswith(
                    "/v1/chat/completions"
                ):
                    state.release.wait(timeout=STUB_RELEASE_TIMEOUT_SECONDS)
                    if _request_streams(body):
                        self._send_stream()
                    else:
                        self._send_json(
                            200,
                            json.dumps(state._completion(), separators=(",", ":")),
                        )
                    return
                if self.command == "GET" and self.path.startswith("/v1/models"):
                    self._send_json(
                        200,
                        json.dumps(
                            {
                                "object": "list",
                                "data": [{"id": state.model, "object": "model"}],
                            },
                            separators=(",", ":"),
                        ),
                    )
                    return
                self._send_json(404, '{"error":"unsupported"}')

            def _send_json(self, status: int, payload: str) -> None:
                data = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_stream(self) -> None:
                # A minimal SSE chat-completion stream: fixed text, no tool call.
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                chunks = (
                    {
                        "id": "chatcmpl-stub-live",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": state.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": STUB_ASSISTANT_TEXT,
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-stub-live",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": state.model,
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}
                        ],
                    },
                )
                for chunk in chunks:
                    self.wfile.write(
                        f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler

    def _completion(self) -> dict[str, object]:
        return {
            "id": "chatcmpl-stub-live",
            "object": "chat.completion",
            "created": 0,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": STUB_ASSISTANT_TEXT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }


def _request_streams(body: bytes) -> bool:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and document.get("stream") is True


# ---------------------------------------------------------------------------
# managed lifecycle body
# ---------------------------------------------------------------------------


def _managed_grant(
    task_id: str, grant_id: str, **overrides: object
) -> RuntimeGrant:
    values: dict[str, object] = {
        "grant_id": grant_id,
        "task_id": task_id,
        "task_revision": 1,
        "execution_mode": "managed_execution",
        "capabilities": ("task.run@1.0.0",),
        "resource_scope": {"scope_type": "hermes_live_probe"},
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    values.update(overrides)
    return RuntimeGrant(**values)  # type: ignore[arg-type]


def _start_broker(
    environment: LiveEnvironment, stub: _StubUpstream, revocation_dir: Path
) -> EgressBroker:
    assert environment.egress_socket is not None
    assert environment.egress_token_key is not None
    assert environment.task_root is not None
    token_key = environment.egress_token_key.read_bytes()
    config = BrokerConfig(
        socket_path=environment.egress_socket,
        upstream_base_url=f"http://127.0.0.1:{stub.port}",
        token_key=token_key,
        provider_key=STUB_PROVIDER_KEY,
        revocation_dir=revocation_dir,
        task_root=environment.task_root,
        provider_host=environment.provider_host,
        allow_insecure_upstream=True,
        socket_group=environment.group,
    )
    broker = EgressBroker(config)
    broker.start()
    return broker


def _wait_for_terminal_status(
    adapter: HermesManagedAdapter, handle: str, task_id: str
) -> object:
    deadline = time.monotonic() + STATUS_TIMEOUT_SECONDS
    terminal = {
        RuntimeState.COMPLETED,
        RuntimeState.FAILED,
        RuntimeState.STOPPED,
        RuntimeState.TIMED_OUT,
        RuntimeState.UNKNOWN,
    }
    while time.monotonic() < deadline:
        status = adapter.get_status(handle, task_id=task_id)
        if status.state in terminal:
            return status
        time.sleep(1.0)
    pytest.fail("the managed task did not reach a terminal state in time")


def _assert_managed_properties(
    dump: str, environment: LiveEnvironment, task_dir: Path
) -> None:
    assert environment.runtime_root is not None
    assert environment.task_root is not None
    assert environment.egress_socket is not None

    assert _property(dump, "ActiveState") == "active"
    assert _property(dump, "DynamicUser") == "yes"
    assert _property(dump, "Group") == environment.group
    assert _property_values(dump, "SupplementaryGroups") in ([], [""])
    assert _property(dump, "PrivateDevices") == "yes"
    assert _property(dump, "PrivateNetwork") == "yes"
    assert _property(dump, "ProtectHome") == "yes"
    assert _property(dump, "ProtectSystem") == "strict"
    assert _property(dump, "WorkingDirectory") == "/task"

    inaccessible = _joined(dump, "InaccessiblePaths")
    assert f"-{environment.task_root}" in inaccessible
    if environment.egress_socket.parent != environment.task_root:
        assert f"-{environment.egress_socket.parent}" in inaccessible

    bind = _joined(dump, "BindPaths")
    assert f"{task_dir}:/task" in bind
    assert f"{environment.task_root}:" not in bind
    readonly = _joined(dump, "BindReadOnlyPaths")
    assert f"{environment.runtime_root}:/runtime" in readonly
    assert f"{environment.egress_socket}:/task/egress.sock" in readonly
    assert ":/workload.py" in readonly

    assert MANAGED_PROMPT_CANARY not in dump
    for path in _personal_paths().values():
        assert str(path) not in dump


def _assert_task_files(task_dir: Path) -> str:
    """Check the task-file channel and return the task egress token."""

    mode = _sudo(["/usr/bin/stat", "-c", "%a", str(task_dir / "egress.token")])
    assert mode.returncode == 0, mode.stderr
    assert mode.stdout.strip() == "640"

    token = _read_remote_text(task_dir / "egress.token")
    assert token is not None
    token = token.strip()
    assert token.startswith("egt.")

    prompt = _read_remote_text(task_dir / "prompt.txt") or ""
    assert MANAGED_PROMPT_CANARY in prompt

    for name in (
        "manifest.json",
        "profile/config.yaml",
        "status/started.json",
        "status/exit.json",
        "stdout.txt",
        "stderr.txt",
    ):
        text = _read_remote_text(task_dir / name) or ""
        assert token not in text, f"the egress token leaked into {name}"
        assert MANAGED_PROMPT_CANARY not in text, f"the prompt leaked into {name}"
    return token


def _assert_child_process(task_dir: Path, token: str) -> None:
    """The prompt is a file and the token is only an environment value."""

    started = _read_remote_json(task_dir / "status" / "started.json")
    assert started is not None, "the started marker is missing"
    child_pid = started.get("child_pid")
    assert type(child_pid) is int and child_pid > 0

    cmdline = _sudo([CAT, f"/proc/{child_pid}/cmdline"])
    assert cmdline.returncode == 0, "the Hermes child process is not alive"
    for fact in HERMES_ARGV_FACTS:
        assert fact in cmdline.stdout
    assert MANAGED_PROMPT_CANARY not in cmdline.stdout
    assert token not in cmdline.stdout

    environ = _sudo([CAT, f"/proc/{child_pid}/environ"])
    assert environ.returncode == 0, "the Hermes child environment is unreadable"
    assert token in environ.stdout, (
        "the task token must reach the child only through its environment"
    )


def _wait_for_stub_request(stub: _StubUpstream, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stub.requests:
            return
        time.sleep(0.2)
    pytest.fail("Hermes never sent a model request through the egress broker")


def _assert_journal_is_clean(unit: str, token: str) -> None:
    completed = _sudo([JOURNALCTL, "--no-pager", "-o", "cat", "-u", unit])
    assert completed.returncode == 0, completed.stderr
    assert MANAGED_PROMPT_CANARY not in completed.stdout
    assert token not in completed.stdout


def _started_marker(task_dir: Path) -> dict[str, object]:
    marker = _read_remote_json(task_dir / "status" / "started.json")
    assert marker is not None, "the started marker is missing"
    return marker


@live_only
class TestManagedLifecycle:
    def test_managed_task_lifecycle_with_stub_broker(
        self, live_environment: LiveEnvironment, tmp_path: Path
    ) -> None:
        environment = live_environment
        reason = environment.lifecycle_skip_reason()
        if reason:
            pytest.skip(reason)

        assert environment.task_root is not None
        assert environment.egress_socket is not None
        assert environment.helper is not None

        if environment.egress_socket.exists():
            pytest.fail(
                "AGENTMGR_HERMES_EGRESS_SOCKET already exists: this body must own "
                "a dedicated test socket and never replaces a live broker socket"
            )

        suffix = secrets.token_hex(4)
        task_id = f"task_hermes_live_{suffix}"
        grant_id = f"grnt_hermes_live_{suffix}"
        idempotency_key = f"idem_hermes_live_{suffix}"
        unit = unit_name_for(task_id, idempotency_key)
        task_dir = environment.task_root / unit
        revocation_dir = (
            environment.egress_socket.parent / f"{unit}-revocations"
        )

        stub = _StubUpstream(environment.model)
        broker: EgressBroker | None = None
        supervisor: HelperSandboxSupervisor | None = None
        try:
            _make_directory(
                revocation_dir,
                owner=_current_user(),
                group=environment.group,
                mode="0770",
            )
            stub.start()
            broker = _start_broker(environment, stub, revocation_dir)
            config = environment.managed_config(revocation_dir)
            supervisor = HelperSandboxSupervisor(config)
            adapter = HermesManagedAdapter(config, supervisor)

            submitted = adapter.submit_task(
                task_id,
                MANAGED_PROMPT_CANARY,
                idempotency_key,
                _managed_grant(task_id, grant_id),
            )
            assert submitted.outcome is SubmitOutcome.ACCEPTED, submitted.error
            handle = submitted.runtime_task_id
            assert handle is not None and handle.startswith("rtask_hermes_")

            dump = _wait_for_active_unit(unit, timeout=UNIT_START_TIMEOUT_SECONDS)
            _wait_for_stub_request(stub, timeout=STUB_RELEASE_TIMEOUT_SECONDS)
            dump = _show(unit)
            _assert_managed_properties(dump, environment, task_dir)

            marker = _started_marker(task_dir)
            assert marker.get("unit") == unit
            assert marker.get("task_id") == task_id
            assert marker.get("runtime_handle") == handle
            started_at = marker.get("started_at")
            main_start = _property(dump, "ExecMainStartTimestamp")

            token = _assert_task_files(task_dir)
            assert token not in _show(unit)
            _assert_child_process(task_dir, token)

            # A duplicate submission reuses the same unit and never relaunches.
            replay = adapter.submit_task(
                task_id,
                MANAGED_PROMPT_CANARY,
                idempotency_key,
                _managed_grant(task_id, grant_id),
            )
            assert replay.outcome is SubmitOutcome.ACCEPTED, replay.error
            assert replay.replayed is True
            assert replay.runtime_task_id == handle
            conflict = adapter.submit_task(
                task_id,
                MANAGED_PROMPT_CANARY + " (different payload)",
                idempotency_key,
                _managed_grant(task_id, grant_id),
            )
            assert conflict.outcome is SubmitOutcome.CONFLICT, conflict.error
            malformed = adapter.submit_task(
                task_id,
                MANAGED_PROMPT_CANARY,
                idempotency_key,
                _managed_grant(task_id, grant_id, execution_mode="advisory"),
            )
            assert malformed.outcome is SubmitOutcome.REJECTED, malformed.error
            # The one unit never restarted: same start timestamp, same marker.
            after = _show(unit)
            assert _property(after, "ExecMainStartTimestamp") == main_start
            assert _started_marker(task_dir).get("started_at") == started_at

            # The stub upstream is the only model path; release its answer.
            stub.release.set()

            status = _wait_for_terminal_status(adapter, handle, task_id)
            assert status.state is RuntimeState.COMPLETED, status
            assert _read_remote_json(task_dir / "status" / "exit.json") is not None

            # No artifact: the adapter must not synthesize success.
            result = adapter.get_result(handle, task_id=task_id)
            assert result.outcome in {
                ResultOutcome.UNKNOWN,
                ResultOutcome.NEEDS_HUMAN,
            }, result
            assert result.result_ref is None
            assert not (task_dir / "out" / "result.txt").exists()

            _assert_journal_is_clean(unit, token)

            stopped = supervisor.stop(unit, reason="live_test")
            assert stopped.stop_confirmed is True
            assert stopped.process_gone is True
            _wait_for_unit_gone(unit, timeout=UNIT_STOP_TIMEOUT_SECONDS)

            supervisor.cleanup(unit, result_read=True)
            assert not task_dir.exists()
        finally:
            stub.stop()
            if broker is not None:
                broker.stop()
            _stop_unit(unit)
            _remove_tree(task_dir, environment.task_root)
            _remove_tree(revocation_dir, environment.egress_socket.parent)


# ---------------------------------------------------------------------------
# DB-free tests: they keep this module useful when every live body skips
# ---------------------------------------------------------------------------


def _probe_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "hermes_probe_workload", PROBE_WORKLOAD
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_workload_reports_only_generic_labels(tmp_path: Path) -> None:
    probe = _probe_module()
    visible = tmp_path / "visible.txt"
    visible.write_text("canary", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "file").write_text("x", encoding="utf-8")

    for path, expected in (
        (str(tmp_path / "missing"), "denied"),
        (str(visible), "reached"),
        (str(empty), "denied"),
        (str(populated), "reached"),
    ):
        observed, _detail = probe.probe_denied_path(path)
        assert observed == expected, path

    observed, _detail = probe.probe_loopback()
    assert observed == "ok"
    observed, _detail = probe.probe_task_dir_write(tmp_path)
    assert observed == "ok"

    report = probe.collect_report(
        task_dir=tmp_path,
        denied_paths={"missing": str(tmp_path / "missing")},
        external=None,
    )
    assert report["ok"] is True
    assert report["failed"] == []
    assert report["probes"]["missing"]["observed"] == "denied"
    raw = json.dumps(report)
    assert str(tmp_path) not in raw
    assert "visible.txt" not in raw


def test_probe_workload_imports_only_the_standard_library() -> None:
    source = PROBE_WORKLOAD.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import app."), stripped
        assert not stripped.startswith("from app."), stripped


def test_probe_argv_reuses_the_production_sandbox_profile() -> None:
    unit = helper.unit_for_task(PROBE_TASK_ID, PROBE_IDEMPOTENCY_KEY)
    task_dir = Path("/var/lib/agentmgr-hermes/tasks") / unit
    runtime_root = Path("/var/lib/agentmgr-hermes/runtime")
    broker_socket = Path("/var/lib/agentmgr-hermes/control/egress.sock")
    workload = Path("/usr/local/libexec/hermes_probe_workload.py")
    prompt = CANARY_PROMPT.encode("utf-8")

    request = helper.SandboxRequest(
        operation="launch",
        task_id=PROBE_TASK_ID,
        grant_id=PROBE_GRANT_ID,
        unit=unit,
        runtime_handle=helper.runtime_handle_for(unit),
        idempotency_key=PROBE_IDEMPOTENCY_KEY,
        prompt_sha256=hashlib.sha256(prompt).hexdigest(),
        prompt_bytes=len(prompt),
        profile_template_sha256=hashlib.sha256(b"probe-profile").hexdigest(),
        egress_token_sha256=hashlib.sha256(CANARY_TOKEN.encode()).hexdigest(),
        ttl_seconds=300,
        runtime_max_sec=PROBE_RUNTIME_MAX_SEC,
        memory_max_mb=256,
        cpu_quota_percent=50,
        tasks_max=64,
    )
    argv = helper.build_systemd_run_argv(
        request,
        task_dir=task_dir,
        runtime_root=runtime_root,
        broker_socket=broker_socket,
        workload=workload,
    )

    assert argv[0] == helper.SYSTEMD_RUN
    assert "--property=Group=agentmgr-hermes" in argv
    assert "--property=DynamicUser=yes" in argv
    assert "--property=PrivateNetwork=yes" in argv
    assert "--property=PrivateDevices=yes" in argv
    assert f"--property=InaccessiblePaths=-{task_dir.parent}" in argv
    assert f"--property=InaccessiblePaths=-{broker_socket.parent}" in argv
    assert f"--property=BindPaths={task_dir}:/task" in argv
    assert f"--property=BindReadOnlyPaths={runtime_root}:/runtime" in argv
    assert f"--property=BindReadOnlyPaths={workload}:/workload.py" in argv
    assert not any("SupplementaryGroups" in item for item in argv)
    assert not any(CANARY_PROMPT in item or CANARY_TOKEN in item for item in argv)


def test_property_parser_reads_repeated_and_single_values() -> None:
    dump = "\n".join(
        [
            "Id=agentmgr-hermes-0123456789abcdef.service",
            "Group=agentmgr-hermes",
            "SupplementaryGroups=",
            "InaccessiblePaths=-/etc/agentmgr",
            "InaccessiblePaths=-/var/lib/agentmgr",
            "PrivateDevices=yes",
        ]
    )
    assert _property(dump, "Group") == "agentmgr-hermes"
    assert _property_values(dump, "SupplementaryGroups") == [""]
    assert _property_values(dump, "InaccessiblePaths") == [
        "-/etc/agentmgr",
        "-/var/lib/agentmgr",
    ]
    assert _property(dump, "PrivateDevices") == "yes"
    assert _property(dump, "PrivateNetwork") == ""


def test_live_gate_names_both_required_variables() -> None:
    assert LIVE_ENABLED == all(
        os.environ.get(name) == "1" for name in LIVE_ENV_VARS
    )
    for name in LIVE_ENV_VARS:
        assert name in LIVE_SKIP_REASON
