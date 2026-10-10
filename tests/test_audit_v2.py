import os, tempfile, unittest, sys, types

# Minimal ccxt stub: these tests exercise our logic, not the third-party SDK.
class _MexcStub:
    def __init__(self, *a, **k): pass
ccxt_stub=types.SimpleNamespace(mexc=_MexcStub)
sys.modules.setdefault("ccxt", ccxt_stub)

DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
DB.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + DB.name
os.environ['ADMIN_TELEGRAM_ID'] = '123'
os.environ['MEXC_API_KEY'] = 'x'
os.environ['MEXC_API_SECRET'] = 'y'

import database
from database import SessionLocal, init_db, create_portfolio, record_trade_event, claim_reentry_event, has_running_symbol_conflict
import crash_hunter
from mexc_client import MexcClient
from rebalancer import Rebalancer

class FakeExchange:
    def __init__(self, exc=None, order=None):
        self.exc=exc; self.order=order; self.markets={}
    def cancel_order(self, order_id, pair):
        if self.exc: raise Exception(self.exc)
        return {'id': order_id, 'status':'canceled'}
    def fetch_order(self, order_id, pair):
        return self.order

class FakeCrashClient:
    quote='USDT'
    def __init__(self, order): self.order=order; self.sell=[]
    def fetch_open_orders(self, symbol): return []
    def fetch_order(self, oid, symbol): return self.order
    def get_ticker_price(self, pair): return 100.0
    def get_free_amount(self, symbol): return 0.0
    def create_limit_sell(self, symbol, amount, price): self.sell.append((symbol,amount,price)); return {'id':'tp1'}
    def cancel_order(self,*a,**k): return {'id':'tp1','status':'canceled'}
    def create_market_order(self,*a,**k): return {'id':'sell1'}


class FakeCrashPlaceClient:
    quote='USDT'
    def get_free_usdt(self): return 100.0
    def cancel_open_buy_orders(self, symbol): return {'cancelled': [], 'errors': []}
    def get_ticker_price(self, pair): return 100.0
    def create_limit_buy(self, symbol, amount, price): return {'id': f'oid-{symbol}-{price:.0f}'}
    def fetch_open_orders(self, symbol): return [{'id':'never'}]
    def fetch_order(self, oid, symbol): return {'id':oid,'status':'open'}

class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): init_db()

    def test_create_portfolio_deduplicates(self):
        db=SessionLocal()
        p=create_portfolio(db, 1, 'P', 100, ['btc','BTC',' eth '])
        self.assertEqual([c.symbol for c in p.coins], ['BTC','ETH'])
        db.close()

    def test_reentry_claim_is_atomic(self):
        db=SessionLocal()
        p=create_portfolio(db, 2, 'P2', 100, ['BTC'])
        c=p.coins[0]
        ev=record_trade_event(db,2,p.id,c.id,'BTC','stop_loss',100,95,1,-5,True)
        self.assertTrue(claim_reentry_event(db,ev.id,2))
        self.assertFalse(claim_reentry_event(db,ev.id,2))
        db.close()

    def test_running_symbol_conflict(self):
        db=SessionLocal()
        p1=create_portfolio(db,3,'A',100,['BTC'])
        p2=create_portfolio(db,3,'B',100,['BTC'])
        p1.is_running=True; db.commit()
        self.assertEqual(has_running_symbol_conflict(db,3,p2.id,['BTC']), ['BTC'])
        db.close()

    def test_cancel_order_raises_real_failure(self):
        c=MexcClient(); c.exchange=FakeExchange(exc='network timeout')
        with self.assertRaises(Exception): c.cancel_order('1','BTC',strict=False)

    def test_cancel_order_accepts_already_closed(self):
        c=MexcClient(); c.exchange=FakeExchange(exc='order already canceled')
        r=c.cancel_order('1','BTC',strict=True)
        self.assertTrue(r['already_gone'])

    def test_crash_order_persists_across_runtime_reload(self):
        crash_hunter._TRACK.clear()
        crash_hunter._PERSISTENCE_LOADED = False
        res = crash_hunter.place_crash_ladder(FakeCrashPlaceClient(), ['BTC'], [-10], 10)
        self.assertTrue(res['placed'])
        oid = res['placed'][0]['order_id']
        crash_hunter._TRACK.clear()
        crash_hunter._PERSISTENCE_LOADED = False
        crash_hunter.monitor_crash_fills(FakeCrashPlaceClient())
        self.assertIn(oid, crash_hunter._TRACK)
        self.assertEqual(crash_hunter._TRACK[oid].status, 'open')

    def test_crash_hunter_does_not_guess_cancelled_as_fill(self):
        crash_hunter._TRACK.clear()
        crash_hunter._TRACK['o1']=crash_hunter.TrackedOrder('BTC','o1',90,10,-10,0)
        msgs=crash_hunter.monitor_crash_fills(FakeCrashClient({'id':'o1','status':'canceled','filled':0}))
        self.assertEqual(crash_hunter._TRACK['o1'].status,'cancelled')
        self.assertEqual(msgs,[])

    def test_crash_hunter_uses_actual_fill(self):
        crash_hunter._TRACK.clear()
        crash_hunter._TRACK['o2']=crash_hunter.TrackedOrder('BTC','o2',90,10,-10,0)
        client=FakeCrashClient({'id':'o2','status':'closed','filled':0.123,'average':91.2})
        crash_hunter.monitor_crash_fills(client)
        t=crash_hunter._TRACK['o2']
        self.assertEqual(t.status,'filled')
        self.assertAlmostEqual(t.amount,0.123)
        self.assertAlmostEqual(t.fill_price,91.2)

if __name__=='__main__': unittest.main(verbosity=2)
