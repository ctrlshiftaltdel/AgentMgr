"""报销单导出：PDF（reportlab + 内置 CJK 字体）与明细 XLSX（openpyxl）。

设计依据：

* ``templates/PLAN.md`` §3「文档生成：DOCX/XLSX/PDF 模板服务」（模板版本可追踪）、
  §5.2「财务复核、付款状态记录、报销单 PDF 与明细 XLSX」、§8「金额采用固定精度
  decimal，不用浮点数保存财务金额」；
* ``docs/PROJECT-CHARTER.md`` 强制原则 6——金额计算必须由确定性程序驱动。

本模块的设计约束：

* **纯函数**：不读写数据库、不读写文件系统。只接受鸭子类型对象（只要具备所需
  属性即可，测试用 ``types.SimpleNamespace``，不强制 ORM 实例），把渲染结果写进
  内存 ``BytesIO`` 后返回 ``bytes``；因此可脱离数据库独立单测。
* **内容模型与渲染分离**：``claim_pdf_lines`` 负责把报销单整理成文本行，
  ``render_claim_pdf`` 与 ``render_claim_xlsx`` 只负责把内容渲染成字节；两者共用
  同一份文本行，保证 PDF 文本、XLSX 表头与测试断言一致。
* **金额一律用 ``Decimal`` 格式化**，绝不经过浮点。唯一的例外是写入 XLSX 单元格
  边界处的一次显式浮点转换（见 ``_amount_for_cell``）：openpyxl 无法承载
  ``Decimal``；数据库侧与 PDF 侧仍全程 ``Decimal``。
* PDF 中文使用 reportlab 内置的 CID 字体 ``STSong-Light``（``UnicodeCIDFont``），
  **不依赖任何外部字体文件**。

本模块**不导入任何数据库或 ORM 依赖**。
"""

from __future__ import annotations

import io
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

__all__ = [
    "CJK_FONT_NAME",
    "MONEY_EXPONENT",
    "XLSX_HEADERS",
    "claim_pdf_lines",
    "render_claim_pdf",
    "render_claim_xlsx",
]

#: 金额统一保留 2 位小数（与 ``app.expense.money`` 口径一致）。
MONEY_EXPONENT = Decimal("0.01")

#: reportlab 内置的简体中文字体（CID 字体，无需外部字体文件）。
CJK_FONT_NAME = "STSong-Light"

#: XLSX 明细表头（日期/描述/金额/税额）。
XLSX_HEADERS = ("日期", "描述", "金额", "税额")

# reportlab 注册字体是进程级副作用，注册一次即可（幂等保护）。
_font_registered = False


# --- 金额处理（Decimal，禁止浮点） -------------------------------------------


def _to_decimal(amount: Any) -> Decimal:
    """把 ``amount`` 转为 ``Decimal``；浮点一律拒绝。

    与 ``app.expense.money.to_decimal`` 同口径：``float`` 无法无损表达大多数十进制
    金额，接受它会静默引入二进制误差，因此显式拒绝并抛 ``TypeError``。
    """
    if isinstance(amount, bool):  # bool 是 int 子类，用作金额几乎总是笔误。
        raise TypeError("bool is not a valid money value")
    if isinstance(amount, float):
        raise TypeError(
            "float is not allowed for money; use Decimal/int/str to keep "
            "fixed-point precision"
        )
    if isinstance(amount, Decimal):
        return amount
    if isinstance(amount, (int, str)):
        return Decimal(amount)
    raise TypeError(f"unsupported money type: {type(amount).__name__}")


def _format_amount(amount: Any) -> str:
    """把金额量化到 2 位小数并格式化为字符串（全程 ``Decimal``）。

    使用 ``Decimal.quantize`` + 格式化，**不经过浮点**；因此 ``0.10 + 0.20`` 的
    合计会显示为 ``'0.30'`` 而不是 ``'0.30000000000000004'``。
    """
    value = _to_decimal(amount).quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)
    return f"{value:.2f}"


def _line_date(line: Any) -> str:
    """返回明细行的日期文本（``None`` 渲染为空串）。"""
    value = getattr(line, "expense_date", None)
    return "" if value is None else str(value)


