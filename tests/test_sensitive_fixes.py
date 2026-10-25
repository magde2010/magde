import asyncio
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from compact_plan import compact_coin_plan, finite_amount, position_remaining
from rebalancer import Rebalancer
from trading_guard import TradingGuard


def coin(**kwargs):
    values = dict(id=1, symbol="BTC", amount=1, remaining_amount=1,
                  tp1_price=10, tp2_price=12, tp3_price=0,
                  tp1_order_id="a", tp2_order_id="b", current_sl_price=0)
    return NS(**(values | kwargs))


def test_zero_remaining_never_falls_back_to_original():
    assert position_remaining(coin(remaining_amount=0)) == 0
    assert position_remaining(coin(remaining_amount=None)) == 1


@pytest.mark.parametrize("raw", ["NaN", "nan", "inf", "-inf", "1e309", "0", "-5"])
def test_financial_input_rejects_invalid(raw):
    with pytest.raises(ValueError):
        finite_amount(raw, 5)


def test_financial_input_accepts_valid():
    assert finite_amount("1,000.25$", 5) == 1000.25


def test_compact_plan_dollars_and_disabled_stop_and_level():
    rows = [NS(portfolio_coin_id=1, level=i, planned_usdt=value, filled_cost=0,
               status="open", order_id=str(i)) for i, value in ((1, 4), (2, 3))]
    assert compact_coin_plan(coin(), 50, 50, rows) == [
        "BTC/TP1/5.00/TP2/6.00/TP3/OFF",
        "BTC/DC1/4.00/DC2/3.00/DC3/0", "BTC/SL/OFF",
    ]


def test_plan_does_not_claim_unconfirmed_order_or_false_trailing():
    rows = [NS(portfolio_coin_id=1, level=1, planned_usdt=4, filled_cost=1,
               status="manual_review", order_id=None)]
    result = compact_coin_plan(coin(tp1_order_id=None), 40, 40, rows)
    assert result[0] == "BTC/TP1/4.00?/TP2/4.80/TP3/OFF"
    assert result[1] == "BTC/DC1/3.00?/DC2/0/DC3/0"
    assert compact_coin_plan(coin(current_sl_price=9), 40, 40, [], True)[0].endswith("TP3/TRAILING")


def test_inactive_and_foreign_dca_not_reported_as_live():
    rows = [NS(portfolio_coin_id=cid, level=1, planned_usdt=100, filled_cost=0,
               status=status, order_id="x") for cid, status in ((2, "open"), (1, "filled"), (1, "cancelled"))]
    assert compact_coin_plan(coin(), 40, 40, rows)[1] == "BTC/DC1/0/DC2/0/DC3/0"


def test_open_partial_buy_is_not_final_and_is_fetched_until_closed():
    class Client:
        quote = "USDT"
        def fetch_order(self, *args, **kwargs):
            return dict(id="buy", status="closed", filled=0.8)
    with patch("rebalancer.time.sleep"):
        result = Rebalancer(Client())._confirm_market_buy(
            "BTC", dict(id="buy", status="open", filled=0.1), 10,
        )
    assert result["filled"] == 0.8


def test_persistently_open_partial_buy_stays_unresolved():
    class Client:
        quote = "USDT"
        def fetch_order(self, *args, **kwargs):
            return dict(id="buy", status="open", filled=0.1)
    with patch("rebalancer.time.sleep"), pytest.raises(RuntimeError):
        Rebalancer(Client())._confirm_market_buy("BTC", dict(id="buy", status="open", filled=0.1), 10)


def test_guard_serializes_nested_operations_and_releases_after_error():
    async def scenario():
        guard = TradingGuard()
        log = []
        @guard.serialized
        async def child():
            log.append("child")
        @guard.serialized
        async def operation(name):
            log.append(name + " begin")
            await asyncio.sleep(0)
            await child()
            log.append(name + " end")
        await asyncio.gather(operation("stop"), operation("monitor"))
        assert log == ["stop begin", "child", "stop end", "monitor begin", "child", "monitor end"]
        @guard.serialized
        async def failure():
            raise ValueError("failure")
        with pytest.raises(ValueError):
            await failure()
        await operation("after")
    asyncio.run(asyncio.wait_for(scenario(), 2))


def test_guard_cancellation_waits_for_inflight_operation():
    async def scenario():
        guard = TradingGuard()
        entered, release = asyncio.Event(), asyncio.Event()
        log = []
        @guard.serialized
        async def first():
            entered.set()
            await release.wait()
            log.append("first complete")
        @guard.serialized
        async def second():
            log.append("second")
        task = asyncio.create_task(first())
        await entered.wait()
        task.cancel()
        other = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert not other.done()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await other
        assert log == ["first complete", "second"]
    asyncio.run(asyncio.wait_for(scenario(), 2))


def test_open_buy_is_persistable_as_pending_not_executed():
    class Client:
        quote = "USDT"
        def get_free_usdt(self):
            return 100
        def create_market_buy_usdt(self, *args):
            return dict(id="open-buy", status="open", filled=0.1)
        def fetch_order(self, *args, **kwargs):
            return dict(id="open-buy", status="open", filled=0.1)
    with patch("rebalancer.time.sleep"):
        result = Rebalancer(Client()).start_portfolio(["BTC"], 10)
    assert not result["executed"]
    assert result["pending_confirmation"][0]["order_id"] == "open-buy"


def test_missing_tp_order_does_not_look_confirmed():
    class Client:
        quote = "USDT"
        def create_limit_sell(self, *args):
            return None
    result = Rebalancer(Client()).place_tp_orders(
        [dict(symbol="TAO", amount=0.01, entry_price=275)],
        tp1_pct=10, tp2_pct=20, tp1_sell_pct=40, tp2_sell_pct=40,
    )[0]
    assert result["tp1_order_id"] is None
    assert "حد السوق" in result["error"]
    assert "TP1" in result["error"] and "TP2" in result["error"]
