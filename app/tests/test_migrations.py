"""Migration round-trip test against the dedicated test database.

Runs ``upgrade head`` -> ``downgrade base`` -> ``upgrade head``, asserting each
step succeeds and that the business tables appear/disappear accordingly.  The
test cleans up by downgrading to base so no business tables are left behind.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[2]
BUSINESS_TABLES = {"companies", "departments", "employees"}


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


def _table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_migration_roundtrip(test_db_url: str) -> None:
    engine = create_engine(test_db_url)
    config = _alembic_config()
    try:
        # Start from a clean slate (only affects the test database).
        command.downgrade(config, "base")
        assert BUSINESS_TABLES.isdisjoint(_table_names(engine))

        # 1) upgrade head
        command.upgrade(config, "head")
        assert BUSINESS_TABLES.issubset(_table_names(engine))

        # 2) downgrade base
        command.downgrade(config, "base")
        assert BUSINESS_TABLES.isdisjoint(_table_names(engine))

        # 3) upgrade head again
        command.upgrade(config, "head")
        assert BUSINESS_TABLES.issubset(_table_names(engine))
    finally:
        # Leave the test database without business tables.
        command.downgrade(config, "base")
        remaining = _table_names(engine)
        engine.dispose()

    assert BUSINESS_TABLES.isdisjoint(remaining)
