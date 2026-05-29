"""Adaptive parameter tuning based on closed trade history.

Reads closed trades from SQLite, computes win rates per strategy and
market series, then adjusts SCANNER_MIN_SCORE, DIR_MIN_DEVIATION, and
a series blacklist accordingly.

Results are persisted to adaptive_params.json so the bot can load them
on startup without recalculating. Re-tuning happens every time
dashboard_sync.sync_trades() runs (every 5 min while bot is live).

Tuning rules (only activate once MIN_TRADES_TO_ADAPT trades are closed):
  SCANNER_MIN_SCORE  +10 if overall win-rate < 45% (be more selective)
                      -5 if overall win-rate > 65% (allow more opportunities)
  DIR_MIN_DEVIATION  +0.01 if directional win-rate < 45% (need stronger signal)
                     -0.01 if directional win-rate > 65% (can enter on weaker edge)
  blacklisted_series: any series with ≥ MIN_SERIES_TRADES and win-rate < 40%
"""
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from utils.logger import logger

PARAMS_FILE         = Path(os.getenv("ADAPTIVE_PARAMS_FILE", "adaptive_params.json"))
BASE_MIN_SCORE      = float(os.getenv("SCANNER_MIN_SCORE", "30"))
BASE_MIN_DEVIATION  = float(os.getenv("DIR_MIN_DEVIATION",  "0.05"))
BASE_TAKE_PROFIT    = float(os.getenv("DIR_TAKE_PROFIT",    "0.04"))
BASE_STOP_LOSS      = float(os.getenv("DIR_STOP_LOSS",      "0.03"))

MIN_TRADES_TO_ADAPT = 10
MIN_SERIES_TRADES   = 3

_params: dict = {}


def _load() -> dict:
    if PARAMS_FILE.exists():
        try:
            return json.loads(PARAMS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(p: dict) -> None:
    try:
        PARAMS_FILE.write_text(json.dumps(p, indent=2))
    except Exception as exc:
        logger.warning(f"adaptive_params: could not save: {exc}")


def _series_key(ticker: str, event_ticker: str | None) -> str:
    src = event_ticker or ticker
    m = re.match(r"^([A-Z]+)", src)
    return m.group(1) if m else src.split("-")[0]


def update_from_trades(closed_trades: list) -> None:
    """Recompute and persist adaptive params from closed TradeDB objects."""
    global _params

    if len(closed_trades) < MIN_TRADES_TO_ADAPT:
        logger.debug(
            f"adaptive_params: {len(closed_trades)} trades < {MIN_TRADES_TO_ADAPT} threshold, skipping tune"
        )
        _params = _load()
        return

    # ── Overall win rate ──────────────────────────────────────────────────────
    total    = len(closed_trades)
    wins     = sum(1 for t in closed_trades if (t.pnl or 0) > 0)
    overall_wr = wins / total

    # ── Per-series stats ──────────────────────────────────────────────────────
    series_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
    for t in closed_trades:
        s = _series_key(t.ticker, t.event_ticker)
        series_stats[s]["count"] += 1
        if (t.pnl or 0) > 0:
            series_stats[s]["wins"] += 1
        series_stats[s]["pnl"] += t.pnl or 0

    blacklisted = sorted(
        s for s, st in series_stats.items()
        if st["count"] >= MIN_SERIES_TRADES and st["wins"] / st["count"] < 0.40
    )

    # ── Per-strategy stats ────────────────────────────────────────────────────
    dir_trades = [t for t in closed_trades if (t.strategy or "") == "directional"]
    dir_wr: float | None = None
    if dir_trades:
        dir_wr = sum(1 for t in dir_trades if (t.pnl or 0) > 0) / len(dir_trades)

    # ── Tune SCANNER_MIN_SCORE ────────────────────────────────────────────────
    new_min_score = BASE_MIN_SCORE
    if overall_wr < 0.45:
        new_min_score = min(50.0, BASE_MIN_SCORE + 10)
    elif overall_wr > 0.65:
        new_min_score = max(15.0, BASE_MIN_SCORE - 5)

    # ── Tune DIR_MIN_DEVIATION ────────────────────────────────────────────────
    new_min_dev = BASE_MIN_DEVIATION
    if dir_wr is not None and len(dir_trades) >= 5:
        if dir_wr < 0.45:
            new_min_dev = min(0.10, BASE_MIN_DEVIATION + 0.01)
        elif dir_wr > 0.65:
            new_min_dev = max(0.03, BASE_MIN_DEVIATION - 0.01)

    # ── Tune DIR_TAKE_PROFIT and DIR_STOP_LOSS ────────────────────────────────
    new_take_profit = BASE_TAKE_PROFIT
    new_stop_loss   = BASE_STOP_LOSS
    if dir_wr is not None and len(dir_trades) >= 5:
        if dir_wr > 0.65:
            # Strong signal — let winners run, give positions a bit more room
            new_take_profit = min(0.08, BASE_TAKE_PROFIT + 0.01)
            new_stop_loss   = min(0.05, BASE_STOP_LOSS   + 0.005)
        elif dir_wr < 0.45:
            # Weak signal — take smaller profits, cut losses faster
            new_take_profit = max(0.02, BASE_TAKE_PROFIT - 0.01)
            new_stop_loss   = max(0.015, BASE_STOP_LOSS  - 0.005)

    p = {
        "scanner_min_score":  new_min_score,
        "dir_min_deviation":  new_min_dev,
        "dir_take_profit":    new_take_profit,
        "dir_stop_loss":      new_stop_loss,
        "blacklisted_series": blacklisted,
        "based_on_trades":    total,
        "overall_win_rate":   round(overall_wr, 4),
        "dir_win_rate":       round(dir_wr, 4) if dir_wr is not None else None,
    }
    _save(p)
    _params = p
    logger.info(
        f"adaptive_params: min_score={new_min_score} min_dev={new_min_dev:.3f} "
        f"tp={new_take_profit:.3f} sl={new_stop_loss:.3f} "
        f"blacklisted={blacklisted or 'none'} "
        f"(overall_wr={overall_wr:.1%} n={total})"
    )


# ── Accessors (used by scanner and directional strategy) ──────────────────────

def scanner_min_score() -> float:
    return float(_params.get("scanner_min_score", BASE_MIN_SCORE))


def dir_min_deviation() -> float:
    return float(_params.get("dir_min_deviation", BASE_MIN_DEVIATION))


def dir_take_profit() -> float:
    return float(_params.get("dir_take_profit", BASE_TAKE_PROFIT))


def dir_stop_loss() -> float:
    return float(_params.get("dir_stop_loss", BASE_STOP_LOSS))


def blacklisted_series() -> set[str]:
    return set(_params.get("blacklisted_series", []))


# Load persisted params at import time so they're available before the first sync.
_params = _load()
