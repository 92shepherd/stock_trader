-- =========================================
-- 015: Phase 2 모델 인프라 - LightGBM 멀티팩터 모델 영속화
-- =========================================
--
-- 목적:
--   Phase 1 의 eval_runs / eval_metrics 는 "단일 팩터의 알파 검증" 용
--   이었다. Phase 2 에서는 여러 팩터를 입력으로 받는 ML 모델이 등장
--   하며, 모델은 "예측값" 을 만들어낸다 (단일 팩터는 만들지 않음).
--   예측값을 영속화하고 모델 메타를 따로 관리하기 위한 3개 테이블.
--
-- 테이블 구성:
--   1) model_runs              - 모델 학습 1회의 메타 (run_id, type,
--                                 train/test 기간, hyperparams, 입력 피처)
--   2) model_predictions       - 종목 x 날짜 x run_id 의 예측값 (hypertable)
--   3) model_feature_importance- run_id x feature_name x importance
--                                 (LightGBM gain 기반 importance)
--
-- 영속화 정책:
--   - 한 model_run 은 한번의 학습 + 평가 사이클. walk-forward 학습은
--     N 개의 run_id 를 만든다 (각 fold 가 별도 run).
--   - 같은 모델 설정을 다른 데이터로 재학습하면 새 run_id (replay).
--   - predictions 는 test 기간 cross-section 의 점추정. rank/z-score
--     변환은 평가 시점에 on-the-fly 계산 (Phase 1 의 ranking 모듈 사용).
--
-- Phase 1 과의 관계:
--   - eval_runs / eval_metrics 는 그대로 사용. 단, eval_runs.factor_name
--     컬럼에 모델의 경우 'model_<type>' (예: 'model_lgbm_reg') 을 기록.
--   - 즉, model_runs 는 모델 메타, eval_runs 는 그 모델의 평가 결과를
--     포함. 둘은 run_id 를 동일하게 쓰지 않는다 (모델은 학습 1회, 평가
--     는 N 회 가능). 대신 eval_runs.notes 에 model_run_id 를 적어
--     참조한다.


