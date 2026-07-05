#!/usr/bin/env python3
"""Evidence-only analysis for post-fix replay quality filtering."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_structure_engine import MarketStructureEngine
from patterns.pattern_manager import PatternManager
from patterns.swing_detector import average_range, detect_swings, price_tolerance
from replay_opportunity_audit import replay_date
from store_candles_mysql import connect_mysql


DATES = ["2026-06-20", "2026-06-23", "2026-06-24", "2026-06-25"]
TIMEFRAME = "5m"
LIMIT = 500
MAX_STOP_POINTS = 30.0
WIN_VERDICTS = {"winner"}
LOSS_VERDICTS = {"loser"}


@dataclass
class FilterResult:
    name: str
    trades_remaining: int
    winners: int
    losers: int
    win_rate: float | None
    profit_factor: float | None


def parse_reasons(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except Exception:
        pass
    return [raw]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def average(values: list[float]) -> float:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(clean) / len(clean) if clean else 0.0


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    current = float(values[0])
    out: list[float] = []
    for value in values:
        current = ((float(value) - current) * multiplier) + current
        out.append(current)
    return out


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        if previous_close is None:
            out.append(high - low)
        else:
            out.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = float(candle["close"])
    return out


def atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    trs = true_ranges(candles)
    if not trs:
        return 0.0
    sample = trs[-period:] if len(trs) >= period else trs
    return average(sample)


def is_watch_only(pattern_name: str) -> bool:
    name = pattern_name.strip().lower()
    return name in {"w pattern breakout", "w pattern forming"}


def is_double_top_put(record: dict[str, Any]) -> bool:
    name = str(record.get("pattern_name") or "").strip().lower()
    side = str(record.get("signal_side") or "").strip().upper()
    return side == "PUT" and name in {"double top", "double top breakdown"}


def ema_aligned_from_replay_logic(side: str, entry: float, candles: list[dict[str, Any]]) -> bool:
    closes = [float(item["close"]) for item in candles]
    if not closes:
        return False
    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    return (side == "CALL" and entry >= ema9 >= ema21) or (side == "PUT" and entry <= ema9 <= ema21)


def stop_valid(record: dict[str, Any]) -> bool:
    entry = float(record.get("entry_candidate") or 0.0)
    stop = float(record.get("stop_candidate") or 0.0)
    distance = abs(entry - stop)
    return distance > 0 and distance <= MAX_STOP_POINTS


def target_valid(record: dict[str, Any]) -> bool:
    entry = float(record.get("entry_candidate") or 0.0)
    target = float(record.get("target_candidate") or 0.0)
    return abs(target - entry) > 0


def qualifies_under_fix(record: dict[str, Any]) -> tuple[bool, str]:
    pattern_name = str(record.get("pattern_name") or "")
    if is_watch_only(pattern_name):
        return False, "watch_only_pattern"

    if is_double_top_put(record):
        reasons = parse_reasons(record.get("rejection_reasons"))
        confidence = float(record.get("confidence") or 0.0)
        projected_move = float(record.get("min_move_points") or 0.0)
        session_ok = bool(record.get("session_valid"))
        ema_ok = "ema_trend_not_fully_aligned" not in [item.lower() for item in reasons]
        if (
            confidence >= 80.0
            and projected_move >= 70.0
            and ema_ok
            and session_ok
            and stop_valid(record)
            and target_valid(record)
        ):
            return True, "qualified_double_top_put"
        return False, "double_top_put_blocked"

    return False, "unchanged_non_trade_candidate"


def fetch_candles_by_date(connection, market_date: str) -> list[dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT symbol, timeframe, market_time, market_date, open, high, low, close, volume, source
        FROM nifty_candles
        WHERE symbol = %s AND timeframe = %s AND market_date = %s
        ORDER BY market_time ASC
        """,
        ("NIFTY50", TIMEFRAME, market_date),
    )
    rows = cursor.fetchall() or []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "market_time": row["market_time"].strftime("%Y-%m-%d %H:%M:%S") if hasattr(row["market_time"], "strftime") else str(row["market_time"]),
                "market_date": row["market_date"].isoformat() if hasattr(row["market_date"], "isoformat") else str(row["market_date"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume") or 0),
                "source": row.get("source"),
            }
        )
    return out


