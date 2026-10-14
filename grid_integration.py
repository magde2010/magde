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


def _payload(cfg, setup_complete=True):
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
        "setup_complete": bool(setup_complete),
    }


def _read_saved(db):
    persistent = _persistent_read()
    if persistent:
        return persistent
    row = db.conn.execute("SELECT config_json FROM grid_bot_configs WHERE id=1").fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def _persistent_read():
    url = os.getenv("DATABASE_URL")
    if not url:
        return {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(url, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS lotfe_grid_settings (id INTEGER PRIMARY KEY, config_json TEXT NOT NULL)"))
            row = conn.execute(text("SELECT config_json FROM lotfe_grid_settings WHERE id=1")).fetchone()
        engine.dispose()
        return json.loads(row[0]) if row else {}
    except Exception as exc:
        log.warning("Persistent Grid settings unavailable: %s", type(exc).__name__)
        return {}


def _persistent_save(payload):
    url = os.getenv("DATABASE_URL")
    if not url:
        return False
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(url, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS lotfe_grid_settings (id INTEGER PRIMARY KEY, config_json TEXT NOT NULL)"))
            conn.execute(text("DELETE FROM lotfe_grid_settings WHERE id=1"))
            conn.execute(text("INSERT INTO lotfe_grid_settings (id, config_json) VALUES (1, :config)"), {"config": json.dumps(payload, default=str)})
        engine.dispose()
        return True
    except Exception as exc:
        log.error("Could not persist Grid settings: %s", type(exc).__name__)
        return False


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
    service = None
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
        saved = _read_saved(_db)
        cfg = _apply_saved(cfg, saved)
        setup_complete = bool(saved.get("setup_complete"))
        exchange = ExchangeAdapter(dry_run=cfg.dry_run)
        market = exchange.market(cfg.symbol)
        if not market.get("spot", True):
            raise RuntimeError("Grid يدعم سوق Spot فقط")
        payload = _payload(cfg, setup_complete=setup_complete)
        _db.save_config(payload)
        _persistent_save(payload)
        service = TradingService(cfg, _db, exchange)
        service.needs_setup = not setup_complete
        if setup_complete:
            service.recover()
        else:
            # Never inspect or trade the environment defaults (BTC/100) before
            # the user explicitly saves the symbol and budget in Telegram.
            service.recovered = True
            _db.update_position(state='PAUSED', running=0)
        _service = service
        return _service, None
    except Exception as exc:
        _error = f"تعذر تهيئة Grid: {type(exc).__name__}: {exc}"
        log.exception("Grid initialization failed")
        if service is not None and _db is not None and "Order creation uncertain" in str(exc):
            service.recovered = True
            service.last_error = str(exc)
            _db.update_position(state="FROZEN", running=0)
            _service = service
            return _service, None
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
        complete = bool(_read_saved(service.db).get("setup_complete"))
        if service.cfg.symbol != "BTC/USDT" and service.cfg.total_budget != Decimal("100"):
            complete = True
        payload = _payload(service.cfg, setup_complete=complete)
        service.db.save_config(payload)
        _persistent_save(payload)
        service.needs_setup = not complete
        return True, "تم حفظ الإعداد بنجاح."
    except Exception as exc:
        return False, f"إعداد غير صالح: {exc}"


def switch_grid_mode(live):
    """Persist and reload the exchange adapter; strategy remains paused."""
    service, error = get_grid_service()
    if error:
        return False, error
    if getattr(service, "needs_setup", False):
        return False, "أكمل إعداد العملة والميزانية من Telegram أولًا؛ لن يتم استخدام BTC/100 الافتراضية."
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
    _persistent_save(payload)
    grid_shutdown()
    fresh, fresh_error = get_grid_service()
    if fresh_error:
        return False, fresh_error
    fresh.db.update_position(running=0, state="PAUSED", emergency_stop=0)
    return True, "تم التحويل إلى LIVE وإعادة تحميل الاتصال. Grid ما زال متوقفًا؛ اضغط تشغيل Grid عند استعدادك." if live else "تم الرجوع إلى DRY RUN."


def replace_grid_orders():
    """Cancel only this Grid strategy's orders, then rebuild from current holdings."""
    service, error = get_grid_service()
    if error:
        return False, error
    if getattr(service, "needs_setup", False):
        return False, "أكمل إعداد العملة والميزانية أولًا."
    if service.cfg.dry_run:
        return False, "Grid ليس في وضع LIVE."
    try:
        from gridbot.recovery import client_id
        remote_cancelled = 0
        remote = list(service.exchange.fetch_open_orders(service.cfg.symbol) or [])
        # Local SQLite may have been lost after a deploy. Match only our
        # deterministic grid-* client IDs; never cancel unrelated user orders.
        for order in remote:
            cid = str(client_id(order) or '')
            if not cid.startswith('grid-'):
                continue
            oid = order.get('id')
            if not oid:
                raise RuntimeError('وجد أمر Grid قديم بلا رقم يمكن إلغاؤه؛ تم الإيقاف بأمان')
            service.exchange.cancel_order(oid, service.cfg.symbol)
            checked = service.exchange.fetch_order(oid, service.cfg.symbol)
            if not checked or str(checked.get('status','')).lower() not in ('canceled','cancelled','closed','rejected','expired'):
                raise RuntimeError(f'لم يتم تأكيد إلغاء الأمر {oid}')
            local = service.db.order_by_client_id(cid)
            if local:
                checked_status = str(checked.get('status','')).lower()
                if checked_status == 'closed':
                    service._sync_order(local, checked, rebuild_tps=False)
                else:
                    service.db.update_order(cid, exchange_order_id=str(oid), status='canceled', raw_status='canceled', last_error=None)
            remote_cancelled += 1
        before = len(service.db.open_orders()) + remote_cancelled
        service._cancel_owned({'initial', 'dca', 'tp'})
        service.db.update_position(running=0, state='PAUSED', emergency_stop=0)
        service._adopt_existing_position()
        if service._position()['state'] != 'FROZEN':
            service._rebuild_tps()
            service._ensure_dca()
            service.db.update_position(state='PAUSED', running=0)
        after = len(service.db.open_orders())
        return True, f"تم إلغاء {before} أمر Grid قديم وإعادة بناء {after} أمرًا للعملة {service.cfg.symbol}. المركز والرصيد لم يُباعا."
    except Exception as exc:
        service.last_error = f"{type(exc).__name__}: {exc}"
        service.db.update_position(state='FROZEN', running=0)
        return False, f"تعذر إعادة البناء بأمان: {type(exc).__name__}: {exc}"


def restart_grid_strategy():
    """Reset Grid bookkeeping, keep the exchange holdings, and rebuild the strategy."""
    service, error = get_grid_service()
    if error:
        return False, error
    if getattr(service, "needs_setup", False):
        return False, "أكمل إعداد العملة والميزانية أولًا."
    if service.cfg.dry_run:
        return False, "إعادة التشغيل العامة متاحة في LIVE فقط."
    try:
        service.db.update_position(running=0, state='PAUSED')
        from gridbot.recovery import client_id
        remote = list(service.exchange.fetch_open_orders(service.cfg.symbol) or [])
        canceled = 0
        for order in remote:
            cid = str(client_id(order) or '')
            if not cid.startswith('grid-'):
                continue
            oid = order.get('id')
            if not oid:
                raise RuntimeError('أمر Grid بلا رقم؛ تم الإيقاف دون إعادة تشغيل')
            service.exchange.cancel_order(oid, service.cfg.symbol)
            checked = service.exchange.fetch_order(oid, service.cfg.symbol)
            status = str((checked or {}).get('status','')).lower()
            if status not in ('canceled','cancelled','closed','rejected','expired'):
                raise RuntimeError(f'لم يتم تأكيد إلغاء الأمر {oid}')
            canceled += 1
        service.db.reset_strategy_orders()
        adopted = service._adopt_existing_position()
        if not adopted:
            service.db.update_position(symbol=service.cfg.symbol, state='PAUSED', running=0)
        if service._position()['quantity'] and service._position()['state'] != 'FROZEN':
            service._rebuild_tps()
        service.db.update_position(running=1, state='ACTIVE')
        service._ensure_dca()
        return True, f"تمت إعادة تشغيل Grid من الصفر بنفس الإعدادات. أُلغي {canceled} أمرًا، وتم الحفاظ على الرصيد الحالي وبناء TP/DCA حسب الميزانية."
    except Exception as exc:
        service.last_error = f"{type(exc).__name__}: {exc}"
        service.db.update_position(state='FROZEN', running=0)
        return False, f"تعذر إعادة التشغيل بأمان: {type(exc).__name__}: {exc}"


async def grid_monitor_job(context):
    service, error = get_grid_service()
    if service is None:
        return
    if getattr(service, "needs_setup", False):
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
