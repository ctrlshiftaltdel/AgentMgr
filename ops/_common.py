"""备份 / 恢复工具的共享助手（仅标准库 + PostgreSQL 客户端工具）。

设计依据 ``docs/DEPLOYMENT-REQUIREMENTS.md`` §5（存储与备份）：

* 附件采用内容寻址存储（``AJAIMGR_STORAGE_ROOT``，默认 ``var/storage``）；
* 数据库备份使用 ``pg_dump``，任务库（``.orchestration/``）单独备份；
* 备份必须存放于生产主机之外，且必须完成一次真实恢复演练后才算验收通过。

本模块只提供**可复用的纯函数**：散列、凭据模式判定、DSN 解析、``psql`` /
``pg_dump`` 调用、SQLite 一致性快照。任何写盘动作都由调用方
（``ops/backup.py`` / ``ops/restore.py``）决定，因此本模块可以安全地被测试
导入。

安全约定（对齐 AGENTS.md §7 与 DEPLOYMENT-REQUIREMENTS §2）：

* **凭据永不落盘**：DSN 中的口令只经环境变量 ``PGPASSWORD`` 传给 libpq，
  绝不进入 argv（``ps`` 可见）、日志或归档；对外展示一律用
  :attr:`DbTarget.display`（脱敏）。
* **凭据不入归档**：按文件名模式排除 ``.env`` / ``auth.json`` / ``*.key`` /
  ``*.pem`` / ``credentials*`` 等，并在归档完成后做一次扫描断言
  （``ops/backup.py`` 的 ``scan_archive_for_credentials``）。
* **任务库只读**：``.orchestration/state.sqlite3`` 用 SQLite 只读 URI 打开，
  再用在线备份 API 生成一致快照；原文件不会被写入。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_FORMAT_VERSION",
    "CREDENTIAL_CONTENT_MARKERS",
    "CREDENTIAL_NAME_PATTERNS",
    "CRITICAL_TABLES",
    "DEFAULT_OUTPUT_DIR",
    "DbTarget",
    "ToolError",
    "create_database",
    "database_exists",
    "drop_database",
    "dump_database",
    "file_sha256",
    "is_credential_path",
    "list_tables",
    "parse_db_url",
    "pg_dump_version",
    "psql_rows",
    "psql_run_script",
    "read_json",
    "row_counts",
    "sha256_bytes",
    "sqlite_integrity",
    "sqlite_snapshot",
    "sqlite_table_count",
    "utc_now",
    "utc_stamp",
    "write_json",
]

#: 归档格式标识与版本（``manifest.json`` 的 ``format`` / ``format_version``）。
BACKUP_FORMAT = "ajaimgr-backup"
BACKUP_FORMAT_VERSION = 1

#: 默认输出目录：仓库之外（DEPLOYMENT-REQUIREMENTS §5「备份必须存放于生产主机
#: 之外」；本机演练时至少不落在仓库内，避免误提交）。
DEFAULT_OUTPUT_DIR = Path.home() / "ajaimgr-backups"

#: manifest 行数比对口径：数据库关键表（W1-25 验收标准第 1/2 步）。
CRITICAL_TABLES: tuple[str, ...] = (
    "companies",
    "employees",
    "expense_claims",
    "expense_lines",
    "audit_events",
    "invoice_verifications",
    "source_files",
)

#: 凭据类**文件名**模式（大小写不敏感，逐路径部件匹配）。
#: 命中即从归档中排除，并在 manifest 的 ``excluded_credential_files`` 中留痕。
CREDENTIAL_NAME_PATTERNS: tuple[str, ...] = (
    r"^\.env$",
    r"^\.env\..+$",
    r"^\.netrc$",
    r"^\.pgpass$",
    r"^\.htpasswd$",
    r"^auth\.json$",
    r"^credentials$",
    r"^credentials\.json$",
    r"^credentials\..+$",
    r"^secret$",
    r"^secret\.json$",
    r"^secrets\.json$",
    r"^secrets\..+$",
    r"^token\.json$",
    r"^service-account.*\.json$",
    r"^.*\.key$",
    r"^.*\.pem$",
    r"^.*\.p12$",
    r"^.*\.pfx$",
    r"^.*\.jks$",
    r"^.*\.keystore$",
    r"^.*\.kdbx$",
    r"^id_rsa$",
    r"^id_ed25519$",
    r"^id_ecdsa$",
)

_CREDENTIAL_RES = tuple(re.compile(p, re.IGNORECASE) for p in CREDENTIAL_NAME_PATTERNS)

#: 内容级凭据标记：私钥头。命中即视为凭据泄漏（fail closed）。
CREDENTIAL_CONTENT_MARKERS: tuple[bytes, ...] = (
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
)

class ToolError(RuntimeError):
    """备份/恢复工具的可预期失败（调用方转成非零退出码）。"""


# --- 时间与散列 ---------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(moment: datetime | None = None) -> str:
    """``20261016T085300Z`` 形式的 UTC 时间戳（用于归档文件名）。"""
    return (moment or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path | str) -> str:
    """流式计算文件 sha256（不整体读入内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path | str) -> dict:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path | str, payload: object) -> None:
    import json

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --- 凭据判定 -----------------------------------------------------------------


