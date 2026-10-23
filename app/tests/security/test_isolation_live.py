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
        assert (
            _probe(evidence, "gateway-cross-task-resource")["observed"]
            == "resource_scope_denied"
        )
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
        assert cleanup["run_dir_present"] is False
        assert cleanup["state_dir_present"] is False
        assert cleanup["resources_dir_present"] is False

    def test_live_evidence_has_no_token_or_host_path(
        self, evidence: dict[str, object]
    ) -> None:
        self_check = evidence.get("evidence_self_check")
        assert isinstance(self_check, dict)
        assert self_check["token_absent"] is True
        assert self_check["home_path_absent"] is True
        assert self_check["ipv4_absent"] is True


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
