"""Bear flag continuation detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, empty_result, normalize_candles


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    if len(clean) < 16:
        return empty_result("Bear Flag", "continuation", timeframe, len(clean))
    impulse = clean[-16:-7]
    flag = clean[-7:]
    impulse_change = impulse[-1]["close"] - impulse[0]["open"]
    impulse_pct = impulse_change / max(impulse[0]["open"], 1)
    flag_high = max(candle["high"] for candle in flag)
    flag_low = min(candle["low"] for candle in flag)
    flag_drift = flag[-1]["close"] - flag[0]["open"]
    detected = impulse_pct < -0.0025 and flag_drift >= impulse_change * 0.35 and flag_high < impulse[0]["open"]
    breakdown = clean[-1]["close"] <= flag_low
    confidence = 63 + (14 if breakdown else 0)
    return build_result(
        name="Bear Flag",
        pattern_type="continuation",
        direction="bearish",
        detected=detected,
        confidence=confidence,
        levels={"flag_high": flag_high, "flag_low": flag_low, "breakdown": breakdown},
        reason="Strong downward impulse followed by shallow consolidation keeps PUT continuation in play.",
        timeframe=timeframe,
        bars_used=16,
    )
