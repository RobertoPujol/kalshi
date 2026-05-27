"""Kalshi REST client (trade-api v2).

Single wrapper for both public market data and authenticated portfolio
endpoints.  Every request is signed (cheap, and a few "public" endpoints
quietly require auth too — signing them all just works).

Required env vars:
  KALSHI_API_KEY_ID         - API Key ID (UUID) from the Kalshi dashboard
  KALSHI_PRIVATE_KEY_PATH   - path to PEM-encoded RSA private key
  KALSHI_ENV                - "prod" (default) or "demo"
  DRY_RUN                   - if "true", orders are logged but not submitted
"""
import os
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import requests

from core.auth import get_auth
from utils.logger import logger

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("true", "1", "yes")

API_BASE = "/trade-api/v2"

_HOSTS = {
    "prod": "https://api.elections.kalshi.com",
    "demo": "https://demo-api.kalshi.co",
}


def _host() -> str:
    env = os.getenv("KALSHI_ENV", "prod").strip().lower()
    return _HOSTS.get(env, _HOSTS["prod"])


class KalshiClient:
    """Thin requests-based Kalshi client with signing + dry-run support."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Accept":       "application/json",
            "Content-Type": "application/json",
            "User-Agent":   "kalshitrader/1.0",
        })

    # ── Low-level request ────────────────────────────────────────────────────

    def _request(self, method: str, endpoint: str,
                 params: dict = None, body: dict = None,
                 retries: int = 3) -> dict | list:
        """
        Send a signed request to /trade-api/v2{endpoint}.

        endpoint:  starts with "/", e.g. "/portfolio/balance"
        params:    query string params (NOT included in the signed path)
        body:      JSON body for POST/PUT
        """
        path  = API_BASE + endpoint
        url   = _host() + path
        query = "?" + urlencode(params) if params else ""

        for attempt in range(retries):
            headers = get_auth().sign(method, path)
            try:
                r = self._session.request(
                    method, url + query,
                    headers=headers,
                    json=body if body is not None else None,
                    timeout=15,
                )
                if r.status_code == 429:
                    # rate-limited — backoff
                    wait = 1.5 ** attempt
                    logger.warning(f"Kalshi 429 on {method} {endpoint}; sleep {wait:.1f}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                if r.status_code == 204 or not r.content:
                    return {}
                return r.json()
            except requests.exceptions.HTTPError as e:
                # Bubble HTTP errors with response body for easier debugging
                body_excerpt = (r.text or "")[:300]
                logger.error(
                    f"Kalshi {method} {endpoint} failed: {e} | body={body_excerpt}"
                )
                if r.status_code in (400, 401, 403, 404):
                    raise   # don't retry client errors
                if attempt == retries - 1:
                    raise
                time.sleep(1.5 ** attempt)
            except Exception as e:
                if attempt == retries - 1:
                    logger.error(f"Kalshi {method} {endpoint} error: {e}")
                    raise
                time.sleep(1.5 ** attempt)

    # ── Public-ish market data ──────────────────────────────────────────────

    def get_markets(self, limit: int = 200,
                    status: str = "open",
                    cursor: Optional[str] = None,
                    event_ticker: Optional[str] = None,
                    series_ticker: Optional[str] = None) -> dict:
        """Return {'markets': [...], 'cursor': '...'}."""
        params = {"limit": limit, "status": status}
        if cursor:        params["cursor"]        = cursor
        if event_ticker:  params["event_ticker"]  = event_ticker
        if series_ticker: params["series_ticker"] = series_ticker
        return self._request("GET", "/markets", params=params) or {}

    def get_market(self, ticker: str) -> dict:
        data = self._request("GET", f"/markets/{ticker}") or {}
        return data.get("market") or data

    def get_events(self, limit: int = 100, status: str = "open",
                   cursor: Optional[str] = None) -> dict:
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/events", params=params) or {}

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request(
            "GET", f"/markets/{ticker}/orderbook",
            params={"depth": depth},
        ) or {}

    # ── Portfolio (authenticated) ────────────────────────────────────────────

    def get_balance(self) -> float:
        """Available USD balance in dollars (Kalshi returns cents)."""
        try:
            data = self._request("GET", "/portfolio/balance") or {}
            return float(data.get("balance") or 0) / 100.0
        except Exception as e:
            logger.warning(f"get_balance failed: {e}")
            return 0.0

    def get_positions(self, ticker: Optional[str] = None) -> list[dict]:
        """List open market positions for the authenticated user."""
        try:
            params = {"limit": 200}
            if ticker:
                params["ticker"] = ticker
            data = self._request("GET", "/portfolio/positions", params=params) or {}
            return data.get("market_positions") or []
        except Exception as e:
            logger.warning(f"get_positions failed: {e}")
            return []

    def get_open_orders(self, ticker: Optional[str] = None) -> list[dict]:
        try:
            params = {"limit": 200, "status": "resting"}
            if ticker:
                params["ticker"] = ticker
            data = self._request("GET", "/portfolio/orders", params=params) or {}
            return data.get("orders") or []
        except Exception as e:
            logger.warning(f"get_open_orders failed: {e}")
            return []

    def get_fills(self, ticker: Optional[str] = None, limit: int = 100) -> list[dict]:
        try:
            params = {"limit": limit}
            if ticker:
                params["ticker"] = ticker
            data = self._request("GET", "/portfolio/fills", params=params) or {}
            return data.get("fills") or []
        except Exception as e:
            logger.warning(f"get_fills failed: {e}")
            return []

    # ── Order placement ──────────────────────────────────────────────────────

    def create_order(self, ticker: str, action: str, side: str,
                     count: int, price: float,
                     order_type: str = "limit",
                     client_order_id: Optional[str] = None) -> Optional[dict]:
        """
        Place an order on a Kalshi market.

        Args:
            ticker:   market ticker (e.g. "PRES-2024-DJT")
            action:   "buy" | "sell"
            side:     "yes" | "no"   (which side of the binary market)
            count:    integer number of contracts (each worth $1)
            price:    price per contract in dollars, 0.01–0.99
            order_type: "limit" (default) or "market"
            client_order_id: optional idempotency UUID (auto-generated if absent)
        """
        action = action.lower()
        side   = side.lower()
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be buy|sell, got {action!r}")
        if side not in ("yes", "no"):
            raise ValueError(f"side must be yes|no, got {side!r}")
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        if not 0.01 <= price <= 0.99:
            raise ValueError(f"price must be 0.01–0.99, got {price}")

        body = {
            "ticker":     ticker,
            "action":     action,
            "side":       side,
            "count":      int(count),
            "type":       order_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        # Kalshi expects the price field as a STRING decimal — the Go server
        # rejects a JSON number with: "cannot unmarshal number into Go struct
        # field CreateOrderRequest.yes_price_dollars of type string".
        # 4-decimal form covers both 1¢ and 0.1¢ (deci_cent) tick markets.
        price_str = f"{round(price, 4):.4f}"
        if side == "yes":
            body["yes_price_dollars"] = price_str
        else:
            body["no_price_dollars"]  = price_str

        log_msg = (f"ORDER {action.upper()} {side.upper()} {count}c "
                   f"@ ${price:.2f} ticker={ticker}")
        if DRY_RUN:
            logger.info(f"[DRY RUN] {log_msg}")
            return {"dry_run": True, **body}

        try:
            data = self._request("POST", "/portfolio/orders", body=body) or {}
            order = data.get("order") or data
            order_id = order.get("order_id", "?")
            logger.info(f"{log_msg} → id={order_id}")
            return order
        except Exception as e:
            logger.error(f"create_order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if DRY_RUN:
            logger.info(f"[DRY RUN] CANCEL order_id={order_id}")
            return True
        try:
            self._request("DELETE", f"/portfolio/orders/{order_id}")
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.warning(f"cancel_order {order_id} failed: {e}")
            return False

    def cancel_all_for_ticker(self, ticker: str) -> bool:
        """Cancel every resting order for one market ticker."""
        if DRY_RUN:
            logger.info(f"[DRY RUN] CANCEL ALL ticker={ticker}")
            return True
        ok = True
        for o in self.get_open_orders(ticker=ticker):
            if not self.cancel_order(o.get("order_id", "")):
                ok = False
        return ok


# Module-level singleton (matches old `poly_client` name pattern, renamed)
kalshi_client = KalshiClient()
