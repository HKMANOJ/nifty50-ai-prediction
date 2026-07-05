"""Head and shoulders reversal detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, is_near, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    highs = swings["highs"]
    lows = swings["lows"]
    if len(highs) < 3 or len(lows) < 2:
        return empty_result("Head and Shoulders", "reversal", timeframe, len(clean))
    left, head, right = highs[-3], highs[-2], highs[-1]
    between_lows = [low for low in lows if left["index"] < low["index"] < right["index"]]
    if len(between_lows) < 2:
        return empty_result("Head and Shoulders", "reversal", timeframe, len(clean))
    neckline = min(low["price"] for low in between_lows[-2:])
    tolerance = price_tolerance(clean, 0.004)
    detected = head["price"] > left["price"] + tolerance and head["price"] > right["price"] + tolerance and is_near(left["price"], right["price"], tolerance * 1.8)
    broke_neckline = clean[-1]["close"] < neckline
    confidence = 66 + (16 if broke_neckline else 0)
    return build_result(
        name="Head and Shoulders",
        pattern_type="reversal",
        direction="bearish",
        detected=detected,
        confidence=confidence,
        levels={"left_shoulder": left["price"], "head": head["price"], "right_shoulder": right["price"], "neckline": neckline, "breakdown": broke_neckline},
        reason="A higher middle peak with two shoulders warns of upside exhaustion; neckline break adds confirmation.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
