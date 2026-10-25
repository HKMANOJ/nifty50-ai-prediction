import unittest
from datetime import datetime
from stock_suggestion_index import detect_3bar_impulse_cascade, analyze_5m_breakout, Suggestion, INDIA_TZ


class Test3BarImpulseCascade(unittest.TestCase):
    def test_bearish_cascade_detected(self):
        # 3 consecutive red candles with lower lows and solid bodies
        candles = [
            {"timestamp": 1000, "open": 100.0, "high": 100.5, "low": 98.0, "close": 98.2, "volume": 1000},
            {"timestamp": 1300, "open": 98.2, "high": 98.5, "low": 96.0, "close": 96.2, "volume": 1200},
            {"timestamp": 1600, "open": 96.2, "high": 96.4, "low": 94.0, "close": 94.2, "volume": 1500},
        ]
        res = detect_3bar_impulse_cascade(candles, "bearish", key_level=99.0, avg_5m_vol=1000)
        self.assertTrue(res["is_cascade"])
        self.assertEqual(res["cascade_type"], "Waterfall Breakdown")
        self.assertGreaterEqual(res["cascade_body_pct"], 60.0)

    def test_bearish_cascade_fails_on_wick_rejection(self):
        # 3 candles, but one is a hammer/pinbar with large lower wick (body < 60%)
        candles = [
            {"timestamp": 1000, "open": 100.0, "high": 100.5, "low": 98.0, "close": 98.2, "volume": 1000},
            {"timestamp": 1300, "open": 98.2, "high": 98.5, "low": 93.0, "close": 98.0, "volume": 1200}, # long lower wick
            {"timestamp": 1600, "open": 98.0, "high": 98.2, "low": 97.0, "close": 97.2, "volume": 1500},
        ]
        res = detect_3bar_impulse_cascade(candles, "bearish", key_level=99.0, avg_5m_vol=1000)
        self.assertFalse(res["is_cascade"])

    def test_bearish_cascade_fails_if_above_pml(self):
        candles = [
            {"timestamp": 1000, "open": 105.0, "high": 105.5, "low": 103.0, "close": 103.2, "volume": 1000},
            {"timestamp": 1300, "open": 103.2, "high": 103.5, "low": 101.0, "close": 101.2, "volume": 1200},
            {"timestamp": 1600, "open": 101.2, "high": 101.4, "low": 99.0, "close": 99.2, "volume": 1500},
        ]
        # PML is at 95.0, so price at 99.2 is still above PML
        res = detect_3bar_impulse_cascade(candles, "bearish", key_level=95.0, avg_5m_vol=1000)
        self.assertFalse(res["is_cascade"])

    def test_bullish_cascade_detected(self):
        # 3 consecutive green candles with higher highs and solid bodies
        candles = [
            {"timestamp": 1000, "open": 100.0, "high": 102.0, "low": 99.8, "close": 101.8, "volume": 1000},
            {"timestamp": 1300, "open": 101.8, "high": 104.0, "low": 101.6, "close": 103.8, "volume": 1200},
            {"timestamp": 1600, "open": 103.8, "high": 106.0, "low": 103.6, "close": 105.8, "volume": 1500},
        ]
        res = detect_3bar_impulse_cascade(candles, "bullish", key_level=101.0, avg_5m_vol=1000)
        self.assertTrue(res["is_cascade"])
        self.assertEqual(res["cascade_type"], "Impulse Breakout")
        self.assertGreaterEqual(res["cascade_body_pct"], 60.0)

    def test_analyze_5m_breakout_integration(self):
        candles = [
            {"timestamp": 1000, "open": 100.0, "high": 100.5, "low": 98.0, "close": 98.5, "volume": 1000},
            {"timestamp": 1300, "open": 98.5, "high": 98.7, "low": 96.0, "close": 96.2, "volume": 1200},
            {"timestamp": 1600, "open": 96.2, "high": 96.4, "low": 94.0, "close": 94.2, "volume": 1500},
            {"timestamp": 1900, "open": 94.2, "high": 94.4, "low": 92.0, "close": 92.2, "volume": 1800},
        ]
        row = {"symbol": "TESTSYM", "ltp": 92.2, "percent_change": -3.5}
        bo = analyze_5m_breakout(candles, "bearish", row)
        self.assertTrue(bo["is_breakout"])
        self.assertTrue(bo["is_cascade"])
        self.assertEqual(bo["chart_structure"], "Active Waterfall")
        self.assertEqual(bo["status"], "Waterfall BD")

    def test_independent_fno_stock_not_in_oi_spurts_qualifies(self):
        from stock_suggestion_index import build_suggestions
        # Stock with valid F&O lot size (e.g. 360ONE lot=500), but NOT in oi_by_symbol
        candles = [
            {"timestamp": 1000, "open": 1000.0, "high": 1005.0, "low": 980.0, "close": 985.0, "volume": 50000},
            {"timestamp": 1300, "open": 985.0, "high": 987.0, "low": 960.0, "close": 962.0, "volume": 60000},
            {"timestamp": 1600, "open": 962.0, "high": 964.0, "low": 940.0, "close": 942.0, "volume": 70000},
            {"timestamp": 1900, "open": 942.0, "high": 944.0, "low": 920.0, "close": 922.0, "volume": 80000},
        ]
        row = {
            "symbol": "360ONE",
            "ltp": 922.0,
            "percent_change": -3.8,
            "volume": 260000,
            "high": 1005.0,
            "low": 920.0,
            "open": 995.0,
            "prev_close": 960.0,
        }
        # Empty OI map: simulate stock NOT being in NSE OI Spurts table
        empty_oi = {}
        idx_ctx = {"NIFTY 50": {"percent_change": -1.0}}
        candles_map = {"360ONE": candles}

        suggestions, total = build_suggestions([row], empty_oi, side="bearish", top=5, index_context=idx_ctx, candles_by_symbol=candles_map)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].symbol, "360ONE")
        self.assertTrue(suggestions[0].is_cascade)
        self.assertEqual(suggestions[0].five_min_status, "Waterfall BD")

    def test_base_and_break_pattern_detected(self):
        # Big Green -> Consolidation Pause -> Next Green Breakout
        candles = [
            {"timestamp": 1000, "open": 100.0, "high": 103.0, "low": 99.8, "close": 102.8, "volume": 5000}, # Pole: big green
            {"timestamp": 1300, "open": 102.8, "high": 103.2, "low": 101.5, "close": 102.0, "volume": 1200}, # Pause: small base holding > 50%
            {"timestamp": 1600, "open": 102.0, "high": 105.5, "low": 101.8, "close": 105.2, "volume": 6000}, # Expansion: next green breakout
        ]
        res = detect_3bar_impulse_cascade(candles, "bullish", key_level=101.0, avg_5m_vol=3000)
        self.assertTrue(res["is_cascade"])
        self.assertEqual(res["cascade_type"], "Impulse Breakout")

    def test_impulse_invalidated_by_subsequent_dump(self):
        # 3 green candles, but followed immediately by a sharp dump closing below the impulse base
        candles = [
            {"timestamp": 1000, "open": 100.0, "high": 102.0, "low": 99.8, "close": 101.8, "volume": 1000},
            {"timestamp": 1300, "open": 101.8, "high": 104.0, "low": 101.6, "close": 103.8, "volume": 1200},
            {"timestamp": 1600, "open": 103.8, "high": 106.0, "low": 103.6, "close": 105.8, "volume": 1500}, # Impulse peak at 1600
            {"timestamp": 1900, "open": 105.8, "high": 105.8, "low": 102.0, "close": 102.2, "volume": 2000}, # Dump candle below 103.6
        ]
        res = detect_3bar_impulse_cascade(candles, "bullish", key_level=101.0, avg_5m_vol=1000)
        self.assertFalse(res["is_cascade"])

    def test_sector_alignment_computes_across_peers(self):
        from stock_suggestion_index import compute_sector_alignment, build_suggestions
        # BLUESTARCO, VOLTAS, HAVELLS are all Consumer Durables
        universe = [
            {"symbol": "BLUESTARCO", "percent_change": -4.2},
            {"symbol": "VOLTAS", "percent_change": -3.9},
            {"symbol": "HAVELLS", "percent_change": -3.8},
            {"symbol": "TCS", "percent_change": 1.2},
            {"symbol": "INFY", "percent_change": 1.5},
        ]
        alignments = compute_sector_alignment(universe)
        # All 3 consumer durables stocks moved in the same direction (-), so alignment is 100%
        self.assertEqual(alignments["BLUESTARCO"], 100.0)
        self.assertEqual(alignments["VOLTAS"], 100.0)
        self.assertEqual(alignments["HAVELLS"], 100.0)
        # Both IT stocks moved in same direction (+), so alignment is 100%
        self.assertEqual(alignments["TCS"], 100.0)
        self.assertEqual(alignments["INFY"], 100.0)

    def test_early_breakout_time_retained_through_late_day_pullback(self):
        # Morning low at 923.20. Decisive breakdown at 09:25 AM (1735012500)
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=5, minutes=30))
        # 09:15 AM: ts=1729741500 (2024-10-24 09:15:00 IST)
        t0 = 1729741500
        candles = [
            {"timestamp": t0, "open": 969.0, "high": 969.0, "low": 923.2, "close": 928.8, "volume": 10000},       # 09:15
            {"timestamp": t0 + 300, "open": 928.4, "high": 929.2, "low": 918.6, "close": 923.1, "volume": 50000}, # 09:20 test
            {"timestamp": t0 + 600, "open": 923.0, "high": 926.4, "low": 912.6, "close": 914.0, "volume": 57000}, # 09:25 decisive breakdown!
            {"timestamp": t0 + 900, "open": 914.0, "high": 914.0, "low": 907.5, "close": 909.6, "volume": 77000}, # 09:30
            {"timestamp": t0 + 1200, "open": 909.0, "high": 909.9, "low": 901.5, "close": 904.9, "volume": 59000},# 09:35
            {"timestamp": t0 + 1500, "open": 904.2, "high": 907.3, "low": 900.3, "close": 901.1, "volume": 49000},# 09:40
        ]
        # Simulate afternoon bounce (15:10 IST) above morning low (925.7 > 923.2), then close at 15:25 IST (922.6)
        candles.append({"timestamp": t0 + 21000, "open": 922.0, "high": 926.0, "low": 921.9, "close": 925.7, "volume": 52000}) # 15:10 bounce
        candles.append({"timestamp": t0 + 22200, "open": 922.6, "high": 922.6, "low": 922.6, "close": 922.6, "volume": 7000})  # 15:25 tick
        
        row = {"symbol": "NAM-INDIA", "ltp": 922.6, "percent_change": -5.47}
        bo = analyze_5m_breakout(candles, "bearish", row)
        self.assertEqual(bo["breakout_time"], "09:25 AM")
        self.assertNotEqual(bo["breakout_time"], "03:25 PM")

    def test_bullish_early_breakout_time_retained_through_late_day_pullback(self):
        # Morning high at 1000.0. Decisive breakout at 09:25 AM (close 1010.0 > 1000.0)
        t0 = 1729741500
        candles = [
            {"timestamp": t0, "open": 990.0, "high": 1000.0, "low": 985.0, "close": 995.0, "volume": 10000},        # 09:15 morning high = 1000.0
            {"timestamp": t0 + 300, "open": 995.0, "high": 1000.5, "low": 992.0, "close": 998.0, "volume": 20000},  # 09:20 test
            {"timestamp": t0 + 600, "open": 998.0, "high": 1012.0, "low": 997.0, "close": 1010.0, "volume": 60000}, # 09:25 decisive breakout!
            {"timestamp": t0 + 900, "open": 1010.0, "high": 1018.0, "low": 1008.0, "close": 1015.0, "volume": 70000},
            {"timestamp": t0 + 1200, "open": 1015.0, "high": 1025.0, "low": 1012.0, "close": 1022.0, "volume": 80000},
        ]
        # Afternoon dip below morning high (995.0 < 1000.0) at 15:10 IST, then closing tick at 15:25 IST (1001.0)
        candles.append({"timestamp": t0 + 21000, "open": 1005.0, "high": 1005.0, "low": 992.0, "close": 995.0, "volume": 30000}) # 15:10 dip
        candles.append({"timestamp": t0 + 22200, "open": 996.0, "high": 1001.5, "low": 995.0, "close": 1001.0, "volume": 15000}) # 15:25 tick
        
        row = {"symbol": "TESTBULL", "ltp": 1001.0, "percent_change": 2.5}
        bo = analyze_5m_breakout(candles, "bullish", row)
        self.assertEqual(bo["breakout_time"], "09:25 AM")
        self.assertNotEqual(bo["breakout_time"], "03:25 PM")

    def test_bull_trap_trend_invalidation(self):
        # Spikes above morning high at 09:25 AM, then subsequently crashes below morning low
        t0 = 1729741500
        candles = [
            {"timestamp": t0, "open": 990.0, "high": 1000.0, "low": 950.0, "close": 995.0, "volume": 10000},        # Morning: High=1000, Low=950
            {"timestamp": t0 + 600, "open": 998.0, "high": 1015.0, "low": 998.0, "close": 1012.0, "volume": 60000}, # 09:25 Breakout spike!
            {"timestamp": t0 + 900, "open": 1010.0, "high": 1010.0, "low": 970.0, "close": 975.0, "volume": 80000},  # Dump begins
            {"timestamp": t0 + 1800, "open": 960.0, "high": 960.0, "low": 930.0, "close": 935.0, "volume": 50000}, # Breaks below morning low (935 < 950)!
            {"timestamp": t0 + 22200, "open": 940.0, "high": 970.0, "low": 940.0, "close": 965.0, "volume": 10000}, # End-of-day bounce
        ]
        row = {"symbol": "TRAPSTOCK", "ltp": 965.0, "percent_change": 0.5}
        bo = analyze_5m_breakout(candles, "bullish", row)
        self.assertTrue(bo["is_failed_trend"])
        self.assertFalse(bo["is_breakout"])
        self.assertEqual(bo["status"], "Failed Breakout")
        self.assertEqual(bo["chart_structure"], "Bull Trap Reversal")

    def test_ultracemco_bull_trap_invalidated(self):
        import json
        from pathlib import Path
        cache_path = Path("inputs/candle_cache.json")
        if not cache_path.exists():
            return
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        entry = cache.get("ULTRACEMCO")
        if not entry:
            return
        candles = entry[1]
        row = {"symbol": "ULTRACEMCO", "ltp": 11050.0, "percent_change": 0.84}
        bo = analyze_5m_breakout(candles, "bullish", row)
        self.assertTrue(bo["is_failed_trend"])
        self.assertFalse(bo["is_breakout"])
        self.assertEqual(bo["status"], "Failed Breakout")
        self.assertEqual(bo["chart_structure"], "Bull Trap Reversal")

    def test_nam_india_cache_breakout_time(self):
        import json
        from pathlib import Path
        cache_path = Path("inputs/candle_cache.json")
        if not cache_path.exists():
            return
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        entry = cache.get("NAM-INDIA")
        if not entry:
            return
        candles = entry[1]
        row = {"symbol": "NAM-INDIA", "ltp": 922.6, "percent_change": -5.47}
        bo = analyze_5m_breakout(candles, "bearish", row)
        self.assertTrue(bo["is_breakout"])
        self.assertEqual(bo["breakout_time"], "09:25 AM")


    def test_fresh_breakout_detected_within_15_minutes(self):
        # 3 candles: 09:15, 09:20, 09:25 (fresh breakout just 5m ago)
        c1_ts = int(datetime(2026, 10, 24, 9, 15, tzinfo=INDIA_TZ).timestamp())
        c2_ts = int(datetime(2026, 10, 24, 9, 20, tzinfo=INDIA_TZ).timestamp())
        c3_ts = int(datetime(2026, 10, 24, 9, 25, tzinfo=INDIA_TZ).timestamp())
        candles = [
            {"timestamp": c1_ts, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
            {"timestamp": c2_ts, "open": 101.0, "high": 101.5, "low": 100.0, "close": 101.0, "volume": 800},
            {"timestamp": c3_ts, "open": 101.0, "high": 103.5, "low": 101.0, "close": 103.0, "volume": 2000},
        ]
        row = {"symbol": "TEST", "ltp": 103.0, "percent_change": 2.0}
        bo = analyze_5m_breakout(candles, "bullish", row)
        self.assertTrue(bo["is_breakout"])
        self.assertEqual(bo["breakout_freshness"], "FRESH")
        self.assertLessEqual(bo["breakout_age_minutes"], 15)

    def test_retest_hold_detected_after_pullback_to_ema9(self):
        # Candle sequence:
        # c1 (09:15): range 100-102
        # c2 (09:20): breakout to 104
        # c3-c6 (09:25-09:40): rally to 106
        # c7-c9 (09:45-09:55): pullback touching EMA9/102.5 and holding 103
        t0 = int(datetime(2026, 10, 24, 9, 15, tzinfo=INDIA_TZ).timestamp())
        candles = [
            {"timestamp": t0, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
            {"timestamp": t0 + 300, "open": 101.0, "high": 104.0, "low": 101.0, "close": 103.5, "volume": 1500},
            {"timestamp": t0 + 600, "open": 103.5, "high": 105.0, "low": 103.0, "close": 104.5, "volume": 1200},
            {"timestamp": t0 + 900, "open": 104.5, "high": 106.0, "low": 104.0, "close": 105.5, "volume": 1300},
            {"timestamp": t0 + 1200, "open": 105.5, "high": 106.5, "low": 104.5, "close": 105.0, "volume": 1100},
            {"timestamp": t0 + 1500, "open": 105.0, "high": 105.0, "low": 103.0, "close": 103.2, "volume": 800},
            {"timestamp": t0 + 1800, "open": 103.2, "high": 103.5, "low": 102.1, "close": 102.8, "volume": 700}, # touched morning_high
            {"timestamp": t0 + 2100, "open": 102.8, "high": 104.2, "low": 102.6, "close": 104.0, "volume": 1400}, # bounced and holding
        ]
        row = {"symbol": "TEST", "ltp": 104.0, "percent_change": 3.0}
        bo = analyze_5m_breakout(candles, "bullish", row)
        self.assertTrue(bo["is_breakout"])
        self.assertEqual(bo["breakout_freshness"], "RETEST")

    def test_tight_stop_loss_in_build_trade_levels(self):
        from stock_suggestion_index import build_trade_levels
        row = {"symbol": "TEST", "ltp": 1000.0, "high": 1005.0, "low": 900.0} # day low is 900! (10% away)
        bo_info = {"tight_stop": 994.0}
        entry, stop, target, plan = build_trade_levels(row, side="bullish", bo_info=bo_info)
        # Stop loss should be tight (around 994) instead of 900!
        self.assertGreater(stop, 985.0)
        self.assertLess(entry - stop, 15.0) # Risk < 1.5%
        self.assertGreater(target, entry)


if __name__ == "__main__":
    unittest.main()
