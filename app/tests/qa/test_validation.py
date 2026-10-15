"""W1-14 §4 amount and field validation, all through the real HTTP surface.

Each case asserts that the malformed input is rejected **and** that no partial
write happened (no line, no payment, unchanged claim status and total).

Two genuine deviations from the required behaviour are recorded here with
``pytest.mark.xfail(strict=True)`` so they are visible and will flip to a failure
the moment they are fixed (see the W1-14 defect list):

* ``test_overlong_description_is_rejected`` -- no max-length bound on
  ``description``; a 100 000-character description is accepted;
* ``test_over_precision_amount_response_matches_database`` -- ``1.234`` is echoed
  back as ``1.234`` while the database stores ``1.23``.

A third deviation -- malformed *numeric* input (negative / out-of-range amounts)
returning HTTP 500 instead of 422 -- is asserted at its real, safe outcome
(rejected, nothing written) and noted in the defect list.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.expense.models import ExpenseClaim, ExpenseLine, ReimbursementPayment
from app.expense.service import add_line as service_add_line
from app.expense.service import create_claim as service_create_claim
from app.identity.authorization import Principal
from app.tests.qa.conftest import (
    World,
    add_line,
    auth_headers,
    claim_with_line,
    create_claim,
    submit_claim,
)

#: Substrings that must never appear in an error response body.
_FORBIDDEN = ("Traceback", "psycopg", "postgresql://", "SELECT ", "sqlalchemy")


def _line_count(engine: Engine, claim_id: int) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count())
            .select_from(ExpenseLine)
            .where(ExpenseLine.claim_id == claim_id)
        )


def _payment_count(engine: Engine, claim_id: int) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count())
            .select_from(ReimbursementPayment)
            .where(ReimbursementPayment.claim_id == claim_id)
        )


def _claim_row(engine: Engine, claim_id: int) -> tuple[str, Decimal]:
    with Session(engine) as session:
        claim = session.get(ExpenseClaim, claim_id)
        return claim.status, claim.total_amount


def _assert_clean(text: str) -> None:
    for needle in _FORBIDDEN:
        assert needle not in text, f"error body leaked {needle!r}"


# --- float amounts ------------------------------------------------------------


def test_json_float_amounts_are_normalised_exactly(
    client: TestClient, world: World
) -> None:
    """JSON numbers are converted to exact decimals: 0.1 + 0.2 == 0.30, not 0.30000...4."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    assert add_line(client, owner, world, claim_id, amount=0.1).status_code == 201
    assert add_line(client, owner, world, claim_id, amount=0.2).status_code == 201

    body = client.get(f"/claims/{claim_id}", headers=owner).json()
    assert body["total_amount"] == "0.30"
    assert "0.30000000000000004" not in client.get(
        f"/claims/{claim_id}", headers=owner
    ).text


def test_service_layer_rejects_python_float_amount(
    qa_engine: Engine, world: World
) -> None:
    """A Python ``float`` passed to the service layer is refused before any write."""
    principal = Principal(employee_id=world.claimant.id, company_id=world.company.id)
    with Session(qa_engine) as session:
        claim = service_create_claim(session, principal)
        with pytest.raises(TypeError):
            service_add_line(
                session,
                principal,
                claim.id,
                expense_date=date(2026, 10, 1),
                category_id=world.category.id,
                description="float 金额",
                amount=1.5,  # type: ignore[arg-type]
            )
        session.flush()
        assert claim.total_amount == Decimal("0.00")
    assert _line_count(qa_engine, claim.id) == 0


def test_service_layer_rejects_float_tax_amount(
    qa_engine: Engine, world: World
) -> None:
    """A float ``tax_amount`` is likewise refused with no partial write."""
    principal = Principal(employee_id=world.claimant.id, company_id=world.company.id)
    with Session(qa_engine) as session:
        claim = service_create_claim(session, principal)
        with pytest.raises(TypeError):
            service_add_line(
                session,
                principal,
                claim.id,
                expense_date=date(2026, 10, 1),
                category_id=world.category.id,
                description="float 税额",
                amount=Decimal("10.00"),
                tax_amount=0.1,  # type: ignore[arg-type]
            )
        session.flush()
    assert _line_count(qa_engine, claim.id) == 0


# --- negative / out-of-range amounts -----------------------------------------


