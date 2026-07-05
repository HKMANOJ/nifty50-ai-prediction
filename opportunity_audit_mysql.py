#!/usr/bin/env python3
"""Persist and evaluate rejected WAIT opportunities for NIFTY AI."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

from store_candles_mysql import connect_mysql


MIN_MISSED_MOVE_POINTS = 35.0
MIN_EVALUATION_CANDLES = 6
MAX_EVALUATION_CANDLES = 12
INTERNAL_REPLAY_REJECTION_REASONS = {
    "historical_replay_mode",
    "live_trade_blocked_during_replay",
    "premium_missing_does_not_block_signal_display",
    "nse_oi_confirmation_not_used_for_historical_replay",
}


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS opportunity_audit (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  audit_key VARCHAR(180) NOT NULL UNIQUE,
  market_time DATETIME NOT NULL,
  symbol VARCHAR(32) NOT NULL DEFAULT 'NIFTY50',
  timeframe VARCHAR(8) NOT NULL DEFAULT '5m',
  signal_side VARCHAR(16) NOT NULL,
  pattern_name VARCHAR(128),
  entry_candidate DECIMAL(12,2),
  target_candidate DECIMAL(12,2),
  stop_candidate DECIMAL(12,2),
  confidence INT,
  pattern_confidence INT,
  oi_confidence INT,
  min_move_points DECIMAL(12,2),
  rejection_reasons TEXT,
  session_valid TINYINT(1) DEFAULT 0,
  premium_available TINYINT(1) DEFAULT 0,
  nse_confirmation_available TINYINT(1) DEFAULT 0,
  future_max_move_points DECIMAL(12,2),
  future_reached_50_points TINYINT(1),
  future_reached_target TINYINT(1),
  future_hit_stop_first TINYINT(1),
  candles_to_reach_50 INT,
  candles_evaluated INT DEFAULT 0,
  final_verdict VARCHAR(32) NOT NULL DEFAULT 'PENDING',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_opportunity_time (symbol, timeframe, market_time),
  INDEX idx_opportunity_verdict (final_verdict, created_at),
  INDEX idx_opportunity_pattern (pattern_name)
)
"""


ALTER_TABLE_SQL = (
    "ALTER TABLE opportunity_audit ADD COLUMN audit_key VARCHAR(180) NOT NULL UNIQUE",
    "ALTER TABLE opportunity_audit ADD COLUMN symbol VARCHAR(32) NOT NULL DEFAULT 'NIFTY50'",
    "ALTER TABLE opportunity_audit ADD COLUMN timeframe VARCHAR(8) NOT NULL DEFAULT '5m'",
    "ALTER TABLE opportunity_audit ADD COLUMN future_hit_stop_first TINYINT(1) NULL",
    "ALTER TABLE opportunity_audit ADD COLUMN candles_evaluated INT DEFAULT 0",
    "ALTER TABLE opportunity_audit ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage rejected WAIT opportunity audit records.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create opportunity_audit table if needed")

    record = sub.add_parser("record", help="Insert or update a rejected WAIT candidate")
    record.add_argument("--payload", required=True, help="JSON audit payload")

    update = sub.add_parser("update-pending", help="Evaluate pending audit rows against later candles")
    update.add_argument("--limit", type=int, default=500)

    summary = sub.add_parser("summary", help="Print dashboard opportunity audit summary")
    summary.add_argument("--limit", type=int, default=1000)

    history = sub.add_parser("history", help="Print recent opportunity audit rows")
    history.add_argument("--limit", type=int, default=120)

    debug = sub.add_parser("debug", help="Print date/verdict-specific audit diagnostics")
    debug.add_argument("--date", required=True, help="Market date in YYYY-MM-DD format")
    debug.add_argument("--verdict", default="", help="Optional final verdict filter, e.g. MISSED_PUT")
    debug.add_argument("--limit", type=int, default=500)

    return parser.parse_args()


def init_table(connection) -> None:
    cursor = connection.cursor()
    cursor.execute(CREATE_TABLE_SQL)
    for statement in ALTER_TABLE_SQL:
        try:
            cursor.execute(statement)
        except Exception:
            pass
    connection.commit()


def serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat(sep=" ")
        elif isinstance(value, date):
            result[key] = value.isoformat()
        elif isinstance(value, (bytes, bytearray)):
            result[key] = value.decode("utf-8", errors="replace")
        elif value.__class__.__name__ == "Decimal":
            result[key] = float(value)
        else:
            result[key] = value
    return result


def nullable_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def nullable_int(value: Any) -> int | None:
    number = nullable_number(value)
    return int(number) if number is not None else None


def bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def normalize_side(value: str) -> str:
    text = str(value or "").upper().replace("BUY ", "").strip()
    if "CALL" in text:
        return "CALL"
    if "PUT" in text:
        return "PUT"
    return "WAIT"


def normalize_market_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("market_time is required for opportunity audit")
    return text.replace("T", " ").split("+", 1)[0]


def rejection_reasons_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return json.dumps([str(item) for item in parsed], separators=(",", ":"))
        except json.JSONDecodeError:
            return json.dumps([value], separators=(",", ":"))
    if isinstance(value, list):
        return json.dumps([str(item) for item in value if str(item).strip()], separators=(",", ":"))
    return "[]"


def parse_reasons(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [str(value)]


def actionable_reasons(reasons: list[str]) -> list[str]:
    """Remove bookkeeping labels so summaries show actual decision blockers."""
    return [reason for reason in reasons if reason not in INTERNAL_REPLAY_REJECTION_REASONS]


def audit_source_type(row: dict[str, Any]) -> str:
    reasons = set(parse_reasons(row.get("rejection_reasons")))
    audit_key = str(row.get("audit_key") or "")
    if "historical_replay_mode" in reasons or audit_key.startswith("replay|"):
        return "backend_replay"
    return "browser_or_live_audit"


def compact_market_time(value: Any) -> str:
    text = str(value or "")
    if " " in text:
        return text.rsplit(" ", 1)[-1][:5]
    if "T" in text:
        return text.rsplit("T", 1)[-1][:5]
    return text[:5]


def pattern_bucket(pattern_name: str) -> str:
    name = str(pattern_name or "").lower()
    if "liquidity" in name or "sweep" in name:
        return "Liquidity Sweep"
    if "double top" in name:
        return "Double Top"
    if "m pattern" in name:
        return "M Pattern"
    if "breakdown" in name:
        return "Breakdown"
    return "Other"


def move_bucket(move_points: Any) -> str:
    move = nullable_number(move_points) or 0.0
    if move >= 50:
        return "moved_50_plus"
    if move >= 35:
        return "moved_35_to_49"
    if move >= 20:
        return "moved_20_to_34"
    return "moved_below_20"


def build_audit_key(payload: dict[str, Any]) -> str:
    market_time = normalize_market_time(payload.get("market_time"))
    side = normalize_side(payload.get("signal_side"))
    pattern = str(payload.get("pattern_name") or "pattern").lower().replace(" ", "-")[:48]
    entry = nullable_number(payload.get("entry_candidate"))
    entry_part = f"{entry:.2f}" if entry is not None else "na"
    return f"{market_time}|{side}|{pattern}|{entry_part}"


def fetch_future_candles(connection, row: dict[str, Any], *, max_candles: int = MAX_EVALUATION_CANDLES) -> list[dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT market_time, open, high, low, close
        FROM nifty_candles
        WHERE symbol = %s
          AND timeframe = %s
          AND market_time > %s
        ORDER BY market_time ASC
        LIMIT %s
        """,
        (
            row.get("symbol") or "NIFTY50",
            row.get("timeframe") or "5m",
            row["market_time"],
            max(1, min(max_candles, MAX_EVALUATION_CANDLES)),
        ),
    )
    return [serialize_row(item) for item in cursor.fetchall() or []]


def evaluate_candidate(row: dict[str, Any], candles: list[dict[str, Any]]) -> dict[str, Any]:
    side = normalize_side(row.get("signal_side"))
    entry = nullable_number(row.get("entry_candidate"))
    target = nullable_number(row.get("target_candidate"))
    stop = nullable_number(row.get("stop_candidate"))
    if side not in {"CALL", "PUT"} or entry is None:
        return {"final_verdict": "PENDING", "candles_evaluated": len(candles)}

    future_max_move = 0.0
    reached_50_at: int | None = None
    reached_target_at: int | None = None
    stop_first_at: int | None = None

    for index, candle in enumerate(candles[:MAX_EVALUATION_CANDLES], start=1):
        open_price = nullable_number(candle.get("open"))
        high = nullable_number(candle.get("high"))
        low = nullable_number(candle.get("low"))
        if open_price is None or high is None or low is None:
            continue

        if side == "CALL":
            move = high - entry
            reached_50 = high >= entry + MIN_MISSED_MOVE_POINTS
            reached_target = target is not None and high >= target
            hit_stop = stop is not None and low <= stop
            fifty_level = entry + MIN_MISSED_MOVE_POINTS
        else:
            move = entry - low
            reached_50 = low <= entry - MIN_MISSED_MOVE_POINTS
            reached_target = target is not None and low <= target
            hit_stop = stop is not None and high >= stop
            fifty_level = entry - MIN_MISSED_MOVE_POINTS

        future_max_move = max(future_max_move, move)

        if hit_stop and not reached_50 and not reached_target and stop_first_at is None:
            stop_first_at = index

        if hit_stop and (reached_50 or reached_target) and stop_first_at is None:
            stop_distance = abs(open_price - stop) if stop is not None else float("inf")
            profit_level = target if reached_target and target is not None else fifty_level
            profit_distance = abs(open_price - profit_level)
            if stop_distance <= profit_distance:
                stop_first_at = index
                continue

        if reached_50 and reached_50_at is None:
            reached_50_at = index
        if reached_target and reached_target_at is None:
            reached_target_at = index

    evaluated = len(candles[:MAX_EVALUATION_CANDLES])
    final_verdict = "PENDING"
    if stop_first_at is not None and reached_50_at is None and reached_target_at is None:
        final_verdict = "BAD_SIGNAL_BLOCKED"
    elif reached_50_at is not None:
        final_verdict = "MISSED_CALL" if side == "CALL" else "MISSED_PUT"
    elif evaluated >= MAX_EVALUATION_CANDLES:
        final_verdict = "GOOD_WAIT"
    elif evaluated >= MIN_EVALUATION_CANDLES and stop_first_at is not None:
        final_verdict = "BAD_SIGNAL_BLOCKED"

    return {
        "future_max_move_points": round(future_max_move, 2),
        "future_reached_50_points": reached_50_at is not None,
        "future_reached_target": reached_target_at is not None,
        "future_hit_stop_first": stop_first_at is not None and (reached_50_at is None or stop_first_at <= reached_50_at),
        "candles_to_reach_50": reached_50_at,
        "candles_evaluated": evaluated,
        "final_verdict": final_verdict,
    }


def update_evaluation_fields(connection, row_id: int, evaluation: dict[str, Any]) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE opportunity_audit
        SET future_max_move_points = %s,
            future_reached_50_points = %s,
            future_reached_target = %s,
            future_hit_stop_first = %s,
            candles_to_reach_50 = %s,
            candles_evaluated = %s,
            final_verdict = %s
        WHERE id = %s
        """,
        (
            nullable_number(evaluation.get("future_max_move_points")),
            None if evaluation.get("future_reached_50_points") is None else bool_int(evaluation.get("future_reached_50_points")),
            None if evaluation.get("future_reached_target") is None else bool_int(evaluation.get("future_reached_target")),
            None if evaluation.get("future_hit_stop_first") is None else bool_int(evaluation.get("future_hit_stop_first")),
            nullable_int(evaluation.get("candles_to_reach_50")),
            nullable_int(evaluation.get("candles_evaluated")) or 0,
            evaluation.get("final_verdict") or "PENDING",
            row_id,
        ),
    )
    connection.commit()


def read_row_by_key(connection, audit_key: str) -> dict[str, Any] | None:
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT * FROM opportunity_audit WHERE audit_key = %s LIMIT 1", (audit_key,))
    return serialize_row(cursor.fetchone())


def record_candidate(connection, payload: dict[str, Any]) -> dict[str, Any]:
    init_table(connection)
    audit_key = str(payload.get("audit_key") or build_audit_key(payload))
    market_time = normalize_market_time(payload.get("market_time"))
    side = normalize_side(payload.get("signal_side"))
    if side not in {"CALL", "PUT"}:
        return {"ok": False, "error": "invalid_signal_side", "message": "Only CALL/PUT WAIT candidates are audited."}

    provided_verdict = str(payload.get("final_verdict") or "PENDING")
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO opportunity_audit
          (audit_key, market_time, symbol, timeframe, signal_side, pattern_name,
           entry_candidate, target_candidate, stop_candidate, confidence, pattern_confidence,
           oi_confidence, min_move_points, rejection_reasons, session_valid, premium_available,
           nse_confirmation_available, future_max_move_points, future_reached_50_points,
           future_reached_target, future_hit_stop_first, candles_to_reach_50, candles_evaluated,
           final_verdict)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          confidence = VALUES(confidence),
          pattern_confidence = VALUES(pattern_confidence),
          oi_confidence = VALUES(oi_confidence),
          min_move_points = VALUES(min_move_points),
          rejection_reasons = VALUES(rejection_reasons),
          session_valid = VALUES(session_valid),
          premium_available = VALUES(premium_available),
          nse_confirmation_available = VALUES(nse_confirmation_available),
          future_max_move_points = COALESCE(VALUES(future_max_move_points), future_max_move_points),
          future_reached_50_points = COALESCE(VALUES(future_reached_50_points), future_reached_50_points),
          future_reached_target = COALESCE(VALUES(future_reached_target), future_reached_target),
          future_hit_stop_first = COALESCE(VALUES(future_hit_stop_first), future_hit_stop_first),
          candles_to_reach_50 = COALESCE(VALUES(candles_to_reach_50), candles_to_reach_50),
          candles_evaluated = GREATEST(COALESCE(VALUES(candles_evaluated), 0), COALESCE(candles_evaluated, 0)),
          final_verdict = CASE
            WHEN VALUES(final_verdict) IS NOT NULL AND VALUES(final_verdict) <> 'PENDING'
            THEN VALUES(final_verdict)
            ELSE final_verdict
          END
        """,
        (
            audit_key,
            market_time,
            payload.get("symbol") or "NIFTY50",
            payload.get("timeframe") or "5m",
            side,
            payload.get("pattern_name") or "",
            nullable_number(payload.get("entry_candidate")),
            nullable_number(payload.get("target_candidate")),
            nullable_number(payload.get("stop_candidate")),
            nullable_int(payload.get("confidence")),
            nullable_int(payload.get("pattern_confidence")),
            nullable_int(payload.get("oi_confidence")),
            nullable_number(payload.get("min_move_points")),
            rejection_reasons_json(payload.get("rejection_reasons")),
            bool_int(payload.get("session_valid")),
            bool_int(payload.get("premium_available")),
            bool_int(payload.get("nse_confirmation_available")),
            nullable_number(payload.get("future_max_move_points")),
            None if payload.get("future_reached_50_points") is None else bool_int(payload.get("future_reached_50_points")),
            None if payload.get("future_reached_target") is None else bool_int(payload.get("future_reached_target")),
            None if payload.get("future_hit_stop_first") is None else bool_int(payload.get("future_hit_stop_first")),
            nullable_int(payload.get("candles_to_reach_50")),
            nullable_int(payload.get("candles_evaluated")) or 0,
            provided_verdict,
        ),
    )
    connection.commit()

    row = read_row_by_key(connection, audit_key)
    if row and row.get("final_verdict") == "PENDING":
        future = fetch_future_candles(connection, row)
        evaluation = evaluate_candidate(row, future)
        if evaluation.get("final_verdict") != "PENDING" or evaluation.get("candles_evaluated", 0) > int(row.get("candles_evaluated") or 0):
            update_evaluation_fields(connection, int(row["id"]), evaluation)
            row = read_row_by_key(connection, audit_key)

    return {"ok": True, "record": row, "created": cursor.rowcount == 1}


def update_pending(connection, limit: int = 500) -> dict[str, Any]:
    init_table(connection)
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT *
        FROM opportunity_audit
        WHERE final_verdict = 'PENDING'
        ORDER BY market_time ASC
        LIMIT %s
        """,
        (max(1, min(limit, 2000)),),
    )
    rows = [serialize_row(row) for row in cursor.fetchall() or []]
    updated = 0
    for row in rows:
        if not row:
            continue
        future = fetch_future_candles(connection, row)
        evaluation = evaluate_candidate(row, future)
        if evaluation.get("final_verdict") != "PENDING" or int(evaluation.get("candles_evaluated") or 0) > int(row.get("candles_evaluated") or 0):
            update_evaluation_fields(connection, int(row["id"]), evaluation)
            updated += 1
    return {"ok": True, "checked": len(rows), "updated": updated}


