"""CLI and trusted-config tests for Hermes managed execution (Issue #97, Task 9).

Everything here is DB-free, network-free and privilege-free.  A ``--noconftest``
run must work, so the module builds its own trusted-config fixtures:

* a missing or malformed trusted configuration fails closed with a stable code
  and never prints the config path, a secret value or an exception message;
* the loader rejects a helper path other than the fixed sudoers wrapper, a
  runtime hash that is not ``sha256:<64 hex>``, control/revocation paths other
  than the provisioned ones, key paths outside the unit-invisible
  ``/etc/agentmgr`` root, a model name that could parse as an option, a local
  proxy template other than the isolated loopback endpoint, provider/path
  mismatches, worker/lease violations and ``runtime_max_sec < 9``;
* the loader accepts no secret-bearing field at all (the provider key belongs
  to the broker);
* an injected trusted dispatcher factory is the only way to run ``--once`` or
  ``--loop`` in these tests; without it there is no Fake/Advisory/Hermes-host
  fallback and a missing service principal is a stable failure;
* only a stable outcome/exception type is printed, never a payload.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime import hermes_managed_cli as cli
from app.runtime.hermes_managed import (
    RUNTIME_ATTESTATION_SCHEMA,
    HermesManagedConfig,
)
from app.tasks.execution import DispatcherResult

EVIDENCE_REF = "evidence:hermes:managed:test-0001"
RUNTIME_ROOT_HASH = "sha256:" + "1" * 64
ATTESTATION_KEY = b"a" * 32
EGRESS_KEY = b"e" * 32
SANDBOX_FACTS = {
    "private_network": True,
    "protect_home": True,
    "protect_system": "strict",
    "no_new_privileges": True,
    "capability_bounding_set": "empty",
    "external_toolsets": [],
    "toolsets": ["terminal"],
    "external_ip": False,
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Create a trusted layout and point the fixed roots at it.

    The production defaults are absolute deployment paths (``/etc/agentmgr`` and
    ``/var/lib/agentmgr-hermes-control``); the unprivileged test fixture moves the
    module constants so the loader can be exercised without root.
    """

    agentmgr_etc = tmp_path / "etc" / "agentmgr"
    hermes_etc = agentmgr_etc / "hermes"
    hermes_etc.mkdir(parents=True)
    control = tmp_path / "control"
    control.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    revoked = tmp_path / "revoked"
    revoked.mkdir()
    (hermes_etc / "attestation-key").write_bytes(ATTESTATION_KEY)
    (hermes_etc / "egress-token-key").write_bytes(EGRESS_KEY)
    for path in (hermes_etc / "attestation-key", hermes_etc / "egress-token-key"):
        path.chmod(0o600)

    monkeypatch.setattr(cli, "ATTESTATION_KEY_ROOT", agentmgr_etc)
    monkeypatch.setattr(cli, "CONTROL_DIR", control)
    monkeypatch.setattr(cli, "BROKER_SOCKET", control / "egress.sock")
    # The trusted config is root-owned in production; the unprivileged test
    # fixture owns its file instead (same seam as the root-helper tests).
    monkeypatch.setattr(cli, "CONFIG_OWNER_UID", os.geteuid())

    layout = SimpleNamespace(
        root=tmp_path,
        etc=hermes_etc,
        control=control,
        runtime=runtime,
        tasks=tasks,
        revoked=revoked,
        attestation_key=hermes_etc / "attestation-key",
        egress_key=hermes_etc / "egress-token-key",
        attestation=hermes_etc / "runtime-attestation.json",
    )
    _write_attestation(layout)
    return layout


