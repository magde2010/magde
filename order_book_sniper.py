"""MEXC Spot order-book liquidity strategy. Starts in PAPER and never enables LIVE implicitly."""
from __future__ import annotations
import logging, time
from collections import defaultdict
from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean
import config
from database import Base, SessionLocal

log = logging.getLogger("lotfe.order_book_sniper")

class OrderBookSettings(Base):
    __tablename__ = "order_book_sniper_settings"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    mode = Column(String(8), default="PAPER", nullable=False)
    live_confirmed = Column(Boolean, default=False, nullable=False)
    order_usdt = Column(Float, default=5.0, nullable=False)
    min_order_usdt = Column(Float, default=2.0, nullable=False)
    max_positions = Column(Integer, default=2, nullable=False)
    tp_pct = Column(Float, default=0.35, nullable=False)
    sl_pct = Column(Float, default=0.25, nullable=False)
    min_24h_volume_usdt = Column(Float, default=1000000.0, nullable=False)
    max_spread_pct = Column(Float, default=0.15, nullable=False)
    wall_multiple = Column(Float, default=3.0, nullable=False)
    max_wall_distance_pct = Column(Float, default=0.45, nullable=False)
    scan_symbols = Column(Integer, default=12, nullable=False)

def get_settings(tid):
    db=SessionLocal()
    try:
        s=db.query(OrderBookSettings).filter_by(telegram_id=int(tid)).first()
        if not s:
            s=OrderBookSettings(telegram_id=int(tid)); db.add(s); db.commit(); db.refresh(s)
        return s
    finally: db.close()

def update_settings(tid, **kw):
    db=SessionLocal()
    try:
        s=db.query(OrderBookSettings).filter_by(telegram_id=int(tid)).first()
        if not s: s=OrderBookSettings(telegram_id=int(tid)); db.add(s)
        for k,v in kw.items():
            if hasattr(s,k): setattr(s,k,v)
        db.commit(); db.refresh(s); return s
    finally: db.close()

