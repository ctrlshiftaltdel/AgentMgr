"""task lifecycle, attempts, dependencies, leases and confirmations

Revision ID: 0015_task_lifecycle
Revises: 0014_gov_grant_constraints
Create Date: 2026-10-21

Implements the durable task aggregate of Issue #79:

* ``tasks`` -- identity, separate responsibility columns, scheduling fields,
  decision/grant bindings, lifecycle state and attempt accounting;
* ``task_events`` -- append-only transition log with a per-task unique
  ``state_revision`` and a database trigger rejecting every row UPDATE/DELETE;
* ``task_attempts`` -- opened execution attempts (closed only by evidence);
* ``task_dependencies`` -- blocking/relating edges with no self-reference;
* ``task_leases`` -- worker leases with monotonic fencing tokens and at most one
  active lease per task;
* ``task_confirmations`` -- payload-bound, single-use confirmations.

No approval or outbox table is created here: task approvals reuse
``workflow_instances`` / ``approval_steps`` and state notifications reuse
``outbox_events``.

Downgrade drops the trigger, function, tables and indexes in reverse order and
leaves the ``0014`` schema intact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0015_task_lifecycle"
down_revision = "0014_gov_grant_constraints"
branch_labels = None
depends_on = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())

_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION public.task_events_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'task_events is append-only (no UPDATE/DELETE)'
        USING ERRCODE = 'restrict_violation';
END;
$function$;
"""

_APPEND_ONLY_TRIGGER = """
CREATE TRIGGER trg_task_events_reject_mutation
BEFORE UPDATE OR DELETE ON public.task_events
FOR EACH ROW EXECUTE FUNCTION public.task_events_reject_mutation();
"""

_DROP_APPEND_ONLY_TRIGGER = (
    "DROP TRIGGER IF EXISTS trg_task_events_reject_mutation "
    "ON public.task_events;"
)
_DROP_APPEND_ONLY_FUNCTION = (
    "DROP FUNCTION IF EXISTS public.task_events_reject_mutation();"
)

