import unittest
from gridbot.recovery import reconcile
class T(unittest.TestCase):
 def test_match_id(self):
  r=reconcile([{"exchange_order_id":"1","client_order_id":"cid"}],[{"id":"1","clientOrderId":"cid"}]); self.assertEqual(r[0]["action"],"matched")
 def test_unknown_must_verify(self):
  r=reconcile([{"exchange_order_id":None,"client_order_id":"cid"}],[]); self.assertEqual(r[0]["action"],"verify_order_history_before_retry")
if __name__=="__main__": unittest.main()
