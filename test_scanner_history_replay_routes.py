"""Offline route safety and live-telemetry isolation. No sockets or DB writes."""
from email.message import Message
from io import BytesIO
import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse
import unittest
from unittest.mock import Mock, patch

import scanner_history_replay_routes as routes
import serve_nifty50_app as app

TOKEN = "only-a-synthetic-test-token-1234567890"
ORIGIN = "https://scanner.example"


class RouteTests(unittest.TestCase):
    def handler(self, path, *, preview=False, method="GET", host=None, origin=None,
                site=None, authorization=None, client="127.0.0.1"):
        h = object.__new__(app.NiftyHandler)
        h.path, h.command, h.request_version = path, method, "HTTP/1.1"
        h.requestline = method + " " + path + " HTTP/1.1"
        h.headers = Message()
        h.headers["Host"] = host or ("127.0.0.1:8003" if preview else "scanner.example")
        for key, value in (("Origin", origin), ("Sec-Fetch-Site", site), ("Authorization", authorization)):
            if value is not None:
                h.headers[key] = value
        h.client_address = (client, 1234)
        service = Mock()
        service.sessions.return_value = {"ok": True, "historical": True, "sessions": []}
        service.snapshots.return_value = {"ok": True, "historical": True, "snapshots": []}
        service.snapshot.return_value = {"ok": True, "historical": True, "snapshot_id": "scan_"+"a"*32, "bullish": []}
        h.server = SimpleNamespace(server_port=8003, scanner_replay_preview=preview, scanner_replay_service=service)
        h.rfile, h.wfile = BytesIO(), BytesIO()
        h.sent_headers, h.status = {}, None
        h.send_response = lambda status: (setattr(h, "status", status), setattr(h, "_headers_buffer", []))
        h.send_header = lambda name, value: h.sent_headers.__setitem__(name, value)
        h.log_message = lambda *args: None
        h._send_json = Mock(side_effect=AssertionError("History must not call the live delivery helper"))
        return h, service

    def run_get(self, path="/api/scanner_replay/sessions", *, env=None, **opts):
        h, service = self.handler(path, **opts)
        with patch.dict(os.environ, env or {}, clear=True):
            getattr(h, "do_"+h.command)()
        return h, service

    def production_env(self):
        return {"SCANNER_REPLAY_ENABLED": "1", "SCANNER_REPLAY_ORIGIN": ORIGIN,
                "SCANNER_REPLAY_TOKEN": TOKEN}

    def payload(self, h):
        return json.loads(h.wfile.getvalue().lstrip(b"\r\n"))

    def test_default_disabled_never_reads_archive(self):
        h, service = self.run_get()
        self.assertEqual(h.status, 404)
        self.assertEqual(self.payload(h)["error"], "replay_disabled")
        service.sessions.assert_not_called()

    def test_production_public_reads_need_no_token_cookie_or_login(self):
        for supplied in (None, "Bearer wrong", TOKEN):
            with self.subTest(supplied=supplied):
                h, service = self.run_get(env=self.production_env(), authorization=supplied)
                self.assertEqual(h.status, 200)
                service.sessions.assert_called_once()
                self.assertNotIn("Set-Cookie", h.sent_headers)
        env = self.production_env()
        del env["SCANNER_REPLAY_TOKEN"]
        h, service = self.run_get(env=env, origin=ORIGIN, site="same-origin")
        self.assertEqual(h.status, 200)
        service.sessions.assert_called_once()
        h._send_json.assert_not_called()

    def test_loopback_alone_cannot_bypass_production_origin_configuration(self):
        h, _ = self.run_get(env=self.production_env(), host="127.0.0.1:8003")
        self.assertEqual(h.status, 503)
        self.assertEqual(self.payload(h)["error"], "replay_origin_not_configured")

    def test_local_preview_is_explicit_and_loopback_only(self):
        h, service = self.run_get(preview=True)
        self.assertEqual(h.status, 200)
        service.sessions.assert_called_once()
        for opts in ({"host":"evil.example"}, {"origin":"https://evil.example"},
                     {"client":"10.0.0.4"}, {"site":"cross-site"}, {"site":"same-site"}):
            with self.subTest(opts=opts):
                h, service = self.run_get(preview=True, **opts)
                self.assertEqual(h.status, 403)
                service.sessions.assert_not_called()

    def test_duplicate_host_headers_rejected(self):
        h, service = self.handler("/api/scanner_replay/sessions")
        h.headers["Host"] = "evil.example"
        with patch.dict(os.environ, self.production_env(), clear=True):
            h.do_GET()
        self.assertEqual(h.status, 403)
        service.sessions.assert_not_called()

    def test_public_reads_still_check_origin_and_fetch_site(self):
        for opts in ({"origin": "https://evil.example"}, {"site": "cross-site"},
                     {"site": "same-site"}):
            with self.subTest(opts=opts):
                h, service = self.run_get(env=self.production_env(), **opts)
                self.assertEqual(h.status, 403)
                service.sessions.assert_not_called()

    def test_no_wildcard_cors_and_response_flag_resets(self):
        h, _ = self.run_get(preview=True)
        self.assertNotIn("Access-Control-Allow-Origin", h.sent_headers)
        self.assertEqual(h.sent_headers["Cache-Control"], "no-store")
        self.assertEqual(h.sent_headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("connect-src 'self'", h.sent_headers["Content-Security-Policy"])
        self.assertFalse(h._history_replay_response)
        h._headers_buffer = []
        h.end_headers()
        self.assertEqual(h.sent_headers["Access-Control-Allow-Origin"], "*")

    def test_snapshot_get_never_creates_live_delivery_receipt(self):
        sid="scan_"+"a"*32
        h, service = self.run_get("/api/scanner_replay/snapshot?date=2026-10-21&snapshot_id="+sid, preview=True)
        self.assertEqual(h.status, 200)
        self.assertNotIn("_delivery", self.payload(h))
        h._send_json.assert_not_called()
        service.snapshot.assert_called_once_with("2026-10-21", sid)

    def test_write_and_cors_preflight_rejected_without_service(self):
        for method in ("POST", "OPTIONS"):
            with self.subTest(method=method):
                h, service = self.run_get(preview=True, method=method)
                self.assertEqual(h.status, 405)
                self.assertEqual(self.payload(h)["error"], "replay_read_only")
                service.sessions.assert_not_called()
                self.assertNotIn("Access-Control-Allow-Origin", h.sent_headers)

    def test_head_uses_same_guard_and_does_not_write_body(self):
        for path, preview, status in (("/scanner_replay.html", False, 200),
                                      ("/scanner_replay%2ehtml", False, 200),
                                      ("/api/scanner_replay/sessions", False, 404),
                                      ("/api/scanner_replay/sessions", True, 200)):
            h, _ = self.run_get(path, method="HEAD", preview=preview)
            self.assertEqual(h.status, status)
            self.assertEqual(h.wfile.getvalue().strip(), b"")
            self.assertNotIn("Access-Control-Allow-Origin", h.sent_headers)

    def test_private_static_files_never_become_public_archive_get_or_head(self):
        paths=("/.env", "/%2eenv", "/.git/config", "/inputs/scanner_telemetry/2026-10-21.jsonl",
               "/inputs/scanner_telemetry/", "/inputs/%73canner_telemetry/x.json", "/foo/../inputs/scanner_telemetry/x",
               "/scanner_history_replay.py", "/venv/lib/site.py", "/__pycache__/x.pyc",
               "/%2finputs/scanner_telemetry/x.jsonl", "/INPUTS/SCANNER_TELEMETRY/x.jsonl")
        for path in paths:
            for method in ("GET", "HEAD"):
                with self.subTest(path=path,method=method):
                    h, service = self.run_get(path, method=method)
                    self.assertEqual(h.status, 404)
                    service.sessions.assert_not_called()
        h, _ = self.handler("/inputs/stock_suggestion_index.latest.json")
        self.assertFalse(routes.deny_private_static(h,urlparse(h.path)))

    def test_parameters_and_unexpected_routes_do_not_invoke_service(self):
        for path in ("/api/scanner_replay/sessions?date=x", "/api/scanner_replay/snapshot?date=x",
                     "/api/scanner_replay/snapshots?date=x&date=y", "/api/scanner_replay/private"):
            h, service = self.run_get(path, preview=True)
            self.assertEqual(h.status, 400)
            self.assertFalse(service.mock_calls)

    def test_generic_errors_do_not_expose_driver_details(self):
        h, service = self.handler("/api/scanner_replay/sessions", preview=True)
        service.sessions.side_effect = RuntimeError("password=secret path=/private")
        h.do_GET()
        self.assertEqual(h.status, 503)
        self.assertNotIn("secret", h.wfile.getvalue().decode())

    def test_public_archive_busy_fails_without_reading_source(self):
        slots = Mock()
        slots.acquire.return_value = False
        with patch.object(routes, "READ_SLOTS", slots):
            h, service = self.run_get(env=self.production_env())
        self.assertEqual(h.status, 503)
        self.assertEqual(self.payload(h)["error"], "replay_busy")
        self.assertFalse(service.mock_calls)
        slots.acquire.assert_called_once_with(blocking=False)
        slots.release.assert_not_called()

    def test_public_archive_slot_is_released_on_success_invalid_route_or_failure(self):
        for path, fail, expected in (("/api/scanner_replay/sessions", False, 200),
                                      ("/api/scanner_replay/unknown", False, 400),
                                      ("/api/scanner_replay/sessions", True, 503)):
            slots = Mock()
            slots.acquire.return_value = True
            h, service = self.handler(path)
            if fail:
                service.sessions.side_effect = RuntimeError("private driver detail")
            with patch.object(routes, "READ_SLOTS", slots), \
                    patch.dict(os.environ, self.production_env(), clear=True):
                h.do_GET()
            self.assertEqual(h.status, expected)
            slots.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
