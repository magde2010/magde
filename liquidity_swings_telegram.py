"""Telegram UI for the isolated Liquidity Swings Spot strategy."""
import asyncio
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
import liquidity_swings as strategy

logger = logging.getLogger(__name__)


def _btn(text, callback):
    return InlineKeyboardButton(text, callback_data=callback)


def _home_keyboard(settings, summary):
    run_label = "⏸ إيقاف فتح صفقات جديدة" if summary["enabled"] else "▶️ تشغيل الاستراتيجية"
    mode_label = "🧪 الوضع PAPER" if summary["mode"] == "paper" else "💰 الوضع LIVE"
    return InlineKeyboardMarkup([
        [_btn(run_label, "liq_toggle")],
        [_btn(mode_label + " · تغيير الوضع", "liq_mode")],
        [_btn("🔎 مراقبة السوق الفعلية", "liq_monitor")],
        [_btn("🪙 العملات المراقبة", "liq_coins"), _btn("⚙️ الإعدادات", "liq_settings")],
        [_btn("📈 الصفقات المفتوحة", "liq_positions"), _btn("📊 الأداء والسجل", "liq_performance")],
        [_btn("🔄 تحديث الحالة", "liq_home"), _btn("↩️ القائمة الرئيسية", "menu")],
    ])


def _settings_keyboard(s):
    return InlineKeyboardMarkup([
        [_btn(f"💵 رأس المال: {s.allocated_capital:g} USDT", "liq_edit_capital"),
         _btn(f"🛒 حجم الصفقة: {s.position_size_usdt:g} USDT", "liq_edit_size")],
        [_btn(f"⚠️ المخاطرة: {s.risk_pct:g}%", "liq_edit_risk"),
         _btn(f"🔢 أقصى صفقات: {s.max_open_positions}", "liq_edit_maxpos")],
        [_btn(f"🛑 حد الخسارة اليومية: {s.daily_loss_limit_usdt:g} USDT", "liq_edit_daily")],
        [_btn(f"🔎 Pivot: {s.pivot_length}", "liq_edit_pivot"),
         _btn(f"🎯 أقل عائد/مخاطرة: {s.min_rr:g}:1", "liq_edit_rr")],
        [_btn("🎚️ مستوى الدخول: " + {"conservative":"محافظ", "balanced":"متوازن", "active":"نشط"}.get(s.entry_profile, "متوازن"), "liq_entry_profile")],
        [_btn(f"🧾 هامش وقف الخسارة: {s.stop_buffer_pct:g}%", "liq_edit_buffer"),
         _btn(f"↔️ أقصى فارق سعر: {s.max_spread_pct:g}%", "liq_edit_spread")],
        [_btn(f"⏱️ أقصى انحراف للدخول: {s.max_entry_deviation_pct:g}%", "liq_edit_deviation")],
        [_btn(f"🔎 دفعة الفحص: {s.max_symbols} عملة/دورة", "liq_edit_maxsymbols")],
        [_btn("🔄 اكتشاف العملات: " + ("✅" if s.auto_discover else "❌"), "liq_auto_toggle")],
        [_btn("↩️ رجوع", "liq_home")],
    ])


def _coins_keyboard(s):
    return InlineKeyboardMarkup([
        [_btn("✏️ تعديل قائمة العملات", "liq_edit_symbols"), _btn("🚫 استبعاد عملات", "liq_edit_excluded")],
        [_btn("🔄 اكتشاف العملات تلقائيًا: " + ("✅" if s.auto_discover else "❌"), "liq_auto_toggle")],
        [_btn("⚙️ إعدادات الاكتشاف", "liq_discovery_settings")],
        [_btn("↩️ رجوع", "liq_home")],
    ])


def _duration_label(seconds):
    if seconds is None:
        return "لا يوجد سجل بعد"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"منذ {seconds} ثانية"
    if seconds < 3600:
        return f"منذ {seconds // 60} دقيقة"
    return f"منذ {seconds // 3600} ساعة"


