"""Issue #56 写回路径的**落库**测试（跑在专用测试库 ``ajaimgr_test`` 上）。

覆盖「确认草稿 → 正式报销记录」的每一条验收：

* **成功**：确认后的草稿写成 ``expense_claims``（``draft``）+ ``expense_lines`` +
  ``expense_invoices``，金额/税额/费用日期按定稿口径映射，并写一条非敏感写回审计；
* **幂等**：同一草稿重复调用不重复建单（返回既有结果、``created=False``、审计只有
  一条 ``extraction_draft.writeback``）；
* **缺字段 fail closed**：业务必填字段缺失时不建任何正式记录，写拒绝审计；
* **未映射类别 fail closed**：``category_id`` 未提供 / 不存在 / 属其它公司一律拒绝；
* **越权**：非本人（``not_draft_owner``）或无 ``expense_claim/create/own`` 权限
  （``missing_permission``）一律拒绝且不建单；
* **挂账去重**：复用 W1-23 语义——``duplicate_key`` 与查验域同口径、跨报销单同票
  两边均置 ``suspected_duplicate``、状态只从查验记录推导；
* **无半成品**：写入中途失败后由调用方回滚，正式表不残留任何行。

纯逻辑部分（映射表/日期/金额/白名单）在 ``test_writeback_mapping.py`` 中且无需数据库。

``audit_events`` 由数据库触发器强制只增不改，绝不删除；未提交的审计写入随回滚消失。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.db.models.organization import Company, Department, Employee
from app.documents.models import SourceFile
from app.expense.invoices import find_cross_claim_duplicates
from app.expense.models import (
    CostCenter,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseInvoice,
    ExpenseLine,
)
from app.expense.service import AuthorizationDeniedError, ValidationError
from app.identity.authorization import Principal
from app.identity.models import EmployeeRole, Role
from app.identity.seed import clear_permission_catalog, seed_permission_catalog
from app.integrations.invoice_verification import (
    InvoiceFields,
    compute_duplicate_key,
)
from app.integrations.models import InvoiceVerification
from app.intake.models import ExtractionDraft, IntakeJob
from app.intake.parsing import InputKind, ManualEntryParser, ParseRequest
from app.intake.pipeline import confirm_draft, record_parse_draft
from app.intake.writeback import (
    WRITEBACK_ACTION,
    WRITEBACK_DENY_ACTION,
    DraftNotFoundError,
    write_back_confirmed_draft,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 一条完整票面的草稿取值（百度适配器口径：金额为 2 位小数字符串）。
INVOICE_VALUES: dict[str, object] = {
    "invoice_no": "12345678",
    "invoice_code": "011002000211",
    "invoice_date": "2026-10-01",
    "amount": "123.45",
    "tax_amount": "3.45",
}

#: 只含业务必填字段的草稿（无票面身份字段 → 不挂账）。
PLAIN_VALUES: dict[str, object] = {
    "expense_date": "2026-10-02",
    "amount": "10.00",
}


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """删除本文件创建的全部业务行（先子后父），并清掉权限目录种子。

    ``invoice_verifications`` 不在共享 ``BUSINESS_TABLES`` 重置清单里，因此一并清掉
    （早于 ``source_files`` / ``employees``，因外键引用）。**不删除** ``audit_events``。
    """
    session.execute(delete(ExpenseInvoice))
    session.execute(delete(InvoiceVerification))
    session.execute(delete(ExpenseLine))
    session.execute(delete(ExpenseClaim))
    session.execute(delete(CostCenter))
    session.execute(delete(ExpenseCategory))
    session.execute(delete(ExtractionDraft))
    session.execute(delete(SourceFile))
    session.execute(delete(IntakeJob))
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


@dataclass
class World:
    """一套测试组织：公司、报销人/旁人、费用类别、成本中心与一份原件。"""

    company: Company
    department: Department
    claimant: Employee
    outsider: Employee
    category: ExpenseCategory
    cost_center: CostCenter
    source_file: SourceFile

    def principal(self, employee: Employee) -> Principal:
        return Principal(employee_id=employee.id, company_id=self.company.id)


def _grant(session: Session, employee: Employee, role_name: str) -> None:
    role = session.scalar(select(Role).where(Role.name == role_name))
    assert role is not None, f"role {role_name!r} not seeded"
    session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))


@pytest.fixture()
def world(session: Session) -> World:
    """造出并提交一套组织数据（提交后写回写入可独立回滚/清理）。"""
    company = Company(name="W1-56 写回路径")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name="财务部")
    session.add(department)
    session.flush()

    def _employee(no: str) -> Employee:
        emp = Employee(
            company_id=company.id,
            department_id=department.id,
            employee_no=no,
        )
        session.add(emp)
        session.flush()
        _grant(session, emp, "employee")
        return emp

    claimant = _employee("E-writeback-claimant")
    outsider = _employee("E-writeback-outsider")

    category = ExpenseCategory(
        company_id=company.id, code="CAT-W1-56", name="差旅"
    )
    cost_center = CostCenter(company_id=company.id, code="CC-W1-56", name="总部")
    session.add(category)
    session.add(cost_center)
    session.flush()

    source_file = SourceFile(
        owner_id=claimant.id,
        storage_key="aa/" + ("a" * 64),
        sha256="a" * 64,
        mime_type="image/png",
        size_bytes=1024,
        original_name="receipt.png",
        scan_status="clean",
    )
    session.add(source_file)
    session.commit()

    return World(
        company=company,
        department=department,
        claimant=claimant,
        outsider=outsider,
        category=category,
        cost_center=cost_center,
        source_file=source_file,
    )


# --- 辅助 ---------------------------------------------------------------------


def _make_draft(
    session: Session,
    employee: Employee,
    *,
    values: dict[str, object],
    confidence: dict[str, float] | None = None,
    source_file_id: int | None = None,
    business_type: str = "expense_claim",
    confirm: bool = True,
) -> ExtractionDraft:
    """造一条草稿（默认已人工确认），返回它。

    有原件引用时用 ``image`` 输入，否则用 ``text`` 输入——``ParseRequest`` 对非文字
    输入强制要求 ``source_file_id``（fail closed），测试不绕过该约束。
    """
    input_kind = InputKind.TEXT if source_file_id is None else InputKind.IMAGE
    job = IntakeJob(
        submitted_by=employee.id,
        channel_type="weixin",
        input_type="image" if input_kind is InputKind.IMAGE else "text",
    )
    session.add(job)
    session.flush()

    parser = ManualEntryParser(values=values, field_confidence=confidence)
    request = ParseRequest(
        input_kind=input_kind,
        business_type=business_type,
        source_file_id=source_file_id,
    )
    result = parser.parse(request)
    job, draft = record_parse_draft(
        session,
        job=job,
        business_type=business_type,
        result=result,
        actor_id=employee.id,
    )
    assert draft is not None
    if confirm:
        confirm_draft(session, draft=draft, confirmed_by=employee.id)
    return draft


def _audit_events(session: Session, draft_id: int) -> list[AuditEvent]:
    """按资源类型 + ID 取审计事件（审计只增不改，必须双条件过滤）。"""
    return list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == "extraction_draft",
                AuditEvent.resource_id == str(draft_id),
            )
            .order_by(AuditEvent.id)
        )
    )


def _events(session: Session, draft_id: int, action: str) -> list[AuditEvent]:
    return [e for e in _audit_events(session, draft_id) if e.action == action]


def _claim_count(session: Session, employee_id: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ExpenseClaim)
        .where(ExpenseClaim.employee_id == employee_id)
    )


def _line_count(session: Session, claim_id: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ExpenseLine)
        .where(ExpenseLine.claim_id == claim_id)
    )


def _invoice_count(session: Session, claim_id: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ExpenseInvoice)
        .where(ExpenseInvoice.claim_id == claim_id)
    )


# --- 成功路径 -----------------------------------------------------------------


def test_writeback_creates_claim_line_and_invoice(session: Session, world: World) -> None:
    """确认草稿 → 报销单草稿 + 明细行 + 挂账发票（同事务），并写非敏感审计。"""
    draft = _make_draft(
        session,
        world.claimant,
        values=dict(INVOICE_VALUES),
        source_file_id=world.source_file.id,
    )
    session.commit()

    result = write_back_confirmed_draft(
        session,
        world.principal(world.claimant),
        draft.id,
        category_id=world.category.id,
        cost_center_id=world.cost_center.id,
    )
    session.commit()

    assert result.created is True
    claim = result.claim
    assert claim.status == "draft"  # 只是草稿，随后走既有提交/审批
    assert claim.employee_id == world.claimant.id
    assert claim.currency == "CNY"
    assert claim.total_amount == Decimal("123.45")
    assert claim.submitted_at is None

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.claim_id == claim.id
    assert line.expense_date == date(2026, 10, 1)  # 默认取票面开票日期
    assert line.category_id == world.category.id
    assert line.cost_center_id == world.cost_center.id
    assert line.amount == Decimal("123.45")
    assert line.tax_amount == Decimal("3.45")
    assert line.description  # 确定性摘要非空
    assert result.expense_date_source == "invoice_date"

    invoice = result.invoice
    assert invoice is not None
    assert invoice.claim_id == claim.id
    assert invoice.source_file_id == world.source_file.id  # 原件引用
    assert invoice.verification_status == "pending_verification"  # 无查验记录
    assert invoice.suspected_duplicate is False
    assert invoice.duplicate_key == compute_duplicate_key(
        InvoiceFields(
            invoice_no="12345678",
            invoice_code="011002000211",
            invoice_date=date(2026, 10, 1),
            amount="123.45",
        )
    )
    # 挂账快照只含白名单字段：不含税号/主体名（W1-23 同口径）。
    snapshot = invoice.invoice_fields_json
    assert set(snapshot) == {
        "invoice_code",
        "invoice_no",
        "invoice_date",
        "invoice_kind",
        "amount",
    }
    assert snapshot["amount"] == "123.45"

    writebacks = _events(session, draft.id, WRITEBACK_ACTION)
    assert len(writebacks) == 1
    details = writebacks[0].details
    assert writebacks[0].actor_id == world.claimant.id
    assert details["claim_id"] == claim.id
    assert details["claim_no"] == claim.claim_no
    assert details["line_count"] == 1
    assert details["invoice_attached"] is True
    assert details["invoice_skip_reason"] is None
    assert details["expense_date_source"] == "invoice_date"
    # 审计不得含票号/税号/主体名明文（票面身份只以单向摘要出现）。
    rendered = str(details)
    assert "12345678" not in rendered
    assert "011002000211" not in rendered


def test_writeback_copies_verification_status_from_verification_record(
    session: Session, world: World
) -> None:
    """挂账状态只从 ``invoice_verifications`` 逐字推导（W1-23 语义）。"""
    draft = _make_draft(
        session,
        world.claimant,
        values=dict(INVOICE_VALUES),
        source_file_id=world.source_file.id,
    )
    session.commit()

    duplicate_key = compute_duplicate_key(
        InvoiceFields(
            invoice_no="12345678",
            invoice_code="011002000211",
            invoice_date=date(2026, 10, 1),
        )
    )
    session.add(
        InvoiceVerification(
            invoice_no="12345678",
            invoice_code="011002000211",
            invoice_date=date(2026, 10, 1),
            duplicate_key=duplicate_key,
            provider_type="authorized_provider",
            provider_name="stub/authorized",
            status="verified_pass",
        )
    )
    session.commit()

    result = write_back_confirmed_draft(
        session,
        world.principal(world.claimant),
        draft.id,
        category_id=world.category.id,
    )
    session.commit()

    assert result.invoice is not None
    assert result.invoice.verification_status == "verified_pass"


def test_expense_date_override_wins_over_invoice_date(
    session: Session, world: World
) -> None:
    """确认时可人工覆盖费用日期（定稿口径）。"""
    draft = _make_draft(
        session,
        world.claimant,
        values=dict(INVOICE_VALUES),
        source_file_id=world.source_file.id,
    )
    session.commit()

    result = write_back_confirmed_draft(
        session,
        world.principal(world.claimant),
        draft.id,
        category_id=world.category.id,
        expense_date="2026-09-30",
    )
    session.commit()

    assert result.expense_date_source == "override"
    assert result.lines[0].expense_date == date(2026, 9, 30)


def test_missing_invoice_fields_creates_claim_without_attach(
    session: Session, world: World
) -> None:
    """票面字段不足时不挂账，但显式记录原因（不静默、不臆造票面）。"""
    draft = _make_draft(session, world.claimant, values=dict(PLAIN_VALUES))
    session.commit()

    result = write_back_confirmed_draft(
        session,
        world.principal(world.claimant),
        draft.id,
        category_id=world.category.id,
    )
    session.commit()

    assert result.invoice is None
    assert result.invoice_skip_reason == "missing_invoice_fields"
    assert result.expense_date_source == "expense_date"  # 业务字段名回退来源
    assert _invoice_count(session, result.claim.id) == 0

    details = _events(session, draft.id, WRITEBACK_ACTION)[0].details
    assert details["invoice_attached"] is False
    assert details["invoice_skip_reason"] == "missing_invoice_fields"


def test_low_confidence_fields_are_marked_in_audit(
    session: Session, world: World
) -> None:
    """低置信度字段名进入写回审计标记（只记字段名，不记取值）。"""
    draft = _make_draft(
        session,
        world.claimant,
        values={**INVOICE_VALUES, "seller_name": "示例供应商"},
        confidence={"amount": 0.2},
        source_file_id=world.source_file.id,
    )
    session.commit()

    result = write_back_confirmed_draft(
        session,
        world.principal(world.claimant),
        draft.id,
        category_id=world.category.id,
    )
    session.commit()

    assert result.low_confidence_fields == ("amount",)
    details = _events(session, draft.id, WRITEBACK_ACTION)[0].details
    assert details["low_confidence_fields"] == ["amount"]
    # 草稿**字段取值**不进审计（字段名可以出现）；票号/主体名明文同样不得出现。
    rendered = str(details)
    assert "示例供应商" not in rendered
    assert "12345678" not in rendered
    assert "011002000211" not in rendered
    assert "seller_name" in rendered  # 字段名可以出现（用于追溯哪些字段被提取）


# --- 幂等 ---------------------------------------------------------------------


def test_repeated_writeback_is_idempotent(session: Session, world: World) -> None:
    """同一草稿重复调用不重复建单：返回既有结果、只写一条写回审计。"""
    draft = _make_draft(
        session,
        world.claimant,
        values=dict(INVOICE_VALUES),
        source_file_id=world.source_file.id,
    )
    session.commit()
    principal = world.principal(world.claimant)

    first = write_back_confirmed_draft(
        session, principal, draft.id, category_id=world.category.id
    )
    session.commit()
    second = write_back_confirmed_draft(
        session, principal, draft.id, category_id=world.category.id
    )
    session.commit()

    assert first.created is True
    assert second.created is False
    assert second.claim.id == first.claim.id
    assert second.claim.claim_no == first.claim.claim_no
    assert second.invoice is not None and first.invoice is not None
    assert second.invoice.id == first.invoice.id
    assert [line.id for line in second.lines] == [line.id for line in first.lines]
    assert second.expense_date_source == "invoice_date"
    assert second.invoice_skip_reason is None

    assert _claim_count(session, world.claimant.id) == 1
    assert _line_count(session, first.claim.id) == 1
    assert _invoice_count(session, first.claim.id) == 1
    assert len(_events(session, draft.id, WRITEBACK_ACTION)) == 1


# --- fail closed：缺字段 / 类别 / 越权 ----------------------------------------


def test_missing_required_field_fails_closed(session: Session, world: World) -> None:
    """业务必填字段缺失（无 amount）→ 拒绝且不建任何正式记录。"""
    draft = _make_draft(
        session, world.claimant, values={"invoice_date": "2026-10-01"}
    )
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "missing_required_fields"
    assert _claim_count(session, world.claimant.id) == 0
    denies = _events(session, draft.id, WRITEBACK_DENY_ACTION)
    assert denies and denies[-1].details["missing_fields"] == ["amount"]


def test_unparseable_expense_date_fails_closed(
    session: Session, world: World
) -> None:
    """日期形态不可识别 → 拒绝（不猜测格式）。"""
    draft = _make_draft(
        session,
        world.claimant,
        values={"invoice_date": "2026年10月01日", "amount": "10.00"},
    )
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "invalid_expense_date"
    assert _claim_count(session, world.claimant.id) == 0


def test_float_amount_fails_closed(session: Session, world: World) -> None:
    """草稿里的 JSON 数字（float）金额被拒绝：正式金额必须固定精度。"""
    draft = _make_draft(
        session,
        world.claimant,
        values={"invoice_date": "2026-10-01", "amount": 123.45},
    )
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "invalid_amount"
    assert _claim_count(session, world.claimant.id) == 0


def test_zero_amount_fails_closed(session: Session, world: World) -> None:
    """金额必须为正：零金额不生成报销单（避免无意义草稿单）。"""
    draft = _make_draft(
        session,
        world.claimant,
        values={"invoice_date": "2026-10-01", "amount": "0.00"},
    )
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "invalid_amount"
    assert _claim_count(session, world.claimant.id) == 0


def test_unconfirmed_draft_fails_closed(session: Session, world: World) -> None:
    """未人工确认的草稿不得进入正式记录（人工确认是唯一入口）。"""
    draft = _make_draft(
        session,
        world.claimant,
        values=dict(INVOICE_VALUES),
        source_file_id=world.source_file.id,
        confirm=False,
    )
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "draft_not_confirmed"
    assert _claim_count(session, world.claimant.id) == 0


def test_unsupported_business_type_fails_closed(
    session: Session, world: World
) -> None:
    """本写入路径只支持报销单；其它业务类型一律拒绝。"""
    draft = _make_draft(
        session,
        world.claimant,
        values={"amount": "10.00"},
        business_type="purchase_request",
    )
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "unsupported_business_type"
    assert _claim_count(session, world.claimant.id) == 0


@pytest.mark.parametrize(
    "category_id,expected_reason",
    [
        (None, "category_not_provided"),
        (True, "category_not_provided"),
        (10**9, "category_not_found"),
    ],
)
def test_unmapped_category_fails_closed(
    session: Session, world: World, category_id: object, expected_reason: str
) -> None:
    """费用类别必须由调用方显式映射：未提供 / 不存在一律 fail closed。"""
    draft = _make_draft(session, world.claimant, values=dict(PLAIN_VALUES))
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=category_id,  # type: ignore[arg-type]
        )

    assert exc.value.reason == expected_reason
    assert _claim_count(session, world.claimant.id) == 0


def test_category_of_another_company_fails_closed(
    session: Session, world: World
) -> None:
    """类别必须属于调用主体的公司（跨公司类别被拒绝）。"""
    other_company = Company(name="W1-56 另一家公司")
    session.add(other_company)
    session.flush()
    other_category = ExpenseCategory(
        company_id=other_company.id, code="CAT-OTHER", name="其它公司类别"
    )
    session.add(other_category)
    session.commit()

    draft = _make_draft(session, world.claimant, values=dict(PLAIN_VALUES))
    session.commit()

    with pytest.raises(ValidationError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=other_category.id,
        )

    assert exc.value.reason == "category_company_mismatch"
    assert _claim_count(session, world.claimant.id) == 0


def test_outsider_cannot_write_back(session: Session, world: World) -> None:
    """他人草稿不得写成自己名下的正式记录。"""
    draft = _make_draft(session, world.claimant, values=dict(PLAIN_VALUES))
    session.commit()

    with pytest.raises(AuthorizationDeniedError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(world.outsider),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "not_draft_owner"
    assert _claim_count(session, world.outsider.id) == 0
    assert _claim_count(session, world.claimant.id) == 0
    denies = _events(session, draft.id, WRITEBACK_DENY_ACTION)
    assert denies and denies[-1].actor_id == world.outsider.id
    assert denies[-1].details["reason"] == "not_draft_owner"


def test_missing_create_permission_fails_closed(
    session: Session, world: World
) -> None:
    """无 ``expense_claim/create/own`` 权限的员工（无角色）被拒绝。"""
    norole = Employee(
        company_id=world.company.id,
        department_id=world.department.id,
        employee_no="E-writeback-norole",
    )
    session.add(norole)
    session.commit()

    draft = _make_draft(session, norole, values=dict(PLAIN_VALUES))
    session.commit()

    with pytest.raises(AuthorizationDeniedError) as exc:
        write_back_confirmed_draft(
            session,
            world.principal(norole),
            draft.id,
            category_id=world.category.id,
        )

    assert exc.value.reason == "missing_permission"
    assert _claim_count(session, norole.id) == 0


def test_unknown_draft_fails_closed(session: Session, world: World) -> None:
    """不存在的草稿 → ``DraftNotFoundError``，并写拒绝审计。"""
    missing_draft_id = world.source_file.id + 10**6
    with pytest.raises(DraftNotFoundError):
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            missing_draft_id,
            category_id=world.category.id,
        )

    assert _claim_count(session, world.claimant.id) == 0
    denies = _events(session, missing_draft_id, WRITEBACK_DENY_ACTION)
    assert denies and denies[-1].details["reason"] == "draft_not_found"


# --- 挂账去重 -----------------------------------------------------------------


def test_same_invoice_across_claims_marks_both_suspected_duplicate(
    session: Session, world: World
) -> None:
    """同一张票挂到第二张报销单：两边均置 ``suspected_duplicate``（W1-23 语义）。"""
    principal = world.principal(world.claimant)

    first_draft = _make_draft(
        session,
        world.claimant,
        values=dict(INVOICE_VALUES),
        source_file_id=world.source_file.id,
    )
    session.commit()
    first = write_back_confirmed_draft(
        session, principal, first_draft.id, category_id=world.category.id
    )
    session.commit()

    second_draft = _make_draft(
        session,
        world.claimant,
        values=dict(INVOICE_VALUES),
        source_file_id=world.source_file.id,
    )
    session.commit()
    second = write_back_confirmed_draft(
        session, principal, second_draft.id, category_id=world.category.id
    )
    session.commit()

    assert first.invoice is not None and second.invoice is not None
    assert second.claim.id != first.claim.id
    assert second.invoice.duplicate_key == first.invoice.duplicate_key
    session.refresh(first.invoice)
    assert first.invoice.suspected_duplicate is True
    assert second.invoice.suspected_duplicate is True

    rows = find_cross_claim_duplicates(session, second.invoice.duplicate_key)
    assert {row.claim_id for row in rows} == {first.claim.id, second.claim.id}


# --- 无半成品 -----------------------------------------------------------------


def test_failed_write_leaves_no_partial_rows_after_rollback(
    session: Session, world: World
) -> None:
    """写入中途失败（摘要超长）后由调用方回滚，正式表不残留任何行。"""
    draft = _make_draft(session, world.claimant, values=dict(PLAIN_VALUES))
    session.commit()

    with pytest.raises(ValidationError):
        write_back_confirmed_draft(
            session,
            world.principal(world.claimant),
            draft.id,
            category_id=world.category.id,
            description="x" * 600,  # 超过 DESCRIPTION_MAX_LENGTH
        )
    session.rollback()

    assert _claim_count(session, world.claimant.id) == 0
    assert session.scalar(select(func.count()).select_from(ExpenseLine)) == 0
    assert session.scalar(select(func.count()).select_from(ExpenseInvoice)) == 0
