-- =========================================
-- 016: EOD 봇 시뮬레이션 인프라
-- =========================================
--
-- 목적:
--   복합 팩터 기반 EOD 봇을 DB-only 시뮬레이션으로 실행.
--   - KIS 매매 API 는 절대 호출하지 않음 (가격 조회만)
--   - 매수/매도/잔고/포지션은 전부 이 스키마 안에서 가상으로 관리
--   - 봇은 REST API 로 생성/정지 가능, 정지 후 거래 없음
--
-- 테이블 구성:
--   1) eod_bots                — 봇 1대의 정체성 + 현재 상태 + 현금
--   2) eod_bot_spec_history    — 봇별 spec 변경 이력 (append-only)
--   3) eod_bot_orders          — 매수/매도 가상 체결 기록
--   4) eod_bot_positions       — (bot, date, symbol) 일별 포지션 스냅샷
--   5) eod_bot_daily_pnl       — (bot, date) 일별 PnL/누적수익률
--   6) eod_bot_runs            — 일별 봇 실행 로그 (성공/실패/스킵)
--
-- 상태 모델 (eod_bots.state):
--   PENDING  - 생성됨, 아직 한 번도 daily tick 안 돔
--   RUNNING  - 정상 동작 중
--   STOPPED  - 정지됨. 정지 시점의 final_pnl/final_return_pct 기록.
--              이후 어떤 daily tick 도 거래를 만들지 않음 (불변).


-- =========================================================
-- 1) eod_bots — 봇 마스터
-- =========================================================
CREATE TABLE IF NOT EXISTS eod_bots (
    bot_id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(64)     NOT NULL UNIQUE,
    state               VARCHAR(16)     NOT NULL DEFAULT 'PENDING',
    strategy_kind       VARCHAR(16)     NOT NULL,
    declarative_spec    JSONB,
    plugin_strategy_id  VARCHAR(64),
    universe            VARCHAR(20)     NOT NULL,
    seed_cash           NUMERIC(18, 2)  NOT NULL,
    cash                NUMERIC(18, 2)  NOT NULL,
    holdings_value      NUMERIC(18, 2)  NOT NULL DEFAULT 0,
    total_value         NUMERIC(18, 2)  NOT NULL,
    last_tick_date      DATE,
    last_tick_run_id    UUID,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    stopped_at          TIMESTAMPTZ,
    final_pnl           NUMERIC(18, 2),
    final_return_pct    NUMERIC(12, 6),
    final_total_value   NUMERIC(18, 2),
    notes               TEXT,
    CONSTRAINT eod_bots_state_chk CHECK (state IN ('PENDING', 'RUNNING', 'STOPPED')),
    CONSTRAINT eod_bots_kind_chk  CHECK (strategy_kind IN ('declarative', 'plugin')),
    CONSTRAINT eod_bots_spec_chk  CHECK (
        (strategy_kind = 'declarative' AND declarative_spec IS NOT NULL)
     OR (strategy_kind = 'plugin'      AND plugin_strategy_id IS NOT NULL)
    ),
    CONSTRAINT eod_bots_cash_chk  CHECK (cash >= 0 AND seed_cash > 0)
);

COMMENT ON TABLE eod_bots IS
    'EOD 봇 마스터. 봇 1대 = 1 row. KIS 매매 API 는 호출하지 않으며 매수/매도/잔고는 '
    '전부 이 시스템 안에서 가상으로 관리된다. state=STOPPED 가 되면 어떤 daily tick 도 '
    '거래를 만들지 않는다 (불변 규칙).';
COMMENT ON COLUMN eod_bots.bot_id IS
    '봇 식별자 (UUID). PK. REST API 의 path parameter 로 사용.';
COMMENT ON COLUMN eod_bots.name IS
    '사람이 읽는 봇 이름. UNIQUE — 같은 이름 중복 생성 불가.';
