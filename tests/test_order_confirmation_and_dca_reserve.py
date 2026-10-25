import unittest
from unittest.mock import Mock, patch

from portfolio_strategy import allocate_dca_order_slots, match_unidentified_dca_order
from rebalancer import Rebalancer


class BudgetReservationTests(unittest.TestCase):
    def test_175_usdt_portfolio_reserves_only_minimum_sized_dca_levels(self):
        # 50% of $175 is $87.50 initial buys and $87.50 DCA in this plan;
        # use the reported $52.50 reserve example to reproduce the 1.75/order bug.
        plan = allocate_dca_order_slots(52.5, 30, 2.0)
        self.assertEqual(plan["eligible_slots"], 26)
        self.assertEqual(plan["skipped_slots"], 4)
        self.assertGreaterEqual(plan["per_order_usdt"], 2.0)
        self.assertLessEqual(plan["budget_used"], 52.5 + 1e-9)

    def test_insufficient_budget_creates_no_subminimum_order(self):
        plan = allocate_dca_order_slots(1.99, 3, 2.0)
        self.assertEqual(plan["eligible_slots"], 0)
        self.assertEqual(plan["skipped_slots"], 3)
        self.assertEqual(plan["per_order_usdt"], 0.0)

    def test_missing_order_id_reconciliation_requires_one_unique_match(self):
        created = 1_800_000_000_000
        candidate = {
            "id": "market-1", "side": "buy", "timestamp": created + 1000,
            "cost": 10.02, "filled": 1.0, "average": 10.02,
        }
        self.assertEqual(match_unidentified_dca_order([candidate], created, 10.0), candidate)
        self.assertIsNone(match_unidentified_dca_order([candidate, dict(candidate, id="market-2")], created, 10.0))
        self.assertIsNone(match_unidentified_dca_order([], created, 10.0))

    def test_missing_id_can_match_unfilled_limit_order_using_amount_times_price(self):
        created = 1_800_000_000_000
        open_limit = {
            "id": "limit-42", "side": "buy", "timestamp": created + 2000,
            "status": "open", "cost": 0.0, "filled": 0.0,
            "amount": 0.5, "price": 20.0,
        }
        self.assertEqual(match_unidentified_dca_order([open_limit], created, 10.0), open_limit)

    def test_missing_id_uses_dca_limit_price_to_distinguish_levels(self):
        created = 1_800_000_000_000
        right_price = {
            "id": "dc2-order", "side": "buy", "timestamp": created + 1000,
            "status": "open", "cost": 0.0, "filled": 0.0,
            "amount": 0.5, "price": 20.0,
        }
        same_budget_wrong_level = {
            "id": "manual-order", "side": "buy", "timestamp": created + 1000,
            "status": "open", "cost": 0.0, "filled": 0.0,
            "amount": 0.47619, "price": 21.0,
        }
        self.assertEqual(
            match_unidentified_dca_order(
                [same_budget_wrong_level, right_price], created, 10.0,
                expected_price=20.0,
            ),
            right_price,
        )
        self.assertIsNone(
            match_unidentified_dca_order(
                [same_budget_wrong_level], created, 10.0,
                expected_price=20.0,
            )
        )

    def test_missing_id_does_not_reuse_an_order_already_linked_to_another_row(self):
        created = 1_800_000_000_000
        candidate = {
            "id": "limit-42", "side": "buy", "timestamp": created + 1000,
            "status": "open", "cost": 0.0, "filled": 0.0,
            "amount": 0.5, "price": 20.0,
        }
        self.assertIsNone(match_unidentified_dca_order(
            [candidate], created, 10.0, exclude_order_ids={"limit-42"}
        ))


class FakeBuyClient:
    quote = "USDT"

    def __init__(self, initial_order, reconciled_order=None):
        self.initial_order = initial_order
        self.reconciled_order = reconciled_order or initial_order
        self.exchange = None
        self.fetches = 0
        self.tp_calls = []

    def get_free_usdt(self):
        return 100.0

    def create_market_buy_usdt(self, symbol, usdt):
        return dict(self.initial_order) if self.initial_order is not None else None

    def fetch_order(self, order_id, symbol, strict=False):
        self.fetches += 1
        if self.reconciled_order is None:
            return None
        return dict(self.reconciled_order)

    def get_total_amount(self, symbol):
        return 0.0

    def get_ticker_price(self, symbol):
        return 10.0


class BuyConfirmationTests(unittest.TestCase):
    def test_start_portfolio_marks_only_confirmed_fill_as_executed(self):
        client = FakeBuyClient(
            {"id": "buy-1", "status": "open", "amount": 1.0},
            {"id": "buy-1", "status": "closed", "filled": 0.4, "amount": 1.0},
        )
        result = Rebalancer(client).start_portfolio(["ABC"], 10.0)
        self.assertEqual(len(result["executed"]), 1)
        self.assertEqual(result["executed"][0]["status"], "filled")
        self.assertAlmostEqual(result["executed"][0]["filled_amount"], 0.4)

    def test_start_portfolio_does_not_claim_fill_when_exchange_quantity_missing(self):
        client = FakeBuyClient(
            {"id": "buy-2", "status": "open", "amount": 1.0},
            {"id": "buy-2", "status": "closed", "amount": 1.0},
        )
        result = Rebalancer(client).start_portfolio(["ABC"], 10.0)
        self.assertEqual(result["executed"], [])
        self.assertTrue(result.get("pending_confirmation"))
        self.assertIn("manual_review", result["pending_confirmation"][0]["status"])

    def test_reentry_never_uses_requested_amount_when_fill_is_unconfirmed(self):
        client = FakeBuyClient(
            {"id": "buy-3", "status": "open", "amount": 1.0},
            {"id": "buy-3", "status": "closed", "amount": 1.0},
        )
        reb = Rebalancer(client)
        reb.place_tp_orders = Mock(side_effect=lambda *args, **kwargs: client.tp_calls.append(args) or [])
        with patch("rebalancer.time.sleep", return_value=None):
            result = reb.reentry_buy_and_place_tp("ABC", 10.0, 3, 5, 8, 3)
        self.assertTrue(result.get("manual_review"))
        self.assertIsNotNone(result.get("error"))
        self.assertEqual(client.tp_calls, [])

    def test_reentry_uses_confirmed_filled_quantity_not_order_amount(self):
        client = FakeBuyClient(
            {"id": "buy-4", "status": "closed", "filled": 0.25, "amount": 1.0},
        )
        reb = Rebalancer(client)
        captured = {}
        def fake_place(items, *args, **kwargs):
            captured["amount"] = items[0]["amount"]
            return [{"symbol": "ABC", "tp1_price": 11.0}]
        reb.place_tp_orders = fake_place
        result = reb.reentry_buy_and_place_tp("ABC", 10.0, 3, 5, 8, 3)
        self.assertAlmostEqual(captured["amount"], 0.25 * 0.998)
        self.assertIsNone(result.get("error"))


if __name__ == "__main__":
    unittest.main()
