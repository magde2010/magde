"""Pure classification helpers for exchange submission results.

These helpers never place, retry, cancel, or reconcile exchange orders.
"""
from typing import Any


def classify_dca_submission(order: Any) -> tuple[str, str | None]:
    """Return (status, order_id) for the documented DCA client contract.

    MexcClient.create_limit_buy_usdt returns None only for local preflight or
    recognized explicit minimum/precision rejection. Exceptions and malformed
    non-None responses remain ambiguous and must not trigger a retry.
    """
    if order is None:
        return "rejected", None
    if not isinstance(order, dict):
        return "manual_review", None
    order_id = str(order.get("id") or "").strip()
    if not order_id:
        return "manual_review", None
    return "open", order_id


def classify_market_sell_submission(order: Any) -> tuple[str, str | None]:
    """Return (classification, order_id) for a market sell response.

    The client contract documents None as a confirmed local minimum/dust
    rejection or a recognized explicit exchange minimum rejection. A non-None
    response without an ID is ambiguous, never dust, and must not be retried.
    """
    if order is None:
        return "dust", None
    if not isinstance(order, dict):
        return "manual_review", None
    order_id = str(order.get("id") or "").strip()
    if not order_id:
        return "manual_review", None
    return "submitted", order_id