def read_history(connection, limit: int = 120) -> list[dict[str, Any]]:
    init_table(connection)
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT *
        FROM opportunity_audit
        ORDER BY market_time DESC, id DESC
        LIMIT %s
        """,
        (max(1, min(limit, 1000)),),
    )
    return [serialize_row(row) for row in cursor.fetchall() or []]


def read_debug_records(connection, *, market_date: str, verdict: str = "", limit: int = 500) -> list[dict[str, Any]]:
    init_table(connection)
    cursor = connection.cursor(dictionary=True)
    normalized_verdict = str(verdict or "").strip().upper()
    max_rows = max(1, min(limit, 2000))
    if normalized_verdict:
        cursor.execute(
            """
            SELECT *
            FROM opportunity_audit
            WHERE DATE(market_time) = %s
              AND final_verdict = %s
            ORDER BY market_time ASC, id ASC
            LIMIT %s
            """,
            (market_date, normalized_verdict, max_rows),
        )
    else:
        cursor.execute(
            """
            SELECT *
            FROM opportunity_audit
            WHERE DATE(market_time) = %s
            ORDER BY market_time ASC, id ASC
            LIMIT %s
            """,
            (market_date, max_rows),
        )
    return [serialize_row(row) for row in cursor.fetchall() or []]


def build_debug_record(row: dict[str, Any]) -> dict[str, Any]:
    reasons = parse_reasons(row.get("rejection_reasons"))
    actionable = actionable_reasons(reasons)
    return {
        "id": row.get("id"),
        "source_type": audit_source_type(row),
        "market_time": row.get("market_time"),
        "signal_time": compact_market_time(row.get("market_time")),
        "signal_side": normalize_side(row.get("signal_side")),
        "pattern_name": row.get("pattern_name"),
        "pattern_bucket": pattern_bucket(str(row.get("pattern_name") or "")),
        "entry_price": nullable_number(row.get("entry_candidate")),
        "target_candidate": nullable_number(row.get("target_candidate")),
        "stop_candidate": nullable_number(row.get("stop_candidate")),
        "confidence": nullable_int(row.get("confidence")),
        "pattern_confidence": nullable_int(row.get("pattern_confidence")),
        "oi_confidence": nullable_int(row.get("oi_confidence")),
        "projected_move_points": nullable_number(row.get("min_move_points")),
        "dynamic_min_move_points": MIN_MISSED_MOVE_POINTS,
        "rejection_reasons": reasons,
        "actionable_rejection_reasons": actionable,
        "session_valid": bool(row.get("session_valid")),
        "premium_available": bool(row.get("premium_available")),
        "nse_confirmation_available": bool(row.get("nse_confirmation_available")),
        "future_max_move_points": nullable_number(row.get("future_max_move_points")),
        "future_reached_50_points": bool(row.get("future_reached_50_points")),
        "future_reached_target": bool(row.get("future_reached_target")),
        "future_hit_stop_first": bool(row.get("future_hit_stop_first")),
        "candles_to_reach_50": nullable_int(row.get("candles_to_reach_50")),
        "candles_evaluated": nullable_int(row.get("candles_evaluated")),
        "final_verdict": row.get("final_verdict"),
        "created_at": row.get("created_at"),
    }


def summarize_debug_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    verdict_counts = Counter(str(row.get("final_verdict") or "PENDING") for row in records)
    source_counts = Counter(str(row.get("source_type") or "unknown") for row in records)
    reason_counts: Counter[str] = Counter()
    actionable_reason_counts: Counter[str] = Counter()
    missed_put_reason_counts: Counter[str] = Counter()
    pattern_counts: Counter[str] = Counter()
    pattern_bucket_counts: Counter[str] = Counter()
    move_bucket_counts: Counter[str] = Counter()
    stop_first_count = 0
    target_without_50_count = 0
    classification_errors: list[dict[str, Any]] = []

    for row in records:
        reasons = row.get("rejection_reasons") or []
        actionable = row.get("actionable_rejection_reasons") or []
        for reason in reasons:
            reason_counts[str(reason)] += 1
        for reason in actionable:
            actionable_reason_counts[str(reason)] += 1

        if row.get("final_verdict") == "MISSED_PUT":
            pattern_counts[str(row.get("pattern_name") or "Unknown")] += 1
            pattern_bucket_counts[str(row.get("pattern_bucket") or "Other")] += 1
            move_bucket_counts[move_bucket(row.get("future_max_move_points"))] += 1
            if row.get("future_hit_stop_first"):
                stop_first_count += 1
            if row.get("future_reached_target") and not row.get("future_reached_50_points"):
                target_without_50_count += 1
            for reason in actionable:
                missed_put_reason_counts[str(reason)] += 1

        if str(row.get("final_verdict") or "").startswith("MISSED_"):
            if not row.get("future_reached_50_points") or row.get("future_hit_stop_first"):
                classification_errors.append(
                    {
                        "id": row.get("id"),
                        "market_time": row.get("market_time"),
                        "final_verdict": row.get("final_verdict"),
                        "future_reached_50_points": row.get("future_reached_50_points"),
                        "future_hit_stop_first": row.get("future_hit_stop_first"),
                    }
                )

    return {
        "total": len(records),
        "verdict_counts": dict(verdict_counts),
        "source_counts": dict(source_counts),
        "reason_counts": dict(reason_counts),
        "actionable_reason_counts": dict(actionable_reason_counts),
        "missed_put_reason_counts": dict(missed_put_reason_counts),
        "missed_put_pattern_counts": dict(pattern_counts),
        "missed_put_pattern_bucket_counts": dict(pattern_bucket_counts),
        "missed_put_move_buckets": dict(move_bucket_counts),
        "missed_put_stop_first_count": stop_first_count,
        "missed_put_target_without_50_count": target_without_50_count,
        "classification_ok": len(classification_errors) == 0,
        "classification_errors": classification_errors,
    }


def debug_report(connection, *, market_date: str, verdict: str = "", limit: int = 500) -> dict[str, Any]:
    date.fromisoformat(market_date)
    update_pending(connection, limit=limit)
    raw_records = read_debug_records(connection, market_date=market_date, verdict=verdict, limit=limit)
    records = [build_debug_record(row) for row in raw_records]
    backend_replay_records = [row for row in records if row.get("source_type") == "backend_replay"]
    browser_or_live_records = [row for row in records if row.get("source_type") == "browser_or_live_audit"]
    return {
        "ok": True,
        "date": market_date,
        "verdict": str(verdict or "").strip().upper() or "ALL",
        "dynamic_min_move_points_current": MIN_MISSED_MOVE_POINTS,
        "summary": summarize_debug_records(records),
        "backend_replay_summary": summarize_debug_records(backend_replay_records),
        "browser_or_live_summary": summarize_debug_records(browser_or_live_records),
        "records": records,
        "backend_replay_records": backend_replay_records,
        "browser_or_live_records": browser_or_live_records,
    }


def summarize(connection, limit: int = 1000) -> dict[str, Any]:
    init_table(connection)
    update_pending(connection, limit=limit)
    rows = read_history(connection, limit=limit)
    total = len(rows)
    verdict_counts = Counter(str(row.get("final_verdict") or "PENDING") for row in rows)
    correct_wait = verdict_counts["GOOD_WAIT"] + verdict_counts["BAD_SIGNAL_BLOCKED"]
    missed_rows = [row for row in rows if row.get("final_verdict") in {"MISSED_CALL", "MISSED_PUT"}]
    completed = [row for row in rows if row.get("final_verdict") != "PENDING"]

    missed_reason_counts: Counter[str] = Counter()
    all_reason_counts: Counter[str] = Counter()
    missed_pattern_counts: Counter[str] = Counter()
    reason_by_verdict: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        reasons = parse_reasons(row.get("rejection_reasons"))
        actionable = actionable_reasons(reasons)
        for reason in actionable:
            all_reason_counts[reason] += 1
            reason_by_verdict[str(row.get("final_verdict") or "PENDING")][reason] += 1
        if row in missed_rows:
            missed_pattern_counts[str(row.get("pattern_name") or "Unknown")] += 1
            for reason in actionable:
                missed_reason_counts[reason] += 1

    top_missed_reason = missed_reason_counts.most_common(1)[0][0] if missed_reason_counts else "None yet"
    worst_gate = missed_reason_counts.most_common(1)[0][0] if missed_reason_counts else (all_reason_counts.most_common(1)[0][0] if all_reason_counts else "Collecting")
    best_missed_pattern = missed_pattern_counts.most_common(1)[0][0] if missed_pattern_counts else "None yet"

    oi_missing_misses = sum(
        count
        for reason, count in missed_reason_counts.items()
        if "option-chain" in reason.lower() or "nse" in reason.lower() or "oi" in reason.lower()
    )
    premium_missing_misses = sum(count for reason, count in missed_reason_counts.items() if "premium" in reason.lower())
    min_move_misses = sum(count for reason, count in missed_reason_counts.items() if "move" in reason.lower())

    reduce_oi_blocking = bool(len(missed_rows) >= 5 and oi_missing_misses / max(len(missed_rows), 1) >= 0.45)
    dynamic_min_move_points = 42 if len(missed_rows) >= 5 and min_move_misses / max(len(missed_rows), 1) >= 0.35 else MIN_MISSED_MOVE_POINTS

    return {
        "ok": True,
        "summary": {
            "total_wait_candidates": total,
            "completed_candidates": len(completed),
            "correct_wait_pct": round((correct_wait / len(completed)) * 100, 1) if completed else None,
            "missed_call_count": verdict_counts["MISSED_CALL"],
            "missed_put_count": verdict_counts["MISSED_PUT"],
            "top_rejection_reason_causing_missed_moves": top_missed_reason,
            "best_missed_pattern": best_missed_pattern,
            "worst_rejection_gate": worst_gate,
            "verdict_counts": dict(verdict_counts),
        },
        "recommendations": {
            "reduce_oi_blocking": reduce_oi_blocking,
            "premium_missing_blocks_trade_record_only": True,
            "dynamic_min_move_points": dynamic_min_move_points,
            "reason": (
                "OI blocking is relaxed only after repeated audited missed moves."
                if reduce_oi_blocking
                else "Collecting enough missed-opportunity evidence before changing live gates."
            ),
        },
        "recent_records": rows[:20],
    }


def main() -> None:
    args = parse_args()
    connection = connect_mysql()
    try:
        if args.command == "init":
            init_table(connection)
            output = {"ok": True, "table": "opportunity_audit"}
        elif args.command == "record":
            output = record_candidate(connection, json.loads(args.payload))
        elif args.command == "update-pending":
            output = update_pending(connection, args.limit)
        elif args.command == "summary":
            output = summarize(connection, args.limit)
        elif args.command == "history":
            output = {"ok": True, "records": read_history(connection, args.limit)}
        elif args.command == "debug":
            output = debug_report(connection, market_date=args.date, verdict=args.verdict, limit=args.limit)
        else:
            output = {"ok": False, "error": "unknown_command"}
    except Exception as exc:
        output = {"ok": False, "error": "opportunity_audit_failed", "message": str(exc)}
    finally:
        connection.close()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
