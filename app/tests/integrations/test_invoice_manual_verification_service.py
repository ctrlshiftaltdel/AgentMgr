"""人工辅助查验结果录入的服务层测试（W1-22，真实 PostgreSQL 测试库，绝不触网）。

覆盖任务验收标准（全部逐条对照）：

a. 无权限角色（如员工）录入 → 拒绝 + 拒绝审计，行内容不变；
b. 有权限但缺证据（缺失/空白）→ 拒绝且**不落任何状态变更**（不写审计、不改库）；
c. 合法录入（财务经办）→ 「查验通过」，``provider_type=manual_assisted``、
   ``verified_by``/``verified_at``/证据落库；成功审计存在且不含票号明文；
d. 幂等：同票再录不产生第二条记录；
e. 越权不可改他人录入（无权限重录 → 拒绝，行不变）；
f. 既有渠道结论「查验不通过」不可被录成「通过」；
g. provider 路径不回退：``verify_invoice(ManualAssistedProvider)`` 仍只产出
   「待人工确认」，且**绝不**触发录入审计（既有 W1-20 测试保留，不重复）。

审计事件跨测试累积（``audit_events`` 只增不改），因此本文件所有审计查询都按
``request_id`` / ``actor_id`` / ``resource_id`` 精确限定，做到顺序无关。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.db.models.organization import Employee
from app.identity.models import EmployeeRole, Role
from app.integrations.invoice_service import (
    MANUAL_DENY_ACTION,
    MANUAL_PROVIDER_NAME,
    MANUAL_PROVIDER_TYPE,
    MANUAL_VERIFY_ACTION,
    ManualVerificationConflictError,
    ManualVerificationDeniedError,
    ManualVerificationEvidenceError,
    record_manual_verification,
    verify_invoice,
)
from app.integrations.invoice_verification import (
    ManualAssistedProvider,
    ProviderType,
    VerificationStatus,
    compute_duplicate_key,
    evidence_ref_for,
)
from app.integrations.models import InvoiceVerification

from app.tests.integrations.invoice_fixtures import (
    FIXED_NOW,
    FIXED_TODAY,
    StubProvider,
    World,
    make_fields,
    make_result,
)

_EVIDENCE = "已在国家税务总局全国增值税发票查验平台核验：票面信息一致，查验通过。"
_UNAUTHORIZED_ERROR = "12345678"  # invoice_no 明文探针（不得出现在审计/消息里）


def _row_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(InvoiceVerification)) or 0


def _manual_events(
    session: Session,
    row: InvoiceVerification | None = None,
    *,
    request_id: str | None = None,
) -> list[AuditEvent]:
    """本文件写入的**成功**录入审计（按资源 ID / 请求 ID 精确限定，顺序无关）。"""
    stmt = select(AuditEvent).where(AuditEvent.action == MANUAL_VERIFY_ACTION)
    if row is not None:
        stmt = stmt.where(AuditEvent.resource_id == str(row.id))
    if request_id is not None:
        stmt = stmt.where(AuditEvent.request_id == request_id)
    return list(session.scalars(stmt.order_by(AuditEvent.id)))


def _manual_deny_events(
    session: Session,
    *,
    actor_id: int | None = None,
    request_id: str | None = None,
) -> list[AuditEvent]:
    """本文件写入的**拒绝**录入审计（按主体/请求 ID 精确限定）。"""
    stmt = select(AuditEvent).where(
        AuditEvent.resource_type == "invoice_verification",
        AuditEvent.action == MANUAL_DENY_ACTION,
    )
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if request_id is not None:
        stmt = stmt.where(AuditEvent.request_id == request_id)
    return list(session.scalars(stmt.order_by(AuditEvent.id)))


def _record(
    session: Session,
    world: World,
    *,
    fields=None,  # type: ignore[no-untyped-def]
    evidence: str | None = _EVIDENCE,
    source_file_id: int | None = None,
    principal=None,  # type: ignore[no-untyped-def]
    request_id: str | None = None,
) -> InvoiceVerification:
    return record_manual_verification(
        session,
        principal if principal is not None else world.principal(world.finance_operator),
        fields if fields is not None else make_fields(),
        evidence=evidence,
        source_file_id=source_file_id,
        request_id=request_id,
        now=FIXED_NOW,
    )


def _seed_row_in_status(
    session: Session,
    world: World,
    status: VerificationStatus,
    *,
    invoice_no: str,
) -> InvoiceVerification:
    """用既有 provider 流程把某张票的行推进到 ``status``（作为录入前状态）。"""
    fields = make_fields(invoice_no=invoice_no)
    principal = world.principal(world.finance_operator)
    if status is VerificationStatus.SUSPECTED_DUPLICATE:
        provider = ManualAssistedProvider()
        verify_invoice(
            session,
            principal,
            provider,
            fields,
            source_file_id=world.source_file_a.id,
            today=FIXED_TODAY,
            now=FIXED_NOW,
        )
        return verify_invoice(
            session,
            principal,
            provider,
            fields,
            source_file_id=world.source_file_b.id,
            today=FIXED_TODAY,
            now=FIXED_NOW,
        )
    if status is VerificationStatus.PENDING_MANUAL_CONFIRMATION:
        provider = ManualAssistedProvider()
    else:
        provider = StubProvider(
            results=[make_result(status)],
            _provider_type=ProviderType.AUTHORIZED_PROVIDER,
        )
    return verify_invoice(
        session, principal, provider, fields, today=FIXED_TODAY, now=FIXED_NOW
    )


# --- c. 合法录入：落库字段 + 成功审计 -----------------------------------------


def test_finance_operator_records_new_invoice_as_verified_pass(
    session: Session, world: World
) -> None:
    row = _record(
        session,
        world,
        evidence=f"  {_EVIDENCE}  ",  # 前后空白应被去除
        source_file_id=world.source_file_a.id,
    )
    session.refresh(row)

    fields = make_fields()
    assert row.status == VerificationStatus.VERIFIED_PASS.value
    assert row.provider_type == MANUAL_PROVIDER_TYPE == "manual_assisted"
    assert row.provider_name == MANUAL_PROVIDER_NAME == "manual_assisted"
    assert row.verified_by == world.finance_operator.id
    assert row.verified_at == FIXED_NOW
    assert row.duplicate_key == compute_duplicate_key(fields)
    assert row.duplicate_check_result == "unique"
    assert row.source_file_id == world.source_file_a.id
    # 录入不调用渠道：不计入渠道调用次数。
    assert row.verify_attempt_count == 0
    assert row.daily_verify_count == 0

    # 证据：结构化 JSON + 内容寻址引用。
    evidence = row.evidence_json
    assert evidence == {
        "channel": "manual_assisted",
        "note": _EVIDENCE,
        "recorded_by": world.finance_operator.id,
        "recorded_at": FIXED_NOW.isoformat(),
    }
    assert row.evidence_ref == evidence_ref_for(evidence)
    assert row.evidence_ref.startswith("sha256:")

    # 成功审计：动作、主体、资源、非敏感 details。
    events = _manual_events(session, row)
    assert len(events) == 1
    event = events[0]
    assert event.action == MANUAL_VERIFY_ACTION
    assert event.actor_id == world.finance_operator.id
    assert event.resource_type == "invoice_verification"
    assert event.resource_id == str(row.id)
    assert event.details["from_status"] is None
    assert event.details["to_status"] == VerificationStatus.VERIFIED_PASS.value
    assert event.details["provider_type"] == "manual_assisted"
    assert event.details["provider_name"] == "manual_assisted"
    assert event.details["has_evidence"] is True
    assert event.details["duplicate_key"] == row.duplicate_key
    # 显式断言：审计 details 不含票号明文（只允许摘要）。
    assert _UNAUTHORIZED_ERROR not in str(event.details)


# --- b. 缺证据拒绝：不落任何状态变更 -------------------------------------------


def test_recording_rejects_missing_or_blank_evidence(
    session: Session, world: World
) -> None:
    for index, bad_evidence in enumerate((None, "", "   ", "\t\n")):
        with pytest.raises(ManualVerificationEvidenceError):
            _record(
                session,
                world,
                evidence=bad_evidence,
                request_id=f"w1-22-svc-evidence-{index}",
            )

    # 不落任何状态变更：没有行、没有成功审计、也没有拒绝审计。
    assert _row_count(session) == 0
    for index in range(4):
        assert (
            _manual_events(session, request_id=f"w1-22-svc-evidence-{index}") == []
        )
        assert (
            _manual_deny_events(
                session, request_id=f"w1-22-svc-evidence-{index}"
            )
            == []
        )


# --- a. 无权限拒绝：403 语义 + 拒绝审计 + 行未变 --------------------------------


def test_recording_requires_invoice_verify_permission(
    session: Session, world: World
) -> None:
    outsider = world.principal(world.outsider)
    with pytest.raises(ManualVerificationDeniedError) as exc:
        _record(
            session, world, principal=outsider, request_id="w1-22-svc-deny-1"
        )
    assert exc.value.reason == "missing_permission"

    assert _row_count(session) == 0
    denials = _manual_deny_events(
        session, actor_id=world.outsider.id, request_id="w1-22-svc-deny-1"
    )
    assert len(denials) == 1
    denial = denials[0]
    assert denial.action == MANUAL_DENY_ACTION
    assert denial.resource_type == "invoice_verification"
    assert denial.resource_id is None
    assert denial.details == {
        "attempted_action": "manual_verify",
        "reason": "missing_permission",
    }
    assert _UNAUTHORIZED_ERROR not in str(denial.details)


# --- c/d. 已存在的「待人工确认」行在原地推进为「查验通过」 ---------------------


def test_recording_completes_pending_manual_row_in_place(
    session: Session, world: World
) -> None:
    fields = make_fields()
    pending = verify_invoice(
        session,
        world.principal(world.finance_operator),
        ManualAssistedProvider(),
        fields,
        today=FIXED_TODAY,
        now=FIXED_NOW,
    )
    assert pending.status == VerificationStatus.PENDING_MANUAL_CONFIRMATION.value
    assert pending.verified_at is None and pending.verified_by is None

    recorded = _record(session, world, fields=fields)
    assert recorded.id == pending.id
    assert _row_count(session) == 1
    assert recorded.status == VerificationStatus.VERIFIED_PASS.value
    assert recorded.verified_by == world.finance_operator.id
    assert recorded.verified_at == FIXED_NOW

    events = _manual_events(session, recorded)
    assert len(events) == 1
    assert events[0].details["from_status"] == "pending_manual_confirmation"
    assert events[0].details["to_status"] == "verified_pass"


def test_all_non_fail_statuses_reach_verified_pass(
    session: Session, world: World
) -> None:
    statuses = (
        VerificationStatus.PENDING_IDENTIFICATION,
        VerificationStatus.PENDING_MANUAL_CONFIRMATION,
        VerificationStatus.PENDING_VERIFICATION,
        VerificationStatus.UNABLE_TO_VERIFY,
        VerificationStatus.SUSPECTED_DUPLICATE,
    )
    for index, status in enumerate(statuses):
        invoice_no = f"8000000{index}"
        row = _seed_row_in_status(session, world, status, invoice_no=invoice_no)
        assert row.status == status.value, f"seed failed for {status}"

        recorded = _record(session, world, fields=make_fields(invoice_no=invoice_no))
        assert recorded.id == row.id
        assert recorded.status == VerificationStatus.VERIFIED_PASS.value
        assert recorded.provider_type == "manual_assisted"

    assert _row_count(session) == len(statuses)


# --- d. 幂等 + 重录审计（from/to 与操作人） ------------------------------------


def test_authorized_rerecord_is_idempotent_and_updates_operator(
    session: Session, world: World
) -> None:
    first = _record(session, world, request_id="w1-22-svc-rerecord-1")
    snapshot_ref = first.evidence_ref

    # 另一位同样持有 invoice/verify/company 的财务人员重录：仍是同一行。
    role = session.scalar(select(Role).where(Role.name == "finance_operator"))
    assert role is not None
    second_operator = Employee(
        company_id=world.company.id,
        department_id=world.department.id,
        employee_no="E-W1-22-finop2",
    )
    session.add(second_operator)
    session.flush()
    session.add(EmployeeRole(employee_id=second_operator.id, role_id=role.id))
    session.flush()

    second = _record(
        session,
        world,
        evidence="复核：结论一致（第二名经办）。",
        principal=world.principal(second_operator),
        request_id="w1-22-svc-rerecord-2",
    )
    assert second.id == first.id
    assert _row_count(session) == 1
    assert second.status == VerificationStatus.VERIFIED_PASS.value
    assert second.verified_by == second_operator.id
    assert second.evidence_ref != snapshot_ref  # 证据已更新
    assert second.evidence_json["note"] == "复核：结论一致（第二名经办）。"

    events = _manual_events(session, second)
    assert len(events) == 2
    assert events[0].actor_id == world.finance_operator.id
    assert events[1].actor_id == second_operator.id
    # 重录：审计记录 from/to 与操作人。
    assert events[1].details["from_status"] == "verified_pass"
    assert events[1].details["to_status"] == "verified_pass"
    assert events[1].details["duplicate_key"] == second.duplicate_key


# --- e. 越权不可改他人录入 -----------------------------------------------------


def test_unauthorized_rerecord_leaves_row_unchanged(
    session: Session, world: World
) -> None:
    row = _record(session, world)
    before = {
        "status": row.status,
        "verified_by": row.verified_by,
        "evidence_ref": row.evidence_ref,
        "evidence_json": dict(row.evidence_json),
    }

    with pytest.raises(ManualVerificationDeniedError):
        _record(
            session,
            world,
            principal=world.principal(world.outsider),
            evidence="越权重录",
            request_id="w1-22-svc-rerecord-deny",
        )

    session.refresh(row)
    assert row.status == before["status"]
    assert row.verified_by == before["verified_by"]
    assert row.evidence_ref == before["evidence_ref"]
    assert row.evidence_json == before["evidence_json"]
    assert _row_count(session) == 1
    denials = _manual_deny_events(
        session, actor_id=world.outsider.id, request_id="w1-22-svc-rerecord-deny"
    )
    assert len(denials) == 1
    assert denials[0].details["reason"] == "missing_permission"


# --- f. 「查验不通过」不可被录入覆盖 -------------------------------------------


def test_verified_fail_cannot_be_overwritten_to_pass(
    session: Session, world: World
) -> None:
    fields = make_fields()
    failed = verify_invoice(
        session,
        world.principal(world.finance_operator),
        StubProvider(
            results=[
                make_result(
                    VerificationStatus.VERIFIED_FAIL,
                    error_code="FailedOperation.InvoiceMismatch",
                )
            ],
            _provider_type=ProviderType.AUTHORIZED_PROVIDER,
        ),
        fields,
        today=FIXED_TODAY,
        now=FIXED_NOW,
    )
    assert failed.status == VerificationStatus.VERIFIED_FAIL.value
    before = {
        "status": failed.status,
        "verified_by": failed.verified_by,
        "verified_at": failed.verified_at,
        "evidence_ref": failed.evidence_ref,
        "verify_attempt_count": failed.verify_attempt_count,
        "error_code": failed.error_code,
    }

    with pytest.raises(ManualVerificationConflictError) as exc:
        _record(session, world, fields=fields, request_id="w1-22-svc-fail-1")
    assert exc.value.current_status == VerificationStatus.VERIFIED_FAIL.value
    assert exc.value.reason == "verified_fail_is_terminal"

    session.refresh(failed)
    assert failed.status == before["status"]
    assert failed.verified_by == before["verified_by"]
    assert failed.verified_at == before["verified_at"]
    assert failed.evidence_ref == before["evidence_ref"]
    assert failed.verify_attempt_count == before["verify_attempt_count"]
    assert failed.error_code == before["error_code"]
    assert _row_count(session) == 1

    denials = _manual_deny_events(session, request_id="w1-22-svc-fail-1")
    assert len(denials) == 1
    assert denials[0].resource_id == str(failed.id)
    assert denials[0].details == {
        "attempted_action": "manual_verify",
        "reason": "verified_fail_is_terminal",
    }
    assert _UNAUTHORIZED_ERROR not in str(denials[0].details)


# --- g. provider 路径不回退：绝不触发录入动作 ----------------------------------


def test_provider_path_never_triggers_the_recording_action(
    session: Session, world: World
) -> None:
    row = verify_invoice(
        session,
        world.principal(world.finance_operator),
        ManualAssistedProvider(),
        make_fields(),
        today=FIXED_TODAY,
        now=FIXED_NOW,
    )
    # 人工辅助渠道永远只产出「待人工确认」，也绝不写录入/录入拒绝审计。
    assert row.status == VerificationStatus.PENDING_MANUAL_CONFIRMATION.value
    assert row.verified_at is None and row.verified_by is None
    assert _manual_events(session, row) == []
    assert _manual_deny_events(session, actor_id=world.finance_operator.id) == []


# --- 显式动作约定：录入审计只由 record_manual_verification 产生 ----------------


def test_manual_recording_audit_actors_are_real_people(session: Session, world: World) -> None:
    """审计 ``actor_id`` 必须是实际人员 ID（PLAN §9），且与 principal 一致。"""
    row = _record(session, world)
    events = _manual_events(session, row)
    assert [event.actor_id for event in events] == [world.finance_operator.id]
    assert row.verified_by == world.finance_operator.id
