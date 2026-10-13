# FAST SCALPER

وحدة مستقلة عن المحافظ.

## الوضع التجريبي

```env
SCALPER_MODE=PAPER
SCALPER_LIVE_ARMED=false
```

لا ترسل أي أمر إلى MEXC. تحاكي الدخول عند الـAsk والخروج عند الـBid وتستخدم نفس شروط الاستراتيجية.

## الوضع الحقيقي

لا تستخدمه إلا بعد مراقبة الـPAPER:

```env
SCALPER_MODE=LIVE
SCALPER_LIVE_ARMED=true
```

المحرك مستقل عن `Rebalancer` وإدارة المحافظ الأساسية.

## قواعد البداية

- Spot / Long only
- 5 USDT للصفقة افتراضيًا
- الحد الأدنى 2 USDT
- حد أقصى 3 صفقات مفتوحة
- TP 0.35%
- SL 0.20%
- Time stop 60 ثانية
- Daily max loss 2 USDT
- 5 خسائر متتالية توقف الدخول
- BTC يجب أن يكون موجبًا على 5s/30s/60s
- Spread <= 0.12%
- Order-book imbalance >= 0.60
- العملات تُرتب حسب سيولة 24 ساعة
- التنفيذ الحقيقي يستخدم Post-Only Limit للدخول ثم Market للخروج

## مهم

الـPAPER هنا محاكاة تنفيذ وليست Backtest تاريخيًا. قبل LIVE يجب مراقبة نتائج الورق ثم مقارنة التنفيذ الفعلي بالـslippage والرسوم.
