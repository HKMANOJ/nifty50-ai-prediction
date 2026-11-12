"""Server lifecycle wiring only: never start scanners, threads or database work."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import serve_nifty50_app as app


class RetentionServerTests(unittest.TestCase):
    def run_main(self, enabled=True, fail_start=False):
        args = SimpleNamespace(host="127.0.0.1", port=0,
                               world_timespan="1d", world_maxrecords=1)
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        thread = Mock()
        worker = Mock(return_value=thread)
        if fail_start:
            worker.side_effect = RuntimeError("private connection details")
        policy = Mock(return_value=enabled)
        fake_retention = SimpleNamespace(retention_enabled=policy,
                                         start_retention_worker=worker)
        fake_websocket = SimpleNamespace(start_websocket_server_thread=Mock())
        with patch.object(app, "parse_args", return_value=args), \
             patch.object(app, "ThreadingHTTPServer", return_value=server), \
             patch.object(app, "start_background_auto_scanner") as scanner, \
             patch.dict("sys.modules", {"scanner_log_retention": fake_retention,
                                        "websocket_server": fake_websocket}), \
             patch("builtins.print") as output:
            app.main()
        return policy, worker, thread, server, scanner, output

    def test_enabled_worker_stops_with_server_without_blocking_shutdown(self):
        policy, worker, thread, server, scanner, _ = self.run_main()
        policy.assert_called_once_with()
        worker.assert_called_once()
        stop = worker.call_args.kwargs["stop_event"]
        self.assertTrue(stop.is_set())
        thread.join.assert_called_once_with(timeout=1)
        scanner.assert_called_once_with(interval_seconds=300)
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()

    def test_disabled_worker_does_not_start_or_interfere_with_scanner(self):
        _, worker, thread, server, scanner, _ = self.run_main(enabled=False)
        worker.assert_not_called()
        thread.join.assert_not_called()
        scanner.assert_called_once()
        server.server_close.assert_called_once()

    def test_maintenance_start_failure_does_not_stop_server_or_leak_exception(self):
        _, _, thread, server, scanner, output = self.run_main(fail_start=True)
        thread.join.assert_not_called()
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()
        scanner.assert_called_once()
        rendered = " ".join(str(call.args) for call in output.call_args_list)
        self.assertIn("worker_start_failed", rendered)
        self.assertNotIn("private connection details", rendered)


if __name__ == "__main__":
    unittest.main()
