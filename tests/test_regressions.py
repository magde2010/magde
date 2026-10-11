"""Regression tests for the MEXC SPOT bot fixes.

Run after installing requirements with:
    python -m unittest discover -s tests -v
All exchange/Telegram calls are mocked; tests do not place live orders.
"""
import os
import tempfile
import unittest
from pathlib import Path

_TEST_DATA = tempfile.mkdtemp(prefix="mazn-bot-tests-")
os.environ["DATA_DIR"] = _TEST_DATA
# Force credentials empty so tests can never send Telegram messages or place live orders.
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_ADMIN_ID"] = "123"
os.environ["MEXC_API_KEY"] = ""
os.environ["MEXC_SECRET_KEY"] = ""
os.environ["DATABASE_URL"] = ""

import main  # noqa: E402


class FakeExchange:
    def __init__(self, price=106.0):
        self.price = price
        self.markets = {"BTC/USDT": {"spot": True}}

    def fetch_ticker(self, symbol):
        return {"last": self.price, "close": self.price, "bid": self.price}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.8f}"

    def load_markets(self):
        return self.markets


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mazn-case-")
        main.DB_PATH = Path(self.tmp.name) / "test.db"
        main.exchange = FakeExchange()
        main.init_db()
        conn = main.get_db()
        try:
            conn.execute("DELETE FROM positions")
            conn.execute("DELETE FROM trades_history")
            conn.execute("DELETE FROM processed")
            conn.execute("UPDATE channels SET last_msg_id=0")
            conn.commit()
        finally:
            conn.close()
        main.db_set("paper_mode", "1")
        main.db_set("trading_enabled", "0")

    def tearDown(self):
        self.tmp.cleanup()

    def test_spot_does_not_override_explicit_short(self):
        sig = main.parse_signal(
            "SPOT SHORT BTC/USDT\nEntry: 100\nTarget: 90\nStop: 105"
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig["direction"], "SHORT")
        self.assertEqual(sig["symbol"], "BTC/USDT")

    def test_signal_words_are_not_parsed_as_tickers(self):
        self.assertIsNone(
            main.parse_signal("BUY\nENTRY: 100\nTARGET: 110\nSTOP: 95")
        )

    def test_tp1_tp2_tp3_labels_do_not_become_prices_1_2_3(self):
        sig = main.parse_signal(
            "Signal: BUY ETHUSDT\nEntry: 10\nTP1: 11\nTP2: 12\nTP3: 13\nSL: 9"
        )
        self.assertEqual(sig["symbol"], "ETH/USDT")
        self.assertEqual(sig["targets"], [11.0, 12.0, 13.0])

    def test_sqlite_row_value_works(self):
        conn = main.get_db()
        try:
            row = conn.execute("SELECT template FROM channels LIMIT 1").fetchone()
            self.assertEqual(main.row_value(row, "template", "auto"), "auto")
        finally:
            conn.close()
        self.assertIsNone(main.row_value(None, "template"))

    def test_paper_position_manages_tp_even_after_global_mode_toggle(self):
        settings = main.get_settings()
        settings["paper_mode"] = True
        sig = {
            "symbol": "BTC/USDT", "entry": 100.0, "target": None,
            "targets": [], "stop": 95.0, "raw": "BTC LONG",
        }
        self.assertTrue(main.open_position(
            sig, {"id": "PAPER-TEST", "amount": 1.0, "price": 100.0, "average": 100.0},
            settings,
        ))
        main.db_set("paper_mode", "0")
        main.check_and_manage_positions()
        conn = main.get_db()
        try:
            pos = conn.execute("SELECT * FROM positions WHERE status='open'").fetchone()
            history = conn.execute("SELECT * FROM trades_history WHERE reason='TP1'").fetchall()
        finally:
            conn.close()
        self.assertEqual(pos["tp1_hit"], 1)
        self.assertAlmostEqual(pos["remaining_qty"], 0.66)
        self.assertEqual(len(history), 1)

    def test_channel_cursor_advances_after_non_signal_posts(self):
        conn = main.get_db()
        try:
            conn.execute("UPDATE channels SET last_msg_id=100 WHERE username='abojasimp'")
            conn.commit()
        finally:
            conn.close()
        old_scrape, old_process = main.scrape_channel, main.process_signal
        try:
            main.scrape_channel = lambda username: [
                {"id": 101, "channel": username, "text": "ordinary market update"}
            ]
            main.process_signal = lambda msg: None
            main.monitor_channels()
        finally:
            main.scrape_channel, main.process_signal = old_scrape, old_process
        conn = main.get_db()
        try:
            row = conn.execute(
                "SELECT last_msg_id FROM channels WHERE username='abojasimp'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["last_msg_id"], 101)

    def test_public_only_init_does_not_request_private_balance(self):
        original_mexc = main.ccxt.mexc
        original_key, original_secret = main.MEXC_API_KEY, main.MEXC_SECRET_KEY
        try:
            main.MEXC_API_KEY = ""
            main.MEXC_SECRET_KEY = ""
            main.ccxt.mexc = lambda options: FakeExchange()
            self.assertTrue(main.init_exchange())
        finally:
            main.ccxt.mexc = original_mexc
            main.MEXC_API_KEY, main.MEXC_SECRET_KEY = original_key, original_secret


if __name__ == "__main__":
    unittest.main()
