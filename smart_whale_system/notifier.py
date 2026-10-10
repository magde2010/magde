"""
إشعارات تليجرام
"""
import logging
import asyncio
from typing import Optional
from telegram import Bot
from telegram.constants import ParseMode
import config

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None


def get_bot() -> Optional[Bot]:
    global _bot
    if _bot is None and config.TELEGRAM_BOT_TOKEN:
        _bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    return _bot


async def send_message(text: str, chat_id: int = None):
    bot = get_bot()
    if not bot:
        logger.warning("Telegram bot not configured")
        return

    target = chat_id or config.ADMIN_TELEGRAM_ID
    if not target:
        logger.warning("No ADMIN_TELEGRAM_ID set")
        return

    try:
        await bot.send_message(
            chat_id=target,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


def notify(text: str):
    """إرسال متزامن (يُستخدم من الـ loop الرئيسي)"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(send_message(text))
        else:
            loop.run_until_complete(send_message(text))
    except RuntimeError:
        # لو مفيش event loop
        asyncio.run(send_message(text))
    except Exception as e:
        logger.error(f"notify error: {e}")


def format_entry(coin: str, price: float, amount: float, cost: float, multiple: float) -> str:
    return (
        f"🟢 <b>دخول جديد — {config.SYSTEM_NAME}</b>\n\n"
        f"العملة: <b>{coin}</b>\n"
        f"السعر: <code>{price:.6g}</code>\n"
        f"الكمية: <code>{amount:.6g}</code>\n"
        f"التكلفة: <b>{cost:.2f} USDT</b>\n"
        f"قوة إشارة الحيتان: <b>{multiple:.1f}x</b>\n"
        f"وقف الخسارة: -{config.STOP_LOSS_PCT}%\n"
        f"هدف 1: +{config.TAKE_PROFIT_1_PCT}% | هدف 2: +{config.TAKE_PROFIT_2_PCT}%"
    )


def format_exit(coin: str, entry: float, exit_p: float, pnl: float, pnl_pct: float, reason: str) -> str:
    emoji = "🟢" if pnl >= 0 else "🔴"
    return (
        f"{emoji} <b>خروج — {config.SYSTEM_NAME}</b>\n\n"
        f"العملة: <b>{coin}</b>\n"
        f"دخول: <code>{entry:.6g}</code> → خروج: <code>{exit_p:.6g}</code>\n"
        f"الربح/الخسارة: <b>{pnl:+.2f} USDT ({pnl_pct:+.2f}%)</b>\n"
        f"السبب: {reason}"
    )


def format_status(open_positions: list, free_usdt: float, allocated: float) -> str:
    lines = [
        f"📊 <b>حالة {config.SYSTEM_NAME}</b>\n",
        f"رأس المال المخصص: <b>{allocated:.0f} USDT</b>",
        f"الرصيد الحر (USDT): <b>{free_usdt:.2f}</b>",
        f"الصفقات المفتوحة: <b>{len(open_positions)}</b> / {config.MAX_OPEN_POSITIONS}\n",
    ]
    if open_positions:
        lines.append("<b>الصفقات:</b>")
        for p in open_positions:
            lines.append(
                f"• {p.coin} | دخول {p.entry_price:.5g} | كمية {p.amount:.5g}"
            )
    else:
        lines.append("لا توجد صفقات مفتوحة حالياً.")
    return "\n".join(lines)
