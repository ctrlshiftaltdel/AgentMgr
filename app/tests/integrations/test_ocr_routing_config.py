"""OCR 端点路由配置（``[ocr].routing``）装载测试（Issue #52，纯逻辑、零网络）。

覆盖验收标准的**取值口径**与**读取口径**：

* 配置文件**整体缺失** → 默认 ``smart``；``[ocr]`` 节缺失 → 默认 ``smart``；
  ``[ocr].routing`` 键缺失 → 默认 ``smart``；
* 取值非法（含非字符串、非表节、非法 TOML）→ **显式报错**（fail closed），错误信息
  只报**键名与允许取值**、不回显配置取值；
* 读取路径：显式参数 → 环境变量 ``AJAIMGR_CONFIG_FILE`` → 默认
  ``config/ajaimgr.toml``；与 ``app.export.config``（导出设置）**同一个文件、同一个
  环境变量**——本文件用断言固定该口径，避免两处常量漂移；
* 仓库内示例文件（``config/ajaimgr.example.toml``）必须能读回 ``smart``（文档即契约）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.export.config import (
    CONFIG_PATH_ENV as EXPORT_CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH as EXPORT_DEFAULT_CONFIG_PATH,
)
from app.integrations.ocr_routing import (
    CODE_CONFIG_INVALID,
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    DEFAULT_ROUTING,
    OCR_ROUTING_KEY,
    OCR_ROUTING_SECTION,
    OcrRoutingConfigError,
    OcrRoutingMode,
    coerce_routing_mode,
    config_path_from_env,
    load_ocr_routing,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "ajaimgr.example.toml"


def _write(tmp_path: Path, text: str, name: str = "ajaimgr.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- 常量与读取口径 -----------------------------------------------------------


def test_config_path_constants_match_export_settings() -> None:
    """「同一个文件、同一个环境变量」（#43 决策）由断言固定，避免两处漂移。"""
    assert CONFIG_PATH_ENV == EXPORT_CONFIG_PATH_ENV == "AJAIMGR_CONFIG_FILE"
    assert DEFAULT_CONFIG_PATH == EXPORT_DEFAULT_CONFIG_PATH == "config/ajaimgr.toml"
    assert OCR_ROUTING_SECTION == "ocr"
    assert OCR_ROUTING_KEY == "ocr.routing"


def test_default_routing_is_smart() -> None:
    """默认值必须与当前生产行为一致（Issue #52 验收第 1 条）。"""
    assert DEFAULT_ROUTING is OcrRoutingMode.SMART
    assert OcrRoutingMode.SMART.value == "smart"
    assert OcrRoutingMode.ROUTED.value == "routed"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({CONFIG_PATH_ENV: "/srv/ajaimgr/ajaimgr.toml"}, "/srv/ajaimgr/ajaimgr.toml"),
        ({CONFIG_PATH_ENV: "  /srv/x.toml  "}, "/srv/x.toml"),
        ({CONFIG_PATH_ENV: "   "}, DEFAULT_CONFIG_PATH),
        ({CONFIG_PATH_ENV: ""}, DEFAULT_CONFIG_PATH),
        ({}, DEFAULT_CONFIG_PATH),
    ],
)
def test_config_path_from_env(environ: dict[str, str], expected: str) -> None:
    assert config_path_from_env(environ) == expected


def test_load_uses_env_path_when_no_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "[ocr]\nrouting = \"routed\"\n")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(path))
    assert load_ocr_routing() is OcrRoutingMode.ROUTED


def test_explicit_path_wins_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = _write(tmp_path, "[ocr]\nrouting = \"routed\"\n", name="env.toml")
    explicit = _write(tmp_path, "[ocr]\nrouting = \"smart\"\n", name="explicit.toml")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(env_path))
    assert load_ocr_routing(explicit) is OcrRoutingMode.SMART


# --- 默认（缺文件 / 缺节 / 缺键） ---------------------------------------------


def test_missing_config_file_defaults_to_smart(tmp_path: Path) -> None:
    assert load_ocr_routing(tmp_path / "absent.toml") is OcrRoutingMode.SMART


def test_config_without_ocr_section_defaults_to_smart(tmp_path: Path) -> None:
    """同一个文件还承载导出设置；没有 ``[ocr]`` 节即未配置路由。"""
    path = _write(
        tmp_path,
        "[template]\nlayout = \"entry_rows\"\n\n[scope]\nstatuses = [\"paid\"]\n",
    )
    assert load_ocr_routing(path) is OcrRoutingMode.SMART


def test_ocr_section_without_routing_key_defaults_to_smart(tmp_path: Path) -> None:
    path = _write(tmp_path, "[ocr]\n# 未设置 routing\n")
    assert load_ocr_routing(path) is OcrRoutingMode.SMART


def test_ocr_section_only_file_is_accepted(tmp_path: Path) -> None:
    """本装载器**不重载**导出配置：只有 ``[ocr]`` 的文件也能读（导出侧自行 fail closed）。"""
    path = _write(tmp_path, "[ocr]\nrouting = \"routed\"\n")
    assert load_ocr_routing(path) is OcrRoutingMode.ROUTED


