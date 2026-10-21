"""Persistent task aggregate (Issue #79).

The package owns the **task** domain: the durable task record, its append-only
event log, execution attempts, dependencies, worker leases, confirmations and
the state machine that joins them.

It deliberately does **not** create a second organisation, approval or outbox
system:

* organisation/RBAC facts come from :mod:`app.db.models.organization` and
  :mod:`app.identity`;
* policy decisions, execution grants and per-call enforcement come from
  :mod:`app.governance` (Issue #78);
* confirmations are task-owned (they bind one task payload), while **approvals**
  reuse ``workflow_instances`` / ``approval_steps`` through
  :mod:`app.workflow.service`;
* state-change notifications are written to the existing transactional
  ``outbox_events`` table through :func:`app.workflow.service.enqueue_event`.

Everything here is transport-free and never commits: the caller owns the
transaction boundary so a task change, its event, its audit row and its outbox
row commit atomically.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Version of the task contract implemented by this package.
__version__ = "1.0.0"
