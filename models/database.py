"""SQLite persistence for trade history and snapshots."""
import os
from datetime import datetime
from sqlalchemy import (Column, Integer, String, Float, DateTime, Text,
                        create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.getenv("DB_PATH", "kalshitrader.db")
engine  = create_engine(f"sqlite:///{DB_PATH}",
                        connect_args={"check_same_thread": False})
Base    = declarative_base()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TradeDB(Base):
    __tablename__ = "trades"
    id          = Column(Integer, primary_key=True, index=True)
    order_id    = Column(String, index=True, nullable=True)
    ticker      = Column(String, index=True)             # Kalshi market ticker
    event_ticker = Column(String, index=True, nullable=True)
    strategy    = Column(String)        # "market_maker" | "directional"
    action      = Column(String)        # "buy" | "sell"
    side        = Column(String)        # "yes" | "no"
    price       = Column(Float)         # $ per contract, 0.01–0.99
    count       = Column(Integer)       # contracts
    pnl         = Column(Float, nullable=True)
    status      = Column(String, default="open")   # open|closed|cancelled
    opened_at   = Column(DateTime, default=datetime.utcnow)
    closed_at   = Column(DateTime, nullable=True)
    note        = Column(Text, nullable=True)


class SnapshotDB(Base):
    __tablename__ = "snapshots"
    id              = Column(Integer, primary_key=True, index=True)
    recorded_at     = Column(DateTime, default=datetime.utcnow)
    balance_usd     = Column(Float)
    open_positions  = Column(Integer)
    total_pnl       = Column(Float)
    daily_pnl       = Column(Float, nullable=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
