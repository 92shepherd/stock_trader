"""SQLAlchemy ORM models.

NOTE: Hypertables (daily_prices, minute_prices) exist as normal tables
from SQLAlchemy's perspective. TimescaleDB-specific features (compression,
continuous aggregates) are applied via raw SQL in migrations.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Ticker(Base):
    __tablename__ = "tickers"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    market: Mapped[str] = mapped_column(String(10))
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisted: Mapped[bool] = mapped_column(Boolean, default=False)
    delisted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Ticker {self.symbol} {self.name} ({self.market})>"


class DailyPrice(Base):
    __tablename__ = "daily_prices"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    market_cap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    institution_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    individual_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    per: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    dividend_yield: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)


class MinutePrice(Base):
    __tablename__ = "minute_prices"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DailyPriceWithName(Base):
    """Read-only mapping for the v_daily_prices view.

    Not a real table. Managed by migrations/005_daily_prices_view.sql.
    Use this ONLY for SELECTs where you want ticker name/sector alongside
    the price row. Writes go through DailyPrice / upsert_daily_prices.

    The composite (symbol, date) is still the logical primary key even
    though a view has no real PK — SQLAlchemy requires at least one
    primary_key=True column to map a class.
    """
    __tablename__ = "v_daily_prices"
    __table_args__ = {"info": {"is_view": True}}

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    market: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    market_cap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    institution_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    individual_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    per: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    dividend_yield: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)


class TickerUS(Base):
    """US stock ticker master.

    Separate from `tickers` (Korean) because the symbol space is
    different (alphanumeric, up to 8 chars with dots/hyphens vs Korea's
    6-digit numeric), the exchanges are different, and the security
    type taxonomy (ETF/ADR/preferred/warrant/unit) is US-specific.
    """
    __tablename__ = "tickers_us"

    symbol: Mapped[str] = mapped_column(String(15), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    exchange: Mapped[str] = mapped_column(String(10))
    security_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_etf: Mapped[bool] = mapped_column(Boolean, default=False)
    test_issue: Mapped[bool] = mapped_column(Boolean, default=False)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisted: Mapped[bool] = mapped_column(Boolean, default=False)
    delisted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<TickerUS {self.symbol} {self.name} ({self.exchange})>"


class DailyPriceUS(Base):
    """US daily prices from yfinance.

    Differences from DailyPrice (Korea):
      - NUMERIC(14,4) instead of (14,2): US penny stocks / ETFs need
        more decimal places.
      - adj_close: yfinance's split/dividend-adjusted close. Use this
        for backtesting; use `close` for raw historical price.
      - dividend / split_ratio columns: yfinance returns these per-day
        on the same call as OHLCV.
      - No market_cap / per / pbr / investor_flows: yfinance's
        `download()` doesn't include them. Fetch via fast_info or a
        separate collector if needed.
    """
    __tablename__ = "daily_prices_us"

    symbol: Mapped[str] = mapped_column(String(15), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    adj_close: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dividend: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), default=0)
    split_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), default=0)
    source: Mapped[str] = mapped_column(String(20), default="yfinance")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CollectionLog(Base):
    __tablename__ = "collection_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collector: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
