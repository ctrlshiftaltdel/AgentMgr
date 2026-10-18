"""Issue #21 渠道消息入站与路由测试（跑在专用测试库 ``ajaimgr_test`` 上）。

覆盖任务验收项 3–9：

1. ``accepted``：新建 ``IntakeJob`` + 写 ``inbound_messages``，``profile_name`` 由
   ``profile_name_for`` 算出且**不产生任何** ``~/.hermes`` 副作用；
2. ``duplicate``：同一 ``(channel_type, external_message_id)`` 重复投递只产生一个作业，
   第二次返回相同 ``intake_job_id``；并**真实**走一遍唯一约束冲突路径（快路径被跳过，
   由 ``IntegrityError`` → 回滚 → 回查兜底），证明并发下不会重复建作业、也不会 500；
3. 六类身份拒绝（unbound / revoked / unverified / employee_inactive /
   company_mismatch / ambiguous）：``rejected`` 且**不建** ``IntakeJob``、
   **不写** ``inbound_messages``；
4. 未知渠道类型 / 未知输入类型 / 缺失消息 ID / 缺失发送者：写库**之前**拒绝，不落库；
5. 拒绝不成为幂等锚点：同一消息在员工完成绑定后仍可被正常受理（不会永久丢单）；
6. 路由冲突（同一消息 ID 来自另一个发送者）拒绝；
7. 审计不含渠道身份明文与消息正文，只含加盐哈希；
8. ``IngestResult`` 的字符串表示不含内部 ``reason``；
9. 适配器以 ``Protocol`` 定义，桩适配器即可驱动全流程（无真实 SDK、无网络调用）。

约定：所有断言都用**增量**（前后计数差）而不是绝对行数，因为 ``audit_events`` 只增
不改、跨测试累积；每个测试自造 company/employee/binding，teardown 逆序清理。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Text as SqlText
from sqlalchemy import cast, create_engine, delete, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.intake.inbound as inbound_module
from app.audit.models import AuditEvent
from app.db.models.organization import Company, Department, Employee
from app.identity.channel_routing import IDENTITY_HASH_SALT_ENV, _hash_channel_identity
from app.identity.models import EmployeeChannelBinding
from app.intake.inbound import (
    ACCEPTED_AUDIT_ACTION,
    INGEST_ACCEPTED,
    INGEST_DUPLICATE,
    INGEST_REJECTED,
    SUPPORTED_CHANNEL_TYPES,
    SUPPORTED_INPUT_TYPES,
    InboundChannelAdapter,
    InboundChannelMessage,
    IngestResult,
    ingest_inbound_message,
)
from app.intake.models import InboundMessage, IntakeJob
from app.runtime.profile_provisioning import profile_name_for

REPO_ROOT = Path(__file__).resolve().parents[3]

_UNIQUE_CONSTRAINT = "uq_employee_channel_bindings_channel_type"
_ANCHOR_CONSTRAINT = "uq_inbound_messages_channel_external_message"


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head（含 0012 inbound_messages）再运行这些测试。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@dataclass
class Org:
    """一个公司 + 一个其它公司，供公司不匹配场景使用。"""

    company: Company
    other_company: Company
    active_employee: Employee
    inactive_employee: Employee
    foreign_employee: Employee  # 属于 other_company


@pytest.fixture()
def org(engine: Engine) -> Iterator[Org]:
    with Session(engine, expire_on_commit=False) as session:
        company = Company(name="W1-21 公司")
        other = Company(name="W1-21 他司")
        session.add_all([company, other])
        session.flush()
        department = Department(company_id=company.id, name="W1-21 部")
        session.add(department)
        session.flush()

        active = Employee(
            company_id=company.id,
            department_id=department.id,
            employee_no="W1-21-active",
            status="active",
        )
        inactive = Employee(
            company_id=company.id,
            department_id=department.id,
            employee_no="W1-21-inactive",
            status="inactive",
        )
        foreign = Employee(
            company_id=other.id,
            employee_no="W1-21-foreign",
            status="active",
        )
        session.add_all([active, inactive, foreign])
        session.commit()

        built = Org(
            company=company,
            other_company=other,
            active_employee=active,
            inactive_employee=inactive,
            foreign_employee=foreign,
        )
    try:
        yield built
    finally:
        with Session(engine) as session:
            session.execute(delete(InboundMessage))
            session.execute(delete(IntakeJob))
            session.execute(delete(EmployeeChannelBinding))
            session.execute(delete(Employee))
            session.execute(delete(Department))
            session.execute(delete(Company))
            session.commit()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            yield sess
        finally:
            sess.rollback()


def _add_binding(
    engine: Engine,
    *,
    employee_id: int,
    external_user_id: str,
    channel_type: str = "weixin",
    status: str = "active",
    verified: bool = True,
) -> int:
    with Session(engine) as session:
        binding = EmployeeChannelBinding(
            employee_id=employee_id,
            channel_type=channel_type,
            external_user_id=external_user_id,
            status=status,
            verified_at=datetime.now(timezone.utc) if verified else None,
        )
        session.add(binding)
        session.commit()
        return binding.id


@contextmanager
def _session_with_duplicate_binding(
    engine: Engine,
    *,
    employee_id: int,
    channel_type: str,
    external_user_id: str,
) -> Iterator[Session]:
    """在同一事务内临时移除唯一约束并插入两条同键绑定，退出时回滚。

    与 W1-19/W1-21 渠道路由测试同一手法：PostgreSQL 的事务性 DDL 让「歧义」由
    **真实** SQL 构造，退出后库结构与数据均复原。
    """
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text(
                    "ALTER TABLE employee_channel_bindings "
                    f"DROP CONSTRAINT {_UNIQUE_CONSTRAINT}"
                )
            )
            insert = text(
                "INSERT INTO employee_channel_bindings "
                "(employee_id, channel_type, external_user_id, status, verified_at) "
                "VALUES (:e, :c, :x, 'active', now())"
            )
            conn.execute(
                insert, {"e": employee_id, "c": channel_type, "x": external_user_id}
            )
            conn.execute(
                insert, {"e": employee_id, "c": channel_type, "x": external_user_id}
            )
            with Session(bind=conn) as session:
                yield session
        finally:
            trans.rollback()


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _tally(session: Session) -> tuple[int, int]:
    """``(受理作业数, 入站锚点数)``——拒绝路径断言两者均无增量。"""
    return (_count(session, IntakeJob), _count(session, InboundMessage))


def _message(
    *,
    external_message_id: str = "msg-1",
    external_user_id: str = "wx-user-1",
    channel_type: str = "weixin",
    input_type: str = "text",
) -> InboundChannelMessage:
    return InboundChannelMessage(
        channel_type=channel_type,
        external_message_id=external_message_id,
        external_user_id=external_user_id,
        input_type=input_type,
    )


def _hermes_snapshot() -> tuple[bool, tuple[str, ...]]:
    """``~/.hermes`` 的存在性 + 文件清单快照（用于断言无文件系统副作用）。"""
    root = Path.home() / ".hermes"
    if not root.exists():
        return (False, ())
    return (True, tuple(sorted(str(p.relative_to(root)) for p in root.rglob("*"))))


def _assert_rejected(
    session: Session,
    result: IngestResult,
    *,
    reason: str,
    before: tuple[int, int],
) -> None:
    """拒绝结果的全部契约：状态、空作业/空 Profile、内部原因、零落库。"""
    assert result.status == INGEST_REJECTED
    assert result.intake_job_id is None
    assert result.profile_name is None
    assert result.reason == reason
    assert _tally(session) == before


# --- 1. accepted ---------------------------------------------------------------


def test_accepted_creates_job_and_anchor(
    session: Session, engine: Engine, org: Org
) -> None:
    """首次受理：建作业 + 写锚点 + 返回由 employee 记录算出的 Profile 名。"""
    _add_binding(engine, employee_id=org.active_employee.id, external_user_id="wx-in-1")

    before_hermes = _hermes_snapshot()
    result = ingest_inbound_message(
        session, _message(external_message_id="msg-ok-1", external_user_id="wx-in-1")
    )
    after_hermes = _hermes_snapshot()

    assert result.status == INGEST_ACCEPTED
    assert result.intake_job_id is not None
    assert result.profile_name == profile_name_for(org.active_employee)
    assert result.profile_name is not None and result.profile_name.startswith("emp-")
    # 只算名字：不创建目录、不写 ~/.hermes、不启动进程。
    assert after_hermes == before_hermes

    job = session.get(IntakeJob, result.intake_job_id)
    assert job is not None
    assert job.submitted_by == org.active_employee.id
    assert job.channel_type == "weixin"
    assert job.input_type == "text"
    assert job.status == "received"
    assert job.received_at is not None

    anchor = session.scalar(
        select(InboundMessage).where(
            InboundMessage.channel_type == "weixin",
            InboundMessage.external_message_id == "msg-ok-1",
        )
    )
    assert anchor is not None
    assert anchor.intake_job_id == result.intake_job_id
    assert anchor.external_user_id == "wx-in-1"
    assert anchor.status == "accepted"
    assert anchor.received_at is not None


@pytest.mark.parametrize("input_type", SUPPORTED_INPUT_TYPES)
def test_every_supported_input_type_is_accepted(
    session: Session, engine: Engine, org: Org, input_type: str
) -> None:
    """服务层输入类型白名单与 ``intake_jobs.input_type`` 的 CHECK 逐字一致。"""
    sender = f"wx-input-{input_type}"
    _add_binding(engine, employee_id=org.active_employee.id, external_user_id=sender)

    result = ingest_inbound_message(
        session,
        _message(
            external_message_id=f"msg-input-{input_type}",
            external_user_id=sender,
            input_type=input_type,
        ),
    )
    assert result.status == INGEST_ACCEPTED
    job = session.get(IntakeJob, result.intake_job_id)
    assert job is not None and job.input_type == input_type


# --- 2. duplicate --------------------------------------------------------------


def test_repeat_delivery_is_duplicate_with_same_job(
    session: Session, engine: Engine, org: Org
) -> None:
    """重复投递只产生一个作业，第二次返回相同 ``intake_job_id``。"""
    _add_binding(engine, employee_id=org.active_employee.id, external_user_id="wx-dup-1")
    message = _message(external_message_id="msg-dup-1", external_user_id="wx-dup-1")

    before = _tally(session)
    first = ingest_inbound_message(session, message)
    second = ingest_inbound_message(session, message)

    assert first.status == INGEST_ACCEPTED
    assert second.status == INGEST_DUPLICATE
    assert second.intake_job_id == first.intake_job_id
    assert second.profile_name == first.profile_name
    assert second.reason is None
    assert _tally(session) == (before[0] + 1, before[1] + 1)


def test_concurrent_double_delivery_falls_back_to_unique_constraint(
    session: Session, engine: Engine, org: Org, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快路径失效（并发窗口）时由唯一约束兜底：回滚本事务、回查并返回 duplicate。"""
    _add_binding(
        engine, employee_id=org.active_employee.id, external_user_id="wx-race-1"
    )
    message = _message(external_message_id="msg-race-1", external_user_id="wx-race-1")

    # 另一个事务抢先提交了同一条消息的受理结果。
    with Session(engine) as concurrent:
        winning_job = IntakeJob(
            submitted_by=org.active_employee.id,
            channel_type="weixin",
            input_type="text",
        )
        concurrent.add(winning_job)
        concurrent.flush()
        concurrent.add(
            InboundMessage(
                channel_type="weixin",
                external_message_id="msg-race-1",
                external_user_id="wx-race-1",
                intake_job_id=winning_job.id,
                status="accepted",
            )
        )
        concurrent.commit()
        winning_job_id = winning_job.id

    real_find = inbound_module._find_inbound
    calls = {"n": 0}

    def flaky_find(
        sess: Session, *, channel_type: str, external_message_id: str
    ) -> InboundMessage | None:
        """第一次返回 None（模拟并发窗口），之后走真实查询。"""
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find(
            sess, channel_type=channel_type, external_message_id=external_message_id
        )

    monkeypatch.setattr(inbound_module, "_find_inbound", flaky_find)

    before = _tally(session)
    result = ingest_inbound_message(session, message)

    assert result.status == INGEST_DUPLICATE
    assert result.intake_job_id == winning_job_id
    assert calls["n"] == 2  # 快路径 + 冲突后回查
    # 本事务插入的作业已随回滚消失：只剩并发事务那一个作业/锚点。
    assert _tally(session) == before


