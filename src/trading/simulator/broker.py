"""PaperBroker — simulated execution against daily_prices.

Responsibilities:
    - Resolve raw fill price for a (symbol, decision_date) given the
      spec's fill_price setting:
          next_open      → daily_prices.open on next business day
          same_day_close → daily_prices.close on decision_date
    - Apply slippage and compute fees.
    - Snap quantities to integer shares (Korean market is whole-share).
    - Refuse fills when daily_prices is missing.

What this broker does NOT do:
    - Talk to KIS (no order API calls).
    - Mutate any persistent state. The runner does the DB writes after
      collecting FillPlan results.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text

from src.db.connection import get_engine
from src.trading.simulator.fees import (
    apply_slippage_buy,
    apply_slippage_sell,
    compute_buy_fees,
    compute_sell_fees,
)
from src.trading.strategy.declarative import ExecutionSpec
from src.utils.logger import logger


@dataclass(frozen=True)
class FillPlan:
    """Result of broker.plan_fill — what would happen if we executed."""
    symbol: str
    side: str  # 'BUY' or 'SELL'
    quantity: int
    raw_price: float
    fill_price: float
    fill_value: float
    fee: float
    slippage_bps: float
    fill_source: str
    fill_date: date


class PaperBroker:
    """Simulated broker using daily_prices for fills.

    Stateless across calls — pass exec_spec per call.
    """

    def __init__(self, exec_spec: ExecutionSpec) -> None:
        self.exec_spec = exec_spec

    def resolve_fill_price(
        self,
        symbol: str,
        decision_date: date,
    ) -> tuple[float | None, date | None]:
        """Return (raw_price, fill_date) according to exec_spec.fill_price."""
        if self.exec_spec.fill_price == "same_day_close":
            price = _load_close(symbol, decision_date)
            if price is None:
                return None, None
            return price, decision_date

        # next_open: search forward up to 7 calendar days
        for offset in range(1, 8):
            d = decision_date + timedelta(days=offset)
            price = _load_open(symbol, d)
            if price is not None:
                return price, d
        return None, None

    def plan_buy(
        self,
        symbol: str,
        target_value: float,
        decision_date: date,
        max_cash: float,
    ) -> FillPlan | None:
        """Plan a BUY for approximately `target_value` KRW worth of `symbol`."""
        if target_value <= 0:
            return None
        raw, fill_date = self.resolve_fill_price(symbol, decision_date)
        if raw is None or fill_date is None or raw <= 0:
            logger.warning(
                f"[broker] BUY {symbol} {decision_date}: no fill price"
            )
            return None
        slip_bps = self.exec_spec.slippage_bps
        fill_price = apply_slippage_buy(raw, slip_bps)

        qty_target = int(target_value / fill_price)
        if qty_target <= 0:
            return None

        # Cap by cash
        qty = qty_target
        while qty > 0:
            fv = qty * fill_price
            fee = compute_buy_fees(fv, self.exec_spec.fee_bps)
            if fv + fee <= max_cash + 1e-6:
                break
            qty -= 1
        if qty <= 0:
            return None

        fill_value = qty * fill_price
        fee = compute_buy_fees(fill_value, self.exec_spec.fee_bps)
        return FillPlan(
            symbol=symbol,
            side="BUY",
            quantity=qty,
            raw_price=float(raw),
            fill_price=float(fill_price),
            fill_value=float(fill_value),
            fee=float(fee),
            slippage_bps=float(slip_bps),
            fill_source="daily_prices",
            fill_date=fill_date,
        )

    def plan_sell(
        self,
        symbol: str,
        quantity: int,
        decision_date: date,
    ) -> FillPlan | None:
        """Plan a SELL of `quantity` shares of `symbol`."""
        if quantity <= 0:
            return None
        raw, fill_date = self.resolve_fill_price(symbol, decision_date)
        if raw is None or fill_date is None or raw <= 0:
            logger.warning(
                f"[broker] SELL {symbol} {decision_date}: no fill price"
            )
            return None

        slip_bps = self.exec_spec.slippage_bps
        fill_price = apply_slippage_sell(raw, slip_bps)
        fill_value = quantity * fill_price
        fee = compute_sell_fees(
            fill_value,
            self.exec_spec.fee_bps,
            self.exec_spec.sell_tax_bps,
        )
        return FillPlan(
            symbol=symbol,
            side="SELL",
            quantity=int(quantity),
            raw_price=float(raw),
            fill_price=float(fill_price),
            fill_value=float(fill_value),
            fee=float(fee),
            slippage_bps=float(slip_bps),
            fill_source="daily_prices",
            fill_date=fill_date,
        )


# ---------------------------------------------------------------------------
# Price lookups
# ---------------------------------------------------------------------------


def _load_close(symbol: str, target_date: date) -> float | None:
    """Most recent daily_prices.close on or before target_date (window 5d)."""
    sql = text("""
        SELECT close FROM daily_prices
         WHERE symbol = :sym
           AND date BETWEEN :lo AND :hi
           AND close IS NOT NULL AND close > 0
         ORDER BY date DESC LIMIT 1
    """)
    lo = target_date - timedelta(days=5)
    with get_engine().connect() as conn:
        row = conn.execute(
            sql, {"sym": symbol, "lo": lo, "hi": target_date}
        ).first()
    return float(row[0]) if row else None


def _load_open(symbol: str, target_date: date) -> float | None:
    """daily_prices.open on exactly target_date."""
    sql = text("""
        SELECT open FROM daily_prices
         WHERE symbol = :sym
           AND date = :d
           AND open IS NOT NULL AND open > 0
        LIMIT 1
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"sym": symbol, "d": target_date}).first()
    return float(row[0]) if row else None


def load_close_for_symbols(
    symbols: list[str],
    target_date: date,
) -> dict[str, float]:
    """Bulk-load daily_prices.close for `symbols` on `target_date`."""
    if not symbols:
        return {}
    sql = text("""
        SELECT symbol, close FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date = :d
           AND close IS NOT NULL AND close > 0
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"syms": list(symbols), "d": target_date}).all()
    return {r[0]: float(r[1]) for r in rows}


def load_close_with_fallback(
    symbols: list[str],
    target_date: date,
    lookback_days: int = 5,
) -> dict[str, float]:
    """Like load_close_for_symbols but allows look-back on holidays."""
    if not symbols:
        return {}
    sql = text("""
        SELECT DISTINCT ON (symbol) symbol, close
          FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :lo AND :d
           AND close IS NOT NULL AND close > 0
         ORDER BY symbol, date DESC
    """)
    lo = target_date - timedelta(days=lookback_days)
    with get_engine().connect() as conn:
        rows = conn.execute(
            sql, {"syms": list(symbols), "lo": lo, "d": target_date}
        ).all()
    return {r[0]: float(r[1]) for r in rows}
