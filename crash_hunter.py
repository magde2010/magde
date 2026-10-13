"""
اصطياد الانهيار — احترافي (مرحلة 1):
- ميزانية مستقلة + سقف لكل عملة
- سلم مستويين (افتراضي)
- تتبّع الأوامر + بعد الامتلاء: هدف حد + وقف منطقي
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from database import SessionLocal, get_active_crash_orders, upsert_crash_order

logger = logging.getLogger(__name__)



@dataclass
class CrashSettings:
    budget_usdt: float = 50.0
    max_per_coin_usdt: float = 25.0
    levels_pct: List[float] = field(default_factory=lambda: [-10.0, -20.0])
    tp_pct: float = 8.0
    sl_pct: float = 5.0


@dataclass
class TrackedOrder:
    symbol: str
    order_id: str
    limit_price: float
    usdt: float
    pct: float
    placed_at: float
    status: str = "open"
    fill_price: float = 0.0
    amount: float = 0.0
    tp_order_id: Optional[str] = None


_TRACK: Dict[str, TrackedOrder] = {}
_SETTINGS = CrashSettings()
_PERSISTENCE_LOADED = False


def _load_persisted_track():
    global _PERSISTENCE_LOADED
    if _PERSISTENCE_LOADED:
        return
    db = SessionLocal()
    try:
        for row in get_active_crash_orders(db):
            if row.order_id in _TRACK:
                continue
            _TRACK[row.order_id] = TrackedOrder(
                symbol=row.symbol, order_id=row.order_id,
                limit_price=float(row.limit_price or 0), usdt=float(row.usdt or 0),
                pct=float(row.pct or 0), placed_at=float(row.placed_at or 0),
                status=row.status or "open", fill_price=float(row.fill_price or 0),
                amount=float(row.amount or 0), tp_order_id=row.tp_order_id,
            )
        _PERSISTENCE_LOADED = True
    except Exception:
        logger.exception("[CRASH] failed to load persisted orders")
    finally:
        db.close()


def _persist(t: TrackedOrder):
    """Persist crash state; retry and surface failure instead of swallowing it."""
    last_exc = None
    for attempt in range(3):
        db = SessionLocal()
        try:
            upsert_crash_order(
                db, order_id=t.order_id, symbol=t.symbol, limit_price=t.limit_price,
                usdt=t.usdt, pct=t.pct, placed_at=t.placed_at, status=t.status,
                fill_price=t.fill_price, amount=t.amount, tp_order_id=t.tp_order_id,
            )
            return True
        except Exception as exc:
            last_exc = exc
            logger.exception("[CRASH] persistence failed for %s attempt=%s", t.order_id, attempt + 1)
            time.sleep(0.25 * (attempt + 1))
        finally:
            db.close()
    raise RuntimeError(f"Crash Hunter state could not be persisted for {t.order_id}: {last_exc}")


def get_settings() -> CrashSettings:
    return _SETTINGS


def update_settings(**kwargs) -> CrashSettings:
    for k, v in kwargs.items():
        if hasattr(_SETTINGS, k) and v is not None:
            setattr(_SETTINGS, k, v)
    return _SETTINGS


def parse_levels(text: str) -> List[float]:
    raw = text.replace("%", " ").replace("،", ",").replace("\n", ",")
    parts = []
    for p in raw.replace(" ", ",").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            v = float(p)
        except ValueError:
            continue
        if v > 0:
            v = -v
        if v >= 0:
            continue
        if v < -80:
            v = -80.0
        parts.append(v)
    return sorted(set(round(x, 2) for x in parts), reverse=True)


def default_manual_levels() -> List[float]:
    return [-10.0, -20.0]



def analyze_market_for_auto(client):
    """تحليل بسيط من شموع BTC/ETH فقط، بدون بيانات إضافية."""
    changes = []
    for symbol in ("BTC", "ETH"):
        try:
            candles = client.exchange.fetch_ohlcv(f"{symbol}/{client.quote}", timeframe="1h", limit=5) or []
            if len(candles) >= 2 and float(candles[-2][4]) > 0:
                changes.append((float(candles[-1][4]) / float(candles[-2][4]) - 1.0) * 100.0)
        except Exception:
            continue
    avg = sum(changes) / len(changes) if changes else 0.0
    levels = [-10.0, -20.0] if avg >= -2.0 else [-7.0, -15.0]
    analyze_market_for_auto.last_budget_mult = 1.0 if avg >= -2.0 else 0.75
    analyze_market_for_auto.last_regime = "neutral" if avg >= -2.0 else "weak"
    return levels, f"حركة BTC/ETH الأخيرة: {avg:+.2f}% — تم اعتماد سلم {levels[0]:g}% و{levels[1]:g}%"



def place_crash_ladder(
    client,
    symbols: List[str],
    levels_pct: List[float],
    budget_usdt: float,
    mode: str = "manual",
    max_per_coin_usdt: Optional[float] = None,
) -> Dict:
    _load_persisted_track()
    s = _SETTINGS
    max_coin = float(max_per_coin_usdt if max_per_coin_usdt is not None else s.max_per_coin_usdt)
    result = {
        "placed": [],
        "skipped": [],
        "errors": [],
        "cancelled": 0,
        "levels": levels_pct,
        "budget": budget_usdt,
        "max_per_coin": max_coin,
        "mode": mode,
        "tp_pct": s.tp_pct,
        "sl_pct": s.sl_pct,
    }
    if not symbols or not levels_pct or budget_usdt < 5:
        result["errors"].append("لا عملات أو ميزانية/مستويات غير كافية")
        return result

    # حد أقصى مستويان — حتى لو الإعدادات القديمة فيها 3
    levels_pct = list(levels_pct)[:2]
    result["levels"] = levels_pct

    try:
        free = float(client.get_free_usdt() or 0)
    except Exception as e:
        result["errors"].append(f"رصيد USDT: {e}")
        return result
    if free + 0.01 < min(budget_usdt, 10):
        result["errors"].append(f"USDT المتاح `{free:.2f}` أقل من المطلوب")
        return result

    budget_usdt = min(budget_usdt, free * 0.98)
    n_lvl = max(1, len(levels_pct))
    # أقل حجم أمر مقبول على المنصة ~2$ (أفضل من 1$ لتفادي الرفض)
    min_order = 2.5
    # أقصى عدد عملات تتحمّله الميزانية مع سقف لكل عملة
    # per_coin = min(max_coin, budget/n)  و per_order = per_coin/n_lvl >= min_order
    # ⇒ n <= budget / (min_order * n_lvl)  و n <= budget / شيء معقول
    max_by_budget = int(budget_usdt / (min_order * n_lvl))
    max_by_cap = int(budget_usdt / min(max_coin, min_order * n_lvl)) if max_coin > 0 else max_by_budget
    max_coins = max(1, min(len(symbols), max_by_budget, max(1, max_by_cap)))
    if max_coins < len(symbols):
        symbols = symbols[:max_coins]
        result["errors"].append(
            f"ℹ️ الميزانية `{budget_usdt:.0f}$` تكفي ≈ `{max_coins}` عملة فقط "
            f"(بدل الكل). زِد الميزانية لتغطية باقي الرموز."
        )
    n_coins = len(symbols)
    per_coin_cap = min(max_coin, budget_usdt / n_coins)
    per_order = per_coin_cap / n_lvl
    if per_order < min_order:
        result["errors"].append(
            f"حجم الأمر ما زال صغير ({per_order:.2f}$). زِد الميزانية (مقترح ≥ `{n_coins * n_lvl * min_order:.0f}$`)."
        )
        return result
    result["budget"] = budget_usdt
    result["symbols_used"] = n_coins

    for sym in symbols:
        # Replace only Crash Hunter orders already tracked in our DB. Never
        # cancel arbitrary BUY orders on the same MEXC symbol.
        try:
            clr = cancel_crash_orders(client, [sym])
            result["cancelled"] += int(clr.get("cancelled") or 0)
            result["errors"].extend(clr.get("errors") or [])
        except Exception as e:
            result["errors"].append(f"{sym} إلغاء: {e}")

        try:
            px = float(client.get_ticker_price(f"{sym}/USDT") or 0)
        except Exception as e:
            result["errors"].append(f"{sym} سعر: {e}")
            continue
        if px <= 0:
            result["skipped"].append(f"{sym}: لا سعر")
            continue

        for pct in levels_pct:
            limit_px = px * (1.0 + pct / 100.0)
            if limit_px <= 0:
                continue
            amount = per_order / limit_px
            try:
                order = client.create_limit_buy(sym, amount, limit_px)
                if order is None:
                    result["skipped"].append(f"{sym} @{limit_px:.6g} ({pct:g}%): تحت الحد")
                    continue
                oid = str(order.get("id") or "")
                result["placed"].append({
                    "symbol": sym,
                    "pct": pct,
                    "price": limit_px,
                    "usdt": per_order,
                    "order_id": oid,
                })
                if oid:
                    tracked = TrackedOrder(
                        symbol=sym,
                        order_id=oid,
                        limit_price=limit_px,
                        usdt=per_order,
                        pct=pct,
                        placed_at=time.time(),
                        amount=amount,
                    )
                    _TRACK[oid] = tracked
                    _persist(tracked)
                logger.info(
                    "[CRASH] place %s pct=%s price=%s usdt=%.2f id=%s",
                    sym, pct, limit_px, per_order, oid,
                )
            except Exception as e:
                result["errors"].append(f"{sym} {pct:g}%: {e}")

    return result


def cancel_crash_orders(client, symbols: List[str]) -> Dict:
    _load_persisted_track()
    cancelled = 0
    errors = []
    wanted = {str(s).upper() for s in symbols}
    for t in list(_TRACK.values()):
        if t.symbol.upper() not in wanted or t.status != "open":
            continue
        try:
            client.cancel_order(t.order_id, t.symbol, strict=True)
            cancelled += 1
            t.status = "cancelled"
            _persist(t)
        except Exception as e:
            errors.append(f"{t.symbol}/{t.order_id}: {e}")
    return {"cancelled": cancelled, "errors": errors}


def monitor_crash_fills(client) -> List[str]:
    _load_persisted_track()
    msgs: List[str] = []
    s = _SETTINGS
    if not _TRACK:
        return msgs

    open_ids = set()
    symbols = sorted({t.symbol for t in _TRACK.values() if t.status == "open"})
    for sym in symbols:
        try:
            orders = client.fetch_open_orders(sym) or []
            for o in orders:
                if o.get("id"):
                    open_ids.add(str(o["id"]))
        except Exception:
            continue

    for oid, t in list(_TRACK.items()):
        if t.status != "open":
            continue
        if oid in open_ids:
            continue

        # An order disappearing from open-orders is NOT proof of a fill. It can
        # also be cancelled, rejected, expired, or hidden by a temporary API
        # failure. Ask the exchange for the authoritative order state first.
        try:
            order = client.fetch_order(oid, t.symbol)
        except Exception:
            order = None
        if not order:
            logger.warning("[CRASH] cannot verify order state %s/%s; leaving tracked order open", t.symbol, oid)
            continue

        order_status = str(order.get("status") or "").lower()
        if order_status in ("canceled", "cancelled", "rejected", "expired"):
            t.status = "cancelled"
            _persist(t)
            continue
        if order_status not in ("closed", "filled"):
            # Unknown/non-final status: do not guess.
            continue

        try:
            px = float(order.get("average") or order.get("price") or t.limit_price)
            filled_amt = float(order.get("filled") or 0.0)
            if filled_amt <= 0:
                filled_amt = float(order.get("amount") or 0.0)
        except (TypeError, ValueError):
            px = t.limit_price
            filled_amt = 0.0

        if filled_amt <= 0 or px <= 0:
            logger.warning("[CRASH] final order has no usable fill quantity %s/%s", t.symbol, oid)
            continue

        t.status = "filled"
        t.fill_price = px
        t.amount = filled_amt
        _persist(t)
        fill_px = t.fill_price or t.limit_price
        tp_px = fill_px * (1.0 + s.tp_pct / 100.0)
        try:
            sell_amt = t.amount * 0.997
            order = client.create_limit_sell(t.symbol, sell_amt, tp_px)
            if order and order.get("id"):
                t.tp_order_id = str(order["id"])
                _persist(t)
            msgs.append(
                f"🎯 *امتلاء اصطياد* `{t.symbol}`\n"
                f"دخول ~`{fill_px:.6g}` | هدف `{s.tp_pct:g}%` @ `{tp_px:.6g}`\n"
                f"وقف منطقي `{s.sl_pct:g}%` تحت الدخول"
            )
            logger.info("[CRASH] filled %s fill~%s tp=%s", t.symbol, fill_px, tp_px)
        except Exception as e:
            msgs.append(f"⚠️ امتلاء `{t.symbol}` لكن فشل وضع الهدف: {e}")
            logger.exception("crash tp place %s", t.symbol)

    for oid, t in list(_TRACK.items()):
        if t.status != "filled" or t.fill_price <= 0:
            continue
        if t.tp_order_id:
            try:
                tp = client.fetch_order(t.tp_order_id, t.symbol, strict=True)
            except Exception:
                tp = None
            if tp:
                tp_status = str(tp.get("status") or "").lower()
                if tp_status in ("closed", "filled"):
                    t.status = "closed"
                    _persist(t)
                    msgs.append(f"✅ إغلاق هدف Crash Hunter `{t.symbol}` تأكد من المنصة")
                    continue
                if tp_status in ("canceled", "cancelled", "rejected", "expired"):
                    t.tp_order_id = None
                    _persist(t)
        try:
            px = float(client.get_ticker_price(f"{t.symbol}/USDT") or 0)
        except Exception:
            continue
        if px <= 0:
            continue
        sl_px = t.fill_price * (1.0 - s.sl_pct / 100.0)
        if px <= sl_px:
            try:
                if t.tp_order_id:
                    try:
                        client.cancel_order(t.tp_order_id, t.symbol, strict=False)
                    except Exception:
                        pass
                free_amt = float(client.get_free_amount(t.symbol) or 0)
                amt = min(float(t.amount or 0) * 0.997, free_amt * 0.997)
                if amt > 0:
                    client.create_market_order(f"{t.symbol}/USDT", "sell", amt)
                else:
                    raise RuntimeError("Crash Hunter SL has no free quantity for the tracked position")
                pnl_pct = (px / t.fill_price - 1.0) * 100.0
                msgs.append(
                    f"🛡 *وقف اصطياد* `{t.symbol}` @ `{px:.6g}` ({pnl_pct:+.1f}%)"
                )
                t.status = "cancelled"
                _persist(t)
                logger.info("[CRASH] SL %s px=%s", t.symbol, px)
            except Exception as e:
                logger.exception("crash SL %s: %s", t.symbol, e)

    return msgs


def format_result(res: Dict, title: str) -> str:
    lines = [
        f"🎯 *{title}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"الوضع: `{res.get('mode')}`",
        f"المستويات: `{', '.join(str(x) for x in res.get('levels') or [])}` %",
        f"الميزانية: `{res.get('budget', 0):.2f}$` | سقف/عملة: `{res.get('max_per_coin', 0):.2f}$`",
        f"بعد الامتلاء: هدف `{res.get('tp_pct', 8):g}%` | وقف `{res.get('sl_pct', 5):g}%`",
        f"✅ أوامر وُضعت: `{len(res.get('placed') or [])}`",
        f"🗑 مُلغاة قديمة: `{res.get('cancelled', 0)}`",
    ]
    skipped = res.get("skipped") or []
    if skipped:
        lines.append(f"⏭ تخطي: `{len(skipped)}`")
    errs = res.get("errors") or []
    if errs:
        lines.append("⚠️ أخطاء:")
        for e in errs[:8]:
            lines.append(f"• {e}")
    placed = res.get("placed") or []
    if placed:
        lines.append("\n*عينات:*")
        for p in placed[:6]:
            lines.append(
                f"• `{p['symbol']}` {p['pct']:g}% @ `{p['price']:.6g}` (~{p['usdt']:.1f}$)"
            )
        if len(placed) > 6:
            lines.append(f"… و `{len(placed) - 6}` أخرى")
    lines.append("\n_بعد الامتلاء يُوضع هدف حد تلقائيًا ويُراقب الوقف._")
    return "\n".join(lines)


def status_summary() -> str:
    open_n = sum(1 for t in _TRACK.values() if t.status == "open")
    filled_n = sum(1 for t in _TRACK.values() if t.status == "filled")
    s = _SETTINGS
    return (
        f"متتبَّع: مفتوح `{open_n}` | ممتلئ `{filled_n}`\n"
        f"هدف `{s.tp_pct:g}%` | وقف `{s.sl_pct:g}%` | سقف/عملة `{s.max_per_coin_usdt:g}$`"
    )
