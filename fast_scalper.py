from __future__ import annotations
import json, logging, threading, time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt
import websocket
from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, Text

import config
from database import Base, SessionLocal

log = logging.getLogger("lotfe.fast_scalper")

class ScalperConfig(Base):
    __tablename__ = "scalper_config"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    enabled = Column(Boolean, default=False)
    mode = Column(String(10), default="PAPER")
    live_confirmed = Column(Boolean, default=False)
    order_usdt = Column(Float, default=5.0)
    min_order_usdt = Column(Float, default=2.0)
    max_positions = Column(Integer, default=3)
    tp_pct = Column(Float, default=0.35)
    sl_pct = Column(Float, default=0.20)
    max_hold_sec = Column(Integer, default=60)
    daily_loss_usdt = Column(Float, default=2.0)
    max_consecutive_losses = Column(Integer, default=5)
    cooldown_sec = Column(Float, default=3.0)
    max_spread_pct = Column(Float, default=0.12)
    min_imbalance = Column(Float, default=0.60)
    min_5s_momentum_pct = Column(Float, default=0.08)
    min_15s_momentum_pct = Column(Float, default=0.03)
    min_30s_momentum_pct = Column(Float, default=0.15)
    btc_filter_enabled = Column(Boolean, default=True)
    btc_5s_min_pct = Column(Float, default=0.015)
    btc_30s_min_pct = Column(Float, default=0.03)
    btc_60s_min_pct = Column(Float, default=0.02)
    auto_coins = Column(Boolean, default=True)
    watchlist_size = Column(Integer, default=14)
    min_24h_volume_usdt = Column(Float, default=5000000.0)
    manual_coins = Column(Text, default="")
    rerank_sec = Column(Integer, default=15)
    entry_timeout_sec = Column(Float, default=0.8)
    updated_at = Column(Float, default=0.0)

DEFAULTS = dict(
    enabled=False, mode="PAPER", live_confirmed=False,
    order_usdt=5.0, min_order_usdt=2.0, max_positions=3,
    tp_pct=0.35, sl_pct=0.20, max_hold_sec=60,
    daily_loss_usdt=2.0, max_consecutive_losses=5, cooldown_sec=3.0,
    max_spread_pct=0.12, min_imbalance=0.60,
    min_5s_momentum_pct=0.08, min_15s_momentum_pct=0.03, min_30s_momentum_pct=0.15,
    btc_filter_enabled=True, btc_5s_min_pct=0.015, btc_30s_min_pct=0.03, btc_60s_min_pct=0.02,
    auto_coins=True, watchlist_size=14, min_24h_volume_usdt=5000000.0,
    manual_coins="", rerank_sec=15, entry_timeout_sec=0.8
)

def get_settings(tid):
    db = SessionLocal()
    try:
        row = db.query(ScalperConfig).filter_by(telegram_id=int(tid)).first()
        if not row:
            row = ScalperConfig(telegram_id=int(tid), **DEFAULTS)
            db.add(row); db.commit(); db.refresh(row)
        return row
    finally:
        db.close()

def update_settings(tid, **values):
    db = SessionLocal()
    try:
        row = db.query(ScalperConfig).filter_by(telegram_id=int(tid)).first()
        if not row:
            row = ScalperConfig(telegram_id=int(tid), **DEFAULTS)
            db.add(row)
        for k,v in values.items():
            if hasattr(row,k) and v is not None:
                setattr(row,k,v)
        row.updated_at=time.time()
        db.commit(); db.refresh(row)
        return row
    finally:
        db.close()

def _coins(row):
    return [x.strip().upper().replace("/","") for x in (row.manual_coins or "").split(",") if x.strip()]

@dataclass
class Quote:
    bid: float=0.0; ask: float=0.0; bid_qty: float=0.0; ask_qty: float=0.0; ts: float=0.0
    @property
    def mid(self): return (self.bid+self.ask)/2 if self.bid>0 and self.ask>0 else 0
    @property
    def spread_pct(self): return ((self.ask-self.bid)/self.mid*100) if self.mid else 999
    @property
    def imbalance(self):
        t=self.bid_qty+self.ask_qty
        return self.bid_qty/t if t else .5

@dataclass
class Position:
    symbol:str; entry:float; amount:float; cost:float; opened_at:float; paper:bool; order_id:str=""

