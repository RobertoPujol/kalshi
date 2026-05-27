"""Position and risk management.

Enforces per-market position limits and total exposure caps so the
bot can't accidentally risk the whole account on one event.

All money values are in USD; sizes are in **contracts** (each Kalshi
contract pays $1 if it settles in your favour, $0 otherwise).

Knobs (all via env vars):
  MAX_POSITION_USD       Max USD exposure per market         (default 25)
  MAX_TOTAL_EXPOSURE     Max USD across all open positions   (default 100)
  MAX_POSITIONS          Max simultaneous open positions     (default 8)
  MIN_ORDER_CONTRACTS    Smallest order, in contracts        (default 2)
  MAX_SPREAD_ENTRY       Don't enter if YES spread > this    (default 0.06)
  MIN_LIQUIDITY          Min market liquidity (USD)          (default 500)
"""
import math
import os

from utils.logger import logger

MAX_POSITION_USD     = float(os.getenv("MAX_POSITION_USD",     "25"))
MAX_TOTAL_EXPOSURE   = float(os.getenv("MAX_TOTAL_EXPOSURE",   "100"))
MAX_POSITIONS        = int(  os.getenv("MAX_POSITIONS",        "8"))
MIN_ORDER_CONTRACTS  = int(  os.getenv("MIN_ORDER_CONTRACTS",  "2"))
MAX_SPREAD_ENTRY     = float(os.getenv("MAX_SPREAD_ENTRY",     "0.06"))
MIN_LIQUIDITY        = float(os.getenv("MIN_LIQUIDITY",        "500"))


def _position_exposure_usd(p: dict) -> float:
    """Best-effort exposure read from a Kalshi position dict.

    Kalshi `/portfolio/positions` returns `market_exposure` in cents
    (the worst-case loss on that position). Falls back to cost basis
    or the Polymarket-style `{size, avgPrice}` for test fixtures.
    """
    if "market_exposure" in p:
        return float(p.get("market_exposure") or 0) / 100.0
    if "total_traded" in p and "position" in p:
        # Cost basis in cents / 100 = dollar exposure approximation
        return abs(float(p.get("total_traded") or 0)) / 100.0
    # Legacy / test-fixture shape
    return float(p.get("size", 0)) * float(p.get("avgPrice", 0))


class RiskManager:
    """Stateless guard — call can_open() before placing any new position."""

    def __init__(self):
        self.max_position    = MAX_POSITION_USD
        self.max_exposure    = MAX_TOTAL_EXPOSURE
        self.max_positions   = MAX_POSITIONS
        self.min_order       = MIN_ORDER_CONTRACTS
        self.max_spread      = MAX_SPREAD_ENTRY
        self.min_liquidity   = MIN_LIQUIDITY

    def can_open(self, market_summary: dict, open_positions: list[dict],
                 balance: float) -> tuple[bool, str]:
        """Return (True, '') if it's safe to open a new position, or
        (False, reason) otherwise.

        Args:
            market_summary:  dict from markets.get_market_summary()
            open_positions:  list of position dicts from client.get_positions()
            balance:         available USD balance (dollars)
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

        total_exposure = sum(_position_exposure_usd(p) for p in open_positions)
        if total_exposure >= self.max_exposure:
            return False, (f"exposure ${total_exposure:.2f} >= max "
                           f"${self.max_exposure:.2f}")

        # Need at least min_order × ~$1 worth of headroom
        if balance < self.min_order:
            return False, f"balance ${balance:.2f} < min order {self.min_order}c"

        return True, ""

    def size_order(self, balance: float, open_positions: list[dict],
                   price: float) -> int:
        """Pick a safe order size **in contracts** for a new position.

        Respects MAX_POSITION_USD, remaining account-wide capacity, and
        keeps a 20% balance buffer.  Returns 0 if no order is safe.
        """
        if price <= 0:
            return 0
        total_exposure = sum(_position_exposure_usd(p) for p in open_positions)
        remaining = max(0, self.max_exposure - total_exposure)
        budget_usd = min(self.max_position, remaining, balance * 0.8)
        # contracts = floor(budget_usd / price_per_contract)
        contracts = int(math.floor(budget_usd / price))
        if contracts < self.min_order:
            return 0
        return contracts

    def snapshot(self) -> dict:
        return {
            "max_position_usd":   self.max_position,
            "max_total_exposure": self.max_exposure,
            "max_positions":      self.max_positions,
            "min_order_contracts": self.min_order,
            "max_spread_entry":   self.max_spread,
            "min_liquidity":      self.min_liquidity,
        }


risk = RiskManager()
