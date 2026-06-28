"""Hammer candlestick detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import body, build_result, candle_range, empty_result, lower_wick, normalize_candles, recent_trend, upper_wick


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    if not clean:
        return empty_result("Hammer", "candlestick", timeframe, 0)
    candle = clean[-1]
    total_range = candle_range(candle)
    candle_body = body(candle)
    detected = (
        total_range > 0
        and candle_body <= total_range * 0.35
        and lower_wick(candle) >= max(candle_body * 2.0, total_range * 0.45)
        and upper_wick(candle) <= total_range * 0.25
    )
    trend = recent_trend(clean[:-1], 10)
    confidence = 62 + (12 if trend == "down" else 0)
    return build_result(
        name="Hammer",
        pattern_type="candlestick",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"low_rejection": candle["low"], "trigger": candle["high"]},
        reason="Long lower wick shows buyers rejected lower prices near the latest candle low.",
        timeframe=timeframe,
        bars_used=1,
    )
