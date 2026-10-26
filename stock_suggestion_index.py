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
import contextlib
import csv
import fcntl
import gzip
import http.cookiejar
import json
import math
import os
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
# Support Render persistent disk via PERSISTENT_DATA_DIR / DATA_DIR environment variables
PERSISTENT_DISK = os.environ.get("PERSISTENT_DATA_DIR") or os.environ.get("DATA_DIR")
INPUT_DIR = Path(PERSISTENT_DISK) if PERSISTENT_DISK else (ROOT / "inputs")
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
# PMH/PML qualification allows a small pullback below the exact opening-range
# extreme (0.5%) so a stock that's still up big on the day but has eased
# slightly off its morning high isn't zeroed out entirely.
PMH_PML_TOLERANCE_PCT = 0.5
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
    # Information Technology
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT", "LTIM": "IT", "LTM": "IT",
    "MPHASIS": "IT", "COFORGE": "IT", "PERSISTENT": "IT", "LTTS": "IT", "KPITTECH": "IT",
    "BIRLASOFT": "IT", "HEXAWARE": "IT", "OFSS": "IT", "TATAELXSI": "IT",
    # Banking
    "HDFCBANK": "Banks", "ICICIBANK": "Banks", "KOTAKBANK": "Banks", "AXISBANK": "Banks",
    "SBIN": "Banks", "INDUSINDBK": "Banks", "BANDHANBNK": "Banks", "IDFCFIRSTB": "Banks",
    "FEDERALBNK": "Banks", "RBLBANK": "Banks", "AUBANK": "Banks", "PNB": "Banks",
    "CANBK": "Banks", "UNIONBANK": "Banks", "BANKBARODA": "Banks", "BANKINDIA": "Banks",
    "INDIANB": "Banks", "MAHABANK": "Banks", "YESBANK": "Banks",
    # Financial Services & Exchanges
    "BAJFINANCE": "Fin Serv", "BAJAJFINSV": "Fin Serv", "MUTHOOTFIN": "Fin Serv",
    "CHOLAFIN": "Fin Serv", "IIFL": "Fin Serv", "ABCAPITAL": "Fin Serv",
    "MANAPPURAM": "Fin Serv", "MOTILALOFS": "Fin Serv", "ANGELONE": "Fin Serv",
    "360ONE": "Fin Serv", "BSE": "Fin Serv", "MCX": "Fin Serv", "CAMS": "Fin Serv", "CDSL": "Fin Serv",
    "KFINTECH": "Fin Serv", "HDFCAMC": "Fin Serv", "NAM-INDIA": "Fin Serv", "HDFCLIFE": "Fin Serv",
    "ICICIGI": "Fin Serv", "ICICIPRULI": "Fin Serv", "SBILIFE": "Fin Serv", "LICI": "Fin Serv",
    "SBICARD": "Fin Serv", "SHRIRAMFIN": "Fin Serv", "PFC": "Fin Serv", "RECLTD": "Fin Serv",
    "IREDA": "Fin Serv", "JIOFIN": "Fin Serv", "LICHSGFIN": "Fin Serv", "PNBHOUSING": "Fin Serv",
    "LTF": "Fin Serv", "MFSL": "Fin Serv", "BAJAJHLDNG": "Fin Serv",
    # Consumer Durables & Electricals
    "BLUESTARCO": "Consumer Durables", "VOLTAS": "Consumer Durables", "HAVELLS": "Consumer Durables",
    "CROMPTON": "Consumer Durables", "DIXON": "Consumer Durables", "AMBER": "Consumer Durables",
    "POLYCAB": "Consumer Durables", "KEI": "Consumer Durables", "PGEL": "Consumer Durables",
    "VMM": "Consumer Durables", "ASTRAL": "Consumer Durables", "SUPREMEIND": "Consumer Durables",
    "APLAPOLLO": "Consumer Durables",
    # Pharmaceuticals & Healthcare
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma", "DIVISLAB": "Pharma",
    "APOLLOHOSP": "Pharma", "LUPIN": "Pharma", "BIOCON": "Pharma", "TORNTPHARM": "Pharma",
    "GLENMARK": "Pharma", "AUROPHARMA": "Pharma", "ABBOTINDIA": "Pharma", "SAGILITY": "Pharma",
    "ALKEM": "Pharma", "FORTIS": "Pharma", "MAXHEALTH": "Pharma", "MANKIND": "Pharma",
    "ZYDUSLIFE": "Pharma", "LAURUSLABS": "Pharma",
    # Automobiles & Ancillaries
    "TATAMOTORS": "Auto", "MARUTI": "Auto", "EICHERMOT": "Auto", "BAJAJ-AUTO": "Auto",
    "HEROMOTOCO": "Auto", "M&M": "Auto", "TVSMOTOR": "Auto", "ASHOKLEY": "Auto",
    "BHARATFORG": "Auto", "BOSCHLTD": "Auto", "FORCEMOT": "Auto", "HYUNDAI": "Auto",
    "MOTHERSON": "Auto", "SONACOMS": "Auto", "TIINDIA": "Auto", "TMPV": "Auto", "UNOMINDA": "Auto",
    # Metals & Mining
    "TATASTEEL": "Metal", "HINDALCO": "Metal", "JSWSTEEL": "Metal", "SAIL": "Metal",
    "VEDL": "Metal", "NMDC": "Metal", "HINDZINC": "Metal", "NATIONALUM": "Metal",
    "JINDALSTEL": "Metal",
    # Energy, Oil & Gas
    "RELIANCE": "Energy", "BPCL": "Energy", "ONGC": "Energy", "IOC": "Energy", "COALINDIA": "Energy",
    "HINDPETRO": "Energy", "GAIL": "Energy", "PETRONET": "Energy", "MGL": "Energy",
    "ATGL": "Energy", "ADANIGREEN": "Energy", "TATAPOWER": "Energy", "OIL": "Energy",
    # FMCG & Consumption
    "HINDUNILVR": "FMCG", "NESTLEIND": "FMCG", "BRITANNIA": "FMCG", "DABUR": "FMCG",
    "MARICO": "FMCG", "GODREJCP": "FMCG", "ITC": "FMCG", "COLPAL": "FMCG",
    "TATACONSUM": "FMCG", "EMAMILTD": "FMCG", "ASIANPAINT": "FMCG", "GODFRYPHLP": "FMCG",
    "JUBLFOOD": "FMCG", "KALYANKJIL": "FMCG", "PAGEIND": "FMCG", "PATANJALI": "FMCG",
    "RADICO": "FMCG", "TITAN": "FMCG", "UNITDSPR": "FMCG", "VBL": "FMCG",
    # Infrastructure, Real Estate & Cement
    "LT": "Infra", "LODHA": "Infra", "DLF": "Infra", "GODREJPROP": "Infra", "PRESTIGE": "Infra",
    "PHOENIXLTD": "Infra", "ULTRACEMCO": "Infra", "AMBUJACEM": "Infra", "SHREECEM": "Infra",
    "OBEROIRLTY": "Infra", "NBCC": "Infra", "RVNL": "Infra", "GMRAIRPORT": "Infra",
    "GRASIM": "Infra", "ADANIENT": "Infra",
    # Capital Goods, Power Equipment & Defence
    "SIEMENS": "Capital Goods", "ABB": "Capital Goods", "BHEL": "Capital Goods",
    "KAYNES": "Capital Goods", "CGPOWER": "Capital Goods", "CUMMINSIND": "Capital Goods",
    "POWERINDIA": "Capital Goods", "SOLARINDS": "Capital Goods",
    "HAL": "Defence", "BEL": "Defence", "BDL": "Defence", "COCHINSHIP": "Defence", "MAZDOCK": "Defence",
    # Power & Green Energy
    "NTPC": "Power", "POWERGRID": "Power", "TORNTPOWER": "Power", "CESC": "Power",
    "ADANIPOWER": "Power", "ADANIENSOL": "Power", "INOXWIND": "Power", "JSWENERGY": "Power",
    "NHPC": "Power", "PREMIERENE": "Power", "SUZLON": "Power", "WAAREEENER": "Power",
    # Chemicals & Agrochemicals
    "ATUL": "Chemicals", "PIDILITIND": "Chemicals", "NAVINFLUOR": "Chemicals",
    "PIIND": "Chemicals", "SRF": "Chemicals", "UPL": "Chemicals",
    # Telecom & Media
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "INDUSTOWER": "Telecom",
    "ZEEL": "Media", "PVRINOX": "Media",
    # Logistics, Aviation & Hotels
    "CONCOR": "Logistics", "IRFC": "Logistics", "IRCTC": "Logistics", "ADANIPORTS": "Logistics",
    "DELHIVERY": "Logistics", "INDHOTEL": "Hotels", "INDIGO": "Aviation",
    # Retail & Internet
    "ZOMATO": "Internet", "POLICYBZR": "Internet", "NYKAA": "Internet", "PAYTM": "Internet",
    "NAUKRI": "Internet", "SWIGGY": "Internet", "ETERNAL": "Internet", "IEX": "Internet",
    "DMART": "Retail", "TRENT": "Retail",
    "ATHERENERG": "EV/Energy", "OLECTRA": "EV/Energy",
}


def get_sector(symbol: str) -> str:
    return SECTOR_MAP.get(symbol, "Other")
# Any stock with a lot size above this is excluded entirely - too much
# capital per contract to be a practical suggestion.
MAX_ALLOWED_LOT_SIZE = 650
# Every F&O stock with a lot size below this is scanned directly every cycle,
# independent of NSE's top-20 gainers/losers or its OI-spurts feed.
LOT_SCAN_THRESHOLD = 650
FO_LOT_SIZES_PATH = INPUT_DIR / "fo_lot_sizes.csv"
_FO_LOT_SIZES_CACHE: dict[str, int] | None = None

# 5-minute candle fetch tuning.
# Candles only change every 5 minutes, so a short TTL cache lets back-to-back
# refreshes (manual "Refresh Live" spam, or bullish+bearish in the same scan)
# reuse the same download instead of re-hitting the slow upstream chart API.
def get_candle_cache_ttl() -> float:
    """Dynamic cache TTL: strict 90 seconds during live NSE market hours to prevent stale data;
    24 hours outside market hours to preserve closed session snapshots between restarts.
    """
    now_ist = datetime.now(INDIA_TZ)
    current_minutes = now_ist.hour * 60 + now_ist.minute
    is_live_market = now_ist.weekday() < 5 and (9 * 60 + 15) <= current_minutes <= (15 * 60 + 35)
    return 90.0 if is_live_market else 86400.0


CANDLE_CACHE_TTL_SECONDS = 90.0     # Default market-hours fallback
CANDLE_FETCH_TIMEOUT = 3.5          # per-request socket timeout
CANDLE_POOL_WAIT_SECONDS = 9.0     # hard cap on the whole batch; stragglers are abandoned
CANDLE_POOL_MAX_WORKERS = 24
CANDLE_CACHE_PATH = INPUT_DIR / "candle_cache.json"
_CANDLE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CANDLE_1M_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CANDLE_CACHE_LOADED = False


def _load_candle_cache() -> None:
    global _CANDLE_CACHE_LOADED
    if _CANDLE_CACHE_LOADED:
        return
    _CANDLE_CACHE_LOADED = True
    try:
        raw = json.loads(CANDLE_CACHE_PATH.read_text(encoding="utf-8"))
        cutoff = _time.time() - get_candle_cache_ttl()
        for sym, entry in raw.items():
            if isinstance(entry, list) and len(entry) == 2 and entry[0] >= cutoff:
                _CANDLE_CACHE[sym] = (float(entry[0]), entry[1])
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - a bad cache file must never break a scan
        print(f"candle cache load skipped: {exc}", file=sys.stderr)


