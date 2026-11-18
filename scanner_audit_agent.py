#!/usr/bin/env python3
"""Read-only scanner evidence auditor; only its own reports may be written.

No scanner imports, market fetches, arbitrary SQL, model tools, or orders.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time as clock
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
VERSION = "scanner-audit-v1"
APP_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = Path(os.environ.get("SCANNER_AUDIT_OUTPUT_DIR", str(APP_ROOT.parent / "scanner_audit_reports")))
READ_SQL = """SELECT id, event_type, session_date, symbol, side,
CASE WHEN event_type IN ('scan_replay','trace_state')
THEN jsonb_build_object('snapshot_id',payload->>'snapshot_id') ELSE payload END AS payload, created_at
FROM public.scanner_event_log WHERE session_date = %s AND id <= %s ORDER BY id"""
CREATE_REPORTS_SQL = """CREATE TABLE IF NOT EXISTS public.scanner_audit_reports (
 report_id text PRIMARY KEY, session_date date NOT NULL,
 generated_at timestamptz NOT NULL, source_max_event_id bigint,
 audit_version text NOT NULL, report jsonb NOT NULL, markdown text NOT NULL
)"""
INSERT_REPORT_SQL = """INSERT INTO public.scanner_audit_reports
(report_id,session_date,generated_at,source_max_event_id,audit_version,report,markdown)
VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT (report_id) DO NOTHING"""


def timestamp(value):
    if isinstance(value, datetime):
        return value.astimezone(IST) if value.tzinfo else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(IST) if parsed.tzinfo else None
    except ValueError:
        return None


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def normalize(event):
    e = dict(event)
    if isinstance(e.get("payload"), str):
        e["payload"] = json.loads(e["payload"])
    if not isinstance(e.get("payload"), dict):
        e["payload"] = {}
    return e


def evidence(e):
    return {"event_id": e.get("id"), "snapshot_id": e["payload"].get("snapshot_id")}


def build_report(events, day, now=None):
    """Pure analysis. Times are observations, never inferred exact market/latency times."""
    date.fromisoformat(day)
    now = now or datetime.now(IST)
    now = timestamp(now)
    if now is None:
        raise ValueError("now must have a timezone")
    events = [normalize(e) for e in events if str(e.get("session_date")) == day]
    counts = Counter(e.get("event_type") for e in events)
    opening = datetime.combine(date.fromisoformat(day), time(9, 15), IST)
    closing = datetime.combine(date.fromisoformat(day), time(15, 30), IST)
    candidates = defaultdict(list)
    snapshots = set()
    decision_snapshots = set()
    rank_lookup = {}
    provenance_failures, health_failures, browser_failures = [], [], []
    starts, completed = [], []
    client_market = []
    premarket = 0
    rejections = Counter()
    source_commits = set()
    for e in events:
        p, kind = e["payload"], e.get("event_type")
        t = timestamp(p.get("observed_at_ist"))
        if kind == "scan_replay" and p.get("snapshot_id"):
            snapshots.add(p["snapshot_id"])
        if kind == "ranking_decision":
            if p.get("snapshot_id"):
                decision_snapshots.add(p["snapshot_id"])
            if p.get("source_commit"):
                source_commits.add(p["source_commit"])
            if p.get("signal_id") and p.get("snapshot_id"):
                rank_lookup[(p["snapshot_id"], p["signal_id"], e.get("symbol"), e.get("side"))] = e
            if t and t < opening:
                premarket += 1
            if t and opening <= t <= closing and e.get("symbol") and e.get("side") in ("bullish", "bearish"):
                candidates[(e["symbol"], e["side"])].append(e)
                if p.get("qualified") is False:
                    rejections[str(p.get("reason") or "unknown")] += 1
        if kind == "data_provenance" and p.get("status") not in ("success", "cache_hit", "shared_scan_input"):
            provenance_failures.append(evidence(e) | {"status": p.get("status")})
        if kind == "scanner_health":
            if "failed" in str(p.get("kind", "")):
                health_failures.append(evidence(e) | {"kind": p.get("kind"), "error_type": p.get("error_type")})
            saved = timestamp(e.get("created_at")) or timestamp(p.get("recorded_at_ist"))
            if p.get("kind") == "scheduled_scan_started" and saved:
                starts.append(saved)
            if p.get("kind") == "scan_completed":
                t = timestamp(p.get("completed_at_ist"))
                if t:
                    completed.append(t)
        if kind == "client_trace":
            t = timestamp(p.get("received_at_ist")) or timestamp(e.get("created_at"))
            if t and opening <= t <= closing:
                client_market.append(e)
            if p.get("kind") in ("load_failed", "render_failed"):
                browser_failures.append(evidence(e) | {"kind": p.get("kind"), "reason": p.get("reason"),
                                                       "transport": p.get("transport")})

    receipts = [e for e in events if e.get("event_type") in ("signal_displayed", "display_rank_observed")
                and e["payload"].get("panel") == "ranking" and e["payload"].get("in_viewport") is True
                and e["payload"].get("page_visible") is True]
    matching, mismatching, unmatched = [], [], []
    for e in receipts:
        p = e["payload"]
        r = rank_lookup.get((p.get("snapshot_id"), p.get("signal_id"), e.get("symbol"), e.get("side")))
        if r is None:
            unmatched.append(evidence(e))
        elif number(p.get("display_rank")) != number(r["payload"].get("rank")):
            mismatching.append(evidence(e))
        else:
            matching.append(evidence(e))

    rows = []
    for (symbol, side), history in sorted(candidates.items()):
        history.sort(key=lambda e: timestamp(e["payload"].get("observed_at_ist")))
        qualified = [e for e in history if e["payload"].get("qualified") is True]
        top5 = [e for e in qualified if number(e["payload"].get("rank")) is not None
                and 1 <= number(e["payload"]["rank"]) <= 5]
        if not qualified:
            continue
        first = qualified[0]
        ft = timestamp(first["payload"]["observed_at_ist"])
        tt = timestamp(top5[0]["payload"]["observed_at_ist"]) if top5 else None
        seen = [e for e in receipts if e.get("symbol") == symbol and e.get("side") == side
                and timestamp(e["payload"].get("rendered_at_client"))
                and opening <= timestamp(e["payload"]["rendered_at_client"]) <= closing]
        seen.sort(key=lambda e: timestamp(e["payload"]["rendered_at_client"]))
        prices = []
        # Fixed horizons anchored to first qualification and first Top-5 observation.
        # No interpolation, no future endpoint chosen for best return, no option P&L claims.
        for anchor_name, anchor in (("qualification", first), ("top5", top5[0] if top5 else None)):
            if not anchor:
                continue
            origin = timestamp(anchor["payload"]["observed_at_ist"])
            reference = number(anchor["payload"].get("ltp"))
            for minutes in (15, 30, 60):
                target = origin + timedelta(minutes=minutes)
                # Consider rejected snapshots too: rejecting later does not censor a loss.
                window = [e for e in history if target <= timestamp(e["payload"]["observed_at_ist"])
                          <= target + timedelta(minutes=6) and number(e["payload"].get("ltp")) is not None]
                item = {"anchor": anchor_name, "minutes": minutes, "intended_at_ist": target.isoformat(),
                        "status": "unknown", "reference_price": reference, "anchor_evidence": evidence(anchor)}
                if target > closing:
                    item["status"] = "horizon_after_close"
                elif reference and reference > 0 and window:
                    observation = window[0]
                    value = number(observation["payload"]["ltp"])
                    item.update(status="observed_quote", observed_at_ist=observation["payload"]["observed_at_ist"],
                                directional_return_pct=round((1 if side == "bullish" else -1)*(value/reference-1)*100, 4),
                                evidence=evidence(observation))
                elif now < target:
                    item["status"] = "pending"
                prices.append(item)
        rows.append({"symbol": symbol, "side": side, "first_qualified_ist": ft.isoformat(),
                     "first_qualified_rank": first["payload"].get("rank"), "qualification_evidence": evidence(first),
                     "first_top5_ist": tt.isoformat() if tt else None,
                     "top5_evidence": evidence(top5[0]) if top5 else None,
                     "qualification_to_top5_minutes": round((tt-ft).total_seconds()/60, 2) if tt else None,
                     "first_visible_client_ist": timestamp(seen[0]["payload"]["rendered_at_client"]).isoformat() if seen else None,
                     "display_evidence": evidence(seen[0]) if seen else None,
                     "latest_observed_rank": history[-1]["payload"].get("rank"),
                     "forward_observations": prices})

    # Preserve per-wave outcome semantics; do not collapse different setups/waves into a winner.
    waves = {}
    for e in events:
        p = e["payload"]
        if e.get("event_type") == "wave_outcome" and p.get("wave_id"):
            origin = timestamp(p.get("origin_at_ist"))
            if origin and opening <= origin <= closing:
                key = (p.get("signal_id"), p["wave_id"])
                prior = waves.get(key)
                if not prior or (timestamp(p.get("observed_at_ist")) or opening) >= (timestamp(prior["payload"].get("observed_at_ist")) or opening):
                    waves[key] = e
    outcomes = [{"symbol": e.get("symbol"), "side": e.get("side"), "evidence": evidence(e),
                 **{k: e["payload"].get(k) for k in ("signal_id", "wave_id", "origin_at_ist", "coverage",
                   "mfe_pct_observed", "mae_pct_observed", "first_touch_observed", "completed_bar_count")}}
                for e in waves.values()]
    market_render_count = sum(e["payload"].get("kind") == "snapshot_rendered" for e in client_market)
    missing = ["Option-contract quotes, actual fills, fees and slippage are not ingested; no option/trade profit inference.",
               "Quote observation is not verified exchange quote time; stale source data can affect outcomes.",
               "Client clocks are unsynchronized; visibility is viewport evidence, not proof of attention or exact latency.",
               "Missing candidates are unobserved, not inferred rejected. Fixed-horizon outcomes may be incomplete.",
               "This is one-session evidence, not validated profitability or causal proof."]
    if not receipts:
        missing.append("No visible ranking receipts: stock-level screen delivery cannot be verified.")
    if not market_render_count:
        missing.append("No market-hours snapshot-render acknowledgements: delivery/reliability is unknown.")
    findings = [{"code": "display_coverage", "severity": "warning" if not market_render_count else "info",
                 "detail": f"{market_render_count} market-hours render acknowledgements; {len(receipts)} visible ranking receipts."},
                {"code": "ranking_gap", "severity": "info",
                 "detail": "Qualification-to-Top-5 gaps measure ranking prominence, not network delay."},
                {"code": "handler_failures", "severity": "warning" if browser_failures else "info",
                 "detail": f"{len(browser_failures)} browser failure events. WebSocket catches may cover chart/index/scanner messages."}]
    forward_summary = []
    for anchor in ("qualification", "top5"):
        for horizon in (15, 30, 60):
            items = [p for r in rows for p in r["forward_observations"] if p["anchor"] == anchor and p["minutes"] == horizon]
            measured = [p["directional_return_pct"] for p in items if p["status"] == "observed_quote"]
            forward_summary.append({"anchor": anchor, "minutes": horizon, "candidate_count": len(items),
                                    "status_counts": dict(Counter(p["status"] for p in items)),
                                    "positive_observations": sum(v > 0 for v in measured),
                                    "negative_observations": sum(v < 0 for v in measured),
                                    "mean_directional_quote_return_pct": round(sum(measured)/len(measured), 4) if measured else None})
    report = {"audit_version": VERSION, "session_date": day, "generated_at_ist": now.isoformat(),
              "session_status": "partial" if now < closing else "closed_session_observations",
              "source": {"event_count": len(events), "max_event_id": max((int(e["id"]) for e in events if e.get("id") is not None), default=None),
                         "commits": sorted(source_commits)},
              "coverage": {"event_counts": dict(counts), "replay_snapshots": len(snapshots),
                           "decision_snapshots": len(decision_snapshots), "decisions_without_replay": sorted(decision_snapshots-snapshots),
                           "premarket_decisions_excluded": premarket, "market_render_acknowledgements": market_render_count,
                           "intraday_observed_symbol_sides": len(candidates), "rejection_observation_counts": dict(rejections),
                           "ranking_receipts": len(receipts), "matching_rank_receipts": len(matching),
                           "mismatching_rank_receipts": mismatching, "unmatched_receipts": unmatched,
                           "last_scan_completed_ist": max(completed).isoformat() if completed else None,
                           "last_scheduled_scan_start_ist": max(starts).isoformat() if starts else None},
              "failures": {"health": health_failures, "source": provenance_failures, "browser": browser_failures},
              "candidates": rows, "forward_summary": forward_summary, "wave_observations": outcomes, "findings": findings,
              "limitations": missing, "ai": {"status": "disabled"}}
    digest = hashlib.sha256(json.dumps(events, sort_keys=True, default=str).encode()).hexdigest()
    report["source"]["input_sha256"] = digest
    report["report_id"] = f"{day}-{VERSION}-{now.strftime('%Y%m%dT%H%M%S%f')}-{digest[:12]}"
    return report


def read_database(day, url):
    """Hard-coded, bounded SELECTs in an enforced read-only consistent transaction."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    with closing(psycopg2.connect(url, connect_timeout=5, options="-c statement_timeout=30000")) as conn:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT max(id) AS cutoff FROM public.scanner_event_log WHERE session_date = %s", (day,))
            cutoff = cur.fetchone()["cutoff"]
            if cutoff is None:
                return []
            cur.execute(READ_SQL, (day, cutoff))
            return [dict(e) for e in cur.fetchall()]


