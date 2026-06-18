# NIFTY 50 Live Deployment

This app can be pushed to GitHub, but the 1-minute real-data refresh needs a running Python backend.

## What GitHub Can Host

GitHub Pages can host:

- `nifty50_ai_prediction_console.html`
- `market_snapshot.latest.json`
- static files in this folder

GitHub Pages cannot run:

- `serve_nifty50_app.py`
- `/api/refresh`
- live NSE/Yahoo/news downloads every 1 minute

## Required Live Backend

Run the Python server on a backend host such as Render, Railway, Fly.io, a VPS, or your own machine:

```bash
python3 serve_nifty50_app.py --host 0.0.0.0 --port 8000
```

Most cloud platforms provide `PORT` automatically, so this also works:

```bash
HOST=0.0.0.0 python3 serve_nifty50_app.py
```

The frontend calls:

- `GET /market_snapshot.latest.json`
- `POST /api/refresh`

## Connect GitHub Page To Backend

Open the GitHub Pages URL with your backend server:

```text
https://YOUR_GITHUB_USER.github.io/YOUR_REPO/nifty50_ai_prediction_console.html?server=https://YOUR_BACKEND_URL
```

The app stores that backend URL in the browser and then refreshes live data every 60 seconds.

## Important

The output is a rules-based probability read from live candles, breadth, market data, and news. It is not guaranteed trading advice. Use the entry, stop loss, and invalidation levels rather than treating the 50-minute target as certain.
