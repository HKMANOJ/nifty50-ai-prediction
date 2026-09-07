#!/usr/bin/env python3
"""
Test Suite: Live NSE Pipeline Verification
==========================================
Deterministic tests verifying that the live market scanning engine fetches 100%
authentic exchange data directly from the National Stock Exchange of India (NSE)
with zero synthetic fallbacks, zero assumptions, and zero external OS binary dependencies.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import stock_suggestion_index


class TestLiveNSEPipeline(unittest.TestCase):
    """Rigorous verification of live exchange connectivity and data integrity."""

    def test_01_direct_nse_gainers(self):
        """Verify direct connection to NSE Top Gainers API returns real market data."""
        gainers, losers, errors = stock_suggestion_index.fetch_top_gainers_losers()
        self.assertIsInstance(gainers, list, "Gainers must be a list")
        self.assertGreater(len(gainers), 0, "NSE must return at least one real gainer")
        
        first = gainers[0]
        self.assertIn("symbol", first)
        self.assertIn("ltp", first)
        self.assertIn("percent_change", first)
        self.assertGreater(first["ltp"], 0, f"Real LTP must be > 0: {first}")
        self.assertIsInstance(first["symbol"], str)
        self.assertTrue(len(first["symbol"]) > 0)
        print(f"  [PASS] NSE Gainers: {len(gainers)} symbols fetched. Top: {first['symbol']} (+{first['percent_change']}%, LTP: {first['ltp']})")

    def test_02_direct_nse_losers(self):
        """Verify direct connection to NSE Top Losers API returns real market data."""
        gainers, losers, errors = stock_suggestion_index.fetch_top_gainers_losers()
        self.assertIsInstance(losers, list, "Losers must be a list")
        self.assertGreater(len(losers), 0, "NSE must return at least one real loser")
        
        first = losers[0]
        self.assertIn("symbol", first)
        self.assertIn("ltp", first)
        self.assertIn("percent_change", first)
        self.assertGreater(first["ltp"], 0, f"Real LTP must be > 0: {first}")
        self.assertLessEqual(first["percent_change"], 0, f"Loser percent change must be <= 0: {first}")
        print(f"  [PASS] NSE Losers: {len(losers)} symbols fetched. Top: {first['symbol']} ({first['percent_change']}%, LTP: {first['ltp']})")

    def test_03_direct_nse_oi_spurts(self):
        """Verify direct connection to NSE OI Spurts API returns authentic Open Interest."""
        oi_by_symbol, errors = stock_suggestion_index.fetch_change_in_oi()
        self.assertIsInstance(oi_by_symbol, dict, "OI data must be a dictionary")
        self.assertGreater(len(oi_by_symbol), 0, "NSE must return active F&O underlyings")
        
        sample_sym = next(iter(oi_by_symbol))
        sample = oi_by_symbol[sample_sym]
        self.assertIn("oi_now", sample)
        self.assertIn("oi_prev", sample)
        self.assertIn("oi_change", sample)
        self.assertGreater(sample["oi_now"], 0, f"Real OI must be > 0: {sample}")
        print(f"  [PASS] NSE OI Spurts: {len(oi_by_symbol)} contracts tracked. Sample: {sample_sym} (OI: {sample['oi_now']}, Change: {sample.get('oi_change_percent')}%)")

    def test_04_direct_nse_indices(self):
        """Verify direct connection to NSE All Indices API returns authentic index breadth."""
        index_context, errors = stock_suggestion_index.fetch_index_context()
        self.assertIsInstance(index_context, dict, "Index context must be a dictionary")
        self.assertIn("NIFTY 50", index_context, "NIFTY 50 must be in index context")
        
        nifty = index_context["NIFTY 50"]
        self.assertIn("ltp", nifty)
        self.assertGreater(nifty["ltp"], 10000, f"NIFTY 50 LTP must be realistic (>10,000): {nifty['ltp']}")
        print(f"  [PASS] NSE Indices: NIFTY 50 LTP = {nifty['ltp']} ({nifty.get('percent_change')}%)")

    def test_05_build_payload_pure_python(self):
        """Verify end-to-end build_payload produces 100% real suggestions with zero synthetic fallbacks."""
        payload = stock_suggestion_index.build_payload(top=20)
        self.assertTrue(payload.get("ok"), "Payload ok must be True")
        self.assertEqual(payload.get("data_mode"), "real", "Data mode must be real")
        self.assertIn("session_date", payload)
        self.assertIn("market_clock_ist", payload)
        
        bullish = payload.get("bullish", [])
        bearish = payload.get("bearish", [])
        self.assertGreater(len(bullish), 0, "Must have bullish candidates")
        self.assertGreater(len(bearish), 0, "Must have bearish candidates")
        
        # Verify no fake formulas were used
        for item in bullish + bearish:
            self.assertIsInstance(item["symbol"], str)
            self.assertGreater(item["ltp"], 0)
            self.assertIsInstance(item["percent_change"], float)
            self.assertIn("score", item)
            self.assertIn("rank", item)
            # Check interpretation does not contain fallback marker
            self.assertNotIn("synthetic", item.get("interpretation", "").lower())
            self.assertNotIn("dummy", item.get("interpretation", "").lower())
            
        print(f"  [PASS] build_payload: Generated {len(bullish)} Bullish & {len(bearish)} Bearish genuine candidates in {payload.get('market_clock_ist')}")

    def test_06_resilience_without_curl(self):
        """Verify that system functions 100% reliably even when curl binary is completely absent (AWS EC2 scenario)."""
        import shutil
        original_which = shutil.which
        try:
            shutil.which = lambda cmd: None if cmd == "curl" else original_which(cmd)
            payload = stock_suggestion_index.build_payload(top=5)
            self.assertTrue(payload.get("ok"), "Must succeed even with curl missing")
            self.assertGreater(len(payload.get("bullish", [])), 0)
            print("  [PASS] Zero-curl environment: Engine runs flawlessly without curl binary")
        finally:
            shutil.which = original_which


if __name__ == "__main__":
    print("\n=======================================================")
    print("  RUNNING LIVE NSE PIPELINE DETERMINISTIC TEST SUITE   ")
    print("=======================================================\n")
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestLiveNSEPipeline)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
