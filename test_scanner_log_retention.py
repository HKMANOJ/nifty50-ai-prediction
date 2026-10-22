"""Offline-only retention checks: injected connections, no source DB writes."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

import scanner_log_retention as retention

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=retention.IST)
CUTOFF = date(2026, 10, 9)


def database(*, expired=0, batches=None, locks=None):
    conn = Mock()
    cursor = conn.cursor.return_value
    cursor.fetchone.side_effect = [(expired,)] if batches is None else [(v,) for v in (locks or [True] * len(batches))]
    cursor.fetchall.side_effect = [[(n,) for n in range(size)] for size in (batches or [])]
    connect = Mock(return_value=conn)
    return connect, conn, cursor


class CutoffTests(unittest.TestCase):
    def test_fourteen_calendar_dates_include_today_and_weekends(self):
        self.assertEqual(retention.retention_cutoff(NOW), CUTOFF)
        kept = [(CUTOFF.replace(day=day) >= CUTOFF) for day in range(9, 23)]
        self.assertEqual(len(kept), 14)
        self.assertTrue(all(kept))
        self.assertEqual(retention.retention_cutoff(datetime(2026, 10, 19, tzinfo=retention.IST)), date(2026, 10, 6))

    def test_ist_midnight_not_utc_or_host_date_is_the_boundary(self):
        before = datetime(2026, 10, 21, 18, 29, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(retention.retention_cutoff(before), date(2026, 10, 8))
        self.assertEqual(retention.retention_cutoff(after), CUTOFF)

    def test_month_year_leap_and_external_dst_boundaries(self):
        samples = (
            (datetime(2026, 1, 3, tzinfo=retention.IST), date(2025, 12, 21)),
            (datetime(2024, 3, 1, tzinfo=retention.IST), date(2024, 2, 17)),
            (datetime(2026, 3, 1, tzinfo=retention.IST), date(2026, 2, 16)),
            (datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0), date(2026, 10, 19)),
            (datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1), date(2026, 10, 19)),
        )
        for stamp, expected in samples:
            with self.subTest(stamp=stamp):
                self.assertEqual(retention.retention_cutoff(stamp), expected)

    def test_invalid_or_naive_dates_fail_before_configuration_or_connection(self):
        for stamp in ("2026-10-22", date(2026, 10, 22), datetime(2026, 10, 22)):
            with self.subTest(stamp=stamp), patch.object(retention, "_writer_url") as configured:
                with self.assertRaises(ValueError):
                    retention.run_retention_pass(now=stamp)
                configured.assert_not_called()

    def test_apply_must_be_a_real_boolean_not_a_truthy_string(self):
        for applying in ("false", "0", "1", 0, 1, None):
            with self.subTest(apply=applying), patch.object(retention, "_writer_url") as configured:
                with self.assertRaises(ValueError):
                    retention.run_retention_pass(apply=applying, now=NOW)
                configured.assert_not_called()


class DatabaseTests(unittest.TestCase):
    def test_import_does_not_connect_or_start_threads(self):
        path = Path(retention.__file__)
        spec = importlib.util.spec_from_file_location("retention_import_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"retention_import_test": module,
                                     "db_adapter": types.SimpleNamespace(DATABASE_URL="writer-secret"),
                                     "psycopg2": types.SimpleNamespace(connect=Mock(side_effect=AssertionError("No DB")))}), \
                patch.object(threading.Thread, "start", side_effect=AssertionError("No worker")):
            spec.loader.exec_module(module)

    def test_default_is_readonly_preview_with_fixed_transaction_timeouts(self):
        connect, conn, cursor = database(expired=1234)
        result = retention.run_retention_pass(now=NOW, url="writer-secret", connect=connect)
        self.assertEqual((result.status, result.cutoff, result.expired_count, result.deleted_count),
                         ("preview", CUTOFF, 1234, 0))
        connect.assert_called_once_with("writer-secret", connect_timeout=3, sslmode="require")
        conn.set_session.assert_called_once_with(readonly=True, isolation_level="READ COMMITTED", autocommit=False)
        self.assertEqual(cursor.execute.call_args_list, [call(retention.TIMEOUTS_SQL, retention.TIMEOUT_PARAMETERS),
                                                       call(retention.PREVIEW_SQL, (CUTOFF,))])
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
        cursor.close.assert_called_once()

    def test_apply_deletes_only_expired_scanner_rows_in_small_committed_batches(self):
        connect, conn, cursor = database(batches=[1000, 7])
        result = retention.run_retention_pass(apply=True, now=NOW, url="writer-secret", connect=connect)
        self.assertEqual((result.status, result.deleted_count, result.committed_batches), ("pass_complete", 1007, 2))
        conn.set_session.assert_called_once_with(readonly=False, isolation_level="READ COMMITTED", autocommit=False)
        expected = [call(retention.TIMEOUTS_SQL, retention.TIMEOUT_PARAMETERS),
                    call(retention.LOCK_SQL, (retention.ADVISORY_LOCK_KEY,)),
                    call(retention.DELETE_SQL, (CUTOFF, 1000))] * 2
        self.assertEqual(cursor.execute.call_args_list, expected)
        self.assertEqual(conn.commit.call_count, 2)
        conn.rollback.assert_called_once()

    def test_sql_scope_bounds_and_null_future_cutoff_preservation(self):
        sql = retention.DELETE_SQL
        self.assertIn("WHERE session_date < %s", sql)
        self.assertNotIn("<=", sql)
        self.assertNotIn("IS NULL", sql)
        self.assertIn("ORDER BY id LIMIT %s", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertEqual(sql.count("public.scanner_event_log"), 2)
        for forbidden in ("TRUNCATE", "CASCADE", "DROP", "VACUUM", "created_at", "CURRENT_DATE", "inputs/"):
            self.assertNotIn(forbidden, sql)
        self.assertIn("pg_try_advisory_xact_lock", retention.LOCK_SQL)
        # SQL NULL comparisons are UNKNOWN; future/cutoff dates fail strict <.
        for day, expired in ((date(2026, 10, 8), True), (CUTOFF, False), (NOW.date(), False),
                             (date(2026, 11, 1), False), (None, False)):
            self.assertEqual(day is not None and day < CUTOFF, expired)

    def test_maximum_five_batches_is_not_claimed_complete(self):
        connect, conn, cursor = database(batches=[1000] * 5)
        result = retention.run_retention_pass(apply=True, now=NOW, url="test", connect=connect)
        self.assertEqual((result.status, result.deleted_count, result.committed_batches), ("batch_cap_reached", 5000, 5))
        self.assertEqual(conn.commit.call_count, 5)
        self.assertEqual(cursor.fetchall.call_count, 5)

    def test_lock_contention_never_attempts_delete(self):
        connect, conn, cursor = database(batches=[0], locks=[False])
        result = retention.run_retention_pass(apply=True, now=NOW, url="test", connect=connect)
        self.assertEqual(result.status, "lock_busy")
        self.assertEqual(cursor.execute.call_args_list, [call(retention.TIMEOUTS_SQL, retention.TIMEOUT_PARAMETERS),
                                                       call(retention.LOCK_SQL, (retention.ADVISORY_LOCK_KEY,))])
        cursor.fetchall.assert_not_called()
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_lock_busy_after_a_commit_retains_acknowledged_count(self):
        connect, conn, cursor = database(batches=[1000, 0], locks=[True, False])
        result = retention.run_retention_pass(apply=True, now=NOW, url="test", connect=connect)
        self.assertEqual((result.status, result.deleted_count, result.committed_batches), ("lock_busy", 1000, 1))
        self.assertEqual(cursor.fetchall.call_count, 1)

    def test_zero_rows_is_success_with_no_unbounded_loop(self):
        connect, conn, cursor = database(batches=[0])
        result = retention.run_retention_pass(apply=True, now=NOW, url="test", connect=connect)
        self.assertEqual((result.status, result.deleted_count, result.committed_batches), ("pass_complete", 0, 1))
        self.assertEqual(conn.commit.call_count, 1)

    def test_error_cleanup_and_secret_free_operational_result(self):
        for stage in ("connect", "transaction", "timeouts", "lock", "delete", "fetch", "commit"):
            with self.subTest(stage=stage):
                connect, conn, cursor = database(batches=[1])
                failure = RuntimeError("password=DO_NOT_LOG host=secret sql=private")
                if stage == "connect":
                    connect.side_effect = failure
                elif stage == "transaction":
                    conn.set_session.side_effect = failure
                elif stage in ("timeouts", "lock", "delete"):
                    offset = {"timeouts": 0, "lock": 1, "delete": 2}[stage]
                    cursor.execute.side_effect = [None] * offset + [failure]
                elif stage == "fetch":
                    cursor.fetchall.side_effect = failure
                else:
                    conn.commit.side_effect = failure
                result = retention.run_retention_pass(apply=True, now=NOW, url="secret-url", connect=connect)
                self.assertEqual(result.status, "error")
                self.assertEqual(result.deleted_count, 0)
                self.assertNotIn("secret", json.dumps(result.public_summary()))
                self.assertNotIn("DO_NOT_LOG", json.dumps(result.public_summary()))
                if stage != "connect":
                    conn.rollback.assert_called_once()
                    conn.close.assert_called_once()
                if stage not in ("connect", "transaction"):
                    cursor.close.assert_called_once()

    def test_later_failure_does_not_erase_acknowledged_prior_batch(self):
        connect, conn, cursor = database(batches=[1000, 1])
        conn.commit.side_effect = [None, RuntimeError("secret")]
        result = retention.run_retention_pass(apply=True, now=NOW, url="test", connect=connect)
        self.assertEqual((result.status, result.error_stage, result.deleted_count, result.committed_batches),
                         ("error", "commit", 1000, 1))

    def test_cleanup_failure_does_not_mask_result(self):
        connect, conn, cursor = database(batches=[0])
        conn.rollback.side_effect = RuntimeError("secret")
        conn.close.side_effect = RuntimeError("secret")
        cursor.close.side_effect = RuntimeError("secret")
        result = retention.run_retention_pass(apply=True, now=NOW, url="test", connect=connect)
        self.assertEqual(result.status, "pass_complete")

    def test_no_configuration_never_connects_or_uses_replay_reader(self):
        connect = Mock(side_effect=AssertionError("No DB"))
        with patch.dict(sys.modules, {"db_adapter": types.SimpleNamespace(DATABASE_URL=None)}):
            result = retention.run_retention_pass(now=NOW, connect=connect)
        self.assertEqual(result.status, "not_configured")
        connect.assert_not_called()

    def test_lazy_existing_writer_configuration(self):
        connect, _, _ = database(expired=3)
        with patch.dict(sys.modules, {"db_adapter": types.SimpleNamespace(DATABASE_URL="existing-writer-url")}):
            result = retention.run_retention_pass(now=NOW, connect=connect)
        self.assertEqual(result.expired_count, 3)
        self.assertEqual(connect.call_args.args, ("existing-writer-url",))

    def test_invalid_preview_or_oversized_batch_is_not_committed(self):
        for count in (None, True, -1, "3"):
            connect, conn, cursor = database(expired=count)
            result = retention.run_retention_pass(now=NOW, url="test", connect=connect)
            self.assertEqual(result.status, "error")
            conn.commit.assert_not_called()
        connect, conn, cursor = database(batches=[1001])
        result = retention.run_retention_pass(apply=True, now=NOW, url="test", connect=connect)
        self.assertEqual(result.status, "error")
        conn.commit.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def test_default_automatic_retention_is_only_for_intended_render_production(self):
        production = {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": retention.PRODUCTION_HOSTNAME}
        self.assertTrue(retention.retention_enabled(production))
        for env in ({}, {"DATABASE_URL": "shared-production-writer"}, {"RENDER": "true"},
                    {"RENDER_EXTERNAL_HOSTNAME": retention.PRODUCTION_HOSTNAME},
                    {**production, "RENDER": "false"},
                    {**production, "RENDER_EXTERNAL_HOSTNAME": "other.onrender.com"}):
            with self.subTest(env=env):
                self.assertFalse(retention.retention_enabled(env))

    def test_explicit_configuration_and_pr_previews_fail_closed(self):
        production = {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": retention.PRODUCTION_HOSTNAME}
        self.assertTrue(retention.retention_enabled({"SCANNER_LOG_RETENTION_ENABLED": "1"}))
        for value in ("0", "", "true", "yes", "2"):
            self.assertFalse(retention.retention_enabled({**production, "SCANNER_LOG_RETENTION_ENABLED": value}))
        for flag in (None, "0", "1"):
            for pr_value in ("true", "TRUE"):
                env = {**production, "IS_PULL_REQUEST": pr_value}
                if flag is not None:
                    env["SCANNER_LOG_RETENTION_ENABLED"] = flag
                self.assertFalse(retention.retention_enabled(env))

    def test_worker_waits_are_cancellable_and_use_fixed_intervals(self):
        for status, seconds in (("pass_complete", 3600), ("batch_cap_reached", 60), ("error", 300), ("lock_busy", 60)):
            with self.subTest(status=status):
                stop = Mock()
                stop.is_set.return_value = False
                stop.wait.return_value = True
                result = retention.RetentionResult(status, CUTOFF)
                with patch.object(retention, "run_retention_pass", return_value=result) as run, \
                        redirect_stderr(io.StringIO()) as logged:
                    retention._retention_loop(stop)
                run.assert_called_once_with(apply=True)
                stop.wait.assert_called_once_with(seconds)
                self.assertIn(status, logged.getvalue())

    def test_already_cancelled_worker_has_no_database_activity(self):
        stop = threading.Event()
        stop.set()
        with patch.object(retention, "run_retention_pass", side_effect=AssertionError("No DB")) as run:
            thread = retention.start_retention_worker(stop)
            thread.join(timeout=1)
        self.assertTrue(thread.daemon)
        self.assertEqual(thread.name, "ScannerLogRetention")
        self.assertFalse(thread.is_alive())
        run.assert_not_called()

    def test_start_helper_starts_daemon_without_calling_pass_in_caller(self):
        stop = threading.Event()
        with patch.object(retention.threading, "Thread") as thread_factory, \
                patch.object(retention, "run_retention_pass", side_effect=AssertionError("No DB")) as run:
            thread = retention.start_retention_worker(stop)
        self.assertIs(thread, thread_factory.return_value)
        self.assertEqual(thread_factory.call_args.kwargs,
                         {"target": retention._retention_loop, "args": (stop,),
                          "name": "ScannerLogRetention", "daemon": True})
        thread.start.assert_called_once_with()
        run.assert_not_called()

    def test_cli_defaults_to_preview_and_requires_explicit_apply(self):
        for arguments, applying in (([], False), (["--apply"], True)):
            result = retention.RetentionResult("preview" if not applying else "pass_complete", CUTOFF)
            with patch.object(retention, "run_retention_pass", return_value=result) as run, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(retention.main(arguments), 0)
            run.assert_called_once_with(apply=applying)
            self.assertEqual(json.loads(output.getvalue())["cutoff"], "2026-10-09")


if __name__ == "__main__":
    unittest.main()
