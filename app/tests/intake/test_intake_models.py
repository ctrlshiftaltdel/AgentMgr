"""W1-9 受理/源文件/解析草稿模型测试（跑在专用测试库 ``ajaimgr_test`` 上）。

覆盖任务第五步第 6–7 项，对齐 ``templates/PLAN.md`` §4.2/§8 与
``docs/research/W0-5-security-compliance.md`` §4.3：

6. ``intake_job`` + ``source_file`` + ``extraction_draft`` 关联可写入，``draft``
   引用 ``source_file_id``；且断言**未向 ``expense_claims`` 等正式业务表写入任何
   行**（解析结果只进草稿区，PLAN §4.2 / §11「W1-4 不得直接将解析结果写入正式
   报销/账务字段」）；
7. 三张表约束：非法 ``input_type`` / ``status`` / ``scan_status``（及
   ``review_status``）被 CHECK 拒绝。

额外验证任务显式要求的两点：``source_files.storage_key`` **不设唯一约束**
（同内容多上传共享 blob，行按上传各存一份），以及 ``extraction_drafts`` 随
``intake_jobs`` ``ON DELETE CASCADE``。

基础数据（company/department/employee）先提交，使后续 ``rollback`` 只撤销出错的
语句；每个断言核对错误信息里的约束名，证明拒绝来自目标 CHECK。session fixture
在结束时按依赖逆序清理，不留残余数据。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.documents.models import SourceFile
from app.expense.models import ExpenseClaim, ExpenseLine
from app.intake.models import ExtractionDraft, IntakeJob

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Formal business tables that the intake pipeline must never write into.
FORMAL_BUSINESS_MODELS = (ExpenseClaim, ExpenseLine)


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
    """确保测试库已迁移到 head（含 0008 intake/source_files）。"""
    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    eng = create_engine(test_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _cleanup(session: Session) -> None:
    """删除这些测试创建的全部业务行（先子后父）。"""
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
    """Create and commit a company/department/employee triple."""
    company = Company(name=f"W1-9 {suffix}")
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


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --- 6. 关联可写入 + 不写正式业务表 -------------------------------------------


def test_intake_chain_persists_and_links(session: Session) -> None:
    """intake_job + source_file + extraction_draft 关联可写入并互相引用。"""
    employee = _make_employee(session, suffix="chain")

    job = IntakeJob(
        submitted_by=employee.id,
        channel_type="weixin",
        input_type="file",
        status="received",
    )
    session.add(job)
    session.flush()

    source = SourceFile(
        owner_id=employee.id,
        storage_key="ab/cd/" + "a" * 64,
        sha256="a" * 64,
        mime_type="application/pdf",
        size_bytes=1234,
        original_name="invoice.pdf",
        scan_status="clean",
    )
    session.add(source)
    session.flush()

    draft = ExtractionDraft(
        intake_job_id=job.id,
        source_file_id=source.id,
        business_type="expense_claim",
        extracted_json={"amount": "123.45", "invoice_no": "INV-1"},
        confidence_json={"amount": 0.98},
        review_status="pending",
    )
    session.add(draft)
    session.commit()

    # Reload from the database (post-commit expiry triggers real queries).
    reloaded = session.scalar(
        select(ExtractionDraft).where(ExtractionDraft.id == draft.id)
    )
    assert reloaded is not None
    assert reloaded.intake_job_id == job.id
    assert reloaded.source_file_id == source.id
    assert reloaded.business_type == "expense_claim"
    assert reloaded.extracted_json["amount"] == "123.45"
    assert reloaded.confidence_json["amount"] == 0.98
    assert reloaded.review_status == "pending"

    # The job is reachable and carries the default channel/status semantics.
    reloaded_job = session.scalar(select(IntakeJob).where(IntakeJob.id == job.id))
    assert reloaded_job is not None
    assert reloaded_job.submitted_by == employee.id
    assert reloaded_job.received_at is not None


def test_draft_json_defaults_to_empty_object(session: Session) -> None:
    """extracted_json / confidence_json 默认 '{}'::jsonb。"""
    employee = _make_employee(session, suffix="defaults")
    job = IntakeJob(submitted_by=employee.id, input_type="text")
    session.add(job)
    session.flush()
    draft = ExtractionDraft(intake_job_id=job.id, business_type="expense_claim")
    session.add(draft)
    session.commit()

    reloaded = session.scalar(
        select(ExtractionDraft).where(ExtractionDraft.id == draft.id)
    )
    assert reloaded is not None
    assert reloaded.extracted_json == {}
    assert reloaded.confidence_json == {}
    assert reloaded.review_status == "pending"
    assert reloaded.source_file_id is None  # source reference is optional


def test_intake_pipeline_writes_no_formal_business_rows(session: Session) -> None:
    """写入受理/源文件/草稿后，正式业务表行数不变（delta 0）。"""
    employee = _make_employee(session, suffix="nofromal")

    before = {model: _count(session, model) for model in FORMAL_BUSINESS_MODELS}

    job = IntakeJob(submitted_by=employee.id, input_type="spreadsheet")
    session.add(job)
    session.flush()
    source = SourceFile(
        owner_id=employee.id,
        storage_key="ef/01/" + "b" * 64,
        sha256="b" * 64,
        mime_type="application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet",
        size_bytes=99,
        scan_status="pending",
    )
    session.add(source)
    session.flush()
    session.add(
        ExtractionDraft(
            intake_job_id=job.id,
            source_file_id=source.id,
            business_type="expense_claim",
        )
    )
    session.commit()

    after = {model: _count(session, model) for model in FORMAL_BUSINESS_MODELS}
    assert after == before, (before, after)
    # And explicitly: no expense claim / line was created.
    assert _count(session, ExpenseClaim) == before[ExpenseClaim]
    assert _count(session, ExpenseLine) == before[ExpenseLine]
    # The intake rows themselves did land.
    assert _count(session, IntakeJob) >= 1
    assert _count(session, SourceFile) >= 1


def test_source_file_storage_key_is_not_unique(session: Session) -> None:
    """同一 storage_key 允许多行（内容寻址：物理 blob 共享，行按上传各存一份）。"""
    employee = _make_employee(session, suffix="dupkey")
    key = "12/34/" + "c" * 64
    session.add_all(
        [
            SourceFile(
                owner_id=employee.id,
                storage_key=key,
                sha256="c" * 64,
                mime_type="application/pdf",
                size_bytes=10,
            ),
            SourceFile(
                owner_id=employee.id,
                storage_key=key,
                sha256="c" * 64,
                mime_type="application/pdf",
                size_bytes=10,
            ),
        ]
    )
    session.commit()  # must not raise: no unique constraint on storage_key

    count = session.scalar(
        select(func.count())
        .select_from(SourceFile)
        .where(SourceFile.storage_key == key)
    )
    assert count == 2


def test_draft_cascades_on_job_delete(session: Session) -> None:
    """删除 intake_job 级联删除其 extraction_drafts。"""
    employee = _make_employee(session, suffix="cascade")
    job = IntakeJob(submitted_by=employee.id, input_type="image")
    session.add(job)
    session.flush()
    session.add(ExtractionDraft(intake_job_id=job.id, business_type="expense_claim"))
    session.commit()

    session.execute(delete(IntakeJob).where(IntakeJob.id == job.id))
    session.commit()

    remaining = session.scalar(
        select(func.count())
        .select_from(ExtractionDraft)
        .where(ExtractionDraft.intake_job_id == job.id)
    )
    assert remaining == 0


# --- 7. CHECK 约束 -------------------------------------------------------------


def test_check_constraints_reject_invalid_values(session: Session) -> None:
    """非法 input_type / status / scan_status / review_status 被 CHECK 拒绝。"""
    employee = _make_employee(session, suffix="checks")
    d64 = "d" * 64
    e64 = "e" * 64

    # intake_jobs.input_type
    with pytest.raises(IntegrityError) as input_err:
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO intake_jobs (submitted_by, input_type) "
                    f"VALUES ({employee.id}, 'bogus')"
                )
            )
    assert "ck_intake_jobs_input_type_allowed" in str(input_err.value)

    # intake_jobs.status
    with pytest.raises(IntegrityError) as status_err:
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO intake_jobs (submitted_by, input_type, status) "
                    f"VALUES ({employee.id}, 'file', 'bogus')"
                )
            )
    assert "ck_intake_jobs_status_allowed" in str(status_err.value)

    # source_files.scan_status
    with pytest.raises(IntegrityError) as scan_err:
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO source_files "
                    "(owner_id, storage_key, sha256, mime_type, size_bytes, "
                    "scan_status) "
                    f"VALUES ({employee.id}, 'aa/bb/{d64}', '{d64}', "
                    "'application/pdf', 1, 'bogus')"
                )
            )
    assert "ck_source_files_scan_status_allowed" in str(scan_err.value)

    # source_files.size_bytes >= 0
    with pytest.raises(IntegrityError) as size_err:
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO source_files "
                    "(owner_id, storage_key, sha256, mime_type, size_bytes) "
                    f"VALUES ({employee.id}, 'aa/bb/{e64}', '{e64}', "
                    "'application/pdf', -1)"
                )
            )
    assert "ck_source_files_size_bytes_non_negative" in str(size_err.value)

    # extraction_drafts.review_status（需要合法 intake_job_id）
    job = IntakeJob(submitted_by=employee.id, input_type="file")
    session.add(job)
    session.flush()
    with pytest.raises(IntegrityError) as review_err:
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO extraction_drafts "
                    "(intake_job_id, business_type, review_status) "
                    f"VALUES ({job.id}, 'expense_claim', 'bogus')"
                )
            )
    assert "ck_extraction_drafts_review_status_allowed" in str(review_err.value)
