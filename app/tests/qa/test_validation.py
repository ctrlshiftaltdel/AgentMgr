"""W1-14 §4 amount and field validation, all through the real HTTP surface.

Each case asserts that the malformed input is rejected **and** that no partial
write happened (no line, no payment, unchanged claim status and total).

The three W1-14 deviations in this file were fixed by task W1-17:

* ``test_overlong_description_is_rejected`` -- ``description`` now has a service
  layer length bound (1..500 chars); the strict ``xfail`` marker is gone;
* ``test_over_precision_amount_response_matches_database`` -- amounts are
  quantized to 2dp (``ROUND_HALF_UP``) before the write, so the response and the
  stored value both read ``1.23``; the strict ``xfail`` marker is gone;
* negative / out-of-range amounts -- rejected in the service layer as a 422
  ``validation_error`` (the bound is in the message) instead of falling through
  to a database CHECK violation and an HTTP 500.

A fourth deviation (the admin console does not audit a denied page) lives in
``test_audit_integrity.py`` and is **out of scope** for W1-17; its ``xfail``
marker is intentionally left in place.
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
from app.expense.money import MAX_AMOUNT
from app.expense.service import DESCRIPTION_MAX_LENGTH
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
    """A negative amount is rejected as 422 and writes no line.

    W1-14 DEFECT #1 (fixed by W1-17): the service layer now rejects the value
    before any write, so the database CHECK constraint is never reached and the
    client gets a 422 ``validation_error`` naming the allowed range instead of a
    500 ``internal_error``.
    """
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)

    response = add_line(client, owner, world, claim_id, amount="-5.00")
    assert response.status_code == 422, response.status_code
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert str(MAX_AMOUNT) in body["error"]["message"]
    assert "-5.00" not in response.text  # the offending value is never echoed
    _assert_clean(response.text)

    assert _line_count(qa_engine, claim_id) == 0
    status, total = _claim_row(qa_engine, claim_id)
    assert status == "draft"
    assert total == Decimal("0.00")


def test_negative_tax_amount_is_rejected_without_partial_write(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A negative tax amount is rejected as 422 and writes no line (DEFECT #1)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount="10.00", tax="-1.00")
    assert response.status_code == 422, response.status_code
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "tax_amount" in body["error"]["message"]
    assert str(MAX_AMOUNT) in body["error"]["message"]
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0
    assert _claim_row(qa_engine, claim_id)[1] == Decimal("0.00")


def test_out_of_range_amount_is_rejected_without_partial_write(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An amount beyond NUMERIC(18,2) is rejected as 422 and writes no line (DEFECT #1)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(
        client, owner, world, claim_id, amount="99999999999999999999.99"
    )
    assert response.status_code == 422, response.status_code
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    # The bound written into the message must match NUMERIC(18,2) exactly.
    assert str(MAX_AMOUNT) in body["error"]["message"]
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0


def test_max_amount_is_accepted_and_one_cent_more_is_not(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """The boundary itself is accepted; one cent above ``NUMERIC(18,2)`` is not."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)

    accepted = add_line(client, owner, world, claim_id, amount=str(MAX_AMOUNT))
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["amount"] == str(MAX_AMOUNT)

    claim_id_two = create_claim(client, owner)
    rejected = add_line(client, owner, world, claim_id_two, amount="10000000000000000.00")
    assert rejected.status_code == 422, rejected.status_code
    assert rejected.json()["error"]["code"] == "validation_error"
    assert _line_count(qa_engine, claim_id_two) == 0


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


# --- description length (W1-14 DEFECT #2, fixed by W1-17) ---------------------


