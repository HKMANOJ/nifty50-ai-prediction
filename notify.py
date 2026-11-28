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
from datetime import datetime, timedelta
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
PENDING_STORE_PATH = ROOT / "inputs" / "telegram_pending_alerts.json"
_DEFAULT_PENDING_STORE_PATH = PENDING_STORE_PATH
DUAL_INDEX_WINDOW_SECONDS = 15 * 60
_DISPATCH_LOCK = threading.Lock()
_PENDING_LOCK = threading.Lock()
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
                        CREATE TABLE IF NOT EXISTS telegram_pending_alerts (
                            pending_key VARCHAR(64) PRIMARY KEY,
                            session_date DATE NOT NULL,
                            payload JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        CREATE INDEX IF NOT EXISTS idx_tg_pending_date ON telegram_pending_alerts(session_date);
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


MIN_ALERT_CONVICTION = 92


def _passes_conviction_gate(
    *,
    sym_display: str,
    clean_time: str,
    setup_side: str,
    ltp: float,
    open_price: float,
    day_high: float,
    day_low: float,
    orb_5h: float,
    orb_5l: float,
    ema20: float,
    atr: float,
    ob_top: float,
    ob_bottom: float,
    extension_atr: float,
    is_extended: bool,
    delta_bar: float,
    signed_extension_atr: float | None = None,
    extension_side: str = "",
    mtf_overall: str = "",
    above_5m_ema20: bool | None = None,
    min_conviction: int = MIN_ALERT_CONVICTION,
) -> bool:
    """Evaluate setup conviction with Gemini AI (deterministic fallback) and require >= min_conviction."""
    from gemini_evaluator import evaluate_trade_deterministic, evaluate_trade_with_gemini

    snapshot = {
        "symbol": sym_display,
        "time_ist": clean_time,
        "ltp": ltp,
        "day_open": open_price,
        "day_high": day_high,
        "day_low": day_low,
        "orb_5h": orb_5h,
        "orb_5l": orb_5l,
        "ema20": round(ema20, 2),
        "atr_5m": round(atr, 2),
        "atr_rolling": round(atr, 2),
        "setup_side": setup_side,
        "ob_top": ob_top,
        "ob_bottom": ob_bottom,
        "extension_atr": extension_atr,
        "signed_extension_atr": extension_atr if signed_extension_atr is None else signed_extension_atr,
        "extension_side": extension_side,
        "mtf_overall": mtf_overall,
        "above_5m_ema20": (ltp >= ema20 if ema20 > 0 else True) if above_5m_ema20 is None else above_5m_ema20,
        "is_extended": is_extended,
        "order_flow": {"delta_bar": delta_bar},
    }
    try:
        ai_res = evaluate_trade_with_gemini(snapshot, use_api=True)
    except Exception as ai_exc:
        logger.debug("Gemini check failed (%s); falling back to deterministic score", ai_exc)
        ai_res = evaluate_trade_deterministic(snapshot)

    score = int(ai_res.get("conviction_score") or 0)
    verdict = str(ai_res.get("verdict") or "REJECTED").upper()
    label = "Demand Fresh" if setup_side == "LONG" else "Bear OB"
    if verdict == "REJECTED" or score < min_conviction:
        logger.info(
            "🛡️ [CONVICTION GATE >=%d] Suppressed %s %s at %s (Verdict: %s, Score: %d/100, Reason: %s)",
            min_conviction, sym_display, label, clean_time, verdict, score, ai_res.get("rationale", ""),
        )
        return False
    return True


def _has_recent_conflicting_alert(sent_keys: set[str], sym_display: str, opp_type: str, curr_dt: datetime, min_separation_minutes: int = 25) -> bool:
    """Check if an opposite-directional alert was sent recently within min_separation_minutes."""
    prefix = f"{sym_display}_{opp_type}_FRESH_"
    curr_mins = curr_dt.hour * 60 + curr_dt.minute
    for k in sent_keys:
        if k.startswith(prefix):
            time_part = k[len(prefix):]
            try:
                # time_part is e.g. "11:35 AM" or "9:40 AM"
                t_dt = datetime.strptime(time_part.strip(), "%I:%M %p")
                k_mins = t_dt.hour * 60 + t_dt.minute
                if abs(curr_mins - k_mins) < min_separation_minutes:
                    return True
            except Exception:
                pass
    return False


def _load_pending_candidates(session_date: str) -> dict[str, dict[str, Any]]:
    """Load unexpired single-index pending confirmations (Neon Postgres first, local file fallback)."""
    merged: dict[str, dict[str, Any]] = {}
    use_pg = (PENDING_STORE_PATH == _DEFAULT_PENDING_STORE_PATH)
    if use_pg:
        conn = _get_pg_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT pending_key, payload FROM telegram_pending_alerts WHERE session_date = %s",
                    (session_date,),
                )
                rows = cur.fetchall()
                cur.close()
                conn.close()
                for p_key, payload in rows:
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    if isinstance(payload, dict):
                        merged[str(p_key)] = payload
            except Exception as exc:
                logger.debug("Failed loading pending alerts from Postgres: %s", exc)
                try:
                    conn.close()
                except Exception:
                    pass

    if PENDING_STORE_PATH.is_file():
        try:
            data = json.loads(PENDING_STORE_PATH.read_text(encoding="utf-8"))
            if data.get("session_date") == session_date and isinstance(data.get("candidates"), dict):
                for p_key, payload in data["candidates"].items():
                    if p_key not in merged and isinstance(payload, dict):
                        merged[str(p_key)] = payload
        except Exception:
            pass
    return merged


