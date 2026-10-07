# Scanner observability

These additions use the existing `scanner_event_log` JSONB table. No new table,
schema migration, score change, ranking change or Early Signals panel is required
by this commit. Events are also written to gitignored
`inputs/scanner_telemetry/<session-date>.jsonl`; database mirroring is best effort.
No extra NSE/Yahoo requests are made for telemetry or outcomes.

## Correlation and scope

`snapshot_id` identifies a genuine scanner run. `signal_id` is deterministic for
IST date + symbol + direction + OI setup. It identifies a candidate/setup across
scans, not an independent trade or every intraday breakout wave. The same ID is
attached to ranked rows, instant triggers, decisions, candidate outcomes and
browser receipts. A different setup or session produces a different ID.

The scanner records every fetched input row's actual exclusion stage, including
missing OI, oversized lots, insufficient volume, failed rally conditions, failed
trend, opening-range/previous-close checks, consolidation and payload rank limit.
Only evaluated score components are present; later, unexecuted gates are not
invented. This is the scanner's intake universe, not every exchange-listed stock.

## Events

| Event | Evidence |
|---|---|
| `scanner_decision` | Candidate/setup, dated decision time, qualification, failed condition, rally component points and blockers, breakout information, price and existing score fields. |
| `ranking_decision` | Server rank, percentage-change ordering, Top-6 eligibility, payload inclusion/limit reason, snapshot and ranking completion time. Server eligibility is not client visibility. |
| `signal_displayed` | Issued snapshot/signal, random browser session, panel, actual displayed rank, client render clock, server receipt clock, page visible and row in viewport. |
| `candidate_outcome` | First candidate observation including rejects/outside Top 6; intended and observed dated 15/30/60-minute checkpoints, direction-adjusted changes and explicit coverage states. |

The legacy `discovery` payload now identifies itself as server Top-6 eligibility
and includes correlation IDs. Its old fields remain readable for historical
analysis. The dashboard currently has a 500-row display cap and reassigns ranks
by percentage change; receipts capture the actual displayed rank instead of
assuming a six-row cap. Instant-trigger upgrades are now mirrored when they occur,
not only when the trigger is first created.

## Visibility receipts

`POST /api/stock_suggestions/display_receipts` accepts at most 100 receipts / 128 KiB.
Each receipt must match a server-issued snapshot and signal for today's IST date,
have a permitted panel/direction/rank and a timezone-aware client timestamp.
Issued snapshot metadata is persisted for delayed scrolling and refreshes.

The dashboard binds ranking-table and Instant Triggers rows after rendering,
waits two animation frames and observes at least 10% viewport intersection while
the page is visible. Hidden tabs, inactive views, offscreen and detached rows do
not acknowledge display. An appearance means the row was available in the
viewport, not that a person noticed it. Other panels are not instrumented yet.

Receipts are batched, retried after transient failures, and deduplicated per IST
date + browser session + panel + signal. Browser session storage and server disk
state prevent refresh/retry duplication. A deterministic `receipt_id` also lets
analyses deduplicate receipts across container replacements. This captures first visibility per panel
and later per-snapshot rank observations (`display_rank_observed`). Same snapshot/rank
retries are deduplicated. Previous rank reflects server arrival order; order by client
time within a browser session, with ties treated as ambiguous. Random browser IDs
contain no account identifier, user agent or IP address.

`prepared_at_ist` marks scanner preparation/ranking completion before persistence,
not actual HTTP delivery. Browser render clocks and server clocks may differ;
do not report exact scanner-to-screen milliseconds without accounting for clock
offset. Quote market timestamps remain null when the feed does not supply a
verified dated observation. Latest candle timestamps are recorded when evaluated.

## Outcome interpretation

Candidate outcomes start at first candidate observation, including rejected
candidates. They are separate from the legacy breakout-trigger outcomes and
must not be mixed as if they share an entry point. Checkpoints record only quotes
observed from their intended time through five minutes later. Late observations
become `missed_window`; missing quotes stay pending until the window expires.
Horizons after market close and unfinished prior sessions become `unavailable`.
No future price is used to qualify or rank a stock.

