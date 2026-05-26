"""Polymarket Gamma API client.

Fetches market metadata, current prices, and volume from the public
Gamma API (no authentication required).

Useful docs:
  https://docs.polymarket.com/
  Endpoint: https://gamma-api.polymarket.com/markets
"""
import os
import time
import requests
from typing import Optional
from utils.logger import logger

GAMMA_BASE = "https://gamma-api.polymarket.com"
_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json", "User-Agent": "polytrader/1.0"})


def _get(path: str, params: dict = None, retries: int = 3) -> list | dict:
    url = f"{GAMMA_BASE}{path}"
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                logger.error(f"Gamma API error {url}: {e}")
                raise
            time.sleep(1.5 ** attempt)


def get_active_markets(limit: int = 200, tag_slug: str = None,
                       min_volume: float = 0) -> list[dict]:
    """Return active markets, optionally filtered by tag and minimum volume."""
    params = {"limit": limit, "active": "true", "closed": "false"}
    if tag_slug:
        params["tag_slug"] = tag_slug
    markets = _get("/markets", params) or []
    if min_volume:
        markets = [m for m in markets
                   if float(m.get("volume") or 0) >= min_volume]
    return markets


def get_market(slug: str) -> Optional[dict]:
    """Fetch a single market by slug."""
    results = _get("/markets", {"slug": slug})
    if isinstance(results, list) and results:
        return results[0]
    return None


def get_markets_by_slugs(slugs: list[str]) -> list[dict]:
    """Batch-fetch a curated list of markets by slug."""
    out = []
    for slug in slugs:
        try:
            m = get_market(slug)
            if m:
                out.append(m)
        except Exception as e:
            logger.warning(f"Could not fetch market {slug}: {e}")
    return out


def get_events(limit: int = 50, tag_slug: str = None) -> list[dict]:
    """Fetch grouped events (each event contains one or more markets)."""
    params = {"limit": limit, "active": "true"}
    if tag_slug:
        params["tag_slug"] = tag_slug
    return _get("/events", params) or []


def parse_price(market: dict, outcome: str = "YES") -> Optional[float]:
    """Extract the YES or NO probability from a market dict."""
    prices = market.get("outcomePrices") or market.get("outcome_prices")
    outcomes = market.get("outcomes")
    if not prices or not outcomes:
        return None
    try:
        if isinstance(prices, str):
            import json
            prices = json.loads(prices)
        if isinstance(outcomes, str):
            import json
            outcomes = json.loads(outcomes)
        idx = outcomes.index(outcome)
        return float(prices[idx])
    except (ValueError, IndexError, TypeError):
        return None


def get_market_summary(market: dict) -> dict:
    """Flatten a raw market dict to the fields we care about."""
    yes_price = parse_price(market, "YES")
    no_price  = parse_price(market, "NO")
    spread = None
    if yes_price is not None and no_price is not None:
        # In a binary market YES + NO ≈ 1; spread = how much house takes
        spread = round(1.0 - yes_price - no_price, 4)
    return {
        "slug":        market.get("slug"),
        "question":    market.get("question"),
        "condition_id": market.get("conditionId"),
        "token_ids":   market.get("clobTokenIds"),
        "yes_price":   yes_price,
        "no_price":    no_price,
        "spread":      spread,
        "volume":      float(market.get("volume") or 0),
        "liquidity":   float(market.get("liquidity") or 0),
        "end_date":    market.get("endDate"),
        "active":      market.get("active", True),
        "closed":      market.get("closed", False),
    }
