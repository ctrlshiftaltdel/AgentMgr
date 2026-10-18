"""manifest 构建（§7.1）：导出结果的**非敏感**结构化描述。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md`` §7

* §7.1 字段结构（期间、模板版本与 SHA-256、记录 ID 集合、行数、金额合计、文件
  哈希、导出人、时间共八项覆盖上游 §7.3）；
* §7.1「不含敏感值」——不写税号、不写发票号/代码明文（票面身份只用
  ``duplicate_key`` 单向摘要）、不写员工姓名、不写摘要文本；
* §7.1「金额一律字符串定点」——``Decimal`` 十进制字符串，禁止浮点；
* §7.3「manifest 本身不含导出文件的字节，只含其哈希与指针」；
* §6.5 幂等——除 ``export_id``/``exported_at`` 外逐字段一致。

**纯逻辑**：不导入数据库/ORM，不读写文件系统。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.expense.money import quantize, sum_amounts
from app.export.template import LoadedTemplate

__all__ = [
    "MANIFEST_VERSION",
    "build_manifest",
    "manifest_bytes",
    "strip_volatile",
]

#: manifest 结构版本。
MANIFEST_VERSION = 1

#: 本条 manifest 对应的**校验通过清单**（全绿才落 manifest，见 §7.1）。
CHECKS: tuple[str, ...] = (
    "debit_credit_balance",
    "gross_equals_net_plus_tax",
    "row_count",
    "amount_total",
    "period_containment",
    "claim_total_matches",
    "payment_matches_total",
    "account_codes_resolved",
    "invoice_status_allowed",
    "template_fingerprint",
)


def _money(value: object) -> str:
    """把金额格式化为 2 位小数定点字符串（禁止浮点）。"""
    return f"{quantize(value):.2f}"


def build_manifest(
    *,
    export_id: str,
    exported_at: str,
    company_id: int,
    period: str,
    date_basis: str,
    claims: Sequence[Any],
    entries: Sequence[Any],
    template: LoadedTemplate,
    file_name: str,
    file_sha256: str,
    file_size_bytes: int,
    file_storage_key: str,
    media_type: str,
    exported_by_employee_id: int,
    exported_by_employee_no: str,
) -> dict[str, Any]:
    """构建 manifest 字典（值均为非敏感字段；金额为定点字符串）。"""
    claim_ids = sorted(claim.claim_id for claim in claims)
    claim_nos = [claim.claim_no for claim in sorted(claims, key=lambda c: c.claim_id)]
    invoice_keys = sorted(
        invoice.duplicate_key for claim in claims for invoice in claim.invoices
    )

    gross_total = sum_amounts(line.amount for claim in claims for line in claim.lines)
    net_total = sum_amounts(line.net_amount for claim in claims for line in claim.lines)
    tax_total = sum_amounts(line.tax_amount for claim in claims for line in claim.lines)
    debit_total = sum_amounts(
        entry.debit for entry in entries if entry.debit is not None
    )
    credit_total = sum_amounts(
        entry.credit for entry in entries if entry.credit is not None
    )
    invoice_count = sum(len(claim.invoices) for claim in claims)
    expense_line_count = sum(len(claim.lines) for claim in claims)

    return {
        "manifest_version": MANIFEST_VERSION,
        "export_id": export_id,
        "scope": {
            "company_id": company_id,
            "period": period,
            "date_basis": date_basis,
            "claim_ids": claim_ids,
            "claim_nos": claim_nos,
            "invoice_duplicate_keys": invoice_keys,
        },
        "template": {
            "template_id": template.template_id,
            "template_version": template.template_version,
            "sha256": template.sha256,
            "worksheet": template.worksheet,
            "layout": template.layout,
        },
        "rows": {
            "voucher_count": len(claims),
            "entry_count": len(entries),
            "expense_line_count": expense_line_count,
            "invoice_count": invoice_count,
        },
        "amounts": {
            "gross_total": _money(gross_total),
            "net_total": _money(net_total),
            "tax_total": _money(tax_total),
            "debit_total": _money(debit_total),
            "credit_total": _money(credit_total),
        },
        "file": {
            "name": file_name,
            "sha256": file_sha256,
            "size_bytes": file_size_bytes,
            "media_type": media_type,
            "storage_key": file_storage_key,
        },
        "checks": list(CHECKS),
        "exported_by": {
            "employee_id": exported_by_employee_id,
            "employee_no": exported_by_employee_no,
        },
        "exported_at": exported_at,
    }


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """把 manifest 序列化为确定性 JSON 字节（排序键、UTF-8、缩进 2）。"""
    return (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def strip_volatile(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """去掉 ``export_id``/``exported_at`` 后的副本，用于幂等比对（§6.5）。"""
    copy = json.loads(json.dumps(manifest, ensure_ascii=False))
    copy.pop("export_id", None)
    copy.pop("exported_at", None)
    return copy
