import atexit
import os
import tempfile
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
    text,
    update,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

import config

# psycopg3 names prepared statements on the connection. Transaction-pooling
# proxies such as PgBouncer can return a connection whose name already exists,
# causing DuplicatePreparedStatement during ordinary SELECT/UPDATE operations.
# Apply the setting to the DBAPI connection itself because some hosts expose a
# plain ``postgresql://`` URL while SQLAlchemy still selects psycopg3.
engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)


@event.listens_for(engine, "connect")
def _disable_psycopg_prepared_statements(dbapi_connection, connection_record):
    """Disable psycopg3 server-side preparation for pooled/proxied databases."""
    if hasattr(dbapi_connection, "prepare_threshold"):
        dbapi_connection.prepare_threshold = None
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

_singleton_connection = None
_singleton_file = None


def acquire_singleton_lock() -> bool:
    """Allow only one running Telegram poller for this deployment/database."""
    global _singleton_connection, _singleton_file
    if _singleton_connection is not None or _singleton_file is not None:
        return True

    if engine.dialect.name == "postgresql":
        connection = engine.connect()
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:lock_key))"),
            {"lock_key": "lotfe.telegram.polling"},
        ).scalar()
        if not acquired:
            connection.close()
            return False
        _singleton_connection = connection
        atexit.register(release_singleton_lock)
        return True

    # SQLite/local fallback: an OS lock prevents duplicate local processes.
    import fcntl

    lock_path = os.path.join(tempfile.gettempdir(), "lotfe-telegram-polling.lock")
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False
    _singleton_file = lock_file
    atexit.register(release_singleton_lock)
    return True


