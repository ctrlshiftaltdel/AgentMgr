"""W1-19 渠道身份路由解析测试（针对 ajaimgr_test 库）。

覆盖任务 W1-19 的解析规则 1–7 与端到端不可采信性：

1. 已绑定 + active + 已核验 → 返回正确的 ``employee_id`` / ``company_id``；
2. 未绑定 → 拒绝；
3. ``revoked`` → 拒绝；
4. ``verified_at`` 为空（未验证）→ 拒绝；
5. 员工非 active → 拒绝；
6. ``expected_company_id`` 不匹配 → 拒绝（正例：匹配则通过）；
7. 歧义（同一 ``(channel_type, external_user_id)`` 命中两条）→ 拒绝——用真实
   DDL 在同一事务内**临时**删除唯一约束并插入两条，解析后回滚，约束与数据均复原；
8. **对外不可区分**：上述各拒绝场景抛出**同一异常类型**且 ``str(exc)`` **完全相同**，
   且文案不含渠道用户 ID / 员工 ID；
9. 端到端：渠道身份参数（查询 / 请求体）无法让请求通过 ``app.api.deps`` 的头部校验
   拿到任何 2xx。

每个测试自行造 company/employee/binding，teardown 删除这些行，不残留数据。
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
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.identity.authorization import Principal
from app.identity.channel_routing import (
    REJECTION_MESSAGE,
    ChannelIdentityRejectedError,
    resolve_principal,
)
from app.identity.models import EmployeeChannelBinding, EmployeeRole, Role
from app.identity.seed import clear_permission_catalog, seed_permission_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_TOKEN = "w1-19-test-internal-token"
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
        company = Company(name="W1-19 公司")
        other = Company(name="W1-19 他司")
        session.add_all([company, other])
        session.flush()
        department = Department(company_id=company.id, name="W1-19 部")
        session.add(department)
        session.flush()

        active = Employee(
            company_id=company.id,
            department_id=department.id,
            employee_no="W1-19-active",
            status="active",
        )
        inactive = Employee(
            company_id=company.id,
            department_id=department.id,
            employee_no="W1-19-inactive",
            status="inactive",
        )
        foreign = Employee(
            company_id=other.id,
            employee_no="W1-19-foreign",
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
    """在同一事务内临时移除唯一约束并插入两条同键绑定，退出时回滚。

    PostgreSQL 支持事务性 DDL：``ALTER TABLE ... DROP CONSTRAINT`` 在回滚时复原，
    两条插入同样回滚，因此调用方退出后库结构与数据均恢复原状。这样「歧义」由
    **真实** SQL 构造，而不是 mock。
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


# --- 规则 1：正向解析 ----------------------------------------------------------


def test_bound_active_verified_resolves_to_principal(engine: Engine, org: Org) -> None:
    """已绑定 + active + 已核验 → 返回正确且来自数据库的 employee_id/company_id。"""
    _add_binding(
        engine, employee_id=org.active_employee.id, external_user_id="wx-ok-1"
    )
    with Session(engine) as session:
        principal = resolve_principal(
            session, channel_type="weixin", external_user_id="wx-ok-1"
        )
    assert principal == Principal(
        employee_id=org.active_employee.id, company_id=org.company.id
    )


def test_expected_company_match_still_returns_db_values(engine: Engine, org: Org) -> None:
    """expected_company_id 匹配时通过，且返回值只取自数据库记录。"""
    _add_binding(
        engine, employee_id=org.active_employee.id, external_user_id="wx-ok-2"
    )
    with Session(engine) as session:
        principal = resolve_principal(
            session,
            channel_type="weixin",
            external_user_id="wx-ok-2",
            expected_company_id=org.company.id,
        )
    # 主体取自绑定 → 员工记录，而非调用方参数。
    assert principal.employee_id == org.active_employee.id
    assert principal.company_id == org.company.id


# --- 规则 2–6：逐条拒绝 --------------------------------------------------------


def test_unbound_is_rejected(engine: Engine, org: Org) -> None:
    with Session(engine) as session:
        with pytest.raises(ChannelIdentityRejectedError):
            resolve_principal(
                session, channel_type="weixin", external_user_id="wx-never-bound"
            )


def test_revoked_is_rejected(engine: Engine, org: Org) -> None:
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        external_user_id="wx-revoked",
        status="revoked",
    )
    with Session(engine) as session:
        with pytest.raises(ChannelIdentityRejectedError):
            resolve_principal(
                session, channel_type="weixin", external_user_id="wx-revoked"
            )


