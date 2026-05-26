"""Directional (delta) strategy for binary prediction markets.

Takes a YES or NO position when a market price deviates significantly
from our fair-value estimate.

Fair-value sources (in priority order):
  1. External model / signal (pluggable via FairValueProvider)
  2. Simple mean-reversion: price has moved >DEVIATION from 7d average
  3. Falls back to no-trade if neither source is available

Env knobs:
  DIR_MIN_DEVIATION    Min price gap vs fair value to enter (default 0.05 = 5¢)
  DIR_TAKE_PROFIT      Close at this P&L gain (default 0.04 = 4¢)
  DIR_STOP_LOSS        Close at this P&L loss (default 0.03 = 3¢)
"""
import os
from typing import Optional, Protocol
from core.orderbook import order_book_manager
from core.client import poly_client
from core.risk import risk
from utils.logger import logger

MIN_DEVIATION = float(os.getenv("DIR_MIN_DEVIATION", "0.05"))
TAKE_PROFIT   = float(os.getenv("DIR_TAKE_PROFIT",   "0.04"))
STOP_LOSS     = float(os.getenv("DIR_STOP_LOSS",     "0.03"))


class FairValueProvider(Protocol):
    """Plug in your own model by implementing this interface."""
    def fair_value(self, slug: str) -> Optional[float]:
        """Return a probability (0-1) or None if no estimate available."""
        ...


class DirectionalTrader:
    """
    Executes directional trades on a single market when price diverges
    from fair value.
    """

    def __init__(self, token_id: str, slug: str,
                 fair_value_provider: Optional[FairValueProvider] = None,
                 neg_risk: bool = False):
        self.token_id = token_id
        self.slug     = slug
        self.neg_risk = neg_risk
        self._fv_provider = fair_value_provider
        # Track our open position: None or {"side", "entry_price", "size", "order_id"}
        self._position: Optional[dict] = None

    # ── Fair value ────────────────────────────────────────────────────────────

    def _get_fair_value(self) -> Optional[float]:
        if self._fv_provider:
            return self._fv_provider.fair_value(self.slug)
        return None

    # ── Entry / exit logic ───────────────────────────────────────────────────

    def update(self, balance: float, open_positions: list[dict],
               market_summary: dict) -> dict:
        """
        Called each trading cycle.  Checks for entry or exit signals.
        """
        mid = order_book_manager.mid_price(self.token_id)
        if mid is None:
            return {"action": "skip", "reason": "no_mid_price"}

        # ── Check exit on existing position ──────────────────────────────────
        if self._position:
            entry = self._position["entry_price"]
            side  = self._position["side"]
            pnl   = (mid - entry) if side == "BUY" else (entry - mid)
            if pnl >= TAKE_PROFIT:
                return self._close("take_profit", mid)
            if pnl <= -STOP_LOSS:
                return self._close("stop_loss", mid)
            return {"action": "hold", "side": side, "pnl": round(pnl, 4)}

        # ── Check entry ───────────────────────────────────────────────────────
        fv = self._get_fair_value()
        if fv is None:
            return {"action": "skip", "reason": "no_fair_value"}

        deviation = fv - mid   # positive → market underpricing YES → BUY
        if abs(deviation) < MIN_DEVIATION:
            return {"action": "watch", "mid": mid, "fv": fv,
                    "deviation": round(deviation, 4)}

        ok, reason = risk.can_open(market_summary, open_positions, balance)
        if not ok:
            return {"action": "skip", "reason": reason}

        side  = "BUY" if deviation > 0 else "SELL"
        size  = risk.size_order(balance, open_positions, mid)
        if size <= 0:
            return {"action": "skip", "reason": "size_zero"}

        price = mid  # market order equivalent — use mid for limit
        result = poly_client.create_order(self.token_id, side, price,
                                          size, self.neg_risk)
        if result:
            self._position = {
                "side":        side,
                "entry_price": price,
                "size":        size,
                "order_id":    result.get("orderID", "dry"),
            }
            logger.info(f"Directional ENTRY {side} {size:.2f}@{price:.4f} "
                        f"slug={self.slug} fv={fv:.4f} dev={deviation:+.4f}")
            return {"action": "entry", "side": side, "price": price,
                    "size": size, "deviation": deviation}
        return {"action": "skip", "reason": "order_failed"}

    def _close(self, reason: str, current_price: float) -> dict:
        if not self._position:
            return {}
        entry  = self._position["entry_price"]
        side   = self._position["side"]
        size   = self._position["size"]
        pnl    = (current_price - entry) * size if side == "BUY" \
                 else (entry - current_price) * size
        close_side = "SELL" if side == "BUY" else "BUY"
        poly_client.create_order(self.token_id, close_side,
                                 current_price, size, self.neg_risk)
        logger.info(f"Directional EXIT {reason} pnl={pnl:+.4f} slug={self.slug}")
        self._position = None
        return {"action": "exit", "reason": reason, "pnl": round(pnl, 4)}
