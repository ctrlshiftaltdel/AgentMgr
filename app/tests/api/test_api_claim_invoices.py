"""发票挂账端点的 API 级测试（Issue #60）。

端点：

* ``POST /claims/{claim_id}/invoices``——挂账（复用 ``app.expense.invoices.
  attach_invoice``，任务 W1-23 语义）；
* ``GET /claims/{claim_id}/invoices``——只读列表。

覆盖验收标准：

a. 合法挂账 → 201 + **脱敏**摘要（票面身份只以单向摘要 ``duplicate_key`` 出现，
   响应与审计都**不回显**票号 / 发票代码明文）；落库状态由服务端从查验记录推导
   （无查验记录 → ``pending_verification``）；
b. 幂等键 ``duplicate_key``：同一张票在同一报销单内重复挂账 → 422（服务层先查
   后拒）+ 拒绝审计，行数不变（不产生第二行）；
c. 状态规则：非 ``draft`` 报销单 → 422 + 拒绝审计，行数不变；
d. 授权：非报销人本人 → 403 ``authorization_denied`` + 拒绝审计，行数不变；
   请求体里的 ``employee_id`` 被显式忽略（归属只认 ``X-Employee-Id``）；
e. 状态推导：已有 ``verified_pass`` 查验记录时挂账行状态逐字复制该状态；
   跨报销单出现同一张票时两侧行均置 ``suspected_duplicate``；
f. 列表：只读、按 ``id`` 升序、脱敏；复用与 ``GET /claims/{id}`` 相同的读取授权
   （越权 / 无读取权限 → 403；报销单不存在 → 404）；
g. 输入校验：非法开票日期 / 空票号 → 422（不落行、不落审计）；
h. 认证既有行为：无令牌 → 503；错令牌 / 缺 ``X-Employee-Id`` → 401。

全部跑在真实 ``ajaimgr_test`` 库 + 真实 ``TestClient`` 上。``invoice_verifications``
不在 ``app/tests/api/conftest.py`` 的清理列表里，且 ``verified_by`` 外键引用
``employees.id``，因此本文件自建夹具在每个测试后清掉自己写入的查验行（须先于
``world`` 夹具删员工执行）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.expense.models import ExpenseInvoice
from app.integrations.invoice_verification import InvoiceFields, compute_duplicate_key
from app.integrations.models import InvoiceVerification
from app.tests.api.conftest import TEST_TOKEN, World

_INVOICE_NO = "12345678"
_INVOICE_CODE = "011002000211"
_INVOICE_DATE = "2026-10-16"
_AMOUNT = "88.50"

_EXPECTED_KEYS = {
    "id",
    "claim_id",
    "verification_status",
    "duplicate_key",
    "suspected_duplicate",
    "source_file_id",
    "invoice_date",
    "invoice_kind",
    "amount",
    "created_at",
}


@pytest.fixture(autouse=True)
def _clean_invoice_rows(engine: Engine, world: World) -> Iterator[None]:
    """每个测试后清掉本模块写入的发票查验行（须先于 ``world`` 删员工执行）。"""
    yield
    with Session(engine) as session:
        session.execute(delete(InvoiceVerification))
        session.commit()


# --- 辅助 ---------------------------------------------------------------------


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "invoice_no": _INVOICE_NO,
        "invoice_date": _INVOICE_DATE,
        "invoice_code": _INVOICE_CODE,
        "invoice_kind": "01",
        "amount": _AMOUNT,
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


def _create_claim(client: TestClient, headers: dict[str, str]) -> int:
    response = client.post("/claims", json={"currency": "CNY"}, headers=headers)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _submitted_claim(
    client: TestClient, headers: dict[str, str], world: World
) -> int:
    claim_id = _create_claim(client, headers)
    line = client.post(
        f"/claims/{claim_id}/lines",
        json={
            "expense_date": "2026-10-01",
            "category_id": world.category.id,
            "description": "出差打车",
            "amount": "10.00",
        },
        headers=headers,
    )
    assert line.status_code == 201, line.text
    submitted = client.post(f"/claims/{claim_id}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    return claim_id


def _attach(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    employee,  # type: ignore[no-untyped-def]
    claim_id: int,
    *,
    request_id: str,
    payload: dict[str, object] | None = None,
    token: str | None = None,
):  # type: ignore[no-untyped-def]
    headers = {**auth(employee, token=token), "X-Request-Id": request_id}
    return client.post(
        f"/claims/{claim_id}/invoices", json=payload or _payload(), headers=headers
    )


def _rows(engine: Engine, claim_id: int | None = None) -> list[ExpenseInvoice]:
    with Session(engine) as session:
        stmt = select(ExpenseInvoice)
        if claim_id is not None:
            stmt = stmt.where(ExpenseInvoice.claim_id == claim_id)
        return list(session.scalars(stmt.order_by(ExpenseInvoice.id)))


def _events(
    engine: Engine, *, action: str, request_id: str | None = None
) -> list[AuditEvent]:
    with Session(engine) as session:
        stmt = select(AuditEvent).where(AuditEvent.action == action)
        if request_id is not None:
            stmt = stmt.where(AuditEvent.request_id == request_id)
        return list(session.scalars(stmt.order_by(AuditEvent.id)))


# --- a. 合法挂账（脱敏 + 状态推导） -------------------------------------------


def test_attach_returns_desensitized_summary(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)

    response = _attach(
        client, auth, world.claimant, claim_id, request_id="issue-60-attach-ok-1"
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert set(body) == _EXPECTED_KEYS
    assert body["claim_id"] == claim_id
    # 无查验记录 → 明确「待查验」，绝不臆造「查验通过」。
    assert body["verification_status"] == "pending_verification"
    assert body["duplicate_key"] == compute_duplicate_key(_fields())
    assert body["suspected_duplicate"] is False
    assert body["source_file_id"] is None
    assert body["invoice_date"] == _INVOICE_DATE
    assert body["invoice_kind"] == "01"
    assert body["amount"] == _AMOUNT
    assert isinstance(body["created_at"], str) and body["created_at"]
    # 响应不回显票号 / 发票代码明文（票面身份只以单向摘要出现）。
    assert _INVOICE_NO not in response.text
    assert _INVOICE_CODE not in response.text

    rows = _rows(engine, claim_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == body["id"]
    assert row.verification_status == "pending_verification"
    assert row.duplicate_key == body["duplicate_key"]
    assert row.invoice_fields_json == {
        "invoice_code": _INVOICE_CODE,
        "invoice_no": _INVOICE_NO,
        "invoice_date": _INVOICE_DATE,
        "invoice_kind": "01",
        "amount": _AMOUNT,
    }

    events = _events(
        engine, action="expense_claim.invoice.attach", request_id="issue-60-attach-ok-1"
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == world.claimant.id
    assert event.resource_id == str(claim_id)
    assert event.details["duplicate_key"] == row.duplicate_key
    assert event.details["verification_status"] == "pending_verification"
    assert event.details["has_source_file"] is False
    # 审计 details 不含票号明文（服务层口径）。
    assert _INVOICE_NO not in str(event.details)


def test_request_body_employee_id_is_ignored(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)

    response = _attach(
        client,
        auth,
        world.claimant,
        claim_id,
        request_id="issue-60-attach-ignored-1",
        payload=_payload(employee_id=world.outsider.id),
    )
    assert response.status_code == 201, response.text

    events = _events(
        engine,
        action="expense_claim.invoice.attach",
        request_id="issue-60-attach-ignored-1",
    )
    assert len(events) == 1
    assert events[0].actor_id == world.claimant.id
    assert _rows(engine, claim_id)[0].claim_id == claim_id


# --- b. 幂等键：同单重复挂账先查后拒 ------------------------------------------


def test_repeat_attach_is_rejected_and_keeps_one_row(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)
    first = _attach(
        client, auth, world.claimant, claim_id, request_id="issue-60-attach-idem-1"
    )
    assert first.status_code == 201, first.text

    second = _attach(
        client, auth, world.claimant, claim_id, request_id="issue-60-attach-idem-2"
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "validation_error"

    assert len(_rows(engine, claim_id)) == 1

    denials = _events(
        engine, action="expense_claim.deny", request_id="issue-60-attach-idem-2"
    )
    assert len(denials) == 1
    assert denials[0].details == {
        "attempted_action": "attach_invoice",
        "reason": "invoice_already_attached",
    }


# --- c. 状态规则：仅 draft -----------------------------------------------------


def test_attach_to_non_draft_claim_is_rejected(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_id = _submitted_claim(client, headers, world)

    response = _attach(
        client,
        auth,
        world.claimant,
        claim_id,
        request_id="issue-60-attach-notdraft-1",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert _rows(engine, claim_id) == []

    denials = _events(
        engine, action="expense_claim.deny", request_id="issue-60-attach-notdraft-1"
    )
    assert len(denials) == 1
    assert denials[0].details == {
        "attempted_action": "attach_invoice",
        "reason": "claim_not_draft",
    }


# --- d. 授权：仅本人 -----------------------------------------------------------


def test_attach_by_other_employee_is_403(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)

    response = _attach(
        client,
        auth,
        world.outsider,
        claim_id,
        request_id="issue-60-attach-deny-1",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"
    assert _rows(engine, claim_id) == []

    denials = _events(
        engine, action="expense_claim.deny", request_id="issue-60-attach-deny-1"
    )
    assert len(denials) == 1
    assert denials[0].actor_id == world.outsider.id
    assert denials[0].details == {
        "attempted_action": "attach_invoice",
        "reason": "not_claim_owner",
    }


# --- e. 状态推导与跨单重复 -----------------------------------------------------


def test_verification_status_is_copied_from_verification_record(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
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
                status="verified_pass",
            )
        )
        session.commit()

    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)
    response = _attach(
        client,
        auth,
        world.claimant,
        claim_id,
        request_id="issue-60-attach-status-1",
    )
    assert response.status_code == 201, response.text
    assert response.json()["verification_status"] == "verified_pass"
    assert _rows(engine, claim_id)[0].verification_status == "verified_pass"


def test_cross_claim_duplicate_marks_both_rows(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_one = _create_claim(client, headers)
    claim_two = _create_claim(client, headers)

    first = _attach(
        client, auth, world.claimant, claim_one, request_id="issue-60-attach-cross-1"
    )
    assert first.status_code == 201, first.text
    assert first.json()["suspected_duplicate"] is False

    second = _attach(
        client, auth, world.claimant, claim_two, request_id="issue-60-attach-cross-2"
    )
    assert second.status_code == 201, second.text
    assert second.json()["suspected_duplicate"] is True

    rows = _rows(engine)
    assert {row.claim_id for row in rows} == {claim_one, claim_two}
    assert all(row.suspected_duplicate for row in rows)


# --- f. 列表：只读、脱敏、复用读取授权 ----------------------------------------


def test_list_returns_desensitized_rows_in_id_order(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
) -> None:
    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)
    for index in range(2):
        response = _attach(
            client,
            auth,
            world.claimant,
            claim_id,
            request_id=f"issue-60-list-ok-{index}",
            payload=_payload(invoice_no=f"1234567{index}"),
        )
        assert response.status_code == 201, response.text

    listed = client.get(f"/claims/{claim_id}/invoices", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert len(body) == 2
    assert [row["id"] for row in body] == sorted(row["id"] for row in body)
    for row in body:
        assert set(row) == _EXPECTED_KEYS
        assert row["claim_id"] == claim_id
    assert _INVOICE_CODE not in listed.text


def test_list_is_scoped_by_claim_read_authorization(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)
    attached = _attach(
        client, auth, world.claimant, claim_id, request_id="issue-60-list-scope-1"
    )
    assert attached.status_code == 201, attached.text

    # 本人（read/own）、主管（read/team，claimant 的 manager）、财务经办
    # （read/company）都能读到；主管与财务经办不是报销人本人，仍可**只读**列出。
    for reader in (world.claimant, world.manager, world.finance_operator):
        allowed = client.get(f"/claims/{claim_id}/invoices", headers=auth(reader))
        assert allowed.status_code == 200, allowed.text
        assert len(allowed.json()) == 1

    # 无读取范围者（outsider 不在本人/团队范围内；sysadmin 无任何 read 权限）→ 403。
    for index, reader in enumerate((world.outsider, world.sysadmin)):
        denied = client.get(
            f"/claims/{claim_id}/invoices",
            headers={
                **auth(reader),
                "X-Request-Id": f"issue-60-list-scope-deny-{index}",
            },
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "authorization_denied"
        denial = _events(
            engine,
            action="expense_claim.deny",
            request_id=f"issue-60-list-scope-deny-{index}",
        )
        assert len(denial) == 1
        assert denial[0].actor_id == reader.id
        assert denial[0].details == {
            "attempted_action": "read",
            "reason": "missing_permission"
            if reader is world.sysadmin
            else "not_in_scope",
        }


def test_list_of_unknown_claim_is_404(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    response = client.get("/claims/99999999/invoices", headers=auth(world.claimant))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- g. 输入校验 → 422 ---------------------------------------------------------


def test_invalid_invoice_identity_is_rejected(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
) -> None:
    headers = auth(world.claimant)
    claim_id = _create_claim(client, headers)

    bad_payloads: tuple[dict[str, object], ...] = (
        _payload(invoice_date="2026/10/16"),
        _payload(invoice_no=""),
        _payload(invoice_no="   "),
        # 位数多到无法量化到 2 位小数（避免输入驱动的 500）。
        _payload(amount="9" * 200),
        # 非有限金额由请求体模型拒绝（finite_number）。
        _payload(amount="NaN"),
        # 缺必填字段（请求体 schema 校验）。
        {"invoice_date": _INVOICE_DATE},
    )
    for index, payload in enumerate(bad_payloads):
        response = _attach(
            client,
            auth,
            world.claimant,
            claim_id,
            request_id=f"issue-60-attach-invalid-{index}",
            payload=payload,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "validation_error"

    assert _rows(engine, claim_id) == []
    # 输入校验拒绝不落审计（不是权限/状态拒绝）。
    for index in range(len(bad_payloads)):
        assert (
            _events(
                engine,
                action="expense_claim.deny",
                request_id=f"issue-60-attach-invalid-{index}",
            )
            == []
        )


# --- h. 认证既有行为 -----------------------------------------------------------


def test_missing_internal_token_returns_503(
    client_without_token: TestClient, world: World
) -> None:
    response = client_without_token.post(
        "/claims/1/invoices",
        json=_payload(),
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "X-Employee-Id": str(world.claimant.id),
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_wrong_or_missing_bearer_returns_401(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    wrong = _attach(
        client,
        auth,
        world.claimant,
        1,
        request_id="issue-60-attach-auth-1",
        token="not-the-right-token",
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"

    missing = client.post(
        "/claims/1/invoices",
        json=_payload(),
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
