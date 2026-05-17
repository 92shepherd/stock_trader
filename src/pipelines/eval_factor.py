"""CLI entry point for evaluating a single factor.

Usage examples:
    # Default: evaluate momentum_20d on ALL universe, last 1 year, horizon=5
    python -m src.pipelines.eval_factor

    # Pick a baseline factor by name
    python -m src.pipelines.eval_factor --factor reversal_5d

    # Specific universe + horizon
    python -m src.pipelines.eval_factor --factor momentum_60d --universe KOSPI --horizon 20

    # Custom date range
    python -m src.pipelines.eval_factor --factor momentum_20d \\
        --start 2024-01-01 --end 2025-12-31 --horizon 5

    # Evaluate one factor across all default horizons (1/5/10/20)
    python -m src.pipelines.eval_factor --factor momentum_20d --all-horizons

    # Run all baseline factors at horizon=5 on ALL (quick benchmark sweep)
    python -m src.pipelines.eval_factor --all-baselines

List available baseline factor names:
    python -m src.pipelines.eval_factor --list-factors
"""
from __future__ import annotations

# Load .env BEFORE importing modules that read os.environ at import time.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import argparse
from datetime import date, timedelta

from src.research.factor_eval import (
    ALL_FACTORS,
    evaluate_factor,
    evaluate_factor_across_horizons,
)
from src.research.returns import DEFAULT_HORIZONS
from src.research.universe import SUPPORTED_UNIVERSES
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _print_metrics_table(label: str, metrics: dict) -> None:
    """Pretty-print the standard metric set."""
    logger.info(f"--- {label} ---")
    keys = [
        "ic_mean", "ic_std", "icir", "ic_t_stat",
        "rank_ic_mean", "rank_ic_std", "rank_icir",
        "hit_rate",
        "long_short_return", "long_short_sharpe",
        "max_drawdown", "turnover_avg",
        "n_days",
    ]
    for k in keys:
        v = metrics.get(k)
        if v is None:
            logger.info(f"  {k:<22s}: n/a")
        else:
            try:
                logger.info(f"  {k:<22s}: {v:>10.4f}")
            except (TypeError, ValueError):
                logger.info(f"  {k:<22s}: {v}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1: evaluate a single factor (cross-sectional IC + L/S portfolio)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--factor", default="momentum_20d",
        help="Factor name (baseline registry key). Use --list-factors to see options. "
             "Default: momentum_20d.",
    )
    parser.add_argument(
        "--universe", default="ALL",
        choices=list(SUPPORTED_UNIVERSES),
        help="Evaluation universe (KOSPI / KOSDAQ / ALL / KOSPI200). Default: ALL.",
    )
    parser.add_argument(
        "--start", type=_parse_date,
        help="Evaluation start date (YYYY-MM-DD). Default: end - 365d.",
    )
    parser.add_argument(
        "--end", type=_parse_date,
        help="Evaluation end date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--horizon", type=int, default=5,
        help="Forward return horizon in trading days (1/5/10/20). Default: 5.",
    )
    parser.add_argument(
        "--all-horizons", action="store_true",
        help="Evaluate at every horizon in DEFAULT_HORIZONS (1/5/10/20).",
    )
    parser.add_argument(
        "--all-baselines", action="store_true",
        help="Run every factor in BASELINE_FACTORS at --horizon. Useful for "
             "a quick benchmark sweep before any ML model.",
    )
    parser.add_argument(
        "--sector-neutralize", action="store_true",
        help="Apply per-date sector demean before ranking.",
    )
    parser.add_argument(
        "--n-quantiles", type=int, default=5,
        help="Quantile count for long-short portfolio (default 5 = top/bot 20%%).",
    )
    parser.add_argument(
        "--min-obs-per-day", type=int, default=20,
        help="Minimum cross-section size to compute IC on a given day. Default 20.",
    )
    parser.add_argument(
        "--no-persist-signals", action="store_true",
        help="Do not upsert factor_signals (useful for ad-hoc dry runs).",
    )
    parser.add_argument(
        "--no-persist-run", action="store_true",
        help="Do not write eval_runs/eval_metrics (compute & log only).",
    )
    parser.add_argument(
        "--no-fwd-returns", action="store_true",
        help="Skip the forward_returns recompute step (assume cache is fresh).",
    )
    parser.add_argument(
        "--notes", default=None,
        help="Free-text note to attach to eval_runs row.",
    )
    parser.add_argument(
        "--list-factors", action="store_true",
        help="Print available baseline factor names and exit.",
    )
    args = parser.parse_args()

    if args.list_factors:
        logger.info("Available factors:")
        for name, spec in ALL_FACTORS.items():
            logger.info(f"  {name:<24s}  kwargs={spec['kwargs']}")
        return

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=365))
    if start > end:
        parser.error(f"--start {start} is after --end {end}")

    common = dict(
        universe=args.universe,
        start=start, end=end,
        do_sector_neutralize=args.sector_neutralize,
        n_quantiles=args.n_quantiles,
        min_obs_per_day=args.min_obs_per_day,
        persist_signals=not args.no_persist_signals,
        persist_run=not args.no_persist_run,
        notes=args.notes,
    )

    if args.all_baselines:
        logger.info(
            f"=== Sweep: {len(ALL_FACTORS)} factors @ horizon={args.horizon} ==="
        )
        # Compute fwd_returns once at the start, then reuse for the rest.
        ensure_first = not args.no_fwd_returns
        for i, factor_name in enumerate(ALL_FACTORS.keys()):
            logger.info(f"\n>>> [{i+1}/{len(ALL_FACTORS)}] {factor_name} <<<")
            try:
                run_id, metrics, _ = evaluate_factor(
                    factor_name=factor_name,
                    horizon_days=args.horizon,
                    ensure_forward_returns=ensure_first,
                    **common,
                )
                _print_metrics_table(f"{factor_name} (h={args.horizon}) -> {run_id}", metrics)
            except Exception as e:
                logger.error(f"  {factor_name}: SKIP ({e})")
            ensure_first = False  # reuse cache for subsequent factors
        return

    if args.all_horizons:
        logger.info(
            f"=== Multi-horizon: factor={args.factor} horizons={list(DEFAULT_HORIZONS)} ==="
        )
        results = evaluate_factor_across_horizons(
            factor_name=args.factor,
            horizons=DEFAULT_HORIZONS,
            **common,
        )
        for h, (run_id, metrics, _) in results.items():
            _print_metrics_table(f"{args.factor} (h={h}) -> {run_id}", metrics)
        return

    # Default: single factor, single horizon
    logger.info(
        f"=== Single eval: factor={args.factor} universe={args.universe} "
        f"period=[{start}..{end}] horizon={args.horizon} ==="
    )
    run_id, metrics, _ = evaluate_factor(
        factor_name=args.factor,
        horizon_days=args.horizon,
        ensure_forward_returns=not args.no_fwd_returns,
        **common,
    )
    _print_metrics_table(f"{args.factor} (h={args.horizon}) -> {run_id}", metrics)


if __name__ == "__main__":
    main()
