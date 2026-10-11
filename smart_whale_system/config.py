import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === API Keys ===
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
COINLOBSTER_API_KEY = os.getenv("COINLOBSTER_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")

# === Database ===
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///smart_whale.db")

# === System Settings ===
ALLOCATED_CAPITAL = float(os.getenv("ALLOCATED_CAPITAL", "150"))
POSITION_SIZE_USDT = float(os.getenv("POSITION_SIZE_USDT", "30"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MIN_MULTIPLE = float(os.getenv("MIN_MULTIPLE", "1.5"))
FLOW_MIN_SCORE = float(os.getenv("FLOW_MIN_SCORE", "82"))
FLOW_CONFIRMATIONS = int(os.getenv("FLOW_CONFIRMATIONS", "2"))
RADAR_WINDOW = os.getenv("RADAR_WINDOW", "4h")
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "7.0"))
TAKE_PROFIT_1_PCT = float(os.getenv("TAKE_PROFIT_1_PCT", "8.0"))
TAKE_PROFIT_2_PCT = float(os.getenv("TAKE_PROFIT_2_PCT", "15.0"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))

QUOTE = "USDT"

# === Safety ===
MIN_ORDER_USDT = float(os.getenv("MIN_ORDER_USDT", "5.0"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "1.5"))

# إعدادات التداول التي يغيرها المستخدم من بوت Telegram.
# مفاتيح API لا تُحفظ هنا ولا تُقبل من رسائل البوت.
RUNTIME_SETTINGS_FILE = Path(__file__).resolve().parent / "runtime_settings.json"
try:
    runtime = json.loads(RUNTIME_SETTINGS_FILE.read_text(encoding="utf-8"))
    if isinstance(runtime, dict):
        ALLOCATED_CAPITAL = float(runtime.get("allocated_capital", ALLOCATED_CAPITAL))
        POSITION_SIZE_USDT = float(runtime.get("position_size_usdt", POSITION_SIZE_USDT))
        MAX_OPEN_POSITIONS = int(runtime.get("max_open_positions", MAX_OPEN_POSITIONS))
        MIN_MULTIPLE = float(runtime.get("min_multiple", MIN_MULTIPLE))
        FLOW_MIN_SCORE = float(runtime.get("min_score", FLOW_MIN_SCORE))
        FLOW_CONFIRMATIONS = int(runtime.get("confirmations_required", FLOW_CONFIRMATIONS))
        RADAR_WINDOW = str(runtime.get("radar_window", RADAR_WINDOW))
        STOP_LOSS_PCT = float(runtime.get("stop_loss_pct", STOP_LOSS_PCT))
        TAKE_PROFIT_1_PCT = float(runtime.get("take_profit_1_pct", TAKE_PROFIT_1_PCT))
        TAKE_PROFIT_2_PCT = float(runtime.get("take_profit_2_pct", TAKE_PROFIT_2_PCT))
        CHECK_INTERVAL_MINUTES = int(runtime.get("check_interval_minutes", CHECK_INTERVAL_MINUTES))
        MIN_ORDER_USDT = float(runtime.get("min_order_usdt", MIN_ORDER_USDT))
        MAX_SLIPPAGE_PCT = float(runtime.get("max_slippage_pct", MAX_SLIPPAGE_PCT))
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    pass

# System name
SYSTEM_NAME = "نظام Flow Engine الذكي"
SYSTEM_VERSION = "2.0.0"
