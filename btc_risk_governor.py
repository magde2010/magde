"""BTC Risk Governor for LOTFE.

BTC is the single market-risk governor. It never force-sells portfolios and it
never decides which coin to buy. It only controls how much *new* risk each
portfolio is allowed to add.

Risk score: 0..100
  80..100  STRONG
  65..79   HEALTHY
  45..64   CAUTION
  25..44   DEFENSIVE
   0..24   CRASH

The score falls quickly when BTC deteriorates, but recovery is deliberately
slow (hysteresis) so one green tick cannot instantly restore full risk.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional

import config

logger = logging.getLogger(__name__)
WS_URL = "wss://wbs-api.mexc.com/ws"


class BTCRiskBlocked(RuntimeError):
    """Raised when BTC risk is too low to add a new position."""


@dataclass(frozen=True)
class BTCSnapshot:
    score: float
    state: str
    price: float
    one_min_pct: Optional[float]
    five_min_pct: Optional[float]
    fifteen_min_pct: Optional[float]
    acceleration_pct: Optional[float]
    volatility_pct: Optional[float]
    age_sec: float
    stale: bool
    reason: str


class BTCRiskGovernor:
    def __init__(self):
        self.enabled = bool(config.BTC_RISK_GOVERNOR_ENABLED)
        self.stale_after = float(config.BTC_RISK_STALE_AFTER_SEC)
        self.recovery_points_per_sec = float(config.BTC_RISK_RECOVERY_POINTS_PER_SEC)
        self.min_buy_usdt = float(config.BTC_RISK_MIN_BUY_USDT)
        self._lock = threading.RLock()
        self._prices = deque(maxlen=1200)
        self._minute_open = 0.0
        self._last_update = 0.0
        self._score: Optional[float] = None
        self._raw_score = 0.0
        self._last_score_change = time.time()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, float(value)))

    @staticmethod
    def _clean_symbol(symbol: str) -> str:
        s = str(symbol or "").upper().replace(" ", "")
        for suffix in ("/USDT", "USDT"):
            if s.endswith(suffix):
                return s[:-len(suffix)]
        return s

    def update_price(self, symbol: str, price: float, event_ts: Optional[float] = None,
                     minute_open: Optional[float] = None):
        if self._clean_symbol(symbol) != "BTC":
            return
        try:
            px = float(price)
        except (TypeError, ValueError):
            return
        if px <= 0:
            return
        ts = float(event_ts or time.time())
        with self._lock:
            self._prices.append((ts, px))
            self._last_update = ts
            if minute_open and float(minute_open) > 0:
                self._minute_open = float(minute_open)
        self.evaluate()

    def _return_since(self, seconds: float, now: float) -> Optional[float]:
        if not self._prices:
            return None
        if now - self._prices[0][0] < seconds * 0.80:
            return None
        target = now - seconds
        baseline = self._prices[0][1]
        for ts, price in self._prices:
            if ts >= target:
                baseline = price
                break
        if baseline <= 0:
            return None
        return (self._prices[-1][1] / baseline - 1.0) * 100.0

    def _volatility(self) -> Optional[float]:
        if len(self._prices) < 6:
            return None
        recent = list(self._prices)[-30:]
        changes = []
        for (_, a), (_, b) in zip(recent, recent[1:]):
            if a > 0 and b > 0:
                changes.append(abs((b / a - 1.0) * 100.0))
        if not changes:
            return None
        return sum(changes) / len(changes)

    def _raw_score_locked(self, now: float) -> float:
        if not self._last_update or now - self._last_update > self.stale_after:
            return 0.0
        price = self._prices[-1][1] if self._prices else 0.0
        one = None
        if self._minute_open > 0:
            one = (price / self._minute_open - 1.0) * 100.0
        five = self._return_since(300.0, now)
        fifteen = self._return_since(900.0, now)
        accel = None
        if one is not None and five is not None:
            accel = one - (five / 5.0)
        vol = self._volatility()

        # BTC-only risk model. Positive BTC momentum adds risk budget; downside
        # speed and volatility remove it. tanh keeps extreme candles bounded.
        score = 50.0
        if one is not None:
            score += 22.0 * math.tanh(one / 1.0)
        if five is not None:
            score += 26.0 * math.tanh(five / 2.5)
        if fifteen is not None:
            score += 16.0 * math.tanh(fifteen / 5.0)
        if accel is not None:
            score += 16.0 * math.tanh(accel / 0.8)
        if vol is not None and vol > 0.30:
            score -= self._clamp((vol - 0.30) * 22.0, 0.0, 18.0)
        return self._clamp(score, 0.0, 100.0)

    @staticmethod
    def _state(score: float) -> str:
        if score >= 80:
            return "STRONG"
        if score >= 65:
            return "HEALTHY"
        if score >= 45:
            return "CAUTION"
        if score >= 25:
            return "DEFENSIVE"
        return "CRASH"

    def evaluate(self) -> BTCSnapshot:
        now = time.time()
        with self._lock:
            raw = self._raw_score_locked(now)
            previous = self._score
            if not self.enabled:
                effective = 100.0
            elif previous is None:
                # First trustworthy BTC tick bootstraps the governor immediately.
                effective = raw
            elif raw < previous:
                # De-risk fast.
                effective = raw
            else:
                # Recovery is intentionally slow: no instant return to 100%.
                elapsed = max(0.0, now - self._last_score_change)
                effective = min(raw, previous + elapsed * self.recovery_points_per_sec)
            if previous is None or abs(effective - previous) >= 0.05:
                self._last_score_change = now
            self._raw_score = raw
            self._score = self._clamp(effective, 0.0, 100.0)
            score = self._score
            age = max(0.0, now - self._last_update) if self._last_update else float("inf")
            price = self._prices[-1][1] if self._prices else 0.0
            one = (price / self._minute_open - 1.0) * 100.0 if price and self._minute_open else None
            five = self._return_since(300.0, now)
            fifteen = self._return_since(900.0, now)
            accel = one - five / 5.0 if one is not None and five is not None else None
            vol = self._volatility()
            stale = age > self.stale_after
            state = self._state(score)
            if stale:
                state = "CRASH"
                reason = "بيانات BTC المباشرة غير متاحة؛ مخاطرة الشراء = 0% حتى تعود البيانات."
            elif raw < score:
                reason = "BTC يتحسن، لكن استعادة المخاطرة تدريجية لمنع الرجوع السريع بعد الهبوط."
            elif score >= 80:
                reason = "BTC قوي؛ المحافظ مسموح لها بأقصى مخاطرة جديدة."
            elif score >= 65:
                reason = "BTC صحي؛ المخاطرة الجديدة مرتفعة لكن ليست قصوى."
            elif score >= 45:
                reason = "BTC تحت المراقبة؛ يتم تخفيض أحجام الدخول الجديدة."
            elif score >= 25:
                reason = "BTC دفاعي؛ يتم تقليص الدخول الجديد بقوة."
            else:
                reason = "BTC في منطقة خطر؛ لا يتم فتح مراكز جديدة."
            return BTCSnapshot(score, state, price, one, five, fifteen, accel, vol, age, stale, reason)

    @staticmethod
    def portfolio_risk_level(score: float, stale: bool = False) -> int:
        """Return five execution levels: 0=strong, 4=crash/stale."""
        if stale:
            return 4
        s = float(score)
        if s >= 80.0:
            return 0
        if s >= 65.0:
            return 1
        if s >= 45.0:
            return 2
        if s >= 25.0:
            return 3
        return 4

    @staticmethod
    def exposure_multiplier_for_score(score: float) -> float:
        """Map BTC health to portfolio exposure, not a linear buy fraction."""
        s = float(score)
        if s >= 80.0:
            return 1.00
        if s >= 65.0:
            return 0.80
        if s >= 45.0:
            return 0.60
        if s >= 25.0:
            return 0.40
        return 0.20

    def snapshot(self) -> Dict:
        s = self.evaluate()
        return {
            "enabled": self.enabled,
            "score": round(s.score, 2),
            "raw_score": round(self._raw_score, 2),
            "state": s.state,
            "risk_pct": round(s.score, 1),
            # Compatibility field: now represents the BTC-approved portfolio
            # exposure tier, rather than a raw linear score/100.
            "buy_multiplier": round(
                self.exposure_multiplier_for_score(s.score) if not s.stale else 0.0,
                4,
            ),
            "price": s.price,
            "one_min_pct": s.one_min_pct,
            "five_min_pct": s.five_min_pct,
            "fifteen_min_pct": s.fifteen_min_pct,
            "acceleration_pct": s.acceleration_pct,
            "volatility_pct": s.volatility_pct,
            "age_sec": s.age_sec,
            "stale": s.stale,
            "reason": s.reason,
        }

    def risk_multiplier(self) -> float:
        return self.snapshot()["buy_multiplier"] if self.enabled else 1.0

    def portfolio_exposure_multiplier(self) -> float:
        snap = self.snapshot()
        if not self.enabled:
            return 1.0
        if snap["stale"]:
            return 0.0
        return float(snap["buy_multiplier"])

    def scale_buy_amount(self, requested_usdt: float) -> float:
        requested = max(0.0, float(requested_usdt or 0))
        if requested <= 0:
            return 0.0
        if not self.enabled:
            return requested
        snap = self.snapshot()
        if snap["stale"] or snap["score"] < 1.0:
            raise BTCRiskBlocked("₿ BTC Risk Governor: لا توجد مخاطرة شراء مسموحة الآن.")
        amount = requested * float(snap["buy_multiplier"])
        if amount < self.min_buy_usdt:
            raise BTCRiskBlocked(
                f"₿ BTC Risk Governor: المبلغ بعد تخفيض المخاطرة {amount:.2f}$ أقل من الحد الأدنى {self.min_buy_usdt:.2f}$"
            )
        return amount

    def scale_buy_quantity(self, requested_amount: float) -> float:
        amount = max(0.0, float(requested_amount or 0))
        if amount <= 0 or not self.enabled:
            return amount
        snap = self.snapshot()
        if snap["stale"] or snap["score"] < 1.0:
            raise BTCRiskBlocked("₿ BTC Risk Governor: لا توجد مخاطرة شراء مسموحة الآن.")
        return amount * float(snap["buy_multiplier"])

    def assert_entry_allowed(self, action: str = "شراء"):
        if not self.enabled:
            return
        snap = self.snapshot()
        if snap["stale"] or snap["score"] < 1.0:
            raise BTCRiskBlocked(
                f"₿ BTC Risk Governor منع {action}. المخاطرة الحالية {snap['score']:.0f}/100."
            )

    def status_text(self) -> str:
        s = self.snapshot()
        icons = {"STRONG": "🟢", "HEALTHY": "🟢", "CAUTION": "🟡", "DEFENSIVE": "🟠", "CRASH": "🔴"}
        def pct(v):
            return "—" if v is None else f"{float(v):+.2f}%"
        return (
            "₿ *BTC RISK GOVERNOR*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"المؤشر: *{s['score']:.0f}/100* {icons.get(s['state'], '⚪')} *{s['state']}*\n"
            f"💰 BTC: `{s['price']:.6g}`\n"
            f"⚡ 1m: `{pct(s['one_min_pct'])}` | 5m: `{pct(s['five_min_pct'])}` | 15m: `{pct(s['fifteen_min_pct'])}`\n"
            f"🌪 التقلب: `{pct(s['volatility_pct'])}`\n"
            f"🎯 مخاطرة الدخول الجديدة: *{s['score']:.0f}%*\n\n"
            f"{s['reason']}\n\n"
            "المحافظ لا تُجبر على البيع؛ BTC يحدد فقط حجم المخاطرة الجديدة."
        )

    # ---------- BTC-only websocket ----------
    def start(self):
        if not self.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._ws_loop, name="lotfe-btc-risk", daemon=True)
            self._thread.start()
            logger.info("[BTC-RISK] started")

    def stop(self):
        self._stop.set()

    def _ws_loop(self):
        try:
            import websocket
        except Exception:
            logger.exception("[BTC-RISK] websocket-client is missing")
            return
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(WS_URL, timeout=8, enable_multithread=True)
                ws.send(json.dumps({
                    "method": "SUBSCRIPTION",
                    "params": ["spot@public.kline.v3.api.pb@BTCUSDT@Min1"],
                }))
                last_ping = time.time()
                while not self._stop.is_set():
                    if time.time() - last_ping >= 20:
                        ws.send(json.dumps({"method": "PING"}))
                        last_ping = time.time()
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if raw is None:
                        break
                    self._handle_message(raw)
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception:
                logger.exception("[BTC-RISK] websocket connection failed")
                self._stop.wait(3.0)

    def _handle_message(self, raw):
        data = self._decode_message(raw)
        if not isinstance(data, dict):
            return
        symbol = self._clean_symbol(data.get("symbol") or data.get("s") or "")
        k = data.get("publicspotkline") or data.get("publicSpotKline") or ((data.get("d") or {}).get("k") if isinstance(data.get("d"), dict) else None)
        if symbol != "BTC" or not isinstance(k, dict):
            return
        price = k.get("closingprice") or k.get("closingPrice") or k.get("c")
        op = k.get("openingprice") or k.get("openingPrice") or k.get("o")
        ts = data.get("createtime") or data.get("createTime") or data.get("t")
        try:
            self.update_price("BTC", float(price), float(ts or time.time() * 1000) / 1000.0, float(op))
        except (TypeError, ValueError):
            return

    @staticmethod
    def _decode_message(raw):
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return None
        if isinstance(raw, (bytes, bytearray)):
            try:
                from ccxt.protobuf.mexc import PushDataV3ApiWrapper_pb2
                from google.protobuf.json_format import MessageToDict
                msg = PushDataV3ApiWrapper_pb2.PushDataV3ApiWrapper()
                msg.ParseFromString(bytes(raw))
                return MessageToDict(msg, preserving_proto_field_name=True)
            except Exception:
                try:
                    return json.loads(bytes(raw).decode("utf-8"))
                except Exception:
                    return None
        return None


BTC_RISK = BTCRiskGovernor()


def start_btc_risk_governor():
    BTC_RISK.start()


def stop_btc_risk_governor():
    BTC_RISK.stop()


def btc_risk_snapshot() -> Dict:
    return BTC_RISK.snapshot()


def btc_risk_status_text() -> str:
    return BTC_RISK.status_text()


def assert_entry_allowed(action: str = "شراء"):
    BTC_RISK.assert_entry_allowed(action)


def scale_buy_amount(usdt_amount: float) -> float:
    return BTC_RISK.scale_buy_amount(usdt_amount)


def portfolio_exposure_multiplier() -> float:
    return BTC_RISK.portfolio_exposure_multiplier()


def portfolio_risk_level() -> int:
    snap = BTC_RISK.snapshot()
    return BTC_RISK.portfolio_risk_level(snap.get("score", 0), bool(snap.get("stale")))


def scale_buy_quantity(amount: float) -> float:
    return BTC_RISK.scale_buy_quantity(amount)
