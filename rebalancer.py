import logging
import time
from typing import Any

from mexc_client import MexcClient

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
# سماح تنفيذ إعادة الدخول حول السعر المحفز حتى لا تضيع الإشارة بسبب الحركة السريعة.
REENTRY_EXEC_TOLERANCE_PCT = 1.0
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

    def _confirmed_total_amount(self, symbol: str, balance_snapshot=None):
        """Return total wallet amount, or None when the balance read is unreliable."""
        if balance_snapshot is not None:
            try:
                key = self.client.normalize_asset_symbol(symbol)
                total = balance_snapshot.get("total") or {}
                free = balance_snapshot.get("free") or {}
                return max(
                    float(total.get(key) or total.get(symbol) or 0),
                    float(free.get(key) or free.get(symbol) or 0),
                )
            except Exception:
                return None
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

    def _confirm_market_sell(self, symbol: str, order: dict[str, Any], requested: float) -> float:
        """Return only exchange-confirmed filled quantity after order state settles."""
        checked = dict(order or {})
        order_id = str(checked.get("id") or "")
        terminal = {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}
        nonterminal = {"open", "new", "pending", "partially_filled", "partially-filled"}

        for attempt in range(4):
            status = str(checked.get("status") or "").lower()
            try:
                filled = max(0.0, float(checked.get("filled") or 0.0))
            except (TypeError, ValueError):
                filled = 0.0
            if status in terminal:
                break
            if status not in nonterminal and filled > 0:
                # Some exchange adapters omit status on a synchronous market fill.
                break
            if not order_id or not hasattr(self.client, "fetch_order") or attempt == 3:
                break
            time.sleep(0.4)
            checked = self.client.fetch_order(order_id, symbol, strict=True) or checked

        status = str(checked.get("status") or "").lower()
        try:
            filled = max(0.0, float(checked.get("filled") or 0.0))
        except (TypeError, ValueError):
            filled = 0.0
        if status in nonterminal and order_id:
            # Do not count a still-live sell as settled; a later fill could make
            # the local position stale and allow another sell on the next pass.
            raise RuntimeError(
                f"بيع السوق ما زال غير محسوم (order={order_id}, status={status}, filled={filled:.8g})"
            )
        if filled <= 0:
            raise RuntimeError(f"لم تؤكد المنصة كمية بيع موجبة (order={order_id or 'بدون رقم'})")
        return min(filled, float(requested))

    def _confirm_market_buy(self, symbol: str, order: dict[str, Any], requested_usdt: float) -> dict[str, Any]:
        """Return a buy order only after a positive fill quantity is confirmed.

        The requested amount is quote currency and must never be mistaken for
        the base-asset quantity. Ambiguous submissions are not retried here.
        """
        checked = dict(order or {})
        order_id = str(checked.get("id") or "")
        if not order_id:
            raise RuntimeError("شراء غير محسوم: لا يوجد رقم أمر يمكن مطابقته؛ لا تعاود الإرسال تلقائيًا")
        for attempt in range(4):
            try:
                raw_filled = checked.get("filled")
                filled = float(raw_filled) if raw_filled is not None else 0.0
            except (TypeError, ValueError):
                filled = 0.0
            status = str(checked.get("status") or "").lower()
            if filled > 0 and status in {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}:
                checked["filled"] = filled
                checked["status"] = str(checked.get("status") or "partial").lower()
                return checked
            if attempt == 3:
                break
            try:
                checked = self.client.fetch_order(order_id, f"{symbol}/{self.quote}", strict=True) or checked
            except TypeError:
                checked = self.client.fetch_order(order_id, f"{symbol}/{self.quote}") or checked
            except Exception:
                # Keep the order ID and report ambiguity instead of claiming a fill.
                if attempt == 2:
                    break
            time.sleep(0.35)
        raise RuntimeError(f"شراء غير مؤكد التنفيذ (order={order_id}); يلزم تسوية الحالة قبل أي إعادة إرسال")

    def _sell_prices(self, symbols: list[str]) -> dict[str, float]:
        """Prefer executable best-bid prices, with compatibility fallback."""
        getter = getattr(self.client, "get_all_sell_prices", None)
        try:
            if callable(getter):
                return getter(symbols) or {}
            return self.client.get_all_prices(symbols) or {}
        except Exception:
            return {}

    @staticmethod
    def _tp_sell_pct(coin: Any, stage: str) -> float:
        """Resolve the TP slice without assuming it is a PortfolioCoin column."""
        name = f"tp{1 if stage == 'tp1' else 2}_sell_pct"
        for source, key in (
            (coin, f"_monitor_{name}"),
            (coin, name),  # compatibility for legacy/test objects
            (getattr(coin, "portfolio", None), name),
        ):
            value = getattr(source, key, None) if source is not None else None
            if value is not None:
                try:
                    return max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    pass
        return 40.0

    def fetch_candles(self, symbol: str, timeframe: str = "1h", limit: int = 80) -> list:
        pair = f"{symbol}/{self.quote}"
        try:
            return self.client.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit) or []
        except Exception as e:
            logger.warning("OHLCV failed for %s: %s", symbol, e)
            return []


    def calculate_targets(self, coins: list[str], method: str = "equal") -> dict[str, float]:
        if not coins:
            return {}
        pct = 100.0 / len(coins)
        return {c: pct for c in coins}

    def start_portfolio(
        self,
        coins: list[str],
        total_usdt: float,
        method: str = "equal",
        min_trade_usdt: float = 2.0,
        dry_run: bool = False,
    ) -> dict:
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
                if dry_run:
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "buy",
                        "usdt": usdt_for_coin,
                        "status": "dry_run",
                    })
                else:
                    order = self.client.create_market_buy_usdt(coin, usdt_for_coin)
                    try:
                        confirmed = self._confirm_market_buy(coin, order, usdt_for_coin)
                        filled = float(confirmed.get("filled") or 0.0)
                        result_status = "filled" if str(confirmed.get("status") or "").lower() in ("closed", "filled") else "partial"
                        results["executed"].append({
                            "symbol": f"{coin}/{self.quote}",
                            "side": "buy",
                            "usdt": usdt_for_coin,
                            "filled_amount": filled,
                            "status": result_status,
                            "order_id": str(confirmed["id"]),
                        })
                    except Exception as confirm_exc:
                        results["errors"].append({coin: str(confirm_exc)})
                        results.setdefault("pending_confirmation", []).append({
                            "symbol": f"{coin}/{self.quote}",
                            "order_id": str((order or {}).get("id") or "") if isinstance(order, dict) else "",
                            "requested_usdt": usdt_for_coin,
                            "status": "manual_review",
                        })
            except Exception as e:
                results["errors"].append({coin: str(e)})
                if not dry_run:
                    # A timeout can happen after the exchange accepted the order.
                    # Persist this as unresolved at the caller; never auto-resubmit.
                    results.setdefault("pending_confirmation", []).append({
                        "symbol": f"{coin}/{self.quote}",
                        "order_id": "",
                        "requested_usdt": usdt_for_coin,
                        "status": "manual_review",
                    })
        return results

    def stop_portfolio(
        self,
        coins: list[str],
        dry_run: bool = False,
        amount_overrides: dict[str, float] | None = None,
    ) -> dict:
        """Sell only explicitly allocated quantities after checking open orders.

        A missing amount override is deliberately not interpreted as permission to
        liquidate the exchange wallet's entire free balance.
        """
        results = {
            "action": "stop",
            "executed": [],
            "errors": [],
            "dry_run": dry_run,
            "total_sold_usdt": 0.0,
        }
        prices = self.client.get_all_prices(coins)
        for coin in coins:
            override = (amount_overrides or {}).get(coin)
            if override is None:
                results["errors"].append({
                    coin: "رفض البيع دون كمية مخصصة صراحةً للمركز (amount_overrides)."
                })
                continue
            try:
                free_amount = max(0.0, float(self.client.get_free_amount(coin) or 0))
                amount = min(max(0.0, float(override or 0.0)), free_amount)
                if amount <= 0:
                    results["errors"].append({coin: "لا توجد كمية حرة ضمن الكمية المخصصة للمركز."})
                    continue
                open_sells = self.client.fetch_open_sell_orders(coin)
                if open_sells:
                    ids = ", ".join(str(o.get("id") or "?") for o in open_sells[:5])
                    results["errors"].append({coin: f"توجد أوامر بيع مفتوحة؛ لم يُرسل أمر جديد: {ids}"})
                    continue
                price = float(prices.get(coin) or 0)
                if dry_run:
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}", "side": "sell",
                        "amount": amount, "usdt": amount * price, "status": "dry_run",
                    })
                    results["total_sold_usdt"] += amount * price
                    continue
                order = self.client.create_market_order(
                    symbol=f"{coin}/{self.quote}", side="sell", amount=amount
                )
                if not order or not order.get("id"):
                    raise RuntimeError("لم يتم تأكيد معرّف أمر البيع؛ النتيجة غير محسومة.")
                order_id = str(order["id"])
                checked = order
                terminal_statuses = {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}
                for _ in range(5):
                    status = str(checked.get("status") or "").lower()
                    if status in terminal_statuses:
                        break
                    checked = self.client.fetch_order(order_id, f"{coin}/{self.quote}", strict=True)
                    if not checked:
                        raise RuntimeError(f"تعذر تأكيد حالة أمر البيع {order_id}؛ لا تعِد الإرسال.")
                    time.sleep(0.4)
                status = str(checked.get("status") or "").lower()
                if status not in terminal_statuses:
                    raise RuntimeError(f"حالة أمر البيع {order_id} غير محسومة ({status or 'unknown'}). لا تعِد الإرسال.")
                filled = max(0.0, float(checked.get("filled") or 0))
                if filled <= 0:
                    raise RuntimeError(f"لم تؤكد المنصة تنفيذ أمر البيع {order_id}.")
                filled = min(filled, amount)
                cost = float(checked.get("cost") or 0)
                avg = float(checked.get("average") or checked.get("price") or 0)
                sold_usdt = cost if cost > 0 else filled * avg
                if sold_usdt <= 0:
                    raise RuntimeError(f"تم تأكيد كمية الأمر {order_id} دون قيمة بيع قابلة للتأكيد.")
                results["executed"].append({
                    "symbol": f"{coin}/{self.quote}", "side": "sell",
                    "amount": filled, "requested_amount": amount,
                    "usdt": sold_usdt, "status": status, "order_id": order_id,
                    "partial": filled + max(1e-10, amount * 1e-6) < amount,
                })
                results["total_sold_usdt"] += sold_usdt
            except Exception as exc:
                results["errors"].append({coin: str(exc)})
        return results

    def cancel_tp_orders(self, coins_data: list[dict[str, Any]]) -> dict:
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
        coins_data: list[dict[str, Any]],
        tp1_pct: float = 0.0,
        tp2_pct: float = 0.0,
        tp3_pct: float = 0.0,
        stop_loss_pct: float = None,
        tp1_sell_pct: float = 0.0,
        tp2_sell_pct: float = 0.0,
        skip_stages: list[str] | None = None,
        control_mode: str = "manual",
    ) -> list[dict]:
        """Place exchange-hosted limit TP orders and calculate an optional bot SL.

        A stop-loss percentage of exactly zero disables the stop. TP orders are
        sized as non-overlapping slices of the original position.
        """
        results = []
        sl_pct = INITIAL_SL_PCT if stop_loss_pct is None else max(0.0, float(stop_loss_pct))
        skip = set(skip_stages or [])
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

            sl = entry * (1.0 - sl_pct / 100.0) if sl_pct > 0 else 0.0
            tp1_price = entry * (1.0 + float(tp1_pct or 0) / 100.0) if float(tp1_pct or 0) > 0 else 0.0
            tp2_price = entry * (1.0 + float(tp2_pct or 0) / 100.0) if float(tp2_pct or 0) > 0 else 0.0
            tp3_price = entry * (1.0 + float(tp3_pct or 0) / 100.0) if float(tp3_pct or 0) > 0 else 0.0
            s1 = max(0.0, min(100.0, float(tp1_sell_pct or 0)))
            s2 = max(0.0, min(100.0 - s1, float(tp2_sell_pct or 0)))
            s3 = max(0.0, 100.0 - s1 - s2)
            order_ids = {"tp1_order_id": None, "tp2_order_id": None, "tp3_order_id": None}
            errors = []
            stages = (
                ("tp1", tp1_price, s1, "tp1_order_id"),
                ("tp2", tp2_price, s2, "tp2_order_id"),
                ("tp3", tp3_price, s3, "tp3_order_id"),
            )
            active_targets = [target for stage, target, slice_pct, key in stages
                              if stage not in skip and target > 0 and slice_pct > 0]
            if any(right <= left for left, right in zip(active_targets, active_targets[1:])):
                results.append({
                    "symbol": symbol, "amount": amount, "entry_price": entry,
                    "tp1_price": tp1_price, "tp2_price": tp2_price, "tp3_price": tp3_price,
                    "sl_price": sl, "stop_loss_price": sl, "original_sl_price": sl,
                    **order_ids, "tp_order_id": None, "mode": "manual", "sl_pct": sl_pct,
                    "error": "نسب أهداف TP غير مرتبة تصاعديًا؛ لم تُرسل أوامر بيع لتجنب خطة أهداف متعارضة.",
                    "warning": None,
                })
                continue
            for stage, target, slice_pct, key in stages:
                if stage in skip or target <= 0 or slice_pct <= 0:
                    continue
                slice_amount = amount * slice_pct / 100.0
                try:
                    order = self.client.create_limit_sell(symbol, slice_amount, target)
                    order_id = str((order or {}).get("id") or "")
                    if not order_id:
                        reason = (
                            "لم يُنشأ أمر صالح: كمية/قيمة الهدف دون حد السوق أو رفض متعلق بالدقة"
                            if order is None else
                            "استجابة بلا معرّف موثوق؛ حالة غير محسومة، راجع المنصة ولا تعِد الإرسال"
                        )
                        errors.append(f"{stage.upper()}: {reason}")
                        continue
                    order_ids[key] = order_id
                except Exception as exc:
                    errors.append(f"{stage.upper()}: {str(exc)[:120]}")
            results.append({
                "symbol": symbol, "amount": amount, "entry_price": entry,
                "tp1_price": tp1_price, "tp2_price": tp2_price, "tp3_price": tp3_price,
                "stop_loss_price": sl, "sl_price": sl, "original_sl_price": sl,
                **order_ids, "tp_order_id": None, "mode": "manual", "sl_pct": sl_pct,
                "tp1_sell_pct": s1, "tp2_sell_pct": s2,
                "error": "; ".join(errors) if errors else None,
                "warning": "مجموع نسب البيع تجاوز 100%؛ تم تقليص نسبة TP2 لمنع حجز كمية زائدة"
                    if float(tp1_sell_pct or 0) + float(tp2_sell_pct or 0) > 100 else None,
            })
        return results

    def sync_manual_tp_orders(self, positions: list[Any]) -> list[dict]:
        """Return TP actions only after MEXC reports a manual limit fill."""
        actions = []
        for coin in positions:
            status = str(getattr(coin, "position_status", "open") or "open")
            if status == "open":
                candidates = [
                    ("tp1", "tp1_order_id", coin.tp1_price),
                    ("tp2", "tp2_order_id", coin.tp2_price),
                    ("tp3", "tp3_order_id", coin.tp3_price),
                ]
            elif status == "tp1_hit":
                candidates = [
                    ("tp2", "tp2_order_id", coin.tp2_price),
                    ("tp3", "tp3_order_id", coin.tp3_price),
                ]
            elif status == "tp2_hit":
                candidates = [("tp3", "tp3_order_id", coin.tp3_price)]
            else:
                continue
            selected = next((item for item in candidates if getattr(coin, item[1], None)), None)
            if not selected:
                continue
            stage, key, fallback_price = selected
            order_id = getattr(coin, key, None)
            try:
                order = self.client.fetch_order(order_id, coin.symbol, strict=True)
            except Exception as exc:
                # A temporary status-read failure must not stop the separate
                # price/SL monitor for every position.
                logger.warning("manual TP status read failed %s %s: %s", coin.symbol, order_id, exc)
                continue
            order_status = str((order or {}).get("status") or "").lower()
            if order_status in ("canceled", "cancelled", "rejected", "expired"):
                setattr(coin, key, None)
                filled = float((order or {}).get("filled") or 0)
                remaining = float(coin.remaining_amount or coin.amount or 0)
                filled = min(max(0.0, filled), max(0.0, remaining))
                if filled > 0:
                    price = float(
                        (order or {}).get("average")
                        or (order or {}).get("price")
                        or fallback_price
                        or 0
                    )
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": coin.symbol,
                        "action": "manual_tp_partial",
                        "stage": stage,
                        "filled_amount": filled,
                        "fill_price": price,
                        "clear_order_key": key,
                    })
                else:
                    actions.append({
                        "coin_id": getattr(coin, "id", None), "symbol": coin.symbol,
                        "action": "manual_tp_order_cleared", "clear_order_key": key,
                    })
                continue
            if order_status not in ("closed", "filled"):
                continue
            remaining = float(coin.remaining_amount or coin.amount or 0)
            filled_raw = (order or {}).get("filled")
            filled = float(filled_raw) if filled_raw is not None else 0.0
            if filled <= 0:
                logger.warning(
                    "Ignoring closed TP order with no confirmed filled quantity: %s %s",
                    coin.symbol, order_id,
                )
                continue
            price = float((order or {}).get("average") or (order or {}).get("price") or fallback_price or 0)
            entry = float(coin.entry_price or 0)
            tp1_px = float(coin.tp1_price or 0)
            portfolio = getattr(coin, "portfolio", None)
            configured_sl = getattr(portfolio, "stop_loss_pct", None) if portfolio is not None else None
            sl_disabled = configured_sl is not None and float(configured_sl) <= 0
            if sl_disabled:
                new_sl = 0.0
            elif stage == "tp1":
                new_sl = entry
            elif stage == "tp2":
                new_sl = max(
                    float(coin.current_sl_price or 0),
                    tp1_px if tp1_px > 0 else entry,
                )
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

    def sync_cloud_tp(self, positions: list[Any]) -> list[dict]:
        """Disabled intentionally: take-profit targets are limit orders on MEXC.

        Never fall back to a bot-triggered market sell if an exchange TP order is
        missing or rejected, as that could duplicate or unexpectedly market-sell.
        """
        return []

    def check_and_manage_positions(self, positions: list[Any]) -> list[dict]:
        """وقف متحرك + إعادة دخول متكررة (بدون أهداف ثابتة).

        المنطق:
        1) لو الرصيد ≈ 0 → إغلاق
        2) لو waiting_reentry والسعر ≥ سعر إعادة الدخول → إشارة شراء (متكرر، مش مرة واحدة)
        3) لو السعر ≤ الاستوب → بيع سوق + waiting_reentry
        4) الاستوب ثابت قبل TP2، ثم يتحرك صعوداً فقط لحماية الجزء المتبقي
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
        prices = self._sell_prices(symbols)

        now = time.time()
        balance_snapshot = None
        exchange = getattr(self.client, "exchange", None)
        if exchange is not None and hasattr(exchange, "fetch_balance"):
            try:
                raw_balance = exchange.fetch_balance()
                if isinstance(raw_balance, dict) and ("total" in raw_balance or "free" in raw_balance):
                    balance_snapshot = raw_balance
            except Exception as exc:
                logger.warning("Batch balance read failed; preserving positions: %s", exc)
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
            effective_amt = self._confirmed_total_amount(symbol, balance_snapshot)
            if effective_amt is None:
                self._balance_zero_streak.pop(symbol, None)
                continue
            market_val = effective_amt * price
            if status != "waiting_reentry":
                if effective_amt <= 0:
                    streak = self._balance_zero_streak.get(symbol, 0) + 1
                    self._balance_zero_streak[symbol] = streak
                    if streak < 3:
                        logger.warning(
                            "Preserving %s: confirmed zero balance read %s/3",
                            symbol, streak,
                        )
                        continue
                    # A confirmed zero balance can be reconciled, but is not itself
                    # evidence of a fill price/quantity. The bot checks shared allocations.
                    silent = remaining <= 0
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "tp_full_close",
                        "stage": "balance_zero",
                        "price": price,
                        "filled_amount": 0.0,
                        "fill_price": 0.0,
                        "silent": silent,
                    })
                    continue
                if market_val < MIN_POSITION_USDT:
                    # Dust is still an asset. Keep its record for explicit/manual reconciliation.
                    logger.warning(
                        "Preserving %s: non-zero dust balance amount=%.8g value=%.6g",
                        symbol, effective_amt, market_val,
                    )
                    self._balance_zero_streak.pop(symbol, None)
                    continue
                self._balance_zero_streak.pop(symbol, None)

            # ----- Ratcheting trailing stop for the residual after TP2 -----
            # The current stop is the persisted high-water mark: it may rise
            # as price rises, but it can never move back down.
            if status == "tp2_hit" and sl > 0 and price > sl:
                portfolio = getattr(coin, "portfolio", None)
                configured_trail = getattr(portfolio, "trail_pct", None)
                try:
                    trail_pct = float(configured_trail)
                except (TypeError, ValueError):
                    trail_pct = 0.0
                if trail_pct <= 0:
                    trail_pct = 2.0
                trailing_sl = price * (1.0 - trail_pct / 100.0)
                if trailing_sl > sl:
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "sl_trail_updated",
                        "old_sl": sl,
                        "new_sl": trailing_sl,
                        "price": price,
                    })
                    sl = trailing_sl

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
                elif price >= reentry_px * (1.0 - REENTRY_EXEC_TOLERANCE_PCT / 100.0) and not getattr(coin, "reentry_touched", False):
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
                    # Cancel TP orders belonging to this tracked position first.
                    # Legacy portfolio TP orders may not exist in the database
                    # anymore. Since portfolio TP is cloud-managed now, any
                    # remaining open sell for this symbol is stale and must be
                    # cancelled before the stop can safely release the balance.
                    tracked_tp_orders = {}
                    for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                        order_id = getattr(coin, key, None)
                        if order_id:
                            tracked_tp_orders[str(order_id)] = key
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
                            if str(order.get("id") or "") not in tracked_tp_orders
                        ]
                        if unknown:
                            ids = ", ".join(str(o.get("id") or "?") for o in unknown[:5])
                            raise RuntimeError(
                                "Untracked open sell order(s) exist for this symbol; "
                                f"refusing to cancel or sell against orders not owned by this position: {ids}"
                            )
                        if attempt < 3:
                            for order in remaining_sells:
                                order_id = str(order.get("id") or "")
                                if order_id and order_id in tracked_tp_orders:
                                    self.client.cancel_order(
                                        order_id, f"{symbol}/{self.quote}", strict=False
                                    )
                    if remaining_sells:
                        ids = [str(order.get("id") or "?") for order in remaining_sells[:5]]
                        raise RuntimeError(
                            f"Open tracked sell orders remain after repeated TP cancellation: {', '.join(ids)}"
                        )

                    # Recheck once more immediately before reconciliation/sizing.
                    # Never submit a market sell while an unrelated sell may still fill.
                    final_open_sells = self.client.fetch_open_sell_orders(symbol)
                    if final_open_sells:
                        ids = ", ".join(str(o.get("id") or "?") for o in final_open_sells[:5])
                        raise RuntimeError(f"Open sell order remains; refusing stop-loss market sell: {ids}")

                    # A limit TP may have partially filled just before its
                    # cancellation. Reconcile those fills before sizing the
                    # stop-loss market order, otherwise the SL can oversell.
                    tp_fills = []
                    for order_id, key in tracked_tp_orders.items():
                        order = self.client.fetch_order(
                            order_id, f"{symbol}/{self.quote}", strict=True
                        )
                        if not order:
                            raise RuntimeError(
                                f"Cannot confirm final TP fill state for order {order_id}"
                            )
                        order_status = str(order.get("status") or "").lower()
                        if order_status not in (
                            "closed", "filled", "canceled", "cancelled", "rejected", "expired"
                        ):
                            raise RuntimeError(
                                f"TP order {order_id} is not confirmed terminal ({order_status or 'unknown'})"
                            )
                        if order.get("filled") is None:
                            raise RuntimeError(
                                f"TP order {order_id} has no confirmed filled quantity"
                            )
                        filled_tp = max(0.0, float(order.get("filled") or 0))
                        if filled_tp <= 0:
                            continue
                        tp_fills.append({
                            "stage": key.replace("_order_id", ""),
                            "filled_amount": filled_tp,
                            "fill_price": float(
                                order.get("average")
                                or order.get("price")
                                or price
                            ),
                        })
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

                tp_filled_amount = sum(
                    float(fill["filled_amount"]) for fill in tp_fills
                )
                if tp_filled_amount > tracked_amount + max(tracked_amount * 1e-6, 1e-12):
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "sl_sell_failed",
                        "error": (
                            "Confirmed TP fills exceed the locally tracked remaining amount; "
                            "manual position reconciliation is required."
                        ),
                        "manual_reconciliation_required": True,
                        "price": price,
                        "sl": sl,
                        "tracked_amount": tracked_amount,
                        "tp_fills": tp_fills,
                        "cancelled_tp_order_ids": list(tracked_tp_orders),
                        "remaining_amount": 0.0,
                    })
                    continue
                tracked_amount = max(0.0, tracked_amount - tp_filled_amount)
                if tracked_amount <= max(float(coin.amount or 0) * 1e-9, 1e-12):
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "tp_full_close",
                        "stage": "tp_orders_filled_before_sl",
                        "price": price,
                        "filled_amount": tp_filled_amount,
                        "fill_price": price,
                        "tp_fills": tp_fills,
                        "cancelled_tp_order_ids": list(tracked_tp_orders),
                    })
                    continue

                # Use the full bot-tracked amount (Mohamed Rade plan). Never use the entire
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
                            "remaining_amount": tracked_amount,
                            "position_status": status,
                            "tp_fills": tp_fills,
                            "cancelled_tp_order_ids": list(tracked_tp_orders),
                        })
                        continue

                    try:
                        order = self.client.create_market_order(
                            f"{symbol}/{self.quote}", "sell", amount
                        )
                        if not order:
                            raise RuntimeError("Exchange did not return a market sell order.")
                        filled = self._confirm_market_sell(
                            f"{symbol}/{self.quote}", order, amount
                        )
                        if filled + max(amount * 1e-6, 1e-12) < amount:
                            actions.append({
                                "coin_id": getattr(coin, "id", None),
                                "symbol": symbol,
                                "action": "sl_sell_partial",
                                "price": price,
                                "sl": sl,
                                "filled_amount": filled,
                                "remaining_amount": max(0.0, tracked_amount - filled),
                                "requested_amount": amount,
                                "position_status": "waiting_reentry",
                                "reentry_price": reentry_trigger_price(price, REENTRY_RECOVER_PCT),
                                "reentry_amount_usdt": max(0.0, filled * price),
                                "tp_fills": tp_fills,
                                "cancelled_tp_order_ids": list(tracked_tp_orders),
                            })
                            continue
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
                            "remaining_amount": tracked_amount,
                            "position_status": status,
                            "tp_fills": tp_fills,
                            "cancelled_tp_order_ids": list(tracked_tp_orders),
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
                    "reentry_amount_usdt": max(0.0, amount * price),
                    "reentry_available": True,   # دايماً متاح
                    "auto_reentry": True,
                    "repeatable": True,
                    "position_status": status,
                    "tp_fills": tp_fills,
                    "cancelled_tp_order_ids": list(tracked_tp_orders),
                })
                continue

            # إذا لم يوجد أمر Limit للمرحلة الحالية، راقب TP محلياً كخطة بديلة.
            has_exchange_tp = any(
                getattr(coin, key, None)
                for key in ("tp1_order_id", "tp2_order_id", "tp3_order_id")
            )
            base_amount = float(coin.amount or remaining or 0)
            tp1_sell_pct = self._tp_sell_pct(coin, "tp1")
            tp2_sell_pct = self._tp_sell_pct(coin, "tp2")
            tp1_px = float(coin.tp1_price or 0)
            tp2_px = float(coin.tp2_price or 0)
            if not has_exchange_tp:
                if status == "open" and tp1_px > 0 and price >= tp1_px:
                    filled = min(remaining, base_amount * max(0.0, tp1_sell_pct) / 100.0)
                    actions.append({
                        "coin_id": getattr(coin, "id", None), "symbol": symbol,
                        "action": "tp1_hit", "price": price, "fill_price": price,
                        "filled_amount": filled, "new_sl": entry,
                    })
                    continue
                if status == "tp1_hit" and tp2_px > 0 and price >= tp2_px:
                    filled = min(remaining, base_amount * max(0.0, tp2_sell_pct) / 100.0)
                    actions.append({
                        "coin_id": getattr(coin, "id", None), "symbol": symbol,
                        "action": "tp2_hit", "price": price, "fill_price": price,
                        "filled_amount": filled,
                        "new_sl": max(sl, tp1_px if tp1_px > 0 else entry),
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
    ) -> dict:
        """Market buy then place the user's manual TP/SL limits for a re-entry."""
        result = {"symbol": symbol, "error": None}
        try:
            order = self.client.create_market_buy_usdt(symbol, usdt_amount)
            result["buy_order_id"] = order.get("id") if order else None
        except Exception as e:
            result["error"] = str(e)
            result["manual_review"] = True
            result["buy_status"] = "unknown"
            return result

        try:
            confirmed = self._confirm_market_buy(symbol, order, usdt_amount)
            # `amount` is the requested base quantity on many exchanges, not a fill.
            # Only `filled` from the confirmed exchange order is safe for TP/SL.
            amount = max(0.0, float(confirmed.get("filled") or 0.0)) * 0.998
            result["buy_order_id"] = str(confirmed["id"])
            result["buy_status"] = str(confirmed.get("status") or "partial").lower()
        except Exception as exc:
            result["error"] = str(exc)
            result["manual_review"] = True
            return result

        if amount <= 0:
            result["error"] = "لم تؤكد المنصة كمية تعبئة موجبة؛ لم يتم وضع TP/SL."
            result["manual_review"] = True
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
