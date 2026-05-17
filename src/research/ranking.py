"""Cross-sectional ranking and normalization utilities.

These are the standard transformations applied to factor signals before
they're compared against forward returns. The order is typically:

    raw_value
       ->  winsorize (clip extreme tails per day)
       ->  (optionally) sector_neutralize
       ->  cross_sectional_zscore  OR  cross_sectional_rank

Every function expects a "long" DataFrame:
    columns include at minimum: symbol, date, <value_col>
    one row per (symbol, date)

Functions never persist - they return a DataFrame the caller can
upsert via repositories.upsert_factor_signals.

Key invariants:
    - All cross-sectional ops are applied PER `date` only (no leak
      across dates).
    - NaN values in <value_col> are preserved (not imputed) - the
      caller decides whether NaN means "no signal" or should be dropped.
    - sector_neutralize uses the `tickers.sector` mapping at evaluation
      time. If sector data is missing for a symbol, the row is kept
      and gets its own "unknown" sector (the global mean acts as the
      group mean).
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from src.db.connection import session_scope
from src.utils.logger import logger


def winsorize(
    df: pd.DataFrame,
    value_col: str,
    lower_pct: float = 0.01,
    upper_pct: float = 0.99,
    date_col: str = "date",
) -> pd.DataFrame:
    """Clip values to the [lower_pct, upper_pct] quantile range PER date.

    Args:
        df: long DataFrame with at least date_col and value_col.
        value_col: column to clip in place (returned as a NEW DataFrame).
        lower_pct: lower quantile threshold (0.01 = 1st percentile).
        upper_pct: upper quantile threshold (0.99 = 99th percentile).
        date_col: column name to group by.

    Returns:
        New DataFrame (same shape as input) with value_col clipped.
        NaN values are preserved unchanged.

    Why per-date: same value can be an outlier on one day and median on
    another. Clipping globally would distort cross-sectional comparison.
    """
    if value_col not in df.columns:
        raise KeyError(f"value_col '{value_col}' not in DataFrame")
    if not (0.0 <= lower_pct < upper_pct <= 1.0):
        raise ValueError(
            f"Bad quantile range: lower_pct={lower_pct}, upper_pct={upper_pct}"
        )

    out = df.copy()

    def _clip_group(s: pd.Series) -> pd.Series:
        if s.notna().sum() < 2:
            return s
        lo = s.quantile(lower_pct)
        hi = s.quantile(upper_pct)
        return s.clip(lower=lo, upper=hi)

    out[value_col] = out.groupby(date_col, group_keys=False)[value_col].transform(
        _clip_group
    )
    return out


def cross_sectional_rank(
    df: pd.DataFrame,
    value_col: str,
    out_col: str = "rank_value",
    date_col: str = "date",
    ascending: bool = True,
) -> pd.DataFrame:
    """Per-date percentile rank in [0, 1] of `value_col`.

    Args:
        df: long DataFrame.
        value_col: column to rank (NaN handled by pandas, propagated to NaN rank).
        out_col: name for the new rank column.
        date_col: column to group by.
        ascending: if True, larger value -> larger rank (1.0 = max). This
                   is the "high signal = strong buy" convention. Reverse
                   when ranking something like volatility where smaller
                   is better.

    Returns:
        New DataFrame with `out_col` appended.

    Convention:
        rank_value of 0.0 means "smallest signal on this date".
        rank_value of 1.0 means "largest signal on this date".
        rank_value of 0.5 means "median signal on this date".
    """
    if value_col not in df.columns:
        raise KeyError(f"value_col '{value_col}' not in DataFrame")

    out = df.copy()

    def _rank_group(s: pd.Series) -> pd.Series:
        # pandas rank(pct=True) gives 1/n .. 1.0; we rescale to 0..1
        n = s.notna().sum()
        if n < 2:
            return pd.Series([float("nan")] * len(s), index=s.index)
        ranks = s.rank(method="average", ascending=ascending, pct=True)
        # rescale: pct=True returns in (0, 1], we want [0, 1] using
        # (rank - 1/n) / (1 - 1/n). This makes the smallest value -> 0
        # and largest -> 1 exactly.
        return (ranks - 1.0 / n) / (1.0 - 1.0 / n)

    out[out_col] = out.groupby(date_col, group_keys=False)[value_col].transform(
        _rank_group
    )
    return out


def cross_sectional_zscore(
    df: pd.DataFrame,
    value_col: str,
    out_col: str = "z_score",
    date_col: str = "date",
) -> pd.DataFrame:
    """Per-date z-score: (x - mean) / std. NaN preserved.

    Robust to all-equal groups (returns NaN z-scores when std == 0,
    avoiding divide-by-zero warnings).
    """
    if value_col not in df.columns:
        raise KeyError(f"value_col '{value_col}' not in DataFrame")

    out = df.copy()

    def _zscore_group(s: pd.Series) -> pd.Series:
        n = s.notna().sum()
        if n < 2:
            return pd.Series([float("nan")] * len(s), index=s.index)
        mu = s.mean()
        sigma = s.std(ddof=0)
        if sigma == 0 or pd.isna(sigma):
            return pd.Series([float("nan")] * len(s), index=s.index)
        return (s - mu) / sigma

    out[out_col] = out.groupby(date_col, group_keys=False)[value_col].transform(
        _zscore_group
    )
    return out


def sector_neutralize(
    df: pd.DataFrame,
    value_col: str,
    out_col: str = "neutral_value",
    date_col: str = "date",
    symbol_col: str = "symbol",
) -> pd.DataFrame:
    """Subtract sector mean per date (a.k.a. sector demean).

    This removes first-order sector effects so the signal reflects
    within-sector ranking. Symbols with missing sector (NULL in tickers)
    are grouped under "__UNKNOWN__" - this is intentionally conservative
    because dropping them would shrink the universe silently.

    Args:
        df: long DataFrame.
        value_col: column to demean.
        out_col: name for the demeaned column.
        date_col, symbol_col: dataframe column names.

    Returns:
        New DataFrame with `out_col` appended. value_col is unchanged.

    Caveat:
        This is a simple group-mean subtraction. A more rigorous
        treatment is a per-date OLS regression on sector dummies and
        keeping the residual (Barra-style). For Phase 1, demean is
        usually enough and much faster.
    """
    if value_col not in df.columns:
        raise KeyError(f"value_col '{value_col}' not in DataFrame")

    sectors = _load_symbol_sectors(df[symbol_col].unique().tolist())
    out = df.copy()
    out["_sector"] = out[symbol_col].map(sectors).fillna("__UNKNOWN__")

    out[out_col] = out[value_col] - out.groupby(
        [date_col, "_sector"], group_keys=False
    )[value_col].transform("mean")

    out = out.drop(columns=["_sector"])
    return out


def _load_symbol_sectors(symbols: list[str]) -> dict[str, str]:
    """Map symbol -> sector from the `tickers` table. NULL sectors are
    omitted from the mapping (the caller substitutes __UNKNOWN__)."""
    if not symbols:
        return {}
    sql = text("""
        SELECT symbol, sector
          FROM tickers
         WHERE symbol = ANY(:syms)
           AND sector IS NOT NULL
    """)
    with session_scope() as session:
        rows = session.execute(sql, {"syms": symbols}).all()
    return {r[0]: r[1] for r in rows}


def prepare_factor_signal_frame(
    df: pd.DataFrame,
    factor_name: str,
    value_col: str,
    universe_label: str | None = None,
    *,
    do_winsorize: bool = True,
    winsorize_pcts: tuple[float, float] = (0.01, 0.99),
    do_sector_neutralize: bool = False,
    rank_ascending: bool = True,
) -> pd.DataFrame:
    """End-to-end transformation pipeline producing rows ready for
    upsert_factor_signals.

    Pipeline:
        1. Optionally winsorize per date.
        2. Optionally sector-neutralize per date (writes neutral_value).
        3. Cross-sectional rank in [0, 1] (writes rank_value).
        4. Cross-sectional z-score (writes z_score).

    Input DataFrame must have columns: symbol, date, <value_col>.

    Output columns (matching FACTOR_SIGNAL_COLUMNS in repositories.py):
        symbol, date, factor_name, raw_value, neutral_value,
        rank_value, z_score, universe.

    The `value_col` of the input becomes `raw_value` in the output.
    """
    if df.empty:
        return pd.DataFrame(columns=[
            "symbol", "date", "factor_name",
            "raw_value", "neutral_value", "rank_value", "z_score",
            "universe",
        ])

    required = {"symbol", "date", value_col}
    missing = required - set(df.columns)
    if missing:
        raise KeyError(f"Input DataFrame missing required columns: {missing}")

    work = df[["symbol", "date", value_col]].rename(
        columns={value_col: "raw_value"}
    ).copy()
    work["date"] = pd.to_datetime(work["date"]).dt.date

    # 1) winsorize raw_value (modifies raw_value in-place)
    rank_source_col = "raw_value"
    if do_winsorize:
        work = winsorize(
            work, value_col="raw_value",
            lower_pct=winsorize_pcts[0],
            upper_pct=winsorize_pcts[1],
        )

    # 2) sector neutralize -> neutral_value (rank/zscore then operate
    #    on neutral_value)
    if do_sector_neutralize:
        work = sector_neutralize(work, value_col="raw_value",
                                 out_col="neutral_value")
        rank_source_col = "neutral_value"
    else:
        work["neutral_value"] = None

    # 3) rank
    work = cross_sectional_rank(
        work, value_col=rank_source_col,
        out_col="rank_value", ascending=rank_ascending,
    )

    # 4) z-score
    work = cross_sectional_zscore(
        work, value_col=rank_source_col, out_col="z_score",
    )

    work["factor_name"] = factor_name
    work["universe"] = universe_label

    out = work[[
        "symbol", "date", "factor_name",
        "raw_value", "neutral_value", "rank_value", "z_score",
        "universe",
    ]]
    logger.debug(
        f"prepare_factor_signal_frame[{factor_name}]: {len(out)} rows"
    )
    return out