def _write_attestation(
    layout: SimpleNamespace,
    *,
    path: Path | None = None,
    **overrides: object,
) -> Path:
    document: dict[str, object] = {
        "schema": RUNTIME_ATTESTATION_SCHEMA,
        "helper_version": "1",
        "runtime_name": "hermes",
        "runtime_version": "0.22.1",
        "runtime_root_hash": RUNTIME_ROOT_HASH,
        "sandbox_facts": dict(SANDBOX_FACTS),
        "broker_socket_ok": True,
        "evidence_ref": EVIDENCE_REF,
        "issued_at": "2026-10-25T12:00:00Z",
        "expires_at": "2026-10-25T12:05:00Z",
        "nonce": "n" * 32,
        "hmac": "0" * 64,
    }
    document.update(overrides)
    target = path or layout.attestation
    target.write_text(json.dumps(document), encoding="utf-8")
    target.chmod(0o600)
    return target


def _config_document(
    layout: SimpleNamespace, **overrides: object
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": cli.CONFIG_SCHEMA,
        "service_identity_id": "svc_1",
        "helper_path": str(cli.SUDOERS_WRAPPER),
        "runtime_root": str(layout.runtime),
        "runtime_root_hash": RUNTIME_ROOT_HASH,
        "task_root": str(layout.tasks),
        "control_dir": str(layout.control),
        "broker_socket": str(cli.BROKER_SOCKET),
        "revocation_dir": str(layout.revoked),
        "attestation_key_path": str(layout.attestation_key),
        "egress_token_key_path": str(layout.egress_key),
        "runtime_attestation_path": str(layout.attestation),
        "evidence_ref": EVIDENCE_REF,
        "runtime_name": "hermes",
        "runtime_version": "0.22.1",
        "model_name": "test-model",
        "egress_provider_host": "provider.example",
        "worker_id": "worker-1",
        "runtime_max_sec": 900,
        "lease_seconds": 60,
        "poll_interval": 0.1,
        "health_backoff_seconds": 0.1,
    }
    document.update(overrides)
    return document


