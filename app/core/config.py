"""Application configuration.

Configuration is read directly from environment variables using ``os.environ``
plus a frozen dataclass, so no third-party settings library is required.

Defaults are deliberately credential-free: the default DSNs point at the local
PostgreSQL unix socket and rely on peer authentication, therefore no password,
token or other secret is ever embedded in this repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Local development DSN: unix-socket connection + peer authentication.
DEFAULT_DB_URL = "postgresql+psycopg:///ajaimgr_dev"
DEFAULT_TEST_DB_URL = "postgresql+psycopg:///ajaimgr_test"
DEFAULT_ENV = "dev"


def _read(name: str, default: str) -> str:
    """Return ``os.environ[name]`` stripped, or ``default`` when unset/blank."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    env: str
    db_url: str
    test_db_url: str

    @property
    def is_dev(self) -> bool:
        return self.env == DEFAULT_ENV


def load_settings() -> Settings:
    """Read settings fresh from the environment.

    Called on demand (rather than only at import time) so tooling such as
    Alembic can observe environment changes made after the process started.
    """
    return Settings(
        env=_read("AJAIMGR_ENV", DEFAULT_ENV),
        db_url=_read("AJAIMGR_DB_URL", DEFAULT_DB_URL),
        test_db_url=_read("AJAIMGR_TEST_DB_URL", DEFAULT_TEST_DB_URL),
    )


# Import-time snapshot for the common case (application code and health checks).
settings = load_settings()
