-- =========================================
-- 012: 컨센서스 (FnGuide) — 종목별 분기/연간 추정치 스냅샷
-- =========================================
--
-- 출처: comp.fnguide.com 컨센서스 페이지 (개인용 비공개 사용 한정)
-- 수집기: src/collectors/consensus_fnguide.py
--
-- 데이터 모델:
--   - 매일 1회 (장 마감 후) 전 종목 스냅샷
--   - 종목 × 예측기간(FQ1~4 + FY1~2) × as_of_date 의 3차원 시계열
--   - 같은 (종목, 예측기간)에 대해 매일 새 row 가 쌓이며, 추정치 변경
--     (estimate revision) 을 시계열로 추적할 수 있다
--
-- 시간축:
--   - hypertable 의 time column 은 as_of_date (스냅샷 시점)
--   - fiscal_period 는 예측 대상 기간을 문자열로 표현
--     예: '2026Q1', '2026Q2', '2026Q3', '2026Q4', 'FY2026', 'FY2027'

-- 메인 테이블 -------------------------------------------------
CREATE TABLE IF NOT EXISTS consensus_estimates (
    symbol              VARCHAR(10)  NOT NULL,
    as_of_date          DATE         NOT NULL,
    fiscal_period       VARCHAR(10)  NOT NULL,
    fiscal_period_type  VARCHAR(10)  NOT NULL,

    -- 재무 추정치 평균
    eps_estimate        NUMERIC(14, 2),
    revenue_estimate    NUMERIC(20, 0),
    op_income_estimate  NUMERIC(20, 0),
    net_income_estimate NUMERIC(20, 0),

    -- 목표주가 / 투자의견
    target_price        NUMERIC(14, 2),
    opinion             VARCHAR(20),

    -- 컨센서스 메타데이터
    n_estimates         SMALLINT,

    -- 수집 메타
    source              VARCHAR(20)  NOT NULL DEFAULT 'fnguide',
    raw_data            JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (symbol, as_of_date, fiscal_period)
);

-- 컬럼 코멘트 -------------------------------------------------
COMMENT ON TABLE  consensus_estimates IS
    '애널리스트 컨센서스 추정치 일별 스냅샷 (FnGuide 출처). 매일 같은 (symbol, fiscal_period) 에 대해 새 row 가 추가됨';
COMMENT ON COLUMN consensus_estimates.symbol IS
    '6자리 KRX 종목코드 (tickers.symbol 참조)';
COMMENT ON COLUMN consensus_estimates.as_of_date IS
    '스냅샷을 수집한 날짜. hypertable 의 시간축. 같은 fiscal_period 에 대해 시계열로 누적';
COMMENT ON COLUMN consensus_estimates.fiscal_period IS
    '추정 대상 기간 문자열. 분기: ''YYYYQn'' (예: ''2026Q3''), 연간: ''FYYYYY'' (예: ''FY2026'')';
COMMENT ON COLUMN consensus_estimates.fiscal_period_type IS
    '예측 기간 타입. ''quarterly'' 또는 ''annual''. 분석시 필터링용 (인덱싱된 컬럼)';
COMMENT ON COLUMN consensus_estimates.eps_estimate IS
    '주당순이익 (EPS) 컨센서스 평균 추정치. 단위: 원';
COMMENT ON COLUMN consensus_estimates.revenue_estimate IS
    '매출액 컨센서스 평균 추정치. 단위: 원 (정수). FnGuide 가 ''억원'' 단위로 노출하더라도 여기서는 원 단위로 환산해 저장';
COMMENT ON COLUMN consensus_estimates.op_income_estimate IS
    '영업이익 컨센서스 평균 추정치. 단위: 원 (정수)';
COMMENT ON COLUMN consensus_estimates.net_income_estimate IS
    '순이익 컨센서스 평균 추정치. 단위: 원 (정수). FnGuide 페이지에 노출되지 않으면 NULL';
COMMENT ON COLUMN consensus_estimates.target_price IS
    '애널리스트 목표주가 평균. 단위: 원. 분기 행에서는 NULL 일 수 있고 보통 연간 행에서만 유효';
COMMENT ON COLUMN consensus_estimates.opinion IS
    '투자의견 평균/대표값 문자열. 예: ''매수'', ''Buy'', ''3.85/5.00''. 원본 표현을 그대로 저장하고 표준화는 view 에서 수행';
COMMENT ON COLUMN consensus_estimates.n_estimates IS
    '해당 (symbol, fiscal_period) 에 추정치를 제공한 애널리스트 수. dispersion 분석에 사용. FnGuide 가 노출하지 않을 경우 NULL';
COMMENT ON COLUMN consensus_estimates.source IS
    '데이터 출처 식별자. 현재는 항상 ''fnguide''. 향후 네이버/한경 등 추가 시 분기됨';
COMMENT ON COLUMN consensus_estimates.raw_data IS
    '원본 응답 보존 (JSONB). 디버깅 / 스키마 변경 시 재파싱용. 운영 정착 후 NULL 로 두는 정책 고려';
COMMENT ON COLUMN consensus_estimates.created_at IS
    'row 삽입 시각 (TIMESTAMPTZ). as_of_date 와 다른 점: 재실행/백필 시 더 늦게 들어올 수 있음';

-- 인덱스 -------------------------------------------------------
-- 1) 종목별 최신 컨센서스 조회 (가장 흔한 쿼리)
CREATE INDEX IF NOT EXISTS idx_consensus_symbol_asof
    ON consensus_estimates(symbol, as_of_date DESC);

-- 2) 예측기간 타입별 필터링 (분기 vs 연간 분리 분석)
CREATE INDEX IF NOT EXISTS idx_consensus_period_type
    ON consensus_estimates(fiscal_period_type, as_of_date DESC);

-- 3) 특정 fiscal_period 의 시계열 추적 (revision 모멘텀 분석)
CREATE INDEX IF NOT EXISTS idx_consensus_symbol_period
    ON consensus_estimates(symbol, fiscal_period, as_of_date DESC);

-- TimescaleDB hypertable 전환 ----------------------------------
-- as_of_date 가 시간축. 일 스냅샷이므로 chunk 는 1 개월.
SELECT create_hypertable(
    'consensus_estimates', 'as_of_date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- 조회 편의용 뷰 -----------------------------------------------
-- 종목 이름 join + 최신 스냅샷만 추출 (각 (symbol, fiscal_period) 당 가장 최근 as_of_date 1개)
CREATE OR REPLACE VIEW v_consensus_latest AS
SELECT
    c.symbol,
    t.name,
    t.market,
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
    '각 (symbol, fiscal_period) 의 가장 최근 스냅샷만 보여주는 편의 뷰. 종목명/시장 join 포함. revision 분석에는 raw 테이블 직접 조회 권장';
