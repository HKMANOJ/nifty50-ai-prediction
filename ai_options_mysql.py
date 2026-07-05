#!/usr/bin/env python3
"""Persist AI option predictions for backtesting."""

from __future__ import annotations

import argparse
import json
import math
from typing import Any

from store_candles_mysql import connect_mysql


MINIMUM_TARGET_POINTS = 50.0
VALID_OPTION_SIDES = {"BUY CALL", "BUY PUT"}


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ai_options (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  signal_key VARCHAR(96) NOT NULL UNIQUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  symbol VARCHAR(32) NOT NULL DEFAULT 'NIFTY50',
  timeframe VARCHAR(8) NOT NULL DEFAULT '5m',
  option_side VARCHAR(16) NOT NULL,
  option_name VARCHAR(64),
  status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
  entry_price DECIMAL(12,2) NOT NULL,
  target_price DECIMAL(12,2) NOT NULL,
  stop_loss DECIMAL(12,2) NOT NULL,
  current_price DECIMAL(12,2),
  exit_price DECIMAL(12,2),
  confidence INT,
  pattern_name VARCHAR(128),
  reason TEXT,
  execution_quality VARCHAR(32),
  premium_entry DECIMAL(12,2),
  premium_target DECIMAL(12,2),
  premium_stop_loss DECIMAL(12,2),
  premium_current DECIMAL(12,2),
  premium_exit DECIMAL(12,2),
  pnl_premium DECIMAL(12,2),
  opened_market_time DATETIME,
  closed_market_time DATETIME,
  result_points DECIMAL(12,2),
  INDEX idx_ai_options_status (status, created_at),
  INDEX idx_ai_options_timeframe (symbol, timeframe, created_at)
)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage AI option prediction records.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create ai_options table if needed")

    latest = sub.add_parser("latest", help="Read latest option prediction")
    latest.add_argument("--status", default="OPEN")

    history = sub.add_parser("history", help="Read prediction history")
    history.add_argument("--status", default="ALL")
    history.add_argument("--option-side", default="ALL")
    history.add_argument("--limit", type=int, default=120)

    record = sub.add_parser("record", help="Insert or return an active option prediction")
    record.add_argument("--payload", required=True, help="JSON prediction payload")

    update = sub.add_parser("update", help="Update prediction status")
    update.add_argument("--id", type=int, required=True)
    update.add_argument("--status", required=True)
    update.add_argument("--current-price", type=float, required=True)
    update.add_argument("--result-points", type=float, default=0.0)
    update.add_argument("--premium-current", type=float, default=None)
    update.add_argument("--premium-exit", type=float, default=None)
    update.add_argument("--pnl-premium", type=float, default=None)
    update.add_argument("--closed-market-time", default="")
    return parser.parse_args()


def init_table(connection) -> None:
    cursor = connection.cursor()
    cursor.execute(CREATE_TABLE_SQL)
    for statement in (
        "ALTER TABLE ai_options ADD COLUMN execution_quality VARCHAR(32) NULL",
        "ALTER TABLE ai_options ADD COLUMN premium_entry DECIMAL(12,2) NULL",
        "ALTER TABLE ai_options ADD COLUMN premium_target DECIMAL(12,2) NULL",
        "ALTER TABLE ai_options ADD COLUMN premium_stop_loss DECIMAL(12,2) NULL",
        "ALTER TABLE ai_options ADD COLUMN premium_current DECIMAL(12,2) NULL",
        "ALTER TABLE ai_options ADD COLUMN premium_exit DECIMAL(12,2) NULL",
        "ALTER TABLE ai_options ADD COLUMN pnl_premium DECIMAL(12,2) NULL",
        "ALTER TABLE ai_options ADD COLUMN exit_price DECIMAL(12,2) NULL",
    ):
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
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat(sep=" ")
        elif isinstance(value, (bytes, bytearray)):
            result[key] = value.decode("utf-8", errors="replace")
        else:
            result[key] = float(value) if value.__class__.__name__ == "Decimal" else value
    return result


