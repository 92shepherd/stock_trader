"""Parse-only smoke test for the new JSON-based FnGuide consensus path.

Avoids importing src.collectors.consensus_fnguide directly (that pulls in
psycopg via repositories), and instead inlines the new fetch+parse logic.
Mirror of consensus_fnguide.py at the time this script was written —
keep in sync if you change the production parser.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = sys.argv[1:] or ["005930", "000660"]

# --- mirror of production constants -----------------------------------------
_URL = "https://comp.fnguide.com/SVO2/json/data/01_06/01_A{symbol}_{aq}_D.json"
_HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://comp.fnguide.com/SVO2/asp/SVD_Consensus.asp",
}
_OK = 100_000_000
_PERIOD_RE = re.compile(r"(\d{2,4})[./\-](\d{1,2})")
_KEYS = ("D_2", "D_3", "D_4", "D_5", "D_6", "D_7")
_ROW_PATTERNS = [
    ("매출액", "revenue_estimate", True),
    ("매출", "revenue_estimate", True),
    ("영업이익", "op_income_estimate", True),
    ("당기순이익", "net_income_estimate", True),
    ("순이익", "net_income_estimate", True),
    ("EPS", "eps_estimate", False),
    ("주당순이익", "eps_estimate", False),
    ("목표주가", "target_price", False),
]


def _to_period(label: str):
    s = re.sub(r"\([A-Za-z]\)", "", label.strip()).strip()
    if not s:
        return None
    m = _PERIOD_RE.search(s)
    if m:
        y = int(m.group(1)) if len(m.group(1)) == 4 else 2000 + int(m.group(1))
        mo = int(m.group(2))
        if mo == 12:
            return f"FY{y}", "annual"
        if mo in (3, 6, 9):
            return f"{y}Q{mo // 3}", "quarterly"
        return f"{y}M{mo:02d}", "quarterly"
    if re.fullmatch(r"\d{4}", s):
        return f"FY{s}", "annual"
    return None


def _amount(text, *, oku=False):
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in {"-", "N/A", "n/a", "NA", "—"}:
        return None
    s = s.replace(",", "").replace(" ", "")
    if s.startswith(("△", "▲")):
        s = "-" + s[1:]
    try:
        v = float(s)
    except ValueError:
        return None
    return v * _OK if oku else v


def _match(label):
    for sub, col, oku in _ROW_PATTERNS:
        if sub in label:
            return col, oku
    return None


def fetch(symbol, aq):
    r = httpx.get(_URL.format(symbol=symbol, aq=aq), headers=_HDRS, timeout=15)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    if len(r.content) < 1500:
        try:
            o = json.loads(text)
        except json.JSONDecodeError:
            return None
        if len(o.get("comp", [])) <= 1:
            return None
        return o
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def consume(obj, sym, today, bucket):
    if not obj:
        return
    comp = obj.get("comp") or []
    if len(comp) < 2:
        return
    header = comp[0]
    periods = [_to_period(str(header.get(k, ""))) for k in _KEYS]
    for row in comp[1:]:
        label = str(row.get("ACCOUNT_NM", ""))
        m = _match(label)
        if not m:
            continue
        col, oku = m
        for i, k in enumerate(_KEYS):
            p = periods[i]
            if p is None:
                continue
            fp, fpt = p
            v = _amount(row.get(k), oku=oku)
            if v is None:
                continue
            entry = bucket.setdefault(fp, {
                "symbol": sym, "as_of_date": today,
                "fiscal_period": fp, "fiscal_period_type": fpt,
                "source": "fnguide",
            })
            if col not in entry:
                entry[col] = v


today = date.today()
for sym in SYMBOLS:
    print(f"\n{'='*72}\n {sym}\n{'='*72}")
    obj_a = fetch(sym, "A")
    obj_q = fetch(sym, "Q")
    print(f" annual JSON: {'OK' if obj_a else 'EMPTY'}   "
          f"quarterly JSON: {'OK' if obj_q else 'EMPTY'}")
    bucket: dict[str, dict[str, Any]] = {}
    consume(obj_a, sym, today, bucket)
    consume(obj_q, sym, today, bucket)
    print(f" parsed rows: {len(bucket)}")
    for fp, row in sorted(bucket.items()):
        print(f"   {fp:8s} type={row.get('fiscal_period_type'):<10s} "
              f"revenue={row.get('revenue_estimate')}  "
              f"op={row.get('op_income_estimate')}  "
              f"net={row.get('net_income_estimate')}  "
              f"eps={row.get('eps_estimate')}")
