"""تقرير حالة السوق — قراءة فقط من MEXC ورادار الحيتان.

لا ينفذ هذه الوحدة أي شراء أو بيع، ولا تنفذ أوامر تداول.
"""
from __future__ import annotations

from typing import Dict, Iterable, List

import ui_theme as ui


_ACTIVE_STATUSES = {"open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"}


def _change(candles: List, bars: int) -> float:
    if len(candles) <= bars:
        return 0.0
    old = float(candles[-bars - 1][4] or 0)
    last = float(candles[-1][4] or 0)
    return ((last / old) - 1.0) * 100.0 if old else 0.0


def _regime(ch1: float, ch4: float) -> tuple[str, str, str]:
    if ch4 <= -8.0 or (ch1 <= -5.0 and ch4 <= -4.0):
        return "انهيار محتمل", "🔴", "الدخول متوقف — راقب الاستوب اليدوي"
    if ch1 <= -3.0 or ch4 <= -5.0:
        return "سوق ضعيف", "🟠", "حذر شديد — لا ترفع المخاطرة"
    if ch1 >= 1.0 and ch4 >= 2.0:
        return "سوق صاعد", "🟢", "الزخم إيجابي — راقب الأهداف"
    return "سوق متوازن/متذبذب", "🟡", "التزم بالأهداف والاستوب اليدوي"


def _fmt_change(value: float) -> str:
    return f"{value:+.2f}%"


def _market_snapshot(client) -> Dict:
    btc_1h = client.exchange.fetch_ohlcv(f"BTC/{client.quote}", timeframe="1h", limit=30) or []
    btc_4h = client.exchange.fetch_ohlcv(f"BTC/{client.quote}", timeframe="4h", limit=12) or []
    ch1 = _change(btc_1h, 1)
    ch4 = _change(btc_4h, 1)
    day = _change(btc_1h, min(23, max(1, len(btc_1h) - 1)))
    regime, emoji, action = _regime(ch1, ch4)
    last = float(btc_1h[-1][4]) if btc_1h else 0.0
    high = max((float(row[2]) for row in btc_1h), default=last)
    low = min((float(row[3]) for row in btc_1h), default=last)
    volatility = ((high - low) / last * 100.0) if last else 0.0
    return {
        "regime": regime,
        "emoji": emoji,
        "action": action,
        "btc": last,
        "ch1": ch1,
        "ch4": ch4,
        "day": day,
        "volatility": volatility,
    }


def _portfolio_snapshot(client, db, telegram_id: int) -> Dict:
    from database import get_portfolios

    portfolios = get_portfolios(db, telegram_id, status="active") or []
    symbols: List[str] = []
    seen = set()
    active_positions = []
    open_cost = 0.0
    open_value = 0.0
    for portfolio in portfolios:
        for coin in portfolio.coins or []:
            symbol = str(coin.symbol or "").upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
            if (coin.position_status or "") not in _ACTIVE_STATUSES:
                continue
            amount = float(coin.remaining_amount or coin.amount or 0)
            entry = float(coin.entry_price or 0)
            if amount <= 0 or entry <= 0:
                continue
            active_positions.append((portfolio, coin, amount, entry))
            open_cost += amount * entry

    prices = client.get_all_prices(symbols) if symbols else {}
    changes = {}
    for symbol in symbols[:30]:
        try:
            ticker = client.exchange.fetch_ticker(f"{symbol}/{client.quote}") or {}
            changes[symbol] = float(ticker.get("percentage") or 0.0)
        except Exception:
            changes[symbol] = 0.0

    for _, coin, amount, entry in active_positions:
        price = float(prices.get(coin.symbol) or entry)
        open_value += amount * price
    gainers = sum(1 for value in changes.values() if value > 0.5)
    losers = sum(1 for value in changes.values() if value < -0.5)
    flat = max(0, len(changes) - gainers - losers)
    return {
        "portfolios": portfolios,
        "symbols": symbols,
        "positions": active_positions,
        "changes": changes,
        "gainers": gainers,
        "losers": losers,
        "flat": flat,
        "open_cost": open_cost,
        "open_value": open_value,
        "pnl": open_value - open_cost,
    }


def build_market_report(client, db, telegram_id: int) -> str:
    """Return a comprehensive, read-only Arabic market report."""
    try:
        snapshot = _market_snapshot(client)
    except Exception as exc:
        snapshot = {
            "regime": "تعذر القراءة", "emoji": "⚪", "action": "لا تغير إعداداتك قبل إعادة المحاولة",
            "btc": 0.0, "ch1": 0.0, "ch4": 0.0, "day": 0.0, "volatility": 0.0,
            "error": str(exc),
        }

    try:
        portfolio = _portfolio_snapshot(client, db, telegram_id)
    except Exception as exc:
        portfolio = {
            "portfolios": [], "symbols": [], "positions": [], "changes": {},
            "gainers": 0, "losers": 0, "flat": 0,
            "open_cost": 0.0, "open_value": 0.0, "pnl": 0.0,
            "error": str(exc),
        }

    lines = [
        ui.page_title(ui.I["market"], "تقرير حالة السوق", "ملخص للسوق ومحافظك."),
        f"{snapshot['emoji']} *الحالة العامة:* {snapshot['regime']}",
        f"◎ الإجراء المقترح: *{snapshot['action']}*",
        "",
        "*BTC كمؤشر للسوق:*",
        f"◦ السعر: `{snapshot['btc']:.6g}` {client.quote}",
        f"◦ آخر ساعة: `{_fmt_change(snapshot['ch1'])}`  ·  4 ساعات: `{_fmt_change(snapshot['ch4'])}`",
        f"◦ آخر 24 ساعة تقريباً: `{_fmt_change(snapshot['day'])}`",
        f"◦ نطاق الحركة: `{snapshot['volatility']:.2f}%`",
    ]
    if snapshot.get("error"):
        lines.append(f"• تعذر بعض بيانات BTC: `{snapshot['error'][:120]}`")

    lines.extend([
        "",
        "*اتساع حركة عملات محافظك:*",
        f"▲ صاعدة `{portfolio['gainers']}`  ·  ▼ هابطة `{portfolio['losers']}`  ·  • هادئة `{portfolio['flat']}`",
        f"◦ المتابعة: `{len(portfolio['symbols'])}`  ·  المراكز المفتوحة: `{len(portfolio['positions'])}`",
        f"◦ قيمة الدخول: `{portfolio['open_cost']:.2f}` USDT",
        f"◦ القيمة الحالية: `{portfolio['open_value']:.2f}` USDT",
        f"◦ غير المحقق: `{portfolio['pnl']:+.2f}` USDT",
    ])

    changes = sorted(portfolio["changes"].items(), key=lambda item: item[1], reverse=True)
    if changes:
        lines.append("▲ *أقوى الحركة:* " + "  |  ".join(f"`{s}` {_fmt_change(v)}" for s, v in changes[:5]))
        lines.append("▼ *أضعف الحركة:* " + "  |  ".join(f"`{s}` {_fmt_change(v)}" for s, v in changes[-5:][::-1]))

    lines.extend([
        "",
        "*وضع التشغيل الحالي:*",
        "• المحافظ تعمل بالوضع اليدوي فقط (TP/SL).",
        "• لا يوجد خروج دفاعي أو دخول تلقائي من تقرير السوق.",
        "• التقرير للقراءة والتحليل فقط ولا ينفذ أي أمر.",
    ])
    return "\n".join(lines)
