"""报销状态机单元测试（纯逻辑，不触碰数据库）。

覆盖任务 W1-6 第三步固定的状态机：

1. 合法主路径 draft→submitted→approved→finance_reviewed→paid 全部成功；
2. 非法迁移抛 ``InvalidTransitionError``，异常信息含当前状态与允许动作；
   ``paid`` 再 ``submit`` 同样抛错；终态 ``allowed_actions`` 为空集；
3. 退回路径 submitted→draft、approved→draft；
4. 撤回重提 submitted→withdrawn→submitted；
5. 未知状态抛 ``ValueError``。
"""

from __future__ import annotations

import pytest

from app.expense.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    allowed_actions,
    next_status,
)

TERMINAL_STATUSES = ("paid", "rejected")


def test_happy_path_walks_to_paid() -> None:
    """draft→submitted→approved→finance_reviewed→paid 逐步成功。"""
    status = "draft"
    for action, expected in (
        ("submit", "submitted"),
        ("approve", "approved"),
        ("finance_review", "finance_reviewed"),
        ("pay", "paid"),
    ):
        status = next_status(status, action)
        assert status == expected
    assert status == "paid"


def test_return_paths_go_back_to_draft() -> None:
    """submitted→draft（退回补件）与 approved→draft 均成立。"""
    assert next_status("submitted", "return") == "draft"
    assert next_status("approved", "return") == "draft"


def test_withdraw_then_resubmit() -> None:
    """submitted→withdrawn→submitted。"""
    withdrawn = next_status("submitted", "withdraw")
    assert withdrawn == "withdrawn"
    assert next_status(withdrawn, "submit") == "submitted"


def test_submitted_branches() -> None:
    """submitted 的四个动作各自到达固定目标。"""
    assert next_status("submitted", "approve") == "approved"
    assert next_status("submitted", "reject") == "rejected"
    assert next_status("submitted", "withdraw") == "withdrawn"
    assert next_status("submitted", "return") == "draft"


def test_approved_branches() -> None:
    """approved 的三个动作各自到达固定目标。"""
    assert next_status("approved", "finance_review") == "finance_reviewed"
    assert next_status("approved", "reject") == "rejected"
    assert next_status("approved", "return") == "draft"


def test_draft_cannot_pay_directly() -> None:
    """draft 直接 pay 非法，异常信息含当前状态与允许动作。"""
    with pytest.raises(InvalidTransitionError) as excinfo:
        next_status("draft", "pay")

    message = str(excinfo.value)
    assert "draft" in message
    assert "submit" in message  # 允许的动作列表出现在信息里
    assert excinfo.value.status == "draft"
    assert excinfo.value.action == "pay"
    assert excinfo.value.allowed == frozenset({"submit"})


def test_paid_is_terminal() -> None:
    """终态 paid 再 submit 抛错，且 allowed_actions 为空集。"""
    assert allowed_actions("paid") == frozenset()
    with pytest.raises(InvalidTransitionError) as excinfo:
        next_status("paid", "submit")
    message = str(excinfo.value)
    assert "paid" in message
    # 空允许列表也要显式表达，避免信息里什么都没有。
    assert "(none)" in message


def test_rejected_is_terminal() -> None:
    """终态 rejected 无任何可用动作。"""
    assert allowed_actions("rejected") == frozenset()
    for action in ("submit", "approve", "pay", "withdraw", "return", "reject"):
        with pytest.raises(InvalidTransitionError):
            next_status("rejected", action)


def test_allowed_actions_per_status() -> None:
    """每个状态的允许动作集合与固定迁移表一致。"""
    assert allowed_actions("draft") == frozenset({"submit"})
    assert allowed_actions("submitted") == frozenset(
        {"approve", "return", "reject", "withdraw"}
    )
    assert allowed_actions("withdrawn") == frozenset({"submit"})
    assert allowed_actions("approved") == frozenset(
        {"finance_review", "return", "reject"}
    )
    assert allowed_actions("finance_reviewed") == frozenset({"pay"})
    for terminal in TERMINAL_STATUSES:
        assert allowed_actions(terminal) == frozenset()


def test_unknown_status_raises_value_error() -> None:
    """未知状态抛 ValueError（区别于非法迁移）。"""
    with pytest.raises(ValueError):
        allowed_actions("not_a_status")
    with pytest.raises(ValueError):
        next_status("not_a_status", "submit")


def test_transition_table_is_complete() -> None:
    """迁移表覆盖全部七个状态，且目标状态都在状态集合内。"""
    assert set(ALLOWED_TRANSITIONS) == {
        "draft",
        "submitted",
        "withdrawn",
        "approved",
        "finance_reviewed",
        "paid",
        "rejected",
    }
    for status, transitions in ALLOWED_TRANSITIONS.items():
        for action, target in transitions.items():
            assert target in ALLOWED_TRANSITIONS, (status, action, target)
