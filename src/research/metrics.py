"""Evaluation metrics for cross-sectional alpha signals.

Definitions used here (standard Grinold-Kahn / Qlib conventions):

    daily IC_t      = Pearson corr of (signal_t, fwd_return_t) across
                      all symbols on day t.
    daily RankIC_t  = Spearman corr (same but on ranks).

    ic_mean         = mean over all days of IC_t.
    ic_std          = stdev over all days of IC_t.
    icir            = ic_mean / ic_std.
    ic_t_stat       = ic_mean * sqrt(N_days)  - simple test against 0.

    hit_rate        = fraction of days where IC_t > 0.

    long_short_*    = portfolio metrics from a top-Q minus bottom-Q
                      strategy where Q is configurable (default 5 ->
                      top 20% vs bottom 20%).

    max_drawdown    = worst peak-to-trough decline of the cumulative
                      long-short return curve.

    turnover_avg    = average fraction of names that ENTERED or LEFT
                      the top-decile portfolio from day to day.

All metrics return float or NaN. NaN is returned (not raised) when there
isn't enough data - the caller is expected to handle NaN explicitly.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from src.utils.logger import logger

# Number of trading days in a year - used for annualization. The Korean
# market has ~245 trading days/year; we use 252 to match the global
# convention (and so cross-market comparisons stay consistent).
TRADING_DAYS_PER_YEAR = 252


def compute_daily_ic(
    df: pd.DataFrame,
    signal_col: str = "rank_value",
    return_col: str = "fwd_return",
    date_col: str = "date",
    min_obs: int = 10,
) -> pd.DataFrame:
    """For each date, compute Pearson IC and Spearman RankIC between
    `signal_col` and `return_col` across symbols.

    Args:
        df: long DataFrame with rows = (symbol, date) pairs and columns
            including signal_col and return_col.
        signal_col: factor signal column (often rank_value or z_score).
        return_col: forward return column (often fwd_return).
        date_col: column to group by.
        min_obs: minimum number of (signal, return) pairs required to
                 compute a daily IC. Days below this return NaN IC.

    Returns:
        DataFrame indexed implicitly with columns [date, ic, rank_ic, n].
        One row per unique date in `df`. NaN where min_obs not met.
    """
    required = {signal_col, return_col, date_col}
    missing = required - set(df.columns)
    if missing:
        raise KeyError(f"compute_daily_ic missing columns: {missing}")

    work = df[[date_col, signal_col, return_col]].copy()
    work = work.dropna(subset=[signal_col, return_col])

    def _daily(g: pd.DataFrame) -> pd.Series:
        n = len(g)
        if n < min_obs:
            return pd.Series({"ic": np.nan, "rank_ic": np.nan, "n": n})
        # Pearson IC (raw values)
        try:
            ic = float(g[signal_col].corr(g[return_col], method="pearson"))
        except Exception:
            ic = np.nan
        # Spearman RankIC (rank-based)
        try:
            rank_ic = float(g[signal_col].corr(g[return_col], method="spearman"))
        except Exception:
            rank_ic = np.nan
        return pd.Series({"ic": ic, "rank_ic": rank_ic, "n": n})

    daily = work.groupby(date_col, sort=True).apply(
        _daily, include_groups=False
    ).reset_index()
    return daily


def aggregate_ic_metrics(daily_ic: pd.DataFrame) -> dict[str, float]:
    """Aggregate the output of compute_daily_ic into scalar metrics.

    Returns a dict with keys:
        ic_mean, ic_std, icir, ic_t_stat,
        rank_ic_mean, rank_ic_std, rank_icir,
        hit_rate, n_days
    """
    if daily_ic.empty:
        return {
            "ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan,
            "ic_t_stat": np.nan,
            "rank_ic_mean": np.nan, "rank_ic_std": np.nan,
            "rank_icir": np.nan,
            "hit_rate": np.nan, "n_days": 0.0,
        }

    ic = daily_ic["ic"].dropna()
    r_ic = daily_ic["rank_ic"].dropna()
    n_days = float(len(ic))

    def _safe_div(a: float, b: float) -> float:
        if pd.isna(b) or b == 0:
            return float("nan")
        return float(a / b)

    ic_mean = float(ic.mean()) if len(ic) > 0 else float("nan")
    ic_std = float(ic.std(ddof=1)) if len(ic) > 1 else float("nan")
    icir = _safe_div(ic_mean, ic_std) if not pd.isna(ic_std) else float("nan")
    ic_t_stat = (
        float(ic_mean * np.sqrt(n_days))
        if (not pd.isna(ic_mean) and n_days > 0)
        else float("nan")
    )

    r_mean = float(r_ic.mean()) if len(r_ic) > 0 else float("nan")
    r_std = float(r_ic.std(ddof=1)) if len(r_ic) > 1 else float("nan")
    r_icir = _safe_div(r_mean, r_std) if not pd.isna(r_std) else float("nan")

    if len(ic) > 0:
        hit_rate = float((ic > 0).sum() / len(ic))
    else:
        hit_rate = float("nan")

    return {
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": icir,
        "ic_t_stat": ic_t_stat,
        "rank_ic_mean": r_mean,
        "rank_ic_std": r_std,
        "rank_icir": r_icir,
        "hit_rate": hit_rate,
        "n_days": n_days,
    }


def compute_quantile_portfolio_returns(
    df: pd.DataFrame,
    signal_col: str = "rank_value",
    return_col: str = "fwd_return",
    date_col: str = "date",
    n_quantiles: int = 5,
    min_obs_per_day: int = 20,
) -> pd.DataFrame:
    """Compute daily portfolio returns for top-Q minus bottom-Q strategy.

    For each date:
        1. Drop NaN signals and returns.
        2. Skip if fewer than min_obs_per_day rows remain.
        3. Sort by signal and split into n_quantiles equal-size buckets.
        4. Bucket return = equal-weighted mean of fwd_return within bucket.
        5. long_short_return = top bucket return - bottom bucket return.

    Returns:
        DataFrame with columns [date, top_return, bottom_return,
        long_short_return, n].

    NOTE on overlapping returns:
        If horizon > 1 and you call this on consecutive base dates, the
        long_short_return series has horizon-day overlap. The Sharpe
        computed downstream is then slightly inflated by serial
        correlation. For Phase 1 we accept this; a proper fix is to
        either (a) use non-overlapping base dates or (b) apply a
        Newey-West correction. Both are out of scope here.
    """
    required = {signal_col, return_col, date_col}
    missing = required - set(df.columns)
    if missing:
        raise KeyError(f"compute_quantile_portfolio_returns missing: {missing}")

    if n_quantiles < 2:
        raise ValueError(f"n_quantiles must be >= 2, got {n_quantiles}")

    work = df[[date_col, signal_col, return_col]].dropna(
        subset=[signal_col, return_col]
    ).copy()

    def _daily(g: pd.DataFrame) -> pd.Series:
        n = len(g)
        if n < min_obs_per_day or n < n_quantiles:
            return pd.Series({
                "top_return": np.nan, "bottom_return": np.nan,
                "long_short_return": np.nan, "n": n,
            })
        # qcut with duplicates='drop' returns fewer bins when many ties.
        # If we don't get n_quantiles bins, fall back to NaN for the day.
        try:
            bins = pd.qcut(
                g[signal_col], q=n_quantiles,
                labels=False, duplicates="drop",
            )
        except Exception:
            return pd.Series({
                "top_return": np.nan, "bottom_return": np.nan,
                "long_short_return": np.nan, "n": n,
            })
        unique_bins = sorted(bins.dropna().unique())
        if len(unique_bins) < n_quantiles:
            return pd.Series({
                "top_return": np.nan, "bottom_return": np.nan,
                "long_short_return": np.nan, "n": n,
            })

        top_label = unique_bins[-1]
        bot_label = unique_bins[0]
        top_ret = float(g.loc[bins == top_label, return_col].mean())
        bot_ret = float(g.loc[bins == bot_label, return_col].mean())
        return pd.Series({
            "top_return": top_ret,
            "bottom_return": bot_ret,
            "long_short_return": top_ret - bot_ret,
            "n": n,
        })

    daily = work.groupby(date_col, sort=True).apply(
        _daily, include_groups=False
    ).reset_index()
    return daily


def compute_sharpe(
    returns: pd.Series,
    horizon_days: int,
) -> float:
    """Annualized Sharpe ratio of a daily-frequency return series.

    Args:
        returns: pandas Series of per-period returns (the period length
                 equals `horizon_days` trading days).
        horizon_days: how many trading days each entry covers. Used to
                      convert horizon-period mean/std into per-day
                      equivalents before annualizing.

    Returns:
        Annualized Sharpe = mean_per_day / std_per_day * sqrt(252).

    The horizon adjustment matters: if you computed top-bottom returns
    on a 20-day horizon, each row represents ~20 days of pnl, not 1 day.
    Treating it as daily would over-annualize.
    """
    r = returns.dropna()
    if len(r) < 2:
        return float("nan")
    if horizon_days < 1:
        raise ValueError("horizon_days must be >= 1")

    mean_per_period = float(r.mean())
    std_per_period = float(r.std(ddof=1))
    if std_per_period == 0:
        return float("nan")

    mean_per_day = mean_per_period / horizon_days
    std_per_day = std_per_period / np.sqrt(horizon_days)
    if std_per_day == 0:
        return float("nan")
    return float(mean_per_day / std_per_day * np.sqrt(TRADING_DAYS_PER_YEAR))


def compute_annualized_return(
    returns: pd.Series,
    horizon_days: int,
) -> float:
    """Approximate annualized return from a per-period return series.

    Uses simple compounding of the period mean - matches how Qlib /
    most papers report this for long-short portfolios.
    """
    r = returns.dropna()
    if len(r) == 0 or horizon_days < 1:
        return float("nan")
    periods_per_year = TRADING_DAYS_PER_YEAR / horizon_days
    mean_r = float(r.mean())
    # geometric annualization: (1 + mean_r) ** periods_per_year - 1
    # Capped to avoid overflow for absurd inputs.
    try:
        return float((1.0 + mean_r) ** periods_per_year - 1.0)
    except OverflowError:
        return float("nan")


def compute_max_drawdown(returns: pd.Series) -> float:
    """Max drawdown of the cumulative return curve.

    Returns a value in [-1, 0], or 0.0 if the series never drew down,
    or NaN if the series is empty.
    """
    r = returns.dropna()
    if len(r) == 0:
        return float("nan")
    equity = (1.0 + r).cumprod()
    peak = equity.cummax()
    dd = (equity / peak) - 1.0
    return float(dd.min())


def compute_turnover(
    df: pd.DataFrame,
    signal_col: str = "rank_value",
    date_col: str = "date",
    symbol_col: str = "symbol",
    top_quantile: float = 0.2,
) -> float:
    """Average daily turnover of the top-quantile basket.

    Defined as: mean over consecutive day pairs of
        |today_top_set XOR yesterday_top_set| / |yesterday_top_set|

    Range: [0, 2] where 0 = same basket every day, 1 = full replacement.

    Returns NaN if fewer than 2 days of usable data.
    """
    if not (0.0 < top_quantile <= 0.5):
        raise ValueError(f"top_quantile must be in (0, 0.5], got {top_quantile}")

    work = df[[date_col, symbol_col, signal_col]].dropna().copy()
    work = work.sort_values([date_col, signal_col], ascending=[True, False])

    daily_top: dict[date, set[str]] = {}
    for d, g in work.groupby(date_col, sort=True):
        n = len(g)
        if n == 0:
            continue
        cutoff = max(1, int(np.ceil(n * top_quantile)))
        daily_top[d] = set(g.iloc[:cutoff][symbol_col])

    dates = sorted(daily_top.keys())
    if len(dates) < 2:
        return float("nan")

    turnovers: list[float] = []
    for prev, curr in zip(dates[:-1], dates[1:]):
        prev_set = daily_top[prev]
        curr_set = daily_top[curr]
        if not prev_set:
            continue
        diff = prev_set.symmetric_difference(curr_set)
        turnovers.append(len(diff) / len(prev_set))
    if not turnovers:
        return float("nan")
    return float(np.mean(turnovers))


def compute_portfolio_metrics(
    panel: pd.DataFrame,
    horizon_days: int,
    signal_col: str = "rank_value",
    return_col: str = "fwd_return",
    date_col: str = "date",
    symbol_col: str = "symbol",
    n_quantiles: int = 5,
    min_obs_per_day: int = 20,
) -> dict[str, float]:
    """Run the full long-short portfolio analysis and return scalar metrics.

    Returns a dict with keys:
        long_short_return  (annualized)
        long_short_sharpe  (annualized)
        max_drawdown
        turnover_avg
    """
    daily = compute_quantile_portfolio_returns(
        panel,
        signal_col=signal_col, return_col=return_col, date_col=date_col,
        n_quantiles=n_quantiles, min_obs_per_day=min_obs_per_day,
    )
    ls_ret = daily["long_short_return"]
    sharpe = compute_sharpe(ls_ret, horizon_days=horizon_days)
    ann_ret = compute_annualized_return(ls_ret, horizon_days=horizon_days)
    mdd = compute_max_drawdown(ls_ret)
    turnover = compute_turnover(
        panel, signal_col=signal_col, date_col=date_col, symbol_col=symbol_col,
        top_quantile=1.0 / n_quantiles,
    )
    return {
        "long_short_return": ann_ret,
        "long_short_sharpe": sharpe,
        "max_drawdown": mdd,
        "turnover_avg": turnover,
    }


def compute_all_metrics(
    panel: pd.DataFrame,
    horizon_days: int,
    signal_col: str = "rank_value",
    return_col: str = "fwd_return",
    date_col: str = "date",
    symbol_col: str = "symbol",
    n_quantiles: int = 5,
    min_obs_per_day: int = 20,
) -> dict[str, float]:
    """Compute the canonical metric bundle expected by eval_metrics table.

    Combines IC metrics (compute_daily_ic + aggregate_ic_metrics) with
    portfolio metrics (compute_portfolio_metrics) into one dict matching
    the 'standard set' documented in migrations/014_research_infra.sql.
    """
    daily_ic = compute_daily_ic(
        panel, signal_col=signal_col, return_col=return_col, date_col=date_col,
        min_obs=min_obs_per_day,
    )
    ic_bundle = aggregate_ic_metrics(daily_ic)
    port_bundle = compute_portfolio_metrics(
        panel, horizon_days=horizon_days,
        signal_col=signal_col, return_col=return_col,
        date_col=date_col, symbol_col=symbol_col,
        n_quantiles=n_quantiles, min_obs_per_day=min_obs_per_day,
    )
    out = {**ic_bundle, **port_bundle}
    return out
