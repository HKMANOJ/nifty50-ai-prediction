#!/usr/bin/env python3
"""Read historical NIFTY candles from local MySQL as JSON."""

from __future__ import annotations

import argparse
import json
from typing import Any

from store_candles_mysql import connect_mysql


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch candles from local MySQL.")
    parser.add_argument("--symbol", default="NIFTY50")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--date", dest="market_date", default="")
    parser.add_argument("--limit", type=int, default=500)
    return parser.parse_args()


def row_to_candle(row: dict[str, Any]) -> dict[str, Any]:
    market_time = row["market_time"]
    market_date = row["market_date"]
    return {
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "market_time": market_time.isoformat(sep=" ") if hasattr(market_time, "isoformat") else str(market_time),
        "market_date": market_date.isoformat() if hasattr(market_date, "isoformat") else str(market_date),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": int(row.get("volume") or 0),
        "source": row.get("source"),
    }


def main() -> None:
    args = parse_args()
    limit = max(1, min(args.limit, 5000))
    params: list[Any] = [args.symbol, args.timeframe]
    where = "symbol = %s AND timeframe = %s"
    if args.market_date:
        where += " AND market_date = %s"
        params.append(args.market_date)
    params.append(limit)

    sql = f"""
    SELECT symbol, timeframe, market_time, market_date, open, high, low, close, volume, source
    FROM nifty_candles
    WHERE {where}
    ORDER BY market_time DESC
    LIMIT %s
    """

    connection = connect_mysql()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = [row_to_candle(row) for row in cursor.fetchall()]
    finally:
        connection.close()

    rows.reverse()
    print(
        json.dumps(
            {
                "ok": True,
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "market_date": args.market_date or None,
                "count": len(rows),
                "candles": rows,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
