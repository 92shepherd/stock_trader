-- =========================================
-- 014: Phase 1 평가 인프라 — Cross-sectional alpha 평가용 테이블
-- =========================================
--
-- 목적:
--   주가 예측 모델을 만들기 전, "어떤 신호가 진짜 알파인가" 를 측정
--   하는 표준 평가 인프라. Cross-sectional ranking 프레임에서 IC /
--   RankIC / ICIR / Sharpe 를 계산하기 위한 4개 테이블을 만든다.
--
-- 테이블 구성:
--   1) forward_returns  - 종목 x 거래일 x horizon 의 미래 수익률
--      (사전 계산해서 캐싱; 매 평가마다 다시 계산하지 않도록)
--   2) factor_signals   - 종목 x 거래일 x factor_name 의 신호값
--      (raw, neutralized, normalized 단계별 보존)
--   3) eval_runs        - 평가 실행 1건의 메타 (run_id, 팩터명, universe,
--                         period, params 등)
--   4) eval_metrics     - eval_runs 에 대한 결과 지표 (IC, RankIC, ICIR,
--                         Sharpe, hit rate, MaxDD, turnover 등)
--
-- 시간축 설계:
--   - forward_returns, factor_signals 는 hypertable (1 month 청크)
--   - eval_runs, eval_metrics 는 일반 테이블 (row 수 적음, 정렬 무관)
--
-- 재실행 안전성:
--   forward_returns, factor_signals 는 PK 가 자연키이므로 ON CONFLICT
--   DO UPDATE 로 idempotent. eval_runs 는 run_id (애플리케이션 생성)가
--   매번 생성.


-- =========================================================
-- 1) forward_returns - 종목 x 날짜 x horizon 의 미래 수익률
-- =========================================================
-- 정의:
--   horizon_days = N 의 forward_return =
--       (close_{t+N} - close_t) / close_t
--   단, t 와 t+N 모두 거래일이어야 함 (휴장은 horizon 계산에서 skip).
--
--   high_return 은 next-day high 기준 별도 컬럼으로 보관 — 사용자
--   현재 모델의 타겟 정의 ((High_{t+1} - Close_t) / Close_t) 와 동일.
--
-- 권리이전(분할/배당) 반영:
--   close 컬럼은 daily_prices 의 수정종가를 가정. 원주가는
--   daily_prices_raw 별도. 평가시 항상 수정종가 기준.

CREATE TABLE IF NOT EXISTS forward_returns (
    symbol         VARCHAR(10)  NOT NULL,
    date           DATE         NOT NULL,
    horizon_days   SMALLINT     NOT NULL,
    fwd_return     NUMERIC(12, 8),
    high_return    NUMERIC(12, 8),
    universe       VARCHAR(20),
    source         VARCHAR(20)  NOT NULL DEFAULT 'daily_prices',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, date, horizon_days)
);

COMMENT ON TABLE forward_returns IS
    '종목 x 거래일 x horizon 의 미래 수익률 캐시. Phase 1 평가 인프라의 핵심 테이블. '
    '평가 반복마다 다시 계산하지 않도록 daily_prices 의 수정종가 기준으로 사전 계산.';
COMMENT ON COLUMN forward_returns.symbol IS
    '6자리 KRX 종목코드 (tickers.symbol 참조).';
COMMENT ON COLUMN forward_returns.date IS
    '기준 거래일 t. 이 row 의 fwd_return 은 date+horizon 시점까지의 수익률을 의미. hypertable 시간축.';
COMMENT ON COLUMN forward_returns.horizon_days IS
    '미래 수익률 horizon (거래일 단위, 휴장 제외). Phase 1 기본값: 1, 5, 10, 20.';
COMMENT ON COLUMN forward_returns.fwd_return IS
    '(close_{t+horizon} - close_t) / close_t. 수정종가 기준 단순 수익률. 분포는 보통 [-0.3, +0.3].';
COMMENT ON COLUMN forward_returns.high_return IS
    'horizon=1 에서만 의미: (high_{t+1} - close_t) / close_t. 그 외 horizon 에서는 NULL. '
    '사용자 단기 타겟 정의와 일치시키기 위한 별도 컬럼.';
