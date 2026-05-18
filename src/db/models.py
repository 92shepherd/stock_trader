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
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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


class ConsensusEstimate(Base):
    """Analyst consensus estimates daily snapshot (FnGuide).

    Source: comp.fnguide.com SVD_Consensus page (개인용 비공개 한정)

    Data model:
        매일 1회 (장 마감 후) 전 종목 × (FQ1~4 + FY1~2) 스냅샷.
        같은 (symbol, fiscal_period) 에 대해 매일 새 row 가 누적되어
        추정치 변경 (estimate revision) 을 시계열로 추적.

    Primary key:
        (symbol, as_of_date, fiscal_period)
        - as_of_date 가 hypertable 의 시간축.
        - fiscal_period 스타일: 'YYYYQn' (분기) 또는 'FYYYYY' (연간).

    Managed by migrations/012_consensus_fnguide.sql
    """
    __tablename__ = "consensus_estimates"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fiscal_period: Mapped[str] = mapped_column(String(10), primary_key=True)
    fiscal_period_type: Mapped[str] = mapped_column(String(10))

    eps_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    revenue_estimate: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    op_income_estimate: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    net_income_estimate: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    target_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    opinion: Mapped[str | None] = mapped_column(String(20), nullable=True)
    n_estimates: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source: Mapped[str] = mapped_column(String(20), default="fnguide")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ConsensusEstimate {self.symbol} {self.as_of_date} "
            f"{self.fiscal_period} eps={self.eps_estimate}>"
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


# =========================================================
# Phase 1 평가 인프라 (migrations/014_research_infra.sql)
# =========================================================

class ForwardReturn(Base):
    """종목 x 거래일 x horizon 의 미래 수익률 캐시.

    Phase 1 평가 인프라의 핵심 테이블. cross-sectional ranking 평가에서
    매번 다시 계산하지 않도록 사전 캐싱.

    Managed by migrations/014_research_infra.sql (hypertable).
    """
    __tablename__ = "forward_returns"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    horizon_days: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    fwd_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    high_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    universe: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="daily_prices")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ForwardReturn {self.symbol} {self.date} "
            f"h={self.horizon_days} r={self.fwd_return}>"
        )


class FactorSignal(Base):
    """종목 x 거래일 x factor 의 신호값.

    한 (symbol, date, factor_name) 에 대해 raw / sector-neutralized /
    cross-sectional rank / z-score 의 4가지 표현을 한 row 에 저장.

    Managed by migrations/014_research_infra.sql (hypertable).
    """
    __tablename__ = "factor_signals"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    factor_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    raw_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    neutral_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    rank_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    z_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    universe: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<FactorSignal {self.factor_name} {self.symbol} {self.date} "
            f"raw={self.raw_value} rank={self.rank_value}>"
        )


class EvalRun(Base):
    """단일 팩터/모델 평가 실행의 메타데이터.

    Managed by migrations/014_research_infra.sql.
    """
    __tablename__ = "eval_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    factor_name: Mapped[str] = mapped_column(String(50))
    universe: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(SmallInteger)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    n_observations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<EvalRun {self.run_id} factor={self.factor_name} "
            f"univ={self.universe} h={self.horizon_days}>"
        )


class EvalMetric(Base):
    """eval_runs 1건에 대한 평가 지표들 (LONG 형식).

    Managed by migrations/014_research_infra.sql.
    """
    __tablename__ = "eval_metrics"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    metric_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<EvalMetric {self.run_id} {self.metric_name}={self.metric_value}>"
        )


# =========================================================
# Phase 2 모델 인프라 (migrations/015_model_runs.sql)
# =========================================================

class ModelRun(Base):
    """ML 모델 학습 1회의 메타데이터.

    walk-forward 학습은 N 개의 fold = N 개의 ModelRun.
    eval_runs/eval_metrics 와 별도 — 모델은 학습 1회, 평가는 N 회 가능.
    관계는 eval_runs.notes 에 model_run_id 를 적어 관리.

    Managed by migrations/015_model_runs.sql.
    """
    __tablename__ = "model_runs"

    model_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_type: Mapped[str] = mapped_column(String(30))
    universe: Mapped[str] = mapped_column(String(20))
    train_start: Mapped[date] = mapped_column(Date)
    train_end: Mapped[date] = mapped_column(Date)
    test_start: Mapped[date] = mapped_column(Date)
    test_end: Mapped[date] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(SmallInteger)
    target_type: Mapped[str] = mapped_column(String(20), default="regression")
    features: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    hyperparams: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    n_train_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_test_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    train_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    valid_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ModelRun {self.model_run_id} type={self.model_type} "
            f"test=[{self.test_start}..{self.test_end}]>"
        )


class ModelPrediction(Base):
    """종목 x 날짜 x model_run 의 예측값.

    Managed by migrations/015_model_runs.sql (hypertable).
    """
    __tablename__ = "model_predictions"

    model_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    horizon_days: Mapped[int] = mapped_column(SmallInteger)
    y_pred: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    y_true: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    rank_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    universe: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ModelPrediction {self.model_run_id} {self.symbol} {self.date} "
            f"pred={self.y_pred}>"
        )


class ModelFeatureImportance(Base):
    """피처 중요도 (LightGBM gain/split 등).

    Managed by migrations/015_model_runs.sql.
    """
    __tablename__ = "model_feature_importance"

    model_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    feature_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    importance_type: Mapped[str] = mapped_column(String(20), primary_key=True, default="gain")
    importance: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    rank: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ModelFeatureImportance {self.model_run_id} "
            f"{self.feature_name}={self.importance}>"
        )


