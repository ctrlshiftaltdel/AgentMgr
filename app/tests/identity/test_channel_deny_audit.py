"""W1-21 渠道身份**拒绝审计**测试（针对真实 ``ajaimgr_test`` 库）。

覆盖任务 W1-21 的两组要求：

第一步（实现）：

1. 六种拒绝（unbound / ambiguous / revoked / unverified / employee_inactive /
   company_mismatch）**各**在同一事务内写一条审计事件，``reason`` 正确；
2. 审计行与异常中均**不含**渠道用户 ID 明文（用真实渠道 ID 串在**整张**审计表与
   异常里搜索均为空）；
3. 同一渠道身份的重复拒绝**去重 / 限频**：首次一定记、窗口内不重复记、窗口过期后
   再记；
4. 盐未配置时**不得**写明文（实测行为与断言）；
5. 审计写入失败时**仍拒绝**（monkeypatch 让 ``record_event`` 抛错，断言仍是同一异常
   而非 500）；
6. 成功解析**不**写拒绝审计。

第二步（测试）：本文件即测试。

实现要点：

* 每个测试用**独立的盐**（``monkeypatch.setenv(AJAIMGR_IDENTITY_HASH_SALT, ...)``），
  使各测试的身份哈希互不相同，避免 ``audit_events`` 跨测试累积导致的去重误判——
  该表只增不改，``conftest`` 只在会话首尾清空它；
* 歧义场景用**真实** DDL（同一事务内临时删除唯一约束并插入两条，退出回滚）构造，
  与 W1-19 测试同一手法；该场景的审计写在**调用方事务**内，故在事务内查询。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.db.models.organization import Company, Department, Employee
from app.identity.authorization import Principal
from app.identity.channel_routing import (
    IDENTITY_HASH_SALT_ENV,
    REJECTION_AUDIT_ACTION,
    REJECTION_AUDIT_RESOURCE_TYPE,
    REJECTION_DEDUPE_WINDOW_SECONDS,
    REJECTION_MESSAGE,
    ChannelIdentityRejectedError,
    _hash_channel_identity,
    resolve_principal,
)
from app.identity.models import EmployeeChannelBinding

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN_ENV = "AJAIMGR_INTERNAL_TOKEN"

_UNIQUE_CONSTRAINT = "uq_employee_channel_bindings_channel_type"


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head 再运行这些测试。"""
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
            session.execute(delete(EmployeeChannelBinding))
            session.execute(delete(Employee))
            session.execute(delete(Department))
            session.execute(delete(Company))
            session.commit()


