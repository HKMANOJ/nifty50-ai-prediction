# Backend scanner audit agent

This standalone worker has no dashboard integration and no access to broker
orders. It does not import or trigger the scanner, change rankings, fetch new
market data, or alter existing logs. AI is disabled by default. Numerical
analysis is deterministic; optional AI only proposes evidence-linked hypotheses.

## Run an evidence report

Use a dedicated PostgreSQL role with SELECT on `public.scanner_event_log` only.
Configure `SCANNER_AUDIT_READ_DATABASE_URL` securely in the deployment environment.
Do not put credentials in Git, CLI arguments, or reports.

```sh
python3 scanner_audit_agent.py --date 2026-10-09
```

For an offline export (JSON array with `id`, `event_type`, `session_date`,
`symbol`, `side`, `payload`, `created_at`, or existing telemetry JSONL):

```sh
python3 scanner_audit_agent.py --date 2026-10-09 --events /path/to/events.json
```

Timezone-aware timestamps are required for time comparisons. Local JSONL without
database event IDs remains usable, but evidence IDs are unavailable. Malformed
JSON causes the job to fail rather than silently dropping evidence.

## Storage

JSON and Markdown reports are written to a sibling `scanner_audit_reports/`
directory **outside the application's public web root**, or to
`SCANNER_AUDIT_OUTPUT_DIR` / `--output`. Output inside the application directory
is rejected to prevent the static server from publicly serving reports. New
directories/files use owner-only permissions. For Render, choose a writable
persistent disk mount outside the public application directory.
Each run has a unique name, source SHA-256, maximum source event ID, generation
time and code version references. JSON contains all candidates and per-wave
outcomes; Markdown provides coverage, timelines, failures and limitations.
Local files on Render are ephemeral unless a persistent disk is configured.

To persist history in Neon, configure a **separate**
`SCANNER_AUDIT_REPORT_DATABASE_URL`. After the one-time initialization, use a
writer role with INSERT on `public.scanner_audit_reports` only (no privileges on
scanner tables). Administrative credentials are needed only for initialization.

```sh
python3 scanner_audit_agent.py --date 2026-10-09 --persist-db --init-reports-db
python3 scanner_audit_agent.py --date 2026-10-09 --persist-db
```

DDL is explicitly requested, never automatic on a read. Writer SQL is restricted
in code to this one report table. Duplicate report IDs do not overwrite records.
Database failures leave local reports intact but exit nonzero; logs show exception
class only, not potentially secret-bearing exception text.

## Optional AI explanation

Configure `OPENAI_API_KEY` and `SCANNER_AUDIT_AI_MODEL` (a model supporting the
Responses API and Structured Outputs). Then explicitly use `--ai`.

```sh
python3 scanner_audit_agent.py --date 2026-10-09 --ai --persist-db
```

**This sends an allowlisted aggregate audit summary to OpenAI**, including stock
symbols, ranking gaps and directional quote observations for up to 40 candidates.
Review/authorize this transmission before enabling it. No raw logs, browser IDs,
broker credentials, actual trade records or database URLs are sent. The model has
no tools, SQL or execution access. Responses use `store: false`; this is not a
promise of zero retention—provider abuse-monitoring policies may still apply.
Structured output plus local validation rejects unknown evidence references.
AI output is labeled unverified; cited references do not guarantee factual
interpretation. A refusal, timeout, missing key, or malformed output leaves the
evidence report available. No live API call is required for evidence reporting.

Implementation follows [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
and [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).

## Daily operation (not enabled automatically)

Run this as a **separate backend worker**, not the dashboard request thread:

```sh
python3 scanner_audit_agent.py --watch --persist-db
```

It runs once per weekday after 16:30 IST, retries failures, and catches up for
today if started later. Market holidays produce explicit empty evidence reports;
no exchange holiday calendar is inferred. Restarting the worker can produce a new
versioned report the same day. It does not backfill missed days automatically;
use `--date` for those. Keep AI opt-in separately; do not configure daily external
transmission without approval. No production schedule, schema, or API key is
installed just by merging these files.

## Interpretation safeguards

- Pre-market ranking observations are excluded from intraday candidate timelines.
- Candidate timelines group symbol/side; setup changes can change signal identity.
  Exact visibility matching uses snapshot + signal + symbol + side.
- First qualification is not necessarily a closed-candle confirmed breakout.
- Ranking gaps are prominence delays, not transport latency. No receipt means
  unknown display, not proof that the stock was absent.
- Quote returns at 15/30/60 minutes use the first observation within a six-minute
  tolerance, including later rejected candidates. They are not interpolated,
  exchange-timestamp verified, executed returns, or option profits.
- Missing observations are unknown, not zero. Pending horizons remain pending.
- Existing per-wave MFE/MAE and first-touch evidence preserve partial coverage,
  exclusion of origin-straddling bars and ambiguity within the same candle.
- Options chain history, fills, fees, slippage, multi-day controlled comparisons
  and counterfactual ranking replays are future additions—not fabricated here.

## Tests

```sh
python3 -m unittest test_scanner_audit_agent test_scanner_telemetry test_scanner_trace -q
```

Tests use synthetic events and mocked database/API connections. They do not
create tables, send telemetry to OpenAI, or mutate production scanner records.
