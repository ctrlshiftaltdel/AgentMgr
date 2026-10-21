"""Fixtures for governance tests (real PostgreSQL test database)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Employee
from app.governance.models import (
    ExecutionGrant,
    GovernanceDecisionRecord,
    GovernanceDelegation,
    GovernancePolicyVersion,
    ServiceIdentity,
    ServiceIdentityRole,
)
from app.identity.models import EmployeeChannelBinding, EmployeeRole
from app.identity.seed import seed_permission_catalog
from app.tests.governance.helpers import (
    Org,
    PolicyContext,
    alembic_config,
    make_org,
    publish_policy,
)


@pytest.fixture(scope="module")
def _schema(test_db_url: str) -> None:
    """Ensure the test database is migrated to head before DB-backed tests.

    Not autouse: the policy/canonicalization tests in this directory are pure
    and must run even when PostgreSQL is unavailable.
    """
    command.upgrade(alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str, _schema: None) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    for model in (
        ExecutionGrant,
        GovernanceDecisionRecord,
        GovernanceDelegation,
        GovernancePolicyVersion,
        ServiceIdentityRole,
        ServiceIdentity,
        EmployeeChannelBinding,
        EmployeeRole,
        Employee,
        Company,
    ):
        session.execute(delete(model))


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            seed_permission_catalog(sess)
            sess.commit()
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


@pytest.fixture()
def org(session: Session) -> Org:
    return make_org(session)


@pytest.fixture()
def policy(session: Session, org: Org) -> PolicyContext:
    return publish_policy(session, org)
