"""W1-5 审计域测试：只增不改 + 人员关联 + 模块无修改/删除入口。

覆盖任务固定的验收点（对齐 ``templates/PLAN.md`` §8/§9/§12 与
``docs/research/W0-5-security-compliance.md`` §7.3）：

1. ``record_event`` 写入成功，可按 ``(resource_type, resource_id)`` 与
   ``request_id`` 查到；
2. 数据库层拒绝 ``UPDATE`` 与 ``DELETE``（触发器生效，不是应用层自觉）；
3. ``app.audit.service`` 不暴露任何修改/删除审计的公开成员；
4. 删除员工时 ``actor_id`` 被置 NULL 而非级联删除审计行。

实现要点：PostgreSQL 中一条语句报错会中止整个事务，因此触发 UPDATE/DELETE 报错
的断言一律用 ``session.begin_nested()``（SAVEPOINT）包裹，否则后续断言会因
「当前事务已中止」失败。测试数据清理**只靠事务回滚**——绝不用 ``DELETE`` 清
``audit_events``（会被触发器拦下）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.audit import service as audit_service
from app.audit.models import AuditEvent
from app.db.models.organization import Company, Employee

REPO_ROOT = Path(__file__).resolve().parents[3]

# 任何名字命中这些词根的公开成员都视为「修改/删除审计」入口。
_FORBIDDEN_MEMBER = re.compile(
    r"(?i)(update|delete|remove|drop|modify|mutate|edit|purge|truncate|wipe)"
)


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head（含 0004 audit_events）。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """每个测试一个外层事务；结束一律回滚，不留任何审计行。

    审计表只增不改，无法用 ``DELETE`` 清理，因此清理必须靠回滚。
    """
    with Session(engine) as sess:
        sess.begin()
        try:
            yield sess
        finally:
            sess.rollback()


def _make_employee(session: Session, *, suffix: str) -> Employee:
    company = Company(name=f"W1-5 {suffix}")
    session.add(company)
    session.flush()
    employee = Employee(company_id=company.id, employee_no=f"E-{suffix}")
    session.add(employee)
    session.flush()
    return employee


# --------------------------------------------------------------------------- #
# 1. 追加写入可查
# --------------------------------------------------------------------------- #
def test_record_event_inserts_and_is_queryable(session: Session) -> None:
    employee = _make_employee(session, suffix="insert")

    event = audit_service.record_event(
        session,
        actor_id=employee.id,
        action="expense.submit",
        resource_type="expense_claim",
        resource_id="CLAIM-1001",
        request_id="req-insert-1",
        details={"from_status": "draft", "to_status": "submitted"},
    )
    session.flush()

    assert event.id is not None
    assert event.created_at is not None

    by_resource = session.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_type == "expense_claim",
            AuditEvent.resource_id == "CLAIM-1001",
        )
    )
    assert by_resource is not None
    assert by_resource.action == "expense.submit"
    assert by_resource.actor_id == employee.id

    by_request = session.scalar(
        select(AuditEvent).where(AuditEvent.request_id == "req-insert-1")
    )
    assert by_request is not None
    assert by_request.id == event.id
    # details 原样保存为结构化 JSONB。
    assert by_request.details == {"from_status": "draft", "to_status": "submitted"}


# --------------------------------------------------------------------------- #
# 2. UPDATE / DELETE 被数据库拒绝
# --------------------------------------------------------------------------- #
def test_update_is_rejected_by_database(session: Session) -> None:
    event = audit_service.record_event(
        session,
        actor_id=None,
        action="permission.grant",
        resource_type="employee_role",
        resource_id="42",
        request_id="req-update-1",
    )
    session.flush()
    event_id = event.id

    # SAVEPOINT 包裹：报错只回滚到保存点，外层事务仍可用。
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(
                update(AuditEvent)
                .where(AuditEvent.id == event_id)
                .values(action="tampered")
            )

    session.expire_all()
    still = session.get(AuditEvent, event_id)
    assert still is not None
    assert still.action == "permission.grant"


def test_delete_is_rejected_by_database(session: Session) -> None:
    event = audit_service.record_event(
        session,
        actor_id=None,
        action="export.download",
        resource_type="document",
        resource_id="DOC-7",
        request_id="req-delete-1",
    )
    session.flush()
    event_id = event.id

    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))

    session.expire_all()
    still = session.get(AuditEvent, event_id)
    assert still is not None
    assert still.request_id == "req-delete-1"


# --------------------------------------------------------------------------- #
# 3. 服务模块不暴露修改/删除入口
# --------------------------------------------------------------------------- #
def test_service_exposes_no_mutation_members() -> None:
    public_names = {name for name in dir(audit_service) if not name.startswith("_")}

    offenders = {name for name in public_names if _FORBIDDEN_MEMBER.search(name)}
    assert offenders == set(), f"service exposes mutation-looking members: {offenders}"

    # 只导出追加写入能力。
    assert audit_service.__all__ == ["record_event"]
    assert "record_event" in public_names


# --------------------------------------------------------------------------- #
# 4. 删除员工 -> actor_id 置 NULL（不级联删除审计）
# --------------------------------------------------------------------------- #
def test_employee_delete_sets_actor_id_null_and_keeps_event(session: Session) -> None:
    employee = _make_employee(session, suffix="setnull")

    event = audit_service.record_event(
        session,
        actor_id=employee.id,
        action="expense.approve",
        resource_type="expense_claim",
        resource_id="CLAIM-2002",
        request_id="req-setnull-1",
    )
    session.flush()
    event_id = event.id
    assert event.actor_id == employee.id

    session.execute(delete(Employee).where(Employee.id == employee.id))
    session.flush()
    session.expire_all()

    # 审计行仍在（未被级联删除），且 actor_id 被数据库置为 NULL。
    survivor = session.get(AuditEvent, event_id)
    assert survivor is not None
    assert survivor.actor_id is None
    assert survivor.action == "expense.approve"
