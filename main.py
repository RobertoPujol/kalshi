"""KalshiTrader — Kalshi automated trading engine.

Scheduler-driven main loop.  Rebuilt from the PolyTrader engine for
Kalshi's REST + WebSocket API.

Cycles:
  Every 30s   → market-making reprice cycle (active MM positions)
  Every 5min  → directional signal check (when a fair-value provider is wired)
  Every 30min → market scan (find/drop markets)
  Daily 00:00 → portfolio snapshot

Usage:
  python main.py                # live (DRY_RUN=false by default)
  DRY_RUN=true python main.py   # paper mode — orders logged, not submitted
"""
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime

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
from research.scanner import scanner
from strategies.market_maker import MarketMaker

os.makedirs("logs", exist_ok=True)
init_db()

# ── Active market makers: ticker → MarketMaker instance ──────────────────────
_market_makers: dict[str, MarketMaker] = {}

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


# ── Scheduled jobs ────────────────────────────────────────────────────────────

def job_market_scan():
    """Refresh tradeable markets and rotate the active MM set."""
    logger.info("── Market scan ──")
    try:
        markets = scanner.scan(limit=200)
        top = markets[:int(os.getenv("MAX_POSITIONS", "8"))]
        active_tickers = {m["ticker"] for m in top if m.get("ticker")}

        # Drop makers for markets no longer in the top list
        for ticker in list(_market_makers.keys()):
            if ticker not in active_tickers:
                _market_makers[ticker].cancel_all()
                del _market_makers[ticker]
                logger.info(f"Dropped MM for {ticker}")

        # Subscribe new tickers to order book and create makers
        for m in top:
            ticker = m.get("ticker")
            if not ticker or ticker in _market_makers:
                continue
            order_book_manager.subscribe(ticker)
            _market_makers[ticker] = MarketMaker(ticker=ticker)
            logger.info(f"Added MM for {ticker}")

        # (Re)start WS if new tickers were added
        order_book_manager.start()
    except Exception as e:
        logger.error(f"job_market_scan error: {e}")


def job_mm_cycle():
    """Reprice all active market-maker quotes."""
    if not _market_makers:
        return
    try:
        balance, positions = _get_context()
        logger.info(f"── MM cycle | {len(_market_makers)} market(s) | "
                    f"bal=${balance:.2f} | pos={len(positions)} ──")
        for ticker, mm in _market_makers.items():
            # Pull the scanner's cached summary for this ticker
            summary = scanner.get(ticker) or {}
            result = mm.update(balance, positions, summary)
            if result.get("action") not in ("hold", "skip"):
                logger.debug(f"  {ticker} {result}")
    except Exception as e:
        logger.error(f"job_mm_cycle error: {e}")


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


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    logger.info(f"KalshiTrader starting | mode={mode} | "
                f"env={os.getenv('KALSHI_ENV', 'prod')}")
    logger.info(f"Risk: {risk.snapshot()}")

    scheduler = BackgroundScheduler(timezone="UTC")

    # Market scan every 30 minutes; fire one immediately on startup.
    scheduler.add_job(job_market_scan, IntervalTrigger(minutes=30),
                      id="market_scan", next_run_time=datetime.utcnow())

    # MM reprice every 30 seconds
    scheduler.add_job(job_mm_cycle, IntervalTrigger(seconds=30),
                      id="mm_cycle")

    # Daily snapshot at midnight UTC
    scheduler.add_job(job_daily_snapshot, CronTrigger(hour=0, minute=0),
                      id="daily_snapshot")

    scheduler.start()
    logger.info("Scheduler started. Press Ctrl-C to stop.")

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        for mm in _market_makers.values():
            mm.cancel_all()
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
