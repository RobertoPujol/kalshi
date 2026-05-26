"""Market scanner — finds tradeable Polymarket markets.

Scans the Gamma API for active markets and scores them on:
  - Volume (more = better price discovery)
  - Liquidity (more = tighter spreads)
  - Spread tightness (tighter = cheaper to trade)
  - Price away from extremes (0.05 – 0.95 range = uncertainty = edge)
  - Recent activity

Results are cached in memory and refreshed on each scan() call.
"""
import os
import time
from typing import Optional
from core.gamma import get_active_markets, get_market_summary
from core.risk import risk
from utils.logger import logger

# Min score (0-100) to be considered tradeable
MIN_SCORE = float(os.getenv("SCANNER_MIN_SCORE", "30"))
# Price range where market making makes sense (too close to 0/1 → no uncertainty)
PRICE_LOW  = float(os.getenv("SCANNER_PRICE_LOW",  "0.05"))
PRICE_HIGH = float(os.getenv("SCANNER_PRICE_HIGH", "0.95"))


def _score_market(m: dict) -> float:
    """Score a market 0-100 based on trade-ability."""
    # Hard gates — below these thresholds the market isn't tradeable at all
    if m.get("liquidity", 0) < risk.min_liquidity:
        return 0.0
    sp = m.get("spread")
    if sp is not None and sp > risk.max_spread:
        return 0.0

    score = 0.0

    # Volume component (0-30 pts): log-scale, 10k USDC ≈ 30 pts
    vol = m.get("volume", 0)
    if vol > 0:
        import math
        score += min(30, math.log10(max(1, vol)) * 10)

    # Liquidity component (0-20 pts)
    liq = m.get("liquidity", 0)
    if liq >= risk.min_liquidity:
        score += min(20, liq / 500)

    # Spread component (0-20 pts): tighter is better
    sp = m.get("spread")
    if sp is not None:
        if sp <= 0.01:   score += 20
        elif sp <= 0.02: score += 15
        elif sp <= 0.04: score += 10
        elif sp <= 0.06: score += 5

    # Price away from extremes (0-30 pts)
    yes = m.get("yes_price")
    if yes is not None and PRICE_LOW <= yes <= PRICE_HIGH:
        # Max score at 0.50 (maximum uncertainty)
        distance_from_extreme = min(yes - PRICE_LOW, PRICE_HIGH - yes)
        score += min(30, distance_from_extreme * 100)

    return round(score, 1)


class MarketScanner:
    def __init__(self):
        self.results: list[dict] = []
        self._last_scan: float = 0

    def scan(self, limit: int = 100, min_score: float = MIN_SCORE) -> list[dict]:
        """
        Fetch active markets from Gamma API, score and filter them.
        Returns list of market dicts sorted by score desc.
        """
        logger.info("Market scan starting...")
        try:
            raw = get_active_markets(limit=limit)
        except Exception as e:
            logger.error(f"Gamma API fetch failed: {e}")
            return self.results

        scored = []
        for m in raw:
            summary = get_market_summary(m)
            summary["score"] = _score_market(summary)
            if summary["score"] >= min_score and not summary.get("closed"):
                scored.append(summary)

        scored.sort(key=lambda x: x["score"], reverse=True)
        self.results = scored
        self._last_scan = time.time()
        logger.info(f"Scan complete | {len(scored)} tradeable markets from {len(raw)} total")
        return scored

    def top(self, n: int = 10) -> list[dict]:
        return self.results[:n]

    def get(self, slug: str) -> Optional[dict]:
        for m in self.results:
            if m.get("slug") == slug:
                return m
        return None

    def age_seconds(self) -> float:
        if not self._last_scan:
            return float("inf")
        return time.time() - self._last_scan


scanner = MarketScanner()
