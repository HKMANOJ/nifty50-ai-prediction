"""Dated, correlated scanner decisions and browser visibility receipts.

Local JSONL is the durable first copy; Neon mirroring is best effort. No
scoring, filtering or trading behavior is changed by these helpers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent / "inputs" / "scanner_telemetry"
VERSION = "scanner-observability-v1"
_RECEIPT_LOCK = threading.Lock()


def signal_id(session_date, symbol, side, setup=None):
    # Candidate identity, not a promise of one unique trade/wave per day.
    value = json.dumps([session_date, symbol, side, setup or "unknown"], separators=(",", ":"))
    return "sig_" + hashlib.sha256(value.encode()).hexdigest()[:24]


def new_scan():
    return "scan_" + uuid.uuid4().hex


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, default=str), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_events(events):
    """One local append and one PostgreSQL batch per call, never one connection per row."""
    if not events:
        return
    for event in events:
        event["payload"].setdefault("telemetry_version", VERSION)
    ROOT.mkdir(parents=True, exist_ok=True)
    for day in sorted({e["session_date"] for e in events}):
        with (ROOT / f"{day}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(e, default=str) + "\n" for e in events if e["session_date"] == day))
    conn = cursor = None
    try:
        import db_adapter
        if not db_adapter.DATABASE_URL:
            return
        import psycopg2
        # Do not reuse db_adapter's process-global connection in HTTP threads.
        conn = psycopg2.connect(db_adapter.DATABASE_URL, connect_timeout=3,
                                options="-c statement_timeout=3000")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO scanner_event_log (event_type, session_date, symbol, side, payload) "
            "SELECT event_type, session_date::date, symbol, side, payload "
            "FROM jsonb_to_recordset(%s::jsonb) AS e("
            "event_type text, session_date text, symbol text, side text, payload jsonb)",
            (json.dumps(events, default=str),),
        )
        conn.commit()
    except Exception as exc:
        print(f"[scanner-telemetry] mirror skipped; local events retained: {exc}", file=sys.stderr)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def event(kind, day, row, details):
    return {"event_type": kind, "session_date": day, "symbol": row.get("symbol"),
            "side": row.get("side"), "payload": details}


def _restore_candidate_outcomes(day):
    """Recover latest checkpoint state after an ephemeral container replacement."""
    conn = cursor = None
    try:
        import db_adapter
        if not db_adapter.DATABASE_URL:
            return {}
        import psycopg2
        conn = psycopg2.connect(db_adapter.DATABASE_URL, connect_timeout=3,
                                options="-c statement_timeout=3000")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT ON (payload->>'signal_id') payload FROM scanner_event_log "
            "WHERE event_type = 'candidate_outcome' AND session_date = %s "
            "ORDER BY payload->>'signal_id', id DESC", (day,))
        result = {}
        for (value,) in cursor.fetchall():
            item = json.loads(value) if isinstance(value, str) else value
            if item.get("signal_id") and item.get("checkpoints"):
                result[item["signal_id"]] = item
        return result
    except Exception as exc:
        print(f"[scanner-telemetry] pending recovery unavailable: {exc}", file=sys.stderr)
        return {}
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def prepare_snapshot(payload, decisions, scan_id):
    """Attach IDs before publication; preserve business values and include rejected candidates."""
    day = payload["session_date"]
    published = datetime.now(IST).isoformat(timespec="milliseconds")
    payload["snapshot_id"] = scan_id
    payload["prepared_at_ist"] = published
    payload["telemetry_version"] = VERSION
    issued = {}
    events = []
    for decision in decisions:
        row = dict(decision)
        row["signal_id"] = signal_id(day, row["symbol"], row["side"], row.get("setup"))
        row.update(snapshot_id=scan_id, observed_at_ist=published,
                   scanner_config=payload.get("scanner_config"),
                   source_commit=os.environ.get("SOURCE_COMMIT") or os.environ.get("RENDER_GIT_COMMIT"),
                   priority_confirmed=bool(row.get("status") == "Priority Rally" and row.get("bell_rang")),
                   qualified_at_ist=published if row.get("qualified") else None,
                   scan_started_at_ist=payload.get("generated_at_ist"),
                   qualification_state="qualified" if row.get("qualified") else "rejected",
                   market_data_observed_at_ist=None,
                   data_timestamp_note="fetch/scan time; upstream quote freshness not established")
        events.append(event("scanner_decision", day, row, row))
        rank_details = {**row, "ranking_completed_at_ist": published,
                        "ranking_basis": "percent_change_desc_bullish_asc_bearish",
                        "server_top6": row.get("rank") is not None and row["rank"] <= 6,
                        "included_in_payload": bool(row.get("included_in_payload")),
                        "exclusion_reason": row.get("reason") if not row.get("included_in_payload") else None,
                        "display_note": "server eligibility only; browser receipt confirms visibility"}
        events.append(event("ranking_decision", day, row, rank_details))
    for side in ("bullish", "bearish"):
        for row in payload.get(side, []) or []:
            row["signal_id"] = signal_id(day, row["symbol"], side, row.get("setup"))
            row["snapshot_id"] = scan_id
            issued[row["signal_id"]] = {"symbol": row["symbol"], "side": side}
    for row in payload.get("instant_triggers", []) or []:
        row["signal_id"] = signal_id(day, row["symbol"], row["side"], row.get("setup"))
        row["snapshot_id"] = scan_id
        issued[row["signal_id"]] = {"symbol": row["symbol"], "side": row["side"]}
    _atomic_json(ROOT / f"issued-{day}-{scan_id}.json",
                 {"signals": issued, "prepared_at_ist": published})
    events.extend(candidate_outcomes(payload, decisions, scan_id, published))
    write_events(events)


def candidate_outcomes(payload, decisions, scan_id, observed):
    """Track first observed candidates, including rejects, without extra market requests.

    Each dated horizon is pending, measured, missed_window or unavailable.
    A late quote never silently substitutes for an earlier checkpoint.
    """
    day = payload["session_date"]
    now = datetime.fromisoformat(observed)
    prior_events = []
    for old_path in ROOT.glob("candidate-outcomes-????-??-??.json"):
        old_day = old_path.stem.removeprefix("candidate-outcomes-")
        marker = ROOT / f"finalized-{old_day}.json"
        if old_day >= day or marker.exists():
            continue
        old_state = _load(old_path, {})
        for item in old_state.values():
            changed_old = False
            for checkpoint in item["checkpoints"].values():
                if checkpoint["status"] == "pending":
                    checkpoint.update(status="unavailable", reason="session_ended_without_observation",
                                      assessed_at_ist=observed)
                    changed_old = True
            if changed_old:
                prior_events.append(event("candidate_outcome", old_day, item, item))
        _atomic_json(old_path, old_state)
        _atomic_json(marker, {"finalized_at_ist": observed})
    if now.weekday() >= 5 or now.strftime("%H:%M") < "09:15":
        return prior_events
    path = ROOT / f"candidate-outcomes-{day}.json"
    state = _load(path, None)
    if not isinstance(state, dict):
        state = _restore_candidate_outcomes(day)
    live = {r["symbol"]: r for r in payload.get("market_universe", []) or []}
    live.update({r["symbol"]: r for r in decisions if r.get("ltp") is not None})
    changed = set()
    for row in decisions:
        if now.strftime("%H:%M") > "15:30":
            break
        if row.get("reason") in ("index_symbol_excluded", "lot_size_above_limit"):
            continue
        price = row.get("ltp")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        sid = signal_id(day, row["symbol"], row["side"], row.get("setup"))
        if sid in state:
            continue
        state[sid] = {"signal_id": sid, "symbol": row["symbol"], "side": row["side"],
                      "setup": row.get("setup"), "origin_snapshot_id": scan_id,
                      "origin": "first_candidate_observation", "qualified_at_origin": row.get("qualified"),
                      "rejection_reason_at_origin": row.get("reason"), "observed_at_ist": observed,
                      "reference_price": price,
                      "checkpoints": {str(m): {"intended_at_ist": (now+timedelta(minutes=m)).isoformat(),
                                              "status": "pending"} for m in (15, 30, 60)}}
        changed.add(sid)
    for sid, item in state.items():
        for checkpoint in item["checkpoints"].values():
            if checkpoint["status"] != "pending":
                continue
            intended = datetime.fromisoformat(checkpoint["intended_at_ist"])
            if now.strftime("%H:%M") > "15:30":
                checkpoint.update(status="unavailable", reason="session_ended_without_observation", assessed_at_ist=observed)
            elif intended.strftime("%H:%M") > "15:30":
                checkpoint.update(status="unavailable", reason="horizon_after_market_close")
            elif now > intended+timedelta(minutes=5):
                checkpoint.update(status="missed_window", assessed_at_ist=observed)
            elif now >= intended:
                quote = live.get(item["symbol"], {})
                price = quote.get("ltp")
                if not isinstance(price, (int, float)) or price <= 0:
                    continue
                direction = 1 if item["side"] == "bullish" else -1
                checkpoint.update(status="measured", observed_at_ist=observed, price=price,
                                  directional_return_pct=round(direction*(price/item["reference_price"]-1)*100, 4),
                                  market_data_observed_at_ist=None)
            else:
                continue
            changed.add(sid)
    _atomic_json(path, state)
    return prior_events + [event("candidate_outcome", day, state[sid], state[sid]) for sid in sorted(changed)]


def record_display_receipts(body):
    """Validate receipts against issued snapshots; deduplicate per browser/panel/signal/day."""
    if not isinstance(body, dict) or not isinstance(body.get("receipts"), list) or len(body["receipts"]) > 100:
        raise ValueError("receipts must be an array of at most 100 items")
    now = datetime.now(IST)
    day = now.date().isoformat()
    registry = {}
    validated = []
    for receipt in body["receipts"]:
        if not isinstance(receipt, dict) or receipt.get("session_date") != day:
            raise ValueError("receipt session does not match today's IST session")
        snapshot_id = receipt.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"scan_[a-f0-9]{32}", snapshot_id):
            raise ValueError("invalid snapshot id")
        if snapshot_id not in registry:
            registry[snapshot_id] = _load(ROOT / f"issued-{day}-{snapshot_id}.json", {})
        issued = registry[snapshot_id]
        known = issued.get("signals", {}).get(receipt.get("signal_id"))
        if not known or receipt.get("side") != known["side"]:
            raise ValueError("unknown snapshot or signal")
        panel = receipt.get("panel")
        client = receipt.get("client_session_id")
        if panel not in ("ranking", "instant_triggers") or not isinstance(client, str) or not (16 <= len(client) <= 80):
            raise ValueError("invalid panel or browser session")
        if receipt.get("page_visible") is not True or receipt.get("in_viewport") is not True:
            raise ValueError("receipt must describe a visible viewport appearance")
        try:
            rendered = datetime.fromisoformat(receipt["rendered_at"].replace("Z", "+00:00"))
            if rendered.tzinfo is None or abs((now-rendered).total_seconds()) > 86400:
                raise ValueError
        except (KeyError, AttributeError, TypeError, ValueError):
            raise ValueError("invalid browser render timestamp") from None
        rank = receipt.get("display_rank")
        if panel == "ranking" and (type(rank) is not int or not 1 <= rank <= 500):
            raise ValueError("invalid display rank")
        details = {"signal_id": receipt["signal_id"], "snapshot_id": receipt["snapshot_id"],
                   "receipt_id": "receipt_" + hashlib.sha256(
                       f"{day}|{client}|{panel}|{receipt['signal_id']}".encode()).hexdigest()[:32],
                   "client_session_id": client, "panel": panel, "display_rank": rank,
                   "rendered_at_client": rendered.isoformat(), "received_at_ist": now.isoformat(),
                   "prepared_at_ist": issued["prepared_at_ist"], "page_visible": True, "in_viewport": True,
                   "clock_note": "client clock; do not infer exact latency without clock synchronization"}
        validated.append((f"{client}|{panel}|{receipt['signal_id']}", event("signal_displayed", day, known, details)))
    with _RECEIPT_LOCK:
        receipt_path = ROOT / f"receipts-{day}.json"
        seen = set(_load(receipt_path, []))
        events = []
        for identity, entry in validated:
            if identity not in seen:
                events.append(entry)
                seen.add(identity)
        write_events(events)
        _atomic_json(receipt_path, sorted(seen))
    return {"ok": True, "accepted": len(events)}
