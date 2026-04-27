"""pykrx 동작 확인용 스크립트.

사용법:
    cd C:\\Users\\Playdata\\workspace\\stock_trader
    .venv\\Scripts\\activate
    python scripts\\debug_pykrx.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from pykrx import stock


def _banner(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def test_version() -> None:
    _banner("1. pykrx version")
    try:
        import pykrx
        print(f"pykrx version: {getattr(pykrx, '__version__', 'unknown')}")
    except Exception as e:
        print(f"FAIL: {e}")


def test_single_ticker_ohlcv() -> None:
    """가장 원시적인 호출 — 삼성전자 특정 일자."""
    _banner("2. get_market_ohlcv(start, end, ticker) — 삼성전자 최근 5거래일")
    # 최근 평일 범위
    end = date.today()
    start = end - timedelta(days=10)
    try:
        df = stock.get_market_ohlcv(
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            "005930",
        )
        print(f"shape: {df.shape}")
        print(f"columns: {list(df.columns)}")
        print(df.tail(5))
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")


def test_all_symbols_ohlcv() -> None:
    """문제의 호출 — 전 종목, 특정 일자."""
    _banner("3. get_market_ohlcv(date, market=KOSPI) — 어제 날짜")
    # 가장 최근 평일
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    d -= timedelta(days=1)  # 하루 더 과거로

    try:
        df = stock.get_market_ohlcv(d.strftime("%Y%m%d"), market="KOSPI")
        print(f"date: {d}")
        print(f"shape: {df.shape}")
        print(f"columns: {list(df.columns)}")
        if not df.empty:
            print(df.head(3))
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")


def test_old_date() -> None:
    """확실히 안정적인 과거 날짜."""
    _banner("4. get_market_ohlcv(2024-06-03, market=KOSPI)")
    try:
        df = stock.get_market_ohlcv("20240603", market="KOSPI")
        print(f"shape: {df.shape}")
        print(f"columns: {list(df.columns)}")
        if not df.empty:
            print(df.head(3))
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")


def test_ticker_list() -> None:
    """종목 마스터는 잘 받아지는지."""
    _banner("5. get_market_ticker_list(market=KOSPI)")
    try:
        tickers = stock.get_market_ticker_list(market="KOSPI")
        print(f"count: {len(tickers)}")
        print(f"sample: {tickers[:5]}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print(f"Python: {sys.version}")
    test_version()
    test_ticker_list()
    test_single_ticker_ohlcv()
    test_all_symbols_ohlcv()
    test_old_date()
    print("\n" + "=" * 60)
    print("Done.")
