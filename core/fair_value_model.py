"""ML fair-value estimator for Kalshi YES contracts.

Trains a GradientBoostingClassifier on recently-settled Kalshi markets:
  features: [yes_price, volume, volume_24h, liquidity, open_interest]
  label:    1 if market settled YES, 0 if NO

The model learns systematic calibration patterns — e.g. whether certain
volume/liquidity profiles correlate with markets being mis-priced relative
to their outcomes.

For open markets, predict_proba returns P(YES settles), used as the
fair-value estimate in the directional strategy.

Retrains automatically when the persisted model is older than 24 hours or
missing.  Requires scikit-learn; gracefully returns None if unavailable.
"""
import os
import pickle
import time
from pathlib import Path
from typing import Optional

from utils.logger import logger

MODEL_FILE  = Path(os.getenv("FV_MODEL_FILE", "fair_value_model.pkl"))
MIN_SAMPLES = int(os.getenv("FV_MIN_SAMPLES", "50"))
MAX_SETTLED = int(os.getenv("FV_MAX_SETTLED", "500"))
RETRAIN_HOURS = float(os.getenv("FV_RETRAIN_HOURS", "24"))


def _features(summary: dict) -> Optional[list]:
    """Extract a feature vector from a market summary.

    Skips markets where yes_price is missing or at the 0/1 boundary
    (settled markets whose prices have been reset to the payout value).
    """
    yes_price = summary.get("yes_price")
    if yes_price is None or yes_price <= 0.005 or yes_price >= 0.995:
        return None
    return [
        yes_price,
        min(summary.get("volume",       0), 1_000_000),
        min(summary.get("volume_24h",   0), 500_000),
        min(summary.get("liquidity",    0), 10_000),
        summary.get("spread") if summary.get("spread") is not None else 0.10,
        min(summary.get("open_interest", 0), 1_000_000),
    ]


def _fetch_training_data() -> tuple[list, list]:
    """Fetch settled markets and return (X, y) arrays."""
    from core.client import kalshi_client
    from core.markets import get_market_summary

    X, y = [], []
    cursor = None
    fetched = 0

    while fetched < MAX_SETTLED:
        try:
            page = kalshi_client.get_markets(
                limit=min(200, MAX_SETTLED - fetched),
                status="settled",
                cursor=cursor,
            )
        except Exception as exc:
            logger.warning(f"fair_value_model: fetch failed: {exc}")
            break

        markets = page.get("markets") or []
        if not markets:
            break

        for m in markets:
            result = m.get("result")
            if result not in ("yes", "no"):
                continue
            summary = get_market_summary(m)
            feat = _features(summary)
            if feat is None:
                continue
            X.append(feat)
            y.append(1 if result == "yes" else 0)
            fetched += 1

        cursor = page.get("cursor") or None
        if not cursor:
            break

    return X, y


class FairValueModel:
    """Scikit-learn–based fair-value provider.

    Implements the FairValueProvider protocol expected by DirectionalTrader:
        def fair_value(self, ticker: str) -> Optional[float]
    """

    def __init__(self):
        self._model = None
        self._trained_at: float = 0.0
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not MODEL_FILE.exists():
            return
        try:
            with MODEL_FILE.open("rb") as f:
                data = pickle.load(f)
            self._model      = data["model"]
            self._trained_at = data.get("trained_at", 0.0)
            logger.info(f"fair_value_model: loaded (n={data.get('n_samples','?')})")
        except Exception as exc:
            logger.warning(f"fair_value_model: load failed: {exc}")

    def _save(self, n_samples: int) -> None:
        try:
            with MODEL_FILE.open("wb") as f:
                pickle.dump({
                    "model":       self._model,
                    "trained_at":  self._trained_at,
                    "n_samples":   n_samples,
                }, f)
        except Exception as exc:
            logger.warning(f"fair_value_model: save failed: {exc}")

    # ── Training ─────────────────────────────────────────────────────────────

    def needs_retrain(self) -> bool:
        age_hours = (time.time() - self._trained_at) / 3600
        return self._model is None or age_hours > RETRAIN_HOURS

    def train(self) -> bool:
        """Fetch settled markets and fit the model.  Returns True on success."""
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            import numpy as np
        except ImportError:
            logger.warning("fair_value_model: scikit-learn not installed — skipping")
            return False

        logger.info("fair_value_model: fetching settled markets for training…")
        X, y = _fetch_training_data()

        if len(X) < MIN_SAMPLES:
            logger.warning(
                f"fair_value_model: only {len(X)} usable samples "
                f"(need {MIN_SAMPLES}) — skipping train"
            )
            return False

        import numpy as np
        model = GradientBoostingClassifier(
            n_estimators=100, max_depth=3,
            learning_rate=0.1, subsample=0.8,
            random_state=42,
        )
        model.fit(np.array(X, dtype=float), np.array(y, dtype=int))

        self._model      = model
        self._trained_at = time.time()
        self._save(len(X))
        logger.info(f"fair_value_model: trained on {len(X)} settled markets")
        return True

    # ── Inference ─────────────────────────────────────────────────────────────

    def fair_value(self, ticker: str) -> Optional[float]:
        """Return predicted P(YES settles) for an open market, or None."""
        if self._model is None:
            return None
        try:
            from core.markets import get_market, get_market_summary
            m = get_market(ticker)
            if not m:
                return None
            feat = _features(get_market_summary(m))
            if feat is None:
                return None
            proba = self._model.predict_proba([feat])[0]
            return float(proba[1])   # P(yes)
        except Exception as exc:
            logger.warning(f"fair_value_model.fair_value({ticker}): {exc}")
            return None


fair_value_model = FairValueModel()
