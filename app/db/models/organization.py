"""Organization core tables: companies, departments, employees.

Design fixed by the task (W1-1) and the project charter (templates/PLAN.md §8):
BIGINT identity primary keys, ``created_at``/``updated_at`` as ``timestamptz NOT
NULL DEFAULT now()``, and explicit CHECK/UNIQUE constraints.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TimestampMixin:
    """Adds the mandatory audit timestamps shared by every core table."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Company(TimestampMixin, Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'CN'")
    )
    base_currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'CNY'")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'closed')",
            name="status_allowed",
        ),
    )


class Department(TimestampMixin, Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # PostgreSQL 17 supports NULLS NOT DISTINCT so top-level departments
        # (parent_id IS NULL) are also protected against duplicate names.
        UniqueConstraint(
            "company_id",
            "parent_id",
            "name",
            name="uq_departments_company_parent_name",
            postgresql_nulls_not_distinct=True,
        ),
    )


class Employee(TimestampMixin, Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id"), nullable=False
    )
    department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id"), nullable=True
    )
    manager_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=True
    )
    employee_no: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "employee_no", name="uq_employees_company_no"),
        CheckConstraint(
            "status IN ('active', 'inactive', 'left')",
            name="status_allowed",
        ),
        CheckConstraint(
            "manager_id IS NULL OR manager_id <> id",
            name="manager_not_self",
        ),
    )
