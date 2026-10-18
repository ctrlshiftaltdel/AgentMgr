"""Track D 导出机制的错误类型（机器可读 + fail closed）。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md``

* §6.3「fail closed 条件清单（任一命中即**不产出文件**）」——12 条；
* §6.3「失败行为：返回明确错误码 + 命中规则清单，**不写任何文件、不写 manifest**，
  并写一条拒绝审计」；
* §6.4 单元格注入防护。

**不变量**：所有导出错误都携带一个**机器可读的 ``code``**（ASCII snake_case），
调用方据此做错误映射，不需要解析人类可读文本。``message`` 只用于运维/开发定位，
**不得包含任何敏感值**（税号、发票号明文、凭据、本机绝对路径等）；配置值一律
不写进错误文本，只写**配置键名**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CODE_ACCOUNT_CODE_UNRESOLVED",
    "CODE_AMOUNT_OUT_OF_RANGE",
    "CODE_CELL_INJECTION_UNESCAPED",
    "CODE_CLAIM_SET_MISMATCH",
    "CODE_CLAIM_TOTAL_MISMATCH",
    "CODE_CONFIG_FILE_MISSING",
    "CODE_CONFIG_INVALID",
    "CODE_CONFIG_MISSING_KEYS",
    "CODE_CURRENCY_UNMAPPED",
    "CODE_DEBIT_CREDIT_IMBALANCE",
    "CODE_INVOICE_AMOUNT_INCONSISTENT",
    "CODE_INVOICE_STATUS_REJECTED",
    "CODE_MISSING_PERMISSION",
    "CODE_NO_RECORDS_IN_PERIOD",
    "CODE_PAYMENT_TOTAL_MISMATCH",
    "CODE_PERIOD_CONTAINMENT_FAILED",
    "CODE_PERIOD_INVALID",
    "CODE_ROW_COUNT_MISMATCH",
    "CODE_STORAGE_ERROR",
    "CODE_TEMPLATE_FINGERPRINT_MISMATCH",
    "CODE_TEMPLATE_HEADER_MISMATCH",
    "CODE_TEMPLATE_MISSING",
    "CODE_UNSUPPORTED_LAYOUT",
    "CODE_WORKSHEET_MISSING",
    "ExportAuthorizationError",
    "ExportConfigError",
    "ExportError",
    "ExportTemplateError",
    "ExportValidationError",
    "ValidationFailure",
]

# --- 机器可读错误码（配置） ---------------------------------------------------

#: 配置文件不存在（导出操作明确报错；**不影响应用启动**）。
CODE_CONFIG_FILE_MISSING = "config_file_missing"
#: 配置文件不是合法 TOML / 取值非法 / 枚举越界。
CODE_CONFIG_INVALID = "config_invalid"
#: 缺少必填配置项（错误只列键名，不回显取值）。
CODE_CONFIG_MISSING_KEYS = "config_missing_keys"

# --- 机器可读错误码（模板） ---------------------------------------------------

#: 模板文件不存在或不可读。
CODE_TEMPLATE_MISSING = "template_missing"
#: 模板 SHA-256 与配置登记的指纹不符。
CODE_TEMPLATE_FINGERPRINT_MISMATCH = "template_fingerprint_mismatch"
#: 目标工作表不存在。
CODE_WORKSHEET_MISSING = "worksheet_missing"
#: 关键表头/列数与登记的布局结构不符。
CODE_TEMPLATE_HEADER_MISMATCH = "template_header_mismatch"
#: 配置的 ``layout`` 不是本实现支持的布局（扩展点；不静默降级）。
CODE_UNSUPPORTED_LAYOUT = "unsupported_layout"

# --- 机器可读错误码（映射 / 校验） -------------------------------------------

#: E1 / E2 借贷不平衡。
CODE_DEBIT_CREDIT_IMBALANCE = "debit_credit_imbalance"
#: E3 价税合计 ≠ 不含税 + 税额，或 ``tax_amount > amount``。
CODE_GROSS_NOT_NET_PLUS_TAX = "gross_not_net_plus_tax"
#: E4 报销单合计与明细合计不符。
CODE_CLAIM_TOTAL_MISMATCH = "claim_total_mismatch"
#: E5 付款金额与报销单合计不符。
CODE_PAYMENT_TOTAL_MISMATCH = "payment_total_mismatch"
#: E6 票面三口径不自洽。
CODE_INVOICE_AMOUNT_INCONSISTENT = "invoice_amount_inconsistent"
#: C1 行数对账失败。
CODE_ROW_COUNT_MISMATCH = "row_count_mismatch"
#: C2 笔数对账失败。
CODE_CLAIM_COUNT_MISMATCH = "claim_count_mismatch"
#: C3 金额合计对账失败。
CODE_AMOUNT_TOTAL_MISMATCH = "amount_total_mismatch"
#: C4 期间包含性失败（含跨期数据）。
CODE_PERIOD_CONTAINMENT_FAILED = "period_containment_failed"
#: C4 变体：请求期间内无任何合格记录。
CODE_NO_RECORDS_IN_PERIOD = "no_records_in_period"
#: 请求的期间字符串不是 ``YYYY-MM``。
CODE_PERIOD_INVALID = "period_invalid"
#: C5 集合一致性：manifest 的 claim_ids 与文件内业务编号集合不一致。
CODE_CLAIM_SET_MISMATCH = "claim_set_mismatch"
#: fail closed #7：任一科目代码无法解析。
CODE_ACCOUNT_CODE_UNRESOLVED = "account_code_unresolved"
#: fail closed #8：挂账发票状态不允许导出（``verified_fail`` / 可疑重复）。
CODE_INVOICE_STATUS_REJECTED = "invoice_status_rejected"
#: fail closed #9：越界金额（``<= 0``、超 ``MAX_AMOUNT``、非 2 位小数、不含税为负）。
CODE_AMOUNT_OUT_OF_RANGE = "amount_out_of_range"
#: fail closed #12：文本单元格含未转义的危险前缀。
CODE_CELL_INJECTION_UNESCAPED = "cell_injection_unescaped"
#: 币别未在配置的映射表中登记（**实现口径澄清**：不臆造币别代码，直接 fail closed）。
CODE_CURRENCY_UNMAPPED = "currency_unmapped"
#: fail closed #11：导出人无导出权限。
CODE_MISSING_PERMISSION = "missing_permission"
#: 导出产物无法落存储（大小 / 类型 / 哈希闸门拒绝；fail closed）。
CODE_STORAGE_ERROR = "storage_error"


@dataclass(frozen=True)
class ValidationFailure:
    """一条命中的校验失败（机器可读）。

    ``rule`` 是文档编号（``E1``/``E7``/``C4``/``fail_closed_9`` 等），``code`` 是
    与 :mod:`app.export.errors` 中常量对应的稳定机器码，``details`` 只放**非敏感**
    的结构化字段（ID、金额、期间等；**不放**税号、发票号明文与任何凭据）。
    """

    rule: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class ExportError(Exception):
    """导出机制错误基类：携带机器可读 ``code``。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


class ExportConfigError(ExportError):
    """配置装载失败（缺必填项 / 取值非法 / 文件缺失）。"""


class ExportTemplateError(ExportError):
    """模板装载或指纹/结构校验失败。"""


class ExportAuthorizationError(ExportError):
    """导出人缺少导出权限。"""


class ExportValidationError(ExportError):
    """校验失败：携带**全部**命中的规则清单（fail closed）。

    ``code`` 取第一条命中的机器码（便于调用方做粗粒度分支），完整清单在
    ``failures`` 中。
    """

    def __init__(self, failures: list[ValidationFailure]) -> None:
        if not failures:
            raise ValueError("ExportValidationError requires at least one failure")
        self.failures: tuple[ValidationFailure, ...] = tuple(failures)
        primary = self.failures[0]
        super().__init__(
            primary.code,
            "export validation failed: "
            + ", ".join(f"{item.rule}:{item.code}" for item in self.failures),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "failures": [item.as_dict() for item in self.failures],
        }
