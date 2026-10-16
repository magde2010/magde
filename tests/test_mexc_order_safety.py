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

    def test_explicit_minimum_amount_rejection_is_treated_as_dust(self):
        client = self.client_with_exchange(Exception("minimum amount not met"))
        self.assertIsNone(client.create_market_order("ABC/USDT", "sell", 1.0))

    def test_fetch_order_strict_propagates_api_errors(self):
        client = self.client_with_exchange()
        client.exchange.fetch_order.side_effect = TimeoutError("network timeout")
        self.assertIsNone(client.fetch_order("123", "ABC/USDT"))
        with self.assertRaisesRegex(TimeoutError, "network timeout"):
            client.fetch_order("123", "ABC/USDT", strict=True)


if __name__ == "__main__":
    unittest.main()