def _save_candle_cache() -> None:
    try:
        cutoff = _time.time() - get_candle_cache_ttl()
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
    money_flow_percentage: float = 0.0
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
    is_cascade: bool = False
    cascade_type: str | None = None
    cascade_time: str | None = None
    cascade_body_pct: float = 0.0
    cascade_vol_ratio: float = 1.0
    breakout_freshness: str = "PENDING"
    breakout_age_minutes: int | None = None
    is_block_surge: bool = False
    block_surge_type: str | None = None
    block_surge_time: str | None = None
    block_surge_turnover_cr: float = 0.0
    block_surge_rvol: float = 1.0
    first_qualified_at: str | None = None
    first_top5_at: str | None = None
    first_rank1_at: str | None = None
    first_alerted_at: str | None = None
    should_alert: bool = False
    alert_count: int = 0
    alert_event_id: str | None = None
    alert_created_at: str | None = None
    alert_delivered_at: str | None = None
    alert_acknowledged_at: str | None = None
    completed_candle_close: float | None = None
    is_stale_data: bool = False
    is_stale_fallback: bool = False
    delivery_status: str = "PENDING"
    claimed_by: str | None = None
    claimed_at: float | None = None
    is_failed_trend: bool = False

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
            "money_flow_percentage": self.money_flow_percentage,
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
            "is_cascade": self.is_cascade,
            "cascade_type": self.cascade_type,
            "cascade_time": self.cascade_time,
            "cascade_body_pct": self.cascade_body_pct,
            "cascade_vol_ratio": self.cascade_vol_ratio,
            "breakout_freshness": self.breakout_freshness,
            "breakout_age_minutes": self.breakout_age_minutes,
            "is_block_surge": self.is_block_surge,
            "block_surge_type": self.block_surge_type,
            "block_surge_time": self.block_surge_time,
            "block_surge_turnover_cr": self.block_surge_turnover_cr,
            "block_surge_rvol": self.block_surge_rvol,
            "first_qualified_at": self.first_qualified_at,
            "first_top5_at": self.first_top5_at,
            "first_rank1_at": self.first_rank1_at,
            "first_alerted_at": self.first_alerted_at,
            "should_alert": self.should_alert,
            "alert_count": self.alert_count,
            "alert_event_id": self.alert_event_id,
            "alert_created_at": self.alert_created_at,
            "alert_delivered_at": self.alert_delivered_at,
            "alert_acknowledged_at": self.alert_acknowledged_at,
            "completed_candle_close": self.completed_candle_close,
            "is_stale_data": self.is_stale_data,
            "is_stale_fallback": self.is_stale_fallback,
            "delivery_status": self.delivery_status,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "is_failed_trend": self.is_failed_trend,
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
    import scanner_trace
    last_err: Exception | None = None
    for attempt in range(3):
        trace_start = _time.monotonic()
        _nse_warm(referer, force=(attempt > 0))
        try:
            raw = _nse_fetch(url, referer=referer, timeout=25.0)
            decoded = json.loads(raw.decode("utf-8"))
            scanner_trace.safe_source("NSE", endpoint=scanner_trace.endpoint_name(url), started=trace_start,
                attempt=attempt+1, upstream_timestamp_raw=scanner_trace.supplied_timestamps(decoded),
                upstream_timestamp_verified=False)
            return decoded
        except json.JSONDecodeError as exc:
            scanner_trace.safe_source("NSE", endpoint=scanner_trace.endpoint_name(url), started=trace_start,
                                     status="invalid_json", attempt=attempt+1)
            raise LiveDataError(f"NSE returned non-JSON for {url}: {raw[:120]!r}") from exc
        except Exception as exc:  # noqa: BLE001 - retry with a fresh handshake
            scanner_trace.safe_source("NSE", endpoint=scanner_trace.endpoint_name(url), started=trace_start,
                                     status="fetch_failed", attempt=attempt+1, error_type=type(exc).__name__)
            last_err = exc
            _time.sleep(0.4 * (attempt + 1))
    raise LiveDataError(str(last_err) or f"NSE request failed: {url}")


def nse_get_csv(url: str, *, referer: str) -> list[dict[str, Any]]:
    import scanner_trace
    trace_start = _time.monotonic()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8-sig")
        rows = [dict(row) for row in csv.DictReader(text.splitlines())]
        scanner_trace.safe_source("NSE_csv", endpoint=scanner_trace.endpoint_name(url), started=trace_start,
                                 row_count=len(rows), upstream_timestamp_verified=False)
        return rows
    except Exception as exc:
        scanner_trace.safe_source("NSE_csv", endpoint=scanner_trace.endpoint_name(url), started=trace_start,
                                 status="fetch_failed", error_type=type(exc).__name__)
        raise


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
        return sorted(filtered, key=lambda row: row["percent_change"] or 0, reverse=True)
    filtered = [row for row in filtered if row["percent_change"] <= 0]
    return sorted(filtered, key=lambda row: row["percent_change"] or 0)


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
    import scanner_trace
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
            trace_start = _time.monotonic()
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
                        scanner_trace.safe_source("Yahoo_index_fallback", symbol=index_name, started=trace_start,
                            fallback_used=True, quote_market_epoch=meta.get("regularMarketTime"),
                            upstream_timestamp_verified=False)
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
                scanner_trace.safe_source("Yahoo_index_fallback", symbol=index_name, started=trace_start,
                                         fallback_used=True, status="fetch_failed", error_type=type(exc).__name__)
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


