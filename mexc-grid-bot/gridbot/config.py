from dataclasses import dataclass, field
from decimal import Decimal
import os

D = lambda x: Decimal(str(x))

def _dec_list(name, default):
    raw = os.getenv(name, ','.join(map(str, default)))
    try: return [D(x.strip()) for x in raw.split(',')]
    except Exception as exc: raise ValueError(f'{name} must be comma-separated decimal values') from exc

@dataclass
class BotConfig:
    symbol: str = 'BTC/USDT'
    total_budget: Decimal = D('100')
    initial_pct: Decimal = D('50')
    dca_drops: list[Decimal] = field(default_factory=lambda: [D('2.5'),D('5'),D('7.5'),D('10')])
    dca_allocations: list[Decimal] = field(default_factory=lambda: [D('20'),D('24'),D('26'),D('30')])
    tp_gains: list[Decimal] = field(default_factory=lambda: [D('2'),D('4'),D('7'),D('10')])
    tp_sell_pcts: list[Decimal] = field(default_factory=lambda: [D('25'),D('25'),D('25'),D('25')])
    upward_recenter_percent: Decimal = D('10')
    recenter_enabled: bool = True
    recenter_cooldown_minutes: int = 15
    keep_position_on_recenter: bool = True
    stop_loss_enabled: bool = False
    stop_loss_percent: Decimal | None = None
    dry_run: bool = True
    live_enabled: bool = False
    poll_seconds: int = 5

    @property
    def initial_budget(self): return self.total_budget * self.initial_pct / D('100')
    @property
    def dca_budget(self): return self.total_budget - self.initial_budget

    def validate(self):
        if not self.symbol or '/' not in self.symbol: raise ValueError('symbol must look like BTC/USDT')
        if self.total_budget <= 0: raise ValueError('total_budget must be positive')
        if not D('0') < self.initial_pct < D('100'): raise ValueError('initial_pct must be between 0 and 100')
        for name, values in (('dca_drops',self.dca_drops),('dca_allocations',self.dca_allocations),('tp_gains',self.tp_gains),('tp_sell_pcts',self.tp_sell_pcts)):
            if len(values) != 4: raise ValueError(name + ' must contain exactly four values')
        if any(x <= 0 or x >= 100 for x in self.dca_drops): raise ValueError('DCA drops must be positive and below 100%')
        if any(x < 0 for x in self.dca_allocations) or sum(self.dca_allocations) != D('100'): raise ValueError('DCA allocations must be non-negative and total 100%')
        if any(x < 0 for x in self.tp_gains): raise ValueError('TP gains cannot be negative')
        if any(x < 0 for x in self.tp_sell_pcts) or sum(self.tp_sell_pcts) != D('100'): raise ValueError('TP sell percentages must be non-negative and total 100%')
        if self.upward_recenter_percent <= 0: raise ValueError('recenter breakout percent must be positive')
        if self.recenter_cooldown_minutes < 0 or self.poll_seconds < 1: raise ValueError('cooldown must be non-negative and poll_seconds positive')
        if self.stop_loss_enabled and (self.stop_loss_percent is None or self.stop_loss_percent <= 0): raise ValueError('enabled stop loss requires an explicit positive percentage')
        if self.stop_loss_enabled: raise ValueError('stop-loss execution is not implemented in this build; keep GRID_STOP_LOSS_ENABLED=false')
        if not self.dry_run and not self.live_enabled: raise ValueError('LIVE mode requires LIVE_TRADING_ENABLED=true explicitly')
        return self

def config_from_env():
    cfg = BotConfig(
        symbol=os.getenv('GRID_SYMBOL','BTC/USDT').strip(),
        total_budget=D(os.getenv('GRID_TOTAL_BUDGET','100')),
        initial_pct=D(os.getenv('GRID_INITIAL_PCT','50')),
        dca_drops=_dec_list('GRID_DCA_DROPS','2.5,5,7.5,10'.split(',')),
        dca_allocations=_dec_list('GRID_DCA_ALLOCATIONS','20,24,26,30'.split(',')),
        tp_gains=_dec_list('GRID_TP_GAINS','2,4,7,10'.split(',')),
        tp_sell_pcts=_dec_list('GRID_TP_SELL_PCTS','25,25,25,25'.split(',')),
        upward_recenter_percent=D(os.getenv('GRID_RECENTER_PERCENT','10')),
        recenter_enabled=os.getenv('GRID_RECENTER_ENABLED','true').lower() in ('1','true','yes'),
        recenter_cooldown_minutes=int(os.getenv('GRID_RECENTER_COOLDOWN_MINUTES','15')),
        stop_loss_enabled=os.getenv('GRID_STOP_LOSS_ENABLED','false').lower() in ('1','true','yes'),
        stop_loss_percent=D(os.environ['GRID_STOP_LOSS_PERCENT']) if os.getenv('GRID_STOP_LOSS_PERCENT') else None,
        dry_run=os.getenv('DRY_RUN','true').lower() in ('1','true','yes'),
        live_enabled=os.getenv('LIVE_TRADING_ENABLED','false').lower() in ('1','true','yes'),
        poll_seconds=int(os.getenv('POLL_SECONDS','5')),
    )
    return cfg.validate()