def _dt_label(value):
    if not value:
        return "—"
    # Strategy timestamps are stored as naive UTC for compatibility with LOTFE DB.
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _monitor_state_label(cycle):
    if not cycle:
        return "⚪ لم تسجل دورة فحص فعلية بعد"
    age = cycle.get("age_seconds")
    state = cycle.get("status")
    if state == "paused":
        return "⏸️ فحص الدخول متوقف من الإعدادات"
    if state == "running":
        return "🟡 دورة فحص جارية الآن"
    if state == "ok":
        if age is not None and age > 180:
            return f"🔴 بيانات المراقبة قديمة ({_duration_label(age)})"
        return "🟢 اكتملت دورة الفحص"
    if state == "partial":
        if age is not None and age > 180:
            return f"🔴 بيانات المراقبة قديمة ({_duration_label(age)})"
        return "🟠 اكتمل الفحص جزئيًا أو ظهرت أخطاء"
    if state == "blocked":
        return "🟠 فحص الدخول متوقف بسبب أحد حدود الحماية"
    if state == "error":
        return "🔴 فشلت دورة الفحص بسبب خطأ"
    if age is not None and age > 180 and state != "running":
        return f"🔴 بيانات المراقبة قديمة ({_duration_label(age)})"
    return "⚪ لم تكتمل أي دورة فحص بعد"


def _monitor_text(monitor):
    cycle = monitor.get("cycle")
    settings = monitor["settings"]
    lines = [
        "🔎 مراقبة السوق الفعلية · Liquidity Swings MTF",
        "━━━━━━━━━━━━━━━━━━━━",
        f"محرك الفحص: {_monitor_state_label(cycle)}",
    ]
    if settings.auto_discover:
        lines.extend([
            f"أزواج Spot النشطة المكتشفة من MEXC: {monitor.get('market_symbols_count', 0)}",
            f"الأزواج التي اجتازت فلتر السيولة والسبريد: {monitor.get('eligible_symbols_count', 0)}",
            f"دفعة الفحص الدوّارة: حتى {monitor.get('scan_batch_size', settings.max_symbols)} عملة لكل دورة",
        ])
    if not cycle:
        lines.extend([
            "آخر فحص: لا يوجد سجل لدورة فعلية حتى الآن.",
            "عدد العملات المفحوصة: 0",
            "ده لا يثبت أن السوق مفحوص. شغّل الاستراتيجية وتأكد أن JobQueue يعمل.",
        ])
    else:
        status = cycle.get("status")
        total = cycle.get("symbols_total", 0)
        checked = cycle.get("symbols_checked", 0)
        errors = cycle.get("errors_count", 0)
        lines.extend([
            f"آخر بداية للدورة: {_dt_label(cycle.get('last_started_at'))}",
            f"آخر نهاية للدورة: {_dt_label(cycle.get('last_finished_at'))}",
            f"عمر آخر دورة مكتملة: {_duration_label(cycle.get('age_seconds'))}",
            f"مدة آخر دورة: {cycle.get('duration_ms', 0) / 1000:.1f} ثانية",
            f"العملات المطلوبة: {total}",
            f"طلبات جلب بيانات الشموع المكتملة دون خطأ API: {checked}/{total}",
            f"الإشارات المكتملة: {cycle.get('signals_found', 0)}",
            f"أخطاء جلب البيانات: {errors}",
            f"سبب/نتيجة الدورة: {cycle.get('reason') or '—'}",
        ])
        if cycle.get("last_error"):
            lines.append(f"آخر خطأ: {str(cycle['last_error'])[:350]}")
        if checked > 0 and errors == 0 and status == "ok":
            lines.append("بيانات MEXC: ✅ استجابات طلبات OHLCV وصلت دون أخطاء API للعملات المفحوصة؛ لو الشموع قليلة سيظهر ذلك في تشخيص العملة")
        elif checked > 0 and errors > 0:
            lines.append("بيانات MEXC: 🟠 بعض طلبات الشموع نجحت وبعضها فشل")
        elif errors > 0:
            lines.append("بيانات MEXC: 🔴 لم تنجح طلبات جلب الشموع في آخر دورة")
        elif status in {"paused", "blocked"}:
            lines.append("بيانات MEXC: ⚪ لم يُختبر اتصال الشموع في هذه الدورة لأن فحص الدخول لم يبدأ")
        else:
            lines.append("بيانات MEXC: ⚪ لا توجد طلبات شموع ناجحة مسجلة في آخر دورة")
        if status in {"ok", "partial"} and cycle.get("age_seconds") is not None and cycle["age_seconds"] <= 180:
            lines.append("✅ هناك سجل لفحص فعلي حديث، وبيانات كل إطار تظهر في تفاصيل العملة.")
        elif status == "paused":
            lines.append("ℹ️ فحص إشارات الدخول متوقف؛ شاشة المراقبة لن تدّعي أن العملات تُحلَّل حاليًا.")
    symbols = monitor.get("symbols", [])
    recently_checked = [r for r in symbols if r.get("last_checked_at")]
    recently_checked.sort(key=lambda r: r.get("last_checked_at"), reverse=True)
    if recently_checked:
        lines.append("\nآخر نتائج مسجلة:")
        for row in recently_checked[:6]:
            lines.append(
                f"• {row['symbol']}: {row.get('state') or '—'} · "
                f"{_duration_label(row.get('age_seconds'))} · إغلاق 5m: {row.get('last_price', 0):.8g}"
            )
    return "\n".join(lines)


