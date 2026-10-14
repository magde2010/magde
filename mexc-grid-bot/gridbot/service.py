import asyncio, logging, uuid
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from .config import D
from .engine import dca_quote_budgets, dca_prices, tp_prices, Position, recenter_confirmed
from .recovery import reconcile, client_id
log=logging.getLogger(__name__)
ACTIVE={'pending','open','unknown','partially_filled','cancel_pending'}
TERMINAL={'closed','canceled','rejected','expired'}

def _dt(value):
    if not value: return None
    try: return datetime.fromisoformat(value.replace('Z','+00:00'))
    except Exception: return None

class TradingService:
    def __init__(self,cfg,db,exchange):
        self.cfg,self.db,self.exchange=cfg,db,exchange; self.recovered=False; self.last_price=None; self.last_error=None
    def _position(self): return self.db.position()
    def _accounting(self):
        p=self._position(); return Position(p['quantity'],p['cost'],p['spent'])
    def _save_accounting(self,accounting,**extra):
        self.db.update_position(quantity=str(accounting.quantity),cost=str(accounting.cost),average_entry=str(accounting.average_entry),spent=str(accounting.spent),remaining_budget=str(max(D('0'),self.cfg.total_budget-accounting.spent)),**extra)
    def _new_client_id(self,role,level):
        # A deterministic key for the strategy intent prevents a retry from creating a second order.
        row=self.db.conn.execute('SELECT COALESCE(MAX(id),0)+1 AS n FROM grid_orders').fetchone(); return f'grid-{role[:3]}-{level}-{row["n"]}-{uuid.uuid4().hex[:8]}'
    def _persist_order(self,role,level,side,price,amount,order_type='limit'):
        price,amount=D(price),D(amount)
        if price<=0 or amount<=0: raise ValueError('order price and amount must be positive')
        market=self.exchange.market(self.cfg.symbol); limits=self.exchange.limits(self.cfg.symbol)
        amount=D(self.exchange.amount_to_precision(self.cfg.symbol,amount)); price=D(self.exchange.price_to_precision(self.cfg.symbol,price))
        if amount < limits['amount_min'] or amount*price < limits['cost_min']: 
            self.db.event('order_skipped_minimum',{'role':role,'level':level,'amount':amount,'price':price,'limits':limits}); return None
        cid=self._new_client_id(role,level)
        # Intent is committed before network I/O. An interrupted/uncertain create is never blindly retried.
        row=self.db.insert_order_intent(cid,self.cfg.symbol,role,level,side,price,amount)
        if row['status']!='pending' or row['exchange_order_id']: return row
        try:
            if order_type == 'market' and hasattr(self.exchange, 'create_market_order'):
                remote=self.exchange.create_market_order(self.cfg.symbol,side,amount,client_order_id=cid)
            else:
                remote=self.exchange.create_limit_order(self.cfg.symbol,side,amount,price,client_order_id=cid)
            self.db.update_order(cid,exchange_order_id=str(remote.get('id')) if remote.get('id') is not None else None,status=remote.get('status') or 'unknown',raw_status=remote.get('status'),filled='0',cost='0',last_error=None)
            saved=self.db.order_by_client_id(cid)
            if D(str(remote.get('filled') or '0'))>0: self._sync_order(saved,remote)
            return self.db.order_by_client_id(cid)
        except Exception as exc:
            self.db.update_order(cid,status='unknown',last_error=type(exc).__name__)
            self.db.event('order_create_uncertain',{'client_order_id':cid,'error_type':type(exc).__name__})
            self.db.update_position(state='FROZEN')
            raise RuntimeError(f'Order creation uncertain for {cid}; recovery required before new orders') from exc
    def recover(self):
        """Must run before any strategy order is allowed; ambiguous intents freeze the bot."""
        local=self.db.recovery_orders()
        try:
            if self.cfg.dry_run:
                for local_order in local:
                    if hasattr(self.exchange,'seed_paper_order') and str(local_order.get('exchange_order_id') or '').startswith('paper-'):
                        self.exchange.seed_paper_order(local_order)
            remote=list(self.exchange.fetch_open_orders(self.cfg.symbol) or [])
            history=list(self.exchange.fetch_orders(self.cfg.symbol,limit=100) or [])
        except Exception as exc:
            self.db.update_position(state='FROZEN'); self.db.event('recovery_api_failed',{'error_type':type(exc).__name__}); self.recovered=False; raise
        all_remote={}
        for o in remote+history:
            if o.get('id') is not None: all_remote[str(o['id'])]=o
            if client_id(o): all_remote['cid:'+str(client_id(o))]=o
        # Include closed-history entries, but do not mistake unrelated exchange orders for ours.
        for match in reconcile(local,list(all_remote.values())):
            l,r=match['local'],match['exchange']
            if r:
                self._sync_order(l,r)
            else:
                # Never resend an intent that may have reached the exchange. A human-visible freeze is safer.
                self.db.update_order(l['client_order_id'],status='unknown',last_error='not found in open orders/history; manual verification required')
                self.db.update_position(state='FROZEN')
                self.db.event('recovery_ambiguous_order',{'client_order_id':l['client_order_id'],'role':l['role']})
        if any(o['status']=='unknown' for o in self.db.open_orders()): self.db.update_position(state='FROZEN')
        p=self._position()
        if p['state']!='FROZEN' and not p['emergency_stop'] and D(p['quantity'])>0:
            active_tps=[o for o in self.db.open_orders() if o['role']=='tp']
            if not active_tps or D(p.get('tp_reference_avg') or '0')!=D(p['average_entry']): self._rebuild_tps()
        self.recovered=True
        return self.db.position()['state']
    def _sync_order(self,local,remote,*,rebuild_tps=True):
        cid=local['client_order_id']; status=(remote.get('status') or 'unknown').lower()
        if status == 'cancelled': status = 'canceled'
        new_filled=D(str(remote.get('filled') or '0')); new_cost=D(str(remote.get('cost') or '0'))
        old_accounted=D(local.get('accounted_filled') or '0'); old_cost_accounted=D(local.get('accounted_cost') or '0')
        delta_q=max(D('0'),new_filled-old_accounted); delta_cost=max(D('0'),new_cost-old_cost_accounted)
        # Order observation and position accounting commit atomically, so restart cannot lose or double-count a fill.
        self.db.conn.execute('BEGIN IMMEDIATE')
        try:
            self.db.update_order(cid,exchange_order_id=str(remote.get('id') or local.get('exchange_order_id') or '') or None,status=status,raw_status=status,filled=str(new_filled),cost=str(new_cost),last_error=None)
            if delta_q>0:
                accounting=self._accounting()
                # Exchange filled/cost are cumulative. Account each newly observed fill using
                # the incremental quote cost, not the cumulative average applied to a delta.
                incremental_cost = delta_cost if delta_cost > 0 else D('0')
                if incremental_cost <= 0:
                    avg_hint = D(str(remote.get('average') or local.get('price') or '0'))
                    incremental_cost = delta_q * avg_hint
                avg = incremental_cost / delta_q if delta_q > 0 else D('0')
                if local['side']=='buy':
                    accounting.add_fill(delta_q,avg,incremental_cost)
                    dca4_done=bool(self._position()['dca4_complete']) or (local['role']=='dca' and int(local['level'])==4)
                    self._save_accounting(accounting,state='DEFENSIVE' if dca4_done else 'ACTIVE',dca4_complete=1 if dca4_done else 0)
                    self.db.event('buy_fill',{'role':local['role'],'level':local['level'],'quantity':delta_q,'average_entry':accounting.average_entry})
                else:
                    if delta_q > accounting.quantity:
                        self.db.update_position(state='FROZEN')
                        self.db.event('sell_fill_exceeds_position', {'client_order_id': cid, 'fill_delta': delta_q, 'position_quantity': accounting.quantity})
                        raise RuntimeError('Exchange sell fill exceeds locally tracked position; manual reconciliation required')
                    accounting.sell_fill(delta_q,avg)
                    defensive=bool(self._position()['dca4_complete'])
                    self._save_accounting(accounting,state=('DEFENSIVE' if defensive else 'ACTIVE') if accounting.quantity>0 else 'IDLE')
                    self.db.event('sell_fill',{'level':local['level'],'quantity':delta_q,'remaining_quantity':accounting.quantity})
                self.db.update_order(cid,accounted_filled=str(new_filled),accounted_cost=str(new_cost))
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            # Accounting inconsistencies must survive transaction rollback as a safety latch.
            self.db.update_position(state='FROZEN')
            self.db.event('order_sync_invariant_failure', {'client_order_id': cid})
            raise
        if delta_q>0 and rebuild_tps: self._rebuild_tps()
    def _cancel_owned(self,roles):
        for order in self.db.open_orders():
            if order['role'] not in roles: continue
            oid=order.get('exchange_order_id')
            if not oid:
                self.db.update_order(order['client_order_id'],status='unknown',last_error='cannot cancel without exchange order id'); self.db.update_position(state='FROZEN'); raise RuntimeError('Cannot safely cancel an owned order without its exchange id')
            try:
                # A successful cancel request is not proof that the order is terminal.
                # Re-fetch it, account any race fills, and only then permit replacement orders.
                self.db.update_order(order['client_order_id'], status='cancel_pending')
                self.exchange.cancel_order(oid, order['symbol'])
                remote = self.exchange.fetch_order(oid, order['symbol'])
                if not remote:
                    raise RuntimeError('cancel result is ambiguous: order status unavailable')
                status = (remote.get('status') or 'unknown').lower()
                if status == 'cancelled': status = 'canceled'
                if status not in TERMINAL:
                    self.db.update_order(order['client_order_id'], status='unknown', raw_status=status,
                                         last_error='cancel not confirmed terminal')
                    self.db.update_position(state='FROZEN')
                    raise RuntimeError('Cancel not confirmed; refusing to place replacement order')
                # Sync any fills that occurred while cancellation was in flight, without
                # recursively rebuilding TPs before all old TPs have been checked.
                self._sync_order(order, remote, rebuild_tps=False)
                self.db.update_order(order['client_order_id'], status=status, raw_status=status, last_error=None)
            except Exception as exc:
                current = self.db.order_by_client_id(order['client_order_id'])
                if current and current['status'] not in TERMINAL:
                    self.db.update_order(order['client_order_id'],status='unknown',last_error=type(exc).__name__)
                self.db.update_position(state='FROZEN')
                raise RuntimeError('Could not safely confirm cancellation; bot frozen') from exc
    def _rebuild_tps(self):
        self._cancel_owned({'tp'})
        if self._position()['state'] == 'FROZEN':
            raise RuntimeError('TP rebuild blocked because an old TP may still be active')
        p=self._accounting()
        if p.quantity<=0 or p.average_entry<=0:
            self.db.update_position(tp_reference_avg='0'); return
        remaining=p.quantity
        for i,(gain,pct) in enumerate(zip(self.cfg.tp_gains,self.cfg.tp_sell_pcts),1):
            if pct<=0: continue
            amount=p.quantity*pct/D('100') if i<4 else remaining
            amount=min(amount,remaining); remaining-=amount
            if amount<=0: continue
            price=p.average_entry*(D('1')+gain/D('100'))
            self._persist_order('tp',i,'sell',price,amount)
        self.db.update_position(tp_reference_avg=str(p.average_entry))
    def _place_initial_if_needed(self,price):
        p=self._position()
        if D(p['quantity'])>0 or any(o['role']=='initial' and o['status'] in ACTIVE|TERMINAL for o in self.db.orders()): return
        amount=self.cfg.initial_budget/price
        # The first LIVE entry must not wait for a limit-price touch. DCA and
        # TP remain limit orders; only the initial allocation uses market.
        initial_type = 'market' if not self.cfg.dry_run else 'limit'
        self._persist_order('initial',0,'buy',price,amount,order_type=initial_type)
    def _ensure_dca(self):
        p=self._position()
        if p['dca4_complete'] or p['emergency_stop'] or p['state']=='FROZEN': return
        reference=D(p['grid_reference'] or '0')
        if reference<=0: return
        budgets=dca_quote_budgets(self.cfg); prices=dca_prices(reference,self.cfg)
        all_orders=self.db.orders()
        for i,(budget,price) in enumerate(zip(budgets,prices),1):
            if budget<=0: continue
            existing=[o for o in all_orders if o['role']=='dca' and int(o['level'])==i and o['status'] not in ('canceled','rejected','expired')]
            if existing: continue
            all_dca=[o for o in all_orders if o['role']=='dca']
            dca_spent=sum((D(o.get('cost') or '0') for o in all_dca),D('0'))
            dca_reserved=sum((max(D('0'),D(o['amount'])-D(o.get('filled') or '0'))*D(o['price']) for o in self.db.open_orders() if o['role']=='dca'),D('0'))
            if dca_spent+dca_reserved+budget>self.cfg.dca_budget: raise RuntimeError('DCA order would exceed its dedicated DCA budget')
            spent=D(p['spent']); reserved=sum((max(D('0'),D(o['amount'])-D(o.get('filled') or '0'))*D(o['price']) for o in self.db.open_orders() if o['side']=='buy'),D('0'))
            if spent+reserved+budget>self.cfg.total_budget: raise RuntimeError('DCA order would exceed total budget')
            self._persist_order('dca',i,'buy',price,budget/price)
    def _recenter(self,candles,now):
        if len(candles)<2: return
        closed=candles[-2]; candle_ms=int(closed[0]); p=self._position(); reference=D(p['grid_reference'] or '0')
        if reference<=0 or candle_ms==p['last_3m_candle_ms']: return
        last=_dt(p['last_recenter_time'])
        candle_open=datetime.fromtimestamp(candle_ms/1000,tz=timezone.utc)
        if recenter_confirmed(reference,D(str(closed[4])),True,candle_open,last,now,self.cfg):
            self._cancel_owned({'dca'})
            self.db.update_position(grid_reference=str(D(str(closed[4]))),last_recenter_time=now.isoformat(),last_3m_candle_ms=candle_ms,state='ACTIVE')
            self.db.event('recenter',{'new_reference':str(closed[4]),'candle_ms':candle_ms})
            if not self.cfg.keep_position_on_recenter: self._cancel_owned({'tp'})
            self._ensure_dca()
        else: self.db.update_position(last_3m_candle_ms=candle_ms)
    async def tick(self):
        if not self.recovered: raise RuntimeError('Recovery must complete before monitoring or order placement')
        ticker=self.exchange.ticker(self.cfg.symbol); price=D(str(ticker.get('last') or ticker.get('close') or '0'))
        if price<=0: raise RuntimeError('Exchange returned invalid ticker price')
        self.last_price=price
        if self.cfg.dry_run: self.exchange.simulate_fills(self.cfg.symbol,price)
        # Reconcile every locally active order with current exchange state/history.
        for local in self.db.open_orders():
            remote=None
            try:
                if local.get('exchange_order_id'): remote=self.exchange.fetch_order(local['exchange_order_id'],local['symbol'])
            except Exception as exc:
                self.db.event('order_poll_failed',{'client_order_id':local['client_order_id'],'error_type':type(exc).__name__}); continue
            if remote: self._sync_order(local,remote)
        p=self._position(); now=datetime.now(timezone.utc)
        try: candles=self.exchange.fetch_ohlcv(self.cfg.symbol,'3m',3)
        except Exception: candles=[]
        if p['emergency_stop']:
            self.db.update_position(state='EMERGENCY_STOP'); return
        if p['state'] in ('FROZEN','DEFENSIVE'): return
        if not p['running']:
            self.db.update_position(state='PAUSED' if D(p['quantity'])>0 else 'IDLE'); return
        if D(p['grid_reference'] or '0')<=0:
            self.db.update_position(grid_reference=str(price),state='ACTIVE')
        self._place_initial_if_needed(price)
        self._ensure_dca()
        if candles: self._recenter(candles,now)
        self.db.update_position(state='ACTIVE')
    async def run_forever(self):
        while True:
            try: await self.tick(); self.last_error=None
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.last_error=f'{type(exc).__name__}: {exc}'; log.exception('Grid monitor iteration failed')
            await asyncio.sleep(self.cfg.poll_seconds)
    def status_text(self):
        p=self._position(); orders=self.db.orders(); active=[o for o in orders if o['status'] in ACTIVE]
        dca=' | '.join(f"L{i}:{next((o['status'] for o in reversed(orders) if o['role']=='dca' and int(o['level'])==i), 'غير منشأ')}" for i in range(1,5))
        tp=' | '.join(f"TP{i}:{next((o['status'] for o in reversed(orders) if o['role']=='tp' and int(o['level'])==i), 'غير منشأ')}" for i in range(1,5))
        return (f"الرمز: {self.cfg.symbol}\nالسعر الحالي: {self.last_price or 'غير متاح'}\nمتوسط الدخول: {p['average_entry']}\nالكمية: {p['quantity']}\nالمبلغ المصروف: {p['spent']} USDT\nالمتبقي من الميزانية: {p['remaining_budget']} USDT\nحالات DCA: {dca}\nحالات TP: {tp}\nالأوامر المفتوحة: {len(active)}\nآخر مزامنة: {p['last_sync_at'] or 'لم تتم'}\nالوضع: {'DRY RUN' if self.cfg.dry_run else 'LIVE'} / {p['state']}\nEmergency Stop: {'مفعل' if p['emergency_stop'] else 'لا'}\nآخر خطأ: {self.last_error or 'لا يوجد'}")
