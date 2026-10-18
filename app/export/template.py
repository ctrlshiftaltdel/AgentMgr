"""模板装载：读取 ``录入数据`` 工作表 + SHA-256 指纹与表头结构校验。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md``

* §2.2.9（``录入数据``：表头行 1、数据起始行 2、31 列 A–AE、一行 = 一条分录）；
* §5.1（31 列逐列映射）；
* §6.3 fail closed #10：「模板缺失、模板 SHA-256 与登记的 ``template_version``
  不符，或目标工作表不存在 → **禁止静默降级为默认格式**」；
* §7.2（模板为运行时装载的受控产物，**不入库**）；
* ``docs/export-templates/template-structure.json``（纯结构 sidecar；本模块的
  :data:`ENTRY_ROWS_HEADERS` 即取自其中 ``录入数据`` 的 ``columns``）。

**不变量**：模板文件是**仓库外的运行时受控产物**。本模块只读它，校验通过才返回；
任何不符（缺文件 / 哈希不符 / 缺工作表 / 表头或列数不符）都抛
:class:`~app.export.errors.ExportTemplateError`，**不产出任何导出文件**，也绝不
回退到内置默认格式。错误文本不回显配置取值。
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from app.export.config import ExportConfig
from app.export.errors import (
    CODE_TEMPLATE_FINGERPRINT_MISMATCH,
    CODE_TEMPLATE_HEADER_MISMATCH,
    CODE_TEMPLATE_MISSING,
    CODE_UNSUPPORTED_LAYOUT,
    CODE_WORKSHEET_MISSING,
    ExportTemplateError,
)

__all__ = [
    "ENTRY_ROWS_HEADERS",
    "LAYOUT_ENTRY_ROWS",
    "WORKSHEET_ENTRY_ROWS",
    "LoadedTemplate",
    "column_index",
    "load_template",
    "normalize_header",
]

#: 首个支持布局：一行 = 一条分录。
LAYOUT_ENTRY_ROWS = "entry_rows"

#: 目标工作表名（模板自带的工具中转表）。
WORKSHEET_ENTRY_ROWS = "录入数据"

#: ``录入数据`` 的 31 列表头（按列序 A–AE）。
#:
#: 取值逐字来自 ``docs/export-templates/template-structure.json`` 中 ``录入数据``
#: 的 ``columns[].header``（即模板原文经 ``_x000D_``/CR/LF/TAB → 单空格归一化后的
#: 文本）。任何列序或表头变化都属于**模板升版**，必须同时更新此处与 sidecar。
ENTRY_ROWS_HEADERS: tuple[str, ...] = (
    "凭证字",
    "凭证号",
    "凭证日期",
    "凭证摘要",
    "科目代码",
    "科目名称",
    "余额方向 D/C(自动补充)",
    "币别代码",
    "币别名称",
    "原币金额",
    "汇率",
    "数量",
    "单价",
    "借方金额",
    "贷方金额",
    "制单人",
    "辅助核算类别A",
    "项目代码A",
    "核算项目名称A",
    "辅助核算类别B",
    "项目代码B",
    "核算项目名称B",
    "辅助核算类别C",
    "项目代码C",
    "核算项目名称C",
    "辅助核算类别D",
    "项目代码D",
    "核算项目名称D",
    "业务编号",
    "附件数",
    "检测错误情况",
)

#: 列名 → 1 基列号（写单元格时用；**不按表头名定位**，见 §2.3 R3/R4）。
COLUMN_INDEX: dict[str, int] = {
    name: index for index, name in enumerate(ENTRY_ROWS_HEADERS, start=1)
}

_EXCEL_ESCAPE_RE = re.compile(r"_x000D_")
_WHITESPACE_RE = re.compile(r"[\r\n\t]+")
_SPACES_RE = re.compile(r" {2,}")


@dataclass(frozen=True)
class LoadedTemplate:
    """一次成功的模板装载结果（指纹 + 结构事实，不含模板内容）。"""

    path: str
    sha256: str
    size_bytes: int
    worksheet: str
    layout: str
    template_id: str
    template_version: int
    column_count: int


def normalize_header(text: object) -> str:
    """把表头文本按 §8.2 的口径归一化后再比对。

    ``_x000D_`` 转义、CR/LF/TAB 折叠为单空格、连续多空格折叠为单空格并去首尾空白
    ——与 sidecar 的 ``header_text_normalization`` 完全一致，避免因模板换行差异
    造成假阴性。
    """
    value = "" if text is None else str(text)
    value = _EXCEL_ESCAPE_RE.sub(" ", value)
    value = _WHITESPACE_RE.sub(" ", value)
    return _SPACES_RE.sub(" ", value).strip()


def column_index(name: str) -> int:
    """返回 1 基列号；未登记的表头名抛 ``KeyError``（调用方只应传已知列名）。"""
    return COLUMN_INDEX[name]


def _fail(code: str, message: str) -> ExportTemplateError:
    return ExportTemplateError(code, message)


def load_template(config: ExportConfig) -> LoadedTemplate:
    """读取并校验模板；任一项不符即 fail closed。

    校验顺序（**先指纹、后结构**）：

    1. ``layout`` 必须是本实现支持的布局（不支持的布局不静默降级）；
    2. 模板文件可读（缺失/不可读 → ``template_missing``）；
    3. 文件字节的 SHA-256 必须与 ``template.sha256`` 一致
       （不符 → ``template_fingerprint_mismatch``）；
    4. 工作簿中存在 ``template.worksheet`` 工作表（缺失 → ``worksheet_missing``）；
    5. 第 1 行的 31 个表头经 :func:`normalize_header` 归一化后必须逐列等于
       :data:`ENTRY_ROWS_HEADERS`（不符 → ``template_header_mismatch``）。

    返回 :class:`LoadedTemplate`，其中**不含**模板内容与任何业务数据。
    """
    if config.template.layout != LAYOUT_ENTRY_ROWS:
        raise _fail(
            CODE_UNSUPPORTED_LAYOUT,
            f"layout {config.template.layout!r} is not implemented in this build",
        )

    template_path = Path(config.template.path)
    try:
        content = template_path.read_bytes()
    except FileNotFoundError:
        raise _fail(
            CODE_TEMPLATE_MISSING,
            f"template file referenced by 'template.path' not found: {template_path}",
        )
    except OSError:
        raise _fail(
            CODE_TEMPLATE_MISSING,
            f"template file referenced by 'template.path' is not readable: "
            f"{template_path}",
        )

    actual_sha = hashlib.sha256(content).hexdigest()
    if actual_sha != config.template.sha256:
        # 只报告「不符」，不回显两个哈希之外的任何配置取值。
        raise _fail(
            CODE_TEMPLATE_FINGERPRINT_MISMATCH,
            "template file does not match the registered SHA-256 fingerprint "
            "(template_version "
            f"{config.template.template_version})",
        )

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - openpyxl raises a variety of types
        raise _fail(
            CODE_TEMPLATE_MISSING,
            "template file is not a readable .xlsx workbook",
        )
    try:
        worksheet = config.template.worksheet
        if worksheet not in workbook.sheetnames:
            raise _fail(
                CODE_WORKSHEET_MISSING,
                f"template workbook has no worksheet named {worksheet!r}",
            )
        sheet = workbook[worksheet]
        header_row = next(
            sheet.iter_rows(min_row=1, max_row=1, values_only=True), ()
        )
        actual = [normalize_header(cell) for cell in header_row]
        # 去掉表头行尾部的空单元格（模板 extent 可能比实际列宽更长）。
        while actual and actual[-1] == "":
            actual.pop()
        expected = list(ENTRY_ROWS_HEADERS)
        if actual != expected:
            raise _fail(
                CODE_TEMPLATE_HEADER_MISMATCH,
                f"worksheet {worksheet!r} header row does not match the registered "
                f"layout {LAYOUT_ENTRY_ROWS!r} "
                f"({len(expected)} columns expected)",
            )
    finally:
        workbook.close()

    return LoadedTemplate(
        path=str(template_path),
        sha256=actual_sha,
        size_bytes=len(content),
        worksheet=worksheet,
        layout=config.template.layout,
        template_id=config.template.template_id,
        template_version=config.template.template_version,
        column_count=len(ENTRY_ROWS_HEADERS),
    )