@pytest.fixture(autouse=True)
def _unique_salt(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """为每个测试注入**独立**的盐，隔离身份哈希（避免跨测试去重误判）。

    需要「盐未配置」行为的测试会在自身内 ``delenv`` 覆盖本 fixture。
    """
    monkeypatch.setenv(IDENTITY_HASH_SALT_ENV, f"w1-21-{request.node.name}")


def _add_binding(
    engine: Engine,
    *,
    employee_id: int,
    channel_type: str = "weixin",
    external_user_id: str,
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
    """在同一事务内临时移除唯一约束并插入两条同键绑定，退出时回滚（真实歧义）。"""
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
            conn.execute(insert, {"e": employee_id, "c": channel_type, "x": external_user_id})
            conn.execute(insert, {"e": employee_id, "c": channel_type, "x": external_user_id})
            with Session(bind=conn) as session:
                yield session
        finally:
            trans.rollback()


def _rejection(**kwargs: object) -> ChannelIdentityRejectedError:
    """调用解析器并返回其抛出的拒绝异常（断言确实抛了）。"""
    with pytest.raises(ChannelIdentityRejectedError) as excinfo:
        resolve_principal(**kwargs)  # type: ignore[arg-type]
    return excinfo.value


def _rejection_rows(
    engine: Engine,
    *,
    resource_id: str | None = None,
    channel_type: str | None = None,
) -> list[AuditEvent]:
    """按条件查询拒绝审计行（真实库查询）。"""
    with Session(engine) as session:
        stmt = select(AuditEvent).where(AuditEvent.action == REJECTION_AUDIT_ACTION)
        if resource_id is not None:
            stmt = stmt.where(AuditEvent.resource_id == resource_id)
        if channel_type is not None:
            stmt = stmt.where(AuditEvent.details["channel_type"].astext == channel_type)
        return list(session.scalars(stmt.order_by(AuditEvent.id)))


# --- 要求 1：六种拒绝各写一条审计且 reason 正确 --------------------------------


def test_each_of_six_rejections_writes_one_audit_with_correct_reason(
    engine: Engine, org: Org
) -> None:
    """六种拒绝：对外同一异常 / 同一文案，且各写一条审计，``reason`` 正确。"""
    salt = "w1-21-six-reasons"
    ct = "weixin"
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        channel_type=ct,
        external_user_id="w1-21-revoked",
        status="revoked",
    )
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        channel_type=ct,
        external_user_id="w1-21-unverified",
        verified=False,
    )
    _add_binding(
        engine,
        employee_id=org.inactive_employee.id,
        channel_type=ct,
        external_user_id="w1-21-inactive-emp",
    )
    _add_binding(
        engine,
        employee_id=org.foreign_employee.id,
        channel_type=ct,
        external_user_id="w1-21-foreign",
    )

    expected: dict[str, str] = {
        "w1-21-unbound": "unbound",
        "w1-21-revoked": "revoked",
        "w1-21-unverified": "unverified",
        "w1-21-inactive-emp": "employee_inactive",
        "w1-21-foreign": "company_mismatch",
    }

    rejections: list[ChannelIdentityRejectedError] = []
    with Session(engine) as session:
        rejections.append(
            _rejection(session=session, channel_type=ct, external_user_id="w1-21-unbound")
        )
        rejections.append(
            _rejection(session=session, channel_type=ct, external_user_id="w1-21-revoked")
        )
        rejections.append(
            _rejection(session=session, channel_type=ct, external_user_id="w1-21-unverified")
        )
        rejections.append(
            _rejection(
                session=session, channel_type=ct, external_user_id="w1-21-inactive-emp"
            )
        )
        rejections.append(
            _rejection(
                session=session,
                channel_type=ct,
                external_user_id="w1-21-foreign",
                expected_company_id=org.company.id,
            )
        )

    # 对外不可区分：同一类型、同一文案。
    assert {type(e) for e in rejections} == {ChannelIdentityRejectedError}
    assert {str(e) for e in rejections} == {REJECTION_MESSAGE}

    # 每条审计：reason 正确、含加盐哈希、actor_id 为 None（无可归因的已核验人员）。
    for ext, reason in expected.items():
        identity_hash = _hash_channel_identity(ct, ext)
        assert identity_hash is not None
        rows = _rejection_rows(engine, resource_id=identity_hash)
        assert len(rows) == 1, (ext, reason, len(rows))
        row = rows[0]
        assert row.details["reason"] == reason
        assert row.details["channel_type"] == ct
        assert row.details["external_user_id_hash"] == identity_hash
        assert row.resource_id == identity_hash
        assert row.resource_type == REJECTION_AUDIT_RESOURCE_TYPE
        assert row.actor_id is None

    # 歧义：真实 DDL 构造，审计写在调用方事务内，故在事务内查询。
    with _session_with_duplicate_binding(
        engine,
        employee_id=org.active_employee.id,
        channel_type=ct,
        external_user_id="w1-21-ambiguous",
    ) as session:
        exc = _rejection(session=session, channel_type=ct, external_user_id="w1-21-ambiguous")
        assert exc.reason == "ambiguous"
        assert str(exc) == REJECTION_MESSAGE
        identity_hash = _hash_channel_identity(ct, "w1-21-ambiguous")
        rows = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == REJECTION_AUDIT_ACTION,
                AuditEvent.resource_id == identity_hash,
            )
        ).all()
        assert len(rows) == 1, "ambiguous rejection must write exactly one audit row"
        assert rows[0].details["reason"] == "ambiguous"


