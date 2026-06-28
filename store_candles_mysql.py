#!/usr/bin/env python3
"""Store NIFTY intraday candles from inputs/nifty50_intraday.json into MySQL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mysql_config import mysql_public_settings, mysql_settings


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "inputs" / "nifty50_intraday.json"


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nifty_candles (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  symbol VARCHAR(32) NOT NULL,
  timeframe VARCHAR(8) NOT NULL,
  market_time DATETIME NOT NULL,
  market_date DATE NOT NULL,
  open DECIMAL(12,2) NOT NULL,
  high DECIMAL(12,2) NOT NULL,
  low DECIMAL(12,2) NOT NULL,
  close DECIMAL(12,2) NOT NULL,
  volume BIGINT DEFAULT 0,
  source VARCHAR(64),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_candle (symbol, timeframe, market_time),
  INDEX idx_candle_date (symbol, timeframe, market_date)
)
"""


INSERT_SQL = """
INSERT INTO nifty_candles
  (symbol, timeframe, market_time, market_date, open, high, low, close, volume, source)
VALUES
  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  open = VALUES(open),
  high = VALUES(high),
  low = VALUES(low),
  close = VALUES(close),
  volume = VALUES(volume),
  source = VALUES(source)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Store NIFTY candles into local MySQL.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Path to nifty50_intraday.json")
    parser.add_argument("--symbol", default="NIFTY50", help="Symbol to write into MySQL")
    parser.add_argument("--timeframes", default="5m", help="Comma-separated timeframes to sync")
    parser.add_argument("--skip-create", action="store_true", help="Do not create/check the nifty_candles table")
    return parser.parse_args()


def load_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Intraday candle file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def mysql_datetime(value: str | None) -> str:
    if not value:
        raise ValueError("Missing market_time")
    return value.replace("T", " ").split("+", 1)[0]


def normalized_rows(payload: dict[str, Any], *, symbol: str, timeframes: list[str]) -> list[tuple[Any, ...]]:
    source = payload.get("provider") or "unknown"
    series = payload.get("series") or {}
    rows: list[tuple[Any, ...]] = []
    for timeframe in timeframes:
        for candle in series.get(timeframe, []) or []:
            market_time = mysql_datetime(candle.get("market_time"))
            market_date = candle.get("market_date") or market_time[:10]
            rows.append(
                (
                    symbol,
                    timeframe,
                    market_time,
                    market_date,
                    float(candle["open"]),
                    float(candle["high"]),
                    float(candle["low"]),
                    float(candle["close"]),
                    int(candle.get("volume") or 0),
                    source,
                )
            )
    return rows


def connect_mysql():
    try:
        import mysql.connector  # type: ignore
        from mysql.connector import errorcode  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: install mysql-connector-python") from exc
    settings = mysql_settings()
    try:
        return mysql.connector.connect(**settings)
    except mysql.connector.Error as exc:
        if exc.errno != errorcode.ER_BAD_DB_ERROR:
            raise
        database = settings.pop("database")
        bootstrap = mysql.connector.connect(**settings)
        try:
            cursor = bootstrap.cursor()
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
            bootstrap.commit()
        finally:
            bootstrap.close()
        settings["database"] = database
        return mysql.connector.connect(**settings)


def main() -> None:
    args = parse_args()
    payload = load_payload(Path(args.input))
    timeframes = [item.strip() for item in args.timeframes.split(",") if item.strip()]
    rows = normalized_rows(payload, symbol=args.symbol, timeframes=timeframes)

    if not rows:
        print(json.dumps({"ok": False, "error": "no_candles", "message": "No candles found for requested timeframes."}, indent=2))
        raise SystemExit(1)

    connection = connect_mysql()
    try:
        cursor = connection.cursor()
        if not args.skip_create:
            cursor.execute(CREATE_TABLE_SQL)
        cursor.executemany(INSERT_SQL, rows)
        connection.commit()
        print(
            json.dumps(
                {
                    "ok": True,
                    "database": mysql_public_settings(),
                    "inserted_or_updated": cursor.rowcount,
                    "candles_seen": len(rows),
                    "timeframes": timeframes,
                },
                indent=2,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
