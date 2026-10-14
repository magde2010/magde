from typing import Dict, List, Optional, Any
from mexc_client import MexcClient
import logging
import time
logger = logging.getLogger(__name__)


def log_trade(event: str, symbol: str = "", **kwargs):
    """لوج موحّد يظهر في Railway عند أي حدث تداول مهم."""
    parts = [f"[TRADE] {event}"]
    if symbol:
        parts.append(f"symbol={symbol}")
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, float):
            parts.append(f"{k}={v:.6g}")
        else:
            parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


# ===== إدارة يدوية ثابتة للأهداف ووقف الخسارة =====
INITIAL_SL_PCT = 3.0
# إعادة الدخول الاختيارية: السعر يرجع فوق الاستوب المضروب بهذا القدر
REENTRY_RECOVER_PCT = 1.5
# تبريد خفيف بالثواني بين محاولات إعادة الدخول لنفس العملة (منع سبام أوامر)
REENTRY_COOLDOWN_SEC = 90
# حد أدنى لقيمة المركز عشان نعتبره مفتوح (dust)
MIN_POSITION_USDT = 0.8


def reentry_trigger_price(price: float, recovery_pct: float) -> float:
    return float(price or 0) * (1.0 + float(recovery_pct or 0) / 100.0)


class Rebalancer:
    """Buy / sell / multi-TP placement & cloud SL monitor. No rebalancing."""

    def __init__(self, client: MexcClient):
        self.client = client
        self.quote = client.quote
        # A single empty/partial exchange response must never close a position.
        self._balance_zero_streak = {}

    def _confirmed_total_amount(self, symbol: str):
        """Return total wallet amount, or None when the balance read is unreliable."""
        exchange = getattr(self.client, "exchange", None)
        if exchange is not None and hasattr(exchange, "fetch_balance"):
            try:
                raw = exchange.fetch_balance()
                if not isinstance(raw, dict) or not ("total" in raw or "free" in raw):
                    return None
                key = self.client.normalize_asset_symbol(symbol)
                total = raw.get("total") or {}
                free = raw.get("free") or {}
                return max(
                    float(total.get(key) or total.get(symbol) or 0),
                    float(free.get(key) or free.get(symbol) or 0),
                )
            except Exception as exc:
                logger.warning("Balance read failed for %s; preserving position: %s", symbol, exc)
                return None
        try:
            return max(
                float(self.client.get_total_amount(symbol) or 0),
                float(self.client.get_free_amount(symbol) or 0),
            )
        except Exception:
            return None

    def _confirm_market_sell(self, symbol: str, order: Dict[str, Any], requested: float) -> float:
        """Return filled quantity; reject unconfirmed or partial stop-loss sells."""
        checked = order or {}
        order_id = str(checked.get("id") or "")
        for _ in range(3):
            try:
                filled = float(checked.get("filled") or 0)
            except (TypeError, ValueError):
                filled = 0.0
            if filled > 0:
                break
            if not order_id or not hasattr(self.client, "fetch_order"):
                break
            time.sleep(0.4)
            checked = self.client.fetch_order(order_id, symbol) or checked
        try:
            filled = float(checked.get("filled") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        tolerance = max(float(requested) * 1e-6, 1e-12)
        if filled <= 0:
            raise RuntimeError(f"لم تؤكد MEXC تنفيذ بيع وقف الخسارة (order={order_id or 'بدون رقم'})")
        if filled + tolerance < float(requested):
            raise RuntimeError(
                f"تم تنفيذ بيع جزئي فقط عند الاستوب: {filled:.8g}/{float(requested):.8g}"
            )
        return min(filled, float(requested))

    def fetch_candles(self, symbol: str, timeframe: str = "1h", limit: int = 80) -> List:
        pair = f"{symbol}/{self.quote}"
        try:
            return self.client.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit) or []
        except Exception as e:
            logger.warning("OHLCV failed for %s: %s", symbol, e)
            return []


    def calculate_targets(self, coins: List[str], method: str = "equal") -> Dict[str, float]:
        if not coins:
            return {}
        pct = 100.0 / len(coins)
        return {c: pct for c in coins}

    def start_portfolio(
        self,
        coins: List[str],
        total_usdt: float,
        method: str = "equal",
        min_trade_usdt: float = 2.0,
        dry_run: bool = False,
    ) -> Dict:
        """Buy coins using up to total_usdt only."""
        results = {
            "action": "start",
            "total_usdt": total_usdt,
            "executed": [],
            "errors": [],
            "dry_run": dry_run,
        }
        import config
        total_usdt = float(total_usdt or 0.0)
        min_order = max(2.0, float(config.PORTFOLIO_MIN_ORDER_USDT), float(min_trade_usdt or 0.0))
        if total_usdt <= 0:
            results["errors"].append("مبلغ شراء المحفظة يجب أن يكون أكبر من صفر.")
            return results
        free_usdt = self.client.get_free_usdt()
        if free_usdt < total_usdt:
            results["errors"].append(
                f"رصيد USDT الحر غير كافٍ. المتاح: `{free_usdt:.2f}$` | المطلوب: `{total_usdt:.2f}$`"
            )
            return results

        # Keep each order above the exchange minimum by reducing the number
        # of selected coins instead of silently shrinking the portfolio budget.
        max_coins_by_budget = int(total_usdt // min_order)
        if max_coins_by_budget <= 0:
            results["errors"].append(
                f"مبلغ المحفظة `{total_usdt:.2f}$` أقل من الحد الأدنى للأمر `{min_order:.2f}$`."
            )
            return results
        selected_coins = list(coins or [])[:max_coins_by_budget]
        if not selected_coins:
            results["errors"].append("لا توجد عملات صالحة للشراء.")
            return results
        if len(selected_coins) < len(coins):
            results["errors"].append(
                f"تم تقليص الشراء إلى `{len(selected_coins)}/{len(coins)}` عملة حتى لا يقل الأمر عن `{min_order:.2f}$`."
            )
        # Equal allocation over selected coins; do not silently reduce budget.
        n = len(selected_coins)
        usdt_each = float(total_usdt) / n
        targets = {c: (100.0 / n) for c in selected_coins}
        for coin, pct in targets.items():
            usdt_for_coin = usdt_each
            if usdt_for_coin < min_order:
                results["errors"].append(f"`{coin}`: المبلغ أقل من الحد الأدنى ({usdt_for_coin:.2f}$)")
                continue
            try:
                import config
                change = None
                if hasattr(self.client, "get_ticker_change_pct"):
                    change = self.client.get_ticker_change_pct(f"{coin}/{self.quote}")
                max_gain = float(getattr(config, "PORTFOLIO_MAX_ENTRY_GAIN_PCT", 5.0) or 5.0)
                if change is not None and float(change) > max_gain:
                    results["errors"].append(f"`{coin}`: تم تخطي الشراء لأن الارتفاع خلال 24 ساعة {float(change):.2f}% أكبر من الحد الآمن {max_gain:.2f}%.")
                    continue
                if dry_run:
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "buy",
                        "usdt": usdt_for_coin,
                        "status": "dry_run",
                    })
                else:
                    order = self.client.create_market_buy_usdt(coin, usdt_for_coin)
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "buy",
                        "usdt": usdt_for_coin,
                        "status": "filled",
                        "order_id": order.get("id") if order else None,
                    })
            except Exception as e:
                results["errors"].append({coin: str(e)})
        return results

    def stop_portfolio(
        self,
        coins: List[str],
        dry_run: bool = False,
        amount_overrides: Optional[Dict[str, float]] = None,
    ) -> Dict:
        """Sell all holdings of the given coins."""
        results = {
            "action": "stop",
            "executed": [],
            "errors": [],
            "dry_run": dry_run,
            "total_sold_usdt": 0.0,
        }
        balances = self.client.get_balance()
        prices = self.client.get_all_prices(coins)
        for coin in coins:
            override = (amount_overrides or {}).get(coin)
            if override is None:
                amount = float(balances.get(coin, 0.0))
            else:
                amount = min(float(override or 0.0), float(balances.get(coin, 0.0)))
            if amount <= 0:
                continue
            amount = amount * 0.999
            usdt_value = amount * prices.get(coin, 0.0)
            try:
                if dry_run:
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "sell",
                        "amount": amount,
                        "usdt": usdt_value,
                        "status": "dry_run",
                    })
                    results["total_sold_usdt"] += usdt_value
                else:
                    order = self.client.create_market_order(
                        symbol=f"{coin}/{self.quote}", side="sell", amount=amount
                    )
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "sell",
                        "amount": amount,
                        "usdt": usdt_value,
                        "status": "filled",
                        "order_id": order.get("id") if order else None,
                    })
                    results["total_sold_usdt"] += usdt_value
            except Exception as e:
                results["errors"].append({coin: str(e)})
        return results

    def cancel_tp_orders(self, coins_data: List[Dict[str, Any]]) -> Dict:
        """Cancel TP order IDs; أوامر منتهية مسبقاً لا تُعتبر فشلاً."""
        result = {"cancelled": [], "errors": []}
        for item in coins_data:
            symbol = item.get("symbol")
            for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                order_id = item.get(key)
                if not order_id:
                    continue
                try:
                    # strict=False: filled/cancelled مسبقاً → نجاح صامت
                    self.client.cancel_order(order_id, f"{symbol}/{self.quote}", strict=False)
                    result["cancelled"].append(order_id)
                except Exception as exc:
                    msg = str(exc).lower()
                    if any(x in msg for x in ("not found", "unknown", "already", "filled", "canceled", "cancelled")):
                        result["cancelled"].append(order_id)
                    else:
                        result["errors"].append({"symbol": symbol, "order_id": order_id, "error": str(exc)})
                        logger.warning("cancel_tp %s %s: %s", symbol, order_id, exc)
        return result

    def place_tp_orders(
        self,
        coins_data: List[Dict[str, Any]],
        tp1_pct: float = 0.0,
        tp2_pct: float = 0.0,
        tp3_pct: float = 0.0,
        stop_loss_pct: float = None,
        tp1_sell_pct: float = 0.0,
        tp2_sell_pct: float = 0.0,
        skip_stages: Optional[List[str]] = None,
        control_mode: str = "manual",
    ) -> List[Dict]:
        """تهيئة أهداف TP اليدوية وأمر وقف الخسارة لكل مركز."""
        results = []
        sl_pct = float(stop_loss_pct) if stop_loss_pct and float(stop_loss_pct) > 0 else INITIAL_SL_PCT
        for item in coins_data:
            symbol = item["symbol"]
            amount = float(item.get("amount") or 0)
            entry = float(item.get("entry_price") or 0)
            if amount <= 0:
                amount = self.client.get_free_amount(symbol)
                if amount <= 0:
                    amount = self.client.get_total_amount(symbol)
                amount = amount * 0.998
            if entry <= 0:
                entry = self.client.get_ticker_price(f"{symbol}/{self.quote}")
            if amount <= 0 or entry <= 0:
                results.append({"symbol": symbol, "error": "no amount or price"})
                continue

            # إلغاء أوامر بيع قديمة (أهداف ثابتة من النظام السابق)
            for oid_key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                oid = item.get(oid_key)
                if oid:
                    try:
                        self.client.cancel_order(oid, f"{symbol}/{self.quote}")
                    except Exception:
                        pass

            # الأهداف والوقف يحددهما المستخدم فقط.
            sl_pct_use = sl_pct

            sl = entry * (1.0 - sl_pct_use / 100.0)
            tp1_price = entry * (1.0 + float(tp1_pct or 0) / 100.0) if float(tp1_pct or 0) > 0 else 0.0
            tp2_price = entry * (1.0 + float(tp2_pct or 0) / 100.0) if float(tp2_pct or 0) > 0 else 0.0
            # اليدوي يستخدم هدفين فقط؛ الـ20% الباقية تُدار بوقف متحرك.
            tp3_price = 0.0
            order_ids = {"tp1_order_id": None, "tp2_order_id": None, "tp3_order_id": None}
            if item.get("place_exchange_orders", True):
                sell1 = float(item.get("tp1_sell_pct_override", tp1_sell_pct) or 0)
                sell2 = float(item.get("tp2_sell_pct_override", tp2_sell_pct) or 0)
                if min(tp1_price, tp2_price) <= 0 or sell1 <= 0 or sell2 <= 0 or sell1 + sell2 >= 100.0:
                    results.append({"symbol": symbol, "error": "invalid manual TP percentages"})
                    continue
                # أمران فقط؛ النسبة المتبقية (20% مع إعداد 40/40) تظل للوقف المتحرك.
                quantities = (
                    ("tp1_order_id", amount * sell1 / 100.0, tp1_price),
                    ("tp2_order_id", amount * sell2 / 100.0, tp2_price),
                )
                placed_ids = []
                skip_notes = []
                try:
                    for key, qty, target in quantities:
                        if qty <= 0 or target <= 0:
                            continue
                        # قيمة الأمر أقل من ~1$ → تخطي بدون فشل كامل
                        if qty * target < 1.0:
                            skip_notes.append(f"{key}: قيمة صغيرة تم تخطيها")
                            continue
                        order = self.client.create_limit_sell(symbol, qty, target)
                        if not order or not order.get("id"):
                            # MEXC رفضت أو أقل من الحد الأدنى — لا نلغي باقي الأوامر
                            skip_notes.append(
                                f"{key}: المنصة لم تقبل الأمر (حد أدنى/دقة/كمية)"
                            )
                            logger.warning(
                                "manual TP skipped %s %s qty=%s price=%s",
                                symbol, key, qty, target,
                            )
                            continue
                        order_ids[key] = order.get("id")
                        placed_ids.append(order.get("id"))
                except Exception as exc:
                    logger.exception("manual TP orders error %s", symbol)
                    for oid in placed_ids:
                        try:
                            self.client.cancel_order(oid, f"{symbol}/{self.quote}")
                        except Exception:
                            pass
                    # حتى مع الفشل نُرجع استوب يدوي عشان المراقبة تشتغل
                    results.append({
                        "symbol": symbol,
                        "amount": amount,
                        "entry_price": entry,
                        "tp1_price": tp1_price,
                        "tp2_price": tp2_price,
                        "tp3_price": tp3_price,
                        "stop_loss_price": sl,
                        "sl_price": sl,
                        "original_sl_price": sl,
                        "tp1_order_id": None,
                        "tp2_order_id": None,
                        "tp3_order_id": None,
                        "tp_order_id": None,
                        "mode": "manual",
                                                "sl_pct": sl_pct_use,
                        "error": f"فشل وضع أهداف يدوية: {exc}",
                        "warning": str(exc),
                    })
                    continue
                if skip_notes and not placed_ids:
                    # مفيش ولا أمر اتحط — نكمل بالاستوب فقط
                    logger.warning("manual %s: no TP placed (%s)", symbol, "; ".join(skip_notes))
            warn = None
            notes = locals().get("skip_notes") or []
            if notes:
                warn = "؛ ".join(notes)
            results.append({
                "symbol": symbol,
                "amount": amount,
                "entry_price": entry,
                "tp1_price": tp1_price,
                "tp2_price": tp2_price,
                "tp3_price": tp3_price,
                "stop_loss_price": sl,
                "sl_price": sl,
                "original_sl_price": sl,
                "tp1_order_id": order_ids["tp1_order_id"],
                "tp2_order_id": order_ids["tp2_order_id"],
                "tp3_order_id": order_ids["tp3_order_id"],
                "tp_order_id": None,
                "mode": "manual",
                "sl_pct": sl_pct_use,
                "tp1_sell_pct": float(tp1_sell_pct or 0),
                "tp2_sell_pct": float(tp2_sell_pct or 0),
                "error": None,
                "warning": warn,
            })
        return results

    def sync_manual_tp_orders(self, positions: List[Any]) -> List[Dict]:
        """Return TP actions only after MEXC reports a manual limit fill."""
        actions = []
        for coin in positions:
            status = str(getattr(coin, "position_status", "open") or "open")
            if status == "open":
                stage, key, fallback_pct, fallback_price = "tp1", "tp1_order_id", 40.0, coin.tp1_price
            elif status == "tp1_hit":
                stage, key, fallback_pct, fallback_price = "tp2", "tp2_order_id", 40.0, coin.tp2_price
            elif status == "tp2_hit":
                stage, key, fallback_pct, fallback_price = "tp3", "tp3_order_id", 30.0, coin.tp3_price
            else:
                continue
            order_id = getattr(coin, key, None)
            if not order_id:
                continue
            order = self.client.fetch_order(order_id, coin.symbol)
            order_status = str((order or {}).get("status") or "").lower()
            if order_status in ("canceled", "cancelled", "rejected", "expired"):
                # Let the normal manual monitor take over on the next line;
                # the DB cleanup is persisted by the monitor job.
                setattr(coin, key, None)
                actions.append({
                    "coin_id": getattr(coin, "id", None), "symbol": coin.symbol,
                    "action": "manual_tp_order_cleared", "clear_order_key": key,
                })
                continue
            if order_status not in ("closed", "filled"):
                continue
            remaining = float(coin.remaining_amount or coin.amount or 0)
            original = float(coin.amount or remaining or 0)
            filled = float((order or {}).get("filled") or 0)
            if filled <= 0:
                filled = min(remaining, original * fallback_pct / 100.0)
            price = float((order or {}).get("average") or (order or {}).get("price") or fallback_price or 0)
            entry = float(coin.entry_price or 0)
            tp1_px = float(coin.tp1_price or 0)
            if stage == "tp1":
                new_sl = entry
            elif stage == "tp2":
                new_sl = max(float(coin.current_sl_price or 0), tp1_px if tp1_px > 0 else entry)
            else:
                new_sl = float(coin.current_sl_price or 0)
            actions.append({
                "coin_id": getattr(coin, "id", None), "symbol": coin.symbol,
                "action": f"{stage}_hit", "price": price, "fill_price": price,
                "filled_amount": min(remaining, filled),
                "new_sl": new_sl,
                "enable_trail": stage == "tp2",
                "exchange_order": True,
            })
        return actions

    def sync_cloud_tp(self, positions: List[Any]) -> List[Dict]:
        """Trigger configured TP targets locally, then market-sell the slice."""
        actions = []
        active = [c for c in positions if (c.position_status or "open") in ("open", "tp1_hit")]
        if not active:
            return actions
        prices = self.client.get_all_prices(list({c.symbol for c in active})) or {}
        for coin in active:
            status = str(coin.position_status or "open")
            if status == "open":
                stage, target, sell_pct = "tp1", float(coin.tp1_price or 0), float(coin.tp1_sell_pct or 0)
            else:
                stage, target, sell_pct = "tp2", float(coin.tp2_price or 0), float(coin.tp2_sell_pct or 0)
            price = float(prices.get(coin.symbol) or prices.get(f"{coin.symbol}/{self.quote}") or 0)
            if target <= 0 or sell_pct <= 0 or price < target:
                continue
            remaining = max(0.0, float(coin.remaining_amount or coin.amount or 0))
            original = max(remaining, float(coin.amount or 0))
            amount = min(remaining, original * sell_pct / 100.0)
            if amount <= 0:
                continue
            try:
                order = self.client.create_market_order(f"{coin.symbol}/{self.quote}", "sell", amount)
                if not order:
                    raise RuntimeError("Exchange rejected the cloud TP amount")
                fill_price = float(order.get("average") or order.get("price") or price)
                self._confirm_market_sell(f"{coin.symbol}/{self.quote}", order, amount)
                actions.append({"coin_id": getattr(coin, "id", None), "symbol": coin.symbol,
                                "action": f"{stage}_hit", "price": fill_price, "fill_price": fill_price,
                                "filled_amount": amount, "new_sl": float(coin.current_sl_price or 0),
                                "enable_trail": stage == "tp2", "cloud_tp": True})
            except Exception as exc:
                logger.warning("cloud TP failed %s %s: %s", coin.symbol, stage, exc)
        return actions

    def check_and_manage_positions(self, positions: List[Any]) -> List[Dict]:
        """وقف متحرك + إعادة دخول متكررة (بدون أهداف ثابتة).

        المنطق:
        1) لو الرصيد ≈ 0 → إغلاق
        2) لو waiting_reentry والسعر ≥ سعر إعادة الدخول → إشارة شراء (متكرر، مش مرة واحدة)
        3) لو السعر ≤ الاستوب → بيع سوق + waiting_reentry
        4) الاستوب اليدوي ثابت، وإعادة الدخول اختيارية بعد تأكيد المستخدم
        """
        import time
        actions = []
        active = [
            c for c in positions
            if (c.position_status or "idle") in (
                "open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit", "waiting_reentry"
            )
        ]
        if not active:
            return actions

        symbols = list({c.symbol for c in active})
        try:
            prices = self.client.get_all_prices(symbols) or {}
        except Exception:
            prices = {}

        now = time.time()
        # تبريد إعادة الدخول في الذاكرة (per process)
        if not hasattr(self, "_reentry_cooldown"):
            self._reentry_cooldown = {}

        for coin in active:
            symbol = coin.symbol
            status = (coin.position_status or "open").strip()
            price = float(prices.get(symbol) or prices.get(f"{symbol}/{self.quote}") or 0)
            if price <= 0:
                try:
                    price = float(self.client.get_ticker_price(f"{symbol}/{self.quote}") or 0)
                except Exception:
                    price = 0
            if price <= 0:
                continue

            entry = float(coin.entry_price or 0)
            remaining = float(coin.remaining_amount or coin.amount or 0)
            sl = float(coin.current_sl_price or 0)

            # ----- رصيد مؤكد (الإجمالي وليس الحر فقط) -----
            # لا نغلق/نحذف المركز عند فشل API أو رد فارغ. وحتى الرد الصفري
            # الصحيح يحتاج 3 دورات مراقبة متتالية قبل المزامنة.
            effective_amt = self._confirmed_total_amount(symbol)
            if effective_amt is None:
                self._balance_zero_streak.pop(symbol, None)
                continue
            market_val = effective_amt * price
            if status != "waiting_reentry":
                if effective_amt <= 0 or market_val < MIN_POSITION_USDT:
                    streak = self._balance_zero_streak.get(symbol, 0) + 1
                    self._balance_zero_streak[symbol] = streak
                    if streak < 3:
                        logger.warning(
                            "Preserving %s: confirmed low balance read %s/3 (amount=%.8g, value=%.6g)",
                            symbol, streak, effective_amt, market_val,
                        )
                        continue
                    # لو القاعدة أصلًا فاضية → صامت (تزامن بدون إشعار)
                    silent = remaining <= 0
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "tp_full_close",
                        "stage": "balance_zero",
                        "price": price,
                        "filled_amount": remaining if remaining > 0 else 0,
                        "fill_price": price,
                        "silent": silent,
                    })
                    continue
                self._balance_zero_streak.pop(symbol, None)

            # ----- إعادة دخول متكررة (سلسة) -----
            if status == "waiting_reentry":
                reentry_px = float(getattr(coin, "reentry_price", 0) or 0)
                if reentry_px <= 0:
                    # لو مفيش سعر محفوظ، ابنِه من آخر استوب
                    last_sl = float(coin.current_sl_price or 0)
                    reentry_px = reentry_trigger_price(last_sl if last_sl > 0 else price, REENTRY_RECOVER_PCT)

                # تبريد: متسمحش بمحاولة كل ثانية
                last_try = float(self._reentry_cooldown.get(symbol, 0) or 0)
                if now - last_try < REENTRY_COOLDOWN_SEC:
                    continue

                if price >= reentry_px > 0:
                    self._reentry_cooldown[symbol] = now
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "reentry_buy",
                        "price": price,
                        "reentry_price": reentry_px,
                        # مهم: متكررة — البوت لازم يصفّر reentry_used بعد النجاح
                        "repeatable": True,
                    })
                elif price >= reentry_px * 0.995 and not getattr(coin, "reentry_touched", False):
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "reentry_touch",
                        "price": price,
                        "reentry_price": reentry_px,
                    })
                continue

            # ----- ضرب الاستوب → بيع -----
            if sl > 0 and price <= sl:
                sold = False
                dust = False
                tracked_amount = max(0.0, remaining)
                if tracked_amount <= 0 and status != "waiting_reentry":
                    tracked_amount = max(0.0, float(coin.amount or 0))

                try:
                    # Cancel only TP orders belonging to this tracked position.
                    # Never cancel another portfolio's sell order for the same symbol.
                    tracked_tp_ids = set()
                    for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                        order_id = getattr(coin, key, None)
                        if order_id:
                            tracked_tp_ids.add(str(order_id))
                            self.client.cancel_order(
                                str(order_id), f"{symbol}/{self.quote}", strict=False
                            )
                    import time as _time
                    remaining_sells = []
                    # MEXC can acknowledge cancellation before the order
                    # disappears from fetch_open_orders. Poll and retry only
                    # orders tracked by this position; never cancel another
                    # strategy's sell order for the same asset.
                    for attempt in range(4):
                        _time.sleep(0.8)
                        remaining_sells = self.client.fetch_open_sell_orders(symbol)
                        if not remaining_sells:
                            break
                        unknown = [
                            order for order in remaining_sells
                            if str(order.get("id") or "") not in tracked_tp_ids
                        ]
                        if unknown:
                            ids = [str(order.get("id") or "?") for order in unknown[:5]]
                            raise RuntimeError(
                                f"Open sell orders remain that are not tracked by this position: {', '.join(ids)}"
                            )
                        if attempt < 3:
                            for order in remaining_sells:
                                self.client.cancel_order(
                                    str(order.get("id")), f"{symbol}/{self.quote}", strict=False
                                )
                    if remaining_sells:
                        ids = [str(order.get("id") or "?") for order in remaining_sells[:5]]
                        raise RuntimeError(
                            f"Open tracked sell orders remain after repeated TP cancellation: {', '.join(ids)}"
                        )
                    free_after_cancel = float(self.client.get_free_amount(symbol) or 0.0)
                except Exception as exc:
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "sl_sell_failed",
                        "error": f"Could not safely release the tracked position: {exc}",
                        "price": price,
                        "sl": sl,
                        "tracked_amount": tracked_amount,
                    })
                    continue

                # Use the full LOTFE-tracked amount. Never use the entire
                # wallet balance, which may include another position.
                amount = min(tracked_amount, free_after_cancel)
                dust = tracked_amount <= 0 or tracked_amount * price < MIN_POSITION_USDT
                if not dust:
                    unavailable_value = max(0.0, tracked_amount - amount) * price
                    if amount <= 0 or amount * price < MIN_POSITION_USDT or unavailable_value >= MIN_POSITION_USDT:
                        actions.append({
                            "coin_id": getattr(coin, "id", None),
                            "symbol": symbol,
                            "action": "sl_sell_failed",
                            "error": (
                                f"Could not sell the full tracked position after cancelling TP orders: "
                                f"tracked={tracked_amount:.8g}, free={free_after_cancel:.8g}"
                            ),
                            "price": price,
                            "sl": sl,
                            "tracked_amount": tracked_amount,
                            "free_after_cancel": free_after_cancel,
                        })
                        continue

                    try:
                        order = self.client.create_market_order(
                            f"{symbol}/{self.quote}", "sell", amount
                        )
                        if not order:
                            raise RuntimeError("Exchange did not return a market sell order.")
                        self._confirm_market_sell(
                            f"{symbol}/{self.quote}", order, amount
                        )
                        sold = True
                    except Exception as exc:
                        actions.append({
                            "coin_id": getattr(coin, "id", None),
                            "symbol": symbol,
                            "action": "sl_sell_failed",
                            "error": str(exc),
                            "price": price,
                            "sl": sl,
                            "tracked_amount": tracked_amount,
                            "free_after_cancel": free_after_cancel,
                            "amount": amount,
                        })
                        continue

                else:
                    sold = True
                    amount = tracked_amount

                trigger = reentry_trigger_price(price, REENTRY_RECOVER_PCT)
                self._reentry_cooldown.pop(symbol, None)
                actions.append({
                    "coin_id": getattr(coin, "id", None),
                    "symbol": symbol,
                    "action": "sl_hit_sold",
                    "sl": sl,
                    "price": price,
                    "amount": amount,
                    "sold": sold,
                    "dust": dust,
                    "was_raised": sl >= entry * 0.999 if entry > 0 else False,
                    "reentry_price": trigger,
                    "reentry_available": True,   # دايماً متاح
                    "auto_reentry": True,
                    "repeatable": True,
                })
                continue

            # الوضع اليدوي فقط: إذا لم توجد أوامر Limit، استخدم مراقبة TP كخطة بديلة.
            # بعد تحقق TP2 لا نرفع الاستوب تلقائياً؛ يبقى وقف المستخدم ثابتاً.
            has_exchange_tp = any(
                getattr(coin, key, None)
                for key in ("tp1_order_id", "tp2_order_id", "tp3_order_id")
            )
            portfolio = getattr(coin, "portfolio", None)
            base_amount = float(coin.amount or remaining or 0)
            tp1_sell_pct = float(getattr(portfolio, "tp1_sell_pct", None) or 40.0)
            tp2_sell_pct = float(getattr(portfolio, "tp2_sell_pct", None) or 40.0)
            tp1_px = float(coin.tp1_price or 0)
            tp2_px = float(coin.tp2_price or 0)
            if not has_exchange_tp:
                if status == "open" and tp1_px > 0 and price >= tp1_px:
                    filled = min(remaining, base_amount * max(0.0, tp1_sell_pct) / 100.0)
                    actions.append({
                        "coin_id": getattr(coin, "id", None), "symbol": symbol,
                        "action": "tp1_hit", "price": price, "fill_price": price,
                        "filled_amount": filled, "new_sl": sl,
                    })
                    continue
                if status == "tp1_hit" and tp2_px > 0 and price >= tp2_px:
                    filled = min(remaining, base_amount * max(0.0, tp2_sell_pct) / 100.0)
                    actions.append({
                        "coin_id": getattr(coin, "id", None), "symbol": symbol,
                        "action": "tp2_hit", "price": price, "fill_price": price,
                        "filled_amount": filled, "new_sl": sl,
                    })
                    continue

        if actions:
            log_trade(
                "cycle_summary",
                count=len(actions),
                types=",".join(sorted({a.get("action", "?") for a in actions})),
            )
            for a in actions:
                log_trade(
                    a.get("action", "unknown"),
                    a.get("symbol", ""),
                    price=a.get("price"),
                    sl=a.get("sl") or a.get("new_sl") or a.get("old_sl"),
                    amount=a.get("amount") or a.get("filled_amount"),
                    mode=a.get("mode"),
                    gain=a.get("gain_pct"),
                    error=a.get("error"),
                    silent=a.get("silent"),
                    stage=a.get("stage"),
                )
        return actions

    def reentry_buy_and_place_tp(
        self,
        symbol: str,
        usdt_amount: float,
        tp1_pct: float,
        tp2_pct: float,
        tp3_pct: float,
        stop_loss_pct: float,
        tp1_sell_pct: float = 40.0,
        tp2_sell_pct: float = 40.0,
        control_mode: str = "manual",
    ) -> Dict:
        """Market buy then place the user's manual TP/SL limits for a re-entry."""
        result = {"symbol": symbol, "error": None}
        # IMPORTANT: capture the balance before the buy. Never use the
        # post-buy wallet balance as the purchased amount because the wallet
        # may already contain an older/untracked position in the same coin.
        try:
            pre_total = float(self.client.get_total_amount(symbol) or 0.0)
        except Exception:
            pre_total = 0.0

        try:
            order = self.client.create_market_buy_usdt(symbol, usdt_amount)
            result["buy_order_id"] = order.get("id") if order else None
        except Exception as e:
            result["error"] = str(e)
            return result

        import time
        time.sleep(1.0)

        # Prefer the actual filled quantity returned by the exchange. If it is
        # missing, derive ONLY the delta in wallet balance caused by this buy.
        # Never fall back to the entire free balance.
        order_filled = 0.0
        if isinstance(order, dict):
            try:
                order_filled = float(order.get("filled") or order.get("amount") or 0.0)
            except (TypeError, ValueError):
                order_filled = 0.0

        try:
            post_total = float(self.client.get_total_amount(symbol) or 0.0)
        except Exception:
            post_total = pre_total

        balance_delta = max(0.0, post_total - pre_total)
        amount = order_filled if order_filled > 0 else balance_delta
        # Leave a tiny buffer for fees/rounding, but NEVER expand the amount.
        amount = max(0.0, min(amount, balance_delta if balance_delta > 0 else amount) * 0.998)
        if amount <= 0:
            result["error"] = "تعذر تحديد كمية إعادة الدخول المشتراة بدقة؛ تم إلغاء وضع TP/SL للحماية."
            return result

        entry = self.client.get_ticker_price(f"{symbol}/{self.quote}")
        placed = self.place_tp_orders(
            [{
                "symbol": symbol,
                "amount": amount,
                "entry_price": entry,
                "place_exchange_orders": False,
            }],
            tp1_pct, tp2_pct, tp3_pct, stop_loss_pct, tp1_sell_pct, tp2_sell_pct,
            control_mode=control_mode,
        )
        if placed:
            result.update(placed[0])
            if result.get("error"):
                result["tp_warning"] = result["error"]
                result["error"] = None
        return result
