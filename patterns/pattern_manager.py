"""Pattern manager that runs all NIFTY candle-pattern detectors."""

from __future__ import annotations

from typing import Any, Callable

from . import (
    ascending_triangle,
    bear_flag,
    bearish_engulfing,
    bull_flag,
    bullish_engulfing,
    cup_handle,
    descending_triangle,
    doji,
    double_bottom,
    double_top,
    falling_wedge,
    hammer,
    head_shoulders,
    inverse_head_shoulders,
    inverted_hammer,
    rising_wedge,
    swing_detector,
    symmetrical_triangle,
    trendline_break,
)


Detector = Callable[[list[dict[str, Any]], str], dict[str, Any]]


class PatternManager:
    """Runs all configured chart and candlestick detectors."""

    def __init__(self, detectors: list[Detector] | None = None) -> None:
        self.detectors = detectors or [
            double_top.detect,
            double_bottom.detect,
            head_shoulders.detect,
            inverse_head_shoulders.detect,
            ascending_triangle.detect,
            descending_triangle.detect,
            symmetrical_triangle.detect,
            rising_wedge.detect,
            falling_wedge.detect,
            bull_flag.detect,
            bear_flag.detect,
            cup_handle.detect,
            trendline_break.detect,
        ]

    def analyze(self, candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
        clean = swing_detector.normalize_candles(candles)
        results: list[dict[str, Any]] = []
        for detector in self.detectors:
            try:
                results.append(detector(clean, timeframe))
            except Exception as exc:  # Keep one detector bug from killing live refresh.
                results.append(
                    swing_detector.build_result(
                        name=getattr(detector, "__module__", "unknown").split(".")[-1],
                        pattern_type="error",
                        direction="neutral",
                        detected=False,
                        confidence=0,
                        reason=f"Detector error: {exc}",
                        timeframe=timeframe,
                        bars_used=len(clean),
                    )
                )

        detected = [item for item in results if item.get("detected")]
        weighted_total = sum(abs(float(item.get("score") or 0)) for item in detected)
        if detected and weighted_total > 0:
            score = sum(float(item.get("score") or 0) * max(float(item.get("confidence") or 0), 1.0) for item in detected)
            divisor = sum(max(float(item.get("confidence") or 0), 1.0) for item in detected)
            composite_score = score / divisor
        else:
            composite_score = 0.0

        bullish = [item for item in detected if item.get("direction") == "bullish"]
        bearish = [item for item in detected if item.get("direction") == "bearish"]
        neutral = [item for item in detected if item.get("direction") == "neutral"]
        primary = max(detected, key=lambda item: abs(float(item.get("score") or 0)), default=None)
        bias = "BUY CALL" if composite_score > 0.18 else "BUY PUT" if composite_score < -0.18 else "WAIT"
        confidence = round(min(88.0, 45.0 + (abs(composite_score) * 42.0) + min(len(detected) * 2.5, 12.0)), 1) if detected else 0.0
        swings = swing_detector.detect_swings(clean)

        return {
            "timeframe": timeframe,
            "bars_analyzed": len(clean),
            "summary": {
                "bias": bias,
                "score": round(composite_score, 3),
                "confidence": confidence,
                "primary_pattern": primary.get("name") if primary else "NONE",
                "bullish_count": len(bullish),
                "bearish_count": len(bearish),
                "neutral_count": len(neutral),
            },
            "detected_patterns": sorted(detected, key=lambda item: abs(float(item.get("score") or 0)), reverse=True),
            "all_patterns": results,
            "swing_context": {
                "trend": swing_detector.recent_trend(clean),
                "latest_swing_highs": swings["highs"][-5:],
                "latest_swing_lows": swings["lows"][-5:],
            },
            "notes": [
                "Pattern output is a probability layer, not a guaranteed entry.",
                "Use pattern bias with option-volume breakpoints, trend, and stop-loss rules.",
            ],
        }


def analyze_patterns(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    return PatternManager().analyze(candles, timeframe)
