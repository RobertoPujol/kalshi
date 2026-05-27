"""Kalshi API key request signing.

Every authenticated Kalshi v2 request needs three headers:
  KALSHI-ACCESS-KEY        - your API Key ID (UUID) from the dashboard
  KALSHI-ACCESS-TIMESTAMP  - Unix time in **milliseconds**, as a string
  KALSHI-ACCESS-SIGNATURE  - RSA-PSS-SHA256 signature, base64-encoded

The signed payload is the concatenation (no separator) of:
    timestamp_ms + METHOD + path_without_query

Example: "1727712345000GET/trade-api/v2/portfolio/balance"

Sign with the private key matching the API Key ID, using PSS padding
with MGF1(SHA-256) and salt length = digest length.

Required env vars:
  KALSHI_API_KEY_ID         - UUID string
  KALSHI_PRIVATE_KEY_PATH   - filesystem path to a PEM-encoded RSA private key
"""
import os
import time
import base64
from functools import lru_cache
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from utils.logger import logger


@lru_cache(maxsize=1)
def _load_private_key() -> rsa.RSAPrivateKey:
    path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not path:
        raise EnvironmentError("KALSHI_PRIVATE_KEY_PATH must be set in .env")
    path = os.path.expanduser(path)
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(f"Private key at {path} is not an RSA key")
    return key


class KalshiAuth:
    """Computes auth headers for Kalshi v2 requests."""

    def __init__(self, key_id: Optional[str] = None):
        self.key_id = key_id or os.getenv("KALSHI_API_KEY_ID", "")
        if not self.key_id:
            raise EnvironmentError("KALSHI_API_KEY_ID must be set in .env")

    def sign(self, method: str, path: str,
             timestamp_ms: Optional[int] = None) -> dict[str, str]:
        """
        Build the three headers for a single request.

        Args:
            method:  HTTP method in upper-case ("GET", "POST", "DELETE", ...)
            path:    Request path INCLUDING /trade-api/v2 prefix, WITHOUT query
                     string.  e.g. "/trade-api/v2/portfolio/balance"
            timestamp_ms: override for testing — defaults to now()
        """
        ts = str(int(timestamp_ms if timestamp_ms is not None
                     else time.time() * 1000))
        payload = (ts + method.upper() + path).encode()

        key = _load_private_key()
        sig = key.sign(
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }


# Lazy module singleton — built on first use so importing this module
# without env vars (e.g. for unit tests) doesn't blow up.
_auth: Optional[KalshiAuth] = None


def get_auth() -> KalshiAuth:
    global _auth
    if _auth is None:
        _auth = KalshiAuth()
        logger.debug(f"Kalshi auth initialised | key_id={_auth.key_id[:8]}...")
    return _auth
