"""报销服务层测试（跑在专用测试库 ``ajaimgr_test`` 上）。

覆盖任务 W1-7 第四步要求的十项：

1. **端到端快乐路径**：创建→明细→提交→主管审批→财务复核→财务主管付款，
   终态 ``paid``、``total_amount`` 精确、``reimbursement_payments`` 有且仅有一行；
2. **越权**：另一名员工 ``add_line``/``submit``/``withdraw`` 均抛
   ``AuthorizationDeniedError``，claim 状态与 total 不变，且各新增一条拒绝审计；
3. **自审**：拥有 approve 权限的主管批自己的报销被拒（并写拒绝审计）；
4. **无审批权限**的员工调 ``approve`` 被拒；
5. **财务复核需 finance_review**：主管调被拒；财务经办可调；
6. **付款需 payment/record**：财务经办调被拒；财务主管可调；
7. **非法迁移**：对已 ``paid`` 的 claim 再 ``submit`` → ``InvalidTransitionError``
   且状态不变；
8. **空明细提交**被拒且状态不变；
9. **审计计数**：快乐路径每个成功迁移恰好写一条事件，action 与 actor_id 正确；
   拒绝事件不计入成功迁移；
10. **float 金额**被拒（``TypeError``）且不产生部分写入。

每个测试自行造 company/department/employee/角色授予/费用类型，session fixture 在
结束后回滚未提交的服务写入并删除业务行（``audit_events`` 由数据库触发器强制
只增不改，绝不删除；未提交的审计写入随回滚消失，不残留）。
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
from app.expense.models import (
    CostCenter,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseLine,
    ReimbursementPayment,
)
from app.expense.service import (
    AuthorizationDeniedError,
    ValidationError,
    add_line,
    approve,
    create_claim,
    finance_review,
    record_payment,
    reject,
    return_for_fix,
    submit,
    withdraw,
)
from app.expense.state_machine import InvalidTransitionError
from app.identity.authorization import Principal
from app.identity.models import EmployeeRole, Role
from app.identity.seed import clear_permission_catalog, seed_permission_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head（含 0006 序列）再运行这些测试。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """删除这些测试创建的全部业务行（先子后父），并清掉权限目录种子。

    **不删除** ``audit_events``：该表由数据库触发器强制只增不改。
    """
    session.execute(delete(ReimbursementPayment))
    session.execute(delete(ExpenseLine))
    session.execute(delete(ExpenseClaim))
    session.execute(delete(CostCenter))
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
    """一套测试组织：公司、四类员工、费用类型与成本中心。"""

    company: Company
    claimant: Employee
    manager: Employee
    outsider: Employee
    finance_operator: Employee
    finance_lead: Employee
    category: ExpenseCategory
    cost_center: CostCenter

    def principal(self, employee: Employee) -> Principal:
        return Principal(employee_id=employee.id, company_id=self.company.id)


def _grant(session: Session, employee: Employee, role_name: str) -> None:
    role = session.scalar(select(Role).where(Role.name == role_name))
    assert role is not None, f"role {role_name!r} not seeded"
    session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))


@pytest.fixture()
def world(session: Session) -> World:
    """造出并提交一套组织数据（提交后服务写入可独立回滚）。"""
    company = Company(name="W1-7 报销服务")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name="财务部")
    session.add(department)
    session.flush()

    def _employee(no: str, *, manager: Employee | None = None) -> Employee:
        emp = Employee(
            company_id=company.id,
            department_id=department.id,
            manager_id=manager.id if manager is not None else None,
            employee_no=no,
        )
        session.add(emp)
        session.flush()
        return emp

    claimant = _employee("E-claimant")
    manager = _employee("E-manager")
    claimant.manager_id = manager.id  # 主管是报销人的上级（在 team 范围内）
    outsider = _employee("E-outsider")
    finance_operator = _employee("E-finop")
    finance_lead = _employee("E-finlead")

    _grant(session, claimant, "employee")
    _grant(session, manager, "manager")
    _grant(session, outsider, "employee")
    _grant(session, finance_operator, "finance_operator")
    _grant(session, finance_lead, "finance_lead")

    category = ExpenseCategory(company_id=company.id, code="CAT-TRV", name="差旅")
    cost_center = CostCenter(company_id=company.id, code="CC-01", name="总部")
    session.add_all([category, cost_center])
    session.commit()

    return World(
        company=company,
        claimant=claimant,
        manager=manager,
        outsider=outsider,
        finance_operator=finance_operator,
        finance_lead=finance_lead,
        category=category,
        cost_center=cost_center,
    )


# --- 造数据的组合辅助 ---------------------------------------------------------


def _draft_with_line(session: Session, world: World, *, amount: Decimal = Decimal("100.10")) -> ExpenseClaim:
    """创建一张带一条明细的草稿报销单。"""
    principal = world.principal(world.claimant)
    claim = create_claim(session, principal)
    add_line(
        session,
        principal,
        claim.id,
        expense_date=date(2026, 10, 1),
        category_id=world.category.id,
        description="出差打车",
        amount=amount,
        cost_center_id=world.cost_center.id,
    )
    return claim


def _submitted(session: Session, world: World) -> ExpenseClaim:
    claim = _draft_with_line(session, world)
    submit(session, world.principal(world.claimant), claim.id)
    return claim


def _approved(session: Session, world: World) -> ExpenseClaim:
    claim = _submitted(session, world)
    approve(session, world.principal(world.manager), claim.id)
    return claim


def _finance_reviewed(session: Session, world: World) -> ExpenseClaim:
    claim = _approved(session, world)
    finance_review(session, world.principal(world.finance_operator), claim.id)
    return claim


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


# --- 1. 端到端快乐路径 --------------------------------------------------------


def test_happy_path_end_to_end(session: Session, world: World) -> None:
    """创建→明细→提交→审批→财务复核→付款，终态 paid 且金额精确。"""
    claimant = world.principal(world.claimant)
    claim = create_claim(session, claimant)
    assert claim.status == "draft"
    assert claim.total_amount == Decimal("0.00")
    assert claim.claim_no.startswith("EC-")
    assert len(claim.claim_no) == len("EC-") + 8

    add_line(
        session,
        claimant,
        claim.id,
        expense_date=date(2026, 10, 1),
        category_id=world.category.id,
        description="高铁票",
        amount=Decimal("100.10"),
        tax_amount=Decimal("3.00"),
        cost_center_id=world.cost_center.id,
    )
    add_line(
        session,
        claimant,
        claim.id,
        expense_date=date(2026, 10, 2),
        category_id=world.category.id,
        description="打车",
        amount=Decimal("200.20"),
    )
    # 精确求和：100.10 + 200.20 == 300.30（走 money 模块，非浮点）。
    assert claim.total_amount == Decimal("300.30")
    assert str(claim.total_amount) == "300.30"

    submit(session, claimant, claim.id)
    assert claim.status == "submitted"
    assert claim.submitted_at is not None

    approve(session, world.principal(world.manager), claim.id)
    assert claim.status == "approved"

    finance_review(session, world.principal(world.finance_operator), claim.id)
    assert claim.status == "finance_reviewed"

    payment = record_payment(
        session,
        world.principal(world.finance_lead),
        claim.id,
        amount=Decimal("300.30"),
        payment_ref="PAY-0001",
    )
    assert claim.status == "paid"
    assert payment.status == "paid"
    assert payment.paid_at is not None
    assert payment.currency == claim.currency == "CNY"
    assert payment.amount == Decimal("300.30")

    # 数据库往返：刷新后 total_amount 仍是精确的 Decimal('300.30')。
    session.refresh(claim)
    assert isinstance(claim.total_amount, Decimal)
    assert claim.total_amount == Decimal("300.30")
    assert str(claim.total_amount) == "300.30"

    # reimbursement_payments 有且仅有一行。
    count = session.scalar(
        select(func.count())
        .select_from(ReimbursementPayment)
        .where(ReimbursementPayment.claim_id == claim.id)
    )
    assert count == 1


# --- 2. 越权 ------------------------------------------------------------------


def test_cross_employee_actions_denied(session: Session, world: World) -> None:
    """另一名员工 add_line/submit/withdraw 均被拒，状态与 total 不变且有拒绝审计。"""
    claim = _draft_with_line(session, world, amount=Decimal("50.00"))
    status_before = claim.status
    total_before = claim.total_amount
    deny_before = len(_deny_events(session, claim))
    outsider = world.principal(world.outsider)

    # add_line 越权
    with pytest.raises(AuthorizationDeniedError) as add_err:
        add_line(
            session,
            outsider,
            claim.id,
            expense_date=date(2026, 10, 3),
            category_id=world.category.id,
            description="越权写入",
            amount=Decimal("999.00"),
        )
    assert add_err.value.attempted_action == "add_line"

    # submit 越权
    with pytest.raises(AuthorizationDeniedError) as submit_err:
        submit(session, outsider, claim.id)
    assert submit_err.value.attempted_action == "submit"

    # withdraw 越权（即便当前 draft 下 withdraw 本身也非法，越权判定先行）
    with pytest.raises(AuthorizationDeniedError) as withdraw_err:
        withdraw(session, outsider, claim.id)
    assert withdraw_err.value.attempted_action == "withdraw"

    # 业务状态与总额不变
    assert claim.status == status_before
    assert claim.total_amount == total_before
    # 越权没有写入任何明细
    line_count = session.scalar(
        select(func.count())
        .select_from(ExpenseLine)
        .where(ExpenseLine.claim_id == claim.id)
    )
    assert line_count == 1
    # 恰好新增三条拒绝审计
    deny_after = _deny_events(session, claim)
    assert len(deny_after) - deny_before == 3
    assert all(e.actor_id == world.outsider.id for e in deny_after[deny_before:])
    assert {e.details["attempted_action"] for e in deny_after[deny_before:]} == {
        "add_line",
        "submit",
        "withdraw",
    }


# --- 3. 自审 ------------------------------------------------------------------


def test_manager_cannot_approve_own_claim(session: Session, world: World) -> None:
    """拥有 approve 权限的主管批自己的报销被拒，状态不变且有拒绝审计。"""
    manager = world.principal(world.manager)
    claim = create_claim(session, manager)
    add_line(
        session,
        manager,
        claim.id,
        expense_date=date(2026, 10, 1),
        category_id=world.category.id,
        description="主管自己的差旅",
        amount=Decimal("80.00"),
    )
    submit(session, manager, claim.id)
    assert claim.status == "submitted"

    with pytest.raises(AuthorizationDeniedError) as err:
        approve(session, manager, claim.id)
    assert err.value.reason == "cannot_approve"

    assert claim.status == "submitted"  # 状态不变
    denials = _deny_events(session, claim)
    assert len(denials) == 1
    assert denials[0].actor_id == world.manager.id
    assert denials[0].details["attempted_action"] == "approve"


# --- 4. 无审批权限 ------------------------------------------------------------


def test_employee_without_approve_permission_denied(
    session: Session, world: World
) -> None:
    """无 approve 权限的普通员工调 approve 被拒。"""
    claim = _submitted(session, world)
    outsider = world.principal(world.outsider)

    with pytest.raises(AuthorizationDeniedError) as err:
        approve(session, outsider, claim.id)
    assert err.value.reason == "cannot_approve"

    assert claim.status == "submitted"
    denials = _deny_events(session, claim)
    assert len(denials) == 1
    assert denials[0].details["attempted_action"] == "approve"


# --- 5. 财务复核权限 ----------------------------------------------------------


def test_finance_review_requires_permission(session: Session, world: World) -> None:
    """主管调 finance_review 被拒；财务经办可调。"""
    claim = _approved(session, world)
    manager = world.principal(world.manager)

    with pytest.raises(AuthorizationDeniedError) as err:
        finance_review(session, manager, claim.id)
    assert err.value.reason == "missing_permission"
    assert claim.status == "approved"  # 状态不变

    finance_review(session, world.principal(world.finance_operator), claim.id)
    assert claim.status == "finance_reviewed"


# --- 6. 付款权限 --------------------------------------------------------------


def test_payment_requires_permission(session: Session, world: World) -> None:
    """财务经办调 record_payment 被拒；财务主管可调。"""
    claim = _finance_reviewed(session, world)
    finop = world.principal(world.finance_operator)

    with pytest.raises(AuthorizationDeniedError) as err:
        record_payment(session, finop, claim.id, amount=Decimal("100.10"))
    assert err.value.reason == "missing_permission"
    assert claim.status == "finance_reviewed"

    # 未产生付款记录
    assert (
        session.scalar(
            select(func.count())
            .select_from(ReimbursementPayment)
            .where(ReimbursementPayment.claim_id == claim.id)
        )
        == 0
    )

    record_payment(
        session, world.principal(world.finance_lead), claim.id, amount=Decimal("100.10")
    )
    assert claim.status == "paid"


# --- 7. 非法迁移 --------------------------------------------------------------


def test_invalid_transition_after_paid(session: Session, world: World) -> None:
    """对已 paid 的 claim 再调 submit → InvalidTransitionError，状态不变且有拒绝审计。"""
    claimant = world.principal(world.claimant)
    claim = _finance_reviewed(session, world)
    record_payment(
        session, world.principal(world.finance_lead), claim.id, amount=Decimal("100.10")
    )
    assert claim.status == "paid"

    with pytest.raises(InvalidTransitionError) as err:
        submit(session, claimant, claim.id)
    assert err.value.status == "paid"
    assert err.value.action == "submit"

    assert claim.status == "paid"  # 状态不变
    denials = _deny_events(session, claim)
    assert len(denials) == 1
    assert denials[0].details == {
        "attempted_action": "submit",
        "reason": "invalid_transition",
    }


# --- 8. 空明细提交 ------------------------------------------------------------


def test_submit_empty_claim_rejected(session: Session, world: World) -> None:
    """空明细提交被拒且状态不变，写一条拒绝审计。"""
    claimant = world.principal(world.claimant)
    claim = create_claim(session, claimant)
    assert claim.total_amount == Decimal("0.00")

    with pytest.raises(ValidationError) as err:
        submit(session, claimant, claim.id)
    assert err.value.reason == "empty_claim"

    assert claim.status == "draft"
    assert claim.submitted_at is None
    denials = _deny_events(session, claim)
    assert len(denials) == 1
    assert denials[0].details["reason"] == "empty_claim"


# --- 9. 审计计数 --------------------------------------------------------------


def test_audit_events_per_successful_transition(
    session: Session, world: World
) -> None:
    """快乐路径每个成功迁移恰好写一条事件，action 与 actor_id 正确。"""
    claimant = world.principal(world.claimant)
    claim = create_claim(session, claimant, request_id="req-create")
    add_line(
        session,
        claimant,
        claim.id,
        expense_date=date(2026, 10, 1),
        category_id=world.category.id,
        description="审计计数",
        amount=Decimal("100.10"),
        request_id="req-line",
    )
    submit(session, claimant, claim.id, request_id="req-submit")
    approve(session, world.principal(world.manager), claim.id, request_id="req-approve")
    finance_review(
        session, world.principal(world.finance_operator), claim.id, request_id="req-fin"
    )
    record_payment(
        session,
        world.principal(world.finance_lead),
        claim.id,
        amount=Decimal("100.10"),
        request_id="req-pay",
    )

    events = _events_for(session, claim)
    success = [e for e in events if e.action != "expense_claim.deny"]

    expected = [
        ("expense_claim.create", world.claimant.id, "req-create"),
        ("expense_claim.add_line", world.claimant.id, "req-line"),
        ("expense_claim.submit", world.claimant.id, "req-submit"),
        ("expense_claim.approve", world.manager.id, "req-approve"),
        ("expense_claim.finance_review", world.finance_operator.id, "req-fin"),
        ("expense_claim.record_payment", world.finance_lead.id, "req-pay"),
    ]
    assert [(e.action, e.actor_id, e.request_id) for e in success] == expected
    # 每个成功事件各一条，且都带 from_status/to_status。
    assert len(success) == len(expected)
    for event in success:
        assert event.resource_type == "expense_claim"
        assert event.resource_id == str(claim.id)
        assert "from_status" in event.details and "to_status" in event.details
    # 快乐路径没有拒绝事件。
    assert _deny_events(session, claim) == []


# --- 10. float 金额 -----------------------------------------------------------


def test_float_amount_rejected(session: Session, world: World) -> None:
    """传 float 金额被 money 模块以 TypeError 拒绝，且不产生部分写入。"""
    claimant = world.principal(world.claimant)
    claim = create_claim(session, claimant)

    with pytest.raises(TypeError):
        add_line(
            session,
            claimant,
            claim.id,
            expense_date=date(2026, 10, 1),
            category_id=world.category.id,
            description="float 金额",
            amount=1.5,  # type: ignore[arg-type]
        )

    # 不产生部分写入：没有明细，总额仍为 0.00。
    line_count = session.scalar(
        select(func.count())
        .select_from(ExpenseLine)
        .where(ExpenseLine.claim_id == claim.id)
    )
    assert line_count == 0
    assert claim.total_amount == Decimal("0.00")

    # float 税额同样被拒。
    with pytest.raises(TypeError):
        add_line(
            session,
            claimant,
            claim.id,
            expense_date=date(2026, 10, 1),
            category_id=world.category.id,
            description="float 税额",
            amount=Decimal("10.00"),
            tax_amount=0.1,  # type: ignore[arg-type]
        )
    assert (
        session.scalar(
            select(func.count())
            .select_from(ExpenseLine)
            .where(ExpenseLine.claim_id == claim.id)
        )
        == 0
    )
    assert claim.total_amount == Decimal("0.00")


# --- 额外：撤回重提、退回补件、拒绝 --------------------------------------------


def test_withdraw_then_resubmit(session: Session, world: World) -> None:
    """submitted → withdrawn → submitted 由本人完成，各写一条成功审计。"""
    claimant = world.principal(world.claimant)
    claim = _submitted(session, world)

    withdraw(session, claimant, claim.id)
    assert claim.status == "withdrawn"
    submit(session, claimant, claim.id)
    assert claim.status == "submitted"

    actions = [e.action for e in _events_for(session, claim)]
    assert actions == [
        "expense_claim.create",
        "expense_claim.add_line",
        "expense_claim.submit",
        "expense_claim.withdraw",
        "expense_claim.submit",
    ]


def test_return_for_fix_and_reject(session: Session, world: World) -> None:
    """主管可退回补件（回到 draft）与拒绝（到 rejected）。"""
    manager = world.principal(world.manager)

    claim = _submitted(session, world)
    return_for_fix(session, manager, claim.id)
    assert claim.status == "draft"

    # 重新提交后拒绝。
    submit(session, world.principal(world.claimant), claim.id)
    reject(session, manager, claim.id)
    assert claim.status == "rejected"

    # 终态后主管再 reject 属非法迁移。
    with pytest.raises(InvalidTransitionError):
        reject(session, manager, claim.id)
