"""Task-domain ORM models (Issue #79).

Conventions follow the rest of the repository:

* ``BIGINT`` identity primary keys and explicit ``CHECK``/``UNIQUE``/FK names
  through :mod:`app.db.base`'s naming convention;
* enums are ``TEXT`` + ``CHECK`` (never a PostgreSQL ``ENUM`` type), so the
  vocabulary can evolve with a migration;
* structured documents use ``JSONB``;
* every business timestamp is ``timestamptz``, supplied by the service layer
  (so the caller owns the transaction clock) with ``now()`` only as a default.

What is deliberately **not** modelled here
------------------------------------------
There is no second organisation, approval or outbox model.  ``tasks`` links to
the existing ``employees``, ``workflow_instances`` (task approvals),
``governance_decisions`` and ``execution_grants`` rows.  Confirmations are task
owned: a confirmation authorises **one** task payload hash and is consumed by
one call.

Invariants enforced at the database layer
-----------------------------------------
* a public ``task_id`` is unique and matches ``task_*``;
* the submission idempotency key is unique **per requester** (contract §9.1:
  the key is scoped by verified principal + operation);
* ``(task_id, state_revision)`` is unique in ``task_events`` -- a stale
  revision can never append a second event;
* ``task_events`` is append-only through a ``BEFORE UPDATE OR DELETE`` trigger
  (mirroring ``audit_events``); ``TRUNCATE`` still works for test resets;
* one active lease per task (partial unique index) and one lease row per
  ``(task_id, fencing_token)``;
* terminal states and ``unknown`` cannot keep an ``active`` grant, and active
  states cannot have an unresolved or inactive accountable owner;
* ``attempt`` only increments through dispatching, so ``running`` with
  ``attempt = 0`` is impossible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Import the referenced model modules so this module is self-sufficient when
# imported alone (see ``app/tests/test_model_imports.py``): tasks reference
# employees, workflow instances, governance decisions and execution grants.
import app.db.models.organization  # noqa: F401
import app.governance.models  # noqa: F401
import app.workflow.models  # noqa: F401
from app.db.base import Base
from app.db.models.organization import TimestampMixin

__all__ = [
    "TASK_STATES",
    "TASK_TERMINAL_STATES",
    "Task",
    "TaskAttempt",
    "TaskConfirmation",
    "TaskDependency",
    "TaskEvent",
    "TaskLease",
]

#: The frozen 18-state vocabulary (``task-envelope.status.state``).
TASK_STATES: tuple[str, ...] = (
    "draft",
    "evaluated",
    "awaiting_clarification",
    "awaiting_confirmation",
    "awaiting_approval",
    "queued",
    "dispatched",
    "running",
    "verifying",
    "succeeded",
    "denied",
    "cancel_requested",
    "cancelled",
    "failed",
    "timed_out",
    "needs_human",
    "partial_success",
    "unknown",
)

#: Task terminal states: no outgoing edge at all (``failed`` is only an
#: *attempt* terminal and may still leave through T65/T70).
TASK_TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "denied", "cancelled", "timed_out", "partial_success"}
)

#: States whose record must be complete and whose owner must be active (S-18).
TASK_ACTIVE_STATES: frozenset[str] = frozenset(
    {
        "queued",
        "dispatched",
        "running",
        "verifying",
        "awaiting_confirmation",
        "awaiting_approval",
    }
)

#: Historical records that may keep a disabled owner with ``owner_escalation``.
TASK_HISTORY_STATES: frozenset[str] = frozenset(
    {"succeeded", "partial_success", "denied", "cancelled", "timed_out", "failed"}
)

_EVENT_TYPES: tuple[str, ...] = (
    "task.created",
    "task.evaluated",
    "task.queued",
    "task.cancel_requested",
    "task.cancelled",
    "task.deadline_exceeded",
    "task.retry_authorized",
    "task.retry_exhausted",
    "governance.denied",
    "governance.clarification_required",
    "governance.confirmation_required",
    "governance.approval_required",
    "clarification.provided",
    "confirmation.recorded",
    "confirmation.rejected",
    "approval.recorded",
    "approval.rejected",
    "worker.leased",
    "worker.lease_expired",
    "dispatch.accepted",
    "dispatch.rejected",
    "dispatch.unsupported",
    "dispatch.unavailable",
    "dispatch.timeout",
    "dispatch.unknown",
    "dispatch.deadline_exceeded",
    "execution.started",
    "execution.completed",
    "execution.failed",
    "execution.timed_out",
    "execution.lost",
    "execution.stopped",
    "verification.passed",
    "verification.partial",
    "verification.failed",
    "verification.inconclusive",
    "verification.evidence_unavailable",
    "reconcile.running",
    "reconcile.completed",
    "reconcile.failed",
    "reconcile.not_found",
    "reconcile.stopped",
    "reconcile.timed_out",
    "reconcile.unresolved",
    "human.escalation_required",
    "human.takeover.running",
    "human.result.submitted",
    "human.redispatch",
    "human.authorization_revoked",
    "human.stop_confirmed",
    "human.declared_failed",
)

_TASK_ID_PATTERN = r"^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_EVENT_ID_PATTERN = r"^evt_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CONFIRMATION_ID_PATTERN = r"^conf_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CORRELATION_ID_PATTERN = r"^corr_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_IDEMPOTENCY_KEY_PATTERN = r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
_EXECUTOR_PATTERN = r"^(prin|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_REQUESTER_PATTERN = r"^(prin|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_OWNER_PATTERN = r"^(prin|role)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_APPROVER_PATTERN = r"^(prin|role)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"

_STATE_SQL = ", ".join(f"'{state}'" for state in TASK_STATES)
_EVENT_SQL = ", ".join(f"'{event}'" for event in _EVENT_TYPES)

_ACTIVE_STATE_SQL = ", ".join(
    f"'{state}'" for state in sorted(TASK_ACTIVE_STATES)
)
_HISTORY_STATE_SQL = ", ".join(
    f"'{state}'" for state in sorted(TASK_HISTORY_STATES)
)


class Task(TimestampMixin, Base):
    """A durable task: identity, responsibility, authority link and lifecycle.

    ``state_revision`` is the optimistic-lock and event-ordering token: every
    state write increments it and appends exactly one ``task_events`` row with
    the same revision.  ``attempt`` is the number of execution attempts that
    have been *opened* (``dispatch.accepted``); it is not a count of retries.
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    goal_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_task_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)

    # -- responsibility (kept separate on purpose) -----------------------
    requester_principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    requester_employee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=True,
    )
    owner_principal_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_employee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=True,
    )
    owner_account_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    owner_escalation: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    assignee_principal_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    executor_principal_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    executor_principal_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'person'")
    )
    executor_employee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=True,
    )
    approver_principal_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    approver_employee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # -- goal / scheduling ----------------------------------------------
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    goal: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    priority: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'normal'")
    )
    priority_requested: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'normal'")
    )
    priority_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority_authorized_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sla: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # -- execution intent and authority ---------------------------------
    execution_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'undetermined'")
    )
    resource_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    requested_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    policy_set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("governance_decisions.decision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    decision_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_purpose: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'initial_execution'")
    )
    grant_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("execution_grants.grant_id", ondelete="RESTRICT"),
        nullable=True,
    )
    grant_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Execution handle bound by the boundary: ``rtask_*`` (Managed) or
    #: ``exec_*`` (Advisory/Governed).  At least one is required while
    #: dispatched/running (F21).
    runtime_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Whether the action may be retried without duplicating side effects.
    #: Unsafe actions escalate to a human instead of being replayed (F... §13).
    retry_safe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    # -- lifecycle -------------------------------------------------------
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'")
    )
    state_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    retry_budget_remaining: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("2")
    )
    stop_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    needs_human_origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    record_completeness: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pre_execution_incomplete'")
    )
    incomplete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- execution payload and gate evidence -----------------------------
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    payload_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmation_ref: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    approval_ref: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    #: Existing workflow instance used for approval (no second approval model).
    workflow_instance_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("workflow_instances.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- timestamps ------------------------------------------------------
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "requester_principal_id",
            "idempotency_key",
            name="uq_tasks_requester_idempotency_key",
        ),
        CheckConstraint(
            f"task_id ~ '{_TASK_ID_PATTERN}'", name="task_id_format"
        ),
        CheckConstraint(
            f"idempotency_key ~ '{_IDEMPOTENCY_KEY_PATTERN}'",
            name="idempotency_key_format",
        ),
        CheckConstraint(
            f"request_hash ~ '{_SHA256_PATTERN}'", name="request_hash_format"
        ),
        CheckConstraint(
            "goal_hash IS NULL OR "
            f"goal_hash ~ '{_SHA256_PATTERN}'",
            name="goal_hash_format",
        ),
        CheckConstraint(
            f"correlation_id ~ '{_CORRELATION_ID_PATTERN}'",
            name="correlation_id_format",
        ),
        CheckConstraint(
            f"requester_principal_id ~ '{_REQUESTER_PATTERN}'",
            name="requester_principal_format",
        ),
        CheckConstraint(
            f"owner_principal_id IS NULL OR "
            f"owner_principal_id ~ '{_OWNER_PATTERN}'",
            name="owner_principal_format",
        ),
        CheckConstraint(
            f"assignee_principal_id IS NULL OR "
            f"assignee_principal_id ~ '{_EXECUTOR_PATTERN}'",
            name="assignee_principal_format",
        ),
        CheckConstraint(
            f"executor_principal_id IS NULL OR "
            f"executor_principal_id ~ '{_EXECUTOR_PATTERN}'",
            name="executor_principal_format",
        ),
        CheckConstraint(
            f"approver_principal_id IS NULL OR "
            f"approver_principal_id ~ '{_APPROVER_PATTERN}'",
            name="approver_principal_format",
        ),
        CheckConstraint(
            "owner_account_status IN "
            "('active', 'disabled', 'unbound', 'unknown')",
            name="owner_account_status_allowed",
        ),
        CheckConstraint(
            f"state IN ({_STATE_SQL})", name="state_allowed"
        ),
        CheckConstraint(
            f"grant_status IS NULL OR "
            "grant_status IN ('active', 'consumed', 'revoked', 'expired')",
            name="grant_status_allowed",
        ),
        CheckConstraint(
            "execution_mode IN ('advisory', 'governed_resource_access', "
            "'managed_execution', 'undetermined')",
            name="execution_mode_allowed",
        ),
        CheckConstraint(
            "executor_principal_type IN ('person', 'agent_instance', 'service')",
            name="executor_principal_type_allowed",
        ),
        CheckConstraint(
            "runtime_task_id IS NULL OR "
            "runtime_task_id ~ '^rtask_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="runtime_task_id_format",
        ),
        CheckConstraint(
            "execution_ref IS NULL OR "
            "execution_ref ~ '^exec_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="execution_ref_format",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="priority_allowed",
        ),
        CheckConstraint(
            "priority_requested IN ('low', 'normal', 'high', 'urgent')",
            name="priority_requested_allowed",
        ),
        CheckConstraint(
            "record_completeness IN ('complete', 'pre_execution_incomplete')",
            name="record_completeness_allowed",
        ),
        CheckConstraint(
            "decision_purpose IN ('initial_execution', 'retry')",
            name="decision_purpose_allowed",
        ),
        CheckConstraint(
            "state_revision >= 0", name="state_revision_non_negative"
        ),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 100",
            name="max_attempts_bounds",
        ),
        CheckConstraint(
            "retry_budget_remaining >= 0 AND retry_budget_remaining <= 100",
            name="retry_budget_bounds",
        ),
        CheckConstraint(
            "attempt <= max_attempts", name="attempt_within_max_attempts"
        ),
        # F27: a running fact requires at least one opened attempt.
        CheckConstraint(
            "state NOT IN ('dispatched', 'running', 'verifying', "
            "'succeeded', 'partial_success') OR attempt >= 1",
            name="attempt_required_after_dispatch",
        ),
        # S-18: active states need a resolved, active owner and no escalation.
        CheckConstraint(
            f"(state NOT IN ({_ACTIVE_STATE_SQL})) OR "
            "(owner_principal_id IS NOT NULL AND owner_account_status = "
            "'active' AND owner_escalation IS NULL)",
            name="active_state_owner_resolved",
        ),
        # S-18: historical rows with a non-active owner carry escalation data.
        CheckConstraint(
            f"(state NOT IN ({_HISTORY_STATE_SQL})) OR "
            "owner_principal_id IS NULL OR owner_account_status = 'active' OR "
            "owner_escalation IS NOT NULL",
            name="history_owner_escalation_present",
        ),
        # F35: an incomplete pre-execution record cannot hold a grant.
        CheckConstraint(
            "record_completeness = 'complete' OR "
            "(grant_id IS NULL AND attempt = 0)",
            name="incomplete_record_has_no_grant",
        ),
        # S-10 / F34: terminal and unknown tasks cannot keep an active grant.
        CheckConstraint(
            "(state NOT IN ('succeeded', 'partial_success', 'failed', "
            "'cancelled', 'timed_out', 'denied', 'unknown')) OR "
            "grant_id IS NULL OR grant_status IN ('revoked', 'consumed')",
            name="terminal_state_grant_invalidated",
        ),
        CheckConstraint(
            "state <> 'unknown' OR grant_id IS NULL OR grant_status = 'revoked'",
            name="unknown_state_grant_revoked",
        ),
        # F30: no unconfirmed stop as a terminal fact.
        CheckConstraint(
            "state NOT IN ('cancelled', 'timed_out') OR stop_confirmed",
            name="terminal_stop_confirmed",
        ),
        # S-20: a denied attempt that had started must have stop evidence.
        CheckConstraint(
            "state <> 'denied' OR attempt = 0 OR stop_confirmed",
            name="denied_after_attempt_stop_confirmed",
        ),
        # T61/T65: a re-queued task that already had an attempt must prove stop.
        CheckConstraint(
            "state <> 'queued' OR attempt = 0 OR stop_confirmed",
            name="requeue_after_attempt_stop_confirmed",
        ),
        CheckConstraint(
            "payload_hash IS NULL OR "
            f"payload_hash ~ '{_SHA256_PATTERN}'",
            name="payload_hash_format",
        ),
        CheckConstraint(
            "policy_content_hash IS NULL OR "
            f"policy_content_hash ~ '{_SHA256_PATTERN}'",
            name="policy_content_hash_format",
        ),
        Index("ix_tasks_state_priority_due", "state", "priority", "due_at"),
        Index("ix_tasks_requester_principal_id", "requester_principal_id"),
        Index("ix_tasks_owner_principal_id", "owner_principal_id"),
        Index("ix_tasks_assignee_principal_id", "assignee_principal_id"),
        Index("ix_tasks_executor_principal_id", "executor_principal_id"),
        Index("ix_tasks_decision_id", "decision_id"),
        Index("ix_tasks_grant_id", "grant_id"),
        Index("ix_tasks_goal_hash", "goal_hash"),
        Index("ix_tasks_created_at", "created_at"),
    )


