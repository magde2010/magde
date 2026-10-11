from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from fast_scalper import get_scalper, get_settings, update_settings

def scalper_keyboard(s):
    mode="🟡 تجريبي" if s.mode=="PAPER" else "🔴 حقيقي"
    auto="🤖 تلقائي" if s.auto_coins else "✋ يدوي"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚡ {mode}",callback_data="sc_mode")],
        [InlineKeyboardButton(f"💰 المركز ${s.order_usdt:g}",callback_data="sc_order"),
         InlineKeyboardButton(f"📊 المراكز {s.max_positions}",callback_data="sc_maxpos")],
        [InlineKeyboardButton(f"🎯 TP {s.tp_pct:g}%",callback_data="sc_tp"),
         InlineKeyboardButton(f"🛑 SL {s.sl_pct:g}%",callback_data="sc_sl")],
        [InlineKeyboardButton(f"⏱ {s.max_hold_sec}s",callback_data="sc_hold"),
         InlineKeyboardButton(f"📉 حد اليوم ${s.daily_loss_usdt:g}",callback_data="sc_dayloss")],
        [InlineKeyboardButton(f"🪙 العملات {auto}",callback_data="sc_coins")],
        [InlineKeyboardButton("📍 المراكز",callback_data="sc_positions"),
         InlineKeyboardButton("📈 فلتر BTC",callback_data="sc_btc")],
        [InlineKeyboardButton("⚙️ شروط الدخول",callback_data="sc_entry")],
        [InlineKeyboardButton("▶️ تشغيل",callback_data="scalper_start"),
         InlineKeyboardButton("⏹ إيقاف",callback_data="scalper_stop")],
        [InlineKeyboardButton("🔄 تحديث",callback_data="scalper_status"),
         InlineKeyboardButton("↩️ الرئيسية",callback_data="menu")]
    ])

def scalper_text(tid):
    s=get_settings(tid); st=get_scalper(tid).status()
    mode="🟡 تجريبي" if s.mode=="PAPER" else "🔴 حقيقي"
    state="🟢 يعمل" if st["enabled"] else "⚪ متوقف"
    return (f"⚡ FAST SCALPER\n━━━━━━━━━━━━━━\nالحالة: {state}\nالوضع: {mode}\n"
            f"حجم المركز: ${s.order_usdt:g}\nالمراكز: {st['open_positions']}/{s.max_positions}\n"
            f"TP: {s.tp_pct:g}% | SL: {s.sl_pct:g}%\nمدة المركز: {s.max_hold_sec}s\n"
            f"حد الخسارة اليومية: ${s.daily_loss_usdt:g}\nالعملات المراقبة: {st['symbols']}\n"
            f"الصفقات: {st['trades']} | 🟢 {st['wins']} | 🔴 {st['losses']}\n"
            f"PnL: ${st['realized']:.4f}\n"
            f"الخسائر المتتالية: {st['consecutive_losses']}\n"
            f"آخر خطأ: {st['last_error'] or 'لا يوجد'}")

def parse_setting(key,text):
    raw=text.strip().replace("$","").replace("%","").replace(",",".")
    if key=="manual_coins":
        return ",".join(dict.fromkeys(x.strip().upper().replace("/","") for x in text.split(",") if x.strip()))
    nums={"order_usdt":float,"max_positions":int,"tp_pct":float,"sl_pct":float,
          "max_hold_sec":int,"daily_loss_usdt":float,"max_consecutive_losses":int,
          "cooldown_sec":float,"max_spread_pct":float,"min_imbalance":float,
          "min_5s_momentum_pct":float,"min_15s_momentum_pct":float,"min_30s_momentum_pct":float,
          "btc_5s_min_pct":float,"btc_30s_min_pct":float,"btc_60s_min_pct":float,
          "watchlist_size":int,"min_24h_volume_usdt":float,"rerank_sec":int,
          "entry_timeout_sec":float}
    if key not in nums: raise ValueError("إعداد غير معروف")
    v=nums[key](raw)
    if v<=0: raise ValueError("القيمة يجب أن تكون أكبر من صفر")
    if key=="min_imbalance" and v>1:raise ValueError("القيمة بين 0 و1")
    return v
