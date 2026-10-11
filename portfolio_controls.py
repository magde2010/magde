"""Pure helpers for safe portfolio entry sizing and holding reduction."""

def calculate_portfolio_entry_budget(investment_usdt, entry_pct, total_coin_count, missing_coin_count):
    """Budget the selected percentage only for missing portfolio assets."""
    pct = int(entry_pct)
    if pct not in (25, 50, 70, 100):
        raise ValueError("نسبة الدخول يجب أن تكون 25 أو 50 أو 70 أو 100")
    total_coins = max(1, int(total_coin_count or 0))
    missing = max(0, min(int(missing_coin_count or 0), total_coins))
    per_coin = max(0.0, float(investment_usdt or 0.0)) * pct / 100.0 / total_coins
    return per_coin * missing


def calculate_safe_trim_amount(remaining_amount, pct, free_amount, price, min_notional=2.0):
    """Never sell above the selected fraction; skip orders below the Spot notional floor."""
    if int(pct) not in (25, 50, 70):
        raise ValueError("نسبة التخفيف يجب أن تكون 25 أو 50 أو 70")
    remaining = max(0.0, float(remaining_amount or 0.0))
    free = max(0.0, float(free_amount or 0.0))
    price = max(0.0, float(price or 0.0))
    amount = min(remaining * int(pct) / 100.0, free) * 0.998
    if price <= 0 or amount * price + 1e-9 < float(min_notional):
        return 0.0
    return amount


def calculate_outstanding_trim_amounts(events):
    """Net manually trimmed quantity by (portfolio_coin_id, symbol), subtracting restores."""
    totals = {}
    for event in events or []:
        event_type = str(getattr(event, "event_type", "") or "")
        if event_type not in ("manual_trim", "manual_trim_restore"):
            continue
        amount = max(0.0, float(getattr(event, "amount", 0.0) or 0.0))
        coin_id = getattr(event, "portfolio_coin_id", None)
        symbol = str(getattr(event, "symbol", "") or "").upper()
        key = (int(coin_id) if coin_id is not None else None, symbol)
        sign = 1.0 if event_type == "manual_trim" else -1.0
        totals[key] = totals.get(key, 0.0) + sign * amount
    return {key: amount for key, amount in totals.items() if amount > 1e-10}
