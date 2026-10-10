import pytest

from portfolio_controls import calculate_portfolio_entry_budget, calculate_safe_trim_amount, calculate_outstanding_trim_amounts

# Rebalancer's exchange client import is type-only for these mocked unit tests.
import sys
import types
sys.modules.setdefault("ccxt", types.ModuleType("ccxt"))
from rebalancer import Rebalancer


@pytest.mark.parametrize("pct", [25, 50, 70, 100])
def test_entry_budget_is_percent_of_assigned_amount_for_missing_coins(pct):
    # 350 USDT, 10 coins, only 4 missing: budget per target coin scales with pct.
    assert calculate_portfolio_entry_budget(350, pct, 10, 4) == pytest.approx(350 * pct / 100 * 4 / 10)


def test_entry_budget_never_counts_existing_coins_as_new_purchases():
    assert calculate_portfolio_entry_budget(100, 50, 8, 0) == 0


def test_invalid_entry_percentage_is_rejected():
    with pytest.raises(ValueError):
        calculate_portfolio_entry_budget(100, 35, 4, 4)


@pytest.mark.parametrize("pct", [25, 50, 70])
def test_trim_amount_never_exceeds_chosen_fraction(pct):
    amount = calculate_safe_trim_amount(10, pct, 100, 1, 2)
    assert amount == pytest.approx(10 * pct / 100 * 0.998)


def test_trim_respects_free_balance_and_two_dollar_floor():
    assert calculate_safe_trim_amount(10, 70, 1, 10, 2) == pytest.approx(0.998)
    assert calculate_safe_trim_amount(10, 25, 100, 0.5, 2) == 0


class FakeClient:
    quote = "USDT"

    def __init__(self, free_usdt=10):
        self.free_usdt = free_usdt
        self.orders = []

    def get_free_usdt(self):
        return self.free_usdt

    def create_market_buy_usdt(self, symbol, usdt_amount):
        self.orders.append((symbol, float(usdt_amount)))
        return {"id": f"fake-{len(self.orders)}"}


def test_rebalancer_allows_two_dollar_orders(monkeypatch):
    import config
    monkeypatch.setattr(config, "PORTFOLIO_MIN_ORDER_USDT", 2.0)
    client = FakeClient(4.0)
    result = Rebalancer(client).start_portfolio(
        coins=["AAA", "BBB"], total_usdt=4.0, min_trade_usdt=2.0, dry_run=False
    )
    assert len(result["executed"]) == 2
    assert [amount for _, amount in client.orders] == pytest.approx([2.0, 2.0])


def test_rebalancer_refuses_budget_below_two_dollars(monkeypatch):
    import config
    monkeypatch.setattr(config, "PORTFOLIO_MIN_ORDER_USDT", 2.0)
    client = FakeClient(10.0)
    result = Rebalancer(client).start_portfolio(
        coins=["AAA"], total_usdt=1.99, min_trade_usdt=2.0, dry_run=False
    )
    assert not result["executed"]
    assert client.orders == []
    assert any("الحد الأدنى" in str(error) for error in result["errors"])


class TradeEvent:
    def __init__(self, event_type, amount, coin_id=1, symbol="AAA"):
        self.event_type = event_type
        self.amount = amount
        self.portfolio_coin_id = coin_id
        self.symbol = symbol


def test_restore_ledger_subtracts_already_restored_quantity():
    events = [
        TradeEvent("manual_trim", 2.0),
        TradeEvent("manual_trim", 1.0),
        TradeEvent("manual_trim_restore", 1.25),
        TradeEvent("tp1", 4.0),  # unrelated TP sells must not affect manual trim restoration
    ]
    result = calculate_outstanding_trim_amounts(events)
    assert result[(1, "AAA")] == pytest.approx(1.75)


def test_restore_ledger_keeps_separate_portfolio_positions_by_coin_id():
    events = [
        TradeEvent("manual_trim", 2.0, coin_id=1, symbol="AAA"),
        TradeEvent("manual_trim", 3.0, coin_id=2, symbol="AAA"),
        TradeEvent("manual_trim_restore", 1.0, coin_id=1, symbol="AAA"),
    ]
    result = calculate_outstanding_trim_amounts(events)
    assert result[(1, "AAA")] == pytest.approx(1.0)
    assert result[(2, "AAA")] == pytest.approx(3.0)