COMMENT ON COLUMN forward_returns.universe IS
    '계산 시점의 종목 분류 (KOSPI / KOSDAQ / KOSPI200 등). NULL 가능 — universe 는 평가 시점에 재구성.';
COMMENT ON COLUMN forward_returns.source IS
    '수익률 계산에 쓴 가격 출처. 기본 daily_prices (수정종가).';
COMMENT ON COLUMN forward_returns.created_at IS
    '캐시 row 생성 시각. 가격 데이터가 갱신되면 ON CONFLICT 로 덮어쓰며 created_at 도 갱신.';

CREATE INDEX IF NOT EXISTS idx_fwd_returns_date_horizon
    ON forward_returns(date DESC, horizon_days);

CREATE INDEX IF NOT EXISTS idx_fwd_returns_symbol_horizon
    ON forward_returns(symbol, horizon_days, date DESC);

SELECT create_hypertable(
    'forward_returns', 'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);


-- =========================================================
-- 2) factor_signals - 종목 x 날짜 x factor_name 의 신호값
-- =========================================================
-- 정의:
--   raw_value      : 원본 신호 (예: 20일 모멘텀의 raw 수익률)
--   neutral_value  : 섹터 중립화 (residualize) 후 값 (선택)
--   rank_value     : Cross-sectional rank in [0, 1] (당일 종목 중 상대 순위)
--   z_score        : Cross-sectional z-score (당일 평균 0, 표준편차 1)
--
-- 한 (symbol, date, factor_name) 에 대해 위 4개 표현을 한 row 에 저장.
-- 후속 모델 학습/평가에서 원하는 형태로 즉시 가져다 쓸 수 있도록.
--
-- factor_name 명명 규칙: <family>_<param> 예: momentum_20d, reversal_5d,
-- rev_momentum_eps (consensus revision), pead_sue 등. 자유형식 텍스트.

CREATE TABLE IF NOT EXISTS factor_signals (
    symbol          VARCHAR(10)  NOT NULL,
    date            DATE         NOT NULL,
    factor_name     VARCHAR(50)  NOT NULL,
    raw_value       NUMERIC(18, 8),
    neutral_value   NUMERIC(18, 8),
    rank_value      NUMERIC(8, 6),
    z_score         NUMERIC(10, 6),
    universe        VARCHAR(20),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, date, factor_name)
);

COMMENT ON TABLE factor_signals IS
    '종목 x 거래일 x factor 의 신호값 캐시. 각 factor 는 raw / sector-neutralized / '
    'cross-sectional rank / z-score 의 4가지 표현을 한 row 에 보관해, 모델 학습이나 '
    'IC 계산에서 즉시 가져다 쓸 수 있도록 한다.';
COMMENT ON COLUMN factor_signals.symbol IS
    '6자리 KRX 종목코드.';
COMMENT ON COLUMN factor_signals.date IS
    '신호 산출 기준일. 이 날짜의 가용 정보로 계산된 신호. hypertable 시간축.';
COMMENT ON COLUMN factor_signals.factor_name IS
    '팩터 식별자 (소문자 snake_case). 예: momentum_20d, reversal_5d, '
    'rev_momentum_eps_q1, pead_sue, rec_change_30d.';
COMMENT ON COLUMN factor_signals.raw_value IS
    '팩터의 원본 값. 단위는 팩터마다 다름. 예: 모멘텀이면 수익률, EPS revision 이면 %.';
COMMENT ON COLUMN factor_signals.neutral_value IS
    '섹터 중립화 (sector residualize) 후 값. 섹터 효과가 제거되어 종목 고유의 신호만 남음. '
    'NULL 인 경우는 중립화를 하지 않은 팩터.';
COMMENT ON COLUMN factor_signals.rank_value IS
    'Cross-sectional rank in [0, 1]. 당일 universe 안에서 raw_value (또는 neutral_value) '
    '의 백분위 순위. 1.0 = 가장 높은 신호. NULL = 당일 cross-section 에서 빠짐.';
COMMENT ON COLUMN factor_signals.z_score IS
    'Cross-sectional z-score. 당일 universe 평균=0, 표준편차=1 로 정규화한 값. '
    'Winsorize 후 적용 권장 (factor_eval 모듈이 처리).';
