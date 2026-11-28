#!/usr/bin/env python3
"""Upstox Market Data Provider for NIFTY50 AI Prediction Engine.

Provides official, high-speed 5m intraday candle aggregation,
Level-2 market depth (real buying/selling pressure), and OAuth2 authentication.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "inputs"
CREDENTIALS_FILE = INPUT_DIR / "upstox_credentials.json"
TOKEN_FILE = INPUT_DIR / "upstox_token.json"
INSTRUMENTS_FILE = INPUT_DIR / "upstox_instruments.json"

INDIA_TZ = timezone(timedelta(hours=5, minutes=30))
UPSTOX_API_BASE = "https://api.upstox.com/v2"

_INSTRUMENTS_CACHE: dict[str, Any] | None = None
_INSTRUMENTS_LOCK = threading.Lock()
_INSTRUMENTS_REFRESH_ATTEMPT_TS = 0.0
_FUTURES_CACHE: dict[tuple[str, bool], tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = {}
_FUTURES_CACHE_TTL_SEC = 60.0
_FUTURES_OI_SUMMARY_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_credentials() -> dict[str, str]:
    creds = {}
    if CREDENTIALS_FILE.exists():
        try:
            creds = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    api_key = os.environ.get("UPSTOX_API_KEY") or os.environ.get("UPSTOX_CLIENT_ID") or creds.get("api_key", "")
    api_secret = os.environ.get("UPSTOX_API_SECRET") or os.environ.get("UPSTOX_CLIENT_SECRET") or creds.get("api_secret", "")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI") or creds.get("redirect_uri", "http://127.0.0.1:8000/api/upstox/callback")
    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "redirect_uri": redirect_uri,
    }


def load_token() -> dict[str, Any]:
    token_data = {}
    if TOKEN_FILE.exists():
        try:
            token_data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not token_data.get("access_token") and os.environ.get("UPSTOX_ACCESS_TOKEN"):
        token_data["access_token"] = os.environ.get("UPSTOX_ACCESS_TOKEN")
    canonical_token_file = (INPUT_DIR / "upstox_token.json").resolve()
    if not token_data.get("access_token") and TOKEN_FILE.resolve() == canonical_token_file:
        try:
            import db_adapter
            cloud_token = db_adapter.load_cloud_state("upstox_token")
            if isinstance(cloud_token, dict) and cloud_token.get("access_token"):
                token_data = cloud_token
                try:
                    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                    TOKEN_FILE.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            pass
    return token_data


def save_token(token_data: dict[str, Any]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token_data["saved_at_epoch"] = time.time()
    token_data["saved_at_ist"] = datetime.now(INDIA_TZ).isoformat()
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    canonical_token_file = (INPUT_DIR / "upstox_token.json").resolve()
    if TOKEN_FILE.resolve() == canonical_token_file:
        try:
            import db_adapter
            db_adapter.save_cloud_state("upstox_token", token_data)
        except Exception:
            pass


def get_login_url() -> str:
    creds = load_credentials()
    api_key = creds.get("api_key", "")
    redirect_uri = creds.get("redirect_uri", "http://127.0.0.1:8000/api/upstox/callback")
    params = {
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
    }
    return f"{UPSTOX_API_BASE}/login/authorization/dialog?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(code: str) -> dict[str, Any]:
    import requests
    creds = load_credentials()
    api_key = creds.get("api_key", "")
    api_secret = creds.get("api_secret", "")
    redirect_uri = creds.get("redirect_uri", "http://127.0.0.1:8000/api/upstox/callback")

    url = f"{UPSTOX_API_BASE}/login/authorization/token"
    payload = {
        "code": code,
        "client_id": api_key,
        "client_secret": api_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    resp = requests.post(url, data=payload, headers=headers, timeout=15)
    try:
        result = resp.json()
    except Exception:
        result = {"raw": resp.text}

    if resp.status_code == 200 and result.get("access_token"):
        save_token(result)
        return result

    err_msg = "Authentication failed"
    if isinstance(result, dict) and result.get("errors"):
        err_msg = result["errors"][0].get("message", err_msg)
    raise RuntimeError(f"Upstox Auth Error ({resp.status_code}): {err_msg}")


def is_authenticated() -> bool:
    token_data = load_token()
    return bool(token_data.get("access_token"))


def get_user_profile() -> dict[str, Any] | None:
    import requests
    token_data = load_token()
    token = token_data.get("access_token")
    if not token:
        return None
    url = f"{UPSTOX_API_BASE}/user/profile"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("data")
    except Exception:
        pass
    return None


def _instruments_need_refresh(mapping: dict[str, Any], now_ms: int | None = None) -> bool:
    """Returns True when key near-month futures contracts in the index have expired."""
    if not isinstance(mapping, dict) or not mapping:
        return True
    cur_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    # Near-month index contracts expire at 15:30 IST (~10:00 UTC) on expiry day, while expiry_ms
    # from Upstox is stored at 23:59:59 IST. Treat anything strictly older than cur_ms as expired.
    checked = 0
    for key in ("NIFTY_FUT", "SENSEX_FUT", "RELIANCE_FUT"):
        entry = mapping.get(key)
        if isinstance(entry, dict) and entry.get("expiry"):
            checked += 1
            if int(entry["expiry"]) < cur_ms:
                return True
    return checked == 0


def _load_instruments_map() -> dict[str, Any]:
    """Loads cached Upstox instruments mapping, auto-rolling when near-month futures expire."""
    global _INSTRUMENTS_CACHE, _INSTRUMENTS_REFRESH_ATTEMPT_TS
    if _INSTRUMENTS_CACHE is not None and not _instruments_need_refresh(_INSTRUMENTS_CACHE):
        return _INSTRUMENTS_CACHE

    with _INSTRUMENTS_LOCK:
        if _INSTRUMENTS_CACHE is None and INSTRUMENTS_FILE.exists():
            try:
                _INSTRUMENTS_CACHE = json.loads(INSTRUMENTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                _INSTRUMENTS_CACHE = None

        needs_dl = _INSTRUMENTS_CACHE is None or _instruments_need_refresh(_INSTRUMENTS_CACHE)
        if needs_dl and (time.monotonic() - _INSTRUMENTS_REFRESH_ATTEMPT_TS) >= 3600.0:
            _INSTRUMENTS_REFRESH_ATTEMPT_TS = time.monotonic()
            try:
                from download_upstox_instruments import download_and_index
                fresh = download_and_index()
                if isinstance(fresh, dict) and fresh:
                    _INSTRUMENTS_CACHE = fresh
            except Exception as dl_err:
                print(f"[UPSTOX WARNING] Failed to refresh master instruments: {dl_err}")

        if _INSTRUMENTS_CACHE is None:
            _INSTRUMENTS_CACHE = {}
        return _INSTRUMENTS_CACHE


def refresh_instruments_if_expired() -> bool:
    """Ensures Upstox instruments mapping is loaded and near-month futures contracts are unexpired."""
    before = _INSTRUMENTS_CACHE
    loaded = _load_instruments_map()
    return bool(loaded) and loaded is not before


def get_instrument_key(symbol: str) -> str | None:
    """Dynamically resolves instrument key from Upstox official master catalog with zero hardcoding."""
    sym = symbol.upper().strip()
    instruments = _load_instruments_map()
    entry = instruments.get(sym)
    if entry and isinstance(entry, dict):
        return entry.get("instrument_key")
    return None


def get_futures_instrument_key(symbol: str) -> str | None:
    """Resolves active near-month futures instrument key (NSE_FO / BSE_FO) for index or F&O stock."""
    raw = symbol.upper().strip()
    if not raw:
        return None
    alias_map = {
        "^NSEI": "NIFTY",
        "NIFTY 50": "NIFTY",
        "NIFTY50": "NIFTY",
        "^NSEBANK": "BANKNIFTY",
        "NIFTY BANK": "BANKNIFTY",
        "^BSESN": "SENSEX",
        "BSE SENSEX": "SENSEX",
        "BSESENSEX": "SENSEX",
    }
    base = alias_map.get(raw, raw)
    if base.endswith(".NS") or base.endswith(".BO"):
        base = base[:-3]
    instruments = _load_instruments_map()
    for candidate in (f"{base}_FUT", f"{raw}_FUT", base if base.endswith("_FUT") else ""):
        if not candidate:
            continue
        entry = instruments.get(candidate)
        if entry and isinstance(entry, dict):
            return entry.get("instrument_key")
    return None


def parse_1m_candles(candles_1m: list[list[Any]]) -> list[dict[str, Any]]:
    """Converts raw Upstox 1-minute candle array to standard dicts in chronological order.
    Upstox candle format: [timestamp_iso, open, high, low, close, volume, oi]
    """
    if not candles_1m:
        return []
    chronological = list(reversed(candles_1m))
    res: list[dict[str, Any]] = []
    for c in chronological:
        try:
            ts_str = c[0]
            dt = datetime.fromisoformat(ts_str)
            res.append({
                "timestamp": int(dt.timestamp()),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "oi": float(c[6]) if len(c) > 6 and c[6] is not None else 0.0,
            })
        except Exception:
            continue
    return res


def aggregate_1m_to_5m(candles_1m: list[list[Any]]) -> list[dict[str, Any]]:
    """Converts Upstox 1-minute candle array to standard 5-minute candles.
    Upstox candle format: [timestamp_iso, open, high, low, close, volume, oi]
    """
    if not candles_1m:
        return []

    # Upstox returns newest candles first; reverse to chronological order
    chronological = list(reversed(candles_1m))
    buckets: dict[int, list[list[Any]]] = {}

    for c in chronological:
        # e.g., '2026-10-23T09:15:00+05:30'
        ts_str = c[0]
        dt = datetime.fromisoformat(ts_str)
        # Bucket by 5-minute boundary
        minute_offset = (dt.minute // 5) * 5
        bucket_dt = dt.replace(minute=minute_offset, second=0, microsecond=0)
        bucket_epoch = int(bucket_dt.timestamp())
        buckets.setdefault(bucket_epoch, []).append(c)

    res: list[dict[str, Any]] = []
    for bucket_epoch in sorted(buckets.keys()):
        group = buckets[bucket_epoch]
        o = float(group[0][1])
        h = max(float(x[2]) for x in group)
        l = min(float(x[3]) for x in group)
        c = float(group[-1][4])
        v = sum(float(x[5]) for x in group)
        candle: dict[str, Any] = {
            "timestamp": bucket_epoch,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        }
        last_raw = group[-1]
        if len(last_raw) > 6 and last_raw[6] is not None:
            oi_val = float(last_raw[6])
            if oi_val > 0:
                candle["oi"] = oi_val
        res.append(candle)
    return res


def validate_candles_integrity(
    candles_5m: list[dict[str, Any]],
    now_ist: datetime,
    allow_multi_day: bool = False
) -> tuple[bool, str]:
    """Validates:
    1. Minimum length (at least 2 candles).
    2. Spacing: strictly monotonic timestamps in intervals of 300 seconds.
    3. OHLC integrity: high >= low, high >= open, high >= close, low <= open, low <= close, volume >= 0.
    4. Session alignment:
       - Mon-Fri 09:20-15:35 IST:
         Latest candle date must match today's date (now_ist.date()).
         Latest candle timestamp must not lag by more than 20 minutes.
       - Outside market hours:
         All session candles must belong to the same session date (unless allow_multi_day=True).
    Returns (is_valid, reason).
    """
    if not candles_5m or len(candles_5m) < 2:
        return False, "insufficient_candle_count"

    prev_ts = None
    for i, c in enumerate(candles_5m):
        ts = c.get("timestamp")
        o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
        v = c.get("volume", 0)

        if ts is None or None in (o, h, l, cl):
            return False, f"null_field_at_index_{i}"

        # Monotonic timestamp check
        if prev_ts is not None and ts <= prev_ts:
            return False, f"non_monotonic_timestamp_at_index_{i}"
        prev_ts = ts

        # OHLC logic check
        if h < l or h < o or h < cl or l > o or l > cl:
            return False, f"invalid_ohlc_spread_at_index_{i}"
        if v < 0:
            return False, f"negative_volume_at_index_{i}"

    # Session date & freshness check
    current_minutes = now_ist.hour * 60 + now_ist.minute
    is_live_session = now_ist.weekday() < 5 and (9 * 60 + 15) <= current_minutes <= (15 * 60 + 35)

    latest_c = candles_5m[-1]
    candle_dt = datetime.fromtimestamp(latest_c["timestamp"], tz=INDIA_TZ)
    is_today_candle = (candle_dt.date() == now_ist.date())

    # For full_range/multi-day charts (allow_multi_day=True), allow historical 5-day candles
    # For single intraday session, only enforce lag check if today is actively trading with today's candles
    if not allow_multi_day:
        if is_live_session and is_today_candle:
            age_minutes = (now_ist - candle_dt).total_seconds() / 60.0
            if age_minutes > 25:
                return False, f"excessive_lag_minutes_{round(age_minutes, 1)}"
        else:
            # Verify consistent intraday session date
            first_c = candles_5m[0]
            first_dt = datetime.fromtimestamp(first_c["timestamp"], tz=INDIA_TZ)
            if first_dt.date() != candle_dt.date():
                # Spanning multiple days in a single intraday session
                return False, "multi_date_spanning_intraday_candles"

    return True, "valid"


def separate_completed_and_forming(
    candles_5m: list[dict[str, Any]],
    now_ist: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Separates closed/completed candles from the currently forming in-progress candle.
    A 5-minute candle starting at timestamp T is completed when current clock >= T + 300.
    Signals (breakout, cascade, trap detection) must ONLY evaluate completed candles.
    """
    if not candles_5m:
        return [], None

    now_epoch = int(now_ist.timestamp())
    completed: list[dict[str, Any]] = []
    forming: dict[str, Any] | None = None

    current_minutes = now_ist.hour * 60 + now_ist.minute
    is_live_session = now_ist.weekday() < 5 and (9 * 60 + 15) <= current_minutes <= (15 * 60 + 35)

    latest_c = candles_5m[-1]
    latest_dt = datetime.fromtimestamp(latest_c["timestamp"], tz=INDIA_TZ)
    is_today_candle = (latest_dt.date() == now_ist.date())

    if not is_live_session or not is_today_candle:
        # Outside live market hours or if today is a holiday/weekend (candles are from prior session),
        # all candles from the last session are closed and completed
        return list(candles_5m), None

    for c in candles_5m:
        if now_epoch >= (c["timestamp"] + 300):
            completed.append(c)
        else:
            forming = c

    return completed, forming


