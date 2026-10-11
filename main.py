#!/usr/bin/env python3
"""
MEXC SPOT Auto Trader v2 - Full Telegram Control
- Web Preview only (no API ID/Hash)
- SPOT BUY only for LONG
- Inline buttons for everything
- Max open positions limit
- 3 Take-Profit levels + trailing stop (move SL to entry on TP1)
- Signal Stop used as initial SL
- Manage channels from bot
- SQLite persistence
"""

import os
import sys
import time
import json
import re
import hashlib
import logging
import traceback
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup
import ccxt
from dotenv import load_dotenv

# Optional Postgres support (Railway DATABASE_URL)
try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

load_dotenv()

# ==================== PATHS & CONFIG ====================
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "trader.db"
LOG_FILE = DATA_DIR / "trader.log"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_ID = str(os.getenv("TELEGRAM_ADMIN_ID", "")).strip()
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "").strip()
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY", "").strip()

# === Defaults (managed only from Telegram Bot after first run) ===
DEFAULT_TRADE_AMOUNT = 10.0
DEFAULT_PAPER = True
DEFAULT_TRADING = False
POLL_INTERVAL = 5
DEFAULT_MAX_POS = 3
MAX_TRADE_HARD_LIMIT = 500.0
DEFAULT_TP_PCTS = [5.0, 10.0, 15.0]  # +5% / +10% / +15%

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("MEXC-SPOT-V2")

# ==================== DATABASE ====================
# Supports both SQLite (default) and Postgres (if DATABASE_URL is set on Railway)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL and HAS_PSYCOPG2)

def get_db():
    """Return a DB connection. Postgres if DATABASE_URL exists, else SQLite."""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        return conn
    else:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

def _execute(conn, sql, params=None):
    """Helper that works for both SQLite and Postgres (converts ? to %s)."""
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    return cur

def init_db():
    conn = get_db()
    cur = conn.cursor() if not USE_POSTGRES else conn.cursor()

    if USE_POSTGRES:
        # Postgres schema
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_msg_id BIGINT DEFAULT 0,
                template TEXT DEFAULT 'auto',
                sample_text TEXT DEFAULT '',
                added_at TEXT
            )
        """)
        try:
            cur.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS template TEXT DEFAULT 'auto'")
        except Exception:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                qty DOUBLE PRECISION,
                remaining_qty DOUBLE PRECISION,
                entry_price DOUBLE PRECISION,
                stop_loss DOUBLE PRECISION,
                current_sl DOUBLE PRECISION,
                tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION, tp3 DOUBLE PRECISION,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                tp3_hit INTEGER DEFAULT 0,
                order_id TEXT,
                signal_raw TEXT,
                opened_at TEXT,
                paper INTEGER DEFAULT 1,
                status TEXT DEFAULT 'open'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades_history (
                id SERIAL PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                qty DOUBLE PRECISION,
                price DOUBLE PRECISION,
                pnl DOUBLE PRECISION,
                reason TEXT,
                paper INTEGER,
                closed_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                msg_key TEXT PRIMARY KEY,
                processed_at TEXT
            )
        """)
    else:
        # SQLite schema
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_msg_id INTEGER DEFAULT 0,
                template TEXT DEFAULT 'auto',
                sample_text TEXT DEFAULT '',
                added_at TEXT
            )
        """)
        try:
            cur.execute("ALTER TABLE channels ADD COLUMN template TEXT DEFAULT 'auto'")
        except Exception:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                qty REAL,
                remaining_qty REAL,
                entry_price REAL,
                stop_loss REAL,
                current_sl REAL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                tp3_hit INTEGER DEFAULT 0,
                order_id TEXT,
                signal_raw TEXT,
                opened_at TEXT,
                paper INTEGER DEFAULT 1,
                status TEXT DEFAULT 'open'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                side TEXT,
                qty REAL,
                price REAL,
                pnl REAL,
                reason TEXT,
                paper INTEGER,
                closed_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                msg_key TEXT PRIMARY KEY,
                processed_at TEXT
            )
        """)

    # defaults
    defaults = {
        "trade_amount": str(DEFAULT_TRADE_AMOUNT),
        "paper_mode": "1" if DEFAULT_PAPER else "0",
        "trading_enabled": "1" if DEFAULT_TRADING else "0",
        "max_positions": str(DEFAULT_MAX_POS),
        "tp1_pct": str(DEFAULT_TP_PCTS[0]),
        "tp2_pct": str(DEFAULT_TP_PCTS[1]),
        "tp3_pct": str(DEFAULT_TP_PCTS[2]),
    }
    for k, v in defaults.items():
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )
        else:
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # default channel
    if USE_POSTGRES:
        cur.execute(
            "INSERT INTO channels (username, enabled, last_msg_id, added_at) VALUES (%s, 1, 0, %s) ON CONFLICT (username) DO NOTHING",
            ("abojasimp", datetime.now(timezone.utc).isoformat())
        )
    else:
        cur.execute(
            "INSERT OR IGNORE INTO channels (username, enabled, last_msg_id, added_at) VALUES (?, 1, 0, ?)",
            ("abojasimp", datetime.now(timezone.utc).isoformat())
        )

    conn.commit()

    # --- Ensure new columns exist (safe for already-created tables) ---
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS template TEXT DEFAULT 'auto'")
            cur.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS sample_text TEXT DEFAULT ''")
            conn.commit()
        else:
            for col, default in [("template", "'auto'"), ("sample_text", "''")]:
                try:
                    conn.execute(f"ALTER TABLE channels ADD COLUMN {col} TEXT DEFAULT {default}")
                    conn.commit()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Column migration warning: {e}")

    conn.close()
    db_type = "PostgreSQL (Railway)" if USE_POSTGRES else "SQLite"
    logger.info(f"Database initialized → {db_type}")

def db_get(key: str, default: str = "") -> str:
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return row["value"] if row else default
        else:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
    finally:
        conn.close()

def db_set(key: str, value: str):
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value))
            )
        else:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()

def row_value(row, key: str, default=None):
    """Read a DB row consistently from sqlite3.Row and dict-like PostgreSQL rows."""
    if row is None:
        return default
    try:
        value = row.get(key, default)
    except AttributeError:
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return default
    return default if value is None else value


def get_settings() -> Dict:
    return {
        "trade_amount": float(db_get("trade_amount", str(DEFAULT_TRADE_AMOUNT))),
        "paper_mode": db_get("paper_mode", "1") == "1",
        "trading_enabled": db_get("trading_enabled", "0") == "1",
        "max_positions": int(db_get("max_positions", str(DEFAULT_MAX_POS))),
        "tp1_pct": float(db_get("tp1_pct", "5")),
        "tp2_pct": float(db_get("tp2_pct", "10")),
        "tp3_pct": float(db_get("tp3_pct", "15")),
    }

# ==================== TELEGRAM (with Inline Buttons) ====================
def tg_api(method: str, payload: dict = None) -> Optional[dict]:
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload or {}, timeout=20)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"TG API {method} status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        # Request exception strings can contain the bot-token URL; never log them verbatim.
        logger.error("TG API error for %s: %s", method, type(e).__name__)
    return None

