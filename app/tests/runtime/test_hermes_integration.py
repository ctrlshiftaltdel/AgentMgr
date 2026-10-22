"""Real-runtime integration tests for the Hermes adapter.

Skipped by default.  Run only against the approved isolated test host after
exporting both environment variables:

```bash
export HERMES_API_URL='http://<test-host>:<api-port>'
export HERMES_API_KEY='<api-key>'
uv run pytest app/tests/runtime/test_hermes_integration.py -q
```

No credential, host name, IP address or profile path is stored in this file or
in the repository.  The optional stop scenario is additionally gated by
``HERMES_INTEGRATION_STOP=1`` because it creates a second run and is not part of
the minimal compatibility check.

The lifecycle scenarios require the explicit **test-only** unverified managed
execution opt-in
(``HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION=1``) so the compatibility mapping
can be exercised before Issue #82. Without it, lifecycle tests are skipped and a
separate test proves that the default configuration refuses such a submission.
"""

from __future__ import annotations

import dataclasses
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.runtime.contract import (
    CancelOutcome,
    HealthStatus,
    ResultOutcome,
    RuntimeGrant,
    RuntimeState,
    SubmitOutcome,
)
from app.runtime.hermes import HermesConfig, HermesRuntimeAdapter

HERMES_API_URL = os.environ.get("HERMES_API_URL")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY")
ALLOW_UNVERIFIED = (
    os.environ.get("HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION") == "1"
)

pytestmark = pytest.mark.skipif(
    not (HERMES_API_URL and HERMES_API_KEY),
    reason="HERMES_API_URL/HERMES_API_KEY not set; real-runtime test skipped",
)


def _config(*, allow_unverified: bool) -> HermesConfig:
    # from_env reads the two variables; the test must never print their values.
    return dataclasses.replace(
        HermesConfig.from_env(),
        allow_unverified_managed_execution=allow_unverified,
    )


def _adapter(*, allow_unverified: bool = ALLOW_UNVERIFIED) -> HermesRuntimeAdapter:
    return HermesRuntimeAdapter(_config(allow_unverified=allow_unverified))


def _grant(task_id: str) -> RuntimeGrant:
    return RuntimeGrant(
        grant_id=f"grnt_{uuid.uuid4().hex[:16]}",
        task_id=task_id,
        task_revision=1,
        execution_mode="managed_execution",
        capabilities=("expense.read@1.0.0",),
        resource_scope={
            "scope_type": "self",
            "resource_ids": ["res_it_0001"],
            "data_classifications": ["internal"],
        },
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _wait_terminal(
    adapter: HermesRuntimeAdapter,
    run_id: str,
    *,
    deadline_seconds: float = 180.0,
):
    deadline = time.monotonic() + deadline_seconds
    last = adapter.get_status(run_id)
    while time.monotonic() < deadline:
        last = adapter.get_status(run_id)
        if last.state in {
            RuntimeState.COMPLETED,
            RuntimeState.FAILED,
            RuntimeState.STOPPED,
            RuntimeState.TIMED_OUT,
            RuntimeState.NOT_FOUND,
        }:
            return last
        if last.state is RuntimeState.UNKNOWN:
            # Keep polling briefly: an isolated status blip is not terminal.
            pass
        time.sleep(1.0)
    return last


def test_health_against_real_runtime() -> None:
    adapter = _adapter()
    result = adapter.health()
    assert result.status is HealthStatus.HEALTHY
    assert result.details.get("api_ok") is True
    assert result.details.get("auth_ok") is True


def test_default_config_refuses_unverified_managed_execution() -> None:
    adapter = _adapter(allow_unverified=False)
    task_id = _unique("task_refuse")
    result = adapter.submit_task(
        task_id,
        "Reply with exactly OK and nothing else.",
        _unique("idem_refuse"),
        _grant(task_id),
    )
    assert result.outcome is SubmitOutcome.UNSUPPORTED
    assert result.runtime_task_id is None


@pytest.mark.skipif(
    not ALLOW_UNVERIFIED,
    reason="set HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION=1 for the lifecycle scenario",
)
def test_short_run_completes_and_replays() -> None:
    adapter = _adapter()
    task_id = _unique("task_it")
    idempotency_key = _unique("idem_it")
    grant = _grant(task_id)

    first = adapter.submit_task(
        task_id,
        "Reply with exactly OK and nothing else.",
        idempotency_key,
        grant,
    )
    assert first.outcome is SubmitOutcome.ACCEPTED
    assert first.runtime_task_id
    assert first.replayed is False

    replay = adapter.submit_task(
        task_id,
        "Reply with exactly OK and nothing else.",
        idempotency_key,
        grant,
    )
    assert replay.outcome is SubmitOutcome.ACCEPTED
    assert replay.runtime_task_id == first.runtime_task_id
    assert replay.replayed is True

    final = _wait_terminal(adapter, first.runtime_task_id)
    assert final.state is RuntimeState.COMPLETED, final.external_state
    result = adapter.get_result(first.runtime_task_id, task_id=task_id)
    # No independent evidence refs are returned, so the adapter reports a
    # partial success rather than claiming verification.
    assert result.outcome is ResultOutcome.PARTIAL_SUCCESS
    assert result.structured_result is False
    assert result.output is not None
    assert "OK" in result.output.upper()


@pytest.mark.skipif(
    not ALLOW_UNVERIFIED,
    reason="set HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION=1 for the lifecycle scenario",
)
@pytest.mark.skipif(
    os.environ.get("HERMES_INTEGRATION_STOP") != "1",
    reason="set HERMES_INTEGRATION_STOP=1 to run the stop scenario",
)
def test_stop_is_requested_then_confirmed() -> None:
    adapter = _adapter()
    task_id = _unique("task_stop")
    idempotency_key = _unique("idem_stop")
    grant = _grant(task_id)
    submitted = adapter.submit_task(
        task_id,
        "Count slowly from 1 to 200, printing each number on its own line.",
        idempotency_key,
        grant,
    )
    assert submitted.outcome is SubmitOutcome.ACCEPTED
    assert submitted.runtime_task_id

    cancel = adapter.cancel_task(
        submitted.runtime_task_id,
        task_id=task_id,
        idempotency_key=_unique("idem_cancel"),
        poll_timeout=30.0,
        poll_interval=0.5,
    )
    assert cancel.outcome in {
        CancelOutcome.STOPPED,
        CancelOutcome.ALREADY_TERMINAL,
        CancelOutcome.REQUESTED,
    }
    if cancel.outcome is CancelOutcome.STOPPED:
        assert cancel.stop_confirmed is True
    elif cancel.outcome is CancelOutcome.ALREADY_TERMINAL:
        # A normal completion is terminal but not a confirmed stop.
        assert cancel.stop_confirmed is False
    else:
        # A request is not a stop; never assert cancelled in that case.
        assert cancel.stop_confirmed is False
