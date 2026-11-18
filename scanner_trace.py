"""Evidence-only tracing. Never supplies inputs to the live scoring pipeline."""
from __future__ import annotations

import hashlib
import json
import math
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import scanner_telemetry as log

_lock = threading.Lock()
_capture = None
_health_queue = queue.Queue(maxsize=1000)
_health_worker = None


def begin(scan_id, started):
    global _capture
    with _lock:
        _capture = {"scan_id": scan_id, "started_at_ist": started, "sources": []}


def source(feed, *, symbol=None, started=None, status="success", **details):
    """No headers, credentials, response bodies, or arbitrary exception text."""
    with _lock:
        if _capture is None:
            return
        _capture["sources"].append({"feed": feed, "symbol": symbol, "status": status,
                                   "received_at_ist": datetime.now(log.IST).isoformat(),
                                   "duration_seconds": round(time.monotonic()-started, 3) if started else None,
                                   **details})


def endpoint_name(url):
    parsed = urlsplit(url)
    return parsed.netloc + parsed.path  # deliberately excludes query strings


def supplied_timestamps(value):
    """Keep supplied feed clock fields with paths, without guessing timezone/meaning."""
    found = {}
    names = {"timestamp", "lastupdatetime", "lastupdatedtime", "lastupdate", "lastupdated"}
    def visit(node, path, depth):
        if depth > 4 or len(found) >= 200:
            return
        if isinstance(node, dict):
            for key, item in node.items():
                child = path+"."+str(key)
                if str(key).lower() in names and isinstance(item, (str, int, float)):
                    found[child] = item
                elif isinstance(item, (dict, list)):
                    visit(item, child, depth+1)
        elif isinstance(node, list):
            for index, item in enumerate(node[:100]):
                visit(item, path+f"[{index}]", depth+1)
    visit(value, "$", 0)
    return found


def safe_source(*args, **kwargs):
    try:
        source(*args, **kwargs)
    except Exception:
        pass


def finish_capture():
    global _capture
    with _lock:
        captured = _capture
        _capture = None
    return captured


def health(kind, **details):
    """Best effort operational event; no market requests and no raw exceptions."""
    try:
        now = datetime.now(log.IST)
        log.write_events([log.event("scanner_health", now.date().isoformat(), {},
                                   {"kind": kind, "recorded_at_ist": now.isoformat(), **details})])
    except Exception:
        pass


def submit_health(kind, **details):
    """Keep telemetry network I/O off HTTP and websocket delivery paths."""
    global _health_worker
    with _lock:
        if _health_worker is None:
            def work():
                while True:
                    entry = _health_queue.get()
                    try:
                        health(entry[0], **entry[1])
                    finally:
                        _health_queue.task_done()
            _health_worker = threading.Thread(target=work, daemon=True, name="ScannerTraceHealth")
            _health_worker.start()
    try:
        _health_queue.put_nowait((kind, details))
    except queue.Full:
        # Explicit stderr evidence, not a false claim of complete telemetry.
        import sys
        print("[scanner-trace] health queue full; event not persisted", file=sys.stderr)


def _recover(day):
    conn = cursor = None
    try:
        import db_adapter
        if not db_adapter.DATABASE_URL:
            return None
        import psycopg2
        conn = psycopg2.connect(db_adapter.DATABASE_URL, connect_timeout=3,
                                options="-c statement_timeout=3000")
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM scanner_event_log WHERE event_type='trace_state' "
                       "AND session_date=%s ORDER BY id DESC LIMIT 1", (day,))
        row = cursor.fetchone()
        return json.loads(row[0]) if row and isinstance(row[0], str) else row[0] if row else None
    except Exception:
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def replay_inputs(scanner, gainers, losers, oi, indexes, candles, sources, extra_symbols):
    # Capture consumed normalized inputs and all numeric/boolean effective constants.
    # No module globals containing credentials, paths or arbitrary environment values.
    config = {k: v for k, v in vars(scanner).items()
              if k.isupper() and isinstance(v, (int, float, bool))}
    hashes = {}
    for name in ("stock_suggestion_index.py", "scanner_telemetry.py", "scanner_trace.py"):
        path = Path(__file__).with_name(name)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"gainers": gainers, "losers": losers, "oi_by_symbol": oi,
            "index_context": indexes, "candles_by_symbol": candles,
            "lot_sizes": scanner.load_fo_lot_sizes(), "effective_numeric_config": config,
            "index_like_symbols": sorted(scanner.INDEX_LIKE_SYMBOLS), "source_hashes": hashes,
            "sources": sources, "extra_symbols_requested": extra_symbols,
            "input_scope": "exact normalized inputs consumed; not raw HTTP responses",
            "quote_market_timestamp": None,
            "freshness_note": "fetch time is not upstream quote time; candle timestamps are bar starts"}