def test_same_message_id_from_another_sender_is_a_route_conflict(
    session: Session, engine: Engine, org: Org
) -> None:
    """同一消息 ID 换了发送者 → 拒绝，不得在第二个身份下静默处理。"""
    _add_binding(engine, employee_id=org.active_employee.id, external_user_id="wx-owner")
    _add_binding(engine, employee_id=org.active_employee.id, external_user_id="wx-other")

    first = ingest_inbound_message(
        session, _message(external_message_id="msg-conflict", external_user_id="wx-owner")
    )
    assert first.status == INGEST_ACCEPTED

    before = _tally(session)
    second = ingest_inbound_message(
        session, _message(external_message_id="msg-conflict", external_user_id="wx-other")
    )
    _assert_rejected(session, second, reason="route_conflict", before=before)


# --- 3. 六类身份拒绝 ------------------------------------------------------------


def test_unbound_is_rejected_and_writes_nothing(session: Session, org: Org) -> None:
    before = _tally(session)
    result = ingest_inbound_message(
        session, _message(external_message_id="msg-unbound", external_user_id="wx-nobody")
    )
    _assert_rejected(session, result, reason="unbound", before=before)


def test_revoked_is_rejected_and_writes_nothing(
    session: Session, engine: Engine, org: Org
) -> None:
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        external_user_id="wx-revoked",
        status="revoked",
    )
    before = _tally(session)
    result = ingest_inbound_message(
        session, _message(external_message_id="msg-revoked", external_user_id="wx-revoked")
    )
    _assert_rejected(session, result, reason="revoked", before=before)