# =========================================================
# EOD 봇 시뮬레이션 (migrations/016_eod_bots.sql)
# =========================================================

import uuid as _uuid_mod  # noqa: E402  (local import to avoid top-level pollution)


class EodBot(Base):
    """EOD 봇 마스터.

    봇 1대 = 1 row. KIS 매매 API 는 호출하지 않으며 모든 매매가 이 시스템 안에서
    가상으로 관리된다. state=STOPPED 가 되면 어떤 daily tick 도 거래를 만들지 않는다.

    Managed by migrations/016_eod_bots.sql.
    """
    __tablename__ = "eod_bots"

    bot_id: Mapped[_uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid_mod.uuid4,
    )
    name: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    strategy_kind: Mapped[str] = mapped_column(String(16))
    declarative_spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    plugin_strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    universe: Mapped[str] = mapped_column(String(20))
    seed_cash: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    holdings_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    total_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    last_tick_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_tick_run_id: Mapped[_uuid_mod.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    final_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    final_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    final_total_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<EodBot {self.name} state={self.state} "
            f"total={self.total_value} kind={self.strategy_kind}>"
        )


class EodBotSpecHistory(Base):
    """봇별 전략 spec 변경 이력 (append-only).

    봇이 생성될 때 1 row, 이후 PATCH /bots/{id}/spec 마다 1 row. 거래 row 가 어느
    spec 버전으로 결정됐는지 eod_bot_orders.spec_history_id 가 참조.

    Managed by migrations/016_eod_bots.sql.
    """
    __tablename__ = "eod_bot_spec_history"

    spec_history_id: Mapped[_uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid_mod.uuid4,
    )
    bot_id: Mapped[_uuid_mod.UUID] = mapped_column(UUID(as_uuid=True))
    spec_version: Mapped[int] = mapped_column(Integer)
    strategy_kind: Mapped[str] = mapped_column(String(16))
    spec_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    plugin_strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_factors: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_by: Mapped[str] = mapped_column(String(32), default="api")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<EodBotSpecHistory bot={self.bot_id} v{self.spec_version} "
            f"kind={self.strategy_kind}>"
        )


class EodBotOrder(Base):
    """EOD 봇의 가상 매수/매도 체결 기록.

    KIS 매매 API 는 호출되지 않으며 모든 매매가 여기에만 존재. 체결가는 daily_prices
    에서 조회한 시초가 또는 종가에 slippage 가 반영된 값.

    Managed by migrations/016_eod_bots.sql.
    """
    __tablename__ = "eod_bot_orders"

    order_id: Mapped[_uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid_mod.uuid4,
    )
    bot_id: Mapped[_uuid_mod.UUID] = mapped_column(UUID(as_uuid=True))
    spec_history_id: Mapped[_uuid_mod.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    run_id: Mapped[_uuid_mod.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(10))
    decision_date: Mapped[date] = mapped_column(Date)
    fill_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    fill_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    slippage_bps: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    composite_score: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    cash_before: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    cash_after: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    fill_source: Mapped[str] = mapped_column(String(20), default="daily_prices")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<EodBotOrder {self.bot_id} {self.side} {self.symbol} "
            f"q={self.quantity}@{self.fill_price} on {self.fill_date}>"
        )


class EodBotPosition(Base):
    """EOD 봇의 일별 포지션 스냅샷.

    (bot_id, date, symbol) 키. daily tick 끝에서 보유 중인 모든 종목을 1줄씩 저장.

    Managed by migrations/016_eod_bots.sql.
    """
    __tablename__ = "eod_bot_positions"

    bot_id: Mapped[_uuid_mod.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    weight_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    composite_score: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<EodBotPosition {self.bot_id} {self.date} {self.symbol} "
            f"q={self.quantity} mv={self.market_value}>"
        )


class EodBotDailyPnL(Base):
    """EOD 봇의 일별 PnL.

    Managed by migrations/016_eod_bots.sql.
    """
    __tablename__ = "eod_bot_daily_pnl"

    bot_id: Mapped[_uuid_mod.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    holdings_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    total_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    daily_return_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 8), nullable=True
    )
    cumulative_return_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 8), nullable=True
    )
    drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    peak_total_value: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    buy_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    sell_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    fee_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    turnover_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    n_positions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<EodBotDailyPnL {self.bot_id} {self.date} "
            f"total={self.total_value} cum={self.cumulative_return_pct}>"
        )


class EodBotRun(Base):
    """봇별 daily tick 실행 로그.

    (bot_id, decision_date) UNIQUE — 같은 날 두 번 tick 방지.

    Managed by migrations/016_eod_bots.sql.
    """
    __tablename__ = "eod_bot_runs"

    run_id: Mapped[_uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid_mod.uuid4,
    )
    bot_id: Mapped[_uuid_mod.UUID] = mapped_column(UUID(as_uuid=True))
    decision_date: Mapped[date] = mapped_column(Date)
    spec_history_id: Mapped[_uuid_mod.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    universe_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eligible_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scored_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    factor_coverage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    target_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_buys: Mapped[int] = mapped_column(Integer, default=0)
    n_sells: Mapped[int] = mapped_column(Integer, default=0)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<EodBotRun {self.bot_id} {self.decision_date} status={self.status}>"
        )
