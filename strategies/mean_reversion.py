"""Mean-reversion strategy for Kalshi binary markets.

Maintains an exponential moving average (EMA) of the YES mid-price.
When the live mid deviates from the EMA by more than MR_BAND, enters a
position betting the price reverts.

  Price << EMA  →  market temporarily oversold  →  BUY YES
  Price >> EMA  →  market temporarily overbought →  BUY NO

Exits on take-profit, stop-loss, or when the price returns within
MR_EXIT_BAND of the EMA (the reversion target).

Env knobs:
  MR_EMA_ALPHA     EMA smoothing factor, 0–1 (default 0.15 ≈ 6-tick halflife)
  MR_BAND          Deviation from EMA required to enter, in $ (default 0.06)
  MR_EXIT_BAND     Deviation at which a held position is closed as reverted (default 0.02)
  MR_TAKE_PROFIT   Profit target per contract in $ (default 0.03)
  MR_STOP_LOSS     Max loss per contract in $ (default 0.03)
"""
import os
from typing import Optional

from core.client import kalshi_client
from core.orderbook import order_book_manager
from core.risk import risk
from utils.logger import logger

MR_EMA_ALPHA   = float(os.getenv("MR_EMA_ALPHA",    "0.15"))
MR_BAND        = float(os.getenv("MR_BAND",          "0.06"))
MR_EXIT_BAND   = float(os.getenv("MR_EXIT_BAND",     "0.02"))
MR_TAKE_PROFIT = float(os.getenv("MR_TAKE_PROFIT",   "0.03"))
MR_STOP_LOSS   = float(os.getenv("MR_STOP_LOSS",     "0.03"))


def _round_price(p: float) -> float:
    return round(round(p / 0.01) * 0.01, 2)


class MeanReversionTrader:
    """Executes mean-reversion trades on a single Kalshi ticker."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._ema: Optional[float] = None
        self._position: Optional[dict] = None  # {side, entry_price, count, order_id}

    def _update_ema(self, mid: float) -> float:
        if self._ema is None:
            self._ema = mid
        else:
            self._ema = MR_EMA_ALPHA * mid + (1 - MR_EMA_ALPHA) * self._ema
        return self._ema

    def update(self, balance: float, open_positions: list[dict],
               market_summary: dict) -> dict:
        """Called each cycle. Updates EMA, checks for entry/exit signals."""
        mid = order_book_manager.mid_price(self.ticker)
        if mid is None:
            return {"action": "skip", "reason": "no_mid_price"}

        ema = self._update_ema(mid)
        deviation = mid - ema   # positive = price above average

        # ── Exit ────────────────────────────────────────────────────────────
        if self._position:
            entry = self._position["entry_price"]
            side  = self._position["side"]
            current_value = mid if side == "yes" else (1.0 - mid)
            pnl_per = current_value - entry

            if pnl_per >= MR_TAKE_PROFIT:
                return self._close("take_profit", mid)
            if pnl_per <= -MR_STOP_LOSS:
                return self._close("stop_loss", mid)
            # Reversion exit: price returned close enough to EMA
            if side == "yes" and deviation >= -MR_EXIT_BAND:
                return self._close("reverted", mid)
            if side == "no"  and deviation <= MR_EXIT_BAND:
                return self._close("reverted", mid)

            return {"action": "hold", "side": side, "pnl_per": round(pnl_per, 4)}

        # ── Entry ────────────────────────────────────────────────────────────
        if abs(deviation) < MR_BAND:
            return {"action": "watch", "mid": mid, "ema": round(ema, 4),
                    "deviation": round(deviation, 4)}

        ok, reason = risk.can_open(market_summary, open_positions, balance)
        if not ok:
            return {"action": "skip", "reason": reason}

        # Price dropped below EMA → buy YES (expect reversion upward)
        # Price rose above EMA  → buy NO  (expect reversion downward)
        if deviation < 0:
            side = "yes"
            entry_price = _round_price(mid)
        else:
            side = "no"
            entry_price = _round_price(1.0 - mid)

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
            logger.info(f"MeanReversion ENTRY BUY {side.upper()} {count}c "
                        f"@${entry_price:.2f} ticker={self.ticker} "
                        f"ema={ema:.4f} dev={deviation:+.4f}")
            return {"action": "entry", "side": side, "price": entry_price,
                    "count": count, "deviation": deviation}
        return {"action": "skip", "reason": "order_failed"}

    def _close(self, reason: str, current_yes_mid: float) -> dict:
        if not self._position:
            return {}
        side  = self._position["side"]
        count = self._position["count"]
        entry = self._position["entry_price"]
        sell_price = _round_price(
            current_yes_mid if side == "yes" else 1.0 - current_yes_mid
        )
        pnl_per = sell_price - entry
        kalshi_client.create_order(
            self.ticker, action="sell", side=side,
            count=count, price=sell_price,
        )
        logger.info(f"MeanReversion EXIT {reason} pnl/contract=${pnl_per:+.4f} "
                    f"x{count} ticker={self.ticker}")
        self._position = None
        return {"action": "exit", "reason": reason,
                "pnl_per": round(pnl_per, 4), "count": count}

    def cancel_all(self):
        kalshi_client.cancel_all_for_ticker(self.ticker)
        self._position = None
        logger.info(f"MeanReversion cancelled all for {self.ticker}")
