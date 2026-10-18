"""写出层：单元格注入防护（§6.4）+ **字节可复现**的 xlsx 渲染。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md``

* §6.4「单元格注入与格式」——文本以 ``=``/``+``/``-``/``@`` 开头必须转义；只产
  ``.xlsx``、不产 CSV、**不使用宏**；金额写数值单元格（2 位小数）、日期写日期型
  单元格、整数列写整数；
* §6.5「幂等与可复现」——导出文件**不得**写入导出时间戳，否则字节不可复现。

**可复现性实现说明（实现口径澄清，已记录进 §9）**：``openpyxl`` 默认会在
``docProps/core.xml`` 写入 ``created``/``modified`` 时间戳，并给每个 zip 条目写入
本地时间戳，导致同一内容两次保存的字节不同。本模块因此：

1. 把工作簿属性 ``created``/``modified`` 固定为一个常量时间；
2. 保存后**重新打包** zip，把所有条目的时间戳固定为常量。

两步都只影响元数据，不改变单元格内容；因此同一批数据 + 同一模板的导出文件字节
**可复现**（manifest 的 ``file.sha256`` 因而可复现）。
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

from openpyxl import Workbook

from app.expense.money import quantize
from app.export.errors import (
    CODE_CELL_INJECTION_UNESCAPED,
    ExportValidationError,
    ValidationFailure,
)
from app.export.mapping import EntryRow
from app.export.template import ENTRY_ROWS_HEADERS, WORKSHEET_ENTRY_ROWS

__all__ = [
    "DANGEROUS_PREFIXES",
    "XLSX_MEDIA_TYPE",
    "escape_text",
    "guard_no_unescaped",
    "render_entry_rows",
]

#: 会被电子表格当作公式 / 外部引用起点的危险前缀（§6.4）。
DANGEROUS_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")

#: xlsx 的媒体类型（与 ``app.documents.storage.XLSX_MIME`` 同值）。
XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

#: 固定时间戳（元数据用；**不含真实导出时间**，见模块 docstring）。
_FIXED_DATETIME = datetime(1980, 1, 1, 0, 0, 0, tzinfo=UTC)
_FIXED_ZIP_DATETIME = (1980, 1, 1, 0, 0, 0)

#: 日期单元格的显示格式（§4.4：显示 ``YYYY-MM-DD`` 而非裸序列号）。
_DATE_FORMAT = "yyyy-mm-dd"

#: ``docProps/core.xml`` 中 ``dcterms:created`` / ``dcterms:modified`` 的固定时间戳
#: （使导出文件不含真实导出时间，字节可复现）。
_CORE_TIMESTAMP_RE = re.compile(
    rb"<dcterms:(created|modified)\b[^>]*>[^<]*</dcterms:\1>"
)
_FIXED_CORE_TIMESTAMP = (
    rb'<dcterms:\1 xsi:type="dcterms:W3CDTF">1980-01-01T00:00:00Z</dcterms:\1>'
)


def escape_text(value: str) -> str:
    """转义以危险前缀开头的文本（前缀单引号，§6.4）。

    摘要、科目名称、核算项目名称等**全部文本单元格**都要过这道闸。
    """
    if value and value[0] in DANGEROUS_PREFIXES:
        return "'" + value
    return value


def _cell_value(value: object) -> object:
    """把一行的单元格值转换成 openpyxl 可承载的类型。"""
    if value is None:
        return None
    if isinstance(value, str):
        return escape_text(value)
    if isinstance(value, Decimal):
        # openpyxl 无法承载 Decimal；只在**写入单元格的边界处**转 float，
        # 上游计算全程保持 Decimal（与 ``app.documents.export`` 同口径）。
        return float(quantize(value))
    return value


def guard_no_unescaped(rows: list[tuple[object, ...]]) -> None:
    """fail closed #12：确认没有任何文本单元格带**未转义**的危险前缀。

    这是纵深防御：正常路径上 :func:`escape_text` 已处理全部文本；若未来有人绕过
    转义直接构造单元格，本闸门会拒绝产出文件，而不是把公式写进导出文件。
    """
    for row in rows:
        for value in row:
            if isinstance(value, str) and value and value[0] in DANGEROUS_PREFIXES:
                raise ExportValidationError(
                    [
                        ValidationFailure(
                            rule="fail_closed_12",
                            code=CODE_CELL_INJECTION_UNESCAPED,
                            message=(
                                "text cell starts with an unescaped dangerous prefix"
                            ),
                        )
                    ]
                )


def _deterministic_zip(raw: bytes) -> bytes:
    """把 openpyxl 产出的 zip 重新打包为**时间戳固定**的 zip。

    ``openpyxl`` 默认会在 ``docProps/core.xml`` 写入 ``created``/``modified``
    时间戳（且部分版本会忽略调用方设置），并给每个 zip 条目写入本地时间戳；这里
    把 core.xml 的两个时间戳改写为常量，并把所有 zip 条目时间戳固定，使同一内容
    两次保存字节一致（§6.5）。
    """
    source = zipfile.ZipFile(io.BytesIO(raw))
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                data = source.read(name)
                if name == "docProps/core.xml":
                    data = _CORE_TIMESTAMP_RE.sub(_FIXED_CORE_TIMESTAMP, data)
                info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_DATETIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(info, data)
    finally:
        source.close()
    return buffer.getvalue()


def render_entry_rows(entries: list[EntryRow]) -> bytes:
    """把分录行渲染为 ``录入数据`` 工作表的 xlsx 字节（**无宏、无时间戳**）。

    表头行逐列取自 :data:`app.export.template.ENTRY_ROWS_HEADERS`；每条分录一行，
    列序与模板 A–AE 一致。金额为数值单元格、日期为日期型单元格（``yyyy-mm-dd``）、
    ``凭证号``/``附件数``/``汇率`` 为整数型单元格。
    """
    # 先转义（§6.4），再用纵深防御闸门复核转义结果（fail closed #12）。
    rows: list[tuple[object, ...]] = [
        tuple(_cell_value(value) for value in entry.cells()) for entry in entries
    ]
    guard_no_unescaped(rows)

    workbook = Workbook()
    workbook.properties.created = _FIXED_DATETIME.replace(tzinfo=None)
    workbook.properties.modified = _FIXED_DATETIME.replace(tzinfo=None)
    sheet = workbook.worksheets[0]
    sheet.title = WORKSHEET_ENTRY_ROWS

    sheet.append(list(ENTRY_ROWS_HEADERS))
    for row in rows:
        sheet.append(list(row))

    _apply_date_format(sheet, len(rows))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return _deterministic_zip(buffer.getvalue())


def _apply_date_format(sheet, row_count: int) -> None:
    """把 ``凭证日期``（C 列）设为日期显示格式（写日期型单元格，§4.4）。"""
    for row_index in range(2, row_count + 2):
        cell = sheet.cell(row=row_index, column=3)
        if isinstance(cell.value, (date, datetime)):
            cell.number_format = _DATE_FORMAT
