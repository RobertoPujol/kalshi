"""KalshiTrader — Kalshi automated trading engine.

Scheduler-driven main loop.  Rebuilt from the PolyTrader engine for
Kalshi's REST + WebSocket API.

Cycles:
  Every 30s   → trading cycle: MM reprice, directional signal check, mean reversion
  Every 30min → market scan (find/drop markets)
  Daily 00:00 → portfolio snapshot
  Daily 01:00 → strategy discovery + ML model retrain

Usage:
  python main.py                # live (DRY_RUN=false by default)
  DRY_RUN=true python main.py   # paper mode — orders logged, not submitted
"""
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()

from utils.logger import logger
from models.database import init_db, get_db
from models.repository import TradeRepo
from core.orderbook import order_book_manager
from core.client import kalshi_client, DRY_RUN
from core.risk import risk
from core.dashboard_sync import sync_trades
from core.fair_value_model import fair_value_model
from research.scanner import scanner
from research.strategy_discovery import run_discovery
from strategies.market_maker import MarketMaker
from strategies.directional import DirectionalTrader
from strategies.mean_reversion import MeanReversionTrader

os.makedirs("logs", exist_ok=True)
init_db()

# ── Active traders: ticker → instance ────────────────────────────────────────
_market_makers:        dict[str, MarketMaker]         = {}
_directional_traders:  dict[str, DirectionalTrader]   = {}
_mr_traders:           dict[str, MeanReversionTrader] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

@contextmanager
def _db():
    gen = get_db()
    db = next(gen)
    try:
        yield db
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def _get_context() -> tuple[float, list]:
    """Return (balance_usd, open_positions) — cheap to call once per cycle."""
    balance   = kalshi_client.get_balance()
    positions = kalshi_client.get_positions()
    return balance, positions


def _load_strategy_config() -> dict:
    """Read strategy_config.json; return {} on missing/invalid file."""
    p = Path(os.getenv("STRATEGY_CONFIG_FILE", "strategy_config.json"))
    if p.exists():
        try:
            return json.loads(p.read_text()).get("strategies", {})
        except Exception:
            pass
    return {}


def _strategy_enabled(config: dict, name: str) -> bool:
    return config.get(name, {}).get("enabled", True)


# ── Scheduled jobs ────────────────────────────────────────────────────────────

def job_market_scan():
    """Refresh tradeable markets and rotate the active trader set."""
    logger.info("── Market scan ──")
    try:
        markets = scanner.scan(limit=200)
        top = markets[:int(os.getenv("MAX_POSITIONS", "8"))]
        active_tickers = {m["ticker"] for m in top if m.get("ticker")}
        config = _load_strategy_config()

        # Drop traders for markets no longer in the top list
        for ticker in list(_market_makers.keys()):
            if ticker not in active_tickers:
                _market_makers[ticker].cancel_all()
                del _market_makers[ticker]
                if ticker in _directional_traders:
                    _directional_traders[ticker].cancel_all()
                    del _directional_traders[ticker]
                if ticker in _mr_traders:
                    _mr_traders[ticker].cancel_all()
                    del _mr_traders[ticker]
                logger.info(f"Dropped all traders for {ticker}")

        # Subscribe new tickers and create strategy instances
        for m in top:
            ticker = m.get("ticker")
            if not ticker:
                continue

            if ticker not in _market_makers:
                order_book_manager.subscribe(ticker)
                _market_makers[ticker] = MarketMaker(ticker=ticker)
                logger.info(f"Added MarketMaker for {ticker}")

            if (_strategy_enabled(config, "directional")
                    and ticker not in _directional_traders):
                _directional_traders[ticker] = DirectionalTrader(
                    ticker=ticker, fair_value_provider=fair_value_model,
                )
                logger.info(f"Added DirectionalTrader for {ticker}")

            if (_strategy_enabled(config, "mean_reversion")
                    and ticker not in _mr_traders):
                _mr_traders[ticker] = MeanReversionTrader(ticker=ticker)
                logger.info(f"Added MeanReversionTrader for {ticker}")

        order_book_manager.start()
    except Exception as e:
        logger.error(f"job_market_scan error: {e}")


