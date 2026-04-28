---
name: collector-scaffolding
description: Use when adding a new data collector to the stock_trader project. Triggers on requests like "수집기 추가", "collector 만들어", "DART 수집기", "네이버 컨센서스 수집", "KIS 분봉 수집", "새 데이터 소스 붙이자" — any time a new external data source needs to be wired into the existing collector → repository → pipeline → migration pattern. Generates 4-5 files in a consistent style matching the established daily_pykrx / daily_fdr conventions.
---

# Collector Scaffolding for stock_trader

This skill captures the project's collector pattern as it stands today (2026-04). Every new data source — DART financials, KIS minute bars, Naver analyst opinions, marcap, etc. — follows the same five-touchpoint shape. This skill makes sure each new collector lands in the right shape on the first try, instead of being reverse-engineered from `daily_fdr.py` every time.

## When to use this skill

Trigger on any request that means "wire a new external data source into this project". Examples:

- "DART 재무제표 수집기 추가해줘"
- "네이버 애널리스트 컨센서스 수집기 만들어"
- "KIS로 분봉 수집기 붙이자"
- "marcap 시가총액 검증용으로 추가"
- "새 데이터 소스 X 붙이는데 기존 패턴 따라서 짜줘"

Do NOT trigger this skill for:
- Modifying an existing collector (that's just an edit; read the file and edit it)
- Bug fixes inside an existing collector
- Pure analysis / backtesting code (no DB write side)
- Schema changes that don't introduce a new data source

## The five touchpoints

Every new collector touches the same five places. Always create/update them in this order:

1. **`migrations/00X_<feature>.sql`** — new table(s) + hypertable conversion if time-series
2. **`src/db/models.py`** — SQLAlchemy ORM class for the new table
3. **`src/db/repositories.py`** — `upsert_<table>` (bulk via COPY) + `get_completed_*` resume helper
4. **`src/collectors/<name>.py`** — fetch + normalize + per-unit collection function + `backfill_<unit>` (with resume) + `backfill_active_universe` (if applicable)
5. **`src/pipelines/collect_<name>.py`** — argparse CLI with `--start`, `--end`, `--days`, `--symbols`, `--skip-tickers`, `--no-skip-done`

If the collector is symbol-iterating (one HTTP call per symbol), follow the **`daily_fdr` pattern**. If it's date-iterating (one HTTP call per date, all symbols), follow the **`daily_pykrx` pattern**. The choice depends entirely on the upstream API shape — see the decision section below.

## Phase 1: Capture intent (always start here)

Before writing any code, gather these facts. Use `ask_user_input_v0` if any are unclear; do NOT guess.

### Required answers

- **Collector name** (snake_case, e.g. `dart_financials`, `naver_consensus`, `kis_minute`). This becomes the `COLLECTOR_NAME` constant, the filename, and the CLI module name.
- **Upstream library / endpoint** — exact package name, exact function/URL.
- **API iteration shape**:
  - **Per-symbol, multi-period**: one call returns one symbol's data for a range. → `daily_fdr` pattern.
  - **Per-period, all-symbols**: one call returns all symbols' data for a single period. → `daily_pykrx` pattern.
  - **Per-symbol, point-in-time**: one call returns one symbol's data for one period (e.g. DART quarterly statement). → modified `daily_fdr` pattern, no period range.
- **Time grain** — daily, minute, quarterly, ad-hoc?
- **Target table** — new table or existing? If new, draft the columns.
- **Resume key** — what tuple identifies "this unit is done"? `(symbol, end_date)` for symbol-iterating, `target_date` for date-iterating, `(symbol, year, quarter)` for DART, etc.
- **Auth** — none / API key from `.env` / OAuth / KRX login? This drives whether the pipeline needs `load_dotenv()` early.
- **Rate limits** — calls/sec from upstream docs. This sets `request_delay` default. If unknown, ask.

### Inferable answers (don't ask)

- Migration number → highest existing in `migrations/` + 1.
- ORM imports → SQLAlchemy 2.0 `Mapped` / `mapped_column` (mirror `models.py`).
- Logger → `from src.utils.logger import logger`.
- Config access → `get_app_config()` for collection params, `get_db_settings()` only if needed.

## Phase 2: Decide the iteration pattern

Read this section before writing code. Picking the wrong pattern is the most expensive mistake.

### Symbol-iterating (`daily_fdr` pattern)

Use when one upstream call returns **one symbol's data over a range of periods**.

Examples: FinanceDataReader, OpenDartReader (per company), KIS minute bars (per symbol).

Key shape:
- `collect_one_symbol(symbol, start_date, end_date) -> int` (rows upserted)
- `backfill_symbols(symbols: list[str], ..., skip_done=True, consecutive_fail_limit=20)`
- `backfill_active_universe(...)` that calls `get_active_tickers()` then delegates to `backfill_symbols`
- Resume helper in repositories: `get_completed_symbols_in_range(collector, start, end) -> set[str]`
- `collection_log` writes one row per symbol with `target_date = end_date`, `symbol = sym`
- Circuit breaker on `consecutive_failures >= consecutive_fail_limit` (default 20 for slow APIs, 10 for fast ones)
- Always `time.sleep(cfg.collection.<grain>.request_delay)` between symbols

### Date-iterating (`daily_pykrx` pattern)

Use when one upstream call returns **all symbols' data for a single period**.

Examples: pykrx `get_market_ohlcv(date, market=...)`, marcap by-date.

Key shape:
- `collect_one_period(target: date) -> int`
- `backfill(start_date, end_date, days, skip_done=True)` that iterates `get_missing_dates(...)`
- No `backfill_active_universe` (not applicable — universe is implicit)
- Resume helper in repositories: `get_missing_dates(collector, start, end)` already exists; reuse it
- `collection_log` writes one row per date with `target_date = date`, `symbol = NULL`
- Circuit breaker on `consecutive_failures >= 10`

### Point-in-time per symbol (variant)

Use for DART-like APIs where one call returns one symbol's snapshot (e.g. one quarter's financial statement).

