"""金额工具：用 ``decimal.Decimal`` 做确定性求和，**禁止浮点**。

财务金额必须由确定性程序计算（``docs/PROJECT-CHARTER.md`` 强制原则 6），且
``templates/PLAN.md`` §8 要求「金额采用固定精度 decimal，不用浮点数保存财务
金额」。因此本模块：

* 只接受 ``Decimal``（或可由 ``Decimal`` 精确构造的 ``int``/``str``），
  **拒绝 ``float``**——``0.1 + 0.2`` 这类二进制浮点会得到 ``0.30000000000000004``；
* 结果量化到 2 位小数（``ROUND_HALF_UP``，与财务四舍五入习惯一致）。

模块内**不出现 ``float`` 字面量或转换**。
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

#: 金额统一保留 2 位小数。
MONEY_EXPONENT = Decimal("0.01")


def to_decimal(value: Decimal | int | str) -> Decimal:
    """把 ``value`` 转为 ``Decimal``；``float`` 一律拒绝。

    ``float`` 无法无损表达大多数十进制金额，接受它会静默引入二进制误差，因此
    这里显式拒绝并抛出 ``TypeError``。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，但用作金额几乎总是笔误。
        raise TypeError("bool is not a valid money value")
    if isinstance(value, float):
        raise TypeError(
            "float is not allowed for money; use Decimal/int/str to keep "
            "fixed-point precision"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) or isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"unsupported money type: {type(value).__name__}")


def quantize(amount: Decimal) -> Decimal:
    """把 ``amount`` 量化到 2 位小数（四舍五入）。"""
    return to_decimal(amount).quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def sum_amounts(amounts: Iterable[Decimal | int | str]) -> Decimal:
    """对一组金额做精确求和，结果量化到 2 位小数。

    空输入返回 ``Decimal('0.00')``。任一元素为 ``float`` 抛 ``TypeError``。
    """
    total = Decimal("0")
    for amount in amounts:
        total += to_decimal(amount)
    return quantize(total)


__all__ = ["MONEY_EXPONENT", "quantize", "sum_amounts", "to_decimal"]
