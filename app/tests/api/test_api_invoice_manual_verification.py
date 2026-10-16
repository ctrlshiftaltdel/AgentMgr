"""人工辅助查验结果录入端点的 API 级测试（W1-22）。

覆盖任务验收标准（端点 ``POST /invoice-verifications/manual``）：

a. 无权限角色（claimant/employee）→ 403 + 拒绝审计存在、行未变；
b. 有权限但缺证据（缺失/空白）→ 422 拒绝、无行变更；
c. 合法录入（finance_operator）→ 201；落库 ``status=verified_pass``、
   ``provider_type=manual_assisted``、``verified_by``/``verified_at``/证据齐全；
   成功审计存在且 details **不含票号明文**；
d. 幂等：同票再录 → 记录数仍为 1；
e. 越权不可改他人录入（无权限重录 → 403，行 verified_by/status 不变）；
f. 已 ``verified_fail`` 的行不可被录成 pass → 409 拒绝 + 拒绝审计 + 行保持 fail；
h. 认证既有行为：无令牌 → 503；错令牌 / 缺 ``X-Employee-Id`` → 401。

全部跑在真实 ``ajaimgr_test`` 库 + 真实 ``TestClient`` 上。本文件自建
``_clean_invoice_rows`` 夹具：``invoice_verifications`` 不在
``app/tests/api/conftest.py`` 的清理列表里，且 ``verified_by`` 外键引用
``employees.id``，因此必须在 ``world`` 夹具删除员工**之前**清掉本文件写入的发票
行——夹具依赖 ``world`` 以保证该顺序；同一家公司里每张票只留一行由服务层保证。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.integrations.invoice_service import (
    MANUAL_DENY_ACTION,
    MANUAL_VERIFY_ACTION,
)
from app.integrations.invoice_verification import InvoiceFields, compute_duplicate_key
from app.integrations.models import InvoiceVerification
from app.tests.api.conftest import TEST_TOKEN, World

_ENDPOINT = "/invoice-verifications/manual"

_INVOICE_NO = "12345678"
_INVOICE_CODE = "011002000211"
_INVOICE_DATE = "2026-10-16"
_EVIDENCE = "已在国家税务总局全国增值税发票查验平台核验：票面信息一致，查验通过。"


@pytest.fixture(autouse=True)
def _clean_invoice_rows(engine: Engine, world: World) -> Iterator[None]:
    """每个测试结束后清掉本模块写入的发票查验行（须先于 ``world`` 删员工执行）。"""
    yield
    with Session(engine) as session:
        session.execute(delete(InvoiceVerification))
        session.commit()


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "invoice_no": _INVOICE_NO,
        "invoice_date": _INVOICE_DATE,
        "invoice_code": _INVOICE_CODE,
        "evidence": _EVIDENCE,
    }
    base.update(overrides)
    return base


def _fields(**overrides: object) -> InvoiceFields:
    base: dict[str, object] = {
        "invoice_no": _INVOICE_NO,
        "invoice_date": _INVOICE_DATE,
        "invoice_code": _INVOICE_CODE,
    }
    base.update(overrides)
    return InvoiceFields(**base)  # type: ignore[arg-type]


def _rows(engine: Engine) -> list[InvoiceVerification]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(InvoiceVerification).order_by(InvoiceVerification.id)
            )
        )


def _events(
    engine: Engine,
    *,
    action: str,
    request_id: str | None = None,
    actor_id: int | None = None,
) -> list[AuditEvent]:
    """按动作 + （请求 ID / 主体 ID）精确定位审计事件，跨测试顺序无关。"""
    with Session(engine) as session:
        stmt = select(AuditEvent).where(AuditEvent.action == action)
        if request_id is not None:
            stmt = stmt.where(AuditEvent.request_id == request_id)
        if actor_id is not None:
            stmt = stmt.where(AuditEvent.actor_id == actor_id)
        return list(session.scalars(stmt.order_by(AuditEvent.id)))


def _post(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    employee,  # type: ignore[no-untyped-def]
    *,
    request_id: str,
    payload: dict[str, object] | None = None,
):  # type: ignore[no-untyped-def]
    headers = {**auth(employee), "X-Request-Id": request_id}
    return client.post(_ENDPOINT, json=payload or _payload(), headers=headers)


# --- a. 无权限角色 → 403 + 拒绝审计 + 行未变 -----------------------------------


def test_unauthorized_role_gets_403_with_deny_audit(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    response = _post(
        client, auth, world.claimant, request_id="w1-22-api-deny-1"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"
    assert _rows(engine) == []

    denials = _events(
        engine, action=MANUAL_DENY_ACTION, request_id="w1-22-api-deny-1"
    )
    assert len(denials) == 1
    event = denials[0]
    assert event.actor_id == world.claimant.id
    assert event.details == {
        "attempted_action": "manual_verify",
        "reason": "missing_permission",
    }
    assert _INVOICE_NO not in str(event.details)


# --- b. 缺证据 / 校验失败 → 422、无行变更 --------------------------------------


def test_missing_or_blank_evidence_is_rejected_without_rows(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    bad_payloads: tuple[dict[str, object], ...] = (
        # 缺失 evidence（请求体 schema 校验）。
        {
            "invoice_no": _INVOICE_NO,
            "invoice_date": _INVOICE_DATE,
            "invoice_code": _INVOICE_CODE,
        },
        _payload(evidence=""),
        _payload(evidence="   "),
    )
    for index, payload in enumerate(bad_payloads):
        response = _post(
            client,
            auth,
            world.finance_operator,
            request_id=f"w1-22-api-evidence-{index}",
            payload=payload,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    assert _rows(engine) == []

    # 不落任何审计（缺证据是输入校验拒绝，不是权限/状态冲突）。
    for index in range(len(bad_payloads)):
        assert (
            _events(
                engine,
                action=MANUAL_VERIFY_ACTION,
                request_id=f"w1-22-api-evidence-{index}",
            )
            == []
        )
        assert (
            _events(
                engine,
                action=MANUAL_DENY_ACTION,
                request_id=f"w1-22-api-evidence-{index}",
            )
            == []
        )


def test_invalid_invoice_identity_is_rejected(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    for index, payload in enumerate(
        (_payload(invoice_date="2026/10/16"), _payload(invoice_no=""))
    ):
        response = _post(
            client,
            auth,
            world.finance_operator,
            request_id=f"w1-22-api-fields-{index}",
            payload=payload,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    assert _rows(engine) == []


# --- c. 合法录入：响应 + 落库 + 审计 -------------------------------------------


def test_finance_operator_records_successfully(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    response = _post(
        client, auth, world.finance_operator, request_id="w1-22-api-ok-1"
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert set(body) == {
        "id",
        "status",
        "provider_type",
        "provider_name",
        "verified_at",
        "verified_by",
        "duplicate_check_result",
        "evidence_ref",
    }
    assert body["status"] == "verified_pass"
    assert body["provider_type"] == "manual_assisted"
    assert body["provider_name"] == "manual_assisted"
    assert body["verified_at"] is not None
    assert body["verified_by"] == world.finance_operator.id
    assert body["duplicate_check_result"] == "unique"
    assert body["evidence_ref"].startswith("sha256:")
    # 响应不回显票号明文。
    assert _INVOICE_NO not in response.text

    rows = _rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == body["id"]
    assert row.status == "verified_pass"
    assert row.provider_type == "manual_assisted"
    assert row.provider_name == "manual_assisted"
    assert row.verified_by == world.finance_operator.id
    assert row.verified_at is not None
    assert row.duplicate_key == compute_duplicate_key(_fields())
    assert row.evidence_ref == body["evidence_ref"]
    evidence = row.evidence_json
    assert evidence is not None
    assert evidence["channel"] == "manual_assisted"
    assert evidence["note"] == _EVIDENCE
    assert evidence["recorded_by"] == world.finance_operator.id
    assert isinstance(evidence["recorded_at"], str) and evidence["recorded_at"]

    events = _events(
        engine, action=MANUAL_VERIFY_ACTION, request_id="w1-22-api-ok-1"
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == world.finance_operator.id
    assert event.resource_type == "invoice_verification"
    assert event.resource_id == str(row.id)
    assert event.details["from_status"] is None
    assert event.details["to_status"] == "verified_pass"
    assert event.details["provider_type"] == "manual_assisted"
    assert event.details["has_evidence"] is True
    assert event.details["duplicate_key"] == row.duplicate_key
    # 显式断言：审计 details 不含票号明文。
    assert _INVOICE_NO not in str(event.details)


def test_request_body_identity_fields_are_ignored(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    """请求体的 employee_id / actor 不得改变操作人（只认 X-Employee-Id）。"""
    response = _post(
        client,
        auth,
        world.finance_operator,
        request_id="w1-22-api-ignored-1",
        payload=_payload(employee_id=world.outsider.id, actor="some-model"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["verified_by"] == world.finance_operator.id
    assert _rows(engine)[0].verified_by == world.finance_operator.id


# --- d. 幂等：同票再录仍只有一行 -----------------------------------------------


def test_repeat_recording_is_idempotent(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    first = _post(
        client, auth, world.finance_operator, request_id="w1-22-api-idem-1"
    )
    assert first.status_code == 201
    second = _post(
        client,
        auth,
        world.finance_operator,
        request_id="w1-22-api-idem-2",
        payload=_payload(evidence="第二次核验：结论一致。"),
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

    rows = _rows(engine)
    assert len(rows) == 1
    assert rows[0].evidence_json["note"] == "第二次核验：结论一致。"

    events = _events(
        engine, action=MANUAL_VERIFY_ACTION, request_id="w1-22-api-idem-2"
    )
    assert len(events) == 1
    assert events[0].details["from_status"] == "verified_pass"
    assert events[0].details["to_status"] == "verified_pass"
    assert events[0].actor_id == world.finance_operator.id


# --- e. 越权不可改他人录入 ------------------------------------------------------


def test_unauthorized_rerecord_leaves_row_unchanged(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    created = _post(
        client, auth, world.finance_operator, request_id="w1-22-api-keep-1"
    )
    assert created.status_code == 201
    before = _rows(engine)[0]

    denied = _post(
        client,
        auth,
        world.claimant,
        request_id="w1-22-api-keep-2",
        payload=_payload(evidence="越权重录"),
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "authorization_denied"

    after = _rows(engine)[0]
    assert after.id == before.id
    assert after.status == "verified_pass"
    assert after.verified_by == world.finance_operator.id
    assert after.evidence_ref == before.evidence_ref
    assert after.evidence_json == before.evidence_json

    denials = _events(
        engine, action=MANUAL_DENY_ACTION, request_id="w1-22-api-keep-2"
    )
    assert len(denials) == 1
    assert denials[0].actor_id == world.claimant.id


def test_another_authorized_finance_can_rerecord(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    """有权限者（finance_lead）重录既有 manual 行：审计记录 from/to 与操作人。"""
    first = _post(
        client, auth, world.finance_operator, request_id="w1-22-api-relay-1"
    )
    assert first.status_code == 201

    second = _post(
        client,
        auth,
        world.finance_lead,
        request_id="w1-22-api-relay-2",
        payload=_payload(evidence="财务主管复核：结论一致。"),
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    rows = _rows(engine)
    assert len(rows) == 1
    assert rows[0].verified_by == world.finance_lead.id

    events = _events(
        engine, action=MANUAL_VERIFY_ACTION, request_id="w1-22-api-relay-2"
    )
    assert len(events) == 1
    assert events[0].actor_id == world.finance_lead.id
    assert events[0].details["from_status"] == "verified_pass"
    assert events[0].details["to_status"] == "verified_pass"


# --- f. verified_fail 不可被覆盖 -----------------------------------------------


def test_verified_fail_cannot_be_recorded_as_pass(
    client: TestClient, world: World, auth, engine: Engine
) -> None:
    # 预置一条渠道结论「查验不通过」的行（模拟既有渠道结果）。
    fields = _fields()
    with Session(engine) as session:
        session.add(
            InvoiceVerification(
                duplicate_key=compute_duplicate_key(fields),
                invoice_no=fields.invoice_no,
                invoice_date=fields.invoice_date,
                invoice_code=fields.invoice_code,
                provider_type="authorized_provider",
                provider_name="stub/authorized",
                status="verified_fail",
                error_code="FailedOperation.InvoiceMismatch",
            )
        )
        session.commit()

    response = _post(
        client, auth, world.finance_operator, request_id="w1-22-api-fail-1"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"

    rows = _rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "verified_fail"
    assert row.verified_by is None
    assert row.evidence_ref is None
    assert row.error_code == "FailedOperation.InvoiceMismatch"

    denials = _events(
        engine, action=MANUAL_DENY_ACTION, request_id="w1-22-api-fail-1"
    )
    assert len(denials) == 1
    assert denials[0].resource_id == str(row.id)
    assert denials[0].details == {
        "attempted_action": "manual_verify",
        "reason": "verified_fail_is_terminal",
    }
    assert _INVOICE_NO not in str(denials[0].details)


# --- h. 认证既有行为：503 / 401 ------------------------------------------------


def test_missing_internal_token_returns_503(
    client_without_token: TestClient, world: World
) -> None:
    response = client_without_token.post(
        _ENDPOINT,
        json=_payload(),
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "X-Employee-Id": str(world.finance_operator.id),
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_wrong_or_missing_bearer_returns_401(
    client: TestClient, world: World
) -> None:
    wrong = client.post(
        _ENDPOINT,
        json=_payload(),
        headers={
            "Authorization": "Bearer not-the-right-token",
            "X-Employee-Id": str(world.finance_operator.id),
        },
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"


def test_missing_employee_header_returns_401(
    client: TestClient, world: World
) -> None:
    response = client.post(
        _ENDPOINT,
        json=_payload(),
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
