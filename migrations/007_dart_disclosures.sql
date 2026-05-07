-- =========================================
-- 007: DART disclosures (Phase 1)
-- =========================================
-- 금융감독원 전자공시시스템(DART) Open API에서 수집하는 데이터.
-- Phase 1: corp_code 매핑 + 공시 목록(주요사항보고 특화)
-- Phase 2 이후: 재무제표(단일회사 주요계정), 주요 재무지표 등이 추가 예정.
--
-- 분리 이유:
--   - DART는 한국 종목에만 해당 (미국 daily_prices_us와 별개)
--   - corp_code는 DART 자체 8자리 ID이며, 우리 종목코드(6자리)와 다름
--   - 공시는 시계열이지만 종목당 빈도가 낮아(연 100건 미만) hypertable 불필요

-- DART 고유번호 매핑 ---------------------------------------------
-- Source: https://opendart.fss.or.kr/api/corpCode.xml (ZIP 다운로드)
-- ~10만 개 row (상장+비상장+해산기업 모두 포함)
-- 우리 분석 대상은 stock_code가 NOT NULL인 ~2,500개 (상장사)
CREATE TABLE IF NOT EXISTS dart_corp_codes (
    corp_code       VARCHAR(8)   PRIMARY KEY,    -- DART 고유번호 (ex: 00126380)
    corp_name       VARCHAR(200) NOT NULL,        -- 정식 회사명
    stock_code      VARCHAR(10),                  -- 종목코드 (상장사만, 비상장은 NULL)
    modify_date     DATE,                         -- DART 측 최종 수정일
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- 종목코드 → corp_code 빠른 역매핑 (다른 컬렉터에서 자주 사용)
-- 상장사만 인덱싱 (NULL 종목코드는 불필요, 부분 인덱스로 크기 절약)
CREATE INDEX IF NOT EXISTS idx_dart_corp_codes_stock
    ON dart_corp_codes(stock_code) WHERE stock_code IS NOT NULL;

-- 회사명 검색 (운영 중 확인용)
CREATE INDEX IF NOT EXISTS idx_dart_corp_codes_name
    ON dart_corp_codes(corp_name);


-- DART 공시 목록 -------------------------------------------------
-- Source: https://opendart.fss.or.kr/api/list.json (페이지네이션)
-- Phase 1 정책: 상장사 공시만 저장 (stock_code NOT NULL)
--             주요사항보고(kind='B') 우선이지만 다른 종류도 모두 받음 — 필터는 조회 시점에
-- 빈도: 상장사 ~2,500개 × 연평균 ~30~100건 = 연간 7만~25만 row 예상
--      → 일반 테이블로 충분 (hypertable 오버킬)
CREATE TABLE IF NOT EXISTS dart_disclosures (
    rcept_no        VARCHAR(14)  PRIMARY KEY,    -- 접수번호 (DART의 글로벌 ID)
    corp_code       VARCHAR(8)   NOT NULL,        -- DART 고유번호
    corp_name       VARCHAR(200) NOT NULL,        -- 회사명 (수집 시점 스냅샷)
    stock_code      VARCHAR(10),                  -- 종목코드
    corp_cls        CHAR(1),                      -- Y=유가, K=코스닥, N=코넥스, E=기타
    report_nm       VARCHAR(500) NOT NULL,        -- 공시 제목
    rcept_dt        DATE         NOT NULL,        -- 접수일자 (공시 일자)
    flr_nm          VARCHAR(200),                 -- 공시 제출인 (회사 또는 임원)
    rm              VARCHAR(50),                  -- 비고 (정정공시 등 표시)
    kind            CHAR(1),                      -- 공시 분류: A/B/C/D/E/F/G/H/I/J
    kind_detail     VARCHAR(20),                  -- 세부 코드 (A001, B001 등 — 추후 확장용)
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- FK: dart_disclosures.corp_code → dart_corp_codes.corp_code
-- (선택: 강제하지 않음 — DART 자체 데이터 정합성 의존이라 약한 결합 유지)
-- 만약 강제하려면 아래 주석 해제:
-- ALTER TABLE dart_disclosures ADD CONSTRAINT fk_dart_disclosures_corp
--     FOREIGN KEY (corp_code) REFERENCES dart_corp_codes(corp_code);

-- 종목코드별 시계열 조회 (가장 빈번한 패턴: "삼성전자 최근 공시")
CREATE INDEX IF NOT EXISTS idx_dart_disclosures_stock_dt
    ON dart_disclosures(stock_code, rcept_dt DESC)
    WHERE stock_code IS NOT NULL;

-- 일별 공시 흐름 조회 ("어제 발표된 모든 주요사항보고")
CREATE INDEX IF NOT EXISTS idx_dart_disclosures_dt_kind
    ON dart_disclosures(rcept_dt DESC, kind);

-- corp_code별 조회 (FK 없이도 자주 사용)
CREATE INDEX IF NOT EXISTS idx_dart_disclosures_corp
    ON dart_disclosures(corp_code, rcept_dt DESC);

-- 주요사항보고만 빠른 조회 (단타 시그널 핵심 패턴)
-- 부분 인덱스로 크기 최소화
CREATE INDEX IF NOT EXISTS idx_dart_disclosures_kind_b
    ON dart_disclosures(rcept_dt DESC, stock_code)
    WHERE kind = 'B' AND stock_code IS NOT NULL;


-- 조회 편의 뷰: 상장사 + 종목명 + (옵션) 한국 tickers와 JOIN ---------
-- 한국 tickers 테이블의 name과 DART corp_name이 다를 수 있어 둘 다 노출.
CREATE OR REPLACE VIEW v_dart_disclosures AS
SELECT
    d.rcept_no,
    d.stock_code,
    d.corp_code,
    d.corp_name        AS dart_corp_name,
    t.name             AS ticker_name,    -- KRX 기준 종목명 (있는 경우)
    d.corp_cls,
    d.kind,
    d.report_nm,
    d.rcept_dt,
    d.flr_nm,
    d.rm
FROM dart_disclosures d
LEFT JOIN tickers t ON t.symbol = d.stock_code
WHERE d.stock_code IS NOT NULL;

COMMENT ON VIEW v_dart_disclosures IS
    '상장사 DART 공시 + 종목명 JOIN 뷰. 단타 시그널 조회용.';