def nullable_number(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except TypeError:
        return None
    return value


def parse_required_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def validate_trade_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    side = str(payload.get("option_side", "")).upper().strip()
    if side not in VALID_OPTION_SIDES:
        return False, "invalid_option_side"

    entry = parse_required_number(payload.get("entry_price"))
    target = parse_required_number(payload.get("target_price"))
    stop_loss = parse_required_number(payload.get("stop_loss"))
    if entry is None or target is None or stop_loss is None:
        return False, "missing_trade_levels"

    target_distance = abs(target - entry)
    if target_distance < MINIMUM_TARGET_POINTS:
        return False, "target_below_50_points"

    if side == "BUY CALL" and not (target > entry and stop_loss < entry):
        return False, "invalid_call_target_or_stop"
    if side == "BUY PUT" and not (target < entry and stop_loss > entry):
        return False, "invalid_put_target_or_stop"

    return True, ""


def latest_open(connection, status: str = "OPEN") -> dict[str, Any] | None:
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT *
        FROM ai_options
        WHERE status = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (status,),
    )
    record = serialize_row(cursor.fetchone())
    if (status or "").upper() == "OPEN" and record:
        execution_quality = str(record.get("execution_quality") or "").lower()
        if execution_quality not in {"premium_live", "premium_missing"}:
            return None
        is_valid, _ = validate_trade_payload(record)
        if not is_valid:
            return None
    return record


def read_history(
    connection,
    *,
    status: str = "ALL",
    option_side: str = "ALL",
    limit: int = 120,
) -> list[dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    where_parts: list[str] = []
    params: list[Any] = []
    normalized_status = (status or "ALL").upper()
    normalized_side = (option_side or "ALL").upper()

    if normalized_status != "ALL":
      where_parts.append("status = %s")
      params.append(normalized_status)
    if normalized_side != "ALL":
      where_parts.append("option_side = %s")
      params.append(normalized_side)

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    params.append(max(1, min(limit, 500)))
    cursor.execute(
        f"""
        SELECT *
        FROM ai_options
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        tuple(params),
    )
    rows = cursor.fetchall() or []
    return [serialize_row(row) for row in rows if row]


def record_prediction(connection, payload: dict[str, Any]) -> dict[str, Any]:
    init_table(connection)
    is_valid, validation_error = validate_trade_payload(payload)
    if not is_valid:
        return {
            "ok": False,
            "created": False,
            "error": validation_error,
            "record": None,
        }

    active = latest_open(connection, "OPEN")
    if active:
        return {"ok": True, "created": False, "record": active}

    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO ai_options
          (signal_key, symbol, timeframe, option_side, option_name, status, entry_price,
           target_price, stop_loss, current_price, confidence, pattern_name, reason, execution_quality,
           premium_entry, premium_target, premium_stop_loss, premium_current, opened_market_time)
        VALUES
          (%s, %s, %s, %s, %s, 'OPEN', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          current_price = VALUES(current_price),
          premium_current = VALUES(premium_current),
          confidence = VALUES(confidence),
          reason = VALUES(reason),
          execution_quality = VALUES(execution_quality)
        """,
        (
            payload["signal_key"],
            payload.get("symbol", "NIFTY50"),
            payload.get("timeframe", "5m"),
            payload["option_side"],
            payload.get("option_name"),
            payload["entry_price"],
            payload["target_price"],
            payload["stop_loss"],
            payload.get("current_price"),
            payload.get("confidence"),
            payload.get("pattern_name"),
            payload.get("reason"),
            payload.get("execution_quality"),
            payload.get("premium_entry"),
            payload.get("premium_target"),
            payload.get("premium_stop_loss"),
            payload.get("premium_current"),
            payload.get("opened_market_time"),
        ),
    )
    connection.commit()
    return {"ok": True, "created": cursor.rowcount == 1, "record": latest_open(connection, "OPEN")}


def update_status(connection, args: argparse.Namespace) -> dict[str, Any]:
    init_table(connection)
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE ai_options
        SET status = %s,
            current_price = %s,
            exit_price = %s,
            result_points = %s,
            premium_current = %s,
            premium_exit = %s,
            pnl_premium = %s,
            closed_market_time = NULLIF(%s, '')
        WHERE id = %s
        """,
        (
            args.status,
            args.current_price,
            args.current_price,
            args.result_points,
            nullable_number(args.premium_current),
            nullable_number(args.premium_exit),
            nullable_number(args.pnl_premium),
            args.closed_market_time,
            args.id,
        ),
    )
    connection.commit()
    return {"ok": True, "updated": cursor.rowcount}


def main() -> None:
    args = parse_args()
    connection = connect_mysql()
    try:
        if args.command == "init":
            init_table(connection)
            output = {"ok": True, "table": "ai_options"}
        elif args.command == "latest":
            init_table(connection)
            output = {"ok": True, "record": latest_open(connection, args.status)}
        elif args.command == "history":
            init_table(connection)
            records = read_history(
                connection,
                status=args.status,
                option_side=args.option_side,
                limit=args.limit,
            )
            output = {"ok": True, "count": len(records), "records": records}
        elif args.command == "record":
            output = record_prediction(connection, json.loads(args.payload))
        elif args.command == "update":
            output = update_status(connection, args)
        else:
            output = {"ok": False, "error": "unknown_command"}
    finally:
        connection.close()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
