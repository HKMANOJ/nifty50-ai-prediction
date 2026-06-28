"""Double top reversal detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, is_near, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    highs = swings["highs"]
    lows = swings["lows"]
    if len(highs) < 2 or not lows:
        return empty_result("Double Top", "reversal", timeframe, len(clean))
    first, second = highs[-2], highs[-1]
    tolerance = price_tolerance(clean, 0.0025)
    valley_lows = [low for low in lows if first["index"] < low["index"] < second["index"]]
    if not valley_lows:
        return empty_result("Double Top", "reversal", timeframe, len(clean))
    neckline = min(valley_lows, key=lambda item: item["price"])
    close = clean[-1]["close"]
    detected = is_near(first["price"], second["price"], tolerance) and second["index"] > first["index"]
    broke_neckline = close < neckline["price"]
    confidence = 64 + (16 if broke_neckline else 0)
    return build_result(
        name="Double Top",
        pattern_type="reversal",
        direction="bearish",
        detected=detected,
        confidence=confidence,
        levels={"top_1": first["price"], "top_2": second["price"], "neckline": neckline["price"], "breakdown": broke_neckline},
        reason="Two similar swing highs formed; a neckline break confirms bearish reversal pressure.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
