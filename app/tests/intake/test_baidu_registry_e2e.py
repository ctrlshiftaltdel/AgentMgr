"""Issue #45 端到端集成测试：已存源文件 → registry 解析 → 草稿 → 人工确认。

链路（**全桩传输、零真实网络、零真实凭据**）：

1. 用 ``app.documents.storage.store_file`` 把票据图片写入临时存储根，并落一行
   ``source_files``（内容寻址 ``storage_key`` + ``sha256`` + ``clean`` 扫描状态）；
2. 用生产级 ``SourceFileReader``（``app.intake.source_reader``）装配默认
   ``ParserRegistry``（``app.intake.assembly.build_default_parser_registry``）；
3. 解析走**注入的桩传输**（百度 token + ``multiple_invoice`` 响应均为本地构造）；
4. ``record_parse_draft`` 写草稿区、``confirm_draft`` 记录确认人。

覆盖的四类语义：

| 场景 | 期望 |
|---|---|
| 解析成功且字段置信度达标 | ``IntakeJob.status='parsed'``、草稿 ``pending`` → 可确认 |
| 低置信度字段 | ``'needs_review'``（草稿仍 ``pending``，人工确认仍可完成） |
| 供应商失败（业务错误码 / 传输异常） | 机器可读 ``error='provider_error'``、``'failed'``、**不建草稿** |
| 凭据缺失 | 不注册解析器、回退 ``ManualEntryParser``，链路仍可完成 |

另含两条 fail-closed 断言：生产 ``SourceReader`` 读取失败（blob 缺失）被收敛为
``provider_error`` 而不是异常；``scan_status != 'clean'`` 的原件在**取 token 之前**
即被拒绝。

本文件需要 PostgreSQL（``ajaimgr_test``）；纯逻辑部分在
``app/tests/intake/test_intake_assembly.py`` 与 ``test_intake_source_reader.py``，
无需数据库。
"""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.db.models.organization import Company, Department, Employee
from app.documents.models import SourceFile
from app.documents.storage import store_file
from app.expense.models import ExpenseClaim, ExpenseLine
from app.integrations.baidu_invoice_ocr import (
    API_KEY_ENV,
    BAIDU_OCR_PARSER_NAME,
    SECRET_KEY_ENV,
    HttpResponse,
    TransportError,
)
from app.intake.assembly import (
    MISSING_CREDENTIAL,
    build_default_parser_registry,
)
from app.intake.models import ExtractionDraft, IntakeJob
from app.intake.parsing import InputKind, ManualEntryParser, ParseRequest
from app.intake.pipeline import confirm_draft, record_parse_draft
from app.intake.source_reader import SourceFileReader

REPO_ROOT = Path(__file__).resolve().parents[3]

API_KEY = "example-bd-ocr-api-key-0001"
SECRET_KEY = "example-bd-ocr-secret-key-9999"
ENV = {API_KEY_ENV: API_KEY, SECRET_KEY_ENV: SECRET_KEY}
TOKEN = "24.example-token.2592000.1763000000.282335-00000001"

FORMAL_BUSINESS_MODELS = (ExpenseClaim, ExpenseLine)

#: 本任务范围内**不含**「供应商字段名 → 业务关键字段」的映射（属产品决策）。百度
#: 适配器产出的开票日期键是 ``invoice_date``，而 ``BUSINESS_REQUIRED_FIELDS`` 的
#: ``expense_claim`` 登记的是 ``amount`` + ``expense_date``；因此本文件在需要
#: 「成功」裁决时显式传入 ``required_fields``，不改动裁决与字段映射逻辑。
E2E_REQUIRED_FIELDS = ("amount", "invoice_date")


# --- 夹具与辅助 ---------------------------------------------------------------


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


@pytest.fixture(scope="module", autouse=True)
def _schema(test_db_url: str) -> None:
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
    company = Company(name=f"E2E-45 {suffix}")
    session.add(company)
    session.flush()
    department = Department(company_id=company.id, name=f"Dept {suffix}")
    session.add(department)
    session.flush()
    employee = Employee(
        company_id=company.id,
        department_id=department.id,
        employee_no=f"E2E-{suffix}",
    )
    session.add(employee)
    session.commit()
    return employee


def _make_job(session: Session, employee: Employee) -> IntakeJob:
    job = IntakeJob(
        submitted_by=employee.id, channel_type="weixin", input_type="image"
    )
    session.add(job)
    session.commit()
    return job


