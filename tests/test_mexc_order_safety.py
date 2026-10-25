import sys
import types
import unittest
from unittest.mock import Mock

# The tests instantiate MexcClient without constructing a live ccxt exchange.
sys.modules.setdefault("ccxt", types.ModuleType("ccxt"))
from mexc_client import MexcClient


class MexcOrderSafetyTests(unittest.TestCase):
    def client_with_exchange(self, create_error=None):
        client = MexcClient.__new__(MexcClient)
        client.quote = "USDT"
        exchange = Mock()
        exchange.markets = {"ABC/USDT": {"limits": {"amount": {"min": 0.001}, "cost": {"min": 1}}}}
        exchange.market.return_value = exchange.markets["ABC/USDT"]
        exchange.amount_to_precision.return_value = "1.0"
        exchange.fetch_ticker.return_value = {"last": 10}
        if create_error:
            exchange.create_order.side_effect = create_error
        else:
            exchange.create_order.return_value = {"id": "123", "status": "closed", "filled": 1.0}
        client.exchange = exchange
        return client

    def test_generic_filter_failure_is_not_misclassified_as_dust(self):
        client = self.client_with_exchange(Exception("filter failure: unknown exchange rule"))
        with self.assertRaisesRegex(Exception, "submission state may be unknown"):
            client.create_market_order("ABC/USDT", "sell", 1.0)
        # An ambiguous submission is surfaced, never retried automatically.
        client.exchange.create_order.assert_called_once()

    def test_explicit_minimum_amount_rejection_is_treated_as_dust(self):
        client = self.client_with_exchange(Exception("minimum amount not met"))
        self.assertIsNone(client.create_market_order("ABC/USDT", "sell", 1.0))
        # Explicit minimum rejection is classified once, not retried.
        client.exchange.create_order.assert_called_once()

    def test_market_sell_empty_response_is_ambiguous_not_dust(self):
        client = self.client_with_exchange()
        client.exchange.create_order.return_value = None
        with self.assertRaisesRegex(Exception, "submission state may be unknown"):
            client.create_market_order("ABC/USDT", "sell", 1.0)
        client.exchange.create_order.assert_called_once()


    def test_limit_buy_is_sized_from_usdt_budget_and_limit_price(self):
        client = self.client_with_exchange()
        client.exchange.price_to_precision.return_value = "10.0"
        client.exchange.amount_to_precision.return_value = "2.0"
        client.exchange.create_order.return_value = {"id": "limit-123", "status": "open"}

        order = client.create_limit_buy_usdt("ABC", 20.0, 10.0)

        self.assertEqual(order["id"], "limit-123")
        client.exchange.create_order.assert_called_once_with(
            symbol="ABC/USDT", type="limit", side="buy", amount=2.0, price=10.0
        )

    def test_limit_buy_respects_exchange_minimum_notional(self):
        client = self.client_with_exchange()
        client.exchange.price_to_precision.return_value = "10.0"
        client.exchange.amount_to_precision.return_value = "0.05"
        client.exchange.markets["ABC/USDT"]["limits"]["cost"]["min"] = 1.0

        self.assertIsNone(client.create_limit_buy_usdt("ABC", 0.5, 10.0))
        client.exchange.create_order.assert_not_called()

    def test_limit_buy_explicit_minimum_rejection_is_not_retried(self):
        client = self.client_with_exchange(Exception("minimum amount not met"))
        client.exchange.price_to_precision.return_value = "10.0"
        client.exchange.amount_to_precision.return_value = "1.0"

        self.assertIsNone(client.create_limit_buy_usdt("ABC", 10.0, 10.0))
        client.exchange.create_order.assert_called_once()

    def test_limit_buy_timeout_is_not_retried(self):
        client = self.client_with_exchange(TimeoutError("network timeout"))
        client.exchange.price_to_precision.return_value = "10.0"
        client.exchange.amount_to_precision.return_value = "1.0"

        with self.assertRaisesRegex(Exception, "Limit buy failed"):
            client.create_limit_buy_usdt("ABC", 10.0, 10.0)
        client.exchange.create_order.assert_called_once()

    def test_limit_buy_empty_response_is_ambiguous_not_rejected(self):
        client = self.client_with_exchange()
        client.exchange.price_to_precision.return_value = "10.0"
        client.exchange.amount_to_precision.return_value = "1.0"
        client.exchange.create_order.return_value = None

        with self.assertRaisesRegex(Exception, "Limit buy failed"):
            client.create_limit_buy_usdt("ABC", 10.0, 10.0)
        client.exchange.create_order.assert_called_once()

    def test_fetch_order_strict_propagates_api_errors(self):
        client = self.client_with_exchange()
        client.exchange.fetch_order.side_effect = TimeoutError("network timeout")
        self.assertIsNone(client.fetch_order("123", "ABC/USDT"))
        with self.assertRaisesRegex(TimeoutError, "network timeout"):
            client.fetch_order("123", "ABC/USDT", strict=True)


if __name__ == "__main__":
    unittest.main()
