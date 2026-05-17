import sys
sys.path.insert(0, '/app')

from datetime import date
from src.research.factor_eval import evaluate_factor

START = date(2025, 9, 1)
END   = date(2026, 4, 25)
HORIZON = 5
UNIVERSE = "ALL"

# persist_signals=True causes DiskFull on large factors (400K+ rows).
# Only persist for small factors; large ones use in-memory only.
FACTORS = [
    # (factor_name, persist_signals)
    ("momentum_20d",         False),
    ("momentum_60d",         False),
    ("reversal_5d",          False),
    ("volatility_20d",       False),
    ("rating_upgrade_30d",   False),
    ("rating_target_upside", False),
    ("quality_roe",          False),
    ("quality_op_margin",    False),
    ("pead_revenue_yoy",     False),
    ("pead_op_yoy",          False),
]

results = []
for fname, do_persist in FACTORS:
    print(f"\n=== {fname} ===", flush=True)
    try:
        run_id, metrics, _ = evaluate_factor(
            fname, UNIVERSE, START, END,
            horizon_days=HORIZON,
            persist_signals=do_persist,
            persist_run=True,
            ensure_forward_returns=False,  # already backfilled
        )
        results.append((fname, metrics, None))
        ic   = metrics.get('ic_mean') or 0
        icir = metrics.get('icir') or 0
        ls   = metrics.get('long_short_sharpe') or 0
        ret  = (metrics.get('long_short_return') or 0) * 100
        hit  = (metrics.get('hit_rate') or 0) * 100
        print(f"  IC={ic:.4f}  ICIR={icir:.4f}  LS_SR={ls:.4f}  LS_Ret={ret:.2f}%  Hit={hit:.1f}%", flush=True)
    except Exception as e:
        results.append((fname, None, str(e)))
        print(f"  ERROR: {e}", flush=True)
        import traceback; traceback.print_exc()

print("\n\n" + "="*75)
print(f"{'Factor':<25} {'IC':>8} {'RankIC':>8} {'ICIR':>8} {'LS_SR':>8} {'LS_Ret%':>8} {'Hit%':>6}")
print("-"*75)
for fname, m, err in results:
    if err:
        print(f"{fname:<25}  ERROR: {err[:45]}")
    else:
        ic  = float(m.get('ic_mean') or 0)
        ric = float(m.get('rank_ic_mean') or 0)
        icir= float(m.get('icir') or 0)
        ls  = float(m.get('long_short_sharpe') or 0)
        ret = float(m.get('long_short_return') or 0)*100
        hit = float(m.get('hit_rate') or 0)*100
        print(f"{fname:<25} {ic:>8.4f} {ric:>8.4f} {icir:>8.4f} {ls:>8.4f} {ret:>8.2f} {hit:>6.1f}")