def markdown(report):
    lines = [f"# Scanner audit — {report['session_date']}", "", f"Generated: {report['generated_at_ist']}",
             "", "Evidence audit only. No ranking changes, trading advice, or executed-profit claims.", "",
             "## Coverage", "", "```json", json.dumps(report["coverage"], indent=2), "```", "",
             "## Findings", ""]
    lines += [f"- {f['code']}: {f['detail']}" for f in report["findings"]]
    lines += ["", "## Candidates (all observed qualified candidates, not only winners)", "",
              "| Stock | Side | First qualified IST | Rank then | First Top-5 IST | First visible client IST |",
              "|---|---|---|---|---|---|"]
    for r in report["candidates"]:
        lines.append("| " + " | ".join(str(r.get(k) or "unknown").replace("|", "/").replace("\n", " ") for k in
                     ("symbol", "side", "first_qualified_ist", "first_qualified_rank", "first_top5_ist", "first_visible_client_ist")) + " |")
    lines += ["", "## Failures", "", "```json", json.dumps(report["failures"], indent=2), "```", "",
              "## Forward quote observations (not trade profits)", "", "```json",
              json.dumps(report["forward_summary"], indent=2), "```", "",
              "## Missing evidence and limitations", ""]
    lines += ["- " + s for s in report["limitations"]]
    lines += ["", "Fixed-horizon directional quote returns and per-wave OHLC evidence are in the companion JSON.", "",
              "## Optional AI hypotheses (unverified; must be replay-tested)", "", "```json",
              json.dumps(report["ai"], indent=2), "```", ""]
    return "\n".join(lines)


