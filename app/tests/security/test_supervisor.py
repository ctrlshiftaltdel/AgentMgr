"""Supervisor lifecycle tests (#82).

Everything here runs against in-memory fakes: no sudo, systemd unit, host path,
database or network.  The tests pin the launch/stop/cleanup state machine of
design §11, the effective-TTL binding between the Grant and ``RuntimeMaxSec``,
and the separation of ``process_gone``, ``stop_confirmed`` and
``never_started``.  They also assert that the bearer token never reaches an
audit event or an exception.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.security.boundary import (
    BYPASSABLE_BOUNDARY,
    CONTRACT_VERSION,
    DEFERRED_BOUNDARY,
    ENFORCED_BOUNDARY,
    PRODUCT_CLAIM,
    SUBSTRATE,
    BoundaryClaim,
    GrantStatus,
    LaunchHandle,
    ManagedTaskSpec,
    ResourceScope,
    unit_name_for_task,
)
from app.security.credentials import (
    CredentialIssuer,
    CredentialValidator,
    InMemoryRevocationStore,
)
from app.security.errors import SecurityError
from app.security.supervisor import (
    AUDIT_EVENT_CLEANUP,
    AUDIT_EVENT_LAUNCH,
    AUDIT_EVENT_STOP,
    AUDIT_EVENT_STOP_UNCONFIRMED,
    ManagedExecutionSupervisor,
    SupervisorConfig,
)
from app.security.systemd import HelperSystemdOps, build_systemd_run_argv

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
KID = "k1"
KEY = bytes(range(32))
TASK_ID = "task_synthetic_0001"
OTHER_TASK_ID = "task_synthetic_0002"
GRANT_ID = "grnt_synthetic_0001"
SUBJECT_ID = "prin_synthetic_0001"
RESOURCE_ID = "res_synthetic_0001"
CAPABILITY = "resource.read@1.0.0"
UNIT = unit_name_for_task(TASK_ID)


def _clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


class FakeTime:
    """Controllable wall clock, monotonic clock and sleep hook."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.mono = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        """Simulate wall-clock and monotonic time passing (slow I/O)."""

        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def advance_monotonic(self, seconds: float) -> None:
        self.mono += seconds


def _scope() -> ResourceScope:
    return ResourceScope(
        scope_type="self",
        resource_ids=(RESOURCE_ID,),
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=(),
    )


def _spec(**overrides: object) -> ManagedTaskSpec:
    value: dict[str, object] = {
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
            "resource_ids": [RESOURCE_ID],
            "connector_ids": [],
            "data_classifications": ["internal"],
            "exclude_resource_ids": [],
        },
        "backend": "file",
    }
    value.update(overrides)
    return ManagedTaskSpec.from_mapping(value)


def _grant(**overrides: object) -> GrantStatus:
    value: dict[str, object] = {
        "grant_id": GRANT_ID,
        "task_id": TASK_ID,
        "subject_id": SUBJECT_ID,
        "execution_mode": "managed_execution",
        "status": "active",
        "usable": True,
        "expires_at": NOW + timedelta(seconds=120),
        "revocation_reason": None,
        "capabilities": (CAPABILITY,),
        "resource_scope": _scope(),
    }
    value.update(overrides)
    return GrantStatus(**value)  # type: ignore[arg-type]


def _boundary_claim() -> BoundaryClaim:
    return BoundaryClaim(
        contract_version=CONTRACT_VERSION,
        substrate=SUBSTRATE,
        network_profile="private_netns",
        product_claim=PRODUCT_CLAIM,
        enforced=ENFORCED_BOUNDARY,
        bypassable=BYPASSABLE_BOUNDARY,
        deferred=DEFERRED_BOUNDARY,
    )


def _handle(*, expires_at: datetime = NOW + timedelta(seconds=60)) -> LaunchHandle:
    return LaunchHandle(
        unit=UNIT,
        task_id=TASK_ID,
        grant_id=GRANT_ID,
        jti="cred_" + "a" * 32,
        expires_at=expires_at,
        network_profile="private_netns",
        boundary_claim=_boundary_claim(),
        launched_at=NOW,
    )


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%a %Y-%m-%d %H:%M:%S UTC")


def _show(
    *,
    active_state: str = "active",
    sub_state: str = "running",
    main_pid: str = "4242",
    result: str = "success",
    exec_main_status: str = "0",
    load_state: str = "loaded",
    enter: datetime | None = None,
    start: datetime | None = None,
    exit: datetime | None = None,
) -> dict[str, str]:
    return {
        "LoadState": load_state,
        "ActiveState": active_state,
        "SubState": sub_state,
        "MainPID": main_pid,
        "Result": result,
        "ExecMainStatus": exec_main_status,
        "ActiveEnterTimestamp": _timestamp(enter) if enter else "n/a",
        "ExecMainStartTimestamp": _timestamp(start) if start else "n/a",
        "ExecMainExitTimestamp": _timestamp(exit) if exit else "n/a",
    }


def _show_activating() -> dict[str, str]:
    return _show(
        active_state="activating", sub_state="start", main_pid="0", enter=None
    )


def _show_active() -> dict[str, str]:
    return _show(
        main_pid="4242",
        enter=NOW - timedelta(seconds=2),
        start=NOW - timedelta(seconds=2),
    )


def _show_deactivating() -> dict[str, str]:
    return _show(
        active_state="deactivating",
        sub_state="stop",
        main_pid="4242",
        enter=NOW - timedelta(seconds=2),
        start=NOW - timedelta(seconds=2),
    )


def _show_completed(exit_at: datetime) -> dict[str, str]:
    return _show(
        active_state="inactive",
        sub_state="dead",
        main_pid="0",
        result="success",
        exec_main_status="0",
        enter=NOW - timedelta(seconds=2),
        start=NOW - timedelta(seconds=2),
        exit=exit_at,
    )


def _show_active_exited(exit_at: datetime) -> dict[str, str]:
    """A successfully finished unit retained by ``RemainAfterExit=yes``."""

    return _show(
        active_state="active",
        sub_state="exited",
        main_pid="0",
        result="success",
        exec_main_status="0",
        enter=NOW - timedelta(seconds=2),
        start=NOW - timedelta(seconds=2),
        exit=exit_at,
    )


def _show_failed(exit_at: datetime) -> dict[str, str]:
    return _show(
        active_state="failed",
        sub_state="failed",
        main_pid="0",
        result="exit-code",
        exec_main_status="1",
        enter=NOW - timedelta(seconds=2),
        start=NOW - timedelta(seconds=2),
        exit=exit_at,
    )


def _show_timed_out(exit_at: datetime) -> dict[str, str]:
    return _show(
        active_state="failed",
        sub_state="failed",
        main_pid="0",
        result="timeout",
        exec_main_status="0",
        enter=NOW - timedelta(seconds=2),
        start=NOW - timedelta(seconds=2),
        exit=exit_at,
    )


def _show_not_found() -> dict[str, str]:
    return _show(
        active_state="inactive",
        sub_state="dead",
        main_pid="0",
        load_state="not-found",
        enter=None,
        start=None,
        exit=None,
    )


def _show_unknown() -> dict[str, str]:
    return _show(
        active_state="unknown",
        sub_state="unknown",
        main_pid="0",
        enter=None,
        start=None,
        exit=None,
    )


class FakeOps:
    """In-memory ``SystemdOps`` that records every call and replays a show plan."""

    def __init__(self) -> None:
        self.launch_calls: list[object] = []
        self.spec_texts: list[str] = []
        self.credentials: list[str] = []
        self.stop_calls: list[str] = []
        self.terminate_calls: list[str] = []
        self.kill_calls: list[str] = []
        self.show_calls: list[str] = []
        self.reset_failed_calls: list[str] = []
        self.remove_sources_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        self.cleaned = False
        self.show_plan: list[Mapping[str, str] | Exception] = []
        self.default_show: Mapping[str, str] = _show_not_found()
        self.show_hook: Callable[[], None] | None = None
        self.show_error: Exception | None = None
        self.launch_error: Exception | None = None
        self.launch_error_with_token = False
        self.stop_error: Exception | None = None
        self.terminate_error: Exception | None = None

    def launch(self, config: object, *, spec_text: str, credential_text: str) -> None:
        self.launch_calls.append(config)
        self.spec_texts.append(spec_text)
        self.credentials.append(credential_text)
        if self.launch_error_with_token:
            raise RuntimeError(f"systemd-run refused {credential_text}")
        if self.launch_error is not None:
            raise self.launch_error

    def stop(self, unit: str) -> None:
        self.stop_calls.append(unit)
        if self.stop_error is not None:
            raise self.stop_error

    def terminate(self, unit: str) -> None:
        self.terminate_calls.append(unit)
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self, unit: str) -> None:
        self.kill_calls.append(unit)

    def show(self, unit: str) -> Mapping[str, str]:
        self.show_calls.append(unit)
        if self.show_hook is not None:
            self.show_hook()
        if self.show_error is not None:
            raise self.show_error
        if self.show_plan:
            item = self.show_plan.pop(0)
            if isinstance(item, Exception):
                raise item
            return dict(item)
        return dict(self.default_show)

    def reset_failed(self, unit: str) -> None:
        self.reset_failed_calls.append(unit)

    def remove_sources(self, unit: str) -> None:
        self.remove_sources_calls.append(unit)

    def cleanup(self, unit: str) -> None:
        self.cleanup_calls.append(unit)
        self.cleaned = True


class FakeProvider:
    """Live Grant provider whose answer a test can mutate between calls."""

    def __init__(self, grant: GrantStatus | None, *, ignore_lookup: bool = False):
        self.grant = grant
        self.ignore_lookup = ignore_lookup
        self.calls = 0

    def status(self, grant_id: str, *, task_id: str) -> GrantStatus | None:
        self.calls += 1
        if self.ignore_lookup:
            return self.grant
        if self.grant is None or self.grant.grant_id != grant_id:
            return None
        return self.grant


class RecordingSink:
    """Audit sink that keeps a copy of every event and can be made to fail."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.fail = False

    def record(self, event: object) -> None:
        if self.fail:
            raise RuntimeError("audit sink unavailable")
        assert isinstance(event, dict)
        self.events.append(dict(event))


