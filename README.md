# LOTFE — Portfolio Manager + Liquidity Swings MTF

بوت Telegram يعمل على **MEXC Spot** ويحتوي على نظامين فقط:

1. **نظام المحافظ** لإدارة المحافظ والمراكز يدويًا.
2. **Liquidity Swings MTF** لمسح سوق MEXC وإدارة صفقات الاستراتيجية.

## نظام المحافظ

يدعم إنشاء المحافظ وتشغيلها وإيقافها وإعادة بنائها، شراء العملات الناقصة، أهداف TP1/TP2/TP3، وقف الخسارة، إعادة الدخول، تخفيف الحيازة واسترجاعها، وسجل الصفقات. توجد حماية من تشغيل نفس الرمز في محافظ متعددة على حساب MEXC واحد.

`btc_risk_governor.py` يخفض ميزانية الشراء الجديدة عند تدهور حالة BTC. وقف الخسارة الأساسي تتم مراقبته من البوت وليس أمر Stop مضمونًا داخل MEXC.

## Liquidity Swings MTF

تعمل الاستراتيجية على أزواج MEXC Spot/USDT وتفحص الأطر 4H و1H و15m و5m باستخدام الشموع المغلقة. تدعم PAPER وLIVE، مع حفظ صفقات الاستراتيجية في جداول مستقلة عن المحافظ.

## التشغيل

نقطة التشغيل:

```bash
python start.py
```

يستخدم Railway الأمر نفسه من `railway.toml`.

## متغيرات البيئة المطلوبة

```env
TELEGRAM_BOT_TOKEN=
MEXC_API_KEY=
MEXC_API_SECRET=
DATABASE_URL=
ADMIN_TELEGRAM_ID=
```

إعدادات اختيارية لحماية مشتريات المحافظ:

```env
BTC_RISK_GOVERNOR_ENABLED=false
BTC_RISK_STALE_AFTER_SEC=60
BTC_RISK_RECOVERY_POINTS_PER_SEC=0.20
BTC_RISK_MIN_BUY_USDT=1.0
```

على Railway يجب ربط PostgreSQL بخدمة البوت في نفس المشروع والبيئة، وضبط `DATABASE_URL` كمرجع لمتغير PostgreSQL الصحيح.

## الملفات

- `bot.py`: واجهة Telegram وتشغيل النظامين.
- `database.py`: جداول المحافظ والاستراتيجية.
- `mexc_client.py`: الاتصال بـMEXC.
- `rebalancer.py`: تنفيذ عمليات المحافظ ووضع TP/SL.
- `portfolio_controls.py`: حسابات ميزانية المحافظ والتخفيف والاسترجاع.
- `market_report.py`: تقرير السوق والمحافظ.
- `btc_risk_governor.py`: حماية ميزانية شراء المحافظ.
- `liquidity_swings.py`: منطق Liquidity Swings MTF.
- `liquidity_swings_telegram.py`: واجهة الاستراتيجية في Telegram.
- `config.py`: الإعدادات.
- `start.py`: نقطة التشغيل.
- `requirements.txt`: الاعتمادات.
- `railway.toml`: إعداد النشر على Railway.
