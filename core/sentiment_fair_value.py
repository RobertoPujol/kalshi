"""Sentiment-augmented fair-value provider for Kalshi markets.

Wraps any base FairValueProvider (e.g. the ML model) and nudges its output
using a news-sentiment signal derived from the market's title.

Formula:
    fair_value = base + sentiment * SENTIMENT_WEIGHT
    clamped to [0.05, 0.95]

If the base provider returns None, falls back to the live order-book mid.
If sentiment is unavailable, returns the base unchanged.

Env knobs:
  SENTIMENT_WEIGHT   $ shift per unit of sentiment (default 0.08)
                     e.g. score=+0.5 → shift= +4¢; score=-1.0 → shift= -8¢
"""
import os
import re
from typing import Optional

from core.sentiment import score as sentiment_score
from utils.logger import logger

SENTIMENT_WEIGHT = float(os.getenv("SENTIMENT_WEIGHT", "0.08"))

_STOP_WORDS = {
    "will", "the", "a", "an", "be", "is", "are", "was", "were",
    "in", "on", "at", "to", "of", "for", "and", "or", "by",
    "this", "that", "it", "its", "how", "what", "when", "where",
    "which", "who", "than", "more", "less", "above", "below",
    "first", "last", "new", "old", "per", "rate", "level", "hit",
    "end", "close", "reach", "stay",
}


def _extract_topic(title: str) -> str:
    """Return a 3–5 keyword search query from a Kalshi market title."""
    words = re.sub(r"[^a-zA-Z0-9 ]", " ", title).split()
    keywords = [w for w in words
                if w.lower() not in _STOP_WORDS and len(w) > 2]
    return " ".join(keywords[:5])


class SentimentFairValueProvider:
    """Implements the FairValueProvider protocol with a sentiment overlay.

    Plug-in usage (in main.py):
        from core.sentiment_fair_value import SentimentFairValueProvider
        from core.fair_value_model import fair_value_model
        fv = SentimentFairValueProvider(base_provider=fair_value_model)
        DirectionalTrader(ticker=t, fair_value_provider=fv)
    """

    def __init__(self, base_provider=None):
        self._base = base_provider
        self._title_cache: dict[str, str] = {}

    def _get_title(self, ticker: str) -> str:
        if ticker not in self._title_cache:
            try:
                from core.markets import get_market
                m = get_market(ticker) or {}
                title = m.get("title") or m.get("subtitle") or ""
            except Exception:
                title = ""
            self._title_cache[ticker] = title
        return self._title_cache[ticker]

    def fair_value(self, ticker: str) -> Optional[float]:
        # Base estimate: ML model → live mid → None
        base: Optional[float] = None
        if self._base is not None:
            base = self._base.fair_value(ticker)
        if base is None:
            try:
                from core.orderbook import order_book_manager
                base = order_book_manager.mid_price(ticker)
            except Exception:
                pass
        if base is None:
            return None

        # Sentiment adjustment
        title = self._get_title(ticker)
        topic = _extract_topic(title) if title else ""
        if not topic:
            return base

        sentiment = sentiment_score(topic)
        if sentiment is None:
            return base

        adjusted = base + sentiment * SENTIMENT_WEIGHT
        clamped  = max(0.05, min(0.95, adjusted))
        logger.debug(
            f"SentimentFV {ticker}: base={base:.3f} "
            f"topic='{topic}' sentiment={sentiment:+.3f} → fv={clamped:.3f}"
        )
        return clamped
