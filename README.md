# LOTFE — MEXC Spot Portfolio Manager

بوت Telegram لإدارة **محافظ MEXC Spot** يدويًا مع مراقبة المراكز، أهداف ربح متعددة، وقف خسارة، إعادة دخول، وقيود حماية على الحساب المشترك.

## ما يعمل في النسخة الحالية

### 1. نظام المحافظ

- إنشاء محافظ متعددة وتحديد المبلغ والعملات.
- تشغيل وإيقاف المحفظة وإعادة البناء.
- شراء العملات الناقصة فقط بعد فحص الرصيد الموجود.
- TP1 وTP2 وTP3 مع نسب بيع قابلة للتعديل.
- وقف خسارة ومراقبة المراكز من البوت.
- إعادة دخول بعد ضرب وقف الخسارة.
- تخفيف الحيازة 25% / 50% / 70% واسترجاع الكمية المباعة.
- سجل للصفقات والأحداث وإعادة البناء.
- حماية من تشغيل نفس الرمز في محافظ متعددة على حساب MEXC واحد.

> وقف الخسارة الأساسي تتم مراقبته من البوت وليس أمر Stop مضمونًا داخل منصة MEXC؛ لذلك يجب أن تبقى الخدمة والاتصال بالـAPI يعملين.

### 2. Liquidity Swings MTF · Spot

- ماسح سوق دوّار لأزواج MEXC Spot/USDT.
- تحليل 4H و1H و15m و5m باستخدام الشموع المغلقة.
- وضع PAPER ووضع LIVE مع تأكيد صريح من Telegram.
- إدارة صفقات الاستراتيجية في جداول منفصلة عن جداول المحافظ.
- شاشة تشخيص لدورات الفحص والأخطاء وعدد العملات المفحوصة.

التفاصيل الفنية في [LIQUIDITY_SWINGS_MTF_README_AR.md](LIQUIDITY_SWINGS_MTF_README_AR.md).

### 3. اصطياد الهبوط Crash Hunter

- سلم شراء يدوي أو تلقائي بناءً على حركة BTC/ETH.
- حفظ الأوامر المفتوحة ومراقبة تنفيذها بعد إعادة التشغيل.
- إلغاء الأوامر المعلقة من واجهة Telegram.

### 4. Flow Engine وWhale Radar

- تقارير تدفق وقراءة نشاط السوق من خلال `whale_reco.py` و`lotfe_flow_engine.py`.
- إعدادات Smart Whale يمكن التحكم بها من البوت، وتشغيل النظام الفرعي الاختياري الموجود في `smart_whale_system/`.
- أي وضع LIVE قد يرسل أوامر فعلية؛ ابدأ دائمًا بوضع PAPER وراجع الإعدادات.

### 5. BTC Risk Governor

يخفض ميزانية الشراء الجديدة تدريجيًا عند تدهور حالة BTC، ولا يبيع المراكز المفتوحة تلقائيًا كإجراء طوارئ. التفاصيل في [BTC_RISK_GOVERNOR_V4_NOTES_AR.md](BTC_RISK_GOVERNOR_V4_NOTES_AR.md).

## نقطة التشغيل

يبدأ Railway التطبيق من:

```bash
python start.py
```

ويقوم `start.py` بتشغيل `bot.py` بعد التحقق من المتغيرات المطلوبة.

## متغيرات البيئة المطلوبة

```env
TELEGRAM_BOT_TOKEN=
MEXC_API_KEY=
MEXC_API_SECRET=
DATABASE_URL=
ADMIN_TELEGRAM_ID=
```

متغيرات اختيارية:

```env
COINLOBSTER_API_KEY=
SIGNAL_CHAT_ID=
BTC_RISK_GOVERNOR_ENABLED=false
BTC_RISK_STALE_AFTER_SEC=60
BTC_RISK_RECOVERY_POINTS_PER_SEC=0.20
BTC_RISK_MIN_BUY_USDT=1.0
```

على Railway يجب أن تكون خدمة PostgreSQL مرتبطة بخدمة البوت في نفس المشروع والبيئة، وأن يكون `DATABASE_URL` مرجعًا لمتغير PostgreSQL الصحيح. لا تستخدم عنوانًا داخليًا قديمًا مثل `postgres.railway.internal` إذا لم يكن قابلًا للحل من الخدمة.

## التشغيل المحلي

```bash
python -m pip install -r requirements.txt
python start.py
```

## الاختبارات

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

الاختبارات لا تنفذ أوامر MEXC حقيقية. يجب اختبار وضع PAPER ومراجعة أوامر المنصة قبل تفعيل LIVE.

## أهم الملفات

| الملف | الوظيفة |
|---|---|
| `bot.py` | واجهة Telegram ونقطة تشغيل التطبيق |
| `database.py` | النماذج والجداول والترحيلات |
| `rebalancer.py` | تنفيذ شراء/بيع المحافظ ووضع TP/SL |
| `mexc_client.py` | الاتصال بمنصة MEXC |
| `portfolio_controls.py` | حسابات الدخول والتخفيف والاسترجاع |
| `liquidity_swings.py` | منطق ماسح Liquidity Swings |
| `liquidity_swings_telegram.py` | واجهة الماسح داخل Telegram |
| `crash_hunter.py` | اصطياد الهبوط ومتابعة الأوامر |
| `market_report.py` | تقرير حالة السوق |
| `smart_whale_system/` | نظام فرعي اختياري لتدفق الحيتان |

> لا يحتوي هذا الريبو على Fast Scalper أو CPR أو SuperTrend أو Sniper؛ هذه الوحدات ليست جزءًا من نقطة التشغيل الحالية.
