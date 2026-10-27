"""Managed Execution dispatcher: claim, preflight, submit, bind and drive.

This module is the production caller that connects the task/ExecutionGrant core
to the frozen :class:`app.runtime.contract.RuntimeAdapter` protocol.  It only
ever claims ``execution_mode="managed_execution"`` work; advisory and governed
tasks stay client/PEP driven.

Scope of the current implementation
-----------------------------------
One dispatch pass owns one task from ``queued`` to a terminal/``verifying``
fact, inside one lease:

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
    :func:`app.tasks.service.start_attempt` and keep the lease for the status
    work in the same pass.  If binding is rejected after acceptance, best-effort
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
    ``timeout``/``unknown``, the Grant is revoked and nothing is resubmitted;
12. drive the bound run through ``adapter.get_status``/``adapter.get_result``
    under the same lease/fencing token (see :meth:`RuntimeDispatcher._drive_accepted`):
    ``running`` is recorded through ``mark_running`` with a hashed start
    evidence ref, a ``completed`` run is only accepted when it carries a
    core-compatible ``result_*`` plus at least one ``evidence:*`` reference and
    ends in ``verifying``, a confirmed ``failed``/``timed_out`` run is recorded
    as such, ``unknown``/``not_found`` fail closed to ``unknown`` with the Grant
    revoked, and ``waiting_input`` is cancelled best-effort: a confirmed stop
    escalates to ``needs_human``, an unconfirmed one fails closed to ``unknown``.

Core-driven cancellation
-----------------------
A task a participant/service cancelled is in ``cancel_requested``.  The drive
observes that state because it **commits after the bind and after every status
observation** (see "Transaction boundary"), so an external ``request_cancel``
can commit while the run is being driven.  When it sees it:

* ``adapter.cancel_task`` is called exactly once and the cancel facts are
  persisted; ``requested`` is **not** a stop and the drive keeps observing.
* a runtime ``stop_confirmed`` is **evidence, not business reconciliation**.  A
  confirmed ``stopped``/``failed``/``timed_out`` observation saves the hashed
  stop evidence in the reconciliation document and escalates the task to
  ``needs_human`` (T52) for a human to reconcile.  The dispatcher never calls
  ``confirm_stop`` and never writes ``cancelled``/``side_effects_reconciled``
  from runtime facts alone.
* ``already_terminal`` and ``completed`` follow the normal result path: a run
  that finished before the cancellation took effect still reaches
  ``verifying`` with its ``result_*``/``evidence:*`` instead of being swallowed
  by the cancel.
* ``unsupported``/``unknown`` (or an adapter exception) never produce
  ``cancelled``: the task stays honest (``cancel_requested`` until the drive
  ends, then ``unknown`` with the cancel facts persisted).

Lease recovery
--------------
:meth:`RuntimeDispatcher.recover_leases` wires
:class:`RuntimeRecoveryChecker` into
:func:`app.tasks.worker.recover_expired_leases` in committed batches: the
worker's fixed per-call window is drained until the backlog is empty, so a
persistent set of expired leases cannot starve later rows.  The adapter is
observed **before** the worker takes its task/lease row locks and the worker is
handed a cached, no-network checker; an external ``cancel_requested`` can
therefore commit while a recovery observation is in flight instead of blocking
on the row lock.
:meth:`RuntimeDispatcher.reconcile_cancel_requests` handles
``cancel_requested`` rows whose lease expired or whose process crashed, and
:meth:`RuntimeDispatcher.cancel_unknown_runs` best-effort stops runs of tasks
that are already ``unknown`` but still carry a persisted ``rtask_*`` handle.
The checker answers only from adapter facts and only for managed tasks: a bound
handle is observed with ``get_status`` (a live run is reported as ``running``
and the worker never replays it).  It never answers ``not_accepted``: the
adapter contract has no explicit no-side-effect guarantee, and a missing run is
not proof that none can still land, so every lookup answer other than a usable
``found`` handle is ``unknown``.  None of these paths ever submits again.

Managed state writes are fenced.  ``escalate_task`` carries no worker/fencing
argument, so :meth:`RuntimeDispatcher._escalate_with_stop` re-reads and verifies
the current lease under its row lock before writing evidence or escalating; a
stale pass raises ``task_fencing_token_stale`` and rolls back.  Recovery-lease
document writes are checked the same way before they are staged.

The drive is **time/lease bounded**, not observation bounded: it waits
``poll_interval`` between observations, renews the lease at roughly half its
life, and stops at the run's remaining ``max_runtime_seconds`` deadline.  A run
that hits the deadline (or the observation cap) is cancelled best-effort: a
confirmed stop saves evidence and escalates ``needs_human``, anything else
records ``lost`` before releasing the lease.  A lost lease rolls the current
unit of work back so the stable idempotency key lets lease recovery adopt the
same run.

Command line
------------
:func:`main` is the process entry point (``python -m app.tasks.execution``).
It exposes only ``--once``/``--loop`` and an ``--adapter <name>`` looked up in a
trusted launcher registry; it never imports a module named by an argument and
has **no** default production adapter.  Without an injected
``dispatcher_factory`` it exits nonzero with
:data:`NO_DISPATCH_CONFIGURATION` (#97 owns the real Hermes configuration), and
an unknown name exits with :data:`UNKNOWN_ADAPTER`.  The Fake adapter exists
only in tests.  See ``docs/operations/runtime-dispatch.md`` for start/stop,
lease/lifecycle, failure and recovery operations.

This module never calls ``task_service.complete_task``: a runtime self-report
can never become ``succeeded``.  The verification boundary is
:mod:`app.tasks.verification`, which independently rejects runtime/agent
self-report evidence.

Transaction boundary
--------------------
``run_once`` uses the **injected** session.  It commits successful and
domain-rejected work and rolls back unexpected exceptions; the caller still owns
``close``.  Two adapter failures are deliberately swallowed because they prove
nothing and a rollback would make an accepted run look unsubmitted:

* after an ambiguous submission, a lookup that raises is treated exactly like
  ``unknown``;
* a status/result call that raises is treated as an unreadable fact and the
  attempt fails closed to ``unknown``.

The drive **never holds the task/lease row lock across an observation or a
sleep**.  After the bind it commits (releasing the claim lock), commits after
every status/cancel fact and every lease renewal, and reloads the task under a
fresh transaction before deciding.  That is what makes a concurrent
``request_cancel`` from another connection able to commit and become visible
while the run is still being driven.

A **lease renewal failure is not swallowed**: no fact may be written under a
stale fencing token, so the current unit of work rolls back and lease recovery
reconciles the run (it may still be running; nothing is replayed).
:meth:`RuntimeDispatcher.run_forever` creates a fresh session from the supplied
factory for every iteration and commits the recovery/cancel passes separately
from the dispatch pass, so recovery work is never rolled back with a failing
dispatch.  A stop signal observed **while a run is being driven** raises a
private shutdown exception after rolling the current unit of work back: the
runtime is left alone (it is not a core cancel) and the still-live lease lets
lease recovery reconcile the run; the stable idempotency key prevents a second
runtime task.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.errors import GovernanceError
from app.governance.grants import grant_document
from app.governance.models import ExecutionGrant, GovernanceDecisionRecord
from app.governance.policies import canonical_json
from app.governance.principal import GovernancePrincipal
from app.runtime.contract import (
    CancelOutcome,
    CapabilityStatus,
    HealthStatus,
    LookupOutcome,
    LookupResult,
    RuntimeAdapter,
    RuntimeCapability,
    RuntimeResult,
    RuntimeState,
    StatusResult,
    SubmitOutcome,
    SubmitResult,
)
from app.runtime.projection import (
    MANAGED_EXECUTION_MODE,
    GrantProjectionError,
    project_runtime_grant,
)
from app.tasks import service as task_service
from app.tasks import verification
from app.tasks import worker
from app.tasks.errors import TaskError, TaskStateConflict
from app.tasks.models import Task, TaskAttempt, TaskLease

__all__ = [
    "DECISION_BINDING_MISMATCH",
    "GRANT_DOCUMENT_INVALID",
    "MAX_RUNTIME_PROMPT_BYTES",
    "REQUIRED_CAPABILITIES",
    "RUNTIME_BIND_FAILED",
    "RUNTIME_CANCEL_RECONCILE",
    "RUNTIME_CANCEL_REQUESTED",
    "RUNTIME_CAPABILITY_UNVERIFIED",
    "RUNTIME_DRIVE_DEADLINE",
    "RUNTIME_DRIVE_OBSERVATION_CAP",
    "RUNTIME_EXECUTION_FAILED",
    "RUNTIME_EXECUTION_LOST",
    "RUNTIME_EXECUTION_TIMED_OUT",
    "RUNTIME_FACT_REFUSED",
    "RUNTIME_HANDLE_MISMATCH",
    "RUNTIME_PROMPT_INVALID",
    "RUNTIME_PROMPT_TOO_LARGE",
    "RUNTIME_RESULT_UNREADABLE",
    "RUNTIME_RESULT_UNUSABLE",
    "RUNTIME_STATUS_UNREADABLE",
    "RUNTIME_STOP_CONFIRMED",
    "RUNTIME_STOP_UNCONFIRMED",
    "RUNTIME_STOP_UNREQUESTED",
    "RUNTIME_SUBMIT_HANDLE_INVALID",
    "RUNTIME_WAITING_INPUT",
    "DispatcherConfig",
    "DispatcherResult",
    "RuntimeDispatcher",
    "RuntimePromptTooLarge",
    "RuntimeRecoveryChecker",
    "build_runtime_idempotency_key",
    "build_runtime_prompt",
    "main",
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

#: Stable reason recorded when a completed run carries no core-compatible
#: ``result_*``/``evidence:*`` pair.  Refs are never synthesized.
RUNTIME_RESULT_UNUSABLE = "runtime_result_unusable"

#: Stable reason recorded when the runtime result cannot be read at all.
RUNTIME_RESULT_UNREADABLE = "runtime_result_unreadable"

#: Stable reason recorded when the task service refuses a completed fact (for
#: example the Grant expired while the run was executing).  The dispatcher
#: cannot overrule the core's authority check, so the attempt fails closed
#: instead of rolling the bind back and replaying the submission forever.
RUNTIME_FACT_REFUSED = "runtime_fact_refused"

#: Stable reason recorded when the runtime status cannot be read at all.
RUNTIME_STATUS_UNREADABLE = "runtime_status_unreadable"

#: Stable reason recorded when a failure/timed-out state has no confirmed stop.
#: A stop is never fabricated from the status alone.
RUNTIME_STOP_UNCONFIRMED = "runtime_stop_unconfirmed"

#: Stable reason recorded when the runtime reports ``stopped`` although this
#: pass never requested a cancellation (no ``cancel_requested`` task state).
RUNTIME_STOP_UNREQUESTED = "runtime_stop_unrequested"

#: Stable escalation reason recorded when a runtime-confirmed stop hands the
#: task to a human.  A runtime stop proves no further runtime actions, not that
#: business side effects are reconciled; only a person may decide that.
RUNTIME_STOP_CONFIRMED = "runtime_stop_confirmed"

#: Stable reason sent to the adapter when a core ``cancel_requested`` task is
#: being driven.  ``requested`` is not a stop; the dispatcher keeps observing.
RUNTIME_CANCEL_REQUESTED = "runtime_cancel_requested"

#: Stable reconciliation reason for the best-effort stop of a task that is
#: already ``unknown`` but still carries a persisted ``rtask_*`` handle.
RUNTIME_CANCEL_RECONCILE = "runtime_cancel_reconcile"

#: Stable reason recorded when the runtime waits for input, which #96 does not
#: support: the run is cancelled best-effort, a confirmed stop escalates the
#: task to ``needs_human`` and an unconfirmed one fails closed to ``unknown``.
RUNTIME_WAITING_INPUT = "runtime_waiting_input_unsupported"

#: Stable reason recorded when the run's execution evidence is lost/unknown.
RUNTIME_EXECUTION_LOST = "runtime_execution_lost"

#: Stable reason recorded when the drive loop reaches the run's runtime deadline
#: and the run is still in progress.  The dispatcher cancels best-effort: a
#: confirmed stop is saved and escalated to a human, otherwise the attempt is
#: recorded ``lost``.  It never leaves a live lease on an in-progress task.
RUNTIME_DRIVE_DEADLINE = "runtime_drive_deadline"

#: Stable reason recorded when a test-only observation cap stops the drive while
#: the run is still in progress.  Production never sets that cap.
RUNTIME_DRIVE_OBSERVATION_CAP = "runtime_drive_observation_cap"

#: Stable reason recorded when the adapter reports a status for another handle.
#: Nothing from that observation is attributed to this task.
RUNTIME_HANDLE_MISMATCH = "runtime_status_handle_mismatch"

#: Stable error codes persisted for a confirmed failed/timed-out run.
RUNTIME_EXECUTION_FAILED = "runtime_execution_failed"
RUNTIME_EXECUTION_TIMED_OUT = "runtime_execution_timed_out"

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

#: Module logger.  Only stable codes and outcome names are logged: never a
#: prompt, credential, task payload, host path or runtime-private URL.
logger = logging.getLogger(__name__)

#: Stable stderr message when no trusted launcher/configuration was supplied.
#: The CLI deliberately has **no** default production adapter: #97 owns the
#: real (Hermes) managed runtime and its deployment configuration.
NO_DISPATCH_CONFIGURATION = "#97 production dispatch configuration not supplied"

#: Stable stderr prefix for an adapter name the trusted registry does not know.
UNKNOWN_ADAPTER = "unknown adapter"

#: Errors a long-running loop retries with a bounded backoff instead of
#: exiting.  Both are deterministic *domain* rejections (state conflicts,
#: lease/fencing refusals, governance denials) that a later pass can recover
#: from.  Programming errors (``TypeError``, ``AttributeError``, ...) are not
#: caught here: a bug must surface, not be retried forever.
RECOVERABLE_LOOP_ERRORS: tuple[type[BaseException], ...] = (
    TaskError,
    GovernanceError,
)

#: Rows recovered per ``worker.recover_expired_leases`` call by
#: :meth:`RuntimeDispatcher.recover_leases`.  The pass drains batches until the
#: backlog is empty, so this is a memory/transaction bound, not a starvation
#: window: a row behind the first batch is reached in the same pass.  Keep it
#: small: each batch commits once and holds its row locks until then (the
#: adapter observation itself already happens outside the lock).
_RECOVERY_BATCH_SIZE = 10


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
    whose name matches a credential fragment are dropped as a **best-effort**
    defence: this is name-based redaction, not content inspection, so a secret
    placed in an ordinary-looking value is not detected.  The governed payload
    must not carry credentials in the first place.

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


def _runtime_start_evidence_ref(
    runtime_task_id: str, observed_at: datetime
) -> str:
    """Return the hashed ``evidence:runtime_start_*`` ref for a running fact.

    The reference is derived only from the opaque handle the adapter already
    returned and the adapter-owned observation time.  It carries no prompt,
    credential or vendor URL, and it matches the frozen ``evidence:*`` grammar.
    """

    material = (
        "agentmgr.runtime.start.v1\n"
        f"{runtime_task_id}\n{_rfc3339(observed_at)}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "evidence:runtime_start_" + digest[:24]


def _runtime_stop_evidence_ref(
    runtime_task_id: str, observed_at: datetime, source: str
) -> str:
    """Return the hashed ``evidence:runtime_stop_*`` ref for a confirmed stop.

    Same contract as :func:`_runtime_start_evidence_ref`: the reference is a
    digest of the opaque handle, the adapter observation time and the fact that
    confirmed it (cancel response or status).  It never carries a vendor reason
    string, path or credential.
    """

    material = (
        "agentmgr.runtime.stop.v1\n"
        f"{runtime_task_id}\n{source}\n{_rfc3339(observed_at)}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "evidence:runtime_stop_" + digest[:24]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Treat a naive DB timestamp as UTC (the columns are timezone-aware)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class _RecoverySnapshot:
    """Detached task facts used for a network observation outside a lock.

    :class:`RuntimeRecoveryChecker` only reads these attributes; a full ORM row
    must not be held (or lazily reloaded) while an adapter call is in flight.
    """

    task_id: str
    state: str
    runtime_task_id: str | None
    grant_id: str | None
    execution_mode: str


@dataclass(frozen=True)
class DispatcherConfig:
    """One dispatcher's identity and pacing.

    ``worker_id`` is the lease owner.  One dispatcher handles one task at a
    time; parallel workers are only safe on *different* tasks.

    ``poll_interval`` is the wait between two status observations of the same
    run (and the delay between idle passes); tests set it to ``0``.
    ``lease_seconds`` is renewed at roughly half its life while a run is being
    driven, so the fencing token stays current until the run's own runtime
    deadline.

    ``max_status_observations`` is a hard safety bound for the drive loop and
    exists for tests: production leaves it ``None`` and relies on the run's
    own runtime budget and the live lease.  A test sets a small value so an
    unexpected non-terminal script fails closed quickly instead of sleeping out
    a 300 s runtime budget.

    ``error_backoff_seconds``/``max_error_backoff_seconds`` pace
    :meth:`RuntimeDispatcher.run_forever` after a recoverable domain/lease
    error.  The delay doubles per consecutive failing pass and is capped, so a
    persistent fault cannot busy-loop the worker.
    """

    worker_id: str
    lease_seconds: int = 300
    poll_interval: float = 0.5
    health_backoff_seconds: float = 5.0
    error_backoff_seconds: float = 5.0
    max_error_backoff_seconds: float = 60.0
    max_status_observations: int | None = None


@dataclass(frozen=True)
class DispatcherResult:
    """Stable, non-sensitive outcome of one dispatch pass.

    Outcomes returned by :meth:`RuntimeDispatcher.run_once`: ``idle``;
    ``verifying`` (the run reported completion and the task now waits for
    independent verification); ``failed``/``timed_out`` (a runtime-reported,
    confirmed failure from a live run); ``needs_human`` (a runtime-confirmed
    stop, an input-waiting run, or a ``cancel_requested`` row handed to a human
    for reconciliation); ``unavailable`` (invalid/revoked/expired Grant or
    decision-binding mismatch); ``unsupported`` (capability gap or adapter
    unsupported); ``adapter_unavailable`` (health/degraded); ``rejected``
    (definite local or adapter rejection); ``timeout`` (submit timed out and no
    run could be adopted) and ``unknown`` (ambiguous submission, conflict,
    post-accept binding failure, or an execution fact the core cannot map
    honestly).

    The reconciliation passes return the same vocabulary: ``needs_human`` /
    ``verifying`` / ``unknown`` from
    :meth:`RuntimeDispatcher.reconcile_cancel_requests`, and ``unknown`` from
    :meth:`RuntimeDispatcher.cancel_unknown_runs`.

    ``cancelled`` is deliberately **not** an outcome: a runtime
    ``stop_confirmed`` is evidence that no further runtime actions happen, not
    that business side effects are reconciled.  A confirmed stop saves the
    evidence and escalates ``needs_human``; only a person (T63) may write
    ``cancelled``.

    ``running`` is deliberately **not** an outcome: a live run is driven to a
    terminal/``verifying`` fact (or the fail-closed path) inside the same pass.

    ``accepted`` is internal to the bind step: a pass that binds a run always
    continues into one of the drive outcomes above, so a bound run is never
    returned as merely "accepted".

    No outcome is ever ``succeeded``: only :mod:`app.tasks.verification` can
    move a task out of ``verifying``.

    ``detail`` is a short machine-readable code, never a payload, credential or
    runtime URL.
    """

    outcome: str
    task_id: str | None = None
    runtime_task_id: str | None = None
    detail: str | None = None


class _DispatcherShutdown(Exception):
    """Internal: the stop event fired while a run was still in progress.

    Raised only between observations, never while a fact is being written.  It
    propagates out of :meth:`RuntimeDispatcher.run_once` (whose rollback leaves
    the task claimable) and is caught by
    :meth:`RuntimeDispatcher.run_forever`, which returns.  The runtime itself
    is not cancelled: shutting the dispatcher down is not a core cancel
    request, and the stable idempotency key lets the next process adopt the
    same run without creating a second one.

    Trade-off: the rolled-back unit of work leaves the claim lease live until
    its ``lease_expires_at``, so the task is **not** claimable by another
    worker for up to ``lease_seconds``.  Shortening or releasing that lease
    here would make a still-running task look abandoned and invite a replay;
    expiry plus lease recovery is the deliberate behaviour documented in
    ``docs/operations/runtime-dispatch.md``.
    """


class RuntimeRecoveryChecker:
    """Answer :class:`app.tasks.worker.RecoveryChecker` from adapter facts.

    The worker asks this object what happened to a submission whose lease
    expired.  It must never guess and never replay:

    * a **bound** ``rtask_*`` handle is authoritative: ``get_status`` is asked,
      and a live run answers ``("running", False)`` -- the worker enters
      ``unknown`` instead of re-dispatching it.  A confirmed
      ``failed``/``timed_out`` answers ``("failed", True)`` so the worker can
      close the attempt with stop evidence.  A status for another handle, an
      unreadable status or any other state answers ``("unknown", False)``; the
      handle stays on the task for later reconciliation.
    * without a bound handle, only a persisted idempotency key can be looked
      up.  ``found`` re-observes the returned handle (or answers ``unknown``
      when the lookup carries no usable one).  **Every other lookup answer,
      including ``not_found``, is ``("unknown", False)``.**

    ``not_accepted`` is deliberately never returned.  The Runtime Adapter
    contract has no explicit "no side effects" guarantee, and a missing run
    today does not prove one was never created elsewhere (or that a late
    submission cannot still land).  The worker treats ``not_accepted`` as a
    licence to roll the attempt back and requeue with the same Grant/key, so
    claiming it from a mere ``not_found`` would risk a replay.  Only an
    explicit contract guarantee (a future field) may change this.

    The checker is deliberately passive: it makes no writes, never submits,
    and never cancels.  It only answers for ``managed_execution`` tasks: a
    non-managed row has no runtime boundary that this dispatcher may reason
    about, so it always answers ``("unknown", False)``.  Best-effort
    cancellation for a task that is already ``unknown`` belongs to
    :meth:`RuntimeDispatcher.cancel_unknown_runs`, where a real lease can be
    acquired for the reconciliation write.
    """

    def __init__(self, adapter: RuntimeAdapter) -> None:
        self._adapter = adapter

    def __call__(
        self,
        *,
        task: object,
        attempt_number: int,
        idempotency_key: str | None,
    ) -> tuple[str, bool]:
        """Return ``(outcome, stop_confirmed)`` for the worker's recovery pass.

        ``attempt_number`` is unused on purpose: this object only reports what
        the adapter proves, and the worker's own T33 guard decides whether the
        retry budget may be rolled back.
        """

        if (
            getattr(task, "execution_mode", None)
            != MANAGED_EXECUTION_MODE
        ):
            # Not a managed run: nothing here may be replayed or observed.
            return ("unknown", False)
        task_id = getattr(task, "task_id", "")
        handle = getattr(task, "runtime_task_id", None)
        if handle:
            if not _valid_runtime_task_id(handle):
                # A handle exists but cannot be observed: absence must not be
                # inferred (and nothing may be replayed).
                return ("unknown", False)
            return self._observe_status(str(task_id), handle)
        if not idempotency_key:
            return ("unknown", False)
        try:
            lookup = self._adapter.find_by_idempotency_key(
                str(task_id),
                getattr(task, "grant_id", "") or "",
                idempotency_key,
            )
        except Exception:  # noqa: BLE001 - unavailable lookup proves nothing
            return ("unknown", False)
        if lookup.outcome is LookupOutcome.FOUND:
            found = lookup.runtime_task_id
            if not _valid_runtime_task_id(found):
                # The run exists but cannot be observed: never adopt/replay.
                return ("unknown", False)
            return self._observe_status(str(task_id), found)
        # ``not_found`` (and every other answer) is not proof of no side
        # effects: never hand the worker a rollback/requeue licence.
        return ("unknown", False)

    def _observe_status(
        self, task_id: str, runtime_task_id: str
    ) -> tuple[str, bool]:
        """Map one bound run's status onto the worker's outcome vocabulary."""

        try:
            status = self._adapter.get_status(
                runtime_task_id, task_id=task_id or None
            )
        except Exception:  # noqa: BLE001 - no fact can be proven
            return ("unknown", False)
        if status.runtime_task_id != runtime_task_id:
            # A status about another run proves nothing about this task.
            return ("unknown", False)
        if (
            status.state in (RuntimeState.FAILED, RuntimeState.TIMED_OUT)
            and status.stop_confirmed
        ):
            return ("failed", True)
        if status.state in (RuntimeState.ACCEPTED, RuntimeState.RUNNING):
            # Still live: never "not accepted", never replayed.
            return ("running", False)
        return ("unknown", False)


class _CachedRecoveryChecker:
    """Replay prefetched observations without any network call.

    :func:`app.tasks.worker.recover_expired_leases` calls its checker while the
    task and lease rows are locked ``FOR UPDATE``.  A network call there would
    block an external ``request_cancel`` for the length of the adapter call and
    stall the recovery loop.  :meth:`RuntimeDispatcher.recover_leases` therefore
    observes the adapter **outside** the lock and passes this object under it.

    The cache entry is keyed by the persisted attempt identity and carries the
    pre-observation ``(state, runtime_task_id)`` fingerprint.  If the row
    changed between the unlocked observation and the fenced recovery, the
    checker answers ``("unknown", False)``: a stale runtime fact is never
    applied, and nothing is replayed.
    """

    def __init__(
        self,
        observations: Mapping[
            tuple[str, int, str | None],
            tuple[tuple[str, str | None], str, bool],
        ],
    ) -> None:
        self._observations = dict(observations)

    def __call__(
        self,
        *,
        task: object,
        attempt_number: int,
        idempotency_key: str | None,
    ) -> tuple[str, bool]:
        key = (
            str(getattr(task, "task_id", "")),
            int(attempt_number),
            idempotency_key,
        )
        entry = self._observations.get(key)
        if entry is None:
            # Not prefetched (new/expired-now lease): fail closed.  The next
            # loop iteration observes it before acquiring any lock.
            return ("unknown", False)
        fingerprint, outcome, stop_confirmed = entry
        if fingerprint != (
            getattr(task, "state", None),
            getattr(task, "runtime_task_id", None),
        ):
            # The row moved after the observation; do not apply a stale fact.
            return ("unknown", False)
        return (outcome, stop_confirmed)


class RuntimeDispatcher:
    """Claim, submit and bind managed tasks for one :class:`RuntimeAdapter`.

    ``session`` is the caller-owned session used by :meth:`run_once` and the
    reconciliation passes.  ``session_factory`` is optional and only needed by
    :meth:`run_forever` (a long-running process opens one fresh session per
    iteration); a launcher that only calls :meth:`run_once` can omit it.
    """

    def __init__(
        self,
        adapter: RuntimeAdapter,
        config: DispatcherConfig,
        *,
        service_principal: GovernancePrincipal,
        session: Session,
        stop_event: threading.Event | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._service_principal = service_principal
        self._session = session
        #: Optional shutdown signal.  ``run_forever`` supplies its own event;
        #: a single pass behaves exactly as before without one.
        self._stop_event = stop_event
        #: Optional fresh-session source for :meth:`run_forever`.  It is a
        #: trusted launcher value (never task/client input) and must return a
        #: new :class:`~sqlalchemy.orm.Session` bound to the same database.
        self._session_factory = session_factory

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
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        """Poll :meth:`run_once` until ``stop_event`` is set.

        One fresh session per iteration, no parallel task execution, and a
        health backoff when the adapter is unavailable.  Each iteration runs
        four independently committed passes:

        1. :meth:`recover_leases` -- expired leases are recovered through
           :class:`RuntimeRecoveryChecker`, so a crashed submission either
           rolls back (only with a definite "never accepted") or enters
           ``unknown``; a live runtime is never replayed.
        2. :meth:`reconcile_cancel_requests` -- stranded ``cancel_requested``
           rows (crashed/expired lease) are cancelled once under a recovery
           lease and reach an honest ``needs_human``/``verifying``/``unknown``
           outcome instead of staying pending forever.
        3. :meth:`run_once` -- one claim/submit/drive pass (which commits after
           every observation, so an external cancel becomes visible).
        4. :meth:`cancel_unknown_runs` -- best-effort stop for tasks that are
           already ``unknown`` but still carry a ``rtask_*`` handle.

        A stop signal observed mid-drive is honoured between observations: the
        current unit of work rolls back (no fact under a half-driven run), the
        still-live lease lets lease recovery reconcile the run, and this loop
        returns.  The runtime is not cancelled and the lease is left to expire
        (see :class:`_DispatcherShutdown`).

        A recoverable domain/lease error (:data:`RECOVERABLE_LOOP_ERRORS`) does
        not kill the loop: the current session is rolled back, a stable error
        code is logged, and the next pass waits an exponentially growing,
        capped ``error_backoff_seconds`` delay.  Programming errors still
        propagate so a bug is not retried forever.
        """

        factory = session_factory or self._session_factory
        if factory is None:
            raise ValueError(
                "run_forever requires a session_factory (constructor or call)"
            )

        consecutive_errors = 0
        while not stop_event.is_set():
            delay: float
            with factory() as session:
                dispatcher = RuntimeDispatcher(
                    self._adapter,
                    self._config,
                    service_principal=self._service_principal,
                    session=session,
                    stop_event=stop_event,
                )
                try:
                    dispatcher.recover_leases()
                    dispatcher.reconcile_cancel_requests()
                    result = dispatcher.run_once()
                    dispatcher.cancel_unknown_runs()
                except _DispatcherShutdown:
                    return
                except RECOVERABLE_LOOP_ERRORS as exc:
                    # A domain/lease rejection proves nothing about the runtime
                    # and must not end the worker.  Only the stable reason code
                    # (or the exception type) is logged -- never the message,
                    # context or a task payload.
                    session.rollback()
                    consecutive_errors += 1
                    code = (
                        getattr(exc, "reason_code", None)
                        or type(exc).__name__
                    )
                    delay = self._error_delay(consecutive_errors)
                    logger.warning(
                        "runtime_dispatch_pass_error code=%s "
                        "consecutive_errors=%d backoff_seconds=%.3f",
                        code,
                        consecutive_errors,
                        delay,
                    )
                else:
                    consecutive_errors = 0
                    delay = self._delay_for(result)
            stop_event.wait(delay)

    def recover_leases(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[worker.LeaseRecovery]:
        """Recover expired managed leases in committed batches.

        The adapter is observed **outside** every task/lease row lock:
        :func:`app.tasks.worker.recover_expired_leases` calls its checker while
        holding ``FOR UPDATE``, so this pass prefetches the runtime facts first,
        then lets the worker apply them under the lock through a no-network
        :class:`_CachedRecoveryChecker`.  An external ``request_cancel`` can
        therefore commit during a recovery observation instead of blocking on
        the row lock.

        The worker scans a fixed window per call, so a single call with a small
        ``limit`` would starve every row behind the first window.  This pass
        drains the backlog: each batch is committed before the next read, and
        the loop stops when a batch recovers nothing (a lease skipped because
        another pass holds it cannot make this spin) or when an explicit
        ``limit`` is reached.

        ``limit=None`` (the default) means every expired lease.  An explicit
        ``limit`` is a hard bound: each batch asks for at most the remaining
        budget, so the pass never recovers more than ``limit`` rows and
        ``limit=0`` performs no recovery at all.  Only ``managed_execution``
        rows are recovered; the generic worker pool owns the other modes.  The
        actor is the injected service principal.
        """

        recovered: list[worker.LeaseRecovery] = []
        remaining = None if limit is None else max(0, int(limit))
        if remaining == 0:
            return recovered
        timestamp = now or _utcnow()
        try:
            self._require_service_principal()
            while True:
                batch_size = (
                    _RECOVERY_BATCH_SIZE
                    if remaining is None
                    else min(_RECOVERY_BATCH_SIZE, remaining)
                )
                candidates, checker = self._prefetch_recovery_observations(
                    now=timestamp, limit=batch_size
                )
                batch = worker.recover_expired_leases(
                    self._session,
                    actor=self._service_principal,
                    checker=checker,
                    now=timestamp,
                    limit=batch_size,
                    execution_mode=MANAGED_EXECUTION_MODE,
                    candidates=candidates,
                )
                self._session.commit()
                recovered.extend(batch)
                if not batch:
                    break
                if remaining is not None:
                    remaining -= len(batch)
                    if remaining <= 0:
                        break
        except Exception:
            self._session.rollback()
            raise
        return recovered

    def _prefetch_recovery_observations(
        self, *, now: datetime, limit: int
    ) -> tuple[list[tuple[int, str]], _CachedRecoveryChecker]:
        """Observe the adapter for one expired-lease batch, outside any lock.

        Reads the candidate identities and the persisted attempt context
        without ``FOR UPDATE``, closes that read transaction, calls the adapter
        with detached snapshots, and returns the exact candidate list plus a
        cached checker.  The worker then re-locks and re-verifies every row; a
        row that changed in between is answered ``unknown`` by the cache
        fingerprint instead of receiving a stale runtime fact.
        """

        timestamp = now or _utcnow()
        candidates = list(
            self._session.execute(
                select(TaskLease.id, TaskLease.task_id)
                .join(Task, Task.task_id == TaskLease.task_id)
                .where(
                    TaskLease.released_at.is_(None),
                    TaskLease.lease_expires_at <= timestamp,
                    Task.execution_mode == MANAGED_EXECUTION_MODE,
                )
                .order_by(TaskLease.id)
                .limit(limit)
            )
        )
        snapshots: list[
            tuple[_RecoverySnapshot, int, str | None]
        ] = []
        for _lease_id, task_id in candidates:
            task = self._session.scalar(
                select(Task).where(Task.task_id == task_id)
            )
            if task is None:
                continue
            attempt = self._load_attempt(task)
            snapshots.append(
                (
                    _RecoverySnapshot(
                        task_id=task.task_id,
                        state=task.state,
                        runtime_task_id=task.runtime_task_id,
                        grant_id=task.grant_id,
                        execution_mode=task.execution_mode,
                    ),
                    attempt.attempt_number if attempt is not None else 0,
                    attempt.idempotency_key if attempt is not None else None,
                )
            )
        # Close the read transaction before any adapter call: no lock and no
        # snapshot is held while the network observation is in flight.
        self._session.commit()
        checker = RuntimeRecoveryChecker(self._adapter)
        observations: dict[
            tuple[str, int, str | None],
            tuple[tuple[str, str | None], str, bool],
        ] = {}
        for snapshot, attempt_number, idempotency_key in snapshots:
            if snapshot.state != "dispatched":
                # The worker never consults the checker for a
                # running/verifying/other row; do not observe the adapter for
                # a fact that cannot be applied.
                continue
            outcome, stop_confirmed = checker(
                task=snapshot,
                attempt_number=attempt_number,
                idempotency_key=idempotency_key,
            )
            observations[
                (snapshot.task_id, attempt_number, idempotency_key)
            ] = (
                (snapshot.state, snapshot.runtime_task_id),
                outcome,
                stop_confirmed,
            )
        return candidates, _CachedRecoveryChecker(observations)

    def reconcile_cancel_requests(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[DispatcherResult]:
        """Reconcile ``cancel_requested`` rows that no driving pass owns.

        A cancel can land on a row whose worker crashed (or whose lease
        expired) *before* the drive observed it.  ``claim_task`` never returns
        such a row, so without this pass it would stay ``cancel_requested``
        forever.  Each candidate is fenced with a fresh recovery lease (a live
        lease means a driving pass still owns it and it is skipped), then:

        * a persisted runtime handle is cancelled once and its status observed;
        * ``completed`` follows the normal result path to ``verifying`` (a
          finished result is never swallowed by the cancel);
        * a confirmed ``stopped``/``failed``/``timed_out`` saves the hashed
          stop evidence and escalates ``needs_human`` (T52) -- a runtime
          stop is never business reconciliation and never writes
          ``cancelled``;
        * ``unknown``/``not_found`` records ``lost`` (T51) -> ``unknown``;
        * a live/unconfirmed runtime, or a cancel that never got a runtime
          handle, is escalated ``needs_human`` so the row cannot stay silently
          pending.

        A bounded scan (``limit=None`` means all) keeps a large backlog from
        starving later rows, and every write happens under the recovery lease.
        ``limit=0`` scans nothing (same hard-bound semantics as
        :meth:`recover_leases`).
        """

        timestamp = now or _utcnow()
        results: list[DispatcherResult] = []
        if limit is not None and int(limit) <= 0:
            return results
        self._require_service_principal()
        try:
            query = (
                select(Task.task_id)
                .where(
                    Task.state == "cancel_requested",
                    Task.execution_mode == MANAGED_EXECUTION_MODE,
                )
                .order_by(Task.id)
            )
            if limit is not None:
                query = query.limit(int(limit))
            for task_id in list(self._session.scalars(query)):
                result = self._reconcile_one_cancel_request(
                    task_id, now=timestamp
                )
                if result is not None:
                    results.append(result)
                # Commit per row: a later failure must not roll back facts
                # already reconciled, and no row lock is held across a
                # runtime observation.
                self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return results

    def cancel_unknown_runs(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[DispatcherResult]:
        """Best-effort stop for ``unknown`` tasks that still carry a handle.

        Only ``task.state == "unknown"`` triggers this: a ``queued`` or
        ``verifying`` task is *not* cancelled merely because a
        ``runtime_reconciliation`` block exists.  The cancel and its status
        re-observation run under a fresh recovery lease, so the reconciliation
        write is fenced (never an unfenced ``task.result`` write).  A confirmed
        stop only saves evidence: the task **stays unknown** for a human to
        reconcile, because a runtime stop is not business reconciliation.  A
        confirmed stop that was already persisted is left alone.

        ``limit=None`` scans every unknown managed row so a fixed 50-row window
        cannot starve newer rows.  ``limit=0`` scans nothing (same hard-bound
        semantics as :meth:`recover_leases`).
        """

        timestamp = now or _utcnow()
        results: list[DispatcherResult] = []
        if limit is not None and int(limit) <= 0:
            return results
        self._require_service_principal()
        try:
            query = (
                select(Task.task_id)
                .where(
                    Task.state == "unknown",
                    Task.execution_mode == MANAGED_EXECUTION_MODE,
                )
                .order_by(Task.id)
            )
            if limit is not None:
                query = query.limit(int(limit))
            for task_id in list(self._session.scalars(query)):
                result = self._cancel_one_unknown(task_id, now=timestamp)
                if result is not None:
                    results.append(result)
                self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return results

    def _reconcile_one_cancel_request(
        self, task_id: str, *, now: datetime
    ) -> DispatcherResult | None:
        """Reconcile one stranded ``cancel_requested`` row."""

        try:
            lease = task_service.acquire_recovery_lease(
                self._session,
                task_id=task_id,
                actor=self._service_principal,
                lease_seconds=self._config.lease_seconds,
                now=now,
            )
        except TaskError:
            # A live lease means a driving/recovery pass owns the row; a
            # non-cancellable state means it moved on.  Neither is ours.
            return None
        # Release the row locks acquired by the lease before observing the
        # runtime.  The fresh lease stays live, so no other recovery can take
        # the row while this pass works.
        self._session.commit()
        task = self._session.scalar(
            select(Task).where(Task.task_id == task_id)
        )
        if task is None or task.state != "cancel_requested":
            if task is not None:
                self._release(task, lease, now=now)
            return None
        previous = (task.result or {}).get("runtime_reconciliation")
        previous_doc = previous if isinstance(previous, Mapping) else {}
        handle = self._persisted_handle(task, previous_doc)
        attempt = self._load_attempt(task)
        key = self._attempt_key(attempt) or self._reconciliation_key(
            previous_doc
        )
        if handle is None:
            # No runtime run was ever bound: there is nothing to stop.  Do not
            # leave the row silently pending; a human decides what it means.
            return self._escalate_with_stop(
                task,
                lease,
                runtime_task_id=None,
                idempotency_key=key,
                cancel=None,
                reason=RUNTIME_CANCEL_REQUESTED,
                stop_ref=None,
                runtime_outcome=None,
                now=now,
            )

        cancel = self._cancel_after_accept(
            handle,
            key,
            task,
            reason=RUNTIME_CANCEL_REQUESTED,
        )
        self._require_current_lease(lease, now=now)
        self._persist_reconciliation(
            task,
            runtime_task_id=handle,
            idempotency_key=key,
            cancel=cancel,
            reason=RUNTIME_CANCEL_REQUESTED,
            now=now,
        )
        status = self._safe_status(task, handle)
        if status is not None:
            if status.state is RuntimeState.COMPLETED:
                return self._observe_completed(
                    task,
                    lease,
                    runtime_task_id=handle,
                    observed_at=status.observed_at,
                    now=now,
                )
            cancel_outcome = (cancel or {}).get("outcome")
            if status.state is not RuntimeState.TIMED_OUT and (
                cancel_outcome == CancelOutcome.ALREADY_TERMINAL.value
                or status.state in (RuntimeState.STOPPED, RuntimeState.FAILED)
            ):
                completed_result = self._verifiable_result(task, handle)
                if completed_result is not None:
                    return self._observe_completed(
                        task,
                        lease,
                        runtime_task_id=handle,
                        observed_at=status.observed_at,
                        now=now,
                        result=completed_result,
                    )
            if status.state in (RuntimeState.UNKNOWN, RuntimeState.NOT_FOUND):
                return self._record_lost(
                    task,
                    lease,
                    runtime_task_id=handle,
                    reason=RUNTIME_EXECUTION_LOST,
                    now=now,
                )

        stop_ref: str | None = None
        runtime_outcome: str | None = None
        if (
            status is not None
            and status.stop_confirmed
            and status.state
            in (
                RuntimeState.STOPPED,
                RuntimeState.FAILED,
                RuntimeState.TIMED_OUT,
            )
        ):
            stop_ref = _runtime_stop_evidence_ref(
                handle, status.observed_at, "status"
            )
            runtime_outcome = status.state.value
        elif bool(cancel.get("stop_confirmed")):
            stop_ref = _runtime_stop_evidence_ref(handle, now, "cancel")
            runtime_outcome = RuntimeState.STOPPED.value

        if stop_ref is not None:
            return self._escalate_with_stop(
                task,
                lease,
                runtime_task_id=handle,
                idempotency_key=key,
                cancel=cancel,
                reason=RUNTIME_STOP_CONFIRMED,
                stop_ref=stop_ref,
                runtime_outcome=runtime_outcome,
                now=now,
            )
        # The runtime is still live or did not confirm the stop.  A requested
        # cancel is not a stop, and the row must not stay pending forever, so
        # hand the cancel facts to a human (T52).
        return self._escalate_with_stop(
            task,
            lease,
            runtime_task_id=handle,
            idempotency_key=key,
            cancel=cancel,
            reason=RUNTIME_CANCEL_REQUESTED,
            stop_ref=None,
            runtime_outcome=None,
            now=now,
        )

    def _cancel_one_unknown(
        self, task_id: str, *, now: datetime
    ) -> DispatcherResult | None:
        """Best-effort stop for one ``unknown`` row, under a recovery lease."""

        task = self._session.scalar(
            select(Task).where(Task.task_id == task_id)
        )
        if task is None or task.state != "unknown":
            return None
        previous = (task.result or {}).get("runtime_reconciliation")
        previous_doc = previous if isinstance(previous, Mapping) else {}
        handle = self._persisted_handle(task, previous_doc)
        if handle is None:
            # No runtime run is owed anything.
            return None
        if previous_doc.get("stop_confirmed") is True:
            # Evidence is already saved; the row stays unknown for a human.
            return None
        try:
            lease = task_service.acquire_recovery_lease(
                self._session,
                task_id=task_id,
                actor=self._service_principal,
                lease_seconds=self._config.lease_seconds,
                now=now,
            )
        except TaskError:
            # Another worker still owns it (or it moved on): not ours.
            return None
        self._session.commit()
        task = self._session.scalar(
            select(Task).where(Task.task_id == task_id)
        )
        if task is None or task.state != "unknown":
            if task is not None:
                self._release(task, lease, now=now)
            return None
        previous = (task.result or {}).get("runtime_reconciliation")
        previous_doc = previous if isinstance(previous, Mapping) else {}
        handle = self._persisted_handle(task, previous_doc)
        if handle is None:
            self._release(task, lease, now=now)
            return None
        attempt = self._load_attempt(task)
        key = self._attempt_key(attempt) or self._reconciliation_key(
            previous_doc
        )
        cancel = self._cancel_after_accept(
            handle,
            key,
            task,
            reason=RUNTIME_CANCEL_RECONCILE,
        )
        original_reason = previous_doc.get("reason") or (
            task.result or {}
        ).get("error_code")
        reason = str(original_reason or RUNTIME_CANCEL_RECONCILE)
        self._require_current_lease(lease, now=now)
        self._persist_reconciliation(
            task,
            runtime_task_id=handle,
            idempotency_key=key,
            cancel=cancel,
            reason=reason,
            now=now,
        )
        stop_ref = self._confirmed_stop_ref(task, handle, cancel, now=now)
        if stop_ref is not None:
            evidence_ref, runtime_outcome = stop_ref
            # Runtime stop evidence only.  The task must stay ``unknown``: the
            # business side effects are a human decision (never auto-cancel).
            # The lease is re-checked after the status observation, which may
            # have outlived a short recovery lease.
            self._require_current_lease(lease, now=now)
            self._persist_reconciliation(
                task,
                runtime_task_id=handle,
                idempotency_key=key,
                cancel=cancel,
                reason=reason,
                stop_evidence_refs=(evidence_ref,),
                runtime_outcome=runtime_outcome,
                now=now,
            )
        self._release(task, lease, now=now)
        return DispatcherResult(
            outcome="unknown",
            task_id=task.task_id,
            runtime_task_id=handle,
            detail=RUNTIME_CANCEL_RECONCILE,
        )

    def _safe_status(
        self, task: Task, runtime_task_id: str
    ) -> StatusResult | None:
        """Read one status, or ``None`` when it cannot be attributed."""

        try:
            status = self._adapter.get_status(
                runtime_task_id, task_id=task.task_id
            )
        except Exception:  # noqa: BLE001 - no fact can be proven
            return None
        if status.runtime_task_id != runtime_task_id:
            return None
        return status

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
            return self._bind_and_drive(
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
                worker_id=lease.worker_id,
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

    def _bind_and_drive(
        self,
        task: Task,
        lease: TaskLease,
        submission: SubmitResult,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> DispatcherResult:
        """Bind an accepted handle, then drive it through status and result.

        A bound run is never re-claimed by :func:`app.tasks.worker.claim_task`
        (that only returns ``queued`` rows), so the pass that owns the lease is
        also the pass that must observe the runtime state.  A bind rejection is
        returned unchanged: it has already recorded its own fail-closed
        outcome, and nothing is polled for a handle the task does not own.
        """

        bound = self._bind_accepted(
            task, lease, submission, idempotency_key=idempotency_key, now=now
        )
        if bound.outcome != "accepted" or not bound.runtime_task_id:
            return bound
        return self._drive_accepted(
            task,
            lease,
            runtime_task_id=bound.runtime_task_id,
            idempotency_key=idempotency_key,
            now=now,
        )

    def _drive_accepted(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> DispatcherResult:
        """Drive the accepted run to a terminal/verifying fact, or fail closed.

        The loop is bounded by the run's own runtime budget
        (``task.max_runtime_seconds``) and, indirectly, by the live lease: the
        lease is renewed at roughly half its life, and a renewal failure stops
        the drive before another fact is written.  Between observations the
        dispatcher waits ``poll_interval`` and refreshes its clock, so a run
        that is not terminal immediately is still driven to an end instead of
        being stranded in ``dispatched``/``running`` with a live lease.

        Only contract enums and the existing task service are used; the
        dispatcher never writes a state directly, never calls
        ``complete_task`` and never invents result/evidence references.  When
        the deadline (or the test-only observation cap) is reached while the
        run is still in progress, the run is cancelled best-effort: a confirmed
        stop saves evidence and escalates ``needs_human``, anything else
        records ``lost`` before the lease is released.

        A task that a participant cancelled while it was running is in
        ``cancel_requested``.  Because this loop commits after every fact and
        reloads the task under a fresh transaction, an external
        ``request_cancel`` can commit while the drive is between observations.
        Before the next observation the dispatcher asks the runtime to stop it
        exactly once; a ``requested`` cancel is **not** a stop, so the loop
        keeps observing.  A confirmed stop is evidence, not reconciliation:
        the evidence is saved and the task is escalated to ``needs_human``.  A
        completion that raced the cancel still goes through the result path.
        """

        # Release the claim/bind row lock now.  From here on the task and
        # lease rows are only locked inside short service calls, so an
        # external ``request_cancel`` can commit while this pass observes the
        # runtime.
        self._session.commit()
        deadline = self._runtime_deadline(task, now=now)
        current = now
        observations = 0
        cancel_facts: dict[str, object] | None = None
        while True:
            # Renewing before a write keeps the fencing token valid for every
            # fact below; a lost lease raises and the current unit of work
            # rolls back (lease recovery reconciles the run).
            self._renew_lease_if_due(task, lease, now=current)
            # Reload under a fresh transaction: the previous iteration
            # committed, so a cancel requested by another connection is
            # visible here.
            self._session.refresh(task)
            if task.state == "cancel_requested" and cancel_facts is None:
                # The core has requested a stop: ask the runtime once.  The
                # cancel response is a request, not a stop, so no terminal fact
                # is written from it; the status observations below decide.
                cancel_facts = self._cancel_after_accept(
                    runtime_task_id,
                    idempotency_key,
                    task,
                    reason=RUNTIME_CANCEL_REQUESTED,
                )
                # The cancel response is a network fact; re-verify the lease
                # before staging the reconciliation document so a lease that
                # expired/released during the call cannot commit a stale fact.
                self._require_current_lease(lease, now=current)
                self._persist_reconciliation(
                    task,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    cancel=cancel_facts,
                    reason=RUNTIME_CANCEL_REQUESTED,
                    now=current,
                )
                # Persist the cancel fact before the runtime call/sleep: no
                # write may keep the task row locked across an observation.
                self._session.commit()
            try:
                status = self._adapter.get_status(
                    runtime_task_id, task_id=task.task_id
                )
            except Exception:  # noqa: BLE001 - the run's state cannot be proven
                # A status call that raises proves nothing about the effects the
                # runtime may already have produced.  Rolling back the bind
                # would make the accepted run look unsubmitted, so the attempt
                # fails closed to unknown instead.
                return self._record_lost(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    reason=RUNTIME_STATUS_UNREADABLE,
                    now=current,
                )
            observations += 1
            if status.runtime_task_id != runtime_task_id:
                # A status about another handle is not evidence about this
                # task; nothing from it may be attributed here.
                return self._record_lost(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    reason=RUNTIME_HANDLE_MISMATCH,
                    now=current,
                )

            # The task may have moved while the adapter call was in flight.
            self._session.refresh(task)
            # Captured before any adapter result/status work below: a cancel
            # that commits during those calls must make the running write
            # conflict instead of being absorbed by T49.
            expected_revision = task.state_revision
            state = status.state
            if task.state == "cancel_requested":
                # The cancel may have landed before *or* during this
                # observation; either way a completed/failed/stopped fact is
                # mapped by the cancel-aware branch so no legal edge is missed.
                decided = self._observe_cancelling(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    status=status,
                    cancel=cancel_facts,
                    now=current,
                )
                if decided is not None:
                    return decided
            elif state is RuntimeState.RUNNING:
                observed_state = self._ensure_running(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    observed_at=status.observed_at,
                    expected_revision=expected_revision,
                    now=current,
                )
                if observed_state == "cancel_requested":
                    # The cancel committed while this observation was being
                    # processed; never absorb it as "running".
                    decided = self._observe_cancelling(
                        task,
                        lease,
                        runtime_task_id=runtime_task_id,
                        idempotency_key=idempotency_key,
                        status=status,
                        cancel=cancel_facts,
                        now=current,
                    )
                    if decided is not None:
                        return decided
            elif state is RuntimeState.COMPLETED:
                return self._observe_completed(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    observed_at=status.observed_at,
                    expected_revision=expected_revision,
                    now=current,
                )
            elif state in (RuntimeState.FAILED, RuntimeState.TIMED_OUT):
                return self._observe_failure(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    status=status,
                    expected_revision=expected_revision,
                    now=current,
                )
            elif state is RuntimeState.WAITING_INPUT:
                return self._observe_waiting_input(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    now=current,
                )
            elif state is RuntimeState.STOPPED:
                return self._observe_unrequested_stop(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    status=status,
                    now=current,
                )
            elif state is not RuntimeState.ACCEPTED:
                # ``unknown`` / ``not_found`` (and any future state this task
                # does not map) prove nothing about the run's effects: no
                # resume/replay.
                return self._record_lost(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    reason=RUNTIME_EXECUTION_LOST,
                    now=current,
                )

            # The run is still in progress.  Never leave it behind: the runtime
            # deadline (or the test-only observation cap) ends the pass with a
            # reconciled and released attempt.
            if current >= deadline:
                return self._abandon_in_progress(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    reason=RUNTIME_DRIVE_DEADLINE,
                    cancel=cancel_facts,
                    now=current,
                )
            cap = self._config.max_status_observations
            if cap is not None and observations >= max(1, int(cap)):
                return self._abandon_in_progress(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    reason=RUNTIME_DRIVE_OBSERVATION_CAP,
                    cancel=cancel_facts,
                    now=current,
                )
            # Commit the in-progress fact (e.g. ``mark_running``) and end the
            # transaction *before* sleeping, so no task/lease row lock is held
            # across the poll interval.
            self._session.commit()
            if self._stop_event is not None and self._stop_event.is_set():
                # Shutting the dispatcher down is not a core cancel: leave the
                # runtime alone.  The still-live lease lets lease recovery
                # reconcile the run; the stable idempotency key prevents a
                # second runtime task.
                raise _DispatcherShutdown
            current = self._wait_for_next_observation(current)

    def _wait_for_next_observation(self, current: datetime) -> datetime:
        """Sleep ``poll_interval`` and return a strictly later clock value."""

        interval = max(0.0, float(self._config.poll_interval))
        if interval > 0:
            time.sleep(interval)
        observed = _utcnow()
        if observed > current:
            return observed
        # An injected clock that is ahead of the wall clock must still advance,
        # otherwise the deadline could never be reached.
        return current + timedelta(seconds=max(interval, 0.001))

    def _runtime_deadline(self, task: Task, *, now: datetime) -> datetime:
        """Return the time this pass must stop driving an in-progress run.

        The task runtime cap is cumulative across attempts, so the remaining
        budget is what ``start_attempt``/``record_attempt_result`` already
        enforce (:func:`app.tasks.service._attempt_runtime_elapsed`), not the
        raw column.  A run that outlives it must not be left running with a
        lease.
        """

        remaining = int(task.max_runtime_seconds or 0)
        elapsed = task_service._attempt_runtime_elapsed(  # noqa: SLF001 - same domain
            self._session, task, now=now
        )
        if elapsed is not None:
            remaining -= int(elapsed.total_seconds())
        return now + timedelta(seconds=max(1, remaining))

    def _renew_lease_if_due(
        self, task: Task, lease: TaskLease, *, now: datetime
    ) -> None:
        """Renew the lease at roughly half its life.

        Renewal is a precondition for every write below.  A failure (stale
        fencing token, released or expired lease) raises before another fact is
        written; ``run_once`` rolls the whole pass back, so the task returns to
        ``queued`` and the stable idempotency key lets the next pass (or lease
        recovery) adopt the same run instead of creating a second one.  The run
        is deliberately not cancelled here: another worker may already own it.
        """

        expires = lease.lease_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        lifetime = timedelta(seconds=max(1, int(self._config.lease_seconds)))
        if expires - now > lifetime / 2:
            return
        worker.renew_lease(
            self._session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            lease_seconds=self._config.lease_seconds,
            now=now,
        )
        # Commit immediately: the renewal must not keep the lease row lock
        # while the drive observes the runtime or sleeps, and it must already
        # be durable before the next fact write (which relies on it).
        self._session.commit()

    def _abandon_in_progress(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        idempotency_key: str,
        reason: str,
        now: datetime,
        cancel: Mapping[str, object] | None = None,
    ) -> DispatcherResult:
        """Cancel best-effort and leave the attempt in an honest state.

        Used when the drive deadline (or the test-only observation cap) is hit
        while the run is still in progress.  A confirmed stop is evidence that
        the runtime is over (not that business effects are reconciled), so it
        is saved and escalated to ``needs_human``; without a confirmed stop the
        attempt is recorded as ``lost``.  When the pass already requested a
        stop for a ``cancel_requested`` task the existing facts are reused
        instead of asking the runtime twice.
        """

        if cancel is None:
            cancel = self._cancel_after_accept(
                runtime_task_id, idempotency_key, task, reason=reason
            )
        confirmed = self._confirmed_stop_ref(
            task, runtime_task_id, cancel, now=now
        )
        if confirmed is not None:
            stop_ref, runtime_outcome = confirmed
            return self._escalate_with_stop(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                idempotency_key=idempotency_key,
                cancel=cancel,
                reason=reason,
                stop_ref=stop_ref,
                runtime_outcome=runtime_outcome,
                now=now,
            )
        self._require_current_lease(lease, now=now)
        self._persist_reconciliation(
            task,
            runtime_task_id=runtime_task_id,
            idempotency_key=idempotency_key,
            cancel=cancel,
            reason=reason,
            now=now,
        )
        return self._record_lost(
            task,
            lease,
            runtime_task_id=runtime_task_id,
            reason=reason,
            now=now,
        )

    def _escalate_with_stop(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str | None,
        idempotency_key: str,
        cancel: Mapping[str, object] | None,
        reason: str,
        stop_ref: str | None,
        runtime_outcome: str | None,
        now: datetime,
    ) -> DispatcherResult:
        """Save runtime stop evidence and hand the task to a human.

        A runtime ``stop_confirmed`` only proves that the runtime will not
        produce further actions; it says nothing about the business side
        effects the task asked for.  The dispatcher therefore never calls
        ``confirm_stop``/``side_effects_reconciled`` and never writes
        ``cancelled``; it persists the hashed evidence and escalates
        ``needs_human`` (T35/T40/T52) so a person can reconcile and then run
        T63.

        ``escalate_task`` cannot express a worker/fencing token, so the current
        lease is re-verified under its row lock *before* any fact is written.
        A stale pass raises ``task_fencing_token_stale`` and its transaction
        rolls back; the current owner keeps the row and no escalation is
        recorded from the stale observation.
        """

        self._require_current_lease(lease, now=now)
        self._persist_reconciliation(
            task,
            runtime_task_id=runtime_task_id,
            idempotency_key=idempotency_key,
            cancel=cancel,
            reason=reason,
            stop_evidence_refs=(stop_ref,) if stop_ref else (),
            runtime_outcome=runtime_outcome,
            now=now,
        )
        try:
            task_service.escalate_task(
                self._session,
                task_id=task.task_id,
                actor=self._service_principal,
                reason_code=reason,
                evidence_refs=(stop_ref,) if stop_ref else (),
                now=now,
            )
        except TaskError:
            # The core refused the escalation (for example the task moved on).
            # Rolling back would erase the accepted run, so fail closed.
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_FACT_REFUSED,
                now=now,
            )
        self._release(task, lease, now=now)
        return DispatcherResult(
            outcome="needs_human",
            task_id=task.task_id,
            runtime_task_id=runtime_task_id,
            detail=reason,
        )

    def _observe_unrequested_stop(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        idempotency_key: str,
        status: StatusResult,
        now: datetime,
    ) -> DispatcherResult:
        """Handle a ``stopped`` status this pass never requested.

        A confirmed stop is a real runtime fact, so it is saved and handed to a
        human (T40/T35) instead of being dropped.  Without confirmation the
        state is not a stop: the attempt fails closed to ``unknown``.
        """

        if not status.stop_confirmed:
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_STOP_UNCONFIRMED,
                now=now,
            )
        stop_ref = _runtime_stop_evidence_ref(
            runtime_task_id, status.observed_at, "status"
        )
        return self._escalate_with_stop(
            task,
            lease,
            runtime_task_id=runtime_task_id,
            idempotency_key=idempotency_key,
            cancel={
                "attempted": False,
                "outcome": RuntimeState.STOPPED.value,
                "stop_confirmed": True,
            },
            reason=RUNTIME_STOP_UNREQUESTED,
            stop_ref=stop_ref,
            runtime_outcome=RuntimeState.STOPPED.value,
            now=now,
        )

    def _confirmed_stop_ref(
        self,
        task: Task,
        runtime_task_id: str,
        cancel: Mapping[str, object],
        *,
        now: datetime,
    ) -> tuple[str, str] | None:
        """Return ``(evidence_ref, runtime_outcome)`` for a confirmed stop.

        ``stop_confirmed`` is trusted from the cancel response, or from one
        follow-up status when the cancel only reported ``requested``.  The
        reference is a hash of the observation, never a vendor string, and the
        outcome is the runtime's own confirming label (``stopped``/``failed``/
        ``timed_out``) so a reconciler never sees a hardcoded ``stopped`` for a
        run that actually failed.  When no stop is confirmed nothing is
        returned and no stop is claimed.
        """

        if bool(cancel.get("stop_confirmed")):
            return (
                _runtime_stop_evidence_ref(runtime_task_id, now, "cancel"),
                RuntimeState.STOPPED.value,
            )
        try:
            status = self._adapter.get_status(
                runtime_task_id, task_id=task.task_id
            )
        except Exception:  # noqa: BLE001 - no stop can be proven
            return None
        if (
            status.runtime_task_id != runtime_task_id
            or not status.stop_confirmed
            or status.state
            not in (
                RuntimeState.STOPPED,
                RuntimeState.FAILED,
                RuntimeState.TIMED_OUT,
            )
        ):
            return None
        return (
            _runtime_stop_evidence_ref(
                runtime_task_id, status.observed_at, "status"
            ),
            status.state.value,
        )

    def _ensure_running(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        observed_at: datetime,
        expected_revision: int | None,
        now: datetime,
    ) -> str:
        """Record ``dispatched -> running`` on fresh runtime evidence.

        A runtime may jump from ``accepted`` straight to a terminal state
        without ever reporting ``running``.  A terminal observation is itself
        proof that execution started, so the running fact is written first: it
        keeps the completion/failure edge legal and never claims more than the
        runtime proved.

        The write is guarded against a cancel that committed **while the
        adapter fact was being read**.  The in-memory task may still say
        ``dispatched`` although the authoritative row is already
        ``cancel_requested``; ``mark_running`` would then accept T49
        (``cancel_requested -> running``) and silently absorb the cancel.  The
        task row is therefore re-read under ``FOR UPDATE`` with
        ``populate_existing`` before the write, and ``expected_revision`` (from
        the post-observation refresh) turns any intervening state change into a
        :class:`TaskStateConflict`.  In that case nothing is written and the
        authoritative state is returned so the caller can route to the cancel
        branch.

        Returns the authoritative task state after the attempt: ``running`` on
        success, otherwise the state that prevented the write.
        """

        # Persist any staged reconciliation fact, then take the row lock and
        # replace the possibly stale identity-map attributes with the
        # authoritative row.  A plain reselect does not refresh a loaded ORM
        # object, so ``populate_existing`` is required here.
        self._session.flush()
        self._session.scalar(
            select(Task)
            .where(Task.task_id == task.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task.state != "dispatched":
            return task.state
        try:
            task_service.mark_running(
                self._session,
                task_id=task.task_id,
                actor=self._service_principal,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                evidence_ref=_runtime_start_evidence_ref(
                    runtime_task_id, observed_at
                ),
                expected_revision=(
                    expected_revision
                    if expected_revision is not None
                    else task.state_revision
                ),
                now=now,
            )
        except TaskStateConflict:
            # Another writer changed the row between the observation and this
            # write.  Re-read (still under the lock) and let the caller decide;
            # never fall through to a terminal failure from a stale state.
            self._session.scalar(
                select(Task)
                .where(Task.task_id == task.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            return task.state
        return "running"

    def _observe_completed(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        observed_at: datetime,
        now: datetime,
        expected_revision: int | None = None,
        result: RuntimeResult | None = None,
    ) -> DispatcherResult:
        """Persist a completed run as ``verifying``, or fail closed.

        ``verifying`` is the furthest the dispatcher may go.  A runtime that
        reports completion without a core-compatible ``result_*`` and at least
        one ``evidence:*`` reference is not a result the core can verify, and
        no reference is ever synthesized to make it fit.
        """

        if result is None:
            result = self._fetch_result(task, runtime_task_id)
        if result is None:
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_RESULT_UNREADABLE,
                now=now,
            )
        evidence_refs = tuple(result.evidence_refs or ())
        try:
            verification.validate_result_refs(
                result.result_ref, evidence_refs, require_result=True
            )
        except TaskError:
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_RESULT_UNUSABLE,
                now=now,
            )
        observed_state = self._ensure_running(
            task,
            lease,
            runtime_task_id=runtime_task_id,
            observed_at=observed_at,
            expected_revision=expected_revision,
            now=now,
        )
        if observed_state not in ("running", "cancel_requested"):
            # Another writer moved the row (recovery/cancel/terminal) while the
            # result was being fetched.  A second managed write from this stale
            # observation would either fail again or overwrite an honest fact,
            # so release what this pass still holds and fail closed.  A stale
            # lease raises here and the pass rolls back.
            self._release(task, lease, now=now)
            return DispatcherResult(
                outcome="unknown",
                task_id=task.task_id,
                runtime_task_id=runtime_task_id,
                detail=RUNTIME_FACT_REFUSED,
            )
        try:
            task_service.record_attempt_result(
                self._session,
                task_id=task.task_id,
                actor=self._service_principal,
                outcome="completed",
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                result_ref=result.result_ref,
                evidence_refs=evidence_refs,
                now=now,
            )
        except TaskError:
            # The core refused the completed fact (for example the Grant
            # expired while the run was executing).  Rolling the pass back
            # would make the accepted run look unsubmitted and replay the
            # submission forever, so the attempt fails closed instead.
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_FACT_REFUSED,
                now=now,
            )
        self._release(task, lease, now=now)
        return DispatcherResult(
            outcome="verifying",
            task_id=task.task_id,
            runtime_task_id=runtime_task_id,
        )

    def _observe_failure(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        idempotency_key: str,
        status: StatusResult,
        now: datetime,
        expected_revision: int | None = None,
    ) -> DispatcherResult:
        """Persist a confirmed ``failed``/``timed_out`` run, or fail closed.

        A failure without a confirmed stop is not a terminal fact this boundary
        may write, and a stop is never fabricated from the status alone.

        The first observation after a bind can already be terminal: a runtime
        may time out (or fail) before it ever reports ``running``.  The
        terminal fact is itself proof that execution started, so the running
        fact is written first -- exactly as the completion path does -- or the
        service would reject the ``dispatched -> timed_out`` edge.

        A cancel requested while this fact was being prepared is never
        displaced by a runtime-reported failure: if the task is
        ``cancel_requested`` at write time (or the authoritative write rejects
        because it moved there), the observation is re-mapped through the
        cancel policy (evidence + ``needs_human``), never written as a plain
        terminal failure.
        """

        state = status.state
        outcome = (
            "failed" if state is RuntimeState.FAILED else "timed_out"
        )
        if not status.stop_confirmed:
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_STOP_UNCONFIRMED,
                now=now,
            )
        result = self._fetch_result(task, runtime_task_id)
        if result is None:
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_RESULT_UNREADABLE,
                now=now,
            )
        evidence_refs = tuple(result.evidence_refs or ())
        try:
            verification.validate_result_refs(
                result.result_ref, evidence_refs, require_result=False
            )
        except TaskError:
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_RESULT_UNUSABLE,
                now=now,
            )
        self._ensure_running(
            task,
            lease,
            runtime_task_id=runtime_task_id,
            observed_at=status.observed_at,
            expected_revision=expected_revision,
            now=now,
        )
        if task.state == "cancel_requested":
            return self._observe_cancelling(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                idempotency_key=idempotency_key,
                status=status,
                cancel=None,
                now=now,
            )
        try:
            task_service.record_attempt_result(
                self._session,
                task_id=task.task_id,
                actor=self._service_principal,
                outcome=outcome,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                result_ref=result.result_ref,
                evidence_refs=evidence_refs,
                stop_confirmed=True,
                error_code=(
                    RUNTIME_EXECUTION_FAILED
                    if outcome == "failed"
                    else RUNTIME_EXECUTION_TIMED_OUT
                ),
                now=now,
            )
        except TaskError:
            # The authoritative state may have moved to ``cancel_requested``
            # after the check above.  Re-read and apply the cancel policy.
            self._session.refresh(task)
            if task.state == "cancel_requested":
                return self._observe_cancelling(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    idempotency_key=idempotency_key,
                    status=status,
                    cancel=None,
                    now=now,
                )
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_FACT_REFUSED,
                now=now,
            )
        self._release(task, lease, now=now)
        return DispatcherResult(
            outcome=outcome,
            task_id=task.task_id,
            runtime_task_id=runtime_task_id,
        )

    def _observe_cancelling(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        idempotency_key: str,
        status: StatusResult,
        cancel: Mapping[str, object] | None,
        now: datetime,
    ) -> DispatcherResult | None:
        """Map one observation of a ``cancel_requested`` run.

        Returns a result when the observation decides the pass; ``None`` means
        the run is still live (accepted/running/waiting_input, or a ``stopped``
        status that does not confirm the stop), so the drive keeps polling.

        A completion that raced the cancel wins: the result is fetched and the
        task enters ``verifying`` exactly as on the un-cancelled path -- a
        cancel must never swallow a finished result.  The adapter also answers
        ``already_terminal`` for a terminal run that never produced a readable
        artifact, so that outcome alone is not a completion: only a result the
        core verifier accepts (a ``result_*`` plus at least one ``evidence:*``
        reference) is promoted through T50.  A ``timed_out`` observation never
        promotes, even when a partial artifact exists.

        Otherwise a confirmed ``stopped``/``failed``/``timed_out`` is
        **evidence, not business reconciliation**: the hashed stop evidence is
        saved and the task is escalated to ``needs_human`` (T52).  The
        dispatcher never calls ``confirm_stop`` and never writes ``cancelled``
        from runtime facts.
        """

        state = status.state
        cancel_outcome = (cancel or {}).get("outcome")
        # A completed artifact wins a cancel race.  ``already_terminal`` alone
        # is ambiguous: the adapter also returns it for a run that was already
        # failed/timed out.  Only a core-verifiable result promotes the run to
        # ``verifying``; otherwise the normal cancel/failure policy applies.
        # A timeout keeps its own outcome even if a partial artifact exists.
        if state is not RuntimeState.TIMED_OUT and (
            cancel_outcome == CancelOutcome.ALREADY_TERMINAL.value
            or state in (RuntimeState.STOPPED, RuntimeState.FAILED)
        ):
            completed_result = self._verifiable_result(task, runtime_task_id)
            if completed_result is not None:
                return self._observe_completed(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    observed_at=status.observed_at,
                    now=now,
                    result=completed_result,
                )
        if state is RuntimeState.COMPLETED:
            return self._observe_completed(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                observed_at=status.observed_at,
                now=now,
            )
        if state in (
            RuntimeState.STOPPED,
            RuntimeState.FAILED,
            RuntimeState.TIMED_OUT,
        ):
            if not status.stop_confirmed:
                if state is RuntimeState.STOPPED:
                    # A terminal-looking state without stop confirmation is
                    # not a stop; keep observing rather than fabricating one.
                    return None
                return self._record_lost(
                    task,
                    lease,
                    runtime_task_id=runtime_task_id,
                    reason=RUNTIME_STOP_UNCONFIRMED,
                    now=now,
                )
            stop_ref = _runtime_stop_evidence_ref(
                runtime_task_id, status.observed_at, "status"
            )
            return self._escalate_with_stop(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                idempotency_key=idempotency_key,
                cancel=cancel,
                reason=RUNTIME_STOP_CONFIRMED,
                stop_ref=stop_ref,
                runtime_outcome=state.value,
                now=now,
            )
        if state in (RuntimeState.UNKNOWN, RuntimeState.NOT_FOUND):
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_EXECUTION_LOST,
                now=now,
            )
        # ``accepted``/``running``/``waiting_input``: still live.  The core
        # requested a stop; never mark it running again from here.
        return None

    def _observe_waiting_input(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> DispatcherResult:
        """Stop an input-waiting run best-effort and hand it to a human.

        #96 has no generic input channel, so the run must not be left waiting
        indefinitely.  When the runtime confirms the stop, the task is escalated
        to ``needs_human`` (T40, or T35 from ``dispatched``) with the stop
        observation as evidence: the run is over and the remaining decision
        belongs to a person.  A ``requested`` cancel is not a stop, so an
        unconfirmed stop still fails closed to ``unknown``.

        The confirmed stop is recorded as an observation reference; the core
        ``stop_confirmed`` flag is **not** written here: the core never
        requested this stop, so the run belongs to a person (``needs_human``)
        rather than being silently reclassified as a core-cancelled task.  The
        attempt therefore stays open exactly as the service defines for
        ``needs_human``.
        """

        cancel = self._cancel_after_accept(
            runtime_task_id,
            idempotency_key,
            task,
            reason=RUNTIME_WAITING_INPUT,
        )
        confirmed = self._confirmed_stop_ref(
            task, runtime_task_id, cancel, now=now
        )
        if confirmed is None:
            self._require_current_lease(lease, now=now)
            self._persist_reconciliation(
                task,
                runtime_task_id=runtime_task_id,
                idempotency_key=idempotency_key,
                cancel=cancel,
                reason=RUNTIME_WAITING_INPUT,
                now=now,
            )
            return self._record_lost(
                task,
                lease,
                runtime_task_id=runtime_task_id,
                reason=RUNTIME_WAITING_INPUT,
                now=now,
            )

        # A confirmed stop follows the shared escalation path (evidence +
        # ``needs_human``), including its fenced-lease check and the
        # TaskError -> fail-closed handling.  A refused escalation therefore
        # never leaves a half-written reconciliation document behind.
        stop_ref, runtime_outcome = confirmed
        return self._escalate_with_stop(
            task,
            lease,
            runtime_task_id=runtime_task_id,
            idempotency_key=idempotency_key,
            cancel=cancel,
            reason=RUNTIME_WAITING_INPUT,
            stop_ref=stop_ref,
            runtime_outcome=runtime_outcome,
            now=now,
        )

    def _load_attempt(self, task: Task) -> TaskAttempt | None:
        """Return the task's current attempt row (used for its stable key)."""

        return self._session.scalar(
            select(TaskAttempt)
            .where(TaskAttempt.task_id == task.task_id)
            .order_by(TaskAttempt.attempt_number.desc())
            .limit(1)
        )

    @staticmethod
    def _attempt_key(attempt: TaskAttempt | None) -> str:
        if attempt is None:
            return ""
        key = attempt.idempotency_key
        return key if isinstance(key, str) else ""

    @staticmethod
    def _reconciliation_key(document: Mapping[str, object]) -> str:
        key = document.get("runtime_key")
        return key if isinstance(key, str) else ""

    @staticmethod
    def _persisted_handle(
        task: Task, document: Mapping[str, object]
    ) -> str | None:
        """Return the opaque handle this task still owns, if any.

        A bound handle lives on the task; a run that was accepted but never
        bound (post-accept binding failure) is only recorded in the
        reconciliation document.  Both are real runtime runs, so both are
        cancel targets; neither is ever resubmitted.
        """

        for candidate in (
            task.runtime_task_id,
            document.get("runtime_task_id"),
        ):
            if _valid_runtime_task_id(candidate):
                return candidate
        return None

    def _record_lost(
        self,
        task: Task,
        lease: TaskLease,
        *,
        runtime_task_id: str | None,
        reason: str,
        now: datetime,
    ) -> DispatcherResult:
        """Enter ``unknown`` when the run's execution evidence is unusable.

        ``record_attempt_result(lost)`` revokes the Grant in the same
        transaction; no result/evidence is synthesized and the task is never
        replayed.
        """

        task_service.record_attempt_result(
            self._session,
            task_id=task.task_id,
            actor=self._service_principal,
            outcome="lost",
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            error_code=reason,
            now=now,
        )
        self._release(task, lease, now=now)
        return DispatcherResult(
            outcome="unknown",
            task_id=task.task_id,
            runtime_task_id=runtime_task_id,
            detail=reason,
        )

    def _fetch_result(
        self, task: Task, runtime_task_id: str
    ) -> RuntimeResult | None:
        """Read the runtime result, or report that it cannot be read.

        An adapter exception is not a result: the caller fails closed instead of
        rolling back a bind for a run the runtime already accepted.
        """

        try:
            return self._adapter.get_result(
                runtime_task_id, task_id=task.task_id
            )
        except Exception:  # noqa: BLE001 - cannot prove the result
            return None

    def _verifiable_result(
        self, task: Task, runtime_task_id: str
    ) -> RuntimeResult | None:
        """Return a result the core verifier can accept, or ``None``.

        A cancel response may be ``already_terminal`` because the runtime had
        already reached a terminal state, not necessarily because a valid
        artifact exists.  Only a host-compatible ``result_*`` plus at least one
        ``evidence:*`` reference justifies the completed/verifying path; the
        caller otherwise applies the cancel/failure policy unchanged.
        """

        result = self._fetch_result(task, runtime_task_id)
        if result is None:
            return None
        try:
            verification.validate_result_refs(
                result.result_ref,
                tuple(result.evidence_refs or ()),
                require_result=True,
            )
        except TaskError:
            return None
        return result

    def _cancel_after_accept(
        self,
        runtime_task_id: str,
        idempotency_key: str,
        task: Task,
        *,
        reason: str = RUNTIME_BIND_FAILED,
    ) -> dict[str, object]:
        """Ask the runtime to stop a run this boundary cannot keep driving.

        Used for the post-accept bind failure, the waiting-input escalation and
        a core ``cancel_requested`` task.  Cancellation is best effort by
        contract: a ``requested`` result is not a stop, and a failed call must
        not hide the unknown outcome.  The returned fact is folded into the
        persisted reconciliation record so a later reconciler knows whether a
        stop is still owed.
        """

        try:
            result = self._adapter.cancel_task(
                runtime_task_id,
                reason=reason,
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
        stop_evidence_refs: Sequence[str] = (),
        runtime_outcome: str | None = None,
    ) -> None:
        """Persist the accepted-but-unbound handle before entering unknown.

        ``record_dispatch_failure`` merges the existing result document, so
        these keys survive the transition.  Only the opaque handle, the stable
        key, cancellation/stop facts, the lookup answer and a reason are stored
        -- never a prompt, token or credential.
        """

        document: dict[str, object] = {
            "runtime_task_id": runtime_task_id,
            "cancel_requested_at": (
                _rfc3339(now)
                if cancel is not None and cancel.get("attempted")
                else None
            ),
            "cancel_outcome": (cancel or {}).get("outcome"),
            "stop_confirmed": bool((cancel or {}).get("stop_confirmed", False)),
            "reason": reason,
        }
        if idempotency_key:
            document["runtime_key"] = idempotency_key
        if runtime_outcome is not None:
            # The runtime's own terminal label (``stopped``/``failed``/
            # ``timed_out``).  It is kept as evidence for the human who
            # reconciles the task; it never becomes a core state by itself.
            document["runtime_outcome"] = runtime_outcome
        if stop_evidence_refs:
            # An observation-hash reference for a runtime-confirmed stop.  It is
            # kept here (not written as a core stop fact) so a confirmed stop is
            # never silently discarded by the fail-closed path.
            document["stop_evidence_refs"] = list(stop_evidence_refs)
        if lookup is not None:
            document["lookup_outcome"] = lookup.outcome.value
        if submit_outcome is not None:
            document["submit_outcome"] = submit_outcome
        previous = (task.result or {}).get("runtime_reconciliation")
        # Only facts from the *same* runtime submission may be inherited.  The
        # stable key is derived from task/grant/attempt, so a different key
        # means a new attempt: the previous run's stop/lookup facts must not
        # leak into it (a fresh run must never look already stopped, and a
        # stale lookup answer must not decide the new submission).
        if (
            isinstance(previous, Mapping)
            and previous.get("runtime_key") == idempotency_key
        ):
            # A follow-up fact in the same pass (e.g. a found handle that could
            # not be bound, or a cancel attempt on an already-unknown task)
            # keeps the lookup/adoption/key evidence already gathered.
            for key in (
                "lookup_outcome",
                "submit_outcome",
                "runtime_key",
                "runtime_outcome",
            ):
                if key in previous and key not in document:
                    document[key] = previous[key]
            if (
                "stop_evidence_refs" not in document
                and isinstance(
                    previous.get("stop_evidence_refs"), (list, tuple)
                )
            ):
                document["stop_evidence_refs"] = list(
                    previous["stop_evidence_refs"]
                )
            if previous.get("stop_confirmed") is True:
                # A confirmed stop never becomes unconfirmed; keep the fact
                # when a later write does not restate it.
                document["stop_confirmed"] = True
        if document.get("stop_evidence_refs"):
            # Stop evidence and the flag travel together: a recorded stop is a
            # confirmed stop (from a cancel response or a status observation).
            document["stop_confirmed"] = True
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
            return self._bind_and_drive(
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
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            reason=reason,
            now=now,
        )
        self._release(task, lease, now=now)

    def _require_current_lease(
        self, lease: TaskLease, *, now: datetime
    ) -> None:
        """Fail closed unless this pass still holds the current lease/fencing.

        ``escalate_task`` and the reconciliation document write have no
        worker/fencing parameter of their own, so they would otherwise be able
        to write a managed fact from a pass whose lease was released or staled
        by another writer.  The lease row is re-read under ``FOR UPDATE`` with
        ``populate_existing`` (a plain reselect does not refresh a loaded ORM
        object) and compared on id/worker/token/expiry.  A mismatch raises
        ``task_fencing_token_stale`` before any fact is written; the caller's
        transaction then rolls back and the current owner keeps the row.

        Works for both a claim lease and a recovery lease: both are ordinary
        :class:`~app.tasks.models.TaskLease` rows with their own worker id and
        fencing token.
        """

        current = self._session.scalar(
            select(TaskLease)
            .where(TaskLease.id == lease.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            current is None
            or current.released_at is not None
            or current.worker_id != lease.worker_id
            or current.fencing_token != lease.fencing_token
            or _as_utc(current.lease_expires_at) <= now
        ):
            raise TaskError(
                "task_fencing_token_stale",
                "the current lease/fencing token is not held by this pass",
                context={"task_id": lease.task_id},
            )

    def _release(self, task: Task, lease: TaskLease, *, now: datetime) -> None:
        """Release the lease this pass actually holds.

        The lease owner is read from the lease row, not the config: the
        recovery/cancel passes hold a *recovery* lease whose ``worker_id``
        differs from the dispatcher's claim id.
        """

        worker.release_lease(
            self._session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            now=now,
        )

    def _delay_for(self, result: DispatcherResult) -> float:
        if result.outcome == "adapter_unavailable":
            return self._config.health_backoff_seconds
        return self._config.poll_interval

    def _error_delay(self, consecutive_errors: int) -> float:
        """Return the bounded, exponentially growing retry delay.

        ``consecutive_errors`` counts failing passes in a row (>= 1).  The
        first delay is ``error_backoff_seconds`` and doubles per failure up to
        ``max_error_backoff_seconds``; a non-positive base (tests) stays ``0``
        so a test never sleeps.
        """

        base = max(0.0, float(self._config.error_backoff_seconds))
        cap = max(base, float(self._config.max_error_backoff_seconds))
        exponent = max(0, int(consecutive_errors) - 1)
        # Cap the exponent before ``2 ** exponent`` so a long outage cannot
        # build a huge integer; the result is capped anyway.
        return min(cap, base * (2 ** min(exponent, 16)))


# -- command line ------------------------------------------------------------

#: A trusted launcher's adapter registry: an operator-supplied ``--adapter``
#: *name* maps to a fully configured dispatcher, or to ``None`` when the name is
#: not registered.  Deployment code builds this mapping; the CLI never imports a
#: module named by an argument, never reads an adapter from task/client input
#: and never falls back to the Fake adapter.
DispatcherFactory = Callable[[str], "RuntimeDispatcher | None"]


def _build_parser() -> argparse.ArgumentParser:
    """Return the fixed CLI parser (flags are names only, never payloads)."""

    parser = argparse.ArgumentParser(
        prog="python -m app.tasks.execution",
        description=(
            "Managed Execution dispatcher.  There is no default production "
            "adapter: #97 supplies the trusted adapter registry and session "
            "configuration.  The Fake adapter is test-only."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once",
        action="store_true",
        help="run one claim/submit/drive pass and exit",
    )
    mode.add_argument(
        "--loop",
        action="store_true",
        help="poll until SIGTERM/SIGINT (one task at a time)",
    )
    parser.add_argument(
        "--adapter",
        required=True,
        metavar="NAME",
        help="name from the trusted adapter registry",
    )
    return parser


def _install_stop_handlers(
    stop_event: threading.Event,
) -> Callable[[], None]:
    """Translate SIGTERM/SIGINT into ``stop_event`` and return a restorer.

    The dispatcher honours the event between observations, so a signal never
    interrupts a fact write.  Handler installation is best effort: off the main
    thread (tests) or on an unsupported platform the event can still be set by
    the caller.  The previous handlers are restored on exit.
    """

    previous: dict[int, Any] = {}

    def _stop(signum: int, frame: object) -> None:
        stop_event.set()

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            previous.pop(signum, None)

    def _restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - thread/OS guard
                pass

    return _restore


def _safe_exception_code(exc: BaseException) -> str:
    """Return a stable, non-sensitive error code for stderr/logging.

    Only a domain reason code (the closed ``TaskError``/``GovernanceError``
    vocabulary) or the exception type is rendered: never the message, context,
    task payload, path or an arbitrary vendor attribute that happens to be
    called ``reason_code``.
    """

    if isinstance(exc, (TaskError, GovernanceError)):
        return exc.reason_code
    return type(exc).__name__


def main(
    argv: Sequence[str] | None = None,
    *,
    dispatcher_factory: DispatcherFactory | None = None,
) -> int:
    """Run the Managed Execution dispatcher CLI and return an exit code.

    ``dispatcher_factory`` is a **trusted** launcher/test injection.  It is the
    only way to obtain an adapter: without it the CLI fails closed and prints
    :data:`NO_DISPATCH_CONFIGURATION`, because #96 ships no production adapter
    (the real Hermes managed runtime belongs to #97).  A name the factory does
    not know is rejected with :data:`UNKNOWN_ADAPTER`; nothing is imported from
    the name and nothing is submitted.

    ``--once`` runs exactly one pass and prints its stable
    :class:`DispatcherResult` outcome (for example ``idle``/``verifying``/
    ``unknown``/``needs_human``) to stdout.  ``--loop`` runs
    :meth:`RuntimeDispatcher.run_forever` until SIGTERM/SIGINT; the loop needs
    the dispatcher to have been built with a ``session_factory``.  Both paths
    print only a stable outcome or exception type/reason code on failure --
    never a credential, task payload, host path or runtime URL.
    """

    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # ``--help`` exits 0; invalid flags exit 2.  argparse has already
        # printed usage, so ``main`` stays an int-returning entry point.
        code = exc.code
        return code if isinstance(code, int) else 2

    if dispatcher_factory is None:
        print(NO_DISPATCH_CONFIGURATION, file=sys.stderr)
        return 2

    try:
        dispatcher = dispatcher_factory(args.adapter)
    except Exception as exc:  # noqa: BLE001 - trusted launcher failure
        print(
            f"dispatch launcher failed: {_safe_exception_code(exc)}",
            file=sys.stderr,
        )
        return 2

    if dispatcher is None:
        print(f"{UNKNOWN_ADAPTER}: {args.adapter}", file=sys.stderr)
        return 2

    if args.once:
        try:
            result = dispatcher.run_once()
        except Exception as exc:  # noqa: BLE001 - report a safe code only
            print(
                f"dispatch pass failed: {_safe_exception_code(exc)}",
                file=sys.stderr,
            )
            return 1
        # The outcome is the fixed ``DispatcherResult`` vocabulary
        # (idle/verifying/unknown/needs_human/...).  It is safe to print and
        # lets an operator tell a no-op pass from a fail-closed one; the task
        # detail (reason code, ids) stays in core state/audit.
        print(result.outcome)
        return 0

    stop_event = threading.Event()
    restore_handlers = _install_stop_handlers(stop_event)
    try:
        dispatcher.run_forever(stop_event)
    except Exception as exc:  # noqa: BLE001 - report a safe code only
        print(
            f"dispatch loop failed: {_safe_exception_code(exc)}",
            file=sys.stderr,
        )
        return 1
    finally:
        restore_handlers()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
