"""Managed Execution dispatcher: claim, preflight, submit and bind.

This module is the production caller that connects the task/ExecutionGrant core
to the frozen :class:`app.runtime.contract.RuntimeAdapter` protocol.  It only
ever claims ``execution_mode="managed_execution"`` work; advisory and governed
tasks stay client/PEP driven.

Scope of the current implementation
-----------------------------------
The whole **submission** half of the loop is implemented here, inside one
lease:

1. validate the injected service principal;
2. claim one managed task with the existing lease/fencing machinery;
3. load the task-bound :class:`~app.governance.models.ExecutionGrant` and
   project it with :func:`app.runtime.projection.project_runtime_grant`;
4. verify the persisted :class:`~app.governance.models.GovernanceDecisionRecord`
   binding **before any adapter call** (task, decision id/hash, task revision);
5. re-verify the whole Grant through the public
   :func:`app.governance.grants.grant_document` serializer (capabilities,
   resource scope, TTL, budget, gates) and fail closed on any mismatch;
6. require ``task.submit`` **and** ``task.status`` to be ``verified``;
7. probe ``adapter.health()``;
8. build the stable runtime idempotency key and the canonical, bounded,
   credential-free prompt;
9. call ``adapter.submit_task`` exactly once for this pass;
10. on ``accepted``, bind the returned ``rtask_*`` handle through
    :func:`app.tasks.service.start_attempt` and keep the lease for the later
    status/result work.  If binding is rejected after acceptance, best-effort
    cancel the runtime task, persist the accepted handle/key for reconciliation
    and record an honest ``unknown`` outcome (never a fake "rejected" and never
    a second submit);
11. on ``timeout``/``unknown``/``conflict``, call
    ``adapter.find_by_idempotency_key`` **before** deciding anything else: a
    ``found`` run is adopted through the same ``start_attempt`` bind (same key,
    worker and fencing token) instead of being submitted again.  Only a
    ``timeout``/``unknown`` run may be adopted this way; a ``conflict`` proves
    the key is bound to a different payload, so it enters ``unknown``.  When no
    adoptable run can be proven the submission is recorded as
    ``timeout``/``unknown``, the Grant is revoked and nothing is resubmitted.

Status polling, result mapping and lease recovery are added by later tasks.
This module never calls ``task_service.complete_task``: a runtime self-report
can never become ``succeeded``.

Transaction boundary
--------------------
``run_once`` uses the **injected** session.  It commits successful and
domain-rejected work and rolls back unexpected exceptions; the caller still owns
``close``.  One exception is deliberately swallowed: after an ambiguous
submission, a lookup that raises is treated exactly like ``unknown`` (the
dispatcher cannot prove no run exists, so it must enter ``unknown`` instead of
losing the claim and trying again).  :meth:`RuntimeDispatcher.run_forever`
creates a fresh session from the supplied factory for every iteration.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.errors import GovernanceError
from app.governance.grants import grant_document
from app.governance.models import ExecutionGrant, GovernanceDecisionRecord
from app.governance.policies import canonical_json
from app.governance.principal import GovernancePrincipal
from app.runtime.contract import (
    CapabilityStatus,
    HealthStatus,
    LookupOutcome,
    LookupResult,
    RuntimeAdapter,
    RuntimeCapability,
    SubmitOutcome,
    SubmitResult,
)
from app.runtime.projection import (
    MANAGED_EXECUTION_MODE,
    GrantProjectionError,
    project_runtime_grant,
)
from app.tasks import service as task_service
from app.tasks import worker
from app.tasks.errors import TaskError
from app.tasks.models import Task, TaskLease

__all__ = [
    "DECISION_BINDING_MISMATCH",
    "GRANT_DOCUMENT_INVALID",
    "MAX_RUNTIME_PROMPT_BYTES",
    "REQUIRED_CAPABILITIES",
    "RUNTIME_BIND_FAILED",
    "RUNTIME_CAPABILITY_UNVERIFIED",
    "RUNTIME_PROMPT_INVALID",
    "RUNTIME_PROMPT_TOO_LARGE",
    "RUNTIME_SUBMIT_HANDLE_INVALID",
    "DispatcherConfig",
    "DispatcherResult",
    "RuntimeDispatcher",
    "RuntimePromptTooLarge",
    "build_runtime_idempotency_key",
    "build_runtime_prompt",
]

#: Capabilities the adapter must report as ``verified`` before a managed task
#: may be handed to it.  A partially verified capability is not enough.
REQUIRED_CAPABILITIES: tuple[RuntimeCapability, ...] = (
    RuntimeCapability.TASK_SUBMIT,
    RuntimeCapability.TASK_STATUS,
)

#: Stable reason recorded when the capability preflight fails.  It is a fixed
#: code (the task stores it as ``error_code``); :attr:`DispatcherResult.detail`
#: names the missing capabilities for the caller's logs.
RUNTIME_CAPABILITY_UNVERIFIED = "runtime_capability_unverified"

#: Stable reason recorded when the persisted Grant does not match the persisted
#: decision it claims to derive from.  The adapter is never called.
DECISION_BINDING_MISMATCH = "decision_binding_mismatch"

#: Stable reason recorded when the public Grant serializer rejects the grant
#: (capabilities/scope/TTL/budget/gates not derivable from the decision).  The
#: adapter is never called.
GRANT_DOCUMENT_INVALID = "grant_document_invalid"

#: Stable reason recorded when the runtime accepted a submission but the
#: binding raised.  The task enters ``unknown``; it is never retried in place.
RUNTIME_BIND_FAILED = "runtime_bind_failed"

#: Stable reason recorded when an accepted result carried no usable handle.
RUNTIME_SUBMIT_HANDLE_INVALID = "runtime_submit_handle_invalid"

#: Stable reason recorded when the governed goal/payload cannot be rendered as
#: canonical JSON.  The task is rejected locally instead of sending junk.
RUNTIME_PROMPT_INVALID = "runtime_prompt_invalid"

#: Stable reason recorded when the governed goal/payload cannot fit the prompt
#: limit.  The task is rejected locally instead of sending an unbounded prompt.
RUNTIME_PROMPT_TOO_LARGE = "runtime_prompt_too_large"

#: Hard upper bound for the canonical UTF-8 runtime prompt.  A governed task
#: whose goal/payload exceeds this is not submitted; 16 KiB keeps the request
#: well inside the runtime boundary's per-request budget while leaving ample
#: room for a normal task document.
MAX_RUNTIME_PROMPT_BYTES = 16 * 1024

#: The database constraint for a safe runtime handle.  Checking it here keeps a
#: malformed adapter reply out of the task row (where it would raise an
#: ``IntegrityError`` after the work was already accepted).
_RUNTIME_TASK_ID_PATTERN = re.compile(
    r"^rtask_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
)

#: Principal types allowed to write Managed Execution facts.
_SERVICE_PRINCIPAL_TYPES = frozenset({"service", "runtime"})

#: Key-name fragments (normalised to lower-case alphanumerics) that mark a
#: value as a credential.  Such keys are dropped from the prompt rather than
#: forwarded to the runtime; the governed payload is not a secret channel.
_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "accesstoken",
    "accesskey",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "sessionkey",
    "token",
)


class RuntimePromptTooLarge(ValueError):
    """The canonical governed prompt exceeds :data:`MAX_RUNTIME_PROMPT_BYTES`.

    The caller must not send it; it records a local rejection instead.  The
    message only contains sizes, never prompt content.
    """

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(
            f"runtime prompt is {size} bytes; the limit is {limit}"
        )


def build_runtime_idempotency_key(
    task: object, grant: object, attempt_number: int
) -> str:
    """Return the stable runtime submission key for one attempt of one Grant.

    The key is derived only from server-owned identifiers (task id, grant id)
    and the attempt number, so the same attempt retried after a crash reuses
    the same key (:class:`~app.runtime.contract.RuntimeAdapter` idempotency),
    while a new Grant or a new attempt gets a different key.  A client never
    supplies any part of it.
    """

    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
    ):
        raise ValueError("attempt_number must be a positive integer")
    task_id = getattr(task, "task_id", None)
    grant_id = getattr(grant, "grant_id", None)
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task must have a non-empty task_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise ValueError("grant must have a non-empty grant_id")

    material = (
        "agentmgr.runtime.submit.v1\n"
        f"{task_id}\n{grant_id}\n{attempt_number}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    # ``idem_`` + 40 hex characters stays inside the repository's 64-char
    # idempotency-key bounds and is collision-safe for this namespace.
    return "idem_" + digest[:40]


def build_runtime_prompt(task: object) -> str:
    """Render the governed ``goal``/``payload`` as a canonical runtime prompt.

    The output is RFC 8785 JCS canonical JSON (UTF-8, sorted keys, no
    whitespace), built only from the persisted, already-governed task columns.
    No identity, authority, session or credential field is added.  Nested keys
    that name a credential are dropped so a stray secret in the governed
    payload can never cross the runtime boundary.

    Raises:
        RuntimePromptTooLarge: the canonical prompt exceeds the fixed bound.
        TypeError: the persisted columns are not JSON-shaped (fail closed).
    """

    goal = getattr(task, "goal", None) or {}
    payload = getattr(task, "payload", None) or {}
    if not isinstance(goal, Mapping) or not isinstance(payload, Mapping):
        raise TypeError("task goal and payload must be JSON objects")

    prompt = canonical_json(
        {
            "goal": _redact_credentials(goal),
            "payload": _redact_credentials(payload),
        }
    )
    size = len(prompt.encode("utf-8"))
    if size > MAX_RUNTIME_PROMPT_BYTES:
        raise RuntimePromptTooLarge(size, MAX_RUNTIME_PROMPT_BYTES)
    return prompt


def _redact_credentials(value: Any) -> Any:
    """Return a JSON-shaped copy with credential-named object keys removed."""

    if isinstance(value, Mapping):
        return {
            key: _redact_credentials(item)
            for key, item in value.items()
            if not (isinstance(key, str) and _is_sensitive_key(key))
        }
    if isinstance(value, (list, tuple)):
        return [_redact_credentials(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalised = "".join(char for char in key.lower() if char.isalnum())
    return any(fragment in normalised for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _valid_runtime_task_id(value: object) -> bool:
    """Return whether ``value`` is a schema-safe ``rtask_*`` handle."""

    return isinstance(value, str) and bool(
        _RUNTIME_TASK_ID_PATTERN.fullmatch(value)
    )


def _rfc3339(value: datetime) -> str:
    """Render a dispatcher-owned timestamp without host-specific formatting."""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DispatcherConfig:
    """One dispatcher's identity and pacing.

    ``worker_id`` is the lease owner.  One dispatcher handles one task at a
    time; parallel workers are only safe on *different* tasks.
    """

    worker_id: str
    lease_seconds: int = 300
    poll_interval: float = 0.5
    health_backoff_seconds: float = 5.0


@dataclass(frozen=True)
class DispatcherResult:
    """Stable, non-sensitive outcome of one dispatch pass.

    Outcomes produced so far: ``idle``, ``accepted`` (submitted and bound, or
    a run found by lookup and adopted after an ambiguous submit),
    ``unavailable`` (invalid/revoked/expired Grant or decision-binding
    mismatch), ``unsupported`` (capability gap or adapter unsupported),
    ``adapter_unavailable`` (health/degraded), ``rejected`` (definite local or
    adapter rejection), ``timeout`` (submit timed out and no run could be
    adopted) and ``unknown`` (ambiguous submission, conflict, or post-accept
    binding failure).

    ``detail`` is a short machine-readable code, never a payload, credential or
    runtime URL.
    """

    outcome: str
    task_id: str | None = None
    runtime_task_id: str | None = None
    detail: str | None = None


class RuntimeDispatcher:
    """Claim, submit and bind managed tasks for one :class:`RuntimeAdapter`."""

    def __init__(
        self,
        adapter: RuntimeAdapter,
        config: DispatcherConfig,
        *,
        service_principal: GovernancePrincipal,
        session: Session,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._service_principal = service_principal
        self._session = session

    # -- public API ----------------------------------------------------------

    def run_once(self, *, now: datetime | None = None) -> DispatcherResult:
        """Run one claim/preflight/submit/bind pass and commit its outcome.

        Unexpected exceptions (including an adapter that raises instead of
        returning a health/status fact) roll the whole pass back and propagate;
        a half-claimed lease is never left behind.
        """
        timestamp = now or _utcnow()
        try:
            self._require_service_principal()
            result = self._dispatch_one(timestamp)
        except Exception:
            self._session.rollback()
            raise
        self._session.commit()
        return result

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        session_factory: Callable[[], Session],
    ) -> None:
        """Poll :meth:`run_once` until ``stop_event`` is set.

        Minimal loop for now: one fresh session per iteration, a health backoff
        when the adapter is unavailable, and no parallel task execution.
        Lease recovery, error backoff and signal handling are completed with the
        cancellation/recovery task.
        """
        while not stop_event.is_set():
            with session_factory() as session:
                dispatcher = RuntimeDispatcher(
                    self._adapter,
                    self._config,
                    service_principal=self._service_principal,
                    session=session,
                )
                result = dispatcher.run_once()
            stop_event.wait(self._delay_for(result))

    # -- dispatch ------------------------------------------------------------

    def _dispatch_one(self, timestamp: datetime) -> DispatcherResult:
        claimed = worker.claim_task(
            self._session,
            worker_id=self._config.worker_id,
            actor=self._service_principal,
            execution_mode=MANAGED_EXECUTION_MODE,
            lease_seconds=self._config.lease_seconds,
            now=timestamp,
        )
        if claimed is None:
            return DispatcherResult(outcome="idle")
        task = claimed.task
        lease = claimed.lease

        # The authoritative Grant is loaded from persisted state only; a client
        # payload never supplies or widens it.
        grant = self._load_grant(task)
        try:
            runtime_grant = project_runtime_grant(task, grant, now=timestamp)
        except GrantProjectionError as exc:
            # No runtime call is made: the Grant does not prove a usable
            # managed authority for this task.
            self._record_failure(
                task,
                lease,
                outcome="unavailable",
                reason=exc.reason_code,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="unavailable",
                task_id=task.task_id,
                detail=exc.reason_code,
            )

        # The Grant is only as trustworthy as the decision it derives from.
        # Verify that binding from persisted rows *before* any adapter call,
        # so a mutated/forged Grant can never turn into a runtime submission.
        binding_mismatch = self._decision_binding_mismatch(
            task, grant, self._load_decision(task)
        )
        if binding_mismatch is not None:
            self._record_failure(
                task,
                lease,
                outcome="unavailable",
                reason=DECISION_BINDING_MISMATCH,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="unavailable",
                task_id=task.task_id,
                detail=f"{DECISION_BINDING_MISMATCH}:{binding_mismatch}",
            )

        # The decision checks above are structural; the public Grant serializer
        # additionally proves that the capabilities, resource scope, TTL,
        # budget and gates are all derivable from that persisted decision.  A
        # locally widened Grant must never reach the runtime.
        try:
            if grant is None:  # pragma: no cover - projection already failed
                raise GovernanceError("grant_missing")
            grant_document(grant)
        except GovernanceError as exc:
            self._record_failure(
                task,
                lease,
                outcome="unavailable",
                reason=GRANT_DOCUMENT_INVALID,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="unavailable",
                task_id=task.task_id,
                detail=f"{GRANT_DOCUMENT_INVALID}:{exc.reason_code}",
            )

        missing = self._unverified_capabilities()
        if missing:
            self._record_failure(
                task,
                lease,
                outcome="unsupported",
                reason=RUNTIME_CAPABILITY_UNVERIFIED,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="unsupported",
                task_id=task.task_id,
                detail=f"{RUNTIME_CAPABILITY_UNVERIFIED}:{','.join(missing)}",
            )

        health = self._adapter.health()
        if health.status is not HealthStatus.HEALTHY:
            # The lease is released and the task stays queued: nothing was
            # submitted, so the next pass (after a backoff) may retry it.
            self._release(task, lease, now=timestamp)
            return DispatcherResult(
                outcome="adapter_unavailable",
                task_id=task.task_id,
                detail=health.status.value,
            )

        attempt_number = task.attempt + 1
        idempotency_key = build_runtime_idempotency_key(
            task, grant, attempt_number
        )
        try:
            prompt = build_runtime_prompt(task)
        except RuntimePromptTooLarge:
            # Never send an unbounded prompt.  No runtime call was made, so the
            # never-dispatched rejection path applies.
            self._record_failure(
                task,
                lease,
                outcome="rejected",
                reason=RUNTIME_PROMPT_TOO_LARGE,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="rejected",
                task_id=task.task_id,
                detail=RUNTIME_PROMPT_TOO_LARGE,
            )
        except (TypeError, ValueError):
            # A governed document that is not canonical JSON can never be sent
            # safely.  Reject it locally instead of poisoning every retry with
            # the same exception.
            self._record_failure(
                task,
                lease,
                outcome="rejected",
                reason=RUNTIME_PROMPT_INVALID,
                now=timestamp,
            )
            return DispatcherResult(
                outcome="rejected",
                task_id=task.task_id,
                detail=RUNTIME_PROMPT_INVALID,
            )

        submission = self._adapter.submit_task(
            task.task_id, prompt, idempotency_key, runtime_grant
        )
        if submission.outcome is SubmitOutcome.ACCEPTED:
            return self._bind_accepted(
                task,
                lease,
                submission,
                idempotency_key=idempotency_key,
                now=timestamp,
            )

        # A definite refusal is recorded locally; an ambiguous or conflicting
        # answer is reconciled through the idempotency key before anything else.
        # Nothing on this path resubmits.
        return self._reconcile_submit_outcome(
            task,
            lease,
            submission,
            idempotency_key=idempotency_key,
            now=timestamp,
        )

    # -- helpers -------------------------------------------------------------

    def _require_service_principal(self) -> None:
        """Fail closed before claiming unless the actor is a trusted service.

        A person (or agent instance) never dispatches Managed Execution: the
        lease/fencing producer for a runtime boundary is a service/runtime
        principal.
        """
        principal = self._service_principal
        if principal.principal_type not in _SERVICE_PRINCIPAL_TYPES:
            raise TaskError(
                "task_unauthorized_actor",
                "the dispatcher must run as a trusted service/runtime principal",
                context={
                    "principal_id": principal.principal_id,
                    "principal_type": principal.principal_type,
                },
            )
        principal.require_active()

    def _load_grant(self, task: Task) -> ExecutionGrant | None:
        if not task.grant_id:
            return None
        return self._session.scalar(
            select(ExecutionGrant).where(
                ExecutionGrant.grant_id == task.grant_id
            )
        )

    def _load_decision(self, task: Task) -> GovernanceDecisionRecord | None:
        if not task.decision_id:
            return None
        return self._session.scalar(
            select(GovernanceDecisionRecord).where(
                GovernanceDecisionRecord.decision_id == task.decision_id
            )
        )

    def _decision_binding_mismatch(
        self,
        task: Task,
        grant: ExecutionGrant | None,
        record: GovernanceDecisionRecord | None,
    ) -> str | None:
        """Return the safe field label when the Grant/decision binding fails.

        This is a persisted-state check only.  It runs before capability/health
        probes and before the key/prompt are built, so a mismatch can never
        reach the adapter.
        """

        if record is None:
            return "decision_missing"
        if record.task_id != task.task_id:
            return "decision_task_mismatch"
        if grant is None:
            return "grant_missing"
        if grant.decision_id != record.decision_id:
            return "grant_decision_mismatch"
        if grant.decision_hash != record.decision_hash:
            return "grant_decision_hash_mismatch"
        if grant.task_revision != record.task_revision:
            return "grant_task_revision_mismatch"
        return None

    def _bind_accepted(
        self,
        task: Task,
        lease: TaskLease,
        submission: SubmitResult,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> DispatcherResult:
        """Bind an accepted runtime handle to the task, or fail closed.

        The same bind is also the adoption path for a run found by idempotency
        lookup after a timeout/unknown submit: the caller passes a synthetic
        accepted result carrying the found ``rtask_*`` handle and the original
        key.

        The lease stays held after a successful bind so the later status/result
        work can keep writing execution facts under the same fencing token.
        """

        runtime_task_id = submission.runtime_task_id
        if not _valid_runtime_task_id(runtime_task_id):
            # The adapter says it accepted but its handle cannot be stored: the
            # row constraint would raise IntegrityError only after the runtime
            # had already started the work.  A malformed *string* is still a
            # cancel target and a reconciliation fact; anything else is not.
            cancel = None
            if isinstance(runtime_task_id, str) and runtime_task_id:
                cancel = self._cancel_after_accept(
                    runtime_task_id, idempotency_key, task
                )
            self._persist_reconciliation(
                task,
                runtime_task_id=(
                    runtime_task_id
                    if isinstance(runtime_task_id, str)
                    else None
                ),
                idempotency_key=idempotency_key,
                cancel=cancel,
                reason=RUNTIME_SUBMIT_HANDLE_INVALID,
                now=now,
            )
            self._record_failure(
                task,
                lease,
                outcome="unknown",
                reason=RUNTIME_SUBMIT_HANDLE_INVALID,
                now=now,
            )
            return DispatcherResult(
                outcome="unknown",
                task_id=task.task_id,
                detail=RUNTIME_SUBMIT_HANDLE_INVALID,
            )

        try:
            task_service.start_attempt(
                self._session,
                task_id=task.task_id,
                actor=self._service_principal,
                worker_id=self._config.worker_id,
                fencing_token=lease.fencing_token,
                runtime_ref=runtime_task_id,
                idempotency_key=idempotency_key,
                now=now,
            )
        except TaskError:
            # The runtime already accepted the work.  A lease/fencing/state
            # rejection must not be recorded as "rejected" (that would claim no
            # side effect) and must never trigger a second submit.  Best-effort
            # cancel, persist the accepted handle/key for reconciliation, then
            # enter the honest unknown state.
            cancel = self._cancel_after_accept(
                runtime_task_id, idempotency_key, task
            )
            self._persist_reconciliation(
                task,
                runtime_task_id=runtime_task_id,
                idempotency_key=idempotency_key,
                cancel=cancel,
                reason=RUNTIME_BIND_FAILED,
                now=now,
            )
            self._record_failure(
                task,
                lease,
                outcome="unknown",
                reason=RUNTIME_BIND_FAILED,
                now=now,
            )
            return DispatcherResult(
                outcome="unknown",
                task_id=task.task_id,
                runtime_task_id=runtime_task_id,
                detail=RUNTIME_BIND_FAILED,
            )

        return DispatcherResult(
            outcome="accepted",
            task_id=task.task_id,
            runtime_task_id=runtime_task_id,
        )

    def _cancel_after_accept(
        self,
        runtime_task_id: str,
        idempotency_key: str,
        task: Task,
    ) -> dict[str, object]:
        """Ask the runtime to stop a submission we could not bind.

        Cancellation is best effort by contract: a ``requested`` result is not
        a stop, and a failed call must not hide the unknown outcome.  The
        returned fact is folded into the persisted reconciliation record so a
        later reconciler knows whether a stop is still owed.
        """

        try:
            result = self._adapter.cancel_task(
                runtime_task_id,
                reason=RUNTIME_BIND_FAILED,
                idempotency_key=idempotency_key,
                task_id=task.task_id,
            )
        except Exception:  # noqa: BLE001 - best-effort stop; unknown is recorded
            # The runtime may already be unreachable.  The handle/key remain in
            # the task result, so reconciliation can retry the stop.
            return {
                "attempted": True,
                "outcome": "error",
                "stop_confirmed": False,
            }
        return {
            "attempted": True,
            "outcome": result.outcome.value,
            "stop_confirmed": bool(result.stop_confirmed),
        }

    def _persist_reconciliation(
        self,
        task: Task,
        *,
        runtime_task_id: str | None,
        idempotency_key: str,
        cancel: Mapping[str, object] | None,
        reason: str,
        now: datetime,
        lookup: LookupResult | None = None,
        submit_outcome: str | None = None,
    ) -> None:
        """Persist the accepted-but-unbound handle before entering unknown.

        ``record_dispatch_failure`` merges the existing result document, so
        these keys survive the transition.  Only the opaque handle, the stable
        key, cancellation facts, the lookup answer and a reason are stored --
        never a prompt, token or credential.
        """

        document: dict[str, object] = {
            "runtime_task_id": runtime_task_id,
            "runtime_key": idempotency_key,
            "cancel_requested_at": (
                _rfc3339(now)
                if cancel is not None and cancel.get("attempted")
                else None
            ),
            "cancel_outcome": (cancel or {}).get("outcome"),
            "stop_confirmed": bool((cancel or {}).get("stop_confirmed", False)),
            "reason": reason,
        }
        if lookup is not None:
            document["lookup_outcome"] = lookup.outcome.value
        if submit_outcome is not None:
            document["submit_outcome"] = submit_outcome
        previous = (task.result or {}).get("runtime_reconciliation")
        if isinstance(previous, Mapping):
            # A follow-up fact in the same pass (e.g. a found handle that could
            # not be bound) keeps the lookup/adoption evidence already gathered.
            for key in ("lookup_outcome", "submit_outcome"):
                if key in previous and key not in document:
                    document[key] = previous[key]
        task.result = {
            **dict(task.result or {}),
            "runtime_reconciliation": document,
        }

    def _reconcile_submit_outcome(
        self,
        task: Task,
        lease: TaskLease,
        submission: SubmitResult,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> DispatcherResult:
        """Map a non-accepted submit outcome without ever resubmitting.

        ``rejected``/``unsupported`` are definite answers: they are recorded
        and no lookup is made.  ``timeout``/``unknown``/``conflict`` mean the
        runtime may already own a run for this key, so the adapter is asked
        first.  A ``found`` run after ``timeout``/``unknown`` is adopted
        through the same bind the ``accepted`` path uses.  A ``conflict`` can
        never be adopted: the runtime has just told us the key is bound to a
        *different* payload, so binding it would attest to effects of the
        wrong input.
        """

        if submission.outcome is SubmitOutcome.REJECTED:
            # Definite refusal with no side effect: no lookup and no retry.
            return self._refuse_submit(
                task, lease, submission, outcome="rejected", now=now
            )
        if submission.outcome is SubmitOutcome.UNSUPPORTED:
            # Definite capability gap: needs_human, no lookup and no retry.
            return self._refuse_submit(
                task, lease, submission, outcome="unsupported", now=now
            )

        reason = f"runtime_submit_{submission.outcome.value}"
        lookup = self._lookup_submission(task, idempotency_key)
        if (
            submission.outcome is not SubmitOutcome.CONFLICT
            and lookup.outcome is LookupOutcome.FOUND
        ):
            # The runtime already created the run whose reply we lost.
            # Adopting its handle is the only correct move; a resubmit would
            # create a second run.  The reconciliation block records it.
            self._persist_reconciliation(
                task,
                runtime_task_id=self._handle_or_none(lookup.runtime_task_id),
                idempotency_key=idempotency_key,
                cancel=None,
                reason=reason,
                now=now,
                lookup=lookup,
                submit_outcome=submission.outcome.value,
            )
            return self._bind_accepted(
                task,
                lease,
                SubmitResult(
                    outcome=SubmitOutcome.ACCEPTED,
                    runtime_task_id=lookup.runtime_task_id,
                    accepted_at=lookup.accepted_at,
                    reported_state=lookup.reported_state,
                ),
                idempotency_key=idempotency_key,
                now=now,
            )

        # No adoptable run could be proven (lookup not_found/unknown/conflict,
        # or a submit conflict with a different payload).  ``not_found`` is a
        # definite query answer, but the adapter contract has no "no side
        # effects" guarantee field, so it is *not* a retry licence here: the
        # task enters unknown, keeps the handle/key for reconciliation and is
        # never resubmitted with this Grant.
        self._persist_reconciliation(
            task,
            runtime_task_id=self._handle_or_none(lookup.runtime_task_id),
            idempotency_key=idempotency_key,
            cancel=None,
            reason=reason,
            now=now,
            lookup=lookup,
            submit_outcome=submission.outcome.value,
        )
        outcome = (
            "timeout"
            if submission.outcome is SubmitOutcome.TIMEOUT
            else "unknown"
        )
        self._record_failure(
            task, lease, outcome=outcome, reason=reason, now=now
        )
        return DispatcherResult(
            outcome=outcome,
            task_id=task.task_id,
            detail=reason,
        )

    def _refuse_submit(
        self,
        task: Task,
        lease: TaskLease,
        submission: SubmitResult,
        *,
        outcome: str,
        now: datetime,
    ) -> DispatcherResult:
        """Record a definite (side-effect-free) submit refusal."""

        reason = f"runtime_submit_{submission.outcome.value}"
        self._record_failure(
            task, lease, outcome=outcome, reason=reason, now=now
        )
        return DispatcherResult(
            outcome=outcome,
            task_id=task.task_id,
            detail=reason,
        )

    def _lookup_submission(
        self, task: Task, idempotency_key: str
    ) -> LookupResult:
        """Ask the adapter which run (if any) this key already created.

        No ``payload_hash`` is passed: the dispatcher does not own the
        adapter's canonical request body (an adapter may wrap the prompt), so
        only the adapter can compare hashes.  A lookup that raises is treated
        exactly like ``unknown`` -- the dispatcher cannot prove there is no
        run, so it must not resubmit and must not lose the claim to a rollback.
        """

        try:
            return self._adapter.find_by_idempotency_key(
                task.task_id,
                task.grant_id,
                idempotency_key,
            )
        except Exception:  # noqa: BLE001 - cannot prove absence
            return LookupResult(outcome=LookupOutcome.UNKNOWN)

    @staticmethod
    def _handle_or_none(value: object) -> str | None:
        """Keep only a string handle in the reconciliation document."""

        return value if isinstance(value, str) else None

    def _unverified_capabilities(self) -> tuple[str, ...]:
        snapshot = self._adapter.capabilities()
        missing: list[str] = []
        for name in REQUIRED_CAPABILITIES:
            capability = snapshot.capability(name)
            if (
                capability is None
                or capability.status is not CapabilityStatus.VERIFIED
            ):
                missing.append(name.value)
        return tuple(missing)

    def _record_failure(
        self,
        task: Task,
        lease: TaskLease,
        *,
        outcome: str,
        reason: str,
        now: datetime,
    ) -> None:
        """Record one dispatch outcome and release the claim."""
        task_service.record_dispatch_failure(
            self._session,
            task_id=task.task_id,
            actor=self._service_principal,
            outcome=outcome,
            worker_id=self._config.worker_id,
            fencing_token=lease.fencing_token,
            reason=reason,
            now=now,
        )
        self._release(task, lease, now=now)

    def _release(self, task: Task, lease: TaskLease, *, now: datetime) -> None:
        worker.release_lease(
            self._session,
            task_id=task.task_id,
            worker_id=self._config.worker_id,
            fencing_token=lease.fencing_token,
            now=now,
        )

    def _delay_for(self, result: DispatcherResult) -> float:
        if result.outcome == "adapter_unavailable":
            return self._config.health_backoff_seconds
        return self._config.poll_interval
