"""
    MEXC Portfolio Manager
    بوت تليجرام لإدارة محافظ متعددة على MEXC Spot بأهداف ووقف خسارة يدويين.
"""
import asyncio
import logging
import math
import time
from datetime import UTC, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import ui_bidi  # إصلاح اتجاه النص العربي (RTL) لكل رسالة
import ui_theme as ui
from compact_plan import compact_coin_plan, finite_amount, position_remaining
from database import (
    Portfolio,
    PortfolioCoin,
    PortfolioDcaOrder,
    PortfolioTrade,
    RebalanceLog,
    SessionLocal,
    acquire_singleton_lock,
    add_coin_to_portfolio,
    claim_reentry_event,
    clear_coin_position,
    close_portfolio,
    create_portfolio,
    delete_orphaned_portfolio_records,
    delete_portfolio_completely,
    get_open_positions,
    get_or_create_user,
    get_portfolio,
    get_portfolio_trade_events,
    get_portfolios,
    get_trade_event,
    init_db,
    log_action,
    mark_reentry_events_used,
    record_trade_event,
    remove_coin_from_portfolio,
    reset_coin_positions,
    reset_portfolio_cycle_state,
    set_portfolio_running,
    update_coin_position,
)
from mexc_client import MexcClient
from order_safety import classify_dca_submission, classify_market_sell_submission
from rebalancer import Rebalancer
from trading_guard import trading_guard

ui_bidi.activate()
from portfolio_controls import (
    calculate_outstanding_trim_amounts,
    calculate_portfolio_entry_budget,
    calculate_safe_trim_amount,
)
from portfolio_strategy import (
    calculate_dca_level_budgets,
    calculate_strategy_budgets,
    claim_cloud_dca_submission,
    claim_dca_fill_delta,
    match_unidentified_dca_order,
    normalize_dca_buy_pct,
    normalize_dca_level_settings,
    normalize_dca_order_count,
    normalize_entry_strategy,
    shift_dca_level_prices,
    spendable_usdt_after_reserves,
)

_SL_FAILURE_ALERTS = {}
_MONITOR_HEALTH = {
    "last_finished_at": None,
    "last_started_at": None,
    "last_duration": 0.0,
    "last_positions": 0,
    "last_actions": 0,
    "last_error": None,
}


def _split_telegram_text(text: str, limit: int = 3800):
    """Split a report by lines below Telegram's message limit."""
    chunks = []
    current = []
    size = 0
    for line in str(text).splitlines():
        line_size = len(line) + 1
        if current and size + line_size > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        # A single unusually long error line must still be sent safely.
        while len(line) + 1 > limit:
            chunks.append(line[: limit - 1])
            line = line[limit - 1 :]
        if line or not current:
            current.append(line)
            size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _format_monitor_health(p) -> str:
    """Readable health report for exchange TP/DCA reconciliation and bot SL monitor."""
    now = time.time()
    finished = _MONITOR_HEALTH.get("last_finished_at")
    if not finished:
        cycle = "⚪ لم تبدأ أي دورة مراقبة بعد"
    else:
        age = max(0, int(now - finished))
        if _MONITOR_HEALTH.get("last_error"):
            cycle = f"🔴 آخر دورة بها خطأ منذ {age} ثانية"
        elif age <= max(15, config.STOP_MONITOR_INTERVAL_SECONDS * 3):
            cycle = f"🟢 آخر دورة ناجحة منذ {age} ثانية"
        else:
            cycle = f"🟡 آخر دورة قديمة منذ {age} ثانية"
    lines = [
        "🔄 *حالة مزامنة أوامر المنصة والاستوب*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{cycle}",
        f"⏱ مدة آخر دورة: `{_MONITOR_HEALTH.get('last_duration', 0):.2f}` ثانية",
        f"📌 المراكز المفحوصة: `{_MONITOR_HEALTH.get('last_positions', 0)}`",
        f"🎯 الإجراءات في آخر دورة: `{_MONITOR_HEALTH.get('last_actions', 0)}`",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    error = _MONITOR_HEALTH.get("last_error")
    lines.append(f"⚠️ الخطأ الأخير: `{str(error)[:160]}`" if error else "✅ لا يوجد خطأ مسجل")
    lines.extend(["", f"📋 *أهداف واستوب محفظة {p.name}*"])
    for coin in p.coins or []:
        status = str(coin.position_status or "idle")
        tp = []
        for label, price in (("TP1", coin.tp1_price), ("TP2", coin.tp2_price), ("TP3", coin.tp3_price)):
            if float(price or 0) > 0:
                tp.append(f"{label} `{float(price):.8g}`")
        sl = float(coin.current_sl_price or coin.original_sl_price or 0)
        lines.append(f"*{coin.symbol.upper()}* — الحالة: `{status}`")
        lines.append(f"🎯 {' | '.join(tp) if tp else 'لا توجد أهداف'}")
        if bool(getattr(coin, "tp_refresh_pending", False)):
            lines.append("⚠️ تجديد أوامر TP بعد DCA قيد التسوية؛ لن يضيف البوت أوامر بيع مكررة.")
        lines.append(f"🛡 الاستوب الحالي: `{sl:.8g}`" if sl > 0 else "🛡 الاستوب: غير مضبوط")
        lines.append("────────────")
    lines.append("🔄 اضغط تحديث للحصول على قراءة جديدة من قاعدة البيانات.")
    return "\n".join(lines)


def _redact_secrets(value: str) -> str:
    """Redact credentials from log messages and tracebacks."""
    import re

    text = str(value)
    for secret in (
        config.TELEGRAM_BOT_TOKEN,
        config.MEXC_API_KEY,
        config.MEXC_API_SECRET,
        config.DATABASE_URL,
    ):
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(https?://api\.telegram\.org/bot)[^/\s]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(\b(?:token|api[_-]?key|api[_-]?secret|password|passwd)=)[^&\s]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"(\bbot\d+):[A-Za-z0-9_-]+", r"\1:[REDACTED]", text)


class _SecretRedactingFormatter(logging.Formatter):
    def format(self, record):
        return _redact_secrets(super().format(record))


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# The Telegram URL logged by httpx contains the bot token. Redact every
# handler's final output and suppress routine request URLs at INFO level.
for _handler in logging.getLogger().handlers:
    _formatter = _handler.formatter
    _format = getattr(getattr(_formatter, "_style", None), "_fmt", None) or "%(message)s"
    _handler.setFormatter(_SecretRedactingFormatter(_format, datefmt=getattr(_formatter, "datefmt", None)))
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

(
    CREATE_NAME, CREATE_AMOUNT, CREATE_COINS, ADD_COIN, INCREASE_AMOUNT,
) = range(5)

_mexc: MexcClient | None = None
_reb: Rebalancer | None = None

# MEXC Spot minimum order notional requested for portfolio buys/sells.
PORTFOLIO_MIN_ORDER_USDT = 2.0
_ACTIVE_PORTFOLIO_TRIMS = set()
_ACTIVE_PORTFOLIO_REBUILDS = set()
_DCA_ACTIVE_STATUSES = ("open", "partial", "manual_review", "cloud_waiting", "submitting", "pending_market")
_DCA_TERMINAL_STATUSES = {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}


def _utc_datetime_ms(value):
    """Convert DB timestamps to epoch milliseconds without local-time ambiguity.

    DB datetime columns are written with datetime.utcnow() (naive UTC). Calling
    timestamp() directly on such values interprets them as the host's local
    timezone and can shift MEXC history queries by hours on non-UTC hosts.
    """
    if not value:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return int(value.timestamp() * 1000)


def _fetch_dca_order_candidates(exchange, pair, since_ms):
    """Collect order history from all useful CCXT views, deduplicating by ID.

    MEXC/CCXT may expose active orders and historical orders through separate
    endpoints. A failed endpoint must not prevent attempts against the others.
    """
    by_id = {}
    methods_tried = []
    methods_succeeded = []
    query_since = max(0, int(since_ms) - 10 * 60 * 1000)
    for method_name in ("fetch_orders", "fetch_open_orders", "fetch_closed_orders", "fetch_canceled_orders"):
        method = getattr(exchange, method_name, None) if exchange is not None else None
        if not callable(method):
            continue
        methods_tried.append(method_name)
        try:
            try:
                rows = method(pair, query_since, 100)
            except TypeError:
                rows = method(pair)
            if not isinstance(rows, (list, tuple)):
                continue
            methods_succeeded.append(method_name)
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                oid = str(row["id"])
                if oid in by_id:
                    merged = dict(by_id[oid])
                    for key, value in row.items():
                        if value in (None, ""):
                            continue
                        if key not in merged or merged[key] in (None, "", 0, 0.0):
                            merged[key] = value
                        elif value not in (0, 0.0):
                            # Prefer non-empty metadata from the latest source.
                            merged[key] = value
                    by_id[oid] = merged
                else:
                    by_id[oid] = dict(row)
        except Exception as exc:
            logger.warning("DCA history source %s failed for %s: %s", method_name, pair, exc)
    logger.info(
        "DCA reconciliation history pair=%s methods_tried=%s methods_ok=%s candidates=%s since_ms=%s",
        pair, methods_tried, methods_succeeded, len(by_id), query_since,
    )
    return list(by_id.values())


def portfolio_strategy_label(portfolio):
    strategy = normalize_entry_strategy(getattr(portfolio, "entry_strategy", "tp_sl"))
    if strategy == "tp_sl_dca":
        pct = normalize_dca_buy_pct(getattr(portfolio, "dca_buy_pct", 50))
        count = normalize_dca_order_count(getattr(portfolio, "dca_order_count", 1))
        return f"أهداف ووقف + DCA ({pct}% شراء / {100-pct}% DCA، {count} طلب لكل عملة)"
    return "أهداف ووقف فقط (100% من المخصص)"



def _resolve_optional_pct(portfolio_value, user_value, default):
    """Resolve a percentage where explicit zero is meaningful (e.g. TP3 off)."""
    for value in (portfolio_value, user_value):
        if value is None:
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return float(default)

def _resolve_stop_loss_pct(portfolio_value, user_value, default=3.0):
    """Resolve stop loss while preserving 0 as an explicit disable signal."""
    return _resolve_optional_pct(portfolio_value, user_value, default)

def _resolved_portfolio_tpsl(db, portfolio):
    user = get_or_create_user(db, portfolio.telegram_id)
    def resolve(portfolio_value, user_value, default):
        for value in (portfolio_value, user_value):
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return float(default)
    def resolve_optional_stop(portfolio_value, user_value, default=3.0):
        # Explicit zero is a deliberate stop-loss disable, not a missing value.
        for value in (portfolio_value, user_value):
            if value is None:
                continue
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        return float(default)
    def resolve_optional_target(portfolio_value, user_value, default):
        for value in (portfolio_value, user_value):
            if value is None:
                continue
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        return float(default)
    return (
        resolve_optional_target(portfolio.tp1_pct, user.tp1_pct, 3.0),
        resolve_optional_target(portfolio.tp2_pct, user.tp2_pct, 5.0),
        resolve_optional_target(portfolio.tp3_pct, user.tp3_pct, 8.0),
        resolve_optional_target(portfolio.tp1_sell_pct, user.tp1_sell_pct, 40.0),
        resolve_optional_target(portfolio.tp2_sell_pct, user.tp2_sell_pct, 40.0),
        resolve_optional_stop(portfolio.stop_loss_pct, user.stop_loss_pct, 3.0),
    )


def _persist_unresolved_market_buys(db, portfolio, pending_items):
    """Persist ambiguous initial market buys so a restart/click cannot forget them."""
    pending_symbols = set()
    for item in pending_items or []:
        raw_symbol = str(item.get("symbol") or "").split("/")[0].upper().strip()
        if not raw_symbol:
            continue
        coin = db.query(PortfolioCoin).filter(
            PortfolioCoin.portfolio_id == portfolio.id,
            PortfolioCoin.symbol == raw_symbol,
        ).first()
        if not coin:
            continue
        pending_symbols.add(raw_symbol)
        existing = db.query(PortfolioDcaOrder).filter(
            PortfolioDcaOrder.portfolio_id == portfolio.id,
            PortfolioDcaOrder.portfolio_coin_id == coin.id,
            PortfolioDcaOrder.level == 0,
            PortfolioDcaOrder.status.in_(tuple(_DCA_ACTIVE_STATUSES)),
        ).first()
        if existing:
            continue
        order_id = str(item.get("order_id") or "").strip() or None
        row = PortfolioDcaOrder(
            portfolio_id=portfolio.id,
            portfolio_coin_id=coin.id,
            symbol=raw_symbol,
            order_id=order_id,
            level=0,  # initial buy, not a DCA rung
            limit_price=0.0,
            planned_usdt=max(0.0, float(item.get("requested_usdt") or 0.0)),
            status="manual_review",
        )
        db.add(row)
    db.commit()
    return pending_symbols


def _refresh_tp_after_dca_fill(db, coin_id, reb=None):
    """Safely replace TP orders after a committed DCA position update.

    The position/DCA fill is committed *before* this function talks to MEXC.
    If the process dies in the small interval after creating an exchange order
    but before saving its ID, the next attempt sees it as an untracked open
    sell and stops for reconciliation instead of placing a duplicate TP order.
    """
    coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == coin_id).first()
    if not coin or not bool(getattr(coin, "tp_refresh_pending", False)):
        return True
    portfolio = db.query(Portfolio).filter(Portfolio.id == coin.portfolio_id).first()
    if not portfolio:
        logger.error("DCA TP refresh has no portfolio for coin_id=%s", coin_id)
        return False

    rebalancer = reb or get_reb()
    client = rebalancer.client
    pair = f"{coin.symbol}/{getattr(client, 'quote', 'USDT')}"
    order_keys = ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id")
    known_ids = {
        str(getattr(coin, key))
        for key in order_keys
        if getattr(coin, key, None)
    }
    terminal = {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}

    # This check is deliberately first. It is the restart safety barrier: a
    # TP created by a previous interrupted attempt has no DB ID, and must never
    # be followed by another automatic sell order for the same balance.
    try:
        open_sells = client.fetch_open_sell_orders(coin.symbol)
    except Exception as exc:
        logger.warning("DCA TP refresh could not read open sells for %s: %s", coin.symbol, exc)
        return False
    untracked = [
        order for order in open_sells
        if str(order.get("id") or "") not in known_ids
    ]
    if untracked:
        ids = ", ".join(str(order.get("id") or "?") for order in untracked[:5])
        logger.error(
            "DCA TP refresh stopped for %s: untracked open sell order(s) %s; manual reconciliation required",
            coin.symbol,
            ids,
        )
        coin._tp_refresh_deferred = True
        return False

    # Cancel only known TP orders and refuse to replace them if one filled while
    # cancellation was in flight. The regular TP monitor will book that fill,
    # clear its ID, then a later cycle can safely refresh the remaining position.
    for key in order_keys:
        order_id = getattr(coin, key, None)
        if not order_id:
            continue
        try:
            state = client.fetch_order(str(order_id), pair, strict=True)
            if not state:
                raise RuntimeError("exchange returned no order state")
            state_status = str(state.get("status") or "").lower()
            if state_status not in terminal:
                client.cancel_order(str(order_id), pair, strict=True)
                state = client.fetch_order(str(order_id), pair, strict=True)
                state_status = str((state or {}).get("status") or "").lower()
            if state_status not in terminal:
                raise RuntimeError(f"order remains non-terminal ({state_status or 'unknown'})")
            if float((state or {}).get("filled") or 0.0) > 0:
                logger.info(
                    "DCA TP refresh deferred for %s because TP %s filled during cancellation",
                    coin.symbol,
                    order_id,
                )
                coin._tp_refresh_deferred = True
                return False
        except Exception as exc:
            logger.warning("DCA TP refresh could not settle %s %s: %s", coin.symbol, order_id, exc)
            coin._tp_refresh_deferred = True
            return False

    # Persist the completed cancellation before placing replacements. A crash
    # here is safe: the next cycle finds no open/untracked sell and retries.
    for key in order_keys:
        setattr(coin, key, None)
    db.commit()

    try:
        if client.fetch_open_sell_orders(coin.symbol):
            logger.error(
                "DCA TP refresh stopped for %s: a sell order appeared after cancellation",
                coin.symbol,
            )
            coin._tp_refresh_deferred = True
            return False
    except Exception as exc:
        logger.warning("DCA TP refresh could not verify post-cancel sells for %s: %s", coin.symbol, exc)
        coin._tp_refresh_deferred = True
        return False

    tp1, tp2, tp3, s1, s2, sl_pct = _resolved_portfolio_tpsl(db, portfolio)
    status = str(coin.position_status or "open")
    skip_stages = []
    if status in ("tp1_hit", "tp2_hit", "tp3_hit"):
        skip_stages.append("tp1")
    if status in ("tp2_hit", "tp3_hit"):
        skip_stages.append("tp2")
    if status == "tp3_hit":
        skip_stages.append("tp3")

    try:
        targets = rebalancer.place_tp_orders(
            [{
                "symbol": coin.symbol,
                "amount": max(0.0, float(coin.remaining_amount or 0.0)),
                "entry_price": max(0.0, float(coin.entry_price or 0.0)),
            }],
            tp1, tp2, tp3, sl_pct, s1, s2,
            skip_stages=skip_stages,
            control_mode="manual",
        )[0]
    except Exception as exc:
        logger.exception("DCA TP replacement failed for %s: %s", coin.symbol, exc)
        coin._tp_refresh_deferred = True
        return False

    coin.tp1_price = float(targets.get("tp1_price") or 0.0)
    coin.tp2_price = float(targets.get("tp2_price") or 0.0)
    coin.tp3_price = float(targets.get("tp3_price") or 0.0)
    coin.tp_price = coin.tp2_price if status == "tp1_hit" else coin.tp1_price
    coin.tp_order_id = targets.get("tp_order_id")
    coin.tp1_order_id = targets.get("tp1_order_id")
    coin.tp2_order_id = targets.get("tp2_order_id")
    coin.tp3_order_id = targets.get("tp3_order_id")
    # The response is now durably associated with the exchange orders. A
    # per-stage error (for example, dust below an exchange minimum) is visible
    # in logs but is not retried in a tight loop.
    coin.tp_refresh_pending = False
    coin._tp_refresh_deferred = bool(targets.get("error"))
    db.commit()
    if targets.get("error"):
        logger.warning("DCA TP replacement completed with warnings for %s: %s", coin.symbol, targets["error"])
    return not bool(targets.get("error"))


def _recover_pending_tp_refreshes(db):
    """Resume durable DCA TP refresh checkpoints after a restart/failure."""
    coins = db.query(PortfolioCoin).join(Portfolio).filter(
        Portfolio.status == "active",
        Portfolio.is_running == True,
        PortfolioCoin.tp_refresh_pending == True,
    ).all()
    for coin in coins:
        try:
            _refresh_tp_after_dca_fill(db, coin.id)
        except Exception:
            logger.exception("DCA TP refresh recovery crashed for coin_id=%s", coin.id)


def _apply_dca_order_snapshot(db, dca_order, snapshot, reb=None):
    """Persist a DCA fill before replacing exchange TP orders.

    The order fill, position basis and stop are one database transaction. TP
    replacement happens only after that transaction commits and is guarded by
    ``PortfolioCoin.tp_refresh_pending`` so a restart cannot duplicate sells.
    """
    if not snapshot:
        return 0.0
    status = str(snapshot.get("status") or "").lower()
    filled_raw = snapshot.get("filled")
    if filled_raw is None:
        # Without a confirmed fill quantity, preserve the order for reconciliation.
        if status in _DCA_TERMINAL_STATUSES:
            dca_order.status = "manual_review"
            db.commit()
        return 0.0
    filled = max(0.0, float(filled_raw or 0.0))
    cost_raw = snapshot.get("cost")
    average = float(snapshot.get("average") or snapshot.get("price") or dca_order.limit_price or 0.0)
    total_cost = max(0.0, float(cost_raw or 0.0))
    if total_cost <= 0 and filled > 0 and average > 0:
        total_cost = filled * average
    old_filled = max(0.0, float(dca_order.filled_amount or 0.0))
    old_cost = max(0.0, float(dca_order.filled_cost or 0.0))
    delta_amount = max(0.0, filled - old_filled)
    delta_cost = max(0.0, total_cost - old_cost)
    if delta_amount > 0 and (delta_cost <= 0 or delta_cost > delta_amount * max(average, dca_order.limit_price, 1.0) * 1.5):
        delta_cost = delta_amount * average
    delta_price = (delta_cost / delta_amount) if delta_amount > 0 and delta_cost > 0 else average

    if delta_amount > 0 and delta_price <= 0:
        # Never advance the cumulative snapshot if its price is unknown: doing
        # so would make a real fill disappear from the position ledger.
        dca_order.status = "manual_review"
        dca_order.updated_at = datetime.utcnow()
        db.commit()
        return 0.0

    if delta_amount > 0:
        coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == dca_order.portfolio_coin_id).first()
        portfolio = db.query(Portfolio).filter(Portfolio.id == dca_order.portfolio_id).first()
        if not coin or not portfolio:
            dca_order.status = "manual_review"
            db.commit()
            return 0.0

        # Multiple monitor workers may read the same cumulative fill. Claim the
        # delta with compare-and-set before changing position totals/trade ledger.
        if not claim_dca_fill_delta(
            db, PortfolioDcaOrder, dca_order.id, old_filled, old_cost,
            max(old_filled, filled), max(old_cost, total_cost), datetime.utcnow(),
        ):
            db.rollback()
            db.refresh(dca_order)
            return 0.0

        previous_remaining = max(0.0, float(coin.remaining_amount or 0.0))
        previous_total_amount = max(0.0, float(coin.amount or 0.0))
        previous_entry = max(0.0, float(coin.entry_price or 0.0))
        previous_status = str(coin.position_status or "idle")
        # Compatibility for old open-position rows that predate remaining_amount.
        if previous_remaining <= 1e-12 and previous_status not in ("idle", "closed", "waiting_reentry"):
            previous_remaining = previous_total_amount
        was_idle = previous_remaining <= 1e-12 or previous_status in ("idle", "closed", "waiting_reentry")
        combined_remaining = previous_remaining + delta_amount
        combined_entry = (
            (previous_remaining * previous_entry + delta_amount * delta_price) / combined_remaining
            if combined_remaining > 0 else delta_price
        )
        combined_total_amount = (
            delta_amount if was_idle
            else max(previous_total_amount, previous_remaining) + delta_amount
        )
        _, _, _, _, _, sl_pct = _resolved_portfolio_tpsl(db, portfolio)
        next_status = "open" if was_idle else coin.position_status
        calculated_sl = combined_entry * (1.0 - sl_pct / 100.0) if sl_pct > 0 else 0.0
        old_sl = max(0.0, float(coin.current_sl_price or 0.0))

        # Commit the factual fill and its protected quantity before issuing any
        # external cancel/create request. `tp_refresh_pending` is the durable
        # recovery checkpoint for every crash window that follows.
        coin.entry_price = combined_entry
        coin.amount = combined_total_amount
        coin.remaining_amount = combined_remaining
        coin.original_sl_price = calculated_sl
        coin.current_sl_price = (
            0.0 if float(sl_pct or 0.0) <= 0
            else (max(old_sl, calculated_sl) if not was_idle else calculated_sl)
        )
        coin.position_status = next_status
        coin.tp_refresh_pending = True
        db.add(PortfolioTrade(
            telegram_id=portfolio.telegram_id,
            portfolio_id=portfolio.id,
            portfolio_coin_id=coin.id,
            symbol=coin.symbol,
            event_type="dca_buy" if int(dca_order.level or 0) > 0 else "initial_buy",
            entry_price=combined_entry,
            exit_price=delta_price,
            amount=delta_amount,
            realized_pnl=0.0,
            reentry_available=False,
            reentry_used=False,
            details=(
                f"DCA limit buy filled; order={dca_order.order_id}; spent≈{delta_cost:.4f} USDT"
                if int(dca_order.level or 0) > 0
                else f"Initial market buy reconciled; order={dca_order.order_id}; spent≈{delta_cost:.4f} USDT"
            ),
        ))

    dca_order.filled_amount = max(old_filled, filled)
    dca_order.filled_cost = max(old_cost, total_cost)
    if status in _DCA_TERMINAL_STATUSES:
        dca_order.status = "filled" if filled > 0 and status in ("closed", "filled") else "cancelled"
    elif filled > 0:
        dca_order.status = "partial"
    else:
        dca_order.status = "open"
    dca_order.updated_at = datetime.utcnow()
    db.commit()

    if delta_amount > 0:
        _refresh_tp_after_dca_fill(db, dca_order.portfolio_coin_id, reb=reb)
    return delta_amount

async def _cancel_dca_orders(db, portfolio, client=None, symbol=None, notify=None):
    """Cancel this portfolio's tracked DCA buys and verify terminal exchange state."""
    client = client or get_mexc()
    loop = asyncio.get_event_loop()
    query = db.query(PortfolioDcaOrder).filter(
        PortfolioDcaOrder.portfolio_id == portfolio.id,
        PortfolioDcaOrder.status.in_(tuple(_DCA_ACTIVE_STATUSES)),
    )
    if symbol:
        query = query.filter(PortfolioDcaOrder.symbol == str(symbol).upper())
    orders = query.order_by(PortfolioDcaOrder.id.asc()).all()
    errors = []
    for dca in orders:
        if dca.status == "cloud_waiting":
            # Cloud triggers have no exchange order until triggered; cancel locally.
            dca.status = "cancelled"
            dca.updated_at = datetime.utcnow()
            db.commit()
            continue
        pair = f"{dca.symbol}/{client.quote}"
        if not dca.order_id:
            # Older versions could persist submitting/manual_review rows without
            # an exchange ID. Before blocking rebalance, try to bind a single
            # matching exchange-history record. Never resubmit or guess an ID.
            exchange = getattr(client, "exchange", None)
            matched = None
            if exchange is not None and any(callable(getattr(exchange, name, None)) for name in ("fetch_orders", "fetch_open_orders", "fetch_closed_orders", "fetch_canceled_orders")):
                try:
                    # Use the immutable row creation time here. `updated_at` changes
                    # whenever we mark manual_review, which previously moved the search
                    # window forward on every retry and made old orders impossible to find.
                    attempted_at = dca.created_at or dca.updated_at or datetime.utcnow()
                    created_ms = _utc_datetime_ms(attempted_at)
                    history = await loop.run_in_executor(
                        None, lambda: _fetch_dca_order_candidates(exchange, pair, created_ms)
                    )
                    already_linked = {
                        str(value[0]) for value in db.query(PortfolioDcaOrder.order_id).filter(
                            PortfolioDcaOrder.portfolio_id == portfolio.id,
                            PortfolioDcaOrder.symbol == dca.symbol,
                            PortfolioDcaOrder.order_id.isnot(None),
                        ).all() if value[0]
                    }
                    matched = match_unidentified_dca_order(
                        history, created_ms, float(dca.planned_usdt or 0.0),
                        expected_price=float(dca.limit_price or 0.0),
                        exclude_order_ids=already_linked,
                    )
                except Exception:
                    logger.exception("DCA cancel history reconciliation failed row=%s", dca.id)
            if matched:
                dca.order_id = str(matched["id"])
                dca.updated_at = datetime.utcnow()
                db.commit()
            else:
                dca.status = "manual_review"
                db.commit()
                errors.append(
                    f"{dca.symbol}: تعذر ربط سجل DCA بلا رقم أمر بسجل وحيد في تاريخ MEXC؛ "
                    "لم تُرسل أوامر جديدة ويلزم التحقق اليدوي"
                )
                continue
        try:
            state = await loop.run_in_executor(None, lambda oid=dca.order_id, pr=pair: client.fetch_order(oid, pr, strict=True))
            if not state:
                raise RuntimeError("لم تُرجع المنصة حالة الأمر")
            _apply_dca_order_snapshot(db, dca, state)
            status = str(state.get("status") or "").lower()
            if status not in _DCA_TERMINAL_STATUSES:
                await loop.run_in_executor(None, lambda oid=dca.order_id, pr=pair: client.cancel_order(oid, pr, strict=True))
                for _ in range(5):
                    await asyncio.sleep(0.35)
                    state = await loop.run_in_executor(None, lambda oid=dca.order_id, pr=pair: client.fetch_order(oid, pr, strict=True))
                    if state:
                        _apply_dca_order_snapshot(db, dca, state)
                        status = str(state.get("status") or "").lower()
                        if status in _DCA_TERMINAL_STATUSES:
                            break
                if status not in _DCA_TERMINAL_STATUSES:
                    raise RuntimeError(f"الأمر ما زال غير محسوم ({status or 'unknown'})")
            # A filled DCA order is reconciled above; terminal orders no longer reserve USDT.
        except Exception as exc:
            logger.exception("DCA cancel/reconcile failed portfolio=%s order=%s", portfolio.id, dca.order_id)
            errors.append(f"{dca.symbol} ({dca.order_id}): {str(exc)[:100]}")
    if errors and notify:
        await notify(errors)
    return errors


def _cloud_dca_reserved_usdt(db, exclude_id=None):
    """USDT notionally reserved by active cloud DCA triggers, including ambiguity."""
    reserve_statuses = ("cloud_waiting", "submitting", "pending_market", "manual_review")
    rows = db.query(PortfolioDcaOrder).join(
        Portfolio, Portfolio.id == PortfolioDcaOrder.portfolio_id
    ).filter(
        Portfolio.status == "active",
        PortfolioDcaOrder.status.in_(reserve_statuses),
    ).all()
    return sum(
        max(0.0, float(row.planned_usdt or 0.0))
        for row in rows if row.id != exclude_id
    )


