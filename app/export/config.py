"""Track D 导出配置装载：**单一 TOML 文件** + ``tomllib``（标准库）。

设计依据：

* ``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md`` §9 决策 D1–D8 与 Issue #43 决策评论
  （「配置采用**文件方式**：单一文件、详尽注释；格式 TOML；零新增依赖」）；
* 上游 ``AgentMgr_AiNativeERP_Integration_Plan.md`` §7（导出契约与模板指纹）。

本模块的设计约束：

* **零新增依赖**：只用 ``tomllib``（Python 3.11+ 标准库），不引入 YAML/TOML 第三方库；
* **单一文件**：全部（非秘密）导出设置集中在**一个**文件里，逐项带注释（示例文件
  见 ``config/ajaimgr.example.toml``）；
* **fail closed**：缺必填项、取值非法、模板哈希格式不符、布局不支持 → 抛
  :class:`~app.export.errors.ExportConfigError`，**不产出任何导出文件**；
* **不影响应用启动**：本模块只在导出操作被调用时装载配置；配置文件缺失不会让
  ``app`` 导入或 HTTP 应用启动失败；
* **不臆造**：没有安全默认的项（如贷方科目、费用科目映射）**不给默认值**，缺失即
  报错——系统不会替部署方猜会计口径；
* **错误文本不含敏感值**：只报**配置键名**与原因码，不回显配置取值（唯一例外是
  配置文件路径本身，用于运维定位，且路径来自部署方自己的配置/环境变量）。

路径解析顺序（Issue 验收标准）：

1. 显式 ``path`` 参数；
2. 环境变量 ``AJAIMGR_CONFIG_FILE``；
3. 默认 ``config/ajaimgr.toml``（相对当前工作目录）。
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from string import Formatter
from typing import Any

from app.expense.money import to_decimal
from app.export.errors import (
    CODE_CONFIG_FILE_MISSING,
    CODE_CONFIG_INVALID,
    CODE_CONFIG_MISSING_KEYS,
    CODE_UNSUPPORTED_LAYOUT,
    ExportConfigError,
)

__all__ = [
    "ALLOWED_SUMMARY_FIELDS",
    "CLAIM_STATUSES",
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_PATH",
    "KNOWN_SCOPES",
    "SUPPORTED_LAYOUTS",
    "AccountsConfig",
    "AuxiliaryConfig",
    "CreditAccount",
    "CreditConfig",
    "CurrencyConfig",
    "ExportConfig",
    "InvoicePolicyConfig",
    "PermissionsConfig",
    "ScopeConfig",
    "SummaryConfig",
    "TaxConfig",
    "TemplateConfig",
    "VoucherConfig",
    "config_path_from_env",
    "load_export_config",
]

#: 环境变量名：覆盖运行时配置文件路径。
CONFIG_PATH_ENV = "AJAIMGR_CONFIG_FILE"

#: 默认运行时配置文件路径（相对当前工作目录）。
DEFAULT_CONFIG_PATH = "config/ajaimgr.toml"

#: 本实现支持的布局（扩展点：后续可加 ``pz`` 等，不得静默降级）。
SUPPORTED_LAYOUTS = frozenset({"entry_rows"})

#: ``expense_claims.status`` 的合法取值（与 ``app.expense.models`` 的 CHECK 一致）。
CLAIM_STATUSES = frozenset(
    {
        "draft",
        "submitted",
        "withdrawn",
        "approved",
        "finance_reviewed",
        "paid",
        "rejected",
    }
)

#: 数据范围档位（与 ``app.identity.permissions`` 的 ``scope`` CHECK 一致）。
KNOWN_SCOPES = frozenset({"own", "team", "company", "authorized"})

#: 摘要模板允许的占位符（其余一律视为配置错误，避免运行期 ``KeyError``）。
ALLOWED_SUMMARY_FIELDS = frozenset(
    {"claim_no", "employee_no", "description", "period", "currency", "status"}
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: 明显的凭据键名（配置文件**不得**承载凭据，命中即拒绝装载）。
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|credential|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)

_STATUS_VALUES = frozenset({"fail", "allow"})
_ATTACHMENT_BASES = frozenset({"invoices", "files"})
_PREPARER_MODES = frozenset({"claimant", "exporter", "literal"})
_TAX_SPLIT_MODES = frozenset({"none", "all", "categories"})
_DATE_BASES = frozenset({"expense_date", "paid_at"})
_AUX_MODES = frozenset({"none", "department", "department_counterparty"})


# --- 配置数据模型（全部 frozen） ----------------------------------------------


@dataclass(frozen=True)
class TemplateConfig:
    """模板与布局绑定（D1/D7）：运行时装载的受控产物 + SHA-256 指纹。"""

    path: str
    sha256: str
    worksheet: str
    layout: str
    template_id: str
    template_version: int


@dataclass(frozen=True)
class ScopeConfig:
    """导出对象范围与日期基准（D5/D9）。"""

    statuses: tuple[str, ...]
    date_basis: str


@dataclass(frozen=True)
class VoucherConfig:
    """凭证字与制单人（D5 / D12.5）。"""

    voucher_word: str
    preparer: str
    preparer_literal: str | None = None


@dataclass(frozen=True)
class SummaryConfig:
    """摘要文案模板（D6）。"""

    template: str
    max_length: int


@dataclass(frozen=True)
class AccountsConfig:
    """费用科目映射（D2）。"""

    expense_map: Mapping[str, str]
    expense_name_map: Mapping[str, str] = field(default_factory=dict)
    expense_default_code: str | None = None
    expense_default_name: str | None = None


@dataclass(frozen=True)
class TaxConfig:
    """进项税拆分规则（D3）。默认 ``split="none"``（安全默认：不拆）。"""

    split: str = "none"
    account_code: str | None = None
    account_name: str | None = None
    split_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreditAccount:
    """一个贷方科目（代码 + 名称）。"""

    code: str
    name: str | None = None


@dataclass(frozen=True)
class CreditConfig:
    """贷方科目口径（D4）。``by_status`` 非空时按报销单状态取值。"""

    account_code: str | None = None
    account_name: str | None = None
    by_status: Mapping[str, CreditAccount] = field(default_factory=dict)

    def account_for(self, status: str) -> CreditAccount | None:
        """返回该状态应使用的贷方科目；未配置返回 ``None``（由映射层 fail closed）。"""
        if self.by_status:
            return self.by_status.get(status)
        if self.account_code:
            return CreditAccount(code=self.account_code, name=self.account_name)
        return None


@dataclass(frozen=True)
class CurrencyConfig:
    """币别映射（D1 的 H/I 列口径）与汇率常量。"""

    rate: str
    codes: Mapping[str, str] = field(default_factory=dict)
    names: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AuxiliaryConfig:
    """辅助核算输出口径（D8）。"""

    mode: str = "none"
    department_class: str | None = None
    counterparty_class: str | None = None
    counterparty_code: str | None = None
    counterparty_name: str | None = None


@dataclass(frozen=True)
class InvoicePolicyConfig:
    """挂账发票的处置口径（D12.1 / D12.4）。"""

    attachment_count_basis: str = "invoices"
    on_verified_fail: str = "fail"
    on_suspected_duplicate: str = "fail"


@dataclass(frozen=True)
class PermissionsConfig:
    """导出所需权限（D10：复用现有 RBAC 三元组，不新增权限编号）。"""

    resource: str
    action: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class ExportConfig:
    """一份已校验的导出配置（不可变）。"""

    template: TemplateConfig
    scope: ScopeConfig
    voucher: VoucherConfig
    summary: SummaryConfig
    accounts: AccountsConfig
    credit: CreditConfig
    currency: CurrencyConfig
    permissions: PermissionsConfig
    tax: TaxConfig = field(default_factory=TaxConfig)
    auxiliary: AuxiliaryConfig = field(default_factory=AuxiliaryConfig)
    invoice_policy: InvoicePolicyConfig = field(default_factory=InvoicePolicyConfig)

    def currency_code(self, currency: str) -> str | None:
        """把本系统币别映射为模板币别代码（未登记返回 ``None``）。"""
        return self.currency.codes.get(currency)

    def currency_name(self, currency: str) -> str | None:
        """把本系统币别映射为模板币别名称（未登记返回 ``None``）。"""
        return self.currency.names.get(currency)


# --- 装载辅助 -----------------------------------------------------------------


def _fail(code: str, message: str) -> ExportConfigError:
    return ExportConfigError(code, message)


def _require_table(
    data: Mapping[str, Any], key: str, *, where: str
) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        raise _fail(
            CODE_CONFIG_MISSING_KEYS, f"missing required config section: {where}"
        )
    if not isinstance(value, Mapping):
        raise _fail(
            CODE_CONFIG_INVALID, f"config key '{where}' must be a table"
        )
    return value


def _require_str(table: Mapping[str, Any], key: str, *, where: str) -> str:
    value = table.get(key)
    if value is None:
        raise _fail(
            CODE_CONFIG_MISSING_KEYS, f"missing required config key: {where}"
        )
    if not isinstance(value, str) or not value.strip():
        raise _fail(
            CODE_CONFIG_INVALID, f"config key '{where}' must be a non-empty string"
        )
    return value.strip()


def _optional_str(table: Mapping[str, Any], key: str, *, where: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(CODE_CONFIG_INVALID, f"config key '{where}' must be a string")
    stripped = value.strip()
    return stripped or None


def _require_table_of_str(table: Mapping[str, Any], key: str, *, where: str) -> dict[str, str]:
    raw = table.get(key)
    if raw is None:
        raise _fail(CODE_CONFIG_MISSING_KEYS, f"missing required config key: {where}")
    if not isinstance(raw, Mapping):
        raise _fail(CODE_CONFIG_INVALID, f"config key '{where}' must be a table")
    result: dict[str, str] = {}
    for map_key, map_value in raw.items():
        if not isinstance(map_value, str) or not map_value.strip():
            raise _fail(
                CODE_CONFIG_INVALID,
                f"config key '{where}.{map_key}' must be a non-empty string",
            )
        result[str(map_key)] = map_value.strip()
    return result


def _optional_table_of_str(
    table: Mapping[str, Any], key: str, *, where: str
) -> dict[str, str]:
    """可选字符串表（缺省返回空表；出现但取值非法仍然报错）。"""
    if table.get(key) is None:
        return {}
    return _require_table_of_str(table, key, where=where)


def _require_int(
    table: Mapping[str, Any], key: str, *, where: str, minimum: int
) -> int:
    value = table.get(key)
    if value is None:
        raise _fail(CODE_CONFIG_MISSING_KEYS, f"missing required config key: {where}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(CODE_CONFIG_INVALID, f"config key '{where}' must be an integer")
    if value < minimum:
        raise _fail(
            CODE_CONFIG_INVALID, f"config key '{where}' must be >= {minimum}"
        )
    return value


def _enum(value: str, allowed: frozenset[str], *, where: str) -> str:
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise _fail(
            CODE_CONFIG_INVALID,
            f"config key '{where}' has an unsupported value; allowed: {options}",
        )
    return value


def _reject_secret_keys(node: Any, *, where: str) -> None:
    """拒绝任何看起来像凭据的配置键（配置文件不得承载凭据）。"""
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{where}.{key}" if where else str(key)
            if _SECRET_KEY_RE.search(str(key)):
                raise _fail(
                    CODE_CONFIG_INVALID,
                    f"config key '{child}' looks like a credential; "
                    "credentials must come from environment variables only",
                )
            _reject_secret_keys(value, where=child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_secret_keys(item, where=f"{where}[{index}]")


def _summary_fields(template: str, *, where: str) -> None:
    """校验摘要模板的占位符全部在白名单内（运行期不会 ``KeyError``）。"""
    try:
        parsed = list(Formatter().parse(template))
    except ValueError:
        raise _fail(CODE_CONFIG_INVALID, f"config key '{where}' is not a valid template")
    for _literal, field_name, _format_spec, _conversion in parsed:
        if field_name is None or field_name == "":
            continue
        if field_name not in ALLOWED_SUMMARY_FIELDS:
            options = ", ".join(sorted(ALLOWED_SUMMARY_FIELDS))
            raise _fail(
                CODE_CONFIG_INVALID,
                f"config key '{where}' uses unknown placeholder "
                f"'{{{field_name}}}'; allowed: {options}",
            )


# --- 各段落解析 ---------------------------------------------------------------


def _parse_template(data: Mapping[str, Any]) -> TemplateConfig:
    table = _require_table(data, "template", where="template")
    sha256 = _require_str(table, "sha256", where="template.sha256").lower()
    if not _SHA256_RE.match(sha256):
        raise _fail(
            CODE_CONFIG_INVALID,
            "config key 'template.sha256' must be a 64-character lowercase hex digest",
        )
    layout = _require_str(table, "layout", where="template.layout")
    if layout not in SUPPORTED_LAYOUTS:
        options = ", ".join(sorted(SUPPORTED_LAYOUTS))
        raise ExportConfigError(
            CODE_UNSUPPORTED_LAYOUT,
            f"config key 'template.layout' is not supported by this build; "
            f"allowed: {options}",
        )
    return TemplateConfig(
        path=_require_str(table, "path", where="template.path"),
        sha256=sha256,
        worksheet=_require_str(table, "worksheet", where="template.worksheet"),
        layout=layout,
        template_id=_require_str(table, "template_id", where="template.template_id"),
        template_version=_require_int(
            table, "template_version", where="template.template_version", minimum=1
        ),
    )


def _parse_scope(data: Mapping[str, Any]) -> ScopeConfig:
    table = _require_table(data, "scope", where="scope")
    raw_statuses = table.get("statuses")
    if raw_statuses is None:
        raise _fail(
            CODE_CONFIG_MISSING_KEYS, "missing required config key: scope.statuses"
        )
    if not isinstance(raw_statuses, list) or not raw_statuses:
        raise _fail(
            CODE_CONFIG_INVALID,
            "config key 'scope.statuses' must be a non-empty array of strings",
        )
    statuses: list[str] = []
    for item in raw_statuses:
        if not isinstance(item, str) or item not in CLAIM_STATUSES:
            raise _fail(
                CODE_CONFIG_INVALID,
                "config key 'scope.statuses' contains an unsupported claim status",
            )
        statuses.append(item)
    date_basis = _enum(
        _require_str(table, "date_basis", where="scope.date_basis"),
        _DATE_BASES,
        where="scope.date_basis",
    )
    return ScopeConfig(statuses=tuple(statuses), date_basis=date_basis)


def _parse_voucher(data: Mapping[str, Any]) -> VoucherConfig:
    table = _require_table(data, "voucher", where="voucher")
    preparer = _enum(
        _require_str(table, "preparer", where="voucher.preparer"),
        _PREPARER_MODES,
        where="voucher.preparer",
    )
    literal = _optional_str(table, "preparer_literal", where="voucher.preparer_literal")
    if preparer == "literal" and literal is None:
        raise _fail(
            CODE_CONFIG_INVALID,
            "config key 'voucher.preparer_literal' is required when "
            "'voucher.preparer' = 'literal'",
        )
    return VoucherConfig(
        voucher_word=_require_str(table, "voucher_word", where="voucher.voucher_word"),
        preparer=preparer,
        preparer_literal=literal,
    )


def _parse_summary(data: Mapping[str, Any]) -> SummaryConfig:
    table = _require_table(data, "summary", where="summary")
    template = _require_str(table, "template", where="summary.template")
    _summary_fields(template, where="summary.template")
    max_length = _require_int(
        table, "max_length", where="summary.max_length", minimum=1
    )
    return SummaryConfig(template=template, max_length=max_length)


def _parse_accounts(data: Mapping[str, Any]) -> AccountsConfig:
    table = _require_table(data, "accounts", where="accounts")
    return AccountsConfig(
        expense_map=_require_table_of_str(
            table, "expense_map", where="accounts.expense_map"
        ),
        expense_name_map=_optional_table_of_str(
            table, "expense_name_map", where="accounts.expense_name_map"
        ),
        expense_default_code=_optional_str(
            table, "expense_default_code", where="accounts.expense_default_code"
        ),
        expense_default_name=_optional_str(
            table, "expense_default_name", where="accounts.expense_default_name"
        ),
    )


def _parse_tax(data: Mapping[str, Any]) -> TaxConfig:
    table = data.get("tax")
    if table is None:
        return TaxConfig()
    if not isinstance(table, Mapping):
        raise _fail(CODE_CONFIG_INVALID, "config key 'tax' must be a table")
    split = _enum(
        _optional_str(table, "split", where="tax.split") or "none",
        _TAX_SPLIT_MODES,
        where="tax.split",
    )
    account_code = _optional_str(table, "account_code", where="tax.account_code")
    account_name = _optional_str(table, "account_name", where="tax.account_name")
    raw_categories = table.get("split_categories")
    if raw_categories is None:
        categories: tuple[str, ...] = ()
    elif isinstance(raw_categories, list) and all(
        isinstance(item, str) for item in raw_categories
    ):
        categories = tuple(raw_categories)
    else:
        raise _fail(
            CODE_CONFIG_INVALID,
            "config key 'tax.split_categories' must be an array of strings",
        )
    if split != "none" and account_code is None:
        raise _fail(
            CODE_CONFIG_MISSING_KEYS,
            "config key 'tax.account_code' is required when 'tax.split' is not 'none'",
        )
    if split == "categories" and not categories:
        raise _fail(
            CODE_CONFIG_INVALID,
            "config key 'tax.split_categories' must not be empty when "
            "'tax.split' = 'categories'",
        )
    return TaxConfig(
        split=split,
        account_code=account_code,
        account_name=account_name,
        split_categories=categories,
    )


def _parse_credit(data: Mapping[str, Any]) -> CreditConfig:
    table = _require_table(data, "credit", where="credit")
    account_code = _optional_str(table, "account_code", where="credit.account_code")
    account_name = _optional_str(table, "account_name", where="credit.account_name")
    by_status: dict[str, CreditAccount] = {}
    raw = table.get("by_status")
    if raw is not None:
        if not isinstance(raw, Mapping):
            raise _fail(CODE_CONFIG_INVALID, "config key 'credit.by_status' must be a table")
        for status, entry in raw.items():
            if status not in CLAIM_STATUSES:
                raise _fail(
                    CODE_CONFIG_INVALID,
                    "config key 'credit.by_status' contains an unsupported claim status",
                )
            if not isinstance(entry, Mapping):
                raise _fail(
                    CODE_CONFIG_INVALID,
                    f"config key 'credit.by_status.{status}' must be a table",
                )
            by_status[str(status)] = CreditAccount(
                code=_require_str(
                    entry, "code", where=f"credit.by_status.{status}.code"
                ),
                name=_optional_str(
                    entry, "name", where=f"credit.by_status.{status}.name"
                ),
            )
    if not by_status and account_code is None:
        raise _fail(
            CODE_CONFIG_MISSING_KEYS,
            "config key 'credit.account_code' (or 'credit.by_status') is required",
        )
    return CreditConfig(
        account_code=account_code, account_name=account_name, by_status=by_status
    )


def _parse_currency(data: Mapping[str, Any]) -> CurrencyConfig:
    table = _require_table(data, "currency", where="currency")
    raw_rate = table.get("rate")
    if raw_rate is None:
        raise _fail(CODE_CONFIG_MISSING_KEYS, "missing required config key: currency.rate")
    try:
        rate = to_decimal(raw_rate)  # 拒绝 float
    except (TypeError, InvalidOperation):
        raise _fail(
            CODE_CONFIG_INVALID,
            "config key 'currency.rate' must be a decimal string or integer",
        )
    if not rate.is_finite() or rate <= Decimal("0"):
        raise _fail(CODE_CONFIG_INVALID, "config key 'currency.rate' must be positive")
    return CurrencyConfig(
        rate=str(rate),
        codes=_require_table_of_str(table, "codes", where="currency.codes"),
        names=_optional_table_of_str(table, "names", where="currency.names"),
    )


def _parse_auxiliary(data: Mapping[str, Any]) -> AuxiliaryConfig:
    table = data.get("auxiliary")
    if table is None:
        return AuxiliaryConfig()
    if not isinstance(table, Mapping):
        raise _fail(CODE_CONFIG_INVALID, "config key 'auxiliary' must be a table")
    mode = _enum(
        _optional_str(table, "mode", where="auxiliary.mode") or "none",
        _AUX_MODES,
        where="auxiliary.mode",
    )
    department_class = _optional_str(
        table, "department_class", where="auxiliary.department_class"
    )
    counterparty_class = _optional_str(
        table, "counterparty_class", where="auxiliary.counterparty_class"
    )
    counterparty_code = _optional_str(
        table, "counterparty_code", where="auxiliary.counterparty_code"
    )
    counterparty_name = _optional_str(
        table, "counterparty_name", where="auxiliary.counterparty_name"
    )
    if mode in {"department", "department_counterparty"} and department_class is None:
        raise _fail(
            CODE_CONFIG_MISSING_KEYS,
            "config key 'auxiliary.department_class' is required when "
            "'auxiliary.mode' outputs departments",
        )
    if mode == "department_counterparty":
        if counterparty_class is None or counterparty_name is None:
            raise _fail(
                CODE_CONFIG_MISSING_KEYS,
                "config keys 'auxiliary.counterparty_class' and "
                "'auxiliary.counterparty_name' are required when "
                "'auxiliary.mode' = 'department_counterparty'",
            )
    return AuxiliaryConfig(
        mode=mode,
        department_class=department_class,
        counterparty_class=counterparty_class,
        counterparty_code=counterparty_code,
        counterparty_name=counterparty_name,
    )


def _parse_invoice_policy(data: Mapping[str, Any]) -> InvoicePolicyConfig:
    table = data.get("invoice_policy")
    if table is None:
        return InvoicePolicyConfig()
    if not isinstance(table, Mapping):
        raise _fail(CODE_CONFIG_INVALID, "config key 'invoice_policy' must be a table")
    return InvoicePolicyConfig(
        attachment_count_basis=_enum(
            _optional_str(table, "attachment_count_basis", where="invoice_policy.attachment_count_basis")
            or "invoices",
            _ATTACHMENT_BASES,
            where="invoice_policy.attachment_count_basis",
        ),
        on_verified_fail=_enum(
            _optional_str(table, "on_verified_fail", where="invoice_policy.on_verified_fail")
            or "fail",
            _STATUS_VALUES,
            where="invoice_policy.on_verified_fail",
        ),
        on_suspected_duplicate=_enum(
            _optional_str(
                table, "on_suspected_duplicate", where="invoice_policy.on_suspected_duplicate"
            )
            or "fail",
            _STATUS_VALUES,
            where="invoice_policy.on_suspected_duplicate",
        ),
    )


def _parse_permissions(data: Mapping[str, Any]) -> PermissionsConfig:
    table = _require_table(data, "permissions", where="permissions")
    raw_scopes = table.get("scopes")
    if raw_scopes is None:
        raise _fail(
            CODE_CONFIG_MISSING_KEYS, "missing required config key: permissions.scopes"
        )
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise _fail(
            CODE_CONFIG_INVALID,
            "config key 'permissions.scopes' must be a non-empty array of strings",
        )
    scopes: list[str] = []
    for item in raw_scopes:
        if not isinstance(item, str) or item not in KNOWN_SCOPES:
            raise _fail(
                CODE_CONFIG_INVALID,
                "config key 'permissions.scopes' contains an unsupported scope; "
                f"allowed: {', '.join(sorted(KNOWN_SCOPES))}",
            )
        scopes.append(item)
    return PermissionsConfig(
        resource=_require_str(table, "resource", where="permissions.resource"),
        action=_require_str(table, "action", where="permissions.action"),
        scopes=tuple(scopes),
    )


# --- 对外入口 -----------------------------------------------------------------


def config_path_from_env(environ: Mapping[str, str] | None = None) -> str:
    """返回运行时配置路径：``AJAIMGR_CONFIG_FILE`` 或默认值。"""
    source = os.environ if environ is None else environ
    raw = source.get(CONFIG_PATH_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_CONFIG_PATH
    return raw.strip()


def load_export_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ExportConfig:
    """装载并校验导出配置（**fail closed**）。

    任何缺失/非法/不支持都抛 :class:`ExportConfigError`，绝不返回「部分配置」或
    猜测默认口径。文件缺失只影响导出操作，**不影响应用启动**（本函数不在导入期
    调用）。
    """
    resolved = Path(path) if path is not None else Path(config_path_from_env(environ))
    try:
        raw_bytes = resolved.read_bytes()
    except FileNotFoundError:
        raise ExportConfigError(
            CODE_CONFIG_FILE_MISSING,
            f"export configuration file not found: {resolved} "
            f"(set {CONFIG_PATH_ENV} to override the default "
            f"'{DEFAULT_CONFIG_PATH}')",
        )
    except OSError:
        raise ExportConfigError(
            CODE_CONFIG_FILE_MISSING,
            f"export configuration file is not readable: {resolved}",
        )

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise ExportConfigError(
            CODE_CONFIG_INVALID,
            f"export configuration file is not valid UTF-8 TOML: {resolved}",
        )

    if not isinstance(data, Mapping):
        raise ExportConfigError(
            CODE_CONFIG_INVALID, "export configuration root must be a table"
        )
    _reject_secret_keys(data, where="")

    return ExportConfig(
        template=_parse_template(data),
        scope=_parse_scope(data),
        voucher=_parse_voucher(data),
        summary=_parse_summary(data),
        accounts=_parse_accounts(data),
        credit=_parse_credit(data),
        currency=_parse_currency(data),
        permissions=_parse_permissions(data),
        tax=_parse_tax(data),
        auxiliary=_parse_auxiliary(data),
        invoice_policy=_parse_invoice_policy(data),
    )