class TaskEvent(Base):
    """Append-only task event; one row per state transition.

    ``(task_id, state_revision)`` is unique, so a writer holding a stale
    revision cannot append an event.  A database trigger rejects every UPDATE
    and DELETE at row level; history is never rewritten.
    """

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    actor_principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id", "sequence", name="uq_task_events_task_sequence"
        ),
        CheckConstraint(
            f"event_id ~ '{_EVENT_ID_PATTERN}'", name="event_id_format"
        ),
        CheckConstraint(
            f"event_type IN ({_EVENT_SQL})", name="event_type_allowed"
        ),
        CheckConstraint(
            "from_state IS NULL OR "
            f"from_state IN ({_STATE_SQL})",
            name="from_state_allowed",
        ),
        CheckConstraint(f"to_state IN ({_STATE_SQL})", name="to_state_allowed"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        Index("ix_task_events_task_id_created_at", "task_id", "created_at"),
        Index("ix_task_events_event_type", "event_type"),
    )


class TaskAttempt(Base):
    """One opened execution attempt.

    The attempt opens at ``dispatch.accepted`` and only closes when the
    execution boundary supplies closing evidence (stopped/failed/timed out/
    not found) or verification fails.  ``unknown`` and ``needs_human`` do not
    close it: an attempt with ``ended_at IS NULL`` and no "never accepted"
    evidence is an *unreconciled active attempt*.
    """

    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'dispatched'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stop_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    runtime_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_task_attempts_task_attempt_number",
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(
            "state IN ('dispatched', 'running', 'verifying', 'succeeded', "
            "'failed', 'cancelled', 'timed_out', 'unknown', 'abandoned')",
            name="state_allowed",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ended_after_started",
        ),
        CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'cancelled', 'timed_out') "
            "OR ended_at IS NOT NULL",
            name="closed_state_has_end",
        ),
        CheckConstraint(
            "state NOT IN ('cancelled', 'timed_out') OR stop_confirmed",
            name="terminal_attempt_stop_confirmed",
        ),
        Index("ix_task_attempts_task_id_state", "task_id", "state"),
    )