def _write_config(
    layout: SimpleNamespace,
    path: Path | None = None,
    *,
    mode: int = 0o600,
    **overrides: object,
) -> Path:
    target = path or layout.root / "managed-cli.json"
    target.write_text(json.dumps(_config_document(layout, **overrides)), encoding="utf-8")
    target.chmod(mode)
    return target


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return _layout(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# trusted configuration loader
# ---------------------------------------------------------------------------


def test_load_config_accepts_the_trusted_document(layout):
    path = _write_config(layout)

    config = cli.load_config(path)

    assert isinstance(config, HermesManagedConfig)
    assert config.helper_path == cli.SUDOERS_WRAPPER
    assert config.runtime_root == layout.runtime
    assert config.runtime_root_hash == RUNTIME_ROOT_HASH
    assert config.task_root == layout.tasks
    assert config.broker_socket == cli.BROKER_SOCKET
    assert config.attestation_key_path == layout.attestation_key
    assert config.egress_token_key_path == layout.egress_key
    assert config.runtime_attestation_path == layout.attestation
    assert config.evidence_ref == EVIDENCE_REF
    assert config.model_name == "test-model"
    assert config.egress_provider_host == "provider.example"
    assert config.worker_id == "worker-1"
    assert config.runtime_max_sec == 900
    assert config.lease_seconds == 60


def test_load_config_requires_a_regular_private_file(layout):
    missing = layout.root / "missing.json"
    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(missing)
    assert excinfo.value.code == "config_unavailable"

    path = _write_config(layout)
    path.chmod(0o644)
    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_mode_invalid"

    path.chmod(0o660)
    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_mode_invalid"

    path.chmod(0o600)
    target = layout.root / "config-target.json"
    os.replace(path, target)
    path.symlink_to(target)
    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_invalid"


def test_config_owner_defaults_to_root():
    assert cli.CONFIG_OWNER_UID == 0


def test_config_owner_mismatch_fails_closed(layout, monkeypatch):
    path = _write_config(layout)
    monkeypatch.setattr(cli, "CONFIG_OWNER_UID", os.geteuid() + 1)

    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_owner_invalid"


def test_load_config_rejects_malformed_json_without_echoing_it(layout):
    path = layout.root / "managed-cli.json"
    path.write_text('{"secret": "TOP-SECRET-VALUE",', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_invalid"
    assert "TOP-SECRET-VALUE" not in str(excinfo.value)
    assert str(path) not in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"unknown_field": "x"}, "config_unknown_field"),
        ({"provider_key": "TOP-SECRET-VALUE"}, "config_secret_field"),
        ({"api_key": "TOP-SECRET-VALUE"}, "config_secret_field"),
        ({"helper_path": "/usr/bin/sudo"}, "config_helper_path_invalid"),
        ({"runtime_root_hash": "sha256:" + "A" * 64}, "config_runtime_hash_invalid"),
        ({"runtime_root_hash": "not-a-digest"}, "config_runtime_hash_invalid"),
        ({"runtime_root": "relative/runtime"}, "config_unsafe_path"),
        ({"task_root": str(Path("/") / ".." / "tasks")}, "config_unsafe_path"),
        ({"control_dir": "/var/lib/other-control"}, "config_control_path_invalid"),
        (
            {"broker_socket": "/run/agentmgr-hermes-control/egress.sock"},
            "config_control_path_invalid",
        ),
        ({"attestation_key_path": "/tmp/attestation-key"}, "config_key_path_invalid"),
        ({"egress_token_key_path": "/tmp/egress-key"}, "config_key_path_invalid"),
        (
            {"runtime_attestation_path": "/tmp/runtime-attestation.json"},
            "config_key_path_invalid",
        ),
        ({"evidence_ref": "not-evidence"}, "config_evidence_ref_invalid"),
        ({"model_name": "--dangerous-option"}, "config_model_invalid"),
        ({"model_name": ""}, "config_model_invalid"),
        ({"runtime_name": ""}, "config_runtime_invalid"),
        ({"runtime_version": "not a version"}, "config_runtime_invalid"),
        (
            {"local_proxy_base_url_template": "http://example.com:{port}/v1"},
            "config_proxy_invalid",
        ),
        (
            {"local_proxy_base_url_template": "http://127.0.0.1:{port}/"},
            "config_proxy_invalid",
        ),
        ({"egress_path_prefix": "/"}, "config_path_prefix_invalid"),
        ({"egress_path_prefix": "v1"}, "config_path_prefix_invalid"),
        ({"egress_provider_host": "provider host"}, "config_provider_invalid"),
        ({"worker_id": ""}, "config_worker_invalid"),
        ({"service_identity_id": "prin_1"}, "config_service_identity_invalid"),
        ({"runtime_max_sec": 8}, "config_limit_invalid"),
        ({"lease_seconds": 0}, "config_limit_invalid"),
        ({"poll_interval": 0}, "config_limit_invalid"),
        ({"health_backoff_seconds": -1}, "config_limit_invalid"),
        ({"runtime_root_hash": None}, "config_runtime_hash_invalid"),
    ],
)
def test_load_config_fails_closed_on_invalid_values(layout, overrides, code):
    path = _write_config(layout, **overrides)

    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "service_identity_id",
        "helper_path",
        "runtime_root",
        "runtime_root_hash",
        "task_root",
        "control_dir",
        "broker_socket",
        "revocation_dir",
        "attestation_key_path",
        "egress_token_key_path",
        "runtime_attestation_path",
        "evidence_ref",
        "runtime_name",
        "runtime_version",
        "model_name",
        "egress_provider_host",
        "worker_id",
    ],
)
def test_load_config_requires_every_trusted_field(layout, field):
    document = _config_document(layout)
    document.pop(field)
    path = layout.root / "managed-cli.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_missing_field"


def test_load_config_rejects_shared_parent_directories(layout):
    # The runtime root must not be the task root or an ancestor of it: one is
    # read-only host code, the other is writable per-task state.
    path = _write_config(layout, task_root=str(layout.runtime))

    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_unsafe_path"


def test_load_config_rejects_a_revocation_dir_inside_the_task_root(layout):
    path = _write_config(layout, revocation_dir=str(layout.tasks / "revoked"))

    with pytest.raises(cli.HermesCliError) as excinfo:
        cli.load_config(path)
    assert excinfo.value.code == "config_unsafe_path"