def _png(width: int = 100, height: int = 100) -> bytes:
    """构造仅含签名 + IHDR 的 PNG（足够魔数嗅探与像素预检，非可解码图片）。"""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = (
        struct.pack(">I", len(ihdr_data))
        + b"IHDR"
        + ihdr_data
        + b"\x00\x00\x00\x00"
    )
    return signature + ihdr


def _stored_source(
    session: Session,
    employee: Employee,
    *,
    tmp_path: Path,
    content: bytes | None = None,
    scan_status: str = "clean",
    sha256: str | None = None,
    storage_key: str | None = None,
) -> SourceFile:
    """写入真实 blob（临时存储根）并落一行 ``source_files``。"""
    payload = _png() if content is None else content
    stored = store_file(
        tmp_path, owner_id=employee.id, content=payload, declared_name="receipt.png"
    )
    source = SourceFile(
        owner_id=employee.id,
        storage_key=stored.storage_key if storage_key is None else storage_key,
        sha256=stored.sha256 if sha256 is None else sha256,
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        original_name="receipt.png",
        scan_status=scan_status,
    )
    session.add(source)
    session.commit()
    return source


def _registry(
    session: Session,
    tmp_path: Path,
    transport: Any,
    *,
    env: Mapping[str, str] | None = None,
) -> Any:
    """按生产装配点构造 registry（原件读取走生产 ``SourceFileReader``）。"""
    return build_default_parser_registry(
        source_reader=SourceFileReader(session=session, root=tmp_path),
        env=ENV if env is None else env,
        transport=transport,
    )


def _request(source_file_id: int) -> ParseRequest:
    return ParseRequest(
        input_kind=InputKind.IMAGE,
        business_type="expense_claim",
        source_file_id=source_file_id,
    )


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _audit_events(
    session: Session, *, resource_type: str, resource_id: str
) -> list[AuditEvent]:
    """按资源类型 + ID 取审计事件（审计只增不改，跨测试残留，必须双条件过滤）。"""
    return list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == resource_type,
                AuditEvent.resource_id == resource_id,
            )
            .order_by(AuditEvent.id)
        )
    )


# --- 桩传输与响应 -------------------------------------------------------------


@dataclass
class StubTransport:
    """按 URL 区分 token 与识别的桩传输（**绝不触网**）。"""

    ocr_results: list[object] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        self.calls.append({"url": url, "headers": dict(headers), "body": body})
        if "oauth" in url:
            payload = {"access_token": TOKEN, "expires_in": 2_592_000}
            return HttpResponse(200, json.dumps(payload).encode("utf-8"))
        if not self.ocr_results:
            raise AssertionError("stub transport has no queued OCR response")
        item = (
            self.ocr_results[0]
            if len(self.ocr_results) == 1
            else self.ocr_results.pop(0)
        )
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResponse)
        return item

    @property
    def ocr_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "oauth" not in call["url"]]


def _vat_response(*, amount_confidence: float = 0.98) -> HttpResponse:
    """一份 VAT 票据识别响应：金额取价税合计（``AmountInFiguers``）。"""

    def field_value(word: str, confidence: float) -> list[dict[str, object]]:
        return [
            {"word": word, "probability": {"average": 0.99, "min": confidence}}
        ]

    body = {
        "words_result_num": 1,
        "log_id": "1394226734160674816",
        "words_result": [
            {
                "type": "vat_invoice",
                "probability": 0.99,
                "result": {
                    "InvoiceCode": field_value("011002100311", 0.99),
                    "InvoiceNum": field_value("12345678", 0.99),
                    "InvoiceDate": field_value("2019年08月28日", 0.98),
                    "TotalAmount": field_value("100.00", 0.99),
                    "AmountInFiguers": field_value("106.00", amount_confidence),
                    "SellerName": field_value("上海示例商贸有限公司", 0.95),
                },
            }
        ],
    }
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


def _error_response(code: object) -> HttpResponse:
    body = {"error_code": code, "error_msg": "provider says no"}
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


# --- 1. 成功 → parsed → 确认 ---------------------------------------------------


