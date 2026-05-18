"""DeclarativeStrategy — JSON spec drives a BaseStrategy implementation.

A bot can be defined entirely by a JSONB spec stored in
`eod_bots.declarative_spec`. The spec is parsed into a `StrategySpec`
Pydantic model and wrapped by `DeclarativeStrategy`, which then plugs
into the rest of the bot engine through `BaseStrategy`.

Why declarative is the default:
    - Created/edited via REST API (no code deploy needed).
    - Reproducible: the JSON IS the strategy. Same spec → same output.
    - Auditable: every spec version stored in eod_bot_spec_history.
    - Safe: no `eval` / code injection — only known parameters.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from src.trading.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Pydantic sub-models for the JSON spec
# ---------------------------------------------------------------------------


class UniverseFilter(BaseModel):
    """Universe filter — applied AFTER the base universe is fetched."""

    base: Literal["KOSPI", "KOSDAQ", "ALL", "KOSPI200"] = "KOSPI"
    min_market_cap_krw: int | None = Field(
        None, description="KRW minimum market cap."
    )
    min_avg_value_krw_20d: int | None = Field(
        None, description="20일 평균 거래대금 하한 (KRW)."
    )
    min_listing_days: int = Field(
        60, description="신규 상장 후 N일 미만 종목은 배제."
    )
    exclude_sectors: list[str] = Field(default_factory=list)
    exclude_symbols: list[str] = Field(default_factory=list)


class FactorComponent(BaseModel):
    """One factor + weight + optional hard-rank filter."""

    factor: str = Field(..., description="factor_name (must be in catalog).")
    weight: float = Field(..., ge=0.0, le=1.0, description="가중치.")
    min_rank: float | None = Field(
        None, ge=0.0, le=1.0,
        description=(
            "0.0~1.0. 이 값 미만의 rank_value 를 가진 종목은 종합 점수 계산에서 "
            "이 팩터에 대해 NaN 처리."
        ),
    )


class SignalSpec(BaseModel):
    """How to combine the factor components into a composite score."""

    combination: Literal["weighted_rank_sum"] = "weighted_rank_sum"
    factor_source: Literal["rank_value", "z_score", "neutral_value", "raw_value"] = (
        "rank_value"
    )
    components: list[FactorComponent] = Field(..., min_length=1)
    missing_factor_policy: Literal["drop_row", "use_zero", "skip_component"] = "drop_row"
    min_factor_coverage: float = Field(
        0.8, ge=0.0, le=1.0,
        description="이 비율 이상의 component 가 데이터 있어야 종합 점수 인정.",
    )

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "SignalSpec":
        total = sum(c.weight for c in self.components)
        if not 0.99 <= total <= 1.01:
            raise ValueError(
                f"signal.components weights sum to {total:.4f}, must be ~1.0"
            )
        return self


class SelectionSpec(BaseModel):
    """Pick the target basket from scored eligible symbols."""

    method: Literal["top_n"] = "top_n"
    top_n: int = Field(20, ge=1, le=500)
    min_composite_percentile: float | None = Field(
        None, ge=0.0, le=1.0,
        description=(
            "Top-N 후보 안에서도 composite score 의 cross-sectional percentile 이 "
            "이 값 미만이면 배제."
        ),
    )


class PortfolioSpec(BaseModel):
    """Convert selected symbols to target weights."""

    weighting: Literal["equal_weight"] = "equal_weight"
    max_weight_per_stock: float = Field(0.05, gt=0, le=0.5)
    max_weight_per_sector: float = Field(0.30, gt=0, le=1.0)
    cash_buffer_pct: float = Field(0.02, ge=0, le=0.2)


class RebalanceSpec(BaseModel):
    """When and how aggressively to rebalance."""

    frequency: Literal["daily"] = "daily"
    min_holding_days: int = Field(0, ge=0)
    turnover_cap_per_day: float = Field(
        1.0, ge=0.0, le=1.0,
        description="하루 매매대금 / 어제 자산 의 상한. 1.0 = 제한 없음.",
    )
    skip_if_universe_coverage_below: float = Field(
        0.50, ge=0.0, le=1.0,
        description="eligible 중 시그널 점수가 있는 비율이 이 값 미만이면 그날 tick 건너뜀.",
    )


class ExecutionSpec(BaseModel):
    """Where the fill price comes from and how much friction is modeled."""

    fill_price: Literal["next_open", "same_day_close"] = "next_open"
    fee_bps: float = Field(25.0, ge=0.0, le=500.0)
    slippage_bps: float = Field(10.0, ge=0.0, le=500.0)
    sell_tax_bps: float = Field(
        23.0, ge=0.0, le=100.0,
        description="매도 시 거래세 (bps). KOSPI/KOSDAQ 일반: 0.23%.",
    )


class StrategySpec(BaseModel):
    """The top-level JSON spec for a declarative strategy."""

    spec_version: str = Field("2.0")
    name: str = Field(...)
    universe: UniverseFilter = Field(default_factory=UniverseFilter)
    signal: SignalSpec
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    portfolio: PortfolioSpec = Field(default_factory=PortfolioSpec)
    rebalance: RebalanceSpec = Field(default_factory=RebalanceSpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)

    @field_validator("name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name cannot be empty")
        if len(v) > 64:
            raise ValueError("name too long (max 64 chars)")
        return v


# ---------------------------------------------------------------------------
# DeclarativeStrategy — BaseStrategy adapter
# ---------------------------------------------------------------------------


class DeclarativeStrategy(BaseStrategy):
    """Adapter: StrategySpec -> BaseStrategy.

    All decision logic is data-driven from the spec; no Python knobs.
    """

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec

    def required_factors(self) -> list[str]:
        return [c.factor for c in self.spec.signal.components]

    def score(self, panel: pd.DataFrame, as_of: date) -> pd.Series:
        """Weighted rank sum with min_rank hard filter and coverage gate."""
        if panel.empty:
            return pd.Series(dtype=float)
        if "symbol" not in panel.columns:
            raise ValueError("score: panel must contain 'symbol' column")

        comps = self.spec.signal.components
        sym_index = panel["symbol"]

        per_comp_values: list[pd.Series] = []
        for c in comps:
            col = c.factor
            if col not in panel.columns:
                per_comp_values.append(
                    pd.Series([np.nan] * len(panel), index=sym_index, name=col)
                )
                continue
            v = pd.to_numeric(panel[col], errors="coerce")
            v.index = sym_index
            v.name = col
            if c.min_rank is not None:
                v = v.where(v >= c.min_rank, other=np.nan)
            per_comp_values.append(v)

        policy = self.spec.signal.missing_factor_policy

        present_matrix = pd.concat(
            [s.notna().astype(int) for s in per_comp_values], axis=1
        )
        coverage = present_matrix.sum(axis=1)
        n_components = len(comps)
        min_required = int(np.ceil(n_components * self.spec.signal.min_factor_coverage))

        composite = pd.Series(0.0, index=sym_index)
        for c, v in zip(comps, per_comp_values):
            if policy == "drop_row":
                composite = composite + c.weight * v
            elif policy == "use_zero":
                composite = composite + c.weight * v.fillna(0.0)
            elif policy == "skip_component":
                contribution = c.weight * v.fillna(0.0)
                composite = composite + contribution
            else:
                raise AssertionError(f"unknown policy: {policy}")

        if policy == "skip_component":
            weights_per_row = pd.Series(0.0, index=sym_index)
            for c, v in zip(comps, per_comp_values):
                weights_per_row = weights_per_row + c.weight * v.notna().astype(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                composite = composite / weights_per_row.replace(0.0, np.nan)

        composite = composite.where(coverage >= min_required, other=np.nan)
        composite.name = "composite_score"
        return composite

    def select(
        self,
        scores: pd.Series,
        eligible: pd.Index,
        sector_map: dict[str, str],
    ) -> dict[str, float]:
        """Top-N with equal weights, per-stock cap and per-sector cap."""
        sel = self.spec.selection
        port = self.spec.portfolio

        s = scores.loc[scores.index.intersection(eligible)].dropna()
        if s.empty:
            return {}

        if sel.min_composite_percentile is not None:
            threshold = s.quantile(sel.min_composite_percentile)
            s = s[s >= threshold]
            if s.empty:
                return {}

        top = s.nlargest(min(sel.top_n, len(s)))
        if top.empty:
            return {}

        investable = max(0.0, 1.0 - port.cash_buffer_pct)
        raw_weight = investable / len(top)
        per_stock = min(raw_weight, port.max_weight_per_stock)

        weights = {sym: per_stock for sym in top.index}
        weights = _apply_sector_cap(weights, sector_map, port.max_weight_per_sector)
        return weights

    def cash_buffer_pct(self) -> float:
        return self.spec.portfolio.cash_buffer_pct

    def min_holding_days(self) -> int:
        return self.spec.rebalance.min_holding_days


def _apply_sector_cap(
    weights: dict[str, float],
    sector_map: dict[str, str],
    cap: float,
) -> dict[str, float]:
    """Enforce per-sector weight cap by proportional trimming."""
    if cap >= 1.0 or not weights:
        return weights
    w = dict(weights)
    for _ in range(20):
        sector_totals: dict[str, float] = {}
        for sym, wt in w.items():
            sec = sector_map.get(sym, "__UNKNOWN__")
            sector_totals[sec] = sector_totals.get(sec, 0.0) + wt
        over = {s: t for s, t in sector_totals.items() if t > cap + 1e-9}
        if not over:
            break
        for sec, total in over.items():
            scale = cap / total
            for sym, wt in list(w.items()):
                if sector_map.get(sym, "__UNKNOWN__") == sec:
                    w[sym] = wt * scale
    return w
