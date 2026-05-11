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


class DailyPriceRaw(Base):
    """원주가(unadjusted) OHLCV.

    daily_prices(수정주가 + fundamentals)와 분리된 테이블. 액면분할/병합
    등 권리 발생 이전의 raw 가격을 보존. KIS API FID_ORG_ADJ_PRC=1로 수집.

    펀더멘털(시총/PER/PBR/투자자흐름)은 가격 조정 여부와 무관하므로
    여기에 두지 않고 daily_prices 한 곳에만 저장한다. 통합 조회는
    v_daily_prices_full 뷰 사용.

    Managed by migrations/011_daily_prices_raw.sql (hypertable + compression).
    """
    __tablename__ = "daily_prices_raw"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


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


class DartCorpCode(Base):
    """DART corp_code master (mapping between DART's 8-digit corp_code
    and our 6-digit stock_code, plus company name).

    Source: https://opendart.fss.or.kr/api/corpCode.xml (ZIP, ~100k rows)
    Most rows are non-listed companies (stock_code is NULL); we keep
    them all because DART references them in disclosure feeds.

    The corp_code is the entry point for almost every other DART API
    call, so this table is effectively a foreign-key dictionary.
    """
    __tablename__ = "dart_corp_codes"

    corp_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    corp_name: Mapped[str] = mapped_column(String(200))
    stock_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    modify_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<DartCorpCode {self.corp_code} {self.corp_name} stock={self.stock_code}>"


class DartDisclosure(Base):
    """DART disclosure list entry (Phase 1).

    One row per DART disclosure, keyed by `rcept_no` (14-digit receipt
    number — DART's globally unique identifier for any disclosure).

    Phase 1 collection policy:
      - Listed companies only (stock_code IS NOT NULL)
      - Major-event reports (kind='B') are the priority signal but all
        kinds are stored; filtering happens at query time

    Use the `v_dart_disclosures` view for joined reads with
    `tickers.name`.
    """
    __tablename__ = "dart_disclosures"

    rcept_no: Mapped[str] = mapped_column(String(14), primary_key=True)
    corp_code: Mapped[str] = mapped_column(String(8))
    corp_name: Mapped[str] = mapped_column(String(200))
    stock_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    corp_cls: Mapped[str | None] = mapped_column(String(1), nullable=True)
    report_nm: Mapped[str] = mapped_column(String(500))
    rcept_dt: Mapped[date] = mapped_column(Date)
    flr_nm: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rm: Mapped[str | None] = mapped_column(String(50), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(1), nullable=True)
    kind_detail: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<DartDisclosure {self.rcept_no} {self.corp_name} "
            f"{self.report_nm[:30]} ({self.rcept_dt})>"
        )


class DartFinancial(Base):
    """DART major-account financial statements (Phase 2).

    Source: fnlttSinglAcnt API — ~12 rows per (company, year, quarter,
    fs_div) call. Stored in LONG form (one row per account_nm) so that
    accounting-standard changes don't require schema migrations.

    Key design choices:
      - PK = (corp_code, bsns_year, reprt_code, fs_div, account_nm)
        — account_id can be NULL for some special accounts, so we use
        account_nm in the PK instead.
      - Amounts stored as NUMERIC(20,0) since DART returns integer KRW.
      - DART returns 3 years (current/prior/prior-prior) per call;
        we keep all three to enable trend calculation without extra
        round-trips.
      - Income-statement items are CUMULATIVE (Q1 = Q1, H1 = Q1+Q2,
        etc.). Quarterly stand-alone values must be derived at query
        time by subtracting prior cumulative.
    """
    __tablename__ = "dart_financials"

    corp_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    bsns_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    reprt_code: Mapped[str] = mapped_column(String(5), primary_key=True)
    fs_div: Mapped[str] = mapped_column(String(3), primary_key=True)
    account_nm: Mapped[str] = mapped_column(String(200), primary_key=True)

    sj_div: Mapped[str | None] = mapped_column(String(10), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    thstrm_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    frmtrm_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    bfefrmtrm_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    thstrm_add_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    currency: Mapped[str | None] = mapped_column(String(10), default="KRW")
    ord: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), default="dart_fnltt_single_acnt")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<DartFinancial {self.corp_code} {self.bsns_year}/{self.reprt_code} "
            f"{self.fs_div} {self.account_nm}={self.thstrm_amount}>"
        )


class DartIndicator(Base):
    """DART major financial indicators (Phase 2).

    Source: fnlttSinglIndx API — ~25 rows per (company, year, quarter,
    fs_div) call. DART pre-computes profitability/stability/growth/
    activity ratios so we don't have to.

    Categories (idx_cl_code):
      - M210000  Profitability (수익성): operating margin, ROA, ROE, ...
      - M220000  Stability     (안정성): debt ratio, current ratio, ...
      - M230000  Growth        (성장성): revenue growth rate, ...
      - M240000  Activity      (활동성): asset turnover, ...

    Values are stored as NUMERIC(15, 4) — ratios in % units, where
    actual range is roughly -1000 to +1000.
    """
    __tablename__ = "dart_indicators"

    corp_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    bsns_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    reprt_code: Mapped[str] = mapped_column(String(5), primary_key=True)
    fs_div: Mapped[str] = mapped_column(String(3), primary_key=True)
    idx_nm: Mapped[str] = mapped_column(String(200), primary_key=True)

    idx_cl_code: Mapped[str | None] = mapped_column(String(7), nullable=True)
    idx_cl_nm: Mapped[str | None] = mapped_column(String(50), nullable=True)
    idx_code: Mapped[str | None] = mapped_column(String(20), nullable=True)

    thstrm_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    frmtrm_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    bfefrmtrm_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)

    source: Mapped[str | None] = mapped_column(String(40), default="dart_fnltt_single_indx")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<DartIndicator {self.corp_code} {self.bsns_year}/{self.reprt_code} "
            f"{self.fs_div} {self.idx_nm}={self.thstrm_value}>"
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