def save_reports(report, output):
    output = Path(output).resolve()
    if output.is_relative_to(APP_ROOT):
        raise ValueError("Reports must be stored outside the public application directory")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = []
    for suffix, content in (("json", json.dumps(report, indent=2, default=str)), ("md", markdown(report))):
        path = output / f"{report['report_id']}.{suffix}"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        paths.append(str(path))
    return paths


def persist_report(report, url, initialize=False):
    """Separate writer credential; only fixed report-table CREATE/INSERT statements."""
    import psycopg2
    with closing(psycopg2.connect(url, connect_timeout=5, options="-c statement_timeout=10000")) as conn:
        with conn:
            with conn.cursor() as cur:
                if initialize:
                    cur.execute(CREATE_REPORTS_SQL)
                cur.execute(INSERT_REPORT_SQL, (report["report_id"], report["session_date"], report["generated_at_ist"],
                            report["source"]["max_event_id"], VERSION, json.dumps(report, default=str), markdown(report)))


def run(args):
    day = args.date or datetime.now(IST).date().isoformat()
    date.fromisoformat(day)
    if args.events:
        path = Path(args.events)
        raw = path.read_text(encoding="utf-8")
        events = json.loads(raw) if raw.lstrip().startswith("[") else [json.loads(s) for s in raw.splitlines() if s.strip()]
    else:
        url = os.environ.get("SCANNER_AUDIT_READ_DATABASE_URL")
        if not url:
            raise ValueError("Set SCANNER_AUDIT_READ_DATABASE_URL or supply --events JSON/JSONL")
        events = read_database(day, url)
    report = build_report(events, day)
    if args.ai:
        from scanner_audit_ai import explain
        report["ai"] = explain(report)
    paths = save_reports(report, args.output)
    mirror_status = "not_requested"
    if args.persist_db:
        url = os.environ.get("SCANNER_AUDIT_REPORT_DATABASE_URL")
        if not url:
            raise ValueError(f"Local reports saved; set SCANNER_AUDIT_REPORT_DATABASE_URL to persist: {paths}")
        persist_report(report, url, initialize=args.init_reports_db)
        mirror_status = "saved"
    print(json.dumps({"report_id": report["report_id"], "files": paths, "database": mirror_status,
                      "ai_status": report["ai"]["status"]}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="IST session date YYYY-MM-DD; default today")
    parser.add_argument("--events", help="Local JSON array or telemetry JSONL (no database needed)")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--persist-db", action="store_true", help="Write only dedicated audit report table")
    parser.add_argument("--init-reports-db", action="store_true", help="Explicitly create report table if needed")
    parser.add_argument("--ai", action="store_true", help="Opt-in: transmit allowlisted aggregate summary to OpenAI")
    parser.add_argument("--watch", action="store_true", help="Standalone backend worker; once per weekday after 16:30 IST")
    args = parser.parse_args()
    if args.init_reports_db and not args.persist_db:
        parser.error("--init-reports-db requires --persist-db")
    if args.watch and (args.date or args.events):
        parser.error("--watch cannot use --date/--events")
    if not args.watch:
        run(args)
        return
    last_day = None
    while True:
        now = datetime.now(IST)
        day = now.date().isoformat()
        if now.weekday() < 5 and now.time() >= time(16, 30) and day != last_day:
            try:
                run(args)
                last_day = day
            except Exception as exc:
                # No secret-bearing URLs, database exception text, or raw API errors.
                print(f"[scanner-audit] report failed: {type(exc).__name__}", file=sys.stderr)
                clock.sleep(300)
        clock.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[scanner-audit] failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
