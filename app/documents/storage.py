"""内容寻址的文件存储服务（仅用标准库，不引入任何第三方依赖）。

设计依据：

* ``templates/PLAN.md`` §8「数据完整性要求」——**「文件路径不能由用户输入直接
  拼接；外部 API 返回内容必须视为不可信数据」**、**「对附件实施文件类型/大小
  限制、恶意文件扫描、授权下载和散列校验」**；
* ``docs/research/W0-5-security-compliance.md`` §4.3（恶意文件上传：类型伪装、
  超大文件、宏、压缩炸弹）与 §5.5（**路径拼接禁止规则**，含反例与正例代码）；
* ``docs/research/W0-1-weixin-ilink.md`` §6.3（**附件一律视为不可信输入**）。

本模块提供四道闸门，全部对**不可信输入**生效：

1. **路径穿越防护**（``resolve_within``）：拒绝空串、绝对路径、含 ``..`` 的部件
   与 URL 编码的穿越序列；再做 ``realpath`` 规范化并用 ``commonpath`` 校验结果
   仍位于允许根目录之内（从而拒绝符号链接逃逸）。
2. **大小限制**（``store_file`` 的 ``max_bytes``）：**先校验大小再落盘**，超限直接
   抛 ``FileTooLargeError``，不写任何文件。
3. **真实魔数类型校验**（``sniff_mime``）：只依据**内容字节的魔数**判断类型，
   **不依赖扩展名**；声明的扩展名与真实魔数不符时抛 ``FileTypeMismatchError``。
   ZIP 系容器进一步按**容器结构**区分：含根级 ``OFD.xml`` 的是 OFD 电子发票
   容器（``application/ofd``），其余按声明的 Office 扩展名区分 docx/xlsx。
4. **散列校验**（``verify_file``）：读取后重算 sha256，不符抛 ``HashMismatchError``。

存储布局为内容寻址 ``f"{sha[:2]}/{sha[2:4]}/{sha}"``：相同内容共享同一个物理
blob（去重发生在存储层），因此不同员工上传相同内容时数据库仍各存一行元数据而
物理文件只有一份。

配置说明：存储根当前从环境变量 ``AJAIMGR_STORAGE_ROOT`` 读取（默认
``var/storage``，相对当前工作目录）。**应后续移入 ``app/core/config.py`` 统一
管理**（与 ``AJAIMGR_DB_URL`` 等一起），避免各模块各自读环境变量；本任务按约束
不得改动 ``app/core/config.py``，故暂留在此。
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_STORAGE_ROOT",
    "FileTooLargeError",
    "FileTypeMismatchError",
    "HashMismatchError",
    "PathTraversalError",
    "StorageError",
    "StoredFile",
    "default_storage_root",
    "read_file",
    "resolve_within",
    "sniff_mime",
    "store_file",
    "verify_file",
]

#: Default per-file size ceiling: 20 MiB (PLAN §8「大小限制」/ W0-5 §4.3).
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

#: Default storage root, relative to the current working directory.
DEFAULT_STORAGE_ROOT = "var/storage"

#: Environment variable that overrides the storage root.
STORAGE_ROOT_ENV = "AJAIMGR_STORAGE_ROOT"


# --- 异常 -------------------------------------------------------------------


class StorageError(Exception):
    """文件存储相关错误的基类。"""


class PathTraversalError(StorageError):
    """路径拼接被拒（穿越、绝对路径、空串或符号链接逃逸）。"""


class FileTooLargeError(StorageError):
    """内容超过 ``max_bytes`` 限制（未落盘即拒绝）。"""


class FileTypeMismatchError(StorageError):
    """声明扩展名与真实魔数不符（类型伪装）。"""


class HashMismatchError(StorageError):
    """读取内容的 sha256 与期望值不符（散列校验失败）。"""


# --- 魔数与 MIME 映射 --------------------------------------------------------

PDF_MIME = "application/pdf"
PNG_MIME = "image/png"
JPEG_MIME = "image/jpeg"
ZIP_MIME = "application/zip"
OFD_MIME = "application/ofd"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
TEXT_MIME = "text/plain"
OCTET_MIME = "application/octet-stream"

#: Real magic numbers used for content sniffing (never the extension).
PDF_SIG = b"%PDF-"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
JPEG_SIG = b"\xff\xd8\xff"
ZIP_SIG = b"PK\x03\x04"

#: Root-level entry that identifies an OFD container (GB/T 33190 / 电子发票标准).
#: OFD is a ZIP container, so the marker entry — not the extension alone — is what
#: separates a real OFD from an arbitrary ZIP (fail closed).
OFD_MARKER = "OFD.xml"

#: Extensions we treat as "binary container" declarations.  A text/unknown-binary
#: payload declared with one of these is treated as a mismatch (fail closed).
KNOWN_BINARY_EXTS = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx", ".zip", ".ofd"}
)

#: Office Open XML formats are ZIP containers, distinguished by the *declared*
#: extension (PLAN §8 / W0-5 §4.3: docx/xlsx are both ZIP).
ZIP_ALLOWED_EXTS = frozenset({".docx", ".xlsx", ".zip"})

#: Declarations accepted for a ZIP container that *structurally* is an OFD
#: (root-level ``OFD.xml``).  ``.zip`` stays accepted because OFD genuinely is a
#: ZIP container, so a ``.zip`` declaration on OFD bytes is not a spoof.
OFD_ALLOWED_EXTS = frozenset({".ofd", ".zip"})


@dataclass(frozen=True)
class StoredFile:
    """一次 ``store_file`` 的结果（去重标记 + 元数据）。"""

    storage_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    deduped: bool


# --- 存储根 ------------------------------------------------------------------


def default_storage_root() -> Path:
    """Return the configured storage root (env ``AJAIMGR_STORAGE_ROOT``).

    NOTE: this belongs in ``app/core/config.py``; see the module docstring.
    """
    raw = os.environ.get(STORAGE_ROOT_ENV)
    if raw is None or not raw.strip():
        raw = DEFAULT_STORAGE_ROOT
    return Path(raw)


def _as_root(root: Path | str) -> Path:
    return root if isinstance(root, Path) else Path(root)


# --- 路径安全 ----------------------------------------------------------------


def _reject_unsafe_part(part: str) -> None:
    """Raise ``PathTraversalError`` for a single unsafe path component."""
    if part == "":
        raise PathTraversalError("empty path component is not allowed")
    candidate = Path(part)
    if candidate.is_absolute():
        raise PathTraversalError(f"absolute path is not allowed: {part!r}")
    if ".." in candidate.parts:
        raise PathTraversalError(f"'..' component is not allowed: {part!r}")
    # URL-encoded traversal (e.g. '%2e%2e%2f' -> '../'): decode and re-check.
    decoded = unquote(part)
    if decoded != part:
        decoded_path = Path(decoded)
        if (
            decoded_path.is_absolute()
            or ".." in decoded_path.parts
            or "/" in decoded
            or "\\" in decoded
        ):
            raise PathTraversalError(
                f"encoded path traversal is not allowed: {part!r}"
            )


def resolve_within(base: Path | str, *parts: str) -> Path:
    """Safely join ``parts`` under ``base`` and return the normalized real path.

    Order of checks (per W0-5 §5.5): reject empty components, absolute paths and
    ``..`` components; then ``realpath``-normalize and verify with ``commonpath``
    that the result is still inside ``base`` (this also rejects symlink escapes,
    because ``realpath`` resolves symlinks before the containment check).

    Raises ``PathTraversalError`` for any unsafe input.
    """
    if not parts:
        raise PathTraversalError("no path components given")

    base_real = Path(os.path.realpath(base))
    joined = base_real
    for part in parts:
        _reject_unsafe_part(part)
        joined = joined / part

    target_real = Path(os.path.realpath(joined))
    try:
        common = os.path.commonpath([str(base_real), str(target_real)])
    except ValueError as exc:  # e.g. different drives
        raise PathTraversalError(f"path escapes base: {joined}") from exc
    if common != str(base_real):
        raise PathTraversalError(f"path escapes base: {target_real}")

    return target_real


# --- 类型嗅探 ----------------------------------------------------------------


def _declared_extension(declared_name: str | None) -> str | None:
    """Return the lower-cased final extension of a declared name, or ``None``.

    The declared name is **untrusted**: it is only ever used to read a suffix,
    never to build a filesystem path.
    """
    if not declared_name:
        return None
    suffix = os.path.splitext(declared_name)[1]
    return suffix.lower() or None


def _looks_like_text(content: bytes) -> bool:
    """Heuristic: valid UTF-8 with no NUL byte is treated as plain text."""
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_ofd_container(content: bytes) -> bool:
    """Return ``True`` when the ZIP bytes hold a root-level ``OFD.xml`` entry.

    OFD (电子发票标准容器, GB/T 33190) is a ZIP container whose root directory
    carries ``OFD.xml``.  Only the central directory is read — no member is ever
    decompressed — so a malicious payload cannot expand here.  Malformed ZIP
    bytes are simply *not* OFD (fail closed: the caller then treats them as a
    generic ZIP, whose declared extensions are validated as before).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    for name in names:
        # Normalize the "./" prefix some producers emit; compare exactly, so a
        # nested "Doc_0/OFD.xml" does not count as the root marker.
        if (name[2:] if name.startswith("./") else name) == OFD_MARKER:
            return True
    return False


