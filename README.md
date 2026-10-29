# 🤖 Mohamed Rade — بوت إدارة محافظ MEXC Spot

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![Tests](https://github.com/magde2010/magde/actions/workflows/tests.yml/badge.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Platform](https://img.shields.io/badge/Platform-Telegram-26A5E4?logo=telegram&logoColor=white)
![Exchange](https://img.shields.io/badge/Exchange-MEXC_Spot-F0B90B)

بوت تيليجرام لإدارة محافظ تداول متعددة على منصة MEXC (**سبوت فقط، بدون فيوتشر**) بأهداف ربح ووقف خسارة يحددها المستخدم لكل عملة، مع سلالم DCA مُدارة سحابيًا، وإعادة توازن تلقائية، وحمايات تنفيذ أوامر، وواجهة عربية كاملة الاتجاه (RTL).

<div dir="rtl" lang="ar">

## ✨ المميزات

- **إدارة محافظ متعددة** — إنشاء المحافظ وتشغيلها وإيقافها، إضافة وحذف العملات، إدارة المراكز، وتنظيف البيانات.
- **أهداف ربح ووقف خسارة** — إعداد يدوي لكل عملة، مع مراقب سحابي يعمل على السيرفر (الافتراضي كل 15 ثانية، ويمكن إنزاله إلى 5 ثوانٍ) وتنبيه عند فشل التنفيذ.
- **سلالم DCA مُدارة سحابيًا** — نسبة الدخول الأولي قابلة للضبط من 10% إلى 90% من ميزانية المحفظة، و1–3 مستويات شراء لكل عملة، ولكل مستوى **وزن مستقل** من احتياطي DCA و**نسبة هبوط مستقلة** عن سعر الدخول (الافتراضي: 50% عند هبوط 3% — 30% عند 6% — 20% عند 10%).
- **إعادة توازن عامة** — محرك كامل مع حماية احتياطي DCA على مستوى الحساب المشترك بين المحافظ.
- **حمايات تنفيذ الأوامر** — تصنيف نتائج الإرسال إلى (ناجح / مرفوض / يحتاج مراجعة يدوية)، منع بيع رصيد مخصص لمركز في محفظة نشطة أخرى على نفس الحساب، ورفض البيع عند وجود أوامر بيع مفتوحة غير محسومة.
- **واجهة عربية احترافية** — نظام واجهة موحد (`ui_theme`) مع إصلاح اتجاه النص RTL تلقائيًا (`ui_bidi`) وقاموس تبسيط للمصطلحات.
- **صلاحيات مقفلة** — البوت يرفض أي مستخدم غير `ADMIN_TELEGRAM_ID`، ويفشل الإقلاع بالكامل إذا غاب المتغير أو كان غير صالح (fail-closed).
- **نسخة واحدة فقط** — قفل على مستوى قاعدة البيانات (advisory lock في PostgreSQL) يمنع تشغيل نسختين من البوت على نفس القاعدة.

## 📋 المتطلبات

| المتطلب | الإصدار |
|---|---|
| Python | **3.12+** (نفس إصدار الـ CI) |
| قاعدة بيانات | PostgreSQL للإنتاج · SQLite للتجربة المحلية فقط |
| حساب MEXC | مفاتيح Spot فقط، **مع تعطيل صلاحية السحب** |
| بوت تيليجرام | توكن من [@BotFather](https://t.me/BotFather) |

> SQLite مناسب للتجربة المحلية فقط: القفل الاحترازي بين النسخ يعمل على PostgreSQL، أما محليًا فيُستخدم قفل ملف (`fcntl`) لا يصلح على بيئات النشر ذات القرص المؤقت.

## 🚀 التشغيل السريع محليًا

```bash
# 1) استنساخ المشروع وتثبيت المتطلبات
git clone https://github.com/magde2010/magde.git
cd magde
python3 -m venv .venv && source .venv/bin/activate   # ويندوز: .venv\Scripts\activate
pip install -r requirements.txt

# 2) إعداد متغيرات البيئة
cp .env.example .env      # ثم املأ القيم الإلزامية (ويندوز: copy .env.example .env)

# 3) التحقق قبل التشغيل (اختياري لكن مستحسن)
pip install -r requirements-dev.txt
python -m pytest -q

# 4) تشغيل البوت
python start.py
```

**ملاحظات مهمة قبل أول تشغيل:**

- `ADMIN_TELEGRAM_ID` هو **رقم** وليس اسم مستخدم — خذه من [@userinfobot](https://t.me/userinfobot).
- البوت لا يقلع إذا كان أي من المتغيرات الإلزامية غائبًا، ورسالة الخطأ تسمّي المتغير الناقص.
- ابدأ بمبالغ صغيرة أو حساب تجريبي قبل أي تداول حقيقي.

## ☁️ النشر على Railway

1. اربط المستودع بمشروع Railway جديد.
2. أضف خدمة **PostgreSQL** واستخدم متغير `DATABASE_URL` المولّد تلقائيًا.
3. عيّن متغيرات البيئة الإلزامية (خمسة أسماء، ست قيم — لأن MEXC له مفتاح وسر).
4. النشر يستخدم `railway.toml` تلقائيًا (`python start.py`) — و`Procfile` متوفر لمنصات Heroku-style.
5. اضبط عدد النسخ على **نسخة واحدة (one replica)**؛ تشغيل نسختين على نفس التوكن يسبب تعارض `getUpdates`.

### التشغيل على Hostless / منصات Worker

البوت يستخدم **Telegram Long Polling** ولا يحتاج منفذ HTTP، لذلك أنشئ المورد من نوع **Worker** وليس Site أو Web App. التفاصيل الكاملة (PostgreSQL، نسخة واحدة، منع تعارض `getUpdates`) في [`docs/HOSTLESS_DEPLOYMENT_AR.md`](docs/HOSTLESS_DEPLOYMENT_AR.md).

## 🔧 متغيرات البيئة

| المتغير | إلزامي | الوصف |
|---|:---:|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | توكن البوت من [@BotFather](https://t.me/BotFather) |
| `MEXC_API_KEY` / `MEXC_API_SECRET` | ✅ | مفاتيح MEXC بصلاحيات **Spot فقط** — عطّل صلاحية السحب |
| `DATABASE_URL` | ✅ | Postgres (Railway) أو SQLite محليًا (`sqlite:///bot.db`) |
| `ADMIN_TELEGRAM_ID` | ✅ | معرّف تيليجرام موجب لصاحب البوت — أي رقم آخر يُرفض |
| `STOP_MONITOR_INTERVAL_SECONDS` | ➖ | فترة مراقب وقف الخسارة بالثواني (الحد الأدنى 5، **الافتراضي 15**) |
| `BALANCE_PRESENCE_MIN_USDT` | ➖ | أدنى قيمة سوقية تُعتبر حيازة حقيقية أثناء فحص العملات الناقصة (افتراضي 1.0) |
| `UI_BIDI_FIX` | ➖ | تفعيل إصلاح اتجاه النص العربي (افتراضي `1` = مفعّل، و`0`/`false` للتعطيل) |

> **قيم ثابتة لا تُضبط عبر البيئة:** حد أمر المحفظة الأدنى **2 USDT** (`config.py`)، فترة التهدئة قبل إعادة الدخول **90 ثانية** (`rebalancer.py`)، وتسامح مطابقة أوامر DCA المفقودة (`portfolio_strategy.py`).

## 🗂️ هيكل المشروع

```
magde/
├── start.py                  # نقطة الدخول — تتحقق من البيئة قبل الاستيراد
├── bot.py                    # منطق البوت الرئيسي: القوائم، المحادثات، المراقب السحابي
├── config.py                 # قراءة متغيرات البيئة والثوابت
├── database.py               # نماذج SQLAlchemy + قفل المفرد (advisory lock) لمنع تكرار الاستقصاء
├── mexc_client.py            # عميل MEXC لتنفيذ الأوامر والاستعلام عن الأرصدة
├── rebalancer.py             # محرك إعادة التوازن وأهداف الربح/وقف الخسارة
├── portfolio_strategy.py     # حسابات ميزانية الدخول وDCA ومستوياته
├── portfolio_controls.py     # حسابات الميزانية والتقليم الآمن
├── compact_plan.py           # تنسيق خطط العرض (قراءة فقط، لا يغيّر أحجام الأوامر)
├── order_safety.py           # تصنيف نتائج إرسال الأوامر (DCA / بيع سوقي)
├── trading_guard.py          # تسلسل عمليات التداول داخل العملية الواحدة (asyncio lock)
├── ui_theme.py               # نظام واجهة تيليجرام الموحد (4.0-pro)
├── ui_bidi.py                # إصلاح اتجاه النص العربي RTL لكل رسالة
├── conftest.py               # ضبط مسار الاستيراد لاختبارات pytest
├── tests/                    # 76 اختبار وحدة (pytest)
├── docs/                     # مستندات المراجعة والسلامة والتشغيل
├── requirements.txt          # تبعيات الإنتاج
├── requirements-dev.txt      # أدوات التطوير (pytest وruff بإصدار مثبّت)
├── pyproject.toml            # إعدادات المشروع وأداة الفحص ruff
├── railway.toml              # إعداد نشر Railway
├── Procfile                  # بديل لمنصات Heroku-style
├── .env.example              # نموذج متغيرات البيئة
├── .editorconfig             # توحيد إعدادات المحرر
├── .github/workflows/        # CI: تشغيل الاختبارات والفحص الجودي تلقائيًا
├── CHANGELOG.md
└── LICENSE
```

## 🧪 الاختبارات

```bash
pip install -r requirements-dev.txt
python -m pytest -q      # 76 اختبارًا
ruff check .             # بوابة الجودة
```

الاختبارات تغطي منطق سلالم DCA والمستويات المستقلة، تصنيف سلامة الأوامر، حجوزات احتياطي DCA، وأهداف/وقف إعادة التوازن — كلها ببيانات وهمية وبدون أي اتصال بـ MEXC أو تيليجرام. يعمل ملف `.github/workflows/tests.yml` على كل push وPR، ويشغّل: `ruff check` + ترجمة كل الوحدات + الاختبارات.

> أدوات الفحص مثبّتة الإصدار في `requirements-dev.txt`، لأن تثبيت `ruff` بأحدث إصدار كان يجعل الفرع الرئيسي يفشل تلقائيًا عند صدور قواعد جديدة.

## 🩺 مشاكل شائعة

| العَرَض | السبب والحل |
|---|---|
| `Missing required environment variables: ...` | متغير إلزامي غير مضبوط — الرسالة تسمّيه بالاسم. |
| `ADMIN_TELEGRAM_ID must be a valid positive Telegram user ID` | القيمة نص أو صفر — استخدم الرقم من @userinfobot. |
| البوت لا يستجيب وتظهر أخطاء `Conflict: terminated by other getUpdates` | نسخة أخرى تعمل بنفس التوكن — أوقفها واضبط نسخة واحدة فقط. |
| `sqlalchemy.exc.ArgumentError` على رابط القاعدة | استخدم `postgresql://` وليس `postgres://` (الكود يصلح الصيغة القديمة تلقائيًا، لكن أبقِ الرابط حديثًا). |
| فشل `ruff check` في CI بعد تحديث المكتبات | ثبّت الإصدار في `requirements-dev.txt` — لا تُثبّت ruff بدون إصدار. |

## 📚 المستندات

| الملف | المحتوى |
|---|---|
| [`GLOSSARY_AR.md`](docs/GLOSSARY_AR.md) | قاموس تبسيط المصطلحات |
| [`DCA_INDEPENDENT_LEVELS_AR.md`](docs/DCA_INDEPENDENT_LEVELS_AR.md) | المستويات المستقلة لـ DCA |
| [`CHANGES_DCA_AR.md`](docs/CHANGES_DCA_AR.md) | تغييرات نظام DCA السحابي |
| [`SAFETY_FIXES_AR.md`](docs/SAFETY_FIXES_AR.md) | إصلاحات السلامة |
| [`AUDIT_REVIEW_AR.md`](docs/AUDIT_REVIEW_AR.md) | مراجعة إعادة التوازن والشراء والبيع وميزانية DCA |
| [`EXCHANGE_ORDERS_REVIEW_AR.md`](docs/EXCHANGE_ORDERS_REVIEW_AR.md) | مراجعة أوامر المنصة |
| [`SECURITY_REVIEW_NOTES.md`](docs/SECURITY_REVIEW_NOTES.md) | ملاحظات الإصلاح الأمني والتشغيلي (بالإنجليزية) |
| [`HOSTLESS_DEPLOYMENT_AR.md`](docs/HOSTLESS_DEPLOYMENT_AR.md) | التشغيل على Worker + PostgreSQL + منع تعارض getUpdates |
| [`LOCAL_FIX_REVIEW_AR.md`](docs/LOCAL_FIX_REVIEW_AR.md) | تعديلات محلية للمراجعة (مرجع تاريخي) |

## 🔐 ملاحظات أمنية

- استخدم مفاتيح MEXC بصلاحيات Spot اللازمة **فقط**، وعطّل صلاحية السحب نهائيًا.
- اختبر التغييرات على حساب تجريبي أو بمبالغ صغيرة قبل التداول الحقيقي.
- قفل النسخة الواحدة يمنع تشغيل نسختين من البوت على نفس قاعدة البيانات.
- لا تشارك `.env` مع أي أحد — وهو مستثنى من المستودع عبر `.gitignore`.
- لا تُرسل التوكن أو المفاتيح في أي issue أو محادثة دعم.

## 📄 الترخيص

MIT — انظر [`LICENSE`](LICENSE).

</div>

---

<div dir="ltr" lang="en">

## English Summary

**Mohamed Rade** is a Telegram bot for managing multiple **MEXC Spot** portfolios (spot only — no futures) with per-coin manual take-profit / stop-loss levels, cloud-managed DCA ladders, full portfolio rebalancing, order-execution safety guards, and a fully RTL Arabic UI.

**Key features**

- Multiple portfolios: create, run, stop, add/remove coins, manage positions, cleanup.
- Manual TP/SL per coin with a cloud stop-loss monitor (5s minimum, 15s default interval).
- DCA ladders: initial/DCA split configurable from 10% to 90%, 1–3 buy levels per coin, each level with its own reserve weight and its own drop percentage from entry (defaults: 50%/3% — 30%/6% — 20%/10%).
- Account-wide DCA-reserve protection across concurrent portfolios, duplicate-fill protection.
- Order-safety classification, cross-portfolio ownership checks that refuse to sell balances pledged to another active portfolio, and no selling while unresolved sell orders exist.
- Admin-only access (`ADMIN_TELEGRAM_ID`), fail-closed startup validation, single-instance advisory lock.
- Arabic RTL UI layer (`ui_theme`, `ui_bidi`).

**Requirements:** Python 3.12+, PostgreSQL (production) or SQLite (local only), MEXC spot-only API keys with withdrawals disabled.

**Quick start**

```bash
git clone https://github.com/magde2010/magde.git && cd magde
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill the 5 required names (6 values)
python start.py                 # or deploy to Railway (railway.toml included)
```

**Tests:** `pip install -r requirements-dev.txt && python -m pytest -q` — 76 unit tests, fully mocked, no exchange calls. CI runs `ruff check` + `compileall` + `pytest` on every push/PR.

**Environment variables:** see the table above. Required: `TELEGRAM_BOT_TOKEN`, `MEXC_API_KEY`, `MEXC_API_SECRET`, `DATABASE_URL`, `ADMIN_TELEGRAM_ID`. Optional: `STOP_MONITOR_INTERVAL_SECONDS`, `BALANCE_PRESENCE_MIN_USDT`, `UI_BIDI_FIX`.

**Hostless deployment:** see [`docs/HOSTLESS_DEPLOYMENT_AR.md`](docs/HOSTLESS_DEPLOYMENT_AR.md) for Worker setup, PostgreSQL, one-replica configuration, and Telegram `getUpdates` conflict prevention.

**Security:** use spot-only MEXC API keys with withdrawal disabled; test with small amounts first; never commit `.env`.

**License:** MIT — see [`LICENSE`](LICENSE).

</div>