COMMENT ON COLUMN eod_bots.state IS
    '봇 상태. PENDING(생성 직후) / RUNNING(daily tick 진행 중) / STOPPED(정지, 거래 영구 동결).';
COMMENT ON COLUMN eod_bots.strategy_kind IS
    '전략 표현 방식. declarative = JSONB spec 으로 명세, plugin = 코드 구현체 ID 로 참조.';
COMMENT ON COLUMN eod_bots.declarative_spec IS
    'declarative 전략의 JSONB 명세. strategy_kind=declarative 일 때 필수. '
    'src.trading.strategy.declarative.StrategySpec 의 Pydantic 모델 구조와 일치.';
COMMENT ON COLUMN eod_bots.plugin_strategy_id IS
    'plugin 전략의 식별자. src.trading.strategy.registry 에 등록된 이름. '
    'strategy_kind=plugin 일 때 필수.';
COMMENT ON COLUMN eod_bots.universe IS
    '봇이 거래하는 종목 universe. KOSPI / KOSDAQ / ALL / KOSPI200.';
COMMENT ON COLUMN eod_bots.seed_cash IS
    '생성 시점 시드 자본 (KRW). 불변. 누적 수익률 계산 분모.';
COMMENT ON COLUMN eod_bots.cash IS
    '현재 보유 현금 (KRW). 매수 시 차감, 매도 시 가산. 음수 불가.';
COMMENT ON COLUMN eod_bots.holdings_value IS
    '현재 보유 종목 평가액 (KRW). 가장 최근 daily tick 의 종가 기준.';
COMMENT ON COLUMN eod_bots.total_value IS
    'cash + holdings_value. UI/조회용 캐시. tick 마다 갱신.';
COMMENT ON COLUMN eod_bots.last_tick_date IS
    '가장 최근 daily tick 의 결정일. NULL = PENDING 상태.';
COMMENT ON COLUMN eod_bots.last_tick_run_id IS
    '가장 최근 daily tick 의 eod_bot_runs.run_id.';
COMMENT ON COLUMN eod_bots.created_at IS
    '봇 생성 시각 (UTC).';
COMMENT ON COLUMN eod_bots.started_at IS
    '봇이 처음 RUNNING 상태로 전이된 시각.';
COMMENT ON COLUMN eod_bots.stopped_at IS
    '봇이 STOPPED 상태로 전이된 시각.';
COMMENT ON COLUMN eod_bots.final_pnl IS
    '정지 시점의 누적 손익 (KRW). total_value - seed_cash. STOPPED 일 때만 NOT NULL.';
COMMENT ON COLUMN eod_bots.final_return_pct IS
    '정지 시점의 누적 수익률 (소수). final_pnl / seed_cash.';
COMMENT ON COLUMN eod_bots.final_total_value IS
    '정지 시점의 총 평가액.';
COMMENT ON COLUMN eod_bots.notes IS
    '자유 텍스트 메모. 정지 사유, 운용 메모 등.';

CREATE INDEX IF NOT EXISTS idx_eod_bots_state    ON eod_bots(state);
CREATE INDEX IF NOT EXISTS idx_eod_bots_kind     ON eod_bots(strategy_kind);
CREATE INDEX IF NOT EXISTS idx_eod_bots_universe ON eod_bots(universe);


-- =========================================================
-- 2) eod_bot_spec_history — spec 변경 이력 (append-only)
-- =========================================================
CREATE TABLE IF NOT EXISTS eod_bot_spec_history (
    spec_history_id     UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_id              UUID            NOT NULL REFERENCES eod_bots(bot_id) ON DELETE CASCADE,
    spec_version        INT             NOT NULL,
    strategy_kind       VARCHAR(16)     NOT NULL,
    spec_json           JSONB,
    plugin_strategy_id  VARCHAR(64),
    required_factors    TEXT[],
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_by          VARCHAR(32)     NOT NULL DEFAULT 'api',
    reason              TEXT,
    UNIQUE (bot_id, spec_version),
    CONSTRAINT eod_spec_kind_chk CHECK (strategy_kind IN ('declarative', 'plugin')),
    CONSTRAINT eod_spec_body_chk CHECK (
        (strategy_kind = 'declarative' AND spec_json IS NOT NULL)
     OR (strategy_kind = 'plugin'      AND plugin_strategy_id IS NOT NULL)
    )
);

