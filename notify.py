#!/usr/bin/env python3
"""
ManojLabs AI Stock Hunter - Telegram Notification Dispatcher
Sends real-time alerts to Telegram when Demand Fresh or Bear OB Fresh setups occur.
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


def get_credentials() -> tuple[str, str, bool]:
    load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    enabled = os.getenv("TELEGRAM_ALERTS_ENABLED", "1").strip().lower() in ("1", "true", "yes")
    return token, chat_id, enabled


def send_telegram_text(text: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Send arbitrary formatted markdown text via Telegram Bot API."""
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


def build_alert_message(item: dict, sector: str = "", is_pe: bool = False, test: bool = False) -> str:
    """Format an institutional Demand Fresh or Bear OB Fresh notification."""
    symbol = item.get("symbol", "UNKNOWN")
    pct_chg = item.get("percent_change", 0.0)
    ltp = item.get("ltp") or item.get("entry") or 0.0
    entry = item.get("entry") or ltp
    stop = item.get("stop_loss")
    target = item.get("target")
    rvol = item.get("rvol_5m") or item.get("rvol")
    lot_size = item.get("lot_size")
    setup = item.get("setup", "Breakout")

    # Order block time and zone
    if is_pe:
        ob_time = item.get("supply_zone_time") or item.get("time") or datetime.now(INDIA_TZ).strftime("%I:%M %p")
        ob_zone = item.get("supply_zone") or item.get("zone", "")
        dot = "🔴"
        title = "BEAR OB FRESH ALERT · SHORT"
        bias = "Short Buildup (Supply Rejection)"
    else:
        ob_time = item.get("demand_fresh_time") or item.get("time") or datetime.now(INDIA_TZ).strftime("%I:%M %p")
        ob_zone = item.get("demand_zone") or item.get("zone", "")
        dot = "🟢"
        title = "DEMAND FRESH ALERT · BUY"
        bias = "Long Buildup (Demand Base Continuation)"

    lines = [
        f"{dot} *{title}*",
        f"⚡ *Stock:* `{symbol}`  |  *LTP:* `₹{ltp:,.2f}` ({pct_chg:+.2f}%)",
    ]
    if sector:
        lines.append(f"🏢 *Sector Alignment:* `{sector}`")

    lines.append(f"⏰ *Base Formed Time:* `{ob_time} IST`")
    if ob_zone:
        lines.append(f"📦 *Order Block Zone:* `{ob_zone}`")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    if entry and stop:
        risk_pts = abs(entry - stop)
        risk_pct = (risk_pts / entry) * 100 if entry else 0.0
        t1 = target or (entry - 1.5 * risk_pts if is_pe else entry + 1.5 * risk_pts)
        t2 = entry - 2.5 * risk_pts if is_pe else entry + 2.5 * risk_pts
        t1_pct = (abs(t1 - entry) / entry) * 100 if entry else 0.0
        t2_pct = (abs(t2 - entry) / entry) * 100 if entry else 0.0

        lines.extend([
            f"💰 *Entry Trigger:* `₹{entry:,.2f}`",
            f"🛑 *Stop Loss:* `₹{stop:,.2f}` (-{risk_pct:.2f}%)",
            f"🎯 *Target 1 (1.5R):* `₹{t1:,.2f}` (+{t1_pct:.2f}%)",
            f"🎯 *Target 2 (2.5R):* `₹{t2:,.2f}` (+{t2_pct:.2f}%)",
        ])

    metrics = []
    if lot_size:
        metrics.append(f"📦 Lot: `{lot_size}`")
    if rvol:
        metrics.append(f"📊 RVOL: `{float(rvol):.1f}x`")
    metrics.append(f"⚡ Setup: `{setup}`")
    if metrics:
        lines.append("  |  ".join(metrics))

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔔 *ManojLabs AI Stock Hunter V2*")
    if test:
        lines.append("*(Test Simulation Verification)*")

    return "\n".join(lines)