def release_singleton_lock() -> None:
    """Release the singleton lock when the process exits."""
    global _singleton_connection, _singleton_file
    if _singleton_connection is not None:
        _singleton_connection.close()
        _singleton_connection = None
    if _singleton_file is not None:
        import fcntl

        fcntl.flock(_singleton_file.fileno(), fcntl.LOCK_UN)
        _singleton_file.close()
        _singleton_file = None


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    default_threshold = Column(Float, default=2.0)
    default_interval_hours = Column(Integer, default=24)
    default_allocation_method = Column(String(20), default="equal")
    default_rebalance_mode = Column(String(20), default="threshold")
    min_trade_usdt = Column(Float, default=2.0)
    max_coins_per_portfolio = Column(Integer, default=30)  # raised 10 → 30
    min_usdt_per_coin = Column(Float, default=2.0)
    # Multi TP + SL (configurable from Telegram bot)
    tp1_pct = Column(Float, default=3.0)    # الهدف 1 %
    tp2_pct = Column(Float, default=5.0)    # الهدف 2 %
    tp3_pct = Column(Float, default=8.0)    # الهدف 3 %
    tp1_sell_pct = Column(Float, default=40.0)  # نسبة البيع عند الهدف 1
    tp2_sell_pct = Column(Float, default=40.0)  # نسبة البيع عند الهدف 2
    # الباقي يُباع عند الهدف 3 أو يبقى مع الاستوب المرفوع
    stop_loss_pct = Column(Float, default=3.0)
    dca_enabled = Column(Boolean, default=False)
    dca_buy_pct = Column(Integer, default=50)
    dca_order_count = Column(Integer, default=3)
    dca_range_pct = Column(Float, default=6.0)
    dca1_alloc_pct = Column(Float, default=50.0)
    dca2_alloc_pct = Column(Float, default=30.0)
    dca3_alloc_pct = Column(Float, default=20.0)
    dca1_drop_pct = Column(Float, default=3.0)
    dca2_drop_pct = Column(Float, default=6.0)
    dca3_drop_pct = Column(Float, default=10.0)
    # حقول قديمة محفوظة للتوافق مع قواعد البيانات السابقة
    trail_pct = Column(Float, default=2.0)      # مسافة الاستوب تحت السعر %
    be_lock_pct = Column(Float, default=4.0)    # بعد ربح X% → قفل عند الدخول
    # legacy single field kept for migration compatibility
    take_profit_pct = Column(Float, default=5.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    investment_usdt = Column(Float, default=0.0)
    base_investment = Column(Float, default=0.0)
    status = Column(String(20), default="active")
    is_running = Column(Boolean, default=False)

    # Entry strategy selected for this portfolio.
    entry_strategy = Column(String(20), default="tp_sl")
    dca_buy_pct = Column(Integer, default=50)  # 50 = 50% buy now / 50% reserved for DCA
    dca_order_count = Column(Integer, default=3)
    dca_range_pct = Column(Float, default=6.0)
    dca1_alloc_pct = Column(Float, default=50.0)
    dca2_alloc_pct = Column(Float, default=30.0)
    dca3_alloc_pct = Column(Float, default=20.0)
    dca1_drop_pct = Column(Float, default=3.0)
    dca2_drop_pct = Column(Float, default=6.0)
    dca3_drop_pct = Column(Float, default=10.0)

    allocation_method = Column(String(20), default="equal")
    rebalance_mode = Column(String(20), default="threshold")
    # جميع المحافظ تعمل يدويًا؛ الحقل محفوظ لتوافق قواعد البيانات القديمة
    control_mode = Column(String(10), default="manual", nullable=False)
    threshold = Column(Float, default=2.0)
    rebalance_interval_hours = Column(Integer, default=24)

    # Per-portfolio TP/SL (None = use user defaults; stop-loss 0 explicitly disables SL)
    tp1_pct = Column(Float, nullable=True)
    tp2_pct = Column(Float, nullable=True)
    tp3_pct = Column(Float, nullable=True)
    tp1_sell_pct = Column(Float, nullable=True)
    tp2_sell_pct = Column(Float, nullable=True)
    stop_loss_pct = Column(Float, nullable=True)
    trail_pct = Column(Float, nullable=True)
    be_lock_pct = Column(Float, nullable=True)

    last_rebalance = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    coins = relationship("PortfolioCoin", back_populates="portfolio", cascade="all, delete-orphan")


class PortfolioCoin(Base):
    __tablename__ = "portfolio_coins"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    target_percent = Column(Float, default=0.0)
    # Position tracking for multi-TP / SL
    entry_price = Column(Float, default=0.0)
    tp1_price = Column(Float, default=0.0)
    tp2_price = Column(Float, default=0.0)
    tp3_price = Column(Float, default=0.0)
    tp_price = Column(Float, default=0.0)  # legacy / current next TP
    current_sl_price = Column(Float, default=0.0)
    tp1_order_id = Column(String(64), nullable=True)
    tp2_order_id = Column(String(64), nullable=True)
    tp3_order_id = Column(String(64), nullable=True)
    tp_order_id = Column(String(64), nullable=True)  # legacy
    position_status = Column(String(20), default="idle")  # idle | open | tp1_hit | tp2_hit | tp3_hit | closed
    amount = Column(Float, default=0.0)
    remaining_amount = Column(Float, default=0.0)
    original_sl_price = Column(Float, default=0.0)  # initial SL, used for re-entry
    reentry_price = Column(Float, default=0.0)
    reentry_used = Column(Boolean, default=False)   # only one re-entry per cycle
    reentry_touched = Column(Boolean, default=False)  # price touched reentry zone
    # Persistent exchange-order reconciliation for market buys/re-entry.
    pending_order_id = Column(String(64), nullable=True)
    pending_order_side = Column(String(10), nullable=True)
    pending_order_usdt = Column(Float, default=0.0)
    pending_order_created_at = Column(DateTime, nullable=True)
    # A DCA fill changes the protected position size. This durable checkpoint
    # prevents a restart between exchange-side TP placement and DB persistence
    # from blindly creating a second set of sell orders.
    tp_refresh_pending = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    portfolio = relationship("Portfolio", back_populates="coins")


class PortfolioDcaOrder(Base):
    """Persistent records for portfolio-owned exchange limit-buy orders."""
    __tablename__ = "portfolio_dca_orders"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, index=True, nullable=False)
    portfolio_coin_id = Column(Integer, index=True, nullable=True)
    symbol = Column(String(20), nullable=False, index=True)
    order_id = Column(String(64), nullable=True, index=True)
    level = Column(Integer, default=1)
    limit_price = Column(Float, default=0.0)
    planned_usdt = Column(Float, default=0.0)
    filled_amount = Column(Float, default=0.0)
    filled_cost = Column(Float, default=0.0)
    status = Column(String(20), default="open", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortfolioTrade(Base):
    """سجل خروج فعلي من مركز، ويُستخدم للإحصائيات وإعادة الدخول اليدوية."""
    __tablename__ = "portfolio_trades"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    portfolio_id = Column(Integer, index=True, nullable=False)
    portfolio_coin_id = Column(Integer, index=True, nullable=True)
    symbol = Column(String(20), nullable=False)
    event_type = Column(String(30), nullable=False)  # tp1 | tp2 | tp3 | stop_loss | reentry
    entry_price = Column(Float, default=0.0)
    exit_price = Column(Float, default=0.0)
    amount = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    reentry_available = Column(Boolean, default=False)
    reentry_used = Column(Boolean, default=False)
    details = Column(String(500), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class RebalanceLog(Base):
    __tablename__ = "rebalance_logs"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    portfolio_id = Column(Integer, nullable=True)
    action = Column(String(50), nullable=False)
    details = Column(String(500), nullable=True)
    success = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)




def init_db():
    Base.metadata.create_all(bind=engine)

    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in ("user_settings", "portfolios", "rebalance_logs"):
            if table not in insp.get_table_names():
                continue
            cols = [c["name"] for c in insp.get_columns(table)]
            if "discord_id" in cols and "telegram_id" not in cols:
                conn.execute(text(f'ALTER TABLE {table} RENAME COLUMN discord_id TO telegram_id'))
                print(f"[migration] Renamed {table}.discord_id → telegram_id")
            elif "discord_id" in cols and "telegram_id" in cols:
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN discord_id'))
                print(f"[migration] Dropped leftover {table}.discord_id")

        if "portfolios" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("portfolios")]
            if "is_running" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN is_running BOOLEAN DEFAULT FALSE"))
            if "started_at" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN started_at TIMESTAMP"))
            if "stopped_at" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN stopped_at TIMESTAMP"))
            if "base_investment" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN base_investment DOUBLE PRECISION DEFAULT 0"))
            if "entry_strategy" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN entry_strategy VARCHAR(20) DEFAULT 'tp_sl'"))
            if "dca_buy_pct" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN dca_buy_pct INTEGER DEFAULT 50"))
            if "dca_order_count" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN dca_order_count INTEGER DEFAULT 3"))
            if "dca_range_pct" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN dca_range_pct DOUBLE PRECISION DEFAULT 6.0"))
            for col, default in [("dca1_alloc_pct", "50.0"), ("dca2_alloc_pct", "30.0"), ("dca3_alloc_pct", "20.0"), ("dca1_drop_pct", "3.0"), ("dca2_drop_pct", "6.0"), ("dca3_drop_pct", "10.0")]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE portfolios ADD COLUMN {col} DOUBLE PRECISION DEFAULT {default}"))
            if "control_mode" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN control_mode VARCHAR(10) DEFAULT 'manual'"))
            for col in ("tp1_pct", "tp2_pct", "tp3_pct", "tp1_sell_pct", "tp2_sell_pct", "stop_loss_pct", "trail_pct", "be_lock_pct"):
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE portfolios ADD COLUMN {col} DOUBLE PRECISION"))
            # Preserve every portfolio and trade record, but remove the obsolete
            # smart/manual choice from runtime behavior.
            conn.execute(text("UPDATE portfolios SET control_mode = 'manual' WHERE control_mode IS NULL OR control_mode != 'manual'"))

        if "portfolio_coins" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("portfolio_coins")]
            if "target_percent" not in cols:
                conn.execute(text("ALTER TABLE portfolio_coins ADD COLUMN target_percent DOUBLE PRECISION DEFAULT 0"))
            for col, typ in [
                ("entry_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp1_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp2_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp3_price", "DOUBLE PRECISION DEFAULT 0"),
                ("current_sl_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp_order_id", "VARCHAR(64)"),
                ("tp1_order_id", "VARCHAR(64)"),
                ("tp2_order_id", "VARCHAR(64)"),
                ("tp3_order_id", "VARCHAR(64)"),
                ("position_status", "VARCHAR(20) DEFAULT 'idle'"),
                ("amount", "DOUBLE PRECISION DEFAULT 0"),
                ("remaining_amount", "DOUBLE PRECISION DEFAULT 0"),
                ("original_sl_price", "DOUBLE PRECISION DEFAULT 0"),
                ("reentry_price", "DOUBLE PRECISION DEFAULT 0"),
                ("reentry_used", "BOOLEAN DEFAULT FALSE"),
                ("reentry_touched", "BOOLEAN DEFAULT FALSE"),
                ("pending_order_id", "VARCHAR(64)"),
                ("pending_order_side", "VARCHAR(10)"),
                ("pending_order_usdt", "DOUBLE PRECISION DEFAULT 0"),
                ("pending_order_created_at", "TIMESTAMP"),
                ("tp_refresh_pending", "BOOLEAN DEFAULT FALSE"),
            ]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE portfolio_coins ADD COLUMN {col} {typ}"))

        if "user_settings" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("user_settings")]
            for col, default in [
                ("take_profit_pct", "5.0"),
                ("stop_loss_pct", "3.0"),
                ("tp1_pct", "3.0"),
                ("tp2_pct", "5.0"),
                ("tp3_pct", "8.0"),
                ("trail_pct", "2.0"),
                ("be_lock_pct", "4.0"),
                ("tp1_sell_pct", "40.0"),
                ("tp2_sell_pct", "40.0"),
            ]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE user_settings ADD COLUMN {col} DOUBLE PRECISION DEFAULT {default}"))
            for col, ddl in [
                ("dca_enabled", "BOOLEAN DEFAULT FALSE"),
                ("dca_buy_pct", "INTEGER DEFAULT 50"),
                ("dca_order_count", "INTEGER DEFAULT 3"),
                ("dca_range_pct", "DOUBLE PRECISION DEFAULT 6.0"),
                ("dca1_alloc_pct", "DOUBLE PRECISION DEFAULT 50.0"),
                ("dca2_alloc_pct", "DOUBLE PRECISION DEFAULT 30.0"),
                ("dca3_alloc_pct", "DOUBLE PRECISION DEFAULT 20.0"),
                ("dca1_drop_pct", "DOUBLE PRECISION DEFAULT 3.0"),
                ("dca2_drop_pct", "DOUBLE PRECISION DEFAULT 6.0"),
                ("dca3_drop_pct", "DOUBLE PRECISION DEFAULT 10.0"),
            ]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE user_settings ADD COLUMN {col} {ddl}"))
            try:
                conn.execute(text("UPDATE user_settings SET max_coins_per_portfolio = 30 WHERE max_coins_per_portfolio < 30 OR max_coins_per_portfolio IS NULL"))
            except Exception:
                pass


