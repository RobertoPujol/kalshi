"""Position and risk management.

Enforces per-market position limits and total exposure caps so the
bot can't inadvertently risk the whole wallet on one market.

Knobs (all via env vars):
  MAX_POSITION_USDC      Max USDC per market (default 25)
  MAX_TOTAL_EXPOSURE     Max total USDC across all open positions (default 100)
  MAX_POSITIONS          Max number of simultaneous open positions (default 8)
  MIN_ORDER_USDC         Minimum order size (default 2)
  MAX_SPREAD_ENTRY       Don't enter if spread > this (default 0.06 = 6 cents)
  MIN_LIQUIDITY          Don't enter a market with less liquidity than this (default 500 USDC)
"""
import os
from utils.logger import logger

MAX_POSITION_USDC  = float(os.getenv("MAX_POSITION_USDC",  "25"))
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "100"))
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS",        "8"))
MIN_ORDER_USDC     = float(os.getenv("MIN_ORDER_USDC",     "2"))
MAX_SPREAD_ENTRY   = float(os.getenv("MAX_SPREAD_ENTRY",   "0.06"))
MIN_LIQUIDITY      = float(os.getenv("MIN_LIQUIDITY",      "500"))


class RiskManager:
    """Stateless guard — call can_open() before placing any new position."""

    def __init__(self):
        self.max_position  = MAX_POSITION_USDC
        self.max_exposure  = MAX_TOTAL_EXPOSURE
        self.max_positions = MAX_POSITIONS
        self.min_order     = MIN_ORDER_USDC
        self.max_spread    = MAX_SPREAD_ENTRY
        self.min_liquidity = MIN_LIQUIDITY

    def can_open(self, market_summary: dict, open_positions: list[dict],
                 balance: float) -> tuple[bool, str]:
        """
        Return (True, "") if it's safe to open a new position, or
        (False, reason) otherwise.

        Args:
            market_summary:  dict from gamma.get_market_summary()
            open_positions:  list of current open position dicts
            balance:         available USDC balance
        """
        # Liquidity gate
        liq = market_summary.get("liquidity", 0)
        if liq < self.min_liquidity:
            return False, f"liquidity {liq:.0f} < min {self.min_liquidity:.0f}"

        # Spread gate
        sp = market_summary.get("spread")
        if sp is not None and sp > self.max_spread:
            return False, f"spread {sp:.4f} > max {self.max_spread:.4f}"

        # Count / exposure gates
        n_open = len(open_positions)
        if n_open >= self.max_positions:
            return False, f"at position cap ({n_open}/{self.max_positions})"

        total_exposure = sum(float(p.get("size", 0)) * float(p.get("avgPrice", 0))
                             for p in open_positions)
        if total_exposure >= self.max_exposure:
            return False, f"exposure {total_exposure:.2f} >= max {self.max_exposure:.2f}"

        if balance < self.min_order:
            return False, f"balance {balance:.2f} < min order {self.min_order:.2f}"

        return True, ""

    def size_order(self, balance: float, open_positions: list[dict],
                   price: float) -> float:
        """
        Calculate safe order size in USDC for a new position.
        Respects MAX_POSITION_USDC and remaining capacity.
        """
        total_exposure = sum(float(p.get("size", 0)) * float(p.get("avgPrice", 0))
                             for p in open_positions)
        remaining_capacity = max(0, self.max_exposure - total_exposure)
        size = min(self.max_position, remaining_capacity, balance * 0.8)
        if size < self.min_order:
            return 0.0
        # Round to 2 decimal places (USDC cents)
        return round(size, 2)

    def snapshot(self) -> dict:
        return {
            "max_position_usdc":  self.max_position,
            "max_total_exposure": self.max_exposure,
            "max_positions":      self.max_positions,
            "min_order_usdc":     self.min_order,
            "max_spread_entry":   self.max_spread,
            "min_liquidity":      self.min_liquidity,
        }


risk = RiskManager()
