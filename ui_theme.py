"""Mohamed Rade — نظام واجهة موحّد واحترافي لتيليجرام (نسخة 4.0-pro).

ملف بديل مباشر (drop-in) لـ ui_theme.py القديم:
• كل الدوال القديمة محفوظة بنفس الأسماء والتوقيعات -> bot.py يعمل بدون أي تعديل.
• إضافات: عناوين عريضة واضحة، تنسيق انسيابي بدون إطار يمين (لأن النص العربي
  يتفاوت في العرض داخل تيليجرام فيكسر أي إطار مغلق)، صفوف بيانات، أشرطة تقدّم،
  شرائح حالة، وقاموس تبسيط للمصطلحات الصعبة.

كل الرسائل تُرسل بـ parse_mode="Markdown" (كما في bot.py) لذلك:
    العريض = *نص*   |   المائل = _نص_   |   الأحادي = `نص`
ويجب أن تكون الرموز * _ ` [ متوازنة داخل الرسالة الواحدة، أو مُهرَّبة عبر safe().
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

APP = "Mohamed Rade"
UI_VERSION = "4.0-pro"

# ── فواصل ────────────────────────────────────────────────────────────────────
SEP = "━" * 20          # فاصل رئيسي
SEP_LIGHT = "─" * 20    # فاصل فرعي / تذييل
ARROW = "▸"
BULLET = "•"

# ── أيقونات ──────────────────────────────────────────────────────────────────
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
    # إضافات النسخة الجديدة
    "help": "❓",
    "info": "ℹ",
    "lock": "🔒",
    "check": "✓",
    "close": "×",
    "dca": "↧",
    "rebalance": "⚖",
    "trim": "✂",
    "restore": "↩",
    "shield": "🛡",
    "cloud": "☁",
    "robot": "🤖",
    "spark": "✦",
    "star": "★",
    "empty": "◌",
    "time": "🕒",
    "bolt": "⚡",
}

HELP_HINT = f"{I['help']} مش فاهم كلمة؟ اكتب /help لعرض شرح المصطلحات."


# ── أدوات Markdown ───────────────────────────────────────────────────────────
def b(text) -> str:
    """عريض."""
    return f"*{text}*"


def it(text) -> str:
    """مائل (استخدمه بحساب: لا تكرّره داخل نفس السطر)."""
    return f"_{text}_"


def mono(text) -> str:
    """خط أحادي المسافة — للأرقام والرموز."""
    return f"`{text}`"


_ESCAPE_TABLE = str.maketrans({c: "\\" + c for c in "_*`[]"})


def safe(text) -> str:
    """تهريب رموز Markdown في أي نص ديناميكي (اسم محفظة، سبب خطأ...).

    يمنع رسالة تيليجرام الشهيرة: Can't parse entities.
    """
    if text is None:
        return ""
    return str(text).translate(_ESCAPE_TABLE)


# ── عناوين وصفحات ────────────────────────────────────────────────────────────
def title(icon: str, text: str) -> str:
    """سطر عنوان عريض."""
    return f"{icon}  {b(safe(text))}"


def page_title(icon: str, title_text: str, subtitle: str | None = None) -> str:
    """عنوان موحّد لكل صفحة: أيقونة + عنوان عريض + وصف رقيق + فاصل."""
    out = f"{icon}  {b(safe(title_text))}"
    if subtitle:
        out += f"\n{safe(subtitle)}"
    return f"{out}\n{SEP}"


def section(text: str, icon: str = ARROW) -> str:
    """عنوان قسم داخلي عريض."""
    return b(f"{icon} {safe(text)}")


def line(light: bool = True) -> str:
    return SEP_LIGHT if light else SEP


def footer() -> str:
    return f"\n\n{SEP_LIGHT}\n_{APP} • Spot Control • v{UI_VERSION}_"


def page(icon: str, title_text: str, subtitle: str | None = None, body: str = "",
         with_footer: bool = True) -> str:
    """يبني رسالة كاملة في نداء واحد → تنسيق متسق في كل الشاشات."""
    parts = [page_title(icon, title_text, subtitle)]
    if body:
        parts.append(body)
    text = "\n".join(parts)
    if with_footer:
        text += footer()
    return text


# ── صفوف البيانات ────────────────────────────────────────────────────────────
def kv(label: str, value, icon: str = BULLET, bold_value: bool = True) -> str:
    """سطر بيانات: التسمية عادية والقيمة عريضة → تباين واضح وسريع القراءة."""
    val = b(value) if bold_value else str(value)
    return f"{icon} {safe(label)}: {val}"


def rows_block(pairs, icon: str = BULLET) -> str:
    return "\n".join(kv(k, v, icon) for k, v in pairs)


def bar(pct: float, width: int = 10, filled: str = "▰", empty: str = "▱") -> str:
    """شريط تقدّم بصري: ▰▰▰▰▱▱▱▱▱▱ 40%"""
    try:
        pct = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        pct = 0.0
    n = int(round(pct / 100 * width))
    return f"{filled * n}{empty * (width - n)} {pct:.0f}%"


def money(value, dp: int = 2) -> str:
    """أرقام بفواصل آلاف → أسهل في القراءة."""
    try:
        return f"{float(value):,.{dp}f}"
    except (TypeError, ValueError):
        return str(value)


def badge(active: bool, on: str = "🟢 يعمل", off: str = "⚪ متوقف") -> str:
    return on if active else off


def status_dot(active: bool) -> str:
    return "🟢" if active else "⚪"


def empty_state(icon: str, headline: str, hint: str | None = None) -> str:
    """رسائل القوائم الفارغة: واضحة وتقول للمستخدم الخطوة التالية."""
    out = f"{icon}  {b(safe(headline))}"
    if hint:
        out += f"\n{safe(hint)}"
    return out


def tip(text: str) -> str:
    return f"{I['spark']} {it(safe(text))}"


# ── قاموس تبسيط المصطلحات ────────────────────────────────────────────────────
# (الاسم كما يظهر، الشرح بالعامية المبسّطة)
GLOSSARY: dict[str, tuple[str, str]] = {
    "dca": ("DCA / الشراء على دفعات",
            "مش هتشتري كل المبلغ مرة واحدة، هتشتري على دفعات. لو السعر نزل تشتري كمية "
            "إضافية فيقل متوسط سعر الشراء وتبقى محتاج صعود أقل عشان تكسب."),
    "rebalance": ("إعادة التوازن",
                  "إعادة توزيع المبلغ على العملات بالنسب اللي انت محددها، عشان عملة "
                  "متأخرة تاخد نصيب أكبر بشكل منظم."),
    "trim": ("تخفيف الحيازة",
             "تبيع جزء من كمية العملة (25% أو 50% أو 70%) وباقي الكمية تفضل معاك — "
             "كسبان جزء من الربح من غير ما تخرج بالكامل."),
    "restore": ("استرجاع الكميات المخفَّفة",
                "ترجّع الكمية اللي باعها التخفيف بمبلغ من المخصص، فترجع العملة لحجمها الأول."),
    "stoploss": ("وقف الخسارة",
                 "سعر تحدده انت؛ لو السعر نزل له البوت يبيع تلقائيًا عشان الخسارة ما تكبر."),
    "takeprofit": ("هدف الربح",
                   "سعر تحدده انت؛ لو السعر وصل له البوت يبيع ويحقق الربح."),
    "limit": ("الأمر المحدد (Limit)",
              "طلب شراء بسعر انت تحدده؛ اتنفّذ فقط لو السعر وصل له أو نزل تحته."),
    "reserve": ("احتياطي DCA",
                "جزء من الفلوس بتحتفظ به جانبًا ومش بيدخل الشراء الأول، مخصص للدفعات "
                "الإضافية لو السعر نزل."),
    "cloud": ("المراقبة السحابية",
              "البوت شغّال على السيرفر بيفحص الأسعار كل ثواني، فلو السعر نزل يبيع "
              "تلقائيًا بدون ما تكون فاتح التطبيق."),
    "balance_free": ("الرصيد المتاح / الحر",
                     "الفلوس اللي مش محجوزة في أي طلب؛ تقدر تشغلها أو تسحبها فورًا."),
    "portfolio": ("المحفظة",
                  "خطة استثمار واحدة: عملات محددة + مبلغ مخصص + أهداف ووقف خسارة."),
    "min_order": ("الحد الأدنى للأمر",
                  "أقل مبلغ MEXC بيقبل الطلب بيه (عندك في المشروع 2 USDT تقريبًا حسب إعداد "
                  "العملة). الطلبات أقل من كده بتترفض."),
    "fees": ("الرسوم",
             "عمولة MEXC على كل شراء أو بيع — بتتخصم من المبلغ، فحسب الأرباح بعدها."),
    "slippage": ("الانزلاق السعري",
                 "الفرق بين السعر اللي شفته والسعر اللي الطلب اتنفّذ بيه فعليًا في السوق."),
    "paper": ("الوضع التجريبي (Paper)",
              "تشغيل الخطة للتجربة والمراقبة بدون تنفيذ أوامر بفلوس حقيقية."),
    "usdt": ("USDT",
             "عملة رقمية ثابتة قيمتها ≈ 1 دولار، بنقيس بيها قيمة المحفظة والأرباح."),
    "alloc": ("المبلغ المخصص",
              "المبلغ اللي محدد للمحفظة ككل؛ منه بيتوزّع الشراء الأول والاحتياطي."),
}

# مرادفات أبسط للاستخدام داخل النصوص الطويلة (اختياري، للنسخ اليدوي)
SIMPLER_WORDS: dict[str, str] = {
    "إعادة توازن عام": "إعادة توزيع عام",
    "المراكز": "العملات المملوكة",
    "تعبئة أمر": "تنفيذ الطلب",
    "المخصص": "المبلغ المحدد",
    "تخفيف الحيازة": "بيع جزء من العملة",
    "أمر DCA": "طلب الشراء الإضافي",
    "مزامنة": "متابعة تلقائية",
    "تجاوز": "تخطّي",
}


def explain(*keys: str, prefix: str = "💡") -> str:
    """شرح مختصر لسطر واحد: explain("dca", "stoploss")."""
    lines = []
    for key in keys:
        entry = GLOSSARY.get(key)
        if not entry:
            continue
        name, desc = entry
        lines.append(f"{prefix} {b(name)} — {safe(desc)}")
    return "\n".join(lines)


def glossary_page() -> str:
    """صفحة /help كاملة: مصطلحات مبسّطة + طريقة الاستخدام."""
    body = [
        page_title(I["help"], f"{APP} · شرح المصطلحات", "كل كلمة صعبة في البوت ومعناها بالبساطة."),
        section("الشراء والدخول"),
        "",
    ]
    for key in ("dca", "reserve", "limit", "alloc", "rebalance", "trim", "restore"):
        name, desc = GLOSSARY[key]
        body.append(f"{BULLET} {b(name)}\n   {safe(desc)}")
    body.append("")
    body.append(section("الحماية والخروج"))
    for key in ("stoploss", "takeprofit", "cloud", "fees", "slippage"):
        name, desc = GLOSSARY[key]
        body.append(f"{BULLET} {b(name)}\n   {safe(desc)}")
    body.append("")
    body.append(section("معلومات عامة"))
    for key in ("portfolio", "balance_free", "min_order", "usdt", "paper"):
        name, desc = GLOSSARY[key]
        body.append(f"{BULLET} {b(name)}\n   {safe(desc)}")
    body.append("")
    body.append(tip("نصيحة: ابدأ بمبلغ صغير، وتأكد من وقف الخسارة قبل أي تشغيل حقيقي."))
    text = "\n".join(body)
    return text + footer()


# ── أزرار (نصوص الأزرار في تيليجرام لا تقبل عريضًا) ─────────────────────────
def button(label: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=callback_data)


def row(*items):
    return [button(label, callback) for label, callback in items]


def markup(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)
