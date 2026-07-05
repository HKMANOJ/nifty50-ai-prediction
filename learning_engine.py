#!/usr/bin/env python3
"""Professional calibration, learning, and backtest reporting for NIFTY AI."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from store_candles_mysql import connect_mysql


MIN_COMPLETED_TRADES = 30
WEIGHT_STEP = 0.03
MIN_WEIGHT = 0.65
MAX_WEIGHT = 1.35
REGIMES = (
    "Trending Bull",
    "Trending Bear",
    "Sideways",
    "High Volatility",
    "Low Volatility",
)


CREATE_PATTERN_PERFORMANCE_SQL = """
CREATE TABLE IF NOT EXISTS PatternPerformance (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  pattern_name VARCHAR(128) NOT NULL UNIQUE,
  total_trades INT NOT NULL DEFAULT 0,
  wins INT NOT NULL DEFAULT 0,
  losses INT NOT NULL DEFAULT 0,
  accuracy DECIMAL(7,2) NOT NULL DEFAULT 0,
  net_profit_points DECIMAL(12,2) NOT NULL DEFAULT 0,
  average_win DECIMAL(12,2) NOT NULL DEFAULT 0,
  average_loss DECIMAL(12,2) NOT NULL DEFAULT 0,
  profit_factor DECIMAL(12,4) NOT NULL DEFAULT 0,
  max_drawdown DECIMAL(12,2) NOT NULL DEFAULT 0,
  weight_multiplier DECIMAL(8,4) NOT NULL DEFAULT 1,
  last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_pattern_perf_updated (last_updated)
)
"""


CREATE_MARKET_REGIME_SQL = """
CREATE TABLE IF NOT EXISTS MarketRegime (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  regime_name VARCHAR(64) NOT NULL,
  pattern_name VARCHAR(128) NOT NULL,
  total_trades INT NOT NULL DEFAULT 0,
  wins INT NOT NULL DEFAULT 0,
  losses INT NOT NULL DEFAULT 0,
  accuracy DECIMAL(7,2) NOT NULL DEFAULT 0,
  net_profit_points DECIMAL(12,2) NOT NULL DEFAULT 0,
  average_win DECIMAL(12,2) NOT NULL DEFAULT 0,
  average_loss DECIMAL(12,2) NOT NULL DEFAULT 0,
  profit_factor DECIMAL(12,4) NOT NULL DEFAULT 0,
  max_drawdown DECIMAL(12,2) NOT NULL DEFAULT 0,
  weight_multiplier DECIMAL(8,4) NOT NULL DEFAULT 1,
  last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_regime_pattern (regime_name, pattern_name),
  INDEX idx_regime_perf_updated (last_updated)
)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build NIFTY AI learning and calibration reports.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create PatternPerformance and MarketRegime tables")
    sub.add_parser("rebuild", help="Recalculate learning tables from completed ai_options trades")
    sub.add_parser("report", help="Recalculate and print the dashboard learning report")
    return parser.parse_args()


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.split(".")[0], fmt)
            except ValueError:
                continue
    return None


def serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize(item) for item in value]
    return value


def clean_pattern_name(value: Any) -> str:
    name = str(value or "").strip()
    return name if name else "Unclassified Pattern"


def trade_time(trade: dict[str, Any]) -> datetime | None:
    return (
        as_datetime(trade.get("opened_market_time"))
        or as_datetime(trade.get("closed_market_time"))
        or as_datetime(trade.get("created_at"))
    )


def learning_pnl(trade: dict[str, Any]) -> float:
    """Use premium P&L when available; otherwise use NIFTY point movement."""
    premium = trade.get("pnl_premium")
    if premium is not None:
        return as_float(premium)
    return as_float(trade.get("result_points"))


def point_pnl(trade: dict[str, Any]) -> float:
    return as_float(trade.get("result_points"))


def init_tables(connection) -> None:
    cursor = connection.cursor()
    cursor.execute(CREATE_PATTERN_PERFORMANCE_SQL)
    cursor.execute(CREATE_MARKET_REGIME_SQL)
    connection.commit()