def test_load_config_accepts_the_runtime_max_sec_floor(layout):
    config = cli.load_config(_write_config(layout, runtime_max_sec=9))
    assert config.runtime_max_sec == 9


def test_load_config_has_no_secret_bearing_fields(layout):
    # Even a well-formed JSON document cannot smuggle a provider credential or
    # an inline key into the launcher configuration.
    for name in ("token", "secret", "password", "provider_key", "api_key"):
        path = _write_config(layout, **{name: "TOP-SECRET-VALUE"})
        with pytest.raises(cli.HermesCliError) as excinfo:
            cli.load_config(path)
        assert excinfo.value.code == "config_secret_field"
        assert "TOP-SECRET-VALUE" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI entry point: fail-closed paths
# ---------------------------------------------------------------------------


def test_missing_configuration_fails_closed(capsys, monkeypatch):
    factory_calls: list[object] = []

    def factory(config: object) -> object:
        factory_calls.append(config)
        return None

    assert cli.main(["--once"], dispatcher_factory=factory) == 2
    err = capsys.readouterr().err
    assert "#97" in err.lower()
    assert "configuration" in err.lower()
    assert factory_calls == []


def test_exactly_one_mode_is_required(layout, capsys):
    path = _write_config(layout)
    factory_calls: list[object] = []

    def factory(config: object) -> object:
        factory_calls.append(config)
        return None

    assert cli.main(["--config", str(path)], dispatcher_factory=factory) == 2
    assert (
        cli.main(
            ["--once", "--loop", "--config", str(path)],
            dispatcher_factory=factory,
        )
        == 2
    )
    assert factory_calls == []
    capsys.readouterr()


def test_missing_config_file_prints_a_stable_code_without_the_path(
    layout, capsys
):
    missing = layout.root / "missing-managed-cli.json"
    assert cli.main(["--once", "--config", str(missing)]) == 2
    err = capsys.readouterr().err
    assert "config_unavailable" in err
    assert str(missing) not in err