class TaskDependency(Base):
    """A directed dependency between two tasks.

    ``type = 'blocks'`` means the target task cannot enter ``queued`` until the
    dependency has succeeded (or, for non-blocking relations, is only
    informational).  Self-dependencies and cycles are rejected by the service.
    """

    __tablename__ = "task_dependencies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    depends_on_task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'blocks'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "depends_on_task_id",
            "type",
            name="uq_task_dependencies_triplet",
        ),
        CheckConstraint("task_id <> depends_on_task_id", name="not_self"),
        CheckConstraint(
            "type IN ('blocks', 'relates_to', 'supersedes')",
            name="type_allowed",
        ),
        Index("ix_task_dependencies_depends_on", "depends_on_task_id"),
    )


class TaskLease(Base):
    """A worker lease with a monotonic fencing token.

    ``fencing_token`` is allocated per task; only the holder of the token in the
    **current active lease** may write execution states.  The partial unique
    index guarantees at most one active (unreleased) lease per task.
    """

    __tablename__ = "task_leases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id", "fencing_token", name="uq_task_leases_task_fencing_token"
        ),
        CheckConstraint("fencing_token >= 1", name="fencing_token_positive"),
        CheckConstraint(
            "lease_expires_at > acquired_at", name="expires_after_acquired"
        ),
        CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at",
            name="released_after_acquired",
        ),
        Index(
            "uq_task_leases_active_task",
            "task_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_task_leases_expires_at", "lease_expires_at"),
        Index("ix_task_leases_worker_id", "worker_id"),
    )