def job_trading_cycle():
    """Run all active strategy instances for one cycle."""
    if not any([_market_makers, _directional_traders, _mr_traders]):
        return
    try:
        balance, positions = _get_context()
        n = (len(_market_makers) + len(_directional_traders) + len(_mr_traders))
        logger.info(f"── Trading cycle | {n} traders | "
                    f"bal=${balance:.2f} | pos={len(positions)} ──")

        for ticker, mm in _market_makers.items():
            summary = scanner.get(ticker) or {}
            result  = mm.update(balance, positions, summary)
            if result.get("action") not in ("hold", "skip", "watch"):
                logger.debug(f"  MM  {ticker} {result}")

        for ticker, dt in _directional_traders.items():
            summary = scanner.get(ticker) or {}
            result  = dt.update(balance, positions, summary)
            if result.get("action") not in ("hold", "skip", "watch"):
                logger.debug(f"  DIR {ticker} {result}")

        for ticker, mr in _mr_traders.items():
            summary = scanner.get(ticker) or {}
            result  = mr.update(balance, positions, summary)
            if result.get("action") not in ("hold", "skip", "watch"):
                logger.debug(f"  MR  {ticker} {result}")

    except Exception as e:
        logger.error(f"job_trading_cycle error: {e}")


def job_daily_snapshot():
    """Save the daily portfolio snapshot."""
    try:
        balance, positions = _get_context()
        with _db() as db:
            closed = TradeRepo.get_closed_trades(db, limit=10000)
            total_pnl = sum(t.pnl or 0 for t in closed)
            TradeRepo.save_snapshot(db, balance=balance,
                                    open_positions=len(positions),
                                    total_pnl=total_pnl)
        logger.info(f"Snapshot saved | balance=${balance:.2f} "
                    f"open={len(positions)} pnl=${total_pnl:.4f}")
    except Exception as e:
        logger.error(f"job_daily_snapshot error: {e}")


def job_strategy_discovery():
    """Evaluate strategy performance and retrain the ML fair-value model."""
    logger.info("── Strategy discovery ──")
    try:
        with _db() as db:
            closed = TradeRepo.get_closed_trades(db, limit=10_000)
        run_discovery(closed)
    except Exception as e:
        logger.error(f"job_strategy_discovery error: {e}")

    # Retrain ML model daily alongside discovery
    try:
        if fair_value_model.needs_retrain():
            fair_value_model.train()
    except Exception as e:
        logger.error(f"job_strategy_discovery (model train) error: {e}")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    logger.info(f"KalshiTrader starting | mode={mode} | "
                f"env={os.getenv('KALSHI_ENV', 'prod')}")
    logger.info(f"Risk: {risk.snapshot()}")

    if not DRY_RUN:
        open_orders = kalshi_client.get_open_orders()
        if open_orders:
            logger.info(f"Cancelling {len(open_orders)} stale order(s) from previous run")
            for o in open_orders:
                kalshi_client.cancel_order(o["order_id"])

    # Load ML model in background — directional trades sit out until it trains
    if fair_value_model.needs_retrain():
        logger.info("Fair-value model is stale — training on startup…")
        fair_value_model.train()

    scheduler = BackgroundScheduler(timezone="UTC")

    # Market scan every 30 minutes; fire immediately on startup
    scheduler.add_job(job_market_scan, IntervalTrigger(minutes=30),
                      id="market_scan", next_run_time=datetime.utcnow())

    # Trading cycle every 30 seconds (MM reprice + directional + mean-reversion)
    scheduler.add_job(job_trading_cycle, IntervalTrigger(seconds=30),
                      id="trading_cycle")

    # Daily snapshot at midnight UTC
    scheduler.add_job(job_daily_snapshot, CronTrigger(hour=0, minute=0),
                      id="daily_snapshot")

    # Push closed trades to dashboard every 5 minutes; also fire immediately
    scheduler.add_job(sync_trades, IntervalTrigger(minutes=5),
                      id="dashboard_sync", next_run_time=datetime.utcnow())

    # Strategy discovery + ML retrain daily at 01:00 UTC; also fire immediately
    scheduler.add_job(job_strategy_discovery, CronTrigger(hour=1, minute=0),
                      id="strategy_discovery", next_run_time=datetime.utcnow())

    scheduler.start()
    logger.info("Scheduler started. Press Ctrl-C to stop.")

    def shutdown(sig, frame):
        logger.info("Shutting down…")
        for mm in _market_makers.values():
            mm.cancel_all()
        for dt in _directional_traders.values():
            dt.cancel_all()
        for mr in _mr_traders.values():
            mr.cancel_all()
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