class OrderBookSniper:
    def __init__(self, tid):
        self.tid=int(tid); self.enabled=False; self.positions={}; self.realized=0.0
        self.last_error=""; self.last_scan=0; self.scanned=0; self.candidates=[]
        self.exchange=None; self.last_wall_seen=defaultdict(int); self.last_wall_price={}
        self._market_cache_at=0.0; self._ranked_cache=[]; self._settings_cache=None
        self._last_manage=0

    def start(self, mexc_client):
        s=get_settings(self.tid)
        if s.mode=="LIVE" and not s.live_confirmed:
            raise RuntimeError("التداول الحقيقي غير مؤكد. فعّل تأكيد LIVE من تيليجرام أولًا.")
        self.exchange=getattr(mexc_client,"exchange",mexc_client)
        self.enabled=True
    def stop(self): self.enabled=False
    def _markets(self):
        ex=self.exchange
        if time.time()-self._market_cache_at < 60 and self._ranked_cache:
            return self._ranked_cache[:get_settings(self.tid).scan_symbols]
        markets=ex.load_markets(); tickers=ex.fetch_tickers()
        ranked=[]
        settings=get_settings(self.tid)
        for sym,m in markets.items():
            if not m.get("spot") or not m.get("active") or m.get("quote")!="USDT": continue
            t=tickers.get(sym) or {}; qv=float(t.get("quoteVolume") or 0); last=float(t.get("last") or 0)
            if last>0 and qv>=settings.min_24h_volume_usdt: ranked.append((qv,sym))
        ranked.sort(reverse=True)
        self._ranked_cache=[s for _,s in ranked[:max(1,settings.scan_symbols)]]
        self._market_cache_at=time.time()
        return self._ranked_cache
    @staticmethod
    def _wall(levels, multiple):
        # A wall must materially exceed nearby levels, not just be the largest of three tiny orders.
        out=[]
        for i,(price,amount,*_) in enumerate(levels):
            notion=float(price)*float(amount)
            neighbors=[float(p)*float(a) for p,a,*_ in levels[max(0,i-2):i]+levels[i+1:i+3]]
            if len(neighbors)>=2:
                med=sorted(neighbors)[len(neighbors)//2]
                if med>0 and notion>=med*multiple: out.append((float(price),notion,notion/med))
        return out
    def _scan_one(self,sym,s):
        ex=self.exchange; ob=ex.fetch_order_book(sym,limit=20)
        bids=ob.get("bids") or []; asks=ob.get("asks") or []
        if not bids or not asks:return
        bid,ask=float(bids[0][0]),float(asks[0][0])
        if bid<=0 or ask<=bid:return
        spread=(ask-bid)/((ask+bid)/2)*100
        if spread>s.max_spread_pct:return
        bw=self._wall(bids,s.wall_multiple); aw=self._wall(asks,s.wall_multiple)
        mid=(bid+ask)/2
        near_bids=[x for x in bw if 0 < (mid-x[0])/mid*100 <= s.max_wall_distance_pct]
        near_asks=[x for x in aw if 0 < (x[0]-mid)/mid*100 <= s.max_wall_distance_pct]
        if near_bids:
            wall=max(near_bids,key=lambda x:x[1]); key=(sym,"B",round(wall[0],12))
            self.last_wall_seen[key]+=1
            # Require persistence over multiple observations to reduce transient spoofing.
            if self.last_wall_seen[key]>=2 and sym not in self.positions and len(self.positions)<s.max_positions:
                self._buy(sym,bid,ask,wall[0],s)
        # Ask walls are used as a target/exit signal for existing positions, not naked short sells on Spot.
        p=self.positions.get(sym)
        if p and near_asks:
            closest=min(near_asks,key=lambda x:x[0])
            if closest[0] > p["entry"]: p["wall_target"]=closest[0]
    def _buy(self,sym,bid,ask,wall,s):
        ex=self.exchange; market=ex.market(sym); tick=((market.get("precision") or {}).get("price"))
        # Aim one tick above the bid wall to improve queue priority, without crossing the spread.
        price=max(wall,bid)
        try:
            if tick is not None and isinstance(tick,(int,float)) and tick>0: price=max(wall,bid)+float(tick)
            price=min(price,ask)
            price=float(ex.price_to_precision(sym,price))
            if price>=ask:return
            cost=max(float(s.order_usdt),float(s.min_order_usdt))
            amount=float(ex.amount_to_precision(sym,cost/price))
            if amount*price < max(2.0,float(s.min_order_usdt)):return
            if s.mode=="PAPER":
                self.positions[sym]={"entry":price,"amount":amount,"cost":price*amount,"opened":time.time(),"paper":True,"wall":wall}
            else:
                order=ex.create_order(sym,"limit","buy",amount,price,{"timeInForce":"GTC"})
                oid=str(order.get("id") or "")
                # Only manage a position after a confirmed fill; do not mistake a resting order for inventory.
                checked=ex.fetch_order(oid,sym) if oid else order
                status=str((checked or {}).get("status") or "").lower()
                filled=float((checked or {}).get("filled") or 0)
                if status not in ("closed","filled") and oid:
                    try: ex.cancel_order(oid,sym)
                    except Exception: pass
                if filled>0:
                    # Preserve any partial fill as inventory so it is still protected by TP/SL.
                    avg=float((checked or {}).get("average") or price)
                    self.positions[sym]={"entry":avg,"amount":filled,"cost":avg*filled,"opened":time.time(),"paper":False,"wall":wall,"order_id":oid}
        except Exception as e: self.last_error=f"{sym}: {e}"[:180]
    def _manage(self):
        if not self.exchange:return
        s=get_settings(self.tid)
        for sym,p in list(self.positions.items()):
            try:
                ticker=self.exchange.fetch_ticker(sym); px=float(ticker.get("bid") or ticker.get("last") or 0)
                if not px:continue
                target=p["entry"]*(1+s.tp_pct/100); stop=p["entry"]*(1-s.sl_pct/100)
                wall_target=p.get("wall_target")
                if px>=target or (wall_target and px>=wall_target) or px<=stop:
                    if not p["paper"]:
                        self.exchange.create_order(sym,"market","sell",float(p["amount"]))
                    self.realized+=(px-p["entry"])*p["amount"]; self.positions.pop(sym,None)
            except Exception as e: self.last_error=f"manage {sym}: {e}"[:180]
    def scan_once(self):
        if not self.enabled or not self.exchange:return
        s=get_settings(self.tid); started=time.time()
        try:
            self._manage()
            symbols=self._markets(); self.scanned=0; self.candidates=[]
            for sym in symbols:
                try:
                    self._scan_one(sym,s); self.scanned+=1
                except Exception as e: self.last_error=f"{sym}: {e}"[:180]
            self.candidates=symbols; self.last_scan=time.time()-started
        except Exception as e:self.last_error=str(e)[:180]
    def status(self):
        s=get_settings(self.tid)
        return {"enabled":self.enabled,"mode":s.mode,"positions":self.positions,"scanned":self.scanned,
                "scan_sec":self.last_scan,"realized":self.realized,"last_error":self.last_error,"symbols":self.candidates}

_instances={}
def get_sniper(tid):
    tid=int(tid)
    if tid not in _instances:_instances[tid]=OrderBookSniper(tid)
    return _instances[tid]
