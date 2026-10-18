#!/usr/bin/env python3
"""AJAIMGR 跨主机 / 异地恢复的**预检 + 本机 dry-run** 封装（Issue #22）。

本模块只做两件事，且**只在单机本地运行**：

``preflight``
    校验操作者显式声明的目标环境（目标库名、目标目录、目标机 PostgreSQL
    主版本）、归档完整性（校验和 / 成员安全 / manifest / 逐成员散列）与
    ``PG*`` 环境变量是否就位；不向目标目录写入任何文件。

``rehearse``
    在 ``preflight`` 全部通过后，用**本机临时目录**复现目标机的落盘步骤：
    传输模拟（复制归档 + 校验和复核）→ 解包 → 附件落盘与散列复核 → 任务库
    落盘与 SQLite 完整性复核 → 写出证据 JSON。数据库恢复步骤**不执行**，
    只作为计划命令输出（见 ``docs/BACKUP-RESTORE.md`` §11）。

它**不建立任何网络连接**：``--target-host`` 只是一个取证标签，本工具不解析
（DNS）、不 ``ssh``、不 ``scp``、不连接任何异地数据库、不执行真实跨主机或
异地恢复。真实演练需要在目标机上执行本工具与 ``ops/restore.py``。

退出码
------

``0``
    ``preflight`` / ``rehearse`` 全部校验通过（``rehearse`` 的数据库步骤记为
    ``NOT_EXECUTED``，结果串明确标注 dry-run）。
``1``
    校验项不符：归档被篡改、成员散列不符、目标机 PG 主版本低于归档
    ``pg_dump`` 主版本、附件/任务库落盘校验失败等。
``2``
    用法 / 声明 / 环境错误（fail closed）：缺参、占位符未替换、受保护或
    生产库名、目标目录不存在或非空、缺少 ``PGHOST`` / ``PGUSER``、
    使用了 ``--allow-missing-*`` 等。

安全约定
--------

* 目标库（``--target-db``）与目标目录（``--target-dir``）**必须显式声明**，
  没有默认值；缺失、冲突、占位符未替换一律非零退出。
* ``--target-db`` 只接受**裸库名**：连接串与口令只能来自 ``PG*`` 环境变量，
  绝不接受 DSN 参数，也绝不把环境变量的值写入 stdout、报告或证据文件
  （只记录「是否已设置」）。
* 目标库名命中受保护 / 生产名单即拒绝；与 ``ops/restore.py`` 同口径，但本
  工具**不提供** ``--force-target`` 逃生口。
* 本工具不提供可用的 ``--allow-missing-*``：这三个开关会降级归档完整性
  要求，**仅供本地演练，禁止用于生产验收**（``docs/BACKUP-RESTORE.md`` §10
  第 9 项）。传入即非零退出。
* 证据文件不得落在仓库内（避免把含主机标签的取证记录提交进版本库）。
* 不引入任何新依赖：标准库 + 既有 ``ops/_common.py`` / ``ops/restore.py``。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 允许 `python3 ops/offsite_restore.py` 直接运行
    sys.path.insert(0, str(REPO_ROOT))

from ops._common import (  # noqa: E402
    ToolError,
    pg_dump_version,
    utc_now,
    utc_stamp,
    write_json,
)
from ops.restore import (  # noqa: E402
    PROTECTED_DB_NAMES as RESTORE_PROTECTED_DB_NAMES,
    Check,
    check_checksum,
    check_member_hashes,
    check_manifest,
    extract_archive,
    restore_orchestration,
    restore_storage,
)

#: dry-run 边界声明：打印、帮助文本与证据文件共用同一句话。
DRY_RUN_ONLY = "仅供本地演练，禁止用于生产验收"

#: ``--allow-missing-*`` 的统一拒绝理由（帮助文本与运行期错误共用）。
ALLOW_MISSING_PROHIBITION = (
    "该开关会降级归档完整性要求，" + DRY_RUN_ONLY
    + "；跨主机/异地恢复验收必须使用完整归档（docs/BACKUP-RESTORE.md §10 第 9 项）"
)

#: 帮助文本尾注（根命令与子命令共用）。
HELP_EPILOG = f"""\
安全边界
  * 本工具不建立任何网络连接：--target-host 只是取证标签，不解析、不连接。
  * 数据库恢复步骤不执行，只输出目标机计划命令（见 docs/BACKUP-RESTORE.md §11）。
  * --allow-missing-checksum / --allow-missing-storage /
    --allow-missing-orchestration：{ALLOW_MISSING_PROHIBITION}。

