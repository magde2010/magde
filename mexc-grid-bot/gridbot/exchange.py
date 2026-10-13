import os, logging, time
from decimal import Decimal
log=logging.getLogger(__name__)
RETRYABLE=(TimeoutError,ConnectionError)
class ExchangeAdapter:
    """MEXC Spot CCXT adapter. Dry run may read public market data but never sends order mutations."""
    def __init__(self,dry_run=True,exchange=None):
        self.dry_run=bool(dry_run); self.paper_orders={}; self.exchange=exchange
        if not self.dry_run and not (os.getenv('MEXC_API_KEY') and os.getenv('MEXC_API_SECRET')): raise RuntimeError('Live mode requires MEXC_API_KEY and MEXC_API_SECRET')
        if self.exchange is None:
            import ccxt
            opts={'enableRateLimit':True,'timeout':15000,'options':{'defaultType':'spot'}}
            if not self.dry_run: opts.update(apiKey=os.environ['MEXC_API_KEY'],secret=os.environ['MEXC_API_SECRET'])
            self.exchange=ccxt.mexc(opts)
            self.exchange.load_markets()
    def _call(self,fn,*args,**kwargs):
        try:
            import ccxt
            retry=(ccxt.NetworkError,ccxt.RequestTimeout,ccxt.ExchangeNotAvailable,ccxt.RateLimitExceeded)
        except ModuleNotFoundError:
            retry=()
        for attempt in range(3):
            try: return fn(*args,**kwargs)
            except retry:
                if attempt==2: raise
                time.sleep(min(2**attempt,4))
    def market(self,symbol): return self.exchange.market(symbol)
    def fetch_open_orders(self,symbol): return [] if self.dry_run else self._call(self.exchange.fetch_open_orders,symbol)
    def fetch_orders(self,symbol,since=None,limit=100):
        if self.dry_run: return list(self.paper_orders.values())
        out=[]
        if self.exchange.has.get('fetchOrders'):
            out.extend(self._call(self.exchange.fetch_orders,symbol,since,limit) or [])
        elif self.exchange.has.get('fetchClosedOrders'):
            out.extend(self._call(self.exchange.fetch_closed_orders,symbol,since,limit) or [])
        if self.exchange.has.get('fetchCanceledOrders'):
            out.extend(self._call(self.exchange.fetch_canceled_orders,symbol,since,limit) or [])
        return out
    def seed_paper_order(self, local):
        oid=str(local.get('exchange_order_id') or ('paper-'+str(local['client_order_id'])))
        self.paper_orders.setdefault(oid, {'id':oid,'clientOrderId':local['client_order_id'],'status':local.get('status') if local.get('status') in ('closed','canceled','rejected','expired') else 'open','symbol':local['symbol'],'side':local['side'],'type':'limit','amount':float(local.get('amount') or 0),'price':float(local.get('price') or 0),'filled':float(local.get('filled') or 0),'cost':float(local.get('cost') or 0),'timestamp':None,'info':{'paper':True}})
        return self.paper_orders[oid]
    def fetch_order(self,order_id,symbol,params=None):
        if self.dry_run: return self.paper_orders.get(str(order_id))
        return self._call(self.exchange.fetch_order,order_id,symbol,params or {})
    def fetch_ohlcv(self,symbol,timeframe='3m',limit=3): return self._call(self.exchange.fetch_ohlcv,symbol,timeframe=timeframe,limit=limit)
    def amount_to_precision(self,symbol,amount): return self.exchange.amount_to_precision(symbol,str(amount))
    def price_to_precision(self,symbol,price): return self.exchange.price_to_precision(symbol,str(price))
    def limits(self,symbol):
        m=self.market(symbol); limits=m.get('limits') or {}; return {'amount_min':Decimal(str((limits.get('amount') or {}).get('min') or '0')),'cost_min':Decimal(str((limits.get('cost') or {}).get('min') or '0'))}
    def create_limit_order(self,symbol,side,amount,price,params=None,client_order_id=None):
        if self.dry_run:
            oid='paper-'+str(client_order_id); o={'id':oid,'clientOrderId':client_order_id,'status':'open','symbol':symbol,'side':side,'type':'limit','amount':float(amount),'price':float(price),'filled':0.0,'cost':0.0,'timestamp':int(time.time()*1000),'info':{'paper':True}}
            self.paper_orders[oid]=o; return o
        p=dict(params or {})
        if client_order_id: p['clientOrderId']=client_order_id
        # Never automatically retry an order-creation request: a timeout may mean it reached MEXC.
        return self.exchange.create_order(symbol,'limit',side,float(amount),float(price),p)
    def cancel_order(self,order_id,symbol):
        if self.dry_run:
            o=self.paper_orders.get(str(order_id));
            if o: o['status']='canceled'
            return {'id':order_id,'status':'canceled'}
        # Cancellation is also sent once; a timeout is reconciled by order ID on the next poll.
        return self.exchange.cancel_order(order_id,symbol)
    def ticker(self,symbol): return self._call(self.exchange.fetch_ticker,symbol)
    def simulate_fills(self,symbol,last_price):
        if not self.dry_run: return []
        filled=[]; p=Decimal(str(last_price))
        for o in self.paper_orders.values():
            if o['status']!='open': continue
            price=Decimal(str(o['price'])); hit=(o['side']=='buy' and p<=price) or (o['side']=='sell' and p>=price)
            if hit:
                o.update(status='closed',filled=o['amount'],cost=float(Decimal(str(o['amount']))*price),average=float(price),remaining=0.0)
                filled.append(o.copy())
        return filled
