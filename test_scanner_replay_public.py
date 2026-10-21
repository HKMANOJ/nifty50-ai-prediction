"""Real loopback HTTP checks for the explicitly public, projected archive."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

import scanner_history_replay as replay
import serve_nifty50_app as app
import test_scanner_history_replay as fixtures


class PublicReplayHTTPTests(unittest.TestCase):
    def test_new_visitor_loads_all_replay_steps_without_key_cookie_or_login(self):
        class QuietHandler(app.NiftyHandler):
            def log_message(self, *args):
                pass

        # Synthetic forbidden fields must not appear even though the source
        # contains them. The HTTP server never connects to a real database.
        secret = "synthetic-private-value-not-for-public-response"
        events = [fixtures.decision("FIRST", rank=1, scanner_config={"secret": secret},
                                    source_path="/private/" + secret, client_session_id=secret,
                                    database_url="postgresql://owner:" + secret + "@example/db",
                                    replay_inputs={"secret": secret}, raw={"secret": secret}),
                  fixtures.decision("LOWER", rank=9, included=False, event_id=2),
                  fixtures.decision("REJECT", rank=None, qualified=False, included=False,
                                    reason="rally_gate_failed", event_id=3), fixtures.complete(3)]
        service = replay.ReplayService(replay.MemoryStore(events, source="neon_scanner_event_log"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        server.scanner_replay_service = service
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, extra_headers=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request(method, path, headers={"Host": "scanner.example", **(extra_headers or {})})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        config = {"SCANNER_REPLAY_ENABLED": "1", "SCANNER_REPLAY_ORIGIN": "https://scanner.example",
                  "SCANNER_REPLAY_READ_DATABASE_URL": "unused-synthetic-reader-url"}
        try:
            with patch.dict(os.environ, config, clear=True), \
                    patch.object(replay.PostgresStore, "_query", side_effect=AssertionError("No real database")):
                for path in ("/scanner_replay.html", "/scanner_replay.js", "/scanner_replay.css"):
                    status, headers, _ = request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertNotIn("Set-Cookie", headers)
                for path in ("/api/scanner_replay/sessions",
                             "/api/scanner_replay/snapshots?date=" + fixtures.DAY,
                             "/api/scanner_replay/snapshot?date=" + fixtures.DAY + "&snapshot_id=" + fixtures.SCAN):
                    status, headers, body = request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(body)["ok"])
                    self.assertNotIn("Set-Cookie", headers)
                    self.assertNotIn("Access-Control-Allow-Origin", headers)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertNotIn(secret, body.decode())
                    for forbidden in ("replay_inputs", "scanner_config", "client_session_id", "source_path", "database_url"):
                        self.assertNotIn(forbidden, body.decode())
                rows = json.loads(body)["bullish"]
                self.assertEqual([row["rank"] for row in rows], [1, 9, None])
                self.assertEqual(rows[-1]["qualified"], False)
                # A formerly configured key must not reintroduce prompts.
                os.environ["SCANNER_REPLAY_TOKEN"] = "unused-legacy-token"
                self.assertEqual(request("GET", "/api/scanner_replay/sessions")[0], 200)
                for path in ("/api/scanner_replay/unlock", "/api/scanner_replay/lock",
                             "/api/scanner_replay/sessions"):
                    self.assertEqual(request("POST", path)[0], 405)
                for path in ("/.env", "/scanner_history_replay.py", "/inputs/scanner_telemetry/raw.jsonl"):
                    self.assertEqual(request("GET", path)[0], 404)
                os.environ["SCANNER_REPLAY_ENABLED"] = "0"
                self.assertEqual(request("GET", "/api/scanner_replay/sessions")[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
