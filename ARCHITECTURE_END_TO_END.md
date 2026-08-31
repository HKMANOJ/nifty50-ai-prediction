# Complete End-to-End System Architecture

**Platform:** manojLabs AI Stock Hunter V2 (Nifty50 AI Prediction Engine)  
**Version:** 2.0.0 (Institutional Real-Time Release)  
**Author:** Manoj Labs  
**Target Environment:** Local macOS / Linux / Cloud Deployment

---

## 1. Architectural Overview & System Topology

The platform is designed as a **low-latency, multi-tiered institutional trading intelligence engine**. It combines real-time data ingestion from the National Stock Exchange of India (NSE), derivatives flow analysis, automated pattern recognition, and bi-directional WebSocket push streaming to deliver sub-second breakout alerts and chart updates.

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       SYSTEM ARCHITECTURE TOPOLOGY                                     │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                                        │
│  [ EXTERNAL MARKET DATA ]                                                                              │
│  ├── Official NSE India Public API (Equities, F&O Variations, Index Ticks, Option Chain)               │
│  ├── Yahoo Finance (Historical 5-Day 5-Minute OHLCV Candles)                                           │
│  └── Global Macro Inputs (Gift Nifty, India VIX, Crude Oil, USD/INR, FII/DII Net Flow)                 │
│                                                                                                        │
│                                           │ (HTTPS / Session Handshake)                                │
│                                           ▼                                                            │
│                                                                                                        │
│  [ DATA INGESTION & PIPELINE LAYER ]                                                                   │
│  ├── stock_suggestion_index.py        (200+ F&O Universe Scanner & ORB Calculator)                     │
│  ├── download_real_market_inputs.py   (Macro, Global Signals & Historical CSVs)                        │
│  ├── collect_nifty50_market_data.py   (Real-time Nifty 50 Ticks & Order Flow)                          │
│  └── fetch_most_active_nse.py         (High Volume / High OI Strikes)                                  │
│                                                                                                        │
│                                           │ (JSON Cache / In-Memory State)                             │
│                                           ▼                                                            │
│                                                                                                        │
│  [ ANALYTICS & INTELLIGENCE ENGINES ]                                                                  │
│  ├── 4-Gate Disciplined Trader Filter (Breakout + RVOL ≥ 2x + Long Buildup + 85% Range Hold)           │
│  ├── Sector Relative Strength (RS)    (12 Sector Themes, Tailwind/Headwind Matrix)                     │
│  ├── Pattern Recognition Suite        (18+ Engines: Bull Flag, Head & Shoulders, Wedges, etc.)         │
│  ├── Market Structure & Liquidity     (BOS, CHoCH, Order Blocks, Liquidity Sweeps, FVGs)               │
│  ├── Options Intelligence Engine      (Live Chain, PCR, Max Pain, Volume / OI Buildup)                 │
│  └── Prediction Ensemble Engine       (Multi-Model Weighted Directional Probabilities)                 │
│                                                                                                        │
│                                           │                                                            │
│                     ┌─────────────────────┴─────────────────────┐                                      │
│                     ▼                                           ▼                                      │
│                                                                                                        │
│  [ APPLICATION SERVER (Port 8001) ]                   [ WEBSOCKET ENGINE (Port 8765) ]                 │
│  ├── ThreadingHTTPServer (serve_nifty50_app.py)       ├── Asyncio WebSocket (websocket_server.py)      │
│  ├── REST Endpoints:                                  ├── Push Channel 1: Live 5m Candle Streaming     │
│  │   ├── /api/stock_suggestions                       ├── Push Channel 2: 5-min Auto-Scan Broadcast    │
│  │   ├── /api/stock_candles (OHLCV + EMA + PMH)       ├── Push Channel 3: Official NSE Index Ticks     │
│  │   ├── /api/index_live (Direct NSE Ticker)          └── Subscription Router (Watchlist Dispatcher)   │
│  │   └── /api/ai_options & /api/accuracy              │                                                │
│  └── Autonomous 5-Min Background Auto-Scanner         │                                                │
│                                                       │                                                │
│                     │                                 │ (< 5ms Push)                                   │
│                     ▼                                 ▼                                                │
│                                                                                                        │
│  [ PRESENTATION TIER (Browser Client) ]                                                                │
│  └── MLAIStockV2.html (Responsive Desktop / Tablet / Mobile UI)                                        │
│      ├── TradingView Lightweight Charts v4.2.1 (Live Painting Candles, PMH/PML Blue Rails)             │
│      ├── ⚡ System Top Pick Card (Single Conviction Setup / Default Blank State)                       │
│      ├── 📊 Sector Analysis Workspace (Heatmap Tiles, Relative Strength Bars, Theme Alignment Matrix)   │
│      ├── High RS Breakout Stock Waves (Bullish / Bearish Sortable Desks)                               │
│      └── Live Real-Time Tickers & Status Telemetry (WS Live, Auto-Scan 5m Active, Clock IST)          │
│                                                                                                        │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Layer-by-Layer Technical Specifications

