#!/usr/bin/env python3
"""Build a real NSE intraday stock suggestion index.

The scanner intentionally ignores the old NIFTY option prediction pipeline.
It uses live NSE Top Gainers/Losers for F&O securities plus Change in Open
Interest by underlying to produce today's practical bullish and bearish lists.
No dummy rows are generated; if real data is unavailable the output says so.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "inputs"
OUTPUT_PATH = INPUT_DIR / "stock_suggestion_index.latest.json"
NSE_BASE = "https://www.nseindia.com"
YAHOO_CHART_BASE = "https://query2.finance.yahoo.com/v8/finance/chart"
INDIA_TZ = ZoneInfo("Asia/Kolkata")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
MARKET_OPEN = time(9, 15)
SUGGESTION_READY = time(10, 0)
MARKET_CLOSE = time(15, 30)
NSE_TOP_ROWS = 20
DEFAULT_SUGGESTIONS_PER_SIDE = 50
MIN_ONE_SIDE_SCORE = 65
MIN_BULLISH_CHANGE = 0.65
MIN_BEARISH_CHANGE = -0.65
MIN_RANGE_EXTREME = 0.55
INDEX_LIKE_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


class LiveDataError(RuntimeError):
    """Raised when a required live NSE source cannot be loaded."""


@dataclass(frozen=True)
class Suggestion:
    symbol: str
    side: str
    rank: int
    setup: str
    trade_plan: str
    ltp: float | None
    percent_change: float | None
    oi_change_percent: float | None
    oi_change: float | None
    volume: float | None
    value_lakhs: float | None
    entry: float | None
    stop_loss: float | None
    target: float | None
    score: int
    one_side_rally_score: int
    rally_type: str
    range_position_percent: float | None
    move_from_open_percent: float | None
    index_context: str
    quality_tags: list[str]
    status: str
    reason: str
    vc_ranking: float = 1.0
    mp_score: float = 3.0
    five_min_status: str = "Consolidating"
    breakout_time: str | None = None
    is_breakout: bool = False
    chart_structure: str = "Base Building"
    rvol_5m: float = 1.0
    ema_trend: str = "neutral"
    vwap: float | None = None
    retest_status: str = "Initial"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "rank": self.rank,
            "setup": self.setup,
            "trade_plan": self.trade_plan,
            "ltp": self.ltp,
            "percent_change": self.percent_change,
            "oi_change_percent": self.oi_change_percent,
            "oi_change": self.oi_change,
            "volume": self.volume,
            "value_lakhs": self.value_lakhs,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "score": self.score,
            "one_side_rally_score": self.one_side_rally_score,
            "rally_type": self.rally_type,
            "range_position_percent": self.range_position_percent,
            "move_from_open_percent": self.move_from_open_percent,
            "index_context": self.index_context,
            "quality_tags": self.quality_tags,
            "status": self.status,
            "reason": self.reason,
            "vc_ranking": self.vc_ranking,
            "mp_score": self.mp_score,
            "five_min_status": self.five_min_status,
            "breakout_time": self.breakout_time,
            "is_breakout": self.is_breakout,
            "chart_structure": self.chart_structure,
            "rvol_5m": self.rvol_5m,
            "ema_trend": self.ema_trend,
            "vwap": self.vwap,
            "retest_status": self.retest_status,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build today's NSE F&O intraday stock suggestion index.")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help="JSON output path")
    parser.add_argument("--top", type=int, default=DEFAULT_SUGGESTIONS_PER_SIDE, help="Number of matched rows to keep per side")
    return parser.parse_args()


def parse_float(value: Any) -> float | None:
    if value in (None, "", "-", "--", "NA", "N/A"):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)

    text = str(value).strip()
    if not text:
        return None
    multiplier = 1.0
    lower = text.lower()
    if lower.endswith("cr"):
        multiplier = 10_000_000.0
        text = text[:-2]
    elif lower.endswith("l"):
        multiplier = 100_000.0
        text = text[:-1]
    elif lower.endswith("k"):
        multiplier = 1_000.0
        text = text[:-1]

    cleaned = (
        text.replace(",", "")
        .replace("%", "")
        .replace("₹", "")
        .replace("Rs.", "")
        .replace("rs.", "")
        .strip()
    )
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def first_value(row: dict[str, Any], keys: Iterable[str]) -> Any:
    lower_map = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        lower_key = key.lower()
        if lower_key in lower_map:
            return lower_map[lower_key]
    return None


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    for suffix in (".NS", "-EQ"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    return symbol


def nse_get_json(url: str, *, referer: str) -> Any:
    cookie_jar = Path(tempfile.gettempdir()) / "nifty_stock_suggestion_nse.cookies"
    base_headers = [
        "-A",
        USER_AGENT,
        "-H",
        "Accept: application/json, text/plain, */*",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-H",
        "Connection: keep-alive",
    ]

    # Use curl because NSE frequently blocks default Python urllib sessions.
    import subprocess

    warmup = [
        "curl",
        "--doh-url",
        "https://dns.google/dns-query",
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "-c",
        str(cookie_jar),
        *base_headers,
        "-e",
        referer,
        NSE_BASE,
    ]
    subprocess.run(warmup, cwd=ROOT, capture_output=True, text=True, timeout=20)

    cmd = [
        "curl",
        "--doh-url",
        "https://dns.google/dns-query",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "-b",
        str(cookie_jar),
        *base_headers,
        "-e",
        referer,
        "-H",
        f"Referer: {referer}",
        url,
    ]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise LiveDataError(result.stderr.strip() or f"NSE request failed: {url}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LiveDataError(f"NSE returned non-JSON for {url}: {result.stdout[:120]!r}") from exc


def nse_get_csv(url: str, *, referer: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8-sig")
    return [dict(row) for row in csv.DictReader(text.splitlines())]


def rows_from_any_payload(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            rows.extend(row for row in payload["data"] if isinstance(row, dict))
        for value in payload.values():
            if isinstance(value, dict) and isinstance(value.get("data"), list):
                rows.extend(row for row in value["data"] if isinstance(row, dict))
            elif isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
    elif isinstance(payload, list):
        rows.extend(row for row in payload if isinstance(row, dict))
    return rows


def extract_fno_variation(payload: Any, *, side: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("FOSec"), dict):
        rows = payload["FOSec"].get("data") or []
    else:
        rows = rows_from_any_payload(payload)

    normalized = [normalize_variation_row(row, side=side) for row in rows if isinstance(row, dict)]
    filtered = [row for row in normalized if row["symbol"] and row["percent_change"] is not None]
    if side == "gainer":
        filtered = [row for row in filtered if row["percent_change"] >= 0]
        return sorted(filtered, key=lambda row: row["percent_change"] or 0, reverse=True)[:NSE_TOP_ROWS]
    filtered = [row for row in filtered if row["percent_change"] <= 0]
    return sorted(filtered, key=lambda row: row["percent_change"] or 0)[:NSE_TOP_ROWS]


def normalize_variation_row(row: dict[str, Any], *, side: str) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(first_value(row, ("symbol", "Symbol", "SYMBOL"))),
        "side": side,
        "open": parse_float(first_value(row, ("open_price", "openPrice", "open", "OPEN"))),
        "high": parse_float(first_value(row, ("high_price", "highPrice", "high", "HIGH"))),
        "low": parse_float(first_value(row, ("low_price", "lowPrice", "low", "LOW"))),
        "prev_close": parse_float(first_value(row, ("prev_price", "prevClose", "previousClose", "prev_close", "PREV. CLOSE"))),
        "ltp": parse_float(first_value(row, ("ltp", "lastPrice", "last_traded_price", "LTP"))),
        "percent_change": parse_float(first_value(row, ("netPrice", "pChange", "percent_change", "perChange", "%CHNG", "% Change"))),
        "volume": parse_float(first_value(row, ("traded_quantity", "totalTradedVolume", "volume", "VOLUME", "Volume (Shares)"))),
        "value_lakhs": parse_float(first_value(row, ("turnover", "totalTradedValue", "value", "VALUE", "Value (₹ Lakhs)"))),
    }


def fetch_top_gainers_losers() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    referer = f"{NSE_BASE}/market-data/top-gainers-losers"
    errors: list[str] = []
    payload = None
    for url in (
        f"{NSE_BASE}/api/live-analysis-variations?index=gainers",
        f"{NSE_BASE}/api/live-analysis-variations?index=gainers&type=FOSec",
    ):
        try:
            payload = nse_get_json(url, referer=referer)
            break
        except Exception as exc:  # noqa: BLE001 - report source errors to UI
            errors.append(f"gainers {url}: {exc}")
    if payload is None:
        raise LiveDataError("; ".join(errors))

    gainers = extract_fno_variation(payload, side="gainer")

    loser_payload = None
    for url in (
        f"{NSE_BASE}/api/live-analysis-variations?index=loosers",
        f"{NSE_BASE}/api/live-analysis-variations?index=losers",
        f"{NSE_BASE}/api/live-analysis-variations?index=loosers&type=FOSec",
    ):
        try:
            loser_payload = nse_get_json(url, referer=referer)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"losers {url}: {exc}")
    if loser_payload is None:
        raise LiveDataError("; ".join(errors))

    losers = extract_fno_variation(loser_payload, side="loser")
    return gainers, losers, errors


def normalize_oi_row(row: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(first_value(row, ("symbol", "underlying", "underlyingSymbol", "SYMBOL")))
    oi_now = parse_float(first_value(row, ("openInterest", "latestOI", "OPEN INTEREST", "oi", "currentOI")))
    oi_prev = parse_float(first_value(row, ("previousOpenInterest", "prevOI", "prevOpenInterest", "OPEN INTEREST Previous", "previousOI")))
    oi_change = parse_float(first_value(row, ("changeInOI", "changeOI", "CHNG IN OI", "change", "oiChange")))
    oi_change_percent = parse_float(first_value(row, ("pchangeinOpenInterest", "pChangeInOI", "%CHNG IN OI", "pctChangeOI", "oiChangePercent")))
    volume = parse_float(first_value(row, ("volume", "VOLUME (contracts)", "contracts", "futVolume")))
    futures_value = parse_float(first_value(row, ("futValue", "futuresValue", "FUTURES VALUE", "value")))

    if oi_change is None and oi_now is not None and oi_prev is not None:
        oi_change = oi_now - oi_prev
    if oi_change_percent is None and oi_prev not in (None, 0) and oi_change is not None:
        oi_change_percent = (oi_change / oi_prev) * 100

    return {
        "symbol": symbol,
        "oi_now": oi_now,
        "oi_prev": oi_prev,
        "oi_change": oi_change,
        "oi_change_percent": oi_change_percent,
        "volume_contracts": volume,
        "futures_value": futures_value,
    }


def fetch_change_in_oi() -> tuple[dict[str, dict[str, Any]], list[str]]:
    referer = f"{NSE_BASE}/market-data/oi-spurts"
    errors: list[str] = []
    payload = None
    for url in (
        f"{NSE_BASE}/api/live-analysis-oi-spurts-underlyings",
        f"{NSE_BASE}/api/live-analysis-oi-spurts-contracts",
    ):
        try:
            payload = nse_get_json(url, referer=referer)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"oi {url}: {exc}")
    if payload is None:
        raise LiveDataError("; ".join(errors))

    rows = [normalize_oi_row(row) for row in rows_from_any_payload(payload)]
    rows = [row for row in rows if row["symbol"]]
    by_symbol = {row["symbol"]: row for row in rows}
    return by_symbol, errors


def normalize_index_row(index_name: str, payload: Any) -> dict[str, Any]:
    meta = payload.get("metadata") if isinstance(payload, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    rows = rows_from_any_payload(payload)
    first_row = rows[0] if rows else {}
    source = meta or first_row
    ltp = parse_float(first_value(source, ("last", "lastPrice", "ltp", "LTP")))
    pct_change = parse_float(first_value(source, ("percChange", "pChange", "percentChange", "%CHNG", "% Change")))
    change = parse_float(first_value(source, ("change", "netChange", "CHNG")))
    return {
        "name": index_name,
        "ltp": ltp,
        "percent_change": pct_change,
        "change": change,
        "direction": "bullish" if (pct_change or 0) > 0 else "bearish" if (pct_change or 0) < 0 else "flat",
    }


def fetch_index_context() -> tuple[dict[str, dict[str, Any]], list[str]]:
    referer = f"{NSE_BASE}/market-data/live-equity-market"
    errors: list[str] = []
    indexes: dict[str, dict[str, Any]] = {}
    
    # 1. Primary: Official NSE allIndices API (matches Groww/Zerodha perfectly)
    try:
        data = nse_get_json(f"{NSE_BASE}/api/allIndices", referer=referer)
        for item in data.get("data", []):
            idx = item.get("index")
            if idx in ("NIFTY 50", "NIFTY BANK"):
                pct = float(item.get("percentChange", 0))
                ltp = float(item.get("last", 0))
                chg = float(item.get("variation", 0))
                indexes[idx] = {
                    "name": idx,
                    "ltp": round(ltp, 2),
                    "percent_change": round(pct, 2),
                    "change": round(chg, 2),
                    "direction": "bullish" if pct > 0 else "bearish" if pct < 0 else "flat",
                }
    except Exception as exc:
        errors.append(f"nse allIndices: {exc}")

    # 2. Fallback: Yahoo Finance (if NSE blocked)
    if "NIFTY 50" not in indexes or "NIFTY BANK" not in indexes:
        mapping = {
            "NIFTY 50": "^NSEI",
            "NIFTY BANK": "^NSEBANK",
        }
        for index_name, ticker in mapping.items():
            if index_name in indexes:
                continue
            try:
                url = f"{YAHOO_CHART_BASE}/{urllib.parse.quote(ticker)}?interval=5m&range=1d"
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json, text/plain, */*",
                    },
                )
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    result = (data.get("chart") or {}).get("result")
                    if result:
                        meta = result[0].get("meta") or {}
                        ltp = meta.get("regularMarketPrice")
                        prev = meta.get("chartPreviousClose") or ltp
                        pct_change = ((ltp - prev) / prev) * 100 if prev else 0.0
                        change = ltp - prev if prev else 0.0
                        indexes[index_name] = {
                            "name": index_name,
                            "ltp": round(float(ltp), 2),
                            "percent_change": round(float(pct_change), 2),
                            "change": round(float(change), 2),
                            "direction": "bullish" if pct_change > 0 else "bearish" if pct_change < 0 else "flat",
                        }
            except Exception as exc:
                errors.append(f"index {index_name}: {exc}")
            
    return indexes, errors


def classify_oi(price_change: float | None, oi_change: float | None) -> str:
    if price_change is None or oi_change is None:
        return "OI not confirmed"
    if price_change >= 0 and oi_change > 0:
        return "Long buildup"
    if price_change >= 0 and oi_change < 0:
        return "Short covering"
    if price_change < 0 and oi_change > 0:
        return "Short buildup"
    if price_change < 0 and oi_change < 0:
        return "Long unwinding"
    return "Flat OI"


def day_range_position(row: dict[str, Any]) -> float | None:
    high = row.get("high")
    low = row.get("low")
    ltp = row.get("ltp")
    if high is None or low is None or ltp is None or high <= low:
        return None
    return max(0.0, min(1.0, (ltp - low) / (high - low)))


def pct_from_open(row: dict[str, Any]) -> float | None:
    open_price = row.get("open")
    ltp = row.get("ltp")
    if open_price in (None, 0) or ltp is None:
        return None
    return ((ltp - open_price) / open_price) * 100


def fetch_5m_candles_for_symbol(symbol: str, full_range: bool = False) -> list[dict[str, Any]]:
    """Fetches real-time 5-minute OHLCV candles for an NSE stock."""
    yahoo_sym = f"{symbol}.NS"
    url = f"{YAHOO_CHART_BASE}/{urllib.parse.quote(yahoo_sym)}?interval=5m&range=5d"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://finance.yahoo.com/quote/{yahoo_sym}/chart",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            result = (data.get("chart") or {}).get("result")
            if not result:
                return []
            quote = result[0]
            timestamps = quote.get("timestamp") or []
            indicators = quote.get("indicators", {})
            quote_data = (indicators.get("quote") or [{}])[0]
            opens = quote_data.get("open") or []
            highs = quote_data.get("high") or []
            lows = quote_data.get("low") or []
            closes = quote_data.get("close") or []
            volumes = quote_data.get("volume") or []
            all_candles: list[dict[str, Any]] = []
            for i, ts in enumerate(timestamps):
                if i < len(opens) and i < len(highs) and i < len(lows) and i < len(closes):
                    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                    v = volumes[i] if i < len(volumes) else 0
                    if None not in (o, h, l, c):
                        all_candles.append({
                            "timestamp": int(ts),
                            "open": float(o),
                            "high": float(h),
                            "low": float(l),
                            "close": float(c),
                            "volume": float(v or 0),
                        })
            if not all_candles:
                return []
            
            if full_range:
                return all_candles

            # Isolate the latest trading session candles (same date as latest timestamp)
            from datetime import datetime as dt_cls
            latest_dt = dt_cls.fromtimestamp(all_candles[-1]["timestamp"], tz=INDIA_TZ).date()
            day_candles = [
                c for c in all_candles
                if dt_cls.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() == latest_dt
            ]
            return day_candles if day_candles else all_candles[-75:]
    except Exception as e:
        print(f"fetch_5m_candles_for_symbol error for {symbol}: {type(e)} {e}")
        return []


def analyze_5m_breakout(candles: list[dict[str, Any]], side: str, row: dict[str, Any], activity_ratio: float) -> dict[str, Any]:
    """Analyzes 5-minute candles using 15m ORB, dynamic afternoon consolidation boxes,
    solid body ratio (rejecting wick traps), and rolling RVOL confirmation."""
    if len(candles) < 2:
        return {
            "is_breakout": False,
            "status": "Setting Opening Range",
            "breakout_time": None,
            "rvol_5m": 1.0,
            "ema_trend": "bullish" if side == "bullish" else "bearish",
            "chart_structure": "Setting ORB",
            "vwap": None,
            "retest_status": "Setting Range",
            "is_failed_trend": False,
        }

    # 1. 9-period EMA on 5-minute closes
    closes = [c["close"] for c in candles]
    k = 2.0 / (9 + 1)
    ema9 = closes[0]
    for price in closes[1:]:
        ema9 = (price * k) + (ema9 * (1.0 - k))

    # 2. Strict 15-Minute Opening Range Baseline (First 3 candles: 09:15 - 09:30 AM)
    orb_count = min(len(candles), 3)
    orb_high = max(c["high"] for c in candles[:orb_count])
    orb_low = min(c["low"] for c in candles[:orb_count])
    day_high = max(c["high"] for c in candles)
    day_low = min(c["low"] for c in candles)

    # 3. Full-day & Rolling 5-minute RVOL
    valid_vols = [c["volume"] for c in candles if c["volume"] > 0]
    avg_5m_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 1.0

    latest = candles[-1]
    latest_close = latest["close"]
    latest_vol = latest["volume"]
    # Rolling 20-candle average for latest candle (avoiding closing auction distortion)
    recent_vols = [x["volume"] for x in candles[-21:-1] if x["volume"] > 0]
    rolling_vol_ref = sum(recent_vols) / len(recent_vols) if recent_vols else avg_5m_vol
    latest_rvol = latest_vol / rolling_vol_ref if rolling_vol_ref > 0 else 1.0
    live_price = row.get("ltp", latest_close)

    # 4. Intraday Cumulative VWAP
    cum_pv = 0.0
    cum_vol = 0.0
    for c in candles:
        v = c.get("volume", 0)
        if v > 0:
            typ = (c["high"] + c["low"] + c["close"]) / 3.0
            cum_pv += typ * v
            cum_vol += v
    vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else round(latest_close, 2)

    # Helper for solid body ratio: body / total range
    # Strictly eliminates fakeout wick traps (e.g. hammers/pinbars like POLYCAB at 11:10 AM)
    def is_solid_directional_candle(c: dict[str, Any], is_bullish: bool) -> bool:
        tot_range = max(0.01, c["high"] - c["low"])
        body = abs(c["close"] - c["open"])
        body_ratio = body / tot_range
        # Must have body >= 40% of total candle range
        if body_ratio < 0.40:
            return False
        # For bullish, close must be green; for bearish, close must be red
        if is_bullish:
            return c["close"] > c["open"] and (c["close"] - c["low"]) >= tot_range * 0.45
        else:
            return c["close"] < c["open"] and (c["high"] - c["close"]) >= tot_range * 0.45

    trading_candles = candles[orb_count:] if len(candles) > orb_count else candles[1:]

    morning_event = None
    afternoon_event = None
    last_wave_idx = -999

    for idx, c in enumerate(trading_candles):
        actual_idx = orb_count + idx
        dt = datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ)
        t_str = dt.strftime("%I:%M %p")
        time_minutes = dt.hour * 60 + dt.minute

        # Intraday breakout entries must occur before 02:45 PM
        if time_minutes > (14 * 60 + 45):
            continue

        prior_vols = [x["volume"] for x in candles[max(0, actual_idx - 20):actual_idx] if x["volume"] > 0]
        r_avg = sum(prior_vols) / len(prior_vols) if prior_vols else avg_5m_vol
        c_rvol = c.get("volume", 0) / r_avg if r_avg > 0 else 1.0
        solid = is_solid_directional_candle(c, is_bullish=(side == "bullish"))
        if not solid:
            continue

        if side == "bullish":
            # A. Morning 15m ORB Breakout (09:30 AM - 10:15 AM)
            is_orb = (c["close"] > orb_high) and (c_rvol >= 1.2 or activity_ratio >= 2.5)

            # B. Mid-day / Afternoon Dynamic Consolidation Box
            box_len = min(12, max(6, actual_idx - max(0, last_wave_idx)))
            box_slice = candles[max(0, actual_idx - box_len):actual_idx]
            box_h = max(x["high"] for x in box_slice) if box_slice else orb_high
            box_l = min(x["low"] for x in box_slice) if box_slice else orb_low
            box_rng = ((box_h - box_l) / box_l) * 100.0 if box_l > 0 else 10.0
            prev_dh = max(x["high"] for x in candles[:actual_idx])
            is_box = (actual_idx >= 8) and (c["close"] > box_h) and (box_rng < 2.5 or c["close"] > prev_dh) and (c_rvol >= 1.3 or c.get("volume", 0) > avg_5m_vol * 1.5)

            if is_orb and morning_event is None and actual_idx <= 12:
                morning_event = {
                    "idx": idx,
                    "actual_idx": actual_idx,
                    "time": t_str,
                    "price": c["close"],
                    "rvol": c_rvol,
                    "volume": c.get("volume", 0),
                    "type": "⚡ Fresh Breakout",
                }
                last_wave_idx = actual_idx
            elif is_box:
                is_afternoon_session = (time_minutes >= 12 * 60)
                if (actual_idx - last_wave_idx >= 6):
                    if is_afternoon_session or not afternoon_event:
                        afternoon_event = {
                            "idx": idx,
                            "actual_idx": actual_idx,
                            "time": t_str,
                            "price": c["close"],
                            "rvol": c_rvol,
                            "volume": c.get("volume", 0),
                            "type": "⚡ Afternoon Breakout",
                        }
                        last_wave_idx = actual_idx
                elif afternoon_event and (actual_idx - afternoon_event["actual_idx"] <= 3):
                    if c_rvol > afternoon_event["rvol"]:
                        afternoon_event["rvol"] = c_rvol

        else:  # Bearish
            # A. Morning 15m ORB Breakdown (09:30 AM - 10:15 AM)
            is_orb = (c["close"] < orb_low) and (c_rvol >= 1.2 or activity_ratio >= 2.5)

            # B. Mid-day / Afternoon Dynamic Consolidation Box
            box_len = min(12, max(6, actual_idx - max(0, last_wave_idx)))
            box_slice = candles[max(0, actual_idx - box_len):actual_idx]
            box_h = max(x["high"] for x in box_slice) if box_slice else orb_high
            box_l = min(x["low"] for x in box_slice) if box_slice else orb_low
            box_rng = ((box_h - box_l) / box_l) * 100.0 if box_l > 0 else 10.0
            prev_dl = min(x["low"] for x in candles[:actual_idx])
            is_box = (actual_idx >= 8) and (c["close"] < box_l) and (box_rng < 2.5 or c["close"] < prev_dl) and (c_rvol >= 1.3 or c.get("volume", 0) > avg_5m_vol * 1.5)

            if is_orb and morning_event is None and actual_idx <= 12:
                morning_event = {
                    "idx": idx,
                    "actual_idx": actual_idx,
                    "time": t_str,
                    "price": c["close"],
                    "rvol": c_rvol,
                    "volume": c.get("volume", 0),
                    "type": "⚡ Fresh Breakdown",
                }
                last_wave_idx = actual_idx
            elif is_box:
                is_afternoon_session = (time_minutes >= 12 * 60)
                if (actual_idx - last_wave_idx >= 6):
                    if is_afternoon_session or not afternoon_event:
                        afternoon_event = {
                            "idx": idx,
                            "actual_idx": actual_idx,
                            "time": t_str,
                            "price": c["close"],
                            "rvol": c_rvol,
                            "volume": c.get("volume", 0),
                            "type": "⚡ Afternoon Breakdown",
                        }
                        last_wave_idx = actual_idx
                elif afternoon_event and (actual_idx - afternoon_event["actual_idx"] <= 3):
                    if c_rvol > afternoon_event["rvol"]:
                        afternoon_event["rvol"] = c_rvol

    active_event = afternoon_event or morning_event
    if not active_event:
        is_near = (latest_close >= orb_high * 0.992) if side == "bullish" else (latest_close <= orb_low * 1.008)
        return {
            "is_breakout": False,
            "status": ("Near BO" if side == "bullish" else "Near BD") if is_near else "Consolidating",
            "breakout_time": None,
            "rvol_5m": 1.0,
            "ema_trend": "bullish" if latest_close >= ema9 else "bearish",
            "chart_structure": ("Testing Resistance" if side == "bullish" else "Testing Support") if is_near else "Base Building",
            "is_failed_trend": False,
            "vwap": vwap,
            "retest_status": "Near" if is_near else "Initial",
        }

    # Evaluate Retest or Trend Failure after the active breakout
    breakout_status = active_event["type"]
    retest_status = "Fresh"
    chart_structure = "Breakout Surge" if side == "bullish" else "Breakdown Slide"
    is_breakout = True
    is_failed_trend = False
    ref_price = active_event["price"]
    breakout_time = active_event["time"]

    for idx in range(active_event["idx"] + 1, len(trading_candles)):
        c = trading_candles[idx]
        prior_vols = [x["volume"] for x in candles[max(0, orb_count + idx - 20):orb_count + idx] if x["volume"] > 0]
        r_avg = sum(prior_vols) / len(prior_vols) if prior_vols else avg_5m_vol
        c_rvol = c.get("volume", 0) / r_avg if r_avg > 0 else 1.0

        if side == "bullish":
            is_pullback = (c["low"] <= ref_price * 1.006) or (c["low"] <= ema9 * 1.004)
            if is_pullback and c["close"] > c["open"] and c["close"] >= ref_price * 0.998 and is_solid_directional_candle(c, is_bullish=True) and c_rvol >= 0.75:
                breakout_status = "Retest Confirmed"
                retest_status = "Confirmed"
                chart_structure = "Retest Bounce Wave"
                break
        else:
            is_pullback = (c["high"] >= ref_price * 0.994) or (c["high"] >= ema9 * 0.996)
            if is_pullback and c["close"] < c["open"] and c["close"] <= ref_price * 1.002 and is_solid_directional_candle(c, is_bullish=False) and c_rvol >= 0.75:
                breakout_status = "Retest Confirmed"
                retest_status = "Confirmed"
                chart_structure = "Retest Rejection Slide"
                break

    # Stop loss check
    if side == "bullish":
        if latest_close < orb_low or latest_close < ref_price * 0.985:
            breakout_status = "Breakout Failed"
            retest_status = "Failed"
            chart_structure = "Failed Breakout"
            is_breakout = False
            is_failed_trend = True
        elif latest_close < ref_price * 0.995:
            breakout_status = "Testing Support"
            retest_status = "Retesting"
            chart_structure = "Testing Support"
            is_breakout = False
    else:
        if latest_close > orb_high or latest_close > ref_price * 1.015:
            breakout_status = "Breakdown Failed"
            retest_status = "Failed"
            chart_structure = "Failed Breakdown"
            is_breakout = False
            is_failed_trend = True
        elif latest_close > ref_price * 1.005:
            breakout_status = "Testing Resistance"
            retest_status = "Retesting"
            chart_structure = "Testing Resistance"
            is_breakout = False

    return {
        "is_breakout": is_breakout,
        "status": breakout_status,
        "breakout_time": breakout_time,
        "rvol_5m": round(active_event["rvol"], 2),
        "ema_trend": "bullish" if latest_close >= ema9 else "bearish",
        "chart_structure": chart_structure,
        "is_failed_trend": is_failed_trend,
        "vwap": vwap,
        "retest_status": retest_status,
        "morning_breakout_time": morning_event["time"] if morning_event else None,
        "afternoon_breakout_time": afternoon_event["time"] if afternoon_event else None,
    }


def index_context_note(row: dict[str, Any], side: str, index_context: dict[str, dict[str, Any]]) -> tuple[str, int]:
    available = [item for item in index_context.values() if item.get("percent_change") is not None]
    stock_change = row.get("percent_change")
    if not available or stock_change is None:
        return "Index context unavailable", 0

    nifty = index_context.get("NIFTY 50", {})
    bank = index_context.get("NIFTY BANK", {})
    nifty_pct = nifty.get("percent_change")
    bank_pct = bank.get("percent_change")
    market_best = max([item["percent_change"] for item in available])
    market_worst = min([item["percent_change"] for item in available])
    note = f"NIFTY {nifty_pct:+.2f}% | BANKNIFTY {bank_pct:+.2f}%" if nifty_pct is not None and bank_pct is not None else "Partial index context"

    if side == "bullish":
        relative_gap = stock_change - market_best
        if relative_gap >= 1.0:
            return f"{note} | strong relative strength", 8
        if market_best >= 0:
            return f"{note} | market supports upside", 5
        if stock_change >= 1.25:
            return f"{note} | stock resisting weak index", 4
        return f"{note} | market not supportive", -4

    relative_gap = market_worst - stock_change
    if relative_gap >= 1.0:
        return f"{note} | strong relative weakness", 8
    if market_worst <= 0:
        return f"{note} | market supports downside", 5
    if stock_change <= -1.25:
        return f"{note} | stock falling despite firm index", 4
    return f"{note} | market not supportive", -4


def calc_stock_activity(row: dict[str, Any], oi: dict[str, Any] | None) -> float:
    vol = float(row.get("volume") or (oi or {}).get("volume_contracts") or 0)
    ltp = float(row.get("ltp") or 1.0)
    val_lakhs = float(row.get("value_lakhs") or (oi or {}).get("futures_value") or 0)
    return (vol * ltp * 0.4) + (val_lakhs * 100_000 * 0.6)


def calc_score(row: dict[str, Any], oi: dict[str, Any] | None, *, side: str, activity_ratio: float) -> int:
    price_change = abs(row.get("percent_change") or 0)
    price_score = min(price_change / 5.0, 1.0) * 20
    vol_score = min(activity_ratio / 2.5, 1.0) * 25

    range_pos = day_range_position(row)
    range_score = 0.0
    if range_pos is not None:
        if side == "bullish":
            range_score = min(max(0.0, range_pos - 0.4) / 0.5, 1.0) * 25
        else:
            range_score = min(max(0.0, 0.6 - range_pos) / 0.5, 1.0) * 25

    oi_change_percent = abs((oi or {}).get("oi_change_percent") or 0)
    oi_score = min(oi_change_percent / 15.0, 1.0) * 15

    setup = classify_oi(row.get("percent_change"), (oi or {}).get("oi_change"))
    alignment_score = 0
    if side == "bullish" and setup in {"Long buildup", "Short covering"}:
        alignment_score = 15 if (setup == "Long buildup" or activity_ratio >= 1.5) else 12
    if side == "bearish" and setup in {"Short buildup", "Long unwinding"}:
        alignment_score = 15 if (setup == "Short buildup" or activity_ratio >= 1.5) else 12

    return int(round(max(0, min(100, price_score + vol_score + range_score + oi_score + alignment_score))))


def assess_one_side_rally(
    row: dict[str, Any],
    oi: dict[str, Any] | None,
    *,
    side: str,
    activity_ratio: float,
    index_context: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    setup = classify_oi(row.get("percent_change"), (oi or {}).get("oi_change"))
    pct_change = row.get("percent_change")
    range_pos = day_range_position(row)
    open_move = pct_from_open(row)
    oi_change_percent = abs((oi or {}).get("oi_change_percent") or 0)
    tags: list[str] = []
    blockers: list[str] = []
    score = 0.0

    # 1. Volume Surge / RVOL Buildup (Max 25 pts)
    if activity_ratio >= 2.5:
        score += 25
        tags.append(f"massive volume buildup ({activity_ratio:.1f}x)")
    elif activity_ratio >= 1.6:
        score += 20
        tags.append(f"high volume surge ({activity_ratio:.1f}x)")
    elif activity_ratio >= 1.0:
        score += 12
        tags.append(f"above-average volume ({activity_ratio:.1f}x)")
    elif activity_ratio >= 0.6:
        score += 6
        tags.append("moderate volume")
    else:
        score += 2

    # 2. Day Range Position / Upper Range Hold (Max 25 pts)
    if range_pos is None:
        score += 10
        tags.append("range position unavailable")
    elif side == "bullish":
        if range_pos >= 0.80:
            score += 25
            tags.append(f"locked near day high ({range_pos*100:.0f}%)")
        elif range_pos >= MIN_RANGE_EXTREME:
            score += 18
            tags.append(f"holding upper range ({range_pos*100:.0f}%)")
        elif range_pos >= 0.45:
            score += 8
        else:
            blockers.append("rejected from day high")
    else:
        if range_pos <= 0.20:
            score += 25
            tags.append(f"locked near day low ({range_pos*100:.0f}%)")
        elif range_pos <= (1 - MIN_RANGE_EXTREME):
            score += 18
            tags.append(f"holding lower range ({range_pos*100:.0f}%)")
        elif range_pos <= 0.55:
            score += 8
        else:
            blockers.append("rejected from day low")

    # 3. Move from Open (Max 20 pts)
    if open_move is None:
        tags.append("open move unavailable")
    elif side == "bullish":
        if open_move >= 0.50:
            score += 20
            tags.append(f"above open with control (+{open_move:.2f}%)")
        elif open_move >= 0.15:
            score += 12
            tags.append(f"above open (+{open_move:.2f}%)")
        elif open_move < 0:
            blockers.append("trading below session open")
    else:
        if open_move <= -0.50:
            score += 20
            tags.append(f"below open with control ({open_move:.2f}%)")
        elif open_move <= -0.15:
            score += 12
            tags.append(f"below open ({open_move:.2f}%)")
        elif open_move > 0:
            blockers.append("trading above session open")

    # 4. Open Interest Setup & Squeeze Potential (Max 20 pts)
    if side == "bullish":
        if setup == "Long buildup":
            score += 20
            tags.append("fresh long buildup")
        elif setup == "Short covering":
            if activity_ratio >= 1.4 or (pct_change is not None and pct_change >= 2.0):
                score += 20
                tags.append("explosive short squeeze")
            else:
                score += 15
                tags.append("short covering rally")
        else:
            blockers.append("OI setup not bullish")
    else:
        if setup == "Short buildup":
            score += 20
            tags.append("fresh short buildup")
        elif setup == "Long unwinding":
            if activity_ratio >= 1.4 or (pct_change is not None and pct_change <= -2.0):
                score += 20
                tags.append("heavy long liquidation")
            else:
                score += 15
                tags.append("long unwinding slide")
        else:
            blockers.append("OI setup not bearish")

    if oi_change_percent >= 15:
        score += 5
        tags.append("OI expansion")
    elif oi_change_percent >= 6:
        score += 3

    # 5. Price Momentum (Max 10 pts)
    if pct_change is None:
        blockers.append("price change unavailable")
    elif side == "bullish":
        if pct_change >= 2.5:
            score += 10
            tags.append("strong price expansion")
        elif pct_change >= 1.2:
            score += 7
            tags.append("clean price momentum")
        elif pct_change >= MIN_BULLISH_CHANGE:
            score += 4
        else:
            blockers.append(f"price move below +{MIN_BULLISH_CHANGE:.2f}%")
    else:
        if pct_change <= -2.5:
            score += 10
            tags.append("strong downside expansion")
        elif pct_change <= -1.2:
            score += 7
            tags.append("clean downside momentum")
        elif pct_change <= MIN_BEARISH_CHANGE:
            score += 4
        else:
            blockers.append(f"price move above {MIN_BEARISH_CHANGE:.2f}%")

    # 6. Index Context Score (Max 8 pts)
    note, index_score = index_context_note(row, side, index_context)
    score += index_score
    tags.append(note)

    score_int = int(round(max(0, min(100, score))))
    is_priority = score_int >= 75 and (range_pos is None or range_pos >= 0.55 if side == "bullish" else range_pos <= 0.45) and (activity_ratio >= 0.8 or score_int >= 85)
    one_side = score_int >= MIN_ONE_SIDE_SCORE and not blockers
    status = "Priority Rally" if (one_side and is_priority) else "Rally Watch" if one_side else "Filtered"
    rally_type = "One-side bullish rally" if side == "bullish" else "One-side bearish slide"

    return {
        "score": score_int,
        "status": status,
        "rally_type": rally_type,
        "range_position_percent": None if range_pos is None else round(range_pos * 100, 1),
        "move_from_open_percent": None if open_move is None else round(open_move, 2),
        "index_context": note,
        "quality_tags": tags,
        "blockers": blockers,
        "is_one_side": one_side,
    }


def build_trade_levels(row: dict[str, Any], *, side: str) -> tuple[float | None, float | None, float | None, str]:
    ltp = row.get("ltp")
    high = row.get("high")
    low = row.get("low")
    open_price = row.get("open")
    if ltp is None:
        return None, None, None, "Real LTP missing; do not trade."

    buffer = max(round(ltp * 0.0008, 2), 0.5)
    if side == "bullish":
        entry = round((high if high is not None else ltp) + buffer, 2)
        base_stop = low if low is not None else min(open_price or ltp, ltp - buffer)
        risk = max(entry - base_stop, max(ltp * 0.004, 3.0))
        stop = round(entry - risk, 2)
        target = round(entry + (risk * 1.7), 2)
        plan = "Buy only if price breaks above the morning high and holds above it."
    else:
        entry = round((low if low is not None else ltp) - buffer, 2)
        base_stop = high if high is not None else max(open_price or ltp, ltp + buffer)
        risk = max(base_stop - entry, max(ltp * 0.004, 3.0))
        stop = round(entry + risk, 2)
        target = round(entry - (risk * 1.7), 2)
        plan = "Sell only if price breaks below the morning low and holds below it."
    return entry, stop, target, plan


def status_from_score(score: int, setup: str) -> str:
    if score >= 75:
        return "Priority Rally"
    if score >= MIN_ONE_SIDE_SCORE:
        return "Rally Watch"
    if "not confirmed" in setup.lower():
        return "Need OI"
    return "Filtered"


def build_suggestions(
    rows: list[dict[str, Any]],
    oi_by_symbol: dict[str, dict[str, Any]],
    *,
    side: str,
    top: int,
    index_context: dict[str, dict[str, Any]],
) -> tuple[list[Suggestion], int]:
    matched_rows = [row for row in rows if row["symbol"] in oi_by_symbol and row["symbol"] not in INDEX_LIKE_SYMBOLS]
    
    # Calculate activities across candidates to compute true relative volume (RVOL)
    activities = [calc_stock_activity(r, oi_by_symbol.get(r["symbol"])) for r in matched_rows]
    pos_acts = sorted([a for a in activities if a > 0])
    if pos_acts:
        mid = len(pos_acts) // 2
        median_act = pos_acts[mid] if len(pos_acts) % 2 else (pos_acts[mid - 1] + pos_acts[mid]) / 2.0
    else:
        median_act = 1.0

    # Fetch 5-minute candles concurrently for all candidate symbols
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    candidate_symbols = [r["symbol"] for r in matched_rows]
    if candidate_symbols:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(candidate_symbols), 8)) as executor:
            future_to_sym = {executor.submit(fetch_5m_candles_for_symbol, sym): sym for sym in candidate_symbols}
            try:
                for future in concurrent.futures.as_completed(future_to_sym, timeout=12.0):
                    sym = future_to_sym[future]
                    try:
                        candles_by_symbol[sym] = future.result()
                    except Exception:
                        candles_by_symbol[sym] = []
            except Exception:
                pass  # Gracefully handle any slow timeouts without crashing

            for future, sym in future_to_sym.items():
                if sym not in candles_by_symbol:
                    if future.done() and not future.cancelled():
                        try:
                            candles_by_symbol[sym] = future.result()
                        except Exception:
                            candles_by_symbol[sym] = []
                    else:
                        candles_by_symbol[sym] = []

    suggestions: list[Suggestion] = []
    for row in matched_rows:
        symbol = row["symbol"]
        oi = oi_by_symbol[symbol]
        row_act = calc_stock_activity(row, oi)
        activity_ratio = (row_act / median_act) if median_act > 0 else 1.0
        vc_ranking = round(max(0.1, activity_ratio), 2)

        setup = classify_oi(row.get("percent_change"), oi.get("oi_change"))
        base_score = calc_score(row, oi, side=side, activity_ratio=activity_ratio)
        rally = assess_one_side_rally(row, oi, side=side, activity_ratio=activity_ratio, index_context=index_context)
        if not rally["is_one_side"]:
            continue

        # 5-Minute Breakout Analysis
        sym_candles = candles_by_symbol.get(symbol, [])
        bo_info = analyze_5m_breakout(sym_candles, side, row, activity_ratio)
        five_min_status = bo_info["status"]
        breakout_time = bo_info["breakout_time"]
        is_breakout = bo_info["is_breakout"]

        # If it dropped below PML (Bullish) or above PMH (Bearish), it failed its trend.
        if bo_info.get("is_failed_trend"):
            continue

        # Boost score and tag if active 5m breakout / retest is confirmed
        rally_score = rally["score"]
        quality_tags = list(rally["quality_tags"])
        retest_stat = bo_info.get("retest_status", "Initial")
        if retest_stat == "Confirmed":
            rally_score = min(100, rally_score + 10)
            quality_tags.insert(0, f"Retest Confirmed ({breakout_time})")
        elif retest_stat == "Fresh":
            rally_score = min(100, rally_score + 10)
            quality_tags.insert(0, f"⚡ Fresh Breakout ({breakout_time})")
        elif retest_stat == "Extended":
            quality_tags.insert(0, f"Extended ({breakout_time}) - Wait Retest")
        elif is_breakout:
            rally_score = min(100, rally_score + 8)
            quality_tags.insert(0, f"5m Breakout ({breakout_time or 'Active'})")
        elif "retest" in five_min_status.lower():
            quality_tags.insert(0, five_min_status)
        elif "near" in five_min_status.lower():
            quality_tags.insert(0, "Near 5m Breakout")

        entry, stop, target, plan = build_trade_levels(row, side=side)
        oi_change_percent = oi.get("oi_change_percent")
        oi_change = oi.get("oi_change")
        display_volume = row.get("volume") or oi.get("volume_contracts")
        
        # Calculate Momentum Points (M.P.)
        trend = abs(row.get("percent_change") or 0) * 0.5
        volatility = abs(oi_change_percent or 0) * 0.08
        liquidity = vc_ranking * 1.4
        mp_score = round(trend + volatility + liquidity, 2)

        reason_parts = [
            five_min_status,
            rally["rally_type"],
            setup,
            f"price move {row.get('percent_change'):+.2f}%" if row.get("percent_change") is not None else "price move unavailable",
        ]
        if breakout_time:
            reason_parts.append(f"BO time {breakout_time}")
        if rally.get("move_from_open_percent") is not None:
            reason_parts.append(f"from open {rally['move_from_open_percent']:+.2f}%")
        if rally.get("range_position_percent") is not None:
            reason_parts.append(f"range hold {rally['range_position_percent']:.1f}%")
        if oi_change_percent is not None:
            reason_parts.append(f"OI change {oi_change_percent:+.2f}%")
        else:
            reason_parts.append("OI change unavailable")
        reason_parts.append(str(rally["index_context"]))
        if display_volume is not None:
            reason_parts.append(f"volume {int(display_volume):,}")

        suggestions.append(
            Suggestion(
                symbol=symbol,
                side="GAINER" if side == "bullish" else "LOSER",
                rank=0,
                setup=setup,
                trade_plan=plan,
                ltp=row.get("ltp"),
                percent_change=row.get("percent_change"),
                oi_change_percent=oi_change_percent,
                oi_change=oi_change,
                volume=display_volume,
                value_lakhs=row.get("value_lakhs"),
                entry=entry,
                stop_loss=stop,
                target=target,
                score=max(base_score, rally_score),
                one_side_rally_score=rally_score,
                rally_type=rally["rally_type"],
                range_position_percent=rally["range_position_percent"],
                move_from_open_percent=rally["move_from_open_percent"],
                index_context=rally["index_context"],
                quality_tags=quality_tags,
                status="Priority Rally" if (is_breakout and rally_score >= 75) else (rally["status"] or status_from_score(rally_score, setup)),
                reason=" | ".join(reason_parts),
                vc_ranking=vc_ranking,
                mp_score=mp_score,
                five_min_status=five_min_status,
                breakout_time=breakout_time,
                is_breakout=is_breakout,
                chart_structure=bo_info.get("chart_structure", "Base Building"),
                rvol_5m=bo_info.get("rvol_5m", 1.0),
                ema_trend=bo_info.get("ema_trend", "neutral"),
                vwap=bo_info.get("vwap"),
                retest_status=bo_info.get("retest_status", "Initial"),
            )
        )

    # Priority Sort:
    # Tier 1 (Top): Active 5-Minute Breakouts (is_breakout=True)
    # Tier 2 (Middle): Near Breakouts (Near BO / Near BD)
    # Tier 3 (Base): Other valid candidates
    # Within each tier: sorted by Volume Multiplier (VC Ranking) × Rally Score descending, then % Change
    # Sort strictly by Percentage Change (Highest % gainers on top for Bullish, Largest % drop on top for Bearish)
    if side == "bullish":
        suggestions.sort(key=lambda item: item.percent_change if item.percent_change is not None else -9999, reverse=True)
    else:
        suggestions.sort(key=lambda item: item.percent_change if item.percent_change is not None else 9999)

    ranked = suggestions[:top]
    return [
        Suggestion(**{**item.to_dict(), "rank": index})
        for index, item in enumerate(ranked, start=1)
    ], len(matched_rows)


def phase_for_time(now_ist: datetime) -> str:
    current = now_ist.time()
    if current < MARKET_OPEN:
        return "Pre-open: waiting for NSE 09:15 data"
    if MARKET_OPEN <= current < SUGGESTION_READY:
        return "Building opening scan: analyze until 10:00"
    if SUGGESTION_READY <= current <= MARKET_CLOSE:
        return "Suggestions ready for today's session"
    return "Market closed: today's list is archived/reset on next session"


def build_payload(top: int) -> dict[str, Any]:
    now_ist = datetime.now(INDIA_TZ)
    errors: list[str] = []
    gainers, losers, variation_errors = fetch_top_gainers_losers()
    errors.extend(variation_errors)
    oi_by_symbol, oi_errors = fetch_change_in_oi()
    errors.extend(oi_errors)
    index_context, index_errors = fetch_index_context()
    errors.extend(index_errors)

    bullish, gainer_stock_overlap = build_suggestions(gainers, oi_by_symbol, side="bullish", top=top, index_context=index_context)
    bearish, loser_stock_overlap = build_suggestions(losers, oi_by_symbol, side="bearish", top=top, index_context=index_context)
    gainers_with_oi = sum(1 for row in gainers if row["symbol"] in oi_by_symbol and row["symbol"] not in INDEX_LIKE_SYMBOLS)
    losers_with_oi = sum(1 for row in losers if row["symbol"] in oi_by_symbol and row["symbol"] not in INDEX_LIKE_SYMBOLS)
    oi_matched = gainers_with_oi + losers_with_oi

    return {
        "ok": True,
        "data_mode": "real",
        "generated_at_ist": now_ist.isoformat(timespec="seconds"),
        "session_date": now_ist.date().isoformat(),
        "market_clock_ist": now_ist.strftime("%H:%M:%S"),
        "analysis_phase": phase_for_time(now_ist),
        "strategy_name": "One-Side Rally Stock Suggestion Index",
        "strategy_rules": [
            "Use only stocks that appear in both NSE F&O Top 20 Gainers/Losers and Change in Open Interest.",
            "Show only one-side rally candidates: price must hold near the day high/low, move from open, and align with OI.",
            "Cross-check NIFTY and BANKNIFTY context for market support or relative strength/weakness.",
            "Trade only after the stock breaks its morning high/low; do not chase before breakout.",
            "Today-only list; refresh creates a new snapshot and the next session resets the view.",
        ],
        "source": {
            "top_gainers_losers": f"{NSE_BASE}/market-data/top-gainers-losers",
            "change_in_oi": f"{NSE_BASE}/market-data/oi-spurts",
            "index_context": f"{NSE_BASE}/market-data/live-equity-market",
        },
        "index_context": index_context,
        "coverage": {
            "gainers_loaded": len(gainers),
            "losers_loaded": len(losers),
            "oi_symbols_loaded": len(oi_by_symbol),
            "gainers_with_oi_match": gainers_with_oi,
            "losers_with_oi_match": losers_with_oi,
            "top_rows_with_oi_match": oi_matched,
            "gainer_stock_overlap_before_rally_filter": gainer_stock_overlap,
            "loser_stock_overlap_before_rally_filter": loser_stock_overlap,
            "max_suggestions_per_side": top,
            "one_side_min_score": MIN_ONE_SIDE_SCORE,
        },
        "summary": {
            "bullish_candidates": len(bullish),
            "bearish_candidates": len(bearish),
            "priority_bullish": sum(1 for item in bullish if item.status == "Priority Rally"),
            "priority_bearish": sum(1 for item in bearish if item.status == "Priority Rally"),
        },
        "bullish": [item.to_dict() for item in bullish],
        "bearish": [item.to_dict() for item in bearish],
        "warnings": errors,
    }


def error_payload(exc: Exception) -> dict[str, Any]:
    now_ist = datetime.now(INDIA_TZ)
    return {
        "ok": False,
        "data_mode": "real",
        "generated_at_ist": now_ist.isoformat(timespec="seconds"),
        "session_date": now_ist.date().isoformat(),
        "market_clock_ist": now_ist.strftime("%H:%M:%S"),
        "analysis_phase": phase_for_time(now_ist),
        "error": "real_nse_data_unavailable",
        "message": str(exc),
        "bullish": [],
        "bearish": [],
        "summary": {
            "bullish_candidates": 0,
            "bearish_candidates": 0,
            "priority_bullish": 0,
            "priority_bearish": 0,
        },
        "coverage": {
            "gainers_loaded": 0,
            "losers_loaded": 0,
            "oi_symbols_loaded": 0,
            "gainers_with_oi_match": 0,
            "losers_with_oi_match": 0,
            "top_rows_with_oi_match": 0,
            "gainer_stock_overlap_before_rally_filter": 0,
            "loser_stock_overlap_before_rally_filter": 0,
            "max_suggestions_per_side": DEFAULT_SUGGESTIONS_PER_SIDE,
            "one_side_min_score": MIN_ONE_SIDE_SCORE,
        },
        "index_context": {},
    }


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    try:
        payload = build_payload(args.top)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - the UI needs a clear live-data status
        payload = error_payload(exc)
        exit_code = 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
