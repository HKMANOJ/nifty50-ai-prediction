"""Doji candlestick detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import body, build_result, candle_range, empty_result, normalize_candles


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    if not clean:
        return empty_result("Doji", "candlestick", timeframe, 0)
    candle = clean[-1]
    total_range = candle_range(candle)
    detected = total_range > 0 and body(candle) <= total_range * 0.10
    return build_result(
        name="Doji",
        pattern_type="candlestick",
        direction="neutral",
        detected=detected,
        confidence=56,
        levels={"high": candle["high"], "low": candle["low"]},
        reason="Latest candle has a very small body, showing short-term indecision.",
        timeframe=timeframe,
        bars_used=1,
    )
