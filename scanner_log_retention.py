"""Bounded retention for the online scanner event log, never local evidence files.

Importing this module has no database or thread side effects. The command-line
default is a read-only preview; deletion requires --apply. The application may
explicitly start the app-scoped worker using its existing writer connection.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import json
import os
import sys
import threading
from typing import Callable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
RETENTION_DAYS = 14
BATCH_SIZE = 1_000
MAX_BATCHES = 5
# Shared across application processes; a transaction releases the lock itself.
ADVISORY_LOCK_KEY = 202610220014
PRODUCTION_HOSTNAME = "nifty50application.onrender.com"
TIMEOUTS_SQL = (
    "SELECT set_config('statement_timeout', %s, true), "
    "set_config('lock_timeout', %s, true), "
    "set_config('idle_in_transaction_session_timeout', %s, true)"
)
TIMEOUT_PARAMETERS = ("10000", "1000", "5000")
LOCK_SQL = "SELECT pg_try_advisory_xact_lock(%s)"
PREVIEW_SQL = (
    "SELECT count(*) FROM public.scanner_event_log WHERE session_date < %s"
)
DELETE_SQL = """WITH expired AS (
 SELECT id FROM public.scanner_event_log
 WHERE session_date < %s
 ORDER BY id LIMIT %s
 FOR UPDATE SKIP LOCKED
)
DELETE FROM public.scanner_event_log AS event
USING expired WHERE event.id = expired.id
RETURNING event.id"""


@dataclass(frozen=True)
class RetentionResult:
    status: str
    cutoff: date
    deleted_count: int = 0
    committed_batches: int = 0
    expired_count: int | None = None
    error_stage: str | None = None

    def public_summary(self) -> dict:
        """Fixed operational fields only: never a URL, SQL or driver exception."""
        result = asdict(self)
        result["cutoff"] = self.cutoff.isoformat()
        result["retention_days"] = RETENTION_DAYS
        return result


def retention_enabled(environ=None) -> bool:
    """Default on only for the intended Render deployment, never PR previews.

    A local app can share production's writer URL. Merely having DATABASE_URL
    therefore must not authorize its automatic background retention worker.
    Explicit 1 opts another non-PR deployment in; explicit 0/invalid values stop
    automatic retention. Importing this module does not evaluate the policy.
    """
    env = os.environ if environ is None else environ
    if str(env.get("IS_PULL_REQUEST", "")).lower() == "true":
        return False
    enabled = env.get("SCANNER_LOG_RETENTION_ENABLED")
    if enabled is not None:
        return enabled == "1"
    return (env.get("RENDER") == "true"
            and env.get("RENDER_EXTERNAL_HOSTNAME") == PRODUCTION_HOSTNAME)


def retention_cutoff(now: datetime | None = None) -> date:
    """Keep today and the preceding 13 IST calendar dates, including weekends."""
    stamp = datetime.now(IST) if now is None else now
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("retention_requires_timezone_aware_datetime")
    return stamp.astimezone(IST).date() - timedelta(days=RETENTION_DAYS - 1)


def _writer_url() -> str | None:
    # Lazily load the same writer configuration used by existing telemetry.
    # In particular, never borrow the replay reader's connection or privileges.
    import db_adapter
    return db_adapter.DATABASE_URL


def run_retention_pass(*, apply: bool = False, now: datetime | None = None,
                       url: str | None = None, connect: Callable | None = None) -> RetentionResult:
    """Preview, or commit at most five 1,000-row scanner-log-only batches.

    NULL/future session dates are deliberately not deleted. A batch-cap status
    means more rows MAY remain, not that an unbounded cleanup was completed.
    A short batch ends this pass, but SKIP LOCKED can leave locked expired rows
    for a later hourly pass. Counts include only acknowledged successful commits;
    a commit error cannot establish whether that last transaction reached the DB.
    """
    if type(apply) is not bool:
        raise ValueError("retention_apply_requires_boolean")
    cutoff = retention_cutoff(now)
    conn = cursor = None
    deleted = committed = 0
    stage = "configuration"
    try:
        database_url = _writer_url() if url is None else url
        if not isinstance(database_url, str) or not database_url.strip():
            return RetentionResult("not_configured", cutoff)
        stage = "connect"
        if connect is None:
            import psycopg2
            connect = psycopg2.connect
        # Transaction-local settings avoid unsupported pooled startup options.
        conn = connect(database_url, connect_timeout=3, sslmode="require")
        stage = "transaction"
        conn.set_session(readonly=not apply, isolation_level="READ COMMITTED", autocommit=False)
        cursor = conn.cursor()
        if not apply:
            stage = "timeouts"
            cursor.execute(TIMEOUTS_SQL, TIMEOUT_PARAMETERS)
            stage = "preview"
            cursor.execute(PREVIEW_SQL, (cutoff,))
            row = cursor.fetchone()
            count = row[0] if row else None
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("invalid_retention_preview_count")
            return RetentionResult("preview", cutoff, expired_count=count)

        for _ in range(MAX_BATCHES):
            stage = "timeouts"
            cursor.execute(TIMEOUTS_SQL, TIMEOUT_PARAMETERS)
            stage = "lock"
            cursor.execute(LOCK_SQL, (ADVISORY_LOCK_KEY,))
            row = cursor.fetchone()
            if not row or row[0] is not True:
                return RetentionResult("lock_busy", cutoff, deleted, committed)
            stage = "delete"
            cursor.execute(DELETE_SQL, (cutoff, BATCH_SIZE))
            rows = cursor.fetchall()
            count = len(rows)
            if count > BATCH_SIZE:
                raise ValueError("invalid_retention_batch_count")
            stage = "commit"
            conn.commit()
            deleted += count
            committed += 1
            if count < BATCH_SIZE:
                return RetentionResult("pass_complete", cutoff, deleted, committed)
        return RetentionResult("batch_cap_reached", cutoff, deleted, committed)
    except Exception:
        # No exception text, traceback, password, host or query reaches logs.
        return RetentionResult("error", cutoff, deleted, committed, error_stage=stage)
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def next_wait_seconds(result: RetentionResult) -> int:
    if result.status == "error":
        return 300
    if result.status in ("batch_cap_reached", "lock_busy"):
        return 60
    return 3600


def _retention_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        result = run_retention_pass(apply=True)
        print("[scanner-retention] " + json.dumps(result.public_summary(), sort_keys=True),
              file=sys.stderr, flush=True)
        # Event.wait is cancellable; there is no blocking server-startup wait.
        if stop_event.wait(next_wait_seconds(result)):
            break


def start_retention_worker(stop_event: threading.Event | None = None) -> threading.Thread:
    """Explicit app lifecycle helper, not an installed OS daemon or scheduler."""
    stop = threading.Event() if stop_event is None else stop_event
    thread = threading.Thread(target=_retention_loop, args=(stop,),
                              name="ScannerLogRetention", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Delete expired online scanner rows in bounded committed batches.")
    args = parser.parse_args(argv)
    result = run_retention_pass(apply=args.apply)
    print(json.dumps(result.public_summary(), sort_keys=True))
    return 1 if result.status in ("error", "not_configured") else 0


if __name__ == "__main__":
    raise SystemExit(main())