def test_unverified_is_rejected(engine: Engine, org: Org) -> None:
    """verified_at IS NULL 的绑定不得认证任何人（规则 3）。"""
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        external_user_id="wx-unverified",
        verified=False,
    )
    with Session(engine) as session:
        with pytest.raises(ChannelIdentityRejectedError):
            resolve_principal(
                session, channel_type="weixin", external_user_id="wx-unverified"
            )


def test_inactive_employee_is_rejected(engine: Engine, org: Org) -> None:
    _add_binding(
        engine,
        employee_id=org.inactive_employee.id,
        external_user_id="wx-inactive-emp",
    )
    with Session(engine) as session:
        with pytest.raises(ChannelIdentityRejectedError):
            resolve_principal(
                session, channel_type="weixin", external_user_id="wx-inactive-emp"
            )


def test_expected_company_mismatch_is_rejected(engine: Engine, org: Org) -> None:
    """员工属于他司，但调用方期望本公司 → 拒绝（防跨公司解析）。"""
    _add_binding(
        engine,
        employee_id=org.foreign_employee.id,
        external_user_id="wx-foreign",
    )
    with Session(engine) as session:
        with pytest.raises(ChannelIdentityRejectedError):
            resolve_principal(
                session,
                channel_type="weixin",
                external_user_id="wx-foreign",
                expected_company_id=org.company.id,
            )


def test_channel_type_is_part_of_identity(engine: Engine, org: Org) -> None:
    """同一 external_user_id 在不同渠道是不同身份，绝不跨渠道复用。"""
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        channel_type="weixin",
        external_user_id="shared-id",
    )
    with Session(engine) as session:
        # 相同 external id 但不同渠道 → 未绑定 → 拒绝。
        with pytest.raises(ChannelIdentityRejectedError):
            resolve_principal(
                session, channel_type="dingtalk", external_user_id="shared-id"
            )


# --- 规则 1 防御性检查：歧义 ---------------------------------------------------


def test_ambiguous_binding_is_rejected(engine: Engine, org: Org) -> None:
    """同一键命中两条绑定 → 拒绝，绝不静默取第一条。"""
    with _session_with_duplicate_binding(
        engine,
        employee_id=org.active_employee.id,
        channel_type="weixin",
        external_user_id="wx-ambiguous",
    ) as session:
        with pytest.raises(ChannelIdentityRejectedError) as excinfo:
            resolve_principal(
                session, channel_type="weixin", external_user_id="wx-ambiguous"
            )
        assert excinfo.value.reason == "ambiguous"

    # 唯一约束已在回滚后复原：库层结构上仍不允许同键两条。
    with engine.connect() as conn:
        restored = conn.execute(
            text(
                "SELECT 1 FROM pg_constraint WHERE conname = :n"
            ),
            {"n": _UNIQUE_CONSTRAINT},
        ).scalar()
    assert restored == 1, "unique constraint must be restored after rollback"


