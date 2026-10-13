import unittest, asyncio, tempfile
from pathlib import Path
from decimal import Decimal as D, ROUND_DOWN
from gridbot.exchange import ExchangeAdapter
from gridbot.config import BotConfig
from gridbot.db import GridDB
from gridbot.service import TradingService
class FakeCCXT:
    def __init__(self): self.create_calls=0; self.cancel_calls=0; self.has={'fetchOrders':True}; self.markets={'BTC/USDT':{'spot':True,'limits':{'amount':{'min':0.000001},'cost':{'min':1}}}}; self.orders={}; self.last=100
    def market(self,s): return self.markets[s]
    def fetch_ticker(self,s): return {'last':self.last}
    def fetch_ohlcv(self,*a,**k): return [[1000,100,101,99,100,1],[181000,100,101,99,100,1]]
    def amount_to_precision(self,s,a): return format(D(str(a)).quantize(D('0.00000001'),rounding=ROUND_DOWN),'f')
    def price_to_precision(self,s,p): return format(D(str(p)).quantize(D('0.00000001'),rounding=ROUND_DOWN),'f')
    def create_order(self,*a,**k): self.create_calls+=1; raise AssertionError('dry run called create_order')
    def cancel_order(self,*a,**k): self.cancel_calls+=1; raise AssertionError('dry run called cancel_order')
class ExchangeTests(unittest.TestCase):
    def test_live_create_timeout_is_not_retried(self):
        fake=FakeCCXT(); calls=[]
        def timeout(*args,**kwargs): calls.append(1); raise TimeoutError('ambiguous create')
        fake.create_order=timeout
        import os
        old_key=os.environ.get('MEXC_API_KEY'); old_secret=os.environ.get('MEXC_API_SECRET')
        os.environ['MEXC_API_KEY']='test-key'; os.environ['MEXC_API_SECRET']='test-secret'
        try:
            x=ExchangeAdapter(dry_run=False,exchange=fake)
            with self.assertRaises(TimeoutError): x.create_limit_order('BTC/USDT','buy','0.1','100',client_order_id='once')
            self.assertEqual(len(calls),1)
        finally:
            if old_key is None: os.environ.pop('MEXC_API_KEY',None)
            else: os.environ['MEXC_API_KEY']=old_key
            if old_secret is None: os.environ.pop('MEXC_API_SECRET',None)
            else: os.environ['MEXC_API_SECRET']=old_secret
    def test_dry_run_never_mutates_exchange(self):
        fake=FakeCCXT(); x=ExchangeAdapter(dry_run=True,exchange=fake)
        order=x.create_limit_order('BTC/USDT','buy','0.1','100',client_order_id='test-cid')
        self.assertTrue(order['id'].startswith('paper-'))
        x.cancel_order(order['id'],'BTC/USDT')
        self.assertEqual(fake.create_calls,0); self.assertEqual(fake.cancel_calls,0)
    def test_dry_run_full_monitor_cycle(self):
        fake=FakeCCXT(); adapter=ExchangeAdapter(dry_run=True,exchange=fake)
        with tempfile.TemporaryDirectory() as d:
            db=GridDB(Path(d)/'own-grid.sqlite3'); cfg=BotConfig(); svc=TradingService(cfg,db,adapter); svc.recover(); db.update_position(running=1)
            asyncio.run(svc.tick())
            self.assertEqual(len(db.orders()),5)
            fake.last=97.5
            asyncio.run(svc.tick())
            self.assertGreater(D(db.position()['quantity']),D('0'))
            self.assertEqual(len([o for o in db.orders() if o['role']=='tp' and o['status']=='open']),4)
            self.assertEqual(fake.create_calls,0); self.assertEqual(fake.cancel_calls,0)
            db.close()
    def test_redaction(self):
        from gridbot.security import redact
        text=redact('api_key=abc123 token:secretvalue Bearer eyJhbGci.abc')
        self.assertNotIn('abc123',text); self.assertNotIn('secretvalue',text); self.assertNotIn('eyJhbGci',text)
if __name__=='__main__': unittest.main()
