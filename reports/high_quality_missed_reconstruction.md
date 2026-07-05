# High-Quality Missed Opportunities Reconstruction

Date generated: 2026-06-30

Scope:
- Filter used:
  - confidence >= 80
  - projected_move >= 70
  - future_move >= 50
  - stop_hit_first = false
- Source data:
  - `opportunity_audit` MySQL table
  - current live qualification logic in [nifty50_ai_prediction_console.html](/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/nifty50_ai_prediction_console.html)
- Current live adaptive settings:
  - `dynamicMinMove = 42`
  - `reduceOiBlocking = true`

Important:
- This reconstruction does **not** use stored replay reasons as the blocker.
- It rebuilds gate status from the current project rules plus the stored candle/session/availability fields.
- Historical replay rows do not preserve full live OI/PCR values, so `PCR_passed` and `OI_passed` are reconstructed from `nse_confirmation_available`.

## Gate rules used

- `pattern_detected`
  - PASS when a directional pattern candidate exists for the row.
- `confidence_threshold_passed`
  - PASS when confidence >= 65.
- `projected_move_threshold_passed`
  - PASS when projected move >= current adaptive threshold 42.
- `EMA_alignment_passed`
  - PASS when trend signal direction matches the trade side.
- `PCR_passed`
  - FAIL when NSE confirmation is unavailable.
- `OI_passed`
  - FAIL when NSE confirmation is unavailable.
- `optionChainAligned`
  - PASS if NSE report is reliable and side-aligned, or if `reduceOiBlocking = true` and the pattern is strong enough for the current relaxation rule.
- `NSE_confirmation_available`
  - Directly from audit row.
- `premium_available`
  - Directly from audit row.
- `session_valid`
  - Directly from audit row.

## Reconstructed Trades

| Date | Time | Pattern | Side | Confidence | Projected Move | Future Move | Pattern | Conf | Move | EMA | PCR | OI | ChainAligned | NSE Conf | Premium | Session | Root Blocker |
|---|---|---|---|---:|---:|---:|---|---|---|---|---|---|---|---|---|---|---|
| 2026-06-24 | 10:45 | Bullish Engulfing | CALL | 84 | 115.33 | 65.75 | PASS | PASS | PASS | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | optionChainAligned |
| 2026-06-25 | 13:20 | Double Top | PUT | 82 | 96.95 | 95.10 | PASS | PASS | PASS | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | optionChainAligned |
| 2026-06-25 | 13:25 | Double Top | PUT | 82 | 89.00 | 92.05 | PASS | PASS | PASS | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | optionChainAligned |
| 2026-06-25 | 14:15 | Double Top Breakdown | PUT | 88 | 83.37 | 75.45 | PASS | PASS | PASS | PASS | FAIL | FAIL | PASS | FAIL | FAIL | PASS | premium_available |
| 2026-06-25 | 14:20 | Double Top Breakdown | PUT | 88 | 83.41 | 70.60 | PASS | PASS | PASS | PASS | FAIL | FAIL | PASS | FAIL | FAIL | PASS | premium_available |
| 2026-06-25 | 14:25 | Double Top Breakdown | PUT | 88 | 81.84 | 68.70 | PASS | PASS | PASS | PASS | FAIL | FAIL | PASS | FAIL | FAIL | PASS | premium_available |
| 2026-06-25 | 14:30 | Double Top Breakdown | PUT | 88 | 82.30 | 66.55 | PASS | PASS | PASS | PASS | FAIL | FAIL | PASS | FAIL | FAIL | PASS | premium_available |
| 2026-06-25 | 14:35 | Double Top Breakdown | PUT | 88 | 86.27 | 61.50 | PASS | PASS | PASS | PASS | FAIL | FAIL | PASS | FAIL | FAIL | PASS | premium_available |
| 2026-06-25 | 14:50 | Bear Flag | PUT | 81 | 106.50 | 63.65 | PASS | PASS | PASS | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | optionChainAligned |

## Why the split happens

### 1. `optionChainAligned` FAIL group

These still fail the current live signal gate:
- Bullish Engulfing
- Double Top (13:20, 13:25)
- Bear Flag (14:50)

Reason:
- Current OI relaxation only kicks in when:
  - pattern confidence >= 76
  - absolute pattern signal >= 0.68
  - trend side matches the pattern side
- These rows do not clear that full relaxation rule.

### 2. `premium_available` FAIL group

These would likely clear the current signal-side gate:
- Double Top Breakdown at 14:15, 14:20, 14:25, 14:30, 14:35

Reason:
- `reduceOiBlocking = true`
- Double Top Breakdown signal is strong enough for OI relaxation
- trend is bearish on all five
- projected move is well above the current adaptive move threshold
- confidence is well above the BUY threshold

So under the current project:
- signal-side gating would likely allow `BUY PUT`
- but `premium_available = false` still prevents proper live option execution / record creation

## Trend / EMA evidence

Trend state reconstructed from `nifty_candles`:

- 2026-06-24 10:45 Bullish Engulfing: `BULLISH`
- 2026-06-25 13:20 Double Top: `SIDEWAYS` with negative trend signal (`-0.14`)
- 2026-06-25 13:25 Double Top: `SIDEWAYS` with negative trend signal (`-0.14`)
- 2026-06-25 14:15 onward bearish rows: `BEARISH`, strength `94`

This means EMA/trend alignment did **not** block any of the 9 filtered trades.

## Aggregated root blockers

| Rank | Root Blocker | Count |
|---|---|---:|
| 1 | optionChainAligned | 4 |
| 2 | premium_available | 5 |
| 3 | confidence / min move / EMA / session | 0 |

## Summary

- Total high-quality missed opportunities: 9
- Still blocked by current signal gate: 4
- Likely no longer blocked by current signal gate, but still blocked at execution-data layer: 5

## Main conclusion

The current project is no longer mainly missing these trades because of confidence or move threshold. For this filtered set, the root cause has shifted to:

1. `optionChainAligned` for weaker or not-fully-relaxed patterns
2. `premium_available` for the strongest bearish breakdowns
