"""Fixtures for the task-domain tests (real PostgreSQL test database).

The tests never mock the database, the policy engine, the workflow service or
the audit ledger.  Each test starts from a truncated business state and a seeded
permission catalog; the append-only ``task_events``/``audit_events`` tables are
emptied with ``TRUNCATE`` (which does not fire row triggers), exactly like the
root test conftest does for ``audit_events``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.governance.principal import GovernancePrincipal, resolve_employee_principal
from app.identity.seed import seed_permission_catalog
from app.tests.governance.helpers import (
    Org,
    PolicyContext,
    alembic_config,
    make_org,
    publish_policy,
    scope,
)

#: Children first.  ``roles``/``permissions`` are intentionally kept: the
#: permission catalog is a static baseline, not per-test business state.
_TRUNCATE = (
    "TRUNCATE TABLE "
    "task_confirmations, task_leases, task_dependencies, task_attempts, "
    "task_events, tasks, outbox_events, notifications, approval_steps, "
    "workflow_instances, execution_grants, governance_decisions, "
    "governance_delegations, governance_policy_versions, "
    "service_identity_roles, service_identities, employee_channel_bindings, "
    "employee_roles, employees, departments, companies "
    "RESTART IDENTITY CASCADE"
)


def reset_task_state(engine: Engine) -> None:
    tables = set(
        row[0]
        for row in engine.connect().execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        )
    )
    if "tasks" not in tables:
        return
    with engine.begin() as connection:
        connection.execute(text(_TRUNCATE))


@pytest.fixture(scope="module")
def _schema(test_db_url: str) -> None:
    command.upgrade(alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str, _schema: None) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    reset_task_state(engine)
    with Session(engine) as sess:
        seed_permission_catalog(sess)
        sess.commit()
        try:
            yield sess
        finally:
            sess.rollback()
            reset_task_state(engine)


@dataclass(frozen=True)
class TaskActors:
    requester: GovernancePrincipal
    owner: GovernancePrincipal
    executor: GovernancePrincipal
    approver: GovernancePrincipal
    outsider: GovernancePrincipal


@dataclass(frozen=True)
class TaskWorld:
    org: Org
    policy: PolicyContext
    actors: TaskActors


@pytest.fixture()
def org(session: Session) -> Org:
    return make_org(session)


@pytest.fixture()
def policy(session: Session, org: Org) -> PolicyContext:
    return publish_policy(session, org)


@pytest.fixture()
def world(session: Session, org: Org, policy: PolicyContext) -> TaskWorld:
    actors = TaskActors(
        requester=resolve_employee_principal(session, employee_id=org.employee.id),
        owner=resolve_employee_principal(session, employee_id=org.manager.id),
        executor=resolve_employee_principal(session, employee_id=org.employee.id),
        approver=resolve_employee_principal(session, employee_id=org.finance.id),
        outsider=resolve_employee_principal(session, employee_id=org.auditor.id),
    )
    # Commit the organisation/policy so concurrent test sessions can see them;
    # each subsequent operation still owns its own transaction.
    session.commit()
    return TaskWorld(org=org, policy=policy, actors=actors)


@pytest.fixture()
def cascade_scope() -> dict:
    return scope()
