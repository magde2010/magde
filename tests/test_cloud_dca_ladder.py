import unittest

from portfolio_strategy import shift_dca_level_prices


class CloudDcaLadderTests(unittest.TestCase):
    def test_profit_moves_every_waiting_level_up_one_rung(self):
        self.assertEqual(shift_dca_level_prices([97, 94, 91], 3, "up"), [100, 97, 94])

    def test_dca_fill_moves_every_waiting_level_down_one_rung(self):
        self.assertEqual(shift_dca_level_prices([97, 94, 91], 3, "down"), [94, 91, 88])

    def test_down_shift_never_crosses_stop(self):
        shifted = shift_dca_level_prices([97, 94, 91], 3, "down", stop_price=90)
        self.assertTrue(all(price > 90 for price in shifted))

    def test_single_remaining_level_still_moves(self):
        self.assertEqual(shift_dca_level_prices([94], 3, "up"), [97])


if __name__ == "__main__":
    unittest.main()


class DcaRangeWithoutStopTests(unittest.TestCase):
    def test_dca_levels_can_be_calculated_when_stop_loss_disabled(self):
        from portfolio_strategy import calculate_dca_prices
        self.assertEqual(calculate_dca_prices(100.0, 6.0, 2), [98.0, 96.0])