def get_or_create_user(db, telegram_id: int):
    user = db.query(UserSettings).filter(UserSettings.telegram_id == telegram_id).first()
    if not user:
        user = UserSettings(telegram_id=telegram_id, max_coins_per_portfolio=30)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_portfolios(db, telegram_id: int, status: str = "active"):
    q = db.query(Portfolio).filter(Portfolio.telegram_id == telegram_id)
    if status:
        q = q.filter(Portfolio.status == status)
    return q.order_by(Portfolio.created_at.desc()).all()


def get_portfolio(db, portfolio_id: int, telegram_id: int = None):
    q = db.query(Portfolio).filter(Portfolio.id == portfolio_id)
    if telegram_id:
        q = q.filter(Portfolio.telegram_id == telegram_id)
    return q.first()


def create_portfolio(db, telegram_id: int, name: str, investment: float, coins: list,
                     allocation_method: str = "equal", rebalance_mode: str = "threshold",
                     threshold: float = 2.0, interval: int = 24,
                     entry_strategy: str = "tp_sl", dca_buy_pct: int = 50,
                     dca_order_count: int = 1) -> Portfolio:
    p = Portfolio(
        telegram_id=telegram_id,
        name=name,
        investment_usdt=investment,
        base_investment=investment,
        allocation_method=allocation_method,
        rebalance_mode=rebalance_mode,
        threshold=threshold,
        rebalance_interval_hours=interval,
        status="active",
        is_running=False,
        entry_strategy=entry_strategy if entry_strategy in ("tp_sl", "tp_sl_dca") else "tp_sl",
        dca_buy_pct=int(dca_buy_pct) if int(dca_buy_pct) in (50, 70) else 50,
        dca_order_count=int(dca_order_count) if int(dca_order_count) in (1, 2, 3) else 1,
    )
    db.add(p)
    db.flush()
    seen = set()
    for raw_symbol in coins:
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        db.add(PortfolioCoin(portfolio_id=p.id, symbol=symbol))
    db.commit()
    db.refresh(p)
    return p


