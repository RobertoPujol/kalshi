"""High-level Kalshi market-data helpers.

Replaces what `core.gamma` did for Polymarket.  Layered on top of the
KalshiClient so the scanner and strategies stay platform-shaped without
caring about pagination or response envelopes.
"""
from typing import Optional

from core.client import kalshi_client
from utils.logger import logger


def get_active_markets(limit: int = 200,
                       series_ticker: Optional[str] = None,
                       min_volume: float = 0) -> list[dict]:
    """Return open (tradeable) markets, optionally filtered by series + volume.

    Kalshi paginates with a cursor; we walk pages until `limit` is hit.
    """
    out: list[dict] = []
    cursor: Optional[str] = None
    page_size = min(1000, max(50, limit))
    while len(out) < limit:
        page = kalshi_client.get_markets(
            limit=page_size, status="open",
            cursor=cursor, series_ticker=series_ticker,
        )
        markets = page.get("markets") or []
        if not markets:
            break
        out.extend(markets)
        cursor = page.get("cursor") or None
        if not cursor:
            break
    if min_volume:
        out = [m for m in out if float(m.get("volume") or 0) >= min_volume]
    return out[:limit]


def get_market(ticker: str) -> Optional[dict]:
    """Fetch a single market by ticker."""
    try:
        return kalshi_client.get_market(ticker)
    except Exception as e:
        logger.warning(f"get_market({ticker}) failed: {e}")
        return None


def get_markets_by_tickers(tickers: list[str]) -> list[dict]:
    """Batch-fetch a curated list of markets by ticker."""
    out = []
    for t in tickers:
        m = get_market(t)
        if m:
            out.append(m)
    return out


def _cents_to_dollars(v) -> Optional[float]:
    """Kalshi returns prices as integer cents (1–99).  Normalise to 0.01–0.99."""
    if v is None:
        return None
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return None


def get_market_summary(market: dict) -> dict:
    """Flatten a Kalshi /markets entry to the fields the scanner/strategy use.

    Kalshi notable fields (cents-denominated unless noted):
      ticker, event_ticker, title, yes_bid, yes_ask, no_bid, no_ask,
      last_price, volume, open_interest, liquidity, close_time, status
    """
    yes_bid = _cents_to_dollars(market.get("yes_bid"))
    yes_ask = _cents_to_dollars(market.get("yes_ask"))
    last    = _cents_to_dollars(market.get("last_price"))
    # Mid + spread off the YES side (NO side mirrors via 1 - x)
    mid = None
    spread = None
    if yes_bid is not None and yes_ask is not None:
        mid    = round((yes_bid + yes_ask) / 2, 4)
        spread = round(yes_ask - yes_bid, 4)
    yes_price = mid if mid is not None else last

    return {
        "ticker":       market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "question":     market.get("title") or market.get("subtitle"),
        "yes_bid":      yes_bid,
        "yes_ask":      yes_ask,
        "yes_price":    yes_price,
        "no_price":     (1.0 - yes_price) if yes_price is not None else None,
        "spread":       spread,
        "volume":       float(market.get("volume") or 0),
        "liquidity":    float(market.get("liquidity") or 0),
        "open_interest": float(market.get("open_interest") or 0),
        "close_time":   market.get("close_time"),
        "status":       market.get("status"),
        "active":       market.get("status") == "active",
        "closed":       market.get("status") in ("closed", "settled"),
    }
