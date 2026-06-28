"""Trendline break detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, fit_line, line_value, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    highs = swings["highs"][-4:]
    lows = swings["lows"][-4:]
    if len(highs) < 2 and len(lows) < 2:
        return empty_result("Trendline Break", "breakout", timeframe, len(clean))
    last_index = clean[-1]["_index"]
    close = clean[-1]["close"]
    tolerance = price_tolerance(clean, 0.0015)
    high_line = fit_line(highs) if len(highs) >= 2 else None
    low_line = fit_line(lows) if len(lows) >= 2 else None
    broke_above = bool(high_line and close > line_value(high_line, last_index) + tolerance)
    broke_below = bool(low_line and close < line_value(low_line, last_index) - tolerance)
    detected = broke_above or broke_below
    direction = "bullish" if broke_above else "bearish" if broke_below else "neutral"
    levels = {}
    if high_line:
        levels["upper_trendline"] = line_value(high_line, last_index)
    if low_line:
        levels["lower_trendline"] = line_value(low_line, last_index)
    levels["break"] = "up" if broke_above else "down" if broke_below else "none"
    return build_result(
        name="Trendline Break",
        pattern_type="breakout",
        direction=direction,
        detected=detected,
        confidence=74,
        levels=levels,
        reason="Latest close broke a recent swing trendline, improving directional follow-through odds.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
