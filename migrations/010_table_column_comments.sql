-- =========================================
-- 010: Table & column comments (documentation)
-- =========================================
-- 지금까지 생성된 모든 테이블과 컬럼에 COMMENT를 부여합니다.
-- COMMENT는 psql의 \d+, DBeaver/DataGrip의 description 컬럼,
-- pg_description 시스템 카탈로그에서 직접 확인 가능합니다.
--
-- 정책:
--   - 한국어 위주 (도메인 용어가 한국 주식시장 기반)
--   - 단위/포맷/null 의미를 명시
--   - 운영상 주의사항(압축, 결측 등)도 포함
--   - VIEW의 COMMENT는 005~008 마이그레이션에서 이미 설정됨
--     → 여기서는 새로 추가만 하고 기존 것은 건드리지 않음

-- =========================================================
-- schema_migrations (마이그레이션 추적 메타 테이블)
-- =========================================================
COMMENT ON TABLE schema_migrations IS
    '적용된 마이그레이션 파일 추적 테이블. src/db/migrate.py가 자동 관리.';

COMMENT ON COLUMN schema_migrations.filename IS
    '적용된 마이그레이션 파일명 (예: 001_init_schema.sql). PK.';
COMMENT ON COLUMN schema_migrations.applied_at IS
    '해당 마이그레이션이 적용된 시각 (UTC).';


-- =========================================================
-- tickers (한국 종목 마스터)
-- =========================================================
COMMENT ON TABLE tickers IS
    '한국(KOSPI/KOSDAQ) 상장 종목 마스터. FinanceDataReader로 수집. '
    'symbol(6자리)을 PK로 사용. 상장폐지 종목은 delisted=TRUE로 보존.';

COMMENT ON COLUMN tickers.symbol IS
    '종목코드 (한국 6자리 숫자, 예: 005930). PK.';
COMMENT ON COLUMN tickers.name IS
    '종목명 (한글, 사명 변경 시 갱신됨).';
COMMENT ON COLUMN tickers.market IS
    '시장 구분: KOSPI / KOSDAQ.';
COMMENT ON COLUMN tickers.sector IS
    '업종 대분류 (FDR desc=True 옵션으로 수집). NULL 가능.';
COMMENT ON COLUMN tickers.industry IS
    '산업 세부 분류. NULL 가능.';
COMMENT ON COLUMN tickers.listing_date IS
    '상장일. FDR에서 미제공 시 NULL.';
COMMENT ON COLUMN tickers.delisted IS
    '상장폐지 여부. TRUE인 종목은 신규 수집 대상에서 제외.';
COMMENT ON COLUMN tickers.delisted_date IS
    '상장폐지일. delisted=FALSE면 NULL.';
COMMENT ON COLUMN tickers.created_at IS
    'row 최초 INSERT 시각.';
COMMENT ON COLUMN tickers.updated_at IS
    'row 마지막 UPDATE 시각 (UPSERT 시 NOW() 갱신).';


-- =========================================================
-- daily_prices (한국 일봉, TimescaleDB hypertable)
-- =========================================================
COMMENT ON TABLE daily_prices IS
    '한국 종목 일봉 시계열 (TimescaleDB hypertable, 1개월 청크). '
    '3개월 경과 데이터는 자동 압축. 압축된 청크는 DELETE 불가 — '
    '재수집 시 TRUNCATE + collection_log 정리 필요.';

COMMENT ON COLUMN daily_prices.symbol IS
    '종목코드 (tickers.symbol 참조, FK 미설정).';
COMMENT ON COLUMN daily_prices.date IS
    '거래일자 (KST 기준). hypertable 시간축.';
COMMENT ON COLUMN daily_prices.open IS
    '시가 (KRW).';
COMMENT ON COLUMN daily_prices.high IS
    '고가 (KRW).';
COMMENT ON COLUMN daily_prices.low IS
    '저가 (KRW).';
COMMENT ON COLUMN daily_prices.close IS
    '종가 (KRW).';
COMMENT ON COLUMN daily_prices.volume IS
    '거래량 (주).';
