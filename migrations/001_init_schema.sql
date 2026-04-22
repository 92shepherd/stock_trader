-- =========================================
-- 001: Base schema
-- =========================================
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 종목 마스터 --------------------------------------------------
CREATE TABLE IF NOT EXISTS tickers (
    symbol          VARCHAR(10) PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    market          VARCHAR(10)  NOT NULL,       -- KOSPI / KOSDAQ
    sector          VARCHAR(100),
    industry        VARCHAR(100),
    listing_date    DATE,
    delisted        BOOLEAN      DEFAULT FALSE,
    delisted_date   DATE,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tickers_market
    ON tickers(market) WHERE delisted = FALSE;
CREATE INDEX IF NOT EXISTS idx_tickers_name
    ON tickers(name);

-- 일봉 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_prices (
    symbol          VARCHAR(10)  NOT NULL,
    date            DATE         NOT NULL,
    open            NUMERIC(14,2),
    high            NUMERIC(14,2),
    low             NUMERIC(14,2),
    close           NUMERIC(14,2),
    volume          BIGINT,
    value           BIGINT,                      -- 거래대금
    market_cap      BIGINT,
    shares          BIGINT,
    foreign_net     BIGINT,
    institution_net BIGINT,
    individual_net  BIGINT,
    per             NUMERIC(10,2),
    pbr             NUMERIC(10,2),
    dividend_yield  NUMERIC(6,3),
    PRIMARY KEY (symbol, date)
);

-- 분봉 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS minute_prices (
    symbol          VARCHAR(10)  NOT NULL,
    ts              TIMESTAMPTZ  NOT NULL,
    open            NUMERIC(14,2),
    high            NUMERIC(14,2),
    low             NUMERIC(14,2),
    close           NUMERIC(14,2),
    volume          BIGINT,
    value           BIGINT,
    PRIMARY KEY (symbol, ts)
);

-- 수집 로그 ---------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_log (
    id              BIGSERIAL PRIMARY KEY,
    collector       VARCHAR(30) NOT NULL,        -- daily_pykrx, minute_kis
    symbol          VARCHAR(10),
    target_date     DATE NOT NULL,
    status          VARCHAR(20) NOT NULL,        -- success, failed, partial, skipped
    rows_inserted   INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_log_collector_date
    ON collection_log(collector, target_date DESC);
CREATE INDEX IF NOT EXISTS idx_log_status
    ON collection_log(status, created_at DESC)
    WHERE status <> 'success';
