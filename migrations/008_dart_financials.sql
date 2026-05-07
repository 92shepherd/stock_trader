-- =========================================
-- 008: DART financial statements & indicators (Phase 2)
-- =========================================
-- 금융감독원 전자공시시스템(DART) Open API에서 수집하는 분기별 재무제표.
-- Phase 2 정책:
--   - 시작: 2020년 1분기 (~22분기, 코로나 이후 최신 패턴)
--   - API: 주요계정(fnlttSinglAcnt) + 주요재무지표(fnlttSinglIndx)
--   - 구분: 연결(CFS) + 별도(OFS) 둘 다
--   - 대상: 상장사 (corp_code via dart_corp_codes.stock_code IS NOT NULL)
--
-- 설계 결정:
--   - LONG 형식 (한 row = 한 계정과목/지표) — 회계기준 변경에 유연
--   - 누적값 그대로 저장 — DART가 손익계산서 항목을 누적 제공함.
--     분기별 단독 값은 분석 시점에 차감 계산
--   - 금액 단위는 원(KRW). DART 응답이 원 단위 정수 string이라
--     NUMERIC(20,0)으로 충분히 보관 (조 단위까지 안전)

-- 분기 보고서 코드 ENUM (CHAR로 저장, 4종 고정) ----------------
-- 11013 = 1분기보고서   (Q1)
-- 11012 = 반기보고서     (Q2 누적)
-- 11014 = 3분기보고서   (Q3 누적)
-- 11011 = 사업보고서     (Q4 = 연간)


-- 주요계정 (fnlttSinglAcnt) -----------------------------------------
-- 한 호출당 ~12 row. 손익/재무상태 핵심 계정만 들어옴.
-- 예시 계정: 매출액, 영업이익, 법인세차감전순이익, 당기순이익,
--           자산총계, 부채총계, 자본총계, 유동자산, 비유동자산
CREATE TABLE IF NOT EXISTS dart_financials (
    -- 식별자 ----------------------------------
    corp_code       VARCHAR(8)   NOT NULL,
    bsns_year       SMALLINT     NOT NULL,        -- 사업연도 (예: 2024)
    reprt_code      CHAR(5)      NOT NULL,        -- 11011 / 11012 / 11013 / 11014
    fs_div          CHAR(3)      NOT NULL,        -- CFS(연결) / OFS(별도)
    sj_div          VARCHAR(10),                  -- BS(재무상태표) / IS(손익계산서) / CIS / CF / SCE
    account_id      VARCHAR(50),                  -- IFRS 표준 계정 ID (ifrs-full_Revenue 등)
    account_nm      VARCHAR(200) NOT NULL,        -- 계정명 (사람이 읽는 이름)

    -- 값 -----------------------------------------
    -- DART는 당기/전기/전전기 3년치를 한 번에 주지만 우리는 (year, reprt) 단위로
    -- 저장하므로 thstrm_amount(당기)만 thstrm_amount로, 비교용 전기/전전기는
    -- 옵션 컬럼으로 저장 (분석 시 trend 계산 편의)
    thstrm_amount   NUMERIC(20, 0),               -- 당기 금액 (원)
    frmtrm_amount   NUMERIC(20, 0),               -- 전기 금액
    bfefrmtrm_amount NUMERIC(20, 0),              -- 전전기 금액

    -- 분기별 단독값 (DART가 별도 제공할 때만 채워짐 — 손익 항목 한정)
    thstrm_add_amount NUMERIC(20, 0),             -- 당기 누적금액 (반기/3분기에서 의미)

    -- 메타 ---------------------------------------
    currency        VARCHAR(10)  DEFAULT 'KRW',
    ord             SMALLINT,                     -- DART 응답의 표시 순서
    source          VARCHAR(40)  DEFAULT 'dart_fnltt_single_acnt',
    created_at      TIMESTAMPTZ  DEFAULT NOW(),

    -- account_id가 NULL인 경우(특수 계정)도 있어 PK는 (corp, year, reprt, fs_div, account_nm)
    -- account_id 대신 account_nm으로 식별. 같은 (회사, 분기, 구분) 안에서
    -- account_nm은 유니크함 (검증 완료)
    PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div, account_nm)
);

-- 종목별 시계열 조회 (가장 빈번한 패턴: "삼성전자 매출 추이")
-- corp_code → stock_code 조인해서 사용
CREATE INDEX IF NOT EXISTS idx_dart_fin_corp_year
    ON dart_financials(corp_code, bsns_year DESC, reprt_code);

-- 분기 단위 cross-section (이번 분기 모든 종목 매출 비교)
CREATE INDEX IF NOT EXISTS idx_dart_fin_year_reprt
    ON dart_financials(bsns_year DESC, reprt_code, fs_div);

-- account_id 검색 (IFRS 표준 ID 기반 분석)
CREATE INDEX IF NOT EXISTS idx_dart_fin_account
    ON dart_financials(account_id) WHERE account_id IS NOT NULL;


