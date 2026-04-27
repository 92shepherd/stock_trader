-- =========================================
-- 005: v_daily_prices - daily prices joined with ticker names
-- =========================================
-- daily_prices는 정규화를 유지하되, 조회 편의를 위해 tickers와 조인한 뷰를 제공합니다.
--   - 종목명이 나중에 바뀌어도 자동 반영 (상장사 사명 변경 대응)
--   - daily_prices 테이블의 저장/압축에는 영향 없음
--   - TimescaleDB 하이퍼테이블의 파티셔닝/압축 정책을 건드리지 않으므로 안전
--
-- 사용 예:
--   SELECT date, name, close FROM v_daily_prices
--   WHERE symbol = '005930' ORDER BY date DESC LIMIT 10;
--
-- 참고: 일반 VIEW라서 쿼리 시마다 조인이 실행됩니다. tickers는 수천 행 규모이고
-- symbol PK 인덱스가 있으므로 부하는 무시 가능합니다. 만약 대량 집계에서 성능
-- 문제가 되면 MATERIALIZED VIEW로 교체 고려.

CREATE OR REPLACE VIEW v_daily_prices AS
SELECT
    d.symbol,
    t.name,
    t.market,
    t.sector,
    t.industry,
    d.date,
    d.open,
    d.high,
    d.low,
    d.close,
    d.volume,
    d.value,
    d.market_cap,
    d.shares,
    d.foreign_net,
    d.institution_net,
    d.individual_net,
    d.per,
    d.pbr,
    d.dividend_yield
FROM daily_prices d
LEFT JOIN tickers t ON t.symbol = d.symbol;

COMMENT ON VIEW v_daily_prices IS
    'daily_prices + tickers JOIN view. 조회용 편의 뷰. '
    '쓰기는 daily_prices에 직접 수행하세요.';
