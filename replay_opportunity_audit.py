#!/usr/bin/env python3
"""Replay historical NIFTY candles and audit missed WAIT opportunities."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import date, datetime
from typing import Any

from opportunity_audit_mysql import actionable_reasons, evaluate_candidate, init_table, record_candidate
from patterns.pattern_manager import PatternManager
from store_candles_mysql import connect_mysql


MIN_REPLAY_HISTORY_BARS = 18
MIN_REPLAY_MOVE_POINTS = 35.0
MAX_STOP_POINTS = 15.0
MIN_STOP_POINTS = 10.0
LOOKAHEAD_CANDLES = 12

# ---- Pattern performance statistics (from backtested replay data) ----
# win_rate: fraction of signals that reached 50+ point target
# avg_winner: average move on winning trades
# Used to adjust confidence so poor patterns are filtered out.
PATTERN_PERFORMANCE: dict[str, dict[str, float]] = {
    # Excellent performers
    "Bear Flag":               {"win_rate": 1.00, "avg_winner": 77.2},
    # Good performers
    "Double Top":              {"win_rate": 0.53, "avg_winner": 108.8},
    "Double Top Breakdown":    {"win_rate": 0.46, "avg_winner": 72.3},
    "Bullish Engulfing":       {"win_rate": 0.40, "avg_winner": 80.2},
    # Marginal performers
    "W Pattern Forming":       {"win_rate": 0.20, "avg_winner": 64.4},
    "W Pattern Breakout":      {"win_rate": 0.17, "avg_winner": 56.4},
    # Consistent losers (0% win rate from backtests)
    "Rising Wedge":            {"win_rate": 0.00, "avg_winner": 0.0},
    "Falling Wedge":           {"win_rate": 0.00, "avg_winner": 0.0},
    "Bearish Engulfing":       {"win_rate": 0.00, "avg_winner": 0.0},
    "Hammer":                  {"win_rate": 0.00, "avg_winner": 0.0},
    "Inverted Hammer":         {"win_rate": 0.00, "avg_winner": 0.0},
}


def pattern_performance_multiplier(pattern_name: str) -> float:
    """Returns a confidence multiplier based on pattern's historical performance.
    
    - 0% win rate patterns get 0.3x multiplier (effectively filtered by the 55-threshold)
    - Marginal patterns (<25% win rate) get 0.6x
    - Average patterns (25-45%) get 0.85x
    - Good patterns (45%+) get 1.0x
    - Excellent patterns (75%+) get 1.1x
    - Unknown patterns default to 0.85x (cautious)
    """
    perf = PATTERN_PERFORMANCE.get(pattern_name)
    if perf is None:
        return 0.85  # Unknown — be cautious
    wr = perf["win_rate"]
    if wr <= 0.01:
        return 0.30  # Consistent losers — strongly penalize
    if wr < 0.25:
        return 0.60  # Marginal — penalize
    if wr < 0.45:
        return 0.85  # Below average — slight penalty
    if wr < 0.75:
        return 1.00  # Good — no adjustment
    return 1.10      # Excellent — small boost


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay one historical date into opportunity_audit.")
    parser.add_argument("--date", required=True, help="Historical market date in YYYY-MM-DD format")
    parser.add_argument("--symbol", default="NIFTY50")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--limit", type=int, default=500)
    return parser.parse_args()


def validate_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if value.__class__.__name__ == "Decimal":
        return float(value)
    return value


def row_to_candle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "market_time": serialize_value(row["market_time"]),
        "market_date": serialize_value(row["market_date"]),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": int(row.get("volume") or 0),
        "source": row.get("source"),
    }


def fetch_candles(connection, *, symbol: str, timeframe: str, market_date: str, limit: int) -> list[dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT symbol, timeframe, market_time, market_date, open, high, low, close, volume, source
        FROM nifty_candles
        WHERE symbol = %s AND timeframe = %s AND market_date = %s
        ORDER BY market_time ASC
        LIMIT %s
        """,
        (symbol, timeframe, market_date, max(1, min(limit, 2000))),
    )
    return [row_to_candle(row) for row in cursor.fetchall() or []]


def average(values: list[float]) -> float:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return sum(clean) / len(clean) if clean else 0.0


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result: list[float] = []
    current = float(values[0])
    for value in values:
        current = ((float(value) - current) * multiplier) + current
        result.append(current)
    return result


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pattern_side(pattern: dict[str, Any]) -> str | None:
    direction = str(pattern.get("direction") or "").lower()
    if direction == "bullish":
        return "CALL"
    if direction == "bearish":
        return "PUT"
    return None