def test_overlong_description_is_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An overlong description is rejected as 422 (task §4); nothing is written.

    The strict ``xfail`` that recorded DEFECT #2 was removed by W1-17: the
    service layer now bounds ``description`` length and the bound is reported in
    the error message.
    """
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(
        client, owner, world, claim_id, description="x" * 100_000
    )
    assert response.status_code == 422, response.status_code
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert str(DESCRIPTION_MAX_LENGTH) in body["error"]["message"]
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0


def test_description_at_max_length_is_accepted_and_one_over_is_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """The length bound is inclusive: 500 chars pass, 501 chars are rejected.

    This replaces ``test_overlong_description_current_behaviour_is_pinned``,
    which asserted the pre-fix (defective) 201 and the persisted row.  The
    behaviour changed under W1-17, so the assertion was updated to the required
    outcome and the coverage was widened to the exact boundary.
    """
    owner = auth_headers(client, world.claimant)

    claim_id = create_claim(client, owner)
    accepted = add_line(
        client, owner, world, claim_id, description="x" * DESCRIPTION_MAX_LENGTH
    )
    assert accepted.status_code == 201, accepted.text
    assert len(accepted.json()["description"]) == DESCRIPTION_MAX_LENGTH
    assert _line_count(qa_engine, claim_id) == 1

    other_claim = create_claim(client, owner)
    rejected = add_line(
        client,
        owner,
        world,
        other_claim,
        description="x" * (DESCRIPTION_MAX_LENGTH + 1),
    )
    assert rejected.status_code == 422, rejected.status_code
    assert rejected.json()["error"]["code"] == "validation_error"
    assert _line_count(qa_engine, other_claim) == 0


def test_empty_description_is_rejected(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """An empty description is rejected (min length 1); nothing is written."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, description="")
    assert response.status_code == 422, response.status_code
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert str(DESCRIPTION_MAX_LENGTH) in body["error"]["message"]
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0


# --- over-precision amounts (W1-14 DEFECT #3, fixed by W1-17) -----------------


def test_over_precision_amount_response_matches_database(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """The persisted amount and the API response must agree.

    The strict ``xfail`` that recorded DEFECT #3 was removed by W1-17: the amount
    is quantized to 2dp (``ROUND_HALF_UP``) before the write, so ``1.234`` is
    stored **and** echoed as ``1.23``.
    """
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount="1.234")
    assert response.status_code == 201
    assert response.json()["amount"] == "1.23"
    with Session(qa_engine) as session:
        stored = session.scalar(
            select(ExpenseLine.amount).where(ExpenseLine.claim_id == claim_id)
        )
    assert Decimal(response.json()["amount"]) == stored
    assert stored == Decimal("1.23")


def test_over_precision_tax_amount_is_quantized_before_response(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """``tax_amount`` is quantized the same way; response and database agree.

    Replaces ``test_over_precision_amount_current_behaviour_is_pinned``, which
    asserted the pre-fix response (``1.234``).  The assertion was updated to the
    required outcome and extended to cover ``tax_amount``.
    """
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(client, owner, world, claim_id, amount="10.00", tax="1.234")
    assert response.status_code == 201, response.text
    assert response.json()["tax_amount"] == "1.23"
    with Session(qa_engine) as session:
        stored = session.scalar(
            select(ExpenseLine.tax_amount).where(ExpenseLine.claim_id == claim_id)
        )
    assert stored == Decimal("1.23")


def test_over_precision_amounts_quantize_before_the_claim_total(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """The claim total is the sum of the *quantized* line amounts (1.23 + 2.35)."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    assert add_line(client, owner, world, claim_id, amount="1.234").status_code == 201
    assert add_line(client, owner, world, claim_id, amount="2.345").status_code == 201

    detail = client.get(f"/claims/{claim_id}", headers=owner).json()
    assert detail["total_amount"] == "3.58"
    assert _claim_row(qa_engine, claim_id)[1] == Decimal("3.58")


# --- database constraint violations are never a 500 ---------------------------


def test_database_constraint_violation_is_mapped_to_422_not_500(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """A DB constraint violation that bypasses service validation is 422, not 500.

    ``category_id`` is a foreign key the service layer does not resolve, so an
    unknown id reaches the database and raises ``IntegrityError``.  The fallback
    mapping must turn that into a 422 ``validation_error`` -- a client input
    problem, not a server fault -- and write nothing.
    """
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = client.post(
        f"/claims/{claim_id}/lines",
        json={
            "expense_date": "2026-10-01",
            "category_id": 999_999_999,
            "description": "FK 指向不存在的费用类型",
            "amount": "10.00",
            "tax_amount": "0.00",
        },
        headers=owner,
    )
    assert response.status_code == 422, response.status_code
    assert response.json()["error"]["code"] == "validation_error"
    _assert_clean(response.text)
    assert _line_count(qa_engine, claim_id) == 0


def test_valid_line_still_succeeds_after_validation(
    client: TestClient, world: World, qa_engine: Engine
) -> None:
    """Regression: a well-formed line is still accepted and totals stay consistent."""
    owner = auth_headers(client, world.claimant)
    claim_id = create_claim(client, owner)
    response = add_line(
        client, owner, world, claim_id, amount="100.00", tax="3.00", description="高铁票"
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["amount"] == "100.00"
    assert body["tax_amount"] == "3.00"
    assert _claim_row(qa_engine, claim_id)[1] == Decimal("100.00")