async def _place_portfolio_dca_orders(db, portfolio, client=None, progress=None):
    """Place persistent limit-buy DCA orders directly on the exchange."""
    strategy = normalize_entry_strategy(getattr(portfolio, "entry_strategy", "tp_sl"))
    if strategy != "tp_sl_dca":
        return {"placed": 0, "skipped": 0, "budget": 0.0, "messages": []}
    client = client or get_mexc()
    existing_active = db.query(PortfolioDcaOrder).filter(
        PortfolioDcaOrder.portfolio_id == portfolio.id,
        PortfolioDcaOrder.level > 0,
        PortfolioDcaOrder.status.in_(tuple(_DCA_ACTIVE_STATUSES)),
    ).all()
    if existing_active:
        reserved = sum(max(0.0, float(row.planned_usdt or 0.0)) for row in existing_active)
        return {
            "placed": 0, "skipped": 0, "budget": reserved,
            "messages": ["يوجد سلم DCA نشط بالفعل؛ لم تتم إضافة مستويات مكررة."],
        }
    coins = list(portfolio.coins or [])
    if not coins:
        return {"placed": 0, "skipped": 0, "budget": 0.0, "messages": ["لا توجد عملات لإنشاء أوامر DCA"]}
    pct = normalize_dca_buy_pct(getattr(portfolio, "dca_buy_pct", 50))
    count = normalize_dca_order_count(getattr(portfolio, "dca_order_count", 1))
    budgets = calculate_strategy_budgets(portfolio.investment_usdt, strategy, pct, len(coins), count)
    reserve_budget = budgets["dca_budget"]
    requested_slots = len(coins) * count
    loop = asyncio.get_running_loop()
    free = max(0.0, float(await loop.run_in_executor(None, client.get_free_usdt) or 0.0))
    already_reserved = _cloud_dca_reserved_usdt(db)
    free_after_reserves = max(0.0, free - already_reserved)
    placing_budget = min(reserve_budget, free_after_reserves * 0.995)
    weights = [getattr(portfolio, f"dca{i}_alloc_pct", DEFAULT_WEIGHT) for i, DEFAULT_WEIGHT in enumerate((50.0, 30.0, 20.0), 1)]
    drops = [getattr(portfolio, f"dca{i}_drop_pct", DEFAULT_DROP) for i, DEFAULT_DROP in enumerate((3.0, 6.0, 10.0), 1)]
    try:
        weights, drops = normalize_dca_level_settings(weights, drops)
    except ValueError as exc:
        return {"placed": 0, "skipped": requested_slots, "budget": 0.0, "messages": [f"إعدادات DCA غير صالحة: {exc}"]}
    per_level_coin_budgets = calculate_dca_level_budgets(placing_budget, len(coins), weights, count)
    placed = skipped = 0
    attempted_slots = 0
    messages = []
    if placing_budget + 0.01 < reserve_budget:
        messages.append(f"ميزانية DCA المتاحة أقل من الاحتياطي الاسمي: المتاح {placing_budget:.2f} من أصل {reserve_budget:.2f} USDT")
    levels_by_coin = {}
    for coin in coins:
        entry = max(0.0, float(coin.entry_price or 0.0))
        if entry <= 0:
            try:
                entry = float(await loop.run_in_executor(
                    None, lambda sym=coin.symbol: client.get_ticker_price(f"{sym}/{client.quote}") or 0.0
                ) or 0.0)
            except Exception:
                entry = 0.0
        levels = [entry * (1.0 - drops[i] / 100.0) for i in range(count)] if entry > 0 else []
        if not levels or any(price <= 0 for price in levels):
            skipped += count
            messages.append(f"{coin.symbol}: تعذر حساب مستويات DCA من نسب الهبوط")
            continue
        levels_by_coin[coin.id] = (coin, levels)

    slot_specs = []
    for level_idx in range(1, count + 1):
        level_budget_per_coin = per_level_coin_budgets[level_idx - 1]
        for coin in coins:
            coin_levels = levels_by_coin.get(coin.id)
            if coin_levels and len(coin_levels[1]) >= level_idx:
                slot_specs.append((coin, level_idx, coin_levels[1][level_idx - 1], level_budget_per_coin))

    total_slots = len(slot_specs)
    for coin, level_idx, level_price, per_order in slot_specs:
        if per_order + 1e-9 < PORTFOLIO_MIN_ORDER_USDT:
            skipped += 1
            continue
        attempted_slots += 1
        row = PortfolioDcaOrder(
            portfolio_id=portfolio.id,
            portfolio_coin_id=coin.id,
            symbol=coin.symbol.upper(),
            order_id=None,
            level=level_idx,
            limit_price=float(level_price),
            planned_usdt=float(per_order),
            status="submitting",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        try:
            order = await loop.run_in_executor(
                None, lambda sym=coin.symbol, budget=per_order, price=float(level_price):
                client.create_limit_buy_usdt(sym, budget, price)
            )
            submission_status, order_id = classify_dca_submission(order)
            if submission_status != "open":
                row.status = submission_status
                row.updated_at = datetime.utcnow()
                db.commit()
                if submission_status == "rejected":
                    messages.append(
                        f"{coin.symbol} DC{level_idx}: لم يُنشأ أمر بسبب فحص مسبق/حد أدنى مؤكد؛ "
                        "لن تتم إعادة المحاولة تلقائيًا"
                    )
                else:
                    messages.append(
                        f"{coin.symbol} DC{level_idx}: استجابة بلا معرّف أمر موثوق؛ "
                        "مراجعة يدوية مطلوبة، وممنوع تكرار الشراء تلقائيًا"
                    )
                skipped += 1
                continue
            row.order_id = order_id
            row.status = "open"
            row.updated_at = datetime.utcnow()
            db.commit()
            placed += 1
            messages.append(
                f"{coin.symbol} DC{level_idx}: شراء Limit بوزن {weights[level_idx - 1]:.1f}% من احتياطي DCA، "
                f"هبوط {drops[level_idx - 1]:.2f}% عند {float(level_price):.8g}، ميزانية {per_order:.2f} USDT، أمر {order_id}"
            )
        except Exception as exc:
            # Submission timeout may still mean the exchange accepted it.
            row.status = "manual_review"
            row.updated_at = datetime.utcnow()
            db.commit()
            messages.append(f"{coin.symbol} DC{level_idx}: حالة إرسال غير محسومة ({str(exc)[:90]})؛ لا إعادة تلقائية")
            skipped += 1
        if progress is not None:
            try:
                await progress(attempted_slots, total_slots, placed, skipped)
            except Exception:
                logger.exception("DCA progress notification failed portfolio=%s", portfolio.id)
    return {"placed": placed, "skipped": skipped, "budget": sum(spec[3] for spec in slot_specs if spec[3] + 1e-9 >= PORTFOLIO_MIN_ORDER_USDT), "messages": messages}


async def _sync_active_dca_orders(db, context=None):
    """Monitor cloud DCA triggers; ambiguous submissions are never retried automatically."""
    # First resume committed DCA fills whose TP replacement was interrupted.
    # The recovery helper refuses to create a duplicate sell if it sees any
    # untracked exchange order left by a previous attempt.
    _recover_pending_tp_refreshes(db)
    active = db.query(PortfolioDcaOrder).join(Portfolio, Portfolio.id == PortfolioDcaOrder.portfolio_id).filter(
        Portfolio.status == "active", Portfolio.is_running == True,
        PortfolioDcaOrder.status.in_(tuple(_DCA_ACTIVE_STATUSES)),
    ).order_by(PortfolioDcaOrder.portfolio_id, PortfolioDcaOrder.symbol, PortfolioDcaOrder.level).all()
    if not active:
        return
    client, reb = get_mexc(), get_reb()
    loop = asyncio.get_event_loop()
    for dca in active:
        try:
            pair = f"{dca.symbol}/{client.quote}"
            if dca.status == "cloud_waiting":
                # Migrate any legacy cloud trigger into a real exchange limit order.
                coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == dca.portfolio_coin_id).first()
                if not coin or str(coin.position_status or "") in ("closed", "idle", "waiting_reentry"):
                    continue
                planned_budget = max(0.0, float(dca.planned_usdt or 0.0))
                free_now = max(0.0, float(await loop.run_in_executor(None, client.get_free_usdt) or 0.0))
                other_reserved = _cloud_dca_reserved_usdt(db, exclude_id=dca.id)
                if free_now - other_reserved + 1e-9 < planned_budget:
                    continue
                if planned_budget + 1e-9 < PORTFOLIO_MIN_ORDER_USDT:
                    dca.status = "manual_review"
                    dca.updated_at = datetime.utcnow()
                    db.commit()
                    continue
                claimed = claim_cloud_dca_submission(db, PortfolioDcaOrder, dca.id)
                if not claimed:
                    db.refresh(dca)
                    continue
                db.refresh(dca)
                try:
                    order = await loop.run_in_executor(
                        None, lambda sym=dca.symbol, budget=planned_budget, price=float(dca.limit_price or 0):
                        client.create_limit_buy_usdt(sym, budget, price)
                    )
                except Exception:
                    dca.status = "manual_review"
                    dca.updated_at = datetime.utcnow()
                    db.commit()
                    continue
                submission_status, order_id = classify_dca_submission(order)
                if submission_status != "open":
                    dca.status = submission_status
                    dca.updated_at = datetime.utcnow()
                    db.commit()
                    continue
                dca.order_id, dca.status = order_id, "open"
                dca.updated_at = datetime.utcnow()
                db.commit()
            if dca.status in ("submitting", "manual_review") and not dca.order_id:
                # Query recent exchange history once; bind an order only if one
                # unique buy matches timestamp and quote cost. Never resubmit.
                exchange = getattr(client, "exchange", None)
                matched = None
                if exchange is not None and any(callable(getattr(exchange, name, None)) for name in ("fetch_orders", "fetch_open_orders", "fetch_closed_orders", "fetch_canceled_orders")):
                    try:
                        # Do not use updated_at: retrying manual_review mutates it and
                        # makes the historical order fall outside the next search window.
                        attempted_at = dca.created_at or dca.updated_at or datetime.utcnow()
                        created_ms = _utc_datetime_ms(attempted_at)
                        history = await loop.run_in_executor(
                            None, lambda: _fetch_dca_order_candidates(exchange, pair, created_ms)
                        )
                        already_linked = {
                            str(value[0]) for value in db.query(PortfolioDcaOrder.order_id).filter(
                                PortfolioDcaOrder.portfolio_id == dca.portfolio_id,
                                PortfolioDcaOrder.symbol == dca.symbol,
                                PortfolioDcaOrder.order_id.isnot(None),
                            ).all() if value[0]
                        }
                        matched = match_unidentified_dca_order(
                            history, created_ms, float(dca.planned_usdt or 0.0),
                            expected_price=float(dca.limit_price or 0.0),
                            exclude_order_ids=already_linked,
                        )
                    except Exception:
                        logger.exception("DCA order-history reconciliation failed row=%s", dca.id)
                if not matched:
                    dca.status = "manual_review"
                    # Preserve the original submission timestamp so repeated history
                    # lookups can still find a delayed exchange record.
                    db.commit()
                    continue
                dca.order_id = str(matched["id"])
                dca.status = "pending_market"
                db.commit()
            if dca.order_id:
                state = await loop.run_in_executor(None, lambda oid=dca.order_id, p=pair: client.fetch_order(oid, p, strict=True))
                if state:
                    previous_status = str(dca.status or "").lower()
                    delta = _apply_dca_order_snapshot(db, dca, state, reb=reb)
                    if int(dca.level or 0) > 0 and dca.status == "filled" and previous_status != "filled":
                        await _shift_cloud_dca_ladder(db, dca.portfolio_coin_id, "down", exclude_id=dca.id, client=client)
                    if delta > 0 and context is not None:
                        pf = db.query(Portfolio).filter(Portfolio.id == dca.portfolio_id).first()
                        coin_now = db.query(PortfolioCoin).filter(PortfolioCoin.id == dca.portfolio_coin_id).first()
                        if pf:
                            try:
                                label = "DCA على المنصة" if int(dca.level or 0) > 0 else "شراء أولي تمت تسويته"
                                suffix = (" تنبيه: لم يمكن تحديث كل أوامر TP بأمان؛ راجع الأوامر المسجلة في MEXC."
                                          if getattr(coin_now, "_tp_refresh_deferred", False) else " تم تحديث متوسط الدخول وأوامر TP.")
                                await context.bot.send_message(pf.telegram_id, f"🪙 {label}: {dca.symbol}، كمية مؤكدة {delta:.8g}.{suffix}")
                            except Exception:
                                logger.exception("DCA fill notification failed order=%s", dca.order_id)
        except Exception:
            logger.exception("Cloud DCA reconciliation failed portfolio=%s row=%s", dca.portfolio_id, dca.id)
            if dca.status == "submitting":
                dca.status = "manual_review"
                db.commit()


async def _shift_cloud_dca_ladder(db, coin_id, direction, exclude_id=None, client=None):
    """Shift remaining DCA levels on MEXC by cancel/confirm/re-place, never by DB only."""
    coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == coin_id).first()
    if not coin:
        return 0
    portfolio = db.query(Portfolio).filter(Portfolio.id == coin.portfolio_id).first()
    if not portfolio:
        return 0
    rows = db.query(PortfolioDcaOrder).filter(
        PortfolioDcaOrder.portfolio_coin_id == coin_id,
        PortfolioDcaOrder.id != (exclude_id or -1),
        PortfolioDcaOrder.status.in_(("cloud_waiting", "open", "partial")),
    ).order_by(PortfolioDcaOrder.level.asc()).all()
    if not rows:
        return 0
    stop_pct = max(0.0, float(portfolio.stop_loss_pct if portfolio.stop_loss_pct is not None else 3.0))
    count = max(1, int(getattr(portfolio, "dca_order_count", 3) or 3))
    drops = sorted(float(getattr(portfolio, f"dca{i}_drop_pct", default) or default)
                   for i, default in enumerate((3.0, 6.0, 10.0), 1))[:count]
    rung_gaps = [drops[i] - drops[i - 1] for i in range(1, len(drops)) if drops[i] > drops[i - 1]]
    average_gap_pct = (sum(rung_gaps) / len(rung_gaps)) if rung_gaps else (drops[0] if drops else 3.0)
    gap = max(float(coin.entry_price or 0) * average_gap_pct / 100.0, 0.0)
    if stop_pct <= 0:
        stop = 0.0
    else:
        stop = float(coin.current_sl_price or coin.original_sl_price or 0.0)
        if stop <= 0 and float(coin.entry_price or 0) > 0:
            stop = float(coin.entry_price) * (1.0 - stop_pct / 100.0)
    prices = shift_dca_level_prices([float(row.limit_price or 0) for row in rows], gap, direction, stop)
    client = client or get_mexc()
    loop = asyncio.get_running_loop()
    moved = 0
    for row, new_price in zip(rows, prices):
        old_order_id = str(row.order_id or "")
        old_filled = float(row.filled_amount or 0.0)
        if old_order_id:
            try:
                state = await loop.run_in_executor(
                    None, lambda oid=old_order_id, sym=row.symbol: client.fetch_order(oid, f"{sym}/{client.quote}", strict=True)
                )
                status = str((state or {}).get("status") or "").lower()
                if status not in _DCA_TERMINAL_STATUSES:
                    await loop.run_in_executor(
                        None, lambda oid=old_order_id, sym=row.symbol: client.cancel_order(oid, f"{sym}/{client.quote}", strict=True)
                    )
                    state = await loop.run_in_executor(
                        None, lambda oid=old_order_id, sym=row.symbol: client.fetch_order(oid, f"{sym}/{client.quote}", strict=True)
                    )
                    status = str((state or {}).get("status") or "").lower()
                if status not in _DCA_TERMINAL_STATUSES:
                    row.status = "manual_review"
                    row.updated_at = datetime.utcnow()
                    db.commit()
                    continue
                # Account for a fill racing the cancel. Never replace a filled rung.
                if float((state or {}).get("filled") or 0.0) > old_filled:
                    _apply_dca_order_snapshot(db, row, state)
                    row.status = "filled" if status in ("closed", "filled") else "cancelled"
                    row.updated_at = datetime.utcnow()
                    db.commit()
                    continue
            except Exception:
                row.status = "manual_review"
                row.updated_at = datetime.utcnow()
                db.commit()
                continue
        row.order_id = None
        row.status = "submitting"
        row.limit_price = float(new_price)
        row.updated_at = datetime.utcnow()
        db.commit()
        try:
            order = await loop.run_in_executor(
                None, lambda sym=row.symbol, budget=float(row.planned_usdt or 0), price=float(new_price):
                client.create_limit_buy_usdt(sym, budget, price)
            )
            submission_status, new_id = classify_dca_submission(order)
            row.status = submission_status
            if submission_status == "open":
                row.order_id = new_id
                moved += 1
        except Exception:
            row.status = "manual_review"
        row.updated_at = datetime.utcnow()
        db.commit()
    return moved