def send_fresh_ob_alert(item: dict, sector: str = "", is_pe: bool = False, force: bool = False, test: bool = False) -> bool:
    """Send a single Demand Fresh or Bear OB alert with session deduplication."""
    symbol = item.get("symbol", "").upper()
    if not symbol:
        return False

    ob_time = item.get("supply_zone_time") if is_pe else item.get("demand_fresh_time")
    ob_time = ob_time or item.get("time") or datetime.now(INDIA_TZ).strftime("%I:%M %p")

    side = "PE" if is_pe else "CE"
    key = f"{symbol}_{side}_{ob_time}"

    if not force and not test:
        sent = _load_sent_keys()
        if key in sent:
            logger.info("Alert already sent to Telegram previously: %s (skipped)", key)
            return True

    text = build_alert_message(item, sector=sector, is_pe=is_pe, test=test)
    ok, reason = send_telegram_text(text)
    if ok:
        logger.info("✓ Dispatched Telegram alert for %s (%s)", symbol, side)
        if not test:
            _save_sent_key(key)
        return True
    else:
        logger.warning("✗ Failed sending Telegram alert for %s: %s", symbol, reason)
        return False


def dispatch_new_fresh_alerts(payload: dict) -> list[str]:
    """Scan payload for any newly formed Demand Fresh or Bear OB Fresh setups and notify Telegram."""
    if not payload:
        return []

    sent_this_cycle: list[str] = []
    bullish = payload.get("bullish") or []
    bearish = payload.get("bearish") or []

    # Map sectors from payload sector_flow if present
    sec_flow = payload.get("sector_flow") or {}
    sym_to_sector = {}
    for sec in sec_flow.get("sectors", []):
        sec_name = sec.get("name", "")
        for s in sec.get("stocks", []):
            if isinstance(s, dict) and s.get("symbol"):
                sym_to_sector[s["symbol"]] = sec_name

    # 1. Bullish Demand Fresh
    for row in bullish:
        if not isinstance(row, dict):
            continue
        qtags = row.get("quality_tags") or []
        is_df = bool(row.get("is_demand_fresh") or any("DEMAND · FRESH" in t for t in qtags))
        pct_change = row.get("percent_change") or 0.0
        if is_df and pct_change >= 0.0:
            sector = sym_to_sector.get(row.get("symbol"), "Top Sector")
            ob_time = row.get("demand_fresh_time") or ""
            key = f"{row.get('symbol')}_CE_{ob_time}"
            if key not in _load_sent_keys():
                if send_fresh_ob_alert(row, sector=sector, is_pe=False):
                    sent_this_cycle.append(key)

    # 2. Bearish Bear OB Fresh
    for row in bearish:
        if not isinstance(row, dict):
            continue
        qtags = row.get("quality_tags") or []
        is_sz = bool(row.get("is_supply_zone") or any("BEAR OB · FRESH" in t or "SUPPLY ZONE" in t for t in qtags))
        pct_change = row.get("percent_change") or 0.0
        if is_sz and pct_change <= 0.0:
            sector = sym_to_sector.get(row.get("symbol"), "Lagging Sector")
            ob_time = row.get("supply_zone_time") or ""
            key = f"{row.get('symbol')}_PE_{ob_time}"
            if key not in _load_sent_keys():
                if send_fresh_ob_alert(row, sector=sector, is_pe=True):
                    sent_this_cycle.append(key)

    return sent_this_cycle


if __name__ == "__main__":
    token, chat_id, enabled = get_credentials()
    print("=" * 60)
    print("ManojLabs Telegram Notification Tester")
    print(f"  Bot Token:   {'*' * 6 + token[-6:] if token else 'NOT CONFIGURED'}")
    print(f"  Chat ID:     {chat_id or 'NOT CONFIGURED'}")
    print(f"  Enabled:     {enabled}")
    print("=" * 60)

    if not token or not chat_id:
        print("Error: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        sys.exit(1)

    print("\nSending live test Demand Fresh alert...")
    test_item = {
        "symbol": "TATACONSUM",
        "percent_change": 3.57,
        "entry": 1118.39,
        "ltp": 1118.39,
        "stop_loss": 1109.76,
        "target": 1131.34,
        "lot_size": 550,
        "rvol_5m": 2.4,
        "setup": "Long buildup",
        "demand_fresh_time": "02:20 PM",
        "demand_zone": "₹1,102.50 - ₹1,104.50 (Fresh)",
    }
    ok = send_fresh_ob_alert(test_item, sector="FMCG (+1.2%)", is_pe=False, test=True, force=True)
    if ok:
        print("✅ Demand Fresh test alert delivered successfully to Telegram!")
    else:
        print("❌ Delivery failed.")
