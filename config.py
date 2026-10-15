import os
from dotenv import load_dotenv

load_dotenv()

# MEXC (مطلوب)
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")
# Railway/Heroku may expose the legacy `postgres://` scheme, which SQLAlchemy 2.x
# rejects. Normalise it so the process can actually start against Postgres.
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# رقم الأدمن فقط يقدر يستخدم البوت (مستحسن جداً)
try:
    ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")
except (TypeError, ValueError):
    ADMIN_TELEGRAM_ID = 0

DEFAULT_THRESHOLD = 2.0
DEFAULT_MIN_TRADE_USDT = 2.0
# Fixed portfolio Spot order floor. Never schedule a portfolio order below $2 USDT.
PORTFOLIO_MIN_ORDER_USDT = 2.0
# A sell order intentionally leaves a tiny safety buffer, which can remain
# as dust in the wallet. Balances below this market value are not treated as
# a real holding during the missing-coin scan.
BALANCE_PRESENCE_MIN_USDT = float(os.getenv("BALANCE_PRESENCE_MIN_USDT", "1.0"))
QUOTE_ASSET = "USDT"
# Fast cloud polling for the software stop-loss. Five seconds is the minimum
# allowed value to avoid excessive exchange API requests.
STOP_MONITOR_INTERVAL_SECONDS = max(
    5.0, float(os.getenv("STOP_MONITOR_INTERVAL_SECONDS", "5"))
)
