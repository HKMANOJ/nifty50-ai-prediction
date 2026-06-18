#!/usr/bin/env python3
"""Build real world/news signals for the NIFTY 50 app.

Sources:
- GDELT DOC 2.0 API for multilingual world-news article retrieval
- FRED API for S&P 500 and Brent crude daily observations
- NewsAPI optionally enriches article coverage if NEWSAPI_KEY is set

Required environment:
- FRED_API_KEY

Optional environment:
- NEWSAPI_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "inputs" / "world_signals.json"
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
NEWSAPI_EVERYTHING = "https://newsapi.org/v2/everything"
FRED_SERIES_OBSERVATIONS = "https://api.stlouisfed.org/fred/series/observations"

POSITIVE_WORDS = {
    "gain", "gains", "growth", "strong", "surge", "surges", "beat", "beats",
    "eases", "easing", "cooling", "optimism", "optimistic", "rally", "rises",
    "rose", "positive", "supportive", "stable", "stability", "improves",
    "improved", "rebound", "rebounds", "bullish", "softens", "recovery"
}
NEGATIVE_WORDS = {
    "fall", "falls", "fell", "drop", "drops", "decline", "declines", "weak",
    "weakness", "war", "conflict", "tariff", "tariffs", "sanction", "sanctions",
    "inflation", "hawkish", "volatility", "selloff", "sell-off", "bearish",
    "risk", "risks", "shock", "slump", "slumps", "concern", "concerns",
    "recession", "fear", "fears", "spike", "spikes"
}


@dataclass
class SeriesPoint:
    date: str
    value: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build real world/news signals for the NIFTY 50 app.")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help="Output path for world_signals.json")
    parser.add_argument("--timespan", default="3days", help="GDELT timespan like 3days, 72h, 1week")
    parser.add_argument("--maxrecords", type=int, default=50, help="Max records per GDELT query")
    return parser.parse_args()


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def score_texts(texts: list[str]) -> float:
    if not texts:
        return 0.0

    total = 0
    positive = 0
    negative = 0
    for text in texts:
        for token in text.lower().replace("/", " ").replace("-", " ").split():
            cleaned = token.strip(".,:;!?()[]{}\"'")
            if not cleaned:
                continue
            total += 1
            if cleaned in POSITIVE_WORDS:
                positive += 1
            if cleaned in NEGATIVE_WORDS:
                negative += 1

    if total == 0:
        return 0.0
    return round(clamp((positive - negative) / total * 8, -1, 1), 3)


def gdelt_query(query: str, *, timespan: str, maxrecords: int) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": str(maxrecords),
        "timespan": timespan,
        "sort": "datedesc",
        "format": "jsonfeed",
    }
    url = GDELT_DOC_API + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)
    return payload.get("items") or []


def newsapi_query(query: str) -> list[dict[str, Any]]:
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return []

    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": "50",
        "from": (datetime.now(UTC) - timedelta(days=3)).date().isoformat(),
    }
    url = NEWSAPI_EVERYTHING + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url, headers={"X-Api-Key": api_key})
    return payload.get("articles") or []


def extract_article_texts(records: list[dict[str, Any]], *, source: str) -> list[str]:
    texts: list[str] = []
    for item in records:
        if source == "gdelt":
            candidate = " ".join(
                part for part in [
                    item.get("title"),
                    item.get("content_text"),
                    item.get("summary"),
                ]
                if part
            ).strip()
        else:
            candidate = " ".join(
                part for part in [
                    item.get("title"),
                    item.get("description"),
                    item.get("content"),
                ]
                if part
            ).strip()
        if candidate:
            texts.append(candidate)
    return texts


def take_headlines(records: list[dict[str, Any]], *, source: str, limit: int = 5) -> list[dict[str, str]]:
    trimmed: list[dict[str, str]] = []
    for item in records[:limit]:
        if source == "gdelt":
            trimmed.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "published_at": item.get("date_published", ""),
                }
            )
        else:
            trimmed.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "published_at": item.get("publishedAt", ""),
                }
            )
    return trimmed


def classify_impact(score: float, positive_at: float = 0.08, negative_at: float = -0.08) -> str:
    if score >= positive_at:
        return "positive"
    if score <= negative_at:
        return "negative"
    return "neutral"


def fred_latest_change(series_id: str, api_key: str) -> dict[str, Any]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "10",
    }
    url = FRED_SERIES_OBSERVATIONS + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)
    observations = payload.get("observations") or []

    points: list[SeriesPoint] = []
    for item in observations:
        value = item.get("value")
        if value in (None, ".", ""):
            continue
        points.append(SeriesPoint(date=item.get("date", ""), value=float(value)))
        if len(points) == 2:
            break

    if len(points) < 2:
        raise RuntimeError(f"FRED series {series_id} did not return 2 usable observations")

    latest, previous = points[0], points[1]
    change_pct = ((latest.value - previous.value) / previous.value) * 100.0
    return {
        "series_id": series_id,
        "date": latest.date,
        "value": round(latest.value, 4),
        "previous_value": round(previous.value, 4),
        "change_pct": round(change_pct, 4),
    }


def build_factor(name: str, score: float, article_count: int, detail: str) -> dict[str, str]:
    return {
        "factor": name,
        "impact": classify_impact(score),
        "detail": f"{detail} Article count: {article_count}. Sentiment score: {score:+.3f}.",
    }


def main() -> None:
    args = parse_args()
    fred_api_key = os.environ.get("FRED_API_KEY")
    if not fred_api_key:
        raise SystemExit("FRED_API_KEY is required to build real world_signals.json")

    india_query = '("india" OR "nifty" OR "sensex" OR "reserve bank of india" OR "rupee" OR "inflation")'
    global_query = '("war" OR "conflict" OR "tariff" OR "sanctions" OR "oil" OR "crude" OR "inflation" OR "fed")'

    india_gdelt = gdelt_query(india_query, timespan=args.timespan, maxrecords=args.maxrecords)
    global_gdelt = gdelt_query(global_query, timespan=args.timespan, maxrecords=args.maxrecords)
    india_newsapi = newsapi_query("India stock market OR Nifty OR RBI OR rupee OR inflation")
    global_newsapi = newsapi_query("tariffs OR war OR sanctions OR crude oil OR inflation OR Federal Reserve")

    india_texts = extract_article_texts(india_gdelt, source="gdelt") + extract_article_texts(india_newsapi, source="newsapi")
    global_texts = extract_article_texts(global_gdelt, source="gdelt") + extract_article_texts(global_newsapi, source="newsapi")

    india_score = score_texts(india_texts)
    global_score = score_texts(global_texts)
    us_markets = fred_latest_change("SP500", fred_api_key)
    brent = fred_latest_change("DCOILBRENTEU", fred_api_key)

    output = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "data_mode": "real",
            "notes": [
                "All values in this file are derived from live source responses.",
                "No synthetic or fallback content is included.",
            ],
        },
        "us_markets": us_markets,
        "brent": brent,
        "news_sentiment": {
            "india": india_score,
            "global": global_score,
        },
        "geopolitical_factors": [
            build_factor(
                "Global risk coverage",
                global_score,
                len(global_texts),
                "Derived from live global-risk news coverage collected through GDELT and optionally NewsAPI.",
            ),
            build_factor(
                "Oil and commodity pressure",
                -clamp(brent["change_pct"] / 3.0, -1, 1),
                len(global_gdelt),
                "Derived from Brent crude change and overlapping global commodity coverage.",
            ),
        ],
        "domestic_factors": [
            build_factor(
                "India macro and market coverage",
                india_score,
                len(india_texts),
                "Derived from live India-focused news coverage collected through GDELT and optionally NewsAPI.",
            ),
            build_factor(
                "US equity spillover",
                clamp(us_markets["change_pct"] / 2.0, -1, 1),
                0,
                "Derived from the latest S&P 500 daily move from FRED.",
            ),
        ],
        "article_counts": {
            "india_market": len(india_texts),
            "global_risk": len(global_texts),
        },
        "headlines": {
            "india_market": take_headlines(india_gdelt, source="gdelt") + take_headlines(india_newsapi, source="newsapi"),
            "global_risk": take_headlines(global_gdelt, source="gdelt") + take_headlines(global_newsapi, source="newsapi"),
        },
        "provenance": [
            {
                "provider": "GDELT DOC 2.0 API",
                "query": india_query,
                "timespan": args.timespan,
                "records": len(india_gdelt),
            },
            {
                "provider": "GDELT DOC 2.0 API",
                "query": global_query,
                "timespan": args.timespan,
                "records": len(global_gdelt),
            },
            {
                "provider": "FRED",
                "series_id": "SP500",
                "records": 2,
            },
            {
                "provider": "FRED",
                "series_id": "DCOILBRENTEU",
                "records": 2,
            },
        ],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output_path)}, indent=2))


if __name__ == "__main__":
    main()