def _monitor_home_keyboard():
    return InlineKeyboardMarkup([
        [_btn("🔄 تحديث المراقبة", "liq_monitor"), _btn("🪙 تفاصيل العملات", "liq_monitor_coins")],
        [_btn("↩️ رجوع للوحة الاستراتيجية", "liq_home")],
    ])


def _monitor_coins_keyboard(monitor):
    rows = []
    buttons = []
    for item in monitor.get("symbols", []):
        state = item.get("state")
        if state == "error":
            marker = "🔴"
        elif item.get("last_checked_at") and item.get("age_seconds") is not None and item["age_seconds"] <= 180:
            marker = "🟢"
        elif state == "not_checked" or not item.get("last_checked_at"):
            marker = "⚪"
        else:
            marker = "🟠"
        buttons.append(_btn(f"{marker} {item['symbol']}", f"liq_mon_sym_{item['symbol']}"))
        if len(buttons) == 2:
            rows.append(buttons)
            buttons = []
    if buttons:
        rows.append(buttons)
    rows.append([_btn("🔄 تحديث القائمة", "liq_monitor_coins"), _btn("↩️ رجوع للمراقبة", "liq_monitor")])
    return InlineKeyboardMarkup(rows)


def _symbol_monitor_text(item, cycle):
    if not item:
        return "لا يوجد سجل لهذه العملة. لم يتم تسجيل فحص فعلي لها بعد."
    state_labels = {
        "not_checked": "لم تُفحص بعد",
        "insufficient_data": "بيانات الشموع غير كافية",
        "trend_not_bullish": "اتجاه 4H ليس صاعدًا",
        "no_liquidity_low": "لا يوجد قاع سيولة 1H مؤكد",
        "waiting_sweep": "في انتظار sweep/reclaim على 15m",
        "waiting_5m_confirmation": "في انتظار تأكيد 5m",
        "no_valid_target": "لا يوجد هدف يحقق العائد/المخاطرة",
        "invalid_risk": "وقف الخسارة غير صالح",
        "signal_ready": "إشارة مكتملة، يجري التحقق قبل الدخول",
        "signal_rejected": "إشارة مرفوضة بعد فحوص الدخول",
        "duplicate_signal": "الإشارة عولجت من قبل",
        "size_rejected": "حجم الصفقة لا يحقق الحد الأدنى",
        "paper_open": "تم فتح صفقة PAPER",
        "live_open": "تم تأكيد صفقة LIVE مفتوحة",
        "live_order_pending": "أمر LIVE ينتظر التأكيد/المصالحة",
        "position_open": "هناك صفقة مفتوحة لهذه العملة",
        "error": "خطأ في جلب بيانات السوق",
    }
    last = item.get("last_checked_at")
    state = item.get("state") or "not_checked"
    lines = [
        f"🔎 تشخيص العملة: {item['symbol']}/USDT",
        "━━━━━━━━━━━━━━━━━━━━",
        f"حالة الفحص: {state_labels.get(state, state)}",
        f"آخر فحص شموع فعلي: {_dt_label(last)} ({_duration_label(item.get('age_seconds'))})",
        f"آخر تحديث لحالة العملة: {_dt_label(item.get('updated_at'))}",
        f"آخر إغلاق شمعة 5m: {item.get('last_price', 0):.8g}",
        f"آخر شمعة مغلقة 4H: {_dt_label(datetime_from_ms(item.get('candle_4h_ts')))}",
        f"آخر شمعة مغلقة 1H: {_dt_label(datetime_from_ms(item.get('candle_1h_ts')))}",
        f"آخر شمعة مغلقة 15m: {_dt_label(datetime_from_ms(item.get('candle_15m_ts')))}",
        f"آخر شمعة مغلقة 5m: {_dt_label(datetime_from_ms(item.get('candle_5m_ts')))}",
        f"التشخيص: {item.get('detail') or '—'}",
    ]
    if item.get("signal_found"):
        lines.append("✅ تم رصد إشارة مكتملة على الشموع المغلقة.")
    if item.get("error"):
        lines.append(f"آخر خطأ: {str(item['error'])[:350]}")
    if cycle:
        lines.append(f"حالة آخر دورة عامة: {_monitor_state_label(cycle)}")
    lines.append("ملاحظة: السعر المعروض هو إغلاق آخر شمعة 5 دقائق مغلقة، وليس سعرًا لحظيًا.")
    return "\n".join(lines)