COMMENT ON COLUMN daily_prices.value IS
    '거래대금 (KRW). pykrx 한정 제공, FDR은 NULL.';
COMMENT ON COLUMN daily_prices.market_cap IS
    '시가총액 (KRW). pykrx 한정.';
COMMENT ON COLUMN daily_prices.shares IS
    '상장주식수. pykrx 한정.';
COMMENT ON COLUMN daily_prices.foreign_net IS
    '외국인 순매수 (주, +매수/-매도). pykrx 한정.';
COMMENT ON COLUMN daily_prices.institution_net IS
    '기관 순매수 (주). pykrx 한정.';
COMMENT ON COLUMN daily_prices.individual_net IS
    '개인 순매수 (주). pykrx 한정.';
COMMENT ON COLUMN daily_prices.per IS
    '주가수익비율 (Price-Earnings Ratio). pykrx 한정.';
COMMENT ON COLUMN daily_prices.pbr IS
    '주가순자산비율 (Price-Book Ratio). pykrx 한정.';
COMMENT ON COLUMN daily_prices.dividend_yield IS
    '배당수익률 (%). pykrx 한정.';


-- =========================================================
-- minute_prices (한국 분봉, TimescaleDB hypertable)
-- =========================================================
COMMENT ON TABLE minute_prices IS
    '한국 종목 분봉 시계열 (TimescaleDB hypertable, 7일 청크). '
    '7일 경과 데이터 자동 압축. KIS Open API로 수집 예정 — '
    '대량 백필은 비실용적 (일자×종목당 1콜).';

COMMENT ON COLUMN minute_prices.symbol IS
    '종목코드.';
COMMENT ON COLUMN minute_prices.ts IS
    '분봉 타임스탬프 (TIMESTAMPTZ, 분 단위 시작 시각). hypertable 시간축.';
COMMENT ON COLUMN minute_prices.open IS
    '해당 분봉 시가 (KRW).';
COMMENT ON COLUMN minute_prices.high IS
    '해당 분봉 고가 (KRW).';
COMMENT ON COLUMN minute_prices.low IS
    '해당 분봉 저가 (KRW).';
COMMENT ON COLUMN minute_prices.close IS
    '해당 분봉 종가 (KRW).';
COMMENT ON COLUMN minute_prices.volume IS
    '해당 분봉 거래량 (주).';
COMMENT ON COLUMN minute_prices.value IS
    '해당 분봉 거래대금 (KRW).';


-- =========================================================
-- collection_log (수집 진행/재개 로그)
-- =========================================================
COMMENT ON TABLE collection_log IS
    '데이터 수집 진행 상태/재개 로그. 논리적 키는 (collector, target_date, symbol). '
    'skip_done=True일 때 status=success 인 row를 건너뛰는 데 사용.';

COMMENT ON COLUMN collection_log.id IS
    'BIGSERIAL surrogate PK.';
COMMENT ON COLUMN collection_log.collector IS
    '수집기 식별자: daily_fdr / daily_pykrx / minute_kis / dart_corp_codes / '
    'dart_disclosures / dart_financials / dart_indicators / daily_us_yfinance 등. '
    '각 컬렉터의 COLLECTOR_NAME 상수 값과 동일.';
COMMENT ON COLUMN collection_log.symbol IS
    '대상 종목코드. 종목 단위 수집은 값 채움, 날짜 단위 수집은 NULL일 수 있음.';
COMMENT ON COLUMN collection_log.target_date IS
    '수집 대상 일자. 날짜 범위 수집 시 end_date 기준으로 기록.';
COMMENT ON COLUMN collection_log.status IS
    '수집 결과: success / failed / partial / skipped. '
    'partial은 일부 row만 저장된 경우, skipped는 데이터 없음/휴장 등.';
COMMENT ON COLUMN collection_log.rows_inserted IS
    '실제로 INSERT/UPSERT된 row 수.';
COMMENT ON COLUMN collection_log.error_message IS
    'failed/partial 시 에러 메시지 (스택 trace는 로그 파일 참조).';
