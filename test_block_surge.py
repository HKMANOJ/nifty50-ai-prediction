import time
import unittest
from stock_suggestion_index import (
    detect_institutional_block_surge,
    Suggestion,
)


class TestInstitutionalBlockSurge(unittest.TestCase):
    def test_1m_institutional_block_surge_ather_scenario(self):
        """Simulates Ather Energy at 02:21 PM:
        Baseline 1m volume: ~3,800 shares/min at ₹1,410 (₹0.5 Cr).
        Institutional block: 185,000 shares at ₹1,415 (₹26.2 Cr turnover, ~48x RVOL).
        """
        now = int(time.time())
        candles_1m = []
        
        # 20 baseline 1-minute candles
        for i in range(20):
            t = now - (20 - i) * 60
            candles_1m.append({
                "timestamp": t,
                "open": 1408.0 + (i * 0.1),
                "high": 1410.0,
                "low": 1407.5,
                "close": 1409.0 + (i * 0.1),
                "volume": 3800.0,
                "oi": 1200000.0,
            })
            
        # The Block Surge 1-minute candle
        surge_ts = now
        candles_1m.append({
            "timestamp": surge_ts,
            "open": 1410.0,
            "high": 1418.0,
            "low": 1409.5,
            "close": 1415.0,
            "volume": 185000.0,  # 185k shares
            "oi": 1250000.0,
        })

        res = detect_institutional_block_surge(
            symbol="ATHERENERG",
            candles_1m=candles_1m,
            candles_5m=[],
            side="bullish",
            row={"ltp": 1415.0, "percent_change": 4.8},
        )

        self.assertTrue(res["is_block_surge"])
        self.assertIn("BUY", res["block_surge_type"])
        self.assertGreaterEqual(res["block_surge_turnover_cr"], 25.0)
        self.assertGreaterEqual(res["block_surge_rvol"], 35.0)
        self.assertEqual(res["block_surge_vol"], 185000)

    def test_normal_volume_no_false_positive(self):
        """Verify that ordinary retail trading with moderate volume does NOT trigger block surge."""
        now = int(time.time())
        candles_1m = []
        for i in range(25):
            t = now - (25 - i) * 60
            candles_1m.append({
                "timestamp": t,
                "open": 500.0,
                "high": 501.5,
                "low": 499.5,
                "close": 501.0,
                "volume": 4500.0,  # Turnover = 4500 * 500 = ₹22.5 Lakhs (0.22 Cr)
                "oi": 500000.0,
            })

        res = detect_institutional_block_surge(
            symbol="NORMALSTK",
            candles_1m=candles_1m,
            candles_5m=[],
            side="bullish",
            row={"ltp": 501.0, "percent_change": 1.2},
        )

        self.assertFalse(res["is_block_surge"])
        self.assertEqual(res["block_surge_turnover_cr"], 0.0)

    def test_5m_fallback_institutional_surge(self):
        """When 1-minute data is unavailable, verify 5-minute fallback triggers on multi-crore block trade."""
        now = int(time.time())
        candles_5m = []
        for i in range(10):
            t = now - (10 - i) * 300
            candles_5m.append({
                "timestamp": t,
                "open": 1400.0,
                "high": 1405.0,
                "low": 1395.0,
                "close": 1402.0,
                "volume": 18000.0,  # ~₹2.5 Cr turnover
            })

        # Massive 5m candle (665,000 shares @ ₹1,488 = ~₹98.9 Cr)
        candles_5m.append({
            "timestamp": now,
            "open": 1409.0,
            "high": 1495.0,
            "low": 1408.0,
            "close": 1488.6,
            "volume": 665243.0,
        })

        res = detect_institutional_block_surge(
            symbol="ATHERENERG",
            candles_1m=[],
            candles_5m=candles_5m,
            side="bullish",
            row={"ltp": 1488.6, "percent_change": 10.5},
        )

        self.assertTrue(res["is_block_surge"])
        self.assertGreaterEqual(res["block_surge_turnover_cr"], 90.0)
        self.assertGreaterEqual(res["block_surge_rvol"], 25.0)

    def test_institutional_fast_track_priority_sorting(self):
        """Verify that a stock with an active Institutional Block Surge (e.g. +4.5%, ₹26 Cr surge)
        is ranked Rank #1 ahead of ordinary breakout stocks with higher percentage gains (+6.0%).
        """
        s1 = Suggestion(
            symbol="ORDINARY_GAIN",
            side="GAINER",
            rank=0,
            setup="Long buildup",
            trade_plan="MOMENTUM_RUN",
            ltp=500.0,
            percent_change=6.2,  # Higher % change
            oi_change_percent=2.5,
            oi_change=10000.0,
            volume=250000.0,
            value_lakhs=1250.0,
            entry=495.0,
            stop_loss=490.0,
            target=520.0,
            score=75,
            one_side_rally_score=75,
            rally_type="Momentum",
            range_position_percent=85.0,
            move_from_open_percent=3.2,
            index_context="NIFTY Bullish",
            quality_tags=["5m Breakout"],
            status="Review",
            reason="Breakout",
            is_breakout=True,
            is_block_surge=False,
        )

        s2 = Suggestion(
            symbol="ATHERENERG",
            side="GAINER",
            rank=0,
            setup="Long buildup",
            trade_plan="MOMENTUM_RUN",
            ltp=1415.0,
            percent_change=4.8,  # Lower % change
            oi_change_percent=5.0,
            oi_change=50000.0,
            volume=185000.0,
            value_lakhs=2620.0,
            entry=1410.0,
            stop_loss=1390.0,
            target=1480.0,
            score=95,
            one_side_rally_score=95,
            rally_type="Momentum",
            range_position_percent=92.0,
            move_from_open_percent=3.6,
            index_context="NIFTY Bullish",
            quality_tags=["⚡ BLOCK SURGE"],
            status="Priority Rally",
            reason="⚡ BLOCK SURGE",
            is_breakout=True,
            is_block_surge=True,
            block_surge_turnover_cr=26.2,
            block_surge_rvol=48.5,
        )

        suggestions = [s1, s2]

        def sort_key_bullish(item: Suggestion):
            if item.is_block_surge:
                return (0, -(item.block_surge_turnover_cr or 0.0), -(item.percent_change or -9999))
            elif item.is_breakout or item.is_cascade:
                return (1, 0.0, -(item.percent_change or -9999))
            elif "near" in (item.five_min_status or "").lower():
                return (2, 0.0, -(item.percent_change or -9999))
            else:
                return (3, 0.0, -(item.percent_change or -9999))

        suggestions.sort(key=sort_key_bullish)

        self.assertEqual(suggestions[0].symbol, "ATHERENERG")
        self.assertEqual(suggestions[1].symbol, "ORDINARY_GAIN")
        self.assertTrue(suggestions[0].to_dict()["is_block_surge"])


if __name__ == "__main__":
    unittest.main()
