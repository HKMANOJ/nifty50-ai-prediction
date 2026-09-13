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
import gzip
import http.cookiejar
import json
import math
import sys
import time as _time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta
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
# A stock trading at <=0.5x its own normal volume (vs the day's median
# activity) is excluded entirely - too little real participation behind the
# move to trust it, regardless of how big the %-change looks.
MIN_VC_RANKING = 0.5
INDEX_LIKE_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
# "Daily Top 5" multi-factor picks: the system waits until this clock time
# for the opening range to settle, then re-locks a fresh 5-per-side pick
# every DAILY_TOP5_WINDOW_MINUTES - stable within a window (no per-scan
# flicker) but rolling across the day, so a late breakout (e.g. 2:40 PM)
# gets its own window instead of being invisible for the whole session.
DAILY_TOP5_LOCK_TIME = time(9, 30)
DAILY_TOP5_WINDOW_MINUTES = 15
# A stock whose own breakout happened after this clock time is not considered
# for Daily Top 5 at all (matches the auto-scanner's own 2:45 PM cutoff) -
# too late in the session to act on for this feature's purpose.
DAILY_TOP5_MAX_BREAKOUT_MINUTES = 14 * 60 + 45  # 2:45 PM IST
# Sector map for the "Sector Alignment" factor - mirrors SECTOR_MAP in
# MLAIStockV2.html (kept in sync manually; both sides use it only to group
# stocks for a same-sector agreement check, not for anything load-bearing).
SECTOR_MAP: dict[str, str] = {
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT", "LTIM": "IT",
    "MPHASIS": "IT", "COFORGE": "IT", "PERSISTENT": "IT", "LTTS": "IT", "KPITTECH": "IT",
    "BIRLASOFT": "IT", "HEXAWARE": "IT",
    "HDFCBANK": "Banks", "ICICIBANK": "Banks", "KOTAKBANK": "Banks", "AXISBANK": "Banks",
    "SBIN": "Banks", "INDUSINDBK": "Banks", "BANDHANBNK": "Banks", "IDFCFIRSTB": "Banks",
    "FEDERALBNK": "Banks", "RBLBANK": "Banks", "AUBANK": "Banks", "PNB": "Banks",
    "CANBK": "Banks", "UNIONBANK": "Banks",
    "BAJFINANCE": "Fin Serv", "BAJAJFINSV": "Fin Serv", "MUTHOOTFIN": "Fin Serv",
    "CHOLAFIN": "Fin Serv", "IIFL": "Fin Serv", "ABCAPITAL": "Fin Serv",
    "MANAPPURAM": "Fin Serv", "MOTILALOFS": "Fin Serv", "ANGELONE": "Fin Serv",
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma", "DIVISLAB": "Pharma",
    "APOLLOHOSP": "Pharma", "LUPIN": "Pharma", "BIOCON": "Pharma", "TORNTPHARM": "Pharma",
    "GLENMARK": "Pharma", "AUROPHARMA": "Pharma", "ABBOTINDIA": "Pharma", "SAGILITY": "Pharma",
    "TATAMOTORS": "Auto", "MARUTI": "Auto", "EICHERMOT": "Auto", "BAJAJ-AUTO": "Auto",
    "HEROMOTOCO": "Auto", "M&M": "Auto", "TVSMOTOR": "Auto", "ASHOKLEY": "Auto",
    "TATASTEEL": "Metal", "HINDALCO": "Metal", "JSWSTEEL": "Metal", "SAIL": "Metal",
    "VEDL": "Metal", "NMDC": "Metal", "HINDZINC": "Metal", "NATIONALUM": "Metal",
    "RELIANCE": "Energy", "BPCL": "Energy", "ONGC": "Energy", "IOC": "Energy", "COALINDIA": "Energy",
    "HINDPETRO": "Energy", "GAIL": "Energy", "PETRONET": "Energy", "MGL": "Energy",
    "ATGL": "Energy", "ADANIGREEN": "Energy", "TATAPOWER": "Energy",
    "HINDUNILVR": "FMCG", "NESTLEIND": "FMCG", "BRITANNIA": "FMCG", "DABUR": "FMCG",
    "MARICO": "FMCG", "GODREJCP": "FMCG", "ITC": "FMCG", "COLPAL": "FMCG",
    "TATACONSUM": "FMCG", "EMAMILTD": "FMCG",
    "LT": "Infra", "LODHA": "Infra", "DLF": "Infra",
    "GODREJPROP": "Infra", "PRESTIGE": "Infra", "PHOENIXLTD": "Infra",
    "ULTRACEMCO": "Infra", "AMBUJACEM": "Infra", "SHREECEM": "Infra",
    "SIEMENS": "Capital Goods", "ABB": "Capital Goods", "BHEL": "Capital Goods",
    "KAYNES": "Capital Goods",
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "INDUSTOWER": "Telecom",
    "ZEEL": "Media", "PVRINOX": "Media",
    "CONCOR": "Logistics", "IRFC": "Logistics", "IRCTC": "Logistics", "ADANIPORTS": "Logistics",
    "NTPC": "Power", "POWERGRID": "Power", "TORNTPOWER": "Power",
    "CESC": "Power", "ADANIPOWER": "Power",
    "ATUL": "Chemicals", "PIDILITIND": "Chemicals", "NAVINFLUOR": "Chemicals",
    "HAL": "Defence", "BEL": "Defence",
    "ZOMATO": "Internet", "POLICYBZR": "Internet", "NYKAA": "Internet", "PAYTM": "Internet",
    "DMART": "Retail", "TRENT": "Retail",
    "ATHERENERG": "EV/Energy", "OLECTRA": "EV/Energy",
}


def get_sector(symbol: str) -> str:
    return SECTOR_MAP.get(symbol, "Other")
# Any stock with a lot size above this is excluded entirely - too much
# capital per contract to be a practical suggestion.
MAX_ALLOWED_LOT_SIZE = 1000
# Every F&O stock with a lot size below this is scanned directly every cycle,
# independent of NSE's top-20 gainers/losers or its OI-spurts feed.
LOT_SCAN_THRESHOLD = 1000
FO_LOT_SIZES_PATH = INPUT_DIR / "fo_lot_sizes.csv"
_FO_LOT_SIZES_CACHE: dict[str, int] | None = None

# 5-minute candle fetch tuning.
# Candles only change every 5 minutes, so a short TTL cache lets back-to-back
# refreshes (manual "Refresh Live" spam, or bullish+bearish in the same scan)
# reuse the same download instead of re-hitting the slow upstream chart API.
CANDLE_CACHE_TTL_SECONDS = 90
CANDLE_FETCH_TIMEOUT = 3.5          # per-request socket timeout
CANDLE_POOL_WAIT_SECONDS = 9.0     # hard cap on the whole batch; stragglers are abandoned
CANDLE_POOL_MAX_WORKERS = 24
# The live refresh runs this module as a fresh subprocess each time, so the
# candle cache is persisted to disk (keyed by symbol -> [epoch, candles]) to
# survive between runs and let back-to-back refreshes skip the upstream fetch.
CANDLE_CACHE_PATH = INPUT_DIR / "candle_cache.json"
_CANDLE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CANDLE_CACHE_LOADED = False


def _load_candle_cache() -> None:
    global _CANDLE_CACHE_LOADED
    if _CANDLE_CACHE_LOADED:
        return
    _CANDLE_CACHE_LOADED = True
    try:
        raw = json.loads(CANDLE_CACHE_PATH.read_text(encoding="utf-8"))
        cutoff = _time.time() - CANDLE_CACHE_TTL_SECONDS
        for sym, entry in raw.items():
            if isinstance(entry, list) and len(entry) == 2 and entry[0] >= cutoff:
                _CANDLE_CACHE[sym] = (float(entry[0]), entry[1])
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - a bad cache file must never break a scan
        print(f"candle cache load skipped: {exc}", file=sys.stderr)


def _save_candle_cache() -> None:
    try:
        cutoff = _time.time() - CANDLE_CACHE_TTL_SECONDS
        fresh = {s: [ts, c] for s, (ts, c) in _CANDLE_CACHE.items() if ts >= cutoff}
        tmp = CANDLE_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fresh), encoding="utf-8")
        tmp.replace(CANDLE_CACHE_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"candle cache save skipped: {exc}", file=sys.stderr)


