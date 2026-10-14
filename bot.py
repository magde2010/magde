"""
    MEXC Portfolio Manager
    بوت تليجرام لإدارة محافظ متعددة على MEXC Spot بأهداف ووقف خسارة يدويين.
"""
import logging
import asyncio
import time
from typing import Optional, Dict
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import config
from database import (
    init_db, SessionLocal, get_or_create_user, get_portfolios, get_portfolio,
    create_portfolio, add_coin_to_portfolio, remove_coin_from_portfolio,
    close_portfolio, delete_portfolio_completely, clear_coin_position,
    delete_orphaned_portfolio_records,
    set_portfolio_running, log_action,
    Portfolio, PortfolioCoin, PortfolioTrade, RebalanceLog,
    update_coin_position, reset_coin_positions, get_open_positions,
    record_trade_event, get_trade_event,
    get_portfolio_trade_events, mark_reentry_events_used,
    reset_portfolio_cycle_state, acquire_singleton_lock,
)
from mexc_client import MexcClient
from rebalancer import Rebalancer
import ui_theme as ui
from portfolio_controls import calculate_portfolio_entry_budget, calculate_safe_trim_amount, calculate_outstanding_trim_amounts

_SL_FAILURE_ALERTS = {}


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

_mexc: Optional[MexcClient] = None
_reb: Optional[Rebalancer] = None

# MEXC Spot minimum order notional requested for portfolio buys/sells.
PORTFOLIO_MIN_ORDER_USDT = 2.0
_ACTIVE_PORTFOLIO_TRIMS = set()


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


def get_reb() -> Rebalancer:
    global _reb
    if _reb is None:
        _reb = Rebalancer(get_mexc())
    return _reb


def is_admin(user_id: int) -> bool:
    if not config.ADMIN_TELEGRAM_ID:
        return True
    return user_id == config.ADMIN_TELEGRAM_ID


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
        [ui.button("▦  نظام Grid", "grid_menu")],
        [ui.button(f"{ui.I['cleanup']}  تنظيف البيانات", "cleanup_db")],
    ])


def pf_keyboard(pf_id: int, is_running: bool):
    """أزرار المحفظة — شكل موحد دون تغيير أي callback."""
    rows = [
        ui.row(
            (f"{ui.I['stop'] if is_running else ui.I['play']}  {'إيقاف' if is_running else 'تشغيل'}", f"toggle_{pf_id}"),
            (f"{ui.I['target']}  الأهداف", f"pf_tpsl_{pf_id}"),
        ),
        ui.row((f"{ui.I['add']}  إضافة عملة", f"addcoin_{pf_id}"), (f"{ui.I['remove']}  إزالة عملة", f"removecoin_{pf_id}")),
        [ui.button("✂️  تخفيف الحيازة 25% / 50% / 70%", f"trim_menu_{pf_id}")],
        [ui.button("↩  استرجاع الكميات التي تم تخفيفها", f"restore_trim_menu_{pf_id}")],
        [ui.button(f"{ui.I['edit']}  تعديل المبلغ المخصص", f"edit_amount_{pf_id}")],
        ui.row((f"↻  إعادة بناء", f"rebuild_{pf_id}"), (f"{ui.I['delete']}  حذف", f"delete_pf_{pf_id}")),
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
        rem = float(coin.remaining_amount or coin.amount or 0)
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
        for coin in p.coins:
            rem = float(coin.remaining_amount or coin.amount or 0)
            entry = float(coin.entry_price or 0)
            px = float(prices.get(coin.symbol) or entry or 0)
            value = rem * px
            cpnl = value - (rem * entry) if rem > 0 and entry > 0 else 0.0
            icon = "🟢" if cpnl >= 0 else "🔴"
            coin_lines.append(
                f"{icon} *{coin.symbol}*: `{value:.2f}` / `{target_per_coin:.2f}` USDT  `{cpnl:+.2f}`"
            )
        coins_block = "\n".join(coin_lines)
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
        ui.page_title(ui.I["portfolio"], f"{p.name}", health),
        f"◦ التحكم: *{mode}*",
        f"◦ المخصص: *{allocated:.0f}* USDT",
        f"◦ العملات: *{n_coins}*  ·  لكل عملة: *{per_coin:.2f}$*",
        f"◦ بداية الدورة: *{baseline:.2f}$*",
        f"◦ القيمة الحالية: *{open_value:.2f}$*",
        f"{t_emoji} *النتيجة: {total_pnl:+.2f}$  ({total_pct:+.2f}%)*",
        f"◦ الحالة: *{health}*",
    ]
    if abs(realized) > 0.0001:
        r_emoji = "🟢" if realized >= 0 else "🔴"
        out.append(f"{r_emoji} محقق منذ إعادة البناء: *{realized:+.2f}* $")
    r24 = "🟢" if realized_24h >= 0 else "🔴"
    out.append(f"{r24} محقق آخر 24 ساعة: *{realized_24h:+.2f}* $")
    out.append("")
    out.append(f"العملات ({len(symbols)})")
    out.append("_لكل عملة: القيمة الحالية / المخصص المستهدف / الربح والخسارة._")
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
        remaining = float(coin.remaining_amount or coin.amount or 0)
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


def _build_cleanup_plan(db, telegram_id: int, presence: Dict[str, Dict[str, float]]) -> Dict:
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