def _detect_by_magic(content: bytes) -> tuple[str, frozenset[str] | None]:
    """Return ``(mime_type, allowed_declared_exts)`` from the real bytes.

    ``allowed_declared_exts`` is ``None`` when the type is a text/unknown-binary
    fallback (the extension cannot be validated against a magic number).
    """
    if content.startswith(PNG_SIG):
        return PNG_MIME, frozenset({".png"})
    if content.startswith(JPEG_SIG):
        return JPEG_MIME, frozenset({".jpg", ".jpeg"})
    if content.startswith(PDF_SIG):
        return PDF_MIME, frozenset({".pdf"})
    if content.startswith(ZIP_SIG):
        # ZIP family.  A root-level OFD.xml makes it an OFD container (structure,
        # not the extension); otherwise it is Office Open XML or a plain archive,
        # distinguished by the declared extension (handled by ``sniff_mime``).
        if _is_ofd_container(content):
            return OFD_MIME, OFD_ALLOWED_EXTS
        return ZIP_MIME, ZIP_ALLOWED_EXTS
    if _looks_like_text(content):
        return TEXT_MIME, None
    return OCTET_MIME, None


def sniff_mime(content: bytes, declared_name: str | None = None) -> str:
    """Determine the MIME type from the **real magic number**, not the name.

    Supports PDF, PNG, JPEG and ZIP: OFD containers are recognized by their
    root-level ``OFD.xml`` structure, docx/xlsx by the declared office extension,
    everything else as a plain archive — with a plain-text fallback.  When the
    declared extension contradicts the detected magic (including a ``.ofd``
    declaration whose bytes are not an OFD container), ``FileTypeMismatchError``
    is raised (fail closed, per W0-5 §4.3).
    """
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("content must be bytes-like")
    content = bytes(content)

    mime, allowed = _detect_by_magic(content)
    ext = _declared_extension(declared_name)

    if ext is not None:
        if allowed is not None and ext not in allowed:
            raise FileTypeMismatchError(
                f"declared extension {ext!r} does not match detected "
                f"type {mime!r}"
            )
        # Text/unknown-binary fallback: a binary-container extension on a
        # non-binary payload is a mismatch (fail closed).
        if allowed is None and ext in KNOWN_BINARY_EXTS:
            raise FileTypeMismatchError(
                f"declared extension {ext!r} does not match detected "
                f"type {mime!r}"
            )

    # Refine the generic ZIP mime for Office Open XML by declared extension.
    # (OFD was already resolved from the container structure above.)
    if mime == ZIP_MIME and ext == ".docx":
        mime = DOCX_MIME
    elif mime == ZIP_MIME and ext == ".xlsx":
        mime = XLSX_MIME

    return mime