def portfolio_entry_pct_keyboard(pf_id: int):
    rows = [
        [InlineKeyboardButton("25% من المخصص", callback_data=f"start_pf_pct_{pf_id}_25"),
         InlineKeyboardButton("50% من المخصص", callback_data=f"start_pf_pct_{pf_id}_50")],
        [InlineKeyboardButton("70% من المخصص", callback_data=f"start_pf_pct_{pf_id}_70"),
         InlineKeyboardButton("100% من المخصص", callback_data=f"start_pf_pct_{pf_id}_100")],
        [InlineKeyboardButton("× إلغاء", callback_data=f"view_{pf_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def all_portfolios_entry_pct_keyboard():
    rows = [
        [InlineKeyboardButton("25% لكل محفظة", callback_data="start_all_pct_25"),
         InlineKeyboardButton("50% لكل محفظة", callback_data="start_all_pct_50")],
        [InlineKeyboardButton("70% لكل محفظة", callback_data="start_all_pct_70"),
         InlineKeyboardButton("100% لكل محفظة", callback_data="start_all_pct_100")],
        [InlineKeyboardButton("× إلغاء", callback_data="list_pf")],
    ]
    return InlineKeyboardMarkup(rows)


def trim_pct_keyboard(pf_id: int):
    rows = [[
        InlineKeyboardButton("✂️ تخفيف 25%", callback_data=f"trim_select_{pf_id}_25"),
        InlineKeyboardButton("✂️ تخفيف 50%", callback_data=f"trim_select_{pf_id}_50"),
        InlineKeyboardButton("✂️ تخفيف 70%", callback_data=f"trim_select_{pf_id}_70"),
    ], [InlineKeyboardButton("↩ رجوع للمحفظة", callback_data=f"view_{pf_id}")]]
    return InlineKeyboardMarkup(rows)


def restore_trim_pct_keyboard(pf_id: int):
    rows = [
        [InlineKeyboardButton("↩ استرجاع 25%", callback_data=f"restore_select_{pf_id}_25"),
         InlineKeyboardButton("↩ استرجاع 50%", callback_data=f"restore_select_{pf_id}_50")],
        [InlineKeyboardButton("↩ استرجاع 70%", callback_data=f"restore_select_{pf_id}_70"),
         InlineKeyboardButton("↩ استرجاع 100%", callback_data=f"restore_select_{pf_id}_100")],
        [InlineKeyboardButton("↩ رجوع للمحفظة", callback_data=f"view_{pf_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def _outstanding_trim_amounts(db, portfolio_id: int, telegram_id: int):
    """Return each position's net manually-trimmed quantity not yet restored."""
    events = get_portfolio_trade_events(db, portfolio_id, telegram_id)
    totals = calculate_outstanding_trim_amounts(events)
    return {key: {"symbol": key[1], "amount": amount} for key, amount in totals.items()}




def get_mexc() -> MexcClient:
    global _mexc
    if _mexc is None:
        _mexc = MexcClient()
    return _mexc


def ar_error(exc, context: str = "") -> str:
    """وصف خطأ بالعربي للبوت + اللوج."""
    msg = str(exc or "").strip()
    low = msg.lower()
    if "not accepted" in low or "tp1" in low or "tp2" in low or "tp3" in low:
        ar = "المنصة رفضت أمر البيع المحدد (كمية صغيرة أو حدود الزوج)"
    elif "insufficient" in low or "balance" in low:
        ar = "الرصيد غير كافٍ لتنفيذ الأمر"
    elif "min" in low and ("notional" in low or "cost" in low or "amount" in low):
        ar = "قيمة الأمر أقل من الحد الأدنى للمنصة"
    elif "precision" in low or "tick" in low:
        ar = "دقة السعر/الكمية غير متوافقة مع قواعد المنصة"
    elif "timeout" in low or "network" in low or "connection" in low:
        ar = "مشكلة اتصال بالمنصة أو انتهاء المهلة"
    elif "rate limit" in low or "too many" in low:
        ar = "تم تجاوز حد الطلبات — حاول بعد لحظات"
    else:
        ar = "خطأ تقني أثناء التنفيذ"
    full = f"{ar}"
    if context:
        full = f"{context}: {full}"
    if msg:
        full = f"{full}\nالتفاصيل: `{msg[:200]}`"
    return full


def _is_silent_reentry_balance_error(error) -> bool:
    """Automatic re-entry should quietly wait when the exchange has no balance."""
    text = str(error or "").lower()
    return any(marker in text for marker in (
        "30004",
        "insufficient position",
        "insufficient balance",
        "insufficient funds",
        "not enough balance",
    ))


def get_reb() -> Rebalancer:
    global _reb
    if _reb is None:
        _reb = Rebalancer(get_mexc())
    return _reb


def _fetch_start_tp_inputs(symbols):
    """Fetch batched free balances and prices off the asyncio event loop.

    This function is intentionally synchronous and must be called in an executor
    from async handlers. Fetch balances and prices in batches so a portfolio with
    many coins does not issue one balance request and one ticker request per coin.
    Never fall back to total (locked + free) balance for a zero-free coin: doing so
    can create duplicate sell orders against funds already locked in existing orders.
    """
    client = get_mexc()
    requested = list(dict.fromkeys(str(symbol).upper().strip() for symbol in symbols if symbol))
    balances = client.get_balance()
    prices = client.get_all_prices(requested)
    coins_data = []
    results = []
    for symbol in requested:
        asset = client.normalize_asset_symbol(symbol)
        amount = float(balances.get(asset, 0.0) or 0.0)
        price = float(prices.get(asset, 0.0) or 0.0)
        if amount <= 0:
            results.append({
                "symbol": symbol, "amount": 0.0, "entry_price": price,
                "error": "لا توجد كمية حرة للبيع؛ قد يكون الرصيد محجوزًا في أوامر مفتوحة. لم يُنشأ أمر بيع مكرر.",
                "tp1_price": 0.0, "tp2_price": 0.0, "tp3_price": 0.0,
                "sl_price": 0.0, "original_sl_price": 0.0,
                "tp1_order_id": None, "tp2_order_id": None, "tp3_order_id": None,
            })
            continue
        if price <= 0:
            results.append({
                "symbol": symbol, "amount": amount, "entry_price": 0.0,
                "error": "تعذر جلب سعر العملة من MEXC؛ لم تُرسل أوامر بيع.",
                "tp1_price": 0.0, "tp2_price": 0.0, "tp3_price": 0.0,
                "sl_price": 0.0, "original_sl_price": 0.0,
                "tp1_order_id": None, "tp2_order_id": None, "tp3_order_id": None,
            })
            continue
        coins_data.append({
            "symbol": symbol, "amount": amount, "entry_price": price,
            "place_exchange_orders": False,
        })
    return coins_data, results


def is_admin(user_id: int) -> bool:
    # Fail closed: an absent or invalid configured admin never opens the bot.
    admin_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
    return admin_id > 0 and user_id == admin_id


async def ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        msg = update.effective_message
        if msg:
            await msg.reply_text("⛔ *غير مصرح*\nهذا البوت مخصص للأدمن فقط.", parse_mode="Markdown")
        return False
    return True


def main_menu_keyboard():
    """Modern main navigation; callback_data intentionally unchanged."""
    return ui.markup([
        ui.row((f"{ui.I['portfolio']}  المحافظ", "list_pf"), (f"{ui.I['new']}  محفظة جديدة", "create_pf")),
        ui.row((f"{ui.I['balance']}  الرصيد", "balance"), (f"{ui.I['settings']}  الإعدادات", "settings")),
        [ui.button(f"{ui.I['cleanup']}  تنظيف البيانات", "cleanup_db")],
        [ui.button(f"{ui.I['help']}  شرح المصطلحات", "help")],
    ])


def pf_keyboard(pf_id: int, is_running: bool):
    """أزرار المحفظة — شكل موحد دون تغيير أي callback."""
    rows = [
        ui.row(
            (f"{ui.I['stop'] if is_running else ui.I['play']}  {'إيقاف' if is_running else 'تشغيل'}", f"toggle_{pf_id}"),
            (f"{ui.I['target']}  الأهداف", f"pf_tpsl_{pf_id}"),
        ),
        [ui.button("☁️  حالة المراقبة السحابية", f"monitor_health_{pf_id}")],
        ui.row((f"{ui.I['add']}  إضافة عملة", f"addcoin_{pf_id}"), (f"{ui.I['remove']}  إزالة عملة", f"removecoin_{pf_id}")),
        [ui.button("✂️  تخفيف الحيازة 25% / 50% / 70%", f"trim_menu_{pf_id}")],
        [ui.button("↩  استرجاع الكميات التي تم تخفيفها", f"restore_trim_menu_{pf_id}")],
        [ui.button(f"{ui.I['edit']}  تعديل المبلغ المخصص", f"edit_amount_{pf_id}")],
        ui.row(("⚖️  إعادة توازن عام", f"rebalance_{pf_id}"), (f"{ui.I['delete']}  حذف", f"delete_pf_{pf_id}")),
        [ui.button(f"{ui.I['back']}  القائمة الرئيسية", "menu")],
    ]
    return ui.markup(rows)


def _missing_selection_key(pf_id: int) -> str:
    return f"missing_reentry_selection_{pf_id}"


def _missing_reentry_keyboard(pf_id: int, missing_symbols, selected, allow_selection=True):
    rows = []
    if allow_selection:
        for symbol in missing_symbols:
            marker = "✅" if symbol in selected else "⬜"
            rows.append([
                InlineKeyboardButton(
                    f"{marker} {symbol}",
                    callback_data=f"missing_toggle_{pf_id}_{symbol}",
                )
            ])
    if selected and allow_selection:
        rows.append([
            InlineKeyboardButton(
                f"✅ تأكيد إعادة الدخول ({len(selected)})",
                callback_data=f"missing_confirm_{pf_id}",
            )
        ])
    rows.append([InlineKeyboardButton(f"{ui.I['refresh']}  إعادة الفحص", callback_data=f"missing_{pf_id}")])
    rows.append([InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")])
    return InlineKeyboardMarkup(rows)


def format_pf(p, current_value: float = None, prices: dict = None, events=None) -> str:
    """عرض مبسّط: الربح/الخسارة من أساس آخر إعادة بناء."""
    status = "🟢 شغالة" if p.is_running else "⚪ متوقفة"
    mode = "يدوي"
    prices = prices or {}
    events = events or []

    # القيمة السوقية الحالية للمراكز المفتوحة
    open_value = 0.0
    for coin in p.coins:
        rem = position_remaining(coin)
        if rem <= 0:
            continue
        px = float(prices.get(coin.symbol) or 0)
        if px <= 0:
            px = float(coin.entry_price or 0)
        open_value += rem * px

    if current_value is not None and current_value >= 0:
        open_value = float(current_value)

    # أساس الحساب = آخر إعادة بناء (base_investment)، وإلا المخصص
    baseline = float(getattr(p, "base_investment", 0) or 0)
    if baseline <= 0:
        baseline = float(p.investment_usdt or 0)

    # محقق منذ آخر إعادة بناء + محقق آخر 24 ساعة
    rebuild_at = getattr(p, "last_rebalance", None)
    realized = 0.0
    realized_24h = 0.0
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    for e in events:
        created = getattr(e, "created_at", None)
        rp = float(getattr(e, "realized_pnl", 0) or 0)
        if created is not None and created >= cutoff_24h:
            realized_24h += rp
        if rebuild_at is not None:
            if created is not None and created < rebuild_at:
                continue
        realized += rp

    # غير المحقق ≈ القيمة الآن − (الأساس − المحقق)
    # تفسير أبسط: الإجمالي من الأساس = (القيمة الآن + المحقق) − الأساس
    # لأن المحقق خرج كـ USDT من المراكز
    equity_now = open_value + max(realized, 0)  # rough if realized positive already cashed
    # أدق: الربح الإجمالي = (قيمة مفتوحة − تكلفة متبقية تقديرية) + محقق
    # المستخدم طلب: من آخر إعادة بناء
    # عند إعادة البناء: baseline = قيمة المراكز وقتها
    # الآن: pnl = open_value - baseline + realized_since
    total_pnl = (open_value - baseline) + realized
    total_pct = (total_pnl / baseline * 100.0) if baseline > 0 else 0.0

    symbols = [c.symbol for c in p.coins]
    if symbols:
        target_per_coin = float(p.investment_usdt or 0) / len(symbols)
        coin_lines = []
        active_statuses = {"open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"}
        ordered_coins = sorted(
            p.coins,
            key=lambda coin: (
                0 if (
                    (coin.position_status or "idle") in active_statuses
                    and position_remaining(coin) > 0
                    and float(getattr(coin, "reentry_price", 0) or 0) <= 0
                    and (
                        position_remaining(coin)
                        * float(prices.get(coin.symbol) or coin.entry_price or 0)
                    ) >= 1.0
                ) else 1
            ),
        )
        for coin in ordered_coins:
            rem = position_remaining(coin)
            entry = float(coin.entry_price or 0)
            px = float(prices.get(coin.symbol) or entry or 0)
            value = rem * px
            coin_lines.append((coin.symbol.upper(), value))
        table_lines = [
            "┌────────────┬────────────┐",
        ]
        for index in range(0, len(coin_lines), 2):
            left_symbol, left_value = coin_lines[index]
            left = f"{left_symbol:<4}{left_value:>6.2f}"
            if index + 1 < len(coin_lines):
                right_symbol, right_value = coin_lines[index + 1]
                right = f"{right_symbol:<4}{right_value:>6.2f}"
            else:
                right = ""
            table_lines.append(f"│ {left:<10} │ {right:<10} │")
            if index + 2 < len(coin_lines):
                table_lines.append("├────────────┼────────────┤")
        table_lines.append("└────────────┴────────────┘")
        coins_block = "```\n" + "\n".join(table_lines) + "\n```"
    else:
        coins_block = "—"

    t_emoji = "🟢" if total_pnl >= 0 else "🔴"
    rebuild_txt = ""
    if rebuild_at is not None:
        try:
            rebuild_txt = f"\nآخر إعادة بناء: `{rebuild_at.strftime('%Y-%m-%d %H:%M')}` UTC"
        except Exception:
            pass

    allocated = float(p.investment_usdt or 0)
    n_coins = len([c for c in p.coins]) if p.coins else 0
    per_coin = (allocated / n_coins) if n_coins else 0.0
    # حالة المحفظة: أخضر / أصفر / أحمر
    if not p.is_running:
        health = "⚪ متوقفة"
    elif total_pct <= -5:
        health = "🔴 أحمر — احترس"
    elif total_pct <= -1.5:
        health = "🟡 أصفر — راقب"
    elif total_pct >= 1.5:
        health = "🟢 أخضر — في ربح"
    else:
        health = "🟢 مستقر"

    out = [
        ui.page_title(ui.I["portfolio"], f"{p.name}"),
        f"💰 المخصص: *{allocated:.0f}* USDT   📊 الحالي: *{open_value:.2f}* USDT",
        f"🪙 العملات: *{n_coins}*   🎯 التخصيص لكل عملة: *{per_coin:.2f}* USDT",
        "📋 *العملات*",
    ]
    out.append("")
    out.append(coins_block)
    return "\n".join(out)
def format_portfolio_stats(p, events, prices=None) -> str:
    """Format realized and current unrealized P&L for one portfolio."""
    prices = prices or {}
    realized = sum(float(e.realized_pnl or 0) for e in events)
    open_cost = 0.0
    open_value = 0.0
    open_lines = []
    for coin in p.coins:
        if coin.position_status not in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
            continue
        remaining = position_remaining(coin)
        entry = float(coin.entry_price or 0)
        price = float(prices.get(coin.symbol) or entry or 0)
        if remaining <= 0 or entry <= 0:
            continue
        cost = remaining * entry
        value = remaining * price
        open_cost += cost
        open_value += value
        pnl = value - cost
        emoji = "🟢" if pnl >= 0 else "🔴"
        open_lines.append(f"{emoji} `{coin.symbol}`: `{pnl:+.2f}` USDT")
    unrealized = open_value - open_cost
    total = realized + unrealized
    r_emoji = "🟢" if realized >= 0 else "🔴"
    u_emoji = "🟢" if unrealized >= 0 else "🔴"
    t_emoji = "🟢" if total >= 0 else "🔴"
    result = (
        ui.page_title("📊", f"إحصائيات · {p.name}") + "\n"
        "━━━━━━━━━━━━━━━━\n"
        f"{r_emoji} المحقق: `{realized:+.2f}` USDT\n"
        f"{u_emoji} غير المحقق: `{unrealized:+.2f}` USDT\n"
        f"{t_emoji} *الإجمالي: `{total:+.2f}` USDT*\n"
        f"عدد العمليات: `{len(events)}`"
    )
    if open_lines:
        result += "\n\n*المراكز المفتوحة:*\n" + "\n".join(open_lines)
    return result


def format_source(s) -> str:
    status = "🟢 *مفعل*" if s.enabled else "🔴 *معطل*"
    return (
        f"📡 *{s.name}*  `#{s.id}`\n"
        "━━━━━━━━━━━━━━━━\n"
        f"الحالة: {status}\n"
        f"الحد الأدنى: `{s.min_usd:,.0f}` $\n"
        f"أقصى تحويلات: `{s.max_tx_count}`\n"
        f"شراء: {'✅ مسموح' if s.allow_buy else '❌ ممنوع'}\n"
        f"بيع: {'✅ مسموح' if s.allow_sell else '❌ ممنوع'}\n"
        f"محافظ الشراء: `{s.buy_portfolio_ids or '—'}`\n"
        f"محافظ البيع: `{s.sell_portfolio_ids or '—'}`\n"
        f"التبريد: `{s.cooldown_minutes}` دقيقة"
    )


def _build_cleanup_plan(db, telegram_id: int, presence: dict[str, dict[str, float]]) -> dict:
    """Find only data that is provably inactive without touching working portfolios."""
    portfolios = get_portfolios(db, telegram_id, status=None)
    closed_portfolios = []
    stale_positions = []

    for portfolio in portfolios:
        portfolio_has_live_asset = any(
            presence.get(coin.symbol, {}).get("present", False)
            for coin in portfolio.coins
        )
        if portfolio.status != "active" and not portfolio_has_live_asset:
            closed_portfolios.append({
                "id": portfolio.id,
                "name": portfolio.name,
                "coins": list(portfolio.coins),
            })
            continue

        # A running portfolio is always protected. A stopped portfolio keeps
        # its configuration, but stale position/TP/SL tracking is removable
        # when MEXC confirms that the asset is no longer held.
        if portfolio.status != "active" or portfolio.is_running:
            continue
        for coin in portfolio.coins:
            if coin.position_status in (None, "", "idle", "waiting_reentry"):
                continue
            if presence.get(coin.symbol, {}).get("present", False):
                continue
            stale_positions.append({
                "id": coin.id,
                "portfolio_id": portfolio.id,
                "portfolio_name": portfolio.name,
                "symbol": coin.symbol,
                "position_status": coin.position_status,
                "coin": coin,
            })

    portfolio_ids = {portfolio.id for portfolio in portfolios}
    trade_query = db.query(PortfolioTrade).filter(
        PortfolioTrade.telegram_id == telegram_id
    )
    log_query = db.query(RebalanceLog).filter(
        RebalanceLog.telegram_id == telegram_id
    )
    if portfolio_ids:
        trade_query = trade_query.filter(~PortfolioTrade.portfolio_id.in_(portfolio_ids))
        log_query = log_query.filter(~RebalanceLog.portfolio_id.in_(portfolio_ids))

    return {
        "closed_portfolios": closed_portfolios,
        "stale_positions": stale_positions,
        "orphan_trades": trade_query.count(),
        "orphan_logs": log_query.count(),
    }


def _cleanup_report(plan: dict) -> str:
    closed = plan["closed_portfolios"]
    stale = plan["stale_positions"]
    orphan_trades = plan["orphan_trades"]
    orphan_logs = plan["orphan_logs"]
    lines = [
        "🔎 *فحص قاعدة البيانات*",
        "",
        "تم الإبقاء على كل محفظة تعمل وكل عملة لها رصيد أو أمر بيع قائم على MEXC.",
        "",
        f"🗑 محافظ مغلقة بلا أصول: `{len(closed)}`",
        f"🧹 مراكز قديمة بلا رصيد: `{len(stale)}`",
        f"🧾 سجلات عمليات يتيمة: `{orphan_trades}` | سجلات إعادة توازن يتيمة: `{orphan_logs}`",
    ]
    if closed:
        lines.append("\n*المحافظ المرشحة للحذف:*")
        lines.extend(f"• #{item['id']} {item['name']}" for item in closed[:10])
        if len(closed) > 10:
            lines.append(f"• ... و`{len(closed) - 10}` أخرى")
    if stale:
        lines.append("\n*بيانات المراكز المرشحة للمسح:*")
        lines.extend(
            f"• {item['portfolio_name']} — `{item['symbol']}` ({item['position_status']})"
            for item in stale[:15]
        )
        if len(stale) > 15:
            lines.append(f"• ... و`{len(stale) - 15}` أخرى")
    if not closed and not stale and not orphan_trades and not orphan_logs:
        lines.append("\n✅ لا توجد بيانات قديمة آمنة للتنظيف.")
    else:
        lines.extend([
            "",
            "لن يتم بيع أي أصل في عملية التنظيف.",
            "سيتم إلغاء أوامر DCA والأهداف المرتبطة بالبيانات القديمة، ولن تُحذف السجلات إذا تعذر التأكد من إلغاء أي أمر.",
        ])
    return "\n".join(lines)


async def _show_cleanup_scan(query, context, tid):

    db = SessionLocal()
    try:
        await query.edit_message_text("⏳ جاري فحص المحافظ والرصيد قبل اقتراح التنظيف...")
        portfolios = get_portfolios(db, tid, status=None)
        symbols = sorted({
            coin.symbol for portfolio in portfolios for coin in portfolio.coins
        })
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None,
                lambda: get_mexc().get_portfolio_presence(symbols) if symbols else {},
            )
        except Exception as exc:
            logger.exception("Database cleanup scan failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص الرصيد من MEXC.\n"
                "لم يتم حذف أو تعديل أي بيانات.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return

        plan = _build_cleanup_plan(db, tid, presence)
        context.user_data["cleanup_ready"] = True
        buttons = []
        if plan["closed_portfolios"] or plan["stale_positions"]:
            buttons.append([
                InlineKeyboardButton(f"{ui.I['save']}  تأكيد التنظيف", callback_data="cleanup_confirm"),
            ])
        buttons.extend([
            [InlineKeyboardButton(f"{ui.I['refresh']}  إعادة الفحص", callback_data="cleanup_db")],
            [InlineKeyboardButton(f"{ui.I['back']}  القائمة", callback_data="menu")],
        ])
        await query.edit_message_text(
            _cleanup_report(plan),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    finally:
        db.close()


@trading_guard.serialized
async def _do_cleanup(query, context, tid):

    db = SessionLocal()
    try:
        await query.edit_message_text("⏳ إعادة الفحص ثم تنظيف البيانات غير النشطة...")
        portfolios = get_portfolios(db, tid, status=None)
        symbols = sorted({
            coin.symbol for portfolio in portfolios for coin in portfolio.coins
        })
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None,
                lambda: get_mexc().get_portfolio_presence(symbols) if symbols else {},
            )
        except Exception as exc:
            logger.exception("Database cleanup preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر إعادة فحص الرصيد.\nلم يتم حذف أو تعديل أي بيانات.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return

        plan = _build_cleanup_plan(db, tid, presence)
        if (
            not plan["closed_portfolios"]
            and not plan["stale_positions"]
            and not plan["orphan_trades"]
            and not plan["orphan_logs"]
        ):
            context.user_data.pop("cleanup_ready", None)
            await query.edit_message_text(
                "✅ لا توجد بيانات قديمة آمنة للتنظيف بعد إعادة الفحص.",
                reply_markup=main_menu_keyboard(),
            )
            return

        cancelled = 0
        cleared = 0
        deleted_portfolios = 0

        for item in plan["stale_positions"]:
            coin = item["coin"]
            portfolio = db.query(Portfolio).filter(Portfolio.id == item["portfolio_id"]).first()
            if not portfolio:
                continue
            tracked_before_dca = max(
                float(coin.remaining_amount or 0.0), float(coin.amount or 0.0)
            )
            dca_errors = await _cancel_dca_orders(
                db, portfolio, client=get_mexc(), symbol=coin.symbol
            )
            if dca_errors:
                logger.warning(
                    "Cleanup skipped stale coin %s portfolio=%s because DCA orders are unresolved: %s",
                    coin.symbol, portfolio.id, dca_errors[:3],
                )
                continue
            db.refresh(coin)
            tracked_after_dca = max(
                float(coin.remaining_amount or 0.0), float(coin.amount or 0.0)
            )
            if tracked_after_dca > tracked_before_dca + max(1e-10, tracked_before_dca * 1e-8):
                # A pending buy filled during cancellation after the original wallet
                # scan. Keep the newly created position for the next fresh scan.
                logger.info(
                    "Cleanup preserved %s portfolio=%s because DCA filled during cleanup",
                    coin.symbol, portfolio.id,
                )
                continue
            result = get_reb().cancel_tp_orders([{
                "symbol": coin.symbol,
                "tp_order_id": coin.tp_order_id,
                "tp1_order_id": coin.tp1_order_id,
                "tp2_order_id": coin.tp2_order_id,
                "tp3_order_id": coin.tp3_order_id,
            }])
            cancelled += len(result.get("cancelled", []))
            if result.get("errors"):
                continue
            if clear_coin_position(db, coin.id):
                cleared += 1

        for item in plan["closed_portfolios"]:
            portfolio_cancel_failed = False
            portfolio = db.query(Portfolio).filter(Portfolio.id == item["id"]).first()
            if not portfolio:
                continue
            dca_errors = await _cancel_dca_orders(db, portfolio, client=get_mexc())
            if dca_errors:
                portfolio_cancel_failed = True
                logger.warning(
                    "Cleanup skipped closed portfolio %s because DCA orders are unresolved: %s",
                    portfolio.id, dca_errors[:3],
                )
            db.refresh(portfolio)
            if any(
                max(float(coin.remaining_amount or 0.0), float(coin.amount or 0.0)) > 1e-10
                for coin in portfolio.coins
            ):
                portfolio_cancel_failed = True
                logger.warning(
                    "Cleanup preserved closed portfolio %s because a DCA order left a tracked position",
                    portfolio.id,
                )
            for coin in item["coins"]:
                result = get_reb().cancel_tp_orders([{
                    "symbol": coin.symbol,
                    "tp_order_id": coin.tp_order_id,
                    "tp1_order_id": coin.tp1_order_id,
                    "tp2_order_id": coin.tp2_order_id,
                    "tp3_order_id": coin.tp3_order_id,
                }])
                cancelled += len(result.get("cancelled", []))
                portfolio_cancel_failed = portfolio_cancel_failed or bool(result.get("errors"))
            if not portfolio_cancel_failed and delete_portfolio_completely(db, item["id"], tid):
                deleted_portfolios += 1

        deleted_trades, deleted_logs = delete_orphaned_portfolio_records(db, tid)
        context.user_data.pop("cleanup_ready", None)
        await query.edit_message_text(
            "✅ *تم تنظيف قاعدة البيانات بعد إعادة الفحص.*\n\n"
            f"المحافظ المحذوفة: `{deleted_portfolios}`\n"
            f"بيانات المراكز القديمة الممسوحة: `{cleared}`\n"
            f"أوامر الأهداف الملغاة: `{cancelled}`\n\n"
            f"سجلات العمليات اليتيمة المحذوفة: `{deleted_trades}`\n"
            f"سجلات إعادة التوازن اليتيمة المحذوفة: `{deleted_logs}`\n\n"
            "تم ترك المحافظ العاملة والعملات ذات الرصيد كما هي.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        db.close()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin(update):
        return
    db = SessionLocal()
    try:
        get_or_create_user(db, update.effective_user.id)
    finally:
        db.close()
    await update.message.reply_text(
        ui.page_title("◆", "Mohamed Rade", "إدارة محافظ MEXC Spot بواجهة عصرية وواضحة.") +
        "\nإدارة المحافظ • إعادة الموازنة وإدارة المراكز\n"
        "اختر القسم المطلوب من القائمة أدناه.\n\n"
        + ui.tip("لو مش فاهم أي كلمة اكتب /help لعرض شرح المصطلحات."),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """صفحة شرح المصطلحات: تبسيط الكلمات الصعبة داخل البوت."""
    if not await ensure_admin(update):
        return
    await update.message.reply_text(
        ui.glossary_page(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❎ تم الإلغاء.\nرجعت للقائمة الرئيسية 👇",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        ui.page_title(ui.I["home"], "لوحة التحكم", "اختر القسم المطلوب:") + ui.footer(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user text without waiting behind exchange-monitoring work.

    Trading functions called from this handler keep their own serialization;
    plain settings and conversation replies must remain responsive.
    """
    if not update.message or not update.message.text:
        return
    if not await ensure_admin(update):
        return

    text = update.message.text
    tid = update.effective_user.id

    if context.user_data.get("waiting"):
        # تعديل إعدادات المحافظ العامة
        user_field = context.user_data.get("edit_user_tpsl")
        if user_field:
            try:
                val = float(text.strip().replace("%", "").replace(",", "."))
                if not math.isfinite(val):
                    raise ValueError("القيمة يجب أن تكون رقمًا محدودًا")
                if val < 0 or val > 100:
                    await update.message.reply_text("أدخل رقم بين 0 و 100.")
                    return
                if user_field == "dca_buy_pct" and not (10 <= val <= 90):
                    await update.message.reply_text("نسبة الدخول الأولي يجب أن تكون من 10 إلى 90%.")
                    return
                if user_field == "dca_order_count" and val not in (1, 2, 3):
                    await update.message.reply_text("عدد أوامر DCA يجب أن يكون 1 أو 2 أو 3.")
                    return
                if user_field == "dca_range_pct" and not (0.1 <= val <= 30):
                    await update.message.reply_text("نطاق DCA يجب أن يكون بين 0.1 و30%.")
                    return
                if user_field.endswith("_alloc_pct") and not (0 <= val <= 100):
                    await update.message.reply_text("وزن المستوى يجب أن يكون بين 0 و100%.")
                    return
                if user_field.endswith("_drop_pct") and not (0.1 <= val <= 90):
                    await update.message.reply_text("نسبة الهبوط يجب أن تكون بين 0.1 و90%.")
                    return
                db = SessionLocal()
                try:
                    user = get_or_create_user(db, update.effective_user.id)
                    if hasattr(user, user_field):
                        if user_field.endswith("_alloc_pct"):
                            weights = [float(getattr(user, f"dca{i}_alloc_pct", default) or 0) for i, default in enumerate((50, 30, 20), 1)]
                            weights[int(user_field[3]) - 1] = val
                            if sum(weights) > 100.000001:
                                await update.message.reply_text(f"مجموع أوزان DC1 وDC2 وDC3 سيصبح {sum(weights):.1f}%. يجب ألا يتجاوز 100%.")
                                return
                        if user_field.endswith("_drop_pct"):
                            drops = [float(getattr(user, f"dca{i}_drop_pct", default) or default) for i, default in enumerate((3, 6, 10), 1)]
                            drops[int(user_field[3]) - 1] = val
                            try:
                                normalize_dca_level_settings([50, 30, 20], drops)
                            except ValueError as exc:
                                await update.message.reply_text(f"ترتيب الهبوط غير صالح: {exc}. يجب أن يكون هبوط DC1 أقل من DC2 وأقل من DC3.")
                                return
                        setattr(user, user_field, val)
                        # انسخ إعدادات التريل لكل محافظ المستخدم
                        if user_field in ("trail_pct", "be_lock_pct", "stop_loss_pct"):
                            for pf in db.query(Portfolio).filter(
                                Portfolio.telegram_id == update.effective_user.id,
                                Portfolio.status == "active",
                            ).all():
                                setattr(pf, user_field, val)
                                if user_field == "stop_loss_pct":
                                    for coin in pf.coins:
                                        if position_remaining(coin) <= 0:
                                            continue
                                        if val == 0:
                                            # Global zero is a true disable, including already persisted stops.
                                            coin.current_sl_price = 0.0
                                            coin.original_sl_price = 0.0
                                        elif float(coin.entry_price or 0) > 0:
                                            calculated = float(coin.entry_price) * (1.0 - val / 100.0)
                                            coin.original_sl_price = calculated
                                            current = float(coin.current_sl_price or 0.0)
                                            coin.current_sl_price = max(current, calculated) if current > 0 else calculated
                        db.commit()
                        await update.message.reply_text(
                            f"✅ تم تحديث `{user_field}` = `{val}`",
                            parse_mode="Markdown",
                            reply_markup=main_menu_keyboard(),
                        )
                    else:
                        await update.message.reply_text("حقل غير معروف.", reply_markup=main_menu_keyboard())
                finally:
                    db.close()
                context.user_data.clear()
            except ValueError:
                await update.message.reply_text("أدخل رقم صحيح (مثال: 5)")
            return

        # تعديل أهداف محفظة معيّنة
        pf_edit = context.user_data.get("edit_pf_tpsl")
        if pf_edit:
            try:
                val = float(text.strip().replace("%", "").replace(",", "."))
                if not math.isfinite(val):
                    raise ValueError("القيمة يجب أن تكون رقمًا محدودًا")
                if val < 0 or val > 100:
                    await update.message.reply_text("أدخل رقم بين 0 و 100 (0 = استخدم العام).")
                    return
                field = pf_edit.get("field")
                pf_id = pf_edit.get("pf_id")
                db = SessionLocal()
                try:
                    p = get_portfolio(db, pf_id, update.effective_user.id)
                    if not p:
                        await update.message.reply_text("المحفظة غير موجودة.")
                        return
                    if field and hasattr(p, field):
                        setattr(p, field, val if val > 0 else None)
                        db.commit()
                        await update.message.reply_text(
                            f"✅ تم تحديث `{field}` = `{val}`",
                            parse_mode="Markdown",
                            reply_markup=main_menu_keyboard(),
                        )
                    else:
                        await update.message.reply_text("حقل غير معروف.", reply_markup=main_menu_keyboard())
                finally:
                    db.close()
                context.user_data.clear()
            except ValueError:
                await update.message.reply_text("أدخل رقم صحيح (مثال: 5)")
            return
        return


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route UI callbacks without holding the global trading lock.

    Mutating exchange workflows called below are individually serialized, while
    menus, settings, and status views can answer immediately during monitoring.
    """
    # شرح المصطلحات: زر مستقل بدون أي تأثير على منطق التداول.
    try:
        if update.callback_query.data == "help":
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                ui.glossary_page(),
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return
    except Exception:
        pass
    query = update.callback_query
    # Answer immediately so Telegram doesn't timeout (query expires ~seconds)
    try:
        await query.answer()
    except Exception:
        pass
    if not await ensure_admin(update):
        return
    data = query.data or ""
    tid = update.effective_user.id
    if data == "menu":
        await query.edit_message_text(
            ui.page_title(ui.I["home"], "لوحة التحكم", "اختر القسم المطلوب من القائمة:") + ui.footer(),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "list_pf":
        db = SessionLocal()
        try:
            pfs = get_portfolios(db, tid, status="active")
            if not pfs:
                await query.edit_message_text(
                    "📭 لا توجد محافظ نشطة حالياً.\nاضغط ➕ *محفظة جديدة* للبدء.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(),
                )
                return
            buttons = [
                [InlineKeyboardButton(
                    f"{'🟢' if p.is_running else '⚪'}"
                    f" #{p.id} {p.name} · {p.investment_usdt:.0f}$",
                    callback_data=f"view_{p.id}",
                )]
                for p in pfs
            ]
            buttons.extend([
                [InlineKeyboardButton("▶️ تشغيل الكل", callback_data="start_all")],
                [InlineKeyboardButton("⏹ إيقاف الكل", callback_data="stop_all_confirm")],
            ])
            buttons.append([InlineKeyboardButton(f"{ui.I['back']}  القائمة الرئيسية", callback_data="menu")])
            await query.edit_message_text(
                ui.page_title(ui.I["portfolio"], f"محافظك النشطة · {len(pfs)}", "اختر محفظة لعرض الحالة وإدارة المراكز."),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        finally:
            db.close()
        return

    if data == "balance":
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: get_mexc().get_portfolio_value())
            total = float(info.get("total_usdt") or 0)
            assets = info.get("assets") or {}
            lines = [ui.page_title(ui.I["balance"], "الرصيد", "حالة الرصيد الفعلية على MEXC."), f"الإجمالي ≈ `{total:.2f}` USDT", ""]
            # sort by value desc
            items = sorted(
                ((k, v) for k, v in assets.items() if float(v.get("usdt_value") or 0) > 0.5),
                key=lambda x: float(x[1].get("usdt_value") or 0),
                reverse=True,
            )
            for sym, row in items[:25]:
                lines.append(
                    f"`{sym}`: `{float(row.get('amount') or 0):.6g}` ≈ `{float(row.get('usdt_value') or 0):.2f}$`"
                )
            if not items:
                lines.append("_لا توجد أرصدة تُذكر_")
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        except Exception as e:
            logger.exception("balance failed")
            await query.edit_message_text(
                f"⚠️ فشل جلب الرصيد: `{e}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        return

    if data == "settings":
        db = SessionLocal()
        try:
            user = get_or_create_user(db, tid)
            # RTL anchor: every line starts with U+200F so Telegram keeps the
            # Arabic reading order. No backticks/pipes in mixed lines — they
            # create LTR islands that scramble the bidi layout.
            rlm = "\u200f"
            tp3_val = float(user.tp3_pct or 0)
            sl_val = float(user.stop_loss_pct or 0)
            total_w = (
                float(user.dca1_alloc_pct or 0)
                + float(user.dca2_alloc_pct or 0)
                + float(user.dca3_alloc_pct or 0)
            )

            def dc_line(idx, weight, drop):
                if float(weight or 0) <= 0:
                    return f"{rlm}◦ DC{idx}: معطّل (وزن 0%)"
                return f"{rlm}◦ DC{idx}: وزن {weight}% — هبوط {drop}%"

            msg = (
                f"{rlm}⚙️ *الإعدادات العامة*\n"
                f"{rlm}أهداف ووقف الخسارة للوضع اليدوي\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"{rlm}🎯 *أهداف الربح والبيع الجزئي*\n"
                f"{rlm}• TP1: {user.tp1_pct}% — بيع {user.tp1_sell_pct}%\n"
                f"{rlm}• TP2: {user.tp2_pct}% — بيع {user.tp2_sell_pct}%\n"
                f"{rlm}• TP3: {max(0, tp3_val)}%"
                + ("" if tp3_val > 0 else " (معطّل)") + "\n\n"
                f"{rlm}🛒 *خطة DCA:* {'مفعّلة ✅' if user.dca_enabled else 'متوقفة ⛔'}\n"
                f"{rlm}• الدخول الأولي: {user.dca_buy_pct}%\n"
                f"{rlm}• عدد أوامر DCA: {user.dca_order_count}\n"
                + dc_line(1, user.dca1_alloc_pct, user.dca1_drop_pct) + "\n"
                + dc_line(2, user.dca2_alloc_pct, user.dca2_drop_pct) + "\n"
                + dc_line(3, user.dca3_alloc_pct, user.dca3_drop_pct) + "\n"
                f"{rlm}• مجموع الأوزان: {total_w:.1f}% من الاحتياطي\n\n"
                f"{rlm}🛑 *وقف الخسارة:* {max(0, sl_val)}%"
                + ("" if sl_val > 0 else " (معطّل)") + "\n\n"
                f"{rlm}📊 أقصى عملات لكل محفظة: {user.max_coins_per_portfolio or 30}\n\n"
                f"{rlm}ℹ️ أهداف TP وأوامر DCA تُرسل كأوامر Limit على MEXC. الوقف يراقبه البوت داخليًا ولا يُرسل إذا كان 0."
            )
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◎ TP1 %", callback_data="set_tp1_pct"),
                     InlineKeyboardButton("↘ بيع 1 %", callback_data="set_tp1_sell_pct")],
                    [InlineKeyboardButton("◎ TP2 %", callback_data="set_tp2_pct"),
                     InlineKeyboardButton("↘ بيع 2 %", callback_data="set_tp2_sell_pct")],
                    [InlineKeyboardButton("◎ TP3 % (0 تعطيل)", callback_data="set_tp3_pct")],
                    [InlineKeyboardButton("🛒 نسبة الدخول الأولي %", callback_data="set_dca_buy_pct")],
                    [InlineKeyboardButton("DC1 وزن %", callback_data="set_dca1_alloc_pct"), InlineKeyboardButton("DC1 هبوط %", callback_data="set_dca1_drop_pct")],
                    [InlineKeyboardButton("DC2 وزن %", callback_data="set_dca2_alloc_pct"), InlineKeyboardButton("DC2 هبوط %", callback_data="set_dca2_drop_pct")],
                    [InlineKeyboardButton("DC3 وزن %", callback_data="set_dca3_alloc_pct"), InlineKeyboardButton("DC3 هبوط %", callback_data="set_dca3_drop_pct")],
                    [InlineKeyboardButton("عدد أوامر DCA", callback_data="set_dca_order_count"),
                     InlineKeyboardButton(f"{'تعطيل' if user.dca_enabled else 'تفعيل'} DCA", callback_data="settings_toggle_dca")],
                    [InlineKeyboardButton("⛔ استوب % (0 تعطيل)", callback_data="set_stop_loss_pct")],
                    [InlineKeyboardButton(f"{ui.I['back']}  القائمة", callback_data="menu")],
                ]),
            )
        finally:
            db.close()
        return

    if data == "settings_toggle_dca":
        db = SessionLocal()
        try:
            user = get_or_create_user(db, tid)
            user.dca_enabled = not bool(user.dca_enabled)
            db.commit()
            await query.edit_message_text(
                f"✅ DCA {'مفعّل' if user.dca_enabled else 'متوقف'}. إعادة التوازن العام ستستخدم هذا الاختيار.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع للإعدادات", callback_data="settings")]]),
            )
        finally:
            db.close()
        return

    if data.startswith("set_") and data in (
        "set_tp1_pct", "set_tp2_pct", "set_tp3_pct",
        "set_tp1_sell_pct", "set_tp2_sell_pct", "set_stop_loss_pct",
        "set_dca_buy_pct", "set_dca_order_count", "set_dca_range_pct",
        "set_dca1_alloc_pct", "set_dca2_alloc_pct", "set_dca3_alloc_pct",
        "set_dca1_drop_pct", "set_dca2_drop_pct", "set_dca3_drop_pct",
    ):
        field = data[len("set_"):]
        context.user_data["waiting"] = True
        context.user_data["edit_user_tpsl"] = field
        labels = {
            "tp1_pct": "هدف 1 % (مثال 8)",
            "tp2_pct": "هدف 2 % (مثال 15)",
            "tp3_pct": "هدف 3 % (0 لتعطيل الهدف الثالث)",
            "tp1_sell_pct": "نسبة البيع عند الهدف 1 (يفضل 40)",
            "tp2_sell_pct": "نسبة البيع عند الهدف 2 (يفضل 40)",
            "stop_loss_pct": "وقف الخسارة الابتدائي % (0 لتعطيله)",
            "dca_buy_pct": "نسبة المخصص للشراء الأولي % (من 10 إلى 90)",
            "dca_order_count": "عدد أوامر DCA (1 أو 2 أو 3)",
            "dca_range_pct": "نطاق هبوط DCA الإجمالي % (مثال 6)",
            "dca1_alloc_pct": "وزن ميزانية DC1 من احتياطي DCA %",
            "dca2_alloc_pct": "وزن ميزانية DC2 من احتياطي DCA %",
            "dca3_alloc_pct": "وزن ميزانية DC3 من احتياطي DCA %",
            "dca1_drop_pct": "نسبة هبوط DC1 تحت الدخول %",
            "dca2_drop_pct": "نسبة هبوط DC2 تحت الدخول %",
            "dca3_drop_pct": "نسبة هبوط DC3 تحت الدخول %",
        }
        await query.edit_message_text(
            f"أرسل قيمة *{labels.get(field, field)}*:\n(رقم بين 0 و 100)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ui.I['back']}  رجوع", callback_data="settings")],
            ]),
        )
        return

    if data == "cleanup_db":
        await _show_cleanup_scan(query, context, tid)
        return

    if data == "cleanup_confirm":
        if not context.user_data.get("cleanup_ready"):
            await query.edit_message_text(
                "⚠️ اعمل فحص أولاً من زر التنظيف.",
                reply_markup=main_menu_keyboard(),
            )
            return
        await _do_cleanup(query, context, tid)
        return

    if data.startswith("monitor_health_"):
        pf_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            await query.edit_message_text(
                _format_monitor_health(p),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 تحديث الحالة", callback_data=f"monitor_health_{pf_id}")],
                    [InlineKeyboardButton("↩ رجوع للمحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("view_") and not data.startswith("view_src_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            coins = [c.symbol for c in p.coins]
            current_value = None
            prices = {}
            if coins:
                try:
                    val = get_mexc().get_coins_value(coins)
                    current_value = float(val.get("total_usdt") or 0)
                    assets = val.get("assets") or {}
                    for sym, row in assets.items():
                        try:
                            prices[sym] = float(row.get("price") or 0)
                        except Exception:
                            pass
                except Exception:
                    current_value = None
                if not prices:
                    try:
                        prices = get_mexc().get_all_prices(coins) or {}
                    except Exception:
                        prices = {}
            events = []
            try:
                events = get_portfolio_trade_events(db, p.id, tid) or []
            except Exception:
                events = []
            await query.edit_message_text(
                format_pf(p, current_value=current_value, prices=prices, events=events),
                parse_mode="Markdown",
                reply_markup=pf_keyboard(
                    p.id, p.is_running,
                ),
            )
        finally:
            db.close()
        return

    # ——— خبراء داخل المحفظة ———
    if data.startswith("pf_tpsl_"):
        pf_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if not pf:
                await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
                return
            user = get_or_create_user(db, tid)
            def show(v, default):
                return f"`{v}`" if v is not None and float(v) > 0 else f"`{default}` (عام)"
            t1 = getattr(pf, "tp1_pct", None)
            t2 = getattr(pf, "tp2_pct", None)
            t3 = getattr(pf, "tp3_pct", None)
            s1 = getattr(pf, "tp1_sell_pct", None)
            s2 = getattr(pf, "tp2_sell_pct", None)
            sl = getattr(pf, "stop_loss_pct", None)
            ut1 = getattr(user, "tp1_pct", 3.0) or 3.0
            ut2 = getattr(user, "tp2_pct", 5.0) or 5.0
            ut3 = getattr(user, "tp3_pct", 8.0) or 8.0
            us1 = getattr(user, "tp1_sell_pct", 40.0) or 40.0
            us2 = getattr(user, "tp2_sell_pct", 30.0) or 30.0
            usl = getattr(user, "stop_loss_pct", 3.0)
            if usl is None:
                usl = 3.0
            msg = (
                f"🎯 *أهداف المحفظة:* {pf.name}\n\n"
                f"TP1: {show(t1, ut1)}% | بيع: {show(s1, us1)}%\n"
                f"TP2: {show(t2, ut2)}% | بيع: {show(s2, us2)}%\n"
                f"المتبقي بعد TP2: 20% يعمل بوقف متحرك\n"
                f"استوب: {'معطّل' if (sl is None and float(usl) == 0) or (sl is not None and float(sl) == 0) else show(sl, usl) + '%'}\n\n"
                f"_لو فاضية = تستخدم الإعدادات العامة_"
            )
            await query.edit_message_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◎ TP1 %", callback_data=f"pftpsl_{pf_id}_tp1_pct"),
                     InlineKeyboardButton("↘ بيع 1 %", callback_data=f"pftpsl_{pf_id}_tp1_sell_pct")],
                    [InlineKeyboardButton("◎ TP2 %", callback_data=f"pftpsl_{pf_id}_tp2_pct"),
                     InlineKeyboardButton("↘ بيع 2 %", callback_data=f"pftpsl_{pf_id}_tp2_sell_pct")],
                    [InlineKeyboardButton("⛔ استوب %", callback_data=f"pftpsl_{pf_id}_stop_loss_pct")],
                    [InlineKeyboardButton("× امسح تخصيص المحفظة", callback_data=f"pftpsl_clear_{pf_id}")],
                    [InlineKeyboardButton(f"{ui.I['back']}  رجوع", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("pftpsl_clear_"):
        pf_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if pf:
                for f in ("tp1_pct", "tp2_pct", "tp3_pct", "tp1_sell_pct", "tp2_sell_pct", "stop_loss_pct"):
                    setattr(pf, f, None)
                db.commit()
            await query.edit_message_text(
                "✅ تم مسح تخصيص المحفظة — هتستخدم الإعدادات العامة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{ui.I['back']}  رجوع", callback_data=f"pf_tpsl_{pf_id}")]]),
            )
        finally:
            db.close()
        return

    if data.startswith("pftpsl_") and not data.startswith("pftpsl_clear_"):
        # pftpsl_{id}_{field}
        parts = data.split("_", 2)
        # data = pftpsl_12_tp1_pct  -> need careful parse
        rest = data[len("pftpsl_"):]  # 12_tp1_pct
        pf_id_str, field = rest.split("_", 1)
        pf_id = int(pf_id_str)
        context.user_data["waiting"] = True
        context.user_data["edit_pf_tpsl"] = {"pf_id": pf_id, "field": field}
        labels = {
            "tp1_pct": "هدف 1 % (مثال 8)",
            "tp2_pct": "هدف 2 % (مثال 15)",
            "tp1_sell_pct": "نسبة البيع عند الهدف 1 (يفضل 40)",
            "tp2_sell_pct": "نسبة البيع عند الهدف 2 (يفضل 40)",
            "stop_loss_pct": "وقف الخسارة %",
        }
        await query.edit_message_text(
            f"أرسل قيمة *{labels.get(field, field)}* لهذه المحفظة:\n(أو `0` لمسح التخصيص واستخدام العام)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{ui.I['back']}  رجوع", callback_data=f"pf_tpsl_{pf_id}")]]),
        )
        return
    if data.startswith("edit_amount_") or data.startswith("increase_"):
        pf_id = int(data.split("_")[2] if data.startswith("edit_amount_") else data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            current = float(p.investment_usdt or 0) if p else 0.0
        finally:
            db.close()
        await query.edit_message_text(
            f"💰 *تعديل المبلغ المخصص*\n\nالمبلغ الحالي: `{current:.2f}` USDT\n\n"
            "أرسل إجمالي ميزانية المحفظة الجديدة، وليس مبلغ زيادة.\n"
            "لن يتم شراء أو بيع أي شيء بمجرد الحفظ. استخدم *إعادة بناء* للمطابقة.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
            ]),
        )
        return
    if data.startswith("trim_menu_"):
        pf_id = int(data.rsplit("_", 1)[1])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if not pf:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            held = sum(1 for c in (pf.coins or []) if position_remaining(c) > 0)
            await query.edit_message_text(
                f"✂️ *تخفيف الحيازة — {pf.name}*\n\n"
                f"المراكز المسجلة بكمية متبقية: `{held}`\n"
                "النسبة تُطبّق على الكمية المتبقية المسجلة لكل عملة داخل هذه المحفظة فقط. "
                "قبل البيع سيحاول البوت إلغاء أوامر TP التابعة للمركز ثم يعيد تنظيم أهداف الكمية المتبقية.\n\n"
                "حماية مهمة: سيتم تخطي العملة إذا كانت مشتركة مع محفظة أخرى عاملة، أو إذا كانت قيمة البيع أقل من 2 USDT، أو تعذر التحقق من إلغاء الأمر.",
                parse_mode="Markdown",
                reply_markup=trim_pct_keyboard(pf_id),
            )
        finally:
            db.close()
        return

    if data.startswith("restore_trim_menu_"):
        pf_id = int(data.rsplit("_", 1)[1])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if not pf:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            outstanding = _outstanding_trim_amounts(db, pf_id, tid)
            lines = []
            coins_by_id = {int(c.id): c for c in (pf.coins or [])}
            for key, data_row in outstanding.items():
                coin = coins_by_id.get(key[0]) if key[0] is not None else None
                symbol = coin.symbol if coin else data_row["symbol"]
                lines.append(f"• `{symbol}`: كمية متاحة للاسترجاع `{data_row['amount']:.8g}`")
            if not lines:
                await query.edit_message_text(
                    f"↩ *استرجاع التخفيف — {pf.name}*\n\nلا توجد كميات مخفّضة مسجلة وغير مسترجعة في سجل هذه المحفظة.",
                    parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, bool(pf.is_running)),
                )
                return
            await query.edit_message_text(
                f"↩ *استرجاع التخفيف — {pf.name}*\n\n"
                "الكمية المتاحة مأخوذة من سجل عمليات التخفيف الفعلية، مطروحًا منها ما تم شراؤه للاسترجاع سابقًا.\n"
                "اختر النسبة من الكمية التي ما زالت غير مسترجعة. الاسترجاع عملية شراء فعلية بسعر السوق، وليس استعادة لسعر البيع القديم.\n\n"
                + "\n".join(lines[:25])
                + "\n\nلن ينفذ أمرًا أقل من 2 USDT أو أمرًا لا يكفي رصيد USDT لتمويله.",
                parse_mode="Markdown", reply_markup=restore_trim_pct_keyboard(pf_id),
            )
        finally:
            db.close()
        return

    if data.startswith("restore_select_"):
        parts = data.split("_")
        pf_id, pct = int(parts[2]), int(parts[3])
        if pct not in (25, 50, 70, 100):
            await query.edit_message_text("نسبة استرجاع غير مسموحة.", reply_markup=main_menu_keyboard())
            return
        await query.edit_message_text(
            f"⚠️ *تأكيد استرجاع {pct}% من الكميات المخفّضة*\n\n"
            "سيشتري البوت الكميات المؤهلة بسعر السوق باستخدام رصيد USDT المتاح.\n"
            "لن يعيد الكمية التي سبق استرجاعها، ولن يرسل أوامر تقل قيمتها عن 2 USDT. إذا كانت المحفظة تعمل، فسيحاول إعادة ضبط أوامر TP بعد تحديث الكمية المسجلة.\n\nهل تريد المتابعة؟",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ تأكيد استرجاع {pct}%", callback_data=f"restore_execute_{pf_id}_{pct}")],
                [InlineKeyboardButton("× إلغاء", callback_data=f"restore_trim_menu_{pf_id}")],
            ]),
        )
        return

    if data.startswith("restore_execute_"):
        parts = data.split("_")
        await _do_restore_trimmed(query, tid, int(parts[2]), int(parts[3]))
        return

    if data.startswith("trim_select_"):
        parts = data.split("_")
        pf_id, pct = int(parts[2]), int(parts[3])
        if pct not in (25, 50, 70):
            await query.edit_message_text("نسبة غير مسموحة.", reply_markup=main_menu_keyboard())
            return
        await query.edit_message_text(
            f"⚠️ *تأكيد تخفيف الحيازة بنسبة {pct}%*\n\n"
            "سيُرسل البوت أوامر بيع سوق للعملات المؤهلة في هذه المحفظة فقط. "
            "قد تُتخطى العملة إذا كانت مشتركة مع محفظة أخرى أو كان الأمر أقل من حد المنصة.\n"
            "لن يتم إيقاف المحفظة، وسيُعاد ضبط أوامر TP للمقدار المتبقي إذا أمكن.\n\n"
            "هل تريد التنفيذ؟",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ تأكيد بيع {pct}%", callback_data=f"trim_execute_{pf_id}_{pct}")],
                [InlineKeyboardButton("× إلغاء", callback_data=f"trim_menu_{pf_id}")],
            ]),
        )
        return

    if data.startswith("trim_execute_"):
        parts = data.split("_")
        await _do_trim_holdings(query, tid, int(parts[2]), int(parts[3]))
        return

    if data == "start_all":
        await query.edit_message_text(
            "▶️ *تشغيل كل المحافظ*\n\n"
            "اختر نسبة المخصص التي تريد استخدامها لشراء العملات الناقصة في كل محفظة.\n"
            "الأرصدة الموجودة بالفعل لن يتم شراؤها من جديد. الحد الأدنى للأمر 2 USDT؛ إذا لم تكفِ النسبة لشراء كل العملات، سيشتري البوت فقط عدد الأوامر التي تستوفي الحد الأدنى ويعرض ما تم تخطيه.",
            parse_mode="Markdown",
            reply_markup=all_portfolios_entry_pct_keyboard(),
        )
        return

    if data.startswith("start_all_pct_"):
        pct = int(data.rsplit("_", 1)[1])
        if pct not in (25, 50, 70, 100):
            await query.edit_message_text("نسبة تشغيل غير مسموحة.", reply_markup=main_menu_keyboard())
            return
        await _do_start_all(query, tid, entry_pct=pct)
        return

    if data.startswith("start_pf_pct_"):
        parts = data.split("_")
        pf_id, pct = int(parts[3]), int(parts[4])
        if pct not in (25, 50, 70, 100):
            await query.edit_message_text("نسبة تشغيل غير مسموحة.", reply_markup=main_menu_keyboard())
            return
        await _do_start(query, tid, pf_id, entry_pct=pct)
        return
    if data == "stop_all_confirm":
        await query.edit_message_text(
            "⚠️ *إيقاف كل المحافظ*\n\nسيتم إيقاف المحافظ العاملة وإلغاء أوامر البيع وبيع المراكز بسعر السوق.\nهل تريد المتابعة؟",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏹ نعم، إيقاف الكل", callback_data="stop_all")],
                [InlineKeyboardButton("× إلغاء", callback_data="list_pf")],
            ]),
        )
        return
    if data == "stop_all":
        await _do_stop_all(query, tid)
        return
    if data.startswith("addcoin_"):
        context.user_data["addcoin_pf"] = int(data.split("_")[1])
        context.user_data["waiting"] = True
        await query.edit_message_text("أرسل رمز العملة (مثال: BTC أو ETH):")
        return ADD_COIN
    if data.startswith("removecoin_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p or not p.coins:
                await query.edit_message_text("لا توجد عملات.", reply_markup=main_menu_keyboard())
                return
            buttons = [[InlineKeyboardButton(f"حذف {c.symbol}", callback_data=f"delcoin_{pf_id}_{c.symbol}")] for c in p.coins]
            buttons.append([InlineKeyboardButton(f"{ui.I['back']}  رجوع", callback_data=f"view_{pf_id}")])
            await query.edit_message_text("اختر العملة للحذف:", reply_markup=InlineKeyboardMarkup(buttons))
        finally:
            db.close()
        return
    if data.startswith("delcoin_"):
        parts = data.split("_")
        pf_id, symbol = int(parts[1]), parts[2].upper()
        await query.edit_message_text(
            f"⚠️ *اختر مصير العملة {symbol}*\n\n"
            "هل تريد بيع رصيدها بسعر السوق ثم حذفها نهائيًا من خطة المحفظة، أم بيع الرصيد مع إبقاء العملة داخل الخطة؟",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 بيع الرصيد وحذف العملة نهائيًا", callback_data=f"coin_sell_delete_{pf_id}_{symbol}")],
                [InlineKeyboardButton("💵 بيع الرصيد مع إبقاء العملة", callback_data=f"coin_sell_keep_{pf_id}_{symbol}")],
                [InlineKeyboardButton("× إلغاء", callback_data=f"view_{pf_id}")],
            ]),
        )
        return
    if data.startswith(("coin_sell_delete_", "coin_sell_keep_")):
        parts = data.split("_")
        # coin_sell_{delete|keep}_{portfolio_id}_{symbol}
        action, pf_id, symbol = parts[2], int(parts[3]), parts[4].upper()
        await _do_remove_coin(query, tid, pf_id, symbol, delete_after_sell=(action == "delete"))
        return
    if data.startswith("close_"):
        pf_id = int(data.split("_")[1])
        await query.edit_message_text(
            "⚠️ هل أنت متأكد من حذف المحفظة؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ui.I['delete']}  نعم، احذفها", callback_data=f"confirm_close_{pf_id}")],
                [InlineKeyboardButton("× إلغاء", callback_data=f"view_{pf_id}")],
            ])
        )
        return
    if data.startswith("confirm_close_"):
        await _do_close(query, tid, int(data.split("_")[2]))
        return

    if data.startswith("toggle_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            portfolio = get_portfolio(db, pf_id, tid)
            if not portfolio:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            is_running = bool(portfolio.is_running)
        finally:
            db.close()
        if is_running:
            await _do_stop(query, tid, pf_id)
        else:
            db = SessionLocal()
            try:
                p = get_portfolio(db, pf_id, tid)
                name = p.name if p else f"#{pf_id}"
                amount = float(p.investment_usdt or 0) if p else 0.0
                strategy = normalize_entry_strategy(getattr(p, "entry_strategy", "tp_sl")) if p else "tp_sl"
                strategy_label = portfolio_strategy_label(p) if p else "أهداف ووقف خسارة فقط"
            finally:
                db.close()
            if strategy == "tp_sl_dca":
                await query.edit_message_text(
                    f"▶️ *تشغيل محفظة {name}*\n\nالمخصص: `{amount:.2f}` USDT\n"
                    f"النظام المحفوظ: *{strategy_label}*\n"
                    "سيطبّق البوت نسبة الشراء والاحتياطي وأوامر DCA المحفوظة عند التشغيل.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ تشغيل حسب إعدادات المحفظة", callback_data=f"start_pf_pct_{pf_id}_100")],
                        [InlineKeyboardButton("× إلغاء", callback_data=f"view_{pf_id}")],
                    ]),
                )
            else:
                await query.edit_message_text(
                    f"▶️ *تشغيل محفظة {name}*\n\n"
                    f"المخصص المسجل: `{amount:.2f}` USDT\n"
                    "سيستخدم النظام كامل المخصص للشراء، مع تجاهل أي نسبة دخول قديمة محفوظة في زر التشغيل.\n"
                    "الحد الأدنى للشراء 2 USDT لكل أمر؛ أي عملة لا تكفي حصتها لهذا الحد قد تُتخطى بدلًا من زيادة الميزانية تلقائيًا.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ تشغيل باستخدام 100% من المخصص", callback_data=f"start_pf_pct_{pf_id}_100")],
                        [InlineKeyboardButton("× إلغاء", callback_data=f"view_{pf_id}")],
                    ]),
                )
        return

    if data == "emergency_stop_all_confirm":
        await query.edit_message_text(
            ui.page_title(ui.I["danger"], "إيقاف طوارئ", "سيتم إيقاف جميع المحافظ العاملة.") + "\n"
            "سيتم إيقاف كل المحافظ العاملة، إلغاء أوامر البيع، وبيع الأرصدة بسعر السوق.\n"
            "ستبقى المحافظ وبياناتها محفوظة، ولن يتم حذفها.\n\n"
            "هل تريد المتابعة؟",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ui.I['danger']}  نعم، أوقف الكل الآن", callback_data="emergency_stop_all")],
                [InlineKeyboardButton("× إلغاء", callback_data="menu")],
            ]),
        )
        return

    if data == "emergency_stop_all":
        await _do_stop_all(query, tid)
        return

    if data.startswith("dca_manual_review_"):
        pf_id = int(data.rsplit("_", 1)[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            unresolved = db.query(PortfolioDcaOrder).filter(
                PortfolioDcaOrder.portfolio_id == pf_id,
                PortfolioDcaOrder.order_id.is_(None),
                PortfolioDcaOrder.status == "manual_review",
            ).order_by(PortfolioDcaOrder.symbol.asc(), PortfolioDcaOrder.level.asc(), PortfolioDcaOrder.id.asc()).all()
            if not unresolved:
                await query.edit_message_text(
                    "لا توجد سجلات DCA نشطة بلا رقم أمر لهذه المحفظة.",
                    reply_markup=pf_keyboard(pf_id, bool(p.is_running)),
                )
                return
            rows = []
            for row in unresolved[:15]:
                rows.append(
                    f"• {row.symbol} DC{int(row.level or 0)} | سجل #{row.id} | "
                    f"السعر {float(row.limit_price or 0):.8g} | "
                    f"مخصص {float(row.planned_usdt or 0):.2f} USDT | الحالة {str(row.status or '').replace('_', ' ')}"
                )
            if len(unresolved) > 15:
                rows.append(f"• وهناك {len(unresolved) - 15} سجلات أخرى")
            await query.edit_message_text(
                "🧾 *مراجعة سجلات DCA غير المرتبطة بأرقام أوامر*\n\n"
                + "\n".join(rows)
                + "\n\n*قبل التأكيد:* افتح MEXC Spot وافحص الأوامر المفتوحة وتاريخ الأوامر لكل عملة أعلاه. "
                + "ألغِ أي أمر شراء ما زال مفتوحًا وتأكد من عدم بقاء أمر محتمل مرتبط بهذه السجلات. "
                + "لو لم تستطع التحقق، لا تضغط تأكيد.\n\n"
                + "التأكيد لا يتصل بـ MEXC ولا يلغي شيئًا هناك؛ سيغيّر حالة السجلات المحلية فقط إلى `cancelled` "
                + "حتى يمكن إعادة المحاولة، لذلك لا تستخدمه إلا بعد مراجعتك اليدوية.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ أؤكد أني راجعت MEXC وألغيت أي أوامر مطابقة", callback_data=f"dca_manual_resolve_{pf_id}")],
                    [InlineKeyboardButton("× إلغاء والعودة للمحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("dca_manual_resolve_"):
        pf_id = int(data.rsplit("_", 1)[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            unresolved = db.query(PortfolioDcaOrder).filter(
                PortfolioDcaOrder.portfolio_id == pf_id,
                PortfolioDcaOrder.order_id.is_(None),
                PortfolioDcaOrder.status == "manual_review",
            ).all()
            if not unresolved:
                await query.edit_message_text(
                    "لا توجد سجلات تحتاج تسوية يدوية. لم يتم تغيير أي أمر على MEXC.",
                    reply_markup=pf_keyboard(pf_id, bool(p.is_running)),
                )
                return
            symbols = sorted({str(row.symbol).upper() for row in unresolved})
            row_ids = [int(row.id) for row in unresolved]
            for row in unresolved:
                row.status = "cancelled"
                row.updated_at = datetime.utcnow()
            log_action(
                db, tid, "manual_dca_reconciliation",
                f"Manual MEXC check portfolio={pf_id}; unresolved rows_count={len(row_ids)} ids={row_ids[:20]} symbols={symbols}. No exchange orders sent/cancelled by this action.",
                success=True, portfolio_id=pf_id,
            )
            # log_action commits; the preceding local row changes are included.
            await query.edit_message_text(
                f"✅ تمت تسوية {len(row_ids)} سجل DCA محليًا للعملات: {', '.join(symbols)}.\n\n"
                 "لم يُرسل البوت أي أمر إلى MEXC ولم يُلغِ أي أمر هناك. تم تحديث قاعدة البيانات فقط بناءً على تأكيدك اليدوي.\n"
                 "إذا تأكدت أن الأوامر المطابقة غير مفتوحة، اضغط إعادة التوازن وأعد المحاولة.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚖️ إعادة التوازن من جديد", callback_data=f"rebalance_{pf_id}")],
                    [InlineKeyboardButton("↩ رجوع للمحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("rebalance_confirm_"):
        pf_id = int(data.rsplit("_", 1)[1])
        db = SessionLocal()
        try:
            user = get_or_create_user(db, tid)
            strategy = "tp_sl_dca" if bool(user.dca_enabled) else "tp_sl"
            buy_pct = int(user.dca_buy_pct or 50)
            dca_count = int(user.dca_order_count or 2)
        finally:
            db.close()
        await _do_rebuild_positions(
            query, tid, pf_id, entry_strategy=strategy, dca_buy_pct=buy_pct,
            dca_order_count=dca_count, use_global_settings=True,
        )
        return
    if data.startswith("rebalance_") and not data.startswith(("rebalance_mode_", "rebalance_dca_")):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            name = p.name
            coin_count = len(p.coins or [])
        finally:
            db.close()
        await query.edit_message_text(
            f"⚠️ *تأكيد إعادة التوازن العامة: {name}*\n\n"
            "سيبدأ البوت دورة جديدة بعد التأكد من إلغاء أوامر DCA وTP القديمة بأمان.\n"
            "سيُصفّر حالات الشراء وDCA القديمة داخل البوت، ثم يفحص الأرصدة ويعيد بناء المراكز حسب الإعدادات الحالية.\n\n"
            "لن تُحذف سجلات الأرباح التاريخية، ولن تُمسح أي سجلات لأوامر غير محسومة إذا تعذر التأكد من حالتها.\n"
            "قد تُرسل أوامر شراء/بيع جديدة بعد اجتياز فحوص الأمان.\n\n"
            f"العملات: `{coin_count}`\nهل تريد المتابعة؟",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تأكيد وبدء دورة جديدة", callback_data=f"rebalance_confirm_{pf_id}")],
                [InlineKeyboardButton("× إلغاء", callback_data=f"view_{pf_id}")],
            ]),
        )
        return
    if data.startswith("rebuild_"):
        await _do_rebuild_positions(query, tid, int(data.split("_")[1]))
        return

    if data == "create_mode_tpsl":
        create_data = context.user_data.get("create") or {}
        if not create_data.get("coins"):
            await query.edit_message_text("انتهت جلسة إنشاء المحفظة. ابدأ من جديد.", reply_markup=main_menu_keyboard())
            return
        await _finish_create_portfolio(query, context, tid, "tp_sl", 100, 0)
        return
    # حذف المحفظة — الزر يبعت delete_pf_
    if data.startswith("delete_pf_"):
        pf_id = int(data.split("_")[-1])
        await query.edit_message_text(
            "⚠️ هل أنت متأكد من حذف المحفظة؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ui.I['delete']}  نعم، احذفها", callback_data=f"confirm_close_{pf_id}")],
                [InlineKeyboardButton("× إلغاء", callback_data=f"view_{pf_id}")],
            ]),
        )
        return

    # أهداف المحفظة (من زر التفاصيل الداخلي إن وُجد)
    if data.startswith("pf_tpsl_") and data.count("_") == 2:
        # already handled above as pf_tpsl_
        pass


