"""
توصيات الحيتان + Flow Engine + تشغيل Spot اختياري.

القرار لم يعد مبنياً على whale radar وحده:
Whale flow → price/volume → CVD → order book → BTC regime → anti-chase.
"""
from __future__ import annotations

import json
import logging
import os

import ui_theme as ui
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from lotfe_flow_engine import get_flow_engine, scan_flow

logger = logging.getLogger(__name__)
STATE_FILE = Path(__file__).resolve().with_name("whale_auto_state.json")


@dataclass
class WhaleAutoSettings:
    mode: str = "paper"  # paper | live
    size_usdt: float = 15.0
    max_positions: int = 3
    min_score: float = 82.0
    take_profit_pct: float = 8.0
    stop_loss_pct: float = 5.0
    scan_interval_seconds: int = 90
    max_exposure_usdt: float = 0.0  # 0 = position_size * max_positions
    confirmations_required: int = 2
    live_confirmations_required: int = 3


@dataclass
class WhalePos:
    symbol: str
    entry: float
    amount: float
    cost: float
    opened_at: float
    mode: str
    score_at_entry: float


@dataclass
class WhaleRuntime:
    active: bool = False
    settings: WhaleAutoSettings = field(default_factory=WhaleAutoSettings)
    paper_cash: float = 100.0
    positions: Dict[str, WhalePos] = field(default_factory=dict)
    closed_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    last_scan: float = 0.0
    telegram_id: Optional[int] = None
    last_recs: List[Dict[str, Any]] = field(default_factory=list)
    confirmations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    cooldowns: Dict[str, float] = field(default_factory=dict)


_WR = WhaleRuntime()