def load_fo_lot_sizes() -> dict[str, int]:
    """Loads and caches NSE F&O contract market lot sizes from inputs/fo_lot_sizes.csv."""
    global _FO_LOT_SIZES_CACHE
    if _FO_LOT_SIZES_CACHE is not None:
        return _FO_LOT_SIZES_CACHE

    lot_sizes: dict[str, int] = {}
    if FO_LOT_SIZES_PATH.exists():
        try:
            with open(FO_LOT_SIZES_PATH, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    c = {k.strip(): (v.strip() if v else "") for k, v in r.items() if k}
                    sym = c.get("SYMBOL") or c.get("Symbol")
                    if not sym:
                        continue
                    for col in ["SEP-26", "OCT-26", "NOV-26", "DEC-26"]:
                        val = c.get(col)
                        if val:
                            try:
                                lot_sizes[sym.strip().upper()] = int(float(val))
                                break
                            except (ValueError, TypeError):
                                pass
        except Exception as e:
            print(f"Error loading fo_lot_sizes.csv: {e}", file=sys.stderr)
    _FO_LOT_SIZES_CACHE = lot_sizes
    return _FO_LOT_SIZES_CACHE


def get_fo_lot_size(symbol: str) -> int:
    """Returns the market lot size for an F&O symbol (0 if unknown)."""
    return load_fo_lot_sizes().get(symbol.strip().upper(), 0)


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
    lot_size: int = 0
    bell_rang: bool = False
    day_range_percent: float | None = None
    prev_close: float | None = None

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
            "lot_size": self.lot_size,
            "bell_rang": self.bell_rang,
            "day_range_percent": self.day_range_percent,
            "prev_close": self.prev_close,
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


# Shared NSE session (cookie jar + opener) so the anti-bot handshake is done
# once every few minutes instead of on every request. Uses urllib only - no
# dependency on a `curl` binary (which is absent from slim container images).
_NSE_OPENER: "urllib.request.OpenerDirector | None" = None
_NSE_WARMED_AT = 0.0
_NSE_WARM_TTL = 180.0


def _nse_opener() -> "urllib.request.OpenerDirector":
    global _NSE_OPENER
    if _NSE_OPENER is None:
        jar = http.cookiejar.CookieJar()
        _NSE_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return _NSE_OPENER


def _nse_fetch(url: str, *, referer: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Referer": referer,
    })
    with _nse_opener().open(req, timeout=timeout) as resp:
        raw = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw


def _nse_warm(referer: str, *, force: bool = False) -> None:
    global _NSE_WARMED_AT
    now = _time.time()
    if not force and (now - _NSE_WARMED_AT) < _NSE_WARM_TTL:
        return
    try:
        _nse_fetch(NSE_BASE, referer=referer, timeout=15.0)
        _NSE_WARMED_AT = _time.time()
    except Exception:
        _NSE_WARMED_AT = 0.0


def nse_get_json(url: str, *, referer: str) -> Any:
    last_err: Exception | None = None
    for attempt in range(3):
        _nse_warm(referer, force=(attempt > 0))
        try:
            raw = _nse_fetch(url, referer=referer, timeout=25.0)
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LiveDataError(f"NSE returned non-JSON for {url}: {raw[:120]!r}") from exc
        except Exception as exc:  # noqa: BLE001 - retry with a fresh handshake
            last_err = exc
            _time.sleep(0.4 * (attempt + 1))
    raise LiveDataError(str(last_err) or f"NSE request failed: {url}")


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
    # Only the current session is needed for the opening-range breakout logic, so
    # ask for range=1d (a much smaller payload) unless a caller wants full history.
    chart_range = "5d" if full_range else "1d"
    url = f"{YAHOO_CHART_BASE}/{urllib.parse.quote(yahoo_sym)}?interval=5m&range={chart_range}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://finance.yahoo.com/quote/{yahoo_sym}/chart",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CANDLE_FETCH_TIMEOUT) as resp:
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
        print(f"fetch_5m_candles_for_symbol error for {symbol}: {type(e)} {e}", file=sys.stderr)
        return []


def fetch_quote_and_candles_for_symbol(symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """For a symbol that ISN'T in NSE's top-20 gainers/losers: build the same
    row shape (open/high/low/ltp/percent_change/volume) directly from Yahoo's
    intraday chart in one request, using its `previousClose` in the response
    metadata - no dependency on the top-20 feed at all. Used to widen the
    candidate pool beyond NSE's arbitrary top-20 cutoff."""
    yahoo_sym = f"{symbol}.NS"
    url = f"{YAHOO_CHART_BASE}/{urllib.parse.quote(yahoo_sym)}?interval=5m&range=1d"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://finance.yahoo.com/quote/{yahoo_sym}/chart",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CANDLE_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = (data.get("chart") or {}).get("result")
        if not result:
            return None
        quote = result[0]
        meta = quote.get("meta") or {}
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        timestamps = quote.get("timestamp") or []
        indicators = quote.get("indicators", {})
        quote_data = (indicators.get("quote") or [{}])[0]
        opens = quote_data.get("open") or []
        highs = quote_data.get("high") or []
        lows = quote_data.get("low") or []
        closes = quote_data.get("close") or []
        volumes = quote_data.get("volume") or []
        candles: list[dict[str, Any]] = []
        for i, ts in enumerate(timestamps):
            if i < len(opens) and i < len(highs) and i < len(lows) and i < len(closes):
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                if None not in (o, h, l, c):
                    v = volumes[i] if i < len(volumes) else 0
                    candles.append({
                        "timestamp": int(ts), "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c), "volume": float(v or 0),
                    })
        if not candles or not prev_close:
            return None

        ltp = candles[-1]["close"]
        pct = ((ltp - prev_close) / prev_close) * 100
        row = {
            "symbol": symbol,
            "side": "gainer" if pct >= 0 else "loser",
            "open": candles[0]["open"],
            "high": max(c["high"] for c in candles),
            "low": min(c["low"] for c in candles),
            "prev_close": float(prev_close),
            "ltp": ltp,
            "percent_change": pct,
            "volume": sum(c["volume"] for c in candles),
            "value_lakhs": None,
        }
        return row, candles
    except Exception as e:
        print(f"fetch_quote_and_candles_for_symbol error for {symbol}: {type(e)} {e}", file=sys.stderr)
        return None


def fetch_extra_candidates(
    symbols: Iterable[str],
    candles_by_symbol: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Widen the candidate pool beyond NSE's top-20 gainers/losers: for every
    OI-spurts symbol not already in that top-20, fetch its own quote+candles
    directly and build a synthetic row in the same shape the top-20 feed
    produces. These rows then go through the EXACT SAME scoring/blocker
    pipeline as top-20 candidates - no lowered bar, just a wider net, so a
    real breakout is never invisible purely because its day-change hasn't
    cleared an arbitrary top-20 cutoff yet."""
    symbols = list(symbols)
    if not symbols:
        return []
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(symbols), CANDLE_POOL_MAX_WORKERS)) as executor:
        future_to_sym = {executor.submit(fetch_quote_and_candles_for_symbol, sym): sym for sym in symbols}
        try:
            for future in concurrent.futures.as_completed(future_to_sym, timeout=CANDLE_POOL_WAIT_SECONDS):
                sym = future_to_sym[future]
                try:
                    result = future.result()
                except Exception:
                    result = None
                if result:
                    row, candles = result
                    rows.append(row)
                    candles_by_symbol[sym] = candles
                    if candles:
                        _CANDLE_CACHE[sym] = (_time.time(), candles)
        except concurrent.futures.TimeoutError:
            pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    return rows


def bulk_fetch_candles(
    symbols: Iterable[str],
    candles_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Populate ``candles_by_symbol`` for every requested symbol.

    Symbols already present in the dict are left alone (so the two sides of a
    scan share one download). Fresh symbols are served from a short-TTL process
    cache when possible, otherwise fetched concurrently. The whole batch is
    capped at ``CANDLE_POOL_WAIT_SECONDS``; symbols whose request has not
    returned by then are recorded as an empty list and the workers abandoned,
    so one slow upstream response can no longer stall the entire refresh.
    """
    _load_candle_cache()
    now = _time.time()
    pending: list[str] = []
    for sym in symbols:
        if sym in candles_by_symbol:
            continue
        cached = _CANDLE_CACHE.get(sym)
        if cached and (now - cached[0]) <= CANDLE_CACHE_TTL_SECONDS:
            candles_by_symbol[sym] = cached[1]
        else:
            pending.append(sym)

    if not pending:
        return candles_by_symbol

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(pending), CANDLE_POOL_MAX_WORKERS)
    )
    future_to_sym = {executor.submit(fetch_5m_candles_for_symbol, sym): sym for sym in pending}
    try:
        for future in concurrent.futures.as_completed(future_to_sym, timeout=CANDLE_POOL_WAIT_SECONDS):
            sym = future_to_sym[future]
            try:
                result = future.result()
            except Exception:
                result = []
            candles_by_symbol[sym] = result
            if result:
                _CANDLE_CACHE[sym] = (_time.time(), result)
    except concurrent.futures.TimeoutError:
        pass
    except Exception:
        pass
    finally:
        # Do not block on stragglers - abandon any request still in flight.
        executor.shutdown(wait=False, cancel_futures=True)

    for sym in pending:
        candles_by_symbol.setdefault(sym, [])
    _save_candle_cache()
    return candles_by_symbol


