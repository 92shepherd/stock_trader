-- =========================================
-- 004: Continuous aggregates (auto-computed rollups)
-- =========================================
-- 분봉에서 5분봉/시간봉/일봉을 자동 집계합니다.
-- 분봉이 들어와야 의미 있으므로, 분봉 수집 시작 후 활용하세요.

-- 5분봉 ----------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS minute5_prices
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '5 minutes', ts) AS bucket,
    first(open, ts) AS open,
    max(high)       AS high,
    min(low)        AS low,
    last(close, ts) AS close,
    sum(volume)     AS volume,
    sum(value)      AS value
FROM minute_prices
GROUP BY symbol, bucket
WITH NO DATA;

DO $$
BEGIN
    PERFORM add_continuous_aggregate_policy('minute5_prices',
        start_offset      => INTERVAL '2 days',
        end_offset        => INTERVAL '10 minutes',
        schedule_interval => INTERVAL '30 minutes');
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

-- 시간봉 ---------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_prices
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    first(open, ts) AS open,
    max(high)       AS high,
    min(low)        AS low,
    last(close, ts) AS close,
    sum(volume)     AS volume,
    sum(value)      AS value
FROM minute_prices
GROUP BY symbol, bucket
WITH NO DATA;

DO $$
BEGIN
    PERFORM add_continuous_aggregate_policy('hourly_prices',
        start_offset      => INTERVAL '3 days',
        end_offset        => INTERVAL '1 hour',
        schedule_interval => INTERVAL '1 hour');
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