def _upstox_request_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": _CHROME_UA,
    }
    token = load_token().get("access_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_upstox_1m_raw(
    inst_key: str,
    now_ist: datetime,
    full_range: bool = False,
) -> tuple[list[list[Any]], str | None]:
    """Fetches raw 1-minute Upstox candles (intraday + historical backfill when needed)."""
    encoded_key = urllib.parse.quote(inst_key)
    headers = _upstox_request_headers()
    raw_candles_1m: list[list[Any]] = []
    error_reason: str | None = None

    url = f"{UPSTOX_API_BASE}/historical-candle/intraday/{encoded_key}/1minute"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                raw_candles_1m = data.get("data", {}).get("candles", [])
            else:
                error_reason = f"Upstox request failed/unauthorized (HTTP {resp.status})"
    except urllib.error.HTTPError as http_err:
        error_reason = f"Upstox request failed/unauthorized (HTTP {http_err.code})"
    except Exception as exc:
        error_reason = f"Upstox request failed/unauthorized ({type(exc).__name__})"

    hist_candles_1m: list[list[Any]] = []
    if full_range or not raw_candles_1m:
        to_date = now_ist.strftime("%Y-%m-%d")
        from_date = (now_ist - timedelta(days=14)).strftime("%Y-%m-%d")
        hist_url = f"{UPSTOX_API_BASE}/historical-candle/{encoded_key}/1minute/{to_date}/{from_date}"
        hist_req = urllib.request.Request(hist_url, headers=headers)
        try:
            with urllib.request.urlopen(hist_req, timeout=8) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw = data.get("data", {}).get("candles", [])
                    if raw:
                        if full_range:
                            hist_candles_1m = raw
                        else:
                            today_prefix = now_ist.strftime("%Y-%m-%d")
                            current_minutes = now_ist.hour * 60 + now_ist.minute
                            is_market_hours = now_ist.weekday() < 5 and (9 * 60 + 15) <= current_minutes <= (15 * 60 + 35)
                            if is_market_hours:
                                hist_candles_1m = [c for c in raw if c[0].startswith(today_prefix)]
                            else:
                                latest_date_prefix = raw[0][0][:10]
                                hist_candles_1m = [c for c in raw if c[0].startswith(latest_date_prefix)]
                        error_reason = None
                else:
                    error_reason = f"Upstox request failed/unauthorized (HTTP {resp.status})"
        except urllib.error.HTTPError as http_err:
            error_reason = f"Upstox request failed/unauthorized (HTTP {http_err.code})"
        except Exception as exc:
            error_reason = f"Upstox request failed/unauthorized ({type(exc).__name__})"

    if full_range and hist_candles_1m:
        seen_ts = set()
        merged = []
        for c in list(raw_candles_1m) + list(hist_candles_1m):
            if c[0] not in seen_ts:
                seen_ts.add(c[0])
                merged.append(c)
        raw_candles_1m = merged
    elif not raw_candles_1m and hist_candles_1m:
        raw_candles_1m = hist_candles_1m

    return raw_candles_1m, error_reason


