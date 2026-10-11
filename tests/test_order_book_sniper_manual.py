from types import SimpleNamespace
import order_book_sniper as sniper_module


class FakeExchange:
    def __init__(self):
        self.markets = {"ENA/USDT": {"spot": True, "active": True, "quote": "USDT", "precision": {"price": 2}}}
        self.book_calls = []
        self.orders = []
        self.cancelled = []

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_order_book(self, symbol, limit=30):
        self.book_calls.append(symbol)
        return {
            "bids": [[100, 1], [99.99, 1], [99.98, 100], [99.97, 1], [99.96, 1]],
            "asks": [[100.1, 1], [100.11, 1], [100.12, 1], [100.13, 1], [100.14, 1]],
        }

    def price_to_precision(self, symbol, price):
        return f"{price:.2f}"

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.4f}"

    def fetch_ticker(self, symbol):
        return {"bid": 100.0, "last": 100.0}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        order = {"id": f"order-{len(self.orders)+1}", "status": "open", "filled": 0}
        self.orders.append((symbol, kind, side, amount, price, params, order))
        return order

    def fetch_order(self, order_id, symbol):
        return {"id": order_id, "status": "open", "filled": 0, "average": None}

    def cancel_order(self, order_id, symbol):
        self.cancelled.append((order_id, symbol))
        return {"id": order_id, "status": "canceled"}


def configure(monkeypatch, mode="PAPER"):
    settings = SimpleNamespace(
        mode=mode, live_confirmed=True, watch_symbols="ENA/USDT", running_symbols="ENA/USDT",
        enabled=True, max_positions=20, max_spread_pct=0.2, wall_multiple=3,
        max_wall_distance_pct=0.45, min_order_usdt=2, tp_pct=0.35, sl_pct=0.25,
        symbol_budgets='{"ENA/USDT":25}', runtime_state="{}",
    )
    budgets = {"ENA/USDT": 25.0}
    def update_settings(_tid, **kwargs):
        for key, value in kwargs.items():
            setattr(settings, key, value)
        return settings
    monkeypatch.setattr(sniper_module, "get_settings", lambda _tid: settings)
    monkeypatch.setattr(sniper_module, "update_settings", update_settings)
    monkeypatch.setattr(sniper_module, "get_symbol_budgets", lambda _tid: budgets.copy())
    return settings


def test_only_user_selected_symbol_is_scanned_and_budget_is_used(monkeypatch):
    configure(monkeypatch)
    exchange = FakeExchange()
    bot = sniper_module.OrderBookSniper(123)
    bot.exchange = exchange
    bot.scan_symbol_now("ENA/USDT")
    assert not bot.positions
    bot.scan_symbol_now("ENA/USDT")
    assert "ENA/USDT" in bot.entry_orders
    order=bot.entry_orders["ENA/USDT"]
    assert order["paper"] is True
    assert order["budget"] == 25.0 / 3
    assert order["dca_index"] == 1
    assert order["dca_total"] == 3
    assert order["price"] > 0
    assert order["wall_usdt"] > 0
    assert bot.status()["wall_status"]["ENA/USDT"]["state"] == "entry_pending"
    assert not bot.positions  # PAPER order is pending, not instantly filled
    assert set(exchange.book_calls) == {"ENA/USDT"}


def test_hard_cap_is_20_active_symbols(monkeypatch):
    settings = configure(monkeypatch)
    exchange = FakeExchange()
    bot = sniper_module.OrderBookSniper(123)
    bot.exchange = exchange
    bot.positions = {f"COIN{i}/USDT": {"entry": 1, "amount": 1, "cost": 1, "paper": True} for i in range(20)}
    bot._scan_one("ENA/USDT", settings)
    assert len(bot.positions) == 20
    assert not exchange.orders


