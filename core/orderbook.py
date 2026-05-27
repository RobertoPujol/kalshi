"""Real-time order book via Kalshi WebSocket feed.

Subscribes to the `orderbook_delta` channel for one or more market
tickers and maintains an in-memory snapshot strategies can query
without round-tripping the REST API every cycle.

Kalshi orderbook quirks:
  - Every binary market is bid-only on both sides (`yes` and `no` arrays).
    Selling YES is equivalent to buying NO at (1 - price), so we synthesise
    YES asks from NO bids: yes_ask = 1 - max(no_bids).
  - Prices on the wire are integer cents (1-99). We normalise to dollars
    (0.01-0.99) on the way in.
  - First message per ticker is `orderbook_snapshot`; subsequent updates
    are `orderbook_delta`.  A delta with size=0 deletes the level.

Usage:
    from core.orderbook import order_book_manager
    order_book_manager.subscribe("PRES-2024-DJT")
    bid, ask = order_book_manager.best_bid_ask("PRES-2024-DJT")
"""
import asyncio
import json
import threading
import time
from typing import Optional

from core.auth import get_auth
from utils.logger import logger

WS_PATH = "/trade-api/ws/v2"
_WS_HOSTS = {
    "prod": "wss://api.elections.kalshi.com",
    "demo": "wss://demo-api.kalshi.co",
}


def _ws_url() -> str:
    import os
    env = os.getenv("KALSHI_ENV", "prod").strip().lower()
    return _WS_HOSTS.get(env, _WS_HOSTS["prod"]) + WS_PATH


# Global state per ticker:
#   {"yes_bids": {price: size}, "no_bids": {price: size}, "ts": epoch}
_books: dict[str, dict] = {}
_lock = threading.Lock()


def _empty_book() -> dict:
    return {"yes_bids": {}, "no_bids": {}, "ts": time.time()}


def _apply_snapshot(ticker: str, msg: dict):
    """Replace the book with a fresh snapshot."""
    with _lock:
        book = _empty_book()
        for price_cents, size in (msg.get("yes") or []):
            book["yes_bids"][price_cents / 100.0] = size
        for price_cents, size in (msg.get("no") or []):
            book["no_bids"][price_cents / 100.0] = size
        _books[ticker] = book


def _apply_delta(ticker: str, msg: dict):
    """Apply a single price-level delta. size=0 removes the level."""
    with _lock:
        book = _books.get(ticker)
        if not book:
            # Delta arrived before snapshot — initialise empty and fill in.
            book = _empty_book()
            _books[ticker] = book
        side = msg.get("side", "").lower()        # "yes" or "no"
        price = msg.get("price")
        size  = msg.get("delta", 0)               # absolute new size, or delta?
        # Kalshi delta: "delta" is the change in resting contracts.
        if price is None or side not in ("yes", "no"):
            return
        price_d = float(price) / 100.0
        bucket = "yes_bids" if side == "yes" else "no_bids"
        current = book[bucket].get(price_d, 0)
        new = current + size
        if new <= 0:
            book[bucket].pop(price_d, None)
        else:
            book[bucket][price_d] = new
        book["ts"] = time.time()


def best_bid_ask(ticker: str) -> tuple[Optional[float], Optional[float]]:
    """Best YES bid and YES ask (ask synthesised from NO bids)."""
    with _lock:
        book = _books.get(ticker)
        if not book:
            return None, None
        yes_bids = book["yes_bids"]
        no_bids  = book["no_bids"]
        yes_bid = max(yes_bids.keys()) if yes_bids else None
        # yes_ask = 1 - best NO bid (the price you'd pay to "sell YES" = buy NO)
        yes_ask = (1.0 - max(no_bids.keys())) if no_bids else None
    return yes_bid, yes_ask


def mid_price(ticker: str) -> Optional[float]:
    bid, ask = best_bid_ask(ticker)
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 4)
    return None


def spread(ticker: str) -> Optional[float]:
    bid, ask = best_bid_ask(ticker)
    if bid is not None and ask is not None:
        return round(ask - bid, 4)
    return None


def book_age_seconds(ticker: str) -> float:
    with _lock:
        book = _books.get(ticker)
        if not book:
            return float("inf")
        return time.time() - book["ts"]


async def _run_ws(tickers: list[str]):
    import websockets

    sub_id = 1
    sub_msg = json.dumps({
        "id": sub_id,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": tickers,
        },
    })

    while True:
        # Sign the WS handshake using the same scheme as REST.
        headers = get_auth().sign("GET", WS_PATH)
        try:
            async with websockets.connect(
                _ws_url(),
                additional_headers=list(headers.items()),
                ping_interval=20, ping_timeout=10,
            ) as ws:
                await ws.send(sub_msg)
                logger.info(f"OrderBook WS connected for {len(tickers)} ticker(s)")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        m     = msg.get("msg") or {}
                        ticker = m.get("market_ticker")
                        if not ticker:
                            continue
                        if mtype == "orderbook_snapshot":
                            _apply_snapshot(ticker, m)
                        elif mtype == "orderbook_delta":
                            _apply_delta(ticker, m)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"OrderBook WS error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


class OrderBookManager:
    """Runs the WS listener in a background thread.

    `subscribe()` is additive — tickers added between starts will be
    picked up the next time `start()` is called (the connection is torn
    down and reopened with the new subscription).
    """

    def __init__(self):
        self._tickers: list[str] = []
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def subscribe(self, *tickers: str):
        for t in tickers:
            if t and t not in self._tickers:
                self._tickers.append(t)

    def start(self):
        if not self._tickers:
            logger.warning("OrderBookManager.start() called with no tickers")
            return
        if self._thread and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="orderbook-ws"
        )
        self._thread.start()
        logger.info(f"OrderBook WS thread started for {len(self._tickers)} ticker(s)")

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(_run_ws(self._tickers))

    def best_bid_ask(self, ticker: str):
        return best_bid_ask(ticker)

    def mid_price(self, ticker: str):
        return mid_price(ticker)

    def spread(self, ticker: str):
        return spread(ticker)

    def is_stale(self, ticker: str, max_age: float = 30.0) -> bool:
        return book_age_seconds(ticker) > max_age


order_book_manager = OrderBookManager()