-- 주요 재무지표 (fnlttSinglIndx) -------------------------------------
-- 한 호출당 ~25 row. DART가 직접 계산해서 제공하는 비율들.
-- 자체 계산이 필요 없어 단타 필터로 즉시 활용 가능.
--
-- 분류(idx_cl_code):
--   M210000 = 수익성지표 (영업이익률, 순이익률, ROA, ROE 등)
--   M220000 = 안정성지표 (부채비율, 유동비율 등)
--   M230000 = 성장성지표 (매출성장률, 영업이익성장률 등)
--   M240000 = 활동성지표 (총자산회전율 등)
CREATE TABLE IF NOT EXISTS dart_indicators (
    -- 식별자 ----------------------------------
    corp_code       VARCHAR(8)   NOT NULL,
    bsns_year       SMALLINT     NOT NULL,
    reprt_code      CHAR(5)      NOT NULL,
    fs_div          CHAR(3)      NOT NULL,         -- CFS / OFS

    -- 지표 식별 -----------------------------
    idx_cl_code     CHAR(7),                       -- M210000 등 분류 코드
    idx_cl_nm       VARCHAR(50),                   -- "수익성지표" 등
    idx_code        VARCHAR(20),                   -- 개별 지표 코드 (예: M211100)
    idx_nm          VARCHAR(200) NOT NULL,         -- "영업이익률" 등 사람이 읽는 이름

    -- 값 ----------------------------------------
    -- 비율은 % 단위로 들어와 NUMERIC(15, 4)면 천만 % 까지도 정밀 보관
    -- (실제로는 -1000 ~ 1000 % 범위가 절대 다수)
    thstrm_value    NUMERIC(15, 4),                -- 당기 값 (% 또는 비율)
    frmtrm_value    NUMERIC(15, 4),                -- 전기 값
    bfefrmtrm_value NUMERIC(15, 4),                -- 전전기 값

    -- 메타 ---------------------------------------
    source          VARCHAR(40)  DEFAULT 'dart_fnltt_single_indx',
    created_at      TIMESTAMPTZ  DEFAULT NOW(),

    PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div, idx_nm)
);

CREATE INDEX IF NOT EXISTS idx_dart_idx_corp_year
    ON dart_indicators(corp_code, bsns_year DESC, reprt_code);

CREATE INDEX IF NOT EXISTS idx_dart_idx_year_reprt
    ON dart_indicators(bsns_year DESC, reprt_code, fs_div);

-- 분류별 cross-section (예: "이번 분기 ROE 상위 100")
CREATE INDEX IF NOT EXISTS idx_dart_idx_class
    ON dart_indicators(idx_cl_code, bsns_year DESC, reprt_code);


-- 조회 편의 뷰: 주요계정 + 종목명 + 보고서 라벨 ---------------------
CREATE OR REPLACE VIEW v_dart_financials AS
SELECT
    f.corp_code,
    c.stock_code,
    c.corp_name,
    t.name              AS ticker_name,
    f.bsns_year,
    f.reprt_code,
    CASE f.reprt_code
        WHEN '11013' THEN 'Q1'
        WHEN '11012' THEN 'H1'
        WHEN '11014' THEN 'Q3'
        WHEN '11011' THEN 'FY'
        ELSE f.reprt_code
    END                 AS reprt_label,
    f.fs_div,
    f.sj_div,
    f.account_id,
    f.account_nm,
    f.thstrm_amount,
    f.frmtrm_amount,
    f.bfefrmtrm_amount,
    f.currency
FROM dart_financials f
LEFT JOIN dart_corp_codes c ON c.corp_code = f.corp_code
LEFT JOIN tickers t ON t.symbol = c.stock_code
WHERE c.stock_code IS NOT NULL;

COMMENT ON VIEW v_dart_financials IS
    '상장사 주요계정 + 종목명 + 보고서 라벨 조회 뷰. Phase 2.';

-- 조회 편의 뷰: 주요재무지표 ----------------------------------------
CREATE OR REPLACE VIEW v_dart_indicators AS
SELECT
    i.corp_code,
    c.stock_code,
    c.corp_name,
    t.name              AS ticker_name,
    i.bsns_year,
    i.reprt_code,
    CASE i.reprt_code
        WHEN '11013' THEN 'Q1'
        WHEN '11012' THEN 'H1'
        WHEN '11014' THEN 'Q3'
        WHEN '11011' THEN 'FY'
        ELSE i.reprt_code
    END                 AS reprt_label,
    i.fs_div,
    i.idx_cl_code,
    i.idx_cl_nm,
    i.idx_code,
    i.idx_nm,
    i.thstrm_value,
    i.frmtrm_value,
    i.bfefrmtrm_value
FROM dart_indicators i
LEFT JOIN dart_corp_codes c ON c.corp_code = i.corp_code
LEFT JOIN tickers t ON t.symbol = c.stock_code
WHERE c.stock_code IS NOT NULL;

COMMENT ON VIEW v_dart_indicators IS
    '상장사 주요재무지표 + 종목명 + 보고서 라벨 조회 뷰. Phase 2.';
