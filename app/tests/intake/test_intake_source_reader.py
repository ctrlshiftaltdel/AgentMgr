"""生产级 ``SourceReader``（``SourceFileReader``）的纯逻辑测试（**无数据库、无网络**）。

覆盖 Issue #45 验收第 1 条：

* ``source_file_id`` → ``source_files`` 行 → ``app.documents.storage.read_file`` →
  sha256 校验 → ``SourceDocument``（``content`` / ``mime_type`` / ``size_bytes`` /
  ``scan_status``）；
* 失败一律 fail closed 且分类明确：``missing_file`` / ``hash_mismatch`` /
  ``storage_error``；错误消息**不含存储路径与文件内容**；
* 只读：读取不改动已入库 blob。

会话用鸭子类型的桩（只实现 ``get(model, pk)``），存储用真实的内容寻址布局
（``store_file`` 写入 ``tmp_path``），因此本文件无需 PostgreSQL 即可运行。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from app.documents.models import SourceFile
from app.documents.storage import store_file
from app.intake.source_reader import (
    HASH_MISMATCH,
    MISSING_FILE,
    STORAGE_ERROR,
    SourceFileNotFound,
    SourceFileReader,
    SourceHashMismatch,
    SourceReadError,
    SourceStorageFailure,
)

#: 一段可辨识的「附件原文」，用于断言错误消息不泄漏内容。
RAW_ATTACHMENT_BODY = b"secret-attachment-body-0123456789"


def _png(width: int = 100, height: int = 100) -> bytes:
    """构造仅含签名 + IHDR 的 PNG（足够魔数嗅探，不是可解码图片）。"""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = (
        struct.pack(">I", len(ihdr_data))
        + b"IHDR"
        + ihdr_data
        + b"\x00\x00\x00\x00"
    )
    return signature + ihdr


class StubRow:
    """``SourceFile`` 行的最小替身（只暴露本模块用到的列）。"""

    def __init__(
        self,
        *,
        storage_key: str,
        sha256: str,
        mime_type: str = "image/png",
        size_bytes: int = 0,
        scan_status: str = "clean",
    ) -> None:
        self.storage_key = storage_key
        self.sha256 = sha256
        self.mime_type = mime_type
        self.size_bytes = size_bytes
        self.scan_status = scan_status


class StubSession:
    """只实现 ``Session.get(model, pk)`` 的桩（记录被查询的主键）。"""

    def __init__(self, row: object | None) -> None:
        self.row = row
        self.requested: list[Any] = []

    def get(self, model: object, primary_key: object) -> object | None:
        self.requested.append((model, primary_key))
        return self.row


def _reader(tmp_path: Path, row: object | None) -> tuple[SourceFileReader, StubSession]:
    session = StubSession(row)
    reader = SourceFileReader(session=session, root=tmp_path)  # type: ignore[arg-type]
    return reader, session


def _stored(tmp_path: Path, content: bytes, *, declared_name: str) -> Any:
    return store_file(tmp_path, owner_id=1, content=content, declared_name=declared_name)


def _row_for(stored: Any, **overrides: Any) -> StubRow:
    fields: dict[str, Any] = {
        "storage_key": stored.storage_key,
        "sha256": stored.sha256,
        "mime_type": stored.mime_type,
        "size_bytes": stored.size_bytes,
    }
    fields.update(overrides)
    return StubRow(**fields)


# --- 成功路径 -----------------------------------------------------------------


def test_reads_verified_content_with_protocol_fields(tmp_path: Path) -> None:
    """返回 ``SourceDocument``：内容为存储字节，MIME/扫描状态原样带出。"""
    content = _png()
    stored = _stored(tmp_path, content, declared_name="receipt.png")
    reader, session = _reader(
        tmp_path, _row_for(stored, scan_status="clean")
    )

    document = reader.read(7)

    assert document.content == content
    assert document.mime_type == "image/png"
    assert document.size_bytes == len(content)
    assert document.scan_status == "clean"
    assert session.requested == [(SourceFile, 7)]


def test_scan_status_is_carried_through_unchanged(tmp_path: Path) -> None:
    """非 ``clean`` 的扫描状态原样返回（是否送第三方由解析器预检决定）。"""
    stored = _stored(tmp_path, _png(), declared_name="receipt.png")
    reader, _ = _reader(tmp_path, _row_for(stored, scan_status="pending"))

    assert reader.read(1).scan_status == "pending"


def test_read_is_read_only(tmp_path: Path) -> None:
    """读取不改动存储：文件集合与字节完全不变。"""
    content = _png()
    stored = _stored(tmp_path, content, declared_name="receipt.png")
    reader, _ = _reader(tmp_path, _row_for(stored))

    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    reader.read(1)
    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    assert after == before


def test_default_root_is_used_when_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未注入 ``root`` 时读 ``default_storage_root()``（环境变量配置）。"""
    content = _png()
    stored = _stored(tmp_path, content, declared_name="receipt.png")
    monkeypatch.setenv("AJAIMGR_STORAGE_ROOT", str(tmp_path))
    reader = SourceFileReader(session=StubSession(_row_for(stored)))  # type: ignore[arg-type]

    assert reader.root == tmp_path
    document = reader.read(1)
    assert document.content == content
    assert document.size_bytes == len(content)