class FailingRevocationStore(InMemoryRevocationStore):
    """Store that never accepts a revocation write."""

    def __init__(self) -> None:
        super().__init__()
        self.revoke_calls: list[tuple[str, str]] = []

    def revoke(self, jti: str, *, reason: str, revoked_at: datetime) -> None:
        self.revoke_calls.append((jti, reason))
        raise SecurityError("unknown", "revocation_store_unwritable")


class SlowRevocationStore(InMemoryRevocationStore):
    """Store whose write takes ``seconds`` of wall-clock time."""

    def __init__(self, fake: FakeTime, seconds: float) -> None:
        super().__init__(fake.clock)
        self.fake = fake
        self.seconds = seconds

    def revoke(self, jti: str, *, reason: str, revoked_at: datetime) -> None:
        self.fake.advance(self.seconds)
        super().revoke(jti, reason=reason, revoked_at=revoked_at)


@dataclass(frozen=True)
class _Harness:
    supervisor: ManagedExecutionSupervisor
    ops: FakeOps
    provider: FakeProvider
    revocation: InMemoryRevocationStore
    sink: RecordingSink
    time: FakeTime


def _build(
    tmp_path: Path,
    *,
    ops: FakeOps | None = None,
    provider: FakeProvider | None = None,
    grant: GrantStatus | None = None,
    revocation: InMemoryRevocationStore | None = None,
    time_source: FakeTime | None = None,
    issuer_clock: Callable[[], datetime] | None = None,
) -> _Harness:
    fake_ops = ops if ops is not None else FakeOps()
    live_provider = (
        provider if provider is not None else FakeProvider(grant or _grant())
    )
    fake_time = time_source if time_source is not None else FakeTime(NOW)
    store = (
        revocation
        if revocation is not None
        else InMemoryRevocationStore(fake_time.clock)
    )
    sink = RecordingSink()
    config = SupervisorConfig(
        workload_source=tmp_path / "workload.py",
        gateway_socket_source=tmp_path / "gateway.sock",
        network_profile="private_netns",
        clock=fake_time.clock,
        monotonic=fake_time.monotonic,
        sleep=fake_time.sleep,
    )
    return _Harness(
        supervisor=ManagedExecutionSupervisor(
            config,
            fake_ops,  # type: ignore[arg-type]
            live_provider,
            CredentialIssuer(
                {KID: KEY},
                KID,
                issuer_clock if issuer_clock is not None else fake_time.clock,
            ),
            store,
            sink,
        ),
        ops=fake_ops,
        provider=live_provider,
        revocation=store,
        sink=sink,
        time=fake_time,
    )