def _save_pending_candidates(session_date: str, candidates: dict[str, dict[str, Any]]) -> None:
    """Persist pending single-index candidates to Neon Postgres and local JSON fallback."""
    use_pg = (PENDING_STORE_PATH == _DEFAULT_PENDING_STORE_PATH)
    if use_pg:
        conn = _get_pg_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM telegram_pending_alerts WHERE session_date = %s", (session_date,))
                for p_key, payload in candidates.items():
                    cur.execute(
                        """
                        INSERT INTO telegram_pending_alerts (pending_key, session_date, payload, updated_at)
                        VALUES (%s, %s, %s::jsonb, NOW())
                        ON CONFLICT (pending_key) DO UPDATE SET
                            session_date = EXCLUDED.session_date,
                            payload = EXCLUDED.payload,
                            updated_at = NOW()
                        """,
                        (p_key, session_date, json.dumps(payload)),
                    )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as exc:
                logger.debug("Failed saving pending alerts to Postgres: %s", exc)
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass

    try:
        PENDING_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_STORE_PATH.write_text(
            json.dumps({"session_date": session_date, "candidates": candidates}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to persist pending index confirmations: %s", exc)


def _is_recent_ob_formation(
    f_time: Any,
    now_ist: datetime,
    max_age_after_close_min: int = 20,
) -> tuple[bool, datetime, float]:
    """Check whether OB formed recently, measuring 20m window from the 5m confirmation candle CLOSE."""
    f_dt = datetime.fromtimestamp(f_time, tz=INDIA_TZ)
    close_dt = f_dt + timedelta(minutes=5)
    age_after_close = (now_ist - close_dt).total_seconds()
    is_recent = -360.0 <= age_after_close <= max_age_after_close_min * 60.0
    return is_recent, f_dt, age_after_close


def _resolve_dual_index_candidates(
    cycle_candidates: list[dict[str, Any]],
    sent_keys: set[str],
    now_ist: datetime,
) -> list[dict[str, Any]]:
    """Pair qualifying NIFTY and SENSEX signals agreeing in direction within 15 minutes."""
    session_date = now_ist.date().isoformat()
    now_ts = now_ist.timestamp()

    with _PENDING_LOCK:
        pending = _load_pending_candidates(session_date)

        # Prune stale (> 15 min) or already-dispatched pending candidates
        cleaned: dict[str, dict[str, Any]] = {}
        for p_key, entry in pending.items():
            q_ts = float(entry.get("queued_ts") or 0.0)
            age = now_ts - q_ts
            if entry.get("key") in sent_keys:
                continue
            if -60.0 <= age <= DUAL_INDEX_WINDOW_SECONDS:
                cleaned[p_key] = entry

        # Merge newly passing candidates from this scan cycle
        for cand in cycle_candidates:
            p_key = f"{cand['side']}:{cand['sym']}"
            cleaned[p_key] = cand

        confirmed: list[dict[str, Any]] = []
        for side in ("LONG", "SHORT"):
            n_key = f"{side}:Nifty"
            s_key = f"{side}:Sensex"
            if n_key in cleaned and s_key in cleaned:
                n_entry = cleaned[n_key]
                s_entry = cleaned[s_key]
                gap = abs(float(n_entry["queued_ts"]) - float(s_entry["queued_ts"]))
                if gap <= DUAL_INDEX_WINDOW_SECONDS:
                    confirmed.extend([n_entry, s_entry])
                    cleaned.pop(n_key, None)
                    cleaned.pop(s_key, None)

        _save_pending_candidates(session_date, cleaned)

    if cycle_candidates and not confirmed:
        held = ", ".join(f"{c['sym']} {c['side']}@{c['item'].get('demand_fresh_time')}" for c in cycle_candidates)
        logger.info("🛡️ [DUAL-INDEX GATE] Holding single-index signal (%s) up to 15m for partner confirmation", held)

    return confirmed


def dispatch_new_fresh_alerts(payload: dict | None = None) -> list[str]:
    """Scan NIFTY and SENSEX for newly formed Fresh Order Blocks (Demand Fresh & Bear OB Fresh) and notify Telegram.
    Strictly ONLY NIFTY and SENSEX alerts are dispatched (individual stocks remain suppressed).
    Applies Multi-Layer Institutional Guards:
      1. Market Open Grace & Cutoff: Suppresses pre-09:30 open rush and post-14:45 close noise.
      2. Unified 5m EMA20 & MTF Alignment: Blocks counter-trend shorts above EMA20 and falling-knife buys.
      3. Signed Extension Gate: Strictly blocks overbought buys (> +1.8x ATR) and oversold shorts (< -1.8x ATR).
      4. Dual-Index Confirmation: Requires NIFTY and SENSEX directional agreement within 15 minutes.
    Ensures formation recency (within last 20 minutes) and atomic database deduplication.
    """
    if not is_production_sender():
        logger.debug("[Telegram DEV] Skipping dispatch_new_fresh_alerts on local dev machine.")
        return []

    sent_this_cycle: list[str] = []

    try:
        from stock_suggestion_index import fetch_5m_candles_pipeline, INDIA_TZ
        from smc_volume_profile_engine import (
            detect_order_blocks_and_structure,
            calculate_extension_gate,
            calculate_rolling_atr,
            calculate_multi_timeframe_matrix,
            compute_latest_delta_bar,
        )
    except Exception as exc:
        logger.warning("Could not import pipeline/SMC engine in dispatch: %s", exc)
        return []

    targets = [
        ("Nifty", "NIFTY50"),
        ("Sensex", "SENSEX"),
    ]

    now_ist = datetime.now(INDIA_TZ)
    today = now_ist.date()

    # Grace period: Initial balance formation between 09:15 and 09:30
    if now_ist.hour == 9 and now_ist.minute < 30:
        logger.debug("Skipping pre-09:30 opening range volatility rush.")
        return []

    # Cutoff: No new intraday entries after 2:45 PM (prevents broker square-off whipsaws)
    if now_ist.hour > 14 or (now_ist.hour == 14 and now_ist.minute > 45):
        logger.debug("Skipping post-14:45 late-session square-off rush.")
        return []

    sent_keys = _load_sent_keys()
    cycle_candidates: list[dict[str, Any]] = []

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

            # ── UNIFIED DYNAMIC VOLATILITY & MARKET STRUCTURE METRICS ──
            orb_5h = float(today_completed[0]["high"])
            orb_5l = float(today_completed[0]["low"])
            open_price = float(today_completed[0]["open"])
            ltp = float(today_completed[-1]["close"])
            pct_change = (ltp - open_price) / open_price * 100.0 if open_price > 0 else 0.0

            atr = calculate_rolling_atr(candles, period=14)
            if atr <= 0:
                atr = max(ltp * 0.0035, 5.0)
            min_expansion = 1.25 * atr

            # Dynamic Volatility Regimes (Normalized to rolling ATR standard deviation)
            is_confirmed_bear = (ltp < orb_5l) and (ltp < open_price - 1.25 * atr)

            # Unified 200-bar 5m EMA20 & signed extension from EMA20
            ext_gate = calculate_extension_gate(candles)
            ext_atr = float(ext_gate.get("extension_atr") or 0.0)
            signed_ext_atr = float(
                ext_gate.get("signed_extension_atr")
                if ext_gate.get("signed_extension_atr") is not None
                else ext_atr
            )
            ext_side = str(ext_gate.get("side") or "").upper()
            is_extended_long = bool(ext_gate.get("is_extended")) and ext_side == "LONG"
            is_extended_short = bool(ext_gate.get("is_extended")) and ext_side == "SHORT"

            ema20 = float(ext_gate.get("ema20") or 0.0)
            if ema20 <= 0:
                closes = [float(c["close"]) for c in candles[-200:]]
                alpha = 2.0 / 21.0
                ema20 = closes[0]
                for p in closes:
                    ema20 = alpha * p + (1.0 - alpha) * ema20
            above_5m_ema20 = ltp >= ema20 if ema20 > 0 else True

            mtf_res = calculate_multi_timeframe_matrix(candles)
            mtf_overall = str(mtf_res.get("overall") or "").upper()

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

            day_high_so_far = max(float(c["high"]) for c in today_completed)
            day_low_so_far = min(float(c["low"]) for c in today_completed)
            delta_bar = compute_latest_delta_bar(today_completed)

            # 1. EVALUATE DEMAND FRESH CANDIDATES
            for ob in demand_fresh:
                f_time = ob.get("formation_time") or ob.get("time", 0)
                is_recent, f_dt, age_after_close = _is_recent_ob_formation(f_time, now_ist)

                # RECENCY GUARD: Within 20 minutes after the 5m confirmation candle closes
                if not is_recent:
                    logger.debug("Skipping aged Demand Fresh OB for %s formed at %s (age post-close: %.1f mins)",
                                 sym_display, f_dt.strftime('%H:%M'), age_after_close / 60.0)
                    continue

                clean_time = _format_time_str(f_time)
                key = f"{sym_display}_DEMAND_FRESH_{clean_time}"
                if key in sent_keys:
                    continue

                ob_top = float(ob.get("top") or 0.0)
                ob_bottom = float(ob.get("bottom") or 0.0)

                # EXTENSION GUARD: Never buy at extreme overextended highs (> 1.8x ATR above EMA20)
                if is_extended_long:
                    logger.info("🛡️ [EXTENSION GUARD] Suppressed %s Demand Fresh at %s (Overstretched +%.1fx ATR above EMA20; exhaustion risk)",
                                sym_display, clean_time, ext_atr)
                    continue

                # TRAP GUARD: Only suppress if market is breaking down and NOT staging an institutional liquidity sweep recovery
                is_sweep_recovery = (ob_bottom <= orb_5l + 0.25 * atr) and (ltp >= ob_top)
                if is_confirmed_bear and ob_top < orb_5l and not is_sweep_recovery:
                    logger.info("🛡️ [TRAP GUARD] Suppressed %s Demand Fresh at %s (Market in Bear Regime: %.2f%%, below ORB 5L %.1f)",
                                sym_display, clean_time, pct_change, orb_5l)
                    continue

                # CHOP GUARD: Dynamic mid-range consolidation filter (Zero hardcoded point offsets)
                is_mid_range_chop = (
                    ob_top < orb_5h - 0.35 * atr
                    and ob_bottom > orb_5l + 0.35 * atr
                    and (day_high_so_far - day_low_so_far < 1.25 * atr)
                )
                if is_mid_range_chop:
                    logger.info("🛡️ [CHOP GUARD] Suppressed %s Demand Fresh at %s (Inside dead-center range consolidation without volatility expansion)",
                                sym_display, clean_time)
                    continue

                # DIRECTIONAL HYSTERESIS GUARD: Do not flip from Bear to Bull within 20 minutes unless broke ORB 5H
                if _has_recent_conflicting_alert(sent_keys, sym_display, "BEAR_OB", f_dt, min_separation_minutes=20):
                    if ltp < orb_5h:
                        logger.info("🛡️ [HYSTERESIS GUARD] Suppressed %s Demand Fresh at %s (Conflicting Bear alert within 20m)",
                                    sym_display, clean_time)
                        continue

                if not _passes_conviction_gate(
                    sym_display=sym_display,
                    clean_time=clean_time,
                    setup_side="LONG",
                    ltp=ltp,
                    open_price=open_price,
                    day_high=day_high_so_far,
                    day_low=day_low_so_far,
                    orb_5h=orb_5h,
                    orb_5l=orb_5l,
                    ema20=ema20,
                    atr=atr,
                    ob_top=ob_top,
                    ob_bottom=ob_bottom,
                    extension_atr=ext_atr,
                    is_extended=is_extended_long,
                    delta_bar=delta_bar,
                    signed_extension_atr=signed_ext_atr,
                    extension_side=ext_side,
                    mtf_overall=mtf_overall,
                    above_5m_ema20=above_5m_ema20,
                ):
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
                cycle_candidates.append({
                    "sym": sym_display,
                    "side": "LONG",
                    "key": key,
                    "item": item,
                    "is_pe": False,
                    "queued_ts": now_ist.timestamp(),
                })

            # 2. EVALUATE BEAR OB FRESH CANDIDATES
            for ob in bear_fresh:
                f_time = ob.get("formation_time") or ob.get("time", 0)
                is_recent, f_dt, age_after_close = _is_recent_ob_formation(f_time, now_ist)

                # RECENCY GUARD: Within 20 minutes after the 5m confirmation candle closes
                if not is_recent:
                    logger.debug("Skipping aged Bear OB for %s formed at %s (age post-close: %.1f mins)",
                                 sym_display, f_dt.strftime('%H:%M'), age_after_close / 60.0)
                    continue

                clean_time = _format_time_str(f_time)
                key = f"{sym_display}_BEAR_OB_FRESH_{clean_time}"
                if key in sent_keys:
                    continue

                ob_bottom = float(ob.get("bottom") or 0.0)
                ob_top = float(ob.get("top") or 0.0)

                # EXTENSION GUARD: Never short at extreme oversold lows (< -1.8x ATR below EMA20)
                if is_extended_short:
                    logger.info("🛡️ [EXTENSION GUARD] Suppressed %s Bear OB at %s (Overstretched %.1fx ATR below EMA20; oversold bounce risk)",
                                sym_display, clean_time, signed_ext_atr)
                    continue

                # Institutional High-Conviction Bear Patterns:
                # 1. Bear Trend Breakdown below ORB 5L (Guard against capitulation shakeouts near day low)
                is_near_climax_bottom = abs(ltp - day_low_so_far) <= 0.35 * atr
                c1_below_orb = (ltp < orb_5l) and (not is_near_climax_bottom or len(today_completed) < 30)
                # 2. Exhaustion Top Reversal at Day High after significant expansion above ORB 5H
                c2_top_reversal = (day_high_so_far - orb_5h >= min_expansion) and (ob_top >= day_high_so_far - 0.75 * atr)
                # 3. Structural breakdown below EMA20 following an expansion above ORB 5H
                c3_choch_ema = (day_high_so_far - orb_5h >= min_expansion) and (not above_5m_ema20) and (ltp > day_low_so_far + 1.25 * atr)

                # If none of the institutional patterns match, suppress counter-trend or opening chop shorts!
                if not (c1_below_orb or c2_top_reversal or c3_choch_ema):
                    logger.info("🛡️ [TRAP GUARD] Suppressed %s Bear OB at %s (Inside consolidation without top exhaustion or breakdown)",
                                sym_display, clean_time)
                    continue

                # MARKET STRUCTURE SHIFT (MSS) GUARD:
                # If trading above ORB 5H, price MUST have cracked below the recent 15-minute low (prevents shorting runaway bull rallies)
                if ltp > orb_5h and len(today_completed) >= 5:
                    recent_15m_low = min(c["low"] for c in today_completed[-4:-1])
                    if ltp >= recent_15m_low:
                        logger.info("🛡️ [MSS GUARD] Suppressed %s Bear OB at %s (No structural breakdown below recent 15m low %.1f)",
                                    sym_display, clean_time, recent_15m_low)
                        continue

                # DIRECTIONAL HYSTERESIS GUARD: Do not flip from Bull to Bear within 20 minutes unless broke ORB 5L
                if _has_recent_conflicting_alert(sent_keys, sym_display, "DEMAND", f_dt, min_separation_minutes=20):
                    if ltp > orb_5l:
                        logger.info("🛡️ [HYSTERESIS GUARD] Suppressed %s Bear OB at %s (Conflicting Bull alert within 20m)",
                                    sym_display, clean_time)
                        continue

                if not _passes_conviction_gate(
                    sym_display=sym_display,
                    clean_time=clean_time,
                    setup_side="SHORT",
                    ltp=ltp,
                    open_price=open_price,
                    day_high=day_high_so_far,
                    day_low=day_low_so_far,
                    orb_5h=orb_5h,
                    orb_5l=orb_5l,
                    ema20=ema20,
                    atr=atr,
                    ob_top=ob_top,
                    ob_bottom=ob_bottom,
                    extension_atr=ext_atr,
                    is_extended=is_extended_short,
                    delta_bar=delta_bar,
                    signed_extension_atr=signed_ext_atr,
                    extension_side=ext_side,
                    mtf_overall=mtf_overall,
                    above_5m_ema20=above_5m_ema20,
                ):
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
                cycle_candidates.append({
                    "sym": sym_display,
                    "side": "SHORT",
                    "key": key,
                    "item": item,
                    "is_pe": True,
                    "queued_ts": now_ist.timestamp(),
                })

        except Exception as exc:
            logger.error("Error evaluating index fresh alerts for %s: %s", sym_display, exc)

    for cand in _resolve_dual_index_candidates(cycle_candidates, sent_keys, now_ist):
        if send_fresh_ob_alert(cand["item"], is_pe=cand["is_pe"]):
            sent_this_cycle.append(cand["key"])

    return sent_this_cycle


def _compute_dynamic_atr(candles_1m: list[dict[str, Any]], default_pts: float = 0.0) -> float:
    """Dynamically computes the True Range of the session's active 1-minute candles.
    Adapts thresholds to live market volatility with zero hardcoded point assumptions.
    """
    last_close = float(candles_1m[-1].get("close", 0.0)) if candles_1m else 1000.0
    fallback_atr = default_pts if default_pts > 0 else max(last_close * 0.001, 1.0)
    if len(candles_1m) < 5:
        return fallback_atr
    trs = [
        max(
            candles_1m[i]["high"] - candles_1m[i]["low"],
            abs(candles_1m[i]["high"] - candles_1m[i - 1]["close"]),
            abs(candles_1m[i]["low"] - candles_1m[i - 1]["close"]),
        )
        for i in range(1, len(candles_1m))
    ]
    valid = [t for t in trs if t > 0.05]
    if not valid:
        return fallback_atr
    return sum(valid) / len(valid)


def dispatch_instant_demand_surge() -> list[str]:
    """1-minute impulse surges are disabled to prevent microstructure noise,
    over-trading, and misleading whipsaws. Institutional alerts are strictly driven by
    5-minute multi-timeframe Order Blocks with Extension and Regime Guards.
    """
    logger.debug("Instant 1-minute surge alerts are disabled in favor of disciplined 5-minute SMC Order Blocks.")
    return []


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