class TaskConfirmation(Base):
    """A single-use confirmation bound to one task execution payload.

    ``payload_hash`` is the RFC 8785 JCS hash of the execution payload.  A
    ``satisfied`` confirmation may be consumed exactly once; replaying it
    returns ``replay_detected``.  ``channel_binding`` preserves the channel
    semantics: cross-channel consumption is refused by the service.
    """

    __tablename__ = "task_confirmations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    confirmation_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    canonicalization: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'rfc8785_jcs'")
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    confirmed_by_principal_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    satisfied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    single_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    channel_binding: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"confirmation_id ~ '{_CONFIRMATION_ID_PATTERN}'",
            name="confirmation_id_format",
        ),
        CheckConstraint(
            f"payload_hash ~ '{_SHA256_PATTERN}'", name="payload_hash_format"
        ),
        CheckConstraint(
            "canonicalization = 'rfc8785_jcs'", name="canonicalization_allowed"
        ),
        CheckConstraint(
            "purpose IN ('high_impact_action', 'data_release', "
            "'irreversible_action', 'payload_change', 'manual_override')",
            name="purpose_allowed",
        ),
        CheckConstraint(
            "status IN ('pending', 'satisfied', 'consumed', 'rejected', "
            "'expired')",
            name="status_allowed",
        ),
        CheckConstraint("single_use", name="single_use_required"),
        CheckConstraint("expires_at > issued_at", name="expires_after_issued"),
        CheckConstraint(
            "confirmed_by_principal_id IS NULL OR "
            "confirmed_by_principal_id ~ "
            "'^(prin|rt|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="confirmed_by_format",
        ),
        # Lifecycle field coherence (contract §8.1).
        CheckConstraint(
            "status <> 'satisfied' OR "
            "(confirmed_by_principal_id IS NOT NULL AND satisfied_at IS NOT "
            "NULL AND consumed_at IS NULL AND consumed_by_call_id IS NULL)",
            name="satisfied_fields",
        ),
        CheckConstraint(
            "status <> 'consumed' OR "
            "(satisfied_at IS NOT NULL AND consumed_at IS NOT NULL AND "
            "consumed_by_call_id IS NOT NULL)",
            name="consumed_fields",
        ),
        CheckConstraint(
            "status NOT IN ('pending', 'rejected') OR "
            "(satisfied_at IS NULL AND consumed_at IS NULL AND "
            "consumed_by_call_id IS NULL)",
            name="unsatisfied_fields",
        ),
        CheckConstraint(
            "status <> 'expired' OR "
            "(consumed_at IS NULL AND consumed_by_call_id IS NULL)",
            name="expired_fields",
        ),
        Index("ix_task_confirmations_task_id_status", "task_id", "status"),
        Index("ix_task_confirmations_expires_at", "expires_at"),
    )
