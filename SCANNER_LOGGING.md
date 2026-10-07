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
analyses deduplicate receipts across container replacements. This captures first visibility per panel,
not every subsequent repaint, reappearance or rank movement. Random browser IDs
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

## Verification

```sh
python3 -m unittest test_scanner_telemetry -v
node test_scanner_display_telemetry.js
node --check scanner_display_telemetry.js
```

Tests use temporary files, mocked database/market access and a loopback HTTP
server. They do not write production logs or perform live scans.
