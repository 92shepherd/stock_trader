-- =========================================
-- 002: Convert to TimescaleDB hypertables
-- =========================================

-- 일봉: 1개월 청크
SELECT create_hypertable(
    'daily_prices', 'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- 분봉: 1주일 청크 (데이터 양이 많으므로 작게)
SELECT create_hypertable(
    'minute_prices', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- 종목별 최근 조회 최적화 인덱스
CREATE INDEX IF NOT EXISTS idx_daily_symbol_date
    ON daily_prices(symbol, date DESC);
CREATE INDEX IF NOT EXISTS idx_minute_symbol_ts
    ON minute_prices(symbol, ts DESC);
