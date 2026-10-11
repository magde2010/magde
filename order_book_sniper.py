"""Manual-symbol MEXC Spot order-book strategy. PAPER by default; LIVE is explicitly armed."""
from __future__ import annotations
import json, logging, time
from collections import defaultdict
from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, text, inspect
from sqlalchemy.exc import OperationalError
from database import Base, SessionLocal

log = logging.getLogger("lotfe.order_book_sniper")

class OrderBookSettings(Base):
    __tablename__ = "order_book_sniper_settings"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    mode = Column(String(8), default="PAPER", nullable=False)
    live_confirmed = Column(Boolean, default=False, nullable=False)
    order_usdt = Column(Float, default=5.0, nullable=False)  # legacy/global fallback
    min_order_usdt = Column(Float, default=2.0, nullable=False)
    max_positions = Column(Integer, default=20, nullable=False)
    tp_pct = Column(Float, default=0.35, nullable=False)
    sl_pct = Column(Float, default=0.25, nullable=False)
    min_24h_volume_usdt = Column(Float, default=1000000.0, nullable=False)  # legacy only
    # 1.2% accommodates thin/small-cap pairs while still avoiding extreme gaps.
    max_spread_pct = Column(Float, default=1.2, nullable=False)
    wall_multiple = Column(Float, default=3.0, nullable=False)
    max_wall_distance_pct = Column(Float, default=1.0, nullable=False)
    scan_symbols = Column(Integer, default=12, nullable=False)  # legacy only
    watch_symbols = Column(String(1000), default="", nullable=False)
    symbol_budgets = Column(String(4000), default="{}", nullable=False)
    running_symbols = Column(String(1000), default="", nullable=False)
    runtime_state = Column(String(12000), default="{}", nullable=False)


def ensure_order_book_schema():
    """Add strategy-only settings without changing portfolio tables."""
    from database import engine
    Base.metadata.create_all(bind=engine, tables=[OrderBookSettings.__table__])
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("order_book_sniper_settings")}
    additions = {
        "watch_symbols": "VARCHAR(1000) NOT NULL DEFAULT ''",
        "symbol_budgets": "VARCHAR(4000) NOT NULL DEFAULT '{}'",
        "running_symbols": "VARCHAR(1000) NOT NULL DEFAULT ''",
        "runtime_state": "VARCHAR(12000) NOT NULL DEFAULT '{}'",
    }
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in cols:
                conn.execute(text(f"ALTER TABLE order_book_sniper_settings ADD COLUMN {name} {ddl}"))


def get_settings(tid):
    db=SessionLocal()
    try:
        s=db.query(OrderBookSettings).filter_by(telegram_id=int(tid)).first()
        if not s:
            s=OrderBookSettings(telegram_id=int(tid)); db.add(s); db.commit(); db.refresh(s)
        # Migrate the old overly strict defaults for existing users once.
        if float(s.max_spread_pct or 0) == 0.15:
            s.max_spread_pct=1.2
            if float(s.max_wall_distance_pct or 0) == 0.45: s.max_wall_distance_pct=1.0
            db.commit(); db.refresh(s)
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


def _csv(value):
    return [x.strip().upper() for x in (value or "").split(",") if x.strip()]


def get_symbol_budgets(tid):
    raw=getattr(get_settings(tid), "symbol_budgets", "{}") or "{}"
    try:
        data=json.loads(raw)
        return {str(k).upper():float(v) for k,v in data.items() if float(v)>0}
    except Exception:
        return {}


def set_symbol_budget(tid, symbol, budget):
    budgets=get_symbol_budgets(tid); budgets[str(symbol).upper()]=float(budget)
    update_settings(tid, symbol_budgets=json.dumps(budgets, separators=(",",":")))
    return budgets