def fetch_5m_candles_pipeline(symbol: str, full_range: bool = False) -> dict[str, Any]:
    """Hierarchical, verified 5-minute candle engine:
    1. Upstox API v2 (Primary Fast Path) -> validate completed/fresh candles
    2. Yahoo Finance (Secondary Fallback) -> validate completed/fresh candles
    3. Short-Lived Local Cache (TTL 90s market hours) -> explicitly tagged as STALE
    4. Unavailable / Error status
    """
    now_ist = datetime.now(INDIA_TZ)
    now_epoch = _time.time()

    # 1. Primary: Official Upstox Provider
    try:
        import upstox_provider
        upstox_res = upstox_provider.fetch_5m_candles_with_telemetry(symbol, full_range=full_range)
        if upstox_res.get("ok") and upstox_res.get("completed_candles"):
            if upstox_res.get("candles_1m"):
                _CANDLE_1M_CACHE[symbol] = (_time.time(), upstox_res["candles_1m"])
            return upstox_res
    except Exception as exc:
        pass

    # 2. Secondary Fallback: Yahoo Finance
    import scanner_trace
    trace_start = _time.monotonic()
    yahoo_sym = f"{symbol}.NS"
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
            if result:
                quote = result[0]
                scanner_trace.safe_source("Yahoo_candles", symbol=symbol, started=trace_start,
                    quote_market_epoch=(quote.get("meta") or {}).get("regularMarketTime"),
                    latest_bar_start_epoch=max(quote.get("timestamp") or [0]) or None,
                    upstream_timestamp_verified=False)
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
                if all_candles:
                    # Collapse multi-ticks in the same 5-minute bucket
                    merged_candles: list[dict[str, Any]] = []
                    for c in all_candles:
                        bucket_ts = (c["timestamp"] // 300) * 300
                        if merged_candles and (merged_candles[-1]["timestamp"] // 300) * 300 == bucket_ts:
                            prev = merged_candles[-1]
                            prev["high"] = max(prev["high"], c["high"])
                            prev["low"] = min(prev["low"], c["low"])
                            prev["close"] = c["close"]
                            prev["volume"] += c["volume"]
                        else:
                            merged_candles.append(dict(c))
                    all_candles = merged_candles

                    # Isolate current session if not full_range
                    if not full_range:
                        from datetime import datetime as dt_cls
                        latest_dt = dt_cls.fromtimestamp(all_candles[-1]["timestamp"], tz=INDIA_TZ).date()
                        all_candles = [
                            c for c in all_candles
                            if dt_cls.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() == latest_dt
                        ] or all_candles[-75:]

                    import upstox_provider
                    is_valid, _ = upstox_provider.validate_candles_integrity(all_candles, now_ist, allow_multi_day=full_range)
                    if is_valid:
                        completed, forming = upstox_provider.separate_completed_and_forming(all_candles, now_ist)
                        if completed:
                            lat_ms = int((_time.monotonic() - trace_start) * 1000)
                            latest_epoch = completed[-1]["timestamp"]
                            latest_ist = datetime.fromtimestamp(latest_epoch, tz=INDIA_TZ).strftime("%I:%M %p")
                            return {
                                "ok": True,
                                "symbol": symbol,
                                "provider": "Yahoo (Fallback)",
                                "completed_candles": completed,
                                "forming_candle": forming,
                                "all_candles": all_candles,
                                "latest_completed_candle_ist": latest_ist,
                                "latest_completed_candle_epoch": latest_epoch,
                                "observed_latency_ms": lat_ms,
                                "fallback_used": True,
                                "is_stale": False,
                                "error": None,
                            }
    except Exception as e:
        scanner_trace.safe_source("Yahoo_candles", symbol=symbol, started=trace_start,
                                 status="fetch_failed", error_type=type(e).__name__)

    # 3. Tertiary: Short-Lived Local Disk Cache (explicitly marked STALE)
    _load_candle_cache()
    cached = _CANDLE_CACHE.get(symbol)
    if cached and (now_epoch - cached[0]) <= get_candle_cache_ttl():
        c_candles = cached[1]
        if c_candles:
            latest_epoch = c_candles[-1]["timestamp"]
            latest_ist = datetime.fromtimestamp(latest_epoch, tz=INDIA_TZ).strftime("%I:%M %p")
            return {
                "ok": True,
                "symbol": symbol,
                "provider": "Cache (Stale)",
                "completed_candles": c_candles,
                "forming_candle": None,
                "all_candles": c_candles,
                "latest_completed_candle_ist": latest_ist,
                "latest_completed_candle_epoch": latest_epoch,
                "observed_latency_ms": 0,
                "fallback_used": True,
                "is_stale": True,
                "error": "Upstream feeds unavailable, serving short-lived local cache",
            }

    # 4. Quaternary: Unavailable / Stale Status
    return {
        "ok": False,
        "symbol": symbol,
        "provider": "Unavailable",
        "completed_candles": [],
        "forming_candle": None,
        "all_candles": [],
        "latest_completed_candle_ist": None,
        "latest_completed_candle_epoch": None,
        "observed_latency_ms": 0,
        "fallback_used": True,
        "is_stale": True,
        "error": "All data providers failed/unauthorized and local cache is expired or empty",
    }


def fetch_5m_candles_for_symbol(symbol: str, full_range: bool = False) -> list[dict[str, Any]]:
    """Fetches real-time 5-minute OHLCV candles for an NSE stock.
    Executes the validated hierarchical pipeline and returns completed candles only.
    """
    pipe_res = fetch_5m_candles_pipeline(symbol, full_range=full_range)
    if pipe_res.get("ok"):
        return pipe_res.get("completed_candles", [])
    return []


def fetch_quote_and_candles_for_symbol(symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """For a symbol that ISN'T in NSE's top-20 gainers/losers: build the same
    row shape (open/high/low/ltp/percent_change/volume) directly from Yahoo's
    intraday chart in one request, using its `previousClose` in the response
    metadata - no dependency on the top-20 feed at all. Used to widen the
    candidate pool beyond NSE's arbitrary top-20 cutoff."""
    import scanner_trace
    trace_start = _time.monotonic()
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
            scanner_trace.safe_source("Yahoo_extra_quote", symbol=symbol, started=trace_start, status="empty_response")
            return None
        quote = result[0]
        meta = quote.get("meta") or {}
        scanner_trace.safe_source("Yahoo_extra_quote", symbol=symbol, started=trace_start,
            quote_market_epoch=meta.get("regularMarketTime"),
            latest_bar_start_epoch=max(quote.get("timestamp") or [0]) or None, upstream_timestamp_verified=False)
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
        scanner_trace.safe_source("Yahoo_extra_quote", symbol=symbol, started=trace_start,
                                 status="fetch_failed", error_type=type(e).__name__)
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
    import scanner_trace
    _load_candle_cache()
    now = _time.time()
    pending: list[str] = []
    for sym in symbols:
        if sym in candles_by_symbol:
            scanner_trace.safe_source("candle_input", symbol=sym, status="shared_scan_input",
                                     cache_age_seconds=None, row_count=len(candles_by_symbol[sym]))
            continue
        cached = _CANDLE_CACHE.get(sym)
        if cached and (now - cached[0]) <= CANDLE_CACHE_TTL_SECONDS:
            candles_by_symbol[sym] = cached[1]
            scanner_trace.safe_source("candle_input", symbol=sym, status="cache_hit",
                                     cache_age_seconds=round(now-cached[0], 3), row_count=len(cached[1]))
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
        if sym not in candles_by_symbol:
            scanner_trace.safe_source("candle_input", symbol=sym, status="batch_timeout_or_missing")
        candles_by_symbol.setdefault(sym, [])
    _save_candle_cache()
    return candles_by_symbol


def detect_3bar_impulse_cascade(
    candles: list[dict[str, Any]],
    side: str,
    *,
    key_level: float | None = None,
    avg_5m_vol: float | None = None,
) -> dict[str, Any]:
    """Detects 3 consecutive 5-minute candles making continuous lower lows (bearish)
    or higher highs (bullish) with >= 60% directional body ratio beyond PML/PMH.
    Serves as the high-conviction micro execution trigger for Two-Tier Approach A."""
    if len(candles) < 3:
        return {
            "is_cascade": False,
            "cascade_type": None,
            "cascade_time": None,
            "cascade_body_pct": 0.0,
            "cascade_vol_ratio": 1.0,
        }

    def eval_trio(trio: list[dict[str, Any]]) -> tuple[bool, float, float]:
        vol_sum = sum(c.get("volume", 0) for c in trio)
        v_ratio = (vol_sum / (3.0 * avg_5m_vol)) if (avg_5m_vol and avg_5m_vol > 0) else 1.0
        if v_ratio < 0.65:
            return False, 0.0, 0.0

        if side == "bearish":
            # Pattern A: 3 Consecutive Red Waterfall
            is_3red = all(c["close"] < c["open"] for c in trio) and (trio[1]["low"] < trio[0]["low"] and trio[2]["low"] < trio[1]["low"])
            # Pattern B: Base-and-Breakdown (Pole -> Pause Flag -> Continuation Breakdown)
            c0_red = trio[0]["close"] < trio[0]["open"]
            c1_pause = trio[1]["high"] <= trio[0]["high"] * 1.002
            c2_break = trio[2]["close"] < trio[2]["open"] and trio[2]["low"] < min(trio[0]["low"], trio[1]["low"])
            is_base_break = c0_red and c1_pause and c2_break

            if not (is_3red or is_base_break):
                return False, 0.0, 0.0

            # Body Dominance Check
            body_ratios = []
            for c in trio:
                rng = c["high"] - c["low"]
                body = c["open"] - c["close"]
                body_ratios.append(body / rng if rng > 0 else 0.0)
            avg_body = sum(body_ratios) / 3.0
            if is_3red and (avg_body < 0.55 or any(b < 0.25 for b in body_ratios)):
                return False, 0.0, 0.0
            if is_base_break and (body_ratios[0] < 0.40 or body_ratios[2] < 0.40):
                return False, 0.0, 0.0

            # Level check: price below key level / PML
            if key_level is not None and trio[2]["close"] > key_level * 1.002:
                return False, 0.0, 0.0

            return True, avg_body, v_ratio
        else:
            # Bullish
            # Pattern A: 3 Consecutive Green Impulse
            is_3green = all(c["close"] > c["open"] for c in trio) and (trio[1]["high"] > trio[0]["high"] and trio[2]["high"] > trio[1]["high"])
            # Pattern B: Base-and-Breakout (Pole -> Pause Flag -> Continuation Breakout)
            c0_green = trio[0]["close"] > trio[0]["open"]
            c1_pause = trio[1]["low"] >= trio[0]["low"] * 0.998
            c2_break = trio[2]["close"] > trio[2]["open"] and trio[2]["high"] > max(trio[0]["high"], trio[1]["high"])
            is_base_break = c0_green and c1_pause and c2_break

            if not (is_3green or is_base_break):
                return False, 0.0, 0.0

            body_ratios = []
            for c in trio:
                rng = c["high"] - c["low"]
                body = c["close"] - c["open"]
                body_ratios.append(body / rng if rng > 0 else 0.0)
            avg_body = sum(body_ratios) / 3.0
            if is_3green and (avg_body < 0.55 or any(b < 0.25 for b in body_ratios)):
                return False, 0.0, 0.0
            if is_base_break and (body_ratios[0] < 0.40 or body_ratios[2] < 0.40):
                return False, 0.0, 0.0

            if key_level is not None and trio[2]["close"] < key_level * 0.998:
                return False, 0.0, 0.0

            return True, avg_body, v_ratio

    # 1. Check latest 3 completed candles
    is_valid, avg_b, v_r = eval_trio(candles[-3:])
    confirmed_candle = candles[-1]

    # 2. If latest bar is consolidating or paused right after an active cascade,
    # evaluate recent candles (last 4 to 5) so an active waterfall isn't lost on 1 neutral bar
    if not is_valid and len(candles) >= 4:
        is_valid, avg_b, v_r = eval_trio(candles[-4:-1])
        confirmed_candle = candles[-2]
        if is_valid:
            if side == "bearish" and candles[-1]["close"] > confirmed_candle["high"]:
                is_valid = False
            elif side == "bullish" and candles[-1]["close"] < confirmed_candle["low"]:
                is_valid = False

    # 3. Wave-level cascade tracking (scanning backwards from most recent):
    # Ensures we find an active cascade that has NOT dumped or failed.
    if not is_valid and len(candles) >= 3:
        for idx in range(len(candles) - 1, 1, -1):
            trio = candles[idx-2:idx+1]
            trio_valid, t_avg_b, t_v_r = eval_trio(trio)
            if trio_valid:
                # Invalidation check: did any subsequent candle dump or reverse?
                subsequent = candles[idx+1:]
                dumped = False
                if side == "bullish":
                    impulse_low = trio[2]["low"]
                    if any(c["close"] < impulse_low * 0.998 for c in subsequent):
                        dumped = True
                else:
                    waterfall_high = trio[2]["high"]
                    if any(c["close"] > waterfall_high * 1.002 for c in subsequent):
                        dumped = True

                if not dumped:
                    is_valid = True
                    avg_b = t_avg_b
                    v_r = t_v_r
                    confirmed_candle = trio[2]
                    break

    if is_valid:
        cascade_type = "Waterfall Breakdown" if side == "bearish" else "Impulse Breakout"
        cascade_time = datetime.fromtimestamp(confirmed_candle["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
        return {
            "is_cascade": True,
            "cascade_type": cascade_type,
            "cascade_time": cascade_time,
            "cascade_body_pct": round(avg_b * 100, 1),
            "cascade_vol_ratio": round(v_r, 2),
        }

    return {
        "is_cascade": False,
        "cascade_type": None,
        "cascade_time": None,
        "cascade_body_pct": 0.0,
        "cascade_vol_ratio": 1.0,
    }


def detect_institutional_block_surge(
    symbol: str,
    candles_1m: list[dict[str, Any]],
    candles_5m: list[dict[str, Any]],
    side: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Evaluates sub-minute and multi-crore Institutional Block Surges (FII/DII footprints).
    
    Checks both:
    1. 1-Minute Candle Radar (Fast Sub-Minute Path):
       - Single 1m turnover >= ₹7.5 Crores AND RVOL_1m >= 4.0x
       - Single 1m RVOL >= 8.0x AND turnover >= ₹3.0 Crores with directional price move (>=0.25%)
       - Extreme 1m turnover >= ₹15.0 Crores (unmistakable institutional block trade)
    2. 5-Minute Candle Radar (Fallback/Confirmation Path):
       - Single 5m turnover >= ₹20.0 Crores AND RVOL_5m >= 3.5x
       - Single 5m RVOL >= 7.0x AND turnover >= ₹10.0 Crores
    
    Looks across the most recent 15 minutes of trading to maintain the alert if an
    institutional block trade occurred recently.
    """
    default_res = {
        "is_block_surge": False,
        "block_surge_type": None,
        "block_surge_time": None,
        "block_surge_turnover_cr": 0.0,
        "block_surge_rvol": 1.0,
        "block_surge_vol": 0,
        "block_surge_price": float(row.get("ltp") or 0.0),
        "surge_age_minutes": 0,
    }

    # 1. Evaluate 1-Minute Candles first (Fast Sub-Minute Radar)
    if candles_1m and len(candles_1m) >= 2:
        valid_vols = [float(c.get("volume") or 0) for c in candles_1m if float(c.get("volume") or 0) > 0]
        if valid_vols:
            sorted_vols = sorted(valid_vols)
            median_1m_vol = sorted_vols[len(sorted_vols) // 2]
            baseline_vol = max(50.0, median_1m_vol)
        else:
            baseline_vol = 50.0

        recent_1m = candles_1m[-15:]
        for idx, c in enumerate(reversed(recent_1m)):
            vol = float(c.get("volume") or 0)
            if vol <= 0:
                continue
            close = float(c.get("close") or row.get("ltp") or 0)
            open_p = float(c.get("open") or close)
            turnover_cr = (vol * close) / 10_000_000.0
            rvol_1m = vol / baseline_vol
            move_pct = ((close - open_p) / open_p * 100) if open_p > 0 else 0.0

            is_bullish_bar = close >= open_p * 0.999
            is_bearish_bar = close <= open_p * 1.001

            is_match = False
            if side == "bullish" and is_bullish_bar:
                if turnover_cr >= 15.0:
                    is_match = True
                elif turnover_cr >= 7.5 and rvol_1m >= 4.0:
                    is_match = True
                elif rvol_1m >= 8.0 and turnover_cr >= 3.0 and move_pct >= 0.25:
                    is_match = True
            elif side == "bearish" and is_bearish_bar:
                if turnover_cr >= 15.0:
                    is_match = True
                elif turnover_cr >= 7.5 and rvol_1m >= 4.0:
                    is_match = True
                elif rvol_1m >= 8.0 and turnover_cr >= 3.0 and move_pct <= -0.25:
                    is_match = True

            if is_match:
                ts = c.get("timestamp")
                surge_time = datetime.fromtimestamp(ts, tz=INDIA_TZ).strftime("%I:%M %p") if ts else None
                stype = "⚡ BLOCK SURGE (BUY)" if side == "bullish" else "⚡ BLOCK SURGE (SELL)"
                return {
                    "is_block_surge": True,
                    "block_surge_type": stype,
                    "block_surge_time": surge_time,
                    "block_surge_turnover_cr": round(turnover_cr, 1),
                    "block_surge_rvol": round(rvol_1m, 1),
                    "block_surge_vol": int(vol),
                    "block_surge_price": close,
                    "surge_age_minutes": idx,
                }

    # 2. Fallback / Confirmation via 5-Minute Candles
    if candles_5m and len(candles_5m) >= 2:
        valid_5m_vols = [float(c.get("volume") or 0) for c in candles_5m if float(c.get("volume") or 0) > 0]
        sorted_5m = sorted(valid_5m_vols) if valid_5m_vols else [1.0]
        median_5m_vol = sorted_5m[len(sorted_5m) // 2]
        baseline_5m = max(100.0, median_5m_vol)

        recent_5m = candles_5m[-4:]
        for idx, c in enumerate(reversed(recent_5m)):
            vol = float(c.get("volume") or 0)
            if vol <= 0:
                continue
            close = float(c.get("close") or row.get("ltp") or 0)
            open_p = float(c.get("open") or close)
            turnover_cr = (vol * close) / 10_000_000.0
            rvol_5m = vol / baseline_5m
            is_bullish_bar = close >= open_p * 0.998
            is_bearish_bar = close <= open_p * 1.002

            is_match = False
            if side == "bullish" and is_bullish_bar:
                if turnover_cr >= 35.0:
                    is_match = True
                elif turnover_cr >= 20.0 and rvol_5m >= 3.5:
                    is_match = True
                elif rvol_5m >= 7.0 and turnover_cr >= 10.0:
                    is_match = True
            elif side == "bearish" and is_bearish_bar:
                if turnover_cr >= 35.0:
                    is_match = True
                elif turnover_cr >= 20.0 and rvol_5m >= 3.5:
                    is_match = True
                elif rvol_5m >= 7.0 and turnover_cr >= 10.0:
                    is_match = True

            if is_match:
                ts = c.get("timestamp")
                surge_time = datetime.fromtimestamp(ts, tz=INDIA_TZ).strftime("%I:%M %p") if ts else None
                stype = "⚡ BLOCK SURGE (BUY)" if side == "bullish" else "⚡ BLOCK SURGE (SELL)"
                return {
                    "is_block_surge": True,
                    "block_surge_type": stype,
                    "block_surge_time": surge_time,
                    "block_surge_turnover_cr": round(turnover_cr, 1),
                    "block_surge_rvol": round(rvol_5m, 1),
                    "block_surge_vol": int(vol),
                    "block_surge_price": close,
                    "surge_age_minutes": idx * 5,
                }

    return default_res


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
            "is_cascade": False,
            "cascade_type": None,
            "cascade_time": None,
            "cascade_body_pct": 0.0,
            "cascade_vol_ratio": 1.0,
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

    # 4. Check 3-Bar Impulse Cascade (Two-Tier Trigger)
    key_level = morning_low if side == "bearish" else morning_high
    if len(candles) >= 6:
        key_level = min(c["low"] for c in candles[:6]) if side == "bearish" else max(c["high"] for c in candles[:6])

    cascade_info = detect_3bar_impulse_cascade(
        candles,
        side,
        key_level=key_level,
        avg_5m_vol=avg_5m_vol,
    )

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

        # Track earliest decisive breakout from the morning range (origin timestamp)
        first_breakout_time = None
        first_breakout_epoch = None
        for c in trading_candles:
            if c["close"] >= morning_high * 1.001 or (c["close"] > morning_high and c["high"] >= morning_high * 1.002 and c["close"] >= c["open"]):
                first_breakout_time = datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = c["timestamp"]
                break

        if first_breakout_time:
            breakout_time = first_breakout_time

        # Major Structural Rail Breakout (PMH) - check if price broke 30m ORB rail later
        if len(candles) >= 6:
            orb_high = max(c["high"] for c in candles[:6])
            if latest_close >= orb_high * 0.995:
                for c in candles[6:]:
                    if c["close"] >= orb_high * 0.999 or c["high"] >= orb_high * 1.001:
                        if not breakout_time:
                            breakout_time = datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                            first_breakout_epoch = c["timestamp"]
                        break

        # Early Detection: Live Forming Wick Breakout & Velocity Ignition (dP/dt)
        live_ltp = float(row.get("ltp") or latest_close)
        live_high = float(row.get("high") or latest["high"])
        live_open = float(row.get("open") or candles[0]["open"])
        open_move = ((live_ltp - live_open) / live_open * 100) if live_open > 0 else 0.0
        elapsed_min = max(5, len(candles) * 5)
        velocity = (open_move / elapsed_min) * max(1.0, latest_rvol)

        if cascade_info["is_cascade"]:
            is_breakout = True
            breakout_status = "Impulse BO"
            chart_structure = "Impulse Breakout"
            if not breakout_time:
                breakout_time = cascade_info["cascade_time"]
            if not first_breakout_epoch:
                first_breakout_epoch = latest["timestamp"]
        elif (live_ltp >= morning_high * 1.001 or live_high >= morning_high * 1.002) and live_ltp >= ema9 and live_ltp >= live_open:
            is_breakout = True
            breakout_status = "Impulse BO"
            chart_structure = "Live Wick Breakout"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = latest["timestamp"]
        elif elapsed_min <= 45 and open_move >= 0.75 and velocity >= 0.05 and live_ltp >= ema9:
            is_breakout = True
            breakout_status = "Impulse BO"
            chart_structure = "Velocity Ignition"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = latest["timestamp"]
        elif latest_close >= morning_high * 0.998 and latest_close >= ema9:
            is_breakout = True
            breakout_status = "Breakout"
            if chart_structure == "Base Building":
                chart_structure = "Breakout Rally"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = latest["timestamp"]
        elif latest_close >= morning_high * 0.992:
            breakout_status = "Near BO"
            if chart_structure == "Base Building":
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

        # Track earliest decisive breakdown from the morning range (origin timestamp)
        # Requires close <= morning_low * 0.999 to avoid false 10-paise wick resets (e.g. 923.10 vs 923.20)
        first_breakout_time = None
        first_breakout_epoch = None
        for c in trading_candles:
            if c["close"] <= morning_low * 0.999 or (c["close"] < morning_low and c["low"] <= morning_low * 0.995 and c["close"] <= c["open"]):
                first_breakout_time = datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = c["timestamp"]
                break

        if first_breakout_time:
            breakout_time = first_breakout_time

        # Major Structural Rail Breakdown (PML) - check if price broke 30m ORB rail later
        if len(candles) >= 6:
            orb_low = min(c["low"] for c in candles[:6])
            if latest_close <= orb_low * 1.005:
                for c in candles[6:]:
                    if c["low"] <= orb_low * 1.0005 or c["close"] <= orb_low * 1.001:
                        if not breakout_time:
                            breakout_time = datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                            first_breakout_epoch = c["timestamp"]
                        break

        # Early Detection: Live Forming Wick Breakdown & Velocity Ignition Slide (dP/dt)
        live_ltp = float(row.get("ltp") or latest_close)
        live_low = float(row.get("low") or latest["low"])
        live_open = float(row.get("open") or candles[0]["open"])
        open_move = ((live_ltp - live_open) / live_open * 100) if live_open > 0 else 0.0
        elapsed_min = max(5, len(candles) * 5)
        velocity = (open_move / elapsed_min) * max(1.0, latest_rvol)

        if cascade_info["is_cascade"]:
            is_breakout = True
            breakout_status = "Waterfall BD"
            chart_structure = "Active Waterfall"
            if not breakout_time:
                breakout_time = cascade_info["cascade_time"]
            if not first_breakout_epoch:
                first_breakout_epoch = latest["timestamp"]
        elif (live_ltp <= morning_low * 0.999 or live_low <= morning_low * 0.998) and live_ltp <= ema9 and live_ltp <= live_open:
            is_breakout = True
            breakout_status = "Waterfall BD"
            chart_structure = "Live Wick Breakdown"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = latest["timestamp"]
        elif elapsed_min <= 45 and open_move <= -0.75 and velocity <= -0.05 and live_ltp <= ema9:
            is_breakout = True
            breakout_status = "Waterfall BD"
            chart_structure = "Velocity Ignition Slide"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = latest["timestamp"]
        elif latest_close <= morning_low * 1.002 and latest_close <= ema9:
            is_breakout = True
            breakout_status = "Breakdown"
            if chart_structure == "Base Building":
                chart_structure = "Breakdown Slide"
            if not breakout_time:
                breakout_time = datetime.fromtimestamp(latest["timestamp"], tz=INDIA_TZ).strftime("%I:%M %p")
                first_breakout_epoch = latest["timestamp"]
        elif latest_close <= morning_low * 1.008:
            breakout_status = "Near BD"
            if chart_structure == "Base Building":
                chart_structure = "Testing Support"

    # Session-wide Trend Invalidation Check (Bull/Bear Trap Protection):
    # A true One-Side Rally candidate must NEVER breach the opposite rail during the session.
    # Bullish: if any candle breached below morning_low AND currently below morning_high -> Bull Trap!
    # Bearish: if any candle breached above morning_high AND currently above morning_low -> Bear Trap!
    is_failed_trend = False
    if side == "bullish":
        has_dumped_below_low = any(c["close"] < morning_low for c in trading_candles) or latest_close < morning_low
        if has_dumped_below_low and latest_close < morning_high * 0.998:
            is_failed_trend = True
            is_breakout = False
            breakout_status = "Failed Breakout"
            chart_structure = "Bull Trap Reversal"
    elif side == "bearish":
        has_spiked_above_high = any(c["close"] > morning_high for c in trading_candles) or latest_close > morning_high
        if has_spiked_above_high and latest_close > morning_low * 1.002:
            is_failed_trend = True
            is_breakout = False
            breakout_status = "Failed Breakdown"
            chart_structure = "Bear Trap Reversal"

    # 5. Breakout Freshness & Timing Engine (On-Time Actionable Zones)
    breakout_freshness = "PENDING"
    breakout_age_minutes = None
    tight_stop = None

    if is_breakout and first_breakout_epoch:
        now_ist = datetime.now(INDIA_TZ)
        current_minutes = now_ist.hour * 60 + now_ist.minute
        is_live_market = now_ist.weekday() < 5 and (9 * 60 + 15) <= current_minutes <= (15 * 60 + 35)

        ref_epoch = int(now_ist.timestamp()) if is_live_market else (latest["timestamp"] + 300)
        breakout_close_epoch = first_breakout_epoch + 300
        breakout_age_minutes = max(0, int((ref_epoch - breakout_close_epoch) / 60))

        if side == "bullish":
            tight_stop = max(morning_high * 0.996, ema9 * 0.997)
            move_past_bo_pct = ((latest_close - morning_high) / morning_high) * 100 if morning_high else 0.0

            recent_4 = candles[-min(len(candles), 4):]
            touched_retest = any(c["low"] <= morning_high * 1.003 or c["low"] <= ema9 * 1.002 for c in recent_4)
            holding_retest = latest_close >= morning_high * 0.998 and latest_close >= ema9 and latest_close >= latest["open"]

            if breakout_age_minutes <= 15 and move_past_bo_pct <= 1.5:
                breakout_freshness = "FRESH"
            elif breakout_age_minutes > 15 and touched_retest and holding_retest:
                breakout_freshness = "RETEST"
            elif breakout_age_minutes > 45 or move_past_bo_pct > 2.5:
                breakout_freshness = "EXTENDED"
            else:
                breakout_freshness = "ACTIVE"
        else:
            tight_stop = min(morning_low * 1.004, ema9 * 1.003)
            move_past_bd_pct = ((morning_low - latest_close) / morning_low) * 100 if morning_low else 0.0

            recent_4 = candles[-min(len(candles), 4):]
            touched_retest = any(c["high"] >= morning_low * 0.997 or c["high"] >= ema9 * 0.998 for c in recent_4)
            holding_retest = latest_close <= morning_low * 1.002 and latest_close <= ema9 and latest_close <= latest["open"]

            if breakout_age_minutes <= 15 and move_past_bd_pct <= 1.5:
                breakout_freshness = "FRESH"
            elif breakout_age_minutes > 15 and touched_retest and holding_retest:
                breakout_freshness = "RETEST"
            elif breakout_age_minutes > 45 or move_past_bd_pct > 2.5:
                breakout_freshness = "EXTENDED"
            else:
                breakout_freshness = "ACTIVE"
    elif "near" in breakout_status.lower():
        breakout_freshness = "COILING"

    return {
        "is_breakout": is_breakout,
        "status": breakout_status,
        "breakout_time": breakout_time,
        "rvol_5m": round(latest_rvol, 2),
        "ema_trend": "bullish" if latest_close >= ema9 else "bearish",
        "chart_structure": chart_structure,
        "is_failed_trend": is_failed_trend,
        "is_cascade": cascade_info["is_cascade"],
        "cascade_type": cascade_info["cascade_type"],
        "cascade_time": cascade_info["cascade_time"],
        "cascade_body_pct": cascade_info["cascade_body_pct"],
        "cascade_vol_ratio": cascade_info["cascade_vol_ratio"],
        "breakout_freshness": breakout_freshness,
        "breakout_age_minutes": breakout_age_minutes,
        "tight_stop": tight_stop,
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


def compute_money_flow_percentage(
    row: dict[str, Any],
    oi: dict[str, Any] | None,
    *,
    side: str,
    vc_ranking: float,
    sector_alignment: float | None = None,
) -> float:
    """Computes money-flow percentage.
    Prioritizes real Level-2 order-book buying/selling depth from Upstox if authenticated,
    falling back to derived volume + OI + sector participation proxy.
    """
    sym = row.get("symbol")
    if sym:
        try:
            import upstox_provider
            depth = upstox_provider.fetch_market_depth(sym)
            if depth and "buying_pressure_pct" in depth:
                return float(depth["buying_pressure_pct"])
        except Exception:
            pass

    score = 0.0
    score += min(vc_ranking, 3.0) / 3.0 * 45.0

    oi_change = abs(float((oi or {}).get("oi_change_percent") or 0.0))
    score += min(oi_change, 25.0) / 25.0 * 35.0

    if sector_alignment is not None:
        score += max(0.0, min(20.0, abs(sector_alignment - 50.0) * 0.4))

    pct_change = row.get("percent_change")
    if pct_change is not None:
        direction_match = 1.0 if ((side == "bullish" and pct_change >= 0) or (side == "bearish" and pct_change <= 0)) else 0.0
        score += direction_match * 10.0

    return round(min(100.0, max(0.0, score)), 1)


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
    score_components: dict[str, float] = {}

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

    score_components["volume"] = score
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

    score_components["range_hold"] = score - sum(score_components.values())
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

    score_components["move_from_open"] = score - sum(score_components.values())
    # 4. Open Interest Setup & Squeeze Potential (Max 20 pts)
    is_v_reversal = (
        open_move is not None
        and ((side == "bullish" and open_move >= 0.5) or (side == "bearish" and open_move <= -0.5))
    )

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
        elif setup in ("Volume / Price Lead", "OI not confirmed", "Flat OI"):
            if activity_ratio >= 1.2:
                score += 15
                tags.append("volume-driven rally")
            else:
                score += 10
                tags.append("price momentum lead")
        elif is_v_reversal and open_move >= 0.5:
            score += 18
            tags.append("bear-trap reversal rally")
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
        elif setup in ("Volume / Price Lead", "OI not confirmed", "Flat OI"):
            if activity_ratio >= 1.2:
                score += 15
                tags.append("volume-driven slide")
            else:
                score += 10
                tags.append("downside price lead")
        elif is_v_reversal and open_move <= -0.5:
            score += 18
            tags.append("bull-trap reversal slide")
        else:
            blockers.append("OI setup not bearish")

    if oi_change_percent >= 15:
        score += 5
        tags.append("OI expansion")
    elif oi_change_percent >= 6:
        score += 3

    score_components["oi_setup_and_expansion"] = score - sum(score_components.values())
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
        elif is_v_reversal and open_move >= 0.5:
            score += 6
            tags.append(f"v-reversal from open (+{open_move:.2f}%)")
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
        elif is_v_reversal and open_move <= -0.5:
            score += 6
            tags.append(f"v-reversal below open ({open_move:.2f}%)")
        else:
            blockers.append(f"price move above {MIN_BEARISH_CHANGE:.2f}%")

    score_components["price_momentum"] = score - sum(score_components.values())
    # 6. Index Context Score (Max 8 pts)
    note, index_score = index_context_note(row, side, index_context)
    score += index_score
    score_components["index_context"] = index_score
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
        "score_components": score_components,
        "is_one_side": one_side,
    }


def build_trade_levels(
    row: dict[str, Any],
    *,
    side: str,
    bo_info: dict[str, Any] | None = None
) -> tuple[float | None, float | None, float | None, str]:
    ltp = row.get("ltp")
    high = row.get("high")
    low = row.get("low")
    open_price = row.get("open")
    if ltp is None:
        return None, None, None, "Real LTP missing; do not trade."

    buffer = max(round(ltp * 0.0008, 2), 0.5)
    if side == "bullish":
        entry = round((ltp if ltp is not None else high) + buffer, 2)
        tight = bo_info.get("tight_stop") if bo_info else None
        if tight and tight < entry:
            risk = entry - tight
            if risk < ltp * 0.004:
                stop = round(entry - max(ltp * 0.005, 3.0), 2)
            elif risk > ltp * 0.015:
                stop = round(entry - (ltp * 0.008), 2)
            else:
                stop = round(tight, 2)
        else:
            base_stop = low if low is not None else min(open_price or ltp, ltp - buffer)
            risk = min(max(entry - base_stop, max(ltp * 0.004, 3.0)), ltp * 0.010)
            stop = round(entry - risk, 2)

        final_risk = max(entry - stop, max(ltp * 0.003, 1.0))
        target = round(entry + (final_risk * 2.0), 2)
        plan = "Buy on confirmed breakout; SL below breakout rail/EMA9."
    else:
        entry = round((ltp if ltp is not None else low) - buffer, 2)
        tight = bo_info.get("tight_stop") if bo_info else None
        if tight and tight > entry:
            risk = tight - entry
            if risk < ltp * 0.004:
                stop = round(entry + max(ltp * 0.005, 3.0), 2)
            elif risk > ltp * 0.015:
                stop = round(entry + (ltp * 0.008), 2)
            else:
                stop = round(tight, 2)
        else:
            base_stop = high if high is not None else max(open_price or ltp, ltp + buffer)
            risk = min(max(base_stop - entry, max(ltp * 0.004, 3.0)), ltp * 0.010)
            stop = round(entry + risk, 2)

        final_risk = max(stop - entry, max(ltp * 0.003, 1.0))
        target = round(entry - (final_risk * 2.0), 2)
        plan = "Sell on confirmed breakdown; SL above breakdown rail/EMA9."
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
    audit: list[dict[str, Any]] | None = None,
    market_universe: list[dict[str, Any]] | None = None,
) -> tuple[list[Suggestion], int]:
    def record(row, decision_reason, **details):
        if audit is not None:
            audit.append({"symbol": row["symbol"], "side": side,
                          "setup": classify_oi(row.get("percent_change"), (oi_by_symbol.get(row["symbol"]) or {}).get("oi_change")),
                          "ltp": row.get("ltp"), "percent_change": row.get("percent_change"),
                          "volume": row.get("volume"), "qualified": False,
                          "included_in_payload": False, **details, "reason": decision_reason})

    for row in rows:
        symbol = row["symbol"]
        lot_size = get_fo_lot_size(symbol)
        if symbol in INDEX_LIKE_SYMBOLS:
            record(row, "index_symbol_excluded")
        elif lot_size <= 0:
            record(row, "non_fno_symbol_excluded")
        elif lot_size > MAX_ALLOWED_LOT_SIZE:
            record(row, "lot_size_above_limit", lot_size=lot_size)
    matched_rows = [
        row for row in rows
        if row["symbol"] not in INDEX_LIKE_SYMBOLS
        and 0 < get_fo_lot_size(row["symbol"]) <= MAX_ALLOWED_LOT_SIZE
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

    # Compute true sector alignment across the broader market universe once,
    # rather than with a single-stock list inside the loop (which broke money flow).
    universe_for_sector = market_universe if market_universe is not None else rows
    sector_alignments = compute_sector_alignment(universe_for_sector)

    suggestions: list[Suggestion] = []
    assessments: dict[str, dict[str, Any]] = {}
    breakout_assessments: dict[str, dict[str, Any]] = {}
    for row in matched_rows:
        symbol = row["symbol"]
        oi = oi_by_symbol.get(symbol, {})
        row_act = calc_stock_activity(row, oi)
        activity_ratio = (row_act / median_act) if median_act > 0 else 1.0
        vc_ranking = round(max(0.1, activity_ratio), 2)
        if vc_ranking <= MIN_VC_RANKING:
            record(row, "volume_confluence_below_minimum", vc_ranking=vc_ranking)
            continue

        stock_sector_align = sector_alignments.get(symbol, 50.0)
        money_flow_percentage = compute_money_flow_percentage(
            row,
            oi,
            side=side,
            vc_ranking=vc_ranking,
            sector_alignment=stock_sector_align,
        )

        setup = classify_oi(row.get("percent_change"), oi.get("oi_change"))
        base_score = calc_score(row, oi, side=side, activity_ratio=activity_ratio)
        rally = assess_one_side_rally(row, oi, side=side, activity_ratio=activity_ratio, index_context=index_context)
        assessments[symbol] = rally
        if not rally["is_one_side"]:
            record(row, "rally_gate_failed", rally=rally, base_score=base_score, vc_ranking=vc_ranking)
            continue

        # 5-Minute Breakout Analysis & 1-Minute Institutional Block Surge Radar
        sym_candles = candles_by_symbol.get(symbol, [])
        sym_1m_candles = _CANDLE_1M_CACHE.get(symbol, (0, []))[1]

        surge_info = detect_institutional_block_surge(
            symbol=symbol,
            candles_1m=sym_1m_candles,
            candles_5m=sym_candles,
            side=side,
            row=row,
        )

        bo_info = analyze_5m_breakout(sym_candles, side, row)
        bo_info["latest_candle_at_ist"] = (
            datetime.fromtimestamp(sym_candles[-1]["timestamp"], tz=INDIA_TZ).isoformat()
            if sym_candles else None
        )
        breakout_assessments[symbol] = bo_info
        five_min_status = bo_info["status"]
        breakout_time = bo_info["breakout_time"]
        is_breakout = bo_info["is_breakout"]

        # If institutional block surge arrived on forming/1m bar, upgrade status
        if surge_info.get("is_block_surge"):
            is_breakout = True
            if not breakout_time:
                breakout_time = surge_info.get("block_surge_time")
            if five_min_status.lower() == "consolidating":
                five_min_status = "Block Surge"

        # If it dropped below PML (Bullish) or above PMH (Bearish), it failed its trend.
        if bo_info.get("is_failed_trend") and not surge_info.get("is_block_surge"):
            record(row, "trend_invalidated", rally=rally, breakout=bo_info, vc_ranking=vc_ranking)
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
                bullish_floor = orb_check_high * (1 - PMH_PML_TOLERANCE_PCT / 100)
                if ltp_check is None or ltp_check < bullish_floor:
                    record(row, "below_opening_range_floor", rally=rally, breakout=bo_info, required_level=bullish_floor)
                    continue
                if prev_close_check is not None and ltp_check <= prev_close_check:
                    record(row, "not_above_previous_close", rally=rally, breakout=bo_info)
                    continue
            else:
                bearish_ceiling = orb_check_low * (1 + PMH_PML_TOLERANCE_PCT / 100)
                if ltp_check is None or ltp_check > bearish_ceiling:
                    record(row, "above_opening_range_ceiling", rally=rally, breakout=bo_info, required_level=bearish_ceiling)
                    continue
                if prev_close_check is not None and ltp_check >= prev_close_check:
                    record(row, "not_below_previous_close", rally=rally, breakout=bo_info)
                    continue

        # Only show active Breakout/Breakdown or Near BO/Near BD candidates (no Consolidating)
        if five_min_status.lower() == "consolidating" and not surge_info.get("is_block_surge"):
            record(row, "consolidating", rally=rally, breakout=bo_info)
            continue

        # Boost score and tag if active institutional block surge, 3-bar cascade or 5m breakout is confirmed
        rally_score = rally["score"]
        quality_tags = list(rally["quality_tags"])
        if surge_info.get("is_block_surge"):
            rally_score = min(100, rally_score + 20)
            stype = surge_info.get("block_surge_type") or "⚡ BLOCK SURGE"
            quality_tags.insert(0, f"{stype} ({surge_info.get('block_surge_time') or 'Active'}, ₹{surge_info.get('block_surge_turnover_cr', 0):.1f}Cr, {surge_info.get('block_surge_rvol', 1):.0f}x vol)")
        if bo_info.get("is_cascade"):
            rally_score = min(100, rally_score + 15)
            c_label = "3-Bar Waterfall" if side == "bearish" else "3-Bar Impulse"
            quality_tags.insert(0, f"⚡ {c_label} ({bo_info.get('cascade_time') or 'Active'}, {bo_info.get('cascade_body_pct', 0):.0f}% body)")
        elif is_breakout:
            rally_score = min(100, rally_score + 8)
            quality_tags.insert(0, f"5m Breakout ({breakout_time or 'Active'})")
        elif "near" in five_min_status.lower():
            quality_tags.insert(0, "Near 5m Breakout")
        elif bo_info.get("no_candle_data"):
            quality_tags.insert(0, "5m data pending (kept on OI + rally strength)")

        # Freshness bonuses / penalties for on-time prioritization
        freshness = bo_info.get("breakout_freshness", "PENDING")
        age_m = bo_info.get("breakout_age_minutes")
        if freshness == "FRESH":
            rally_score = min(100, rally_score + 10)
            age_str = f"{age_m}m ago" if age_m is not None else "Just now"
            quality_tags.insert(0, f"⚡ Fresh Breakout ({age_str})")
        elif freshness == "RETEST":
            rally_score = min(100, rally_score + 8)
            quality_tags.insert(0, "🔄 Retest Hold")
        elif freshness == "EXTENDED":
            rally_score = max(50, rally_score - 8)
            quality_tags.append("⚠️ Extended (Late move)")
        elif freshness == "COILING":
            quality_tags.insert(0, "⏳ Coiling Near Rail")

        entry, stop, target, plan = build_trade_levels(row, side=side, bo_info=bo_info)
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
        if (bo_info.get("is_cascade") or surge_info.get("is_block_surge")) and rally_score >= 65:
            status_val = "Priority Rally"
        # Matches the frontend bell (MLAIStockV2.html checkAndTriggerBellAlerts):
        # rings on confirmed breakout or active institutional block surge
        bell_rang = bool((is_breakout or bo_info.get("is_cascade") or surge_info.get("is_block_surge")) and "near" not in five_min_status.lower())

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
                money_flow_percentage=money_flow_percentage,
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
                is_cascade=bool(bo_info.get("is_cascade")),
                cascade_type=bo_info.get("cascade_type"),
                cascade_time=bo_info.get("cascade_time"),
                cascade_body_pct=float(bo_info.get("cascade_body_pct") or 0.0),
                cascade_vol_ratio=float(bo_info.get("cascade_vol_ratio") or 1.0),
                breakout_freshness=bo_info.get("breakout_freshness", "PENDING"),
                breakout_age_minutes=bo_info.get("breakout_age_minutes"),
                is_block_surge=bool(surge_info.get("is_block_surge")),
                block_surge_type=surge_info.get("block_surge_type"),
                block_surge_time=surge_info.get("block_surge_time"),
                block_surge_turnover_cr=float(surge_info.get("block_surge_turnover_cr") or 0.0),
                block_surge_rvol=float(surge_info.get("block_surge_rvol") or 1.0),
            )
        )

    # Priority Sort:
    # Combines Price Expansion Momentum with Institutional Block Surge Velocity.
    # Active breakouts/surges get Tier-0 priority, and block surge grants up to +5.0%
    # momentum boost to real movers (pct >= 0.75%) so that high-volume runners (Ather, Dr Reddy)
    # lead the board, while flat heavyweight stocks (+0.20%) do not displace rally leaders.
    if side == "bullish":
        def sort_key_bullish(item: Suggestion):
            pct = item.percent_change if item.percent_change is not None else -9999.0
            surge_boost = min(5.0, (item.block_surge_turnover_cr or 0.0) * 0.15) if (item.is_block_surge and pct >= 0.75) else 0.0
            cascade_boost = 1.0 if item.is_cascade else 0.0
            bo_boost = 0.5 if item.is_breakout else 0.0
            effective_momentum = pct + surge_boost + cascade_boost + bo_boost

            tier = 0 if (item.is_breakout or item.is_cascade or item.is_block_surge) else (1 if "near" in (item.five_min_status or "").lower() else 2)
            return (tier, -effective_momentum)

        suggestions.sort(key=sort_key_bullish)
    else:
        def sort_key_bearish(item: Suggestion):
            pct = item.percent_change if item.percent_change is not None else 9999.0
            surge_boost = min(5.0, (item.block_surge_turnover_cr or 0.0) * 0.15) if (item.is_block_surge and pct <= -0.75) else 0.0
            cascade_boost = 1.0 if item.is_cascade else 0.0
            bo_boost = 0.5 if item.is_breakout else 0.0
            effective_momentum = abs(pct) + surge_boost + cascade_boost + bo_boost

            tier = 0 if (item.is_breakout or item.is_cascade or item.is_block_surge) else (1 if "near" in (item.five_min_status or "").lower() else 2)
            return (tier, -effective_momentum)

        suggestions.sort(key=sort_key_bearish)

    if audit is not None:
        for index, item in enumerate(suggestions, start=1):
            record({"symbol": item.symbol, "ltp": item.ltp, "percent_change": item.percent_change,
                    "volume": item.volume}, "included" if index <= top else "payload_rank_limit",
                   **{**item.to_dict(), "side": side, "qualified": True,
                      "rank": index, "included_in_payload": index <= top,
                      "rally": assessments[item.symbol],
                      "breakout": breakout_assessments[item.symbol],
                      "scanner_reason": item.reason,
                      "breakout_bonus": 8 if item.is_breakout else 0})
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


def _passes_base_filter(symbol: str, oi_by_symbol: dict[str, dict[str, Any]] | None = None) -> bool:
    lot = get_fo_lot_size(symbol)
    return (
        symbol not in INDEX_LIKE_SYMBOLS
        and 0 < lot <= MAX_ALLOWED_LOT_SIZE
    )


CANDIDATE_LIFECYCLE_PATH = INPUT_DIR / "candidate_lifecycle.json"


def _lifecycle_paths() -> tuple[Path, Path, Path]:
    base = CANDIDATE_LIFECYCLE_PATH
    bak = base.with_name(f"{base.stem}.bak")
    lock = base.with_name(f"{base.stem}.lock")
    return base, bak, lock


@contextlib.contextmanager
def _lifecycle_file_lock():
    base, bak, lock = _lifecycle_paths()
    base.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_file.close()


def _load_lifecycle_state() -> dict[str, Any]:
    base, bak, _ = _lifecycle_paths()
    if base.exists():
        try:
            content = base.read_text(encoding="utf-8")
            if content.strip():
                return json.loads(content)
        except Exception as exc:
            print(f"[lifecycle] Primary state corrupted ({exc}), attempting backup...", file=sys.stderr)
            if bak.exists():
                try:
                    bak_content = bak.read_text(encoding="utf-8")
                    if bak_content.strip():
                        print("[lifecycle] Successfully recovered state from backup file.", file=sys.stderr)
                        return json.loads(bak_content)
                except Exception as bak_exc:
                    print(f"[lifecycle] Backup state also unreadable ({bak_exc}).", file=sys.stderr)

    return {"session_date": None, "candidates": {}}


def _save_lifecycle_state(state: dict[str, Any]) -> None:
    try:
        base, bak, _ = _lifecycle_paths()
        base.parent.mkdir(parents=True, exist_ok=True)
        # 1. Update backup copy if valid primary file currently exists
        if base.exists():
            try:
                raw_primary = base.read_text(encoding="utf-8")
                if raw_primary.strip():
                    bak.write_text(raw_primary, encoding="utf-8")
            except Exception:
                pass

        # 2. Atomic write of primary file using unique temporary file + os.replace
        temp_path = base.with_name(
            f"{base.name}.{os.getpid()}.{_time.time_ns()}.tmp"
        )
        temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(temp_path, base)
    except Exception as exc:  # noqa: BLE001
        print(f"[lifecycle] save skipped: {exc}", file=sys.stderr)


def _parse_time_minutes(time_str: str | None) -> int:
    """Parses 'HH:MM AM/PM' or 'HH:MM:SS' into minutes from midnight."""
    if not time_str or not isinstance(time_str, str):
        return -1
    s = time_str.strip().upper()
    try:
        if "AM" in s or "PM" in s:
            dt = datetime.strptime(s, "%I:%M %p")
            return dt.hour * 60 + dt.minute
        parts = s.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return -1


def evaluate_direction_evidence(item: Suggestion, opposite_item: Suggestion) -> float:
    """Computes an evidence score for resolving concurrent bullish/bearish collisions.
    Evidence hierarchy:
    1. Invalidation of old setup (if opposite is failed_trend, candidate gets +1000 pts).
    2. Completed-bar structure strength (Waterfall/Impulse cascade: +60 pts, Clean BO: +40 pts).
    3. Newest confirmed reversal timestamp (recency: more recent breakout/cascade wins).
    4. Confirmation / quality score (one_side_rally_score / score + volume activity).
    5. Net percentage change magnitude as final tie-breaker."""
    score = 0.0

    # 1. Invalidation of old setup
    if getattr(opposite_item, "is_failed_trend", False):
        score += 1000.0
    if getattr(item, "is_failed_trend", False):
        score -= 1000.0

    # 2. Completed-bar structure strength
    structure_weights = {
        "Waterfall Breakdown": 60.0,
        "Impulse Breakout": 60.0,
        "Clean Breakdown": 40.0,
        "Clean Breakout": 40.0,
        "Base Breakdown": 20.0,
        "Base Breakout": 20.0,
        "Active Waterfall": 50.0,
        "Active Rally": 30.0,
    }
    score += structure_weights.get(item.chart_structure, 10.0)
    score -= structure_weights.get(opposite_item.chart_structure, 10.0)

    if item.is_cascade:
        score += 30.0
    if opposite_item.is_cascade:
        score -= 30.0

    # 3. Newest confirmed reversal timestamp (minutes from midnight)
    t_item = max(_parse_time_minutes(item.breakout_time), _parse_time_minutes(item.cascade_time))
    t_opp = max(_parse_time_minutes(opposite_item.breakout_time), _parse_time_minutes(opposite_item.cascade_time))
    if t_item > t_opp and t_opp > 0:
        score += 25.0
    elif t_opp > t_item and t_item > 0:
        score -= 25.0

    # 4. Confirmation / quality score & volume activity
    q_item = float(item.one_side_rally_score or item.score or 0) + (item.vc_ranking * 2.0)
    q_opp = float(opposite_item.one_side_rally_score or opposite_item.score or 0) + (opposite_item.vc_ranking * 2.0)
    score += (q_item - q_opp) * 0.2

    # 5. Net percentage change magnitude as final tie-breaker
    chg_item = abs(item.percent_change or 0.0)
    chg_opp = abs(opposite_item.percent_change or 0.0)
    score += (chg_item - chg_opp) * 2.0

    return score


def claim_alert_delivery(
    alert_event_id: str,
    worker_id: str,
    lease_seconds: float = 30.0,
    now_epoch: float | None = None,
) -> bool:
    """Atomic claim state machine: PENDING -> CLAIMED -> DELIVERED -> ACKNOWLEDGED.
    Guarantees that between multiple concurrent workers (e.g. 2 WebSocket workers),
    only the worker that successfully claims PENDING -> CLAIMED may broadcast the alert."""
    if not alert_event_id:
        return False
    if now_epoch is None:
        now_epoch = _time.time()

    with _lifecycle_file_lock():
        state = _load_lifecycle_state()
        candidates = state.get("candidates", {})
        target_entry = None
        for entry in candidates.values():
            if entry.get("alert_event_id") == alert_event_id:
                target_entry = entry
                break

        if not target_entry:
            return False

        if target_entry.get("alert_acknowledged_at"):
            return False

        status = target_entry.get("delivery_status", "PENDING")

        if status == "PENDING":
            target_entry["delivery_status"] = "CLAIMED"
            target_entry["claimed_by"] = worker_id
            target_entry["claimed_at"] = now_epoch
            target_entry["claim_expires_at"] = now_epoch + lease_seconds
            _save_lifecycle_state(state)
            return True
        elif status == "CLAIMED":
            # Check if lease expired (worker crashed or timed out)
            expires_at = target_entry.get("claim_expires_at", 0.0)
            if now_epoch > expires_at:
                target_entry["claimed_by"] = worker_id
                target_entry["claimed_at"] = now_epoch
                target_entry["claim_expires_at"] = now_epoch + lease_seconds
                _save_lifecycle_state(state)
                return True
            return False
        elif status in ("DELIVERED", "ACKNOWLEDGED"):
            return False

        return False


def mark_alert_delivered(
    alert_event_id: str,
    worker_id: str | None = None,
    delivered_at_ist: str | None = None,
) -> bool:
    """Transitions state from CLAIMED -> DELIVERED after successful transmission."""
    with _lifecycle_file_lock():
        state = _load_lifecycle_state()
        candidates = state.get("candidates", {})
        found = False
        if delivered_at_ist is None:
            delivered_at_ist = datetime.now(INDIA_TZ).strftime("%H:%M:%S")
        for entry in candidates.values():
            if entry.get("alert_event_id") == alert_event_id:
                entry["delivery_status"] = "DELIVERED"
                entry["alert_delivered_at"] = delivered_at_ist
                found = True
        if found:
            _save_lifecycle_state(state)
        return found


def acknowledge_alert(
    alert_event_id: str,
    acknowledged_at_ist: str | None = None,
    client_id: str | None = None,
) -> bool:
    """Marks an alert event as delivered and acknowledged by the UI client.
    Guarantees 'one logical alert' delivery semantics even if UI was disconnected.
    Strictly validates alert_event_id format and existence in candidate lifecycle store."""
    if not alert_event_id or not isinstance(alert_event_id, str):
        return False

    import re
    if not re.match(r"^evt_[A-Za-z0-9_-]+$", alert_event_id):
        return False

    with _lifecycle_file_lock():
        state = _load_lifecycle_state()
        candidates = state.get("candidates", {})
        found = False
        if acknowledged_at_ist is None:
            acknowledged_at_ist = datetime.now(INDIA_TZ).strftime("%H:%M:%S")
        for entry in candidates.values():
            if entry.get("alert_event_id") == alert_event_id:
                entry["delivery_status"] = "ACKNOWLEDGED"
                entry["alert_acknowledged_at"] = acknowledged_at_ist
                entry["alert_delivered_at"] = entry.get("alert_delivered_at") or acknowledged_at_ist
                if client_id:
                    entry.setdefault("acknowledged_by_clients", []).append(client_id)
                found = True
                break
        if found:
            _save_lifecycle_state(state)
        return found


def _invalidate_lifecycle_entry(
    cands_store: dict[str, dict[str, Any]],
    symbol: str,
    side: str,
    market_clock_ist: str,
    now_epoch: float,
    reason: str,
) -> None:
    key = f"{symbol}_{side}"
    entry = cands_store.get(key)
    if entry:
        entry["is_active"] = False
        entry["is_invalidated"] = True
        entry["invalidated_at"] = market_clock_ist
        entry["invalidated_epoch"] = now_epoch
        entry["invalidation_reason"] = reason
        entry["delivery_status"] = "PENDING"
        entry["claimed_by"] = None
        entry["claimed_at"] = None
        entry["claim_expires_at"] = None
        # 3 completed 5m candles = 15 minutes cooldown (900 seconds)
        entry["cooldown_until_epoch"] = now_epoch + 900.0
    else:
        cands_store[key] = {
            "symbol": symbol,
            "side": side,
            "first_qualified_at": None,
            "first_top5_at": None,
            "first_rank1_at": None,
            "first_alerted_at": None,
            "alert_fired": False,
            "alert_count": 0,
            "alert_event_id": None,
            "alert_created_at": None,
            "alert_delivered_at": None,
            "alert_acknowledged_at": None,
            "delivery_status": "PENDING",
            "claimed_by": None,
            "claimed_at": None,
            "claim_expires_at": None,
            "current_rank": 99,
            "is_active": False,
            "is_invalidated": True,
            "invalidated_at": market_clock_ist,
            "invalidated_epoch": now_epoch,
            "invalidation_reason": reason,
            "cooldown_until_epoch": now_epoch + 900.0,
            "last_breakout_level": 0.0,
            "last_structure": None,
        }


def _process_candidate_lifecycle(
    cands_store: dict[str, dict[str, Any]],
    item: Suggestion,
    side: str,
    rank: int,
    market_clock_ist: str,
    now_epoch: float,
) -> Suggestion:
    key = f"{item.symbol}_{side}"
    entry = cands_store.get(key)

    # Rule 8: Stale fallback data CANNOT invalidate or requalify setups
    if getattr(item, "is_stale_data", False) or getattr(item, "is_stale_fallback", False):
        d = item.to_dict()
        if entry:
            d["first_qualified_at"] = entry.get("first_qualified_at")
            d["first_top5_at"] = entry.get("first_top5_at")
            d["first_rank1_at"] = entry.get("first_rank1_at")
            d["first_alerted_at"] = entry.get("first_alerted_at")
            d["alert_event_id"] = entry.get("alert_event_id")
            d["alert_created_at"] = entry.get("alert_created_at")
            d["alert_delivered_at"] = entry.get("alert_delivered_at")
            d["alert_acknowledged_at"] = entry.get("alert_acknowledged_at")
            d["alert_count"] = entry.get("alert_count", 0)
            d["delivery_status"] = entry.get("delivery_status", "PENDING")
            d["claimed_by"] = entry.get("claimed_by")
            d["claimed_at"] = entry.get("claimed_at")
        d["should_alert"] = False
        d["bell_rang"] = False
        return Suggestion(**d)

    is_qualified = bool(item.is_breakout or item.is_cascade or "near" in (item.five_min_status or "").lower())
    is_confirmed_breakout = bool(item.is_breakout or item.is_cascade)
    breakout_level = item.entry or item.ltp or 0.0

    if entry is None:
        # First time seen this session
        alert_fired = bool(item.bell_rang)
        alert_count = 1 if alert_fired else 0
        event_id = f"evt_{item.symbol}_{side}_w1_{int(now_epoch)}" if alert_fired else None
        created_at = market_clock_ist if alert_fired else None

        entry = {
            "symbol": item.symbol,
            "side": side,
            "first_qualified_at": market_clock_ist if is_qualified else None,
            "first_top5_at": market_clock_ist if rank <= 5 else None,
            "first_rank1_at": market_clock_ist if rank == 1 else None,
            "first_alerted_at": market_clock_ist if alert_fired else None,
            "alert_fired": alert_fired,
            "alert_count": alert_count,
            "alert_event_id": event_id,
            "alert_created_at": created_at,
            "alert_delivered_at": None,
            "alert_acknowledged_at": None,
            "delivery_status": "PENDING" if alert_fired else "ACKNOWLEDGED",
            "claimed_by": None,
            "claimed_at": None,
            "claim_expires_at": None,
            "current_rank": rank,
            "is_active": True,
            "is_invalidated": False,
            "invalidated_at": None,
            "invalidation_reason": None,
            "cooldown_until_epoch": 0.0,
            "last_breakout_level": breakout_level,
            "last_structure": item.chart_structure,
        }
        cands_store[key] = entry
    else:
        # Candidate already exists in lifecycle store
        entry["is_active"] = True

        # Rule 9: Preserve original timestamps; do not overwrite during refreshes
        if is_qualified and not entry.get("first_qualified_at"):
            entry["first_qualified_at"] = market_clock_ist

        # Rule 3: Rank movement or leaving/returning to Top 5 does not overwrite first_top5_at
        if rank <= 5 and not entry.get("first_top5_at"):
            entry["first_top5_at"] = market_clock_ist

        # Rule 2: Rank movement to #1 records first_rank1_at once
        if rank == 1 and not entry.get("first_rank1_at"):
            entry["first_rank1_at"] = market_clock_ist

        entry["current_rank"] = rank

        # Rule 9 Check: Close-based stop-loss evaluation
        # Completed candle close is checked; if not available, fallback to ltp.
        # Intrabar low/high wick touches that recover by completed candle close do NOT invalidate.
        if getattr(item, "is_failed_trend", False):
            _invalidate_lifecycle_entry(cands_store, item.symbol, side, market_clock_ist, now_epoch, "Trend Invalidation / Trap")
        elif item.stop_loss is not None:
            check_price = getattr(item, "completed_candle_close", None)
            if check_price is None:
                check_price = item.ltp

            if check_price is not None:
                if side == "bullish" and check_price < item.stop_loss:
                    _invalidate_lifecycle_entry(cands_store, item.symbol, side, market_clock_ist, now_epoch, "Stop Loss Breached")
                elif side == "bearish" and check_price > item.stop_loss:
                    _invalidate_lifecycle_entry(cands_store, item.symbol, side, market_clock_ist, now_epoch, "Stop Loss Breached")

        if entry.get("is_invalidated"):
            # Setup was previously invalidated.
            # Rules 6 & 7: Check if eligible for a SECOND same-direction alert
            cooldown_passed = now_epoch >= entry.get("cooldown_until_epoch", 0.0)
            
            # Genuinely new structure check:
            old_level = entry.get("last_breakout_level", 0.0)
            if side == "bullish":
                new_structure = (breakout_level > old_level * 1.002) and is_confirmed_breakout
            else:
                new_structure = (breakout_level < old_level * 0.998) and is_confirmed_breakout

            if cooldown_passed and new_structure:
                # Genuinely new structure formed after cooldown!
                new_count = entry.get("alert_count", 1) + 1
                entry["is_invalidated"] = False
                entry["invalidated_at"] = None
                entry["invalidation_reason"] = None
                entry["last_breakout_level"] = breakout_level
                entry["last_structure"] = item.chart_structure
                entry["alert_count"] = new_count
                entry["alert_fired"] = True
                entry["alert_event_id"] = f"evt_{item.symbol}_{side}_w{new_count}_{int(now_epoch)}"
                entry["alert_created_at"] = market_clock_ist
                entry["alert_delivered_at"] = None
                entry["alert_acknowledged_at"] = None
                entry["delivery_status"] = "PENDING"
                entry["claimed_by"] = None
                entry["claimed_at"] = None
                entry["claim_expires_at"] = None
        else:
            # Active, valid setup
            if not entry.get("alert_fired") and item.bell_rang:
                # Rule 1: Alert ONLY ONCE when first qualified
                entry["first_alerted_at"] = market_clock_ist
                entry["alert_fired"] = True
                entry["alert_count"] = 1
                entry["last_breakout_level"] = breakout_level
                entry["last_structure"] = item.chart_structure
                entry["alert_event_id"] = f"evt_{item.symbol}_{side}_w1_{int(now_epoch)}"
                entry["alert_created_at"] = market_clock_ist
                entry["alert_delivered_at"] = None
                entry["alert_acknowledged_at"] = None
                entry["delivery_status"] = "PENDING"
                entry["claimed_by"] = None
                entry["claimed_at"] = None
                entry["claim_expires_at"] = None

    # An alert remains active/deliverable as long as it was created and has not been acknowledged
    should_alert = bool(
        entry.get("alert_event_id")
        and entry.get("alert_created_at")
        and not entry.get("alert_acknowledged_at")
    )

    # Return updated Suggestion with lifecycle attributes
    d = item.to_dict()
    d["rank"] = rank
    d["first_qualified_at"] = entry.get("first_qualified_at")
    d["first_top5_at"] = entry.get("first_top5_at")
    d["first_rank1_at"] = entry.get("first_rank1_at")
    d["first_alerted_at"] = entry.get("first_alerted_at")
    d["alert_event_id"] = entry.get("alert_event_id")
    d["alert_created_at"] = entry.get("alert_created_at")
    d["alert_delivered_at"] = entry.get("alert_delivered_at")
    d["alert_acknowledged_at"] = entry.get("alert_acknowledged_at")
    d["delivery_status"] = entry.get("delivery_status", "PENDING")
    d["claimed_by"] = entry.get("claimed_by")
    d["claimed_at"] = entry.get("claimed_at")
    d["should_alert"] = should_alert
    d["alert_count"] = entry.get("alert_count", 0)
    d["bell_rang"] = should_alert

    return Suggestion(**d)


def enforce_candidate_lifecycle_and_mutual_exclusion(
    session_date: str,
    market_clock_ist: str,
    bullish: list[Suggestion],
    bearish: list[Suggestion],
    now_epoch: float | None = None,
    is_trading_session: bool | None = None,
) -> tuple[list[Suggestion], list[Suggestion]]:
    if now_epoch is None:
        now_epoch = _time.time()

    with _lifecycle_file_lock():
        state = _load_lifecycle_state()

        # Check if date is a weekend (Saturday=5, Sunday=6)
        is_weekend = False
        try:
            is_weekend = datetime.fromisoformat(session_date).weekday() >= 5
        except Exception:
            pass

        if is_trading_session is False or is_weekend:
            # Rule 7: Weekend / holiday / closed market refresh must NOT create a fake new session
            # Keep previous active session candidates intact
            if state.get("candidates"):
                cands_store = state.get("candidates", {})
            else:
                cands_store = {}
        else:
            if state.get("session_date") != session_date:
                state = {"session_date": session_date, "candidates": {}}
            cands_store: dict[str, dict[str, Any]] = state.get("candidates", {})

        # Step 1: Evidence-Based Mutual Exclusion between Bullish and Bearish (Rule 8 & Concern 3)
        bull_map = {item.symbol: item for item in bullish}
        bear_map = {item.symbol: item for item in bearish}
        overlap_symbols = set(bull_map.keys()) & set(bear_map.keys())

        for sym in overlap_symbols:
            b_item = bull_map[sym]
            br_item = bear_map[sym]
            evidence = evaluate_direction_evidence(b_item, br_item)
            if evidence >= 0:
                del bear_map[sym]
                _invalidate_lifecycle_entry(cands_store, sym, "bearish", market_clock_ist, now_epoch, "Direction reversed to Bullish (Evidence resolved)")
            else:
                del bull_map[sym]
                _invalidate_lifecycle_entry(cands_store, sym, "bullish", market_clock_ist, now_epoch, "Direction reversed to Bearish (Evidence resolved)")

        # Step 2: Check Direction Reversals against historical session state
        for sym in list(bull_map.keys()):
            opposite_key = f"{sym}_bearish"
            if opposite_key in cands_store and not cands_store[opposite_key].get("is_invalidated"):
                _invalidate_lifecycle_entry(cands_store, sym, "bearish", market_clock_ist, now_epoch, "Direction reversed to Bullish")

        for sym in list(bear_map.keys()):
            opposite_key = f"{sym}_bullish"
            if opposite_key in cands_store and not cands_store[opposite_key].get("is_invalidated"):
                _invalidate_lifecycle_entry(cands_store, sym, "bullish", market_clock_ist, now_epoch, "Direction reversed to Bearish")
            if opposite_key in cands_store and not cands_store[opposite_key].get("is_invalidated"):
                _invalidate_lifecycle_entry(cands_store, sym, "bearish", market_clock_ist, now_epoch, "Direction reversed to Bullish")

        for sym in list(bear_map.keys()):
            opposite_key = f"{sym}_bullish"
            if opposite_key in cands_store and not cands_store[opposite_key].get("is_invalidated"):
                _invalidate_lifecycle_entry(cands_store, sym, "bullish", market_clock_ist, now_epoch, "Direction reversed to Bearish")

        # Step 3: Process Bullish Candidates Lifecycle
        final_bullish = []
        for rank, item in enumerate(bull_map.values(), start=1):
            enriched_item = _process_candidate_lifecycle(
                cands_store=cands_store,
                item=item,
                side="bullish",
                rank=rank,
                market_clock_ist=market_clock_ist,
                now_epoch=now_epoch,
            )
            final_bullish.append(enriched_item)

        # Step 4: Process Bearish Candidates Lifecycle
        final_bearish = []
        for rank, item in enumerate(bear_map.values(), start=1):
            enriched_item = _process_candidate_lifecycle(
                cands_store=cands_store,
                item=item,
                side="bearish",
                rank=rank,
                market_clock_ist=market_clock_ist,
                now_epoch=now_epoch,
            )
            final_bearish.append(enriched_item)

        # Mark candidates not present in this snapshot as not currently active
        active_keys = {f"{sym}_bullish" for sym in bull_map} | {f"{sym}_bearish" for sym in bear_map}
        for k, v in cands_store.items():
            if k not in active_keys:
                v["is_active"] = False

        state["candidates"] = cands_store
        if not is_weekend and is_trading_session is not False:
            state["session_date"] = session_date
        _save_lifecycle_state(state)

        return final_bullish, final_bearish


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
            entry_timing = "EARLY IGNITION ⚡" if (bo_min is not None and bo_min <= 9 * 60 + 45) else "CONTINUATION 🔄"
            entry_price = round(float(item.entry or item.ltp or 0), 2)
            scored.append({
                "symbol": item.symbol,
                "sector": get_sector(item.symbol),
                "lot_size": item.lot_size,
                "ltp": item.ltp,
                "entry_price": entry_price,
                "percent_change": item.percent_change,
                "entry_timing": entry_timing,
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

        # Backfill entry_price and entry_timing if missing from an earlier snapshot
        for side_name in ("bullish", "bearish"):
            for entry in state["top5"].get(side_name, []):
                if entry.get("entry_price") is None:
                    entry["entry_price"] = entry.get("ltp")
                if entry.get("entry_timing") is None:
                    bo_min = _parse_clock_to_minutes(entry.get("breakout_time"))
                    entry["entry_timing"] = "EARLY IGNITION ⚡" if (bo_min is not None and bo_min <= 9 * 60 + 45) else "CONTINUATION 🔄"

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
        from scanner_telemetry import signal_id
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
                    existing.setdefault("signal_id", signal_id(session_date, item.symbol, side_name, existing.get("setup")))
                    if not existing.get("upgraded_to_priority") and item.status == "Priority Rally" and item.bell_rang:
                        existing["upgraded_to_priority"] = True
                        existing["upgraded_at_ist"] = payload.get("market_clock_ist")
                        existing["signal_id"] = signal_id(session_date, item.symbol, side_name, existing.get("setup"))
                        existing["qualified_at_ist"] = datetime.now(INDIA_TZ).isoformat(timespec="milliseconds")
                        existing["snapshot_id"] = payload.get("snapshot_id")
                        _log_event_to_db("instant_trigger", session_date, item.symbol, side_name, existing)
                    continue

                if not (item.is_breakout or item.is_block_surge):
                    continue  # only pin on a confirmed breakout or institutional block surge, not Near BO/BD

                breakdown = score_instant_trigger_factors(item, side_name, index_context, sector_alignment)
                is_upgraded = bool(item.status == "Priority Rally" and item.bell_rang)
                new_item = {
                    "signal_id": signal_id(session_date, item.symbol, side_name, item.setup),
                    "snapshot_id": payload.get("snapshot_id"),
                    "detected_at_ist": datetime.now(INDIA_TZ).isoformat(timespec="milliseconds"),
                    "qualified_at_ist": datetime.now(INDIA_TZ).isoformat(timespec="milliseconds") if is_upgraded else None,
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
                    "is_block_surge": item.is_block_surge,
                    "block_surge_turnover_cr": item.block_surge_turnover_cr,
                    "block_surge_rvol": item.block_surge_rvol,
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
    # Postgres is the durable source of truth - a local-file-only state gets
    # silently wiped whenever the process restarts (Render's ephemeral
    # filesystem on every redeploy/idle-sleep, or just not running
    # continuously), losing whatever was still pending mid-day and, worse,
    # leaving a stale local file that gets re-flushed as a duplicate the next
    # time it IS loaded. Try Postgres first; fall back to the local file only
    # if the database is unavailable.
    try:
        import db_adapter
        if db_adapter.DATABASE_URL:
            conn = db_adapter.get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT session_date, items FROM outcome_tracker_pending_state WHERE id = 1")
            row = cur.fetchone()
            cur.close()
            if row is not None:
                session_date, items = row
                return {
                    "session_date": str(session_date) if session_date else None,
                    "items": items or [],
                }
    except Exception as exc:  # noqa: BLE001
        print(f"[outcome_tracker] postgres load skipped: {exc}", file=sys.stderr)

    try:
        return json.loads(OUTCOME_TRACKER_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"session_date": None, "items": []}


def _save_outcome_tracker_state(state: dict[str, Any]) -> None:
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        OUTCOME_TRACKER_STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[outcome_tracker] local save skipped: {exc}", file=sys.stderr)

    try:
        import db_adapter
        if db_adapter.DATABASE_URL:
            conn = db_adapter.get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO outcome_tracker_pending_state (id, session_date, items, updated_at) "
                "VALUES (1, %s, %s::jsonb, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "session_date = EXCLUDED.session_date, items = EXCLUDED.items, updated_at = NOW()",
                (state.get("session_date"), json.dumps(state.get("items", []), default=str)),
            )
            conn.commit()
            cur.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[outcome_tracker] postgres save skipped: {exc}", file=sys.stderr)


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
                "signal_id": trig.get("signal_id"),
                "origin_snapshot_id": trig.get("snapshot_id"),
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
                passes_level = ltp >= level * (1 - PMH_PML_TOLERANCE_PCT / 100)
                passes_prev = prev_close is not None and ltp > prev_close
            else:
                level = orb_low
                passes_level = ltp <= level * (1 + PMH_PML_TOLERANCE_PCT / 100)
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
    import scanner_telemetry
    import scanner_trace
    scan_id = scanner_telemetry.new_scan()
    decision_audit: list[dict[str, Any]] = []
    now_ist = datetime.now(INDIA_TZ)
    scanner_trace.begin(scan_id, now_ist.isoformat())
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
        and 0 < lot <= MAX_ALLOWED_LOT_SIZE
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

    market_universe_rows = list((*gainers, *losers))
    bullish, gainer_stock_overlap = build_suggestions(
        gainers, oi_by_symbol, side="bullish", top=top,
        index_context=index_context, candles_by_symbol=shared_candles, audit=decision_audit,
        market_universe=market_universe_rows,
    )
    bearish, loser_stock_overlap = build_suggestions(
        losers, oi_by_symbol, side="bearish", top=top,
        index_context=index_context, candles_by_symbol=shared_candles, audit=decision_audit,
        market_universe=market_universe_rows,
    )
    # Enforce candidate lifecycle rules and mutual exclusion
    session_date = now_ist.date().isoformat()
    market_clock_ist = now_ist.strftime("%H:%M:%S")
    bullish, bearish = enforce_candidate_lifecycle_and_mutual_exclusion(
        session_date=session_date,
        market_clock_ist=market_clock_ist,
        bullish=bullish,
        bearish=bearish,
    )
    gainers_with_oi = sum(
        1 for row in gainers
        if row["symbol"] not in INDEX_LIKE_SYMBOLS
        and 0 < get_fo_lot_size(row["symbol"]) <= MAX_ALLOWED_LOT_SIZE
    )
    losers_with_oi = sum(
        1 for row in losers
        if row["symbol"] not in INDEX_LIKE_SYMBOLS
        and 0 < get_fo_lot_size(row["symbol"]) <= MAX_ALLOWED_LOT_SIZE
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
        "snapshot_id": scan_id,
        "scanner_config": {"min_one_side_score": MIN_ONE_SIDE_SCORE,
                           "min_vc_ranking": MIN_VC_RANKING,
                           "max_lot_size": MAX_ALLOWED_LOT_SIZE,
                           "pmh_pml_tolerance_pct": PMH_PML_TOLERANCE_PCT,
                           "max_suggestions_per_side": top},
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
    try:
        replay = scanner_trace.replay_inputs(sys.modules[__name__], gainers, losers, oi_by_symbol,
            index_context, shared_candles, scanner_trace.finish_capture(), extra_symbols)
        replay["missing_extra_candidates"] = sorted(set(extra_symbols)-{r["symbol"] for r in gainers+losers})
        replay["missing_candidate_note"] = "requested but no normalized row returned; consult source failures/timeouts"
        scanner_telemetry.prepare_snapshot(payload, decision_audit, scan_id, replay=replay)
    except Exception as exc:
        print(f"[scanner-telemetry] scan logging skipped: {exc}", file=sys.stderr)
        scanner_trace.health("trace_failed", snapshot_id=scan_id, error_type=type(exc).__name__)
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


def build_premarket_reset_payload(now_ist: datetime) -> dict[str, Any]:
    """Called once, shortly before market open (see the background scanner in
    serve_nifty50_app.py), to actually clear yesterday's leftover picks
    instead of leaving the last real scan's full numbers on screen looking
    "live" until the first 9:15+ scan completes (which can take 30+ seconds).
    Resets the per-feature state files to today/empty and returns an honest,
    empty payload shaped like build_payload()'s real output - the dashboard
    should look freshly ready, not like yesterday's close."""
    session_date = now_ist.date().isoformat()

    _save_tracked_state({"session_date": session_date, "items": []})
    _save_daily_top5_state({
        "session_date": session_date, "window_start_ist": None, "locked_at_ist": None,
        "top5": {"bullish": [], "bearish": []},
    })
    _save_instant_triggers_state({"session_date": session_date, "items": []})
    _save_top5_eligible_state({"session_date": session_date, "first_seen": {}})
    _save_lifecycle_state({"session_date": session_date, "candidates": {}})
    # Deliberately NOT touching the Outcome Tracker's pending state - those
    # items are still waiting on in-progress +15/30/60min checks from the
    # prior session and must be left alone to finish naturally.

    return {
        "ok": True,
        "data_mode": "real",
        "generated_at_ist": now_ist.isoformat(timespec="seconds"),
        "session_date": session_date,
        "market_clock_ist": now_ist.strftime("%H:%M:%S"),
        "analysis_phase": phase_for_time(now_ist),
        "strategy_name": "One-Side Rally Stock Suggestion Index",
        "source": {},
        "index_context": {},
        "coverage": {
            "gainers_loaded": 0, "losers_loaded": 0, "oi_symbols_loaded": 0,
            "gainers_with_oi_match": 0, "losers_with_oi_match": 0, "top_rows_with_oi_match": 0,
            "gainer_stock_overlap_before_rally_filter": 0, "loser_stock_overlap_before_rally_filter": 0,
            "max_suggestions_per_side": DEFAULT_SUGGESTIONS_PER_SIDE, "one_side_min_score": MIN_ONE_SIDE_SCORE,
        },
        "summary": {"bullish_candidates": 0, "bearish_candidates": 0, "priority_bullish": 0, "priority_bearish": 0},
        "bullish": [],
        "bearish": [],
        "warnings": [],
        "market_universe": [],
        "tracked_breakouts": [],
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
        if event_type == "outcome":
            # A stuck/stale local outcome_tracker_state.json (e.g. after a
            # container restart wipes tracking mid-day) can get re-flushed on
            # a later day's session-rollover, writing the same logical
            # (date, symbol, side, trigger_time) outcome again. Upsert on
            # that natural key instead of blindly inserting, backed by
            # idx_outcome_dedup, so re-flushes overwrite rather than pile up.
            cur.execute(
                "INSERT INTO scanner_event_log (event_type, session_date, symbol, side, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (event_type, session_date, symbol, side, (payload->>'trigger_time_ist')) "
                "WHERE event_type = 'outcome' "
                "DO UPDATE SET payload = EXCLUDED.payload, created_at = NOW()",
                (event_type, session_date, symbol, side, json.dumps(payload, default=str)),
            )
        else:
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
    write server Top-6 eligibility evidence, not proof of browser visibility.
    Actual viewport appearances are recorded separately as signal_displayed."""
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
                    "signal_id": row.get("signal_id"),
                    "snapshot_id": payload.get("snapshot_id"),
                    "evidence_type": "server_top6_eligibility",
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
        import scanner_trace
        scanner_trace.health("scan_failed", error_type=type(exc).__name__, sources=scanner_trace.finish_capture())
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
