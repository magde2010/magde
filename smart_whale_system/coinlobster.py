"""
CoinLobster API Client
"""
import logging
from typing import Dict, List, Optional, Any
import requests
import config

logger = logging.getLogger(__name__)

BASE_URL = "https://coinlobster.com/api/ai/v1"
PUBLIC_URL = "https://coinlobster.com/api/public"


class CoinLobsterClient:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or config.COINLOBSTER_API_KEY
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            })

    def _get(self, path: str, params: dict = None, public: bool = False) -> Optional[Dict]:
        url = (PUBLIC_URL if public else BASE_URL) + path
        try:
            r = self.session.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("success") is False:
                    logger.warning(f"CoinLobster error: {data.get('error')}")
                    return None
                return data
            else:
                logger.warning(f"CoinLobster HTTP {r.status_code}: {r.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"CoinLobster request failed: {e}")
            return None

    def get_radar(self, window: str = None) -> List[Dict]:
        """
        يرجع قائمة العملات مع نشاط الحيتان.
        window: 1h | 4h | 24h
        """
        window = window or config.RADAR_WINDOW
        data = self._get("/whales/radar", params={"window": window})
        if not data:
            # fallback to public
            data = self._get("/whale-radar", params={"window": window}, public=True)

        if not data:
            return []

        # Handle different response shapes
        rows = []
        if "data" in data and "windows" in data["data"]:
            # Full API shape
            windows = data["data"]["windows"]
            rows = windows.get(window) or windows.get("4h") or []
        elif "rows" in data:
            rows = data["rows"]
        elif isinstance(data.get("data"), list):
            rows = data["data"]

        return rows or []

    def get_unusual_buys(self, min_multiple: float = None, window: str = None) -> List[Dict]:
        """
        يرجع فقط العملات اللي فيها شراء حيتان غير عادي.
        """
        min_multiple = min_multiple or config.MIN_MULTIPLE
        rows = self.get_radar(window)

        result = []
        for r in rows:
            coin = (r.get("coin") or "").upper()
            if not coin:
                continue

            direction = (r.get("direction") or "").lower()
            multiple = r.get("multiple")
            unusual = r.get("unusual", False)
            net = r.get("netUsd") or 0
            buy = r.get("buyUsd") or 0
            sell = r.get("sellUsd") or 0

            # شروط الشراء غير العادي
            is_buy_pressure = direction == "buy" or (buy > sell * 1.5)
            strong_multiple = multiple is not None and multiple >= min_multiple
            flagged = unusual is True

            if is_buy_pressure and (strong_multiple or flagged):
                result.append({
                    "coin": coin,
                    "multiple": multiple,
                    "net_usd": net,
                    "buy_usd": buy,
                    "sell_usd": sell,
                    "unusual": flagged,
                    "direction": direction,
                    "raw": r,
                })

        # ترتيب حسب قوة الإشارة
        result.sort(key=lambda x: (x["multiple"] or 0), reverse=True)
        return result

    def get_coin_flow(self, coin: str) -> Optional[Dict]:
        """تدفق حيتان لعملة واحدة"""
        data = self._get(f"/whales/flow/{coin.upper()}")
        if data and "data" in data:
            return data["data"]
        return None

    def health(self) -> bool:
        """اختبار بسيط إن المفتاح شغال"""
        data = self._get("/whales/radar", params={"window": "1h"})
        return data is not None