def add_coin_to_portfolio(db, portfolio_id: int, symbol: str, max_coins: int = 30):
    symbol = symbol.upper().strip()
    p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if not p:
        return False, "المحفظة غير موجودة"
    existing = [c.symbol for c in p.coins]
    if symbol in existing:
        return False, f"`{symbol}` موجودة مسبقاً"
    if len(existing) >= max_coins:
        return False, f"وصلت للحد الأقصى ({max_coins})"
    db.add(PortfolioCoin(portfolio_id=portfolio_id, symbol=symbol))
    db.commit()
    return True, f"✅ تم إضافة `{symbol}`"


def remove_coin_from_portfolio(db, portfolio_id: int, symbol: str) -> bool:
    deleted = db.query(PortfolioCoin).filter(
        PortfolioCoin.portfolio_id == portfolio_id,
        PortfolioCoin.symbol == symbol.upper()
    ).delete()
    db.commit()
    return deleted > 0


def delete_portfolio_completely(db, portfolio_id: int, telegram_id: int = None) -> bool:
    """Delete a portfolio and its non-relational history after an explicit cleanup."""
    q = db.query(Portfolio).filter(Portfolio.id == portfolio_id)
    if telegram_id is not None:
        q = q.filter(Portfolio.telegram_id == telegram_id)
    portfolio = q.first()
    if not portfolio:
        return False

    db.query(PortfolioDcaOrder).filter(
        PortfolioDcaOrder.portfolio_id == portfolio_id
    ).delete(synchronize_session=False)
    db.query(PortfolioTrade).filter(
        PortfolioTrade.portfolio_id == portfolio_id
    ).delete(synchronize_session=False)
    db.query(RebalanceLog).filter(
        RebalanceLog.portfolio_id == portfolio_id
    ).delete(synchronize_session=False)
    db.delete(portfolio)
    db.commit()
    return True