def test_unverified_is_rejected_and_writes_nothing(
    session: Session, engine: Engine, org: Org
) -> None:
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        external_user_id="wx-unverified",
        verified=False,
    )
    before = _tally(session)
    result = ingest_inbound_message(
        session,
        _message(external_message_id="msg-unverified", external_user_id="wx-unverified"),
    )
    _assert_rejected(session, result, reason="unverified", before=before)


def test_inactive_employee_is_rejected_and_writes_nothing(
    session: Session, engine: Engine, org: Org
) -> None:
    _add_binding(
        engine, employee_id=org.inactive_employee.id, external_user_id="wx-inactive"
    )
    before = _tally(session)
    result = ingest_inbound_message(
        session,
        _message(external_message_id="msg-inactive", external_user_id="wx-inactive"),
    )
    _assert_rejected(session, result, reason="employee_inactive", before=before)


def test_company_mismatch_is_rejected_and_writes_nothing(
    session: Session, engine: Engine, org: Org
) -> None:
    _add_binding(engine, employee_id=org.active_employee.id, external_user_id="wx-xcom")
    before = _tally(session)
    result = ingest_inbound_message(
        session,
        _message(external_message_id="msg-xcom", external_user_id="wx-xcom"),
        expected_company_id=org.other_company.id,
    )
    _assert_rejected(session, result, reason="company_mismatch", before=before)