def trace_events(payload, decisions, scan_id, observed, replay=None):
    day = payload["session_date"]
    now = datetime.fromisoformat(observed)
    path = log.ROOT / f"trace-state-{day}.json"
    state = log._load(path, None)
    if state is None:
        state = _recover(day)
    continuity = "restored" if state else "unknown_before_first_record"
    state = state or {"candidates": {}}
    candidates = state.setdefault("candidates", {})
    events = []
    if replay is not None:
        events.append(log.event("scan_replay", day, {}, {
            "snapshot_id": scan_id, "captured_at_ist": observed, "input_hash": _digest(replay),
            "inputs": replay, "replay_note": "evaluate future formulas offline; do not call live feeds"}))
        for feed in (replay.get("sources") or {}).get("sources", []):
            events.append(log.event("data_provenance", day, {"symbol": feed.get("symbol")},
                                    {"snapshot_id": scan_id, **feed}))
    for row in decisions:
        sid = row["signal_id"]
        previous = candidates.get(sid)
        qualified = bool(row.get("qualified"))
        confirmed = qualified and bool(row.get("is_breakout"))
        wave_changed = confirmed and (not previous or not previous.get("confirmed") or
                                     previous.get("breakout_time") != row.get("breakout_time"))
        current = dict(previous or {})
        current.setdefault("first_observed_at_ist", observed)
        if qualified:
            current.setdefault("first_qualified_at_ist", observed)
        if wave_changed:
            current.update(wave_id="wave_"+_digest([sid, scan_id, row.get("breakout_time")])[:24],
                           wave_started_at_ist=observed, wave_reference_price=row.get("ltp"),
                           wave_stop=row.get("stop_loss"), wave_target=row.get("target"),
                           path_points=[], candle_evidence={})
        current.update(signal_id=sid, symbol=row["symbol"], side=row["side"], qualified=qualified,
                       confirmed=confirmed, rank=row.get("rank"), reason=row.get("reason"),
                       percent_change=row.get("percent_change"), score=row.get("score"),
                       breakout_time=row.get("breakout_time"), last_observed_at_ist=observed,
                       last_snapshot_id=scan_id)
        fields = ("qualified", "confirmed", "reason", "breakout_time")
        if previous is None or wave_changed or any(previous.get(k) != current.get(k) for k in fields):
            events.append(log.event("signal_lifecycle", day, row, {
                **current, "snapshot_id": scan_id, "continuity": continuity,
                "transition": "first_observed" if previous is None else
                              "new_confirmed_wave" if wave_changed else
                              "eligibility_gained" if qualified and not previous.get("qualified") else
                              "eligibility_lost" if not qualified and previous.get("qualified") else "state_changed",
                "previous_state": {k: previous.get(k) for k in fields} if previous else None,
                "previous_wave_id": previous.get("wave_id") if previous else None,
                "note": "eligibility loss is not necessarily trade invalidation; no absence-based inference"}))
        if previous is None or previous.get("rank") != current["rank"]:
            events.append(log.event("rank_transition", day, row, {
                "signal_id": sid, "wave_id": current.get("wave_id"), "snapshot_id": scan_id,
                "observed_at_ist": observed, "previous_rank": previous.get("rank") if previous else None,
                "rank": current["rank"], "previous_percent_change": previous.get("percent_change") if previous else None,
                "percent_change": current["percent_change"], "score": current["score"],
                "entered_top5": current["rank"] is not None and current["rank"] <= 5 and
                                (not previous or previous.get("rank") is None or previous["rank"] > 5),
                "exited_top5": bool(previous and previous.get("rank") is not None and previous["rank"] <= 5 and
                                    (current["rank"] is None or current["rank"] > 5)),
                "ranking_basis": "daily_percent_change", "continuity": continuity}))
        row["wave_id"] = current.get("wave_id")
        row["first_qualified_at_ist"] = current.get("first_qualified_at_ist")
        candidates[sid] = current
    # Missing candidates are unobserved, not rejected. Track existing waves using
    # only candles already fetched this scan, including symbols outside payload limits.
    candles = (replay or {}).get("candles_by_symbol", {})
    for sid, current in candidates.items():
        if not current.get("wave_id"):
            continue
        evidence = current.setdefault("candle_evidence", {})
        origin = datetime.fromisoformat(current["wave_started_at_ist"])
        for bar in candles.get(current["symbol"], []):
            ts = bar.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            start = datetime.fromtimestamp(ts, log.IST)
            if start >= origin and start+timedelta(minutes=5) <= now and start.date().isoformat() == day:
                evidence[str(ts)] = bar
        ordered = sorted(evidence.values(), key=lambda b: b["timestamp"])
        reference = current.get("wave_reference_price")
        if ordered and isinstance(reference, (int, float)) and reference > 0:
            bull = current["side"] == "bullish"
            favourable = max((b["high"]/reference-1)*100 if bull else (1-b["low"]/reference)*100 for b in ordered)
            adverse = max((1-b["low"]/reference)*100 if bull else (b["high"]/reference-1)*100 for b in ordered)
            stop, target = current.get("wave_stop"), current.get("wave_target")
            touches = []
            for bar in ordered:
                stop_hit = stop is not None and (bar["low"] <= stop if bull else bar["high"] >= stop)
                target_hit = target is not None and (bar["high"] >= target if bull else bar["low"] <= target)
                if stop_hit or target_hit:
                    touches.append({"bar_start_epoch": bar["timestamp"], "stop": stop_hit, "target": target_hit,
                                    "order": "ambiguous_same_bar" if stop_hit and target_hit else "stop" if stop_hit else "target"})
            expected = math.floor((min(now.timestamp(), datetime.fromisoformat(day+"T15:30:00+05:30").timestamp())-
                                   math.ceil(origin.timestamp()/300)*300)/300)
            outcome = {"signal_id": sid, "wave_id": current["wave_id"], "snapshot_id": scan_id,
                       "origin_at_ist": current["wave_started_at_ist"], "reference_price": reference,
                       "observed_at_ist": observed, "completed_bar_count": len(ordered),
                       "coverage": "complete_completed_bars" if len(ordered) == max(0, expected) else "partial",
                       "mfe_pct_observed": round(max(0, favourable), 4), "mae_pct_observed": round(max(0, adverse), 4),
                       "first_touch_observed": touches[0] if touches else None,
                       "stop": stop, "target": target,
                       "note": "observed OHLC bounds, not executed trade profit; same-bar order unknown; origin-straddling candle excluded"}
            if _digest(outcome.get("first_touch_observed")) != current.get("last_touch_hash") or len(ordered) != current.get("last_bar_count"):
                events.append(log.event("wave_outcome", day, current, outcome))
            current["last_touch_hash"] = _digest(outcome.get("first_touch_observed"))
            current["last_bar_count"] = len(ordered)
    state.update(snapshot_id=scan_id, recorded_at_ist=observed)
    log._atomic_json(path, state)
    events.append(log.event("trace_state", day, {}, state))
    events.append(log.event("scanner_health", day, {}, {
        "kind": "scan_completed", "snapshot_id": scan_id, "started_at_ist": payload.get("generated_at_ist"),
        "completed_at_ist": observed, "candidate_count": len(decisions),
        "unobserved_previous_candidate_count": len(set(candidates)-{r["signal_id"] for r in decisions}),
        "scan_interval_note": "scheduler cadence is logged separately; absent rows are not inferred rejected"}))
    return events
