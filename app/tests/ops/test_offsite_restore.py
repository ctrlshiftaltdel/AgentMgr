"""Issue #22 跨主机/异地恢复封装测试（``ops/offsite_restore.py``）。

覆盖验收点，全部**不依赖真实隔地主机、不连接任何数据库、不执行 ssh**：

1. 缺参 fail closed（``--target-db`` / ``--target-dir`` / ``--target-pg-major`` 等）；
2. 目标库为受保护/生产名（``postgres`` / ``ajaimgr_dev`` / ``ajaimgr_prod``）即拒绝；
3. 目标库必须给裸名（DSN 出现在命令行即拒绝：凭据只能来自 ``PG*`` 环境变量）；
4. 目标目录不存在 / 非空即拒绝，且「预检失败不写目标目录」；
5. 目标机 PG 主版本低于归档 ``pg_dump`` 主版本时给出明确提示（退出码 1）；
6. 缺少 ``PGHOST`` / ``PGUSER`` 即拒绝（本工具不读取、不回显其值）；
7. ``--allow-missing-*`` 传入即拒绝，``--help`` 文本写明禁止用于生产验收；
8. 本机 dry-run 成功路径：临时目录里模拟传输 + 附件/任务库落盘校验 + 证据留存，
   数据库步骤记 ``NOT_EXECUTED``；
9. 证据文件不含口令/环境变量取值；归档被篡改时在写目标目录之前停止。

归档由本测试用 ``tarfile`` 直接构造（manifest 结构对齐 ``ops/backup.py``），
因此不需要 PostgreSQL 与 ``pg_dump``；``db/database.sql`` 内容仅为占位，
dry-run 明确不执行数据库恢复，绝不会被 ``psql`` 消费。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OFFSITE_PY = REPO_ROOT / "ops" / "offsite_restore.py"

#: 目标机上「连接目标/账号来自环境变量」的最小集合（值本身不参与任何连接）。
BASE_ENV = {
    "PGHOST": "target-host",
    "PGUSER": "restore-drill",
}

#: 与本工具无关的连接类环境变量：测试里一律清掉，避免宿主机环境污染结论。
SCRUBBED_ENV = ("PGDATABASE", "PGPASSWORD", "PGPASSFILE", "PGSSLCERT", "PGSSLKEY")


# --- 进程助手 -----------------------------------------------------------------


def run(
    *args: str,
    env: dict[str, str] | None = None,
    drop_env: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """用当前解释器运行工具，返回真实退出码与输出。"""
    merged = dict(os.environ)
    for key in (*SCRUBBED_ENV, *drop_env):
        merged.pop(key, None)
    merged.update(BASE_ENV)
    merged.update(env or {})
    for key in drop_env:
        merged.pop(key, None)
    return subprocess.run(
        [sys.executable, str(OFFSITE_PY), *args],
        cwd=str(REPO_ROOT),
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def decl_args(archive: Path, target_dir: Path, *extra: str, db: str = "ajaimgr_restore_check") -> list[str]:
    return [
        "--archive",
        str(archive),
        "--target-host",
        "target-host",
        "--target-db",
        db,
        "--target-dir",
        str(target_dir),
        "--target-pg-major",
        "17",
        *extra,
    ]


# --- 归档构造 -----------------------------------------------------------------


def _state_sqlite(path: Path) -> tuple[bytes, int]:
    """造一个真实的小任务库，返回 (bytes, 表数)。"""
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT);
            INSERT INTO tasks (id, status) VALUES ('22', 'DISPATCHED');
            INSERT INTO events (note) VALUES ('dispatch'), ('verify');
            """
        )
        connection.commit()
        tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
    finally:
        connection.close()
    return path.read_bytes(), int(tables)