def test_ambiguous_binding_is_rejected_and_writes_nothing(
    session: Session, engine: Engine, org: Org
) -> None:
    before = _tally(session)
    with _session_with_duplicate_binding(
        engine,
        employee_id=org.active_employee.id,
        channel_type="weixin",
        external_user_id="wx-ambiguous",
    ) as ambiguous_session:
        result = ingest_inbound_message(
            ambiguous_session,
            _message(external_message_id="msg-ambiguous", external_user_id="wx-ambiguous"),
        )
        _assert_rejected(
            ambiguous_session, result, reason="ambiguous", before=before
        )


def test_rejection_does_not_anchor_a_later_legitimate_delivery(
    session: Session, engine: Engine, org: Org
) -> None:
    """拒绝不写锚点：员工完成绑定后，同一条消息仍能被正常受理。"""
    message = _message(external_message_id="msg-late", external_user_id="wx-late")

    first = ingest_inbound_message(session, message)
    assert first.status == INGEST_REJECTED
    assert first.reason == "unbound"

    _add_binding(engine, employee_id=org.active_employee.id, external_user_id="wx-late")
    second = ingest_inbound_message(session, message)
    assert second.status == INGEST_ACCEPTED
    assert second.intake_job_id is not None


# --- 4. 结构校验（写库之前拒绝） ------------------------------------------------


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (_message(channel_type="telegram"), "unknown_channel_type"),
        (_message(channel_type=""), "unknown_channel_type"),
        (_message(input_type="video"), "unknown_input_type"),
        (_message(external_message_id=""), "missing_message_id"),
        (_message(external_message_id="   "), "missing_message_id"),
        (_message(external_user_id=""), "missing_sender_id"),
        (_message(external_user_id="  "), "missing_sender_id"),
    ],
    ids=[
        "unknown-channel",
        "blank-channel",
        "unknown-input",
        "blank-message-id",
        "spaces-message-id",
        "blank-sender",
        "spaces-sender",
    ],
)
def test_structural_rejections_write_nothing(
    session: Session, org: Org, message: InboundChannelMessage, reason: str
) -> None:
    """未知渠道/输入、缺失消息 ID/发送者：不落库、也不走到身份解析。

    这里刻意**不建任何绑定**：若校验被放到身份解析之后，结果会变成 ``unbound``
    而不是这里的结构原因——断言 reason 即可证明校验发生在写库与解析之前。
    """
    before = _tally(session)
    result = ingest_inbound_message(session, message)
    _assert_rejected(session, result, reason=reason, before=before)