def is_credential_path(path: str | Path) -> bool:
    """路径中任一部件命中凭据模式即返回 ``True``。"""
    for part in Path(path).parts:
        if part in ("/", "."):
            continue
        for rx in _CREDENTIAL_RES:
            if rx.match(part):
                return True
    return False


# --- DSN 解析 -----------------------------------------------------------------

_URL_RE = re.compile(r"^(?P<scheme>[A-Za-z0-9_+.]+)://(?P<rest>.*)$", re.S)


def _libpq_dsn(netloc: str, path: str, query: str = "") -> str:
    """拼 libpq 连接 URI。

    注意：不能用 ``urllib.parse.urlunsplit``——``postgresql`` 不在其
    ``uses_netloc`` 列表中，空 netloc 时会把 ``postgresql:///db`` 压成
    ``postgresql:/db``，libpq 会把它当成库名。这里手工拼接。
    """
    dsn = f"postgresql://{netloc}{path}"
    if query:
        dsn = f"{dsn}?{query}"
    return dsn


@dataclass(frozen=True)
class DbTarget:
    """一个 PostgreSQL 连接目标（口令永不进入 argv）。"""

    raw: str
    libpq_dsn: str
    database: str
    host: str = ""
    port: int | None = None
    user: str | None = None
    password: str | None = None
    options: dict[str, str] = field(default_factory=dict)

    @property
    def display(self) -> str:
        """脱敏展示串（绝不含口令）。"""
        if self.password is None:
            return self.libpq_dsn
        netloc = self.host or ""
        if self.user:
            netloc = f"{quote(self.user)}@{netloc}"
        if self.port:
            netloc = f"{netloc}:{self.port}"
        return f"{_libpq_dsn(netloc, f'/{self.database}')} (password omitted)"

    def subprocess_env(self) -> dict[str, str]:
        """子进程环境：口令只经 ``PGPASSWORD`` 传递。"""
        env = dict(os.environ)
        if self.password:
            env["PGPASSWORD"] = self.password
        return env

    def with_database(self, name: str) -> "DbTarget":
        """同主机/同凭据、换库名的副本。"""
        parts = urlsplit(self.libpq_dsn)
        return DbTarget(
            raw=self.raw,
            libpq_dsn=_libpq_dsn(parts.netloc, f"/{name}", parts.query),
            database=name,
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            options=dict(self.options),
        )


