-- =========================================
-- 013: 한경 컨센서스 — 종목별 애널리스트 리포트 수집
-- =========================================
--
-- 출처: markets.hankyung.com/consensus (한국경제신문 컨센서스)
-- 수집기: src/collectors/consensus_hankyung.py
--
-- 데이터 모델:
--   - 개별 애널리스트 리포트 단위 저장 (FnGuide 스냅샷 방식과 다름)
--   - REPORT_IDX 를 PK 로 사용 — 동일 리포트 중복 수집 안전
--   - 날짜 범위 지정 수집 지원 (1년치 백필 포함)
--
-- 주요 필드:
--   report_idx       — 한경 API 의 고유 리포트 ID
--   business_code    — 6자리 KRX 종목코드
--   report_date      — 리포트 발행일
--   grade_value      — 투자의견 원문 (Buy, Strong Buy, Hold 등)
--   target_price     — 목표주가 (원)
--   prev_target_price — 직전 목표주가 (원), 상향/하향 추적용

CREATE TABLE IF NOT EXISTS hankyung_reports (
    report_idx          BIGINT       NOT NULL,
    business_code       VARCHAR(10),
    business_name       VARCHAR(100),
    report_date         DATE         NOT NULL,

    -- 투자의견 / 목표주가
    grade_value         VARCHAR(30),
    target_price        NUMERIC(14, 0),
    prev_target_price   NUMERIC(14, 0),
    change_price        NUMERIC(14, 0),

    -- 리포트 메타
    analyst             VARCHAR(100),
    office_name         VARCHAR(100),
    report_title        VARCHAR(500),

    -- 수집 메타
    collected_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (report_idx)
);

-- 컬럼 코멘트
COMMENT ON TABLE  hankyung_reports IS
    '한국경제신문 컨센서스 애널리스트 리포트. 리포트 단위 저장 (report_idx PK)';
COMMENT ON COLUMN hankyung_reports.report_idx IS
    '한경 API 의 고유 리포트 식별자 (REPORT_IDX). PK';
COMMENT ON COLUMN hankyung_reports.business_code IS
    '6자리 KRX 종목코드. reportType=CO(기업) 리포트에서만 유효';
COMMENT ON COLUMN hankyung_reports.report_date IS
    '리포트 발행일 (REPORT_DATE). 수집 날짜와 다를 수 있음';
COMMENT ON COLUMN hankyung_reports.grade_value IS
    '투자의견 원문. 예: Buy, Strong Buy, Hold, Sell. 한경 API 원본값 그대로 저장';
COMMENT ON COLUMN hankyung_reports.target_price IS
    '목표주가 (원). TARGET_STOCK_PRICES 원본값';
COMMENT ON COLUMN hankyung_reports.prev_target_price IS
    '직전 목표주가 (원). OLD_TARGET_STOCK_PRICES. NULL 이면 신규 커버리지';
COMMENT ON COLUMN hankyung_reports.change_price IS
    '목표주가 변동액 (target_price - prev_target_price). 양수=상향, 음수=하향';
COMMENT ON COLUMN hankyung_reports.analyst IS
    '담당 애널리스트 이름 (REPORT_WRITER)';
COMMENT ON COLUMN hankyung_reports.office_name IS
    '증권사명 (OFFICE_NAME)';
COMMENT ON COLUMN hankyung_reports.report_title IS
    '리포트 제목 (REPORT_TITLE)';
COMMENT ON COLUMN hankyung_reports.collected_at IS
    'DB 삽입 시각. 백필 시 report_date 보다 늦을 수 있음';

-- 인덱스
-- 1) 종목별 최신 리포트 조회 (가장 흔한 쿼리)
CREATE INDEX IF NOT EXISTS idx_hankyung_code_date
    ON hankyung_reports(business_code, report_date DESC);

-- 2) 날짜별 전체 리포트 조회 (일별 수집 모니터링)
CREATE INDEX IF NOT EXISTS idx_hankyung_date
    ON hankyung_reports(report_date DESC);

-- 최신 리포트 뷰 (종목당 가장 최근 리포트 1건)
CREATE OR REPLACE VIEW v_hankyung_latest AS
SELECT DISTINCT ON (business_code)
    report_idx,
    business_code,
    business_name,
    report_date,
    grade_value,
    target_price,
    prev_target_price,
    change_price,
    analyst,
    office_name,
    report_title
  FROM hankyung_reports
 WHERE business_code IS NOT NULL
 ORDER BY business_code, report_date DESC, report_idx DESC;

COMMENT ON VIEW v_hankyung_latest IS
    '종목별 가장 최근 한경 컨센서스 리포트 1건. 현재 투자의견/목표주가 조회용';