def clear_coin_position(db, coin_id: int) -> bool:
    """Clear active TP/SL/re-entry state while keeping the configured coin."""
    coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == coin_id).first()
    if not coin:
        return False
    # A cleared position is a cycle boundary. Any pending stop-loss re-entry
    # offer belongs to the old cycle and must be consumed as history only.
    invalidate_reentry_events(db, coin.portfolio_id, coin.symbol, reason="position cleared")
    coin.entry_price = 0.0
    coin.tp_price = 0.0
    coin.tp1_price = 0.0
    coin.tp2_price = 0.0
    coin.tp3_price = 0.0
    coin.current_sl_price = 0.0
    coin.tp_order_id = None
    coin.tp1_order_id = None
    coin.tp2_order_id = None
    coin.tp3_order_id = None
    coin.position_status = "idle"
    coin.amount = 0.0
    coin.remaining_amount = 0.0
    coin.original_sl_price = 0.0
    coin.reentry_price = 0.0
    coin.reentry_used = False
    coin.reentry_touched = False
    coin.tp_refresh_pending = False
    db.commit()
    return True


def clear_coin_order_records(db, coin_id: int) -> bool:
    """Delete all persisted TP order references for a coin, keeping its position data."""
    coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == coin_id).first()
    if not coin:
        return False
    coin.tp_order_id = None
    coin.tp1_order_id = None
    coin.tp2_order_id = None
    coin.tp3_order_id = None
    coin.tp_price = 0.0
    coin.tp1_price = 0.0
    coin.tp2_price = 0.0
    coin.tp3_price = 0.0
    coin.tp_refresh_pending = False
    db.commit()
    return True


