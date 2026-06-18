# Run The NIFTY50 App

## 1. One-click live mode

Start the live server:

```bash
python3 /Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/serve_nifty50_app.py
```

Then open:

[http://127.0.0.1:8000/nifty50_ai_prediction_console.html](http://127.0.0.1:8000/nifty50_ai_prediction_console.html)

Each time you click one of the prediction buttons, the app now:

1. downloads the latest market inputs
2. refreshes the latest news and world signals
3. rebuilds `market_snapshot.latest.json`
4. runs the prediction on that fresh snapshot

The UI now has two direct actions:

- `Predict Tomorrow`: next-session forecast after the latest official close
- `Live Market View`: current live/pre-open/post-close bias using the latest real signals

The live screen now also includes:

- a real intraday NIFTY chart with `1m`, `5m`, `15m`, and `1h` timeframe switching
- a rules-based `next candle` action card with `BUY BIAS`, `SELL BIAS`, or `WAIT`
- a component-stock dashboard with a `RELIANCE / HDFCBANK / ICICIBANK` heavyweight watchlist
- real `Top 10 Bullish Stocks` and `Top 10 Bearish Stocks` inside the current NIFTY 50 universe

The lower half of the app now shows the latest `24h` geopolitical and India-market news headlines used by the model.

The app is session-aware:

- before `9:15 AM IST`, the overnight inputs are treated as a forecast for `today's` session
- during live cash-market hours, the daily block still uses the latest completed close while the intraday chart below provides the live minute-candle action bias
- after the official daily close is available, it treats the run as a forecast for the `next` session

## 2. Manual input refresh

If you want to refresh the raw files yourself, run:

```bash
python3 /Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/download_real_market_inputs.py
```

This writes the required files into:

`/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/inputs`

Expected names:

- `nifty50_history.csv`
- `india_vix.csv`
- `fii_dii.csv`
- `gift_nifty.csv`
- `usdinr.csv`
- `world_signals.json`

The downloader also auto-generates:

- `nifty50_intraday.json`
  Real minute candles for the live chart and next-candle action panel.
- `nifty50_components_live.json`
  Real NIFTY 50 component-stock dashboard data for the bullish / bearish leaderboard and heavyweight cards.

See:

[inputs/README.md](/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/inputs/README.md)

If you want to rebuild only `world_signals.json` from live sources:

```bash
python3 /Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/build_world_signals.py
```

If you also have NewsAPI access, add `NEWSAPI_KEY=your_key_here`.
If you have FRED access, add `FRED_API_KEY=your_key_here` as an optional backup for market-series fetches.

## 3. Build the normalized snapshot manually

Run:

```bash
python3 /Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/collect_nifty50_market_data.py
```

This writes:

`/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/market_snapshot.latest.json`

If any required input is missing or invalid, the collector exits with an error and does not write a snapshot.

## 4. Open the app manually

Alternative:

You can also open the HTML file directly from disk and then click `Load Snapshot JSON` inside the app to choose:

`/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/market_snapshot.latest.json`

## Notes

- There is no sample-data fallback anymore.
- The one-click refresh path requires `serve_nifty50_app.py`. A plain `python3 -m http.server` cannot run the downloader or collector.
- The HTML app now refuses to run unless `market_snapshot.latest.json` exists and is marked as real data.
- If the page is opened with `file://`, the browser cannot auto-refresh live data. In that case, use the in-app `Load Snapshot JSON` button or run `serve_nifty50_app.py`.
- The HTML app uses the normalized snapshot and a rules-based forecast instead of calling an LLM directly from the browser.
- The downloader currently uses live NSE, NSE report-detail, NSE page header, Google News RSS, and Yahoo Finance endpoints.
- The live chart uses Yahoo Finance `^NSEI` minute candles, and the stock dashboard uses the official Nifty Indices constituent CSV plus Yahoo Finance per-stock 5 minute charts.
- This is a research starter, not a production trading engine.
