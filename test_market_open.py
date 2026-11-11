import json
import unittest
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import serve_nifty50_app as app
import stock_suggestion_index as scanner


class MarketOpenTests(unittest.TestCase):
    def test_premarket_reset_keeps_previous_snapshot_for_open(self):
        previous = {"ok": True, "session_date": "2026-11-11",
                    "bullish": [{"symbol": "FORCEMOT"}], "bearish": []}
        warmup = datetime(2026, 11, 12, 9, 12, tzinfo=app.IST_TZ)
        opening = warmup.replace(minute=15)
        reset = {"ok": True, "session_date": "2026-11-12", "bullish": [], "bearish": []}
        handler = app.NiftyHandler.__new__(app.NiftyHandler)
        handler._send_json = Mock()
        handler._kick_background_refresh = Mock()

        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(previous))
            with (
                patch.object(app, "STOCK_SUGGESTION_SNAPSHOT", path),
                patch.object(app, "_scanner_data_dir", None),
                patch.object(app, "_SCAN_STATE", {"payload": None}),
                patch.object(app, "LAST_AUTO_SCAN_INFO", {}),
                patch.object(app, "_premarket_reset_date", None),
                patch.object(app, "run_command", return_value={"stdout": reset}),
                patch("scanner_log_retention.retention_enabled", return_value=False),
                patch.object(app, "get_ist_now", return_value=opening),
            ):
                app._run_premarket_reset_if_due(warmup)
                handler._serve_stock_suggestions()
                self.assertEqual(json.loads(path.read_text()), previous)
                status, snapshot = handler._send_json.call_args.args
                self.assertEqual(status, HTTPStatus.OK)
                self.assertTrue(snapshot["_previous_session"])
                self.assertEqual(snapshot["bullish"], previous["bullish"])
                handler._kick_background_refresh.assert_not_called()

    def test_cold_open_returns_status_without_waiting_for_the_scan(self):
        handler = app.NiftyHandler.__new__(app.NiftyHandler)
        handler._cached_or_snapshot = Mock(return_value=None)
        handler._send_json = Mock()
        handler._kick_background_refresh = Mock()
        with (
            patch.object(app, "_SCAN_STATE", {}),
            patch.object(app, "get_ist_now", return_value=datetime(2026, 11, 12, 9, 15, tzinfo=app.IST_TZ)),
        ):
            handler._serve_stock_suggestions()
        handler._kick_background_refresh.assert_called_once()
        status, snapshot = handler._send_json.call_args.args
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(snapshot["_awaiting_scan"])
        self.assertEqual(snapshot["session_date"], "2026-11-12")

    def test_sensex_quote_uses_previous_session_close(self):
        def bar(day, hour, minute, close):
            stamp = datetime(2026, 11, day, hour, minute, tzinfo=scanner.INDIA_TZ).timestamp()
            return {"timestamp": stamp, "close": close}

        history = [bar(10, 15, 20, 80000), bar(10, 15, 25, 81000), bar(11, 9, 15, 81500)]
        result = {"completed_candles": history, "forming_candle": bar(11, 9, 20, 81810), "provider": "Upstox"}
        official = {"data": [
            {"index": "NIFTY 50", "last": 25255.05},
            {"index": "NIFTY BANK", "last": 60792.4},
        ]}
        with (
            patch.object(scanner, "nse_get_json", return_value=official),
            patch.object(scanner, "fetch_5m_candles_pipeline", return_value=result) as candles,
        ):
            quotes, errors = scanner.fetch_index_context()
        candles.assert_called_once_with("SENSEX", full_range=True)
        self.assertEqual(errors, [])
        self.assertEqual(quotes["SENSEX"]["ltp"], 81810)
        self.assertEqual(quotes["SENSEX"]["percent_change"], 1.0)


if __name__ == "__main__":
    unittest.main()
