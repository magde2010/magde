"""Optional in-process integration for the standalone mexc-grid-bot package."""
import os
import sys
import logging
from pathlib import Path

log = logging.getLogger(__name__)
_service = None
_db = None
_lock = None
_error = None


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
        exchange = ExchangeAdapter(dry_run=cfg.dry_run)
        market = exchange.market(cfg.symbol)
        if not market.get("spot", True):
            raise RuntimeError("Grid يدعم سوق Spot فقط")
        _db.save_config({
            "symbol": cfg.symbol,
            "total_budget": str(cfg.total_budget),
            "initial_pct": str(cfg.initial_pct),
            "dca_drops": list(map(str, cfg.dca_drops)),
            "dca_allocations": list(map(str, cfg.dca_allocations)),
            "tp_gains": list(map(str, cfg.tp_gains)),
            "tp_sell_pcts": list(map(str, cfg.tp_sell_pcts)),
            "dry_run": cfg.dry_run,
            "live_enabled": cfg.live_enabled,
        })
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
