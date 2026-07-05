"""Bullish engulfing candlestick detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, empty_result, is_bearish, is_bullish, normalize_candles, recent_trend


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    if len(clean) < 2:
        return empty_result("Bullish Engulfing", "candlestick", timeframe, len(clean))
    previous = clean[-2]
    current = clean[-1]
    prev_low = min(previous["open"], previous["close"])
    prev_high = max(previous["open"], previous["close"])
    curr_low = min(current["open"], current["close"])
    curr_high = max(current["open"], current["close"])
    detected = is_bearish(previous) and is_bullish(current) and curr_low <= prev_low and curr_high >= prev_high
    trend = recent_trend(clean[:-1], 8)
    confidence = 68 + (10 if trend == "down" else 0)
    return build_result(
        name="Bullish Engulfing",
        pattern_type="candlestick",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"engulfed_high": prev_high, "engulfed_low": prev_low},
        reason="Latest green candle engulfed the prior red body, showing short-term demand absorption.",
        timeframe=timeframe,
        bars_used=2,
    )
