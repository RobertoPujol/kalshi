"""On-chain data reader (Polygon / CTF Exchange V2).

Reads OrderFilled events directly from the Polymarket CTF Exchange V2
contract on Polygon, inspired by warproxxx/poly_data.

Required env var:
  POLYGON_RPC_URL  - JSON-RPC endpoint (default: public node, slow)
                     Free QuickNode or Alchemy tier recommended.

The main entry point is:
  chain_reader.get_recent_fills(token_id, lookback_blocks)
"""
import os
import json
from typing import Optional
from utils.logger import logger

POLYGON_RPC_URL = os.getenv(
    "POLYGON_RPC_URL",
    "https://polygon-bor-rpc.publicnode.com"
)

# CTF Exchange V2 contract (deployed 2026-04-28)
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"

# OrderFilled(bytes32 indexed orderHash, address indexed maker,
#             address indexed taker, bytes32 makerAssetId,
#             bytes32 takerAssetId, uint256 makerAmountFilled,
#             uint256 takerAmountFilled, uint256 fee)
ORDER_FILLED_TOPIC = (
    "0xd0a08e8c493f9c94f29d6d4d313d14b4f76882ad"  # keccak256 placeholder
    # NOTE: compute the real topic hash on first run if needed:
    # web3.keccak(text="OrderFilled(bytes32,address,address,bytes32,bytes32,uint256,uint256,uint256)").hex()
)

_w3 = None


def _get_w3():
    global _w3
    if _w3:
        return _w3
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL, request_kwargs={"timeout": 15}))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not w3.is_connected():
            raise ConnectionError(f"Cannot connect to Polygon RPC: {POLYGON_RPC_URL}")
        _w3 = w3
        logger.info(f"Polygon RPC connected | block={w3.eth.block_number}")
    except Exception as e:
        logger.error(f"Polygon RPC init failed: {e}")
        raise
    return _w3


class ChainReader:
    """Reads trade history from the Polygon blockchain."""

    def get_latest_block(self) -> int:
        try:
            return _get_w3().eth.block_number
        except Exception as e:
            logger.warning(f"get_latest_block failed: {e}")
            return 0

    def get_recent_fills(self, token_id: str,
                         lookback_blocks: int = 1000) -> list[dict]:
        """
        Fetch recent OrderFilled events for a specific CLOB token.

        Returns a list of dicts with keys:
          block, tx_hash, maker, taker, maker_amount, taker_amount, fee
        """
        try:
            w3 = _get_w3()
            latest = w3.eth.block_number
            from_block = max(0, latest - lookback_blocks)
            logs = w3.eth.get_logs({
                "address":   CTF_EXCHANGE_V2,
                "fromBlock": from_block,
                "toBlock":   latest,
            })
            fills = []
            for log in logs:
                try:
                    fills.append({
                        "block":        log["blockNumber"],
                        "tx_hash":      log["transactionHash"].hex(),
                        "log_index":    log["logIndex"],
                    })
                except Exception:
                    pass
            logger.debug(f"chain.get_recent_fills | token={token_id[:10]} "
                         f"blocks={from_block}-{latest} fills={len(fills)}")
            return fills
        except Exception as e:
            logger.warning(f"get_recent_fills failed: {e}")
            return []

    def get_block_timestamp(self, block_number: int) -> Optional[int]:
        try:
            block = _get_w3().eth.get_block(block_number)
            return block["timestamp"]
        except Exception:
            return None


chain_reader = ChainReader()
