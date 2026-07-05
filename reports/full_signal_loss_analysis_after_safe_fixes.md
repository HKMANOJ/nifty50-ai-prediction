# Full Signal Loss Analysis After Safe Fixes

Date generated: 2026-06-30

Safe changes applied in simulation:
- Replay/session validation uses candle timestamp instead of current system time.
- Premium missing does not block BUY CALL / BUY PUT signal display or underlying trade record.
- Missing NSE/OI/PCR can be bypassed only for strong patterns when confidence >= 80, projected move >= 70, and EMA is aligned.

Scope:
- Source report rows analyzed: `254`
- Newly actionable signals after safe fixes: `38`
- Winners unlocked: `1`
- Losers unlocked: `23`
- Neither target nor stop reached: `14`

## Summary

| Metric | Value |
|---|---:|
| Total signals | 38 |
| Winners | 1 |
| Losers | 23 |
| Win rate | 4.2% |
| Profit factor estimate | 0.27 |
| Additional good trades recovered | 1 |
| False signals added | 23 |

## Recovery Source

| Fix | Recovered Signals |
|---|---:|
| session_fix | 0 |
| premium_record_only | 38 |
| option_chain_fallback | 6 |

## Pattern Breakdown (unlocked signals)

| Pattern | Signals | Winners | Losers | Neither |
|---|---:|---:|---:|---:|
| W Pattern Breakout | 18 | 0 | 18 | 0 |
| Double Top Breakdown | 14 | 0 | 2 | 12 |
| W Pattern Forming | 3 | 0 | 3 | 0 |
| Double Top | 2 | 1 | 0 | 1 |
| Bear Flag | 1 | 0 | 0 | 1 |

## Unlocked Winners

| Date | Time | Side | Pattern | Confidence | Projected Move | Future Move | Recovery Source |
|---|---|---|---|---:|---:|---:|---|
| 2026-06-25 | 13:25 | PUT | Double Top | 82 | 89.00 | 92.05 | premium_record_only, option_chain_fallback |

## Unlocked Losers

| Date | Time | Side | Pattern | Confidence | Projected Move | Future Move | Recovery Source |
|---|---|---|---|---:|---:|---:|---|
| 2026-06-24 | 11:40 | CALL | W Pattern Breakout | 88 | 43.35 | 6.75 | premium_record_only |
| 2026-06-24 | 11:45 | CALL | W Pattern Breakout | 88 | 43.80 | 6.65 | premium_record_only |
| 2026-06-24 | 11:50 | CALL | W Pattern Breakout | 88 | 48.55 | 8.05 | premium_record_only |
| 2026-06-24 | 11:55 | CALL | W Pattern Breakout | 88 | 53.35 | 15.70 | premium_record_only |
| 2026-06-24 | 12:00 | CALL | W Pattern Breakout | 88 | 44.15 | 6.50 | premium_record_only |
| 2026-06-24 | 12:15 | CALL | W Pattern Breakout | 88 | 52.40 | 50.95 | premium_record_only |
| 2026-06-24 | 13:30 | CALL | W Pattern Breakout | 88 | 74.04 | 17.45 | premium_record_only |
| 2026-06-24 | 13:35 | CALL | W Pattern Breakout | 88 | 74.46 | 15.40 | premium_record_only |
| 2026-06-24 | 13:40 | CALL | W Pattern Breakout | 88 | 69.19 | 14.35 | premium_record_only |
| 2026-06-24 | 13:45 | CALL | W Pattern Breakout | 88 | 70.34 | 3.85 | premium_record_only |
| 2026-06-24 | 13:50 | CALL | W Pattern Breakout | 88 | 69.86 | 1.00 | premium_record_only |
| 2026-06-24 | 13:55 | CALL | W Pattern Breakout | 88 | 73.01 | 12.95 | premium_record_only |
| 2026-06-24 | 14:10 | CALL | W Pattern Breakout | 88 | 74.59 | 12.10 | premium_record_only |
| 2026-06-24 | 14:15 | CALL | W Pattern Forming | 82 | 154.40 | 10.65 | premium_record_only, option_chain_fallback |
| 2026-06-24 | 14:20 | CALL | W Pattern Forming | 82 | 158.20 | 14.45 | premium_record_only, option_chain_fallback |
| 2026-06-24 | 14:25 | CALL | W Pattern Forming | 82 | 157.65 | 13.90 | premium_record_only, option_chain_fallback |
| 2026-06-25 | 11:55 | CALL | W Pattern Breakout | 88 | 63.01 | 24.90 | premium_record_only |
| 2026-06-25 | 12:00 | CALL | W Pattern Breakout | 88 | 61.34 | 13.55 | premium_record_only |
| 2026-06-25 | 12:05 | CALL | W Pattern Breakout | 88 | 57.80 | 11.75 | premium_record_only |
| 2026-06-25 | 12:10 | CALL | W Pattern Breakout | 88 | 57.36 | 7.85 | premium_record_only |
| 2026-06-25 | 12:15 | CALL | W Pattern Breakout | 88 | 56.57 | 4.40 | premium_record_only |
| 2026-06-25 | 13:10 | PUT | Double Top Breakdown | 88 | 80.25 | 42.20 | premium_record_only |
| 2026-06-25 | 14:40 | PUT | Double Top Breakdown | 88 | 93.01 | 31.75 | premium_record_only |

