import sqlite3, json
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS grid_bot_configs (id INTEGER PRIMARY KEY CHECK(id=1), config_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grid_positions (id INTEGER PRIMARY KEY CHECK(id=1), symbol TEXT NOT NULL, grid_reference TEXT, quantity TEXT NOT NULL DEFAULT '0', average_entry TEXT NOT NULL DEFAULT '0', cost TEXT NOT NULL DEFAULT '0', spent TEXT NOT NULL DEFAULT '0', remaining_budget TEXT NOT NULL DEFAULT '0', state TEXT NOT NULL DEFAULT 'IDLE', dca4_complete INTEGER NOT NULL DEFAULT 0, emergency_stop INTEGER NOT NULL DEFAULT 0, running INTEGER NOT NULL DEFAULT 0, last_recenter_time TEXT, last_3m_candle_ms INTEGER, last_sync_at TEXT, tp_reference_avg TEXT NOT NULL DEFAULT '0', tp_base_quantity TEXT NOT NULL DEFAULT '0');
CREATE TABLE IF NOT EXISTS grid_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL UNIQUE, exchange_order_id TEXT, symbol TEXT NOT NULL, role TEXT NOT NULL, level INTEGER NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, price TEXT, amount TEXT, filled TEXT NOT NULL DEFAULT '0', cost TEXT NOT NULL DEFAULT '0', raw_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT, accounted_filled TEXT NOT NULL DEFAULT '0', accounted_cost TEXT NOT NULL DEFAULT '0');
CREATE INDEX IF NOT EXISTS idx_grid_orders_status ON grid_orders(status);
CREATE INDEX IF NOT EXISTS idx_grid_orders_role ON grid_orders(role,status);
CREATE TABLE IF NOT EXISTS grid_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
'''
def now(): return datetime.now(timezone.utc).isoformat()
class GridDB:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(str(self.path),timeout=30,isolation_level=None,check_same_thread=False); self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL'); self.conn.execute('PRAGMA foreign_keys=ON'); self.conn.execute('PRAGMA busy_timeout=30000'); self.conn.executescript(SCHEMA)
        cols={r['name'] for r in self.conn.execute('PRAGMA table_info(grid_positions)')}
        for name,decl in [('cost',"TEXT NOT NULL DEFAULT '0'"),('emergency_stop','INTEGER NOT NULL DEFAULT 0'),('running','INTEGER NOT NULL DEFAULT 0'),('last_sync_at','TEXT'),('tp_reference_avg',"TEXT NOT NULL DEFAULT '0'"),('tp_base_quantity',"TEXT NOT NULL DEFAULT '0'")]:
            if name not in cols: self.conn.execute(f'ALTER TABLE grid_positions ADD COLUMN {name} {decl}')
        ocols={r['name'] for r in self.conn.execute('PRAGMA table_info(grid_orders)')}
        if 'last_error' not in ocols: self.conn.execute('ALTER TABLE grid_orders ADD COLUMN last_error TEXT')
        if 'accounted_filled' not in ocols: self.conn.execute("ALTER TABLE grid_orders ADD COLUMN accounted_filled TEXT NOT NULL DEFAULT '0'")
        if 'accounted_cost' not in ocols: self.conn.execute("ALTER TABLE grid_orders ADD COLUMN accounted_cost TEXT NOT NULL DEFAULT '0'")
        self.conn.execute("INSERT OR IGNORE INTO grid_positions(id,symbol,remaining_budget) VALUES(1,'BTC/USDT','0')")
    def event(self,event_type,details): self.conn.execute('INSERT INTO grid_events(event_type,details_json,created_at) VALUES(?,?,?)',(event_type,json.dumps(details,default=str),now()))
    def save_config(self,data): self.conn.execute('INSERT INTO grid_bot_configs(id,config_json,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at',(json.dumps(data,default=str),now()))
    def position(self): return dict(self.conn.execute('SELECT * FROM grid_positions WHERE id=1').fetchone())
    def update_position(self,**kw):
        if not kw: return
        kw['last_sync_at']=now(); sql=', '.join(f'{k}=?' for k in kw)
        self.conn.execute(f'UPDATE grid_positions SET {sql} WHERE id=1',(*kw.values(),))
    def orders(self, statuses=None):
        if statuses:
            marks=','.join('?' for _ in statuses); rows=self.conn.execute(f'SELECT * FROM grid_orders WHERE status IN ({marks}) ORDER BY id',tuple(statuses)).fetchall()
        else: rows=self.conn.execute('SELECT * FROM grid_orders ORDER BY id').fetchall()
        return [dict(r) for r in rows]
    def open_orders(self): return self.orders(['pending','open','unknown','partially_filled','cancel_pending'])
    def recovery_orders(self):
        rows=self.conn.execute("SELECT * FROM grid_orders WHERE status IN ('pending','open','unknown','partially_filled','cancel_pending') OR CAST(filled AS REAL)>CAST(accounted_filled AS REAL) ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    def order_by_client_id(self,cid):
        r=self.conn.execute('SELECT * FROM grid_orders WHERE client_order_id=?',(cid,)).fetchone(); return dict(r) if r else None
    def insert_order_intent(self,cid,symbol,role,level,side,price,amount):
        t=now(); self.conn.execute('INSERT OR IGNORE INTO grid_orders(client_order_id,symbol,role,level,side,status,price,amount,created_at,updated_at) VALUES(?,?,?,?,? ,?,?,?,?,?)',(cid,symbol,role,level,side,'pending',str(price),str(amount),t,t)); return self.order_by_client_id(cid)
    def update_order(self,cid,**kw):
        kw['updated_at']=now(); sql=', '.join(f'{k}=?' for k in kw); self.conn.execute(f'UPDATE grid_orders SET {sql} WHERE client_order_id=?',(*kw.values(),cid))
    def close(self): self.conn.close()
    def reset_strategy_orders(self):
        self.conn.execute('DELETE FROM grid_orders')
        self.conn.execute('DELETE FROM grid_events')
        self.conn.execute("UPDATE grid_positions SET grid_reference=NULL,quantity='0',average_entry='0',cost='0',spent='0',remaining_budget='0',state='PAUSED',dca4_complete=0,emergency_stop=0,running=0,last_recenter_time=NULL,last_3m_candle_ms=NULL,tp_reference_avg='0',tp_base_quantity='0' WHERE id=1")
