# Nifty50 Ranking & Scoring Methodology

This document captures the current logic used in the `nifty50Application` branch and the recommended improvements to make the ranking more reliable for local testing.

## 1. Current ranking logic

The ranking is implemented in `stock_suggestion_index.py`.

### Main idea
The system does not rank stocks purely by percentage gain/loss.
It first filters candidates and then ranks only the valid ones.

### Current order of checks
1. Pull live data from NSE:
   - Top gainers / losers
   - Change in Open Interest (OI)
   - Index context (NIFTY / BANKNIFTY)
   - 5-minute candles
2. Filter invalid candidates
3. Calculate performance score
4. Evaluate breakout quality
5. Apply PMH/PML validation
6. Sort valid candidates by move strength

---

## 2. Layer 1: Independent Universe & Liquidity Filters

The system screens the **entire NSE F&O universe** independently, rather than relying on external Top 20 tables:

- **Source of Truth**: Official NSE F&O Master list (`inputs/fo_lot_sizes.csv`) with ~205 underlyings.
- **Lot Size Gate**: `MAX_ALLOWED_LOT_SIZE = 650` (filters out giant illiquid contracts, keeping liquid stocks).
- **No Arbitrary 20-Row Cutoff**: The scanner inspects all qualified F&O securities. A stock down -2.5% will never vanish just because other stocks pushed it to rank 21 on NSE.
- **Decoupled OI Requirement**: OI data enriches the score (+20 pts for buildup/liquidation), but missing an external "OI Spurt" flag no longer eliminates a high-volume stock.
- **Index Exclusions**: Index symbols (`NIFTY`, `BANKNIFTY`, `FINNIFTY`) are excluded from stock lists.
- **Liquidity Filter**: `MIN_VC_RANKING = 0.5` (Volume Confluence / RVOL minimum).
- **Minimum Move Threshold**: `MIN_BULLISH_CHANGE = +0.65%`, `MIN_BEARISH_CHANGE = -0.65%`.

---

## 3. Current score model

The current one-side rally score is built in `assess_one_side_rally()`.

### Components

| Component | Max points |
|---|---:|
| Volume / RVOL buildup | 25 |
| Day range position / upper/lower range hold | 25 |
| Move from open | 20 |
| OI setup & squeeze potential | 20 |
| Price momentum | 10 |
| Index context | 8 |

Total: 108, capped at 100.

### Score logic

- Price change is used heavily
- OI setup matters a lot
- Range position matters a lot
- Index context provides support / resistance adjustment
- Volume is a major filter

### Current status mapping

- `score >= 75` -> `Priority Rally`
- `score >= 65` -> `Rally Watch`
- otherwise -> `Filtered`

---

## 4. Breakout logic

The scanner uses 5-minute candles to detect breakout structure.

### Key idea
- The first candle sets the opening range
- Bullish breakout = price breaks above opening-range high and holds
- Bearish breakdown = price breaks below opening-range low and holds
- EMA support is also considered

### Breakout conditions
For bullish:
- latest close >= morning high threshold
- latest close >= EMA9
- trend should not fail by falling back below the range floor

For bearish:
- latest close <= morning low threshold
- latest close <= EMA9
- trend should not fail by bouncing above the range ceiling

### PMH / PML validation
This filters out weak-looking moves:
- bullish stocks must remain above the morning-zone high threshold
- bearish stocks must remain below the morning-zone low threshold
- also required to remain above/below previous close depending on side

This prevents ranking a stock that only looks good on paper.

### 4.1 Two-Tier 3-Bar Impulse Cascade (Approach A)
To eliminate wick traps and false breakouts:
- **Tier 1 (Foundation Readiness)**: Evaluates volume, OI setup, day range position, and move from open.
- **Tier 2 (Micro Execution Trigger)**: Evaluates 3 consecutive 5-minute candles:
  - Bearish: 3 consecutive Red candles, consecutive Lower Lows, average body $\ge 60\%$, closed below PML, volume ratio $\ge 0.70\times$. Triggers `⚡ Active Waterfall`.
  - Bullish: 3 consecutive Green candles, consecutive Higher Highs, average body $\ge 60\%$, closed above PMH, volume ratio $\ge 0.70\times$. Triggers `⚡ Impulse Breakout`.
- **Score Boost**: Awards $+15$ points and promotes confirmed candidates to `Priority Rally` with immediate dashboard bell notification.

---

## 5. Final ranking logic

After score and breakout validation, the final ordering is:

- Bullish side: sort by percent change descending
- Bearish side: sort by percent change ascending

This means:
- strongest gainers first on bullish side
- strongest losers first on bearish side

In other words:

`Filter first -> score second -> rank by percent move last`

---

## 6. What is missing / what should be improved

The project is already good, but it can be improved with two important additions:

1. Money flow participation
2. 1-minute candle confirmation in background

### Why add these?
Because price + OI + volume alone can still create false positives during noisy intraday moves.

### Best approach
- Keep 5-minute candles as the main ranking engine
- Use 1-minute candles as a micro-confirmation layer
- Use money flow / participation as a quality layer

---

## 7. Recommended improved score system

The improved score should be a hybrid score, not a pure 5-minute score.

