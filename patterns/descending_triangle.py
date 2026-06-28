"""Descending triangle continuation detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, is_near, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    highs = swings["highs"][-4:]
    lows = swings["lows"][-4:]
    if len(highs) < 2 or len(lows) < 2:
        return empty_result("Descending Triangle", "continuation", timeframe, len(clean))
    tolerance = price_tolerance(clean, 0.003)
    support = min(low["price"] for low in lows)
    flat_bottoms = sum(1 for low in lows if is_near(low["price"], support, tolerance)) >= 2
    falling_highs = highs[-1]["price"] < highs[0]["price"] - (tolerance * 0.4)
    breakdown = clean[-1]["close"] < support - (tolerance * 0.2)
    detected = flat_bottoms and falling_highs
    confidence = 64 + (14 if breakdown else 0)
    return build_result(
        name="Descending Triangle",
        pattern_type="continuation",
        direction="bearish",
        detected=detected,
        confidence=confidence,
        levels={"support": support, "last_lower_high": highs[-1]["price"], "breakdown": breakdown},
        reason="Flat support with lower highs shows sellers pressing into demand.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
