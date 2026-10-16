"""W1-25 备份/恢复工具测试：真实 pg_dump + 真实 PostgreSQL 恢复演练。

覆盖任务固定的验收点（对齐 ``docs/DEPLOYMENT-REQUIREMENTS.md`` §5 与
``docs/BACKUP-RESTORE.md``）：

1. **完整往返**：备份 → 恢复到干净目标库 → 关键表行数与附件 sha256 全部一致；
2. **篡改检测**：改动归档中某个附件字节后恢复，必须非零退出码并指出该项；
3. **行数不符检测**：manifest 中的行数与实际不符时报错（非零退出码）；
4. **凭据不入归档**：待备份目录里放 ``.env`` / ``auth.json`` / ``server.key``，
   断言归档中不含它们（并被记入 manifest 的排除清单）；
5. **归档校验和**：被篡改的归档被拒绝（``<归档>.sha256`` 旁车比对失败）；
6. **缺失组成部分守卫**：必需组成部分（附件存储根 / 任务库目录）缺失且未给
   ``--allow-missing-*`` 开关时非零退出（``ToolError`` → 2）且不写归档；
   显式放行时 manifest 顶部 ``missing_components`` 如实记录；``--orchestration-dir``
   的默认值按 ``--repo-root`` 解析（不再取自脚本自身位置）。

测试使用**真实**数据库（``AJAIMGR_TEST_DB_URL``，即 ``ajaimgr_test``）与真实
``pg_dump``/``psql``；恢复目标是专用演练库 ``ajaimgr_restore_check``，每个恢复
用例都 ``--recreate-db`` 重建，模块结束时删除。附件是经产品代码
``app.documents.storage.store_file`` 落盘的**真实内容寻址 blob**，任务库是临时
目录里的真实 SQLite（绝不触碰仓库内 ``.orchestration/``）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKUP_PY = REPO_ROOT / "ops" / "backup.py"
RESTORE_PY = REPO_ROOT / "ops" / "restore.py"

#: 演练专用恢复库：名字必须含 ``restore``，否则 ``--recreate-db`` 会被安全守卫拒绝。
RESTORE_DB_NAME = "ajaimgr_restore_check"
RESTORE_DB_URL = f"postgresql+psycopg:///{RESTORE_DB_NAME}"

CRITICAL_TABLES = (
    "companies",
    "employees",
    "expense_claims",
    "expense_lines",
    "audit_events",
    "invoice_verifications",
    "source_files",
)

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000154a24f9d00000000"
    "49454e44ae426082"
)
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
TEXT_BYTES = "W1-25 附件（测试用真实票据说明）\n".encode()


# --- 进程助手 -----------------------------------------------------------------


def run(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """用当前解释器（uv 管理的 venv）运行工具，返回真实退出码与输出。"""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(REPO_ROOT),
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def psql(database: str, sql: str) -> list[list[str]]:
    result = subprocess.run(
        ["psql", "-X", "-q", "-A", "-t", "-F", "\t", "-d", database, "-c", sql],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]


def database_name(db_url: str) -> str:
    return db_url.rsplit("/", 1)[-1]


# --- 归档助手 -----------------------------------------------------------------


def archive_members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as tar:
        members: dict[str, bytes] = {}
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            assert handle is not None
            members[member.name] = handle.read()
        return members


def write_archive(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name in sorted(members):
            data = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))


def refresh_sidecar(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_orchestration_dir(root: Path) -> Path:
    """造一个真实的任务库目录（SQLite + config.json），绝不碰仓库内 .orchestration/。"""
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state.sqlite3"
    connection = sqlite3.connect(str(state))
    try:
        connection.executescript(
            """
            CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE locks (path TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT);
            INSERT INTO tasks (id, status) VALUES ('W1-25', 'DISPATCHED');
            INSERT INTO locks (path) VALUES ('ops/');
            INSERT INTO events (note) VALUES ('dispatch'), ('review');
            """
        )
        connection.commit()
    finally:
        connection.close()
    (root / "config.json").write_text(
        json.dumps({"retry_cap": 3}) + "\n", encoding="utf-8"
    )
    return root


def make_storage_root(root: Path, *, with_credentials: bool = False) -> Path:
    """用产品代码落盘真实附件（内容寻址 + 魔数/散列校验）。"""
    from app.documents import storage

    root.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("receipt.png", PNG_1X1),
        ("invoice.pdf", PDF_BYTES),
        ("note.txt", TEXT_BYTES),
    ):
        result = storage.store_file(root, owner_id=1, content=content, declared_name=name)
        storage.verify_file(root, result.storage_key, result.sha256)
    if with_credentials:
        (root / ".env").write_text("DB_PASSWORD=NOT-A-REAL-SECRET\n", encoding="utf-8")
        (root / "auth.json").write_text('{"token": "NOT-A-REAL-TOKEN"}\n', encoding="utf-8")
        (root / "server.key").write_text("NOT-A-REAL-KEY\n", encoding="utf-8")
    return root


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def restore_db_url():
    """演练用恢复库：用完即删（本机角色 agents 有 CREATEDB 权限）。"""
    yield RESTORE_DB_URL
    subprocess.run(["dropdb", "--if-exists", RESTORE_DB_NAME], check=False)


@pytest.fixture(scope="module")
def drill(tmp_path_factory, test_db_url) -> dict:
    """模块级演练环境：真实附件目录 + 真实任务库目录 + 源数据库 URL。"""
    base = tmp_path_factory.mktemp("w1-25-drill")
    storage_root = make_storage_root(base / "storage", with_credentials=True)
    orchestration_dir = make_orchestration_dir(base / "orchestration")
    return {
        "base": base,
        "storage_root": storage_root,
        "orchestration_dir": orchestration_dir,
        "source_db_url": test_db_url,
    }


@pytest.fixture(scope="module")
def backup_archive(drill, tmp_path_factory) -> Path:
    """跑一次真实备份（对测试库 + 真实附件 + 真实任务库），返回归档路径。"""
    archive = tmp_path_factory.mktemp("w1-25-archive") / "drill.tar.gz"
    result = run(
        BACKUP_PY,
        "--db-url",
        drill["source_db_url"],
        "--storage-root",
        str(drill["storage_root"]),
        "--orchestration-dir",
        str(drill["orchestration_dir"]),
        "--output",
        str(archive),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert archive.is_file()
    return archive


def restore(archive: Path, work: Path, target: str, *extra: str) -> subprocess.CompletedProcess:
    return run(
        RESTORE_PY,
        str(archive),
        "--target-db-url",
        target,
        "--recreate-db",
        "--workdir",
        str(work / "work"),
        "--storage-target",
        str(work / "storage"),
        "--orchestration-target",
        str(work / "orchestration"),
        "--report",
        str(work / "report.json"),
        *extra,
    )


# --- 1. 完整往返 ---------------------------------------------------------------


def test_full_round_trip(backup_archive, restore_db_url, drill, tmp_path):
    """备份 → 恢复到干净目标库：行数与附件 sha256 全部一致，退出码 0。"""
    work = tmp_path / "round-trip"
    result = restore(backup_archive, work, restore_db_url, "--verbose")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout

    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    assert report["result"] == "PASS"
    assert all(check["status"] == "PASS" for check in report["checks"]), report["checks"]

    manifest = json.loads(archive_members(backup_archive)["manifest.json"].decode())

    # 独立复核：直接查恢复后的数据库，而不是只看工具自己的结论。
    for table in CRITICAL_TABLES:
        expected = manifest["database"]["row_counts"][table]
        actual = int(psql(RESTORE_DB_NAME, f"SELECT count(*) FROM {table}")[0][0])
        assert actual == expected, f"{table}: restored={actual} manifest={expected}"

    # 独立复核：附件逐个重算 sha256。
    storage_target = work / "storage"
    for entry in manifest["storage"]["files"]:
        inner = entry["path"].split("/", 1)[1]
        restored_file = storage_target / inner
        assert restored_file.is_file(), entry["path"]
        assert sha256_file(restored_file) == entry["sha256"], entry["path"]

    # 独立复核：恢复后的任务库可打开、表数与 manifest 一致。
    state = work / "orchestration" / "state.sqlite3"
    connection = sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)
    try:
        tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        tasks = connection.execute("SELECT count(*) FROM tasks").fetchone()[0]
    finally:
        connection.close()
    assert tables == manifest["orchestration"]["state_table_count"]
    assert tasks == 1


def test_restore_reports_every_check(backup_archive, restore_db_url, tmp_path):
    """逐项校验表必须完整出现（防止悄悄少做某一项）。"""
    work = tmp_path / "checks"
    result = restore(backup_archive, work, restore_db_url)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    names = {check["name"] for check in report["checks"]}
    assert names == {
        "archive_checksum",
        "archive_members",
        "manifest",
        "member_hashes",
        "db_restore",
        "db_tables",
        "db_row_counts",
        "storage_restore",
        "storage_hashes",
        "storage_content_address",
        "orchestration_restore",
        "orchestration_sqlite",
        "orchestration_config",
    }


# --- 2. 篡改检测（附件字节） ---------------------------------------------------


def test_tampered_attachment_byte_is_rejected(backup_archive, restore_db_url, tmp_path):
    """改动归档中某个附件字节（并刷新归档校验和）→ 非零退出码 + 指出该项。"""
    members = archive_members(backup_archive)
    blob = next(name for name in members if name.startswith("storage/"))
    data = bytearray(members[blob])
    data[0] ^= 0xFF
    members[blob] = bytes(data)

    tampered = tmp_path / "tampered-storage.tar.gz"
    write_archive(tampered, members)
    refresh_sidecar(tampered)  # 让归档校验和通过，逼出成员级比对

    work = tmp_path / "tampered-work"
    result = restore(tampered, work, restore_db_url)
    assert result.returncode != 0, result.stdout

    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    statuses = {check["name"]: check for check in report["checks"]}
    assert statuses["archive_checksum"]["status"] == "PASS"
    assert statuses["member_hashes"]["status"] == "FAIL"
    assert any(blob in diff for diff in statuses["member_hashes"]["diffs"])
    assert "RESULT: FAIL" in result.stdout


# --- 3. 行数不符检测 -----------------------------------------------------------


def test_row_count_mismatch_is_rejected(backup_archive, restore_db_url, tmp_path):
    """manifest 行数与实际不符 → 非零退出码 + 逐表差异。"""
    members = archive_members(backup_archive)
    manifest = json.loads(members["manifest.json"].decode())
    manifest["database"]["row_counts"]["companies"] = 99
    members["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")

    tampered = tmp_path / "rowcount.tar.gz"
    write_archive(tampered, members)
    refresh_sidecar(tampered)

    work = tmp_path / "rowcount-work"
    result = restore(tampered, work, restore_db_url)
    assert result.returncode != 0, result.stdout

    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    statuses = {check["name"]: check for check in report["checks"]}
    assert statuses["member_hashes"]["status"] == "PASS"
    assert statuses["db_row_counts"]["status"] == "FAIL"
    assert any("companies" in diff for diff in statuses["db_row_counts"]["diffs"])
    rows = {row["table"]: row for row in report["database"]["row_counts"]}
    assert rows["companies"]["manifest"] == 99
    assert rows["companies"]["restored"] != 99


# --- 4. 凭据不入归档 -----------------------------------------------------------


def test_credentials_are_excluded_from_archive(drill, tmp_path):
    """待备份目录里的 .env / auth.json / server.key 不得进入归档。"""
    storage_root = make_storage_root(tmp_path / "storage", with_credentials=True)
    orchestration_dir = make_orchestration_dir(tmp_path / "orchestration")
    (orchestration_dir / ".env").write_text("SECRET=NOT-REAL\n", encoding="utf-8")
    (orchestration_dir / "auth.json").write_text('{"x": 1}\n', encoding="utf-8")

    archive = tmp_path / "creds.tar.gz"
    result = run(
        BACKUP_PY,
        "--db-url",
        drill["source_db_url"],
        "--storage-root",
        str(storage_root),
        "--orchestration-dir",
        str(orchestration_dir),
        "--output",
        str(archive),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "credential scan  : 8 members, 0 violation(s)" in result.stdout or (
        "0 violation(s)" in result.stdout
    )

    members = archive_members(archive)
    for forbidden in (".env", "auth.json", "server.key"):
        assert not any(
            name.split("/")[-1] == forbidden for name in members
        ), f"{forbidden} leaked into the archive"

    manifest = json.loads(members["manifest.json"].decode())
    excluded = {entry["path"] for entry in manifest["excluded_credential_files"]}
    assert {".env", "auth.json", "server.key"} <= excluded
    # 真正的附件仍在归档里（排除策略不能把正常文件也丢掉）。
    assert len(manifest["storage"]["files"]) == 3


def test_undeclared_archive_member_is_rejected(backup_archive, restore_db_url, tmp_path):
    """归档里多出 manifest 未登记的成员 → 非零退出码（manifest 是校验根）。"""
    members = archive_members(backup_archive)
    members["db/extra-injected.sql"] = b"SELECT 1;\n"

    tampered = tmp_path / "undeclared.tar.gz"
    write_archive(tampered, members)
    refresh_sidecar(tampered)

    work = tmp_path / "undeclared-work"
    result = restore(tampered, work, restore_db_url)
    assert result.returncode != 0, result.stdout

    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["member_hashes"]["status"] == "FAIL"
    assert any(
        "db/extra-injected.sql" in diff for diff in checks["member_hashes"]["diffs"]
    )


# --- 5. 归档校验和 -------------------------------------------------------------


def test_tampered_archive_checksum_is_rejected(backup_archive, restore_db_url, tmp_path):
    """归档字节被改动而旁车校验和未更新 → 拒绝（非零退出码）。"""
    corrupted = tmp_path / "corrupted.tar.gz"
    raw = bytearray(backup_archive.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    corrupted.write_bytes(bytes(raw))
    shutil.copyfile(
        backup_archive.with_name(backup_archive.name + ".sha256"),
        corrupted.with_name(corrupted.name + ".sha256"),
    )

    work = tmp_path / "corrupted-work"
    result = restore(corrupted, work, restore_db_url)
    assert result.returncode != 0, result.stdout

    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["archive_checksum"]["status"] == "FAIL"
    assert "archive_checksum" in result.stdout
    # 校验和不通过时不得继续恢复：目标库不应被创建/覆盖。
    assert "db_restore" not in checks


# --- 6. 缺失组成部分守卫（必需组成部分缺失即失败） -------------------------------


def test_missing_storage_fails_without_allow_switch(drill, tmp_path):
    """storage 缺失且未给开关（orchestration 给有效值以隔离）→ 退出码 2、
    报错含开关名与解析路径、且 --output 目标文件不存在（不得留下残缺归档）。"""
    missing_storage = tmp_path / "no-such-storage"
    archive = tmp_path / "missing-storage.tar.gz"
    assert not missing_storage.exists()

    result = run(
        BACKUP_PY,
        "--db-url",
        drill["source_db_url"],
        "--storage-root",
        str(missing_storage),
        "--orchestration-dir",
        str(drill["orchestration_dir"]),
        "--output",
        str(archive),
    )
    # ToolError 通道 → 退出码 2（非零）。
    assert result.returncode == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "--allow-missing-storage" in combined
    assert "storage" in combined
    assert str(missing_storage.resolve()) in combined
    assert not archive.exists()
    assert not archive.with_name(archive.name + ".sha256").exists()


def test_missing_orchestration_fails_without_allow_switch(drill, tmp_path):
    """orchestration 缺失且未给开关（storage 给有效值）→ 退出码 2 且不写归档。"""
    missing_orch = tmp_path / "no-such-orchestration"
    archive = tmp_path / "missing-orchestration.tar.gz"
    assert not missing_orch.exists()

    result = run(
        BACKUP_PY,
        "--db-url",
        drill["source_db_url"],
        "--storage-root",
        str(drill["storage_root"]),
        "--orchestration-dir",
        str(missing_orch),
        "--output",
        str(archive),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "--allow-missing-orchestration" in combined
    assert str(missing_orch.resolve()) in combined
    assert not archive.exists()
    assert not archive.with_name(archive.name + ".sha256").exists()


def test_missing_components_with_switches_are_recorded(drill, tmp_path):
    """两个组成部分均缺失 + 两个开关均给 → 退出码 0；``missing_components``
    恰为 ``["storage", "orchestration"]``；归档成员只有 manifest 与数据库 dump。"""
    archive = tmp_path / "both-missing.tar.gz"
    result = run(
        BACKUP_PY,
        "--db-url",
        drill["source_db_url"],
        "--storage-root",
        str(tmp_path / "no-storage"),
        "--orchestration-dir",
        str(tmp_path / "no-orchestration"),
        "--output",
        str(archive),
        "--allow-missing-storage",
        "--allow-missing-orchestration",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    members = archive_members(archive)
    assert sorted(members) == ["db/database.sql", "manifest.json"]

    manifest = json.loads(members["manifest.json"].decode("utf-8"))
    assert manifest["missing_components"] == ["storage", "orchestration"]
    # stdout 汇总同样可见。
    assert "missing parts    : storage, orchestration" in result.stdout


def test_orchestration_dir_default_resolves_under_repo_root(drill, tmp_path):
    """--orchestration-dir 缺省值按 --repo-root 解析：source_dir == <fixture>/.orchestration。"""
    fixture_repo = tmp_path / "fixture-repo"
    fixture_orch = make_orchestration_dir(fixture_repo / ".orchestration")
    archive = tmp_path / "default-orchestration.tar.gz"

    result = run(
        BACKUP_PY,
        "--db-url",
        drill["source_db_url"],
        "--repo-root",
        str(fixture_repo),
        "--storage-root",
        str(drill["storage_root"]),
        # 故意不给 --orchestration-dir：默认值必须解析到 fixture 仓库下。
        "--output",
        str(archive),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    manifest = json.loads(archive_members(archive)["manifest.json"].decode("utf-8"))
    assert manifest["orchestration"]["source_dir"] == str(fixture_orch.resolve())
    assert manifest["missing_components"] == []


# --- 额外安全守卫 --------------------------------------------------------------


def test_backup_refuses_output_inside_repository(drill, tmp_path):
    """备份默认不落在仓库内：显式写到仓库内必须被拒绝且不留文件。"""
    inside = REPO_ROOT / "var" / "w1-25-should-not-exist.tar.gz"
    assert not inside.exists()
    result = run(
        BACKUP_PY,
        "--db-url",
        drill["source_db_url"],
        "--storage-root",
        str(drill["storage_root"]),
        "--orchestration-dir",
        str(drill["orchestration_dir"]),
        "--output",
        str(inside),
    )
    assert result.returncode != 0
    assert "refusing to write the backup inside the repository" in (
        result.stdout + result.stderr
    )
    assert not inside.exists()


def test_restore_refuses_protected_database(backup_archive, test_db_url, tmp_path):
    """--recreate-db 指向真实库（ajaimgr_test）时必须被守卫拦下。"""
    result = restore(backup_archive, tmp_path / "guard", test_db_url)
    assert result.returncode != 0
    assert "refusing to restore into protected database" in (
        result.stdout + result.stderr
    )