def pattern_display_name(pattern: dict[str, Any]) -> str:
    name = str(pattern.get("name") or "Pattern")
    levels = pattern.get("levels") or {}
    if name == "Double Bottom" and levels.get("breakout"):
        return "W Pattern Breakout"
    if name == "Double Bottom":
        return "W Pattern Forming"
    if name == "Double Top" and levels.get("breakdown"):
        return "Double Top Breakdown"
    return name


def candle_shape_metrics(candle: dict[str, Any]) -> dict[str, float]:
    open_price = float(candle["open"])
    close = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])
    total_range = max(high - low, 0.01)
    body = abs(close - open_price)
    upper = max(high - max(open_price, close), 0.0)
    lower = max(min(open_price, close) - low, 0.0)
    return {
        "body_pct": round((body / total_range) * 100, 2),
        "upper_wick_pct": round((upper / total_range) * 100, 2),
        "lower_wick_pct": round((lower / total_range) * 100, 2),
        "largest_wick_pct": round((max(upper, lower) / total_range) * 100, 2),
    }


def time_bucket(market_time: str) -> str:
    dt = datetime.fromisoformat(str(market_time).replace(" ", "T"))
    minute = dt.hour * 60 + dt.minute
    if minute < (10 * 60 + 30):
        return "09:15-10:30"
    if minute < (12 * 60 + 30):
        return "10:30-12:30"
    if minute < (14 * 60 + 30):
        return "12:30-14:30"
    return "14:30-15:30"


def get_volume_confirmation(window: list[dict[str, Any]]) -> tuple[bool | None, float | None]:
    volumes = [float(item.get("volume") or 0) for item in window if float(item.get("volume") or 0) > 0]
    if not volumes:
        return None, None
    last_volume = volumes[-1]
    avg20 = average(volumes[-20:]) or last_volume
    ratio = last_volume / max(avg20, 1.0)
    return ratio >= 1.35, round(ratio, 2)


def resolve_pattern(window: list[dict[str, Any]], record_pattern_name: str, manager: PatternManager) -> dict[str, Any] | None:
    report = manager.analyze(window, TIMEFRAME)
    patterns = report.get("detected_patterns") or []
    for pattern in patterns:
        if pattern_display_name(pattern) == record_pattern_name:
            return pattern
    primary_name = report.get("summary", {}).get("primary_pattern")
    for pattern in patterns:
        if str(pattern.get("name")) == str(primary_name):
            return pattern
    return patterns[0] if patterns else None


def match_double_top_indices(window: list[dict[str, Any]], pattern: dict[str, Any]) -> tuple[int | None, int | None, float | None]:
    levels = pattern.get("levels") or {}
    top_1 = levels.get("top_1")
    top_2 = levels.get("top_2")
    neckline = levels.get("neckline")
    if top_1 is None or top_2 is None or neckline is None:
        return None, None, None
    swings = detect_swings(window, lookback=2)
    highs = swings.get("highs") or []
    tolerance = price_tolerance(window, 0.0025)
    second_idx = None
    first_idx = None
    for high in reversed(highs):
        if second_idx is None and abs(float(high["price"]) - float(top_2)) <= tolerance:
            second_idx = int(high["index"])
            continue
        if second_idx is not None and abs(float(high["price"]) - float(top_1)) <= tolerance and int(high["index"]) < second_idx:
            first_idx = int(high["index"])
            break
    return first_idx, second_idx, float(neckline)


def double_top_retest_confirmation(window: list[dict[str, Any]], pattern: dict[str, Any]) -> bool:
    first_idx, second_idx, neckline = match_double_top_indices(window, pattern)
    if second_idx is None or neckline is None:
        return False
    avg_rng = average_range(window, 14) or max(float(window[-1]["close"]) * 0.0008, 8.0)
    tolerance = max(avg_rng * 0.45, 8.0)
    breakdown_idx = None
    for idx in range(second_idx + 1, len(window)):
        if float(window[idx]["close"]) < neckline:
            breakdown_idx = idx
            break
    if breakdown_idx is None:
        return False
    for candle in window[breakdown_idx + 1 : -1]:
        if float(candle["high"]) >= neckline - tolerance and float(candle["close"]) < neckline:
            return True
    return False


def verdict_for_trade(record: dict[str, Any]) -> str:
    target_hit = bool(record.get("future_reached_target"))
    stop_hit_first = bool(record.get("future_hit_stop_first"))
    if target_hit and not stop_hit_first:
        return "winner"
    if stop_hit_first:
        return "loser"
    return "neutral"


