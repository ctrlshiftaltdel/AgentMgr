"""W1-15 每员工 Profile 生成器与校验器测试（纯文件系统，不接触 ~/.hermes）。

硬约束（来自项目所有者，最高优先级）
====================================
本测试**绝不**在本机新建/启动/停止任何真实 Hermes Profile，**绝不**读写
``~/.hermes``（含 ``~/.hermes/profiles``）。所有临时目录都建在**禁用根目录之外**
（见 :func:`_safe_temp_root`），并且：

1. 每个测试前后对 ``~/.hermes`` 的**关键文件**（``config.yaml`` / ``.env`` /
   ``auth.json`` / ``SOUL.md``）与 ``~/.hermes/profiles`` **目录本身的 mtime** 做快照
   对比；
2. 通过 monkeypatch 拦截一切写系统调用（``builtins.open`` 写模式、``os.mkdir`` /
   ``os.makedirs`` / ``os.open``、``pathlib.Path.mkdir`` / ``write_text`` /
   ``write_bytes`` / ``touch``），断言**没有任何写操作指向禁用路径**；
3. 断言「计划/校验/CLI 的 dry-run」不产生任何文件。

覆盖任务验收标准：正常生成（隔离要素齐备、config 无明文凭据）、权限不符被报错、
缺必填文件被报错、尝试写入 ``~/.hermes`` 被拒绝、dry-run 零文件、全程未触碰
``~/.hermes``。
"""

from __future__ import annotations

import builtins
import os
import pathlib
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.runtime.profile_provisioning import (
    DIR_MODE,
    REQUIRED_DIRS,
    SENSITIVE_FILE_MODE,
    ForbiddenPathError,
    ensure_safe_path,
    forbidden_roots,
    main,
    plan_profile,
    profile_name_for,
    render_config,
    render_config_text,
    scan_plaintext_secrets,
    validate_profile_dir,
)

# --------------------------------------------------------------------------- #
# 受保护路径与安全临时目录
# --------------------------------------------------------------------------- #


def _hermes_home() -> Path:
    """默认 Profile 的 home（``$HERMES_HOME`` 或 ``~/.hermes``）。"""
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _under_forbidden(path: os.PathLike[str] | str) -> bool:
    resolved = Path(path).expanduser().resolve()
    for root in forbidden_roots():
        if resolved == root or root in resolved.parents:
            return True
    return False


def _safe_temp_root() -> Path:
    """返回一个**必然在禁用根目录之外**的临时根。

    默认沿用 ``$TMPDIR``；若 ``$TMPDIR`` 恰好落在 ``~/.hermes`` 之下（本机默认如此，
    因为 Hermes 的 scratch 目录在 ``~/.hermes/cache/scratch``），则回退到
    ``/tmp/ajaimgr-w15-tests``，确保测试不写入 ``~/.hermes``。
    """
    candidate = Path(os.environ.get("AJAIMGR_W15_TEST_TMP") or tempfile.gettempdir())
    if _under_forbidden(candidate):
        candidate = Path("/tmp/ajaimgr-w15-tests")
    candidate.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    assert not _under_forbidden(candidate), f"临时根 {candidate} 落在禁用目录内"
    return candidate


@pytest.fixture()
def tmp_root() -> Iterator[Path]:
    """在禁用根目录之外创建、用完即删的临时目录。"""
    root = Path(tempfile.mkdtemp(prefix="w15-", dir=_safe_temp_root())).resolve()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 写操作拦截 + ~/.hermes 快照
# --------------------------------------------------------------------------- #