def datetime_from_ms(value):
    if value is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _settings_text(s, summary):
    mode = "🧪 PAPER" if s.mode == "paper" else "💰 LIVE"
    status = "🟢 يسمح بفتح صفقات" if s.enabled else "⏸ فتح الصفقات متوقف"
    if s.mode == "live" and not s.live_confirmed:
        status = "🛑 LIVE غير مؤكد، لن تُفتح صفقات حقيقية"
    symbols = ", ".join(s.symbols) or "لا توجد عملات"
    if s.auto_discover:
        symbols += f"\n+ فحص تلقائي دوّار حتى {s.max_symbols} عملة في كل دورة"
        symbols += f"\nأزواج Spot النشطة المكتشفة: {s.market_symbols_count}"
        symbols += f"\nالمؤهلة حسب السيولة والسبريد: {len(s.discovered_symbols)}"
        discovered = [x for x in s.discovered_symbols if x not in s.symbols and x not in s.excluded_symbols]
        if discovered:
            symbols += "\nأمثلة من القائمة المؤهلة: " + ", ".join(discovered[:8])
    if s.excluded_symbols:
        symbols += "\nالمستبعدة من الاكتشاف: " + ", ".join(s.excluded_symbols)
    monitor = strategy.monitoring_status(s.telegram_id)
    cycle = monitor.get("cycle")
    scan_line = f"\nالمراقبة الفعلية: {_monitor_state_label(cycle)}"
    if cycle:
        scan_line += f"\nآخر فحص: {cycle.get('symbols_checked', 0)}/{cycle.get('symbols_total', 0)} عملة · {_duration_label(cycle.get('age_seconds'))}"
    else:
        scan_line += "\nآخر فحص فعلي: لم يسجل بعد"
    return (
        "💧 *Liquidity Swings MTF · Spot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الحالة: {status}\nالوضع: {mode}\n"
        f"العملات: {symbols}\n"
        f"الصفقات المفتوحة: {summary['open_positions']} / {s.max_open_positions}\n"
        f"رأس المال المخصص: {s.allocated_capital:.2f} USDT\n"
        f"ربح/خسارة اليوم (مغلق): {summary['daily_pnl']:+.3f} USDT\n"
        f"الربح/الخسارة الكلي (مغلق): {summary['total_pnl']:+.3f} USDT"
        f"{scan_line}\n\n"
        "*طريقة الإشارة:* اتجاه 4H صاعد + قاع سيولة 1H + sweep/reclaim على 15m + كسر صاعد مؤكد على 5m.\n"
        "يتم استخدام الشموع المغلقة فقط. شراء Spot فقط، ولا توجد صفقات Short."
    )


async def show_home(query, telegram_id: int):
    s = strategy.get_settings(telegram_id)
    sm = strategy.summary(telegram_id)
    await query.edit_message_text(
        _settings_text(s, sm), parse_mode="Markdown", reply_markup=_home_keyboard(s, sm)
    )