COMMENT ON TABLE eod_bot_spec_history IS
    '봇별 전략 spec 변경 이력. append-only. 봇이 매수/매도한 시점에 어떤 spec 으로 '
    '결정됐는지 eod_bot_orders.spec_history_id 가 이 테이블을 참조한다.';
COMMENT ON COLUMN eod_bot_spec_history.spec_history_id IS
    'spec 이력 식별자. PK. eod_bot_orders.spec_history_id 가 FK 로 참조.';
COMMENT ON COLUMN eod_bot_spec_history.bot_id IS
    '봇 ID. eod_bots.bot_id 참조. ON DELETE CASCADE.';
COMMENT ON COLUMN eod_bot_spec_history.spec_version IS
    '봇 내부 spec 버전 (1부터 증가). UNIQUE (bot_id, spec_version).';
COMMENT ON COLUMN eod_bot_spec_history.strategy_kind IS
    'declarative 또는 plugin.';
COMMENT ON COLUMN eod_bot_spec_history.spec_json IS
    'declarative 전략의 JSONB 명세 스냅샷.';
COMMENT ON COLUMN eod_bot_spec_history.plugin_strategy_id IS
    'plugin 전략의 식별자 스냅샷.';
COMMENT ON COLUMN eod_bot_spec_history.required_factors IS
    '이 spec 이 의존하는 factor_name 배열. factor production 부분 실패 시 영향받는 '
    '봇을 한 SQL 로 조회하기 위함.';
COMMENT ON COLUMN eod_bot_spec_history.created_at IS
    'spec 변경 시각.';
COMMENT ON COLUMN eod_bot_spec_history.created_by IS
    'spec 을 생성/변경한 주체. "api" / "system" / "user".';
COMMENT ON COLUMN eod_bot_spec_history.reason IS
    '변경 사유 자유 텍스트.';

CREATE INDEX IF NOT EXISTS idx_eod_spec_bot
    ON eod_bot_spec_history(bot_id, spec_version DESC);
CREATE INDEX IF NOT EXISTS idx_eod_spec_factors
    ON eod_bot_spec_history USING GIN(required_factors);


-- =========================================================
-- 3) eod_bot_orders — 매수/매도 가상 체결 기록
-- =========================================================
CREATE TABLE IF NOT EXISTS eod_bot_orders (
    order_id            UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_id              UUID            NOT NULL REFERENCES eod_bots(bot_id) ON DELETE CASCADE,
    spec_history_id     UUID            REFERENCES eod_bot_spec_history(spec_history_id),
    run_id              UUID,
    side                VARCHAR(8)      NOT NULL,
    symbol              VARCHAR(10)     NOT NULL,
    decision_date       DATE            NOT NULL,
    fill_date           DATE            NOT NULL,
    quantity            INTEGER         NOT NULL,
    fill_price          NUMERIC(14, 2)  NOT NULL,
    fill_value          NUMERIC(18, 2)  NOT NULL,
    fee                 NUMERIC(14, 2)  NOT NULL DEFAULT 0,
    slippage_bps        NUMERIC(8, 2)   NOT NULL DEFAULT 0,
    composite_score     NUMERIC(10, 6),
    cash_before         NUMERIC(18, 2),
    cash_after          NUMERIC(18, 2),
    fill_source         VARCHAR(20)     NOT NULL DEFAULT 'daily_prices',
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT eod_orders_side_chk CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT eod_orders_qty_chk  CHECK (quantity > 0),
    CONSTRAINT eod_orders_price_chk CHECK (fill_price > 0)
);

