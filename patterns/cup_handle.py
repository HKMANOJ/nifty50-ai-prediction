"""Cup and handle detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, empty_result, is_near, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    if len(clean) < 32:
        return empty_result("Cup and Handle", "continuation", timeframe, len(clean))
    window = clean[-40:] if len(clean) >= 40 else clean
    midpoint = len(window) // 2
    left_high = max(window[:midpoint], key=lambda candle: candle["high"])
    trough = min(window, key=lambda candle: candle["low"])
    right_high = max(window[midpoint:], key=lambda candle: candle["high"])
    tolerance = price_tolerance(window, 0.004)
    handle = window[-8:]
    handle_low = min(candle["low"] for candle in handle)
    rim = min(left_high["high"], right_high["high"])
    shallow_handle = handle_low > trough["low"] + ((rim - trough["low"]) * 0.45)
    detected = trough["_index"] > left_high["_index"] and trough["_index"] < right_high["_index"] and is_near(left_high["high"], right_high["high"], tolerance * 2.0) and shallow_handle
    breakout = clean[-1]["close"] > rim + (tolerance * 0.2)
    confidence = 60 + (18 if breakout else 0)
    return build_result(
        name="Cup and Handle",
        pattern_type="continuation",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"rim": rim, "cup_low": trough["low"], "handle_low": handle_low, "breakout": breakout},
        reason="Rounded recovery with shallow handle can support bullish continuation after rim breakout.",
        timeframe=timeframe,
        bars_used=len(window),
    )