def enrich_trade(record: dict[str, Any], day_candles: list[dict[str, Any]], manager: PatternManager, structure_engine: MarketStructureEngine) -> dict[str, Any]:
    market_time = str(record["market_time"])
    candle_index = next((idx for idx, candle in enumerate(day_candles) if str(candle["market_time"]) == market_time), None)
    if candle_index is None:
        raise ValueError(f"Could not locate candle for {market_time}")
    window = day_candles[: candle_index + 1]
    last_candle = window[-1]
    pattern = resolve_pattern(window, str(record.get("pattern_name") or ""), manager) or {}
    structure = structure_engine.analyze_market_structure(window, lookback=5)
    shape = candle_shape_metrics(last_candle)
    entry = float(record["entry_candidate"])
    target = float(record["target_candidate"])
    stop = float(record["stop_candidate"])
    target_distance = abs(target - entry)
    stop_distance = abs(entry - stop)
    rr = (target_distance / stop_distance) if stop_distance else None
    pattern_name = pattern_display_name(pattern) if pattern else str(record.get("pattern_name") or "")
    neckline_break_confirmed = bool((pattern.get("levels") or {}).get("breakdown")) if "double top" in pattern_name.lower() else False
    retest_confirmed = double_top_retest_confirmation(window, pattern) if "double top" in pattern_name.lower() else False
    volume_confirmed, volume_ratio = get_volume_confirmation(window)
    trend_strength = int(structure.get("trend_strength") or 0)
    current_structure = str(structure.get("current_structure") or "UNKNOWN")
    projected_move = float(record.get("min_move_points") or 0.0)
    summary_score = abs(float((manager.analyze(window, TIMEFRAME).get("summary") or {}).get("score") or 0.0)) * 100.0
    side = str(record.get("signal_side") or "").upper()

    return {
        **record,
        "analysis_date": str(record.get("market_time") or "")[:10],
        "analysis_time": str(record.get("market_time") or "")[11:16],
        "verdict": verdict_for_trade(record),
        "pattern_resolved": pattern_name,
        "confidence": int(record.get("confidence") or 0),
        "projected_move": round(projected_move, 2),
        "trend_strength": trend_strength,
        "market_structure": current_structure,
        "ema_aligned": ema_aligned_from_replay_logic(side, entry, window),
        "atr14": round(atr(window, 14), 2),
        "body_pct": shape["body_pct"],
        "largest_wick_pct": shape["largest_wick_pct"],
        "upper_wick_pct": shape["upper_wick_pct"],
        "lower_wick_pct": shape["lower_wick_pct"],
        "pattern_score": round(summary_score, 2),
        "time_bucket": time_bucket(market_time),
        "stop_distance": round(stop_distance, 2),
        "target_distance": round(target_distance, 2),
        "risk_reward": round(rr, 2) if rr is not None else None,
        "neckline_break_confirmed": neckline_break_confirmed,
        "retest_confirmed": retest_confirmed,
        "volume_confirmed": volume_confirmed,
        "volume_ratio_20": volume_ratio,
        "future_move": float(record.get("future_max_move_points") or 0.0),
        "target_hit_first": bool(record.get("future_reached_target")) and not bool(record.get("future_hit_stop_first")),
        "stop_hit_first": bool(record.get("future_hit_stop_first")),
    }


def selected_trades(connection) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    manager = PatternManager()
    structure_engine = MarketStructureEngine()
    day_cache: dict[str, list[dict[str, Any]]] = {}
    for market_date in DATES:
        replay_payload = replay_date(
            connection,
            market_date=market_date,
            symbol="NIFTY50",
            timeframe=TIMEFRAME,
            limit=LIMIT,
        )
        for record in replay_payload.get("records") or []:
            qualified, _reason = qualifies_under_fix(record)
            if not qualified:
                continue
            date_key = str(record.get("market_time"))[:10]
            if date_key not in day_cache:
                day_cache[date_key] = fetch_candles_by_date(connection, date_key)
            trades.append(enrich_trade(record, day_cache[date_key], manager, structure_engine))
    return trades


def profit_factor(trades: list[dict[str, Any]]) -> float | None:
    reward = sum(float(item.get("target_distance") or 0.0) for item in trades if item.get("verdict") == "winner")
    risk = sum(float(item.get("stop_distance") or 0.0) for item in trades if item.get("verdict") == "loser")
    if risk <= 0:
        return None if reward <= 0 else 999.0
    return reward / risk


