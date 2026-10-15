import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


# The rebalancer only needs the MexcClient type annotation at import time.
fake_mexc_module = types.ModuleType("mexc_client")
fake_mexc_module.MexcClient = object
sys.modules.setdefault("mexc_client", fake_mexc_module)

from rebalancer import Rebalancer


class FakeClient:
    quote = "USDT"

    def __init__(self):
        self.price = 110.0
        self.order = None
        self.order_to_create = {
            "id": "market-1",
            "status": "closed",
            "filled": 1.0,
            "average": 110.0,
        }
        self.market_orders = []
        self.cancelled = []
        self.free_amounts = [1.0, 0.7]
        self.exchange = None
        self.sell_price = None

    def get_all_prices(self, symbols):
        return {symbol: self.price for symbol in symbols}

    def get_all_sell_prices(self, symbols):
        price = self.sell_price if self.sell_price is not None else self.price
        return {symbol: price for symbol in symbols}

    def fetch_order(self, order_id, symbol):
        return self.order

    def create_market_order(self, symbol, side, amount):
        self.market_orders.append((symbol, side, amount))
        return dict(self.order_to_create)

    def cancel_order(self, order_id, symbol, strict=False):
        self.cancelled.append((order_id, symbol))

    def fetch_open_sell_orders(self, symbol):
        return []

    def get_total_amount(self, symbol):
        return 1.0

    def get_free_amount(self, symbol):
        if len(self.free_amounts) > 1:
            return self.free_amounts.pop(0)
        return self.free_amounts[0]

    def get_ticker_price(self, symbol):
        return self.price


def position(**overrides):
    defaults = {
        "id": 7,
        "symbol": "ABC",
        "position_status": "open",
        "amount": 1.0,
        "remaining_amount": 1.0,
        "entry_price": 100.0,
        "tp1_price": 110.0,
        "tp2_price": 120.0,
        "tp3_price": 0.0,
        "tp1_sell_pct": 40.0,
        "tp2_sell_pct": 40.0,
        "tp1_order_id": None,
        "tp2_order_id": None,
        "tp3_order_id": None,
        "tp_order_id": None,
        "current_sl_price": 90.0,
        "reentry_price": 0.0,
        "reentry_touched": False,
        "portfolio": SimpleNamespace(
            tp1_sell_pct=40.0, tp2_sell_pct=40.0, trail_pct=2.0
        ),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class PortfolioTakeProfitStopLossTests(unittest.TestCase):
    def test_cloud_tp_does_not_duplicate_current_exchange_order(self):
        client = FakeClient()
        reb = Rebalancer(client)
        coin = position(tp1_order_id="limit-tp1")

        actions = reb.sync_cloud_tp([coin])

        self.assertEqual(actions, [])
        self.assertEqual(client.market_orders, [])

    def test_cloud_tp_uses_confirmed_partial_fill_amount(self):
        client = FakeClient()
        client.order_to_create = {
            "id": "market-1",
            "status": "closed",
            "filled": 0.2,
            "average": 110.0,
        }
        reb = Rebalancer(client)
        coin = position()

        actions = reb.sync_cloud_tp([coin])

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["filled_amount"], 0.2)
        self.assertEqual(actions[0]["requested_amount"], 0.4)
        self.assertTrue(actions[0]["partial_fill"])

    def test_cloud_tp_uses_portfolio_setting_without_nonexistent_coin_columns(self):
        client = FakeClient()
        reb = Rebalancer(client)
        coin = position(portfolio=SimpleNamespace(tp1_sell_pct=25, tp2_sell_pct=35))
        del coin.tp1_sell_pct
        del coin.tp2_sell_pct

        actions = reb.sync_cloud_tp([coin])

        self.assertEqual(len(actions), 1)
        self.assertAlmostEqual(actions[0]["requested_amount"], 0.25)
        self.assertEqual(client.market_orders[0][2], 0.25)

    def test_cloud_tp_triggers_on_sellable_bid_not_last_price(self):
        client = FakeClient()
        client.price = 111.0
        client.sell_price = 109.9
        reb = Rebalancer(client)

        actions = reb.sync_cloud_tp([position()])

        self.assertEqual(actions, [])
        self.assertEqual(client.market_orders, [])

    def test_manual_tp_closed_order_is_reconciled(self):
        client = FakeClient()
        client.order = {
            "id": "limit-tp1",
            "status": "closed",
            "filled": 0.35,
            "average": 110.0,
        }
        reb = Rebalancer(client)
        coin = position(tp1_order_id="limit-tp1")

        actions = reb.sync_manual_tp_orders([coin])

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "tp1_hit")
        self.assertEqual(actions[0]["filled_amount"], 0.35)

    def test_cancelled_manual_tp_partial_fill_is_not_lost(self):
        client = FakeClient()
        client.order = {
            "id": "limit-tp1",
            "status": "canceled",
            "filled": 0.15,
            "average": 110.0,
        }
        reb = Rebalancer(client)
        coin = position(tp1_order_id="limit-tp1")

        actions = reb.sync_manual_tp_orders([coin])

        self.assertEqual(actions[0]["action"], "manual_tp_partial")
        self.assertEqual(actions[0]["filled_amount"], 0.15)
        self.assertEqual(actions[0]["clear_order_key"], "tp1_order_id")

    def test_stop_loss_subtracts_tp_fill_racing_with_cancel(self):
        client = FakeClient()
        client.price = 89.0
        client.order = {
            "id": "limit-tp1",
            "status": "canceled",
            "filled": 0.3,
            "average": 110.0,
        }
        client.order_to_create = {
            "id": "market-sl",
            "status": "closed",
            "filled": 0.7,
            "average": 89.0,
        }
        reb = Rebalancer(client)
        coin = position(tp1_order_id="limit-tp1")

        with patch("rebalancer.time.sleep", return_value=None):
            actions = reb.check_and_manage_positions([coin])

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "sl_hit_sold")
        self.assertAlmostEqual(actions[0]["amount"], 0.7)
        self.assertEqual(actions[0]["tp_fills"][0]["filled_amount"], 0.3)
        self.assertEqual(client.market_orders[0][2], 0.7)

    def test_stop_loss_cancels_legacy_untracked_sell_orders(self):
        client = FakeClient()
        client.price = 89.0
        client.order_to_create = {
            "id": "market-sl",
            "status": "closed",
            "filled": 1.0,
            "average": 89.0,
        }
        client.free_amounts = [1.0]
        stale = [{"id": "legacy-1", "side": "sell"}, {"id": "legacy-2", "side": "sell"}]
        client.fetch_open_sell_orders = lambda symbol: (
            stale if len(client.cancelled) < len(stale) else []
        )
        reb = Rebalancer(client)
        with patch("rebalancer.time.sleep", return_value=None):
            actions = reb.check_and_manage_positions([position()])
        self.assertEqual(actions[0]["action"], "sl_hit_sold")
        self.assertIn(("legacy-1", "ABC/USDT"), client.cancelled)
        self.assertIn(("legacy-2", "ABC/USDT"), client.cancelled)

    def test_stop_trails_up_after_tp2_and_never_sets_above_market(self):
        client = FakeClient()
        client.price = 120.0
        client.free_amounts = [1.0]
        reb = Rebalancer(client)
        coin = position(
            position_status="tp2_hit",
            current_sl_price=105.0,
            remaining_amount=0.2,
        )

        actions = reb.check_and_manage_positions([coin])

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "sl_trail_updated")
        self.assertAlmostEqual(actions[0]["new_sl"], 117.6)
        self.assertLess(actions[0]["new_sl"], actions[0]["price"])


if __name__ == "__main__":
    unittest.main()
