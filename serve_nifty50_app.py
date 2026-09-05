#!/usr/bin/env python3
"""Serve the live market app with refresh APIs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from stock_suggestion_index import build_payload, error_payload
import time
from functools import partial
from datetime import datetime, timezone, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scan_clock import next_bar_close


ROOT = Path(__file__).resolve().parent

# Ensure local venv packages (e.g. websockets) are importable under any python3 invoker
import glob
for _sp in glob.glob(str(ROOT / "venv/lib/python*/site-packages")):
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

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
STOCK_SUGGESTION_SNAPSHOT = ROOT / "inputs" / "stock_suggestion_index.latest.json"
REFRESH_LOCK = threading.Lock()

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

IN_MEMORY_STOCK_SUGGESTIONS: dict[str, Any] | None = None
LAST_AUTO_SCAN_INFO: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 60,
    "last_run_ist": None,
    "last_duration_seconds": None,
    "status": "idle",
    "next_run_ist": None,
}

def start_background_auto_scanner(interval_seconds: int = 60) -> None:
    """Spawns an autonomous background daemon thread that rescans 200+ NSE stocks every 60s during market hours."""
    def _loop() -> None:
        time.sleep(5)  # Grace period on boot
        while True:
            try:
                now_ist = get_ist_now()
                if is_ist_market_hours(now_ist):
                    if REFRESH_LOCK.acquire(blocking=False):
                        LAST_AUTO_SCAN_INFO["status"] = "running"
                        scan_start = time.time()
                        print(f"[{now_ist.strftime('%H:%M:%S IST')}] [AUTO-SCANNER] Executing 5-minute background market scan...")
                        try:
                            global IN_MEMORY_STOCK_SUGGESTIONS
                            try:
                                payload = build_payload(20)
                            except Exception as exc:
                                payload = error_payload(exc)
                            
                            dur = round(time.time() - scan_start, 1)
                            is_ok = payload.get("ok", False)
                            
                            # Cache in memory and disk
                            IN_MEMORY_STOCK_SUGGESTIONS = payload
                            STOCK_SUGGESTION_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
                            STOCK_SUGGESTION_SNAPSHOT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                            
                            LAST_AUTO_SCAN_INFO["last_run_ist"] = get_ist_now().strftime("%I:%M:%S %p IST")
                            LAST_AUTO_SCAN_INFO["last_duration_seconds"] = dur
                            LAST_AUTO_SCAN_INFO["status"] = "success" if is_ok else "failed"
                            print(f"[{get_ist_now().strftime('%H:%M:%S IST')}] [AUTO-SCANNER] In-memory scan completed in {dur}s (ok={is_ok})")
                            
                            if is_ok:
                                try:
                                    from websocket_server import broadcast_scanner_update_sync
                                    broadcast_scanner_update_sync(payload)
                                except Exception as bc_err:
                                    print(f"[WS BROADCAST ERROR] {bc_err}")
                        except Exception as scan_err:
                            print(f"[AUTO-SCANNER ERROR] {scan_err}")
                            LAST_AUTO_SCAN_INFO["status"] = f"error: {scan_err}"
                        finally:
                            REFRESH_LOCK.release()
                    else:
                        print("[AUTO-SCANNER] Skipping interval: manual refresh or lock is currently active.")

                    now = get_ist_now()
                    target_time = next_bar_close(now, interval_minutes=5, offset_seconds=4)
                    sleep_gap = max(5.0, (target_time - get_ist_now()).total_seconds())
                    LAST_AUTO_SCAN_INFO["next_run_ist"] = target_time.strftime("%I:%M:%S %p IST")
                    time.sleep(sleep_gap)
                else:
                    LAST_AUTO_SCAN_INFO["status"] = "market_closed"
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

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed_path = urlparse(self.path)
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

        if parsed_path.path == "/api/stock_candles":
            self._serve_stock_candles(parse_qs(parsed_path.query))
            return

        if parsed_path.path == "/api/index_live":
            self._serve_index_live()
            return

        super().do_GET()

    def _serve_index_live(self) -> None:
        """Real-time NIFTY 50 & BANK NIFTY fetch from official NSE India API (matches Groww/Zerodha)."""
        result = {}
        try:
            from stock_suggestion_index import nse_get_json, NSE_BASE
            data = nse_get_json(f"{NSE_BASE}/api/allIndices", referer=f"{NSE_BASE}/market-data/live-equity-market")
            for item in data.get("data", []):
                idx = item.get("index")
                if idx in ("NIFTY 50", "NIFTY BANK"):
                    pct = float(item.get("percentChange", 0))
                    ltp = float(item.get("last", 0))
                    chg = float(item.get("variation", 0))
                    result[idx] = {
                        "name": idx,
                        "ltp": round(ltp, 2),
                        "percent_change": round(pct, 2),
                        "change": round(chg, 2),
                        "direction": "bullish" if pct >= 0 else "bearish",
                    }
        except Exception:
            pass

        # Fallback if NSE failed: try Yahoo Finance
        if not result or "NIFTY 50" not in result:
            import urllib.request as _ur
            for symbol, label in [("^NSEI", "NIFTY 50"), ("^NSEBANK", "NIFTY BANK")]:
                if label in result:
                    continue
                try:
                    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d"
                    req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with _ur.urlopen(req, timeout=6) as r:
                        data = json.loads(r.read())
                    meta = data["chart"]["result"][0]["meta"]
                    ltp = meta.get("regularMarketPrice") or 0
                    prev = meta.get("chartPreviousClose") or meta.get("previousClose") or ltp
                    pct = round(((ltp - prev) / prev) * 100, 2) if prev else 0
                    chg = round(ltp - prev, 2)
                    result[label] = {
                        "name": label,
                        "ltp": round(ltp, 2),
                        "percent_change": pct,
                        "change": chg,
                        "direction": "bullish" if pct >= 0 else "bearish",
                    }
                except Exception:
                    pass

        self._send_json(HTTPStatus.OK, {"ok": True, "index_context": result})


    def do_POST(self) -> None:
        parsed_path = urlparse(self.path)
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
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

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
        """Serve the latest real NSE intraday stock suggestion index."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        today_str = now_ist.date().isoformat()

        # If market is closed or weekend, serve cached snapshot (do not attempt live refresh against closed NSE)
        if not is_ist_market_hours(now_ist):
            if STOCK_SUGGESTION_SNAPSHOT.exists():
                try:
                    payload = json.loads(STOCK_SUGGESTION_SNAPSHOT.read_text(encoding="utf-8"))
                    payload["_auto_scan_info"] = LAST_AUTO_SCAN_INFO
                    self._send_json(HTTPStatus.OK, payload)
                    return
                except Exception:
                    pass

        global IN_MEMORY_STOCK_SUGGESTIONS
        
        # 1. Try In-Memory Cache First (Light Speed)
        payload = IN_MEMORY_STOCK_SUGGESTIONS
        
        # 2. Fallback to Disk if server just restarted
        if not payload and STOCK_SUGGESTION_SNAPSHOT.exists():
            try:
                payload = json.loads(STOCK_SUGGESTION_SNAPSHOT.read_text(encoding="utf-8"))
                IN_MEMORY_STOCK_SUGGESTIONS = payload
            except Exception:
                pass
                
        if payload:
            try:
                snapshot_date = payload.get("session_date")
                payload["_auto_scan_info"] = LAST_AUTO_SCAN_INFO
                if payload.get("ok") and snapshot_date == today_str:
                    self._send_json(HTTPStatus.OK, payload)
                    return
                status, refreshed = self._refresh_stock_suggestions_payload()
                refreshed["_auto_refresh_reason"] = "stale_session_auto_refreshed"
                refreshed["_auto_scan_info"] = LAST_AUTO_SCAN_INFO
                self._send_json(status, refreshed)
                return
            except Exception as e:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": "stock_suggestions_load_failed",
                        "message": f"Failed to load stock suggestion data: {str(e)}",
                    },
                )
                return

        status, payload = self._refresh_stock_suggestions_payload()
        payload["_auto_refresh_reason"] = "snapshot_missing"
        payload["_auto_scan_info"] = LAST_AUTO_SCAN_INFO
        self._send_json(status, payload)

    def _refresh_stock_suggestions_payload(self) -> tuple[HTTPStatus, dict]:
        if not REFRESH_LOCK.acquire(blocking=False):
            return HTTPStatus.CONFLICT, {
                "ok": False,
                "error": "refresh_in_progress",
                "message": "A live refresh is already running. Please wait for it to finish.",
            }
        try:
            global IN_MEMORY_STOCK_SUGGESTIONS
            try:
                import time
                start_t = time.time()
                payload = build_payload(20)
                dur = round(time.time() - start_t, 2)
            except Exception as exc:
                payload = error_payload(exc)
                dur = 0
            
            IN_MEMORY_STOCK_SUGGESTIONS = payload
            STOCK_SUGGESTION_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
            STOCK_SUGGESTION_SNAPSHOT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        finally:
            REFRESH_LOCK.release()

        payload["_refresh"] = {
            "ok": payload.get("ok", False),
            "duration_seconds": dur,
            "stderr": payload.get("error", "")
        }
        
        try:
            from websocket_server import broadcast_scanner_update_sync
            broadcast_scanner_update_sync(payload)
        except Exception as e:
            print(f"[WS] Manual refresh broadcast failed: {e}")
            
        return HTTPStatus.OK if payload.get("ok") else HTTPStatus.INTERNAL_SERVER_ERROR, payload

    def _refresh_stock_suggestions(self) -> None:
        status, payload = self._refresh_stock_suggestions_payload()
        self._send_json(status, payload)

    def _serve_stock_candles(self, query: dict[str, list[str]]) -> None:
        symbol = (query.get("symbol") or ["MUTHOOTFIN"])[0].strip().upper()
        try:
            from stock_suggestion_index import fetch_5m_candles_for_symbol
            candles = fetch_5m_candles_for_symbol(symbol, full_range=True)
            if not candles:
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "symbol": symbol,
                    "candles": [],
                    "message": "No candle data available."
                })
                return
            
            # Compute PDH (Prior Day High), PDL (Prior Day Low), Open, High, Low
            opens = [c["open"] for c in candles if c.get("open") is not None]
            highs = [c["high"] for c in candles if c.get("high") is not None]
            lows = [c["low"] for c in candles if c.get("low") is not None]
            closes = [c["close"] for c in candles if c.get("close") is not None]
            volumes = [c["volume"] for c in candles if c.get("volume") is not None]

            # Morning opening range (first 3 to 6 candles e.g. 09:15 - 09:45)
            base_count = min(len(candles), 6)
            orb_high = max(c["high"] for c in candles[:base_count]) if candles else None
            orb_low = min(c["low"] for c in candles[:base_count]) if candles else None

            # Calculate 9 EMA line
            ema9_series = []
            if closes:
                k = 2.0 / (9 + 1)
                curr_ema = closes[0]
                for c in candles:
                    p = c["close"]
                    curr_ema = (p * k) + (curr_ema * (1.0 - k))
                    ema9_series.append({"time": c["timestamp"], "value": round(curr_ema, 2)})

            # Format candles for TradingView Lightweight Charts
            # Lightweight charts expects: { time: unix_timestamp (seconds), open, high, low, close, volume }
            tv_candles = []
            tv_volumes = []
            for c in candles:
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
                    "value": int(c["volume"]),
                    "color": "rgba(10, 143, 79, 0.6)" if is_up else "rgba(255, 80, 80, 0.6)",
                })

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
            })
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": "stock_candles_failed",
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
    
    # Launch autonomous 60-second background auto-scanner
    start_background_auto_scanner(interval_seconds=60)

    # Launch Real-Time WebSocket Push Server on port 8765
    try:
        from websocket_server import start_websocket_server_thread
        start_websocket_server_thread(host="0.0.0.0", port=8765)
        print("[WEBSOCKET] Real-time push engine active on ws://127.0.0.1:8765")
    except Exception as ws_err:
        print(f"[WEBSOCKET ERROR] Failed to start WebSocket engine: {ws_err}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down stock suggestion live server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
