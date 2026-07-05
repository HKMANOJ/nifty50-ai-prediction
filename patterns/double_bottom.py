"""Double bottom reversal detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, is_near, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    lows = swings["lows"]
    highs = swings["highs"]
    if len(lows) < 2 or not highs:
        return empty_result("Double Bottom", "reversal", timeframe, len(clean))
    first, second = lows[-2], lows[-1]
    tolerance = price_tolerance(clean, 0.0006)
    peak_highs = [high for high in highs if first["index"] < high["index"] < second["index"]]
    if not peak_highs:
        return empty_result("Double Bottom", "reversal", timeframe, len(clean))
    neckline = max(peak_highs, key=lambda item: item["price"])
    close = clean[-1]["close"]
    is_recent = (len(clean) - 1 - second["index"]) <= 15
    is_invalidated = close < min(first["price"], second["price"]) - tolerance
    
    detected = (
        is_near(first["price"], second["price"], tolerance)
        and second["index"] > first["index"]
        and is_recent
        and not is_invalidated
    )
    
    broke_neckline = close > neckline["price"] and not is_invalidated
    confidence = 64 + (16 if broke_neckline else 0)
    return build_result(
        name="Double Bottom",
        pattern_type="reversal",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"bottom_1": first["price"], "bottom_2": second["price"], "neckline": neckline["price"], "breakout": broke_neckline},
        reason="Two similar swing lows formed recently without invalidation; a neckline breakout confirms bullish pressure.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
