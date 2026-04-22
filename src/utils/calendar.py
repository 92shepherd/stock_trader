"""Trading calendar utilities for KRX."""
from __future__ import annotations

from datetime import date, timedelta

from pykrx import stock


def get_business_days(start: date, end: date) -> list[date]:
    """Return list of KRX trading days between [start, end] inclusive."""
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")
    days = stock.get_previous_business_day(start_s, end_s)  # noqa: F841
    # pykrx returns a single day; better approach: iterate and filter
    result: list[date] = []
    d = start
    while d <= end:
        # pykrx has no direct is_open; use nearest business day
        prev = stock.get_nearest_business_day_in_a_week(
            d.strftime("%Y%m%d"), prev=False
        )
        if prev == d.strftime("%Y%m%d"):
            result.append(d)
        d += timedelta(days=1)
    return result


def latest_business_day(ref: date | None = None) -> date:
    """Return the latest KRX trading day <= ref (default: today)."""
    ref = ref or date.today()
    s = stock.get_nearest_business_day_in_a_week(ref.strftime("%Y%m%d"))
    return date.fromisoformat(f"{s[:4]}-{s[4:6]}-{s[6:]}")