class Feed:
    URL="wss://wbs-api.mexc.com/ws"
    def __init__(self):
        self.symbols=[]; self.quotes={}; self.prices=defaultdict(lambda:deque(maxlen=600))
        self.lock=threading.RLock(); self.stop_event=threading.Event(); self.thread=None; self.ws=None
    def set_symbols(self,symbols):
        clean=list(dict.fromkeys(x.upper().replace("/","") for x in symbols))
        changed=clean!=self.symbols; self.symbols=clean
        if changed and self.thread and self.thread.is_alive(): self.restart()
    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear(); self.thread=threading.Thread(target=self._run,daemon=True); self.thread.start()
    def restart(self):
        self.stop()
        self.stop_event.clear()
        self.thread=threading.Thread(target=self._run,daemon=True); self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.ws:
            try:self.ws.close()
            except:pass
    def _run(self):
        while not self.stop_event.is_set():
            try:
                streams=[f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{s}" for s in self.symbols[:30]]
                self.ws=websocket.WebSocketApp(
                    self.URL,
                    on_open=lambda w:w.send(json.dumps({"method":"SUBSCRIPTION","params":streams})),
                    on_message=self._msg,
                    on_error=lambda w,e:log.warning("scalper ws: %s",e))
                self.ws.run_forever(ping_interval=20,ping_timeout=10)
            except Exception: log.exception("scalper ws loop")
            finally:self.ws=None
            if not self.stop_event.is_set():time.sleep(2)
    def _msg(self,_ws,raw):
        try:
            m=json.loads(raw); sym=str(m.get("symbol") or "").upper()
            if not sym or "bookTicker" not in str(m.get("channel") or ""):return
            x=m.get("publicbookticker") or m.get("publicAggreBookTicker") or {}
            b=float(x.get("bidprice") or x.get("bidPrice") or 0)
            a=float(x.get("askprice") or x.get("askPrice") or 0)
            bq=float(x.get("bidquantity") or x.get("bidQuantity") or 0)
            aq=float(x.get("askquantity") or x.get("askQuantity") or 0)
            if b<=0 or a<=0:return
            now=time.time()
            with self.lock:
                self.quotes[sym]=Quote(b,a,bq,aq,now); self.prices[sym].append((now,(a+b)/2))
        except Exception: pass
    def quote(self,s):
        with self.lock:return self.quotes.get(s.upper().replace("/",""))
    def ret(self,s,seconds):
        with self.lock:data=list(self.prices[s.upper().replace("/","")])
        if len(data)<2:return 0
        cutoff=time.time()-seconds
        old=next((p for t,p in data if t>=cutoff),data[0][1]); last=data[-1][1]
        return (last/old-1)*100 if old else 0

class FastScalper:
    def __init__(self,tid):
        self.tid=int(tid); self.feed=Feed(); self.positions={}; self.symbols=[]
        self.realized=0.0; self.trades=self.wins=self.losses=self.consecutive_losses=0
        self.last_trade=0; self.last_rank=0; self.last_error=""
        self.thread=None; self.stop_event=threading.Event()
        self.exchange=ccxt.mexc({"apiKey":config.MEXC_API_KEY,"secret":config.MEXC_API_SECRET,
                                 "enableRateLimit":True,"options":{"defaultType":"spot","recvWindow":10000}})
    def s(self): return get_settings(self.tid)
    def start(self):
        s=self.s()
        if s.mode=="LIVE" and not s.live_confirmed: raise RuntimeError("التداول الحقيقي غير مؤكد من تليجرام.")
        self.symbols=self._select(s); self.feed.set_symbols(self.symbols); self.feed.start()
        self.stop_event.clear()
        update_settings(self.tid,enabled=True)
        if not self.thread or not self.thread.is_alive():
            self.thread=threading.Thread(target=self._loop,daemon=True); self.thread.start()
    def stop(self):
        self.stop_event.set(); self.feed.stop(); update_settings(self.tid,enabled=False)
    def _select(self,s):
        manual=_coins(s)
        if not s.auto_coins and manual:return manual[:30]
        try:
            markets=self.exchange.load_markets(); ticks=self.exchange.fetch_tickers(); arr=[]
            for pair,t in ticks.items():
                m=markets.get(pair) or {}
                if m.get("spot") and m.get("active") and m.get("quote")=="USDT" and m.get("base") not in {"USDT","USDC","USDE","FDUSD","DAI"}:
                    qv=float(t.get("quoteVolume") or 0)
                    if qv>=s.min_24h_volume_usdt:arr.append((qv,m["symbol"].replace("/","")))
            arr.sort(reverse=True); out=[x[1] for x in arr[:s.watchlist_size]]
            return list(dict.fromkeys(manual+out))[:30] if manual else out[:30]
        except Exception as e:
            self.last_error=str(e)[:200]; return manual[:30] if manual else ["BTCUSDT"]
    def _loop(self):
        while not self.stop_event.is_set():
            try:
                s=self.s()
                if time.time()-self.last_rank>=max(5,s.rerank_sec):
                    new=self._select(s)
                    if new!=self.symbols:self.symbols=new;self.feed.set_symbols(new)
                    self.last_rank=time.time()
                self._manage(s)
                if not self._locked(s):self._scan(s)
            except Exception as e:self.last_error=str(e)[:200]
            time.sleep(.1)
    def _locked(self,s):
        return self.realized<=-s.daily_loss_usdt or self.consecutive_losses>=s.max_consecutive_losses or len(self.positions)>=s.max_positions or time.time()-self.last_trade<s.cooldown_sec
    def _btc_ok(self,s):
        if not s.btc_filter_enabled:return True
        q=self.feed.quote("BTCUSDT")
        return bool(q and time.time()-q.ts<3 and self.feed.ret("BTCUSDT",5)>=s.btc_5s_min_pct and self.feed.ret("BTCUSDT",30)>=s.btc_30s_min_pct and self.feed.ret("BTCUSDT",60)>=s.btc_60s_min_pct)
    def _signal(self,sym,s):
        if sym=="BTCUSDT":return False
        q=self.feed.quote(sym)
        return bool(q and time.time()-q.ts<2 and q.spread_pct<=s.max_spread_pct and q.imbalance>=s.min_imbalance and self.feed.ret(sym,5)>=s.min_5s_momentum_pct and self.feed.ret(sym,15)>=s.min_15s_momentum_pct and self.feed.ret(sym,30)>=s.min_30s_momentum_pct)
    def _scan(self,s):
        if not self._btc_ok(s):return
        for sym in self.symbols:
            if sym in self.positions:continue
            if self._signal(sym,s):
                try:self._enter(sym,s)
                except Exception as e:self.last_error=f"{sym}: {e}"[:200]
                if len(self.positions)>=s.max_positions:break
    def _pair(self,s):return s[:-4]+"/USDT" if s.endswith("USDT") else s
    def _enter(self,sym,s):
        q=self.feed.quote(sym)
        if not q:return
        pair=self._pair(sym); cost=max(s.order_usdt,s.min_order_usdt); amount=float(self.exchange.amount_to_precision(pair,cost/q.ask))
        now=time.time()
        if s.mode=="PAPER":
            self.positions[sym]=Position(sym,q.ask,amount,amount*q.ask,now,True);self.last_trade=now;return
        price=float(self.exchange.price_to_precision(pair,q.bid))
        order=self.exchange.create_order(pair,"limit","buy",amount,price,params={"postOnly":True})
        oid=str(order.get("id") or ""); deadline=time.time()+s.entry_timeout_sec; filled=None
        while time.time()<deadline:
            time.sleep(.1);filled=self.exchange.fetch_order(oid,pair)
            if str(filled.get("status") or "").lower() in {"closed","filled"}:break
        if str((filled or {}).get("status") or "").lower() not in {"closed","filled"}:
            try:self.exchange.cancel_order(oid,pair)
            except:pass
            return
        avg=float(filled.get("average") or price); amt=float(filled.get("filled") or amount)
        self.positions[sym]=Position(sym,avg,amt,avg*amt,now,False,oid);self.last_trade=now
    def _manage(self,s):
        for sym,p in list(self.positions.items()):
            q=self.feed.quote(sym)
            if not q:continue
            px=q.bid
            if px>=p.entry*(1+s.tp_pct/100):self._exit(p,px)
            elif px<=p.entry*(1-s.sl_pct/100):self._exit(p,px)
            elif time.time()-p.opened_at>=s.max_hold_sec:self._exit(p,px)
    def _exit(self,p,px):
        try:
            if not p.paper:
                pair=self._pair(p.symbol);o=self.exchange.create_order(pair,"market","sell",p.amount);px=float(o.get("average") or o.get("price") or px)
            pnl=(px-p.entry)*p.amount;self.realized+=pnl;self.trades+=1
            if pnl>=0:self.wins+=1;self.consecutive_losses=0
            else:self.losses+=1;self.consecutive_losses+=1
            self.positions.pop(p.symbol,None);self.last_trade=time.time()
        except Exception as e:self.last_error=str(e)[:200]
    def status(self):
        s=self.s()
        return {"enabled":bool(s.enabled),"mode":s.mode,"live_confirmed":bool(s.live_confirmed),
                "symbols":len(self.symbols),"symbol_list":self.symbols[:30],"open_positions":len(self.positions),
                "positions":[{"symbol":p.symbol,"entry":p.entry,"cost":p.cost,"age":int(time.time()-p.opened_at),"paper":p.paper} for p in self.positions.values()],
                "trades":self.trades,"wins":self.wins,"losses":self.losses,"realized":self.realized,
                "consecutive_losses":self.consecutive_losses,"last_error":self.last_error}

_instances={}
def get_scalper(tid):
    tid=int(tid)
    if tid not in _instances:_instances[tid]=FastScalper(tid)
    return _instances[tid]

# Backward compatibility with the existing bot's import.
class _Compat:
    def __init__(self,tid=0): self.tid=tid
    def _x(self): return get_scalper(self.tid or getattr(config,"ADMIN_TELEGRAM_ID",0))
    def start(self): return self._x().start()
    def stop(self): return self._x().stop()
    def status(self): return self._x().status()
SCALPER=_Compat()