def _save_state() -> None:
    try:
        payload = {
            "settings": vars(_WR.settings),
            "paper_cash": _WR.paper_cash,
            "closed_pnl": _WR.closed_pnl,
            "trades": _WR.trades,
            "wins": _WR.wins,
            "last_recs": _WR.last_recs[-20:],
            "confirmations": _WR.confirmations,
            "cooldowns": _WR.cooldowns,
            "positions": {sym: vars(pos) for sym, pos in _WR.positions.items()},
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("save whale state: %s", exc)


def _load_state() -> None:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        settings = payload.get("settings") or {}
        defaults = WhaleAutoSettings()
        _WR.settings = WhaleAutoSettings(
            mode=str(settings.get("mode", defaults.mode)),
            size_usdt=float(settings.get("size_usdt", defaults.size_usdt)),
            max_positions=int(settings.get("max_positions", defaults.max_positions)),
            min_score=float(settings.get("min_score", defaults.min_score)),
            take_profit_pct=float(settings.get("take_profit_pct", defaults.take_profit_pct)),
            stop_loss_pct=float(settings.get("stop_loss_pct", defaults.stop_loss_pct)),
            scan_interval_seconds=int(settings.get("scan_interval_seconds", defaults.scan_interval_seconds)),
            max_exposure_usdt=float(settings.get("max_exposure_usdt", defaults.max_exposure_usdt)),
            confirmations_required=int(settings.get("confirmations_required", defaults.confirmations_required)),
            live_confirmations_required=int(settings.get("live_confirmations_required", defaults.live_confirmations_required)),
        )
        _WR.paper_cash = float(payload.get("paper_cash", 100.0))
        _WR.closed_pnl = float(payload.get("closed_pnl", 0.0))
        _WR.trades = int(payload.get("trades", 0))
        _WR.wins = int(payload.get("wins", 0))
        _WR.last_recs = list(payload.get("last_recs") or [])
        _WR.confirmations = dict(payload.get("confirmations") or {})
        _WR.cooldowns = {str(k): float(v) for k, v in (payload.get("cooldowns") or {}).items()}
        for sym, raw in (payload.get("positions") or {}).items():
            _WR.positions[sym] = WhalePos(
                symbol=str(raw["symbol"]),
                entry=float(raw["entry"]),
                amount=float(raw["amount"]),
                cost=float(raw["cost"]),
                opened_at=float(raw["opened_at"]),
                mode=str(raw.get("mode", _WR.settings.mode)),
                score_at_entry=float(raw.get("score_at_entry", 0)),
            )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        logger.info("no previous whale state: %s", exc)


_load_state()


def save_whale_state() -> None:
    _save_state()


def get_whale_runtime() -> WhaleRuntime:
    return _WR


def whale_settings_dict() -> Dict[str, Any]:
    return dict(vars(_WR.settings))


def apply_whale_settings(values: Optional[Dict[str, Any]]) -> None:
    if not isinstance(values, dict):
        return
    defaults = WhaleAutoSettings()
    _WR.settings = WhaleAutoSettings(
        mode=str(values.get("mode", defaults.mode)),
        size_usdt=float(values.get("size_usdt", defaults.size_usdt)),
        max_positions=int(values.get("max_positions", defaults.max_positions)),
        min_score=float(values.get("min_score", defaults.min_score)),
        take_profit_pct=float(values.get("take_profit_pct", defaults.take_profit_pct)),
        stop_loss_pct=float(values.get("stop_loss_pct", defaults.stop_loss_pct)),
        scan_interval_seconds=int(values.get("scan_interval_seconds", defaults.scan_interval_seconds)),
        max_exposure_usdt=float(values.get("max_exposure_usdt", defaults.max_exposure_usdt)),
        confirmations_required=int(values.get("confirmations_required", defaults.confirmations_required)),
        live_confirmations_required=int(values.get("live_confirmations_required", defaults.live_confirmations_required)),
    )


def _confirm_signal(symbol: str, score: float, eligible: bool, now: float) -> int:
    """Count confirmations only when enough time has passed between observations."""
    item = _WR.confirmations.get(symbol) or {"count": 0, "last_ts": 0.0, "last_score": 0.0}
    last_ts = float(item.get("last_ts", 0.0))
    age = now - last_ts
    engine_settings = get_flow_engine().settings
    min_gap = max(60, engine_settings.confirmation_minutes * 60)
    max_age = max(min_gap, engine_settings.max_confirmation_age_minutes * 60)

    if not eligible or score < _WR.settings.min_score:
        item = {"count": 0, "last_ts": now, "last_score": score}
        _WR.confirmations[symbol] = item
        return 0

    if age > max_age:
        item["count"] = 1
    elif age >= min_gap:
        if score >= float(item.get("last_score", 0.0)) - 12:
            item["count"] = int(item.get("count", 0)) + 1
        else:
            item["count"] = 1
    else:
        # Same observation window; do not inflate confirmation count from UI refreshes.
        item["count"] = int(item.get("count", 0))

    item["last_ts"] = now if age >= min_gap or last_ts <= 0 else last_ts
    item["last_score"] = score
    _WR.confirmations[symbol] = item
    return int(item["count"])


def _cooldown_active(symbol: str, now: float) -> bool:
    until = float(_WR.cooldowns.get(symbol, 0.0))
    if until and until > now:
        return True
    if until:
        _WR.cooldowns.pop(symbol, None)
    return False


def fetch_recommendations(limit: int = 8) -> List[Dict[str, Any]]:
    """Build ranked early-opportunity signals from the Flow Engine."""
    try:
        rows = scan_flow(limit=max(3, limit))
    except Exception as exc:
        logger.exception("flow scan failed: %s", exc)
        return []

    now = time.time()
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        sym = row.get("symbol")
        if not sym:
            continue
        confirmation_count = _confirm_signal(sym, float(row.get("score", 0)), bool(row.get("eligible")), now)
        row["confirmation_count"] = confirmation_count
        required = _WR.settings.live_confirmations_required if _WR.settings.mode == "live" else _WR.settings.confirmations_required
        row["required_confirmations"] = required
        row["confirmed"] = confirmation_count >= required
        row["can_buy"] = bool(row.get("eligible")) and float(row.get("score", 0)) >= _WR.settings.min_score and row["confirmed"]
        if _cooldown_active(sym, now):
            row["can_buy"] = False
            row.setdefault("block_reasons", []).append("في فترة تهدئة بعد صفقة سابقة")
        enriched.append(row)

    enriched.sort(
        key=lambda x: (
            bool(x.get("can_buy")),
            float(x.get("score", 0)),
            int(x.get("confirmation_count", 0)),
            float(x.get("whale_net_usd", 0)),
        ),
        reverse=True,
    )
    _WR.last_recs = enriched[:20]
    _save_state()
    return enriched[:limit]


def format_reco_message(items: List[Dict[str, Any]], prices_chg: Optional[Dict[str, float]] = None) -> str:
    """Modern, compact Telegram presentation; signal logic is unchanged."""
    prices_chg = prices_chg or {}
    if not items:
        return ui.page_title(ui.I["flow"], "Flow Engine", "لا توجد بيانات كافية الآن.")

    lines = [
        ui.page_title(ui.I["flow"], "Flow Engine", "اصطياد التجميع المبكر — بدون مطاردة الحركة."),
        "",
    ]
    for i, it in enumerate(items, 1):
        sym = it.get("symbol", "?")
        score = float(it.get("score", 0))
        flag = "🔥" if it.get("can_buy") else "🟢" if score >= 75 else "🟡" if score >= 60 else "🔴"
        c1 = float(it.get("change_1h_pct", 0))
        c24 = float(it.get("change_24h_pct", 0))
        conf = int(it.get("confirmation_count", 0))
        req = int(it.get("required_confirmations", 2))
        whale_net = float(it.get("whale_net_usd", 0))
        buy_share = float(it.get("buy_share", 0.5)) * 100
        multiple = float(it.get("whale_multiple", 1.0))
        lines.append(
            f"{flag} *{i}. {sym}*  ·  `{score:.0f}/100`  {it.get('classification','')}\n"
            f"   {ui.I['flow']} Net `{whale_net/1e6:+.2f}M$`  ·  شراء `{buy_share:.0f}%`  ·  نشاط `{multiple:.1f}x`\n"
            f"   📈 1س `{c1:+.1f}%`  ·  24س `{c24:+.1f}%`  ·  تأكيد `{conf}/{req}`"
        )
        reasons = (it.get("reasons") or [])[:3]
        if reasons:
            lines.append("   › " + "  |  ".join(reasons))
        blocks = (it.get("block_reasons") or [])[:2]
        if blocks:
            lines.append("   ⚠ " + "  |  ".join(blocks))
        lines.append("")

    lines.append("_Live لا يشتري إلا بعد اكتمال شروط Flow Engine والتأكيدات._")
    return "\n".join(lines)


def whale_auto_start(telegram_id: int, settings: Optional[WhaleAutoSettings] = None) -> str:
    rt = _WR
    if settings:
        rt.settings = settings
    rt.active = True
    rt.telegram_id = telegram_id
    if not rt.positions:
        rt.closed_pnl = 0.0
        rt.trades = 0
        rt.wins = 0
    if rt.settings.mode == "paper" and rt.paper_cash <= 0:
        rt.paper_cash = float(rt.settings.size_usdt) * max(1, rt.settings.max_positions)
    _save_state()
    s = rt.settings
    mode_ar = "تجريبي 🧪" if s.mode == "paper" else "حقيقي 💰"
    return (
        f"{ui.I['play']} *Flow Engine شغّال* — {mode_ar}\n"
        f"▣ صفقة `{s.size_usdt:g}$`  ·  أقصى `{s.max_positions}`  ·  قوة ≥ `{s.min_score:g}`\n"
        f"✓ تأكيدات `{s.live_confirmations_required if s.mode == 'live' else s.confirmations_required}`\n"
        f"◎ هدف `{s.take_profit_pct:g}%`  ·  وقف `{s.stop_loss_pct:g}%`\n"
        f"◷ فحص كل `{s.scan_interval_seconds}`ث\n"
        + ("ورقي: محاكاة فقط." if s.mode == "paper" else "حقيقي: شراء Spot فقط بعد تأكيد Flow Engine.")
    )


def whale_auto_stop(client=None) -> str:
    rt = _WR
    notes = []
    if rt.settings.mode == "live" and client:
        for sym, pos in list(rt.positions.items()):
            try:
                order = client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                if order:
                    notes.append(sym)
                    rt.positions.pop(sym, None)
                else:
                    logger.warning("live stop could not close %s; position kept in state", sym)
            except Exception as exc:
                logger.warning("whale stop sell %s: %s", sym, exc)
    elif rt.settings.mode == "paper":
        # Paper positions are closed only as bookkeeping; no exchange action is needed.
        rt.positions.clear()
    else:
        # Do not silently erase live positions when no client was supplied.
        logger.warning("live whale stop called without client; positions kept")
    rt.active = False
    _save_state()
    extra = f"\nبيع: {', '.join(notes)}" if notes else ""
    return f"{ui.I['stop']} توقف Flow Engine.{extra}\n{whale_status_text(None)}"


def whale_status_text(client=None) -> str:
    rt = _WR
    s = rt.settings
    mode_ar = "تجريبي 🧪" if s.mode == "paper" else "حقيقي 💰"
    st = "🟢 شغّال" if rt.active else "⚪ متوقف"
    lines = [
        ui.page_title(ui.I["flow"], "Flow Engine", f"{st}  ·  {mode_ar}"),
        f"▣ صفقة `{s.size_usdt:g}$`  ·  أقصى `{s.max_positions}`  ·  قوة ≥ `{s.min_score:g}`",
        f"✓ تأكيدات `{s.live_confirmations_required if s.mode == 'live' else s.confirmations_required}`  ·  {ui.I['clock']} `{s.scan_interval_seconds}`ث",
        f"◎ هدف `{s.take_profit_pct:g}%`  ·  ⛔ وقف `{s.stop_loss_pct:g}%`",
        f"الصفقات `{rt.trades}`  ·  الفائزة `{rt.wins}`  ·  المحقق `{rt.closed_pnl:+.2f}$`",
    ]
    if s.mode == "paper":
        lines.append(f"نقد ورقي: `{rt.paper_cash:.2f}$`")
    if rt.positions:
        lines.append("*تفاصيل المراكز المفتوحة:*")
        total_value = 0.0
        total_pnl = 0.0
        for sym, p in rt.positions.items():
            px = p.entry
            if client:
                try:
                    px = float(client.get_ticker_price(f"{sym}/USDT") or p.entry)
                except Exception:
                    pass
            value = px * p.amount
            pnl = value - p.cost
            pct = pnl / p.cost * 100 if p.cost else 0
            total_value += value
            total_pnl += pnl
            sign = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"• {sign} *{sym}*\n"
                f"  الكمية: `{p.amount:.8g}` | الدخول: `{p.entry:.8g}$`\n"
                f"  الحالي: `{px:.8g}$` | القيمة الحالية: `{value:.2f}$`\n"
                f"  الربح/الخسارة: `{pnl:+.2f}$` (`{pct:+.2f}%`) | score الدخول: `{p.score_at_entry:.0f}`"
            )
        total_pct = total_pnl / (total_value - total_pnl) * 100 if (total_value - total_pnl) else 0
        lines.append(
            f"*إجمالي المراكز:* `{total_value:.2f}$` | "
            f"العائم: `{total_pnl:+.2f}$` (`{total_pct:+.2f}%`)"
        )
    if rt.last_recs:
        lines.append("*آخر Radar:* " + ", ".join(f"`{x['symbol']}`({float(x['score']):.0f})" for x in rt.last_recs[:6]))
    return "\n".join(lines)


