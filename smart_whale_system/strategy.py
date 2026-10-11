"""استراتيجية النظام الذكي — مبنية على Flow Engine المشترك."""
import logging
from typing import Dict
import time

import config
from flow_engine import scan_flow, get_flow_engine
from mexc_trader import MexcTrader
from database import (
    get_open_positions, open_position, update_position_amount, close_position,
    log_trade, is_system_running,
)
from notifier import notify, format_entry, format_exit, format_status

logger = logging.getLogger(__name__)


class SmartStrategy:
    def __init__(self):
        self.mexc = MexcTrader()
        self._confirm_counts: Dict[str, int] = {}
        self._last_confirm_ts: Dict[str, float] = {}

    def run_cycle(self):
        if not is_system_running():
            logger.info("النظام متوقف — تخطي الدورة")
            return
        logger.info("=== بدء دورة Flow Engine ===")
        try:
            self._manage_open_positions()
            self._look_for_entries()
        except Exception as exc:
            logger.exception("خطأ في دورة النظام الذكي: %s", exc)
            notify(f"⚠️ خطأ في النظام الذكي:\n<code>{str(exc)[:300]}</code>")

    def _manage_open_positions(self):
        positions = get_open_positions()
        if not positions:
            return

        signals = {r["symbol"]: r for r in scan_flow(limit=12)}
        for pos in positions:
            price = self.mexc.get_price(pos.coin)
            if price <= 0:
                continue

            if price <= pos.stop_loss:
                self._exit_full(pos, price, "وقف خسارة")
                continue
            if price >= pos.take_profit_2:
                self._exit_full(pos, price, f"هدف 2 (+{config.TAKE_PROFIT_2_PCT:.0f}%)")
                continue
            if not pos.tp1_hit and price >= pos.take_profit_1:
                self._exit_partial(pos, price, 50, f"هدف 1 (+{config.TAKE_PROFIT_1_PCT:.0f}%)")
                continue

            signal = signals.get(pos.coin, {})
            if (
                signal
                and float(signal.get("score", 50)) <= 38
                and signal.get("direction") == "sell"
            ):
                self._exit_full(pos, price, f"انعكاس Flow قوي — score {float(signal['score']):.0f}")

    def _look_for_entries(self):
        open_pos = get_open_positions()
        if len(open_pos) >= config.MAX_OPEN_POSITIONS:
            return

        free_usdt = self.mexc.get_free_usdt()
        available = min(free_usdt, config.ALLOCATED_CAPITAL)
        used = sum(
            p.cost_usdt * (p.amount / p.original_amount)
            for p in open_pos if p.original_amount > 0
        )
        remaining_budget = max(0.0, config.ALLOCATED_CAPITAL - used)
        if remaining_budget < config.POSITION_SIZE_USDT or available < config.POSITION_SIZE_USDT:
            return

        signals = scan_flow(limit=12)
        if not signals:
            logger.info("لا توجد إشارات Flow حالياً")
            return

        engine_settings = get_flow_engine().settings
        minimum = max(1.0, float(getattr(config, "FLOW_MIN_SCORE", engine_settings.min_score_buy)))
        open_coins = {p.coin for p in open_pos}
        for sig in signals:
            coin = sig["symbol"]
            if coin in open_coins:
                continue
            if not sig.get("eligible"):
                continue
            if float(sig.get("score", 0)) < minimum:
                continue
            # تأكيدين متتاليين على الأقل قبل أي شراء.
            now = time.time()
            last = self._last_confirm_ts.get(coin, 0.0)
            if now - last > 30 * 60:
                self._confirm_counts[coin] = 0
            self._confirm_counts[coin] = self._confirm_counts.get(coin, 0) + 1
            self._last_confirm_ts[coin] = now
            if self._confirm_counts[coin] < 2:
                continue

            size = min(config.POSITION_SIZE_USDT, remaining_budget, available)
            if size < config.MIN_ORDER_USDT:
                continue
            order = self.mexc.buy_market(coin, size)
            if not order:
                continue

            pos = open_position(
                coin=coin,
                entry_price=order["price"],
                amount=order["amount"],
                cost_usdt=order["cost"],
                multiple=sig.get("whale_multiple"),
                notes=(
                    f"flow_score={sig.get('score')} "
                    f"whale_net={sig.get('whale_net_usd')} "
                    f"confirm={self._confirm_counts.get(coin, 0)}"
                ),
            )
            log_trade(
                coin=coin, side="buy", amount=order["amount"], price=order["price"],
                cost_usdt=order["cost"], reason=f"Flow Engine {sig.get('score')}/100",
                order_id=str(order.get("id")),
            )
            notify(format_entry(
                coin=coin,
                price=order["price"],
                amount=order["amount"],
                cost=order["cost"],
                multiple=float(sig.get("whale_multiple") or 0),
            ) + f"\n🧠 Flow Score: <b>{float(sig.get('score',0)):.0f}/100</b>\n✅ تأكيدات: <b>{int(sig.get('confirmation_count',0))}/2</b>")
            break

    def _exit_full(self, pos, price: float, reason: str):
        order = self.mexc.sell_market(pos.coin, amount=pos.amount)
        if not order:
            order = self.mexc.sell_market(pos.coin, percent=100)
        if not order:
            logger.error("فشل بيع %s", pos.coin)
            return
        close_position(
            position_id=pos.id, exit_price=order["price"], exit_reason=reason,
            sold_amount=order["amount"],
        )
        log_trade(
            coin=pos.coin, side="sell", amount=order["amount"], price=order["price"],
            cost_usdt=order.get("cost") or 0, reason=reason,
            order_id=str(order.get("id")),
        )
        ratio = order["amount"] / pos.original_amount if pos.original_amount else 1
        cost_part = pos.cost_usdt * ratio
        pnl = order["amount"] * order["price"] - cost_part
        pnl_pct = pnl / cost_part * 100 if cost_part else 0
        notify(format_exit(
            coin=pos.coin, entry=pos.entry_price, exit_p=order["price"],
            pnl=pnl, pnl_pct=pnl_pct, reason=reason,
        ))

    def _exit_partial(self, pos, price: float, percent: float, reason: str):
        sell_amount = pos.amount * percent / 100.0
        order = self.mexc.sell_market(pos.coin, amount=sell_amount)
        if not order:
            return
        new_amount = max(0, pos.amount - order["amount"])
        update_position_amount(pos.id, new_amount, tp1_hit=True)
        log_trade(
            coin=pos.coin, side="sell", amount=order["amount"], price=order["price"],
            cost_usdt=order.get("cost") or 0, reason=reason,
            order_id=str(order.get("id")),
        )
        ratio = order["amount"] / pos.original_amount if pos.original_amount else 0.5
        cost_part = pos.cost_usdt * ratio
        pnl = order["amount"] * order["price"] - cost_part
        pnl_pct = pnl / cost_part * 100 if cost_part else 0
        notify(
            f"🟡 <b>جني أرباح جزئي — {pos.coin}</b>\n"
            f"السبب: {reason}\n"
            f"الربح التقريبي: <b>{pnl:+.2f} USDT ({pnl_pct:+.1f}%)</b>\n"
            f"المتبقي: <code>{new_amount:.6g}</code>"
        )

    def manual_status(self) -> str:
        positions = get_open_positions()
        free = self.mexc.get_free_usdt()
        return format_status(positions, free, config.ALLOCATED_CAPITAL)