def delete_orphaned_portfolio_records(db, telegram_id: int):
    """Remove trade/log rows that reference a portfolio no longer in the DB."""
    portfolio_ids = {
        portfolio_id
        for (portfolio_id,) in db.query(Portfolio.id).filter(
            Portfolio.telegram_id == telegram_id
        ).all()
    }
    trade_query = db.query(PortfolioTrade).filter(
        PortfolioTrade.telegram_id == telegram_id
    )
    log_query = db.query(RebalanceLog).filter(
        RebalanceLog.telegram_id == telegram_id
    )
    if portfolio_ids:
        trade_query = trade_query.filter(
            ~PortfolioTrade.portfolio_id.in_(portfolio_ids)
        )
        log_query = log_query.filter(
            ~RebalanceLog.portfolio_id.in_(portfolio_ids)
        )
    trade_count = trade_query.delete(synchronize_session=False)
    log_count = log_query.delete(synchronize_session=False)
    # Remove only genuinely orphaned DCA records; do not delete another user's
    # still-live portfolio orders while sweeping this user's historical rows.
    dca_count = db.query(PortfolioDcaOrder).filter(
        ~PortfolioDcaOrder.portfolio_id.in_(db.query(Portfolio.id))
    ).delete(synchronize_session=False)
    if trade_count or log_count or dca_count:
        db.commit()
    return trade_count, log_count


def close_portfolio(db, portfolio_id: int):
    p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if p:
        invalidate_reentry_events(db, portfolio_id, reason="portfolio closed")
        for c in p.coins:
            c.entry_price = 0.0
            c.tp_price = 0.0
            c.tp1_price = 0.0
            c.tp2_price = 0.0
            c.tp3_price = 0.0
            c.current_sl_price = 0.0
            c.tp_order_id = None
            c.tp1_order_id = None
            c.tp2_order_id = None
            c.tp3_order_id = None
            c.position_status = "idle"
            c.amount = 0.0
            c.remaining_amount = 0.0
            c.original_sl_price = 0.0
            c.reentry_price = 0.0
            c.reentry_used = False
            c.reentry_touched = False
            c.tp_refresh_pending = False
        p.status = "closed"
        p.is_running = False
        p.closed_at = datetime.utcnow()
        db.commit()


def set_portfolio_running(db, portfolio_id: int, running: bool):
    p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if p:
        p.is_running = running
        if running:
            p.started_at = datetime.utcnow()
            p.stopped_at = None
        else:
            p.stopped_at = datetime.utcnow()
        db.commit()
        return p
    return None


