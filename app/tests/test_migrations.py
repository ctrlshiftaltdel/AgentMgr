"""Migration round-trip test against the dedicated test database.

Runs ``upgrade head`` -> ``downgrade base`` -> ``upgrade head``, asserting each
step succeeds and that the business tables appear/disappear accordingly.  The
test cleans up by downgrading to base so no business tables are left behind.

W1-16 additionally asserts that ``0003_permission_catalog`` is a *frozen
snapshot*: at that revision the catalog is exactly the 7/19/31 state it was
authored with (not whatever ``app.identity.permissions`` holds now), and every
database reaches the same 7/20/32 head state.  The revision must also stay
self-contained -- no ``app.*`` import -- so it can never read active code again.
"""

from __future__ import annotations

import ast
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[2]
BUSINESS_TABLES = {"companies", "departments", "employees"}

CATALOG_REVISION = "0003_permission_catalog"


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


def _table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _catalog_counts(engine: Engine) -> tuple[int, int, int]:
    """Return ``(roles, permissions, role_permissions)`` row counts."""
    with engine.connect() as connection:
        roles = connection.scalar(text("SELECT count(*) FROM roles"))
        permissions = connection.scalar(text("SELECT count(*) FROM permissions"))
        links = connection.scalar(text("SELECT count(*) FROM role_permissions"))
    return int(roles), int(permissions), int(links)


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


def test_0003_is_frozen_snapshot_and_head_matches(test_db_url: str) -> None:
    """W1-16: ``0003`` writes its frozen 7/19/31; head is 7/20/32 everywhere."""
    engine = create_engine(test_db_url)
    config = _alembic_config()
    try:
        command.downgrade(config, "base")

        # At the frozen revision the catalog is the snapshot the revision was
        # authored with -- NOT the live 7/20/32 catalog.  This is what makes the
        # chain reproducible instead of drifting with app.identity.permissions.
        command.upgrade(config, CATALOG_REVISION)
        assert _catalog_counts(engine) == (7, 19, 31)

        # 0009_perm_alignment lands the 20th permission and the 32nd mapping, so
        # head carries the same catalog content that every database reaches,
        # whether freshly migrated or historically migrated.
        command.upgrade(config, "head")
        assert _catalog_counts(engine) == (7, 20, 32)
    finally:
        # Leave the test database at head for the rest of the suite.
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        engine.dispose()


def test_0003_does_not_import_application_code() -> None:
    """W1-16: the frozen snapshot must not read active ``app.*`` code."""
    source = (
        REPO_ROOT / "migrations" / "versions" / "0003_permission_catalog.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [
        name for name in imported if name == "app" or name.startswith("app.")
    ]
    assert offenders == [], f"0003 must not import application code: {offenders}"
