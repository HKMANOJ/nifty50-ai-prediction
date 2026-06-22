#!/usr/bin/env python3
"""NIFTY prediction engine that consumes nifty_pattern_detector.py output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nifty_pattern_detector import candles_from_snapshot, clamp, detect_patterns, num


ROOT = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = ROOT / "market_snapshot.latest.json"


WEIGHTS = {
    "pattern_engine": 0.30,
    "market_structure": 0.25,
    "oi_pcr": 0.20,
    "liquidity": 0.10,
    "volatility": 0.10,
    "news_global": 0.05,
}


def signed(direction: str, value: float) -> float:
    if direction == "BULLISH":
        return value
    if direction == "BEARISH":
        return -value
    return 0.0


def option_oi_pcr_score(snapshot: dict[str, Any], pattern_direction: str) -> tuple[float, dict[str, Any]]:
    options = snapshot.get("options_volume") or {}
    totals = options.get("totals") or options.get("summary") or {}
    put_oi = num(totals.get("put_open_interest") or totals.get("put_oi"), 0.0) or 0.0
    call_oi = num(totals.get("call_open_interest") or totals.get("call_oi"), 0.0) or 0.0
    put_volume = num(totals.get("put_volume"), 0.0) or 0.0
    call_volume = num(totals.get("call_volume"), 0.0) or 0.0
    pcr = put_oi / call_oi if call_oi else (put_volume / call_volume if call_volume else None)
    if pcr is None:
        return 0.0, {"pcr": None, "detail": "No complete OI/PCR data available."}
    raw = clamp((pcr - 1.0) / 0.6, -1.0, 1.0)
    if pattern_direction == "BEARISH":
        raw *= -1
    return raw, {"pcr": round(pcr, 3), "detail": f"PCR {pcr:.2f} scored against {pattern_direction.lower()} pattern direction."}


def volatility_score(snapshot: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    market = snapshot.get("market") or {}
    signals = snapshot.get("numeric_signals") or {}
    realized = num(market.get("volatility_20d_pct"), 0.0) or 0.0
    vix_change = num(signals.get("india_vix_change_pct"), 0.0) or 0.0
    calmness = clamp((1.15 - realized) / 1.15, -1.0, 1.0)
    vix = clamp(-vix_change / 5.0, -1.0, 1.0)
    score = (calmness * 0.55) + (vix * 0.45)
    return score, {"realized_volatility_pct": realized, "india_vix_change_pct": vix_change}


def news_global_score(snapshot: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    signals = snapshot.get("numeric_signals") or {}
    pieces = [
        clamp(num(signals.get("news_sentiment_india"), 0.0) or 0.0, -1.0, 1.0) * 0.35,
        clamp(num(signals.get("news_sentiment_global"), 0.0) or 0.0, -1.0, 1.0) * 0.25,
        clamp((num(signals.get("gift_nifty_change_pct"), 0.0) or 0.0) / 0.8, -1.0, 1.0) * 0.25,
        clamp((num(signals.get("us_markets_change_pct"), 0.0) or 0.0) / 1.2, -1.0, 1.0) * 0.15,
    ]
    return sum(pieces), {
        "india_news": signals.get("news_sentiment_india"),
        "global_news": signals.get("news_sentiment_global"),
        "gift_nifty": signals.get("gift_nifty_change_pct"),
        "us_markets": signals.get("us_markets_change_pct"),
    }


def build_prediction(snapshot: dict[str, Any], timeframe: str = "5m") -> dict[str, Any]:
    candles = candles_from_snapshot(snapshot, timeframe)
    pattern = detect_patterns(candles, snapshot.get("options_volume"))
    direction = pattern.get("direction", "NEUTRAL")

    pattern_component = signed(direction, (num(pattern.get("pattern_score"), 0.0) or 0.0) / 100.0)
    structure_component = signed(direction, (num(pattern.get("trend_strength"), 0.0) or 0.0) / 100.0)
    oi_component, oi_detail = option_oi_pcr_score(snapshot, direction)
    liquidity_component = signed(direction, 1.0 if pattern.get("liquidity_sweep") else 0.0)
    volatility_component, volatility_detail = volatility_score(snapshot)
    news_component, news_detail = news_global_score(snapshot)

    weighted_score = (
        pattern_component * WEIGHTS["pattern_engine"]
        + structure_component * WEIGHTS["market_structure"]
        + oi_component * WEIGHTS["oi_pcr"]
        + liquidity_component * WEIGHTS["liquidity"]
        + volatility_component * WEIGHTS["volatility"]
        + news_component * WEIGHTS["news_global"]
    )

    confidence = num(pattern.get("confidence"), 0.0) or 0.0
    pattern_score = num(pattern.get("pattern_score"), 0.0) or 0.0
    trend_strength = num(pattern.get("trend_strength"), 0.0) or 0.0
    hard_gate_passed = confidence >= 75 and pattern_score >= 70 and trend_strength >= 60
    signal = "WAIT"
    if hard_gate_passed:
        if weighted_score >= 0.35 and direction == "BULLISH":
            signal = "BUY CE"
        elif weighted_score <= -0.35 and direction == "BEARISH":
            signal = "BUY PE"

    last_price = None
    if candles:
        last_price = num(candles[-1].get("close"))
    market = snapshot.get("market") or {}
    last_price = last_price or num(market.get("last_close"))
    projected_points = abs(weighted_score) * max((last_price or 0.0) * 0.0025, 40.0)
    target = None
    stop_loss = None
    if signal == "BUY CE" and last_price:
        target = last_price + projected_points
        stop_loss = last_price - max(projected_points * 0.45, 18.0)
    elif signal == "BUY PE" and last_price:
        target = last_price - projected_points
        stop_loss = last_price + max(projected_points * 0.45, 18.0)

    gates = {
        "confidence": confidence >= 75,
        "pattern_score": pattern_score >= 70,
        "trend_strength": trend_strength >= 60,
    }
    return {
        "signal": signal,
        "direction": direction,
        "confidence": round(confidence),
        "pattern_score": round(pattern_score),
        "trend_strength": round(trend_strength),
        "weighted_score": round(weighted_score, 3),
        "target": round(target, 2) if target is not None else None,
        "stop_loss": round(stop_loss, 2) if stop_loss is not None else None,
        "gates": gates,
        "weights": WEIGHTS,
        "components": {
            "pattern_engine": round(pattern_component, 3),
            "market_structure": round(structure_component, 3),
            "oi_pcr": round(oi_component, 3),
            "liquidity": round(liquidity_component, 3),
            "volatility": round(volatility_component, 3),
            "news_global": round(news_component, 3),
        },
        "component_details": {
            "oi_pcr": oi_detail,
            "volatility": volatility_detail,
            "news_global": news_detail,
        },
        "pattern_engine": pattern,
        "reasoning": (
            pattern.get("reasoning", [])
            + ([] if hard_gate_passed else ["WAIT gate active: confidence >= 75, pattern_score >= 70, and trend_strength >= 60 are required"])
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run gated NIFTY prediction from the latest market snapshot.")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--timeframe", default="5m")
    args = parser.parse_args()
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    print(json.dumps(build_prediction(snapshot, args.timeframe), indent=2))


if __name__ == "__main__":
    main()