def build_archive(
    base: Path,
    *,
    name: str = "offsite-drill.tar.gz",
    pg_dump_version: str = "pg_dump (PostgreSQL) 17.11",
    missing_components: tuple[str, ...] = (),
) -> Path:
    """构造一个结构合法的归档（manifest + dump 占位 + 附件 + 任务库）。"""
    base.mkdir(parents=True, exist_ok=True)
    state_bytes, table_count = _state_sqlite(base / "state.sqlite3")
    config_bytes = json.dumps({"retry_cap": 3}).encode("utf-8") + b"\n"
    dump_bytes = b"-- dry-run fixture: this dump is never executed\nSELECT 1;\n"

    members: dict[str, bytes] = {
        "db/database.sql": dump_bytes,
        "orchestration/state.sqlite3": state_bytes,
        "orchestration/config.json": config_bytes,
    }
    storage_files: list[dict] = []
    for content in (b"offsite-dry-run attachment\n", b"second attachment\n"):
        digest = hashlib.sha256(content).hexdigest()
        path = f"storage/{digest[:2]}/{digest[2:4]}/{digest}"
        members[path] = content
        storage_files.append(
            {"path": path, "sha256": digest, "size_bytes": len(content), "kind": "storage"}
        )

    member_rows = [
        {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "kind": "database" if path.startswith("db/") else (
                "storage" if path.startswith("storage/") else "orchestration"
            ),
        }
        for path, data in sorted(members.items())
    ]
    manifest = {
        "format": "ajaimgr-backup",
        "format_version": 1,
        "missing_components": list(missing_components),
        "created_at": "2026-10-18T00:00:00+00:00",
        "tool": "ops/backup.py",
        "label": "issue-22-fixture",
        "host": "backup-host",
        "python_version": "3.13.0",
        "pg_dump_version": pg_dump_version,
        "database": {
            "dsn_redacted": "postgresql:///ajaimgr_dev (password omitted)",
            "database": "ajaimgr_dev",
            "dump_path": "db/database.sql",
            "dump_sha256": hashlib.sha256(dump_bytes).hexdigest(),
            "dump_size_bytes": len(dump_bytes),
            "table_names": ["companies", "employees"],
            "row_counts": {"companies": 1, "employees": 2},
            "critical_tables": ["companies", "employees"],
            "missing_critical_tables": [],
        },
        "storage": {"file_count": len(storage_files), "files": storage_files},
        "orchestration": {
            "source_dir": "/srv/ajaimgr/.orchestration",
            "file_count": 2,
            "files": [
                row for row in member_rows if row["path"].startswith("orchestration/")
            ],
            "state_table_count": table_count,
            "state_integrity": "ok",
        },
        "members": member_rows,
        "excluded_credential_files": [],
        "warnings": [],
        "notes": [],
    }
    members["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")

    archive = base / name
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(members):
            data = members[path]
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    refresh_sidecar(archive)
    return archive


def refresh_sidecar(archive: Path) -> None:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )


def rewrite_archive(source: Path, destination: Path, mutate) -> Path:
    """读出现有归档成员、交给 ``mutate`` 修改后再写回（用于篡改用例）。"""
    with tarfile.open(source, "r:gz") as tar:
        members = {
            member.name: tar.extractfile(member).read()
            for member in tar.getmembers()
            if member.isfile()
        }
    mutate(members)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as tar:
        for name in sorted(members):
            data = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    refresh_sidecar(destination)
    return destination


@pytest.fixture
def archive(tmp_path) -> Path:
    return build_archive(tmp_path / "fixture")


@pytest.fixture
def target_dir(tmp_path) -> Path:
    path = tmp_path / "target"
    path.mkdir()
    return path


# --- 1. 缺参 fail closed --------------------------------------------------------


def test_missing_required_parameters_fail_closed(archive):
    """只给 --archive：必须一次性列出全部缺失参数并退出码 2。"""
    result = run("preflight", "--archive", str(archive))
    assert result.returncode == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    for flag in ("--target-host", "--target-db", "--target-dir", "--target-pg-major"):
        assert flag in combined, flag
    assert "本工具不提供默认值" in combined