COMMENT ON COLUMN collection_log.duration_ms IS
    '해당 수집 단위의 수행 시간 (밀리초).';
COMMENT ON COLUMN collection_log.created_at IS
    '로그 기록 시각.';


-- =========================================================
-- tickers_us (미국 종목 마스터)
-- =========================================================
COMMENT ON TABLE tickers_us IS
    '미국(NASDAQ/NYSE/AMEX 등) 상장 종목 마스터. '
    'NASDAQ Trader FTP의 nasdaqtraded.txt + otherlisted.txt로 수집. '
    '한국 tickers와 통화/거래시간/세션이 달라 별도 테이블로 분리.';

COMMENT ON COLUMN tickers_us.symbol IS
    '미국 티커 (1~5자, BRK.B 같은 점/하이픈 표기 허용 → VARCHAR(15)). PK.';
COMMENT ON COLUMN tickers_us.name IS
    '회사명/종목명 (영문).';
COMMENT ON COLUMN tickers_us.exchange IS
    '거래소: NASDAQ / NYSE / AMEX / NYSEARCA / BATS.';
COMMENT ON COLUMN tickers_us.security_type IS
    '증권 종류: COMMON / ETF / ADR / PREFERRED / WARRANT / UNIT 등.';
COMMENT ON COLUMN tickers_us.is_etf IS
    'ETF 여부 (NASDAQ Trader 파일의 ETF 플래그).';
COMMENT ON COLUMN tickers_us.test_issue IS
    '테스트용 종목 여부. 일반 조회에서는 제외 권장.';
COMMENT ON COLUMN tickers_us.listing_date IS
    '상장일. yfinance fast_info에서 사후 채움 — 보통 NULL.';
COMMENT ON COLUMN tickers_us.delisted IS
    '상장폐지 여부.';
COMMENT ON COLUMN tickers_us.delisted_date IS
    '상장폐지일.';
COMMENT ON COLUMN tickers_us.created_at IS
    'row 최초 INSERT 시각.';
COMMENT ON COLUMN tickers_us.updated_at IS
    'row 마지막 UPDATE 시각.';


-- =========================================================
-- daily_prices_us (미국 일봉, TimescaleDB hypertable)
-- =========================================================
COMMENT ON TABLE daily_prices_us IS
    '미국 종목 일봉 시계열 (TimescaleDB hypertable, 1개월 청크). '
    'yfinance에서 수집. 3개월 경과 데이터 자동 압축. '
    '시가총액/PER 등은 별도 API라 미저장.';

COMMENT ON COLUMN daily_prices_us.symbol IS
    '미국 티커 (tickers_us.symbol 참조).';
COMMENT ON COLUMN daily_prices_us.date IS
    '거래일자 (ET 기준). hypertable 시간축.';
COMMENT ON COLUMN daily_prices_us.open IS
    '시가 (USD). 저가주/ETF 대응을 위해 NUMERIC(14,4).';
COMMENT ON COLUMN daily_prices_us.high IS
    '고가 (USD).';
COMMENT ON COLUMN daily_prices_us.low IS
    '저가 (USD).';
COMMENT ON COLUMN daily_prices_us.close IS
    '종가 — yfinance의 unadjusted Close (raw, USD).';
COMMENT ON COLUMN daily_prices_us.adj_close IS
    '수정종가 — 분할/배당 반영 (yfinance Adj Close, USD).';
COMMENT ON COLUMN daily_prices_us.volume IS
    '거래량 (주).';
COMMENT ON COLUMN daily_prices_us.dividend IS
    '해당일 지급 배당금 (USD/share). 배당락이 아닌 일은 0.';
COMMENT ON COLUMN daily_prices_us.split_ratio IS
    '해당일 분할 비율 (1:2면 2.0, 분할 없으면 0).';
COMMENT ON COLUMN daily_prices_us.source IS
    '데이터 출처. 기본 ''yfinance''. 향후 polygon/alpaca 등 추가 시 분기.';