def parse_db_url(raw: str) -> DbTarget:
    """解析 SQLAlchemy 风格 DSN（``postgresql+psycopg://...``）或 libpq DSN。

    支持 ``postgresql+psycopg:///ajaimgr_dev``（unix socket + peer 认证）以及
    ``postgresql+psycopg://user:pw@host:5432/db?sslmode=require``。口令会被剥离
    出连接串，改由环境变量传递。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ToolError("empty database URL")

    match = _URL_RE.match(raw)
    if match is None:
        # 非 URL 形式（libpq keyword/value 或裸库名）：原样交给 libpq。
        name = raw.split()[0] if raw.split() else raw
        return DbTarget(raw=raw, libpq_dsn=raw, database=name)

    rest = match.group("rest")
    parts = urlsplit("postgresql://" + rest)
    database = parts.path.lstrip("/")
    if not database:
        raise ToolError(f"database name missing in URL: {raw}")

    options: dict[str, str] = {}
    if parts.query:
        for pair in parts.query.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                options[key] = value

    password = parts.password
    netloc = parts.hostname or ""
    if parts.username:
        netloc = f"{quote(parts.username)}@{netloc}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    libpq_dsn = _libpq_dsn(netloc, parts.path, parts.query)

    return DbTarget(
        raw=raw,
        libpq_dsn=libpq_dsn,
        database=database,
        host=parts.hostname or "",
        port=parts.port,
        user=parts.username,
        password=password,
        options=options,
    )


# --- 子进程封装 ---------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            env=env,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - 环境缺工具
        raise ToolError(f"command not found: {argv[0]}") from exc


def pg_dump_version() -> str:
    result = _run(["pg_dump", "--version"])
    if result.returncode != 0:
        raise ToolError("pg_dump --version failed")
    return result.stdout.decode("utf-8", "replace").strip()


def psql_rows(target: DbTarget, sql: str, *, database: str | None = None) -> list[list[str]]:
    """执行一条 SQL，返回以 ``\\t`` 分隔的行（``-A -t`` 无边框输出）。"""
    dsn = target.libpq_dsn if database is None else target.with_database(database).libpq_dsn
    result = _run(
        ["psql", "-X", "-q", "-A", "-t", "-F", "\t", "-d", dsn, "-c", sql],
        env=target.subprocess_env(),
    )
    if result.returncode != 0:
        raise ToolError(
            "psql failed: " + result.stderr.decode("utf-8", "replace").strip()
        )
    out = result.stdout.decode("utf-8", "replace")
    return [line.split("\t") for line in out.splitlines() if line.strip() != ""]


def psql_run_script(target: DbTarget, script: Path) -> subprocess.CompletedProcess:
    """用 ``ON_ERROR_STOP=1`` 回放 SQL 脚本（数据库恢复）。"""
    return _run(
        [
            "psql",
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            target.libpq_dsn,
            "-f",
            str(script),
        ],
        env=target.subprocess_env(),
    )


def dump_database(target: DbTarget, out_path: Path) -> str:
    """``pg_dump`` 到 ``out_path``（plain SQL，``--no-owner``）。

    ``--no-owner`` 让归档可以在不同角色下恢复（本机演练角色与生产角色不同）；
    所有权与 GRANT 由 DEPLOYMENT-REQUIREMENTS §2 的角色/授权脚本负责重建，
    这一点已在 ``docs/BACKUP-RESTORE.md`` 中作为已知限制写明。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            "pg_dump",
            "--format=plain",
            "--no-owner",
            "--no-password",
            "-d",
            target.libpq_dsn,
            "-f",
            str(out_path),
        ],
        env=target.subprocess_env(),
    )
    if result.returncode != 0:
        raise ToolError(
            "pg_dump failed: " + result.stderr.decode("utf-8", "replace").strip()
        )
    return pg_dump_version()


# --- 数据库内省 ---------------------------------------------------------------


def list_tables(target: DbTarget) -> list[str]:
    """``public`` 模式下的普通表名（升序）。"""
    rows = psql_rows(
        target,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
    )
    return [row[0] for row in rows if row]


def row_counts(target: DbTarget, tables: tuple[str, ...] | list[str]) -> dict[str, int | None]:
    """按 ``tables`` 返回行数；表不存在记 ``None``（用于恢复后比对差异）。"""
    present = set(list_tables(target))
    wanted = [t for t in tables if t in present]
    counts: dict[str, int | None] = {t: None for t in tables}
    if not wanted:
        return counts
    union = " UNION ALL ".join(
        f"SELECT '{t}' AS t, count(*) AS n FROM {t}" for t in wanted
    )
    for name, value in psql_rows(target, union):
        counts[name] = int(value)
    return counts


def database_exists(name: str, *, target: DbTarget) -> bool:
    rows = psql_rows(
        target,
        f"SELECT 1 FROM pg_database WHERE datname = '{name.replace(chr(39), chr(39) * 2)}'",
        database="postgres",
    )
    return bool(rows)


