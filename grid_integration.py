"""Optional in-process integration for the standalone mexc-grid-bot package."""
import os
import sys
import json
import logging
from decimal import Decimal
from pathlib import Path

log = logging.getLogger(__name__)
_service = None
_db = None
_lock = None
_error = None


def _payload(cfg):
    return {
        "symbol": cfg.symbol,
        "total_budget": str(cfg.total_budget),
        "initial_pct": str(cfg.initial_pct),
        "dca_drops": list(map(str, cfg.dca_drops)),
        "dca_allocations": list(map(str, cfg.dca_allocations)),
        "tp_gains": list(map(str, cfg.tp_gains)),
        "tp_sell_pcts": list(map(str, cfg.tp_sell_pcts)),
        "recenter_percent": str(cfg.upward_recenter_percent),
        "dry_run": cfg.dry_run,
        "live_enabled": cfg.live_enabled,
    }


def _read_saved(db):
    row = db.conn.execute("SELECT config_json FROM grid_bot_configs WHERE id=1").fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def _apply_saved(cfg, saved):
    """Apply only user-editable persisted values, then validate them."""
    if not saved:
        return cfg
    for key, attr in {
        "symbol": "symbol", "total_budget": "total_budget", "initial_pct": "initial_pct",
        "dca_drops": "dca_drops", "dca_allocations": "dca_allocations",
        "tp_gains": "tp_gains", "tp_sell_pcts": "tp_sell_pcts",
        "recenter_percent": "upward_recenter_percent",
    }.items():
        if key not in saved:
            continue
        value = saved[key]
        if isinstance(value, list):
            value = [Decimal(str(x)) for x in value]
        elif key != "symbol":
            value = Decimal(str(value))
        setattr(cfg, attr, value)
    if "dry_run" in saved:
        cfg.dry_run = bool(saved["dry_run"])
    if "live_enabled" in saved:
        cfg.live_enabled = bool(saved["live_enabled"])
    return cfg.validate()


def _reset_paper_session(service):
    """Discard only simulated orders/position; never touch live exchange orders."""
    if not service.cfg.dry_run:
        return
    for order in service.db.open_orders():
        service.db.update_order(
            order["client_order_id"],
            status="canceled",
            raw_status="paper_reset",
            last_error="Dry Run session reset before settings/live mode change",
        )
    budget = str(service.cfg.total_budget)
    service.db.update_position(
        symbol=service.cfg.symbol,
        grid_reference=None,
        quantity="0",
        average_entry="0",
        cost="0",
        spent="0",
        remaining_budget=budget,
        state="PAUSED",
        dca4_complete=0,
        emergency_stop=0,
        running=0,
        tp_reference_avg="0",
    )
    service.db.event("paper_session_reset", {"reason": "settings_or_live_mode_change"})


def get_grid_service():
    global _service, _db, _lock, _error
    if _service is not None:
        return _service, None
    if os.getenv("GRID_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return None, "Grid معطل عبر GRID_ENABLED=false"
    package_root = Path(__file__).resolve().parent / "mexc-grid-bot"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    try:
        from gridbot.config import config_from_env
        from gridbot.db import GridDB
        from gridbot.exchange import ExchangeAdapter
        from gridbot.lock import SingletonLock
        from gridbot.service import TradingService

        cfg = config_from_env()
        _lock = SingletonLock(os.getenv("GRID_LOCK_PATH", "./data/grid_bot.lock"))
        _lock.acquire()
        _db = GridDB(os.getenv("GRID_DB_PATH", "./data/grid_bot.sqlite3"))
        cfg = _apply_saved(cfg, _read_saved(_db))
        exchange = ExchangeAdapter(dry_run=cfg.dry_run)
        market = exchange.market(cfg.symbol)
        if not market.get("spot", True):
            raise RuntimeError("Grid يدعم سوق Spot فقط")
        _db.save_config(_payload(cfg))
        service = TradingService(cfg, _db, exchange)
        service.recover()
        _service = service
        return _service, None
    except Exception as exc:
        _error = f"تعذر تهيئة Grid: {type(exc).__name__}: {exc}"
        log.exception("Grid initialization failed")
        if _db is not None:
            try: _db.close()
            except Exception: pass
            _db = None
        if _lock is not None:
            try: _lock.release()
            except Exception: pass
            _lock = None
        return None, _error


def update_grid_setting(field, raw_value):
    service, error = get_grid_service()
    if error:
        return False, error
    position = service.db.position()
    if position.get("running"):
        return False, "أوقف Grid أولًا وألغِ/انتظر الأوامر المفتوحة قبل تعديل الإعدادات."
    if service.cfg.dry_run:
        _reset_paper_session(service)
    elif service.db.open_orders():
        return False, "توجد أوامر LIVE مفتوحة؛ ألغها أو انتظر تنفيذها قبل تعديل الإعدادات."
    try:
        if field == "symbol":
            value = str(raw_value).strip().upper().replace("-", "/")
            if "/" not in value:
                value += "/USDT"
        elif field in {"dca_drops", "dca_allocations", "tp_gains", "tp_sell_pcts"}:
            value = [Decimal(x.strip().replace(",", ".")) for x in str(raw_value).split(",") if x.strip()]
        else:
            value = Decimal(str(raw_value).strip().replace(",", ".").replace("%", ""))
        attr = {"symbol": "symbol", "budget": "total_budget", "initial": "initial_pct", "recenter": "upward_recenter_percent"}.get(field, field)
        previous = getattr(service.cfg, attr)
        setattr(service.cfg, attr, value)
        try:
            service.cfg.validate()
        except Exception:
            setattr(service.cfg, attr, previous)
            raise
        service.db.save_config(_payload(service.cfg))
        return True, "تم حفظ الإعداد بنجاح."
    except Exception as exc:
        return False, f"إعداد غير صالح: {exc}"


def switch_grid_mode(live):
    """Persist and reload the exchange adapter; strategy remains paused."""
    service, error = get_grid_service()
    if error:
        return False, error
    if service.db.position().get("running"):
        return False, "أوقف Grid أولًا وتأكد من عدم وجود أوامر مفتوحة."
    if service.cfg.dry_run:
        _reset_paper_session(service)
    elif service.db.open_orders():
        return False, "توجد أوامر LIVE مفتوحة؛ لا يمكن تبديل الوضع قبل مراجعتها."
    if live and not (os.getenv("MEXC_API_KEY") and os.getenv("MEXC_API_SECRET")):
        return False, "مفاتيح MEXC غير موجودة في بيئة التشغيل. لا ترسلها عبر Telegram."
    payload = _payload(service.cfg)
    payload["dry_run"] = not live
    payload["live_enabled"] = bool(live)
    service.db.save_config(payload)
    grid_shutdown()
    fresh, fresh_error = get_grid_service()
    if fresh_error:
        return False, fresh_error
    fresh.db.update_position(running=0, state="PAUSED", emergency_stop=0)
    return True, "تم التحويل إلى LIVE وإعادة تحميل الاتصال. Grid ما زال متوقفًا؛ اضغط تشغيل Grid عند استعدادك." if live else "تم الرجوع إلى DRY RUN."


async def grid_monitor_job(context):
    service, error = get_grid_service()
    if service is None:
        return
    try:
        await service.tick()
    except Exception:
        log.exception("Grid monitor cycle failed")


def grid_shutdown():
    global _service, _db, _lock
    if _db is not None:
        try: _db.close()
        except Exception: pass
    if _lock is not None:
        try: _lock.release()
        except Exception: pass
    _service = _db = _lock = None
