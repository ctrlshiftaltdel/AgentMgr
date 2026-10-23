"""Managed execution supervisor: launch, poll, stop and cleanup (Issue #82).

The supervisor is the trusted controller described by design §11.  It validates
the server-resolved spec, reads the **live** Grant, issues a task-bound
credential, starts one transient systemd unit through :class:`SystemdOps`, and
reports only the facts it can prove:

* ``process_gone``, ``stop_confirmed`` and ``never_started`` stay independent
  (design §11.1); a natural ``completed``/``failed`` is never upgraded to a
  confirmed stop, and an unconfirmed stop is never reported as stopped.
* ``RuntimeMaxSec`` is derived from the *effective* TTL
  (``min(spec.ttl_seconds, grant_remaining, 300)``), so the unit cannot outlive
  its Grant.
* The credential is revoked before a stop is requested; a failed revocation
  write is retried once, recorded in the audit event and never blocks the stop.
* Cleanup refuses unless ``process_gone`` or ``never_started`` is true.
* The bearer token is passed only to ``SystemdOps.launch(credential_text=...)``.
  It never reaches the handle, an audit event, an exception message or a
  ``repr``.

The unit's task state machine is never written here: this module reports
observations and stop evidence; task transitions belong to the core.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from app.security.boundary import (
    BYPASSABLE_BOUNDARY,
    CONTRACT_VERSION,
    DEFERRED_BOUNDARY,
    ENFORCED_BOUNDARY,
    EXECUTION_MODE_MANAGED,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    NETWORK_PROFILES,
    PRODUCT_CLAIM,
    SUBSTRATE,
    BoundaryClaim,
    CleanupResult,
    GrantStatus,
    GrantStatusProvider,
    LaunchHandle,
    ManagedTaskSpec,
    RuntimeObservation,
    StopResult,
    unit_name_for_task,
)
from app.security.credentials import CredentialIssuer, RevocationStore
from app.security.errors import SecurityError
from app.security.gateway import AuditSink
from app.security.systemd import (
    DEFAULT_RUNTIME_ROOT,
    NetworkProfile,
    SystemdOps,
    SystemdUnitConfig,
    observation_from_show,
)

__all__ = [
    "AUDIT_EVENT_CLEANUP",
    "AUDIT_EVENT_LAUNCH",
    "AUDIT_EVENT_STOP",
    "AUDIT_EVENT_STOP_UNCONFIRMED",
    "KILL_TIMEOUT_SECONDS",
    "LAUNCH_TIMEOUT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "STOP_REASONS",
    "STOP_TIMEOUT_SECONDS",
    "SUPERVISOR_AUDIT_FIELDS",
    "ManagedExecutionSupervisor",
    "SupervisorConfig",
]

#: Audit event names (design §11.2/§11.4).  The payload whitelist below is the
#: only thing the supervisor may record; the token is never a member.
AUDIT_EVENT_LAUNCH = "security.exec.supervisor.launch"
AUDIT_EVENT_STOP = "security.exec.supervisor.stop"
AUDIT_EVENT_STOP_UNCONFIRMED = "security.exec.supervisor.stop_unconfirmed"
AUDIT_EVENT_CLEANUP = "security.exec.supervisor.cleanup"

SUPERVISOR_AUDIT_FIELDS: frozenset[str] = frozenset(
    {
        "ts",
        "event",
        "task_id",
        "grant_id",
        "credential_jti",
        "unit",
        "reason",
        "state",
        "result",
        "systemd_result",
        "exec_main_status",
        "process_gone",
        "stop_confirmed",
        "never_started",
        "ttl_seconds",
        "network_profile",
        "contract_version",
        "main_pid",
        "credential_revoke_failed",
        "stop_command_failed",
        "units_remaining",
        "state_dir_removed",
        "credential_removed",
    }
)

#: The only stop reasons design §11.4 permits.
STOP_REASONS: frozenset[str] = frozenset(
    {"grant_revoked", "grant_expired", "manual_stop", "task_terminal"}
)

#: Poll windows (design §11.2–§11.4).  The loops are iteration-bounded so a
#: supplied sleep function controls wall-clock time in tests.
LAUNCH_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0
KILL_TIMEOUT_SECONDS = 3.0
POLL_INTERVAL_SECONDS = 0.1

_SUBJECT_PATTERN = re.compile(r"^(prin|svc|rt)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
_GRANT_PATTERN = re.compile(r"^grnt_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_JTI_PATTERN = re.compile(r"^cred_[0-9a-f]{32}$")

_T = TypeVar("_T")


def _require_source_path(value: object, detail: str) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or "\x00" in str(value)
    ):
        raise ValueError(detail)
    return value


def _unknown_observation() -> RuntimeObservation:
    return RuntimeObservation(
        state="unknown",
        process_gone=False,
        stop_confirmed=False,
        never_started=False,
        main_pid=0,
        result="unknown",
        exec_main_status=None,
    )


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Fixed supervisor inputs; no task-supplied path is accepted here.

    ``monotonic`` and ``sleep`` are injectable so deadline-bound poll windows
    can be tested without wall-clock waits; production uses the stdlib.
    """

    workload_source: Path
    gateway_socket_source: Path
    network_profile: NetworkProfile
    clock: Callable[[], datetime]
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        _require_source_path(self.workload_source, "workload_source_invalid")
        _require_source_path(
            self.gateway_socket_source, "gateway_socket_source_invalid"
        )
        if (
            not isinstance(self.network_profile, str)
            or self.network_profile not in NETWORK_PROFILES
        ):
            raise ValueError("network_profile_invalid")
        if not callable(self.clock):
            raise ValueError("clock must be callable")
        if not callable(self.monotonic):
            raise ValueError("monotonic must be callable")
        if not callable(self.sleep):
            raise ValueError("sleep must be callable")