## Still Blocked After Safe Fixes

| Date | Time | Side | Pattern | Confidence | Projected Move | Future Move | Still Blocked By |
|---|---|---|---|---:|---:|---:|---|
| 2026-06-24 | 10:25 | CALL | Higher-close continuation | 50 | 25.56 | 21.10 | confidence, min_move, option_chain |
| 2026-06-24 | 10:30 | CALL | Higher-close continuation | 50 | 18.43 | 21.10 | confidence, min_move, option_chain |
| 2026-06-24 | 10:35 | PUT | Bearish engulfing reversal | 50 | 7.33 | 0.00 | confidence, min_move, ema, option_chain |
| 2026-06-24 | 10:45 | CALL | Bullish Engulfing | 84 | 115.33 | 65.75 | option_chain |
| 2026-06-24 | 10:45 | CALL | Bullish engulfing reversal | 50 | 22.99 | 43.55 | confidence, min_move, option_chain |
| 2026-06-24 | 10:50 | PUT | Rising Wedge | 73 | 110.27 | 7.25 | ema, option_chain |
| 2026-06-24 | 10:50 | CALL | Ascending Triangle Breakout | 51 | 20.65 | 43.55 | confidence, min_move, option_chain |
| 2026-06-24 | 10:55 | PUT | Rising Wedge | 73 | 101.01 | 0.45 | ema, option_chain |
| 2026-06-24 | 10:55 | CALL | Ascending Triangle | 50 | 19.05 | 51.75 | confidence, min_move, option_chain |
| 2026-06-24 | 11:00 | PUT | Rising Wedge | 73 | 101.17 | 5.70 | ema, option_chain |
| 2026-06-24 | 11:05 | CALL | Bullish breakout | 58 | 30.59 | 25.15 | confidence, min_move, option_chain |
| 2026-06-24 | 11:15 | PUT | Rising Wedge | 73 | 87.26 | 12.00 | ema, option_chain |
| 2026-06-24 | 11:20 | PUT | Rising Wedge | 73 | 87.10 | 18.20 | ema, option_chain |
| 2026-06-24 | 11:20 | CALL | Ascending Triangle Breakout | 52 | 22.83 | 22.45 | confidence, min_move, option_chain |
| 2026-06-24 | 11:25 | PUT | Rising Wedge | 73 | 85.21 | 16.95 | ema, option_chain |
| 2026-06-24 | 11:25 | CALL | Ascending Triangle Breakout | 52 | 20.12 | 22.45 | confidence, min_move, option_chain |
| 2026-06-24 | 11:30 | PUT | Rising Wedge | 73 | 81.56 | 9.30 | ema, option_chain |
| 2026-06-24 | 11:35 | PUT | Rising Wedge | 73 | 78.49 | 16.35 | ema, option_chain |
| 2026-06-24 | 11:40 | CALL | Bullish breakout | 56 | 24.34 | 6.45 | confidence, min_move, option_chain |
| 2026-06-24 | 11:50 | CALL | W Pattern Breakout | 55 | 21.83 | 0.00 | confidence, min_move, option_chain |
| 2026-06-24 | 12:00 | CALL | W Pattern Breakout | 55 | 22.05 | 0.00 | confidence, min_move |
| 2026-06-24 | 12:05 | PUT | Rising Wedge | 72 | 71.99 | 20.75 | ema, option_chain |
| 2026-06-24 | 12:10 | PUT | Rising Wedge | 72 | 69.44 | 13.95 | ema, option_chain |
| 2026-06-24 | 12:10 | PUT | Bearish breakdown | 50 | 1.05 | 12.00 | confidence, min_move, ema, option_chain |
| 2026-06-24 | 12:15 | CALL | Ascending Triangle | 52 | 17.33 | 35.70 | confidence, min_move, option_chain |
| 2026-06-24 | 12:20 | PUT | Rising Wedge | 71 | 66.24 | 38.40 | ema, option_chain |
| 2026-06-24 | 12:25 | PUT | Rising Wedge | 71 | 65.33 | 28.65 | ema, option_chain |
| 2026-06-24 | 12:25 | PUT | Bearish engulfing reversal | 50 | 10.02 | 12.00 | confidence, min_move, ema, option_chain |
| 2026-06-24 | 12:30 | PUT | Double Top Breakdown | 69 | 33.35 | 9.05 | min_move |
| 2026-06-24 | 12:35 | PUT | Rising Wedge | 72 | 69.47 | 3.60 | option_chain |
| 2026-06-24 | 12:35 | PUT | Descending Triangle | 50 | 2.48 | 0.00 | confidence, min_move, option_chain |
| 2026-06-24 | 12:40 | PUT | Double Top | 59 | 47.00 | 12.20 | confidence, ema, option_chain |
| 2026-06-24 | 12:45 | PUT | Double Top | 59 | 40.75 | 5.95 | confidence, min_move, ema, option_chain |
| 2026-06-24 | 12:50 | CALL | Bullish Engulfing | 82 | 65.21 | 94.75 | option_chain |
| 2026-06-24 | 12:55 | PUT | Double Top | 61 | 60.00 | 10.25 | confidence, ema, option_chain |
| 2026-06-24 | 12:55 | CALL | W Pattern Breakout | 55 | 22.98 | 77.15 | confidence, min_move, option_chain |
| 2026-06-24 | 13:00 | PUT | Double Top | 60 | 52.05 | 1.75 | confidence, ema, option_chain |
| 2026-06-24 | 13:05 | CALL | W Pattern Breakout | 86 | 27.40 | 69.75 | min_move |
| 2026-06-24 | 13:05 | CALL | W Pattern Breakout | 55 | 23.38 | 69.20 | confidence, min_move |
| 2026-06-24 | 13:10 | CALL | W Pattern Breakout | 84 | 16.30 | 58.65 | min_move |
| 2026-06-24 | 13:20 | CALL | W Pattern Breakout | 83 | 7.70 | 50.05 | min_move |
| 2026-06-24 | 13:35 | CALL | W Pattern Breakout | 57 | 26.36 | 0.00 | confidence, min_move |
| 2026-06-24 | 13:50 | CALL | W Pattern Breakout | 55 | 21.14 | 0.00 | confidence, min_move |
| 2026-06-24 | 14:05 | CALL | W Pattern Breakout | 52 | 9.47 | 0.00 | confidence, min_move |
| 2026-06-24 | 14:20 | CALL | W Pattern Breakout | 55 | 22.04 | 1.25 | confidence, min_move |
| 2026-06-24 | 14:25 | CALL | W Pattern Breakout | 55 | 22.42 | 5.00 | confidence, min_move |
| 2026-06-24 | 14:30 | PUT | Double Top | 64 | 86.10 | 46.10 | confidence, ema, option_chain |
| 2026-06-24 | 14:30 | CALL | W Pattern Breakout | 53 | 12.63 | 87.90 | confidence, min_move |
| 2026-06-24 | 14:35 | PUT | Double Top | 63 | 76.70 | 36.70 | confidence, option_chain |
| 2026-06-24 | 14:35 | PUT | Descending Triangle Breakdown | 50 | 4.69 | 31.70 | confidence, min_move, option_chain |
| 2026-06-24 | 14:40 | PUT | Double Top | 64 | 89.85 | 49.85 | confidence, ema, option_chain |
| 2026-06-24 | 14:40 | CALL | W Pattern Breakout | 55 | 20.90 | 105.65 | confidence, min_move |
| 2026-06-24 | 14:45 | CALL | W Pattern Breakout | 53 | 12.70 | 105.65 | confidence, min_move |
| 2026-06-24 | 14:45 | PUT | Double Top | 64 | 83.70 | 43.70 | confidence, ema, option_chain |
| 2026-06-24 | 14:50 | CALL | W Pattern Breakout | 55 | 23.90 | 102.40 | confidence, min_move |
| 2026-06-24 | 14:50 | PUT | Double Top | 64 | 97.65 | 57.65 | confidence, ema, option_chain |
| 2026-06-24 | 14:55 | CALL | W Pattern Breakout | 53 | 14.47 | 100.65 | confidence, min_move |
| 2026-06-24 | 14:55 | PUT | Double Top | 64 | 87.50 | 47.50 | confidence, ema, option_chain |
| 2026-06-24 | 15:00 | CALL | W Pattern Breakout | 50 | 6.62 | 100.65 | confidence, min_move, ema, option_chain |
| 2026-06-24 | 15:00 | PUT | Double Top | 63 | 71.95 | 31.95 | confidence, option_chain |
| 2026-06-24 | 15:05 | CALL | W Pattern Breakout | 50 | 4.41 | 116.25 | confidence, min_move, ema, option_chain |
| 2026-06-24 | 15:05 | PUT | Double Top Breakdown | 68 | 26.40 | 24.30 | min_move |
| 2026-06-24 | 15:10 | PUT | Double Top Breakdown | 50 | 24.26 | 0.00 | confidence, min_move, option_chain |
| 2026-06-24 | 15:10 | PUT | Double Top Breakdown | 83 | 5.30 | 2.85 | min_move |
| 2026-06-24 | 15:15 | PUT | Double Top Breakdown | 50 | 26.30 | 0.00 | confidence, min_move, option_chain |
| 2026-06-24 | 15:15 | PUT | Double Top Breakdown | 84 | 9.35 | 3.80 | min_move |
| 2026-06-24 | 15:20 | PUT | Double Top Breakdown | 50 | 22.78 | 0.00 | confidence, min_move, option_chain |
| 2026-06-24 | 15:20 | PUT | Double Top Breakdown | 85 | 17.80 | 12.25 | min_move |
| 2026-06-24 | 15:25 | PUT | Double Top Breakdown | 50 | 20.64 | 0.00 | confidence, min_move, option_chain |
| 2026-06-24 | 15:25 | PUT | Double Top Breakdown | 84 | 12.00 | 0.00 | min_move |
| 2026-06-25 | 10:35 | PUT | Bearish engulfing reversal | 50 | 7.90 | 0.00 | confidence, min_move, ema, option_chain |
| 2026-06-25 | 10:50 | PUT | Bearish Engulfing | 65 | 72.76 | 5.80 | ema, option_chain |
| 2026-06-25 | 10:50 | PUT | Bearish engulfing reversal | 50 | 13.09 | 0.00 | confidence, min_move, ema, option_chain |
| 2026-06-25 | 10:55 | PUT | Double Top | 62 | 65.90 | 22.00 | confidence, ema, option_chain |
| 2026-06-25 | 11:00 | PUT | Double Top | 60 | 49.45 | 5.55 | confidence, ema, option_chain |
| 2026-06-25 | 11:05 | PUT | Double Top | 61 | 58.20 | 8.55 | confidence, ema, option_chain |
| 2026-06-25 | 11:05 | CALL | Ascending Triangle | 54 | 23.86 | 51.95 | confidence, min_move, option_chain |
| 2026-06-25 | 11:15 | CALL | W Pattern Forming | 76 | 37.15 | 64.45 | min_move, option_chain |
| 2026-06-25 | 11:15 | CALL | Higher-close continuation | 54 | 21.09 | 56.10 | confidence, min_move, option_chain |
| 2026-06-25 | 11:20 | PUT | Bearish Engulfing | 64 | 63.99 | 11.35 | confidence, ema, option_chain |
| 2026-06-25 | 11:20 | PUT | Bearish engulfing reversal | 50 | 11.80 | 0.00 | confidence, min_move, ema, option_chain |
| 2026-06-25 | 11:25 | PUT | Double Top | 59 | 44.95 | 9.50 | confidence, ema, option_chain |
| 2026-06-25 | 11:30 | PUT | Double Top | 59 | 45.15 | 13.90 | confidence, ema, option_chain |
| 2026-06-25 | 11:35 | PUT | Double Top | 60 | 49.15 | 17.90 | confidence, ema, option_chain |
| 2026-06-25 | 11:40 | PUT | Double Top | 60 | 50.30 | 19.05 | confidence, ema, option_chain |
| 2026-06-25 | 11:45 | PUT | Double Top | 61 | 58.80 | 27.55 | confidence, ema, option_chain |
| 2026-06-25 | 11:45 | CALL | Bullish breakout | 57 | 20.72 | 54.40 | confidence, min_move, option_chain |
| 2026-06-25 | 11:50 | CALL | W Pattern Breakout | 86 | 26.45 | 52.85 | min_move |
| 2026-06-25 | 11:50 | CALL | W Pattern Breakout | 56 | 20.47 | 50.45 | confidence, min_move, option_chain |
| 2026-06-25 | 11:55 | CALL | W Pattern Breakout | 57 | 24.99 | 22.90 | confidence, min_move |
| 2026-06-25 | 12:00 | CALL | W Pattern Breakout | 58 | 26.52 | 11.25 | confidence, min_move |
| 2026-06-25 | 12:05 | CALL | W Pattern Breakout | 58 | 24.81 | 8.80 | confidence, min_move |
| 2026-06-25 | 12:10 | CALL | W Pattern Breakout | 57 | 25.15 | 0.00 | confidence, min_move |
| 2026-06-25 | 12:15 | CALL | W Pattern Breakout | 57 | 24.49 | 0.00 | confidence, min_move |
| 2026-06-25 | 12:20 | CALL | W Pattern Breakout | 65 | 8.80 | 1.75 | min_move |
| 2026-06-25 | 12:20 | CALL | W Pattern Breakout | 55 | 14.06 | 0.00 | confidence, min_move |
| 2026-06-25 | 12:30 | PUT | Lower-close continuation | 50 | 5.85 | 7.55 | confidence, min_move, option_chain |
| 2026-06-25 | 12:35 | CALL | W Pattern Breakout | 68 | 27.60 | 7.40 | min_move, ema, option_chain |
| 2026-06-25 | 12:40 | PUT | Lower-close continuation | 50 | 5.55 | 7.55 | confidence, min_move, option_chain |
| 2026-06-25 | 13:00 | CALL | Bullish Engulfing | 66 | 81.27 | 7.65 | ema, option_chain |
| 2026-06-25 | 13:00 | CALL | Bullish engulfing reversal | 50 | 6.88 | 0.00 | confidence, min_move, ema, option_chain |
| 2026-06-25 | 13:10 | PUT | Bearish breakdown | 51 | 17.71 | 38.65 | confidence, min_move, option_chain |
| 2026-06-25 | 13:15 | CALL | Bullish Engulfing | 73 | 87.99 | 14.70 | ema, option_chain |
| 2026-06-25 | 13:15 | CALL | Bullish engulfing reversal | 50 | 6.43 | 0.00 | confidence, min_move, ema, option_chain |
| 2026-06-25 | 13:25 | PUT | Descending Triangle | 50 | 17.06 | 79.75 | confidence, min_move, option_chain |
| 2026-06-25 | 13:30 | CALL | Bullish Engulfing | 66 | 85.69 | 19.10 | ema, option_chain |
| 2026-06-25 | 13:30 | CALL | Bullish engulfing reversal | 50 | 4.67 | 1.00 | confidence, min_move, ema, option_chain |
| 2026-06-25 | 13:35 | PUT | Double Top | 64 | 106.85 | 109.90 | confidence, ema, option_chain |
| 2026-06-25 | 13:40 | PUT | Double Top | 64 | 107.90 | 130.50 | confidence, ema, option_chain |
| 2026-06-25 | 13:45 | PUT | Double Top | 64 | 110.10 | 135.70 | confidence, ema, option_chain |
| 2026-06-25 | 13:45 | CALL | Higher-close continuation | 53 | 8.14 | 0.00 | confidence, min_move, option_chain |
| 2026-06-25 | 13:50 | PUT | Double Top | 80 | 65.45 | 122.55 | option_chain |
| 2026-06-25 | 13:50 | CALL | Higher-close continuation | 50 | 3.91 | 0.00 | confidence, min_move, ema, option_chain |
| 2026-06-25 | 13:55 | PUT | Double Top | 78 | 55.40 | 112.50 | option_chain |
| 2026-06-25 | 14:00 | PUT | Double Top | 79 | 59.95 | 141.85 | option_chain |
| 2026-06-25 | 14:05 | PUT | Double Top | 78 | 48.80 | 130.70 | option_chain |
| 2026-06-25 | 14:10 | PUT | Double Top Breakdown | 84 | 14.55 | 96.45 | min_move |
| 2026-06-25 | 14:10 | PUT | Bearish breakdown | 54 | 25.83 | 88.45 | confidence, min_move, option_chain |
| 2026-06-25 | 14:15 | PUT | Bearish breakdown | 62 | 34.49 | 69.70 | confidence, min_move, option_chain |
| 2026-06-25 | 14:20 | PUT | Lower-close continuation | 60 | 32.41 | 54.35 | confidence, min_move, option_chain |

## Interpretation

- Safe fixes unlock `38` blocked setups from the existing loss-analysis set.
- They recover `1` good trades and add `23` false signals.
- Biggest recovery path: `premium_record_only`.
- This means the remaining missed-move problem is still mainly strategy gating, not premium/session plumbing alone.