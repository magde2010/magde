"""
Lotfe Flow Engine
=================
A signal layer that combines whale flow with spot-market confirmation.

The engine is intentionally read-only. It does not place orders; whale_reco.py
handles optional execution after the signal has passed confirmation gates.

Inputs:
- CoinLobster whale-radar (authenticated when COINLOBSTER_API_KEY is set;
  keyless public radar is used as fallback)
- MEXC spot ticker / OHLCV / recent trades / order book via ccxt

The scoring goal is not "whales bought = buy". It tries to find an early setup:
strong whale activity + healthy spot confirmation + price not already extended.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

COINLOBSTER_BASE = "https://coinlobster.com"
PUBLIC_RADAR = f"{COINLOBSTER_BASE}/api/public/whale-radar"
AUTH_RADAR = f"{COINLOBSTER_BASE}/api/ai/v1/whales/radar"

DEFAULT_EXCLUDED = {
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "PYUSD", "USDE", "USDS",
    "USD1", "USDD", "EURC", "WBTC",
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


def _key() -> str:
    return (os.getenv("COINLOBSTER_API_KEY") or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _pct_change(new: float, old: float) -> float:
    if old <= 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def _mean(values: Iterable[float]) -> float:
    vals = [float(v) for v in values]
    return sum(vals) / len(vals) if vals else 0.0


def _stddev(values: Iterable[float]) -> float:
    vals = [float(v) for v in values]
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))


def _request_json(url: str, *, auth: bool = False, timeout: int = 15) -> Optional[dict]:
    headers = {
        "User-Agent": "LotfeFlowEngine/2.0",
        "Accept": "application/json",
    }
    if auth and _key():
        headers["Authorization"] = f"Bearer {_key()}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            data = json.loads(payload)
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Flow Engine GET failed %s: %s", url[:100], exc)
        return None


def _extract_rows(payload: Optional[dict], window: str = "1h") -> List[Dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload.get("rows"), list):
        return [x for x in payload["rows"] if isinstance(x, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        windows = data.get("windows")
        if isinstance(windows, dict):
            rows = windows.get(window) or windows.get("4h") or windows.get("24h") or []
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
        for key in ("rows", "items", "results"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _get_first(raw: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


@dataclass(frozen=True)
class FlowSettings:
    radar_window: str = "1h"
    radar_cache_seconds: int = 90
    market_cache_seconds: int = 60
    candidate_limit: int = 10
    analysis_limit: int = 8
    min_score_watch: float = 65.0
    min_score_buy: float = 82.0
    min_score_live: float = 88.0
    min_buy_share: float = 0.56
    min_unusual_multiple: float = 1.5
    max_1h_pump_pct: float = 4.0
    max_24h_pump_pct: float = 5.0
    max_distance_ema20_pct: float = 8.0
    min_quote_volume_usdt: float = 750_000.0
    min_orderbook_imbalance: float = 0.03
    ohlcv_timeframe: str = "15m"
    ohlcv_bars: int = 80
    trades_limit: int = 120
    orderbook_limit: int = 20
    confirmation_minutes: int = 15
    max_confirmation_age_minutes: int = 30
    enable_mexc_market_data: bool = True
    excluded_symbols: Tuple[str, ...] = tuple(sorted(DEFAULT_EXCLUDED))

    @classmethod
    def from_env(cls) -> "FlowSettings":
        excluded = {
            x.strip().upper()
            for x in os.getenv("FLOW_EXCLUDED_SYMBOLS", "").split(",")
            if x.strip()
        }
        excluded |= DEFAULT_EXCLUDED
        return cls(
            radar_window=os.getenv("FLOW_RADAR_WINDOW", "1h").strip() or "1h",
            radar_cache_seconds=max(30, _env_int("FLOW_RADAR_CACHE_SECONDS", 90)),
            market_cache_seconds=max(20, _env_int("FLOW_MARKET_CACHE_SECONDS", 60)),
            candidate_limit=max(3, _env_int("FLOW_CANDIDATE_LIMIT", 10)),
            analysis_limit=max(3, _env_int("FLOW_ANALYSIS_LIMIT", 8)),
            min_score_watch=_env_float("FLOW_MIN_SCORE_WATCH", 65.0),
            min_score_buy=_env_float("FLOW_MIN_SCORE_BUY", 82.0),
            min_score_live=_env_float("FLOW_MIN_SCORE_LIVE", 88.0),
            min_buy_share=_env_float("FLOW_MIN_BUY_SHARE", 0.56),
            min_unusual_multiple=_env_float("FLOW_MIN_UNUSUAL_MULTIPLE", 1.5),
            max_1h_pump_pct=_env_float("FLOW_MAX_1H_PUMP_PCT", 4.0),
            max_24h_pump_pct=_env_float("FLOW_MAX_24H_PUMP_PCT", 5.0),
            max_distance_ema20_pct=_env_float("FLOW_MAX_DISTANCE_EMA20_PCT", 8.0),
            min_quote_volume_usdt=_env_float("FLOW_MIN_QUOTE_VOLUME_USDT", 750_000.0),
            min_orderbook_imbalance=_env_float("FLOW_MIN_ORDERBOOK_IMBALANCE", 0.03),
            ohlcv_timeframe=os.getenv("FLOW_OHLCV_TIMEFRAME", "15m"),
            ohlcv_bars=max(40, _env_int("FLOW_OHLCV_BARS", 80)),
            trades_limit=max(40, _env_int("FLOW_TRADES_LIMIT", 120)),
            orderbook_limit=max(5, _env_int("FLOW_ORDERBOOK_LIMIT", 20)),
            confirmation_minutes=max(5, _env_int("FLOW_CONFIRMATION_MINUTES", 15)),
            max_confirmation_age_minutes=max(confirmation_minutes, _env_int("FLOW_MAX_CONFIRMATION_AGE_MINUTES", 30)),
            enable_mexc_market_data=_env_bool("FLOW_ENABLE_MEXC_MARKET_DATA", True),
            excluded_symbols=tuple(sorted(excluded)),
        )


@dataclass
class FlowSignal:
    symbol: str
    score: float
    classification: str
    eligible: bool
    buy_share: float = 0.5
    whale_net_usd: float = 0.0
    whale_multiple: float = 1.0
    unusual: bool = False
    direction: str = ""
    price: float = 0.0
    change_1h_pct: float = 0.0
    change_24h_pct: float = 0.0
    distance_ema20_pct: float = 0.0
    volume_acceleration: float = 0.0
    cvd_pct: float = 0.0
    orderbook_imbalance: float = 0.0
    btc_regime: str = "محايد"
    confirmation_score: float = 0.0
    reasons: List[str] = None
    block_reasons: List[str] = None
    components: Dict[str, float] = None
    raw: Dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []
        if self.block_reasons is None:
            self.block_reasons = []
        if self.components is None:
            self.components = {}
        if self.raw is None:
            self.raw = {}

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FlowEngine:
    """Reusable scoring engine for both Telegram radar and the smart subsystem."""

    def __init__(self, settings: Optional[FlowSettings] = None):
        self.settings = settings or FlowSettings.from_env()
        self._radar: Dict[str, Any] = {"ts": 0.0, "rows": []}
        self._market_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._exchange = None

    def _exchange_client(self):
        if not self.settings.enable_mexc_market_data:
            return None
        if self._exchange is None:
            try:
                import ccxt  # type: ignore
                self._exchange = ccxt.mexc({
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                })
            except Exception as exc:
                logger.warning("ccxt/MEXC market data unavailable: %s", exc)
                self._exchange = False
        return None if self._exchange is False else self._exchange

    def _load_radar(self) -> List[Dict[str, Any]]:
        now = time.time()
        if now - float(self._radar.get("ts", 0.0)) < self.settings.radar_cache_seconds:
            return list(self._radar.get("rows") or [])

        rows: List[Dict[str, Any]] = []
        key = _key()
        if key:
            url = AUTH_RADAR + "?" + urllib.parse.urlencode({"window": self.settings.radar_window})
            rows = _extract_rows(_request_json(url, auth=True), self.settings.radar_window)
        if not rows:
            url = PUBLIC_RADAR + "?" + urllib.parse.urlencode({"window": self.settings.radar_window})
            rows = _extract_rows(_request_json(url, auth=False), self.settings.radar_window)

        self._radar = {"ts": now, "rows": rows}
        return list(rows)

    def _normalise_radar(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = str(_get_first(row, "coin", "symbol", "asset", default="") or "").upper().strip()
        if not symbol or "/" in symbol or symbol in self.settings.excluded_symbols:
            return None
        if len(symbol) > 15:
            return None

        buy = _safe_float(_get_first(row, "buyUsd", "buy_usd", "buy", "buysUsd", default=0))
        sell = _safe_float(_get_first(row, "sellUsd", "sell_usd", "sell", "sellsUsd", default=0))
        net = _safe_float(_get_first(row, "netUsd", "net_usd", "net", default=buy - sell))
        total = buy + sell
        buy_share = _safe_float(_get_first(row, "buyShare", "buy_share", "buy_share_24h", default=0.5))
        if buy_share > 1.0:
            buy_share /= 100.0
        if total > 0:
            buy_share = buy / total
        multiple = _safe_float(_get_first(row, "multiple", "multiple_vs_avg", "activity_multiple", default=1.0), 1.0)
        unusual = bool(_get_first(row, "unusual", "flagged", default=False))
        direction = str(_get_first(row, "direction", "side", default="") or "").lower()
        if not direction:
            direction = "buy" if net > 0 else "sell" if net < 0 else "neutral"
        price = _safe_float(_get_first(row, "price", "last", default=0))

        return {
            "symbol": symbol,
            "buy": buy,
            "sell": sell,
            "net": net,
            "total": total,
            "buy_share": _clamp(buy_share, 0, 1),
            "multiple": max(0.0, multiple),
            "unusual": unusual,
            "direction": direction,
            "price": price,
            "raw": row,
        }

    def _radar_candidates(self) -> List[Dict[str, Any]]:
        normalised = []
        for raw in self._load_radar():
            item = self._normalise_radar(raw)
            if item:
                normalised.append(item)
        # Prefer unusual positive pressure, then net flow, then multiple.
        normalised.sort(
            key=lambda x: (
                1 if x["direction"] == "buy" else 0,
                1 if x["unusual"] else 0,
                x["buy_share"],
                x["net"],
                x["multiple"],
            ),
            reverse=True,
        )
        return normalised[: self.settings.candidate_limit]

    def _mexc_market(self, symbol: str) -> Dict[str, Any]:
        now = time.time()
        cached = self._market_cache.get(symbol)
        if cached and now - cached[0] < self.settings.market_cache_seconds:
            return dict(cached[1])

        exchange = self._exchange_client()
        if exchange is None:
            return {}
        pair = f"{symbol}/USDT"
        out: Dict[str, Any] = {"symbol": symbol}
        try:
            ticker = exchange.fetch_ticker(pair)
            out["price"] = _safe_float(ticker.get("last") or ticker.get("close"))
            out["change_24h_pct"] = _safe_float(ticker.get("percentage"))
            out["quote_volume"] = _safe_float(ticker.get("quoteVolume"))
            out["bid"] = _safe_float(ticker.get("bid"))
            out["ask"] = _safe_float(ticker.get("ask"))
            if out.get("bid") and out.get("ask"):
                mid = (out["bid"] + out["ask"]) / 2
                out["spread_pct"] = ((out["ask"] - out["bid"]) / mid * 100) if mid else 0.0
        except Exception as exc:
            logger.debug("ticker %s: %s", pair, exc)

        ohlcv: List[List[float]] = []
        try:
            ohlcv = exchange.fetch_ohlcv(pair, timeframe=self.settings.ohlcv_timeframe, limit=self.settings.ohlcv_bars) or []
            closes = [float(x[4]) for x in ohlcv if len(x) >= 6 and _safe_float(x[4]) > 0]
            vols = [float(x[5]) for x in ohlcv if len(x) >= 6]
            if closes:
                price = out.get("price") or closes[-1]
                out["price"] = price
                # 15m bars: 4 bars ~= 1h. Timeframe parser supports our common values.
                bars_per_hour = {"1m": 60, "5m": 12, "15m": 4, "30m": 2, "1h": 1}.get(self.settings.ohlcv_timeframe, 4)
                h1_index = max(0, len(closes) - 1 - bars_per_hour)
                h4_index = max(0, len(closes) - 1 - bars_per_hour * 4)
                out["change_1h_pct"] = _pct_change(price, closes[h1_index])
                out["change_4h_pct"] = _pct_change(price, closes[h4_index])
                window = closes[-20:] if len(closes) >= 20 else closes
                ema = window[0]
                alpha = 2 / (len(window) + 1)
                for close in window[1:]:
                    ema = alpha * close + (1 - alpha) * ema
                out["ema20"] = ema
                out["distance_ema20_pct"] = _pct_change(price, ema)
                if len(closes) >= 16:
                    recent_ranges = [abs(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(len(closes) - 15, len(closes)) if closes[i - 1] > 0]
                    older_ranges = [abs(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(len(closes) - 35, len(closes) - 15) if closes[i - 1] > 0]
                    out["range_compression"] = (_mean(older_ranges) / _mean(recent_ranges)) if _mean(recent_ranges) > 0 else 1.0
            if vols:
                recent = _mean(vols[-4:]) if len(vols) >= 4 else _mean(vols)
                prior = _mean(vols[-16:-4]) if len(vols) >= 16 else _mean(vols[:-4]) if len(vols) > 4 else 0.0
                out["volume_acceleration"] = recent / prior if prior > 0 else 1.0
        except Exception as exc:
            logger.debug("ohlcv %s: %s", pair, exc)

        try:
            trades = exchange.fetch_trades(pair, limit=self.settings.trades_limit) or []
            buy_notional = sell_notional = 0.0
            previous_price = 0.0
            for t in trades:
                price = _safe_float(t.get("price"))
                amount = _safe_float(t.get("amount"))
                notional = price * amount
                side = str(t.get("side") or "").lower()
                if side == "buy":
                    buy_notional += notional
                elif side == "sell":
                    sell_notional += notional
                elif previous_price > 0:
                    if price >= previous_price:
                        buy_notional += notional
                    else:
                        sell_notional += notional
                previous_price = price or previous_price
            total = buy_notional + sell_notional
            out["cvd_pct"] = ((buy_notional - sell_notional) / total * 100) if total > 0 else 0.0
            out["trade_notional"] = total
        except Exception as exc:
            logger.debug("trades %s: %s", pair, exc)

        try:
            ob = exchange.fetch_order_book(pair, limit=self.settings.orderbook_limit)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            bid_depth = sum(_safe_float(px) * _safe_float(amount) for px, amount, *_ in bids)
            ask_depth = sum(_safe_float(px) * _safe_float(amount) for px, amount, *_ in asks)
            depth_total = bid_depth + ask_depth
            out["orderbook_imbalance"] = ((bid_depth - ask_depth) / depth_total) if depth_total > 0 else 0.0
        except Exception as exc:
            logger.debug("orderbook %s: %s", pair, exc)

        self._market_cache[symbol] = (now, dict(out))
        return out

    def _btc_regime(self) -> str:
        market = self._mexc_market("BTC")
        if not market:
            return "محايد"
        c1 = _safe_float(market.get("change_1h_pct"))
        c4 = _safe_float(market.get("change_4h_pct"))
        d20 = _safe_float(market.get("distance_ema20_pct"))
        if c4 >= 1.0 and c1 >= -0.5 and d20 >= -3.0:
            return "داعم 🟢"
        if c4 <= -1.5 or (c1 <= -1.5 and d20 <= -3.0):
            return "ضاغط 🔴"
        return "محايد 🟡"

    def _score_whale(self, x: Dict[str, Any]) -> Tuple[float, List[str]]:
        score = 0.0
        reasons: List[str] = []
        bs = x["buy_share"]
        multiple = x["multiple"]
        net = x["net"]
        total = x["total"]

        score += _clamp((bs - 0.50) / 0.20 * 14.0, 0, 14)
        if bs >= 0.62:
            reasons.append(f"شراء حيتان {bs*100:.0f}%")
        elif bs >= self.settings.min_buy_share:
            reasons.append(f"ضغط شراء {bs*100:.0f}%")
        if multiple >= 3.0:
            score += 8
            reasons.append(f"نشاط {multiple:.1f}x الطبيعي")
        elif multiple >= self.settings.min_unusual_multiple:
            score += 5
            reasons.append(f"نشاط {multiple:.1f}x")
        if x["unusual"]:
            score += 4
            reasons.append("الرادار معلّم Unusual")
        if total > 0 and abs(net) > 0:
            net_share = abs(net) / total
            score += min(4, net_share * 5)
        if net <= 0 or x["direction"] == "sell":
            score -= 8
        return _clamp(score, 0, 25), reasons

    def _score_structure(self, market: Dict[str, Any]) -> Tuple[float, List[str], List[str]]:
        score = 0.0
        reasons: List[str] = []
        blocks: List[str] = []
        c1 = _safe_float(market.get("change_1h_pct"))
        c24 = _safe_float(market.get("change_24h_pct"))
        d20 = _safe_float(market.get("distance_ema20_pct"))
        accel = _safe_float(market.get("volume_acceleration"), 1.0)
        compression = _safe_float(market.get("range_compression"), 1.0)

        if c24 > self.settings.max_24h_pump_pct:
            blocks.append(f"ارتفع {c24:+.1f}% خلال 24س — مطاردة")
        elif c24 > 5:
            score += 3
        elif -6 <= c24 <= 3:
            score += 8
            reasons.append("السعر لم ينطلق بقوة بعد")
        elif c24 < -12:
            score += 2
            reasons.append("هبوط عميق — يحتاج تأكيد إضافي")
        else:
            score += 4

        if c1 > self.settings.max_1h_pump_pct:
            blocks.append(f"قفزة {c1:+.1f}% خلال ساعة")
        elif -2.0 <= c1 <= 2.5:
            score += 5
        else:
            score += 2

        if abs(d20) <= self.settings.max_distance_ema20_pct:
            score += 6
            if -2.0 <= d20 <= 3.5:
                reasons.append("قريب من EMA20 — ليس بعيدًا عن القاعدة")
        elif d20 > self.settings.max_distance_ema20_pct:
            blocks.append(f"بعيد عن EMA20 ({d20:+.1f}%)")

        if accel >= 1.4:
            score += 4
            reasons.append(f"الحجم يتسارع {accel:.1f}x")
        elif accel >= 1.15:
            score += 2
        if compression >= 1.15:
            score += 2
            reasons.append("تذبذب منضغط قبل الحركة")

        return _clamp(score, 0, 25), reasons, blocks

    def _score_confirmation(self, market: Dict[str, Any], btc_regime: str) -> Tuple[float, List[str]]:
        score = 0.0
        reasons: List[str] = []
        cvd = _safe_float(market.get("cvd_pct"))
        ob = _safe_float(market.get("orderbook_imbalance"))
        spread = _safe_float(market.get("spread_pct"))
        volume = _safe_float(market.get("quote_volume"))
        if cvd >= 12:
            score += 7
            reasons.append(f"CVD موجب {cvd:+.0f}%")
        elif cvd >= 4:
            score += 4
        elif cvd < -8:
            score -= 3
        if ob >= 0.12:
            score += 6
            reasons.append(f"Order Book لصالح المشترين {ob*100:+.0f}%")
        elif ob >= self.settings.min_orderbook_imbalance:
            score += 3
        elif ob <= -0.12:
            score -= 3
        if volume >= self.settings.min_quote_volume_usdt:
            score += 4
        if spread > 1.0:
            score -= 2
        elif 0 < spread <= 0.8:
            score += 2
        if btc_regime.startswith("داعم"):
            score += 2
        elif btc_regime.startswith("ضاغط"):
            score -= 2
        return _clamp(score, 0, 20), reasons

    def analyse(self, item: Dict[str, Any], btc_regime: Optional[str] = None) -> FlowSignal:
        symbol = item["symbol"]
        market = self._mexc_market(symbol) if self.settings.enable_mexc_market_data else {}
        regime = btc_regime or self._btc_regime()
        whale_score, whale_reasons = self._score_whale(item)
        structure_score, structure_reasons, block_reasons = self._score_structure(market)
        confirm_score, confirm_reasons = self._score_confirmation(market, regime)

        price = _safe_float(market.get("price"), item.get("price", 0.0))
        c1 = _safe_float(market.get("change_1h_pct"))
        c24 = _safe_float(market.get("change_24h_pct"))
        if c24 == 0 and "change_24h_pct" in item["raw"]:
            c24 = _safe_float(item["raw"].get("change_24h_pct"))
        if not market and c24 == 0:
            # No market data: keep whale-only signal visible but never call it a buy.
            block_reasons.append("لا توجد بيانات سوق فورية للتأكيد")

        # Base market-quality points: volume, price data and a clean spread.
        quality = 0.0
        reasons: List[str] = []
        reasons.extend(whale_reasons)
        reasons.extend(structure_reasons)
        reasons.extend(confirm_reasons)
        quote_volume = _safe_float(market.get("quote_volume"))
        if quote_volume >= self.settings.min_quote_volume_usdt:
            quality += 5
        else:
            if market:
                block_reasons.append(f"حجم 24س منخفض ({quote_volume/1e6:.2f}M$)")
        spread = _safe_float(market.get("spread_pct"))
        if 0 < spread <= 0.8:
            quality += 3
        elif spread > 1.5:
            block_reasons.append(f"Spread مرتفع {spread:.2f}%")

        # Raw components max at 78 points in this model; normalize to a user-friendly 0-100 scale.
        score = _clamp((whale_score + structure_score + confirm_score + quality) / 78.0 * 100.0)
        eligible = True
        if item["buy_share"] < self.settings.min_buy_share:
            block_reasons.append(f"نسبة الشراء {item['buy_share']*100:.0f}% أقل من الحد")
        if item["net"] <= 0 and item["total"] > 0:
            block_reasons.append("صافي تدفق الحيتان غير موجب")
        if not item["unusual"] and item["multiple"] < self.settings.min_unusual_multiple:
            block_reasons.append("نشاط الحيتان غير كافٍ")
        if block_reasons:
            eligible = False

        if score >= self.settings.min_score_buy and eligible:
            classification = "🔥 فرصة مبكرة"
        elif score >= self.settings.min_score_watch:
            classification = "🟢 شراء مشروط" if eligible else "🟡 مراقبة"
        else:
            classification = "🟡 مراقبة" if score >= 50 else "🔴 تجنب"

        components = {
            "whale": round(whale_score, 2),
            "structure": round(structure_score, 2),
            "confirmation": round(confirm_score, 2),
            "quality": round(quality, 2),
        }
        if not reasons:
            reasons = ["بيانات غير كافية لإضافة سبب إيجابي"]

        return FlowSignal(
            symbol=symbol,
            score=round(score, 2),
            classification=classification,
            eligible=eligible,
            buy_share=item["buy_share"],
            whale_net_usd=item["net"],
            whale_multiple=item["multiple"],
            unusual=item["unusual"],
            direction=item["direction"],
            price=price,
            change_1h_pct=c1,
            change_24h_pct=c24,
            distance_ema20_pct=_safe_float(market.get("distance_ema20_pct")),
            volume_acceleration=_safe_float(market.get("volume_acceleration"), 1.0),
            cvd_pct=_safe_float(market.get("cvd_pct")),
            orderbook_imbalance=_safe_float(market.get("orderbook_imbalance")),
            btc_regime=regime,
            confirmation_score=round(confirm_score, 2),
            reasons=reasons[:8],
            block_reasons=block_reasons[:8],
            components=components,
            raw={"radar": item["raw"], "market": market},
        )

    def scan(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        candidates = self._radar_candidates()
        if not candidates:
            return []
        btc_regime = self._btc_regime() if self.settings.enable_mexc_market_data else "محايد"
        out: List[FlowSignal] = []
        for item in candidates[: self.settings.analysis_limit]:
            try:
                out.append(self.analyse(item, btc_regime=btc_regime))
            except Exception as exc:
                logger.exception("analyse %s failed: %s", item.get("symbol"), exc)
        out.sort(key=lambda s: (s.score, s.eligible, s.whale_net_usd), reverse=True)
        chosen = out[: (limit or 8)]
        return [s.as_dict() for s in chosen]


def build_flow_engine() -> FlowEngine:
    return FlowEngine(FlowSettings.from_env())


_ENGINE: Optional[FlowEngine] = None


def get_flow_engine() -> FlowEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = build_flow_engine()
    return _ENGINE


def scan_flow(limit: int = 8) -> List[Dict[str, Any]]:
    """Convenience API used by the bot."""
    return get_flow_engine().scan(limit=limit)