def test_missing_sender_passed_as_none_is_rejected(session: Session) -> None:
    """渠道侧确实可能给不出消息 ID / 发送者（``None``）——必须明确拒绝。"""
    before = _tally(session)
    message = InboundChannelMessage(
        channel_type="weixin",
        external_message_id="msg-none-sender",
        external_user_id=None,  # type: ignore[arg-type]
        input_type="text",
    )
    result = ingest_inbound_message(session, message)
    _assert_rejected(session, result, reason="missing_sender_id", before=before)


# --- 5. 数据库约束 -------------------------------------------------------------


def test_unique_constraint_rejects_second_anchor(session: Session, org: Org) -> None:
    """``UNIQUE (channel_type, external_message_id)`` 在库层兜底（服务被绕过也有效）。"""
    job = IntakeJob(
        submitted_by=org.active_employee.id, channel_type="weixin", input_type="text"
    )
    session.add(job)
    session.commit()

    session.add(
        InboundMessage(
            channel_type="weixin",
            external_message_id="msg-unique",
            external_user_id="wx-a",
            intake_job_id=job.id,
            status="accepted",
        )
    )
    session.commit()

    with pytest.raises(IntegrityError) as excinfo:
        with session.begin_nested():
            session.add(
                InboundMessage(
                    channel_type="weixin",
                    external_message_id="msg-unique",
                    external_user_id="wx-b",
                    intake_job_id=job.id,
                    status="accepted",
                )
            )
            session.flush()
    assert _ANCHOR_CONSTRAINT in str(excinfo.value)


def test_same_message_id_on_another_channel_is_allowed(
    session: Session, org: Org
) -> None:
    """幂等键是**复合**键：同一消息 ID 在不同渠道是两条独立投递。"""
    job = IntakeJob(
        submitted_by=org.active_employee.id, channel_type="weixin", input_type="text"
    )
    session.add(job)
    session.commit()

    for channel in SUPPORTED_CHANNEL_TYPES[:2]:
        session.add(
            InboundMessage(
                channel_type=channel,
                external_message_id="msg-shared",
                external_user_id="wx-a",
                intake_job_id=job.id,
                status="accepted",
            )
        )
    session.commit()  # must not raise

    count = session.scalar(
        select(func.count())
        .select_from(InboundMessage)
        .where(InboundMessage.external_message_id == "msg-shared")
    )
    assert count == 2