@trading_guard.serialized
async def _do_rebuild_positions(query, tid, pf_id, entry_strategy=None, dca_buy_pct=None, dca_order_count=None, starting=False, use_global_settings=False):
    """دورة كاملة داخل المحفظة:
    1) إلغاء كل أوامر البيع القديمة من المنصة + مسحها من القاعدة
    2) التعرف على الرصيد الفعلي
    3) شراء العملات الناقصة
    4) إعادة وضع أهداف TP/SL الجديدة
    """
    rebuild_key = (int(tid), int(pf_id))
    if rebuild_key in _ACTIVE_PORTFOLIO_REBUILDS or rebuild_key in _ACTIVE_PORTFOLIO_TRIMS:
        await query.edit_message_text(
            "⏳ إعادة التوازن لهذه المحفظة قيد التنفيذ بالفعل؛ لم يتم إرسال أوامر إضافية.",
            reply_markup=pf_keyboard(pf_id, True),
        )
        return
    _ACTIVE_PORTFOLIO_REBUILDS.add(rebuild_key)
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not p.is_running and not starting:
            await query.edit_message_text(
                "⚠️ المحفظة متوقفة.\nشغّلها أولاً ثم استخدم *إعادة بناء المراكز*.",

                reply_markup=pf_keyboard(pf_id, False),
            )
            return
        if not p.coins:
            await query.edit_message_text(
                "لا توجد عملات في هذه المحفظة.",
                reply_markup=pf_keyboard(pf_id, True),
            )
            return

        symbols = [str(c.symbol).upper() for c in p.coins]
        shared_allocations = db.query(PortfolioCoin).join(Portfolio).filter(
            PortfolioCoin.symbol.in_(symbols),
            PortfolioCoin.portfolio_id != p.id,
            Portfolio.status == "active",
            PortfolioCoin.remaining_amount > 0,
        ).all()
        if shared_allocations:
            conflicts = sorted({str(c.symbol).upper() for c in shared_allocations})
            await query.edit_message_text(
                "⛔ أُوقفت إعادة البناء قبل إلغاء أي أمر أو تنفيذ تداول، لأن هذه العملات "
                "مخصصة أيضًا لمحافظ أخرى على حساب MEXC نفسه.\n"
                f"العملات: {', '.join(conflicts)}\n"
                "سوِّ المراكز المشتركة أولًا ثم أعد المحاولة.",
                reply_markup=pf_keyboard(pf_id, True),
            )
            return

        chosen_strategy = normalize_entry_strategy(entry_strategy or getattr(p, "entry_strategy", "tp_sl"))
        chosen_buy_pct = normalize_dca_buy_pct(
            dca_buy_pct if dca_buy_pct is not None else getattr(p, "dca_buy_pct", 50)
        ) if chosen_strategy == "tp_sl_dca" else 100
        chosen_order_count = normalize_dca_order_count(
            dca_order_count if dca_order_count is not None else getattr(p, "dca_order_count", 1)
        ) if chosen_strategy == "tp_sl_dca" else 1

        user = get_or_create_user(db, tid)
        if use_global_settings:
            chosen_strategy = "tp_sl_dca" if bool(user.dca_enabled) else "tp_sl"
            chosen_buy_pct = normalize_dca_buy_pct(user.dca_buy_pct) if chosen_strategy == "tp_sl_dca" else 100
            chosen_order_count = normalize_dca_order_count(user.dca_order_count) if chosen_strategy == "tp_sl_dca" else 1

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = _resolve_optional_pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolve_optional_pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolve_optional_pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _resolve_optional_pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _resolve_optional_pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        if use_global_settings:
            sl_pct = 3.0 if user.stop_loss_pct is None else max(0.0, float(user.stop_loss_pct))
        else:
            sl_raw = p.stop_loss_pct if p.stop_loss_pct is not None else user.stop_loss_pct
            sl_pct = max(0.0, float(3.0 if sl_raw is None else sl_raw))
        if use_global_settings:
            tp1 = float(user.tp1_pct if user.tp1_pct is not None else 3.0)
            tp2 = float(user.tp2_pct if user.tp2_pct is not None else 5.0)
            tp3 = float(user.tp3_pct if user.tp3_pct is not None else 8.0)
            s1 = float(user.tp1_sell_pct if user.tp1_sell_pct is not None else 40.0)
            s2 = float(user.tp2_sell_pct if user.tp2_sell_pct is not None else 40.0)
            p.tp1_pct, p.tp2_pct, p.tp3_pct = tp1, tp2, tp3
            p.tp1_sell_pct, p.tp2_sell_pct, p.stop_loss_pct = s1, s2, sl_pct
            p.dca_range_pct = float(user.dca_range_pct if user.dca_range_pct is not None else 6.0)
            for field, default in (("dca1_alloc_pct", 50.0), ("dca2_alloc_pct", 30.0), ("dca3_alloc_pct", 20.0), ("dca1_drop_pct", 3.0), ("dca2_drop_pct", 6.0), ("dca3_drop_pct", 10.0)):
                setattr(p, field, float(getattr(user, field, default) if getattr(user, field, None) is not None else default))

        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            + ui.SEP + "\n"
            "① إلغاء أوامر DCA والحماية القديمة...",

        )

        client = get_mexc()
        reb = get_reb()
        dca_cancel_errors = await _cancel_dca_orders(db, p, client=client)
        if dca_cancel_errors:
            await query.edit_message_text(
                "⚠️ تعذرت إعادة التوازن لأن بعض أوامر DCA لم تُلغَ/تُحسم بأمان:\n"
                + "\n".join(f"• {item}" for item in dca_cancel_errors[:6])
                + "\nلم أرسل أوامر إعادة التوازن. راجع MEXC ثم أعد المحاولة.\n\n"
                + "بعد فحص سجل الأوامر المفتوحة والتاريخ في MEXC وإلغاء أي أوامر شراء محتملة لهذه السجلات، "
                + "استخدم *مراجعة DCA يدويًا* أدناه. الزر لا يلغي أوامر على المنصة ولا يرسل تداولًا؛ "
                + "هو فقط يعلّم السجلات المحلية بأنها تمت مراجعتها بناءً على تأكيدك.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🧾 مراجعة DCA يدويًا", callback_data=f"dca_manual_review_{pf_id}")],
                    [InlineKeyboardButton("↩ رجوع للمحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return
        p.entry_strategy = chosen_strategy
        p.dca_buy_pct = chosen_buy_pct
        p.dca_order_count = chosen_order_count
        if use_global_settings:
            p.dca_range_pct = float(user.dca_range_pct if user.dca_range_pct is not None else 6.0)
            for field, default in (("dca1_alloc_pct", 50.0), ("dca2_alloc_pct", 30.0), ("dca3_alloc_pct", 20.0), ("dca1_drop_pct", 3.0), ("dca2_drop_pct", 6.0), ("dca3_drop_pct", 10.0)):
                setattr(p, field, float(getattr(user, field, default) if getattr(user, field, None) is not None else default))
        db.commit()
        symbols = [c.symbol for c in p.coins]
        cancelled_count = 0
        cancel_errors = []

        # 1) Cancel DB-known TP orders and legacy untracked portfolio sells.
        # Portfolio TP is code-monitored now; the portfolio owns sell orders for
        # its symbols during this operation, so stale exchange orders are removed.
        for coin in p.coins:
            known_ids = {
                str(getattr(coin, key))
                for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id")
                if getattr(coin, key, None)
            }
            try:
                res = reb.cancel_tp_orders([{
                    "symbol": coin.symbol,
                    "tp_order_id": getattr(coin, "tp_order_id", None),
                    "tp1_order_id": getattr(coin, "tp1_order_id", None),
                    "tp2_order_id": getattr(coin, "tp2_order_id", None),
                    "tp3_order_id": getattr(coin, "tp3_order_id", None),
                }])
                cancelled_count += len(res.get("cancelled") or [])
                for e in res.get("errors") or []:
                    cancel_errors.append(f"{coin.symbol}: {e.get('error', e)}")
            except Exception as exc:
                cancel_errors.append(f"{coin.symbol}: {exc}")
            try:
                for order in client.fetch_open_sell_orders(coin.symbol) or []:
                    order_id = str(order.get("id") or "")
                    if not order_id or order_id in known_ids:
                        continue
                    client.cancel_order(order_id, f"{coin.symbol}/{client.quote}", strict=False)
                    cancelled_count += 1
            except Exception as exc:
                cancel_errors.append(f"{coin.symbol}: تعذر تنظيف أمر بيع قديم غير مسجل: {exc}")
            # Keep every persisted ID until all cancellations across the portfolio
            # are confirmed. A partial cleanup must be safely retryable.

        if cancel_errors:
            await query.edit_message_text(
                "⚠️ لم تتم إعادة البناء لأن إلغاء بعض أوامر TP لم يُحسم بأمان:\n"
                + "\n".join(f"• {item}" for item in cancel_errors[:8])
                + "\nلم تُمسح معرّفات الأوامر أو حالات المراكز، ولم تُرسل أوامر إعادة التوازن. "
                "راجع أوامر MEXC ثم أعد المحاولة.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, True),
            )
            return

        # Only clear the IDs after the complete cancellation pass succeeded.
        for coin in p.coins:
            update_coin_position(
                db, coin.id,
                tp_order_id=None,
                tp1_order_id=None,
                tp2_order_id=None,
                tp3_order_id=None,
            )

        # All exchange orders relevant to the old cycle have now been reconciled
        # and cancellations verified. Clear the old DCA ledger rows so the new
        # cycle cannot inherit stale buy/DCA states. Keep PortfolioTrade as audit history.
        deleted_dca_rows = db.query(PortfolioDcaOrder).filter(
            PortfolioDcaOrder.portfolio_id == pf_id
        ).delete(synchronize_session=False)
        db.commit()

        # Rebalance begins a fresh cycle: reset active TP/SL/pending/re-entry state
        # while preserving realized-trade and profit history for audit/reporting.
        reset_portfolio_cycle_state(db, pf_id, invalidate_reentry=True)

        await asyncio.sleep(1.0)
        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            + ui.SEP + "\n"
            f"① أُلغي `{cancelled_count}` أمر قديم\n"
            "② فحص الرصيد الفعلي...",

        )

        # 2) انتظار قصير بعد الإلغاء ثم قراءة الرصيد من جديد
        await asyncio.sleep(1.5)
        presence = client.get_portfolio_presence(symbols)
        min_usdt = float(getattr(config, "BALANCE_PRESENCE_MIN_USDT", 1.0))
        allocated = float(p.investment_usdt or 0)
        n_coins = len(p.coins)
        strategy_budgets = calculate_strategy_budgets(
            allocated, p.entry_strategy, p.dca_buy_pct, n_coins, p.dca_order_count
        )
        initial_buy_budget = float(strategy_budgets["initial_budget"])
        dca_budget_target = max(0.0, float(strategy_budgets["dca_budget"]))
        configured_target_per_coin = initial_buy_budget / n_coins if n_coins > 0 else 0.0
        free_usdt = max(0.0, float(client.get_free_usdt() or 0.0))
        # Treat available USDT up to the configured DCA budget as reserved cash.
        # Sales may replenish the pool, but this reserve is never used for buys.
        # Protect this portfolio's target DCA reserve plus every other active
        # portfolio's outstanding DCA reserve on the same shared MEXC account.
        other_portfolio_dca_reserve = _cloud_dca_reserved_usdt(db)
        dca_cash_reserve = dca_budget_target + other_portfolio_dca_reserve
        rebalance_free_usdt = spendable_usdt_after_reserves(free_usdt, dca_cash_reserve)

        # First measure the real wallet value. If the configured budget is no
        # longer fully present, use the actual available pool instead of asking
        # for outside USDT. This lets adding a coin sell small portions of the
        # existing positions and redistribute the current value equally.
        current_pool_value = sum(
            float((presence.get(c.symbol) or presence.get(c.symbol.upper()) or {}).get("market_value") or 0)
            for c in p.coins
        )
        effective_pool = max(0.0, current_pool_value) + rebalance_free_usdt
        target_per_coin = (
            min(configured_target_per_coin, effective_pool / n_coins)
            if n_coins > 0 else 0.0
        )

        # Equalize against the real current pool: sell excess positions first,
        # then buy missing value with the released USDT.
        coin_status = []  # (coin, amount, price, market_value, need_buy, excess_sell)
        total_value = 0.0
        total_need = 0.0
        total_excess = 0.0
        for coin in p.coins:
            info = presence.get(coin.symbol) or presence.get(coin.symbol.upper()) or {}
            val = float(info.get("market_value") or 0)
            amt = float(info.get("amount") or 0)
            price = float(info.get("price") or 0)
            if amt <= 0 or val < min_usdt:
                val = 0.0
                amt = 0.0
            need = max(0.0, target_per_coin - val)
            excess = max(0.0, val - target_per_coin)
            if need < 1.0:
                need = 0.0
            if excess < 1.0:
                excess = 0.0
            total_value += val
            total_need += need
            total_excess += excess
            coin_status.append((coin, amt, price, val, need, excess))

        under = [x for x in coin_status if x[4] >= 1.0]
        overweight = [x for x in coin_status if x[5] >= 1.0]
        ok_coins = [x for x in coin_status if x[4] < 1.0 and x[5] < 1.0]

        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            + ui.SEP + "\n"
            f"① أُلغي `{cancelled_count}` أمر قديم\n"
            f"② هدف كل عملة: *`{target_per_coin:.2f}$`*  (مخصص `{allocated:.0f}$`)\n"
            f"القيمة الحالية: `{total_value:.2f}$`\n"
            f"ناقص: `{total_need:.2f}$` | زائد للبيع: `{total_excess:.2f}$`\n"
            f"USDT متاح: `{free_usdt:.2f}$`\n"
            f"تحت الهدف: `{len(under)}` | فوق الهدف: `{len(overweight)}` | متوازن: `{len(ok_coins)}`\n"
            "③ بيع الزائد ثم تكميل الناقص...",

        )

        sold_trim = []
        sell_errors = []
        sell_execution_uncertain = False
        total_sold_usdt = 0.0

        # 3أ) بيع الزائد حتى كل عملة ≈ الهدف (مايتخطاش المخصص للفرد)
        for coin, amt, price, val, need, excess in overweight:
            try:
                px = price if price > 0 else float(client.get_ticker_price(f"{coin.symbol}/{client.quote}") or 0)
                if px <= 0 or amt <= 0:
                    continue
                sell_amt = (excess / px) * 0.998
                free_amt = float(client.get_free_amount(coin.symbol) or 0)
                if free_amt < sell_amt * 0.5:
                    await asyncio.sleep(0.4)
                    free_amt = float(client.get_free_amount(coin.symbol) or 0)
                if free_amt <= 0:
                    sell_errors.append(f"{coin.symbol}: لا توجد كمية حرة مؤكدة للبيع")
                    continue
                sell_amt = min(sell_amt, free_amt * 0.998)
                if sell_amt * px < 1.0:
                    continue
                order = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda s=coin.symbol, a=sell_amt: client.create_market_order(
                        f"{s}/{client.quote}", "sell", a
                    ),
                )
                if order is None:
                    sell_errors.append(f"{coin.symbol}: غبار تحت الحد الأدنى")
                    continue
                filled_amt = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda o=order, a=sell_amt, s=coin.symbol: reb._confirm_market_sell(
                        f"{s}/{client.quote}", o, a
                    ),
                )
                if filled_amt <= 0:
                    sell_errors.append(f"{coin.symbol}: لم تتأكد كمية البيع المنفذة")
                    continue
                got = filled_amt * px
                total_sold_usdt += got
                sold_trim.append(f"{coin.symbol}(-{got:.1f}$)")
                new_amt = max(0.0, amt - filled_amt)
                update_coin_position(
                    db, coin.id,
                    amount=new_amt,
                    remaining_amount=new_amt,
                )
                logger.info(
                    "rebuild trim sell %s excess=%.2f sold_amt=%.6g",
                    coin.symbol, excess, sell_amt,
                )
            except Exception as exc:
                sell_errors.append(f"{coin.symbol}: {exc}")
                sell_execution_uncertain = True
                logger.exception("rebuild trim %s", coin.symbol)

        if sell_execution_uncertain:
            await query.edit_message_text(
                "⚠️ توقف إعادة التوازن لأن حالة بيع سوق غير محسومة. لم تُرسل مشتريات لاحقة؛ "
                "افحص أوامر MEXC والرصيد قبل إعادة المحاولة.\n\n"
                + "\n".join(f"• {item}" for item in sell_errors[:6]),
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, True),
            )
            return

        # حدّث الحضور بعد البيع
        await asyncio.sleep(1.0)
        presence = client.get_portfolio_presence(symbols)
        free_usdt = max(0.0, float(client.get_free_usdt() or 0.0))
        rebalance_free_usdt = spendable_usdt_after_reserves(free_usdt, dca_cash_reserve)

        # أعد حساب الناقص بعد التشذيب
        under = []
        for coin in p.coins:
            info = presence.get(coin.symbol) or presence.get(coin.symbol.upper()) or {}
            val = float(info.get("market_value") or 0)
            amt = float(info.get("amount") or 0)
            price = float(info.get("price") or 0)
            if amt <= 0 or val < min_usdt:
                val = 0.0
                amt = 0.0
            need = max(0.0, target_per_coin - val)
            if need < 1.0:
                need = 0.0
            if need >= 1.0:
                under.append((coin, amt, price, val, need))

        # Small excesses can be scattered across many positions and each one
        # may be below the normal trim threshold. If that leaves the missing
        # coin unfunded, release the gap from the largest existing positions.
        required_gap = max(0.0, sum(item[4] for item in under) - rebalance_free_usdt)
        funding_sold_usdt = 0.0
        funding_sell_uncertain = False
        funding_free_before = rebalance_free_usdt
        if required_gap >= 1.0:
            under_symbols = {item[0].symbol for item in under}
            candidates = []
            for coin in p.coins:
                if coin.symbol in under_symbols:
                    continue
                info = presence.get(coin.symbol) or presence.get(coin.symbol.upper()) or {}
                val = float(info.get("market_value") or 0)
                price = float(info.get("price") or 0)
                if val >= 2.0 and price > 0:
                    candidates.append((val, coin, price))
            for val, coin, price in sorted(candidates, reverse=True, key=lambda item: item[0]):
                if required_gap < 1.0:
                    break
                sell_value = min(required_gap, max(0.0, val - 1.0))
                if sell_value < 1.0:
                    continue
                sell_amt = (sell_value / price) * 0.998
                try:
                    order = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda s=coin.symbol, a=sell_amt: client.create_market_order(
                            f"{s}/{client.quote}", "sell", a
                        ),
                    )
                    if order is None:
                        continue
                    filled = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda o=order, a=sell_amt, s=coin.symbol: reb._confirm_market_sell(
                            f"{s}/{client.quote}", o, a
                        ),
                    )
                    filled_value = min(sell_value, max(0.0, float(filled or 0)) * price)
                    if filled_value < 1.0:
                        sell_errors.append(f"{coin.symbol}: لم يتأكد تنفيذ البيع التمويلي")
                        continue
                    total_sold_usdt += filled_value
                    funding_sold_usdt += filled_value
                    sold_trim.append(f"{coin.symbol}(-{filled_value:.1f}$)")
                    required_gap -= filled_value
                except Exception as exc:
                    sell_errors.append(f"{coin.symbol}: {exc}")
                    funding_sell_uncertain = True
            if funding_sell_uncertain:
                await query.edit_message_text(
                    "⚠️ توقف إعادة التوازن لأن حالة بيع تمويلي غير محسومة. لم تُرسل مشتريات لاحقة؛ "
                    "افحص أوامر MEXC والرصيد قبل إعادة المحاولة.\n\n"
                    + "\n".join(f"• {item}" for item in sell_errors[:6]),
                    parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, True),
                )
                return
            if total_sold_usdt > 0:
                # MEXC may report the filled market order before the quote
                # balance is refreshed. Poll until the funding proceeds are
                # visible, rather than attempting the buy with stale USDT.
                expected_free = funding_free_before + funding_sold_usdt * 0.90
                for _ in range(10):
                    await asyncio.sleep(0.6)
                    free_usdt = max(0.0, float(client.get_free_usdt() or 0.0))
                    rebalance_free_usdt = spendable_usdt_after_reserves(free_usdt, dca_cash_reserve)
                    if funding_sold_usdt <= 0 or rebalance_free_usdt >= expected_free:
                        break
                presence = client.get_portfolio_presence(symbols)
                under = []
                for coin in p.coins:
                    info = presence.get(coin.symbol) or presence.get(coin.symbol.upper()) or {}
                    val = float(info.get("market_value") or 0)
                    amt = float(info.get("amount") or 0)
                    price = float(info.get("price") or 0)
                    if amt <= 0 or val < min_usdt:
                        val = 0.0
                        amt = 0.0
                    need = max(0.0, target_per_coin - val)
                    if need >= 1.0:
                        under.append((coin, amt, price, val, need))

        # 3ب) تكميل الناقص — لا نتجاوز المخصص الإجمالي تقريبًا
        bought = []
        topped = []
        buy_errors = []
        total_spent = 0.0
        # سقف الإنفاق: المخصص - القيمة الحالية بعد البيع + هامش صغير
        try:
            cur_after = sum(
                float((presence.get(c.symbol) or presence.get(c.symbol.upper()) or {}).get("market_value") or 0)
                for c in p.coins
            )
        except Exception:
            cur_after = total_value - total_sold_usdt
        # Selling excess can change the available pool. Recompute the equal
        # share now, otherwise a missing coin can still use the old nominal
        # target and incorrectly report that extra USDT is required.
        post_sale_pool = max(0.0, cur_after) + max(0.0, rebalance_free_usdt)
        post_sale_target = (
            min(configured_target_per_coin, post_sale_pool / n_coins)
            if n_coins > 0 else 0.0
        )
        if post_sale_target < target_per_coin:
            target_per_coin = post_sale_target
            under = []
            for coin in p.coins:
                info = presence.get(coin.symbol) or presence.get(coin.symbol.upper()) or {}
                val = float(info.get("market_value") or 0)
                amt = float(info.get("amount") or 0)
                price = float(info.get("price") or 0)
                if amt <= 0 or val < min_usdt:
                    val = 0.0
                    amt = 0.0
                need = max(0.0, target_per_coin - val)
                if need >= 1.0:
                    under.append((coin, amt, price, val, need))
        # All proceeds from the funding sells are available for the missing
        # coins. The previous 10% multiplier stranded most of the released
        # cash and caused false "USDT available" errors.
        budget_left = max(0.0, initial_buy_budget - cur_after) + total_sold_usdt

        # Fund the largest shortfall first; otherwise earlier small purchases
        # can consume the released USDT and leave the newly added coin last.
        under.sort(key=lambda item: item[4], reverse=True)
        for coin, amt, price, val, need in under:
            # حدّث الرصيد الحر قبل كل شراء
            free_usdt = max(0.0, float(client.get_free_usdt() or 0.0))
            rebalance_free_usdt = spendable_usdt_after_reserves(free_usdt, dca_cash_reserve)
            buy_amt = min(need, rebalance_free_usdt * 0.995)
            if budget_left > 0:
                buy_amt = min(buy_amt, budget_left)
            if buy_amt < 1.0:
                buy_errors.append(
                    f"{coin.symbol}: يحتاج `{need:.2f}$` لكن USDT المتاح بعد احتياطي DCA `{rebalance_free_usdt:.2f}$`"
                )
                continue
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda c=coin, a=buy_amt: reb.reentry_buy_and_place_tp(
                        c.symbol, a, tp1, tp2, tp3, sl_pct, s1, s2,
                        control_mode="manual",
                    ),
                )
                if result.get("error"):
                    buy_errors.append(f"{coin.symbol}: {result.get('warning') or result.get('error')}")
                    continue
                entry = float(result.get("entry_price") or 0)
                amount = float(result.get("amount") or result.get("remaining_amount") or 0)
                # بعد الشراء: الكمية الكلية ≈ القديمة + الجديدة
                new_total_amt = max(amount, client.get_total_amount(coin.symbol) * 0.998)
                update_coin_position(
                    db, coin.id,
                    entry_price=entry if entry > 0 else (price or coin.entry_price or 0),
                    amount=new_total_amt,
                    remaining_amount=new_total_amt,
                    tp1_price=result.get("tp1_price", 0),
                    tp2_price=result.get("tp2_price", 0),
                    tp3_price=result.get("tp3_price", 0),
                    tp_price=result.get("tp1_price", 0),
                    current_sl_price=result.get("sl_price", 0),
                    original_sl_price=result.get("original_sl_price", 0),
                    tp1_order_id=result.get("tp1_order_id"),
                    tp2_order_id=result.get("tp2_order_id"),
                    tp3_order_id=result.get("tp3_order_id"),
                    position_status="open",
                    reentry_used=False,
                    reentry_touched=False,
                )
                mark_reentry_events_used(db, p.id, coin.symbol)
                total_spent += buy_amt
                budget_left = max(0.0, budget_left - buy_amt)
                if val < min_usdt:
                    bought.append(coin.symbol)
                else:
                    topped.append(f"{coin.symbol}(+{buy_amt:.1f}$)")
            except Exception as exc:
                buy_errors.append(f"{coin.symbol}: {exc}")
            await asyncio.sleep(0.35)

        # 4) إعادة وضع الأهداف للعملات المكتملة مسبقاً
        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            + ui.SEP + "\n"
            f"① أُلغي `{cancelled_count}` أمر\n"
            f"② هدف كل عملة: `{target_per_coin:.2f}$`\n"
            f"③ اشتريت: `{len(bought)}` | كمّلت: `{len(topped)}` | صُرف: `{total_spent:.2f}$`\n"
            "④ إعادة وضع الأهداف للباقي...",

        )

        refreshed = list(bought)  # already have TP from reentry_buy
        refresh_errors = []
        already_done = set(bought) | {t.split("(")[0] for t in topped}

        refresh_total = len(coin_status)
        refresh_done = 0
        loop = asyncio.get_running_loop()
        for coin, amt, price, val, need, excess in coin_status:
            refresh_done += 1
            await query.edit_message_text(
                f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
                + ui.SEP + "\n"
                f"① أُلغي `{cancelled_count}` أمر\n"
                f"② تم شراء/تكميل `{len(bought) + len(topped)}` عملة\n"
                f"③ جاري تجهيز الحماية: `{refresh_done}/{refresh_total}` عملة\n"
                f"④ العملة الحالية: `{coin.symbol}` — تجهيز TP والاستوب...",
                parse_mode="Markdown",
            )
            if coin.symbol in already_done:
                if coin.symbol not in refreshed:
                    refreshed.append(coin.symbol)
                if refresh_done % 5 == 0 or refresh_done == refresh_total:
                    await query.edit_message_text(
                        f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
                        + ui.SEP + "\n"
                        f"① أُلغي `{cancelled_count}` أمر\n"
                        f"② تم شراء/تكميل `{len(bought) + len(topped)}` عملة\n"
                        f"③ جاري تجهيز الحماية: `{refresh_done}/{refresh_total}` عملة\n"
                        "④ تجهيز أوامر TP على المنصة ومراجعة الاستوب...",
                        parse_mode="Markdown",
                    )
                continue
            try:
                def _refresh_one_coin():
                    free_amt = client.get_free_amount(coin.symbol)
                    use_amt = (free_amt if free_amt > 0 else amt) * 0.998
                    entry = float(coin.entry_price or 0)
                    if entry <= 0:
                        entry = price or client.get_ticker_price(f"{coin.symbol}/{client.quote}")
                    if use_amt <= 0 or entry <= 0:
                        return use_amt, entry, {"symbol": coin.symbol, "error": "لا يوجد رصيد كافٍ"}
                    result = reb.place_tp_orders(
                        [{
                            "symbol": coin.symbol,
                            "amount": use_amt,
                            "entry_price": entry,
                            "place_exchange_orders": False,
                        }],
                        tp1, tp2, tp3, sl_pct, s1, s2,
                        control_mode="manual",
                    )[0]
                    return use_amt, entry, result

                use_amt, entry, result = await loop.run_in_executor(None, _refresh_one_coin)
                if use_amt <= 0 or entry <= 0:
                    refresh_errors.append(f"{coin.symbol}: لا يوجد رصيد كافٍ")
                    continue
                if result.get("error"):
                    refresh_errors.append(f"{coin.symbol}: {result.get('warning') or result.get('error')}")
                update_coin_position(
                    db, coin.id,
                    entry_price=entry,
                    amount=use_amt,
                    remaining_amount=use_amt,
                    tp1_price=result.get("tp1_price", 0),
                    tp2_price=result.get("tp2_price", 0),
                    tp3_price=result.get("tp3_price", 0),
                    tp_price=result.get("tp1_price", 0),
                    current_sl_price=result.get("sl_price", 0),
                    original_sl_price=result.get("original_sl_price", 0),
                    tp1_order_id=result.get("tp1_order_id"),
                    tp2_order_id=result.get("tp2_order_id"),
                    tp3_order_id=result.get("tp3_order_id"),
                    position_status="open",
                    reentry_used=False,
                    reentry_touched=False,
                    reentry_price=0.0,
                )
                refreshed.append(coin.symbol)
            except Exception as exc:
                refresh_errors.append(f"{coin.symbol}: {exc}")
            if refresh_done % 5 == 0 or refresh_done == refresh_total:
                await query.edit_message_text(
                    f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
                    + ui.SEP + "\n"
                    f"① أُلغي `{cancelled_count}` أمر\n"
                    f"② تم شراء/تكميل `{len(bought) + len(topped)}` عملة\n"
                    f"③ جاري تجهيز الحماية: `{refresh_done}/{refresh_total}` عملة\n"
                    "④ تجهيز أوامر TP على المنصة ومراجعة الاستوب...",
                    parse_mode="Markdown",
                )

        # أساس حقيقي للحساب من الآن = قيمة المراكز بعد إعادة البناء
        try:
            from datetime import datetime as _dt
            final_value = 0.0
            for coin in p.coins:
                rem = position_remaining(coin)
                entry = float(coin.entry_price or 0)
                if rem > 0 and entry > 0:
                    try:
                        px = float(client.get_ticker_price(f"{coin.symbol}/{client.quote}") or entry)
                    except Exception:
                        px = entry
                    final_value += rem * px
            if final_value <= 0:
                final_value = float(total_value or p.investment_usdt or 0)
            p.base_investment = final_value
            p.last_rebalance = _dt.utcnow()
            db.commit()
        except Exception:
            logger.exception("failed to set rebuild baseline")

        dca_result = await _place_portfolio_dca_orders(db, p, client=client)
        if starting:
            # Only expose the new DCA orders to the live monitor after the initial
            # allocation and order setup have completed successfully.
            set_portfolio_running(db, pf_id, True)
            db.refresh(p)

        log_action(
            db, tid, "rebuild_positions",
            f"Rebuild {p.name}: target={target_per_coin:.2f} cancel={cancelled_count} "
            f"trim={sold_trim} buy={bought} top={topped} spent={total_spent:.2f} sold={total_sold_usdt:.2f}",
            not (buy_errors or refresh_errors),
            pf_id,
        )

        free_after = client.get_free_usdt()
        refreshed_symbols = {
            str(symbol).split("(", 1)[0].strip().upper()
            for symbol in refreshed
        }
        # Symbols entered from Telegram may be mixed-case (e.g. Gm), while
        # exchange/database rows are commonly uppercase (GM). Match in Python
        # so the detailed TP/SL section cannot disappear بسبب اختلاف الحالة.
        rebuilt_coins = [
            coin for coin in p.coins
            if str(coin.symbol or "").strip().upper() in refreshed_symbols
            and float(coin.remaining_amount or 0) > 0
        ] if refreshed_symbols else []
        # Final report: concise user-facing summary, not internal counters.
        if total_spent > 0 or total_sold_usdt > 0:
            actions = []
            if total_spent > 0:
                actions.append(f"شراء `{total_spent:.2f}$`")
            if total_sold_usdt > 0:
                actions.append(f"بيع `{total_sold_usdt:.2f}$`")
            action_summary = "تم تنفيذ: " + " | ".join(actions)
        else:
            action_summary = "لم يتم شراء أو بيع؛ تم تحديث خطة الحماية فقط"
        value_delta = total_value - target_per_coin * n_coins
        delta_text = f"أعلى من الهدف بـ `{value_delta:.2f}$`" if value_delta >= 0 else f"أقل من الهدف بـ `{abs(value_delta):.2f}$`"
        def _with_dividers(items):
            result = []
            for index, item in enumerate(items):
                result.append(item)
                if index < len(items) - 1:
                    result.append("────────────")
            return result

        lines = _with_dividers([
            f"✅ *تم تحديث محفظة* `{p.name}`",
            "🟡 الحالة: راجع تأكيد الأهداف والوقف في الخطة والملاحظات",
            f"💰 المخصص: *`{allocated:.2f}$`*",
            f"⚙️ النظام: *{portfolio_strategy_label(p)}*",
            f"🛒 ميزانية الشراء الأولي: *`{initial_buy_budget:.2f}$`*",
            f"📊 القيمة الحالية: *`{total_value:.2f}$`* — {delta_text}",
            f"💵 رصيد USDT المتاح بعد إعداد DCA: *`{free_after:.2f}$`*",
            f"🔧 النتيجة: {action_summary}",
            f"🎯 تم تحديث الأهداف والاستوب لـ `{len(refreshed)}` عملة",
        ])
        if chosen_strategy == "tp_sl_dca":
            lines.append(
                f"🧲 أوامر DCA: `{dca_result.get('placed', 0)}` موضوعة / ` {dca_result.get('skipped', 0)}` متخطاة؛ "
                f"ميزانية الاحتياطي الاسمية `{strategy_budgets['dca_budget']:.2f}$`"
            )
        if rebuilt_coins:
            lines.extend(["", "━━━━━━━━━━━━━━━━━━━━", "📋 *خطة التداول الحالية*", ""])
            plan_dca_rows = db.query(PortfolioDcaOrder).filter(
                PortfolioDcaOrder.portfolio_id == p.id,
                PortfolioDcaOrder.level > 0,
            ).all()
            lines.append("القيم USDT؛ TP تقديري عند الهدف؛ ? = أمر غير مؤكد؛ DC = ميزانية متبقية؛ 0 = لا مبلغ نشط")
            for coin in rebuilt_coins:
                lines.extend(f"`{line}`" for line in compact_coin_plan(
                    coin, s1, s2, plan_dca_rows, trailing_enabled=sl_pct > 0,
                ))
                lines.append("")
        all_errs = cancel_errors[:3] + sell_errors + buy_errors + refresh_errors + [item for item in (dca_result.get("messages") or []) if ": شراء Limit بوزن " not in str(item)]
        if all_errs:
            lines.append("\n⚠️ ملاحظات:")
            for e in all_errs[:10]:
                lines.append(f"• {e}")
            if any("USDT المتاح" in str(e) for e in buy_errors):
                lines.append(
                    "\n💡 تم توزيع القيمة الموجودة بالتساوي؛ لا يوجد شراء إضافي "
                    "لأن القيمة الحالية أقل من الميزانية الاسمية."
                )
        report_chunks = _split_telegram_text("\n".join(lines))
        # Edit the progress message with the first part, then send the rest;
        # Telegram rejects messages above its size limit.
        await query.edit_message_text(
            report_chunks[0],
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, True) if len(report_chunks) == 1 else None,
        )
        for index, chunk in enumerate(report_chunks[1:], start=1):
            await query.message.reply_text(
                chunk,
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, True) if index == len(report_chunks) - 1 else None,
            )
    except Exception as exc:
        logger.exception("rebuild_positions failed")
        await query.edit_message_text(
            f"❌ فشل إعادة البناء:\n{str(exc)[:900]}",
            reply_markup=pf_keyboard(pf_id, True),
        )
    finally:
        _ACTIVE_PORTFOLIO_REBUILDS.discard(rebuild_key)
        db.close()


