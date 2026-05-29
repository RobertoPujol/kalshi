"""Daily strategy discovery and parameter optimization.

Evaluates each known strategy on closed trade history:
  - win_rate   (fraction of trades where pnl > 0)
  - avg_pnl    (mean P&L per trade)
  - score      = win_rate × mean_positive_pnl  (reward signal)

Runs a parameter grid for each strategy type.  Since trades don't log
which exact params were live, we use a heuristic: the win-rate and average
P&L of the strategy's trade history to score candidate configs, then pick
the one most aligned with observed performance.

Disables a strategy only when it has enough history AND a clearly poor
win rate (< 30%).  Unknown / new strategies start enabled.

Writes results to strategy_config.json.  main.py reads this at startup
and after each discovery run to decide which strategies to instantiate.
"""
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from utils.logger import logger

CONFIG_FILE     = Path(os.getenv("STRATEGY_CONFIG_FILE", "strategy_config.json"))
MIN_EVAL_TRADES = int(os.getenv("DISCOVERY_MIN_TRADES", "5"))
DISABLE_BELOW   = float(os.getenv("DISCOVERY_DISABLE_WR", "0.30"))

_ALL_STRATEGIES = ["market_maker", "directional", "mean_reversion"]

# Parameter candidates for each strategy type.
# When a strategy has enough history, we select the variant that best
# matches its observed performance profile.
_PARAM_GRIDS: dict[str, list[dict]] = {
    "market_maker": [
        {"half_spread": 0.01, "min_price": 0.05, "max_price": 0.95},
        {"half_spread": 0.02, "min_price": 0.05, "max_price": 0.95},
        {"half_spread": 0.03, "min_price": 0.08, "max_price": 0.92},
    ],
    "directional": [
        {"min_deviation": 0.04, "take_profit": 0.03, "stop_loss": 0.02},
        {"min_deviation": 0.05, "take_profit": 0.04, "stop_loss": 0.03},
        {"min_deviation": 0.07, "take_profit": 0.06, "stop_loss": 0.03},
        {"min_deviation": 0.08, "take_profit": 0.08, "stop_loss": 0.04},
    ],
    "mean_reversion": [
        {"band": 0.04, "take_profit": 0.02, "stop_loss": 0.02},
        {"band": 0.06, "take_profit": 0.03, "stop_loss": 0.03},
        {"band": 0.08, "take_profit": 0.04, "stop_loss": 0.03},
    ],
}


# ── Evaluation helpers ────────────────────────────────────────────────────────

def _reward(trades: list) -> float:
    """win_rate × mean_positive_pnl.  Zero if no trades."""
    if not trades:
        return 0.0
    wins = [t.pnl for t in trades if (t.pnl or 0) > 0]
    win_rate = len(wins) / len(trades)
    avg_win  = (sum(wins) / len(wins)) if wins else 0.0
    return win_rate * avg_win


def _eval_all(closed_trades: list) -> dict[str, dict]:
    """Compute per-strategy stats from closed trades."""
    by_strat: dict[str, list] = defaultdict(list)
    for t in closed_trades:
        by_strat[t.strategy or "unknown"].append(t)

    out: dict[str, dict] = {}
    for strat, trades in by_strat.items():
        total   = len(trades)
        wins    = sum(1 for t in trades if (t.pnl or 0) > 0)
        avg_pnl = sum(t.pnl or 0 for t in trades) / total
        out[strat] = {
            "count":    total,
            "win_rate": round(wins / total, 4),
            "avg_pnl":  round(avg_pnl, 6),
            "score":    round(_reward(trades), 6),
        }
    return out


def _best_params(strategy: str, trades: list) -> dict:
    """Select the parameter variant that best fits observed trade performance.

    Since we don't log per-trade params, we use the overall win-rate and
    avg_pnl as a proxy to pick the most aligned candidate from the grid.
    """
    if len(trades) < MIN_EVAL_TRADES:
        return {}

    grid = _PARAM_GRIDS.get(strategy, [])
    if not grid:
        return {}

    total   = len(trades)
    wins    = sum(1 for t in trades if (t.pnl or 0) > 0)
    win_rate = wins / total
    avg_pnl  = sum(t.pnl or 0 for t in trades) / total
    base_reward = _reward(trades)

    best_score  = -1.0
    best_params = grid[0]

    for params in grid:
        # Heuristic: score each candidate relative to observed performance.
        if strategy == "directional":
            if win_rate > 0.65:
                # Strong signal → prefer larger TP (let winners run)
                score = base_reward * (1 + (params["take_profit"] - 0.04) * 5)
            elif win_rate < 0.45:
                # Weak signal → prefer higher min_deviation (fewer, better trades)
                score = base_reward * (1 + (params["min_deviation"] - 0.05) * 3)
            else:
                score = base_reward

        elif strategy == "market_maker":
            if avg_pnl > 0:
                # Profitable → can tighten spread for more fills
                score = base_reward * (1.0 - params["half_spread"])
            else:
                # Losing → widen spread for better margin
                score = base_reward * params["half_spread"] * 10
            score = max(score, 0)

        elif strategy == "mean_reversion":
            if win_rate > 0.55:
                # Works well → tighter band for more opportunities
                score = base_reward * (1.0 - params["band"])
            else:
                # Struggling → wider band for higher-conviction entries
                score = base_reward * params["band"] * 5
            score = max(score, 0)

        else:
            score = base_reward

        if score > best_score:
            best_score  = score
            best_params = params

    return best_params


# ── Main entry point ──────────────────────────────────────────────────────────

def run_discovery(closed_trades: list) -> dict:
    """Evaluate all strategies and write strategy_config.json.

    Returns the config dict (also written to disk).
    """
    stats = _eval_all(closed_trades)
    config: dict[str, Any] = {"strategies": {}}

    for strat in _ALL_STRATEGIES:
        s        = stats.get(strat, {})
        count    = s.get("count", 0)
        win_rate = s.get("win_rate")

        # Disable only when we have enough history and win rate is clearly poor
        if count >= MIN_EVAL_TRADES and win_rate is not None and win_rate < DISABLE_BELOW:
            enabled = False
            reason  = f"disabled: win_rate={win_rate:.0%} over {count} trades"
        else:
            enabled = True
            reason  = (
                f"enabled: win_rate={win_rate:.0%} score={s.get('score', 0):.4f}"
                if win_rate is not None
                else "enabled: no history yet"
            )

        strat_trades = [t for t in closed_trades if (t.strategy or "") == strat]
        best = _best_params(strat, strat_trades)

        config["strategies"][strat] = {
            "enabled":     enabled,
            "reason":      reason,
            "stats":       s,
            "best_params": best,
        }
        suffix = f" → params={best}" if best else ""
        logger.info(f"strategy_discovery [{strat}] {reason}{suffix}")

    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
        logger.info(f"strategy_discovery: wrote {CONFIG_FILE}")
    except Exception as exc:
        logger.warning(f"strategy_discovery: could not write config: {exc}")

    return config