_TASK_STATES = (
    "'draft', 'evaluated', 'awaiting_clarification', 'awaiting_confirmation', "
    "'awaiting_approval', 'queued', 'dispatched', 'running', 'verifying', "
    "'succeeded', 'denied', 'cancel_requested', 'cancelled', 'failed', "
    "'timed_out', 'needs_human', 'partial_success', 'unknown'"
)
_ACTIVE_STATES = (
    "'queued', 'dispatched', 'running', 'verifying', "
    "'awaiting_confirmation', 'awaiting_approval'"
)
_HISTORY_STATES = (
    "'succeeded', 'partial_success', 'denied', 'cancelled', 'timed_out', "
    "'failed'"
)
_TERMINAL_OR_UNKNOWN = (
    "'succeeded', 'partial_success', 'failed', 'cancelled', 'timed_out', "
    "'denied', 'unknown'"
)
#: ``classify_necessity`` defines a task as live unless it is terminal or
#: ``failed``; the partial unique index uses the same state set (C8).
_NON_LIVE_STATES = (
    "'succeeded', 'partial_success', 'denied', 'cancelled', 'timed_out', "
    "'failed'"
)
_EVENT_TYPES = (
    "'task.created', 'task.evaluated', 'task.queued', 'task.cancel_requested', "
    "'task.cancelled', 'task.deadline_exceeded', 'task.retry_authorized', "
    "'task.retry_exhausted', 'governance.denied', "
    "'governance.clarification_required', 'governance.confirmation_required', "
    "'governance.approval_required', 'clarification.provided', "
    "'confirmation.recorded', 'confirmation.rejected', 'approval.recorded', "
    "'approval.rejected', 'worker.leased', 'worker.lease_expired', "
    "'dispatch.accepted', 'dispatch.rejected', 'dispatch.unsupported', "
    "'dispatch.unavailable', 'dispatch.timeout', 'dispatch.unknown', "
    "'dispatch.deadline_exceeded', 'execution.started', "
    "'execution.completed', 'execution.failed', 'execution.timed_out', "
    "'execution.lost', 'execution.stopped', 'verification.passed', "
    "'verification.partial', 'verification.failed', "
    "'verification.inconclusive', 'verification.evidence_unavailable', "
    "'reconcile.running', 'reconcile.completed', 'reconcile.failed', "
    "'reconcile.not_found', 'reconcile.stopped', 'reconcile.timed_out', "
    "'reconcile.unresolved', 'human.escalation_required', "
    "'human.takeover.running', 'human.result.submitted', 'human.redispatch', "
    "'human.authorization_revoked', 'human.stop_confirmed', "
    "'human.declared_failed'"
)
_TASK_ID = r"^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_EVENT_ID = r"^evt_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CONFIRMATION_ID = r"^conf_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CORRELATION_ID = r"^corr_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_IDEM = r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_SHA256 = r"^sha256:[a-f0-9]{64}$"
_REQUESTER = r"^(prin|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_EXECUTOR = r"^(prin|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_OWNER = r"^(prin|role)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_APPROVER = r"^(prin|role)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("goal_hash", sa.Text(), nullable=True),
        sa.Column("client_task_ref", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("requester_principal_id", sa.Text(), nullable=False),
        sa.Column("requester_employee_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_principal_id", sa.Text(), nullable=True),
        sa.Column("owner_employee_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "owner_account_status", sa.Text(), server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("owner_escalation", _JSONB, nullable=True),
        sa.Column("assignee_principal_id", sa.Text(), nullable=True),
        sa.Column("executor_principal_id", sa.Text(), nullable=True),
        sa.Column(
            "executor_principal_type", sa.Text(),
            server_default=sa.text("'person'"), nullable=False,
        ),
        sa.Column("executor_employee_id", sa.BigInteger(), nullable=True),
        sa.Column("approver_principal_id", sa.Text(), nullable=True),
        sa.Column("approver_employee_id", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "goal", _JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "priority", sa.Text(), server_default=sa.text("'normal'"), nullable=False
        ),
        sa.Column(
            "priority_requested", sa.Text(), server_default=sa.text("'normal'"),
            nullable=False,
        ),
        sa.Column("priority_rationale", sa.Text(), nullable=True),
        sa.Column("priority_authorized_by", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sla", _JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "execution_mode", sa.Text(),
            server_default=sa.text("'undetermined'"), nullable=False,
        ),
        sa.Column(
            "resource_scope", _JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "requested_capabilities", _JSONB,
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column("policy_set_id", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=True),
        sa.Column("policy_content_hash", sa.Text(), nullable=True),
        sa.Column("decision_id", sa.Text(), nullable=True),
        sa.Column("decision_revision", sa.Integer(), nullable=True),
        sa.Column("decision_type", sa.Text(), nullable=True),
        sa.Column(
            "decision_purpose", sa.Text(),
            server_default=sa.text("'initial_execution'"), nullable=False,
        ),
        sa.Column("grant_id", sa.Text(), nullable=True),
        sa.Column("grant_status", sa.Text(), nullable=True),
        sa.Column("runtime_task_id", sa.Text(), nullable=True),
        sa.Column("execution_ref", sa.Text(), nullable=True),
        sa.Column(
            "retry_safe", sa.Boolean(), server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "max_calls", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "max_runtime_seconds", sa.Integer(), server_default=sa.text("300"),
            nullable=False,
        ),
        sa.Column("max_cost_units", sa.Float(), nullable=True),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column(
            "state_revision", sa.Integer(), server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_event_id", sa.Text(), nullable=True),
        sa.Column(
            "attempt", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False
        ),
        sa.Column(
            "retry_budget_remaining", sa.Integer(), server_default=sa.text("2"),
            nullable=False,
        ),
        sa.Column(
            "stop_confirmed", sa.Boolean(), server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "result", _JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("needs_human_origin", sa.Text(), nullable=True),
        sa.Column(
            "record_completeness", sa.Text(),
            server_default=sa.text("'pre_execution_incomplete'"), nullable=False,
        ),
        sa.Column("incomplete_reason", sa.Text(), nullable=True),
        sa.Column(
            "payload", _JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("confirmation_ref", _JSONB, nullable=True),
        sa.Column("approval_ref", _JSONB, nullable=True),
        sa.Column("workflow_instance_id", sa.BigInteger(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["requester_employee_id"], ["employees.id"], ondelete="RESTRICT",
            name="fk_tasks_requester_employee_id_employees",
        ),
        sa.ForeignKeyConstraint(
            ["owner_employee_id"], ["employees.id"], ondelete="RESTRICT",
            name="fk_tasks_owner_employee_id_employees",
        ),
        sa.ForeignKeyConstraint(
            ["executor_employee_id"], ["employees.id"], ondelete="RESTRICT",
            name="fk_tasks_executor_employee_id_employees",
        ),
        sa.ForeignKeyConstraint(
            ["approver_employee_id"], ["employees.id"], ondelete="RESTRICT",
            name="fk_tasks_approver_employee_id_employees",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["governance_decisions.decision_id"],
            ondelete="RESTRICT",
            name="fk_tasks_decision_id_governance_decisions",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["execution_grants.grant_id"], ondelete="RESTRICT",
            name="fk_tasks_grant_id_execution_grants",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_instance_id"], ["workflow_instances.id"],
            ondelete="SET NULL",
            name="fk_tasks_workflow_instance_id_workflow_instances",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("task_id", name="uq_tasks_task_id"),
        sa.UniqueConstraint(
            "requester_principal_id", "idempotency_key",
            name="uq_tasks_requester_idempotency_key",
        ),
        sa.CheckConstraint(f"task_id ~ '{_TASK_ID}'", name="task_id_format"),
        sa.CheckConstraint(
            f"idempotency_key ~ '{_IDEM}'", name="idempotency_key_format"
        ),
        sa.CheckConstraint(
            f"request_hash ~ '{_SHA256}'", name="request_hash_format"
        ),
        sa.CheckConstraint(
            f"goal_hash IS NULL OR goal_hash ~ '{_SHA256}'", name="goal_hash_format"
        ),
        sa.CheckConstraint(
            f"correlation_id ~ '{_CORRELATION_ID}'", name="correlation_id_format"
        ),
        sa.CheckConstraint(
            f"requester_principal_id ~ '{_REQUESTER}'",
            name="requester_principal_format",
        ),
        sa.CheckConstraint(
            f"owner_principal_id IS NULL OR owner_principal_id ~ '{_OWNER}'",
            name="owner_principal_format",
        ),
        sa.CheckConstraint(
            f"assignee_principal_id IS NULL OR assignee_principal_id ~ '{_EXECUTOR}'",
            name="assignee_principal_format",
        ),
        sa.CheckConstraint(
            f"executor_principal_id IS NULL OR executor_principal_id ~ '{_EXECUTOR}'",
            name="executor_principal_format",
        ),
        sa.CheckConstraint(
            f"approver_principal_id IS NULL OR approver_principal_id ~ '{_APPROVER}'",
            name="approver_principal_format",
        ),
        sa.CheckConstraint(
            "owner_account_status IN ('active', 'disabled', 'unbound', 'unknown')",
            name="owner_account_status_allowed",
        ),
        sa.CheckConstraint(f"state IN ({_TASK_STATES})", name="state_allowed"),
        sa.CheckConstraint(
            "grant_status IS NULL OR grant_status IN "
            "('active', 'consumed', 'revoked', 'expired')",
            name="grant_status_allowed",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('advisory', 'governed_resource_access', "
            "'managed_execution', 'undetermined')",
            name="execution_mode_allowed",
        ),
        sa.CheckConstraint(
            "executor_principal_type IN ('person', 'agent_instance', 'service')",
            name="executor_principal_type_allowed",
        ),
        sa.CheckConstraint(
            "runtime_task_id IS NULL OR "
            "runtime_task_id ~ '^rtask_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="runtime_task_id_format",
        ),
        sa.CheckConstraint(
            "execution_ref IS NULL OR "
            "execution_ref ~ '^exec_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="execution_ref_format",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="priority_allowed",
        ),
        sa.CheckConstraint(
            "priority_requested IN ('low', 'normal', 'high', 'urgent')",
            name="priority_requested_allowed",
        ),
        sa.CheckConstraint(
            "record_completeness IN ('complete', 'pre_execution_incomplete')",
            name="record_completeness_allowed",
        ),
        sa.CheckConstraint(
            "decision_purpose IN ('initial_execution', 'retry')",
            name="decision_purpose_allowed",
        ),
        sa.CheckConstraint(
            "state_revision >= 0", name="state_revision_non_negative"
        ),
        sa.CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 100", name="max_attempts_bounds"
        ),
        sa.CheckConstraint(
            "retry_budget_remaining >= 0 AND retry_budget_remaining <= 100",
            name="retry_budget_bounds",
        ),
        sa.CheckConstraint(
            "max_calls >= 1 AND max_calls <= 1000", name="max_calls_bounds"
        ),
        sa.CheckConstraint(
            "max_runtime_seconds >= 1 AND max_runtime_seconds <= 86400",
            name="max_runtime_seconds_bounds",
        ),
        sa.CheckConstraint(
            "max_cost_units IS NULL OR max_cost_units >= 0",
            name="max_cost_units_non_negative",
        ),
        sa.CheckConstraint(
            "attempt <= max_attempts", name="attempt_within_max_attempts"
        ),
        sa.CheckConstraint(
            "state NOT IN ('dispatched', 'running', 'verifying', 'succeeded', "
            "'partial_success') OR attempt >= 1",
            name="attempt_required_after_dispatch",
        ),
        sa.CheckConstraint(
            f"(state NOT IN ({_ACTIVE_STATES})) OR "
            "(owner_principal_id IS NOT NULL AND owner_account_status = 'active' "
            "AND owner_escalation IS NULL)",
            name="active_state_owner_resolved",
        ),
        sa.CheckConstraint(
            f"(state NOT IN ({_HISTORY_STATES})) OR owner_principal_id IS NULL "
            "OR owner_account_status = 'active' OR owner_escalation IS NOT NULL",
            name="history_owner_escalation_present",
        ),
        sa.CheckConstraint(
            "record_completeness = 'complete' OR "
            "(grant_id IS NULL AND attempt = 0)",
            name="incomplete_record_has_no_grant",
        ),
        sa.CheckConstraint(
            f"(state NOT IN ({_TERMINAL_OR_UNKNOWN})) OR grant_id IS NULL OR "
            "grant_status IN ('revoked', 'consumed')",
            name="terminal_state_grant_invalidated",
        ),
        sa.CheckConstraint(
            "state <> 'unknown' OR grant_id IS NULL OR "
            "grant_status IN ('revoked', 'consumed')",
            name="unknown_state_grant_revoked",
        ),
        sa.CheckConstraint(
            "state NOT IN ('cancelled', 'timed_out') OR stop_confirmed",
            name="terminal_stop_confirmed",
        ),
        sa.CheckConstraint(
            "state <> 'denied' OR attempt = 0 OR stop_confirmed",
            name="denied_after_attempt_stop_confirmed",
        ),
        sa.CheckConstraint(
            "state <> 'queued' OR attempt = 0 OR stop_confirmed",
            name="requeue_after_attempt_stop_confirmed",
        ),
        sa.CheckConstraint(
            f"payload_hash IS NULL OR payload_hash ~ '{_SHA256}'",
            name="payload_hash_format",
        ),
        sa.CheckConstraint(
            f"policy_content_hash IS NULL OR policy_content_hash ~ '{_SHA256}'",
            name="policy_content_hash_format",
        ),
    )
    op.create_index("ix_tasks_state_priority_due", "tasks", ["state", "priority", "due_at"])
    op.create_index("ix_tasks_requester_principal_id", "tasks", ["requester_principal_id"])
    op.create_index("ix_tasks_owner_principal_id", "tasks", ["owner_principal_id"])
    op.create_index("ix_tasks_assignee_principal_id", "tasks", ["assignee_principal_id"])
    op.create_index("ix_tasks_executor_principal_id", "tasks", ["executor_principal_id"])
    op.create_index("ix_tasks_decision_id", "tasks", ["decision_id"])
    op.create_index("ix_tasks_grant_id", "tasks", ["grant_id"])
    op.create_index("ix_tasks_goal_hash", "tasks", ["goal_hash"])
    op.create_index(
        "uq_tasks_live_requester_goal",
        "tasks",
        ["requester_principal_id", "goal_hash"],
        unique=True,
        postgresql_where=sa.text(
            f"goal_hash IS NOT NULL AND state NOT IN ({_NON_LIVE_STATES})"
        ),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("actor_principal_id", sa.Text(), nullable=False),
        sa.Column(
            "payload", _JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], ondelete="CASCADE",
            name="fk_task_events_task_id_tasks",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_events"),
        sa.UniqueConstraint("event_id", name="uq_task_events_event_id"),
        sa.UniqueConstraint(
            "task_id", "sequence", name="uq_task_events_task_sequence"
        ),
        sa.CheckConstraint(f"event_id ~ '{_EVENT_ID}'", name="event_id_format"),
        sa.CheckConstraint(
            f"event_type IN ({_EVENT_TYPES})", name="event_type_allowed"
        ),
        sa.CheckConstraint(
            f"from_state IS NULL OR from_state IN ({_TASK_STATES})",
            name="from_state_allowed",
        ),
        sa.CheckConstraint(f"to_state IN ({_TASK_STATES})", name="to_state_allowed"),
        sa.CheckConstraint("sequence >= 1", name="sequence_positive"),
    )
    op.create_index(
        "ix_task_events_task_id_created_at", "task_events", ["task_id", "created_at"]
    )
    op.create_index("ix_task_events_event_type", "task_events", ["event_type"])
    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(_APPEND_ONLY_TRIGGER)

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'dispatched'"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "stop_confirmed", sa.Boolean(), server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("result_ref", sa.Text(), nullable=True),
        sa.Column(
            "evidence_refs", _JSONB, server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("runtime_ref", sa.Text(), nullable=True),
        sa.Column("execution_ref", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], ondelete="CASCADE",
            name="fk_task_attempts_task_id_tasks",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_attempts"),
        sa.UniqueConstraint(
            "task_id", "attempt_number",
            name="uq_task_attempts_task_attempt_number",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        sa.CheckConstraint(
            "state IN ('dispatched', 'running', 'verifying', 'succeeded', "
            "'failed', 'cancelled', 'timed_out', 'unknown', 'abandoned')",
            name="state_allowed",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name="ended_after_started"
        ),
        sa.CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'cancelled', 'timed_out') "
            "OR ended_at IS NOT NULL",
            name="closed_state_has_end",
        ),
        sa.CheckConstraint(
            "state NOT IN ('cancelled', 'timed_out') OR stop_confirmed",
            name="terminal_attempt_stop_confirmed",
        ),
    )
    op.create_index(
        "ix_task_attempts_task_id_state", "task_attempts", ["task_id", "state"]
    )

    op.create_table(
        "task_dependencies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("depends_on_task_id", sa.Text(), nullable=False),
        sa.Column(
            "type", sa.Text(), server_default=sa.text("'blocks'"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], ondelete="CASCADE",
            name="fk_task_dependencies_task_id_tasks",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id"], ["tasks.task_id"], ondelete="RESTRICT",
            name="fk_task_dependencies_depends_on_task_id_tasks",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_dependencies"),
        sa.UniqueConstraint(
            "task_id", "depends_on_task_id", "type",
            name="uq_task_dependencies_triplet",
        ),
        sa.CheckConstraint("task_id <> depends_on_task_id", name="not_self"),
        sa.CheckConstraint(
            "type IN ('blocks', 'relates_to', 'supersedes')", name="type_allowed"
        ),
    )
    op.create_index(
        "ix_task_dependencies_depends_on", "task_dependencies", ["depends_on_task_id"]
    )

    op.create_table(
        "task_leases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], ondelete="CASCADE",
            name="fk_task_leases_task_id_tasks",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_leases"),
        sa.UniqueConstraint(
            "task_id", "fencing_token", name="uq_task_leases_task_fencing_token"
        ),
        sa.CheckConstraint("fencing_token >= 1", name="fencing_token_positive"),
        sa.CheckConstraint(
            "lease_expires_at > acquired_at", name="expires_after_acquired"
        ),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at",
            name="released_after_acquired",
        ),
    )
    op.create_index(
        "uq_task_leases_active_task",
        "task_leases",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index("ix_task_leases_expires_at", "task_leases", ["lease_expires_at"])
    op.create_index("ix_task_leases_worker_id", "task_leases", ["worker_id"])

    op.create_table(
        "task_confirmations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("confirmation_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column(
            "canonicalization", sa.Text(),
            server_default=sa.text("'rfc8785_jcs'"), nullable=False,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("confirmed_by_principal_id", sa.Text(), nullable=True),
        sa.Column("satisfied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_call_id", sa.Text(), nullable=True),
        sa.Column(
            "single_use", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("channel_binding", _JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], ondelete="CASCADE",
            name="fk_task_confirmations_task_id_tasks",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_confirmations"),
        sa.UniqueConstraint(
            "confirmation_id", name="uq_task_confirmations_confirmation_id"
        ),
        sa.CheckConstraint(
            f"confirmation_id ~ '{_CONFIRMATION_ID}'", name="confirmation_id_format"
        ),
        sa.CheckConstraint(
            f"payload_hash ~ '{_SHA256}'", name="payload_hash_format"
        ),
        sa.CheckConstraint(
            "canonicalization = 'rfc8785_jcs'", name="canonicalization_allowed"
        ),
        sa.CheckConstraint(
            "purpose IN ('high_impact_action', 'data_release', "
            "'irreversible_action', 'payload_change', 'manual_override')",
            name="purpose_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'satisfied', 'consumed', 'rejected', "
            "'expired')",
            name="status_allowed",
        ),
        sa.CheckConstraint("single_use", name="single_use_required"),
        sa.CheckConstraint("expires_at > issued_at", name="expires_after_issued"),
        sa.CheckConstraint(
            "confirmed_by_principal_id IS NULL OR "
            "confirmed_by_principal_id ~ "
            "'^(prin|rt|svc)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'",
            name="confirmed_by_format",
        ),
        sa.CheckConstraint(
            "status <> 'satisfied' OR (confirmed_by_principal_id IS NOT NULL "
            "AND satisfied_at IS NOT NULL AND consumed_at IS NULL "
            "AND consumed_by_call_id IS NULL)",
            name="satisfied_fields",
        ),
        sa.CheckConstraint(
            "status <> 'consumed' OR (satisfied_at IS NOT NULL "
            "AND consumed_at IS NOT NULL AND consumed_by_call_id IS NOT NULL)",
            name="consumed_fields",
        ),
        sa.CheckConstraint(
            "status NOT IN ('pending', 'rejected') OR (satisfied_at IS NULL "
            "AND consumed_at IS NULL AND consumed_by_call_id IS NULL)",
            name="unsatisfied_fields",
        ),
        sa.CheckConstraint(
            "status <> 'expired' OR (consumed_at IS NULL AND "
            "consumed_by_call_id IS NULL)",
            name="expired_fields",
        ),
    )
    op.create_index(
        "ix_task_confirmations_task_id_status",
        "task_confirmations",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_task_confirmations_expires_at", "task_confirmations", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_confirmations_expires_at", table_name="task_confirmations")
    op.drop_index(
        "ix_task_confirmations_task_id_status", table_name="task_confirmations"
    )
    op.drop_table("task_confirmations")

    op.drop_index("ix_task_leases_worker_id", table_name="task_leases")
    op.drop_index("ix_task_leases_expires_at", table_name="task_leases")
    op.drop_index("uq_task_leases_active_task", table_name="task_leases")
    op.drop_table("task_leases")

    op.drop_index(
        "ix_task_dependencies_depends_on", table_name="task_dependencies"
    )
    op.drop_table("task_dependencies")

    op.drop_index("ix_task_attempts_task_id_state", table_name="task_attempts")
    op.drop_table("task_attempts")

    op.execute(_DROP_APPEND_ONLY_TRIGGER)
    op.execute(_DROP_APPEND_ONLY_FUNCTION)
    op.drop_index("ix_task_events_event_type", table_name="task_events")
    op.drop_index("ix_task_events_task_id_created_at", table_name="task_events")
    op.drop_table("task_events")

    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_index("uq_tasks_live_requester_goal", table_name="tasks")
    op.drop_index("ix_tasks_goal_hash", table_name="tasks")
    op.drop_index("ix_tasks_grant_id", table_name="tasks")
    op.drop_index("ix_tasks_decision_id", table_name="tasks")
    op.drop_index("ix_tasks_executor_principal_id", table_name="tasks")
    op.drop_index("ix_tasks_assignee_principal_id", table_name="tasks")
    op.drop_index("ix_tasks_owner_principal_id", table_name="tasks")
    op.drop_index("ix_tasks_requester_principal_id", table_name="tasks")
    op.drop_index("ix_tasks_state_priority_due", table_name="tasks")
    op.drop_table("tasks")
