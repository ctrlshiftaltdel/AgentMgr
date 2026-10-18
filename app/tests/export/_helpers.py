"""Track D 导出测试的共享辅助（**不提交任何真实模板**）。

本模块只在测试内用 ``openpyxl`` 构造**合成**的最小 ``录入数据`` 结构，并生成对应的
TOML 配置文本；真实模板由部署方持有、**不入库**，测试一律不依赖它。

设计要点：

* 合成模板的表头逐列取自 :data:`app.export.template.ENTRY_ROWS_HEADERS`，因此它同时
  是「模板结构校验」的正面用例；
* 配置一律经**真实装载器** :func:`app.export.config.load_export_config` 读取（写 TOML
  文本 → 装载），从而覆盖解析与 fail closed 路径；
* 不依赖网络、不依赖真实凭据、不触碰仓库内文件（全部写在 ``tmp_path``）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from openpyxl import Workbook

from app.export.config import ExportConfig, load_export_config
from app.export.template import ENTRY_ROWS_HEADERS, WORKSHEET_ENTRY_ROWS

__all__ = [
    "build_config_toml",
    "build_template_xlsx",
    "make_config",
    "q",
    "write_config",
]


def q(value: object) -> str:
    """把 Python 值渲染为 TOML 字面量（基本字符串 / 数组 / 表 / 布尔 / 整数）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "[" + ", ".join(q(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{k} = {q(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def build_template_xlsx(
    path: Path,
    *,
    headers: Sequence[object] | None = None,
    sheet_name: str = WORKSHEET_ENTRY_ROWS,
    extra_sheets: Sequence[str] = (),
) -> str:
    """写出一个合成模板 xlsx，返回其 SHA-256（供配置指纹使用）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(list(ENTRY_ROWS_HEADERS if headers is None else headers))
    for name in extra_sheets:
        workbook.create_sheet(name)
    workbook.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_config_toml(
    *,
    template_path: str,
    template_sha: str,
    worksheet: str = WORKSHEET_ENTRY_ROWS,
    layout: str = "entry_rows",
    template_id: str = "synthetic-template",
    template_version: int = 1,
    statuses: Sequence[str] = ("paid",),
    date_basis: str = "expense_date",
    voucher_word: str = "记",
    preparer: str = "claimant",
    preparer_literal: str = "",
    summary_template: str = "{claim_no} {employee_no} {description}",
    summary_max_length: int = 500,
    expense_map: Mapping[str, str] | None = None,
    expense_name_map: Mapping[str, str] | None = None,
    expense_default_code: str = "",
    expense_default_name: str = "",
    tax_split: str = "none",
    tax_account_code: str = "",
    tax_account_name: str = "",
    tax_split_categories: Sequence[str] = (),
    credit_code: str = "100201",
    credit_name: str = "银行存款",
    credit_by_status: Mapping[str, Mapping[str, str]] | None = None,
    currency_rate: str = "1",
    currency_codes: Mapping[str, str] | None = None,
    currency_names: Mapping[str, str] | None = None,
    aux_mode: str = "none",
    aux_department_class: str = "",
    aux_counterparty_class: str = "",
    aux_counterparty_code: str = "",
    aux_counterparty_name: str = "",
    attachment_basis: str = "invoices",
    on_verified_fail: str = "fail",
    on_suspected_duplicate: str = "fail",
    permission_resource: str = "expense_claim",
    permission_action: str = "read",
    permission_scopes: Sequence[str] = ("company", "authorized"),
    extra_lines: str = "",
) -> str:
    """渲染一份完整的 TOML 配置文本（全部段落；与示例文件同结构）。"""
    codes = {"CNY": "RMB"} if currency_codes is None else dict(currency_codes)
    names = {"CNY": "人民币"} if currency_names is None else dict(currency_names)
    expense = {"CAT-TRV": "560101"} if expense_map is None else dict(expense_map)
    expense_names = (
        {"CAT-TRV": "管理费用-差旅费"}
        if expense_name_map is None
        else dict(expense_name_map)
    )

    lines = [
        "[template]",
        f"path = {q(template_path)}",
        f"sha256 = {q(template_sha)}",
        f"worksheet = {q(worksheet)}",
        f"layout = {q(layout)}",
        f"template_id = {q(template_id)}",
        f"template_version = {q(template_version)}",
        "",
        "[scope]",
        f"statuses = {q(list(statuses))}",
        f"date_basis = {q(date_basis)}",
        "",
        "[voucher]",
        f"voucher_word = {q(voucher_word)}",
        f"preparer = {q(preparer)}",
        f"preparer_literal = {q(preparer_literal)}",
        "",
        "[summary]",
        f"template = {q(summary_template)}",
        f"max_length = {q(summary_max_length)}",
        "",
        "[accounts]",
        f"expense_map = {q(expense)}",
        f"expense_name_map = {q(expense_names)}",
        f"expense_default_code = {q(expense_default_code)}",
        f"expense_default_name = {q(expense_default_name)}",
        "",
        "[tax]",
        f"split = {q(tax_split)}",
        f"account_code = {q(tax_account_code)}",
        f"account_name = {q(tax_account_name)}",
        f"split_categories = {q(list(tax_split_categories))}",
        "",
        "[credit]",
        f"account_code = {q(credit_code)}",
        f"account_name = {q(credit_name)}",
        "",
        "[currency]",
        f"rate = {q(currency_rate)}",
        f"codes = {q(codes)}",
        f"names = {q(names)}",
        "",
        "[auxiliary]",
        f"mode = {q(aux_mode)}",
        f"department_class = {q(aux_department_class)}",
        f"counterparty_class = {q(aux_counterparty_class)}",
        f"counterparty_code = {q(aux_counterparty_code)}",
        f"counterparty_name = {q(aux_counterparty_name)}",
        "",
        "[invoice_policy]",
        f"attachment_count_basis = {q(attachment_basis)}",
        f"on_verified_fail = {q(on_verified_fail)}",
        f"on_suspected_duplicate = {q(on_suspected_duplicate)}",
        "",
        "[permissions]",
        f"resource = {q(permission_resource)}",
        f"action = {q(permission_action)}",
        f"scopes = {q(list(permission_scopes))}",
    ]
    if credit_by_status is not None:
        lines.append("")
        lines.append("[credit.by_status]")
        for status, account in credit_by_status.items():
            lines.append(f"{status} = {q(dict(account))}")
    if extra_lines:
        lines.append("")
        lines.append(extra_lines)
    return "\n".join(lines) + "\n"


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def make_config(tmp_path: Path, **overrides: object) -> ExportConfig:
    """在 ``tmp_path`` 造出合成模板 + 配置并装载，返回 :class:`ExportConfig`。"""
    template_path = tmp_path / "synthetic-template.xlsx"
    sha = build_template_xlsx(
        template_path,
        headers=overrides.pop("headers", None),  # type: ignore[arg-type]
        sheet_name=str(overrides.pop("sheet_name", WORKSHEET_ENTRY_ROWS)),
    )
    overrides.setdefault("template_path", str(template_path))
    overrides.setdefault("template_sha", sha)
    text = build_config_toml(**overrides)  # type: ignore[arg-type]
    config_path = write_config(tmp_path / "ajaimgr.toml", text)
    return load_export_config(config_path)
