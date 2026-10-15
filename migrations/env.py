"""Alembic migration environment.

The DSN is read from ``app.core.config`` (environment driven), never from
alembic.ini, so no credentials are committed.  ``target_metadata`` points at the
shared declarative base so autogenerate sees every ORM model.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repository root importable regardless of the invocation cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import load_settings  # noqa: E402
from app.db.base import Base  # noqa: E402

# Importing the models package registers all tables on Base.metadata.
import app.db.models  # noqa: E402,F401

# Identity/RBAC models live in their own domain package; importing them here
# registers roles/permissions/bindings tables on Base.metadata so that
# ``alembic check`` / autogenerate can see them.
import app.identity.models  # noqa: E402,F401

# Audit models live in their own domain package; importing them here registers
# the append-only ``audit_events`` table on Base.metadata so that
# ``alembic check`` / autogenerate can see it.
import app.audit.models  # noqa: E402,F401

# Expense-domain models live in their own package; importing them here registers
# expense_categories/cost_centers/expense_claims/expense_lines/
# reimbursement_payments on Base.metadata so that ``alembic check`` /
# autogenerate can see them.
import app.expense.models  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the DSN from the environment at run time."""
    return load_settings().db_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
