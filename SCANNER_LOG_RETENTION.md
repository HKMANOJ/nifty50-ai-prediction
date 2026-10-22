# Scanner log retention — 14 IST calendar days

This maintenance policy affects **only `public.scanner_event_log` in PostgreSQL**.
It does not alter ranking rules, signals, options, candles, other database tables,
local JSONL files or separate research/audit reports.

Keep today and the preceding 13 IST calendar dates. The cutoff is calculated from
the actual current IST date, never the latest scan's date. Delete only rows whose
`session_date` is strictly before that cutoff. For example, on 22 October 2026,
keep 9–22 October inclusive; 8 October and earlier expire. This is not 14 trading
sessions. NULL and future session dates are not silently deleted.

## Automatic operation after deployment

The normal app server starts an app-scoped background maintenance worker at
startup on the intended Render production hostname
`nifty50application.onrender.com`. It runs independently of scanner requests,
without a new OS daemon, Codex automation, database extension or public deletion
endpoint. It does not depend on a browser session. No new environment value
is required for that production service.

Default-off in local checkouts, other services and PR previews prevents a local
test that shares the production writer URL from silently deleting history.
`SCANNER_LOG_RETENTION_ENABLED=0` disables maintenance. Explicit `=1` opts in a
non-preview deployment; PR previews always remain disabled. Invalid values fail
closed. Do not enable this in a development app pointed at production unless
that destructive maintenance is intentional.

The worker uses the existing server-side writer `DATABASE_URL`, not an archive
reader's credentials or a process-global connection. The writer must
already have DELETE permission on the table. No role/privilege changes occur.
TLS, connection/statement/lock limits, an advisory transaction lock and small
committed batches limit interference with live writes. A pass deletes at most
5,000 expired rows; backlog passes are separated by a minute. Normal checks are
hourly, failures retry after five minutes. Failure never substitutes successful
cleanup, drops a table or stops the scanner. A sleeping/offline app does not
clean up; it resumes after startup.

Maintenance logs report fixed status, cutoff and committed deletion count without
credentials or raw database exceptions. Partial progress stays explicit when a
later batch fails. No continuously growing database cleanup telemetry is added.

## Operator preview / manual application

The module imports without connecting. Its CLI defaults to a read-only preview:

```sh
python3 scanner_log_retention.py
```

Only an explicit `--apply` requests permanent batched deletion:

```sh
python3 scanner_log_retention.py --apply
```

Do not run apply until the database target and expired coverage are reviewed.
This code does not export a recoverable backup. Deleted records disappear from
online analysis; separate local research artifacts remain, but they
are not guaranteed to be a complete database backup.

## Storage and limitations

The read-only 22 October preview found 622,530 rows, of which 61,416 had
`session_date < 2026-10-09`. The scanner table occupied 923 MB including indexes
and associated storage. Those figures are a point-in-time observation, not an
estimate of bytes freed by DELETE.

Deleting rows does not necessarily shrink the allocated table immediately.
PostgreSQL vacuuming normally makes dead-row space reusable; Neon history,
branches and plan accounting can also affect reported usage. This policy does
not guarantee the warning clears immediately or that 14 days fit the plan.
No `VACUUM FULL`, table rewrite, branch deletion, plan upgrade or schema/index
change is performed. Check retained volume and provider usage after rollout.

Sources: [PostgreSQL vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html),
[Neon branch storage](https://neon.com/docs/manage/branches),
[Render default environment values](https://render.com/docs/environment-variables).

The separate production Replay UI and API were rolled back at the user's
request. Retention remains enabled independently; it has no Replay dependency.
