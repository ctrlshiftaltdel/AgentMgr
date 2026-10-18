"""配置装载测试（纯逻辑；``--noconftest`` 下可跑，不触库）。

覆盖 Issue #53 验收标准第 1、2 条与「配置 fail-closed 用例」：

* 单一 TOML 文件 + ``tomllib``；``AJAIMGR_CONFIG_FILE`` 覆盖；默认路径；
* 缺必填项 / 取值非法 / 模板哈希格式不符 / 布局不支持 / 疑似凭据键 → fail closed；
* 错误只报键名与原因码，**不回显配置取值**；
* 仓库内 ``config/ajaimgr.example.toml`` 本身必须可被真实装载器解析。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.export.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    config_path_from_env,
    load_export_config,
)
from app.export.errors import (
    CODE_CONFIG_FILE_MISSING,
    CODE_CONFIG_INVALID,
    CODE_CONFIG_MISSING_KEYS,
    CODE_UNSUPPORTED_LAYOUT,
    ExportConfigError,
)

from _helpers import (
    build_config_toml,
    build_template_xlsx,
    make_config,
    q,
    write_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _toml(tmp_path: Path, **overrides: object) -> str:
    template_path = tmp_path / "t.xlsx"
    sha = build_template_xlsx(template_path)
    overrides.setdefault("template_path", str(template_path))
    overrides.setdefault("template_sha", sha)
    return build_config_toml(**overrides)  # type: ignore[arg-type]


# --- 路径解析 -----------------------------------------------------------------


def test_config_path_defaults_when_env_unset() -> None:
    assert config_path_from_env({}) == DEFAULT_CONFIG_PATH


def test_config_path_uses_env_override() -> None:
    assert config_path_from_env({CONFIG_PATH_ENV: "/etc/ajaimgr.toml"}) == (
        "/etc/ajaimgr.toml"
    )


def test_config_path_ignores_blank_env() -> None:
    assert config_path_from_env({CONFIG_PATH_ENV: "   "}) == DEFAULT_CONFIG_PATH


def test_load_export_config_reads_env_path(tmp_path: Path, monkeypatch) -> None:
    config_path = write_config(tmp_path / "custom.toml", _toml(tmp_path))
    monkeypatch.setenv(CONFIG_PATH_ENV, str(config_path))
    config = load_export_config()
    assert config.template.layout == "entry_rows"


# --- 快乐路径 -----------------------------------------------------------------


def test_load_valid_config_exposes_all_sections(tmp_path: Path) -> None:
    config = make_config(tmp_path, tax_split="all", tax_account_code="22210101")
    assert config.template.worksheet == "录入数据"
    assert config.scope.statuses == ("paid",)
    assert config.scope.date_basis == "expense_date"
    assert config.voucher.voucher_word == "记"
    assert config.accounts.expense_map["CAT-TRV"] == "560101"
    assert config.tax.split == "all"
    assert config.credit.account_for("paid").code == "100201"
    assert config.currency_code("CNY") == "RMB"
    assert config.permissions.scopes == ("company", "authorized")


def test_credit_by_status_takes_precedence(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        credit_by_status={
            "paid": {"code": "100201", "name": "银行存款"},
            "finance_reviewed": {"code": "224101", "name": "其他应付款-员工"},
        },
    )
    assert config.credit.account_for("paid").code == "100201"
    assert config.credit.account_for("finance_reviewed").code == "224101"
    assert config.credit.account_for("draft") is None


def test_optional_sections_default_to_safe_values(tmp_path: Path) -> None:
    template_path = tmp_path / "t.xlsx"
    sha = build_template_xlsx(template_path)
    text = f"""
[template]
path = {q(str(template_path))}
sha256 = {q(sha)}
worksheet = "录入数据"
layout = "entry_rows"
template_id = "synthetic-template"
template_version = 1

[scope]
statuses = ["paid"]
date_basis = "expense_date"

[voucher]
voucher_word = "记"
preparer = "claimant"

[summary]
template = "{{claim_no}}"
max_length = 500

[accounts]
expense_map = {{ "CAT-TRV" = "560101" }}

[credit]
account_code = "100201"

[currency]
rate = "1"
codes = {{ CNY = "RMB" }}

