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
        self.limit_sells = []
        self.cancelled = []
        self.free_amounts = [1.0, 0.7]
        self.exchange = None
        self.sell_price = None

    def get_all_prices(self, symbols):
        return {symbol: self.price for symbol in symbols}

    def get_all_sell_prices(self, symbols):
        price = self.sell_price if self.sell_price is not None else self.price
        return {symbol: price for symbol in symbols}

    def fetch_order(self, order_id, symbol, strict=False):
        return self.order

    def create_market_order(self, symbol, side, amount):
        self.market_orders.append((symbol, side, amount))
        return dict(self.order_to_create)

    def create_limit_sell(self, symbol, amount, price):
        self.limit_sells.append((symbol, amount, price))
        return {"id": f"tp-{len(self.limit_sells)}", "status": "open", "amount": amount, "price": price}

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
    def test_tp_cancel_failure_is_reported_and_not_counted_as_cancelled(self):
        client = FakeClient()
        def fail_cancel(order_id, symbol, strict=False):
            raise TimeoutError("exchange timeout")
        client.cancel_order = fail_cancel
        result = Rebalancer(client).cancel_tp_orders([{
            "symbol": "ABC", "tp1_order_id": "tp-live-1",
        }])
        self.assertEqual(result["cancelled"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["order_id"], "tp-live-1")

    def test_market_sell_confirmation_uses_actual_partial_fill(self):
        client = FakeClient()
        reb = Rebalancer(client)
        filled = reb._confirm_market_sell(
            "ABC/USDT",
            {"id": "sell-partial", "status": "closed", "filled": 0.35},
            1.0,
        )
        self.assertAlmostEqual(filled, 0.35)

    def test_market_sell_waits_for_terminal_state_before_counting_partial_fill(self):
        client = FakeClient()
        client.order = {"id": "sell-poll", "status": "closed", "filled": 0.4}
        reb = Rebalancer(client)
        with patch("rebalancer.time.sleep", return_value=None):
            filled = reb._confirm_market_sell(
                "ABC/USDT",
                {"id": "sell-poll", "status": "open", "filled": 0.2},
                1.0,
            )
        self.assertAlmostEqual(filled, 0.4)

    def test_market_sell_still_open_is_unresolved_even_if_partially_filled(self):
        client = FakeClient()
        client.order = {"id": "sell-live", "status": "open", "filled": 0.3}
        reb = Rebalancer(client)
        with patch("rebalancer.time.sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                reb._confirm_market_sell(
                    "ABC/USDT",
                    {"id": "sell-live", "status": "open", "filled": 0.3},
                    1.0,
                )

    def test_market_sell_without_confirmed_fill_is_not_counted(self):
        client = FakeClient()
        client.order = {"id": "sell-unknown", "status": "open", "filled": 0.0}
        reb = Rebalancer(client)
        with patch("rebalancer.time.sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                reb._confirm_market_sell(
                    "ABC/USDT",
                    {"id": "sell-unknown", "status": "open", "filled": 0.0},
                    1.0,
                )

    def test_cloud_tp_does_not_duplicate_current_exchange_order(self):
        client = FakeClient()
        reb = Rebalancer(client)
        coin = position(tp1_order_id="limit-tp1")

        actions = reb.sync_cloud_tp([coin])

        self.assertEqual(actions, [])
        self.assertEqual(client.market_orders, [])

    def test_cloud_tp_never_falls_back_to_market_sell(self):
        client = FakeClient()
        reb = Rebalancer(client)
        actions = reb.sync_cloud_tp([position()])
        self.assertEqual(actions, [])
        self.assertEqual(client.market_orders, [])

    def test_invalid_tp_ordering_places_no_exchange_orders(self):
        client = FakeClient()
        result = Rebalancer(client).place_tp_orders(
            [{"symbol": "ABC", "amount": 1.0, "entry_price": 100.0}],
            tp1_pct=8, tp2_pct=5, tp3_pct=10, stop_loss_pct=0,
            tp1_sell_pct=40, tp2_sell_pct=40,
        )[0]
        self.assertIn("غير مرتبة", result["error"])
        self.assertEqual(client.limit_sells, [])

    def test_place_tp_orders_creates_exchange_limit_slices_and_zero_sl_disables_stop(self):
        client = FakeClient()
        reb = Rebalancer(client)
        result = reb.place_tp_orders(
            [{"symbol": "ABC", "amount": 1.0, "entry_price": 100.0}],
            tp1_pct=3, tp2_pct=5, tp3_pct=8, stop_loss_pct=0,
            tp1_sell_pct=40, tp2_sell_pct=40,
        )[0]
        self.assertEqual(result["sl_price"], 0.0)
        self.assertEqual(result["tp1_order_id"], "tp-1")
        self.assertEqual(result["tp2_order_id"], "tp-2")
        self.assertEqual(result["tp3_order_id"], "tp-3")
        self.assertAlmostEqual(client.limit_sells[0][1], 0.4)
        self.assertAlmostEqual(client.limit_sells[1][1], 0.4)
        self.assertAlmostEqual(client.limit_sells[2][1], 0.2)
        self.assertEqual(client.market_orders, [])

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

    def test_manual_tp_closed_order_without_filled_amount_is_ignored(self):
        client = FakeClient()
        client.order = {
            "id": "limit-tp1",
            "status": "closed",
            "filled": None,
            "average": 110.0,
        }
        reb = Rebalancer(client)
        coin = position(tp1_order_id="limit-tp1")

        actions = reb.sync_manual_tp_orders([coin])

        self.assertEqual(actions, [])

    def test_nonzero_dust_balance_is_not_auto_closed(self):
        client = FakeClient()
        client.price = 100.0
        client.get_total_amount = lambda symbol: 0.0001
        client.get_free_amount = lambda symbol: 0.0001
        reb = Rebalancer(client)

        actions = reb.check_and_manage_positions([position()])

        self.assertEqual(actions, [])
        self.assertEqual(client.market_orders, [])

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

    def test_stop_loss_refuses_to_cancel_untracked_sell_orders(self):
        client = FakeClient()
        client.price = 89.0
        client.order_to_create = {
            "id": "market-sl",
            "status": "closed",
            "filled": 1.0,
            "average": 89.0,
        }
        client.free_amounts = [1.0]
        stale = [{"id": "manual-1", "side": "sell"}]
        client.fetch_open_sell_orders = lambda symbol: stale
        reb = Rebalancer(client)
        with patch("rebalancer.time.sleep", return_value=None):
            actions = reb.check_and_manage_positions([position()])
        self.assertEqual(actions[0]["action"], "sl_sell_failed")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(client.market_orders, [])
        self.assertIn("Untracked open sell order", actions[0]["error"])

    def test_stop_portfolio_refuses_to_liquidate_wallet_without_allocation(self):
        client = FakeClient()
        reb = Rebalancer(client)

        result = reb.stop_portfolio(["ABC"])

        self.assertEqual(client.market_orders, [])
        self.assertEqual(result["executed"], [])
        self.assertTrue(result["errors"])

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