def test_launch_rejects_non_managed_expired_revoked_and_task_mismatch(
    tmp_path: Path,
) -> None:
    cases = [
        (
            "execution_mode_mismatch",
            _spec(execution_mode="legacy_shell"),
            _grant(),
        ),
        (
            "execution_mode_mismatch",
            _spec(),
            _grant(execution_mode="legacy_shell"),
        ),
        (
            "grant_expired",
            _spec(),
            _grant(expires_at=NOW - timedelta(seconds=1)),
        ),
        (
            "grant_expired",
            _spec(),
            _grant(expires_at=NOW + timedelta(seconds=0.5)),
        ),
        ("grant_revoked", _spec(), _grant(status="revoked")),
        ("cross_task_denied", _spec(), _grant(task_id=OTHER_TASK_ID)),
    ]

    for expected, spec, grant in cases:
        harness = _build(
            tmp_path,
            grant=grant,
            provider=FakeProvider(grant, ignore_lookup=True),
        )
        with pytest.raises(SecurityError) as caught:
            harness.supervisor.launch(spec)
        assert caught.value.code == expected
        assert harness.ops.launch_calls == []
        # The collision preflight happens before the live Grant read; a spec
        # rejected by validate() makes no privileged call at all.
        expected_shows = 1 if spec.execution_mode == "managed_execution" else 0
        assert len(harness.ops.show_calls) == expected_shows


