"""Push closed bot trades to the Kalshi dashboard Worker on Cloudflare,
then recompute adaptive trading parameters from the same data.

Set these in .env:
  DASHBOARD_URL     = https://kalshi-dashboard.weightloss.workers.dev
  DASHBOARD_SECRET  = <value of the BOT_SYNC_SECRET Worker secret>
"""
import os
from datetime import datetime

import requests

from core.adaptive_params import update_from_trades
from models.database import SessionLocal
from models.repository import TradeRepo
from utils.logger import logger

DASHBOARD_URL    = os.getenv("DASHBOARD_URL", "").rstrip("/")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def sync_trades() -> None:
    if not DASHBOARD_URL or not DASHBOARD_SECRET:
        logger.debug("dashboard sync skipped — DASHBOARD_URL / DASHBOARD_SECRET not set")
        return
    db = SessionLocal()
    try:
        closed = TradeRepo.get_closed_trades(db, limit=10_000)
        update_from_trades(closed)   # always tune params, even if dashboard is unreachable
        if not closed:
            return
        payload = [
            {
                "bot_id":       t.id,
                "order_id":     t.order_id,
                "ticker":       t.ticker,
                "event_ticker": t.event_ticker,
                "strategy":     t.strategy,
                "action":       t.action,
                "side":         t.side,
                "price":        t.price,
                "count":        t.count,
                "pnl":          t.pnl,
                "status":       t.status,
                "opened_at":    _iso(t.opened_at),
                "closed_at":    _iso(t.closed_at),
                "note":         t.note,
            }
            for t in closed
        ]
        resp = requests.post(
            DASHBOARD_URL + "/api/ingest-trades",
            json=payload,
            headers={"X-Bot-Secret": DASHBOARD_SECRET},
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"Dashboard sync: pushed {len(payload)} trade(s) → HTTP {resp.status_code}")
    except Exception as exc:
        logger.warning(f"Dashboard sync failed (non-fatal): {exc}")
    finally:
        db.close()
