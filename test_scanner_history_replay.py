"""Offline history projection and read-only database boundary checks."""
from copy import deepcopy
from datetime import date
import json
import os
import unittest
from unittest.mock import Mock, patch

import scanner_history_replay as replay

DAY = "2026-10-19"
SCAN = "scan_" + "a" * 32
OTHER_SCAN = "scan_" + "b" * 32
BATCH = "batch_" + "a" * 32
OTHER_BATCH = "batch_" + "b" * 32
AT = DAY + "T09:25:00+05:30"
START = DAY + "T09:24:00+05:30"


def decision(symbol="TEST", side="bullish", rank=1, *, snapshot=SCAN, batch=BATCH,
             qualified=True, included=True, event_id=1, **values):
    payload = {
        "snapshot_id": snapshot, "log_batch_id": batch, "symbol": symbol, "side": side,
        "observed_at_ist": AT, "scan_started_at_ist": START,
        "rank": rank, "qualified": qualified, "included_in_payload": included,
        "setup": "Long buildup", "reason": "included", "ltp": 100,
        "percent_change": 2, "lot_size": 100, **values,
    }
    return {"id": event_id, "event_type": "ranking_decision", "session_date": DAY,
            "symbol": symbol, "side": side, "payload": payload}


def complete(count=1, *, snapshot=SCAN, batch=BATCH, event_id=100, **values):
    return {"id": event_id, "event_type": "scanner_health", "session_date": DAY,
            "symbol": None, "side": None, "payload": {
                "kind": "scan_completed", "snapshot_id": snapshot, "log_batch_id": batch,
                "candidate_count": count, "started_at_ist": START,
                "completed_at_ist": AT, **values}}


def service(events, **kwargs):
    return replay.ReplayService(replay.MemoryStore(events), **kwargs)


