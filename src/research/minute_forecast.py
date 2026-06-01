"""Per-symbol LightGBM minute-bar price forecast.

개요:
    특정 종목에 대해 직전 한달치 분봉 + 일봉/공시/컨센서스 피처를 기반으로
    당일 또는 익일 장 운영시간(09:00~15:30, 390분봉)의 close 수익률을 예측.

예측 대상:
    predicted_return = (close_t - prev_close) / prev_close
    predicted_close  = prev_close × (1 + predicted_return)

    'prev_close'는 예측 대상 날짜의 전일 종가.

호출 규칙:
    - 08:00 이전: 당일 장 예측 생성
    - 08:00 이후: 다음 영업일 장 예측 생성
    - 이미 같은 (symbol, ts)가 존재하면 upsert (갱신)

모델 설계:
    - LightGBM Regressor (per-symbol 전용)
    - Walk-forward: 직전 22거래일(~1개월) 분봉 데이터로 학습,
      당일/익일 390개 포인트 예측
    - 학습 타겟: (close - 전일종가) / 전일종가 (= 장중 수익률)
    - 피처 그룹 A: 분봉 시계열 (시간 위치, 롤링 통계, 거래량)
    - 피처 그룹 B: 일봉 (모멘텀, 전일 거래대금, 시총)
    - 피처 그룹 C: 공시/컨센서스 (최근 공시 여부, EPS 컨센서스 변화)

Point-in-Time 안전:
    예측 시점 기준으로 가용한 데이터만 사용.
    공시: rcept_dt < target_date
    컨센서스: as_of_date < target_date
"""
from __future__ import annotations

import warnings
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine
from src.db.repositories import upsert_minute_price_predictions
from src.utils.logger import logger

# 한국 시간대
KST = ZoneInfo("Asia/Seoul")

# 장 운영 시간 (KST)
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)

# 예측 분기 기준 시각 (KST): 이 시각 이전이면 당일, 이후면 익일
FORECAST_CUTOFF = time(8, 0)

# 한달 분봉 학습 기간 (거래일 수)
TRAIN_TRADING_DAYS = 22

# LightGBM 기본 하이퍼파라미터
_LGBM_PARAMS: dict[str, Any] = {
    "objective": "regression",
    "metric": "rmse",
    "n_estimators": 200,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 10,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_alpha": 0.1,
    "reg_lambda": 0.1,
    "n_jobs": -1,
    "random_state": 42,
    "verbose": -1,
}