def merge_futures_into_candles(
    spot_candles: list[dict[str, Any]],
    fut_candles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Overlays futures volume (when spot volume is zero) and real OI by exact 5m timestamp.
    Never modifies spot OHLC prices.
    """
    fut_by_ts = {int(fc["timestamp"]): fc for fc in (fut_candles or []) if fc.get("timestamp") is not None}
    matched = 0
    real_oi_bars = 0
    used_fut_volume = False

    for sc in spot_candles or []:
        ts = sc.get("timestamp")
        fc = fut_by_ts.get(int(ts)) if ts is not None else None
        if fc is None:
            if not (sc.get("oi") and sc.get("oi_is_real")):
                sc["oi"] = None
                sc["oi_is_real"] = False
            sc.setdefault("volume_source", "spot")
            continue

        matched += 1
        fut_vol = float(fc.get("volume") or 0.0)
        fut_oi = fc.get("oi")
        spot_vol = float(sc.get("volume") or 0.0)
        sc["futures_volume"] = fut_vol
        # When futures bar exists for this timestamp, trust real futures volume (even if 0 contracts traded)
        if spot_vol <= 0:
            sc["volume"] = fut_vol
            sc["volume_source"] = "futures"
            if fut_vol > 0:
                used_fut_volume = True
        else:
            sc.setdefault("volume_source", "spot")

        if fut_oi is not None and float(fut_oi) > 0:
            sc["oi"] = float(fut_oi)
            sc["oi_is_real"] = True
            real_oi_bars += 1
        else:
            sc["oi"] = None
            sc["oi_is_real"] = False

    return {
        "futures_matched": matched,
        "futures_oi_available": real_oi_bars > 0,
        "volume_source": "futures" if used_fut_volume else "spot",
    }


def fetch_futures_candles(
    symbol: str,
    full_range: bool = False,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetches both 5m and 1m futures candles (volume + OI) with a 60-second in-memory cache."""
    fut_key = get_futures_instrument_key(symbol)
    if not fut_key:
        return None, [], []

    cache_key = (fut_key, bool(full_range))
    now_mono = time.monotonic()
    cached = _FUTURES_CACHE.get(cache_key)
    if cached and len(cached) == 3 and (now_mono - cached[0]) < _FUTURES_CACHE_TTL_SEC:
        return fut_key, cached[1], cached[2]

    now_ist = datetime.now(INDIA_TZ)
    raw_fut, _ = _fetch_upstox_1m_raw(fut_key, now_ist, full_range=full_range)
    if not raw_fut:
        return fut_key, [], []

    fut_5m = aggregate_1m_to_5m(raw_fut)
    fut_1m = parse_1m_candles(raw_fut)
    if fut_5m:
        _FUTURES_CACHE[cache_key] = (now_mono, fut_5m, fut_1m)  # type: ignore[assignment]
    return fut_key, fut_5m, fut_1m


def fetch_5m_futures_overlay(
    symbol: str,
    full_range: bool = False,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Fetches 5m futures candles (volume + OI) with a 60-second in-memory cache."""
    fut_key, fut_5m, _ = fetch_futures_candles(symbol, full_range=full_range)
    return fut_key, fut_5m


def extract_oi_row_from_futures_candles(
    symbol: str,
    fut_5m: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Derives an NSE-compatible OI spurt summary row from multi-day Upstox futures candles."""
    if not fut_5m:
        return None
    by_day: dict[str, list[dict[str, Any]]] = {}
    for c in fut_5m:
        ts = c.get("timestamp")
        if ts is None:
            continue
        day_str = datetime.fromtimestamp(int(ts), tz=INDIA_TZ).date().isoformat()
        by_day.setdefault(day_str, []).append(c)

    if not by_day:
        return None
    days = sorted(by_day)
    latest_bars = by_day[days[-1]]

    def _last_positive_oi(bars: list[dict[str, Any]]) -> float | None:
        for b in reversed(bars):
            val = b.get("oi")
            if val is not None and float(val) > 0:
                return float(val)
        return None

    oi_now = _last_positive_oi(latest_bars)
    if oi_now is None:
        return None

    oi_prev = None
    for d in reversed(days[:-1]):
        oi_prev = _last_positive_oi(by_day[d])
        if oi_prev is not None:
            break

    oi_change = (oi_now - oi_prev) if oi_prev is not None else None
    oi_change_percent = ((oi_change / oi_prev) * 100.0) if (oi_prev and oi_change is not None) else None
    volume_contracts = sum(float(b.get("volume") or 0.0) for b in latest_bars)
    # calc_stock_activity expects futures_value in INR Lakhs.
    futures_value_lakhs = sum(
        float(b.get("volume") or 0.0) * float(b.get("close") or 0.0)
        for b in latest_bars
    ) / 100_000.0

    return {
        "symbol": symbol.upper().strip(),
        "oi_now": oi_now,
        "oi_prev": oi_prev,
        "oi_change": oi_change,
        "oi_change_percent": round(oi_change_percent, 2) if oi_change_percent is not None else None,
        "volume_contracts": volume_contracts,
        "futures_value": round(futures_value_lakhs, 2),
        "source": "upstox_futures",
    }


def fetch_futures_oi_summary(symbol: str) -> dict[str, Any] | None:
    """Lightweight near-month futures OI summary using 30m historical + today's intraday bars (~4-25 KB, no 14d 1m cache)."""
    fut_key = get_futures_instrument_key(symbol)
    if not fut_key:
        return None
    now_mono = time.monotonic()
    cached = _FUTURES_OI_SUMMARY_CACHE.get(fut_key)
    if cached and (now_mono - cached[0]) < _FUTURES_CACHE_TTL_SEC:
        return dict(cached[1]) if cached[1] is not None else None

    now_ist = datetime.now(INDIA_TZ)
    encoded_key = urllib.parse.quote(fut_key)
    headers = _upstox_request_headers()
    to_date = now_ist.strftime("%Y-%m-%d")
    from_date = (now_ist - timedelta(days=7)).strftime("%Y-%m-%d")

    raw_hist: list[list[Any]] = []
    try:
        hist_url = f"{UPSTOX_API_BASE}/historical-candle/{encoded_key}/30minute/{to_date}/{from_date}"
        with urllib.request.urlopen(urllib.request.Request(hist_url, headers=headers), timeout=6) as resp:
            if resp.status == 200:
                raw_hist = json.loads(resp.read().decode("utf-8")).get("data", {}).get("candles", [])
    except Exception:
        raw_hist = []

    raw_intra: list[list[Any]] = []
    if now_ist.weekday() < 5 and (now_ist.hour * 60 + now_ist.minute) >= (9 * 60 + 15):
        try:
            intra_url = f"{UPSTOX_API_BASE}/historical-candle/intraday/{encoded_key}/1minute"
            with urllib.request.urlopen(urllib.request.Request(intra_url, headers=headers), timeout=5) as resp:
                if resp.status == 200:
                    raw_intra = json.loads(resp.read().decode("utf-8")).get("data", {}).get("candles", [])
        except Exception:
            raw_intra = []

    by_day: dict[str, list[list[Any]]] = {}
    for row in raw_hist:
        try:
            day_str = str(row[0])[:10]
            by_day.setdefault(day_str, []).append(row)
        except Exception:
            continue
    if raw_intra:
        today_prefix = str(raw_intra[0][0])[:10]
        by_day[today_prefix] = list(raw_intra)

    if not by_day:
        _FUTURES_OI_SUMMARY_CACHE[fut_key] = (now_mono, None)
        return None

    days = sorted(by_day)
    latest_rows = by_day[days[-1]]  # Upstox raw order is newest-first

    def _newest_positive_oi(rows_desc: list[list[Any]]) -> float | None:
        # Handle both newest-first raw lists safely by ordering by timestamp descending
        for r in sorted(rows_desc, key=lambda x: str(x[0]), reverse=True):
            if len(r) > 6 and r[6] is not None and float(r[6]) > 0:
                return float(r[6])
        return None

    oi_now = _newest_positive_oi(latest_rows)
    if oi_now is None:
        _FUTURES_OI_SUMMARY_CACHE[fut_key] = (now_mono, None)
        return None

    oi_prev = None
    for d in reversed(days[:-1]):
        oi_prev = _newest_positive_oi(by_day[d])
        if oi_prev is not None:
            break

    oi_change = (oi_now - oi_prev) if oi_prev is not None else None
    oi_change_percent = ((oi_change / oi_prev) * 100.0) if (oi_prev and oi_change is not None) else None
    volume_contracts = sum(float(r[5] or 0.0) for r in latest_rows)
    futures_value_lakhs = sum(float(r[5] or 0.0) * float(r[4] or 0.0) for r in latest_rows) / 100_000.0

    summary = {
        "symbol": symbol.upper().strip(),
        "oi_now": oi_now,
        "oi_prev": oi_prev,
        "oi_change": oi_change,
        "oi_change_percent": round(oi_change_percent, 2) if oi_change_percent is not None else None,
        "volume_contracts": volume_contracts,
        "futures_value": round(futures_value_lakhs, 2),
        "source": "upstox_futures_30m",
    }
    _FUTURES_OI_SUMMARY_CACHE[fut_key] = (now_mono, summary)
    return dict(summary)


def fetch_5m_candles_with_telemetry(
    symbol: str,
    full_range: bool = False,
    include_futures: bool | None = None,
) -> dict[str, Any]:
    """Fetches high-speed intraday 5-minute candles via Upstox API v2.
    When full_range=True, combines the past 5 trading days with today's live session for full chart context.
    Measures observed latency, validates session date, monotonic spacing, and OHLC integrity,
    and isolates the forming in-progress candle from completed candles.
    """
    t0 = time.monotonic()
    now_ist = datetime.now(INDIA_TZ)

    inst_key = get_instrument_key(symbol)
    if not inst_key:
        return {
            "ok": False,
            "symbol": symbol,
            "provider": "Upstox",
            "completed_candles": [],
            "forming_candle": None,
            "all_candles": [],
            "latest_completed_candle_ist": None,
            "latest_completed_candle_epoch": None,
            "observed_latency_ms": int((time.monotonic() - t0) * 1000),
            "fallback_used": False,
            "is_stale": False,
            "error": "Instrument key not found in NSE master index",
        }

    raw_candles_1m, error_reason = _fetch_upstox_1m_raw(inst_key, now_ist, full_range=full_range)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if not raw_candles_1m:
        return {
            "ok": False,
            "symbol": symbol,
            "provider": "Upstox",
            "completed_candles": [],
            "forming_candle": None,
            "all_candles": [],
            "candles_1m": [],
            "latest_completed_candle_ist": None,
            "latest_completed_candle_epoch": None,
            "observed_latency_ms": latency_ms,
            "fallback_used": False,
            "is_stale": False,
            "error": error_reason or "Empty candle response from Upstox API",
        }

    # Parse 1-minute candles for sub-minute institutional volume shock radar
    parsed_1m = parse_1m_candles(raw_candles_1m)

    # Aggregate 1-minute to 5-minute candles
    all_5m = aggregate_1m_to_5m(raw_candles_1m)

    # Validate integrity
    is_valid, reason = validate_candles_integrity(all_5m, now_ist, allow_multi_day=full_range)
    if not is_valid:
        return {
            "ok": False,
            "symbol": symbol,
            "provider": "Upstox",
            "completed_candles": [],
            "forming_candle": None,
            "all_candles": all_5m,
            "candles_1m": parsed_1m,
            "latest_completed_candle_ist": None,
            "latest_completed_candle_epoch": None,
            "observed_latency_ms": latency_ms,
            "fallback_used": False,
            "is_stale": False,
            "error": f"Upstox candle validation failed: {reason}",
        }

    total_spot_vol = sum(float(c.get("volume") or 0.0) for c in all_5m)
    should_enrich_futures = (include_futures is True) or (include_futures is None and total_spot_vol <= 0)
    fut_meta = {"futures_matched": 0, "futures_oi_available": False, "volume_source": "spot"}
    fut_key = None
    if should_enrich_futures:
        fut_key, fut_5m, fut_1m = fetch_futures_candles(symbol, full_range=full_range)
        if fut_5m:
            fut_meta = merge_futures_into_candles(all_5m, fut_5m)
        if fut_1m and parsed_1m:
            merge_futures_into_candles(parsed_1m, fut_1m)

    # Exclude the forming candle from completed candles
    completed_candles, forming_candle = separate_completed_and_forming(all_5m, now_ist)

    latest_ist = None
    latest_epoch = None
    if completed_candles:
        latest_epoch = completed_candles[-1]["timestamp"]
        latest_dt = datetime.fromtimestamp(latest_epoch, tz=INDIA_TZ)
        latest_ist = latest_dt.strftime("%I:%M %p")

    return {
        "ok": True,
        "symbol": symbol,
        "provider": "Upstox",
        "completed_candles": completed_candles,
        "forming_candle": forming_candle,
        "all_candles": all_5m,
        "candles_1m": parsed_1m,
        "latest_completed_candle_ist": latest_ist,
        "latest_completed_candle_epoch": latest_epoch,
        "observed_latency_ms": int((time.monotonic() - t0) * 1000),
        "fallback_used": False,
        "is_stale": False,
        "futures_instrument_key": fut_key,
        "volume_source": fut_meta["volume_source"],
        "oi_is_real": fut_meta["futures_oi_available"],
        "error": None,
    }


def fetch_5m_candles(
    symbol: str,
    full_range: bool = False,
    include_futures: bool | None = None,
) -> list[dict[str, Any]]:
    """Fetches high-speed intraday 5-minute candles via Upstox API.
    Returns only completed, validated candles (forming candle excluded).
    """
    res = fetch_5m_candles_with_telemetry(symbol, full_range=full_range, include_futures=include_futures)
    if res.get("ok"):
        return res.get("completed_candles", [])
    return []


def _validate_candle_freshness(candles_5m: list[dict[str, Any]], now_ist: datetime) -> bool:
    """Backward-compatible wrapper around validate_candles_integrity."""
    is_valid, _ = validate_candles_integrity(candles_5m, now_ist)
    return is_valid


def fetch_market_depth(symbol: str) -> dict[str, Any] | None:
    """Fetches Level-2 market depth and live order-book imbalance."""
    token_data = load_token()
    token = token_data.get("access_token")
    if not token:
        return None

    inst_key = get_instrument_key(symbol)
    if not inst_key:
        return None

    encoded_key = urllib.parse.quote(inst_key)
    url = f"{UPSTOX_API_BASE}/market-quote/quotes?instrument_key={encoded_key}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ManojLabs-Nifty50Scanner/2.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            quote_map = data.get("data", {})
            if not quote_map:
                return None
            
            # Key might have colon e.g. "NSE_EQ:NAM-INDIA"
            quote = next(iter(quote_map.values()), {})
            total_buy = float(quote.get("total_buy_quantity") or 0.0)
            total_sell = float(quote.get("total_sell_quantity") or 0.0)
            total_depth = total_buy + total_sell
            
            pressure_pct = (total_buy / total_depth * 100.0) if total_depth > 0 else 50.0
            
            return {
                "total_buy_quantity": total_buy,
                "total_sell_quantity": total_sell,
                "buying_pressure_pct": round(pressure_pct, 1),
                "last_price": float(quote.get("last_price") or 0.0),
                "volume": float(quote.get("volume") or 0.0),
                "oi": float(quote.get("oi") or 0.0),
            }
    except Exception:
        return None