def test_launch_issues_credential_and_removes_sources(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(
        tmp_path,
        ops=ops,
        grant=_grant(expires_at=NOW + timedelta(seconds=30)),
    )

    handle = harness.supervisor.launch(_spec())

    assert handle.unit == UNIT
    assert handle.task_id == TASK_ID
    assert handle.grant_id == GRANT_ID
    assert handle.network_profile == "private_netns"
    assert handle.boundary_claim.network_profile == "private_netns"
    assert handle.launched_at == NOW

    assert len(ops.launch_calls) == 1
    config = ops.launch_calls[0]
    assert config.network_profile == "private_netns"  # type: ignore[attr-defined]
    # The enforced TTL is the Grant remainder, not the larger spec request.
    assert config.spec.ttl_seconds == 30  # type: ignore[attr-defined]
    assert "--property=RuntimeMaxSec=30s" in build_systemd_run_argv(
        config  # type: ignore[arg-type]
    )
    # Both the systemd argv and the helper/spec payload carry the effective TTL.
    assert json.loads(ops.spec_texts[0])["ttl_seconds"] == 30

    claims = CredentialValidator({KID: KEY}, _clock(NOW)).validate(ops.credentials[0])
    assert claims.task_id == TASK_ID
    assert claims.grant_id == GRANT_ID
    assert claims.subject_id == SUBJECT_ID
    assert claims.exp - claims.iat == 30
    assert handle.jti == claims.jti
    assert handle.expires_at == datetime.fromtimestamp(claims.exp, UTC)
    assert ops.credentials[0] not in repr(handle)

    assert ops.remove_sources_calls == [UNIT]

    launch_events = [
        event
        for event in harness.sink.events
        if event["event"] == AUDIT_EVENT_LAUNCH
    ]
    assert len(launch_events) == 1
    event = launch_events[0]
    assert event["unit"] == UNIT
    assert event["credential_jti"] == claims.jti
    assert event["grant_id"] == GRANT_ID
    assert event["ttl_seconds"] == 30
    assert ops.credentials[0] not in json.dumps(harness.sink.events)


def test_launch_start_timeout_with_unit_present_stops_and_returns_unknown(
    tmp_path: Path,
) -> None:
    fake = FakeTime(NOW)
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_activating(), _show_active()]
    ops.default_show = _show_completed(NOW - timedelta(seconds=1))
    # A single slow ``show`` consumes the whole 10s launch window.
    ops.show_hook = lambda: fake.advance_monotonic(100)
    harness = _build(tmp_path, ops=ops, time_source=fake)

    with pytest.raises(SecurityError) as caught:
        harness.supervisor.launch(_spec())

    assert caught.value.code == "unknown"
    assert ops.stop_calls == [UNIT]
    assert ops.credentials
    assert ops.credentials[0] not in str(caught.value)
    assert ops.remove_sources_calls == [UNIT]
    # Preflight + one deadline-bounded poll + one final inspection.  The old
    # iteration-bound loop would have called show ~101 times.
    assert len(ops.show_calls) == 3


