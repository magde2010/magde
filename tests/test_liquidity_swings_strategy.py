from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import liquidity_swings as liq


def candle(ts, open_, high, low, close, volume=1000):
    return [ts, open_, high, low, close, volume]


def test_confirmed_pivots_need_right_side_confirmation():
    candles = [candle(i, 10, 11, 9, 10) for i in range(10)]
    candles[4] = candle(4, 10, 15, 8, 10)
    highs, lows = liq.confirmed_pivots(candles, 2)
    assert (4, 15.0) in highs
    assert (4, 8.0) in lows
    # A pivot candidate too close to the end is not confirmed.
    candles.append(candle(10, 10, 20, 7, 10))
    highs, lows = liq.confirmed_pivots(candles, 2)
    assert all(index <= len(candles) - 3 for index, _ in highs)
    assert all(index <= len(candles) - 3 for index, _ in lows)


def test_mtf_sweep_reclaim_and_5m_confirmation_create_long_setup():
    four_h = []
    for i in range(65):
        close = 50 + i * 1.0
        four_h.append(candle(i, close - 0.3, close + 1, close - 1, close))

    one_h = []
    for i in range(120):
        base = 101 + i * 0.08
        low = base - 1
        high = base + 2
        close = base + 0.5
        if i == 80:
            low, high, close = 90.0, 110.0, 105.0
        if i in (79, 81):
            low = 100.0
        if i == 90:
            high, close = 130.0, 115.0
        if i > 90:
            high = 129.0 - (i - 90) * 0.25
        one_h.append(candle(i, close - 0.5, high, low, close))

    fifteen_m = [candle(i, 100, 103, 98, 101) for i in range(40)]
    fifteen_m[-1] = candle(39, 92, 94, 89, 91.5)

    five_m = [candle(i, 98, 99, 97, 98.5) for i in range(40)]
    five_m[-2] = candle(38, 98.8, 99.5, 98.0, 99.0)
    five_m[-1] = candle(39, 99.2, 101.5, 99.0, 100.5)

    result = liq.detect_long_setup(four_h, one_h, fifteen_m, five_m, pivot_length=2, min_rr=2.0)
    assert result is not None
    assert result["stop"] < result["entry"] < result["target"]
    assert result["rr"] >= 2.0
    assert result["signal_ts"] == 39


def test_no_long_entry_when_4h_trend_is_not_bullish():
    four_h = [candle(i, 100 - i, 101 - i, 98 - i, 99 - i) for i in range(65)]
    common = [candle(i, 100, 102, 98, 101) for i in range(120)]
    result = liq.detect_long_setup(four_h, common, common[:40], common[:40], pivot_length=2)
    assert result is None