async def _show_missing_reentry(query, context, tid, pf_id, toggle_symbol=None):
    """Show missing portfolio coins and let the user build a buy selection."""

    db = SessionLocal()
    try:
        pf = get_portfolio(db, pf_id, tid)
        if not pf:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return

        symbols = [c.symbol for c in pf.coins]
        if not symbols:
            await query.edit_message_text(
                "المحفظة لا تحتوي على عملات.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        loop = asyncio.get_event_loop()
        try:
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(symbols)
            )
        except Exception as exc:
            logger.exception("Portfolio missing-coin scan failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص الرصيد من MEXC.\n"
                "لم يتم اعتبار أي عملة ناقصة ولم يتم تنفيذ أي شراء.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['refresh']}  إعادة المحاولة", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        missing_symbols = [
            symbol for symbol in symbols
            if not presence.get(symbol, {}).get("present", False)
        ]
        key = _missing_selection_key(pf_id)
        selected = set(context.user_data.get(key, []))
        selected.intersection_update(missing_symbols)
        if not pf.is_running:
            selected.clear()
            context.user_data.pop(key, None)

        if toggle_symbol and pf.is_running:
            toggle_symbol = toggle_symbol.upper()
            if toggle_symbol in missing_symbols:
                if toggle_symbol in selected:
                    selected.remove(toggle_symbol)
                else:
                    selected.add(toggle_symbol)
            context.user_data[key] = sorted(selected)

        if not missing_symbols:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                f"✅ فحص *{pf.name}* مكتمل.\n"
                f"كل العملات المسجلة ({len(symbols)}) موجودة بقيمة سوقية فعلية "
                f"(أكبر من `{config.BALANCE_PRESENCE_MIN_USDT:g}` USDT).\n"
                "تم احتساب العملات الموجودة داخل أوامر البيع المفتوحة، "
                "وتجاهل بقايا البيع الصغيرة.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        present_count = len(symbols) - len(missing_symbols)
        lines = [
            f"🔎 *فحص العملات — {pf.name}*",
            "",
            f"المسجلة: `{len(symbols)}` | الموجودة: `{present_count}` | الناقصة: `{len(missing_symbols)}`",
            "",
            f"يتم تجاهل بقايا البيع الأقل من `{config.BALANCE_PRESENCE_MIN_USDT:g}` USDT.",
            (
                "اختر العملات التي تريد إعادة دخولها."
                if pf.is_running
                else "⚠️ المحفظة متوقفة؛ الفحص متاح للعرض فقط، ولن يظهر خيار شراء."
            ),
            "لا يوجد شراء عند الاختيار؛ الشراء لا يبدأ إلا بعد زر التأكيد.",
            "",
            "العملة المحددة: " + (
                ", ".join(f"`{symbol}`" for symbol in sorted(selected))
                if selected else "—"
            ),
        ]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_missing_reentry_keyboard(
                pf_id, missing_symbols, selected, allow_selection=pf.is_running
            ),
        )
    finally:
        db.close()


@trading_guard.serialized
async def _do_missing_reentry(query, context, tid, pf_id):
    """Re-check and buy only the coins explicitly confirmed by the user."""

    key = _missing_selection_key(pf_id)
    if context.user_data.get(f"{key}_in_progress"):
        await query.edit_message_text("⏳ إعادة الدخول قيد التنفيذ بالفعل.")
        return

    selected = set(context.user_data.get(key, []))
    if not selected:
        await query.edit_message_text(
            "اختر عملة واحدة على الأقل أولاً.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⌕ فحص العملات الناقصة", callback_data=f"missing_{pf_id}")],
                [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
            ]),
        )
        return

    db = SessionLocal()
    try:
        pf = get_portfolio(db, pf_id, tid)
        if not pf:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not pf.is_running:
            await query.edit_message_text(
                "⚠️ المحفظة أصبحت متوقفة. لم يتم تنفيذ أي شراء.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        coin_map = {coin.symbol.upper(): coin for coin in pf.coins}
        selected = {symbol.upper() for symbol in selected if symbol.upper() in coin_map}
        if not selected:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                "لم تعد هناك عملات صالحة لإعادة الدخول.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        # Re-check immediately before placing any order. A balance appearing
        # after the first scan must remove that coin from the buy list.
        loop = asyncio.get_event_loop()
        try:
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(list(coin_map))
            )
        except Exception as exc:
            logger.exception("Final portfolio missing-coin scan failed")
            await query.edit_message_text(
                "⚠️ تعذر إعادة فحص الرصيد قبل التنفيذ.\n"
                "تم إلغاء العملية بالكامل ولم يتم شراء أي عملة.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['refresh']}  إعادة الفحص", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        no_longer_missing = {
            symbol for symbol in selected
            if presence.get(symbol, {}).get("present", False)
        }
        selected -= no_longer_missing
        if not selected:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                "ℹ️ العملات المحددة أصبحت موجودة بالفعل في الرصيد.\n"
                "تم إلغاء الشراء.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        per_coin_usdt = max(
            5.0,
            float(pf.investment_usdt or 0) / max(1, len(pf.coins)),
        )
        required_usdt = per_coin_usdt * len(selected)
        try:
            free_usdt_raw = await loop.run_in_executor(None, get_mexc().get_free_usdt)
            free_usdt = spendable_usdt_after_reserves(
                free_usdt_raw, _cloud_dca_reserved_usdt(db)
            )
        except Exception as exc:
            logger.exception("Free USDT preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر التحقق من رصيد USDT الحر.\n"
                "تم إلغاء العملية ولم يتم شراء أي عملة.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['refresh']}  إعادة الفحص", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return
        if free_usdt < required_usdt:
            await query.edit_message_text(
                f"⚠️ رصيد USDT الحر غير كافٍ.\n"
                f"المتاح: `{free_usdt:.2f}` USDT\n"
                f"المطلوب تقريباً: `{required_usdt:.2f}` USDT\n\n"
                "تم إلغاء العملية ولم يتم شراء أي عملة.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        context.user_data[f"{key}_in_progress"] = True
        selected_symbols = sorted(selected)
        await query.edit_message_text(
            "⏳ تم التأكيد.\n"
            f"سيتم تنفيذ إعادة دخول لـ `{len(selected_symbols)}` عملة فقط:\n"
            + ", ".join(f"`{symbol}`" for symbol in selected_symbols),
            parse_mode="Markdown",
        )

        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = _resolve_optional_pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolve_optional_pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolve_optional_pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _resolve_optional_pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _resolve_optional_pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _resolve_stop_loss_pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        succeeded = []
        errors = []
        for symbol in selected_symbols:
            coin = coin_map[symbol]
            try:
                result = await loop.run_in_executor(
                    None,
            lambda symbol=symbol: get_reb().reentry_buy_and_place_tp(
                        symbol, per_coin_usdt, tp1, tp2, tp3, sl_pct, s1, s2,
                        control_mode="manual",
                    ),
                )
            except Exception as exc:
                logger.exception("Missing-coin re-entry failed for %s", symbol)
                errors.append(f"`{symbol}`: {exc}")
                continue

            if result.get("error"):
                errors.append(f"`{symbol}`: {result['error']}")
                continue

            update_coin_position(
                db, coin.id,
                entry_price=result.get("entry_price", 0),
                tp1_price=result.get("tp1_price", 0),
                tp2_price=result.get("tp2_price", 0),
                tp3_price=result.get("tp3_price", 0),
                tp_price=result.get("tp1_price", 0),
                current_sl_price=result.get("sl_price", 0),
                original_sl_price=result.get("original_sl_price") or result.get("sl_price", 0),
                amount=result.get("amount", 0),
                remaining_amount=result.get("amount", 0),
                tp1_order_id=result.get("tp1_order_id"),
                tp2_order_id=result.get("tp2_order_id"),
                tp3_order_id=result.get("tp3_order_id"),
                position_status="open",
                reentry_used=False,
                reentry_touched=False,
                reentry_price=0.0,
            )
            mark_reentry_events_used(db, pf.id, symbol)
            log_action(db, tid, "missing_coin_reentry", f"Re-entry for missing {symbol}", True, pf.id)
            success_line = f"`{symbol}` عند `{float(result.get('entry_price') or 0):.6g}`"
            if result.get("tp_warning"):
                success_line += f" (تحذير TP: {result['tp_warning']})"
            succeeded.append(success_line)

        context.user_data.pop(key, None)
        lines = ["🔄 *نتيجة إعادة الدخول*"]
        if succeeded:
            lines.append("\n✅ تم الشراء:")
            lines.extend(f"• {item}" for item in succeeded)
        if errors:
            lines.append("\n⚠️ لم يتم الشراء:")
            lines.extend(f"• {item}" for item in errors)
        lines.append("\nلم يتم تنفيذ أي عملة لم تكن محددة في شاشة التأكيد.")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf.id, pf.is_running),
        )
    finally:
        context.user_data.pop(f"{key}_in_progress", None)
        db.close()


@trading_guard.serialized
async def _do_manual_reentry(query, tid, event_id):
    """Buy a stopped coin again only after the user presses its button."""

    db = SessionLocal()
    try:
        event = get_trade_event(db, event_id, tid)
        if (
            not event
            or event.event_type != "stop_loss"
            or not bool(event.reentry_available)
            or bool(event.reentry_used)
        ):
            await query.edit_message_text("عملية إعادة الدخول غير متاحة أو تم تنفيذها مسبقاً.", reply_markup=main_menu_keyboard())
            return
        pf = get_portfolio(db, event.portfolio_id, tid)
        coin = next((c for c in (pf.coins if pf else []) if c.id == event.portfolio_coin_id), None)
        if not pf or not coin:
            await query.edit_message_text("المحفظة أو العملة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if str(coin.position_status or "") != "waiting_reentry":
            await query.edit_message_text(
                "عملية إعادة الدخول لم تعد صالحة لأن حالة المركز تغيّرت. لم يُرسل أي أمر شراء.",
                reply_markup=pf_keyboard(pf.id, pf.is_running),
            )
            return
        other_allocation = db.query(PortfolioCoin).join(Portfolio).filter(
            PortfolioCoin.symbol == coin.symbol,
            PortfolioCoin.portfolio_id != pf.id,
            Portfolio.status == "active",
            PortfolioCoin.remaining_amount > 0,
        ).first()
        if other_allocation:
            await query.edit_message_text(
                f"⛔ لم تتم إعادة دخول `{coin.symbol}` لأن العملة مخصصة أيضًا لمحفظة أخرى على حساب MEXC نفسه.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf.id}")],
                ]),
            )
            return
        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = _resolve_optional_pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolve_optional_pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolve_optional_pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _resolve_optional_pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _resolve_optional_pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _resolve_stop_loss_pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
        # A stop-loss re-entry may only reuse the approximate cash actually
        # released by that stop, never the portfolio's full equal allocation.
        usdt = max(0.0, float(event.amount or 0.0)) * max(0.0, float(event.exit_price or 0.0))
        if usdt <= 0:
            usdt = float(pf.investment_usdt or 0) / max(1, len(pf.coins))
        if usdt < float(config.PORTFOLIO_MIN_ORDER_USDT):
            await query.edit_message_text(
                f"⚠️ حصة إعادة الدخول `{usdt:.2f}$` أقل من الحد الأدنى للأمر "
                f"`{config.PORTFOLIO_MIN_ORDER_USDT:.2f}$`.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("المحفظة", callback_data=f"view_{pf.id}")],
                ]),
            )
            return

        free_raw = float(await asyncio.get_event_loop().run_in_executor(None, get_mexc().get_free_usdt) or 0.0)
        spendable = spendable_usdt_after_reserves(free_raw, _cloud_dca_reserved_usdt(db))
        if spendable + 1e-9 < usdt:
            await query.edit_message_text(
                f"⚠️ لم تتم إعادة الدخول لحماية احتياطي DCA. المتاح بعد الاحتياطي `{spendable:.2f}` USDT، والمطلوب `{usdt:.2f}`.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf.id}")]]),
            )
            return

        # Atomically consume the offer before any exchange call. This closes
        # double-click/replayed-callback races. If the exchange response later
        # becomes ambiguous, keep the coin in a review-only state instead of
        # risking a second automatic or manual buy.
        if not claim_reentry_event(db, event.id, tid):
            await query.edit_message_text(
                "عملية إعادة الدخول استُخدمت أو تغيّرت حالتها بالفعل. لم يُرسل أي أمر شراء.",
                reply_markup=pf_keyboard(pf.id, pf.is_running),
            )
            return
        update_coin_position(
            db,
            coin.id,
            position_status="reentry_review",
            pending_order_id=None,
            pending_order_side="buy",
            pending_order_usdt=usdt,
            pending_order_created_at=datetime.utcnow(),
        )
        await query.edit_message_text(f"⏳ جاري إعادة دخول `{coin.symbol}`...", parse_mode="Markdown")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: get_reb().reentry_buy_and_place_tp(
                coin.symbol, usdt, tp1, tp2, tp3, sl_pct, s1, s2,
                control_mode="manual",
            ),
        )
        if result.get("error"):
            await query.edit_message_text(
                f"⚠️ فشل إعادة دخول `{coin.symbol}`:\n`{result['error']}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⛔  الاستوبات / إعادة الدخول", callback_data=f"stopped_{pf.id}")],
                    [InlineKeyboardButton(f"{ui.I['back']}  المحفظة", callback_data=f"view_{pf.id}")],
                ]),
            )
            return

        update_coin_position(
            db, coin.id,
            entry_price=result.get("entry_price", 0),
            tp1_price=result.get("tp1_price", 0),
            tp2_price=result.get("tp2_price", 0),
            tp3_price=result.get("tp3_price", 0),
            tp_price=result.get("tp1_price", 0),
            current_sl_price=result.get("sl_price", 0),
            original_sl_price=result.get("original_sl_price") or result.get("sl_price", 0),
            amount=result.get("amount", 0),
            remaining_amount=result.get("amount", 0),
            tp1_order_id=result.get("tp1_order_id"),
            tp2_order_id=result.get("tp2_order_id"),
            tp3_order_id=result.get("tp3_order_id"),
            position_status="open",
            reentry_used=False,
            reentry_touched=False,
            reentry_price=0.0,
            pending_order_id=None,
            pending_order_side=None,
            pending_order_usdt=0.0,
            pending_order_created_at=None,
        )
        log_action(db, tid, "manual_reentry", f"Manual re-entry for {coin.symbol}", True, pf.id)
        await query.edit_message_text(
            f"✅ تمت إعادة دخول `{coin.symbol}` في محفظة *{pf.name}*\n"
            f"الدخول `{result.get('entry_price', 0):.6g}` | "
            f"TP1 `{result.get('tp1_price', 0):.6g}` | "
            f"TP2 `{result.get('tp2_price', 0):.6g}` | "
            f"TP3 `{result.get('tp3_price', 0):.6g}`\n"
            f"SL `{result.get('sl_price', 0):.6g}`",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf.id, pf.is_running),
        )
    finally:
        db.close()


