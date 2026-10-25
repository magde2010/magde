# تشغيل سالم على Hostless

## نوع المورد

أنشئ المورد كـ **Worker** وليس Site أو Web App، لأن البوت يستخدم Telegram Long Polling ولا يحتاج إلى منفذ HTTP.

- المستودع: `salemahmedsa/salem`
- الفرع: `main`
- أمر البناء: `pip install -r requirements.txt`
- أمر التشغيل: `python start.py`
- عدد النسخ: نسخة واحدة فقط (`Replicas = 1`)

لا تشغّل نفس `TELEGRAM_BOT_TOKEN` في Worker آخر أو Deployment قديم. Telegram يسمح بطلب `getUpdates` واحد فقط لكل توكن؛ تشغيل نسختين ينتج الخطأ:

```text
telegram.error.Conflict: terminated by other getUpdates request
```

أوقف أي Deployment قديم قبل تشغيل النسخة الجديدة، وانتظر حتى يتوقف بالكامل، ثم شغّل Worker واحدًا فقط.

## المتغيرات

أضف المتغيرات التالية في Environment Variables داخل Hostless:

```env
TELEGRAM_BOT_TOKEN=...
MEXC_API_KEY=...
MEXC_API_SECRET=...
DATABASE_URL=postgresql://...
ADMIN_TELEGRAM_ID=...
STOP_MONITOR_INTERVAL_SECONDS=15
BALANCE_PRESENCE_MIN_USDT=1.0
UI_BIDI_FIX=1
```

استخدم رابط PostgreSQL الداخلي إذا كانت قاعدة البيانات والـ Worker داخل نفس مشروع Hostless. يجب ألا تضع التوكن أو مفاتيح MEXC أو رابط قاعدة البيانات داخل GitHub.

## سبب تحذير max_instances

مهمة `monitor_positions_job` تفحص المراكز دوريًا. مع عدد كبير من العملات قد تستغرق الدورة أكثر من الفترة المحددة، فيظهر:

```text
تم تخطي تنفيذ المهمة ... max_instances=1
```

هذا التحذير يعني أن الدورة السابقة ما زالت تعمل، وليس أن المهمة فشلت. القيمة الافتراضية أصبحت 15 ثانية لتقليل ضغط MEXC وتحسين استجابة Telegram. يمكن استخدام 5 ثوانٍ فقط إذا كانت الدورة تنتهي بسرعة.

## بعد النشر

1. احفظ المتغيرات.
2. تأكد أن عدد النسخ يساوي 1.
3. أوقف Deployments القديمة.
4. نفّذ Restart أو Redeploy مرة واحدة.
5. راجع السجلات وابحث عن:

```text
Bot starting (MEXC Portfolio Manager)...
Position monitor job scheduled
```