-- =========================================================
-- 1) model_runs - 모델 학습 1회의 메타데이터
-- =========================================================
CREATE TABLE IF NOT EXISTS model_runs (
    model_run_id    VARCHAR(64)   PRIMARY KEY,
    model_type      VARCHAR(30)   NOT NULL,
    universe        VARCHAR(20)   NOT NULL,
    train_start     DATE          NOT NULL,
    train_end       DATE          NOT NULL,
    test_start      DATE          NOT NULL,
    test_end        DATE          NOT NULL,
    horizon_days    SMALLINT      NOT NULL,
    target_type     VARCHAR(20)   NOT NULL DEFAULT 'regression',
    features        JSONB,
    hyperparams     JSONB,
    n_train_rows    INTEGER,
    n_test_rows     INTEGER,
    train_loss      NUMERIC(20, 8),
    valid_loss      NUMERIC(20, 8),
    notes           TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE model_runs IS
    'LightGBM (등) ML 모델 학습 1회의 메타데이터. walk-forward 학습에서 각 fold = 1 run.';
COMMENT ON COLUMN model_runs.model_run_id IS
    '학습 실행 식별자. YYYYMMDD_HHMMSS_<type>_<universe>_fold<N> 형식 권장. PK.';
COMMENT ON COLUMN model_runs.model_type IS
    '모델 종류. 예: lgbm_reg, lgbm_rank, lgbm_cls, ridge_reg.';
COMMENT ON COLUMN model_runs.universe IS
    '학습/평가 universe (KOSPI / KOSDAQ / ALL / KOSPI200).';
COMMENT ON COLUMN model_runs.train_start IS
    '학습 데이터 시작일 (inclusive).';
COMMENT ON COLUMN model_runs.train_end IS
    '학습 데이터 종료일 (inclusive).';
COMMENT ON COLUMN model_runs.test_start IS
    '테스트 데이터 시작일 (inclusive). train_end + gap_days 이상 떨어져야 leakage 방지.';
COMMENT ON COLUMN model_runs.test_end IS
    '테스트 데이터 종료일 (inclusive).';
COMMENT ON COLUMN model_runs.horizon_days IS
    'forward return horizon (forward_returns.horizon_days 와 일치).';
COMMENT ON COLUMN model_runs.target_type IS
    '타겟 방식. regression(default) / ranking / classification.';
COMMENT ON COLUMN model_runs.features IS
    '학습에 사용한 피처 이름들 JSONB array. 예: ["momentum_20d", "rev_eps_1m", "quality_roe"].';
COMMENT ON COLUMN model_runs.hyperparams IS
    '모델 하이퍼파라미터 JSON. LightGBM: {"num_leaves": 31, "learning_rate": 0.05, ...}.';
COMMENT ON COLUMN model_runs.n_train_rows IS
    '학습에 실제로 사용된 (date, symbol) 페어 수 (NaN drop 후).';
COMMENT ON COLUMN model_runs.n_test_rows IS
    '테스트에 사용된 행 수.';
COMMENT ON COLUMN model_runs.train_loss IS
    '학습 끝 시점의 학습 loss (MSE 등). 모델/타겟에 따라 의미가 다름.';
COMMENT ON COLUMN model_runs.valid_loss IS
    'validation set (조기 종료용) loss. validation 미사용 시 NULL.';
COMMENT ON COLUMN model_runs.notes IS
    '자유 텍스트 메모. 예: "walk-forward fold 3/12".';
COMMENT ON COLUMN model_runs.created_at IS
    '학습 완료 시각.';

CREATE INDEX IF NOT EXISTS idx_model_runs_type_universe
    ON model_runs(model_type, universe, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_runs_period
    ON model_runs(test_start, test_end);


-- =========================================================
-- 2) model_predictions - 종목 x 날짜 x run 의 예측값 (hypertable)
-- =========================================================
CREATE TABLE IF NOT EXISTS model_predictions (
    model_run_id    VARCHAR(64)   NOT NULL,
    symbol          VARCHAR(10)   NOT NULL,
    date            DATE          NOT NULL,
    horizon_days    SMALLINT      NOT NULL,
    y_pred          NUMERIC(18, 8),
    y_true          NUMERIC(18, 8),
    rank_value      NUMERIC(8, 6),
    universe        VARCHAR(20),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (model_run_id, symbol, date)
);

COMMENT ON TABLE model_predictions IS
    '모델 예측값 (test set). 종목 x 날짜 x run_id 의 long format. y_true 도 같이 저장해서 '
    '재평가 시 forward_returns 다시 join 안 해도 됨. hypertable (1 month 청크).';
COMMENT ON COLUMN model_predictions.model_run_id IS
    'model_runs.model_run_id 참조 (FK 미설정 - hypertable 제약).';
COMMENT ON COLUMN model_predictions.symbol IS
    '6자리 KRX 종목코드.';
COMMENT ON COLUMN model_predictions.date IS
    '예측 기준일 t. y_pred 는 (t+horizon_days) 시점의 수익률 예측. hypertable 시간축.';
COMMENT ON COLUMN model_predictions.horizon_days IS
    '예측 horizon. forward_returns.horizon_days 와 일치.';
COMMENT ON COLUMN model_predictions.y_pred IS
    '모델의 raw 예측값. 회귀: 예측 수익률. 분류: positive class probability.';
COMMENT ON COLUMN model_predictions.y_true IS
    '실측 forward return (있을 때만; 미래 예측 시 NULL).';
COMMENT ON COLUMN model_predictions.rank_value IS
    '같은 (model_run_id, date) 내 y_pred 의 cross-sectional percentile rank [0, 1]. '
    '평가 시 cross-sectional 비교를 위해 미리 저장. NULL 이면 호출자가 사후 계산.';
COMMENT ON COLUMN model_predictions.universe IS
    '예측 시점 universe 식별자 (메타).';
COMMENT ON COLUMN model_predictions.created_at IS
    '예측 row 생성 시각.';

CREATE INDEX IF NOT EXISTS idx_model_preds_run_date
    ON model_predictions(model_run_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_model_preds_date
    ON model_predictions(date DESC, model_run_id);

SELECT create_hypertable(
    'model_predictions', 'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);


-- =========================================================
-- 3) model_feature_importance - feature 별 중요도
-- =========================================================
CREATE TABLE IF NOT EXISTS model_feature_importance (
    model_run_id    VARCHAR(64)   NOT NULL REFERENCES model_runs(model_run_id) ON DELETE CASCADE,
    feature_name    VARCHAR(50)   NOT NULL,
    importance      NUMERIC(20, 8),
    importance_type VARCHAR(20)   NOT NULL DEFAULT 'gain',
    rank            SMALLINT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (model_run_id, feature_name, importance_type)
);

COMMENT ON TABLE model_feature_importance IS
    '모델별 피처 중요도. LightGBM 의 split/gain importance 또는 다른 모델의 동등 지표. '
    'feature engineering 진단 + 알파 디케이 추적용.';
COMMENT ON COLUMN model_feature_importance.model_run_id IS
    'model_runs.model_run_id 참조. ON DELETE CASCADE.';
COMMENT ON COLUMN model_feature_importance.feature_name IS
    '피처 이름. factor_signals.factor_name 과 일치하거나 (예: momentum_20d) 모델 내부 '
    '파생 피처일 수 있음.';
COMMENT ON COLUMN model_feature_importance.importance IS
    '중요도 점수. gain 기반은 보통 [0, +inf), split 기반은 정수 (분기 횟수).';
COMMENT ON COLUMN model_feature_importance.importance_type IS
    'gain | split | permutation 등. 기본 gain.';
COMMENT ON COLUMN model_feature_importance.rank IS
    '같은 (model_run_id, importance_type) 내 순위 (1=가장 중요). NULL 가능.';
COMMENT ON COLUMN model_feature_importance.created_at IS
    'row 생성 시각.';

CREATE INDEX IF NOT EXISTS idx_model_fi_run
    ON model_feature_importance(model_run_id, importance_type, rank);


-- =========================================================
-- 조회 편의 뷰 - 모델 + 평가 결과 join (eval_runs.notes 에 model_run_id)
-- =========================================================
CREATE OR REPLACE VIEW v_model_eval_summary AS
SELECT
    mr.model_run_id,
    mr.model_type,
    mr.universe,
    mr.train_start, mr.train_end,
    mr.test_start, mr.test_end,
    mr.horizon_days,
    mr.target_type,
    mr.n_train_rows, mr.n_test_rows,
    mr.train_loss, mr.valid_loss,
    es.run_id          AS eval_run_id,
    es.ic_mean,
    es.rank_ic_mean,
    es.icir,
    es.rank_icir,
    es.hit_rate,
    es.long_short_sharpe,
    es.long_short_return,
    es.max_drawdown,
    mr.created_at
  FROM model_runs mr
  LEFT JOIN (
      SELECT v.*, r.notes
        FROM v_eval_summary v
        JOIN eval_runs r USING (run_id)
  ) es ON es.notes LIKE '%' || mr.model_run_id || '%'
 ORDER BY mr.created_at DESC;

COMMENT ON VIEW v_model_eval_summary IS
    '모델 학습 1건 + 그 모델의 평가 결과 (v_eval_summary) 를 join 한 wide 뷰. '
    'eval_runs.notes 에 model_run_id 가 포함되어 있어야 매칭됨 - trainer 가 자동 처리.';
