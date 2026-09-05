#!/usr/bin/env python3
"""
scan_clock.py — align the scanner to bar closes instead of a free-running timer.

A 60-second timer drifts against 5-minute bar boundaries, so on average you
learn about a closed bar ~30s after it closed, and up to 60s late. Since the
tradeable decision is made at a bar close, that latency is pure loss.

This waits until just after each bar boundary instead.

    from scan_clock import bar_close_sleeper
    for _ in bar_close_sleeper(interval_minutes=5, offset_seconds=4):
        run_scan()          # always fires ~4s after 09:20:00, 09:25:00, ...
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


def next_bar_close(now: datetime, interval_minutes: int, offset_seconds: int) -> datetime:
    """The next wall-clock instant `offset_seconds` after a bar boundary."""
    minute_of_day = now.hour * 60 + now.minute
    open_minute = MARKET_OPEN[0] * 60 + MARKET_OPEN[1]
    elapsed = minute_of_day - open_minute
    bars_done = elapsed // interval_minutes + 1
    boundary_minute = open_minute + bars_done * interval_minutes
    target = now.replace(hour=boundary_minute // 60, minute=boundary_minute % 60,
                         second=offset_seconds, microsecond=0)
    if target <= now:
        target += timedelta(minutes=interval_minutes)
    return target


def in_market_hours(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    m = dt.hour * 60 + dt.minute
    return (MARKET_OPEN[0] * 60 + MARKET_OPEN[1]) <= m <= (MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1])


def bar_close_sleeper(interval_minutes: int = 5, offset_seconds: int = 4,
                      idle_seconds: int = 30) -> Iterator[datetime]:
    """Yields once shortly after every bar close during market hours."""
    while True:
        now = datetime.now(IST)
        if not in_market_hours(now):
            time.sleep(idle_seconds)
            continue
        target = next_bar_close(now, interval_minutes, offset_seconds)
        gap = (target - datetime.now(IST)).total_seconds()
        if gap > 0:
            time.sleep(gap)
        yield datetime.now(IST)


if __name__ == "__main__":
    base = datetime(2026, 9, 4, 10, 3, 27, tzinfo=IST)
    print("now                  -> next scan")
    for mins in (0, 1, 2, 5, 7):
        n = base + timedelta(minutes=mins)
        print(f"  {n:%H:%M:%S}  ->  {next_bar_close(n, 5, 4):%H:%M:%S}")