def tg_send(text: str, reply_markup: dict = None, chat_id: str = None) -> bool:
    cid = chat_id or TELEGRAM_ADMIN_ID
    if not cid:
        return False
    payload = {
        "chat_id": cid,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = tg_api("sendMessage", payload)
    return bool(res and res.get("ok"))

def tg_edit(chat_id: str, message_id: int, text: str, reply_markup: dict = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_api("editMessageText", payload)

def tg_answer_callback(callback_id: str, text: str = ""):
    tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

def main_menu_keyboard() -> dict:
    s = get_settings()
    return {
        "inline_keyboard": [
            [
                {"text": f"{'🟢' if s['trading_enabled'] else '🔴'} التداول: {'ON' if s['trading_enabled'] else 'OFF'}", "callback_data": "toggle_trading"},
                {"text": f"{'📝' if s['paper_mode'] else '💰'} Paper: {'ON' if s['paper_mode'] else 'OFF'}", "callback_data": "toggle_paper"},
            ],
            [
                {"text": f"💵 المبلغ: {s['trade_amount']} USDT", "callback_data": "set_amount"},
                {"text": f"📊 حد الصفقات: {s['max_positions']}", "callback_data": "set_maxpos"},
            ],
            [
                {"text": f"🎯 TP1: {s['tp1_pct']}%", "callback_data": "set_tp1"},
                {"text": f"🎯 TP2: {s['tp2_pct']}%", "callback_data": "set_tp2"},
                {"text": f"🎯 TP3: {s['tp3_pct']}%", "callback_data": "set_tp3"},
            ],
            [
                {"text": "📈 الصفقات المفتوحة", "callback_data": "positions"},
                {"text": "💰 الرصيد", "callback_data": "balance"},
            ],
            [
                {"text": "📡 القنوات", "callback_data": "channels"},
                {"text": "📜 آخر الإشارات", "callback_data": "last_signals"},
            ],
            [
                {"text": "⚙️ الإعدادات", "callback_data": "settings"},
                {"text": "🛑 Kill Switch", "callback_data": "kill"},
            ],
            [
                {"text": "🔄 تحديث", "callback_data": "refresh"},
            ],
        ]
    }

def amount_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "5", "callback_data": "amt_5"},
                {"text": "10", "callback_data": "amt_10"},
                {"text": "15", "callback_data": "amt_15"},
                {"text": "20", "callback_data": "amt_20"},
            ],
            [
                {"text": "25", "callback_data": "amt_25"},
                {"text": "50", "callback_data": "amt_50"},
                {"text": "100", "callback_data": "amt_100"},
            ],
            [{"text": "🔙 رجوع", "callback_data": "menu"}],
        ]
    }

def maxpos_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "1", "callback_data": "maxpos_1"},
                {"text": "2", "callback_data": "maxpos_2"},
                {"text": "3", "callback_data": "maxpos_3"},
                {"text": "5", "callback_data": "maxpos_5"},
            ],
            [
                {"text": "7", "callback_data": "maxpos_7"},
                {"text": "10", "callback_data": "maxpos_10"},
            ],
            [{"text": "🔙 رجوع", "callback_data": "menu"}],
        ]
    }

def tp_keyboard(which: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "3%", "callback_data": f"tp_{which}_3"},
                {"text": "5%", "callback_data": f"tp_{which}_5"},
                {"text": "7%", "callback_data": f"tp_{which}_7"},
                {"text": "10%", "callback_data": f"tp_{which}_10"},
            ],
            [
                {"text": "12%", "callback_data": f"tp_{which}_12"},
                {"text": "15%", "callback_data": f"tp_{which}_15"},
                {"text": "20%", "callback_data": f"tp_{which}_20"},
            ],
            [{"text": "🔙 رجوع", "callback_data": "menu"}],
        ]
    }

def channels_keyboard() -> dict:
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT username, enabled, COALESCE(template,'auto') as template FROM channels ORDER BY id")
            rows = cur.fetchall()
        else:
            try:
                rows = conn.execute("SELECT username, enabled, COALESCE(template,'auto') as template FROM channels ORDER BY id").fetchall()
            except Exception:
                rows = conn.execute("SELECT username, enabled FROM channels ORDER BY id").fetchall()
                rows = [dict(r) | {"template": "auto"} for r in rows]
    finally:
        conn.close()
    buttons = []
    for r in rows:
        status = "✅" if r["enabled"] else "❌"
        tmpl = row_value(r, "template", "auto") or "auto"
        buttons.append([{"text": f"{status} @{r['username']} [{tmpl}]", "callback_data": f"ch_toggle_{r['username']}"}])
        buttons.append([
            {"text": f"📋 نموذج @{r['username']}", "callback_data": f"ch_tmpl_{r['username']}"},
            {"text": f"🗑 حذف", "callback_data": f"ch_del_{r['username']}"},
        ])
    buttons.append([{"text": "➕ إضافة قناة", "callback_data": "ch_add"}])
    buttons.append([{"text": "🔙 رجوع", "callback_data": "menu"}])
    return {"inline_keyboard": buttons}

# ==================== CHANNEL SCRAPER ====================
def scrape_channel(username: str) -> List[Dict]:
    url = f"https://t.me/s/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        messages = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            msg = wrap.select_one(".tgme_widget_message")
            if not msg:
                continue
            data_post = msg.get("data-post", "")
            msg_id = 0
            if "/" in data_post:
                try:
                    msg_id = int(data_post.split("/")[-1])
                except:
                    pass
            if msg_id == 0:
                link = msg.select_one("a.tgme_widget_message_date")
                if link and link.get("href"):
                    m = re.search(r"/(\d+)$", link["href"])
                    if m:
                        msg_id = int(m.group(1))
            text_el = msg.select_one(".tgme_widget_message_text")
            text = text_el.get_text("\n", strip=True) if text_el else ""
            if msg_id > 0 and text:
                messages.append({"id": msg_id, "text": text, "channel": username})
        messages.sort(key=lambda x: x["id"])
        return messages
    except Exception as e:
        logger.error(f"Scrape @{username}: {e}")
        return []