MODEL_VERSION = "lgbm_v1"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_minute_forecast(
    symbol: str,
    *,
    now: datetime | None = None,
    save_feature_snapshot: bool = False,
) -> dict[str, Any]:
    """종목 하나에 대한 분봉 예측 실행 + DB upsert.

    Args:
        symbol: 6자리 KRX 종목코드.
        now: 현재 시각 (KST, timezone-aware). None 이면 시스템 시각 사용.
              테스트 시 원하는 시각을 주입 가능.
        save_feature_snapshot: True 이면 feature_snapshot JSONB 저장.
                                운영 환경에서는 용량 절약을 위해 False 권장.

    Returns:
        {
            "symbol": str,
            "target_date": "YYYY-MM-DD",
            "n_rows": int,          # upsert 된 분봉 수 (390)
            "prev_close": float,
            "model_version": str,
            "train_rows": int,      # 학습에 사용된 행 수
        }

    Raises:
        ValueError: 분봉 또는 일봉 데이터 부족.
    """
    if now is None:
        now = datetime.now(tz=KST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=KST)

    target_date = _resolve_target_date(now)
    logger.info(
        f"[minute_forecast] {symbol}: now={now.strftime('%H:%M')}, "
        f"target_date={target_date}"
    )

    # 전일 종가
    prev_close = _get_prev_close(symbol, target_date)
    if prev_close is None or prev_close <= 0:
        raise ValueError(
            f"{symbol}: 전일 종가를 찾을 수 없습니다 (target_date={target_date}). "
            "daily_prices 백필 확인 필요."
        )

    # 학습 데이터 빌드
    train_df = _build_train_dataset(symbol, target_date)
    if len(train_df) < 50:
        raise ValueError(
            f"{symbol}: 학습 데이터 부족 ({len(train_df)} rows < 50). "
            f"최소 {TRAIN_TRADING_DAYS}거래일 분봉 필요."
        )

    # 예측 피처 빌드 (target_date 390개 포인트)
    pred_features = _build_pred_features(symbol, target_date)

    # LightGBM 학습 + 예측
    feature_cols = [c for c in train_df.columns if c not in ("target",)]
    X_train = train_df[feature_cols]
    y_train = train_df["target"]

    model = _fit_lgbm(X_train, y_train)

    X_pred = pred_features[feature_cols]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        y_pred = model.predict(X_pred)

    # 결과 DataFrame 조립
    pred_ts = _generate_market_ts(target_date)  # 390개 KST TIMESTAMPTZ
    result_df = pd.DataFrame({
        "symbol": symbol,
        "ts": pred_ts,
        "predicted_return": y_pred,
        "predicted_close": [
            round(prev_close * (1.0 + r), 0) for r in y_pred
        ],
        "prev_close": prev_close,
        "model_version": MODEL_VERSION,
    })

    if save_feature_snapshot:
        snapshots = pred_features[feature_cols].to_dict(orient="records")
        result_df["feature_snapshot"] = snapshots

    rows = upsert_minute_price_predictions(result_df)
    logger.success(
        f"[minute_forecast] {symbol}: {rows} rows upserted "
        f"(target={target_date}, prev_close={prev_close:,.0f})"
    )

    return {
        "symbol": symbol,
        "target_date": target_date.isoformat(),
        "n_rows": rows,
        "prev_close": float(prev_close),
        "model_version": MODEL_VERSION,
        "train_rows": len(train_df),
    }


# ---------------------------------------------------------------------------
# Target date 결정
# ---------------------------------------------------------------------------


def _resolve_target_date(now: datetime) -> date:
    """08:00 KST 기준으로 당일/익일 결정."""
    today = now.date()
    if now.time() < FORECAST_CUTOFF:
        # 08:00 이전 → 당일 예측
        return _ensure_trading_day(today, direction=1)
    else:
        # 08:00 이후 → 다음 영업일 예측
        return _ensure_trading_day(today + timedelta(days=1), direction=1)


def _ensure_trading_day(d: date, direction: int = 1) -> date:
    """d 가 주말이면 direction 방향의 다음 평일로 이동.

    direction=1: 미래 방향 (다음 평일)
    direction=-1: 과거 방향 (직전 평일)
    """
    while d.weekday() >= 5:  # 5=토, 6=일
        d += timedelta(days=direction)
    return d


# ---------------------------------------------------------------------------
# 데이터 로딩
# ---------------------------------------------------------------------------


