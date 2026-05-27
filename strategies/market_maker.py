"""Market-making strategy for Kalshi binary markets.

Posts a YES BUY at (mid - half_spread) and a YES SELL at (mid + half_spread)
and earns the difference each round-trip.

How it works:
  1. Read live best bid/ask from the WebSocket order book.
  2. Calculate quote prices by backing off from mid by HALF_SPREAD.
  3. If our existing orders are still within REPRICE_THRESHOLD of the
     new quote, leave them alone (avoids unnecessary cancel/replace).
  4. Otherwise cancel and repost.

Kalshi orders are placed in *contracts* (integers), not USD notional.

Env knobs:
  MM_HALF_SPREAD         Our half-spread in $ (default 0.02 = 2¢)
  MM_REPRICE_THRESHOLD   Cancel/replace only if price moved more than this
                         (default 0.01 — Kalshi tick is 1¢, so anything
                         smaller would never fire)
  MM_MIN_PRICE           Don't post YES below this price (default 0.04)
  MM_MAX_PRICE           Don't post YES above this price (default 0.96)
"""
import os
from typing import Optional

from core.client import kalshi_client
from core.orderbook import order_book_manager
from core.risk import risk
from utils.logger import logger

HALF_SPREAD       = float(os.getenv("MM_HALF_SPREAD",       "0.02"))
REPRICE_THRESHOLD = float(os.getenv("MM_REPRICE_THRESHOLD", "0.01"))
MM_MIN_PRICE      = float(os.getenv("MM_MIN_PRICE",         "0.04"))
MM_MAX_PRICE      = float(os.getenv("MM_MAX_PRICE",         "0.96"))


def _round_price(p: float) -> float:
    """Round to nearest 1¢ (Kalshi standard tick size)."""
    return round(round(p / 0.01) * 0.01, 2)


class MarketMaker:
    """Manages market-making quotes for a single Kalshi ticker.

    Typical usage (called from the main scheduler loop):
        mm = MarketMaker(ticker)
        mm.update(balance, open_positions, market_summary)
    """

    def __init__(self, ticker: str):
        self.ticker = ticker
        # Track our live orders so we don't over-cancel
        self._buy_order:  Optional[dict] = None   # {order_id, price, count}
        self._sell_order: Optional[dict] = None

    # ── Quoting ──────────────────────────────────────────────────────────────

    def _quote_prices(self) -> tuple[Optional[float], Optional[float]]:
        """Return (bid_price, ask_price) based on live mid, or (None, None)."""
        mid = order_book_manager.mid_price(self.ticker)
        if mid is None:
            return None, None
        bid = _round_price(mid - HALF_SPREAD)
        ask = _round_price(mid + HALF_SPREAD)
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
        return diff >= REPRICE_THRESHOLD

    # ── Main update ──────────────────────────────────────────────────────────

    def update(self, balance: float, open_positions: list[dict],
               market_summary: dict) -> dict:
        """Called each trading cycle. Reprices or cancels/replaces quotes."""
        ok, reason = risk.can_open(market_summary, open_positions, balance)

        if order_book_manager.is_stale(self.ticker):
            return {"action": "skip", "reason": "stale_order_book"}

        bid_px, ask_px = self._quote_prices()
        if bid_px is None:
            return {"action": "skip", "reason": "no_mid_price"}

        count = risk.size_order(balance, open_positions, bid_px)
        if count <= 0:
            return {"action": "skip", "reason": "size_zero"}

        actions = []

        # ── Buy side (YES BUY at bid) ────────────────────────────────────────
        if self._should_reprice(bid_px, self._buy_order):
            if self._buy_order:
                kalshi_client.cancel_order(self._buy_order["order_id"])
                self._buy_order = None
            if ok:
                result = kalshi_client.create_order(
                    self.ticker, action="buy", side="yes",
                    count=count, price=bid_px,
                )
                if result:
                    self._buy_order = {
                        "order_id": result.get("order_id", "dry"),
                        "price":    bid_px,
                        "count":    count,
                    }
                    actions.append(f"buy@{bid_px:.2f}x{count}")
        else:
            actions.append(f"buy_unchanged@{bid_px:.2f}")

        # ── Sell side (YES SELL at ask) ──────────────────────────────────────
        if self._should_reprice(ask_px, self._sell_order):
            if self._sell_order:
                kalshi_client.cancel_order(self._sell_order["order_id"])
                self._sell_order = None
            # Always post a sell — closes long YES inventory if filled.
            result = kalshi_client.create_order(
                self.ticker, action="sell", side="yes",
                count=count, price=ask_px,
            )
            if result:
                self._sell_order = {
                    "order_id": result.get("order_id", "dry"),
                    "price":    ask_px,
                    "count":    count,
                }
                actions.append(f"sell@{ask_px:.2f}x{count}")
        else:
            actions.append(f"sell_unchanged@{ask_px:.2f}")

        mid = order_book_manager.mid_price(self.ticker)
        sp  = order_book_manager.spread(self.ticker)
        logger.debug(f"MM {self.ticker} mid={mid:.4f} spread={sp:.4f} "
                     f"actions={actions}")
        return {"action": "quoted", "actions": actions, "mid": mid, "spread": sp}

    def cancel_all(self):
        """Cancel both sides. Call when shutting down or rotating out."""
        kalshi_client.cancel_all_for_ticker(self.ticker)
        self._buy_order  = None
        self._sell_order = None
        logger.info(f"MM cancelled all quotes for {self.ticker}")
