"""统一资料接收流水线（intake）核心数据模型：受理作业与解析草稿。

列结构由 Master 在任务 W1-9 中统一固定，并对齐：

* ``templates/PLAN.md`` §4.2「统一资料接收流水线」——接收原始资料 → 身份/权限
  验证 → 文件类型/大小/安全扫描与去重 → 解析 → **形成结构化草稿 + 字段置信度 +
  原件引用** → 追问/确认 → 后端校验 → 提交/审批 → 写入正式业务表。该节同时
  强制：**「原始文件不得被解析结果覆盖」**、**「低置信度或关键字段缺失时，应
  要求人工确认」**；
* ``templates/PLAN.md`` §8「工作流、收件和文件」表清单——
  ``intake_jobs(id, submitted_by, channel_type, input_type, status, received_at)``
  与 ``extraction_drafts(id, intake_job_id, business_type, extracted_json,
  confidence_json, review_status)``；
* ``templates/PLAN.md`` §8「数据完整性要求」——
  **「外部 API 返回内容必须视为不可信数据」**；
* ``docs/research/W0-5-security-compliance.md`` §4.3（恶意文件上传）；
* ``docs/research/W0-1-weixin-ilink.md`` §6.3（**附件一律视为不可信输入**）。

关键不变量：

* 主键均为 ``BIGINT`` 自增，``created_at``/``updated_at`` 为
  ``timestamptz NOT NULL DEFAULT now()``；
* ``input_type`` / ``status`` / ``review_status`` 用 ``TEXT`` + ``CHECK`` 限定
  取值集合（不用数据库 ENUM，便于演进）；
* ``extraction_drafts`` 随 ``intake_jobs`` ``ON DELETE CASCADE`` 删除；
  ``source_file_id`` 可空并指向 ``source_files``——这是**原件引用**，原始文件不
  被草稿覆盖（PLAN §4.2），草稿只承载「模型提取结果 + 置信度」；
* 本层**不写正式业务表**：解析结果先进入草稿区，需人工确认后才由后端写入报销/
  账务等正式表（PLAN §4.2、§11「W1-4 不得直接将解析结果写入正式报销/账务字段」）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

__all__ = ["ExtractionDraft", "IntakeJob"]


class IntakeJob(TimestampMixin, Base):
    """一次资料受理作业：一条来自某个渠道、某种输入类型的原始资料接收记录。

    ``input_type`` 取值集合固定为 ``text`` / ``image`` / ``file`` / ``voice`` /
    ``spreadsheet``（对应 PLAN §4.1 支持的输入）；``status`` 取值集合固定为
    ``received`` / ``parsed`` / ``needs_review`` / ``failed``。状态迁移由确定性
    后端驱动（PROJECT-CHARTER 强制原则 6），本层只承载数据与约束。
    """

    __tablename__ = "intake_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 提交人（内部员工）；不接受模型自报身份（PLAN §2.1 第 4 点）。
    submitted_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'weixin'")
    )
    input_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'received'")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "input_type IN ('text', 'image', 'file', 'voice', 'spreadsheet')",
            name="input_type_allowed",
        ),
        CheckConstraint(
            "status IN ('received', 'parsed', 'needs_review', 'failed')",
            name="status_allowed",
        ),
    )


class ExtractionDraft(TimestampMixin, Base):
    """一条解析草稿：某次受理作业的模型提取结果 + 字段置信度 + 原件引用。

    * ``extracted_json`` / ``confidence_json`` 为 ``JSONB NOT NULL DEFAULT
      '{}'::jsonb``——模型输出**一律视为不可信数据**，只作为草稿保存，须经结构
      校验与后端规则裁决后才进入正式业务表（PLAN §4.2、§8）；
    * ``source_file_id`` 可空并指向 ``source_files``：原始文件不得被解析结果覆盖，
      草稿只保存对原件的引用（PLAN §4.2）；
    * ``review_status`` 取值集合固定为 ``pending`` / ``confirmed`` / ``rejected``；
      低置信度或关键字段缺失时保持 ``pending``，要求人工确认（PLAN §4.2）。
    """

    __tablename__ = "extraction_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    intake_job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("intake_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_files.id"), nullable=True
    )
    business_type: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    confidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )

    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'rejected')",
            name="review_status_allowed",
        ),
    )