State and local JSONL survive process restarts on a persistent volume. If current
candidate state is absent, the latest mirrored candidate outcomes for today's
session are recovered from Neon before collecting new observations. Container
replacement without a persistent `inputs` volume can still lose unmirrored state,
older-session finalization and local receipt deduplication. Coverage flags
must be respected. These prices do not prove trade profitability, executed entries,
stop/target order or one-sided movement.

## Extended tracing (v2)

Scoring, ranking and existing market requests remain unchanged. Additional evidence:

- `scan_replay`: exact normalized inputs per completed scan: gainers/losers, OI,
  indexes, fetched candidate candles, effective numeric/boolean constants, lot-size
  map, exclusions, code hashes and requested/missing extra symbols. This is not raw
  HTTP bodies; credentials/headers/environment strings are not copied. Future replay
  must use these inputs and matching code offline, not live feeds or future candles.
  No new formula or full replay harness is deployed by this patch.
- `data_provenance`: NSE attempts/failure types/raw timestamp fields, Yahoo quote
  epoch/bar-start fields, candle cache age/reuse and missing batch results. Supplied
  upstream timestamps are *unverified*, not proof that row prices belong to those
  timestamps. Yahoo synthetic row prices use last candle closes, not metadata quotes.
- `signal_lifecycle`: first observed/qualified time, eligibility changes and new
  confirmed wave IDs. Origin is scanner observation, not backdated to a candle label.
  Eligibility loss is not necessarily trade invalidation; absent intake rows are
  unobserved, not rejected. Restore daily trace state from Neon after local loss when
  available; otherwise mark prior continuity unknown.
- `rank_transition`: old/new rank, Top-5 entry/exit, percentage/score evidence. Existing
  daily-percentage ordering drives rank; compare the full replay pool to see who moved
  ahead. No invented intermediate ranks between scans.
- `client_trace`: allowlisted anonymous receipt/render/ignored-snapshot, load/render
  failure, reconnect, visibility and connectivity diagnostics; shared snapshot/delivery
  IDs for HTTP/WS. Render follows two animation frames; viewport evidence is separate.
  HTTP duration uses a monotonic browser clock. Absolute client/server clocks remain
  unsynchronized. No raw error text, URLs, IPs or user agents are accepted. Diagnostics
  are client-reported, not authenticated identity or proof of human attention.
  The bounded diagnostics retry queue is saved in browser session storage when
  available; expired prior-day entries are discarded. Browser storage failure,
  a destroyed browser session or overflow can still lose unsent evidence.
- `scanner_health`: scheduler startup, attempts/completion/skips/failure, HTTP writes
  and WS send/failure/skip counts. A bounded daemon queue keeps DB I/O off delivery
  paths. Shutdown/overflow can lose queued health evidence; overflow prints to stderr.
- `logging_health`: failed DB batches get local `health-<date>.jsonl` records with
  batch ID, error class and count. No recursive write to the failing DB. Local JSONL
  is not durable on ephemeral Render disks: use a persistent inputs volume and verify
  Neon coverage. A total logging outage can still leave gaps. No automatic deletion
  or schema/index changes are made. Monitor replay/trace storage volume.
- `wave_outcome`: completed-candle observed MFE/MAE bounds and first observed
  stop/target touch, with reference/levels frozen at the first observed confirmed
  wave. Excludes forming/prior-session/origin-straddling bars. Missing bars mark partial
  coverage; both levels in one candle are `ambiguous_same_bar`. No assumed fills,
  intrabar sequence or trade profitability. Replay contains the observed candle path.

The existing 14:45 automatic cutoff and scan cadence are unchanged and limit outcome
coverage. Full trace starts after deployment; no missing old evidence is backfilled.

## Verification

```sh
python3 -m unittest test_scanner_telemetry -v
python3 -m unittest test_scanner_trace -v
node test_scanner_display_telemetry.js
node test_scanner_loading.js
node --check scanner_display_telemetry.js
```

Tests use temporary files, mocked database/market access and a loopback HTTP
server. They do not write production logs or perform live scans.
