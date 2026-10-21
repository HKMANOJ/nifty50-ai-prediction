"""Read recorded scanner decisions without running or changing the scanner.

The database path uses its own read-only connection.  This module does not
import scanner code, open local paths, create tables, or write telemetry.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
import math
import os
import re
import threading
import time
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MAX_EVENTS_PER_DAY = 40_000
MAX_SESSIONS = 30
SCAN_ID = re.compile(r"scan_[0-9a-f]{32}\Z")
BATCH_ID = re.compile(r"batch_[0-9a-f]{32}\Z")
SYMBOL = re.compile(r"[A-Z0-9&._-]{1,40}\Z")
SIDES = ("bullish", "bearish")
ROW_FIELDS = (
    "symbol", "side", "rank", "qualified", "included_in_payload", "reason",
    "setup", "ltp", "percent_change", "volume", "vc_ranking", "mp_score",
    "five_min_status", "breakout_time", "is_breakout", "one_side_rally_score",
    "status", "lot_size", "observed_at_ist",
)
_NUMBERS = frozenset(("ltp", "percent_change", "volume", "vc_ranking", "mp_score",
                      "one_side_rally_score", "lot_size"))
_BOOLS = frozenset(("qualified", "included_in_payload", "is_breakout"))
_EXTRA_FIELDS = ("snapshot_id", "log_batch_id", "kind", "candidate_count",
                 "scan_started_at_ist", "started_at_ist", "completed_at_ist",
                 "ranking_completed_at_ist", "source_commit")


class ReplayError(Exception):
    """Public error codes deliberately contain no driver or connection details."""

    def __init__(self, code: str, status: int = 503):
        self.code = code
        self.status = status
        super().__init__(code)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ReplayError("invalid_session_date", 400)
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ReplayError("invalid_session_date", 400) from None


def _snapshot_id(value):
    if not isinstance(value, str) or not SCAN_ID.fullmatch(value):
        raise ReplayError("invalid_snapshot_id", 400)
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(IST).isoformat() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _number(value):
    # bool is an int subclass, but is never a recorded numeric observation.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (OverflowError, TypeError):
        return None


def _integer(value, *, minimum=0):
    number = _number(value)
    if number is None or number < minimum or number != int(number):
        return None
    return int(number)


def _text(value, length=240):
    if not isinstance(value, str):
        return None
    return "".join(c for c in value[:length] if c >= " " and c != "\x7f")


def _payload(event):
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _relevant(event):
    return event.get("event_type") == "ranking_decision" or (
        event.get("event_type") == "scanner_health"
        and _payload(event).get("kind") == "scan_completed")


class EventStore(Protocol):
    source: str

    def sessions(self) -> list[dict]: ...

    def events(self, day: str) -> list[dict]: ...


class MemoryStore:
    """Already-loaded evidence only; no file or HTTP path can be opened here."""

    def __init__(self, events, source="local_recorded_scanner_preview"):
        self.source = source
        self._events = deepcopy(list(events))

    def sessions(self):
        by_day = defaultdict(set)
        for event in self._events:
            if not isinstance(event, Mapping) or not _relevant(event):
                continue
            try:
                day = _day(str(event.get("session_date")))
                sid = _snapshot_id(_payload(event).get("snapshot_id"))
            except ReplayError:
                continue
            by_day[day].add(sid)
        return [{"date": day, "snapshot_count": len(by_day[day])}
                for day in sorted(by_day, reverse=True)[:MAX_SESSIONS]]

    def events(self, day):
        day = _day(day)
        rows = [event for event in self._events if isinstance(event, Mapping)
                and str(event.get("session_date")) == day and _relevant(event)]
        if len(rows) > MAX_EVENTS_PER_DAY:
            raise ReplayError("day_event_limit")
        return deepcopy(rows)


# Keep the JSON projection explicit at the database boundary.  Nested objects,
# arrays and unbounded strings never leave PostgreSQL for this API.
_SELECT_KEYS = tuple(dict.fromkeys(ROW_FIELDS + _EXTRA_FIELDS))
_JSON_PROJECTION = ", ".join(
    "'{0}', CASE jsonb_typeof(payload->'{0}') "
    "WHEN 'string' THEN to_jsonb(left(payload->>'{0}', 512)) "
    "WHEN 'number' THEN payload->'{0}' WHEN 'boolean' THEN payload->'{0}' "
    "ELSE NULL END".format(key) for key in _SELECT_KEYS)
EVENTS_SQL = (
    "SELECT id, event_type, session_date, symbol, side, "
    "jsonb_build_object(" + _JSON_PROJECTION + ") AS payload "
    "FROM public.scanner_event_log WHERE session_date = %s "
    "AND (event_type = %s OR (event_type = %s AND payload->>'kind' = %s)) "
    "ORDER BY id LIMIT %s"
)
SESSIONS_SQL = """WITH days AS (
 SELECT session_date FROM public.scanner_event_log
 WHERE (event_type = %s OR (event_type = %s AND payload->>'kind' = %s))
 AND payload->>'snapshot_id' ~ %s
 GROUP BY session_date ORDER BY session_date DESC LIMIT %s
)
SELECT e.session_date, count(DISTINCT e.payload->>'snapshot_id') AS snapshot_count
FROM public.scanner_event_log e JOIN days d ON d.session_date=e.session_date
WHERE (e.event_type = %s OR (e.event_type = %s AND e.payload->>'kind' = %s))
AND e.payload->>'snapshot_id' ~ %s
GROUP BY e.session_date ORDER BY e.session_date DESC LIMIT %s"""


class PostgresStore:
    source = "neon_scanner_event_log"

    def __init__(self, url=None, *, connect=None):
        # No fallback to a scanner writer's DATABASE_URL.  The dedicated role
        # should have SELECT on public.scanner_event_log only.
        self._url = url if url is not None else os.environ.get("SCANNER_REPLAY_READ_DATABASE_URL")
        self._connect = connect

    def _query(self, sql, parameters, columns):
        if not self._url:
            raise ReplayError("replay_database_not_configured")
        conn = cursor = None
        try:
            connect = self._connect
            if connect is None:
                import psycopg2
                connect = psycopg2.connect
            conn = connect(self._url, connect_timeout=3, sslmode="require", options=(
                "-c statement_timeout=3000 -c lock_timeout=1000 "
                "-c idle_in_transaction_session_timeout=5000 "
                "-c default_transaction_read_only=on"))
            conn.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
            cursor = conn.cursor()
            cursor.execute(sql, parameters)
            rows = cursor.fetchall()
            return [dict(row) if isinstance(row, Mapping) else dict(zip(columns, row)) for row in rows]
        except Exception:
            raise ReplayError("replay_database_unavailable") from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

    def sessions(self):
        pattern = "^scan_[0-9a-f]{32}$"
        params = ("ranking_decision", "scanner_health", "scan_completed", pattern, MAX_SESSIONS) * 2
        rows = self._query(SESSIONS_SQL, params, ("session_date", "snapshot_count"))
        result = []
        for row in rows[:MAX_SESSIONS]:
            try:
                day = _day(str(row.get("session_date")))
            except ReplayError:
                continue
            count = _integer(row.get("snapshot_count"), minimum=1)
            if count is not None:
                result.append({"date": day, "snapshot_count": count})
        return result

    def events(self, day):
        day = _day(day)
        rows = self._query(EVENTS_SQL,
                           (day, "ranking_decision", "scanner_health", "scan_completed", MAX_EVENTS_PER_DAY + 1),
                           ("id", "event_type", "session_date", "symbol", "side", "payload"))
        if len(rows) > MAX_EVENTS_PER_DAY:
            raise ReplayError("day_event_limit")
        return rows


def _project_row(event, day):
    payload = _payload(event)
    row, issues = {}, set()
    for name in ROW_FIELDS:
        value = payload.get(name)
        if name in _NUMBERS:
            row[name] = _number(value)
        elif name in _BOOLS:
            row[name] = value if isinstance(value, bool) else None
        elif name == "rank":
            row[name] = _integer(value, minimum=1)
            if value is not None and row[name] is None:
                issues.add("invalid_rank")
        elif name == "observed_at_ist":
            row[name] = _timestamp(value)
        else:
            row[name] = _text(value)
    row["symbol"] = _text(event.get("symbol"), 40)
    row["side"] = event.get("side") if event.get("side") in SIDES else None
    if not row["symbol"] or not SYMBOL.fullmatch(row["symbol"]):
        row["symbol"] = None
        issues.add("invalid_symbol")
    if row["side"] is None:
        issues.add("invalid_side")
    for name in ("symbol", "side"):
        if payload.get(name) is not None and payload.get(name) != event.get(name):
            issues.add("row_identity_mismatch")
    if row["qualified"] is None:
        issues.add("missing_or_invalid_qualification")
    if row["qualified"] is True and row["rank"] is None:
        issues.add("qualified_row_without_rank")
    if row["included_in_payload"] is None:
        issues.add("missing_or_invalid_payload_inclusion")
    elif row["included_in_payload"] is True and row["qualified"] is not True:
        issues.add("payload_inclusion_without_qualification")
    if row["rank"] is not None and row["qualified"] is False:
        issues.add("rank_on_rejected_row")
    if row["observed_at_ist"] is None:
        issues.add("missing_or_invalid_observation_time")
    elif row["observed_at_ist"][:10] != day:
        issues.add("observation_session_mismatch")
    return row, issues


def project_day(events, day):
    """Pure projection; preserve observed rows and report evidence limitations."""
    day = _day(day)
    if len(events) > MAX_EVENTS_PER_DAY:
        raise ReplayError("day_event_limit")
    grouped = defaultdict(list)
    for event in events:
        if not isinstance(event, Mapping) or str(event.get("session_date")) != day or not _relevant(event):
            continue
        sid = _payload(event).get("snapshot_id")
        if isinstance(sid, str) and SCAN_ID.fullmatch(sid):
            grouped[sid].append(event)
    result = {}
    for sid, group in grouped.items():
        records = [event for event in group if event.get("event_type") == "ranking_decision"]
        markers = [event for event in group if event.get("event_type") == "scanner_health"]
        issues, invalid, unknown = set(), False, False
        batches = {_payload(event).get("log_batch_id") for event in group
                   if isinstance(_payload(event).get("log_batch_id"), str)
                   and BATCH_ID.fullmatch(_payload(event)["log_batch_id"])}
        ambiguous = len(batches) > 1
        if ambiguous:
            issues.add("ambiguous_log_batches")
            invalid = True
        if any(not isinstance(_payload(event).get("log_batch_id"), str)
               or not BATCH_ID.fullmatch(_payload(event)["log_batch_id"]) for event in group):
            issues.add("missing_or_invalid_log_batch_id")
            unknown = True
        if not markers:
            issues.add("missing_scan_completed_marker")
            unknown = True
        elif len(markers) > 1:
            issues.add("duplicate_scan_completed_markers")
            invalid = True
        expected = _integer(_payload(markers[0]).get("candidate_count")) if len(markers) == 1 else None
        if markers and expected is None:
            issues.add("missing_or_invalid_expected_rows")
            unknown = True
        if expected is not None and expected != len(records):
            issues.add("recorded_row_count_mismatch")
            invalid = True
        rows, observed, started, identities, ranks, event_ids = [], set(), set(), set(), set(), set()
        for event in records:
            payload = _payload(event)
            row, row_issues = _project_row(event, day)
            issues.update(row_issues)
            invalid = invalid or bool(row_issues)
            rows.append(row)
            if row["observed_at_ist"]:
                observed.add(row["observed_at_ist"])
            value = _timestamp(payload.get("scan_started_at_ist"))
            if value:
                started.add(value)
            identity = (row["side"], row["symbol"], row["setup"])
            if identity in identities:
                issues.add("duplicate_candidate_rows")
                invalid = True
            identities.add(identity)
            if row["rank"] is not None:
                rank_key = (row["side"], row["rank"])
                if rank_key in ranks:
                    issues.add("duplicate_stored_ranks")
                    invalid = True
                ranks.add(rank_key)
            event_id = event.get("id")
            if event_id is not None:
                if event_id in event_ids:
                    issues.add("duplicate_event_ids")
                    invalid = True
                event_ids.add(event_id)
        completed = {_timestamp(_payload(marker).get("completed_at_ist")) for marker in markers}
        completed.discard(None)
        if len(markers) == 1 and not completed:
            issues.add("missing_or_invalid_completion_time")
            invalid = True
        for marker in markers:
            value = _timestamp(_payload(marker).get("started_at_ist"))
            if value:
                started.add(value)
        if len(observed) > 1 or len(completed) > 1 or (observed and completed and observed != completed):
            issues.add("inconsistent_snapshot_times")
            invalid = True
        if len(started) > 1:
            issues.add("inconsistent_scan_start_times")
            invalid = True
        at = next(iter(observed)) if len(observed) == 1 else next(iter(completed)) if len(completed) == 1 else None
        if at is None:
            issues.add("missing_snapshot_time")
            unknown = True
        if at and at[:10] != day:
            issues.add("snapshot_session_mismatch")
            invalid = True
        rows.sort(key=lambda row: (row["rank"] is None, row["rank"] or 0))
        commits = [_text(_payload(record).get("source_commit"), 64) for record in records]
        commits = [value if value and re.fullmatch(r"[0-9a-fA-F]{7,64}", value) else None for value in commits]
        integrity = "incomplete" if invalid else "unknown" if unknown else "complete"
        result[sid] = {
            "session_date": day, "snapshot_id": sid, "observed_at_ist": at,
            "started_at_ist": next(iter(started)) if len(started) == 1 else None,
            "integrity": integrity, "issues": sorted(issues),
            "coverage": {"expected_rows": expected, "recorded_rows": len(records)},
            "provenance": {"recorded_source_commits": sorted({value for value in commits if value}),
                           "rows_without_source_commit": sum(value is None for value in commits),
                           "runtime_verification": "not_independently_verified"},
            "bullish": [row for row in rows if row["side"] == "bullish"],
            "bearish": [row for row in rows if row["side"] == "bearish"],
            "unassigned": [row for row in rows if row["side"] not in SIDES],
        }
    return result


class ReplayService:
    """Thread-safe short cache bounded to a small number of recorded days."""

    def __init__(self, store: EventStore, cache_seconds=10, *, max_cached_days=4, clock=None):
        self.store = store
        self.source = (store.source if store.source in (
            "neon_scanner_event_log", "local_recorded_scanner_preview") else "recorded_scanner_history")
        self._ttl = min(30, max(5, float(cache_seconds)))
        self._max_days = min(8, max(1, int(max_cached_days)))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._cache = {}

    def _base(self):
        return {"ok": True, "source": self.source, "historical": True}

    def _cached(self, key, loader):
        with self._lock:
            now = self._clock()
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < self._ttl:
                return deepcopy(cached[1])
            for old_key, (saved, _) in list(self._cache.items()):
                if now - saved >= self._ttl:
                    del self._cache[old_key]
            value = loader()
            if key != "sessions":
                day_keys = [item for item in self._cache if item != "sessions"]
                while len(day_keys) >= self._max_days:
                    oldest = min(day_keys, key=lambda item: self._cache[item][0])
                    del self._cache[oldest]
                    day_keys.remove(oldest)
            self._cache[key] = (self._clock(), value)
            return deepcopy(value)

    def sessions(self):
        rows = self._cached("sessions", self.store.sessions)
        return {**self._base(), "sessions": rows[:MAX_SESSIONS]}

    def _day_snapshots(self, day):
        return self._cached(day, lambda: project_day(self.store.events(day), day))

    def snapshots(self, day):
        day = _day(day)
        rows = []
        for value in self._day_snapshots(day).values():
            rows.append({"id": value["snapshot_id"], "at": value["observed_at_ist"],
                         "started_at": value["started_at_ist"], **value["coverage"],
                         "integrity": value["integrity"], "issues": value["issues"]})
        rows.sort(key=lambda row: (row["at"] or "", row["id"]))
        return {**self._base(), "session_date": day, "snapshots": rows}

    def snapshot(self, day, snapshot_id):
        day, snapshot_id = _day(day), _snapshot_id(snapshot_id)
        result = self._day_snapshots(day).get(snapshot_id)
        if result is None:
            raise ReplayError("snapshot_not_found", 404)
        if "ambiguous_log_batches" in result["issues"]:
            raise ReplayError("ambiguous_snapshot", 409)
        return {**self._base(), **result}
