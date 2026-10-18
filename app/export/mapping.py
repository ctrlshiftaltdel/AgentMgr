"""映射引擎：把报销单 / 明细行 / 挂账发票投影为 ``录入数据`` 分录行（§5.1 + §5.5）。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md``

* §5.1「目标布局 ``录入数据``（31 列）」的逐列处置；
* §5.5「四种布局共用的派生规则（必须集中实现，不得各表各写一份）」；
* §4.4「金额与日期口径」——金额一律 ``Decimal``（复用 ``app.expense.money``），
  日期写日期型单元格；
* §6.3 fail closed #7（任一科目代码无法解析）与 #9（越界金额）。

**纯逻辑**：本模块不导入数据库/ORM，也不读写文件系统；输入是
:class:`ClaimInput` / :class:`LineInput` / :class:`InvoiceInput`（由服务层从 ORM
行投影而来），输出是 :class:`EntryRow` 列表。因此可脱离数据库单测。

**凭证粒度与分录顺序**（§5.5，字节可复现的前提）：

* 一张报销单 = 一张凭证（``claim_id`` 决定凭证边界）；
* 分录顺序固定：借方费用分录（按 ``line_id``）→ 税费分录（若有，一条聚合）→
  贷方分录（一条）；
* 凭证号在期间内按（首个基准日、``claim_id``）升序从 1 编号。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.expense.money import quantize, sum_amounts
from app.export.config import ExportConfig
from app.export.errors import (
    CODE_ACCOUNT_CODE_UNRESOLVED,
    CODE_CURRENCY_UNMAPPED,
    CODE_PERIOD_CONTAINMENT_FAILED,
    ExportValidationError,
    ValidationFailure,
)

__all__ = [
    "ClaimInput",
    "EntryRow",
    "InvoiceInput",
    "LineInput",
    "basis_date",
    "build_entries",
    "gross_total",
    "net_total",
    "render_summary",
    "should_split_tax",
    "tax_total",
]

#: 列序（1 基）在 :mod:`app.export.template` 的 ``ENTRY_ROWS_HEADERS`` 中定义；
#: 本模块只按字段名组织一行，写出时由 :meth:`EntryRow.cells` 按列序展开。


@dataclass(frozen=True)
class InvoiceInput:
    """一条挂账发票（``expense_invoices`` + 对应查验记录的非敏感投影）。"""

    duplicate_key: str
    amount: Decimal | None
    verification_status: str
    suspected_duplicate: bool
    has_source_file: bool
    # 查验记录（``invoice_verifications``）票面三口径，均可空；E6 用。
    amount_without_tax: Decimal | None = None
    verified_tax_amount: Decimal | None = None
    amount_with_tax: Decimal | None = None


@dataclass(frozen=True)
class LineInput:
    """一条报销明细行（``expense_lines`` + 类别/部门名称投影）。"""

    line_id: int
    expense_date: date
    category_code: str
    category_name: str
    description: str
    amount: Decimal
    tax_amount: Decimal
    department_name: str | None = None

    @property
    def net_amount(self) -> Decimal:
        """不含税金额（派生：``amount - tax_amount``，§4.4）。"""
        return self.amount - self.tax_amount


@dataclass(frozen=True)
class ClaimInput:
    """一张报销单及其明细/发票（凭证边界）。"""

    claim_id: int
    claim_no: str
    currency: str
    status: str
    total_amount: Decimal
    employee_no: str
    lines: tuple[LineInput, ...]
    invoices: tuple[InvoiceInput, ...] = ()
    payment_amount: Decimal | None = None
    payment_date: date | None = None


@dataclass(frozen=True)
class EntryRow:
    """一条 ``录入数据`` 分录行（31 列中本系统负责填写的部分）。"""

    voucher_word: str
    voucher_no: int
    entry_date: date
    summary: str
    account_code: str
    account_name: str | None
    currency_code: str
    currency_name: str | None
    original_amount: Decimal
    exchange_rate: Decimal
    debit: Decimal | None
    credit: Decimal | None
    preparer: str
    business_no: str
    attachment_count: int
    aux_class_a: str | None = None
    aux_code_a: str | None = None
    aux_name_a: str | None = None
    aux_class_b: str | None = None
    aux_code_b: str | None = None
    aux_name_b: str | None = None

    def cells(self) -> tuple[object, ...]:
        """按 ``录入数据`` 列序 A–AE 展开为 31 个单元格值（工具列留空）。"""
        return (
            self.voucher_word,
            self.voucher_no,
            self.entry_date,
            self.summary,
            self.account_code,
            self.account_name,
            None,  # G 余额方向：模板标注「自动补充」，工具列
            self.currency_code,
            self.currency_name,
            self.original_amount,
            self.exchange_rate,
            None,  # L 数量：无数据源
            None,  # M 单价：无数据源
            self.debit,
            self.credit,
            self.preparer,
            self.aux_class_a,
            self.aux_code_a,
            self.aux_name_a,
            self.aux_class_b,
            self.aux_code_b,
            self.aux_name_b,
            None,  # W 辅助核算类别C
            None,  # X 项目代码C
            None,  # Y 核算项目名称C
            None,  # Z 辅助核算类别D
            None,  # AA 项目代码D
            None,  # AB 核算项目名称D
            self.business_no,
            self.attachment_count,
            None,  # AE 检测错误情况：本系统写出前自行校验
        )


def _unresolved(rule: str, message: str, details: dict[str, object]) -> ExportValidationError:
    return ExportValidationError(
        [ValidationFailure(rule=rule, code=CODE_ACCOUNT_CODE_UNRESOLVED, message=message, details=dict(details))]
    )


def basis_date(claim: ClaimInput, date_basis: str) -> date | None:
    """返回该凭证的**基准日**（§5.5 期间与 C 列口径）。

    * ``expense_date``：取明细行 ``expense_date`` 的最小值（费用发生口径）；
    * ``paid_at``：取付款记录 ``paid_at`` 的日期部分（付款口径）。

    无明细/无付款时返回 ``None``（由校验层 fail closed）。
    """
    if date_basis == "paid_at":
        return claim.payment_date
    if not claim.lines:
        return None
    return min(line.expense_date for line in claim.lines)


def render_summary(
    template: str, *, max_length: int, values: dict[str, str]
) -> str:
    """渲染摘要模板并截断到 ``max_length``。

    占位符白名单已在配置装载期校验，这里对缺失字段取空串作纵深防御（不抛异常）。
    """

    class _Safe(dict):
        def __missing__(self, key: str) -> str:  # pragma: no cover - 防御分支
            return ""

    rendered = template.format_map(_Safe(values)).strip()
    if len(rendered) > max_length:
        rendered = rendered[:max_length]
    return rendered


def _resolve_expense_account(
    line: LineInput, config: ExportConfig, claim: ClaimInput
) -> tuple[str, str | None]:
    """解析费用科目代码与名称（D2）；无法解析即 fail closed。"""
    code = config.accounts.expense_map.get(line.category_code)
    name = config.accounts.expense_name_map.get(line.category_code)
    if code is None:
        code = config.accounts.expense_default_code
        name = name if name is not None else config.accounts.expense_default_name
    if not code:
        raise _unresolved(
            "fail_closed_7",
            "expense category has no configured accounting account code",
            {"claim_id": claim.claim_id, "category_code": line.category_code},
        )
    if name is None:
        name = line.category_name or None
    return code, name


def should_split_tax(line: LineInput, config: ExportConfig) -> bool:
    """D3：该明细行是否拆分进项税。默认 ``none``（安全默认：不拆）。"""
    if config.tax.split == "none":
        return False
    if config.tax.split == "all":
        return line.tax_amount > Decimal("0")
    return line.category_code in config.tax.split_categories and line.tax_amount > Decimal("0")


def build_entries(
    claims: list[ClaimInput],
    config: ExportConfig,
    *,
    period: str,
    exporter_employee_no: str | None = None,
) -> list[EntryRow]:
    """把已选中的报销单投影为分录行列表（固定顺序，字节可复现）。

    调用方（服务层）必须已通过 :mod:`app.export.validation` 的全部校验；本函数仍
    会对**科目解析**与**币别映射**这两类「无法产出合法凭证」的情况 fail closed。
    """
    ordered = sorted(
        claims,
        key=lambda claim: (basis_date(claim, config.scope.date_basis) or date.min, claim.claim_id),
    )

    entries: list[EntryRow] = []
    for voucher_no, claim in enumerate(ordered, start=1):
        voucher_date = basis_date(claim, config.scope.date_basis)
        if voucher_date is None:
            # 校验层（validate_claims）应先于本函数拦住这种情况；这里纵深防御，
            # 保证任何路径都以「机器可读的 fail closed」结束，而不是断言失败。
            raise ExportValidationError(
                [
                    ValidationFailure(
                        rule="C4",
                        code=CODE_PERIOD_CONTAINMENT_FAILED,
                        message=(
                            "claim has no basis date for the configured date basis"
                        ),
                        details={
                            "claim_id": claim.claim_id,
                            "date_basis": config.scope.date_basis,
                        },
                    )
                ]
            )
        currency_code = config.currency_code(claim.currency)
        if currency_code is None:
            raise ExportValidationError(
                [
                    ValidationFailure(
                        rule="fail_closed_9",
                        code=CODE_CURRENCY_UNMAPPED,
                        message="claim currency has no configured template mapping",
                        details={"claim_id": claim.claim_id},
                    )
                ]
            )
        currency_name = config.currency_name(claim.currency)
        rate = Decimal(config.currency.rate)
        preparer = _preparer(claim, config, exporter_employee_no)
        attachment_count = _attachment_count(claim, config)

        lines = sorted(claim.lines, key=lambda line: line.line_id)
        tax_total = Decimal("0.00")
        for line in lines:
            code, name = _resolve_expense_account(line, config, claim)
            split = should_split_tax(line, config)
            debit = quantize(line.net_amount) if split else quantize(line.amount)
            if split:
                tax_total += line.tax_amount
            summary = _summary(config, claim, period, line.description)
            entries.append(
                EntryRow(
                    voucher_word=config.voucher.voucher_word,
                    voucher_no=voucher_no,
                    entry_date=line.expense_date
                    if config.scope.date_basis == "expense_date"
                    else voucher_date,
                    summary=summary,
                    account_code=code,
                    account_name=name,
                    currency_code=currency_code,
                    currency_name=currency_name,
                    original_amount=debit,
                    exchange_rate=rate,
                    debit=debit,
                    credit=None,
                    preparer=preparer,
                    business_no=claim.claim_no,
                    attachment_count=attachment_count,
                    **_aux_fields(line, config),
                )
            )

        tax_total = quantize(tax_total)
        if tax_total > Decimal("0.00"):
            tax_code = config.tax.account_code
            if not tax_code:
                raise _unresolved(
                    "fail_closed_7",
                    "tax split is enabled but no tax account code is configured",
                    {"claim_id": claim.claim_id},
                )
            entries.append(
                EntryRow(
                    voucher_word=config.voucher.voucher_word,
                    voucher_no=voucher_no,
                    entry_date=voucher_date,
                    summary=_summary(config, claim, period, ""),
                    account_code=tax_code,
                    account_name=config.tax.account_name,
                    currency_code=currency_code,
                    currency_name=currency_name,
                    original_amount=tax_total,
                    exchange_rate=rate,
                    debit=tax_total,
                    credit=None,
                    preparer=preparer,
                    business_no=claim.claim_no,
                    attachment_count=attachment_count,
                    **_aux_fields(None, config),
                )
            )

        credit = quantize(
            claim.payment_amount
            if claim.payment_amount is not None
            else claim.total_amount
        )
        credit_account = config.credit.account_for(claim.status)
        if credit_account is None or not credit_account.code:
            raise _unresolved(
                "fail_closed_7",
                "no credit account configured for this claim status",
                {"claim_id": claim.claim_id},
            )
        entries.append(
            EntryRow(
                voucher_word=config.voucher.voucher_word,
                voucher_no=voucher_no,
                entry_date=voucher_date,
                summary=_summary(config, claim, period, ""),
                account_code=credit_account.code,
                account_name=credit_account.name,
                currency_code=currency_code,
                currency_name=currency_name,
                original_amount=credit,
                exchange_rate=rate,
                debit=None,
                credit=credit,
                preparer=preparer,
                business_no=claim.claim_no,
                attachment_count=attachment_count,
                **_aux_fields(None, config),
            )
        )

    return entries


def _summary(config: ExportConfig, claim: ClaimInput, period: str, description: str) -> str:
    return render_summary(
        config.summary.template,
        max_length=config.summary.max_length,
        values={
            "claim_no": claim.claim_no,
            "employee_no": claim.employee_no,
            "description": description,
            "period": period,
            "currency": claim.currency,
            "status": claim.status,
        },
    )


def _preparer(
    claim: ClaimInput, config: ExportConfig, exporter_employee_no: str | None
) -> str:
    """制单人取值（D12.5）：报销人 / 导出人 / 固定文案。"""
    if config.voucher.preparer == "exporter":
        return exporter_employee_no or ""
    if config.voucher.preparer == "literal":
        return config.voucher.preparer_literal or ""
    return claim.employee_no


def _attachment_count(claim: ClaimInput, config: ExportConfig) -> int:
    """附件数口径（D12.1）：挂账发票行数 / 原件引用非空计数。"""
    if config.invoice_policy.attachment_count_basis == "files":
        return sum(1 for invoice in claim.invoices if invoice.has_source_file)
    return len(claim.invoices)


def _aux_fields(line: LineInput | None, config: ExportConfig) -> dict[str, str | None]:
    """辅助核算槽位（D8）：默认全部留空；可选部门 / 部门+往来。"""
    fields: dict[str, str | None] = {
        "aux_class_a": None,
        "aux_code_a": None,
        "aux_name_a": None,
        "aux_class_b": None,
        "aux_code_b": None,
        "aux_name_b": None,
    }
    if config.auxiliary.mode == "none" or line is None:
        return fields
    if config.auxiliary.mode in {"department", "department_counterparty"}:
        # R（项目代码A）无权威来源（departments 无会计侧编码）→ 留空（§4.3 缺口）。
        if line.department_name:
            fields["aux_class_a"] = config.auxiliary.department_class
            fields["aux_name_a"] = line.department_name
    if config.auxiliary.mode == "department_counterparty":
        # 往来单位名称在本系统**没有权威来源**（§4.3 缺口）→ 由配置给定固定值，
        # 未配置时配置装载期已 fail closed（见 config._parse_auxiliary）。
        fields["aux_class_b"] = config.auxiliary.counterparty_class
        fields["aux_code_b"] = config.auxiliary.counterparty_code
        fields["aux_name_b"] = config.auxiliary.counterparty_name
    return fields


def gross_total(claims: list[ClaimInput]) -> Decimal:
    """Σ 明细价税合计（§4.4：``amount`` 即价税合计）。"""
    return sum_amounts(line.amount for claim in claims for line in claim.lines)


def net_total(claims: list[ClaimInput]) -> Decimal:
    """Σ 明细不含税金额（派生 ``amount - tax_amount``）。"""
    return sum_amounts(line.net_amount for claim in claims for line in claim.lines)


def tax_total(claims: list[ClaimInput]) -> Decimal:
    """Σ 明细税额。"""
    return sum_amounts(line.tax_amount for claim in claims for line in claim.lines)
