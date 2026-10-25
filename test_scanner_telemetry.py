"""Offline regression tests: no market fetches, production files or database writes."""
import json
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import scanner_telemetry as telemetry
import stock_suggestion_index as scanner
from serve_nifty50_app import NiftyHandler
ORIGINAL_WRITE = telemetry.write_events


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch_root = patch.object(telemetry, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.sink = patch.object(telemetry, "write_events")
        self.write = self.sink.start()
        self.addCleanup(self.sink.stop)
        self.recovery = patch.object(telemetry, "_restore_candidate_outcomes", return_value={})
        self.recovery.start()
        self.addCleanup(self.recovery.stop)
        self.day = datetime.now(telemetry.IST).date().isoformat()
        self.scan_id = telemetry.new_scan()
        self.sid = telemetry.signal_id(self.day, "TEST", "bullish", "Long buildup")
        telemetry._atomic_json(self.root / f"issued-{self.day}-{self.scan_id}.json", {
            "signals": {self.sid: {"symbol": "TEST", "side": "bullish"}},
            "prepared_at_ist": datetime.now(telemetry.IST).isoformat()})

    def receipt(self, **overrides):
        return {"session_date": self.day, "snapshot_id": self.scan_id, "signal_id": self.sid,
                "side": "bullish", "panel": "ranking", "display_rank": 7,
                "client_session_id": "test_browser_session_123", "page_visible": True,
                "in_viewport": True, "rendered_at": datetime.now(telemetry.IST).isoformat(), **overrides}

    def test_signal_identity_is_dated_and_direction_setup_specific(self):
        self.assertEqual(self.sid, telemetry.signal_id(self.day, "TEST", "bullish", "Long buildup"))
        self.assertNotEqual(self.sid, telemetry.signal_id("2000-01-01", "TEST", "bullish", "Long buildup"))
        self.assertNotEqual(self.sid, telemetry.signal_id(self.day, "TEST", "bearish", "Long buildup"))
        self.assertNotEqual(self.sid, telemetry.signal_id(self.day, "TEST", "bullish", "Short covering"))

    def test_receipts_deduplicate_across_retry_and_restart_per_browser_panel(self):
        body = {"receipts": [self.receipt(), self.receipt()]}
        self.assertEqual(telemetry.record_display_receipts(body)["accepted"], 1)
        self.assertEqual(telemetry.record_display_receipts(body)["accepted"], 0)
        other = self.receipt(client_session_id="second_browser_session_123")
        self.assertEqual(telemetry.record_display_receipts({"receipts": [other]})["accepted"], 1)
        instant = self.receipt(panel="instant_triggers", display_rank=None)
        self.assertEqual(telemetry.record_display_receipts({"receipts": [instant]})["accepted"], 1)

    def test_batch_validation_does_not_partially_write(self):
        for override in ({"page_visible": False}, {"in_viewport": False}, {"side": "bearish"},
                         {"session_date": "2000-01-01"}, {"display_rank": 0},
                         {"snapshot_id": "../../private"}, {"signal_id": "unknown"},
                         {"rendered_at": "2026-10-07T09:20:00"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                telemetry.record_display_receipts({"receipts": [self.receipt(), self.receipt(**override)]})
        self.write.assert_not_called()

    def test_snapshot_attaches_ids_without_mutating_ranking(self):
        payload = {"session_date": self.day, "generated_at_ist": datetime.now(telemetry.IST).isoformat(),
                   "bullish": [{"symbol": "TEST", "setup": "Long buildup", "rank": 9, "one_side_rally_score": 80}],
                   "bearish": [], "instant_triggers": []}
        decisions = [{"symbol": "TEST", "side": "bullish", "setup": "Long buildup", "rank": 9,
                      "qualified": True, "included_in_payload": True, "reason": "included"},
                     {"symbol": "NOOI", "side": "bullish", "reason": "oi_data_unavailable", "qualified": False}]
        telemetry.prepare_snapshot(payload, decisions, self.scan_id)
        self.assertEqual(payload["bullish"][0]["rank"], 9)
        self.assertEqual(payload["bullish"][0]["signal_id"], self.sid)
        events = self.write.call_args.args[0]
        self.assertEqual([e["event_type"] for e in events[:4]],
                         ["scanner_decision", "ranking_decision", "scanner_decision", "ranking_decision"])
        self.assertFalse(events[1]["payload"]["server_top6"])
        self.assertTrue(events[1]["payload"]["included_in_payload"])
        self.assertEqual(events[2]["payload"]["reason"], "oi_data_unavailable")

    def test_candidate_outcomes_include_rejects_and_adjust_direction(self):
        payload = {"session_date": "2026-10-07", "market_universe": []}
        rows = [{"symbol": "TEST", "side": side, "setup": "build", "ltp": 100,
                 "qualified": False, "reason": "rally_gate_failed"} for side in ("bullish", "bearish")]
        first = telemetry.candidate_outcomes(payload, rows, self.scan_id, "2026-10-07T09:20:00+05:30")
        self.assertEqual(len(first), 2)
        self.assertFalse(first[0]["payload"]["qualified_at_origin"])
        payload["market_universe"] = [{"symbol": "TEST", "ltp": 105}]
        measured = telemetry.candidate_outcomes(payload, [], self.scan_id, "2026-10-07T09:36:00+05:30")
        returns = {e["side"]: e["payload"]["checkpoints"]["15"]["directional_return_pct"] for e in measured}
        self.assertEqual(returns, {"bullish": 5.0, "bearish": -5.0})

    def test_late_quotes_are_not_used_for_missed_checkpoint(self):
        payload = {"session_date": "2026-10-07", "market_universe": []}
        rows = [{"symbol": "TEST", "side": "bullish", "ltp": 100}]
        telemetry.candidate_outcomes(payload, rows, self.scan_id, "2026-10-07T09:20:00+05:30")
        payload["market_universe"] = [{"symbol": "TEST", "ltp": 120}]
        result = telemetry.candidate_outcomes(payload, [], self.scan_id, "2026-10-07T09:51:00+05:30")
        checkpoints = result[0]["payload"]["checkpoints"]
        self.assertEqual(checkpoints["15"]["status"], "missed_window")
        self.assertNotIn("price", checkpoints["15"])
        self.assertEqual(checkpoints["30"]["status"], "measured")
        result = telemetry.candidate_outcomes({"session_date": "2026-10-08"}, [], self.scan_id, "2026-10-08T09:00:00+05:30")
        self.assertEqual(result[0]["session_date"], "2026-10-07")
        self.assertEqual(result[0]["payload"]["checkpoints"]["60"]["status"], "unavailable")

    def test_receipt_http_route_validates_and_accepts(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), NiftyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/stock_suggestions/display_receipts"
            data = json.dumps({"receipts": [self.receipt()]}).encode()
            request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.loads(response.read())["accepted"], 1)
            request = urllib.request.Request(url, data=b'{"receipts": "invalid"}', headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_candidate_state_recovers_original_reference_from_neon(self):
        day = "2026-10-07"
        sid = telemetry.signal_id(day, "TEST", "bullish")
        recovered = {sid: {"signal_id": sid, "symbol": "TEST", "side": "bullish",
                           "reference_price": 100, "checkpoints": {
                               "15": {"intended_at_ist": "2026-10-07T09:35:00+05:30", "status": "pending"}}}}
        with patch.object(telemetry, "_restore_candidate_outcomes", return_value=recovered) as restore:
            events = telemetry.candidate_outcomes(
                {"session_date": day, "market_universe": [{"symbol": "TEST", "ltp": 105}]},
                [], self.scan_id, "2026-10-07T09:36:00+05:30")
        restore.assert_called_once_with(day)
        self.assertEqual(events[0]["payload"]["reference_price"], 100)
        self.assertEqual(events[0]["payload"]["checkpoints"]["15"]["directional_return_pct"], 5)

    def test_db_mirror_failure_retains_local_event(self):
        entry = telemetry.event("scanner_decision", self.day, {"symbol": "TEST", "side": "bullish"}, {"signal_id": self.sid})
        from unittest.mock import Mock
        driver = types.SimpleNamespace(connect=Mock(side_effect=RuntimeError("offline")))
        with patch.dict("sys.modules", {"db_adapter": types.SimpleNamespace(DATABASE_URL="test-only"), "psycopg2": driver}):
            ORIGINAL_WRITE([entry])
        logged = json.loads((self.root / f"{self.day}.jsonl").read_text())
        self.assertEqual(logged["payload"]["signal_id"], self.sid)

    def test_db_events_use_one_batch_and_own_connection(self):
        from unittest.mock import Mock
        conn = Mock()
        cursor = conn.cursor.return_value
        driver = types.SimpleNamespace(connect=Mock(return_value=conn))
        entries = [telemetry.event("scanner_decision", self.day, {"symbol": "TEST", "side": "bullish"}, {"signal_id": self.sid}),
                   telemetry.event("ranking_decision", self.day, {"symbol": "TEST", "side": "bullish"}, {"rank": 8})]
        with patch.dict("sys.modules", {"db_adapter": types.SimpleNamespace(DATABASE_URL="test-only"), "psycopg2": driver}):
            ORIGINAL_WRITE(entries)
        cursor.execute.assert_called_once()
        self.assertEqual(len(json.loads(cursor.execute.call_args.args[1][0])), 2)
        conn.commit.assert_called_once()
        conn.close.assert_called_once()


class ScannerAuditTests(unittest.TestCase):
    def test_instant_priority_upgrade_is_logged_when_it_happens(self):
        old = {"symbol": "TEST", "side": "bullish", "setup": "Long buildup", "upgraded_to_priority": False}
        item = types.SimpleNamespace(symbol="TEST", status="Priority Rally", bell_rang=True)
        with patch.object(scanner, "_load_instant_triggers_state", return_value={"session_date": "2026-10-07", "items": [old]}), \
             patch.object(scanner, "_save_instant_triggers_state"), patch.object(scanner, "_log_event_to_db") as log:
            scanner.update_instant_triggers({"session_date": "2026-10-07", "market_clock_ist": "09:26:06"}, [item], [], {}, [])
        log.assert_called_once()
        self.assertEqual(log.call_args.args[0], "instant_trigger")
        self.assertEqual(old["upgraded_at_ist"], "09:26:06")
        self.assertTrue(old["signal_id"].startswith("sig_"))

    def test_audit_preserves_suggestions_and_records_failed_gates(self):
        rows = [{"symbol": symbol, "ltp": 110, "percent_change": 2, "volume": activity,
                 "high": 111, "low": 100, "open": 105, "prev_close": 100}
                for symbol, activity in (("WIN", 100), ("QUIET", 10), ("FAIL", 100), ("NOOI", 100))]
        oi = {s: {"oi_change": 100, "oi_change_percent": 10} for s in ("WIN", "QUIET", "FAIL")}

        def assess(row, *args, **kwargs):
            return {"score": 85, "is_one_side": row["symbol"] != "FAIL", "status": "Priority Rally",
                    "rally_type": "rally", "quality_tags": [], "blockers": ["failed"] if row["symbol"] == "FAIL" else [],
                    "range_position_percent": 90, "move_from_open_percent": 2, "index_context": "test"}

        with patch.object(scanner, "get_fo_lot_size", return_value=100), \
             patch.object(scanner, "bulk_fetch_candles"), \
             patch.object(scanner, "calc_stock_activity", side_effect=lambda r, o: r["volume"]), \
             patch.object(scanner, "assess_one_side_rally", side_effect=assess), \
             patch.object(scanner, "analyze_5m_breakout", return_value={"status": "Breakout", "breakout_time": "09:20 AM", "is_breakout": True}):
            args = dict(side="bullish", top=50, index_context={}, candles_by_symbol={})
            before, matched = scanner.build_suggestions(rows, oi, **args)
            audit = []
            after, matched_after = scanner.build_suggestions(rows, oi, audit=audit, **args)
        self.assertEqual([s.to_dict() for s in before], [s.to_dict() for s in after])
        self.assertEqual(matched, matched_after)
        reasons = {r["symbol"]: r["reason"] for r in audit}
        self.assertEqual(reasons, {"WIN": "included", "QUIET": "volume_confluence_below_minimum",
                                   "FAIL": "rally_gate_failed", "NOOI": "included"})


if __name__ == "__main__":
    unittest.main()
