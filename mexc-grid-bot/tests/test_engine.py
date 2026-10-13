import unittest
from decimal import Decimal as D
from datetime import datetime,timedelta,timezone
from gridbot.config import BotConfig
from gridbot.engine import Position,dca_quote_budgets,dca_prices,tp_prices,can_place_dca,budget_remaining,recenter_confirmed
class T(unittest.TestCase):
 def setUp(self): self.c=BotConfig()
 def test_100_split_50_50(self): self.assertEqual(self.c.initial_budget,D("50")); self.assertEqual(self.c.dca_budget,D("50")); self.assertEqual(sum(dca_quote_budgets(self.c)),D("50.0"))
 def test_dca_sum(self): self.c.validate(); self.assertEqual(sum(self.c.dca_allocations),D("100"))
 def test_tp_sum(self): self.c.validate(); self.assertEqual(sum(self.c.tp_sell_pcts),D("100"))
 def test_tp1_100_allowed(self): self.c.tp_sell_pcts=[D("100"),D("0"),D("0"),D("0")]; self.c.validate()
 def test_weighted_average(self):
  p=Position(); p.add_fill(1,100); p.add_fill(1,90); self.assertEqual(p.average_entry,D("95")); p.add_fill(2,80); self.assertEqual(p.average_entry,D("87.5"))
 def test_budget_cap(self): self.assertEqual(budget_remaining(self.c,99),D("1")); self.assertEqual(budget_remaining(self.c,101),D("0"))
 def test_no_dca5(self): self.assertFalse(can_place_dca(4,False,False)); self.assertFalse(can_place_dca(3,True,False)); self.assertFalse(can_place_dca(0,False,True)); self.assertTrue(can_place_dca(3,False,False))
 def test_close_required(self):
  now=datetime.now(timezone.utc); candle=now-timedelta(minutes=3)
  self.assertFalse(recenter_confirmed(100,111,False,candle,None,now,self.c)); self.assertTrue(recenter_confirmed(100,111,True,candle,None,now,self.c))
 def test_cooldown(self):
  now=datetime.now(timezone.utc); self.assertFalse(recenter_confirmed(100,111,True,now-timedelta(minutes=3),now-timedelta(minutes=5),now,self.c))
 def test_levels(self): self.assertEqual(dca_prices(100,self.c),[D("97.500"),D("95.00"),D("92.500"),D("90.0")]); self.assertEqual(tp_prices(100,self.c),[D("102.00"),D("104.00"),D("107.00"),D("110.00")])
if __name__=="__main__": unittest.main()