COMMENT ON TABLE eod_bot_orders IS
    'EOD 봇의 가상 매수/매도 체결 기록. KIS 매매 API 는 호출되지 않으며 모든 매매가 '
    '여기에만 존재한다. 체결가는 daily_prices 에서 조회한 시초가 또는 종가.';
COMMENT ON COLUMN eod_bot_orders.order_id IS
    '주문 식별자 (UUID). PK.';
COMMENT ON COLUMN eod_bot_orders.bot_id IS
    '봇 ID. ON DELETE CASCADE.';
COMMENT ON COLUMN eod_bot_orders.spec_history_id IS
    '이 주문이 결정된 시점의 spec 버전.';
COMMENT ON COLUMN eod_bot_orders.run_id IS
    '이 주문을 생성한 daily tick 의 run_id.';
COMMENT ON COLUMN eod_bot_orders.side IS
    '주문 방향. BUY 또는 SELL.';
COMMENT ON COLUMN eod_bot_orders.symbol IS
    '6자리 KRX 종목코드.';
COMMENT ON COLUMN eod_bot_orders.decision_date IS
    '시그널이 계산된 날짜 (T).';
COMMENT ON COLUMN eod_bot_orders.fill_date IS
    '체결이 일어난 날짜. 일반적으로 decision_date + 1 영업일.';
COMMENT ON COLUMN eod_bot_orders.quantity IS
    '체결 수량 (주).';
COMMENT ON COLUMN eod_bot_orders.fill_price IS
    '체결 단가 (KRW/주). slippage 가 이미 반영된 값.';
COMMENT ON COLUMN eod_bot_orders.fill_value IS
    'quantity × fill_price (수수료 제외).';
COMMENT ON COLUMN eod_bot_orders.fee IS
    '수수료 (KRW). BUY: 거래 수수료. SELL: 거래 수수료 + 거래세.';
COMMENT ON COLUMN eod_bot_orders.slippage_bps IS
    '체결 슬리피지 (bps). spec.execution.slippage_bps 값.';
COMMENT ON COLUMN eod_bot_orders.composite_score IS
    '매수 결정 시점의 종합 시그널 점수.';
COMMENT ON COLUMN eod_bot_orders.cash_before IS
    '이 주문 직전 봇 cash 잔액.';
COMMENT ON COLUMN eod_bot_orders.cash_after IS
    '이 주문 직후 봇 cash 잔액.';
COMMENT ON COLUMN eod_bot_orders.fill_source IS
    '체결가 출처. "daily_prices"(수정종가) / "daily_prices_raw"(원주가).';
COMMENT ON COLUMN eod_bot_orders.created_at IS
    'row 생성 시각.';

CREATE INDEX IF NOT EXISTS idx_eod_orders_bot_date ON eod_bot_orders(bot_id, fill_date DESC);
CREATE INDEX IF NOT EXISTS idx_eod_orders_run      ON eod_bot_orders(run_id);
CREATE INDEX IF NOT EXISTS idx_eod_orders_symbol   ON eod_bot_orders(symbol);


-- =========================================================
-- 4) eod_bot_positions — 일별 포지션 스냅샷
-- =========================================================
CREATE TABLE IF NOT EXISTS eod_bot_positions (
    bot_id              UUID            NOT NULL REFERENCES eod_bots(bot_id) ON DELETE CASCADE,
    date                DATE            NOT NULL,
    symbol              VARCHAR(10)     NOT NULL,
    quantity            INTEGER         NOT NULL,
    avg_cost            NUMERIC(14, 2)  NOT NULL,
    market_price        NUMERIC(14, 2),
    market_value        NUMERIC(18, 2),
    unrealized_pnl      NUMERIC(18, 2),
    weight_pct          NUMERIC(8, 6),
    sector              VARCHAR(100),
    composite_score     NUMERIC(10, 6),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bot_id, date, symbol),
    CONSTRAINT eod_pos_qty_chk CHECK (quantity > 0)
);