Treat like symbol-iterating, but iterate `(symbol, period)` pairs. Resume key becomes `(symbol, year, quarter)` — write a custom `get_completed_symbol_periods` helper following the `get_completed_symbols_in_range` shape.

## Phase 3: Generate the files

Once intent is captured and pattern is decided, generate files in this order. **Use the templates in `references/`** (see file list below) — do not improvise. Adapt template values to the captured intent; preserve the structural conventions exactly.

### Order matters

1. Migration first (the schema is the contract everything else binds to).
2. Model next (mirrors the table).
3. Repository upsert + resume helper.
4. Collector module.
5. Pipeline CLI.

After step 1, before continuing to 2, **ask the user to apply the migration** (`python scripts/init_db.py` or whatever the project uses). Don't generate ORM code against a non-existent table.

### Conventions to preserve (do not deviate)

These are non-negotiable patterns that already exist across `daily_fdr.py`, `daily_pykrx.py`, `tickers.py`, `repositories.py`. New collectors must match.

- **`from __future__ import annotations`** at the top of every new module.
- **`COLLECTOR_NAME = "<name>"`** constant at module top — used for `collection_log.collector` filtering.
- **Tenacity retry** on the raw upstream call: `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)` on a `_safe_*` private function.
- **Defensive column check** on upstream response — verify expected columns exist before processing; on mismatch log a warning and return empty DataFrame instead of raising.
- **Bulk upsert via COPY + ON CONFLICT** in repository, mirroring `upsert_daily_prices` exactly. Don't use ORM `bulk_insert_mappings` for time-series tables — too slow.
- **Resume by default** — `skip_done=True` is the default for any backfill. The user has to opt out with `--no-skip-done`.
- **Circuit breaker on consecutive failures** — abort the loop, log a clear "Re-run to resume" message, do not raise.
- **`tqdm` progress bar** with `desc="<descriptive label>"`.
- **Status values** in `collection_log`: `success` / `failed` / `skipped` / `partial`. Reserve `skipped` for "fetched but no data" (holiday / halted / not-yet-listed) — NOT for resume-skipped symbols (those don't get a new log row).
- **CLI flags** mirror `collect_daily_fdr.py` exactly: `--start`, `--end`, `--days`, `--symbols`, `--markets`, `--skip-tickers`, `--no-skip-done`. Add new flags only if genuinely needed.
- **Pipeline `load_dotenv()` early** — top of file, before any module that reads env vars at import time. Mirror `collect_daily.py`'s comment about why.
- **Pipeline structure**: Step 1 (refresh master if relevant) → Step 2 (do the collection). Use `logger.info("=== Step N: ... ===")` headers.

### Conventions that vary (decide per-collector)

- `request_delay` default — set in `config/settings.yaml` under `collection.<grain>`. Match upstream rate limits.
- `consecutive_fail_limit` — 10 for fast/reliable upstreams (pykrx), 20 for slow scrapers (fdr).
- Whether `backfill_active_universe` makes sense — only if the data has a per-symbol shape and you want full coverage.

## Phase 4: Verify

Before declaring done:

1. **Migration applies cleanly** on top of the existing schema. If it adds columns to an existing table, use `ADD COLUMN IF NOT EXISTS`. If it creates a hypertable, use `if_not_exists => TRUE`.
2. **Smoke test** — every collector module's `if __name__ == "__main__":` block runs a tiny end-to-end test (e.g. 2 symbols × 30 days). Add this; users rely on it.
3. **CLI `--help`** is readable and mirrors `python -m src.pipelines.collect_daily_fdr --help`.
4. **Add the collector to `pyproject.toml` dependencies** if it introduces a new package. Check it isn't already there. Use `>=` pin matching style of existing deps.
5. **Remind the user about the Notion doc** — per project convention, "주식 트레이딩 프로젝트" page expects a sub-page entry for major collectors. Don't generate it; just remind.

## Anti-patterns (do NOT do these)

- ❌ Row-by-row INSERT for time-series — always COPY + ON CONFLICT.
- ❌ Generating fundamentals you didn't fetch — leave columns NULL, let other collectors fill via ON CONFLICT DO UPDATE.
- ❌ `requests` library — use `httpx` (already a project dep).
- ❌ Skipping the `_safe_*` retry wrapper — every external call gets one.
- ❌ `print(...)` for diagnostics — always `logger`.
- ❌ Hardcoding magic numbers — push to `config/settings.yaml` under `collection.<grain>`.
- ❌ Catching `Exception` and swallowing — always log + re-raise OR log + write `failed` to `collection_log` and continue.
- ❌ Bumping migration numbers without checking — read `migrations/` first.
- ❌ Adding a new top-level package without updating `pyproject.toml` `[tool.setuptools] packages = [...]`.

## File templates

Templates live in `references/` next to this SKILL.md:

- `references/conventions.md` — full enumeration of the patterns above with code snippets, for cases where the model needs concrete examples to anchor on
- `references/template_migration.sql` — annotated SQL migration template
- `references/template_collector_per_symbol.py` — symbol-iterating collector template (daily_fdr shape)
- `references/template_collector_per_date.py` — date-iterating collector template (daily_pykrx shape)
- `references/template_repository_additions.py` — upsert + resume helper template
- `references/template_pipeline_cli.py` — argparse CLI template

Read these as references. Do not paste them verbatim — adapt names, columns, table names, and any pattern-specific details to the captured intent. Preserve structural conventions (imports, retry decorator, log_collection calls, CLI flag set) exactly.

## Communication with the user

- Always show the file plan **before** writing files. List the 5 files with their target paths.
- After capturing intent, restate it back as a one-paragraph summary and ask "맞아?" before generating. The cost of a wrong assumption here is rewriting all 5 files.
- When generating, write the migration first, then **stop and ask the user to apply it** before continuing. The rest of the code binds to the schema.
- After everything is generated, give the user the exact commands to run a smoke test and a real backfill.

## Style notes

- Korean comments are fine in SQL migrations (matches existing `001_init_schema.sql` style).
- English docstrings + Korean inline comments where helpful is the established Python style — mirror it.
- Keep docstrings substantive: explain the *strategy*, not just the args. Look at `daily_pykrx.py`'s module docstring for the bar.