def _get_prev_close(symbol: str, target_date: date) -> float | None:
    """target_date 이전 가장 최근 종가 반환."""
    sql = text("""
        SELECT close
          FROM daily_prices
         WHERE symbol = :sym
           AND date < :td
           AND close IS NOT NULL AND close > 0
         ORDER BY date DESC
         LIMIT 1
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"sym": symbol, "td": target_date}).fetchone()
    return float(row[0]) if row else None


def _load_minute_bars(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
) -> pd.DataFrame:
    """분봉 OHLCV 로딩. ts 는 KST timezone-aware."""
    sql = text("""
        SELECT ts, open, high, low, close, volume, value
          FROM minute_prices
         WHERE symbol = :sym
           AND ts >= :s AND ts < :e
         ORDER BY ts
    """)
    with get_engine().connect() as conn:
        df = pd.read_sql(sql, conn, params={"sym": symbol, "s": start_dt, "e": end_dt})
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Seoul")
    for col in ("open", "high", "low", "close", "volume", "value"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_daily_prices(symbol: str, start: date, end: date) -> pd.DataFrame:
    """일봉 (close, volume, value, market_cap, per, pbr) 로딩."""
    sql = text("""
        SELECT date, close, volume, value, market_cap, per, pbr
          FROM daily_prices
         WHERE symbol = :sym
           AND date BETWEEN :s AND :e
           AND close IS NOT NULL AND close > 0
         ORDER BY date
    """)
    with get_engine().connect() as conn:
        df = pd.read_sql(sql, conn, params={"sym": symbol, "s": start, "e": end})
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def _load_dart_features(symbol: str, before_date: date) -> dict[str, float]:
    """공시 기반 피처 (Point-in-time: rcept_dt < before_date)."""
    sql = text("""
        SELECT COUNT(*) AS n_disclosures,
               SUM(CASE WHEN kind = 'B' THEN 1 ELSE 0 END) AS n_major,
               MAX(rcept_dt) AS last_rcept_dt
          FROM dart_disclosures d
          JOIN dart_corp_codes c ON c.corp_code = d.corp_code
         WHERE c.stock_code = :sym
           AND rcept_dt >= :since
           AND rcept_dt < :before
    """)
    since = before_date - timedelta(days=30)
    with get_engine().connect() as conn:
        row = conn.execute(
            sql, {"sym": symbol, "since": since, "before": before_date}
        ).fetchone()

    if not row or row[0] is None:
        return {
            "dart_n_disclosures_30d": 0.0,
            "dart_n_major_30d": 0.0,
            "dart_days_since_last": 30.0,
        }

    days_since = (
        (before_date - row[2]).days if row[2] else 30
    )
    return {
        "dart_n_disclosures_30d": float(row[0] or 0),
        "dart_n_major_30d": float(row[1] or 0),
        "dart_days_since_last": float(min(days_since, 30)),
    }


def _load_consensus_features(symbol: str, before_date: date) -> dict[str, float]:
    """컨센서스 기반 피처 (Point-in-time: as_of_date < before_date)."""
    sql = text("""
        SELECT
            AVG(eps_estimate)                   AS avg_eps,
            AVG(target_price)                   AS avg_tp,
            MAX(n_estimates)                    AS max_n_est,
            MAX(as_of_date)                     AS latest_date
          FROM consensus_estimates
         WHERE symbol = :sym
           AND as_of_date >= :since
           AND as_of_date < :before
           AND fiscal_period_type = 'annual'
    """)
    since = before_date - timedelta(days=30)
    with get_engine().connect() as conn:
        row = conn.execute(
            sql, {"sym": symbol, "since": since, "before": before_date}
        ).fetchone()

    prev_close = _get_prev_close(symbol, before_date) or 1.0

    if not row or row[0] is None:
        return {
            "consensus_tp_upside": 0.0,
            "consensus_n_estimates": 0.0,
            "consensus_has_data": 0.0,
        }

    avg_tp = float(row[1] or 0)
    tp_upside = (avg_tp / prev_close - 1.0) if avg_tp > 0 and prev_close > 0 else 0.0
    return {
        "consensus_tp_upside": float(tp_upside),
        "consensus_n_estimates": float(row[2] or 0),
        "consensus_has_data": 1.0,
    }


# ---------------------------------------------------------------------------
# 피처 엔지니어링
# ---------------------------------------------------------------------------


def _generate_market_ts(target_date: date) -> list[datetime]:
    """09:00~15:30 KST 1분봉 타임스탬프 390개 생성."""
    result = []
    base = datetime(target_date.year, target_date.month, target_date.day,
                    9, 0, 0, tzinfo=KST)
    # 09:00 ~ 15:29 (390분)
    for i in range(390):
        result.append(base + timedelta(minutes=i))
    return result


def _make_minute_features(bars: pd.DataFrame, prev_close: float) -> pd.DataFrame:
    """분봉 시계열 피처 엔지니어링.

    입력: 하루치 분봉 DataFrame (ts, open, high, low, close, volume, value)
    출력: 분봉별 피처 DataFrame (390행 × 피처컬럼)
    """
    df = bars.copy().sort_values("ts").reset_index(drop=True)

    # 기본 수익률
    df["return_from_prev"] = (df["close"] / prev_close) - 1.0
    df["bar_return"] = df["close"].pct_change().fillna(0.0)

    # 장 시작 후 경과 분 (시간 위치 피처)
    open_ts = df["ts"].iloc[0].replace(hour=9, minute=0, second=0, microsecond=0)
    df["minutes_since_open"] = (df["ts"] - open_ts).dt.total_seconds() / 60.0
    # sin/cos 인코딩으로 주기성 표현
    total_minutes = 390.0
    df["time_sin"] = (df["minutes_since_open"] * 2 * 3.14159 / total_minutes).apply(
        lambda x: __import__("math").sin(x)
    )
    df["time_cos"] = (df["minutes_since_open"] * 2 * 3.14159 / total_minutes).apply(
        lambda x: __import__("math").cos(x)
    )

    # 요일 (0=월 ~ 4=금)
    df["weekday"] = df["ts"].dt.weekday

    # 롤링 피처 (5분, 20분)
    for w in (5, 20):
        df[f"close_ma_{w}m"] = (
            df["close"].rolling(w, min_periods=1).mean() / prev_close - 1.0
        )
        df[f"vol_ma_{w}m"] = df["volume"].rolling(w, min_periods=1).mean()
        df[f"range_{w}m"] = (
            df["high"].rolling(w, min_periods=1).max()
            - df["low"].rolling(w, min_periods=1).min()
        ) / prev_close

    # 거래량 급증 비율 (현재 / 20분 평균)
    df["volume_surge"] = (
        df["volume"] / (df["vol_ma_20m"].replace(0, float("nan")))
    ).fillna(1.0).clip(0, 10)

    # VWAP 대비 가격 위치
    cum_value = df["value"].cumsum()
    cum_volume = df["volume"].cumsum().replace(0, float("nan"))
    df["vwap"] = cum_value / cum_volume
    df["price_vs_vwap"] = (df["close"] / df["vwap"].fillna(prev_close)) - 1.0

    # 누적 수익률
    df["cum_return"] = (df["close"] / prev_close) - 1.0

    feature_cols = [
        "minutes_since_open", "time_sin", "time_cos", "weekday",
        "bar_return", "cum_return",
        "close_ma_5m", "close_ma_20m",
        "range_5m", "range_20m",
        "vol_ma_5m", "vol_ma_20m",
        "volume_surge", "price_vs_vwap",
    ]
    return df[feature_cols].fillna(0.0)


def _make_daily_features(
    symbol: str,
    daily_df: pd.DataFrame,
    target_date: date,
) -> dict[str, float]:
    """일봉 기반 피처 (모멘텀, 거래대금, 시총 등)."""
    if daily_df.empty:
        return {
            "momentum_5d": 0.0,
            "momentum_20d": 0.0,
            "prev_volume": 0.0,
            "prev_value": 0.0,
            "prev_market_cap": 0.0,
            "per": 0.0,
            "pbr": 0.0,
        }

    closes = daily_df["close"].values
    n = len(closes)

    def _ret(lag: int) -> float:
        if n <= lag:
            return 0.0
        denom = closes[-(lag + 1)]
        return float((closes[-1] / denom) - 1.0) if denom > 0 else 0.0

    last = daily_df.iloc[-1]
    return {
        "momentum_5d": _ret(5),
        "momentum_20d": _ret(20),
        "prev_volume": float(last.get("volume", 0) or 0),
        "prev_value": float(last.get("value", 0) or 0),
        "prev_market_cap": float(last.get("market_cap", 0) or 0),
        "per": float(last.get("per", 0) or 0),
        "pbr": float(last.get("pbr", 0) or 0),
    }


def _build_one_day_features(
    symbol: str,
    bars_day: pd.DataFrame,
    prev_close: float,
    daily_feats: dict[str, float],
    dart_feats: dict[str, float],
    consensus_feats: dict[str, float],
    include_target: bool,
) -> pd.DataFrame:
    """하루치 분봉 피처 + 일봉/공시/컨센서스 피처를 합쳐 DataFrame 반환.

    include_target=True: target 컬럼(수익률) 포함 (학습용)
    include_target=False: target 컬럼 제외, 예측 슬롯만 (추론용)
    """
    if bars_day.empty:
        return pd.DataFrame()

    minute_feats = _make_minute_features(bars_day, prev_close)

    # 날봉/공시/컨센서스 피처는 모든 분봉에 브로드캐스팅
    for k, v in {**daily_feats, **dart_feats, **consensus_feats}.items():
        minute_feats[k] = v

    if include_target:
        targets = (bars_day["close"].values / prev_close) - 1.0
        minute_feats["target"] = targets

    return minute_feats.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 학습 데이터셋 빌드
# ---------------------------------------------------------------------------


def _build_train_dataset(symbol: str, target_date: date) -> pd.DataFrame:
    """직전 TRAIN_TRADING_DAYS 거래일의 학습 데이터 빌드.

    각 거래일에 대해:
        - 분봉 피처 + 일봉/공시/컨센서스 피처 계산
        - target = (close - 전일종가) / 전일종가
    """
    # 충분한 달력일 버퍼 (주말/공휴일 포함)
    buffer_days = TRAIN_TRADING_DAYS * 2 + 14
    train_start_dt = datetime(
        *(target_date - timedelta(days=buffer_days)).timetuple()[:3],
        9, 0, 0, tzinfo=KST,
    )
    train_end_dt = datetime(
        *target_date.timetuple()[:3], 0, 0, 0, tzinfo=KST
    )

    bars_all = _load_minute_bars(symbol, train_start_dt, train_end_dt)
    if bars_all.empty:
        return pd.DataFrame()

    # 날짜별로 분리
    bars_all["_date"] = bars_all["ts"].dt.date
    trading_days = sorted(bars_all["_date"].unique())

    # 최근 TRAIN_TRADING_DAYS 거래일만 사용
    trading_days = trading_days[-TRAIN_TRADING_DAYS:]

    # 일봉 로딩 (모멘텀 계산을 위해 충분한 과거 포함)
    daily_start = target_date - timedelta(days=buffer_days)
    daily_df = _load_daily_prices(
        symbol,
        daily_start,
        target_date - timedelta(days=1),
    )

    # 공시/컨센서스 피처 (학습기간 전체에 대해 고정 — 근사값)
    dart_feats = _load_dart_features(symbol, target_date)
    consensus_feats = _load_consensus_features(symbol, target_date)

    frames: list[pd.DataFrame] = []
    for i, d in enumerate(trading_days):
        bars_day = bars_all[bars_all["_date"] == d].drop(columns=["_date"])

        # 전일 종가 (d 기준 직전 종가)
        prev_rows = daily_df[daily_df["date"] < d]
        if prev_rows.empty:
            continue
        pc = float(prev_rows.iloc[-1]["close"])
        if pc <= 0:
            continue

        # 일봉 피처: d 이전 데이터로만 계산 (PIT)
        daily_feats = _make_daily_features(
            symbol,
            daily_df[daily_df["date"] < d],
            d,
        )

        day_df = _build_one_day_features(
            symbol, bars_day, pc,
            daily_feats, dart_feats, consensus_feats,
            include_target=True,
        )
        if not day_df.empty:
            frames.append(day_df)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# 예측 피처 빌드 (target_date 390포인트, 실제 분봉 없음 → 0으로 채움)
# ---------------------------------------------------------------------------


def _build_pred_features(symbol: str, target_date: date) -> pd.DataFrame:
    """예측 대상 날짜의 피처 빌드.

    실제 분봉은 아직 없으므로 시간 위치 피처만 구성하고,
    나머지 분봉 통계는 학습 기간 마지막 거래일의 패턴을 참조.
    일봉/공시/컨센서스 피처는 target_date 기준 가장 최신 값 사용.
    """
    pred_ts = _generate_market_ts(target_date)
    n = len(pred_ts)  # 390

    prev_close = _get_prev_close(symbol, target_date) or 1.0

    # 시간 위치 피처
    import math
    minutes = list(range(n))
    time_sin = [math.sin(m * 2 * math.pi / 390.0) for m in minutes]
    time_cos = [math.cos(m * 2 * math.pi / 390.0) for m in minutes]
    weekday = target_date.weekday()

    # 일봉 피처
    buffer_days = TRAIN_TRADING_DAYS * 2 + 14
    daily_df = _load_daily_prices(
        symbol,
        target_date - timedelta(days=buffer_days),
        target_date - timedelta(days=1),
    )
    daily_feats = _make_daily_features(symbol, daily_df, target_date)

    # 공시/컨센서스 피처
    dart_feats = _load_dart_features(symbol, target_date)
    consensus_feats = _load_consensus_features(symbol, target_date)

    # 학습 마지막 거래일의 평균 분봉 통계 (롤링 피처 근사)
    last_day_stats = _get_last_day_rolling_stats(symbol, target_date, prev_close)

    rows = []
    for i, m in enumerate(minutes):
        row: dict[str, float] = {
            "minutes_since_open": float(m),
            "time_sin": time_sin[i],
            "time_cos": time_cos[i],
            "weekday": float(weekday),
            "bar_return": 0.0,
            "cum_return": 0.0,
            "close_ma_5m": 0.0,
            "close_ma_20m": 0.0,
            "range_5m": last_day_stats.get("range_5m", 0.0),
            "range_20m": last_day_stats.get("range_20m", 0.0),
            "vol_ma_5m": last_day_stats.get("vol_ma_5m", 0.0),
            "vol_ma_20m": last_day_stats.get("vol_ma_20m", 0.0),
            "volume_surge": 1.0,
            "price_vs_vwap": 0.0,
        }
        row.update(daily_feats)
        row.update(dart_feats)
        row.update(consensus_feats)
        rows.append(row)

    return pd.DataFrame(rows)


def _get_last_day_rolling_stats(
    symbol: str,
    before_date: date,
    prev_close: float,
) -> dict[str, float]:
    """직전 거래일의 평균 롤링 통계 (예측 피처 초기값으로 사용)."""
    last_date = _ensure_trading_day(before_date - timedelta(days=1), direction=-1)
    start_dt = datetime(*last_date.timetuple()[:3], 9, 0, 0, tzinfo=KST)
    end_dt = datetime(*last_date.timetuple()[:3], 15, 31, 0, tzinfo=KST)

    bars = _load_minute_bars(symbol, start_dt, end_dt)
    if bars.empty:
        return {}

    feats = _make_minute_features(bars, prev_close)
    return {
        "range_5m": float(feats["range_5m"].mean()),
        "range_20m": float(feats["range_20m"].mean()),
        "vol_ma_5m": float(feats["vol_ma_5m"].mean()),
        "vol_ma_20m": float(feats["vol_ma_20m"].mean()),
    }


# ---------------------------------------------------------------------------
# LightGBM 학습
# ---------------------------------------------------------------------------


def _fit_lgbm(X: pd.DataFrame, y: pd.Series):  # type: ignore[return]
    """LightGBM Regressor 학습. lightgbm 없으면 ImportError."""
    try:
        from lightgbm import LGBMRegressor
    except ImportError as e:
        raise ImportError(
            "lightgbm 패키지가 필요합니다: pip install lightgbm"
        ) from e

    model = LGBMRegressor(**_LGBM_PARAMS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(X, y)
    return model