# --- 要求 2：明文不落库、不进异常 ----------------------------------------------


def test_no_plaintext_channel_id_in_audit_or_exception(engine: Engine, org: Org) -> None:
    """真实渠道 ID 串在**整张**审计表与异常里搜索均为空（绝不落明文）。"""
    plaintext = "wx-w1-21-plaintext-probe-8f4c1a"
    ct = "weixin"

    with Session(engine) as session:
        exc = _rejection(session=session, channel_type=ct, external_user_id=plaintext)

    # 异常（类型 + 文案 + repr）不含明文。
    rendered = f"{exc!r} {exc} {exc.args!r} {vars(exc)!r}"
    assert plaintext not in rendered
    assert str(exc) == REJECTION_MESSAGE

    # 整张审计表（所有列拼接为文本）不含明文。
    with Session(engine) as session:
        hits = session.scalar(
            text("SELECT count(*) FROM audit_events WHERE audit_events::text LIKE :p"),
            {"p": f"%{plaintext}%"},
        )
    assert hits == 0, "plaintext channel id must never be written to audit_events"

    # 但该拒绝**确实**被审计了（以哈希形式），证明上面不是「什么都没写」的假阴性。
    identity_hash = _hash_channel_identity(ct, plaintext)
    rows = _rejection_rows(engine, resource_id=identity_hash)
    assert len(rows) == 1
    assert rows[0].details["external_user_id_hash"] == identity_hash
    assert plaintext not in str(rows[0].details)


# --- 要求 3：去重 / 限频 -------------------------------------------------------


def test_repeated_rejection_is_deduplicated_but_first_is_always_recorded(
    engine: Engine, org: Org
) -> None:
    """同一渠道身份重复拒绝：首次一定记，窗口内不重复记。"""
    ct = "weixin"
    ext = "w1-21-dedupe"
    identity_hash = _hash_channel_identity(ct, ext)
    assert identity_hash is not None

    # 首次拒绝：一定写一条。
    with Session(engine) as session:
        _rejection(session=session, channel_type=ct, external_user_id=ext)
    assert len(_rejection_rows(engine, resource_id=identity_hash)) == 1

    # 窗口内再拒 5 次：行数不变（去重生效）。
    for _ in range(5):
        with Session(engine) as session:
            _rejection(session=session, channel_type=ct, external_user_id=ext)
    assert len(_rejection_rows(engine, resource_id=identity_hash)) == 1, (
        "repeated rejections within the window must not grow the append-only table"
    )

    # 不同身份仍然各记一条（去重只针对同一身份）。
    other = "w1-21-dedupe-other"
    other_hash = _hash_channel_identity(ct, other)
    with Session(engine) as session:
        _rejection(session=session, channel_type=ct, external_user_id=other)
    assert len(_rejection_rows(engine, resource_id=other_hash)) == 1


def test_rejection_is_recorded_again_after_the_dedupe_window_expires(
    engine: Engine, org: Org
) -> None:
    """窗口过期后同一身份再次被拒会**再记**一条（限频不是永久静默）。"""
    ct = "weixin"
    ext = "w1-21-window-expiry"
    identity_hash = _hash_channel_identity(ct, ext)
    assert identity_hash is not None

    # 直接植入一条「早已过期」的同身份拒绝审计（created_at 在窗口之外）。
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=REJECTION_DEDUPE_WINDOW_SECONDS * 2
    )
    with Session(engine) as session:
        session.add(
            AuditEvent(
                actor_id=None,
                action=REJECTION_AUDIT_ACTION,
                resource_type=REJECTION_AUDIT_RESOURCE_TYPE,
                resource_id=identity_hash,
                request_id=None,
                details={"channel_type": ct, "reason": "unbound"},
                created_at=stale,
            )
        )
        session.commit()
    assert len(_rejection_rows(engine, resource_id=identity_hash)) == 1

    # 窗口已过：本次拒绝必须**再**写一条。
    with Session(engine) as session:
        _rejection(session=session, channel_type=ct, external_user_id=ext)
    assert len(_rejection_rows(engine, resource_id=identity_hash)) == 2


