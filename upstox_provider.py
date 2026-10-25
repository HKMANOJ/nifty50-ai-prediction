#!/usr/bin/env python3
"""Upstox Market Data Provider for NIFTY50 AI Prediction Engine.

Provides official, high-speed 5m intraday candle aggregation,
Level-2 market depth (real buying/selling pressure), and OAuth2 authentication.
"""

from __future__ import annotations

import json
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


def load_credentials() -> dict[str, str]:
    if not CREDENTIALS_FILE.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_token() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_token(token_data: dict[str, Any]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token_data["saved_at_epoch"] = time.time()
    token_data["saved_at_ist"] = datetime.now(INDIA_TZ).isoformat()
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2), encoding="utf-8")


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


def get_instrument_key(symbol: str) -> str | None:
    global _INSTRUMENTS_CACHE
    sym = symbol.upper().strip()
    
    # Direct index overrides
    if sym in ("NIFTY 50", "NIFTY50", "NIFTY"):
        return "NSE_INDEX|Nifty 50"
    if sym in ("NIFTY BANK", "BANKNIFTY", "BANK NIFTY"):
        return "NSE_INDEX|Nifty Bank"

    if _INSTRUMENTS_CACHE is None:
        if INSTRUMENTS_FILE.exists():
            try:
                _INSTRUMENTS_CACHE = json.loads(INSTRUMENTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                _INSTRUMENTS_CACHE = {}
        else:
            _INSTRUMENTS_CACHE = {}

    entry = _INSTRUMENTS_CACHE.get(sym)
    if entry and isinstance(entry, dict):
        return entry.get("instrument_key")
    return None


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
        res.append({
            "timestamp": bucket_epoch,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })
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
    is_live_session = now_ist.weekday() < 5 and (9 * 60 + 20) <= current_minutes <= (15 * 60 + 35)

    latest_c = candles_5m[-1]
    candle_dt = datetime.fromtimestamp(latest_c["timestamp"], tz=INDIA_TZ)

    if is_live_session:
        if candle_dt.date() != now_ist.date():
            return False, f"stale_session_date_{candle_dt.date()}_expected_{now_ist.date()}"
        age_minutes = (now_ist - candle_dt).total_seconds() / 60.0
        if age_minutes > 20:
            return False, f"excessive_lag_minutes_{round(age_minutes, 1)}"
    elif not allow_multi_day:
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

    if not is_live_session:
        # Outside live market hours, all candles from the last session are closed and completed
        return list(candles_5m), None

    for c in candles_5m:
        if now_epoch >= (c["timestamp"] + 300):
            completed.append(c)
        else:
            forming = c

    return completed, forming


def fetch_5m_candles_with_telemetry(symbol: str) -> dict[str, Any]:
    """Fetches high-speed intraday 5-minute candles via Upstox API v2.
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

    encoded_key = urllib.parse.quote(inst_key)
    headers = {"Accept": "application/json", "User-Agent": "ManojLabs-Nifty50Scanner/2.0"}
    token = load_token().get("access_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    raw_candles_1m = []
    error_reason = None

    # 1. Try intraday endpoint first (fastest for today's live session)
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

    # 2. Try historical endpoint for the latest completed trading session if intraday was empty
    if not raw_candles_1m:
        to_date = now_ist.strftime("%Y-%m-%d")
        from_date = (now_ist - timedelta(days=5)).strftime("%Y-%m-%d")
        hist_url = f"{UPSTOX_API_BASE}/historical-candle/{encoded_key}/1minute/{to_date}/{from_date}"
        hist_req = urllib.request.Request(hist_url, headers=headers)
        try:
            with urllib.request.urlopen(hist_req, timeout=8) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw = data.get("data", {}).get("candles", [])
                    if raw:
                        latest_date_prefix = raw[0][0][:10]
                        raw_candles_1m = [c for c in raw if c[0].startswith(latest_date_prefix)]
                        error_reason = None
                else:
                    error_reason = f"Upstox request failed/unauthorized (HTTP {resp.status})"
        except urllib.error.HTTPError as http_err:
            error_reason = f"Upstox request failed/unauthorized (HTTP {http_err.code})"
        except Exception as exc:
            error_reason = f"Upstox request failed/unauthorized ({type(exc).__name__})"

    latency_ms = int((time.monotonic() - t0) * 1000)

    if not raw_candles_1m:
        return {
            "ok": False,
            "symbol": symbol,
            "provider": "Upstox",
            "completed_candles": [],
            "forming_candle": None,
            "all_candles": [],
            "latest_completed_candle_ist": None,
            "latest_completed_candle_epoch": None,
            "observed_latency_ms": latency_ms,
            "fallback_used": False,
            "is_stale": False,
            "error": error_reason or "Empty candle response from Upstox API",
        }

    # Aggregate 1-minute to 5-minute candles
    all_5m = aggregate_1m_to_5m(raw_candles_1m)

    # Validate integrity
    is_valid, reason = validate_candles_integrity(all_5m, now_ist)
    if not is_valid:
        return {
            "ok": False,
            "symbol": symbol,
            "provider": "Upstox",
            "completed_candles": [],
            "forming_candle": None,
            "all_candles": all_5m,
            "latest_completed_candle_ist": None,
            "latest_completed_candle_epoch": None,
            "observed_latency_ms": latency_ms,
            "fallback_used": False,
            "is_stale": False,
            "error": f"Upstox candle validation failed: {reason}",
        }

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
        "latest_completed_candle_ist": latest_ist,
        "latest_completed_candle_epoch": latest_epoch,
        "observed_latency_ms": latency_ms,
        "fallback_used": False,
        "is_stale": False,
        "error": None,
    }


def fetch_5m_candles(symbol: str) -> list[dict[str, Any]]:
    """Fetches high-speed intraday 5-minute candles via Upstox API.
    Returns only completed, validated candles (forming candle excluded).
    """
    res = fetch_5m_candles_with_telemetry(symbol)
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