# ==================== FLEXIBLE PARSER (Arabic + English) ====================
def normalize_symbol(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.upper().strip()
    s = re.sub(r"[#$“”\"']", "", s)
    # Strip quote assets only as a suffix/pair, never from the middle of a base ticker.
    s = re.sub(r"(?:/|-)?USDT$", "", s)
    s = re.sub(r"(?:/|-)USD$", "", s)
    s = s.replace("/", "").replace("-", "").replace(".", "")
    s = re.sub(r"[^A-Z0-9]", "", s)
    if len(s) < 2 or len(s) > 15 or not re.search(r"[A-Z]", s):
        return None
    # Reject common signal vocabulary so words like BUY/ENTRY do not become fake tickers.
    if s in {
        "SPOT", "LONG", "SHORT", "SIGN", "SIGNAL", "SWING", "ENTER", "ENTRY",
        "TARGET", "TARGETS", "TP", "TP1", "TP2", "TP3", "STOP", "STOPLOSS",
        "SL", "LEVERAGE", "BUY", "SELL", "COIN", "TOKEN", "SYMBOL", "PRICE",
        "USDT", "USD", "MEXC", "FUTURES", "TRADE", "TRADING", "OPEN", "CLOSE",
        "NOW", "CURRENT", "BUYING", "SELLING", "RISK", "REWARD", "PROFIT",
        "LOSS", "ANALYSIS", "SETUP", "UPDATE", "EXIT", "HOLD", "BREAKOUT",
        "PUMP", "MOON", "ENTRYPRICE", "TAKEPROFIT", "STOPPRICE", "MARKET",
        "POSITION", "RECOMMENDATION", "RECOMMENDED", "ALERT", "PREMIUM",
    }:
        return None
    return f"{s}/USDT"

def parse_signal(text: str, template: str = "auto") -> Optional[Dict]:
    """
    Flexible parser supporting multiple channel formats.
    template: "auto" | "arabic_spot" | "english_long"
    """
    if not text or len(text) < 8:
        return None
    original = text
    text_lower = text.lower()

    # Direction: explicit LONG/BUY wins, otherwise reject explicit SHORT/SELL.
    # "SPOT" alone describes the market type and must not override a SHORT signal.
    direction = None
    has_long = bool(re.search(r"\b(long|buy|لونج|شراء)\b", text_lower) or "🟢" in text)
    has_short = bool(re.search(r"\b(short|sell|شورت|بيع)\b", text_lower) or "🔴" in text)
    if has_long:
        direction = "LONG"
    elif has_short:
        direction = "SHORT"
    elif re.search(r"\b(spot|سبوت)\b", text_lower):
        direction = "LONG"
    elif re.search(r"(دخول|enter|entry|هدف|target|وقف|stop)", text_lower):
        direction = "LONG"
    else:
        return None

    # Leverage (ignored)
    lev_match = re.search(r"(?:leverage|lev|x|×)\s*[:=]?\s*(\d+)", text_lower)
    leverage = int(lev_match.group(1)) if lev_match else None

    def find_prices(patterns):
        results = []
        for p in patterns:
            for m in re.finditer(p, text, re.IGNORECASE | re.DOTALL):
                try:
                    raw = m.group(1).replace(",", "").replace("..", "").strip()
                    raw = re.sub(r"\.+$", "", raw)
                    if "-" in raw and re.match(r"^[\d.]+-[\d.]+$", raw):
                        parts = raw.split("-")
                        val = float(parts[0])
                    else:
                        val = float(raw)
                    if val > 0:
                        results.append(val)
                except Exception:
                    pass
        return results

    entries = find_prices([
        r"(?:الدخول من السعر الحالي|دخول|enter|entry|سعر الدخول)\s*[:=]?\s*([\d.\-]+)",
        r"(?:enter|entry)\s*[:=]?\s*([\d.\-]+)",
    ])
    entry = entries[0] if entries else None

    targets = find_prices([
        r"(?:الهدف الثاني|هدف ثاني|target\s*2|tp\s*2)\s*[:=]\s*([\d.]+)",
        r"(?:الهدف الثالث|هدف ثالث|target\s*3|tp\s*3)\s*[:=]\s*([\d.]+)",
        r"(?:الهدف الاول|الهدف الأول|هدف اول|target\s*1|tp\s*1)\s*[:=]\s*([\d.]+)",
        r"(?:target|tp|تارجيت|الهدف)\s*[:=]\s*([\d.]+)",
        r"(?:target|tp)(?!\s*[123]\b)\s+([\d.]+)",
        r"(?:الهدف الاول|الهدف الأول|هدف اول|الهدف)\s+([\d.]+)",
    ])
    targets = sorted(set(targets))
    target = targets[0] if targets else None

    stops = find_prices([
        r"(?:الوقف|وقف|stop|sl|ستوب)\s*(?:اغلاق يوم|إغلاق يوم|اسفل|أسفل)?\s*[:=]?\s*([\d.]+)",
        r"(?:stop|sl)\s*[:=]?\s*([\d.]+)",
        r"الوقف\s*[:=]?\s*([\d.]+)",
    ])
    stop = stops[0] if stops else None

    symbol = None
    # Prefer unambiguous ticker forms before scanning ordinary words.
    symbol_patterns = [
        r"(?:#|\$)([A-Za-z0-9]{2,15})\b",
        r"\b([A-Za-z0-9]{2,15})\s*/\s*USDT\b",
        r"\b([A-Za-z0-9]{2,15})\s*USDT\b",
        r"(?:symbol|coin|token|العملة|الرمز)\s*[:=\-]?\s*([A-Za-z0-9]{2,15})\b",
    ]
    for pattern in symbol_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = normalize_symbol(match.group(1))
            if candidate:
                symbol = candidate
                break

    if not symbol:
        lines_to_scan = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines_to_scan[:8]:
            clean = re.sub(r"[^\w\s/]", " ", line)
            for word in clean.split():
                candidate = normalize_symbol(word)
                if candidate:
                    symbol = candidate
                    break
            if symbol:
                break

    if not symbol:
        return None

    if not any([entry, target, stop]) and direction != "LONG":
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "leverage": leverage,
        "entry": entry,
        "target": target,
        "targets": targets,
        "stop": stop,
        "raw": original[:700],
        "template": template,
    }


def signal_key(channel: str, msg_id: int, sig: Dict) -> str:
    s = f"{channel}|{msg_id}|{sig['symbol']}|{sig['direction']}|{sig.get('entry')}|{sig.get('stop')}"
    return hashlib.sha256(s.encode()).hexdigest()[:20]

# ==================== MEXC SPOT ====================
exchange: Optional[ccxt.Exchange] = None

def init_exchange() -> bool:
    """Initialize public market data even without keys; private actions still require credentials."""
    global exchange
    try:
        options = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        has_credentials = bool(MEXC_API_KEY and MEXC_SECRET_KEY)
        if has_credentials:
            options.update({"apiKey": MEXC_API_KEY, "secret": MEXC_SECRET_KEY})
        exchange = ccxt.mexc(options)
        exchange.load_markets()
        if has_credentials:
            exchange.fetch_balance()
            logger.info("MEXC Spot connected (authenticated)")
        else:
            logger.warning("MEXC public market data connected; API keys missing, live trading disabled")
        return True
    except Exception as e:
        logger.error(f"MEXC init failed: {e}")
        exchange = None
        return False

def get_usdt_balance() -> float:
    if not exchange:
        return 0.0
    try:
        bal = exchange.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0) or 0)
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0

def market_exists(symbol: str) -> bool:
    if not exchange:
        return False
    m = exchange.markets.get(symbol)
    return bool(m and m.get("spot", False))

def calculate_amount(symbol: str, usdt_amount: float) -> Optional[float]:
    if not exchange or not market_exists(symbol):
        return None
    try:
        market = exchange.markets[symbol]
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("close") or 0)
        if price <= 0:
            return None
        amount = usdt_amount / price
        amount = float(exchange.amount_to_precision(symbol, amount))
        min_amount = (market.get("limits") or {}).get("amount", {}).get("min") or 0
        min_cost = (market.get("limits") or {}).get("cost", {}).get("min") or 0
        if amount < min_amount or (amount * price) < min_cost:
            return None
        return amount
    except Exception as e:
        logger.error(f"calc amount: {e}")
        return None

def execute_spot_buy(symbol: str, usdt_amount: float, paper: bool) -> Optional[Dict]:
    if paper:
        if not exchange or not market_exists(symbol):
            logger.warning("Paper buy skipped: no public market data for %s", symbol)
            return None
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception as e:
            logger.warning("Paper ticker failed for %s: %s", symbol, e)
            return None
        if price <= 0:
            logger.warning("Paper buy skipped: invalid price for %s", symbol)
            return None
        qty = usdt_amount / price
        if qty <= 0:
            return None
        return {
            "id": f"PAPER-{int(time.time())}",
            "symbol": symbol,
            "side": "buy",
            "amount": qty,
            "price": price,
            "average": price,
            "cost": usdt_amount,
            "status": "closed",
            "paper": True,
        }
    if not exchange or not market_exists(symbol):
        return None
    amount = calculate_amount(symbol, usdt_amount)
    if not amount:
        return None
    if get_usdt_balance() < usdt_amount * 1.01:
        return None
    try:
        order = exchange.create_order(symbol, "market", "buy", amount)
        return order
    except Exception as e:
        logger.error(f"Buy failed: {e}")
        tg_send(f"❌ خطأ في الشراء\n{symbol}\n{str(e)[:250]}")
        return None