COMMENT ON COLUMN factor_signals.universe IS
    '신호 계산에 사용된 universe 식별자. 같은 factor_name 이라도 universe 가 다르면 '
    'rank_value / z_score 가 달라지므로 메타 보존.';
COMMENT ON COLUMN factor_signals.created_at IS
    'row 생성 시각.';

CREATE INDEX IF NOT EXISTS idx_factor_signals_factor_date
    ON factor_signals(factor_name, date DESC);

CREATE INDEX IF NOT EXISTS idx_factor_signals_symbol_factor
    ON factor_signals(symbol, factor_name, date DESC);

SELECT create_hypertable(
    'factor_signals', 'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);


-- =========================================================
-- 3) eval_runs - 평가 실행 1건의 메타데이터
-- =========================================================
-- 1회의 "팩터/모델 평가" 실행을 식별하는 메타 테이블.
-- 같은 팩터를 다른 universe / period / params 로 여러 번 평가할 수
-- 있으므로 run_id 로 구분. eval_metrics 가 이 run_id 를 참조.
--
-- run_id 는 애플리케이션에서 생성 (gen_random_uuid() 사용 가능하지만
-- 가독성 위해 YYYYMMDD_HHMMSS_<factor>_<universe> 형식 권장).

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id          VARCHAR(64)   PRIMARY KEY,
    factor_name     VARCHAR(50)   NOT NULL,
    universe        VARCHAR(20)   NOT NULL,
    start_date      DATE          NOT NULL,
    end_date        DATE          NOT NULL,
    horizon_days    SMALLINT      NOT NULL,
    params          JSONB,
    n_observations  INTEGER,
    notes           TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE eval_runs IS
    '단일 팩터/모델 평가 실행의 메타데이터. 같은 팩터를 다른 universe/period/params 로 '
    '여러 번 평가할 수 있으므로 run_id 로 구분.';
COMMENT ON COLUMN eval_runs.run_id IS
    '실행 식별자. YYYYMMDD_HHMMSS_<factor>_<universe> 형식 권장 (가독성). PK.';
COMMENT ON COLUMN eval_runs.factor_name IS
    '평가된 팩터 식별자. factor_signals.factor_name 과 일치.';
COMMENT ON COLUMN eval_runs.universe IS
    '평가 universe 식별자 (KOSPI / KOSDAQ / ALL / KOSPI200 등).';
COMMENT ON COLUMN eval_runs.start_date IS
    '평가 기간 시작일 (inclusive). 거래일 기준.';
COMMENT ON COLUMN eval_runs.end_date IS
    '평가 기간 종료일 (inclusive). 거래일 기준.';
COMMENT ON COLUMN eval_runs.horizon_days IS
    '평가에 사용한 forward return horizon (1/5/10/20 등). '
    'forward_returns.horizon_days 와 매칭.';
COMMENT ON COLUMN eval_runs.params IS
    '평가 파라미터 JSON. 예: {"winsorize": 0.01, "sector_neutral": true, "min_universe_size": 100}.';
COMMENT ON COLUMN eval_runs.n_observations IS
    '평가에 사용된 (date, symbol) 페어 총 개수. universe size x 거래일 수에 근사.';
COMMENT ON COLUMN eval_runs.notes IS
    '자유 텍스트 메모. 데이터 이슈, 분석 코멘트 등.';
COMMENT ON COLUMN eval_runs.created_at IS
    '평가 실행 시각.';