def test_success_path_records_parsed_draft_and_confirms(
    session: Session, tmp_path: Path
) -> None:
    """成功：解析器读到真实存储字节，草稿落库为 ``parsed``，人工确认后 ``confirmed``。"""
    employee = _make_employee(session, suffix="ok")
    job = _make_job(session, employee)
    content = _png()
    source = _stored_source(session, employee, tmp_path=tmp_path, content=content)

    transport = StubTransport(ocr_results=[_vat_response()])
    registry = _registry(session, tmp_path, transport)

    parser = registry.resolve(InputKind.IMAGE)
    assert parser is not None
    assert parser.parser_name == BAIDU_OCR_PARSER_NAME

    result = parser.parse(_request(source.id))
    assert result.ok, result.error
    assert result.fields["amount"] == "106.00"  # 价税合计
    assert result.fields["invoice_date"] == "2019-08-28"  # 中文日期 → ISO
    assert set(result.fields) == set(result.field_confidence)

    # 送往桩的请求体确实是**存储中的字节**（证明生产 SourceReader 生效）。
    assert len(transport.ocr_calls) == 1
    form = parse_qs(transport.ocr_calls[0]["body"].decode("utf-8"))
    assert base64.b64decode(form["image"][0]) == content
    assert form["probability"] == ["true"]

    before = {model: _count(session, model) for model in FORMAL_BUSINESS_MODELS}
    job, draft = record_parse_draft(
        session,
        job=job,
        business_type="expense_claim",
        result=result,
        required_fields=E2E_REQUIRED_FIELDS,
    )
    assert draft is not None
    assert job.status == "parsed"
    assert draft.review_status == "pending"  # 人工确认是进入正式记录的唯一路径
    assert draft.source_file_id == source.id  # 原件引用，原件不被覆盖
    assert draft.extracted_json["amount"] == "106.00"
    assert set(draft.confidence_json) == set(draft.extracted_json)

    confirm_draft(session, draft=draft, confirmed_by=employee.id)
    session.commit()

    reloaded = session.scalar(
        select(ExtractionDraft).where(ExtractionDraft.id == draft.id)
    )
    assert reloaded is not None
    assert reloaded.review_status == "confirmed"

    events = _audit_events(
        session, resource_type="extraction_draft", resource_id=str(draft.id)
    )
    created = [e for e in events if e.action == "extraction_draft.created"]
    confirmed = [e for e in events if e.action == "extraction_draft.confirmed"]
    assert created and confirmed
    assert created[0].details["parser_name"] == BAIDU_OCR_PARSER_NAME
    assert created[0].details["parser_version"] == "1"
    assert created[0].details["source_file_id"] == source.id
    assert confirmed[0].actor_id == employee.id

    # 只写草稿区：正式业务表行数不变。
    after = {model: _count(session, model) for model in FORMAL_BUSINESS_MODELS}
    assert after == before


# --- 2. 低置信度 → needs_review → 仍可确认 -------------------------------------


def test_low_confidence_path_marks_needs_review_then_confirms(
    session: Session, tmp_path: Path
) -> None:
    """低置信度：``needs_review`` + 草稿 ``pending``，人工确认后仍可 ``confirmed``。"""
    employee = _make_employee(session, suffix="low")
    job = _make_job(session, employee)
    source = _stored_source(session, employee, tmp_path=tmp_path)

    transport = StubTransport(ocr_results=[_vat_response(amount_confidence=0.4)])
    registry = _registry(session, tmp_path, transport)
    parser = registry.resolve(InputKind.IMAGE)
    assert parser is not None

    result = parser.parse(_request(source.id))
    assert result.ok
    assert result.field_confidence["amount"] == pytest.approx(0.4)

    job, draft = record_parse_draft(
        session,
        job=job,
        business_type="expense_claim",
        result=result,
        required_fields=E2E_REQUIRED_FIELDS,
    )
    assert draft is not None
    assert job.status == "needs_review"
    assert draft.review_status == "pending"

    created = [
        event
        for event in _audit_events(
            session, resource_type="extraction_draft", resource_id=str(draft.id)
        )
        if event.action == "extraction_draft.created"
    ]
    assert created
    assert created[0].details["low_confidence_fields"] == ["amount"]
    assert created[0].details["requires_human_review"] is True

    confirm_draft(session, draft=draft, confirmed_by=employee.id)
    session.commit()
    assert draft.review_status == "confirmed"