def test_invalid_config_prints_no_paths_or_secrets(layout, capsys):
    path = _write_config(layout, provider_key="TOP-SECRET-VALUE")

    assert cli.main(["--once", "--config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "config_secret_field" in err
    assert "TOP-SECRET-VALUE" not in err
    assert str(path) not in err
    assert str(layout.etc) not in err


def test_missing_runtime_attestation_fails_closed(layout, capsys):
    layout.attestation.unlink()
    path = _write_config(layout)

    assert cli.main(["--once", "--config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "attestation_unavailable" in err
    assert str(layout.attestation) not in err


def test_invalid_runtime_attestation_fails_closed(layout, capsys):
    _write_attestation(layout, runtime_root_hash="sha256:" + "2" * 64)
    path = _write_config(layout)

    assert cli.main(["--once", "--config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "attestation_invalid" in err
    assert "sha256:" not in err


def test_missing_key_file_fails_closed(layout, capsys):
    layout.attestation_key.unlink()
    path = _write_config(layout)

    assert cli.main(["--once", "--config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "attestation_key_unavailable" in err
    assert str(layout.attestation_key) not in err


def test_missing_service_principal_fails_closed(layout, capsys, monkeypatch):
    path = _write_config(layout)

    class _Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    session = _Session()

    def _factory() -> object:
        return session

    def _resolve(_session: object, _identity: str) -> object:
        from app.governance.errors import PrincipalResolutionError

        raise PrincipalResolutionError("principal_unbound")

    monkeypatch.setattr(cli, "_session_factory", _factory)
    monkeypatch.setattr(cli, "_resolve_service", _resolve)
    monkeypatch.setattr(cli, "_helper_is_executable", lambda _config: True)

    assert cli.main(["--once", "--config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "principal_unbound" in err
    assert "config_" not in err
    assert session.closed is True


# ---------------------------------------------------------------------------
# CLI entry point: injected trusted dispatcher
# ---------------------------------------------------------------------------


def test_once_calls_run_once_with_the_trusted_config(layout, capsys):
    path = _write_config(layout)
    calls: list[str] = []
    seen: list[object] = []

    class _Runner:
        def run_once(self) -> DispatcherResult:
            calls.append("run_once")
            return DispatcherResult(outcome="idle")

    def factory(config: object) -> object:
        seen.append(config)
        return _Runner()

    assert cli.main(["--once", "--config", str(path)], dispatcher_factory=factory) == 0

    assert calls == ["run_once"]
    assert len(seen) == 1
    assert isinstance(seen[0], HermesManagedConfig)
    assert seen[0].runtime_root == layout.runtime
    captured = capsys.readouterr()
    assert captured.out == "idle\n"
    assert captured.err == ""


def test_loop_calls_run_forever_until_the_stop_event(layout, capsys):
    path = _write_config(layout)
    observed: list[bool] = []

    class _Runner:
        def run_forever(self, stop_event: threading.Event) -> None:
            observed.append(stop_event.is_set())
            stop_event.set()

    assert (
        cli.main(
            ["--loop", "--config", str(path)],
            dispatcher_factory=lambda _config: _Runner(),
        )
        == 0
    )

    assert observed == [False]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_dispatcher_exception_prints_only_the_type(layout, capsys):
    path = _write_config(layout)

    class _Runner:
        def run_once(self) -> DispatcherResult:
            raise RuntimeError("token=TOP-SECRET-VALUE /srv/operator/state.db")

    assert (
        cli.main(
            ["--once", "--config", str(path)],
            dispatcher_factory=lambda _config: _Runner(),
        )
        == 1
    )

    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "TOP-SECRET-VALUE" not in captured.out + captured.err
    assert "/srv" not in captured.out + captured.err
    assert captured.out == ""


def test_no_default_adapter_or_fallback_exists():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    # There is one construction path: the Hermes managed adapter.  No Fake,
    # Advisory or host-Hermes fallback may be reachable from the launcher.
    assert "FakeAdapter" not in source
    assert "Advisory" not in source
    assert "app.runtime.hermes import" not in source
    assert "HermesAdapter" not in source
    assert "fallback" not in source.lower()


def test_config_loader_needs_no_attestation_key_owner_seam(layout):
    # The loader validates shape and the pinned roots only.  Key ownership and
    # mode are runtime preflight checks, so the loader stays usable for a
    # deployment whose adapter identity is not root.
    assert stat.S_IMODE(layout.attestation_key.stat().st_mode) == 0o600
    config = cli.load_config(_write_config(layout))
    assert config.attestation_key_path == layout.attestation_key


# ---------------------------------------------------------------------------
# provisioning and verification scripts (text/consistency checks only)
# ---------------------------------------------------------------------------


def _operations_script(name: str) -> Path:
    return Path(cli.__file__).resolve().parents[2] / "ops" / "security" / name


def test_provisioning_script_pins_the_fixed_surface_and_is_shell_safe():
    source = _operations_script("provision_hermes_runtime.sh").read_text(encoding="utf-8")

    for fixed in (
        "/usr/local/libexec/agentmgr_hermes_exec.py",
        "/usr/local/libexec/agentmgr-hermes-exec",
        "/usr/local/libexec/agentmgr_hermes_workload.py",
        "/etc/sudoers.d/agentmgr-hermes",
        "/etc/agentmgr/hermes-exec.json",
        "/etc/agentmgr/hermes/egress-broker.json",
        "/var/lib/agentmgr-hermes-control",
        'CONTROL_SOCKET="${CONTROL_DIR}/egress.sock"',
    ):
        assert fixed in source
    assert "--dry-run" in source
    assert "--rollback" in source
    assert "--refresh-runtime" in source

    # No shell indirection, no eval, and no service start: provisioning installs
    # files only, and never touches a personal/default Hermes gateway.
    assert "eval " not in source
    assert "sh -c" not in source
    assert "systemctl start" not in source
    assert "hermes gateway" not in source
    assert "systemctl enable --now agentmgr-hermes-egress.service" in source


def test_verification_script_is_read_only_and_stdlib_only():
    shell = _operations_script("verify_hermes_isolation.sh").read_text(encoding="utf-8")
    python = _operations_script("verify_hermes_isolation.py").read_text(encoding="utf-8")

    assert "exec python3" in shell
    assert "verify_hermes_isolation.py" in shell
    assert "from app." not in python
    assert "import app" not in python
    assert "subprocess" not in python
    assert "systemctl" not in python


def _load_verifier():
    import importlib.util
    import sys

    path = _operations_script("verify_hermes_isolation.py")
    spec = importlib.util.spec_from_file_location("verify_hermes_isolation_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_manifest_hash_matches_the_provisioning_script(tmp_path):
    import re
    import subprocess
    import sys

    verifier = _load_verifier()
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "hermes").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / "bin" / "hermes").chmod(0o755)
    (root / "README").write_text("runtime\n", encoding="utf-8")
    (root / "link").symlink_to("README")

    source = _operations_script("provision_hermes_runtime.sh").read_text(encoding="utf-8")
    block = re.findall(r"<<'PY'\n(.*?)\nPY\n", source, re.S)[0]
    completed = subprocess.run(
        [sys.executable, "-", str(root)],
        input=block,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert verifier._runtime_manifest(root) == completed.stdout.strip()


def test_verifier_rejects_a_runtime_symlink_escape(tmp_path):
    verifier = _load_verifier()
    root = tmp_path / "runtime"
    root.mkdir()
    (root / ".env").write_text("canary\n", encoding="utf-8")
    assert verifier._profile_marker(root) == ".env"

    (root / "escape").symlink_to("/home/<user>/.hermes/profile")
    try:
        verifier._runtime_manifest(root)
    except verifier.VerificationError as exc:
        assert exc.code == "runtime_root_symlink_escape"
    else:  # pragma: no cover - the assertion above is the expected path
        raise AssertionError("a personal-profile symlink must be rejected")


def test_build_runner_wires_the_managed_adapter_and_dispatcher(layout, monkeypatch):
    launcher = cli.load_launcher_config(_write_config(layout))
    captured: dict[str, object] = {}

    class _Session:
        def close(self) -> None:
            captured["closed"] = True

    class _Principal:
        is_service = True

        def require_active(self) -> "_Principal":
            captured["active"] = True
            return self

    class _Supervisor:
        def __init__(self, config: object) -> None:
            captured["supervisor_config"] = config

    class _Adapter:
        def __init__(self, config: object, supervisor: object) -> None:
            captured["adapter_config"] = config
            captured["supervisor"] = supervisor

    class _Dispatcher:
        def __init__(
            self,
            adapter: object,
            config: object,
            *,
            service_principal: object,
            session: object,
            session_factory: object = None,
        ) -> None:
            captured["adapter"] = adapter
            captured["dispatcher_config"] = config
            captured["service_principal"] = service_principal
            captured["session"] = session
            captured["session_factory"] = session_factory

    monkeypatch.setattr(cli, "_session_factory", lambda: _Session())
    monkeypatch.setattr(cli, "_resolve_service", lambda _session, _identity: _Principal())
    monkeypatch.setattr(cli, "HelperSandboxSupervisor", _Supervisor)
    monkeypatch.setattr(cli, "HermesManagedAdapter", _Adapter)
    monkeypatch.setattr(cli, "RuntimeDispatcher", _Dispatcher)

    runner = cli._build_runner(launcher)
    runner.close()

    assert captured["active"] is True
    assert captured["adapter_config"] is launcher.managed
    assert captured["supervisor_config"] is launcher.managed
    assert captured["service_principal"].is_service is True
    dispatcher_config = captured["dispatcher_config"]
    assert dispatcher_config.worker_id == "worker-1"
    assert dispatcher_config.lease_seconds == 60
    assert captured["closed"] is True
