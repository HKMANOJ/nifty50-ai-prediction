#!/usr/bin/env python3
"""
ManojLabs AI Stock Hunter - Telegram Notification Dispatcher
Strictly alerts on NIFTY and SENSEX Demand Fresh setups & 1-Minute Demand Surges.
Suppresses all individual stocks, Bear OBs, and extra messages.
Guarantees strict once-and-only-once delivery via Neon PostgreSQL atomic reservation.
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
from typing import Any
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
_PG_TABLE_CHECKED = False


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


DEFAULT_BOT_TOKEN = ""
DEFAULT_CHAT_ID = ""


def get_credentials() -> tuple[str, str, bool]:
    load_env()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN).strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or DEFAULT_CHAT_ID).strip()
    enabled = os.getenv("TELEGRAM_ALERTS_ENABLED", "1").strip().lower() in ("1", "true", "yes")
    return token, chat_id, enabled


def is_production_sender() -> bool:
    """Checks whether this instance is authorized to dispatch real Telegram alerts.
    Prevents local laptop/desktop development processes from double-firing production alerts.
    """
    env_force = os.getenv("FORCE_LOCAL_TELEGRAM_ALERTS", "").strip().lower()
    if env_force in ("1", "true", "yes"):
        return True
    if env_force in ("0", "false", "no"):
        return False

    # Production cloud environments (Render, AWS, Heroku, etc.)
    if os.getenv("RENDER") or os.getenv("IS_PRODUCTION") or os.getenv("DEPLOYED_ENV") in ("prod", "production"):
        return True

    # Local development workstation (Mac/Windows/Linux non-cloud)
    return False


def _get_pg_conn():
    """Returns an active PostgreSQL connection if DATABASE_URL is configured."""
    global _PG_TABLE_CHECKED
    try:
        import db_adapter
        if db_adapter.DATABASE_URL:
            conn = db_adapter.get_db_connection()
            if not _PG_TABLE_CHECKED:
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_sent_alerts (
                            alert_key VARCHAR(120) PRIMARY KEY,
                            session_date DATE NOT NULL,
                            alert_text TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        CREATE INDEX IF NOT EXISTS idx_tg_alerts_date ON telegram_sent_alerts(session_date);
                        """
                    )
                    conn.commit()
                    cur.close()
                    _PG_TABLE_CHECKED = True
                except Exception as t_err:
                    logger.debug("Table check skipped: %s", t_err)
            return conn
    except Exception as exc:
        logger.debug("Postgres connection unavailable: %s", exc)
    return None


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


def _load_sent_keys_local(session_date: str) -> set[str]:
    """Load sent alert keys from local file store."""
    if not SENT_STORE_PATH.is_file():
        return set()
    try:
        data = json.loads(SENT_STORE_PATH.read_text(encoding="utf-8"))
        if data.get("session_date") == session_date:
            return set(data.get("sent_keys", []))
    except Exception:
        pass
    return set()


