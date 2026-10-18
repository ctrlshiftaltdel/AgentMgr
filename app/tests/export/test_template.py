"""模板装载测试（纯逻辑；合成模板，**不提交真实模板**）。

覆盖 Issue #53 验收标准第 3 条：

* 读取 ``录入数据`` 工作表（openpyxl）；
* SHA-256 与配置一致、工作表存在、31 列表头逐列符合登记结构；
* 任一不符 → 抛错且**不产出文件**（``load_template`` 只返回结构事实）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.export.errors import (
    CODE_TEMPLATE_FINGERPRINT_MISMATCH,
    CODE_TEMPLATE_HEADER_MISMATCH,
    CODE_TEMPLATE_MISSING,
    CODE_UNSUPPORTED_LAYOUT,
    CODE_WORKSHEET_MISSING,
    ExportTemplateError,
)
from app.export.template import (
    ENTRY_ROWS_HEADERS,
    WORKSHEET_ENTRY_ROWS,
    load_template,
    normalize_header,
)

from _helpers import build_template_xlsx, make_config


def test_loads_synthetic_template(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    loaded = load_template(config)
    assert loaded.worksheet == WORKSHEET_ENTRY_ROWS
    assert loaded.layout == "entry_rows"
    assert loaded.column_count == 31
    assert loaded.sha256 == config.template.sha256
    assert loaded.template_version == 1


def test_sha256_mismatch_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path, template_sha="a" * 64)
    with pytest.raises(ExportTemplateError) as excinfo:
        load_template(config)
    assert excinfo.value.code == CODE_TEMPLATE_FINGERPRINT_MISMATCH


def test_missing_template_file_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path, template_path=str(tmp_path / "absent.xlsx"))
    with pytest.raises(ExportTemplateError) as excinfo:
        load_template(config)
    assert excinfo.value.code == CODE_TEMPLATE_MISSING


def test_missing_worksheet_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path, sheet_name="其它表")
    with pytest.raises(ExportTemplateError) as excinfo:
        load_template(config)
    assert excinfo.value.code == CODE_WORKSHEET_MISSING


def test_header_mismatch_fails_closed(tmp_path: Path) -> None:
    headers = list(ENTRY_ROWS_HEADERS)
    headers[0] = "凭证字X"
    template_path = tmp_path / "wrong-header.xlsx"
    sha = build_template_xlsx(template_path, headers=headers)
    config = make_config(
        tmp_path, template_path=str(template_path), template_sha=sha
    )
    with pytest.raises(ExportTemplateError) as excinfo:
        load_template(config)
    assert excinfo.value.code == CODE_TEMPLATE_HEADER_MISMATCH


def test_short_header_row_fails_closed(tmp_path: Path) -> None:
    template_path = tmp_path / "short-header.xlsx"
    sha = build_template_xlsx(template_path, headers=list(ENTRY_ROWS_HEADERS)[:10])
    config = make_config(
        tmp_path, template_path=str(template_path), template_sha=sha
    )
    with pytest.raises(ExportTemplateError) as excinfo:
        load_template(config)
    assert excinfo.value.code == CODE_TEMPLATE_HEADER_MISMATCH


def test_unsupported_layout_fails_closed(tmp_path: Path) -> None:
    """布局不支持时先于文件读取失败（不静默降级为默认格式）。"""
    import dataclasses

    config = make_config(tmp_path)
    patched = dataclasses.replace(
        config, template=dataclasses.replace(config.template, layout="pz")
    )
    with pytest.raises(ExportTemplateError) as excinfo:
        load_template(patched)
    assert excinfo.value.code == CODE_UNSUPPORTED_LAYOUT


def test_header_normalization_tolerates_template_line_breaks() -> None:
    """``_x000D_`` / CR / LF / TAB 与连续多空格归一化后再比对（§8.2 口径）。"""
    assert normalize_header("余额方向 D/C(自动补充)") == "余额方向 D/C(自动补充)"
    assert normalize_header("凭证_x000D_日期") == "凭证 日期"
    assert normalize_header("借方\r\n金额") == "借方 金额"
    assert normalize_header("  摘  要\t") == "摘 要"
    assert normalize_header(None) == ""


def test_header_normalization_matches_template_with_line_breaks(
    tmp_path: Path,
) -> None:
    """带换行的模板表头经归一化后仍能通过校验（真实模板的换行差异）。"""
    headers = list(ENTRY_ROWS_HEADERS)
    headers[3] = "凭证摘要_x000D_"
    template_path = tmp_path / "normalized.xlsx"
    sha = build_template_xlsx(template_path, headers=headers)
    config = make_config(
        tmp_path, template_path=str(template_path), template_sha=sha
    )
    loaded = load_template(config)
    assert loaded.column_count == 31
