import asyncio, tempfile, unittest
from decimal import Decimal as D, ROUND_DOWN
from pathlib import Path
from gridbot.config import BotConfig
from gridbot.db import GridDB
from gridbot.service import TradingService

class FakeExchange:
    def __init__(self): self.orders={}; self.next=0; self.real_mutations=0; self.price=D('100')
    def market(self,s): return {'spot':True,'limits':{}}
    def limits(self,s): return {'amount_min':D('0.000001'),'cost_min':D('1')}
    def amount_to_precision(self,s,a): return format(D(a).quantize(D('0.00000001'),rounding=ROUND_DOWN),'f')
    def price_to_precision(self,s,p): return format(D(p).quantize(D('0.00000001')),'f')
    def create_limit_order(self,s,side,amount,price,params=None,client_order_id=None):
        self.real_mutations += 1; self.next+=1; oid=str(self.next)
        o={'id':oid,'clientOrderId':client_order_id,'status':'open','symbol':s,'side':side,'amount':float(amount),'price':float(price),'filled':0,'cost':0,'info':{}}
        self.orders[oid]=o; return o
    def cancel_order(self,oid,s):
        self.real_mutations += 1; self.orders[str(oid)]['status']='canceled'; return self.orders[str(oid)]
    def fetch_open_orders(self,s): return [o for o in self.orders.values() if o['status']=='open']
    def fetch_orders(self,s,since=None,limit=100): return list(self.orders.values())
    def fetch_order(self,oid,s,params=None): return self.orders.get(str(oid))
    def fetch_ohlcv(self,s,timeframe='3m',limit=3): return [[1000,100,101,99,100,1],[181000,100,101,99,100,1]]
    def ticker(self,s): return {'last':str(self.price)}
    def simulate_fills(self,s,last_price):
        for o in self.orders.values():
            if o['status']!='open': continue
            hit=(o['side']=='buy' and D(str(last_price))<=D(str(o['price']))) or (o['side']=='sell' and D(str(last_price))>=D(str(o['price'])))
            if hit: o.update(status='closed',filled=o['amount'],cost=o['amount']*o['price'],average=o['price'])
    def seed_paper_order(self,o): return o

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=GridDB(Path(self.tmp.name)/'isolated.sqlite3'); self.cfg=BotConfig(); self.ex=FakeExchange(); self.s=TradingService(self.cfg,self.db,self.ex); self.s.recover()
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def test_recovery_gates_orders(self):
        s=TradingService(self.cfg,self.db,self.ex)
        with self.assertRaises(RuntimeError): asyncio.run(s.tick())
    def test_tick_creates_initial_and_four_dca(self):
        self.db.update_position(running=1)
        asyncio.run(self.s.tick())
        roles=[o['role'] for o in self.db.orders()]
        self.assertEqual(roles.count('initial'),1); self.assertEqual(roles.count('dca'),4)
        self.assertLessEqual(sum(D(o['amount'])*D(o['price']) for o in self.db.open_orders() if o['side']=='buy'),self.cfg.total_budget)
    def test_buy_fill_rebuilds_four_tp(self):
        self.db.update_position(running=1,grid_reference='100')
        order=self.s._persist_order('initial',0,'buy',100,D('0.5'))
        remote=self.ex.orders[order['exchange_order_id']]; remote.update(status='closed',filled=0.5,cost=50,average=100)
        self.s._sync_order(self.db.order_by_client_id(order['client_order_id']),remote)
        tps=[o for o in self.db.orders() if o['role']=='tp' and o['status']=='open']
        self.assertEqual(len(tps),4)
        self.assertEqual(sum(D(o['amount']) for o in tps),D('0.5'))
    def test_emergency_stop_prevents_new_orders(self):
        self.db.update_position(emergency_stop=1,running=1)
        before=len(self.db.orders()); asyncio.run(self.s.tick())
        self.assertEqual(len(self.db.orders()),before)
    def test_restart_recovery_does_not_duplicate_orders(self):
        self.db.update_position(running=1,grid_reference='100')
        asyncio.run(self.s.tick()); before=len(self.db.orders())
        self.ex.price=D('102')
        restarted=TradingService(self.cfg,self.db,self.ex); self.assertNotEqual(restarted.recover(),'FROZEN')
        asyncio.run(restarted.tick()); self.assertEqual(len(self.db.orders()),before)
    def test_tp_one_hundred_percent_creates_one_sell_order(self):
        self.cfg.tp_sell_pcts=[D('100'),D('0'),D('0'),D('0')]
        self.db.update_position(quantity='1',cost='100',average_entry='100')
        self.s._rebuild_tps()
        active=[o for o in self.db.open_orders() if o['role']=='tp']
        self.assertEqual(len(active),1); self.assertEqual(int(active[0]['level']),1)
        self.assertEqual(D(active[0]['amount']),D('1'))
    def test_dca_fill_rebuilds_tps_from_new_average(self):
        self.db.update_position(quantity='1',cost='100',average_entry='100',spent='100')
        self.s._rebuild_tps(); old_ids={o['exchange_order_id'] for o in self.db.open_orders() if o['role']=='tp'}
        row=self.s._persist_order('dca',1,'buy',80,D('0.5'))
        remote=self.ex.orders[row['exchange_order_id']]; remote.update(status='closed',filled=0.5,cost=40,average=80)
        self.s._sync_order(self.db.order_by_client_id(row['client_order_id']),remote)
        active=[o for o in self.db.open_orders() if o['role']=='tp']
        self.assertEqual(len(active),4)
        self.assertTrue(all(o['exchange_order_id'] not in old_ids for o in active))
        self.assertEqual(D(self.db.position()['average_entry']),D('280')/D('3'))
    def test_timeout_freezes_without_retry(self):
        class TimeoutExchange(FakeExchange):
            def create_limit_order(self,*args,**kwargs): raise TimeoutError('simulated timeout')
        ex=TimeoutExchange(); s=TradingService(self.cfg,self.db,ex); s.recovered=True
        with self.assertRaises(RuntimeError): s._persist_order('initial',0,'buy',100,D('0.5'))
        self.assertEqual(len(self.db.orders()),1); self.assertEqual(self.db.position()['state'],'FROZEN')
        asyncio.run(s.tick())
        self.assertEqual(len(self.db.orders()),1)
    def test_dca4_fill_enters_defensive_state(self):
        self.db.update_position(quantity='1',cost='100',average_entry='100',spent='100',running=1)
        row=self.s._persist_order('dca',4,'buy',90,D('0.1'))
        remote=self.ex.orders[row['exchange_order_id']]; remote.update(status='closed',filled=0.1,cost=9,average=90)
        self.s._sync_order(self.db.order_by_client_id(row['client_order_id']),remote)
        self.assertEqual(self.db.position()['state'],'DEFENSIVE')
        self.assertEqual(self.db.position()['dca4_complete'],1)
    def test_unknown_order_is_not_retried(self):
        row=self.db.insert_order_intent('known-cid','BTC/USDT','dca',1,'buy','90','1')
        self.db.update_order('known-cid',status='unknown',last_error='timeout')
        state=self.s.recover()
        self.assertEqual(state,'FROZEN')
        self.assertEqual(len(self.db.orders()),1)
    def test_cancel_must_be_confirmed_before_tp_replacement(self):
        self.db.update_position(quantity='1',cost='100',average_entry='100',spent='100')
        self.s._rebuild_tps()
        old=[o for o in self.db.open_orders() if o['role']=='tp']
        class AmbiguousCancel(FakeExchange):
            def cancel_order(self, oid, symbol):
                self.real_mutations += 1
                return {'id': oid, 'status': 'canceled'}
            def fetch_order(self, oid, symbol, params=None):
                return self.orders.get(str(oid))  # remains open, so cancel is not confirmed
        ex=AmbiguousCancel(); ex.orders=self.ex.orders; ex.next=self.ex.next
        svc=TradingService(self.cfg,self.db,ex); svc.recovered=True
        before=len(self.db.orders())
        with self.assertRaises(RuntimeError): svc._rebuild_tps()
        self.assertEqual(self.db.position()['state'],'FROZEN')
        self.assertEqual(len(self.db.orders()),before)
        self.assertTrue(any(o['status']=='open' for o in old))

    def test_incremental_partial_fill_cost_is_accounted_correctly(self):
        row=self.s._persist_order('initial',0,'buy',100,D('2'))
        local=self.db.order_by_client_id(row['client_order_id'])
        remote=self.ex.orders[row['exchange_order_id']]
        remote.update(status='open',filled=1,cost=100,average=100)
        self.s._sync_order(local,remote,rebuild_tps=False)
        remote.update(filled=2,cost=220,average=110)
        self.s._sync_order(self.db.order_by_client_id(row['client_order_id']),remote,rebuild_tps=False)
        p=self.db.position()
        self.assertEqual(D(p['quantity']),D('2'))
        self.assertEqual(D(p['cost']),D('220'))
        self.assertEqual(D(p['average_entry']),D('110'))

    def test_sell_overfill_freezes_instead_of_erasing_position(self):
        self.db.update_position(quantity='1',cost='100',average_entry='100',spent='100')
        row=self.s._persist_order('tp',1,'sell',110,D('1'))
        remote=self.ex.orders[row['exchange_order_id']]
        remote.update(status='closed',filled=2,cost=220,average=110)
        with self.assertRaises(RuntimeError):
            self.s._sync_order(self.db.order_by_client_id(row['client_order_id']),remote,rebuild_tps=False)
        self.assertEqual(self.db.position()['state'],'FROZEN')
        self.assertEqual(D(self.db.position()['quantity']),D('1'))

    def test_decimal_average(self):
        p=self.s._accounting(); p.add_fill('1','100'); p.add_fill('1','90'); self.assertEqual(p.average_entry,D('95'))

if __name__=='__main__': unittest.main()