def _save_sent_key_local(key: str, session_date: str) -> None:
    """Save an alert key into local file store."""
    try:
        existing = _load_sent_keys_local(session_date)
        existing.add(key)
        SENT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENT_STORE_PATH.write_text(
            json.dumps({"session_date": session_date, "sent_keys": sorted(list(existing))}, indent=2),
            encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Failed to persist local sent key %s: %s", key, exc)


def _load_sent_keys() -> set[str]:
    """Load already sent alert keys for the current trading day.
    Checks Neon PostgreSQL first, falling back to local file.
    """
    today = datetime.now(INDIA_TZ).date().isoformat()
    conn = _get_pg_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT alert_key FROM telegram_sent_alerts WHERE session_date = %s", (today,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
            db_keys = {r[0] for r in rows}
            # Mirror to local file for offline fallback
            local_keys = _load_sent_keys_local(today)
            combined = db_keys | local_keys
            try:
                SENT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
                SENT_STORE_PATH.write_text(
                    json.dumps({"session_date": today, "sent_keys": sorted(list(combined))}, indent=2),
                    encoding="utf-8"
                )
            except Exception:
                pass
            return combined
        except Exception as exc:
            logger.debug("Failed querying Postgres for sent keys: %s", exc)
            try:
                conn.close()
            except Exception:
                pass
    return _load_sent_keys_local(today)


def reserve_alert_atomic(key: str, session_date: str, alert_text: str = "") -> bool:
    """Atomically attempts to reserve this alert key in Neon PostgreSQL.
    Returns True if this worker won the lock (first caller to insert).
    Returns False if already reserved by any worker (Postgres PRIMARY KEY constraint).
    Falls back gracefully to local file lock if Postgres is unreachable.
    """
    with _DISPATCH_LOCK:
        # 1. Try Neon Postgres atomic insert
        conn = _get_pg_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO telegram_sent_alerts (alert_key, session_date, alert_text, created_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (alert_key) DO NOTHING
                    RETURNING alert_key;
                    """,
                    (key, session_date, alert_text)
                )
                row = cur.fetchone()
                conn.commit()
                cur.close()
                conn.close()
                if row:
                    _save_sent_key_local(key, session_date)
                    return True
                else:
                    logger.info("Alert key already reserved in Postgres: %s (skipped)", key)
                    _save_sent_key_local(key, session_date)
                    return False
            except Exception as db_err:
                logger.warning("Postgres atomic reservation error: %s, falling back to local file", db_err)
                try:
                    conn.close()
                except Exception:
                    pass

        # 2. Fallback to local file store
        sent = _load_sent_keys_local(session_date)
        if key in sent:
            logger.info("Alert already sent locally: %s (skipped)", key)
            return False
        _save_sent_key_local(key, session_date)
        return True


def _save_sent_key(key: str) -> None:
    """Save an alert key so it is never re-sent during this session."""
    today = datetime.now(INDIA_TZ).date().isoformat()
    reserve_alert_atomic(key, today)


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
    🔴 Sensex Bear OB fresh at 11:35 AM
    """
    raw_sym = str(item.get("symbol", "NIFTY")).upper()
    display_name = "Nifty" if "NIFTY" in raw_sym else ("Sensex" if "SENSEX" in raw_sym else raw_sym.title())
    ob_time = item.get("demand_fresh_time") or item.get("time") or item.get("formation_time_str")
    clean_time = _format_time_str(ob_time)

    is_bear = (
        is_pe
        or item.get("is_pe")
        or item.get("side") in ("PE", "bearish")
        or item.get("type") in ("BEARISH", "-OB")
        or "BEAR" in str(item.get("label", "")).upper()
        or "SUPPLY" in str(item.get("label", "")).upper()
    )

    if is_bear:
        return f"🔴 {display_name} Bear OB fresh at {clean_time}"
    return f"🟢 {display_name} Demand fresh at {clean_time}"


def send_fresh_ob_alert(item: dict, sector: str = "", is_pe: bool = False, force: bool = False, test: bool = False) -> bool:
    """Send a single Demand Fresh or Bear OB Fresh alert strictly for NIFTY or SENSEX with atomic deduplication."""
    if not isinstance(item, dict):
        return False

    raw_sym = str(item.get("symbol", "")).upper()

    # STRICT FILTER 1: Exclusively NIFTY and SENSEX (strictly block all individual stocks)
    if not any(k in raw_sym for k in ("NIFTY", "SENSEX")):
        logger.info("Ignoring alert for %s (Strictly NIFTY & SENSEX only)", raw_sym)
        return False

    # STRICT FILTER 2: Must not be tested or mitigated (Fresh only)
    if item.get("is_demand_tested") or item.get("status") in ("Tested", "Mitigated") or item.get("mitigated"):
        logger.info("Ignoring tested/mitigated order block for %s", raw_sym)
        return False

    display_name = "Nifty" if "NIFTY" in raw_sym else "Sensex"
    ob_time = item.get("demand_fresh_time") or item.get("time") or item.get("formation_time_str")
    clean_time = _format_time_str(ob_time)

    is_bear = (
        is_pe
        or item.get("is_pe")
        or item.get("side") in ("PE", "bearish")
        or item.get("type") in ("BEARISH", "-OB")
        or "BEAR" in str(item.get("label", "")).upper()
        or "SUPPLY" in str(item.get("label", "")).upper()
    )

    today = datetime.now(INDIA_TZ).date().isoformat()
    action_type = "BEAR_OB" if is_bear else "DEMAND"
    key = f"{display_name}_{action_type}_FRESH_{clean_time}"
    text = build_alert_message(item, sector=sector, is_pe=is_bear, test=test)

    # In local development mode, suppress actual Telegram dispatch to avoid double-firing with Render
    if not test and not force and not is_production_sender():
        logger.info("[Telegram DEV] Skipping real Telegram send on local dev workstation for %s (%s).", display_name, text)
        return True

    if not force and not test:
        # Atomic reservation check: only proceed if this worker successfully acquires the lock
        acquired = reserve_alert_atomic(key, today, text)
        if not acquired:
            logger.info("Alert already sent to Telegram previously: %s (skipped)", key)
            return True

    ok, reason = send_telegram_text(text)
    if ok:
        logger.info("✓ Dispatched Telegram alert: %s", text)
        return True
    else:
        logger.warning("✗ Failed sending Telegram alert for %s: %s", display_name, reason)
        return False


def dispatch_new_fresh_alerts(payload: dict | None = None) -> list[str]:
    """Scan NIFTY and SENSEX for newly formed Fresh Order Blocks (Demand Fresh & Bear OB Fresh) and notify Telegram.
    Strictly ONLY NIFTY and SENSEX alerts are dispatched (individual stocks remain suppressed).
    Applies Trend Regime Guard (ORB 5H / ORB 5L, % change, and ATR) to eliminate counter-trend traps:
      - In confirmed Bear regimes (e.g. crashing below ORB 5L), minor Demand bounces are suppressed as bull traps.
      - In confirmed Bull regimes (e.g. rallying above ORB 5H), minor Bear pullbacks are suppressed as bear traps.
    Ensures formation recency (within last 20 minutes) and atomic database deduplication.
    """
    if not is_production_sender():
        logger.debug("[Telegram DEV] Skipping dispatch_new_fresh_alerts on local dev machine.")
        return []

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

    now_ist = datetime.now(INDIA_TZ)
    today = now_ist.date()

    for sym_display, sym_code in targets:
        try:
            res = fetch_5m_candles_pipeline(sym_code, full_range=True)
            candles = res.get("completed_candles") or []
            if not candles:
                continue

            # Must strictly belong to today's active session
            all_days = [datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() for c in candles]
            if not all_days or max(all_days) != today:
                logger.debug("Candles for %s are not from today %s; skipping pre-market/yesterday alerts", sym_display, today)
                continue

            today_completed = [
                c for c in candles
                if datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() == today
            ]
            if not today_completed:
                continue

            # ── TREND REGIME GUARD & OPENING RANGE LEVELS ──
            orb_5h = float(today_completed[0]["high"])
            orb_5l = float(today_completed[0]["low"])
            open_price = float(today_completed[0]["open"])
            ltp = float(today_completed[-1]["close"])
            pct_change = (ltp - open_price) / open_price * 100.0

            # Dynamic session 5m ATR
            base_ranges = [float(c["high"]) - float(c["low"]) for c in today_completed[:min(14, len(today_completed))]]
            atr = sum(base_ranges) / len(base_ranges) if base_ranges else (25.0 if sym_display == "Nifty" else 75.0)

            # Confirmed Trend Regimes:
            # Bear Regime: Price below ORB 5L and down > 0.35% (or > 1.2x ATR below open)
            is_confirmed_bear = (ltp < orb_5l) and (pct_change < -0.35 or ltp < open_price - 1.2 * atr)
            # Bull Regime: Price above ORB 5H and up > 0.35% (or > 1.2x ATR above open)
            is_confirmed_bull = (ltp > orb_5h) and (pct_change > +0.35 or ltp > open_price + 1.2 * atr)

            ob_res = detect_order_blocks_and_structure(candles)
            raw_obs = ob_res.get("order_blocks", [])

            # Separate Bullish Demand Fresh vs Bearish Supply / Bear OB Fresh
            demand_fresh = [
                ob for ob in raw_obs
                if (ob.get("type") == "BULLISH" or ob.get("label") == "DEMAND")
                and ob.get("status") == "Fresh"
                and not ob.get("mitigated")
                and not ob.get("tested")
                and datetime.fromtimestamp(ob.get("time", 0), tz=INDIA_TZ).date() == today
            ]

            bear_fresh = [
                ob for ob in raw_obs
                if (ob.get("type") == "BEARISH" or ob.get("label") in ("BEAR OB", "SUPPLY"))
                and ob.get("status") == "Fresh"
                and not ob.get("mitigated")
                and not ob.get("tested")
                and datetime.fromtimestamp(ob.get("time", 0), tz=INDIA_TZ).date() == today
            ]

            sent_keys = _load_sent_keys()

            # 1. EVALUATE DEMAND FRESH CANDIDATES
            for ob in demand_fresh:
                f_time = ob.get("formation_time") or ob.get("time", 0)
                f_dt = datetime.fromtimestamp(f_time, tz=INDIA_TZ)

                # RECENCY GUARD: Zone must have formed recently (within last 20 minutes)
                age_seconds = (now_ist - f_dt).total_seconds()
                if age_seconds < -60 or age_seconds > 20 * 60:
                    logger.debug("Skipping aged Demand Fresh OB for %s formed at %s (age: %.1f mins)",
                                 sym_display, f_dt.strftime('%H:%M'), age_seconds / 60.0)
                    continue

                clean_time = _format_time_str(f_time)
                key = f"{sym_display}_DEMAND_FRESH_{clean_time}"
                if key in sent_keys:
                    continue

                ob_top = float(ob.get("top") or 0.0)

                # TRAP GUARD: If market is in confirmed Bear Regime and zone formed below ORB 5L,
                # this is a classic dead-cat bounce / counter-trend trap. Suppress!
                if is_confirmed_bear and ob_top < orb_5l:
                    logger.info("🛡️ [TRAP GUARD] Suppressed %s Demand Fresh at %s (Market in Bear Regime: %.2f%%, below ORB 5L %.1f)",
                                sym_display, clean_time, pct_change, orb_5l)
                    continue

                item = {
                    "symbol": sym_display,
                    "demand_fresh_time": clean_time,
                    "top": ob.get("top"),
                    "bottom": ob.get("bottom"),
                    "status": "Fresh",
                    "mitigated": False,
                    "type": "BULLISH",
                }
                if send_fresh_ob_alert(item, is_pe=False):
                    sent_this_cycle.append(key)

            # 2. EVALUATE BEAR OB FRESH CANDIDATES
            for ob in bear_fresh:
                f_time = ob.get("formation_time") or ob.get("time", 0)
                f_dt = datetime.fromtimestamp(f_time, tz=INDIA_TZ)

                # RECENCY GUARD: Zone must have formed recently (within last 20 minutes)
                age_seconds = (now_ist - f_dt).total_seconds()
                if age_seconds < -60 or age_seconds > 20 * 60:
                    logger.debug("Skipping aged Bear OB for %s formed at %s (age: %.1f mins)",
                                 sym_display, f_dt.strftime('%H:%M'), age_seconds / 60.0)
                    continue

                clean_time = _format_time_str(f_time)
                key = f"{sym_display}_BEAR_OB_FRESH_{clean_time}"
                if key in sent_keys:
                    continue

                ob_bottom = float(ob.get("bottom") or 0.0)

                # TRAP GUARD: If market is in confirmed Bull Regime and zone formed above ORB 5H,
                # this is a counter-trend dip. Suppress!
                if is_confirmed_bull and ob_bottom > orb_5h:
                    logger.info("🛡️ [TRAP GUARD] Suppressed %s Bear OB at %s (Market in Bull Regime: %.2f%%, above ORB 5H %.1f)",
                                sym_display, clean_time, pct_change, orb_5h)
                    continue

                item = {
                    "symbol": sym_display,
                    "demand_fresh_time": clean_time,
                    "top": ob.get("top"),
                    "bottom": ob.get("bottom"),
                    "status": "Fresh",
                    "mitigated": False,
                    "type": "BEARISH",
                    "is_pe": True,
                }
                if send_fresh_ob_alert(item, is_pe=True):
                    sent_this_cycle.append(key)

        except Exception as exc:
            logger.error("Error evaluating index fresh alerts for %s: %s", sym_display, exc)

    return sent_this_cycle


def _compute_dynamic_atr(candles_1m: list[dict[str, Any]], default_pts: float = 20.0) -> float:
    """Dynamically computes the True Range of the session's active 1-minute candles.
    Adapts thresholds to live market volatility instead of using fixed hardcoded points.
    """
    if len(candles_1m) < 5:
        return default_pts
    trs = [
        max(
            candles_1m[i]["high"] - candles_1m[i]["low"],
            abs(candles_1m[i]["high"] - candles_1m[i - 1]["close"]),
            abs(candles_1m[i]["low"] - candles_1m[i - 1]["close"]),
        )
        for i in range(1, len(candles_1m))
    ]
    valid = [t for t in trs if t > 0.5]
    if not valid:
        return default_pts
    return sum(valid) / len(valid)


def dispatch_instant_demand_surge() -> list[str]:
    """Scan NIFTY and SENSEX 1-minute candles in real time for instant breakouts & breakdowns.
    Fires the exact minute the impulse candle breaks out of the base, giving an early entry.
    Dual-directional:
      - Demand Surge (⚡ Green impulse breakout) in Bull / Neutral regimes.
      - Bear Breakdown Surge (⚡ Red impulse breakdown) in Bear / Neutral regimes.
    Trend Regime Guard:
      - In confirmed Bear regimes (e.g. crashing below ORB 5L), counter-trend Demand Surges are suppressed to prevent traps.
      - In confirmed Bull regimes (e.g. rallying above ORB 5H), counter-trend Bear Surges are suppressed.
    All thresholds are computed dynamically from real-time ATR with zero hardcoded prices.
    Uses atomic Postgres reservation to guarantee once-only dispatch across all instances.
    """
    if not is_production_sender():
        logger.debug("[Telegram DEV] Skipping dispatch_instant_demand_surge on local dev machine.")
        return []

    sent_this_cycle: list[str] = []

    try:
        import upstox_provider
    except Exception as exc:
        logger.warning("Could not import upstox_provider: %s", exc)
        return []

    targets = [
        ("Sensex", "SENSEX", 25.0),
        ("Nifty", "NIFTY", 10.0),
    ]

    now_ist = datetime.now(INDIA_TZ)
    today = now_ist.date()

    for sym_display, sym_code, fallback_atr in targets:
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

            # ── TREND REGIME FROM 1-MINUTE CANDLES ──
            orb_5h = max(c["high"] for c in today_c1m[:min(5, len(today_c1m))])
            orb_5l = min(c["low"] for c in today_c1m[:min(5, len(today_c1m))])
            open_p = float(today_c1m[0]["open"])
            ltp = float(today_c1m[-1]["close"])
            pct_chg = (ltp - open_p) / open_p * 100.0

            # Compute dynamic ATR threshold from today's live volatility
            session_atr = _compute_dynamic_atr(today_c1m, default_pts=fallback_atr)
            dynamic_min_pts = round(1.2 * session_atr, 1)

            is_bear_regime = (ltp < orb_5l) and (pct_chg < -0.35 or ltp < open_p - 1.2 * session_atr)
            is_bull_regime = (ltp > orb_5h) and (pct_chg > +0.35 or ltp > open_p + 1.2 * session_atr)

            # Look at the latest completed 1-minute candle
            curr = today_c1m[-1]
            candle_range = curr["high"] - curr["low"]
            c_dt = datetime.fromtimestamp(curr["timestamp"], tz=INDIA_TZ)
            time_str = c_dt.strftime("%I:%M %p").lstrip("0")
            curr_minutes = c_dt.hour * 60 + c_dt.minute

            # Base of preceding 3 to 8 candles
            base_candles = today_c1m[-9:-1]
            if not base_candles:
                continue

            base_high = max(c["high"] for c in base_candles)
            base_low = min(c["low"] for c in base_candles)

            sent_keys = _load_sent_keys()

            def _is_recently_sent(prefix: str) -> bool:
                for k in sent_keys:
                    if k.startswith(prefix):
                        try:
                            hhmm = k.split("_")[-1]
                            k_min = int(hhmm[:2]) * 60 + int(hhmm[2:])
                            if abs(curr_minutes - k_min) < 12:
                                return True
                        except Exception:
                            pass
                return False

            # ── CASE 1: DEMAND SURGE (GREEN IMPULSE BREAKOUT) ──
            bull_body = curr["close"] - curr["open"]
            upper_wick = curr["high"] - curr["close"]
            if (
                bull_body >= dynamic_min_pts
                and (candle_range <= 0 or upper_wick / candle_range <= 0.35)
                and curr["close"] > base_high
                and not is_bear_regime  # TRAP GUARD: Never buy into overhead breakdown walls
            ):
                key = f"{sym_display}_1M_SURGE_{today.isoformat()}_{c_dt.strftime('%H%M')}"
                if key not in sent_keys and not _is_recently_sent(f"{sym_display}_1M_SURGE_{today.isoformat()}_"):
                    msg = f"⚡ {sym_display} Demand Surge at {time_str} @ {curr['close']:,.0f} (Base: {base_high:,.0f})"
                    acquired = reserve_alert_atomic(key, today.isoformat(), msg)
                    if acquired:
                        ok, reason = send_telegram_text(msg)
                        if ok:
                            logger.info("✓ Dispatched Instant Demand Surge: %s", msg)
                            sent_this_cycle.append(key)
                        else:
                            logger.warning("✗ Failed sending Telegram alert for %s: %s", sym_display, reason)

            # ── CASE 2: BEAR BREAKDOWN SURGE (RED IMPULSE BREAKDOWN) ──
            bear_body = curr["open"] - curr["close"]
            lower_wick = curr["close"] - curr["low"]
            if (
                bear_body >= dynamic_min_pts
                and (candle_range <= 0 or lower_wick / candle_range <= 0.35)
                and curr["close"] < base_low
                and not is_bull_regime  # TRAP GUARD: Never short into runaway bull rallies
            ):
                key = f"{sym_display}_1M_BEAR_SURGE_{today.isoformat()}_{c_dt.strftime('%H%M')}"
                if key not in sent_keys and not _is_recently_sent(f"{sym_display}_1M_BEAR_SURGE_{today.isoformat()}_"):
                    msg = f"⚡ {sym_display} Bear Surge at {time_str} @ {curr['close']:,.0f} (Roof: {base_high:,.0f})"
                    acquired = reserve_alert_atomic(key, today.isoformat(), msg)
                    if acquired:
                        ok, reason = send_telegram_text(msg)
                        if ok:
                            logger.info("✓ Dispatched Instant Bear Surge: %s", msg)
                            sent_this_cycle.append(key)
                        else:
                            logger.warning("✗ Failed sending Telegram alert for %s: %s", sym_display, reason)

        except Exception as exc:
            logger.error("Error evaluating instant surge for %s: %s", sym_display, exc)

    return sent_this_cycle


if __name__ == "__main__":
    token, chat_id, enabled = get_credentials()
    print("=" * 60)
    print("ManojLabs Telegram Notification Tester (NIFTY & SENSEX Demand Fresh)")
    print(f"  Bot Token:        {'*' * 6 + token[-6:] if token else 'NOT CONFIGURED'}")
    print(f"  Chat ID:          {chat_id or 'NOT CONFIGURED'}")
    print(f"  Enabled:          {enabled}")
    print(f"  Production Mode:  {is_production_sender()}")
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
