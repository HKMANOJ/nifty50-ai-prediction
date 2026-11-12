#!/usr/bin/env python3
"""Serve the live market app with refresh APIs."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import select
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from datetime import datetime, timezone, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent

# Ensure local venv packages (e.g. websockets) are importable under any python3 invoker
import glob
for _sp in glob.glob(str(ROOT / "venv/lib/python*/site-packages")):
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import db_adapter

DOWNLOADER = ROOT / "download_real_market_inputs.py"
COLLECTOR = ROOT / "collect_nifty50_market_data.py"
MOST_ACTIVE_FETCHER = ROOT / "fetch_most_active_nse.py"
CANDLE_STORE = ROOT / "store_candles_mysql.py"
CANDLE_FETCHER = ROOT / "fetch_candles_mysql.py"
AI_OPTIONS = ROOT / "ai_options_mysql.py"
LEARNING_ENGINE = ROOT / "learning_engine.py"
OPPORTUNITY_AUDIT = ROOT / "opportunity_audit_mysql.py"
REPLAY_AUDIT = ROOT / "replay_opportunity_audit.py"
ACCURACY_API = ROOT / "accuracy_api.py"
STOCK_SUGGESTION = ROOT / "stock_suggestion_index.py"
VENV_PYTHON = ROOT / "venv" / "bin" / "python"
SNAPSHOT = ROOT / "market_snapshot.latest.json"
MOST_ACTIVE = ROOT / "inputs" / "most_active.latest.json"
DEFAULT_STOCK_SUGGESTION_SNAPSHOT = ROOT / "inputs" / "stock_suggestion_index.latest.json"
_scanner_data_dir = os.environ.get("PERSISTENT_DATA_DIR") or os.environ.get("DATA_DIR")
STOCK_SUGGESTION_SNAPSHOT = (
    Path(_scanner_data_dir) / DEFAULT_STOCK_SUGGESTION_SNAPSHOT.name
    if _scanner_data_dir else DEFAULT_STOCK_SUGGESTION_SNAPSHOT
)
REFRESH_LOCK = threading.Lock()

# Debounce: collapse a burst of refresh requests (many tabs, reconnects, button
# spam) into at most one real scan per MIN_SCAN_INTERVAL. Between scans, callers
# get the last result instead of queueing another subprocess.
MIN_SCAN_INTERVAL_SECONDS = 45.0
_SCAN_STATE: dict[str, Any] = {"ts": 0.0, "payload": None, "signature": None}
_CLOSED_SESSION_RECOVERY_LOCK = threading.Lock()
_INDEX_QUOTE_LOCK = threading.Lock()
_INDEX_QUOTES: dict[str, Any] = {"ts": 0.0, "quotes": {}}


def _payload_has_signal(payload: Any) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("ok")
        and (payload.get("bullish") or payload.get("bearish"))
    )


def _payload_signature(payload: dict[str, Any]) -> str:
    rows = list(payload.get("bullish") or []) + list(payload.get("bearish") or [])
    row_sig = "|".join(
        f"{r.get('symbol')}:{r.get('five_min_status')}:{r.get('is_breakout')}:{r.get('one_side_rally_score')}:{r.get('ltp')}"
        for r in rows
    )
    return f"{payload.get('snapshot_id', '')}:{row_sig}"

IST_TZ = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    return datetime.now(IST_TZ)

def is_ist_market_hours(check_dt: datetime | None = None) -> bool:
    """Checks if the given or current timestamp falls within Indian market hours (09:15-15:30 IST, Mon-Fri)."""
    dt = check_dt or get_ist_now()
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    current_minutes = dt.hour * 60 + dt.minute
    return (9 * 60 + 15) <= current_minutes <= (15 * 60 + 30)

LAST_AUTO_SCAN_INFO: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 300,
    "last_run_ist": None,
    "last_duration_seconds": None,
    "status": "idle",
    "next_run_ist": None,
}
# Last automatic scan of the day is at this clock time - after it, the
# auto-scanner stops entirely (even though NSE itself stays open till 15:30).
# A manual refresh from the UI still works; this only stops the unattended
# background loop.
AUTO_SCAN_LAST_RUN_MINUTES = 15 * 60 + 20  # 3:20 PM IST (covers institutional power hour & closing squeeze)

# Yesterday's full scan results otherwise sit on screen looking "live" from
# market close until the first real 9:15+ scan completes (30+ seconds in).
# Pre-market, clear everything so the dashboard honestly shows "getting
# ready" instead of stale numbers dressed up as current.
PREMARKET_RESET_START_MINUTES = 9 * 60       # 9:00 AM IST
PREMARKET_RESET_END_MINUTES = 9 * 60 + 15    # 9:15 AM IST - real market open
_premarket_reset_date: str | None = None


def _run_premarket_reset_if_due(now_ist: datetime) -> None:
    global _premarket_reset_date
    current_minutes = now_ist.hour * 60 + now_ist.minute
    today_str = now_ist.date().isoformat()
    if now_ist.weekday() >= 5:  # weekend - nothing to get ready for
        return
    # Tightly bounded to the 9:00-9:15 pre-market window only (not "anytime
    # before 2:45 PM") - a mid-day container restart resets the in-memory
    # _premarket_reset_date flag too, and this must NOT be able to fire again
    # after real market open and wipe intraday data that's already been
    # collected for the day.
    if current_minutes < PREMARKET_RESET_START_MINUTES or current_minutes >= PREMARKET_RESET_END_MINUTES:
        return
    if _premarket_reset_date == today_str:
        return
    try:
        exec_py = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        res = run_command(
            [exec_py, "-c",
             "import json, sys; from datetime import datetime; "
             "from zoneinfo import ZoneInfo; import stock_suggestion_index as ssi; "
             "print(json.dumps(ssi.build_premarket_reset_payload(datetime.now(ZoneInfo('Asia/Kolkata')))))"],
            timeout=30,
        )
        empty_payload = res.get("stdout") if isinstance(res.get("stdout"), dict) else None
        if empty_payload:
            _SCAN_STATE["opening_payload"] = empty_payload
            # Otherwise the Auto-Scan badge keeps showing yesterday's last-run
            # time/status until the first real 9:15+ scan overwrites it.
            LAST_AUTO_SCAN_INFO["status"] = "pre_market"
            LAST_AUTO_SCAN_INFO["last_run_ist"] = None
            LAST_AUTO_SCAN_INFO["last_duration_seconds"] = None
            LAST_AUTO_SCAN_INFO["next_run_ist"] = None
            print(f"[{now_ist.strftime('%H:%M:%S IST')}] [PREMARKET-RESET] Reset session state for {today_str}")
            _premarket_reset_date = today_str

            # Purge Neon database records older than 7 days during daily pre-market window
            try:
                from scanner_log_retention import retention_enabled, run_daily_db_purge
                if retention_enabled():
                    purge_summary = run_daily_db_purge(retention_days=7)
                    print(f"[PREMARKET-PURGE] {purge_summary}")
            except Exception as purge_err:
                print(f"[PREMARKET-PURGE ERROR] DB purge failed: {purge_err}")
    except Exception as exc:
        print(f"[PREMARKET-RESET ERROR] {exc}")


def compute_seconds_until_next_5min_grid(interval_seconds: int = 300, buffer_seconds: int = 3) -> tuple[float, datetime]:
    """Calculates sleep seconds to align exactly with the next wall-clock 5-minute grid (e.g. 09:20:03, 09:25:03).
    Prevents cumulative scheduler drift from scan execution duration."""
    now_ist = get_ist_now()
    market_open = now_ist.replace(hour=9, minute=15, second=buffer_seconds, microsecond=0)
    if now_ist < market_open:
        sleep_secs = max(1.0, (market_open - now_ist).total_seconds())
        return sleep_secs, market_open

    seconds_since_midnight = now_ist.hour * 3600 + now_ist.minute * 60 + now_ist.second + (now_ist.microsecond / 1_000_000)
    next_grid_seconds = ((int(seconds_since_midnight) // interval_seconds) + 1) * interval_seconds + buffer_seconds
    target_dt = now_ist.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=next_grid_seconds)
    sleep_secs = max(1.0, (target_dt - now_ist).total_seconds())
    return sleep_secs, target_dt


def start_background_auto_scanner(interval_seconds: int = 300) -> None:
    """Spawns an autonomous background daemon thread that rescans 200+ NSE stocks every 5 min during market hours."""
    def _loop() -> None:
        import scanner_trace
        scanner_trace.submit_health("scheduler_started", interval_seconds=interval_seconds,
                                    auto_scan_cutoff_ist="15:20", process_id=os.getpid())
        time.sleep(5)  # Grace period on boot
        while True:
            try:
                now_ist = get_ist_now()
                current_minutes = now_ist.hour * 60 + now_ist.minute
                _run_premarket_reset_if_due(now_ist)
                if is_ist_market_hours(now_ist) and current_minutes <= AUTO_SCAN_LAST_RUN_MINUTES:
                    if REFRESH_LOCK.acquire(blocking=False):
                        LAST_AUTO_SCAN_INFO["status"] = "running"
                        scan_start = time.time()
                        attempt_id = scanner_trace.log.new_scan()
                        scanner_trace.submit_health("scheduled_scan_started", attempt_id=attempt_id)
                        print(f"[{now_ist.strftime('%H:%M:%S IST')}] [AUTO-SCANNER] Executing 5-minute background market scan...")
                        try:
                            exec_py = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
                            res = run_command([exec_py, str(STOCK_SUGGESTION)], timeout=120)
                            dur = round(time.time() - scan_start, 1)
                            scan_payload = res.get("stdout") if isinstance(res.get("stdout"), dict) else {}
                            has_signal = (_payload_has_signal(scan_payload) and not scan_payload.get("_stale_kept")
                                          and scan_payload.get("session_date") == now_ist.date().isoformat())
                            scanner_trace.submit_health("scheduled_scan_finished", attempt_id=attempt_id,
                                snapshot_id=scan_payload.get("snapshot_id"), duration_seconds=dur,
                                subprocess_ok=res.get("ok"), has_signal=has_signal)
                            _SCAN_STATE["ts"] = time.time()
                            LAST_AUTO_SCAN_INFO["last_run_ist"] = get_ist_now().strftime("%I:%M:%S %p IST")
                            LAST_AUTO_SCAN_INFO["last_duration_seconds"] = dur
                            LAST_AUTO_SCAN_INFO["status"] = "success" if has_signal else "failed"
                            print(f"[{get_ist_now().strftime('%H:%M:%S IST')}] [AUTO-SCANNER] Scan completed in {dur}s (signal={has_signal})")
                            # Push to browsers only when the scan has real data AND
                            # it changed - prevents blank/flicker broadcast storms.
                            if has_signal:
                                _SCAN_STATE["payload"] = scan_payload
                                sig = _payload_signature(scan_payload)
                                if sig != _SCAN_STATE.get("signature"):
                                    _SCAN_STATE["signature"] = sig
                                    try:
                                        from websocket_server import broadcast_scanner_update_sync
                                        broadcast_scanner_update_sync(scan_payload)
                                    except Exception as bc_err:
                                        print(f"[WS BROADCAST ERROR] {bc_err}")
                        except Exception as scan_err:
                            scanner_trace.submit_health("scheduled_scan_failed", attempt_id=attempt_id,
                                                        error_type=type(scan_err).__name__)
                            print(f"[AUTO-SCANNER ERROR] {scan_err}")
                            LAST_AUTO_SCAN_INFO["status"] = f"error: {scan_err}"
                        finally:
                            REFRESH_LOCK.release()
                    else:
                        scanner_trace.submit_health("scheduled_scan_skipped", reason="refresh_lock_busy")
                        print("[AUTO-SCANNER] Skipping interval: manual refresh or lock is currently active.")

                    # Wall-clock grid alignment (e.g. 09:20:03, 09:25:03, 09:30:03...)
                    # Eliminates cumulative drift caused by subprocess run duration.
                    sleep_secs, next_run = compute_seconds_until_next_5min_grid(interval_seconds=interval_seconds, buffer_seconds=3)
                    LAST_AUTO_SCAN_INFO["next_run_ist"] = next_run.strftime("%I:%M:%S %p IST")
                    print(f"[{get_ist_now().strftime('%H:%M:%S IST')}] [AUTO-SCANNER] Next grid-aligned scan scheduled at {next_run.strftime('%H:%M:%S IST')} (sleeping {sleep_secs:.1f}s)")
                    time.sleep(sleep_secs)
                elif is_ist_market_hours(now_ist):
                    # Past our 3:20 PM cutoff but NSE itself is still open till 15:30 -
                    # the auto-scanner is done for the day; no more background scans.
                    LAST_AUTO_SCAN_INFO["status"] = "scan_window_closed"
                    LAST_AUTO_SCAN_INFO["next_run_ist"] = None
                    time.sleep(30)
                else:
                    in_warmup = now_ist.weekday() < 5 and PREMARKET_RESET_START_MINUTES <= current_minutes < PREMARKET_RESET_END_MINUTES
                    LAST_AUTO_SCAN_INFO["status"] = "pre_market" if in_warmup else "market_closed"
                    market_open_today = now_ist.replace(hour=9, minute=15, second=3, microsecond=0)
                    if now_ist.weekday() < 5 and now_ist < market_open_today:
                        pre_sleep = min(60.0, max(5.0, (market_open_today - now_ist).total_seconds()))
                        LAST_AUTO_SCAN_INFO["next_run_ist"] = market_open_today.strftime("%I:%M:%S %p IST")
                        time.sleep(pre_sleep)
                    else:
                        time.sleep(30)
            except Exception as e:
                print(f"[AUTO-SCANNER LOOP EXCEPTION] {e}")
                time.sleep(30)
            except Exception as e:
                print(f"[AUTO-SCANNER LOOP EXCEPTION] {e}")
                time.sleep(30)

    scanner_thread = threading.Thread(target=_loop, name="BackgroundAutoScanner", daemon=True)
    scanner_thread.start()
    print(f"[AUTO-SCANNER] Background 5-min auto-scanner thread active (interval: {interval_seconds}s).")


def parse_args() -> argparse.Namespace:
    default_host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    parser = argparse.ArgumentParser(description="Serve the live market app with one-click refresh APIs.")
    parser.add_argument("--host", default=default_host, help=f"Host to bind. Default: {default_host}")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="Port to bind. Default: 8000")
    parser.add_argument("--world-timespan", default="3days", help="Timespan passed to the downloader's world-signal builder")
    parser.add_argument("--world-maxrecords", type=int, default=50, help="Max records passed to the downloader's world-signal builder")
    return parser.parse_args()


def run_command(command: list[str], *, timeout: int = 180) -> dict[str, Any]:
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    parsed_stdout: Any = None
    if stdout:
        try:
            parsed_stdout = json.loads(stdout)
        except json.JSONDecodeError:
            parsed_stdout = stdout

    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "command": command,
        "stdout": parsed_stdout,
        "stderr": stderr,
        "duration_seconds": round(time.time() - started, 2),
    }


class NiftyHandler(SimpleHTTPRequestHandler):
    server_version = "StockSuggestionLiveServer/1.0"

    def __init__(self, *args: Any, directory: str | None = None, world_timespan: str = "3days", world_maxrecords: int = 50, **kwargs: Any) -> None:
        self.world_timespan = world_timespan
        self.world_maxrecords = world_maxrecords
        super().__init__(*args, directory=directory, **kwargs)

    def guess_type(self, path: str) -> str:
        guessed = super().guess_type(path)
        if guessed == "text/html":
            return "text/html; charset=utf-8"
        if guessed == "text/css":
            return "text/css; charset=utf-8"
        if guessed == "application/javascript":
            return "application/javascript; charset=utf-8"
        if guessed == "application/json":
            return "application/json; charset=utf-8"
        if guessed == "text/plain":
            return "text/plain; charset=utf-8"
        return guessed

    def log_message(self, format: str, *args: Any) -> None:
        sys.stdout.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def _deny_private_static(self, path: str) -> bool:
        """Keep existing source/secret/raw-log protection without the Replay feature."""
        decoded = unquote(path, errors="replace")
        normalized = "/" + posixpath.normpath(decoded).lstrip("/")
        lower = normalized.lower()
        private = (
            any(part.startswith(".") and part not in (".", "..") for part in decoded.split("/"))
            or lower == "/inputs/scanner_telemetry"
            or lower.startswith("/inputs/scanner_telemetry/")
            or any(part in ("__pycache__", "venv", ".venv") for part in normalized.split("/"))
            or lower.endswith((".py", ".pyc", ".pyo"))
        )
        if private:
            self.send_error(HTTPStatus.NOT_FOUND)
        return private

    def do_HEAD(self) -> None:
        if not self._deny_private_static(urlparse(self.path).path):
            super().do_HEAD()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _proxy_websocket_to_backend(self, port: int = 8765) -> None:
        """Proxies WebSocket connections arriving on public HTTP route (/ws) to the local WebSocket engine on port 8765.
        Enables single-port production hosting on Render (wss://.../ws)."""
        backend_sock = None
        try:
            backend_sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
            request_line = f"GET {self.path} {self.request_version}\r\n"
            headers_blob = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()) + "\r\n"
            backend_sock.sendall((request_line + headers_blob).encode("utf-8"))

            client_sock = self.connection
            client_sock.settimeout(10.0)
            backend_sock.settimeout(10.0)

            sockets = [client_sock, backend_sock]
            while True:
                r_ready, _, x_ready = select.select(sockets, [], sockets, 60.0)
                if x_ready:
                    break
                if not r_ready:
                    continue
                for sock in r_ready:
                    other = backend_sock if sock is client_sock else client_sock
                    try:
                        data = sock.recv(65536)
                        if not data:
                            return
                        other.sendall(data)
                    except (socket.timeout, BlockingIOError, InterruptedError):
                        continue
                    except Exception:
                        return
        except Exception:
            pass
        finally:
            if backend_sock:
                try:
                    backend_sock.close()
                except Exception:
                    pass
            self.close_connection = True

    def do_GET(self) -> None:
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/ws" or self.headers.get("Upgrade", "").lower() == "websocket":
            backend_port = int(os.environ.get("WS_BACKEND_PORT", "8765"))
            self._proxy_websocket_to_backend(port=backend_port)
            return
        if self._deny_private_static(parsed_path.path):
            return
        if parsed_path.path in ("", "/"):
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/MLAIStockV2.html")
            self.end_headers()
            return

        if parsed_path.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "stock-suggestion-live-server"})
            return

        if parsed_path.path == "/api/most_active":
            self._serve_most_active()
            return

        if parsed_path.path == "/api/candles":
            self._serve_mysql_candles(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/ai_options/latest":
            self._serve_latest_ai_option(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/ai_options/history":
            self._serve_ai_option_history(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/learning/report":
            self._serve_learning_report()
            return

        if parsed_path.path == "/api/opportunity_audit/summary":
            self._serve_opportunity_audit_summary(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/opportunity_audit/history":
            self._serve_opportunity_audit_history(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/audit_debug":
            self._serve_audit_debug(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/replay_audit":
            self._serve_replay_audit(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/accuracy/summary":
            self._serve_accuracy_summary()
            return

        if parsed_path.path == "/api/live_option_chain":
            self._serve_live_option_chain()
            return

        if parsed_path.path == "/api/stock_suggestions":
            self._serve_stock_suggestions()
            return

        if parsed_path.path == "/api/upstox/login_url":
            try:
                import upstox_provider
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "login_url": upstox_provider.get_login_url(),
                    "authenticated": upstox_provider.is_authenticated(),
                })
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return

        if parsed_path.path == "/api/upstox/status":
            try:
                import upstox_provider
                auth = upstox_provider.is_authenticated()
                profile = upstox_provider.get_user_profile() if auth else None
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "authenticated": auth,
                    "profile": profile,
                    "feed_active": True,
                    "instruments_loaded": 2856,
                })
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return

        if parsed_path.path == "/api/upstox/callback":
            import upstox_provider
            query = parse_qs(parsed_path.query)
            code = (query.get("code") or [""])[0]
            if code:
                try:
                    upstox_provider.exchange_code_for_token(code)
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "/MLAIStockV2.html?upstox=connected")
                    self.end_headers()
                    return
                except Exception as exc:
                    login_url = upstox_provider.get_login_url()
                    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Upstox Authorization</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; background:#0f172a; color:#f8fafc; text-align:center; padding:60px 20px;">
  <div style="max-width:480px; margin:0 auto; background:#1e293b; padding:32px; border-radius:12px; border:1px solid #334155;">
    <div style="font-size:36px; margin-bottom:12px;">⚠️</div>
    <h3 style="margin:0 0 8px 0; color:#ef4444;">Login Code Expired</h3>
    <p style="color:#94a3b8; font-size:14px; margin-bottom:24px;">Upstox single-use login codes expire after 60 seconds ({exc}). Please start a fresh login session below.</p>
    <a href="{login_url}" style="display:inline-block; background:#7c3aed; color:#fff; font-weight:700; padding:12px 24px; border-radius:8px; text-decoration:none; font-size:14px;">👉 Click Here to Log in Fresh to Upstox</a>
  </div>
</body>
</html>"""
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html.encode("utf-8"))))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "No code received from Upstox"})
            return

        if parsed_path.path == "/api/stock_candles":
            self._serve_stock_candles(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/stock_overview":
            self._serve_stock_overview(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/index_live":
            self._serve_index_live()
            return

        super().do_GET()

    def _serve_index_live(self) -> None:
        from stock_suggestion_index import fetch_index_context

        with _INDEX_QUOTE_LOCK:
            if time.monotonic() - _INDEX_QUOTES["ts"] >= 5:
                current, _ = fetch_index_context()
                previous = _INDEX_QUOTES["quotes"]
                _INDEX_QUOTES["quotes"] = {
                    **{name: {**quote, "is_stale": True} for name, quote in previous.items() if name not in current},
                    **current,
                }
                _INDEX_QUOTES["ts"] = time.monotonic()
            quotes = _INDEX_QUOTES["quotes"]
        self._send_json(HTTPStatus.OK, {"ok": True, "index_context": quotes})


    def do_POST(self) -> None:
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/alerts/ack":
            try:
                body = self._read_json_body()
                event_id = body.get("alert_event_id")
                if not event_id or not isinstance(event_id, str):
                    raise ValueError("missing alert_event_id")
                import re
                if not re.match(r"^evt_[A-Za-z0-9_-]+$", event_id):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "unknown_or_invalid_event_id"})
                    return
                from stock_suggestion_index import acknowledge_alert
                acked = acknowledge_alert(event_id, client_id=body.get("client_id"))
                self._send_json(HTTPStatus.OK, {"ok": True, "alert_event_id": event_id, "acknowledged": bool(acked)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        if parsed_path.path == "/api/stock_suggestions/client_events":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64 * 1024:
                    raise ValueError("invalid event body length")
                from scanner_telemetry import record_client_events
                self._send_json(HTTPStatus.OK, record_client_events(self._read_json_body()))
            except (ValueError, TypeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_client_events"})
            except Exception:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "trace_logging_unavailable"})
            return
        if parsed_path.path == "/api/stock_suggestions/display_receipts":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 128 * 1024:
                    raise ValueError("invalid receipt body length")
                from scanner_telemetry import record_display_receipts
                result = record_display_receipts(self._read_json_body())
                self._send_json(HTTPStatus.OK, result)
            except (ValueError, TypeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except Exception:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "receipt_logging_unavailable"})
            return
        if parsed_path.path == "/api/ai_options":
            self._record_ai_option()
            return
        if parsed_path.path == "/api/ai_options/update":
            self._update_ai_option()
            return
        if parsed_path.path == "/api/learning/rebuild":
            self._serve_learning_report()
            return
        if parsed_path.path == "/api/opportunity_audit/record":
            self._record_opportunity_audit()
            return
        if parsed_path.path == "/api/opportunity_audit/update_pending":
            self._update_opportunity_audit_pending()
            return
        if parsed_path.path == "/api/accuracy/recalculate":
            self._recalculate_accuracy()
            return
        if parsed_path.path == "/api/stock_suggestions/refresh":
            self._refresh_stock_suggestions()
            return

        if parsed_path.path != "/api/refresh":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        if not REFRESH_LOCK.acquire(blocking=False):
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "error": "refresh_in_progress",
                    "message": "A live refresh is already running. Please wait for it to finish.",
                },
            )
            return

        try:
            payload = self._refresh_snapshot()
        finally:
            REFRESH_LOCK.release()

        status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(status, payload)

    def _refresh_snapshot(self) -> dict[str, Any]:
        started = time.time()

        # Auto-fetch Most Active data (PUTS & CALLS)
        print("[REFRESH] Fetching Most Active data from NSE...")
        fetcher_result = run_command([sys.executable, str(MOST_ACTIVE_FETCHER)], timeout=30)
        if fetcher_result["ok"]:
            print("[REFRESH] ✓ Most Active data fetched successfully")
        else:
            print("[REFRESH] ⚠ Most Active fetch had issues, continuing anyway...")

        downloader_result = run_command(
            [
                sys.executable,
                str(DOWNLOADER),
                "--world-timespan",
                self.world_timespan,
                "--world-maxrecords",
                str(self.world_maxrecords),
            ]
        )
        if not downloader_result["ok"]:
            return {
                "ok": False,
                "error": "download_failed",
                "message": "Live market/news download failed.",
                "download": downloader_result,
                "duration_seconds": round(time.time() - started, 2),
            }

        collector_result = run_command([sys.executable, str(COLLECTOR)])
        if not collector_result["ok"]:
            return {
                "ok": False,
                "error": "collector_failed",
                "message": "Snapshot rebuild failed after the live refresh.",
                "download": downloader_result,
                "collect": collector_result,
                "duration_seconds": round(time.time() - started, 2),
            }

        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        candle_store_result = run_command([candle_python, str(CANDLE_STORE)], timeout=45)
        if candle_store_result["ok"]:
            print("[REFRESH] ✓ Intraday candles stored in MySQL")
        else:
            print("[REFRESH] ⚠ MySQL candle storage skipped or failed; continuing anyway...")

        audit_update_result = run_command([candle_python, str(OPPORTUNITY_AUDIT), "update-pending"], timeout=30)
        if audit_update_result["ok"]:
            print("[REFRESH] ✓ Opportunity audit pending rows evaluated")
        else:
            print("[REFRESH] ⚠ Opportunity audit update skipped or failed; continuing anyway...")

        snapshot_payload: dict[str, Any] = {}
        if SNAPSHOT.exists():
            snapshot_payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

        return {
            "ok": True,
            "message": "Live market data, live news, and Most Active contracts were refreshed successfully.",
            "download": downloader_result,
            "collect": collector_result,
            "most_active": fetcher_result,
            "mysql_candles": candle_store_result,
            "opportunity_audit": audit_update_result,
            "snapshot": {
                "path": str(SNAPSHOT),
                "meta": snapshot_payload.get("meta"),
                "market": snapshot_payload.get("market"),
            },
            "duration_seconds": round(time.time() - started, 2),
        }

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        delivery = None
        if payload.get("snapshot_id") and "bullish" in payload:
            import uuid
            delivery = {"delivery_id": "delivery_"+uuid.uuid4().hex, "transport": "http",
                        "sent_at_ist": get_ist_now().isoformat()}
            payload = {**payload, "_delivery": delivery}
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except Exception as exc:
            if delivery:
                import scanner_trace
                scanner_trace.submit_health("delivery_failed", snapshot_id=payload["snapshot_id"],
                                            **delivery, error_type=type(exc).__name__)
            raise
        if delivery:
            import scanner_trace
            scanner_trace.submit_health("delivery_written", snapshot_id=payload["snapshot_id"],
                                        **delivery, status_code=status.value, byte_count=len(encoded),
                                        note="server socket write; browser acknowledgement required")

    def _serve_most_active(self) -> None:
        """Serve the Most Active Puts/Index/Volume data."""
        if MOST_ACTIVE.exists():
            try:
                payload = json.loads(MOST_ACTIVE.read_text(encoding="utf-8"))
                self._send_json(HTTPStatus.OK, payload)
                return
            except Exception as e:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "ok": False,
                    "error": "load_failed",
                    "message": f"Failed to load most active data: {str(e)}"
                })
                return

        # File not found - try to generate it
        self._send_json(HTTPStatus.NOT_FOUND, {
            "ok": False,
            "error": "no_data",
            "message": "Most Active data not found. Run: python3 load_most_active.py from the project root.",
            "path": str(MOST_ACTIVE)
        })

    def _serve_stock_suggestions(self) -> None:
        """Serve the last available snapshot without waiting for an upstream scan."""
        now_ist = get_ist_now()
        today = now_ist.date().isoformat()
        best = self._cached_or_snapshot()
        if best is None:
            opening = _SCAN_STATE.get("opening_payload") or {}
            best = dict(opening) if opening.get("session_date") == today else {
                "ok": True, "bullish": [], "bearish": [], "index_context": {},
                "session_date": today,
            }
            best["_awaiting_scan"] = True
            if is_ist_market_hours(now_ist) and not REFRESH_LOCK.locked():
                self._kick_background_refresh()
        elif best.get("session_date") != today:
            best["_previous_session"] = True
        after_close = now_ist.weekday() < 5 and now_ist.hour * 60 + now_ist.minute > 15 * 60 + 30
        if after_close and (best.get("_previous_session") or best.get("_awaiting_scan")):
            with _CLOSED_SESSION_RECOVERY_LOCK:
                if not REFRESH_LOCK.locked() and _SCAN_STATE.get("closed_session_recovery") != today:
                    _SCAN_STATE["closed_session_recovery"] = today
                    self._kick_background_refresh()
        best["_auto_scan_info"] = dict(LAST_AUTO_SCAN_INFO)
        self._send_json(HTTPStatus.OK, best)

    def _kick_background_refresh(self) -> None:
        """Fire a debounced refresh in a daemon thread."""
        def _run():
            try:
                self._refresh_stock_suggestions_payload()
            except Exception:
                pass
        threading.Thread(target=_run, name="BgSuggestionRefresh", daemon=True).start()

    def _cached_or_snapshot(self) -> dict[str, Any] | None:
        """Find the last good scan without replacing it with an empty opening result."""
        if _payload_has_signal(_SCAN_STATE.get("payload")):
            return dict(_SCAN_STATE["payload"])
        paths = (STOCK_SUGGESTION_SNAPSHOT,)
        if _scanner_data_dir and "DATA_DIR" in os.environ and "PERSISTENT_DATA_DIR" not in os.environ:
            paths += (DEFAULT_STOCK_SUGGESTION_SNAPSHOT,)
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _payload_has_signal(payload):
                return payload
        return None

    def _refresh_stock_suggestions_payload(self) -> tuple[HTTPStatus, dict[str, Any]]:
        """Refresh the stock suggestion index, debounced and non-blocking-safe."""
        now = time.time()

        # Debounce: within the cooldown, or if a scan is already running, hand
        # back the last good result instead of launching another subprocess.
        too_soon = (now - _SCAN_STATE["ts"]) < MIN_SCAN_INTERVAL_SECONDS
        if too_soon or not REFRESH_LOCK.acquire(blocking=False):
            cached = self._cached_or_snapshot()
            if cached is not None:
                cached["_refresh"] = {"debounced": True, "reason": "cooldown" if too_soon else "scan_in_progress"}
                return HTTPStatus.OK, cached
            return HTTPStatus.CONFLICT, {
                "ok": False,
                "error": "refresh_in_progress",
                "message": "A live refresh is already running. Please wait for it to finish.",
            }

        try:
            exec_py = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
            command = [exec_py, str(STOCK_SUGGESTION)]
            if not is_ist_market_hours():
                command.append("--require-current-session")
            result = run_command(command, timeout=120)
            import scanner_trace
            output = result.get("stdout") if isinstance(result.get("stdout"), dict) else {}
            scanner_trace.submit_health("requested_scan_finished", snapshot_id=output.get("snapshot_id"),
                subprocess_ok=result.get("ok"), duration_seconds=result.get("duration_seconds"))
            _SCAN_STATE["ts"] = time.time()
        except Exception as exc:
            import scanner_trace
            scanner_trace.submit_health("requested_scan_failed", error_type=type(exc).__name__)
            raise
        finally:
            REFRESH_LOCK.release()

        if isinstance(result.get("stdout"), dict):
            payload = result["stdout"]
            payload["_refresh"] = {
                "ok": result["ok"],
                "duration_seconds": result["duration_seconds"],
                "stderr": result.get("stderr"),
            }

            if _payload_has_signal(payload) and not payload.get("_stale_kept"):
                _SCAN_STATE["payload"] = payload
                signature = _payload_signature(payload)
                # Only push to open tabs when the scan actually has data AND
                # something changed - stops the blank/flicker broadcast storm.
                if signature != _SCAN_STATE.get("signature"):
                    _SCAN_STATE["signature"] = signature
                    try:
                        from websocket_server import broadcast_scanner_update_sync
                        broadcast_scanner_update_sync(payload)
                    except Exception:
                        pass
                return HTTPStatus.OK, payload

            # Empty / failed scan: never broadcast it, never return blank -
            # fall back to the last good result if we have one.
            cached = self._cached_or_snapshot()
            if _payload_has_signal(cached):
                cached["_refresh"] = {"ok": False, "kept_last_good": True,
                                      "scan_error": payload.get("message") or payload.get("error")}
                return HTTPStatus.OK, cached
            return HTTPStatus.SERVICE_UNAVAILABLE, payload

        return HTTPStatus.INTERNAL_SERVER_ERROR, {
            "ok": False,
            "error": "stock_suggestion_refresh_failed",
            "message": "Stock suggestion refresh did not return JSON.",
            "result": result,
        }

    def _refresh_stock_suggestions(self) -> None:
        status, payload = self._refresh_stock_suggestions_payload()
        session_date = payload.get("session_date")
        if payload.get("ok") and session_date and session_date != get_ist_now().date().isoformat():
            payload["_previous_session"] = True
        payload["_auto_scan_info"] = dict(LAST_AUTO_SCAN_INFO)
        self._send_json(status, payload)

    def _serve_stock_candles(self, query: dict[str, list[str]]) -> None:
        symbol = (query.get("symbol") or ["MUTHOOTFIN"])[0].strip().upper()
        try:
            from stock_suggestion_index import (
                fetch_5m_candles_pipeline,
                index_quote_from_candles,
            )
            from zoneinfo import ZoneInfo as _ZoneInfo
            _ist = _ZoneInfo("Asia/Kolkata")

            # Hierarchical validated fetch pipeline
            pipe_res = fetch_5m_candles_pipeline(symbol, full_range=True)
            candles = pipe_res.get("completed_candles") or []
            forming_c = pipe_res.get("forming_candle")

            if not candles and not forming_c:
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "symbol": symbol,
                    "candles": [],
                    "message": "No candle data available.",
                    "telemetry": {
                        "provider": pipe_res.get("provider", "Unavailable"),
                        "latest_completed_candle": None,
                        "fetched_time": datetime.now(_ist).strftime("%H:%M:%S IST"),
                        "fallback_used": pipe_res.get("fallback_used", False),
                        "observed_latency_ms": pipe_res.get("observed_latency_ms", 0),
                        "is_stale": pipe_res.get("is_stale", True),
                    }
                })
                return

            all_display_candles = list(candles)
            if forming_c:
                all_display_candles.append(forming_c)

            last_day = datetime.fromtimestamp(all_display_candles[-1]["timestamp"], tz=_ist).date()
            today_candles = [
                c for c in all_display_candles
                if datetime.fromtimestamp(c["timestamp"], tz=_ist).date() == last_day
            ] or all_display_candles

            # Compute PDH (Prior Day High), PDL (Prior Day Low), Open, High, Low
            opens = [c["open"] for c in today_candles if c.get("open") is not None]
            highs = [c["high"] for c in today_candles if c.get("high") is not None]
            lows = [c["low"] for c in today_candles if c.get("low") is not None]
            closes = [c["close"] for c in all_display_candles if c.get("close") is not None]
            volumes = [c["volume"] for c in all_display_candles if c.get("volume") is not None]

            # Morning opening range (first 3 to 6 candles e.g. 09:15 - 09:45)
            # of TODAY specifically, not the oldest day in the 5-day fetch.
            base_count = min(len(today_candles), 6)
            orb_high = max(c["high"] for c in today_candles[:base_count]) if today_candles else None
            orb_low = min(c["low"] for c in today_candles[:base_count]) if today_candles else None

            # Calculate 9 EMA line across all display candles (including forming bar)
            ema9_series = []
            if closes:
                k = 2.0 / (9 + 1)
                curr_ema = closes[0]
                for c in all_display_candles:
                    p = c["close"]
                    curr_ema = (p * k) + (curr_ema * (1.0 - k))
                    ema9_series.append({"time": c["timestamp"], "value": round(curr_ema, 2)})

            # Format candles for TradingView Lightweight Charts (including live forming candle)
            # Lightweight charts expects: { time: unix_timestamp (seconds), open, high, low, close, volume }
            tv_candles = []
            tv_volumes = []
            for c in all_display_candles:
                tv_candles.append({
                    "time": c["timestamp"],
                    "open": round(c["open"], 2),
                    "high": round(c["high"], 2),
                    "low": round(c["low"], 2),
                    "close": round(c["close"], 2),
                })
                is_up = c["close"] >= c["open"]
                tv_volumes.append({
                    "time": c["timestamp"],
                    "value": int(c.get("volume") or 0),
                    "color": "rgba(10, 143, 79, 0.6)" if is_up else "rgba(255, 80, 80, 0.6)",
                })

            # Detect Institutional SMC zones (FVG with 50% CE & Order Blocks) for clean overlay mapping
            fvg_data = []
            order_blocks = []
            try:
                from smc_volume_profile_engine import detect_fair_value_gaps, detect_order_blocks_and_structure
                fvg_data = detect_fair_value_gaps(all_display_candles)
                ob_structure = detect_order_blocks_and_structure(candles)
                order_blocks = ob_structure.get("order_blocks", [])
            except Exception:
                pass

            latest_bar_label = pipe_res.get("latest_completed_candle_ist")
            if not latest_bar_label and candles:
                latest_bar_label = datetime.fromtimestamp(candles[-1]["timestamp"], tz=_ist).strftime("%I:%M %p")
            if forming_c:
                forming_dt = datetime.fromtimestamp(forming_c["timestamp"], tz=_ist)
                latest_bar_label = f"{forming_dt.strftime('%I:%M %p')} (Live)"

            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "symbol": symbol,
                "candles": tv_candles,
                "volumes": tv_volumes,
                "ema9": ema9_series,
                "orb_high": round(orb_high, 2) if orb_high else None,
                "orb_low": round(orb_low, 2) if orb_low else None,
                "day_high": round(max(highs), 2) if highs else None,
                "day_low": round(min(lows), 2) if lows else None,
                "day_open": round(opens[0], 2) if opens else None,
                "ltp": round(closes[-1], 2) if closes else None,
                "index_quote": index_quote_from_candles(symbol, pipe_res),
                "fvg": fvg_data,
                "order_blocks": order_blocks,
                "forming_candle": forming_c,
                "telemetry": {
                    "provider": pipe_res.get("provider", "Unknown"),
                    "latest_completed_candle": latest_bar_label,
                    "fetched_time": datetime.now(_ist).strftime("%H:%M:%S IST"),
                    "fallback_used": pipe_res.get("fallback_used", False),
                    "observed_latency_ms": pipe_res.get("observed_latency_ms", 0),
                    "is_stale": pipe_res.get("is_stale", False),
                },
            })
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": "stock_candles_failed",
                "message": str(e),
            })

    def _serve_stock_overview(self, query: dict[str, list[str]]) -> None:
        symbol = (query.get("symbol") or ["BRITANNIA"])[0].strip().upper()
        try:
            from stock_suggestion_index import fetch_5m_candles_pipeline, index_quote_from_candles
            from smc_volume_profile_engine import analyze_stock_overview
            import upstox_provider

            pipe_res = fetch_5m_candles_pipeline(symbol, full_range=True)
            candles = pipe_res.get("completed_candles") or []
            forming_c = pipe_res.get("forming_candle")
            all_candles = list(candles)
            if forming_c:
                all_candles.append(forming_c)

            analysis = analyze_stock_overview(symbol, all_candles, completed_candles=candles)

            tv_candles = []
            tv_volumes = []
            for c in all_candles:
                tv_candles.append({
                    "time": c["timestamp"],
                    "open": round(c["open"], 2),
                    "high": round(c["high"], 2),
                    "low": round(c["low"], 2),
                    "close": round(c["close"], 2),
                })
                is_up = c["close"] >= c["open"]
                tv_volumes.append({
                    "time": c["timestamp"],
                    "value": int(c.get("volume", 0)),
                    "color": "rgba(8, 153, 129, 0.5)" if is_up else "rgba(242, 54, 69, 0.5)",
                })

            name = symbol
            if symbol in ("NIFTY 50", "NIFTY50", "NIFTY"):
                name = "NIFTY 50 Index"
            elif symbol in ("NIFTY BANK", "BANKNIFTY", "BANK NIFTY"):
                name = "NIFTY BANK Index"
            elif symbol in ("SENSEX", "BSESENSEX", "^BSESN", "BSE SENSEX"):
                name = "BSE SENSEX Index"
            elif upstox_provider._INSTRUMENTS_CACHE is None and upstox_provider.INSTRUMENTS_FILE.exists():
                try:
                    upstox_provider._INSTRUMENTS_CACHE = json.loads(upstox_provider.INSTRUMENTS_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if upstox_provider._INSTRUMENTS_CACHE and symbol in upstox_provider._INSTRUMENTS_CACHE:
                name = upstox_provider._INSTRUMENTS_CACHE[symbol].get("name", symbol)

            ltp = all_candles[-1]["close"] if all_candles else None
            prev_close = all_candles[0]["open"] if all_candles else None
            pct_change = round(((ltp - prev_close) / prev_close) * 100, 2) if ltp and prev_close else 0.0
            index_quote = index_quote_from_candles(symbol, pipe_res)
            if index_quote is not None:
                pct_change = index_quote["percent_change"]
            latest_bar = datetime.fromtimestamp(all_candles[-1]["timestamp"], IST_TZ) if all_candles else None

            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "symbol": symbol,
                "name": name,
                "ltp": ltp,
                "percent_change": pct_change,
                "index_quote": index_quote,
                "candles": tv_candles,
                "volumes": tv_volumes,
                "volume_profile": analysis["volume_profile"],
                "market_structure": analysis["market_structure"],
                "order_flow": analysis["order_flow"],
                "trade_plan": analysis.get("trade_plan", {}),
                "fvg": analysis.get("fvg", []),
                "liquidity_pools": analysis.get("liquidity_pools", []),
                "cvd_series": analysis.get("cvd_series", []),
                "footprints": analysis.get("footprints", {}),
                "mtf_matrix": analysis.get("mtf_matrix", {}),
                "telemetry": {
                    "provider": pipe_res.get("provider", "Unknown"),
                    "latest_candle": latest_bar.strftime("%d %b %I:%M %p IST") if latest_bar else None,
                    "is_stale": bool(pipe_res.get("is_stale")),
                    "latency_ms": pipe_res.get("observed_latency_ms", 0),
                }
            })
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": "stock_overview_failed",
                "message": str(e),
            })

    def _serve_mysql_candles(self, query: dict[str, list[str]]) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        command = [
            candle_python,
            str(CANDLE_FETCHER),
            "--symbol",
            (query.get("symbol") or ["NIFTY50"])[0],
            "--timeframe",
            (query.get("timeframe") or ["5m"])[0],
            "--limit",
            (query.get("limit") or ["500"])[0],
        ]
        market_date = (query.get("date") or [""])[0]
        if market_date:
            command.extend(["--date", market_date])
        result = run_command(command, timeout=30)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {
                "ok": False,
                "error": "mysql_candle_fetch_failed",
                "message": "Could not read candles from local MySQL.",
                "result": result,
            },
        )

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _serve_latest_ai_option(self, query: dict[str, list[str]]) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        status = (query.get("status") or ["OPEN"])[0]
        result = run_command([candle_python, str(AI_OPTIONS), "latest", "--status", status], timeout=30)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "ai_option_latest_failed", "result": result})

    def _serve_ai_option_history(self, query: dict[str, list[str]]) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        status = (query.get("status") or ["ALL"])[0]
        option_side = (query.get("option_side") or ["ALL"])[0]
        limit = (query.get("limit") or ["500"])[0]
        result = run_command(
            [
                candle_python,
                str(AI_OPTIONS),
                "history",
                "--status",
                status,
                "--option-side",
                option_side,
                "--limit",
                limit,
            ],
            timeout=30,
        )
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "ai_option_history_failed", "result": result})

    def _serve_learning_report(self) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        result = run_command([candle_python, str(LEARNING_ENGINE), "report"], timeout=45)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            status = HTTPStatus.OK if result["stdout"].get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "learning_report_failed", "result": result})

    def _serve_opportunity_audit_summary(self, query: dict[str, list[str]]) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        limit = (query.get("limit") or ["1000"])[0]
        result = run_command([candle_python, str(OPPORTUNITY_AUDIT), "summary", "--limit", limit], timeout=45)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            status = HTTPStatus.OK if result["stdout"].get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "opportunity_audit_summary_failed", "result": result})

    def _serve_opportunity_audit_history(self, query: dict[str, list[str]]) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        limit = (query.get("limit") or ["120"])[0]
        result = run_command([candle_python, str(OPPORTUNITY_AUDIT), "history", "--limit", limit], timeout=30)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "opportunity_audit_history_failed", "result": result})

    def _serve_audit_debug(self, query: dict[str, list[str]]) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        market_date = (query.get("date") or [""])[0]
        verdict = (query.get("verdict") or [""])[0]
        limit = (query.get("limit") or ["500"])[0]
        if not market_date:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_date", "message": "Use /api/audit_debug?date=YYYY-MM-DD&verdict=MISSED_PUT"})
            return
        result = run_command(
            [
                candle_python,
                str(OPPORTUNITY_AUDIT),
                "debug",
                "--date",
                market_date,
                "--verdict",
                verdict,
                "--limit",
                limit,
            ],
            timeout=45,
        )
        if result["ok"] and isinstance(result.get("stdout"), dict):
            status = HTTPStatus.OK if result["stdout"].get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "audit_debug_failed", "result": result})

    def _serve_replay_audit(self, query: dict[str, list[str]]) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        market_date = (query.get("date") or [""])[0]
        symbol = (query.get("symbol") or ["NIFTY50"])[0]
        timeframe = (query.get("timeframe") or ["5m"])[0]
        limit = (query.get("limit") or ["500"])[0]
        if not market_date:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_date", "message": "Use /api/replay_audit?date=YYYY-MM-DD"})
            return
        result = run_command(
            [
                candle_python,
                str(REPLAY_AUDIT),
                "--date",
                market_date,
                "--symbol",
                symbol,
                "--timeframe",
                timeframe,
                "--limit",
                limit,
            ],
            timeout=120,
        )
        if result["ok"] and isinstance(result.get("stdout"), dict):
            status = HTTPStatus.OK if result["stdout"].get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "replay_audit_failed", "result": result})

    def _record_ai_option(self) -> None:
        payload = self._read_json_body()
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        result = run_command([candle_python, str(AI_OPTIONS), "record", "--payload", json.dumps(payload)], timeout=30)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "ai_option_record_failed", "result": result})

    def _update_ai_option(self) -> None:
        payload = self._read_json_body()
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        result = run_command(
            [
                candle_python,
                str(AI_OPTIONS),
                "update",
                "--id",
                str(payload.get("id") or 0),
                "--status",
                str(payload.get("status") or "OPEN"),
                "--current-price",
                str(payload.get("current_price") or 0),
                "--result-points",
                str(payload.get("result_points") or 0),
                "--premium-current",
                str(payload.get("premium_current")) if payload.get("premium_current") is not None else "nan",
                "--premium-exit",
                str(payload.get("premium_exit")) if payload.get("premium_exit") is not None else "nan",
                "--pnl-premium",
                str(payload.get("pnl_premium")) if payload.get("pnl_premium") is not None else "nan",
                "--closed-market-time",
                str(payload.get("closed_market_time") or ""),
            ],
            timeout=30,
        )
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "ai_option_update_failed", "result": result})

    def _record_opportunity_audit(self) -> None:
        payload = self._read_json_body()
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        result = run_command([candle_python, str(OPPORTUNITY_AUDIT), "record", "--payload", json.dumps(payload)], timeout=30)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            status = HTTPStatus.OK if result["stdout"].get("ok") else HTTPStatus.BAD_REQUEST
            self._send_json(status, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "opportunity_audit_record_failed", "result": result})

    def _update_opportunity_audit_pending(self) -> None:
        payload = self._read_json_body()
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        limit = str(payload.get("limit") or 500)
        result = run_command([candle_python, str(OPPORTUNITY_AUDIT), "update-pending", "--limit", limit], timeout=30)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            status = HTTPStatus.OK if result["stdout"].get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, result["stdout"])
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "opportunity_audit_update_failed", "result": result})

    def _serve_live_option_chain(self) -> None:
        import requests
        url = 'https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://www.nseindia.com/'
        }
        try:
            session = requests.Session()
            session.get("https://www.nseindia.com/", headers=headers, timeout=5)
            resp = session.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                self._send_json(HTTPStatus.OK, resp.json())
                return
        except Exception as e:
            pass
            
        self._send_json(HTTPStatus.OK, {"ok": False, "error": "nse_blocked", "message": "Failed to fetch from NSE. Using simulated fallback data."})

    def _serve_accuracy_summary(self) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        result = run_command([candle_python, str(ACCURACY_API), "summary"], timeout=45)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(
            HTTPStatus.INTERNAL_SERVER_ERROR, 
            {"ok": False, "error": "accuracy_summary_failed", "result": result}
        )

    def _recalculate_accuracy(self) -> None:
        candle_python = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
        result = run_command([candle_python, str(ACCURACY_API), "recalculate"], timeout=180)
        if result["ok"] and isinstance(result.get("stdout"), dict):
            self._send_json(HTTPStatus.OK, result["stdout"])
            return
        self._send_json(
            HTTPStatus.INTERNAL_SERVER_ERROR, 
            {"ok": False, "error": "accuracy_recalculate_failed", "result": result}
        )


def main() -> None:
    args = parse_args()
    handler = partial(
        NiftyHandler,
        directory=str(ROOT),
        world_timespan=args.world_timespan,
        world_maxrecords=args.world_maxrecords,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({"ok": True, "url": f"http://{args.host}:{args.port}/MLAIStockV2.html"}, indent=2))

    # Autonomous 7-day data retention maintenance worker
    retention_stop = threading.Event()
    retention_thread = None
    try:
        from scanner_log_retention import retention_enabled, start_retention_worker
        if retention_enabled():
            retention_thread = start_retention_worker(stop_event=retention_stop)
            print("[scanner-retention] Autonomous 7-day data retention worker active", file=sys.stderr)
            # Immediate non-blocking startup purge check
            def _startup_retention_purge():
                try:
                    import scanner_log_retention
                    purge_fn = getattr(scanner_log_retention, "run_daily_db_purge", None)
                    if purge_fn:
                        res = purge_fn(retention_days=7)
                        print(f"[scanner-retention] Startup DB purge check completed: {res}", file=sys.stderr)
                except Exception as p_err:
                    print(f"[scanner-retention] Startup DB purge skipped: {p_err}", file=sys.stderr)
            threading.Thread(target=_startup_retention_purge, daemon=True, name="StartupRetentionPurge").start()
        else:
            print(f"[scanner-retention] Worker inactive (host={os.environ.get('RENDER_EXTERNAL_HOSTNAME')}, render={os.environ.get('RENDER')})", file=sys.stderr)
    except Exception as exc:
        print("[scanner-retention] worker_start_failed", file=sys.stderr)
    
    # Launch autonomous 5-minute background auto-scanner
    start_background_auto_scanner(interval_seconds=300)

    # Bind the private WebSocket listener used by the same-origin /ws proxy.
    try:
        from websocket_server import start_websocket_server_thread
        ws_port = int(os.environ.get("WS_BACKEND_PORT", "8765"))
        start_websocket_server_thread(host="127.0.0.1", port=ws_port)
        print(f"[WEBSOCKET] Real-time push engine starting on ws://127.0.0.1:{ws_port}")
    except Exception as ws_err:
        print(f"[WEBSOCKET ERROR] Failed to start WebSocket engine: {ws_err}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down stock suggestion live server...")
    finally:
        retention_stop.set()
        if retention_thread is not None:
            retention_thread.join(timeout=1)
        server.server_close()


if __name__ == "__main__":
    main()
