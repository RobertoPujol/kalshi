"""Market scanner — finds tradeable Kalshi markets.

Walks the /markets endpoint and scores each open market on:
  - Volume (more = better price discovery)
  - Liquidity (more = tighter spreads)
  - YES-side spread tightness (tighter = cheaper to trade)
  - Price away from extremes (0.05–0.95 = real uncertainty)

Results are cached in memory between scan() calls.
"""
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

from core.adaptive_params import scanner_min_score, blacklisted_series
from core.markets import get_active_markets, get_market_summary
from core.risk import risk
from utils.logger import logger

PRICE_LOW  = float(os.getenv("SCANNER_PRICE_LOW",  "0.05"))
PRICE_HIGH = float(os.getenv("SCANNER_PRICE_HIGH", "0.95"))


def _score_market(m: dict) -> float:
    """Score a market 0–100 on trade-ability."""
    # Hard gates
    if m.get("liquidity", 0) < risk.min_liquidity:
        return 0.0
    sp = m.get("spread")
    if sp is not None and sp > risk.max_spread:
        return 0.0

    # Time-to-close gate: reject markets expiring within MIN_HOURS_TO_CLOSE.
    # Same-day event markets (MLB totals/spreads, intraday elections) have
    # rapidly-changing fair values and leave unsellable inventory at expiry.
    close_time_str = m.get("close_time")
    if close_time_str:
        try:
            close_dt   = datetime.fromisoformat(
                close_time_str.replace("Z", "+00:00"))
            hours_left = (close_dt - datetime.now(timezone.utc)
                          ).total_seconds() / 3600
            if hours_left < risk.min_hours_to_close:
                return 0.0
        except Exception:
            pass

    score = 0.0

    # Volume component (0–30): log-scale, 10k USD ≈ 30 pts
    vol = m.get("volume", 0)
    if vol > 0:
        score += min(30, math.log10(max(1, vol)) * 10)

    # Liquidity component (0–20): scaled for Kalshi's thinner markets
    liq = m.get("liquidity", 0)
    if liq >= risk.min_liquidity:
        score += min(20, liq / 50)

    # Spread component (0–20): tighter is better
    if sp is not None:
        if   sp <= 0.01: score += 20
        elif sp <= 0.02: score += 15
        elif sp <= 0.04: score += 10
        elif sp <= 0.06: score += 5
        elif sp <= 0.10: score += 2

    # Price away from extremes (0–30): max at 0.50
    yes = m.get("yes_price")
    if yes is not None and PRICE_LOW <= yes <= PRICE_HIGH:
        distance_from_extreme = min(yes - PRICE_LOW, PRICE_HIGH - yes)
        score += min(30, distance_from_extreme * 100)

    return round(score, 1)


class MarketScanner:
    def __init__(self):
        self.results: list[dict] = []
        self._last_scan: float = 0

    def scan(self, limit: int = 200,
             min_score: float | None = None) -> list[dict]:
        """Fetch open Kalshi markets, score and filter them.

        Returns list of market summaries sorted by score desc.
        min_score defaults to the adaptive value (falls back to env/default).
        """
        effective_min = min_score if min_score is not None else scanner_min_score()
        blocked = blacklisted_series()
        logger.info(f"Market scan starting (min_score={effective_min}, blacklisted={blocked or 'none'})...")
        try:
            raw = get_active_markets(limit=limit)
        except Exception as e:
            logger.error(f"Kalshi /markets fetch failed: {e}")
            return self.results

        import re
        def _series(ticker: str) -> str:
            m = re.match(r"^([A-Z]+)", ticker or "")
            return m.group(1) if m else ""

        scored = []
        for m in raw:
            summary = get_market_summary(m)
            if _series(summary.get("ticker", "")) in blocked:
                continue
            summary["score"] = _score_market(summary)
            if summary["score"] >= effective_min and not summary.get("closed"):
                scored.append(summary)

        scored.sort(key=lambda x: x["score"], reverse=True)
        self.results = scored
        self._last_scan = time.time()
        logger.info(f"Scan complete | {len(scored)} tradeable markets "
                    f"from {len(raw)} total")
        return scored

    def top(self, n: int = 10) -> list[dict]:
        return self.results[:n]

    def get(self, ticker: str) -> Optional[dict]:
        for m in self.results:
            if m.get("ticker") == ticker:
                return m
        return None

    def age_seconds(self) -> float:
        if not self._last_scan:
            return float("inf")
        return time.time() - self._last_scan


scanner = MarketScanner()
