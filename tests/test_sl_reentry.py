import sys
import types
import unittest
from types import SimpleNamespace

sys.modules.setdefault("ccxt", types.SimpleNamespace(mexc=lambda *args, **kwargs: None))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))

from rebalancer import Rebalancer


class FakeClient:
    quote = "USDT"

    def __init__(
        self,
        order,
        free_balances=(0.0, 10.0),
        cancel_result=None,
        fetched_order=None,
    ):
        self.order = order
        self.free_balances = list(free_balances)
        self.cancel_result = cancel_result or {"cancelled": [], "errors": []}
        self.fetched_order = fetched_order
        self.sell_calls = []
        self.cancel_calls = []

    def get_all_prices(self, symbols):
        return {"GM": 0.9}

    def get_free_amount(self, symbol):
        if self.free_balances:
            return self.free_balances.pop(0)
        return 0.0

    def get_total_amount(self, symbol):
        return 10.0

    def cancel_all_open_sells(self, symbol):
        self.cancel_calls.append(symbol)
        return self.cancel_result

    def create_market_order(self, symbol, side, amount):
        self.sell_calls.append((symbol, side, amount))
        return self.order

    def fetch_order(self, order_id, symbol):
        return self.fetched_order


def make_position():
    return SimpleNamespace(
        id=7,
        symbol="GM",
        position_status="tp1_hit",
        remaining_amount=5.0,
        amount=10.0,
        entry_price=1.0,
        current_sl_price=0.95,
        tp1_price=0.0,
        tp1_order_id="tp-1",
        tp2_order_id=None,
        tp3_order_id=None,
        reentry_price=0.0,
        reentry_touched=False,
        reentry_used=False,
    )


class StopLossReentryTests(unittest.TestCase):
    def test_sells_only_remaining_tracked_amount_after_tp_cancel(self):
        client = FakeClient({"id": "sell-1", "status": "closed", "filled": 5.0})
        actions = Rebalancer(client).check_and_manage_positions([make_position()])

        self.assertEqual(len(client.sell_calls), 1)
        self.assertAlmostEqual(client.sell_calls[0][2], 5.0)
        self.assertEqual(actions[0]["action"], "sl_hit_sold")
        self.assertTrue(actions[0]["reentry_available"])

    def test_market_order_failure_does_not_claim_a_successful_stop(self):
        client = FakeClient(None)
        actions = Rebalancer(client).check_and_manage_positions([make_position()])

        self.assertEqual(actions[0]["action"], "sl_sell_failed")

    def test_unfilled_market_order_does_not_enable_reentry(self):
        client = FakeClient({"id": "sell-open", "status": "open", "filled": 0.0})
        actions = Rebalancer(client).check_and_manage_positions([make_position()])

        self.assertEqual(actions[0]["action"], "sl_sell_failed")

    def test_partial_market_order_does_not_enable_reentry(self):
        client = FakeClient({"id": "sell-partial", "status": "closed", "filled": 2.0})
        actions = Rebalancer(client).check_and_manage_positions([make_position()])

        self.assertEqual(actions[0]["action"], "sl_sell_failed")

    def test_cancellation_error_prevents_market_sell(self):
        client = FakeClient(
            {"id": "sell-1", "status": "closed", "filled": 4.99},
            cancel_result={"cancelled": [], "errors": [{"error": "exchange timeout"}]},
        )
        actions = Rebalancer(client).check_and_manage_positions([make_position()])

        self.assertEqual(actions[0]["action"], "sl_sell_failed")
        self.assertEqual(client.sell_calls, [])


if __name__ == "__main__":
    unittest.main()
