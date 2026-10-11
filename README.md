# LOTFE — المحافظ وLiquidity Swings MTF

بوت Telegram لتشغيل نظامين فقط على MEXC Spot:

1. **نظام المحافظ** لإدارة المراكز يدويًا.
2. **Liquidity Swings MTF** كاستراتيجية مستقلة لمسح السوق وإدارة صفقاتها.

## نظام المحافظ

- إنشاء محافظ متعددة وتحديد المبلغ والعملات.
- تشغيل وإيقاف المحفظة وإعادة البناء.
- شراء العملات الناقصة فقط بعد فحص الرصيد.
- أهداف TP1 وTP2 وTP3 مع نسب بيع قابلة للتعديل.
- وقف خسارة ومراقبة المراكز من البوت.
- إعادة دخول بعد ضرب وقف الخسارة.
- تخفيف الحيازة 25% / 50% / 70% واسترجاع الكمية المباعة.
- سجل للصفقات والأحداث وإعادة البناء.
- منع تشغيل نفس الرمز في محافظ متعددة على حساب MEXC واحد.
- BTC Risk Governor لتخفيض ميزانية الشراء الجديدة عند تدهور حالة BTC.

> وقف الخسارة الأساسي تتم مراقبته من البوت وليس أمر Stop مضمونًا داخل MEXC؛ يجب أن تبقى الخدمة والاتصال بالـAPI يعملين.

## Liquidity Swings MTF · Spot

- ماسح دوّار لأزواج MEXC Spot/USDT.
- تحليل 4H و1H و15m و5m باستخدام الشموع المغلقة.
- وضع PAPER ووضع LIVE مع تأكيد صريح من Telegram.
- إدارة صفقات الاستراتيجية في جداول منفصلة عن جداول المحافظ.
- شاشة تشخيص لدورات الفحص والأخطاء وعدد العملات المفحوصة.

التفاصيل في [LIQUIDITY_SWINGS_MTF_README_AR.md](LIQUIDITY_SWINGS_MTF_README_AR.md).

## نقطة التشغيل

يبدأ Railway التطبيق من:

```bash
python start.py
```

ويقوم `start.py` بتشغيل `bot.py` بعد التحقق من المتغيرات المطلوبة.

## متغيرات البيئة

```env
TELEGRAM_BOT_TOKEN=
MEXC_API_KEY=
MEXC_API_SECRET=
DATABASE_URL=
ADMIN_TELEGRAM_ID=
BTC_RISK_GOVERNOR_ENABLED=false
BTC_RISK_STALE_AFTER_SEC=60
BTC_RISK_RECOVERY_POINTS_PER_SEC=0.20
BTC_RISK_MIN_BUY_USDT=1.0
```

على Railway يجب ربط خدمة PostgreSQL بخدمة البوت في نفس المشروع والبيئة، وضبط `DATABASE_URL` كمرجع لمتغير PostgreSQL الصحيح. لا تستخدم عنوانًا داخليًا قديمًا مثل `postgres.railway.internal` إذا لم يكن قابلًا للحل من الخدمة.

## التشغيل المحلي

```bash
python -m pip install -r requirements.txt
python start.py
```

## الاختبارات

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

الاختبارات لا تنفذ أوامر MEXC حقيقية. ابدأ بوضع PAPER وراجع أوامر المنصة قبل تفعيل LIVE.

## الملفات الأساسية

| الملف | الوظيفة |
|---|---|
| `bot.py` | واجهة Telegram ونقطة تشغيل التطبيق |
| `database.py` | نماذج وجداول المحافظ والاستراتيجية |
| `rebalancer.py` | تنفيذ شراء/بيع المحافظ ووضع TP/SL |
| `mexc_client.py` | الاتصال بمنصة MEXC |
| `portfolio_controls.py` | حسابات الدخول والتخفيف والاسترجاع |
| `btc_risk_governor.py` | حماية ميزانية شراء المحافظ |
| `liquidity_swings.py` | منطق استراتيجية Liquidity Swings |
| `liquidity_swings_telegram.py` | واجهة الاستراتيجية داخل Telegram |
| `market_report.py` | تقرير السوق والمحافظ |
