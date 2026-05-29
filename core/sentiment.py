"""News sentiment fetcher and scorer for Kalshi market topics.

Fetches recent headlines via NewsAPI (requires NEWS_API_KEY) or falls back
to GDELT (free, no key needed).  Scores headlines with VADER.

Env knobs:
  NEWS_API_KEY           NewsAPI.org key — enables higher-quality results;
                         without it, falls back to GDELT (free, no signup)
  SENTIMENT_CACHE_TTL    Seconds to cache a topic's score (default 900 = 15 min)
  SENTIMENT_MAX_ARTICLES Max headlines to score per topic (default 20)
"""
import os
import time
from typing import Optional

import requests

from utils.logger import logger

CACHE_TTL    = int(os.getenv("SENTIMENT_CACHE_TTL",     "900"))
MAX_ARTICLES = int(os.getenv("SENTIMENT_MAX_ARTICLES",  "20"))
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# topic → (score, expires_at)
_cache: dict[str, tuple[float, float]] = {}


def _vader_score(texts: list[str]) -> float:
    """Return mean VADER compound score over all texts. Range: -1.0 to +1.0."""
    try:
        import nltk
        from nltk.sentiment.vader import SentimentIntensityAnalyzer
        try:
            sia = SentimentIntensityAnalyzer()
        except LookupError:
            nltk.download("vader_lexicon", quiet=True)
            sia = SentimentIntensityAnalyzer()
        scores = [sia.polarity_scores(t)["compound"] for t in texts if t.strip()]
        return sum(scores) / len(scores) if scores else 0.0
    except ImportError:
        logger.warning("sentiment: nltk not installed — pip install nltk")
        return 0.0


def _fetch_newsapi(query: str) -> list[str]:
    """Fetch headlines+descriptions from NewsAPI.org."""
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":        query,
                "sortBy":   "publishedAt",
                "pageSize": MAX_ARTICLES,
                "language": "en",
                "apiKey":   NEWS_API_KEY,
            },
            timeout=6,
        )
        r.raise_for_status()
        texts = []
        for a in r.json().get("articles", []):
            title = a.get("title") or ""
            desc  = a.get("description") or ""
            if title:
                texts.append(f"{title}. {desc}".strip())
        return texts
    except Exception as exc:
        logger.warning(f"sentiment: NewsAPI error for '{query}': {exc}")
        return []


def _fetch_gdelt(query: str) -> list[str]:
    """Fetch article titles from GDELT (free, no key, last 24 h)."""
    try:
        r = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query":      query,
                "mode":       "artlist",
                "maxrecords": MAX_ARTICLES,
                "format":     "json",
                "timespan":   "24H",
            },
            timeout=8,
        )
        r.raise_for_status()
        return [a.get("title", "") for a in r.json().get("articles", [])
                if a.get("title")]
    except Exception as exc:
        logger.warning(f"sentiment: GDELT error for '{query}': {exc}")
        return []


def score(topic: str) -> Optional[float]:
    """Return a sentiment score in [-1, +1] for the topic, or None on failure.

    Cached for SENTIMENT_CACHE_TTL seconds.  Uses NewsAPI when NEWS_API_KEY is
    set, GDELT otherwise.
    """
    now = time.time()
    if topic in _cache:
        cached_score, expires = _cache[topic]
        if now < expires:
            return cached_score

    texts = _fetch_newsapi(topic) if NEWS_API_KEY else _fetch_gdelt(topic)
    if not texts:
        return None

    s = _vader_score(texts)
    _cache[topic] = (s, now + CACHE_TTL)
    logger.debug(f"sentiment: '{topic}' score={s:+.3f} n={len(texts)}")
    return s