CREATE INDEX IF NOT EXISTS idx_eval_runs_factor
    ON eval_runs(factor_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_universe_period
    ON eval_runs(universe, start_date, end_date);


-- =========================================================
-- 4) eval_metrics - 평가 결과 지표
-- =========================================================
-- eval_runs 1건에 대해 N 개의 metric_name -> metric_value 를 저장하는
-- LONG 형식 테이블. 향후 새 지표 (예: ICIR_3y, hit_rate_top_decile 등)
-- 를 추가해도 스키마 변경 없이 row 만 더하면 됨.
--
-- 표준 지표 (반드시 채울 것):
--   - ic_mean            : 시간 평균 IC (Pearson)
--   - ic_std             : IC 의 시간 표준편차
--   - icir               : ic_mean / ic_std
--   - ic_t_stat          : ic_mean * sqrt(N_days)
--   - rank_ic_mean       : 시간 평균 RankIC (Spearman)
--   - rank_ic_std        : RankIC 표준편차
--   - rank_icir          : rank_ic_mean / rank_ic_std
--   - hit_rate           : IC > 0 인 날의 비율
--   - long_short_sharpe  : Top-Bottom decile 포트폴리오의 Sharpe
--   - long_short_return  : 연환산 수익률
--   - max_drawdown       : 최대 손실폭
--   - turnover_avg       : 일평균 종목 교체율 (선택)
--   - n_days             : 평가 기간 영업일 수

CREATE TABLE IF NOT EXISTS eval_metrics (
    run_id          VARCHAR(64)   NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
    metric_name     VARCHAR(50)   NOT NULL,
    metric_value    NUMERIC(20, 8),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, metric_name)
);

COMMENT ON TABLE eval_metrics IS
    'eval_runs 1건에 대한 평가 지표들 (LONG 형식). 표준 지표: ic_mean / ic_std / icir / '
    'ic_t_stat / rank_ic_mean / rank_ic_std / rank_icir / hit_rate / long_short_sharpe / '
    'long_short_return / max_drawdown / turnover_avg / n_days.';
COMMENT ON COLUMN eval_metrics.run_id IS
    'eval_runs.run_id 참조. ON DELETE CASCADE.';
COMMENT ON COLUMN eval_metrics.metric_name IS
    '지표 식별자 (소문자 snake_case). 표준 지표는 ic_mean, rank_ic_mean, icir 등.';
COMMENT ON COLUMN eval_metrics.metric_value IS
    '지표 값. IC/RankIC 는 [-1, 1] 범위, Sharpe 는 보통 [-3, 3], hit_rate 는 [0, 1].';
COMMENT ON COLUMN eval_metrics.created_at IS
    '지표 기록 시각.';

CREATE INDEX IF NOT EXISTS idx_eval_metrics_name
    ON eval_metrics(metric_name, metric_value DESC);


-- =========================================================
-- 조회 편의 뷰
-- =========================================================

-- 최근 평가 결과를 한눈에 보는 wide 뷰 (run x 주요 지표)
CREATE OR REPLACE VIEW v_eval_summary AS
SELECT
    r.run_id,
    r.factor_name,
    r.universe,
    r.start_date,
    r.end_date,
    r.horizon_days,
    r.n_observations,
    MAX(CASE WHEN m.metric_name = 'ic_mean'           THEN m.metric_value END) AS ic_mean,
    MAX(CASE WHEN m.metric_name = 'rank_ic_mean'      THEN m.metric_value END) AS rank_ic_mean,
    MAX(CASE WHEN m.metric_name = 'icir'              THEN m.metric_value END) AS icir,
    MAX(CASE WHEN m.metric_name = 'rank_icir'         THEN m.metric_value END) AS rank_icir,
    MAX(CASE WHEN m.metric_name = 'ic_t_stat'         THEN m.metric_value END) AS ic_t_stat,
    MAX(CASE WHEN m.metric_name = 'hit_rate'          THEN m.metric_value END) AS hit_rate,
    MAX(CASE WHEN m.metric_name = 'long_short_sharpe' THEN m.metric_value END) AS long_short_sharpe,
    MAX(CASE WHEN m.metric_name = 'long_short_return' THEN m.metric_value END) AS long_short_return,
    MAX(CASE WHEN m.metric_name = 'max_drawdown'      THEN m.metric_value END) AS max_drawdown,
    MAX(CASE WHEN m.metric_name = 'n_days'            THEN m.metric_value END) AS n_days,
    r.created_at
  FROM eval_runs r
  LEFT JOIN eval_metrics m USING (run_id)
 GROUP BY r.run_id
 ORDER BY r.created_at DESC;

COMMENT ON VIEW v_eval_summary IS
    '평가 실행별 표준 지표를 wide 형식으로 펼친 요약 뷰. 같은 팩터를 다른 universe/period 로 '
    '실행한 결과들을 한눈에 비교할 때 사용.';
