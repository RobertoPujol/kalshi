from datetime import datetime
from sqlalchemy.orm import Session

from models.database import TradeDB, SnapshotDB


class TradeRepo:

    @staticmethod
    def open_trade(db: Session, order_id: str, ticker: str,
                   strategy: str, action: str, side: str,
                   price: float, count: int,
                   event_ticker: str | None = None,
                   note: str | None = None) -> TradeDB:
        t = TradeDB(order_id=order_id, ticker=ticker,
                    event_ticker=event_ticker, strategy=strategy,
                    action=action, side=side, price=price, count=count,
                    status="open", note=note)
        db.add(t)
        db.commit()
        db.refresh(t)
        return t

    @staticmethod
    def close_trade(db: Session, order_id: str, pnl: float) -> TradeDB | None:
        t = db.query(TradeDB).filter(
            TradeDB.order_id == order_id, TradeDB.status == "open"
        ).first()
        if not t:
            return None
        t.pnl       = round(pnl, 6)
        t.status    = "closed"
        t.closed_at = datetime.utcnow()
        db.commit()
        db.refresh(t)
        return t

    @staticmethod
    def get_open_trades(db: Session) -> list[TradeDB]:
        return db.query(TradeDB).filter(TradeDB.status == "open").all()

    @staticmethod
    def get_closed_trades(db: Session, limit: int = 100) -> list[TradeDB]:
        return db.query(TradeDB).filter(
            TradeDB.status == "closed"
        ).order_by(TradeDB.closed_at.desc()).limit(limit).all()

    @staticmethod
    def save_snapshot(db: Session, balance: float, open_positions: int,
                      total_pnl: float, daily_pnl: float | None = None):
        snap = SnapshotDB(balance_usd=balance, open_positions=open_positions,
                          total_pnl=total_pnl, daily_pnl=daily_pnl)
        db.add(snap)
        db.commit()