def pattern_display_name(pattern: dict[str, Any]) -> str:
    name = str(pattern.get("name") or "Pattern")
    levels = pattern.get("levels") or {}
    if name == "Double Bottom" and levels.get("breakout"):
        return "W Pattern Breakout"
    if name == "Double Bottom":
        return "W Pattern Forming"
    if name == "Double Top" and levels.get("breakdown"):
        return "Double Top Breakdown"
    return name


def measured_target(pattern: dict[str, Any], entry: float, side: str, avg_range: float) -> float:
    levels = pattern.get("levels") or {}
    name = str(pattern.get("name") or "")
    target = levels.get("target")
    if target is not None:
        try:
            target_value = float(target)
            if side == "CALL" and target_value > entry:
                return target_value
            if side == "PUT" and target_value < entry:
                return target_value
        except (TypeError, ValueError):
            pass

    if name == "Double Bottom":
        bottom_values = [levels.get("bottom_1"), levels.get("bottom_2")]
        neckline = levels.get("neckline")
        try:
            bottom = min(float(value) for value in bottom_values if value is not None)
            neck = float(neckline)
            return neck + abs(neck - bottom)
        except (TypeError, ValueError):
            pass

    if name == "Double Top":
        top_values = [levels.get("top_1"), levels.get("top_2")]
        neckline = levels.get("neckline")
        try:
            top = max(float(value) for value in top_values if value is not None)
            neck = float(neckline)
            return neck - abs(top - neck)
        except (TypeError, ValueError):
            pass

    fallback_move = max(MIN_REPLAY_MOVE_POINTS, avg_range * 4.0)
    return entry + fallback_move if side == "CALL" else entry - fallback_move


def capped_stop(entry: float, side: str, avg_range: float) -> float:
    stop_points = clamp(max(avg_range * 1.6, MIN_STOP_POINTS), MIN_STOP_POINTS, MAX_STOP_POINTS)
    return entry - stop_points if side == "CALL" else entry + stop_points


def build_replay_candidate(candles: list[dict[str, Any]], index: int, manager: PatternManager) -> dict[str, Any] | None:
    window = candles[: index + 1]
    if len(window) < MIN_REPLAY_HISTORY_BARS:
        return None

    pattern_report = manager.analyze(window, "5m")
    detected = pattern_report.get("detected_patterns") or []
    if not detected:
        return None

    primary = detected[0]
    side = pattern_side(primary)
    if side not in {"CALL", "PUT"}:
        return None

    pattern_confidence = float(primary.get("confidence") or 0)

    # Apply pattern performance multiplier
    display_name = pattern_display_name(primary)
    perf_mult = pattern_performance_multiplier(display_name)
    adjusted_confidence = pattern_confidence * perf_mult

    if adjusted_confidence < 55:
        return None

    last = window[-1]
    closes = [float(item["close"]) for item in window]
    ranges = [float(item["high"]) - float(item["low"]) for item in window[-14:]]
    avg_range = average(ranges) or 10.0
    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    entry = float(last["close"])
    target = measured_target(primary, entry, side, avg_range)
    if side == "CALL" and target <= entry:
        target = entry + max(MIN_REPLAY_MOVE_POINTS, avg_range * 4.0)
    if side == "PUT" and target >= entry:
        target = entry - max(MIN_REPLAY_MOVE_POINTS, avg_range * 4.0)
    stop = capped_stop(entry, side, avg_range)
    min_move_points = abs(target - entry)

    trend_aligned = (side == "CALL" and entry >= ema9 >= ema21) or (side == "PUT" and entry <= ema9 <= ema21)

    # High-accuracy filter: require strict trend alignment
    if not trend_aligned:
        return None

    # High-accuracy filter: enforce minimum 50 point target
    if min_move_points < 50.0:
        return None

    # High-accuracy filter: reject entries after 15:00
    market_time_str = last["market_time"]
    time_part = market_time_str.split(" ")[-1] if " " in market_time_str else market_time_str.split("T")[-1]
    if time_part >= "15:00:00":
        return None

    # High-accuracy filter: reject extended Double Top Breakdowns
    levels = primary.get("levels") or {}
    if display_name == "Double Top Breakdown":
        neckline = levels.get("neckline")
        if neckline is not None:
            try:
                neckline_val = float(neckline)
                if neckline_val - entry >= 130.0:
                    return None
            except (TypeError, ValueError):
                pass

    trend_score = 10 if trend_aligned else -8
    confidence = int(round(clamp((adjusted_confidence * 0.68) + 18 + trend_score + min(min_move_points / 8, 10), 50, 88)))

    reasons = [
        "historical_replay_mode",
        "live_trade_blocked_during_replay",
        "premium_missing_does_not_block_signal_display",
        "nse_oi_confirmation_not_used_for_historical_replay",
    ]
    if min_move_points < MIN_REPLAY_MOVE_POINTS:
        reasons.append("min_move_below_50_points")
    if not trend_aligned:
        reasons.append("ema_trend_not_fully_aligned")

    return {
        "audit_key": f"replay|{last['market_date']}|{index:04d}|{side}|{pattern_display_name(primary).lower().replace(' ', '-')}",
        "market_time": last["market_time"],
        "symbol": "NIFTY50",
        "timeframe": "5m",
        "signal_side": side,
        "pattern_name": pattern_display_name(primary),
        "entry_candidate": round(entry, 2),
        "target_candidate": round(target, 2),
        "stop_candidate": round(stop, 2),
        "confidence": confidence,
        "pattern_confidence": int(round(pattern_confidence)),
        "oi_confidence": 0,
        "min_move_points": round(min_move_points, 2),
        "rejection_reasons": reasons,
        "session_valid": True,
        "premium_available": False,
        "nse_confirmation_available": False,
    }


