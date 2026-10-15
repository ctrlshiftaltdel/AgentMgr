"""金额工具单元测试：固定精度 Decimal 求和，拒绝浮点。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.expense.money import MONEY_EXPONENT, quantize, sum_amounts, to_decimal


def test_sum_of_ten_and_twenty_is_exactly_thirty() -> None:
    """0.10 + 0.20 == 0.30（Decimal，绝无浮点误差）。"""
    result = sum_amounts([Decimal("0.10"), Decimal("0.20")])
    assert result == Decimal("0.30")
    assert isinstance(result, Decimal)
    # 显式排除二进制浮点会给出的 0.30000000000000004。
    assert str(result) == "0.30"


def test_float_is_rejected() -> None:
    """浮点金额一律拒绝，避免静默引入二进制误差。"""
    with pytest.raises(TypeError):
        sum_amounts([0.1, 0.2])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        to_decimal(1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_decimal(True)


def test_empty_sum_is_zero() -> None:
    assert sum_amounts([]) == Decimal("0.00")


def test_sum_quantizes_to_two_places() -> None:
    """结果量化到 2 位小数。"""
    assert sum_amounts([Decimal("1.005")]) == Decimal("1.01")
    assert quantize(Decimal("2.345")) == Decimal("2.35")
    assert MONEY_EXPONENT == Decimal("0.01")


def test_accepts_int_and_str() -> None:
    assert sum_amounts([1, "2.50", Decimal("3.50")]) == Decimal("7.00")


def test_module_source_has_no_float_literal() -> None:
    """源码中不得出现 ``float`` 用法（防御性静态检查）。"""
    import app.expense.money as money

    source = __import__("pathlib").Path(money.__file__).read_text(encoding="utf-8")
    assert "float(" not in source
    assert ": float" not in source