def _cleanup_report(plan: Dict) -> str:
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
            "سيتم إلغاء أوامر الأهداف المرتبطة بالبيانات القديمة ثم حذفها فقط بعد إعادة الفحص.",
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
        ui.page_title("◆", "LOTFE", "إدارة محافظ MEXC Spot بواجهة عصرية وواضحة.") +
        "\nإدارة المحافظ • إعادة الموازنة وإدارة المراكز\n"
        "اختر القسم المطلوب من القائمة أدناه.",
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
    if not update.message or not update.message.text:
        return
    if not await ensure_admin(update):
        return

    text = update.message.text
    tid = update.effective_user.id

    grid_field = context.user_data.pop("grid_setting", None)
    if grid_field:
        from grid_integration import update_grid_setting
        ok, message = update_grid_setting(grid_field, text)
        await update.message.reply_text(
            ("✅ " if ok else "⚠️ ") + message,
            reply_markup=main_menu_keyboard() if ok else None,
        )
        return

    if context.user_data.get("waiting"):
        # تعديل إعدادات المحافظ العامة
        user_field = context.user_data.get("edit_user_tpsl")
        if user_field:
            try:
                val = float(text.strip().replace("%", "").replace(",", "."))
                if val < 0 or val > 100:
                    await update.message.reply_text("أدخل رقم بين 0 و 100.")
                    return
                db = SessionLocal()
                try:
                    user = get_or_create_user(db, update.effective_user.id)
                    if hasattr(user, user_field):
                        setattr(user, user_field, val)
                        # انسخ إعدادات التريل لكل محافظ المستخدم
                        if user_field in ("trail_pct", "be_lock_pct", "stop_loss_pct"):
                            for pf in db.query(Portfolio).filter(
                                Portfolio.telegram_id == update.effective_user.id,
                                Portfolio.status == "active",
                            ).all():
                                setattr(pf, user_field, val)
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
    if data == "grid_settings":
        from grid_integration import get_grid_service
        service, error = get_grid_service()
        if error:
            await query.edit_message_text(f"⚠️ {error}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ رجوع", callback_data="grid_menu")]]))
            return
        c = service.cfg
        await query.edit_message_text(
            "⚙️ *إعدادات Grid*\n\n"
            f"العملة: `{c.symbol}`\nالميزانية: `{c.total_budget}` USDT\nالدخول الأولي: `{c.initial_pct}%`\n"
            f"الوضع: `{'DRY RUN' if c.dry_run else 'LIVE'}`\n"
            f"DCA هبوط: `{','.join(map(str, c.dca_drops))}%`\n"
            f"DCA توزيع: `{','.join(map(str, c.dca_allocations))}%`\n"
            f"TP أهداف: `{','.join(map(str, c.tp_gains))}%`\n"
            f"TP بيع: `{','.join(map(str, c.tp_sell_pcts))}%`\n"
            f"إعادة التمركز: `{c.upward_recenter_percent}%`\n\n"
            "أوقف Grid قبل تعديل أي إعداد.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("العملة", callback_data="grid_set_symbol"), InlineKeyboardButton("الميزانية", callback_data="grid_set_budget")],
                [InlineKeyboardButton("الدخول الأولي %", callback_data="grid_set_initial"), InlineKeyboardButton("إعادة التمركز %", callback_data="grid_set_recenter")],
                [InlineKeyboardButton("هبوط DCA الأربعة", callback_data="grid_set_dca_drops")],
                [InlineKeyboardButton("توزيع DCA الأربعة", callback_data="grid_set_dca_allocations")],
                [InlineKeyboardButton("أهداف TP الأربعة", callback_data="grid_set_tp_gains")],
                [InlineKeyboardButton("نسب بيع TP الأربعة", callback_data="grid_set_tp_sell_pcts")],
                [InlineKeyboardButton("🔴 طلب التحويل إلى LIVE", callback_data="grid_live_request"), InlineKeyboardButton("🟡 العودة إلى Dry Run", callback_data="grid_dry_run")],
                [InlineKeyboardButton("↩ رجوع إلى Grid", callback_data="grid_menu")],
            ]),
        )
        return
    if data == "grid_live_request":
        from grid_integration import get_grid_service
        service, error = get_grid_service()
        if error:
            await query.edit_message_text(f"⚠️ {error}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ رجوع", callback_data="grid_settings")]]))
            return
        await query.edit_message_text(
            "⚠️ *تأكيد التداول الحقيقي*\n\n"
            f"العملة: `{service.cfg.symbol}`\n"
            f"إجمالي الميزانية: `{service.cfg.total_budget}` USDT\n"
            f"الدخول الأولي: `{service.cfg.initial_pct}%`\n"
            "بعد التأكيد سيتصل النظام بحساب MEXC ويرسل أوامر فعلية عند تشغيل Grid.\n"
            "تأكد أن مفتاح MEXC للسوق الفوري فقط وبدون صلاحية سحب.\n\n"
            "التأكيد لا يبدأ التداول تلقائيًا؛ سيبقى Grid متوقفًا.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ أؤكد التحويل إلى LIVE", callback_data="grid_live_confirm")],
                [InlineKeyboardButton("إلغاء", callback_data="grid_settings")],
            ]),
        )
        return
    if data in {"grid_live_confirm", "grid_dry_run"}:
        from grid_integration import switch_grid_mode
        ok, message = switch_grid_mode(data == "grid_live_confirm")
        await query.edit_message_text(
            ("✅ " if ok else "⚠️ ") + message,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ إعدادات Grid", callback_data="grid_settings"), InlineKeyboardButton("↩ Grid", callback_data="grid_menu")]]),
        )
        return
    if data.startswith("grid_set_"):
        field = data[len("grid_set_"):]
        prompts = {
            "symbol": "أرسل رمز العملة، مثال: BTC/USDT أو ETH/USDT",
            "budget": "أرسل إجمالي الميزانية بالدولار، مثال: 100",
            "initial": "أرسل نسبة الدخول الأولي، مثال: 50 (والباقي يذهب إلى DCA)",
            "recenter": "أرسل نسبة الصعود التي عندها يعاد تمركز الشبكة، مثال: 10",
            "dca_drops": "أرسل 4 نسب هبوط مفصولة بفواصل، مثال: 2.5,5,7.5,10",
            "dca_allocations": "أرسل 4 نسب توزيع DCA ومجموعها يجب أن يكون 100، مثال: 20,24,26,30",
            "tp_gains": "أرسل 4 أهداف ربح مفصولة بفواصل، مثال: 2,4,7,10",
            "tp_sell_pcts": "أرسل 4 نسب بيع ومجموعها يجب أن يكون 100، مثال: 25,25,25,25 أو 100,0,0,0",
        }
        if field not in prompts:
            return
        context.user_data["grid_setting"] = field
        await query.edit_message_text(f"⚙️ {prompts[field]}\n\nاكتب /cancel للإلغاء.")
        return
    if data == "grid_menu":
        from grid_integration import get_grid_service
        service, error = get_grid_service()
        if error:
            await query.edit_message_text(
                f"⚠️ {error}\n\nتأكد من إعدادات GRID_* ثم أعد تشغيل البوت.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ القائمة", callback_data="menu")]]),
            )
            return
        p = service.db.position()
        state = p.get("state") or "IDLE"
        mode = "DRY RUN" if service.cfg.dry_run else "LIVE"
        setup_note = "\n⚠️ أكمل اختيار العملة والميزانية من الإعدادات قبل التشغيل." if getattr(service, "needs_setup", False) else ""
        text = (
            "▦ *نظام Grid المستقل*\n\n"
            f"العملة: `{service.cfg.symbol}`\n"
            f"الوضع: `{mode}`\n"
            f"الحالة: `{state}`\n"
            f"الميزانية: `{service.cfg.total_budget}` USDT\n"
            f"المنصرف: `{p.get('spent', '0')}` USDT\n"
            f"المتبقي: `{p.get('remaining_budget', '0')}` USDT\n"
            f"المتوسط: `{p.get('average_entry', '0')}`\n\n"
            "النظام يبدأ متوقفًا. شغّله بعد مراجعة الإعدادات." + setup_note
        )
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ تشغيل Grid", callback_data="grid_run"), InlineKeyboardButton("⏸ إيقاف", callback_data="grid_stop")],
                [InlineKeyboardButton("🛑 إيقاف طوارئ", callback_data="grid_emergency")],
                [InlineKeyboardButton("📊 متابعة المستويات", callback_data="grid_levels")],
                [InlineKeyboardButton("♻️ إلغاء الأوامر وبناء الجديد", callback_data="grid_replace_confirm")],
                [InlineKeyboardButton("⚙️ إعدادات Grid", callback_data="grid_settings")],
                [InlineKeyboardButton("🔄 تحديث", callback_data="grid_menu"), InlineKeyboardButton("↩ القائمة", callback_data="menu")],
            ]),
        )
        return
    if data == "grid_levels":
        from grid_integration import get_grid_service
        service, error = get_grid_service()
        if error:
            await query.edit_message_text(f"⚠️ {error}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Grid", callback_data="grid_menu")]]))
            return
        await query.edit_message_text(
            "📊 *متابعة Grid التفصيلية*\n\n" + service.status_text(),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تحديث", callback_data="grid_levels")],
                [InlineKeyboardButton("⚙️ الإعدادات", callback_data="grid_settings"), InlineKeyboardButton("↩ Grid", callback_data="grid_menu")],
            ]),
        )
        return
    if data == "grid_replace_confirm":
        from grid_integration import get_grid_service
        service, error = get_grid_service()
        if error:
            await query.edit_message_text(f"⚠️ {error}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Grid", callback_data="grid_menu")]]))
            return
        await query.edit_message_text(
            "⚠️ *إعادة بناء أوامر Grid*\n\n"
            f"العملة المتأثرة فقط: `{service.cfg.symbol}`\n"
            "سيتم إلغاء أوامر Grid المفتوحة لهذه العملة، ثم بناء DCA وTP الجديدين حسب الإعدادات والميزانية.\n"
            "لن يتم بيع المركز ولن يتم لمس نظام المحافظ.\n\nهل تريد التنفيذ؟",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نفذ الآن", callback_data="grid_replace_execute")],
                [InlineKeyboardButton("إلغاء", callback_data="grid_menu")],
            ]),
        )
        return
    if data == "grid_replace_execute":
        from grid_integration import replace_grid_orders
        ok, message = replace_grid_orders()
        await query.edit_message_text(
            ("✅ " if ok else "⚠️ ") + message,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 متابعة المستويات", callback_data="grid_levels"), InlineKeyboardButton("↩ Grid", callback_data="grid_menu")]]),
        )
        return
    if data in {"grid_run", "grid_stop", "grid_emergency"}:
        from grid_integration import get_grid_service
        service, error = get_grid_service()
        if error:
            await query.edit_message_text(f"⚠️ {error}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ القائمة", callback_data="menu")]]))
            return
        if data == "grid_run":
            p = service.db.position()
            if getattr(service, "needs_setup", False):
                await query.answer("أكمل إعداد العملة والميزانية أولًا", show_alert=True)
                return
            if p.get("emergency_stop") or p.get("state") == "FROZEN":
                await query.answer("لا يمكن التشغيل: Emergency Stop أو FROZEN", show_alert=True)
                return
            service.db.update_position(running=1, state="ACTIVE")
            service.db.event("strategy_started", {"by": tid, "source": "portfolio_bot"})
            try:
                await service.tick()
            except Exception as exc:
                service.last_error = f"{type(exc).__name__}: {exc}"
                service.db.update_position(state="FROZEN")
                await query.answer("فشل التشغيل؛ افتح متابعة المستويات لمعرفة الخطأ", show_alert=True)
                await query.edit_message_text(
                    f"⚠️ فشل تشغيل Grid\n\n`{type(exc).__name__}: {exc}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 متابعة المستويات", callback_data="grid_levels")]]),
                )
                return
        elif data == "grid_stop":
            service.db.update_position(running=0, state="PAUSED")
            service.db.event("strategy_paused", {"by": tid, "source": "portfolio_bot"})
        else:
            service.db.update_position(emergency_stop=1, running=0, state="EMERGENCY_STOP")
            service.db.event("emergency_stop", {"by": tid, "source": "portfolio_bot"})
        await query.answer("تم تحديث حالة Grid")
        # Re-render the same screen after the state change.
        service, _ = get_grid_service()
        p = service.db.position()
        await query.edit_message_text(
            f"▦ *Grid* — `{p.get('state')}`\nالعملة: `{service.cfg.symbol}`\nالمنصرف: `{p.get('spent', '0')}` USDT\nالمتبقي: `{p.get('remaining_budget', '0')}` USDT",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ تشغيل", callback_data="grid_run"), InlineKeyboardButton("⏸ إيقاف", callback_data="grid_stop")],
                [InlineKeyboardButton("🛑 طوارئ", callback_data="grid_emergency")],
                [InlineKeyboardButton("📊 متابعة المستويات", callback_data="grid_levels")],
                [InlineKeyboardButton("⚙️ إعدادات Grid", callback_data="grid_settings")],
                [InlineKeyboardButton("🔄 تحديث", callback_data="grid_menu"), InlineKeyboardButton("↩ القائمة", callback_data="menu")],
            ]),
        )
        return
    if data == "menu":
        await query.edit_message_text(
            ui.page_title(ui.I["home"], "لوحة التحكم", "اختار القسم المطلوب من القائمة:") + ui.footer(),
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
            msg = (
                ui.page_title(ui.I["settings"], "الإعدادات العامة", "أهداف ووقف الخسارة للوضع اليدوي.") + "\n"
                "*الوضع اليدوي — أهداف ووقف الخسارة:*\n"
                f"TP1: `{user.tp1_pct or 3}%` | بيع: `{user.tp1_sell_pct or 40}%`\n"
                f"TP2: `{user.tp2_pct or 5}%` | بيع: `{user.tp2_sell_pct or 40}%`\n"
                f"وقف ابتدائي: `{user.stop_loss_pct or 3}%`\n\n"
                "أوامر TP توضع على المنصة، ووقف الخسارة الثابت يراقبه البوت.\n\n"
                f"أقصى عملات/محفظة: `{user.max_coins_per_portfolio or 30}`"
            )
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◎ TP1 %", callback_data="set_tp1_pct"),
                     InlineKeyboardButton("↘ بيع 1 %", callback_data="set_tp1_sell_pct")],
                    [InlineKeyboardButton("◎ TP2 %", callback_data="set_tp2_pct"),
                     InlineKeyboardButton("↘ بيع 2 %", callback_data="set_tp2_sell_pct")],
                    [InlineKeyboardButton("⛔ استوب %", callback_data="set_stop_loss_pct")],
                    [InlineKeyboardButton(f"{ui.I['back']}  القائمة", callback_data="menu")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("set_") and data in (
        "set_tp1_pct", "set_tp2_pct",
        "set_tp1_sell_pct", "set_tp2_sell_pct", "set_stop_loss_pct",
    ):
        field = data[len("set_"):]
        context.user_data["waiting"] = True
        context.user_data["edit_user_tpsl"] = field
        labels = {
            "tp1_pct": "هدف 1 % (مثال 8)",
            "tp2_pct": "هدف 2 % (مثال 15)",
            "tp1_sell_pct": "نسبة البيع عند الهدف 1 (يفضل 40)",
            "tp2_sell_pct": "نسبة البيع عند الهدف 2 (يفضل 40)",
            "stop_loss_pct": "وقف الخسارة الابتدائي %",
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
            usl = getattr(user, "stop_loss_pct", 3.0) or 3.0
            msg = (
                f"🎯 *أهداف المحفظة:* {pf.name}\n\n"
                f"TP1: {show(t1, ut1)}% | بيع: {show(s1, us1)}%\n"
                f"TP2: {show(t2, ut2)}% | بيع: {show(s2, us2)}%\n"
                f"المتبقي بعد TP2: 20% يعمل بوقف متحرك\n"
                f"استوب: {show(sl, usl)}%\n\n"
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
            held = sum(1 for c in (pf.coins or []) if float(c.remaining_amount or c.amount or 0) > 0)
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
            "اختار نسبة المخصص التي تريد استخدامها لشراء العملات الناقصة في كل محفظة.\n"
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
        await _do_remove_coin(query, tid, int(parts[1]), parts[2])
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
            finally:
                db.close()
            await query.edit_message_text(
                f"▶️ *تشغيل محفظة {name}*\n\n"
                f"المخصص المسجل: `{amount:.2f}` USDT\n"
                "اختار نسبة المخصص التي تستخدم لشراء العملات الناقصة فقط. هذه ليست نسبة بيع، ولن يشتري البوت عملة لها رصيد موجود بالفعل.\n"
                "الحد الأدنى للشراء 2 USDT لكل أمر؛ أي عملة لا تكفي حصتها لهذا الحد قد تُتخطى بدلًا من زيادة الميزانية تلقائيًا.",
                parse_mode="Markdown",
                reply_markup=portfolio_entry_pct_keyboard(pf_id),
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

    if data.startswith("rebuild_"):
        await _do_rebuild_positions(query, tid, int(data.split("_")[1]))
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


async def _do_rebuild_positions(query, tid, pf_id):
    """دورة كاملة داخل المحفظة:
    1) إلغاء كل أوامر البيع القديمة من المنصة + مسحها من القاعدة
    2) التعرف على الرصيد الفعلي
    3) شراء العملات الناقصة
    4) إعادة وضع أهداف TP/SL الجديدة
    """
    import time

    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not p.is_running:
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

        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            + ui.SEP + "\n"
            "① إلغاء الأهداف القديمة من المنصة...",

        )

        client = get_mexc()
        reb = get_reb()
        symbols = [c.symbol for c in p.coins]
        cancelled_count = 0
        cancel_errors = []

        # 1) Cancel DB-known TP orders + any open sells on exchange
        for coin in p.coins:
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
            # Clear order IDs from DB immediately
            update_coin_position(
                db, coin.id,
                tp_order_id=None,
                tp1_order_id=None,
                tp2_order_id=None,
                tp3_order_id=None,
            )

        # إعادة البناء تبدأ دورة جديدة؛ نمسح حالات TP/SL وpending/re-entry
        # مع الحفاظ على سجل PortfolioTrade والأرباح التاريخية.
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
        target_per_coin = allocated / n_coins if n_coins > 0 else 0.0
        free_usdt = client.get_free_usdt()

        # هدف كل عملة = التخصيص ÷ العدد — توازن حقيقي: بيع الزائد + تكميل الناقص
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
        total_sold_usdt = 0.0

        # 3أ) بيع الزائد حتى كل عملة ≈ الهدف (مايتخطاش المخصص للفرد)
        for coin, amt, price, val, need, excess in overweight:
            try:
                px = price if price > 0 else float(client.get_ticker_price(f"{coin.symbol}/{client.quote}") or 0)
                if px <= 0 or amt <= 0:
                    continue
                sell_amt = (excess / px) * 0.998
                free_amt = float(client.get_free_amount(coin.symbol) or 0)
                # بعد إلغاء الأوامر يفترض الرصيد حر؛ لو مقفول نحاول الإجمالي
                if free_amt < sell_amt * 0.5:
                    await asyncio.sleep(0.4)
                    free_amt = float(client.get_free_amount(coin.symbol) or 0)
                sell_amt = min(sell_amt, free_amt * 0.998) if free_amt > 0 else sell_amt
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
                got = sell_amt * px
                total_sold_usdt += got
                sold_trim.append(f"{coin.symbol}(-{got:.1f}$)")
                new_amt = max(0.0, amt - sell_amt)
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
                logger.exception("rebuild trim %s", coin.symbol)

        # حدّث الحضور بعد البيع
        await asyncio.sleep(1.0)
        presence = client.get_portfolio_presence(symbols)
        free_usdt = client.get_free_usdt()

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
        budget_left = max(0.0, allocated - cur_after) + total_sold_usdt * 0.1

        for coin, amt, price, val, need in under:
            # حدّث الرصيد الحر قبل كل شراء
            free_usdt = client.get_free_usdt()
            buy_amt = min(need, free_usdt * 0.995)
            if budget_left > 0:
                buy_amt = min(buy_amt, budget_left)
            if buy_amt < 1.0:
                buy_errors.append(
                    f"{coin.symbol}: يحتاج `{need:.2f}$` لكن USDT المتاح `{free_usdt:.2f}$`"
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
            time.sleep(0.35)

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

        for coin, amt, price, val, need, excess in coin_status:
            if coin.symbol in already_done:
                if coin.symbol not in refreshed:
                    refreshed.append(coin.symbol)
                continue
            try:
                free_amt = client.get_free_amount(coin.symbol)
                use_amt = free_amt if free_amt > 0 else amt
                use_amt = use_amt * 0.998
                entry = float(coin.entry_price or 0)
                if entry <= 0:
                    entry = price or client.get_ticker_price(f"{coin.symbol}/{client.quote}")
                if use_amt <= 0 or entry <= 0:
                    refresh_errors.append(f"{coin.symbol}: لا يوجد رصيد كافٍ")
                    continue
                # الوضع اليدوي فقط: استخدم النسب التي حددها المستخدم.
                use_tp1, use_tp2, use_tp3, use_sl = tp1, tp2, tp3, sl_pct
                result = reb.place_tp_orders(
                    [{
                        "symbol": coin.symbol,
                        "amount": use_amt,
                        "entry_price": entry,
                        "place_exchange_orders": False,
                    }],
                    use_tp1, use_tp2, use_tp3, use_sl, s1, s2,
                    control_mode="manual",
                )[0]
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

        # أساس حقيقي للحساب من الآن = قيمة المراكز بعد إعادة البناء
        try:
            from datetime import datetime as _dt
            final_value = 0.0
            for coin in p.coins:
                rem = float(coin.remaining_amount or coin.amount or 0)
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

        log_action(
            db, tid, "rebuild_positions",
            f"Rebuild {p.name}: target={target_per_coin:.2f} cancel={cancelled_count} "
            f"trim={sold_trim} buy={bought} top={topped} spent={total_spent:.2f} sold={total_sold_usdt:.2f}",
            not (buy_errors or refresh_errors),
            pf_id,
        )

        free_after = client.get_free_usdt()
        # Final report
        lines = [
            f"✅ *تمت إعادة بناء مراكز* `{p.name}`",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💰 التخصيص: `{allocated:.0f}$` ÷ `{n_coins}` = *`{target_per_coin:.2f}$`* لكل عملة",
            f"📊 كانت القيمة: `{total_value:.2f}$` | الناقص: `{total_need:.2f}$`",
            f"🗑 أوامر قديمة ملغاة: `{cancelled_count}`",
            f"🔻 بيع الزائد: `{len(sold_trim)}`" + (f" — {', '.join(f'`{x}`' for x in sold_trim[:8])}" if sold_trim else ""),
            f"🛒 عملات جديدة: `{len(bought)}`" + (f" — {', '.join(f'`{x}`' for x in bought)}" if bought else ""),
            f"📈 تم تكميل: `{len(topped)}`" + (f" — {', '.join(f'`{x}`' for x in topped[:8])}" if topped else ""),
            f"💵 صُرف: `{total_spent:.2f}$` | من البيع: `{total_sold_usdt:.2f}$` | USDT: `{free_after:.2f}$`",
            f"🎯 أهداف/وقف وُضعت لـ: `{len(refreshed)}` عملة",
            f"📌 أساس الحساب من الآن: *`{float(p.base_investment or 0):.2f}$`* (آخر إعادة بناء)",
        ]
        all_errs = cancel_errors[:3] + sell_errors + buy_errors + refresh_errors
        if all_errs:
            lines.append("\n⚠️ ملاحظات:")
            for e in all_errs[:10]:
                lines.append(f"• {e}")
            if any("USDT المتاح" in str(e) for e in buy_errors):
                lines.append("\n💡 حوّل USDT إضافي للحساب ثم أعد *إعادة بناء المراكز*.")
        await query.edit_message_text(
            "\n".join(lines),

            reply_markup=pf_keyboard(pf_id, True),
        )
    except Exception as exc:
        logger.exception("rebuild_positions failed")
        await query.edit_message_text(
            f"❌ فشل إعادة البناء:\n{str(exc)[:900]}",
            reply_markup=pf_keyboard(pf_id, True),
        )
    finally:
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
            free_usdt = await loop.run_in_executor(None, get_mexc().get_free_usdt)
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

        tp1 = pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

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


async def _do_manual_reentry(query, tid, event_id):
    """Buy a stopped coin again only after the user presses its button."""

    db = SessionLocal()
    try:
        event = get_trade_event(db, event_id, tid)
        if not event or event.event_type != "stop_loss":
            await query.edit_message_text("عملية إعادة الدخول غير متاحة أو تم تنفيذها مسبقاً.", reply_markup=main_menu_keyboard())
            return
        pf = get_portfolio(db, event.portfolio_id, tid)
        coin = next((c for c in (pf.coins if pf else []) if c.id == event.portfolio_coin_id), None)
        if not pf or not coin:
            await query.edit_message_text("المحفظة أو العملة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
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
        )
        event.reentry_available = False
        event.reentry_used = True
        db.commit()
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


async def _do_start(query, tid, pf_id, entry_pct=100):

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
                free_usdt = await loop.run_in_executor(None, lambda: float(get_mexc().get_free_usdt() or 0))
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
                await query.edit_message_text(
                    "⛔ *لا يمكن التشغيل بسبب الرصيد الحر*\n"
                    f"نسبة الدخول: `{entry_pct}%`\n"
                    f"ميزانية الشراء المقدرة للعملات الناقصة: `{purchase_total:.2f}` USDT\n"
                    f"المتاح: `{free_usdt:.2f}` USDT\n"
                    f"الناقص: `{purchase_total - free_usdt:.2f}` USDT",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                log_action(db, tid, "start_blocked_usdt", f"pct={entry_pct} need={purchase_total:.2f} free={free_usdt:.2f}", False, pf_id)
                return
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
        tp1 = _pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
        if True:
            # Keep the resolved user defaults on the portfolio so the monitor
            # uses exactly the same percentages after this session ends.
            p.tp1_pct, p.tp2_pct, p.tp3_pct = tp1, tp2, tp3
            p.tp1_sell_pct, p.tp2_sell_pct, p.stop_loss_pct = s1, s2, sl_pct
            db.flush()

        purchase_result = {"executed": [], "errors": []}
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
            if purchase_result.get("errors") and not purchase_result.get("executed"):
                err = "\n".join(str(e) for e in purchase_result["errors"])
                await query.edit_message_text(
                    f"❌ فشل شراء العملات الناقصة:\n`{err}`",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                return
        else:
            await query.edit_message_text("✅ الرصيد موجود. جاري تشغيل المحفظة بدون شراء جديد...")

        import time
        time.sleep(1.5)
        coins_data = []
        for c in p.coins:
            amount = get_mexc().get_free_amount(c.symbol)
            price = get_mexc().get_ticker_price(f"{c.symbol}/USDT")
            coins_data.append({
                "symbol": c.symbol,
                "amount": amount,
                "entry_price": price,
                "place_exchange_orders": False,
            })

        tp_results = get_reb().place_tp_orders(
            coins_data, tp1, tp2, tp3, sl_pct, s1, s2,
            control_mode="manual",
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
        for r in tp_results:
            coin_obj = next((c for c in p.coins if c.symbol == r["symbol"]), None)
            if not coin_obj:
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
            )
            if r.get("error"):
                lines.append(f"⚠️ `{r['symbol']}`: {r['error']}")
            lines.append(
                f"`{r['symbol']}` دخول `{r.get('entry_price',0):.6g}`\n"
                f"  TP1 `{r.get('tp1_price',0):.6g}` | TP2 `{r.get('tp2_price',0):.6g}` | "
                f"TP3 `{r.get('tp3_price',0):.6g}` | SL `{r.get('sl_price',0):.6g}`"
            )

        set_portfolio_running(db, pf_id, True)
        log_action(db, tid, "start", f"Started {p.name} multi-TP", True, pf_id)
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, True))
    finally:
        db.close()


async def _do_trim_holdings(query, tid, pf_id, pct):
    """Safely reduce tracked holdings by a selected percentage without stopping a portfolio."""
    if pct not in (25, 50, 70):
        await query.edit_message_text("نسبة تخفيف غير صالحة.", reply_markup=main_menu_keyboard())
        return
    lock_key = (int(tid), int(pf_id))
    if lock_key in _ACTIVE_PORTFOLIO_TRIMS:
        await query.edit_message_text("⏳ يجري بالفعل تنفيذ تخفيف لهذه المحفظة؛ انتظر النتيجة.")
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
        tp1 = _resolved(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolved(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolved(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        sell1 = _resolved(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        sell2 = _resolved(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _resolved(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        async def _restore_tp_orders(coin_obj, amount, reference_price):
            """Restore this running position's TP orders after a canceled trim attempt."""
            if not p.is_running or amount <= 0:
                return True
            try:
                tp_res = await loop.run_in_executor(
                    None,
                    lambda s=coin_obj.symbol, a=amount, e=reference_price: get_reb().place_tp_orders(
                        [{"symbol": s, "amount": a, "entry_price": e, "place_exchange_orders": False}],
                        tp1, tp2, tp3, sl_pct, sell1, sell2, control_mode="manual",
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
                    current_sl_price=tr.get("sl_price", coin_obj.current_sl_price),
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

            # The wallet is shared across portfolios; do not manually trim an asset
            # whose holding is tracked by another running portfolio.
            other = db.query(PortfolioCoin).join(Portfolio).filter(
                PortfolioCoin.symbol == symbol,
                PortfolioCoin.portfolio_id != p.id,
                Portfolio.telegram_id == tid,
                Portfolio.status == "active",
                Portfolio.is_running == True,
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
                    state = await loop.run_in_executor(None, lambda o=oid, s=symbol: client.fetch_order(o, f"{s}/{client.quote}"))
                    status = str((state or {}).get("status") or "").lower()
                    filled_on_order = float((state or {}).get("filled") or 0)
                    if status in {"closed", "filled"} and filled_on_order > 0:
                        cancel_problem = f"أمر TP `{oid}` يبدو أنه نُفّذ بالفعل؛ حدّث حالة المركز ثم أعد المحاولة"
                        break
                    if status not in {"closed", "canceled", "cancelled", "expired", "rejected"}:
                        cancel_problem = f"لم أستطع تأكيد إلغاء أمر TP `{oid}`"
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


async def _do_restore_trimmed(query, tid, pf_id, pct):
    """Repurchase a selected percentage of net manually-trimmed quantity from the ledger."""
    if pct not in (25, 50, 70, 100):
        await query.edit_message_text("نسبة استرجاع غير صالحة.", reply_markup=main_menu_keyboard())
        return
    lock_key = (int(tid), int(pf_id))
    if lock_key in _ACTIVE_PORTFOLIO_TRIMS:
        await query.edit_message_text("⏳ يجري بالفعل تعديل حيازة هذه المحفظة؛ انتظر النتيجة.")
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
        tp1 = _resolved(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _resolved(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _resolved(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        sell1 = _resolved(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        sell2 = _resolved(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
        sl_pct = _resolved(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        async def _rearm_tp(coin_obj, amount, reference_price):
            if not pf.is_running or amount <= 0:
                return True
            try:
                tp_res = await loop.run_in_executor(
                    None,
                    lambda s=coin_obj.symbol, a=amount, e=reference_price: get_reb().place_tp_orders(
                        [{"symbol": s, "amount": a, "entry_price": e, "place_exchange_orders": False}],
                        tp1, tp2, tp3, sl_pct, sell1, sell2, control_mode="manual",
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
                    current_sl_price=trade.get("sl_price", coin_obj.current_sl_price),
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
                Portfolio.telegram_id == tid,
                Portfolio.status == "active",
                Portfolio.is_running == True,
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
                free_usdt = float(await loop.run_in_executor(None, lambda: client.get_free_usdt()) or 0.0)
                # Preserve the requested percentage as a ceiling. Reduce only if free USDT cannot fund it.
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
                        state = await loop.run_in_executor(None, lambda o=oid, s=pair: client.fetch_order(o, s))
                        status = str((state or {}).get("status") or "").lower()
                        if status not in {"closed", "canceled", "cancelled", "expired", "rejected"}:
                            cancel_failed = True
                            break
                if cancel_failed:
                    results.append(f"⚠️ `{symbol}`: تعذر تأكيد إلغاء هدف بيع قديم؛ لم يتم الشراء.")
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
                if fill_amount > target_amount:
                    fill_amount = target_amount
                old_remaining = max(0.0, float(coin.remaining_amount or 0.0))
                old_entry = max(0.0, float(coin.entry_price or 0.0))
                new_remaining = old_remaining + fill_amount
                avg_entry = ((old_remaining * old_entry) + (fill_amount * fill_price)) / new_remaining if new_remaining > 0 else fill_price
                total_amount = max(float(coin.amount or 0.0), new_remaining)
                update_coin_position(
                    db, coin.id,
                    amount=total_amount,
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


async def _do_stop(query, tid, pf_id):
    """إيقاف المحفظة: إلغاء الأوامر + بيع كل العملات بسعر السوق."""

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
        notes = []
        cancelled_count = 0
        sold_total = 0.0
        sold_symbols = []
        fail_symbols = []

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
                Portfolio.telegram_id == tid,
                Portfolio.status == "active",
                Portfolio.is_running == True,
            ).first()
            if other:
                shared.add(c.symbol)

        # 2) بيع كل عملة
        def _sell_one(sym, tracked, is_shared):
            free = float(client.get_free_amount(sym) or 0)
            total = float(client.get_total_amount(sym) or 0)
            if is_shared and tracked > 0:
                amount = min(tracked * 0.998, free if free > 0 else total)
            else:
                amount = (free if free > 0 else total) * 0.998
            if amount <= 0:
                return {"ok": True, "dust": True, "usdt": 0.0, "amount": 0.0}
            try:
                px = float(client.get_ticker_price(f"{sym}/{client.quote}") or 0)
                order = client.create_market_order(f"{sym}/{client.quote}", "sell", amount)
                if order is None:
                    return {"ok": True, "dust": True, "usdt": 0.0, "amount": amount}
                return {
                    "ok": True,
                    "dust": False,
                    "usdt": amount * px,
                    "amount": amount,
                    "price": px,
                    "order_id": order.get("id"),
                }
            except Exception as e:
                return {"ok": False, "error": str(e), "usdt": 0.0, "amount": amount}

        for c in coins:
            sym = c.symbol
            tracked = float(c.remaining_amount or c.amount or 0)
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
            await asyncio.sleep(0.25)

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
            f"أوامر ملغاة: `{cancelled_count}`",
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


async def _do_remove_coin(query, tid, pf_id, symbol):
    """حذف عملة: إلغاء أوامرها ثم بيع الرصيد بسعر السوق ثم شيلها من القاعدة."""
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
        await query.edit_message_text(
            f"⏳ جاري حذف `{sym}`:\n"
            "1) إلغاء أوامر البيع\n"
            "2) بيع الرصيد بسعر السوق\n"
            "3) حذفها من المحفظة",
            parse_mode="Markdown",
        )

        client = get_mexc()
        reb = get_reb()
        loop = asyncio.get_event_loop()
        notes = []
        cancelled_count = 0

        # 1) إلغاء أوامر TP المسجّلة + أي أوامر بيع مفتوحة على الزوج
        def _cancel_all():
            res = {"cancelled": [], "errors": []}
            # من القاعدة
            for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                oid = getattr(coin, key, None)
                if not oid:
                    continue
                try:
                    client.cancel_order(str(oid), sym, strict=False)
                    res["cancelled"].append(str(oid))
                except Exception as exc:
                    # أمر منتهي/ملغي مسبقاً — نكمل
                    logger.warning("cancel %s %s: %s", sym, oid, exc)
                    res["errors"].append(str(exc))
            return res

        try:
            cancel_result = await loop.run_in_executor(None, _cancel_all)
            cancelled_count = len(cancel_result.get("cancelled") or [])
        except Exception as exc:
            logger.exception("remove_coin cancel phase")
            notes.append(ar_error(exc, "إلغاء الأوامر"))

        # انتظر تحرير الرصيد بعد الإلغاء
        await asyncio.sleep(1.2)

        # 2) بيع الرصيد الحر (وبعد فشل، جرّب الإجمالي لو اتفك)
        sold_usdt = 0.0
        sold_amt = 0.0
        sell_ok = False

        # هل العملة في محفظة شغّالة تانية؟
        other_running = db.query(PortfolioCoin).join(Portfolio).filter(
            PortfolioCoin.symbol == sym,
            PortfolioCoin.portfolio_id != p.id,
            Portfolio.telegram_id == tid,
            Portfolio.status == "active",
            Portfolio.is_running == True,
        ).first()

        tracked = float(coin.remaining_amount or coin.amount or 0)

        def _sell():
            free = float(client.get_free_amount(sym) or 0)
            total = float(client.get_total_amount(sym) or 0)
            # لو في محفظة تانية شغالة: نبيع بس الكمية المتتبعة قدر الإمكان
            if other_running and tracked > 0:
                amount = min(tracked * 0.998, free if free > 0 else total)
            else:
                amount = free if free > 0 else total
                amount = amount * 0.998
            if amount <= 0:
                return {"sold": 0.0, "amount": 0.0, "dust": True, "error": None}
            try:
                px = float(client.get_ticker_price(f"{sym}/{client.quote}") or 0)
                order = client.create_market_order(
                    f"{sym}/{client.quote}", "sell", amount
                )
                if order is None:
                    # dust تحت الحد الأدنى
                    return {"sold": 0.0, "amount": amount, "dust": True, "error": None}
                usdt = amount * px
                return {"sold": usdt, "amount": amount, "dust": False, "error": None, "order_id": order.get("id")}
            except Exception as e:
                return {"sold": 0.0, "amount": amount, "dust": False, "error": str(e)}

        try:
            sell_result = await loop.run_in_executor(None, _sell)
        except Exception as exc:
            logger.exception("remove_coin sell phase")
            await query.edit_message_text(
                f"❌ فشل بيع `{sym}`\n{ar_error(exc, 'بيع السوق')}\n"
                "لم تُحذف من القاعدة — عالج الخطأ ثم أعد المحاولة.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        if sell_result.get("error"):
            logger.error("sell %s failed: %s", sym, sell_result["error"])
            await query.edit_message_text(
                f"❌ فشل بيع `{sym}` بسعر السوق.\n"
                f"{ar_error(sell_result['error'], 'بيع')}\n"
                "لم تُحذف من القاعدة.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        sold_usdt = float(sell_result.get("sold") or 0)
        sold_amt = float(sell_result.get("amount") or 0)
        if sell_result.get("dust") and sold_amt <= 0 and tracked <= 0:
            notes.append("لا يوجد رصيد للبيع (أو غبار تحت الحد الأدنى)")
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

        # 3) حذف من القاعدة
        if not remove_coin_from_portfolio(db, pf_id, sym):
            await query.edit_message_text(
                f"⚠️ تم التعامل مع `{sym}` على المنصة لكن فشل حذف السجل من القاعدة.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        log_action(db, tid, "remove_coin", f"Removed {sym} sold≈{sold_usdt:.2f}", True, pf_id)
        msg = (
            f"✅ تم حذف *{sym}* من محفظة *{p.name}*\n"
            f"أوامر ملغاة: `{cancelled_count}`\n"
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



async def _do_close(query, tid, pf_id):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        if p.is_running and coins:
            tp_orders = [{
                "symbol": c.symbol,
                "tp_order_id": getattr(c, "tp_order_id", None),
                "tp1_order_id": getattr(c, "tp1_order_id", None),
                "tp2_order_id": getattr(c, "tp2_order_id", None),
                "tp3_order_id": getattr(c, "tp3_order_id", None),
            } for c in p.coins]
            get_reb().cancel_tp_orders(tp_orders)
            get_reb().stop_portfolio(coins, dry_run=False)
        close_portfolio(db, pf_id)
        await query.edit_message_text("✅ تم حذف المحفظة.", reply_markup=main_menu_keyboard())
    finally:
        db.close()


async def create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["create"]["name"] = update.message.text.strip()
    await update.message.reply_text("أرسل *مبلغ الاستثمار* بالـ USDT:", parse_mode="Markdown")
    return CREATE_AMOUNT


async def create_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ""))
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
    db = SessionLocal()
    try:
        p = create_portfolio(db, update.effective_user.id, data["name"], data["amount"], coins)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم إنشاء *{p.name}* (#{p.id})\nالمبلغ: `{p.investment_usdt}`\nالعملات: `{', '.join(coins)}`",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
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
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def increase_amount_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", "").replace("$", ""))
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


async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE):
    """مراقبة مراكز المحافظ وأوامر TP/SL وإعادة الدخول."""
    db = SessionLocal()
    try:
        positions = get_open_positions(db)
        if not positions:
            return
        logger.info(
            "[MONITOR] open_positions=%s symbols=%s",
            len(positions),
            ",".join(sorted({c.symbol for c in positions})[:30]),
        )
        # Run sync CCXT work off the event loop so Telegram stays responsive
        loop = asyncio.get_event_loop()
        reb = get_reb()
        cloud_tp_actions = await loop.run_in_executor(
            None, lambda: reb.sync_cloud_tp(positions)
        )
        # Cloud TP changes the same position that the price/SL pass reads.
        # Exclude those coins from this cycle's stale snapshot to prevent a
        # just-filled TP from immediately triggering a second market sell.
        cloud_handled_ids = {
            action.get("coin_id")
            for action in cloud_tp_actions
            if action.get("coin_id") is not None
            and action.get("action") in {"tp1_hit", "tp2_hit", "tp3_hit", "tp_full_close"}
        }
        price_positions = [
            position for position in positions
            if getattr(position, "id", None) not in cloud_handled_ids
        ]
        price_actions = await loop.run_in_executor(
            None, lambda: reb.check_and_manage_positions(price_positions)
        )
        actions = cloud_tp_actions + price_actions
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

            if act["action"] == "manual_tp_order_cleared":
                key = act.get("clear_order_key")
                if key in ("tp1_order_id", "tp2_order_id", "tp3_order_id"):
                    update_coin_position(db, coin.id, **{key: None})
                continue

            if act["action"] == "tp_full_close":
                # مزامنة: رصيد المنصة صفر — بدون إشعار مزعج (البيع الحقيقي له إشعار SL/إيقاف)
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                fill_price = float(act.get("fill_price") or act.get("price") or 0)
                stage = act.get("stage") or "tp"
                filled_amount = float(act.get("filled_amount") or remaining_before or 0)
                if remaining_before > 0 and fill_price and float(coin.entry_price or 0) > 0:
                    pnl = (fill_price - float(coin.entry_price)) * min(filled_amount, remaining_before)
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "balance_sync",
                        coin.entry_price, fill_price, min(filled_amount, remaining_before), pnl,
                        details=f"مزامنة رصيد صفر ({stage})",
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
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or 0)
                if filled_amount <= 0:
                    filled_amount = remaining_before * 0.40
                filled_amount = min(filled_amount, remaining_before)
                # لو بعد الخصم مفيش باقي → إغلاق كامل
                new_rem = max(0.0, remaining_before - filled_amount)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp1_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                if new_rem <= 0 or new_rem < remaining_before * 0.05:
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
                        current_sl_price=(
                            float(act.get("new_sl") or coin.entry_price or 0)
                            if True
                            else coin.entry_price
                        ) or coin.entry_price,
                        remaining_amount=new_rem,
                        tp1_order_id=None,
                    )
                    manual_mode = True
                    tp1_protection = "الاستوب اليدوي ظل ثابتاً" if manual_mode else f"تم نقل الاستوب إلى سعر الدخول `{coin.entry_price:.6g}`"
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
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or 0)
                if filled_amount <= 0:
                    filled_amount = remaining_before * 0.50
                filled_amount = min(filled_amount, remaining_before)
                new_rem = max(0.0, remaining_before - filled_amount)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp2_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                if new_rem <= 0 or new_rem < remaining_before * 0.05:
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
                        current_sl_price=(
                            coin.current_sl_price
                            if True
                            else act["new_sl"]
                        ),
                        remaining_amount=new_rem,
                        tp2_order_id=None,
                    )
                    manual_mode = True
                    tp2_protection = "الاستوب اليدوي ظل ثابتاً" if manual_mode else f"تم رفع الاستوب لحماية الربح إلى `{act['new_sl']:.6g}`"
                    tp2_mode = "(الجزء المتبقي يعمل بالأهداف اليدوية)" if manual_mode else "(الجزء المتبقي يعمل بالأهداف اليدوية)"
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
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or remaining_before)
                filled_amount = min(filled_amount, remaining_before)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp3_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "tp3",
                    coin.entry_price, fill_price, filled_amount, pnl,
                    details="TP3 filled",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="closed",
                    current_sl_price=0.0,
                    remaining_amount=max(0.0, remaining_before - filled_amount),
                    amount=max(0.0, remaining_before - filled_amount),
                    tp3_order_id=None,
                )
                remove_coin_from_portfolio(db, pf.id, symbol)
                msg = (
                    f"🎯 *تحقق الهدف 3 (الأخير)* — `{symbol}`\n"
                    f"السعر: `{act['price']:.6g}`\n"
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
                # buy again with equal share of portfolio investment
                user = get_or_create_user(db, tid)
                def _pct(pf_val, user_val, default):
                    if pf_val is not None and float(pf_val) > 0:
                        return float(pf_val)
                    if user_val is not None and float(user_val) > 0:
                        return float(user_val)
                    return default
                tp1 = _pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
                tp2 = _pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
                tp3 = _pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
                s1 = _pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
                s2 = _pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 40.0)
                sl_pct = _pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
                n_coins = max(1, len(pf.coins) if pf else 1)
                usdt = (pf.investment_usdt if pf else 20) / n_coins
                if usdt < float(config.PORTFOLIO_MIN_ORDER_USDT):
                    return {
                        "symbol": symbol,
                        "error": (
                            f"حصة إعادة الدخول {usdt:.2f}$ أقل من الحد الأدنى "
                            f"{config.PORTFOLIO_MIN_ORDER_USDT:.2f}$"
                        ),
                    }
                loop = asyncio.get_event_loop()
                buy_res = await loop.run_in_executor(
                    None,
                    lambda: get_reb().reentry_buy_and_place_tp(
                        symbol, usdt, tp1, tp2, tp3, sl_pct, s1, s2
                    ),
                )
                if buy_res.get("error"):
                    try:
                        await context.bot.send_message(
                            tid, f"⚠️ فشل إعادة دخول `{symbol}`: `{buy_res['error']}`", parse_mode="Markdown"
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
                filled_amount = float(act.get("filled_amount") or 0)
                remaining_amount = float(act.get("remaining_amount") or 0)
                exit_price = float(act.get("price") or 0)
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
                    position_status="open",
                    remaining_amount=remaining_amount,
                    current_sl_price=float(act.get("sl") or coin.current_sl_price or 0),
                )
                try:
                    await context.bot.send_message(
                        tid,
                        f"⚠️ تنفيذ جزئي للاستوب `{symbol}`: تم بيع `{filled_amount:.8g}`، "
                        f"المتبقي `{remaining_amount:.8g}` — ستتم إعادة المحاولة دون تصفير المركز.",
                    )
                except Exception:
                    pass

            elif act["action"] == "sl_hit_sold":
                exit_price = float(act.get("price") or 0)
                stopped_amount = float(act.get("amount") or 0)
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
                    f"تم البيع فوراً بسعر السوق ≈ `{act['price']:.6g}`\n"
                    f"النتيجة: `{pnl:+.2f}` USDT\n"
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
    except Exception:
        logger.exception("monitor_positions_job error")
    finally:
        db.close()


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN مطلوب")
    if not config.MEXC_API_KEY or not config.MEXC_API_SECRET:
        raise SystemExit("MEXC_API_KEY و MEXC_API_SECRET مطلوبان")
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL مطلوب")

    if not acquire_singleton_lock():
        raise SystemExit(
            "نسخة أخرى من البوت تعمل بالفعل؛ تم إيقاف هذه النسخة لمنع Telegram 409 Conflict."
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
        try:
            from grid_integration import get_grid_service, grid_monitor_job
            grid_service, grid_error = get_grid_service()
            if grid_service is not None:
                app.job_queue.run_repeating(
                    grid_monitor_job,
                    interval=max(5, grid_service.cfg.poll_seconds),
                    first=10,
                    job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
                )
                logger.info("Grid monitor scheduled (every %ss)", max(5, grid_service.cfg.poll_seconds))
            elif grid_error:
                logger.warning("Grid is unavailable; portfolio bot will continue: %s", grid_error)
        except Exception:
            logger.exception("Grid integration failed; portfolio bot will continue")
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
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    logger.info("Bot starting (MEXC Portfolio Manager)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
