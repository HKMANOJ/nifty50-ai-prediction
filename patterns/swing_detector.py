"""Shared swing and candle helpers used by the pattern detectors."""

from __future__ import annotations

from statistics import mean
from typing import Any


def to_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "-"):
        return default
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def normalize_candles(candles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, candle in enumerate(candles or []):
        open_price = to_float(candle.get("open"))
        high = to_float(candle.get("high"))
        low = to_float(candle.get("low"))
        close = to_float(candle.get("close"))
        if None in (open_price, high, low, close):
            continue
        clean = dict(candle)
        clean.update(
            {
                "open": float(open_price),
                "high": max(float(high), float(open_price), float(close)),
                "low": min(float(low), float(open_price), float(close)),
                "close": float(close),
                "_index": len(normalized),
                "_source_index": index,
            }
        )
        normalized.append(clean)
    return normalized


def candle_range(candle: dict[str, Any]) -> float:
    return max(float(candle["high"]) - float(candle["low"]), 0.0)


def body(candle: dict[str, Any]) -> float:
    return abs(float(candle["close"]) - float(candle["open"]))


def upper_wick(candle: dict[str, Any]) -> float:
    return max(float(candle["high"]) - max(float(candle["open"]), float(candle["close"])), 0.0)


def lower_wick(candle: dict[str, Any]) -> float:
    return max(min(float(candle["open"]), float(candle["close"])) - float(candle["low"]), 0.0)


def is_bullish(candle: dict[str, Any]) -> bool:
    return float(candle["close"]) > float(candle["open"])


def is_bearish(candle: dict[str, Any]) -> bool:
    return float(candle["close"]) < float(candle["open"])


def average_range(candles: list[dict[str, Any]], period: int = 14) -> float:
    recent = candles[-period:] if period > 0 else candles
    ranges = [candle_range(candle) for candle in recent if candle_range(candle) > 0]
    return mean(ranges) if ranges else 0.0


def last_close(candles: list[dict[str, Any]]) -> float | None:
    return float(candles[-1]["close"]) if candles else None


def price_tolerance(candles: list[dict[str, Any]], pct: float = 0.003) -> float:
    close = last_close(candles) or 0.0
    return max(close * pct, average_range(candles) * 0.55, 0.01)


def detect_swings(candles: list[dict[str, Any]], lookback: int = 2) -> dict[str, list[dict[str, Any]]]:
    clean = normalize_candles(candles)
    if len(clean) < (lookback * 2) + 1:
        return {"highs": [], "lows": []}

    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    for index in range(lookback, len(clean) - lookback):
        window = clean[index - lookback : index + lookback + 1]
        candle = clean[index]
        high = float(candle["high"])
        low = float(candle["low"])
        if high >= max(float(item["high"]) for item in window):
            highs.append({"index": candle["_index"], "price": high, "candle": candle})
        if low <= min(float(item["low"]) for item in window):
            lows.append({"index": candle["_index"], "price": low, "candle": candle})
    return {"highs": highs, "lows": lows}


def slope_between(first: dict[str, Any], second: dict[str, Any]) -> float:
    bars = max(int(second["index"]) - int(first["index"]), 1)
    return (float(second["price"]) - float(first["price"])) / bars


def fit_line(points: list[dict[str, Any]]) -> tuple[float, float] | None:
    if len(points) < 2:
        return None
    xs = [float(point["index"]) for point in points]
    ys = [float(point["price"]) for point in points]
    x_mean = mean(xs)
    y_mean = mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    intercept = y_mean - (slope * x_mean)
    return slope, intercept


def line_value(line: tuple[float, float], index: int | float) -> float:
    slope, intercept = line
    return (slope * float(index)) + intercept


def recent_trend(candles: list[dict[str, Any]], window: int = 12, threshold_pct: float = 0.0012) -> str:
    clean = normalize_candles(candles)
    if len(clean) < 3:
        return "flat"
    recent = clean[-window:] if len(clean) >= window else clean
    start = float(recent[0]["close"])
    end = float(recent[-1]["close"])
    if start <= 0:
        return "flat"
    change_pct = (end - start) / start
    if change_pct > threshold_pct:
        return "up"
    if change_pct < -threshold_pct:
        return "down"
    return "flat"


def is_near(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance


def build_result(
    *,
    name: str,
    pattern_type: str,
    direction: str = "neutral",
    detected: bool = False,
    confidence: float = 0.0,
    levels: dict[str, Any] | None = None,
    reason: str = "",
    timeframe: str = "5m",
    bars_used: int = 0,
) -> dict[str, Any]:
    signal = 1 if direction == "bullish" else -1 if direction == "bearish" else 0
    clean_confidence = max(0.0, min(float(confidence), 100.0)) if detected else 0.0
    return {
        "name": name,
        "type": pattern_type,
        "direction": direction,
        "signal": signal,
        "detected": bool(detected),
        "confidence": round(clean_confidence, 1),
        "score": round(signal * (clean_confidence / 100.0), 3),
        "levels": levels or {},
        "reason": reason,
        "timeframe": timeframe,
        "bars_used": bars_used,
    }


def empty_result(name: str, pattern_type: str, timeframe: str, bars_used: int = 0) -> dict[str, Any]:
    return build_result(name=name, pattern_type=pattern_type, timeframe=timeframe, bars_used=bars_used)


def detect(candles: list[dict[str, Any]], timeframe: str = "5m") -> dict[str, Any]:
    clean = normalize_candles(candles)
    swings = detect_swings(clean)
    return build_result(
        name="Swing Detector",
        pattern_type="context",
        direction="neutral",
        detected=bool(swings["highs"] or swings["lows"]),
        confidence=50 if swings["highs"] or swings["lows"] else 0,
        levels={
            "swing_highs": swings["highs"][-5:],
            "swing_lows": swings["lows"][-5:],
            "trend": recent_trend(clean),
        },
        reason="Latest swing highs/lows extracted for chart-pattern context.",
        timeframe=timeframe,
        bars_used=len(clean),
    )
