"""Daily-tick runner for EOD bots.

Flow per (bot, decision_date):

    0. Verify bot is RUNNING (skip if PENDING/STOPPED).
    1. Verify no run exists for (bot, decision_date).
    2. Load strategy (DeclarativeStrategy or plugin).
    3. Build universe via src.research.universe.get_universe.
    4. Load wide factor panel via src.trading.factors.loader.
    5. Apply risk filters.
    6. score() → composite Series.
    7. Coverage gate: if too few symbols scored, SKIPPED.
    8. select() → target weights dict.
    9. Convert target weights → BUY/SELL fills via PaperBroker.
   10. Persist orders, update bot cash, snapshot positions, record PnL.
   11. Mark run as SUCCESS.

STOPPED-safety: at step 0 the runner reads the bot state. If a bot was
stopped between cron scheduling and tick execution, the tick refuses.
"""
from __future__ import annotations

import time
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from src.research.universe import get_universe
from src.trading.factors import factor_coverage, load_factor_panel
from src.trading.repositories.bots import (
    DuplicateTickError,
    apply_orders_to_bot,
    get_bot,
    get_current_spec,
    get_open_positions,
    get_running_bots,
    insert_orders,
    insert_run_log,
    record_daily_pnl,
    snapshot_positions,
    update_bot_state_after_tick,
    update_run_log,
)
from src.trading.simulator.broker import (
    FillPlan,
    PaperBroker,
    load_close_with_fallback,
)
from src.trading.strategy.base import BaseStrategy
from src.trading.strategy.declarative import (
    DeclarativeStrategy,
    ExecutionSpec,
    StrategySpec,
)
from src.trading.strategy.registry import get_plugin_strategy
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_bot_daily(bot_id: uuid.UUID, decision_date: date) -> dict[str, Any]:
    """Run one daily tick for one bot. Returns a result dict.

    Result keys:
        bot_id, decision_date, status (SUCCESS/SKIPPED/FAILED),
        run_id (if a run row was created), reason (for SKIPPED/FAILED),
        n_buys, n_sells, total_value.

    This function NEVER raises for "expected" outcomes — those become
    SKIPPED/FAILED in the return dict. Only programming errors raise.
    """
    started = time.monotonic()
    bot = get_bot(bot_id)

    # --- Gate 0: state ---
    if bot.state != "RUNNING":
        logger.info(
            f"[bot {bot.name}] state={bot.state} — skipping tick on {decision_date}"
        )
        return {
            "bot_id": bot_id,
            "decision_date": decision_date,
            "status": "SKIPPED",
            "reason": f"bot_state_{bot.state.lower()}",
        }

    # --- Load spec ---
    spec_history = get_current_spec(bot_id)
    if spec_history is None:
        logger.error(f"[bot {bot.name}] no spec history — refusing tick")
        return {
            "bot_id": bot_id, "decision_date": decision_date,
            "status": "FAILED", "reason": "no_spec_history",
        }

    # Build strategy object
    try:
        strategy = _build_strategy(spec_history)
    except Exception as e:
        logger.exception(f"[bot {bot.name}] strategy build failed: {e}")
        return {
            "bot_id": bot_id, "decision_date": decision_date,
            "status": "FAILED", "reason": "strategy_build_failed",
            "error": str(e),
        }

    # --- Open run row (idempotence check) ---
    try:
        run_row = insert_run_log(
            bot_id=bot_id,
            decision_date=decision_date,
            spec_history_id=spec_history.spec_history_id,
        )
    except DuplicateTickError as e:
        logger.info(f"[bot {bot.name}] duplicate tick refused: {e}")
        return {
            "bot_id": bot_id, "decision_date": decision_date,
            "status": "SKIPPED", "reason": "duplicate_tick",
        }

    run_id = run_row.run_id

    try:
        result = _execute_tick(
            bot=bot, strategy=strategy, spec_history=spec_history,
            run_id=run_id, decision_date=decision_date, started_at=started,
        )
        return result
    except Exception as e:
        logger.exception(f"[bot {bot.name}] tick failed unexpectedly: {e}")
        update_run_log(
            run_id=run_id, status="FAILED",
            error_message=str(e),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {
            "bot_id": bot_id, "decision_date": decision_date,
            "status": "FAILED", "run_id": run_id, "reason": "exception",
            "error": str(e),
        }


def run_all_running_bots(decision_date: date) -> list[dict[str, Any]]:
    """Fan-out: run today's tick for every RUNNING bot. Sequential."""
    bots = get_running_bots()
    logger.info(f"[bot runner] {len(bots)} RUNNING bots on {decision_date}")
    results: list[dict[str, Any]] = []
    for b in bots:
        results.append(run_bot_daily(b.bot_id, decision_date))
    return results


# ---------------------------------------------------------------------------
# Strategy construction
# ---------------------------------------------------------------------------


def _build_strategy(spec_history) -> BaseStrategy:
    """Materialize the strategy object from a spec_history row."""
    if spec_history.strategy_kind == "declarative":
        spec = StrategySpec.model_validate(spec_history.spec_json)
        return DeclarativeStrategy(spec)
    elif spec_history.strategy_kind == "plugin":
        cls = get_plugin_strategy(spec_history.plugin_strategy_id)
        if cls is None:
            raise RuntimeError(
                f"plugin strategy '{spec_history.plugin_strategy_id}' "
                f"is not registered"
            )
        return cls()
    else:
        raise RuntimeError(
            f"unknown strategy_kind: {spec_history.strategy_kind}"
        )


# ---------------------------------------------------------------------------
# Tick execution
# ---------------------------------------------------------------------------


def _execute_tick(
    *,
    bot,
    strategy: BaseStrategy,
    spec_history,
    run_id: uuid.UUID,
    decision_date: date,
    started_at: float,
) -> dict[str, Any]:
    """Heart of the daily tick."""

    # --- 1. Universe ---
    universe_base = bot.universe
    syms_universe = get_universe(universe_base, decision_date)
    if not syms_universe:
        logger.warning(
            f"[bot {bot.name}] SKIPPED on {decision_date}: empty_universe "
            f"(get_universe('{universe_base}', {decision_date}) 는 빈 결과 반환). "
            f"tickers 테이블 명탈상장 상태 혹은 universe 함수 구현 확인필요."
        )
        update_run_log(
            run_id=run_id, status="SKIPPED",
            skip_reason="empty_universe",
            universe_size=0, eligible_size=0, scored_size=0,
            target_size=0, n_buys=0, n_sells=0,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return _result(bot, decision_date, "SKIPPED", "empty_universe",
                       run_id=run_id)

    logger.info(
        f"[bot {bot.name}] {decision_date}: universe={universe_base} "
        f"size={len(syms_universe)}"
    )

    # --- 2. Factor panel ---
    required = strategy.required_factors()
    if not required:
        update_run_log(
            run_id=run_id, status="SKIPPED",
            skip_reason="strategy_has_no_factors",
            universe_size=len(syms_universe),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return _result(bot, decision_date, "SKIPPED",
                       "strategy_has_no_factors", run_id=run_id)

    panel = load_factor_panel(
        factors=required,
        universe=universe_base,
        as_of=decision_date,
    )

    coverage = factor_coverage(required, universe_base, decision_date)

    if panel.empty:
        logger.warning(
            f"[bot {bot.name}] SKIPPED on {decision_date}: no_factor_data. "
            f"factor_signals 에 (date={decision_date}, universe={universe_base}, "
            f"factor in {required}) 로 조회되는 row 없음. coverage={coverage}"
        )
        update_run_log(
            run_id=run_id, status="SKIPPED",
            skip_reason="no_factor_data",
            universe_size=len(syms_universe),
            eligible_size=0, scored_size=0,
            factor_coverage=coverage,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return _result(bot, decision_date, "SKIPPED", "no_factor_data",
                       run_id=run_id)

    logger.info(
        f"[bot {bot.name}] {decision_date}: factor_panel rows={len(panel)} "
        f"coverage={coverage}"
    )

    # Restrict panel to symbols in today's universe
    panel = panel[panel["symbol"].isin(syms_universe)].reset_index(drop=True)

    # --- 3. Apply universe filters from declarative spec ---
    spec_obj: StrategySpec | None = None
    if isinstance(strategy, DeclarativeStrategy):
        spec_obj = strategy.spec

    eligible_panel = _apply_universe_filters(panel, spec_obj)
    eligible_syms = set(eligible_panel["symbol"]) if not eligible_panel.empty else set()

    if not eligible_syms:
        uf_summary = (
            f"min_market_cap_krw={spec_obj.universe.min_market_cap_krw}, "
            f"min_avg_value_krw_20d={spec_obj.universe.min_avg_value_krw_20d}, "
            f"exclude_sectors={spec_obj.universe.exclude_sectors}"
            if spec_obj else "no spec"
        )
        logger.warning(
            f"[bot {bot.name}] SKIPPED on {decision_date}: no_eligible_after_filters. "
            f"panel_rows={len(panel)}, filter=({uf_summary}). "
            f"필터가 너무 빡세서 종목 0개가 남음."
        )
        update_run_log(
            run_id=run_id, status="SKIPPED",
            skip_reason="no_eligible_after_filters",
            universe_size=len(syms_universe),
            eligible_size=0, scored_size=0,
            factor_coverage=coverage,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return _result(bot, decision_date, "SKIPPED",
                       "no_eligible_after_filters", run_id=run_id)

    logger.info(
        f"[bot {bot.name}] {decision_date}: eligible={len(eligible_syms)}"
    )

    # --- 4. Score ---
    scores = strategy.score(panel, decision_date)
    scored = scores.dropna()
    scored = scored[scored.index.isin(eligible_syms)]

    # Coverage gate
    if spec_obj is not None:
        min_cov = spec_obj.rebalance.skip_if_universe_coverage_below
    else:
        min_cov = 0.5
    cov_ratio = len(scored) / max(1, len(eligible_syms))
    if eligible_syms and cov_ratio < min_cov:
        logger.warning(
            f"[bot {bot.name}] SKIPPED on {decision_date}: low_signal_coverage. "
            f"scored={len(scored)} / eligible={len(eligible_syms)} = {cov_ratio:.2%} "
            f"< 임계값 {min_cov:.2%}. factor_coverage={coverage}"
        )
        update_run_log(
            run_id=run_id, status="SKIPPED",
            skip_reason="low_signal_coverage",
            universe_size=len(syms_universe),
            eligible_size=len(eligible_syms),
            scored_size=len(scored),
            factor_coverage=coverage,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return _result(bot, decision_date, "SKIPPED",
                       "low_signal_coverage", run_id=run_id)

    logger.info(
        f"[bot {bot.name}] {decision_date}: scored={len(scored)} "
        f"({cov_ratio:.1%} of eligible)"
    )

    # --- 5. Build sector_map ---
    if "sector" in panel.columns:
        sector_map = dict(zip(panel["symbol"], panel["sector"]))
    else:
        sector_map = {}

    # --- 6. Select targets ---
    target_weights = strategy.select(
        scored,
        eligible=pd.Index(eligible_syms),
        sector_map=sector_map,
    )
    target_size = len(target_weights)

    # --- 7. Current portfolio & order construction ---
    current_positions = get_open_positions(bot.bot_id)
    syms_for_mark = set(current_positions.keys()) | set(target_weights.keys())
    close_map = load_close_with_fallback(
        list(syms_for_mark), decision_date, lookback_days=7,
    )

    cash_start = float(bot.cash)

    if spec_obj is not None:
        exec_spec = spec_obj.execution
    else:
        exec_spec = ExecutionSpec()

    broker = PaperBroker(exec_spec)
    fills = _build_orders(
        current_positions=current_positions,
        target_weights=target_weights,
        bot_total_value=float(bot.total_value),
        bot_cash=cash_start,
        close_map=close_map,
        broker=broker,
        decision_date=decision_date,
        min_holding_days=strategy.min_holding_days(),
        turnover_cap=(
            spec_obj.rebalance.turnover_cap_per_day if spec_obj else 1.0
        ),
        composite_scores=scored.to_dict(),
    )

    # --- 8. Persist orders + cash ---
    n_buys = sum(1 for f in fills if f.side == "BUY")
    n_sells = sum(1 for f in fills if f.side == "SELL")

    insert_orders(
        bot_id=bot.bot_id,
        run_id=run_id,
        spec_history_id=spec_history.spec_history_id,
        decision_date=decision_date,
        fills=fills,
        composite_scores=scored.to_dict(),
        cash_walk_start=cash_start,
    )

    if fills:
        apply_orders_to_bot(bot_id=bot.bot_id, fills=fills)

    # --- 9. Compute final positions ---
    final_positions = _apply_fills_to_positions(current_positions, fills)
    bot_after_cash = float(get_bot(bot.bot_id).cash)
    holdings_val_float = _holdings_value(final_positions, close_map)
    total_val_float = bot_after_cash + holdings_val_float

    pos_snapshot_rows = _build_position_snapshot(
        final_positions, close_map, sector_map,
        composite_scores=scored.to_dict(),
        total_value=total_val_float,
    )
    snapshot_positions(
        bot_id=bot.bot_id,
        decision_date=decision_date,
        positions=pos_snapshot_rows,
    )

    # --- 10. Daily PnL ---
    holdings_value = Decimal(str(round(holdings_val_float, 2)))
    cash_final = Decimal(str(round(bot_after_cash, 2)))
    total_value = cash_final + holdings_value

    buy_value = Decimal(
        str(round(sum(f.fill_value for f in fills if f.side == "BUY"), 2))
    )
    sell_value = Decimal(
        str(round(sum(f.fill_value for f in fills if f.side == "SELL"), 2))
    )
    fee_total = Decimal(
        str(round(sum(f.fee for f in fills), 2))
    )

    record_daily_pnl(
        bot_id=bot.bot_id,
        decision_date=decision_date,
        cash=cash_final, holdings_value=holdings_value, total_value=total_value,
        seed_cash=bot.seed_cash,
        n_positions=len(final_positions),
        trades_count=len(fills),
        buy_value=buy_value, sell_value=sell_value, fee_total=fee_total,
    )

    update_bot_state_after_tick(
        bot_id=bot.bot_id,
        decision_date=decision_date,
        last_tick_run_id=run_id,
        holdings_value=holdings_value,
        total_value=total_value,
    )

    # --- 11. Finalize run ---
    duration_ms = int((time.monotonic() - started_at) * 1000)
    update_run_log(
        run_id=run_id, status="SUCCESS",
        universe_size=len(syms_universe),
        eligible_size=len(eligible_syms),
        scored_size=len(scored),
        factor_coverage=coverage,
        target_size=target_size,
        n_buys=n_buys, n_sells=n_sells,
        duration_ms=duration_ms,
    )

    logger.success(
        f"[bot {bot.name}] {decision_date}: "
        f"BUY={n_buys} SELL={n_sells} "
        f"cash={cash_final} holdings={holdings_value} total={total_value} "
        f"({duration_ms}ms)"
    )

    return {
        "bot_id": bot.bot_id,
        "decision_date": decision_date,
        "status": "SUCCESS",
        "run_id": run_id,
        "n_buys": n_buys, "n_sells": n_sells,
        "total_value": float(total_value),
    }


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------


def _apply_universe_filters(
    panel: pd.DataFrame,
    spec: StrategySpec | None,
) -> pd.DataFrame:
    """Apply min_market_cap / min_avg_value / exclude lists from the spec."""
    if spec is None or panel.empty:
        return panel
    uf = spec.universe
    out = panel.copy()
    if uf.min_market_cap_krw is not None and "market_cap" in out.columns:
        out = out[out["market_cap"].fillna(0) >= uf.min_market_cap_krw]
    if uf.min_avg_value_krw_20d is not None and "avg_value_20d" in out.columns:
        out = out[out["avg_value_20d"].fillna(0) >= uf.min_avg_value_krw_20d]
    if uf.exclude_sectors:
        out = out[~out["sector"].isin(uf.exclude_sectors)]
    if uf.exclude_symbols:
        out = out[~out["symbol"].isin(uf.exclude_symbols)]
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------


def _build_orders(
    *,
    current_positions: dict[str, dict[str, Any]],
    target_weights: dict[str, float],
    bot_total_value: float,
    bot_cash: float,
    close_map: dict[str, float],
    broker: PaperBroker,
    decision_date: date,
    min_holding_days: int,
    turnover_cap: float,
    composite_scores: dict[str, float],
) -> list[FillPlan]:
    """Diff current vs target, produce SELL list + BUY list.

    Simplification for Phase 1:
        - SELL: any held symbol not in target → full liquidation
        - SELL: held symbol in target but min_holding_days not met → keep
        - BUY: any target symbol not currently held → buy target_value
        - Held + target: keep current quantity (no rebalance trim).
    """
    fills: list[FillPlan] = []
    sell_value_total = 0.0
    buy_value_total = 0.0
    cash_budget = bot_cash
    # Turnover_cap is per-side; we sum both sides and compare against cap × 2
    cap_value = turnover_cap * bot_total_value * 2

    # --- SELL pass ---
    targets_set = set(target_weights.keys())
    for sym, info in current_positions.items():
        if sym in targets_set:
            continue
        if min_holding_days > 0:
            held_days = (decision_date - info["last_date"]).days
            if held_days < min_holding_days:
                continue
        qty = info["quantity"]
        if qty <= 0:
            continue
        plan = broker.plan_sell(sym, qty, decision_date)
        if plan is None:
            logger.warning(f"  [order] SELL {sym}: no fill price — skipping")
            continue
        if sell_value_total + buy_value_total + plan.fill_value > cap_value + 1e-6:
            logger.info(
                f"  [order] SELL {sym} skipped — turnover cap"
            )
            continue
        fills.append(plan)
        sell_value_total += plan.fill_value
        cash_budget += plan.fill_value - plan.fee

    # --- BUY pass (descending score order) ---
    sorted_targets = sorted(
        target_weights.items(),
        key=lambda kv: composite_scores.get(kv[0], 0.0),
        reverse=True,
    )
    for sym, weight in sorted_targets:
        if sym in current_positions:
            continue
        target_value = weight * bot_total_value
        if target_value <= 0:
            continue
        if sell_value_total + buy_value_total + target_value > cap_value + 1e-6:
            logger.info(
                f"  [order] BUY {sym} skipped — turnover cap"
            )
            continue
        plan = broker.plan_buy(
            sym, target_value, decision_date, max_cash=cash_budget,
        )
        if plan is None:
            continue
        fills.append(plan)
        buy_value_total += plan.fill_value
        cash_budget -= plan.fill_value + plan.fee

    return fills


def _apply_fills_to_positions(
    current_positions: dict[str, dict[str, Any]],
    fills: list[FillPlan],
) -> dict[str, dict[str, Any]]:
    """Compute the post-fill positions dict."""
    pos = {
        sym: dict(info) for sym, info in current_positions.items()
    }
    for f in fills:
        if f.side == "SELL":
            pos.pop(f.symbol, None)
        else:  # BUY
            if f.symbol in pos:
                old = pos[f.symbol]
                new_q = old["quantity"] + f.quantity
                new_cost = (
                    old["quantity"] * old["avg_cost"]
                    + f.quantity * f.fill_price
                ) / new_q
                pos[f.symbol] = {
                    "quantity": new_q,
                    "avg_cost": new_cost,
                    "last_date": f.fill_date,
                }
            else:
                pos[f.symbol] = {
                    "quantity": f.quantity,
                    "avg_cost": f.fill_price,
                    "last_date": f.fill_date,
                }
    return pos


def _holdings_value(
    positions: dict[str, dict[str, Any]],
    close_map: dict[str, float],
) -> float:
    """Σ(quantity × close) over positions. Missing close → use avg_cost."""
    total = 0.0
    for sym, info in positions.items():
        px = close_map.get(sym, info["avg_cost"])
        total += info["quantity"] * px
    return total


def _build_position_snapshot(
    positions: dict[str, dict[str, Any]],
    close_map: dict[str, float],
    sector_map: dict[str, str],
    composite_scores: dict[str, float],
    total_value: float,
) -> list[dict[str, Any]]:
    """Build snapshot_positions input rows."""
    rows: list[dict[str, Any]] = []
    for sym, info in positions.items():
        mp = close_map.get(sym)
        mv = info["quantity"] * mp if mp is not None else None
        upnl = (
            info["quantity"] * (mp - info["avg_cost"])
            if mp is not None else None
        )
        wp = (mv / total_value) if (mv is not None and total_value > 0) else None
        rows.append({
            "symbol": sym,
            "quantity": info["quantity"],
            "avg_cost": info["avg_cost"],
            "market_price": mp,
            "market_value": mv,
            "unrealized_pnl": upnl,
            "weight_pct": wp,
            "sector": sector_map.get(sym),
            "composite_score": composite_scores.get(sym),
        })
    return rows


def _result(
    bot, decision_date: date, status: str, reason: str,
    *, run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "bot_id": bot.bot_id,
        "decision_date": decision_date,
        "status": status,
        "reason": reason,
        "run_id": run_id,
    }