async def handle_liquidity_callback(update, context, get_mexc):
    query = update.callback_query
    data = query.data or ""
    tid = int(update.effective_user.id)
    try:
        if data == "liq_home":
            await show_home(query, tid)
            return
        if data == "liq_toggle":
            s = strategy.get_settings(tid)
            if not s.enabled:
                if s.mode == "live" and not s.live_confirmed:
                    await query.edit_message_text(
                        "⚠️ وضع LIVE غير مؤكد. لن يتم إرسال أوامر حقيقية قبل تأكيدك الصريح.",
                        reply_markup=InlineKeyboardMarkup([
                            [_btn("✅ تأكيد التداول الحقيقي على MEXC Spot", "liq_live_confirm")],
                            [_btn("↩️ رجوع", "liq_home")],
                        ]),
                    )
                    return
                strategy.update_settings(tid, enabled=True)
                await query.edit_message_text(
                    "▶️ تم تشغيل الاستراتيجية لفتح صفقات جديدة.\nستظل إدارة الصفقات المفتوحة مستمرة حتى عند إيقاف فتح صفقات جديدة.",
                    reply_markup=InlineKeyboardMarkup([[_btn("فتح لوحة الاستراتيجية", "liq_home")]]),
                )
            else:
                strategy.update_settings(tid, enabled=False)
                await query.edit_message_text(
                    "⏸ تم إيقاف فتح صفقات جديدة. متابعة وقف الخسارة والأهداف للصفقات المفتوحة مستمرة.",
                    reply_markup=InlineKeyboardMarkup([[_btn("فتح لوحة الاستراتيجية", "liq_home")]]),
                )
            return
        if data == "liq_mode":
            s = strategy.get_settings(tid)
            await query.edit_message_text(
                f"الوضع الحالي: {'PAPER تجريبي' if s.mode == 'paper' else 'LIVE حقيقي'}\nاختار الوضع:",
                reply_markup=InlineKeyboardMarkup([
                    [_btn("🧪 تفعيل PAPER", "liq_mode_paper")],
                    [_btn("💰 اختيار LIVE الحقيقي", "liq_mode_live")],
                    [_btn("↩️ رجوع", "liq_home")],
                ]),
            )
            return
        if data == "liq_mode_paper":
            strategy.update_settings(tid, mode="paper", live_confirmed=False)
            await show_home(query, tid)
            return
        if data == "liq_mode_live":
            strategy.update_settings(tid, mode="live", live_confirmed=False, enabled=False)
            await query.edit_message_text(
                "⚠️ *تأكيد مهم*\nاختيار LIVE يسمح بإرسال أوامر شراء وبيع حقيقية من رصيد MEXC Spot. قد تحدث خسائر. لن يبدأ التنفيذ قبل ضغط زر التأكيد بالأسفل.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [_btn("✅ أنا فاهم، أكد التداول الحقيقي", "liq_live_confirm")],
                    [_btn("رجوع إلى PAPER", "liq_mode_paper")],
                ]),
            )
            return
        if data == "liq_live_confirm":
            strategy.update_settings(tid, mode="live", live_confirmed=True, enabled=True)
            await query.edit_message_text(
                "✅ تم تأكيد LIVE. الاستراتيجية مفعّلة لفتح صفقات جديدة على MEXC Spot. راجع حجم الصفقة وحدود المخاطرة قبل الاعتماد عليها.",
                reply_markup=InlineKeyboardMarkup([[_btn("لوحة الاستراتيجية", "liq_home")]]),
            )
            return
        if data in {"liq_coins", "liq_edit_symbols", "liq_edit_excluded", "liq_auto_toggle", "liq_discovery_settings"}:
            s = strategy.get_settings(tid)
            if data == "liq_edit_symbols":
                context.user_data["liq_waiting"] = "symbols"
                await query.edit_message_text(
                    "✏️ أرسل رموز العملات مفصولة بفاصلة، من غير /USDT. مثال:\n`BTC,ETH,SOL,XRP`\nسيتم التحقق من أن كل زوج متاح في سوق MEXC Spot USDT.",
                    parse_mode="Markdown",
                )
                return
            if data == "liq_edit_excluded":
                context.user_data["liq_waiting"] = "excluded_symbols"
                await query.edit_message_text(
                    "🚫 أرسل رموز العملات التي تريد منع الاكتشاف التلقائي لها، مفصولة بفاصلة.\nلإلغاء الاستبعاد اكتب `none`. مثال: `PEPE,DOGE`",
                    parse_mode="Markdown",
                )
                return
            if data == "liq_auto_toggle":
                strategy.update_settings(tid, auto_discover=not s.auto_discover)
                await query.edit_message_text("✅ تم تحديث خيار اكتشاف العملات.", reply_markup=InlineKeyboardMarkup([[_btn("العملات", "liq_coins"), _btn("الإعدادات", "liq_settings")]]))
                return
            if data == "liq_discovery_settings":
                context.user_data["liq_waiting"] = "min_volume"
                await query.edit_message_text(
                    f"أرسل الحد الأدنى لحجم التداول اليومي لكل عملة بالدولار (الحالي {s.min_daily_quote_volume:g}).\nمثال: `1000000`",
                    parse_mode="Markdown",
                )
                return
            await query.edit_message_text(
                f"🪙 *العملات المراقبة*\nيدويًا: {', '.join(s.symbols) or 'لا توجد'}\n"
                f"الاكتشاف التلقائي: {'مفعل' if s.auto_discover else 'متوقف'}\n"
                f"حجم دفعة الفحص لكل دورة: {s.max_symbols} عملة\n"
                f"أزواج Spot النشطة المكتشفة: {s.market_symbols_count}\n"
                f"الأزواج المؤهلة بعد فلتر السيولة والسبريد: {len(s.discovered_symbols)}\n"
                f"أمثلة من القائمة المؤهلة: {', '.join(s.discovered_symbols[:12]) or 'لم يتم الاكتشاف بعد'}\n"
                f"المستبعدة: {', '.join(s.excluded_symbols) or 'لا يوجد'}\n\n"
                "الاكتشاف التلقائي يجلب أزواج Spot/USDT المؤهلة حسب السيولة والسبريد، ثم يدوّر الفحص بينها على دفعات عبر الدورات لتجنب ضغط API.",
                parse_mode="Markdown", reply_markup=_coins_keyboard(s),
            )
            return
        if data == "liq_settings":
            s = strategy.get_settings(tid)
            await query.edit_message_text("⚙️ *إعدادات Liquidity Swings*\nاختار قيمة لتعديلها:", parse_mode="Markdown", reply_markup=_settings_keyboard(s))
            return
        if data == "liq_entry_profile":
            await query.edit_message_text(
                "🎚️ *مستوى شروط الدخول*\nاختار المستوى. تغيير المستوى لا يضمن الربح؛ فقط يغيّر صرامة شروط الإشارة.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [_btn("🛡 محافظ", "liq_profile_conservative")],
                    [_btn("⚖️ متوازن (موصى به للتجربة)", "liq_profile_balanced")],
                    [_btn("🔥 نشط", "liq_profile_active")],
                    [_btn("↩️ رجوع", "liq_settings")],
                ]),
            )
            return
        if data in {"liq_profile_conservative", "liq_profile_balanced", "liq_profile_active"}:
            profile = data.replace("liq_profile_", "")
            strategy.update_settings(tid, entry_profile=profile)
            labels = {"conservative": "محافظ", "balanced": "متوازن", "active": "نشط"}
            await query.edit_message_text(
                f"✅ تم ضبط مستوى الدخول على: {labels[profile]}\nالوضع لم يتغير، وإعدادات المخاطرة ووقف الخسارة كما هي.",
                reply_markup=InlineKeyboardMarkup([[_btn("⚙️ الإعدادات", "liq_settings"), _btn("العودة للاستراتيجية", "liq_home")]]),
            )
            return
        if data.startswith("liq_edit_"):
            field = data.replace("liq_edit_", "")
            prompts = {
                "capital": ("allocated_capital", "رأس المال المخصص للاستراتيجية بـ USDT", "مثال: `100`", (10, 1_000_000)),
                "size": ("position_size_usdt", "قيمة الصفقة القصوى بـ USDT", "مثال: `5`", (2, 100_000)),
                "risk": ("risk_pct", "نسبة المخاطرة المستهدفة من رأس المال (%)", "مثال: `1`", (0.1, 10)),
                "maxpos": ("max_open_positions", "الحد الأقصى للصفقات المفتوحة", "مثال: `3`", (1, 30)),
                "daily": ("daily_loss_limit_usdt", "حد الخسارة اليومية بـ USDT. استخدم 0 لتعطيله", "مثال: `5`", (0, 100_000)),
                "pivot": ("pivot_length", "طول Pivot لتأكيد القمم والقيعان", "مثال: `14`", (2, 30)),
                "rr": ("min_rr", "أقل عائد مقابل المخاطرة", "مثال: `2` يعني 2:1", (1, 10)),
                "buffer": ("stop_buffer_pct", "هامش أسفل ذيل sweep لوقف الخسارة (%)", "مثال: `0.2`", (0, 5)),
                "maxsymbols": ("max_symbols", "عدد العملات التي تُفحص في كل دورة (تدوير تلقائي على السوق كله)", "مثال: `10`", (1, 40)),
                "spread": ("max_spread_pct", "أقصى فرق مسموح بين سعر الشراء والبيع (%) عند اكتشاف العملات تلقائيًا", "مثال: `0.5`", (0.01, 5)),
                "deviation": ("max_entry_deviation_pct", "أقصى انحراف مسموح بين إغلاق شمعة التأكيد والسعر الحالي قبل الدخول (%)", "مثال: `0.5`", (0.05, 5)),
            }
            if field not in prompts:
                await query.edit_message_text("حقل غير معروف.", reply_markup=InlineKeyboardMarkup([[_btn("رجوع", "liq_settings")]]))
                return
            key, label, example, bounds = prompts[field]
            context.user_data["liq_waiting"] = key
            context.user_data["liq_bounds"] = bounds
            await query.edit_message_text(f"✏️ {label}\n{example}", parse_mode="Markdown")
            return
        if data == "liq_monitor":
            monitor = strategy.monitoring_status(tid)
            await query.edit_message_text(
                _monitor_text(monitor), reply_markup=_monitor_home_keyboard(),
            )
            return
        if data == "liq_monitor_coins":
            monitor = strategy.monitoring_status(tid)
            lines = [
                "🪙 تفاصيل العملات المراقبة",
                "الأخضر: فحص حديث خلال 3 دقائق. البرتقالي: بيانات قديمة. الأحمر: خطأ. الرمادي: لم تُفحص بعد.",
                "اختار عملة لمعرفة آخر الشموع وسبب قبول/رفض الإشارة.",
            ]
            await query.edit_message_text(
                "\n\n".join(lines), reply_markup=_monitor_coins_keyboard(monitor),
            )
            return
        if data.startswith("liq_mon_sym_"):
            base = data[len("liq_mon_sym_"):].upper()
            monitor = strategy.monitoring_status(tid)
            item = next((row for row in monitor.get("symbols", []) if row["symbol"] == base), None)
            await query.edit_message_text(
                _symbol_monitor_text(item, monitor.get("cycle")),
                reply_markup=InlineKeyboardMarkup([
                    [_btn("🔄 تحديث التفاصيل", f"liq_mon_sym_{base}"), _btn("🪙 قائمة العملات", "liq_monitor_coins")],
                    [_btn("↩️ رجوع للمراقبة", "liq_monitor")],
                ]),
            )
            return
        if data == "liq_positions":
            s = strategy.get_settings(tid)
            positions = strategy.list_positions(tid, s.mode, 20)
            if not positions:
                text = f"📈 لا توجد صفقات مسجلة في وضع {'PAPER' if s.mode == 'paper' else 'LIVE'}."
            else:
                lines = [f"📈 *الصفقات {s.mode.upper()}*", "━━━━━━━━━━━━━━━━━━━━"]
                status_labels = {
                    "open": "مفتوحة", "pending_entry": "تأكيد شراء", "pending_exit": "تأكيد بيع",
                    "entry_submitting": "دخول غير مؤكد", "exit_submitting": "خروج غير مؤكد",
                    "entry_failed": "فشل الدخول", "closed": "مغلقة",
                }
                for p in positions:
                    if p.status == "open":
                        lines.append(f"🟢 `{p.symbol}/USDT` | دخول `{p.entry_price:.8g}` | SL `{p.stop_price:.8g}` | TP `{p.target_price:.8g}` | كمية `{p.amount:.7g}`")
                    else:
                        label = status_labels.get(p.status, "حالة أخرى")
                        lines.append(f"{label} `{p.symbol}/USDT` | PnL {p.realized_pnl:+.3f} USDT | {p.exit_reason or p.last_error or '—'}")
                text = "\n".join(lines)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[_btn("🔄 تحديث", "liq_positions"), _btn("رجوع", "liq_home")]]))
            return
        if data == "liq_performance":
            s = strategy.get_settings(tid)
            positions = strategy.list_positions(tid, s.mode, 50)
            closed = [p for p in positions if p.status == "closed"]
            wins = sum(1 for p in closed if p.realized_pnl > 0)
            losses = sum(1 for p in closed if p.realized_pnl < 0)
            total = sum(p.realized_pnl for p in closed)
            lines = [f"📊 *الأداء {s.mode.upper()}*", "━━━━━━━━━━━━━━━━━━━━", f"الصفقات المغلقة: {len(closed)}", f"الرابحة: {wins} | الخاسرة: {losses}", f"PnL ضمن آخر 50 سجل: {total:+.3f} USDT", ""]
            for p in closed[:10]:
                lines.append(f"`{p.symbol}`: {p.realized_pnl:+.3f} USDT ({p.exit_reason or '—'})")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[_btn("🔄 تحديث", "liq_performance"), _btn("رجوع", "liq_home")]]))
            return
    except Exception as exc:
        logger.exception("Liquidity Telegram handler failed")
        try:
            await query.edit_message_text(
                f"⚠️ خطأ داخل الاستراتيجية: `{str(exc)[:250]}`",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[_btn("رجوع", "liq_home")]]),
            )
        except Exception:
            pass


