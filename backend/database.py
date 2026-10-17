from datetime import datetime
from typing import List, Optional
from sqlalchemy import create_engine, String, Float, Integer, ForeignKey, DateTime, Boolean, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

# SQLite local file database (Change this single URL to postgresql://... later for prod)
DATABASE_URL = "sqlite:///./ipotrack.db"

engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False, "timeout": 30}, # SQLite + FastAPI
    echo=False # Set to True if you want to see the raw SQL queries printed in terminal!
)


if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _enable_sqlite_wal(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
        finally:
            cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

# 1. Parent Table: Core IPO Metadata
class IPO(Base):
    __tablename__ = "ipos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(50), default="Mainboard") # Mainboard IPOs only
    price_band_high: Mapped[float] = mapped_column(Float)
    lot_size: Mapped[int] = mapped_column(Integer)
    open_date: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    close_date: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    listing_date: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    listing_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    listing_gain_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    registrar_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    registrar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source_status: Mapped[str] = mapped_column(String(50), default="Active", nullable=False)
    ipoji_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # One-to-Many Relationships (Cascade delete: if IPO is deleted, its history is deleted)
    gmp_logs: Mapped[List["GMPHistory"]] = relationship(
        back_populates="ipo", cascade="all, delete-orphan", order_by="GMPHistory.recorded_at"
    )
    sub_logs: Mapped[List["SubscriptionSnapshot"]] = relationship(
        back_populates="ipo", cascade="all, delete-orphan", order_by="SubscriptionSnapshot.recorded_at"
    )
    listing_logs: Mapped[List["ListingPerformanceSnapshot"]] = relationship(
        back_populates="ipo", cascade="all, delete-orphan", order_by="ListingPerformanceSnapshot.recorded_at"
    )

# 2. Child Table: Time-Series GMP Tracking
class GMPHistory(Base):
    __tablename__ = "gmp_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    gmp_price: Mapped[float] = mapped_column(Float)
    estimated_listing_price: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(20), default="refresh", nullable=False)

    ipo: Mapped["IPO"] = relationship(back_populates="gmp_logs")

# 3. Child Table: Time-Series Subscription Tracking
class SubscriptionSnapshot(Base):
    __tablename__ = "subscription_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    qib_times: Mapped[float] = mapped_column(Float, default=0.0)
    nii_times: Mapped[float] = mapped_column(Float, default=0.0)
    retail_times: Mapped[float] = mapped_column(Float, default=0.0)
    total_times: Mapped[float] = mapped_column(Float, default=0.0)
    retail_is_proxy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    category_split_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_subscription_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ipo: Mapped["IPO"] = relationship(back_populates="sub_logs")

# 4. Listing results from IPOJi, retained as observations for audit/history.
class ListingPerformanceSnapshot(Base):
    __tablename__ = "listing_performance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    listing_date: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    listing_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    listing_gain_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ipo: Mapped["IPO"] = relationship(back_populates="listing_logs")


class SyncStatus(Base):
    """Singleton row tracking the most recent live-data sync attempt."""

    __tablename__ = "sync_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    status: Mapped[str] = mapped_column(String(20), default="cached", nullable=False)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

def init_db():
    """Creates all tables in the database if they don't exist yet."""
    Base.metadata.create_all(bind=engine)
    # create_all does not add columns to an existing SQLite database.
    columns = {column["name"] for column in inspect(engine).get_columns("ipos")}
    if "source_status" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE ipos ADD COLUMN source_status VARCHAR(50) NOT NULL DEFAULT 'Active'"))
    if "listing_date" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE ipos ADD COLUMN listing_date VARCHAR(50)"))
    if "listing_price" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE ipos ADD COLUMN listing_price FLOAT"))
    if "listing_gain_pct" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE ipos ADD COLUMN listing_gain_pct FLOAT"))
    if "ipoji_url" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE ipos ADD COLUMN ipoji_url VARCHAR(500)"))
    if "registrar_url" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE ipos ADD COLUMN registrar_url VARCHAR(500)"))
            connection.execute(text(
                "UPDATE ipos SET registrar_name = NULL "
                "WHERE registrar_url IS NULL "
                "AND LOWER(TRIM(COALESCE(registrar_name, ''))) "
                "IN ('kfin / link intime', 'link intime')"
            ))

    gmp_history_columns = {
        column["name"] for column in inspect(engine).get_columns("gmp_history")
    }
    if "source" not in gmp_history_columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE gmp_history "
                "ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'refresh'"
            ))

    subscription_columns = {
        column["name"] for column in inspect(engine).get_columns("subscription_snapshots")
    }
    if "retail_is_proxy" not in subscription_columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE subscription_snapshots "
                "ADD COLUMN retail_is_proxy BOOLEAN NOT NULL DEFAULT 0"
            ))
    if "category_split_available" not in subscription_columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE subscription_snapshots "
                "ADD COLUMN category_split_available BOOLEAN NOT NULL DEFAULT 0"
            ))
    if "total_subscription_available" not in subscription_columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE subscription_snapshots "
                "ADD COLUMN total_subscription_available BOOLEAN NOT NULL DEFAULT 0"
            ))

    # Clear far-future placeholder dates from previously stored rows.
    with engine.begin() as connection:
        for column in ("open_date", "close_date", "listing_date"):
            connection.execute(text(
                f"UPDATE ipos SET {column} = NULL "
                f"WHERE (TRIM({column}) LIKE '20%' "
                f"AND CAST(SUBSTR(TRIM({column}), 1, 4) AS INTEGER) >= 2030) "
                f"OR UPPER(TRIM({column})) = 'TBA'"
            ))

    # Remove previously stored SME IPOs and all their time-series rows.
    # The scraper and sync pipeline now accept Mainboard IPOs only.
    with SessionLocal() as db:
        if db.get(SyncStatus, 1) is None:
            db.add(SyncStatus(id=1, status="cached"))
            db.commit()

        sme_ids = [
            ipo_id
            for (ipo_id,) in db.query(IPO.id).filter(IPO.category.ilike("%sme%")).all()
        ]
        if sme_ids:
            for history_model in (GMPHistory, SubscriptionSnapshot, ListingPerformanceSnapshot):
                db.query(history_model).filter(
                    history_model.ipo_id.in_(sme_ids)
                ).delete(synchronize_session=False)
            db.query(IPO).filter(IPO.id.in_(sme_ids)).delete(synchronize_session=False)
            db.commit()
            print(f"[Database] Removed {len(sme_ids)} stored SME IPOs and their history")
