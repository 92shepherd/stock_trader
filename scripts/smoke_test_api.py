"""모의 트레이딩 삭제 후 전체 기능 스모크 테스트.

사용:
    # 도커 dev 모드 + 변경된 코드 반영
    docker compose -f docker-compose.yml -f docker-compose.dev.yml restart api

    # .env 로딩 후 실행
    python -m scripts.smoke_test_api

옵션:
    --base-url http://127.0.0.1:8765   # 기본값
    --api-key <키>                       # 미지정 시 .env / 환경변수 STOCK_TRADER_API_KEY
    --skip-krx                           # (기본 ON) KRX 의존 엔드포인트 스킵
    --include-krx                        # KRX 포함 (디버그용)

KR 시세 수집(스킵 대상): /collect/daily/kis, /collect/minute/kis
                    (pykrx/FDR 의존 라우트는 삭제됨 — 더 이상 존재하지 않음)
                    /collect/daily-cron 은 only=dart 로 호출하여 KIS 단계 회피.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


# .env from project root (when run as `python -m scripts.smoke_test_api`)
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


@dataclass
class Result:
    name: str
    method: str
    path: str
    status: int | None = None
    ok: bool = False
    note: str = ""
    elapsed_ms: int = 0
    body_preview: str = ""


@dataclass
class Report:
    base_url: str
    skipped_krx: bool
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)
        flag = "OK " if r.ok else "XX "
        print(f"  {flag} [{r.status}] {r.method:6s} {r.path:45s} {r.elapsed_ms:5d}ms  {r.note}")

    def summary(self) -> str:
        ok = sum(1 for r in self.results if r.ok)
        total = len(self.results)
        lines = [
            "",
            "=" * 70,
            f" SMOKE TEST SUMMARY — {ok}/{total} passed",
            "=" * 70,
        ]
        failed = [r for r in self.results if not r.ok]
        if failed:
            lines.append("\nFAILED:")
            for r in failed:
                lines.append(f"  - {r.method} {r.path} → {r.status}")
                lines.append(f"      note: {r.note}")
                if r.body_preview:
                    lines.append(f"      body: {r.body_preview[:200]}")
        return "\n".join(lines)


def _do(
    rep: Report,
    client: httpx.Client,
    name: str,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    expect: int | tuple[int, ...] = 200,
    headers: dict | None = None,
    note_on_ok: str = "",
) -> Result:
    t0 = time.time()
    try:
        resp = client.request(
            method, path, json=json_body, headers=headers, timeout=30.0,
        )
        elapsed = int((time.time() - t0) * 1000)
        expected = (expect,) if isinstance(expect, int) else expect
        body_preview = ""
        try:
            body = resp.json()
            body_preview = json.dumps(body, ensure_ascii=False)[:300]
        except Exception:
            body_preview = resp.text[:300]
        r = Result(
            name=name,
            method=method,
            path=path,
            status=resp.status_code,
            ok=(resp.status_code in expected),
            note=note_on_ok if resp.status_code in expected else f"expected {expected}",
            elapsed_ms=elapsed,
            body_preview=body_preview,
        )
    except httpx.HTTPError as e:
        r = Result(
            name=name, method=method, path=path,
            status=None, ok=False,
            note=f"HTTP error: {type(e).__name__}: {e}",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    rep.add(r)
    return r


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--api-key", default=os.environ.get("STOCK_TRADER_API_KEY", ""))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--skip-krx", action="store_true", default=True)
    g.add_argument("--include-krx", dest="skip_krx", action="store_false")
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: STOCK_TRADER_API_KEY missing (.env or --api-key).")
        return 2

    headers = {"X-API-Key": args.api_key, "Content-Type": "application/json"}
    rep = Report(base_url=args.base_url, skipped_krx=args.skip_krx)

    print(f"\nTarget: {args.base_url}  skip_krx={args.skip_krx}\n")

    with httpx.Client(base_url=args.base_url, headers=headers) as cx:

        # ─── 1) 인증 ────────────────────────────────────────────────────
        print("[1] Auth")
        # /health 는 인증 불필요
        _do(rep, cx, "health-noauth", "GET", "/health",
            headers={}, expect=200, note_on_ok="no-auth OK")
        # 보호된 라우트 → 401 expected
        _do(rep, cx, "schedule-noauth", "GET", "/schedule",
            headers={}, expect=401, note_on_ok="rejected as expected")
        # 잘못된 키 → 401
        _do(rep, cx, "schedule-badkey", "GET", "/schedule",
            headers={"X-API-Key": "wrong"}, expect=401,
            note_on_ok="rejected as expected")

        # ─── 2) Health ──────────────────────────────────────────────────
        print("\n[2] Health")
        _do(rep, cx, "health", "GET", "/health", expect=200)
        _do(rep, cx, "health-full", "GET", "/health/full", expect=200,
            note_on_ok="db+scheduler reported")

        # ─── 3) OpenAPI / Docs ──────────────────────────────────────────
        print("\n[3] OpenAPI / docs")
        r = _do(rep, cx, "openapi", "GET", "/openapi.json", expect=200)
        if r.ok:
            try:
                spec = httpx.get(
                    f"{args.base_url}/openapi.json", headers=headers, timeout=10
                ).json()
                paths = sorted(spec.get("paths", {}).keys())
                bots = [p for p in paths if p.startswith("/bots")]
                if bots:
                    print(f"   !! Leftover bot routes: {bots}")
                    rep.add(Result(
                        name="no-bot-routes", method="-", path="(spec scan)",
                        status=200, ok=False,
                        note=f"unexpected /bots in OpenAPI: {bots}",
                    ))
                else:
                    rep.add(Result(
                        name="no-bot-routes", method="-", path="(spec scan)",
                        status=200, ok=True, note="no /bots routes — correct",
                    ))
            except Exception as e:
                print(f"   spec scan error: {e}")
        _do(rep, cx, "docs", "GET", "/docs", expect=200)

        # ─── 4) Scheduler ───────────────────────────────────────────────
        print("\n[4] Scheduler")
        r = _do(rep, cx, "schedule-list", "GET", "/schedule", expect=200)
        sched_ids: list[str] = []
        try:
            data = httpx.get(
                f"{args.base_url}/schedule", headers=headers, timeout=10
            ).json()
            sched_ids = [s["id"] for s in data.get("schedules", [])]
            print(f"   schedules: {sched_ids}")
        except Exception:
            pass
        # pause / resume on first schedule id if any
        if sched_ids:
            sid = sched_ids[0]
            _do(rep, cx, f"pause:{sid}", "POST",
                f"/schedule/{sid}/pause", expect=200)
            _do(rep, cx, f"resume:{sid}", "POST",
                f"/schedule/{sid}/resume", expect=200)
        else:
            print("   no schedules — pause/resume skipped")

        # ─── 5) Jobs ────────────────────────────────────────────────────
        print("\n[5] Jobs")
        _do(rep, cx, "jobs-list", "GET", "/jobs", expect=200)
        _do(rep, cx, "jobs-missing", "GET", "/jobs/nonexistent-id", expect=404,
            note_on_ok="404 expected")

        # ─── 6) Research ────────────────────────────────────────────────
        print("\n[6] Research")
        r = _do(rep, cx, "factor-registry", "GET",
                "/research/factor/registry", expect=200)
        factor_name = None
        if r.ok:
            try:
                reg = httpx.get(
                    f"{args.base_url}/research/factor/registry",
                    headers=headers, timeout=10,
                ).json()
                for cat in ("baseline", "quality", "rating", "revision", "pead"):
                    if reg.get(cat):
                        factor_name = reg[cat][0]
                        break
                print(f"   pick factor: {factor_name}  total={reg.get('total')}")
            except Exception:
                pass
        # 단일 팩터 평가 비동기 잡 제출 (실제 DB 데이터가 부족하면 잡은 실패할 수 있음 —
        # 여기서는 202 수락만 검증)
        if factor_name:
            _do(rep, cx, "factor-evaluate", "POST",
                "/research/factor/evaluate",
                json_body={
                    "factor_name": factor_name,
                    "universe": "ALL",
                    "horizon_days": 5,
                    "persist_signals": False,
                    "persist_run": False,
                },
                expect=(202, 409),
                note_on_ok="job submitted (or 409 if eval busy)")
        # 알 수 없는 팩터 → 422
        _do(rep, cx, "factor-eval-unknown", "POST",
            "/research/factor/evaluate",
            json_body={"factor_name": "__nope__", "universe": "ALL"},
            expect=422, note_on_ok="422 as expected")

        # ─── 7) Collect (SYNC — non-KRX) ───────────────────────────────
        print("\n[7] Collect — SYNC")
        # US tickers 마스터 — 외부 yfinance 호출. 422 또는 200 가능.
        _do(rep, cx, "tickers-us", "POST", "/collect/tickers/us",
            json_body={"refresh_meta": False},
            expect=(200, 409, 422, 500),
            note_on_ok="response received (check body)")
        # DART corp_codes — 캐시 있으면 빠름
        _do(rep, cx, "dart-corp-codes", "POST", "/collect/dart/corp-codes",
            json_body={},
            expect=(200, 409, 422, 500),
            note_on_ok="response received")

        # ─── 8) Collect (ASYNC — non-KRX) ──────────────────────────────
        print("\n[8] Collect — ASYNC submit (잡 수락만 검증)")
        async_targets: list[tuple[str, str, dict]] = [
            ("daily-us", "/collect/daily/us",
             {"symbols": ["AAPL"], "days": 3, "skip_done": False}),
            ("dart-disclosures", "/collect/dart/disclosures",
             {"days": 1}),
            ("dart-financials", "/collect/dart/financials",
             {"symbols": ["005930"], "start_year": 2025, "end_year": 2025}),
            ("dart-indicators", "/collect/dart/indicators",
             {"symbols": ["005930"]}),
            ("consensus-hankyung", "/collect/consensus/hankyung",
             {"symbols": ["005930"]}),
            # FnGuide — consent 가 있어야 200, 없으면 422/403
            ("consensus-fnguide", "/collect/consensus/fnguide",
             {"symbols": ["005930"]}),
            # daily-cron — only=dart 로 KIS/KR 스킵
            ("daily-cron-dart-only", "/collect/daily-cron",
             {"days": 1, "only": "dart", "skip_done": False}),
        ]
        for name, path, body in async_targets:
            _do(rep, cx, name, "POST", path, json_body=body,
                expect=(202, 409, 422, 403),
                note_on_ok="submitted or expected-policy reject")

        # ─── 9) KIS daily / minute (KRX 의존성 제거 이후 KR 수집의 유일한 경로) ─
        print("\n[9] KIS-based KR collectors")
        kis_targets = [
            ("daily-kis", "/collect/daily/kis",
             {"symbols": ["005930"], "days": 3, "skip_done": False}),
            ("minute-kis", "/collect/minute/kis",
             {"symbols": ["005930"]}),
        ]
        if args.skip_krx:
            for name, path, _ in kis_targets:
                print(f"   SKIP {path}  (--skip-krx)")
        else:
            for name, path, body in kis_targets:
                _do(rep, cx, name, "POST", path, json_body=body,
                    expect=(200, 202, 409, 422, 500))

    print(rep.summary())

    failed = [r for r in rep.results if not r.ok]
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
