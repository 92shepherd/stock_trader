-- =========================================
-- 006: US market tables (tickers_us, daily_prices_us)
-- =========================================
-- 미국 주식 데이터를 한국 주식과 완전히 분리하여 저장합니다.
-- 분리 이유:
--   1. 통화가 다름 (KRW vs USD) → market_cap 같은 BIGINT 컬럼 의미가 다름
--   2. 거래시간이 다름 (KST vs ET) → 동일 date 비교 시 혼란
--   3. 세션 구분이 다름 (한국은 정규장만, 미국은 pre/regular/post)
--   4. PER/PBR/거래대금 계산 단위·관행이 다름
-- 한국 daily_prices와 컬럼 셋이 비슷해 보여도 의미가 달라 별도 테이블이 안전.

-- 미국 종목 마스터 ----------------------------------------------
-- Source: NASDAQ Trader FTP의 nasdaqtraded.txt + otherlisted.txt
-- 참고: 미국 티커는 보통 1~5자, BRK.B/BRK-B 같은 점/하이픈 표기 포함 가능
--       → VARCHAR(15)로 여유 확보 (한국은 6자리 숫자라 VARCHAR(10)이었음)
CREATE TABLE IF NOT EXISTS tickers_us (
    symbol          VARCHAR(15) PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    exchange        VARCHAR(10)  NOT NULL,       -- NASDAQ / NYSE / AMEX / NYSEARCA / BATS
    security_type   VARCHAR(20),                 -- COMMON / ETF / ADR / PREFERRED / WARRANT / UNIT 등
    is_etf          BOOLEAN      DEFAULT FALSE,  -- NASDAQ Trader 파일의 ETF 플래그
    test_issue      BOOLEAN      DEFAULT FALSE,  -- 테스트용 종목 (조회 제외 권장)
    listing_date    DATE,                        -- yfinance fast_info에서 사후 채움 (선택)
    delisted        BOOLEAN      DEFAULT FALSE,
    delisted_date   DATE,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tickers_us_exchange
    ON tickers_us(exchange) WHERE delisted = FALSE;
CREATE INDEX IF NOT EXISTS idx_tickers_us_type
    ON tickers_us(security_type) WHERE delisted = FALSE;
CREATE INDEX IF NOT EXISTS idx_tickers_us_name
    ON tickers_us(name);

-- 미국 일봉 ----------------------------------------------------
-- yfinance가 제공하는 컬럼:
--   Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits
-- 우리가 저장하는 컬럼:
--   - close: yfinance의 unadjusted Close (raw)
--   - adj_close: yfinance의 Adj Close (수정주가, 분할/배당 반영)
--   - volume: 거래량 (주)
--   - dividend: 그날 지급된 배당금 (USD/share, 보통 0)
--   - split_ratio: 그날 발생한 분할 비율 (보통 0, 1:2면 2.0)
-- 시가총액·PER 등은 일봉 시계열에 직접 안 받음 (yfinance fast_info는 별도 API 호출).
CREATE TABLE IF NOT EXISTS daily_prices_us (
    symbol          VARCHAR(15)  NOT NULL,
    date            DATE         NOT NULL,
    open            NUMERIC(14,4),               -- 미국 주식은 소수점 2자리 이상도 흔함 (저가주, ETF)
    high            NUMERIC(14,4),
    low             NUMERIC(14,4),
    close           NUMERIC(14,4),
    adj_close       NUMERIC(14,4),               -- 분할/배당 조정된 수정종가
    volume          BIGINT,
    dividend        NUMERIC(12,6) DEFAULT 0,     -- per share, USD
    split_ratio     NUMERIC(12,6) DEFAULT 0,     -- 0 = no split that day
    source          VARCHAR(20)   DEFAULT 'yfinance',
    created_at      TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (symbol, date)
);

-- Hypertable 변환 (한국 daily_prices와 동일하게 1개월 청크)
SELECT create_hypertable(
    'daily_prices_us', 'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- 종목별 최근 조회 최적화 인덱스
CREATE INDEX IF NOT EXISTS idx_daily_us_symbol_date
    ON daily_prices_us(symbol, date DESC);

-- 압축 정책: 3개월 지난 데이터 자동 압축 (한국 daily_prices와 동일 기준)
ALTER TABLE daily_prices_us SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'date DESC'
);

DO $$
BEGIN
    PERFORM add_compression_policy('daily_prices_us', INTERVAL '3 months');
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

-- 조회 편의 뷰: 일봉 + 종목명/거래소 ------------------------------
-- 한국의 v_daily_prices와 같은 패턴. delisted 필터는 일부러 안 넣음
-- (과거 데이터를 조회할 때 상폐 종목도 보여야 정상).
CREATE OR REPLACE VIEW v_daily_prices_us AS
SELECT
    d.symbol,
    t.name,
    t.exchange,
    t.security_type,
    t.is_etf,
    d.date,
    d.open,
    d.high,
    d.low,
    d.close,
    d.adj_close,
    d.volume,
    d.dividend,
    d.split_ratio
FROM daily_prices_us d
LEFT JOIN tickers_us t ON t.symbol = d.symbol;

COMMENT ON VIEW v_daily_prices_us IS
    'daily_prices_us + tickers_us JOIN view. 조회용 편의 뷰.';
