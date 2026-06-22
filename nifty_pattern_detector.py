#!/usr/bin/env python3
"""Institutional-style NIFTY pattern detection engine.

This module is deterministic and data-only. It does not promise prediction
accuracy; it converts OHLCV candles plus optional option-flow data into a
structured market-pattern read that a prediction engine can gate against.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = ROOT / "market_snapshot.latest.json"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def num(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "-"):
        return default
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def avg(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    return mean(clean) if clean else None


def pct_change(current: float | None, previous: float | None) -> float:
    if current is None or previous in (None, 0):
        return 0.0
    return ((current - previous) / previous) * 100.0


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    time: str = ""

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return max(self.high - self.low, 0.01)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def bullish(self) -> bool:
        return self.close >= self.open


def normalize_candles(raw_candles: list[dict[str, Any]]) -> list[Candle]:
    candles: list[Candle] = []
    for row in raw_candles:
        open_value = num(row.get("open"))
        high_value = num(row.get("high"))
        low_value = num(row.get("low"))
        close_value = num(row.get("close"))
        if None in (open_value, high_value, low_value, close_value):
            continue
        candles.append(
            Candle(
                open=open_value or 0.0,
                high=high_value or 0.0,
                low=low_value or 0.0,
                close=close_value or 0.0,
                volume=num(row.get("volume"), 0.0) or 0.0,
                time=str(row.get("market_time") or row.get("time_utc") or row.get("time") or ""),
            )
        )
    return candles


def find_swings(candles: list[Candle], radius: int = 2) -> list[dict[str, Any]]:
    swings: list[dict[str, Any]] = []
    if len(candles) < (radius * 2) + 1:
        return swings
    for index in range(radius, len(candles) - radius):
        window = candles[index - radius : index + radius + 1]
        candle = candles[index]
        is_high = candle.high >= max(item.high for item in window)
        is_low = candle.low <= min(item.low for item in window)
        if is_high:
            swings.append({"type": "high", "index": index, "price": candle.high, "time": candle.time})
        if is_low:
            swings.append({"type": "low", "index": index, "price": candle.low, "time": candle.time})
    return swings


def classify_structure(swings: list[dict[str, Any]], candles: list[Candle]) -> dict[str, Any]:
    highs = [item for item in swings if item["type"] == "high"]
    lows = [item for item in swings if item["type"] == "low"]
    last_high = highs[-1]["price"] if highs else max((c.high for c in candles), default=None)
    last_low = lows[-1]["price"] if lows else min((c.low for c in candles), default=None)
    structure = "RANGE"
    trend = "SIDEWAYS"
    score = 0.0

    if len(highs) >= 2 and len(lows) >= 2:
        high_relation = "HH" if highs[-1]["price"] > highs[-2]["price"] else "LH"
        low_relation = "HL" if lows[-1]["price"] > lows[-2]["price"] else "LL"
        structure = f"{high_relation}-{low_relation}"
        if structure == "HH-HL":
            trend = "UPTREND"
            score = 0.85
        elif structure == "LH-LL":
            trend = "DOWNTREND"
            score = -0.85
        elif structure == "HH-LL":
            trend = "EXPANDING_RANGE"
            score = 0.15
        elif structure == "LH-HL":
            trend = "CONTRACTING_RANGE"
            score = 0.0

    closes = [c.close for c in candles[-20:]]
    momentum = pct_change(closes[-1], closes[0]) if len(closes) >= 2 else 0.0
    trend_strength = round(clamp(abs(score) * 70 + min(abs(momentum) * 18, 30), 0, 100))
    return {
        "structure": structure,
        "trend_direction": trend,
        "trend_strength": trend_strength,
        "last_swing_high": round(last_high, 2) if last_high is not None else None,
        "last_swing_low": round(last_low, 2) if last_low is not None else None,
        "internal_swing": swings[-1] if swings else None,
        "external_swing": swings[-3] if len(swings) >= 3 else (swings[0] if swings else None),
        "signal": score,
    }


def detect_bos_choch(swings: list[dict[str, Any]], candles: list[Candle], structure: dict[str, Any]) -> dict[str, Any]:
    if not candles:
        return {"bos": None, "choch": None, "signal": 0.0}
    last_close = candles[-1].close
    highs = [item for item in swings if item["type"] == "high"]
    lows = [item for item in swings if item["type"] == "low"]
    prior_high = highs[-1]["price"] if highs else None
    prior_low = lows[-1]["price"] if lows else None
    bos = None
    choch = None
    signal = 0.0

    if prior_high is not None and last_close > prior_high:
        bos = "bullish"
        signal = 1.0
        if structure.get("trend_direction") == "DOWNTREND":
            choch = "bullish"
    if prior_low is not None and last_close < prior_low:
        bos = "bearish"
        signal = -1.0
        if structure.get("trend_direction") == "UPTREND":
            choch = "bearish"
    return {"bos": bos, "choch": choch, "signal": signal}


def detect_liquidity(swings: list[dict[str, Any]], candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 2 or len(swings) < 2:
        return {"buy_side_liquidity": None, "sell_side_liquidity": None, "liquidity_sweep": None, "liquidity_zone": None, "signal": 0.0}
    last = candles[-1]
    highs = [item for item in swings if item["type"] == "high"]
    lows = [item for item in swings if item["type"] == "low"]
    buy_side = highs[-1]["price"] if highs else None
    sell_side = lows[-1]["price"] if lows else None
    sweep = None
    zone = None
    signal = 0.0
    if buy_side is not None and last.high > buy_side and last.close < buy_side:
        sweep = "bearish"
        zone = buy_side
        signal = -1.0
    if sell_side is not None and last.low < sell_side and last.close > sell_side:
        sweep = "bullish"
        zone = sell_side
        signal = 1.0
    return {
        "buy_side_liquidity": round(buy_side, 2) if buy_side is not None else None,
        "sell_side_liquidity": round(sell_side, 2) if sell_side is not None else None,
        "liquidity_sweep": sweep,
        "stop_hunt": sweep,
        "liquidity_grab": sweep,
        "liquidity_zone": round(zone, 2) if zone is not None else None,
        "signal": signal,
    }


def detect_fvgs(candles: list[Candle]) -> dict[str, Any]:
    fvgs: list[dict[str, Any]] = []
    for index in range(2, len(candles)):
        c1 = candles[index - 2]
        c3 = candles[index]
        if c1.high < c3.low:
            lower, upper = c1.high, c3.low
            filled = any(item.low <= upper and item.high >= lower for item in candles[index + 1 :])
            fvgs.append({"fvg_type": "bullish", "upper": round(upper, 2), "lower": round(lower, 2), "filled": filled, "index": index})
        if c1.low > c3.high:
            lower, upper = c3.high, c1.low
            filled = any(item.high >= lower and item.low <= upper for item in candles[index + 1 :])
            fvgs.append({"fvg_type": "bearish", "upper": round(upper, 2), "lower": round(lower, 2), "filled": filled, "index": index})
    active = next((item for item in reversed(fvgs) if not item["filled"]), None)
    signal = 1.0 if active and active["fvg_type"] == "bullish" else -1.0 if active and active["fvg_type"] == "bearish" else 0.0
    return {"fvg": "active" if active else "none", "active_fvg": active, "all_fvgs": fvgs[-8:], "signal": signal}


def detect_order_block(candles: list[Candle], bos: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 4:
        return {"order_block": None, "zone": None, "mitigation": False, "breaker_block": False, "signal": 0.0}
    direction = bos.get("bos") or bos.get("choch")
    if direction == "bullish":
        candidates = [c for c in candles[-12:-1] if not c.bullish]
        block = candidates[-1] if candidates else None
        signal = 1.0 if block else 0.0
        label = "bullish" if block else None
    elif direction == "bearish":
        candidates = [c for c in candles[-12:-1] if c.bullish]
        block = candidates[-1] if candidates else None
        signal = -1.0 if block else 0.0
        label = "bearish" if block else None
    else:
        block = None
        signal = 0.0
        label = None
    if not block:
        return {"order_block": None, "zone": None, "mitigation": False, "breaker_block": False, "signal": 0.0}
    low, high = min(block.open, block.close, block.low), max(block.open, block.close, block.high)
    last = candles[-1]
    mitigation = last.low <= high and last.high >= low
    breaker = (label == "bullish" and last.close < low) or (label == "bearish" and last.close > high)
    return {
        "order_block": label,
        "zone": [round(low, 2), round(high, 2)],
        "mitigation": mitigation,
        "breaker_block": breaker,
        "signal": 0.0 if breaker else signal,
    }


def detect_candles(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 3:
        return {"patterns": [], "primary": None, "signal": 0.0}
    last, prev, third = candles[-1], candles[-2], candles[-3]
    patterns: list[dict[str, Any]] = []
    body_ratio = last.body / last.range
    if body_ratio <= 0.12:
        patterns.append({"pattern": "Doji", "direction": "neutral", "confidence": 55})
    if last.lower_wick >= last.body * 2 and last.upper_wick <= last.body * 0.8:
        patterns.append({"pattern": "Hammer", "direction": "bullish", "confidence": 68})
    if last.upper_wick >= last.body * 2 and last.lower_wick <= last.body * 0.8:
        patterns.append({"pattern": "Shooting Star", "direction": "bearish", "confidence": 68})
    if body_ratio >= 0.78:
        patterns.append({"pattern": "Marubozu", "direction": "bullish" if last.bullish else "bearish", "confidence": 72})
    if last.bullish and not prev.bullish and last.close >= prev.open and last.open <= prev.close:
        patterns.append({"pattern": "Bullish Engulfing", "direction": "bullish", "confidence": 78})
    if not last.bullish and prev.bullish and last.open >= prev.close and last.close <= prev.open:
        patterns.append({"pattern": "Bearish Engulfing", "direction": "bearish", "confidence": 78})
    if not third.bullish and third.body / third.range > 0.45 and prev.body / prev.range < 0.28 and last.bullish and last.close > (third.open + third.close) / 2:
        patterns.append({"pattern": "Morning Star", "direction": "bullish", "confidence": 80})
    if third.bullish and third.body / third.range > 0.45 and prev.body / prev.range < 0.28 and not last.bullish and last.close < (third.open + third.close) / 2:
        patterns.append({"pattern": "Evening Star", "direction": "bearish", "confidence": 80})
    last_three = candles[-3:]
    if all(c.bullish for c in last_three) and all(last_three[i].close > last_three[i - 1].close for i in range(1, 3)):
        patterns.append({"pattern": "Three White Soldiers", "direction": "bullish", "confidence": 82})
    if all(not c.bullish for c in last_three) and all(last_three[i].close < last_three[i - 1].close for i in range(1, 3)):
        patterns.append({"pattern": "Three Black Crows", "direction": "bearish", "confidence": 82})
    primary = max(patterns, key=lambda item: item["confidence"], default=None)
    signal = 1.0 if primary and primary["direction"] == "bullish" else -1.0 if primary and primary["direction"] == "bearish" else 0.0
    return {"patterns": patterns, "primary": primary, "signal": signal}


def detect_reversal_patterns(swings: list[dict[str, Any]]) -> dict[str, Any]:
    highs = [item["price"] for item in swings if item["type"] == "high"][-5:]
    lows = [item["price"] for item in swings if item["type"] == "low"][-5:]
    candidates: list[dict[str, Any]] = []
    tolerance = 0.0025
    if len(highs) >= 2 and abs(highs[-1] - highs[-2]) / max(highs[-1], 1) <= tolerance:
        candidates.append({"pattern": "Double Top", "direction": "bearish", "confidence": 72})
    if len(lows) >= 2 and abs(lows[-1] - lows[-2]) / max(lows[-1], 1) <= tolerance:
        candidates.append({"pattern": "Double Bottom", "direction": "bullish", "confidence": 72})
    if len(highs) >= 3 and max(highs[-3:]) - min(highs[-3:]) <= max(highs[-1], 1) * tolerance:
        candidates.append({"pattern": "Triple Top", "direction": "bearish", "confidence": 78})
    if len(lows) >= 3 and max(lows[-3:]) - min(lows[-3:]) <= max(lows[-1], 1) * tolerance:
        candidates.append({"pattern": "Triple Bottom", "direction": "bullish", "confidence": 78})
    if len(highs) >= 3 and highs[-2] > highs[-3] and highs[-2] > highs[-1] and abs(highs[-3] - highs[-1]) / max(highs[-2], 1) <= 0.006:
        candidates.append({"pattern": "Head And Shoulders", "direction": "bearish", "confidence": 82})
    if len(lows) >= 3 and lows[-2] < lows[-3] and lows[-2] < lows[-1] and abs(lows[-3] - lows[-1]) / max(lows[-2], 1) <= 0.006:
        candidates.append({"pattern": "Inverse Head And Shoulders", "direction": "bullish", "confidence": 82})
    primary = max(candidates, key=lambda item: item["confidence"], default=None)
    signal = 1.0 if primary and primary["direction"] == "bullish" else -1.0 if primary and primary["direction"] == "bearish" else 0.0
    return {"patterns": candidates, "primary": primary, "signal": signal}


def detect_continuation(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 12:
        return {"pattern": None, "confidence": 0, "signal": 0.0}
    impulse = candles[-12:-6]
    base = candles[-6:]
    impulse_move = impulse[-1].close - impulse[0].open
    base_range = max(c.high for c in base) - min(c.low for c in base)
    impulse_range = max(c.high for c in impulse) - min(c.low for c in impulse)
    closes = [c.close for c in base]
    highs = [c.high for c in base]
    lows = [c.low for c in base]
    confidence = 0
    pattern = None
    signal = 0.0
    if impulse_move > 0 and base_range <= impulse_range * 0.55 and closes[-1] >= avg(closes) if avg(closes) is not None else False:
        pattern, confidence, signal = "Bull Flag", 76, 1.0
    elif impulse_move < 0 and base_range <= impulse_range * 0.55 and closes[-1] <= avg(closes) if avg(closes) is not None else False:
        pattern, confidence, signal = "Bear Flag", 76, -1.0
    elif max(highs) - min(highs) <= max(closes[-1], 1) * 0.0025 and lows[-1] > lows[0]:
        pattern, confidence, signal = "Ascending Triangle", 74, 1.0
    elif max(lows) - min(lows) <= max(closes[-1], 1) * 0.0025 and highs[-1] < highs[0]:
        pattern, confidence, signal = "Descending Triangle", 74, -1.0
    elif base_range <= max(closes[-1], 1) * 0.004:
        pattern, confidence, signal = "Rectangle", 62, 0.0
    elif base_range <= impulse_range * 0.35:
        pattern, confidence, signal = "Pennant", 70, 1.0 if impulse_move > 0 else -1.0
    if len(candles) >= 24:
        prior = candles[-24:-8]
        if min(c.low for c in prior) < min(c.low for c in base) and closes[-1] > avg([c.close for c in prior]) if avg([c.close for c in prior]) is not None else False:
            pattern, confidence, signal = "Cup And Handle", max(confidence, 71), 1.0
    return {"pattern": pattern, "confidence": confidence, "signal": signal}


def volume_confirmation(candles: list[Candle], direction_signal: float) -> dict[str, Any]:
    if len(candles) < 12:
        return {"volume_confirmation": False, "relative_volume": None, "signal": 0.0}
    recent = avg([c.volume for c in candles[-3:]]) or 0.0
    baseline = avg([c.volume for c in candles[-20:-3]]) or avg([c.volume for c in candles[:-3]]) or 0.0
    relative = recent / baseline if baseline else None
    confirmed = bool(relative is not None and relative >= 1.12 and abs(direction_signal) > 0)
    contraction = bool(relative is not None and relative <= 0.82)
    signal = 1.0 if confirmed else -0.35 if contraction and abs(direction_signal) > 0 else 0.0
    return {"volume_confirmation": confirmed, "volume_contraction": contraction, "relative_volume": round(relative, 2) if relative is not None else None, "signal": signal}


def option_flow_confirmation(option_flow: dict[str, Any] | None, direction_signal: float) -> dict[str, Any]:
    option_flow = option_flow or {}
    totals = option_flow.get("totals") or option_flow.get("summary") or {}
    put_oi = num(totals.get("put_open_interest") or totals.get("put_oi"), 0.0) or 0.0
    call_oi = num(totals.get("call_open_interest") or totals.get("call_oi"), 0.0) or 0.0
    put_volume = num(totals.get("put_volume"), 0.0) or 0.0
    call_volume = num(totals.get("call_volume"), 0.0) or 0.0
    pcr = put_oi / call_oi if call_oi else (put_volume / call_volume if call_volume else None)
    put_change = num(totals.get("put_oi_change") or totals.get("put_open_interest_change"), 0.0) or 0.0
    call_change = num(totals.get("call_oi_change") or totals.get("call_open_interest_change"), 0.0) or 0.0
    confirmation = "weak"
    signal = 0.0
    if direction_signal > 0:
        if (put_change > 0 and call_change <= 0) or (pcr is not None and pcr >= 1.05):
            confirmation, signal = "strong", 1.0
        elif pcr is not None and pcr >= 0.95:
            confirmation, signal = "moderate", 0.5
    elif direction_signal < 0:
        if (call_change > 0 and put_change <= 0) or (pcr is not None and pcr <= 0.95):
            confirmation, signal = "strong", 1.0
        elif pcr is not None and pcr <= 1.05:
            confirmation, signal = "moderate", 0.5
    return {"option_flow_confirmation": confirmation, "pcr": round(pcr, 3) if pcr is not None else None, "signal": signal}


def build_reasoning(parts: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    continuation = parts["continuation"]
    reversal = parts["reversal"].get("primary")
    candle = parts["candle"].get("primary")
    if continuation.get("pattern"):
        reasons.append(f"{continuation['pattern']} detected")
    if reversal:
        reasons.append(f"{reversal['pattern']} detected")
    if candle:
        reasons.append(f"{candle['pattern']} candle detected")
    if parts["bos"].get("bos"):
        reasons.append(f"{parts['bos']['bos'].title()} BOS detected")
    if parts["bos"].get("choch"):
        reasons.append(f"{parts['bos']['choch'].title()} CHOCH detected")
    if parts["liquidity"].get("liquidity_sweep"):
        reasons.append(f"{parts['liquidity']['liquidity_sweep'].title()} liquidity sweep near {parts['liquidity'].get('liquidity_zone')}")
    if parts["fvg"].get("active_fvg"):
        reasons.append(f"Active {parts['fvg']['active_fvg']['fvg_type']} FVG")
    if parts["order_block"].get("order_block"):
        reasons.append(f"{parts['order_block']['order_block'].title()} order block at {parts['order_block'].get('zone')}")
    if parts["volume"].get("volume_confirmation"):
        reasons.append("Volume expansion confirms the pattern")
    if parts["option_flow"].get("option_flow_confirmation") == "strong":
        reasons.append("Option flow confirmation is strong")
    return reasons or ["No institutional pattern stack is confirmed yet"]


def score_patterns(parts: dict[str, Any]) -> tuple[int, str, str, int]:
    score = 0
    direction_points = 0.0

    structure = parts["structure"]
    if structure["structure"] == "HH-HL":
        score += 15
        direction_points += 1
    elif structure["structure"] == "LH-LL":
        score += 15
        direction_points -= 1

    if parts["bos"]["bos"]:
        score += 20
        direction_points += parts["bos"]["signal"]
    if parts["bos"]["choch"]:
        score += 20
        direction_points += parts["bos"]["signal"]
    if parts["liquidity"]["liquidity_sweep"]:
        score += 20
        direction_points += parts["liquidity"]["signal"]
    if parts["fvg"]["active_fvg"]:
        score += 15
        direction_points += parts["fvg"]["signal"]
    if parts["order_block"]["order_block"]:
        score += 15
        direction_points += parts["order_block"]["signal"]
    if parts["continuation"].get("pattern"):
        score += 15
        direction_points += parts["continuation"]["signal"]
    reversal_primary = parts["reversal"].get("primary")
    if reversal_primary and reversal_primary["pattern"] in {"Double Bottom", "Double Top"}:
        score += 15
        direction_points += parts["reversal"]["signal"]
    if parts["volume"]["volume_confirmation"]:
        score += 15
    if parts["option_flow"]["option_flow_confirmation"] == "strong":
        score += 20

    pattern_score = round(clamp(score, 0, 100))
    direction = "BULLISH" if direction_points > 0.5 else "BEARISH" if direction_points < -0.5 else "NEUTRAL"
    primary = (
        parts["continuation"].get("pattern")
        or (parts["reversal"].get("primary") or {}).get("pattern")
        or (parts["candle"].get("primary") or {}).get("pattern")
        or structure["structure"]
    )
    confidence = round(
        clamp(
            pattern_score * 0.62
            + structure["trend_strength"] * 0.22
            + (parts["continuation"].get("confidence") or 0) * 0.08
            + ((parts["reversal"].get("primary") or {}).get("confidence") or 0) * 0.05
            + ((parts["candle"].get("primary") or {}).get("confidence") or 0) * 0.03,
            0,
            100,
        )
    )
    return pattern_score, direction, primary, confidence


def detect_patterns(raw_candles: list[dict[str, Any]], option_flow: dict[str, Any] | None = None) -> dict[str, Any]:
    candles = normalize_candles(raw_candles)
    if len(candles) < 8:
        return {
            "direction": "NEUTRAL",
            "pattern": None,
            "confidence": 0,
            "pattern_score": 0,
            "structure": "INSUFFICIENT_DATA",
            "trend_strength": 0,
            "pattern_bias": "NEUTRAL_PATTERN",
            "reasoning": ["At least 8 candles are required for institutional pattern detection"],
        }

    swings = find_swings(candles)
    structure = classify_structure(swings, candles)
    bos = detect_bos_choch(swings, candles, structure)
    liquidity = detect_liquidity(swings, candles)
    fvg = detect_fvgs(candles)
    order_block = detect_order_block(candles, bos)
    reversal = detect_reversal_patterns(swings)
    continuation = detect_continuation(candles)
    candle = detect_candles(candles)

    provisional_signal = (
        structure["signal"] * 0.25
        + bos["signal"] * 0.2
        + liquidity["signal"] * 0.15
        + fvg["signal"] * 0.1
        + order_block["signal"] * 0.1
        + continuation["signal"] * 0.12
        + reversal["signal"] * 0.05
        + candle["signal"] * 0.03
    )
    volume = volume_confirmation(candles, provisional_signal)
    option_flow = option_flow_confirmation(option_flow, provisional_signal)

    parts = {
        "structure": structure,
        "bos": bos,
        "liquidity": liquidity,
        "fvg": fvg,
        "order_block": order_block,
        "reversal": reversal,
        "continuation": continuation,
        "candle": candle,
        "volume": volume,
        "option_flow": option_flow,
    }
    pattern_score, direction, primary_pattern, confidence = score_patterns(parts)
    pattern_bias = "BULLISH_PATTERN" if direction == "BULLISH" else "BEARISH_PATTERN" if direction == "BEARISH" else "NEUTRAL_PATTERN"
    return {
        "direction": direction,
        "pattern": primary_pattern,
        "confidence": confidence,
        "pattern_score": pattern_score,
        "structure": structure["structure"],
        "trend_direction": structure["trend_direction"],
        "trend_strength": structure["trend_strength"],
        "last_swing_high": structure["last_swing_high"],
        "last_swing_low": structure["last_swing_low"],
        "internal_swing": structure["internal_swing"],
        "external_swing": structure["external_swing"],
        "bos": bos["bos"],
        "choch": bos["choch"],
        "liquidity_sweep": liquidity["liquidity_sweep"],
        "liquidity_zone": liquidity["liquidity_zone"],
        "buy_side_liquidity": liquidity["buy_side_liquidity"],
        "sell_side_liquidity": liquidity["sell_side_liquidity"],
        "fvg": fvg["fvg"],
        "active_fvg": fvg["active_fvg"],
        "order_block": order_block["order_block"],
        "order_block_zone": order_block["zone"],
        "mitigation": order_block["mitigation"],
        "breaker_block": order_block["breaker_block"],
        "reversal_patterns": reversal["patterns"],
        "continuation_pattern": continuation,
        "candle_patterns": candle["patterns"],
        "volume_confirmation": volume["volume_confirmation"],
        "relative_volume": volume["relative_volume"],
        "option_flow_confirmation": option_flow["option_flow_confirmation"],
        "pcr": option_flow["pcr"],
        "pattern_bias": pattern_bias,
        "reasoning": build_reasoning(parts),
    }


def candles_from_snapshot(snapshot: dict[str, Any], timeframe: str = "5m") -> list[dict[str, Any]]:
    intraday = snapshot.get("intraday") or {}
    series = intraday.get("series") or {}
    return list(series.get(timeframe) or series.get("5m") or series.get("1m") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect institutional NIFTY patterns from a market snapshot.")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--timeframe", default="5m")
    args = parser.parse_args()
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    result = detect_patterns(candles_from_snapshot(snapshot, args.timeframe), snapshot.get("options_volume"))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
