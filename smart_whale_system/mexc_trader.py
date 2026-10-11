"""
MEXC Spot Trader - مبسط وآمن
"""
import logging
from typing import Dict, Optional, Tuple
import ccxt
import config

logger = logging.getLogger(__name__)


class MexcTrader:
    def __init__(self):
        self.exchange = ccxt.mexc({
            "apiKey": config.MEXC_API_KEY,
            "secret": config.MEXC_API_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "recvWindow": 10000,
            },
        })
        self.quote = config.QUOTE

    def _pair(self, coin: str) -> str:
        return f"{coin.upper()}/{self.quote}"

    def get_free_usdt(self) -> float:
        try:
            bal = self.exchange.fetch_balance()
            return float(bal.get("free", {}).get(self.quote, 0) or 0)
        except Exception as e:
            logger.error(f"get_free_usdt error: {e}")
            return 0.0

    def get_price(self, coin: str) -> float:
        try:
            ticker = self.exchange.fetch_ticker(self._pair(coin))
            return float(ticker.get("last") or 0)
        except Exception as e:
            logger.error(f"get_price {coin} error: {e}")
            return 0.0

    def get_balance(self, coin: str) -> float:
        try:
            bal = self.exchange.fetch_balance()
            free = bal.get("free", {})
            return float(free.get(coin.upper(), 0) or 0)
        except Exception as e:
            logger.error(f"get_balance {coin} error: {e}")
            return 0.0

    def buy_market(self, coin: str, usdt_amount: float) -> Optional[Dict]:
        """
        شراء بسعر السوق بمبلغ USDT محدد.
        يرجع تفاصيل الأمر أو None عند الفشل.
        """
        pair = self._pair(coin)
        usdt_amount = round(usdt_amount, 2)

        if usdt_amount < config.MIN_ORDER_USDT:
            logger.warning(f"مبلغ الشراء صغير جداً: {usdt_amount}")
            return None

        try:
            # التأكد من وجود الرصيد
            free = self.get_free_usdt()
            if free < usdt_amount:
                logger.warning(f"رصيد USDT غير كافٍ: {free:.2f} < {usdt_amount}")
                return None

            price = self.get_price(coin)
            if price <= 0:
                return None

            # حساب الكمية
            amount = usdt_amount / price
            # تقريب حسب دقة السوق
            markets = self.exchange.load_markets()
            market = markets.get(pair)
            if market:
                amount = float(self.exchange.amount_to_precision(pair, amount))

            if amount <= 0:
                return None

            order = self.exchange.create_order(
                symbol=pair,
                type="market",
                side="buy",
                amount=amount,
            )
            logger.info(f"BUY {coin}: {amount} @ ~{price} = {usdt_amount} USDT | order={order.get('id')}")
            return {
                "id": order.get("id"),
                "coin": coin.upper(),
                "side": "buy",
                "amount": float(order.get("filled") or amount),
                "price": float(order.get("average") or price),
                "cost": float(order.get("cost") or usdt_amount),
                "raw": order,
            }
        except Exception as e:
            logger.error(f"buy_market {coin} failed: {e}")
            return None

    def sell_market(self, coin: str, amount: float = None, percent: float = 100.0) -> Optional[Dict]:
        """
        بيع بسعر السوق.
        amount: كمية محددة، أو percent من الرصيد المتاح.
        """
        pair = self._pair(coin)
        try:
            free = self.get_balance(coin)
            if free <= 0:
                logger.warning(f"لا يوجد رصيد لـ {coin}")
                return None

            if amount is None:
                amount = free * (percent / 100.0)

            # تقريب
            markets = self.exchange.load_markets()
            market = markets.get(pair)
            if market:
                amount = float(self.exchange.amount_to_precision(pair, amount))

            if amount <= 0:
                return None

            # تأكد إن المبلغ مش أقل من الحد الأدنى
            price = self.get_price(coin)
            if price > 0 and (amount * price) < config.MIN_ORDER_USDT:
                # لو القيمة صغيرة، بيع الكل
                amount = free
                if market:
                    amount = float(self.exchange.amount_to_precision(pair, amount))

            order = self.exchange.create_order(
                symbol=pair,
                type="market",
                side="sell",
                amount=amount,
            )
            logger.info(f"SELL {coin}: {amount} | order={order.get('id')}")
            return {
                "id": order.get("id"),
                "coin": coin.upper(),
                "side": "sell",
                "amount": float(order.get("filled") or amount),
                "price": float(order.get("average") or price),
                "cost": float(order.get("cost") or 0),
                "raw": order,
            }
        except Exception as e:
            logger.error(f"sell_market {coin} failed: {e}")
            return None

    def get_position_value(self, coin: str) -> Tuple[float, float]:
        """يرجع (الكمية, القيمة بالدولار)"""
        amount = self.get_balance(coin)
        price = self.get_price(coin)
        return amount, amount * price if price > 0 else 0.0