### Layer 1: Data Ingestion & Network Transport

The application ingests live data without relying on paid enterprise feeds by utilizing authenticated session handshakes with official exchange endpoints:

1. **Exchange Session Warmup & Cookie Jar:**
   - Script: `stock_suggestion_index.py` & `serve_nifty50_app.py`
   - Mechanism: Bypasses automated scraping blocks by sending an initial GET request to `https://www.nseindia.com` with browser emulation headers (`User-Agent`, `Accept-Language`, `Referer`). Cookies are captured into an in-memory session or temporary cookie jar (`inputs/.nse_cookies.txt`).
2. **Official NSE Index Feed (`/api/allIndices`):**
   - Fetches real-time valuations for `NIFTY 50` and `NIFTY BANK`.
   - Returns Last Traded Price (LTP), Previous Close, Absolute Change, and Percentage Change.
   - Eliminates math discrepancies seen in third-party feeds (e.g. Yahoo Finance adjusted baselines).
3. **F&O Variation Feed (`/api/live-analysis-variations`):**
   - Extracts top gainers, top losers, and volume variations across all 200+ NSE F&O contracts.
4. **Historical Intraday Candles (`YAHOO_CHART_BASE`):**
   - Retrieves 5-day historical 5-minute OHLCV candle arrays for charting and Opening Range calculations.

---

### Layer 2: Analytics, Mathematical Models & Decision Engine

#### 1. The Disciplined Trader 4-Gate Filter (Max 1–2 Trades/Day)
Located in `MLAIStockV2.html` and `stock_suggestion_index.py`:

```
Input: 200+ F&O Stocks
   │
   ├── Gate 1: is_breakout === true
   │   (5m candle close ABOVE morning opening range high / PMH)
   │
   ├── Gate 2: vc_ranking (RVOL) >= 2.0x
   │   (Volume is at least 200% of 20-day moving average)
   │
   ├── Gate 3: setup === "Long buildup"
   │   (Price UP + Open Interest UP; filters out short-covering traps)
   │
   └── Gate 4: range_position_percent >= 85%
       (Price holding top 15% of daily range: (LTP - Low) / (High - Low) >= 0.85)
   │
   ▼
Output:
   ├── If all 4 pass: ⚡ SYSTEM TOP PICK (High Conviction CE Buy)
   └── If any fail:   BLANK STATE (🔍 "No high-conviction setup right now")
```

#### 2. Sector Relative Strength (RS) & Theme Alignment
Located in `MLAIStockV2.html` (`SECTOR_MAP` + `renderSectorView`):
- Maps 200+ symbols to 12 sectors (`IT`, `Banks`, `Auto`, `Pharma`, `Metal`, `Energy`, `EV/Energy`, `Infra`, `Telecom`, `Chemicals`, `Capital Goods`, `Internet`).
- Computes average sector momentum:
  $$\text{Sector Avg \%} = \frac{1}{N} \sum_{i=1}^N \text{Stock}_i \text{ \% Change}$$
- **Theme Alignment Matrix:** Evaluates stock direction against sector tailwind:
  - Stock $\uparrow$ + Sector $\uparrow$ + RVOL $\ge 2\text{x}$ $\rightarrow$ **⚡ High Conviction CE Buy**
  - Stock $\uparrow$ + Sector $\downarrow$ $\rightarrow$ **⚠️ Weak Sector — Avoid** (Divergence trap)
  - Stock $\downarrow$ + Sector $\downarrow$ $\rightarrow$ **📉 High Conviction PE Buy**

