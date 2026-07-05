"""Symmetrical triangle compression detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, fit_line, line_value, normalize_candles


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    highs = swings["highs"][-4:]
    lows = swings["lows"][-4:]
    if len(highs) < 2 or len(lows) < 2:
        return empty_result("Symmetrical Triangle", "compression", timeframe, len(clean))
    high_line = fit_line(highs)
    low_line = fit_line(lows)
    if not high_line or not low_line:
        return empty_result("Symmetrical Triangle", "compression", timeframe, len(clean))
    last_index = clean[-1]["_index"]
    upper = line_value(high_line, last_index)
    lower = line_value(low_line, last_index)
    close = clean[-1]["close"]
    detected = high_line[0] < 0 and low_line[0] > 0 and upper > lower
    if close > upper:
        direction = "bullish"
        breakout = "up"
    elif close < lower:
        direction = "bearish"
        breakout = "down"
    else:
        direction = "neutral"
        breakout = "inside"
    confidence = 60 + (14 if breakout != "inside" else 0)
    return build_result(
        name="Symmetrical Triangle",
        pattern_type="compression",
        direction=direction,
        detected=detected,
        confidence=confidence,
        levels={"upper_trendline": upper, "lower_trendline": lower, "breakout": breakout},
        reason="Lower highs and higher lows show volatility compression; trade direction improves after breakout.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
