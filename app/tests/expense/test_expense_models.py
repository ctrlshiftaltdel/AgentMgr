"""报销域模型测试：金额固定精度、约束与级联，跑在专用测试库上。

覆盖任务 W1-6 第五步要求：

1. 写入 123.45 的 line 后读回**精确等于** ``Decimal('123.45')``（断言类型与值）；
2. 两条 0.10 与 0.20 的 line 求和为 ``Decimal('0.30')``；
3. 负数金额被 CHECK 拒绝；
4. ``claim_no`` 重复插入被拒；
5. 非法 ``status`` 被 CHECK 拒绝；
6. 五张表的金额列在数据库里确实是 ``NUMERIC(18, 2)``；
7. ``expense_lines`` 随报销单 ``ON DELETE CASCADE``。

基础数据（company/employee/category/cost_center/claim）在断言前先提交，使
``rollback`` 只撤销出错的语句，不会连带撤销上下文——否则失败会退化为外键错误，
掩盖真正被检验的约束。每个断言还核对错误信息里的约束名，证明拒绝来自目标
CHECK/UNIQUE，而非偶然的其它完整性错误。

session fixture 在结束时按依赖逆序清理，不留残余数据。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.expense.models import (
    CostCenter,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseLine,
    ReimbursementPayment,
)
from app.expense.money import sum_amounts

REPO_ROOT = Path(__file__).resolve().parents[3]

MONEY_TABLES = {
    "expense_claims": ("total_amount",),
    "expense_lines": ("amount", "tax_amount"),
    "reimbursement_payments": ("amount",),
}


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """Make sure the test database is migrated to head before these tests."""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """Delete every business row created by these tests (children first)."""
    for model in (
        ReimbursementPayment,
        ExpenseLine,
        ExpenseClaim,
        CostCenter,
        ExpenseCategory,
        Employee,
        Department,
        Company,
    ):
        session.execute(delete(model))


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


def _make_context(
    session: Session, *, suffix: str
) -> tuple[Employee, ExpenseCategory, CostCenter]:
    """Create and **commit** company/department/employee/category/cost_center."""
    company = Company(name=f"W1-6 {suffix}")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name=f"Dept {suffix}")
    session.add(department)
    session.flush()
    employee = Employee(
        company_id=company.id,
        department_id=department.id,
        employee_no=f"E-{suffix}",
    )
    category = ExpenseCategory(
        company_id=company.id, code=f"CAT-{suffix}", name=f"Category {suffix}"
    )
    cost_center = CostCenter(
        company_id=company.id, code=f"CC-{suffix}", name=f"CostCenter {suffix}"
    )
    session.add_all([employee, category, cost_center])
    session.commit()
    return employee, category, cost_center


def _make_claim(
    session: Session, employee: Employee, *, claim_no: str
) -> ExpenseClaim:
    """Create and **commit** a claim so later rollbacks keep it."""
    claim = ExpenseClaim(claim_no=claim_no, employee_id=employee.id)
    session.add(claim)
    session.commit()
    return claim


def test_money_columns_are_numeric_18_2(engine: Engine) -> None:
    """五张表的金额列在数据库里必须是 NUMERIC(18, 2)，绝无浮点类型。"""
    insp = inspect(engine)
    for table, columns in MONEY_TABLES.items():
        found = {col["name"]: col["type"] for col in insp.get_columns(table)}
        for column in columns:
            rendered = str(found[column]).upper()
            assert rendered == "NUMERIC(18, 2)", (table, column, rendered)
            assert "FLOAT" not in rendered and "DOUBLE" not in rendered


def test_line_amount_readback_is_exact_decimal(session: Session) -> None:
    """写入 123.45 后读回精确等于 Decimal('123.45')（类型与值）。"""
    employee, category, cost_center = _make_context(session, suffix="prec")
    claim = _make_claim(session, employee, claim_no="CLM-PREC-1")
    line = ExpenseLine(
        claim_id=claim.id,
        expense_date=date(2026, 10, 1),
        category_id=category.id,
        description="差旅",
        amount=Decimal("123.45"),
        cost_center_id=cost_center.id,
    )
    session.add(line)
    session.commit()

    # ORM 读回（commit 后对象过期，触发真实 DB 查询）。
    reloaded = session.scalar(select(ExpenseLine).where(ExpenseLine.id == line.id))
    assert reloaded is not None
    assert isinstance(reloaded.amount, Decimal)
    assert reloaded.amount == Decimal("123.45")
    assert str(reloaded.amount) == "123.45"

    # 原始 SQL 读回，确认落库值与类型。
    raw = session.execute(
        text("SELECT amount FROM expense_lines WHERE id = :i"), {"i": line.id}
    ).scalar()
    assert isinstance(raw, Decimal)
    assert raw == Decimal("123.45")


def test_sum_of_two_lines_is_exact_thirty(session: Session) -> None:
    """两条 0.10 与 0.20 的 line 求和为 Decimal('0.30')。"""
    employee, category, _ = _make_context(session, suffix="sum")
    claim = _make_claim(session, employee, claim_no="CLM-SUM-1")
    session.add_all(
        [
            ExpenseLine(
                claim_id=claim.id,
                expense_date=date(2026, 10, 2),
                category_id=category.id,
                description="A",
                amount=Decimal("0.10"),
            ),
            ExpenseLine(
                claim_id=claim.id,
                expense_date=date(2026, 10, 2),
                category_id=category.id,
                description="B",
                amount=Decimal("0.20"),
            ),
        ]
    )
    session.commit()

    amounts = list(
        session.scalars(
            select(ExpenseLine.amount).where(ExpenseLine.claim_id == claim.id)
        )
    )
    assert len(amounts) == 2
    assert all(isinstance(a, Decimal) for a in amounts)
    total = sum_amounts(amounts)
    assert total == Decimal("0.30")
    assert str(total) == "0.30"


def test_negative_amount_rejected(session: Session) -> None:
    """负数金额被对应 CHECK 拒绝（amount / tax_amount / total_amount）。"""
    employee, category, _ = _make_context(session, suffix="neg")
    claim = _make_claim(session, employee, claim_no="CLM-NEG-1")

    session.add(
        ExpenseLine(
            claim_id=claim.id,
            expense_date=date(2026, 10, 3),
            category_id=category.id,
            description="negative",
            amount=Decimal("-1.00"),
        )
    )
    with pytest.raises(IntegrityError) as amount_err:
        session.commit()
    assert "ck_expense_lines_amount_non_negative" in str(amount_err.value)
    session.rollback()

    session.add(
        ExpenseLine(
            claim_id=claim.id,
            expense_date=date(2026, 10, 3),
            category_id=category.id,
            description="neg tax",
            amount=Decimal("10.00"),
            tax_amount=Decimal("-0.01"),
        )
    )
    with pytest.raises(IntegrityError) as tax_err:
        session.commit()
    assert "ck_expense_lines_tax_amount_non_negative" in str(tax_err.value)
    session.rollback()

    session.add(
        ExpenseClaim(
            claim_no="CLM-NEG-2",
            employee_id=employee.id,
            total_amount=Decimal("-5.00"),
        )
    )
    with pytest.raises(IntegrityError) as total_err:
        session.commit()
    assert "ck_expense_claims_total_amount_non_negative" in str(total_err.value)
    session.rollback()


def test_duplicate_claim_no_rejected(session: Session) -> None:
    """claim_no 重复插入被唯一约束拒绝。"""
    employee, _, _ = _make_context(session, suffix="dup")
    _make_claim(session, employee, claim_no="CLM-DUP-1")

    session.add(ExpenseClaim(claim_no="CLM-DUP-1", employee_id=employee.id))
    with pytest.raises(IntegrityError) as excinfo:
        session.commit()
    assert "uq_expense_claims_claim_no" in str(excinfo.value)
    session.rollback()


def test_invalid_status_rejected(session: Session) -> None:
    """非法 status 被 CHECK 拒绝（claims 与 payments 都是）。"""
    employee, _, _ = _make_context(session, suffix="status")
    session.add(
        ExpenseClaim(
            claim_no="CLM-BADSTATUS", employee_id=employee.id, status="bogus"
        )
    )
    with pytest.raises(IntegrityError) as claim_err:
        session.commit()
    assert "ck_expense_claims_status_allowed" in str(claim_err.value)
    session.rollback()

    claim = _make_claim(session, employee, claim_no="CLM-OK")
    session.add(
        ReimbursementPayment(
            claim_id=claim.id, amount=Decimal("1.00"), status="bogus"
        )
    )
    with pytest.raises(IntegrityError) as pay_err:
        session.commit()
    assert "ck_reimbursement_payments_status_allowed" in str(pay_err.value)
    session.rollback()


def test_payment_amount_must_be_positive(session: Session) -> None:
    """付款金额必须 > 0，0 被拒。"""
    employee, _, _ = _make_context(session, suffix="pay")
    claim = _make_claim(session, employee, claim_no="CLM-PAY-1")
    session.add(ReimbursementPayment(claim_id=claim.id, amount=Decimal("0.00")))
    with pytest.raises(IntegrityError) as excinfo:
        session.commit()
    assert "ck_reimbursement_payments_amount_positive" in str(excinfo.value)
    session.rollback()


def test_category_company_code_unique(session: Session) -> None:
    """(company_id, code) 唯一约束生效。"""
    employee, category, _ = _make_context(session, suffix="catuniq")
    session.add(
        ExpenseCategory(
            company_id=employee.company_id, code=category.code, name="dup"
        )
    )
    with pytest.raises(IntegrityError) as excinfo:
        session.commit()
    assert "uq_expense_categories_company_code" in str(excinfo.value)
    session.rollback()


def test_claim_delete_cascades_lines(session: Session) -> None:
    """删除报销单级联删除其明细。"""
    employee, category, _ = _make_context(session, suffix="cascade")
    claim = _make_claim(session, employee, claim_no="CLM-CASCADE-1")
    session.add(
        ExpenseLine(
            claim_id=claim.id,
            expense_date=date(2026, 10, 4),
            category_id=category.id,
            description="cascade",
            amount=Decimal("9.99"),
        )
    )
    session.commit()

    session.execute(delete(ExpenseClaim).where(ExpenseClaim.id == claim.id))
    session.commit()

    remaining = session.scalar(
        select(ExpenseLine).where(ExpenseLine.claim_id == claim.id)
    )
    assert remaining is None
