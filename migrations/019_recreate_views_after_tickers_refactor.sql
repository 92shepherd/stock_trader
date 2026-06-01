-- 019_recreate_views_after_tickers_refactor.sql
--
-- 018_tickers_dart_refactor.sql 이 tickers 테이블의 market/sector/industry
-- 컬럼을 제거함에 따라 해당 컬럼을 참조하던 세 뷰를 corp_cls/induty_code 기준으로
-- 재생성한다.
--
-- 대상 뷰:
--   v_daily_prices       (005에서 최초 생성)
--   v_daily_prices_full  (011에서 최초 생성)
--   v_consensus_latest   (012에서 최초 생성)
--
-- 컬럼 대체:
--   market   → corp_cls    (Y=KOSPI, K=KOSDAQ, N=KONEX, E=기타)
--   sector   → 제거        (induty_code 첫 2자리로 query-time 파생, ksic.py 참조)
--   industry → induty_code (KSIC 5자리 업종코드)

-- v_daily_prices: daily_prices + tickers (수정주가)
CREATE OR REPLACE VIEW v_daily_prices AS
SELECT
    d.symbol,
    t.name,
    t.corp_cls,
    t.induty_code,
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
    'market→corp_cls(Y/K/N/E), sector/industry→induty_code(KSIC) 로 교체됨(018/019). '
    '쓰기는 daily_prices 에 직접 수행.';

-- v_daily_prices_full: 수정주가 + 원주가 + tickers 통합
CREATE OR REPLACE VIEW v_daily_prices_full AS
SELECT
    d.symbol,
    t.name,
    t.corp_cls,
    t.induty_code,
    d.date,
    d.open               AS open_adj,
    d.high               AS high_adj,
    d.low                AS low_adj,
    d.close              AS close_adj,
    d.volume,
    d.value,
    r.open               AS open_raw,
    r.high               AS high_raw,
    r.low                AS low_raw,
    r.close              AS close_raw,
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
    'tickers(종목명) 통합 뷰. corp_cls/induty_code 컬럼 반영(018/019).';

-- v_consensus_latest: 컨센서스 최신 스냅샷 + tickers
CREATE OR REPLACE VIEW v_consensus_latest AS
SELECT
    c.symbol,
    t.name,
    t.corp_cls,
    c.as_of_date,
    c.fiscal_period,
    c.fiscal_period_type,
    c.eps_estimate,
    c.revenue_estimate,
    c.op_income_estimate,
    c.net_income_estimate,
    c.target_price,
    c.opinion,
    c.n_estimates,
    c.source
  FROM consensus_estimates c
  LEFT JOIN tickers t ON t.symbol = c.symbol
  WHERE (c.symbol, c.fiscal_period, c.as_of_date) IN (
      SELECT symbol, fiscal_period, MAX(as_of_date)
        FROM consensus_estimates
       GROUP BY symbol, fiscal_period
  );

COMMENT ON VIEW v_consensus_latest IS
    '각 (symbol, fiscal_period) 의 가장 최근 스냅샷만 보여주는 편의 뷰. '
    'corp_cls 컬럼 반영(018/019, market 컬럼 대체).';
