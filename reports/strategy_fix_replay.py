#!/usr/bin/env python3
"""Replay evidence check for the current strategy-fix rules."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from replay_opportunity_audit import replay_date
from store_candles_mysql import connect_mysql


DATES = ["2026-06-20", "2026-06-23", "2026-06-24", "2026-06-25"]
TIMEFRAME = "5m"
LIMIT = 500
MAX_STOP_POINTS = 30.0


@dataclass
class Metrics:
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    open_or_neutral: int = 0
    reward_points: float = 0.0
    risk_points: float = 0.0
    watch_only_skipped: int = 0
    unlocked_double_top_puts: int = 0

    def as_dict(self) -> dict[str, Any]:
        decided = self.winners + self.losers
        win_rate = (self.winners / decided * 100.0) if decided else 0.0
        profit_factor = (self.reward_points / self.risk_points) if self.risk_points else None
        return {
            "total_trades": self.total_trades,
            "winners": self.winners,
            "losers": self.losers,
            "open_or_neutral": self.open_or_neutral,
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "watch_only_skipped": self.watch_only_skipped,
            "unlocked_double_top_puts": self.unlocked_double_top_puts,
            "reward_points": round(self.reward_points, 2),
            "risk_points": round(self.risk_points, 2),
        }


def parse_reasons(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except Exception:
        pass
    return [raw]


def is_watch_only(pattern_name: str) -> bool:
    name = pattern_name.strip().lower()
    return name in {"w pattern breakout", "w pattern forming"}


def is_double_top_put(record: dict[str, Any]) -> bool:
    name = str(record.get("pattern_name") or "").strip().lower()
    side = str(record.get("signal_side") or "").strip().upper()
    return side == "PUT" and name in {"double top", "double top breakdown"}


def ema_aligned(reasons: list[str]) -> bool:
    lowered = [item.lower() for item in reasons]
    return "ema_trend_not_fully_aligned" not in lowered


def stop_valid(record: dict[str, Any]) -> bool:
    entry = float(record.get("entry_candidate") or 0.0)
    stop = float(record.get("stop_candidate") or 0.0)
    distance = abs(entry - stop)
    return distance > 0 and distance <= MAX_STOP_POINTS


def target_valid(record: dict[str, Any]) -> bool:
    entry = float(record.get("entry_candidate") or 0.0)
    target = float(record.get("target_candidate") or 0.0)
    return abs(target - entry) > 0


def qualifies_under_fix(record: dict[str, Any]) -> tuple[bool, str]:
    pattern_name = str(record.get("pattern_name") or "")
    if is_watch_only(pattern_name):
      return False, "watch_only_pattern"

    if is_double_top_put(record):
        reasons = parse_reasons(record.get("rejection_reasons"))
        confidence = float(record.get("confidence") or 0.0)
        projected_move = float(record.get("min_move_points") or 0.0)
        session_ok = bool(record.get("session_valid"))
        if (
            confidence >= 80.0
            and projected_move >= 70.0
            and ema_aligned(reasons)
            and session_ok
            and stop_valid(record)
            and target_valid(record)
        ):
            return True, "qualified_double_top_put"
        return False, "double_top_put_blocked"

    return False, "unchanged_non_trade_candidate"


def apply_trade_outcome(metrics: Metrics, record: dict[str, Any]) -> None:
    metrics.total_trades += 1
    entry = float(record.get("entry_candidate") or 0.0)
    target = float(record.get("target_candidate") or 0.0)
    stop = float(record.get("stop_candidate") or 0.0)
    reward = abs(target - entry)
    risk = abs(entry - stop)
    target_hit = bool(record.get("future_reached_target"))
    stop_hit_first = bool(record.get("future_hit_stop_first"))

    if target_hit and not stop_hit_first:
        metrics.winners += 1
        metrics.reward_points += reward
        return
    if stop_hit_first:
        metrics.losers += 1
        metrics.risk_points += risk
        return
    metrics.open_or_neutral += 1


def analyze_date(connection, market_date: str) -> dict[str, Any]:
    payload = replay_date(
        connection,
        market_date=market_date,
        symbol="NIFTY50",
        timeframe=TIMEFRAME,
        limit=LIMIT,
    )
    records = payload.get("records") or []
    metrics = Metrics()
    examples: list[dict[str, Any]] = []

    for record in records:
        qualified, reason = qualifies_under_fix(record)
        if reason == "watch_only_pattern":
            metrics.watch_only_skipped += 1
        if qualified:
            metrics.unlocked_double_top_puts += 1
            apply_trade_outcome(metrics, record)
            if len(examples) < 6:
                examples.append(
                    {
                        "market_time": record.get("market_time"),
                        "pattern_name": record.get("pattern_name"),
                        "signal_side": record.get("signal_side"),
                        "confidence": record.get("confidence"),
                        "projected_move": record.get("min_move_points"),
                        "future_max_move_points": record.get("future_max_move_points"),
                        "future_reached_target": record.get("future_reached_target"),
                        "future_hit_stop_first": record.get("future_hit_stop_first"),
                    }
                )

    return {
        "date": market_date,
        "source_wait_candidates": len(records),
        "summary": metrics.as_dict(),
        "examples": examples,
    }


def main() -> None:
    connection = connect_mysql()
    try:
        per_day = [analyze_date(connection, market_date) for market_date in DATES]
    finally:
        connection.close()

    total = Metrics()
    total_candidates = 0
    for day in per_day:
        total_candidates += int(day["source_wait_candidates"])
        summary = day["summary"]
        total.total_trades += int(summary["total_trades"])
        total.winners += int(summary["winners"])
        total.losers += int(summary["losers"])
        total.open_or_neutral += int(summary["open_or_neutral"])
        total.watch_only_skipped += int(summary["watch_only_skipped"])
        total.unlocked_double_top_puts += int(summary["unlocked_double_top_puts"])
        total.reward_points += float(summary["reward_points"])
        total.risk_points += float(summary["risk_points"])

    output = {
        "dates": DATES,
        "candidate_source": "historical replay WAIT-candidate stream",
        "total_source_candidates": total_candidates,
        "per_day": per_day,
        "overall": total.as_dict(),
        "notes": [
            "This report measures the newly allowed trade subset after the strategy fix.",
            "W Pattern Breakout and W Pattern Forming are treated as WATCH-only and excluded from BUY trades.",
            "Only Double Top / Double Top Breakdown PUT setups with confidence >= 80, projected move >= 70, EMA alignment, valid session, and valid stop are promoted to trade candidates.",
            "Premium is intentionally ignored as a trade blocker here and treated as record-only.",
        ],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