### Recommended final formula

`final_score = breakout_score + range_score + volume_score + oi_score + momentum_score + index_context_score + money_flow_score + micro_confirmation_score + sector_score - penalties`

### Proposed weights

| Factor | Suggested points |
|---|---:|
| 5m breakout structure | 25 |
| Range / day position | 15 |
| Volume / RVOL | 15 |
| OI setup | 10 |
| Price momentum | 10 |
| Index context | 8 |
| Money flow / participation | 10 |
| 1m confirmation | 5 |
| Sector breadth | 2 |
| Risk penalties | subtract |

Total should be capped at 100.

### Recommended score bands

| Score | Status |
|---|---|
| 80+ | Priority Rally |
| 70–79 | Rally Watch |
| 60–69 | Watchlist |
| Below 60 | Filtered |

### Important rule
If 5m breakout is invalid, do not allow a high score even if 1m looks good.

---

## 8. Money flow layer

This is very important for intraday and F&O ranking.

### Good money-flow inputs
- sector breadth
- participation strength
- relative volume
- cash market flow proxy
- options flow aggression / CE-PE behavior
- institutional participation proxy

### How to use it
- if sector is strong and stock is leading the sector, boost score
- if sector is weak or opposite, reduce score
- if 1m confirms the move but money flow is weak, keep ranking lower

This helps separate a real breakout from a temporary one-off move.

---

## 9. 1-minute confirmation layer

This should run in the background while the 5-minute ranking remains the primary engine.

### Recommended logic
- 5m decides if the trend is valid
- 1m checks if the move is continuing
- if 1m confirms the 5m move, add a small boost
- if 1m reverses against the 5m move, reduce confidence

### Example
- 5m bullish + 1m bullish = strong confirmation
- 5m bullish + 1m weak or sideways = moderate confidence
- 5m bullish + 1m bearish = reduce score / reject or delay ranking

This is better than letting short-term 1m noise dominate the ranking.

---

## 10. Risk penalties to add

These are essential for quality:

- failed breakout: -15
- late breakout: -10
- weak support after breakout: -10
- 1m reversal against 5m: -10
- weak money flow: -10
- low liquidity: -5
- poor sector breadth: -5

This prevents the scorer from overrating noisy or weak trades.

---

## 11. Recommended project structure for development

To keep the code maintainable, split functionality into these modules:

- `data_fetchers.py`
  - NSE top gainers/losers
  - OI / options data
  - index context
  - 5m and 1m candles
  - lot size data

- `feature_engine.py`
  - percent change
  - range position
  - breakout logic
  - pullback logic
  - move-from-open

- `money_flow_engine.py`
  - participation score
  - sector money flow
  - institutional proxy

- `sector_engine.py`
  - sector alignment
  - relative strength
  - breadth checks

- `score_engine.py`
  - combine all scores
  - apply risk penalties
  - map to status

- `ranking_engine.py`
  - filter candidates
  - sort by score + movement
  - assign rank

- `risk_engine.py`
  - failed breakout detection
  - late-day penalties
  - reversal protection

- `payload_builder.py`
  - final JSON payload
  - UI-friendly output

---

## 12. Development plan for local testing

### Phase 1
Implement and validate:
- 5m breakout detection
- range position
- volume / RVOL
- OI setup mapping
- current ranking order

### Phase 2
Add:
- 1m confirmation check
- micro-trend score
- background 1m check while keeping 5m as primary

### Phase 3
Add:
- money flow score
- sector breadth
- relative strength
- risk penalties

### Phase 4
Tune:
- thresholds
- score weights
- risk penalties
- final status transitions

### Phase 5
Local validation:
- run multiple scans
- compare old vs new ranking
- check false positives on noisy moves
- test breakout behavior across different times of the day

---

## 13. Local test checklist

Use this checklist in IntelliJ / local machine tomorrow:

- [ ] Build fresh payload from current repo
- [ ] Confirm 5m breakout detection works
- [ ] Confirm OI filter works
- [ ] Confirm volume threshold works
- [ ] Confirm PMH/PML filter works
- [ ] Confirm rank order is correct
- [ ] Add 1m confirmation in background and verify no noise explosion
- [ ] Add money flow factor and verify score changes
- [ ] Add sector breadth factor and verify quality improvement
- [ ] Check that weak breakouts are filtered correctly
- [ ] Ensure 5m remains the primary engine
- [ ] Ensure 1m does not dominate ranking

---

## 14. Final takeaway

The current system is already strong because it combines:
- real market data
- breakout structure
- volume strength
- OI logic
- index support

The best next enhancement is:

- keep 5-minute as the backbone
- add 1-minute as confirmation
- add money flow as a real-money validation factor
- add sector breadth and risk penalties
- keep final rank driven by a weighted score, not just percentage move

This will significantly improve signal quality and reduce false positives.

---

## 15. Recommended next step

Implement the improved scoring in the following order:

1. 5m + 1m confirmation
2. money flow score
3. sector breadth score
4. risk penalties
5. final score and ranking update

Once this is in place, the ranking should become more stable and more tradeable in live intraday usage.

This document is for local testing and tuning in IntelliJ before finalizing the upgrade.