COMMENT ON TABLE eod_bot_positions IS
    'EOD 봇의 일별 포지션 스냅샷. daily tick 끝에서 보유 중인 모든 종목을 1줄씩 저장. '
    '어제 보유 → 오늘도 보유 = 어제와 오늘 둘 다 row 가 있음 (중복 저장 의도적).';
COMMENT ON COLUMN eod_bot_positions.bot_id IS
    '봇 ID. ON DELETE CASCADE.';
COMMENT ON COLUMN eod_bot_positions.date IS
    '스냅샷 날짜 (보통 decision_date).';
COMMENT ON COLUMN eod_bot_positions.symbol IS
    '6자리 KRX 종목코드.';
COMMENT ON COLUMN eod_bot_positions.quantity IS
    '보유 수량 (주). 0 인 종목은 row 자체가 없음.';
COMMENT ON COLUMN eod_bot_positions.avg_cost IS
    '평균 매입 단가 (KRW/주).';
COMMENT ON COLUMN eod_bot_positions.market_price IS
    '이 날짜 종가 (KRW/주). 휴장일에는 NULL 가능.';
COMMENT ON COLUMN eod_bot_positions.market_value IS
    'quantity × market_price.';
COMMENT ON COLUMN eod_bot_positions.unrealized_pnl IS
    'quantity × (market_price - avg_cost).';
COMMENT ON COLUMN eod_bot_positions.weight_pct IS
    '이 종목의 봇 자산 내 비중 (0~1 소수).';
COMMENT ON COLUMN eod_bot_positions.sector IS
    '종목 섹터 (tickers.sector 의 스냅샷).';
COMMENT ON COLUMN eod_bot_positions.composite_score IS
    '이 종목의 그날 시그널 점수.';
COMMENT ON COLUMN eod_bot_positions.created_at IS
    'row 생성 시각.';

CREATE INDEX IF NOT EXISTS idx_eod_pos_bot_date ON eod_bot_positions(bot_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_eod_pos_symbol   ON eod_bot_positions(symbol, date DESC);


-- =========================================================
-- 5) eod_bot_daily_pnl — 일별 봇 단위 PnL
-- =========================================================
CREATE TABLE IF NOT EXISTS eod_bot_daily_pnl (
    bot_id                  UUID            NOT NULL REFERENCES eod_bots(bot_id) ON DELETE CASCADE,
    date                    DATE            NOT NULL,
    cash                    NUMERIC(18, 2)  NOT NULL,
    holdings_value          NUMERIC(18, 2)  NOT NULL,
    total_value             NUMERIC(18, 2)  NOT NULL,
    daily_return_pct        NUMERIC(12, 8),
    cumulative_return_pct   NUMERIC(12, 8),
    drawdown_pct            NUMERIC(12, 8),
    peak_total_value        NUMERIC(18, 2),
    trades_count            INTEGER         NOT NULL DEFAULT 0,
    buy_value               NUMERIC(18, 2)  NOT NULL DEFAULT 0,
    sell_value              NUMERIC(18, 2)  NOT NULL DEFAULT 0,
    fee_total               NUMERIC(14, 2)  NOT NULL DEFAULT 0,
    turnover_pct            NUMERIC(8, 6),
    n_positions             INTEGER         NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bot_id, date)
);

COMMENT ON TABLE eod_bot_daily_pnl IS
    'EOD 봇의 일별 단위 PnL. daily tick 마다 1 row. 봇 누적 수익률 차트와 risk 분석의 source.';
COMMENT ON COLUMN eod_bot_daily_pnl.bot_id IS
    '봇 ID. ON DELETE CASCADE.';
COMMENT ON COLUMN eod_bot_daily_pnl.date IS
    'PnL 기준일 (decision_date).';
COMMENT ON COLUMN eod_bot_daily_pnl.cash IS
    '이 날짜 종가 평가 시점의 봇 현금.';
