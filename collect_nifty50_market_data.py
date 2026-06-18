#!/usr/bin/env python3
"""Build a real-data-only NIFTY 50 market snapshot from local input files.

Workflow:

1. Download official or otherwise approved real source files listed in
   `nifty50_data_sources.json`.
2. Place them in the `inputs/` folder using the required filenames.
3. Run this script to normalize them into one snapshot JSON consumed by the UI.

This script refuses to emit output if any required real input is missing or
invalid. It never fabricates market values.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "inputs"
OUTPUT_PATH = ROOT / "market_snapshot.latest.json"
SOURCE_MANIFEST = ROOT / "nifty50_data_sources.json"


DATE_KEYS = ("date", "Date", "DATE", "Trade Date", "Index Date", "TIMESTAMP")
OPEN_KEYS = ("open", "Open", "OPEN", "Open Price")
HIGH_KEYS = ("high", "High", "HIGH", "High Price")
LOW_KEYS = ("low", "Low", "LOW", "Low Price")
CLOSE_KEYS = ("close", "Close", "CLOSE", "Closing Index Value", "Close Price", "last")
VIX_KEYS = ("close", "Close", "CLOSE", "India VIX", "Current VIX")
FII_KEYS = ("fii_net_crore", "FII Net", "FPI Net", "Net FII/FPI", "FII/FPI Net")
DII_KEYS = ("dii_net_crore", "DII Net", "Net DII")
CHANGE_PCT_KEYS = ("change_pct", "Change %", "% Change", "Percent Change")
LAST_KEYS = ("last", "Last", "LAST", "Close", "close")
USDINR_KEYS = ("reference_rate", "Reference Rate", "USDINR", "Rate", "Close")
REQUIRED_INPUT_FILES = (
    "nifty50_history.csv",
    "india_vix.csv",
    "fii_dii.csv",
    "gift_nifty.csv",
    "usdinr.csv",
    "world_signals.json",
)


@dataclass
class FileStatus:
    filename: str
    exists: bool
    rows: int = 0
    note: str = ""


class RealDataRequiredError(Exception):
    """Raised when the snapshot cannot be built from complete real inputs."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("Real market data is incomplete or invalid")
        self.payload = payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize NIFTY 50 market data into one snapshot JSON.")
    parser.add_argument(
        "--input-dir",
        default=str(INPUT_DIR),
        help="Directory containing input CSV/JSON files. Defaults to ./inputs",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
        help="Output JSON path. Defaults to ./market_snapshot.latest.json",
    )
    return parser.parse_args()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_sources() -> list[dict[str, Any]]:
    if not SOURCE_MANIFEST.exists():
        return []
    return read_json(SOURCE_MANIFEST).get("sources", [])


