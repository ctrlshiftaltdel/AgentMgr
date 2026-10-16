"""报销域核心数据模型：费用类型、成本中心、报销单、费用明细、发票挂账与付款记录。

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
* ``expense_lines`` 通过 ``ON DELETE CASCADE`` 随报销单删除；``expense_invoices``
  同样随报销单级联删除；
* ``expense_invoices.verification_status`` 的取值集合**从
  ``app.integrations.models.STATUS_VALUES`` 导入**（与查验记录的七状态同一集合，
  不得自造取值）；该列是查验记录状态的副本，唯一合法来源是
  ``app.integrations.invoice_verification`` 的七状态，调用方不得指定；
* ``expense_invoices`` 以 ``(claim_id, duplicate_key)`` 唯一（同一张票在同一报销
  单内只挂一次）；``duplicate_key`` 是票面身份的单向摘要
  （``compute_duplicate_key``），与查验域同口径。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

# 发票查验七状态的**唯一来源**：报销域不得自造取值（任务 W1-23）。该集合与
# ``invoice_verifications.status`` 的 CHECK 完全相同，因此挂账行的状态列可以
# 逐字复制查验记录而无需另行映射。
from app.integrations.models import STATUS_VALUES

# 固定精度金额类型：NUMERIC(18, 2)。集中定义，确保五张表口径一致，且任何
# 后续改动只能在一处发生（避免个别列退化为浮点）。
MONEY = Numeric(18, 2)

#: ``verification_status`` 的 CHECK 表达式片段（七状态，来自查验域）。
_VERIFICATION_STATUS_SQL = ", ".join(f"'{value}'" for value in STATUS_VALUES)


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


class ExpenseInvoice(TimestampMixin, Base):
    """报销单与发票的关联（挂账）行：一张发票挂在某张报销单上。

    * ``invoice_fields_json``：调用方提交的**票面要素快照**
      （``invoice_code`` / ``invoice_no`` / ``invoice_date``（ISO 字符串）/
      ``invoice_kind`` / ``amount``（字符串定点，禁止 float）），**不含税号**；
    * ``verification_status``：查验记录状态的副本，取值集合与
      ``invoice_verifications.status`` 完全相同（七状态，来自查验域）；该列
      **只能由服务层**从查验记录推导/同步（``app.expense.invoices``），调用方
      不得指定，更不得把任何挂账行直接写成「查验通过」；
    * ``duplicate_key``：票面身份的单向摘要（``compute_duplicate_key``，与查验域
      同口径），``(claim_id, duplicate_key)`` 唯一——同一张票在同一报销单内只
      挂一次；跨报销单出现同一张票时由服务层在两边的行上置
      ``suspected_duplicate=True``；
    * ``source_file_id``：该票原件引用（可空），与
      ``invoice_verifications.source_file_id`` 同口径。
    """

    __tablename__ = "expense_invoices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expense_claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_files.id"), nullable=True
    )
    invoice_fields_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    verification_status: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_key: Mapped[str] = mapped_column(Text, nullable=False)
    suspected_duplicate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        UniqueConstraint(
            "claim_id", "duplicate_key", name="uq_expense_invoices_claim_duplicate"
        ),
        CheckConstraint(
            f"verification_status IN ({_VERIFICATION_STATUS_SQL})",
            name="verification_status_allowed",
        ),
        Index("ix_expense_invoices_duplicate_key", "duplicate_key"),
        Index("ix_expense_invoices_claim_id", "claim_id"),
    )


__all__ = [
    "CostCenter",
    "ExpenseCategory",
    "ExpenseClaim",
    "ExpenseInvoice",
    "ExpenseLine",
    "ReimbursementPayment",
]