COMMENT ON COLUMN eod_bot_daily_pnl.holdings_value IS
    '이 날짜 종가 평가 시점의 보유 종목 평가액 합계.';
COMMENT ON COLUMN eod_bot_daily_pnl.total_value IS
    'cash + holdings_value. 봇 총 자산.';
COMMENT ON COLUMN eod_bot_daily_pnl.daily_return_pct IS
    '직전 영업일 대비 일간 수익률 (소수). 첫 row 는 NULL.';
COMMENT ON COLUMN eod_bot_daily_pnl.cumulative_return_pct IS
    '시드 자본 대비 누적 수익률 (소수).';
COMMENT ON COLUMN eod_bot_daily_pnl.drawdown_pct IS
    'peak 대비 낙폭 (0 이하 값).';
COMMENT ON COLUMN eod_bot_daily_pnl.peak_total_value IS
    '봇 시작 이후 이 날짜까지의 최대 total_value.';
COMMENT ON COLUMN eod_bot_daily_pnl.trades_count IS
    '이 날짜 daily tick 에서 발생한 매매 건수 (BUY + SELL).';
COMMENT ON COLUMN eod_bot_daily_pnl.buy_value IS
    '이 날짜 매수 체결 금액 합계 (KRW, 수수료 제외).';
COMMENT ON COLUMN eod_bot_daily_pnl.sell_value IS
    '이 날짜 매도 체결 금액 합계 (KRW, 수수료 제외).';
COMMENT ON COLUMN eod_bot_daily_pnl.fee_total IS
    '이 날짜 수수료 합계 (KRW).';
COMMENT ON COLUMN eod_bot_daily_pnl.turnover_pct IS
    '일 회전율 = (buy_value + sell_value) / (2 × yesterday_total_value).';
COMMENT ON COLUMN eod_bot_daily_pnl.n_positions IS
    '이 날짜 종가 시점의 보유 종목 수.';
COMMENT ON COLUMN eod_bot_daily_pnl.created_at IS
    'row 생성 시각.';

CREATE INDEX IF NOT EXISTS idx_eod_pnl_bot ON eod_bot_daily_pnl(bot_id, date DESC);


-- =========================================================
-- 6) eod_bot_runs — 일별 봇 실행 로그
-- =========================================================
CREATE TABLE IF NOT EXISTS eod_bot_runs (
    run_id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_id                  UUID            NOT NULL REFERENCES eod_bots(bot_id) ON DELETE CASCADE,
    decision_date           DATE            NOT NULL,
    spec_history_id         UUID            REFERENCES eod_bot_spec_history(spec_history_id),
    status                  VARCHAR(16)     NOT NULL,
    started_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    finished_at             TIMESTAMPTZ,
    duration_ms             INTEGER,
    universe_size           INTEGER,
    eligible_size           INTEGER,
    scored_size             INTEGER,
    factor_coverage         JSONB,
    target_size             INTEGER,
    n_buys                  INTEGER         NOT NULL DEFAULT 0,
    n_sells                 INTEGER         NOT NULL DEFAULT 0,
    skip_reason             VARCHAR(64),
    error_message           TEXT,
    UNIQUE (bot_id, decision_date),
    CONSTRAINT eod_runs_status_chk CHECK (status IN ('SUCCESS', 'SKIPPED', 'FAILED'))
);

COMMENT ON TABLE eod_bot_runs IS
    '봇별 daily tick 실행 로그. (bot_id, decision_date) UNIQUE — 같은 날 두 번 tick 방지.';
COMMENT ON COLUMN eod_bot_runs.run_id IS
    'daily tick 실행 식별자 (UUID). PK. eod_bot_orders.run_id 가 참조.';
COMMENT ON COLUMN eod_bot_runs.bot_id IS
    '봇 ID. ON DELETE CASCADE.';
COMMENT ON COLUMN eod_bot_runs.decision_date IS
    '이 tick 의 시그널 결정일. UNIQUE(bot_id, decision_date).';
