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



if __name__ == "__main__":
    unittest.main()
