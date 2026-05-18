"""Paper broker: KIS price-query only, no KIS order API.

The simulator reads fill prices from daily_prices and computes
fee/slippage from the spec's execution settings. No order is ever
sent to KIS — that's the entire point of this layer.
"""
from __future__ import annotations

from src.trading.simulator.broker import (
    FillPlan,
    PaperBroker,
    load_close_for_symbols,
    load_close_with_fallback,
)
from src.trading.simulator.fees import compute_buy_fees, compute_sell_fees

__all__ = [
    "FillPlan",
    "PaperBroker",
    "compute_buy_fees",
    "compute_sell_fees",
    "load_close_for_symbols",
    "load_close_with_fallback",
]
