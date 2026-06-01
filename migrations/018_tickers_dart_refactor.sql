-- 018_tickers_dart_refactor.sql
--
-- pykrx/FDR 의존성 폐기에 따른 tickers 테이블 스키마 재정의.
-- 새 마스터 소스: DART /api/company.json (회사개황) 으로 일원화.
--
-- 변경:
--   유지:  symbol(PK), name, delisted, delisted_date, created_at, updated_at
--   삭제:  market, sector, industry, listing_date
--          - market         → corp_cls 로 대체 (Y=KOSPI, K=KOSDAQ, N=KONEX, E=기타)
--          - sector         → induty_code 의 대분류로 query-time 파생
--                             (src/utils/ksic.py 참조). 컬럼으로 저장하지 않음.
--          - industry       → induty_code (KSIC 5자리) 로 대체
--          - listing_date   → DART 가 제공하지 않음. daily_prices.MIN(date) 로
--                             사후 파생 가능. 컬럼은 제거.
--   추가:  corp_code(VARCHAR(8))   — DART 8자리. dart_corp_codes 와 조인 키.
--          corp_cls(CHAR(1))       — 시장 구분. Y/K/N/E.
--          induty_code(VARCHAR(10))— KSIC 한국표준산업분류 5자리.
--          est_dt(DATE)            — 설립일 (YYYY-MM-DD).
--          acc_mt(CHAR(2))         — 결산월 (예: '12').
--
-- 데이터 보존:
--   기존 row 의 symbol/name 은 그대로 남음. 새 컬럼은 NULL 로 추가된 후
--   src.collectors.tickers_kr.refresh_tickers_from_dart(force_all=True) 로 채운다.
--   삭제되는 컬럼의 값은 백업 없이 폐기 (pykrx/FDR 수집 결과라 재현 가능).
--
-- 운영 메모:
--   migrations/010_table_column_comments.sql 의 tickers.market/sector/industry/
--   listing_date COMMENT 들은 컬럼 자체가 사라지면서 함께 제거됨 (PG 동작).

-- 1) market/sector/industry 를 참조하는 뷰 선 제거 (컬럼 DROP 전에 필요)
--    새 컬럼 구조(corp_cls/induty_code)로 재생성은 마이그레이션 마지막에 수행.
DROP VIEW IF EXISTS v_daily_prices_full;
DROP VIEW IF EXISTS v_daily_prices;
DROP VIEW IF EXISTS v_consensus_latest;

-- 2) 기존 컬럼 제거 (모두 있을 수도 / 없을 수도 있어 IF EXISTS)
ALTER TABLE tickers DROP COLUMN IF EXISTS market;
ALTER TABLE tickers DROP COLUMN IF EXISTS sector;
ALTER TABLE tickers DROP COLUMN IF EXISTS industry;
ALTER TABLE tickers DROP COLUMN IF EXISTS listing_date;

-- 2) DART 메타 컬럼 추가
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS corp_code   VARCHAR(8);
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS corp_cls    CHAR(1);
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS induty_code VARCHAR(10);
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS est_dt      DATE;
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS acc_mt      CHAR(2);

-- 3) 무결성 제약
-- corp_cls 도메인 검증 (DART /api/company.json corp_cls 값과 일치)
ALTER TABLE tickers DROP CONSTRAINT IF EXISTS tickers_corp_cls_chk;
ALTER TABLE tickers
    ADD CONSTRAINT tickers_corp_cls_chk
    CHECK (corp_cls IS NULL OR corp_cls IN ('Y', 'K', 'N', 'E'));

-- corp_code 는 사실상 1:1 매핑 (한 기업 = 한 stock_code = 한 corp_code).
-- NULL 허용 (DART 미매칭 종목 / 부트스트랩 직후) 이지만 NOT NULL 끼리는 UNIQUE.
DROP INDEX IF EXISTS uq_tickers_corp_code;
CREATE UNIQUE INDEX uq_tickers_corp_code
    ON tickers(corp_code) WHERE corp_code IS NOT NULL;

-- 자주 필터될 컬럼 인덱스
CREATE INDEX IF NOT EXISTS idx_tickers_corp_cls ON tickers(corp_cls);
CREATE INDEX IF NOT EXISTS idx_tickers_induty   ON tickers(induty_code);

-- 4) 컬럼 코멘트 (새 컬럼 한정)
COMMENT ON COLUMN tickers.corp_code IS
    'DART 8자리 corp_code. dart_corp_codes(corp_code) 와의 조인 키. '
    'NULL 은 DART 미매칭 (드물게 발생 — KONEX 신규 등).';
COMMENT ON COLUMN tickers.corp_cls IS
    '시장 구분 (DART /api/company.json corp_cls). '
    'Y=유가증권시장(KOSPI), K=코스닥(KOSDAQ), N=코넥스(KONEX), E=기타.';
COMMENT ON COLUMN tickers.induty_code IS
    'KSIC 한국표준산업분류 5자리 업종코드 (DART /api/company.json). '
    '대분류(섹터)는 src/utils/ksic.py 의 induty_code_to_sector() 로 파생.';
COMMENT ON COLUMN tickers.est_dt IS
    '회사 설립일 (DART /api/company.json est_dt, YYYYMMDD → DATE). '
    '상장일과는 다름. KRX 상장일 정보는 보존하지 않음.';
COMMENT ON COLUMN tickers.acc_mt IS
    '결산월 2자리 (예: "12" = 12월 결산). DART /api/company.json acc_mt. '
    '재무 수집기에서 분기/연간 결산일 추정에 활용.';

COMMENT ON TABLE tickers IS
    'KR 상장 종목 마스터. DART 회사개황 기반. IPO/상장폐지/종목명변경 '
    '이벤트 시에만 refresh_tickers_from_dart() 호출하여 갱신. '
    'pykrx/FinanceDataReader 의존성 폐기 (2026-05).';

-- 5) 뷰 재생성 — market/sector/industry → corp_cls/induty_code 로 교체
--    v_daily_prices: daily_prices + tickers (수정주가)
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
    'market→corp_cls(Y/K/N/E), sector/industry→induty_code(KSIC) 로 교체됨(018). '
    '쓰기는 daily_prices 에 직접 수행.';

--    v_daily_prices_full: 수정주가 + 원주가 + tickers 통합
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
    'tickers(종목명) 통합 뷰. corp_cls/induty_code 컬럼 반영(018).';

--    v_consensus_latest: 컨센서스 최신 스냅샷 + tickers
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
    'corp_cls 컬럼 반영(018, market 컬럼 대체).';