# --- 内容模型（供 PDF 渲染与测试断言共用） ------------------------------------


def claim_pdf_lines(
    claim: Any,
    lines: Any,
    *,
    employee_name: str,
    company_name: str,
) -> list[str]:
    """把报销单整理成文本行列表。

    至少包含：公司名、报销单号（``claim.claim_no``）、员工名、币种
    （``claim.currency``）、状态（``claim.status``）、每条明细（日期/描述/金额/
    税额）与合计（``claim.total_amount``）。金额一律用 ``Decimal`` 格式化。

    ``claim`` 与 ``lines`` 采用鸭子类型：只要对象具备所需属性即可。
    """
    result: list[str] = [
        f"公司: {company_name}",
        f"报销单号: {claim.claim_no}",
        f"员工: {employee_name}",
        f"币种: {claim.currency}",
        f"状态: {claim.status}",
        "明细:",
    ]
    for line in lines:
        result.append(
            f"{_line_date(line)}  {line.description}  "
            f"金额: {_format_amount(line.amount)}  "
            f"税额: {_format_amount(line.tax_amount)}"
        )
    result.append(f"合计: {_format_amount(claim.total_amount)}")
    return result


# --- PDF 渲染（reportlab，内置 CJK 字体） -------------------------------------


def _ensure_cjk_font() -> None:
    """注册 reportlab 内置的 ``STSong-Light`` CID 字体（幂等）。"""
    global _font_registered
    if not _font_registered:
        pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT_NAME))
        _font_registered = True


def render_claim_pdf(
    claim: Any,
    lines: Any,
    *,
    employee_name: str,
    company_name: str,
) -> bytes:
    """用 reportlab 生成报销单 PDF，返回字节（内存 ``BytesIO``）。

    内容取自 ``claim_pdf_lines``；中文使用内置 ``STSong-Light`` 字体渲染，无需外部
    字体文件。超出一页时自动换页。
    """
    _ensure_cjk_font()

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin_x = 40.0
    line_height = 16.0
    top_y = height - 50.0
    bottom_y = 50.0

    pdf.setFont(CJK_FONT_NAME, 11)
    y = top_y
    for text in claim_pdf_lines(
        claim, lines, employee_name=employee_name, company_name=company_name
    ):
        if y < bottom_y:
            pdf.showPage()
            pdf.setFont(CJK_FONT_NAME, 11)
            y = top_y
        pdf.drawString(margin_x, y, text)
        y -= line_height

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# --- XLSX 渲染（openpyxl） ----------------------------------------------------


def _amount_for_cell(amount: Any) -> float:
    """把 ``Decimal`` 金额写入 XLSX 数值单元格。

    openpyxl 无法承载 ``Decimal``，因此**只在写入单元格的边界处做一次显式浮点
    转换**（本模块中唯一的浮点调用）。所有上游计算仍为 ``Decimal``；数据库侧与
    PDF 侧也保持 ``Decimal``，不受此处影响。
    """
    value = _to_decimal(amount).quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)
    return float(value)


def _total_tax(lines: Any) -> Decimal:
    """对明细税额做精确 ``Decimal`` 求和并量化到 2 位小数。"""
    total = Decimal("0")
    for line in lines:
        total += _to_decimal(line.tax_amount)
    return total.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def render_claim_xlsx(claim: Any, lines: Any) -> bytes:
    """用 openpyxl 生成明细工作簿，返回字节（内存 ``BytesIO``）。

    首行为表头（日期/描述/金额/税额），随后每条明细一行，最后一行为合计
    （金额取 ``claim.total_amount``，税额取明细税额之和）。金额单元格写为数值。
    """
    workbook = Workbook()
    sheet = workbook.worksheets[0]
    sheet.title = "报销明细"

    sheet.append(list(XLSX_HEADERS))
    for line in lines:
        sheet.append(
            [
                _line_date(line),
                line.description,
                _amount_for_cell(line.amount),
                _amount_for_cell(line.tax_amount),
            ]
        )
    sheet.append(
        [
            "合计",
            "",
            _amount_for_cell(claim.total_amount),
            _amount_for_cell(_total_tax(lines)),
        ]
    )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