@trading_guard.serialized
async def _do_start(query, tid, pf_id, entry_pct=100):

    operation_key = (int(tid), int(pf_id))
    operation_locked = False
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        if not coins:
            await query.edit_message_text("المحفظة بدون عملات.", reply_markup=pf_keyboard(pf_id, False))
            return
        if p.is_running:
            await query.edit_message_text("المحفظة شغالة مسبقاً.", reply_markup=pf_keyboard(pf_id, True))
            return

        # MEXC Spot has one shared balance per API account, not per Telegram user
        # or portfolio. Do not adopt/buy an asset already allocated elsewhere.
        conflicts = db.query(PortfolioCoin).join(Portfolio).filter(
            PortfolioCoin.symbol.in_(coins),
            PortfolioCoin.portfolio_id != p.id,
            Portfolio.status == "active",
            PortfolioCoin.remaining_amount > 0,
        ).all()
        if conflicts:
            conflict_symbols = sorted({str(c.symbol).upper() for c in conflicts})
            await query.edit_message_text(
                "⛔ لم يتم تشغيل المحفظة لتجنب تخصيص رصيد MEXC نفسه لأكثر من محفظة.\n"
                f"العملات المتعارضة: {', '.join(conflict_symbols)}\n"
                "أوقف/سوِّ المراكز الأخرى أولًا، ثم أعد المحاولة.",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return

        # DCA portfolios need a full allocation pass so the initial-buy share and
        # the reserved limit-buy share are both respected. Per-coin entry sliders
        # are intentionally overridden by the saved portfolio strategy.
        if normalize_entry_strategy(getattr(p, "entry_strategy", "tp_sl")) == "tp_sl_dca":
            await _do_rebuild_positions(query, tid, pf_id, starting=True)
            return
        entry_pct = 100  # TP/SL-only strategy uses the complete assigned budget.

        # Check the wallet before buying so restarting a stopped portfolio
        # cannot purchase coins that are already held.
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(coins)
            )
        except Exception as exc:
            logger.exception("Portfolio start balance preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص أرصدة المحفظة من MEXC.\n"
                "لم يتم تشغيل المحفظة ولم يتم تنفيذ أي شراء.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return

        present_coins = [
            coin for coin in coins
            if presence.get(str(coin).upper().strip(), {}).get("present", False)
        ]
        missing_coins = [coin for coin in coins if coin not in present_coins]
        entry_pct = int(entry_pct)
        if entry_pct not in (25, 50, 70, 100):
            await query.edit_message_text("نسبة الدخول غير صالحة.", reply_markup=pf_keyboard(pf_id, False))
            return
        full_budget = float(p.investment_usdt or 0)
        purchase_total = calculate_portfolio_entry_budget(
            full_budget, entry_pct, len(coins), len(missing_coins)
        )
        if missing_coins and purchase_total > 0:
            try:
                loop = asyncio.get_event_loop()
                free_usdt_raw = await loop.run_in_executor(None, lambda: float(get_mexc().get_free_usdt() or 0))
                free_usdt = spendable_usdt_after_reserves(
                    free_usdt_raw, _cloud_dca_reserved_usdt(db)
                )
            except Exception as exc:
                logger.exception("USDT preflight failed")
                await query.edit_message_text(
                    "⚠️ تعذر قراءة رصيد USDT من MEXC. لم يتم الشراء.\n"
                    f"الخطأ: `{str(exc)[:180]}`",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                return
            if free_usdt + 0.01 < purchase_total:
                # Existing positions can fund the missing coin. Switch to the
                # full rebalance path instead of asking the user for fresh USDT;
                # it sells only per-coin excess above the new equal allocation.
                set_portfolio_running(db, pf_id, True)
                db.commit()
                await _do_rebuild_positions(query, tid, pf_id)
                return
        if operation_key in _ACTIVE_PORTFOLIO_REBUILDS or operation_key in _ACTIVE_PORTFOLIO_TRIMS:
            await query.edit_message_text(
                "⏳ توجد عملية شراء/بيع أخرى لهذه المحفظة؛ لم يبدأ التشغيل لتجنب أوامر متداخلة.",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return
        _ACTIVE_PORTFOLIO_REBUILDS.add(operation_key)
        operation_locked = True
        balance_lines = []
        for coin in present_coins:
            info = presence.get(str(coin).upper().strip(), {})
            amount = float(info.get("amount") or 0)
            value = float(info.get("market_value") or 0)
            balance_lines.append(f"`{coin}`: `{amount:.6g}` ≈ `{value:.2f}` USDT")

        user = get_or_create_user(db, tid)
        # Portfolio-specific overrides, else user defaults
        def _pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default
        tp1 = _resolve_optional_pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolve_optional_pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolve_optional_pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _resolve_optional_pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _resolve_optional_pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _resolve_stop_loss_pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
        if True:
            # Keep the resolved user defaults on the portfolio so the monitor
            # uses exactly the same percentages after this session ends.
            p.tp1_pct, p.tp2_pct, p.tp3_pct = tp1, tp2, tp3
            p.tp1_sell_pct, p.tp2_sell_pct, p.stop_loss_pct = s1, s2, sl_pct
            db.flush()

        purchase_result = {"executed": [], "errors": []}
        pending_buy_items = []
        pending_buy_symbols = set()
        if missing_coins:
            # The chosen percentage is applied to the portfolio's assigned budget.
            # Only the proportional shares for missing coins are purchased; existing
            # holdings are never bought a second time.
            if purchase_total + 1e-9 < PORTFOLIO_MIN_ORDER_USDT:
                await query.edit_message_text(
                    f"⛔ ميزانية الدخول `{entry_pct}%` لا تكفي لأمر شراء واحد.\n"
                    f"الميزانية المقدرة للعملات الناقصة: `{purchase_total:.2f}` USDT؛ "
                    f"الحد الأدنى للشراء `{PORTFOLIO_MIN_ORDER_USDT:.2f}` USDT.\n"
                    "لم يتم إرسال أي أمر شراء. زوّد المخصص أو اختر نسبة دخول أكبر.",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                return
            await query.edit_message_text(
                "⏳ جاري فحص المحفظة ثم شراء العملات الناقصة فقط...\n"
                f"نسبة الدخول: `{entry_pct}%` | ميزانية الدخول: `{purchase_total:.2f}` USDT\n"
                f"الموجود: `{len(present_coins)}` | الناقص: `{len(missing_coins)}`"
            )
            purchase_result = await loop.run_in_executor(
                None,
                lambda: get_reb().start_portfolio(
                    coins=missing_coins,
                    total_usdt=purchase_total,
                    method=p.allocation_method or "equal",
                    min_trade_usdt=PORTFOLIO_MIN_ORDER_USDT,
                    dry_run=False,
                ),
            )
            pending_buy_items = purchase_result.get("pending_confirmation") or []
            pending_buy_symbols = _persist_unresolved_market_buys(db, p, pending_buy_items)
            if purchase_result.get("errors") and not purchase_result.get("executed") and not pending_buy_items:
                err = "\n".join(str(e) for e in purchase_result["errors"])
                await query.edit_message_text(
                    f"❌ فشل شراء العملات الناقصة:\n`{err}`",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                return
        # MEXC calls and TP order submissions are synchronous CCXT operations.
        # Keep them off the asyncio event loop, otherwise the bot appears frozen
        # on the previous status message while each exchange request waits.
        await query.edit_message_text(
            "⏳ جاري قراءة أرصدة العملات وأسعارها من MEXC وتجهيز أوامر TP/SL...\n"
            "قد يستغرق ذلك بعض الوقت إذا كانت المنصة بطيئة؛ لن يتم شراء عملات جديدة في هذه الخطوة.",
            reply_markup=pf_keyboard(pf_id, False),
        )
        loop = asyncio.get_running_loop()
        try:
            coins_data, tp_results = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: _fetch_start_tp_inputs(
                        [c.symbol for c in p.coins
                         if str(c.symbol).upper() not in pending_buy_symbols],
                    ),
                ),
                timeout=90,
            )
        except asyncio.TimeoutError:
            await query.edit_message_text(
                "⚠️ انتهت مهلة قراءة أرصدة وأسعار MEXC بعد 90 ثانية.\n"
                "لم تُرسل أوامر TP/SL ولم تُشغّل المحفظة. تحقق من اتصال MEXC ثم أعد المحاولة.",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return
        except Exception as exc:
            logger.exception("Portfolio start balance/price fetch failed")
            await query.edit_message_text(
                "⚠️ تعذر قراءة أرصدة العملات وأسعارها من MEXC. لم تُرسل أوامر TP/SL.\n"
                f"التفاصيل: `{str(exc)[:180]}`\n"
                "تحقق من اتصال MEXC وسجلات الاستضافة ثم أعد المحاولة.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return

        if not coins_data and not pending_buy_symbols:
            await query.edit_message_text(
                "⚠️ لم أجد أي كمية حرة قابلة لتجهيز أوامر TP/SL. قد تكون الأرصدة محجوزة في أوامر مفتوحة أو تعذر جلب الأسعار.\n"
                "لم أرسل أوامر بيع جديدة ولم أغيّر حالة المحفظة إلى شغالة. افحص الأوامر المفتوحة في MEXC ثم أعد المحاولة.",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return

        # Submit each coin's TP orders in the executor and report progress every
        # few coins. This keeps Telegram responsive and makes a slow exchange
        # visibly progress instead of leaving the user on a stale status message.
        for index, item in enumerate(coins_data, start=1):
            try:
                coin_results = await loop.run_in_executor(
                    None,
                    lambda current=item: get_reb().place_tp_orders(
                        [current], tp1, tp2, tp3, sl_pct, s1, s2,
                        control_mode="manual",
                    ),
                )
                tp_results.extend(coin_results)
            except Exception as exc:
                logger.exception("Portfolio start TP submission failed for %s", item.get("symbol"))
                tp_results.append({
                    "symbol": item["symbol"], "amount": item["amount"],
                    "entry_price": item["entry_price"], "tp1_price": 0.0,
                    "tp2_price": 0.0, "tp3_price": 0.0, "sl_price": 0.0,
                    "original_sl_price": 0.0, "tp1_order_id": None,
                    "tp2_order_id": None, "tp3_order_id": None,
                    "error": f"حالة إرسال TP غير مؤكدة، راجع MEXC قبل إعادة المحاولة: {str(exc)[:100]}",
                })
            if index % 3 == 0 or index == len(coins_data):
                await query.edit_message_text(
                    "⏳ جاري تجهيز أوامر TP/SL على MEXC...\n"
                    f"تقدم العملات: `{index}/{len(coins_data)}`\n"
                    "لم يتم شراء عملات جديدة في هذه الخطوة.",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )

        await query.edit_message_text(
            "✅ انتهى تجهيز أوامر TP/SL لكل العملات.\n"
            "💾 جاري حفظ المراكز في قاعدة البيانات ثم تجهيز أوامر DCA...",
            reply_markup=pf_keyboard(pf_id, False),
        )
        lines = [
            (
                f"✅ الرصيد موجود وتم تشغيل *{p.name}* بدون شراء جديد."
                if not missing_coins
                else f"✅ تم تشغيل *{p.name}*"
            ),
            f"🎯 TP1 `{tp1}%`({s1}%) | TP2 `{tp2}%`({s2}%) | TP3 `{tp3}%`",
            f"🛡 استوب `{sl_pct}%`",
            f"💰 نسبة الدخول المختارة: `{entry_pct}%` من المخصص (للعملات الناقصة فقط)",
            "",
        ]
        if balance_lines:
            lines.extend(["💰 *الرصيد الموجود:*", *balance_lines, ""])
        if missing_coins:
            lines.append("🛒 تم شراء العملات الناقصة فقط: " + ", ".join(f"`{x}`" for x in missing_coins))
            lines.append("")
        if purchase_result.get("errors"):
            lines.append("⚠️ ملاحظات الشراء:\n" + "\n".join(f"• {x}" for x in purchase_result["errors"]))
        saved_positions = 0
        for r in tp_results:
            coin_obj = next((c for c in p.coins if c.symbol == r["symbol"]), None)
            if not coin_obj:
                continue
            if float(r.get("amount") or 0) <= 0 or (r.get("error") and float(r.get("entry_price") or 0) <= 0):
                lines.append(f"⚠️ `{r['symbol']}`: {r.get('error') or 'لا توجد كمية حرة؛ لم يتم تغيير سجل المركز.'}")
                continue
            update_coin_position(
                db, coin_obj.id,
                entry_price=r.get("entry_price", 0),
                tp1_price=r.get("tp1_price", 0),
                tp2_price=r.get("tp2_price", 0),
                tp3_price=r.get("tp3_price", 0),
                tp_price=r.get("tp1_price", 0),
                current_sl_price=r.get("sl_price", 0),
                original_sl_price=r.get("original_sl_price") or r.get("sl_price", 0),
                amount=r.get("amount", 0),
                remaining_amount=r.get("amount", 0),
                tp1_order_id=r.get("tp1_order_id"),
                tp2_order_id=r.get("tp2_order_id"),
                tp3_order_id=r.get("tp3_order_id"),
                position_status="open",
                reentry_used=False,
                reentry_touched=False,
                reentry_price=0.0,
                commit=False,
            )
            saved_positions += 1
            if r.get("error"):
                lines.append(f"⚠️ `{r['symbol']}`: {r['error']}")
            lines.append(
                f"`{r['symbol']}` دخول `{r.get('entry_price',0):.6g}`\n"
                f"  TP1 `{r.get('tp1_price',0):.6g}` | TP2 `{r.get('tp2_price',0):.6g}` | "
                f"TP3 `{r.get('tp3_price',0):.6g}` | SL `{r.get('sl_price',0):.6g}`"
            )

        db.commit()
        await query.edit_message_text(
            "✅ تم حفظ مراكز TP/SL بنجاح. جاري إنهاء تشغيل المحفظة...",
            reply_markup=pf_keyboard(pf_id, False),
        )
        if pending_buy_symbols:
            lines.append(
                "⚠️ أوامر شراء أولي غير محسومة ومحفوظة للتسوية: "
                + ", ".join(f"`{symbol}`" for symbol in sorted(pending_buy_symbols))
                + ". لم تُوضع لها أهداف TP/SL ولم يُنشأ سلم DCA حتى تتأكد الكمية. "
                "لا تعاود الشراء يدويًا؛ بعد التسوية أعد بناء المحفظة لتجهيز السلم."
            )
        elif normalize_entry_strategy(getattr(p, "entry_strategy", "tp_sl")) == "tp_sl_dca":
            async def _start_dca_progress(done, total, placed, skipped):
                if total and (done % 3 == 0 or done == total):
                    await query.edit_message_text(
                        "📉 جاري تجهيز أوامر DCA على MEXC...\n"
                        f"التقدم: `{done}/{total}` أمر | أُرسل: `{placed}` | متخطّى: `{skipped}`\n"
                        "لم يتم شراء عملات جديدة؛ هذه أوامر Limit للاحتياطي فقط.",
                        parse_mode="Markdown",
                        reply_markup=pf_keyboard(pf_id, False),
                    )
            await query.edit_message_text(
                "📉 جاري تجهيز أوامر DCA على MEXC...\n"
                "تم حفظ مراكز TP/SL، وسيظهر التقدم أثناء إرسال كل مستوى.",
                reply_markup=pf_keyboard(pf_id, False),
            )
            dca_result = await _place_portfolio_dca_orders(
                db, p, client=get_mexc(), progress=_start_dca_progress
            )
            if dca_result.get("messages"):
                lines.append("\n📉 DCA: " + " | ".join(dca_result["messages"]))
            lines.append(
                f"\n📉 أوامر DCA على MEXC: `{dca_result.get('placed', 0)}` أُرسل، "
                f"`{dca_result.get('skipped', 0)}` متخطّى، ميزانية محجوزة محليًا "
                f"`{dca_result.get('budget', 0.0):.2f}` USDT (الأوامر المفتوحة تحجز المبلغ داخل MEXC)."
            )
        set_portfolio_running(db, pf_id, True)
        log_action(db, tid, "start", f"Started {p.name} multi-TP", True, pf_id)
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, True))
    finally:
        if operation_locked:
            _ACTIVE_PORTFOLIO_REBUILDS.discard(operation_key)
        db.close()


@trading_guard.serialized
async def _do_trim_holdings(query, tid, pf_id, pct):
    """Safely reduce tracked holdings by a selected percentage without stopping a portfolio."""
    if pct not in (25, 50, 70):
        await query.edit_message_text("نسبة تخفيف غير صالحة.", reply_markup=main_menu_keyboard())
        return
    lock_key = (int(tid), int(pf_id))
    if lock_key in _ACTIVE_PORTFOLIO_TRIMS or lock_key in _ACTIVE_PORTFOLIO_REBUILDS:
        await query.edit_message_text("⏳ يجري بالفعل تنفيذ عملية شراء/بيع لهذه المحفظة؛ انتظر النتيجة.")
        return
    _ACTIVE_PORTFOLIO_TRIMS.add(lock_key)
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = list(p.coins or [])
        candidates = [c for c in coins if float(c.remaining_amount or 0) > 0 and c.position_status not in ("idle", "closed")]
        if not candidates:
            await query.edit_message_text(
                "لا توجد مراكز مفتوحة مسجلة يمكن تخفيفها. لم يتم بيع أي رصيد غير معروف.",
                reply_markup=pf_keyboard(pf_id, bool(p.is_running)),
            )
            return

        client = get_mexc()
        loop = asyncio.get_event_loop()
        results = []
        trimmed_any = False
        skipped_shared = []

        # Load effective TP/SL settings so TP orders can be recreated on the remaining size.
        user = get_or_create_user(db, tid)
        def _resolved(pf_value, user_value, default):
            if pf_value is not None and float(pf_value) > 0:
                return float(pf_value)
            if user_value is not None and float(user_value) > 0:
                return float(user_value)
            return float(default)
        tp1 = _resolve_optional_pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolve_optional_pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolve_optional_pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _resolve_optional_pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _resolve_optional_pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _resolve_stop_loss_pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        async def _restore_tp_orders(coin_obj, amount, reference_price):
            """Restore this running position's TP orders after a canceled trim attempt."""
            if not p.is_running or amount <= 0:
                return True
            try:
                tp_res = await loop.run_in_executor(
                    None,
                    lambda s=coin_obj.symbol, a=amount, e=reference_price: get_reb().place_tp_orders(
                        [{"symbol": s, "amount": a, "entry_price": e, "place_exchange_orders": False}],
                        tp1, tp2, tp3, sl_pct, s1, s2, control_mode="manual",
                    ),
                )
                tr = next((x for x in (tp_res or []) if x.get("symbol") == coin_obj.symbol), None)
                if not tr or tr.get("error"):
                    return False
                update_coin_position(
                    db, coin_obj.id,
                    tp1_price=tr.get("tp1_price", coin_obj.tp1_price),
                    tp2_price=tr.get("tp2_price", coin_obj.tp2_price),
                    tp3_price=tr.get("tp3_price", coin_obj.tp3_price),
                    tp_price=tr.get("tp1_price", coin_obj.tp_price),
                    current_sl_price=max(
                        float(coin_obj.current_sl_price or 0),
                        float(tr.get("sl_price") or 0),
                    ),
                    tp1_order_id=tr.get("tp1_order_id"),
                    tp2_order_id=tr.get("tp2_order_id"),
                    tp3_order_id=tr.get("tp3_order_id"),
                    tp_order_id=tr.get("tp_order_id"),
                )
                return True
            except Exception:
                logger.exception("restore TP after trim attempt %s", coin_obj.symbol)
                return False

        for coin in candidates:
            symbol = str(coin.symbol or "").upper().strip()
            tracked = float(coin.remaining_amount or 0)
            if not symbol or tracked <= 0:
                continue

            # The wallet is shared across portfolios; protect tracked holdings
            # even when their portfolio is currently paused.
            other = db.query(PortfolioCoin).join(Portfolio).filter(
                PortfolioCoin.symbol == symbol,
                PortfolioCoin.portfolio_id != p.id,
                Portfolio.status == "active",
                PortfolioCoin.remaining_amount > 0,
            ).first()
            if other:
                skipped_shared.append(symbol)
                results.append(f"⏭ `{symbol}`: موجودة في محفظة أخرى عاملة؛ تم التخطي للحماية.")
                continue

            orders_cancelled_for_coin = False
            sell_order_submitted = False
            order_ids = [
                str(oid) for oid in (
                    getattr(coin, "tp_order_id", None),
                    getattr(coin, "tp1_order_id", None),
                    getattr(coin, "tp2_order_id", None),
                    getattr(coin, "tp3_order_id", None),
                ) if oid
            ]
            entry_price = float(coin.entry_price or 0)
            current_price = 0.0
            try:
                current_price = float(await loop.run_in_executor(None, lambda s=symbol: client.get_ticker_price(f"{s}/{client.quote}") or 0) or 0)
            except Exception:
                current_price = 0.0
            if current_price <= 0:
                results.append(f"⚠️ `{symbol}`: تعذر قراءة السعر؛ لم يتم البيع.")
                continue

            target_amount = tracked * pct / 100.0
            target_value = target_amount * current_price
            if target_value + 1e-9 < PORTFOLIO_MIN_ORDER_USDT:
                results.append(
                    f"⏭ `{symbol}`: قيمة الجزء المستهدف `{target_value:.2f}$` أقل من الحد الأدنى `{PORTFOLIO_MIN_ORDER_USDT:.2f}$`؛ لم يتم تكبير الأمر تلقائيًا."
                )
                continue

            # Cancel only the TP order IDs owned by this tracked position. If the
            # exchange still reports one open, abort that coin rather than risking
            # an oversell against a live limit order.
            cancel_problem = None
            for oid in order_ids:
                try:
                    await loop.run_in_executor(None, lambda o=oid, s=symbol: client.cancel_order(o, f"{s}/{client.quote}", strict=True))
                except Exception:
                    pass
                try:
                    state = await loop.run_in_executor(
                        None, lambda o=oid, s=symbol: client.fetch_order(o, f"{s}/{client.quote}")
                    )
                except Exception:
                    state = None
                status = str((state or {}).get("status") or "").lower()
                try:
                    raw_filled = (state or {}).get("filled")
                    filled_on_order = (
                        float(raw_filled) if raw_filled is not None else -1.0
                    )
                except (TypeError, ValueError):
                    filled_on_order = -1.0
                if status not in {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}:
                    cancel_problem = f"لم أستطع تأكيد الحالة النهائية لأمر TP `{oid}`"
                    break
                if filled_on_order < 0:
                    cancel_problem = f"لم أستطع تأكيد الكمية المنفذة لأمر TP `{oid}`"
                    break
                if filled_on_order > 0:
                    cancel_problem = (
                        f"أمر TP `{oid}` نُفّذ منه جزء أو كله؛ انتظر مزامنة المراقب ثم أعد المحاولة"
                    )
                    break
            if cancel_problem:
                results.append(f"⚠️ `{symbol}`: {cancel_problem}؛ لم يتم البيع.")
                continue

            # If any sell order remains (possibly manual or owned by another
            # subsystem), do not create a competing sale against the same balance.
            open_sells = await loop.run_in_executor(
                None, lambda s=symbol: client.fetch_open_sell_orders(s)
            )
            if open_sells:
                ids = ", ".join(str(x.get("id") or "?") for x in open_sells[:3])
                results.append(f"⏭ `{symbol}`: ما زال هناك أمر بيع مفتوح ({ids})؛ تم التخطي لتجنب تداخل الأوامر.")
                continue
            orders_cancelled_for_coin = bool(order_ids)
            if orders_cancelled_for_coin:
                update_coin_position(
                    db, coin.id,
                    tp_order_id=None, tp1_order_id=None,
                    tp2_order_id=None, tp3_order_id=None,
                )

            await asyncio.sleep(1.0)
            try:
                free_before = float(await loop.run_in_executor(None, lambda s=symbol: client.get_free_amount(s)) or 0)
                actual_request = calculate_safe_trim_amount(tracked, pct, free_before, current_price, PORTFOLIO_MIN_ORDER_USDT)
                if actual_request <= 0:
                    if orders_cancelled_for_coin and not await _restore_tp_orders(coin, tracked, entry_price or current_price):
                        results.append(f"⚠️ `{symbol}`: لم يتم البيع وتعذرت إعادة أهداف TP؛ راجع المحفظة.")
                    results.append(f"⏭ `{symbol}`: الكمية الحرة بعد إلغاء الأوامر لا تكفي لبيع بقيمة `{PORTFOLIO_MIN_ORDER_USDT:.2f}$`؛ لم يتم تكبير الأمر تلقائيًا.")
                    continue
                request_value = actual_request * current_price
                if request_value + 1e-9 < PORTFOLIO_MIN_ORDER_USDT:
                    if orders_cancelled_for_coin and not await _restore_tp_orders(coin, tracked, entry_price or current_price):
                        results.append(f"⚠️ `{symbol}`: لم يتم البيع وتعذرت إعادة أهداف TP؛ راجع المحفظة.")
                    results.append(f"⏭ `{symbol}`: الكمية الحرة أقل من حد البيع `{PORTFOLIO_MIN_ORDER_USDT:.2f}$`؛ لم يتم البيع.")
                    continue
                order = await loop.run_in_executor(
                    None,
                    lambda s=symbol, a=actual_request: client.create_market_order(f"{s}/{client.quote}", "sell", a),
                )
                if not order or not order.get("id"):
                    if orders_cancelled_for_coin and not await _restore_tp_orders(coin, tracked, entry_price or current_price):
                        results.append(f"⚠️ `{symbol}`: لم يتم البيع وتعذرت إعادة أهداف TP؛ راجع المحفظة.")
                    results.append(f"⏭ `{symbol}`: المنصة لم تقبل أمر البيع (حد أدنى/دقة/رصيد حر).")
                    continue
                sell_order_submitted = True
                oid = str(order.get("id"))
                fill_amount = float(order.get("filled") or 0)
                fill_price = float(order.get("average") or order.get("price") or current_price)
                state = order
                if fill_amount <= 0:
                    await asyncio.sleep(0.6)
                    state = await loop.run_in_executor(None, lambda s=symbol, o=oid: client.fetch_order(o, f"{s}/{client.quote}")) or order
                    fill_amount = float(state.get("filled") or 0)
                    fill_price = float(state.get("average") or state.get("price") or fill_price)
                if fill_amount <= 0:
                    free_after = float(await loop.run_in_executor(None, lambda s=symbol: client.get_free_amount(s)) or 0)
                    fill_amount = max(0.0, min(actual_request, free_before - free_after))
                if fill_amount <= 0:
                    # The order was acknowledged but no fill can be verified. Clear old
                    # canceled TP IDs and require exchange reconciliation before re-arming.
                    update_coin_position(db, coin.id, tp_order_id=None, tp1_order_id=None, tp2_order_id=None, tp3_order_id=None)
                    results.append(f"⚠️ `{symbol}`: أمر البيع `{oid}` أُرسل لكن لم يمكن تأكيد الكمية المنفذة؛ ألغيت أهداف TP القديمة. راجع سجل MEXC قبل أي إعادة محاولة.")
                    continue
                fill_amount = min(float(fill_amount), tracked)
                sold_value = fill_amount * fill_price
                pnl = (fill_price - entry_price) * fill_amount if entry_price > 0 else 0.0
                remaining = max(0.0, tracked - fill_amount)
                update_coin_position(
                    db, coin.id,
                    amount=remaining,
                    remaining_amount=remaining,
                    position_status=("closed" if remaining <= max(tracked * 1e-6, 1e-12) else coin.position_status),
                    tp_order_id=None,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                )
                try:
                    record_trade_event(
                        db, tid, p.id, coin.id, symbol, "manual_trim",
                        entry_price, fill_price, fill_amount, pnl,
                        details=f"Manual holdings trim {pct}% requested; sold {fill_amount:.8g} units; proceeds≈{sold_value:.4f} USDT",
                    )
                except Exception:
                    logger.exception("record manual trim %s", symbol)
                    results.append(
                        f"⚠️ `{symbol}`: تم البيع وتحديث المركز، لكن تعذر حفظ سجل التخفيف؛ لا تستخدم الاسترجاع قبل مراجعة السجل."
                    )

                # Re-create target orders only if this portfolio is still running
                # and the position remains open. Amount/remaining tracking is not
                # overwritten by the returned TP calculation.
                if p.is_running and remaining > max(tracked * 1e-6, 1e-12):
                    if not await _restore_tp_orders(coin, remaining, entry_price or fill_price):
                        results.append(f"⚠️ `{symbol}`: تم البيع لكن تعذرت إعادة وضع أهداف TP؛ راجع المحفظة فورًا.")

                trimmed_any = True
                results.append(
                    f"✅ `{symbol}`: بيع `{fill_amount:.8g}` ≈ `{sold_value:.2f}$`; "
                    f"المتبقي `{remaining:.8g}`; المحقق `{pnl:+.2f}$`."
                )
            except Exception as exc:
                logger.exception("manual trim failed for %s", symbol)
                if orders_cancelled_for_coin and not sell_order_submitted:
                    if not await _restore_tp_orders(coin, tracked, entry_price or current_price):
                        results.append(f"⚠️ `{symbol}`: تعذرت إعادة أهداف TP بعد فشل العملية؛ راجع أوامر MEXC.")
                elif sell_order_submitted:
                    update_coin_position(db, coin.id, tp_order_id=None, tp1_order_id=None, tp2_order_id=None, tp3_order_id=None)
                    results.append(f"⚠️ `{symbol}`: احتمال أن أمر السوق وصل للمنصة؛ لا تعِد المحاولة قبل مراجعة سجل الأوامر.")
                results.append(f"❌ `{symbol}`: فشل التخفيف: `{str(exc)[:120]}`")

        log_action(db, tid, "manual_trim", f"portfolio={p.name} requested_pct={pct} trimmed={trimmed_any} skipped_shared={','.join(skipped_shared)}", trimmed_any, p.id)
        header = [
            f"✂️ *نتيجة تخفيف {p.name} بنسبة {pct}%*",
            "النسبة المستهدفة من الكمية المتبقية المسجلة لكل عملة، وليست من كامل رصيد الحساب.",
            "",
        ]
        if not results:
            results.append("لم تُنفّذ مبيعات؛ لا توجد كميات مؤهلة.")
        await query.edit_message_text(
            "\\n".join(header + results[:28]),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, bool(p.is_running)),
        )
    except Exception as exc:
        logger.exception("manual holdings trim fatal")
        try:
            await query.edit_message_text(
                f"❌ تعذر تخفيف الحيازة: `{str(exc)[:180]}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
        except Exception:
            pass
    finally:
        db.close()
        _ACTIVE_PORTFOLIO_TRIMS.discard(lock_key)


@trading_guard.serialized
async def _do_restore_trimmed(query, tid, pf_id, pct):
    """Repurchase a selected percentage of net manually-trimmed quantity from the ledger."""
    if pct not in (25, 50, 70, 100):
        await query.edit_message_text("نسبة استرجاع غير صالحة.", reply_markup=main_menu_keyboard())
        return
    lock_key = (int(tid), int(pf_id))
    if lock_key in _ACTIVE_PORTFOLIO_TRIMS or lock_key in _ACTIVE_PORTFOLIO_REBUILDS:
        await query.edit_message_text("⏳ يجري بالفعل تنفيذ عملية شراء/بيع لهذه المحفظة؛ انتظر النتيجة.")
        return
    _ACTIVE_PORTFOLIO_TRIMS.add(lock_key)
    db = SessionLocal()
    try:
        pf = get_portfolio(db, pf_id, tid)
        if not pf:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        outstanding = _outstanding_trim_amounts(db, pf_id, tid)
        if not outstanding:
            await query.edit_message_text("لا توجد كميات مخفّضة غير مسترجعة في سجل هذه المحفظة.", reply_markup=pf_keyboard(pf_id, bool(pf.is_running)))
            return

        coins_by_id = {int(c.id): c for c in (pf.coins or [])}
        client = get_mexc()
        loop = asyncio.get_event_loop()
        results = []
        restored_any = False
        skipped_shared = []

        user = get_or_create_user(db, tid)
        def _resolved(pf_value, user_value, default):
            if pf_value is not None and float(pf_value) > 0:
                return float(pf_value)
            if user_value is not None and float(user_value) > 0:
                return float(user_value)
            return float(default)
        tp1 = _resolve_optional_pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolve_optional_pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolve_optional_pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _resolve_optional_pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _resolve_optional_pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _resolve_stop_loss_pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        async def _rearm_tp(coin_obj, amount, reference_price):
            if not pf.is_running or amount <= 0:
                return True
            try:
                tp_res = await loop.run_in_executor(
                    None,
                    lambda s=coin_obj.symbol, a=amount, e=reference_price: get_reb().place_tp_orders(
                        [{"symbol": s, "amount": a, "entry_price": e, "place_exchange_orders": False}],
                        tp1, tp2, tp3, sl_pct, s1, s2, control_mode="manual",
                    ),
                )
                trade = next((x for x in (tp_res or []) if x.get("symbol") == coin_obj.symbol), None)
                if not trade or trade.get("error"):
                    return False
                update_coin_position(
                    db, coin_obj.id,
                    tp1_price=trade.get("tp1_price", coin_obj.tp1_price),
                    tp2_price=trade.get("tp2_price", coin_obj.tp2_price),
                    tp3_price=trade.get("tp3_price", coin_obj.tp3_price),
                    tp_price=trade.get("tp1_price", coin_obj.tp_price),
                    current_sl_price=max(
                        float(coin_obj.current_sl_price or 0),
                        float(trade.get("sl_price") or 0),
                    ),
                    tp1_order_id=trade.get("tp1_order_id"),
                    tp2_order_id=trade.get("tp2_order_id"),
                    tp3_order_id=trade.get("tp3_order_id"),
                    tp_order_id=trade.get("tp_order_id"),
                )
                return True
            except Exception:
                logger.exception("rearm TP after trim restore %s", coin_obj.symbol)
                return False

        for key, item in outstanding.items():
            coin = coins_by_id.get(key[0]) if key[0] is not None else None
            if coin is None:
                results.append(f"⏭ `{item['symbol']}`: لا يوجد مركز مطابق في المحفظة؛ راجع السجل يدويًا.")
                continue
            symbol = str(coin.symbol or item["symbol"]).upper().strip()
            outstanding_amount = max(0.0, float(item["amount"] or 0.0))
            target_amount = outstanding_amount * pct / 100.0
            if not symbol or target_amount <= 0:
                continue

            other = db.query(PortfolioCoin).join(Portfolio).filter(
                PortfolioCoin.symbol == symbol,
                PortfolioCoin.portfolio_id != pf.id,
                Portfolio.status == "active",
                PortfolioCoin.remaining_amount > 0,
            ).first()
            if other:
                skipped_shared.append(symbol)
                results.append(f"⏭ `{symbol}`: العملة موجودة في محفظة أخرى عاملة؛ تم التخطي للحماية من خلط المراكز.")
                continue

            try:
                pair = f"{symbol}/{client.quote}"
                price = float(await loop.run_in_executor(None, lambda s=pair: client.get_ticker_price(s)) or 0.0)
                if price <= 0:
                    results.append(f"⚠️ `{symbol}`: تعذر قراءة السعر؛ لم يتم الشراء.")
                    continue
                if target_amount * price + 1e-9 < PORTFOLIO_MIN_ORDER_USDT:
                    results.append(f"⏭ `{symbol}`: قيمة الاسترجاع المستهدفة `{target_amount * price:.2f}$` أقل من `{PORTFOLIO_MIN_ORDER_USDT:.2f}$`؛ تم التخطي.")
                    continue
                free_usdt_raw = float(await loop.run_in_executor(None, lambda: client.get_free_usdt()) or 0.0)
                free_usdt = spendable_usdt_after_reserves(
                    free_usdt_raw, _cloud_dca_reserved_usdt(db)
                )
                # Preserve DCA reserves first, then apply the requested restore ceiling.
                requested_amount = min(target_amount, max(0.0, free_usdt * 0.998 / price))
                if requested_amount * price + 1e-9 < PORTFOLIO_MIN_ORDER_USDT:
                    results.append(f"⏭ `{symbol}`: رصيد USDT الحر لا يكفي لأمر بقيمة `{PORTFOLIO_MIN_ORDER_USDT:.2f}$`.")
                    continue

                # Cancel only TP orders recorded for this portfolio position; an uncancelled sell can oversell after restore.
                order_ids = [str(oid) for oid in (
                    getattr(coin, "tp_order_id", None), getattr(coin, "tp1_order_id", None),
                    getattr(coin, "tp2_order_id", None), getattr(coin, "tp3_order_id", None),
                ) if oid]
                cancel_failed = False
                for oid in order_ids:
                    try:
                        await loop.run_in_executor(None, lambda o=oid, s=pair: client.cancel_order(o, s, strict=True))
                    except Exception:
                        pass
                    try:
                        state = await loop.run_in_executor(
                            None, lambda o=oid, s=pair: client.fetch_order(o, s)
                        )
                    except Exception:
                        state = None
                    status = str((state or {}).get("status") or "").lower()
                    try:
                        raw_filled = (state or {}).get("filled")
                        filled_on_order = (
                            float(raw_filled) if raw_filled is not None else -1.0
                        )
                    except (TypeError, ValueError):
                        filled_on_order = -1.0
                    if status not in {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}:
                        cancel_failed = True
                        break
                    if filled_on_order != 0:
                        cancel_failed = True
                        break
                if cancel_failed:
                    results.append(
                        f"⚠️ `{symbol}`: تعذر إثبات إلغاء TP بدون تنفيذ؛ لم يتم الشراء. "
                        "انتظر مزامنة المراقب أو راجع سجل MEXC قبل إعادة المحاولة."
                    )
                    continue
                if order_ids:
                    update_coin_position(db, coin.id, tp_order_id=None, tp1_order_id=None, tp2_order_id=None, tp3_order_id=None)
                await asyncio.sleep(0.5)
                # If any unrelated sell order remains, do not add another order under a possibly stale TP state.
                open_sells = await loop.run_in_executor(None, lambda s=symbol: client.fetch_open_sell_orders(s))
                if open_sells:
                    results.append(f"⚠️ `{symbol}`: ما زال هناك أمر بيع مفتوح بعد الفحص؛ لم يتم الشراء ولم تُعد أوامر TP تلقائيًا لتجنب تداخل أوامر البيع. راجع أوامر MEXC ثم أعد ضبط الأهداف.")
                    continue

                free_before = float(await loop.run_in_executor(None, lambda s=symbol: client.get_free_amount(s)) or 0.0)
                order = await loop.run_in_executor(None, lambda s=pair, a=requested_amount: client.create_market_order(s, "buy", a))
                if not order or not order.get("id"):
                    results.append(f"⏭ `{symbol}`: المنصة لم تقبل أمر الشراء (الحد الأدنى/الدقة/الرصيد).")
                    if pf.is_running and order_ids:
                        await _rearm_tp(coin, float(coin.remaining_amount or 0), float(coin.entry_price or price))
                    continue
                oid = str(order.get("id"))
                fill_amount = float(order.get("filled") or 0.0)
                fill_price = float(order.get("average") or order.get("price") or price)
                state = order
                if fill_amount <= 0:
                    await asyncio.sleep(0.7)
                    state = await loop.run_in_executor(None, lambda o=oid, s=pair: client.fetch_order(o, s)) or order
                    fill_amount = float(state.get("filled") or 0.0)
                    fill_price = float(state.get("average") or state.get("price") or fill_price)
                if fill_amount <= 0:
                    # No speculative bookkeeping when an exchange acknowledgement has no verifiable fill.
                    update_coin_position(db, coin.id, tp_order_id=None, tp1_order_id=None, tp2_order_id=None, tp3_order_id=None)
                    results.append(f"⚠️ `{symbol}`: أُرسل الأمر `{oid}` لكن لم يمكن تأكيد كمية الشراء؛ راجع سجل MEXC قبل إعادة المحاولة.")
                    continue
                fill_amount = min(fill_amount, target_amount)
                old_remaining = max(0.0, float(coin.remaining_amount or 0.0))
                old_entry = max(0.0, float(coin.entry_price or 0.0))
                new_remaining = old_remaining + fill_amount
                avg_entry = ((old_remaining * old_entry) + (fill_amount * fill_price)) / new_remaining if new_remaining > 0 else fill_price
                update_coin_position(
                    db, coin.id,
                    amount=new_remaining,
                    remaining_amount=new_remaining,
                    entry_price=avg_entry,
                    position_status="open",
                    tp_order_id=None, tp1_order_id=None, tp2_order_id=None, tp3_order_id=None,
                )
                try:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "manual_trim_restore",
                        avg_entry, fill_price, fill_amount, 0.0,
                        details=f"Restored {pct}% of outstanding manually trimmed quantity; bought {fill_amount:.8g} units; order={oid}",
                    )
                except Exception:
                    logger.exception("record manual trim restore %s", symbol)
                    results.append(f"⚠️ `{symbol}`: تم الشراء لكن تعذّر حفظ سجل الاسترجاع؛ لا تعِد العملية قبل مراجعة قاعدة البيانات وسجل MEXC.")
                if pf.is_running and not await _rearm_tp(coin, new_remaining, avg_entry):
                    results.append(f"⚠️ `{symbol}`: تم الشراء وتحديث الكمية، لكن تعذرت إعادة أوامر TP؛ راجع المحفظة فورًا.")
                restored_any = True
                results.append(f"✅ `{symbol}`: تم شراء `{fill_amount:.8g}` (≈ `{fill_amount * fill_price:.2f}$`)، والمتبقي غير المسترجع ≈ `{max(0.0, outstanding_amount - fill_amount):.8g}`.")
            except Exception as exc:
                logger.exception("manual trim restore failed for %s", symbol)
                results.append(f"❌ `{symbol}`: تعذّر الاسترجاع: `{str(exc)[:120]}`")

        log_action(db, tid, "manual_trim_restore", f"portfolio={pf.name} requested_pct={pct} restored={restored_any} skipped_shared={','.join(skipped_shared)}", restored_any, pf.id)
        if not results:
            results.append("لم يتم تنفيذ شراء؛ لا توجد كميات مؤهلة للاسترجاع.")
        await query.edit_message_text(
            "\n".join([f"↩ *نتيجة استرجاع التخفيف — {pf.name} ({pct}%)*", "تم الاعتماد على الكميات المسجلة في سجل البيع والاسترجاع الفعلي:", ""] + results[:28]),
            parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, bool(pf.is_running)),
        )
    except Exception as exc:
        logger.exception("manual trim restore fatal")
        try:
            await query.edit_message_text(f"❌ تعذر استرجاع الكميات: `{str(exc)[:180]}`", parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, False))
        except Exception:
            pass
    finally:
        db.close()
        _ACTIVE_PORTFOLIO_TRIMS.discard(lock_key)


@trading_guard.serialized
async def _do_stop(query, tid, pf_id):
    """إيقاف المحفظة: إلغاء الأوامر + بيع كل العملات بسعر السوق."""

    operation_key = (int(tid), int(pf_id))
    if operation_key in _ACTIVE_PORTFOLIO_REBUILDS or operation_key in _ACTIVE_PORTFOLIO_TRIMS:
        await query.edit_message_text(
            "⏳ توجد عملية شراء/بيع أخرى لهذه المحفظة؛ لم يبدأ الإيقاف لتجنب أوامر متداخلة.",
            reply_markup=pf_keyboard(pf_id, True),
        )
        return
    _ACTIVE_PORTFOLIO_REBUILDS.add(operation_key)
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = list(p.coins)
        symbols = [c.symbol for c in coins]
        await query.edit_message_text(
            f"⏳ جاري إيقاف *{p.name}*...\n"
            "1) إلغاء أوامر البيع\n"
            "2) بيع كل العملات بسعر السوق",
            parse_mode="Markdown",
        )

        client = get_mexc()
        loop = asyncio.get_event_loop()
        dca_cancel_errors = await _cancel_dca_orders(db, p, client=client)
        if dca_cancel_errors:
            await query.edit_message_text(
                "⚠️ لم يتم إيقاف المحفظة أو بيع الأرصدة لأن بعض أوامر DCA لم تُحسم بأمان:\n"
                + "\n".join(f"• {item}" for item in dca_cancel_errors[:6])
                + "\nراجع MEXC ثم أعد الإيقاف. بقيت المحفظة في حالتها الحالية.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        notes = []
        cancelled_count = 0
        sold_total = 0.0
        sold_symbols = []
        fail_symbols = []
        filled_amounts = {}

        # 1) إلغاء أوامر TP + كل أوامر البيع المفتوحة لكل عملة
        def _cancel_phase():
            n = 0
            errs = []
            for c in coins:
                sym = c.symbol
                for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                    oid = getattr(c, key, None)
                    if not oid:
                        continue
                    try:
                        client.cancel_order(str(oid), sym, strict=False)
                        n += 1
                    except Exception as exc:
                        logger.warning("stop cancel %s %s: %s", sym, oid, exc)
            return n, errs

        try:
            cancelled_count, cerrs = await loop.run_in_executor(None, _cancel_phase)
            notes.extend(cerrs[:5])
        except Exception as exc:
            logger.exception("stop cancel phase")
            notes.append(ar_error(exc, "إلغاء الأوامر"))

        await asyncio.sleep(1.5)

        # رموز موجودة في محافظ شغّالة أخرى (حماية الرصيد المشترك)
        shared = set()
        for c in coins:
            other = db.query(PortfolioCoin).join(Portfolio).filter(
                PortfolioCoin.symbol == c.symbol,
                PortfolioCoin.portfolio_id != p.id,
                Portfolio.status == "active",
                PortfolioCoin.remaining_amount > 0,
            ).first()
            if other:
                shared.add(c.symbol)

        # 2) بيع كل عملة
        def _sell_one(sym, tracked, is_shared):
            # Exchange spot balances are shared. Never liquidate an asset allocated
            # to another portfolio, even if that portfolio is currently paused.
            if is_shared:
                return {"ok": False, "error": "الرصيد مشترك مع محفظة أخرى؛ تم تجاوز البيع وحفظ المركز.", "usdt": 0.0, "amount": 0.0}
            try:
                # Do not race a manual/foreign sell order that could still consume
                # this shared spot balance after our market order is submitted.
                open_sells = client.fetch_open_sell_orders(sym)
                if open_sells:
                    ids = ", ".join(str(o.get("id") or "?") for o in open_sells[:5])
                    return {"ok": False, "error": f"توجد أوامر بيع مفتوحة ({ids})؛ لم يتم إرسال بيع سوق.", "usdt": 0.0, "amount": 0.0}
                tracked = max(0.0, float(tracked or 0))
                free = max(0.0, float(client.get_free_amount(sym) or 0))
            except Exception as exc:
                return {"ok": False, "error": f"تعذر تأكيد الرصيد الحر: {exc}", "usdt": 0.0, "amount": 0.0}
            amount = min(tracked, free)
            if amount <= 0:
                return {"ok": False, "error": "لا توجد كمية حرة مؤكدة ضمن كمية المحفظة المسجلة؛ تم الاحتفاظ بسجل المركز.", "usdt": 0.0, "amount": 0.0}
            try:
                order = client.create_market_order(f"{sym}/{client.quote}", "sell", amount)
                if not order:
                    return {"ok": False, "error": "لم يتم إنشاء أمر بيع (قد تكون الكمية أقل من الحد الأدنى).", "usdt": 0.0, "amount": 0.0}
                order_id = order.get("id")
                checked = order
                filled = float(checked.get("filled") or 0)
                for _ in range(3):
                    if filled > 0 or not order_id:
                        break
                    time.sleep(0.4)
                    fetched = client.fetch_order(str(order_id), f"{sym}/{client.quote}", strict=True)
                    if fetched:
                        checked = fetched
                        filled = float(checked.get("filled") or 0)
                status = str(checked.get("status") or "").lower()
                terminal = status in {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}
                if not order_id or not terminal or filled <= 0:
                    return {"ok": False, "error": f"حالة الأمر {order_id or 'غير المرقم'} غير محسومة ({status or 'unknown'}); تم الاحتفاظ بسجل المركز دون إعادة البيع.", "usdt": 0.0, "amount": 0.0}
                filled = min(filled, amount)
                px = float(checked.get("average") or checked.get("price") or client.get_ticker_price(f"{sym}/{client.quote}") or 0)
                if px <= 0:
                    cost = float(checked.get("cost") or 0)
                    px = cost / filled if cost > 0 else 0
                return {
                    "ok": True,
                    "dust": False,
                    "usdt": filled * px,
                    "amount": filled,
                    "price": px,
                    "order_id": order_id,
                    "partial": (
                        filled + max(1e-10, amount * 1e-6) < amount
                        or free + max(1e-10, tracked * 1e-6) < tracked * 0.99
                    ),
                }
            except Exception as e:
                return {"ok": False, "error": str(e), "usdt": 0.0, "amount": 0.0}

        for c in coins:
            sym = c.symbol
            tracked = position_remaining(c)
            try:
                res = await loop.run_in_executor(None, lambda s=sym, t=tracked, sh=(sym in shared): _sell_one(s, t, sh))
            except Exception as exc:
                logger.exception("stop sell %s", sym)
                fail_symbols.append(f"{sym}: {exc}")
                continue
            if not res.get("ok"):
                fail_symbols.append(f"{sym}: {res.get('error')}")
                logger.error("stop sell failed %s: %s", sym, res.get("error"))
                continue
            amt = float(res.get("amount") or 0)
            usdt = float(res.get("usdt") or 0)
            if amt > 0 and usdt > 0:
                sold_total += usdt
                sold_symbols.append(sym)
                filled_amounts[c.id] = amt
                entry = float(c.entry_price or 0)
                px = float(res.get("price") or (usdt / amt if amt else 0))
                pnl = (px - entry) * amt if entry > 0 else 0
                try:
                    record_trade_event(
                        db, tid, p.id, c.id, sym, "manual_stop",
                        entry, px, amt, pnl,
                        details="إيقاف المحفظة — بيع سوق",
                    )
                except Exception:
                    logger.exception("record stop trade %s", sym)
                if res.get("partial"):
                    fail_symbols.append(f"{sym}: تنفيذ جزئي للأمر {res.get('order_id')}; تم حفظ الكمية المتبقية للمراجعة.")
            await asyncio.sleep(0.25)

        # Do not erase position history after a failed/unconfirmed/partial sell.
        if fail_symbols:
            for coin in coins:
                filled = float(filled_amounts.get(coin.id, 0.0) or 0.0)
                if filled <= 0:
                    continue
                tracked = max(0.0, position_remaining(coin))
                remaining = max(0.0, tracked - filled)
                updates = {
                    "amount": remaining,
                    "remaining_amount": remaining,
                    "position_status": "open" if remaining > 1e-10 else "idle",
                    "tp_order_id": None, "tp1_order_id": None,
                    "tp2_order_id": None, "tp3_order_id": None,
                }
                try:
                    update_coin_position(db, coin.id, **updates)
                except Exception:
                    logger.exception("could not preserve residual position %s", coin.symbol)
            db.commit()
        else:
            reset_coin_positions(db, pf_id)
        set_portfolio_running(db, pf_id, False)
        log_action(
            db, tid, "stop",
            f"Stopped {p.name} sold={sold_total:.2f} fails={len(fail_symbols)}",
            len(fail_symbols) == 0,
            pf_id,
        )

        lines = [
            f"⏹ *تم إيقاف {p.name}*",
            f"أوامر DCA/البيع الملغاة: `{cancelled_count}`",
            f"تم البيع ≈ `{sold_total:.2f}` USDT",
            f"عملات بيعت: `{len(sold_symbols)}` / `{len(symbols)}`",
            "المحفظة محفوظة (متوقفة).",
        ]
        if fail_symbols:
            lines.append("\n⚠️ فشل بيع:")
            lines.extend(f"• {x}" for x in fail_symbols[:10])
            lines.append("راجع الرصيد على MEXC وأعد المحاولة إن لزم.")
            logger.error("portfolio stop partial failures pf=%s: %s", pf_id, fail_symbols)
        if notes:
            lines.append("\nملاحظات إلغاء: " + " | ".join(str(n) for n in notes[:3]))

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, False),
        )
    except Exception as exc:
        logger.exception("do_stop fatal")
        try:
            await query.edit_message_text(
                f"❌ {ar_error(exc, 'إيقاف المحفظة')}",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            pass
    finally:
        _ACTIVE_PORTFOLIO_REBUILDS.discard(operation_key)
        db.close()


async def _do_start_all(query, tid, entry_pct=100):
    """تشغيل المحافظ المتوقفة واحدة تلو الأخرى مع الحفاظ على إعداداتها."""
    db = SessionLocal()
    try:
        portfolios = get_portfolios(db, tid, status="active") or []
        pf_ids = [p.id for p in portfolios if not p.is_running]
    finally:
        db.close()
    if not pf_ids:
        await query.edit_message_text("✅ كل المحافظ النشطة تعمل بالفعل.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 المحافظ", callback_data="list_pf")]]))
        return
    results = []
    for pf_id in pf_ids:
        try:
            await _do_start(query, tid, pf_id, entry_pct=entry_pct)
            status_db = SessionLocal()
            try:
                checked_portfolio = get_portfolio(status_db, pf_id, tid)
                started = bool(checked_portfolio and checked_portfolio.is_running)
            finally:
                status_db.close()
            if started:
                results.append(f"✅ #{pf_id} — تعمل (نسبة الدخول {entry_pct}%)")
            else:
                results.append(f"⚠️ #{pf_id} — لم يكتمل التشغيل؛ راجع نتيجة المحفظة/الرصيد")
        except Exception as exc:
            logger.exception("start_all pf=%s", pf_id)
            results.append(f"❌ #{pf_id}: {str(exc)[:100]}")
    await query.edit_message_text(f"▶️ *اكتمل طلب تشغيل الكل* — نسبة الدخول `{entry_pct}%` لكل محفظة\n\n" + "\n".join(results), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 عرض المحافظ", callback_data="list_pf")]]))

async def _do_stop_all(query, tid):
    """إيقاف كل المحافظ العاملة بالطريقة الآمنة نفسها المستخدمة فرديًا."""
    db = SessionLocal()
    try:
        portfolios = get_portfolios(db, tid, status="active") or []
        pf_ids = [p.id for p in portfolios if p.is_running]
    finally:
        db.close()

    if not pf_ids:
        await query.edit_message_text(
            "✅ لا توجد محافظ عاملة حاليًا.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await query.edit_message_text(
        f"🚨 جاري إيقاف `{len(pf_ids)}` محفظة للطوارئ...\n"
        "سيتم إلغاء الأوامر وبيع الأرصدة بسعر السوق.",
        parse_mode="Markdown",
    )
    for pf_id in pf_ids:
        await _do_stop(query, tid, pf_id)

    await query.edit_message_text(
        f"✅ *اكتمل إيقاف الطوارئ*\n"
        f"تمت معالجة `{len(pf_ids)}` محفظة.\n"
        "المحافظ محفوظة كمتوقفة ويمكن تشغيلها لاحقًا.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


@trading_guard.serialized
async def _do_remove_coin(query, tid, pf_id, symbol, delete_after_sell=True):
    """بيع رصيد العملة بأمان، ثم حذفها نهائيًا أو إبقاؤها في الخطة حسب اختيار المستخدم."""
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coin = next((c for c in p.coins if c.symbol.upper() == symbol.upper()), None)
        if not coin:
            await query.edit_message_text(
                f"العملة `{symbol}` غير موجودة في المحفظة.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        sym = coin.symbol
        action_label = "حذفها نهائيًا من الخطة" if delete_after_sell else "إبقاء العملة في خطة المحفظة"
        await query.edit_message_text(
            f"⏳ جاري معالجة `{sym}`:\n"
            "1) إلغاء أوامر DCA والبيع المرتبطة\n"
            "2) بيع الرصيد المخصص بسعر السوق\n"
            f"3) {action_label}",
            parse_mode="Markdown",
        )

        client = get_mexc()
        reb = get_reb()
        dca_errors = await _cancel_dca_orders(db, p, client=client, symbol=sym)
        if dca_errors:
            await query.edit_message_text(
                f"⚠️ لم يتم بيع أو تغيير `{sym}` لأن حالة أوامر DCA لم تُحسم.\n"
                + "\n".join(f"• {item}" for item in dca_errors[:5])
                + "\nراجع أوامر MEXC ثم أعد المحاولة.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        loop = asyncio.get_event_loop()
        notes = []
        cancelled_count = 0
        reconciled_tracked = None

        # Cancel only orders explicitly linked to this portfolio position.
        # Any other open sell may be manual or belong to another strategy; never cancel it.
        tracked_order_ids = {
            str(getattr(coin, key))
            for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id")
            if getattr(coin, key, None)
        }
        def _cancel_tracked():
            cancelled = []
            for oid in tracked_order_ids:
                client.cancel_order(oid, f"{sym}/{client.quote}", strict=True)
                cancelled.append(oid)
            return cancelled
        try:
            cancelled_count = len(await loop.run_in_executor(None, _cancel_tracked))
            # A remaining order not owned by this position blocks selling. We cannot
            # prove its ownership, and the exchange's free balance is the only safe guard.
            open_sells = await loop.run_in_executor(
                None, lambda: client.fetch_open_sell_orders(sym)
            )
            unknown_sells = [o for o in open_sells if str(o.get("id") or "") not in tracked_order_ids]
            if unknown_sells:
                ids = ", ".join(str(o.get("id") or "?") for o in unknown_sells[:5])
                await query.edit_message_text(
                    f"⚠️ لم يتم حذف `{sym}`: توجد أوامر بيع مفتوحة غير مرتبطة بهذا المركز ({ids}). "
                    "لن ألغيها أو أبيع الرصيد حتى تراجعها يدويًا.",
                    parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
                )
                return
            # Confirm tracked orders are no longer open before selling; reconcile fills.
            await asyncio.sleep(0.8)
            open_sells = await loop.run_in_executor(None, lambda: client.fetch_open_sell_orders(sym))
            if open_sells:
                await query.edit_message_text(
                    f"⚠️ لم يتم حذف `{sym}` لأن أوامر البيع لم تُلغَ/تُحسم بعد. احتفظت بسجل المركز.",
                    parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
                )
                return

            # Cancellation can race with a partial TP fill. Reconcile each tracked
            # order's terminal status and actual filled quantity before selling the
            # residual, otherwise the market sell could exceed this position.
            original_tracked = max(0.0, position_remaining(coin))
            confirmed_tp_fills = []
            terminal_statuses = {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}
            for oid in tracked_order_ids:
                state = await loop.run_in_executor(
                    None, lambda order_id=oid: client.fetch_order(
                        order_id, f"{sym}/{client.quote}", strict=True
                    )
                )
                if not state or str(state.get("status") or "").lower() not in terminal_statuses:
                    raise RuntimeError(f"تعذر تأكيد الحالة النهائية لأمر TP {oid}")
                if state.get("filled") is None:
                    raise RuntimeError(f"المنصة لم تُرجع كمية التنفيذ لأمر TP {oid}")
                filled_tp = max(0.0, float(state.get("filled") or 0))
                if filled_tp > 0:
                    fill_px = float(state.get("average") or state.get("price") or 0)
                    confirmed_tp_fills.append((oid, filled_tp, fill_px))
            tp_filled_total = sum(item[1] for item in confirmed_tp_fills)
            if tp_filled_total > original_tracked + max(1e-10, original_tracked * 1e-6):
                raise RuntimeError("مجموع تعبئات TP المؤكدة يتجاوز كمية المركز المسجلة")
            reconciled_tracked = max(0.0, original_tracked - tp_filled_total)
            for oid, fill_qty, fill_px in confirmed_tp_fills:
                if fill_px > 0:
                    entry_px = float(coin.entry_price or 0)
                    pnl = (fill_px - entry_px) * fill_qty if entry_px > 0 else 0.0
                    record_trade_event(
                        db, tid, p.id, coin.id, sym, "tp_reconciled_on_remove",
                        entry_px, fill_px, fill_qty, pnl,
                        details=f"TP fill reconciled during coin removal; order={oid}",
                    )
            if tp_filled_total > 0:
                update_coin_position(
                    db, coin.id, amount=reconciled_tracked,
                    remaining_amount=reconciled_tracked,
                    tp_order_id=None, tp1_order_id=None, tp2_order_id=None, tp3_order_id=None,
                )
        except Exception as exc:
            logger.exception("remove_coin cancel/reconcile phase")
            await query.edit_message_text(
                f"❌ تعذر تأكيد حالة أوامر `{sym}`: {str(exc)[:160]}\nلم يُبع الرصيد ولم يُحذف السجل.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        # Never sell an allocation while another running portfolio tracks the same asset.
        other_running = db.query(PortfolioCoin).join(Portfolio).filter(
            PortfolioCoin.symbol == sym,
            PortfolioCoin.portfolio_id != p.id,
            Portfolio.status == "active",
            PortfolioCoin.remaining_amount > 0,
        ).first()
        if other_running:
            await query.edit_message_text(
                f"⚠️ لم يتم حذف `{sym}`: العملة مستخدمة في محفظة أخرى تعمل حاليًا. "
                "أوقف/راجع المحفظة الأخرى أولًا لتجنب بيع تخصيص مشترك.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        tracked = reconciled_tracked if reconciled_tracked is not None else max(
            0.0, position_remaining(coin)
        )
        if tracked <= max(1e-12, float(coin.amount or tracked or 0) * 1e-8):
            if reconciled_tracked is not None and reconciled_tracked <= max(1e-12, float(coin.amount or 0) * 1e-8):
                if delete_after_sell:
                    remove_coin_from_portfolio(db, pf_id, sym)
                    final_action = "حُذفت نهائيًا من الخطة"
                else:
                    clear_coin_position(db, coin.id)
                    final_action = "بقيت في الخطة بدون رصيد مفتوح"
                await query.edit_message_text(
                    f"✅ `{sym}`: أكدت المنصة أن أوامر TP باعت الكمية المسجلة بالكامل. {final_action}. لم يُرسل أمر بيع إضافي.",
                    parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
                )
                return
            await query.edit_message_text(
                f"⚠️ لم يتم حذف `{sym}` لأن الكمية المسجلة صفر. راجع المركز يدويًا.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        # Sell only this position's tracked amount that is confirmed free.
        def _sell():
            free = max(0.0, float(client.get_free_amount(sym) or 0))
            amount = min(tracked, free)
            if amount <= 0:
                return {"error": "لا توجد كمية حرة كافية ضمن الكمية المسجلة للمركز."}
            px = float(client.get_ticker_price(f"{sym}/{client.quote}") or 0)
            order = client.create_market_order(f"{sym}/{client.quote}", "sell", amount)
            submission_class, order_id = classify_market_sell_submission(order)
            if submission_class == "dust":
                # MexcClient's contract uses None only for a preflight/explicit
                # minimum rejection. This is not a sale: let the caller remove
                # the active tracking record while clearly preserving the fact
                # that the residual balance may still exist on the exchange.
                return {"dust": True, "amount": amount, "price": px,
                        "reason": "رفض مؤكد للحد الأدنى/الكمية غير القابلة للبيع"}
            if submission_class != "submitted":
                return {"error": "لم تُرجع المنصة معرّف أمر قابلًا للتحقق؛ لم يُحذف المركز."}
            checked = order
            # Always query the exchange at least once to reconcile actual fills.
            for _ in range(5):
                fetched = client.fetch_order(order_id, f"{sym}/{client.quote}", strict=True)
                if fetched:
                    checked = fetched
                    status = str(checked.get("status") or "").lower()
                    filled = float(checked.get("filled") or 0)
                    if status in {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}:
                        break
                time.sleep(0.6)
            filled = min(amount, max(0.0, float(checked.get("filled") or 0)))
            cost = float(checked.get("cost") or 0)
            avg = float(checked.get("average") or checked.get("price") or 0)
            if avg <= 0 and cost > 0 and filled > 0:
                avg = cost / filled
            if filled <= 0 or avg <= 0:
                return {"error": f"تعذر تأكيد كمية/قيمة التنفيذ للأمر {order_id}; احتفظت بسجل المركز."}
            status = str(checked.get("status") or "").lower()
            terminal = status in {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}
            if not terminal:
                return {"error": f"الأمر {order_id} ما زال غير محسوم ({status or 'unknown'}). لا تعِد البيع؛ راجع الأمر في MEXC مع الاحتفاظ بسجل المركز."}
            return {"amount": filled, "sold": cost if cost > 0 else filled * avg,
                    "price": avg, "order_id": order_id, "status": status,
                    "requested": amount, "terminal": terminal,
                    "full_fill": terminal and filled + max(1e-10, amount*1e-6) >= amount}

        try:
            sell_result = await loop.run_in_executor(None, _sell)
        except Exception as exc:
            logger.exception("remove_coin sell phase")
            sell_result = {"error": f"نتيجة الأمر غير مؤكدة: {exc}"}
        if sell_result.get("error"):
            await query.edit_message_text(
                f"❌ لم يتم حذف `{sym}`. {ar_error(sell_result['error'], 'بيع السوق')}\n"
                "احتفظت بسجل المركز لتسوية حالة الأمر.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        if sell_result.get("dust"):
            # This branch is only reachable for the client's explicit None/dust
            # contract, not for a malformed response or a network timeout.
            if delete_after_sell:
                if not remove_coin_from_portfolio(db, pf_id, sym):
                    await query.edit_message_text(
                        f"⚠️ رصيد `{sym}` أقل من الحد الأدنى للبيع، لكن تعذر حذف سجل العملة من قاعدة البيانات.",
                        parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
                    )
                    return
                final_action = "أُزيلت من المراكز النشطة"
            else:
                clear_coin_position(db, coin.id)
                final_action = "تم تصفير المركز النشط مع إبقاء العملة في الخطة"
            dust_amount = max(0.0, float(sell_result.get("amount") or 0.0))
            dust_value = dust_amount * max(0.0, float(sell_result.get("price") or 0.0))
            log_action(
                db, tid, "remove_coin_dust",
                f"Dust not sold: {sym}; tracked_amount={dust_amount:.12g}; approx_usdt={dust_value:.8g}; "
                f"delete_after_sell={delete_after_sell}; no exchange order submitted",
                True, pf_id,
            )
            await query.edit_message_text(
                f"⚠️ *تمت معالجة {sym} كرصيد غبار (Dust)*\n"
                f"السبب: رفض مؤكد بسبب الحد الأدنى للبيع.\n"
                f"الكمية غير المباعة: `{dust_amount:.8g}`، والقيمة التقريبية: `{dust_value:.4f}` USDT.\n"
                f"{final_action}.\n"
                "لم يتم تنفيذ بيع لهذا الرصيد، وقد يظل موجودًا في حساب MEXC. "
                "تم تسجيله كغبار غير مباع، ولم تتم إعادة محاولة البيع.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        sold_amt = float(sell_result.get("amount") or 0)
        sold_usdt = float(sell_result.get("sold") or 0)
        remaining = max(0.0, tracked - sold_amt)
        if not sell_result.get("full_fill") or remaining > max(1e-10, tracked * 1e-6):
            update_coin_position(
                db, coin.id, amount=remaining, remaining_amount=remaining,
                position_status="open" if remaining > 1e-10 else "idle",
                tp_order_id=None, tp1_order_id=None, tp2_order_id=None, tp3_order_id=None,
            )
            await query.edit_message_text(
                f"⚠️ تم تنفيذ بيع جزئي مؤكد لـ `{sym}`: `{sold_amt:.8g}`. "
                f"الكمية المتبقية `{remaining:.8g}` محفوظة في سجل المركز. "
                f"رقم الأمر: `{sell_result.get('order_id')}`. لم أحذف المركز.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        sell_ok = True

        # سجّل حدث البيع إن وُجدت كمية
        try:
            entry = float(coin.entry_price or 0)
            px = sold_usdt / sold_amt if sold_amt > 0 else 0
            pnl = (px - entry) * sold_amt if entry > 0 and sold_amt > 0 else 0
            if sold_amt > 0:
                record_trade_event(
                    db, tid, p.id, coin.id, sym, "manual_remove",
                    entry, px, sold_amt, pnl,
                    details="حذف عملة من المحفظة — بيع سوق",
                )
        except Exception:
            logger.exception("record remove trade")

        # 3) Apply the selected portfolio-plan action after confirming the market sell.
        if delete_after_sell:
            if not remove_coin_from_portfolio(db, pf_id, sym):
                await query.edit_message_text(
                    f"⚠️ تم بيع رصيد `{sym}` على المنصة لكن فشل حذف السجل من القاعدة.",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, p.is_running),
                )
                return
            final_action = "تم حذف العملة نهائيًا من الخطة"
        else:
            clear_coin_position(db, coin.id)
            final_action = "تم بيع الرصيد مع إبقاء العملة داخل الخطة"

        log_action(db, tid, "remove_coin", f"Sold {sym}; delete_after_sell={delete_after_sell}; sold≈{sold_usdt:.2f}", True, pf_id)
        msg = (
            f"✅ {final_action}: *{sym}* في محفظة *{p.name}*\n"
            f"أوامر بيع ملغاة: `{cancelled_count}`\n"
            f"بيع سوق ≈ `{sold_usdt:.2f}` USDT"
        )
        if notes:
            msg += "\n\n⚠️ " + " | ".join(notes)
        await query.edit_message_text(
            msg,
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, p.is_running),
        )
    except Exception as exc:
        logger.exception("remove_coin fatal")
        try:
            await query.edit_message_text(
                f"❌ {ar_error(exc, 'حذف العملة')}",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            pass
    finally:
        db.close()



@trading_guard.serialized
async def _do_close(query, tid, pf_id):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        # A stopped portfolio can still have a live exchange limit-buy order if a
        # previous shutdown was interrupted. Never close/delete it without reconciling.
        dca_errors = await _cancel_dca_orders(db, p, client=get_mexc())
        if dca_errors:
            await query.edit_message_text(
                "⚠️ لم يتم إغلاق المحفظة لأن حالة أوامر DCA لم تُحسم بأمان:\n"
                + "\n".join(f"• {item}" for item in dca_errors[:6])
                + "\nراجع MEXC ثم أعد المحاولة.",
                parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        has_tracked_positions = any(
            max(float(c.remaining_amount or 0), float(c.amount or 0)) > 1e-10
            for c in p.coins
        )
        if p.is_running or has_tracked_positions:
            # Use the same guarded stop workflow as the normal stop button. The
            # old direct stop_portfolio call sold the entire free wallet balance
            # and then erased records even when an order failed or filled partially.
            await _do_stop(query, tid, pf_id)
            db.expire_all()
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text(
                    "⚠️ تعذر إعادة تحميل المحفظة بعد الإيقاف؛ لم يتم حذفها.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            residual = [
                c for c in p.coins
                if max(float(c.remaining_amount or 0), float(c.amount or 0)) > 1e-10
            ]
            if residual:
                names = ", ".join(c.symbol for c in residual[:8])
                await query.edit_message_text(
                    "⚠️ لم يتم حذف المحفظة لأن بعض المراكز لم تُغلق بأمان. "
                    f"السجلات محفوظة للمراجعة: {names}. راجع أوامر MEXC ثم أعد المحاولة.",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                return
        close_portfolio(db, pf_id)
        await query.edit_message_text("✅ تم إغلاق المحفظة بعد التأكد من عدم وجود مراكز متبقية.", reply_markup=main_menu_keyboard())
    finally:
        db.close()


async def _finish_create_portfolio(query, context, tid, strategy, buy_pct, order_count):
    data = context.user_data.get("create") or {}
    name = str(data.get("name") or "").strip()
    amount = float(data.get("amount") or 0.0)
    coins = list(data.get("coins") or [])
    if not name or amount < 5 or not coins:
        await query.edit_message_text("بيانات إنشاء المحفظة غير مكتملة. ابدأ من جديد.", reply_markup=main_menu_keyboard())
        context.user_data.pop("create", None)
        return
    strategy = normalize_entry_strategy(strategy)
    buy_pct = normalize_dca_buy_pct(buy_pct) if strategy == "tp_sl_dca" else 100
    order_count = normalize_dca_order_count(order_count) if strategy == "tp_sl_dca" else 1
    db = SessionLocal()
    try:
        p = create_portfolio(
            db, tid, name, amount, coins,
            entry_strategy=strategy,
            dca_buy_pct=buy_pct,
            dca_order_count=order_count,
        )
        strategy_label = portfolio_strategy_label(p)
        await query.edit_message_text(
            f"✅ تم إنشاء *{p.name}* (#{p.id})\n"
            f"المبلغ المخصص: `{p.investment_usdt:.2f} USDT`\n"
            f"العملات: `{', '.join(coins)}`\n"
            f"النظام: *{strategy_label}*\n\n"
            "لم تُرسل أوامر شراء الآن. عند تشغيل المحفظة أو إعادة توازنها سيُطبّق التقسيم المختار.",
            parse_mode="Markdown", reply_markup=main_menu_keyboard(),
        )
        context.user_data.clear()
    finally:
        db.close()


async def create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["create"]["name"] = update.message.text.strip()
    await update.message.reply_text("أرسل *مبلغ الاستثمار* بالـ USDT:", parse_mode="Markdown")
    return CREATE_AMOUNT


async def create_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = finite_amount(update.message.text, 5)
        if amount < 5:
            await update.message.reply_text("المبلغ لازم يكون ≥ 5 USDT")
            return CREATE_AMOUNT
        context.user_data["create"]["amount"] = amount
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return CREATE_AMOUNT
    await update.message.reply_text("أرسل رموز العملات مفصولة بمسافة\nمثال: `BTC ETH SOL`", parse_mode="Markdown")
    return CREATE_COINS


async def create_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper().replace(",", " ")
    coins = [c.strip() for c in raw.split() if c.strip()]
    if not coins:
        await update.message.reply_text("أدخل عملة واحدة على الأقل.")
        return CREATE_COINS
    if len(coins) > 30:
        await update.message.reply_text("الحد الأقصى 30 عملة.")
        return CREATE_COINS
    data = context.user_data["create"]
    data["coins"] = coins
    await update.message.reply_text(
        "✅ تم حفظ بيانات المحفظة والعملات.\n\n"
        "سيتم إنشاء المحفظة بنظام أهداف TP ووقف خسارة فقط، بدون تفعيل DCA أثناء الإنشاء.\n"
        "يمكنك ضبط إعدادات DCA وتفعيلها لاحقًا من الإعدادات العامة، ثم ستُطبّق عند إعادة الموازنة.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ إنشاء المحفظة بدون DCA", callback_data="create_mode_tpsl")],
            [InlineKeyboardButton("× إلغاء", callback_data="menu")],
        ]),
    )
    return ConversationHandler.END


async def add_coin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()
    pf_id = context.user_data.get("addcoin_pf")
    if not pf_id:
        context.user_data.clear()
        await update.message.reply_text("حدث خطأ.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        ok, msg = add_coin_to_portfolio(db, pf_id, symbol, max_coins=user.max_coins_per_portfolio or 30)
        context.user_data.clear()
        p = get_portfolio(db, pf_id, update.effective_user.id)
        if ok and p:
            msg += (
                "\n\n📊 تم إضافة العملة إلى الخطة فقط. "
                "لإعادة تقسيم المبلغ بالتساوي، اضغط *إعادة توزيع الآن*؛ "
                "سيبيع البوت الزيادة من المراكز الحالية ثم يشتري الناقص."
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚖️ إعادة توازن عام الآن", callback_data=f"rebalance_{pf_id}")],
                [InlineKeyboardButton("↩ المحفظة", callback_data=f"view_{pf_id}")],
            ])
        else:
            markup = main_menu_keyboard()
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)
    finally:
        db.close()
    return ConversationHandler.END


async def increase_amount_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = finite_amount(update.message.text, 0.00000001)
        if amount <= 0:
            await update.message.reply_text("المبلغ لازم يكون موجب.")
            return INCREASE_AMOUNT
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return INCREASE_AMOUNT
    pf_id = context.user_data.get("edit_amount_pf") or context.user_data.get("increase_pf")
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, update.effective_user.id)
        if not p:
            await update.message.reply_text("غير موجودة.", reply_markup=main_menu_keyboard())
            context.user_data.clear()
            return ConversationHandler.END
        coins = [c.symbol for c in (p.coins or [])]
        if not coins:
            await update.message.reply_text("المحفظة بدون عملات.", reply_markup=main_menu_keyboard())
            context.user_data.clear()
            return ConversationHandler.END

        min_order = max(2.0, float(getattr(config, "PORTFOLIO_MIN_ORDER_USDT", 2.0)))
        minimum_total = max(2.0, min_order * len(coins))
        if amount < minimum_total:
            await update.message.reply_text(
                f"المخصص الجديد يجب أن يكون `{minimum_total:.2f}` USDT على الأقل لـ `{len(coins)}` عملة.",
                parse_mode="Markdown",
            )
            return INCREASE_AMOUNT
        old = float(p.investment_usdt or 0)
        p.investment_usdt = amount
        db.commit()
        context.user_data.clear()
        msg = (
            "✅ *تم تعديل المبلغ المخصص*\n"
            f"القديم: `{old:.2f}` USDT\n"
            f"الجديد: `{amount:.2f}` USDT\n"
            f"المستهدف لكل عملة: `{amount / len(coins):.2f}` USDT\n\n"
            "لم يتم تنفيذ شراء أو بيع تلقائي. استخدم *إعادة بناء* للمطابقة."
        )
        await update.message.reply_text(
            msg, parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, bool(p.is_running)),
        )
    finally:
        db.close()
    return ConversationHandler.END


@trading_guard.serialized
async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE):
    """مراقبة مراكز المحافظ وأوامر TP/SL وإعادة الدخول."""
    cycle_started = time.time()
    cycle_positions = 0
    cycle_actions = 0
    cycle_error = None
    _MONITOR_HEALTH["last_started_at"] = cycle_started
    db = SessionLocal()
    try:
        await _sync_active_dca_orders(db, context=context)
        positions = get_open_positions(db)
        # A manual trim/restore owns its portfolio balance while in flight.
        # Avoid a concurrent monitor sell/buy against the same tracked quantity;
        # the next polling cycle resumes monitoring after the operation ends.
        positions = [
            position for position in positions
            if (
                int(getattr(position.portfolio, "telegram_id", 0) or 0),
                int(getattr(position.portfolio, "id", 0) or 0),
            ) not in (_ACTIVE_PORTFOLIO_TRIMS | _ACTIVE_PORTFOLIO_REBUILDS)
        ]
        cycle_positions = len(positions)
        if not positions:
            return
        # PortfolioCoin has no TP sell-percentage columns. Resolve settings
        # from its portfolio (then user defaults) and cache them on this
        # in-memory snapshot before handing it to the synchronous monitor.
        settings_by_user = {}

        def _resolved_sell_pct(portfolio_value, user_value):
            for value in (portfolio_value, user_value):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    return min(100.0, number)
            return 40.0

        for position in positions:
            portfolio = getattr(position, "portfolio", None)
            owner_id = int(
                getattr(portfolio, "telegram_id", None) or config.ADMIN_TELEGRAM_ID
            )
            if owner_id not in settings_by_user:
                settings_by_user[owner_id] = get_or_create_user(db, owner_id)
            user_settings = settings_by_user[owner_id]
            position._monitor_tp1_sell_pct = _resolved_sell_pct(
                getattr(portfolio, "tp1_sell_pct", None),
                getattr(user_settings, "tp1_sell_pct", None),
            )
            position._monitor_tp2_sell_pct = _resolved_sell_pct(
                getattr(portfolio, "tp2_sell_pct", None),
                getattr(user_settings, "tp2_sell_pct", None),
            )
        logger.info(
            "[MONITOR] open_positions=%s symbols=%s",
            len(positions),
            ",".join(sorted({c.symbol for c in positions})[:30]),
        )
        # Run sync CCXT work off the event loop so Telegram stays responsive
        loop = asyncio.get_event_loop()
        reb = get_reb()
        # Exchange-hosted TP orders are the only TP execution path.
        # The legacy cloud TP fallback is intentionally disabled.
        manual_tp_actions = await loop.run_in_executor(
            None, lambda: reb.sync_manual_tp_orders(positions)
        )
        manual_handled_ids = {
            action.get("coin_id")
            for action in manual_tp_actions
            if action.get("coin_id") is not None
        }
        cloud_tp_actions = []  # no bot-triggered market take-profit orders
        # Keep this set for compatibility with older action payloads.
        cloud_handled_ids = {
            action.get("coin_id")
            for action in cloud_tp_actions
            if action.get("coin_id") is not None
            and action.get("action") in {"tp1_hit", "tp2_hit", "tp3_hit", "tp_full_close"}
        }
        price_positions = [
            position for position in positions
            if getattr(position, "id", None) not in cloud_handled_ids
            and getattr(position, "id", None) not in manual_handled_ids
        ]
        price_actions = await loop.run_in_executor(
            None, lambda: reb.check_and_manage_positions(price_positions)
        )
        actions = manual_tp_actions + cloud_tp_actions + price_actions
        cycle_actions = len(actions)
        if actions:
            logger.info(
                "[MONITOR] actions=%s types=%s",
                len(actions),
                ",".join(a.get("action", "?") for a in actions),
            )
        for act in actions:
            symbol = act["symbol"]
            coin_id = act.get("coin_id")
            if coin_id is None:
                # Compatibility with action payloads produced by older code.
                coin = next((c for c in positions if c.symbol == symbol), None)
            else:
                # A symbol can exist in multiple portfolios. Always reload the
                # exact row that produced the action, and skip it if a user
                # removed it while this monitor cycle was running.
                coin = db.query(PortfolioCoin).filter(
                    PortfolioCoin.id == coin_id
                ).first()
            if not coin:
                continue
            pf = coin.portfolio
            tid = pf.telegram_id if pf else config.ADMIN_TELEGRAM_ID
            if act.get("action") in {"tp1_hit", "tp2_hit", "tp3_hit"}:
                await _shift_cloud_dca_ladder(db, coin.id, "up", client=get_mexc())

            if act.get("action") in {"sl_hit_wait_reentry", "sl_hit_sold", "sl_sell_partial", "tp_full_close"} and pf:
                tracked_dca = db.query(PortfolioDcaOrder).filter(
                    PortfolioDcaOrder.portfolio_id == pf.id,
                    PortfolioDcaOrder.portfolio_coin_id == coin.id,
                    PortfolioDcaOrder.status.in_(tuple(_DCA_ACTIVE_STATUSES)),
                ).all()
                before_fills = {row.id: float(row.filled_amount or 0.0) for row in tracked_dca}
                dca_cancel_errors = await _cancel_dca_orders(db, pf, symbol=symbol)
                after_fills = {
                    row.id: float(row.filled_amount or 0.0)
                    for row in db.query(PortfolioDcaOrder).filter(
                        PortfolioDcaOrder.portfolio_id == pf.id,
                        PortfolioDcaOrder.portfolio_coin_id == coin.id,
                    ).all()
                }
                changed_by_dca = any(after_fills.get(row_id, 0.0) > previous + 1e-10 for row_id, previous in before_fills.items())
                if dca_cancel_errors:
                    try:
                        await context.bot.send_message(
                            tid,
                            f"⚠️ لم أنفّذ خروج `{symbol}` بعد لأن أوامر DCA لم تُلغَ/تُحسم بأمان. راجع MEXC: "
                            + "; ".join(dca_cancel_errors[:3]),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    continue
                if changed_by_dca:
                    # The order filled during the exit race. The position basis and
                    # protected quantity changed, so reevaluate on the next monitor pass.
                    logger.info("DCA fill raced with exit action for %s; defer exit to next monitor cycle", symbol)
                    continue

            def record_reconciled_tp_fills(fills):
                """Persist limit-order fills discovered during an SL cancel race."""
                for fill in fills or []:
                    amount = max(0.0, float(fill.get("filled_amount") or 0))
                    fill_price = float(fill.get("fill_price") or 0)
                    if amount <= 0 or fill_price <= 0 or not pf:
                        continue
                    stage = str(fill.get("stage") or "tp")
                    entry_price = float(coin.entry_price or 0)
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, stage,
                        entry_price, fill_price, amount,
                        (fill_price - entry_price) * amount,
                        details="TP limit fill reconciled while cancelling for stop loss",
                    )

            def clear_cancelled_tp_ids(order_ids):
                wanted = {str(order_id) for order_id in (order_ids or [])}
                updates = {
                    key: None
                    for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id")
                    if getattr(coin, key, None) and str(getattr(coin, key)) in wanted
                }
                if updates:
                    update_coin_position(db, coin.id, **updates)

            if act["action"] == "manual_tp_order_cleared":
                key = act.get("clear_order_key")
                if key in ("tp1_order_id", "tp2_order_id", "tp3_order_id"):
                    update_coin_position(db, coin.id, **{key: None})
                continue

            if act["action"] == "sl_trail_updated":
                update_coin_position(
                    db, coin.id,
                    current_sl_price=float(act.get("new_sl") or coin.current_sl_price or 0),
                )
                continue

            if act["action"] == "manual_tp_partial":
                remaining_before = max(
                    0.0, position_remaining(coin)
                )
                filled_amount = min(
                    remaining_before, max(0.0, float(act.get("filled_amount") or 0))
                )
                fill_price = float(act.get("fill_price") or act.get("price") or 0)
                entry_price = float(coin.entry_price or 0)
                if filled_amount > 0 and fill_price > 0 and pf:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, act.get("stage") or "tp",
                        entry_price, fill_price, filled_amount,
                        (fill_price - entry_price) * filled_amount,
                        details="Cancelled TP limit order had a partial fill",
                    )
                key = act.get("clear_order_key")
                updates = {
                    "remaining_amount": max(0.0, remaining_before - filled_amount),
                }
                # Once a cancelled TP has any confirmed fill, treat that stage
                # as consumed. This avoids re-selling its already-filled slice
                # through the local fallback target.
                if act.get("stage") == "tp1":
                    updates["position_status"] = "tp1_hit"
                    updates["current_sl_price"] = max(
                        float(coin.current_sl_price or 0),
                        float(coin.entry_price or 0),
                    )
                elif act.get("stage") == "tp2":
                    updates["position_status"] = "tp2_hit"
                    updates["current_sl_price"] = max(
                        float(coin.current_sl_price or 0),
                        float(coin.tp1_price or coin.entry_price or 0),
                    )
                if key in ("tp1_order_id", "tp2_order_id", "tp3_order_id"):
                    updates[key] = None
                update_coin_position(db, coin.id, **updates)
                continue

            if act["action"] == "tp_full_close":
                # Spot balance is shared across all portfolios using these API keys.
                # A zero balance observed by one position must not erase another allocation.
                other_allocation = db.query(PortfolioCoin).join(Portfolio).filter(
                    PortfolioCoin.symbol == symbol,
                    PortfolioCoin.portfolio_id != pf.id,
                    Portfolio.status == "active",
                    PortfolioCoin.remaining_amount > 0,
                ).first()
                if other_allocation:
                    logger.warning(
                        "Preserving %s portfolio=%s: another portfolio still tracks this shared asset",
                        symbol, pf.id,
                    )
                    continue
                # Only confirmed TP fills are booked as trades; a zero-balance
                # reconciliation alone does not prove fill quantity or execution price.
                remaining_before = position_remaining(coin)
                fill_price = float(act.get("fill_price") or act.get("price") or 0)
                stage = act.get("stage") or "tp"
                filled_raw = act.get("filled_amount")
                filled_amount = min(remaining_before, max(0.0, float(filled_raw or 0)))
                record_reconciled_tp_fills(act.get("tp_fills"))
                # A balance-zero observation is not proof that the tracked quantity
                # was filled at the current ticker price. Never fabricate a trade/PnL.
                if act.get("tp_fills") and filled_amount > 0 and fill_price and float(coin.entry_price or 0) > 0:
                    pnl = (fill_price - float(coin.entry_price)) * filled_amount
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "balance_sync",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details=f"مزامنة تعبئات TP مؤكدة ({stage})",
                    )
                update_coin_position(
                    db, coin.id,
                    position_status="idle",
                    current_sl_price=0.0,
                    remaining_amount=0.0,
                    amount=0.0,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    tp_order_id=None,
                )
                remove_coin_from_portfolio(db, pf.id, symbol)
                logger.info(
                    "balance_zero sync silent %s pf=%s remaining_was=%.6g",
                    symbol, getattr(pf, "id", None), remaining_before,
                )

            elif act["action"] == "tp1_hit":
                remaining_before = position_remaining(coin)
                filled_raw = act.get("filled_amount")
                filled_amount = float(filled_raw) if filled_raw is not None else 0.0
                if filled_amount <= 0:
                    logger.warning("Ignoring TP1 action without confirmed fill: %s", symbol)
                    continue
                filled_amount = min(filled_amount, remaining_before)
                new_rem = max(0.0, remaining_before - filled_amount)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp1_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                if new_rem <= max(1e-12, remaining_before * 1e-8):
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp1",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP1 full close",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="closed",
                        current_sl_price=0.0,
                        remaining_amount=0.0,
                        amount=0.0,
                        tp1_order_id=None,
                        tp2_order_id=None,
                        tp3_order_id=None,
                        tp_order_id=None,
                    )
                    remove_coin_from_portfolio(db, pf.id, symbol)
                    msg = (
                        f"✅ *إغلاق كامل (هدف 1)* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                else:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp1",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP1 filled",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="tp1_hit",
                        current_sl_price=float(
                            act.get("new_sl") if act.get("new_sl") is not None else (coin.entry_price or 0)
                        ),
                        remaining_amount=new_rem,
                        tp1_order_id=None,
                    )
                    tp1_protection = f"تم نقل الاستوب إلى سعر الدخول `{coin.entry_price:.6g}`"
                    msg = (
                        f"🎯 *تحقق الهدف 1* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"{tp1_protection}\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "tp2_hit":
                remaining_before = position_remaining(coin)
                filled_raw = act.get("filled_amount")
                filled_amount = float(filled_raw) if filled_raw is not None else 0.0
                if filled_amount <= 0:
                    logger.warning("Ignoring TP2 action without confirmed fill: %s", symbol)
                    continue
                filled_amount = min(filled_amount, remaining_before)
                new_rem = max(0.0, remaining_before - filled_amount)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp2_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                if new_rem <= max(1e-12, remaining_before * 1e-8):
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp2",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP2 full close",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="closed",
                        current_sl_price=0.0,
                        remaining_amount=0.0,
                        amount=0.0,
                        tp1_order_id=None,
                        tp2_order_id=None,
                        tp3_order_id=None,
                        tp_order_id=None,
                    )
                    remove_coin_from_portfolio(db, pf.id, symbol)
                    msg = (
                        f"✅ *إغلاق كامل (هدف 2)* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                else:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp2",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP2 filled",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="tp2_hit",
                        current_sl_price=float(
                            act.get("new_sl") if act.get("new_sl") is not None else (coin.current_sl_price or 0)
                        ),
                        remaining_amount=new_rem,
                        tp2_order_id=None,
                    )
                    tp2_protection = (
                        f"تم رفع الاستوب إلى `{float(act.get('new_sl') or 0):.6g}`"
                    )
                    tp2_mode = "(الجزء المتبقي محمي باستوب متحرك)"
                    msg = (
                        f"🎯 *تحقق الهدف 2* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"{tp2_protection}\n"
                        f"{tp2_mode}\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "tp3_hit":
                remaining_before = position_remaining(coin)
                filled_raw = act.get("filled_amount")
                filled_amount = float(filled_raw) if filled_raw is not None else 0.0
                if filled_amount <= 0:
                    logger.warning("Ignoring TP3 action without confirmed fill: %s", symbol)
                    continue
                filled_amount = min(filled_amount, remaining_before)
                new_rem = max(0.0, remaining_before - filled_amount)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp3_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "tp3",
                    coin.entry_price, fill_price, filled_amount, pnl,
                    details="TP3 filled",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="closed" if new_rem <= max(1e-12, remaining_before * 1e-8) else "tp2_hit",
                    current_sl_price=float(coin.current_sl_price or 0),
                    remaining_amount=0.0 if new_rem <= max(1e-12, remaining_before * 1e-8) else new_rem,
                    amount=0.0 if new_rem <= max(1e-12, remaining_before * 1e-8) else new_rem,
                    tp3_order_id=None,
                )
                if new_rem <= max(1e-12, remaining_before * 1e-8):
                    remove_coin_from_portfolio(db, pf.id, symbol)
                msg = (
                    f"🎯 *تحقق الهدف 3* — `{symbol}`\n"
                    f"المنفذ: `{filled_amount:.8g}`، المتبقي: `{new_rem:.8g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "sl_hit_wait_reentry":
                update_coin_position(
                    db, coin.id,
                    position_status="waiting_reentry",
                    current_sl_price=0.0,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    amount=0.0,
                    reentry_price=act["reentry_price"],
                    reentry_touched=False,
                    reentry_used=False,
                )
                msg = (
                    f"🛡 *ضرب الاستوب{' المرفوع' if act.get('was_raised') else ''}* — `{symbol}`\n"
                    f"تم البيع ≈ `{act['price']:.6g}`\n"
                    f"⏳ انتظار إعادة دخول عند الاستوب الأصلي `{act['reentry_price']:.6g}`\n"
                    f"(ارتداد 1.5% فوق سعر البيع)\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "reentry_touched":
                update_coin_position(db, coin.id, reentry_touched=True)
                msg = (
                    f"📍 *لمس منطقة إعادة الدخول* — `{symbol}`\n"
                    f"السعر `{act['price']:.6g}` ≤ `{act['reentry_price']:.6g}`\n"
                    f"في انتظار ارتداد +1.5% فوق سعر البيع للشراء..."
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "reentry_buy":
                # Re-entry is another buy against the same shared spot account.
                other_allocation = db.query(PortfolioCoin).join(Portfolio).filter(
                    PortfolioCoin.symbol == symbol,
                    PortfolioCoin.portfolio_id != pf.id,
                    Portfolio.status == "active",
                    PortfolioCoin.remaining_amount > 0,
                ).first()
                if other_allocation:
                    logger.warning(
                        "Skipping automatic re-entry for shared asset %s portfolio=%s",
                        symbol, pf.id,
                    )
                    try:
                        await context.bot.send_message(
                            tid,
                            f"⛔ تم تخطي إعادة دخول `{symbol}` لأن العملة مخصصة لمحفظة أخرى على حساب MEXC نفسه.",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    continue
                # Re-enter only with the USDT value that was actually sold at
                # the stop. Never buy the full equal portfolio allocation here.
                user = get_or_create_user(db, tid)
                def _pct(pf_val, user_val, default):
                    if pf_val is not None and float(pf_val) > 0:
                        return float(pf_val)
                    if user_val is not None and float(user_val) > 0:
                        return float(user_val)
                    return default
                tp1 = _resolve_optional_pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
                tp2 = _resolve_optional_pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
                tp3 = _resolve_optional_pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
                s1 = _resolve_optional_pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
                s2 = _resolve_optional_pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
                sl_pct = _resolve_stop_loss_pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
                n_coins = max(1, len(pf.coins) if pf else 1)
                usdt = float(act.get("reentry_amount_usdt") or 0.0)
                if usdt <= 0:
                    usdt = (pf.investment_usdt if pf else 20) / n_coins
                if usdt < float(config.PORTFOLIO_MIN_ORDER_USDT):
                    return {
                        "symbol": symbol,
                        "error": (
                            f"حصة إعادة الدخول {usdt:.2f}$ أقل من الحد الأدنى "
                            f"{config.PORTFOLIO_MIN_ORDER_USDT:.2f}$"
                        ),
                    }
                free_raw = float(await asyncio.get_event_loop().run_in_executor(None, get_mexc().get_free_usdt) or 0.0)
                spendable = spendable_usdt_after_reserves(free_raw, _cloud_dca_reserved_usdt(db))
                if spendable + 1e-9 < usdt:
                    logger.warning(
                        "Skipping automatic re-entry %s: spendable USDT %.4f below %.4f after DCA reserves",
                        symbol, spendable, usdt,
                    )
                    try:
                        await context.bot.send_message(
                            tid,
                            f"⚠️ تم تأجيل إعادة دخول `{symbol}` لحماية احتياطي DCA. "
                            f"المتاح بعد الاحتياطي `{spendable:.2f}` USDT، والمطلوب `{usdt:.2f}`.",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    continue
                loop = asyncio.get_event_loop()
                buy_res = await loop.run_in_executor(
                    None,
                    lambda: get_reb().reentry_buy_and_place_tp(
                        symbol, usdt, tp1, tp2, tp3, sl_pct, s1, s2
                    ),
                )
                if buy_res.get("error"):
                    if _is_silent_reentry_balance_error(buy_res.get("error")):
                        logger.info(
                            "Automatic re-entry skipped quietly for %s: exchange balance unavailable",
                            symbol,
                        )
                    else:
                        try:
                            await context.bot.send_message(
                                tid,
                                f"⚠️ فشل إعادة دخول `{symbol}`: `{buy_res['error']}`",
                                parse_mode="Markdown",
                            )
                        except Exception:
                            pass
                else:
                    update_coin_position(
                        db, coin.id,
                        entry_price=buy_res.get("entry_price", act["price"]),
                        tp1_price=buy_res.get("tp1_price", 0),
                        tp2_price=buy_res.get("tp2_price", 0),
                        tp3_price=buy_res.get("tp3_price", 0),
                        current_sl_price=buy_res.get("sl_price", 0),
                        original_sl_price=buy_res.get("original_sl_price") or buy_res.get("sl_price", 0),
                        amount=buy_res.get("amount", 0),
                        remaining_amount=buy_res.get("amount", 0),
                        tp1_order_id=buy_res.get("tp1_order_id"),
                        tp2_order_id=buy_res.get("tp2_order_id"),
                        tp3_order_id=buy_res.get("tp3_order_id"),
                        position_status="open",
                        reentry_used=False,
                        reentry_touched=False,
                        reentry_price=0.0,
                    )
                    # The automatic buy consumed the same stop-loss offer;
                    # stale manual callbacks must never be able to buy again.
                    mark_reentry_events_used(db, pf.id, symbol)
                    msg = (
                        f"🔄 *إعادة دخول* — `{symbol}`\n"
                        f"شراء عند ≈ `{buy_res.get('entry_price', act['price']):.6g}`\n"
                        f"وقف متحرك | SL `{buy_res.get('sl_price', 0):.6g}`\n"
                        f"وقف متحرك مفعّل — إعادة الدخول متاحة مرة أخرى بعد أي استوب\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                    try:
                        await context.bot.send_message(tid, msg, parse_mode="Markdown")
                    except Exception:
                        pass

            elif act["action"] == "sl_sell_partial":
                record_reconciled_tp_fills(act.get("tp_fills"))
                filled_amount = float(act.get("filled_amount") or 0)
                remaining_amount = float(act.get("remaining_amount") or 0)
                exit_price = float(act.get("price") or 0)
                sold_value = filled_amount * exit_price
                entry_price = float(coin.entry_price or 0)
                if filled_amount > 0:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "stop_loss_partial",
                        entry_price, exit_price,
                        filled_amount,
                        (exit_price - entry_price) * filled_amount,
                        details="Stop loss partially filled; remaining amount is retried",
                    )
                update_coin_position(
                    db, coin.id,
                    position_status=act.get("position_status") or coin.position_status,
                    remaining_amount=remaining_amount,
                    reentry_price=float(act.get("reentry_price") or 0),
                    reentry_touched=False,
                    reentry_used=False,
                    current_sl_price=float(act.get("sl") or coin.current_sl_price or 0),
                )
                clear_cancelled_tp_ids(act.get("cancelled_tp_order_ids"))
                try:
                    await context.bot.send_message(
                        tid,
                        f"🛡 *ضرب الاستوب — تنفيذ جزئي — {symbol}*\n"
                        f"تم بيع: `{filled_amount:.8g}` وحدة\n"
                        f"بسعر تقريبي: `{exit_price:.8g}` USDT\n"
                        f"قيمة البيع: *`{sold_value:.2f}` USDT*\n"
                        f"المتبقي: `{remaining_amount:.8g}` وحدة\n"
                        f"إعادة الدخول ستكون بقيمة البيع فقط: *`{sold_value:.2f}` USDT*\n"
                        "المتبقي محفوظ ولن يُباع مرة أخرى بسبب هذا الاستوب.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            elif act["action"] == "sl_hit_sold":
                record_reconciled_tp_fills(act.get("tp_fills"))
                exit_price = float(act.get("price") or 0)
                stopped_amount = float(act.get("amount") or 0)
                sold_value = stopped_amount * exit_price
                entry_price = float(coin.entry_price or 0)
                pnl = (exit_price - entry_price) * stopped_amount
                reentry_available = bool(act.get("reentry_available"))
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "stop_loss",
                    entry_price, exit_price, stopped_amount, pnl,
                    reentry_available=reentry_available,
                    details="Stop loss filled",
                )
                # Auto re-entry: park in waiting_reentry with trigger price.
                # Manual button still available as backup.
                # دائماً متاح لإعادة الدخول — مش مرة واحدة
                reentry_available = True
                trigger = float(act.get("reentry_price") or 0)
                update_coin_position(
                    db, coin.id,
                    position_status="waiting_reentry",
                    current_sl_price=float(act.get("sl") or 0) or 0.0,
                    tp_order_id=None,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    amount=0.0,
                    remaining_amount=0.0,
                    reentry_price=trigger,
                    reentry_touched=False,
                    reentry_used=False,
                )
                raised = " (بعد التريلينج)" if act.get("was_raised") else ""
                if trigger > 0:
                    extra = (
                        f"⏳ إعادة دخول تلقائي عند الصعود فوق `{trigger:.6g}` "
                        f"(+1.5% من سعر البيع) — متكرر بعد أي استوب"
                    )
                else:
                    extra = "إعادة الدخول التلقائي مفعّلة."
                msg = (
                    f"🛡 *ضرب الاستوب{raised}* — `{symbol}`\n"
                    f"تم بيع: `{stopped_amount:.8g}` وحدة\n"
                    f"بسعر السوق ≈ `{exit_price:.8g}` USDT\n"
                    f"قيمة البيع: *`{sold_value:.2f}` USDT*\n"
                    f"النتيجة: `{pnl:+.2f}` USDT\n"
                    f"إعادة الدخول بنفس قيمة البيع: *`{sold_value:.2f}` USDT*\n"
                    "سماح التنفيذ حول السعر: 1%\n"
                    f"{extra}\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(
                        tid,
                        msg,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                "🔄 عرض العملات وإعادة الدخول",
                                callback_data=f"stopped_{pf.id}",
                            )]
                        ]) if reentry_available else None,
                    )
                except Exception:
                    pass

            elif act["action"] == "sl_sell_failed":
                if act.get("cancelled_tp_order_ids") is not None:
                    record_reconciled_tp_fills(act.get("tp_fills"))
                    if act.get("manual_reconciliation_required"):
                        # Freeze this row out of automatic SL retries rather
                        # than risking a second sale against inconsistent
                        # exchange/database quantities.
                        update_coin_position(
                            db, coin.id, position_status="reconcile_required"
                        )
                    else:
                        update_coin_position(
                            db, coin.id,
                            position_status=act.get("position_status") or coin.position_status,
                            remaining_amount=max(
                                0.0,
                                float(act.get("remaining_amount") or 0),
                            ),
                        )
                    clear_cancelled_tp_ids(act.get("cancelled_tp_order_ids"))
                alert_key=(tid, getattr(act, "coin_id", None) if not isinstance(act, dict) else act.get("coin_id"), symbol)
                now=time.time()
                if now-_SL_FAILURE_ALERTS.get(alert_key, 0) < 60:
                    continue
                _SL_FAILURE_ALERTS[alert_key]=now
                try:
                    await context.bot.send_message(
                        tid,
                        f"⚠️ فشل بيع `{symbol}` بعد ضرب الاستوب:\n`{act.get('error')}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
    except Exception as exc:
        cycle_error = str(exc)
        logger.exception("monitor_positions_job error")
    finally:
        _MONITOR_HEALTH.update(
            last_finished_at=time.time(),
            last_duration=max(0.0, time.time() - cycle_started),
            last_positions=cycle_positions,
            last_actions=cycle_actions,
            last_error=cycle_error,
        )
        db.close()


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN مطلوب")
    if not config.MEXC_API_KEY or not config.MEXC_API_SECRET:
        raise SystemExit("MEXC_API_KEY و MEXC_API_SECRET مطلوبان")
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL مطلوب")

    # Railway may briefly keep the old container alive during a rolling deploy.
    # Wait for its advisory lock to be released instead of exiting immediately
    # and entering a restart loop with misleading 409 messages.
    lock_acquired = False
    for attempt in range(24):
        if acquire_singleton_lock():
            lock_acquired = True
            break
        logger.warning(
            "نسخة أخرى تعمل؛ انتظار تحرير قفل Telegram (%s/24)",
            attempt + 1,
        )
        time.sleep(5)
    if not lock_acquired:
        raise SystemExit(
            "تعذر تحرير قفل النسخة القديمة بعد دقيقتين؛ أوقف النسخة القديمة في Railway ثم أعد التشغيل."
        )

    init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.bot_data["get_mexc"] = get_mexc

    # Fast cloud monitor for TP/SL; one complete market sell is safer than
    # sequential chunks because it minimizes time exposed during a fast drop.
    if app.job_queue:
        app.job_queue.run_repeating(
            monitor_positions_job,
            interval=config.STOP_MONITOR_INTERVAL_SECONDS,
            first=15,
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
        logger.info(
            "Position monitor job scheduled (every %.1fs)",
            config.STOP_MONITOR_INTERVAL_SECONDS,
        )
    else:
        logger.warning("JobQueue not available — install python-telegram-bot[job-queue]")

    async def entry_create(update, context):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        if not await ensure_admin(update):
            return ConversationHandler.END
        context.user_data["create"] = {}
        await query.edit_message_text(
            "📝 أرسل *اسم المحفظة*:",
            parse_mode="Markdown",
        )
        return CREATE_NAME

    async def entry_addcoin(update, context):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        if not await ensure_admin(update):
            return ConversationHandler.END
        data = query.data or ""
        pf_id = int(data.split("_")[1])
        context.user_data["addcoin_pf"] = pf_id
        await query.edit_message_text("أرسل رمز العملة (مثال: BTC أو ETH):")
        return ADD_COIN

    async def entry_increase(update, context):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        if not await ensure_admin(update):
            return ConversationHandler.END
        data = query.data or ""
        pf_id = int(data.split("_")[2] if data.startswith("edit_amount_") else data.split("_")[1])
        context.user_data["edit_amount_pf"] = pf_id
        context.user_data["increase_pf"] = pf_id
        await query.edit_message_text(
            "💰 *تعديل المبلغ المخصص*\n"
            "أرسل إجمالي ميزانية المحفظة الجديدة بالـ USDT.\n"
            "لن يتم تنفيذ شراء أو بيع تلقائي؛ استخدم إعادة البناء للمطابقة.",
            parse_mode="Markdown",
        )
        return INCREASE_AMOUNT

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_create, pattern="^create_pf$"),
            CallbackQueryHandler(entry_addcoin, pattern="^addcoin_"),
            CallbackQueryHandler(entry_increase, pattern="^(increase_|edit_amount_)"),
        ],
        states={
            CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_name)],
            CREATE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_amount)],
            CREATE_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_coins)],
            ADD_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_coin_msg)],
            INCREASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, increase_amount_msg)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    logger.info("Bot starting (MEXC Portfolio Manager)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
