"""Market-making strategy for binary prediction markets.

Places limit orders on both sides of the order book and earns the
bid-ask spread.  Core pricing logic adapted from warproxxx/poly-maker.

How it works:
  1. Fetch real-time best bid/ask from the WebSocket order book.
  2. Calculate our quote prices by backing off from mid by HALF_SPREAD.
  3. If our existing orders are still within REPRICE_THRESHOLD of the
     new quote prices, leave them alone (avoids unnecessary cancel/replace).
  4. Otherwise cancel and repost.

Env knobs:
  MM_HALF_SPREAD         Our half-spread in cents (default 0.02 = 2¢)
  MM_REPRICE_THRESHOLD   Cancel/replace only if price moved more than this (default 0.005)
  MM_MIN_PRICE           Don't post below this probability (default 0.04)
  MM_MAX_PRICE           Don't post above this probability (default 0.96)
"""
import os
from typing import Optional
from core.orderbook import order_book_manager
from core.client import poly_client
from core.risk import risk
from utils.logger import logger

HALF_SPREAD         = float(os.getenv("MM_HALF_SPREAD",         "0.02"))
REPRICE_THRESHOLD   = float(os.getenv("MM_REPRICE_THRESHOLD",   "0.005"))
MM_MIN_PRICE        = float(os.getenv("MM_MIN_PRICE",           "0.04"))
MM_MAX_PRICE        = float(os.getenv("MM_MAX_PRICE",           "0.96"))


def _round_price(p: float) -> float:
    """Round to nearest 0.01 (Polymarket tick size)."""
    return round(round(p / 0.01) * 0.01, 4)


class MarketMaker:
    """
    Manages market-making quotes for a single token.

    Typical usage (called from the main scheduler loop):
        mm = MarketMaker(token_id, neg_risk=False)
        mm.update(balance, open_positions)
    """

    def __init__(self, token_id: str, neg_risk: bool = False,
                 complement_token_id: Optional[str] = None):
        self.token_id  = token_id
        self.neg_risk  = neg_risk
        # For a binary market, YES and NO are complements.
        # complement_token_id is needed if the market is neg-risk style.
        self.complement_id = complement_token_id

        # Track our live orders so we don't over-cancel
        self._buy_order:  Optional[dict] = None   # {"order_id", "price", "size"}
        self._sell_order: Optional[dict] = None

    # ── Quoting ──────────────────────────────────────────────────────────────

    def _quote_prices(self) -> tuple[Optional[float], Optional[float]]:
        """Return (bid_price, ask_price) based on live mid, or (None, None)."""
        mid = order_book_manager.mid_price(self.token_id)
        if mid is None:
            return None, None
        bid = _round_price(mid - HALF_SPREAD)
        ask = _round_price(mid + HALF_SPREAD)
        # Clamp to safe range
        bid = max(MM_MIN_PRICE, bid)
        ask = min(MM_MAX_PRICE, ask)
        if bid >= ask:
            return None, None
        return bid, ask

    def _should_reprice(self, new_price: float,
                        existing_order: Optional[dict]) -> bool:
        if existing_order is None:
            return True
        diff = abs(new_price - existing_order["price"])
        return diff > REPRICE_THRESHOLD

    # ── Main update ──────────────────────────────────────────────────────────

    def update(self, balance: float, open_positions: list[dict],
               market_summary: dict) -> dict:
        """
        Called each trading cycle.  Reprices or cancels/replaces quotes.

        Returns a status dict with what happened.
        """
        ok, reason = risk.can_open(market_summary, open_positions, balance)

        if order_book_manager.is_stale(self.token_id):
            return {"action": "skip", "reason": "stale_order_book"}

        bid_px, ask_px = self._quote_prices()
        if bid_px is None:
            return {"action": "skip", "reason": "no_mid_price"}

        size = risk.size_order(balance, open_positions, bid_px)
        if size <= 0:
            return {"action": "skip", "reason": "size_zero"}

        actions = []

        # ── Buy side ──────────────────────────────────────────────────────────
        if self._should_reprice(bid_px, self._buy_order):
            if self._buy_order:
                poly_client.cancel_order(self._buy_order["order_id"])
            if ok:
                result = poly_client.create_order(
                    self.token_id, "BUY", bid_px, size, self.neg_risk
                )
                if result:
                    self._buy_order = {
                        "order_id": result.get("orderID", "dry"),
                        "price":    bid_px,
                        "size":     size,
                    }
                    actions.append(f"buy@{bid_px:.4f}")
            else:
                self._buy_order = None
        else:
            actions.append(f"buy_unchanged@{bid_px:.4f}")

        # ── Sell side ─────────────────────────────────────────────────────────
        if self._should_reprice(ask_px, self._sell_order):
            if self._sell_order:
                poly_client.cancel_order(self._sell_order["order_id"])
            # Can always post a sell to exit an existing position
            result = poly_client.create_order(
                self.token_id, "SELL", ask_px, size, self.neg_risk
            )
            if result:
                self._sell_order = {
                    "order_id": result.get("orderID", "dry"),
                    "price":    ask_px,
                    "size":     size,
                }
                actions.append(f"sell@{ask_px:.4f}")
        else:
            actions.append(f"sell_unchanged@{ask_px:.4f}")

        mid = order_book_manager.mid_price(self.token_id)
        sp  = order_book_manager.spread(self.token_id)
        logger.debug(f"MM {self.token_id[:10]}... mid={mid:.4f} "
                     f"spread={sp:.4f} actions={actions}")
        return {"action": "quoted", "actions": actions, "mid": mid, "spread": sp}

    def cancel_all(self):
        """Cancel both sides. Call when shutting down or rotating out."""
        poly_client.cancel_all_for_token(self.token_id)
        self._buy_order  = None
        self._sell_order = None
        logger.info(f"MM cancelled all quotes for {self.token_id[:10]}...")
