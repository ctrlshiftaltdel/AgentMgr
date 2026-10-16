"""发票查验服务层测试（真实 PostgreSQL 测试库 + 本地桩渠道，绝不触网）。

覆盖 W1-20 第三步与第四步的硬性业务规则：

1. 只有被认可渠道能进入「查验通过」；``manual_assisted`` 与未识别来源一律不能；
2. 「查验不通过」与「无法查验/接口异常」严格区分、互不覆盖；
3. 幂等（同一张票重复查验不产生第二条记录）与当日限次（超过 5 次当日不可再查）；
4. 审计与证据不含凭据、税号、票号明文。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.integrations.invoice_verification import (
    DAILY_VERIFY_LIMIT,
    InvoiceFace,
    ManualAssistedProvider,
    ProviderType,
    VerificationStatus,
    compute_duplicate_key,
)
from app.integrations.invoice_service import (
    DENY_ACTION,
    DUPLICATE_SUSPECTED,
    VERIFY_ACTION,
    DailyVerifyLimitExceeded,
    InvoiceVerificationDeniedError,
    UnrecognizedProviderError,
    classify_duplicate,
    verify_invoice,
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

FACE = InvoiceFace(
    invoice_no="12345678",
    invoice_code="011002000211",
    seller_tax_code="91310000MA1FL0XXXX",
    buyer_tax_code="91320000MA1FL0YYYY",
    amount_without_tax=Decimal("100.00"),
    amount_with_tax=Decimal("113.00"),
)


def _rows(session: Session) -> list[InvoiceVerification]:
    return list(session.scalars(select(InvoiceVerification).order_by(InvoiceVerification.id)))


def _row_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(InvoiceVerification)) or 0


def _audit_events(session: Session, row: InvoiceVerification) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == "invoice_verification",
                AuditEvent.resource_id == str(row.id),
            )
            .order_by(AuditEvent.id)
        )
    )


def _deny_events(session: Session) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == "invoice_verification",
                AuditEvent.action == DENY_ACTION,
            )
            .order_by(AuditEvent.id)
        )
    )


def _verify(session, world, provider, *, source_file_id=None, fields=None, principal=None):
    return verify_invoice(
        session,
        principal if principal is not None else world.principal(world.finance_operator),
        provider,
        fields if fields is not None else make_fields(),
        source_file_id=source_file_id,
        today=FIXED_TODAY,
        now=FIXED_NOW,
    )


# --- 1. 只有被认可渠道能进入「查验通过」 ---------------------------------------


def test_authorized_provider_can_reach_verified_pass(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE, provider_ref="req-1")],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)

    assert row.status == VerificationStatus.VERIFIED_PASS.value
    assert row.provider_type == "authorized_provider"
    assert row.provider_ref == "req-1"
    assert row.invoice_no == "12345678"
    assert row.invoice_code == "011002000211"
    assert row.seller_tax_code == "91310000MA1FL0XXXX"
    assert str(row.amount_without_tax) == "100.00"
    assert str(row.amount_with_tax) == "113.00"
    assert row.currency == "CNY"
    assert row.invoice_type == "01"
    assert row.verified_by == world.finance_operator.id
    assert row.verified_at is not None
    assert row.duplicate_key == compute_duplicate_key(make_fields())
    assert row.duplicate_check_result == "unique"
    assert row.verify_attempt_count == 1
    assert row.daily_verify_count == 1
    assert row.last_verify_date == FIXED_TODAY


def test_manual_assisted_provider_never_reaches_verified_pass(
    session: Session, world: World
) -> None:
    row = _verify(
        session, world, ManualAssistedProvider(), source_file_id=world.source_file_a.id
    )
    assert row.status == VerificationStatus.PENDING_MANUAL_CONFIRMATION.value
    assert row.status != VerificationStatus.VERIFIED_PASS.value
    # 未取得渠道结论：不记查验时间/操作人。
    assert row.verified_at is None
    assert row.verified_by is None


def test_manual_provider_claiming_pass_is_downgraded(session: Session, world: World) -> None:
    """即便人工辅助渠道「自称」通过，服务层也必须降级为待人工确认。"""
    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE)],
        _provider_type=ProviderType.MANUAL_ASSISTED,
    )
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert row.status == VerificationStatus.PENDING_MANUAL_CONFIRMATION.value
    assert row.status != VerificationStatus.VERIFIED_PASS.value


def test_unrecognized_provider_is_rejected(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE)],
        _provider_type="wechat_scrape",
    )
    with pytest.raises(UnrecognizedProviderError) as exc:
        _verify(session, world, provider)
    assert exc.value.reason == "unrecognized_provider"
    assert provider.calls == []
    assert _row_count(session) == 0
    denials = _deny_events(session)
    assert len(denials) == 1
    assert denials[0].details["reason"] == "unrecognized_provider"


# --- 2. 不通过 vs 无法查验 严格区分 -------------------------------------------


def test_verified_fail_not_overwritten_by_unable(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[
            make_result(
                VerificationStatus.VERIFIED_FAIL,
                face=FACE,
                error_code="FailedOperation.InvoiceMismatch",
            ),
            make_result(VerificationStatus.UNABLE_TO_VERIFY, error_code="Timeout"),
        ],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert row.status == VerificationStatus.VERIFIED_FAIL.value

    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    # 技术性失败不得抹掉「票面不一致」的结论。
    assert row.status == VerificationStatus.VERIFIED_FAIL.value
    assert _row_count(session) == 1
    assert row.verify_attempt_count == 2


def test_unable_then_fail_upgrades_to_verified_fail(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[
            make_result(VerificationStatus.UNABLE_TO_VERIFY, error_code="Timeout"),
            make_result(VerificationStatus.VERIFIED_FAIL, face=FACE),
        ],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert row.status == VerificationStatus.UNABLE_TO_VERIFY.value

    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert row.status == VerificationStatus.VERIFIED_FAIL.value
    assert _row_count(session) == 1


def test_verified_pass_is_terminal(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[
            make_result(VerificationStatus.VERIFIED_PASS, face=FACE),
            make_result(VerificationStatus.UNABLE_TO_VERIFY, error_code="Timeout"),
        ],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    _verify(session, world, provider, source_file_id=world.source_file_a.id)
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert row.status == VerificationStatus.VERIFIED_PASS.value


# --- 3. 幂等与当日限次 --------------------------------------------------------


def test_repeat_verification_does_not_create_second_row(
    session: Session, world: World
) -> None:
    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE)],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    first = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    second = _verify(session, world, provider, source_file_id=world.source_file_a.id)

    assert first.id == second.id
    assert _row_count(session) == 1
    assert second.verify_attempt_count == 2
    assert second.daily_verify_count == 2


def test_daily_limit_blocks_the_sixth_call(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE)],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    for _ in range(DAILY_VERIFY_LIMIT):
        _verify(session, world, provider, source_file_id=world.source_file_a.id)

    assert len(provider.calls) == DAILY_VERIFY_LIMIT
    row = _rows(session)[0]
    assert row.daily_verify_count == DAILY_VERIFY_LIMIT

    with pytest.raises(DailyVerifyLimitExceeded) as exc:
        _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert exc.value.limit == DAILY_VERIFY_LIMIT
    # 第 6 次既不改库也不调用渠道。
    assert len(provider.calls) == DAILY_VERIFY_LIMIT
    assert _row_count(session) == 1
    assert row.daily_verify_count == DAILY_VERIFY_LIMIT


def test_daily_limit_resets_on_a_new_day(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE)],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    for _ in range(DAILY_VERIFY_LIMIT):
        _verify(session, world, provider, source_file_id=world.source_file_a.id)
    with pytest.raises(DailyVerifyLimitExceeded):
        _verify(session, world, provider, source_file_id=world.source_file_a.id)

    # 次日重新计数。
    tomorrow = FIXED_TODAY + timedelta(days=1)
    row = verify_invoice(
        session,
        world.principal(world.finance_operator),
        provider,
        make_fields(),
        source_file_id=world.source_file_a.id,
        today=tomorrow,
        now=FIXED_NOW + timedelta(days=1),
    )
    assert row.daily_verify_count == 1
    assert row.last_verify_date == tomorrow
    assert row.verify_attempt_count == DAILY_VERIFY_LIMIT + 1


def test_suspected_duplicate_when_same_invoice_on_another_source_file(
    session: Session, world: World
) -> None:
    provider = ManualAssistedProvider()
    first = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert first.duplicate_check_result == "unique"

    second = _verify(session, world, provider, source_file_id=world.source_file_b.id)
    assert first.id == second.id
    assert _row_count(session) == 1
    assert second.duplicate_check_result == DUPLICATE_SUSPECTED
    assert second.status == VerificationStatus.SUSPECTED_DUPLICATE.value


def test_classify_duplicate_rules() -> None:
    assert classify_duplicate(None, 1) == "unique"
    assert classify_duplicate(1, None) == "unique"
    assert classify_duplicate(1, 1) == "unique"
    assert classify_duplicate(1, 2) == DUPLICATE_SUSPECTED


# --- 4. 授权与审计 ------------------------------------------------------------


def test_verify_requires_invoice_verify_permission(
    session: Session, world: World
) -> None:
    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE)],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    outsider = world.principal(world.outsider)
    with pytest.raises(InvoiceVerificationDeniedError) as exc:
        _verify(session, world, provider, principal=outsider)
    assert exc.value.reason == "missing_permission"
    assert provider.calls == []
    assert _row_count(session) == 0

    denials = _deny_events(session)
    assert len(denials) == 1
    assert denials[0].actor_id == world.outsider.id
    assert denials[0].details == {
        "attempted_action": "verify",
        "reason": "missing_permission",
    }


def test_success_audit_has_no_sensitive_plaintext(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[
            make_result(
                VerificationStatus.VERIFIED_PASS,
                face=FACE,
                provider_ref="req-audit",
                evidence={
                    "Response": {
                        "Invoice": {
                            "Number": "12345678",
                            "Code": "011002000211",
                            "SellerTaxCode": "91310000MA1FL0XXXX",
                            "SecretId": "AKIDshould-not-appear",
                        }
                    }
                },
            )
        ],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    row = _verify(
        session,
        world,
        provider,
        source_file_id=world.source_file_a.id,
        fields=make_fields(seller_tax_code="91310000MA1FL0XXXX"),
    )

    events = _audit_events(session, row)
    assert len(events) == 1
    event = events[0]
    assert event.action == VERIFY_ACTION
    assert event.actor_id == world.finance_operator.id
    assert event.resource_type == "invoice_verification"

    rendered = str(event.details)
    for sensitive in (
        "12345678",
        "011002000211",
        "91310000MA1FL0XXXX",
        "AKIDshould-not-appear",
    ):
        assert sensitive not in rendered
    # details 里只有单向摘要与非敏感值。
    assert event.details["duplicate_key"] == row.duplicate_key
    assert event.details["status"] == VerificationStatus.VERIFIED_PASS.value
    assert event.details["provider_name"] == "stub/authorized"


def test_evidence_is_sanitized_before_storage(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[
            make_result(
                VerificationStatus.VERIFIED_PASS,
                face=FACE,
                evidence={
                    "Response": {
                        "Invoice": {
                            "Number": "12345678",
                            "Code": "011002000211",
                            "CheckCode": "654321",
                            "SellerTaxCode": "91310000MA1FL0XXXX",
                            "SecretId": "AKIDshould-not-appear",
                        },
                        "RequestId": "req-1",
                    }
                },
            )
        ],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    session.refresh(row)

    assert row.evidence_ref is not None
    assert row.evidence_ref.startswith("sha256:")
    stored = row.evidence_json
    assert stored is not None
    invoice = stored["Response"]["Invoice"]
    assert invoice["Number"] == "****5678"
    assert invoice["Code"] == "****0211"
    assert invoice["CheckCode"] == "****4321"
    assert invoice["SellerTaxCode"] == "****XXXX"
    assert invoice["SecretId"] == "***redacted***"
    assert "12345678" not in str(stored)
    assert "AKIDshould-not-appear" not in str(stored)


def test_unable_result_keeps_previous_evidence(session: Session, world: World) -> None:
    provider = StubProvider(
        results=[
            make_result(
                VerificationStatus.VERIFIED_PASS,
                face=FACE,
                evidence={"Response": {"Invoice": {"Number": "12345678"}}},
            ),
            make_result(VerificationStatus.UNABLE_TO_VERIFY, error_code="Timeout"),
        ],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    ref_before = row.evidence_ref
    assert ref_before is not None

    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)
    assert row.status == VerificationStatus.VERIFIED_PASS.value  # 终态不被覆盖
    assert row.evidence_ref == ref_before


def test_duplicate_key_unique_constraint_prevents_second_row(
    session: Session, world: World
) -> None:
    """数据库层兜底：同一 ``duplicate_key`` 无法插入第二行。"""
    from sqlalchemy.exc import IntegrityError

    provider = StubProvider(
        results=[make_result(VerificationStatus.VERIFIED_PASS, face=FACE)],
        _provider_type=ProviderType.AUTHORIZED_PROVIDER,
    )
    row = _verify(session, world, provider, source_file_id=world.source_file_a.id)

    clone = InvoiceVerification(
        duplicate_key=row.duplicate_key,
        invoice_no="12345678",
        invoice_date=FIXED_TODAY,
        provider_type="authorized_provider",
        status="pending_verification",
    )
    # 用 SAVEPOINT 隔离这次注定失败的插入，外层事务与首行不受影响。
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(clone)
            session.flush()
    assert _row_count(session) == 1