def test_negative_line_amount_is_rejected_without_partial_write(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A negative amount is rejected and writes no line.

    W1-14 DEFECT #1: the rejection surfaces as HTTP 500 (the ``IntegrityError``
    raised by the ``ck_expense_lines_amount_non_negative`` CHECK is not mapped to
    422), so the requirement "rejected without a partial write" holds but the
    status code is wrong for a client input error.
    """
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)

    response = add_line(client, owner, world, claim_id, amount="-5.00")
    assert response.status_code >= 400, response.status_code
    assert response.status_code != 201
    _assert_clean(response.text)

    assert _line_count(qa_engine, claim_id) == 0
    status, total = _claim_row(qa_engine, claim_id)
    assert status == "draft"
    assert total == Decimal("0.00")


def test_negative_tax_amount_is_rejected_without_partial_write(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A negative tax amount is rejected and writes no line (see DEFECT #1)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount="10.00", tax="-1.00")
    assert response.status_code >= 400
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0
    assert _claim_row(qa_engine, claim_id)[1] == Decimal("0.00")


def test_out_of_range_amount_is_rejected_without_partial_write(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An amount beyond NUMERIC(18,2) is rejected and writes no line (see DEFECT #1)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(
        client, owner, world, claim_id, amount="99999999999999999999.99"
    )
    assert response.status_code >= 400
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0


# --- zero amounts -------------------------------------------------------------


def test_zero_amount_line_is_allowed_but_zero_total_claim_cannot_submit(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A 0.00 line is accepted (DB allows >= 0); the resulting 0-total claim cannot submit."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    added = add_line(client, owner, world, claim_id, amount="0.00")
    assert added.status_code == 201

    submit = submit_claim(client, owner, claim_id)
    assert submit.status_code == 422
    assert submit.json()["error"]["code"] == "validation_error"
    assert _claim_row(qa_engine, claim_id)[0] == "draft"
    assert _claim_row(qa_engine, claim_id)[1] == Decimal("0.00")


def test_zero_and_negative_payment_are_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A payment of 0.00 or a negative amount is rejected and registers nothing."""
    owner = auth_headers(client, world.claimant)
    claim_id = claim_with_line(client, owner, world)
    assert submit_claim(client, owner, claim_id).status_code == 200
    assert (
        client.post(
            f"/claims/{claim_id}/approve", headers=auth_headers(client, world.manager)
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/claims/{claim_id}/finance-review",
            headers=auth_headers(client, world.finance_operator),
        ).status_code
        == 200
    )

    payer = auth_headers(client, world.finance_lead)
    for amount in ("0.00", "-1.00"):
        response = client.post(
            f"/claims/{claim_id}/payments",
            json={"amount": amount},
            headers=payer,
        )
        assert response.status_code == 422, (amount, response.status_code)
        assert response.json()["error"]["code"] == "validation_error"
    assert _payment_count(qa_engine, claim_id) == 0
    assert _claim_row(qa_engine, claim_id)[0] == "finance_reviewed"


# --- field validation ---------------------------------------------------------


@pytest.mark.parametrize("bad_date", ["not-a-date", "2026-02-30", "2026-13-01", ""])
def test_invalid_expense_date_is_rejected_without_partial_write(
    client: TestClient, world: World, qa_engine: Engine, bad_date: str
) -> None:
    """An unparseable / impossible date is rejected by schema validation."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, expense_date=bad_date)
    assert response.status_code == 422, (bad_date, response.status_code)
    assert response.json()["error"]["code"] == "validation_error"
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0


def test_missing_required_fields_are_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A line without amount / expense_date is refused by schema validation."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    for payload in (
        {"description": "no amount", "category_id": world.category.id},
        {"amount": "10.00", "category_id": world.category.id},
        {"amount": "10.00", "expense_date": "2026-10-01"},
    ):
        response = client.post(
            f"/claims/{claim_id}/lines", json=payload, headers=owner
        )
        assert response.status_code == 422, payload
        _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0


def test_empty_amount_string_is_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An empty amount string is not a valid decimal."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount="")
    assert response.status_code == 422
    assert _line_count(qa_engine, claim_id) == 0


def test_boolean_amount_is_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A boolean is not a valid money value (rejected at the schema boundary)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount=True)
    assert response.status_code == 422
    assert _line_count(qa_engine, claim_id) == 0


def test_empty_claim_submit_is_rejected_without_state_change(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A claim with no lines cannot be submitted."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = submit_claim(client, owner, claim_id)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    with Session(qa_engine) as session:
        claim = session.get(ExpenseClaim, claim_id)
        assert claim.status == "draft"
        assert claim.submitted_at is None


# --- known deviations (recorded, strict xfail) --------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="W1-14 DEFECT #2: no max-length bound on description; a 100k-char "
    "description is accepted (201) instead of rejected",
)
def test_overlong_description_is_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An overlong description must be rejected (task §4)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(
        client, owner, world, claim_id, description="x" * 100_000
    )
    assert response.status_code == 422, response.status_code


def test_overlong_description_current_behaviour_is_pinned(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Pin the observed behaviour of DEFECT #2 so the gap stays visible.

    Requirement: reject.  Observed: accepted and persisted.  This test documents
    the current (defective) outcome and asserts the write is at least complete and
    consistent -- it is not an endorsement of the behaviour.
    """
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, description="x" * 100_000)
    assert response.status_code == 201  # observed defect: should be 422
    assert _line_count(qa_engine, claim_id) == 1


@pytest.mark.xfail(
    strict=True,
    reason="W1-14 DEFECT #3: amount is not quantized to 2dp before the response; "
    "1.234 is echoed while the DB stores 1.23",
)
def test_over_precision_amount_response_matches_database(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """The persisted amount and the API response must agree."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount="1.234")
    assert response.status_code == 201
    with Session(qa_engine) as session:
        stored = session.scalar(
            select(ExpenseLine.amount).where(ExpenseLine.claim_id == claim_id)
        )
    assert Decimal(response.json()["amount"]) == stored


def test_over_precision_amount_current_behaviour_is_pinned(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Pin DEFECT #3: response says 1.234 while DB/total say 1.23."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount="1.234")
    assert response.status_code == 201
    assert response.json()["amount"] == "1.234"  # observed defect
    detail = client.get(f"/claims/{claim_id}", headers=owner).json()
    assert detail["total_amount"] == "1.23"  # DB value after NUMERIC(18,2) rounding