def test_liquidity_strategy_tables_are_separate_from_portfolio_tables(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(liq, "_ENGINE", engine)
    monkeypatch.setattr(liq, "_SESSION_FACTORY", factory)
    liq.init_liquidity_db()
    assert set(liq.LiquidityBase.metadata.tables) == {
        "liquidity_strategy_settings", "liquidity_strategy_positions",
        "liquidity_scan_health", "liquidity_symbol_scan",
    }
    s = liq.update_settings(123, mode="paper", symbols_json='["BTC", "ETH"]')
    assert s.mode == "paper"
    assert s.symbols == ["BTC", "ETH"]
    sm = liq.summary(123)
    assert sm["open_positions"] == 0
    assert sm["mode"] == "paper"


class FakeExchange:
    def __init__(self, candles_by_tf):
        self.candles_by_tf = candles_by_tf
        self.markets = {"BTC/USDT": {"base": "BTC", "quote": "USDT", "active": True, "spot": True}}

    def load_markets(self):
        return self.markets

    def fetch_ohlcv(self, pair, timeframe, limit=120):
        return self.candles_by_tf[timeframe]


class FakeClient:
    quote = "USDT"

    def __init__(self, candles_by_tf):
        self.exchange = FakeExchange(candles_by_tf)

    def get_ticker_price(self, pair):
        return 100.5


def _candidate_candles():
    four_h = []
    for i in range(65):
        close = 50 + i * 1.0
        four_h.append(candle(i, close - 0.3, close + 1, close - 1, close))
    one_h = []
    for i in range(120):
        base = 101 + i * 0.08
        low, high, close = base - 1, base + 2, base + 0.5
        if i == 80:
            low, high, close = 90.0, 110.0, 105.0
        if i in (79, 81):
            low = 100.0
        if i == 90:
            high, close = 130.0, 115.0
        if i > 90:
            high = 129.0 - (i - 90) * 0.25
        one_h.append(candle(i, close - 0.5, high, low, close))
    fifteen_m = [candle(i, 100, 103, 98, 101) for i in range(40)]
    fifteen_m[-1] = candle(39, 92, 94, 89, 91.5)
    five_m = [candle(i, 98, 99, 97, 98.5) for i in range(40)]
    five_m[-2] = candle(38, 98.8, 99.5, 98.0, 99.0)
    five_m[-1] = candle(39, 99.2, 101.5, 99.0, 100.5)
    # Add the unfinished candle that _closed_ohlcv deliberately discards.
    return {
        "4h": four_h + [candle(65, 114, 116, 113, 115)],
        "1h": one_h + [candle(120, 109, 111, 108, 110)],
        "15m": fifteen_m + [candle(40, 92, 95, 91, 94)],
        "5m": five_m + [candle(40, 100, 101, 99, 100.5)],
    }


def test_paper_scan_persists_one_position_and_deduplicates_signal(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(liq, "_ENGINE", engine)
    monkeypatch.setattr(liq, "_SESSION_FACTORY", factory)
    liq.init_liquidity_db()
    liq.update_settings(
        999, mode="paper", enabled=True, symbols_json='["BTC"]', auto_discover=False,
        pivot_length=2, min_rr=2.0, allocated_capital=100.0, position_size_usdt=5.0,
        max_open_positions=3,
    )
    client = FakeClient(_candidate_candles())
    events_first = liq.scan_once(client, 999)
    open_positions = [p for p in liq.list_positions(999, "paper") if p.status == "open"]
    assert len(open_positions) == 1
    assert open_positions[0].symbol == "BTC"
    assert open_positions[0].amount > 0
    assert any("دخول PAPER" in item["text"] for item in events_first)

    health = liq.monitoring_status(999)
    assert health["cycle"]["status"] == "ok"
    assert health["cycle"]["symbols_total"] == 1
    assert health["cycle"]["symbols_checked"] == 1
    assert health["cycle"]["signals_found"] == 1
    btc_status = next(row for row in health["symbols"] if row["symbol"] == "BTC")
    assert btc_status["state"] == "paper_open"
    assert btc_status["candle_4h_ts"] is not None
    assert btc_status["candle_5m_ts"] is not None
    assert btc_status["last_price"] == 100.5

    events_second = liq.scan_once(client, 999)
    open_positions_after = [p for p in liq.list_positions(999, "paper") if p.status == "open"]
    assert len(open_positions_after) == 1
    assert not any("دخول PAPER" in item["text"] for item in events_second)


def test_paused_strategy_does_not_claim_that_symbols_were_scanned(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(liq, "_ENGINE", engine)
    monkeypatch.setattr(liq, "_SESSION_FACTORY", factory)
    liq.init_liquidity_db()
    liq.update_settings(321, enabled=False, mode="paper", symbols_json='["BTC", "ETH"]')

    client = FakeClient(_candidate_candles())
    liq.scan_once(client, 321)
    monitor = liq.monitoring_status(321)

    assert monitor["cycle"]["status"] == "paused"
    assert monitor["cycle"]["symbols_total"] == 2
    assert monitor["cycle"]["symbols_checked"] == 0
    assert all(row["last_checked_at"] is None for row in monitor["symbols"])
    assert "متوقف" in monitor["cycle"]["reason"]


def test_diagnostic_reports_the_failed_mtf_stage():
    four_h = [candle(i, 100 - i, 101 - i, 98 - i, 99 - i) for i in range(65)]
    common = [candle(i, 100, 102, 98, 101) for i in range(120)]
    signal, state, detail = liq.diagnose_long_setup(four_h, common, common[:40], common[:40], pivot_length=2)
    assert signal is None
    assert state == "trend_not_bullish"
    assert "4H" in detail


def test_live_partial_exit_reconciliation_keeps_remaining_amount(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(liq, "_ENGINE", engine)
    monkeypatch.setattr(liq, "_SESSION_FACTORY", factory)
    liq.init_liquidity_db()

    class OrderClient:
        quote = "USDT"
        class Exchange:
            def fetch_order(self, order_id, pair):
                return {"id": order_id, "status": "canceled", "filled": 0.4, "average": 110.0}
        exchange = Exchange()

    with liq.db_session() as db:
        pos = liq.LiquidityPosition(
            telegram_id=777, symbol="BTC", mode="live", signal_key="BTC:one",
            status="pending_exit", entry_price=100.0, stop_price=95.0, target_price=110.0,
            amount=1.0, initial_amount=1.0, intended_usdt=100.0,
            entry_fee_estimate=0.1, pending_order_id="sell-1", pending_side="sell",
            pending_exit_reason="target",
        )
        db.add(pos)
        db.flush()
        events = liq._reconcile_pending(OrderClient(), db, pos, 110.0)
        assert pos.status == "open"
        assert abs(pos.amount - 0.6) < 1e-9
        assert pos.pending_order_id is None
        assert pos.realized_pnl > 0
        assert any("بيع جزئي" in message for message in events)


class MarketUniverseExchange:
    def __init__(self, count=8):
        self.markets = {}
        self.tickers = {}
        for i in range(count):
            base = f"COIN{i:02d}"
            pair = f"{base}/USDT"
            self.markets[pair] = {
                "base": base, "quote": "USDT", "active": True,
                "spot": True, "type": "spot",
            }
            self.tickers[pair] = {
                "quoteVolume": 2_000_000 + i * 10_000,
                "baseVolume": 2_000_000,
                "last": 1.0,
                "bid": 1.0,
                "ask": 1.001,
            }
        self.markets["DELISTED/USDT"] = {
            "base": "DELISTED", "quote": "USDT", "active": False, "spot": True,
        }
        self.markets["PERP/USDT"] = {
            "base": "PERP", "quote": "USDT", "active": True, "spot": False, "swap": True,
        }
        self.ticker_calls = 0

    def load_markets(self):
        return self.markets

    def fetch_tickers(self, symbols=None):
        self.ticker_calls += 1
        if symbols is None:
            return self.tickers
        return {symbol: self.tickers[symbol] for symbol in symbols if symbol in self.tickers}


class MarketUniverseClient:
    quote = "USDT"

    def __init__(self, count=8):
        self.exchange = MarketUniverseExchange(count)


def test_auto_discovery_rotates_across_full_eligible_market(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(liq, "_ENGINE", engine)
    monkeypatch.setattr(liq, "_SESSION_FACTORY", factory)
    liq.init_liquidity_db()
    liq.update_settings(
        552, auto_discover=True, symbols_json="[]", max_symbols=3,
        min_daily_quote_volume=1_000_000, max_spread_pct=0.5,
    )
    client = MarketUniverseClient(8)
    now = liq._now()
    with liq.db_session() as db:
        settings = liq.get_settings(552, db)
        first = liq._discover_symbols(client, settings, now)
        db.flush()
        second = liq._discover_symbols(client, settings, now)
        db.flush()
        third = liq._discover_symbols(client, settings, now)
        db.flush()
        assert len(first) == len(second) == len(third) == 3
        assert len(set(first + second + third)) == 8
        assert len(settings.discovered_symbols) == 8
        assert settings.market_symbols_count == 8
        assert settings.discovery_cursor == 1
        # One discovery refresh should use a single bulk ticker request, then reuse it.
        assert client.exchange.ticker_calls == 1


def test_discovery_filters_low_volume_wide_spread_and_non_spot_markets(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(liq, "_ENGINE", engine)
    monkeypatch.setattr(liq, "_SESSION_FACTORY", factory)
    liq.init_liquidity_db()
    client = MarketUniverseClient(3)
    client.exchange.markets["LOWVOL/USDT"] = {
        "base": "LOWVOL", "quote": "USDT", "active": True, "spot": True,
    }
    client.exchange.tickers["LOWVOL/USDT"] = {
        "quoteVolume": 10, "last": 1, "bid": 1, "ask": 1.001,
    }
    client.exchange.markets["WIDESPREAD/USDT"] = {
        "base": "WIDESPREAD", "quote": "USDT", "active": True, "spot": True,
    }
    client.exchange.tickers["WIDESPREAD/USDT"] = {
        "quoteVolume": 2_000_000, "last": 1, "bid": 1, "ask": 1.02,
    }
    liq.update_settings(553, auto_discover=True, symbols_json="[]", max_symbols=10)
    with liq.db_session() as db:
        settings = liq.get_settings(553, db)
        chosen = liq._discover_symbols(client, settings, liq._now())
        assert len(chosen) == 3
        assert "DELISTED" not in settings.discovered_symbols
        assert "PERP" not in settings.discovered_symbols
        assert "LOWVOL" not in settings.discovered_symbols
        assert "WIDESPREAD" not in settings.discovered_symbols


def test_closed_ohlcv_cache_avoids_repeating_same_history(monkeypatch):
    class CountingExchange:
        def __init__(self):
            self.calls = 0

        def fetch_ohlcv(self, pair, timeframe, limit=120):
            self.calls += 1
            return [candle(i, 10, 11, 9, 10) for i in range(25)]

    exchange = CountingExchange()
    first = liq._closed_ohlcv(exchange, "CACHE/USDT", "5m", 120)
    second = liq._closed_ohlcv(exchange, "CACHE/USDT", "5m", 120)
    assert len(first) == 24
    assert len(second) == 24
    assert first[-1][0] == 23  # unfinished final candle is excluded
    assert exchange.calls == 1
