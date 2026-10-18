"""Issue #59：存储层 OFD 容器识别与白名单（纯标准库 + ``tmp_path``）。

背景（#57 真实跑批确证）：OFD 是**电子发票标准容器**（GB/T 33190），本质是 ZIP，
根目录带 ``OFD.xml``。此前 ``sniff_mime`` 只认 docx/xlsx/zip 三类 ZIP 声明，声明
``.ofd`` 一律 ``FileTypeMismatchError``，而 OCR 适配器（``baidu_invoice_ocr``）
已声明支持 ``application/ofd`` —— 解析器能力在产品链路上不可达。

本文件覆盖的验收项：

1. 合法 OFD（测试内构造最小 OFD 容器：zip + 根级 ``OFD.xml``）→ ``application/ofd``，
   ``store_file`` 可入库且返回该 MIME；
2. fail closed 不放松：伪 OFD（zip 无 ``OFD.xml``）、非 ZIP 内容声明 ``.ofd``、
   仅**嵌套** ``OFD.xml``（非根级）→ 全部拒绝，且不落盘；
3. plain zip 声明 ``.zip`` 行为不变（``application/zip``）；docx/xlsx 行为不变；
4. 解析器侧既有 ``application/ofd`` 声明保持兼容（MIME 字符串一致）。

所有测试一律使用 ``tmp_path`` 作为存储根，**绝不向仓库目录写入任何文件**，
也不发起任何网络调用。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from app.documents.storage import (
    DOCX_MIME,
    OFD_MIME,
    XLSX_MIME,
    ZIP_MIME,
    FileTypeMismatchError,
    read_file,
    sniff_mime,
    store_file,
    verify_file,
)

# --- 测试夹具字节 -------------------------------------------------------------

OFD_XML = b'<?xml version="1.0" encoding="UTF-8"?>\n<ofd:OFD xmlns:ofd="x"/>\n'


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    """Build a real (in-memory) ZIP container from ``{name: bytes}``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


#: Minimal but structurally valid OFD: root-level ``OFD.xml`` + a document body.
OFD_BYTES = _zip_bytes(
    {
        "OFD.xml": OFD_XML,
        "Doc_0/Document.xml": b"<Document/>",
        "Doc_0/Pages/Page_0/Content.xml": b"<Content/>",
    }
)

#: A ZIP that is *not* an OFD (no root-level OFD.xml).
PLAIN_ZIP_BYTES = _zip_bytes({"readme.txt": b"hello"})

