-- =========================================
-- 003: Compression policies
-- =========================================

-- 일봉: 3개월 지난 데이터 자동 압축
ALTER TABLE daily_prices SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'date DESC'
);

-- 정책이 이미 있으면 에러 없이 넘어가도록
DO $$
BEGIN
    PERFORM add_compression_policy('daily_prices', INTERVAL '3 months');
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

-- 분봉: 7일 지난 데이터 자동 압축
ALTER TABLE minute_prices SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'ts DESC'
);

DO $$
BEGIN
    PERFORM add_compression_policy('minute_prices', INTERVAL '7 days');
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
