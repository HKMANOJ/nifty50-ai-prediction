# 2026-06-29 Session Analysis (12:30-15:15)

Source logic used:
- `/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/nifty50_ai_prediction_console.html`
- Evaluated as if each candle were processed during live NSE hours, so `session validity` is not falsely blocked by post-close review mode.

Key result:
- The engine did detect bearish structure repeatedly.
- It did not generate `BUY PUT` because `NSE/OI confirmation` was not reliable, `option-chain side` never aligned, and `projected move` never reached the hard 50-point gate in this window.

## Candle-by-candle

| Time | Pattern | Pattern Conf | Market Bias | Projected Move | Combined Conf | Signal | Rejection Reasons | Min Move | EMA | OI | PCR | Premium | Session |
|---|---|---:|---|---:|---:|---|---|---|---|---|---|---|---|
| 12:30 | Breakdown Retest Failure | 68 | Bearish | 37.70 | 61 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 37.70 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 12:35 | Breakdown Retest Failure | 68 | Bearish | 39.03 | 62 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 39.03 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 12:40 | Double Top Breakdown | 72 | Bearish | 32.55 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 32.55 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 12:45 | Double Top Breakdown | 72 | Bearish | 31.19 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 31.19 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 12:50 | Double Top Breakdown | 72 | Bearish | 30.35 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 30.35 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 12:55 | Double Top Breakdown | 72 | Neutral | 15.29 | 56 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 15.29 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:00 | Double Top Breakdown | 72 | Bearish | 22.11 | 57 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 22.11 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:05 | Double Top Breakdown | 72 | Neutral | 12.65 | 55 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 12.65 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:10 | Double Top Breakdown | 72 | Bearish | 25.02 | 57 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 25.02 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:15 | Double Top Breakdown | 72 | Bearish | 26.92 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 26.92 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:20 | Double Top Breakdown | 72 | Bearish | 26.64 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 26.64 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:25 | Double Top Breakdown | 72 | Neutral | 15.18 | 56 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 15.18 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:30 | Double Top Breakdown | 72 | Bearish | 28.39 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 28.39 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:35 | Double Top Breakdown | 72 | Bearish | 25.08 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 25.08 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:40 | Double Top Breakdown | 72 | Bearish | 29.17 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 29.17 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:45 | Double Top Breakdown | 72 | Bearish | 29.26 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 29.26 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:50 | Double Top Breakdown | 72 | Bearish | 32.24 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 32.24 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 13:55 | Double Top Breakdown | 72 | Bearish | 30.89 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 30.89 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:00 | Double Top Breakdown | 72 | Bearish | 18.67 | 56 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 18.67 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:05 | Double Top Breakdown | 72 | Neutral | 15.86 | 56 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 15.86 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:10 | Double Top Breakdown | 72 | Neutral | 13.21 | 56 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 13.21 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:15 | Ascending Triangle | 54 | Neutral | 1.01 | 50 | WAIT | Pattern transition not cleared; NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 1.01 / 50 fail | SIDEWAYS | one-sided / unreliable | unavailable | missing | valid |
| 14:20 | Double Top Breakdown | 72 | Bearish | 24.09 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 24.09 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:25 | Double Top Breakdown | 72 | Bearish | 23.59 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 23.59 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:30 | Ascending Triangle | 54 | Neutral | 0.89 | 50 | WAIT | Pattern transition not cleared; NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 0.89 / 50 fail | SIDEWAYS | one-sided / unreliable | unavailable | missing | valid |
| 14:35 | Double Top Breakdown | 72 | Bearish | 29.90 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 29.90 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:40 | Double Top Breakdown | 45 | Neutral | 10.01 | 50 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 10.01 / 50 fail | SIDEWAYS | one-sided / unreliable | unavailable | missing | valid |
| 14:45 | Double Top Breakdown | 45 | Neutral | 8.12 | 50 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 8.12 / 50 fail | SIDEWAYS | one-sided / unreliable | unavailable | missing | valid |
| 14:50 | Double Top Breakdown | 72 | Bearish | 30.64 | 58 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 30.64 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 14:55 | Double Top Breakdown | 72 | Bearish | 36.45 | 60 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 36.45 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 15:00 | Double Top Breakdown | 72 | Bearish | 39.09 | 61 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 39.09 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 15:05 | Double Top Breakdown | 72 | Bearish | 25.45 | 56 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 25.45 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 15:10 | Double Top Breakdown | 72 | Bearish | 41.80 | 62 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate | 41.80 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |
| 15:15 | Double Top Breakdown | 72 | Neutral | 24.70 | 56 | WAIT | NSE/OI confirmation missing; Option-chain side not aligned; Pattern/OI/news agreement too weak; Minimum move filter blocked; Confidence below BUY gate; Base candle score is neutral | 24.70 / 50 fail | BEARISH | one-sided / unreliable | unavailable | missing | valid |

## Earliest bearish candidate

There was **no candle in this window that met the project’s own “reasonable PUT” standard** of:
- bearish pattern,
- bearish score alignment,
- valid option-chain confirmation,
- and 50+ projected move.

If we still take the **earliest bearish candidate** your engine recognized, it was:

- Time: `12:30`
- Pattern: `Breakdown Retest Failure`
- Entry candidate: `23934.15`
- Stop loss: `23964.15`
- Target: `23884.15`
- Combined confidence: `61%`
- Projected move: `37.70`
- Future downside achieved:
  - from close: `18.25` points
  - from trigger entry: `9.00` points
- Outcome:
  - `stop hit first = true`
  - `target hit first = false`

## What-if support 23925 broke?

Simulated on the `15:10` candle:

| Simulated Close | Projected Move | Confidence | Signal | Remaining Blocks |
|---|---:|---:|---|---|
| 23924.90 | 45.31 | 56 | WAIT | OI missing, option-chain not aligned, min move still below 50, confidence below BUY gate |
| 23920.00 | 47.20 | 58 | WAIT | OI missing, option-chain not aligned, min move still below 50, confidence below BUY gate |
| 23915.00 | 49.17 | 60 | WAIT | OI missing, option-chain not aligned, min move still below 50, confidence below BUY gate |
| 23910.00 | 51.17 | 61 | WAIT | OI missing, option-chain not aligned, confidence below BUY gate |

So:
- A clean break of `23925` **alone** would **not** have generated `BUY PUT`.
- Even after the move threshold clears near `23910`, the current logic still blocks the trade because:
  - `nseReport.isReliable === false`
  - `optionChainAligned === false`
  - `confidenceReview.signal === 'WAIT'`

## Root cause

The engine saw bearish price structure, but the live trade gate never opened because **one-sided NSE option data kept `optionChainAligned` false while projected move also stayed below the 50-point threshold for the whole 12:30–15:15 window**.