#: docx / xlsx shaped containers (ZIP + Office parts, no OFD marker).
DOCX_BYTES = _zip_bytes(
    {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"}
)
XLSX_BYTES = _zip_bytes(
    {"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>"}
)

#: ZIP whose OFD.xml sits in a subdirectory only — not the root marker.
NESTED_MARKER_BYTES = _zip_bytes({"Doc_0/OFD.xml": OFD_XML})

#: Malformed ZIP bytes (magic only) and non-ZIP payloads.
BROKEN_ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 16
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
TEXT_BYTES = "报销事由：差旅费\n".encode("utf-8")


def _blob_files(root: Path) -> list[Path]:
    """Every regular file currently under ``root`` (empty if it does not exist)."""
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


# --- 1. 合法 OFD 被识别并接受 --------------------------------------------------


def test_sniff_mime_recognizes_ofd_container() -> None:
    """zip + 根级 OFD.xml → application/ofd（声明 .ofd 或不声明均可）。"""
    assert OFD_MIME == "application/ofd"
    assert sniff_mime(OFD_BYTES, "invoice.ofd") == OFD_MIME
    assert sniff_mime(OFD_BYTES, "INVOICE.OFD") == OFD_MIME
    assert sniff_mime(OFD_BYTES, None) == OFD_MIME
    # OFD genuinely is a ZIP container, so a .zip declaration is not a spoof.
    assert sniff_mime(OFD_BYTES, "invoice.zip") == OFD_MIME


def test_store_file_accepts_ofd_and_roundtrips(tmp_path: Path) -> None:
    """store_file 接受 .ofd：MIME 正确、blob 落盘、可读回并校验摘要。"""
    root = tmp_path / "store"
    result = store_file(root, owner_id=1, content=OFD_BYTES, declared_name="a.ofd")

    expected_sha = hashlib.sha256(OFD_BYTES).hexdigest()
    assert result.mime_type == OFD_MIME
    assert result.sha256 == expected_sha
    assert result.size_bytes == len(OFD_BYTES)
    assert result.deduped is False
    assert result.storage_key == f"{expected_sha[:2]}/{expected_sha[2:4]}/{expected_sha}"

    blob = root / expected_sha[:2] / expected_sha[2:4] / expected_sha
    assert blob.is_file()
    assert read_file(root, result.storage_key) == OFD_BYTES
    verify_file(root, result.storage_key, result.sha256)
    assert len(_blob_files(root)) == 1

    # Identical content from another owner dedupes onto the same blob.
    again = store_file(root, owner_id=2, content=OFD_BYTES, declared_name="b.ofd")
    assert again.deduped is True
    assert again.mime_type == OFD_MIME
    assert len(_blob_files(root)) == 1


def test_sniff_mime_ofd_marker_with_dot_slash_prefix() -> None:
    """生产者可能写出 ``./OFD.xml``：归一化后仍按根级标记识别。"""
    dotted = _zip_bytes({"./OFD.xml": OFD_XML})
    assert sniff_mime(dotted, "invoice.ofd") == OFD_MIME


# --- 2. fail closed：伪 OFD 一律拒绝 -------------------------------------------


@pytest.mark.parametrize(
    "content",
    [PLAIN_ZIP_BYTES, DOCX_BYTES, XLSX_BYTES, NESTED_MARKER_BYTES],
    ids=["plain-zip", "docx-shaped", "xlsx-shaped", "nested-marker-only"],
)
def test_fake_ofd_declaration_is_rejected(content: bytes) -> None:
    """ZIP 内容声明 .ofd 但无根级 OFD.xml → FileTypeMismatchError。"""
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(content, "invoice.ofd")


@pytest.mark.parametrize(
    "content",
    [BROKEN_ZIP_BYTES, PDF_BYTES, PNG_BYTES, JPEG_BYTES, TEXT_BYTES],
    ids=["broken-zip", "pdf", "png", "jpeg", "text"],
)
def test_non_ofd_content_declared_ofd_is_rejected(content: bytes) -> None:
    """非 OFD 内容（含坏 ZIP、非 ZIP 载体）声明 .ofd → 拒绝。"""
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(content, "invoice.ofd")


def test_store_file_rejects_fake_ofd_and_writes_nothing(tmp_path: Path) -> None:
    """伪 OFD 走 store_file 同样被拒，且磁盘上不留下任何文件。"""
    root = tmp_path / "store"
    with pytest.raises(FileTypeMismatchError):
        store_file(
            root, owner_id=1, content=PLAIN_ZIP_BYTES, declared_name="fake.ofd"
        )
    assert _blob_files(root) == []
    assert not root.exists()


def test_ofd_container_declared_office_ext_is_rejected() -> None:
    """OFD 内容声明 .docx/.xlsx → 拒绝（声明与结构不符）。"""
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(OFD_BYTES, "invoice.docx")
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(OFD_BYTES, "invoice.xlsx")


# --- 3. 既有行为不回归 ---------------------------------------------------------


def test_existing_zip_family_behaviour_unchanged() -> None:
    """plain zip / docx / xlsx / 坏 ZIP 的既有判定不变。"""
    assert sniff_mime(PLAIN_ZIP_BYTES, "archive.zip") == ZIP_MIME
    assert sniff_mime(DOCX_BYTES, "doc.docx") == DOCX_MIME
    assert sniff_mime(XLSX_BYTES, "sheet.xlsx") == XLSX_MIME
    # Malformed ZIP bytes keep the previous (unrefined) ZIP classification.
    assert sniff_mime(BROKEN_ZIP_BYTES, "archive.zip") == ZIP_MIME
    # .ofd is a binary-container declaration: it no longer slips through on a
    # text payload, but the pre-existing rejections still hold.
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(PDF_BYTES, "spoof.png")
    with pytest.raises(FileTypeMismatchError):
        sniff_mime(TEXT_BYTES, "spoof.pdf")


def test_existing_non_zip_behaviour_unchanged() -> None:
    """PDF/PNG/JPEG/文本的既有判定不变。"""
    assert sniff_mime(PDF_BYTES, None) == "application/pdf"
    assert sniff_mime(PDF_BYTES, "report.pdf") == "application/pdf"
    assert sniff_mime(PNG_BYTES, "x.png") == "image/png"
    assert sniff_mime(JPEG_BYTES, "photo.jpg") == "image/jpeg"
    assert sniff_mime(TEXT_BYTES, "notes.txt") == "text/plain"


# --- 4. 解析器侧兼容 -----------------------------------------------------------


def test_ocr_adapter_still_declares_same_ofd_mime() -> None:
    """OCR 适配器声明的 OFD MIME 与存储层常量一致（链路可达）。"""
    from app.integrations.baidu_invoice_ocr import _MIME_SPECS

    assert _MIME_SPECS[OFD_MIME] == ("ofd_file", "ofd")
