"""End-to-end walk-forward training driver for Phase 2 LightGBM model.

Pipeline:
    1. build_panel(...) -> (date, symbol, features, y) DataFrame.
    2. For each WalkForwardSplit:
         a. split_panel_by_date -> (train, test).
         b. fit LGBMRegressorModel on train; (optionally) use last
            ~20% of train as in-fold validation for early stopping.
         c. predict on test; convert to cross-sectional rank.
         d. Compute metrics (IC, RankIC, ICIR, Sharpe, MaxDD, ...)
            via research.metrics.
         e. Persist model_runs row, model_predictions rows,
            model_feature_importance rows, eval_runs + eval_metrics
            rows (with notes linking back to model_run_id).

Returned summary:
    Dict with per-fold metrics and an aggregate (mean across folds).

Notes:
    - Walk-forward defaults match a sensible long-horizon study:
      train ~ 3 years, test ~ 6 months, gap = horizon_days, step =
      6 months (non-overlapping test windows).
    - The features list is the master truth: missing features in
      factor_signals are filled with NaN (LightGBM handles natively).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from src.db.repositories import (
    insert_eval_metrics,
    insert_eval_run,
    insert_feature_importance,
    insert_model_run,
    upsert_model_predictions,
)
from src.research.dataset import (
    DEFAULT_PHASE2_FEATURES,
    build_panel,
    split_panel_by_date,
)
from src.research.factor_eval import make_run_id
from src.research.metrics import compute_all_metrics
from src.research.model_lgbm import LGBMRegressorModel
from src.research.walk_forward import WalkForwardSplit, rolling_windows
from src.utils.logger import logger


def _make_model_run_id(model_type: str, universe: str, fold: int) -> str:
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{now}_{model_type}_{universe[:8]}_f{fold:02d}"


def _cross_sectional_rank(
    df: pd.DataFrame, value_col: str, date_col: str = "date",
) -> pd.Series:
    """Per-date percentile rank in [0, 1] of value_col. NaN preserved."""
    def _r(s: pd.Series) -> pd.Series:
        n = s.notna().sum()
        if n < 2:
            return pd.Series([np.nan] * len(s), index=s.index)
        ranks = s.rank(method="average", ascending=True, pct=True)
        return (ranks - 1.0 / n) / (1.0 - 1.0 / n)
    return df.groupby(date_col, group_keys=False)[value_col].transform(_r)


def train_lgbm_walk_forward(
    universe: str,
    start: date,
    end: date,
    horizon_days: int = 5,
    feature_names: list[str] | None = None,
    train_days: int = 365 * 3,
    test_days: int = 180,
    step_days: int | None = None,
    *,
    feature_repr: str = "rank_value",
    target_normalize: bool = True,
    n_quantiles: int = 5,
    lgbm_params: dict | None = None,
    n_estimators: int = 500,
    early_stopping_rounds: int = 30,
    persist: bool = True,
    compute_missing_features: bool = False,
    notes: str | None = None,
) -> dict:
    """Run walk-forward training and evaluation of LightGBM regressor.

    Args:
        universe: ALL / KOSPI / KOSDAQ / KOSPI200.
        start, end: overall data window for the study.
        horizon_days: forward return horizon (must match forward_returns cache).
        feature_names: features to use. None = DEFAULT_PHASE2_FEATURES.
        train_days, test_days: walk-forward window lengths (calendar days).
        step_days: advance between folds. None = test_days.
        feature_repr: which factor_signals column to use as feature
                      ('rank_value' default).
        target_normalize: per-date z-score y in LGBM training.
        n_quantiles: long-short evaluation portfolio quantile count.
        lgbm_params: LightGBM hyperparams. None = library defaults.
        n_estimators: max boosting rounds.
        early_stopping_rounds: stop if validation loss doesn't improve.
        persist: write model_runs/predictions/importance + eval_runs/metrics.
        compute_missing_features: if a feature isn't in factor_signals
            cache, compute on the fly. Slow but useful for first run.
        notes: free-text appended to the eval_runs.notes for each fold.

    Returns:
        Dict {
            "folds": [per-fold dict with model_run_id, eval_run_id,
                      metrics, n_train, n_test],
            "aggregate": {metric_name: mean across folds},
            "feature_names": [...]
        }
    """
    feature_names = feature_names or list(DEFAULT_PHASE2_FEATURES)
    gap_days = horizon_days  # prevent label leakage from train into test

    logger.info(
        f"=== train_lgbm_walk_forward ===\n"
        f"  universe={universe} period=[{start}..{end}] horizon={horizon_days}\n"
        f"  train_days={train_days} test_days={test_days} gap={gap_days}\n"
        f"  features={feature_names}"
    )

    # 1) Build the full panel ONCE; the trainer slices by date for each fold
    panel = build_panel(
        feature_names=feature_names,
        universe=universe,
        start=start, end=end,
        horizon_days=horizon_days,
        feature_repr=feature_repr,
        compute_missing=compute_missing_features,
    )
    if panel.empty:
        raise RuntimeError("build_panel returned empty - check data prerequisites")

    # 2) Generate walk-forward splits
    splits = rolling_windows(
        start=start, end=end,
        train_days=train_days, test_days=test_days,
        step_days=step_days,
        gap_days=gap_days,
    )
    if not splits:
        raise RuntimeError(
            f"No walk-forward splits fit in [{start}..{end}] with "
            f"train_days={train_days}, test_days={test_days}, gap={gap_days}"
        )
    logger.info(f"  generated {len(splits)} walk-forward fold(s)")

    fold_results: list[dict] = []
    for fold_idx, split in enumerate(splits, start=1):
        logger.info(
            f"\n--- Fold {fold_idx}/{len(splits)}: "
            f"train=[{split.train_start}..{split.train_end}] "
            f"test=[{split.test_start}..{split.test_end}] ---"
        )
        try:
            result = _run_one_fold(
                panel=panel,
                split=split,
                fold_idx=fold_idx,
                universe=universe,
                horizon_days=horizon_days,
                feature_names=feature_names,
                target_normalize=target_normalize,
                n_quantiles=n_quantiles,
                lgbm_params=lgbm_params,
                n_estimators=n_estimators,
                early_stopping_rounds=early_stopping_rounds,
                persist=persist,
                notes=notes,
            )
        except Exception as e:
            logger.error(f"  fold {fold_idx} FAILED: {e}")
            continue
        fold_results.append(result)

    if not fold_results:
        raise RuntimeError("All folds failed")

    aggregate = _aggregate_fold_metrics(fold_results)
    logger.success(
        f"\n=== Walk-forward done: {len(fold_results)} folds successful ==="
    )
    for k, v in aggregate.items():
        try:
            logger.info(f"  AGG {k:<22s}: {v:>10.4f}")
        except (TypeError, ValueError):
            logger.info(f"  AGG {k:<22s}: {v}")

    return {
        "folds": fold_results,
        "aggregate": aggregate,
        "feature_names": feature_names,
    }


def _run_one_fold(
    panel: pd.DataFrame,
    split: WalkForwardSplit,
    fold_idx: int,
    universe: str,
    horizon_days: int,
    feature_names: list[str],
    target_normalize: bool,
    n_quantiles: int,
    lgbm_params: dict | None,
    n_estimators: int,
    early_stopping_rounds: int,
    persist: bool,
    notes: str | None,
) -> dict:
    """Train + predict + evaluate + persist a single fold."""
    train_df, test_df = split_panel_by_date(
        panel, split.train_start, split.train_end,
        split.test_start, split.test_end,
    )
    if train_df.empty or test_df.empty:
        raise RuntimeError(
            f"fold {fold_idx}: empty train or test (n_train={len(train_df)}, "
            f"n_test={len(test_df)})"
        )

    # In-fold validation: last 20% of train (still time-respecting)
    valid_split_date = split.train_start + timedelta(
        days=int((split.train_end - split.train_start).days * 0.8)
    )
    fit_df = train_df[train_df["date"] <= valid_split_date]
    valid_df = train_df[train_df["date"] > valid_split_date]
    if valid_df.empty:
        # tiny windows: skip valid
        fit_df = train_df
        valid_df = None

    # Train
    model = LGBMRegressorModel(
        params=lgbm_params,
        n_estimators=n_estimators,
        early_stopping_rounds=early_stopping_rounds,
        target_normalize=target_normalize,
    )
    model.fit(
        train_df=fit_df,
        feature_cols=feature_names,
        y_col="y",
        valid_df=valid_df,
    )

    # Predict on test
    test_df = test_df.copy()
    test_df["y_pred"] = model.predict(test_df)
    test_df["rank_value"] = _cross_sectional_rank(test_df, "y_pred")

    # Compute metrics on test (cross-sectional ranking eval)
    metrics_df = test_df.rename(columns={"y": "fwd_return"})
    metrics_df = metrics_df.dropna(subset=["rank_value", "fwd_return"])
    metrics = compute_all_metrics(
        metrics_df,
        horizon_days=horizon_days,
        signal_col="rank_value",
        return_col="fwd_return",
        n_quantiles=n_quantiles,
    )
    logger.info(
        f"  fold {fold_idx}: ic_mean={metrics.get('ic_mean'):.4f} "
        f"rank_icir={metrics.get('rank_icir'):.4f} "
        f"sharpe={metrics.get('long_short_sharpe'):.4f}"
    )

    model_run_id = _make_model_run_id("lgbm_reg", universe, fold_idx)
    eval_run_id: str | None = None

    if persist:
        # 1) model_runs
        insert_model_run(
            model_run_id=model_run_id,
            model_type="lgbm_reg",
            universe=universe,
            train_start=split.train_start,
            train_end=split.train_end,
            test_start=split.test_start,
            test_end=split.test_end,
            horizon_days=horizon_days,
            target_type="regression",
            features=feature_names,
            hyperparams=model.params,
            n_train_rows=int(len(fit_df)),
            n_test_rows=int(len(test_df)),
            train_loss=model.train_loss_,
            valid_loss=model.valid_loss_,
            notes=(notes or "") + f" fold {fold_idx}",
        )

        # 2) model_predictions
        preds_df = test_df[[
            "symbol", "date", "y_pred", "y", "rank_value",
        ]].rename(columns={"y": "y_true"}).copy()
        preds_df["model_run_id"] = model_run_id
        preds_df["horizon_days"] = horizon_days
        preds_df["universe"] = universe
        upsert_model_predictions(preds_df)

        # 3) feature importance
        importance = model.feature_importance(importance_type="gain")
        insert_feature_importance(model_run_id, importance, importance_type="gain")

        # 4) eval_runs + eval_metrics (link via notes)
        eval_run_id = make_run_id("model_lgbm_reg", universe)
        eval_notes = f"model_run_id={model_run_id}; fold={fold_idx}"
        if notes:
            eval_notes += f"; {notes}"
        insert_eval_run(
            run_id=eval_run_id,
            factor_name="model_lgbm_reg",
            universe=universe,
            start_date=split.test_start,
            end_date=split.test_end,
            horizon_days=horizon_days,
            params={
                "model_run_id": model_run_id,
                "features": feature_names,
                "lgbm_params": model.params,
            },
            n_observations=int(len(metrics_df)),
            notes=eval_notes,
        )
        insert_eval_metrics(eval_run_id, metrics)
        logger.success(
            f"  fold {fold_idx} persisted: model_run_id={model_run_id} "
            f"eval_run_id={eval_run_id}"
        )

    return {
        "fold": fold_idx,
        "model_run_id": model_run_id,
        "eval_run_id": eval_run_id,
        "train_start": split.train_start, "train_end": split.train_end,
        "test_start": split.test_start, "test_end": split.test_end,
        "n_train": int(len(fit_df)),
        "n_valid": int(len(valid_df)) if valid_df is not None else 0,
        "n_test": int(len(test_df)),
        "metrics": metrics,
    }


def _aggregate_fold_metrics(folds: list[dict]) -> dict:
    """Mean (ignoring NaN) of each metric across folds.

    Standard practice in walk-forward studies: the cross-fold mean
    matters more than any single fold, but we also flag dispersion
    via the std.
    """
    if not folds:
        return {}
    keys = list(folds[0]["metrics"].keys())
    out: dict = {}
    for k in keys:
        vals = [f["metrics"].get(k) for f in folds]
        vals = [v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v))]
        if not vals:
            out[k] = float("nan")
            continue
        out[k] = float(np.mean(vals))
        out[f"{k}_std"] = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
    return out
