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
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

# W1-24 导入自足性：``extraction_drafts.source_file_id`` 声明了指向
# ``source_files.id`` 的外键，而 ``source_files`` 定义在 ``app.documents.models``。
# 显式导入该模块，使本模块被单独导入时 ``source_files`` 也注册到
# ``Base.metadata``，否则 ``configure_mappers`` / 外键解析 / Alembic autogenerate
# 会因找不到被引用表而失败（此前依赖 migrations/env.py 与 conftest 的统一导入规避）。
import app.documents.models  # noqa: E402,F401  (registers ``source_files``)

__all__ = ["ExtractionDraft", "InboundMessage", "IntakeJob"]


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


class InboundMessage(TimestampMixin, Base):
    """一条**渠道入站消息**的受理锚点（Issue #21）：幂等键 + 受理结果引用。

    职责边界（对齐 ``docs/research/W0-2-profile-isolation.md`` §4.3/§4.4）：

    * **幂等锚点**：``UNIQUE (channel_type, external_message_id)`` 是「同一渠道消息
      只受理一次」的唯一真源。业务层的「先查后插」只是快路径，并发下由该约束兜底
      ——插入冲突时服务层捕获 ``IntegrityError`` 后回查既有行并返回 ``duplicate``
      （见 ``app.intake.inbound``），绝不把冲突泄漏成 500、也绝不重复创建受理作业。
    * **身份查找键，不是凭据**：``external_user_id`` 与
      ``employee_channel_bindings.external_user_id`` 同口径——明文 ``TEXT``，因为它
      是**查找键**（``(channel_type, external_user_id)`` 复合身份键，W0-1 §6.5），
      不是可用于认证的秘密。真正的授权真源仍是 ``employee_channel_bindings``；
      本表**不**承载任何授权判定结果，只记录「这条消息被受理成了哪个作业」。
    * **不存正文**：本表**刻意不存**消息正文、附件或任何外部载荷——正文只允许以
      适配器内存中的形式短暂存在（PLAN §8「外部 API 返回内容必须视为不可信数据」），
      本表只保留受理所需的键与引用，避免隐私数据在业务库中无谓扩散。

    字段：

    * ``received_at``：**本系统**收到该消息的时间（``timestamptz``，库侧默认
      ``now()``）；渠道侧时间戳不可信，故不落库。
    * ``intake_job_id``：受理后创建的 ``intake_jobs`` 行；随该作业
      ``ON DELETE CASCADE`` 删除（与 ``extraction_drafts`` 同口径）。
    * ``status``：本层当前只写 ``accepted``（已建受理作业）；``rejected`` 值保留给
      「记录每一次被拒投递」的将来模式——当前策略**不**为拒绝落行，理由见
      ``app.intake.inbound`` 的模块说明（拒绝不得成为后续合法投递的幂等锚点）。
    """

    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 渠道类型与渠道消息 ID 共同构成入站去重键（§4.4 主键的等价形式）。
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 渠道侧发送者标识：查找键，非凭据（见类 docstring）。
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    intake_job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("intake_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'accepted'")
    )

    __table_args__ = (
        UniqueConstraint(
            "channel_type",
            "external_message_id",
            name="uq_inbound_messages_channel_external_message",
        ),
        CheckConstraint(
            "status IN ('accepted', 'rejected')", name="status_allowed"
        ),
        Index("ix_inbound_messages_intake_job_id", "intake_job_id"),
    )