def update_coin_position(db, coin_id: int, commit: bool = True, **kwargs):
    """Update position fields on a PortfolioCoin."""
    coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == coin_id).first()
    if not coin:
        return None
    for k, v in kwargs.items():
        if hasattr(coin, k):
            setattr(coin, k, v)
    if commit:
        db.commit()
        db.refresh(coin)
    return coin


def invalidate_reentry_events(db, portfolio_id: int, symbol: str = None, reason: str = ""):
    """Invalidate all pending re-entry offers from an old position cycle.

    Historical trade rows are intentionally preserved for P&L/audit purposes;
    only the actionable state is consumed so it cannot leak into a new cycle.
    """
    q = db.query(PortfolioTrade).filter(
        PortfolioTrade.portfolio_id == portfolio_id,
        PortfolioTrade.event_type == "stop_loss",
        PortfolioTrade.reentry_available == True,
        PortfolioTrade.reentry_used == False,
    )
    if symbol:
        q = q.filter(PortfolioTrade.symbol == symbol.upper())
    events = q.all()
    for ev in events:
        ev.reentry_available = False
        ev.reentry_used = True
        if reason:
            suffix = f" | {reason}"
            ev.details = ((ev.details or "") + suffix)[:500]
    return len(events)


def reset_portfolio_cycle_state(db, portfolio_id: int, invalidate_reentry: bool = True):
    """Hard-reset ACTIVE position state while preserving portfolio configuration/history.

    This is the boundary between two position cycles. No TP/SL/re-entry state
    from the previous cycle may remain actionable after this function returns.
    """
    if invalidate_reentry:
        invalidate_reentry_events(db, portfolio_id, reason="cycle reset")

    coins = db.query(PortfolioCoin).filter(PortfolioCoin.portfolio_id == portfolio_id).all()
    for c in coins:
        c.entry_price = 0.0
        c.tp_price = 0.0
        c.tp1_price = 0.0
        c.tp2_price = 0.0
        c.tp3_price = 0.0
        c.current_sl_price = 0.0
        c.pending_order_id = None
        c.pending_order_side = None
        c.pending_order_usdt = 0.0
        c.pending_order_created_at = None
        c.tp_order_id = None
        c.tp1_order_id = None
        c.tp2_order_id = None
        c.tp3_order_id = None
        c.position_status = "idle"
        c.amount = 0.0
        c.remaining_amount = 0.0
        c.original_sl_price = 0.0
        c.reentry_price = 0.0
        c.reentry_used = False
        c.reentry_touched = False
        c.tp_refresh_pending = False
    db.commit()
    return {"coins_reset": len(coins)}


def reset_coin_positions(db, portfolio_id: int):
    """Backward-compatible wrapper: stop a portfolio and close the active cycle."""
    return reset_portfolio_cycle_state(db, portfolio_id, invalidate_reentry=True)


