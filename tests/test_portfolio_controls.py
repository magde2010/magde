import unittest
from types import SimpleNamespace

from portfolio_controls import (
    calculate_outstanding_trim_amounts,
    calculate_safe_trim_amount,
)


class PortfolioReductionTests(unittest.TestCase):
    def test_trim_is_capped_by_percentage_and_free_wallet_amount(self):
        amount = calculate_safe_trim_amount(
            remaining_amount=10.0,
            pct=50,
            free_amount=3.0,
            price=10.0,
            min_notional=2.0,
        )
        self.assertAlmostEqual(amount, 2.994)
        self.assertLessEqual(amount, 5.0)
        self.assertLessEqual(amount, 3.0)

    def test_restore_ledger_nets_only_manual_trim_events(self):
        events = [
            SimpleNamespace(event_type="manual_trim", amount=2.0,
                            portfolio_coin_id=3, symbol="abc"),
            SimpleNamespace(event_type="manual_trim_restore", amount=0.5,
                            portfolio_coin_id=3, symbol="ABC"),
            SimpleNamespace(event_type="stop_loss", amount=1.0,
                            portfolio_coin_id=3, symbol="ABC"),
        ]
        self.assertEqual(
            calculate_outstanding_trim_amounts(events),
            {(3, "ABC"): 1.5},
        )


if __name__ == "__main__":
    unittest.main()