async def handle_liquidity_text(update, context, get_mexc) -> bool:
    """Consume text input awaiting a Liquidity Strategy setting; return True if consumed."""
    waiting = context.user_data.get("liq_waiting")
    if not waiting or not update.message or not update.message.text:
        return False
    tid = int(update.effective_user.id)
    raw = update.message.text.strip()
    completed = False
    try:
        if waiting == "excluded_symbols":
            if raw.lower() in {"none", "لا", "clear", "-"}:
                bases = []
            else:
                bases = list(dict.fromkeys(
                    token.strip().upper().replace("/USDT", "")
                    for token in raw.replace("\n", ",").replace(";", ",").split(",") if token.strip()
                ))
            if len(bases) > 40:
                raise ValueError("يمكن استبعاد 40 رمزًا كحد أقصى")
            loop = asyncio.get_running_loop()
            known = await loop.run_in_executor(None, lambda: set(strategy._market_symbols(get_mexc())))
            invalid = [base for base in bases if base not in known]
            if invalid:
                raise ValueError("أزواج Spot USDT غير متاحة: " + ", ".join(invalid[:12]))
            strategy.update_settings(tid, excluded_symbols_json=__import__("json").dumps(bases))
            await update.message.reply_text("✅ تم حفظ قائمة الاستبعاد: " + (", ".join(bases) or "لا يوجد استبعاد"), reply_markup=InlineKeyboardMarkup([[_btn("العودة للعملات", "liq_coins"), _btn("لوحة الاستراتيجية", "liq_home")]]))
            completed = True
        elif waiting == "symbols":
            bases = list(dict.fromkeys(
                token.strip().upper().replace("/USDT", "")
                for token in raw.replace("\n", ",").replace(";", ",").split(",") if token.strip()
            ))
            if not bases or len(bases) > 40:
                raise ValueError("أدخل من 1 إلى 40 رمزًا مفصولًا بفاصلة")
            loop = asyncio.get_running_loop()
            known = await loop.run_in_executor(None, lambda: set(strategy._market_symbols(get_mexc())))
            invalid = [base for base in bases if base not in known]
            if invalid:
                raise ValueError("أزواج Spot USDT غير متاحة: " + ", ".join(invalid[:12]))
            strategy.update_settings(tid, symbols_json=__import__("json").dumps(bases))
            await update.message.reply_text("✅ تم حفظ العملات: " + ", ".join(bases), reply_markup=InlineKeyboardMarkup([[_btn("العودة للعملات", "liq_coins"), _btn("لوحة الاستراتيجية", "liq_home")]]))
            completed = True
        elif waiting == "min_volume":
            val = float(raw.replace(",", ".").replace("$", ""))
            if not 10_000 <= val <= 1_000_000_000:
                raise ValueError("القيمة من 10000 إلى 1000000000 USDT")
            strategy.update_settings(tid, min_daily_quote_volume=val)
            await update.message.reply_text(f"✅ تم حفظ حد حجم التداول: {val:g} USDT", reply_markup=InlineKeyboardMarkup([[_btn("العودة للعملات", "liq_coins"), _btn("لوحة الاستراتيجية", "liq_home")]]))
            completed = True
        else:
            val = float(raw.replace(",", ".").replace("%", "").replace("$", ""))
            bounds = context.user_data.get("liq_bounds")
            if not bounds:
                s = strategy.get_settings(tid)
                ranges = {
                    "allocated_capital": (10, 1_000_000), "position_size_usdt": (2, 100_000),
                    "risk_pct": (0.1, 10), "max_open_positions": (1, 30),
                    "daily_loss_limit_usdt": (0, 100_000), "pivot_length": (2, 30),
                    "min_rr": (1, 10), "stop_buffer_pct": (0, 5), "max_symbols": (1, 40),
                    "max_spread_pct": (0.01, 5), "max_entry_deviation_pct": (0.05, 5),
                }
                bounds = ranges[waiting]
            low, high = bounds
            if not low <= val <= high:
                raise ValueError(f"القيمة يجب أن تكون بين {low:g} و {high:g}")
            integer_fields = {"max_open_positions", "pivot_length", "max_symbols"}
            result = int(val) if waiting in integer_fields else val
            if waiting == "position_size_usdt" and val < 2:
                raise ValueError("الحد الأدنى للصفقة 2 USDT")
            strategy.update_settings(tid, **{waiting: result})
            await update.message.reply_text("✅ تم حفظ الإعداد بنجاح.", reply_markup=InlineKeyboardMarkup([[_btn("الإعدادات", "liq_settings"), _btn("لوحة الاستراتيجية", "liq_home")]]))
            completed = True
    except Exception as exc:
        await update.message.reply_text(f"❌ لم يتم حفظ القيمة: {str(exc)[:250]}")
        return True
    finally:
        if completed:
            context.user_data.pop("liq_waiting", None)
            context.user_data.pop("liq_bounds", None)
    return True


async def liquidity_scan_job(context):
    configured_admin = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
    try:
        loop = asyncio.get_running_loop()
        users = [configured_admin] if configured_admin else await loop.run_in_executor(None, strategy.active_telegram_ids)
        for tid in users:
            events = await loop.run_in_executor(
                None, lambda user_id=tid: strategy.scan_once(context.application.bot_data["get_mexc"](), user_id)
            )
            for event in events:
                try:
                    await context.bot.send_message(tid, event["text"], parse_mode="Markdown")
                except Exception:
                    # Fallback without Markdown if the market symbol has awkward characters.
                    await context.bot.send_message(tid, event["text"])
    except Exception:
        logger.exception("Liquidity scan job failed")