# --- 3. 供应商失败 → 机器可读失败码、不建草稿 ---------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_error_response("17"), id="business-error-code"),
        pytest.param(TransportError("boom-secret-transport-error"), id="transport"),
    ],
)
def test_provider_failure_is_machine_readable_and_writes_no_draft(
    session: Session, tmp_path: Path, failure: object
) -> None:
    """供应商失败：失败码稳定、无草稿、``failed``，且不泄漏响应体/异常原文。"""
    employee = _make_employee(session, suffix="fail")
    job = _make_job(session, employee)
    source = _stored_source(session, employee, tmp_path=tmp_path)

    transport = StubTransport(ocr_results=[failure])
    registry = _registry(session, tmp_path, transport)
    parser = registry.resolve(InputKind.IMAGE)
    assert parser is not None

    result = parser.parse(_request(source.id))
    assert not result.ok
    assert result.error == "provider_error"  # 机器可读、供应商无关
    assert dict(result.fields) == {}

    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
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

    events = _audit_events(
        session, resource_type="intake_job", resource_id=str(job.id)
    )
    assert [event.action for event in events] == ["intake_parse.failed"]
    rendered = json.dumps(events[0].details, ensure_ascii=False)
    assert events[0].details["error"] == "provider_error"
    # 供应商原始信息不进审计（可能是响应体或异常原文）。
    assert "provider says no" not in rendered
    assert "boom-secret-transport-error" not in rendered
    assert "quota_daily_exceeded" not in rendered


# --- 4. 凭据缺失 → 回退人工录入，链路仍可完成 ----------------------------------


def test_missing_credentials_falls_back_to_manual_entry(
    session: Session, tmp_path: Path
) -> None:
    """无凭据：不注册外部解析器（返回值带非敏感说明），回退人工录入后草稿仍可确认。"""
    employee = _make_employee(session, suffix="nokey")
    job = _make_job(session, employee)
    source = _stored_source(session, employee, tmp_path=tmp_path)

    transport = StubTransport(ocr_results=[_vat_response()])
    registry = _registry(session, tmp_path, transport, env={})

    assert registry.registered_kinds == frozenset()
    assert registry.degraded is True
    assert registry.skipped_parsers == (BAIDU_OCR_PARSER_NAME,)
    note = registry.notes[0]
    assert note.reason == MISSING_CREDENTIAL
    assert note.env_vars == (API_KEY_ENV, SECRET_KEY_ENV)  # 只有变量名
    assert API_KEY not in note.detail
    assert registry.resolve(InputKind.IMAGE) is None
    assert transport.calls == []  # 未注册 → 不发任何请求

    manual = ManualEntryParser(
        values={"amount": "88.00", "expense_date": "2026-10-01"}
    )
    result = manual.parse(_request(source.id))
    assert result.ok
    assert result.parser_name == "manual_entry"

    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    assert draft is not None
    assert job.status == "parsed"  # 关键字段齐全，人工录入置信度 1.0

    confirm_draft(session, draft=draft, confirmed_by=employee.id)
    session.commit()
    assert draft.review_status == "confirmed"


# --- 5/6. fail closed：读取失败与非 clean 原件 ---------------------------------


def test_source_reader_failure_is_collapsed_to_provider_error(
    session: Session, tmp_path: Path
) -> None:
    """生产 ``SourceReader`` 读取失败（blob 缺失）→ ``provider_error``，不抛异常。"""
    employee = _make_employee(session, suffix="noblob")
    job = _make_job(session, employee)
    source = _stored_source(session, employee, tmp_path=tmp_path)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.unlink()  # 行还在，blob 已被清掉

    transport = StubTransport(ocr_results=[_vat_response()])
    registry = _registry(session, tmp_path, transport)
    parser = registry.resolve(InputKind.IMAGE)
    assert parser is not None

    result = parser.parse(_request(source.id))
    assert not result.ok
    assert result.error == "provider_error"
    assert transport.ocr_calls == []  # 未拿到原件 → 不发识别请求

    job, draft = record_parse_draft(
        session, job=job, business_type="expense_claim", result=result
    )
    session.commit()
    assert draft is None
    assert job.status == "failed"


def test_non_clean_source_is_rejected_before_any_request(
    session: Session, tmp_path: Path
) -> None:
    """``scan_status != 'clean'``：发请求前拒绝（连 token 都不取）。"""
    employee = _make_employee(session, suffix="dirty")
    _make_job(session, employee)
    source = _stored_source(
        session, employee, tmp_path=tmp_path, scan_status="pending"
    )

    transport = StubTransport(ocr_results=[_vat_response()])
    registry = _registry(session, tmp_path, transport)
    parser = registry.resolve(InputKind.IMAGE)
    assert parser is not None

    result = parser.parse(_request(source.id))
    assert not result.ok
    assert result.error == "provider_error"
    assert transport.calls == []
