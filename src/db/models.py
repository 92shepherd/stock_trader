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