def analyze_5m_breakout(candles: list[dict[str, Any]], side: str, row: dict[str, Any]) -> dict[str, Any]:
    """Analyzes 5-minute candles using a strict 5-Minute Opening Range Breakout (ORB) and Continuous Wave Tracking."""
    if len(candles) < 2:
        # No intraday candles came back (upstream slow/blocked). Flag it as its
        # own state instead of "Consolidating" so the row is kept on the list
        # rather than silently dropped - it just is not eligible for a
        # confirmed-breakout badge until candles arrive on a later scan.
        return {
            "is_breakout": False,
            "status": "Awaiting 5m",
            "breakout_time": None,
            "rvol_5m": 1.0,
            "ema_trend": "bullish" if side == "bullish" else "bearish",
            "chart_structure": "Base Building",
            "no_candle_data": True,
        }

    # 1. 9-period EMA on 5-minute closes
    closes = [c["close"] for c in candles]
    k = 2.0 / (9 + 1)
    ema9 = closes[0]
    for price in closes[1:]:
        ema9 = (price * k) + (ema9 * (1.0 - k))

    # 2. Strict 5-Minute Opening Range (The high/low of the very first candle)
    # This prevents blindly calling 09:15 a breakout. The first candle SETS the resistance line.
    first_candle = candles[0]
    morning_high = first_candle["high"]
    morning_low = first_candle["low"]

    # 3. 5-minute Average Volume
    valid_vols = [c["volume"] for c in candles if c["volume"] > 0]
    avg_5m_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 1.0

    latest = candles[-1]
    latest_close = latest["close"]
    latest_vol = latest["volume"]
    latest_rvol = latest_vol / avg_5m_vol if avg_5m_vol > 0 else 1.0

    breakout_time = None
    is_breakout = False
    breakout_status = "Consolidating"
    chart_structure = "Base Building"

    # We only look for breakouts starting from the SECOND candle (09:20 onwards)
    trading_candles = candles[1:]

    if side == "bullish":
        # Check higher highs and higher lows structure on 5m chart
        recent = candles[-min(len(candles), 8):]
        if len(recent) >= 4:
            higher_highs = sum(1 for i in range(1, len(recent)) if recent[i]["high"] >= recent[i-1]["high"])
            higher_lows = sum(1 for i in range(1, len(recent)) if recent[i]["low"] >= recent[i-1]["low"])
            if higher_highs >= len(recent) * 0.6 and higher_lows >= len(recent) * 0.6:
                chart_structure = "Higher Highs Wave"
            elif latest_close >= ema9:
                chart_structure = "Uptrend on EMA9"
            else:
                chart_structure = "Pullback to EMA"

        # Continuous Wave Tracker for the Breakout
        current_wave_start = None
        for c in trading_candles:
            if c["close"] >= morning_high * 0.999: # breached the first candle's high
                if not current_wave_start:
                    current_wave_start = c["timestamp"]
            else:
                current_wave_start = None # reset if it drops back below the first candle
        
        if current_wave_start:
            breakout_time = datetime.fromtimestamp(current_wave_start, tz=INDIA_TZ).strftime("%I:%M %p")

        # Active breakout condition
        if latest_close >= morning_high * 0.998 and latest_close >= ema9:
            is_breakout = True
            breakout_status = "Breakout"
            chart_structure = "Breakout Rally"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
        elif latest_close >= morning_high * 0.992:
            breakout_status = "Near BO"
            chart_structure = "Testing Resistance"

    else:
        # Bearish Breakdown condition
        recent = candles[-min(len(candles), 8):]
        if len(recent) >= 4:
            lower_highs = sum(1 for i in range(1, len(recent)) if recent[i]["high"] <= recent[i-1]["high"])
            lower_lows = sum(1 for i in range(1, len(recent)) if recent[i]["low"] <= recent[i-1]["low"])
            if lower_highs >= len(recent) * 0.6 and lower_lows >= len(recent) * 0.6:
                chart_structure = "Lower Lows Slide"
            elif latest_close <= ema9:
                chart_structure = "Downtrend on EMA9"
            else:
                chart_structure = "Bounce to EMA"

        # Continuous Wave Tracker for the Breakdown
        current_wave_start = None
        for c in trading_candles:
            if c["close"] <= morning_low * 1.001: # breached the first candle's low
                if not current_wave_start:
                    current_wave_start = c["timestamp"]
            else:
                current_wave_start = None # reset if it bounces back above
        
        if current_wave_start:
            breakout_time = datetime.fromtimestamp(current_wave_start, tz=INDIA_TZ).strftime("%I:%M %p")

        # Active breakout condition
        if latest_close <= morning_low * 1.002 and latest_close <= ema9:
            is_breakout = True
            breakout_status = "Breakdown"
            chart_structure = "Breakdown Slide"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
        elif latest_close <= morning_low * 1.008:
            breakout_status = "Near BD"
            chart_structure = "Testing Support"

    is_failed_trend = False
    if side == "bullish" and latest_close < morning_low:
        is_failed_trend = True
    elif side == "bearish" and latest_close > morning_high:
        is_failed_trend = True

    return {
        "is_breakout": is_breakout,
        "status": breakout_status,
        "breakout_time": breakout_time,
        "rvol_5m": round(latest_rvol, 2),
        "ema_trend": "bullish" if latest_close >= ema9 else "bearish",
        "chart_structure": chart_structure,
        "is_failed_trend": is_failed_trend,
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
    is_priority = score_int >= 75 and (range_pos is None or range_pos >= 0.60 if side == "bullish" else range_pos <= 0.40) and activity_ratio >= 1.1
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
    candles_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[Suggestion], int]:
    matched_rows = [
        row for row in rows
        if row["symbol"] in oi_by_symbol
        and row["symbol"] not in INDEX_LIKE_SYMBOLS
        and get_fo_lot_size(row["symbol"]) <= MAX_ALLOWED_LOT_SIZE
    ]
    
    # Calculate activities across candidates to compute true relative volume (RVOL)
    activities = [calc_stock_activity(r, oi_by_symbol.get(r["symbol"])) for r in matched_rows]
    pos_acts = sorted([a for a in activities if a > 0])
    if pos_acts:
        mid = len(pos_acts) // 2
        median_act = pos_acts[mid] if len(pos_acts) % 2 else (pos_acts[mid - 1] + pos_acts[mid]) / 2.0
    else:
        median_act = 1.0

    # Fetch 5-minute candles for all candidate symbols. A caller can pass a
    # shared dict so both sides of one scan reuse a single download; anything
    # missing is filled here (served from the short-TTL cache when possible).
    if candles_by_symbol is None:
        candles_by_symbol = {}
    candidate_symbols = [r["symbol"] for r in matched_rows]
    if candidate_symbols:
        bulk_fetch_candles(candidate_symbols, candles_by_symbol)

    suggestions: list[Suggestion] = []
    for row in matched_rows:
        symbol = row["symbol"]
        oi = oi_by_symbol[symbol]
        row_act = calc_stock_activity(row, oi)
        activity_ratio = (row_act / median_act) if median_act > 0 else 1.0
        vc_ranking = round(max(0.1, activity_ratio), 2)
        if vc_ranking <= MIN_VC_RANKING:
            continue

        setup = classify_oi(row.get("percent_change"), oi.get("oi_change"))
        base_score = calc_score(row, oi, side=side, activity_ratio=activity_ratio)
        rally = assess_one_side_rally(row, oi, side=side, activity_ratio=activity_ratio, index_context=index_context)
        if not rally["is_one_side"]:
            continue

        # 5-Minute Breakout Analysis
        sym_candles = candles_by_symbol.get(symbol, [])
        bo_info = analyze_5m_breakout(sym_candles, side, row)
        five_min_status = bo_info["status"]
        breakout_time = bo_info["breakout_time"]
        is_breakout = bo_info["is_breakout"]

        # If it dropped below PML (Bullish) or above PMH (Bearish), it failed its trend.
        if bo_info.get("is_failed_trend"):
            continue

        # Must be trading beyond today's opening-range high/low (PMH/PML - same
        # first-6-candle level the chart draws in blue) AND beyond yesterday's
        # close - drops a stock still sitting inside this morning's range or
        # still on the wrong side of prev close, even if %-change looks
        # positive/negative on paper. Skipped only when too few candles exist
        # yet to judge (not excluded - same "not yet known" philosophy used
        # elsewhere), not applied as a silent exclusion.
        if len(sym_candles) >= 6:
            orb_check_high = max(c["high"] for c in sym_candles[:6])
            orb_check_low = min(c["low"] for c in sym_candles[:6])
            ltp_check = row.get("ltp")
            prev_close_check = row.get("prev_close")
            if side == "bullish":
                if ltp_check is None or ltp_check < orb_check_high:
                    continue
                if prev_close_check is not None and ltp_check <= prev_close_check:
                    continue
            else:
                if ltp_check is None or ltp_check > orb_check_low:
                    continue
                if prev_close_check is not None and ltp_check >= prev_close_check:
                    continue

        # Only show active Breakout/Breakdown or Near BO/Near BD candidates (no Consolidating)
        if five_min_status.lower() == "consolidating":
            continue

        # Boost score and tag if active 5m breakout is confirmed
        rally_score = rally["score"]
        quality_tags = list(rally["quality_tags"])
        if is_breakout:
            rally_score = min(100, rally_score + 8)
            quality_tags.insert(0, f"5m Breakout ({breakout_time or 'Active'})")
        elif "near" in five_min_status.lower():
            quality_tags.insert(0, "Near 5m Breakout")
        elif bo_info.get("no_candle_data"):
            quality_tags.insert(0, "5m data pending (kept on OI + rally strength)")

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

        status_val = rally["status"] or status_from_score(rally_score, setup)
        # Matches the frontend bell (MLAIStockV2.html checkAndTriggerBellAlerts):
        # rings on confirmed breakout alone, no longer waits for Priority Rally -
        # that used to delay Tracked Breakouts pins by 20-30+ min on real cases.
        # status_val can still separately reach "Priority Rally" later once the
        # score catches up; this flag just no longer waits for it.
        bell_rang = bool(is_breakout and "near" not in five_min_status.lower())

        _high, _low, _prev_close = row.get("high"), row.get("low"), row.get("prev_close")
        day_range_percent = (
            round(((_high - _low) / _prev_close) * 100, 2)
            if (_high is not None and _low is not None and _prev_close) else None
        )

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
                status=status_val,
                reason=" | ".join(reason_parts),
                vc_ranking=vc_ranking,
                mp_score=mp_score,
                five_min_status=five_min_status,
                breakout_time=breakout_time,
                is_breakout=is_breakout,
                chart_structure=bo_info.get("chart_structure", "Base Building"),
                rvol_5m=bo_info.get("rvol_5m", 1.0),
                ema_trend=bo_info.get("ema_trend", "neutral"),
                lot_size=get_fo_lot_size(symbol),
                bell_rang=bell_rang,
                day_range_percent=day_range_percent,
                prev_close=row.get("prev_close"),
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