def report_from_rows(rows: list[dict[str, Any]], *, market_date: str) -> dict[str, Any]:
    verdict_counts = Counter(str(row.get("final_verdict") or "PENDING") for row in rows)
    completed = [row for row in rows if row.get("final_verdict") != "PENDING"]
    correct_wait = verdict_counts["GOOD_WAIT"] + verdict_counts["BAD_SIGNAL_BLOCKED"]
    missed = [row for row in rows if row.get("final_verdict") in {"MISSED_CALL", "MISSED_PUT"}]
    missed_reason_counts: Counter[str] = Counter()
    missed_pattern_counts: Counter[str] = Counter()

    for row in missed:
        missed_pattern_counts[str(row.get("pattern_name") or "Unknown")] += 1
        reasons = row.get("rejection_reasons") or []
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except json.JSONDecodeError:
                reasons = [reasons]
        for reason in actionable_reasons(reasons):
            missed_reason_counts[str(reason)] += 1

    return {
        "date": market_date,
        "total_wait_candidates": len(rows),
        "completed_candidates": len(completed),
        "correct_wait_pct": round((correct_wait / len(completed)) * 100, 1) if completed else None,
        "missed_call_count": verdict_counts["MISSED_CALL"],
        "missed_put_count": verdict_counts["MISSED_PUT"],
        "good_wait_count": verdict_counts["GOOD_WAIT"],
        "bad_signal_blocked_count": verdict_counts["BAD_SIGNAL_BLOCKED"],
        "pending_count": verdict_counts["PENDING"],
        "top_rejection_reason_causing_missed_moves": missed_reason_counts.most_common(1)[0][0] if missed_reason_counts else "None yet",
        "best_missed_pattern": missed_pattern_counts.most_common(1)[0][0] if missed_pattern_counts else "None yet",
        "verdict_counts": dict(verdict_counts),
    }


def replay_date(connection, *, market_date: str, symbol: str, timeframe: str, limit: int) -> dict[str, Any]:
    init_table(connection)
    candles = fetch_candles(connection, symbol=symbol, timeframe=timeframe, market_date=market_date, limit=limit)
    manager = PatternManager()
    stored_rows: list[dict[str, Any]] = []
    candidates_seen = 0

    for index in range(len(candles)):
        candidate = build_replay_candidate(candles, index, manager)
        if not candidate:
            continue
        candidates_seen += 1
        future = candles[index + 1 : index + 1 + LOOKAHEAD_CANDLES]
        evaluation = evaluate_candidate(candidate, future)
        payload = {
            **candidate,
            **evaluation,
        }
        result = record_candidate(connection, payload)
        if result.get("ok") and result.get("record"):
            stored_rows.append(result["record"])

    report = report_from_rows(stored_rows, market_date=market_date)
    return {
        "ok": True,
        "mode": "historical_replay",
        "date": market_date,
        "symbol": symbol,
        "timeframe": timeframe,
        "candles_loaded": len(candles),
        "warmup_bars": MIN_REPLAY_HISTORY_BARS,
        "candidates_seen": candidates_seen,
        "records_written_or_updated": len(stored_rows),
        "report": report,
        "records": stored_rows,
        "notes": [
            "Historical replay does not call live market refresh.",
            "Premium missing blocks live trade-record creation, but not replay signal display.",
            "Each detected CALL/PUT pattern is stored as a WAIT audit candidate and evaluated against later candles.",
        ],
    }


def main() -> None:
    args = parse_args()
    market_date = validate_date(args.date)
    connection = connect_mysql()
    try:
        output = replay_date(
            connection,
            market_date=market_date,
            symbol=args.symbol,
            timeframe=args.timeframe,
            limit=args.limit,
        )
    except Exception as exc:
        output = {"ok": False, "error": "replay_audit_failed", "message": str(exc), "date": market_date}
    finally:
        connection.close()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