COMMENT ON COLUMN eod_bot_runs.spec_history_id IS
    '이 tick 시점에 사용된 spec 버전.';
COMMENT ON COLUMN eod_bot_runs.status IS
    '실행 결과. SUCCESS / SKIPPED / FAILED.';
COMMENT ON COLUMN eod_bot_runs.started_at IS
    'tick 시작 시각.';
COMMENT ON COLUMN eod_bot_runs.finished_at IS
    'tick 종료 시각.';
COMMENT ON COLUMN eod_bot_runs.duration_ms IS
    '소요 시간 (ms). 성능 모니터링용.';
COMMENT ON COLUMN eod_bot_runs.universe_size IS
    'universe (예: KOSPI) 의 종목 수.';
COMMENT ON COLUMN eod_bot_runs.eligible_size IS
    'universe 중 risk filter 통과 종목 수.';
COMMENT ON COLUMN eod_bot_runs.scored_size IS
    'eligible 중 시그널 점수가 계산된 종목 수.';
COMMENT ON COLUMN eod_bot_runs.factor_coverage IS
    '팩터별 데이터 보유 종목 수 JSONB. 디버깅용.';
COMMENT ON COLUMN eod_bot_runs.target_size IS
    '시그널 결과로 선택된 목표 보유 종목 수.';
COMMENT ON COLUMN eod_bot_runs.n_buys IS
    '이 tick 에서 발생한 매수 체결 건수.';
COMMENT ON COLUMN eod_bot_runs.n_sells IS
    '이 tick 에서 발생한 매도 체결 건수.';
COMMENT ON COLUMN eod_bot_runs.skip_reason IS
    'status=SKIPPED 일 때 사유 코드.';
COMMENT ON COLUMN eod_bot_runs.error_message IS
    'status=FAILED 일 때 예외 메시지.';

CREATE INDEX IF NOT EXISTS idx_eod_runs_bot_date ON eod_bot_runs(bot_id, decision_date DESC);
CREATE INDEX IF NOT EXISTS idx_eod_runs_status   ON eod_bot_runs(status, started_at DESC);


-- =========================================================
-- 조회 편의 뷰
-- =========================================================

CREATE OR REPLACE VIEW v_eod_bot_summary AS
SELECT
    b.bot_id,
    b.name,
    b.state,
    b.strategy_kind,
    b.plugin_strategy_id,
    b.universe,
    b.seed_cash,
    b.cash,
    b.holdings_value,
    b.total_value,
    (b.total_value - b.seed_cash)               AS pnl,
    CASE WHEN b.seed_cash > 0
         THEN (b.total_value - b.seed_cash) / b.seed_cash
         ELSE NULL
    END                                          AS return_pct,
    b.last_tick_date,
    b.created_at,
    b.started_at,
    b.stopped_at,
    b.final_pnl,
    b.final_return_pct,
    (SELECT COUNT(*) FROM eod_bot_orders o WHERE o.bot_id = b.bot_id) AS total_orders,
    (SELECT MAX(spec_version) FROM eod_bot_spec_history h WHERE h.bot_id = b.bot_id) AS current_spec_version
FROM eod_bots b;

COMMENT ON VIEW v_eod_bot_summary IS
    '봇별 한 줄 요약. REST API GET /bots 의 응답 소스로 사용.';


CREATE OR REPLACE VIEW v_eod_bot_equity_curve AS
SELECT
    p.bot_id,
    b.name AS bot_name,
    p.date,
    p.total_value,
    p.cash,
    p.holdings_value,
    p.daily_return_pct,
    p.cumulative_return_pct,
    p.drawdown_pct,
    p.n_positions,
    p.trades_count,
    p.turnover_pct
FROM eod_bot_daily_pnl p
JOIN eod_bots b ON b.bot_id = p.bot_id
ORDER BY p.bot_id, p.date;

COMMENT ON VIEW v_eod_bot_equity_curve IS
    '봇별 일별 equity curve. 차트 작성용 wide view.';
