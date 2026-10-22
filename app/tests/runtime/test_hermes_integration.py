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
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from app.runtime.contract import (
    CancelOutcome,
    HealthStatus,
    ResultOutcome,
    RuntimeState,
    SubmitOutcome,
)
from app.runtime.hermes import HermesConfig, HermesRuntimeAdapter

HERMES_API_URL = os.environ.get("HERMES_API_URL")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY")

pytestmark = pytest.mark.skipif(
    not (HERMES_API_URL and HERMES_API_KEY),
    reason="HERMES_API_URL/HERMES_API_KEY not set; real-runtime test skipped",
)


def _adapter() -> HermesRuntimeAdapter:
    # from_env reads the two variables; the test must never print their values.
    return HermesRuntimeAdapter(HermesConfig.from_env())


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
    assert result.status is not HealthStatus.UNAVAILABLE
    assert result.details.get("api_ok") is True
    assert result.details.get("auth_ok") is True


def test_short_run_completes_and_replays() -> None:
    adapter = _adapter()
    task_id = _unique("task_it")
    idempotency_key = _unique("idem_it")

    first = adapter.submit_task(
        task_id,
        "Reply with exactly OK and nothing else.",
        idempotency_key,
    )
    assert first.outcome is SubmitOutcome.ACCEPTED
    assert first.runtime_task_id
    assert first.replayed is False

    replay = adapter.submit_task(
        task_id,
        "Reply with exactly OK and nothing else.",
        idempotency_key,
    )
    assert replay.outcome is SubmitOutcome.ACCEPTED
    assert replay.runtime_task_id == first.runtime_task_id
    assert replay.replayed is True

    final = _wait_terminal(adapter, first.runtime_task_id)
    assert final.state is RuntimeState.COMPLETED, final.external_state
    result = adapter.get_result(first.runtime_task_id)
    assert result.outcome is ResultOutcome.SUCCEEDED
    assert result.structured_result is False
    assert result.output is not None
    assert "OK" in result.output.upper()


@pytest.mark.skipif(
    os.environ.get("HERMES_INTEGRATION_STOP") != "1",
    reason="set HERMES_INTEGRATION_STOP=1 to run the stop scenario",
)
def test_stop_is_requested_then_confirmed() -> None:
    adapter = _adapter()
    task_id = _unique("task_stop")
    idempotency_key = _unique("idem_stop")
    submitted = adapter.submit_task(
        task_id,
        "Count slowly from 1 to 200, printing each number on its own line.",
        idempotency_key,
    )
    assert submitted.outcome is SubmitOutcome.ACCEPTED
    assert submitted.runtime_task_id

    cancel = adapter.cancel_task(
        submitted.runtime_task_id,
        poll_timeout=30.0,
        poll_interval=0.5,
    )
    assert cancel.outcome in {
        CancelOutcome.STOPPED,
        CancelOutcome.ALREADY_TERMINAL,
        CancelOutcome.REQUESTED,
    }
    if cancel.outcome is CancelOutcome.REQUESTED:
        # A request is not a stop; never assert cancelled in that case.
        assert cancel.stop_confirmed is False
    else:
        assert cancel.stop_confirmed is True