def test_invalid_status_is_rejected_by_check(session: Session, org: Org) -> None:
    """``status`` 只允许 ``accepted`` / ``rejected``（TEXT + CHECK，非 ENUM）。"""
    job = IntakeJob(
        submitted_by=org.active_employee.id, channel_type="weixin", input_type="text"
    )
    session.add(job)
    session.commit()

    with pytest.raises(IntegrityError) as excinfo:
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO inbound_messages "
                    "(channel_type, external_message_id, external_user_id, "
                    "intake_job_id, status) "
                    "VALUES ('weixin', 'msg-bad-status', 'wx-a', :job, 'bogus')"
                ),
                {"job": job.id},
            )
    assert "ck_inbound_messages_status_allowed" in str(excinfo.value)


# --- 6. 审计与字符串表示 -------------------------------------------------------


def test_accepted_audit_uses_salted_hash_and_never_plaintext(
    session: Session,
    engine: Engine,
    org: Org,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受理审计只含加盐哈希；整张审计表与结果字符串都不出现渠道身份明文。"""
    monkeypatch.setenv(IDENTITY_HASH_SALT_ENV, "issue-21-audit-salt")
    sender = "wx-audit-4711"
    _add_binding(engine, employee_id=org.active_employee.id, external_user_id=sender)

    result = ingest_inbound_message(
        session, _message(external_message_id="msg-audit", external_user_id=sender)
    )
    assert result.status == INGEST_ACCEPTED

    event = session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == ACCEPTED_AUDIT_ACTION,
            AuditEvent.resource_id == str(result.intake_job_id),
        )
    )
    assert event is not None
    assert event.actor_id == org.active_employee.id
    assert event.details["channel_type"] == "weixin"
    assert event.details["input_type"] == "text"
    assert event.details["intake_job_id"] == result.intake_job_id
    assert event.details["external_user_id_hash"] == _hash_channel_identity(
        "weixin", sender
    )
    # 正文与渠道消息 ID 不进审计。
    assert "msg-audit" not in str(event.details)

    leaks = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(cast(AuditEvent.details, SqlText).like(f"%{sender}%"))
    )
    assert leaks == 0
    assert sender not in repr(result)
    assert sender not in str(result)


def test_rejected_result_string_hides_internal_reason(
    session: Session, org: Org
) -> None:
    """``reason`` 只供内部使用，不得出现在对象字符串表示里。"""
    result = ingest_inbound_message(
        session, _message(external_message_id="msg-repr", external_user_id="wx-nobody")
    )
    assert result.status == INGEST_REJECTED
    assert result.reason == "unbound"
    assert result.reason not in repr(result)
    assert result.reason not in str(result)
    assert "unbound" not in repr(result)


# --- 7. 适配器契约 -------------------------------------------------------------


class _StubAdapter:
    """测试用桩适配器：只做字段搬运，不含任何渠道 SDK。"""

    def __init__(self, channel_type: str) -> None:
        self._channel_type = channel_type

    @property
    def channel_type(self) -> str:
        return self._channel_type

    def normalize(self, payload: object) -> InboundChannelMessage:
        raw = payload
        assert isinstance(raw, dict)
        return InboundChannelMessage(
            channel_type=self._channel_type,
            external_message_id=str(raw["message_id"]),
            external_user_id=str(raw["sender"]),
            input_type=str(raw["input_type"]),
        )


def test_stub_adapter_satisfies_protocol_and_drives_ingest(
    session: Session, engine: Engine, org: Org
) -> None:
    """Protocol 用结构类型判定；桩适配器归一化后的消息可直接驱动受理。"""
    adapter = _StubAdapter("weixin")
    assert isinstance(adapter, InboundChannelAdapter)
    assert not isinstance(object(), InboundChannelAdapter)

    _add_binding(
        engine, employee_id=org.active_employee.id, external_user_id="wx-adapter"
    )
    message = adapter.normalize(
        {"message_id": "msg-adapter", "sender": "wx-adapter", "input_type": "file"}
    )
    assert message.channel_type == adapter.channel_type

    result = ingest_inbound_message(session, message)
    assert result.status == INGEST_ACCEPTED
    job = session.get(IntakeJob, result.intake_job_id)
    assert job is not None
    assert job.input_type == "file"
    assert job.channel_type == "weixin"