# --- 写入 / 读取 / 校验 -------------------------------------------------------


def store_file(
    root: Path | str,
    *,
    owner_id: int,
    content: bytes,
    declared_name: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> StoredFile:
    """Store ``content`` under ``root`` with content-addressed keying.

    Checks run in this order (all before any bytes hit disk):

    1. size (``> max_bytes`` -> ``FileTooLargeError``, nothing written);
    2. real-magic type sniff (``FileTypeMismatchError`` on spoofing);
    3. sha256 -> ``storage_key = f"{sha[:2]}/{sha[2:4]}/{sha}"``.

    If the target blob already exists it is **not** rewritten and the result is
    marked ``deduped=True``.  Otherwise the write is atomic (temp file + rename).

    ``owner_id`` identifies the uploading employee for audit/metadata purposes;
    it does not affect the blob path, because identical content is shared across
    owners at the storage layer (the per-upload ownership row lives in
    ``source_files``).
    """
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("content must be bytes-like")
    content = bytes(content)

    # 1. Size gate FIRST: never write to disk before rejecting an oversized file.
    size = len(content)
    if size > max_bytes:
        raise FileTooLargeError(
            f"content size {size} exceeds limit {max_bytes}"
        )

    # 2. Real-magic type check (independent of the declared extension).
    mime_type = sniff_mime(content, declared_name)

    # 3. Content address.
    sha = hashlib.sha256(content).hexdigest()
    storage_key = f"{sha[:2]}/{sha[2:4]}/{sha}"
    dest = resolve_within(root, sha[:2], sha[2:4], sha)

    if dest.exists():
        # Content-addressed: identical content already stored, do not rewrite.
        return StoredFile(
            storage_key=storage_key,
            sha256=sha,
            size_bytes=size,
            mime_type=mime_type,
            deduped=True,
        )

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: temp file in the same directory, fsync, then rename.
    fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise

    return StoredFile(
        storage_key=storage_key,
        sha256=sha,
        size_bytes=size,
        mime_type=mime_type,
        deduped=False,
    )


def read_file(root: Path | str, storage_key: str) -> bytes:
    """Read a blob by ``storage_key``, with the same path-safety checks."""
    if not isinstance(storage_key, str):
        raise TypeError("storage_key must be a string")
    parts = storage_key.split("/")
    target = resolve_within(root, *parts)
    return target.read_bytes()


def verify_file(
    root: Path | str, storage_key: str, expected_sha256: str
) -> None:
    """Re-hash the stored blob and raise ``HashMismatchError`` on mismatch."""
    data = read_file(root, storage_key)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise HashMismatchError(
            f"sha256 mismatch for {storage_key!r}: "
            f"expected {expected_sha256!r}, got {actual!r}"
        )