COMMENT ON COLUMN daily_prices_us.created_at IS
    'row 최초 INSERT 시각.';


-- =========================================================
-- dart_corp_codes (DART 고유번호 ↔ 종목코드 매핑)
-- =========================================================
COMMENT ON TABLE dart_corp_codes IS
    'DART 고유번호 매핑 테이블. opendart.fss.or.kr/api/corpCode.xml 로 수집. '
    '~10만 row (상장+비상장+해산 모두). stock_code IS NOT NULL이 상장사(~2,500개).';

COMMENT ON COLUMN dart_corp_codes.corp_code IS
    'DART 고유번호 (8자리, 예: 00126380). PK.';
COMMENT ON COLUMN dart_corp_codes.corp_name IS
    'DART 등록 정식 회사명.';
COMMENT ON COLUMN dart_corp_codes.stock_code IS
    '종목코드 (상장사만 채워짐). tickers.symbol과 동일 체계.';
COMMENT ON COLUMN dart_corp_codes.modify_date IS
    'DART 측 최종 수정일자.';
COMMENT ON COLUMN dart_corp_codes.created_at IS
    'row 최초 INSERT 시각.';
COMMENT ON COLUMN dart_corp_codes.updated_at IS
    'row 마지막 UPDATE 시각.';


-- =========================================================
-- dart_disclosures (DART 공시 목록)
-- =========================================================
COMMENT ON TABLE dart_disclosures IS
    'DART 공시 목록. opendart.fss.or.kr/api/list.json 로 수집. '
    '상장사 공시 위주(stock_code NOT NULL). 종목당 연 30~100건 빈도라 hypertable 미적용.';

COMMENT ON COLUMN dart_disclosures.rcept_no IS
    '접수번호 (DART 글로벌 ID, 14자리). PK.';
COMMENT ON COLUMN dart_disclosures.corp_code IS
    'DART 고유번호 (dart_corp_codes.corp_code 참조, FK 미설정).';
COMMENT ON COLUMN dart_disclosures.corp_name IS
    '회사명 (수집 시점 스냅샷).';
COMMENT ON COLUMN dart_disclosures.stock_code IS
    '종목코드. 비상장사 공시는 NULL.';
COMMENT ON COLUMN dart_disclosures.corp_cls IS
    '법인 구분: Y=유가증권, K=코스닥, N=코넥스, E=기타.';
COMMENT ON COLUMN dart_disclosures.report_nm IS
    '공시 제목/보고서명.';
COMMENT ON COLUMN dart_disclosures.rcept_dt IS
    '접수일자 (공시 일자, KST).';
COMMENT ON COLUMN dart_disclosures.flr_nm IS
    '공시 제출인 (회사명 또는 임원명).';
COMMENT ON COLUMN dart_disclosures.rm IS
    '비고. 정정공시 등의 표시 정보.';
COMMENT ON COLUMN dart_disclosures.kind IS
    '공시 분류 1자리 코드: '
    'A(정기공시) / B(주요사항보고) / C(발행공시) / D(지분공시) / E(기타공시) / '
    'F(외부감사관련) / G(펀드공시) / H(자산유동화) / I(거래소공시) / J(공정위공시).';
COMMENT ON COLUMN dart_disclosures.kind_detail IS
    '세부 코드 (A001, B001 등). 추후 확장용.';
COMMENT ON COLUMN dart_disclosures.created_at IS
    'row 최초 INSERT 시각.';


-- =========================================================
-- dart_financials (DART 주요계정, LONG 형식)
-- =========================================================
COMMENT ON TABLE dart_financials IS
    'DART 분기별 주요계정 (fnlttSinglAcnt). LONG 형식 — 한 row = 한 계정과목. '
    '금액 단위 KRW, 누적값 그대로 저장 (분기 단독값은 분석 시 차감 계산). '
    '연결(CFS)/별도(OFS) 둘 다 저장.';

COMMENT ON COLUMN dart_financials.corp_code IS
    'DART 고유번호.';
COMMENT ON COLUMN dart_financials.bsns_year IS
    '사업연도 (예: 2024).';
