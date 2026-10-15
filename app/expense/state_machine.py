"""报销单状态机（纯逻辑，不触碰数据库）。

状态与动作由 Master 在任务 W1-6 中固定，**不得增删**：

* ``draft --submit--> submitted``
* ``submitted --approve--> approved``；``submitted --return--> draft``（退回补件）；
  ``submitted --reject--> rejected``；``submitted --withdraw--> withdrawn``
* ``withdrawn --submit--> submitted``
* ``approved --finance_review--> finance_reviewed``；``approved --return--> draft``；
  ``approved --reject--> rejected``
* ``finance_reviewed --pay--> paid``
* ``paid`` 与 ``rejected`` 为终态（无可用动作）

状态转换必须由确定性程序驱动（``docs/PROJECT-CHARTER.md`` 强制原则 6），因此这里
把迁移表与判定函数集中在一处，供服务层与测试共用。本模块**不导入任何数据库
依赖**，可独立单测。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

#: 状态 → {动作 → 目标状态}。只读视图，防止调用方意外改写全局迁移表。
ALLOWED_TRANSITIONS: Final = MappingProxyType(
    {
        "draft": MappingProxyType({"submit": "submitted"}),
        "submitted": MappingProxyType(
            {
                "approve": "approved",
                "return": "draft",
                "reject": "rejected",
                "withdraw": "withdrawn",
            }
        ),
        "withdrawn": MappingProxyType({"submit": "submitted"}),
        "approved": MappingProxyType(
            {
                "finance_review": "finance_reviewed",
                "return": "draft",
                "reject": "rejected",
            }
        ),
        "finance_reviewed": MappingProxyType({"pay": "paid"}),
        "paid": MappingProxyType({}),
        "rejected": MappingProxyType({}),
    }
)


class InvalidTransitionError(Exception):
    """在某个状态下执行了不被允许的动作。

    异常信息必须包含**当前状态**与**该状态下允许的动作列表**，便于调用方与
    审计日志直接定位问题。
    """

    def __init__(self, status: str, action: str, allowed: frozenset[str]) -> None:
        self.status = status
        self.action = action
        self.allowed = allowed
        allowed_repr = ", ".join(sorted(allowed)) if allowed else "(none)"
        super().__init__(
            f"invalid transition: action {action!r} is not allowed from status "
            f"{status!r}; allowed actions: {allowed_repr}"
        )


def _transitions_for(status: str) -> MappingProxyType[str, str]:
    """返回 ``status`` 的动作映射；未知状态抛 ``ValueError``。"""
    try:
        return ALLOWED_TRANSITIONS[status]
    except KeyError:
        known = ", ".join(sorted(ALLOWED_TRANSITIONS))
        raise ValueError(
            f"unknown claim status {status!r}; known statuses: {known}"
        ) from None


def allowed_actions(status: str) -> frozenset[str]:
    """返回 ``status`` 下允许的动作集合；未知状态抛 ``ValueError``。

    终态（``paid``/``rejected``）返回空 ``frozenset``。
    """
    return frozenset(_transitions_for(status))


def next_status(status: str, action: str) -> str:
    """按状态机计算 ``(status, action)`` 的目标状态。

    未知 ``status`` 抛 ``ValueError``；非法 ``action`` 抛
    ``InvalidTransitionError``（信息含当前状态与允许动作列表）。
    """
    transitions = _transitions_for(status)
    try:
        return transitions[action]
    except KeyError:
        raise InvalidTransitionError(status, action, frozenset(transitions)) from None


__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidTransitionError",
    "allowed_actions",
    "next_status",
]
