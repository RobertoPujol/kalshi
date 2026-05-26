"""PolyTrader — Polymarket automated trading engine.

Scheduler-driven main loop.  Inspired by warproxxx/poly_data and
warproxxx/poly-maker, rebuilt as a clean standalone engine.

Cycles:
  Every 30s  → market-making reprice cycle (active MM positions)
  Every 5min → directional signal check
  Every 30min → market scan (find/drop markets)
  Daily 00:00 → portfolio snapshot

Usage:
  python main.py                # live (DRY_RUN=false by default)
  DRY_RUN=true python main.py   # paper mode — orders logged, not submitted
"""
import os
import sys
import signal
import time
from contextlib import contextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()

from utils.logger import logger
from models.database import init_db, get_db
from models.repository import TradeRepo
from core.gamma import get_market_summary
from core.orderbook import order_book_manager
from core.client import poly_client, DRY_RUN
from core.risk import risk
from research.scanner import scanner
from strategies.market_maker import MarketMaker

os.makedirs("logs", exist_ok=True)
init_db()

# ── Active market makers: token_id → MarketMaker instance ────────────────────
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
    """Return (balance, open_positions) — cheaply cached per cycle."""
    balance   = poly_client.get_balance()
    positions = poly_client.get_positions()
    return balance, positions


# ── Scheduled jobs ────────────────────────────────────────────────────────────

def job_market_scan():
    """Find the best markets and subscribe the order book to their tokens."""
    global _market_makers
    logger.info("── Market scan ──")
    try:
        markets = scanner.scan(limit=200)
        top = markets[:int(os.getenv("MAX_POSITIONS", "8"))]
        active_slugs = {m["slug"] for m in top}

        # Drop makers for markets no longer in top list
        to_drop = [tid for tid, mm in _market_makers.items()
                   if mm not in [x for x in _market_makers.values()
                                  if x.token_id in active_slugs]]
        for tid in list(_market_makers.keys()):
            slug = next((m["slug"] for m in scanner.results
                         if m.get("token_ids") and tid in str(m["token_ids"])), None)
            if slug and slug not in active_slugs:
                _market_makers[tid].cancel_all()
                del _market_makers[tid]
                logger.info(f"Dropped MM for {slug}")

        # Subscribe new tokens to order book and create makers
        for m in top:
            token_ids_raw = m.get("token_ids")
            if not token_ids_raw:
                continue
            import json
            try:
                tids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
            except Exception:
                continue
            for tid in tids[:1]:   # primary token only
                if tid not in _market_makers:
                    order_book_manager.subscribe(tid)
                    _market_makers[tid] = MarketMaker(token_id=tid)
                    logger.info(f"Added MM for {m['slug']} token={tid[:10]}...")

        # (Re)start WS if new tokens were added
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
                    f"bal={balance:.2f} USDC | pos={len(positions)} ──")
        for tid, mm in _market_makers.items():
            # Find market summary for this token
            m_summary = next(
                (get_market_summary({"slug": s, "volume": 0, "liquidity": 0})
                 for s in [next((m["slug"] for m in scanner.results
                                  if m.get("token_ids") and tid in str(m.get("token_ids",""))),
                                None)]
                 if s), {}
            )
            # Use cached scanner result if available
            for scan_m in scanner.results:
                if scan_m.get("token_ids") and tid in str(scan_m.get("token_ids", "")):
                    m_summary = scan_m
                    break
            result = mm.update(balance, positions, m_summary)
            if result.get("action") not in ("hold", "skip"):
                logger.debug(f"  {tid[:10]}... {result}")
    except Exception as e:
        logger.error(f"job_mm_cycle error: {e}")


def job_daily_snapshot():
    """Save daily portfolio snapshot to the database."""
    try:
        balance, positions = _get_context()
        with _db() as db:
            open_trades = TradeRepo.get_open_trades(db)
            closed = TradeRepo.get_closed_trades(db, limit=10000)
            total_pnl = sum(t.pnl or 0 for t in closed)
            TradeRepo.save_snapshot(db, balance=balance,
                                    open_positions=len(positions),
                                    total_pnl=total_pnl)
        logger.info(f"Snapshot saved | balance={balance:.2f} open={len(positions)} "
                    f"pnl={total_pnl:.4f}")
    except Exception as e:
        logger.error(f"job_daily_snapshot error: {e}")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    logger.info(f"PolyTrader starting | mode={mode}")
    logger.info(f"Risk: {risk.snapshot()}")

    scheduler = BackgroundScheduler(timezone="UTC")

    # Market scan every 30 minutes
    scheduler.add_job(job_market_scan, IntervalTrigger(minutes=30),
                      id="market_scan", next_run_time=__import__("datetime").datetime.utcnow())

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
