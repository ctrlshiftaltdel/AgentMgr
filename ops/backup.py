#!/usr/bin/env python3
"""AJAIMGR 备份工具：数据库 + 附件内容寻址目录 + 任务库 → 单一归档。

用法（在仓库根目录用 uv 管理的解释器运行）::

    uv run python ops/backup.py --output /var/backups/ajaimgr/ajaimgr.tar.gz

归档内容（``manifest.json`` 记录逐成员 sha256/字节数与数据库关键表行数）::

    manifest.json                     # 归档根：格式版本、行数、成员散列、排除项
    db/database.sql                   # pg_dump --format=plain --no-owner
    storage/<aa>/<bb>/<sha256>        # AJAIMGR_STORAGE_ROOT 的全部内容寻址文件
    orchestration/state.sqlite3       # 任务库（SQLite 在线备份快照，只读源）
    orchestration/config.json         # 任务库配置

另生成 ``<归档>.sha256`` 校验和旁车文件（``sha256sum`` 格式），供
``ops/restore.py`` 在恢复前拒绝被篡改的归档。

硬约束（对齐 ``docs/DEPLOYMENT-REQUIREMENTS.md`` §5 与 AGENTS.md §7）:

* **凭据绝不入归档**：``.env`` / ``auth.json`` / ``*.key`` / ``*.pem`` /
  ``credentials*`` 等按文件名模式排除，排除项记入 manifest；归档完成后
  再扫描一次成员名与私钥头，命中即中止并删除归档（fail closed）。
* **默认不落在仓库内**：默认输出目录 ``~/ajaimgr-backups``；若输出路径位于
  仓库内则直接拒绝（防止把备份提交进版本库）。
* **任务库只读**：``.orchestration/`` 下的文件只读打开，不写入、不改动。
* **必需组成部分缺失即失败**：解析后的附件存储根（``--storage-root``，默认
  ``<repo-root>/var/storage``）与任务库目录（``--orchestration-dir``，默认
  ``<repo-root>/.orchestration``）必须存在且为目录；任一缺失时非零退出
  （``ToolError`` → 退出码 2）且**不写出任何归档/旁车文件**——只有显式给出
  ``--allow-missing-storage`` / ``--allow-missing-orchestration`` 才放行，
  放行时 manifest 顶部 ``missing_components`` 记录缺失清单（正常情况下为空）。
* 不引入任何新依赖：``pg_dump`` / ``psql``（PostgreSQL 客户端） + 标准库。
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 允许 `python3 ops/backup.py` 直接运行
    sys.path.insert(0, str(REPO_ROOT))

from ops._common import (  # noqa: E402
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    CREDENTIAL_CONTENT_MARKERS,
    CRITICAL_TABLES,
    DEFAULT_OUTPUT_DIR,
    ToolError,
    copy_file,
    dump_database,
    file_sha256,
    is_credential_path,
    iter_files,
    list_tables,
    parse_db_url,
    python_version,
    row_counts,
    sqlite_integrity,
    sqlite_snapshot,
    sqlite_table_count,
    utc_now,
    utc_stamp,
    write_json,
)

#: 归档内的顶层目录名。
DB_DIR = "db"
STORAGE_DIR = "storage"
ORCH_DIR = "orchestration"
MANIFEST_NAME = "manifest.json"

#: 内容寻址布局：``<aa>/<bb>/<sha256>``。
CONTENT_ADDRESSED_RE = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$")

#: 归档成员大小超过该值时跳过内容扫描（只做文件名扫描）。
CONTENT_SCAN_LIMIT = 64 * 1024 * 1024


# --- 默认值 -------------------------------------------------------------------


def default_db_url() -> str:
    """``--db-url`` > ``AJAIMGR_DB_URL`` > ``app.core.config`` 默认值。"""
    raw = os.environ.get("AJAIMGR_DB_URL")
    if raw and raw.strip():
        return raw.strip()
    try:
        from app.core.config import load_settings

        return load_settings().db_url
    except Exception:  # pragma: no cover - 产品代码不可导入时的兜底
        return "postgresql+psycopg:///ajaimgr_dev"


def default_storage_root() -> str:
    raw = os.environ.get("AJAIMGR_STORAGE_ROOT")
    if raw and raw.strip():
        return raw.strip()
    return "var/storage"


#: ``--orchestration-dir`` 的默认值：相对 ``--repo-root`` 解析（与
#: ``--storage-root`` 一致），不再取自脚本自身位置。
DEFAULT_ORCHESTRATION_SUBDIR = ".orchestration"


def resolve_under(base: Path, raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


# --- 收集成员 -----------------------------------------------------------------


def _member(path: str, sha256: str, size: int, kind: str) -> dict:
    return {"path": path, "sha256": sha256, "size_bytes": size, "kind": kind}


def collect_storage(
    root: Path, stage: Path, *, label: str = "storage"
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """把附件存储根的全部文件复制进暂存区并计算散列。

    返回 ``(members, excluded, integrity_errors, warnings)``。命中凭据模式的
    文件被**排除**并记录；内容寻址文件名与其内容 sha256 不符时记入
    ``integrity_errors``（仍然备份，但演练/恢复阶段会被单独报出）。
    """
    members: list[dict] = []
    excluded: list[dict] = []
    integrity_errors: list[dict] = []
    warnings: list[str] = []

    if not root.is_dir():
        warnings.append(f"{label} root does not exist or is not a directory: {root}")
        return members, excluded, integrity_errors, warnings

    for source in iter_files(root):
        rel = source.relative_to(root)
        rel_posix = rel.as_posix()
        if is_credential_path(rel_posix):
            excluded.append({"root": label, "path": rel_posix})
            continue
        digest = file_sha256(source)
        size = source.stat().st_size
        match = CONTENT_ADDRESSED_RE.match(rel_posix)
        if match and match.group(3) != digest:
            integrity_errors.append(
                {
                    "root": label,
                    "path": rel_posix,
                    "expected_sha256": match.group(3),
                    "actual_sha256": digest,
                }
            )
        destination = stage / label / rel
        copy_file(source, destination)
        members.append(_member(f"{label}/{rel_posix}", digest, size, label))

    if not members:
        warnings.append(f"{label} root contains no files: {root}")
    return members, excluded, integrity_errors, warnings


def collect_orchestration(
    root: Path, stage: Path
) -> tuple[list[dict], list[dict], dict, list[str]]:
    """备份任务库：``state.sqlite3``（只读一致快照）+ ``config.json``。"""
    members: list[dict] = []
    excluded: list[dict] = []
    meta: dict = {"source_dir": str(root)}
    warnings: list[str] = []

    if not root.is_dir():
        warnings.append(f"orchestration dir does not exist: {root}")
        return members, excluded, meta, warnings

    for source in iter_files(root):
        rel_posix = source.relative_to(root).as_posix()
        if is_credential_path(rel_posix):
            excluded.append({"root": "orchestration", "path": rel_posix})
            continue
        if rel_posix not in ("state.sqlite3", "config.json"):
            # 只备份任务库本体与配置；.orchestration/backups/ 等派生副本不重复归档。
            continue
        if rel_posix == "state.sqlite3":
            destination = stage / ORCH_DIR / "state.sqlite3"
            sqlite_snapshot(source, destination)
            meta["state_table_count"] = sqlite_table_count(destination)
            meta["state_integrity"] = sqlite_integrity(destination)
            meta["state_source"] = str(source)
        else:
            destination = stage / ORCH_DIR / "config.json"
            copy_file(source, destination)
            meta["config_source"] = str(source)
        members.append(
            _member(
                f"{ORCH_DIR}/{rel_posix}",
                file_sha256(destination),
                destination.stat().st_size,
                "orchestration",
            )
        )

    if not members:
        warnings.append(f"orchestration dir contains no state.sqlite3/config.json: {root}")
    return members, excluded, meta, warnings


# --- 归档 ---------------------------------------------------------------------


def _normalise_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """抹掉本机 uid/gid/用户名与权限噪声，便于跨机恢复与比对。"""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o600 if info.isfile() else 0o700
    return info


def write_archive(stage: Path, archive: Path, member_paths: list[str]) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        tar.add(stage / MANIFEST_NAME, arcname=MANIFEST_NAME, filter=_normalise_tarinfo)
        for rel in member_paths:
            tar.add(stage / rel, arcname=rel, filter=_normalise_tarinfo)


def scan_archive_for_credentials(archive: Path) -> dict:
    """扫描断言：归档成员名不得命中凭据模式，内容不得含私钥头。"""
    violations: list[dict] = []
    member_count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            member_count += 1
            if not member.isfile():
                violations.append(
                    {"path": member.name, "reason": "non-regular member in archive"}
                )
                continue
            if is_credential_path(member.name):
                violations.append(
                    {"path": member.name, "reason": "credential-like file name"}
                )
                continue
            if member.size <= CONTENT_SCAN_LIMIT:
                handle = tar.extractfile(member)
                data = handle.read() if handle is not None else b""
                for marker in CREDENTIAL_CONTENT_MARKERS:
                    if marker in data:
                        violations.append(
                            {
                                "path": member.name,
                                "reason": f"private key marker: {marker.decode()}",
                            }
                        )
                        break
    return {"member_count": member_count, "violations": violations}


def write_checksum_sidecar(archive: Path) -> Path:
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{file_sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return sidecar


# --- 主流程 -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops/backup.py",
        description="AJAIMGR 数据库 + 附件 + 任务库备份（单一归档，含 manifest 与校验和）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="数据库连接串（默认取 AJAIMGR_DB_URL，其次 app.core.config 默认值）",
    )
    parser.add_argument(
        "--storage-root",
        default=None,
        help="附件存储根（默认取 AJAIMGR_STORAGE_ROOT，其次 var/storage，相对 --repo-root 解析）",
    )
    parser.add_argument(
        "--orchestration-dir",
        default=None,
        help="任务库目录（默认 <repo-root>/.orchestration）",
    )
    parser.add_argument(
        "--allow-missing-storage",
        action="store_true",
        help="允许附件存储根缺失（默认：缺失即失败，退出码 2，不写出归档）",
    )
    parser.add_argument(
        "--allow-missing-orchestration",
        action="store_true",
        help="允许任务库目录缺失（默认：缺失即失败，退出码 2，不写出归档）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=f"归档输出路径（默认 {DEFAULT_OUTPUT_DIR}/ajaimgr-backup-<UTC>.tar.gz；不得位于仓库内）",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="仓库根（用于默认路径与「输出不得在仓库内」检查）",
    )
    parser.add_argument("--label", default="", help="可选备注，写入 manifest")
    return parser


def create_backup(args: argparse.Namespace) -> dict:
    repo_root = Path(args.repo_root).expanduser().resolve()

    db_url = args.db_url or default_db_url()
    target = parse_db_url(db_url)
    storage_root = resolve_under(repo_root, args.storage_root or default_storage_root())
    # 缺陷 B 修复：任务库默认值同样按 --repo-root 解析（与 --storage-root 一致），
    # 不再取自脚本自身位置（REPO_ROOT）。
    orchestration_dir = resolve_under(
        repo_root, args.orchestration_dir or DEFAULT_ORCHESTRATION_SUBDIR
    )

    # 缺陷 A 守卫：必需组成部分缺失时必须在**写出归档之前**失败（走 ToolError →
    # 退出码 2），不得留下残缺归档或旁车文件；只有显式开关可以放行。
    missing_components = [
        name
        for name, path in (
            ("storage", storage_root),
            ("orchestration", orchestration_dir),
        )
        if not path.is_dir()
    ]
    blockers: list[str] = []
    if "storage" in missing_components and not args.allow_missing_storage:
        blockers.append(
            f"required component 'storage' is missing: {storage_root} "
            "(does not exist or is not a directory); pass --allow-missing-storage "
            "to back up without attachment storage"
        )
    if "orchestration" in missing_components and not args.allow_missing_orchestration:
        blockers.append(
            f"required component 'orchestration' is missing: {orchestration_dir} "
            "(does not exist or is not a directory); pass --allow-missing-orchestration "
            "to back up without the task ledger"
        )
    if blockers:
        raise ToolError("\n".join(blockers))

    if args.output:
        archive = Path(args.output).expanduser()
        if not archive.is_absolute():
            archive = Path.cwd() / archive
        archive = archive.resolve()
    else:
        archive = (DEFAULT_OUTPUT_DIR / f"ajaimgr-backup-{utc_stamp()}.tar.gz").resolve()

    if archive == repo_root or repo_root in archive.parents:
        raise ToolError(
            f"refusing to write the backup inside the repository: {archive} "
            f"(repo root {repo_root}); 备份必须存放于生产主机之外 "
            "(DEPLOYMENT-REQUIREMENTS §5)"
        )

    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ajaimgr-backup-stage-") as tmp:
        stage = Path(tmp)

        # 1) 数据库完整备份 + 关键表行数。
        (stage / DB_DIR).mkdir(parents=True, exist_ok=True)
        dump_path = stage / DB_DIR / "database.sql"
        dump_version = dump_database(target, dump_path)
        tables = list_tables(target)
        counts = row_counts(target, CRITICAL_TABLES)
        missing = [t for t in CRITICAL_TABLES if t not in tables]
        if missing:
            warnings.append(f"critical tables absent in source database: {missing}")

        members: list[dict] = [
            _member(
                f"{DB_DIR}/database.sql",
                file_sha256(dump_path),
                dump_path.stat().st_size,
                "database",
            )
        ]

        # 2) 附件内容寻址目录。
        storage_members, storage_excluded, integrity_errors, storage_warnings = (
            collect_storage(storage_root, stage, label=STORAGE_DIR)
        )
        members.extend(storage_members)
        warnings.extend(storage_warnings)

        # 3) 任务库。
        orch_members, orch_excluded, orch_meta, orch_warnings = collect_orchestration(
            orchestration_dir, stage
        )
        members.extend(orch_members)
        warnings.extend(orch_warnings)

        # 4) manifest（自身不入 members：它是校验的根，无法自证）。
        manifest = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            # 顶部显式记录：缺失且被 --allow-missing-* 放行的必需组成部分
            # （正常场景为空 list）。
            "missing_components": missing_components,
            "created_at": utc_now().isoformat(),
            "tool": "ops/backup.py",
            "label": args.label,
            "host": socket.gethostname(),
            "python_version": python_version(),
            "pg_dump_version": dump_version,
            "database": {
                "dsn_redacted": target.display,
                "database": target.database,
                "dump_path": f"{DB_DIR}/database.sql",
                "dump_sha256": members[0]["sha256"],
                "dump_size_bytes": members[0]["size_bytes"],
                "table_names": tables,
                "row_counts": counts,
                "critical_tables": list(CRITICAL_TABLES),
                "missing_critical_tables": missing,
            },
            "storage": {
                "source_root": str(storage_root),
                "file_count": len(storage_members),
                "total_bytes": sum(m["size_bytes"] for m in storage_members),
                "files": storage_members,
                "content_address_integrity_errors": integrity_errors,
            },
            "orchestration": {
                **orch_meta,
                "source_dir": str(orchestration_dir),
                "file_count": len(orch_members),
                "files": orch_members,
            },
            "members": members,
            "excluded_credential_files": storage_excluded + orch_excluded,
            "warnings": warnings,
            "notes": [
                "manifest.json 自身不在 members 中：它是校验根，无法自证散列。",
                "pg_dump 使用 --no-owner；所有权与 GRANT 由角色/授权脚本重建。",
                "备份必须复制到生产主机之外（本机演练不满足该生产要求）。",
            ],
        }
        write_json(stage / MANIFEST_NAME, manifest)

        member_paths = [m["path"] for m in members]
        write_archive(stage, archive, member_paths)

        scan = scan_archive_for_credentials(archive)
        if scan["violations"]:
            archive.unlink(missing_ok=True)
            archive.with_name(archive.name + ".sha256").unlink(missing_ok=True)
            raise ToolError(
                "credential scan failed; archive deleted: "
                + "; ".join(f"{v['path']} ({v['reason']})" for v in scan["violations"])
            )

    sidecar = write_checksum_sidecar(archive)
    return {
        "archive": str(archive),
        "archive_size_bytes": archive.stat().st_size,
        "archive_sha256": file_sha256(archive),
        "checksum_sidecar": str(sidecar),
        "members": len(members) + 1,
        "missing_components": missing_components,
        "database": manifest["database"],
        "storage_file_count": manifest["storage"]["file_count"],
        "orchestration_file_count": manifest["orchestration"]["file_count"],
        "excluded_credential_files": manifest["excluded_credential_files"],
        "credential_scan": scan,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = create_backup(args)
    except ToolError as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 2

    print("BACKUP OK")
    print(f"  archive          : {summary['archive']}")
    print(f"  archive size     : {summary['archive_size_bytes']} bytes")
    print(f"  archive sha256   : {summary['archive_sha256']}")
    print(f"  checksum sidecar : {summary['checksum_sidecar']}")
    print(f"  members          : {summary['members']}")
    print(
        "  database         : "
        f"{summary['database']['database']} "
        f"({len(summary['database']['table_names'])} tables, dump "
        f"{summary['database']['dump_size_bytes']} bytes)"
    )
    print("  row counts       : " + ", ".join(
        f"{name}={count}" for name, count in summary["database"]["row_counts"].items()
    ))
    print(f"  storage files    : {summary['storage_file_count']}")
    print(f"  orchestration    : {summary['orchestration_file_count']} file(s)")
    print(
        "  missing parts    : "
        + (", ".join(summary["missing_components"]) or "none")
    )
    print(
        "  excluded creds   : "
        + (", ".join(e["path"] for e in summary["excluded_credential_files"]) or "none")
    )
    print(
        f"  credential scan  : {summary['credential_scan']['member_count']} members, "
        f"{len(summary['credential_scan']['violations'])} violation(s)"
    )
    for warning in summary["warnings"]:
        print(f"  WARNING          : {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
