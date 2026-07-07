"""Fibonacci Retracement and previous session consolidation range detector."""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from collections import defaultdict


class FibonacciEngine:
    """
    Analyzes historical and intraday candle structures to extract:
    1. Previous day's consolidation range (value area bounds).
    2. Active trend leg swing high/low points.
    3. Fibonacci retracement levels.
    """

    def __init__(self, lookback_period: int = 100):
        self.lookback_period = lookback_period

    def get_market_date(self, candle: Dict[str, Any]) -> str:
        """Helper to extract or parse market date from candle."""
        if "market_date" in candle and candle["market_date"]:
            return str(candle["market_date"])
        if "market_time" in candle and candle["market_time"]:
            return str(candle["market_time"]).split("T")[0]
        return ""

    def analyze_structure(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyzes the candle series and returns a structural dictionary.
        """
        result = {
            "consolidation_detected": False,
            "consolidation_high": 0.0,
            "consolidation_low": 0.0,
            "prev_day_high": 0.0,
            "prev_day_low": 0.0,
            "active_leg_direction": "flat",
            "active_leg_low": 0.0,
            "active_leg_high": 0.0,
            "fib_levels": {},
            "near_fib_level": None,
            "breakout_status": "inside"
        }

        if not candles:
            return result

        # Group candles by date
        candles_by_date = defaultdict(list)
        for c in candles:
            date_str = self.get_market_date(c)
            if date_str:
                candles_by_date[date_str].append(c)

        # Sort dates chronologically
        sorted_dates = sorted(candles_by_date.keys())
        if len(sorted_dates) < 1:
            return result

        current_date = sorted_dates[-1]
        
        # 1. Previous Day Consolidation Box Calculation
        prev_day_candles = []
        if len(sorted_dates) >= 2:
            prev_date = sorted_dates[-2]
            prev_day_candles = candles_by_date[prev_date]

        if prev_day_candles:
            closes = sorted([float(c["close"]) for c in prev_day_candles])
            highs = [float(c["high"]) for c in prev_day_candles]
            lows = [float(c["low"]) for c in prev_day_candles]
            
            if closes:
                # 25th to 75th percentiles represent the consolidation value area
                result["consolidation_low"] = round(closes[len(closes) // 4], 2)
                result["consolidation_high"] = round(closes[(3 * len(closes)) // 4], 2)
                result["consolidation_detected"] = True
                result["prev_day_high"] = round(max(highs), 2)
                result["prev_day_low"] = round(min(lows), 2)

        # 2. Identify the active trend leg (impluse move)
        # Look across the last lookback_period candles (up to current index)
        window = candles[-self.lookback_period:]
        if len(window) >= 10:
            lows = [float(c["low"]) for c in window]
            highs = [float(c["high"]) for c in window]
            
            min_val = min(lows)
            max_val = max(highs)
            
            min_idx = lows.index(min_val)
            max_idx = highs.index(max_val)
            
            height = max_val - min_val
            
            # Leg must be at least 25 points to be considered a structural impulse move
            if height >= 25.0:
                result["active_leg_low"] = round(min_val, 2)
                result["active_leg_high"] = round(max_val, 2)
                
                if min_idx < max_idx:
                    # Bullish trend leg (low to high)
                    result["active_leg_direction"] = "up"
                    result["fib_levels"] = {
                        "fib_0": round(max_val, 2),
                        "fib_236": round(max_val - height * 0.236, 2),
                        "fib_382": round(max_val - height * 0.382, 2),
                        "fib_500": round(max_val - height * 0.500, 2),
                        "fib_618": round(max_val - height * 0.618, 2),
                        "fib_786": round(max_val - height * 0.786, 2),
                        "fib_100": round(min_val, 2)
                    }
                else:
                    # Bearish trend leg (high to low)
                    result["active_leg_direction"] = "down"
                    result["fib_levels"] = {
                        "fib_0": round(min_val, 2),
                        "fib_236": round(min_val + height * 0.236, 2),
                        "fib_382": round(min_val + height * 0.382, 2),
                        "fib_500": round(min_val + height * 0.500, 2),
                        "fib_618": round(min_val + height * 0.618, 2),
                        "fib_786": round(min_val + height * 0.786, 2),
                        "fib_100": round(max_val, 2)
                    }

        # 3. Check current price relations
        current_price = float(candles[-1]["close"])
        
        # Consolidation Box breakout status
        if result["consolidation_detected"]:
            if current_price > result["consolidation_high"]:
                result["breakout_status"] = "breakout_above"
            elif current_price < result["consolidation_low"]:
                result["breakout_status"] = "breakdown_below"
            else:
                result["breakout_status"] = "inside"

        # Check if price is near a Fibonacci level (within 8.0 points tolerance)
        if result["fib_levels"]:
            min_dist = 9999.0
            nearest_lbl = None
            for lbl, val in result["fib_levels"].items():
                dist = abs(current_price - val)
                if dist < min_dist:
                    min_dist = dist
                    nearest_lbl = lbl
            if min_dist <= 8.0:
                result["near_fib_level"] = {
                    "level": nearest_lbl,
                    "price": result["fib_levels"][nearest_lbl],
                    "distance": round(min_dist, 2)
                }

        return result
