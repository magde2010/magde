# 🤖 Mohamed Rade — بوت إدارة محافظ MEXC Spot

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![Tests](https://github.com/magde2010/magde/actions/workflows/tests.yml/badge.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Platform](https://img.shields.io/badge/Platform-Telegram-26A5E4?logo=telegram&logoColor=white)
![Exchange](https://img.shields.io/badge/Exchange-MEXC_Spot-F0B90B)

بوت تيليجرام لإدارة محافظ تداول متعددة على منصة MEXC (**سبوت فقط**) بأهداف ربح ووقف خسارة يدويين، مع سلالم DCA سحابية، إعادة توازن تلقائية، حمايات تنفيذ أوامر، وواجهة عربية كاملة الاتجاه (RTL).

<div dir="rtl">

## ✨ المميزات

- **إدارة محافظ متعددة** — إنشاء المحافظ وتشغيلها وإيقافها، إضافة وحذف العملات، إدارة المراكز، وتنظيف البيانات.
- **أهداف ربح ووقف خسارة** — إعداد يدوي لكل عملة مع مراقب إيقاف سحابي سريع (5 ثوانٍ كحد أدنى) وتنبيهات فشل.
- **سلالم DCA سحابية** — تخصيص 50/50 أو 70/30 بين الشراء الأولي وDCA، مع 1–3 أوامر شراء لكل عملة، وتوزيع المستويات بالتساوي بين سعر الدخول ووقف الخسارة.
- **إعادة توازن عامة** — محرك كامل مع حماية احتياطيات DCA على مستوى الحساب المشترك بين المحافظ.
- **حمايات تنفيذ الأوامر** — تصنيف نتائج الإرسال، منع البيع المزدوج للرصيد المخصص لمحفظة أخرى، فحص الملكية بتسامحات قابلة للضبط، ورفض البيع عند وجود أوامر غير محسومة.
- **واجهة عربية احترافية** — نظام واجهة موحد (`ui_theme`) مع إصلاح اتجاه النص RTL تلقائيًا (`ui_bidi`) وقاموس تبسيط للمصطلحات.
- **صلاحيات مقفلة** — البوت يرفض أي مستخدم غير `ADMIN_TELEGRAM_ID`، ويفشل الإقلاع كاملًا إذا غاب المتغير أو كان غير صالح (fail-closed).

## 🚀 التشغيل السريع محليًا

```bash
# 1) استنساخ المشروع وتثبيت المتطلبات
git clone https://github.com/magde2010/magde.git
cd magde
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) إعداد متغيرات البيئة
cp .env.example .env      # ثم املأ القيم الإلزامية

# 3) تشغيل البوت
python start.py
```

## ☁️ النشر على Railway

1. اربط المستودع بمشروع Railway جديد.
2. أضف خدمة **PostgreSQL** واستخدم متغير `DATABASE_URL` المولّد تلقائيًا.
3. عيّن متغيرات البيئة الإلزامية الخمسة (انظر الجدول أدناه).
4. النشر يستخدم `railway.toml` تلقائيًا (`python start.py`) — و`Procfile` متوفر لمنصات Heroku-style.

## 🔧 متغيرات البيئة

| المتغير | إلزامي | الوصف |
|---|:---:|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | توكن البوت من [@BotFather](https://t.me/BotFather) |
| `MEXC_API_KEY` / `MEXC_API_SECRET` | ✅ | مفاتيح MEXC بصلاحيات **Spot فقط** — عطّل صلاحية السحب |
| `DATABASE_URL` | ✅ | Postgres (Railway) أو SQLite محليًا (`sqlite:///bot.db`) |
| `ADMIN_TELEGRAM_ID` | ✅ | معرّف تيليجرام موجب لصاحب البوت — أي رقم آخر يُرفض |
| `STOP_MONITOR_INTERVAL_SECONDS` | ➖ | فترة مراقب وقف الخسارة بالثواني (الحد الأدنى 5، افتراضي 5) |
| `BALANCE_PRESENCE_MIN_USDT` | ➖ | أدنى قيمة سوقية تُعتبر حيازة حقيقية أثناء فحص العملات الناقصة (افتراضي 1.0) |
| `BTC_PORTFOLIO_ACTION_COOLDOWN_SEC` | ➖ | فترة التهدئة بين إجراءات المحفظة بالثواني (افتراضي 1800) |
| `OWNERSHIP_TOLERANCE_PCT` / `_AMOUNT` / `_USDT` | ➖ | تسامحات فحص ملكية العملة بين المحافظ (افتراضي 1.5% / 0.02 / 1.0) |

> حد أمر المحفظة الأدنى ثابت عند **2 USDT** في `config.py` ولا يُضبط عبر البيئة.

## 🗂️ هيكل المشروع

```
magde/
├── bot.py                  # منطق البوت الرئيسي: القوائم، المحادثات، المراقب السحابي
├── config.py               # قراءة متغيرات البيئة والثوابت
├── database.py             # نماذج SQLAlchemy + قفل المفرد (advisory lock) لمنع تكرار الاستقصاء
├── mexc_client.py          # عميل MEXC لتنفيذ الأوامر والاستعلام عن الأرصدة
├── rebalancer.py           # محرك إعادة التوازن وأهداف الربح/وقف الخسارة
├── portfolio_strategy.py   # حسابات ميزانية الدخول وDCA ومستوياته
├── portfolio_controls.py   # حسابات الميزانية والتقليم الآمن
├── order_safety.py         # تصنيف نتائج إرسال الأوامر (DCA / بيع سوقي)
├── ui_theme.py             # نظام واجهة تيليجرام الموحد (4.0-pro)
├── ui_bidi.py              # إصلاح اتجاه النص العربي RTL لكل رسالة
├── start.py                # نقطة الدخول — تتحقق من البيئة قبل الاستيراد
├── tests/                  # 56 اختبار وحدة (pytest)
├── docs/                   # مستندات المراجعة والسلامة بالعربية
├── .github/workflows/      # CI: تشغيل الاختبارات والفحص الجودي تلقائيًا
└── pyproject.toml          # إعدادات المشروع وأداة الفحص ruff
```

## 🧪 الاختبارات

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

الاختبارات تغطي منطق سلالم DCA والمستويات المستقلة، تصنيف سلامة الأوامر، حجوزات احتياطي DCA، وأهداف/وقف إعادة التوازن — كلها ببيانات وهمية وبدون أي اتصال بـ MEXC أو تيليجرام. يعمل ملف `.github/workflows/tests.yml` تلقائيًا على كل push وPR.

## 📚 المستندات

| الملف | المحتوى |
|---|---|
| [`GLOSSARY_AR.md`](docs/GLOSSARY_AR.md) | قاموس تبسيط المصطلحات |
| [`DCA_INDEPENDENT_LEVELS_AR.md`](docs/DCA_INDEPENDENT_LEVELS_AR.md) | المستويات المستقلة لـ DCA |
| [`CHANGES_DCA_AR.md`](docs/CHANGES_DCA_AR.md) | تغييرات نظام DCA السحابي |
| [`SAFETY_FIXES_AR.md`](docs/SAFETY_FIXES_AR.md) | إصلاحات السلامة |
| [`SECURITY_REVIEW_NOTES.md`](docs/SECURITY_REVIEW_NOTES.md) | ملاحظات الإصلاح الأمني والتشغيلي |
| [`AUDIT_REVIEW_AR.md`](docs/AUDIT_REVIEW_AR.md) | مراجعة إعادة التوازن والشراء والبيع وميزانية DCA |
| [`EXCHANGE_ORDERS_REVIEW_AR.md`](docs/EXCHANGE_ORDERS_REVIEW_AR.md) | مراجعة أوامر المنصة |

## 🔐 ملاحظات أمنية

- استخدم مفاتيح MEXC بصلاحيات Spot اللازمة **فقط**، وعطّل صلاحية السحب نهائيًا.
- اختبر التغييرات على حساب تجريبي أو بمبالغ صغيرة قبل التداول الحقيقي.
- قفل المفرد (advisory lock) يمنع تشغيل نسختين من البوت على نفس قاعدة البيانات.
- لا تشارك `.env` مع أي أحد — وهو مستثنى من المستودع عبر `.gitignore`.

## 📄 الترخيص

MIT — انظر [`LICENSE`](LICENSE).

</div>

---

<div dir="ltr">

## English Summary

**Mohamed Rade** is a Telegram bot for managing multiple **MEXC Spot** portfolios (spot only — no futures) with manual take-profit / stop-loss levels, cloud DCA ladders, full portfolio rebalancing, order-execution safety guards, and a fully RTL Arabic UI.

**Key features**

- Multiple portfolios: create, run, stop, add/remove coins, manage positions, cleanup.
- Manual TP/SL per coin with a fast cloud stop-loss monitor (5s minimum interval).
- DCA ladders: 50/50 or 70/30 initial/DCA split, 1–3 buy orders per coin, levels evenly spaced between entry and the original stop-loss.
- Account-wide DCA-reserve protection across concurrent portfolios, atomic claim of DCA levels, duplicate-fill protection.
- Order-safety classification, cross-portfolio ownership checks with configurable tolerances, no selling balances pledged to another portfolio.
- Admin-only access (`ADMIN_TELEGRAM_ID`), fail-closed startup validation, single-instance advisory lock.

**Quick start**

```bash
git clone https://github.com/magde2010/magde.git && cd magde
pip install -r requirements.txt
cp .env.example .env   # fill required values
python start.py        # or deploy to Railway (railway.toml included)
```

**Tests:** `pip install pytest && python -m pytest -q` — 56 unit tests, fully mocked, no exchange calls.

**Environment variables:** see the table above (required: `TELEGRAM_BOT_TOKEN`, `MEXC_API_KEY`, `MEXC_API_SECRET`, `DATABASE_URL`, `ADMIN_TELEGRAM_ID`).

**Hostless deployment:** see [`docs/HOSTLESS_DEPLOYMENT_AR.md`](docs/HOSTLESS_DEPLOYMENT_AR.md) for Worker setup, PostgreSQL, one-replica configuration, and Telegram `getUpdates` conflict prevention.

**Security:** use spot-only MEXC API keys with withdrawal disabled; test with small amounts first.

**License:** MIT — see [`LICENSE`](LICENSE).

</div>
