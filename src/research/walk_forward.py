"""Walk-forward split generators for time-series-respecting evaluation.

Why walk-forward (not k-fold):
    In time-series data, future leaks into the past trivially destroy
    evaluation validity. Shuffled cross-validation puts future days in
    the train set used to predict earlier days - the model "cheats"
    by definition. Walk-forward enforces strict temporal ordering:
    train on [t0, t1], test on [t1 + gap, t2], then advance.

Two flavors:

    Rolling   - fixed train window length. As time advances the train
                window slides forward so old data is dropped. Good when
                you suspect strong non-stationarity (recent regime
                weighted more).

    Expanding - train window starts at a fixed t0 and grows. Test
                window slides forward. Good when you assume the data
                generating process is stable enough to benefit from
                more samples.

The `gap_days` parameter is the buffer between train end and test start.
Set it to >= horizon_days when targets overlap (a horizon=20 forward
return on day t is known only on t+20; including day t in train and
day t+1 in test leaks the label).

Output:
    List of WalkForwardSplit namedtuples (start/end of train and test).
    All dates are inclusive ends.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class WalkForwardSplit:
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    def __repr__(self) -> str:
        return (
            f"<WFSplit train=[{self.train_start}..{self.train_end}] "
            f"test=[{self.test_start}..{self.test_end}]>"
        )


def rolling_windows(
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    step_days: int | None = None,
    gap_days: int = 0,
) -> list[WalkForwardSplit]:
    """Rolling walk-forward splits over [start, end].

    Args:
        start: overall window start (calendar day).
        end: overall window end (calendar day).
        train_days: length of each train window in CALENDAR days.
                    (Calendar, not trading - simpler and the eval logic
                    later filters to business days anyway.)
        test_days: length of each test window in calendar days.
        step_days: advance step between consecutive splits. Default
                   = test_days (non-overlapping test windows).
        gap_days: buffer between train_end and test_start. Set this to
                  >= forecast horizon to prevent label leakage when
                  forward returns overlap.

    Returns:
        List of splits in chronological order. Empty if [start, end] is
        too short to fit one full split.

    Raises:
        ValueError on non-positive day counts or start > end.
    """
    _validate(start, end, train_days, test_days, gap_days)
    if step_days is None:
        step_days = test_days
    if step_days < 1:
        raise ValueError(f"step_days must be >= 1, got {step_days}")

    splits: list[WalkForwardSplit] = []
    cursor = start
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days - 1)
        test_start = train_end + timedelta(days=gap_days + 1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > end:
            break
        splits.append(WalkForwardSplit(
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
        ))
        cursor = cursor + timedelta(days=step_days)
    return splits


def expanding_windows(
    start: date,
    end: date,
    initial_train_days: int,
    test_days: int,
    step_days: int | None = None,
    gap_days: int = 0,
) -> list[WalkForwardSplit]:
    """Expanding (growing-train) walk-forward splits.

    Same arguments as rolling_windows, except `initial_train_days` is
    the LENGTH of the first train window. Each subsequent split keeps
    the same train_start (anchored at `start`) but train_end advances
    by step_days.
    """
    _validate(start, end, initial_train_days, test_days, gap_days)
    if step_days is None:
        step_days = test_days
    if step_days < 1:
        raise ValueError(f"step_days must be >= 1, got {step_days}")

    splits: list[WalkForwardSplit] = []
    train_start = start
    cursor_train_end = start + timedelta(days=initial_train_days - 1)
    while True:
        train_end = cursor_train_end
        test_start = train_end + timedelta(days=gap_days + 1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > end:
            break
        splits.append(WalkForwardSplit(
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
        ))
        cursor_train_end = cursor_train_end + timedelta(days=step_days)
    return splits


def _validate(
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    gap_days: int,
) -> None:
    if start > end:
        raise ValueError(f"start {start} > end {end}")
    if train_days < 1:
        raise ValueError(f"train_days must be >= 1, got {train_days}")
    if test_days < 1:
        raise ValueError(f"test_days must be >= 1, got {test_days}")
    if gap_days < 0:
        raise ValueError(f"gap_days must be >= 0, got {gap_days}")
