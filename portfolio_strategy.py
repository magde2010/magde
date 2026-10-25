"""Portfolio entry/DCA allocation rules and price-level helpers."""

from datetime import datetime

VALID_ENTRY_STRATEGIES = {"tp_sl", "tp_sl_dca"}
VALID_DCA_BUY_PCTS = set(range(10, 91))
VALID_DCA_ORDER_COUNTS = {1, 2, 3}


def normalize_entry_strategy(strategy):
    value = str(strategy or "tp_sl").strip().lower()
    return value if value in VALID_ENTRY_STRATEGIES else "tp_sl"


def normalize_dca_buy_pct(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 50
    return number if number in VALID_DCA_BUY_PCTS else 50


def normalize_dca_order_count(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 1
    return number if number in VALID_DCA_ORDER_COUNTS else 1


def calculate_strategy_budgets(investment_usdt, strategy="tp_sl", buy_pct=100, coin_count=1, order_count=1):
    """Return nominal budgets; this helper never places an exchange order."""
    investment = max(0.0, float(investment_usdt or 0.0))
    coins = max(1, int(coin_count or 0))
    strategy = normalize_entry_strategy(strategy)
    if strategy == "tp_sl_dca":
        buy_pct = normalize_dca_buy_pct(buy_pct)
        order_count = normalize_dca_order_count(order_count)
        initial_pct = buy_pct
        dca_pct = 100 - buy_pct
    else:
        initial_pct = 100
        dca_pct = 0
        order_count = 0
    initial_budget = investment * initial_pct / 100.0
    dca_budget = investment * dca_pct / 100.0
    return {
        "initial_pct": initial_pct,
        "dca_pct": dca_pct,
        "initial_budget": initial_budget,
        "dca_budget": dca_budget,
        "initial_per_coin": initial_budget / coins,
        "dca_per_coin": dca_budget / coins,
        "dca_per_order": dca_budget / (coins * order_count) if order_count else 0.0,
        "dca_order_count": order_count,
    }


DEFAULT_DCA_LEVELS = ((50.0, 3.0), (30.0, 6.0), (20.0, 10.0))


def normalize_dca_level_settings(weights, drops):
    """Validate three independent DCA budget weights and drop percentages.

    Weights are percentages of the DCA reserve (not of the full portfolio).
    A total below 100% intentionally leaves some reserve unused.
    """
    clean_weights, clean_drops = [], []
    for index in range(3):
        try:
            weight = float(weights[index])
        except (TypeError, ValueError, IndexError):
            weight = DEFAULT_DCA_LEVELS[index][0]
        try:
            drop = float(drops[index])
        except (TypeError, ValueError, IndexError):
            drop = DEFAULT_DCA_LEVELS[index][1]
        if not 0 <= weight <= 100:
            raise ValueError(f"DC{index + 1} allocation must be between 0 and 100")
        if not 0.1 <= drop <= 90:
            raise ValueError(f"DC{index + 1} drop must be between 0.1 and 90")
        clean_weights.append(weight)
        clean_drops.append(drop)
    if sum(clean_weights) > 100.000001:
        raise ValueError("DCA level allocation weights must not exceed 100% in total")
    if not (clean_drops[0] < clean_drops[1] < clean_drops[2]):
        raise ValueError("DCA drop percentages must increase from DC1 to DC3")
    return clean_weights, clean_drops


def calculate_dca_level_budgets(dca_reserve, coin_count, weights, order_count=3):
    """Budget by DCA rung and coin, with no level exceeding its reserve weight."""
    reserve = max(0.0, float(dca_reserve or 0.0))
    coins = max(1, int(coin_count or 1))
    count = normalize_dca_order_count(order_count)
    clean_weights, _ = normalize_dca_level_settings(weights, [3, 6, 10])
    return [reserve * clean_weights[i] / 100.0 / coins if i < count else 0.0
            for i in range(3)]


def calculate_dca_prices(entry_price, dca_range_pct, order_count):
    """Spread limit buys evenly inside the configured DCA range.

    Levels are evenly spaced below entry within the configured DCA range.
    The caller caps the range at an enabled stop-loss percentage, while a zero
    stop leaves the independent DCA range intact.
    """
    entry = float(entry_price or 0.0)
    range_pct = float(dca_range_pct or 0.0)
    count = normalize_dca_order_count(order_count)
    if entry <= 0 or range_pct <= 0 or range_pct >= 100:
        return []
    return [entry * (1.0 - (range_pct * index / (count + 1)) / 100.0)
            for index in range(1, count + 1)]


def shift_dca_level_prices(prices, step, direction, stop_price=0.0):
    """Move every waiting cloud-DCA trigger by one rung without crossing the stop."""
    step = max(0.0, float(step or 0.0))
    stop = max(0.0, float(stop_price or 0.0))
    sign = 1.0 if str(direction).lower() == "up" else -1.0
    shifted = []
    for raw in prices:
        price = max(0.0, float(raw or 0.0)) + sign * step
        if sign < 0 and stop > 0:
            price = max(price, stop * 1.000001)
        shifted.append(price)
    return shifted



def claim_cloud_dca_submission(db, order_model, order_id, now=None):
    """Atomically claim a cloud DCA row so only one worker submits its market buy."""
    try:
        changed = db.query(order_model).filter(
            order_model.id == order_id,
            order_model.status == "cloud_waiting",
        ).update({
            order_model.status: "submitting",
            order_model.updated_at: now or datetime.now(),
        }, synchronize_session=False)
        db.commit()
        return int(changed or 0) == 1
    except Exception:
        db.rollback()
        raise

def claim_dca_fill_delta(db, order_model, order_id, previous_filled, previous_cost,
                         new_filled, new_cost, now=None):
    """CAS-claim a cumulative fill snapshot inside the caller's transaction.

    No commit is issued here: the caller must commit the fill ledger and the
    corresponding position/trade update atomically.
    """
    from sqlalchemy import func

    changed = db.query(order_model).filter(
        order_model.id == order_id,
        func.coalesce(order_model.filled_amount, 0.0) == float(previous_filled or 0.0),
        func.coalesce(order_model.filled_cost, 0.0) == float(previous_cost or 0.0),
    ).update({
        order_model.filled_amount: max(0.0, float(new_filled or 0.0)),
        order_model.filled_cost: max(0.0, float(new_cost or 0.0)),
        order_model.updated_at: now or datetime.now(),
    }, synchronize_session=False)
    return int(changed or 0) == 1

def spendable_usdt_after_reserves(free_usdt, reserved_usdt):
    """Return free USDT that may be spent without touching tracked reserves."""
    try:
        free = max(0.0, float(free_usdt or 0.0))
        reserved = max(0.0, float(reserved_usdt or 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, free - reserved)

def allocate_dca_order_slots(budget, requested_slots, min_order=2.0):
    """Allocate only as many cloud-DCA triggers as can meet the minimum order.

    Returns a deterministic plan without submitting orders. Any unallocatable
    slots are explicitly reported as skipped instead of creating sub-minimum
    orders or silently overspending the reserve.
    """
    budget = max(0.0, float(budget or 0.0))
    slots = max(0, int(requested_slots or 0))
    minimum = max(0.01, float(min_order or 2.0))
    eligible = min(slots, int((budget + 1e-9) // minimum))
    per_order = budget / eligible if eligible else 0.0
    # Guard floating point rounding at the exchange minimum.
    if eligible and per_order + 1e-9 < minimum:
        eligible -= 1
        per_order = budget / eligible if eligible else 0.0
    return {
        "eligible_slots": eligible,
        "skipped_slots": slots - eligible,
        "per_order_usdt": per_order,
        "budget_used": per_order * eligible,
    }


def match_unidentified_dca_order(candidates, created_at_ms, planned_usdt,
                                 now_ms=None, window_ms=600_000,
                                 tolerance_pct=0.02, exclude_order_ids=None,
                                 expected_price=None):
    """Match a missing-ID submission only when exactly one recent buy fits.

    Ambiguous/no matches return None; callers must keep manual_review and must
    never resubmit an order just because history lookup failed.
    """
    try:
        created = int(created_at_ms)
        planned = float(planned_usdt or 0.0)
    except (TypeError, ValueError):
        return None
    if created <= 0 or planned <= 0:
        return None
    matches = []
    excluded = {str(value) for value in (exclude_order_ids or []) if value not in (None, "")}
    tolerance_pct = max(0.0, float(tolerance_pct))
    try:
        expected_price = float(expected_price or 0.0)
    except (TypeError, ValueError):
        expected_price = 0.0
    # With a known DCA limit price, permit wider quote-notional tolerance for
    # amount precision rounding, while also requiring the order price to match.
    if expected_price > 0:
        tolerance_pct = max(tolerance_pct, 0.08)
    tolerance = max(0.05, planned * tolerance_pct)
    for order in candidates or []:
        if not isinstance(order, dict) or not order.get("id"):
            continue
        if str(order.get("id")) in excluded:
            continue
        if str(order.get("side") or "").lower() != "buy":
            continue
        try:
            timestamp = int(order.get("timestamp") or 0)
            # For an unfilled limit order, exchanges commonly return cost=0 and
            # filled=0. Compare its quote notional (amount * limit price) instead.
            cost = float(order.get("cost") or 0.0)
            order_price = float(order.get("price") or 0.0)
            if cost <= 0:
                filled = float(order.get("filled") or 0.0)
                average = float(order.get("average") or 0.0)
                if filled > 0 and average > 0:
                    cost = filled * average
                else:
                    amount = float(order.get("amount") or 0.0)
                    price = float(order.get("price") or 0.0)
                    cost = amount * price
        except (TypeError, ValueError):
            continue
        if timestamp < created - 5_000 or timestamp > created + int(window_ms):
            continue
        if expected_price > 0:
            if order_price <= 0:
                continue
            # Price matching helps distinguish DC1/DC2/DC3 when budgets are equal.
            if abs(order_price - expected_price) > max(expected_price * 0.005, 1e-10):
                continue
        if cost <= 0 or abs(cost - planned) > tolerance:
            continue
        matches.append(order)
    return matches[0] if len(matches) == 1 else None


# Regression coverage for matching DCA history to an exact ladder price.
