#!/usr/bin/env python3
"""Serve the NIFTY50 app with a one-click live refresh API."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
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
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
SNAPSHOT = ROOT / "market_snapshot.latest.json"
MOST_ACTIVE = ROOT / "inputs" / "most_active.latest.json"
REFRESH_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the NIFTY50 app with one-click live refresh.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="Host to bind. Default: 127.0.0.1")
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
    server_version = "Nifty50LiveServer/1.0"

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
        if parsed_path.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "nifty50-live-server"})
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

        super().do_GET()

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
    print(json.dumps({"ok": True, "url": f"http://{args.host}:{args.port}/nifty50_ai_prediction_console.html"}, indent=2))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down NIFTY50 live server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
