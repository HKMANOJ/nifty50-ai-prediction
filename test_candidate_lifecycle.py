import unittest
import time
import tempfile
import json
import os
import threading
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import stock_suggestion_index as ssi
from stock_suggestion_index import (
    Suggestion,
    enforce_candidate_lifecycle_and_mutual_exclusion,
    _invalidate_lifecycle_entry,
    claim_alert_delivery,
    mark_alert_delivered,
    acknowledge_alert,
    evaluate_direction_evidence,
    INDIA_TZ,
)


def make_test_suggestion(
    symbol: str = "RELIANCE",
    side: str = "bullish",
    rank: int = 1,
    ltp: float = 2500.0,
    percent_change: float = 2.5,
    score: int = 80,
    is_breakout: bool = True,
    bell_rang: bool = True,
    five_min_status: str = "Breakout Confirmed",
    chart_structure: str = "Base Breakout",
    rvol_5m: float = 2.0,
    money_flow_percentage: float = 65.0,
    stop_loss: float | None = None,
    target: float | None = None,
    entry: float | None = None,
    completed_candle_close: float | None = None,
    is_stale_data: bool = False,
    is_stale_fallback: bool = False,
    is_failed_trend: bool = False,
    breakout_time: str = "09:25 AM",
) -> Suggestion:
    if entry is None:
        entry = ltp
    if stop_loss is None:
        stop_loss = round(ltp * 0.99, 2) if side == "bullish" else round(ltp * 1.01, 2)
    if target is None:
        target = round(ltp * 1.02, 2) if side == "bullish" else round(ltp * 0.98, 2)

    return Suggestion(
        symbol=symbol,
        side=side,
        rank=rank,
        setup="Bullish Breakout" if side == "bullish" else "Bearish Breakdown",
        trade_plan="Trade plan",
        ltp=ltp,
        percent_change=percent_change,
        oi_change_percent=5.0,
        oi_change=100000.0,
        volume=500000.0,
        value_lakhs=12500.0,
        entry=entry,
        stop_loss=stop_loss,
        target=target,
        score=score,
        one_side_rally_score=score,
        rally_type="Strong Rally",
        range_position_percent=85.0 if side == "bullish" else 15.0,
        move_from_open_percent=2.0 if side == "bullish" else -2.0,
        index_context="Market in uptrend",
        quality_tags=["High RVOL", "Fresh Momentum"],
        status="Priority Rally",
        reason="Clean breakout holding day high",
        vc_ranking=3.5,
        mp_score=4.0,
        money_flow_percentage=money_flow_percentage,
        five_min_status=five_min_status,
        breakout_time=breakout_time,
        is_breakout=is_breakout,
        chart_structure=chart_structure,
        rvol_5m=rvol_5m,
        ema_trend=side,
        lot_size=250,
        bell_rang=bell_rang,
        completed_candle_close=completed_candle_close,
        is_stale_data=is_stale_data,
        is_stale_fallback=is_stale_fallback,
        is_failed_trend=is_failed_trend,
    )


# Helper function for multiprocessing test (must be top-level pickleable)
def _worker_process_scan(lifecycle_file_path: str, symbol: str, side: str, market_clock: str, epoch: float):
    import stock_suggestion_index as mod
    mod.CANDIDATE_LIFECYCLE_PATH = Path(lifecycle_file_path)
    cand = make_test_suggestion(symbol=symbol, side=side, rank=1, bell_rang=True)
    bull, _ = mod.enforce_candidate_lifecycle_and_mutual_exclusion(
        session_date="2026-10-25",
        market_clock_ist=market_clock,
        bullish=[cand],
        bearish=[],
        now_epoch=epoch,
    )
    return bull[0].alert_event_id, bull[0].should_alert


