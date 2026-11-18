import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
import stock_suggestion_index as ssi
from stock_suggestion_index import (
    build_suggestions,
    enforce_candidate_lifecycle_and_mutual_exclusion,
    Suggestion,
    INDIA_TZ,
)
from test_candidate_lifecycle import make_test_suggestion


class TestDemandFreshAndBearOBAlerts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lifecycle_path = Path(self.temp_dir.name) / "candidate_lifecycle.json"
        self.patcher = patch.object(ssi, "CANDIDATE_LIFECYCLE_PATH", self.lifecycle_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()
    def test_bullish_demand_fresh_triggers_bell(self):
        """Bullish stock with DEMAND FRESH and valid timestamp must set bell_rang=True."""
        t0 = int(datetime(2026, 11, 11, 9, 15, tzinfo=INDIA_TZ).timestamp())
        candles = [
            {"timestamp": t0, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10000},
            {"timestamp": t0 + 300, "open": 101.0, "high": 103.0, "low": 100.0, "close": 102.5, "volume": 12000},
            {"timestamp": t0 + 600, "open": 102.5, "high": 103.0, "low": 98.0, "close": 98.5, "volume": 15000},
            {"timestamp": t0 + 900, "open": 98.5, "high": 99.0, "low": 97.5, "close": 98.0, "volume": 20000}, # swing low / demand OB at 09:30 AM
            {"timestamp": t0 + 1200, "open": 98.0, "high": 101.0, "low": 98.0, "close": 100.5, "volume": 50000},
            {"timestamp": t0 + 1500, "open": 100.5, "high": 104.0, "low": 100.0, "close": 103.8, "volume": 80000}, # BOS
            {"timestamp": t0 + 1800, "open": 103.8, "high": 105.0, "low": 103.5, "close": 104.5, "volume": 60000},
            {"timestamp": t0 + 2100, "open": 104.5, "high": 105.5, "low": 104.0, "close": 105.0, "volume": 40000},
        ]
        row = {
            "symbol": "UNOMINDA",
            "ltp": 105.0,
            "percent_change": 3.13,
            "high": 105.5,
            "low": 97.5,
            "prev_close": 101.8,
            "oi_change_percent": 12.0,
            "oi_change": 50000.0,
            "lot_size": 550,
            "volume": 200000,
            "value_lakhs": 5000,
        }
        res, _ = build_suggestions(
            [row],
            {"UNOMINDA": row},
            side="bullish",
            top=5,
            index_context={},
            candles_by_symbol={"UNOMINDA": candles},
        )
        self.assertEqual(len(res), 1)
        item = res[0]
        self.assertTrue(item.is_demand_fresh)
        self.assertEqual(item.demand_fresh_time, "09:35 AM")
        self.assertTrue(item.bell_rang, "DEMAND · FRESH stock must ring bell")

    def test_bullish_without_demand_fresh_suppresses_bell(self):
        """Bullish stock with generic breakout but NO demand fresh must have bell_rang=False."""
        t0 = int(datetime(2026, 11, 11, 9, 15, tzinfo=INDIA_TZ).timestamp())
        # Linear rising candles with no base / swing low order block
        candles = [
            {"timestamp": t0 + (i * 300), "open": 100.0 + i, "high": 101.0 + i, "low": 99.8 + i, "close": 100.8 + i, "volume": 10000}
            for i in range(8)
        ]
        row = {
            "symbol": "INDIGO",
            "ltp": 107.8,
            "percent_change": 4.77,
            "high": 108.0,
            "low": 99.8,
            "prev_close": 102.8,
            "oi_change_percent": 15.0,
            "oi_change": 60000.0,
            "lot_size": 150,
            "volume": 200000,
            "value_lakhs": 5000,
        }
        res, _ = build_suggestions(
            [row],
            {"INDIGO": row},
            side="bullish",
            top=5,
            index_context={},
            candles_by_symbol={"INDIGO": candles},
        )
        self.assertEqual(len(res), 1)
        item = res[0]
        self.assertFalse(item.is_demand_fresh)
        self.assertFalse(item.bell_rang, "Generic breakout without DEMAND · FRESH must NEVER ring bell")

    def test_bearish_bear_ob_fresh_triggers_bell(self):
        """Bearish stock with BEAR OB FRESH and valid timestamp must set bell_rang=True."""
        t0 = int(datetime(2026, 11, 11, 9, 15, tzinfo=INDIA_TZ).timestamp())
        candles = [
            {"timestamp": t0, "open": 100.0, "high": 100.5, "low": 98.0, "close": 98.5, "volume": 10000},
            {"timestamp": t0 + 300, "open": 98.5, "high": 99.0, "low": 97.0, "close": 97.5, "volume": 12000},
            {"timestamp": t0 + 600, "open": 97.5, "high": 103.0, "low": 97.0, "close": 102.5, "volume": 15000},
            {"timestamp": t0 + 900, "open": 102.5, "high": 104.0, "low": 102.0, "close": 103.5, "volume": 20000}, # swing high / supply OB at 09:30 AM
            {"timestamp": t0 + 1200, "open": 103.5, "high": 103.5, "low": 99.0, "close": 99.5, "volume": 50000},
            {"timestamp": t0 + 1500, "open": 99.5, "high": 100.0, "low": 96.0, "close": 96.5, "volume": 80000}, # BOS down
            {"timestamp": t0 + 1800, "open": 96.5, "high": 97.0, "low": 95.0, "close": 95.5, "volume": 60000},
            {"timestamp": t0 + 2100, "open": 95.5, "high": 96.0, "low": 94.0, "close": 94.5, "volume": 40000},
        ]
        row = {
            "symbol": "MAXHEALTH",
            "ltp": 94.5,
            "percent_change": -3.2,
            "high": 104.0,
            "low": 94.0,
            "prev_close": 97.6,
            "oi_change_percent": 10.0,
            "oi_change": 40000.0,
            "lot_size": 525,
            "volume": 200000,
            "value_lakhs": 5000,
        }
        res, _ = build_suggestions(
            [row],
            {"MAXHEALTH": row},
            side="bearish",
            top=5,
            index_context={},
            candles_by_symbol={"MAXHEALTH": candles},
        )
        self.assertEqual(len(res), 1)
        item = res[0]
        self.assertTrue(item.is_supply_zone)
        self.assertEqual(item.supply_zone_time, "09:35 AM")
        self.assertTrue(item.bell_rang, "BEAR OB · FRESH stock must ring bell")

    def test_bearish_without_bear_ob_fresh_suppresses_bell(self):
        """Bearish stock with generic breakdown but NO bear OB fresh must have bell_rang=False."""
        t0 = int(datetime(2026, 11, 11, 9, 15, tzinfo=INDIA_TZ).timestamp())
        # Linear declining candles with no supply order block
        candles = [
            {"timestamp": t0 + (i * 300), "open": 100.0 - i, "high": 100.2 - i, "low": 99.0 - i, "close": 99.2 - i, "volume": 10000}
            for i in range(8)
        ]
        row = {
            "symbol": "MFSL",
            "ltp": 92.2,
            "percent_change": -1.91,
            "high": 100.2,
            "low": 92.0,
            "prev_close": 94.0,
            "oi_change_percent": 8.0,
            "oi_change": 30000.0,
            "lot_size": 400,
            "volume": 200000,
            "value_lakhs": 5000,
        }
        res, _ = build_suggestions(
            [row],
            {"MFSL": row},
            side="bearish",
            top=5,
            index_context={},
            candles_by_symbol={"MFSL": candles},
        )
        self.assertEqual(len(res), 1)
        item = res[0]
        self.assertFalse(item.is_supply_zone)
        self.assertFalse(item.bell_rang, "Generic breakdown without BEAR OB · FRESH must NEVER ring bell")

    def test_lifecycle_timestamps_match_order_block_time(self):
        """Lifecycle engine records the exact order block time into alert_created_at."""
        t0 = int(datetime(2026, 11, 11, 9, 15, tzinfo=INDIA_TZ).timestamp())
        candles = [
            {"timestamp": t0, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10000},
            {"timestamp": t0 + 300, "open": 101.0, "high": 103.0, "low": 100.0, "close": 102.5, "volume": 12000},
            {"timestamp": t0 + 600, "open": 102.5, "high": 103.0, "low": 98.0, "close": 98.5, "volume": 15000},
            {"timestamp": t0 + 900, "open": 98.5, "high": 99.0, "low": 97.5, "close": 98.0, "volume": 20000},
            {"timestamp": t0 + 1200, "open": 98.0, "high": 101.0, "low": 98.0, "close": 100.5, "volume": 50000},
            {"timestamp": t0 + 1500, "open": 100.5, "high": 104.0, "low": 100.0, "close": 103.8, "volume": 80000},
            {"timestamp": t0 + 1800, "open": 103.8, "high": 105.0, "low": 103.5, "close": 104.5, "volume": 60000},
            {"timestamp": t0 + 2100, "open": 104.5, "high": 105.5, "low": 104.0, "close": 105.0, "volume": 40000},
        ]
        row = {
            "symbol": "POLICYBZR",
            "ltp": 105.0,
            "percent_change": 3.02,
            "high": 105.5,
            "low": 97.5,
            "prev_close": 101.8,
            "oi_change_percent": 12.0,
            "oi_change": 50000.0,
            "lot_size": 350,
            "volume": 200000,
            "value_lakhs": 5000,
        }
        res, _ = build_suggestions(
            [row],
            {"POLICYBZR": row},
            side="bullish",
            top=5,
            index_context={},
            candles_by_symbol={"POLICYBZR": candles},
        )
        self.assertEqual(len(res), 1)
        item = res[0]
        self.assertTrue(item.bell_rang)
        self.assertEqual(item.demand_fresh_time, "09:35 AM")

        bull, bear = enforce_candidate_lifecycle_and_mutual_exclusion(
            session_date="2026-11-11",
            market_clock_ist="10:00:00",
            bullish=[item],
            bearish=[],
            now_epoch=1794373200.0,
        )
        self.assertEqual(len(bull), 1)
        self.assertTrue(bull[0].should_alert)
        self.assertEqual(bull[0].alert_created_at, "09:35 AM")


if __name__ == "__main__":
    unittest.main()
