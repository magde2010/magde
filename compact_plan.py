"""Read-only plan formatting. Never changes order sizing or trading settings."""
import math


def position_remaining(coin):
    value = getattr(coin, "remaining_amount", None)
    if value is None:
        value = getattr(coin, "amount", 0)
    value = float(value or 0)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid tracked position quantity")
    return value


def finite_amount(raw, minimum):
    value = float(str(raw).strip().replace(",", "").replace("$", ""))
    if not math.isfinite(value) or value < minimum:
        raise ValueError("Amount must be finite and above the minimum")
    return value


def compact_coin_plan(coin, sell1, sell2, dca_rows, trailing_enabled=False):
    """TP notionals are estimates at target prices, not confirmed proceeds.

    A '?' means the target/order is not confirmed. DCA amounts are remaining
    quote budgets of active records; unknown submissions stay visibly uncertain.
    """
    amount = position_remaining(coin)
    s1 = max(0.0, min(100.0, float(sell1)))
    s2 = max(0.0, min(100.0 - s1, float(sell2)))
    values = []
    for stage, pct in ((1, s1), (2, s2), (3, 100.0 - s1 - s2)):
        price = float(getattr(coin, f"tp{stage}_price", 0) or 0)
        oid = getattr(coin, f"tp{stage}_order_id", None)
        if stage == 3 and price <= 0:
            value = "TRAILING" if trailing_enabled and float(getattr(coin, "current_sl_price", 0) or 0) > 0 else "OFF"
        elif price <= 0 or pct <= 0 or amount <= 0:
            value = "0"
        else:
            value = f"{amount * pct / 100 * price:.2f}" + ("?" if not oid else "")
        values.extend((f"TP{stage}", value))
    symbol = str(coin.symbol).upper()
    tp_line = "/".join((symbol, *values))
    active = {"cloud_waiting", "submitting", "manual_review", "open", "partial", "pending_market"}
    levels = {}
    for row in dca_rows:
        if row.portfolio_coin_id == coin.id and row.status in active and 1 <= int(row.level or 0) <= 3:
            levels.setdefault(int(row.level), []).append(row)
    values = []
    for level in (1, 2, 3):
        rows = levels.get(level, [])
        budget = sum(max(0.0, float(r.planned_usdt or 0) - float(r.filled_cost or 0)) for r in rows)
        uncertain = any(not r.order_id or r.status in ("submitting", "manual_review") for r in rows)
        value = (f"{budget:.2f}" if budget > 0 else "0") + ("?" if uncertain else "")
        values.extend((f"DC{level}", value))
    dca_line = "/".join((symbol, *values))
    sl = float(getattr(coin, "current_sl_price", 0) or 0)
    sl_line = f"{symbol}/SL/{sl:.8g}" if sl > 0 else f"{symbol}/SL/OFF"
    return [tp_line, dca_line, sl_line]
