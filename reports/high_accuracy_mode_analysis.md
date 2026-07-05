# High Accuracy Mode Evidence Report

Scope:
- Replay source: current post-fix strategy replay subset
- Dates: 2026-06-20, 2026-06-23, 2026-06-24, 2026-06-25
- Full replay set after fixes: 25 trades
- This report analyzes only the 2 winners and 9 losers, as requested
- 14 neutral / open trades are excluded from filter statistics

## Trade Count Check
- Decided trades analyzed: 6
- Winners: 2
- Losers: 4
- Baseline win rate: 33.3%
- Baseline profit factor: 1.33

## Decided Trades

| Date | Time | Pattern | Verdict | Confidence | Projected Move | Trend Strength | EMA | ATR | Body % | Wick % | Pattern Score | Time Bucket | Stop Dist | Target Dist | RR | Neckline | Retest | Volume | Future Move |
|---|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|---|---|---:|
| 2026-06-23 | 11:50 | Double Top Breakdown | winner | 88 | 70.29 | 50 | PASS | 17.57 | 52.33 | 31.00 | 80.00 | 10:30-12:30 | 28.11 | 70.29 | 2.50 | YES | YES | N/A | 96.90 |
| 2026-06-23 | 12:20 | Double Top Breakdown | loser | 88 | 94.31 | 50 | PASS | 23.58 | 88.89 | 8.38 | 77.10 | 10:30-12:30 | 30.00 | 94.31 | 3.14 | YES | YES | N/A | 15.30 |
| 2026-06-23 | 13:55 | Double Top | loser | 82 | 81.15 | 50 | PASS | 19.02 | 60.32 | 39.68 | 21.30 | 12:30-14:30 | 30.00 | 81.15 | 2.71 | NO | NO | N/A | 4.85 |
| 2026-06-25 | 13:10 | Double Top Breakdown | loser | 88 | 80.25 | 45 | PASS | 20.68 | 73.16 | 15.50 | 80.00 | 12:30-14:30 | 30.00 | 80.25 | 2.67 | YES | NO | N/A | 42.20 |
| 2026-06-25 | 13:25 | Double Top | winner | 82 | 89.00 | 45 | PASS | 21.86 | 52.46 | 36.27 | 0.00 | 12:30-14:30 | 30.00 | 89.00 | 2.97 | NO | NO | N/A | 92.05 |
| 2026-06-25 | 14:40 | Double Top Breakdown | loser | 88 | 93.01 | 45 | PASS | 23.25 | 70.99 | 26.53 | 80.00 | 14:30-15:30 | 30.00 | 93.01 | 3.10 | YES | NO | N/A | 31.75 |

## Winners vs Losers

| Metric | Winners | Losers |
|---|---:|---:|
| Avg confidence | 85.0 | 86.5 |
| Avg projected move | 79.65 | 87.18 |
| Avg trend strength | 47.5 | 47.5 |
| Avg ATR14 | 19.71 | 21.63 |
| Avg candle body % | 52.39 | 73.34 |
| Avg largest wick % | 33.64 | 22.52 |
| Avg pattern score | 40.0 | 64.6 |
| Avg stop distance | 29.05 | 30.0 |
| Avg target distance | 79.65 | 87.18 |
| Avg risk reward | 2.74 | 2.91 |
| EMA aligned count | 2 | 4 |
| Neckline break count | 1 | 3 |
| Retest confirmed count | 1 | 1 |
| Volume confirmed count | 0 | 0 |

### What exists in winners but not in losers
- Winner patterns: {'Double Top Breakdown': 1, 'Double Top': 1}
- Loser patterns: {'Double Top Breakdown': 3, 'Double Top': 1}
- Winner time buckets: {'10:30-12:30': 1, '12:30-14:30': 1}
- Loser time buckets: {'10:30-12:30': 1, '12:30-14:30': 2, '14:30-15:30': 1}

## Single-Filter Tests

| Filter | Trades Remaining | Winners | Losers | Win Rate | Profit Factor |
|---|---:|---:|---:|---:|---:|
| confidence >= 85 | 4 | 1 | 3 | 25.0% | 0.78 |
| confidence >= 90 | 0 | 0 | 0 | n/a | n/a |
| projected_move >= 90 | 2 | 0 | 2 | 0.0% | 0.00 |
| trend_strength >= 80 | 0 | 0 | 0 | n/a | n/a |
| trend_strength >= 90 | 0 | 0 | 0 | n/a | n/a |
| risk_reward >= 1.5 | 6 | 2 | 4 | 33.3% | 1.33 |
| neckline break confirmation | 4 | 1 | 3 | 25.0% | 0.78 |
| retest confirmation before entry | 2 | 1 | 1 | 50.0% | 2.34 |
| volume confirmation if available | 0 | 0 | 0 | n/a | n/a |

## High Accuracy Mode Search

- Best restrictive combo found on the 6 decided trades: `retest`
- Trades kept: 2
- Winners: 1
- Losers: 1
- Win rate: 50.0%
- Profit factor: 2.34

## Recommended High Accuracy Mode

### Double Top
- Current sample does not justify a more aggressive plain `Double Top` entry rule.
- In high-accuracy mode, keep plain `Double Top` as review/watch unless a cleaner breakdown state is already confirmed.
- This reduces trade frequency and avoids promoting a pattern that is split in the current decided sample.

### Double Top Breakdown
- Do not raise confidence to 85 or 90 based on this sample; both filters reduce quality.
- Do not raise projected move to 90; in this replay slice it removes winners and keeps only losers.
- Keep the current confidence >= 80 and projected move >= 70 floors.
- Keep EMA alignment, neckline breakdown confirmation, valid stop, and risk-reward >= 1.5.
- Add retest confirmation before entry for high-accuracy mode; it is the only tested filter that improved both win rate and profit factor on the current decided trades.
- Use volume confirmation only as an enhancer when candle volume is present; it cannot be a core gate on this sample because volume confirmation was unavailable.

## Notes
- This report is evidence only and does not change production logic.
- Retest confirmation here is reconstructed from historical candles because the current replay path does not store a dedicated retest flag.
- Volume confirmation uses the current project’s 20-bar volume ratio rule: breakout confirmed at 1.35x or higher.
