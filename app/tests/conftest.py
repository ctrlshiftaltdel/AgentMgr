"""Shared pytest fixtures for the backend test suite.

Environment variables are set at import time, before any ``app.*`` module is
imported, so both the application engine and the Alembic tooling run against the
test database (``AJAIMGR_TEST_DB_URL``) for the whole session.

Database hygiene (task W1-14)
-----------------------------
A session-scoped autouse fixture (:func:`pristine_database`) migrates the test
database to head and then **fully resets every business table** before any test
runs, and again after the session.  This makes the suite repeatable even when a
third-party script (or an earlier crashed run) left residual rows behind: during
W1-13 a leftover ``expense_claims`` row referencing an employee made the identity
tests' teardown fail on a foreign key, and stray role/permission rows broke the
catalog count assertions, forcing the operator to ``dropdb``/``createdb``.

``audit_events`` is append-only **at the database layer** (a ``BEFORE UPDATE OR
DELETE`` trigger rejects row-level mutation, see migration ``0004_audit_events``),
so it can never be emptied with ``DELETE``; the reset therefore uses
``TRUNCATE ... RESTART IDENTITY CASCADE``, which does not fire row-level triggers.
All tables are truncated in a single statement so the ``CASCADE`` resolves the
foreign-key graph regardless of listing order, and the permission catalog is
re-seeded afterwards so the database matches its post-migration baseline
(7 roles / 20 permissions / 32 mappings).

The reset never replaces the real database with a mock or a skipped path: it is a
plain ``TRUNCATE`` against the same PostgreSQL test database the tests use.
"""

from __future__ import annotations

import os

TEST_DB_URL = os.environ.get("AJAIMGR_TEST_DB_URL", "postgresql+psycopg:///ajaimgr_test")

# Force the test database for the duration of the test session.  The default
# (dev) DSN is only ever used outside of tests.
os.environ["AJAIMGR_TEST_DB_URL"] = TEST_DB_URL
os.environ["AJAIMGR_DB_URL"] = TEST_DB_URL
os.environ.setdefault("AJAIMGR_ENV", "test")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every business table created by migrations ``0001``..``0009``, listed children
#: first.  ``alembic_version`` is deliberately excluded -- it must survive a reset
#: so the schema stays at head.
BUSINESS_TABLES: tuple[str, ...] = (
    "audit_events",
    "notifications",
    "outbox_events",
    "approval_steps",
    "workflow_instances",
    "reimbursement_payments",
    "expense_lines",
    "expense_claims",
    "cost_centers",
    "expense_categories",
    "extraction_drafts",
    "source_files",
    "intake_jobs",
    "employee_channel_bindings",
    "employee_roles",
    "role_permissions",
    "permissions",
    "roles",
    "employees",
    "departments",
    "companies",
)

#: Standalone sequence created by migration ``0006_expense_claim_no_seq``; it is
#: not owned by a table column, so ``RESTART IDENTITY`` does not reset it.
CLAIM_NO_SEQUENCE = "expense_claim_no_seq"


def alembic_config():
    """Return an Alembic ``Config`` pointed at this repository's migrations."""
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


def reset_business_state(engine: Engine) -> int:
    """Empty every business table, however dirty it is, and restart identities.

    Returns the number of tables that were truncated (``0`` when the schema is
    absent, e.g. right after ``test_migrations`` left the database at base).

    ``audit_events`` can only be emptied with ``TRUNCATE`` (its ``BEFORE UPDATE
    OR DELETE`` trigger rejects row-level deletes), so ``TRUNCATE ... RESTART
    IDENTITY CASCADE`` is used for the whole set.  A single statement lets
    PostgreSQL resolve the foreign-key graph via ``CASCADE``, so the listing
    order cannot cause an FK error.  The standalone claim-number sequence is
    restarted too, if it exists.
    """
    existing = set(inspect(engine).get_table_names())
    present = [table for table in BUSINESS_TABLES if table in existing]
    if not present:
        return 0
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(present)
                + " RESTART IDENTITY CASCADE"
            )
        )
        connection.execute(
            text(f"ALTER SEQUENCE IF EXISTS {CLAIM_NO_SEQUENCE} RESTART WITH 1")
        )
    return len(present)


def seed_permission_catalog_if_schema_present(engine: Engine) -> None:
    """Re-seed the permission catalog when the tables exist (post-migration state)."""
    from app.identity.seed import seed_permission_catalog

    if "roles" not in inspect(engine).get_table_names():
        return
    with Session(engine) as session:
        seed_permission_catalog(session)
        session.commit()


@pytest.fixture(scope="session", autouse=True)
def pristine_database(test_db_url: str):
    """Reset the test database to its post-migration baseline for the session.

    Runs once, before any test: migrate to head, truncate every business table
    (including the append-only ``audit_events``), then re-seed the permission
    catalog.  A second reset runs after the session so the next run starts clean
    even if a test left rows behind.  Any pre-existing dirty data -- however it
    got there -- is removed before the first test executes.
    """
    from alembic import command
    from sqlalchemy import create_engine

    engine = create_engine(test_db_url)
    try:
        command.upgrade(alembic_config(), "head")
        reset_business_state(engine)
        seed_permission_catalog_if_schema_present(engine)
        yield
        truncated = reset_business_state(engine)
        if truncated:
            seed_permission_catalog_if_schema_present(engine)
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """DSN of the dedicated test database."""
    return TEST_DB_URL


@pytest.fixture(scope="session")
def client() -> TestClient:
    """A ``TestClient`` bound to a freshly built application."""
    from app.api.app import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
