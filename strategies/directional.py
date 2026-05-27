"""Directional (delta) strategy for Kalshi binary markets.

Opens a YES position when market price diverges from our fair-value
estimate by more than MIN_DEVIATION, then closes on either TAKE_PROFIT
or STOP_LOSS.

Fair-value sources (in priority order):
  1. External signal (pluggable via FairValueProvider)
  2. None → no trade (sit out)

Env knobs:
  DIR_MIN_DEVIATION    Min price gap vs fair value to enter (default 0.05)
  DIR_TAKE_PROFIT      Close at this $ gain per contract    (default 0.04)
  DIR_STOP_LOSS        Close at this $ loss per contract    (default 0.03)
"""
import os
from typing import Optional, Protocol

from core.client import kalshi_client
from core.orderbook import order_book_manager
from core.risk import risk
from utils.logger import logger

MIN_DEVIATION = float(os.getenv("DIR_MIN_DEVIATION", "0.05"))
TAKE_PROFIT   = float(os.getenv("DIR_TAKE_PROFIT",   "0.04"))
STOP_LOSS     = float(os.getenv("DIR_STOP_LOSS",     "0.03"))


class FairValueProvider(Protocol):
    """Plug in your own model by implementing this interface."""
    def fair_value(self, ticker: str) -> Optional[float]:
        """Return YES probability (0–1) or None if no estimate."""
        ...


def _round_price(p: float) -> float:
    """Round to Kalshi 1¢ tick."""
    return round(round(p / 0.01) * 0.01, 2)


class DirectionalTrader:
    """Executes directional trades on a single Kalshi ticker."""

    def __init__(self, ticker: str,
                 fair_value_provider: Optional[FairValueProvider] = None):
        self.ticker = ticker
        self._fv_provider = fair_value_provider
        # Open position: None or {"side", "entry_price", "count", "order_id"}
        # side is "yes"|"no" — the side we bought
        self._position: Optional[dict] = None

    def _get_fair_value(self) -> Optional[float]:
        if self._fv_provider:
            return self._fv_provider.fair_value(self.ticker)
        return None

    # ── Entry / exit logic ───────────────────────────────────────────────────

    def update(self, balance: float, open_positions: list[dict],
               market_summary: dict) -> dict:
        """Check for entry or exit signals once per cycle."""
        mid = order_book_manager.mid_price(self.ticker)
        if mid is None:
            return {"action": "skip", "reason": "no_mid_price"}

        # ── Exit logic for an existing position ──────────────────────────────
        if self._position:
            entry = self._position["entry_price"]
            side  = self._position["side"]   # "yes" | "no"
            # P&L per contract = (current price - entry) for a YES long;
            # for a NO long, current "value" = 1 - yes_mid.
            current_value = mid if side == "yes" else (1.0 - mid)
            pnl_per = current_value - entry
            if pnl_per >= TAKE_PROFIT:
                return self._close("take_profit", mid)
            if pnl_per <= -STOP_LOSS:
                return self._close("stop_loss", mid)
            return {"action": "hold", "side": side, "pnl_per": round(pnl_per, 4)}

        # ── Entry ────────────────────────────────────────────────────────────
        fv = self._get_fair_value()
        if fv is None:
            return {"action": "skip", "reason": "no_fair_value"}

        deviation = fv - mid       # positive → market underpricing YES
        if abs(deviation) < MIN_DEVIATION:
            return {"action": "watch", "mid": mid, "fv": fv,
                    "deviation": round(deviation, 4)}

        ok, reason = risk.can_open(market_summary, open_positions, balance)
        if not ok:
            return {"action": "skip", "reason": reason}

        # Underpriced YES → buy YES; overpriced YES → buy NO at (1 - mid).
        if deviation > 0:
            side, entry_price = "yes", _round_price(mid)
        else:
            side, entry_price = "no",  _round_price(1.0 - mid)

        count = risk.size_order(balance, open_positions, entry_price)
        if count <= 0:
            return {"action": "skip", "reason": "size_zero"}

        result = kalshi_client.create_order(
            self.ticker, action="buy", side=side,
            count=count, price=entry_price,
        )
        if result:
            self._position = {
                "side":        side,
                "entry_price": entry_price,
                "count":       count,
                "order_id":    result.get("order_id", "dry"),
            }
            logger.info(f"Directional ENTRY BUY {side.upper()} {count}c "
                        f"@${entry_price:.2f} ticker={self.ticker} "
                        f"fv={fv:.4f} dev={deviation:+.4f}")
            return {"action": "entry", "side": side, "price": entry_price,
                    "count": count, "deviation": deviation}
        return {"action": "skip", "reason": "order_failed"}

    def _close(self, reason: str, current_yes_mid: float) -> dict:
        if not self._position:
            return {}
        side  = self._position["side"]
        count = self._position["count"]
        entry = self._position["entry_price"]
        # Closing = SELL the side we hold, at current market.
        sell_price = (current_yes_mid if side == "yes"
                      else 1.0 - current_yes_mid)
        sell_price = _round_price(sell_price)
        pnl_per = sell_price - entry
        kalshi_client.create_order(
            self.ticker, action="sell", side=side,
            count=count, price=sell_price,
        )
        logger.info(f"Directional EXIT {reason} pnl/contract=${pnl_per:+.4f} "
                    f"x{count} ticker={self.ticker}")
        self._position = None
        return {"action": "exit", "reason": reason,
                "pnl_per": round(pnl_per, 4), "count": count}