@pytest.mark.parametrize(
    ("show", "state", "process_gone", "stop_confirmed", "never_started"),
    [
        (_show_activating(), "starting", False, False, False),
        (_show_active(), "running", False, False, False),
        (_show_deactivating(), "stopping", False, False, False),
        (
            _show_completed(NOW - timedelta(seconds=1)),
            "completed",
            True,
            False,
            False,
        ),
        (
            _show_active_exited(NOW - timedelta(seconds=1)),
            "completed",
            True,
            False,
            False,
        ),
        (
            _show_timed_out(NOW - timedelta(seconds=1)),
            "timed_out",
            True,
            True,
            False,
        ),
        (_show_failed(NOW - timedelta(seconds=1)), "failed", True, False, False),
        # A handle proves a launch attempt, so a vanished unit is ``unknown``
        # with process evidence, never "never started"/safe-to-retry.
        (_show_not_found(), "unknown", True, False, False),
        (_show_unknown(), "unknown", False, False, False),
    ],
)
def test_poll_maps_all_observation_states(
    tmp_path: Path,
    show: Mapping[str, str],
    state: str,
    process_gone: bool,
    stop_confirmed: bool,
    never_started: bool,
) -> None:
    ops = FakeOps()
    ops.default_show = show
    harness = _build(tmp_path, ops=ops)

    observation = harness.supervisor.poll(_handle())

    assert observation.state == state
    assert observation.process_gone is process_gone
    assert observation.stop_confirmed is stop_confirmed
    assert observation.never_started is never_started


