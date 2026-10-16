"""W1-26 受理解析流水线**落库**测试（跑在专用测试库 ``ajaimgr_test`` 上）。

覆盖 Issue #3 验收第 2/3/5 条的持久化与追溯：

* 解析结果写入 ``extraction_drafts``——逐字段值 + 逐字段置信度 + ``source_file_id``
  原件引用，``review_status`` 初始为 ``pending``；
* 关键字段缺失/低置信度时 ``intake_jobs.status='needs_review'``，齐全时 ``'parsed'``；
  解析失败时 ``'failed'`` 且**不建草稿**；
* **不写正式业务表**（``expense_claims`` / ``expense_lines`` 行数不变）；
* 追溯：草稿可经审计追溯到来源文件、解析器版本与确认人（审计 ``actor_id``）。

纯逻辑部分（接口/兜底/裁决/凭据 fail closed）在
``app/tests/intake/test_parsing_pipeline.py`` 中且无需数据库；本文件只补持久化路径。
"""

from __future__ import annotations

from collections.abc import Iterator
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
from app.expense.models import ExpenseClaim, ExpenseLine
from app.intake.models import ExtractionDraft, IntakeJob
from app.intake.parsing import InputKind, ManualEntryParser, ParseRequest, ParseResult
from app.intake.pipeline import confirm_draft, record_parse_draft, reject_draft

REPO_ROOT = Path(__file__).resolve().parents[3]

FORMAL_BUSINESS_MODELS = (ExpenseClaim, ExpenseLine)

#: A recognizable "attachment原文/识别全文" sample that must never reach audit/logs.
RAW_ATTACHMENT_TEXT = "发票号码 00001111 合计金额 123.45 附件原文"


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
    """删除本文件创建的业务行（先子后父）；审计只增不改，按资源 ID 过滤查询。"""
    session.execute(delete(ExtractionDraft))
    session.execute(delete(SourceFile))
    session.execute(delete(IntakeJob))
    session.execute(delete(Employee))
    session.execute(delete(Department))
    session.execute(delete(Company))


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        try:
            yield sess
        finally:
            sess.rollback()
            _cleanup(sess)
            sess.commit()


def _make_employee(session: Session, *, suffix: str) -> Employee:
    company = Company(name=f"W1-26 {suffix}")
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
    session.add(employee)
    session.commit()
    return employee


def _make_job(session: Session, employee: Employee, *, input_type: str) -> IntakeJob:
    job = IntakeJob(
        submitted_by=employee.id, channel_type="weixin", input_type=input_type
    )
    session.add(job)
    session.commit()
    return job


def _make_source(session: Session, employee: Employee, *, suffix: str) -> SourceFile:
    source = SourceFile(
        owner_id=employee.id,
        storage_key=f"ab/{suffix[:2]}/" + (suffix[0] * 64),
        sha256=suffix[0] * 64,
        mime_type="image/png",
        size_bytes=1024,
        original_name="receipt.png",
        scan_status="clean",
    )
    session.add(source)
    session.commit()
    return source


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _audit_events(session: Session, *, resource_id: str) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_id == resource_id)
            .order_by(AuditEvent.id)
        )
    )


def _manual_result(
    *, source_file_id: int, values: dict[str, object], confidence: dict[str, float] | None
) -> ParseResult:
    parser = ManualEntryParser(values=values, field_confidence=confidence)
    request = ParseRequest(
        input_kind=InputKind.IMAGE,
        business_type="expense_claim",
        source_file_id=source_file_id,
    )
    return parser.parse(request)


# --- 2. 解析结果写入草稿区 -----------------------------------------------------


def test_record_draft_persists_draft_with_confidence_and_source(
    session: Session,
) -> None:
    """成功解析：草稿落库，逐字段置信度 + 原件引用，job 置 parsed。"""
    employee = _make_employee(session, suffix="ok")
    job = _make_job(session, employee, input_type="image")
    source = _make_source(session, employee, suffix="aa")

    result = _manual_result(
        source_file_id=source.id,
        values={"amount": "123.45", "expense_date": "2026-10-01"},
        confidence=None,
    )
    job, draft = record_parse_draft(
        session,
        job=job,
        business_type="expense_claim",
        result=result,
        actor_id=employee.id,
    )
    session.commit()

    assert draft is not None
    reloaded = session.scalar(
        select(ExtractionDraft).where(ExtractionDraft.id == draft.id)
    )
    assert reloaded is not None
    assert reloaded.intake_job_id == job.id
    assert reloaded.source_file_id == source.id  # 原件引用
    assert reloaded.business_type == "expense_claim"
    assert reloaded.extracted_json == {
        "amount": "123.45",
        "expense_date": "2026-10-01",
    }
    # 逐字段置信度，键集合与提取字段一致
    assert set(reloaded.confidence_json) == set(reloaded.extracted_json)
    assert reloaded.review_status == "pending"  # 人工确认是唯一入口

    reloaded_job = session.scalar(select(IntakeJob).where(IntakeJob.id == job.id))
    assert reloaded_job is not None
    assert reloaded_job.status == "parsed"


def test_record_draft_writes_no_formal_business_rows(session: Session) -> None:
    """写草稿后正式业务表行数不变（禁止直接写正式字段）。"""
    employee = _make_employee(session, suffix="noformal")
    job = _make_job(session, employee, input_type="file")
    source = _make_source(session, employee, suffix="bb")

    before = {model: _count(session, model) for model in FORMAL_BUSINESS_MODELS}
    result = _manual_result(
        source_file_id=source.id,
        values={"amount": "9.99", "expense_date": "2026-10-02"},
        confidence=None,
    )
    record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    session.commit()
    after = {model: _count(session, model) for model in FORMAL_BUSINESS_MODELS}

    assert after == before


