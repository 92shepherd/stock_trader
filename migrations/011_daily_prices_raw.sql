-- =========================================
-- 011: daily_prices_raw — 원주가(unadjusted) 일봉
-- =========================================
--
-- Why a separate table (not just _raw columns on daily_prices)?
--   메모리상 원칙: "Never denormalize daily_prices — keep it normalized;
--   use views for joins to preserve TimescaleDB compression policies."
--   같은 테이블에 _adj/_raw 8개 컬럼을 추가하면 컬럼이 24개로 늘고
--   기존 압축 segmentby/orderby 정책에 영향을 줄 수 있어서 분리 운영함.
--
-- Population:
--   src/collectors/daily_kis.py 가 KIS API
--   /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice
--   를 두 번 호출 (FID_ORG_ADJ_PRC=0 수정주가 → daily_prices,
--                  FID_ORG_ADJ_PRC=1 원주가     → daily_prices_raw).
--
-- Columns intentionally OMITTED (vs daily_prices):
--   market_cap, shares, foreign_net, institution_net, individual_net,
--   per, pbr, dividend_yield
--   이 값들은 가격 조정 여부와 무관 — daily_prices에만 저장.
--   v_daily_prices 같은 통합 뷰에서 daily_prices_raw와 join할 때
--   raw 가격은 raw에서, 펀더멘털은 daily_prices에서 가져옴.

CREATE TABLE IF NOT EXISTS daily_prices_raw (
    symbol      VARCHAR(10) NOT NULL,
    date        DATE        NOT NULL,
    open        NUMERIC(14,2),
    high        NUMERIC(14,2),
    low         NUMERIC(14,2),
    close       NUMERIC(14,2),
    volume      BIGINT,
    value       BIGINT,                          -- 거래대금
    PRIMARY KEY (symbol, date)
);

-- Hypertable + 종목별 최근조회 인덱스 (daily_prices와 동일 정책)
SELECT create_hypertable(
    'daily_prices_raw', 'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_daily_raw_symbol_date
    ON daily_prices_raw(symbol, date DESC);

-- 압축 정책 (3개월 지난 데이터 자동 압축)
ALTER TABLE daily_prices_raw SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'date DESC'
);

DO $$
BEGIN
    PERFORM add_compression_policy('daily_prices_raw', INTERVAL '3 months');
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

COMMENT ON TABLE daily_prices_raw IS
    '원주가(unadjusted) OHLCV. KIS API FID_ORG_ADJ_PRC=1로 수집. '
    '액면분할/액면병합 등 권리 발생 이전의 raw 가격을 보존. '
    '시총/PER/PBR/투자자흐름 등 fundamentals는 daily_prices에만 저장.';

-- =========================================
-- v_daily_prices_full — 수정주가 + 원주가 + 종목명 통합 view
-- =========================================
-- daily_prices(수정주가 + fundamentals) + daily_prices_raw(원주가) +
-- tickers(종목명) 을 한번에 조회. 분석/디버깅 편의용.
--
-- 주의: 양쪽 테이블 모두에 (symbol, date) 행이 있어야 한다는 보장이 없음.
-- KIS 호출 한 쪽이 실패하면 한쪽만 채워질 수 있음. 따라서 LEFT JOIN으로
-- 누락된 raw는 NULL로 두고, 분석 코드에서 raw_close IS NOT NULL 체크 권장.

CREATE OR REPLACE VIEW v_daily_prices_full AS
SELECT
    d.symbol,
    t.name,
    t.market,
    t.sector,
    t.industry,
    d.date,
    -- 수정주가 (adjusted)
    d.open               AS open_adj,
    d.high               AS high_adj,
    d.low                AS low_adj,
    d.close              AS close_adj,
    d.volume,
    d.value,
    -- 원주가 (raw / unadjusted)
    r.open               AS open_raw,
    r.high               AS high_raw,
    r.low                AS low_raw,
    r.close              AS close_raw,
    -- 펀더멘털 (가격 조정 여부 무관, 한 곳에만 저장)
    d.market_cap,
    d.shares,
    d.foreign_net,
    d.institution_net,
    d.individual_net,
    d.per,
    d.pbr,
    d.dividend_yield
FROM daily_prices d
LEFT JOIN daily_prices_raw r
       ON r.symbol = d.symbol AND r.date = d.date
LEFT JOIN tickers t
       ON t.symbol = d.symbol;

COMMENT ON VIEW v_daily_prices_full IS
    'daily_prices(수정주가+fundamentals) + daily_prices_raw(원주가) + '
    'tickers(종목명) 통합 뷰. 차트분석은 close_adj, 호가/주문 검증은 close_raw.';