def read_completed_trades(connection) -> list[dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT *
            FROM ai_options
            WHERE status IN ('SUCCESS', 'FAILURE')
            ORDER BY COALESCE(opened_market_time, created_at) ASC, id ASC
            """
        )
        return cursor.fetchall() or []
    except Exception:
        return []


def current_weight(connection, table: str, pattern_name: str, regime_name: str | None = None) -> float:
    cursor = connection.cursor(dictionary=True)
    if table == "MarketRegime" and regime_name:
        cursor.execute(
            "SELECT weight_multiplier FROM MarketRegime WHERE regime_name = %s AND pattern_name = %s",
            (regime_name, pattern_name),
        )
    else:
        cursor.execute(
            "SELECT weight_multiplier FROM PatternPerformance WHERE pattern_name = %s",
            (pattern_name,),
        )
    row = cursor.fetchone()
    return as_float(row.get("weight_multiplier"), 1.0) if row else 1.0


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 2)


def profit_factor(values: list[float]) -> float:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value < 0))
    if gross_profit <= 0:
        return 0.0
    if gross_loss <= 0:
        return 999.0
    return round(gross_profit / gross_loss, 4)


def average_rr(trades: list[dict[str, Any]]) -> float:
    ratios: list[float] = []
    for trade in trades:
        premium_entry = trade.get("premium_entry")
        premium_target = trade.get("premium_target")
        premium_stop = trade.get("premium_stop_loss")
        if premium_entry is not None and premium_target is not None and premium_stop is not None:
            reward = abs(as_float(premium_target) - as_float(premium_entry))
            risk = abs(as_float(premium_entry) - as_float(premium_stop))
        else:
            reward = abs(as_float(trade.get("target_price")) - as_float(trade.get("entry_price")))
            risk = abs(as_float(trade.get("entry_price")) - as_float(trade.get("stop_loss")))
        if reward > 0 and risk > 0:
            ratios.append(reward / risk)
    return round(sum(ratios) / len(ratios), 2) if ratios else 0.0


def metric_block(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [learning_pnl(trade) for trade in trades]
    point_values = [point_pnl(trade) for trade in trades]
    wins = sum(1 for trade in trades if str(trade.get("status")).upper() == "SUCCESS")
    losses = sum(1 for trade in trades if str(trade.get("status")).upper() == "FAILURE")
    positive = [value for value in values if value > 0]
    negative = [value for value in values if value < 0]
    premium_available = sum(1 for trade in trades if trade.get("pnl_premium") is not None)
    total = len(trades)
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "accuracy": round((wins / total) * 100, 2) if total else 0.0,
        "net_profit_points": round(sum(point_values), 2),
        "learning_net_profit": round(sum(values), 2),
        "average_win": round(sum(positive) / len(positive), 2) if positive else 0.0,
        "average_loss": round(sum(negative) / len(negative), 2) if negative else 0.0,
        "profit_factor": profit_factor(values),
        "max_drawdown": max_drawdown(values),
        "average_rr": average_rr(trades),
        "expectancy": round(sum(values) / total, 2) if total else 0.0,
        "premium_coverage_pct": round((premium_available / total) * 100, 2) if total else 0.0,
        "premium_net_profit": round(sum(as_float(trade.get("pnl_premium")) for trade in trades if trade.get("pnl_premium") is not None), 2),
    }


def adjusted_weight(existing: float, metrics: dict[str, Any]) -> tuple[float, str]:
    total = int(metrics["total_trades"])
    if total < MIN_COMPLETED_TRADES:
        return 1.0, f"collecting_samples_{total}_of_{MIN_COMPLETED_TRADES}"

    accuracy = as_float(metrics["accuracy"])
    pf = as_float(metrics["profit_factor"])
    net = as_float(metrics["learning_net_profit"])
    drawdown = as_float(metrics["max_drawdown"])

    delta = 0.0
    reason = "stable"
    if accuracy > 65 and pf > 1.5 and net > 0:
        delta = WEIGHT_STEP
        reason = "strong_pattern_slow_increase"
    elif (accuracy < 50 and pf < 1.0) or (net < 0 and drawdown > abs(net) * 0.5):
        delta = -WEIGHT_STEP
        reason = "weak_pattern_slow_decrease"

    next_weight = max(MIN_WEIGHT, min(MAX_WEIGHT, existing + delta))
    return round(next_weight, 4), reason


def upsert_pattern(connection, pattern_name: str, metrics: dict[str, Any], weight: float) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO PatternPerformance
          (pattern_name, total_trades, wins, losses, accuracy, net_profit_points,
           average_win, average_loss, profit_factor, max_drawdown, weight_multiplier)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          total_trades = VALUES(total_trades),
          wins = VALUES(wins),
          losses = VALUES(losses),
          accuracy = VALUES(accuracy),
          net_profit_points = VALUES(net_profit_points),
          average_win = VALUES(average_win),
          average_loss = VALUES(average_loss),
          profit_factor = VALUES(profit_factor),
          max_drawdown = VALUES(max_drawdown),
          weight_multiplier = VALUES(weight_multiplier),
          last_updated = CURRENT_TIMESTAMP
        """,
        (
            pattern_name,
            metrics["total_trades"],
            metrics["wins"],
            metrics["losses"],
            metrics["accuracy"],
            metrics["net_profit_points"],
            metrics["average_win"],
            metrics["average_loss"],
            metrics["profit_factor"],
            metrics["max_drawdown"],
            weight,
        ),
    )


