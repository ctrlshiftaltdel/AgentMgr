"""报销单与发票的关联（W1-23）服务层测试（跑在专用测试库 ``ajaimgr_test`` 上）。

逐条覆盖任务验收 a–g：

a. **关联成功**（本人 + draft）：``invoice_fields_json`` 票面要素快照正确（不含
   税号/校验码）、``duplicate_key == compute_duplicate_key(fields)``、
   ``source_file_id`` 记录、无查验行时 ``verification_status='pending_verification'``，
   成功审计 ``expense_claim.invoice.attach`` 的 details 不含票号/代码明文；
b. **状态只来源于查验记录**：预置七状态各一行 → attach 后逐字一致；用
   ``inspect.signature`` 断言 ``attach_invoice`` 无任何可传入状态的参数；
   ``sync_expense_invoice_status`` 在查验行变化后能更新（pending → verified_fail →
   verified_pass；查验行消失 → 回到待查验）；
c. **同单重复**：服务层先查后拒（可读 ``DuplicateInvoiceAttachError``）且行数不变；
   绕过服务层直接二次 INSERT → ``IntegrityError`` 且约束名**恰为**
   ``uq_expense_invoices_claim_duplicate``（防无关 FK 失败也通过）；
d. **跨单重复**：同 key 挂到第二张报销单 → 两行 ``suspected_duplicate=True``，
   ``find_cross_claim_duplicates`` 返回 2 行；
e. **submit 校验**：挂 ``verified_fail`` → 提交被拒（``ValidationError`` + 拒绝审计，
   claim 仍 draft）；挂账后查验行才变 fail（live sync）→ 同样被拒；
   ``pending_*`` / ``verified_pass`` / ``unable_to_verify`` / ``suspected_duplicate``
   → 提交成功且状态原样保留；无挂账发票的既有提交行为不变；
f. **越权**：他人 attach 到我的报销单 → 拒绝 + 拒绝审计、行未创建；非 draft
   报销单 attach → 拒绝；不存在的报销单 → ``ClaimNotFoundError``。
g. TRUNCATE 清单已加入 ``expense_invoices``（``app/tests/conftest.py``）。

``invoice_verifications`` 不在 ``app/tests/conftest.py`` 的 ``BUSINESS_TABLES`` 里，
因此本模块的 ``session`` 夹具在结束时连同 ``expense_invoices`` 一起清掉本模块写入
的发票查验行，保证测试可重复、顺序无关；``audit_events`` 由数据库触发器强制只增
不改，绝不删除。
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, func, inspect as sa_inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.audit.models import AuditEvent
from app.db.models.organization import Company, Department, Employee
from app.documents.models import SourceFile
from app.expense import invoices as invoices_module
from app.expense.invoices import (
    ATTACH_ACTION,
    DuplicateInvoiceAttachError,
    attach_invoice,
    find_cross_claim_duplicates,
    list_claim_invoices,
    sync_expense_invoice_status,
)
from app.expense.models import (
    ExpenseCategory,
    ExpenseClaim,
    ExpenseInvoice,
    ExpenseLine,
)
from app.expense.service import (
    AuthorizationDeniedError,
    ClaimNotFoundError,
    ValidationError,
    add_line,
    create_claim,
    submit,
)
from app.identity.authorization import Principal
from app.identity.models import EmployeeRole, Role
from app.identity.seed import clear_permission_catalog, seed_permission_catalog
from app.integrations.invoice_verification import (
    VerificationStatus,
    compute_duplicate_key,
)
from app.integrations.models import STATUS_VALUES, InvoiceVerification
from app.tests.integrations.invoice_fixtures import make_fields

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 允许提交的六种「非 fail」状态（``verified_fail`` 之外的全部七状态）。
SUBMITTABLE_STATUSES = (
    "pending_identification",
    "pending_manual_confirmation",
    "pending_verification",
    "verified_pass",
    "unable_to_verify",
    "suspected_duplicate",
)


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head（含 0011）再运行这些测试。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """删除本模块创建的全部业务行（先子后父），并清掉权限目录种子。

    ``invoice_verifications``（0010）不在共享 ``BUSINESS_TABLES`` 重置清单里，
    必须在这里清掉（早于 ``source_files``/``employees`` 删除，因外键引用）。
    **不删除** ``audit_events``：该表由数据库触发器强制只增不改。
    """
    session.execute(delete(ExpenseInvoice))
    session.execute(delete(InvoiceVerification))
    session.execute(delete(ExpenseLine))
    session.execute(delete(ExpenseClaim))
    session.execute(delete(SourceFile))
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


@dataclass
class World:
    """一套测试组织：公司、员工（报销人/旁人）、费用类型与一份原件。"""

    company: Company
    claimant: Employee
    outsider: Employee
    category: ExpenseCategory
    source_file: SourceFile

    def principal(self, employee: Employee) -> Principal:
        return Principal(employee_id=employee.id, company_id=self.company.id)


def _grant(session: Session, employee: Employee, role_name: str) -> None:
    role = session.scalar(select(Role).where(Role.name == role_name))
    assert role is not None, f"role {role_name!r} not seeded"
    session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))


@pytest.fixture()
def world(session: Session) -> World:
    """造出并提交一套组织数据（提交后服务写入可独立回滚/清理）。"""
    company = Company(name="W1-23 报销发票挂账")
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

    claimant = _employee("E-W1-23-claimant")
    outsider = _employee("E-W1-23-outsider")

    category = ExpenseCategory(company_id=company.id, code="CAT-W1-23", name="差旅")
    source_file = SourceFile(
        owner_id=claimant.id,
        storage_key="ab/cd/" + "c" * 64,
        sha256="c" * 64,
        mime_type="application/pdf",
        size_bytes=2048,
        original_name="invoice-w1-23.pdf",
        scan_status="clean",
    )
    session.add_all([category, source_file])
    session.commit()

    return World(
        company=company,
        claimant=claimant,
        outsider=outsider,
        category=category,
        source_file=source_file,
    )


# --- 造数据的组合辅助 ---------------------------------------------------------


def _draft(session: Session, world: World) -> ExpenseClaim:
    """创建一张草稿报销单（无明细）。"""
    return create_claim(session, world.principal(world.claimant))


def _draft_with_line(session: Session, world: World) -> ExpenseClaim:
    """创建一张带一条明细的草稿报销单（submit 需要非空明细）。"""
    claim = _draft(session, world)
    add_line(
        session,
        world.principal(world.claimant),
        claim.id,
        expense_date=date(2026, 10, 1),
        category_id=world.category.id,
        description="出差打车",
        amount=Decimal("100.00"),
    )
    return claim


def _verification_row(
    session: Session,
    fields,  # type: ignore[no-untyped-def]
    *,
    status: str,
    source_file_id: int | None = None,
) -> InvoiceVerification:
    """预置一条发票查验记录（状态来自七状态集合）。"""
    row = InvoiceVerification(
        source_file_id=source_file_id,
        invoice_no=fields.invoice_no,
        invoice_date=fields.invoice_date,
        invoice_code=fields.invoice_code,
        duplicate_key=compute_duplicate_key(fields),
        provider_type="authorized_provider",
        provider_name="test/authorized",
        status=status,
        verify_attempt_count=1,
        daily_verify_count=1,
        last_verify_date=date(2026, 10, 16),
    )
    session.add(row)
    session.flush()
    return row


def _events_for(session: Session, claim: ExpenseClaim) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_id == str(claim.id))
            .order_by(AuditEvent.id)
        )
    )


def _deny_events(session: Session, claim: ExpenseClaim) -> list[AuditEvent]:
    return [e for e in _events_for(session, claim) if e.action == "expense_claim.deny"]


def _attach_events(session: Session, claim: ExpenseClaim) -> list[AuditEvent]:
    return [e for e in _events_for(session, claim) if e.action == ATTACH_ACTION]


def _row_count(session: Session, claim_id: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ExpenseInvoice)
        .where(ExpenseInvoice.claim_id == claim_id)
    )


# --- a. 关联成功 --------------------------------------------------------------


def test_attach_success_snapshot_and_derived_status(
    session: Session, world: World
) -> None:
    """本人 + draft：快照/摘要/原件/推导状态正确，审计 details 不含明文。"""
    claimant = world.principal(world.claimant)
    claim = _draft(session, world)
    fields = make_fields()

    row = attach_invoice(
        session,
        claimant,
        claim.id,
        fields=fields,
        source_file_id=world.source_file.id,
        request_id="req-w1-23-attach",
    )

    key = compute_duplicate_key(fields)
    assert row.claim_id == claim.id
    assert row.source_file_id == world.source_file.id
    assert row.duplicate_key == key
    # 无查验行 → 明确待查验（绝不臆造「通过」）。
    assert row.verification_status == "pending_verification"
    assert row.suspected_duplicate is False

    # 票面要素快照：五要素、日期为 ISO 字符串、金额为字符串定点、不含税号。
    assert row.invoice_fields_json == {
        "invoice_code": "011002000211",
        "invoice_no": "12345678",
        "invoice_date": "2026-10-16",
        "invoice_kind": "01",
        "amount": "100.00",
    }
    assert isinstance(row.invoice_fields_json["amount"], str)
    assert "seller_tax_code" not in row.invoice_fields_json
    assert "buyer_tax_code" not in row.invoice_fields_json
    assert "check_code" not in row.invoice_fields_json

    # 数据库往返：JSONB 落库内容与对象一致。
    raw = session.execute(
        text("SELECT invoice_fields_json FROM expense_invoices WHERE id = :i"),
        {"i": row.id},
    ).scalar_one()
    assert raw == row.invoice_fields_json

    # 成功审计：action/actor/request_id 与 details（仅非敏感）。
    events = _attach_events(session, claim)
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == world.claimant.id
    assert event.request_id == "req-w1-23-attach"
    assert event.resource_type == "expense_claim"
    assert event.resource_id == str(claim.id)
    assert event.details["claim_id"] == claim.id
    assert event.details["duplicate_key"] == key
    assert event.details["has_source_file"] is True
    assert event.details["verification_status"] == "pending_verification"
    assert event.details["suspected_duplicate"] is False
    # 不含票号/代码明文（身份只以单向摘要出现）。
    blob = str(event.details)
    assert "12345678" not in blob
    assert "011002000211" not in blob
    # 快照/审计里都不含税号或校验码键。
    assert "tax_code" not in blob and "check_code" not in blob


# --- b. 状态来源与签名 ---------------------------------------------------------


def test_verification_status_copied_verbatim_for_all_seven_states(
    session: Session, world: World
) -> None:
    """预置七状态各一行 → attach 后状态逐字一致；未预置的行待查验。"""
    # 七状态集合与契约层同一集合（不得自造取值）。
    assert set(STATUS_VALUES) == {s.value for s in VerificationStatus}

    claimant = world.principal(world.claimant)
    claim = _draft(session, world)

    for index, status in enumerate(STATUS_VALUES):
        fields = make_fields(invoice_no=f"9{index:07d}")
        _verification_row(
            session, fields, status=status, source_file_id=world.source_file.id
        )
        row = attach_invoice(session, claimant, claim.id, fields=fields)
        assert row.verification_status == status  # 逐字复制，含未成功/疑似重复

    # 无查验行的一张 → pending_verification。
    loose = make_fields(invoice_no="80000000")
    loose_row = attach_invoice(session, claimant, claim.id, fields=loose)
    assert loose_row.verification_status == "pending_verification"

    # list_claim_invoices：只含本单、按 id 升序。
    listed = list_claim_invoices(session, claim)
    assert len(listed) == len(STATUS_VALUES) + 1
    assert [row.id for row in listed] == sorted(row.id for row in listed)
    assert {row.claim_id for row in listed} == {claim.id}


def test_attach_signature_has_no_status_parameter() -> None:
    """``attach_invoice`` 签名中没有任何可传入状态的参数（也没有 **kwargs）。"""
    signature = inspect.signature(attach_invoice)
    for name, parameter in signature.parameters.items():
        assert "status" not in name.lower(), (name, parameter)
        assert parameter.kind is not inspect.Parameter.VAR_KEYWORD, (name, parameter)
    assert set(signature.parameters) == {
        "session",
        "principal",
        "claim_id",
        "fields",
        "source_file_id",
        "request_id",
    }


def test_sync_follows_live_verification_row_changes(
    session: Session, world: World
) -> None:
    """sync：pending → verified_fail → verified_pass；查验行消失 → 回到待查验。"""
    claimant = world.principal(world.claimant)
    claim = _draft(session, world)
    fields = make_fields()

    row = attach_invoice(session, claimant, claim.id, fields=fields)
    assert row.verification_status == "pending_verification"

    verification = _verification_row(session, fields, status="verified_fail")
    sync_expense_invoice_status(session, row)
    assert row.verification_status == "verified_fail"
    # 数据库往返：同步值确实写出。
    session.flush()
    stored = session.execute(
        text("SELECT verification_status FROM expense_invoices WHERE id = :i"),
        {"i": row.id},
    ).scalar_one()
    assert stored == "verified_fail"

    # 查验行更新（如渠道/人工重查）后同步跟随。
    verification.status = "verified_pass"
    session.flush()
    sync_expense_invoice_status(session, row)
    assert row.verification_status == "verified_pass"

    # 查验行消失 → 回到「待查验」，绝不臆造结论。
    session.delete(verification)
    session.flush()
    sync_expense_invoice_status(session, row)
    assert row.verification_status == "pending_verification"


# --- c. 同一报销单内重复挂账 ---------------------------------------------------


def test_same_claim_duplicate_rejected_by_service(
    session: Session, world: World
) -> None:
    """同单重复：服务层先查后拒（可读错误）+ 拒绝审计；表内仍只有一行。"""
    claimant = world.principal(world.claimant)
    claim = _draft(session, world)
    fields = make_fields()
    attach_invoice(session, claimant, claim.id, fields=fields)

    with pytest.raises(DuplicateInvoiceAttachError) as err:
        attach_invoice(session, claimant, claim.id, fields=fields)
    # 可读错误：专属类型 + 明确的 attempted_action/reason。
    assert isinstance(err.value, ValidationError)
    assert err.value.attempted_action == "attach_invoice"
    assert err.value.reason == "invoice_already_attached"
    assert err.value.claim_id == claim.id

    assert _row_count(session, claim.id) == 1
    denials = _deny_events(session, claim)
    assert len(denials) == 1
    assert denials[0].actor_id == world.claimant.id
    assert denials[0].details == {
        "attempted_action": "attach_invoice",
        "reason": "invoice_already_attached",
    }


def test_same_claim_duplicate_unique_constraint_backstop(
    session: Session, world: World
) -> None:
    """约束兜底：绕过服务层直接二次 INSERT → 约束名恰为 uq_expense_invoices_claim_duplicate。"""
    claimant = world.principal(world.claimant)
    claim = _draft(session, world)
    fields = make_fields()
    first = attach_invoice(session, claimant, claim.id, fields=fields)
    session.flush()

    # 直接构造第二行（不经过服务层先查后拒），模拟并发竞态的写入路径。
    duplicate = ExpenseInvoice(
        claim_id=claim.id,
        source_file_id=None,
        invoice_fields_json=dict(first.invoice_fields_json),
        verification_status=first.verification_status,
        duplicate_key=first.duplicate_key,
        suspected_duplicate=False,
    )
    with pytest.raises(IntegrityError) as excinfo:
        with session.begin_nested():  # savepoint：失败只回滚本次 INSERT
            session.add(duplicate)
            session.flush()
    # 恰为目标唯一约束（而非偶发的外键/CHECK 失败）。
    assert (
        excinfo.value.orig.diag.constraint_name
        == "uq_expense_invoices_claim_duplicate"
    )
    assert "uq_expense_invoices_claim_duplicate" in str(excinfo.value)
    # 失败语句被回滚后，表内仍然只有第一行。
    assert _row_count(session, claim.id) == 1


# --- d. 跨报销单重复标记 -------------------------------------------------------


def test_cross_claim_duplicate_marks_both_rows(session: Session, world: World) -> None:
    """同 key 挂到第二张报销单 → 两行均 suspected_duplicate=True，查询入口返回 2 行。"""
    claimant = world.principal(world.claimant)
    claim_one = _draft(session, world)
    claim_two = _draft(session, world)
    fields = make_fields()

    first = attach_invoice(session, claimant, claim_one.id, fields=fields)
    assert first.suspected_duplicate is False  # 首次出现不是重复

    second = attach_invoice(session, claimant, claim_two.id, fields=fields)
    assert second.suspected_duplicate is True

    # 先挂的那一行也被同事务标记（数据库往返确认）。
    session.refresh(first)
    assert first.suspected_duplicate is True
    session.expire(second)
    assert second.suspected_duplicate is True

    key = compute_duplicate_key(fields)
    rows = find_cross_claim_duplicates(session, key)
    assert len(rows) == 2
    assert [row.id for row in rows] == sorted(row.id for row in rows)
    assert {row.claim_id for row in rows} == {claim_one.id, claim_two.id}
    assert all(row.suspected_duplicate for row in rows)

    # 同一张报销单里的另一张票不受影响（不同 key → 未标记、查询入口只见 1 行）。
    other_fields = make_fields(invoice_no="87654321")
    third = attach_invoice(session, claimant, claim_one.id, fields=other_fields)
    assert third.suspected_duplicate is False
    other_rows = find_cross_claim_duplicates(session, compute_duplicate_key(other_fields))
    assert [row.id for row in other_rows] == [third.id]


# --- e. submit 校验 ------------------------------------------------------------


def test_submit_rejected_when_attached_invoice_verification_failed(
    session: Session, world: World
) -> None:
    """挂 verified_fail → 提交被拒（ValidationError + 拒绝审计），claim 仍 draft。"""
    claimant = world.principal(world.claimant)
    claim = _draft_with_line(session, world)
    fields = make_fields()
    _verification_row(session, fields, status="verified_fail")
    row = attach_invoice(session, claimant, claim.id, fields=fields)
    assert row.verification_status == "verified_fail"  # 逐字复制

    with pytest.raises(ValidationError) as err:
        submit(session, claimant, claim.id)
    assert err.value.attempted_action == "submit"
    assert err.value.reason == "invoice_verification_failed"
    assert claim.status == "draft"
    assert claim.submitted_at is None

    denials = _deny_events(session, claim)
    assert len(denials) == 1
    assert denials[0].actor_id == world.claimant.id
    assert denials[0].details == {
        "attempted_action": "submit",
        "reason": "invoice_verification_failed",
    }
    # 挂账行状态原样保留（不得被改写为通过等其它值）。
    assert row.verification_status == "verified_fail"


def test_submit_rechecks_live_verification_status_via_sync(
    session: Session, world: World
) -> None:
    """挂账时无查验行（pending），提交前查验行才变 fail → 同样被拒（live 读）。"""
    claimant = world.principal(world.claimant)
    claim = _draft_with_line(session, world)
    fields = make_fields()

    row = attach_invoice(session, claimant, claim.id, fields=fields)
    assert row.verification_status == "pending_verification"
    # 挂账后才出现查验结论（如后续查验/人工录入）。
    _verification_row(session, fields, status="verified_fail")

    with pytest.raises(ValidationError) as err:
        submit(session, claimant, claim.id)
    assert err.value.reason == "invoice_verification_failed"
    assert claim.status == "draft"
    # submit 钩子先对全部挂账行做 sync：行已推进到当前查验状态。
    assert row.verification_status == "verified_fail"
    denials = _deny_events(session, claim)
    assert denials[-1].details["reason"] == "invoice_verification_failed"


@pytest.mark.parametrize("status", SUBMITTABLE_STATUSES)
def test_submit_allowed_for_non_fail_statuses_and_status_preserved(
    session: Session, world: World, status: str
) -> None:
    """verified_pass 与各「待…」/unable_to_verify/疑似重复 → 提交成功且状态原样。"""
    claimant = world.principal(world.claimant)
    claim = _draft_with_line(session, world)
    fields = make_fields()
    _verification_row(session, fields, status=status)
    row = attach_invoice(session, claimant, claim.id, fields=fields)
    assert row.verification_status == status

    submit(session, claimant, claim.id)
    assert claim.status == "submitted"
    assert claim.submitted_at is not None
    # 绝不改写：通过/待查验/无法查验/疑似重复都原样保留。
    assert row.verification_status == status
    session.flush()
    stored = session.execute(
        text("SELECT verification_status FROM expense_invoices WHERE id = :i"),
        {"i": row.id},
    ).scalar_one()
    assert stored == status


def test_submit_rejected_for_unknown_synced_status_defensive_branch(
    session: Session, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """防御分支：同步后状态不为七状态之一 → 拒绝（reason=invoice_status_unknown）。

    数据库 CHECK 让「七状态之外」的正常路径不可达，这里在内存中模拟该场景：
    替换 sync 为 no-op 并用 ``set_committed_value`` 写入未知值（保持 ORM 对象
    clean，避免 autoflush 把非法值写库）。
    """
    claimant = world.principal(world.claimant)
    claim = _draft_with_line(session, world)
    fields = make_fields()
    row = attach_invoice(session, claimant, claim.id, fields=fields)

    monkeypatch.setattr(
        invoices_module, "sync_expense_invoice_status", lambda session, row: row
    )
    set_committed_value(row, "verification_status", "bogus_status")

    with pytest.raises(ValidationError) as err:
        submit(session, claimant, claim.id)
    assert err.value.reason == "invoice_status_unknown"
    assert claim.status == "draft"
    denials = _deny_events(session, claim)
    assert denials[-1].details == {
        "attempted_action": "submit",
        "reason": "invoice_status_unknown",
    }


def test_submit_without_attached_invoices_unchanged(
    session: Session, world: World
) -> None:
    """无挂账发票的提交行为与既有完全一致（成功且状态 submitted）。"""
    claimant = world.principal(world.claimant)
    claim = _draft_with_line(session, world)

    submit(session, claimant, claim.id)
    assert claim.status == "submitted"
    assert claim.submitted_at is not None
    assert _deny_events(session, claim) == []
    assert session.scalar(
        select(func.count())
        .select_from(ExpenseInvoice)
        .where(ExpenseInvoice.claim_id == claim.id)
    ) == 0


# --- f. 越权与非法状态 ---------------------------------------------------------


def test_attach_denied_for_outsider(session: Session, world: World) -> None:
    """他人 attach 到我的报销单 → 拒绝 + 拒绝审计、行未创建。"""
    claim = _draft(session, world)
    fields = make_fields()

    with pytest.raises(AuthorizationDeniedError) as err:
        attach_invoice(session, world.principal(world.outsider), claim.id, fields=fields)
    assert err.value.attempted_action == "attach_invoice"
    assert err.value.reason == "not_claim_owner"
    assert err.value.claim_id == claim.id

    assert _row_count(session, claim.id) == 0
    denials = _deny_events(session, claim)
    assert len(denials) == 1
    assert denials[0].actor_id == world.outsider.id
    assert denials[0].details == {
        "attempted_action": "attach_invoice",
        "reason": "not_claim_owner",
    }


def test_attach_denied_for_non_draft_claim(session: Session, world: World) -> None:
    """非 draft 报销单 attach → 拒绝（claim_not_draft）+ 拒绝审计、行未创建。"""
    claimant = world.principal(world.claimant)
    claim = _draft_with_line(session, world)
    submit(session, claimant, claim.id)
    assert claim.status == "submitted"

    fields = make_fields()
    with pytest.raises(ValidationError) as err:
        attach_invoice(session, claimant, claim.id, fields=fields)
    assert err.value.attempted_action == "attach_invoice"
    assert err.value.reason == "claim_not_draft"
    assert claim.status == "submitted"

    assert _row_count(session, claim.id) == 0
    denials = _deny_events(session, claim)
    assert denials[-1].details == {
        "attempted_action": "attach_invoice",
        "reason": "claim_not_draft",
    }


def test_attach_claim_not_found(session: Session, world: World) -> None:
    """不存在的报销单 → ClaimNotFoundError。"""
    with pytest.raises(ClaimNotFoundError) as err:
        attach_invoice(
            session, world.principal(world.claimant), 987654321, fields=make_fields()
        )
    assert err.value.claim_id == 987654321


# --- 额外：级联删除（模型口径与 expense_lines 一致） ---------------------------


def test_rows_cascade_with_claim_delete(session: Session, world: World) -> None:
    """删除报销单级联删除其挂账行（claim_id FK ON DELETE CASCADE）。"""
    claimant = world.principal(world.claimant)
    claim = _draft(session, world)
    row = attach_invoice(session, claimant, claim.id, fields=make_fields())
    row_id = row.id
    session.flush()

    session.execute(delete(ExpenseClaim).where(ExpenseClaim.id == claim.id))
    session.flush()

    assert (
        session.scalar(select(ExpenseInvoice.id).where(ExpenseInvoice.id == row_id))
        is None
    )


# --- 迁移 0011 的往返与结构（本任务新增） --------------------------------------


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def _table_names(engine: Engine) -> set[str]:
    return set(sa_inspect(engine).get_table_names())


def _head_revision(config: Config) -> str:
    """当前迁移脚本的 head revision（不写死，避免未来新增迁移后本测试过期）。"""
    return ScriptDirectory.from_config(config).get_current_head()


def test_migration_0011_roundtrip_and_shape(engine: Engine) -> None:
    """head → 退到 0010 → 升回 head：expense_invoices 按预期增删，结构齐备。"""
    config = _alembic_config()
    head = _head_revision(config)
    try:
        command.upgrade(config, "head")
        assert "expense_invoices" in _table_names(engine)
        assert _current_revision(engine) == head

        command.downgrade(config, "0010_invoice_verifications")
        assert "expense_invoices" not in _table_names(engine)
        assert _current_revision(engine) == "0010_invoice_verifications"

        command.upgrade(config, "head")
        assert "expense_invoices" in _table_names(engine)
        assert _current_revision(engine) == head

        # 结构：唯一约束/索引名与模型一致；CHECK 覆盖七状态；无浮点列。
        inspector = sa_inspect(engine)
        unique_names = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("expense_invoices")
        }
        assert "uq_expense_invoices_claim_duplicate" in unique_names
        index_names = {
            index["name"] for index in inspector.get_indexes("expense_invoices")
        }
        assert {
            "ix_expense_invoices_duplicate_key",
            "ix_expense_invoices_claim_id",
        } <= index_names

        with engine.connect() as connection:
            checks = dict(
                connection.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid = to_regclass('expense_invoices') "
                        "AND contype = 'c'"
                    )
                ).all()
            )
        definition = checks["ck_expense_invoices_verification_status_allowed"]
        for status in STATUS_VALUES:
            assert status in definition

        for column in inspector.get_columns("expense_invoices"):
            rendered = str(column["type"]).upper()
            assert "FLOAT" not in rendered and "DOUBLE" not in rendered
            assert "REAL" not in rendered
    finally:
        # 无论断言是否失败，都恢复到 head，避免影响后续测试。
        command.upgrade(config, "head")
