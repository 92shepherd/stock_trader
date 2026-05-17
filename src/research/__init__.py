"""Cross-sectional alpha research package (Phases 1 & 2).

Phase 1 - Evaluation infrastructure:
    universe          - Survivorship-aware universe construction.
    returns           - Forward return computation (cached in DB).
    ranking           - Cross-sectional rank / z-score / winsorize /
                        sector neutralize.
    metrics           - IC, RankIC, ICIR, hit rate, Sharpe, MaxDD, turnover.
    walk_forward      - Rolling / expanding split generators.
    factor_eval       - End-to-end single-factor evaluation driver.
    factors_baseline  - momentum / reversal / volatility baseline factors.

Phase 2 - Multi-factor alpha + ML model:
    factors_revision  - Earnings revision factors (consensus_estimates).
                        Strongest single-factor alpha in literature.
    factors_pead      - Post-earnings announcement drift (dart_financials
                        joined with dart_disclosures for point-in-time).
    factors_rating    - Analyst recommendation changes & target-price
                        upside (hankyung_reports).
    factors_quality   - ROE / ROA / op-margin / inverted debt ratio
                        (dart_indicators, PIT-joined).
    dataset           - Build the (date, symbol, features, y) panel for ML.
    model_lgbm        - LightGBM regressor wrapper with target normalization.
    trainer           - Walk-forward training + persistence to model_runs /
                        model_predictions / eval_metrics.

Design principle:
    Every function operates on plain pandas DataFrames so it can be
    used in notebooks for ad-hoc analysis. Persistence sits behind
    explicit repository calls - research.* modules never auto-commit
    unless asked.

Point-in-time discipline:
    All Phase 2 factors that depend on financial filings (PEAD,
    quality) join with dart_disclosures to filter by rcept_dt (actual
    publication date). Consensus and rating factors use the data's
    own timestamp (as_of_date / report_date) which is already PIT-safe.

Unified registry:
    factor_eval.ALL_FACTORS merges baseline + revision + PEAD + rating
    + quality factor specs. CLI tools refer to factors by name from
    this registry. To add a new factor, define a function that returns
    [symbol, date, raw_value] and append a {fn, kwargs} entry to one
    of the per-family dicts.
"""
from __future__ import annotations
