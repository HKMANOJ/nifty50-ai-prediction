"""Rollback regression tests: synthetic localhost files only, never a database."""
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import serve_nifty50_app as app


class QuietHandler(app.NiftyHandler):
    def log_message(self, *args):
        pass


class RollbackFilesTests(unittest.TestCase):
    def test_dashboard_has_no_production_replay_link(self):
        html = (app.ROOT / "MLAIStockV2.html").read_text()
        self.assertNotIn("scanner_replay.html", html)
        self.assertIn('id="refreshBtn"', html)

    def test_replay_assets_modules_and_routes_are_removed_but_retention_remains(self):
        for name in ("scanner_history_replay.py", "scanner_history_replay_routes.py",
                     "scanner_replay.html", "scanner_replay.js", "scanner_replay.css"):
            with self.subTest(name=name):
                self.assertFalse((app.ROOT / name).exists())
        source = Path(app.__file__).read_text()
        self.assertNotIn("scanner_history_replay", source)
        self.assertIn("start_retention_worker", source)
        self.assertTrue((app.ROOT / "scanner_log_retention.py").is_file())


class RollbackHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        fixtures = {
            "MLAIStockV2.html": "<html>synthetic dashboard</html>",
            "app.css": "body{color:green}",
            "inputs/most_active.latest.json": '{"synthetic":true}',
            ".env": "PRIVATE_TEST_VALUE",
            ".git/config": "PRIVATE_TEST_VALUE",
            "scripts/secret.py": "PRIVATE_TEST_VALUE",
            "scripts/secret.PY": "PRIVATE_TEST_VALUE",
            "inputs/scanner_telemetry/private.jsonl": "PRIVATE_TEST_VALUE",
            "venv/private.txt": "PRIVATE_TEST_VALUE",
            "__pycache__/secret.pyc": "PRIVATE_TEST_VALUE",
        }
        for relative, content in fixtures.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET"):
        request = urllib.request.Request(self.origin + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as response:
            try:
                return response.code, response.read()
            finally:
                response.close()

    def test_existing_private_file_protection_survives_for_get_and_head(self):
        paths = ("/.env", "/%2eenv", "/.git/config", "/%2e%2e/.env",
                 "/scripts/secret.py", "/scripts/secret.PY",
                 "/inputs/scanner_telemetry/private.jsonl",
                 "/inputs/%73canner_telemetry/private.jsonl",
                 "/venv/private.txt", "/__pycache__/secret.pyc")
        for method in ("GET", "HEAD"):
            for path in paths:
                with self.subTest(method=method, path=path):
                    status, body = self.request(path, method)
                    self.assertEqual(status, 404)
                    self.assertNotIn(b"PRIVATE_TEST_VALUE", body)
                    if method == "HEAD":
                        self.assertEqual(body, b"")

    def test_old_replay_pages_and_apis_are_unavailable_without_running_scanner(self):
        paths = ("/scanner_replay.html", "/scanner_replay.js", "/scanner_replay.css",
                 "/api/scanner_replay/sessions",
                 "/api/scanner_replay/snapshots?date=2026-10-22",
                 "/api/scanner_replay/snapshot?date=2026-10-22&snapshot_id=scan_" + "a" * 32)
        with patch.object(app, "run_command", side_effect=AssertionError("No scanner work")) as run:
            for method in ("GET", "HEAD"):
                for path in paths:
                    with self.subTest(method=method, path=path):
                        self.assertEqual(self.request(path, method)[0], 404)
            self.assertEqual(self.request("/api/scanner_replay/unlock", "POST")[0], 404)
            run.assert_not_called()

    def test_dashboard_public_inputs_and_health_still_work(self):
        for path in ("/MLAIStockV2.html", "/app.css", "/inputs/most_active.latest.json", "/api/health"):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
        status, body = self.request("/MLAIStockV2.html", "HEAD")
        self.assertEqual((status, body), (200, b""))


if __name__ == "__main__":
    unittest.main()