COMMENT ON COLUMN dart_financials.reprt_code IS
    '보고서 코드: 11013(Q1) / 11012(반기) / 11014(Q3) / 11011(사업보고서/연간).';
COMMENT ON COLUMN dart_financials.fs_div IS
    '재무제표 구분: CFS(연결) / OFS(별도).';
COMMENT ON COLUMN dart_financials.sj_div IS
    '재무제표 종류: BS(재무상태표) / IS(손익계산서) / CIS(포괄손익) / CF(현금흐름) / SCE(자본변동).';
COMMENT ON COLUMN dart_financials.account_id IS
    'IFRS 표준 계정 ID (예: ifrs-full_Revenue). 비표준 계정은 NULL.';
COMMENT ON COLUMN dart_financials.account_nm IS
    '계정명 (한글, 예: ''매출액'', ''영업이익''). PK 구성요소.';
COMMENT ON COLUMN dart_financials.thstrm_amount IS
    '당기 금액 (KRW, 누적값).';
COMMENT ON COLUMN dart_financials.frmtrm_amount IS
    '전기 금액 (KRW). 전년 동기 비교용.';
COMMENT ON COLUMN dart_financials.bfefrmtrm_amount IS
    '전전기 금액 (KRW). 2년 전 동기 비교용.';
COMMENT ON COLUMN dart_financials.thstrm_add_amount IS
    '당기 누적금액 (KRW). 반기/3분기 손익 항목에서 의미. DART 미제공 시 NULL.';
COMMENT ON COLUMN dart_financials.currency IS
    '통화 (기본 KRW).';
COMMENT ON COLUMN dart_financials.ord IS
    'DART 응답상 표시 순서.';
COMMENT ON COLUMN dart_financials.source IS
    '데이터 출처/API 식별자 (기본: dart_fnltt_single_acnt).';
COMMENT ON COLUMN dart_financials.created_at IS
    'row 최초 INSERT 시각.';


-- =========================================================
-- dart_indicators (DART 주요재무지표, LONG 형식)
-- =========================================================
COMMENT ON TABLE dart_indicators IS
    'DART 분기별 주요재무지표 (fnlttSinglIndx). LONG 형식 — 한 row = 한 지표. '
    'DART가 직접 계산해서 제공 — 자체 계산 불필요, 단타 필터에 즉시 활용 가능. '
    '값은 % 단위 또는 비율.';

COMMENT ON COLUMN dart_indicators.corp_code IS
    'DART 고유번호.';
COMMENT ON COLUMN dart_indicators.bsns_year IS
    '사업연도.';
COMMENT ON COLUMN dart_indicators.reprt_code IS
    '보고서 코드 (11011/11012/11013/11014).';
COMMENT ON COLUMN dart_indicators.fs_div IS
    '재무제표 구분: CFS(연결) / OFS(별도).';
COMMENT ON COLUMN dart_indicators.idx_cl_code IS
    '지표 분류 코드: M210000(수익성) / M220000(안정성) / M230000(성장성) / M240000(활동성).';
COMMENT ON COLUMN dart_indicators.idx_cl_nm IS
    '지표 분류명 (한글, 예: ''수익성지표'').';
COMMENT ON COLUMN dart_indicators.idx_code IS
    '개별 지표 코드 (예: M211100).';
COMMENT ON COLUMN dart_indicators.idx_nm IS
    '지표명 (한글, 예: ''영업이익률'', ''ROE''). PK 구성요소.';
COMMENT ON COLUMN dart_indicators.thstrm_value IS
    '당기 값 (% 또는 비율).';
COMMENT ON COLUMN dart_indicators.frmtrm_value IS
    '전기 값.';
COMMENT ON COLUMN dart_indicators.bfefrmtrm_value IS
    '전전기 값.';
COMMENT ON COLUMN dart_indicators.source IS
    '데이터 출처/API 식별자 (기본: dart_fnltt_single_indx).';
COMMENT ON COLUMN dart_indicators.created_at IS
    'row 최초 INSERT 시각.';
