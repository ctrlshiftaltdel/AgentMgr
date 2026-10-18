"""生产级 ``SourceReader``：按 ``source_file_id`` 读取**只读**原件（Issue #45）。

``app.integrations.baidu_invoice_ocr`` 定义了 ``SourceReader`` 协议与
``SourceDocument`` 载体（解析器需要原件字节，而 ``ParseRequest`` 只携带
``source_file_id``），但产品代码中一直没有实现。本模块补齐这一缺口，使
「已入库源文件 → 解析器」在产品代码里真正可达。

读取顺序（每一步都可失败，**全部 fail closed**）：

1. 用调用方注入的 ``Session`` 按主键取 ``source_files`` 行（``SourceFile``）；
2. 用 ``app.documents.storage.read_file`` 按 ``storage_key`` 读取内容寻址 blob
   （该函数自带路径穿越防护：空部件 / 绝对路径 / ``..`` / URL 编码穿越 / 符号
   链接逃逸一律拒绝）；
3. 对读到的字节重算 sha256，与行内 ``sha256`` 比对。

错误分类（``SourceReadError.code``，机器可读、稳定）：

| ``code`` | 触发条件 | 异常类型 |
|---|---|---|
| ``missing_file`` | 行不存在、主键非法、blob 不存在 | ``SourceFileNotFound`` |
| ``hash_mismatch`` | 重算 sha256 与行内值不符 | ``SourceHashMismatch`` |
| ``storage_error`` | 存储键不安全或底层读取失败（含 ``OSError``） | ``SourceStorageFailure`` |

硬边界：

* **只读**：本模块不写任何文件、不改任何数据库行、**绝不 commit**——事务边界由
  调用方（服务层）控制；解析器拿到的只是字节副本，原始文件不会被解析结果覆盖；
* **错误消息不含路径与内容**：分类码与固定文案之外不携带 ``storage_key``、存储根
  路径、文件名或文件内容；底层异常的上下文被**刻意抑制**（``from None``），避免
  异常链把存储路径或内容带进日志与审计；
* **不引入依赖**：只用标准库与项目既有模块，不做任何网络调用；
* 读取失败时**抛异常**而不是返回空/截断内容：``ExternalParser`` 会把异常收敛为
  ``error='provider_error'`` 的失败结果，上层据此回退人工录入（「无供应商也可
  工作」）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from app.documents.models import SourceFile
from app.documents.storage import (
    PathTraversalError,
    StorageError,
    default_storage_root,
    read_file,
)
from app.integrations.baidu_invoice_ocr import SourceDocument

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "HASH_MISMATCH",
    "MISSING_FILE",
    "STORAGE_ERROR",
    "SourceFileNotFound",
    "SourceFileReader",
    "SourceHashMismatch",
    "SourceReadError",
    "SourceStorageFailure",
]

#: ``SourceReadError.code`` 的稳定取值（机器可读分类）。
MISSING_FILE = "missing_file"
HASH_MISMATCH = "hash_mismatch"
STORAGE_ERROR = "storage_error"


# --- 异常 ---------------------------------------------------------------------


class SourceReadError(Exception):
    """读取源文件失败（fail closed）。

    ``code`` 是机器可读分类（见模块说明表）；``str(exc)`` 是**不含路径与内容**的
    固定文案。子类覆盖 ``code`` 与默认文案，便于调用方按类型或按码分派。
    """

    code: str = STORAGE_ERROR
    default_message: str = "source file read failed"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(self.default_message if message is None else message)


class SourceFileNotFound(SourceReadError):
    """``source_files`` 行不存在、主键非法，或 blob 已不在存储中。"""

    code = MISSING_FILE
    default_message = "source file not found"


class SourceHashMismatch(SourceReadError):
    """读到的字节与行内 ``sha256`` 不符（内容被替换/损坏，fail closed）。"""

    code = HASH_MISMATCH
    default_message = "source file content hash mismatch"


class SourceStorageFailure(SourceReadError):
    """存储键不安全或底层读取失败（路径穿越、权限、I/O 错误等）。"""

    code = STORAGE_ERROR
    default_message = "source file storage read failed"


# --- 实现 ---------------------------------------------------------------------


class SourceFileReader:
    """``SourceReader`` 的生产实现：``source_file_id`` → 已校验的 ``SourceDocument``。

    * ``session``：调用方的事务上下文（本类只读，不 commit）；解析器通常在同一个
      受理事务里被调用，故直接复用该 session，保证「读到的行」与「写入的草稿」
      处于同一快照语义下；
    * ``root``：存储根；省略时用 ``app.documents.storage.default_storage_root()``
      （环境变量 ``AJAIMGR_STORAGE_ROOT``，默认 ``var/storage``）。

    ``read`` 返回的 ``SourceDocument.size_bytes`` 取**实际读到的字节数**（内容已
    经 sha256 校验，长度由内容唯一决定），``scan_status`` 原样带出——是否允许送第
    三方识别由解析器的预检决定（非 ``clean`` 一律拒绝）。
    """

    def __init__(self, *, session: Session, root: Path | str | None = None) -> None:
        self._session = session
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        """存储根（显式注入优先，否则读环境变量配置）。"""
        return self._root if self._root is not None else default_storage_root()

    def read(self, source_file_id: int) -> SourceDocument:
        """读取并校验一份原件；失败抛 ``SourceReadError`` 子类（fail closed）。"""
        row = self._lookup(source_file_id)
        content = self._read_blob(row.storage_key)

        actual = hashlib.sha256(content).hexdigest()
        if actual != row.sha256:
            # 不携带 storage_key / 期望值 / 实际值（可能被用于探测存储内容）。
            raise SourceHashMismatch()

        return SourceDocument(
            content=content,
            mime_type=row.mime_type,
            size_bytes=len(content),
            scan_status=row.scan_status,
        )

    def _lookup(self, source_file_id: int) -> SourceFile:
        """取 ``source_files`` 行；非法主键与缺行同样按 ``missing_file`` 处理。"""
        if isinstance(source_file_id, bool) or not isinstance(source_file_id, int):
            raise SourceFileNotFound("invalid source file reference")
        row = self._session.get(SourceFile, source_file_id)
        if row is None:
            raise SourceFileNotFound()
        return row

    def _read_blob(self, storage_key: str) -> bytes:
        """按存储键读取 blob；底层异常按分类收敛，且**抑制异常链**（不带路径）。"""
        try:
            return read_file(self.root, storage_key)
        except FileNotFoundError:
            raise SourceFileNotFound("source file blob not found") from None
        except PathTraversalError:
            raise SourceStorageFailure("unsafe storage key rejected") from None
        except OSError:
            raise SourceStorageFailure("storage read failed") from None
        except StorageError:
            raise SourceStorageFailure("storage read failed") from None
