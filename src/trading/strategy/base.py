"""BaseStrategy — abstract contract for every bot strategy.

Both DeclarativeStrategy (JSON-driven) and Plugin strategies (Python
subclasses) implement this interface so the bot runner doesn't need
to know which flavor it's executing.

Lifecycle in one daily tick:

    1. runner asks `strategy.required_factors()` to know which columns
       to pull from factor_signals.
    2. runner loads the wide panel and passes it to `strategy.score(...)`
       which returns a Series[symbol -> composite_score].
    3. runner applies risk filters externally; the eligible symbol
       set is given to `strategy.select(...)` which picks final weights.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class BaseStrategy(ABC):
    """Contract: factor-based cross-sectional long-only strategy.

    All concrete strategies (declarative + plugins) implement these
    three methods.
    """

    @abstractmethod
    def required_factors(self) -> list[str]:
        """Names of factors this strategy reads from the panel.

        Must be names known to `src.trading.factors.catalog`. The runner
        loads exactly these columns from factor_signals.
        """

    @abstractmethod
    def score(
        self,
        panel: pd.DataFrame,
        as_of: date,
    ) -> pd.Series:
        """Composite score per symbol on `as_of`.

        Returns:
            pd.Series indexed by symbol with float scores. Higher =
            stronger buy signal. NaN means "no opinion / skip this
            symbol".
        """

    @abstractmethod
    def select(
        self,
        scores: pd.Series,
        eligible: pd.Index,
        sector_map: dict[str, str],
    ) -> dict[str, float]:
        """Pick target portfolio weights from scored symbols.

        Returns:
            dict[symbol -> target_weight]. Weights are fractions of
            total_value, sum should be ≤ 1.0 (the remainder stays as
            cash). All weights must be ≥ 0 (long-only).
        """

    # --- Optional hooks with sensible defaults ---

    def cash_buffer_pct(self) -> float:
        """Fraction of total_value to keep as cash (not invested)."""
        return 0.02

    def min_holding_days(self) -> int:
        """Don't sell positions younger than this many calendar days."""
        return 0