def first_value(row: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def parse_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    cleaned = (
        text.replace(",", "")
        .replace("%", "")
        .replace("₹", "")
        .replace("Rs.", "")
        .replace("Rs", "")
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None

    formats = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d %b %Y",
        "%Y/%m/%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader]


def sort_by_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: row["date"])


def round_or_none(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def classify_bias(value: float | None, bullish_at: float, bearish_at: float) -> str:
    if value is None:
        return "unknown"
    if value >= bullish_at:
        return "bullish"
    if value <= bearish_at:
        return "bearish"
    return "neutral"


def classify_inverse_bias(value: float | None, bullish_at: float, bearish_at: float) -> str:
    if value is None:
        return "unknown"
    if value <= bullish_at:
        return "bullish"
    if value >= bearish_at:
        return "bearish"
    return "neutral"


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / previous) * 100.0


def safe_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return mean(values)


def safe_stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return pstdev(values)


def compute_trailing_returns(closes: list[float]) -> list[float]:
    returns: list[float] = []
    for idx in range(1, len(closes)):
        prev_close = closes[idx - 1]
        cur_close = closes[idx]
        if prev_close:
            returns.append(((cur_close - prev_close) / prev_close) * 100.0)
    return returns


def load_price_history(path: Path) -> tuple[dict[str, Any] | None, FileStatus]:
    if not path.exists():
        return None, FileStatus(path.name, False, note="Missing NIFTY 50 history CSV")

    raw_rows = read_csv_rows(path)
    normalized: list[dict[str, Any]] = []
    for row in raw_rows:
        row_date = parse_date(first_value(row, DATE_KEYS))
        close = parse_float(first_value(row, CLOSE_KEYS))
        if row_date is None or close is None:
            continue
        normalized.append(
            {
                "date": row_date,
                "open": parse_float(first_value(row, OPEN_KEYS)),
                "high": parse_float(first_value(row, HIGH_KEYS)),
                "low": parse_float(first_value(row, LOW_KEYS)),
                "close": close,
            }
        )

    normalized = sort_by_date(normalized)
    if len(normalized) < 21:
        return None, FileStatus(path.name, True, rows=len(normalized), note="Need at least 21 valid history rows")

    latest = normalized[-1]
    previous = normalized[-2]
    closes = [row["close"] for row in normalized if row.get("close") is not None]
    returns = compute_trailing_returns(closes)

    return (
        {
            "latest_date": latest["date"].isoformat(),
            "last_close": round_or_none(latest["close"]),
            "previous_close": round_or_none(previous["close"]),
            "return_1d_pct": round_or_none(pct_change(latest["close"], previous["close"])),
            "sma_5": round_or_none(safe_mean(closes[-5:])),
            "sma_20": round_or_none(safe_mean(closes[-20:])),
            "volatility_20d_pct": round_or_none(safe_stdev(returns[-20:])),
            "open": round_or_none(latest.get("open")),
            "high": round_or_none(latest.get("high")),
            "low": round_or_none(latest.get("low")),
        },
        FileStatus(path.name, True, rows=len(normalized), note="Loaded price history"),
    )


def load_single_value_series(
    path: Path,
    value_keys: tuple[str, ...],
    note: str,
) -> tuple[dict[str, Any] | None, FileStatus]:
    if not path.exists():
        return None, FileStatus(path.name, False, note=f"Missing {note}")

    raw_rows = read_csv_rows(path)
    normalized: list[dict[str, Any]] = []
    for row in raw_rows:
        row_date = parse_date(first_value(row, DATE_KEYS))
        value = parse_float(first_value(row, value_keys))
        if row_date is None or value is None:
            continue
        normalized.append({"date": row_date, "value": value})

    normalized = sort_by_date(normalized)
    if len(normalized) < 2:
        return None, FileStatus(path.name, True, rows=len(normalized), note=f"Need at least 2 valid rows in {note}")

    latest = normalized[-1]
    previous = normalized[-2] if len(normalized) > 1 else None
    return (
        {
            "latest_date": latest["date"].isoformat(),
            "value": round_or_none(latest["value"]),
            "change_pct": round_or_none(pct_change(latest["value"], previous["value"])) if previous else None,
        },
        FileStatus(path.name, True, rows=len(normalized), note=f"Loaded {note}"),
    )


def load_flow_data(path: Path) -> tuple[dict[str, Any] | None, FileStatus]:
    if not path.exists():
        return None, FileStatus(path.name, False, note="Missing FII/DII CSV")

    raw_rows = read_csv_rows(path)
    normalized: list[dict[str, Any]] = []
    for row in raw_rows:
        row_date = parse_date(first_value(row, DATE_KEYS))
        fii = parse_float(first_value(row, FII_KEYS))
        dii = parse_float(first_value(row, DII_KEYS))
        if row_date is None or (fii is None and dii is None):
            continue
        normalized.append({"date": row_date, "fii": fii, "dii": dii})

    normalized = sort_by_date(normalized)
    if not normalized:
        return None, FileStatus(path.name, True, rows=0, note="No valid rows in FII/DII CSV")

    latest = normalized[-1]
    return (
        {
            "latest_date": latest["date"].isoformat(),
            "fii_net_crore": round_or_none(latest["fii"]),
            "dii_net_crore": round_or_none(latest["dii"]),
        },
        FileStatus(path.name, True, rows=len(normalized), note="Loaded FII/DII flows"),
    )


def load_gift_nifty(path: Path) -> tuple[dict[str, Any] | None, FileStatus]:
    if not path.exists():
        return None, FileStatus(path.name, False, note="Missing GIFT Nifty CSV")

    raw_rows = read_csv_rows(path)
    normalized: list[dict[str, Any]] = []
    for row in raw_rows:
        row_date = parse_date(first_value(row, DATE_KEYS))
        last_value = parse_float(first_value(row, LAST_KEYS))
        change_pct = parse_float(first_value(row, CHANGE_PCT_KEYS))
        if row_date is None and last_value is None and change_pct is None:
            continue
        normalized.append({"date": row_date, "last": last_value, "change_pct": change_pct})

    normalized = [row for row in normalized if row["date"] is not None]
    normalized = sort_by_date(normalized)
    if not normalized:
        return None, FileStatus(path.name, True, rows=0, note="No valid rows in GIFT Nifty CSV")

    latest = normalized[-1]
    if latest["change_pct"] is None:
        if len(normalized) < 2 or latest["last"] is None or normalized[-2]["last"] is None:
            return None, FileStatus(path.name, True, rows=len(normalized), note="Need change_pct or 2 valid last-price rows in GIFT Nifty CSV")
        latest_change_pct = pct_change(latest["last"], normalized[-2]["last"])
    else:
        latest_change_pct = latest["change_pct"]

    return (
        {
            "latest_date": latest["date"].isoformat(),
            "last": round_or_none(latest["last"]),
            "change_pct": round_or_none(latest_change_pct),
        },
        FileStatus(path.name, True, rows=len(normalized), note="Loaded GIFT Nifty"),
    )


def load_world_signals(path: Path) -> tuple[dict[str, Any] | None, FileStatus]:
    if not path.exists():
        return None, FileStatus(path.name, False, note="Missing world_signals.json")

    payload = read_json(path)
    missing_fields: list[str] = []
    if parse_float((payload.get("us_markets") or {}).get("change_pct")) is None:
        missing_fields.append("us_markets.change_pct")
    if parse_float((payload.get("brent") or {}).get("change_pct")) is None:
        missing_fields.append("brent.change_pct")
    if parse_float((payload.get("news_sentiment") or {}).get("india")) is None:
        missing_fields.append("news_sentiment.india")
    if parse_float((payload.get("news_sentiment") or {}).get("global")) is None:
        missing_fields.append("news_sentiment.global")
    if not isinstance(payload.get("geopolitical_factors"), list):
        missing_fields.append("geopolitical_factors")
    if not isinstance(payload.get("domestic_factors"), list):
        missing_fields.append("domestic_factors")
    if missing_fields:
        return None, FileStatus(path.name, True, rows=0, note="Missing required world_signals fields: " + ", ".join(missing_fields))
    return payload, FileStatus(path.name, True, rows=1, note="Loaded world/news signal bundle")


def load_intraday_data(path: Path) -> tuple[dict[str, Any] | None, FileStatus]:
    if not path.exists():
        return None, FileStatus(path.name, False, note="Missing optional intraday candle JSON")

    payload = read_json(path)
    normalized_series: dict[str, list[dict[str, Any]]] = {}
    total_rows = 0
    for timeframe in ("1m", "5m"):
        rows = (payload.get("series") or {}).get(timeframe) or []
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            timestamp = str(row.get("time_utc") or "").strip()
            market_time = str(row.get("market_time") or "").strip()
            market_date = str(row.get("market_date") or "").strip()
            open_value = parse_float(row.get("open"))
            high_value = parse_float(row.get("high"))
            low_value = parse_float(row.get("low"))
            close_value = parse_float(row.get("close"))
            volume_value = parse_float(row.get("volume"))
            if not timestamp or not market_date or None in (open_value, high_value, low_value, close_value):
                continue
            normalized_rows.append(
                {
                    "time_utc": timestamp,
                    "market_time": market_time or timestamp,
                    "market_date": market_date,
                    "open": round_or_none(open_value),
                    "high": round_or_none(high_value),
                    "low": round_or_none(low_value),
                    "close": round_or_none(close_value),
                    "volume": int(volume_value) if volume_value is not None else None,
                }
            )
        if normalized_rows:
            normalized_series[timeframe] = normalized_rows
            total_rows += len(normalized_rows)

    if not normalized_series:
        return None, FileStatus(path.name, True, rows=0, note="No valid intraday candle rows")

    day_summary = payload.get("day_summary") or {}
    return (
        {
            "provider": payload.get("provider") or "Yahoo Finance intraday chart API",
            "symbol": payload.get("symbol") or "^NSEI",
            "market_timezone": payload.get("market_timezone") or "Asia/Kolkata",
            "generated_at": payload.get("generated_at"),
            "as_of_utc": payload.get("as_of_utc") or normalized_series[next(iter(normalized_series))][-1]["time_utc"],
            "last_price": round_or_none(parse_float(payload.get("last_price"))),
            "previous_close": round_or_none(parse_float(payload.get("previous_close"))),
            "official_chart_svg_url": payload.get("official_chart_svg_url"),
            "official_summary_source": payload.get("official_summary_source"),
            "official_summary_error": payload.get("official_summary_error"),
            "day_summary": {
                "index": day_summary.get("index"),
                "last": round_or_none(parse_float(day_summary.get("last"))),
                "open": round_or_none(parse_float(day_summary.get("open"))),
                "high": round_or_none(parse_float(day_summary.get("high"))),
                "low": round_or_none(parse_float(day_summary.get("low"))),
                "previous_close": round_or_none(parse_float(day_summary.get("previous_close"))),
                "change": round_or_none(parse_float(day_summary.get("change"))),
                "change_pct": round_or_none(parse_float(day_summary.get("change_pct"))),
                "updated_at": day_summary.get("updated_at"),
                "chart_svg_url": day_summary.get("chart_svg_url"),
            },
            "series_meta": payload.get("series_meta") or {},
            "series": normalized_series,
            "notes": payload.get("notes") or [],
        },
        FileStatus(path.name, True, rows=total_rows, note="Loaded optional intraday candles"),
    )


def load_component_dashboard(path: Path) -> tuple[dict[str, Any] | None, FileStatus]:
    if not path.exists():
        return None, FileStatus(path.name, False, note="Missing optional NIFTY 50 component dashboard JSON")

    payload = read_json(path)
    normalized_stocks: list[dict[str, Any]] = []
    for row in payload.get("stocks") or []:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        normalized_stocks.append(
            {
                "symbol": symbol,
                "yahoo_symbol": str(row.get("yahoo_symbol") or f"{symbol}.NS").strip(),
                "company_name": str(row.get("company_name") or "").strip(),
                "sector": str(row.get("sector") or "").strip(),
                "last_price": round_or_none(parse_float(row.get("last_price"))),
                "previous_close": round_or_none(parse_float(row.get("previous_close"))),
                "day_change_pct": round_or_none(parse_float(row.get("day_change_pct"))),
                "raw_day_change_pct": round_or_none(parse_float(row.get("raw_day_change_pct"))),
                "session_open": round_or_none(parse_float(row.get("session_open"))),
                "intraday_change_pct": round_or_none(parse_float(row.get("intraday_change_pct"))),
                "opening_gap_pct": round_or_none(parse_float(row.get("opening_gap_pct"))),
                "day_high": round_or_none(parse_float(row.get("day_high"))),
                "day_low": round_or_none(parse_float(row.get("day_low"))),
                "ema9": round_or_none(parse_float(row.get("ema9"))),
                "ema21": round_or_none(parse_float(row.get("ema21"))),
                "range_position": round_or_none(parse_float(row.get("range_position")), 3),
                "volume": int(parse_float(row.get("volume"))) if parse_float(row.get("volume")) is not None else None,
                "bars_loaded": int(parse_float(row.get("bars_loaded"))) if parse_float(row.get("bars_loaded")) is not None else None,
                "signal": str(row.get("signal") or "neutral").strip().lower(),
                "score": round_or_none(parse_float(row.get("score")), 3),
                "momentum_rank": round_or_none(parse_float(row.get("momentum_rank")), 3),
                "opening_rank": round_or_none(parse_float(row.get("opening_rank")), 3),
                "volume_curve_average": round_or_none(parse_float(row.get("volume_curve_average"))),
                "volume_curve_average_m": round_or_none(parse_float(row.get("volume_curve_average_m"))),
                "volume_multiple": round_or_none(parse_float(row.get("volume_multiple")), 2),
                "market_pressure": round_or_none(parse_float(row.get("market_pressure")), 2),
                "previous_day_high": round_or_none(parse_float(row.get("previous_day_high"))),
                "previous_day_low": round_or_none(parse_float(row.get("previous_day_low"))),
                "level_status": str(row.get("level_status") or "--").strip().upper(),
                "break_direction": str(row.get("break_direction") or "").strip().lower() or None,
                "break_time_market": row.get("break_time_market"),
                "break_minutes": int(parse_float(row.get("break_minutes"))) if parse_float(row.get("break_minutes")) is not None else None,
                "break_label": str(row.get("break_label") or "--").strip(),
                "session_momentum": str(row.get("session_momentum") or "Neutral").strip(),
                "session_trend_label": str(row.get("session_trend_label") or "Balanced Session").strip(),
                "momentum_tags": [str(tag).strip().upper() for tag in (row.get("momentum_tags") or []) if str(tag).strip()],
                "signal_arrow": str(row.get("signal_arrow") or "flat").strip().lower(),
                "action_bias": str(row.get("action_bias") or "WAIT").strip().upper(),
                "corporate_action_adjusted": bool(row.get("corporate_action_adjusted")),
                "last_bar_time_utc": row.get("last_bar_time_utc"),
                "last_bar_market_time": row.get("last_bar_market_time"),
                "session_date": row.get("session_date"),
            }
        )

    if not normalized_stocks:
        return None, FileStatus(path.name, True, rows=0, note="No valid component-stock rows")

    stock_by_symbol = {row["symbol"]: row for row in normalized_stocks}
    tracked_symbols = [
        str(symbol).strip()
        for symbol in (payload.get("tracked_symbols") or ["RELIANCE", "HDFCBANK", "ICICIBANK"])
        if str(symbol).strip()
    ]

    return (
        {
            "provider": payload.get("provider") or "Nifty Indices constituent CSV + Yahoo Finance public chart API",
            "universe": payload.get("universe") or "NIFTY 50",
            "generated_at": payload.get("generated_at"),
            "coverage": {
                "requested": int(parse_float((payload.get("coverage") or {}).get("requested")) or len(normalized_stocks)),
                "loaded": int(parse_float((payload.get("coverage") or {}).get("loaded")) or len(normalized_stocks)),
                "failed": int(parse_float((payload.get("coverage") or {}).get("failed")) or 0),
            },
            "market_summary": {
                "bullish": int(parse_float((payload.get("market_summary") or {}).get("bullish")) or len([row for row in normalized_stocks if row.get("signal") == "bullish"])),
                "bearish": int(parse_float((payload.get("market_summary") or {}).get("bearish")) or len([row for row in normalized_stocks if row.get("signal") == "bearish"])),
                "neutral": int(parse_float((payload.get("market_summary") or {}).get("neutral")) or len([row for row in normalized_stocks if row.get("signal") == "neutral"])),
            },
            "tracked_symbols": tracked_symbols,
            "heavyweights": [stock_by_symbol[symbol] for symbol in tracked_symbols if symbol in stock_by_symbol],
            "ticker_strip": payload.get("ticker_strip") or [],
            "top_momentum_bullish": payload.get("top_momentum_bullish") or [],
            "top_momentum_bearish": payload.get("top_momentum_bearish") or [],
            "opening_momentum_bullish": payload.get("opening_momentum_bullish") or [],
            "opening_momentum_bearish": payload.get("opening_momentum_bearish") or [],
            "top_bullish": sorted(normalized_stocks, key=lambda row: row.get("score") or 0.0, reverse=True)[:10],
            "top_bearish": sorted(normalized_stocks, key=lambda row: row.get("score") or 0.0)[:10],
            "stocks": normalized_stocks,
            "failures": payload.get("failures") or [],
            "notes": payload.get("notes") or [],
        },
        FileStatus(path.name, True, rows=len(normalized_stocks), note="Loaded optional component-stock dashboard"),
    )


def source_lookup() -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in load_sources() if "id" in item}


