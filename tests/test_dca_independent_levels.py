import pytest

from portfolio_strategy import (
    calculate_dca_level_budgets,
    normalize_dca_level_settings,
)


def test_default_dca_weights_split_reserve_by_level():
    weights, drops = normalize_dca_level_settings([50, 30, 20], [3, 6, 10])
    assert weights == [50.0, 30.0, 20.0]
    assert drops == [3.0, 6.0, 10.0]
    assert calculate_dca_level_budgets(300, 1, weights, 3) == [150.0, 90.0, 60.0]


def test_level_budgets_divide_each_rung_across_coins():
    assert calculate_dca_level_budgets(500, 2, [50, 30, 20], 3) == [125.0, 75.0, 50.0]


def test_disabled_levels_receive_no_budget():
    assert calculate_dca_level_budgets(300, 1, [50, 30, 20], 2) == [150.0, 90.0, 0.0]


def test_weights_cannot_exceed_reserve():
    with pytest.raises(ValueError, match="must not exceed 100"):
        normalize_dca_level_settings([50, 30, 21], [3, 6, 10])


def test_invalid_level_drop_is_rejected():
    with pytest.raises(ValueError, match="drop must be"):
        normalize_dca_level_settings([50, 30, 20], [0, 6, 10])


def test_drop_levels_must_be_ordered_from_near_to_deep():
    with pytest.raises(ValueError, match="must increase"):
        normalize_dca_level_settings([50, 30, 20], [6, 3, 10])
