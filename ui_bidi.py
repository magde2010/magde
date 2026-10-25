"""Mohamed Rade — إصلاح اتجاه النص العربي (RTL) تلقائيًا لكل رسالة.

المشكلة: تيليجرام يرتّب السطر حسب أقوى حرف فيه. أي سطر عربي جواه رقم أو كلمة
إنجليزية (12.50 / USDT / DCA) بيتلخبط في العرض ويطلع مقلوب أو متنطط.
الحل: نعزل كل مقطع لاتيني/رقمي داخل علامتي LRI…PDI، فيفضل مرتب زي ما هو
داخل السطر العربي. الطريقة آمنة: لا تلمس ما بين علامات `` ` `` ولا الروابط،
وفعّالة مرة واحدة فقط (idempotent)، وأي خطأ = إرجاع النص الأصلي بدون تعديل.

الإيقاف الطارئ: اضبط المتغير UI_BIDI_FIX=0
"""
from __future__ import annotations

import functools
import os
import re

LRI, PDI, RLM = "\u2066", "\u2069", "\u200f"
ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
LTR_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%/,:+-]*")
PROTECTED = re.compile(r"(`[^`]*`|https?://\S+|\*\*?[^*\n]+\*\*?)")
ENABLED = os.getenv("UI_BIDI_FIX", "1").lower() not in ("0", "false", "no", "off")
_ACTIVE = False


def _fix_segment(seg: str) -> str:
    def repl(m):
        token = m.group(0)
        start = m.start()
        if start and seg[start - 1] == LRI:      # سبق عزله -> لا تكرر
            return token
        return f"{LRI}{token}{PDI}"
    return LTR_RUN.sub(repl, seg)


def fix_line(text: str) -> str:
    """يصلح سطرًا واحدًا فقط إذا كان عربيًا وفيه مقاطع لاتينية/أرقام."""
    if not text or not ARABIC.search(text):
        return text
    if not LTR_RUN.search(text):
        return text
    parts = PROTECTED.split(text)
    for i, seg in enumerate(parts):
        if i % 2 == 1:                            # كود أو رابط أو نص عريض محمي
            continue
        parts[i] = _fix_segment(seg)
    return "".join(parts)


def polish(text: str) -> str:
    """تنظيف نهائي لأي نص صادر: اتجاه + مسافات + أسطر فارغة زائدة."""
    if not isinstance(text, str) or not ENABLED:
        return text
    try:
        lines = [fix_line(ln.rstrip()) for ln in text.split("\n")]
        out = "\n".join(lines)
        out = re.sub(r"\n{4,}", "\n\n\n", out)
        out = re.sub(r"(?m)^[ \t]+$", "", out)
        return out
    except Exception:
        return text


def _apply(value):
    return polish(value) if isinstance(value, str) else value


def _kwargs(kwargs):
    for key in ("text", "caption"):
        if isinstance(kwargs.get(key), str):
            kwargs = dict(kwargs)
            kwargs[key] = polish(kwargs[key])
    return kwargs


def activate() -> bool:
    """يفعّل الإصلاح على كل رسائل البوت (مرة واحدة فقط)."""
    global _ACTIVE
    if _ACTIVE or not ENABLED:
        return False
    try:
        from telegram import Bot, CallbackQuery, Message
    except Exception:
        return False

    def wrap(fn, pos: int):
        @functools.wraps(fn)
        async def inner(self, *args, **kwargs):
            try:
                if len(args) > pos:
                    args = list(args)
                    args[pos] = _apply(args[pos])
                    args = tuple(args)
                kwargs = _kwargs(kwargs)
            except Exception:
                pass
            return await fn(self, *args, **kwargs)
        inner.__wrapped_by__ = "ui_bidi"
        return inner

    try:
        Message.reply_text = wrap(Message.reply_text, 0)
        Message.edit_text = wrap(Message.edit_text, 0)
        CallbackQuery.edit_message_text = wrap(CallbackQuery.edit_message_text, 0)
        Bot.send_message = wrap(Bot.send_message, 1)
        Bot.edit_message_text = wrap(Bot.edit_message_text, 2)
    except Exception:
        return False
    _ACTIVE = True
    return True
