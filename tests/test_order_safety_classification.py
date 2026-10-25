import unittest

from order_safety import classify_dca_submission, classify_market_sell_submission


class OrderSafetyClassificationTests(unittest.TestCase):
    def test_dca_none_is_confirmed_rejection_not_active_order(self):
        self.assertEqual(classify_dca_submission(None), ("rejected", None))

    def test_dca_missing_id_is_ambiguous_and_never_retryable(self):
        self.assertEqual(classify_dca_submission({"status": "open"}), ("manual_review", None))

    def test_dca_valid_id_is_open(self):
        self.assertEqual(classify_dca_submission({"id": "abc-1"}), ("open", "abc-1"))

    def test_market_sell_none_is_dust_not_a_sale(self):
        self.assertEqual(classify_market_sell_submission(None), ("dust", None))

    def test_market_sell_missing_id_is_ambiguous_not_dust(self):
        self.assertEqual(classify_market_sell_submission({"status": "closed", "filled": 0}), ("manual_review", None))

    def test_market_sell_valid_id_requires_reconciliation(self):
        self.assertEqual(classify_market_sell_submission({"id": "sell-42"}), ("submitted", "sell-42"))


if __name__ == "__main__":
    unittest.main()
