#!/usr/bin/env python3
"""Download the real input files required by the NIFTY 50 collector.

This script fetches only live upstream data and writes the exact files expected
by `collect_nifty50_market_data.py`.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nifty_pattern_detector import detect_patterns


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "inputs"
BUILD_WORLD_SIGNALS = ROOT / "build_world_signals.py"
NSE_BASE = "https://www.nseindia.com"
YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_NSEI_SYMBOL = "%5ENSEI"
NIFTY50_CONSTITUENTS_CSV = "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv"
USER_AGENT = "Mozilla/5.0"
RECENT_NSE_WINDOW_DAYS = 60
INDIA_TZ = ZoneInfo("Asia/Kolkata")
COMPONENT_CHART_WORKERS = 8
COMPONENT_WATCHLIST = ("RELIANCE", "HDFCBANK", "ICICIBANK")


class DownloadError(RuntimeError):
    """Raised when a required live source cannot be downloaded or parsed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download real market inputs for the NIFTY 50 app.")
    parser.add_argument("--input-dir", default=str(INPUT_DIR), help="Output directory for the required input files")
    parser.add_argument("--from-date", default=(date.today() - timedelta(days=RECENT_NSE_WINDOW_DAYS)).isoformat(), help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", default=date.today().isoformat(), help="End date in YYYY-MM-DD")
    parser.add_argument("--skip-world-signals", action="store_true", help="Skip building inputs/world_signals.json")
    parser.add_argument("--world-timespan", default="3days", help="Timespan passed to build_world_signals.py")
    parser.add_argument("--world-maxrecords", type=int, default=50, help="Max records passed to build_world_signals.py")
    return parser.parse_args()


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_nse_date(value: str) -> date:
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise DownloadError(f"Unsupported NSE date format: {value!r}")


def parse_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    cleaned = text.replace(",", "").replace("%", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def iso_date(value: str) -> str:
    return parse_nse_date(value).isoformat()


def nse_range_date(value: date) -> str:
    return value.strftime("%d-%m-%Y")


def run_curl(url: str, *, referer: str | None = None, accept: str = "application/json, text/plain, */*") -> str:
    cmd = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "-A",
        USER_AGENT,
        "-H",
        f"Accept: {accept}",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
    ]
    if referer:
        cmd.extend(["-e", referer, "-H", f"Referer: {referer}"])
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise DownloadError(f"curl failed for {url}: {stderr}")
    return result.stdout


def run_curl_with_cookies(url: str, cookie_file: str, *, save_cookies: bool = False, referer: str | None = None, accept: str = "application/json, text/plain, */*") -> str:
    cmd = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "-A",
        USER_AGENT,
        "-H",
        f"Accept: {accept}",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
    ]
    if save_cookies:
        cmd.extend(["-c", cookie_file])
    else:
        cmd.extend(["-b", cookie_file])
        
    if referer:
        cmd.extend(["-e", referer, "-H", f"Referer: {referer}"])
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise DownloadError(f"curl with cookies failed for {url}: {stderr}")
    return result.stdout


def fetch_text_direct(url: str, *, referer: str | None = None, accept: str = "application/json, text/plain, */*") -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def fetch_json_direct(url: str, *, referer: str | None = None) -> Any:
    return json.loads(fetch_text_direct(url, referer=referer))


def fetch_json(url: str, *, referer: str | None = None) -> Any:
    return json.loads(run_curl(url, referer=referer))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def latest_row_is_stale(rows: list[dict[str, Any]], to_date: date) -> bool:
    if not rows:
        return True
    latest = parse_iso_date(rows[-1]["date"])
    return latest < (to_date - timedelta(days=7))


def extract_json_object(html: str, marker: str) -> dict[str, Any]:
    marker_index = html.find(marker)
    if marker_index < 0:
        raise DownloadError(f"Could not find marker {marker!r}")

    start = html.find("{", marker_index)
    if start < 0:
        raise DownloadError(f"Could not find JSON object start for marker {marker!r}")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:index + 1])

    raise DownloadError(f"Could not find JSON object end for marker {marker!r}")


def round_or_none(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / previous) * 100.0


def compute_ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    multiplier = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = ((value - ema) * multiplier) + ema
    return ema


