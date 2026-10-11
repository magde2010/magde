# Lotfe — Flow Engine Smart Whale System v2

النظام لم يعد يعتمد على Whale Radar وحده. أصبح يستخدم طبقة Flow Engine مشتركة مع البوت الأساسي:

- 🐋 تدفق/نشاط الحيتان من CoinLobster Radar
- 📈 حركة السعر وEMA20 على MEXC Spot
- 📊 تسارع الحجم
- CVD تقريبي من آخر الصفقات
- Order Book imbalance
- حالة BTC العامة
- 🚫 Anti-Chase: يمنع ملاحقة الارتفاعات الحادة
- ✅ تأكيد متتالٍ قبل الشراء

## منطق الدخول

لا تتحول إشارة واحدة إلى Buy. يجب أن تكون العملة مؤهلة، وتتجاوز Flow Score، ثم تمر بعدد التأكيدات المطلوب. في الوضع الحقيقي يرفع النظام الحد المطلوب ويستخدم تأكيدات أكثر.

## Environment Variables

```
MEXC_API_KEY=
MEXC_API_SECRET=
COINLOBSTER_API_KEY=
TELEGRAM_BOT_TOKEN=
ADMIN_TELEGRAM_ID=
DATABASE_URL=sqlite:///smart_whale.db

ALLOCATED_CAPITAL=150
POSITION_SIZE_USDT=30
MAX_OPEN_POSITIONS=5
MIN_MULTIPLE=1.5
FLOW_MIN_SCORE=82
FLOW_CONFIRMATIONS=2
RADAR_WINDOW=1h
STOP_LOSS_PCT=7.0
TAKE_PROFIT_1_PCT=8.0
TAKE_PROFIT_2_PCT=15.0
CHECK_INTERVAL_MINUTES=10
FLOW_RADAR_WINDOW=1h
FLOW_MIN_SCORE_BUY=82
FLOW_CONFIRMATION_MINUTES=15
FLOW_ENABLE_MEXC_MARKET_DATA=true
```

يمكن ترك `COINLOBSTER_API_KEY` فارغًا؛ سيستخدم الرادار العام كـfallback، لكن البيانات المدفوعة تكون أعمق عند وجود المفتاح.

## التشغيل

```bash
pip install -r requirements.txt
python main.py
python main.py status
```

الوضع الحقيقي يتطلب مفاتيح MEXC. اختبر Paper/الرادار أولًا، ولا تعتبر الإشارة ضمانًا للربح.