class ManagedExecutionSupervisor:
    """Launch/poll/stop/cleanup state machine over an injected systemd adapter."""

    def __init__(
        self,
        config: SupervisorConfig,
        ops: SystemdOps,
        provider: GrantStatusProvider,
        issuer: CredentialIssuer,
        revocation_store: RevocationStore,
        audit_sink: AuditSink,
    ) -> None:
        if not isinstance(config, SupervisorConfig):
            raise ValueError("config must be a SupervisorConfig")
        for attribute, owner in (
            ("launch", ops),
            ("stop", ops),
            ("kill", ops),
            ("show", ops),
            ("reset_failed", ops),
            ("remove_sources", ops),
            ("cleanup", ops),
            ("status", provider),
            ("issue", issuer),
            ("revoke", revocation_store),
            ("prune", revocation_store),
            ("record", audit_sink),
        ):
            if not callable(getattr(owner, attribute, None)):
                raise ValueError(f"{attribute} is not callable")
        self._config = config
        self._ops = ops
        self._provider = provider
        self._issuer = issuer
        self._revocation_store = revocation_store
        self._audit_sink = audit_sink
        #: Public markers so an unauditable stop/cleanup is never silent.
        self.audit_failures = 0
        self.last_audit_failed = False

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------
    def launch(self, spec: ManagedTaskSpec) -> LaunchHandle:
        """Validate, issue a credential and start one sandboxed unit."""

        if not isinstance(spec, ManagedTaskSpec):
            raise SecurityError("invalid_spec", "spec_invalid")
        spec.validate(self._now())
        unit = unit_name_for_task(spec.task_id)

        # A stale or live unit with the same derived name must not be reused;
        # ``systemd-run --unit`` would otherwise race or refuse unpredictably.
        # This preflight does not need the Grant and may block, so it runs
        # before the Grant is read and the TTL is computed.
        preflight = self._observe(unit)
        if preflight.state != "not_started":
            raise SecurityError("invalid_spec", "unit_name_collision")

        # The Grant is read *after* the slow preflight: the expiry and the TTL
        # must reflect the same fresh instant used for the credential.
        now = self._now()
        grant = self._require_grant(spec, now)
        effective_ttl = self._effective_ttl(spec, grant, now)
        effective_spec = replace(spec, ttl_seconds=effective_ttl)

        issued = self._issuer.issue(
            task_id=spec.task_id,
            grant_id=spec.grant_id,
            subject_id=grant.subject_id,
            capabilities=spec.capabilities,
            resource_scope=spec.resource_scope,
            ttl_seconds=effective_ttl,
        )
        # The issuer has its own clock.  Refuse to launch a credential whose
        # absolute expiry is beyond the Grant expiry observed above.
        if issued.claims.exp > int(grant.expires_at.timestamp()):
            raise SecurityError("grant_expired", "grant_expiry_too_soon")

        handle = LaunchHandle(
            unit=unit,
            task_id=spec.task_id,
            grant_id=spec.grant_id,
            jti=issued.claims.jti,
            expires_at=datetime.fromtimestamp(issued.claims.exp, UTC),
            network_profile=self._config.network_profile,
            boundary_claim=self._claim(),
            launched_at=now,
        )
        unit_config = SystemdUnitConfig(
            unit=unit,
            # The effective TTL is what ``RuntimeMaxSec`` is built from, and the
            # same spec is sent to the helper, so neither can outlive the Grant.
            spec=effective_spec,
            credential_source=(
                DEFAULT_RUNTIME_ROOT / "credentials" / f"{unit}.json"
            ),
            workload_source=self._config.workload_source,
            gateway_socket_source=self._config.gateway_socket_source,
            network_profile=self._config.network_profile,
        )

        failure: SecurityError | None = None
        try:
            self._ops.launch(
                unit_config,
                spec_text=json.dumps(effective_spec.as_dict()),
                credential_text=issued.token,
            )
        except SecurityError as exc:
            failure = exc
        except Exception:
            failure = SecurityError("unit_start_failed", "launch_failed")
        if failure is not None:
            # A command timeout may have started the unit; other failures are
            # treated as definitive and rely on the unit's own evidence.
            raise self._fail_launch(
                handle,
                failure,
                had_start_evidence=failure.code == "timeout",
            ) from None

        observation = self._await_active(handle, had_start_evidence=True)

        # The credential source is only needed until systemd has injected the
        # credential copy; remove it as soon as the unit is provably active.
        removed: SecurityError | None = None
        try:
            self._ops.remove_sources(unit)
        except SecurityError as exc:
            removed = exc
        except Exception:
            removed = SecurityError("unknown", "remove_sources_failed")
        if removed is not None:
            raise self._fail_launch(
                handle,
                removed,
                observation=observation,
                had_start_evidence=True,
            ) from None

        if not self._try_audit(
            {
                "ts": self._now().isoformat(),
                "event": AUDIT_EVENT_LAUNCH,
                "task_id": spec.task_id,
                "grant_id": spec.grant_id,
                "credential_jti": handle.jti,
                "unit": unit,
                "ttl_seconds": effective_ttl,
                "network_profile": self._config.network_profile,
                "contract_version": CONTRACT_VERSION,
                "main_pid": observation.main_pid,
            }
        ):
            # No audit, no execution: stop and revoke before reporting failure.
            self._stop(handle, "manual_stop")
            raise SecurityError("unknown", "audit_unavailable") from None
        return handle

    def poll(self, handle: LaunchHandle) -> RuntimeObservation:
        """Return one factual observation; this never writes task state."""

        unit = self._validated_handle(handle)
        return self._observe(unit, had_start_evidence=True)

    def stop(self, handle: LaunchHandle, reason: str) -> StopResult:
        """Revoke the credential, stop the unit and report stop evidence."""

        result, _ = self._stop(handle, reason)
        return result

    def cleanup(self, handle: LaunchHandle) -> CleanupResult:
        """Remove unit residue only after ``process_gone``/``never_started``."""

        unit = self._validated_handle(handle)
        observation = self._observe(unit, had_start_evidence=True)
        if not (observation.process_gone or observation.never_started):
            raise SecurityError("unit_stop_unconfirmed", "cleanup_refused")

        failure: SecurityError | None = None
        try:
            self._ops.cleanup(unit)
        except SecurityError as exc:
            failure = exc
        except Exception:
            failure = SecurityError("unknown", "cleanup_failed")
        if failure is not None:
            raise failure from None

        # Best effort: expired revocation records are housekeeping, not a
        # prerequisite for reporting that the unit's residue is gone.
        try:
            self._revocation_store.prune(self._now())
        except Exception:
            pass

        result = CleanupResult(
            units_remaining=0,
            state_dir_removed=True,
            credential_removed=True,
        )
        if not self._try_audit(
            {
                "ts": self._now().isoformat(),
                "event": AUDIT_EVENT_CLEANUP,
                "task_id": handle.task_id,
                "grant_id": handle.grant_id,
                "credential_jti": handle.jti,
                "unit": unit,
                "process_gone": observation.process_gone,
                "never_started": observation.never_started,
                "units_remaining": result.units_remaining,
                "state_dir_removed": result.state_dir_removed,
                "credential_removed": result.credential_removed,
            }
        ):
            # Cleanup already happened; report that its audit evidence is
            # missing instead of silently claiming success.
            raise SecurityError("unknown", "audit_unavailable") from None
        return result

    def check_and_stop(self, handle: LaunchHandle) -> RuntimeObservation:
        """Stop when the live Grant is revoked or expired.

        The live Grant is read first: a revoked/expired Grant must enter the
        stop flow even when ``show`` is unavailable.
        """

        unit = self._validated_handle(handle)
        reason = self._revocation_reason(handle)
        if reason is not None:
            _, stopped = self._stop(handle, reason)
            return stopped
        try:
            return self._observe(unit, had_start_evidence=True)
        except SecurityError:
            return _unknown_observation()

    # ------------------------------------------------------------------
    # Launch helpers
    # ------------------------------------------------------------------
    def _require_grant(self, spec: ManagedTaskSpec, now: datetime) -> GrantStatus:
        grant: GrantStatus | None = None
        failure: SecurityError | None = None
        try:
            grant = self._provider.status(spec.grant_id, task_id=spec.task_id)
        except SecurityError as exc:
            failure = exc
        except Exception:
            failure = SecurityError("unknown", "grant_source_unavailable")
        if failure is not None:
            raise failure from None

        if grant is None:
            raise SecurityError("grant_missing", "grant_missing")
        if not isinstance(grant, GrantStatus):
            raise SecurityError("unknown", "grant_status_invalid")
        if grant.grant_id != spec.grant_id or grant.task_id != spec.task_id:
            raise SecurityError("cross_task_denied", "grant_identity_mismatch")
        if grant.status != "active" or not grant.usable:
            raise SecurityError("grant_revoked", "grant_not_usable")
        if (
            grant.execution_mode != EXECUTION_MODE_MANAGED
            or spec.execution_mode != EXECUTION_MODE_MANAGED
        ):
            raise SecurityError(
                "execution_mode_mismatch", "managed_execution_required"
            )
        if grant.expires_at <= now:
            raise SecurityError("grant_expired", "grant_expiry_passed")
        if _SUBJECT_PATTERN.fullmatch(grant.subject_id) is None:
            raise SecurityError("invalid_spec", "subject_id_invalid")
        if not set(spec.capabilities) <= set(grant.capabilities):
            raise SecurityError("capability_not_granted", "capability_not_in_grant")
        if not spec.resource_scope.is_subset_of(grant.resource_scope):
            raise SecurityError("resource_scope_denied", "scope_not_in_grant")
        return grant

    @staticmethod
    def _effective_ttl(
        spec: ManagedTaskSpec, grant: GrantStatus, now: datetime
    ) -> int:
        remaining = (grant.expires_at - now).total_seconds()
        effective = int(min(spec.ttl_seconds, remaining, MAX_TTL_SECONDS))
        if effective < MIN_TTL_SECONDS:
            raise SecurityError("grant_expired", "grant_expiry_too_soon")
        return effective

    def _await_active(
        self, handle: LaunchHandle, *, had_start_evidence: bool
    ) -> RuntimeObservation:
        deadline = self._monotonic() + LAUNCH_TIMEOUT_SECONDS
        observation: RuntimeObservation | None = None
        while True:
            try:
                observation = self._observe(
                    handle.unit, had_start_evidence=had_start_evidence
                )
            except SecurityError as exc:
                raise self._fail_launch(
                    handle,
                    exc,
                    had_start_evidence=had_start_evidence,
                ) from None
            if observation.state == "running" or observation.process_gone:
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._config.sleep(min(POLL_INTERVAL_SECONDS, remaining))

        if observation is not None and observation.state == "running":
            return observation
        if observation is not None and observation.process_gone:
            raise self._fail_launch(
                handle,
                SecurityError("unit_start_failed", "unit_not_active"),
                observation=observation,
                had_start_evidence=had_start_evidence,
            ) from None

        # The deadline passed while the unit was still starting.  Inspect once
        # more: a unit that did become active must be stopped, never reported
        # as successfully isolated.
        final = observation
        if final is None or not final.process_gone:
            try:
                final = self._observe(
                    handle.unit, had_start_evidence=had_start_evidence
                )
            except SecurityError:
                final = observation

        if final is not None and final.state == "running":
            raise self._fail_launch(
                handle,
                SecurityError("timeout", "launch_timeout"),
                observation=final,
                had_start_evidence=had_start_evidence,
            ) from None
        if final is None or final.never_started:
            raise self._fail_launch(
                handle,
                SecurityError("unit_start_failed", "unit_not_started"),
                observation=final,
                had_start_evidence=had_start_evidence,
            ) from None
        if final.process_gone:
            raise self._fail_launch(
                handle,
                SecurityError("unit_start_failed", "unit_not_active"),
                observation=final,
                had_start_evidence=had_start_evidence,
            ) from None
        raise self._fail_launch(
            handle,
            SecurityError("timeout", "launch_timeout"),
            observation=final,
            had_start_evidence=had_start_evidence,
        ) from None

    def _fail_launch(
        self,
        handle: LaunchHandle,
        launch_error: SecurityError,
        *,
        observation: RuntimeObservation | None = None,
        had_start_evidence: bool = False,
    ) -> SecurityError:
        """Revoke and terminate a partially started unit, returning the error."""

        revoked = self._revoke(handle, "manual_stop", self._now())
        try:
            self._ops.remove_sources(handle.unit)
        except Exception:
            pass
        if observation is None:
            try:
                observation = self._observe(
                    handle.unit, had_start_evidence=had_start_evidence
                )
            except SecurityError:
                observation = None

        present = observation is None or not observation.never_started
        if present:
            for operation in (self._ops.stop, self._ops.kill):
                try:
                    operation(handle.unit)
                except Exception:
                    pass

        if not revoked:
            return SecurityError("unknown", "credential_revoke_failed")
        if present:
            if launch_error.code == "timeout" or observation is None:
                return SecurityError("unknown", "launch_outcome_unknown")
            if observation.process_gone:
                # The unit has process evidence: it may have run.  Never report
                # "not started"/safe-to-retry.
                return SecurityError("unit_start_failed", "unit_outcome_unknown")
            return SecurityError(
                "unit_start_failed", "unit_present_after_launch_error"
            )
        if launch_error.code == "privilege_required":
            return SecurityError("privilege_required", "launch_unavailable")
        return SecurityError("unit_start_failed", "unit_not_started")

    # ------------------------------------------------------------------
    # Stop helper
    # ------------------------------------------------------------------
    def _stop(
        self, handle: LaunchHandle, reason: str
    ) -> tuple[StopResult, RuntimeObservation]:
        unit = self._validated_handle(handle)
        if reason not in STOP_REASONS:
            raise SecurityError("invalid_spec", "stop_reason_invalid")

        # Revocation can be slow; the stop dispatch time is captured only
        # immediately before the stop command so exit ordering is not skewed.
        revoked_at = self._now()
        revoked = self._revoke(handle, reason, revoked_at)
        dispatch_at = self._now()

        stop_failed = False
        try:
            self._ops.stop(unit)
        except Exception:
            # The evidence loop below decides the outcome; a failed stop
            # command is not proof that the process is still running.
            stop_failed = True

        observation: RuntimeObservation | None = None
        deadline = self._monotonic() + STOP_TIMEOUT_SECONDS
        while True:
            observation = self._stop_observation(unit, dispatch_at)
            if observation is not None and observation.process_gone:
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._config.sleep(min(POLL_INTERVAL_SECONDS, remaining))

        if observation is None or not observation.process_gone:
            try:
                self._ops.kill(unit)
            except Exception:
                pass
            kill_deadline = self._monotonic() + KILL_TIMEOUT_SECONDS
            while True:
                observation = self._stop_observation(unit, dispatch_at)
                if observation is not None and observation.process_gone:
                    break
                remaining = kill_deadline - self._monotonic()
                if remaining <= 0:
                    break
                self._config.sleep(min(POLL_INTERVAL_SECONDS, remaining))

        if observation is None:
            observation = _unknown_observation()

        if observation.process_gone:
            stop_confirmed = observation.stop_confirmed
            result_name = observation.state
            if stop_failed and result_name == "cancelled":
                # A failed stop command must not be reported as a confirmed
                # cancellation.  Genuine timeout evidence keeps ``timed_out``.
                stop_confirmed = False
                result_name = "unknown"
            result = StopResult(
                stop_confirmed=stop_confirmed,
                process_gone=True,
                result=result_name,
                exec_main_status=observation.exec_main_status,
            )
            event_name = AUDIT_EVENT_STOP
        else:
            result = StopResult(
                stop_confirmed=False,
                process_gone=False,
                result="unit_stop_unconfirmed",
                exec_main_status=observation.exec_main_status,
            )
            event_name = AUDIT_EVENT_STOP_UNCONFIRMED

        event: dict[str, object] = {
            "ts": self._now().isoformat(),
            "event": event_name,
            "task_id": handle.task_id,
            "grant_id": handle.grant_id,
            "credential_jti": handle.jti,
            "unit": unit,
            "reason": reason,
            "state": observation.state,
            "result": result.result,
            "systemd_result": observation.result,
            "process_gone": result.process_gone,
            "stop_confirmed": result.stop_confirmed,
            "never_started": observation.never_started,
            "credential_revoke_failed": not revoked,
            "stop_command_failed": stop_failed,
        }
        if result.exec_main_status is not None:
            event["exec_main_status"] = result.exec_main_status
        self._try_audit(event)
        return result, observation

    def _stop_observation(
        self, unit: str, dispatch_at: datetime
    ) -> RuntimeObservation | None:
        try:
            return self._observe(
                unit, stop_requested_at=dispatch_at, had_start_evidence=True
            )
        except SecurityError:
            # An unreadable state is not evidence that the process is gone;
            # keep polling and, if needed, report an unconfirmed stop.
            return None

    def _revocation_reason(self, handle: LaunchHandle) -> str | None:
        try:
            grant = self._provider.status(
                handle.grant_id, task_id=handle.task_id
            )
        except Exception:
            # A Grant source that cannot answer is fail-closed: stop.
            return "grant_revoked"
        if not isinstance(grant, GrantStatus):
            return "grant_revoked"
        if (
            grant.grant_id != handle.grant_id
            or grant.task_id != handle.task_id
            or grant.execution_mode != EXECUTION_MODE_MANAGED
            or grant.status != "active"
            or not grant.usable
        ):
            return "grant_revoked"
        if grant.expires_at <= self._now():
            return "grant_expired"
        return None

    # ------------------------------------------------------------------
    # Small invariants
    # ------------------------------------------------------------------
    def _validated_handle(self, handle: object) -> str:
        if not isinstance(handle, LaunchHandle):
            raise ValueError("handle must be a LaunchHandle")
        unit = unit_name_for_task(handle.task_id)
        if unit != handle.unit:
            raise SecurityError("invalid_spec", "unit_name_mismatch")
        if (
            not isinstance(handle.grant_id, str)
            or _GRANT_PATTERN.fullmatch(handle.grant_id) is None
        ):
            raise SecurityError("invalid_spec", "grant_id_invalid")
        if (
            not isinstance(handle.jti, str)
            or _JTI_PATTERN.fullmatch(handle.jti) is None
        ):
            raise SecurityError("invalid_spec", "jti_invalid")
        if (
            not isinstance(handle.expires_at, datetime)
            or handle.expires_at.utcoffset() is None
        ):
            raise SecurityError("invalid_spec", "grant_expiry_invalid")
        if (
            not isinstance(handle.network_profile, str)
            or handle.network_profile not in NETWORK_PROFILES
        ):
            raise SecurityError("invalid_spec", "network_profile_invalid")
        return unit

    def _claim(self) -> BoundaryClaim:
        return BoundaryClaim(
            contract_version=CONTRACT_VERSION,
            substrate=SUBSTRATE,
            network_profile=self._config.network_profile,
            product_claim=PRODUCT_CLAIM,
            enforced=ENFORCED_BOUNDARY,
            bypassable=BYPASSABLE_BOUNDARY,
            deferred=DEFERRED_BOUNDARY,
        )

    def _now(self) -> datetime:
        moment = self._config.clock()
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return moment.astimezone(UTC)

    def _monotonic(self) -> float:
        value = self._config.monotonic()
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("monotonic must return a finite number")
        return float(value)

    def _observe(
        self,
        unit: str,
        *,
        stop_requested_at: datetime | None = None,
        had_start_evidence: bool = False,
    ) -> RuntimeObservation:
        show = self._invoke(
            lambda: self._ops.show(unit),
            code="unknown",
            detail="show_failed",
        )
        if not isinstance(show, Mapping):
            raise SecurityError("unknown", "show_invalid")
        return observation_from_show(
            show,
            stop_requested_at=stop_requested_at,
            had_start_evidence=had_start_evidence,
        )

    def _revoke(
        self, handle: LaunchHandle, reason: str, moment: datetime
    ) -> bool:
        # Design §11.5: retry a failed revocation write once.  The caller still
        # stops the unit and records the failure; the gateway's live Grant
        # check is the independent backstop.
        for _ in range(2):
            try:
                self._revocation_store.revoke(
                    handle.jti, reason=reason, revoked_at=moment
                )
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _invoke(
        operation: Callable[[], _T], *, code: str, detail: str
    ) -> _T:
        """Run one adapter call and translate unexpected errors fail-closed.

        The raise happens after the ``except`` block so neither ``__cause__``
        nor ``__context__`` retains an exception that may contain a token.
        """

        result: _T | None = None
        failure: SecurityError | None = None
        try:
            result = operation()
        except SecurityError as exc:
            failure = exc
        except Exception:
            failure = SecurityError(code, detail)
        if failure is not None:
            raise failure from None
        return result  # type: ignore[return-value]

    def _audit(self, event: dict[str, object]) -> None:
        if not set(event) <= SUPERVISOR_AUDIT_FIELDS:
            raise RuntimeError("audit event outside the whitelist")
        self._audit_sink.record(event)

    def _try_audit(self, event: dict[str, object]) -> bool:
        """Record an event, exposing (never hiding) an audit failure."""

        try:
            self._audit(event)
            return True
        except Exception:
            self.audit_failures += 1
            self.last_audit_failed = True
            return False