# --- 失败路径（fail closed + 分类） -------------------------------------------


def test_missing_row_is_missing_file(tmp_path: Path) -> None:
    """行不存在 → ``missing_file``；消息不含存储路径。"""
    reader, _ = _reader(tmp_path, None)

    with pytest.raises(SourceFileNotFound) as excinfo:
        reader.read(42)

    assert excinfo.value.code == MISSING_FILE
    assert str(tmp_path) not in str(excinfo.value)


def test_missing_blob_is_missing_file(tmp_path: Path) -> None:
    """行在但 blob 已被删除 → ``missing_file``（不静默返回空内容）。"""
    stored = _stored(tmp_path, _png(), declared_name="receipt.png")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.unlink()
    reader, _ = _reader(tmp_path, _row_for(stored))

    with pytest.raises(SourceFileNotFound) as excinfo:
        reader.read(1)

    assert excinfo.value.code == MISSING_FILE
    assert stored.storage_key not in str(excinfo.value)
    assert str(tmp_path) not in str(excinfo.value)


def test_hash_mismatch_is_hash_mismatch(tmp_path: Path) -> None:
    """内容与行内 sha256 不符 → ``hash_mismatch``；消息不含内容与路径。"""
    content = b"%PDF-1.4 " + RAW_ATTACHMENT_BODY
    stored = _stored(tmp_path, content, declared_name="invoice.pdf")
    reader, _ = _reader(tmp_path, _row_for(stored, sha256="0" * 64))

    with pytest.raises(SourceHashMismatch) as excinfo:
        reader.read(1)

    message = str(excinfo.value)
    assert excinfo.value.code == HASH_MISMATCH
    assert RAW_ATTACHMENT_BODY.decode() not in message
    assert stored.storage_key not in message
    assert str(tmp_path) not in message


def test_unsafe_storage_key_is_storage_error(tmp_path: Path) -> None:
    """路径穿越键被拒 → ``storage_error``；消息不含该键与根路径。"""
    row = StubRow(storage_key="../../etc/passwd", sha256="a" * 64)
    reader, _ = _reader(tmp_path, row)

    with pytest.raises(SourceStorageFailure) as excinfo:
        reader.read(1)

    message = str(excinfo.value)
    assert excinfo.value.code == STORAGE_ERROR
    assert "passwd" not in message
    assert "etc" not in message
    assert str(tmp_path) not in message


def test_unreadable_root_is_storage_error(tmp_path: Path) -> None:
    """存储根不可读（此处是普通文件）→ ``storage_error``，不泄漏底层异常原文。"""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    reader, _ = _reader(blocker, StubRow(storage_key="ab/cd/ef", sha256="b" * 64))

    with pytest.raises(SourceStorageFailure) as excinfo:
        reader.read(1)

    assert excinfo.value.code == STORAGE_ERROR
    assert str(blocker) not in str(excinfo.value)


@pytest.mark.parametrize("bad_reference", [None, True, "1", 1.5])
def test_invalid_reference_is_missing_file(tmp_path: Path, bad_reference: object) -> None:
    """非法主键（``None``/布尔/字符串/浮点）按 ``missing_file`` 处理，不抛原生异常。"""
    reader, _ = _reader(tmp_path, _row_for(_stored(tmp_path, _png(), declared_name="r.png")))

    with pytest.raises(SourceReadError) as excinfo:
        reader.read(bad_reference)  # type: ignore[arg-type]

    assert excinfo.value.code == MISSING_FILE


def test_error_codes_are_distinct_stable_strings() -> None:
    """分类码是稳定的机器可读字符串（供服务层分派）。"""
    assert (MISSING_FILE, HASH_MISMATCH, STORAGE_ERROR) == (
        "missing_file",
        "hash_mismatch",
        "storage_error",
    )
    assert SourceFileNotFound.code == MISSING_FILE
    assert SourceHashMismatch.code == HASH_MISMATCH
    assert SourceStorageFailure.code == STORAGE_ERROR