def build_sources_used(
    files: dict[str, FileStatus],
    lookup: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    mapping = {
        "nifty50_history.csv": "nse_nifty50_history",
        "india_vix.csv": "nse_india_vix",
        "fii_dii.csv": "nse_fii_dii",
        "gift_nifty.csv": "nseix_gift_nifty",
        "usdinr.csv": "rbi_fbil_reference_rates",
        "world_signals.json": "world_signals_bundle",
        "nifty50_intraday.json": "yahoo_nsei_intraday",
        "nifty50_components_live.json": "nifty50_component_dashboard",
    }

    sources_used: list[dict[str, Any]] = []
    for filename, file_status in files.items():
        source_id = mapping.get(filename)
        source_info = lookup.get(source_id, {})
        sources_used.append(
            {
                "id": source_id or filename,
                "name": source_info.get("provider", filename),
                "status": "loaded" if file_status.exists else "missing",
                "rows": file_status.rows,
                "note": file_status.note,
                "url": source_info.get("url"),
            }
        )
    return sources_used


def raise_if_inputs_incomplete(
    *,
    file_statuses: dict[str, FileStatus],
    lookup: dict[str, dict[str, Any]],
    datasets: dict[str, Any],
    input_dir: Path,
) -> None:
    blocking_inputs = [filename for filename, dataset in datasets.items() if dataset is None]
    if not blocking_inputs:
        return

    payload = {
        "ok": False,
        "error": "real_data_required",
        "message": "Collector stopped because one or more required real inputs are missing or invalid.",
        "input_dir": str(input_dir),
        "required_inputs": list(REQUIRED_INPUT_FILES),
        "blocking_inputs": blocking_inputs,
        "sources_used": build_sources_used(file_statuses, lookup),
    }
    raise RealDataRequiredError(payload)


def build_snapshot_from_inputs(input_dir: Path) -> dict[str, Any]:
    price_history, price_status = load_price_history(input_dir / "nifty50_history.csv")
    vix, vix_status = load_single_value_series(input_dir / "india_vix.csv", VIX_KEYS, "India VIX CSV")
    flows, flow_status = load_flow_data(input_dir / "fii_dii.csv")
    gift_nifty, gift_status = load_gift_nifty(input_dir / "gift_nifty.csv")
    usdinr, usdinr_status = load_single_value_series(input_dir / "usdinr.csv", USDINR_KEYS, "USD/INR CSV")
    world_signals, world_status = load_world_signals(input_dir / "world_signals.json")
    intraday, intraday_status = load_intraday_data(input_dir / "nifty50_intraday.json")
    stock_dashboard, stock_dashboard_status = load_component_dashboard(input_dir / "nifty50_components_live.json")

    file_statuses = {
        price_status.filename: price_status,
        vix_status.filename: vix_status,
        flow_status.filename: flow_status,
        gift_status.filename: gift_status,
        usdinr_status.filename: usdinr_status,
        world_status.filename: world_status,
    }
    if intraday_status.exists and intraday is not None:
        file_statuses[intraday_status.filename] = intraday_status
    if stock_dashboard_status.exists and stock_dashboard is not None:
        file_statuses[stock_dashboard_status.filename] = stock_dashboard_status

    lookup = source_lookup()
    datasets = {
        "nifty50_history.csv": price_history,
        "india_vix.csv": vix,
        "fii_dii.csv": flows,
        "gift_nifty.csv": gift_nifty,
        "usdinr.csv": usdinr,
        "world_signals.json": world_signals,
    }
    raise_if_inputs_incomplete(
        file_statuses=file_statuses,
        lookup=lookup,
        datasets=datasets,
        input_dir=input_dir,
    )

    us_markets_change = parse_float((world_signals.get("us_markets") or {}).get("change_pct"))
    brent_change = parse_float((world_signals.get("brent") or {}).get("change_pct"))
    india_news_score = parse_float((world_signals.get("news_sentiment") or {}).get("india"))
    global_news_score = parse_float((world_signals.get("news_sentiment") or {}).get("global"))
    geopolitical_factors = world_signals.get("geopolitical_factors") or []
    domestic_factors = world_signals.get("domestic_factors") or []
    headline_groups = world_signals.get("headlines") or {}
    news_sources = world_signals.get("news_sources") or {}
    provider_failures = world_signals.get("provider_failures") or {}

    global_signals = [
        {
            "source": "US Markets",
            "signal": classify_bias(us_markets_change, 0.2, -0.2),
            "detail": "Derived from real world_signals.json US market change input.",
        },
        {
            "source": "GIFT Nifty",
            "signal": classify_bias((gift_nifty or {}).get("change_pct"), 0.2, -0.2),
            "detail": "Uses the latest downloaded GIFT Nifty change percentage.",
        },
        {
            "source": "Brent Crude",
            "signal": classify_inverse_bias(brent_change, -0.2, 0.2),
            "detail": "Lower crude is treated as supportive for India.",
        },
        {
            "source": "USD/INR",
            "signal": classify_inverse_bias((usdinr or {}).get("change_pct"), -0.1, 0.1),
            "detail": "A stronger rupee or softer dollar is treated as supportive.",
        },
        {
            "source": "FII Activity",
            "signal": classify_bias((flows or {}).get("fii_net_crore"), 300.0, -300.0),
            "detail": "Foreign investor buying supports the next-session bias.",
        },
        {
            "source": "DII Activity",
            "signal": classify_bias((flows or {}).get("dii_net_crore"), 300.0, -300.0),
            "detail": "Domestic investor buying acts as a stabilizer.",
        },
    ]

    return {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "as_of_date": price_history["latest_date"] or date.today().isoformat(),
            "data_mode": "real",
            "completeness": 1.0,
            "notes": [
                "Snapshot built from real input files placed in ./inputs.",
                "No synthetic or fallback market data was used.",
            ],
        },
        "market": {
            "last_close": price_history.get("last_close"),
            "previous_close": price_history.get("previous_close"),
            "return_1d_pct": price_history.get("return_1d_pct"),
            "sma_5": price_history.get("sma_5"),
            "sma_20": price_history.get("sma_20"),
            "volatility_20d_pct": price_history.get("volatility_20d_pct"),
        },
        "numeric_signals": {
            "gift_nifty_change_pct": gift_nifty.get("change_pct"),
            "india_vix_change_pct": vix.get("change_pct"),
            "fii_net_crore": flows.get("fii_net_crore"),
            "dii_net_crore": flows.get("dii_net_crore"),
            "usd_inr_change_pct": usdinr.get("change_pct"),
            "us_markets_change_pct": us_markets_change,
            "brent_change_pct": brent_change,
            "news_sentiment_india": india_news_score,
            "news_sentiment_global": global_news_score,
        },
        "global_signals": global_signals,
        "geopolitical_factors": geopolitical_factors,
        "domestic_factors": domestic_factors,
        "news": {
            "headlines": {
                "geopolitical": (headline_groups.get("global_risk") or [])[:10],
                "india_market": (headline_groups.get("india_market") or [])[:10],
            },
            "sources": news_sources,
            "provider_failures": provider_failures,
        },
        "intraday": intraday,
        "stock_dashboard": stock_dashboard,
        "missing_inputs": [],
        "sources_used": build_sources_used(file_statuses, lookup),
    }


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_path = Path(args.output)
    try:
        snapshot = build_snapshot_from_inputs(input_dir)
    except RealDataRequiredError as exc:
        if output_path.exists():
            output_path.unlink()
        print(json.dumps(exc.payload, indent=2))
        raise SystemExit(1) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, snapshot)

    status_line = {
        "ok": True,
        "output": str(output_path),
        "data_mode": snapshot["meta"]["data_mode"],
        "as_of_date": snapshot["meta"]["as_of_date"],
        "missing_inputs": snapshot.get("missing_inputs", []),
    }
    print(json.dumps(status_line, indent=2))


if __name__ == "__main__":
    main()
