"""模板装载：读取目标工作表 + SHA-256 指纹与表头结构校验（按布局）。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md``

* §2.2.9（旧模板 ``录入数据``：表头行 1、数据起始行 2、31 列 A–AE）；
* §5.1（旧布局 31 列逐列映射）；
* §5.6（**新布局** ``voucher_template``：「凭证模版」单表 25 列 A–Y，Issue #71）；
* §6.3 fail closed #10：「模板缺失、模板 SHA-256 与登记的 ``template_version``
  不符，或目标工作表不存在 → **禁止静默降级为默认格式**」；
* §7.2（模板为运行时装载的受控产物，**不入库**）；
* ``docs/export-templates/template-structure.json``（旧模板纯结构 sidecar）与
  ``docs/export-templates/voucher-template-structure.json``（新模板纯结构 sidecar）。

**布局登记**：每个布局登记一组**按列序排列**的表头（1 基列号 = 表头下标 + 1）。
装载时按 ``config.template.layout`` 取登记表头逐列比对；**不按表头名定位**，也不
做模糊匹配（见 §2.3 R3/R4）。

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
    "LAYOUT_COLUMN_INDEX",
    "LAYOUT_ENTRY_ROWS",
    "LAYOUT_HEADERS",
    "LAYOUT_VOUCHER_TEMPLATE",
    "LAYOUT_WORKSHEETS",
    "VOUCHER_TEMPLATE_HEADERS",
    "WORKSHEET_ENTRY_ROWS",
    "WORKSHEET_VOUCHER_TEMPLATE",
    "LoadedTemplate",
    "column_index",
    "layout_column_index",
    "load_template",
    "normalize_header",
]

#: 首个支持布局：一行 = 一条分录（旧模板 ``录入数据``，31 列）。
LAYOUT_ENTRY_ROWS = "entry_rows"

#: 旧布局的目标工作表名（模板自带的工具中转表）。
WORKSHEET_ENTRY_ROWS = "录入数据"

#: 新布局（Issue #71）：单表 25 列，一行 = 一条分录，按列号定位。
LAYOUT_VOUCHER_TEMPLATE = "voucher_template"

#: 新布局的目标工作表名（新模板《凭证导入模板-20261019》的单表）。
WORKSHEET_VOUCHER_TEMPLATE = "凭证模版"

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

#: ``凭证模版`` 的 25 列表头（按列序 A–Y；Issue #71 新布局）。
#:
#: 取值逐字来自 ``docs/export-templates/voucher-template-structure.json`` 中
#: ``凭证模版`` 的 ``columns[].header``。**按列号定位**：下标 + 1 即 1 基列号；
#: 任何列序或表头变化都属于**模板升版**，必须同时更新此处与 sidecar。
VOUCHER_TEMPLATE_HEADERS: tuple[str, ...] = (
    "日期",
    "凭证字",
    "凭证号",
    "附件数",
    "分录序号",
    "摘要",
    "科目代码",
    "科目名称",
    "借方金额",
    "贷方金额",
    "客户",
    "供应商",
    "职员",
    "项目",
    "部门",
    "存货",
    "自定义辅助核算类别",
    "自定义辅助核算编码",
    "自定义辅助核算类别1",
    "自定义辅助核算编码1",
    "数量",
    "单价",
    "原币金额",
    "币别",
    "汇率",
)

#: 布局标识 → 按列序排列的表头（1 基列号 = 下标 + 1）。
#:
#: 新增布局必须在此登记，否则 :func:`load_template` 以 ``unsupported_layout``
#: 失败（不静默降级）。
LAYOUT_HEADERS: dict[str, tuple[str, ...]] = {
    LAYOUT_ENTRY_ROWS: ENTRY_ROWS_HEADERS,
    LAYOUT_VOUCHER_TEMPLATE: VOUCHER_TEMPLATE_HEADERS,
}

#: 布局标识 → 该布局在模板中的默认工作表名（sidecar 登记值）。
#:
#: **仅供文档/测试引用**：实际装载使用 ``config.template.worksheet``（部署方持有
#: 的模板可能对工作表重命名，但只要表头逐列一致即视为同一布局）。
LAYOUT_WORKSHEETS: dict[str, str] = {
    LAYOUT_ENTRY_ROWS: WORKSHEET_ENTRY_ROWS,
    LAYOUT_VOUCHER_TEMPLATE: WORKSHEET_VOUCHER_TEMPLATE,
}

#: 布局标识 → （列名 → 1 基列号）。
LAYOUT_COLUMN_INDEX: dict[str, dict[str, int]] = {
    layout: {name: index for index, name in enumerate(headers, start=1)}
    for layout, headers in LAYOUT_HEADERS.items()
}

#: 旧布局的列名 → 1 基列号（向后兼容别名，等价于
#: ``LAYOUT_COLUMN_INDEX[LAYOUT_ENTRY_ROWS]``）。
COLUMN_INDEX: dict[str, int] = LAYOUT_COLUMN_INDEX[LAYOUT_ENTRY_ROWS]

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
    """返回旧布局（``entry_rows``）的 1 基列号；未登记的表头名抛 ``KeyError``。"""
    return COLUMN_INDEX[name]


def layout_column_index(layout: str, name: str) -> int:
    """返回指定布局中 ``name`` 的 1 基列号；布局或列名未登记抛 ``KeyError``。"""
    return LAYOUT_COLUMN_INDEX[layout][name]


def _fail(code: str, message: str) -> ExportTemplateError:
    return ExportTemplateError(code, message)


def load_template(config: ExportConfig) -> LoadedTemplate:
    """读取并校验模板；任一项不符即 fail closed。

    校验顺序（**先布局、再指纹、后结构**）：

    1. ``layout`` 必须是本实现登记的布局（未登记不静默降级）；
    2. 模板文件可读（缺失/不可读 → ``template_missing``）；
    3. 文件字节的 SHA-256 必须与 ``template.sha256`` 一致
       （不符 → ``template_fingerprint_mismatch``）；
    4. 工作簿中存在 ``template.worksheet`` 工作表（缺失 → ``worksheet_missing``）；
    5. 第 1 行的表头经 :func:`normalize_header` 归一化后必须逐列等于该布局登记的
       表头（列数与列序同时校验；不符 → ``template_header_mismatch``）。

    返回 :class:`LoadedTemplate`，其中**不含**模板内容与任何业务数据。
    """
    expected_headers = LAYOUT_HEADERS.get(config.template.layout)
    if expected_headers is None:
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
        expected = list(expected_headers)
        if actual != expected:
            raise _fail(
                CODE_TEMPLATE_HEADER_MISMATCH,
                f"worksheet {worksheet!r} header row does not match the registered "
                f"layout {config.template.layout!r} "
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
        column_count=len(expected_headers),
    )
