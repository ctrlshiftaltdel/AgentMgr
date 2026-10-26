"""Real MCP client + live uvicorn + real managed Hermes runtime E2E (Issue #83).

**Test-only, environment-gated, Master-run.**  Nothing in this module runs by
default: :data:`live_only` skips the live body unless ``AGENTMGR_E2E_LIVE=1``
and the #97 live gate (``AGENTMGR_HERMES_LIVE=1``, ``AGENTMGR_HERMES_SUDO=1``)
are set, and the fixture adds a skip reason for every missing runtime, helper,
key or provider input.  A skip is never acceptance evidence; the implementer
must never claim a live run.

What this module proves (when run on the approved test host)
------------------------------------------------------------

* A **real uvicorn server** listens on a loopback TCP socket and serves the
  mounted Streamable HTTP MCP endpoint at ``/mcp/``.  No ``TestClient`` and no
  ASGI transport is used.
* The **official MCP SDK** ``streamable_http_client`` with a real
  ``httpx2.AsyncClient`` completes ``initialize``/``tools/list`` and calls
  ``governance.get_task``/``governance.get_result`` with the internal bearer
  token and ``X-Employee-Id`` headers.
* The task is created by the trusted test fixture with
  ``execution_mode="managed_execution"`` (never by the public MCP submit tool)
  on the real PostgreSQL test database, and is driven by the real #96
  ``RuntimeDispatcher`` + real ``HermesManagedAdapter``/helper/broker with the
  dedicated provider key.
* The dispatcher stops at ``verifying``; an independent ``complete_task`` with
  the **actual artifact SHA-256** and evidence ref moves it to ``succeeded``.
* The MCP client then reads ``succeeded``, the exact ``result_ref``/
  ``evidence_refs`` and a passed verification; a second employee is denied with
  the stable ``task_unauthorized_actor`` code.
* The provider key never appears in the unit argv/environment, task directory,
  journal or MCP response; no auto-``succeeded`` and no
  ``side_effects_reconciled`` are recorded.

Environment (all paths absolute, on a persistent filesystem; never committed)
----------------------------------------------------------------------------

The #97 live environment contract is reused read-only from
:mod:`app.tests.tasks.test_hermes_managed_live`:

* ``AGENTMGR_E2E_LIVE=1`` -- this module's gate.
* ``AGENTMGR_HERMES_LIVE=1``, ``AGENTMGR_HERMES_SUDO=1`` -- the #97 gate.
* ``AGENTMGR_HERMES_RUNTIME_ROOT``, ``AGENTMGR_HERMES_RUNTIME_ROOT_HASH``,
  ``AGENTMGR_HERMES_TASK_ROOT``, ``AGENTMGR_HERMES_EGRESS_SOCKET``,
  ``AGENTMGR_HERMES_HELPER``, ``AGENTMGR_HERMES_ATTESTATION_KEY``,
  ``AGENTMGR_HERMES_EGRESS_TOKEN_KEY``, ``AGENTMGR_HERMES_EVIDENCE_REF``,
  ``AGENTMGR_HERMES_RUNTIME_VERSION``.
* ``AGENTMGR_HERMES_PROVIDER_KEY_FILE``, ``AGENTMGR_HERMES_PROVIDER_BASE_URL``,
  ``AGENTMGR_HERMES_MODEL`` -- the dedicated test provider key and upstream.

The suite must run as the trusted adapter user, which must be a member of the
sandbox group and allowed to run the helper with ``sudo -n``.  Every unit and
task directory this module creates is removed in ``finally``; the personal or
default Hermes profile, gateway, auth, ``.env`` and sessions are never touched.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import anyio
import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.identity.seed import seed_permission_catalog
from app.tasks.execution import DispatcherConfig, RuntimeDispatcher
from app.tests.mcp.helpers import tool_error_code, tool_json, tool_text
from app.tests.tasks.conftest import reset_task_state
from app.tests.tasks.helpers import submit_managed
from app.tests.tasks.test_hermes_managed_live import (
    ARTIFACT_BYTES,
    ARTIFACT_DIGEST,
    ARTIFACT_INSTRUCTION,
    CRITERION_ID,
    REAL_TASK_MAX_RUNTIME_SECONDS,
    VERIFIER_ID,
    VERIFIER_VERSION,
    WORKER_ID,
    LiveEnvironment,
    _artifact_goal,
    _build_world,
    _ChildCapture,
    _cleanup_unit,
    _complete_artifact_run,
    _false_success,
    _mentions,
    _real_backend,
    _RealArtifactRun,
    _require_finished_files,
    _require_verified_runtime,
    _service_principal,
    _snapshot_attempt,
    _unit_for_task,
    _wait_for_unit_gone,
    read_environment,
)

#: This module's own gate.  The #97 gate is checked separately so a missing
#: runtime/provider input produces a precise skip reason instead of a failure.
E2E_LIVE_ENV = "AGENTMGR_E2E_LIVE"
HERMES_GATE_ENV_VARS = ("AGENTMGR_HERMES_LIVE", "AGENTMGR_HERMES_SUDO")

E2E_SKIP_REASON = (
    "requires the owner-approved #83 live test host: set "
    "AGENTMGR_E2E_LIVE=1 plus the #97 live environment "
    "(AGENTMGR_HERMES_LIVE=1, AGENTMGR_HERMES_SUDO=1, runtime/task/helper/"
    "keys/model/provider inputs)"
)

#: A test-only internal token.  It is not a credential and is never printed.
E2E_INTERNAL_TOKEN = "issue-83-e2e-internal-token"

#: The live server must come up quickly; the managed run itself is bounded by
#: the #97 runtime/task budgets.
SERVER_START_TIMEOUT_SECONDS = 30.0
SERVER_STOP_TIMEOUT_SECONDS = 30.0
MCP_TIMEOUT_SECONDS = 30.0
MCP_SSE_READ_TIMEOUT_SECONDS = 300.0

live_only = pytest.mark.skipif(
    os.environ.get(E2E_LIVE_ENV) != "1", reason=E2E_SKIP_REASON
)


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def _gate_reason(environment: LiveEnvironment) -> str | None:
    """Return why the live body cannot run, or ``None`` when it can."""

    if os.environ.get(E2E_LIVE_ENV) != "1":
        return E2E_SKIP_REASON
    missing = [
        name for name in HERMES_GATE_ENV_VARS if os.environ.get(name) != "1"
    ]
    if missing:
        return (
            "the real-client managed E2E requires the #97 live gate: set "
            + ", ".join(f"{name}=1" for name in missing)
        )
    return (
        environment.managed_skip_reason()
        or environment.real_model_skip_reason()
    )


@pytest.fixture(scope="module")
def live_environment() -> LiveEnvironment:
    """Resolve the #97 live environment and skip with a precise reason."""

    environment = read_environment()
    reason = _gate_reason(environment)
    if reason:
        pytest.skip(reason)
    sudo = shutil.which("sudo") or "/usr/bin/sudo"
    try:
        completed = subprocess.run(
            [sudo, "-n", "true"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is None or completed.returncode != 0:
        pytest.skip(
            "the real-client managed E2E requires passwordless sudo "
            "('sudo -n true' failed)"
        )
    return environment


@pytest.fixture(scope="module")
def _schema(test_db_url: str) -> None:
    """Migrate the real test database to head (the root conftest also does)."""

    from alembic import command

    from app.tests.conftest import alembic_config

    command.upgrade(alembic_config(), "head")


# ---------------------------------------------------------------------------
# real TCP server harness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveServer:
    """A real uvicorn listener bound to a loopback TCP socket."""

    base_url: str
    port: int


def _wait_for_started(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if not thread.is_alive():
            pytest.fail(
                "the live uvicorn server exited before it started; the "
                "application lifespan or the MCP session manager failed"
            )
        if time.monotonic() > deadline:
            pytest.fail(
                "the live uvicorn server did not start within "
                f"{SERVER_START_TIMEOUT_SECONDS:.0f}s"
            )
        time.sleep(0.05)


@contextlib.contextmanager
def _live_server(app: Any) -> Iterator[LiveServer]:
    """Run ``app`` in a real uvicorn server on ``127.0.0.1:<free port>``.

    The socket is bound by the test, not by uvicorn's config, so the port is
    known before the server starts and no port race is possible.  The server
    runs in a daemon thread with its own event loop; the test talks to it over
    TCP only.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        name="e2e-uvicorn",
        daemon=True,
    )
    thread.start()
    try:
        _wait_for_started(server, thread)
        yield LiveServer(base_url=f"http://127.0.0.1:{port}", port=port)
    finally:
        server.should_exit = True
        thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        with contextlib.suppress(OSError):
            sock.close()
        if thread.is_alive():
            pytest.fail(
                "the live uvicorn server did not stop within "
                f"{SERVER_STOP_TIMEOUT_SECONDS:.0f}s"
            )


# ---------------------------------------------------------------------------
# real MCP client
# ---------------------------------------------------------------------------


def _mcp_client(base_url: str, token: str, employee_id: int) -> httpx2.AsyncClient:
    """A real HTTP client carrying only the trusted transport headers."""

    return httpx2.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Employee-Id": str(employee_id),
        },
        timeout=httpx2.Timeout(
            MCP_TIMEOUT_SECONDS, read=MCP_SSE_READ_TIMEOUT_SECONDS
        ),
    )


@contextlib.asynccontextmanager
async def _open_session(http_client: httpx2.AsyncClient, base_url: str):
    """Open the official SDK Streamable HTTP session over the real client."""

    mcp_url = base_url.rstrip("/") + "/mcp/"
    async with streamable_http_client(
        mcp_url, http_client=http_client
    ) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


async def _client_observation(
    base_url: str,
    token: str,
    requester_id: int,
    outsider_id: int,
    task_id: str,
    expected_result_ref: str,
    expected_evidence_ref: str,
    secrets_to_scan: tuple[bytes, ...],
) -> None:
    """Read the completed task through the real MCP client, then deny an outsider."""

    async with _mcp_client(base_url, token, requester_id) as http_client:
        async with _open_session(http_client, base_url) as session:
            tools = (await session.list_tools()).tools
            names = {tool.name for tool in tools}
            assert {
                "governance.get_task",
                "governance.get_result",
            } <= names, sorted(names)

            task_call = await session.call_tool(
                "governance.get_task", {"task_id": task_id}
            )
            assert task_call.is_error is False, tool_text(task_call)
            task_text = tool_text(task_call)
            task_document = tool_json(task_call)
            assert task_document["task_id"] == task_id
            assert task_document["state"] == "succeeded"
            assert task_document["status"] == "succeeded"
            assert task_document["execution_mode"] == "managed_execution"
            assert task_document["is_terminal"] is True
            assert task_document["completed_at"]

            result_call = await session.call_tool(
                "governance.get_result", {"task_id": task_id}
            )
            assert result_call.is_error is False, tool_text(result_call)
            result_text = tool_text(result_call)
            result_document = tool_json(result_call)
            assert result_document["task_id"] == task_id
            assert result_document["state"] == "succeeded"
            assert result_document["note"] == "terminal"
            payload = result_document["result"]
            assert set(payload) == {
                "outcome",
                "result_ref",
                "evidence_refs",
                "verification",
                "stop_confirmed",
                "side_effects_reconciled",
            }
            assert payload["outcome"] == "passed"
            assert payload["result_ref"] == expected_result_ref
            assert payload["evidence_refs"] == [expected_evidence_ref]
            assert payload["verification"]["result"] == "passed"
            assert payload["side_effects_reconciled"] is False

            # The response is a whitelisted projection: the prompt, the
            # internal token, the task egress token and the provider key are
            # never part of it.
            for text in (task_text, result_text):
                for secret in secrets_to_scan:
                    assert not _mentions(text, secret), (
                        "the MCP response leaked a prompt/token/key"
                    )

    async with _mcp_client(base_url, token, outsider_id) as outsider_client:
        async with _open_session(outsider_client, base_url) as session:
            denied_task = await session.call_tool(
                "governance.get_task", {"task_id": task_id}
            )
            denied_result = await session.call_tool(
                "governance.get_result", {"task_id": task_id}
            )
            for denied in (denied_task, denied_result):
                assert denied.is_error is True
                assert tool_error_code(denied) == "task_unauthorized_actor"


# ---------------------------------------------------------------------------
# live body
# ---------------------------------------------------------------------------


@live_only
def test_real_mcp_client_reads_managed_artifact_result(
    live_environment: LiveEnvironment,
    test_db_url: str,
    _schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive one real managed artifact task and read it over real MCP/TCP."""

    del _schema
    environment = live_environment
    assert environment.task_root is not None
    assert environment.egress_socket is not None
    assert environment.runtime_root is not None
    assert environment.provider_key_file is not None
    assert environment.model is not None

    # This test only uses the provisioned runtime/task roots.  A personal or
    # default Hermes profile is never a valid source for either path.
    home = Path.home()
    for label, path in (
        ("runtime root", environment.runtime_root),
        ("task root", environment.task_root),
    ):
        assert path != home, f"the {label} must not be the user home"
        assert home not in path.parents, (
            f"the {label} must not live under the user home"
        )

    engine = create_engine(test_db_url)
    session = Session(engine)
    reset_task_state(engine)
    # The hand-built world must not depend on fixture residue: seed the
    # catalog explicitly after the truncate.
    seed_permission_catalog(session)
    session.commit()
    world = _build_world(session)
    service = _service_principal(session, world)
    # Capture the directory ids while the session is open: the live server
    # reads the committed rows through its own session, and the test-side
    # session is closed before the MCP client calls.
    requester_id = world.org.employee.id
    outsider_id = world.org.auditor.id
    owner_id = world.org.manager.id

    token = E2E_INTERNAL_TOKEN
    monkeypatch.setenv("AJAIMGR_INTERNAL_TOKEN", token)
    from app.api.app import create_app

    app = create_app()

    unit: str | None = None
    task_dir: Path | None = None
    port: int | None = None
    try:
        with _live_server(app) as server:
            port = server.port
            with _real_backend(environment, label="e2e") as backend:
                try:
                    _require_verified_runtime(backend)
                    dispatcher = RuntimeDispatcher(
                        backend.adapter,
                        DispatcherConfig(
                            worker_id=WORKER_ID,
                            poll_interval=0.5,
                            max_status_observations=None,
                        ),
                        service_principal=service,
                        session=session,
                    )
                    suffix = secrets.token_hex(4)
                    task = submit_managed(
                        session,
                        world,
                        key=f"idem_e2e_real_client_{suffix}",
                        goal=_artifact_goal(ARTIFACT_INSTRUCTION),
                        max_runtime_seconds=REAL_TASK_MAX_RUNTIME_SECONDS,
                    )
                    session.commit()
                    unit = _unit_for_task(session, task)
                    task_id = task.task_id
                    task_dir = environment.task_root / unit

                    capture = _ChildCapture(task_dir)
                    capture.start()
                    result = dispatcher.run_once()
                    session.refresh(task)
                    capture.stop()

                    attempt = _snapshot_attempt(
                        task=task,
                        unit=unit,
                        task_dir=task_dir,
                        result=result,
                        capture=capture,
                    )

                    # The dispatcher may only reach ``verifying``.  A task or
                    # runtime that already reports success before the
                    # independent verifier is a false success.
                    assert _false_success(attempt) is None
                    assert attempt.task_state == "verifying"
                    assert attempt.dispatcher_outcome == "verifying"
                    assert attempt.artifact_bytes == ARTIFACT_BYTES
                    assert attempt.artifact_digest == ARTIFACT_DIGEST
                    assert attempt.result_document["outcome"] == "completed"
                    assert (
                        attempt.result_document.get("verification") or {}
                    ).get("result") == "pending"
                    assert (
                        attempt.result_document.get("side_effects_reconciled")
                        is not True
                    )

                    # A launched run must expose the full finished-run file set,
                    # a readable journal and a captured child; otherwise the
                    # key's absence cannot be proven.
                    assert attempt.launched
                    assert attempt.captured, attempt.capture_error
                    assert attempt.journal_ok
                    assert attempt.journal.strip()
                    _require_finished_files(attempt.files)

                    key = backend.provider_key
                    assert hmac.compare_digest(
                        backend.broker.config.provider_key, key
                    ), "the broker is not configured with the real key"
                    assert not _mentions(repr(backend.config), key), (
                        "the adapter configuration carries the provider key"
                    )
                    for relative, text in attempt.files.items():
                        assert not _mentions(text, key), (
                            f"the provider key leaked into the task file {relative}"
                        )
                    assert not _mentions(
                        json.dumps(attempt.result_document), key
                    ), "the provider key leaked into the persisted task result"
                    assert not _mentions(attempt.journal, key), (
                        "the provider key leaked into the unit journal"
                    )
                    for argument in attempt.child_argv:
                        assert not _mentions(argument, key), (
                            "the provider key leaked into the Hermes child argv"
                        )
                    assert not _mentions(attempt.child_environ, key), (
                        "the provider key leaked into the Hermes child environment"
                    )

                    # Positive control: the sandbox child carries the
                    # short-lived task token, not the provider key, and uses
                    # the sandbox profile rather than a personal Hermes home.
                    assert "HERMES_HOME=/task/profile" in attempt.child_environ
                    assert f"HOME={home}" not in attempt.child_environ
                    egress_token = attempt.files.get("egress.token", "").strip()
                    assert egress_token.startswith("egt."), (
                        "the task egress token is missing"
                    )
                    assert _mentions(
                        attempt.child_environ, egress_token.encode()
                    ), "the task token never reached the child environment"

                    run = _RealArtifactRun(
                        environment=environment,
                        backend=backend,
                        provider_key=key,
                        task_id=task_id,
                        unit=unit,
                        task_dir=task_dir,
                        handle=task.runtime_task_id or "",
                        task_state=task.state,
                        result_document=dict(task.result or {}),
                        artifact_digest=attempt.artifact_digest or "",
                        artifact_bytes=attempt.artifact_bytes or b"",
                        owner_employee_id=owner_id,
                        attempts=(attempt,),
                    )
                    completed = _complete_artifact_run(engine, run)
                    assert completed.final_state == "succeeded"
                    assert completed.final_outcome == "passed"
                    assert completed.all_criteria_passed is True

                    actual_hex = hashlib.sha256(
                        completed.artifact_bytes
                    ).hexdigest()
                    assert completed.artifact_digest == f"sha256:{actual_hex}"
                    expected_result_ref = f"result_{actual_hex[:32]}"
                    expected_evidence_ref = (
                        f"evidence:hermes_artifact_{actual_hex[:32]}"
                    )

                    # Release the test-side transaction before the live server
                    # reads the committed row through its own session.
                    session.close()

                    anyio.run(
                        _client_observation,
                        server.base_url,
                        token,
                        requester_id,
                        outsider_id,
                        task_id,
                        expected_result_ref,
                        expected_evidence_ref,
                        (
                            key,
                            token.encode("utf-8"),
                            ARTIFACT_INSTRUCTION.encode("utf-8"),
                            egress_token.encode("utf-8"),
                        ),
                    )
                finally:
                    if unit is not None and task_dir is not None:
                        _cleanup_unit(
                            backend,
                            unit=unit,
                            task_dir=task_dir,
                            task_root=environment.task_root,
                        )
                        assert not task_dir.exists(), (
                            "a managed task directory survived the live cleanup"
                        )
                        _wait_for_unit_gone(unit, timeout=30)
            assert not environment.egress_socket.exists(), (
                "the broker socket survived the live cleanup"
            )
        assert port is not None
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1.0)
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# DB-free checks (collection and these bodies never touch PostgreSQL or sudo)
# ---------------------------------------------------------------------------


def test_e2e_gate_names_the_required_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(E2E_LIVE_ENV, raising=False)
    monkeypatch.delenv("AGENTMGR_HERMES_LIVE", raising=False)
    monkeypatch.delenv("AGENTMGR_HERMES_SUDO", raising=False)

    reason = _gate_reason(read_environment())
    assert reason is not None
    assert E2E_LIVE_ENV in reason
    assert "AGENTMGR_HERMES_LIVE" in reason
    assert "AGENTMGR_HERMES_SUDO" in reason


def test_artifact_digest_is_the_exact_ok_artifact() -> None:
    assert ARTIFACT_BYTES == b"OK"
    assert ARTIFACT_DIGEST == "sha256:" + hashlib.sha256(b"OK").hexdigest()
    assert CRITERION_ID
    assert VERIFIER_ID
    assert VERIFIER_VERSION


def test_live_server_harness_uses_a_real_tcp_socket() -> None:
    """The harness must serve a real listener, not an ASGI transport."""

    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    with _live_server(app) as server:
        response = httpx2.get(server.base_url + "/health", timeout=5.0)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        port = server.port

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1.0)
