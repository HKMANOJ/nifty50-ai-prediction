import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from stock_suggestion_index import analyze_5m_breakout, assess_one_side_rally, compute_daily_top5, Suggestion

INDIA_TZ = ZoneInfo("Asia/Kolkata")

class TestEarlyDetectionEngine(unittest.TestCase):
    def test_live_forming_wick_breakout(self):
        # Candle 1 (09:15): High = 1000, Low = 980
        # Candle 2 (09:20): Close = 995, but live tick LTP = 1003 (pierces morning high)
        today = datetime.now(INDIA_TZ).date()
        t0 = int(datetime(today.year, today.month, today.day, 9, 15, tzinfo=INDIA_TZ).timestamp())
        candles = [
            {"timestamp": t0, "open": 985.0, "high": 1000.0, "low": 980.0, "close": 990.0, "volume": 1000},
            {"timestamp": t0 + 300, "open": 990.0, "high": 995.0, "low": 988.0, "close": 995.0, "volume": 800},
        ]
        # Live forming tick has penetrated above 1000 to 1003
        row = {"symbol": "WICKSTOCK", "ltp": 1003.0, "high": 1004.0, "open": 985.0, "percent_change": 1.8}
        bo = analyze_5m_breakout(candles, "bullish", row)
        self.assertTrue(bo["is_breakout"], "Live wick penetrating morning high must trigger breakout")
        self.assertEqual(bo["status"], "Impulse BO")
        self.assertEqual(bo["chart_structure"], "Live Wick Breakout")

    def test_velocity_ignition_early_momentum(self):
        # Stock surges +1.2% in first 10 minutes with volume while candle close is consolidating near EMA
        today = datetime.now(INDIA_TZ).date()
        t0 = int(datetime(today.year, today.month, today.day, 9, 15, tzinfo=INDIA_TZ).timestamp())
        candles = [
            {"timestamp": t0, "open": 1000.0, "high": 1005.0, "low": 998.0, "close": 1002.0, "volume": 5000},
            {"timestamp": t0 + 300, "open": 1002.0, "high": 1004.0, "low": 1001.0, "close": 1003.0, "volume": 6000},
        ]
        # High velocity run: LTP = 1012 (+1.2% from open 1000)
        row = {"symbol": "FASTSTOCK", "ltp": 1012.0, "high": 1015.0, "open": 1000.0, "percent_change": 1.2}
        bo = analyze_5m_breakout(candles, "bullish", row)
        self.assertTrue(bo["is_breakout"])
        self.assertIn(bo["chart_structure"], ["Velocity Ignition", "Impulse BO", "Live Wick Breakout"])

    def test_bear_trap_v_reversal_unlock(self):
        # Stock opened gap down (-1.0%), but has surged +0.8% above open
        row = {
            "symbol": "REVSTOCK",
            "percent_change": -0.2, # still slightly negative vs yesterday
            "open": 1000.0,
            "ltp": 1010.0, # +1.0% above open
            "high": 1012.0,
            "low": 995.0,
            "volume": 200000,
        }
        oi = {"oi_change": 5.0}
        rally = assess_one_side_rally(row, oi, side="bullish", activity_ratio=1.5, index_context={})
        self.assertTrue(rally["is_one_side"], "V-reversal stock with +1% move above open must pass rally gate")
        self.assertIn("bear-trap reversal rally", rally["quality_tags"])

    def test_top5_entry_timing_classification(self):
        s_early = Suggestion(
            symbol="EARLY", side="bullish", rank=1, setup="Long buildup", trade_plan="Buy",
            ltp=2000.0, percent_change=3.0, oi_change_percent=5.0, oi_change=100.0,
            volume=50000.0, value_lakhs=100.0, entry=1950.0, stop_loss=1930.0, target=2050.0,
            score=90, one_side_rally_score=90, rally_type="One-side bullish rally",
            range_position_percent=80.0, move_from_open_percent=2.5, index_context="Nifty bullish",
            quality_tags=["volume-driven rally"], status="Priority Rally", reason="Test",
            breakout_time="09:25 AM", five_min_status="Impulse BO", is_breakout=True
        )
        s_late = Suggestion(
            symbol="LATE", side="bullish", rank=2, setup="Long buildup", trade_plan="Buy",
            ltp=1500.0, percent_change=1.5, oi_change_percent=3.0, oi_change=50.0,
            volume=30000.0, value_lakhs=80.0, entry=1480.0, stop_loss=1460.0, target=1550.0,
            score=80, one_side_rally_score=80, rally_type="One-side bullish rally",
            range_position_percent=70.0, move_from_open_percent=1.2, index_context="Nifty bullish",
            quality_tags=["volume-driven rally"], status="Priority Rally", reason="Test",
            breakout_time="10:30 AM", five_min_status="Breakout", is_breakout=True
        )
        top5 = compute_daily_top5([s_early, s_late], [], {}, [])
        bullish = top5["bullish"]
        early_cand = next(x for x in bullish if x["symbol"] == "EARLY")
        late_cand = next(x for x in bullish if x["symbol"] == "LATE")
        self.assertEqual(early_cand["entry_timing"], "EARLY IGNITION ⚡")
        self.assertEqual(late_cand["entry_timing"], "CONTINUATION 🔄")
        self.assertEqual(early_cand["entry_price"], 1950.0)

if __name__ == "__main__":
    unittest.main()
