-- =========================================
-- 00X: <FEATURE_DESCRIPTION>
-- =========================================
--
-- TEMPLATE NOTES (delete before applying):
--   - Replace 00X with next number after scanning migrations/ directory.
--   - Replace <FEATURE_DESCRIPTION>, <table>, column names, types.
--   - Korean comments are encouraged (matches existing 001_init_schema.sql style).
--   - If extending an existing table instead of creating a new one,
--     use ADD COLUMN IF NOT EXISTS — see "Extending existing table" block below.
--   - Hypertable conversion is OPTIONAL — only for time-series tables
--     where the time column is the natural query axis. Master data
--     tables (like `tickers`) are NOT hypertables.

-- 주 테이블 ----------------------------------------------------
CREATE TABLE IF NOT EXISTS <table> (
    -- PK 컬럼들 (시계열이면 보통 (symbol, time_col) 형태)
    symbol          VARCHAR(10)  NOT NULL,
    <time_col>      <DATE/TIMESTAMPTZ>  NOT NULL,

    -- 데이터 컬럼들 (NULL 허용 — 다른 collector가 채울 수도 있음)
    <col1>          NUMERIC(14,2),
    <col2>          BIGINT,
    <col3>          VARCHAR(50),

    -- 메타데이터
    source          VARCHAR(20),         -- 'naver' / 'kis' / 'dart' 등
    raw_data        JSONB,               -- 원본 응답 보존 (선택)
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (symbol, <time_col>)
);

-- 인덱스 -------------------------------------------------------
-- 종목별 최근 조회 최적화 (가장 흔한 쿼리 패턴)
CREATE INDEX IF NOT EXISTS idx_<table>_symbol_<time_col>
    ON <table>(symbol, <time_col> DESC);

-- 추가 필터 컬럼 인덱스 (필요한 경우)
-- CREATE INDEX IF NOT EXISTS idx_<table>_<col>
--     ON <table>(<col>) WHERE <col> IS NOT NULL;

-- TimescaleDB hypertable 전환 (시계열인 경우만) -------------------
-- chunk_time_interval 가이드:
--   daily 단위    → '1 month'
--   minute 단위   → '7 days'
--   quarterly 단위 → '1 year'
SELECT create_hypertable(
    '<table>', '<time_col>',
    chunk_time_interval => INTERVAL '<1 month>',
    if_not_exists => TRUE
);

-- =========================================
-- 옵션: 기존 테이블 확장 (대신 사용)
-- =========================================
-- 새 collector가 기존 테이블에 컬럼만 추가하는 경우:
--
-- ALTER TABLE daily_prices
--     ADD COLUMN IF NOT EXISTS <new_col1> NUMERIC(14,2),
--     ADD COLUMN IF NOT EXISTS <new_col2> BIGINT;
--
-- 컬럼 추가 후, repositories.py의 DAILY_COLUMNS 리스트에도 추가 필요.
