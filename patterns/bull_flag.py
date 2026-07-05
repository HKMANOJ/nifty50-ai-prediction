"""Bull flag continuation detector."""

from __future__ import annotations

from typing import Any

from .swing_detector import build_result, empty_result, normalize_candles


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    if len(clean) < 16:
        return empty_result("Bull Flag", "continuation", timeframe, len(clean))
    impulse = clean[-16:-7]
    flag = clean[-7:]
    impulse_change = impulse[-1]["close"] - impulse[0]["open"]
    impulse_pct = impulse_change / max(impulse[0]["open"], 1)
    flag_high = max(candle["high"] for candle in flag)
    flag_low = min(candle["low"] for candle in flag)
    flag_drift = flag[-1]["close"] - flag[0]["open"]
    detected = impulse_pct > 0.0025 and flag_drift <= impulse_change * 0.35 and flag_low > impulse[0]["open"]
    breakout = clean[-1]["close"] >= flag_high
    confidence = 63 + (14 if breakout else 0)
    return build_result(
        name="Bull Flag",
        pattern_type="continuation",
        direction="bullish",
        detected=detected,
        confidence=confidence,
        levels={"flag_high": flag_high, "flag_low": flag_low, "breakout": breakout},
        reason="Strong upward impulse followed by shallow consolidation keeps CALL continuation in play.",
        timeframe=timeframe,
        bars_used=16,
    )