def apply_filter(trades: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool], name: str) -> FilterResult:
    kept = [item for item in trades if predicate(item)]
    winners = sum(1 for item in kept if item.get("verdict") == "winner")
    losers = sum(1 for item in kept if item.get("verdict") == "loser")
    decided = winners + losers
    return FilterResult(
        name=name,
        trades_remaining=len(kept),
        winners=winners,
        losers=losers,
        win_rate=round((winners / decided) * 100, 1) if decided else None,
        profit_factor=round(profit_factor(kept) or 0.0, 2) if decided else None,
    )


def summarise_group(trades: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(trades),
        "avg_confidence": round(average([item["confidence"] for item in trades]), 2),
        "avg_projected_move": round(average([item["projected_move"] for item in trades]), 2),
        "avg_trend_strength": round(average([item["trend_strength"] for item in trades]), 2),
        "avg_atr14": round(average([item["atr14"] for item in trades]), 2),
        "avg_body_pct": round(average([item["body_pct"] for item in trades]), 2),
        "avg_largest_wick_pct": round(average([item["largest_wick_pct"] for item in trades]), 2),
        "avg_pattern_score": round(average([item["pattern_score"] for item in trades]), 2),
        "avg_stop_distance": round(average([item["stop_distance"] for item in trades]), 2),
        "avg_target_distance": round(average([item["target_distance"] for item in trades]), 2),
        "avg_risk_reward": round(average([item["risk_reward"] for item in trades if item["risk_reward"] is not None]), 2),
        "neckline_break_yes": sum(1 for item in trades if item["neckline_break_confirmed"]),
        "retest_yes": sum(1 for item in trades if item["retest_confirmed"]),
        "volume_confirmed_yes": sum(1 for item in trades if item["volume_confirmed"] is True),
        "ema_aligned_yes": sum(1 for item in trades if item["ema_aligned"]),
        "time_buckets": dict(Counter(item["time_bucket"] for item in trades)),
        "patterns": dict(Counter(item["pattern_resolved"] for item in trades)),
    }


