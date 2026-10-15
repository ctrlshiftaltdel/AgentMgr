"""Shared pytest fixtures for the backend test suite.

Environment variables are set at import time, before any ``app.*`` module is
imported, so both the application engine and the Alembic tooling run against the
test database (``AJAIMGR_TEST_DB_URL``) for the whole session.
"""

from __future__ import annotations

import os

TEST_DB_URL = os.environ.get("AJAIMGR_TEST_DB_URL", "postgresql+psycopg:///ajaimgr_test")

# Force the test database for the duration of the test session.  The default
# (dev) DSN is only ever used outside of tests.
os.environ["AJAIMGR_TEST_DB_URL"] = TEST_DB_URL
os.environ["AJAIMGR_DB_URL"] = TEST_DB_URL
os.environ.setdefault("AJAIMGR_ENV", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


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
