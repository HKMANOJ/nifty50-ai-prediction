"""Inverse head and shoulders reversal detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, detect_swings, empty_result, is_near, normalize_candles, price_tolerance


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    lows = swings["lows"]
    highs = swings["highs"]
    if len(lows) < 3 or len(highs) < 2:
        return empty_result("Inverse Head and Shoulders", "reversal", timeframe, len(clean))
    left, head, right = lows[-3], lows[-2], lows[-1]
    between_highs = [high for high in highs if left["index"] < high["index"] < right["index"]]
    if len(between_highs) < 2:
        return empty_result("Inverse Head and Shoulders", "reversal", timeframe, len(clean))
    neckline = max(high["price"] for high in between_highs[-2:])
    tolerance = price_tolerance(clean, 0.004)
    detected = head["price"] < left["price"] - tolerance and head["price"] < right["price"] - tolerance and is_near(left["price"], right["price"], tolerance * 1.8)
    broke_neckline = clean[-1]["close"] > neckline
    confidence = 66 + (16 if broke_neckline else 0)
    return build_result(
        name="Inverse Head and Shoulders",
        pattern_type="reversal",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"left_shoulder": left["price"], "head": head["price"], "right_shoulder": right["price"], "neckline": neckline, "breakout": broke_neckline},
        reason="A lower middle trough with two shoulders warns of downside exhaustion; neckline breakout adds confirmation.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