def _position_current_price(client, sym: str, fallback: float) -> float:
    try:
        return float(client.get_ticker_price(f"{sym}/USDT") or fallback)
    except Exception:
        return fallback


def _close_position_paper(sym: str, pos: WhalePos, price: float, reason: str) -> str:
    value = price * pos.amount
    pnl = value - pos.cost
    _WR.paper_cash += value
    _WR.closed_pnl += pnl
    _WR.trades += 1
    if pnl > 0:
        _WR.wins += 1
    del _WR.positions[sym]
    _WR.cooldowns[sym] = time.time() + 15 * 60
    return f"{sym}: {reason} | PnL {pnl:+.2f}$ ({(pnl/pos.cost*100 if pos.cost else 0):+.1f}%)"


def whale_auto_tick(client) -> List[str]:
    """Scan -> confirm -> optionally buy. Exits stay TP/SL + strong flow reversal."""
    rt = _WR
    if not rt.active:
        return []
    s = rt.settings
    now = time.time()
    if now - rt.last_scan < max(30, int(s.scan_interval_seconds)):
        return []
    rt.last_scan = now
    msgs: List[str] = []

    # Manage existing positions.
    try:
        recommendations = fetch_recommendations(limit=12)
        rec_map = {r["symbol"]: r for r in recommendations}
    except Exception as exc:
        logger.warning("flow scan in tick failed: %s", exc)
        recommendations = []
        rec_map = {}

    for sym, pos in list(rt.positions.items()):
        px = _position_current_price(client, sym, pos.entry)
        change_pct = (px / pos.entry - 1.0) * 100.0 if pos.entry else 0.0
        row = rec_map.get(sym, {})
        score = float(row.get("score", 50))
        reason = None
        if change_pct >= s.take_profit_pct:
            reason = f"هدف +{change_pct:.1f}%"
        elif change_pct <= -s.stop_loss_pct:
            reason = f"وقف {change_pct:.1f}%"
        elif score <= 38 and (row.get("direction") == "sell" or float(row.get("whale_net_usd", 0)) < 0):
            reason = f"انعكاس تدفق الحيتان — score {score:.0f}"
        if not reason:
            continue

        if s.mode == "paper":
            msgs.append(f"{ui.I['paper']} " + _close_position_paper(sym, pos, px, reason))
        else:
            try:
                order = client.create_market_order(f"{sym}/USDT", "sell", pos.amount)
                if order:
                    amount = float(order.get("filled") or pos.amount)
                    exit_price = float(order.get("average") or px)
                    value = amount * exit_price
                    pnl = value - (pos.cost * (amount / pos.amount if pos.amount else 1.0))
                    rt.closed_pnl += pnl
                    rt.trades += 1
                    if pnl > 0:
                        rt.wins += 1
                    del rt.positions[sym]
                    rt.cooldowns[sym] = now + 15 * 60
                    msgs.append(f"💰 SELL *{sym}* — {reason} | PnL `{pnl:+.2f}$`")
            except Exception as exc:
                logger.error("live sell %s failed: %s", sym, exc)

    # Find new entries.
    if len(rt.positions) >= s.max_positions:
        _save_state()
        return msgs
    open_coins = set(rt.positions)
    exposure = sum(p.cost for p in rt.positions.values())
    max_exposure = s.max_exposure_usdt if s.max_exposure_usdt > 0 else s.size_usdt * s.max_positions
    free = float(getattr(client, "get_free_usdt", lambda: 0.0)() or 0.0) if s.mode == "live" else rt.paper_cash

    for sig in recommendations:
        coin = sig["symbol"]
        if coin in open_coins or _cooldown_active(coin, now):
            continue
        score = float(sig.get("score", 0))
        min_score = max(s.min_score, float(get_flow_engine().settings.min_score_live if s.mode == "live" else get_flow_engine().settings.min_score_buy))
        required = s.live_confirmations_required if s.mode == "live" else s.confirmations_required
        if score < min_score or not sig.get("eligible") or int(sig.get("confirmation_count", 0)) < required:
            continue
        size = min(s.size_usdt, max_exposure - exposure, free)
        if size < 5:
            break

        price = float(sig.get("price", 0) or 0)
        if price <= 0:
            price = _position_current_price(client, coin, 0)
        if price <= 0:
            continue

        if s.mode == "paper":
            amount = size / price
            rt.positions[coin] = WhalePos(coin, price, amount, size, now, "paper", score)
            rt.paper_cash -= size
            exposure += size
            free -= size
            msgs.append(
                f"{ui.I['paper']} *PAPER BUY {coin}* `{size:.2f}$`  ·  score `{score:.0f}`  ·  "
                f"تأكيد `{sig.get('confirmation_count')}/{required}`"
            )
        else:
            try:
                order = client.create_market_buy_usdt(f"{coin}/USDT", size) if hasattr(client, "create_market_buy_usdt") else None
                if not order:
                    logger.warning("live buy skipped; no order for %s", coin)
                    continue
                amount = float(order.get("filled") or order.get("amount") or 0)
                avg = float(order.get("average") or order.get("price") or price)
                cost = float(order.get("cost") or size)
                if amount <= 0 or avg <= 0:
                    continue
                rt.positions[coin] = WhalePos(coin, avg, amount, cost, now, "live", score)
                exposure += cost
                free -= cost
                msgs.append(
                    f"{ui.I['live']} *BUY {coin}* `{cost:.2f}$`  ·  score `{score:.0f}`  ·  "
                    f"تأكيد `{sig.get('confirmation_count')}/{required}`"
                )
            except Exception as exc:
                logger.error("live buy %s failed: %s", coin, exc)
                continue

        open_coins.add(coin)
        if len(rt.positions) >= s.max_positions or exposure >= max_exposure:
            break

    _save_state()
    return msgs