def _passes_base_filter(symbol: str, oi_by_symbol: dict[str, dict[str, Any]]) -> bool:
    return (
        symbol in oi_by_symbol
        and symbol not in INDEX_LIKE_SYMBOLS
        and get_fo_lot_size(symbol) <= MAX_ALLOWED_LOT_SIZE
    )


# ── Tracked breakouts (pinned once alerted, tracked until a real outcome) ───
# A stock that rings the bell gets pinned here for the rest of the session and
# keeps being checked directly, independent of whether it stays in NSE's live
# top-20 gainers/losers. Pure frozen snapshot, captured once at the moment
# the bell rings - no live re-checking. The trader monitors it manually from
# here on; this is the permanent record of what the alert actually showed.
TRACKED_BREAKOUTS_PATH = INPUT_DIR / "tracked_breakouts.json"
MAX_TRACKED_BREAKOUTS = 20
DAILY_TOP5_STATE_PATH = INPUT_DIR / "daily_top5.json"
INSTANT_TRIGGERS_PATH = INPUT_DIR / "instant_triggers.json"
MAX_INSTANT_TRIGGERS = 40
TOP5_ELIGIBLE_STATE_PATH = INPUT_DIR / "top5_eligible_state.json"


def _load_tracked_state() -> dict[str, Any]:
    try:
        return json.loads(TRACKED_BREAKOUTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"session_date": None, "items": []}


def _save_tracked_state(state: dict[str, Any]) -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        TRACKED_BREAKOUTS_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[tracked] save skipped: {exc}", file=sys.stderr)


def update_tracked_breakouts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        session_date = payload.get("session_date")
        state = _load_tracked_state()
        if state.get("session_date") != session_date:
            state = {"session_date": session_date, "items": []}

        items: list[dict[str, Any]] = state["items"]
        by_key = {(it["symbol"], it["side"]) for it in items}

        # Pin every fresh bell-ringing candidate from this scan that isn't
        # already tracked - a one-time, frozen snapshot of the row exactly as
        # the F&O table showed it at that moment. Never touched again.
        for side_name in ("bullish", "bearish"):
            for row in payload.get(side_name, []) or []:
                if not row.get("bell_rang"):
                    continue
                key = (row.get("symbol"), side_name)
                if key in by_key:
                    continue
                tracked_entry = {
                    "symbol": row.get("symbol"),
                    "side": side_name,
                    "lot_size": row.get("lot_size"),
                    "setup": row.get("setup"),
                    "alert_time_ist": row.get("breakout_time") or payload.get("market_clock_ist"),
                    "alert_price": row.get("ltp"),
                    "percent_change": row.get("percent_change"),
                    "volume": row.get("volume"),
                    "vc_ranking": row.get("vc_ranking"),
                    "mp_score": row.get("mp_score"),
                    "five_min_status": row.get("five_min_status"),
                    "breakout_time": row.get("breakout_time"),
                    "is_breakout": row.get("is_breakout"),
                    "rally_score": row.get("one_side_rally_score"),
                    "action_status": row.get("status"),
                    "entry": row.get("entry"),
                    "stop_loss": row.get("stop_loss"),
                    "target": row.get("target"),
                }
                items.append(tracked_entry)
                _log_event_to_db("tracked_breakout", session_date, row.get("symbol"), side_name, tracked_entry)
                by_key.add(key)

        items = items[-MAX_TRACKED_BREAKOUTS:]
        state["items"] = items
        _save_tracked_state(state)
        return list(reversed(items))  # most recently alerted first
    except Exception as exc:  # noqa: BLE001 - must never break a scan
        print(f"[tracked] update skipped: {exc}", file=sys.stderr)
        return []


def compute_sector_alignment(market_universe: list[dict[str, Any]]) -> dict[str, float]:
    """For each symbol: what % of its own sector peers (in the broader
    OI-confirmed universe) are moving in the SAME direction as it is today.
    High = the whole sector is behind the move, not just one stock alone."""
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for row in market_universe:
        by_sector.setdefault(get_sector(row["symbol"]), []).append(row)

    alignment: dict[str, float] = {}
    for rows in by_sector.values():
        for row in rows:
            own_pct = row.get("percent_change")
            if not own_pct:
                continue
            peers = [r for r in rows if r["symbol"] != row["symbol"] and r.get("percent_change")]
            if not peers:
                alignment[row["symbol"]] = 50.0  # no peers to compare against - neutral
                continue
            same_direction = sum(1 for p in peers if (p["percent_change"] > 0) == (own_pct > 0))
            alignment[row["symbol"]] = round((same_direction / len(peers)) * 100, 1)
    return alignment