退出码
  0 校验通过（dry-run）  1 校验项不符  2 用法/声明/环境错误（fail closed）
"""

#: 绝不允许作为目标库的名字：``ops/restore.py`` 的保护名单 + 生产库名。
PROTECTED_DB_NAMES: frozenset[str] = frozenset(RESTORE_PROTECTED_DB_NAMES) | frozenset(
    {"ajaimgr", "ajaimgr_prod", "ajaimgr_production", "ajaimgr_live"}
)

#: 库名里出现这些片段即视为生产库（fail closed，宁可拒绝）。
PRODUCTION_NAME_HINTS: tuple[str, ...] = ("prod", "prd", "live", "master", "primary")

#: 目标机 PostgreSQL 主版本必须显式声明（无法从单机推断）。
PG_MAJOR_MIN = 10
PG_MAJOR_MAX = 99

#: 未加引号的 PostgreSQL 标识符；库名必须是这个形状。
DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: 主机标签：只做记录，不接受端口/账号（避免把 `host:port` 拼进目标机命令）。
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: ``<target-host>`` 这类文档占位符：运行时必须已替换。
PLACEHOLDER_RE = re.compile(r"^<[^<>]+>$")

#: 连接目标与账号只从环境变量读取（口令由 libpq 自行读取，本工具从不读）。
REQUIRED_ENV: tuple[str, ...] = ("PGHOST", "PGUSER")


def _inside_repository(path: Path) -> bool:
    return path == REPO_ROOT or REPO_ROOT in path.parents


def _resolve(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


# --- 参数声明 -----------------------------------------------------------------


@dataclass(frozen=True)
class Declaration:
    """操作者显式声明的目标环境（不含任何凭据）。"""

    archive: Path
    target_host: str
    target_db: str
    target_dir: Path
    target_pg_major: int
    evidence: Path | None = None
    verbose: bool = False

    @property
    def sidecar(self) -> Path:
        return self.archive.with_name(self.archive.name + ".sha256")


def _declaration_problems(args: argparse.Namespace, environ) -> list[str]:
    """收集全部声明级问题（不短路，便于一次改完）。"""
    problems: list[str] = []

    for flag in (
        "allow_missing_checksum",
        "allow_missing_storage",
        "allow_missing_orchestration",
    ):
        if getattr(args, flag, False):
            problems.append(f"--{flag.replace('_', '-')} is not available: {ALLOW_MISSING_PROHIBITION}")

    required = (
        ("--archive", args.archive),
        ("--target-host", args.target_host),
        ("--target-db", args.target_db),
        ("--target-dir", args.target_dir),
        ("--target-pg-major", args.target_pg_major),
    )
    missing = [flag for flag, value in required if value is None or value == ""]
    if missing:
        problems.append(
            "missing required parameter(s): "
            + ", ".join(missing)
            + "；目标库与目标目录必须由操作者显式声明，本工具不提供默认值"
        )

    for flag, value in (
        ("--target-host", args.target_host),
        ("--target-db", args.target_db),
        ("--target-dir", args.target_dir),
    ):
        if isinstance(value, str) and PLACEHOLDER_RE.match(value.strip()):
            problems.append(
                f"{flag} is still the documentation placeholder {value.strip()!r}; "
                "replace it with the real value before running (仓库内不得提交真实主机名/库名/IP)"
            )

    if args.target_host:
        label = args.target_host.strip()
        if not HOST_LABEL_RE.match(label):
            problems.append(
                f"--target-host must be a plain host label (letters/digits/._-), got {label!r}; "
                "不要带端口或账号，它只作为证据标签记录，永不解析、永不连接"
            )

    if args.target_db:
        name = args.target_db.strip()
        if any(token in name for token in ("://", "@", "/", "?")):
            problems.append(
                f"--target-db must be a bare database name, got {name!r}; "
                "连接串与口令只能来自 PG* 环境变量，禁止出现在命令行"
            )
        elif not DB_NAME_RE.match(name):
            problems.append(
                f"--target-db must match ^[a-z][a-z0-9_]{{0,62}}$ (lowercase, unquoted), got {name!r}"
            )
        elif name in PROTECTED_DB_NAMES:
            problems.append(
                f"refusing protected database name {name!r}; 该库是本机真实库/模板库，"
                "跨主机演练必须使用新的 *_restore_check 库（本工具没有 --force-target）"
            )
        else:
            hints = [hint for hint in PRODUCTION_NAME_HINTS if hint in name]
            if hints:
                problems.append(
                    f"refusing production-looking database name {name!r} "
                    f"(contains {', '.join(hints)}); use a dedicated drill database"
                )

    pg_database = (environ.get("PGDATABASE") or "").strip()
    if pg_database and args.target_db and pg_database != args.target_db.strip():
        problems.append(
            f"conflicting target: environment PGDATABASE={pg_database!r} != "
            f"--target-db {args.target_db.strip()!r}; unset PGDATABASE or align the two"
        )

    missing_env = [key for key in REQUIRED_ENV if not (environ.get(key) or "").strip()]
    if missing_env:
        problems.append(
            "missing environment variable(s): "
            + ", ".join(missing_env)
            + "；连接目标与账号只从环境变量读取（本工具只检查是否设置，不读取、"
            "不回显、不落盘其值）；口令由 libpq 经 PGPASSWORD / PGPASSFILE / 证书读取"
        )

    if args.archive:
        archive = _resolve(args.archive)
        if not archive.is_file():
            problems.append(f"--archive not found or not a regular file: {archive}")
        elif _inside_repository(archive):
            problems.append(
                f"--archive must not live inside the repository: {archive}; "
                "归档与证据都不得提交进版本库"
            )

    if args.target_dir:
        target_dir = _resolve(args.target_dir)
        if _inside_repository(target_dir):
            problems.append(
                f"--target-dir must not be inside the repository: {target_dir}"
            )
        elif not target_dir.exists():
            problems.append(
                f"--target-dir does not exist: {target_dir}; 请先在目标机创建该目录并把"
                "属主/权限授予执行账户（本工具不自动创建目录）"
            )
        elif not target_dir.is_dir():
            problems.append(f"--target-dir is not a directory: {target_dir}")
        else:
            try:
                entries = sorted(entry.name for entry in target_dir.iterdir())
            except OSError as exc:
                problems.append(f"--target-dir cannot be listed: {target_dir} ({exc})")
            else:
                if entries:
                    shown = ", ".join(entries[:5]) + (" ..." if len(entries) > 5 else "")
                    problems.append(
                        f"--target-dir must be empty: {target_dir} holds "
                        f"{len(entries)} entry(ies) ({shown}); 恢复产物不得覆盖既有文件"
                    )
            if not os.access(target_dir, os.W_OK):
                problems.append(
                    f"--target-dir is not writable by the current user: {target_dir}"
                )

    if args.target_pg_major is not None and not (
        PG_MAJOR_MIN <= int(args.target_pg_major) <= PG_MAJOR_MAX
    ):
        problems.append(
            f"--target-pg-major looks wrong: {args.target_pg_major} "
            f"(expected {PG_MAJOR_MIN}..{PG_MAJOR_MAX})"
        )

    if args.evidence:
        evidence = _resolve(args.evidence)
        if _inside_repository(evidence):
            problems.append(
                f"--evidence must not be written inside the repository: {evidence}; "
                "证据文件留在目标机，不得提交进版本库"
            )

    return problems


def validate_declaration(args: argparse.Namespace, environ) -> Declaration:
    """把 CLI + 环境收敛成 :class:`Declaration`；任何问题都 fail closed。"""
    problems = _declaration_problems(args, environ)
    if problems:
        raise ToolError(
            "offsite restore declaration rejected (fail closed):\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
    return Declaration(
        archive=_resolve(args.archive),
        target_host=args.target_host.strip(),
        target_db=args.target_db.strip(),
        target_dir=_resolve(args.target_dir),
        target_pg_major=int(args.target_pg_major),
        evidence=_resolve(args.evidence) if args.evidence else None,
        verbose=bool(args.verbose),
    )


# --- 主版本 -------------------------------------------------------------------


def version_major(text: str | None) -> int | None:
    """从 ``pg_dump (PostgreSQL) 17.11`` 提取主版本；无法识别返回 ``None``。"""
    if not text:
        return None
    match = re.search(r"PostgreSQL\)?\s*(\d+)", text) or re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def local_pg_dump_version() -> str | None:
    """本机 ``pg_dump --version``（可能不存在；仅用于取证与告警）。"""
    try:
        return pg_dump_version()
    except ToolError:
        return None


def check_pg_version(
    manifest: dict, target_major: int, local_version: str | None
) -> tuple[list[Check], list[str]]:
    """归档 ``pg_dump`` 主版本 vs 目标机主版本。

    ``pg_dump`` 的输出可以被**同主版本或更新**的服务器加载，但不能被更旧的
    主版本加载；因此 ``target < archive`` 直接判失败，``target > archive``
    只告警（跨主版本升级路径需要在目标机另行验证）。
    """
    warnings: list[str] = []
    dump_version = manifest.get("pg_dump_version")
    dump_major = version_major(dump_version)
    if dump_major is None:
        return (
            [
                Check(
                    "pg_target_major",
                    False,
                    f"manifest pg_dump_version is missing or unparsable: {dump_version!r}",
                    diffs=[
                        "归档未记录可识别的 pg_dump 版本，无法校验目标机主版本兼容性；"
                        "请用 ops/backup.py 重新生成归档"
                    ],
                )
            ],
            warnings,
        )

    ok = target_major >= dump_major
    diffs: list[str] = []
    if not ok:
        diffs.append(
            f"目标机 PostgreSQL 主版本 {target_major} 低于归档 pg_dump 主版本 "
            f"{dump_major}（{dump_version}）：pg_dump 输出不能被更旧的主版本加载。"
            f"请在目标机安装 PostgreSQL {dump_major} 或更新的客户端，"
            f"或在目标机用同主版本 pg_dump 重新导出归档"
        )
    elif target_major > dump_major:
        warnings.append(
            f"target PostgreSQL major {target_major} > archive pg_dump major {dump_major}: "
            "跨主版本升级恢复路径，需在目标机额外验证扩展/排序规则差异"
        )

    local_major = version_major(local_version)
    if local_major is not None and local_major != target_major:
        warnings.append(
            f"本机 pg_dump 主版本 {local_major} 与声明的目标机主版本 {target_major} 不同："
            "本次 dry-run 不能代表目标机的客户端行为"
        )
    elif local_major is None:
        warnings.append(
            "本机没有可用的 pg_dump：dry-run 未覆盖客户端版本，目标机须自行执行 "
            "`psql --version` 并核对主版本"
        )

    return (
        [
            Check(
                "pg_target_major",
                ok,
                f"archive pg_dump={dump_major} ({dump_version}), "
                f"target declared={target_major}, local client={local_version or 'unavailable'}",
                diffs=diffs,
            )
        ],
        warnings,
    )


# --- 预检 ---------------------------------------------------------------------


def run_preflight(decl: Declaration) -> tuple[list[Check], dict]:
    """只读预检：不向 ``--target-dir`` 写入任何文件（解包到系统临时目录）。"""
    checks: list[Check] = []
    info: dict = {"archive_sha256": None, "manifest": {}, "warnings": []}

    checksum_check, digest = check_checksum(decl.archive, allow_missing=False)
    checks.append(checksum_check)
    info["archive_sha256"] = digest
    if not checksum_check.ok:
        return checks, info

    manifest: dict = {}
    with tempfile.TemporaryDirectory(prefix="ajaimgr-offsite-preflight-") as tmp:
        extracted = Path(tmp) / "extracted"
        extract_check, _ = extract_archive(decl.archive, extracted)
        checks.append(extract_check)
        if extract_check.ok:
            manifest_check, manifest = check_manifest(extracted)
            checks.append(manifest_check)
            if manifest:
                member_check, _ = check_member_hashes(extracted, manifest)
                checks.append(member_check)
                # 归档必须是完整的：--allow-missing-* 生成的三缺一归档不得进入验收。
                missing_components = manifest.get("missing_components") or []
                checks.append(
                    Check(
                        "manifest_completeness",
                        not missing_components,
                        f"missing_components={missing_components or 'none'}",
                        diffs=[
                            f"归档缺少必需组成部分 {name!r}（生成时用了 "
                            f"--allow-missing-{name}）；本工具{DRY_RUN_ONLY}，"
                            "该归档不能作为生产验收输入"
                            for name in missing_components
                        ],
                    )
                )

    info["manifest"] = manifest
    if not manifest:
        return checks, info
    version_checks, warnings = check_pg_version(
        manifest, decl.target_pg_major, local_pg_dump_version()
    )
    checks.extend(version_checks)
    info["warnings"] = warnings
    return checks, info


# --- 本机 dry-run 演练 ---------------------------------------------------------


def run_rehearse(decl: Declaration) -> tuple[list[Check], dict, dict]:
    """在 ``--target-dir`` 内复现目标机落盘步骤（数据库步骤不执行）。"""
    checks, info = run_preflight(decl)
    if not all(check.ok for check in checks):
        # 预检未通过：目标目录保持原样（仍然为空），不产生任何半成品。
        return checks, info, {}

    manifest = info["manifest"]
    artifacts: dict = {"incoming": None, "storage": None, "orchestration": None}

    incoming = decl.target_dir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    copied = incoming / decl.archive.name
    shutil.copy2(decl.archive, copied)
    shutil.copy2(decl.sidecar, copied.with_name(copied.name + ".sha256"))
    artifacts["incoming"] = str(copied)

    transfer_check, copied_digest = check_checksum(copied, allow_missing=False)
    checks.append(
        Check(
            "transfer_checksum",
            transfer_check.ok and copied_digest == info["archive_sha256"],
            f"copied to {copied}; {transfer_check.detail}",
            diffs=(
                transfer_check.diffs
                or (
                    []
                    if copied_digest == info["archive_sha256"]
                    else [
                        f"transferred copy sha256 {copied_digest} != source "
                        f"{info['archive_sha256']}"
                    ]
                )
            ),
        )
    )
    if not checks[-1].ok:
        return checks, info, artifacts

    extracted = decl.target_dir / "work" / "extracted"
    extract_check, _ = extract_archive(copied, extracted)
    checks.append(
        Check(
            "rehearsal_extract",
            extract_check.ok,
            f"archive copied into the target directory unpacked: {extract_check.detail}",
            diffs=extract_check.diffs,
        )
    )
    if not extract_check.ok:
        return checks, info, artifacts

    storage_target = decl.target_dir / "restored-storage"
    storage_checks, storage_info = restore_storage(manifest, extracted, storage_target)
    checks.extend(storage_checks)
    artifacts["storage"] = str(storage_target)

    orchestration_target = decl.target_dir / "restored-orchestration"
    orch_checks, orch_info = restore_orchestration(
        manifest, extracted, orchestration_target
    )
    checks.extend(orch_checks)
    artifacts["orchestration"] = str(orchestration_target)

    # 数据库步骤：dry-run 明确不执行（不连接任何数据库，包括本机）。
    checks.append(
        Check(
            "db_restore",
            True,
            "NOT EXECUTED: dry-run 不执行数据库恢复；目标机按 plan 第 4 步执行 ops/restore.py",
            skipped=True,
        )
    )
    info["storage"] = storage_info
    info["orchestration"] = orch_info
    return checks, info, artifacts


# --- 计划（在目标机执行，本工具不执行） -----------------------------------------


def build_plan(decl: Declaration) -> list[dict]:
    """目标机操作计划：只渲染命令文本，绝不执行。

    命令按目标机的 POSIX 路径渲染（``as_posix``）——本机若是 Windows 演练环境，
    也不会把反斜杠混进给目标机的命令。
    """
    archive = decl.archive.as_posix()
    archive_name = decl.archive.name
    target_dir = decl.target_dir.as_posix()
    incoming = f"{target_dir}/incoming/{archive_name}"
    report = f"{target_dir}/evidence/restore-report.json"
    return [
        {
            "step": 1,
            "phase": "transfer",
            "command": (
                f"rsync -a --checksum {archive} "
                f"<account>@{decl.target_host}:{target_dir}/incoming/"
            ),
            "note": "归档与 <归档>.sha256 旁车必须一起传输；传输账户由 DBA 指定。",
        },
        {
            "step": 2,
            "phase": "verify-transfer",
            "command": f"cd {target_dir}/incoming && sha256sum -c {archive_name}.sha256",
            "note": "在目标机本地复核传输完整性；不通过则停止，重新传输。",
        },
        {
            "step": 3,
            "phase": "create-database",
            "command": f"sudo -u postgres createdb -O <app-role> {decl.target_db}",
            "note": (
                "先确认目标库不存在（本工具不执行 DROP）；<app-role> 由 DBA 填写，"
                "禁止在本命令里出现口令。"
            ),
        },
        {
            "step": 4,
            "phase": "restore",
            "command": (
                f"uv run python ops/restore.py {incoming} "
                f"--target-db-url postgresql+psycopg:///{decl.target_db} "
                f"--storage-target {target_dir}/run/storage "
                f"--orchestration-target {target_dir}/run/orchestration "
                f"--report {report} --verbose"
            ),
            "note": (
                "连接目标与账号由 PG* 环境变量提供；禁止追加 --allow-missing-*，"
                "禁止 --recreate-db（目标库必须是新建的演练库）。"
            ),
        },
        {
            "step": 5,
            "phase": "verify",
            "command": "核对 ops/restore.py 的 13 项校验与 §11.4 校验清单",
            "note": "任一项 FAIL 即按 §11.5 回滚；不得以 SKIP 项充当通过。",
        },
        {
            "step": 6,
            "phase": "switch-service",
            "command": "按 §11.3 第 6 步把服务指向新库/新目录后重启（必须先备份现网配置）",
            "note": "切换前冻结写入；切换失败按 §11.5 回滚到原库/原目录。",
        },
        {
            "step": 7,
            "phase": "evidence",
            "command": f"把本证据 JSON、restore-report.json 与命令输出归档到 {target_dir}/evidence/",
            "note": "证据留在目标机，复制一份到受控存储；不得提交进仓库。",
        },
    ]


# --- 证据 ---------------------------------------------------------------------


def build_evidence(
    decl: Declaration,
    *,
    mode: str,
    checks: list[Check],
    info: dict,
    artifacts: dict,
    started_at: str,
) -> dict:
    """证据 JSON：不含任何凭据，也不记录环境变量的值。"""
    database_step = "NOT_EXECUTED" if mode == "rehearse" else "PLANNED"
    return {
        "tool": "ops/offsite_restore.py",
        "mode": mode,
        "scope": "dry-run/local",
        "dry_run_only": True,
        "started_at": started_at,
        "finished_at": utc_now().isoformat(),
        "result": "PASS" if all(check.ok for check in checks) else "FAIL",
        "archive": {
            "path": str(decl.archive),
            "name": decl.archive.name,
            "sha256": info.get("archive_sha256"),
            "size_bytes": decl.archive.stat().st_size,
            "sidecar": str(decl.sidecar),
        },
        "target": {
            "host_label": decl.target_host,
            "database": decl.target_db,
            "directory": str(decl.target_dir),
            "pg_major_declared": decl.target_pg_major,
        },
        "environment": {
            "required_keys": list(REQUIRED_ENV),
            "present_keys": [key for key in REQUIRED_ENV if os.environ.get(key)],
            "values_recorded": False,
        },
        "database_step": database_step,
        "checks": [check.as_dict() for check in checks],
        "storage": info.get("storage", {}),
        "orchestration": info.get("orchestration", {}),
        "artifacts": artifacts,
        "plan": build_plan(decl),
        "warnings": info.get("warnings", []),
        "notes": [
            "本记录来自单机 dry-run：未建立任何网络连接，未执行真实跨主机/异地恢复。",
            "数据库恢复步骤由操作者在目标机按 plan 执行 ops/restore.py，本工具不执行。",
            "环境变量只记录是否设置，不记录取值；PGPASSWORD 由 libpq 读取，本工具从不读取。",
            f"--allow-missing-* {DRY_RUN_ONLY}（docs/BACKUP-RESTORE.md §10 第 9 项）。",
        ],
    }


# --- 输出 ---------------------------------------------------------------------


def render_report(decl: Declaration, mode: str, checks: list[Check], info: dict) -> None:
    print("== AJAIMGR offsite restore (dry-run / local only) ==")
    print(f"mode           : {mode}")
    print(f"archive        : {decl.archive}")
    print(f"archive sha256 : {info.get('archive_sha256')}")
    print(f"target host    : {decl.target_host}  (取证标签；不解析、不连接)")
    print(f"target database: {decl.target_db}")
    print(f"target dir     : {decl.target_dir}")
    print(f"target pg major: {decl.target_pg_major}")
    print()
    print(f"{'CHECK':<26} {'RESULT':<7} DETAIL")
    for check in checks:
        print(f"{check.name:<26} {check.status:<7} {check.detail}")

    failures = [check for check in checks if not check.ok and not check.skipped]
    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} check(s) failed)")
        for check in failures:
            print(f"- {check.name}: {check.detail}")
            for diff in check.diffs:
                print(f"    {diff}")
        return

    print("PLAN（在目标机执行，本工具不执行）")
    for step in build_plan(decl):
        print(f"  {step['step']}. {step['phase']:<15} {step['command']}")
        if decl.verbose:
            print(f"     note: {step['note']}")
    for warning in info.get("warnings", []):
        print(f"WARNING: {warning}")
    print()
    print(
        f"RESULT: DRY-RUN PASS ({len(checks)} check(s)); "
        f"database step NOT EXECUTED; {DRY_RUN_ONLY}"
    )


# --- CLI ----------------------------------------------------------------------


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--archive",
        default=None,
        required=False,
        help="已传输到目标机的归档路径（必须与 .sha256 旁车同目录；不得位于仓库内）",
    )
    parser.add_argument(
        "--target-host",
        default=None,
        required=False,
        help="目标机标签，仅写入证据；不解析、不连接、不执行 ssh/scp",
    )
    parser.add_argument(
        "--target-db",
        default=None,
        required=False,
        help="目标库裸名（如 ajaimgr_restore_check）；受保护/生产名一律拒绝",
    )
    parser.add_argument(
        "--target-dir",
        default=None,
        required=False,
        help="目标机上已存在且**为空**的目录（恢复产物写入此处；本工具不自动创建）",
    )
    parser.add_argument(
        "--target-pg-major",
        type=int,
        default=None,
        required=False,
        help="目标机 PostgreSQL 服务器主版本（如 17）；必须显式声明，无法从单机推断",
    )
    parser.add_argument(
        "--evidence",
        default=None,
        help="证据 JSON 输出路径（默认 preflight 不写、rehearse 写 <target-dir>/evidence/）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出计划备注与逐项细节")
    # 三个开关故意「可解析但一律拒绝」：让误用得到可操作的解释，而不是 argparse 的
    # unrecognized arguments。帮助文本同时写明禁止用于生产验收。
    for flag in (
        "allow-missing-checksum",
        "allow-missing-storage",
        "allow-missing-orchestration",
    ):
        parser.add_argument(
            f"--{flag}",
            action="store_true",
            help=f"[拒绝] {DRY_RUN_ONLY}；本工具不提供可用放行开关，传入即非零退出",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops/offsite_restore.py",
        description=(
            "AJAIMGR 跨主机/异地恢复的预检 + 本机 dry-run 封装。"
            "不连接任何异地主机、不执行真实跨主机恢复；数据库恢复只输出计划命令。"
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{preflight,rehearse}")

    preflight = subparsers.add_parser(
        "preflight",
        help="只读预检：参数、归档完整性、目标目录、PG 主版本（不写目标目录）",
        description="只读预检：参数、归档完整性、目标目录、PG 主版本（不写目标目录）。",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(preflight)

    rehearse = subparsers.add_parser(
        "rehearse",
        help="本机 dry-run：传输模拟 + 附件/任务库落盘校验 + 证据（数据库不执行）",
        description=(
            "本机 dry-run：传输模拟 + 附件/任务库落盘校验 + 证据留存；"
            "数据库恢复步骤不执行。"
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(rehearse)
    return parser


def _write_evidence(path: Path, payload: dict) -> None:
    write_json(path, payload)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = utc_now().isoformat()
    try:
        decl = validate_declaration(args, os.environ)
        if args.command == "preflight":
            checks, info = run_preflight(decl)
            evidence = build_evidence(
                decl,
                mode="preflight",
                checks=checks,
                info=info,
                artifacts={},
                started_at=started_at,
            )
            render_report(decl, "preflight", checks, info)
            if args.evidence:
                _write_evidence(decl.evidence, evidence)
            return 0 if all(check.ok for check in checks) else 1

        checks, info, artifacts = run_rehearse(decl)
        evidence = build_evidence(
            decl,
            mode="rehearse",
            checks=checks,
            info=info,
            artifacts=artifacts,
            started_at=started_at,
        )
        render_report(decl, "rehearse", checks, info)
        if all(check.ok for check in checks):
            evidence_path = decl.evidence or (
                decl.target_dir / "evidence" / f"offsite-restore-{utc_stamp()}.json"
            )
            _write_evidence(evidence_path, evidence)
            print(f"EVIDENCE       : {evidence_path}")
        elif args.evidence:
            _write_evidence(decl.evidence, evidence)
            print(f"EVIDENCE       : {decl.evidence}")
        return 0 if all(check.ok for check in checks) else 1
    except ToolError as exc:
        print(f"OFFSITE RESTORE FAILED: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"OFFSITE RESTORE FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
