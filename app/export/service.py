"""Track D 导出服务层入口：``export_claims_period``（只读投影 + 审计 + 幂等）。

设计依据：``docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md``

* §7.2「与 ``app/documents/storage.py`` 的复用点」——导出文件与 manifest 落在同一
  内容寻址存储根，各落一行 ``source_files`` 元数据；大小/类型/哈希校验全部复用；
* §7.2「审计」——成功写 ``finance.export.period``（``details`` 只放非敏感聚合值），
  拒绝写对应 ``*.deny``（沿用 ``app.expense.service`` 的 ``_record_deny`` 模式）；
* §6.3 fail closed——任一校验/配置/模板失败：**不写文件、不写 manifest**，写一条
  拒绝审计后抛错；
* §6.5 幂等——同期间 + 同模板重复导出：manifest 除 ``export_id``/``exported_at``
  外逐字段一致；导出文件不含时间戳 → 字节可复现。

**只读投影**：本函数只读取报销域数据，绝不修改 ``expense_claims`` / ``expense_lines``
/ ``expense_invoices`` / ``reimbursement_payments``（调用方可在导出前后断言不变）。
它只**追加**两类记录：``source_files``（导出文件与 manifest 的元数据行）与
``audit_events``（成功/拒绝审计）。两者都不 commit——事务边界由调用方控制。
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.db.models.organization import Department, Employee
from app.documents.models import SourceFile
from app.documents.storage import (
    DEFAULT_MAX_BYTES,
    StorageError,
    default_storage_root,
    store_file,
)
from app.expense.models import (
    ExpenseCategory,
    ExpenseClaim,
    ExpenseInvoice,
    ExpenseLine,
    ReimbursementPayment,
)
from app.export.config import ExportConfig, load_export_config
from app.export.errors import (
    CODE_MISSING_PERMISSION,
    CODE_PERIOD_INVALID,
    CODE_STORAGE_ERROR,
    ExportAuthorizationError,
    ExportError,
    ExportValidationError,
)
from app.export.manifest import build_manifest, manifest_bytes
from app.export.mapping import (
    ClaimInput,
    InvoiceInput,
    LineInput,
    build_entries,
)
from app.export.template import load_template
from app.export.validation import parse_period, validate_claims, validate_export
from app.export.writer import XLSX_MEDIA_TYPE, render_export
from app.identity.authorization import Principal, has_permission
from app.integrations.models import InvoiceVerification

__all__ = [
    "ACTION_DENY",
    "ACTION_EXPORT",
    "RESOURCE_TYPE",
    "ExportResult",
    "export_claims_period",
]

#: 成功审计动作名（上游 Track B 已规划）。
ACTION_EXPORT = "finance.export.period"

#: 拒绝审计动作名（fail closed：配置 / 模板 / 校验 / 权限）。
ACTION_DENY = "finance.export.period.deny"

#: 审计 ``resource_type``。
RESOURCE_TYPE = "expense_export"


@dataclass(frozen=True)
class ExportResult:
    """一次成功导出的结果（manifest + 存储指针；**不含**文件字节）。"""

    export_id: str
    manifest: dict[str, Any]
    file_name: str
    file_sha256: str
    file_size_bytes: int
    file_storage_key: str
    file_media_type: str
    manifest_sha256: str
    manifest_storage_key: str
    entry_count: int
    voucher_count: int


# --- 审计 ---------------------------------------------------------------------


def _record_deny(
    session: Session,
    principal: Principal,
    *,
    reason: str,
    period: str | None,
    request_id: str | None,
) -> None:
    """写一条拒绝审计事件（不 commit）；``details`` 只含原因码与期间。"""
    record_event(
        session,
        actor_id=principal.employee_id,
        action=ACTION_DENY,
        resource_type=RESOURCE_TYPE,
        resource_id=None,
        request_id=request_id,
        details={"attempted_action": "export", "reason": reason, "period": period},
    )


def _record_success(
    session: Session,
    principal: Principal,
    *,
    result: ExportResult,
    period: str,
    company_id: int,
    template_version: int,
    request_id: str | None,
) -> None:
    """写一条成功审计事件；``details`` 只放非敏感聚合值（§7.3）。"""
    record_event(
        session,
        actor_id=principal.employee_id,
        action=ACTION_EXPORT,
        resource_type=RESOURCE_TYPE,
        resource_id=result.export_id,
        request_id=request_id,
        details={
            "period": period,
            "company_id": company_id,
            "template_version": template_version,
            "voucher_count": result.voucher_count,
            "entry_count": result.entry_count,
            "file_sha256": result.file_sha256,
        },
    )


# --- 期间与查询 ---------------------------------------------------------------


def _period_bounds(period: str) -> tuple[date, date]:
    """返回期间的 ``[start, end]``（含首尾日）。"""
    year, month = parse_period(period)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _period_datetime_bounds(period: str) -> tuple[datetime, datetime]:
    """返回期间的 ``[start, next_start)``（用于 ``timestamptz`` 比较）。"""
    year, month = parse_period(period)
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        next_start = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        next_start = datetime(year, month + 1, 1, tzinfo=UTC)
    return start, next_start


def _select_claim_ids(
    session: Session, principal: Principal, config: ExportConfig, period: str
) -> list[int]:
    """按公司 / 状态 / 期间选出报销单 ID（升序，保证确定性）。"""
    base_filters = (
        Employee.company_id == principal.company_id,
        ExpenseClaim.status.in_(tuple(config.scope.statuses)),
    )
    if config.scope.date_basis == "paid_at":
        start, next_start = _period_datetime_bounds(period)
        statement = (
            select(ExpenseClaim.id)
            .join(Employee, Employee.id == ExpenseClaim.employee_id)
            .join(
                ReimbursementPayment,
                ReimbursementPayment.claim_id == ExpenseClaim.id,
            )
            .where(
                *base_filters,
                ReimbursementPayment.status == "paid",
                ReimbursementPayment.paid_at.is_not(None),
                ReimbursementPayment.paid_at >= start,
                ReimbursementPayment.paid_at < next_start,
            )
        )
    else:
        start, end = _period_bounds(period)
        statement = (
            select(ExpenseClaim.id)
            .join(Employee, Employee.id == ExpenseClaim.employee_id)
            .join(ExpenseLine, ExpenseLine.claim_id == ExpenseClaim.id)
            .where(
                *base_filters,
                ExpenseLine.expense_date >= start,
                ExpenseLine.expense_date <= end,
            )
        )
    return sorted({int(claim_id) for claim_id in session.scalars(statement)})


def _load_claims(
    session: Session, claim_ids: list[int]
) -> list[ClaimInput]:
    """把 ORM 行投影为映射引擎的纯输入（只读；不含税号等敏感字段）。"""
    if not claim_ids:
        return []
    claims = {
        claim.id: claim
        for claim in session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id.in_(claim_ids))
        )
    }
    lines = list(
        session.scalars(
            select(ExpenseLine)
            .where(ExpenseLine.claim_id.in_(claim_ids))
            .order_by(ExpenseLine.id)
        )
    )
    category_ids = {line.category_id for line in lines}
    categories = {
        category.id: category
        for category in session.scalars(
            select(ExpenseCategory).where(ExpenseCategory.id.in_(category_ids))
        )
    }
    employee_ids = {claim.employee_id for claim in claims.values()}
    employees = {
        employee.id: employee
        for employee in session.scalars(
            select(Employee).where(Employee.id.in_(employee_ids))
        )
    }
    department_ids = {
        employee.department_id
        for employee in employees.values()
        if employee.department_id is not None
    }
    departments = {
        department.id: department
        for department in session.scalars(
            select(Department).where(Department.id.in_(department_ids))
        )
    }
    payments: dict[int, ReimbursementPayment] = {}
    for payment in session.scalars(
        select(ReimbursementPayment)
        .where(
            ReimbursementPayment.claim_id.in_(claim_ids),
            ReimbursementPayment.status == "paid",
        )
        .order_by(ReimbursementPayment.id)
    ):
        payments[payment.claim_id] = payment  # 取 id 最大的一条（确定性）
    invoices = list(
        session.scalars(
            select(ExpenseInvoice)
            .where(ExpenseInvoice.claim_id.in_(claim_ids))
            .order_by(ExpenseInvoice.id)
        )
    )
    verifications = _load_verifications(session, invoices)

    result: list[ClaimInput] = []
    for claim_id in claim_ids:
        claim = claims.get(claim_id)
        if claim is None:  # 理论上不会发生（ID 来自同一次查询）
            continue
        employee = employees.get(claim.employee_id)
        employee_no = employee.employee_no if employee is not None else ""
        department_name = None
        if employee is not None and employee.department_id is not None:
            department = departments.get(employee.department_id)
            department_name = department.name if department is not None else None
        line_inputs = tuple(
            LineInput(
                line_id=line.id,
                expense_date=line.expense_date,
                category_code=categories[line.category_id].code,
                category_name=categories[line.category_id].name,
                description=line.description,
                amount=line.amount,
                tax_amount=line.tax_amount,
                department_name=department_name,
            )
            for line in lines
            if line.claim_id == claim_id
        )
        payment = payments.get(claim_id)
        result.append(
            ClaimInput(
                claim_id=claim.id,
                claim_no=claim.claim_no,
                currency=claim.currency,
                status=claim.status,
                total_amount=claim.total_amount,
                employee_no=employee_no,
                lines=line_inputs,
                invoices=_invoice_inputs(claim_id, invoices, verifications),
                payment_amount=payment.amount if payment is not None else None,
                payment_date=payment.paid_at.date()
                if payment is not None and payment.paid_at is not None
                else None,
            )
        )
    return result


def _load_verifications(
    session: Session, invoices: Iterable[ExpenseInvoice]
) -> dict[str, InvoiceVerification]:
    """按 ``duplicate_key`` 装载查验记录（只读金额三口径，不读税号）。"""
    keys = {invoice.duplicate_key for invoice in invoices}
    if not keys:
        return {}
    return {
        verification.duplicate_key: verification
        for verification in session.scalars(
            select(InvoiceVerification).where(
                InvoiceVerification.duplicate_key.in_(keys)
            )
        )
    }


def _snapshot_amount(invoice: ExpenseInvoice) -> Decimal | None:
    """从票面快照取价税合计（字符串定点）；缺失或不可解析返回 ``None``。"""
    raw = (invoice.invoice_fields_json or {}).get("amount")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _invoice_inputs(
    claim_id: int,
    invoices: Iterable[ExpenseInvoice],
    verifications: Mapping[str, InvoiceVerification],
) -> tuple[InvoiceInput, ...]:
    result: list[InvoiceInput] = []
    for invoice in invoices:
        if invoice.claim_id != claim_id:
            continue
        verification = verifications.get(invoice.duplicate_key)
        result.append(
            InvoiceInput(
                duplicate_key=invoice.duplicate_key,
                amount=_snapshot_amount(invoice),
                verification_status=invoice.verification_status,
                suspected_duplicate=bool(invoice.suspected_duplicate),
                has_source_file=invoice.source_file_id is not None,
                amount_without_tax=verification.amount_without_tax
                if verification is not None
                else None,
                verified_tax_amount=verification.tax_amount
                if verification is not None
                else None,
                amount_with_tax=verification.amount_with_tax
                if verification is not None
                else None,
            )
        )
    return tuple(result)


# --- 存储 ---------------------------------------------------------------------


def _store_blob(
    session: Session,
    principal: Principal,
    *,
    storage_root: Path | str,
    content: bytes,
    declared_name: str,
) -> Any:
    """落一个内容寻址 blob 并写一行 ``source_files`` 元数据（复用 storage 的四道闸门）。"""
    stored = store_file(
        storage_root,
        owner_id=principal.employee_id,
        content=content,
        declared_name=declared_name,
        max_bytes=DEFAULT_MAX_BYTES,
    )
    session.add(
        SourceFile(
            owner_id=principal.employee_id,
            storage_key=stored.storage_key,
            sha256=stored.sha256,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            original_name=declared_name,
        )
    )
    session.flush()
    return stored


# --- 服务层入口 ---------------------------------------------------------------


def export_claims_period(
    session: Session,
    principal: Principal,
    *,
    period: str,
    config: ExportConfig | None = None,
    request_id: str | None = None,
    storage_root: Path | str | None = None,
    now: datetime | None = None,
) -> ExportResult:
    """把某期间内已选状态的报销单投影为**目标布局**（``template.layout``）的 xlsx + manifest。

    流程（任一步失败即 fail closed：不写文件、不写 manifest、写拒绝审计）：

    1. 装载配置（``AJAIMGR_CONFIG_FILE`` / 默认 ``config/ajaimgr.toml``）；
    2. 授权：命中 ``permissions`` 段任一 ``(resource, action, scope)`` 三元组；
    3. 校验请求期间格式（``YYYY-MM``）；
    4. 装载模板（SHA-256 指纹 + 工作表 + 表头结构）；
    5. 只读选出期间内、公司内、状态合格的报销单；
    6. §6 全量校验（E1–E7 / C1–C5 / fail closed）；
    7. 生成分录行 → 渲染 xlsx（按布局）→ 构建 manifest → 落存储（含哈希校验）→ 审计。

    ``storage_root`` 默认取 ``app.documents.storage.default_storage_root()``；
    ``now`` 仅用于 manifest 的 ``exported_at``（测试可注入固定值）。
    """
    resolved_period: str | None = period
    try:
        resolved_config = config if config is not None else load_export_config()
        _authorize(session, principal, resolved_config)
        try:
            parse_period(period)
        except ValueError as exc:
            raise ExportError(CODE_PERIOD_INVALID, str(exc)) from exc
        loaded_template = load_template(resolved_config)

        claim_ids = _select_claim_ids(session, principal, resolved_config, period)
        claims = _load_claims(session, claim_ids)

        # 先跑凭证级校验（E3–E7 / C4 / fail closed #6/#8/#9）：非法数据不进入映射。
        claim_failures = validate_claims(claims, resolved_config, period=period)
        if claim_failures:
            raise ExportValidationError(claim_failures)

        entries = build_entries(
            claims,
            resolved_config,
            period=period,
            exporter_employee_no=_employee_no(session, principal.employee_id),
        )
        failures = validate_export(claims, entries, resolved_config, period=period)
        if failures:
            raise ExportValidationError(list(failures))

        export_id = str(uuid4())
        exported_at = (now or datetime.now(UTC)).isoformat()
        file_name = f"expense-export-{period}.xlsx"
        manifest_name = f"expense-export-{period}.manifest.json"
        root = storage_root if storage_root is not None else default_storage_root()

        content = render_export(entries, resolved_config)
        stored_file = _store_blob(
            session,
            principal,
            storage_root=root,
            content=content,
            declared_name=file_name,
        )
        manifest = build_manifest(
            export_id=export_id,
            exported_at=exported_at,
            company_id=principal.company_id,
            period=period,
            date_basis=resolved_config.scope.date_basis,
            claims=claims,
            entries=entries,
            template=loaded_template,
            file_name=file_name,
            file_sha256=stored_file.sha256,
            file_size_bytes=stored_file.size_bytes,
            file_storage_key=stored_file.storage_key,
            media_type=XLSX_MEDIA_TYPE,
            exported_by_employee_id=principal.employee_id,
            exported_by_employee_no=_employee_no(session, principal.employee_id),
        )
        stored_manifest = _store_blob(
            session,
            principal,
            storage_root=root,
            content=manifest_bytes(manifest),
            declared_name=manifest_name,
        )
        result = ExportResult(
            export_id=export_id,
            manifest=manifest,
            file_name=file_name,
            file_sha256=stored_file.sha256,
            file_size_bytes=stored_file.size_bytes,
            file_storage_key=stored_file.storage_key,
            file_media_type=stored_file.mime_type,
            manifest_sha256=stored_manifest.sha256,
            manifest_storage_key=stored_manifest.storage_key,
            entry_count=len(entries),
            voucher_count=len(claims),
        )
        _record_success(
            session,
            principal,
            result=result,
            period=period,
            company_id=principal.company_id,
            template_version=loaded_template.template_version,
            request_id=request_id,
        )
        return result
    except StorageError as exc:
        # 存储四道闸门（大小 / 真实魔数类型 / 路径安全 / 哈希）拒绝 → fail closed。
        _record_deny(
            session,
            principal,
            reason=CODE_STORAGE_ERROR,
            period=resolved_period,
            request_id=request_id,
        )
        raise ExportError(
            CODE_STORAGE_ERROR, "export artifact could not be stored"
        ) from exc
    except ExportError as exc:
        _record_deny(
            session,
            principal,
            reason=exc.code,
            period=resolved_period,
            request_id=request_id,
        )
        raise


def _authorize(session: Session, principal: Principal, config: ExportConfig) -> None:
    """命中 ``permissions`` 段任一 ``(resource, action, scope)`` 三元组才放行。"""
    permissions = config.permissions
    granted = any(
        has_permission(
            session, principal, permissions.resource, permissions.action, scope
        )
        for scope in permissions.scopes
    )
    if not granted:
        raise ExportAuthorizationError(
            CODE_MISSING_PERMISSION,
            "principal lacks any permission configured for export",
        )


def _employee_no(session: Session, employee_id: int) -> str:
    """返回员工工号（导出人标识；员工表无姓名字段，§4.3）。"""
    value = session.scalar(
        select(Employee.employee_no).where(Employee.id == employee_id)
    )
    return value or ""