def test_missing_parameter_message_is_actionable(archive, target_dir):
    """缺 --target-dir 时其余参数合法也必须失败（不落默认目录）。"""
    args = decl_args(archive, target_dir)
    index = args.index("--target-dir")
    del args[index : index + 2]
    result = run("preflight", *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--target-dir" in (result.stdout + result.stderr)


# --- 2. 受保护 / 生产库名 -------------------------------------------------------


@pytest.mark.parametrize(
    "db",
    ["postgres", "template1", "ajaimgr_dev", "ajaimgr_test", "ajaimgr_prod"],
)
def test_protected_or_production_database_is_refused(archive, target_dir, db):
    result = run("preflight", *decl_args(archive, target_dir, db=db))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing" in result.stderr
    assert db in result.stderr
    # 该工具没有 --force-target 逃生口，错误里必须说明。
    assert "--force-target" in result.stderr


# --- 3. 目标库必须是裸名 --------------------------------------------------------


def test_target_database_must_be_a_bare_name(archive, target_dir):
    """DSN 出现在命令行即拒绝：连接串与口令只能来自环境变量。"""
    result = run(
        "preflight",
        *decl_args(archive, target_dir, db="postgresql://restore@target-host/ajaimgr"),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "bare database name" in result.stderr
    assert "PG*" in result.stderr


# --- 4. 目标目录 ---------------------------------------------------------------


def test_missing_target_directory_is_refused(archive, tmp_path):
    missing = tmp_path / "no-such-target"
    assert not missing.exists()
    result = run("preflight", *decl_args(archive, missing))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "does not exist" in result.stderr
    assert str(missing.resolve()) in result.stderr
    assert "不自动创建" in result.stderr


def test_non_empty_target_directory_is_refused(archive, target_dir):
    (target_dir / "leftover.txt").write_text("previous run\n", encoding="utf-8")
    result = run("preflight", *decl_args(archive, target_dir))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "must be empty" in result.stderr
    assert "leftover.txt" in result.stderr


def test_placeholder_target_host_is_refused(archive, target_dir):
    """文档占位符 <target-host> 没替换时必须报错（防止模板被直接执行）。"""
    args = decl_args(archive, target_dir)
    args[args.index("--target-host") + 1] = "<target-host>"
    result = run("preflight", *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "placeholder" in result.stderr


# --- 5. PG 主版本 --------------------------------------------------------------


def test_pg_major_older_than_archive_is_reported(tmp_path):
    """目标机 13 < 归档 pg_dump 17 → 校验 FAIL（退出码 1）并给出可操作提示。"""
    archive = build_archive(tmp_path / "fixture")
    target = tmp_path / "target"
    target.mkdir()
    args = decl_args(archive, target)
    args[args.index("--target-pg-major") + 1] = "13"
    result = run("preflight", *args)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pg_target_major" in result.stdout
    assert "17" in result.stdout and "13" in result.stdout
    assert "低于归档 pg_dump 主版本" in result.stdout
    # 目标目录必须保持为空：预检失败不得留下任何产物。
    assert list(target.iterdir()) == []


def test_newer_target_major_only_warns(tmp_path):
    """目标机主版本高于归档 → 通过但记录告警（跨主版本升级路径）。"""
    archive = build_archive(tmp_path / "fixture")
    target = tmp_path / "target"
    target.mkdir()
    args = decl_args(archive, target)
    args[args.index("--target-pg-major") + 1] = "18"
    result = run("preflight", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "跨主版本升级恢复路径" in result.stdout


def test_unparsable_dump_version_fails_closed(tmp_path):
    archive = build_archive(tmp_path / "fixture", pg_dump_version="unknown")
    target = tmp_path / "target"
    target.mkdir()
    result = run("preflight", *decl_args(archive, target))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pg_dump_version" in result.stdout


def test_incomplete_archive_is_refused(tmp_path):
    """用 --allow-missing-* 生成的残缺归档不得作为验收输入。"""
    archive = build_archive(tmp_path / "fixture", missing_components=("storage",))
    target = tmp_path / "target"
    target.mkdir()
    result = run("preflight", *decl_args(archive, target))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "manifest_completeness" in result.stdout
    assert "--allow-missing-storage" in result.stdout
    assert "禁止用于生产验收" in result.stdout


# --- 6. 凭据只从环境变量 --------------------------------------------------------


def test_missing_pg_environment_fails_closed(archive, target_dir):
    result = run(
        "preflight",
        *decl_args(archive, target_dir),
        drop_env=("PGHOST", "PGUSER"),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "PGHOST" in result.stderr and "PGUSER" in result.stderr
    assert "不回显" in result.stderr


def test_pgdatabase_conflict_is_refused(archive, target_dir):
    result = run(
        "preflight",
        *decl_args(archive, target_dir),
        env={"PGDATABASE": "ajaimgr_dev"},
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "PGDATABASE" in result.stderr
    assert "conflicting target" in result.stderr


# --- 7. --allow-missing-* 禁止用于生产验收 --------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--allow-missing-checksum",
        "--allow-missing-storage",
        "--allow-missing-orchestration",
    ],
)
def test_allow_missing_switches_are_rejected(archive, target_dir, flag):
    result = run("preflight", *decl_args(archive, target_dir, flag))
    assert result.returncode == 2, result.stdout + result.stderr
    assert flag in result.stderr
    assert "禁止用于生产验收" in result.stderr


def test_help_text_states_the_prohibitions():
    for args in (("--help",), ("preflight", "--help"), ("rehearse", "--help")):
        result = run(*args)
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "禁止用于生产验收" in combined, args
        assert "不建立任何网络连接" in combined, args
        assert "allow-missing-checksum" in combined, args


# --- 8. 本机 dry-run 成功路径 ---------------------------------------------------


def test_dry_run_rehearsal_passes_locally(archive, target_dir, tmp_path):
    """本机临时目录演练：传输校验 + 附件/任务库落盘 + 证据，退出码 0。"""
    evidence_path = tmp_path / "evidence" / "rehearsal.json"
    result = run(
        "rehearse",
        *decl_args(archive, target_dir, "--evidence", str(evidence_path), "--verbose"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN PASS" in result.stdout
    assert "database step NOT EXECUTED" in result.stdout

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["result"] == "PASS"
    assert evidence["mode"] == "rehearse"
    assert evidence["scope"] == "dry-run/local"
    assert evidence["dry_run_only"] is True
    assert evidence["database_step"] == "NOT_EXECUTED"
    assert evidence["target"]["host_label"] == "target-host"
    assert evidence["target"]["database"] == "ajaimgr_restore_check"
    assert evidence["environment"]["values_recorded"] is False

    statuses = {check["name"]: check for check in evidence["checks"]}
    for name in (
        "archive_checksum",
        "archive_members",
        "manifest",
        "member_hashes",
        "manifest_completeness",
        "pg_target_major",
        "transfer_checksum",
        "rehearsal_extract",
        "storage_restore",
        "storage_hashes",
        "storage_content_address",
        "orchestration_restore",
        "orchestration_sqlite",
        "orchestration_config",
    ):
        assert statuses[name]["status"] == "PASS", (name, statuses[name])
    assert statuses["db_restore"]["status"] == "SKIP"

    # 传输模拟：归档与旁车确实复制进目标目录且散列一致。
    incoming = target_dir / "incoming" / archive.name
    assert incoming.is_file()
    assert hashlib.sha256(incoming.read_bytes()).hexdigest() == evidence["archive"]["sha256"]

    # 附件落盘在目标目录内，散列可独立复核。
    storage_target = target_dir / "restored-storage"
    for entry in evidence["storage"]["files"]:
        restored = storage_target / entry["path"].split("/", 1)[1]
        assert restored.is_file(), entry["path"]
        assert hashlib.sha256(restored.read_bytes()).hexdigest() == entry["sha256"]

    # 任务库落盘后可只读打开。
    state = target_dir / "restored-orchestration" / "state.sqlite3"
    connection = sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)
    try:
        tasks = connection.execute("SELECT count(*) FROM tasks").fetchone()[0]
    finally:
        connection.close()
    assert tasks == 1

    # 证据与计划都明确标注「不执行数据库恢复」。
    assert any("未执行真实跨主机" in note for note in evidence["notes"])
    phases = {step["phase"] for step in evidence["plan"]}
    assert {"transfer", "create-database", "restore", "evidence"} <= phases


def test_preflight_keeps_target_directory_untouched(archive, target_dir):
    result = run("preflight", *decl_args(archive, target_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(target_dir.iterdir()) == []


# --- 9. 证据不含凭据 / 篡改即停 -------------------------------------------------


def test_evidence_never_contains_the_password(archive, target_dir, tmp_path):
    sentinel = "NOT-A-REAL-PASSWORD-22"
    evidence_path = tmp_path / "evidence.json"
    result = run(
        "rehearse",
        *decl_args(archive, target_dir, "--evidence", str(evidence_path)),
        env={"PGPASSWORD": sentinel},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr
    assert sentinel not in evidence_path.read_text(encoding="utf-8")


def test_tampered_archive_stops_before_writing(archive, target_dir, tmp_path):
    """归档成员被改动（旁车已刷新）→ 预检 FAIL，目标目录保持为空。"""

    def mutate(members: dict[str, bytes]) -> None:
        blob = next(name for name in members if name.startswith("storage/"))
        data = bytearray(members[blob])
        data[0] ^= 0xFF
        members[blob] = bytes(data)

    tampered = rewrite_archive(archive, tmp_path / "tampered" / archive.name, mutate)
    result = run("rehearse", *decl_args(tampered, target_dir))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "member_hashes" in result.stdout
    assert list(target_dir.iterdir()) == []


def test_archive_inside_repository_is_refused():
    """归档不得位于仓库内（防止把归档/证据提交进版本库）。"""
    inside = REPO_ROOT / "var" / "issue-22-should-not-exist.tar.gz"
    assert not inside.exists()
    target = REPO_ROOT / "var" / "issue-22-should-not-exist-target"
    assert not target.exists()
    result = run(
        "preflight",
        "--archive",
        str(inside),
        "--target-host",
        "target-host",
        "--target-db",
        "ajaimgr_restore_check",
        "--target-dir",
        str(target),
        "--target-pg-major",
        "17",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "inside the repository" in result.stderr
