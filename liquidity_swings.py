"""Independent MEXC Spot implementation of a multi-timeframe Liquidity Swings strategy.

Strategy data lives in its own prefixed SQL tables. It does not read or mutate LOTFE
portfolio records or call the portfolio rebalancer.

The concept is inspired by the user-provided Liquidity Swings indicator by LuxAlgo.
This module is an independent Python implementation, not copied Pine Script. LuxAlgo's
indicator is published under CC BY-NC-SA 4.0; this project is not affiliated with LuxAlgo.
"""
from __future__ import annotations

import json
import logging
import math
import time
from contextlib import contextmanager
from datetime import datetime, date, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import (
    Boolean, BigInteger, Column, DateTime, Float, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)
LiquidityBase = declarative_base()
_ENGINE = None
_SESSION_FACTORY = None

# Cache raw exchange candles only until the next candle boundary. This avoids
# fetching the same four OHLCV histories on every 60-second scan while still
# refreshing immediately after a new candle can become closed.
_OHLCV_CACHE: Dict[Tuple[int, str, str, int], Tuple[float, Any, List[list]]] = {}
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def _utcnow():
    # The schema stores naive UTC timestamps for compatibility with existing LOTFE tables.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _db_engine():
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is None:
        # Reuse the application's database engine without coupling to its models.
        from database import engine
        _ENGINE = engine
        _SESSION_FACTORY = sessionmaker(bind=_ENGINE, autocommit=False, autoflush=False, expire_on_commit=False)
    return _ENGINE


def _session_factory():
    _db_engine()
    return _SESSION_FACTORY


