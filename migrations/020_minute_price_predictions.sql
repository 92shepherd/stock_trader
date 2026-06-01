-- 020_minute_price_predictions.sql
--
-- 분봉 가격 예측 결과 저장 테이블.
--
-- 목적:
--   특정 종목의 장 운영시간(09:00~15:30, 총 390분봉) 동안의
--   예측 close 가격을 1분 단위로 저장한다.
--
-- 예측 생성 규칙:
--   - 오전 08:00 이전 호출 → 당일(오늘) 장 운영시간 예측
--   - 오전 08:00 이후 호출 → 다음 영업일 장 운영시간 예측
--   - 같은 (symbol, ts) 가 이미 존재하면 upsert (ON CONFLICT DO UPDATE)
--
-- 모델:
--   LightGBM 회귀. 종목별 전용 모델 (per-symbol).
--   입력 피처: 직전 한달치 분봉 통계 + 일봉 모멘텀/펀더멘털 + 공시/컨센서스.
--   예측 대상: 전일 종가 대비 수익률 → 역변환하여 가격으로 저장.
--
-- 설계:
--   - ts 컬럼이 hypertable 시간축 (1 month 청크).
--   - PK: (symbol, ts) — (종목, 분봉 타임스탬프) 유일성 보장.
--   - predicted_return: 모델 raw 출력 (전일 종가 대비 수익률).
--   - predicted_close: 역변환 가격 (prev_close × (1 + predicted_return)).
--   - model_version: 모델 재학습 시 버전 추적용.
--   - feature_snapshot: 예측에 사용된 피처 값 JSONB (디버깅/재현용).
--
-- TimescaleDB 주의:
--   hypertable 에 대한 DELETE 는 압축 청크에서 불가. upsert 로만 갱신.

CREATE TABLE IF NOT EXISTS minute_price_predictions (
    symbol              VARCHAR(10)     NOT NULL,
    ts                  TIMESTAMPTZ     NOT NULL,
    predicted_return    NUMERIC(12, 8),
    predicted_close     NUMERIC(14, 2),
    prev_close          NUMERIC(14, 2),
    model_version       VARCHAR(40)     NOT NULL DEFAULT 'lgbm_v1',
    feature_snapshot    JSONB,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, ts)
);

COMMENT ON TABLE minute_price_predictions IS
    '종목별 분봉(1분) 예측 가격. LightGBM per-symbol 모델 출력. '
    '오전 08:00 기준으로 당일/익일 09:00~15:30 (390 포인트) 를 생성. '
    'upsert-only (hypertable 압축 이슈로 DELETE 사용 금지).';

COMMENT ON COLUMN minute_price_predictions.symbol IS
    '6자리 KRX 종목코드. tickers.symbol 참조.';

COMMENT ON COLUMN minute_price_predictions.ts IS
    '예측 대상 분봉 타임스탬프 (KST, timezone-aware). '
    'hypertable 시간축. 09:00~15:30, 1분 간격으로 390개 포인트.';

COMMENT ON COLUMN minute_price_predictions.predicted_return IS
    '모델 raw 출력: 전일 종가 대비 수익률. '
    '예: 0.01234 = +1.234%. predicted_close = prev_close × (1 + predicted_return).';

COMMENT ON COLUMN minute_price_predictions.predicted_close IS
    '예측 close 가격(원). prev_close × (1 + predicted_return) 역변환 결과. '
    '단가 직관 확인용 — 실제 트레이딩 신호는 predicted_return 사용 권장.';

COMMENT ON COLUMN minute_price_predictions.prev_close IS
    '역변환 기준이 된 전일 종가. 예측 당시 daily_prices 에서 조회한 값. '
    '보존하는 이유: prev_close 변경(수정주가 소급 반영 등) 시 predicted_close 재현에 필요.';

COMMENT ON COLUMN minute_price_predictions.model_version IS
    '예측에 사용된 모델 버전 식별자. '
    'lgbm_v1 = 기본 LightGBM 회귀 모델. 재학습/구조변경 시 버전 올림.';

COMMENT ON COLUMN minute_price_predictions.feature_snapshot IS
    '예측에 사용된 피처 값 JSON (디버깅 및 예측 재현용). '
    '예: {"momentum_20d": 0.03, "prev_volume_ratio": 1.2, ...}. '
    'NULL 가능 — 저장 비용이 클 경우 생략 가능.';

COMMENT ON COLUMN minute_price_predictions.created_at IS
    '최초 예측 생성 시각.';

COMMENT ON COLUMN minute_price_predictions.updated_at IS
    '마지막 upsert 시각. 같은 (symbol, ts) 재예측 시 갱신.';

-- 인덱스: 날짜별 전체 종목 조회 (당일 예측 조회 패턴)
CREATE INDEX IF NOT EXISTS idx_mpp_ts_symbol
    ON minute_price_predictions(ts DESC, symbol);

-- 인덱스: 종목별 최근 예측 조회
CREATE INDEX IF NOT EXISTS idx_mpp_symbol_ts
    ON minute_price_predictions(symbol, ts DESC);

-- hypertable 변환 (1 month 청크)
SELECT create_hypertable(
    'minute_price_predictions', 'ts',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists       => TRUE
);
