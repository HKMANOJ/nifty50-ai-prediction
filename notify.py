#!/usr/bin/env python3
"""
ManojLabs AI Stock Hunter - Telegram Notification Dispatcher
Strictly alerts on NIFTY and SENSEX Demand Fresh setups.
Suppresses all individual stocks, Bear OBs, and extra messages.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("telegram_notify")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("[%(asctime)s] [Telegram] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(ch)

INDIA_TZ = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
SENT_STORE_PATH = ROOT / "inputs" / "telegram_sent_alerts.json"
_DISPATCH_LOCK = threading.Lock()


def load_env() -> None:
    """Load environment variables from project .env file if not already set."""
    candidates = [
        ROOT / ".env",
        Path.home() / "Downloads" / "nifty50-ai-prediction" / ".env",
    ]
    for env_file in candidates:
        if env_file.is_file():
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception as exc:
                logger.debug("Failed reading %s: %s", env_file, exc)


load_env()


DEFAULT_BOT_TOKEN = "8719411311:AAGyHgXwUBAn7AL5VkIFfbgp2QpQIWQU6DE"
DEFAULT_CHAT_ID = "6093954934"


def get_credentials() -> tuple[str, str, bool]:
    load_env()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN).strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or DEFAULT_CHAT_ID).strip()
    enabled = os.getenv("TELEGRAM_ALERTS_ENABLED", "1").strip().lower() in ("1", "true", "yes")
    return token, chat_id, enabled


def send_telegram_text(text: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Send text via Telegram Bot API."""
    token, chat_id, enabled = get_credentials()
    if not enabled:
        logger.info("Telegram notifications are currently disabled (TELEGRAM_ALERTS_ENABLED=0)")
        return False, "disabled"
    if not token or not chat_id:
        msg = "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured"
        logger.warning(msg)
        return False, msg

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }
    encoded = urllib.parse.urlencode(payload).encode("utf-8")

    req = urllib.request.Request(url, data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if data.get("ok"):
                return True, "ok"
            return False, data.get("description", "Unknown Telegram error")
    except urllib.error.HTTPError as he:
        body = he.read().decode("utf-8", errors="ignore")
        logger.error("Telegram HTTP error %s: %s | %s", he.code, he.reason, body)
        return False, f"HTTP {he.code}: {body}"
    except Exception as exc:
        logger.error("Telegram network error: %s", exc)
        return False, str(exc)


def _load_sent_keys() -> set[str]:
    """Load already sent alert keys for the current trading day."""
    if not SENT_STORE_PATH.is_file():
        return set()
    try:
        data = json.loads(SENT_STORE_PATH.read_text(encoding="utf-8"))
        today = datetime.now(INDIA_TZ).date().isoformat()
        if data.get("session_date") == today:
            return set(data.get("sent_keys", []))
    except Exception:
        pass
    return set()


def _save_sent_key(key: str) -> None:
    """Save an alert key so it is never re-sent during this session."""
    with _DISPATCH_LOCK:
        try:
            today = datetime.now(INDIA_TZ).date().isoformat()
            existing = set()
            if SENT_STORE_PATH.is_file():
                try:
                    data = json.loads(SENT_STORE_PATH.read_text(encoding="utf-8"))
                    if data.get("session_date") == today:
                        existing = set(data.get("sent_keys", []))
                except Exception:
                    pass
            existing.add(key)
            SENT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SENT_STORE_PATH.write_text(
                json.dumps({"session_date": today, "sent_keys": sorted(list(existing))}, indent=2),
                encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Failed to persist sent key %s: %s", key, exc)


def _format_time_str(time_val: Any) -> str:
    """Formats time into clean string without leading zeros e.g. '9:40 AM'."""
    if not time_val:
        return datetime.now(INDIA_TZ).strftime("%I:%M %p").lstrip("0")
    if isinstance(time_val, (int, float)):
        dt = datetime.fromtimestamp(time_val, tz=INDIA_TZ)
        return dt.strftime("%I:%M %p").lstrip("0")
    s = str(time_val).strip()
    if s.startswith("0") and len(s) > 1 and s[1].isdigit():
        s = s[1:]
    return s


def build_alert_message(item: dict, sector: str = "", is_pe: bool = False, test: bool = False) -> str:
    """Format single clean line alert:
    🟢 Nifty Demand fresh at 9:40 AM
    or
    🟢 Sensex Demand fresh at 9:40 AM
    """
    raw_sym = str(item.get("symbol", "NIFTY")).upper()
    display_name = "Nifty" if "NIFTY" in raw_sym else ("Sensex" if "SENSEX" in raw_sym else raw_sym.title())
    ob_time = item.get("demand_fresh_time") or item.get("time") or item.get("formation_time_str")
    clean_time = _format_time_str(ob_time)

    # Strictly 1 single clean line requested by user:
    # "Nifty Demand fresh at 9:40 like that no exctra unwanted message delete all"
    return f"🟢 {display_name} Demand fresh at {clean_time}"


def send_fresh_ob_alert(item: dict, sector: str = "", is_pe: bool = False, force: bool = False, test: bool = False) -> bool:
    """Send a single Demand Fresh alert strictly for NIFTY or SENSEX with session deduplication."""
    if not isinstance(item, dict):
        return False

    raw_sym = str(item.get("symbol", "")).upper()

    # STRICT FILTER 1: Exclusively NIFTY and SENSEX (strictly block all individual stocks)
    if not any(k in raw_sym for k in ("NIFTY", "SENSEX")):
        logger.info("Ignoring alert for %s (Strictly NIFTY & SENSEX only)", raw_sym)
        return False

    # STRICT FILTER 2: Demand Fresh only (no Bear OB, no PE, no tested zones)
    if is_pe or item.get("is_pe") or item.get("side") in ("PE", "bearish") or item.get("type") == "BEARISH":
        logger.info("Ignoring Bear OB / PE alert for %s (Demand Fresh only)", raw_sym)
        return False

    # STRICT FILTER 3: Must not be tested or mitigated
    if item.get("is_demand_tested") or item.get("status") in ("Tested", "Mitigated") or item.get("mitigated"):
        logger.info("Ignoring tested/mitigated order block for %s", raw_sym)
        return False

    display_name = "Nifty" if "NIFTY" in raw_sym else "Sensex"
    ob_time = item.get("demand_fresh_time") or item.get("time") or item.get("formation_time_str")
    clean_time = _format_time_str(ob_time)

    key = f"{display_name}_DEMAND_FRESH_{clean_time}"

    if not force and not test:
        sent = _load_sent_keys()
        if key in sent:
            logger.info("Alert already sent to Telegram previously: %s (skipped)", key)
            return True

    text = build_alert_message(item, sector=sector, is_pe=False, test=test)
    ok, reason = send_telegram_text(text)
    if ok:
        logger.info("✓ Dispatched Telegram alert: %s", text)
        if not test:
            _save_sent_key(key)
        return True
    else:
        logger.warning("✗ Failed sending Telegram alert for %s: %s", display_name, reason)
        return False


def dispatch_new_fresh_alerts(payload: dict | None = None) -> list[str]:
    """Scan NIFTY and SENSEX for newly formed Demand Fresh setups and notify Telegram.
    Strictly ONLY NIFTY and SENSEX Demand Fresh alerts are dispatched.
    All individual stocks, Bear OBs, and tested setups are completely suppressed.
    """
    sent_this_cycle: list[str] = []

    try:
        from stock_suggestion_index import fetch_5m_candles_pipeline, INDIA_TZ
        from smc_volume_profile_engine import detect_order_blocks_and_structure
    except Exception as exc:
        logger.warning("Could not import pipeline/SMC engine in dispatch: %s", exc)
        return []

    targets = [
        ("Nifty", "NIFTY50"),
        ("Sensex", "SENSEX"),
    ]

    for sym_display, sym_code in targets:
        try:
            res = fetch_5m_candles_pipeline(sym_code, full_range=True)
            candles = res.get("completed_candles") or []
            if not candles:
                continue

            all_days = [datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() for c in candles]
            last_day = max(all_days) if all_days else None
            today_completed = [
                c for c in candles
                if datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() == last_day
            ]
            if not today_completed:
                continue

            ob_res = detect_order_blocks_and_structure(today_completed)
            raw_obs = ob_res.get("order_blocks", [])

            # Strictly Demand Fresh only (Bullish, Fresh status, not mitigated, not tested)
            demand_fresh = [
                ob for ob in raw_obs
                if (ob.get("type") == "BULLISH" or ob.get("label") == "DEMAND")
                and ob.get("status") == "Fresh"
                and not ob.get("mitigated")
                and not ob.get("tested")
                and datetime.fromtimestamp(ob.get("time", 0), tz=INDIA_TZ).date() == last_day
            ]

            for ob in demand_fresh:
                f_time = ob.get("formation_time") or ob.get("time")
                clean_time = _format_time_str(f_time)
                key = f"{sym_display}_DEMAND_FRESH_{clean_time}"
                if key in _load_sent_keys():
                    continue

                item = {
                    "symbol": sym_display,
                    "demand_fresh_time": clean_time,
                    "top": ob.get("top"),
                    "bottom": ob.get("bottom"),
                    "status": "Fresh",
                    "mitigated": False,
                }
                if send_fresh_ob_alert(item, is_pe=False):
                    sent_this_cycle.append(key)
        except Exception as exc:
            logger.error("Error evaluating index demand fresh for %s: %s", sym_display, exc)

    return sent_this_cycle


def dispatch_instant_demand_surge() -> list[str]:
    """Scan NIFTY and SENSEX 1-minute candles in real time for instant demand surges.
    Fires the exact minute the impulse candle breaks out of the base, giving an early entry.
    """
    sent_this_cycle: list[str] = []

    try:
        import upstox_provider
    except Exception as exc:
        logger.warning("Could not import upstox_provider: %s", exc)
        return []

    targets = [
        ("Sensex", "SENSEX", 38),  # Min 38 pts impulse for Sensex
        ("Nifty", "NIFTY", 14),    # Min 14 pts impulse for Nifty
    ]

    now_ist = datetime.now(INDIA_TZ)
    today = now_ist.date()

    for sym_display, sym_code, min_pts in targets:
        try:
            u_res = upstox_provider.fetch_5m_candles_with_telemetry(sym_code, full_range=False)
            c1m = u_res.get("candles_1m") or []
            if not c1m:
                continue

            today_c1m = [
                c for c in c1m
                if datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() == today
            ]
            if len(today_c1m) < 6:
                continue

            # Look at the latest completed 1-minute candle
            curr = today_c1m[-1]
            body = curr["close"] - curr["open"]
            candle_range = curr["high"] - curr["low"]

            # Must be a solid green candle meeting minimum points
            if body < min_pts:
                continue

            # Upper wick must be controlled (< 35% of total range)
            if candle_range > 0 and (curr["high"] - curr["close"]) / candle_range > 0.35:
                continue

            # Base of preceding 3 to 8 candles
            base_candles = today_c1m[-9:-1]
            if not base_candles:
                continue

            base_high = max(c["high"] for c in base_candles)

            # Must break out above base_high
            if curr["close"] > base_high:
                c_dt = datetime.fromtimestamp(curr["timestamp"], tz=INDIA_TZ)
                time_str = c_dt.strftime("%I:%M %p").lstrip("0")
                key = f"{sym_display}_1M_SURGE_{today.isoformat()}_{c_dt.strftime('%H%M')}"

                # Deduplication: Check if already sent or sent within last 12 minutes
                sent_keys = _load_sent_keys()
                if key in sent_keys:
                    continue

                curr_minutes = c_dt.hour * 60 + c_dt.minute
                recent_sent = False
                for k in sent_keys:
                    if k.startswith(f"{sym_display}_1M_SURGE_{today.isoformat()}_"):
                        try:
                            hhmm = k.split("_")[-1]
                            k_min = int(hhmm[:2]) * 60 + int(hhmm[2:])
                            if abs(curr_minutes - k_min) < 12:
                                recent_sent = True
                                break
                        except Exception:
                            pass
                if recent_sent:
                    continue

                msg = f"⚡ {sym_display} Demand Surge at {time_str} @ {curr['close']:,.0f} (Base: {base_high:,.0f})"
                ok, reason = send_telegram_text(msg)
                if ok:
                    logger.info("✓ Dispatched Instant Demand Surge: %s", msg)
                    _save_sent_key(key)
                    sent_this_cycle.append(key)
                else:
                    logger.warning("✗ Failed sending Telegram alert for %s: %s", sym_display, reason)
        except Exception as exc:
            logger.error("Error evaluating instant demand surge for %s: %s", sym_display, exc)

    return sent_this_cycle


if __name__ == "__main__":
    token, chat_id, enabled = get_credentials()
    print("=" * 60)
    print("ManojLabs Telegram Notification Tester (NIFTY & SENSEX Demand Fresh)")
    print(f"  Bot Token:   {'*' * 6 + token[-6:] if token else 'NOT CONFIGURED'}")
    print(f"  Chat ID:     {chat_id or 'NOT CONFIGURED'}")
    print(f"  Enabled:     {enabled}")
    print("=" * 60)

    if not token or not chat_id:
        print("Error: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        sys.exit(1)

    print("\nSending live test NIFTY Demand Fresh alert...")
    test_item = {
        "symbol": "Nifty",
        "demand_fresh_time": "9:40 AM",
    }
    ok = send_fresh_ob_alert(test_item, is_pe=False, test=True, force=True)
    if ok:
        print("✅ Demand Fresh test alert delivered successfully to Telegram!")
    else:
        print("❌ Delivery failed.")

    print("\nSending live test Instant 1-Minute Surge alert...")
    surge_msg = "⚡ Sensex Demand Surge at 1:50 PM @ 81,920 (Base: 81,875)"
    s_ok, s_reason = send_telegram_text(surge_msg)
    if s_ok:
        print("✅ Instant 1-Minute Surge test alert delivered successfully to Telegram!")
    else:
        print(f"❌ Surge delivery failed: {s_reason}")
