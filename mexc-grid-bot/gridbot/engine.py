from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from .config import D

def dca_quote_budgets(cfg):
    cfg.validate(); return [cfg.dca_budget*x/D('100') for x in cfg.dca_allocations]
def dca_prices(reference,cfg):
    r=D(reference); return [r*(D('1')-x/D('100')) for x in cfg.dca_drops]
def tp_prices(average_entry,cfg):
    a=D(average_entry); return [a*(D('1')+x/D('100')) for x in cfg.tp_gains]
def can_place_dca(index,dca4_complete,emergency_stopped): return 0 <= index < 4 and not dca4_complete and not emergency_stopped
def budget_remaining(cfg,spent): return max(D('0'),cfg.total_budget-D(spent))
def quote_to_quantity(budget,price):
    p=D(price)
    if p<=0: raise ValueError('price must be positive')
    return (D(budget)/p).quantize(D('0.00000001'),rounding=ROUND_DOWN)
def recenter_confirmed(reference,close_price,candle_closed,candle_open_time,last_recenter_time,now,cfg):
    if not cfg.recenter_enabled or not candle_closed: return False
    if last_recenter_time is not None and now < last_recenter_time+timedelta(minutes=cfg.recenter_cooldown_minutes): return False
    return D(close_price) > D(reference)*(D('1')+cfg.upward_recenter_percent/D('100')) and (last_recenter_time is None or candle_open_time>last_recenter_time)
def validate_tp_sellable(quantity,min_amount,amount_precision):
    q=D(quantity).quantize(D(amount_precision),rounding=ROUND_DOWN)
    return q if q>=D(min_amount) else D('0')

class Position:
    """Decimal-only accounting helper; exchange cumulative fills must be applied as deltas."""
    def __init__(self, quantity='0', cost='0', spent='0'):
        self.quantity,self.cost,self.spent=D(quantity),D(cost),D(spent)
    @property
    def average_entry(self): return self.cost/self.quantity if self.quantity>0 else D('0')
    def add_fill(self,quantity,price,quote_cost=None):
        q,p=D(quantity),D(price); cost=D(quote_cost) if quote_cost is not None else q*p
        if q<=0 or p<=0 or cost<0: raise ValueError('invalid fill')
        self.quantity+=q; self.cost+=cost; self.spent+=cost
    def sell_fill(self,quantity,price):
        q,p=D(quantity),D(price)
        if q<=0 or q>self.quantity: raise ValueError('sell quantity exceeds position')
        avg=self.average_entry; self.quantity-=q; self.cost=max(D('0'),self.cost-avg*q)
        return (p-avg)*q