# --- 要求 4：盐未配置时不得写明文 ----------------------------------------------


def test_missing_salt_records_reason_without_any_identity(
    engine: Engine, org: Org, monkeypatch: pytest.MonkeyPatch
) -> None:
    """盐未配置（专用盐与内部令牌均缺失）：只记 reason，**绝不**退化为写明文。"""
    monkeypatch.delenv(IDENTITY_HASH_SALT_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)

    # 唯一 channel_type：盐缺失时去重键退化为 (channel_type, reason)，需与其它测试隔离。
    ct = "w1-21-nosalt-channel"
    plaintext = "wx-w1-21-nosalt-plaintext-2b7d"

    with Session(engine) as session:
        exc = _rejection(session=session, channel_type=ct, external_user_id=plaintext)
    assert exc.reason == "unbound"
    assert plaintext not in f"{exc!r} {exc}"

    rows = _rejection_rows(engine, channel_type=ct)
    assert len(rows) == 1
    row = rows[0]
    assert row.details["reason"] == "unbound"
    assert row.details["channel_type"] == ct
    # 无盐 ⇒ 无身份哈希：既没有哈希字段，resource_id 也为 None，更无明文。
    assert "external_user_id_hash" not in row.details
    assert row.resource_id is None
    assert plaintext not in str(row.details)

    with Session(engine) as session:
        hits = session.scalar(
            text("SELECT count(*) FROM audit_events WHERE audit_events::text LIKE :p"),
            {"p": f"%{plaintext}%"},
        )
    assert hits == 0


# --- 要求 5：审计失败仍拒绝 ----------------------------------------------------


def test_audit_write_failure_still_rejects(
    engine: Engine, org: Org, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``record_event`` 抛错时仍是同一拒绝异常（不是 500、不放行）。"""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(
        "app.identity.channel_routing.record_event", _boom
    )
    with Session(engine) as session:
        with pytest.raises(ChannelIdentityRejectedError) as excinfo:
            resolve_principal(
                session, channel_type="weixin", external_user_id="w1-21-audit-fail"
            )
    assert type(excinfo.value) is ChannelIdentityRejectedError
    assert str(excinfo.value) == REJECTION_MESSAGE
    assert excinfo.value.reason == "unbound"


def test_audit_dedupe_query_failure_still_rejects(
    engine: Engine, org: Org, monkeypatch: pytest.MonkeyPatch
) -> None:
    """去重查询本身抛错时也仍拒绝（审计链路任何一环故障都不得放行）。"""

    def _boom(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("dedupe query down")

    monkeypatch.setattr(
        "app.identity.channel_routing._recent_rejection_exists", _boom
    )
    with Session(engine) as session:
        with pytest.raises(ChannelIdentityRejectedError) as excinfo:
            resolve_principal(
                session, channel_type="weixin", external_user_id="w1-21-dedupe-fail"
            )
    assert type(excinfo.value) is ChannelIdentityRejectedError
    assert str(excinfo.value) == REJECTION_MESSAGE
    assert excinfo.value.reason == "unbound"


# --- 要求 6：成功解析不写拒绝审计 ----------------------------------------------


def test_successful_resolution_writes_no_rejection_audit(
    engine: Engine, org: Org
) -> None:
    """已绑定 + active + 已核验：返回主体，且**不**写任何拒绝审计。"""
    ct = "weixin"
    ext = "w1-21-success"
    _add_binding(
        engine, employee_id=org.active_employee.id, channel_type=ct, external_user_id=ext
    )
    identity_hash = _hash_channel_identity(ct, ext)

    with Session(engine) as session:
        principal = resolve_principal(session, channel_type=ct, external_user_id=ext)
    assert principal == Principal(
        employee_id=org.active_employee.id, company_id=org.company.id
    )

    # 该身份的哈希在拒绝审计里查不到任何行：成功解析不写拒绝审计。
    assert _rejection_rows(engine, resource_id=identity_hash) == []