def average(values: list[float]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def session_clock(iso_value: str | None) -> str | None:
    if not iso_value:
        return None
    try:
        return datetime.fromisoformat(iso_value).astimezone(INDIA_TZ).strftime("%H:%M:%S")
    except ValueError:
        return None


def split_sessions(points: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        market_date = str(point.get("market_date") or "").strip()
        if not market_date:
            continue
        grouped.setdefault(market_date, []).append(point)
    return [(market_date, grouped[market_date]) for market_date in sorted(grouped)]


def first_level_break(
    points: list[dict[str, Any]],
    level: float | None,
    *,
    direction: str,
) -> dict[str, Any] | None:
    if level is None:
        return None
    for point in points:
        high_value = parse_float(point.get("high"))
        low_value = parse_float(point.get("low"))
        if direction == "up" and high_value is not None and high_value >= level:
            return point
        if direction == "down" and low_value is not None and low_value <= level:
            return point
    return None


def minutes_from_session_open(points: list[dict[str, Any]], iso_value: str | None) -> int | None:
    if not points or not iso_value:
        return None
    try:
        start_time = datetime.fromisoformat(str(points[0]["market_time"]))
        event_time = datetime.fromisoformat(iso_value)
    except (KeyError, TypeError, ValueError):
        return None
    elapsed = int((event_time - start_time).total_seconds() // 60)
    return max(elapsed, 0)


def session_momentum_label(score: float) -> tuple[str, str]:
    if score >= 1.0:
        return "Strong Bull", "Medium -> Strong (Bull)"
    if score >= 0.45:
        return "Medium Bull", "Open -> Trend (Bull)"
    if score <= -1.0:
        return "Strong Bear", "Medium -> Strong (Bear)"
    if score <= -0.45:
        return "Medium Bear", "Open -> Trend (Bear)"
    return "Neutral", "Balanced Session"


def build_session_tags(
    *,
    opening_gap_pct: float | None,
    intraday_change_pct: float | None,
    range_position: float | None,
    ema9: float | None,
    ema21: float | None,
    last_price: float | None,
    breakout_direction: str | None,
) -> list[str]:
    tags: list[str] = []
    if opening_gap_pct is not None and abs(opening_gap_pct) >= 0.35:
        tags.append("PRE")
    if intraday_change_pct is not None and abs(intraday_change_pct) >= 0.75:
        tags.append("OPEN")
    trend_up = last_price is not None and ema9 is not None and ema21 is not None and last_price >= ema9 and ema9 >= ema21
    trend_down = last_price is not None and ema9 is not None and ema21 is not None and last_price <= ema9 and ema9 <= ema21
    if breakout_direction or trend_up or trend_down or (range_position is not None and abs(range_position - 0.5) >= 0.22):
        tags.append("TREND")
    return tags


def download_nifty_history(input_dir: Path, from_date: date, to_date: date) -> int:
    def fetch_window(window_from: date) -> list[dict[str, Any]]:
        url = (
            f"{NSE_BASE}/api/historicalOR/indicesHistory?"
            f"indexType=NIFTY%2050&from={nse_range_date(window_from)}&to={nse_range_date(to_date)}"
        )
        payload = fetch_json(url, referer=f"{NSE_BASE}/reports-indices-historical-index-data")
        rows = payload.get("data") or []
        normalized = [
            {
                "date": iso_date(row["EOD_TIMESTAMP"]),
                "open": row["EOD_OPEN_INDEX_VAL"],
                "high": row["EOD_HIGH_INDEX_VAL"],
                "low": row["EOD_LOW_INDEX_VAL"],
                "close": row["EOD_CLOSE_INDEX_VAL"],
            }
            for row in rows
            if row.get("EOD_TIMESTAMP") and row.get("EOD_CLOSE_INDEX_VAL") is not None
        ]
        normalized.sort(key=lambda row: row["date"])
        return normalized

    normalized = fetch_window(from_date)
    if latest_row_is_stale(normalized, to_date):
        normalized = fetch_window(to_date - timedelta(days=RECENT_NSE_WINDOW_DAYS))
    if len(normalized) < 21:
        raise DownloadError("NIFTY 50 history returned fewer than 21 usable rows")
    write_csv(input_dir / "nifty50_history.csv", ["date", "open", "high", "low", "close"], normalized)
    return len(normalized)


def download_india_vix(input_dir: Path, from_date: date, to_date: date) -> int:
    def fetch_window(window_from: date) -> list[dict[str, Any]]:
        url = f"{NSE_BASE}/api/historicalOR/vixhistory?from={nse_range_date(window_from)}&to={nse_range_date(to_date)}"
        payload = fetch_json(url, referer=f"{NSE_BASE}/reports-indices-historical-vix")
        rows = payload.get("data") or []
        normalized = [
            {
                "date": iso_date(row["EOD_TIMESTAMP"]),
                "close": row["EOD_CLOSE_INDEX_VAL"],
            }
            for row in rows
            if row.get("EOD_TIMESTAMP") and row.get("EOD_CLOSE_INDEX_VAL") is not None
        ]
        normalized.sort(key=lambda row: row["date"])
        return normalized

    normalized = fetch_window(from_date)
    if latest_row_is_stale(normalized, to_date):
        normalized = fetch_window(to_date - timedelta(days=RECENT_NSE_WINDOW_DAYS))
    if len(normalized) < 2:
        raise DownloadError("India VIX history returned fewer than 2 usable rows")
    write_csv(input_dir / "india_vix.csv", ["date", "close"], normalized)
    return len(normalized)


def download_fii_dii(input_dir: Path) -> int:
    url = f"{NSE_BASE}/api/fiidiiTradeReact?csv=true"
    csv_text = run_curl(url, referer=f"{NSE_BASE}/reports/fii-dii/", accept="text/csv, */*")
    reader = csv.reader(csv_text.splitlines())
    rows = list(reader)
    if len(rows) < 2:
        raise DownloadError("FII/DII CSV did not contain usable rows")

    merged: dict[str, dict[str, Any]] = {}
    for row in rows[1:]:
        if len(row) < 5:
            continue
        category = "".join(row[0].split()).upper()
        trade_date = row[1].strip().strip('"')
        net_value = row[4].strip().strip('"').replace(",", "")
        if not trade_date or not net_value:
            continue

        iso_trade_date = iso_date(trade_date)
        merged.setdefault(iso_trade_date, {"date": iso_trade_date, "fii_net_crore": "", "dii_net_crore": ""})
        if category.startswith("FII") or "FPI" in category:
            merged[iso_trade_date]["fii_net_crore"] = net_value
        elif category == "DII":
            merged[iso_trade_date]["dii_net_crore"] = net_value

    normalized = sorted(merged.values(), key=lambda row: row["date"])
    if not normalized:
        raise DownloadError("FII/DII CSV could not be normalized into collector format")
    write_csv(input_dir / "fii_dii.csv", ["date", "fii_net_crore", "dii_net_crore"], normalized)
    return len(normalized)


def download_gift_nifty(input_dir: Path) -> int:
    gift: dict[str, Any] = {}
    try:
        market_status = fetch_json(
            f"{NSE_BASE}/api/marketStatus",
            referer=f"{NSE_BASE}/reports-indices-historical-index-data",
        )
        gift = market_status.get("giftnifty") or {}
    except DownloadError:
        url = f"{NSE_BASE}/reports-indices-historical-index-data"
        html = run_curl(url, referer=f"{NSE_BASE}/")
        header_data = extract_json_object(html, "window.headerData = ")
        gift = ((header_data.get("marketStatus") or {}).get("giftnifty") or {})

    timestamp = str(gift.get("TIMESTMP", "")).strip()
    last_price = gift.get("LASTPRICE")
    change_pct = gift.get("PERCHANGE")
    if not timestamp or last_price in (None, "") or change_pct in (None, ""):
        raise DownloadError("Could not extract live GIFT Nifty data from NSE page header")

    trade_date = timestamp.split()[0]
    normalized = [{
        "date": iso_date(trade_date),
        "last": last_price,
        "change_pct": change_pct,
    }]
    write_csv(input_dir / "gift_nifty.csv", ["date", "last", "change_pct"], normalized)
    return len(normalized)


def download_usdinr(input_dir: Path, from_date: date, to_date: date) -> int:
    url = (
        f"{NSE_BASE}/api/historicalOR/rbi-reference-rate-stats?"
        f"from={nse_range_date(from_date)}&to={nse_range_date(to_date)}"
    )
    payload = fetch_json(url, referer=f"{NSE_BASE}/report-detail/rbi-reference-rate-statistics")
    rows = payload.get("data") or []
    normalized = [
        {
            "date": iso_date(row["TRADE_DATE"]),
            "reference_rate": row["USDINR"],
        }
        for row in rows
        if row.get("TRADE_DATE") and row.get("USDINR") is not None
    ]
    normalized.sort(key=lambda row: row["date"])
    if len(normalized) < 2:
        raise DownloadError("USD/INR reference-rate history returned fewer than 2 usable rows")
    write_csv(input_dir / "usdinr.csv", ["date", "reference_rate"], normalized)
    return len(normalized)


def fetch_nifty_live_summary() -> dict[str, Any]:
    payload = fetch_json(f"{NSE_BASE}/api/allIndices", referer=f"{NSE_BASE}/market-data/live-market-indices")
    for row in payload.get("data") or []:
        if str(row.get("index", "")).strip().upper() != "NIFTY 50":
            continue
        return {
            "index": row.get("index"),
            "last": round_or_none(parse_float(row.get("last"))),
            "open": round_or_none(parse_float(row.get("open"))),
            "high": round_or_none(parse_float(row.get("high"))),
            "low": round_or_none(parse_float(row.get("low"))),
            "previous_close": round_or_none(parse_float(row.get("variation"))),
            "change": round_or_none(parse_float(row.get("variation"))),
            "change_pct": round_or_none(parse_float(row.get("percentChange"))),
            "updated_at": row.get("lastUpdateTime"),
            "chart_svg_url": row.get("chartTodayPath"),
        }
    raise DownloadError("Could not locate NIFTY 50 in NSE allIndices live summary")


def normalize_yahoo_candles(payload: dict[str, Any], *, label: str) -> dict[str, Any]:
    result = (((payload.get("chart") or {}).get("result")) or [])
    if not result:
        raise DownloadError(f"Yahoo Finance returned no chart result for {label}")

    chart = result[0]
    timestamps = chart.get("timestamp") or []
    quote = (((chart.get("indicators") or {}).get("quote")) or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    meta = chart.get("meta") or {}

    points: list[dict[str, Any]] = []
    for idx, timestamp in enumerate(timestamps):
        open_value = parse_float(opens[idx]) if idx < len(opens) else None
        high_value = parse_float(highs[idx]) if idx < len(highs) else None
        low_value = parse_float(lows[idx]) if idx < len(lows) else None
        close_value = parse_float(closes[idx]) if idx < len(closes) else None
        volume_value = parse_float(volumes[idx]) if idx < len(volumes) else None
        if None in (open_value, high_value, low_value, close_value):
            continue

        candle_utc = datetime.fromtimestamp(int(timestamp), tz=ZoneInfo("UTC"))
        candle_ist = candle_utc.astimezone(INDIA_TZ)
        points.append(
            {
                "time_utc": candle_utc.isoformat(),
                "market_time": candle_ist.isoformat(),
                "market_date": candle_ist.date().isoformat(),
                "open": round_or_none(open_value),
                "high": round_or_none(high_value),
                "low": round_or_none(low_value),
                "close": round_or_none(close_value),
                "volume": int(volume_value) if volume_value is not None else None,
            }
        )

    if not points:
        raise DownloadError(f"Yahoo Finance returned no usable candles for {label}")

    return {
        "label": label,
        "currency": meta.get("currency"),
        "exchange_timezone": meta.get("exchangeTimezoneName") or "Asia/Kolkata",
        "previous_close": round_or_none(parse_float(meta.get("previousClose"))),
        "regular_market_price": round_or_none(parse_float(meta.get("regularMarketPrice"))),
        "session_date": points[-1]["market_date"],
        "points": points,
    }


def scale_and_clean_bees_candles(bees_points: list[dict[str, Any]], scaling_factor: float) -> list[dict[str, Any]]:
    scaled_points = []
    for pt in bees_points:
        scaled_points.append({
            "time_utc": pt["time_utc"],
            "market_time": pt["market_time"],
            "market_date": pt["market_date"],
            "open": round(pt["open"] * scaling_factor, 2),
            "high": round(pt["high"] * scaling_factor, 2),
            "low": round(pt["low"] * scaling_factor, 2),
            "close": round(pt["close"] * scaling_factor, 2),
            "volume": pt.get("volume", 0)
        })
    return scaled_points


def merge_candles(yahoo_points: list[dict[str, Any]], nse_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not nse_points:
        return yahoo_points
    today_str = datetime.now(INDIA_TZ).date().isoformat()
    # Keep Yahoo Finance index candles that are BEFORE today
    filtered_yahoo = [pt for pt in yahoo_points if pt.get("market_date", "") < today_str]
    return filtered_yahoo + nse_points


def download_nifty_intraday(input_dir: Path) -> int:
    # 1. Fetch official index candles (15m delayed)
    one_minute = normalize_yahoo_candles(
        fetch_json(
            f"{YAHOO_CHART_BASE}/{YAHOO_NSEI_SYMBOL}?range=1d&interval=1m",
            referer="https://finance.yahoo.com/quote/%5ENSEI/chart",
        ),
        label="1m",
    )
    five_minute = normalize_yahoo_candles(
        fetch_json(
            f"{YAHOO_CHART_BASE}/{YAHOO_NSEI_SYMBOL}?range=5d&interval=5m",
            referer="https://finance.yahoo.com/quote/%5ENSEI/chart",
        ),
        label="5m",
    )

    # 2. Fetch real-time proxy candles from NIFTYBEES.NS (zero delay)
    print("[REAL-TIME PROXY] Fetching real-time proxy candles from NIFTYBEES.NS...")
    bees_1m = None
    bees_5m = None
    try:
        bees_1m_payload = fetch_json(
            f"{YAHOO_CHART_BASE}/NIFTYBEES.NS?range=1d&interval=1m",
            referer="https://finance.yahoo.com/quote/NIFTYBEES.NS/chart",
        )
        bees_1m = normalize_yahoo_candles(bees_1m_payload, label="1m")
    except Exception as e:
        print(f"[REAL-TIME PROXY] ⚠ Failed to fetch NIFTYBEES 1m chart: {e}")

    try:
        bees_5m_payload = fetch_json(
            f"{YAHOO_CHART_BASE}/NIFTYBEES.NS?range=5d&interval=5m",
            referer="https://finance.yahoo.com/quote/NIFTYBEES.NS/chart",
        )
        bees_5m = normalize_yahoo_candles(bees_5m_payload, label="5m")
    except Exception as e:
        print(f"[REAL-TIME PROXY] ⚠ Failed to fetch NIFTYBEES 5m chart: {e}")

    # 3. Fetch real-time live index summary to compute exact scaling factor dynamically
    live_summary = None
    try:
        live_summary = fetch_nifty_live_summary()
    except Exception as e:
        print(f"[REAL-TIME PROXY] ⚠ Failed to fetch NSE live summary for scaling calibration: {e}")

    # Compute dynamic scaling factor
    live_index_price = None
    if live_summary and live_summary.get("last") is not None:
        live_index_price = float(live_summary["last"])
    if not live_index_price and one_minute.get("regular_market_price") is not None:
        live_index_price = float(one_minute["regular_market_price"])

    bees_last_price = None
    if bees_1m and bees_1m.get("points"):
        bees_last_price = float(bees_1m["points"][-1]["close"])
    elif bees_1m and bees_1m.get("regular_market_price") is not None:
        bees_last_price = float(bees_1m["regular_market_price"])

    if live_index_price and bees_last_price and bees_last_price > 0:
        scaling_factor = live_index_price / bees_last_price
        print(f"[REAL-TIME PROXY] Calibrated scaling factor dynamically: {scaling_factor:.6f} ({live_index_price} / {bees_last_price})")
    else:
        # Fallback to standard ratio (~88.15 in mid-2026)
        scaling_factor = 88.15
        print(f"[REAL-TIME PROXY] ⚠ Using fallback scaling factor: {scaling_factor:.6f}")

    # 4. Merge real-time candles for today
    if bees_1m and bees_1m.get("points"):
        print(f"[REAL-TIME PROXY] ✓ Merging NIFTYBEES 1m real-time candles scaled by {scaling_factor:.6f}")
        scaled_1m = scale_and_clean_bees_candles(bees_1m["points"], scaling_factor)
        one_minute_points = merge_candles(one_minute["points"], scaled_1m)
    else:
        one_minute_points = one_minute["points"]

    if bees_5m and bees_5m.get("points"):
        print(f"[REAL-TIME PROXY] ✓ Merging NIFTYBEES 5m real-time candles scaled by {scaling_factor:.6f}")
        scaled_5m = scale_and_clean_bees_candles(bees_5m["points"], scaling_factor)
        five_minute_points = merge_candles(five_minute["points"], scaled_5m)
    else:
        five_minute_points = five_minute["points"]

    provider_name = "Yahoo Finance NIFTYBEES.NS Proxy + ^NSEI Historical" if (bees_1m or bees_5m) else "Yahoo Finance intraday chart API"

    live_summary: dict[str, Any] | None = None
    live_summary_error: str | None = None
    try:
        live_summary = fetch_nifty_live_summary()
    except DownloadError as exc:
        live_summary_error = str(exc)

    payload = {
        "provider": provider_name,
        "symbol": "^NSEI",
        "market_timezone": one_minute.get("exchange_timezone") or "Asia/Kolkata",
        "generated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "as_of_utc": one_minute_points[-1]["time_utc"] if one_minute_points else one_minute["points"][-1]["time_utc"],
        "last_price": live_summary.get("last") if live_summary else (one_minute_points[-1]["close"] if one_minute_points else one_minute.get("regular_market_price")),
        "previous_close": one_minute.get("previous_close"),
        "official_summary_source": "NSE allIndices",
        "official_summary_error": live_summary_error,
        "day_summary": live_summary,
        "official_chart_svg_url": (live_summary or {}).get("chart_svg_url"),
        "series_meta": {
            "1m": {
                "range": "1d",
                "interval": "1m",
                "points": len(one_minute_points),
                "session_date": one_minute_points[-1]["market_date"] if one_minute_points else one_minute.get("session_date"),
            },
            "5m": {
                "range": "5d",
                "interval": "5m",
                "points": len(five_minute_points),
                "session_date": five_minute_points[-1]["market_date"] if five_minute_points else five_minute.get("session_date"),
            },
        },
        "series": {
            "1m": detect_patterns(one_minute_points),
            "5m": detect_patterns(five_minute_points),
        },
        "notes": [
            "Intraday candles are sourced from a combination of Yahoo Finance NIFTYBEES.NS real-time proxy and ^NSEI historical data.",
            "Daily closes, flows, RBI rates, and world/news signals continue to come from the existing real-data pipeline.",
            "Candlestick patterns are detected using the nifty_pattern_detector.py module.",
        ],
    }
    write_json(input_dir / "nifty50_intraday.json", payload)
    return len(one_minute_points) + len(five_minute_points)


def fetch_nifty50_constituents() -> list[dict[str, str]]:
    csv_text = fetch_text_direct(
        NIFTY50_CONSTITUENTS_CSV,
        referer="https://niftyindices.com/indices/equity/broad-based-indices/nifty--50",
        accept="text/csv, */*",
    )
    rows = list(csv.DictReader(csv_text.splitlines()))
    normalized = [
        {
            "company_name": str(row.get("Company Name") or "").strip(),
            "sector": str(row.get("Industry") or "").strip(),
            "symbol": str(row.get("Symbol") or "").strip(),
            "series": str(row.get("Series") or "").strip(),
            "isin_code": str(row.get("ISIN Code") or "").strip(),
        }
        for row in rows
        if str(row.get("Symbol") or "").strip()
    ]
    if len(normalized) < 40:
        raise DownloadError("NIFTY 50 constituent CSV returned too few usable symbols")
    return normalized


def build_component_signal(row: dict[str, str]) -> dict[str, Any]:
    symbol = row["symbol"]
    yahoo_symbol = f"{symbol}.NS"
    payload = fetch_json_direct(
        f"{YAHOO_CHART_BASE}/{yahoo_symbol}?range=5d&interval=5m&includePrePost=false&events=div%2Csplits",
        referer=f"https://finance.yahoo.com/quote/{yahoo_symbol}/chart",
    )
    normalized = normalize_yahoo_candles(payload, label=f"{symbol} 5m")
    all_points = normalized["points"]
    sessions = split_sessions(all_points)
    if not sessions:
        raise DownloadError(f"{symbol} returned no usable session values")
    _, points = sessions[-1]
    previous_sessions = [session_points for _, session_points in sessions[:-1] if session_points]
    previous_session_points = previous_sessions[-1] if previous_sessions else []
    closes = [float(point["close"]) for point in points if point.get("close") is not None]
    highs = [float(point["high"]) for point in points if point.get("high") is not None]
    lows = [float(point["low"]) for point in points if point.get("low") is not None]
    volumes = [float(point["volume"]) for point in points if point.get("volume") is not None]
    if not closes or not highs or not lows:
        raise DownloadError(f"{symbol} returned no usable intraday values")

    last_price = parse_float(normalized.get("regular_market_price")) or closes[-1]
    previous_close = parse_float(normalized.get("previous_close"))
    session_open = parse_float(points[0].get("open"))
    session_high = max(highs)
    session_low = min(lows)
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    raw_day_change_pct = pct_change(last_price, previous_close)
    intraday_change_pct = pct_change(last_price, session_open)
    opening_gap_pct = pct_change(session_open, previous_close)
    corporate_action_adjusted = bool(
        opening_gap_pct is not None
        and intraday_change_pct is not None
        and abs(opening_gap_pct) >= 20.0
        and abs(intraday_change_pct) <= 8.0
    )
    day_change_pct = intraday_change_pct if corporate_action_adjusted else raw_day_change_pct
    range_position = 0.5 if session_high == session_low else (last_price - session_low) / (session_high - session_low)
    structure_score = 0.0
    if ema9 is not None:
        structure_score += 0.6 if last_price >= ema9 else -0.6
    if ema9 is not None and ema21 is not None:
        structure_score += 0.4 if ema9 >= ema21 else -0.4

    score = (
        clamp((day_change_pct or 0.0) / 1.8, -1.8, 1.8) * 0.58
        + clamp((intraday_change_pct or 0.0) / 1.2, -1.2, 1.2) * 0.22
        + structure_score * 0.15
        + clamp((range_position - 0.5) * 2, -1.0, 1.0) * 0.05
    )
    signal = "bullish" if score >= 0.2 else "bearish" if score <= -0.2 else "neutral"
    breakout_direction = "up" if signal == "bullish" else "down" if signal == "bearish" else None

    previous_day_high = max(
        [float(point["high"]) for point in previous_session_points if point.get("high") is not None],
        default=None,
    )
    previous_day_low = min(
        [float(point["low"]) for point in previous_session_points if point.get("low") is not None],
        default=None,
    )
    breakout_point = first_level_break(points, previous_day_high, direction="up")
    breakdown_point = first_level_break(points, previous_day_low, direction="down")
    active_level_break = breakout_point if breakout_point is not None else breakdown_point
    active_break_direction = "up" if breakout_point is not None else "down" if breakdown_point is not None else None
    break_market_time = active_level_break.get("market_time") if active_level_break is not None else None
    break_minutes = minutes_from_session_open(points, break_market_time) if active_level_break is not None else None

    volume_reference_points = [session_points[:len(points)] for session_points in previous_sessions[-4:] if session_points]
    reference_cumulative_volumes = [
        sum(float(point["volume"]) for point in reference_points if point.get("volume") is not None)
        for reference_points in volume_reference_points
    ]
    volume_curve_average = average(reference_cumulative_volumes)
    volume_multiple = (sum(volumes) / volume_curve_average) if volume_curve_average not in (None, 0) else None
    market_pressure = score * 5.0
    primary_momentum, secondary_momentum = session_momentum_label(score)
    tags = build_session_tags(
        opening_gap_pct=opening_gap_pct,
        intraday_change_pct=intraday_change_pct,
        range_position=range_position,
        ema9=ema9,
        ema21=ema21,
        last_price=last_price,
        breakout_direction=active_break_direction,
    )
    level_status = "PDH" if breakout_point is not None else "PDL" if breakdown_point is not None else "--"
    break_label = (
        f"{break_minutes} min Broken · {session_clock(break_market_time)}"
        if break_minutes is not None and break_market_time
        else "--"
    )
    momentum_rank = (
        abs(score) * 0.62
        + min(abs(day_change_pct or 0.0) / 5.0, 1.5) * 0.24
        + min((volume_multiple or 0.0) / 3.0, 1.2) * 0.14
    )
    opening_rank = (
        min(abs(opening_gap_pct or 0.0) / 1.8, 1.6) * 0.35
        + min(abs(intraday_change_pct or 0.0) / 2.2, 1.6) * 0.35
        + min((volume_multiple or 0.0) / 2.5, 1.4) * 0.30
    )
    signal_arrow = "up" if signal == "bullish" else "down" if signal == "bearish" else "flat"
    action_bias = "BUY CALL" if signal == "bullish" else "BUY PUT" if signal == "bearish" else "WAIT"

    return {
        "symbol": symbol,
        "yahoo_symbol": yahoo_symbol,
        "company_name": row["company_name"],
        "sector": row["sector"],
        "series": row["series"],
        "isin_code": row["isin_code"],
        "last_price": round_or_none(last_price),
        "previous_close": round_or_none(previous_close),
        "day_change_pct": round_or_none(day_change_pct),
        "raw_day_change_pct": round_or_none(raw_day_change_pct),
        "session_open": round_or_none(session_open),
        "intraday_change_pct": round_or_none(intraday_change_pct),
        "opening_gap_pct": round_or_none(opening_gap_pct),
        "day_high": round_or_none(session_high),
        "day_low": round_or_none(session_low),
        "range_position": round_or_none(range_position, 3),
        "ema9": round_or_none(ema9),
        "ema21": round_or_none(ema21),
        "volume": int(sum(volumes)) if volumes else None,
        "bars_loaded": len(points),
        "signal": signal,
        "score": round_or_none(score, 3),
        "momentum_rank": round_or_none(momentum_rank, 3),
        "opening_rank": round_or_none(opening_rank, 3),
        "volume_curve_average": round_or_none(volume_curve_average),
        "volume_curve_average_m": round_or_none((volume_curve_average or 0.0) / 1000000, 2) if volume_curve_average is not None else None,
        "volume_multiple": round_or_none(volume_multiple, 2),
        "market_pressure": round_or_none(market_pressure, 2),
        "previous_day_high": round_or_none(previous_day_high),
        "previous_day_low": round_or_none(previous_day_low),
        "level_status": level_status,
        "break_direction": active_break_direction,
        "break_time_market": session_clock(break_market_time),
        "break_minutes": break_minutes,
        "break_label": break_label,
        "session_momentum": primary_momentum,
        "session_trend_label": secondary_momentum,
        "momentum_tags": tags,
        "signal_arrow": signal_arrow,
        "action_bias": action_bias,
        "corporate_action_adjusted": corporate_action_adjusted,
        "last_bar_time_utc": points[-1]["time_utc"],
        "last_bar_market_time": points[-1]["market_time"],
        "session_date": points[-1]["market_date"],
    }


def download_nifty50_component_dashboard(input_dir: Path) -> int:
    constituents = fetch_nifty50_constituents()
    signals: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=COMPONENT_CHART_WORKERS) as executor:
        future_map = {executor.submit(build_component_signal, row): row for row in constituents}
        for future in as_completed(future_map):
            row = future_map[future]
            try:
                signals.append(future.result())
            except Exception as exc:
                failures.append({"symbol": row["symbol"], "message": str(exc)})

    if not signals:
        raise DownloadError("Could not build any live NIFTY 50 component stock signals")

    signals.sort(key=lambda item: item.get("score") or 0.0, reverse=True)
    tracked_map = {item["symbol"]: item for item in signals}
    bullish = [item for item in signals if item.get("signal") == "bullish"]
    bearish = [item for item in signals if item.get("signal") == "bearish"]
    neutral = [item for item in signals if item.get("signal") == "neutral"]
    top_momentum_bullish = sorted(
        bullish,
        key=lambda item: ((item.get("momentum_rank") or 0.0), (item.get("day_change_pct") or 0.0)),
        reverse=True,
    )[:16]
    top_momentum_bearish = sorted(
        bearish,
        key=lambda item: ((item.get("momentum_rank") or 0.0), abs(item.get("day_change_pct") or 0.0)),
        reverse=True,
    )[:16]
    opening_momentum_bullish = sorted(
        [item for item in bullish if (item.get("opening_rank") or 0.0) > 0.18],
        key=lambda item: ((item.get("opening_rank") or 0.0), (item.get("volume_multiple") or 0.0)),
        reverse=True,
    )[:12]
    opening_momentum_bearish = sorted(
        [item for item in bearish if (item.get("opening_rank") or 0.0) > 0.18],
        key=lambda item: ((item.get("opening_rank") or 0.0), (item.get("volume_multiple") or 0.0)),
        reverse=True,
    )[:12]
    ticker_strip = sorted(
        signals,
        key=lambda item: abs(item.get("day_change_pct") or 0.0),
        reverse=True,
    )[:8]
    payload = {
        "provider": "Nifty Indices constituent CSV + Yahoo Finance public chart API",
        "universe": "NIFTY 50",
        "generated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "tracked_symbols": list(COMPONENT_WATCHLIST),
        "coverage": {
            "requested": len(constituents),
            "loaded": len(signals),
            "failed": len(failures),
        },
        "market_summary": {
            "bullish": len(bullish),
            "bearish": len(bearish),
            "neutral": len(neutral),
        },
        "ticker_strip": ticker_strip,
        "top_momentum_bullish": top_momentum_bullish,
        "top_momentum_bearish": top_momentum_bearish,
        "opening_momentum_bullish": opening_momentum_bullish,
        "opening_momentum_bearish": opening_momentum_bearish,
        "top_bullish": signals[:10],
        "top_bearish": sorted(signals, key=lambda item: item.get("score") or 0.0)[:10],
        "heavyweights": [tracked_map[symbol] for symbol in COMPONENT_WATCHLIST if symbol in tracked_map],
        "stocks": signals,
        "failures": failures,
        "notes": [
            "Universe membership is downloaded from the official Nifty Indices constituent CSV.",
            "Live component signals are derived from Yahoo Finance 5 minute chart data for each NIFTY 50 stock.",
        ],
    }
    write_json(input_dir / "nifty50_components_live.json", payload)
    return len(signals)


def build_world_signals(input_dir: Path, *, timespan: str, maxrecords: int) -> None:
    cmd = [
        sys.executable,
        str(BUILD_WORLD_SIGNALS),
        "--output",
        str(input_dir / "world_signals.json"),
        "--timespan",
        timespan,
        "--maxrecords",
        str(maxrecords),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Unknown world-signals failure"
        raise DownloadError(f"build_world_signals.py failed: {message}")


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    from_date = parse_iso_date(args.from_date)
    to_date = parse_iso_date(args.to_date)
    if from_date > to_date:
        raise SystemExit("--from-date must be on or before --to-date")

    input_dir.mkdir(parents=True, exist_ok=True)

    counts = {
        "nifty50_history.csv": download_nifty_history(input_dir, from_date, to_date),
        "india_vix.csv": download_india_vix(input_dir, from_date, to_date),
        "fii_dii.csv": download_fii_dii(input_dir),
        "gift_nifty.csv": download_gift_nifty(input_dir),
        "usdinr.csv": download_usdinr(input_dir, from_date, to_date),
        "nifty50_intraday.json": download_nifty_intraday(input_dir),
        "nifty50_components_live.json": download_nifty50_component_dashboard(input_dir),
    }
    if not args.skip_world_signals:
        build_world_signals(input_dir, timespan=args.world_timespan, maxrecords=args.world_maxrecords)
        counts["world_signals.json"] = 1

    print(
        json.dumps(
            {
                "ok": True,
                "input_dir": str(input_dir),
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "files_written": counts,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except DownloadError as exc:
        print(json.dumps({"ok": False, "error": "download_failed", "message": str(exc)}, indent=2))
        raise SystemExit(1) from exc
