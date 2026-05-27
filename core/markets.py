"""High-level Kalshi market-data helpers.

Replaces what `core.gamma` did for Polymarket.  Layered on top of the
KalshiClient so the scanner and strategies stay platform-shaped without
caring about pagination or response envelopes.
"""
from typing import Optional

from core.client import kalshi_client
from utils.logger import logger


def _to_float(v) -> float:
    """Kalshi returns numbers as strings (e.g. '0.1520', '125.00').  Parse."""
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _market_volume(m: dict) -> float:
    """24h traded volume in contracts. Kalshi field is `volume_24h_fp` (str).

    We use the 24-hour window — not lifetime — because lifetime volume
    keeps long-dead esports combo markets at the top of the list. The
    24h figure is the right "is anybody trading this *now*" signal.
    """
    return _to_float(m.get("volume_24h_fp")
                     or m.get("volume_24h")
                     or m.get("volume_fp")
                     or m.get("volume"))


def _market_liquidity(m: dict) -> float:
    """Approximate USD liquidity at top of book — Kalshi doesn't expose a
    single `liquidity` field, so we synthesise it as:
        yes_bid_size_fp * yes_bid_dollars  +  no_bid_size_fp * no_bid_dollars
    (i.e. the $ value of resting bids on both sides of the book).
    """
    yb_size = _to_float(m.get("yes_bid_size_fp"))
    yb_px   = _to_float(m.get("yes_bid_dollars"))
    nb_size = _to_float(m.get("no_bid_size_fp"))
    nb_px   = _to_float(m.get("no_bid_dollars"))
    return yb_size * yb_px + nb_size * nb_px


def get_active_markets(limit: int = 200,
                       series_ticker: Optional[str] = None,
                       min_volume: float = 1.0,
                       max_pages: int = 50) -> list[dict]:
    """Return open (tradeable) markets, sorted by 24h volume descending.

    Kalshi `/markets` returns results in no useful order — the first several
    thousand entries are zero-volume placeholder series. We walk pages and
    pre-filter on `min_volume` (24h contracts traded) inside the loop,
    stopping when we've found `limit` qualifying markets or exhausted
    `max_pages` of API calls.

    With page_size=1000 and max_pages=50 we can walk up to 50k markets per
    scan; in practice we stop well before that.
    """
    out: list[dict] = []
    cursor: Optional[str] = None
    page_size = 1000
    pages_walked = 0
    total_seen = 0
    while len(out) < limit and pages_walked < max_pages:
        page = kalshi_client.get_markets(
            limit=page_size, status="open",
            cursor=cursor, series_ticker=series_ticker,
        )
        pages_walked += 1
        markets = page.get("markets") or []
        if not markets:
            break
        total_seen += len(markets)
        for m in markets:
            if _market_volume(m) >= min_volume:
                out.append(m)
        cursor = page.get("cursor") or None
        if not cursor:
            break

    logger.debug(f"get_active_markets: walked {pages_walked} page(s) "
                 f"({total_seen} markets), kept {len(out)} with "
                 f"24h volume>={min_volume}")
    out.sort(key=_market_volume, reverse=True)
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


def get_market_summary(market: dict) -> dict:
    """Flatten a Kalshi /markets entry to the fields scanner + strategies use.

    Kalshi's response keys (current as of 2026-05):
      ticker, event_ticker, title, subtitle, status
      yes_bid_dollars / yes_ask_dollars   (string $ amounts, "0.0000"-"1.0000")
      no_bid_dollars  / no_ask_dollars
      yes_bid_size_fp / yes_ask_size_fp   (string contract counts)
      no_bid_size_fp  / no_ask_size_fp
      volume_fp, volume_24h_fp, open_interest_fp   (string contract counts)
      last_price_dollars
      open_time, close_time
    Prices arrive as decimal-dollar strings; sizes as float-point strings.
    """
    yes_bid = _to_float(market.get("yes_bid_dollars")) or None
    yes_ask = _to_float(market.get("yes_ask_dollars")) or None
    no_bid  = _to_float(market.get("no_bid_dollars"))  or None
    no_ask  = _to_float(market.get("no_ask_dollars"))  or None
    last    = _to_float(market.get("last_price_dollars")) or None

    mid = None
    spread = None
    if yes_bid is not None and yes_ask is not None and yes_ask > yes_bid:
        mid    = round((yes_bid + yes_ask) / 2, 4)
        spread = round(yes_ask - yes_bid, 4)
    yes_price = mid if mid is not None else last

    status = market.get("status")
    return {
        "ticker":        market.get("ticker"),
        "event_ticker":  market.get("event_ticker"),
        "question":      market.get("title") or market.get("subtitle"),
        "yes_bid":       yes_bid,
        "yes_ask":       yes_ask,
        "no_bid":        no_bid,
        "no_ask":        no_ask,
        "yes_price":     yes_price,
        "no_price":      (1.0 - yes_price) if yes_price is not None else None,
        "spread":        spread,
        "volume":        _market_volume(market),
        "volume_24h":    _to_float(market.get("volume_24h_fp")),
        "liquidity":     _market_liquidity(market),
        "open_interest": _to_float(market.get("open_interest_fp")),
        "close_time":    market.get("close_time"),
        "status":        status,
        "active":        status == "active",
        "closed":        status in ("closed", "settled", "finalized"),
    }