def test_live_entry_order_is_tracked_and_manage_can_cancel_it(monkeypatch):
    configure(monkeypatch, mode="LIVE")
    exchange = FakeExchange()
    bot = sniper_module.OrderBookSniper(123)
    bot.exchange = exchange
    bot.scan_symbol_now("ENA/USDT")
    bot.scan_symbol_now("ENA/USDT")
    assert "ENA/USDT" in bot.entry_orders
    ok, message = bot.close_symbol("ENA/USDT")
    assert ok, message
    assert exchange.cancelled == [("order-1", "ENA/USDT")]
    assert "ENA/USDT" not in bot.entry_orders


def test_paper_entry_is_not_filled_until_market_reaches_limit(monkeypatch):
    configure(monkeypatch, mode="PAPER")
    exchange = FakeExchange()
    bot = sniper_module.OrderBookSniper(123)
    bot.exchange = exchange
    bot.scan_symbol_now("ENA/USDT")
    bot.scan_symbol_now("ENA/USDT")
    order = bot.entry_orders["ENA/USDT"]
    assert not bot.positions
    # Price remains above limit, so simulated order remains pending.
    bot._manage_entry_orders()
    assert "ENA/USDT" in bot.entry_orders
    # Once best ask reaches the limit, PAPER simulates a fill.
    exchange.fetch_order_book = lambda symbol, limit=30: {
        "bids": [[order["price"], 5]], "asks": [[order["price"], 5]]
    }
    bot._manage_entry_orders()
    assert "ENA/USDT" not in bot.entry_orders
    assert bot.positions["ENA/USDT"]["paper"] is True
    assert bot.positions["ENA/USDT"]["entry"] == order["price"]


def test_closing_paper_pending_order_does_not_call_exchange_cancel(monkeypatch):
    configure(monkeypatch, mode="PAPER")
    exchange = FakeExchange()
    bot = sniper_module.OrderBookSniper(123)
    bot.exchange = exchange
    bot.scan_symbol_now("ENA/USDT")
    bot.scan_symbol_now("ENA/USDT")
    assert "ENA/USDT" in bot.entry_orders
    ok, message = bot.close_symbol("ENA/USDT")
    assert ok, message
    assert "ENA/USDT" not in bot.entry_orders
    assert exchange.cancelled == []


def test_best_sell_wall_is_tracked_after_entry(monkeypatch):
    configure(monkeypatch, mode="PAPER")
    exchange = FakeExchange()
    exchange.fetch_order_book = lambda symbol, limit=30: {
        "bids": [[100, 1], [99.99, 1], [99.98, 1], [99.97, 1], [99.96, 1]],
        "asks": [[100.1, 1], [100.11, 1], [100.12, 100], [100.13, 1], [100.14, 1]],
    }
    bot = sniper_module.OrderBookSniper(123)
    bot.exchange = exchange
    bot.positions = {"ENA/USDT": {"entry": 100.0, "amount": 0.25, "cost": 25.0, "paper": True}}
    bot._scan_one("ENA/USDT", sniper_module.get_settings(123))
    status = bot.status()["wall_status"]["ENA/USDT"]
    assert status["state"] == "position_open"
    assert status["sell_wall_price"] == 100.12
    assert status["sell_wall_usdt"] > 0
    assert bot.positions["ENA/USDT"]["wall_target"] == 100.12


def test_dca_second_entry_uses_one_third_budget_and_lower_wall(monkeypatch):
    configure(monkeypatch, mode="PAPER")
    exchange = FakeExchange()
    bot = sniper_module.OrderBookSniper(123)
    bot.exchange = exchange
    bot.positions = {"ENA/USDT": {"entry": 100.0, "amount": 0.1, "cost": 10.0, "paper": True, "dca_entries": 1}}
    settings = sniper_module.get_settings(123)
    bot._scan_one("ENA/USDT", settings)
    bot._scan_one("ENA/USDT", settings)
    order = bot.entry_orders["ENA/USDT"]
    assert order["dca_index"] == 2
    assert order["budget"] == 25.0 / 3
