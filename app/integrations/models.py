"""外部集成域数据模型：发票查验记录 ``invoice_verifications``。

列结构对齐 ``templates/PLAN.md`` §8「外部集成」表清单——
``invoice_verifications(id, source_file_id, provider_type, provider_ref, status,
verified_at, verified_by, evidence_ref)``——并**补齐** §7.2 要求保存的全部字段
（发票代码/号码、开票日期、销售方与购买方税号、金额、税额、价税合计、币种、票据
类型、原件引用、重复检查结果、查验渠道、查验时间、操作人、结果状态、证据引用）。
字段来源对照见 ``docs/research/W0-4-invoice-verification.md``（W0-4）§8。

关键不变量：

* **金额用固定精度 decimal**：``amount_without_tax`` / ``tax_amount`` /
  ``amount_with_tax`` 一律 ``NUMERIC(18, 2)``，**绝不使用浮点**（PLAN §8「金额采用
  固定精度 decimal，不用浮点数保存财务金额」）；三列均可空，因为渠道在
  「无法查验」时不会返回票面，此时不得臆造金额（W0-4 §8 的诚实标注）；
* **税号字段受权限保护**：``seller_tax_code`` / ``buyer_tax_code`` 在 W0-5 §3.1 的
  字段分级中属 **B 类（必须脱敏后才可入模型/日志）**，是**敏感字段**。读取/展示这
  两列必须经过权限判定——仅拥有 ``invoice/read/company``（财务经办/财务主管）或
  ``invoice/read/authorized``（审计人员只读授权范围）的角色可见；**禁止**把它们
  写进审计 ``details``、普通日志或模型提示词（W0-5 §3.2 日志禁止项）；
* **``duplicate_key`` 建唯一约束以支撑幂等**：同一张票（同一身份摘要）重复查验
  **不产生第二条记录**（本任务第四步第 3 条）。该键是票面身份的单向 sha256 摘要，
  不含票号/代码明文，可安全写入日志与审计；
* **``evidence_ref`` 指向脱敏后的证据存储**：``evidence_json`` 保存经
  ``app.integrations.invoice_verification.sanitize_evidence`` 脱敏后的渠道原始响应
  （票号/代码/校验码/税号打码、凭据类键整体丢弃），``evidence_ref`` 是这份脱敏证据
  的内容寻址引用（``sha256:<hex>``），可用于校验证据未被篡改；将来若把证据迁到对象
  存储，只需保留该引用（PLAN §8「文件原件与业务元数据分开保存」的同一思路）；
* ``status`` / ``provider_type`` / ``duplicate_check_result`` 用 ``TEXT`` + ``CHECK``
  限定取值集合（不用数据库 ENUM，便于演进）；
* 计数列（``verify_attempt_count`` / ``daily_verify_count``）支撑渠道的「同一张票
  当日查验次数上限」（W0-4 §2.5：超过 5 次当日不可再查）；
* ``verified_by`` 指向 ``employees.id``：操作人必须是**实际人员**，不得填模型名字或
  角色名（PLAN §9）；员工按 ``status='left'`` 软删除，因此该引用在实践中仍可解析。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

# W1-24 导入自足性：``invoice_verifications.source_file_id`` 声明了指向
# ``source_files.id`` 的外键，而 ``source_files`` 定义在 ``app.documents.models``。
# 显式导入该模块，使本模块被单独导入时 ``source_files`` 也注册到
# ``Base.metadata``，否则 ``configure_mappers`` / 外键解析 / Alembic autogenerate
# 会因找不到被引用表而失败（此前依赖 migrations/env.py 与 conftest 的统一导入规避）。
import app.documents.models  # noqa: E402,F401  (registers ``source_files``)

__all__ = ["InvoiceVerification"]

#: 固定精度金额类型：``NUMERIC(18, 2)``（与 ``app.expense.models`` 口径一致）。
MONEY = Numeric(18, 2)

#: ``status`` 允许的取值（严格对应 PLAN §7.2 的七种状态）。
STATUS_VALUES: tuple[str, ...] = (
    "pending_identification",
    "pending_manual_confirmation",
    "pending_verification",
    "verified_pass",
    "verified_fail",
    "unable_to_verify",
    "suspected_duplicate",
)

#: ``provider_type`` 允许的取值（PLAN §7.1 的三类实现）。
PROVIDER_TYPE_VALUES: tuple[str, ...] = (
    "manual_assisted",
    "authorized_provider",
    "official_api",
)

#: ``duplicate_check_result`` 允许的取值（业务去重结果，渠道不提供）。
DUPLICATE_CHECK_VALUES: tuple[str, ...] = ("unique", "suspected_duplicate")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class InvoiceVerification(TimestampMixin, Base):
    """一条发票查验记录（同一张票只有一行——``duplicate_key`` 唯一）。

    列语义详见模块 docstring。**税号字段（``seller_tax_code`` /
    ``buyer_tax_code``）受权限保护**，读取/展示必须经过授权判定。
    """

    __tablename__ = "invoice_verifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # --- 原件引用 -------------------------------------------------------------
    # 原始附件不得被查验结果覆盖，这里只保存对 source_files 的引用（PLAN §4.2）。
    source_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_files.id"), nullable=True
    )

    # --- 票面字段（PLAN §7.2） ------------------------------------------------
    invoice_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_no: Mapped[str] = mapped_column(Text, nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 受权限保护（B 类敏感字段）：仅授权角色可读，禁止入日志/审计/提示词。
    seller_tax_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_tax_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_without_tax: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    tax_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    amount_with_tax: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'CNY'")
    )
    invoice_type: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 幂等与去重 -----------------------------------------------------------
    # 票面身份的单向 sha256 摘要；唯一约束保证「同一张票只有一行」。
    duplicate_key: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_check_result: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unique'")
    )

    # --- 查验渠道与结果 -------------------------------------------------------
    provider_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 证据 -----------------------------------------------------------------
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # --- 时间与操作人 ---------------------------------------------------------
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=True
    )

    # --- 幂等/限次计数 --------------------------------------------------------
    verify_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    daily_verify_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_verify_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "duplicate_key", name="uq_invoice_verifications_duplicate_key"
        ),
        CheckConstraint(_in_list("status", STATUS_VALUES), name="status_allowed"),
        CheckConstraint(
            _in_list("provider_type", PROVIDER_TYPE_VALUES),
            name="provider_type_allowed",
        ),
        CheckConstraint(
            _in_list("duplicate_check_result", DUPLICATE_CHECK_VALUES),
            name="duplicate_check_result_allowed",
        ),
        CheckConstraint(
            "amount_without_tax IS NULL OR amount_without_tax >= 0",
            name="amount_without_tax_non_negative",
        ),
        CheckConstraint(
            "tax_amount IS NULL OR tax_amount >= 0",
            name="tax_amount_non_negative",
        ),
        CheckConstraint(
            "amount_with_tax IS NULL OR amount_with_tax >= 0",
            name="amount_with_tax_non_negative",
        ),
        CheckConstraint("verify_attempt_count >= 0", name="verify_attempt_count_non_negative"),
        CheckConstraint("daily_verify_count >= 0", name="daily_verify_count_non_negative"),
        Index("ix_invoice_verifications_invoice_no", "invoice_no"),
        Index("ix_invoice_verifications_source_file_id", "source_file_id"),
        Index("ix_invoice_verifications_status", "status"),
    )
