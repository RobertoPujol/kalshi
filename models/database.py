"""SQLite persistence for trade history and snapshots."""
import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.getenv("DB_PATH", "polytrader.db")
engine  = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
Base    = declarative_base()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TradeDB(Base):
    __tablename__ = "trades"
    id          = Column(Integer, primary_key=True, index=True)
    order_id    = Column(String, index=True, nullable=True)
    slug        = Column(String, index=True)
    token_id    = Column(String)
    strategy    = Column(String)          # "market_maker" | "directional"
    side        = Column(String)          # "BUY" | "SELL"
    price       = Column(Float)
    size        = Column(Float)
    pnl         = Column(Float, nullable=True)
    status      = Column(String, default="open")   # open | closed | cancelled
    opened_at   = Column(DateTime, default=datetime.utcnow)
    closed_at   = Column(DateTime, nullable=True)
    note        = Column(Text, nullable=True)


class SnapshotDB(Base):
    __tablename__ = "snapshots"
    id              = Column(Integer, primary_key=True, index=True)
    recorded_at     = Column(DateTime, default=datetime.utcnow)
    balance_usdc    = Column(Float)
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