[permissions]
resource = "expense_claim"
action = "read"
scopes = ["company"]
"""
    config_path = write_config(tmp_path / "minimal.toml", text)
    config = load_export_config(config_path)
    assert config.tax.split == "none"
    assert config.tax.account_code is None
    assert config.auxiliary.mode == "none"
    assert config.invoice_policy.on_verified_fail == "fail"
    assert config.invoice_policy.on_suspected_duplicate == "fail"
    assert config.invoice_policy.attachment_count_basis == "invoices"
    assert config.credit.account_for("paid").code == "100201"


# --- fail closed --------------------------------------------------------------


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(tmp_path / "nope.toml")
    assert excinfo.value.code == CODE_CONFIG_FILE_MISSING


def test_invalid_toml_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "bad.toml", "this is = = not toml\n")
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID


def test_missing_required_section_fails_closed(tmp_path: Path) -> None:
    text = _toml(tmp_path)
    trimmed = text.replace("[permissions]", "[permissions_removed]")
    config_path = write_config(tmp_path / "noperm.toml", trimmed)
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_MISSING_KEYS
    assert "permissions" in excinfo.value.message


def test_missing_required_key_fails_closed(tmp_path: Path) -> None:
    text = _toml(tmp_path).replace('layout = "entry_rows"\n', "")
    config_path = write_config(tmp_path / "nolayout.toml", text)
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_MISSING_KEYS
    assert "template.layout" in excinfo.value.message


def test_bad_sha256_format_fails_closed(tmp_path: Path) -> None:
    text = _toml(tmp_path).replace(
        'sha256 = "', 'sha256 = "NOT-A-HASH-'
    )
    config_path = write_config(tmp_path / "badsha.toml", text)
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID


def test_unsupported_layout_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "layout.toml", _toml(tmp_path, layout="pz")
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_UNSUPPORTED_LAYOUT


def test_invalid_status_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "status.toml", _toml(tmp_path, statuses=["not_a_status"])
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID


def test_invalid_date_basis_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "basis.toml", _toml(tmp_path, date_basis="whenever")
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID


def test_unknown_summary_placeholder_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "summary.toml",
        _toml(tmp_path, summary_template="{claim_no} {unknown_field}"),
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID
    assert "summary.template" in excinfo.value.message


def test_tax_split_without_account_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "tax.toml", _toml(tmp_path, tax_split="all")
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_MISSING_KEYS
    assert "tax.account_code" in excinfo.value.message


def test_tax_categories_without_list_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "taxcat.toml",
        _toml(tmp_path, tax_split="categories", tax_account_code="22210101"),
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID
    assert "tax.split_categories" in excinfo.value.message


def test_credit_without_account_fails_closed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "credit.toml", _toml(tmp_path, credit_code="")
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_MISSING_KEYS
    assert "credit.account_code" in excinfo.value.message


def test_aux_department_mode_requires_class(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "aux.toml", _toml(tmp_path, aux_mode="department")
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_MISSING_KEYS
    assert "auxiliary.department_class" in excinfo.value.message


def test_aux_counterparty_mode_requires_name(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "aux2.toml",
        _toml(
            tmp_path,
            aux_mode="department_counterparty",
            aux_department_class="部门",
        ),
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_MISSING_KEYS
    assert "auxiliary.counterparty_name" in excinfo.value.message


def test_preparer_literal_requires_text(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "prep.toml", _toml(tmp_path, preparer="literal")
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID


def test_float_currency_rate_fails_closed(tmp_path: Path) -> None:
    text = _toml(tmp_path).replace('rate = "1"', "rate = 1.5")
    config_path = write_config(tmp_path / "rate.toml", text)
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID


def test_credential_like_key_fails_closed(tmp_path: Path) -> None:
    text = _toml(tmp_path) + '\n[secrets]\napi_key = "should-not-be-here"\n'
    config_path = write_config(tmp_path / "secret.toml", text)
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert excinfo.value.code == CODE_CONFIG_INVALID
    assert "credential" in excinfo.value.message


def test_error_message_does_not_echo_values(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "status.toml", _toml(tmp_path, statuses=["secret-status"])
    )
    with pytest.raises(ExportConfigError) as excinfo:
        load_export_config(config_path)
    assert "secret-status" not in excinfo.value.message


# --- 仓库内示例文件 -----------------------------------------------------------


def test_repository_example_config_loads(tmp_path: Path) -> None:
    """``config/ajaimgr.example.toml`` 必须能被真实装载器解析（文档即契约）。"""
    config = load_export_config(REPO_ROOT / "config" / "ajaimgr.example.toml")
    assert config.template.layout == "entry_rows"
    assert config.template.worksheet == "录入数据"
    assert config.tax.split == "none"
    assert config.scope.statuses == ("paid",)


def test_example_config_contains_no_real_values() -> None:
    """示例文件不得含本机/个人信息：SHA 为全 0 占位、无家目录绝对路径。"""
    text = (REPO_ROOT / "config" / "ajaimgr.example.toml").read_text(encoding="utf-8")
    assert "0" * 64 in text
    home = os.path.expanduser("~")
    if home and home != "~":
        assert home not in text
