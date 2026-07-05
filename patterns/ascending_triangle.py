"""Ascending triangle continuation detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, is_near, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    highs = swings["highs"][-4:]
    lows = swings["lows"][-4:]
    if len(highs) < 2 or len(lows) < 2:
        return empty_result("Ascending Triangle", "continuation", timeframe, len(clean))
    tolerance = price_tolerance(clean, 0.003)
    resistance = max(high["price"] for high in highs)
    flat_tops = sum(1 for high in highs if is_near(high["price"], resistance, tolerance)) >= 2
    rising_lows = lows[-1]["price"] > lows[0]["price"] + (tolerance * 0.4)
    breakout = clean[-1]["close"] > resistance + (tolerance * 0.2)
    detected = flat_tops and rising_lows
    confidence = 64 + (14 if breakout else 0)
    return build_result(
        name="Ascending Triangle",
        pattern_type="continuation",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"resistance": resistance, "last_higher_low": lows[-1]["price"], "breakout": breakout},
        reason="Flat resistance with rising lows shows buyers pressing into supply.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
