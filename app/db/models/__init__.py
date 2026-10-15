"""ORM models package.

Importing this package registers every model on ``app.db.base.Base.metadata``,
which Alembic uses as ``target_metadata``.
"""

from __future__ import annotations

from app.db.models.organization import Company, Department, Employee

__all__ = ["Company", "Department", "Employee"]