#### 3. Pattern Recognition Suite (`patterns/`)
Contains 18+ algorithmic pattern engines that inspect price action:
- `bull_flag.py` / `bear_flag.py`: Flags and consolidations.
- `head_shoulders.py` / `inverse_head_shoulders.py`: Reversal structures.
- `ascending_triangle.py` / `descending_triangle.py`: Symmetrical and asymmetrical breakouts.
- `hammer.py` / `inverted_hammer.py` / `bullish_engulfing.py`: Candlestick reversal patterns.
- `fibonacci_engine.py`: Automatic swing high/low 0.382, 0.5, 0.618 golden pocket retracements.
- `swing_detector.py`: Structural pivots.

---

### Layer 3: Application Server & Real-Time WebSocket Tier

#### 1. HTTP Application Server (`serve_nifty50_app.py`)
- **Port:** `8001` (Threaded `ThreadingHTTPServer`)
- **Routing Dispatcher:**
  - `GET /`: Redirects to `/MLAIStockV2.html`.
  - `GET /api/stock_suggestions`: Serves current 200-stock rankings, top pick, and auto-scan telemetry.
  - `GET /api/stock_candles?symbol=XYZ`: Computes 5m candles, 9 EMA, Prior Day High (PMH), and Prior Day Low (PML).
  - `GET /api/index_live`: Direct official NSE index feed.
  - `POST /api/stock_suggestions/refresh`: Manual forced live rescan.
- **Autonomous 5-Minute Background Auto-Scanner:**
  - Dedicated background daemon thread (`start_background_auto_scanner`).
  - Active strictly during Indian market hours (**09:15 to 15:30 IST, Monday to Friday**).
  - Acquires `REFRESH_LOCK` to prevent race conditions with manual user refreshes.
  - Automatically triggers WebSocket broadcast upon completion.

#### 2. WebSocket Push Server (`websocket_server.py`)
- **Port:** `8765` (`ws://127.0.0.1:8765`)
- **Architecture:** Asynchronous event loop (`asyncio` + `websockets` RFC 6455).
- **Client Watchlist Tracking:** Maps each connected client socket to its actively inspected stock symbol (`CLIENT_WATCHLIST[ws] = "ATHERENERG"`).
- **Channels:**
  - `candle_update`: Pushes latest 5m OHLCV bar to subscribers every 15s.
  - `scanner_update`: Broadcasts full scan payload to all connected clients the instant a 5-min scan completes.
  - `index_update`: Broadcasts live NIFTY / BANK NIFTY quotes.

---

### Layer 4: Presentation & UI Tier (`MLAIStockV2.html`)

- **Design System:** Clean, modern institutional layout (`manojLabs` branding, dark navy sidebar, crisp white canvas, emerald green bullish / rose red bearish accents).
- **Responsive Navigation:** Desktop sidebar + mobile/tablet sliding drawer with 3-line hamburger toggle.
- **TradingView Lightweight Charts v4.2.1 Integration:**
  - Local standalone script (`lightweight-charts.standalone.js`).
  - Native real-time bar updates via `_candleSeries.update(candle)`.
  - Persistent blue price line rails for Prior Day High (**PMH**) and Prior Day Low (**PML**).
  - 5-Day continuous historical scrolling with zoom and fit controls.
- **WebSocket Client Engine:**
  - Auto-subscribes upon stock selection.
  - Self-healing auto-reconnection loop (retries every 2.5s if socket disconnects).
  - Status badges: `● WS: Push Live`, `Auto-Scan: 5m Active`, `● Live Real-Time Data`.

---

## 3. End-to-End Data Flow Sequence

The diagram below traces the exact journey of a trade signal from the exchange floor to your screen:

