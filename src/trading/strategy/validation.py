"""StrategySpec validation that requires DB / catalog access.

Pydantic does the shape and per-field range checks at construction. This
module does the cross-cutting checks that need the catalog:

  - factor names exist in `src.trading.factors.catalog`
  - universe is supported by `src.research.universe.SUPPORTED_UNIVERSES`
  - top_n × max_weight_per_stock is enough to fill 100%
"""
from __future__ import annotations

from src.research.universe import SUPPORTED_UNIVERSES
from src.trading.factors.catalog import is_known_factor, list_factor_names
from src.trading.strategy.declarative import StrategySpec


class SpecValidationError(ValueError):
    """One or more validation errors against the live catalog."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_spec(spec: StrategySpec, *, raise_on_error: bool = True) -> list[str]:
    """Cross-check a parsed StrategySpec against the live catalog.

    Returns list of error message strings. Empty list = spec is valid.
    """
    errors: list[str] = []

    # 1) universe
    if spec.universe.base not in SUPPORTED_UNIVERSES:
        errors.append(
            f"universe.base '{spec.universe.base}' not in "
            f"{SUPPORTED_UNIVERSES}"
        )

    # 2) every factor name is in the catalog
    known = set(list_factor_names())
    for comp in spec.signal.components:
        if not is_known_factor(comp.factor):
            errors.append(
                f"signal.components.factor '{comp.factor}' is not in the "
                f"factor catalog. Known: {sorted(known)[:5]} ... (total "
                f"{len(known)})"
            )

    # 3) duplicate factor names
    factor_names = [c.factor for c in spec.signal.components]
    if len(factor_names) != len(set(factor_names)):
        dupes = {f for f in factor_names if factor_names.count(f) > 1}
        errors.append(
            f"signal.components has duplicate factors: {sorted(dupes)}"
        )

    # 4) feasibility: top_n × max_weight_per_stock ≥ (1 - cash_buffer)
    sel = spec.selection
    port = spec.portfolio
    achievable = sel.top_n * port.max_weight_per_stock
    target = 1.0 - port.cash_buffer_pct
    if achievable + 1e-9 < target:
        errors.append(
            f"selection.top_n ({sel.top_n}) × portfolio.max_weight_per_stock "
            f"({port.max_weight_per_stock}) = {achievable:.4f} cannot reach "
            f"target invested fraction {target:.4f}."
        )

    if errors and raise_on_error:
        raise SpecValidationError(errors)
    return errors


def required_factors_for_spec(spec: StrategySpec) -> list[str]:
    """Return the sorted unique list of factor names referenced by spec."""
    return sorted({c.factor for c in spec.signal.components})
