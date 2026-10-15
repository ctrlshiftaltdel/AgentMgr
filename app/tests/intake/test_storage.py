"""W1-9 文件存储服务测试（纯标准库 + ``tmp_path``，不触碰数据库/仓库目录）。

覆盖任务第五步第 1–5 项，对齐 ``templates/PLAN.md`` §8 与
``docs/research/W0-5-security-compliance.md`` §4.3（恶意文件上传）与 §5.5
（路径拼接禁止规则）：

1. ``store_file`` 写入并返回 sha256；同一内容第二次入库 ``deduped=True`` 且磁盘
   上只有一个 blob；
2. 超限内容（``max_bytes`` 设小值）抛 ``FileTooLargeError``，且**未在磁盘上留下
   任何文件**；
3. 类型伪装：声明名 ``x.png`` 但内容是 PDF 字节 → ``FileTypeMismatchError``；
   真实 PNG 内容 + ``.png`` 名 → 通过；
4. 路径穿越：``resolve_within`` 对 ``../x``、``/etc/passwd``、``a/../../b``、URL
   编码 ``%2e%2e%2f`` 全部抛 ``PathTraversalError``；root 内指向 root 外的符号
   链接被拒（读与写路径均被拒）；
5. ``verify_file`` 摘要不符抛 ``HashMismatchError``。

所有测试一律使用 ``tmp_path`` 作为存储根，**绝不向仓库目录写入任何文件**。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.documents.storage import (
    FileTooLargeError,
    FileTypeMismatchError,
    HashMismatchError,
    PathTraversalError,
    default_storage_root,
    read_file,
    resolve_within,
    sniff_mime,
    store_file,
    verify_file,
)

# Minimal but real magic-number payloads.
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 16
TEXT_BYTES = "报销事由：差旅费\n".encode("utf-8")


def _blob_files(root: Path) -> list[Path]:
    """Every regular file currently under ``root`` (empty if it does not exist)."""
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


# --- 1. 写入 / sha256 / 去重 ---------------------------------------------------


def test_store_file_writes_and_returns_sha256(tmp_path: Path) -> None:
    """store_file 写入并返回正确的 sha256，blob 落盘。"""
    root = tmp_path / "store"
    result = store_file(root, owner_id=1, content=PNG_BYTES, declared_name="a.png")

    expected_sha = hashlib.sha256(PNG_BYTES).hexdigest()
    assert result.sha256 == expected_sha
    assert result.size_bytes == len(PNG_BYTES)
    assert result.mime_type == "image/png"
    assert result.deduped is False
    assert result.storage_key == (
        f"{expected_sha[:2]}/{expected_sha[2:4]}/{expected_sha}"
    )

    # The blob exists exactly at the content-addressed path.
    blob = root / expected_sha[:2] / expected_sha[2:4] / expected_sha
    assert blob.is_file()
    assert blob.read_bytes() == PNG_BYTES
    assert len(_blob_files(root)) == 1


def test_store_file_dedupes_identical_content(tmp_path: Path) -> None:
    """同一内容第二次入库 deduped=True，磁盘上仍只有一个 blob。"""
    root = tmp_path / "store"

    first = store_file(root, owner_id=1, content=PDF_BYTES, declared_name="a.pdf")
    assert first.deduped is False
    assert len(_blob_files(root)) == 1

    # A different owner uploading identical content shares the same blob.
    second = store_file(root, owner_id=2, content=PDF_BYTES, declared_name="b.pdf")
    assert second.deduped is True
    assert second.sha256 == first.sha256
    assert second.storage_key == first.storage_key
    assert len(_blob_files(root)) == 1  # still exactly one physical blob


# --- 2. 超限不落盘 -------------------------------------------------------------


def test_store_file_over_limit_raises_and_writes_nothing(tmp_path: Path) -> None:
    """超限内容抛 FileTooLargeError，且磁盘上不留下任何文件。"""
    root = tmp_path / "store"
    content = b"A" * 100

    with pytest.raises(FileTooLargeError):
        store_file(
            root, owner_id=1, content=content, declared_name="big.txt", max_bytes=10
        )

    # Size is checked before any directory/file is created.
    assert _blob_files(root) == []
    assert not root.exists()


# --- 3. 类型伪装 ---------------------------------------------------------------


def test_store_file_rejects_extension_spoofing(tmp_path: Path) -> None:
    """声明 x.png 但内容是 PDF 字节 → FileTypeMismatchError。"""
    root = tmp_path / "store"
    with pytest.raises(FileTypeMismatchError):
        store_file(root, owner_id=1, content=PDF_BYTES, declared_name="x.png")
    # Nothing written on rejection.
    assert _blob_files(root) == []


def test_store_file_accepts_matching_png(tmp_path: Path) -> None:
    """真实 PNG 内容 + .png 名 → 通过。"""
    root = tmp_path / "store"
    result = store_file(root, owner_id=1, content=PNG_BYTES, declared_name="x.png")
    assert result.mime_type == "image/png"
    assert result.deduped is False


def test_sniff_mime_uses_magic_not_extension() -> None:
    """嗅探依据真实魔数：docx/xlsx 均属 ZIP，按声明扩展名区分。"""
    assert sniff_mime(PDF_BYTES, None) == "application/pdf"
    assert sniff_mime(PDF_BYTES, "report.pdf") == "application/pdf"
    assert sniff_mime(PNG_BYTES, None) == "image/png"
    assert sniff_mime(JPEG_BYTES, "photo.jpg") == "image/jpeg"
    assert sniff_mime(ZIP_BYTES, "doc.docx") == (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    )
    assert sniff_mime(ZIP_BYTES, "sheet.xlsx") == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert sniff_mime(ZIP_BYTES, "archive.zip") == "application/zip"
    assert sniff_mime(TEXT_BYTES, "notes.txt") == "text/plain"

    # Extension that contradicts the magic is rejected (fail closed).
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(PDF_BYTES, "spoof.png")
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(PDF_BYTES, "anything.dat")  # non-matching binary ext
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(TEXT_BYTES, "spoof.pdf")


# --- 4. 路径穿越 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../x", "/etc/passwd", "a/../../b", "%2e%2e%2f", ""],
)
def test_resolve_within_rejects_traversal(tmp_path: Path, bad: str) -> None:
    """四类穿越输入（及空串）全部抛 PathTraversalError。"""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathTraversalError):
        resolve_within(root, bad)


def test_resolve_within_accepts_safe_relative(tmp_path: Path) -> None:
    """普通相对部件被接受并规范化为 root 内的真实路径。"""
    root = tmp_path / "root"
    root.mkdir()
    resolved = resolve_within(root, "ab", "cd", "deadbeef")
    assert str(resolved).startswith(str(root.resolve()))
    assert resolved.name == "deadbeef"


def test_symlink_escape_is_rejected_for_read_and_write(tmp_path: Path) -> None:
    """root 内指向 root 外的符号链接：读路径与写路径均被拒。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)

    # Joining through the symlink escapes the root → rejected.
    with pytest.raises(PathTraversalError):
        resolve_within(root, "link", "secret.txt")
    with pytest.raises(PathTraversalError):
        read_file(root, "link/secret.txt")

    # A direct blob path under root still works (control case).
    stored = store_file(root, owner_id=1, content=TEXT_BYTES, declared_name="n.txt")
    assert read_file(root, stored.storage_key) == TEXT_BYTES


