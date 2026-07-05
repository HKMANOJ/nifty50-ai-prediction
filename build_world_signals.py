#!/usr/bin/env python3
"""Build real world/news signals for the NIFTY 50 app.

Sources used by default:
- Yahoo Finance chart API for S&P 500 and Brent daily closes
- Google News RSS for India/global headline coverage

Optional enrichments:
- GDELT DOC 2.0 API when it responds successfully
- NewsAPI when NEWSAPI_KEY is set
- FRED when FRED_API_KEY is set and Yahoo is unavailable

This script does not fabricate market values. It only writes output when the
required live upstream calls succeed.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "inputs" / "world_signals.json"
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
NEWSAPI_EVERYTHING = "https://newsapi.org/v2/everything"
FRED_SERIES_OBSERVATIONS = "https://api.stlouisfed.org/fred/series/observations"
YAHOO_CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}
RSS_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
JSON_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/json, text/plain, */*",
}
YAHOO_HEADERS = {
    **JSON_HEADERS,
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
}

MARKET_WATCH_ITEMS = [
    {"id": "dow_jones", "rank": 2, "label": "Dow Jones", "region": "US", "category": "US Market", "symbol": "^DJI", "impact": "direct"},
    {"id": "nasdaq", "rank": 3, "label": "Nasdaq", "region": "US", "category": "US Market", "symbol": "^IXIC", "impact": "direct"},
    {"id": "sp500", "rank": 4, "label": "S&P 500", "region": "US", "category": "US Market", "symbol": "^GSPC", "impact": "direct"},
    {"id": "dow_futures", "rank": 5, "label": "Dow Futures", "region": "US", "category": "US Futures", "symbol": "YM=F", "impact": "direct"},
    {"id": "nasdaq_futures", "rank": 6, "label": "Nasdaq Futures", "region": "US", "category": "US Futures", "symbol": "NQ=F", "impact": "direct"},
    {"id": "sp500_futures", "rank": 7, "label": "S&P 500 Futures", "region": "US", "category": "US Futures", "symbol": "ES=F", "impact": "direct"},
    {"id": "nikkei", "rank": 8, "label": "Nikkei 225", "region": "Japan", "category": "Asia", "symbol": "^N225", "impact": "direct"},
    {"id": "kospi", "rank": 9, "label": "KOSPI", "region": "South Korea", "category": "Asia", "symbol": "^KS11", "impact": "direct"},
    {"id": "hang_seng", "rank": 10, "label": "Hang Seng", "region": "Hong Kong", "category": "Asia", "symbol": "^HSI", "impact": "direct"},
    {"id": "shanghai", "rank": 11, "label": "Shanghai Composite", "region": "China", "category": "Asia", "symbol": "000001.SS", "impact": "direct"},
    {"id": "csi_300", "rank": 12, "label": "CSI 300", "region": "China", "category": "Asia", "symbol": "000300.SS", "impact": "direct"},
    {"id": "dax", "rank": 13, "label": "DAX", "region": "Europe", "category": "Europe", "symbol": "^GDAXI", "impact": "direct"},
    {"id": "ftse", "rank": 14, "label": "FTSE 100", "region": "Europe", "category": "Europe", "symbol": "^FTSE", "impact": "direct"},
    {"id": "cac", "rank": 15, "label": "CAC 40", "region": "Europe", "category": "Europe", "symbol": "^FCHI", "impact": "direct"},
    {"id": "dxy", "rank": 18, "label": "Dollar Index", "region": "US", "category": "Macro", "symbol": "DX-Y.NYB", "impact": "inverse"},
    {"id": "us10y", "rank": 19, "label": "US 10Y Yield", "region": "US", "category": "Macro", "symbol": "^TNX", "impact": "inverse"},
    {"id": "gold", "rank": 20, "label": "Gold", "region": "Global", "category": "Risk", "symbol": "GC=F", "impact": "mixed"},
    {"id": "bitcoin", "rank": 21, "label": "Bitcoin", "region": "Global", "category": "Risk", "symbol": "BTC-USD", "impact": "direct"},
    {"id": "cboe_vix", "rank": 22, "label": "CBOE VIX", "region": "US", "category": "Risk", "symbol": "^VIX", "impact": "inverse"},
]

POSITIVE_WORDS = {
    "gain", "gains", "growth", "strong", "surge", "surges", "beat", "beats",
    "eases", "easing", "cooling", "optimism", "optimistic", "rally", "rises",
    "rose", "positive", "supportive", "stable", "stability", "improves",
    "improved", "rebound", "rebounds", "bullish", "softens", "recovery",
}
NEGATIVE_WORDS = {
    "fall", "falls", "fell", "drop", "drops", "decline", "declines", "weak",
    "weakness", "war", "conflict", "tariff", "tariffs", "sanction", "sanctions",
    "inflation", "hawkish", "volatility", "selloff", "sell-off", "bearish",
    "risk", "risks", "shock", "slump", "slumps", "concern", "concerns",
    "recession", "fear", "fears", "spike", "spikes",
}


class LiveDataError(RuntimeError):
    """Raised when the world-signal bundle cannot be built from live sources."""


@dataclass
class Article:
    title: str
    url: str
    published_at: str
    text: str
    source: str


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


def fetch_text(url: str, headers: dict[str, str] | None = None, timeout: int = 10) -> str:
    request = urllib.request.Request(url, headers=headers or DEFAULT_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 10) -> Any:
    return json.loads(fetch_text(url, headers=headers, timeout=timeout))


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def compact_whitespace(text: str) -> str:
    return " ".join(text.split())


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


def gdelt_query(query: str, *, timespan: str, maxrecords: int) -> tuple[list[Article], dict[str, Any]]:
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": str(maxrecords),
        "timespan": timespan,
        "sort": "datedesc",
        "format": "jsonfeed",
    }
    url = GDELT_DOC_API + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url, headers=JSON_HEADERS, timeout=6)
    items = payload.get("items") or []

    articles: list[Article] = []
    for item in items:
        text = compact_whitespace(
            " ".join(
                part
                for part in [
                    item.get("title"),
                    item.get("content_text"),
                    item.get("summary"),
                ]
                if part
            )
        )
        articles.append(
            Article(
                title=compact_whitespace(item.get("title", "")),
                url=item.get("url", ""),
                published_at=item.get("date_published", ""),
                text=text,
                source="GDELT DOC 2.0 API",
            )
        )

    return articles, {"provider": "GDELT DOC 2.0 API", "query": query, "timespan": timespan, "records": len(articles), "url": url}


def newsapi_query(query: str) -> tuple[list[Article], dict[str, Any]] | None:
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return None

    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": "50",
        "from": (datetime.now(UTC) - timedelta(days=3)).date().isoformat(),
    }
    url = NEWSAPI_EVERYTHING + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url, headers={**JSON_HEADERS, "X-Api-Key": api_key}, timeout=8)
    items = payload.get("articles") or []

    articles: list[Article] = []
    for item in items:
        text = compact_whitespace(
            " ".join(
                part
                for part in [
                    item.get("title"),
                    item.get("description"),
                    item.get("content"),
                ]
                if part
            )
        )
        articles.append(
            Article(
                title=compact_whitespace(item.get("title", "")),
                url=item.get("url", ""),
                published_at=item.get("publishedAt", ""),
                text=text,
                source="NewsAPI",
            )
        )

    return articles, {"provider": "NewsAPI", "query": query, "records": len(articles), "url": url}


def google_news_rss_query(
    query: str,
    *,
    hl: str,
    gl: str,
    ceid: str,
) -> tuple[list[Article], dict[str, Any]]:
    params = {"q": query, "hl": hl, "gl": gl, "ceid": ceid}
    url = GOOGLE_NEWS_RSS + "?" + urllib.parse.urlencode(params)
    xml_text = fetch_text(url, headers=RSS_HEADERS, timeout=8)
    root = ET.fromstring(xml_text)

    articles: list[Article] = []
    for item in root.findall(".//item"):
        title = compact_whitespace(item.findtext("title", ""))
        link = compact_whitespace(item.findtext("link", ""))
        published_at = compact_whitespace(item.findtext("pubDate", ""))
        description = compact_whitespace(item.findtext("description", ""))
        text = compact_whitespace(" ".join(part for part in [title, description] if part))
        articles.append(
            Article(
                title=title,
                url=link,
                published_at=published_at,
                text=text,
                source="Google News RSS",
            )
        )

    return articles, {"provider": "Google News RSS", "query": query, "records": len(articles), "url": url}


def dedupe_articles(articles: list[Article]) -> list[Article]:
    deduped: list[Article] = []
    seen: set[str] = set()
    for article in articles:
        key = (article.url or article.title).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(article)
    return deduped


def take_headlines(articles: list[Article], limit: int = 5) -> list[dict[str, str]]:
    return [
        {
            "title": article.title,
            "url": article.url,
            "published_at": article.published_at,
            "source": article.source,
        }
        for article in articles[:limit]
    ]


def classify_impact(score: float, positive_at: float = 0.08, negative_at: float = -0.08) -> str:
    if score >= positive_at:
        return "positive"
    if score <= negative_at:
        return "negative"
    return "neutral"


def yahoo_latest_change(symbol: str) -> tuple[dict[str, Any], dict[str, Any]]:
    url = f"{YAHOO_CHART_API}/{urllib.parse.quote(symbol, safe='=^')}?range=1mo&interval=1d"
    payload = fetch_json(url, headers=YAHOO_HEADERS, timeout=8)
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise LiveDataError(f"Yahoo Finance returned no chart data for {symbol}")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []

    points: list[SeriesPoint] = []
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        points.append(
            SeriesPoint(
                date=datetime.fromtimestamp(timestamp, UTC).date().isoformat(),
                value=float(close),
            )
        )

    if len(points) < 2:
        raise LiveDataError(f"Yahoo Finance did not return 2 usable daily closes for {symbol}")

    previous = points[-2]
    latest = points[-1]
    change_pct = ((latest.value - previous.value) / previous.value) * 100.0
    return (
        {
            "provider": "Yahoo Finance",
            "symbol": symbol,
            "date": latest.date,
            "value": round(latest.value, 4),
            "previous_value": round(previous.value, 4),
            "change_pct": round(change_pct, 4),
            "history": [{"date": point.date, "value": round(point.value, 4)} for point in points[-20:]],
        },
        {
            "provider": "Yahoo Finance",
            "symbol": symbol,
            "records": len(points),
            "url": url,
        },
    )


def fred_latest_change(series_id: str, api_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "10",
    }
    url = FRED_SERIES_OBSERVATIONS + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url, headers=JSON_HEADERS, timeout=8)
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
        raise LiveDataError(f"FRED series {series_id} did not return 2 usable observations")

    latest, previous = points[0], points[1]
    change_pct = ((latest.value - previous.value) / previous.value) * 100.0
    return (
        {
            "provider": "FRED",
            "series_id": series_id,
            "date": latest.date,
            "value": round(latest.value, 4),
            "previous_value": round(previous.value, 4),
            "change_pct": round(change_pct, 4),
        },
        {
            "provider": "FRED",
            "series_id": series_id,
            "records": 2,
            "url": url,
        },
    )


def resolve_market_series(yahoo_symbol: str, fred_series_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    errors: list[str] = []

    try:
        return yahoo_latest_change(yahoo_symbol)
    except Exception as exc:
        errors.append(f"Yahoo Finance failed for {yahoo_symbol}: {exc}")

    fred_api_key = os.environ.get("FRED_API_KEY")
    if fred_series_id and fred_api_key:
        try:
            return fred_latest_change(fred_series_id, fred_api_key)
        except Exception as exc:
            errors.append(f"FRED failed for {fred_series_id}: {exc}")

    raise LiveDataError("; ".join(errors) or f"No live market source succeeded for {yahoo_symbol}")


def build_market_watch() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for item in MARKET_WATCH_ITEMS:
        try:
            series, meta = yahoo_latest_change(item["symbol"])
            rows.append(
                {
                    **item,
                    **series,
                    "status": "live",
                }
            )
            provenance.append(meta)
        except Exception as exc:
            rows.append(
                {
                    **item,
                    "status": "unavailable",
                    "error": str(exc),
                }
            )
    return rows, provenance


def collect_topic_articles(
    *,
    topic_name: str,
    gdelt_query_text: str,
    rss_query_text: str,
    rss_hl: str,
    rss_gl: str,
    rss_ceid: str,
    newsapi_query_text: str,
    timespan: str,
    maxrecords: int,
) -> dict[str, Any]:
    articles: list[Article] = []
    provenance: list[dict[str, Any]] = []
    failures: list[str] = []

    try:
        gdelt_articles, gdelt_meta = gdelt_query(gdelt_query_text, timespan=timespan, maxrecords=maxrecords)
        articles.extend(gdelt_articles)
        provenance.append(gdelt_meta)
    except Exception as exc:
        failures.append(f"GDELT failed for {topic_name}: {exc}")

    try:
        rss_articles, rss_meta = google_news_rss_query(
            rss_query_text,
            hl=rss_hl,
            gl=rss_gl,
            ceid=rss_ceid,
        )
        articles.extend(rss_articles)
        provenance.append(rss_meta)
    except Exception as exc:
        failures.append(f"Google News RSS failed for {topic_name}: {exc}")

    newsapi_result = None
    try:
        newsapi_result = newsapi_query(newsapi_query_text)
    except Exception as exc:
        failures.append(f"NewsAPI failed for {topic_name}: {exc}")
    if newsapi_result:
        newsapi_articles, newsapi_meta = newsapi_result
        articles.extend(newsapi_articles)
        provenance.append(newsapi_meta)

    if not provenance:
        raise LiveDataError(f"No live news provider succeeded for {topic_name}. " + " | ".join(failures))

    deduped = dedupe_articles(articles)
    return {
        "articles": deduped,
        "provenance": provenance,
        "failures": failures,
    }


def build_factor(name: str, score: float, article_count: int, detail: str) -> dict[str, str]:
    return {
        "factor": name,
        "impact": classify_impact(score),
        "detail": f"{detail} Article count: {article_count}. Sentiment score: {score:+.3f}.",
    }


def main() -> None:
    args = parse_args()

    india_bundle = collect_topic_articles(
        topic_name="india_market",
        gdelt_query_text='("india" OR "nifty" OR "sensex" OR "reserve bank of india" OR "rupee" OR "inflation")',
        rss_query_text="India stock market OR Nifty OR RBI OR rupee OR inflation",
        rss_hl="en-IN",
        rss_gl="IN",
        rss_ceid="IN:en",
        newsapi_query_text="India stock market OR Nifty OR RBI OR rupee OR inflation",
        timespan=args.timespan,
        maxrecords=args.maxrecords,
    )
    global_bundle = collect_topic_articles(
        topic_name="global_risk",
        gdelt_query_text='("war" OR "conflict" OR "tariff" OR "sanctions" OR "oil" OR "crude" OR "inflation" OR "fed")',
        rss_query_text="war OR tariff OR sanctions OR oil OR inflation OR Federal Reserve",
        rss_hl="en-US",
        rss_gl="US",
        rss_ceid="US:en",
        newsapi_query_text="war OR tariff OR sanctions OR crude oil OR inflation OR Federal Reserve",
        timespan=args.timespan,
        maxrecords=args.maxrecords,
    )

    india_articles = india_bundle["articles"]
    global_articles = global_bundle["articles"]

    india_score = score_texts([article.text for article in india_articles])
    global_score = score_texts([article.text for article in global_articles])

    us_markets, us_markets_meta = resolve_market_series("^GSPC", fred_series_id="SP500")
    brent, brent_meta = resolve_market_series("BZ=F", fred_series_id="DCOILBRENTEU")
    market_watch, market_watch_provenance = build_market_watch()

    output = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "data_mode": "real",
            "notes": [
                "All values in this file are derived from live source responses fetched during this run.",
                "No synthetic or fallback market values are included.",
            ],
        },
        "us_markets": us_markets,
        "brent": brent,
        "market_watch": market_watch,
        "news_sentiment": {
            "india": india_score,
            "global": global_score,
        },
        "geopolitical_factors": [
            build_factor(
                "Global risk coverage",
                global_score,
                len(global_articles),
                "Derived from live global-risk headline coverage.",
            ),
            build_factor(
                "Oil and commodity pressure",
                -clamp(brent["change_pct"] / 3.0, -1, 1),
                len(global_articles),
                "Derived from Brent crude change and overlapping global commodity coverage.",
            ),
        ],
        "domestic_factors": [
            build_factor(
                "India macro and market coverage",
                india_score,
                len(india_articles),
                "Derived from live India-focused headline coverage.",
            ),
            build_factor(
                "US equity spillover",
                clamp(us_markets["change_pct"] / 2.0, -1, 1),
                len(global_articles),
                "Derived from the latest US equity move.",
            ),
        ],
        "article_counts": {
            "india_market": len(india_articles),
            "global_risk": len(global_articles),
        },
        "headlines": {
            "india_market": take_headlines(india_articles),
            "global_risk": take_headlines(global_articles),
        },
        "news_sources": {
            "india_market": [entry["provider"] for entry in india_bundle["provenance"]],
            "global_risk": [entry["provider"] for entry in global_bundle["provenance"]],
        },
        "provider_failures": {
            "india_market": india_bundle["failures"],
            "global_risk": global_bundle["failures"],
        },
        "provenance": india_bundle["provenance"] + global_bundle["provenance"] + [us_markets_meta, brent_meta] + market_watch_provenance,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output_path)}, indent=2))


if __name__ == "__main__":
    main()
