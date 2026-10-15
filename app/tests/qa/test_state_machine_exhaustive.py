"""W1-14 §5 state-machine exhaustion: every status x every action.

Two layers are exercised:

* the **pure** transition table (``app.expense.state_machine.next_status``) for all
  7 statuses x 7 actions = 49 combinations -- legal cells return the documented
  target, illegal cells raise ``InvalidTransitionError``;
* the **service layer** against the real database for the same 49 combinations,
  asserting that a legal transition lands on the target status and an illegal one
  raises without changing the persisted status.

``add_line`` (draft-only, and not a state-machine action) is exhausted separately.

Legal cells (10): ``draft→submit``; ``submitted→{approve,return,reject,withdraw}``;
``withdrawn→submit``; ``approved→{finance_review,return,reject}``;
``finance_reviewed→pay``.  The remaining 39 cells are illegal.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.expense.models import ExpenseClaim
from app.expense.service import (
    ValidationError,
    add_line,
    approve,
    create_claim,
    finance_review,
    record_payment,
    reject,
    return_for_fix,
    submit,
    withdraw,
)
from app.expense.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    next_status,
)
from app.identity.authorization import Principal
from app.tests.qa.conftest import World

STATUSES: tuple[str, ...] = (
    "draft",
    "submitted",
    "withdrawn",
    "approved",
    "finance_reviewed",
    "paid",
    "rejected",
)

ACTIONS: tuple[str, ...] = (
    "submit",
    "withdraw",
    "approve",
    "return",
    "reject",
    "finance_review",
    "pay",
)


def _principal(employee_id: int, world: World) -> Principal:
    return Principal(employee_id=employee_id, company_id=world.company.id)


def _build_claim_in_status(
    session: Session, world: World, status: str
) -> ExpenseClaim:
    """Drive a fresh claim to ``status`` through the service layer."""
    owner = _principal(world.claimant.id, world)
    approver = _principal(world.manager.id, world)
    finance = _principal(world.finance_operator.id, world)
    payer = _principal(world.finance_lead.id, world)

    claim = create_claim(session, owner)
    add_line(
        session,
        owner,
        claim.id,
        expense_date=date(2026, 10, 1),
        category_id=world.category.id,
        description="状态机",
        amount=Decimal("100.00"),
    )
    if status == "draft":
        return claim
    submit(session, owner, claim.id)
    if status == "submitted":
        return claim
    if status == "withdrawn":
        withdraw(session, owner, claim.id)
        return claim
    if status == "approved":
        approve(session, approver, claim.id)
        return claim
    if status == "rejected":
        reject(session, approver, claim.id)
        return claim
    approve(session, approver, claim.id)
    if status == "finance_reviewed":
        finance_review(session, finance, claim.id)
        return claim
    finance_review(session, finance, claim.id)
    assert status == "paid"
    record_payment(session, payer, claim.id, amount=Decimal("100.00"))
    return claim


def _attempt(session: Session, world: World, action: str, claim_id: int):
    """Invoke the service action with a principal that is authorised for it.

    Using a fully-authorised principal (owner for owner-only actions, the
    claimant's manager for approvals, finance roles for review/payment) means the
    *only* gate left is the state machine, which is what this suite isolates.
    """
    owner = _principal(world.claimant.id, world)
    approver = _principal(world.manager.id, world)
    finance = _principal(world.finance_operator.id, world)
    payer = _principal(world.finance_lead.id, world)

    if action == "submit":
        return submit(session, owner, claim_id)
    if action == "withdraw":
        return withdraw(session, owner, claim_id)
    if action == "approve":
        return approve(session, approver, claim_id)
    if action == "return":
        return return_for_fix(session, approver, claim_id)
    if action == "reject":
        return reject(session, approver, claim_id)
    if action == "finance_review":
        return finance_review(session, finance, claim_id)
    if action == "pay":
        return record_payment(session, payer, claim_id, amount=Decimal("100.00"))
    raise AssertionError(f"unknown action {action!r}")


def test_transition_table_shape() -> None:
    """The table is 7 statuses x 7 actions: 10 legal cells, 39 illegal."""
    assert set(ALLOWED_TRANSITIONS) == set(STATUSES)
    assert sum(len(row) for row in ALLOWED_TRANSITIONS.values()) == 10
    assert len(STATUSES) * len(ACTIONS) == 49
    assert 49 - 10 == 39


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("action", ACTIONS)
def test_pure_state_machine(status: str, action: str) -> None:
    """Pure table: legal cells return the target, illegal cells raise."""
    if action in ALLOWED_TRANSITIONS[status]:
        assert next_status(status, action) == ALLOWED_TRANSITIONS[status][action]
    else:
        with pytest.raises(InvalidTransitionError) as excinfo:
            next_status(status, action)
        assert excinfo.value.status == status
        assert excinfo.value.action == action


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("action", ACTIONS)
def test_service_state_machine_exhaustive(
    qa_engine: Engine, world: World, status: str, action: str
) -> None:
    """Service layer: legal transitions land on the target; illegal ones leave state."""
    legal = action in ALLOWED_TRANSITIONS[status]
    expected = ALLOWED_TRANSITIONS[status][action] if legal else status

    with Session(qa_engine, expire_on_commit=False) as session:
        claim = _build_claim_in_status(session, world, status)
        session.commit()
        claim_id = claim.id
        assert claim.status == status

        if legal:
            _attempt(session, world, action, claim_id)
            session.commit()
            assert claim.status == expected, (status, action, claim.status)
        else:
            with pytest.raises(InvalidTransitionError) as excinfo:
                _attempt(session, world, action, claim_id)
            assert excinfo.value.status == status
            assert excinfo.value.action == action
            session.rollback()

        # Re-read from the database: the persisted status is exactly as expected.
        session.expire_all()
        reloaded = session.get(ExpenseClaim, claim_id)
        assert reloaded is not None
        assert reloaded.status == expected, (status, action, reloaded.status)


@pytest.mark.parametrize("status", STATUSES)
def test_add_line_is_draft_only(qa_engine: Engine, world: World, status: str) -> None:
    """``add_line`` only works on a draft; every other status raises and writes nothing."""
    owner = _principal(world.claimant.id, world)
    with Session(qa_engine, expire_on_commit=False) as session:
        claim = _build_claim_in_status(session, world, status)
        session.commit()
        if status == "draft":
            line = add_line(
                session,
                owner,
                claim.id,
                expense_date=date(2026, 10, 2),
                category_id=world.category.id,
                description="ok",
                amount=Decimal("1.00"),
            )
            assert line.id is not None
        else:
            with pytest.raises(ValidationError) as excinfo:
                add_line(
                    session,
                    owner,
                    claim.id,
                    expense_date=date(2026, 10, 2),
                    category_id=world.category.id,
                    description="no",
                    amount=Decimal("1.00"),
                )
            assert excinfo.value.reason == "claim_not_draft"
            session.rollback()
            session.expire_all()
            assert session.get(ExpenseClaim, claim.id).status == status
