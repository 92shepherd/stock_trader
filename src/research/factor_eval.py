"""Top-level driver for a single factor evaluation run.

This module ties everything together:
    universe       -> who's in the cross-section
    factors_*      -> raw signal values
    ranking        -> winsorize / sector-neutralize / rank / z-score
    returns        -> forward returns (cached)
    metrics        -> IC, RankIC, ICIR, hit rate, Sharpe, MaxDD, turnover
    repositories   -> persist factor_signals, eval_runs, eval_metrics

The public entry point is `evaluate_factor(...)` which produces a
fully-populated eval_run with associated eval_metrics rows. The result
DataFrame is also returned so the caller can inspect or chart it
without re-reading from DB.

Run identifiers:
    `make_run_id` produces 'YYYYMMDD_HHMMSS_<factor>_<universe>' which
    is human-readable and naturally sortable. It is not strictly unique
    on the second boundary, so callers running in parallel should add
    their own suffix.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd

from src.db.repositories import (
    insert_eval_metrics,
    insert_eval_run,
    query_forward_returns,
    upsert_factor_signals,
)
from src.research.factors_baseline import BASELINE_FACTORS
from src.research.factors_pead import PEAD_FACTORS
from src.research.factors_quality import QUALITY_FACTORS
from src.research.factors_rating import RATING_FACTORS
from src.research.factors_revision import REVISION_FACTORS
from src.research.metrics import compute_all_metrics
from src.research.ranking import prepare_factor_signal_frame
from src.research.returns import DEFAULT_HORIZONS, backfill_forward_returns
from src.research.universe import get_universe
from src.utils.logger import logger


# Unified factor registry. Phase 2 factors are merged in at import time so
# CLI can refer to them by name uniformly (BASELINE_FACTORS itself stays
# untouched in factors_baseline.py).
ALL_FACTORS: dict[str, dict] = {
    **BASELINE_FACTORS,
    **REVISION_FACTORS,
    **PEAD_FACTORS,
    **RATING_FACTORS,
    **QUALITY_FACTORS,
}


def make_run_id(factor_name: str, universe: str) -> str:
    """Generate a sortable, human-readable run_id.

    Format: YYYYMMDD_HHMMSS_<factor>_<universe>
    Example: '20260516_153045_momentum_20d_ALL'
    """
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Truncate factor/universe defensively to fit the VARCHAR(64) PK.
    factor_clean = factor_name[:25]
    univ_clean = universe[:10]
    return f"{now}_{factor_clean}_{univ_clean}"


def evaluate_factor(
    factor_name: str,
    universe: str,
    start: date,
    end: date,
    horizon_days: int = 5,
    *,
    signal_fn: Callable | None = None,
    signal_kwargs: dict | None = None,
    do_winsorize: bool = True,
    winsorize_pcts: tuple[float, float] = (0.01, 0.99),
    do_sector_neutralize: bool = False,
    n_quantiles: int = 5,
    min_obs_per_day: int = 20,
    persist_signals: bool = True,
    persist_run: bool = True,
    notes: str | None = None,
    ensure_forward_returns: bool = True,
) -> tuple[str, dict, pd.DataFrame]:
    """Run an end-to-end evaluation of one factor over one period.

    Args:
        factor_name: identifier used both as eval_runs.factor_name and
                     factor_signals.factor_name. If `signal_fn` is None,
                     this must be a key in `BASELINE_FACTORS`.
        universe: one of SUPPORTED_UNIVERSES from research.universe.
        start, end: evaluation period (inclusive, calendar dates).
        horizon_days: forward-return horizon to evaluate against.
                      Must be in DEFAULT_HORIZONS or already cached in
                      forward_returns table.
        signal_fn: optional callable that produces the raw signal.
                   Signature: fn(symbols, start, end, **kwargs) -> DataFrame
                   with columns [symbol, date, raw_value]. When None,
                   BASELINE_FACTORS[factor_name] is used.
        signal_kwargs: kwargs passed to signal_fn. When None, defaults
                       come from BASELINE_FACTORS (if applicable).
        do_winsorize, winsorize_pcts: ranking.prepare_factor_signal_frame
                                      parameters.
        do_sector_neutralize: same.
        n_quantiles, min_obs_per_day: metrics parameters.
        persist_signals: if True, upsert factor_signals into the DB.
        persist_run: if True, write eval_runs + eval_metrics rows.
        notes: free-text note attached to the run.
        ensure_forward_returns: if True, recompute and upsert forward
                                returns for the evaluation period before
                                evaluating. Set False to reuse existing
                                cache.

    Returns:
        Tuple (run_id, metrics_dict, joined_panel_df). The DataFrame is
        the joined factor-signal x forward-return panel used to compute
        the metrics, with columns [symbol, date, factor_name, raw_value,
        neutral_value, rank_value, z_score, universe, horizon_days,
        fwd_return, high_return].
    """
    # --- Resolve signal source ---------------------------------------
    if signal_fn is None:
        if factor_name not in ALL_FACTORS:
            raise KeyError(
                f"factor_name '{factor_name}' not in ALL_FACTORS "
                f"and no signal_fn supplied. Known factors: "
                f"{list(ALL_FACTORS.keys())}"
            )
        spec = ALL_FACTORS[factor_name]
        signal_fn = spec["fn"]
        if signal_kwargs is None:
            signal_kwargs = dict(spec["kwargs"])
    signal_kwargs = signal_kwargs or {}

    # --- Build the universe and panel --------------------------------
    # We use the universe of the START date as the symbol list for
    # signal computation. The per-day point-in-time filter is applied
    # at the joining step (only keep (date, symbol) rows whose symbol
    # was in the universe on that date). For Phase 1 we approximate by
    # using the END date's universe as the master list - this captures
    # all symbols that were ever in the universe during the period.
    universe_end = get_universe(universe, end)
    if not universe_end:
        raise RuntimeError(
            f"Universe '{universe}' on {end} is empty. "
            "Check that tickers table is populated and that this date "
            "is a sensible point-in-time."
        )

    logger.info(
        f"evaluate_factor: factor={factor_name} universe={universe} "
        f"period=[{start}..{end}] horizon={horizon_days}d "
        f"|universe|={len(universe_end)}"
    )

    # --- Compute raw signal ------------------------------------------
    raw = signal_fn(symbols=universe_end, start=start, end=end, **signal_kwargs)
    if raw.empty:
        raise RuntimeError(
            f"Signal function for {factor_name} returned no rows. "
            "Likely the price history is too short or the universe is empty."
        )
    logger.info(f"  raw signal: {len(raw):,} rows")

    # --- Cross-sectional transformation ------------------------------
    signal_frame = prepare_factor_signal_frame(
        raw,
        factor_name=factor_name,
        value_col="raw_value",
        universe_label=universe,
        do_winsorize=do_winsorize,
        winsorize_pcts=winsorize_pcts,
        do_sector_neutralize=do_sector_neutralize,
    )
    logger.info(f"  prepared signal frame: {len(signal_frame):,} rows")

    if persist_signals:
        n_sig = upsert_factor_signals(signal_frame)
        logger.info(f"  factor_signals upserted: {n_sig:,} rows")

    # --- Forward returns ---------------------------------------------
    if ensure_forward_returns:
        # Cover the full period so the panel can be joined; horizons
        # other than the requested one are also computed (cheap, and
        # the cache is reusable for next time).
        backfill_forward_returns(
            symbols=universe_end,
            start=start, end=end,
            horizons=DEFAULT_HORIZONS,
            universe_label=universe,
        )

    fwd = query_forward_returns(
        horizon_days=horizon_days,
        start=start, end=end,
        symbols=universe_end,
    )
    if fwd.empty:
        raise RuntimeError(
            f"No forward_returns available for horizon={horizon_days} "
            f"in [{start}..{end}]. Set ensure_forward_returns=True or "
            "backfill manually."
        )
    logger.info(f"  forward_returns: {len(fwd):,} rows (h={horizon_days})")

    # --- Join signal & forward returns -------------------------------
    panel = signal_frame.merge(
        fwd[["symbol", "date", "horizon_days", "fwd_return", "high_return"]],
        on=["symbol", "date"], how="inner",
    )
    if panel.empty:
        raise RuntimeError(
            "Joined panel is empty - signal and forward returns do not "
            "share any (symbol, date) pairs."
        )
    logger.info(f"  joined panel: {len(panel):,} rows")

    # rank_value / z_score may be NaN on days where the cross-section is
    # too small; drop those for metric computation (still kept in DB).
    panel_for_metrics = panel.dropna(subset=["rank_value", "fwd_return"]).copy()
    # Ensure numeric dtypes (database NUMERIC columns come back as Decimal)
    panel_for_metrics["rank_value"] = pd.to_numeric(
        panel_for_metrics["rank_value"], errors="coerce"
    )
    panel_for_metrics["fwd_return"] = pd.to_numeric(
        panel_for_metrics["fwd_return"], errors="coerce"
    )

    # --- Metrics -----------------------------------------------------
    metrics = compute_all_metrics(
        panel_for_metrics,
        horizon_days=horizon_days,
        signal_col="rank_value",
        return_col="fwd_return",
        n_quantiles=n_quantiles,
        min_obs_per_day=min_obs_per_day,
    )
    logger.info(
        f"  metrics: ic_mean={metrics.get('ic_mean'):.4f} "
        f"rank_icir={metrics.get('rank_icir'):.4f} "
        f"sharpe={metrics.get('long_short_sharpe'):.4f}"
    )

    # --- Persist run + metrics ---------------------------------------
    run_id = make_run_id(factor_name, universe)
    if persist_run:
        params = {
            "winsorize": list(winsorize_pcts) if do_winsorize else None,
            "sector_neutral": do_sector_neutralize,
            "n_quantiles": n_quantiles,
            "min_obs_per_day": min_obs_per_day,
            "signal_kwargs": signal_kwargs,
        }
        insert_eval_run(
            run_id=run_id,
            factor_name=factor_name,
            universe=universe,
            start_date=start,
            end_date=end,
            horizon_days=horizon_days,
            params=params,
            n_observations=int(len(panel_for_metrics)),
            notes=notes,
        )
        n_metrics = insert_eval_metrics(run_id, metrics)
        logger.success(
            f"  eval_run persisted: run_id={run_id} ({n_metrics} metrics)"
        )

    return run_id, metrics, panel


def evaluate_factor_across_horizons(
    factor_name: str,
    universe: str,
    start: date,
    end: date,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    **kwargs,
) -> dict[int, tuple[str, dict, pd.DataFrame]]:
    """Convenience: evaluate the same factor at multiple horizons.

    Returns dict mapping horizon -> (run_id, metrics, panel).
    The forward_returns cache is computed once (on the first horizon).
    """
    out: dict[int, tuple[str, dict, pd.DataFrame]] = {}
    first = True
    for h in horizons:
        result = evaluate_factor(
            factor_name=factor_name,
            universe=universe,
            start=start, end=end,
            horizon_days=h,
            ensure_forward_returns=first,
            **kwargs,
        )
        out[h] = result
        first = False
    return out


if __name__ == "__main__":
    # Smoke test: momentum_20d on KOSPI, last 6 months, horizon=5
    end = date.today()
    start = end - timedelta(days=180)
    run_id, metrics, _ = evaluate_factor(
        factor_name="momentum_20d",
        universe="KOSPI",
        start=start, end=end,
        horizon_days=5,
        notes="smoke test from __main__",
    )
    logger.success(f"Smoke test done. run_id={run_id}")
    for k, v in metrics.items():
        logger.info(f"  {k}: {v}")