def execute_spot_sell(symbol: str, amount: float, reason: str, paper: bool) -> Optional[Dict]:
    if paper:
        return {
            "id": f"PAPER-SELL-{int(time.time())}",
            "symbol": symbol,
            "side": "sell",
            "amount": amount,
            "status": "closed",
            "paper": True,
            "reason": reason,
        }
    if not exchange:
        return None
    try:
        amount = float(exchange.amount_to_precision(symbol, amount))
        if amount <= 0:
            return None
        order = exchange.create_order(symbol, "market", "sell", amount)
        return order
    except Exception as e:
        logger.error(f"Sell failed: {e}")
        tg_send(f"❌ خطأ في البيع\n{symbol}\n{str(e)[:250]}")
        return None

# ==================== POSITIONS & TRAILING ====================
def count_open_positions() -> int:
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT COUNT(*) as c FROM positions WHERE status='open'")
            n = cur.fetchone()["c"]
        else:
            n = conn.execute("SELECT COUNT(*) as c FROM positions WHERE status='open'").fetchone()["c"]
        return n
    finally:
        conn.close()

def open_position(sig: Dict, order: Dict, settings: Dict) -> bool:
    symbol = sig["symbol"]
    # Prefer actual filled quantity and average execution price over requested values.
    qty = float(order.get("filled") or order.get("amount") or 0)
    price = float(order.get("average") or order.get("price") or sig.get("entry") or 0)
    if price <= 0 and exchange:
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception:
            logger.exception("Unable to recover entry price for %s", symbol)
    if qty <= 0 or price <= 0:
        logger.error("Refusing to save invalid position %s: qty=%s price=%s", symbol, qty, price)
        tg_send(f"🚨 تحذير: أمر {symbol} قد يكون نُفّذ لكن بيانات الكمية/السعر غير صالحة. راجع حساب MEXC يدويًا فورًا.")
        return False

    tp_pcts = sorted([
        max(0.1, float(settings["tp1_pct"])),
        max(0.2, float(settings["tp2_pct"])),
        max(0.3, float(settings["tp3_pct"])),
    ])
    tp1 = price * (1 + tp_pcts[0] / 100)
    tp2 = price * (1 + tp_pcts[1] / 100)
    tp3 = price * (1 + tp_pcts[2] / 100)

    # Prefer targets from the signal itself if present
    sig_targets = sig.get("targets") or []
    if len(sig_targets) >= 1 and sig_targets[0] > price:
        tp1 = float(sig_targets[0])
    if len(sig_targets) >= 2 and sig_targets[1] > price:
        tp2 = float(sig_targets[1])
    if len(sig_targets) >= 3 and sig_targets[2] > price:
        tp3 = float(sig_targets[2])
    elif sig.get("target") and sig["target"] > price:
        tp1 = float(sig["target"])

    # Keep long targets strictly increasing even if a signal provides only one target
    # or Telegram settings were configured out of order.
    tp2 = max(tp2, tp1 * 1.001)
    tp3 = max(tp3, tp2 * 1.001)

    requested_sl = float(sig["stop"]) if sig.get("stop") else 0.0
    sl = requested_sl if 0 < requested_sl < price else price * 0.95

    conn = get_db()
    try:
        params = (
            symbol, qty, qty, price, sl, sl,
            tp1, tp2, tp3,
            str(order.get("id")),
            sig.get("raw", "")[:500],
            datetime.now(timezone.utc).isoformat(),
            1 if settings["paper_mode"] else 0,
        )
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO positions (
                    symbol, qty, remaining_qty, entry_price, stop_loss, current_sl,
                    tp1, tp2, tp3, order_id, signal_raw, opened_at, paper, status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
            """, params)
        else:
            conn.execute("""
                INSERT INTO positions (
                    symbol, qty, remaining_qty, entry_price, stop_loss, current_sl,
                    tp1, tp2, tp3, order_id, signal_raw, opened_at, paper, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open')
            """, params)
        conn.commit()
        return True
    finally:
        conn.close()

def check_and_manage_positions():
    settings = get_settings()
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM positions WHERE status='open'")
            rows = cur.fetchall()
        else:
            rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            symbol = row["symbol"]
            remaining = float(row["remaining_qty"])
            if remaining <= 0:
                continue

            # Use each position's stored mode, not the current global Paper toggle.
            # Paper trades still use public live prices to simulate TP/SL.
            paper = bool(row["paper"])
            if not exchange:
                continue
            ticker = exchange.fetch_ticker(symbol)
            current = float(ticker.get("last") or ticker.get("close") or ticker.get("bid") or 0)
            if current <= 0:
                logger.warning("Skipping position %s: invalid ticker price", symbol)
                continue

            entry = float(row["entry_price"])
            tp1, tp2, tp3 = float(row["tp1"]), float(row["tp2"]), float(row["tp3"])
            current_sl = float(row["current_sl"])

            # Stop Loss hit
            if current <= current_sl:
                order = execute_spot_sell(symbol, remaining, "STOP", paper)
                if order:
                    exit_price = float(order.get("average") or order.get("price") or current)
                    close_position(row["id"], remaining, exit_price, "STOP", paper)
                    tg_send(f"🛑 STOP HIT\n{symbol}\nPrice: {exit_price}\nSL: {current_sl}")
                continue

            # TP3 full close
            if not row["tp3_hit"] and current >= tp3:
                order = execute_spot_sell(symbol, remaining, "TP3", paper)
                if order:
                    exit_price = float(order.get("average") or order.get("price") or current)
                    close_position(row["id"], remaining, exit_price, "TP3", paper)
                    tg_send(f"🎯🎯🎯 TP3 HIT (Full)\n{symbol}\nPrice: {exit_price}")
                continue

            # TP2 partial (close half of remaining)
            if not row["tp2_hit"] and current >= tp2:
                sell_qty = remaining * 0.5
                order = execute_spot_sell(symbol, sell_qty, "TP2", paper)
                if order:
                    sold_qty = min(remaining, float(order.get("filled") or order.get("amount") or sell_qty))
                    sell_price = float(order.get("average") or order.get("price") or current)
                    new_remaining = max(0.0, remaining - sold_qty)
                    new_sl = tp1
                    update_position_after_tp(
                        row["id"], new_remaining, new_sl, tp2_hit=True,
                        sold_qty=sold_qty, sell_price=sell_price, entry_price=entry,
                        reason="TP2", paper=paper,
                    )
                    tg_send(f"🎯🎯 TP2 HIT\n{symbol}\nClosed: {sold_qty:.6f}\nNew SL → {new_sl}")
                continue

            # TP1 partial (close 1/3 of original or remaining)
            if not row["tp1_hit"] and current >= tp1:
                sell_qty = remaining * 0.34
                order = execute_spot_sell(symbol, sell_qty, "TP1", paper)
                if order:
                    sold_qty = min(remaining, float(order.get("filled") or order.get("amount") or sell_qty))
                    sell_price = float(order.get("average") or order.get("price") or current)
                    new_remaining = max(0.0, remaining - sold_qty)
                    new_sl = entry
                    update_position_after_tp(
                        row["id"], new_remaining, new_sl, tp1_hit=True,
                        sold_qty=sold_qty, sell_price=sell_price, entry_price=entry,
                        reason="TP1", paper=paper,
                    )
                    tg_send(f"🎯 TP1 HIT\n{symbol}\nClosed: {sold_qty:.6f}\nSL moved to Entry: {entry}")
                continue

        except Exception as e:
            logger.error(f"Position manage {row['symbol']}: {e}")

def update_position_after_tp(
    pos_id: int, new_qty: float, new_sl: float, tp1_hit=False, tp2_hit=False,
    sold_qty: float = 0.0, sell_price: float = 0.0, entry_price: float = 0.0,
    reason: str = "", paper: bool = True,
):
    """Atomically update a partial TP and record its realized PnL."""
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT symbol FROM positions WHERE id=%s", (pos_id,))
            row = cur.fetchone()
            symbol = row[0] if row else None
            if tp1_hit:
                cur.execute(
                    "UPDATE positions SET remaining_qty=%s, current_sl=%s, tp1_hit=1 WHERE id=%s",
                    (new_qty, new_sl, pos_id)
                )
            if tp2_hit:
                cur.execute(
                    "UPDATE positions SET remaining_qty=%s, current_sl=%s, tp2_hit=1 WHERE id=%s",
                    (new_qty, new_sl, pos_id)
                )
            if symbol and sold_qty > 0 and sell_price > 0 and entry_price > 0 and reason:
                pnl = (sell_price - entry_price) * sold_qty
                cur.execute(
                    """INSERT INTO trades_history (symbol, side, qty, price, pnl, reason, paper, closed_at)
                       VALUES (%s, 'sell', %s, %s, %s, %s, %s, %s)""",
                    (symbol, sold_qty, sell_price, pnl, reason, 1 if paper else 0,
                     datetime.now(timezone.utc).isoformat()),
                )
        else:
            row = conn.execute("SELECT symbol FROM positions WHERE id=?", (pos_id,)).fetchone()
            symbol = row["symbol"] if row else None
            if tp1_hit:
                conn.execute(
                    "UPDATE positions SET remaining_qty=?, current_sl=?, tp1_hit=1 WHERE id=?",
                    (new_qty, new_sl, pos_id)
                )
            if tp2_hit:
                conn.execute(
                    "UPDATE positions SET remaining_qty=?, current_sl=?, tp2_hit=1 WHERE id=?",
                    (new_qty, new_sl, pos_id)
                )
            if symbol and sold_qty > 0 and sell_price > 0 and entry_price > 0 and reason:
                pnl = (sell_price - entry_price) * sold_qty
                conn.execute(
                    """INSERT INTO trades_history (symbol, side, qty, price, pnl, reason, paper, closed_at)
                       VALUES (?, 'sell', ?, ?, ?, ?, ?, ?)""",
                    (symbol, sold_qty, sell_price, pnl, reason, 1 if paper else 0,
                     datetime.now(timezone.utc).isoformat()),
                )
        conn.commit()
    finally:
        conn.close()

def close_position(pos_id: int, qty: float, price: float, reason: str, paper: bool):
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM positions WHERE id=%s", (pos_id,))
            row = cur.fetchone()
            if row:
                entry = float(row["entry_price"])
                pnl = (price - entry) * qty
                cur.execute("UPDATE positions SET remaining_qty=0, status='closed' WHERE id=%s", (pos_id,))
                cur.execute("""
                    INSERT INTO trades_history (symbol, side, qty, price, pnl, reason, paper, closed_at)
                    VALUES (%s, 'sell', %s, %s, %s, %s, %s, %s)
                """, (row["symbol"], qty, price, pnl, reason, 1 if paper else 0, datetime.now(timezone.utc).isoformat()))
        else:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            if row:
                entry = float(row["entry_price"])
                pnl = (price - entry) * qty
                conn.execute("UPDATE positions SET remaining_qty=0, status='closed' WHERE id=?", (pos_id,))
                conn.execute("""
                    INSERT INTO trades_history (symbol, side, qty, price, pnl, reason, paper, closed_at)
                    VALUES (?, 'sell', ?, ?, ?, ?, ?, ?)
                """, (row["symbol"], qty, price, pnl, reason, 1 if paper else 0, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()

# ==================== PROCESS SIGNAL ====================
def already_processed(key: str) -> bool:
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM processed WHERE msg_key=%s", (key,))
            row = cur.fetchone()
        else:
            row = conn.execute("SELECT 1 FROM processed WHERE msg_key=?", (key,)).fetchone()
        return bool(row)
    finally:
        conn.close()

def mark_processed(key: str):
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO processed (msg_key, processed_at) VALUES (%s, %s) ON CONFLICT (msg_key) DO NOTHING",
                (key, datetime.now(timezone.utc).isoformat())
            )
            # keep table small
            cur.execute("""
                DELETE FROM processed WHERE msg_key NOT IN (
                    SELECT msg_key FROM processed ORDER BY processed_at DESC LIMIT 500
                )
            """)
        else:
            conn.execute(
                "INSERT OR IGNORE INTO processed (msg_key, processed_at) VALUES (?, ?)",
                (key, datetime.now(timezone.utc).isoformat())
            )
            conn.execute("DELETE FROM processed WHERE rowid NOT IN (SELECT rowid FROM processed ORDER BY processed_at DESC LIMIT 500)")
        conn.commit()
    finally:
        conn.close()

def process_signal(msg: Dict):
    settings = get_settings()
    channel = msg["channel"]
    msg_id = msg["id"]
    text = msg["text"]

    # Get channel template if any
    template = "auto"
    conn = None
    try:
        conn = get_db()
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT template FROM channels WHERE username=%s", (channel,))
            row = cur.fetchone()
        else:
            try:
                row = conn.execute("SELECT template FROM channels WHERE username=?", (channel,)).fetchone()
            except Exception:
                row = None
        template = row_value(row, "template", "auto") or "auto"
    except Exception:
        logger.debug("Unable to read channel template for @%s", channel, exc_info=True)
    finally:
        if conn is not None:
            conn.close()

    sig = parse_signal(text, template=template)
    if not sig:
        return

    # If channel has a custom sample template, require basic similarity
    conn = None
    try:
        conn = get_db()
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT sample_text FROM channels WHERE username=%s", (channel,))
            row = cur.fetchone()
        else:
            try:
                row = conn.execute("SELECT sample_text FROM channels WHERE username=?", (channel,)).fetchone()
            except Exception:
                row = None
        sample = row_value(row, "sample_text", "") or ""
        if sample and len(sample) > 20:
            # simple keyword overlap check
            sample_words = set(re.findall(r"[\w\u0600-\u06FF]{3,}", sample.lower()))
            msg_words = set(re.findall(r"[\w\u0600-\u06FF]{3,}", text.lower()))
            # keep only meaningful trading words
            important = {"long", "short", "enter", "entry", "target", "stop", "spot", "سبوت", "دخول", "هدف", "وقف", "لونج"}
            sample_imp = sample_words & important
            msg_imp = msg_words & important
            if sample_imp and not (sample_imp & msg_imp):
                # also allow if prices exist in both
                if not re.search(r"\d+\.\d+", text):
                    logger.info(f"Skip @{channel} msg - low similarity to template")
                    return
    except Exception as e:
        logger.debug(f"similarity check: {e}")
    finally:
        if conn is not None:
            conn.close()

    key = signal_key(channel, msg_id, sig)
    if already_processed(key):
        logger.info(f"Skip duplicate key {key}")
        return
    mark_processed(key)

    # Extra protection: do not open another position on same symbol if already open
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT id FROM positions WHERE symbol=%s AND status='open'", (sig["symbol"],))
            existing = cur.fetchone()
        else:
            existing = conn.execute("SELECT id FROM positions WHERE symbol=? AND status='open'", (sig["symbol"],)).fetchone()
        if existing:
            logger.info(f"Skip {sig['symbol']} - already have open position")
            tg_send(f"ℹ️ تم تجاهل {sig['symbol']} لأنها مفتوحة بالفعل")
            return
    finally:
        conn.close()


    # Update last_msg_id for channel
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                "UPDATE channels SET last_msg_id = GREATEST(last_msg_id, %s) WHERE username=%s",
                (msg_id, channel)
            )
        else:
            conn.execute(
                "UPDATE channels SET last_msg_id = MAX(last_msg_id, ?) WHERE username=?",
                (msg_id, channel)
            )
        conn.commit()
    finally:
        conn.close()

    if sig["direction"] != "LONG":
        tg_send(f"ℹ️ إشارة SHORT تم تجاهلها\n{sig['symbol']}\nمن @{channel}")
        return

    # Notify
    lev = f"{sig['leverage']}x (مهمل)" if sig.get("leverage") else "لا يوجد"
    notify = (
        f"🚨 توصية جديدة من @{channel}\n\n"
        f"العملية: <b>{sig['symbol']}</b>\n"
        f"النوع: LONG → SPOT BUY\n"
        f"الدخول: {sig.get('entry') or 'السوق'}\n"
        f"الهدف من الإشارة: {sig.get('target') or '-'}\n"
        f"الوقف من الإشارة: {sig.get('stop') or '-'}\n"
        f"الرافعة: {lev}\n"
        f"Paper: {settings['paper_mode']}\n"
        f"التداول: {settings['trading_enabled']}"
    )
    tg_send(notify)

    if not settings["trading_enabled"]:
        return

    if count_open_positions() >= settings["max_positions"]:
        tg_send(f"⚠️ تم الوصول لحد الصفقات المفتوحة ({settings['max_positions']})")
        return

    amount = min(settings["trade_amount"], MAX_TRADE_HARD_LIMIT)
    if amount <= 0:
        return

    if not settings["paper_mode"]:
        if not MEXC_API_KEY or not MEXC_SECRET_KEY:
            tg_send("❌ التداول الحقيقي متوقف: أضف MEXC_API_KEY وMEXC_SECRET_KEY أولًا")
            return
        if not exchange:
            tg_send("❌ MEXC غير متصل")
            return
        if not market_exists(sig["symbol"]):
            tg_send(f"❌ {sig['symbol']} غير موجود في MEXC Spot")
            return
        if get_usdt_balance() < amount * 1.02:
            tg_send("❌ رصيد USDT غير كافٍ")
            return

    order = execute_spot_buy(sig["symbol"], amount, settings["paper_mode"])
    if order:
        if not open_position(sig, order, settings):
            tg_send(f"🚨 تم استلام رد شراء لـ {sig['symbol']} لكن لم تُسجّل الصفقة بأمان. راجع أوامر MEXC قبل إعادة التشغيل.")
            return
        price = order.get("average") or order.get("price") or sig.get("entry")
        qty = order.get("filled") or order.get("amount")
        tg_send(
            f"✅ تم تنفيذ SPOT BUY\n\n"
            f"<b>{sig['symbol']}</b>\n"
            f"المبلغ: {amount} USDT\n"
            f"الكمية: {qty}\n"
            f"السعر: {price}\n"
            f"Order: {order.get('id')}\n"
            f"{'(PAPER)' if settings['paper_mode'] else ''}"
        )
    else:
        tg_send(f"❌ فشل تنفيذ الشراء لـ {sig['symbol']}")

# ==================== BOT HANDLERS ====================
waiting_for: Dict[str, str] = {}  # admin_id -> "add_channel" etc.

def handle_callback(cq: dict, _answer: bool = True):
    data = cq.get("data", "")
    cq_id = cq["id"]
    msg = cq.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")
    if chat_id != TELEGRAM_ADMIN_ID:
        tg_answer_callback(cq_id, "غير مصرح")
        return

    if _answer:
        tg_answer_callback(cq_id)

    if data == "menu" or data == "refresh":
        s = get_settings()
        text = (
            f"🤖 <b>MEXC SPOT Trader</b>\n\n"
            f"التداول: {'🟢 ON' if s['trading_enabled'] else '🔴 OFF'}\n"
            f"Paper: {'📝 ON' if s['paper_mode'] else '💰 OFF'}\n"
            f"المبلغ: {s['trade_amount']} USDT\n"
            f"حد الصفقات: {s['max_positions']}\n"
            f"TPs: {s['tp1_pct']}% / {s['tp2_pct']}% / {s['tp3_pct']}%\n"
            f"صفقات مفتوحة: {count_open_positions()}"
        )
        tg_edit(chat_id, message_id, text, main_menu_keyboard())

    elif data == "toggle_trading":
        cur = get_settings()["trading_enabled"]
        db_set("trading_enabled", "0" if cur else "1")
        handle_callback({"data": "menu", "id": cq_id, "message": msg}, _answer=False)

    elif data == "toggle_paper":
        cur = get_settings()["paper_mode"]
        db_set("paper_mode", "0" if cur else "1")
        handle_callback({"data": "menu", "id": cq_id, "message": msg}, _answer=False)

    elif data == "set_amount":
        tg_edit(chat_id, message_id, "اختر مبلغ الصفقة (USDT):", amount_keyboard())

    elif data.startswith("amt_"):
        val = float(data.split("_")[1])
        db_set("trade_amount", str(val))
        tg_send(f"✅ تم تعيين المبلغ إلى {val} USDT")
        handle_callback({"data": "menu", "id": cq_id, "message": msg}, _answer=False)

    elif data == "set_maxpos":
        tg_edit(chat_id, message_id, "اختر الحد الأقصى لعدد الصفقات المفتوحة:", maxpos_keyboard())

    elif data.startswith("maxpos_"):
        val = int(data.split("_")[1])
        db_set("max_positions", str(val))
        tg_send(f"✅ حد الصفقات = {val}")
        handle_callback({"data": "menu", "id": cq_id, "message": msg}, _answer=False)

    elif data.startswith("set_tp"):
        which = data[-1]  # 1/2/3
        tg_edit(chat_id, message_id, f"اختر نسبة TP{which}:", tp_keyboard(which))

    elif data.startswith("tp_"):
        parts = data.split("_")
        which, pct = parts[1], parts[2]
        db_set(f"tp{which}_pct", pct)
        tg_send(f"✅ TP{which} = {pct}%")
        handle_callback({"data": "menu", "id": cq_id, "message": msg}, _answer=False)

    elif data == "positions":
        conn = get_db()
        try:
            if USE_POSTGRES:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT * FROM positions WHERE status='open'")
                rows = cur.fetchall()
            else:
                rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        finally:
            conn.close()
        if not rows:
            text = "لا توجد صفقات مفتوحة"
        else:
            text = "📈 <b>الصفقات المفتوحة</b>\n\n"
            for r in rows:
                text += (
                    f"<b>{r['symbol']}</b>\n"
                    f"الكمية المتبقية: {r['remaining_qty']:.6f}\n"
                    f"الدخول: {r['entry_price']}\n"
                    f"الوقف الحالي: {r['current_sl']}\n"
                    f"TP1/2/3: {r['tp1']:.4f} / {r['tp2']:.4f} / {r['tp3']:.4f}\n"
                    f"TP hits: {r['tp1_hit']}/{r['tp2_hit']}/{r['tp3_hit']}\n\n"
                )
        tg_edit(chat_id, message_id, text, {"inline_keyboard": [[{"text": "🔙 رجوع", "callback_data": "menu"}]]})

    elif data == "balance":
        if not exchange:
            text = "MEXC غير متصل"
        else:
            bal = get_usdt_balance()
            text = f"💰 USDT المتاح: <b>{bal:.2f}</b>"
        tg_edit(chat_id, message_id, text, {"inline_keyboard": [[{"text": "🔙 رجوع", "callback_data": "menu"}]]})

    elif data == "channels":
        tg_edit(chat_id, message_id, "📡 إدارة القنوات:", channels_keyboard())

    elif data.startswith("ch_toggle_"):
        user = data.replace("ch_toggle_", "")
        conn = get_db()
        try:
            if USE_POSTGRES:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT enabled FROM channels WHERE username=%s", (user,))
                row = cur.fetchone()
                if row:
                    newv = 0 if row["enabled"] else 1
                    cur.execute("UPDATE channels SET enabled=%s WHERE username=%s", (newv, user))
            else:
                row = conn.execute("SELECT enabled FROM channels WHERE username=?", (user,)).fetchone()
                if row:
                    newv = 0 if row["enabled"] else 1
                    conn.execute("UPDATE channels SET enabled=? WHERE username=?", (newv, user))
            conn.commit()
        finally:
            conn.close()
        handle_callback({"data": "channels", "id": cq_id, "message": msg}, _answer=False)

    elif data.startswith("ch_del_"):
        user = data.replace("ch_del_", "")
        conn = get_db()
        try:
            if USE_POSTGRES:
                cur = conn.cursor()
                cur.execute("DELETE FROM channels WHERE username=%s", (user,))
            else:
                conn.execute("DELETE FROM channels WHERE username=?", (user,))
            conn.commit()
        finally:
            conn.close()
        tg_send(f"تم حذف @{user}")
        handle_callback({"data": "channels", "id": cq_id, "message": msg}, _answer=False)

    elif data.startswith("ch_tmpl_"):
        user = data.replace("ch_tmpl_", "")
        kb = {
            "inline_keyboard": [
                [{"text": "🔄 تلقائي (auto)", "callback_data": f"settmpl_{user}_auto"}],
                [{"text": "🇸🇦 عربي سبوت", "callback_data": f"settmpl_{user}_arabic_spot"}],
                [{"text": "🇬🇧 إنجليزي Long", "callback_data": f"settmpl_{user}_english_long"}],
                [{"text": "🔙 رجوع", "callback_data": "channels"}],
            ]
        }
        tg_edit(chat_id, message_id, f"اختر نموذج الاقتناص لـ @{user}:", kb)

    elif data.startswith("settmpl_"):
        parts = data.split("_", 2)
        # settmpl_username_template  → but username may contain _
        # safer: settmpl_{user}_{tmpl}
        rest = data[len("settmpl_"):]
        # last part is template
        if rest.endswith("_auto"):
            user, tmpl = rest[:-5], "auto"
        elif rest.endswith("_arabic_spot"):
            user, tmpl = rest[:-12], "arabic_spot"
        elif rest.endswith("_english_long"):
            user, tmpl = rest[:-14], "english_long"
        else:
            user, tmpl = rest, "auto"
        conn = get_db()
        try:
            if USE_POSTGRES:
                cur = conn.cursor()
                cur.execute("UPDATE channels SET template=%s WHERE username=%s", (tmpl, user))
            else:
                try:
                    conn.execute("UPDATE channels SET template=? WHERE username=?", (tmpl, user))
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
        tg_send(f"✅ تم تعيين نموذج @{user} → {tmpl}")
        handle_callback({"data": "channels", "id": cq_id, "message": msg}, _answer=False)

    elif data == "ch_add":
        waiting_for[TELEGRAM_ADMIN_ID] = "add_channel"
        tg_send("أرسل الآن يوزرنيم القناة (مثال: abojasimp أو @abojasimp)\nبدون رابط.")
        tg_edit(chat_id, message_id, "في انتظار اسم القناة...", {"inline_keyboard": [[{"text": "🔙 إلغاء", "callback_data": "menu"}]]})

    elif data == "settings":
        s = get_settings()
        text = (
            f"⚙️ الإعدادات الحالية\n\n"
            f"المبلغ: {s['trade_amount']} USDT\n"
            f"حد الصفقات: {s['max_positions']}\n"
            f"TP1: {s['tp1_pct']}%\n"
            f"TP2: {s['tp2_pct']}%\n"
            f"TP3: {s['tp3_pct']}%\n"
            f"Paper: {s['paper_mode']}\n"
            f"Trading: {s['trading_enabled']}"
        )
        tg_edit(chat_id, message_id, text, {"inline_keyboard": [[{"text": "🔙 رجوع", "callback_data": "menu"}]]})

    elif data == "kill":
        db_set("trading_enabled", "0")
        tg_send("🛑 Kill Switch تم تفعيله – التداول متوقف فورًا")
        handle_callback({"data": "menu", "id": cq_id, "message": msg}, _answer=False)

    elif data == "last_signals":
        tg_send("آخر الإشارات تُعرض عند اكتشافها مباشرة.")
        handle_callback({"data": "menu", "id": cq_id, "message": msg}, _answer=False)

def handle_text_message(text: str, chat_id: str):
    if chat_id != TELEGRAM_ADMIN_ID:
        return
    if chat_id in waiting_for and waiting_for[chat_id] == "add_channel":
        if text.startswith("/"):
            tg_send("أضف اسم القناة بدون /  (مثال: abojasimp)\nأو /cancel للإلغاء")
            if text.strip().lower() in ("/cancel", "/start", "/menu"):
                del waiting_for[chat_id]
                tg_send("تم الإلغاء", main_menu_keyboard())
            return
        username = text.strip().lstrip("@").lower()
        username = re.sub(r"[^a-z0-9_]", "", username)
        if len(username) < 3:
            tg_send("اسم قناة غير صالح")
            return
        # Save username temporarily and ask for sample template
        waiting_for[chat_id] = f"add_template:{username}"
        tg_send(
            f"تم استلام القناة @{username}\n\n"
            "الآن أرسل <b>نموذج توصية</b> من هذه القناة (انسخ رسالة توصية قديمة والصقها هنا).\n"
            "البوت سيستخدم هذا النموذج لتمييز صفقات هذه القناة.\n\n"
            "أو أرسل /skip لتخطي واستخدام الوضع التلقائي."
        )
        return

    if chat_id in waiting_for and str(waiting_for[chat_id]).startswith("add_template:"):
        if text.strip().lower() in ("/cancel", "/start", "/menu"):
            del waiting_for[chat_id]
            tg_send("تم الإلغاء", main_menu_keyboard())
            return
        username = waiting_for[chat_id].split(":", 1)[1]
        sample = text.strip()
        template_name = "custom"
        if sample.lower() in ("/skip", "skip", "تخطي"):
            sample = ""
            template_name = "auto"

        conn = get_db()
        try:
            if USE_POSTGRES:
                cur = conn.cursor()
                try:
                    cur.execute(
                        """INSERT INTO channels (username, enabled, last_msg_id, template, sample_text, added_at)
                           VALUES (%s, 1, 0, %s, %s, %s)
                           ON CONFLICT (username) DO UPDATE SET template=EXCLUDED.template, sample_text=EXCLUDED.sample_text""",
                        (username, template_name, sample[:1500], datetime.now(timezone.utc).isoformat())
                    )
                except Exception:
                    cur.execute(
                        """INSERT INTO channels (username, enabled, last_msg_id, template, added_at)
                           VALUES (%s, 1, 0, %s, %s)
                           ON CONFLICT (username) DO UPDATE SET template=EXCLUDED.template""",
                        (username, template_name, datetime.now(timezone.utc).isoformat())
                    )
                tg_send(f"✅ تمت إضافة @{username}\nالنموذج: {template_name}")
            else:
                try:
                    # ensure columns exist
                    try:
                        conn.execute("ALTER TABLE channels ADD COLUMN sample_text TEXT DEFAULT ''")
                    except Exception:
                        pass
                    try:
                        conn.execute("ALTER TABLE channels ADD COLUMN template TEXT DEFAULT 'auto'")
                    except Exception:
                        pass
                    conn.execute(
                        """INSERT OR REPLACE INTO channels (username, enabled, last_msg_id, template, sample_text, added_at)
                           VALUES (?, 1, 0, ?, ?, ?)""",
                        (username, template_name, sample[:1500], datetime.now(timezone.utc).isoformat())
                    )
                    tg_send(f"✅ تمت إضافة @{username}\nالنموذج: {template_name}")
                except Exception as e:
                    tg_send(f"خطأ: {e}")
            conn.commit()
        finally:
            conn.close()
        del waiting_for[chat_id]
        return

    # commands
    if text.startswith("/start") or text.startswith("/menu"):
        s = get_settings()
        text_msg = (
            f"🤖 <b>MEXC SPOT Auto Trader v2</b>\n\n"
            f"التداول: {'🟢 ON' if s['trading_enabled'] else '🔴 OFF'}\n"
            f"Paper: {'📝 ON' if s['paper_mode'] else '💰 OFF'}\n"
            f"المبلغ: {s['trade_amount']} USDT\n"
            f"حد الصفقات: {s['max_positions']}\n"
            f"استخدم الأزرار للتحكم الكامل"
        )
        tg_send(text_msg, main_menu_keyboard())
    elif text.startswith("/status"):
        s = get_settings()
        tg_send(f"Trading: {s['trading_enabled']} | Paper: {s['paper_mode']} | Amount: {s['trade_amount']} | Open: {count_open_positions()}")

# ==================== MAIN LOOP ====================
last_update_id = 0

def poll_telegram():
    global last_update_id
    res = tg_api("getUpdates", {"offset": last_update_id + 1, "timeout": 2, "limit": 20})
    if not res or not res.get("ok"):
        return
    for u in res.get("result", []):
        last_update_id = max(last_update_id, u["update_id"])
        if "callback_query" in u:
            handle_callback(u["callback_query"])
        elif "message" in u:
            msg = u["message"]
            chat_id = str(msg["chat"]["id"])
            text = msg.get("text", "")
            if text:
                handle_text_message(text, chat_id)

def monitor_channels():
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT username, last_msg_id FROM channels WHERE enabled=1")
            channels = cur.fetchall()
        else:
            channels = conn.execute("SELECT username, last_msg_id FROM channels WHERE enabled=1").fetchall()
    finally:
        conn.close()
    for ch in channels:
        username = ch["username"]
        last_id = ch["last_msg_id"] or 0
        messages = scrape_channel(username)
        if not messages:
            continue
        if last_id == 0:
            max_id = max(m["id"] for m in messages)
            conn = get_db()
            try:
                if USE_POSTGRES:
                    cur = conn.cursor()
                    cur.execute("UPDATE channels SET last_msg_id=%s WHERE username=%s", (max_id, username))
                else:
                    conn.execute("UPDATE channels SET last_msg_id=? WHERE username=?", (max_id, username))
                conn.commit()
            finally:
                conn.close()
            logger.info(f"@{username} first run → skip history, last_id={max_id}")
            continue
        had_error = False
        for msg in messages:
            if msg["id"] > last_id:
                try:
                    process_signal(msg)
                except Exception:
                    had_error = True
                    logger.exception("Failed processing @%s message %s", username, msg.get("id"))
        # Advance checkpoint for handled posts, including non-signals and duplicates.
        # Keep the old checkpoint when a handler fails so the next poll can retry.
        if not had_error:
            max_id = max(m["id"] for m in messages)
            if max_id > last_id:
                conn = get_db()
                try:
                    if USE_POSTGRES:
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE channels SET last_msg_id=GREATEST(last_msg_id, %s) WHERE username=%s",
                            (max_id, username),
                        )
                    else:
                        conn.execute(
                            "UPDATE channels SET last_msg_id=MAX(last_msg_id, ?) WHERE username=?",
                            (max_id, username),
                        )
                    conn.commit()
                finally:
                    conn.close()

def print_banner():
    s = get_settings()
    print("=" * 55)
    print("MEXC SPOT AUTO TRADER v2")
    print("Telegram API ID / Hash : NOT USED")
    print("Trading Mode           : SPOT ONLY")
    print("Futures / Leverage / Short : DISABLED")
    print(f"Paper Mode             : {s['paper_mode']}")
    print(f"Trading Enabled        : {s['trading_enabled']}")
    print(f"Trade Amount           : {s['trade_amount']} USDT")
    print(f"Max Open Positions     : {s['max_positions']}")
    print(f"TPs                    : {s['tp1_pct']}% / {s['tp2_pct']}% / {s['tp3_pct']}%")
    print("Control                : Telegram Bot (Buttons)")
    print("=" * 55)

def telegram_worker():
    """Background thread: only handles bot commands/buttons so response is fast."""
    # Clear any webhook so getUpdates works (prevents 409 Conflict)
    try:
        tg_api("deleteWebhook", {"drop_pending_updates": False})
        logger.info("Webhook cleared (if any)")
    except Exception as e:
        logger.warning(f"deleteWebhook: {e}")
    logger.info("Telegram worker started")
    while True:
        try:
            poll_telegram()
            time.sleep(0.35)
        except Exception as e:
            logger.error(f"Telegram worker error: {e}")
            time.sleep(3)

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID required")
        sys.exit(1)

    init_db()
    print_banner()
    init_exchange()

    # Start fast Telegram listener in background
    t = threading.Thread(target=telegram_worker, daemon=True)
    t.start()

    tg_send(
        "🚀 البوت يعمل الآن\n"
        f"Paper: {get_settings()['paper_mode']}\n"
        f"استخدم /start أو الأزرار",
        main_menu_keyboard()
    )

    consecutive_errors = 0
    while True:
        try:
            # Main thread focuses on channel monitoring + position management
            monitor_channels()
            check_and_manage_positions()
            consecutive_errors = 0
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Shutdown")
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Loop error: {e}\n{traceback.format_exc()}")
            if consecutive_errors >= 8:
                tg_send(f"❌ أخطاء متكررة\n{str(e)[:200]}")
                time.sleep(60)
                consecutive_errors = 0
            else:
                time.sleep(min(20, consecutive_errors * 2))

if __name__ == "__main__":
    main()
