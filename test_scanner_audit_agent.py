import json
import os
from pathlib import Path
import tempfile
import sys
import types
from argparse import Namespace
import unittest
from unittest.mock import MagicMock, patch

import scanner_audit_agent as audit
import scanner_audit_ai as ai

DAY = "2026-10-09"


def event(kind, at="09:20:00", symbol="COLPAL", side="bullish", **payload):
    return {"id": 1, "session_date": DAY, "event_type": kind, "symbol": symbol, "side": side,
            "created_at": f"{DAY}T{at}+05:30",
            "payload": {"observed_at_ist": f"{DAY}T{at}+05:30", **payload}}


def rank(at, rank=2, price=100, **p):
    return event("ranking_decision", at, qualified=True, rank=rank, ltp=price,
                 signal_id="sig_a", snapshot_id="scan_"+at, **p)


class AuditTests(unittest.TestCase):
    def report(self, events, at="17:00:00"):
        return audit.build_report(events, DAY, audit.timestamp(f"{DAY}T{at}+05:30"))

    def test_early_signal_and_gap(self):
        r = self.report([rank("09:20:00", 20), rank("09:40:00", 3)])
        self.assertEqual(r["candidates"][0]["qualification_to_top5_minutes"], 20)
        self.assertIsNone(r["candidates"][0]["first_visible_client_ist"])
        self.assertIn("No visible ranking", " ".join(r["limitations"]))

    def test_premarket_excluded(self):
        r = self.report([rank("08:30:00", 1), rank("09:20:00", 7)])
        self.assertIsNone(r["candidates"][0]["first_top5_ist"])
        self.assertEqual(r["coverage"]["premarket_decisions_excluded"], 1)

    def test_receipt_exact_snapshot_identity(self):
        e = event("signal_displayed", panel="ranking", snapshot_id="scan_09:20:00", signal_id="sig_a",
                  page_visible=True, in_viewport=True, display_rank=2, rendered_at_client=f"{DAY}T09:20:15+05:30")
        r = self.report([rank("09:20:00"), e])
        self.assertEqual(r["coverage"]["matching_rank_receipts"], 1)
        e["payload"]["display_rank"] = 3
        self.assertEqual(len(self.report([rank("09:20:00"), e])["coverage"]["mismatching_rank_receipts"]), 1)
        e["payload"]["snapshot_id"] = "wrong"
        self.assertEqual(len(self.report([rank("09:20:00"), e])["coverage"]["unmatched_receipts"]), 1)

    def test_rejected_later_is_not_censored(self):
        later = rank("09:35:00", None, 95)
        later["payload"]["qualified"] = False
        later["payload"]["reason"] = "trend_failed"
        r = self.report([rank("09:20:00"), later])
        self.assertEqual(r["candidates"][0]["forward_observations"][0]["directional_return_pct"], -5)
        self.assertEqual(r["coverage"]["rejection_observation_counts"]["trend_failed"], 1)

    def test_missing_window_is_unknown(self):
        r = self.report([rank("09:20:00"), rank("09:43:00", price=120)])
        self.assertEqual(r["candidates"][0]["forward_observations"][0]["status"], "unknown")
        self.assertIsNone(r["forward_summary"][0]["mean_directional_quote_return_pct"])

    def test_bearish_and_pending(self):
        a, b = rank("09:20:00"), rank("09:35:00", price=95)
        a["side"] = b["side"] = "bearish"
        r = self.report([a, b], at="09:36:00")
        self.assertEqual(r["session_status"], "partial")
        self.assertEqual(r["candidates"][0]["forward_observations"][0]["directional_return_pct"], 5)
        self.assertEqual(r["candidates"][0]["forward_observations"][1]["status"], "pending")

    def test_wave_coverage_and_ambiguity_preserved(self):
        a = event("wave_outcome", signal_id="sig_a", wave_id="wave_a", origin_at_ist=f"{DAY}T09:20:00+05:30",
                  coverage="partial", first_touch_observed={"order": "ambiguous_same_bar"}, mfe_pct_observed=3, mae_pct_observed=2)
        b = event("wave_outcome", "09:40:00", signal_id="sig_a", wave_id="wave_b", origin_at_ist=f"{DAY}T09:35:00+05:30")
        r = self.report([a, b])
        self.assertEqual(len(r["wave_observations"]), 2)
        self.assertEqual(r["wave_observations"][0]["first_touch_observed"]["order"], "ambiguous_same_bar")

    def test_files_are_versioned_no_overwrite(self):
        r = self.report([rank("09:20:00")])
        with tempfile.TemporaryDirectory() as d:
            files = audit.save_reports(r, d)
            self.assertEqual(json.loads(Path(files[0]).read_text())["report_id"], r["report_id"])
            self.assertIn("COLPAL", Path(files[1]).read_text())
            with self.assertRaises(FileExistsError):
                audit.save_reports(r, d)

    def test_reports_cannot_be_public_static_files(self):
        with self.assertRaises(ValueError):
            audit.save_reports(self.report([]), audit.APP_ROOT / "reports" / "scanner_audit")

    def test_offline_runner_generates_report_without_ai_or_db(self):
        args = Namespace(date=DAY, events="fixture.json", ai=False, persist_db=False, init_reports_db=False)
        with tempfile.TemporaryDirectory() as d:
            args.output = d
            with patch.object(Path, "read_text", return_value=json.dumps([rank("09:20:00")])):
                with patch("builtins.print"), patch.object(audit, "read_database") as reader, patch.object(audit, "persist_report") as writer:
                    result = audit.run(args)
                    reader.assert_not_called()
                    writer.assert_not_called()
            self.assertEqual(result["ai"]["status"], "disabled")
            self.assertEqual(len(list(Path(d).glob("*.json"))), 1)
            self.assertEqual(len(list(Path(d).glob("*.md"))), 1)

    def test_reader_readonly_fixed_sql_closes(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"cutoff": 2}
        cur.fetchall.return_value = []
        driver = types.ModuleType("psycopg2")
        driver.connect = MagicMock(return_value=conn)
        extras = types.ModuleType("psycopg2.extras")
        extras.RealDictCursor = object
        with patch.dict(sys.modules, {"psycopg2": driver, "psycopg2.extras": extras}):
            self.assertEqual(audit.read_database(DAY, "test-url"), [])
        conn.set_session.assert_called_once_with(readonly=True, isolation_level="REPEATABLE READ")
        self.assertTrue(all(call.args[0].lstrip().upper().startswith("SELECT") for call in cur.execute.call_args_list))
        conn.close.assert_called_once()

    def test_writer_only_report_table(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        driver = types.ModuleType("psycopg2")
        driver.connect = MagicMock(return_value=conn)
        with patch.dict(sys.modules, {"psycopg2": driver}):
            audit.persist_report(self.report([]), "test-url", initialize=True)
        self.assertEqual(len(cur.execute.call_args_list), 2)
        for call in cur.execute.call_args_list:
            self.assertIn("public.scanner_audit_reports", call.args[0])
            self.assertNotIn("scanner_event_log", call.args[0])
        conn.close.assert_called_once()

    def test_ai_missing_key_and_failure_keep_evidence(self):
        r = self.report([rank("09:20:00")])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ai.explain(r)["status"], "not_configured")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "SCANNER_AUDIT_AI_MODEL": "test"}):
            self.assertEqual(ai.explain(r, post=MagicMock(side_effect=RuntimeError("secret")))["status"], "failed")
        self.assertEqual(len(r["candidates"]), 1)

    def test_ai_fixed_endpoint_safe_input_validated_evidence(self):
        r = self.report([rank("09:20:00")])
        response = MagicMock()
        result = {"hypotheses": [{"hypothesis": "Test delay", "test": "Replay", "evidence_keys": ["coverage"]}], "unknowns": []}
        def configure():
            response.json.return_value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}
        configure()
        post = MagicMock(return_value=response)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "SCANNER_AUDIT_AI_MODEL": "test"}):
            self.assertEqual(ai.explain(r, post=post)["status"], "completed")
            body = post.call_args.kwargs["json"]
            self.assertFalse(body["store"])
            self.assertNotIn("tools", body)
            self.assertNotIn("secret", body["input"])
            self.assertNotIn("client_session_id", body["input"])
            self.assertEqual(post.call_args.args[0], ai.ENDPOINT)
            result["hypotheses"][0]["evidence_keys"] = ["fabricated"]
            configure()
            self.assertEqual(ai.explain(r, post=post)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