class ProjectorTests(unittest.TestCase):
    def test_conflicting_inclusion_and_rejection_are_not_certified_complete(self):
        result = service([decision(qualified=False), complete()]).snapshot(DAY, SCAN)
        self.assertEqual(result["integrity"], "incomplete")
        self.assertIn("rank_on_rejected_row", result["issues"])
        self.assertIn("payload_inclusion_without_qualification", result["issues"])

    def test_commit_identity_is_separate_from_runtime_verification(self):
        result = service([decision(source_commit="a"*40), complete()]).snapshot(DAY, SCAN)
        self.assertEqual(result["provenance"]["recorded_source_commits"], ["a"*40])
        self.assertEqual(result["provenance"]["runtime_verification"], "not_independently_verified")
        self.assertEqual(result["provenance"]["rows_without_source_commit"], 0)

    def test_complete_snapshot_preserves_ranks_and_all_candidate_kinds(self):
        events = [decision("HIGHLOT", rank=7, lot_size=9000, event_id=1),
                  decision("FIRST", rank=1, event_id=2),
                  decision("REJECT", rank=None, qualified=False, included=False,
                           reason="rally_gate_failed", event_id=3),
                  decision("BEAR", "bearish", rank=3, event_id=4), complete(4)]
        before = deepcopy(events)
        result = service(events).snapshot(DAY, SCAN)
        self.assertEqual(result["integrity"], "complete")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["coverage"], {"expected_rows": 4, "recorded_rows": 4})
        self.assertEqual([r["rank"] for r in result["bullish"]], [1, 7, None])
        self.assertEqual(result["bullish"][1]["lot_size"], 9000)
        self.assertEqual(result["bullish"][2]["qualified"], False)
        self.assertEqual(result["bearish"][0]["rank"], 3)
        self.assertTrue(result["historical"])
        self.assertEqual(result["source"], "local_recorded_scanner_preview")
        self.assertEqual(events, before)

    def test_numeric_zero_and_false_are_preserved_but_bool_is_not_numeric(self):
        result = service([decision(ltp=0, percent_change=0, volume=0, vc_ranking=0,
                                   mp_score=False, one_side_rally_score=True, lot_size=False,
                                   is_breakout=False), complete()]).snapshot(DAY, SCAN)
        row = result["bullish"][0]
        self.assertEqual([row[k] for k in ("ltp", "percent_change", "volume", "vc_ranking")], [0, 0, 0, 0])
        self.assertIs(row["is_breakout"], False)
        for name in ("mp_score", "one_side_rally_score", "lot_size"):
            self.assertIsNone(row[name])

    def test_nonfinite_and_nested_values_do_not_escape_projection(self):
        row = decision(ltp=float("inf"), percent_change=float("nan"), reason={"secret": "x"},
                       volume="100", scanner_config={"key": "secret"},
                       source_path="/secret", client_session_id="browser-secret", raw={"x": 1})
        result = service([row, complete()]).snapshot(DAY, SCAN)
        projected = result["bullish"][0]
        self.assertEqual(set(projected), set(replay.ROW_FIELDS))
        for name in ("ltp", "percent_change", "volume", "reason"):
            self.assertIsNone(projected[name])
        self.assertNotIn("secret", json.dumps(result, allow_nan=False))

    def test_unranked_qualified_is_retained_and_flagged(self):
        result = service([decision(rank=None), complete()]).snapshot(DAY, SCAN)
        self.assertEqual(len(result["bullish"]), 1)
        self.assertIsNone(result["bullish"][0]["rank"])
        self.assertEqual(result["integrity"], "incomplete")
        self.assertIn("qualified_row_without_rank", result["issues"])

    def test_missing_qualification_is_unknown_not_default_false(self):
        row = decision()
        del row["payload"]["qualified"]
        result = service([row, complete()]).snapshot(DAY, SCAN)
        self.assertIsNone(result["bullish"][0]["qualified"])
        self.assertIn("missing_or_invalid_qualification", result["issues"])

    def test_malformed_rank_and_string_booleans_are_not_coerced(self):
        result = service([decision(rank=True, qualified="false", included="true"), complete()]).snapshot(DAY, SCAN)
        row = result["bullish"][0]
        self.assertIsNone(row["rank"])
        self.assertIsNone(row["qualified"])
        self.assertIsNone(row["included_in_payload"])
        self.assertEqual(result["integrity"], "incomplete")

    def test_partial_batch_exposes_recorded_rows_with_warning(self):
        result = service([decision(), complete(3)]).snapshot(DAY, SCAN)
        self.assertEqual(result["coverage"], {"expected_rows": 3, "recorded_rows": 1})
        self.assertEqual(result["integrity"], "incomplete")
        self.assertEqual(len(result["bullish"]), 1)
        self.assertIn("recorded_row_count_mismatch", result["issues"])

    def test_missing_marker_and_legacy_batch_have_unknown_integrity(self):
        result = service([decision()]).snapshot(DAY, SCAN)
        self.assertEqual(result["integrity"], "unknown")
        self.assertIn("missing_scan_completed_marker", result["issues"])
        legacy = [decision(), complete()]
        for event in legacy:
            event["payload"].pop("log_batch_id")
        result = service(legacy).snapshot(DAY, SCAN)
        self.assertEqual(result["integrity"], "unknown")
        self.assertIn("missing_or_invalid_log_batch_id", result["issues"])

    def test_unknown_expected_count_is_not_inferred(self):
        for value in (None, False, "1", -1):
            with self.subTest(value=value):
                result = service([decision(), complete(candidate_count=value)]).snapshot(DAY, SCAN)
                self.assertEqual(result["integrity"], "unknown")
                self.assertIsNone(result["coverage"]["expected_rows"])

    def test_zero_candidate_scan_is_complete_and_listed(self):
        svc = service([complete(0)])
        result = svc.snapshot(DAY, SCAN)
        self.assertEqual(result["integrity"], "complete")
        self.assertEqual(result["bullish"], [])
        self.assertEqual(result["coverage"], {"expected_rows": 0, "recorded_rows": 0})
        self.assertEqual(svc.sessions()["sessions"], [{"date": DAY, "snapshot_count": 1}])

    def test_ambiguous_batches_are_listed_but_snapshot_fails_closed(self):
        svc = service([decision(), complete(), decision("OTHER", batch=OTHER_BATCH, event_id=2),
                       complete(batch=OTHER_BATCH, event_id=101)])
        listed = svc.snapshots(DAY)["snapshots"][0]
        self.assertEqual(listed["integrity"], "incomplete")
        self.assertIn("ambiguous_log_batches", listed["issues"])
        self.assertEqual(listed["recorded_rows"], 2)
        with self.assertRaises(replay.ReplayError) as caught:
            svc.snapshot(DAY, SCAN)
        self.assertEqual((caught.exception.code, caught.exception.status), ("ambiguous_snapshot", 409))

    def test_marker_from_other_batch_cannot_certify_rows(self):
        svc = service([decision(), complete(batch=OTHER_BATCH)])
        self.assertIn("ambiguous_log_batches", svc.snapshots(DAY)["snapshots"][0]["issues"])
        with self.assertRaises(replay.ReplayError):
            svc.snapshot(DAY, SCAN)

    def test_duplicate_candidates_ranks_events_and_markers_are_flagged(self):
        duplicate = decision()
        result = service([duplicate, deepcopy(duplicate), complete(2), complete(2, event_id=101)]).snapshot(DAY, SCAN)
        self.assertEqual(len(result["bullish"]), 2)
        for issue in ("duplicate_candidate_rows", "duplicate_stored_ranks", "duplicate_event_ids",
                      "duplicate_scan_completed_markers"):
            self.assertIn(issue, result["issues"])
        self.assertEqual(result["integrity"], "incomplete")

    def test_concurrent_scans_share_clock_but_are_not_combined(self):
        svc = service([decision(), complete(), decision("SECOND", snapshot=OTHER_SCAN, batch=OTHER_BATCH,
                                                     event_id=2),
                       complete(snapshot=OTHER_SCAN, batch=OTHER_BATCH, event_id=101)])
        self.assertEqual(len(svc.snapshots(DAY)["snapshots"]), 2)
        self.assertEqual(svc.snapshot(DAY, SCAN)["bullish"][0]["symbol"], "TEST")
        self.assertEqual(svc.snapshot(DAY, OTHER_SCAN)["bullish"][0]["symbol"], "SECOND")

    def test_timezone_aware_times_are_normalized_and_naive_time_flagged(self):
        row = decision(observed_at_ist=DAY + "T03:55:00Z")
        self.assertEqual(service([row, complete()]).snapshot(DAY, SCAN)["observed_at_ist"], AT)
        row = decision(observed_at_ist=DAY + "T09:25:00")
        result = service([row, complete()]).snapshot(DAY, SCAN)
        self.assertIn("missing_or_invalid_observation_time", result["issues"])
        self.assertEqual(result["integrity"], "incomplete")

    def test_time_and_session_mismatch_never_look_complete(self):
        row = decision(observed_at_ist="2026-10-20T09:25:00+05:30")
        result = service([row, complete()]).snapshot(DAY, SCAN)
        self.assertIn("observation_session_mismatch", result["issues"])
        self.assertIn("inconsistent_snapshot_times", result["issues"])
        self.assertEqual(result["integrity"], "incomplete")

    def test_unknown_direction_retains_safe_row_for_accounting(self):
        row = decision(side="unknown")
        result = service([row, complete()]).snapshot(DAY, SCAN)
        self.assertEqual(len(result["unassigned"]), 1)
        self.assertIsNone(result["unassigned"][0]["side"])
        self.assertEqual(result["coverage"]["recorded_rows"], 1)
        self.assertEqual(result["integrity"], "incomplete")

    def test_holiday_empty_day_and_absent_snapshot_are_distinct(self):
        svc = service([])
        self.assertEqual(svc.sessions()["sessions"], [])
        self.assertEqual(svc.snapshots("2026-10-20")["snapshots"], [])
        with self.assertRaises(replay.ReplayError) as caught:
            svc.snapshot("2026-10-20", SCAN)
        self.assertEqual(caught.exception.status, 404)

    def test_request_validation_rejects_paths_and_sql_inputs(self):
        svc = service([])
        for day in ("../inputs", "2026-02-30", "2026-1-1", DAY + "' OR true", None):
            with self.subTest(day=day), self.assertRaises(replay.ReplayError) as caught:
                svc.snapshots(day)
            self.assertEqual(caught.exception.status, 400)
        for sid in ("../secret", SCAN + "x", "scan_123", None):
            with self.subTest(snapshot=sid), self.assertRaises(replay.ReplayError):
                svc.snapshot(DAY, sid)

    def test_sessions_only_list_recorded_days_and_at_most_30(self):
        events = []
        for number in range(1, 32):
            event = complete()
            event["session_date"] = f"2026-10-{number:02}"
            events.append(event)
        result = service(events).sessions()["sessions"]
        self.assertEqual(len(result), 30)
        self.assertEqual(result[0]["date"], "2026-10-31")
        self.assertEqual(result[-1]["date"], "2026-10-02")

    def test_day_event_limit_fails_without_truncating(self):
        with patch.object(replay, "MAX_EVENTS_PER_DAY", 1):
            with self.assertRaises(replay.ReplayError) as caught:
                service([decision(), complete()]).snapshots(DAY)
        self.assertEqual(caught.exception.code, "day_event_limit")

    def test_short_cache_expires_is_bounded_and_cannot_be_mutated(self):
        store = replay.MemoryStore([decision(), complete()])
        store.events = Mock(wraps=store.events)
        now = [0]
        svc = replay.ReplayService(store, cache_seconds=5, max_cached_days=1, clock=lambda: now[0])
        result = svc.snapshot(DAY, SCAN)
        result["bullish"][0]["rank"] = 999
        self.assertEqual(svc.snapshot(DAY, SCAN)["bullish"][0]["rank"], 1)
        self.assertEqual(store.events.call_count, 1)
        now[0] = 6
        svc.snapshot(DAY, SCAN)
        self.assertEqual(store.events.call_count, 2)
        svc.snapshots("2026-10-20")
        self.assertLessEqual(len(svc._cache), 1)


