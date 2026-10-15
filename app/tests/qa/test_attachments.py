"""W1-14 §6 attachment negatives (real filesystem, ``tmp_path`` only).

The attachment gates live in ``app.documents.storage`` and are exercised against a
real temporary directory -- never the repository tree.  Covered:

* path traversal in four shapes (``../``, absolute, URL-encoded, ``..`` mid-path)
  plus the empty component;
* symlink escape on both the read and the write path;
* declared-extension / real-magic mismatch (type spoofing);
* size ceiling (nothing is written when the limit is exceeded);
* sha256 mismatch on read-back;
* identical-content dedup (one physical blob).

The module never writes under the repository working directory: every test uses
``tmp_path``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.documents.storage import (
    DEFAULT_MAX_BYTES,
    FileTooLargeError,
    FileTypeMismatchError,
    HashMismatchError,
    PathTraversalError,
    read_file,
    resolve_within,
    sniff_mime,
    store_file,
    verify_file,
)

# Real magic-number payloads.
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 16
TEXT_BYTES = "报销事由：差旅费\n".encode("utf-8")


def _blob_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


# --- path traversal -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../x",
        "/etc/passwd",
        "a/../../b",
        "%2e%2e%2f",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "..",
        "",
        "sub/../../escape",
    ],
)
def test_resolve_within_rejects_traversal(tmp_path: Path, bad: str) -> None:
    """Traversal / absolute / encoded / empty components all raise PathTraversalError."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathTraversalError):
        resolve_within(root, bad)


def test_read_file_rejects_traversal_storage_key(tmp_path: Path) -> None:
    """A traversal ``storage_key`` is refused on the read path too."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(PathTraversalError):
        read_file(root, "../secret.txt")
    with pytest.raises(PathTraversalError):
        read_file(root, "/etc/passwd")
    with pytest.raises(PathTraversalError):
        read_file(root, "..%2f..%2fetc%2fpasswd")


def test_symlink_escape_is_rejected_on_read_and_write(tmp_path: Path) -> None:
    """A symlink inside the root pointing outside is refused for read and write."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathTraversalError):
        resolve_within(root, "link", "secret.txt")
    with pytest.raises(PathTraversalError):
        read_file(root, "link/secret.txt")

    # Control: a genuine blob under the root still works.
    stored = store_file(root, owner_id=1, content=TEXT_BYTES, declared_name="n.txt")
    assert read_file(root, stored.storage_key) == TEXT_BYTES


def test_safe_relative_components_resolve_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    resolved = resolve_within(root, "ab", "cd", "deadbeef")
    assert str(resolved).startswith(str(root.resolve()))
    assert resolved.name == "deadbeef"


# --- type spoofing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "declared"),
    [
        (PDF_BYTES, "x.png"),
        (PDF_BYTES, "anything.dat"),
        (PNG_BYTES, "x.pdf"),
        (TEXT_BYTES, "spoof.pdf"),
        (ZIP_BYTES, "archive.png"),
    ],
)
def test_extension_magic_mismatch_is_rejected(
    tmp_path: Path, content: bytes, declared: str
) -> None:
    """A declared extension contradicting the real magic number is refused."""
    root = tmp_path / "store"
    with pytest.raises(FileTypeMismatchError):
        store_file(root, owner_id=1, content=content, declared_name=declared)
    # Nothing is written when the type check fails.
    assert _blob_files(root) == []


def test_sniff_mime_uses_magic_not_extension(tmp_path: Path) -> None:
    assert sniff_mime(PDF_BYTES, None) == "application/pdf"
    assert sniff_mime(PNG_BYTES, "x.png") == "image/png"
    assert sniff_mime(JPEG_BYTES, "p.jpg") == "image/jpeg"
    assert sniff_mime(ZIP_BYTES, "a.zip") == "application/zip"
    assert sniff_mime(ZIP_BYTES, "d.docx").endswith("wordprocessingml.document")
    assert sniff_mime(ZIP_BYTES, "s.xlsx").endswith("spreadsheetml.sheet")
    assert sniff_mime(TEXT_BYTES, "n.txt") == "text/plain"


# --- size ceiling -------------------------------------------------------------


def test_over_limit_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    """Content above ``max_bytes`` raises FileTooLargeError and touches no disk."""
    root = tmp_path / "store"
    with pytest.raises(FileTooLargeError):
        store_file(
            root,
            owner_id=1,
            content=b"A" * 100,
            declared_name="big.txt",
            max_bytes=10,
        )
    assert _blob_files(root) == []
    assert not root.exists()


def test_default_max_bytes_is_positive() -> None:
    assert DEFAULT_MAX_BYTES > 0


# --- hash verification --------------------------------------------------------


def test_verify_file_detects_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "store"
    stored = store_file(root, owner_id=1, content=PNG_BYTES, declared_name="a.png")
    verify_file(root, stored.storage_key, stored.sha256)  # correct hash: no error
    with pytest.raises(HashMismatchError):
        verify_file(root, stored.storage_key, "0" * 64)


# --- dedup --------------------------------------------------------------------


def test_identical_content_shares_one_blob(tmp_path: Path) -> None:
    """Same content uploaded twice dedups to one physical blob, hashes match."""
    root = tmp_path / "store"
    first = store_file(root, owner_id=1, content=PDF_BYTES, declared_name="a.pdf")
    second = store_file(root, owner_id=2, content=PDF_BYTES, declared_name="b.pdf")

    expected_sha = hashlib.sha256(PDF_BYTES).hexdigest()
    assert first.sha256 == second.sha256 == expected_sha
    assert first.storage_key == second.storage_key
    assert first.deduped is False
    assert second.deduped is True
    assert len(_blob_files(root)) == 1


def test_store_file_is_content_addressed(tmp_path: Path) -> None:
    """The storage key is derived from the sha256 (content addressing)."""
    root = tmp_path / "store"
    stored = store_file(root, owner_id=1, content=JPEG_BYTES, declared_name="p.jpg")
    sha = hashlib.sha256(JPEG_BYTES).hexdigest()
    assert stored.storage_key == f"{sha[:2]}/{sha[2:4]}/{sha}"
    assert (root / sha[:2] / sha[2:4] / sha).is_file()