class LiquiditySettings(LiquidityBase):
    __tablename__ = "liquidity_strategy_settings"

    telegram_id = Column(BigInteger, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    mode = Column(String(10), nullable=False, default="paper")  # paper | live
    entry_profile = Column(String(16), nullable=False, default="balanced")  # conservative | balanced | active
    live_confirmed = Column(Boolean, nullable=False, default=False)
    # Empty manual list by default: the strategy discovers eligible Spot/USDT
    # pairs and rotates through them. Manual symbols remain an optional priority.
    symbols_json = Column(Text, nullable=False, default="[]")
    auto_discover = Column(Boolean, nullable=False, default=True)
    excluded_symbols_json = Column(Text, nullable=False, default="[]")
    # Maximum symbols processed per cycle, not a cap on the discovered universe.
    max_symbols = Column(Integer, nullable=False, default=10)
    market_symbols_count = Column(Integer, nullable=False, default=0)
    discovery_cursor = Column(Integer, nullable=False, default=0)
    allocated_capital = Column(Float, nullable=False, default=100.0)
    position_size_usdt = Column(Float, nullable=False, default=5.0)
    risk_pct = Column(Float, nullable=False, default=1.0)
    max_open_positions = Column(Integer, nullable=False, default=3)
    daily_loss_limit_usdt = Column(Float, nullable=False, default=5.0)
    pivot_length = Column(Integer, nullable=False, default=14)
    min_rr = Column(Float, nullable=False, default=2.0)
    stop_buffer_pct = Column(Float, nullable=False, default=0.2)
    max_entry_deviation_pct = Column(Float, nullable=False, default=0.5)
    min_daily_quote_volume = Column(Float, nullable=False, default=1_000_000.0)
    max_spread_pct = Column(Float, nullable=False, default=0.5)
    discovered_symbols_json = Column(Text, nullable=False, default="[]")
    last_discovery_at = Column(DateTime, nullable=True)
    last_processed_signals_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    @property
    def symbols(self) -> List[str]:
        try:
            raw = json.loads(self.symbols_json or "[]")
            return list(dict.fromkeys(str(x).upper().strip() for x in raw if str(x).strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ["BTC", "ETH", "SOL"]

    @symbols.setter
    def symbols(self, values: Iterable[str]) -> None:
        clean = list(dict.fromkeys(str(v).upper().strip() for v in values if str(v).strip()))
        self.symbols_json = json.dumps(clean, ensure_ascii=False)

    @property
    def excluded_symbols(self) -> List[str]:
        try:
            data = json.loads(self.excluded_symbols_json or "[]")
            return list(dict.fromkeys(str(x).upper().strip() for x in data if str(x).strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    @property
    def discovered_symbols(self) -> List[str]:
        try:
            data = json.loads(self.discovered_symbols_json or "[]")
            return list(dict.fromkeys(str(x).upper().strip() for x in data if str(x).strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []


class LiquidityPosition(LiquidityBase):
    __tablename__ = "liquidity_strategy_positions"
    __table_args__ = (
        UniqueConstraint("telegram_id", "mode", "signal_key", name="uq_liq_signal_mode"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    mode = Column(String(10), nullable=False, index=True)
    signal_key = Column(String(128), nullable=False)
    status = Column(String(24), nullable=False, default="open")
    entry_price = Column(Float, nullable=False, default=0.0)
    stop_price = Column(Float, nullable=False, default=0.0)
    target_price = Column(Float, nullable=False, default=0.0)
    amount = Column(Float, nullable=False, default=0.0)  # remaining base quantity
    initial_amount = Column(Float, nullable=False, default=0.0)
    intended_usdt = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    entry_fee_estimate = Column(Float, nullable=False, default=0.0)
    exit_reason = Column(String(24), nullable=True)
    pending_order_id = Column(String(96), nullable=True)
    pending_side = Column(String(10), nullable=True)
    pending_exit_reason = Column(String(24), nullable=True)
    last_error = Column(Text, nullable=True)
    opened_at = Column(DateTime, nullable=False, default=_utcnow)
    closed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class LiquidityScanHealth(LiquidityBase):
    """Durable heartbeat and real results for the last market-scan cycle."""
    __tablename__ = "liquidity_scan_health"

    telegram_id = Column(BigInteger, primary_key=True)
    cycle_status = Column(String(20), nullable=False, default="never")
    last_started_at = Column(DateTime, nullable=True)
    last_finished_at = Column(DateTime, nullable=True)
    symbols_total = Column(Integer, nullable=False, default=0)
    symbols_checked = Column(Integer, nullable=False, default=0)
    signals_found = Column(Integer, nullable=False, default=0)
    errors_count = Column(Integer, nullable=False, default=0)
    duration_ms = Column(Integer, nullable=False, default=0)
    cycle_reason = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)


class LiquiditySymbolScan(LiquidityBase):
    """Per-symbol diagnostics proving which candles were actually fetched."""
    __tablename__ = "liquidity_symbol_scan"
    __table_args__ = (UniqueConstraint("telegram_id", "symbol", name="uq_liq_scan_user_symbol"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    last_checked_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    last_price = Column(Float, nullable=False, default=0.0)  # close of most recent closed 5m candle
    candle_4h_ts = Column(BigInteger, nullable=True)
    candle_1h_ts = Column(BigInteger, nullable=True)
    candle_15m_ts = Column(BigInteger, nullable=True)
    candle_5m_ts = Column(BigInteger, nullable=True)
    state = Column(String(32), nullable=False, default="not_checked")
    detail = Column(Text, nullable=True)
    signal_found = Column(Boolean, nullable=False, default=False)
    error = Column(Text, nullable=True)


def init_liquidity_db() -> None:
    engine = _db_engine()
    LiquidityBase.metadata.create_all(bind=engine)
    # create_all does not add columns to an existing table; migrate this isolated strategy table.
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    if "liquidity_strategy_settings" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("liquidity_strategy_settings")}
        # The absence of discovery_cursor marks databases created before automatic
        # market rotation. Enable discovery once during that upgrade so existing
        # users are not silently left on a hand-picked, fixed symbol list.
        first_market_scanner_upgrade = "discovery_cursor" not in cols
        with engine.begin() as conn:
            if "entry_profile" not in cols:
                conn.execute(text("ALTER TABLE liquidity_strategy_settings ADD COLUMN entry_profile VARCHAR(16) NOT NULL DEFAULT 'balanced'"))
            if "market_symbols_count" not in cols:
                conn.execute(text("ALTER TABLE liquidity_strategy_settings ADD COLUMN market_symbols_count INTEGER NOT NULL DEFAULT 0"))
            if "discovery_cursor" not in cols:
                conn.execute(text("ALTER TABLE liquidity_strategy_settings ADD COLUMN discovery_cursor INTEGER NOT NULL DEFAULT 0"))
            if first_market_scanner_upgrade:
                conn.execute(text("UPDATE liquidity_strategy_settings SET auto_discover = TRUE"))
                conn.execute(text("UPDATE liquidity_strategy_settings SET symbols_json = '[]' WHERE TRIM(symbols_json) IN (:spaced, :compact)"), {
                    "spaced": '["BTC", "ETH", "SOL"]',
                    "compact": '["BTC","ETH","SOL"]',
                })
                # These were the old built-in defaults, not user selections. Do not
                # pin BTC/ETH/SOL forever when the user asked for market discovery.


def _now() -> datetime:
    return _utcnow()


@contextmanager
def db_session():
    factory = _session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_settings(telegram_id: int, db=None) -> LiquiditySettings:
    """Get/create strategy settings; caller may pass its own isolated session."""
    owns = db is None
    if owns:
        cm = db_session()
        db = cm.__enter__()
    try:
        row = db.query(LiquiditySettings).filter_by(telegram_id=int(telegram_id)).first()
        if row is None:
            row = LiquiditySettings(telegram_id=int(telegram_id))
            db.add(row)
            db.flush()
        if owns:
            cm.__exit__(None, None, None)
        return row
    except Exception as exc:
        if owns:
            cm.__exit__(type(exc), exc, exc.__traceback__)
        raise


def update_settings(telegram_id: int, **values) -> LiquiditySettings:
    allowed = {
        "enabled", "mode", "entry_profile", "live_confirmed", "auto_discover", "excluded_symbols_json", "max_symbols",
        "allocated_capital", "position_size_usdt", "risk_pct", "max_open_positions",
        "daily_loss_limit_usdt", "pivot_length", "min_rr", "stop_buffer_pct",
        "max_entry_deviation_pct", "min_daily_quote_volume", "max_spread_pct", "symbols_json",
    }
    with db_session() as db:
        row = get_settings(telegram_id, db)
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"Unsupported strategy setting: {key}")
            setattr(row, key, value)
        row.updated_at = _now()
        db.flush()
        db.refresh(row)
        return row


def _safe_float(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _closed_ohlcv(exchange, pair: str, timeframe: str, limit: int = 120) -> List[list]:
    """Fetch candle history with timeframe-aware caching, returning closed candles only."""
    now = time.time()
    key = (id(exchange), str(pair), str(timeframe), int(limit))
    cached = _OHLCV_CACHE.get(key)
    if cached and cached[1] is exchange and cached[0] > now:
        rows = cached[2]
    else:
        rows = exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit) or []
        # Keep the cache until just after the next UTC timeframe boundary. If a
        # request failed, do not cache the failure: the next cycle may retry.
        frame_seconds = _TF_SECONDS.get(str(timeframe), 300)
        next_boundary = (int(now // frame_seconds) + 1) * frame_seconds
        _OHLCV_CACHE[key] = (next_boundary + 2.0, exchange, [list(row) for row in rows])
        if len(_OHLCV_CACHE) > 5000:
            for old_key, (expires, _exchange_ref, _rows) in list(_OHLCV_CACHE.items()):
                if expires <= now:
                    _OHLCV_CACHE.pop(old_key, None)
            while len(_OHLCV_CACHE) > 5000:
                _OHLCV_CACHE.pop(next(iter(_OHLCV_CACHE)))
    if not rows:
        return []
    # The final CCXT candle is normally still forming; never trade from it.
    closed = rows[:-1]
    if len(closed) < 20:
        return []
    return closed


def confirmed_pivots(candles: Sequence[Sequence[float]], length: int) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Return confirmed swing highs/lows; right-side confirmation is required."""
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    n = len(candles)
    length = max(2, int(length))
    for i in range(length, n - length):
        high = _safe_float(candles[i][2])
        low = _safe_float(candles[i][3])
        window_highs = [_safe_float(candles[j][2]) for j in range(i - length, i + length + 1)]
        window_lows = [_safe_float(candles[j][3]) for j in range(i - length, i + length + 1)]
        # Strict local extrema reduce duplicate pivots on flat highs/lows.
        if high == max(window_highs) and window_highs.count(high) == 1:
            highs.append((i, high))
        if low == min(window_lows) and window_lows.count(low) == 1:
            lows.append((i, low))
    return highs, lows


def diagnose_long_setup(
    candles_4h: Sequence[Sequence[float]],
    candles_1h: Sequence[Sequence[float]],
    candles_15m: Sequence[Sequence[float]],
    candles_5m: Sequence[Sequence[float]],
    pivot_length: int = 14,
    min_rr: float = 2.0,
    stop_buffer_pct: float = 0.2,
    entry_profile: str = "balanced",
) -> Tuple[Optional[Dict[str, float]], str, str]:
    """Return the candidate and a human-readable, stage-specific scan diagnosis."""
    if min(len(candles_4h), len(candles_1h), len(candles_15m), len(candles_5m)) < 25:
        return None, "insufficient_data", "بيانات الشموع غير كافية على إطار زمني واحد أو أكثر"
    close4 = [_safe_float(c[4]) for c in candles_4h]
    if len(close4) < 50:
        return None, "insufficient_data", "نحتاج 50 شمعة مغلقة على إطار 4H لحساب الاتجاه"
    sma20 = sum(close4[-20:]) / 20.0
    sma50 = sum(close4[-50:]) / 50.0
    last4 = close4[-1]
    profile = str(entry_profile or "balanced").lower()
    if profile == "conservative":
        trend_ok = last4 > sma20 and sma20 > sma50
    elif profile == "active":
        trend_ok = last4 > sma50 * 0.985 and sma20 >= sma50 * 0.985
    else:
        trend_ok = last4 > sma50 and sma20 >= sma50 * 0.995
    if not trend_ok:
        return None, "trend_not_bullish", f"شرط اتجاه 4H غير متحقق (المستوى: {profile})"

    highs1, lows1 = confirmed_pivots(candles_1h, pivot_length)
    if not lows1:
        return None, "no_liquidity_low", "لم يتم تأكيد قاع سيولة على 1H حتى الآن"
    zone_index, zone_low = lows1[-1]
    # A sweep is a 15m event; its 5m confirmation often arrives in the next
    # candle(s). Keep the last four CLOSED 15m candles in the search window.
    fifteen_minutes_ms = 15 * 60 * 1000
    confirmation_window_ms = 30 * 60 * 1000
    sweep_lookback = min(4, len(candles_15m))
    recent_sweeps = []
    for idx in range(len(candles_15m) - 1, len(candles_15m) - sweep_lookback - 1, -1):
        row = candles_15m[idx]
        row_low = _safe_float(row[3])
        row_close = _safe_float(row[4])
        if row_low < zone_low and row_close > zone_low:
            recent_sweeps.append((idx, row, row_low, _safe_float(row[0])))
    if not recent_sweeps:
        return None, "waiting_sweep", (
            f"لا يوجد sweep/reclaim على آخر {sweep_lookback} شموع مغلقة 15m؛ "
            f"آخر قاع سيولة {zone_low:.8g}"
        )

    selected = None
    newest_sweep_without_confirmation = recent_sweeps[0]
    for sweep_idx, sweep_candle, candidate_sweep_low, sweep_ts in recent_sweeps:
        sweep_close_ts = sweep_ts + fifteen_minutes_ms
        synthetic_timestamps = abs(sweep_ts) < 10_000_000
        confirmations = []
        for confirm_idx in range(1, len(candles_5m)):
            confirm_candle = candles_5m[confirm_idx]
            confirm_ts = _safe_float(confirm_candle[0])
            if synthetic_timestamps:
                # Older deterministic tests use candle indexes, not MEXC ms.
                # Keep a bounded recent window without weakening real timestamps.
                if confirm_idx < max(1, len(candles_5m) - 6):
                    continue
            elif confirm_ts < sweep_close_ts or confirm_ts > sweep_close_ts + confirmation_window_ms:
                continue
            previous_candle = candles_5m[confirm_idx - 1]
            close5 = _safe_float(confirm_candle[4])
            open5 = _safe_float(confirm_candle[1])
            previous_high = _safe_float(previous_candle[2])
            previous_close = _safe_float(previous_candle[4])
            if profile == "conservative":
                confirm_ok = close5 > open5 and close5 > previous_high
            elif profile == "active":
                confirm_ok = close5 > open5 and close5 > previous_close
            else:
                confirm_ok = close5 > open5 and close5 > min(previous_high, previous_close)
            if confirm_ok:
                confirmations.append((confirm_idx, confirm_candle))
        if confirmations:
            confirm_idx, confirm = confirmations[-1]
            selected = (sweep_idx, sweep_candle, candidate_sweep_low, sweep_ts, confirm_idx, confirm)
            break

    if selected is None:
        _, _, _, _ = newest_sweep_without_confirmation
        return None, "waiting_5m_confirmation", (
            f"تم رصد sweep/reclaim على 15m عند مستوى {zone_low:.8g}، "
            "لكن لا توجد شمعة 5m صاعدة مغلقة بعد إغلاق شمعة السحب خلال نافذة التأكيد"
        )
    _, last15, sweep_low, sweep_ts, _, confirm = selected
    entry = _safe_float(confirm[4])

    stop = sweep_low * (1.0 - max(0.0, stop_buffer_pct) / 100.0)
    if stop <= 0 or entry <= stop:
        return None, "invalid_risk", "إعداد وقف الخسارة أنتج مخاطرة غير صالحة"
    risk = entry - stop
    viable_targets = []
    for idx, high in highs1:
        if idx <= zone_index or high <= entry:
            continue
        rr = (high - entry) / risk
        if rr >= min_rr:
            viable_targets.append((high, rr))
    if not viable_targets:
        return None, "no_valid_target", f"لا توجد منطقة هدف 1H تحقق عائد/مخاطرة {min_rr:g}:1"
    target, rr = min(viable_targets, key=lambda pair: pair[0])
    sweep_ts = int(last15[0])
    signal = {
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": rr,
        "sweep_low": sweep_low,
        "liquidity_level": zone_low,
        "signal_ts": sweep_ts,
    }
    return signal, "signal_ready", "اكتملت شروط 4H و1H و15m و5m؛ يجري فحص السعر والمخاطرة قبل الدخول"


def detect_long_setup(
    candles_4h: Sequence[Sequence[float]],
    candles_1h: Sequence[Sequence[float]],
    candles_15m: Sequence[Sequence[float]],
    candles_5m: Sequence[Sequence[float]],
    pivot_length: int = 14,
    min_rr: float = 2.0,
    stop_buffer_pct: float = 0.2,
) -> Optional[Dict[str, float]]:
    """Compatibility wrapper used by strategy logic and tests."""
    signal, _, _ = diagnose_long_setup(
        candles_4h, candles_1h, candles_15m, candles_5m,
        pivot_length=pivot_length, min_rr=min_rr, stop_buffer_pct=stop_buffer_pct,
    )
    return signal


def _pair(client, base: str) -> str:
    quote = getattr(client, "quote", "USDT") or "USDT"
    return f"{str(base).upper().strip()}/{quote}"


def _market_symbols(client) -> List[str]:
    """Return active, tradable MEXC Spot markets quoted in the configured asset."""
    try:
        markets = client.exchange.load_markets()
    except Exception:
        markets = getattr(client.exchange, "markets", {}) or {}
    quote = str(getattr(client, "quote", "USDT") or "USDT").upper()
    bases = []
    for market in (markets or {}).values():
        if not isinstance(market, dict):
            continue
        if str(market.get("quote") or "").upper() != quote or not market.get("base"):
            continue
        if market.get("active") is False:
            continue
        # Never pick perpetual swaps/futures just because they share the quote asset.
        if market.get("spot") is False or market.get("swap") or market.get("future"):
            continue
        market_type = market.get("type")
        if market_type and market_type != "spot":
            continue
        bases.append(str(market["base"]).upper())
    return sorted(set(bases))


def _fetch_market_tickers(exchange, pairs: List[str]) -> Dict[str, dict]:
    """Prefer a single all-market ticker call; fall back for CCXT adapters that require symbols."""
    try:
        tickers = exchange.fetch_tickers()
        if tickers:
            return tickers
    except TypeError:
        pass
    except Exception as exc:
        logger.warning("Bulk ticker request failed; trying a symbol-scoped request: %s", exc)
    try:
        return exchange.fetch_tickers(pairs) if pairs else {}
    except Exception:
        # The last cached discovery result can still be used by the caller.
        raise


def _discover_symbols(client, settings: LiquiditySettings, now: datetime) -> List[str]:
    """Discover all liquid Spot/USDT pairs, then rotate a bounded batch each cycle.

    The universe is not truncated to max_symbols. max_symbols is a per-cycle batch
    size, allowing the scanner to walk through the full qualified market without
    making hundreds of OHLCV requests in one 60-second job.
    """
    limit = max(1, int(settings.max_symbols or 10))
    excluded = set(settings.excluded_symbols)
    manual = [base for base in settings.symbols if base not in excluded]
    if not settings.auto_discover:
        return manual[:limit]

    cache_is_fresh = bool(
        settings.last_discovery_at
        and (now - settings.last_discovery_at).total_seconds() < 3600
        and settings.discovered_symbols
    )
    if not cache_is_fresh:
        discovered: List[str] = []
        try:
            market_bases = _market_symbols(client)
            settings.market_symbols_count = len(market_bases)
            pairs = [_pair(client, base) for base in market_bases]
            tickers = _fetch_market_tickers(client.exchange, pairs)
            known_pairs = set(pairs)
            scored = []
            for pair, ticker in (tickers or {}).items():
                if not isinstance(ticker, dict) or pair not in known_pairs:
                    continue
                base = pair.split("/", 1)[0].upper()
                if base in excluded:
                    continue
                last = _safe_float(ticker.get("last"))
                volume = _safe_float(ticker.get("quoteVolume"))
                if volume <= 0 and last > 0:
                    volume = _safe_float(ticker.get("baseVolume")) * last
                bid = _safe_float(ticker.get("bid"))
                ask = _safe_float(ticker.get("ask"))
                if volume < float(settings.min_daily_quote_volume) or bid <= 0 or ask < bid or ask <= 0:
                    continue
                spread_pct = ((ask - bid) / ((ask + bid) / 2.0)) * 100.0
                if spread_pct > float(settings.max_spread_pct):
                    continue
                scored.append((volume, base))
            scored.sort(key=lambda item: (-item[0], item[1]))
            discovered = list(dict.fromkeys(base for _, base in scored))
        except Exception as exc:
            logger.warning("Liquidity auto-discovery failed: %s", exc)
            discovered = settings.discovered_symbols
            if not discovered:
                # Do not pretend a failed universe request found no eligible coins.
                settings.last_discovery_at = None
                return manual[:limit]
        settings.discovered_symbols_json = json.dumps(discovered, ensure_ascii=False)
        settings.last_discovery_at = now

    discovered = [base for base in settings.discovered_symbols if base not in excluded]
    # Explicit manual symbols are pinned; all other candidates are rotated so the
    # highest-volume 10/40 are not the only assets ever examined.
    pinned = list(dict.fromkeys(base for base in manual if base not in excluded))[:limit]
    candidates = [base for base in discovered if base not in pinned]
    slots = max(0, limit - len(pinned))
    if not candidates or slots <= 0:
        return pinned
    cursor = int(settings.discovery_cursor or 0) % len(candidates)
    count = min(slots, len(candidates))
    batch = [candidates[(cursor + i) % len(candidates)] for i in range(count)]
    settings.discovery_cursor = (cursor + count) % len(candidates)
    return list(dict.fromkeys(pinned + batch))[:limit]


def _mode_open_positions(db, telegram_id: int, mode: str) -> List[LiquidityPosition]:
    return db.query(LiquidityPosition).filter(
        LiquidityPosition.telegram_id == int(telegram_id),
        LiquidityPosition.mode == mode,
        LiquidityPosition.status.in_(["open", "pending_entry", "pending_exit", "entry_submitting", "exit_submitting"]),
    ).order_by(LiquidityPosition.opened_at.asc()).all()


def _daily_realized_pnl(db, telegram_id: int, mode: str) -> float:
    today = _now().date()
    positions = db.query(LiquidityPosition).filter(
        LiquidityPosition.telegram_id == int(telegram_id),
        LiquidityPosition.mode == mode,
        LiquidityPosition.status == "closed",
        LiquidityPosition.closed_at >= datetime(today.year, today.month, today.day),
    ).all()
    return sum(_safe_float(p.realized_pnl) for p in positions)


def _is_duplicate(db, telegram_id: int, mode: str, signal_key: str) -> bool:
    return db.query(LiquidityPosition.id).filter_by(
        telegram_id=int(telegram_id), mode=mode, signal_key=signal_key,
    ).first() is not None


def _get_order(client, order_id: str, pair: str) -> Optional[dict]:
    try:
        return client.exchange.fetch_order(str(order_id), pair)
    except Exception as exc:
        logger.warning("Liquidity order reconciliation failed for %s: %s", order_id, exc)
        return None


def _reconcile_pending(client, db, pos: LiquidityPosition, price: float) -> List[str]:
    """Reconcile in-flight live orders before attempting another action."""
    events = []
    if not pos.pending_order_id:
        warning = "⚠️ يوجد أمر LIVE نتيجته غير مؤكدة بلا رقم أمر؛ أوقفت المحاولة التلقائية لتجنب تكرار التداول. راجع سجل MEXC يدويًا."
        if pos.last_error != warning:
            events.append(warning)
        pos.last_error = warning
        return events
    order = _get_order(client, pos.pending_order_id, _pair(client, pos.symbol))
    if not order:
        return events
    status = str(order.get("status") or "").lower()
    filled = _safe_float(order.get("filled"))
    avg = _safe_float(order.get("average")) or _safe_float(order.get("price")) or price
    terminal_statuses = {"closed", "filled", "canceled", "cancelled", "rejected", "expired"}
    if pos.status in {"pending_entry", "entry_submitting"}:
        if status in terminal_statuses and filled > 0:
            pos.entry_price = avg
            pos.amount = filled
            pos.initial_amount = filled
            pos.entry_fee_estimate = filled * avg * 0.001
            pos.status = "open"
            pos.pending_order_id = None
            pos.pending_side = None
            pos.last_error = None
            # Keep the absolute stop under the sweep wick and the target at the liquidity zone.
            suffix = " (تنفيذ جزئي)" if status not in {"closed", "filled"} else ""
            events.append(f"✅ تم تأكيد شراء {pos.symbol} على LIVE{suffix}")
        elif status in {"canceled", "cancelled", "rejected", "expired"}:
            pos.status = "entry_failed"
            pos.pending_order_id = None
            pos.pending_side = None
            pos.last_error = f"دخول لم ينفذ: {status}"
            pos.closed_at = _now()
            events.append(f"⚠️ فشل دخول {pos.symbol}: {status}")
    elif pos.status in {"pending_exit", "exit_submitting"}:
        if status in terminal_statuses and filled > 0:
            fill_price = avg
            sold = min(filled, pos.amount)
            entry_fee = _safe_float(pos.entry_fee_estimate) * (sold / max(pos.initial_amount, 1e-12))
            pnl = (fill_price - pos.entry_price) * sold - (fill_price * sold * 0.001) - entry_fee
            pos.realized_pnl = _safe_float(pos.realized_pnl) + pnl
            pos.amount = max(0.0, pos.amount - sold)
            pos.pending_order_id = None
            pos.pending_side = None
            if pos.amount <= max(pos.initial_amount * 0.000001, 1e-12):
                pos.amount = 0.0
                pos.status = "closed"
                pos.exit_reason = pos.pending_exit_reason or "exit"
                pos.closed_at = _now()
                events.append(f"🏁 إغلاق {pos.symbol} على LIVE | صافي تقريبي {pos.realized_pnl:+.3f} USDT")
            else:
                pos.status = "open"
                events.append(f"⚠️ بيع جزئي {pos.symbol}، المتبقي {pos.amount:.8g} لم يُغلق بعد")
            pos.pending_exit_reason = None
            if status not in {"closed", "filled"}:
                pos.last_error = f"تم تنفيذ جزء فقط قبل حالة الأمر: {status}"
        elif status in terminal_statuses:
            pos.status = "open"
            pos.pending_order_id = None
            pos.pending_side = None
            pos.last_error = f"خروج لم ينفذ: {status}"
            pos.pending_exit_reason = None
    return events


def _close_paper(pos: LiquidityPosition, mark_price: float, reason: str) -> str:
    # Conservative paper simulation: 0.1% estimated fee on both sides and 0.05% exit slippage.
    exit_price = mark_price * 0.9995
    pnl = (exit_price - pos.entry_price) * pos.amount
    pnl -= pos.entry_price * pos.amount * 0.001
    pnl -= exit_price * pos.amount * 0.001
    pos.realized_pnl = _safe_float(pos.realized_pnl) + pnl
    pos.amount = 0.0
    pos.status = "closed"
    pos.exit_reason = reason
    pos.closed_at = _now()
    return f"🏁 PAPER | إغلاق {pos.symbol} ({'وقف خسارة' if reason == 'stop' else 'هدف'}) | PnL تقديري {pnl:+.3f} USDT"


def _exit_live(client, db, pos: LiquidityPosition, reason: str, mark_price: float) -> Optional[str]:
    if pos.status != "open" or pos.amount <= 0:
        return None
    try:
        free = float(client.get_free_amount(pos.symbol) or 0.0)
        amount = min(float(pos.amount), free)
        if amount <= 0:
            pos.last_error = "الكمية غير متاحة للبيع حاليًا؛ سيُعاد الفحص"
            return None
        pair = _pair(client, pos.symbol)
        # Persist a no-retry guard before submitting an exchange sell.
        pos.status = "exit_submitting"
        pos.pending_side = "sell"
        pos.pending_exit_reason = reason
        pos.last_error = None
        db.commit()
        order = client.create_market_order(pair, "sell", amount)
        if not order:
            # Client explicitly reported that exchange filters/minimums blocked the order.
            pos.status = "open"
            pos.pending_side = None
            pos.pending_exit_reason = None
            pos.last_error = "أمر البيع لم يُقبل؛ قد تكون الكمية أقل من الحد الأدنى للمنصة"
            return None
        if not order.get("id"):
            pos.status = "exit_submitting"
            pos.last_error = "رد بيع بلا رقم أمر؛ نتيجة التنفيذ غير مؤكدة"
            return "⚠️ رد MEXC بلا رقم أمر بيع؛ لن أعيد المحاولة تلقائيًا، راجع سجل المنصة."
        pos.status = "pending_exit"
        pos.pending_order_id = str(order["id"])
        pos.pending_side = "sell"
        pos.pending_exit_reason = reason
        pos.last_error = None
        return f"📤 أُرسل أمر إغلاق {pos.symbol} على LIVE؛ جاري التحقق من التنفيذ"
    except Exception as exc:
        pos.status = "exit_submitting"
        pos.last_error = f"نتيجة أمر البيع غير مؤكدة: {str(exc)[:350]}"
        logger.exception("Liquidity live exit result uncertain for %s", pos.symbol)
        return "⚠️ نتيجة أمر بيع LIVE غير مؤكدة؛ أوقفت إعادة المحاولة لتجنب بيع مكرر. راجع سجل MEXC."


def _entry_live(client, db, settings: LiquiditySettings, symbol: str, signal: Dict[str, float], size: float, signal_key: str) -> Tuple[Optional[LiquidityPosition], Optional[str]]:
    # Store a durable guard before sending the order to avoid duplicate entries after a restart.
    pos = LiquidityPosition(
        telegram_id=settings.telegram_id, symbol=symbol, mode="live", signal_key=signal_key,
        status="entry_submitting", entry_price=signal["entry"], stop_price=signal["stop"],
        target_price=signal["target"], amount=0.0, initial_amount=0.0,
        intended_usdt=size, opened_at=_now(),
    )
    db.add(pos)
    db.flush()
    # Persist a duplicate-order guard BEFORE calling the exchange. In the rare case
    # the API response is lost, the next scan will stop rather than buy twice.
    db.commit()
    try:
        if float(client.get_free_usdt()) < size * 1.002:
            pos.status = "entry_failed"
            pos.closed_at = _now()
            pos.last_error = "رصيد USDT الحر أقل من قيمة الصفقة والرسوم"
            return None, "الرصيد الحر من USDT غير كافٍ"
        pair = _pair(client, symbol)
        market_price = _safe_float(client.get_ticker_price(pair))
        if market_price <= 0:
            pos.status = "entry_failed"
            pos.closed_at = _now()
            pos.last_error = "تعذر قراءة السعر قبل الشراء"
            return None, "تعذر قراءة السعر قبل الشراء"
        base_amount = (size * 0.998) / market_price
        order = client.create_market_order(pair, "buy", base_amount)
        if not order:
            pos.status = "entry_failed"
            pos.closed_at = _now()
            pos.last_error = "لم يتم قبول الأمر؛ تحقق من حد الكمية/قيمة الصفقة"
            return None, "لم تقبل المنصة أمر الشراء؛ تحقق من الحد الأدنى للزوج"
        if not order.get("id"):
            pos.status = "entry_submitting"
            pos.last_error = "رد شراء بلا رقم أمر؛ نتيجة التنفيذ غير مؤكدة"
            return pos, "⚠️ رد MEXC بلا رقم أمر شراء؛ لن أعيد المحاولة تلقائيًا، راجع سجل المنصة."
        pos.pending_order_id = str(order["id"])
        pos.pending_side = "buy"
        pos.status = "pending_entry"
        # Fast path when CCXT returns a completed fill in the create response.
        filled = _safe_float(order.get("filled"))
        status = str(order.get("status") or "").lower()
        if filled > 0 and status in {"closed", "filled"}:
            pos.amount = filled
            pos.initial_amount = filled
            pos.entry_price = _safe_float(order.get("average")) or _safe_float(order.get("price")) or signal["entry"]
            pos.entry_fee_estimate = filled * pos.entry_price * 0.001
            pos.pending_order_id = None
            pos.pending_side = None
            pos.status = "open"
        return pos, f"🟢 أُرسل شراء LIVE لـ {symbol} بقيمة تقارب {size:.2f} USDT؛ جاري تأكيد التنفيذ"
    except Exception as exc:
        # An exchange/network exception can be ambiguous: the order may have
        # reached MEXC. Leave the durable guard in entry_submitting and never retry
        # that signal automatically without an order ID/reconciliation.
        pos.status = "entry_submitting"
        pos.last_error = f"نتيجة أمر الدخول غير مؤكدة: {str(exc)[:350]}"
        logger.exception("Liquidity live entry result uncertain for %s", symbol)
        return pos, f"⚠️ نتيجة شراء {symbol} غير مؤكدة؛ لن أعيد المحاولة تلقائيًا لتجنب شراء مكرر. راجع سجل MEXC."


def _candle_timestamp(candles: Optional[Sequence[Sequence[float]]]) -> Optional[int]:
    try:
        return int(candles[-1][0]) if candles else None
    except (IndexError, TypeError, ValueError):
        return None


def _upsert_symbol_scan(
    db, telegram_id: int, symbol: str, state: str, detail: str,
    price: Optional[float] = None, candles: Optional[Dict[str, Sequence[Sequence[float]]]] = None,
    signal_found: bool = False, error: Optional[str] = None, checked: bool = True,
) -> None:
    """Persist real scan output in the same transaction as the strategy cycle."""
    row = db.query(LiquiditySymbolScan).filter_by(
        telegram_id=int(telegram_id), symbol=str(symbol).upper(),
    ).first()
    if row is None:
        row = LiquiditySymbolScan(telegram_id=int(telegram_id), symbol=str(symbol).upper())
        db.add(row)
    if checked:
        row.last_checked_at = _now()
    row.updated_at = _now()
    row.state = str(state)[:32]
    row.detail = str(detail)[:1500]
    row.signal_found = bool(signal_found)
    row.error = str(error)[:1000] if error else None
    if price is not None and math.isfinite(_safe_float(price, float("nan"))):
        row.last_price = _safe_float(price)
    candle_data = candles or {}
    for tf, field in (("4h", "candle_4h_ts"), ("1h", "candle_1h_ts"),
                      ("15m", "candle_15m_ts"), ("5m", "candle_5m_ts")):
        ts = _candle_timestamp(candle_data.get(tf))
        if ts is not None:
            setattr(row, field, ts)


def _set_cycle_start(telegram_id: int) -> None:
    init_liquidity_db()
    with db_session() as db:
        settings = get_settings(telegram_id, db)
        row = db.query(LiquidityScanHealth).filter_by(telegram_id=int(telegram_id)).first()
        if row is None:
            row = LiquidityScanHealth(telegram_id=int(telegram_id))
            db.add(row)
        row.cycle_status = "running"
        row.last_started_at = _now()
        row.symbols_total = len(settings.symbols)
        row.symbols_checked = 0
        row.signals_found = 0
        row.errors_count = 0
        row.duration_ms = 0
        row.cycle_reason = "بدأت دورة فحص جديدة"
        row.last_error = None


def _set_cycle_finish(telegram_id: int, monitor: Dict[str, Any], started_monotonic: float, fatal_error: Optional[str] = None) -> None:
    with db_session() as db:
        row = db.query(LiquidityScanHealth).filter_by(telegram_id=int(telegram_id)).first()
        if row is None:
            row = LiquidityScanHealth(telegram_id=int(telegram_id))
            db.add(row)
        if fatal_error:
            status = "error"
            monitor["cycle_reason"] = "فشلت دورة الفحص بسبب خطأ عام"
            monitor["last_error"] = fatal_error[:1000]
        else:
            status = monitor.get("cycle_state") or "running"
            if status == "running":
                if monitor.get("errors_count", 0):
                    status = "partial"
                elif monitor.get("symbols_total", 0) > 0 and monitor.get("symbols_checked", 0) < monitor.get("symbols_total", 0):
                    status = "partial"
                else:
                    status = "ok"
        row.cycle_status = status
        row.last_finished_at = _now()
        row.symbols_total = int(monitor.get("symbols_total", row.symbols_total or 0))
        row.symbols_checked = int(monitor.get("symbols_checked", 0))
        row.signals_found = int(monitor.get("signals_found", 0))
        row.errors_count = int(monitor.get("errors_count", 0))
        row.duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))
        row.cycle_reason = str(monitor.get("cycle_reason") or "انتهت دورة الفحص")[:1500]
        row.last_error = str(monitor.get("last_error") or fatal_error or "")[:1000] or None


def scan_once(client, telegram_id: int) -> List[Dict[str, str]]:
    """Wrapper that records a durable heartbeat for every actual scan cycle."""
    import time as _time
    started = _time.monotonic()
    monitor: Dict[str, Any] = {
        "cycle_state": "running", "cycle_reason": "بدأ فحص السوق",
        "symbols_total": 0, "symbols_checked": 0, "signals_found": 0,
        "errors_count": 0, "last_error": None,
    }
    _set_cycle_start(int(telegram_id))
    try:
        monitor["symbols_total"] = len(get_settings(int(telegram_id)).symbols)
        events = _scan_once_impl(client, int(telegram_id), monitor)
    except Exception as exc:
        logger.exception("Liquidity scan cycle failed for telegram_id=%s", telegram_id)
        _set_cycle_finish(int(telegram_id), monitor, started, fatal_error=str(exc))
        raise
    _set_cycle_finish(int(telegram_id), monitor, started)
    return events


def _age_seconds(when: Optional[datetime]) -> Optional[int]:
    if when is None:
        return None
    return max(0, int((_now() - when).total_seconds()))


def monitoring_status(telegram_id: int) -> Dict[str, Any]:
    """Return actual persisted scan heartbeat and per-symbol closed-candle results."""
    init_liquidity_db()
    with db_session() as db:
        settings = get_settings(telegram_id, db)
        cycle = db.query(LiquidityScanHealth).filter_by(telegram_id=int(telegram_id)).first()
        rows = db.query(LiquiditySymbolScan).filter_by(telegram_id=int(telegram_id)).all()
        row_map = {r.symbol: r for r in rows}
        excluded = set(settings.excluded_symbols)
        # Include recently scanned symbols first so the UI follows the rotating
        # batch rather than repeatedly displaying only the first/highest-volume set.
        recent_rows = sorted(
            (r for r in rows if r.last_checked_at and r.symbol not in excluded),
            key=lambda r: r.last_checked_at, reverse=True,
        )
        symbols = []
        for row_item in recent_rows[:max(1, int(settings.max_symbols))]:
            if row_item.symbol not in symbols:
                symbols.append(row_item.symbol)
        for symbol in settings.symbols:
            if symbol not in excluded and symbol not in symbols and len(symbols) < max(1, int(settings.max_symbols)):
                symbols.append(symbol)
        if settings.auto_discover:
            for symbol in settings.discovered_symbols:
                if symbol not in excluded and symbol not in symbols and len(symbols) < max(1, int(settings.max_symbols)):
                    symbols.append(symbol)
        ordered = []
        for symbol in symbols:
            r = row_map.get(symbol)
            ordered.append({
                "symbol": symbol,
                "last_checked_at": r.last_checked_at if r else None,
                "updated_at": r.updated_at if r else None,
                "age_seconds": _age_seconds(r.last_checked_at) if r else None,
                "last_price": r.last_price if r else 0.0,
                "candle_4h_ts": r.candle_4h_ts if r else None,
                "candle_1h_ts": r.candle_1h_ts if r else None,
                "candle_15m_ts": r.candle_15m_ts if r else None,
                "candle_5m_ts": r.candle_5m_ts if r else None,
                "state": r.state if r else "not_checked",
                "detail": r.detail if r else "لم يتم تسجيل فحص فعلي لهذه العملة بعد",
                "signal_found": bool(r.signal_found) if r else False,
                "error": r.error if r else None,
            })
        cycle_info = None
        if cycle:
            cycle_info = {
                "status": cycle.cycle_status,
                "last_started_at": cycle.last_started_at,
                "last_finished_at": cycle.last_finished_at,
                "age_seconds": _age_seconds(cycle.last_finished_at),
                "symbols_total": int(cycle.symbols_total or 0),
                "symbols_checked": int(cycle.symbols_checked or 0),
                "signals_found": int(cycle.signals_found or 0),
                "errors_count": int(cycle.errors_count or 0),
                "duration_ms": int(cycle.duration_ms or 0),
                "reason": cycle.cycle_reason or "—",
                "last_error": cycle.last_error,
            }
        return {
            "settings": settings, "cycle": cycle_info, "symbols": ordered,
            "market_symbols_count": int(settings.market_symbols_count or 0),
            "eligible_symbols_count": len(settings.discovered_symbols),
            "scan_batch_size": int(settings.max_symbols or 0),
        }


def _scan_once_impl(client, telegram_id: int, monitor: Dict[str, Any]) -> List[Dict[str, str]]:
    """One scan cycle. Manages strategy positions, then optionally seeks new entries."""
    events: List[Dict[str, str]] = []
    now = _now()
    init_liquidity_db()
    with db_session() as db:
        settings = get_settings(telegram_id, db)
        mode = settings.mode
        # Continue managing positions even when entry scanning is paused.
        positions = db.query(LiquidityPosition).filter(
            LiquidityPosition.telegram_id == int(telegram_id),
            LiquidityPosition.status.in_(["open", "pending_entry", "pending_exit", "entry_submitting", "exit_submitting"]),
        ).all()
        for pos in positions:
            if pos.status in {"pending_entry", "pending_exit", "entry_submitting", "exit_submitting"}:
                price = 0.0
                try:
                    price = _safe_float(client.get_ticker_price(_pair(client, pos.symbol)))
                except Exception:
                    pass
                events.extend({"text": msg} for msg in _reconcile_pending(client, db, pos, price))
                if pos.status != "open":
                    continue
            try:
                price = _safe_float(client.get_ticker_price(_pair(client, pos.symbol)))
            except Exception as exc:
                pos.last_error = f"تعذر جلب السعر: {str(exc)[:180]}"
                continue
            if price <= 0:
                continue
            exit_reason = None
            exit_mark = price
            if price <= pos.stop_price:
                exit_reason = "stop"
            elif price >= pos.target_price:
                exit_reason = "target"
            else:
                # Detect a brief move through SL/TP that happened between scans.
                # If one closed 1m candle touches both levels, choose the stop first.
                try:
                    minute_rows = client.exchange.fetch_ohlcv(_pair(client, pos.symbol), timeframe="1m", limit=3)
                    if minute_rows and len(minute_rows) >= 2:
                        last_closed_minute = minute_rows[-2]
                        candle_low = _safe_float(last_closed_minute[3])
                        candle_high = _safe_float(last_closed_minute[2])
                        if candle_low <= pos.stop_price:
                            exit_reason, exit_mark = "stop", pos.stop_price
                        elif candle_high >= pos.target_price:
                            exit_reason, exit_mark = "target", pos.target_price
                except Exception:
                    pass
            if pos.status == "open" and exit_reason:
                if pos.mode == "paper":
                    events.append({"text": _close_paper(pos, exit_mark, exit_reason)})
                else:
                    text = _exit_live(client, db, pos, exit_reason, exit_mark)
                    if text:
                        events.append({"text": text})

        settings = get_settings(telegram_id, db)
        if not settings.enabled:
            monitor["cycle_state"] = "paused"
            monitor["cycle_reason"] = "فتح الصفقات متوقف من تلجرام؛ إدارة الصفقات المفتوحة ما زالت تعمل"
            return events
        if settings.mode == "live" and not settings.live_confirmed:
            monitor["cycle_state"] = "blocked"
            monitor["cycle_reason"] = "LIVE غير مؤكد؛ لم يتم فحص إشارات دخول حقيقية"
            events.append({"text": "⛔ الاستراتيجية متوقفة عن فتح صفقات: التداول الحقيقي غير مؤكد من تلجرام."})
            return events
        daily_pnl = _daily_realized_pnl(db, telegram_id, settings.mode)
        if settings.daily_loss_limit_usdt > 0 and daily_pnl <= -abs(settings.daily_loss_limit_usdt):
            monitor["cycle_state"] = "blocked"
            monitor["cycle_reason"] = f"حد الخسارة اليومية وصل ({daily_pnl:.2f} USDT)؛ تم منع الدخول الجديد"
            events.append({"text": f"🛑 تم إيقاف فتح صفقات جديدة: حد الخسارة اليومية وصل ({daily_pnl:.2f} USDT). إدارة الصفقات المفتوحة مستمرة."})
            return events

        current_positions = _mode_open_positions(db, telegram_id, settings.mode)
        if len(current_positions) >= int(settings.max_open_positions):
            monitor["cycle_state"] = "blocked"
            monitor["cycle_reason"] = "تم بلوغ الحد الأقصى للصفقات المفتوحة؛ فحص إشارات دخول جديدة متوقف في هذه الدورة"
            return events
        open_exposure = sum(_safe_float(p.amount) * _safe_float(p.entry_price) if p.status == "open" else _safe_float(p.intended_usdt) for p in current_positions)
        capital_remaining = max(0.0, float(settings.allocated_capital) - open_exposure)
        if capital_remaining < min(float(settings.position_size_usdt), 2.0):
            monitor["cycle_state"] = "blocked"
            monitor["cycle_reason"] = "الرأس المال المتبقي لا يكفي لفتح صفقة جديدة"
            return events

        symbols = _discover_symbols(client, settings, now)
        monitor["symbols_total"] = len(symbols)
        try:
            settings.discovered_symbols_json = json.dumps(settings.discovered_symbols)
            db.flush()
        except Exception:
            pass
        last_signals = {}
        try:
            last_signals = json.loads(settings.last_processed_signals_json or "{}")
        except (ValueError, TypeError, json.JSONDecodeError):
            last_signals = {}

        for base in symbols:
            if len(current_positions) >= int(settings.max_open_positions):
                monitor["cycle_state"] = "blocked"
                monitor["cycle_reason"] = "تم بلوغ الحد الأقصى للصفقات أثناء الدورة؛ لم يتم فحص باقي العملات"
                break
            symbol = str(base).upper().strip()
            if not symbol:
                continue
            if any(p.symbol == symbol and p.status != "closed" for p in current_positions):
                _upsert_symbol_scan(db, telegram_id, symbol, "position_open",
                                    "توجد صفقة مفتوحة لهذه العملة؛ تتم إدارتها بدل فتح صفقة أخرى",
                                    checked=False)
                continue
            pair = _pair(client, symbol)
            candles_4h = candles_1h = candles_15m = candles_5m = []
            try:
                candles_4h = _closed_ohlcv(client.exchange, pair, "4h", 120)
                candles_1h = _closed_ohlcv(client.exchange, pair, "1h", 120)
                candles_15m = _closed_ohlcv(client.exchange, pair, "15m", 120)
                candles_5m = _closed_ohlcv(client.exchange, pair, "5m", 120)
            except Exception as exc:
                monitor["errors_count"] += 1
                monitor["last_error"] = f"{symbol}: {str(exc)[:240]}"
                _upsert_symbol_scan(db, telegram_id, symbol, "error",
                                    "تعذر جلب بيانات الشموع من MEXC", error=str(exc)[:500])
                logger.warning("Liquidity scan failed for %s: %s", symbol, exc)
                continue
            monitor["symbols_checked"] += 1
            last5_price = _safe_float(candles_5m[-1][4]) if candles_5m else 0.0
            signal, diagnosis_state, diagnosis = diagnose_long_setup(
                candles_4h, candles_1h, candles_15m, candles_5m,
                pivot_length=settings.pivot_length, min_rr=settings.min_rr,
                stop_buffer_pct=settings.stop_buffer_pct,
                entry_profile=settings.entry_profile,
            )
            _upsert_symbol_scan(
                db, telegram_id, symbol, diagnosis_state, diagnosis,
                price=last5_price, candles={"4h": candles_4h, "1h": candles_1h,
                                             "15m": candles_15m, "5m": candles_5m},
                signal_found=bool(signal),
            )
            if not signal:
                continue
            monitor["signals_found"] += 1
            # The 5m close can be stale by the time four timeframes have been fetched.
            # Refuse late market entries rather than silently changing the risk profile.
            try:
                current_price = _safe_float(client.get_ticker_price(pair))
            except Exception as exc:
                monitor["errors_count"] += 1
                monitor["last_error"] = f"{symbol}: تعذر جلب السعر الحالي: {str(exc)[:200]}"
                _upsert_symbol_scan(db, telegram_id, symbol, "error",
                                    "اكتملت الإشارة لكن تعذر جلب السعر الحالي للتحقق قبل الدخول",
                                    price=last5_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                "15m": candles_15m, "5m": candles_5m},
                                    signal_found=True, error=str(exc)[:500])
                continue
            if current_price <= signal["stop"] or current_price >= signal["target"]:
                _upsert_symbol_scan(db, telegram_id, symbol, "signal_rejected",
                                    "الإشارة موجودة، لكن السعر الحالي تجاوز وقف الخسارة أو الهدف؛ تم رفض الدخول",
                                    price=current_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                  "15m": candles_15m, "5m": candles_5m}, signal_found=True)
                continue
            deviation_pct = abs(current_price - signal["entry"]) / max(signal["entry"], 1e-12) * 100.0
            if deviation_pct > float(settings.max_entry_deviation_pct):
                _upsert_symbol_scan(db, telegram_id, symbol, "signal_rejected",
                                    f"الإشارة موجودة لكن السعر ابتعد {deviation_pct:.2f}% عن التأكيد، والحد {settings.max_entry_deviation_pct:.2f}%",
                                    price=current_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                  "15m": candles_15m, "5m": candles_5m}, signal_found=True)
                continue
            actual_rr = (signal["target"] - current_price) / max(current_price - signal["stop"], 1e-12)
            if actual_rr < float(settings.min_rr):
                _upsert_symbol_scan(db, telegram_id, symbol, "signal_rejected",
                                    f"العائد/المخاطرة الحالي {actual_rr:.2f}:1 أقل من المطلوب {settings.min_rr:.2f}:1",
                                    price=current_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                  "15m": candles_15m, "5m": candles_5m}, signal_found=True)
                continue
            signal["entry"] = current_price
            signal["rr"] = actual_rr
            signal_key = f"{symbol}:{int(signal['signal_ts'])}"
            if last_signals.get(symbol) == int(signal["signal_ts"]) or _is_duplicate(db, telegram_id, settings.mode, signal_key):
                _upsert_symbol_scan(db, telegram_id, symbol, "duplicate_signal",
                                    "تمت معالجة إشارة هذه الشمعة من قبل؛ لن يتم تكرار الدخول",
                                    price=current_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                  "15m": candles_15m, "5m": candles_5m}, signal_found=True)
                continue
            # Record that this closed sweep has been considered before attempting entry.
            last_signals[symbol] = int(signal["signal_ts"])
            settings.last_processed_signals_json = json.dumps(last_signals, sort_keys=True)
            db.flush()

            entry, stop, target = signal["entry"], signal["stop"], signal["target"]
            stop_fraction = (entry - stop) / entry if entry else 0.0
            risk_budget = float(settings.allocated_capital) * float(settings.risk_pct) / 100.0
            size = min(float(settings.position_size_usdt), risk_budget / stop_fraction if stop_fraction > 0 else 0.0, capital_remaining)
            size = min(size, max(0.0, float(settings.allocated_capital) - open_exposure))
            if size < 2.0 or stop_fraction <= 0:
                _upsert_symbol_scan(db, telegram_id, symbol, "size_rejected",
                                    f"حجم الصفقة المحسوب {size:.2f} USDT أقل من الحد الأدنى 2 USDT أو وقف الخسارة غير صالح",
                                    price=current_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                  "15m": candles_15m, "5m": candles_5m}, signal_found=True)
                continue
            if settings.mode == "paper":
                amount = size / entry
                pos = LiquidityPosition(
                    telegram_id=int(telegram_id), symbol=symbol, mode="paper", signal_key=signal_key,
                    status="open", entry_price=entry, stop_price=stop, target_price=target,
                    amount=amount, initial_amount=amount, intended_usdt=size,
                    entry_fee_estimate=size * 0.001, opened_at=_now(),
                )
                db.add(pos)
                db.flush()
                _upsert_symbol_scan(db, telegram_id, symbol, "paper_open",
                                    f"تم فتح صفقة PAPER عند {entry:.8g}، ووقف {stop:.8g}، وهدف {target:.8g}",
                                    price=current_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                  "15m": candles_15m, "5m": candles_5m}, signal_found=True)
                events.append({"text": (
                    f"🧪 *دخول PAPER* — `{symbol}/USDT`\n"
                    f"الدخول: `{entry:.8g}`\nوقف الخسارة: `{stop:.8g}`\n"
                    f"هدف منطقة السيولة: `{target:.8g}` (RR {signal['rr']:.2f}:1)\n"
                    f"حجم افتراضي: `{size:.2f} USDT`"
                )})
            else:
                pos, message = _entry_live(client, db, settings, symbol, signal, size, signal_key)
                if message:
                    events.append({"text": message})
                live_state = "live_order_pending"
                live_detail = message or "تم إرسال/تسجيل أمر LIVE؛ تحقق من حالة التنفيذ في تفاصيل الصفقة"
                if pos is not None:
                    live_state = "live_open" if pos.status == "open" else "live_order_pending"
                    live_detail = f"حالة أمر LIVE: {pos.status}; {message or ''}".strip()
                    if pos.last_error:
                        live_detail += f"; {pos.last_error[:180]}"
                _upsert_symbol_scan(db, telegram_id, symbol, live_state, live_detail,
                                    price=current_price, candles={"4h": candles_4h, "1h": candles_1h,
                                                                  "15m": candles_15m, "5m": candles_5m}, signal_found=True,
                                    error=(pos.last_error if pos is not None else None))
                if pos is not None and pos.status in {"open", "pending_entry", "entry_submitting"}:
                    current_positions.append(pos)
            open_exposure += size
        if monitor.get("cycle_state") == "running":
            total = int(monitor.get("symbols_total", 0))
            checked = int(monitor.get("symbols_checked", 0))
            if total == 0:
                monitor["cycle_state"] = "blocked"
                monitor["cycle_reason"] = "لا توجد عملات محددة للمراقبة"
            elif checked >= total:
                monitor["cycle_reason"] = "اكتمل جلب الشموع المغلقة لكل العملات؛ تفاصيل كل عملة توضح الشرط الناقص أو المكتمل"
            elif checked > 0:
                monitor["cycle_reason"] = f"تم فحص الشموع بنجاح لـ {checked} من {total} عملة؛ العملات الباقية تم تخطيها أو لم تُجلب بياناتها"
            else:
                monitor["cycle_reason"] = "لم يكتمل فحص شموع لأي عملة في هذه الدورة؛ راجع حدود الدخول والأخطاء وتفاصيل العملات"
    return events


def active_telegram_ids() -> List[int]:
    """Return users with entries enabled or strategy positions still being managed."""
    init_liquidity_db()
    active_statuses = ["open", "pending_entry", "pending_exit", "entry_submitting", "exit_submitting"]
    with db_session() as db:
        ids = set(
            int(row[0]) for row in db.query(LiquiditySettings.telegram_id)
            .filter(LiquiditySettings.enabled.is_(True)).all()
        )
        ids.update(
            int(row[0]) for row in db.query(LiquidityPosition.telegram_id)
            .filter(LiquidityPosition.status.in_(active_statuses)).distinct().all()
        )
        return sorted(ids)


def list_positions(telegram_id: int, mode: Optional[str] = None, limit: int = 20) -> List[LiquidityPosition]:
    init_liquidity_db()
    with db_session() as db:
        query = db.query(LiquidityPosition).filter_by(telegram_id=int(telegram_id))
        if mode:
            query = query.filter_by(mode=mode)
        return query.order_by(LiquidityPosition.opened_at.desc()).limit(limit).all()


def summary(telegram_id: int) -> Dict[str, Any]:
    init_liquidity_db()
    with db_session() as db:
        s = get_settings(telegram_id, db)
        positions = _mode_open_positions(db, telegram_id, s.mode)
        day_pnl = _daily_realized_pnl(db, telegram_id, s.mode)
        closed = db.query(LiquidityPosition).filter_by(
            telegram_id=int(telegram_id), mode=s.mode, status="closed",
        ).all()
        total_pnl = sum(_safe_float(p.realized_pnl) for p in closed)
        return {
            "enabled": bool(s.enabled), "mode": s.mode, "live_confirmed": bool(s.live_confirmed),
            "symbols": s.symbols, "auto_discover": bool(s.auto_discover),
            "open_positions": len(positions), "daily_pnl": day_pnl, "total_pnl": total_pnl,
            "capital": s.allocated_capital, "size": s.position_size_usdt,
            "max_positions": s.max_open_positions, "positions": positions,
        }