def upsert_regime(connection, regime_name: str, pattern_name: str, metrics: dict[str, Any], weight: float) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO MarketRegime
          (regime_name, pattern_name, total_trades, wins, losses, accuracy, net_profit_points,
           average_win, average_loss, profit_factor, max_drawdown, weight_multiplier)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          total_trades = VALUES(total_trades),
          wins = VALUES(wins),
          losses = VALUES(losses),
          accuracy = VALUES(accuracy),
          net_profit_points = VALUES(net_profit_points),
          average_win = VALUES(average_win),
          average_loss = VALUES(average_loss),
          profit_factor = VALUES(profit_factor),
          max_drawdown = VALUES(max_drawdown),
          weight_multiplier = VALUES(weight_multiplier),
          last_updated = CURRENT_TIMESTAMP
        """,
        (
            regime_name,
            pattern_name,
            metrics["total_trades"],
            metrics["wins"],
            metrics["losses"],
            metrics["accuracy"],
            metrics["net_profit_points"],
            metrics["average_win"],
            metrics["average_loss"],
            metrics["profit_factor"],
            metrics["max_drawdown"],
            weight,
        ),
    )


def fetch_context_candles(connection, trade: dict[str, Any], limit: int = 48) -> list[dict[str, Any]]:
    opened = trade_time(trade)
    if not opened:
        return []
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT market_time, open, high, low, close, volume
            FROM nifty_candles
            WHERE symbol = %s
              AND timeframe = %s
              AND market_time <= %s
            ORDER BY market_time DESC
            LIMIT %s
            """,
            (
                trade.get("symbol") or "NIFTY50",
                trade.get("timeframe") or "5m",
                opened,
                limit,
            ),
        )
        return list(reversed(cursor.fetchall() or []))
    except Exception:
        return []


def detect_regimes(candles: list[dict[str, Any]]) -> list[str]:
    if len(candles) < 12:
        return ["Sideways"]

    first_close = as_float(candles[0].get("close"))
    last_close = as_float(candles[-1].get("close"))
    highs = [as_float(row.get("high")) for row in candles]
    lows = [as_float(row.get("low")) for row in candles]
    closes = [as_float(row.get("close")) for row in candles]
    if first_close <= 0 or last_close <= 0:
        return ["Sideways"]

    change_pct = ((last_close - first_close) / first_close) * 100
    realized_range_pct = ((max(highs) - min(lows)) / last_close) * 100
    avg_bar_range_pct = sum(((as_float(row.get("high")) - as_float(row.get("low"))) / max(as_float(row.get("close")), 1)) * 100 for row in candles) / len(candles)
    midline = sum(closes[-8:]) / min(8, len(closes))

    regimes: list[str] = []
    if change_pct >= 0.22 and last_close >= midline:
        regimes.append("Trending Bull")
    elif change_pct <= -0.22 and last_close <= midline:
        regimes.append("Trending Bear")
    else:
        regimes.append("Sideways")

    if realized_range_pct >= 0.75 or avg_bar_range_pct >= 0.11:
        regimes.append("High Volatility")
    elif realized_range_pct <= 0.28 and avg_bar_range_pct <= 0.055:
        regimes.append("Low Volatility")

    return [regime for regime in regimes if regime in REGIMES]


