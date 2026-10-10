"""
قاعدة بيانات بسيطة للنظام الذكي
"""
import logging
from datetime import datetime
from typing import List, Optional, Dict
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, Text, inspect
)
from sqlalchemy.orm import declarative_base, sessionmaker
import config

logger = logging.getLogger(__name__)

Base = declarative_base()
engine = None
SessionLocal = None


class Position(Base):
    __tablename__ = "smart_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin = Column(String(20), nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    amount = Column(Float, nullable=False)          # الكمية المتبقية
    original_amount = Column(Float, nullable=False) # الكمية الأصلية
    cost_usdt = Column(Float, nullable=False)       # تكلفة الدخول
    stop_loss = Column(Float, nullable=False)
    take_profit_1 = Column(Float, nullable=False)
    take_profit_2 = Column(Float, nullable=False)
    tp1_hit = Column(Boolean, default=False)
    status = Column(String(20), default="open")     # open / closed
    entry_signal_multiple = Column(Float, nullable=True)
    entry_time = Column(DateTime, default=datetime.utcnow)
    exit_time = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)
    pnl_usdt = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)


class TradeLog(Base):
    __tablename__ = "smart_trade_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)       # buy / sell
    amount = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    cost_usdt = Column(Float, nullable=False)
    reason = Column(String(100), nullable=True)
    order_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SystemState(Base):
    __tablename__ = "smart_system_state"

    id = Column(Integer, primary_key=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    global engine, SessionLocal
    engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # إنشاء الجداول لو مش موجودة
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized for Smart Whale System")


def get_session():
    if SessionLocal is None:
        init_db()
    return SessionLocal()


# === Position helpers ===

def get_open_positions() -> List[Position]:
    session = get_session()
    try:
        return session.query(Position).filter(Position.status == "open").all()
    finally:
        session.close()


def get_open_position(coin: str) -> Optional[Position]:
    session = get_session()
    try:
        return (
            session.query(Position)
            .filter(Position.coin == coin.upper(), Position.status == "open")
            .first()
        )
    finally:
        session.close()


def open_position(
    coin: str,
    entry_price: float,
    amount: float,
    cost_usdt: float,
    multiple: float = None,
    notes: str = None,
) -> Position:
    session = get_session()
    try:
        stop = entry_price * (1 - config.STOP_LOSS_PCT / 100)
        tp1 = entry_price * (1 + config.TAKE_PROFIT_1_PCT / 100)
        tp2 = entry_price * (1 + config.TAKE_PROFIT_2_PCT / 100)

        pos = Position(
            coin=coin.upper(),
            entry_price=entry_price,
            amount=amount,
            original_amount=amount,
            cost_usdt=cost_usdt,
            stop_loss=stop,
            take_profit_1=tp1,
            take_profit_2=tp2,
            entry_signal_multiple=multiple,
            notes=notes,
        )
        session.add(pos)
        session.commit()
        session.refresh(pos)
        return pos
    finally:
        session.close()


def update_position_amount(position_id: int, new_amount: float, tp1_hit: bool = False):
    session = get_session()
    try:
        pos = session.query(Position).get(position_id)
        if pos:
            pos.amount = new_amount
            if tp1_hit:
                pos.tp1_hit = True
            session.commit()
    finally:
        session.close()


def close_position(
    position_id: int,
    exit_price: float,
    exit_reason: str,
    sold_amount: float = None,
):
    session = get_session()
    try:
        pos = session.query(Position).get(position_id)
        if not pos:
            return

        amount = sold_amount if sold_amount is not None else pos.amount
        # حساب تقريبي للـ PnL (نسبي للكمية المباعة)
        ratio = amount / pos.original_amount if pos.original_amount > 0 else 1
        cost_part = pos.cost_usdt * ratio
        revenue = amount * exit_price
        pnl = revenue - cost_part
        pnl_pct = (pnl / cost_part * 100) if cost_part > 0 else 0

        pos.status = "closed"
        pos.exit_time = datetime.utcnow()
        pos.exit_price = exit_price
        pos.exit_reason = exit_reason
        pos.pnl_usdt = pnl
        pos.pnl_pct = pnl_pct
        pos.amount = 0
        session.commit()
    finally:
        session.close()


def log_trade(
    coin: str,
    side: str,
    amount: float,
    price: float,
    cost_usdt: float,
    reason: str = None,
    order_id: str = None,
):
    session = get_session()
    try:
        log = TradeLog(
            coin=coin.upper(),
            side=side,
            amount=amount,
            price=price,
            cost_usdt=cost_usdt,
            reason=reason,
            order_id=order_id,
        )
        session.add(log)
        session.commit()
    finally:
        session.close()


def get_system_flag(key: str, default: str = None) -> Optional[str]:
    session = get_session()
    try:
        row = session.query(SystemState).filter(SystemState.key == key).first()
        return row.value if row else default
    finally:
        session.close()


def set_system_flag(key: str, value: str):
    session = get_session()
    try:
        row = session.query(SystemState).filter(SystemState.key == key).first()
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
        else:
            row = SystemState(key=key, value=value)
            session.add(row)
        session.commit()
    finally:
        session.close()


def is_system_running() -> bool:
    return get_system_flag("running", "0") == "1"


def set_system_running(running: bool):
    set_system_flag("running", "1" if running else "0")
