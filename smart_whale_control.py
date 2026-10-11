"""تحكم آمن وإعدادات خدمة smart_whale_system من بوت Lotfe."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
SYSTEM_DIR = ROOT / "smart_whale_system"
PID_FILE = SYSTEM_DIR / ".smart_whale.pid"
LOG_FILE = SYSTEM_DIR / "smart_whale.log"
SETTINGS_FILE = SYSTEM_DIR / "runtime_settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "allocated_capital": 150.0,
    "position_size_usdt": 30.0,
    "max_open_positions": 5,
    "min_multiple": 1.5,
    "min_score": 82.0,
    "confirmations_required": 2,
    "live_confirmations_required": 3,
    "radar_window": "4h",
    "stop_loss_pct": 7.0,
    "take_profit_1_pct": 8.0,
    "take_profit_2_pct": 15.0,
    "check_interval_minutes": 10,
    "min_order_usdt": 5.0,
    "max_slippage_pct": 1.5,
}

FIELD_LABELS = {
    "allocated_capital": "رأس المال المخصص",
    "position_size_usdt": "حجم الصفقة",
    "max_open_positions": "أقصى صفقات مفتوحة",
    "min_multiple": "الحد الأدنى لنشاط الحيتان",
    "min_score": "حد Flow Score للشراء",
    "confirmations_required": "تأكيدات الورقي",
    "live_confirmations_required": "تأكيدات الحقيقي",
    "radar_window": "نافذة الرادار",
    "stop_loss_pct": "وقف الخسارة",
    "take_profit_1_pct": "الهدف الأول",
    "take_profit_2_pct": "الهدف الثاني",
    "check_interval_minutes": "فاصل الفحص",
    "min_order_usdt": "الحد الأدنى للأمر",
    "max_slippage_pct": "أقصى انزلاق سعري",
}


def load_settings() -> dict[str, Any]:
    values = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            values.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError, TypeError):
        pass
    return values


def apply_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Apply and persist a complete settings snapshot loaded from the DB."""
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (values or {}).items() if k in DEFAULT_SETTINGS})
    SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="runtime_settings.", suffix=".tmp", dir=SYSTEM_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, SETTINGS_FILE)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return merged


def update_setting(key: str, value: Any) -> dict[str, Any]:
    if key not in DEFAULT_SETTINGS:
        raise ValueError("إعداد غير معروف")
    values = load_settings()
    values[key] = value
    SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="runtime_settings.", suffix=".tmp", dir=SYSTEM_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, SETTINGS_FILE)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return values


def settings_text() -> str:
    s = load_settings()
    return (
        "⚙️ *إعدادات نظام الحيتان الذكي*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 رأس المال المخصص: `{s['allocated_capital']:g}$`\n"
        f"📦 حجم الصفقة: `{s['position_size_usdt']:g}$`\n"
        f"📊 أقصى صفقات مفتوحة: `{s['max_open_positions']}`\n"
        f"🐋 الحد الأدنى لنشاط الحيتان: `{s['min_multiple']:g}x`\n"
        f"🧠 Flow Score للشراء: `{s['min_score']:g}` | تأكيدات: `{s['confirmations_required']}` / Live `{s['live_confirmations_required']}`\n"
        f"🛑 وقف الخسارة: `{s['stop_loss_pct']:g}%`\n"
        f"🎯 الهدف 1: `{s['take_profit_1_pct']:g}%` | الهدف 2: `{s['take_profit_2_pct']:g}%`\n"
        f"⏱️ الفحص كل: `{s['check_interval_minutes']}` دقيقة\n"
        f"📡 نافذة الرادار: `{s['radar_window']}` — الفحص عام\n"
        f"📏 الحد الأدنى للأمر: `{s['min_order_usdt']:g}$`\n"
        f"↔️ أقصى انزلاق: `{s['max_slippage_pct']:g}%`\n\n"
        "تُحفظ الإعدادات على الخادم وتُطبّق عند تشغيل النظام التالي."
    )


def _read_pid() -> Optional[int]:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running() -> bool:
    pid = _read_pid()
    if _alive(pid):
        return True
    if pid:
        PID_FILE.unlink(missing_ok=True)
    return False


def status_text() -> str:
    pid = _read_pid()
    if _alive(pid):
        return f"🟢 يعمل\nPID: `{pid}`\nالسجل: `smart_whale_system/smart_whale.log`"
    return "⚪ متوقف\nلم يتم تشغيل نظام الحيتان الذكي حاليًا."


def start() -> tuple[bool, str]:
    if is_running():
        return False, "النظام يعمل بالفعل."
    if not (SYSTEM_DIR / "main.py").exists():
        return False, "ملفات النظام غير موجودة."
    try:
        log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        log.write("\n=== smart whale started from Lotfe bot ===\n")
        proc = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=str(SYSTEM_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        time.sleep(0.8)
        if proc.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            return False, "فشل بدء النظام؛ راجع smart_whale_system/smart_whale.log."
        return True, f"تم تشغيل النظام بنجاح. PID: `{proc.pid}`"
    except Exception as exc:
        return False, f"تعذر تشغيل النظام: `{str(exc)[:180]}`"


def stop() -> tuple[bool, str]:
    pid = _read_pid()
    if not _alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return False, "النظام متوقف بالفعل."
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.25)
            if not _alive(pid):
                break
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        return True, "تم إيقاف نظام الحيتان الذكي."
    except Exception as exc:
        return False, f"تعذر إيقاف النظام: `{str(exc)[:180]}`"