def record_trade_event(
    db,
    telegram_id: int,
    portfolio_id: int,
    portfolio_coin_id: int,
    symbol: str,
    event_type: str,
    entry_price: float,
    exit_price: float,
    amount: float,
    realized_pnl: float,
    reentry_available: bool = False,
    details: str = "",
):
    event = PortfolioTrade(
        telegram_id=telegram_id,
        portfolio_id=portfolio_id,
        portfolio_coin_id=portfolio_coin_id,
        symbol=symbol,
        event_type=event_type,
        entry_price=float(entry_price or 0),
        exit_price=float(exit_price or 0),
        amount=float(amount or 0),
        realized_pnl=float(realized_pnl or 0),
        reentry_available=reentry_available,
        reentry_used=False,
        details=(details or "")[:500],
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_trade_event(db, event_id: int, telegram_id: int = None):
    q = db.query(PortfolioTrade).filter(PortfolioTrade.id == event_id)
    if telegram_id:
        q = q.filter(PortfolioTrade.telegram_id == telegram_id)
    return q.first()


def claim_reentry_event(db, event_id: int, telegram_id: int = None) -> bool:
    """Atomically consume a manual re-entry event.

    This closes the double-click/race window where two Telegram callbacks could
    both observe reentry_used=False and submit two market buys.
    """
    filters = [
        PortfolioTrade.id == event_id,
        PortfolioTrade.event_type == "stop_loss",
        PortfolioTrade.reentry_available == True,
        PortfolioTrade.reentry_used == False,
    ]
    if telegram_id is not None:
        filters.append(PortfolioTrade.telegram_id == telegram_id)
    result = db.execute(
        update(PortfolioTrade)
        .where(*filters)
        .values(reentry_available=False, reentry_used=True)
    )
    db.commit()
    return result.rowcount == 1


def has_running_symbol_conflict(db, telegram_id: int, portfolio_id: int, symbols) -> list:
    """Return symbols already owned by another running portfolio of the user.

    MEXC Spot balances are account-level, so two simultaneously-running virtual
    portfolios cannot safely own the same base asset without a separate custody
    ledger. We fail closed instead of risking another portfolio's position.
    """
    clean = {str(s).upper().strip() for s in (symbols or []) if str(s).strip()}
    if not clean:
        return []
    rows = db.query(PortfolioCoin.symbol).join(Portfolio).filter(
        PortfolioCoin.symbol.in_(clean),
        PortfolioCoin.portfolio_id != portfolio_id,
        Portfolio.telegram_id == telegram_id,
        Portfolio.status == "active",
        Portfolio.is_running == True,
    ).all()
    return sorted({str(r[0]).upper() for r in rows})


def get_reentry_candidates(db, portfolio_id: int, telegram_id: int = None):
    q = db.query(PortfolioTrade).filter(
        PortfolioTrade.portfolio_id == portfolio_id,
        PortfolioTrade.event_type == "stop_loss",
        PortfolioTrade.reentry_available == True,
        PortfolioTrade.reentry_used == False,
    )
    if telegram_id:
        q = q.filter(PortfolioTrade.telegram_id == telegram_id)
    return q.order_by(PortfolioTrade.created_at.desc()).all()


def mark_reentry_events_used(db, portfolio_id: int, symbol: str):
    """Consume outstanding manual stop-loss re-entry offers for a coin."""
    events = db.query(PortfolioTrade).filter(
        PortfolioTrade.portfolio_id == portfolio_id,
        PortfolioTrade.symbol == symbol.upper(),
        PortfolioTrade.event_type == "stop_loss",
        PortfolioTrade.reentry_available == True,
        PortfolioTrade.reentry_used == False,
    ).all()
    for ev in events:
        ev.reentry_available = False
        ev.reentry_used = True
    db.commit()
    return len(events)


def get_portfolio_trade_events(db, portfolio_id: int, telegram_id: int = None):
    q = db.query(PortfolioTrade).filter(PortfolioTrade.portfolio_id == portfolio_id)
    if telegram_id:
        q = q.filter(PortfolioTrade.telegram_id == telegram_id)
    return q.order_by(PortfolioTrade.created_at.desc()).all()


def get_open_positions(db, telegram_id: int = None):
    """Return all coins with open positions (for the monitor job)."""
    q = db.query(PortfolioCoin).join(Portfolio).filter(
        PortfolioCoin.position_status.in_(["open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit", "waiting_reentry"]),
        Portfolio.is_running == True,
        Portfolio.status == "active",
    )
    if telegram_id:
        q = q.filter(Portfolio.telegram_id == telegram_id)
    return q.all()


def log_action(db, telegram_id: int, action: str, details: str, success: bool = True, portfolio_id: int = None):
    log = RebalanceLog(
        telegram_id=telegram_id,
        portfolio_id=portfolio_id,
        action=action,
        details=details,
        success=success
    )
    db.add(log)
    db.commit()