@contextmanager
def _track_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    """拦截写系统调用，记录 ``(操作, 目标路径)``。仅记录，不阻断。"""
    written: list[tuple[str, str]] = []

    def record(op: str, target: object) -> None:
        try:
            written.append((op, os.fspath(target)))  # type: ignore[arg-type]
        except TypeError:
            written.append((op, str(target)))

    real_open = builtins.open

    def tracked_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            record("builtins.open", file)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)

    real_os_mkdir = os.mkdir

    def tracked_os_mkdir(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        record("os.mkdir", path)
        return real_os_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", tracked_os_mkdir)

    real_os_makedirs = os.makedirs

    def tracked_os_makedirs(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        record("os.makedirs", path)
        return real_os_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", tracked_os_makedirs)

    real_os_open = os.open

    def tracked_os_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            record("os.open", path)
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", tracked_os_open)

    real_mkdir = pathlib.Path.mkdir

    def tracked_mkdir(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        record("Path.mkdir", self)
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", tracked_mkdir)

    real_write_text = pathlib.Path.write_text

    def tracked_write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        record("Path.write_text", self)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", tracked_write_text)

    real_write_bytes = pathlib.Path.write_bytes

    def tracked_write_bytes(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        record("Path.write_bytes", self)
        return real_write_bytes(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_bytes", tracked_write_bytes)

    real_touch = pathlib.Path.touch

    def tracked_touch(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        record("Path.touch", self)
        return real_touch(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "touch", tracked_touch)

    yield written


#: 绝不能被本任务触碰的默认 Profile 关键文件。
_CROWN_JEWELS = ("config.yaml", ".env", "auth.json", "SOUL.md")


def _crown_jewel_snapshot() -> dict[str, tuple[int, int] | None]:
    home = _hermes_home()
    snapshot: dict[str, tuple[int, int] | None] = {}
    for name in _CROWN_JEWELS:
        try:
            st = (home / name).stat()
        except OSError:
            snapshot[name] = None
            continue
        snapshot[name] = (st.st_mtime_ns, st.st_size)
    return snapshot


def _profiles_dir_mtime() -> int | None:
    try:
        return (_hermes_home() / "profiles").stat().st_mtime_ns
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 测试夹具：构造符合 / 不符合要求的 Profile 目录
# --------------------------------------------------------------------------- #


def _build_profile_dir(
    root: Path,
    name: str = "emp-w15-1",
    *,
    dir_mode: int = DIR_MODE,
    config_mode: int = SENSITIVE_FILE_MODE,
    soul_mode: int = SENSITIVE_FILE_MODE,
    config_text: str = (
        "model:\n"
        '  api_key: "${HERMES_PROFILE_EMP_W15_1_MODEL_API_KEY}"\n'
    ),
    omit_dirs: tuple[str, ...] = (),
    omit_files: tuple[str, ...] = (),
) -> Path:
    """在 ``root`` 下造一个 Profile 目录（默认符合要求）。"""
    profile = root / name
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, dir_mode)
    for sub in REQUIRED_DIRS:
        if sub in omit_dirs:
            continue
        child = profile / sub
        child.mkdir(exist_ok=True)
        os.chmod(child, DIR_MODE)
    files = {
        "config.yaml": config_text,
        ".env": "",
        "state.db": "",
        "SOUL.md": "# SOUL\n",
    }
    for filename, text in files.items():
        if filename in omit_files:
            continue
        target = profile / filename
        target.write_text(text, encoding="utf-8")
    modes = {
        "config.yaml": config_mode,
        ".env": SENSITIVE_FILE_MODE,
        "state.db": SENSITIVE_FILE_MODE,
        "SOUL.md": soul_mode,
    }
    for filename, mode in modes.items():
        if filename in omit_files:
            continue
        os.chmod(profile / filename, mode)
    return profile


# --------------------------------------------------------------------------- #
# 1. 正常生成
# --------------------------------------------------------------------------- #


def test_plan_contains_all_isolation_elements(tmp_root: Path) -> None:
    plan = plan_profile({"id": 1, "employee_no": "E1"}, base_dir=tmp_root / "profiles")

    assert plan.profile_name == "emp-e1"
    assert plan.profile_dir == tmp_root / "profiles" / "emp-e1"
    assert plan.working_dir == plan.profile_dir / "workspace"
    assert plan.temp_dir == plan.profile_dir / "tmp"
    assert plan.service_account.startswith("ajaimgr-")
    assert plan.service_unit == "hermes-gateway-emp-e1.service"
    assert plan.dir_mode == DIR_MODE
    assert plan.sensitive_file_mode == SENSITIVE_FILE_MODE
    assert plan.session_namespace == "agent:emp-e1"
    assert set(plan.required_dirs) == set(REQUIRED_DIRS)

    codes = {element.code for element in plan.isolation_elements}
    assert {
        "profile_dir",
        "config",
        "session_namespace",
        "memory_namespace",
        "working_dir",
        "temp_dir",
        "service_account",
        "internal_api_identity",
        "tool_authorization",
        "audit_context",
    } <= codes

    # 凭据只以引用出现：有名字与用途，绝无值字段。
    assert plan.credential_refs
    for ref in plan.credential_refs:
        assert ref.name and ref.purpose
        assert ref.as_dict().keys() == {"name", "purpose", "kind"}


def test_plan_derives_profile_name_and_is_pure(tmp_root: Path) -> None:
    assert profile_name_for(42) == "emp-42"
    assert profile_name_for({"id": 7, "employee_no": "A-001"}) == "emp-a-001"

    base = tmp_root / "profiles"
    plan_profile({"id": 2}, base_dir=base)
    plan_profile({"id": 3}, base_dir=base)
    # 纯函数：不创建任何文件/目录。
    assert not base.exists()
    assert list(tmp_root.rglob("*")) == []


def test_render_config_has_no_plaintext_credentials(tmp_root: Path) -> None:
    plan = plan_profile({"id": 3, "employee_no": "E3"}, base_dir=tmp_root / "profiles")
    config = render_config(plan)

    assert config["model"]["api_key"] == "${" + plan.model_api_key_ref + "}"
    assert config["terminal"]["cwd"] == str(plan.working_dir)
    assert config["terminal"]["tmpdir"] == str(plan.temp_dir)
    assert config["terminal"]["home_mode"] == "profile"
    assert config["sessions"]["namespace"] == plan.session_namespace
    assert config["memory"]["dir"] == str(plan.profile_dir / "memories")

    text = render_config_text(plan)
    assert scan_plaintext_secrets(text) == []
    assert "${" in text  # 密钥为引用
    assert "sk-" not in text


def test_scan_flags_plaintext_but_ignores_references() -> None:
    assert scan_plaintext_secrets('api_key: "${HERMES_X}"') == []
    assert scan_plaintext_secrets("api_key: <redacted>") == []
    assert scan_plaintext_secrets("api_key_ref: HERMES_PROFILE_X_MODEL_API_KEY") == []
    assert scan_plaintext_secrets("api_key: sk-live0123456789abcdef") != []
    assert scan_plaintext_secrets("WEIXIN_TOKEN=abc123secret") != []


# --------------------------------------------------------------------------- #
# 2/3. 校验：权限与必填项
# --------------------------------------------------------------------------- #


def test_validate_accepts_correct_dir(tmp_root: Path) -> None:
    profile = _build_profile_dir(tmp_root)
    assert validate_profile_dir(profile) == []


def test_validate_flags_wrong_dir_mode(tmp_root: Path) -> None:
    profile = _build_profile_dir(tmp_root, dir_mode=0o755)
    problems = validate_profile_dir(profile)
    codes = {p.code for p in problems}
    assert "dir_mode" in codes
    assert any(p.path == str(profile) for p in problems if p.code == "dir_mode")


def test_validate_flags_wrong_sensitive_file_mode(tmp_root: Path) -> None:
    profile = _build_profile_dir(tmp_root, config_mode=0o644)
    problems = validate_profile_dir(profile)
    assert any(
        p.code == "sensitive_file_mode" and p.path.endswith("config.yaml")
        for p in problems
    )


def test_validate_flags_group_writable_soul_md(tmp_root: Path) -> None:
    profile = _build_profile_dir(tmp_root, soul_mode=0o664)
    problems = validate_profile_dir(profile)
    assert any(p.code == "insecure_mode" and p.path.endswith("SOUL.md") for p in problems)


def test_validate_flags_missing_required_file(tmp_root: Path) -> None:
    profile = _build_profile_dir(tmp_root, omit_files=("config.yaml",))
    problems = validate_profile_dir(profile)
    assert any(
        p.code == "missing_file" and p.path.endswith("config.yaml") for p in problems
    )


def test_validate_flags_missing_required_subdir(tmp_root: Path) -> None:
    profile = _build_profile_dir(tmp_root, omit_dirs=("sessions",))
    problems = validate_profile_dir(profile)
    assert any(p.code == "missing_dir" and p.path.endswith("sessions") for p in problems)


def test_validate_flags_plaintext_credential(tmp_root: Path) -> None:
    profile = _build_profile_dir(
        tmp_root, config_text='model:\n  api_key: "sk-live0123456789abcdef"\n'
    )
    problems = validate_profile_dir(profile)
    assert any(
        p.code == "plaintext_credential" and p.path.endswith("config.yaml")
        for p in problems
    )


def test_validate_flags_missing_directory(tmp_root: Path) -> None:
    problems = validate_profile_dir(tmp_root / "does-not-exist")
    assert [p.code for p in problems] == ["missing_dir"]


# --------------------------------------------------------------------------- #
# 4. 尝试写入 ~/.hermes 被拒绝
# --------------------------------------------------------------------------- #


def test_forbidden_roots_include_hermes_home() -> None:
    roots = forbidden_roots()
    assert roots
    assert (Path.home() / ".hermes").resolve() in roots


def test_ensure_safe_path_rejects_forbidden_roots() -> None:
    for root in forbidden_roots():
        with pytest.raises(ForbiddenPathError):
            ensure_safe_path(root)
        with pytest.raises(ForbiddenPathError):
            ensure_safe_path(root / "profiles" / "emp-1")


def test_ensure_safe_path_allows_tmp(tmp_root: Path) -> None:
    assert ensure_safe_path(tmp_root / "profiles" / "emp-1") == (
        tmp_root / "profiles" / "emp-1"
    )


def test_plan_refuses_forbidden_base_dir_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden_base = _hermes_home() / "profiles"
    with _track_writes(monkeypatch) as written:
        with pytest.raises(ForbiddenPathError):
            plan_profile({"id": 999}, base_dir=forbidden_base)
    # 拒绝发生在任何文件系统副作用之前：完全没有写操作。
    assert written == []


def test_validate_refuses_forbidden_path_without_touching_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = _hermes_home() / "profiles" / "profile-a"
    with _track_writes(monkeypatch) as written:
        problems = validate_profile_dir(forbidden)
    assert [p.code for p in problems] == ["forbidden_path"]
    assert written == []


def test_cli_plan_refuses_forbidden_base_dir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "plan",
            "--employee-id",
            "1",
            "--base-dir",
            str(_hermes_home() / "profiles"),
        ]
    )
    assert rc == 2
    assert "禁用根目录" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 5. dry-run 不产生任何文件
# --------------------------------------------------------------------------- #


def test_cli_plan_dry_run_creates_no_files(
    tmp_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_root / "profiles"
    rc = main(
        ["plan", "--employee-id", "7", "--employee-no", "E7", "--base-dir", str(base)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "emp-e7" in out
    assert "dry-run" in out
    # dry-run：目标根目录与任何文件都不应被创建。
    assert not base.exists()
    assert list(tmp_root.rglob("*")) == []


def test_cli_validate_exit_codes(
    tmp_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _build_profile_dir(tmp_root, name="emp-good")
    assert main(["validate", str(good)]) == 0
    assert "OK" in capsys.readouterr().out

    bad = _build_profile_dir(tmp_root, name="emp-bad", dir_mode=0o755)
    assert main(["validate", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "不符合" in out
    assert "dir_mode" in out


# --------------------------------------------------------------------------- #
# 6. 全程未触碰 ~/.hermes
# --------------------------------------------------------------------------- #


def test_test_temp_root_is_outside_forbidden(tmp_root: Path) -> None:
    assert not _under_forbidden(tmp_root)


def test_all_code_paths_never_touch_hermes_home(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _hermes_home()
    if not home.exists():
        pytest.skip("本机不存在 ~/.hermes，断言无对象")

    jewels_before = _crown_jewel_snapshot()
    profiles_mtime_before = _profiles_dir_mtime()

    with _track_writes(monkeypatch) as written:
        plan = plan_profile(
            {"id": 5150, "employee_no": "W15-5150"}, base_dir=tmp_root / "profiles"
        )
        render_config(plan)
        render_config_text(plan)
        good = _build_profile_dir(tmp_root, name="emp-w15-5150")
        assert validate_profile_dir(good) == []
        assert main(
            [
                "plan",
                "--employee-id",
                "5150",
                "--employee-no",
                "W15-5150",
                "--base-dir",
                str(tmp_root / "profiles"),
            ]
        ) == 0
        assert main(["validate", str(good)]) == 0

    forbidden_writes = [(op, path) for op, path in written if _under_forbidden(path)]
    assert forbidden_writes == [], f"测试期间出现对禁用路径的写操作：{forbidden_writes}"

    # 关键文件与 profiles 目录本身均未被触碰。
    assert _crown_jewel_snapshot() == jewels_before
    assert _profiles_dir_mtime() == profiles_mtime_before
