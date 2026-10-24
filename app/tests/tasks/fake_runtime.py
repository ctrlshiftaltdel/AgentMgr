"""A scripted, in-process :class:`RuntimeAdapter` for dispatcher tests.

The fake reports facts only, exactly like a real adapter: it never writes task
state and never inspects the database.  Every call is recorded so a test can
assert that a fail-closed path did **not** reach the runtime.

This helper is test-only evidence.  It never upgrades a real runtime's
capability labels and must not be used to claim that Hermes (or any other
runtime) supports Managed Execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    CancelOutcome,
    CancelResult,
    Capability,
    CapabilityStatus,
    EventBatch,
    HealthResult,
    HealthStatus,
    InputOutcome,
    InputResult,
    LookupOutcome,
    LookupResult,
    ResultOutcome,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeGrant,
    RuntimeResult,
    RuntimeState,
    RuntimeType,
    StatusResult,
    SubmitOutcome,
    SubmitResult,
    VersionSource,
)

__all__ = ["FakeRuntimeAdapter"]

#: Opaque handle the fake returns for an accepted submission.
DEFAULT_RUNTIME_TASK_ID = "rtask_fake_0001"

#: Capabilities the fake advertises.  ``task.submit``/``task.status`` are the
#: two the dispatcher requires; the rest keep the snapshot shaped like a real
#: managed runtime rather than a submit-only special case.
_ADVERTISED_CAPABILITIES = (
    RuntimeCapability.TASK_SUBMIT,
    RuntimeCapability.TASK_STATUS,
    RuntimeCapability.TASK_LOOKUP,
    RuntimeCapability.TASK_RESULT,
    RuntimeCapability.TASK_CANCEL,
    RuntimeCapability.TASK_EVENTS_POLL,
    RuntimeCapability.TASK_INPUT_PROVIDE,
    RuntimeCapability.RUNTIME_HEALTH,
    RuntimeCapability.RUNTIME_MANAGED_EXECUTION,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FakeRuntimeAdapter:
    """Scripted adapter with recorded calls and conservative defaults.

    Parameters script one response per protocol method.  ``status`` also
    accepts a sequence: each ``get_status`` call consumes the next item and the
    final item repeats, so a test can script a short state progression.
    ``result`` accepts a sequence in the same way.  ``lookup`` accepts a
    sequence in the same way, which lets a test script ``unknown`` followed by
    ``found`` (or any other reconciliation progression).  ``lookup_error``
    raises from ``find_by_idempotency_key`` instead of returning a result;
    ``status_error``/``result_error`` do the same for the status/result calls
    so a test can prove the dispatcher fails closed when those facts cannot be
    read.

    Unscripted defaults are deliberately conservative:

    * ``capabilities`` is ``verified`` (set ``capabilities_verified=False`` to
      script an unsupported runtime);
    * ``submit`` is ``accepted`` (an ACCEPTED result with a handle is
      remembered per ``(task_id, idempotency_key)``; a repeat returns the same
      handle with ``replayed=True`` and creates no second runtime task);
    * ``status`` is ``accepted`` (never a terminal state the test did not ask
      for, and a stable answer that proves the poll loop always terminates);
    * ``lookup`` is ``unknown`` and ``get_result`` is ``unknown`` (never a
      definite "not found"/"succeeded" answer the test did not ask for);
    * ``cancel`` is ``requested`` with ``stop_confirmed=False``;
    * ``provide_input`` is ``unsupported``.

    Passing an exception instance as ``health`` raises it from :meth:`health`,
    which lets a test exercise the dispatcher's unexpected-error rollback.
    """

    def __init__(
        self,
        *,
        capabilities_verified: bool = True,
        health: HealthStatus | BaseException = HealthStatus.HEALTHY,
        submit: SubmitResult | None = None,
        status: StatusResult | Sequence[StatusResult] | None = None,
        status_error: BaseException | None = None,
        result: RuntimeResult | Sequence[RuntimeResult] | None = None,
        result_error: BaseException | None = None,
        lookup: LookupResult | Sequence[LookupResult] | None = None,
        lookup_error: BaseException | None = None,
        cancel: CancelResult | None = None,
        events: EventBatch | None = None,
        provide_input: InputResult | None = None,
        capabilities: RuntimeCapabilities | None = None,
        runtime_task_id: str = DEFAULT_RUNTIME_TASK_ID,
        now: datetime | None = None,
    ) -> None:
        self.capabilities_verified = capabilities_verified
        self.health_status = health
        self.runtime_task_id = runtime_task_id
        self.now = now or _utcnow()

        self._capabilities = capabilities
        self._submit = submit
        self._status = status
        self._status_index = 0
        self.status_error = status_error
        self._result = result
        self._result_index = 0
        self.result_error = result_error
        self._lookup = lookup
        self._lookup_index = 0
        self.lookup_error = lookup_error
        self._cancel = cancel
        self._events = events
        self._provide_input = provide_input
        self._runtime_task_by_key: dict[tuple[str, str], str] = {}

        #: ``(task_id, idempotency_key, grant_id, prompt)`` per submission.
        self.submit_calls: list[tuple[str, str, str, str]] = []
        #: Handles actually created by the fake, in creation order.  A replay
        #: with the same ``(task_id, idempotency_key)`` returns the existing
        #: handle and does **not** append here.
        self.created_runtime_task_ids: list[str] = []
        self.lookup_calls: list[tuple[str, str, str, str | None]] = []
        self.status_calls: list[tuple[str, str | None]] = []
        self.result_calls: list[tuple[str, str | None]] = []
        self.cancel_calls: list[dict[str, object]] = []
        #: Returned cancel facts, parallel to :attr:`cancel_calls`.
        self.cancel_results: list[CancelResult] = []
        self.event_calls: list[tuple[str, str | None]] = []
        self.input_calls: list[tuple[str, str, str]] = []
        self.capability_probes: list[bool] = []
        self.health_calls = 0

    # -- capability / health -------------------------------------------------

    def capabilities(self, *, probe: bool = False) -> RuntimeCapabilities:
        self.capability_probes.append(probe)
        if self._capabilities is not None:
            return self._capabilities
        status = (
            CapabilityStatus.VERIFIED
            if self.capabilities_verified
            else CapabilityStatus.UNVERIFIED
        )
        limitations = (
            None
            if status is CapabilityStatus.VERIFIED
            else "the fake adapter script marks this capability as unverified"
        )
        return RuntimeCapabilities(
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            boundary=BOUNDARY,
            runtime_id="rt_fake_0001",
            runtime_type=RuntimeType.OTHER,
            runtime_version=None,
            version_source=VersionSource.UNKNOWN,
            capabilities=tuple(
                Capability(
                    name=name.value,
                    version="1.0.0",
                    status=status,
                    evidence_ref=(
                        "evidence:fake_runtime_capability"
                        if status is CapabilityStatus.VERIFIED
                        else None
                    ),
                    limitations=limitations,
                )
                for name in _ADVERTISED_CAPABILITIES
            ),
            observed_at=self.now,
            runtime_name="fake-runtime",
        )

    def health(self) -> HealthResult:
        self.health_calls += 1
        if isinstance(self.health_status, BaseException):
            raise self.health_status
        return HealthResult(status=self.health_status, checked_at=self.now)

    # -- submission / lookup -------------------------------------------------

    def submit_task(
        self,
        task_id: str,
        prompt: str,
        idempotency_key: str,
        grant: RuntimeGrant,
        instructions: str | None = None,
    ) -> SubmitResult:
        self.submit_calls.append(
            (task_id, idempotency_key, grant.grant_id, prompt)
        )
        # The runtime idempotency contract: the same task/key reuses one
        # handle and reports the replay; it never creates a second run.
        existing = self._runtime_task_by_key.get((task_id, idempotency_key))
        if existing is not None:
            return SubmitResult(
                outcome=SubmitOutcome.ACCEPTED,
                runtime_task_id=existing,
                accepted_at=self.now,
                reported_state=RuntimeState.ACCEPTED.value,
                replayed=True,
            )
        if self._submit is not None:
            result = self._submit
        else:
            result = SubmitResult(
                outcome=SubmitOutcome.ACCEPTED,
                runtime_task_id=self.runtime_task_id,
                accepted_at=self.now,
                reported_state=RuntimeState.ACCEPTED.value,
            )
        if (
            result.outcome is SubmitOutcome.ACCEPTED
            and isinstance(result.runtime_task_id, str)
            and result.runtime_task_id
        ):
            self._runtime_task_by_key[(task_id, idempotency_key)] = (
                result.runtime_task_id
            )
            self.created_runtime_task_ids.append(result.runtime_task_id)
        return result

    def find_by_idempotency_key(
        self,
        task_id: str,
        grant_id: str,
        idempotency_key: str,
        payload_hash: str | None = None,
    ) -> LookupResult:
        self.lookup_calls.append(
            (task_id, grant_id, idempotency_key, payload_hash)
        )
        if self.lookup_error is not None:
            raise self.lookup_error
        scripted = self._lookup
        if scripted is None:
            return LookupResult(outcome=LookupOutcome.UNKNOWN)
        if isinstance(scripted, LookupResult):
            return scripted
        items = list(scripted)
        if not items:
            raise AssertionError("FakeRuntimeAdapter lookup sequence is empty")
        index = min(self._lookup_index, len(items) - 1)
        self._lookup_index += 1
        return items[index]

    # -- status / events / result -------------------------------------------

    def get_status(
        self,
        runtime_task_id: str,
        *,
        task_id: str | None = None,
    ) -> StatusResult:
        self.status_calls.append((runtime_task_id, task_id))
        if self.status_error is not None:
            raise self.status_error
        return self._next_status(runtime_task_id)

    def poll_events(
        self,
        runtime_task_id: str,
        cursor: str | None = None,
        max_events: int = 100,
        timeout: float = 30.0,
        *,
        task_id: str | None = None,
    ) -> EventBatch:
        self.event_calls.append((runtime_task_id, cursor))
        if self._events is not None:
            return self._events
        return EventBatch(events=(), next_cursor=None, has_more=False)

    def get_result(
        self,
        runtime_task_id: str,
        *,
        task_id: str | None = None,
    ) -> RuntimeResult:
        self.result_calls.append((runtime_task_id, task_id))
        if self.result_error is not None:
            raise self.result_error
        scripted = self._result
        if scripted is None:
            return RuntimeResult(
                outcome=ResultOutcome.UNKNOWN,
                result_ref=None,
                summary=None,
                structured_payload=None,
                artifacts=(),
                evidence_refs=(),
                verification_hints=(),
                stop_confirmed=False,
                completed_at=None,
            )
        if isinstance(scripted, RuntimeResult):
            return scripted
        items = list(scripted)
        if not items:
            raise AssertionError("FakeRuntimeAdapter result sequence is empty")
        index = min(self._result_index, len(items) - 1)
        self._result_index += 1
        return items[index]

    # -- control -------------------------------------------------------------

    def cancel_task(
        self,
        runtime_task_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> CancelResult:
        self.cancel_calls.append(
            {
                "runtime_task_id": runtime_task_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "task_id": task_id,
            }
        )
        if self._cancel is not None:
            result = self._cancel
        else:
            result = CancelResult(
                outcome=CancelOutcome.REQUESTED,
                stop_confirmed=False,
                observed_at=self.now,
            )
        self.cancel_results.append(result)
        return result

    def provide_input(
        self,
        runtime_task_id: str,
        input_request_id: str,
        payload_hash: str,
        payload: Mapping[str, object],
    ) -> InputResult:
        self.input_calls.append(
            (runtime_task_id, input_request_id, payload_hash)
        )
        if self._provide_input is not None:
            return self._provide_input
        return InputResult(outcome=InputOutcome.UNSUPPORTED, observed_at=self.now)

    # -- internals -----------------------------------------------------------

    def _next_status(self, runtime_task_id: str) -> StatusResult:
        scripted = self._status
        if scripted is None:
            return StatusResult(
                runtime_task_id=runtime_task_id,
                state=RuntimeState.ACCEPTED,
                external_state=None,
                observed_at=self.now,
            )
        if isinstance(scripted, StatusResult):
            return scripted
        items = list(scripted)
        if not items:
            raise AssertionError("FakeRuntimeAdapter status sequence is empty")
        index = min(self._status_index, len(items) - 1)
        self._status_index += 1
        return items[index]