def test_check_and_stop_keeps_running_while_grant_active(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    observation = harness.supervisor.check_and_stop(handle)

    assert observation.state == "running"
    assert harness.revocation.lookup(handle.jti) is None
    assert ops.terminate_calls == []
    assert ops.stop_calls == []


def test_grant_revoke_stops_and_confirms(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    harness.provider.grant = _grant(status="revoked")
    ops.show_plan = [_show_active_exited(NOW + timedelta(seconds=1))]
    ops.default_show = _show_active_exited(NOW + timedelta(seconds=1))

    observation = harness.supervisor.check_and_stop(handle)

    assert observation.state == "cancelled"
    assert observation.process_gone is True
    assert observation.stop_confirmed is True
    assert observation.never_started is False
    assert ops.terminate_calls == [UNIT]
    assert ops.stop_calls == []

    record = harness.revocation.lookup(handle.jti)
    assert record is not None
    assert record.reason == "grant_revoked"

    stop_events = [
        event
        for event in harness.sink.events
        if event["event"] in {AUDIT_EVENT_STOP, AUDIT_EVENT_STOP_UNCONFIRMED}
    ]
    assert stop_events
    assert stop_events[-1]["stop_confirmed"] is True


def test_natural_completion_race_keeps_completed(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    ops.default_show = _show_completed(NOW - timedelta(seconds=1))

    first = harness.supervisor.stop(handle, "grant_revoked")
    assert first.result == "completed"
    assert first.process_gone is True
    assert first.stop_confirmed is False

    # A second stop is an idempotent no-op and must not downgrade the evidence.
    second = harness.supervisor.stop(handle, "grant_revoked")
    assert second == first
    assert len(ops.terminate_calls) == 2
    assert ops.stop_calls == []

    record = harness.revocation.lookup(handle.jti)
    assert record is not None
    assert record.reason == "grant_revoked"


def test_same_second_stop_evidence_stays_unknown(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    # The classic systemd timestamp has whole-second precision, so an exit in
    # the same second as the stop request cannot be ordered.
    ops.default_show = _show_completed(NOW)

    result = harness.supervisor.stop(handle, "manual_stop")

    assert result.process_gone is True
    assert result.stop_confirmed is False
    assert result.result == "unknown"


def test_stop_continues_when_revocation_write_fails(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    store = FailingRevocationStore()
    harness = _build(tmp_path, ops=ops, revocation=store)
    handle = harness.supervisor.launch(_spec())

    ops.default_show = _show_completed(NOW + timedelta(seconds=1))

    result = harness.supervisor.stop(handle, "grant_revoked")

    assert result.process_gone is True
    assert result.stop_confirmed is True
    assert ops.terminate_calls == [UNIT]
    assert ops.stop_calls == []
    # Design §11.5: one retry, then continue stopping and flag the audit.
    assert len(store.revoke_calls) == 2

    events = [
        event for event in harness.sink.events if event["event"] == AUDIT_EVENT_STOP
    ]
    assert events
    assert events[-1]["credential_revoke_failed"] is True


def test_stop_timeout_unconfirmed_does_not_cleanup(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    ops.default_show = _show_deactivating()

    result = harness.supervisor.stop(handle, "manual_stop")

    assert result.result == "unit_stop_unconfirmed"
    assert result.process_gone is False
    assert result.stop_confirmed is False
    assert ops.terminate_calls == [UNIT]
    assert ops.kill_calls == [UNIT]
    assert ops.stop_calls == []

    with pytest.raises(SecurityError) as caught:
        harness.supervisor.cleanup(handle)
    assert caught.value.code == "unit_stop_unconfirmed"
    assert ops.cleanup_calls == []
    assert ops.reset_failed_calls == []


def test_stop_uses_terminate_and_preserves_exit_evidence(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    # RemainAfterExit keeps a SIGTERM'd unit loaded as active/exited; the
    # supervisor reads that evidence instead of collecting it with stop.
    ops.default_show = _show_active_exited(NOW + timedelta(seconds=1))

    result = harness.supervisor.stop(handle, "manual_stop")

    assert result.stop_confirmed is True
    assert result.result == "cancelled"
    assert ops.terminate_calls == [UNIT]
    assert ops.stop_calls == []
    assert ops.kill_calls == []
    assert ops.cleanup_calls == []
    assert ops.cleaned is False


def test_cleanup_requires_process_gone_or_never_started(tmp_path: Path) -> None:
    refused_ops = FakeOps()
    refused_ops.default_show = _show_deactivating()
    refused = _build(tmp_path, ops=refused_ops)

    with pytest.raises(SecurityError) as caught:
        refused.supervisor.cleanup(_handle())
    assert caught.value.code == "unit_stop_unconfirmed"
    assert refused_ops.cleanup_calls == []

    completed_ops = FakeOps()
    completed_ops.default_show = _show_active_exited(NOW - timedelta(seconds=1))
    completed = _build(tmp_path, ops=completed_ops)

    result = completed.supervisor.cleanup(_handle())
    assert result.units_remaining == 0
    assert result.state_dir_removed is True
    assert result.credential_removed is True
    assert completed_ops.cleanup_calls == [UNIT]
    assert completed_ops.cleaned is True
    assert completed_ops.stop_calls == []
    assert completed_ops.terminate_calls == []
    assert any(
        event["event"] == AUDIT_EVENT_CLEANUP for event in completed.sink.events
    )

    vanished_ops = FakeOps()
    vanished_ops.default_show = _show_not_found()
    vanished = _build(tmp_path, ops=vanished_ops)

    # A handle proves a launch attempt: a vanished unit is process evidence,
    # not "never started".  Cleanup is therefore allowed (process_gone=true).
    observation = vanished.supervisor.poll(_handle())
    assert observation.state == "unknown"
    assert observation.process_gone is True
    assert observation.never_started is False

    result = vanished.supervisor.cleanup(_handle())
    assert result.units_remaining == 0
    assert vanished_ops.cleanup_calls == [UNIT]


def test_token_absent_from_audit_and_exception(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    harness.supervisor.launch(_spec())

    token = ops.credentials[0]
    assert token.startswith("agx1.")
    assert token not in json.dumps(harness.sink.events)
    assert token not in repr(harness.sink.events)

    fatal = FakeOps()
    fatal.launch_error_with_token = True
    fatal.default_show = _show_not_found()
    failed = _build(tmp_path, ops=fatal)

    with pytest.raises(SecurityError) as caught:
        failed.supervisor.launch(_spec())

    error = caught.value
    leaked = fatal.credentials[0]
    assert leaked.startswith("agx1.")
    assert error.code == "unit_start_failed"
    assert leaked not in str(error)
    assert leaked not in repr(error)
    assert leaked not in (error.detail or "")
    assert error.__cause__ is None
    assert error.__context__ is None


def test_effective_ttl_bounded_by_fresh_grant_after_slow_preflight(
    tmp_path: Path,
) -> None:
    fake = FakeTime(NOW)
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    # A slow preflight show consumes 10s of wall-clock time before the Grant is
    # read, so the effective TTL must be recomputed against the fresh expiry.
    ops.show_hook = lambda: fake.advance(10)
    grant = _grant(expires_at=NOW + timedelta(seconds=30))
    harness = _build(tmp_path, ops=ops, grant=grant, time_source=fake)

    handle = harness.supervisor.launch(_spec())  # spec ttl is 60

    config = ops.launch_calls[0]
    assert config.spec.ttl_seconds == 20  # type: ignore[attr-defined]
    assert "--property=RuntimeMaxSec=20s" in build_systemd_run_argv(
        config  # type: ignore[arg-type]
    )
    assert json.loads(ops.spec_texts[0])["ttl_seconds"] == 20

    claims = CredentialValidator({KID: KEY}, fake.clock).validate(
        ops.credentials[0]
    )
    assert claims.exp <= int(grant.expires_at.timestamp())
    assert handle.expires_at <= grant.expires_at


def test_credential_beyond_grant_expiry_is_rejected(tmp_path: Path) -> None:
    fake = FakeTime(NOW)
    issuer_fake = FakeTime(NOW + timedelta(seconds=10))
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(
        tmp_path,
        ops=ops,
        grant=_grant(expires_at=NOW + timedelta(seconds=30)),
        time_source=fake,
        issuer_clock=issuer_fake.clock,
    )

    with pytest.raises(SecurityError) as caught:
        harness.supervisor.launch(_spec())

    # Even though the effective TTL is 30s, the issuer clock is 10s ahead, so
    # the credential would expire 10s after the Grant; refuse to launch.
    assert caught.value.code == "grant_expired"
    assert caught.value.detail == "grant_expiry_too_soon"
    assert ops.launch_calls == []


def test_helper_payload_uses_effective_ttl(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(
        tmp_path,
        ops=ops,
        grant=_grant(expires_at=NOW + timedelta(seconds=30)),
    )
    harness.supervisor.launch(_spec())  # spec ttl is 60

    captured: list[tuple[list[str], str | None]] = []

    def runner(
        argv: list[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        captured.append((argv, input_text))
        return subprocess.CompletedProcess(argv, 0, "", "")

    helper = HelperSystemdOps(Path("/opt/example/agentmgr-exec"), runner=runner)
    helper.launch(
        ops.launch_calls[0],  # type: ignore[arg-type]
        spec_text=ops.spec_texts[0],
        credential_text="agx1.placeholder",
    )

    payload = json.loads(captured[0][1] or "{}")
    assert payload["spec"]["ttl_seconds"] == 30
    assert payload["spec"]["ttl_seconds"] != 60


def test_launch_success_then_not_found_is_not_never_started(
    tmp_path: Path,
) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_not_found()]
    ops.default_show = _show_not_found()
    harness = _build(tmp_path, ops=ops)

    with pytest.raises(SecurityError) as caught:
        harness.supervisor.launch(_spec())

    assert caught.value.code == "unit_start_failed"
    assert caught.value.detail == "unit_outcome_unknown"
    assert ops.stop_calls == [UNIT]

    observation = harness.supervisor.poll(_handle())
    assert observation.state == "unknown"
    assert observation.process_gone is True
    assert observation.never_started is False


def test_stop_window_uses_monotonic_deadline(tmp_path: Path) -> None:
    fake = FakeTime(NOW)
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops, time_source=fake)
    handle = harness.supervisor.launch(_spec())

    before = len(ops.show_calls)
    # One hung inspection consumes both the 10s stop and the 3s kill window.
    ops.show_hook = lambda: fake.advance_monotonic(100)
    ops.default_show = _show_deactivating()

    result = harness.supervisor.stop(handle, "manual_stop")

    assert result.result == "unit_stop_unconfirmed"
    assert len(ops.show_calls) - before == 2


def test_check_and_stop_stops_revoked_grant_when_show_fails(
    tmp_path: Path,
) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    harness.provider.grant = _grant(status="revoked")
    ops.show_error = SecurityError("unknown", "show_failed")

    observation = harness.supervisor.check_and_stop(handle)

    assert observation.state == "unknown"
    assert observation.process_gone is False
    assert ops.terminate_calls == [UNIT]
    assert ops.stop_calls == []
    record = harness.revocation.lookup(handle.jti)
    assert record is not None
    assert record.reason == "grant_revoked"


def test_slow_revocation_keeps_natural_completion(tmp_path: Path) -> None:
    fake = FakeTime(NOW)
    store = SlowRevocationStore(fake, seconds=10)
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(
        tmp_path, ops=ops, revocation=store, time_source=fake
    )
    handle = harness.supervisor.launch(_spec())

    # The exit precedes the *dispatched* stop by 5s, even though the original
    # stop call started before it; revoking took 10s.
    ops.default_show = _show_completed(NOW + timedelta(seconds=5))

    result = harness.supervisor.stop(handle, "grant_revoked")

    assert result.result == "completed"
    assert result.process_gone is True
    assert result.stop_confirmed is False


def test_failed_stop_command_does_not_confirm_cancelled(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    ops.terminate_error = SecurityError(
        "unit_stop_unconfirmed", "terminate_failed"
    )
    ops.default_show = _show_completed(NOW + timedelta(seconds=1))

    result = harness.supervisor.stop(handle, "manual_stop")

    assert result.process_gone is True
    assert result.stop_confirmed is False
    assert result.result == "unknown"
    events = [
        event for event in harness.sink.events if event["event"] == AUDIT_EVENT_STOP
    ]
    assert events
    assert events[-1]["stop_command_failed"] is True


def test_failed_stop_command_keeps_timeout_confirmed(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    ops.terminate_error = SecurityError(
        "unit_stop_unconfirmed", "terminate_failed"
    )
    ops.default_show = _show_timed_out(NOW + timedelta(seconds=1))

    result = harness.supervisor.stop(handle, "manual_stop")

    assert result.process_gone is True
    assert result.stop_confirmed is True
    assert result.result == "timed_out"


def test_handle_grant_id_is_validated_before_use(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    bad = replace(_handle(), grant_id="not_a_grant")

    with pytest.raises(SecurityError) as caught:
        harness.supervisor.poll(bad)

    assert caught.value.code == "invalid_spec"
    assert caught.value.detail == "grant_id_invalid"
    assert harness.ops.show_calls == []


def test_stop_audit_failure_sets_marker(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    harness.sink.fail = True
    ops.default_show = _show_completed(NOW + timedelta(seconds=1))

    result = harness.supervisor.stop(handle, "manual_stop")

    assert result.stop_confirmed is True
    assert harness.supervisor.audit_failures >= 1
    assert harness.supervisor.last_audit_failed is True


def test_cleanup_audit_failure_is_not_silent(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    handle = harness.supervisor.launch(_spec())

    ops.default_show = _show_completed(NOW + timedelta(seconds=1))
    harness.sink.fail = True

    with pytest.raises(SecurityError) as caught:
        harness.supervisor.cleanup(handle)

    assert caught.value.code == "unknown"
    assert caught.value.detail == "audit_unavailable"
    assert ops.cleanup_calls == [UNIT]
    assert harness.supervisor.last_audit_failed is True


def test_launch_audit_failure_stops_and_sets_marker(tmp_path: Path) -> None:
    ops = FakeOps()
    ops.show_plan = [_show_not_found(), _show_active()]
    ops.default_show = _show_active()
    harness = _build(tmp_path, ops=ops)
    harness.sink.fail = True
    ops.default_show = _show_completed(NOW + timedelta(seconds=1))

    with pytest.raises(SecurityError) as caught:
        harness.supervisor.launch(_spec())

    assert caught.value.code == "unknown"
    assert caught.value.detail == "audit_unavailable"
    assert ops.terminate_calls == [UNIT]
    assert ops.stop_calls == []
    assert harness.supervisor.last_audit_failed is True
