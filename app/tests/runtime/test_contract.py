"""Contract-level tests: no vendor objects cross the B3 boundary."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from app.runtime import contract


class _StubAdapter:
    def capabilities(self, *, probe: bool = False):
        raise NotImplementedError

    def submit_task(
        self, task_id, prompt, idempotency_key, grant, instructions=None
    ):
        raise NotImplementedError

    def find_by_idempotency_key(
        self, task_id, grant_id, idempotency_key, payload_hash=None
    ):
        raise NotImplementedError

    def get_status(self, runtime_task_id, *, task_id=None):
        raise NotImplementedError

    def poll_events(
        self,
        runtime_task_id,
        cursor=None,
        max_events=100,
        timeout=30.0,
        *,
        task_id=None,
    ):
        raise NotImplementedError

    def cancel_task(
        self,
        runtime_task_id,
        *,
        reason=None,
        idempotency_key=None,
        task_id=None,
        poll_timeout=None,
        poll_interval=None,
    ):
        raise NotImplementedError

    def get_result(self, runtime_task_id, *, task_id=None):
        raise NotImplementedError

    def provide_input(
        self,
        runtime_task_id,
        input_request_id,
        payload_hash,
        payload,
    ):
        raise NotImplementedError

    def health(self):
        raise NotImplementedError


def test_runtime_adapter_protocol_is_structural() -> None:
    adapter = _StubAdapter()
    assert isinstance(adapter, contract.RuntimeAdapter)

    class MissingCancel:
        pass

    assert not isinstance(MissingCancel(), contract.RuntimeAdapter)


def test_contract_constants_are_frozen() -> None:
    assert contract.ADAPTER_CONTRACT_VERSION == "1.0.0"
    assert contract.BOUNDARY == "b3_runtime_adapter"


def test_capability_status_values() -> None:
    assert {item.value for item in contract.CapabilityStatus} == {
        "verified",
        "partially_verified",
        "unverified",
    }


def test_runtime_state_values_match_frozen_contract() -> None:
    assert {item.value for item in contract.RuntimeState} == {
        "accepted",
        "running",
        "waiting_input",
        "completed",
        "failed",
        "stopped",
        "timed_out",
        "unknown",
        "not_found",
    }


def test_submit_cancel_result_and_health_outcomes() -> None:
    assert {item.value for item in contract.SubmitOutcome} == {
        "accepted",
        "rejected",
        "unsupported",
        "timeout",
        "unknown",
        "conflict",
    }
    assert {item.value for item in contract.LookupOutcome} == {
        "found",
        "not_found",
        "conflict",
        "unknown",
    }
    assert {item.value for item in contract.CancelOutcome} == {
        "requested",
        "already_terminal",
        "stopped",
        "unsupported",
        "unknown",
    }
    assert {item.value for item in contract.ResultOutcome} == {
        "succeeded",
        "partial_success",
        "failed",
        "cancelled",
        "timed_out",
        "needs_human",
        "unknown",
    }
    assert {item.value for item in contract.HealthStatus} == {
        "healthy",
        "degraded",
        "unavailable",
    }


def test_error_codes_match_contract_section_310() -> None:
    assert {item.value for item in contract.ErrorCode} == {
        "unauthorized",
        "forbidden",
        "not_found",
        "conflict",
        "invalid_request",
        "unsupported",
        "rate_limited",
        "timeout",
        "unavailable",
        "internal",
        "unknown",
    }


def test_capability_snapshot_lookup() -> None:
    snapshot = contract.RuntimeCapabilities(
        adapter_contract_version=contract.ADAPTER_CONTRACT_VERSION,
        boundary=contract.BOUNDARY,
        runtime_id="rt_test_0001",
        runtime_type=contract.RuntimeType.HERMES,
        runtime_version=None,
        version_source=contract.VersionSource.UNKNOWN,
        capabilities=(
            contract.Capability(
                name=contract.RuntimeCapability.TASK_SUBMIT.value,
                version="1.0.0",
                status=contract.CapabilityStatus.VERIFIED,
                evidence_ref="docs/example.md",
            ),
            contract.Capability(
                name=contract.RuntimeCapability.TASK_INPUT_PROVIDE.value,
                version="1.0.0",
                status=contract.CapabilityStatus.UNVERIFIED,
                evidence_ref=None,
                limitations="not supported",
            ),
        ),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert snapshot.capability(contract.RuntimeCapability.TASK_SUBMIT) is not None
    assert snapshot.capability("task.input.provide").status is (
        contract.CapabilityStatus.UNVERIFIED
    )
    assert snapshot.capability("runtime.isolation") is None


def test_runtime_grant_projection_is_server_resolved_shape() -> None:
    grant = contract.RuntimeGrant(
        grant_id="grnt_test_0001",
        task_id="task_test_0001",
        task_revision=3,
        execution_mode="managed_execution",
        capabilities=("expense.read@1.0.0",),
        resource_scope={
            "scope_type": "self",
            "resource_ids": ["res_0001"],
            "data_classifications": ["internal"],
        },
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert grant.is_expired(now=datetime(2026, 1, 1, tzinfo=UTC)) is False
    assert grant.is_expired(now=datetime(2028, 1, 1, tzinfo=UTC)) is True
    assert contract.RuntimeGrant(
        grant_id="grnt_test_0002",
        task_id="task_test_0001",
        task_revision=1,
        execution_mode="managed_execution",
        capabilities=("expense.read@1.0.0",),
        resource_scope={"scope_type": "self"},
        expires_at=None,
    ).is_expired() is True


def test_lookup_result_is_neutral_and_serialisable() -> None:
    result = contract.LookupResult(
        outcome=contract.LookupOutcome.FOUND,
        runtime_task_id="rtask_0001",
        payload_hash="sha256:" + "0" * 64,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.outcome is contract.LookupOutcome.FOUND
    assert "hermes" not in repr(result).lower()


def test_runtime_event_may_omit_time_without_fabrication() -> None:
    event = contract.RuntimeEvent(
        event_id="1",
        occurred_at=None,
        type="progress",
        payload={},
        redacted=False,
    )
    assert event.occurred_at is None
    assert event.redacted is False


def test_runtime_error_info_is_safe_and_serialisable() -> None:
    info = contract.RuntimeErrorInfo(
        code=contract.ErrorCode.TIMEOUT,
        message="runtime request timed out",
        context={"operation": "submit"},
    )
    assert info.to_dict() == {
        "code": "timeout",
        "message": "runtime request timed out",
        "context": {"operation": "submit"},
    }


def test_no_hermes_specific_export_crosses_the_contract() -> None:
    exported = set(contract.__all__)
    assert all("hermes" not in name.lower() for name in exported)
    assert "HermesRuntimeAdapter" not in exported
    # Every dataclass field uses a neutral contract type.
    for name in exported:
        value = getattr(contract, name)
        if dataclasses.is_dataclass(value):
            for item in dataclasses.fields(value):
                assert "hermes" not in item.name.lower()