# --- 5. 散列校验 ---------------------------------------------------------------


def test_verify_file_detects_hash_mismatch(tmp_path: Path) -> None:
    """verify_file 摘要不符抛 HashMismatchError；相符不抛。"""
    root = tmp_path / "store"
    stored = store_file(root, owner_id=1, content=PNG_BYTES, declared_name="a.png")

    # Correct hash: no error.
    verify_file(root, stored.storage_key, stored.sha256)

    # Wrong hash: HashMismatchError.
    with pytest.raises(HashMismatchError):
        verify_file(root, stored.storage_key, "0" * 64)


def test_read_file_roundtrip_and_missing(tmp_path: Path) -> None:
    """read_file 正确读回；不存在的 key 抛 FileNotFoundError。"""
    root = tmp_path / "store"
    stored = store_file(root, owner_id=1, content=JPEG_BYTES, declared_name="p.jpg")
    assert read_file(root, stored.storage_key) == JPEG_BYTES

    missing = "aa/bb/" + "f" * 64
    with pytest.raises(FileNotFoundError):
        read_file(root, missing)


def test_default_storage_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """存储根从 AJAIMGR_STORAGE_ROOT 读取，默认 var/storage。"""
    monkeypatch.delenv("AJAIMGR_STORAGE_ROOT", raising=False)
    assert default_storage_root() == Path("var/storage")

    monkeypatch.setenv("AJAIMGR_STORAGE_ROOT", str(tmp_path / "custom"))
    assert default_storage_root() == tmp_path / "custom"
