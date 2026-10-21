# Public main-scanner history replay

Replay-only release prepared on 21 October 2026. Code deployment and production
enablement are separate: the archive API is disabled until its configuration is
provided. Existing experimental ranking rules remain separate and unchanged.
This feature does not record a new strategy or start a collector. The updated
release makes the displayed archive public when replay is enabled, as requested:
every visitor can open the link without an access key, including the first visit.

## What it does

The dashboard's new **Replay** link opens `/scanner_replay.html`, an isolated
historical screen with recorded session/scan selection, previous/next controls,
stock search, both directions and retained/Top-5/all-qualified/all-logged views.
Recorded ranks are preserved, including gaps and qualified rows outside the
retained payload. Current lot filters are not applied. Rejected and missing
qualification evidence are distinguished. Missing snapshots are not filled.

Public read-only APIs (no login or access key):

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

- `SCANNER_REPLAY_ENABLED=1`: explicitly enables public archive access. Leave
  disabled or set to `0` if the displayed history must not be public.
- `SCANNER_REPLAY_READ_DATABASE_URL`: a dedicated PostgreSQL/Neon connection whose
  role has SELECT access to the intended scanner archive. Do not put it in HTML,
  source control, a browser URL or chat. There is deliberately no fallback to the
  scanner writer's `DATABASE_URL`.
- `SCANNER_REPLAY_ORIGIN=https://nifty50application.onrender.com` (or the exact
  deployed HTTPS origin).

`SCANNER_REPLAY_TOKEN` is no longer used. An existing Render value can remain
unused; do not copy it into HTML, JavaScript or a replay link. No new flag or
browser setup is required. The page automatically loads the available sessions,
then the selected day's scans and recorded snapshot using anonymous GETs with
`credentials: "omit"`. It has no unlock form, access-key state, cookies, browser
storage or account system. Public API access is intentional, not protected by
the obscurity of the link or by browser CORS restrictions. Anyone with the link
or API address can read the safe projected history while replay is enabled.

The dedicated database connection/password remain server-side and are never
returned to a visitor. Public responses use an allowlisted projection of scanner
records, not raw JSON telemetry. Public visibility does not grant database
connections, SQL access, record editing, production configuration or trading
access. This is a public archive, not a guarantee that sharing its historical
market data meets every upstream provider's licensing requirements.

This update reuses existing Render configuration; no new environment variable,
database role, database secret or production credential is created by the code.
Disabled or unavailable archives do not fall back to local/live
files. Database reads enforce TLS, read-only repeatable-read transactions and
connection/statement/lock timeouts. Responses have no-store and security headers;
wildcard CORS is suppressed for replay, but CORS is not authentication and does
not make the public archive private. At most four archive requests are admitted
concurrently; saturation returns a generic temporary-unavailable error without
starting another source read. Existing bounded caches and DB timeouts remain.

Use a read URL pointed at the intended production archive. DB location alone is
not independent proof of runtime provenance: if local runs previously wrote to
that same table, they can appear too. Recorded commit identities and missing
identity counts are surfaced, with runtime verification explicitly unknown.
No silent local-source fallback or production-attribution claim is made.

The main server also denies static GET/HEAD access to raw scanner telemetry,
hidden paths, Python sources and virtual-environment paths, preventing raw archive
files from bypassing the safe projected archive. Existing public input JSON endpoints
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
python3 -m unittest test_scanner_history_replay test_scanner_history_replay_routes test_scanner_replay_public test_scanner_telemetry -q
node --test test_scanner_replay_ui.cjs test_scanner_replay_races.cjs
```

The original release passed 54 Python and 6 JavaScript checks in the clean release
checkout. The public-viewer update passes 58 Python and 13 JavaScript checks,
including a real loopback HTTP sequence without keys/cookies against synthetic
records, private-field/file exclusion and busy-slot admission/release checks.
The JavaScript checks cover first-visit
anonymous autoload, missing history, local-preview identity, no credential/storage
or live-feed use, failure cleanup, rank preservation, unknown evidence and stale
request races. The database boundary is mocked; test success alone does not
establish live Neon permissions or deployed Render integration. After deploying,
verify the page in a fresh/private browser without a key and confirm the session,
scan and snapshot requests return recorded history. Disabled/database-unavailable
states must remain honest errors, never substitute live or local data.

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
