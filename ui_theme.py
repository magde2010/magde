"""Modern, consistent Telegram UI helpers for LOTFE.

Presentation-only helpers: callback_data and business logic remain untouched.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

APP = "LOTFE"
UI_VERSION = "3.0-polished"
SEP = "━━━━━━━━━━━━━━━━━━━━"
SEP_LIGHT = "──────────────"

I = {
    "home": "⌂",
    "portfolio": "▦",
    "new": "＋",
    "flow": "🐋",
    "balance": "◍",
    "settings": "⚙",
    "cleanup": "⌫",
    "play": "▶",
    "stop": "■",
    "refresh": "⟳",
    "back": "‹",
    "add": "＋",
    "remove": "−",
    "save": "✓",
    "delete": "×",
    "warning": "⚠",
    "danger": "⛔",
    "paper": "◌",
    "live": "●",
    "bull": "▲",
    "bear": "▼",
    "neutral": "•",
    "clock": "◷",
    "target": "◎",
    "edit": "✎",
    "money": "💰",
    "coin": "🪙",
    "chart": "📈",
    "diamond": "💎",
    "folder": "▣",
}


def button(label: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=callback_data)


def row(*items):
    return [button(label, callback) for label, callback in items]


def markup(rows):
    return InlineKeyboardMarkup(rows)


def page_title(icon: str, title: str, subtitle: str | None = None) -> str:
    """عنوان موحد ببطاقة بصرية واضحة داخل Telegram."""
    text = f"┏━━ {icon}  *{APP} · {title}* ━━┓\n"
    if subtitle:
        text += f"┃ {subtitle}\n"
    text += "┗━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    return text


def footer() -> str:
    return f"\n\n{SEP_LIGHT}\n_{APP} • Spot Control_"


def status_dot(active: bool) -> str:
    return "🟢" if active else "⚪"
