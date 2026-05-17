"""LightGBM regression wrapper for cross-sectional alpha modeling.

Why LightGBM (not Transformer / DNN):
    Qlib Alpha360 benchmarks (Microsoft, ~20-seed mean) consistently
    show GBDT models (LightGBM, XGBoost, CatBoost, DoubleEnsemble)
    outperforming vanilla Transformer / TabNet on equity ranking
    tasks. Stock data's low SNR + non-stationarity + modest sample
    size favors GBDT.

Target convention (Phase 2 decision):
    - Regression on raw forward_return (the actual numeric value).
    - Optional cross-sectional z-score normalization of y BEFORE
      training, controlled by `target_normalize`. Recommended because
      it dampens regime-driven scale changes in returns.
    - Evaluation is always done in rank space: predictions are
      converted to cross-sectional rank, then IC/RankIC/Sharpe are
      computed via Phase 1 metrics module.

Hyperparameters:
    Defaults are conservative for low-SNR financial data:
        num_leaves          = 31     (small trees - regularize)
        learning_rate       = 0.05
        n_estimators        = 500    (with early stopping)
        min_data_in_leaf    = 200    (large min - dampen noise)
        feature_fraction    = 0.8
        bagging_fraction    = 0.8
        bagging_freq        = 5
        lambda_l1           = 0.0
        lambda_l2           = 0.1
        objective           = 'regression'
        metric              = 'rmse'
    Override via the `params` argument of train(...).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.utils.logger import logger

try:
    import lightgbm as lgb
except ImportError:  # pragma: no cover
    lgb = None


DEFAULT_LGBM_PARAMS: dict = {
    "objective": "regression",
    "metric": "rmse",
    "boosting_type": "gbdt",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "lambda_l1": 0.0,
    "lambda_l2": 0.1,
    "verbose": -1,
    "force_row_wise": True,  # avoids a perf warning on small datasets
}


class LGBMRegressorModel:
    """Thin wrapper around lightgbm.Booster for cross-sectional regression.

    The wrapper:
      - Standardizes the train/predict API across walk-forward folds.
      - Optionally z-score-normalizes y per training date before fitting
        (recommended for cross-sectional return regression).
      - Returns predictions on the ORIGINAL y-scale (denormalized) so
        downstream rank/IC calculations work without further changes.
      - Exposes feature importance as a plain dict.
    """

    def __init__(
        self,
        params: dict | None = None,
        n_estimators: int = 500,
        early_stopping_rounds: int = 30,
        target_normalize: bool = True,
    ) -> None:
        if lgb is None:
            raise ImportError(
                "lightgbm is not installed. Add it to pyproject.toml: "
                "`lightgbm>=4.3.0` and reinstall."
            )
        self.params = {**DEFAULT_LGBM_PARAMS, **(params or {})}
        self.n_estimators = n_estimators
        self.early_stopping_rounds = early_stopping_rounds
        self.target_normalize = target_normalize
        self.booster: "lgb.Booster | None" = None
        self.feature_names: list[str] = []
        self.train_loss_: float | None = None
        self.valid_loss_: float | None = None
        # Stats used for y normalization (per-date z-score is applied
        # at fit time; at predict time we don't need to invert because
        # we evaluate in rank space anyway)
        self._train_date_stats: dict = {}

    @staticmethod
    def _zscore_y_per_date(
        df: pd.DataFrame, y_col: str, date_col: str = "date",
    ) -> pd.Series:
        """Per-date z-score of y. NaN preserved.

        Returns a Series aligned to df.index.
        """
        def _z(s: pd.Series) -> pd.Series:
            n = s.notna().sum()
            if n < 2:
                return pd.Series([np.nan] * len(s), index=s.index)
            mu = s.mean()
            sd = s.std(ddof=0)
            if sd == 0 or pd.isna(sd):
                return pd.Series([np.nan] * len(s), index=s.index)
            return (s - mu) / sd
        return df.groupby(date_col, group_keys=False)[y_col].transform(_z)

    def fit(
        self,
        train_df: pd.DataFrame,
        feature_cols: list[str],
        y_col: str = "y",
        date_col: str = "date",
        valid_df: pd.DataFrame | None = None,
    ) -> "LGBMRegressorModel":
        """Fit on train_df. valid_df (if provided) is used for early stop.

        Args:
            train_df: long DataFrame with date_col + feature_cols + y_col.
            feature_cols: list of column names to use as features.
            y_col: target column.
            date_col: date column (for per-date y-normalization).
            valid_df: optional validation set, same columns. When None,
                      no early stopping is applied.

        Returns self for chaining.
        """
        if not feature_cols:
            raise ValueError("feature_cols is empty")
        self.feature_names = list(feature_cols)

        # Drop rows where y is NaN
        train_df = train_df.dropna(subset=[y_col]).copy()
        if train_df.empty:
            raise ValueError("train_df is empty after dropping NaN y")

        # Prepare target
        if self.target_normalize:
            y_train = self._zscore_y_per_date(train_df, y_col, date_col)
        else:
            y_train = train_df[y_col]
        # Drop rows whose normalized y is NaN (small cross-sections)
        mask = y_train.notna()
        train_df = train_df.loc[mask].copy()
        y_train = y_train.loc[mask]

        X_train = train_df[feature_cols].astype("float32")

        train_set = lgb.Dataset(
            X_train, label=y_train.astype("float32"),
            feature_name=feature_cols,
            free_raw_data=False,
        )

        valid_sets = [train_set]
        valid_names = ["train"]
        if valid_df is not None and not valid_df.empty:
            valid_df = valid_df.dropna(subset=[y_col]).copy()
            if self.target_normalize:
                y_valid = self._zscore_y_per_date(valid_df, y_col, date_col)
            else:
                y_valid = valid_df[y_col]
            v_mask = y_valid.notna()
            valid_df = valid_df.loc[v_mask].copy()
            y_valid = y_valid.loc[v_mask]
            X_valid = valid_df[feature_cols].astype("float32")
            valid_set = lgb.Dataset(
                X_valid, label=y_valid.astype("float32"),
                feature_name=feature_cols, reference=train_set,
                free_raw_data=False,
            )
            valid_sets.append(valid_set)
            valid_names.append("valid")

        callbacks = []
        if valid_df is not None and not valid_df.empty:
            callbacks.append(
                lgb.early_stopping(
                    self.early_stopping_rounds, verbose=False
                )
            )
        callbacks.append(lgb.log_evaluation(period=0))

        self.booster = lgb.train(
            params=self.params,
            train_set=train_set,
            num_boost_round=self.n_estimators,
            valid_sets=valid_sets,
            valid_names=valid_names,
            callbacks=callbacks,
        )

        # Capture losses for logging / persistence
        eval_result = self.booster.eval_train()
        if eval_result:
            # Each entry: (dataset_name, metric_name, value, is_higher_better)
            self.train_loss_ = float(eval_result[0][2])
        if valid_df is not None and not valid_df.empty:
            v_eval = self.booster.eval_valid()
            if v_eval:
                self.valid_loss_ = float(v_eval[0][2])

        logger.info(
            f"LGBM fit done: n_train={len(train_df):,}, "
            f"best_iter={self.booster.best_iteration}, "
            f"train_loss={self.train_loss_}, valid_loss={self.valid_loss_}"
        )
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        """Predict on rows of df. Returns numpy array aligned to df.index."""
        if self.booster is None:
            raise RuntimeError("Model not fitted yet")
        X = df[self.feature_names].astype("float32")
        return self.booster.predict(X, num_iteration=self.booster.best_iteration)

    def feature_importance(
        self, importance_type: str = "gain",
    ) -> dict[str, float]:
        """Return dict[feature_name -> importance]."""
        if self.booster is None:
            raise RuntimeError("Model not fitted yet")
        vals = self.booster.feature_importance(importance_type=importance_type)
        return {n: float(v) for n, v in zip(self.feature_names, vals)}
