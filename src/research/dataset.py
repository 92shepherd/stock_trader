"""Build the (date, symbol) x features panel for ML training.

Two ways the panel can be assembled:

    (A) From the factor_signals CACHE - if you've already run
        evaluate_factor for each desired factor, the rank_value /
        z_score / raw_value columns are sitting in factor_signals.
        Just SELECT them and pivot.

    (B) ON-THE-FLY computation - call each factor function from
        ALL_FACTORS, run prepare_factor_signal_frame, and stack the
        results. Slower but works without a cache.

Default: (A). Use (B) only when the cache hasn't been populated
(e.g. very first training run).

Output schema:
    A long DataFrame with columns:
        symbol, date, <feature1>, <feature2>, ..., y, universe
    where:
        - each feature is the rank_value (default) or z_score or raw_value
          of the corresponding factor on that (symbol, date)
        - y is the forward return for the configured horizon, drawn from
          forward_returns
        - rows where ANY feature is missing are kept (LightGBM handles
          NaN natively); rows where y is missing are dropped

The "long" format is convenient for LightGBM but the trainer downstream
treats each row as one training example.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine
from src.db.repositories import (
    query_factor_signals,
    query_forward_returns,
    upsert_factor_signals,
)
from src.research.factor_eval import ALL_FACTORS
from src.research.ranking import prepare_factor_signal_frame
from src.research.universe import get_universe
from src.utils.logger import logger


def build_panel(
    feature_names: list[str],
    universe: str,
    start: date,
    end: date,
    horizon_days: int,
    feature_repr: str = "rank_value",
    compute_missing: bool = False,
) -> pd.DataFrame:
    """Assemble feature matrix + target panel.

    Args:
        feature_names: list of factor_name strings that must be in
                       ALL_FACTORS (or already in factor_signals).
        universe: universe identifier (KOSPI / KOSDAQ / ALL / KOSPI200).
        start, end: panel date range (base dates).
        horizon_days: forward return horizon (1/5/10/20).
        feature_repr: which column of factor_signals to use as feature
                      value. Options:
                          'rank_value' (default, cross-sectional rank)
                          'z_score'    (cross-sectional z-score)
                          'raw_value'  (raw, untransformed)
        compute_missing: if True, any feature not found in factor_signals
                         for the given (universe, period) is computed
                         on the fly via the function in ALL_FACTORS and
                         upserted into factor_signals.

    Returns:
        DataFrame [symbol, date, *feature_names, y, universe].
        Rows where y is NaN are dropped. Feature columns may have NaN.
    """
    if feature_repr not in ("rank_value", "z_score", "raw_value"):
        raise ValueError(f"Bad feature_repr: {feature_repr}")
    if not feature_names:
        raise ValueError("feature_names is empty")

    # 1) Resolve universe
    symbols = get_universe(universe, end)
    if not symbols:
        raise RuntimeError(f"Universe '{universe}' empty on {end}")
    logger.info(
        f"build_panel: universe={universe} |syms|={len(symbols)} "
        f"period=[{start}..{end}] features={feature_names}"
    )

    # 2) For each feature, load signal frame from cache (or compute)
    feature_frames: dict[str, pd.DataFrame] = {}
    for fname in feature_names:
        sig = query_factor_signals(
            factor_name=fname, start=start, end=end, symbols=symbols,
        )
        if sig.empty and compute_missing:
            sig = _compute_and_cache(fname, symbols, start, end, universe)
        if sig.empty:
            logger.warning(
                f"  feature '{fname}' has no rows in factor_signals "
                f"for [{start}..{end}] - using all-NaN column"
            )
        feature_frames[fname] = sig

    # 3) Load forward returns
    fwd = query_forward_returns(
        horizon_days=horizon_days, start=start, end=end, symbols=symbols,
    )
    if fwd.empty:
        raise RuntimeError(
            f"No forward_returns for horizon={horizon_days} in [{start}..{end}]. "
            "Run research.returns.backfill_forward_returns first."
        )

    # 4) Build the base panel from (symbol, date) of fwd_return
    panel = fwd[["symbol", "date", "fwd_return"]].rename(columns={"fwd_return": "y"})
    panel["y"] = pd.to_numeric(panel["y"], errors="coerce")
    panel = panel.dropna(subset=["y"])

    # 5) Merge each feature column
    for fname, sig in feature_frames.items():
        if sig.empty:
            panel[fname] = pd.NA
            continue
        feat = sig[["symbol", "date", feature_repr]].rename(
            columns={feature_repr: fname}
        )
        feat[fname] = pd.to_numeric(feat[fname], errors="coerce")
        panel = panel.merge(feat, on=["symbol", "date"], how="left")

    panel["universe"] = universe
    panel["date"] = pd.to_datetime(panel["date"]).dt.date

    cols = ["symbol", "date"] + feature_names + ["y", "universe"]
    panel = panel[cols].reset_index(drop=True)

    n_with_features = panel[feature_names].notna().any(axis=1).sum()
    logger.info(
        f"  panel: {len(panel):,} rows, {n_with_features:,} have >= 1 feature"
    )
    return panel


def _compute_and_cache(
    factor_name: str,
    symbols: list[str],
    start: date,
    end: date,
    universe: str,
) -> pd.DataFrame:
    """Compute a missing factor on-the-fly via ALL_FACTORS and upsert
    the result into factor_signals.

    Used as a convenience fallback in build_panel. For production
    workflows, run pipelines.eval_factor for each factor first to
    populate the cache properly.
    """
    if factor_name not in ALL_FACTORS:
        logger.warning(
            f"compute_missing: '{factor_name}' not in ALL_FACTORS - skip"
        )
        return pd.DataFrame()

    spec = ALL_FACTORS[factor_name]
    logger.info(f"  computing missing factor '{factor_name}' on the fly...")
    raw = spec["fn"](symbols=symbols, start=start, end=end, **spec["kwargs"])
    if raw.empty:
        return pd.DataFrame()

    signal_frame = prepare_factor_signal_frame(
        raw,
        factor_name=factor_name,
        value_col="raw_value",
        universe_label=universe,
    )
    if not signal_frame.empty:
        upsert_factor_signals(signal_frame)
        logger.info(
            f"  upserted {len(signal_frame):,} rows of '{factor_name}'"
        )
    # Reload from DB so the format matches query_factor_signals output
    return query_factor_signals(
        factor_name=factor_name, start=start, end=end, symbols=symbols,
    )


def split_panel_by_date(
    panel: pd.DataFrame,
    train_start: date,
    train_end: date,
    test_start: date,
    test_end: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Time-based split of the panel into (train, test).

    Inclusive on both ends. The caller is responsible for setting
    test_start >= train_end + gap_days to avoid label leakage.
    """
    train = panel[(panel["date"] >= train_start) & (panel["date"] <= train_end)].copy()
    test = panel[(panel["date"] >= test_start) & (panel["date"] <= test_end)].copy()
    logger.info(
        f"split: train=[{train_start}..{train_end}] n={len(train):,}, "
        f"test=[{test_start}..{test_end}] n={len(test):,}"
    )
    return train, test


# Default feature set for Phase 2 LightGBM model.
# Mix of:
#   - Phase 1 baseline (price-only) for sanity
#   - Phase 2 revision (consensus revision is the strongest single alpha)
#   - Phase 2 PEAD (post-earnings drift)
#   - Phase 2 quality (low-frequency baseline)
#   - Phase 2 rating (analyst signal)
DEFAULT_PHASE2_FEATURES: list[str] = [
    # Baseline (price-only)
    "momentum_20d",
    "reversal_5d",
    "volatility_20d",
    # Revision
    "rev_eps_1m_fy1",
    "rev_eps_3m_fy1",
    # PEAD
    "pead_revenue_yoy",
    "pead_op_yoy",
    # Quality
    "quality_roe",
    "quality_op_margin",
    # Rating
    "rating_upgrade_30d",
    "rating_target_upside",
]
