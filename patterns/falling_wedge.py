"""Falling wedge detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, fit_line, line_value, normalize_candles


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    highs = swings["highs"][-4:]
    lows = swings["lows"][-4:]
    if len(highs) < 2 or len(lows) < 2:
        return empty_result("Falling Wedge", "reversal", timeframe, len(clean))
    high_line = fit_line(highs)
    low_line = fit_line(lows)
    if not high_line or not low_line:
        return empty_result("Falling Wedge", "reversal", timeframe, len(clean))
    last_index = clean[-1]["_index"]
    upper = line_value(high_line, last_index)
    detected = high_line[0] < 0 and low_line[0] < 0 and high_line[0] < low_line[0]
    breakout = clean[-1]["close"] > upper
    confidence = 62 + (16 if breakout else 0)
    return build_result(
        name="Falling Wedge",
        pattern_type="reversal",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"upper_trendline": upper, "breakout": breakout, "high_slope": high_line[0], "low_slope": low_line[0]},
        reason="Price is falling in a narrowing channel, which often warns of downside exhaustion.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
