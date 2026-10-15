"""文档域核心数据模型：源文件（内容寻址存储的元数据）。

列结构由 Master 在任务 W1-9 中统一固定，并对齐：

* ``templates/PLAN.md`` §8「工作流、收件和文件」表清单——
  ``source_files(id, owner_id, storage_key, sha256, mime_type, size_bytes,
  scan_status)``；
* ``templates/PLAN.md`` §8「数据完整性要求」——
  **「文件路径不能由用户输入直接拼接；外部 API 返回内容必须视为不可信数据」**、
  **「对附件实施文件类型/大小限制、恶意文件扫描、授权下载和散列校验」**；
* ``docs/research/W0-5-security-compliance.md`` §5.4（附件存储与下载授权）与
  §5.5（**路径拼接禁止规则**）、§4.3（恶意文件上传）；
* ``docs/research/W0-1-weixin-ilink.md`` §6.3（**附件一律视为不可信输入**）。

关键不变量：

* ``storage_key`` **不设唯一约束**：存储采用内容寻址，同一内容被不同员工上传时
  物理 blob 共享，但数据库行按上传各存一份（owner 元数据不同）；去重发生在
  ``app.documents.storage`` 存储层，而不是数据库层；
* ``storage_key`` / ``sha256`` 建索引，便于散列校验与去重查询；
* ``size_bytes >= 0``；``scan_status`` 用 ``TEXT`` + ``CHECK`` 限定为
  ``pending`` / ``clean`` / ``infected`` / ``failed``（恶意文件扫描，见 W0-5
  §4.3）；未扫描/扫描失败的文件不得进入业务（本层只记录状态）。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.organization import TimestampMixin

__all__ = ["SourceFile"]


class SourceFile(TimestampMixin, Base):
    """一个已入库的源文件：内容寻址存储的元数据行（每个上传各一行）。

    ``storage_key`` 使用内容寻址格式 ``f"{sha[:2]}/{sha[2:4]}/{sha}"``（由
    ``app.documents.storage.store_file`` 生成），**不设唯一约束**——同一 sha256
    的物理 blob 由存储层去重共享，但每个上传者各留一行元数据（``owner_id`` 等
    不同）。``sha256`` 用于下载/读取时的散列校验（PLAN §8）。
    """

    __tablename__ = "source_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        CheckConstraint(
            "scan_status IN ('pending', 'clean', 'infected', 'failed')",
            name="scan_status_allowed",
        ),
        Index("ix_source_files_storage_key", "storage_key"),
        Index("ix_source_files_sha256", "sha256"),
    )