def confidence_calibration(trades: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {"60": [], "70": [], "80": [], "90": []}
    for trade in trades:
        confidence = int(as_float(trade.get("confidence"), 0))
        if confidence < 55:
            continue
        bucket = min(90, max(60, int(round(confidence / 10.0) * 10)))
        buckets[str(bucket)].append(trade)

    report: dict[str, Any] = {}
    errors: list[float] = []
    for bucket, bucket_trades in buckets.items():
        total = len(bucket_trades)
        wins = sum(1 for trade in bucket_trades if str(trade.get("status")).upper() == "SUCCESS")
        actual = round((wins / total) * 100, 2) if total else None
        expected = float(bucket)
        error = round(actual - expected, 2) if actual is not None else None
        if error is not None:
            errors.append(abs(error))
        report[bucket] = {
            "expected_confidence": expected,
            "actual_success_rate": actual,
            "sample_size": total,
            "calibration_error": error,
        }

    report["mean_absolute_error"] = round(sum(errors) / len(errors), 2) if errors else None
    report["status"] = "reliable" if sum(len(items) for items in buckets.values()) >= 100 else "collecting_samples"
    return report


def backtest_for_months(trades: list[dict[str, Any]], months: int) -> dict[str, Any]:
    now = datetime.now()
    cutoff = now - timedelta(days=months * 30)
    window = [trade for trade in trades if (trade_time(trade) or datetime.min) >= cutoff]
    metrics = metric_block(window)
    return {
        "months": months,
        "total_trades": metrics["total_trades"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "accuracy": metrics["accuracy"],
        "profit_factor": metrics["profit_factor"],
        "max_drawdown": metrics["max_drawdown"],
        "average_rr": metrics["average_rr"],
        "expectancy": metrics["expectancy"],
    }


def walk_forward_report(trades: list[dict[str, Any]]) -> dict[str, Any]:
    dated = [(trade_time(trade), trade) for trade in trades if trade_time(trade)]
    if not dated:
        return {"windows": [], "summary": "No dated completed trades available."}

    dated.sort(key=lambda item: item[0])
    start = dated[0][0]
    end = dated[-1][0]
    windows: list[dict[str, Any]] = []
    cursor = start
    while cursor + timedelta(days=120) <= end + timedelta(days=1):
        train_end = cursor + timedelta(days=90)
        validate_end = train_end + timedelta(days=30)
        train = [trade for dt, trade in dated if cursor <= dt < train_end]
        validate = [trade for dt, trade in dated if train_end <= dt < validate_end]
        if train or validate:
            windows.append(
                {
                    "train_start": cursor.date().isoformat(),
                    "train_end": train_end.date().isoformat(),
                    "validate_end": validate_end.date().isoformat(),
                    "train": metric_block(train),
                    "validate": metric_block(validate),
                    "learning_ready": len(train) >= MIN_COMPLETED_TRADES,
                }
            )
        cursor += timedelta(days=30)

    validation_metrics = [window["validate"] for window in windows if window["validate"]["total_trades"]]
    if validation_metrics:
        avg_accuracy = sum(item["accuracy"] for item in validation_metrics) / len(validation_metrics)
        avg_expectancy = sum(item["expectancy"] for item in validation_metrics) / len(validation_metrics)
        avg_pf = sum(item["profit_factor"] for item in validation_metrics if item["profit_factor"] < 900) / max(1, sum(1 for item in validation_metrics if item["profit_factor"] < 900))
        summary = {
            "windows": len(windows),
            "avg_validation_accuracy": round(avg_accuracy, 2),
            "avg_validation_profit_factor": round(avg_pf, 2),
            "avg_validation_expectancy": round(avg_expectancy, 2),
        }
    else:
        summary = {"windows": len(windows), "message": "Need more rolling validation samples."}

    return {"windows": windows, "summary": summary}


def read_pattern_rows(connection) -> list[dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT *
        FROM PatternPerformance
        ORDER BY total_trades DESC, net_profit_points DESC
        """
    )
    return cursor.fetchall() or []


def read_market_regime_rows(connection) -> list[dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT *
        FROM MarketRegime
        ORDER BY regime_name ASC, total_trades DESC, net_profit_points DESC
        """
    )
    return cursor.fetchall() or []


def best_and_worst_pattern(rows: list[dict[str, Any]]) -> tuple[str, str]:
    ready = [row for row in rows if as_float(row.get("total_trades")) > 0]
    if not ready:
        return "Collecting data", "Collecting data"
    best = max(
        ready,
        key=lambda row: (
            as_float(row.get("profit_factor")) if as_float(row.get("profit_factor")) < 900 else 99,
            as_float(row.get("net_profit_points")),
            as_float(row.get("accuracy")),
        ),
    )
    worst = min(
        ready,
        key=lambda row: (
            as_float(row.get("net_profit_points")),
            as_float(row.get("profit_factor")) if as_float(row.get("profit_factor")) < 900 else 99,
            -as_float(row.get("max_drawdown")),
        ),
    )
    return clean_pattern_name(best.get("pattern_name")), clean_pattern_name(worst.get("pattern_name"))


def strategy_grade(metrics: dict[str, Any], calibration: dict[str, Any]) -> str:
    total = int(metrics["total_trades"])
    accuracy = as_float(metrics["accuracy"])
    pf = as_float(metrics["profit_factor"])
    drawdown = as_float(metrics["max_drawdown"])
    net = as_float(metrics["learning_net_profit"])
    calibration_error = as_float(calibration.get("mean_absolute_error"), 25)

    if total >= 100 and accuracy >= 65 and pf >= 1.8 and net > 0 and calibration_error <= 8 and drawdown <= max(net * 0.35, 1):
        return "A"
    if total >= 50 and accuracy >= 56 and pf >= 1.3 and net > 0 and calibration_error <= 15:
        return "B"
    if total >= 20 and accuracy >= 50 and pf >= 1.0:
        return "C"
    return "D"


def maturity_score(metrics: dict[str, Any], calibration: dict[str, Any], walk_forward: dict[str, Any]) -> int:
    total = int(metrics["total_trades"])
    pf = as_float(metrics["profit_factor"])
    accuracy = as_float(metrics["accuracy"])
    premium_coverage = as_float(metrics["premium_coverage_pct"])
    calibration_error = calibration.get("mean_absolute_error")
    windows = int((walk_forward.get("summary") or {}).get("windows") or 0) if isinstance(walk_forward.get("summary"), dict) else 0

    score = 30
    score += min(20, total // 5)
    score += 15 if pf >= 1.5 else 8 if pf >= 1.1 else 0
    score += 12 if accuracy >= 65 else 7 if accuracy >= 55 else 0
    score += 10 if premium_coverage >= 70 else 5 if premium_coverage >= 30 else 0
    if calibration_error is not None:
        score += 10 if calibration_error <= 8 else 5 if calibration_error <= 15 else 0
    score += min(8, windows * 2)
    return max(0, min(100, score))


def expected_improvement(metrics: dict[str, Any]) -> str:
    total = int(metrics["total_trades"])
    premium_coverage = as_float(metrics["premium_coverage_pct"])
    if total < MIN_COMPLETED_TRADES:
        return "5-8% after 30+ completed trades"
    if premium_coverage < 50:
        return "10-15% after reliable premium capture"
    return "6-10% from calibrated weights and regime filters"


def build_dashboard_report(connection, trades: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = metric_block(trades)
    calibration = confidence_calibration(trades)
    backtests = [backtest_for_months(trades, months) for months in (3, 6, 12)]
    walk_forward = walk_forward_report(trades)
    pattern_rows = read_pattern_rows(connection)
    regime_rows = read_market_regime_rows(connection)
    best_pattern, worst_pattern = best_and_worst_pattern(pattern_rows)
    grade = strategy_grade(metrics, calibration)
    score = maturity_score(metrics, calibration, walk_forward)

    learning_status = (
        f"Collecting samples ({metrics['total_trades']}/{MIN_COMPLETED_TRADES})"
        if metrics["total_trades"] < MIN_COMPLETED_TRADES
        else "Active - slow weight updates enabled"
    )

    return {
        "ok": True,
        "summary": {
            "net_profit_points": metrics["net_profit_points"],
            "profit_factor": metrics["profit_factor"],
            "max_drawdown": metrics["max_drawdown"],
            "best_pattern": best_pattern,
            "worst_pattern": worst_pattern,
            "learning_status": learning_status,
            "model_confidence_calibration": calibration,
            "strategy_grade": grade,
            "project_maturity_score": score,
            "expected_improvement_after_calibration": expected_improvement(metrics),
        },
        "current_strategy_grade": grade,
        "current_project_maturity_score": score,
        "expected_improvement_after_calibration": expected_improvement(metrics),
        "backtests": backtests,
        "walk_forward": walk_forward,
        "pattern_performance": [serialize(row) for row in pattern_rows],
        "market_regime_performance": [serialize(row) for row in regime_rows],
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def rebuild_learning(connection) -> dict[str, Any]:
    init_tables(connection)
    trades = read_completed_trades(connection)

    by_pattern: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_pattern[clean_pattern_name(trade.get("pattern_name"))].append(trade)

    learning_actions: list[dict[str, Any]] = []
    for pattern_name, pattern_trades in sorted(by_pattern.items()):
        metrics = metric_block(pattern_trades)
        existing = current_weight(connection, "PatternPerformance", pattern_name)
        weight, reason = adjusted_weight(existing, metrics)
        upsert_pattern(connection, pattern_name, metrics, weight)
        learning_actions.append({"pattern_name": pattern_name, "weight_multiplier": weight, "reason": reason})

    by_regime_pattern: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    candle_cache: dict[int, list[dict[str, Any]]] = {}
    for trade in trades:
        trade_id = int(trade.get("id") or 0)
        candle_cache[trade_id] = fetch_context_candles(connection, trade)
        regimes = detect_regimes(candle_cache[trade_id])
        pattern_name = clean_pattern_name(trade.get("pattern_name"))
        for regime in regimes:
            by_regime_pattern[(regime, pattern_name)].append(trade)

    for (regime_name, pattern_name), regime_trades in sorted(by_regime_pattern.items()):
        metrics = metric_block(regime_trades)
        existing = current_weight(connection, "MarketRegime", pattern_name, regime_name)
        weight, _reason = adjusted_weight(existing, metrics)
        upsert_regime(connection, regime_name, pattern_name, metrics, weight)

    connection.commit()
    report = build_dashboard_report(connection, trades)
    report["learning_actions"] = learning_actions
    return report


def main() -> None:
    args = parse_args()
    try:
        connection = connect_mysql()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "mysql_unavailable", "message": str(exc)}, indent=2))
        return

    try:
        if args.command == "init":
            init_tables(connection)
            output = {"ok": True, "tables": ["PatternPerformance", "MarketRegime"], "regimes": REGIMES}
        elif args.command in {"rebuild", "report"}:
            output = rebuild_learning(connection)
        else:
            output = {"ok": False, "error": "unknown_command"}
    except Exception as exc:
        output = {"ok": False, "error": "learning_engine_failed", "message": str(exc)}
    finally:
        connection.close()

    print(json.dumps(serialize(output), indent=2))


if __name__ == "__main__":
    main()
