from dataclasses import replace
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from serve_nifty50_app import NiftyHandler
from smc_volume_profile_engine import detect_order_blocks_and_structure, latest_fresh_order_block
from stock_suggestion_index import _process_candidate_lifecycle
from test_candidate_lifecycle import make_test_suggestion


START = int(datetime(2026, 11, 11, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())


def candle(index, open_, high, low, close):
    return {
        "timestamp": START + index * 300, "open": open_, "high": high,
        "low": low, "close": close, "volume": 10_000,
    }


def demand_candles():
    return [
        candle(0, 101, 102, 100, 101),
        candle(1, 101, 101, 99, 100),
        candle(2, 100, 106, 99, 105),
        candle(3, 105, 107, 103, 106),
        candle(4, 106, 108, 104, 107),
    ]


def mirror(candles):
    return [
        dict(c, open=200 - c["open"], high=200 - c["low"],
             low=200 - c["high"], close=200 - c["close"])
        for c in candles
    ]


class FreshOrderBlockTests(unittest.TestCase):
    def test_polycab_confirms_at_first_impulse_close(self):
        bars = [
            candle(0, 7952, 8011.5, 7916, 8004.5),
            candle(1, 8002.5, 8048, 7998, 8046),
            candle(2, 8046, 8049, 8016.5, 8030),
            candle(3, 8030.5, 8132.5, 8030, 8111.5),
        ]
        base_time = bars[2]["timestamp"]
        self.assertIsNone(latest_fresh_order_block(bars[:3], bullish=True))

        block = latest_fresh_order_block(bars, bullish=True)
        self.assertIsNotNone(block)
        self.assertEqual(block["time"], base_time)
        self.assertEqual(block["formation_time"], bars[3]["timestamp"])
        self.assertEqual((block["bottom"], block["top"]), (8016.5, 8049))

    def test_strong_impulse_can_confirm_with_only_two_completed_candles(self):
        bars = demand_candles()[1:3]
        for bullish, sample in ((True, bars), (False, mirror(bars))):
            with self.subTest(bullish=bullish):
                block = latest_fresh_order_block(sample, bullish=bullish)
                self.assertIsNotNone(block)
                self.assertEqual(block["time"], bars[0]["timestamp"])
                self.assertEqual(block["status"], "Fresh")

    def test_weak_impulse_requires_a_consecutive_second_candle(self):
        bars = [
            candle(0, 102, 107, 98, 101),
            candle(1, 101, 102, 99, 100),
            candle(2, 101, 104, 101, 103),
            candle(3, 103, 111, 103, 110),
        ]
        for bullish, sample in ((True, bars), (False, mirror(bars))):
            with self.subTest(bullish=bullish):
                self.assertIsNone(latest_fresh_order_block(sample[:3], bullish=bullish))
                delayed = dict(sample[3], timestamp=START + 1200)
                self.assertIsNone(latest_fresh_order_block(sample[:3] + [delayed], bullish=bullish))
                block = latest_fresh_order_block(sample, bullish=bullish)
                self.assertIsNotNone(block)
                self.assertEqual(block["time"], bars[1]["timestamp"])

    def test_zone_lifecycle_for_both_sides(self):
        for bullish in (True, False):
            with self.subTest(bullish=bullish):
                bars = demand_candles()
                untouched = bars + [candle(i, 107, 108, 104, 107) for i in range(5, 15)]
                tested = untouched + [candle(15, 107, 107, 100.5, 104)]
                breached = tested + [candle(16, 104, 104, 98, 102)]
                for sample, expected in [(untouched, "Fresh"), (tested, "Tested"), (breached, "Mitigated")]:
                    if not bullish:
                        sample = mirror(sample)
                    blocks = detect_order_blocks_and_structure(sample)["order_blocks"]
                    block = next(b for b in blocks if b["time"] == START + 300)
                    self.assertEqual(block["status"], expected)
                    self.assertEqual(block["formation_time"], START + 600)
                    selected = latest_fresh_order_block(sample, bullish=bullish)
                    self.assertEqual(selected, block if expected == "Fresh" else None)

    def test_forming_candle_is_shown_but_does_not_change_chart_freshness(self):
        handler = NiftyHandler.__new__(NiftyHandler)
        handler._send_json = Mock()
        bars = demand_candles()
        forming = candle(5, 107, 108, 98, 99)
        with patch("stock_suggestion_index.fetch_5m_candles_pipeline", return_value={
            "completed_candles": bars, "forming_candle": forming,
        }):
            handler._serve_stock_candles({"symbol": ["TEST"]})

        _, payload = handler._send_json.call_args.args
        self.assertEqual(payload["candles"][-1]["time"], forming["timestamp"])
        self.assertIn(latest_fresh_order_block(bars, bullish=True), payload["order_blocks"])
        self.assertIsNone(latest_fresh_order_block(bars + [forming], bullish=True))

    def test_alert_uses_base_close_and_never_restores_a_tested_badge(self):
        store = {}
        original = replace(make_test_suggestion("TEST", "bullish"),
                           is_demand_fresh=True, demand_fresh_time="09:25 AM")
        first = _process_candidate_lifecycle(store, original, "bullish", 1, "10:05:00", START + 3000)
        self.assertEqual(first.alert_created_at, "09:25 AM")
        self.assertTrue(first.should_alert)

        tested = replace(original, is_demand_fresh=False, demand_fresh_time=None, bell_rang=False)
        result = _process_candidate_lifecycle(store, tested, "bullish", 1, "10:10:00", START + 3300)
        self.assertFalse(result.is_demand_fresh)
        self.assertIsNone(result.demand_fresh_time)
        self.assertFalse(result.should_alert)

        newer = replace(original, demand_fresh_time="10:15 AM")
        result = _process_candidate_lifecycle(store, newer, "bullish", 1, "10:25:00", START + 4200)
        self.assertTrue(result.should_alert)
        self.assertEqual(result.alert_created_at, "10:15 AM")
        self.assertEqual(result.alert_count, 2)

    def test_cached_index_fresh_ob_structure(self):
        from serve_nifty50_app import _get_cached_index_fresh_ob, _attach_fresh_ob_to_index_context, _INDEX_OB_CACHE
        _INDEX_OB_CACHE["ts"] = 0.0  # invalidate
        bars = demand_candles()
        with patch("stock_suggestion_index.fetch_5m_candles_pipeline", return_value={"completed_candles": bars}):
            res = _get_cached_index_fresh_ob("NIFTY 50")
            if res:
                self.assertIn("type", res)
                self.assertIn("status", res)
                self.assertIn("formation_time_str", res)

        mock_payload = {
            "ok": True,
            "index_context": {
                "NIFTY 50": {"ltp": 25000.0},
                "SENSEX": {"ltp": 81000.0}
            }
        }
        _attach_fresh_ob_to_index_context(mock_payload)
        self.assertIn("fresh_ob", mock_payload["index_context"]["NIFTY 50"])

    def test_telegram_single_line_format_and_filters(self):
        import notify
        # Format checks: Bullish Demand Fresh
        nifty_msg = notify.build_alert_message({"symbol": "NIFTY", "demand_fresh_time": "09:40 AM"})
        self.assertEqual(nifty_msg, "🟢 Nifty Demand fresh at 9:40 AM")

        sensex_msg = notify.build_alert_message({"symbol": "SENSEX", "demand_fresh_time": "9:40 AM"})
        self.assertEqual(sensex_msg, "🟢 Sensex Demand fresh at 9:40 AM")

        # Format checks: Bearish Bear OB Fresh
        nifty_bear_msg = notify.build_alert_message({"symbol": "NIFTY", "type": "BEARISH", "demand_fresh_time": "11:35 AM"})
        self.assertEqual(nifty_bear_msg, "🔴 Nifty Bear OB fresh at 11:35 AM")

        sensex_bear_msg = notify.build_alert_message({"symbol": "SENSEX", "label": "BEAR OB", "demand_fresh_time": "11:35 AM"})
        self.assertEqual(sensex_bear_msg, "🔴 Sensex Bear OB fresh at 11:35 AM")

        with patch.object(notify, "send_telegram_text", return_value=(True, "")):
            # Non-index stock rejected (Strictly NIFTY & SENSEX only)
            self.assertFalse(notify.send_fresh_ob_alert({"symbol": "NESTLEIND", "demand_fresh_time": "12:45 PM"}, test=True))
            self.assertFalse(notify.send_fresh_ob_alert({"symbol": "RELIANCE", "demand_fresh_time": "10:15 AM"}, test=True))

            # Bear OB / PE alerts are actively ACCEPTED for NIFTY & SENSEX
            self.assertTrue(notify.send_fresh_ob_alert({"symbol": "NIFTY", "is_pe": True, "demand_fresh_time": "11:35 AM"}, test=True))
            self.assertTrue(notify.send_fresh_ob_alert({"symbol": "SENSEX", "type": "BEARISH", "demand_fresh_time": "11:35 AM"}, test=True))
            self.assertTrue(notify.send_fresh_ob_alert({"symbol": "SENSEX", "side": "bearish", "demand_fresh_time": "02:00 PM"}, test=True))

            # Tested / mitigated rejected
            self.assertFalse(notify.send_fresh_ob_alert({"symbol": "NIFTY", "status": "Tested", "demand_fresh_time": "01:30 PM"}, test=True))
            self.assertFalse(notify.send_fresh_ob_alert({"symbol": "SENSEX", "mitigated": True, "demand_fresh_time": "01:30 PM"}, test=True))

    def test_telegram_conviction_gate_requires_92_or_above(self):
        import gemini_evaluator
        import notify

        self.assertEqual(notify.MIN_ALERT_CONVICTION, 92)
        common = dict(
            sym_display="Nifty", clean_time="10:15 AM", setup_side="LONG",
            ltp=25900.0, open_price=25870.0, day_high=25910.0, day_low=25735.0,
            orb_5h=25880.0, orb_5l=25770.0, ema20=25840.0, atr=55.0,
            ob_top=25855.0, ob_bottom=25807.0, extension_atr=1.0,
            is_extended=False, delta_bar=0.0,
        )
        cases = [
            ({"verdict": "APPROVED", "conviction_score": 91}, False),
            ({"verdict": "APPROVED", "conviction_score": 90}, False),
            ({"verdict": "REJECTED", "conviction_score": 95}, False),
            ({"verdict": "APPROVED", "conviction_score": 92}, True),
            ({"verdict": "APPROVED", "conviction_score": 95}, True),
        ]
        for verdict_payload, expected in cases:
            with self.subTest(verdict_payload=verdict_payload):
                with patch.object(gemini_evaluator, "evaluate_trade_with_gemini", return_value=verdict_payload):
                    self.assertEqual(notify._passes_conviction_gate(**common), expected)

    def test_dual_index_confirmation_requires_agreement_within_15_mins(self):
        import tempfile
        from pathlib import Path
        import notify

        ist = ZoneInfo("Asia/Kolkata")
        with tempfile.TemporaryDirectory() as tmp:
            pending_file = Path(tmp) / "pending.json"
            with patch.object(notify, "PENDING_STORE_PATH", pending_file):
                # 1. Single index alone is held (empty output)
                t0 = datetime(2026, 11, 20, 11, 5, tzinfo=ist)
                n_long = {"sym": "Nifty", "side": "LONG", "key": "Nifty_DEMAND_FRESH_11:05 AM",
                          "item": {"symbol": "Nifty", "demand_fresh_time": "11:05 AM"}, "is_pe": False, "queued_ts": t0.timestamp()}
                self.assertEqual(notify._resolve_dual_index_candidates([n_long], set(), t0), [])

                # 2. Opposite index agrees within 10 minutes -> both dispatch together
                t1 = datetime(2026, 11, 20, 11, 15, tzinfo=ist)
                s_long = {"sym": "Sensex", "side": "LONG", "key": "Sensex_DEMAND_FRESH_11:15 AM",
                          "item": {"symbol": "Sensex", "demand_fresh_time": "11:15 AM"}, "is_pe": False, "queued_ts": t1.timestamp()}
                confirmed = notify._resolve_dual_index_candidates([s_long], set(), t1)
                self.assertEqual([c["sym"] for c in confirmed], ["Nifty", "Sensex"])

                # 3. Single index held > 15m expires when partner arrives 20m later
                t2 = datetime(2026, 11, 20, 12, 0, tzinfo=ist)
                n_short = {"sym": "Nifty", "side": "SHORT", "key": "Nifty_BEAR_OB_FRESH_12:00 PM",
                           "item": {"symbol": "Nifty", "demand_fresh_time": "12:00 PM"}, "is_pe": True, "queued_ts": t2.timestamp()}
                self.assertEqual(notify._resolve_dual_index_candidates([n_short], set(), t2), [])

                t3 = datetime(2026, 11, 20, 12, 20, tzinfo=ist)
                s_short = {"sym": "Sensex", "side": "SHORT", "key": "Sensex_BEAR_OB_FRESH_12:20 PM",
                           "item": {"symbol": "Sensex", "demand_fresh_time": "12:20 PM"}, "is_pe": True, "queued_ts": t3.timestamp()}
                self.assertEqual(notify._resolve_dual_index_candidates([s_short], set(), t3), [])

                # 4. Opposite sides do not confirm each other
                t4 = datetime(2026, 11, 20, 12, 25, tzinfo=ist)
                n_long2 = {"sym": "Nifty", "side": "LONG", "key": "Nifty_DEMAND_FRESH_12:25 PM",
                           "item": {"symbol": "Nifty", "demand_fresh_time": "12:25 PM"}, "is_pe": False, "queued_ts": t4.timestamp()}
                self.assertEqual(notify._resolve_dual_index_candidates([n_long2], set(), t4), [])

                # 5. Simultaneous confirmation on matching side fires both immediately
                t5 = datetime(2026, 11, 20, 13, 40, tzinfo=ist)
                n_long3 = {"sym": "Nifty", "side": "LONG", "key": "Nifty_DEMAND_FRESH_1:40 PM",
                           "item": {"symbol": "Nifty", "demand_fresh_time": "1:40 PM"}, "is_pe": False, "queued_ts": t5.timestamp()}
                s_long2 = {"sym": "Sensex", "side": "LONG", "key": "Sensex_DEMAND_FRESH_1:40 PM",
                           "item": {"symbol": "Sensex", "demand_fresh_time": "1:40 PM"}, "is_pe": False, "queued_ts": t5.timestamp()}
                both = notify._resolve_dual_index_candidates([n_long3, s_long2], set(), t5)
                self.assertEqual(len(both), 2)

    def test_deterministic_guards_block_runaway_bull_oversold_and_falling_knife(self):
        import gemini_evaluator

        # Signed negative extension (< -1.8x) blocks oversold Bear OB even when extension_atr is unsigned magnitude
        oversold = gemini_evaluator.evaluate_trade_deterministic({
            "setup_side": "SHORT", "ltp": 25500.0, "day_high": 25800.0, "day_low": 25490.0,
            "orb_5h": 25780.0, "orb_5l": 25700.0, "ema20": 25660.0, "atr_rolling": 50.0,
            "ob_top": 25540.0, "ob_bottom": 25500.0, "extension_atr": 2.4,
            "signed_extension_atr": -2.4, "extension_side": "SHORT", "is_extended": True,
            "above_5m_ema20": False,
        })
        self.assertEqual(oversold["verdict"], "REJECTED")
        self.assertEqual(oversold["trade_classification"], "OVERSOLD_TRAP")

        # Runaway Bull: price riding above 5m EMA20 blocks Bear OB BEFORE top-exhaustion logic
        runaway_bull_short = gemini_evaluator.evaluate_trade_deterministic({
            "setup_side": "SHORT", "ltp": 26120.0, "day_high": 26140.0, "day_low": 25880.0,
            "orb_5h": 25910.0, "orb_5l": 25870.0, "ema20": 26080.0, "atr_rolling": 50.0,
            "ob_top": 26135.0, "ob_bottom": 26105.0, "extension_atr": 0.8,
            "signed_extension_atr": 0.8, "extension_side": "LONG", "is_extended": False,
            "above_5m_ema20": True,
        })
        self.assertEqual(runaway_bull_short["verdict"], "REJECTED")
        self.assertEqual(runaway_bull_short["trade_classification"], "BULL_TREND_CHOP")

        # Confirmed Top Reversal BELOW 5m EMA20 is approved
        confirmed_top_short = gemini_evaluator.evaluate_trade_deterministic({
            "setup_side": "SHORT", "ltp": 26010.0, "day_high": 26140.0, "day_low": 25880.0,
            "orb_5h": 25910.0, "orb_5l": 25870.0, "ema20": 26040.0, "atr_rolling": 50.0,
            "ob_top": 26120.0, "ob_bottom": 26080.0, "extension_atr": 0.6,
            "signed_extension_atr": -0.6, "extension_side": "SHORT", "is_extended": False,
            "above_5m_ema20": False,
        })
        self.assertEqual(confirmed_top_short["verdict"], "APPROVED")
        self.assertEqual(confirmed_top_short["trade_classification"], "TOP_EXHAUSTION_REVERSAL")

        # Falling-knife Demand below 5m EMA20 or Bearish MTF rejected
        falling_knife = gemini_evaluator.evaluate_trade_deterministic({
            "setup_side": "LONG", "ltp": 25600.0, "day_high": 25850.0, "day_low": 25580.0,
            "orb_5h": 25840.0, "orb_5l": 25780.0, "ema20": 25660.0, "atr_rolling": 50.0,
            "ob_top": 25620.0, "ob_bottom": 25585.0, "extension_atr": 1.2,
            "signed_extension_atr": -1.2, "extension_side": "SHORT", "is_extended": False,
            "above_5m_ema20": False, "mtf_overall": "BEARISH ▼",
        })
        self.assertEqual(falling_knife["verdict"], "REJECTED")
        self.assertEqual(falling_knife["trade_classification"], "COUNTER_TREND_KNIFE")

        # Symmetric MTF rejection on SHORT when MTF trend is BULLISH
        counter_trend_short = gemini_evaluator.evaluate_trade_deterministic({
            "setup_side": "SHORT", "ltp": 25820.0, "day_high": 25950.0, "day_low": 25800.0,
            "orb_5h": 25910.0, "orb_5l": 25840.0, "ema20": 25860.0, "atr_rolling": 50.0,
            "ob_top": 25845.0, "ob_bottom": 25810.0, "above_5m_ema20": False,
            "mtf_overall": "BULLISH ▲",
        })
        self.assertEqual(counter_trend_short["verdict"], "REJECTED")
        self.assertEqual(counter_trend_short["trade_classification"], "COUNTER_TREND_RALLY")

        # Hard structural rejection must NOT be overridden even if mocked Gemini LLM returns 99 APPROVED
        with patch("gemini_evaluator.get_gemini_api_key", return_value="fake-key"), \
             patch("gemini_evaluator.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"conviction_score": 99, "verdict": "APPROVED"}'}]}}]}
            llm_res = gemini_evaluator.evaluate_trade_with_gemini({
                "setup_side": "SHORT", "ltp": 26120.0, "day_high": 26140.0, "day_low": 25880.0,
                "orb_5h": 25910.0, "orb_5l": 25870.0, "ema20": 26080.0, "atr_rolling": 50.0,
                "ob_top": 26135.0, "ob_bottom": 26105.0, "above_5m_ema20": True,
            }, use_api=True)
            self.assertEqual(llm_res["verdict"], "REJECTED")
            mock_post.assert_not_called()

    def test_signed_extension_gate_preserves_ui_abs_and_exposes_signed(self):
        from smc_volume_profile_engine import calculate_extension_gate

        # Build 25 candles hovering near 1000, then sharp drop to 940 (< -1.8x ATR below EMA20)
        bars = [candle(i, 1000, 1002, 998, 1000) for i in range(22)]
        bars.append(candle(22, 1000, 1000, 975, 976))
        bars.append(candle(23, 976, 977, 945, 946))
        bars.append(candle(24, 946, 948, 935, 936))
        gate = calculate_extension_gate(bars)
        self.assertGreater(gate["extension_atr"], 1.8)
        self.assertLess(gate["signed_extension_atr"], -1.8)
        self.assertEqual(gate["side"], "SHORT")
        self.assertTrue(gate["is_extended"])

    def test_futures_merge_preserves_spot_ohlc_and_attaches_real_oi(self):
        import upstox_provider

        spot = [
            {"timestamp": 1000, "open": 25600.0, "high": 25620.0, "low": 25590.0, "close": 25615.0, "volume": 0.0},
            {"timestamp": 1300, "open": 25615.0, "high": 25625.0, "low": 25605.0, "close": 25610.0, "volume": 0.0},
            {"timestamp": 1600, "open": 25610.0, "high": 25615.0, "low": 25595.0, "close": 25600.0, "volume": 0.0},
        ]
        fut = [
            {"timestamp": 1000, "open": 25690.0, "high": 25710.0, "low": 25680.0, "close": 25705.0, "volume": 24500.0, "oi": 12450000.0},
            {"timestamp": 1300, "open": 25705.0, "high": 25715.0, "low": 25695.0, "close": 25700.0, "volume": 18200.0, "oi": 12465000.0},
        ]
        meta = upstox_provider.merge_futures_into_candles(spot, fut)
        self.assertEqual(meta["futures_matched"], 2)
        self.assertTrue(meta["futures_oi_available"])
        # Spot OHLC never overwritten by futures price
        self.assertEqual(spot[0]["open"], 25600.0)
        self.assertEqual(spot[0]["close"], 25615.0)
        # Volume & OI attached from futures
        self.assertEqual(spot[0]["volume"], 24500.0)
        self.assertEqual(spot[0]["oi"], 12450000.0)
        self.assertTrue(spot[0]["oi_is_real"])
        self.assertEqual(spot[1]["oi"], 12465000.0)
        # Missing futures timestamp does not fake OI
        self.assertIsNone(spot[2]["oi"])
        self.assertFalse(spot[2]["oi_is_real"])

    def test_order_flow_and_footprint_parity_no_fake_oi(self):
        from smc_volume_profile_engine import (
            calculate_order_flow_and_absorption,
            calculate_cvd_series,
            compute_latest_delta_bar,
            build_candle_footprints,
        )

        # Spot/offline candles with zero volume and no OI must NOT synthesize fake 1.25M OI
        no_oi_bars = [{**candle(i, 100 + i, 104 + i, 99 + i, 103 + i), "volume": 0.0} for i in range(6)]
        of = calculate_order_flow_and_absorption(no_oi_bars)
        cvd = calculate_cvd_series(no_oi_bars)
        self.assertGreater(of["total_volume"], 0)
        self.assertEqual(of["volume_source"], "estimated")
        self.assertEqual(round(of["delta"]), round(cvd[-1]["value"]))
        fp_no_oi = build_candle_footprints(no_oi_bars)
        last_fp = fp_no_oi["_summary"][-1]
        self.assertIsNone(last_fp["oi"])
        self.assertIsNone(last_fp["oi_change"])
        self.assertFalse(last_fp["oi_is_real"])
        self.assertEqual(last_fp["total_vol"], of["recent_clusters"][-1]["total_vol"])
        self.assertEqual(compute_latest_delta_bar(no_oi_bars), round(of["recent_clusters"][-1]["delta"], 1))

        # Candles with real futures OI compute exact OI difference, and 0-vol futures bars stay 0 (no 23K spike)
        real_oi_bars = [
            {**candle(0, 100, 105, 99, 104), "volume": 220.0, "oi": 500000.0, "oi_is_real": True, "volume_source": "futures"},
            {**candle(1, 104, 109, 103, 108), "volume": 0.0, "oi": 500000.0, "oi_is_real": True, "volume_source": "futures"},
            {**candle(2, 108, 112, 107, 110), "volume": 40.0, "oi": 518500.0, "oi_is_real": True, "volume_source": "futures"},
        ]
        fp_real = build_candle_footprints(real_oi_bars)
        self.assertEqual(fp_real["_summary"][1]["total_vol"], 0)
        self.assertEqual(fp_real["_summary"][1]["net_delta"], 0)
        self.assertEqual(fp_real["_summary"][2]["total_vol"], 40)
        self.assertEqual(fp_real["_summary"][2]["oi"], 518500)
        self.assertEqual(fp_real["_summary"][2]["oi_change"], 18500)
        self.assertTrue(fp_real["_summary"][2]["oi_is_real"])
        # Ladder ask+bid sums must equal exact bar total volume (never round 220->236 or 40->42)
        for idx, expected_v in ((0, 220), (1, 0), (2, 40)):
            ts_key = str(real_oi_bars[idx]["timestamp"])
            lv_sum = sum(lv["ask_vol"] + lv["bid_vol"] for lv in fp_real[ts_key]["levels"])
            self.assertEqual(lv_sum, expected_v)

    def test_pattern_engine_diverse_shapes_and_no_clones(self):
        from high_conviction_pattern_engine import detect_high_conviction_patterns

        # 1. Verify weak 40-48% patterns (Bull Flag) are excluded by default, allowed only with include_weak_patterns=True
        bars = []
        base_ts = 1700000000
        for i in range(12):
            bars.append({"time": base_ts + i * 300, "open": 100.0, "high": 100.6, "low": 99.4, "close": 100.1, "volume": 1000.0})
        for i in range(6):
            p = 100.0 + i * 2.3
            bars.append({"time": base_ts + (12 + i) * 300, "open": p, "high": p + 2.5, "low": p - 0.2, "close": p + 2.3, "volume": 2500.0})
        for i in range(8):
            hi = 113.8 - i * 0.2
            lo = 112.2 - i * 0.1
            bars.append({"time": base_ts + (18 + i) * 300, "open": hi - 0.3, "high": hi, "low": lo, "close": lo + 0.3, "volume": 900.0})
        bars.append({"time": base_ts + 26 * 300, "open": 112.8, "high": 115.4, "low": 112.6, "close": 115.0, "volume": 3200.0})

        default_pats = detect_high_conviction_patterns(bars, min_confidence=70.0, max_patterns=5)
        self.assertEqual([p["short_name"] for p in default_pats if "FLAG" in p["short_name"] or "TRIANGLE" in p["short_name"]], [])

        opt_pats = detect_high_conviction_patterns(bars, min_confidence=70.0, max_patterns=5, include_weak_patterns=True)
        self.assertIn("BULL FLAG", [p["short_name"] for p in opt_pats])

        # 2. Verify strong Double Bottom (69% tier) is detected cleanly with zero overlapping clones
        db_bars = []
        prices = [
            (110, 111, 108, 109), (109, 109.5, 106, 107), (107, 107.5, 104, 105),
            (105, 105.5, 102, 103), (103, 103.5, 100.0, 101.0),  # Trough 1 @ 100
            (101, 104, 100.8, 103.5), (103.5, 106.5, 103, 106), (106, 108.5, 105.5, 108),
            (108, 110.2, 107.5, 109.8),  # Peak @ 110.2
            (109.8, 110.0, 107, 107.5), (107.5, 108, 105, 105.5), (105.5, 106, 103, 103.5),
            (103.5, 104, 100.2, 101.2),  # Trough 2 @ 100.2
            (101.2, 104.5, 101, 104), (104, 107.5, 103.8, 107), (107, 111.5, 106.8, 111.0),  # Breakout > 110.2
            (111, 112.5, 110.5, 112.0), (112, 113, 111.5, 112.5),
        ]
        # Pad initial bars so len >= 25
        for k in range(10):
            db_bars.append({"time": base_ts + k * 300, "open": 110.0, "high": 111.0, "low": 109.0, "close": 110.0, "volume": 1500.0})
        for idx_p, (o, h, l, cl) in enumerate(prices):
            db_bars.append({"time": base_ts + (10 + idx_p) * 300, "open": o, "high": h, "low": l, "close": cl, "volume": 1800.0})
        # Use strict 3-bar swing around Trough 1 & Trough 2 -> verify 1 clean Double Bottom
        db_res = detect_high_conviction_patterns(db_bars, min_confidence=70.0, max_patterns=4)
        self.assertEqual([p["short_name"] for p in db_res].count("DOUBLE BOTTOM"), 1)

    def test_gemini_approved_preserves_det_score_above_92(self):
        import gemini_evaluator

        snapshot = {
            "setup_side": "SHORT", "ltp": 25710.0, "day_high": 25920.0, "day_low": 25700.0,
            "orb_5h": 25909.0, "orb_5l": 25814.0, "ema20": 25760.0, "atr_rolling": 40.0,
            "ob_top": 25750.0, "ob_bottom": 25720.0, "extension_atr": 1.25,
            "signed_extension_atr": -1.25, "extension_side": "SHORT", "is_extended": False,
            "above_5m_ema20": False, "mtf_overall": "BEARISH ▼",
        }
        det = gemini_evaluator.evaluate_trade_deterministic(snapshot)
        self.assertGreaterEqual(int(det["conviction_score"]), 92)

        with patch("gemini_evaluator.get_gemini_api_key", return_value="fake-key"), \
             patch("gemini_evaluator.requests.post") as mock_post:
            # Score 90 APPROVED should preserve quantitative >=92 conviction
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": '{"conviction_score": 90, "verdict": "APPROVED", "rationale": "Strong trend"}'}]}}]
            }
            res = gemini_evaluator.evaluate_trade_with_gemini(snapshot, use_api=True)
            self.assertEqual(res["verdict"], "APPROVED")
            self.assertGreaterEqual(int(res["conviction_score"]), 92)

            # Explicit REJECTED must still reject
            mock_post.return_value.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": '{"conviction_score": 45, "verdict": "REJECTED", "rationale": "Exhausted"}'}]}}]
            }
            res_rej = gemini_evaluator.evaluate_trade_with_gemini(snapshot, use_api=True)
            self.assertEqual(res_rej["verdict"], "REJECTED")
            self.assertEqual(int(res_rej["conviction_score"]), 45)

    def test_notify_recency_measures_from_bar_close(self):
        import notify

        ist = ZoneInfo("Asia/Kolkata")
        f_dt = datetime(2026, 11, 27, 10, 50, 0, tzinfo=ist)
        # 11:10:03 AM is 20m03s after open, 15m03s after bar close (10:55:00) -> should pass
        now_ok = datetime(2026, 11, 27, 11, 10, 3, tzinfo=ist)
        ok, _, _ = notify._is_recent_ob_formation(f_dt.timestamp(), now_ok)
        self.assertTrue(ok)
        # 11:20:00 AM is 25m after bar close -> should expire
        now_old = datetime(2026, 11, 27, 11, 20, 0, tzinfo=ist)
        ok_old, _, _ = notify._is_recent_ob_formation(f_dt.timestamp(), now_old)
        self.assertFalse(ok_old)

    def test_accuracy_dashboard_local_outcome_fallback(self):
        import accuracy_dashboard

        with patch("accuracy_dashboard.connect_mysql", side_effect=RuntimeError("empty db")):
            summary = accuracy_dashboard.get_accuracy_dashboard_data()
            self.assertEqual(summary["overall"]["data_source"], "local_outcome_log")
            self.assertGreater(summary["overall"]["total_trades"], 0)
            self.assertGreater(len(summary["patterns"]), 0)

    def test_upstox_futures_oi_summary_and_expiry_check(self):
        import upstox_provider

        ist = ZoneInfo("Asia/Kolkata")
        day1_t = int(datetime(2026, 11, 26, 15, 25, tzinfo=ist).timestamp())
        day2_t1 = int(datetime(2026, 11, 27, 9, 15, tzinfo=ist).timestamp())
        day2_t2 = int(datetime(2026, 11, 27, 15, 25, tzinfo=ist).timestamp())
        fut_bars = [
            {"timestamp": day1_t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000, "oi": 1_000_000},
            {"timestamp": day2_t1, "open": 101.0, "high": 102.0, "low": 100.5, "close": 101.5, "volume": 2000, "oi": 1_050_000},
            {"timestamp": day2_t2, "open": 101.5, "high": 103.0, "low": 101.0, "close": 102.0, "volume": 3000, "oi": 1_100_000},
        ]
        oi_row = upstox_provider.extract_oi_row_from_futures_candles("RELIANCE", fut_bars)
        self.assertIsNotNone(oi_row)
        self.assertEqual(oi_row["oi_prev"], 1_000_000.0)
        self.assertEqual(oi_row["oi_now"], 1_100_000.0)
        self.assertEqual(oi_row["oi_change"], 100_000.0)
        self.assertAlmostEqual(oi_row["oi_change_percent"], 10.0, places=2)
        self.assertEqual(oi_row["volume_contracts"], 5000.0)

        now_ms = int(datetime(2026, 12, 30, 10, 0, tzinfo=ist).timestamp() * 1000)
        expired_map = {"NIFTY_FUT": {"expiry": now_ms - 10_000}, "SENSEX_FUT": {"expiry": now_ms + 86_400_000}}
        fresh_map = {"NIFTY_FUT": {"expiry": now_ms + 86_400_000}, "SENSEX_FUT": {"expiry": now_ms + 172_800_000}}
        self.assertTrue(upstox_provider._instruments_need_refresh(expired_map, now_ms=now_ms))
        self.assertFalse(upstox_provider._instruments_need_refresh(fresh_map, now_ms=now_ms))

    def test_cloud_nse_block_falls_back_to_upstox_candles_and_futures_oi(self):
        import tempfile
        import stock_suggestion_index as ssi

        ist = ZoneInfo("Asia/Kolkata")
        bars = []
        base_dt = datetime(2026, 11, 27, 9, 15, tzinfo=ist)
        for idx_b in range(18):
            ts = int((base_dt.timestamp()) + idx_b * 300)
            px = 1000.0 + idx_b * 2.5
            bars.append({
                "timestamp": ts, "open": px - 1.0, "high": px + 1.5,
                "low": px - 1.5, "close": px, "volume": 50_000.0 + idx_b * 1000.0,
            })
        fake_row = {
            "symbol": "RELIANCE", "side": "gainer", "open": 1000.0, "high": 1044.0, "low": 998.5,
            "prev_close": 995.0, "ltp": 1042.5, "percent_change": 4.77, "volume": 1_050_000.0, "value_lakhs": None,
        }
        fake_idx = {"NIFTY 50": {"name": "NIFTY 50", "ltp": 25600.0, "percent_change": 0.9, "direction": "bullish", "source": "Upstox"}}
        fake_oi = {"symbol": "RELIANCE", "oi_now": 1_100_000.0, "oi_prev": 1_000_000.0, "oi_change": 100_000.0, "oi_change_percent": 10.0, "volume_contracts": 5000.0, "futures_value": 50.0}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch.object(ssi, "CANDIDATE_LIFECYCLE_PATH", td_path / "lc.json"), \
                 patch.object(ssi, "TRACKED_BREAKOUTS_PATH", td_path / "tr.json"), \
                 patch.object(ssi, "DAILY_TOP5_STATE_PATH", td_path / "dt.json"), \
                 patch.object(ssi, "INSTANT_TRIGGERS_PATH", td_path / "it.json"), \
                 patch.object(ssi, "TOP5_ELIGIBLE_STATE_PATH", td_path / "t5.json"), \
                 patch.object(ssi, "DISCOVERY_STATE_PATH", td_path / "ds.json"), \
                 patch.object(ssi, "OUTCOME_TRACKER_STATE_PATH", td_path / "ot.json"), \
                 patch.object(ssi, "OUTPUT_PATH", td_path / "out.json"), \
                 patch.object(ssi, "fetch_top_gainers_losers", side_effect=ssi.LiveDataError("nse_blocked")), \
                 patch.object(ssi, "fetch_change_in_oi", side_effect=ssi.LiveDataError("nse_blocked")), \
                 patch.object(ssi, "fetch_index_context", return_value=(fake_idx, [])), \
                 patch.object(ssi, "load_fo_lot_sizes", return_value={"RELIANCE": 500}), \
                 patch.object(ssi, "fetch_extra_candidates", side_effect=lambda syms, cb, **kw: (cb.__setitem__("RELIANCE", bars), [dict(fake_row)])[1]), \
                 patch("upstox_provider.fetch_futures_oi_summary", return_value=fake_oi):
                payload = ssi.build_payload(20, require_current_session=False)

        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["coverage"]["oi_symbols_loaded"], 1)
        self.assertTrue(any(w.startswith("nse variations unavailable") for w in payload["warnings"]))
        self.assertEqual(payload["bullish"][0]["symbol"], "RELIANCE")
        self.assertEqual(payload["bullish"][0]["setup"], "Long buildup")

    def test_server_prefers_newer_cloud_snapshot_over_stale_disk(self):
        import json
        import tempfile
        import serve_nifty50_app as app

        stale_disk = {
            "ok": True, "session_date": "2026-11-25", "generated_at_ist": "2026-11-25T15:20:03+05:30",
            "bullish": [{"symbol": "OLD"}], "bearish": [], "coverage": {"oi_symbols_loaded": 50},
        }
        fresh_cloud = {
            "ok": True, "session_date": "2026-11-27", "generated_at_ist": "2026-11-27T15:20:03+05:30",
            "bullish": [{"symbol": "NEW"}], "bearish": [], "coverage": {"oi_symbols_loaded": 50},
        }
        with tempfile.TemporaryDirectory() as td:
            snap_file = Path(td) / "stock_suggestion_index.latest.json"
            snap_file.write_text(json.dumps(stale_disk), encoding="utf-8")
            with patch.object(app, "STOCK_SUGGESTION_SNAPSHOT", snap_file), \
                 patch.object(app, "DEFAULT_STOCK_SUGGESTION_SNAPSHOT", snap_file), \
                 patch.dict(app._SCAN_STATE, {"payload": None, "ts": 0.0, "signature": None}), \
                 patch.dict(app._CLOUD_SNAPSHOT_CACHE, {"payload": None, "ts": 0.0}), \
                 patch("db_adapter.load_cloud_state", return_value=fresh_cloud):
                handler = app.NiftyHandler.__new__(app.NiftyHandler)
                chosen = handler._cached_or_snapshot()
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["session_date"], "2026-11-27")
        self.assertEqual(chosen["bullish"][0]["symbol"], "NEW")

    def test_upstox_fetch_futures_oi_summary_uses_30m_candles_without_caching_1m(self):
        import json
        import upstox_provider

        sample_30m = {
            "status": "success",
            "data": {
                "candles": [
                    ["2026-11-27T15:00:00+05:30", 2500.0, 2510.0, 2495.0, 2505.0, 3000, 1120000],
                    ["2026-11-27T14:30:00+05:30", 2490.0, 2505.0, 2488.0, 2500.0, 2000, 1110000],
                    ["2026-11-26T15:00:00+05:30", 2480.0, 2485.0, 2475.0, 2482.0, 4000, 1000000],
                ]
            },
        }
        seen_urls: list[str] = []

        class _FakeResp:
            status = 200

            def __init__(self, body: dict):
                self._raw = json.dumps(body).encode("utf-8")

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def _fake_urlopen(req, timeout=6):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            seen_urls.append(url)
            if "/30minute/" in url:
                return _FakeResp(sample_30m)
            return _FakeResp({"status": "success", "data": {"candles": []}})

        with patch.dict(upstox_provider._FUTURES_OI_SUMMARY_CACHE, {}, clear=True), \
             patch.dict(upstox_provider._FUTURES_CACHE, {}, clear=True), \
             patch.object(upstox_provider, "get_futures_instrument_key", return_value="NSE_FO|59097"), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            res = upstox_provider.fetch_futures_oi_summary("RELIANCE")
            fut_cache_size = len(upstox_provider._FUTURES_CACHE)

        self.assertIsNotNone(res)
        self.assertTrue(any("/30minute/" in u for u in seen_urls))
        self.assertEqual(res["symbol"], "RELIANCE")
        self.assertEqual(res["oi_now"], 1120000.0)
        self.assertEqual(res["oi_prev"], 1000000.0)
        self.assertEqual(res["oi_change"], 120000.0)
        self.assertEqual(res["oi_change_percent"], 12.0)
        self.assertEqual(res["volume_contracts"], 5000.0)
        self.assertEqual(res["source"], "upstox_futures_30m")
        self.assertEqual(fut_cache_size, 0)

    def test_compact_db_telemetry_filters_verbose_events_from_postgres(self):
        import json
        import os
        import tempfile
        from unittest.mock import MagicMock
        import db_adapter
        import scanner_telemetry

        events = [
            {
                "event_type": "ranking_decision",
                "session_date": "2026-11-27",
                "symbol": "RELIANCE",
                "side": "bullish",
                "payload": {"score": 90},
            },
            {
                "event_type": "outcome",
                "session_date": "2026-11-27",
                "symbol": "RELIANCE",
                "side": "bullish",
                "payload": {"pnl": 1.2},
            },
        ]
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with tempfile.TemporaryDirectory() as td:
            root_path = Path(td)
            env_patch = {"SCANNER_COMPACT_DB_TELEMETRY": "1", "SCANNER_VERBOSE_DB_TELEMETRY": "0"}
            with patch.dict(os.environ, env_patch), \
                 patch.object(scanner_telemetry, "ROOT", root_path), \
                 patch.object(db_adapter, "DATABASE_URL", "postgresql://mock/test"), \
                 patch("psycopg2.connect", return_value=mock_conn):
                scanner_telemetry.write_events(events)

            local_lines = (root_path / "2026-11-27.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(local_lines), 2)
            uploaded = json.loads(mock_cursor.execute.call_args[0][1][0])
            self.assertEqual([row["event_type"] for row in uploaded], ["outcome"])

    def test_head_health_returns_200_and_empty_content_length(self):
        from http import HTTPStatus
        from unittest.mock import MagicMock
        import serve_nifty50_app as app

        handler = app.NiftyHandler.__new__(app.NiftyHandler)
        handler.path = "/api/health"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        with patch.object(app, "_maybe_trigger_wakeup_catchup") as mock_wake:
            handler.do_HEAD()

        mock_wake.assert_called_once()
        handler.send_response.assert_called_once_with(HTTPStatus.OK)
        handler.send_header.assert_any_call("Content-Length", "0")
        handler.end_headers.assert_called_once()


if __name__ == "__main__":
    unittest.main()