class DatabaseBoundaryTests(unittest.TestCase):
    def connection(self, rows):
        conn = Mock()
        conn.cursor.return_value.fetchall.return_value = rows
        connect = Mock(return_value=conn)
        return replay.PostgresStore("test-readonly-url", connect=connect), connect, conn

    def test_queries_are_parameterized_and_connections_enforce_readonly(self):
        store, connect, conn = self.connection([])
        self.assertEqual(store.events(DAY), [])
        self.assertEqual(connect.call_args.args, ("test-readonly-url",))
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 3)
        self.assertEqual(connect.call_args.kwargs["sslmode"], "require")
        self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
        self.assertIn("statement_timeout=3000", connect.call_args.kwargs["options"])
        conn.set_session.assert_called_once_with(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
        sql, parameters = conn.cursor.return_value.execute.call_args.args
        self.assertNotIn(DAY, sql)
        self.assertEqual(parameters[0], DAY)
        self.assertEqual(parameters[-1], replay.MAX_EVENTS_PER_DAY + 1)
        self.assertIn("public.scanner_event_log", sql)
        self.assertNotIn("SELECT payload", sql)
        self.assertNotIn("source_path", sql)
        self.assertNotIn("client_session_id", sql)
        self.assertNotIn("inputs", sql)
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
        conn.cursor.return_value.close.assert_called_once()

    def test_sessions_are_projected_with_fixed_bounds(self):
        store, _, conn = self.connection([(date(2026, 10, 19), 2)])
        self.assertEqual(store.sessions(), [{"date": DAY, "snapshot_count": 2}])
        sql, params = conn.cursor.return_value.execute.call_args.args
        self.assertTrue(sql.startswith("WITH days"))
        self.assertEqual(params[4], 30)
        self.assertEqual(params[-1], 30)

    def test_database_errors_never_include_credentials_or_driver_text(self):
        store, _, conn = self.connection([])
        conn.cursor.return_value.execute.side_effect = RuntimeError("password=secret host=private")
        with self.assertRaises(replay.ReplayError) as caught:
            store.events(DAY)
        self.assertEqual(str(caught.exception), "replay_database_unavailable")
        self.assertIsNone(caught.exception.__cause__)
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()

    def test_constructor_never_connects_and_writer_url_is_not_a_fallback(self):
        connect = Mock()
        with patch.dict(os.environ, {"DATABASE_URL": "writer-secret"}, clear=True):
            store = replay.PostgresStore(connect=connect)
            connect.assert_not_called()
            with self.assertRaises(replay.ReplayError) as caught:
                store.sessions()
        self.assertEqual(caught.exception.code, "replay_database_not_configured")
        connect.assert_not_called()

    def test_dedicated_env_url_is_used_and_each_request_gets_own_connection(self):
        store, connect, _ = self.connection([])
        with patch.dict(os.environ, {"SCANNER_REPLAY_READ_DATABASE_URL": "dedicated-read-url"}):
            store = replay.PostgresStore(connect=connect)
        store.sessions()
        store.events(DAY)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(connect.call_args.args, ("dedicated-read-url",))

    def test_limit_overflow_is_not_successfully_truncated(self):
        store, _, _ = self.connection([(1, "ranking_decision", DAY, "TEST", "bullish", {})] * 2)
        with patch.object(replay, "MAX_EVENTS_PER_DAY", 1):
            with self.assertRaises(replay.ReplayError) as caught:
                store.events(DAY)
        self.assertEqual(caught.exception.code, "day_event_limit")


if __name__ == "__main__":
    unittest.main()