```
NSE India Exchange
       │
       ▼ (Every 5 minutes at candle close, e.g. 10:05:00 AM)
[serve_nifty50_app.py: Background Auto-Scanner Thread]
       │
       ▼ (Executes stock_suggestion_index.py)
[Fetches 200+ F&O Variations + OI + Candles]
       │
       ▼ (Evaluates 4 Institutional Gates)
       │  • Breakout above morning high? YES
       │  • RVOL >= 2.0x? YES (Ather Energy: 4.19x / Coforge: 2.0x)
       │  • Setup == Long Buildup? YES
       │  • Range Position >= 85%? YES (98.5%)
       │
       ▼ (Saves to inputs/stock_suggestion_index.latest.json)
[Calls broadcast_scanner_update_sync(payload)]
       │
       ▼ (< 5ms WebSocket Broadcast)
[ws://127.0.0.1:8765 WebSocket Engine]
       │
       ▼ (Pushed over open TCP Socket)
[Chrome Browser: MLAIStockV2.html ws.onmessage]
       │
       ├── 1. renderAll(payload)
       │      ⚡ SYSTEM TOP PICK lights up green: "COFORGE 09:30 AM Breakout"
       │
       └── 2. _candleSeries.update(candle)
              TradingView chart paints the live breakout candle above the PMH line
              without any page refresh or mouse clicks!
```

---

## 4. Complete Project Directory & File Manifest

| File / Directory | Layer | Purpose |
| :--- | :--- | :--- |
| **`MLAIStockV2.html`** | Frontend | Primary modern trading console (TradingView charts, Top Pick card, Sector Analysis). |
| **`serve_nifty50_app.py`** | Server | Threaded HTTP server, REST endpoints, market-hours gate, and 5-min auto-scanner daemon. |
| **`websocket_server.py`** | Server | Dedicated RFC 6455 WebSocket push server on port 8765 streaming live candles & broadcasts. |
| **`stock_suggestion_index.py`** | Ingestion / Logic | F&O market scanner, ORB calculation, RVOL, Long Buildup classification, 4-gate filter. |
| **`lightweight-charts.standalone.js`** | Frontend Lib | Local TradingView charting library v4.2.1. |
| **`patterns/`** | Intelligence | 18+ technical chart pattern detection modules (flags, triangles, wedges, candles, etc.). |
| **`market_structure_engine.py`** | Intelligence | Smart money structure detector (BOS, CHoCH, Swing highs/lows). |
| **`liquidity_engine.py`** | Intelligence | Liquidity sweeps, Order blocks, Fair Value Gaps (FVG). |
| **`options_engine.py`** | Intelligence | Option chain analysis, PCR, max pain, open interest shifts. |
| **`prediction_engine.py`** | Intelligence | Multi-model ensemble combining technicals, options, and macro sentiment into Call/Put probabilities. |
| **`download_real_market_inputs.py`** | Ingestion | Downloads global macro data (Gift Nifty, India VIX, Crude Oil, USD/INR, FII/DII). |
| **`collect_nifty50_market_data.py`**| Ingestion | Real-time tick collector for Nifty 50. |
| **`inputs/`** | Data Storage | Latest JSON snapshots (`stock_suggestion_index.latest.json`, `most_active.latest.json`). |
| **`store_candles_mysql.py`** | Database | MySQL candle storage adapter. |
| **`fetch_candles_mysql.py`** | Database | MySQL candle retrieval adapter. |
| **`ai_options_mysql.py`** | Database | Audit logging for prediction recommendations and trade outcomes. |
| **`learning_engine.py`** | Learning | Post-trade accuracy reconciler and weight tuning. |

---

## 5. Operations & How to Run

### Starting the Platform:
To launch both the HTTP Application Server and the WebSocket Push Server simultaneously:

```bash
cd /Users/hkmanoj/Downloads/nifty50-ai-prediction
source venv/bin/activate
python3 serve_nifty50_app.py --port 8001
```

### Accessing the Dashboard:
Open your browser to:
👉 **`http://127.0.0.1:8001/MLAIStockV2.html`**

### Verifying Service Health:
- **HTTP Server:** `curl http://127.0.0.1:8001/api/health` $\rightarrow$ `{"ok": true, "service": "stock-suggestion-live-server"}`
- **WebSocket Engine:** `ws://127.0.0.1:8765` $\rightarrow$ Sends welcome payload with status `online`.
- **Top Bar Badges:**
  - `● WS: Push Live` (Green)
  - `Auto-Scan: 5m Active` (Blue during market hours, Grey outside market hours)
  - `NIFTY 50` & `BANK NIFTY` (Real-time official NSE quotes matching Groww/Zerodha)
