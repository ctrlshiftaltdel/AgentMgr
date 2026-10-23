"""Live managed-execution isolation tests (Issue #82).

The live tests in :class:`TestLiveIsolation` need the owner-approved isolation
test host: a Linux host with systemd >= 249, passwordless ``sudo -n`` for the
controller user and the ability to start transient ``DynamicUser`` units.  They
are skipped unless **both** ``AGENTMGR_ISOLATION_TEST=1`` and
``AGENTMGR_ISOLATION_SUDO=1`` are set; a skip is not acceptance evidence.

Every live test consumes one evidence document produced by a single
``ops/security/verify_isolation.py`` run (the module-scoped ``evidence``
fixture).  The harness starts the PEP gateway in-process with a live in-memory
Grant provider, launches real transient units and records the §16.3 probes.

The two tests outside that class are DB-free unit tests: they run on any host and
keep this module meaningful when the live tests skip.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

LIVE_ENV_VARS = ("AGENTMGR_ISOLATION_TEST", "AGENTMGR_ISOLATION_SUDO")
LIVE_ENABLED = all(os.environ.get(name) == "1" for name in LIVE_ENV_VARS)
LIVE_SKIP_REASON = (
    "requires the owner-approved isolation test host: set "
    "AGENTMGR_ISOLATION_TEST=1 and AGENTMGR_ISOLATION_SUDO=1"
)
LIVE_TIMEOUT_SECONDS = 240

#: A class-level skip keeps the unit tests below running on hosts without the
#: isolation test host (a module-level ``pytestmark`` would skip them too).
live_only = pytest.mark.skipif(not LIVE_ENABLED, reason=LIVE_SKIP_REASON)


def _evidence_path() -> Path:
    configured = os.environ.get("AGENTMGR_ISOLATION_EVIDENCE")
    if configured:
        return Path(configured)
    directory = Path(tempfile.mkdtemp(prefix="agentmgr-isolation-evidence-"))
    return directory / "evidence.json"


@pytest.fixture(scope="module")
def evidence() -> dict[str, object]:
    """Run the live harness once and return its evidence document."""

    if not LIVE_ENABLED:
        pytest.skip(LIVE_SKIP_REASON)

    path = _evidence_path().resolve()
    assert not path.is_relative_to(REPO_ROOT), (
        "AGENTMGR_ISOLATION_EVIDENCE must point outside the repository"
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    environment = dict(os.environ)
    environment["AGENTMGR_ISOLATION_EVIDENCE"] = str(path)
    environment["PYTHONPATH"] = str(REPO_ROOT)

    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "ops" / "security" / "verify_isolation.py")],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=LIVE_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr)[-2000:]
        pytest.fail(
            f"verify_isolation.py exited {completed.returncode}: {detail}"
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _probes(evidence: dict[str, object], *, row: int | None = None) -> list[dict]:
    items = evidence.get("probes")
    assert isinstance(items, list)
    return [
        item
        for item in items
        if isinstance(item, dict) and (row is None or item.get("row") == row)
    ]


def _assert_rows_pass(evidence: dict[str, object], rows: set[int]) -> None:
    for row in sorted(rows):
        matched = _probes(evidence, row=row)
        assert matched, f"matrix row {row} has no probe evidence"
        failed = [item for item in matched if item.get("passed") is not True]
        assert not failed, f"matrix row {row} failed: {failed}"


def _probe(evidence: dict[str, object], probe_id: str) -> dict:
    matched = [item for item in _probes(evidence) if item.get("id") == probe_id]
    assert matched, f"probe {probe_id!r} missing from evidence"
    assert matched[0].get("passed") is True, matched[0]
    return matched[0]


def _gateway_probe(evidence: dict[str, object], probe_id: str) -> dict:
    gateway = evidence.get("gateway")
    assert isinstance(gateway, dict)
    items = gateway.get("probes")
    assert isinstance(items, list)
    matched = [item for item in items if item.get("id") == probe_id]
    assert matched, f"gateway probe {probe_id!r} missing"
    assert matched[0].get("passed") is True, matched[0]
    return matched[0]


@live_only
class TestLiveIsolation:
    """The §16.3 real-isolation matrix, read from one harness run."""

    def test_live_filesystem_and_privilege_boundaries(
        self, evidence: dict[str, object]
    ) -> None:
        # Rows 1-5 and 10-14: host files, cross-task state/credential sources,
        # capabilities, namespaces and process visibility.
        _assert_rows_pass(evidence, {1, 2, 3, 4, 5, 10, 11, 12, 13, 14})

        process = evidence.get("process")
        assert isinstance(process, dict)
        assert process["cap_eff"] == "0000000000000000"
        assert process["no_new_privs"] == "1"
        assert process["proc_only_self"] is True
        assert process["uid"] >= 1

    def test_live_network_is_af_unix_only(
        self, evidence: dict[str, object]
    ) -> None:
        # Rows 6-9 run inside the unit; row 15 is the one allowed gateway read.
        _assert_rows_pass(evidence, {6, 7, 8, 9, 15})

        for probe in _probes(evidence, row=6) + _probes(evidence, row=7) + (
            _probes(evidence, row=8) + _probes(evidence, row=9)
        ):
            assert probe["observed"] in {"EAFNOSUPPORT", "EPERM", "EACCES"}, probe

        assert evidence.get("network_profile") in {
            "private_netns",
            "af_unix_only_fallback",
        }
        profiles = evidence.get("profile_probes")
        assert isinstance(profiles, list) and profiles
        assert any(item.get("used") is True for item in profiles)

    def test_live_credential_expiry_revocation_scope_and_cross_task(
        self, evidence: dict[str, object]
    ) -> None:
        assert _probe(evidence, "gateway-allowed-read")["observed"] == "ok"
        assert (
            _probe(evidence, "gateway-expired-credential")["observed"]
            == "credential_expired"
        )
        assert (
            _probe(evidence, "gateway-revoked-credential")["observed"]
            == "credential_revoked"
        )
        assert (
            _probe(evidence, "gateway-cross-task-identity")["observed"]
            == "cross_task_denied"
        )
        # Another task's resource is outside this credential's resource scope;
        # identity (task/grant/subject) mismatch is the separate cross_task case.
        cross_resource = _probe(evidence, "gateway-cross-task-resource")
        assert cross_resource["observed"] == "resource_scope_denied"
        assert "scope" in cross_resource.get("note", "")
        assert (
            _probe(evidence, "gateway-out-of-scope-resource")["observed"]
            == "resource_scope_denied"
        )
        assert (
            _probe(evidence, "gateway-ungranted-capability")["observed"]
            == "capability_not_granted"
        )
        assert (
            _probe(evidence, "gateway-grant-revoked")["observed"] == "grant_revoked"
        )
        # Revocation is followed by a verified stop, not just a denied call.
        assert _probe(evidence, "stop-on-revoke")["process_gone"] is True

    def test_live_ttl_and_stop_confirmed(
        self, evidence: dict[str, object]
    ) -> None:
        timeout = _probe(evidence, "ttl-timeout")
        assert timeout["observed"] == "timeout"
        assert timeout["elapsed_seconds"] <= 8.0
        assert timeout["process_gone"] is True
        assert timeout["stop_confirmed"] is True
        assert timeout["main_pid"] == 0

        stop = _probe(evidence, "stop-on-revoke")
        assert stop["elapsed_seconds"] <= 5.0
        assert stop["main_pid"] == 0
        assert stop["process_gone"] is True
        assert stop["stop_confirmed"] is True

        ttl = evidence.get("ttl")
        assert isinstance(ttl, dict)
        assert ttl["timeout_effective_seconds"] == 2
        assert ttl["matrix_effective_seconds"] < ttl["matrix_requested_seconds"]

    def test_live_gateway_rate_limits(
        self, evidence: dict[str, object]
    ) -> None:
        gateway = evidence.get("gateway")
        assert isinstance(gateway, dict)
        assert gateway["mode"] == "in_process_harness"
        assert gateway["grant_provider"] == "in_memory_live"
        limits = gateway["limits"]
        assert limits == {
            "per_task_connections": 8,
            "per_task_requests_per_second": 16,
            "global_connections": 64,
            "global_requests_per_second": 200,
            "max_request_bytes": 65536,
            "read_write_timeout_seconds": 5,
        }

        rate = _gateway_probe(evidence, "gateway-rate-limit")
        assert rate["observed"] == "rate_limited"
        assert rate["requests"] == 17
        assert rate["throttled_index"] == 17

        busy = _gateway_probe(evidence, "gateway-busy")
        assert busy["observed"] == "busy"
        assert busy["busy_after_seconds"] < 1.0
        assert busy["idle_connections"] == 8
        assert busy["idle_closed_within_seconds"] <= 7.0
        assert busy["connections_closed"] == 9
        assert busy["follow_up_allowed"] is True

    def test_live_run_directory_ownership_and_source_removal(
        self, evidence: dict[str, object]
    ) -> None:
        run = evidence.get("run_directory")
        assert isinstance(run, dict)
        assert run["owner_matches_controller"] is True
        assert run["mode"] == "0700"
        assert run["gateway_socket_owner_matches"] is True
        assert run["credential_source_present_after_active"] is False
        assert run["spec_source_present_after_active"] is False
        assert run["unit_cannot_traverse"] is True

    def test_live_cleanup_leaves_no_units_or_dirs(
        self, evidence: dict[str, object]
    ) -> None:
        cleanup = evidence.get("cleanup")
        assert isinstance(cleanup, dict)
        assert cleanup["units_remaining"] == 0
        assert cleanup["remaining_unit_names"] == []
        assert cleanup["remaining_state_dirs"] == []
        assert cleanup["run_dir_present"] is False
        assert cleanup["resources_dir_present"] is False
        assert cleanup["state_clean"] is True
        # The harness removes the shared state roots only when it created them;
        # on a host where a root pre-existed it must be left in place.
        assert cleanup["state_dir_present"] is False or (
            cleanup["state_root_preexisting"] is True
        )
        assert cleanup["state_link_present"] is False or (
            cleanup["state_link_preexisting"] is True
        )

    def test_live_evidence_has_no_token_or_host_path(
        self, evidence: dict[str, object]
    ) -> None:
        self_check = evidence.get("evidence_self_check")
        assert isinstance(self_check, dict)
        assert self_check["token_absent"] is True
        assert self_check["home_path_absent"] is True
        assert self_check["ipv4_absent"] is True
        gateway = evidence.get("gateway")
        assert isinstance(gateway, dict)
        assert gateway["audit"]["token_absent"] is True


def test_smoke_workload_gateway_call_round_trip(tmp_path: Path) -> None:
    """The unit-side NDJSON client is testable without a sandbox or database."""

    workload = importlib.import_module("ops.security.smoke_workload")
    socket_path = tmp_path / "gateway.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    received: dict[str, object] = {}
    body = {
        "ok": True,
        "result": {
            "resource_id": "res_synthetic_0001",
            "media_type": "application/json",
            "body": {"kind": "synthetic", "value": "alpha"},
        },
    }

    def serve() -> None:
        connection, _ = listener.accept()
        with connection, connection.makefile("rb") as stream:
            line = stream.readline(65537)
            received.update(json.loads(line))
            payload = json.dumps(body, separators=(",", ":")).encode("ascii")
            connection.sendall(payload + b"\n")
        listener.close()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    try:
        response = workload.gateway_call(
            "resource.read@1.0.0",
            "res_synthetic_0001",
            token="agx1.test.token",
            socket_path=socket_path,
        )
    finally:
        listener.close()
        server.join(timeout=2)

    assert response == body
    assert received == {
        "version": "1.0.0",
        "token": "agx1.test.token",
        "capability": "resource.read@1.0.0",
        "resource": "res_synthetic_0001",
        "payload": {},
    }

    # A failed connection must not echo the bearer token in its message.
    with pytest.raises(workload.WorkloadError) as failure:
        workload.gateway_call(
            "resource.read@1.0.0",
            "res_synthetic_0001",
            token="agx1.test.token",
            socket_path=tmp_path / "missing.sock",
        )
    assert "agx1.test.token" not in str(failure.value)


def test_harness_refuses_evidence_path_inside_repository(
    tmp_path: Path,
) -> None:
    """Evidence must never be written into the repository working tree."""

    from app.security.gateway import GatewayLimits

    harness = importlib.import_module("ops.security.verify_isolation")
    outside = tmp_path / "evidence.json"
    assert harness.validate_evidence_path(outside, REPO_ROOT) == outside

    config = harness.HarnessConfig(
        repo_root=REPO_ROOT,
        run_root=harness.DEFAULT_RUNTIME_ROOT,
        resources_root=harness.RESOURCES_ROOT,
        evidence_path=outside,
    )
    assert config.gateway_limits == GatewayLimits()
    assert config.run_root.is_absolute()
    assert config.resources_root.is_absolute()

    with pytest.raises(harness.HarnessError):
        harness.validate_evidence_path(REPO_ROOT / "evidence.json", REPO_ROOT)


def _unit_harness(tmp_path: Path):
    harness = importlib.import_module("ops.security.verify_isolation")
    config = harness.HarnessConfig(
        repo_root=REPO_ROOT,
        run_root=tmp_path / "run",
        resources_root=tmp_path / "resources",
        evidence_path=tmp_path / "evidence.json",
    )
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    return harness, harness._Harness(config, staging)


def _completed(
    argv: list[str] | tuple[str, ...], returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, "", "")


def test_optional_inaccessible_targets_strip_prefix(tmp_path: Path) -> None:
    harness, instance = _unit_harness(tmp_path)

    assert harness.optional_inaccessible_targets(
        ["-/etc/agentmgr", "-/run/dbus", "/srv"]
    ) == ("/etc/agentmgr", "/run/dbus", "/srv")

    raw = instance._inaccessible_path_entries()
    assert raw and all(entry.startswith("-/") for entry in raw)
    stripped = instance._inaccessible_paths()
    assert stripped == list(harness.optional_inaccessible_targets(raw))
    assert all(not path.startswith("-") for path in stripped)


def test_preflight_allows_absent_optional_paths(
    tmp_path: Path, monkeypatch
) -> None:
    harness, instance = _unit_harness(tmp_path)
    absent = {"/etc/agentmgr", "/var/lib/agentmgr"}
    not_yet_created = {str(instance.run_root), str(instance.resources_root)}

    def fake_run(argv, timeout=60.0):
        if len(argv) > 1 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                list(argv), 0, "systemd 257 (257.7-1)\n", ""
            )
        return _completed(argv)

    def fake_exists(path) -> bool:
        text = str(path)
        return text not in absent and text not in not_yet_created

    monkeypatch.setattr(harness.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(harness.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(harness, "_run", fake_run)
    monkeypatch.setattr(harness, "_sudo", lambda *args, **kwargs: _completed(args))
    monkeypatch.setattr(harness, "_sudo_exists", fake_exists)

    evidence = harness._new_evidence(REPO_ROOT, harness.datetime.now(harness.UTC))
    instance.preflight(evidence)  # must not raise on absent optional paths

    assert evidence["preflight"]["inaccessible_paths_optional"] is True
    assert evidence["preflight"]["inaccessible_paths_absent"] == [
        "/etc/agentmgr",
        "/var/lib/agentmgr",
    ]
    assert instance.state_checked is True
    assert instance.state_root_preexisting is True


def test_remaining_units_fails_closed(tmp_path: Path, monkeypatch) -> None:
    harness, instance = _unit_harness(tmp_path)

    monkeypatch.setattr(
        harness,
        "_sudo",
        lambda *args, **kwargs: _completed(args, returncode=5),
    )
    with pytest.raises(harness.HarnessError) as failure:
        instance._remaining_units()
    assert str(failure.value) == "unit_list_failed"
    with pytest.raises(harness.HarnessError):
        instance.record_cleanup({})

    evidence = _passing_evidence()
    evidence["cleanup"] = {}
    assert harness._overall_result(evidence) is False


def test_teardown_removes_only_created_roots(
    tmp_path: Path, monkeypatch
) -> None:
    harness, instance = _unit_harness(tmp_path)
    calls: list[list[str]] = []

    def fake_sudo(*args, **kwargs):
        calls.append(list(args))
        return _completed(args)

    monkeypatch.setattr(harness, "_sudo", fake_sudo)

    instance.created_run_root = True
    instance.created_resources_root = True
    instance.state_checked = True
    instance.state_root_preexisting = True
    instance.state_link_preexisting = True
    instance.teardown()

    removals = [call for call in calls if call[:3] == ["rm", "-rf", "--"]]
    assert len(removals) == 1
    removed = {Path(item) for item in removals[0][3:]}
    assert instance.run_root in removed
    assert instance.resources_root in removed
    assert harness.DEFAULT_STATE_ROOT not in removed
    assert harness.DEFAULT_STATE_LINK_ROOT not in removed


def test_teardown_does_not_touch_state_before_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    harness, instance = _unit_harness(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        harness, "_sudo", lambda *args, **kwargs: calls.append(list(args)) or _completed(args)
    )

    instance.teardown()

    assert [call for call in calls if call[:3] == ["rm", "-rf", "--"]] == []


def test_partial_setup_tracks_only_created_run_root(
    tmp_path: Path, monkeypatch
) -> None:
    harness, instance = _unit_harness(tmp_path)
    calls: list[list[str]] = []

    def fake_sudo(*args, **kwargs):
        calls.append(list(args))
        if (
            args[0] == "install"
            and "-d" in args
            and Path(args[-1]) == instance.resources_root
        ):
            return _completed(args, returncode=1)
        return _completed(args)

    monkeypatch.setattr(harness, "_sudo", fake_sudo)

    with pytest.raises(harness.HarnessError) as failure:
        instance.prepare_filesystem()
    assert str(failure.value) == "install_directory_failed"
    assert instance.created_run_root is True
    assert instance.created_resources_root is False

    # Teardown after the failed setup removes the run root it created, but not
    # the resources root or the shared state roots.
    instance.teardown()
    removals = [call for call in calls if call[:3] == ["rm", "-rf", "--"]]
    assert len(removals) == 1
    removed = {Path(item) for item in removals[0][3:]}
    assert instance.run_root in removed
    assert instance.resources_root not in removed
    assert harness.DEFAULT_STATE_ROOT not in removed
    assert harness.DEFAULT_STATE_LINK_ROOT not in removed


def _passing_evidence() -> dict[str, object]:
    return {
        "probes": [{"passed": True}],
        "gateway": {
            "mode": "in_process_harness",
            "grant_provider": "in_memory_live",
            "probes": [{"passed": True}],
            "audit": {"token_absent": True},
        },
        "failures": [],
        "cleanup": {
            "units_remaining": 0,
            "remaining_unit_names": [],
            "remaining_state_dirs": [],
            "run_dir_present": False,
            "resources_dir_present": False,
            "state_dir_present": False,
            "state_root_preexisting": False,
            "state_link_present": False,
            "state_link_preexisting": False,
            "state_clean": True,
        },
        "run_directory": {
            "owner_matches_controller": True,
            "mode": "0700",
            "gateway_socket_owner_matches": True,
            "credential_source_present_after_active": False,
            "spec_source_present_after_active": False,
            "unit_cannot_traverse": True,
        },
        "ttl": {
            "matrix_requested_seconds": 300,
            "matrix_effective_seconds": 240,
            "timeout_requested_seconds": 2,
            "timeout_effective_seconds": 2,
        },
        "network_profile": "private_netns",
        "profile_probes": [
            {"profile": "private_netns", "used": True, "gateway_connect": True},
            {
                "profile": "af_unix_only_fallback",
                "used": False,
                "gateway_connect": True,
            },
        ],
        "evidence_self_check": {
            "token_absent": True,
            "home_path_absent": True,
            "ipv4_absent": True,
        },
    }


def test_overall_result_accepts_a_complete_pass() -> None:
    harness = importlib.import_module("ops.security.verify_isolation")
    assert harness._overall_result(_passing_evidence()) is True


def test_overall_result_allows_only_flagged_preexisting_state() -> None:
    harness = importlib.import_module("ops.security.verify_isolation")

    evidence = _passing_evidence()
    evidence["cleanup"]["state_dir_present"] = True
    evidence["cleanup"]["state_root_preexisting"] = True
    evidence["cleanup"]["state_clean"] = True
    assert harness._overall_result(evidence) is True

    evidence["cleanup"]["state_root_preexisting"] = False
    evidence["cleanup"]["state_clean"] = False
    assert harness._overall_result(evidence) is False


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("run_directory", "mode", "0755"),
        ("run_directory", "owner_matches_controller", False),
        ("run_directory", "credential_source_present_after_active", True),
        ("run_directory", "unit_cannot_traverse", False),
        ("ttl", "matrix_effective_seconds", 301),
        ("ttl", "timeout_effective_seconds", 3),
        ("ttl", "matrix_effective_seconds", "240"),
        ("profile_probes", "x", []),
        ("gateway", "audit", {"token_absent": False}),
        ("cleanup", "remaining_state_dirs", ["agentmgr-task-aaaaaaaaaaaa"]),
        ("cleanup", "units_remaining", 1),
        ("cleanup", "state_clean", False),
        ("probes", "x", [{"passed": False}]),
    ],
)
def test_overall_result_rejects_a_failed_recorded_check(
    section: str, key: str, value: object
) -> None:
    harness = importlib.import_module("ops.security.verify_isolation")
    evidence = _passing_evidence()
    if section == "profile_probes":
        evidence[section] = value
    elif section == "probes":
        evidence[section] = value
    else:
        assert isinstance(evidence[section], dict)
        evidence[section][key] = value
    assert harness._overall_result(evidence) is False