# --- 3. 状态裁决：低置信度 / 关键字段缺失 / 失败 -------------------------------


def test_low_confidence_marks_job_needs_review(session: Session) -> None:
    """低置信度字段 → needs_review，草稿仍 pending。"""
    employee = _make_employee(session, suffix="low")
    job = _make_job(session, employee, input_type="image")
    source = _make_source(session, employee, suffix="cc")

    result = _manual_result(
        source_file_id=source.id,
        values={"amount": "123.45", "expense_date": "2026-10-01"},
        confidence={"amount": 0.2},
    )
    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    session.commit()

    assert draft is not None
    assert draft.review_status == "pending"
    assert job.status == "needs_review"

    events = _audit_events(session, resource_id=str(draft.id))
    created = [e for e in events if e.action == "extraction_draft.created"]
    assert created, events
    assert created[0].details["low_confidence_fields"] == ["amount"]
    assert created[0].details["requires_human_review"] is True


def test_missing_key_field_marks_job_needs_review(session: Session) -> None:
    """关键字段缺失 → needs_review，缺失字段写入审计明细。"""
    employee = _make_employee(session, suffix="missing")
    job = _make_job(session, employee, input_type="image")
    source = _make_source(session, employee, suffix="dd")

    result = _manual_result(
        source_file_id=source.id,
        values={"amount": "123.45"},  # 缺 expense_date
        confidence=None,
    )
    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    session.commit()

    assert draft is not None
    assert job.status == "needs_review"
    created = _audit_events(session, resource_id=str(draft.id))
    assert created[0].details["missing_fields"] == ["expense_date"]


def test_failed_parse_writes_no_draft_and_marks_job_failed(
    session: Session,
) -> None:
    """解析失败：不建草稿，job 置 failed，写失败审计（含失败码）。"""
    employee = _make_employee(session, suffix="fail")
    job = _make_job(session, employee, input_type="voice")
    source = _make_source(session, employee, suffix="ee")

    failed = ParseResult.failed(
        input_kind=InputKind.VOICE,
        parser_name="stub_asr",
        parser_version="0.0-test",
        error="provider_error",
        source_file_id=source.id,
    )
    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=failed
    )
    session.commit()

    assert draft is None
    assert job.status == "failed"
    drafts = session.scalar(
        select(func.count())
        .select_from(ExtractionDraft)
        .where(ExtractionDraft.intake_job_id == job.id)
    )
    assert drafts == 0

    events = _audit_events(session, resource_id=str(job.id))
    assert [e.action for e in events] == ["intake_parse.failed"]
    assert events[0].details["error"] == "provider_error"


# --- 5. 追溯：来源文件 / 解析器版本 / 确认人 -----------------------------------


def test_confirmation_records_confirmer_and_parser_version(session: Session) -> None:
    """确认草稿记录确认人（审计 actor_id）与解析器版本；只改草稿状态。"""
    employee = _make_employee(session, suffix="confirm")
    confirmer = _make_employee(session, suffix="confirmer")
    job = _make_job(session, employee, input_type="image")
    source = _make_source(session, employee, suffix="ff")

    result = _manual_result(
        source_file_id=source.id,
        values={"amount": "123.45", "expense_date": "2026-10-01"},
        confidence=None,
    )
    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    assert draft is not None

    confirm_draft(session, draft=draft, confirmed_by=confirmer.id)
    session.commit()

    reloaded = session.scalar(
        select(ExtractionDraft).where(ExtractionDraft.id == draft.id)
    )
    assert reloaded is not None
    assert reloaded.review_status == "confirmed"

    events = _audit_events(session, resource_id=str(draft.id))
    created = [e for e in events if e.action == "extraction_draft.created"]
    confirmed = [e for e in events if e.action == "extraction_draft.confirmed"]
    assert created and confirmed
    # 解析器版本 + 来源文件在创建事件；确认人在确认事件（actor_id）。
    assert created[0].details["parser_version"] == "1"
    assert created[0].details["source_file_id"] == source.id
    assert confirmed[0].actor_id == confirmer.id


def test_reject_draft_records_operator(session: Session) -> None:
    """否决草稿：review_status=rejected，记录操作人。"""
    employee = _make_employee(session, suffix="reject")
    job = _make_job(session, employee, input_type="image")
    source = _make_source(session, employee, suffix="gg")

    result = _manual_result(
        source_file_id=source.id, values={"amount": "1.00"}, confidence=None
    )
    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    assert draft is not None
    reject_draft(session, draft=draft, rejected_by=employee.id)
    session.commit()

    assert draft.review_status == "rejected"
    events = _audit_events(session, resource_id=str(draft.id))
    rejected = [e for e in events if e.action == "extraction_draft.rejected"]
    assert rejected and rejected[0].actor_id == employee.id


def test_audit_details_never_contain_raw_attachment_text(session: Session) -> None:
    """审计明细只含字段名与标识，绝不含字段取值或识别原文。"""
    employee = _make_employee(session, suffix="redact")
    job = _make_job(session, employee, input_type="image")
    source = _make_source(session, employee, suffix="hh")

    result = _manual_result(
        source_file_id=source.id,
        values={"amount": "123.45", "raw_note": RAW_ATTACHMENT_TEXT},
        confidence=None,
    )
    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    session.commit()
    assert draft is not None

    events = _audit_events(session, resource_id=str(draft.id))
    rendered = " ".join(str(e.details) for e in events)
    assert RAW_ATTACHMENT_TEXT not in rendered
    assert "123.45" not in rendered
    # 字段名可以出现（用于追溯「哪些字段被提取」）。
    assert "raw_note" in rendered
