# Main-scanner history replay

Replay-only release prepared on 21 October 2026. Code deployment and production
enablement are separate: the archive API is disabled until its configuration is
provided. Existing experimental ranking rules remain separate and unchanged.
This feature does not record a new strategy or start a collector.

## What it does

The dashboard's new **Replay** link opens `/scanner_replay.html`, an isolated
historical screen with recorded session/scan selection, previous/next controls,
stock search, both directions and retained/Top-5/all-qualified/all-logged views.
Recorded ranks are preserved, including gaps and qualified rows outside the
retained payload. Current lot filters are not applied. Rejected and missing
qualification evidence are distinguished. Missing snapshots are not filled.

Read-only APIs:

- `GET /api/scanner_replay/sessions` — latest 30 recorded sessions.
- `GET /api/scanner_replay/snapshots?date=YYYY-MM-DD` — actual logged snapshots.
- `GET /api/scanner_replay/snapshot?date=YYYY-MM-DD&snapshot_id=scan_...` — safe
  projected rows from that one recorded snapshot, not the latest live response.

Reads existing `public.scanner_event_log` ranking decisions and matching
`scanner_health/scan_completed` markers. Shared snapshot/batch identity,
candidate-count, duplicate and timestamp checks determine log completeness.
Conflicting batches fail closed. Completeness does not prove price accuracy.
There is no exact reconstruction of browser visibility, pinned daily lists,
“Top5Eligible” or historical chart candles from these ranking rows alone.

No WebSocket, live-index/candle requests, alerts, live rendering, display receipts,
rule replay, rescoring, trading, model calls or data writes occur on this screen.
It bypasses the live `_send_json` delivery-telemetry path deliberately.

## Production enablement — operator action required

Set these server-side secrets/configuration in the intended production service:

- `SCANNER_REPLAY_ENABLED=1`.
- `SCANNER_REPLAY_READ_DATABASE_URL`: a dedicated PostgreSQL/Neon connection whose
  role has SELECT access to the intended scanner archive. Do not put it in HTML,
  source control, a browser URL or chat. There is deliberately no fallback to the
  scanner writer's `DATABASE_URL`.
- `SCANNER_REPLAY_ORIGIN=https://nifty50application.onrender.com` (or the exact
  deployed HTTPS origin).
- `SCANNER_REPLAY_TOKEN`: a securely generated random access key, at least 24
  characters. Share only through the user's approved secret-management process.

The viewer asks for the history access key. It sends it only in the same-origin
Authorization header and keeps it in memory, not browser storage or URL query.
No production secret or credential has been created/configured by this task.
Disabled, unauthenticated or unavailable archives do not fall back to local/live
files. Database reads enforce TLS, read-only repeatable-read transactions and
connection/statement/lock timeouts. Responses have no-store and security headers;
wildcard CORS is suppressed for replay.

Use a read URL pointed at the intended production archive. DB location alone is
not independent proof of runtime provenance: if local runs previously wrote to
that same table, they can appear too. Recorded commit identities and missing
identity counts are surfaced, with runtime verification explicitly unknown.
No silent local-source fallback or production-attribution claim is made.

The main server also denies static GET/HEAD access to raw scanner telemetry,
hidden paths, Python sources and virtual-environment paths, preventing raw archive
files from bypassing replay access controls. Existing public input JSON endpoints
remain available. This is not a comprehensive security audit of all old routes.

## Data retention and gaps

This feature reads existing online logs; it does not create a new archive,
backfill missing scans, guarantee database mirroring, change retention or extend
market-hours collection. Missing completion markers or partial batches are
labelled unknown/incomplete. Source freshness and option profitability are not
established by an archived ranking or its historical BO/BD candle label.

## Safe local preview and verification

The development workspace has a separate foreground loopback-only preview using
saved LOCAL scanner records from 21 October, explicitly labelled **not production
history**. Its launcher/data are not included in this production release. It
starts no scanner, market collector or DB connection. The main local app and
experimental replay remain separate.

Tests from this repository:

```sh
python3 -m unittest test_scanner_history_replay test_scanner_history_replay_routes test_scanner_telemetry -q
node --test test_scanner_replay_ui.cjs test_scanner_replay_races.cjs
```

54 Python and 6 JavaScript checks passed in the clean release checkout, including read-only connection options,
auth/origin/private-path boundaries, historical/live telemetry isolation, exact
rank preservation, unknown evidence, incomplete batches and stale request races.
The DB boundary was mocked: live Neon connectivity/role permissions and deployed
Render integration still need verification after approved configuration.

## Deployment isolation

The development `nifty50Application` checkout contains unrelated LOCAL experiments
and chart changes. This release was prepared in a separate clean checkout.
**Do not stage/push the entire dirty development checkout.**

The replay-only change set is:

- New `scanner_history_replay.py`, `scanner_history_replay_routes.py`.
- New `scanner_replay.html`, `scanner_replay.js`, `scanner_replay.css`.
- New history/replay tests and this document.
- In `MLAIStockV2.html`: only the single Replay anchor beside Refresh Live.
- In `serve_nifty50_app.py`: only replay dispatch, HEAD/static archive protection,
  write rejection and per-response CORS suppression/reset.

Only those changes belong in the release. No ranking, thresholds, experimental
scoring, production log records or trading settings are included. To roll back,
revert the replay commit on the application branch; do not reset or erase the
separate local research work.