# --- 合法取值 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("smart", OcrRoutingMode.SMART), ("routed", OcrRoutingMode.ROUTED)],
)
def test_valid_values_are_read(
    tmp_path: Path, value: str, expected: OcrRoutingMode
) -> None:
    path = _write(tmp_path, f"[ocr]\nrouting = \"{value}\"\n")
    assert load_ocr_routing(path) is expected


def test_surrounding_whitespace_is_tolerated_but_case_is_not(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "[ocr]\nrouting = \" routed \"\n")
    assert load_ocr_routing(path) is OcrRoutingMode.ROUTED

    # 不做大小写折叠：``Smart`` 属**非法**取值（显式失败，避免「以为切了路由」）。
    path = _write(tmp_path, "[ocr]\nrouting = \"Smart\"\n")
    with pytest.raises(OcrRoutingConfigError):
        load_ocr_routing(path)


# --- 非法取值（fail closed） --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["legacy", "smart_routed", "SMART", "", "  "],
    ids=["unknown-word", "other-word", "upper-case", "empty", "blank"],
)
def test_invalid_string_value_fails_closed(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, f"[ocr]\nrouting = \"{value}\"\n")
    with pytest.raises(OcrRoutingConfigError) as caught:
        load_ocr_routing(path)

    error = caught.value
    assert error.code == CODE_CONFIG_INVALID
    assert OCR_ROUTING_KEY in error.message  # 只报键名
    assert "unsupported value" in error.message
    assert "smart" in error.message and "routed" in error.message  # 列出允许取值
    if value.strip():
        assert value not in error.message  # 绝不回显配置取值


@pytest.mark.parametrize(
    "value",
    ["1", "true", "[\"smart\"]", "{ mode = \"smart\" }"],
    ids=["integer", "boolean", "array", "inline-table"],
)
def test_non_string_value_fails_closed(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, f"[ocr]\nrouting = {value}\n")
    with pytest.raises(OcrRoutingConfigError) as caught:
        load_ocr_routing(path)
    assert caught.value.code == CODE_CONFIG_INVALID
    assert OCR_ROUTING_KEY in caught.value.message


def test_sub_table_instead_of_string_fails_closed(tmp_path: Path) -> None:
    path = _write(tmp_path, "[ocr.routing]\nmode = \"smart\"\n")
    with pytest.raises(OcrRoutingConfigError) as caught:
        load_ocr_routing(path)
    assert caught.value.code == CODE_CONFIG_INVALID
    assert OCR_ROUTING_KEY in caught.value.message


def test_ocr_section_not_a_table_fails_closed(tmp_path: Path) -> None:
    path = _write(tmp_path, "ocr = \"smart\"\n")
    with pytest.raises(OcrRoutingConfigError) as caught:
        load_ocr_routing(path)
    assert caught.value.code == CODE_CONFIG_INVALID
    assert f"'{OCR_ROUTING_SECTION}'" in caught.value.message


def test_malformed_toml_fails_closed(tmp_path: Path) -> None:
    """文件既然存在，说明部署方想配置它——非法 TOML 必须显式失败而不是静默回默认。"""
    path = _write(tmp_path, "[ocr\nrouting = \"smart\"\n")
    with pytest.raises(OcrRoutingConfigError) as caught:
        load_ocr_routing(path)
    assert caught.value.code == CODE_CONFIG_INVALID
    assert str(path) in caught.value.message  # 路径用于运维定位


def test_non_utf8_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ajaimgr.toml"
    path.write_bytes(b"[ocr]\nrouting = \"\xff\xfe\"\n")
    with pytest.raises(OcrRoutingConfigError):
        load_ocr_routing(path)


def test_unreadable_path_fails_closed(tmp_path: Path) -> None:
    # 目录不是可读文件：``read_bytes`` 抛 ``IsADirectoryError``（OSError 子类）。
    with pytest.raises(OcrRoutingConfigError) as caught:
        load_ocr_routing(tmp_path)
    assert caught.value.code == CODE_CONFIG_INVALID


# --- 取值归一化辅助 -----------------------------------------------------------


def test_coerce_routing_mode_accepts_enum_and_string() -> None:
    assert coerce_routing_mode(OcrRoutingMode.ROUTED, where="x") is (
        OcrRoutingMode.ROUTED
    )
    assert coerce_routing_mode("smart", where="x") is OcrRoutingMode.SMART


def test_coerce_routing_mode_reports_where_and_hides_value() -> None:
    with pytest.raises(OcrRoutingConfigError) as caught:
        coerce_routing_mode("legacy", where="argument 'routing'")
    assert caught.value.code == CODE_CONFIG_INVALID
    assert "argument 'routing'" in caught.value.message
    assert "legacy" not in caught.value.message


# --- 仓库内示例文件（文档即契约） ---------------------------------------------


def test_repository_example_config_reads_smart() -> None:
    assert EXAMPLE_CONFIG.is_file()
    assert load_ocr_routing(EXAMPLE_CONFIG) is OcrRoutingMode.SMART


def test_example_config_documents_both_modes_and_the_default() -> None:
    """示例文件必须解释两种模式、默认值与 fail closed 口径（部署方据此选择）。"""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert "AgentMgr **项目统一配置**" in text
    assert "[ocr]" in text
    assert 'routing = "smart"' in text
    assert '"routed"' in text
    assert "AJAIMGR_CONFIG_FILE" in text
    # 说明「非法取值显式报错」，而不是静默回退。
    assert "fail closed" in text
