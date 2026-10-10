import os
from dotenv import load_dotenv

load_dotenv()

# Railway may expose the database through an internal hostname. That hostname
# is only resolvable when the app and PostgreSQL are in the same project/environment.
def _database_url():
    configured = (os.getenv("DATABASE_URL") or "").strip()
    public = (os.getenv("DATABASE_PUBLIC_URL") or "").strip()
    if ".railway.internal" in configured:
        return public or configured
    return configured or public


# MEXC (مطلوب)
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
COINLOBSTER_API_KEY = os.getenv("COINLOBSTER_API_KEY", "")
DATABASE_URL = _database_url()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# رقم الأدمن فقط يقدر يستخدم البوت (مستحسن جداً)
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")

DEFAULT_THRESHOLD = 2.0
DEFAULT_MIN_TRADE_USDT = 2.0
# Fixed portfolio Spot order floor. Never schedule a portfolio order below $2 USDT.
PORTFOLIO_MIN_ORDER_USDT = 2.0
# A sell order intentionally leaves a tiny safety buffer, which can remain
# as dust in the wallet. Balances below this market value are not treated as
# a real holding during the missing-coin scan.
BALANCE_PRESENCE_MIN_USDT = float(os.getenv("BALANCE_PRESENCE_MIN_USDT", "1.0"))
QUOTE_ASSET = "USDT"

# BTC Risk Governor settings (disabled by default; values match .env.example).
BTC_RISK_GOVERNOR_ENABLED = os.getenv("BTC_RISK_GOVERNOR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
BTC_RISK_STALE_AFTER_SEC = float(os.getenv("BTC_RISK_STALE_AFTER_SEC", "60"))
BTC_RISK_RECOVERY_POINTS_PER_SEC = float(os.getenv("BTC_RISK_RECOVERY_POINTS_PER_SEC", "0.20"))
BTC_RISK_MIN_BUY_USDT = float(os.getenv("BTC_RISK_MIN_BUY_USDT", "1.0"))
