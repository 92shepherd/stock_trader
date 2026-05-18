"""Fee + slippage helpers for the paper broker.

Korean retail typical (rough):
    매수 수수료 ~15 bps
    매도 수수료 ~15 bps
    거래세       23 bps (KOSPI/KOSDAQ; ETF는 면제)

Spec gives `fee_bps` (both directions), `sell_tax_bps`, and
`slippage_bps`. Slippage is applied to the raw price; fees are
charged on the fill value.
"""
from __future__ import annotations

from decimal import Decimal


def apply_slippage_buy(raw_price: float, slippage_bps: float) -> float:
    """Raise price by slippage_bps for a buy. raw_price > 0."""
    return float(raw_price) * (1.0 + slippage_bps / 10_000.0)


def apply_slippage_sell(raw_price: float, slippage_bps: float) -> float:
    """Lower price by slippage_bps for a sell. raw_price > 0."""
    return float(raw_price) * (1.0 - slippage_bps / 10_000.0)


def compute_buy_fees(fill_value: float, fee_bps: float) -> float:
    """Buy-side fee: fill_value × fee_bps / 10000."""
    return float(fill_value) * fee_bps / 10_000.0


def compute_sell_fees(
    fill_value: float, fee_bps: float, sell_tax_bps: float,
) -> float:
    """Sell-side fee + tax: fill_value × (fee_bps + sell_tax_bps) / 10000."""
    return float(fill_value) * (fee_bps + sell_tax_bps) / 10_000.0


def round_to_won(amount: float) -> Decimal:
    """Round KRW amount to 2 decimal places."""
    return Decimal(str(round(float(amount), 2)))
