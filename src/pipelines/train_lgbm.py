"""CLI entry point for walk-forward LightGBM training (Phase 2).

Usage examples:
    # Default: KOSPI, last 5 years, horizon=5, default features
    python -m src.pipelines.train_lgbm

    # ALL universe, specific period
    python -m src.pipelines.train_lgbm --universe ALL \\
        --start 2021-01-01 --end 2025-12-31 --horizon 5

    # Custom features (override default Phase 2 set)
    python -m src.pipelines.train_lgbm --features momentum_20d rev_eps_1m_fy1 quality_roe

    # Different horizons to compare
    python -m src.pipelines.train_lgbm --horizon 10
    python -m src.pipelines.train_lgbm --horizon 20

    # Dry-run (no DB writes; just log metrics)
    python -m src.pipelines.train_lgbm --no-persist

    # First-time training: compute missing factors on the fly
    python -m src.pipelines.train_lgbm --compute-missing
"""
from __future__ import annotations

# Load .env BEFORE importing modules that read os.environ at import time.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import argparse
from datetime import date, timedelta

from src.research.dataset import DEFAULT_PHASE2_FEATURES
from src.research.trainer import train_lgbm_walk_forward
from src.research.universe import SUPPORTED_UNIVERSES
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 2: walk-forward LightGBM regression on multi-factor panel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--universe", default="ALL", choices=list(SUPPORTED_UNIVERSES))
    parser.add_argument("--start", type=_parse_date,
                        help="Overall start date. Default: end - 5y.")
    parser.add_argument("--end", type=_parse_date,
                        help="Overall end date. Default: today.")
    parser.add_argument("--horizon", type=int, default=5,
                        help="Forward return horizon in trading days. Default 5.")
    parser.add_argument("--features", nargs="+", default=None,
                        help=f"Feature factor names. Default: {DEFAULT_PHASE2_FEATURES}")
    parser.add_argument("--train-days", type=int, default=365 * 3,
                        help="Walk-forward train window length (calendar days). Default 1095.")
    parser.add_argument("--test-days", type=int, default=180,
                        help="Walk-forward test window length. Default 180.")
    parser.add_argument("--step-days", type=int, default=None,
                        help="Walk-forward step. Default = test-days (non-overlapping).")
    parser.add_argument("--feature-repr", default="rank_value",
                        choices=["rank_value", "z_score", "raw_value"],
                        help="Which factor_signals column to use as feature.")
    parser.add_argument("--no-target-normalize", action="store_true",
                        help="Disable per-date z-score of y during training.")
    parser.add_argument("--n-quantiles", type=int, default=5,
                        help="Long-short portfolio quantile count.")
    parser.add_argument("--n-estimators", type=int, default=500)
    parser.add_argument("--early-stopping", type=int, default=30)
    parser.add_argument("--compute-missing", action="store_true",
                        help="If a feature isn't in factor_signals cache, compute it.")
    parser.add_argument("--no-persist", action="store_true",
                        help="Do not write model_runs / predictions / eval rows.")
    parser.add_argument("--notes", default=None)
    args = parser.parse_args()

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=365 * 5))
    if start > end:
        parser.error(f"--start {start} after --end {end}")

    result = train_lgbm_walk_forward(
        universe=args.universe,
        start=start, end=end,
        horizon_days=args.horizon,
        feature_names=args.features,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        feature_repr=args.feature_repr,
        target_normalize=not args.no_target_normalize,
        n_quantiles=args.n_quantiles,
        n_estimators=args.n_estimators,
        early_stopping_rounds=args.early_stopping,
        persist=not args.no_persist,
        compute_missing_features=args.compute_missing,
        notes=args.notes,
    )

    logger.info("\n=== Per-fold summary ===")
    for f in result["folds"]:
        m = f["metrics"]
        logger.info(
            f"  Fold {f['fold']:>2}: "
            f"test=[{f['test_start']}..{f['test_end']}]  "
            f"ic={m.get('ic_mean'):.4f}  "
            f"rank_ic={m.get('rank_ic_mean'):.4f}  "
            f"icir={m.get('icir'):.4f}  "
            f"sharpe={m.get('long_short_sharpe'):.4f}"
        )


if __name__ == "__main__":
    main()