def _clamp100(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def score_daily_top5_factors(
    item: Suggestion,
    side: str,
    index_context: dict[str, dict[str, Any]],
    sector_alignment: dict[str, float],
) -> dict[str, Any]:
    """Twelve equal-weighted, transparent factors (no hidden tuning) scored
    0-100 each, from data the scanner already computes for this row. The
    composite (plain average) decides the Daily Top 5 - a separate axis from
    Rank (pure %-change) and Rally Score/Priority Rally (the 6-component,
    blocker-gated score used for Action Status)."""
    is_bullish = side == "bullish"

    trend_score = 100.0 if (item.ema_trend == "bullish") == is_bullish else 0.0
    strength_score = float(item.one_side_rally_score)
    momentum_score = _clamp100(abs(item.move_from_open_percent or 0) / 5 * 100)
    volume_score = _clamp100((item.vc_ranking / 3) * 100)
    velocity_score = _clamp100((item.rvol_5m / 3) * 100)

    nifty_pct = (index_context.get("NIFTY 50") or {}).get("percent_change") or 0.0
    stock_pct = item.percent_change or 0.0
    rs_raw = (stock_pct - nifty_pct) if is_bullish else (nifty_pct - stock_pct)
    relative_strength_score = _clamp100((rs_raw / 3) * 100)

    sector_alignment_score = sector_alignment.get(item.symbol, 50.0)
    market_direction_score = _clamp100(((nifty_pct if is_bullish else -nifty_pct) / 1.0) * 100)
    liquidity_score = _clamp100(((item.value_lakhs or 0) / 5000) * 100)
    volatility_score = _clamp100(((item.day_range_percent or 0) / 4) * 100)

    if item.range_position_percent is None:
        support_resistance_score = 50.0
    else:
        support_resistance_score = (
            item.range_position_percent if is_bullish else (100 - item.range_position_percent)
        )

    st = (item.five_min_status or "").lower()
    if item.is_breakout:
        price_action_score = 100.0
    elif "near" in st:
        price_action_score = 65.0
    else:
        price_action_score = 35.0

    factors = {
        "Trend": trend_score,
        "Strength": strength_score,
        "Momentum": momentum_score,
        "Volume": volume_score,
        "Velocity": velocity_score,
        "Relative Strength": relative_strength_score,
        "Sector Alignment": sector_alignment_score,
        "Market Direction": market_direction_score,
        "Liquidity": liquidity_score,
        "Volatility": volatility_score,
        "Support & Resistance": support_resistance_score,
        "Price Action": price_action_score,
    }
    composite = round(sum(factors.values()) / len(factors), 1)
    return {"factors": factors, "composite": composite}


def compute_daily_top5(
    bullish: list[Suggestion],
    bearish: list[Suggestion],
    index_context: dict[str, dict[str, Any]],
    market_universe: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    sector_alignment = compute_sector_alignment(market_universe)
    result: dict[str, list[dict[str, Any]]] = {}
    for side_name, items in (("bullish", bullish), ("bearish", bearish)):
        scored = []
        for item in items:
            bo_min = _parse_clock_to_minutes(item.breakout_time)
            if bo_min is not None and bo_min > DAILY_TOP5_MAX_BREAKOUT_MINUTES:
                continue  # breakout happened too late in the session - skip
            breakdown = score_daily_top5_factors(item, side_name, index_context, sector_alignment)
            scored.append({
                "symbol": item.symbol,
                "sector": get_sector(item.symbol),
                "lot_size": item.lot_size,
                "ltp": item.ltp,
                "percent_change": item.percent_change,
                "setup": item.setup,
                "breakout_time": item.breakout_time,
                "five_min_status": item.five_min_status,
                "composite_score": breakdown["composite"],
                "factors": breakdown["factors"],
            })
        scored.sort(key=lambda x: x["composite_score"], reverse=True)
        result[side_name] = scored[:5]
    return result


def _current_top5_window_start(now_ist: datetime) -> str | None:
    """The start (HH:MM) of the current DAILY_TOP5_WINDOW_MINUTES-wide window,
    anchored at DAILY_TOP5_LOCK_TIME (09:30). None before that anchor - the
    system is still waiting for the opening range to settle."""
    anchor = now_ist.replace(
        hour=DAILY_TOP5_LOCK_TIME.hour, minute=DAILY_TOP5_LOCK_TIME.minute,
        second=0, microsecond=0,
    )
    if now_ist < anchor:
        return None
    elapsed_min = int((now_ist - anchor).total_seconds() // 60)
    window_index = elapsed_min // DAILY_TOP5_WINDOW_MINUTES
    window_start = anchor + timedelta(minutes=window_index * DAILY_TOP5_WINDOW_MINUTES)
    return window_start.strftime("%H:%M")


def _load_daily_top5_state() -> dict[str, Any]:
    try:
        return json.loads(DAILY_TOP5_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"session_date": None, "window_start_ist": None, "locked_at_ist": None,
                 "top5": {"bullish": [], "bearish": []}}


def _save_daily_top5_state(state: dict[str, Any]) -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        DAILY_TOP5_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[daily_top5] save skipped: {exc}", file=sys.stderr)


def update_daily_top5(
    payload: dict[str, Any],
    bullish: list[Suggestion],
    bearish: list[Suggestion],
    index_context: dict[str, dict[str, Any]],
    market_universe: list[dict[str, Any]],
    now_ist: datetime,
) -> dict[str, Any]:
    """Waits until DAILY_TOP5_LOCK_TIME (09:30 IST) for the opening range to
    settle, then re-locks a fresh 5-per-side pick every
    DAILY_TOP5_WINDOW_MINUTES on the 12-factor scorecard. Stable within a
    window (no per-scan flicker), rolling across the day - so a breakout at
    any time gets its own window instead of being invisible all day."""
    try:
        session_date = payload.get("session_date")
        state = _load_daily_top5_state()
        if state.get("session_date") != session_date:
            state = {"session_date": session_date, "window_start_ist": None, "locked_at_ist": None,
                     "top5": {"bullish": [], "bearish": []}}

        window_start = _current_top5_window_start(now_ist)
        if window_start is not None and window_start != state.get("window_start_ist"):
            state["top5"] = compute_daily_top5(bullish, bearish, index_context, market_universe)
            state["window_start_ist"] = window_start
            state["locked_at_ist"] = now_ist.strftime("%H:%M:%S")
            _save_daily_top5_state(state)
            for side_name in ("bullish", "bearish"):
                for entry in state["top5"].get(side_name, []):
                    _log_event_to_db(
                        "daily_top5", session_date, entry.get("symbol"), side_name,
                        {**entry, "window_start_ist": window_start, "locked_at_ist": state["locked_at_ist"]},
                    )

        window_end_ist = None
        if state.get("window_start_ist"):
            h, m = map(int, state["window_start_ist"].split(":"))
            window_end = now_ist.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(minutes=DAILY_TOP5_WINDOW_MINUTES)
            window_end_ist = window_end.strftime("%H:%M")

        return {
            "locked": state.get("window_start_ist") is not None,
            "locked_at_ist": state.get("locked_at_ist"),
            "window_start_ist": state.get("window_start_ist"),
            "window_end_ist": window_end_ist,
            "window_minutes": DAILY_TOP5_WINDOW_MINUTES,
            "lock_time_ist": DAILY_TOP5_LOCK_TIME.strftime("%H:%M"),
            "bullish": state["top5"]["bullish"],
            "bearish": state["top5"]["bearish"],
        }
    except Exception as exc:  # noqa: BLE001 - must never break a scan
        print(f"[daily_top5] update skipped: {exc}", file=sys.stderr)
        return {"locked": False, "locked_at_ist": None, "window_start_ist": None, "window_end_ist": None,
                "window_minutes": DAILY_TOP5_WINDOW_MINUTES, "lock_time_ist": DAILY_TOP5_LOCK_TIME.strftime("%H:%M"),
                "bullish": [], "bearish": []}


def score_instant_trigger_factors(
    item: Suggestion,
    side: str,
    index_context: dict[str, dict[str, Any]],
    sector_alignment: dict[str, float],
) -> dict[str, Any]:
    """Five INSTANT-only factors - everything knowable the moment a breakout
    candle closes, nothing that needs the day to accumulate. Deliberately
    excludes Strength/Momentum/day-RVOL (those are lagging, see Daily Top 5) -
    this is what lets Instant Triggers fire ~30 min earlier than the Bell,
    at the cost of skipping the "hold above the level" confirmation."""
    is_bullish = side == "bullish"

    st = (item.five_min_status or "").lower()
    if item.is_breakout:
        price_action_score = 100.0
    elif "near" in st:
        price_action_score = 65.0
    else:
        price_action_score = 35.0

    if item.range_position_percent is None:
        support_resistance_score = 50.0
    else:
        support_resistance_score = (
            item.range_position_percent if is_bullish else (100 - item.range_position_percent)
        )

    # Fresh money (Long buildup / Short buildup) tends to hold better than
    # buildup driven by the opposite side closing out (Short covering / Long
    # unwinding), which can fizzle once that closing pressure is done.
    setup = item.setup or ""
    if is_bullish:
        oi_setup_score = 100.0 if setup == "Long buildup" else 55.0 if setup == "Short covering" else 50.0
    else:
        oi_setup_score = 100.0 if setup == "Short buildup" else 55.0 if setup == "Long unwinding" else 50.0

    nifty_pct = (index_context.get("NIFTY 50") or {}).get("percent_change") or 0.0
    market_direction_score = _clamp100(((nifty_pct if is_bullish else -nifty_pct) / 1.0) * 100)
    sector_score = sector_alignment.get(item.symbol, 50.0)
    sector_index_alignment_score = round((market_direction_score + sector_score) / 2, 1)

    volume_surge_score = _clamp100((item.rvol_5m / 3) * 100)

    factors = {
        "Price Action": price_action_score,
        "Support & Resistance": support_resistance_score,
        "OI Setup Quality": oi_setup_score,
        "Sector/Index Alignment": sector_index_alignment_score,
        "Volume Surge (this candle)": volume_surge_score,
    }
    composite = round(sum(factors.values()) / len(factors), 1)
    return {"factors": factors, "composite": composite}


def _load_instant_triggers_state() -> dict[str, Any]:
    try:
        return json.loads(INSTANT_TRIGGERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"session_date": None, "items": []}


def _save_instant_triggers_state(state: dict[str, Any]) -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        INSTANT_TRIGGERS_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[instant_triggers] save skipped: {exc}", file=sys.stderr)


def update_instant_triggers(
    payload: dict[str, Any],
    bullish: list[Suggestion],
    bearish: list[Suggestion],
    index_context: dict[str, dict[str, Any]],
    market_universe: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fires the moment is_breakout flips True - NOT gated by score=100 or
    Priority Rally like the Bell. Scored on 5 instant-only factors. Pinned
    once per (symbol, side) the first time seen this session (same
    once-per-day convention as Tracked Breakouts), then frozen - except for
    an `upgraded_to_priority` flag that flips on later if the slower Rally
    Score independently confirms the same move."""
    try:
        session_date = payload.get("session_date")
        state = _load_instant_triggers_state()
        if state.get("session_date") != session_date:
            state = {"session_date": session_date, "items": []}

        items: list[dict[str, Any]] = state["items"]
        by_key = {(it["symbol"], it["side"]): it for it in items}
        sector_alignment = compute_sector_alignment(market_universe)

        for side_name, candidates in (("bullish", bullish), ("bearish", bearish)):
            for item in candidates:
                key = (item.symbol, side_name)
                existing = by_key.get(key)

                if existing is not None:
                    if not existing.get("upgraded_to_priority") and item.status == "Priority Rally" and item.bell_rang:
                        existing["upgraded_to_priority"] = True
                        existing["upgraded_at_ist"] = payload.get("market_clock_ist")
                    continue

                if not item.is_breakout:
                    continue  # only pin on a confirmed breakout, not Near BO/BD

                breakdown = score_instant_trigger_factors(item, side_name, index_context, sector_alignment)
                is_upgraded = bool(item.status == "Priority Rally" and item.bell_rang)
                new_item = {
                    "symbol": item.symbol,
                    "side": side_name,
                    "lot_size": item.lot_size,
                    "setup": item.setup,
                    "breakout_time": item.breakout_time,
                    "triggered_at_ist": payload.get("market_clock_ist"),
                    "price_at_trigger": item.ltp,
                    "percent_change_at_trigger": item.percent_change,
                    "instant_score": breakdown["composite"],
                    "factors": breakdown["factors"],
                    "rally_score_at_trigger": item.one_side_rally_score,
                    "upgraded_to_priority": is_upgraded,
                    "upgraded_at_ist": payload.get("market_clock_ist") if is_upgraded else None,
                }
                items.append(new_item)
                by_key[key] = new_item
                _log_event_to_db("instant_trigger", session_date, item.symbol, side_name, new_item)

        items = items[-MAX_INSTANT_TRIGGERS:]
        state["items"] = items
        _save_instant_triggers_state(state)
        return list(reversed(items))  # most recently triggered first
    except Exception as exc:  # noqa: BLE001 - must never break a scan
        print(f"[instant_triggers] update skipped: {exc}", file=sys.stderr)
        return []


OUTCOME_TRACKER_STATE_PATH = INPUT_DIR / "outcome_tracker_state.json"
OUTCOME_LOG_DIR = INPUT_DIR / "outcome_log"
OUTCOME_CHECKPOINTS_MINUTES = (15, 30, 60)
MAX_PENDING_OUTCOMES = 200


def _load_outcome_tracker_state() -> dict[str, Any]:
    try:
        return json.loads(OUTCOME_TRACKER_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"session_date": None, "items": []}


def _save_outcome_tracker_state(state: dict[str, Any]) -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        OUTCOME_TRACKER_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[outcome_tracker] save skipped: {exc}", file=sys.stderr)


def _flush_outcome_log(session_date: str | None, items: list[dict[str, Any]]) -> None:
    if not session_date or not items:
        return
    try:
        OUTCOME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTCOME_LOG_DIR / f"{session_date}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it) + "\n")
                _log_event_to_db("outcome", session_date, it.get("symbol"), it.get("side"), it)
    except Exception as exc:  # noqa: BLE001
        print(f"[outcome_tracker] flush skipped: {exc}", file=sys.stderr)


def update_outcome_tracker(
    payload: dict[str, Any],
    bullish: list[Suggestion],
    bearish: list[Suggestion],
    market_universe: list[dict[str, Any]],
    instant_triggers: list[dict[str, Any]],
) -> None:
    """Silent background logging only - never shown in the UI, no payload
    field. For every Instant Trigger, records price/%-change at +15/+30/+60
    minutes after the trigger plus a continuously-updated "latest seen"
    value, so continuation factors (does volume-holding predict a bigger
    move? does OI accel? does sector breadth?) can be checked empirically
    later instead of guessed. A finished day's items are appended to
    inputs/outcome_log/<session_date>.jsonl when the next session starts."""
    try:
        session_date = payload.get("session_date")
        now_min = _parse_clock_to_minutes(payload.get("market_clock_ist"))
        state = _load_outcome_tracker_state()

        if state.get("session_date") != session_date:
            # New day: whatever was pending from the previous session is done -
            # flush it to that day's permanent log before resetting.
            _flush_outcome_log(state.get("session_date"), state.get("items", []))
            state = {"session_date": session_date, "items": []}

        items: list[dict[str, Any]] = state["items"]
        watched_keys = {(it["symbol"], it["side"]) for it in items}

        for trig in instant_triggers:
            key = (trig["symbol"], trig["side"])
            if key in watched_keys or len(items) >= MAX_PENDING_OUTCOMES:
                continue
            trigger_min = _parse_clock_to_minutes(trig.get("triggered_at_ist"))
            if trigger_min is None:
                continue
            items.append({
                "symbol": trig["symbol"],
                "side": trig["side"],
                "session_date": session_date,
                "setup": trig.get("setup"),
                "trigger_time_ist": trig.get("triggered_at_ist"),
                "trigger_minute": trigger_min,
                "trigger_price": trig.get("price_at_trigger"),
                "trigger_percent_change": trig.get("percent_change_at_trigger"),
                "instant_score": trig.get("instant_score"),
                "instant_factors": trig.get("factors"),
                "rally_score_at_trigger": trig.get("rally_score_at_trigger"),
                "checkpoints": {f"t{m}": None for m in OUTCOME_CHECKPOINTS_MINUTES},
                "latest_price": trig.get("price_at_trigger"),
                "latest_percent_change": trig.get("percent_change_at_trigger"),
                "latest_seen_ist": trig.get("triggered_at_ist"),
            })
            watched_keys.add(key)

        if now_min is not None:
            bullish_by_symbol = {s.symbol: s for s in bullish}
            bearish_by_symbol = {s.symbol: s for s in bearish}
            universe_by_symbol = {r["symbol"]: r for r in market_universe}

            for it in items:
                live = (
                    bullish_by_symbol.get(it["symbol"])
                    or bearish_by_symbol.get(it["symbol"])
                    or universe_by_symbol.get(it["symbol"])
                )
                if live is None:
                    continue
                ltp = live.ltp if hasattr(live, "ltp") else live.get("ltp")
                pct = live.percent_change if hasattr(live, "percent_change") else live.get("percent_change")
                if ltp is None:
                    continue
                it["latest_price"] = ltp
                it["latest_percent_change"] = pct
                it["latest_seen_ist"] = payload.get("market_clock_ist")

                trigger_price = it.get("trigger_price")
                for m in OUTCOME_CHECKPOINTS_MINUTES:
                    ckey = f"t{m}"
                    if it["checkpoints"].get(ckey) is not None:
                        continue
                    if now_min < it["trigger_minute"] + m:
                        continue
                    return_pct = (
                        round(((ltp - trigger_price) / trigger_price) * 100, 2)
                        if trigger_price else None
                    )
                    it["checkpoints"][ckey] = {
                        "price": ltp,
                        "percent_change": pct,
                        "return_from_trigger_pct": return_pct,
                        "recorded_at_ist": payload.get("market_clock_ist"),
                    }

        state["items"] = items
        _save_outcome_tracker_state(state)
    except Exception as exc:  # noqa: BLE001 - must never break a scan
        print(f"[outcome_tracker] update skipped: {exc}", file=sys.stderr)


# Top 5 Eligible: the three gates a stock must clear. MIN_ELIGIBLE_RALLY_SCORE
# and MIN_PULLBACK_SINCE_BO_PCT come from the candle-level dive, then an
# explicit direction change on request: eligibility now REQUIRES more than
# this much pullback since breakout (not less) - the opposite of the
# original "shallow pullback = stronger runner" finding from that dive.
# NOT yet validated across many sessions either way, treat as a first cut.
MIN_ELIGIBLE_RALLY_SCORE = 80
MIN_PULLBACK_SINCE_BO_PCT = 1.0


def _pullback_since_breakout_pct(symbol: str, breakout_time: str | None, side: str) -> float | None:
    """Max drawdown (%) from the post-breakout running high (bullish) or
    running low (bearish), using only candles from breakout_time onward.
    Returns None if there aren't at least 2 such candles yet - too soon
    after the breakout to judge, not automatically disqualified."""
    if not breakout_time:
        return None
    candles = _CANDLE_CACHE.get(symbol, (None, []))[1]
    if not candles:
        return None
    bo_min = _parse_clock_to_minutes(breakout_time)
    if bo_min is None:
        return None
    since = [
        c for c in candles
        if (lambda dt: dt.hour * 60 + dt.minute)(datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ)) >= bo_min
    ]
    if len(since) < 2:
        return None
    if side == "bullish":
        running_extreme = since[0]["high"]
        max_dd = 0.0
        for c in since:
            running_extreme = max(running_extreme, c["high"])
            max_dd = max(max_dd, (running_extreme - c["low"]) / running_extreme * 100)
    else:
        running_extreme = since[0]["low"]
        max_dd = 0.0
        for c in since:
            running_extreme = min(running_extreme, c["low"])
            max_dd = max(max_dd, (c["high"] - running_extreme) / running_extreme * 100)
    return round(max_dd, 2)


def compute_top5_eligible(
    bullish: list[Suggestion],
    bearish: list[Suggestion],
) -> dict[str, list[dict[str, Any]]]:
    """Live, recomputed every scan (no locking/pinning) - a short, harder-
    gated list meant to actually trade, not just watch. Three gates, all
    required: confirmed breakout, Rally Score >= 80, pullback since
    breakout <= 1.5%. A stock too fresh to judge the pullback on is simply
    not listed yet, not excluded - it can appear a few candles later."""
    result: dict[str, list[dict[str, Any]]] = {}
    for side_name, items in (("bullish", bullish), ("bearish", bearish)):
        eligible = []
        for item in items:
            if not item.is_breakout:
                continue
            if item.one_side_rally_score < MIN_ELIGIBLE_RALLY_SCORE:
                continue
            # Same 2:45 PM cutoff as Daily Top 5 - a breakout this late has too
            # few post-breakout candles to judge the pullback reliably anyway.
            bo_min = _parse_clock_to_minutes(item.breakout_time)
            if bo_min is not None and bo_min > DAILY_TOP5_MAX_BREAKOUT_MINUTES:
                continue
            pullback = _pullback_since_breakout_pct(item.symbol, item.breakout_time, side_name)
            if pullback is None or pullback <= MIN_PULLBACK_SINCE_BO_PCT:
                continue
            eligible.append({
                "symbol": item.symbol,
                "lot_size": item.lot_size,
                "setup": item.setup,
                "breakout_time": item.breakout_time,
                "ltp": item.ltp,
                "percent_change": item.percent_change,
                "rally_score": item.one_side_rally_score,
                "status": item.status,
                "vc_ranking": item.vc_ranking,
                "pullback_since_bo_pct": pullback,
            })
        # Rank by move SIZE among the quality-gated survivors, not by score -
        # sorting by score alone let a barely-moved but "clean" stock (e.g.
        # MCX at -0.73%) outrank a genuinely bigger mover (LTM at -2.26%)
        # just because its score happened to hit 100. Score/pullback/breakout
        # already did their job as pass/fail gates above.
        eligible.sort(key=lambda x: -abs(x["percent_change"] or 0))
        result[side_name] = eligible[:5]
    return result


def _load_top5_eligible_state() -> dict[str, Any]:
    try:
        return json.loads(TOP5_ELIGIBLE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"session_date": None, "first_seen": {}}


def _save_top5_eligible_state(state: dict[str, Any]) -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        TOP5_ELIGIBLE_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[top5_eligible] save skipped: {exc}", file=sys.stderr)


def annotate_top5_eligible_since(
    top5_eligible: dict[str, list[dict[str, Any]]],
    session_date: str | None,
    market_clock_ist: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """The eligible list itself is live/unpinned (can shrink, grow, or drop a
    name entirely as pullback changes scan to scan) - this only remembers
    the first clock time each (symbol, side) was EVER seen eligible today,
    so the UI can show "listed since HH:MM" without pinning the list itself."""
    try:
        state = _load_top5_eligible_state()
        if state.get("session_date") != session_date:
            state = {"session_date": session_date, "first_seen": {}}

        first_seen: dict[str, str] = state["first_seen"]
        changed = False
        for side_name, items in top5_eligible.items():
            for it in items:
                key = f"{it['symbol']}_{side_name}"
                if key not in first_seen:
                    first_seen[key] = market_clock_ist
                    changed = True
                it["eligible_since_ist"] = first_seen[key]

        if changed:
            state["first_seen"] = first_seen
            _save_top5_eligible_state(state)
        return top5_eligible
    except Exception as exc:  # noqa: BLE001 - must never break a scan
        print(f"[top5_eligible] annotate skipped: {exc}", file=sys.stderr)
        return top5_eligible


def compute_pmh_pml_filter(
    bullish: list[Suggestion],
    bearish: list[Suggestion],
) -> dict[str, list[dict[str, Any]]]:
    """Live, recomputed every scan (no locking) - a stock qualifies as a real
    gainer/loser only if it's ALSO holding beyond today's opening-range
    high/low (PMH/PML - the same first-6-candle level the chart draws as the
    blue PMH/PML lines) AND beyond yesterday's close, not just showing a
    positive/negative %-change. Shows every qualifying candidate, not capped
    to 5 - this is a filter, not a ranked shortlist."""
    result: dict[str, list[dict[str, Any]]] = {}
    for side_name, items in (("bullish", bullish), ("bearish", bearish)):
        qualifying = []
        for item in items:
            candles = _CANDLE_CACHE.get(item.symbol, (None, []))[1]
            if not candles:
                continue
            base = candles[:6]
            orb_high = max(c["high"] for c in base)
            orb_low = min(c["low"] for c in base)
            ltp = item.ltp
            prev_close = item.prev_close
            if ltp is None:
                continue
            if side_name == "bullish":
                level = orb_high
                passes_level = ltp >= level
                passes_prev = prev_close is not None and ltp > prev_close
            else:
                level = orb_low
                passes_level = ltp <= level
                passes_prev = prev_close is not None and ltp < prev_close
            if not (passes_level and passes_prev):
                continue
            qualifying.append({
                "symbol": item.symbol,
                "lot_size": item.lot_size,
                "setup": item.setup,
                "ltp": ltp,
                "percent_change": item.percent_change,
                "orb_level": round(level, 2),
                "prev_close": round(prev_close, 2) if prev_close is not None else None,
                "rally_score": item.one_side_rally_score,
                "status": item.status,
                "five_min_status": item.five_min_status,
                "breakout_time": item.breakout_time,
                "vc_ranking": item.vc_ranking,
            })
        result[side_name] = qualifying
    return result


def build_payload(top: int) -> dict[str, Any]:
    now_ist = datetime.now(INDIA_TZ)
    errors: list[str] = []

    # The three upstream NSE reads are independent - run them concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_var = ex.submit(fetch_top_gainers_losers)
        f_oi = ex.submit(fetch_change_in_oi)
        f_idx = ex.submit(fetch_index_context)
        gainers, losers, variation_errors = f_var.result()
        oi_by_symbol, oi_errors = f_oi.result()
        index_context, index_errors = f_idx.result()
    errors.extend(variation_errors)
    errors.extend(oi_errors)
    errors.extend(index_errors)

    # Widen intake beyond NSE's top-20 gainers/losers entirely: every F&O
    # stock with lot size < LOT_SCAN_THRESHOLD is checked directly via its own
    # quote+candles, regardless of whether NSE's top-20 OR its (dynamic,
    # can-narrow-during-the-day) OI-spurts feed happens to include it. The
    # stock's own lot size is a fixed, always-known fact - not something NSE
    # decides moment to moment - so this makes NSE's lists an OPTIONAL extra
    # signal (still used for OI/setup classification when available) rather
    # than the sole gatekeeper for whether we ever look at a stock at all.
    shared_candles: dict[str, list[dict[str, Any]]] = {}
    known_symbols = {r["symbol"] for r in gainers} | {r["symbol"] for r in losers}
    extra_symbols = [
        sym for sym, lot in load_fo_lot_sizes().items()
        if sym not in known_symbols
        and sym not in INDEX_LIKE_SYMBOLS
        and lot < LOT_SCAN_THRESHOLD
    ]
    if extra_symbols:
        extra_rows = fetch_extra_candidates(extra_symbols, shared_candles)
        for row in extra_rows:
            (gainers if row["side"] == "gainer" else losers).append(row)

    # Warm the 5-minute candle cache once for the union of both sides'
    # candidates, so bullish and bearish do not each pay a separate fetch wait.
    union_symbols = {
        row["symbol"]
        for row in (*gainers, *losers)
        if _passes_base_filter(row["symbol"], oi_by_symbol)
    }
    if union_symbols:
        bulk_fetch_candles(sorted(union_symbols), shared_candles)

    bullish, gainer_stock_overlap = build_suggestions(
        gainers, oi_by_symbol, side="bullish", top=top,
        index_context=index_context, candles_by_symbol=shared_candles,
    )
    bearish, loser_stock_overlap = build_suggestions(
        losers, oi_by_symbol, side="bearish", top=top,
        index_context=index_context, candles_by_symbol=shared_candles,
    )
    gainers_with_oi = sum(
        1 for row in gainers
        if row["symbol"] in oi_by_symbol
        and row["symbol"] not in INDEX_LIKE_SYMBOLS
        and get_fo_lot_size(row["symbol"]) <= MAX_ALLOWED_LOT_SIZE
    )
    losers_with_oi = sum(
        1 for row in losers
        if row["symbol"] in oi_by_symbol
        and row["symbol"] not in INDEX_LIKE_SYMBOLS
        and get_fo_lot_size(row["symbol"]) <= MAX_ALLOWED_LOT_SIZE
    )
    oi_matched = gainers_with_oi + losers_with_oi

    # Broader snapshot for sector-level aggregation: every OI-confirmed F&O
    # stock with a valid lot size and price move, BEFORE the strict rally-
    # score/blocker filtering that narrows things down to bullish/bearish
    # "breakout candidates". Reuses rows already fetched above (top-20 +
    # widened OI-spurts pool) - no extra network cost. Lets the Sector
    # Analysis view show every sector's real state, not just sectors that
    # happen to have a stock breaking out right now.
    matched_universe_rows = [
        row for row in (*gainers, *losers)
        if _passes_base_filter(row["symbol"], oi_by_symbol)
    ]
    # RVOL: same activity-ratio-vs-median formula build_suggestions uses,
    # just computed across this whole broader pool instead of only the
    # narrower breakout-candidate pool - so the numbers mean the same thing.
    _universe_activities = [
        calc_stock_activity(r, oi_by_symbol.get(r["symbol"])) for r in matched_universe_rows
    ]
    _pos_acts = sorted(a for a in _universe_activities if a > 0)
    if _pos_acts:
        _mid = len(_pos_acts) // 2
        _median_act = _pos_acts[_mid] if len(_pos_acts) % 2 else (_pos_acts[_mid - 1] + _pos_acts[_mid]) / 2.0
    else:
        _median_act = 1.0

    market_universe = []
    for row in matched_universe_rows:
        symbol = row["symbol"]
        activity = calc_stock_activity(row, oi_by_symbol.get(symbol))
        vc_ranking = round(max(0.1, activity / _median_act), 2) if _median_act > 0 else 1.0
        range_pos = day_range_position(row)
        market_universe.append({
            "symbol": symbol,
            "percent_change": row.get("percent_change"),
            "ltp": row.get("ltp"),
            "volume": row.get("volume"),
            "lot_size": get_fo_lot_size(symbol),
            "vc_ranking": vc_ranking,
            "range_position_percent": None if range_pos is None else round(range_pos * 100, 1),
        })

    payload = {
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
        "market_universe": market_universe,
    }
    payload["tracked_breakouts"] = update_tracked_breakouts(payload)
    payload["daily_top5"] = update_daily_top5(payload, bullish, bearish, index_context, market_universe, now_ist)
    payload["instant_triggers"] = update_instant_triggers(payload, bullish, bearish, index_context, market_universe)
    update_outcome_tracker(payload, bullish, bearish, market_universe, payload["instant_triggers"])
    payload["top5_eligible"] = annotate_top5_eligible_since(
        compute_top5_eligible(bullish, bearish),
        payload.get("session_date"), payload.get("market_clock_ist"),
    )
    payload["pmh_pml_filter"] = compute_pmh_pml_filter(bullish, bearish)
    return payload


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
        "tracked_breakouts": [],
        "market_universe": [],
        "daily_top5": {"locked": False, "locked_at_ist": None, "window_start_ist": None,
                        "window_end_ist": None, "window_minutes": DAILY_TOP5_WINDOW_MINUTES,
                        "lock_time_ist": DAILY_TOP5_LOCK_TIME.strftime("%H:%M"),
                        "bullish": [], "bearish": []},
        "instant_triggers": [],
        "top5_eligible": {"bullish": [], "bearish": []},
        "pmh_pml_filter": {"bullish": [], "bearish": []},
    }


def _has_signal(payload: dict[str, Any]) -> bool:
    return bool(payload.get("ok")) and bool(payload.get("bullish") or payload.get("bearish"))


# ── Remote log mirror (Neon Postgres) ───────────────────────────────────────
# Every local log below also mirrors its key events to a "scanner_event_log"
# table in Postgres (via db_adapter.py, DATABASE_URL from .env), so they're
# traceable even when not running/watching this app locally. Local JSON/JSONL
# files stay the source of truth; this is a best-effort mirror only - a DB
# hiccup must never break a scan, same philosophy as every local log here.
def _log_event_to_db(
    event_type: str,
    session_date: str | None,
    symbol: str | None,
    side: str | None,
    payload: dict[str, Any],
) -> None:
    try:
        import db_adapter
        if not db_adapter.DATABASE_URL:
            return
        conn = db_adapter.get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO scanner_event_log (event_type, session_date, symbol, side, payload) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (event_type, session_date, symbol, side, json.dumps(payload, default=str)),
        )
        conn.commit()
        cur.close()
    except Exception as exc:  # noqa: BLE001 - must never break a scan
        print(f"[db_log] {event_type} skipped: {exc}", file=sys.stderr)


# ── Silent scan history logger ──────────────────────────────────────────────
# Every real scan's candidates are appended to a local, gitignored, append-only
# JSONL log - one file per session date. Nothing here touches the UI or the
# served snapshot; it exists purely so a later question like "do rank 6-20
# breakouts actually work out?" can be answered from real data instead of a
# guess. Logging is best-effort: it must never break or slow down a scan.
SCAN_HISTORY_DIR = INPUT_DIR / "scan_history"


def log_scan_snapshot(payload: dict[str, Any]) -> None:
    try:
        session_date = payload.get("session_date") or datetime.now(INDIA_TZ).date().isoformat()
        logged_at = payload.get("generated_at_ist") or datetime.now(INDIA_TZ).isoformat(timespec="seconds")
        lines: list[str] = []
        for side_name in ("bullish", "bearish"):
            for row in payload.get(side_name, []) or []:
                lines.append(json.dumps({
                    "logged_at_ist": logged_at,
                    "session_date": session_date,
                    "side": side_name,
                    "symbol": row.get("symbol"),
                    "rank": row.get("rank"),
                    "score": row.get("one_side_rally_score"),
                    "status": row.get("status"),
                    "five_min_status": row.get("five_min_status"),
                    "is_breakout": row.get("is_breakout"),
                    "breakout_time": row.get("breakout_time"),
                    "percent_change": row.get("percent_change"),
                    "ltp": row.get("ltp"),
                    "entry": row.get("entry"),
                    "stop_loss": row.get("stop_loss"),
                    "target": row.get("target"),
                    "lot_size": row.get("lot_size"),
                    "setup": row.get("setup"),
                    "vc_ranking": row.get("vc_ranking"),
                    "rvol_5m": row.get("rvol_5m"),
                    "range_position_percent": row.get("range_position_percent"),
                }, ensure_ascii=False))
        if not lines:
            return
        SCAN_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        log_path = SCAN_HISTORY_DIR / f"{session_date}.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as exc:  # noqa: BLE001 - logging must never break a scan
        print(f"[scan-history] log skipped: {exc}", file=sys.stderr)


# ── Discovery-delay log ─────────────────────────────────────────────────────
# The dashboard now only DISPLAYS the top MAX_DISPLAY_ROWS per side (UI-only
# cap; the scoring/ranking business logic below is untouched and still runs
# on the full candidate pool). This logs, once per symbol per session, the
# gap between a stock's real breakout_time (candle-derived origin) and the
# moment it first reached that visible top tier - so the discovery delay we
# have been diagnosing by hand all session gets tracked automatically.
DISCOVERY_LOG_DIR = INPUT_DIR / "discovery_log"
DISCOVERY_STATE_PATH = INPUT_DIR / "discovery_state.json"
MAX_DISPLAY_ROWS = 6  # keep in sync with MLAIStockV2.html's MAX_DISPLAY_ROWS


def _parse_clock_to_minutes(text: str | None) -> int | None:
    """'09:20 AM' / '13:30:05' -> minutes since midnight, or None."""
    if not text:
        return None
    text = text.strip()
    ampm = ""
    upper = text.upper()
    if upper.endswith("AM") or upper.endswith("PM"):
        ampm = upper[-2:]
        text = text[:-2].strip()
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if ampm == "PM" and h != 12:
        h += 12
    if ampm == "AM" and h == 12:
        h = 0
    return h * 60 + m


def _load_discovery_state() -> dict[str, Any]:
    try:
        return json.loads(DISCOVERY_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"session_date": None, "logged_keys": []}


def _save_discovery_state(state: dict[str, Any]) -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        DISCOVERY_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[discovery] state save skipped: {exc}", file=sys.stderr)


def log_discovery_delays(payload: dict[str, Any]) -> None:
    """The first time a symbol reaches rank <= MAX_DISPLAY_ROWS this session,
    write one permanent row: its true breakout_time vs the moment it actually
    became visible in the (now capped) dashboard, and the gap between them."""
    try:
        session_date = payload.get("session_date")
        state = _load_discovery_state()
        if state.get("session_date") != session_date:
            state = {"session_date": session_date, "logged_keys": []}
        logged = {tuple(k) for k in state["logged_keys"]}

        now_str = payload.get("market_clock_ist")
        now_min = _parse_clock_to_minutes(now_str)
        new_lines: list[str] = []
        for side_name in ("bullish", "bearish"):
            for row in payload.get(side_name, []) or []:
                rank = row.get("rank")
                if rank is None or rank > MAX_DISPLAY_ROWS:
                    continue
                key = (row.get("symbol"), side_name)
                if key in logged:
                    continue
                logged.add(key)
                bo_min = _parse_clock_to_minutes(row.get("breakout_time"))
                delay_minutes = (now_min - bo_min) if (bo_min is not None and now_min is not None) else None
                discovery_entry = {
                    "session_date": session_date,
                    "symbol": row.get("symbol"),
                    "side": side_name,
                    "breakout_time": row.get("breakout_time"),
                    "first_shown_in_top6_at": now_str,
                    "delay_minutes": delay_minutes,
                    "rank_when_shown": rank,
                    "score_when_shown": row.get("one_side_rally_score"),
                    "status_when_shown": row.get("status"),
                    "percent_change_when_shown": row.get("percent_change"),
                    "vc_ranking_when_shown": row.get("vc_ranking"),
                }
                new_lines.append(json.dumps(discovery_entry, ensure_ascii=False))
                _log_event_to_db("discovery", session_date, row.get("symbol"), side_name, discovery_entry)

        if new_lines:
            DISCOVERY_LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = DISCOVERY_LOG_DIR / f"{session_date}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")

        state["logged_keys"] = [list(k) for k in logged]
        _save_discovery_state(state)
    except Exception as exc:  # noqa: BLE001 - logging must never break a scan
        print(f"[discovery] log skipped: {exc}", file=sys.stderr)


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

    # Log every genuine scan's candidates in the background (not the stale-kept
    # fallback below - that would duplicate an already-logged snapshot under a
    # new timestamp and corrupt the time series).
    if _has_signal(payload):
        log_scan_snapshot(payload)
        log_discovery_delays(payload)

    # Never let a failed/empty scan clobber a good snapshot. If this run produced
    # no usable signal but a previous good snapshot from the SAME session exists,
    # keep serving that one and write the failure to a side file instead.
    if not _has_signal(payload) and output.exists():
        try:
            prev = json.loads(output.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        if _has_signal(prev) and prev.get("session_date") == payload.get("session_date"):
            (output.parent / "stock_suggestion_index.error.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            # Emit the last good snapshot on stdout so callers (server refresh
            # endpoint / WS broadcast) keep showing real data instead of blank.
            prev["_stale_kept"] = True
            prev["_last_scan_error"] = payload.get("message") or payload.get("error")
            print(json.dumps(prev, indent=2))
            return exit_code

    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
