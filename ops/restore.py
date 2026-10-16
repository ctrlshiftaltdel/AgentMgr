#!/usr/bin/env python3
"""AJAIMGR 恢复校验工具：从归档恢复到**干净目标**并逐项校验。

用法（在仓库根目录用 uv 管理的解释器运行）::

    uv run python ops/restore.py ~/ajaimgr-backups/ajaimgr-backup-<UTC>.tar.gz \
        --target-db-url postgresql+psycopg:///ajaimgr_restore_check \
        --recreate-db \
        --storage-target /tmp/restore-storage \
        --orchestration-target /tmp/restore-orchestration \
        --verbose

校验项（任一项不符 → 非零退出码并逐项列出差异）::

    archive_checksum          归档 sha256 与 <归档>.sha256 旁车一致
    archive_members           成员名安全（无绝对路径 / .. / 符号链接 / 硬链接）
    manifest                  格式标识与版本可用
    member_hashes             逐成员 sha256 与字节数与 manifest 一致
    db_restore                psql 回放 dump 成功（ON_ERROR_STOP=1）
    db_tables                 manifest 记录的表全部存在
    db_row_counts             关键表行数与 manifest 一致
    storage_restore           附件逐个落盘
    storage_hashes            恢复后的附件逐个 sha256 与 manifest 一致
    storage_content_address   内容寻址布局的 blob 文件名等于其内容 sha256
    orchestration_restore     任务库文件落盘
    orchestration_sqlite      state.sqlite3 可打开、integrity_check=ok、表数一致
    orchestration_config      config.json 可解析为 JSON

安全约定：

* 归档校验和旁车缺失时**默认拒绝**（``--allow-missing-checksum`` 才放行）；
* 恢复目标数据库若属于受保护名单（``ajaimgr_dev`` / ``ajaimgr_test`` /
  ``postgres`` / ``template*``）必须显式 ``--force-target``；
* ``--recreate-db``（会 DROP 目标库）要求库名含 ``restore`` 或显式
  ``--force-target``，避免误删真实库；
* DSN 口令只经 ``PGPASSWORD`` 传递，报告与日志只写脱敏串；
* 默认工作目录与恢复目标都在仓库之外（临时目录），不触碰真实
  ``.orchestration/`` 与 ``var/storage``。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops._common import (  # noqa: E402
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    CRITICAL_TABLES,
    DbTarget,
    ToolError,
    create_database,
    database_exists,
    drop_database,
    file_sha256,
    list_tables,
    parse_db_url,
    psql_run_script,
    read_json,
    row_counts,
    sqlite_integrity,
    sqlite_table_count,
    utc_now,
    write_json,
)

MANIFEST_NAME = "manifest.json"
DB_MEMBER = "db/database.sql"
STORAGE_PREFIX = "storage/"
ORCH_PREFIX = "orchestration/"

#: 绝不允许被 ``--recreate-db`` 覆盖的库名（本机真实库）。
PROTECTED_DB_NAMES = frozenset(
    {"postgres", "template0", "template1", "ajaimgr_dev", "ajaimgr_test"}
)

CONTENT_ADDRESSED_RE = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$")


@dataclass
class Check:
    """单项校验结果。"""

    name: str
    ok: bool
    detail: str = ""
    diffs: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "diffs": list(self.diffs),
        }


# --- 归档校验 -----------------------------------------------------------------


def check_checksum(archive: Path, *, allow_missing: bool) -> tuple[Check, str]:
    digest = file_sha256(archive)
    sidecar = archive.with_name(archive.name + ".sha256")
    if not sidecar.is_file():
        if allow_missing:
            return (
                Check(
                    "archive_checksum",
                    True,
                    f"sha256={digest}; sidecar missing (allowed by --allow-missing-checksum)",
                    skipped=True,
                ),
                digest,
            )
        return (
            Check(
                "archive_checksum",
                False,
                f"checksum sidecar missing: {sidecar}",
                diffs=[f"expected sidecar file {sidecar.name} next to the archive"],
            ),
            digest,
        )
    expected = sidecar.read_text(encoding="utf-8").split()[0].strip().lower()
    ok = expected == digest
    return (
        Check(
            "archive_checksum",
            ok,
            f"sidecar={sidecar.name} expected={expected} actual={digest}",
            diffs=[] if ok else [f"archive sha256 {digest} != sidecar {expected}"],
        ),
        digest,
    )


def extract_archive(archive: Path, destination: Path) -> tuple[Check, dict]:
    """安全解包：拒绝绝对路径、``..``、符号链接与硬链接成员。"""
    unsafe: list[str] = []
    files: list[tarfile.TarInfo] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                name = member.name
                parts = Path(name).parts
                if name.startswith("/") or ".." in parts or not member.isfile():
                    unsafe.append(
                        f"{name} "
                        f"({'non-regular member' if not member.isfile() else 'unsafe path'})"
                    )
                    continue
                files.append(member)
            if unsafe:
                return (
                    Check(
                        "archive_members",
                        False,
                        f"{len(members)} members, {len(unsafe)} unsafe",
                        diffs=unsafe,
                    ),
                    {},
                )
            for member in files:
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(member)
                if handle is None:  # pragma: no cover - 由 isfile() 排除
                    continue
                with open(target, "wb") as out:
                    shutil.copyfileobj(handle, out)
    except (tarfile.TarError, OSError) as exc:
        return (
            Check("archive_members", False, f"archive cannot be read: {exc}"),
            {},
        )
    return (
        Check("archive_members", True, f"{len(files)} regular file members extracted"),
        {"member_count": len(files), "names": [m.name for m in files]},
    )


def check_manifest(extracted: Path) -> tuple[Check, dict]:
    path = extracted / MANIFEST_NAME
    if not path.is_file():
        return Check("manifest", False, f"missing {MANIFEST_NAME}"), {}
    try:
        manifest = read_json(path)
    except (ValueError, OSError) as exc:
        return Check("manifest", False, f"unreadable manifest: {exc}"), {}
    problems: list[str] = []
    if manifest.get("format") != BACKUP_FORMAT:
        problems.append(f"format {manifest.get('format')!r} != {BACKUP_FORMAT!r}")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        problems.append(
            f"format_version {manifest.get('format_version')!r} != {BACKUP_FORMAT_VERSION!r}"
        )
    if not manifest.get("members"):
        problems.append("members list is empty")
    ok = not problems
    return (
        Check(
            "manifest",
            ok,
            f"format={manifest.get('format')} version={manifest.get('format_version')} "
            f"created_at={manifest.get('created_at')} members={len(manifest.get('members') or [])}",
            diffs=problems,
        ),
        manifest,
    )


def check_member_hashes(extracted: Path, manifest: dict) -> tuple[Check, list[dict]]:
    diffs: list[str] = []
    rows: list[dict] = []
    for member in manifest.get("members", []):
        rel = member["path"]
        path = extracted / rel
        if not path.is_file():
            diffs.append(f"{rel}: missing from archive")
            rows.append({"path": rel, "ok": False, "reason": "missing"})
            continue
        digest = file_sha256(path)
        size = path.stat().st_size
        if digest != member["sha256"]:
            diffs.append(
                f"{rel}: sha256 {digest} != manifest {member['sha256']}"
            )
            rows.append({"path": rel, "ok": False, "reason": "sha256 mismatch"})
            continue
        if size != member["size_bytes"]:
            diffs.append(
                f"{rel}: size {size} != manifest {member['size_bytes']}"
            )
            rows.append({"path": rel, "ok": False, "reason": "size mismatch"})
            continue
        rows.append({"path": rel, "ok": True, "sha256": digest, "size_bytes": size})

    # 归档里出现 manifest 未登记的成员 → 视为篡改（manifest 是校验根）。
    declared = {member["path"] for member in manifest.get("members", [])}
    declared.add(MANIFEST_NAME)
    for path in sorted(extracted.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(extracted).as_posix()
        if rel not in declared:
            diffs.append(f"{rel}: member present in archive but not listed in manifest")
            rows.append({"path": rel, "ok": False, "reason": "undeclared member"})

    return (
        Check(
            "member_hashes",
            not diffs,
            f"{len(rows)} members compared against manifest",
            diffs=diffs,
        ),
        rows,
    )


# --- 数据库恢复 ---------------------------------------------------------------


def _guard_target(name: str, *, destructive: bool, force: bool) -> None:
    if name in PROTECTED_DB_NAMES and not force:
        raise ToolError(
            f"refusing to restore into protected database {name!r}; "
            "use --force-target if this is really intended"
        )
    if destructive and not force and "restore" not in name.lower():
        raise ToolError(
            f"--recreate-db would DROP database {name!r}; target name must contain "
            "'restore' (e.g. ajaimgr_restore_check) or pass --force-target"
        )


def restore_database(
    manifest: dict,
    extracted: Path,
    target: DbTarget,
    *,
    create: bool,
    recreate: bool,
    skip: bool,
    force: bool,
) -> tuple[list[Check], dict]:
    if skip:
        return (
            [
                Check("db_restore", True, "skipped by --skip-db", skipped=True),
                Check("db_tables", True, "skipped by --skip-db", skipped=True),
                Check("db_row_counts", True, "skipped by --skip-db", skipped=True),
            ],
            {},
        )

    info: dict = {"database": target.database, "dsn_redacted": target.display}
    checks: list[Check] = []

    _guard_target(target.database, destructive=recreate, force=force)

    if recreate:
        if database_exists(target.database, target=target):
            info["drop_method"] = drop_database(target.database, target=target)
        info["create_method"] = create_database(
            target.database, target=target, owner=target.user
        )
    elif create and not database_exists(target.database, target=target):
        info["create_method"] = create_database(
            target.database, target=target, owner=target.user
        )

    dump_path = extracted / DB_MEMBER
    if not dump_path.is_file():
        checks.append(Check("db_restore", False, f"missing {DB_MEMBER} in archive"))
        return checks, info

    result = psql_run_script(target, dump_path)
    stderr = result.stderr.decode("utf-8", "replace").strip()
    checks.append(
        Check(
            "db_restore",
            result.returncode == 0,
            f"psql -v ON_ERROR_STOP=1 -f {DB_MEMBER} -d {target.database} "
            f"exit={result.returncode}",
            diffs=[] if result.returncode == 0 else [stderr or "psql failed"],
        )
    )
    if result.returncode != 0:
        return checks, info

    expected_tables = manifest.get("database", {}).get("table_names", [])
    restored_tables = list_tables(target)
    missing = [t for t in expected_tables if t not in restored_tables]
    checks.append(
        Check(
            "db_tables",
            not missing,
            f"{len(restored_tables)} tables in restored database, "
            f"{len(expected_tables)} recorded in manifest",
            diffs=[f"table missing after restore: {t}" for t in missing],
        )
    )

    expected_counts = manifest.get("database", {}).get("row_counts", {})
    names = list(dict.fromkeys(list(expected_counts) + list(CRITICAL_TABLES)))
    restored_counts = row_counts(target, names)
    diffs: list[str] = []
    table_rows: list[dict] = []
    for name in names:
        want = expected_counts.get(name)
        got = restored_counts.get(name)
        ok = want == got
        if not ok:
            diffs.append(f"{name}: manifest={want} restored={got}")
        table_rows.append({"table": name, "manifest": want, "restored": got, "ok": ok})
    info["row_counts"] = table_rows
    checks.append(
        Check(
            "db_row_counts",
            not diffs,
            f"{len(table_rows)} critical tables compared",
            diffs=diffs,
        )
    )
    return checks, info


# --- 附件恢复 -----------------------------------------------------------------


def restore_storage(
    manifest: dict, extracted: Path, storage_target: Path
) -> tuple[list[Check], dict]:
    files = manifest.get("storage", {}).get("files", [])
    diffs: list[str] = []
    rows: list[dict] = []
    integrity_diffs: list[str] = []

    for entry in files:
        rel = entry["path"]
        if not rel.startswith(STORAGE_PREFIX):
            diffs.append(f"{rel}: unexpected storage member path")
            continue
        inner = rel[len(STORAGE_PREFIX):]
        source = extracted / rel
        destination = storage_target / inner
        if not source.is_file():
            diffs.append(f"{rel}: missing from archive")
            rows.append({"path": rel, "ok": False, "reason": "missing"})
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

        digest = file_sha256(destination)
        size = destination.stat().st_size
        if digest != entry["sha256"] or size != entry["size_bytes"]:
            diffs.append(
                f"{rel}: restored sha256={digest} size={size} "
                f"!= manifest sha256={entry['sha256']} size={entry['size_bytes']}"
            )
            rows.append({"path": rel, "ok": False, "reason": "sha256/size mismatch"})
            continue
        rows.append({"path": rel, "ok": True, "sha256": digest, "size_bytes": size})

        match = CONTENT_ADDRESSED_RE.match(inner)
        if match and match.group(3) != digest:
            integrity_diffs.append(
                f"{rel}: content address {match.group(3)} != actual sha256 {digest}"
            )

    storage_restore = Check(
        "storage_restore",
        not diffs,
        f"{len(rows)} attachment file(s) restored to {storage_target}",
        diffs=diffs,
    )
    storage_hashes = Check(
        "storage_hashes",
        not diffs,
        f"{sum(1 for r in rows if r.get('ok'))}/{len(rows)} attachment sha256 verified",
        diffs=diffs,
    )
    content_address = Check(
        "storage_content_address",
        not integrity_diffs,
        f"{sum(1 for r in rows if r.get('ok'))} content-addressed blob(s) checked",
        diffs=integrity_diffs,
    )
    return [storage_restore, storage_hashes, content_address], {"files": rows}


# --- 任务库恢复 ---------------------------------------------------------------


def restore_orchestration(
    manifest: dict, extracted: Path, orchestration_target: Path
) -> tuple[list[Check], dict]:
    files = manifest.get("orchestration", {}).get("files", [])
    info: dict = {"target": str(orchestration_target), "files": []}
    diffs: list[str] = []

    for entry in files:
        rel = entry["path"]
        source = extracted / rel
        if not source.is_file():
            diffs.append(f"{rel}: missing from archive")
            continue
        inner = rel[len(ORCH_PREFIX):] if rel.startswith(ORCH_PREFIX) else Path(rel).name
        destination = orchestration_target / inner
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        info["files"].append({"path": rel, "sha256": file_sha256(destination)})

    restore_check = Check(
        "orchestration_restore",
        not diffs,
        f"{len(info['files'])} file(s) restored to {orchestration_target}",
        diffs=diffs,
    )
    if diffs:
        return [restore_check], info

    state = orchestration_target / "state.sqlite3"
    if state.is_file():
        integrity = sqlite_integrity(state)
        tables = sqlite_table_count(state)
        expected = manifest.get("orchestration", {}).get("state_table_count")
        problems: list[str] = []
        if integrity != "ok":
            problems.append(f"integrity_check={integrity}")
        if expected is not None and tables != expected:
            problems.append(f"table count {tables} != manifest {expected}")
        info["state_table_count"] = tables
        info["state_integrity"] = integrity
        sqlite_check = Check(
            "orchestration_sqlite",
            not problems,
            f"integrity_check={integrity} tables={tables} (manifest={expected})",
            diffs=problems,
        )
    else:
        sqlite_check = Check(
            "orchestration_sqlite", False, "state.sqlite3 not restored"
        )

    config = orchestration_target / "config.json"
    if config.is_file():
        try:
            payload = json.loads(config.read_text(encoding="utf-8"))
            config_check = Check(
                "orchestration_config",
                True,
                f"config.json parsed ({len(payload)} key(s))",
            )
        except ValueError as exc:
            config_check = Check(
                "orchestration_config", False, f"config.json invalid: {exc}"
            )
    else:
        config_check = Check("orchestration_config", False, "config.json not restored")

    return [restore_check, sqlite_check, config_check], info


# --- 报告 ---------------------------------------------------------------------


def render_report(
    archive: Path,
    archive_sha256: str,
    checks: list[Check],
    db_info: dict,
    storage_info: dict,
    orchestration_info: dict,
    *,
    verbose: bool,
) -> None:
    print("== AJAIMGR restore verification ==")
    print(f"archive        : {archive}")
    print(f"archive sha256 : {archive_sha256}")
    print()
    print(f"{'CHECK':<26} {'RESULT':<7} DETAIL")
    for check in checks:
        print(f"{check.name:<26} {check.status:<7} {check.detail}")

    if db_info.get("row_counts"):
        print()
        print("row counts (manifest vs restored):")
        print(f"  {'table':<24} {'manifest':>10} {'restored':>10}  result")
        for row in db_info["row_counts"]:
            print(
                f"  {row['table']:<24} {str(row['manifest']):>10} "
                f"{str(row['restored']):>10}  {'OK' if row['ok'] else 'MISMATCH'}"
            )

    if storage_info.get("files"):
        print()
        print(f"attachment sha256 ({len(storage_info['files'])} file(s)):")
        for row in storage_info["files"]:
            if row.get("ok"):
                if verbose:
                    print(f"  OK   {row['path']} sha256={row['sha256']}")
            else:
                print(f"  FAIL {row['path']} ({row.get('reason')})")

    failures = [c for c in checks if not c.ok and not c.skipped]
    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} check(s) failed)")
        for check in failures:
            print(f"- {check.name}: {check.detail}")
            for diff in check.diffs:
                print(f"    {diff}")
    else:
        print(f"RESULT: PASS ({len(checks)} check(s) passed)")


# --- 主流程 -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops/restore.py",
        description="AJAIMGR 归档恢复 + 逐项校验（任一项不符即非零退出码）",
    )
    parser.add_argument("archive", help="由 ops/backup.py 生成的归档路径")
    parser.add_argument(
        "--target-db-url",
        default=None,
        help="恢复目标数据库连接串（演练建议 *_restore_check；默认取 AJAIMGR_DB_URL）",
    )
    parser.add_argument("--create-db", action="store_true", help="目标库不存在时创建")
    parser.add_argument(
        "--recreate-db",
        action="store_true",
        help="先 DROP 再 CREATE 目标库（库名须含 restore，或用 --force-target）",
    )
    parser.add_argument(
        "--force-target", action="store_true", help="允许对受保护/非演练库执行破坏性恢复"
    )
    parser.add_argument("--skip-db", action="store_true", help="跳过数据库恢复（仅校验附件/任务库）")
    parser.add_argument(
        "--storage-target", default=None, help="附件恢复目标目录（默认 <workdir>/restored-storage）"
    )
    parser.add_argument(
        "--orchestration-target",
        default=None,
        help="任务库恢复目标目录（默认 <workdir>/restored-orchestration）",
    )
    parser.add_argument("--workdir", default=None, help="解包工作目录（默认新建临时目录）")
    parser.add_argument("--report", default=None, help="把 JSON 报告写到该路径")
    parser.add_argument("--drop-db-after", action="store_true", help="校验完成后删除目标库")
    parser.add_argument("--verbose", action="store_true", help="打印每个附件的 sha256 比对结果")
    parser.add_argument(
        "--allow-missing-checksum",
        action="store_true",
        help="允许缺少 <归档>.sha256 旁车（默认拒绝）",
    )
    return parser


def _finish(
    args: argparse.Namespace,
    archive: Path,
    archive_sha256: str,
    checks: list[Check],
    *,
    manifest: dict,
    db_info: dict,
    storage_info: dict,
    orch_info: dict,
    member_rows: list[dict],
    workdir: Path | None,
    storage_target: Path | None,
    orchestration_target: Path | None,
) -> tuple[int, dict]:
    """渲染报告、按需写 JSON 报告，并返回退出码（任何 FAIL → 1）。"""
    render_report(
        archive,
        archive_sha256,
        checks,
        db_info,
        storage_info,
        orch_info,
        verbose=args.verbose,
    )
    report = {
        "archive": str(archive),
        "archive_sha256": archive_sha256,
        "verified_at": utc_now().isoformat(),
        "manifest": {
            "format": manifest.get("format"),
            "format_version": manifest.get("format_version"),
            "created_at": manifest.get("created_at"),
            "pg_dump_version": manifest.get("pg_dump_version"),
        },
        "workdir": str(workdir) if workdir else None,
        "storage_target": str(storage_target) if storage_target else None,
        "orchestration_target": (
            str(orchestration_target) if orchestration_target else None
        ),
        "checks": [c.as_dict() for c in checks],
        "member_hashes": member_rows,
        "database": db_info,
        "storage": storage_info,
        "orchestration": orch_info,
        "result": "PASS" if all(c.ok for c in checks) else "FAIL",
    }
    if args.report:
        write_json(args.report, report)
    return (0 if all(c.ok for c in checks) else 1), report


def restore(args: argparse.Namespace) -> tuple[int, dict]:
    archive = Path(args.archive).expanduser().resolve()
    if not archive.is_file():
        raise ToolError(f"archive not found: {archive}")

    checksum_check, archive_sha256 = check_checksum(
        archive, allow_missing=args.allow_missing_checksum
    )
    checks: list[Check] = [checksum_check]

    workdir: Path | None = None
    storage_target: Path | None = None
    orchestration_target: Path | None = None
    manifest: dict = {}
    db_info: dict = {}
    storage_info: dict = {}
    orch_info: dict = {}
    member_rows: list[dict] = []

    if checksum_check.ok:
        workdir = (
            Path(args.workdir).expanduser().resolve()
            if args.workdir
            else Path(tempfile.mkdtemp(prefix="ajaimgr-restore-"))
        )
        workdir.mkdir(parents=True, exist_ok=True)
        extracted = workdir / "extracted"
        storage_target = (
            Path(args.storage_target).expanduser().resolve()
            if args.storage_target
            else workdir / "restored-storage"
        )
        orchestration_target = (
            Path(args.orchestration_target).expanduser().resolve()
            if args.orchestration_target
            else workdir / "restored-orchestration"
        )

        extract_check, _ = extract_archive(archive, extracted)
        checks.append(extract_check)

        if extract_check.ok:
            manifest_check, manifest = check_manifest(extracted)
            checks.append(manifest_check)

        if extract_check.ok and manifest:
            member_check, member_rows = check_member_hashes(extracted, manifest)
            checks.append(member_check)

            target_db_url = args.target_db_url or os.environ.get("AJAIMGR_DB_URL")
            if args.skip_db:
                db_checks, db_info = restore_database(
                    manifest,
                    extracted,
                    parse_db_url(
                        target_db_url or "postgresql+psycopg:///postgres"
                    ),
                    create=False,
                    recreate=False,
                    skip=True,
                    force=args.force_target,
                )
            else:
                if not target_db_url:
                    raise ToolError(
                        "no target database: pass --target-db-url or set AJAIMGR_DB_URL"
                    )
                db_target = parse_db_url(target_db_url)
                db_checks, db_info = restore_database(
                    manifest,
                    extracted,
                    db_target,
                    create=args.create_db or args.recreate_db,
                    recreate=args.recreate_db,
                    skip=False,
                    force=args.force_target,
                )
            checks.extend(db_checks)

            storage_checks, storage_info = restore_storage(
                manifest, extracted, storage_target
            )
            checks.extend(storage_checks)

            orch_checks, orch_info = restore_orchestration(
                manifest, extracted, orchestration_target
            )
            checks.extend(orch_checks)

            if (
                args.drop_db_after
                and not args.skip_db
                and db_info.get("database")
                and target_db_url
            ):
                db_target = parse_db_url(target_db_url)
                _guard_target(
                    db_target.database, destructive=True, force=args.force_target
                )
                db_info["drop_method"] = drop_database(
                    db_target.database, target=db_target
                )

    return _finish(
        args,
        archive,
        archive_sha256,
        checks,
        manifest=manifest,
        db_info=db_info,
        storage_info=storage_info,
        orch_info=orch_info,
        member_rows=member_rows,
        workdir=workdir,
        storage_target=storage_target,
        orchestration_target=orchestration_target,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code, _ = restore(args)
    except ToolError as exc:
        print(f"RESTORE FAILED: {exc}", file=sys.stderr)
        return 2
    except (OSError, tarfile.TarError) as exc:
        print(f"RESTORE FAILED: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
