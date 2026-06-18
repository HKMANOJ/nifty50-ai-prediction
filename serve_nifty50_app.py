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


ROOT = Path(__file__).resolve().parent
DOWNLOADER = ROOT / "download_real_market_inputs.py"
COLLECTOR = ROOT / "collect_nifty50_market_data.py"
SNAPSHOT = ROOT / "market_snapshot.latest.json"
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
        if self.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "nifty50-live-server"})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/refresh":
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

        snapshot_payload: dict[str, Any] = {}
        if SNAPSHOT.exists():
            snapshot_payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

        return {
            "ok": True,
            "message": "Live market data, live news, and the normalized snapshot were refreshed successfully.",
            "download": downloader_result,
            "collect": collector_result,
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
