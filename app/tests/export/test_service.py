"""服务层入口测试（**触库**；在专用测试库 ``ajaimgr_test`` 上跑）。

覆盖 Issue #53 验收标准第 7、8 条：

* ``export_claims_period`` 只读投影（导出前后报销域业务状态**逐字段不变**）；
* 写审计（成功 ``finance.export.period`` / 拒绝 ``finance.export.period.deny``）；
* 幂等（同期同模板：manifest 除 ``export_id``/``exported_at`` 外逐字段一致；
  文件字节可复现 → ``file.sha256`` 相同）；
* 导出文件与 manifest 落内容寻址存储（``app.documents.storage``）并各写一行
  ``source_files``；
* fail closed：无权限 / 校验失败 / 模板指纹不符 → **不写文件、不写 manifest**。

本文件需要 PostgreSQL；本机纯逻辑子集用 ``--noconftest`` 排除本文件。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from openpyxl import load_workbook
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.db.models.organization import Company, Department, Employee
from app.documents.models import SourceFile
from app.documents.storage import verify_file
from app.expense.models import (
    ExpenseCategory,
    ExpenseClaim,
    ExpenseInvoice,
    ExpenseLine,
    ReimbursementPayment,
)
from app.export.errors import (
    CODE_MISSING_PERMISSION,
    ExportAuthorizationError,
    ExportError,
    ExportTemplateError,
    ExportValidationError,
)
from app.export.manifest import strip_volatile
from app.export.service import (
    ACTION_DENY,
    ACTION_EXPORT,
    export_claims_period,
)
from app.export.template import WORKSHEET_ENTRY_ROWS
from app.identity.authorization import Principal
from app.identity.models import EmployeeRole, Role
from app.identity.seed import clear_permission_catalog, seed_permission_catalog
from app.integrations.models import InvoiceVerification

from _helpers import make_config

REPO_ROOT = Path(__file__).resolve().parents[3]
PERIOD = "2026-10"
DUPLICATE_KEY = "b" * 64


def _alembic_config() -> AlembicConfig:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    session.execute(delete(SourceFile))
    session.execute(delete(InvoiceVerification))
    session.execute(delete(ReimbursementPayment))
    session.execute(delete(ExpenseInvoice))
    session.execute(delete(ExpenseLine))
    session.execute(delete(ExpenseClaim))
    session.execute(delete(ExpenseCategory))
    session.execute(delete(EmployeeRole))
    clear_permission_catalog(session)
    session.execute(delete(Employee))
    session.execute(delete(Department))
    session.execute(delete(Company))


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            seed_permission_catalog(sess)
            sess.commit()
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


@pytest.fixture()
def world(session: Session) -> dict[str, object]:
    """一套测试组织：公司、报销人、财务经办、无权限员工、费用类别、已付款报销单。"""
    company = Company(name="TrackD 导出测试")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name="财务部")
    session.add(department)
    session.flush()

    def _employee(no: str) -> Employee:
        employee = Employee(
            company_id=company.id, department_id=department.id, employee_no=no
        )
        session.add(employee)
        session.flush()
        return employee

    claimant = _employee("E-claimant")
    finance = _employee("E-finance")
    outsider = _employee("E-outsider")
    for employee, role_name in (
        (claimant, "employee"),
        (finance, "finance_operator"),
    ):
        role = session.scalar(select(Role).where(Role.name == role_name))
        assert role is not None
        session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))

    category = ExpenseCategory(company_id=company.id, code="CAT-TRV", name="差旅")
    session.add(category)
    session.flush()

    claim = ExpenseClaim(
        claim_no="EC-00000001",
        employee_id=claimant.id,
        currency="CNY",
        total_amount=Decimal("100.00"),
        status="paid",
    )
    session.add(claim)
    session.flush()
    session.add(
        ExpenseLine(
            claim_id=claim.id,
            expense_date=date(2026, 10, 5),
            category_id=category.id,
            description="出差打车",
            amount=Decimal("100.00"),
            tax_amount=Decimal("0.00"),
        )
    )
    session.add(
        ReimbursementPayment(
            claim_id=claim.id,
            amount=Decimal("100.00"),
            currency="CNY",
            status="paid",
            paid_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
        )
    )
    session.add(
        ExpenseInvoice(
            claim_id=claim.id,
            invoice_fields_json={"invoice_no": "0001", "amount": "100.00"},
            verification_status="verified_pass",
            duplicate_key=DUPLICATE_KEY,
            suspected_duplicate=False,
        )
    )
    session.add(
        InvoiceVerification(
            invoice_no="0001",
            invoice_date=date(2026, 10, 5),
            amount_without_tax=Decimal("100.00"),
            tax_amount=Decimal("0.00"),
            amount_with_tax=Decimal("100.00"),
            currency="CNY",
            duplicate_key=DUPLICATE_KEY,
            provider_type="manual_assisted",
            status="verified_pass",
        )
    )
    session.commit()
    return {
        "company": company,
        "claimant": claimant,
        "finance": finance,
        "outsider": outsider,
        "claim": claim,
    }


def _principal(world: dict[str, object], key: str) -> Principal:
    company = world["company"]
    employee = world[key]
    return Principal(employee_id=employee.id, company_id=company.id)  # type: ignore[union-attr]


def _snapshot(session: Session) -> list[tuple[object, ...]]:
    """报销域业务行的快照（用于「导出前后状态不变」断言）。"""
    claims = sorted(
        (c.id, c.claim_no, c.status, c.total_amount) for c in session.scalars(select(ExpenseClaim))
    )
    lines = sorted(
        (line.id, line.claim_id, line.expense_date, line.amount, line.tax_amount, line.description)
        for line in session.scalars(select(ExpenseLine))
    )
    payments = sorted(
        (p.id, p.claim_id, p.amount, p.status, p.paid_at)
        for p in session.scalars(select(ReimbursementPayment))
    )
    invoices = sorted(
        (i.id, i.claim_id, i.verification_status, i.duplicate_key, i.suspected_duplicate)
        for i in session.scalars(select(ExpenseInvoice))
    )
    return [tuple(claims), tuple(lines), tuple(payments), tuple(invoices)]


# --- 快乐路径 -----------------------------------------------------------------


def test_export_end_to_end(session: Session, world, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    principal = _principal(world, "finance")
    before = _snapshot(session)

    result = export_claims_period(
        session,
        principal,
        period=PERIOD,
        config=config,
        storage_root=tmp_path / "storage",
        now=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )

    # 只读投影：业务状态逐字段不变。
    assert _snapshot(session) == before

    # manifest 八项。
    manifest = result.manifest
    assert manifest["manifest_version"] == 1
    assert manifest["scope"]["period"] == PERIOD
    assert manifest["scope"]["claim_ids"] == [world["claim"].id]
    assert manifest["scope"]["claim_nos"] == ["EC-00000001"]
    assert manifest["scope"]["invoice_duplicate_keys"] == [DUPLICATE_KEY]
    assert manifest["template"]["sha256"] == config.template.sha256
    assert manifest["template"]["worksheet"] == WORKSHEET_ENTRY_ROWS
    assert manifest["rows"] == {
        "voucher_count": 1,
        "entry_count": 2,
        "expense_line_count": 1,
        "invoice_count": 1,
    }
    assert manifest["amounts"] == {
        "gross_total": "100.00",
        "net_total": "100.00",
        "tax_total": "0.00",
        "debit_total": "100.00",
        "credit_total": "100.00",
    }
    assert manifest["file"]["sha256"] == result.file_sha256
    assert manifest["file"]["storage_key"] == result.file_storage_key
    assert manifest["exported_by"] == {
        "employee_id": principal.employee_id,
        "employee_no": "E-finance",
    }
    assert manifest["exported_at"].startswith("2026-11-01T12:00:00")

    # 文件落内容寻址存储且哈希可复核。
    root = tmp_path / "storage"
    verify_file(root, result.file_storage_key, result.file_sha256)
    verify_file(root, result.manifest_storage_key, result.manifest_sha256)

    # 导出文件可被 openpyxl 读回，且只写了两条分录。
    from app.documents.storage import read_file

    workbook = load_workbook(io.BytesIO(read_file(root, result.file_storage_key)))
    sheet = workbook[WORKSHEET_ENTRY_ROWS]
    assert sheet.max_row == 3
    assert sheet[2][4].value == "560101"
    assert sheet[3][4].value == "100201"

    # 两行 source_files 元数据（导出文件 + manifest）。
    files = list(session.scalars(select(SourceFile)))
    assert len(files) == 2
    assert {f.sha256 for f in files} == {
        result.file_sha256,
        result.manifest_sha256,
    }

    # 成功审计。
    events = list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.action == ACTION_EXPORT)
        )
    )
    assert len(events) == 1
    assert events[0].actor_id == principal.employee_id
    assert events[0].details["file_sha256"] == result.file_sha256
    assert events[0].details["entry_count"] == 2


def test_export_is_idempotent(session: Session, world, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    principal = _principal(world, "finance")
    root = tmp_path / "storage"

    first = export_claims_period(
        session, principal, period=PERIOD, config=config, storage_root=root
    )
    second = export_claims_period(
        session, principal, period=PERIOD, config=config, storage_root=root
    )

    assert first.export_id != second.export_id
    assert strip_volatile(first.manifest) == strip_volatile(second.manifest)
    assert first.file_sha256 == second.file_sha256
    assert first.file_storage_key == second.file_storage_key
    # 内容寻址：相同内容只落一份 blob（两次导出 + manifest = 2 个物理文件）。
    blobs = [p for p in root.rglob("*") if p.is_file()]
    assert len(blobs) == 2


# --- 拒绝路径 ----------------------------------------------------------------


def test_missing_permission_fails_closed(session: Session, world, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    principal = _principal(world, "outsider")
    before = _snapshot(session)
    storage = tmp_path / "storage"

    with pytest.raises(ExportAuthorizationError) as excinfo:
        export_claims_period(
            session, principal, period=PERIOD, config=config, storage_root=storage
        )
    assert excinfo.value.code == CODE_MISSING_PERMISSION
    assert _snapshot(session) == before
    assert not storage.exists()
    denies = list(
        session.scalars(select(AuditEvent).where(AuditEvent.action == ACTION_DENY))
    )
    assert len(denies) == 1
    assert denies[0].details["reason"] == CODE_MISSING_PERMISSION


def test_template_fingerprint_mismatch_fails_closed(
    session: Session, world, tmp_path: Path
) -> None:
    config = make_config(tmp_path, template_sha="c" * 64)
    principal = _principal(world, "finance")
    storage = tmp_path / "storage"

    with pytest.raises(ExportTemplateError):
        export_claims_period(
            session, principal, period=PERIOD, config=config, storage_root=storage
        )
    assert not storage.exists()
    assert list(session.scalars(select(SourceFile))) == []


def test_validation_failure_fails_closed(
    session: Session, world, tmp_path: Path
) -> None:
    """付款金额与报销单合计不符（E5）→ 不产出文件、不写 manifest、写拒绝审计。"""
    config = make_config(tmp_path)
    claim = world["claim"]
    claim.total_amount = Decimal("120.00")  # 与明细/付款不符
    session.commit()
    principal = _principal(world, "finance")
    storage = tmp_path / "storage"

    with pytest.raises(ExportValidationError):
        export_claims_period(
            session, principal, period=PERIOD, config=config, storage_root=storage
        )
    assert not storage.exists()
    assert list(session.scalars(select(SourceFile))) == []
    denies = list(
        session.scalars(select(AuditEvent).where(AuditEvent.action == ACTION_DENY))
    )
    assert len(denies) == 1


def test_no_records_in_period_fails_closed(
    session: Session, world, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    principal = _principal(world, "finance")
    storage = tmp_path / "storage"

    with pytest.raises(ExportValidationError) as excinfo:
        export_claims_period(
            session,
            principal,
            period="2026-09",
            config=config,
            storage_root=storage,
        )
    assert excinfo.value.code == "no_records_in_period"
    assert not storage.exists()


def test_invalid_period_fails_closed(session: Session, world, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    principal = _principal(world, "finance")

    with pytest.raises(ExportError) as excinfo:
        export_claims_period(
            session,
            principal,
            period="2026-13",
            config=config,
            storage_root=tmp_path / "storage",
        )
    assert excinfo.value.code == "period_invalid"


def test_manifest_json_is_written_to_storage(
    session: Session, world, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    principal = _principal(world, "finance")
    result = export_claims_period(
        session,
        principal,
        period=PERIOD,
        config=config,
        storage_root=tmp_path / "storage",
    )
    from app.documents.storage import read_file

    payload = json.loads(read_file(tmp_path / "storage", result.manifest_storage_key))
    assert payload["export_id"] == result.export_id
    assert payload["checks"]


def test_second_company_data_is_not_exported(
    session: Session, world, tmp_path: Path
) -> None:
    """数据范围限定在公司内：另一公司的报销单不得进入导出。"""
    other_company = Company(name="TrackD 其它公司")
    session.add(other_company)
    session.flush()
    other_employee = Employee(
        company_id=other_company.id, employee_no="E-other"
    )
    session.add(other_employee)
    session.flush()
    other_claim = ExpenseClaim(
        claim_no="EC-00000002",
        employee_id=other_employee.id,
        currency="CNY",
        total_amount=Decimal("100.00"),
        status="paid",
    )
    session.add(other_claim)
    session.flush()
    category = session.scalar(select(ExpenseCategory))
    session.add(
        ExpenseLine(
            claim_id=other_claim.id,
            expense_date=date(2026, 10, 6),
            category_id=category.id,
            description="其它公司差旅",
            amount=Decimal("100.00"),
            tax_amount=Decimal("0.00"),
        )
    )
    session.commit()

    config = make_config(tmp_path)
    result = export_claims_period(
        session,
        _principal(world, "finance"),
        period=PERIOD,
        config=config,
        storage_root=tmp_path / "storage",
    )
    assert result.manifest["scope"]["claim_nos"] == ["EC-00000001"]


def test_audit_events_count_for_success_is_one(
    session: Session, world, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    principal = _principal(world, "finance")
    export_claims_period(
        session,
        principal,
        period=PERIOD,
        config=config,
        storage_root=tmp_path / "storage",
    )
    assert session.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.action == ACTION_EXPORT)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.action == ACTION_DENY)
    ) == 0


def test_storage_failure_fails_closed(
    session: Session, world, tmp_path: Path, monkeypatch
) -> None:
    """存储闸门拒绝（大小/类型/哈希）→ fail closed + 拒绝审计，不写元数据。"""
    import app.export.service as service_module
    from app.documents.storage import StorageError

    def _boom(*args: object, **kwargs: object) -> object:
        raise StorageError("storage gate rejected the artifact")

    monkeypatch.setattr(service_module, "store_file", _boom)
    config = make_config(tmp_path)

    with pytest.raises(ExportError) as excinfo:
        export_claims_period(
            session,
            _principal(world, "finance"),
            period=PERIOD,
            config=config,
            storage_root=tmp_path / "storage",
        )
    assert excinfo.value.code == "storage_error"
    assert list(session.scalars(select(SourceFile))) == []
    denies = list(
        session.scalars(select(AuditEvent).where(AuditEvent.action == ACTION_DENY))
    )
    assert len(denies) == 1
