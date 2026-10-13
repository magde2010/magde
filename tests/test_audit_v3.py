import os, sys, types, tempfile, unittest

class _MexcStub:
    def __init__(self, *a, **k): pass
sys.modules.setdefault('ccxt', types.SimpleNamespace(mexc=_MexcStub))
DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False); DB.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + DB.name
os.environ['ADMIN_TELEGRAM_ID'] = '123'
os.environ['MEXC_API_KEY'] = 'x'; os.environ['MEXC_API_SECRET'] = 'y'

from mexc_client import MexcClient
import crash_hunter

class BuyExchange:
    def __init__(self, error): self.error=error; self.markets={}; self.calls=0
    def load_markets(self): self.markets={'BTC/USDT': {}}; return self.markets
    def market(self, symbol): return {'limits': {'amount': {'min': 0}, 'cost': {'min': 1}}}
    def amount_to_precision(self, symbol, amount): return amount
    def fetch_ticker(self, symbol): return {'last': 100}
    def create_order(self, **kwargs):
        self.calls += 1
        raise Exception(self.error)

class TPClient:
    quote='USDT'
    def __init__(self): self.sell=[]
    def fetch_open_orders(self, symbol): return []
    def fetch_order(self, oid, symbol, strict=False):
        if oid == 'tp1': return {'id':'tp1','status':'closed','filled':0.1,'average':108}
        return {'id':oid,'status':'closed','filled':0.1,'average':90}
    def get_ticker_price(self, pair): return 100
    def get_free_amount(self, symbol): return 0
    def create_limit_sell(self,*args): return {'id':'tp1'}
    def cancel_order(self,*args,**kwargs): return {'id':'tp1','status':'canceled'}
    def create_market_order(self,*args,**kwargs): self.sell.append(args); return {'id':'sell'}

class V3Tests(unittest.TestCase):
    def test_market_buy_timeout_never_falls_back_to_second_order(self):
        c=MexcClient(); ex=BuyExchange('network timeout'); c.exchange=ex
        with self.assertRaises(Exception): c.create_market_buy_usdt('BTC', 10)
        self.assertEqual(ex.calls, 1)

    def test_market_buy_explicit_unsupported_cost_can_fallback(self):
        class E(BuyExchange):
            def create_order(self, **kwargs):
                self.calls += 1
                if self.calls == 1: raise Exception('unsupported cost parameter')
                return {'id':'buy2','filled':0.1,'average':100}
        c=MexcClient(); ex=E(''); c.exchange=ex
        c.get_ticker_price=lambda pair: 100
        r=c.create_market_buy_usdt('BTC',10)
        self.assertEqual(r['id'],'buy2'); self.assertEqual(ex.calls,2)


    def test_crash_tp_fill_closes_position(self):
        crash_hunter._TRACK.clear(); crash_hunter._PERSISTENCE_LOADED=True
        t=crash_hunter.TrackedOrder('BTC','buy1',90,10,-10,0,status='filled',fill_price=90,amount=0.1,tp_order_id='tp1')
        crash_hunter._TRACK['buy1']=t
        c=TPClient()
        msgs=crash_hunter.monitor_crash_fills(c)
        self.assertEqual(crash_hunter._TRACK['buy1'].status,'closed')
        self.assertFalse(c.sell)

    def test_no_blanket_cancel_calls_in_portfolio_bot(self):
        text=open(os.path.join(os.path.dirname(__file__),'..','bot.py'),encoding='utf-8').read()
        self.assertNotIn('cancel_all_open_sells(', text)
        crash = open(os.path.join(os.path.dirname(__file__),'..','crash_hunter.py'),encoding='utf-8').read()
        self.assertNotIn('client.cancel_open_buy_orders(sym)', crash)

if __name__ == '__main__': unittest.main()