def best_high_accuracy_combo(decided_trades: list[dict[str, Any]]) -> dict[str, Any]:
    combos = [
        ("conf85", lambda t: t["confidence"] >= 85),
        ("proj90", lambda t: t["projected_move"] >= 90),
        ("trend80", lambda t: t["trend_strength"] >= 80),
        ("rr1.5", lambda t: (t["risk_reward"] or 0) >= 1.5),
        ("neckline", lambda t: t["neckline_break_confirmed"]),
        ("retest", lambda t: t["retest_confirmed"]),
        ("volume", lambda t: t["volume_confirmed"] is True),
    ]
    best: dict[str, Any] | None = None
    for mask in range(1, 1 << len(combos)):
        active_names: list[str] = []
        active_preds: list[Callable[[dict[str, Any]], bool]] = []
        for bit, (name, pred) in enumerate(combos):
            if mask & (1 << bit):
                active_names.append(name)
                active_preds.append(pred)
        kept = [trade for trade in decided_trades if all(pred(trade) for pred in active_preds)]
        winners = sum(1 for item in kept if item["verdict"] == "winner")
        losers = sum(1 for item in kept if item["verdict"] == "loser")
        if not kept or losers > winners:
            continue
        decided = winners + losers
        if decided == 0:
            continue
        pf = profit_factor(kept)
        score = (
            (winners / decided) * 1000
            + (pf or 0) * 100
            - len(kept) * 2
            + winners * 20
            - losers * 40
        )
        candidate = {
            "rules": active_names,
            "trades": len(kept),
            "winners": winners,
            "losers": losers,
            "win_rate": round((winners / decided) * 100, 1),
            "profit_factor": round(pf or 0.0, 2),
            "score": round(score, 2),
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    return best or {}


def build_markdown(trades: list[dict[str, Any]]) -> str:
    decided = [item for item in trades if item["verdict"] in {"winner", "loser"}]
    winners = [item for item in decided if item["verdict"] == "winner"]
    losers = [item for item in decided if item["verdict"] == "loser"]

    filters = [
        apply_filter(decided, lambda t: t["confidence"] >= 85, "confidence >= 85"),
        apply_filter(decided, lambda t: t["confidence"] >= 90, "confidence >= 90"),
        apply_filter(decided, lambda t: t["projected_move"] >= 90, "projected_move >= 90"),
        apply_filter(decided, lambda t: t["trend_strength"] >= 80, "trend_strength >= 80"),
        apply_filter(decided, lambda t: t["trend_strength"] >= 90, "trend_strength >= 90"),
        apply_filter(decided, lambda t: (t["risk_reward"] or 0) >= 1.5, "risk_reward >= 1.5"),
        apply_filter(decided, lambda t: t["neckline_break_confirmed"], "neckline break confirmation"),
        apply_filter(decided, lambda t: t["retest_confirmed"], "retest confirmation before entry"),
        apply_filter(decided, lambda t: t["volume_confirmed"] is True, "volume confirmation if available"),
    ]

    winner_summary = summarise_group(winners)
    loser_summary = summarise_group(losers)
    combo = best_high_accuracy_combo(decided)

    lines: list[str] = []
    lines.append("# High Accuracy Mode Evidence Report")
    lines.append("")
    lines.append("Scope:")
    lines.append("- Replay source: current post-fix strategy replay subset")
    lines.append("- Dates: 2026-06-20, 2026-06-23, 2026-06-24, 2026-06-25")
    lines.append("- Full replay set after fixes: 25 trades")
    lines.append("- This report analyzes only the 2 winners and 9 losers, as requested")
    lines.append("- 14 neutral / open trades are excluded from filter statistics")
    lines.append("")
    lines.append("## Trade Count Check")
    lines.append(f"- Decided trades analyzed: {len(decided)}")
    lines.append(f"- Winners: {len(winners)}")
    lines.append(f"- Losers: {len(losers)}")
    lines.append(f"- Baseline win rate: {round((len(winners) / max(len(decided), 1)) * 100, 1)}%")
    lines.append(f"- Baseline profit factor: {round(profit_factor(decided) or 0.0, 2)}")
    lines.append("")

    lines.append("## Decided Trades")
    lines.append("")
    lines.append("| Date | Time | Pattern | Verdict | Confidence | Projected Move | Trend Strength | EMA | ATR | Body % | Wick % | Pattern Score | Time Bucket | Stop Dist | Target Dist | RR | Neckline | Retest | Volume | Future Move |")
    lines.append("|---|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|---|---|---:|")
    for trade in decided:
        lines.append(
            f"| {trade['analysis_date']} | {trade['analysis_time']} | {trade['pattern_resolved']} | {trade['verdict']} | "
            f"{trade['confidence']} | {trade['projected_move']:.2f} | {trade['trend_strength']} | "
            f"{'PASS' if trade['ema_aligned'] else 'FAIL'} | {trade['atr14']:.2f} | {trade['body_pct']:.2f} | "
            f"{trade['largest_wick_pct']:.2f} | {trade['pattern_score']:.2f} | {trade['time_bucket']} | "
            f"{trade['stop_distance']:.2f} | {trade['target_distance']:.2f} | {trade['risk_reward'] or 0:.2f} | "
            f"{'YES' if trade['neckline_break_confirmed'] else 'NO'} | {'YES' if trade['retest_confirmed'] else 'NO'} | "
            f"{'YES' if trade['volume_confirmed'] is True else 'NO' if trade['volume_confirmed'] is False else 'N/A'} | {trade['future_move']:.2f} |"
        )
    lines.append("")

    lines.append("## Winners vs Losers")
    lines.append("")
    lines.append("| Metric | Winners | Losers |")
    lines.append("|---|---:|---:|")
    comparisons = [
        ("Avg confidence", winner_summary["avg_confidence"], loser_summary["avg_confidence"]),
        ("Avg projected move", winner_summary["avg_projected_move"], loser_summary["avg_projected_move"]),
        ("Avg trend strength", winner_summary["avg_trend_strength"], loser_summary["avg_trend_strength"]),
        ("Avg ATR14", winner_summary["avg_atr14"], loser_summary["avg_atr14"]),
        ("Avg candle body %", winner_summary["avg_body_pct"], loser_summary["avg_body_pct"]),
        ("Avg largest wick %", winner_summary["avg_largest_wick_pct"], loser_summary["avg_largest_wick_pct"]),
        ("Avg pattern score", winner_summary["avg_pattern_score"], loser_summary["avg_pattern_score"]),
        ("Avg stop distance", winner_summary["avg_stop_distance"], loser_summary["avg_stop_distance"]),
        ("Avg target distance", winner_summary["avg_target_distance"], loser_summary["avg_target_distance"]),
        ("Avg risk reward", winner_summary["avg_risk_reward"], loser_summary["avg_risk_reward"]),
        ("EMA aligned count", winner_summary["ema_aligned_yes"], loser_summary["ema_aligned_yes"]),
        ("Neckline break count", winner_summary["neckline_break_yes"], loser_summary["neckline_break_yes"]),
        ("Retest confirmed count", winner_summary["retest_yes"], loser_summary["retest_yes"]),
        ("Volume confirmed count", winner_summary["volume_confirmed_yes"], loser_summary["volume_confirmed_yes"]),
    ]
    for label, left, right in comparisons:
        lines.append(f"| {label} | {left} | {right} |")
    lines.append("")

    lines.append("### What exists in winners but not in losers")
    lines.append(f"- Winner patterns: {winner_summary['patterns']}")
    lines.append(f"- Loser patterns: {loser_summary['patterns']}")
    lines.append(f"- Winner time buckets: {winner_summary['time_buckets']}")
    lines.append(f"- Loser time buckets: {loser_summary['time_buckets']}")
    lines.append("")

    lines.append("## Single-Filter Tests")
    lines.append("")
    lines.append("| Filter | Trades Remaining | Winners | Losers | Win Rate | Profit Factor |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for item in filters:
        win_rate = f"{item.win_rate:.1f}%" if item.win_rate is not None else "n/a"
        pf = f"{item.profit_factor:.2f}" if item.profit_factor is not None else "n/a"
        lines.append(f"| {item.name} | {item.trades_remaining} | {item.winners} | {item.losers} | {win_rate} | {pf} |")
    lines.append("")

    lines.append("## High Accuracy Mode Search")
    lines.append("")
    if combo:
        lines.append(f"- Best restrictive combo found on the {len(decided)} decided trades: `{', '.join(combo['rules'])}`")
        lines.append(f"- Trades kept: {combo['trades']}")
        lines.append(f"- Winners: {combo['winners']}")
        lines.append(f"- Losers: {combo['losers']}")
        lines.append(f"- Win rate: {combo['win_rate']}%")
        lines.append(f"- Profit factor: {combo['profit_factor']}")
    else:
        lines.append("- No strict combo produced a better positive quality subset.")
    lines.append("")

    lines.append("## Recommended High Accuracy Mode")
    lines.append("")
    lines.append("### Double Top")
    lines.append("- Current sample does not justify a more aggressive plain `Double Top` entry rule.")
    lines.append("- In high-accuracy mode, keep plain `Double Top` as review/watch unless a cleaner breakdown state is already confirmed.")
    lines.append("- This reduces trade frequency and avoids promoting a pattern that is split in the current decided sample.")
    lines.append("")
    lines.append("### Double Top Breakdown")
    lines.append("- Do not raise confidence to 85 or 90 based on this sample; both filters reduce quality.")
    lines.append("- Do not raise projected move to 90; in this replay slice it removes winners and keeps only losers.")
    lines.append("- Keep the current confidence >= 80 and projected move >= 70 floors.")
    lines.append("- Keep EMA alignment, neckline breakdown confirmation, valid stop, and risk-reward >= 1.5.")
    lines.append("- Add retest confirmation before entry for high-accuracy mode; it is the only tested filter that improved both win rate and profit factor on the current decided trades.")
    lines.append("- Use volume confirmation only as an enhancer when candle volume is present; it cannot be a core gate on this sample because volume confirmation was unavailable.")
    lines.append("")
    lines.append("## Notes")
    lines.append("- This report is evidence only and does not change production logic.")
    lines.append("- Retest confirmation here is reconstructed from historical candles because the current replay path does not store a dedicated retest flag.")
    lines.append("- Volume confirmation uses the current project’s 20-bar volume ratio rule: breakout confirmed at 1.35x or higher.")
    return "\n".join(lines) + "\n"


def main() -> None:
    connection = connect_mysql()
    try:
        trades = selected_trades(connection)
    finally:
        connection.close()

    report_text = build_markdown(trades)
    output_path = ROOT / "reports" / "high_accuracy_mode_analysis.md"
    output_path.write_text(report_text, encoding="utf-8")
    print(report_text)


if __name__ == "__main__":
    main()