def create_database(name: str, *, target: DbTarget, owner: str | None = None) -> str:
    """创建数据库，返回实际使用的方式（``createdb`` / ``sudo -u postgres``）。"""
    argv = ["createdb"]
    if owner:
        argv += ["-O", owner]
    argv.append(name)
    result = _run(argv, env=target.subprocess_env())
    if result.returncode == 0:
        return "createdb"
    first_error = result.stderr.decode("utf-8", "replace").strip()
    sudo = ["sudo", "-n", "-u", "postgres", "createdb"]
    if owner:
        sudo += ["-O", owner]
    sudo.append(name)
    fallback = _run(sudo, env=target.subprocess_env())
    if fallback.returncode == 0:
        return "sudo -n -u postgres createdb"
    raise ToolError(
        f"cannot create database {name!r}: createdb failed ({first_error}); "
        f"sudo -u postgres fallback failed "
        f"({fallback.stderr.decode('utf-8', 'replace').strip()})"
    )


def drop_database(name: str, *, target: DbTarget) -> str:
    """删除数据库（``--if-exists``），返回实际使用的方式。"""
    result = _run(["dropdb", "--if-exists", name], env=target.subprocess_env())
    if result.returncode == 0:
        return "dropdb"
    first_error = result.stderr.decode("utf-8", "replace").strip()
    fallback = _run(
        ["sudo", "-n", "-u", "postgres", "dropdb", "--if-exists", name],
        env=target.subprocess_env(),
    )
    if fallback.returncode == 0:
        return "sudo -n -u postgres dropdb"
    raise ToolError(
        f"cannot drop database {name!r}: dropdb failed ({first_error}); "
        f"sudo -u postgres fallback failed "
        f"({fallback.stderr.decode('utf-8', 'replace').strip()})"
    )


# --- SQLite（任务库） ---------------------------------------------------------


def _readonly_uri(path: Path | str) -> str:
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def sqlite_snapshot(src: Path | str, dst: Path | str) -> None:
    """生成一致快照，且**不在源目录留下任何文件**（含 ``-wal`` / ``-shm``）。

    做法：先把 ``state.sqlite3``（连同可能存在的 ``-wal`` / ``-shm`` 旁车）复制到
    ``dst`` 所在目录，再对**副本**用 SQLite 在线备份 API 生成 ``dst``，最后对
    ``dst`` 跑 ``PRAGMA integrity_check``（非 ``ok`` 直接报错）。这样任务库目录
    始终只读——直接对源文件打开连接会在源目录创建 ``-shm``/``-wal`` 旁车文件
    （W1-25 实测发生过），属于对受保护目录的写入。
    """
    src_path = Path(src)
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    staged = dst_path.parent / f".snapshot-source-{src_path.name}"
    shutil.copy2(src_path, staged)
    staged_siblings: list[Path] = []
    for suffix in ("-wal", "-shm"):
        sibling = Path(f"{src_path}{suffix}")
        if sibling.is_file():
            staged_sibling = Path(f"{staged}{suffix}")
            shutil.copy2(sibling, staged_sibling)
            staged_siblings.append(staged_sibling)

    try:
        source = sqlite3.connect(str(staged))
        try:
            target = sqlite3.connect(str(dst_path))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    finally:
        staged.unlink(missing_ok=True)
        for staged_sibling in staged_siblings:
            staged_sibling.unlink(missing_ok=True)

    integrity = sqlite_integrity(dst_path)
    if integrity != "ok":
        raise ToolError(
            f"sqlite snapshot of {src_path} failed integrity_check: {integrity}"
        )


def sqlite_table_count(path: Path | str) -> int:
    """``sqlite_master`` 中 ``type='table'`` 的条目数（只读打开）。"""
    connection = sqlite3.connect(_readonly_uri(path), uri=True)
    try:
        rows = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()
        return int(rows[0])
    finally:
        connection.close()


def sqlite_integrity(path: Path | str) -> str:
    """``PRAGMA integrity_check`` 的首行结果（``ok`` 表示通过）。"""
    connection = sqlite3.connect(_readonly_uri(path), uri=True)
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        return "; ".join(str(row[0]) for row in rows)
    finally:
        connection.close()


# --- 目录工具 -----------------------------------------------------------------


def iter_files(root: Path | str):
    """按稳定顺序遍历 ``root`` 下的普通文件（不跟随符号链接）。"""
    root_path = Path(root)
    if not root_path.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            yield candidate


def copy_file(src: Path | str, dst: Path | str) -> None:
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_path)


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
