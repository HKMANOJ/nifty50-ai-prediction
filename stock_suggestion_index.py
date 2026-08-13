#!/usr/bin/env python3
"""Build a real NSE intraday stock suggestion index.

The scanner intentionally ignores the old NIFTY option prediction pipeline.
It uses live NSE Top Gainers/Losers for F&O securities plus Change in Open
Interest by underlying to produce today's practical bullish and bearish lists.
No dummy rows are generated; if real data is unavailable the output says so.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "inputs"
OUTPUT_PATH = INPUT_DIR / "stock_suggestion_index.latest.json"
NSE_BASE = "https://www.nseindia.com"
INDIA_TZ = ZoneInfo("Asia/Kolkata")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MARKET_OPEN = time(9, 15)
SUGGESTION_READY = time(10, 0)
MARKET_CLOSE = time(15, 30)
NSE_TOP_ROWS = 20
DEFAULT_SUGGESTIONS_PER_SIDE = 5


class LiveDataError(RuntimeError):
    """Raised when a required live NSE source cannot be loaded."""


@dataclass(frozen=True)
class Suggestion:
    symbol: str
    side: str
    rank: int
    setup: str
    trade_plan: str
    ltp: float | None
    percent_change: float | None
    oi_change_percent: float | None
    oi_change: float | None
    volume: float | None
    value_lakhs: float | None
    entry: float | None
    stop_loss: float | None
    target: float | None
    score: int
    status: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "rank": self.rank,
            "setup": self.setup,
            "trade_plan": self.trade_plan,
            "ltp": self.ltp,
            "percent_change": self.percent_change,
            "oi_change_percent": self.oi_change_percent,
            "oi_change": self.oi_change,
            "volume": self.volume,
            "value_lakhs": self.value_lakhs,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "score": self.score,
            "status": self.status,
            "reason": self.reason,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build today's NSE F&O intraday stock suggestion index.")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help="JSON output path")
    parser.add_argument("--top", type=int, default=DEFAULT_SUGGESTIONS_PER_SIDE, help="Number of matched rows to keep per side")
    return parser.parse_args()


def parse_float(value: Any) -> float | None:
    if value in (None, "", "-", "--", "NA", "N/A"):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)

    text = str(value).strip()
    if not text:
        return None
    multiplier = 1.0
    lower = text.lower()
    if lower.endswith("cr"):
        multiplier = 10_000_000.0
        text = text[:-2]
    elif lower.endswith("l"):
        multiplier = 100_000.0
        text = text[:-1]
    elif lower.endswith("k"):
        multiplier = 1_000.0
        text = text[:-1]

    cleaned = (
        text.replace(",", "")
        .replace("%", "")
        .replace("₹", "")
        .replace("Rs.", "")
        .replace("rs.", "")
        .strip()
    )
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def first_value(row: dict[str, Any], keys: Iterable[str]) -> Any:
    lower_map = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        lower_key = key.lower()
        if lower_key in lower_map:
            return lower_map[lower_key]
    return None


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    for suffix in (".NS", "-EQ"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    return symbol


def nse_get_json(url: str, *, referer: str) -> Any:
    cookie_jar = Path(tempfile.gettempdir()) / "nifty_stock_suggestion_nse.cookies"
    base_headers = [
        "-A",
        USER_AGENT,
        "-H",
        "Accept: application/json, text/plain, */*",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-H",
        "Connection: keep-alive",
    ]

    # Use curl because NSE frequently blocks default Python urllib sessions.
    import subprocess

    warmup = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "-c",
        str(cookie_jar),
        *base_headers,
        "-e",
        referer,
        NSE_BASE,
    ]
    subprocess.run(warmup, cwd=ROOT, capture_output=True, text=True, timeout=20)

    cmd = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "-b",
        str(cookie_jar),
        *base_headers,
        "-e",
        referer,
        "-H",
        f"Referer: {referer}",
        url,
    ]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise LiveDataError(result.stderr.strip() or f"NSE request failed: {url}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LiveDataError(f"NSE returned non-JSON for {url}: {result.stdout[:120]!r}") from exc


def nse_get_csv(url: str, *, referer: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8-sig")
    return [dict(row) for row in csv.DictReader(text.splitlines())]


def rows_from_any_payload(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            rows.extend(row for row in payload["data"] if isinstance(row, dict))
        for value in payload.values():
            if isinstance(value, dict) and isinstance(value.get("data"), list):
                rows.extend(row for row in value["data"] if isinstance(row, dict))
            elif isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
    elif isinstance(payload, list):
        rows.extend(row for row in payload if isinstance(row, dict))
    return rows


def extract_fno_variation(payload: Any, *, side: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("FOSec"), dict):
        rows = payload["FOSec"].get("data") or []
    else:
        rows = rows_from_any_payload(payload)

    normalized = [normalize_variation_row(row, side=side) for row in rows if isinstance(row, dict)]
    filtered = [row for row in normalized if row["symbol"] and row["percent_change"] is not None]
    if side == "gainer":
        filtered = [row for row in filtered if row["percent_change"] >= 0]
        return sorted(filtered, key=lambda row: row["percent_change"] or 0, reverse=True)[:NSE_TOP_ROWS]
    filtered = [row for row in filtered if row["percent_change"] <= 0]
    return sorted(filtered, key=lambda row: row["percent_change"] or 0)[:NSE_TOP_ROWS]


def normalize_variation_row(row: dict[str, Any], *, side: str) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(first_value(row, ("symbol", "Symbol", "SYMBOL"))),
        "side": side,
        "open": parse_float(first_value(row, ("open_price", "openPrice", "open", "OPEN"))),
        "high": parse_float(first_value(row, ("high_price", "highPrice", "high", "HIGH"))),
        "low": parse_float(first_value(row, ("low_price", "lowPrice", "low", "LOW"))),
        "prev_close": parse_float(first_value(row, ("prev_price", "prevClose", "previousClose", "prev_close", "PREV. CLOSE"))),
        "ltp": parse_float(first_value(row, ("ltp", "lastPrice", "last_traded_price", "LTP"))),
        "percent_change": parse_float(first_value(row, ("netPrice", "pChange", "percent_change", "perChange", "%CHNG", "% Change"))),
        "volume": parse_float(first_value(row, ("traded_quantity", "totalTradedVolume", "volume", "VOLUME", "Volume (Shares)"))),
        "value_lakhs": parse_float(first_value(row, ("turnover", "totalTradedValue", "value", "VALUE", "Value (₹ Lakhs)"))),
    }


def fetch_top_gainers_losers() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    referer = f"{NSE_BASE}/market-data/top-gainers-losers"
    errors: list[str] = []
    payload = None
    for url in (
        f"{NSE_BASE}/api/live-analysis-variations?index=gainers",
        f"{NSE_BASE}/api/live-analysis-variations?index=gainers&type=FOSec",
    ):
        try:
            payload = nse_get_json(url, referer=referer)
            break
        except Exception as exc:  # noqa: BLE001 - report source errors to UI
            errors.append(f"gainers {url}: {exc}")
    if payload is None:
        raise LiveDataError("; ".join(errors))

    gainers = extract_fno_variation(payload, side="gainer")

    loser_payload = None
    for url in (
        f"{NSE_BASE}/api/live-analysis-variations?index=loosers",
        f"{NSE_BASE}/api/live-analysis-variations?index=losers",
        f"{NSE_BASE}/api/live-analysis-variations?index=loosers&type=FOSec",
    ):
        try:
            loser_payload = nse_get_json(url, referer=referer)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"losers {url}: {exc}")
    if loser_payload is None:
        raise LiveDataError("; ".join(errors))

    losers = extract_fno_variation(loser_payload, side="loser")
    return gainers, losers, errors


def normalize_oi_row(row: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(first_value(row, ("symbol", "underlying", "underlyingSymbol", "SYMBOL")))
    oi_now = parse_float(first_value(row, ("openInterest", "latestOI", "OPEN INTEREST", "oi", "currentOI")))
    oi_prev = parse_float(first_value(row, ("previousOpenInterest", "prevOI", "prevOpenInterest", "OPEN INTEREST Previous", "previousOI")))
    oi_change = parse_float(first_value(row, ("changeInOI", "changeOI", "CHNG IN OI", "change", "oiChange")))
    oi_change_percent = parse_float(first_value(row, ("pchangeinOpenInterest", "pChangeInOI", "%CHNG IN OI", "pctChangeOI", "oiChangePercent")))
    volume = parse_float(first_value(row, ("volume", "VOLUME (contracts)", "contracts", "futVolume")))
    futures_value = parse_float(first_value(row, ("futValue", "futuresValue", "FUTURES VALUE", "value")))

    if oi_change is None and oi_now is not None and oi_prev is not None:
        oi_change = oi_now - oi_prev
    if oi_change_percent is None and oi_prev not in (None, 0) and oi_change is not None:
        oi_change_percent = (oi_change / oi_prev) * 100

    return {
        "symbol": symbol,
        "oi_now": oi_now,
        "oi_prev": oi_prev,
        "oi_change": oi_change,
        "oi_change_percent": oi_change_percent,
        "volume_contracts": volume,
        "futures_value": futures_value,
    }


def fetch_change_in_oi() -> tuple[dict[str, dict[str, Any]], list[str]]:
    referer = f"{NSE_BASE}/market-data/oi-spurts"
    errors: list[str] = []
    payload = None
    for url in (
        f"{NSE_BASE}/api/live-analysis-oi-spurts-underlyings",
        f"{NSE_BASE}/api/live-analysis-oi-spurts-contracts",
    ):
        try:
            payload = nse_get_json(url, referer=referer)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"oi {url}: {exc}")
    if payload is None:
        raise LiveDataError("; ".join(errors))

    rows = [normalize_oi_row(row) for row in rows_from_any_payload(payload)]
    rows = [row for row in rows if row["symbol"]]
    by_symbol = {row["symbol"]: row for row in rows}
    return by_symbol, errors


def classify_oi(price_change: float | None, oi_change: float | None) -> str:
    if price_change is None or oi_change is None:
        return "OI not confirmed"
    if price_change >= 0 and oi_change > 0:
        return "Long buildup"
    if price_change >= 0 and oi_change < 0:
        return "Short covering"
    if price_change < 0 and oi_change > 0:
        return "Short buildup"
    if price_change < 0 and oi_change < 0:
        return "Long unwinding"
    return "Flat OI"


def calc_score(row: dict[str, Any], oi: dict[str, Any] | None, *, side: str, max_volume: float) -> int:
    price_change = abs(row.get("percent_change") or 0)
    price_score = min(price_change / 5.0, 1.0) * 35
    volume = row.get("volume") or 0
    volume_score = (volume / max_volume * 18) if max_volume > 0 else 0
    value_score = min((row.get("value_lakhs") or 0) / 100_000, 1.0) * 7

    oi_change_percent = abs((oi or {}).get("oi_change_percent") or 0)
    oi_direction = (oi or {}).get("oi_change")
    oi_score = min(oi_change_percent / 20.0, 1.0) * 25

    setup = classify_oi(row.get("percent_change"), oi_direction)
    alignment_score = 0
    if side == "bullish" and setup in {"Long buildup", "Short covering"}:
        alignment_score = 15 if setup == "Long buildup" else 10
    if side == "bearish" and setup in {"Short buildup", "Long unwinding"}:
        alignment_score = 15 if setup == "Short buildup" else 10

    return int(round(max(0, min(100, price_score + volume_score + value_score + oi_score + alignment_score))))


def build_trade_levels(row: dict[str, Any], *, side: str) -> tuple[float | None, float | None, float | None, str]:
    ltp = row.get("ltp")
    high = row.get("high")
    low = row.get("low")
    open_price = row.get("open")
    if ltp is None:
        return None, None, None, "Real LTP missing; do not trade."

    buffer = max(round(ltp * 0.0008, 2), 0.5)
    if side == "bullish":
        entry = round((high if high is not None else ltp) + buffer, 2)
        base_stop = low if low is not None else min(open_price or ltp, ltp - buffer)
        risk = max(entry - base_stop, max(ltp * 0.004, 3.0))
        stop = round(entry - risk, 2)
        target = round(entry + (risk * 1.7), 2)
        plan = "Buy only if price breaks above the morning high and holds above it."
    else:
        entry = round((low if low is not None else ltp) - buffer, 2)
        base_stop = high if high is not None else max(open_price or ltp, ltp + buffer)
        risk = max(base_stop - entry, max(ltp * 0.004, 3.0))
        stop = round(entry + risk, 2)
        target = round(entry - (risk * 1.7), 2)
        plan = "Sell/short only if price breaks below the morning low and holds below it."
    return entry, stop, target, plan


def status_from_score(score: int, setup: str) -> str:
    if score >= 78:
        return "Priority"
    if score >= 62:
        return "Watch"
    if "not confirmed" in setup.lower():
        return "Need OI"
    return "Low priority"


def build_suggestions(rows: list[dict[str, Any]], oi_by_symbol: dict[str, dict[str, Any]], *, side: str, top: int) -> list[Suggestion]:
    matched_rows = [row for row in rows if row["symbol"] in oi_by_symbol]
    max_volume = max([row.get("volume") or 0 for row in matched_rows] or [0])
    suggestions: list[Suggestion] = []
    for row in matched_rows:
        symbol = row["symbol"]
        oi = oi_by_symbol[symbol]
        setup = classify_oi(row.get("percent_change"), oi.get("oi_change"))
        score = calc_score(row, oi, side=side, max_volume=max_volume)
        entry, stop, target, plan = build_trade_levels(row, side=side)
        oi_change_percent = oi.get("oi_change_percent")
        oi_change = oi.get("oi_change")
        reason_parts = [
            setup,
            f"price move {row.get('percent_change'):+.2f}%" if row.get("percent_change") is not None else "price move unavailable",
        ]
        if oi_change_percent is not None:
            reason_parts.append(f"OI change {oi_change_percent:+.2f}%")
        else:
            reason_parts.append("OI change unavailable")
        if row.get("volume") is not None:
            reason_parts.append(f"volume {int(row['volume']):,}")
        suggestions.append(
            Suggestion(
                symbol=symbol,
                side="GAINER" if side == "bullish" else "LOSER",
                rank=0,
                setup=setup,
                trade_plan=plan,
                ltp=row.get("ltp"),
                percent_change=row.get("percent_change"),
                oi_change_percent=oi_change_percent,
                oi_change=oi_change,
                volume=row.get("volume"),
                value_lakhs=row.get("value_lakhs"),
                entry=entry,
                stop_loss=stop,
                target=target,
                score=score,
                status=status_from_score(score, setup),
                reason=" | ".join(reason_parts),
            )
        )

    suggestions.sort(key=lambda item: (item.score, abs(item.percent_change or 0)), reverse=True)
    ranked = suggestions[:top]
    return [
        Suggestion(**{**item.to_dict(), "rank": index})
        for index, item in enumerate(ranked, start=1)
    ]


def phase_for_time(now_ist: datetime) -> str:
    current = now_ist.time()
    if current < MARKET_OPEN:
        return "Pre-open: waiting for NSE 09:15 data"
    if MARKET_OPEN <= current < SUGGESTION_READY:
        return "Building opening scan: analyze until 10:00"
    if SUGGESTION_READY <= current <= MARKET_CLOSE:
        return "Suggestions ready for today's session"
    return "Market closed: today's list is archived/reset on next session"


def build_payload(top: int) -> dict[str, Any]:
    now_ist = datetime.now(INDIA_TZ)
    errors: list[str] = []
    gainers, losers, variation_errors = fetch_top_gainers_losers()
    errors.extend(variation_errors)
    oi_by_symbol, oi_errors = fetch_change_in_oi()
    errors.extend(oi_errors)

    bullish = build_suggestions(gainers, oi_by_symbol, side="bullish", top=top)
    bearish = build_suggestions(losers, oi_by_symbol, side="bearish", top=top)
    gainers_with_oi = sum(1 for row in gainers if row["symbol"] in oi_by_symbol)
    losers_with_oi = sum(1 for row in losers if row["symbol"] in oi_by_symbol)
    oi_matched = gainers_with_oi + losers_with_oi

    return {
        "ok": True,
        "data_mode": "real",
        "generated_at_ist": now_ist.isoformat(timespec="seconds"),
        "session_date": now_ist.date().isoformat(),
        "market_clock_ist": now_ist.strftime("%H:%M:%S"),
        "analysis_phase": phase_for_time(now_ist),
        "strategy_name": "Intraday Stock Suggestion Index",
        "strategy_rules": [
            "Use only stocks that appear in both NSE F&O Top 20 Gainers/Losers and Change in Open Interest.",
            "Prefer Long Buildup for bullish candidates and Short Buildup for bearish candidates.",
            "Trade only after the stock breaks its morning high/low; do not chase before breakout.",
            "Today-only list; refresh creates a new snapshot and the next session resets the view.",
        ],
        "source": {
            "top_gainers_losers": f"{NSE_BASE}/market-data/top-gainers-losers",
            "change_in_oi": f"{NSE_BASE}/market-data/oi-spurts",
        },
        "coverage": {
            "gainers_loaded": len(gainers),
            "losers_loaded": len(losers),
            "oi_symbols_loaded": len(oi_by_symbol),
            "gainers_with_oi_match": gainers_with_oi,
            "losers_with_oi_match": losers_with_oi,
            "top_rows_with_oi_match": oi_matched,
            "max_suggestions_per_side": top,
        },
        "summary": {
            "bullish_candidates": len(bullish),
            "bearish_candidates": len(bearish),
            "priority_bullish": sum(1 for item in bullish if item.status == "Priority"),
            "priority_bearish": sum(1 for item in bearish if item.status == "Priority"),
        },
        "bullish": [item.to_dict() for item in bullish],
        "bearish": [item.to_dict() for item in bearish],
        "warnings": errors,
    }


def error_payload(exc: Exception) -> dict[str, Any]:
    now_ist = datetime.now(INDIA_TZ)
    return {
        "ok": False,
        "data_mode": "real",
        "generated_at_ist": now_ist.isoformat(timespec="seconds"),
        "session_date": now_ist.date().isoformat(),
        "market_clock_ist": now_ist.strftime("%H:%M:%S"),
        "analysis_phase": phase_for_time(now_ist),
        "error": "real_nse_data_unavailable",
        "message": str(exc),
        "bullish": [],
        "bearish": [],
        "summary": {
            "bullish_candidates": 0,
            "bearish_candidates": 0,
            "priority_bullish": 0,
            "priority_bearish": 0,
        },
        "coverage": {
            "gainers_loaded": 0,
            "losers_loaded": 0,
            "oi_symbols_loaded": 0,
            "gainers_with_oi_match": 0,
            "losers_with_oi_match": 0,
            "top_rows_with_oi_match": 0,
            "max_suggestions_per_side": DEFAULT_SUGGESTIONS_PER_SIDE,
        },
    }


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    try:
        payload = build_payload(args.top)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - the UI needs a clear live-data status
        payload = error_payload(exc)
        exit_code = 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