class TestCandidateLifecycleRules(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lifecycle_path = Path(self.temp_dir.name) / "candidate_lifecycle.json"
        self.patcher = patch.object(ssi, "CANDIDATE_LIFECYCLE_PATH", self.lifecycle_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_rule_1_single_alert_on_initial_qualification(self):
        """Rule 1: Alert only once when the stock first becomes qualified."""
        cand = make_test_suggestion("TCS", "bullish", rank=1, bell_rang=True)
        session_date = "2026-10-25"

        # Snapshot 1: First qualified -> should alert
        bull, bear = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        self.assertEqual(len(bull), 1)
        self.assertTrue(bull[0].should_alert)
        self.assertEqual(bull[0].alert_count, 1)
        self.assertEqual(bull[0].first_alerted_at, "09:25:00")
        self.assertIsNotNone(bull[0].alert_event_id)

        # UI receives and acknowledges alert
        acknowledge_alert(bull[0].alert_event_id, "09:25:05")

        # Snapshot 2: Refreshed scan -> alert suppressed
        cand_refresh = make_test_suggestion("TCS", "bullish", rank=1, bell_rang=True)
        bull2, bear2 = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:30:00",
            bullish=[cand_refresh],
            bearish=[],
            now_epoch=1300.0,
        )
        self.assertFalse(bull2[0].should_alert)
        self.assertEqual(bull2[0].alert_count, 1)
        self.assertEqual(bull2[0].first_alerted_at, "09:25:00")

    def test_rule_2_rank_movement_does_not_re_alert(self):
        """Rule 2: Rank movement such as #5 -> #2 -> #1 updates existing row; does not produce another alert."""
        session_date = "2026-10-25"

        d1 = make_test_suggestion("D1", "bullish", rank=1, bell_rang=False)
        d2 = make_test_suggestion("D2", "bullish", rank=2, bell_rang=False)
        d3 = make_test_suggestion("D3", "bullish", rank=3, bell_rang=False)
        d4 = make_test_suggestion("D4", "bullish", rank=4, bell_rang=False)
        infy = make_test_suggestion("INFY", "bullish", rank=5, bell_rang=True)

        # Snapshot 1: INFY enters at rank 5
        res, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[d1, d2, d3, d4, infy],
            bearish=[],
            now_epoch=1000.0,
        )
        infy_row = [x for x in res if x.symbol == "INFY"][0]
        self.assertEqual(infy_row.rank, 5)
        self.assertEqual(infy_row.first_top5_at, "09:20:00")
        self.assertIsNone(infy_row.first_rank1_at)
        self.assertTrue(infy_row.should_alert)

        # Acknowledge initial alert
        acknowledge_alert(infy_row.alert_event_id, "09:20:05")

        # Snapshot 2: Rank moves to #2
        res2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[d1, infy, d2, d3, d4],
            bearish=[],
            now_epoch=1300.0,
        )
        infy_row2 = [x for x in res2 if x.symbol == "INFY"][0]
        self.assertEqual(infy_row2.rank, 2)
        self.assertEqual(infy_row2.first_top5_at, "09:20:00")
        self.assertIsNone(infy_row2.first_rank1_at)
        self.assertFalse(infy_row2.should_alert)

        # Snapshot 3: Rank moves to #1
        res3, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:30:00",
            bullish=[infy, d1, d2, d3, d4],
            bearish=[],
            now_epoch=1600.0,
        )
        infy_row3 = [x for x in res3 if x.symbol == "INFY"][0]
        self.assertEqual(infy_row3.rank, 1)
        self.assertEqual(infy_row3.first_top5_at, "09:20:00")
        self.assertEqual(infy_row3.first_rank1_at, "09:30:00")
        self.assertFalse(infy_row3.should_alert)

    def test_rule_3_leaving_and_returning_to_top5_does_not_re_alert(self):
        """Rule 3: A stock leaving and returning to Top 5 does not create a new alert while original setup remains valid."""
        session_date = "2026-10-25"
        dummy_stocks = [make_test_suggestion(f"D{i}", "bullish", rank=i, bell_rang=False) for i in range(1, 10)]
        target = make_test_suggestion("SBIN", "bullish", rank=3, bell_rang=True)

        # In Top 5 at rank 3
        list1 = [dummy_stocks[0], dummy_stocks[1], target] + dummy_stocks[2:7]
        r1, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=list1,
            bearish=[],
            now_epoch=1000.0,
        )
        sbin1 = [x for x in r1 if x.symbol == "SBIN"][0]
        self.assertEqual(sbin1.rank, 3)
        self.assertEqual(sbin1.first_top5_at, "09:20:00")
        self.assertTrue(sbin1.should_alert)
        acknowledge_alert(sbin1.alert_event_id, "09:20:02")

        # Drops to rank 8 (leaves Top 5)
        list2 = dummy_stocks[:7] + [target]
        r2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:30:00",
            bullish=list2,
            bearish=[],
            now_epoch=1600.0,
        )
        sbin2 = [x for x in r2 if x.symbol == "SBIN"][0]
        self.assertEqual(sbin2.rank, 8)
        self.assertEqual(sbin2.first_top5_at, "09:20:00")
        self.assertFalse(sbin2.should_alert)

        # Returns to rank 4 (re-enters Top 5)
        list3 = dummy_stocks[:3] + [target] + dummy_stocks[3:7]
        r3, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:40:00",
            bullish=list3,
            bearish=[],
            now_epoch=2200.0,
        )
        sbin3 = [x for x in r3 if x.symbol == "SBIN"][0]
        self.assertEqual(sbin3.rank, 4)
        self.assertEqual(sbin3.first_top5_at, "09:20:00")  # Preserved
        self.assertFalse(sbin3.should_alert)  # Suppressed!

    def test_rule_4_metric_changes_update_same_record(self):
        """Rule 4: Percentage, RVOL, money flow and score changes update the same record."""
        session_date = "2026-10-25"
        cand1 = make_test_suggestion("HDFC", "bullish", rank=1, ltp=1600.0, percent_change=1.5,
                                      rvol_5m=1.8, money_flow_percentage=55.0, score=78, bell_rang=True)
        r1, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand1],
            bearish=[],
            now_epoch=1000.0,
        )
        self.assertEqual(r1[0].ltp, 1600.0)
        self.assertEqual(r1[0].percent_change, 1.5)
        self.assertEqual(r1[0].rvol_5m, 1.8)
        self.assertEqual(r1[0].money_flow_percentage, 55.0)
        self.assertEqual(r1[0].score, 78)
        acknowledge_alert(r1[0].alert_event_id, "09:25:02")

        # Refresh with surging metrics
        cand2 = make_test_suggestion("HDFC", "bullish", rank=1, ltp=1630.0, percent_change=3.4,
                                      rvol_5m=4.2, money_flow_percentage=82.0, score=95, bell_rang=True)
        r2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:35:00",
            bullish=[cand2],
            bearish=[],
            now_epoch=1600.0,
        )
        self.assertEqual(r2[0].ltp, 1630.0)
        self.assertEqual(r2[0].percent_change, 3.4)
        self.assertEqual(r2[0].rvol_5m, 4.2)
        self.assertEqual(r2[0].money_flow_percentage, 82.0)
        self.assertEqual(r2[0].score, 95)
        # Original timestamps preserved and alert suppressed
        self.assertEqual(r2[0].first_qualified_at, "09:25:00")
        self.assertEqual(r2[0].first_alerted_at, "09:25:00")
        self.assertFalse(r2[0].should_alert)

    def test_rule_5_bullish_and_bearish_setups_are_separate_identities(self):
        """Rule 5: Bullish and bearish setups are separate identities."""
        session_date = "2026-10-25"
        bull = make_test_suggestion("AXISBANK", "bullish", rank=1, bell_rang=True)
        bear = make_test_suggestion("WIPRO", "bearish", rank=1, bell_rang=True)

        r_bull, r_bear = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[bull],
            bearish=[bear],
            now_epoch=1000.0,
        )
        self.assertEqual(r_bull[0].symbol, "AXISBANK")
        self.assertEqual(r_bear[0].symbol, "WIPRO")

        state = ssi._load_lifecycle_state()
        self.assertIn("AXISBANK_bullish", state["candidates"])
        self.assertIn("WIPRO_bearish", state["candidates"])
        self.assertEqual(state["candidates"]["AXISBANK_bullish"]["side"], "bullish")
        self.assertEqual(state["candidates"]["WIPRO_bearish"]["side"], "bearish")

    def test_rules_6_and_7_second_wave_requires_invalidation_cooldown_and_new_structure(self):
        """Rules 6 & 7: Permit a second same-direction alert only after the first setup was explicitly
        invalidated and a genuinely new structure formed, requiring a new breakout level and cooldown (15m)."""
        session_date = "2026-10-25"
        # Initial Wave 1 breakout at 1000.0
        cand1 = make_test_suggestion("TATAMOTORS", "bullish", rank=1, entry=1000.0, ltp=1002.0,
                                     stop_loss=990.0, bell_rang=True)
        b1, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand1],
            bearish=[],
            now_epoch=1000.0,
        )
        self.assertTrue(b1[0].should_alert)
        self.assertEqual(b1[0].alert_count, 1)
        acknowledge_alert(b1[0].alert_event_id, "09:20:05")

        # Invalidate the setup at 09:35 (epoch 1900.0) due to stop loss breached
        cand_invalid = make_test_suggestion("TATAMOTORS", "bullish", rank=1, entry=1000.0, ltp=970.0,
                                            stop_loss=980.0, bell_rang=False)
        enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:35:00",
            bullish=[cand_invalid],
            bearish=[],
            now_epoch=1900.0,
        )
        state = ssi._load_lifecycle_state()
        tatam_state = state["candidates"]["TATAMOTORS_bullish"]
        self.assertTrue(tatam_state["is_invalidated"])
        self.assertEqual(tatam_state["cooldown_until_epoch"], 1900.0 + 900.0)  # Cooldown = 2800.0 (15 min)

        # Attempt 1: New breakout attempt at 09:40 (epoch 2200.0) -> During 15-minute cooldown
        # Cooldown has NOT elapsed (2200 < 2800). Must NOT alert!
        cand_cooldown = make_test_suggestion("TATAMOTORS", "bullish", rank=1, entry=1010.0, ltp=1012.0,
                                             stop_loss=1000.0, is_breakout=True, bell_rang=True)
        b_cool, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:40:00",
            bullish=[cand_cooldown],
            bearish=[],
            now_epoch=2200.0,
        )
        self.assertFalse(b_cool[0].should_alert)
        self.assertEqual(b_cool[0].alert_count, 1)

        # Attempt 2: Cooldown passed (epoch 2900.0 > 2800.0), BUT price is at or below old breakout level (1000.0)
        # Genuinely new structure has NOT formed. Must NOT alert!
        cand_no_new_struct = make_test_suggestion("TATAMOTORS", "bullish", rank=1, entry=999.0, ltp=1000.0,
                                                  stop_loss=990.0, is_breakout=True, bell_rang=True)
        b_no_struct, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:55:00",
            bullish=[cand_no_new_struct],
            bearish=[],
            now_epoch=2900.0,
        )
        self.assertFalse(b_no_struct[0].should_alert)
        self.assertEqual(b_no_struct[0].alert_count, 1)

        # Attempt 3: Cooldown passed AND genuinely new higher structure formed (entry = 1015.0 > 1000.0 * 1.002)
        # Wave 2 alert MUST fire!
        cand_wave2 = make_test_suggestion("TATAMOTORS", "bullish", rank=1, entry=1015.0, ltp=1016.0,
                                          stop_loss=1005.0, is_breakout=True, bell_rang=True,
                                          chart_structure="Higher Base Impulse")
        b_wave2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="10:00:00",
            bullish=[cand_wave2],
            bearish=[],
            now_epoch=3000.0,
        )
        self.assertTrue(b_wave2[0].should_alert)
        self.assertEqual(b_wave2[0].alert_count, 2)
        state_after = ssi._load_lifecycle_state()["candidates"]["TATAMOTORS_bullish"]
        self.assertFalse(state_after["is_invalidated"])
        self.assertEqual(state_after["last_breakout_level"], 1015.0)

    def test_rule_8_mutual_exclusion_and_direction_reversal(self):
        """Rule 8: Never allow same symbol on both bullish and bearish active lists simultaneously.
        When direction reverses, invalidate the old state before activating the new one."""
        session_date = "2026-10-25"

        # Step A: Symbol present in both input lists simultaneously -> Mutual exclusion resolves by percent_change
        cand_bull = make_test_suggestion("COALINDIA", "bullish", rank=1, percent_change=1.2, bell_rang=True)
        cand_bear = make_test_suggestion("COALINDIA", "bearish", rank=1, percent_change=-0.8, bell_rang=True)

        res_bull, res_bear = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand_bull],
            bearish=[cand_bear],
            now_epoch=1000.0,
        )
        # Positive change wins -> admitted to bullish, dropped from bearish
        self.assertEqual(len(res_bull), 1)
        self.assertEqual(len(res_bear), 0)
        self.assertEqual(res_bull[0].symbol, "COALINDIA")

        # Step B: Direction Reversal in subsequent scan
        # COALINDIA collapses to -3.0% and appears in bearish list
        cand_reversal = make_test_suggestion("COALINDIA", "bearish", rank=1, percent_change=-3.0, bell_rang=True)
        res_bull2, res_bear2 = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="10:15:00",
            bullish=[],
            bearish=[cand_reversal],
            now_epoch=4000.0,
        )
        self.assertEqual(len(res_bull2), 0)
        self.assertEqual(len(res_bear2), 1)
        self.assertEqual(res_bear2[0].symbol, "COALINDIA")
        self.assertEqual(res_bear2[0].side, "bearish")

        # Verify old bullish state was invalidated
        state = ssi._load_lifecycle_state()
        bull_entry = state["candidates"]["COALINDIA_bullish"]
        self.assertTrue(bull_entry["is_invalidated"])
        self.assertEqual(bull_entry["invalidation_reason"], "Direction reversed to Bearish")
        self.assertFalse(bull_entry["is_active"])

    def test_rule_9_timestamps_preserved_across_refreshes(self):
        """Rule 9: Keep the original first_qualified_at, first_top5_at, first_rank1_at and first_alerted_at;
        do not overwrite them during refreshes."""
        session_date = "2026-10-25"
        cand = make_test_suggestion("MARUTI", "bullish", rank=1, bell_rang=True)

        # Scan 1 at 09:16:00
        b1, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:16:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        self.assertEqual(b1[0].first_qualified_at, "09:16:00")
        self.assertEqual(b1[0].first_top5_at, "09:16:00")
        self.assertEqual(b1[0].first_rank1_at, "09:16:00")
        self.assertEqual(b1[0].first_alerted_at, "09:16:00")

        # Scan 2 at 09:30:00 (14 minutes later)
        cand2 = make_test_suggestion("MARUTI", "bullish", rank=1, bell_rang=True)
        b2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:30:00",
            bullish=[cand2],
            bearish=[],
            now_epoch=1840.0,
        )
        self.assertEqual(b2[0].first_qualified_at, "09:16:00")
        self.assertEqual(b2[0].first_top5_at, "09:16:00")
        self.assertEqual(b2[0].first_rank1_at, "09:16:00")
        self.assertEqual(b2[0].first_alerted_at, "09:16:00")

        # Scan 3 at 10:15:00 (1 hour later)
        cand3 = make_test_suggestion("MARUTI", "bullish", rank=1, bell_rang=True)
        b3, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="10:15:00",
            bullish=[cand3],
            bearish=[],
            now_epoch=4540.0,
        )
        self.assertEqual(b3[0].first_qualified_at, "09:16:00")
        self.assertEqual(b3[0].first_top5_at, "09:16:00")
        self.assertEqual(b3[0].first_rank1_at, "09:16:00")
        self.assertEqual(b3[0].first_alerted_at, "09:16:00")

    # ═══════════════════════════════════════════════════════════════════════
    # 10 FINAL PRODUCTION INTEGRATION TESTS
    # ═══════════════════════════════════════════════════════════════════════

    def test_final_1_two_concurrent_scans_single_alert(self):
        """1. Two concurrent scans qualify the same symbol - exactly one alert."""
        cand_a = make_test_suggestion("CONC_SYM", "bullish", rank=1, bell_rang=True)
        cand_b = make_test_suggestion("CONC_SYM", "bullish", rank=1, bell_rang=True)

        def run_scan(cand):
            bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
                session_date="2026-10-25",
                market_clock_ist="09:20:00",
                bullish=[cand],
                bearish=[],
                now_epoch=1000.0,
            )
            return bull[0].alert_event_id, bull[0].alert_count

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(run_scan, cand_a)
            fut2 = executor.submit(run_scan, cand_b)
            ev1, cnt1 = fut1.result()
            ev2, cnt2 = fut2.result()

        # Both workers return the same logical alert event ID and alert_count 1
        self.assertEqual(ev1, ev2)
        self.assertEqual(cnt1, 1)
        self.assertEqual(cnt2, 1)

        state = ssi._load_lifecycle_state()
        self.assertEqual(state["candidates"]["CONC_SYM_bullish"]["alert_count"], 1)

    def test_final_2_server_restart_no_duplicate_alert(self):
        """2. Server restarts immediately after qualification - no duplicate alert."""
        session_date = "2026-10-25"
        cand = make_test_suggestion("RESTART_SYM", "bullish", rank=1, bell_rang=True)

        # Pre-restart scan
        bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        self.assertTrue(bull[0].should_alert)
        event_id = bull[0].alert_event_id
        acknowledge_alert(event_id, "09:20:03")

        # Simulate immediate server restart: state file is loaded from disk on boot
        # Run next scan cycle post-restart
        cand_after_restart = make_test_suggestion("RESTART_SYM", "bullish", rank=1, bell_rang=True)
        bull_after, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand_after_restart],
            bearish=[],
            now_epoch=1300.0,
        )
        self.assertFalse(bull_after[0].should_alert)
        self.assertEqual(bull_after[0].alert_count, 1)
        self.assertEqual(bull_after[0].alert_event_id, event_id)

    def test_final_3_state_file_partially_written_corrupted_safe_recovery(self):
        """3. State file is partially written/corrupted - safe recovery without re-alerting everything."""
        session_date = "2026-10-25"
        cand = make_test_suggestion("CORRUPT_SAFE", "bullish", rank=1, bell_rang=True)

        # Normal scan creates primary state and backup (.bak)
        bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        acknowledge_alert(bull[0].alert_event_id, "09:20:02")

        # Trigger a second scan to ensure .bak is fully populated
        enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1300.0,
        )

        # Corrupt the primary file by writing truncated JSON (simulating sudden crash mid-write)
        self.lifecycle_path.write_text('{"session_date": "2026-10-25", "candidates": {"CORRUPT_SAFE_bullish": {"sy', encoding="utf-8")

        # Next scan loads state: primary fails JSON parse, successfully recovers from .bak
        cand2 = make_test_suggestion("CORRUPT_SAFE", "bullish", rank=1, bell_rang=True)
        bull2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:30:00",
            bullish=[cand2],
            bearish=[],
            now_epoch=1600.0,
        )
        # Did NOT re-alert! State recovered from backup safely
        self.assertFalse(bull2[0].should_alert)
        self.assertEqual(bull2[0].alert_count, 1)

    def test_final_4_ui_disconnected_alert_remains_available_after_reconnect(self):
        """4. UI is disconnected during qualification - alert remains available after reconnect."""
        session_date = "2026-10-25"
        cand = make_test_suggestion("DISCONN_SYM", "bullish", rank=1, bell_rang=True)

        # 09:20 - Qualified while UI is disconnected (no acknowledge_alert sent)
        bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        self.assertTrue(bull[0].should_alert)
        event_id = bull[0].alert_event_id

        # 09:25 - Scanner continues running; UI is still offline
        bull2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1300.0,
        )
        # Alert remains available as an undelivered event!
        self.assertTrue(bull2[0].should_alert)
        self.assertEqual(bull2[0].alert_event_id, event_id)

        # 09:30 - UI reconnects, polls snapshot, and sends acknowledgment
        self.assertTrue(acknowledge_alert(event_id, "09:30:05"))

        # 09:35 - Subsequent scan confirms alert is settled
        bull3, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:35:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1900.0,
        )
        self.assertFalse(bull3[0].should_alert)

    def test_final_5_render_two_workers_unique_alert_event(self):
        """5. Render runs two workers (separate processes) - one unique alert event."""
        # Test true multi-process concurrency using process pool
        with ProcessPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(
                _worker_process_scan,
                str(self.lifecycle_path),
                "RENDER_SYM",
                "bullish",
                "09:20:00",
                1000.0,
            )
            fut2 = executor.submit(
                _worker_process_scan,
                str(self.lifecycle_path),
                "RENDER_SYM",
                "bullish",
                "09:20:00",
                1000.0,
            )
            ev1, al1 = fut1.result()
            ev2, al2 = fut2.result()

        # Both worker processes generated/shared the exact same logical alert event
        self.assertEqual(ev1, ev2)

    def test_final_6_midnight_utc_during_indian_time_no_incorrect_reset(self):
        """6. Midnight UTC occurs during Indian time - no incorrect session reset."""
        # In UTC, 00:00:00 on 2026-10-25 is 05:30:00 AM IST on the same Indian day.
        # At 23:55:00 UTC on 2026-10-24, IST is 05:25:00 AM on 2026-10-25!
        t_before_utc_midnight = datetime(2026, 10, 24, 23, 55, 0, tzinfo=timezone.utc)
        t_after_utc_midnight = datetime(2026, 10, 25, 0, 5, 0, tzinfo=timezone.utc)

        ist_before = t_before_utc_midnight.astimezone(INDIA_TZ)
        ist_after = t_after_utc_midnight.astimezone(INDIA_TZ)

        # Both timestamps belong to the exact same Indian session date: 2026-10-25
        self.assertEqual(ist_before.date().isoformat(), "2026-10-25")
        self.assertEqual(ist_after.date().isoformat(), "2026-10-25")

        cand = make_test_suggestion("UTC_MIDNIGHT", "bullish", rank=1, bell_rang=True)
        session_date = ist_before.date().isoformat()

        # Scan right before UTC midnight
        b1, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist=ist_before.strftime("%H:%M:%S"),
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        acknowledge_alert(b1[0].alert_event_id, ist_before.strftime("%H:%M:%S"))

        # Scan right after UTC midnight (system clock crossed UTC 00:00)
        b2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist=ist_after.strftime("%H:%M:%S"),
            bullish=[cand],
            bearish=[],
            now_epoch=1600.0,
        )
        # No incorrect session reset! Existing candidate state preserved
        self.assertFalse(b2[0].should_alert)
        self.assertEqual(b2[0].alert_count, 1)

    def test_final_7_weekend_holiday_refresh_no_fake_session(self):
        """7. Weekend/holiday refresh - no fake new session."""
        friday_date = "2026-10-23"
        saturday_date = "2026-10-24"  # Saturday (weekday == 5)

        cand = make_test_suggestion("FRIDAY_WINNER", "bullish", rank=1, bell_rang=True)

        # Friday session scan
        b_fri, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=friday_date,
            market_clock_ist="15:25:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
            is_trading_session=True,
        )
        self.assertEqual(len(b_fri), 1)

        # Saturday manual refresh (weekend)
        b_sat, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=saturday_date,
            market_clock_ist="11:00:00",
            bullish=[cand],
            bearish=[],
            now_epoch=2000.0,
            is_trading_session=False,
        )

        state = ssi._load_lifecycle_state()
        # Does NOT create a fake session or wipe candidates
        self.assertIn("FRIDAY_WINNER_bullish", state["candidates"])
        self.assertEqual(state.get("session_date"), friday_date)

    def test_final_8_stale_fallback_data_cannot_invalidate_or_requalify(self):
        """8. Stale fallback data cannot invalidate or requalify."""
        session_date = "2026-10-25"

        # Case A: Valid setup cannot be invalidated by stale fallback data
        cand_active = make_test_suggestion("ACTIVE_SYM", "bullish", rank=1, ltp=1000.0, stop_loss=990.0, bell_rang=True)
        b_act, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand_active],
            bearish=[],
            now_epoch=1000.0,
        )
        acknowledge_alert(b_act[0].alert_event_id, "09:20:05")

        # Stale fallback returns a crashed price below stop loss (e.g. 900.0)
        cand_stale = make_test_suggestion("ACTIVE_SYM", "bullish", rank=1, ltp=900.0, stop_loss=990.0,
                                          is_stale_fallback=True)
        enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand_stale],
            bearish=[],
            now_epoch=1300.0,
        )
        state = ssi._load_lifecycle_state()
        self.assertFalse(state["candidates"]["ACTIVE_SYM_bullish"]["is_invalidated"])

        # Case B: Invalidated setup cannot be requalified by stale fallback data
        _invalidate_lifecycle_entry(state["candidates"], "ACTIVE_SYM", "bullish", "09:30:00", 1600.0, "Manual Invalidation")
        ssi._save_lifecycle_state(state)

        cand_stale_breakout = make_test_suggestion("ACTIVE_SYM", "bullish", rank=1, entry=1050.0, ltp=1055.0,
                                                   is_breakout=True, bell_rang=True, is_stale_data=True)
        b_req, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:50:00",
            bullish=[cand_stale_breakout],
            bearish=[],
            now_epoch=2800.0,
        )
        state_after = ssi._load_lifecycle_state()
        self.assertTrue(state_after["candidates"]["ACTIVE_SYM_bullish"]["is_invalidated"])
        self.assertFalse(b_req[0].should_alert)

    def test_final_9_stop_loss_intrabar_touch_completed_candle_recovers_no_invalidation(self):
        """9. Stop-loss touched intrabar but completed candle recovers - no invalidation under close-based policy."""
        session_date = "2026-10-25"
        cand = make_test_suggestion("WICK_TEST", "bullish", rank=1, entry=1000.0, ltp=1005.0,
                                    stop_loss=995.0, bell_rang=True)
        b1, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        acknowledge_alert(b1[0].alert_event_id, "09:20:05")

        # Intrabar low touched 992.0 (below stop_loss 995.0), BUT completed candle closed at 1001.0
        cand_intrabar = make_test_suggestion(
            "WICK_TEST", "bullish", rank=1, entry=1000.0,
            ltp=992.0,  # Intrabar low touched stop
            stop_loss=995.0,
            completed_candle_close=1001.0,  # Completed candle close recovered!
            bell_rang=False,
        )
        b2, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand_intrabar],
            bearish=[],
            now_epoch=1300.0,
        )
        state = ssi._load_lifecycle_state()
        # Setup was NOT invalidated because candle closed above stop loss
        self.assertFalse(state["candidates"]["WICK_TEST_bullish"]["is_invalidated"])

    def test_final_10_opposite_reversal_and_second_wave_concurrent_single_direction_survives(self):
        """10. Opposite reversal and same-direction second wave arrive concurrently - only one active direction survives."""
        session_date = "2026-10-25"
        # Extreme collision: symbol appears in both bullish (attempting wave) and bearish (attempting breakdown)
        cand_bull = make_test_suggestion("COLLISION_SYM", "bullish", rank=1, percent_change=1.8, bell_rang=True)
        cand_bear = make_test_suggestion("COLLISION_SYM", "bearish", rank=1, percent_change=-0.5, bell_rang=True)

        res_bull, res_bear = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="10:00:00",
            bullish=[cand_bull],
            bearish=[cand_bear],
            now_epoch=3000.0,
        )

        # Mutual exclusion strictly resolves: exactly ONE active direction survives
        total_active = len([x for x in res_bull if x.symbol == "COLLISION_SYM"]) + \
                       len([x for x in res_bear if x.symbol == "COLLISION_SYM"])
        self.assertEqual(total_active, 1)

        # Bullish had +1.8% vs -0.5% -> Bullish survived, Bearish was invalidated
        self.assertEqual(len(res_bull), 1)
        self.assertEqual(len(res_bear), 0)
        state = ssi._load_lifecycle_state()
        self.assertTrue(state["candidates"]["COLLISION_SYM_bearish"]["is_invalidated"])

    def test_atomic_claim_state_transitions(self):
        """Production Concern 1: PENDING -> CLAIMED -> DELIVERED -> ACKNOWLEDGED state machine."""
        session_date = "2026-10-25"
        cand = make_test_suggestion("CLAIM_TEST", "bullish", rank=1, bell_rang=True)
        res_bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand],
            bearish=[],
            now_epoch=100.0,
        )
        evt_id = res_bull[0].alert_event_id
        self.assertIsNotNone(evt_id)
        self.assertEqual(res_bull[0].delivery_status, "PENDING")

        # Worker 1 claims alert delivery
        claimed_w1 = claim_alert_delivery(evt_id, worker_id="render_worker_1", now_epoch=100.0)
        self.assertTrue(claimed_w1)

        state = ssi._load_lifecycle_state()
        entry = state["candidates"]["CLAIM_TEST_bullish"]
        self.assertEqual(entry["delivery_status"], "CLAIMED")
        self.assertEqual(entry["claimed_by"], "render_worker_1")

        # Worker 2 attempts concurrent claim on same active alert -> rejected
        claimed_w2 = claim_alert_delivery(evt_id, worker_id="render_worker_2", now_epoch=101.0)
        self.assertFalse(claimed_w2)

        # Worker 1 marks delivery completed
        delivered = mark_alert_delivered(evt_id, worker_id="render_worker_1", delivered_at_ist="09:20:01")
        self.assertTrue(delivered)

        state = ssi._load_lifecycle_state()
        entry = state["candidates"]["CLAIM_TEST_bullish"]
        self.assertEqual(entry["delivery_status"], "DELIVERED")

        # Browser client acknowledges delivery
        acked = acknowledge_alert(evt_id, acknowledged_at_ist="09:20:02", client_id="browser_tab_42")
        self.assertTrue(acked)

        state = ssi._load_lifecycle_state()
        entry = state["candidates"]["CLAIM_TEST_bullish"]
        self.assertEqual(entry["delivery_status"], "ACKNOWLEDGED")
        self.assertEqual(entry["alert_acknowledged_at"], "09:20:02")
        self.assertIn("browser_tab_42", entry.get("acknowledged_by_clients", []))

        # Further claims are rejected
        self.assertFalse(claim_alert_delivery(evt_id, worker_id="render_worker_1", now_epoch=105.0))

    def test_atomic_claim_lease_expiration_recovery(self):
        """Production Concern 1: Worker crash recovery via lease expiration."""
        session_date = "2026-10-25"
        cand = make_test_suggestion("LEASE_TEST", "bullish", rank=1, bell_rang=True)
        res_bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:20:00",
            bullish=[cand],
            bearish=[],
            now_epoch=100.0,
        )
        evt_id = res_bull[0].alert_event_id

        # Worker 1 claims with a 5-second lease at t=100.0
        self.assertTrue(claim_alert_delivery(evt_id, worker_id="worker_crashed", lease_seconds=5.0, now_epoch=100.0))

        # Worker 2 tries at t=102.0 (lease still valid) -> fails
        self.assertFalse(claim_alert_delivery(evt_id, worker_id="worker_backup", lease_seconds=5.0, now_epoch=102.0))

        # Worker 2 tries at t=106.0 (lease expired at 105.0) -> succeeds and takes over
        self.assertTrue(claim_alert_delivery(evt_id, worker_id="worker_backup", lease_seconds=5.0, now_epoch=106.0))

        state = ssi._load_lifecycle_state()
        entry = state["candidates"]["LEASE_TEST_bullish"]
        self.assertEqual(entry["claimed_by"], "worker_backup")

    def test_evidence_based_reversal_structure_over_net_pct(self):
        """Production Concern 3: Reversal selection uses completed-candle evidence & structure, not net %."""
        # Bullish has net change +4.0%, but is_failed_trend=True and old structure
        cand_bull = make_test_suggestion(
            symbol="TIE_TEST",
            side="bullish",
            rank=2,
            percent_change=4.0,
            score=60,
            chart_structure="Consolidation Range",
            bell_rang=True,
            is_failed_trend=True,
            breakout_time="09:30 AM",
        )

        # Bearish has smaller net change -1.2%, but confirmed Waterfall Cascade Breakdown and fresher timestamp
        cand_bear = make_test_suggestion(
            symbol="TIE_TEST",
            side="bearish",
            rank=1,
            percent_change=-1.2,
            score=85,
            chart_structure="Waterfall Cascade Breakdown",
            bell_rang=True,
            is_failed_trend=False,
            breakout_time="10:15 AM",
        )

        # Verify evidence evaluation scoring
        bull_ev = evaluate_direction_evidence(cand_bull, cand_bear)
        bear_ev = evaluate_direction_evidence(cand_bear, cand_bull)
        self.assertGreater(bear_ev, bull_ev)

        # In mutual exclusion, Bearish setup must win despite Bullish having higher absolute % change (+4.0% vs -1.2%)
        res_bull, res_bear = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date="2026-10-25",
            market_clock_ist="10:15:00",
            bullish=[cand_bull],
            bearish=[cand_bear],
            now_epoch=5000.0,
        )
        self.assertEqual(len(res_bear), 1)
        self.assertEqual(len(res_bull), 0)
        self.assertEqual(res_bear[0].symbol, "TIE_TEST")

    def test_acknowledge_alert_strict_validation(self):
        """Strict validation of /api/alerts/ack input and existence check."""
        # Format rejection
        self.assertFalse(acknowledge_alert(""))
        self.assertFalse(acknowledge_alert("DROP TABLE candidates;"))
        self.assertFalse(acknowledge_alert("evt_bad-format!@#$"))
        self.assertFalse(acknowledge_alert("invalid_prefix_123"))

        # Valid format but nonexistent event rejection
        self.assertFalse(acknowledge_alert("evt_NONEXISTENT_SYMBOL_1234567890"))

        # Valid format and existing candidate acceptance
        session_date = "2026-10-25"
        cand = make_test_suggestion("ACK_VAL_SYM", "bullish", rank=1, bell_rang=True)
        res_bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="09:25:00",
            bullish=[cand],
            bearish=[],
            now_epoch=100.0,
        )
        evt_id = res_bull[0].alert_event_id
        self.assertTrue(evt_id.startswith("evt_"))

        self.assertTrue(acknowledge_alert(evt_id, client_id="test_client_uuid"))
        state = ssi._load_lifecycle_state()
        entry = state["candidates"]["ACK_VAL_SYM_bullish"]
        self.assertEqual(entry["delivery_status"], "ACKNOWLEDGED")
        self.assertIn("test_client_uuid", entry["acknowledged_by_clients"])

    def test_websocket_proxy_and_ack_http_routes(self):
        """Production Concern 2: Transparent WebSocket proxying on single public port (/ws) and /api/alerts/ack endpoint."""
        import asyncio
        import http.server
        import socket
        import urllib.request
        import urllib.error
        import websockets
        from serve_nifty50_app import NiftyHandler

        # 1. Start a lightweight WebSocket test backend on a free port
        backend_loop = asyncio.new_event_loop()
        backend_port_container = []

        async def _echo_handler(ws):
            await ws.send(json.dumps({"type": "ws_welcome", "service": "backend_engine"}))
            async for msg in ws:
                data = json.loads(msg)
                await ws.send(json.dumps({"type": "echo", "received": data}))

        async def _start_ws():
            srv = await websockets.serve(_echo_handler, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            backend_port_container.append(port)
            return srv

        def _run_ws():
            asyncio.set_event_loop(backend_loop)
            srv = backend_loop.run_until_complete(_start_ws())
            backend_loop.run_forever()

        t_ws = threading.Thread(target=_run_ws, daemon=True)
        t_ws.start()
        for _ in range(50):
            if backend_port_container:
                break
            time.sleep(0.05)
        ws_port = backend_port_container[0]

        # 2. Point NiftyHandler backend port to our test WebSocket server and start HTTP server on port 0
        old_env_port = os.environ.get("WS_BACKEND_PORT")
        os.environ["WS_BACKEND_PORT"] = str(ws_port)
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NiftyHandler)
            http_port = httpd.server_address[1]
            t_http = threading.Thread(target=httpd.serve_forever, daemon=True)
            t_http.start()

            # 3. Test /api/alerts/ack with invalid ID -> HTTP 400
            req_bad = urllib.request.Request(
                f"http://127.0.0.1:{http_port}/api/alerts/ack",
                data=json.dumps({"alert_event_id": "malicious_fake_id"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_bad)
            self.assertEqual(ctx.exception.code, 400)

            # 4. Enforce lifecycle and get valid alert_event_id
            session_date = "2026-10-25"
            cand = make_test_suggestion("HTTP_ACK_SYM", "bullish", rank=1, bell_rang=True)
            res_bull, _ = enforce_candidate_lifecycle_and_mutual_exclusion(
                session_date=session_date,
                market_clock_ist="09:20:00",
                bullish=[cand],
                bearish=[],
                now_epoch=100.0,
            )
            evt_id = res_bull[0].alert_event_id
            self.assertIsNotNone(evt_id)

            # 5. Test /api/alerts/ack with valid ID -> HTTP 200
            req_good = urllib.request.Request(
                f"http://127.0.0.1:{http_port}/api/alerts/ack",
                data=json.dumps({"alert_event_id": evt_id, "client_id": "pytest_client"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req_good) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(body.get("ok"))
                self.assertTrue(body.get("acknowledged"))

            # 6. Test WebSocket proxying on single public port /ws
            async def _test_ws_proxy():
                uri = f"ws://127.0.0.1:{http_port}/ws"
                async with websockets.connect(uri) as client:
                    welcome_raw = await client.recv()
                    welcome = json.loads(welcome_raw)
                    self.assertEqual(welcome.get("type"), "ws_welcome")

                    await client.send(json.dumps({"action": "ping", "test": True}))
                    reply_raw = await client.recv()
                    reply = json.loads(reply_raw)
                    self.assertEqual(reply.get("type"), "echo")
                    self.assertTrue(reply.get("received", {}).get("test"))

            asyncio.run(_test_ws_proxy())
            httpd.shutdown()
        finally:
            if old_env_port is not None:
                os.environ["WS_BACKEND_PORT"] = old_env_port
            else:
                os.environ.pop("WS_BACKEND_PORT", None)

    def test_lifecycle_entry_saves_trade_metrics(self):
        """Validates that candidate lifecycle stores complete trade metrics (entry, sl, tgt, score, rvol, range_hold)
        so alert payloads and reconnection catch-ups never display placeholder '--'."""
        session_date = "2026-10-25"
        cand = make_test_suggestion(
            "METRIC_SYM",
            "bullish",
            rank=1,
            bell_rang=True,
            entry=1250.5,
            stop_loss=1235.0,
            target=1280.0,
            score=95,
            rvol_5m=3.2,
            breakout_time="10:15 AM",
        )

        enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date=session_date,
            market_clock_ist="10:15:00",
            bullish=[cand],
            bearish=[],
            now_epoch=1000.0,
        )
        state = ssi._load_lifecycle_state()
        entry = state["candidates"]["METRIC_SYM_bullish"]

        self.assertEqual(entry.get("entry"), 1250.5)
        self.assertEqual(entry.get("stop_loss"), 1235.0)
        self.assertEqual(entry.get("target"), 1280.0)
        self.assertEqual(entry.get("score"), 95)
        self.assertEqual(entry.get("rvol_5m"), 3.2)
        self.assertEqual(entry.get("range_position_percent"), 85.0)
        self.assertEqual(entry.get("breakout_time"), "10:15 AM")
        self.assertEqual(entry.get("alert_created_epoch"), 1000.0)


if __name__ == "__main__":
    unittest.main()
