"""报销域核心数据模型：费用类型、成本中心、报销单、费用明细与付款记录。

列结构由 Master 在任务 W1-6 中统一固定，并对齐：

* ``templates/PLAN.md`` §5.2「报销模块（第一期）」与 §8「报销」表清单；
* ``templates/PLAN.md`` §8「数据完整性要求」——
  **「金额采用固定精度 decimal，不用浮点数保存财务金额」**；
* ``docs/PROJECT-CHARTER.md`` 强制原则 6——
  金额计算必须由确定性程序驱动。

关键不变量：

* 所有金额列一律 ``NUMERIC(18, 2)``（``Decimal``），**绝不使用 ``Float``/``REAL``/
  ``DOUBLE``**：财务金额必须以固定精度保存，浮点二进制表示会在舍入上产生偏差；
* 主键均为 ``BIGINT`` 自增，``created_at``/``updated_at`` 为
  ``timestamptz NOT NULL DEFAULT now()``；
* ``status`` 用 ``TEXT`` + ``CHECK`` 限定取值集合（不用数据库 ENUM，便于演进）；
* ``expense_categories``/``cost_centers`` 以 ``(company_id, code)`` 唯一；
* ``expense_claims.claim_no`` 全局唯一；``expense_claims.total_amount >= 0``；
  ``expense_lines.amount >= 0``、``tax_amount >= 0``；
  ``reimbursement_payments.amount > 0``；
* ``expense_lines`` 通过 ``ON DELETE CASCADE`` 随报销单删除。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

# 固定精度金额类型：NUMERIC(18, 2)。集中定义，确保五张表口径一致，且任何
# 后续改动只能在一处发生（避免个别列退化为浮点）。
MONEY = Numeric(18, 2)


class ExpenseCategory(TimestampMixin, Base):
    """费用类型（报销科目分类），按公司隔离。"""

    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "code", name="uq_expense_categories_company_code"
        ),
        CheckConstraint("status IN ('active', 'inactive')", name="status_allowed"),
    )


class CostCenter(TimestampMixin, Base):
    """成本中心，按公司隔离。"""

    __tablename__ = "cost_centers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_cost_centers_company_code"),
        CheckConstraint("status IN ('active', 'inactive')", name="status_allowed"),
    )


class ExpenseClaim(TimestampMixin, Base):
    """报销单（表头）。

    ``status`` 取值集合与 ``app.expense.state_machine`` 的状态机一致；状态转换
    由确定性程序驱动（PROJECT-CHARTER 强制原则 6），本层只承载数据与约束。
    """

    __tablename__ = "expense_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    claim_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    employee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'CNY'")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'withdrawn', 'approved', "
            "'finance_reviewed', 'paid', 'rejected')",
            name="status_allowed",
        ),
    )


class ExpenseLine(TimestampMixin, Base):
    """报销明细行：金额与税额均为固定精度 ``NUMERIC(18, 2)``。"""

    __tablename__ = "expense_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expense_claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("expense_categories.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    cost_center_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("cost_centers.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_amount_non_negative"),
    )


class ReimbursementPayment(TimestampMixin, Base):
    """报销付款记录：金额必须严格为正。"""

    __tablename__ = "reimbursement_payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("expense_claims.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'CNY'")
    )
    payment_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'paid', 'failed')", name="status_allowed"
        ),
    )


__all__ = [
    "CostCenter",
    "ExpenseCategory",
    "ExpenseClaim",
    "ExpenseLine",
    "ReimbursementPayment",
]
