"""``voucher_template`` 布局适配器（新模板「凭证模版」，25 列 A–Y，Issue #71）。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md``

* §5.6（新布局逐列映射，25 列）；
* §5.5（派生规则集中实现：凭证粒度、分录顺序、金额口径、科目解析、工具列留空）；
* ``docs/export-templates/voucher-template-structure.json``（纯结构 sidecar；
  本模块的表头来源 :data:`app.export.template.VOUCHER_TEMPLATE_HEADERS` 即取自其
  ``凭证模版`` 的 ``columns``）。

**纯逻辑**：不导入数据库/ORM，也不读写文件系统。输入是
:class:`~app.export.mapping.EntryRow` 列表 + :class:`~app.export.config.ExportConfig`，
输出是按**列号**展开的单元格元组列表。

**按列号定位**：本布局**不按表头名写入**，列序由
:data:`app.export.template.VOUCHER_TEMPLATE_HEADERS` 固定（A–Y）；表头名只在
配置层用于选择「命名辅助核算列」。

**安全默认**：无权威数据源的列一律留空——``数量``/``单价``（本系统无数据源）、
以及未在 ``[auxiliary.named_columns]`` 登记的命名辅助核算列。
"""

from __future__ import annotations

from app.export.config import ExportConfig
from app.export.mapping import EntryRow
from app.export.template import VOUCHER_TEMPLATE_HEADERS

__all__ = [
    "NAMED_AUXILIARY_HEADERS",
    "voucher_template_cells",
    "voucher_template_rows",
]

#: 命名辅助核算列的表头（新布局 K–T 共 10 列），按列序。
#:
#: 与 :data:`app.export.config.VOUCHER_TEMPLATE_NAMED_COLUMNS` 是同一集合的
#: 有序视图（``test_voucher_layout`` 有防漂移断言）。
NAMED_AUXILIARY_HEADERS: tuple[str, ...] = VOUCHER_TEMPLATE_HEADERS[10:20]


def _named_value(entry: EntryRow, config: ExportConfig, header: str) -> str | None:
    """解析一个命名辅助核算列的取值（未登记 / ``blank`` → ``None``）。"""
    spec = config.auxiliary.named_columns.get(header)
    if spec is None or spec.source == "blank":
        return None
    if spec.source == "department":
        return entry.department_name
    return spec.value  # source == "literal"（配置装载期已保证 value 非空）


def voucher_template_cells(
    entry: EntryRow, *, sequence: int, config: ExportConfig
) -> tuple[object, ...]:
    """把一条分录展开为 ``凭证模版`` 的 25 个单元格值（按列号 A–Y）。

    ``sequence`` 是该分录在**本凭证内**的 1 基序号（``分录序号`` 列）。
    凭证号在 ``voucher.number_mode = "blank"`` 时写空，否则写 ``entry.voucher_no``
    （已在映射层按 ``voucher.number_start`` 编号）。
    """
    voucher_no = None if config.voucher.number_mode == "blank" else entry.voucher_no
    return (
        entry.entry_date,  # A 日期
        entry.voucher_word,  # B 凭证字
        voucher_no,  # C 凭证号
        entry.attachment_count,  # D 附件数
        sequence,  # E 分录序号
        entry.summary,  # F 摘要
        entry.account_code,  # G 科目代码
        entry.account_name,  # H 科目名称
        entry.debit,  # I 借方金额
        entry.credit,  # J 贷方金额
        _named_value(entry, config, "客户"),  # K
        _named_value(entry, config, "供应商"),  # L
        _named_value(entry, config, "职员"),  # M
        _named_value(entry, config, "项目"),  # N
        _named_value(entry, config, "部门"),  # O
        _named_value(entry, config, "存货"),  # P
        _named_value(entry, config, "自定义辅助核算类别"),  # Q
        _named_value(entry, config, "自定义辅助核算编码"),  # R
        _named_value(entry, config, "自定义辅助核算类别1"),  # S
        _named_value(entry, config, "自定义辅助核算编码1"),  # T
        None,  # U 数量：无数据源（安全默认留空）
        None,  # V 单价：无数据源（安全默认留空）
        entry.original_amount,  # W 原币金额
        entry.currency_code,  # X 币别
        entry.exchange_rate,  # Y 汇率
    )


def voucher_template_rows(
    entries: list[EntryRow], config: ExportConfig
) -> list[tuple[object, ...]]:
    """把分录行列表展开为 ``凭证模版`` 的行（每条分录一行，按列号）。

    ``分录序号`` 在**同一凭证内**从 1 起递增；分录顺序沿用映射层的确定性顺序
    （借方费用 → 税费 → 贷方），因此输出字节可复现（§6.5）。
    """
    counters: dict[int, int] = {}
    rows: list[tuple[object, ...]] = []
    for entry in entries:
        counters[entry.voucher_no] = counters.get(entry.voucher_no, 0) + 1
        rows.append(
            voucher_template_cells(
                entry, sequence=counters[entry.voucher_no], config=config
            )
        )
    return rows
