import os, logging
from dotenv import load_dotenv
from .config import config_from_env
from .db import GridDB
from .lock import SingletonLock
from .security import configure_logging
from .exchange import ExchangeAdapter
from .service import TradingService
from .telegram_ui import run_telegram

def main():
    load_dotenv(); configure_logging(os.getenv('LOG_LEVEL','INFO'))
    cfg=config_from_env()
    if not cfg.dry_run and not cfg.live_enabled: raise RuntimeError('LIVE trading disabled: explicitly set LIVE_TRADING_ENABLED=true to proceed')
    db_path=os.getenv('GRID_DB_PATH','./data/grid_bot.sqlite3'); lock_path=os.getenv('GRID_LOCK_PATH','./data/grid_bot.lock')
    with SingletonLock(lock_path):
        db=GridDB(db_path)
        try:
            exchange=ExchangeAdapter(dry_run=cfg.dry_run)
            market=exchange.market(cfg.symbol)
            if not market.get('spot',True): raise RuntimeError('Only Spot symbols are allowed')
            db.save_config({'symbol':cfg.symbol,'total_budget':str(cfg.total_budget),'initial_pct':str(cfg.initial_pct),'dca_drops':list(map(str,cfg.dca_drops)),'dca_allocations':list(map(str,cfg.dca_allocations)),'tp_gains':list(map(str,cfg.tp_gains)),'tp_sell_pcts':list(map(str,cfg.tp_sell_pcts)),'dry_run':cfg.dry_run,'live_enabled':cfg.live_enabled})
            service=TradingService(cfg,db,exchange)
            # Recovery gates all order placement. Failure aborts startup instead of guessing.
            state=service.recover(); db.event('startup',{'dry_run':cfg.dry_run,'symbol':cfg.symbol,'recovery_state':state})
            if state=='FROZEN': logging.error('Startup recovery is ambiguous; strategy order placement is frozen')
            run_telegram(service)
        finally: db.close()
if __name__=='__main__': main()