def test_db_prevents_duplicate_identity_key(engine: Engine, org: Org) -> None:
    """库层 UNIQUE (channel_type, external_user_id) 使歧义在结构上不可能。"""
    from sqlalchemy.exc import IntegrityError

    _add_binding(
        engine, employee_id=org.active_employee.id, external_user_id="wx-dup"
    )
    with Session(engine) as session:
        session.add(
            EmployeeChannelBinding(
                employee_id=org.active_employee.id,
                channel_type="weixin",
                external_user_id="wx-dup",
                status="active",
                verified_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# --- 规则 7：对外不可区分 ------------------------------------------------------


def _collect_rejections(engine: Engine, org: Org) -> list[ChannelIdentityRejectedError]:
    """构造规则 2–6 与歧义的六种拒绝，返回各自的异常实例。"""
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        external_user_id="wx-rev",
        status="revoked",
    )
    _add_binding(
        engine,
        employee_id=org.active_employee.id,
        external_user_id="wx-unver",
        verified=False,
    )
    _add_binding(
        engine, employee_id=org.inactive_employee.id, external_user_id="wx-inact"
    )
    _add_binding(
        engine, employee_id=org.foreign_employee.id, external_user_id="wx-for"
    )

    out: list[ChannelIdentityRejectedError] = []
    with Session(engine) as session:
        out.append(
            _rejection(
                session=session,
                channel_type="weixin",
                external_user_id="wx-absent",
            )
        )
        out.append(
            _rejection(session=session, channel_type="weixin", external_user_id="wx-rev")
        )
        out.append(
            _rejection(session=session, channel_type="weixin", external_user_id="wx-unver")
        )
        out.append(
            _rejection(session=session, channel_type="weixin", external_user_id="wx-inact")
        )
        out.append(
            _rejection(
                session=session,
                channel_type="weixin",
                external_user_id="wx-for",
                expected_company_id=org.company.id,
            )
        )
    with _session_with_duplicate_binding(
        engine,
        employee_id=org.active_employee.id,
        channel_type="weixin",
        external_user_id="wx-amb",
    ) as session:
        out.append(
            _rejection(session=session, channel_type="weixin", external_user_id="wx-amb")
        )
    return out


def test_all_rejections_are_indistinguishable(engine: Engine, org: Org) -> None:
    """六种拒绝：同一异常类型、同一文案，且文案不泄露渠道 / 员工标识。"""
    rejections = _collect_rejections(engine, org)
    assert len(rejections) == 6

    types = {type(exc) for exc in rejections}
    assert types == {ChannelIdentityRejectedError}

    messages = {str(exc) for exc in rejections}
    assert messages == {REJECTION_MESSAGE}

    for exc in rejections:
        rendered = f"{exc!r} {exc}"
        for leak in ("wx-absent", "wx-rev", "wx-unver", "wx-inact", "wx-for", "wx-amb"):
            assert leak not in rendered
        assert str(org.active_employee.id) not in rendered

    # 内部原因仍然可区分（供审计 / 调试），但只存在于 .reason，不进入文案。
    assert {exc.reason for exc in rejections} == {
        "unbound",
        "revoked",
        "unverified",
        "employee_inactive",
        "company_mismatch",
        "ambiguous",
    }


# --- 规则 9：端到端不可采信 ----------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
    from app.api.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_unbound_channel_identity_yields_no_principal_via_api(
    client: TestClient, org: Org
) -> None:
    """渠道身份（查询 / 请求体）无法让请求拿到任何 2xx：头部校验仍 fail closed。"""
    bearer = {"Authorization": f"Bearer {TEST_TOKEN}"}
    channel = {"channel_type": "weixin", "external_user_id": "wx-never-bound"}

    query = client.get("/claims", params=channel, headers=bearer)
    assert query.status_code == 401

    body = client.post("/claims", json=channel, headers=bearer)
    assert body.status_code == 401

    # 即便把一个有效员工塞进渠道参数（冒充「解析结果」），没有 X-Employee-Id 头
    # 依然 401——主体绝不由渠道参数给出。
    spoofed = client.get(
        "/claims",
        params={**channel, "employee_id": str(org.active_employee.id)},
        headers=bearer,
    )
    assert spoofed.status_code == 401


# --- 第三步：后台绑定必须写入 verified_at --------------------------------------


@pytest.fixture()
def admin_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
    from app.api.app import create_app

    with TestClient(
        create_app(), raise_server_exceptions=False, follow_redirects=False
    ) as test_client:
        yield test_client


def test_admin_bind_sets_verified_at_and_enables_resolution(
    engine: Engine, org: Org, admin_client: TestClient
) -> None:
    """后台 bind 动作写入 verified_at，且该绑定随后可被解析器认证（第三步）。"""
    from app.api.admin_auth import SESSION_COOKIE_NAME

    with Session(engine) as session:
        seed_permission_catalog(session)
        role = session.scalar(select(Role).where(Role.name == "sysadmin"))
        assert role is not None
        admin = Employee(
            company_id=org.company.id, employee_no="W1-19-admin", status="active"
        )
        session.add(admin)
        session.flush()
        session.add(EmployeeRole(employee_id=admin.id, role_id=role.id))
        session.commit()

    try:
        login = admin_client.post(
            "/admin/login", data={"token": TEST_TOKEN, "employee_no": "W1-19-admin"}
        )
        assert login.status_code == 303, login.text
        cookie = login.cookies.get(SESSION_COOKIE_NAME)
        assert cookie

        bind = admin_client.post(
            "/admin/bindings",
            data={
                "action": "bind",
                "employee_id": str(org.active_employee.id),
                "channel_type": "weixin",
                "external_user_id": "wx-admin-bound",
            },
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"},
        )
        assert bind.status_code == 303, bind.text

        with Session(engine) as session:
            binding = session.scalar(
                select(EmployeeChannelBinding).where(
                    EmployeeChannelBinding.external_user_id == "wx-admin-bound"
                )
            )
            assert binding is not None
            assert binding.verified_at is not None, (
                "admin bind must stamp verified_at, else the binding can never "
                "authenticate anyone"
            )
            principal = resolve_principal(
                session, channel_type="weixin", external_user_id="wx-admin-bound"
            )
        assert principal.employee_id == org.active_employee.id
        assert principal.company_id == org.company.id
    finally:
        with Session(engine) as session:
            session.execute(delete(EmployeeRole))
            session.execute(delete(Employee))
            clear_permission_catalog(session)
            session.commit()
