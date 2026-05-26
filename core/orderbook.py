"""Real-time order book via Polymarket WebSocket feed.

Subscribes to the market WebSocket for one or more token IDs and
maintains an in-memory best-bid/best-ask snapshot that strategies
can query without hitting the REST API on every cycle.

Usage:
    from core.orderbook import order_book_manager
    order_book_manager.subscribe("0xabc...")
    bid, ask = order_book_manager.best_bid_ask("0xabc...")
"""
import asyncio
import json
import threading
import time
from typing import Optional
from utils.logger import logger

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Global state: token_id → {"bids": {price: size}, "asks": {price: size}, "ts": epoch}
_books: dict[str, dict] = {}
_lock = threading.Lock()


def _update_book(token_id: str, side: str, price: float, size: float):
    with _lock:
        if token_id not in _books:
            _books[token_id] = {"bids": {}, "asks": {}, "ts": time.time()}
        book = _books[token_id]
        bucket = "bids" if side.lower() == "buy" else "asks"
        if size == 0:
            book[bucket].pop(price, None)
        else:
            book[bucket][price] = size
        book["ts"] = time.time()


def best_bid_ask(token_id: str) -> tuple[Optional[float], Optional[float]]:
    """Return (best_bid, best_ask) or (None, None) if no data."""
    with _lock:
        book = _books.get(token_id)
        if not book:
            return None, None
        bids = book["bids"]
        asks = book["asks"]
        best_bid = max(bids.keys()) if bids else None
        best_ask = min(asks.keys()) if asks else None
    return best_bid, best_ask


def mid_price(token_id: str) -> Optional[float]:
    bid, ask = best_bid_ask(token_id)
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return None


def spread(token_id: str) -> Optional[float]:
    bid, ask = best_bid_ask(token_id)
    if bid is not None and ask is not None:
        return round(ask - bid, 4)
    return None


def book_age_seconds(token_id: str) -> float:
    """How many seconds since the last order book update."""
    with _lock:
        book = _books.get(token_id)
        if not book:
            return float("inf")
        return time.time() - book["ts"]


async def _run_ws(token_ids: list[str]):
    import websockets
    sub_msg = json.dumps({
        "auth": {},
        "markets": token_ids,
        "type": "market"
    })
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20,
                                          ping_timeout=10) as ws:
                await ws.send(sub_msg)
                logger.info(f"OrderBook WS connected for {len(token_ids)} token(s)")
                async for raw in ws:
                    try:
                        msgs = json.loads(raw)
                        if not isinstance(msgs, list):
                            msgs = [msgs]
                        for msg in msgs:
                            t = msg.get("asset_id") or msg.get("token_id")
                            event = msg.get("event_type", "")
                            if t and event in ("book", "price_change"):
                                for entry in msg.get("bids", []):
                                    _update_book(t, "buy",
                                                 float(entry["price"]),
                                                 float(entry["size"]))
                                for entry in msg.get("asks", []):
                                    _update_book(t, "sell",
                                                 float(entry["price"]),
                                                 float(entry["size"]))
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"OrderBook WS error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


class OrderBookManager:
    """Runs the WebSocket listener in a background thread."""

    def __init__(self):
        self._token_ids: list[str] = []
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def subscribe(self, *token_ids: str):
        for tid in token_ids:
            if tid not in self._token_ids:
                self._token_ids.append(tid)

    def start(self):
        if not self._token_ids:
            logger.warning("OrderBookManager.start() called with no subscribed tokens")
            return
        if self._thread and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="orderbook-ws"
        )
        self._thread.start()
        logger.info(f"OrderBook WS thread started for {len(self._token_ids)} token(s)")

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(_run_ws(self._token_ids))

    def best_bid_ask(self, token_id: str):
        return best_bid_ask(token_id)

    def mid_price(self, token_id: str):
        return mid_price(token_id)

    def spread(self, token_id: str):
        return spread(token_id)

    def is_stale(self, token_id: str, max_age: float = 30.0) -> bool:
        return book_age_seconds(token_id) > max_age


order_book_manager = OrderBookManager()
