"""迁移 0010（``invoice_verifications``）的往返与结构测试。

验证 0010 的 ``upgrade`` → ``downgrade`` 干净往返。自 0011（W1-23）起 head 不再
等于 0010，``downgrade -1`` 只会退回 0011 的前驱（即 0010），因此本测试**显式
指定目标 revision**（退到恰好 0010 → 退到 0009 → 升回 head），保持对 0010 本身
的往返验证；并断言表结构满足 PLAN §8 / §7.2 的硬性要求（金额为 numeric、
``duplicate_key`` 唯一、七种状态受 CHECK 约束、证据引用列存在）。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from app.integrations.models import (
    DUPLICATE_CHECK_VALUES,
    PROVIDER_TYPE_VALUES,
    STATUS_VALUES,
)

from app.tests.integrations.invoice_fixtures import alembic_config

REVISION = "0010_invoice_verifications"
PREVIOUS = "0009_perm_alignment"
TABLE = "invoice_verifications"


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def _table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _ensure_head(engine: Engine) -> None:
    command.upgrade(alembic_config(), "head")


def _head_revision(config: Config) -> str:
    """当前迁移脚本的 head revision（不写死，避免新增迁移后本测试过期）。"""
    return ScriptDirectory.from_config(config).get_current_head()


def test_migration_0010_roundtrip(engine: Engine) -> None:
    """退到恰好 0010 → 退到 0009 → 升回 head：三步都成功且表按预期增删。"""
    config = alembic_config()
    head = _head_revision(config)
    try:
        _ensure_head(engine)
        assert TABLE in _table_names(engine)

        # 退到恰好 0010（head 为更新的 revision 时，这一步先丢弃其后的迁移）。
        command.downgrade(config, REVISION)
        assert TABLE in _table_names(engine)
        assert _current_revision(engine) == REVISION

        # 0010 自身的降级：表随迁移消失，库回到 0009。
        command.downgrade(config, PREVIOUS)
        assert TABLE not in _table_names(engine)
        assert _current_revision(engine) == PREVIOUS

        # 升回 head：0010 重新建表。
        command.upgrade(config, "head")
        assert TABLE in _table_names(engine)
        assert _current_revision(engine) == head
    finally:
        # 无论断言是否失败，都把测试库恢复到 head，避免影响后续测试。
        command.upgrade(config, "head")


def test_migration_0010_columns_and_types(engine: Engine) -> None:
    """列齐全、金额为固定精度 numeric（非浮点）。"""
    _ensure_head(engine)
    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}

    # PLAN §8 的列。
    for required in (
        "id",
        "source_file_id",
        "provider_type",
        "provider_ref",
        "status",
        "verified_at",
        "verified_by",
        "evidence_ref",
    ):
        assert required in columns, f"missing column: {required}"

    # PLAN §7.2 要求保存的全部字段。
    for required in (
        "invoice_code",
        "invoice_no",
        "invoice_date",
        "seller_tax_code",
        "buyer_tax_code",
        "amount_without_tax",
        "tax_amount",
        "amount_with_tax",
        "currency",
        "invoice_type",
        "duplicate_key",
        "duplicate_check_result",
        "provider_name",
        "error_code",
        "reason",
        "evidence_json",
        "verify_attempt_count",
        "daily_verify_count",
        "last_verify_date",
    ):
        assert required in columns, f"missing column: {required}"

    # 金额必须是 NUMERIC(18, 2)，绝不是浮点。
    for money_column in ("amount_without_tax", "tax_amount", "amount_with_tax"):
        rendered = str(columns[money_column]["type"]).upper()
        assert "NUMERIC" in rendered or "DECIMAL" in rendered, rendered
        assert "FLOAT" not in rendered and "DOUBLE" not in rendered and "REAL" not in rendered


def test_migration_0010_constraints(engine: Engine) -> None:
    """``duplicate_key`` 唯一 + 三类 CHECK 取值集合。"""
    _ensure_head(engine)
    inspector = inspect(engine)

    unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints(TABLE)
    }
    assert "uq_invoice_verifications_duplicate_key" in unique_names

    index_names = {index["name"] for index in inspector.get_indexes(TABLE)}
    assert "ix_invoice_verifications_invoice_no" in index_names

    with engine.connect() as connection:
        check_defs = connection.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE conrelid = to_regclass(:table) "
                "AND contype = 'c'"
            ),
            {"table": TABLE},
        ).all()
    definitions = {name: definition for name, definition in check_defs}

    for status in STATUS_VALUES:
        assert status in definitions["ck_invoice_verifications_status_allowed"]
    for provider_type in PROVIDER_TYPE_VALUES:
        assert provider_type in definitions["ck_invoice_verifications_provider_type_allowed"]
    for duplicate in DUPLICATE_CHECK_VALUES:
        assert (
            duplicate
            in definitions["ck_invoice_verifications_duplicate_check_result_allowed"]
        )

    # 外键：原件引用与操作人。
    foreign_keys = {
        fk["name"] for fk in inspector.get_foreign_keys(TABLE)
    }
    assert "fk_invoice_verifications_source_file_id_source_files" in foreign_keys
    assert "fk_invoice_verifications_verified_by_employees" in foreign_keys
