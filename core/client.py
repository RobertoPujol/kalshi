"""Polymarket CLOB client wrapper.

Wraps py_clob_client to provide a cleaner interface and centralised
error handling.  All order placement goes through here so we have
one place to add logging, dry-run mode, and rate-limiting.

Required env vars:
  PK                 - Ethereum private key (hex, with or without 0x prefix)
  BROWSER_ADDRESS    - MetaMask / browser-wallet address that holds USDC
  POLY_HOST          - optional, defaults to https://clob.polymarket.com
  DRY_RUN            - if "true", orders are logged but not submitted
"""
import os
from typing import Optional
from utils.logger import logger

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("true", "1", "yes")

# Lazy-init: defer importing py_clob_client until first use so the rest of
# the code can be imported in environments without it (e.g. for tests).
_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        from web3 import Web3
        from dotenv import load_dotenv
        load_dotenv()
        pk = os.getenv("PK", "")
        browser = os.getenv("BROWSER_ADDRESS", "")
        host = os.getenv("POLY_HOST", "https://clob.polymarket.com")
        if not pk or not browser:
            raise EnvironmentError("PK and BROWSER_ADDRESS must be set in .env")
        clob = ClobClient(
            host=host,
            key=pk,
            chain_id=POLYGON,
            funder=Web3.to_checksum_address(browser),
            signature_type=2,
        )
        clob.set_api_creds(clob.create_or_derive_api_creds())
        _client = clob
        logger.info("Polymarket CLOB client initialised")
    except Exception as e:
        logger.error(f"CLOB client init failed: {e}")
        raise
    return _client


class PolyClient:
    """Thin wrapper around ClobClient with logging and dry-run support."""

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_order_book(self, token_id: str) -> dict:
        """Return the live order book for a token."""
        return _get_client().get_order_book(token_id)

    def get_positions(self) -> list[dict]:
        """Return all open positions for the authenticated wallet."""
        try:
            return _get_client().get_positions() or []
        except Exception as e:
            logger.warning(f"get_positions failed: {e}")
            return []

    def get_open_orders(self, market: str = None) -> list[dict]:
        """Return open orders, optionally filtered by market condition_id."""
        try:
            from py_clob_client.clob_types import OpenOrderParams
            params = OpenOrderParams(market=market) if market else OpenOrderParams()
            return _get_client().get_orders(params) or []
        except Exception as e:
            logger.warning(f"get_open_orders failed: {e}")
            return []

    def get_balance(self) -> float:
        """Return available USDC balance (collateral)."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            data = _get_client().get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return float(data.get("balance") or 0)
        except Exception as e:
            logger.warning(f"get_balance failed: {e}")
            return 0.0

    # ── Write ─────────────────────────────────────────────────────────────────

    def create_order(self, token_id: str, side: str, price: float,
                     size: float, neg_risk: bool = False) -> Optional[dict]:
        """
        Place a limit order.

        Args:
            token_id: CLOB token ID (from market clobTokenIds)
            side:     "BUY" or "SELL"
            price:    0.01 – 0.99 (probability in cents)
            size:     USDC notional
            neg_risk: True for neg-risk markets (multi-outcome)
        """
        log_msg = f"ORDER {side} {size:.2f} USDC @ {price:.4f} token={token_id[:8]}..."
        if DRY_RUN:
            logger.info(f"[DRY RUN] {log_msg}")
            return {"dry_run": True, "token_id": token_id, "side": side,
                    "price": price, "size": size}
        try:
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
            args = OrderArgs(token_id=token_id, price=price, size=size,
                             side=side.upper())
            opts = PartialCreateOrderOptions(neg_risk=neg_risk)
            result = _get_client().create_and_post_order(args, options=opts)
            logger.info(f"{log_msg} → id={result.get('orderID', '?')}")
            return result
        except Exception as e:
            logger.error(f"create_order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if DRY_RUN:
            logger.info(f"[DRY RUN] CANCEL order_id={order_id}")
            return True
        try:
            _get_client().cancel(order_id)
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.warning(f"cancel_order {order_id} failed: {e}")
            return False

    def cancel_all_for_token(self, token_id: str) -> bool:
        if DRY_RUN:
            logger.info(f"[DRY RUN] CANCEL ALL token={token_id[:8]}...")
            return True
        try:
            _get_client().cancel_all_asset(token_id)
            logger.info(f"Cancelled all orders for token {token_id[:8]}...")
            return True
        except Exception as e:
            logger.warning(f"cancel_all_for_token failed: {e}")
            return False


# Module-level singleton
poly_client = PolyClient()
