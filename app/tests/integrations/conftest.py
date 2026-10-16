"""发票查验（W1-20）测试夹具。

全部测试跑在**真实** PostgreSQL 测试库 ``ajaimgr_test`` 上（与既有套件一致），
但**绝不调用任何真实渠道**：腾讯云适配器的网络出口一律注入桩传输函数，服务层用
本地桩 provider。

``invoice_verifications`` 不在 ``app/tests/conftest.py`` 的 ``BUSINESS_TABLES`` 里
（该文件不在本任务允许改动的路径内），因此本目录的 ``session`` 夹具在结束时清空
该表与本次创建的组织数据，保证测试可重复、顺序无关。共享辅助放在
``app.tests.integrations.invoice_fixtures``，避免同一模块被以两个名字重复导入。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.documents.models import SourceFile
from app.identity.models import EmployeeRole, Role
from app.identity.seed import seed_permission_catalog
from app.tests.integrations.invoice_fixtures import World, alembic_config, cleanup


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head（含 0010）再运行这些测试。"""
    command.upgrade(alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            seed_permission_catalog(sess)
            sess.commit()
            yield sess
        finally:
            sess.rollback()
            cleanup(sess)
            sess.commit()


def _grant(session: Session, employee: Employee, role_name: str) -> None:
    role = session.scalar(select(Role).where(Role.name == role_name))
    assert role is not None, f"role {role_name!r} not seeded"
    session.add(EmployeeRole(employee_id=employee.id, role_id=role.id))


@pytest.fixture()
def world(session: Session) -> World:
    """造出并提交一套组织数据（提交后服务写入可独立回滚/清理）。"""
    company = Company(name="W1-20 发票查验")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name="财务部")
    session.add(department)
    session.flush()

    def _employee(no: str, role_name: str) -> Employee:
        emp = Employee(
            company_id=company.id,
            department_id=department.id,
            employee_no=no,
        )
        session.add(emp)
        session.flush()
        _grant(session, emp, role_name)
        return emp

    claimant = _employee("E-W1-20-claimant", "employee")
    finance_operator = _employee("E-W1-20-finop", "finance_operator")
    outsider = _employee("E-W1-20-outsider", "employee")

    def _source_file(owner: Employee, name: str) -> SourceFile:
        sf = SourceFile(
            owner_id=owner.id,
            storage_key="aa/bb/" + "0" * 64,
            sha256="0" * 64,
            mime_type="application/pdf",
            size_bytes=1024,
            original_name=name,
            scan_status="clean",
        )
        session.add(sf)
        session.flush()
        return sf

    source_file_a = _source_file(claimant, "invoice-a.pdf")
    source_file_b = _source_file(claimant, "invoice-b.pdf")
    session.commit()

    return World(
        company=company,
        department=department,
        claimant=claimant,
        finance_operator=finance_operator,
        outsider=outsider,
        source_file_a=source_file_a,
        source_file_b=source_file_b,
    )