class OrderBookSniper:
    def __init__(self, tid):
        self.tid=int(tid); self.enabled=False; self.positions={}; self.entry_orders={}; self.realized=0.0
        self.last_error=""; self.last_scan=0; self.scanned=0; self.candidates=[]
        self.exchange=None; self.last_wall_seen=defaultdict(int); self.last_wall_price={}
        self.wall_status={}
        self._last_manage=0
        try:
            state=json.loads(getattr(get_settings(self.tid), "runtime_state", "{}") or "{}")
            self.positions=state.get("positions", {}) or {}
            self.entry_orders=state.get("entry_orders", {}) or {}
            self.realized=float(state.get("realized", 0.0) or 0.0)
            self.wall_status=state.get("wall_status", {}) or {}
        except Exception:
            self.positions={}; self.entry_orders={}; self.realized=0.0; self.wall_status={}

    def _save_runtime(self):
        state={"positions":self.positions,"entry_orders":self.entry_orders,"realized":self.realized,"wall_status":self.wall_status}
        update_settings(self.tid, runtime_state=json.dumps(state,separators=(",",":")))

    def start(self, mexc_client):
        s=get_settings(self.tid)
        if s.mode=="LIVE" and not s.live_confirmed:
            raise RuntimeError("التداول الحقيقي غير مؤكد. فعّل تأكيد LIVE من تيليجرام أولًا.")
        self.exchange=getattr(mexc_client,"exchange",mexc_client)
        configured=_csv(s.watch_symbols)
        if not configured:
            raise RuntimeError("أضف زوجًا وحدد ميزانيته أولًا من زر إضافة عملة.")
        budgets=get_symbol_budgets(self.tid)
        configured=[sym for sym in configured if budgets.get(sym,0)>=max(2.0,float(s.min_order_usdt))]
        if not configured:
            raise RuntimeError("لا توجد عملات بميزانية صالحة. أضف العملة وحدد ميزانيتها من تيليجرام.")
        markets=self.exchange.load_markets()
        configured=[sym for sym in configured if sym in markets and markets[sym].get("spot") and markets[sym].get("active") and markets[sym].get("quote")=="USDT"]
        if not configured:
            raise RuntimeError("لا توجد أزواج مختارة صالحة في MEXC Spot/USDT.")
        update_settings(self.tid, enabled=True, running_symbols=",".join(configured))
        self.enabled=True

    def start_symbol(self, symbol, mexc_client):
        s=get_settings(self.tid)
        if s.mode=="LIVE" and not s.live_confirmed:
            raise RuntimeError("التداول الحقيقي غير مؤكد. فعّل تأكيد LIVE من تيليجرام أولًا.")
        symbol=str(symbol).strip().upper()
        if symbol not in _csv(s.watch_symbols):
            raise RuntimeError("أضف الزوج وحدد ميزانيته أولًا.")
        budget=get_symbol_budgets(self.tid).get(symbol,0)
        if budget < max(2.0,float(s.min_order_usdt)):
            raise RuntimeError("ميزانية الزوج أقل من الحد الأدنى للأمر.")
        ex=getattr(mexc_client,"exchange",mexc_client)
        markets=ex.load_markets()
        market=markets.get(symbol)
        if not market or not market.get("spot") or not market.get("active") or market.get("quote")!="USDT":
            raise RuntimeError("الزوج غير متاح في MEXC Spot/USDT.")
        running=_csv(s.running_symbols)
        if symbol not in running: running.append(symbol)
        update_settings(self.tid, enabled=True, running_symbols=",".join(running))
        self.exchange=ex; self.enabled=True

    def stop_symbol(self, symbol):
        symbol=symbol.upper()
        pending=self.entry_orders.get(symbol)
        if pending and self.exchange:
            if pending.get("paper"):
                self.entry_orders.pop(symbol,None); self._save_runtime()
            else:
                try:
                    self.exchange.cancel_order(pending["id"],symbol)
                    self.entry_orders.pop(symbol,None); self._save_runtime()
                except Exception as e:
                    self.last_error=f"تعذر إلغاء أمر الدخول عند إيقاف {symbol}: {str(e)[:100]}"
        s=get_settings(self.tid); running=[x for x in _csv(s.running_symbols) if x!=symbol]
        update_settings(self.tid, running_symbols=",".join(running), enabled=bool(running))
        self.enabled=bool(running)

    def stop(self):
        # Stop new entries and cancel this strategy's resting entry orders, but keep managing filled positions.
        for sym, pending in list(self.entry_orders.items()):
            if not self.exchange: break
            if pending.get("paper"):
                self.entry_orders.pop(sym,None); self._save_runtime()
            else:
                try:
                    self.exchange.cancel_order(pending["id"],sym)
                    self.entry_orders.pop(sym,None); self._save_runtime()
                except Exception as e:
                    self.last_error=f"تعذر إلغاء أمر الدخول {sym} عند إيقاف الكل: {str(e)[:100]}"
        update_settings(self.tid, enabled=False, running_symbols="")
        self.enabled=False

    @staticmethod
    def _wall(levels, multiple):
        out=[]
        for i,(price,amount,*_) in enumerate(levels):
            notion=float(price)*float(amount)
            neighbors=[float(p)*float(a) for p,a,*_ in levels[max(0,i-2):i]+levels[i+1:i+3]]
            if len(neighbors)>=2:
                med=sorted(neighbors)[len(neighbors)//2]
                if med>0 and notion>=med*multiple: out.append((float(price),notion,notion/med))
        return out

    def _active_symbols_count(self):
        return len(set(self.positions) | set(self.entry_orders))

    def _scan_one(self,sym,s):
        ex=self.exchange
        if sym in self.entry_orders:
            entry=self.entry_orders[sym]
            # Keep the displayed market price live while the entry order is resting.
            try:
                book=ex.fetch_order_book(sym,limit=5)
                bids=book.get("bids") or []; asks=book.get("asks") or []
                bid=float(bids[0][0]) if bids else entry.get("bid")
                ask=float(asks[0][0]) if asks else entry.get("ask")
                spread=((ask-bid)/((ask+bid)/2)*100) if bid and ask and ask>bid else entry.get("spread_pct")
            except Exception:
                bid,ask,spread=entry.get("bid"),entry.get("ask"),entry.get("spread_pct")
            self.wall_status[sym]={"state":"entry_pending","bid":bid,"ask":ask,"spread_pct":spread,"wall_price":entry.get("wall"),"wall_usdt":entry.get("wall_usdt"),"wall_multiple":entry.get("wall_multiple"),"entry_wall_price":entry.get("wall"),"entry_wall_usdt":entry.get("wall_usdt"),"entry_wall_multiple":entry.get("wall_multiple"),"order_price":entry.get("price"),"budget":entry.get("budget"),"paper":bool(entry.get("paper")),"reason":"انتظار تنفيذ أمر الدخول عند الجدار المحدد","updated":time.time()}
            self._save_runtime()
            return
        ob=ex.fetch_order_book(sym,limit=30)
        bids=ob.get("bids") or []; asks=ob.get("asks") or []
        if not bids or not asks:
            self.wall_status[sym]={"state":"no_book","reason":"دفتر الأوامر فارغ","updated":time.time()}; self._save_runtime(); return
        bid,ask=float(bids[0][0]),float(asks[0][0])
        if bid<=0 or ask<=bid:
            self.wall_status[sym]={"state":"bad_book","bid":bid,"ask":ask,"reason":"أسعار دفتر الأوامر غير صالحة","updated":time.time()}; self._save_runtime(); return
        spread=(ask-bid)/((ask+bid)/2)*100
        if spread>s.max_spread_pct:
            self.wall_status[sym]={"state":"spread_rejected","bid":bid,"ask":ask,"spread_pct":spread,"reason":f"فرق السعر {spread:.4f}% أكبر من الحد {s.max_spread_pct:g}%","updated":time.time()}; self._save_runtime(); return
        bw=self._wall(bids,s.wall_multiple); aw=self._wall(asks,s.wall_multiple)
        mid=(bid+ask)/2
        near_bids=[x for x in bw if 0 < (mid-x[0])/mid*100 <= s.max_wall_distance_pct]
        near_asks=[x for x in aw if 0 < (x[0]-mid)/mid*100 <= s.max_wall_distance_pct]
        self.wall_status[sym]={"state":"watching","bid":bid,"ask":ask,"spread_pct":spread,"wall_price":None,"wall_usdt":None,"wall_multiple":None,"confirmations":0,"reason":"لم يتم العثور على جدار شراء قريب مستوفٍ للشروط","updated":time.time()}
        if near_bids:
            wall=max(near_bids,key=lambda x:x[1]); key=(sym,"B",round(wall[0],12))
            self.wall_status[sym].update({"wall_price":wall[0],"wall_usdt":wall[1],"wall_multiple":wall[2],"reason":"تم رصد جدار شراء، بانتظار تأكيد ظهوره في قراءة ثانية"})
            # A changed wall price resets persistence. Two separate snapshots are required.
            previous=self.last_wall_price.get(sym)
            if previous is not None and abs(previous-wall[0]) > max(wall[0]*0.0002, 1e-12):
                self.last_wall_seen[key]=0
            self.last_wall_price[sym]=wall[0]
            self.last_wall_seen[key]+=1
            self.wall_status[sym]["confirmations"]=self.last_wall_seen[key]
            if self.last_wall_seen[key]>=2 and sym not in self.positions and self._active_symbols_count() < min(20,max(1,int(s.max_positions))):
                self._buy(sym,bid,ask,wall[0],s,wall_usdt=wall[1],wall_multiple=wall[2],spread_pct=spread)
            elif self._active_symbols_count() >= min(20,max(1,int(s.max_positions))):
                self.wall_status[sym].update({"state":"capacity_full","reason":"تم بلوغ الحد الأقصى للمراكز/أوامر الدخول"})
        else:
            for old_key in [k for k in self.last_wall_seen if k[0]==sym]:
                self.last_wall_seen[old_key]=0
        if not near_bids:
            self._save_runtime()
        p=self.positions.get(sym)
        if p:
            sell_wall=min((x for x in near_asks if x[0]>p["entry"]),key=lambda x:x[0],default=None)
            if sell_wall:
                p["wall_target"]=sell_wall[0]
                self.wall_status[sym].update({"state":"position_open","entry_price":p.get("entry"),"sell_wall_price":sell_wall[0],"sell_wall_usdt":sell_wall[1],"sell_wall_multiple":sell_wall[2],"wall_target":sell_wall[0],"reason":"مركز مفتوح؛ أفضل جدار بيع قريب مرصود، والهدف عنده","updated":time.time()})
            else:
                p.pop("wall_target",None)
                self.wall_status[sym].update({"state":"position_open","entry_price":p.get("entry"),"sell_wall_price":None,"sell_wall_usdt":None,"sell_wall_multiple":None,"wall_target":None,"reason":"مركز مفتوح؛ لا يوجد جدار بيع مؤهل قريب حاليًا","updated":time.time()})
            self._save_runtime()

    def _buy(self,sym,bid,ask,wall,s,wall_usdt=0.0,wall_multiple=0.0,spread_pct=0.0):
        ex=self.exchange
        try:
            budgets=get_symbol_budgets(self.tid)
            cost=float(budgets.get(sym,0))
            if cost < max(2.0,float(s.min_order_usdt)):
                self.last_error=f"{sym}: ميزانية العملة أقل من الحد الأدنى"; return
            price=max(wall,bid)
            market=ex.market(sym); precision=((market.get("precision") or {}).get("price"))
            # CCXT precision can be decimal places or tick size depending on exchange mode.
            if isinstance(precision,int) and precision>=0:
                tick=10**(-precision); price=max(wall,bid)+tick
            elif isinstance(precision,float) and 0<precision<1:
                price=max(wall,bid)+precision
            price=min(price,ask)
            price=float(ex.price_to_precision(sym,price))
            if price>=ask:return
            amount=float(ex.amount_to_precision(sym,cost/price))
            if amount<=0 or amount*price+1e-9<max(2.0,float(s.min_order_usdt)):
                self.last_error=f"{sym}: الكمية بعد التقريب أقل من الحد الأدنى"; return
            if s.mode=="PAPER":
                # Simulate a resting limit order; do not pretend it filled at submission time.
                oid="PAPER-"+str(int(time.time()*1000))
                self.entry_orders[sym]={"id":oid,"price":price,"amount":amount,"filled":0.0,"cost":0.0,"wall":wall,"wall_usdt":wall_usdt,"wall_multiple":wall_multiple,"bid":bid,"ask":ask,"spread_pct":spread_pct,"created":time.time(),"budget":cost,"paper":True}
            else:
                order=ex.create_order(sym,"limit","buy",amount,price,{"timeInForce":"GTC"})
                oid=str(order.get("id") or "")
                if not oid: raise RuntimeError("MEXC لم يُرجع رقمًا لأمر الدخول")
                self.entry_orders[sym]={"id":oid,"price":price,"amount":amount,"filled":0.0,"cost":0.0,"wall":wall,"wall_usdt":wall_usdt,"wall_multiple":wall_multiple,"bid":bid,"ask":ask,"spread_pct":spread_pct,"created":time.time(),"budget":cost,"paper":False}
            self.wall_status[sym]={"state":"entry_pending","bid":bid,"ask":ask,"spread_pct":spread_pct,"wall_price":wall,"wall_usdt":wall_usdt,"wall_multiple":wall_multiple,"confirmations":self.last_wall_seen.get((sym,"B",round(wall,12)),2),"order_price":price,"budget":cost,"paper":s.mode=="PAPER","reason":"أمر دخول محدد السعر معلق، بانتظار وصول السوق إليه" if s.mode=="PAPER" else "تم إرسال أمر شراء محدد السعر إلى MEXC","updated":time.time()}
            self._save_runtime()
        except Exception as e: self.last_error=f"{sym}: {e}"[:180]

    def _manage_entry_orders(self):
        changed=False
        for sym,entry in list(self.entry_orders.items()):
            try:
                if entry.get("paper"):
                    book=self.exchange.fetch_order_book(sym,limit=5)
                    asks=book.get("asks") or []
                    # A simulated buy fills only when the best ask reaches the limit price.
                    if asks and float(asks[0][0]) <= float(entry["price"]):
                        filled=float(entry["amount"]); avg=float(entry["price"])
                        self.positions[sym]={"entry":avg,"amount":filled,"cost":avg*filled,"opened":time.time(),"paper":True,"wall":entry["wall"],"budget":entry["budget"]}
                        self.entry_orders.pop(sym,None); changed=True
                        self.wall_status[sym]={"state":"position_open","bid":None,"ask":float(asks[0][0]),"wall_price":entry.get("wall"),"wall_usdt":entry.get("wall_usdt"),"wall_multiple":entry.get("wall_multiple"),"entry_price":avg,"budget":entry["budget"],"paper":True,"reason":"تمت محاكاة تنفيذ أمر الدخول بعد وصول سعر العرض إلى الحد المحدد","updated":time.time()}
                    continue
                order=self.exchange.fetch_order(entry["id"],sym)
                filled=float(order.get("filled") or 0); avg=float(order.get("average") or entry["price"])
                delta=max(0.0,filled-float(entry.get("filled") or 0))
                if delta>0:
                    p=self.positions.get(sym)
                    if p:
                        old=float(p["amount"]); total=old+delta
                        p["entry"]=(float(p["entry"])*old+avg*delta)/total
                        p["amount"]=total; p["cost"]+=avg*delta
                    else:
                        self.positions[sym]={"entry":avg,"amount":delta,"cost":avg*delta,"opened":time.time(),"paper":False,"wall":entry["wall"],"budget":entry["budget"],"order_id":entry["id"]}
                    entry["filled"]=filled; entry["cost"]=filled*avg; changed=True
                status=str(order.get("status") or "").lower()
                if status in ("closed","canceled","cancelled","rejected","expired"):
                    self.entry_orders.pop(sym,None); changed=True
            except Exception as e:
                self.last_error=f"متابعة أمر دخول {sym}: {e}"[:180]
        if changed: self._save_runtime()

    def _sell_market(self, sym, amount):
        """Submit a market sell and return the quantity the exchange confirms as filled."""
        amount=float(self.exchange.amount_to_precision(sym,float(amount)))
        if amount<=0: raise RuntimeError("الكمية القابلة للبيع تساوي صفرًا بعد التقريب")
        order=self.exchange.create_order(sym,"market","sell",amount)
        oid=str((order or {}).get("id") or "")
        checked=order or {}
        if oid:
            try: checked=self.exchange.fetch_order(oid,sym) or checked
            except Exception: pass
        filled=float((checked or {}).get("filled") or 0)
        status=str((checked or {}).get("status") or "").lower()
        if filled<=0 and status in ("closed","filled"):
            filled=amount
        if filled<=0:
            raise RuntimeError("لم تؤكد MEXC تنفيذ كمية البيع؛ تم الإبقاء على المركز للمراجعة")
        return min(filled,amount)

    def _manage(self):
        if not self.exchange:return
        s=get_settings(self.tid)
        self._manage_entry_orders()
        for sym,p in list(self.positions.items()):
            try:
                ticker=self.exchange.fetch_ticker(sym); px=float(ticker.get("bid") or ticker.get("last") or 0)
                if not px:continue
                target=p["entry"]*(1+s.tp_pct/100); stop=p["entry"]*(1-s.sl_pct/100)
                wall_target=p.get("wall_target")
                if px>=target or (wall_target and px>=wall_target) or px<=stop:
                    # Cancel any unfilled remainder first; otherwise it could reopen exposure after exit.
                    pending=self.entry_orders.get(sym)
                    if pending:
                        try:
                            self.exchange.cancel_order(pending["id"],sym)
                            self.entry_orders.pop(sym,None)
                        except Exception as cancel_error:
                            try:
                                chk=self.exchange.fetch_order(pending["id"],sym)
                                if str(chk.get("status") or "").lower() in ("canceled","cancelled","closed","rejected","expired"):
                                    self.entry_orders.pop(sym,None)
                                else:
                                    self.last_error=f"{sym}: لم يمكن إلغاء بقية أمر الدخول؛ تم تأجيل الخروج"; continue
                            except Exception:
                                self.last_error=f"{sym}: تعذر التحقق من أمر الدخول؛ تم تأجيل الخروج"; continue
                    amount=float(p["amount"])
                    filled=amount if p["paper"] else self._sell_market(sym,amount)
                    self.realized+=(px-p["entry"])*filled
                    remaining=max(0.0,amount-filled)
                    if remaining>1e-12:
                        p["amount"]=remaining; p["cost"]=remaining*float(p["entry"])
                        self.last_error=f"{sym}: تم تنفيذ بيع جزئي فقط؛ المتبقي {remaining:.8g}"
                    else:
                        self.positions.pop(sym,None)
                    self._save_runtime()
            except Exception as e:self.last_error=f"manage {sym}: {e}"[:180]

    def scan_symbol_now(self, symbol, mexc_client=None):
        if mexc_client is not None: self.exchange=getattr(mexc_client,"exchange",mexc_client)
        if not self.exchange: raise RuntimeError("اتصال MEXC غير متاح")
        s=get_settings(self.tid); started=time.time(); sym=symbol.upper()
        try:
            self._manage()
            self._scan_one(sym,s)
            self.scanned=1; self.candidates=[sym]; self.last_scan=time.time()-started
        except Exception as e:
            self.last_error=str(e)[:180]; raise

    def scan_once(self):
        s=get_settings(self.tid)
        running=_csv(s.running_symbols)
        self.enabled=bool(s.enabled and running)
        if not self.exchange:return
        started=time.time()
        try:
            self._manage()  # TP/SL management continues even when new entries are stopped.
            self.scanned=0; self.candidates=[]
            if self.enabled:
                for sym in running:
                    try:
                        self._scan_one(sym,s); self.scanned+=1; self.candidates.append(sym)
                    except Exception as e:self.last_error=f"{sym}: {e}"[:180]
            self.last_scan=time.time()-started
        except Exception as e:self.last_error=str(e)[:180]

    def close_symbol(self, symbol):
        """Cancel this strategy's tracked entry order before market-selling any filled inventory."""
        sym=symbol.upper(); messages=[]
        if self.exchange and sym in self.entry_orders:
            # Sync any last partial fill before cancellation and closing.
            self._manage_entry_orders()
        pending=self.entry_orders.get(sym)
        if pending and self.exchange:
            if pending.get("paper"):
                self.entry_orders.pop(sym,None); messages.append("تم إلغاء أمر الدخول المحاكي"); self._save_runtime()
            else:
                try:
                    self.exchange.cancel_order(pending["id"],sym)
                    self.entry_orders.pop(sym,None); messages.append("تم إلغاء أمر الدخول المعلق"); self._save_runtime()
                except Exception as e:
                    # Do not market sell until the pending buy has been canceled/verified.
                    try:
                        chk=self.exchange.fetch_order(pending["id"],sym)
                        if str(chk.get("status") or "").lower() in ("canceled","cancelled","closed","rejected","expired"):
                            self.entry_orders.pop(sym,None); self._save_runtime()
                        else:
                            return False, f"تعذر إلغاء أمر الدخول، لم يتم البيع لتجنب شراء جديد أثناء الإغلاق: {str(e)[:100]}"
                    except Exception:
                        return False, f"تعذر التحقق من إلغاء أمر الدخول، لم يتم البيع: {str(e)[:100]}"
        p=self.positions.get(sym)
        if p:
            try:
                amount=float(p["amount"])
                if not p.get("paper"):
                    filled=self._sell_market(sym,amount)
                    remaining=max(0.0,amount-filled)
                    if remaining>1e-12:
                        p["amount"]=remaining; p["cost"]=remaining*float(p["entry"])
                        messages.append(f"تم بيع جزء من المركز؛ المتبقي {remaining:.8g} يحتاج مراجعة")
                    else:
                        self.positions.pop(sym,None); messages.append("تم تأكيد بيع المركز بسعر السوق")
                else:
                    try:
                        ticker=self.exchange.fetch_ticker(sym); px=float(ticker.get("bid") or ticker.get("last") or p["entry"])
                    except Exception: px=float(p["entry"])
                    self.realized+=(px-float(p["entry"]))*amount
                    self.positions.pop(sym,None); messages.append("تم إغلاق مركز PAPER محاكيًا")
                self._save_runtime()
            except Exception as e:return False,"فشل إغلاق المركز: "+str(e)[:120]
        if not messages: return False,"لا يوجد مركز أو أمر دخول تابع للاستراتيجية لهذا الزوج"
        self.stop_symbol(sym)
        return True,"\n".join(messages)

    def status(self):
        s=get_settings(self.tid)
        return {"enabled":bool(s.enabled),"mode":s.mode,"positions":self.positions,"entry_orders":self.entry_orders,
                "wall_status":self.wall_status,"scanned":self.scanned,"scan_sec":self.last_scan,"realized":self.realized,"last_error":self.last_error,
                "symbols":_csv(s.watch_symbols),"running_symbols":_csv(s.running_symbols),"budgets":get_symbol_budgets(self.tid)}

_instances={}
def get_sniper(tid):
    tid=int(tid)
    if tid not in _instances:_instances[tid]=OrderBookSniper(tid)
    return _instances[tid]
