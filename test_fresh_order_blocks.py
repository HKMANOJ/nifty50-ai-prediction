from dataclasses import replace
from datetime import datetime
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
    def test_zone_lifecycle_for_both_sides(self):
        for bullish in (True, False):
            with self.subTest(bullish=bullish):
                bars = demand_candles()
                untouched = bars + [candle(i, 107, 108, 104, 107) for i in range(5, 15)]
                tested = untouched + [candle(15, 107, 107, 101, 104)]
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


if __name__ == "__main__":
    unittest.main()
