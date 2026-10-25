import math
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
# Fail closed: missing/invalid admin ID must never grant public access.
try:
    ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "") or "")
    ADMIN_TELEGRAM_ID = max(0, ADMIN_TELEGRAM_ID)
except (TypeError, ValueError):
    ADMIN_TELEGRAM_ID = 0

def optional_env_float(name: str, default: float, minimum: float | None = None) -> float:
    """Read an optional finite float without making a configuration typo crash import.

    Required credentials are still validated by ``start.py``. Optional tuning
    values, however, should fall back to a documented safe default rather than
    leaving a worker restart-looping because of an accidental value such as
    ``five`` or ``NaN`` in Railway.
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = float(default)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(float(minimum), value) if minimum is not None else value


DEFAULT_THRESHOLD = 2.0
DEFAULT_MIN_TRADE_USDT = 2.0
# Fixed portfolio Spot order floor. Never schedule a portfolio order below $2 USDT.
PORTFOLIO_MIN_ORDER_USDT = 2.0
# A sell order intentionally leaves a tiny safety buffer, which can remain
# as dust in the wallet. Balances below this market value are not treated as
# a real holding during the missing-coin scan.
BALANCE_PRESENCE_MIN_USDT = optional_env_float("BALANCE_PRESENCE_MIN_USDT", 1.0, 0.0)
QUOTE_ASSET = "USDT"
# Cloud polling for the software stop-loss. Five seconds remains supported, but
# fifteen seconds is a safer default for portfolios with many open positions.
STOP_MONITOR_INTERVAL_SECONDS = optional_env_float("STOP_MONITOR_INTERVAL_SECONDS", 15.0, 5.0)
