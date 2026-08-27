# POC Architecture Specification: Clean Modular Integration of High-Accuracy Institutional Filters

**Document Version:** 1.0.0  
**Target File:** `POC_ENHANCEMENTS_SPECIFICATION.md`  
**Workspace:** `/Users/hkmanoj/Downloads/nifty50-ai-prediction`  
**Core Objective:** Implement **Candle Body-to-Wick Ratio**, **Intraday VWAP Distance**, and **Sectoral Heatmap Analysis** without creating any visual clutter or overloading the main dashboard.

---

## 1. Executive Design Philosophy: "Clean Navigation, Zero Clutter"

To maintain the pristine, institutional ergonomics of the Vardhaan Capital terminal, we **will not cram extra widgets into the existing screen**. 

Instead, we use a **modular dual-layer architecture**:
1. **Dedicated Navigation Views (Sidebar Tabs):** The existing inactive sidebar tabs (`Sector Analysis` and `Breakout Hunter`) are transformed into dedicated, focused workstations.
2. **Minimalist Inline Micro-Badges:** On the current `Futures & Options` screen, we only add compact 2-word tags (`Solid 82%`, `Near VWAP +0.6%`) that give immediate confirmation without adding visual noise.

```
+─────────────────────────────────────────────────────────────────────────────────────────────+
|                                    SIDEBAR NAVIGATION                                       |
+─────────────────────────────────────────────────────────────────────────────────────────────+
| [📈 Futures & Options]    ───► Main Workstation (Uncluttered, with subtle micro-tags)       |
| [🎯 Breakout Hunter]      ───► Dedicated Zero-Fakeout View (Body-to-Wick + VWAP Zones)      |
| [📊 Sector Analysis]      ───► Dedicated Sector Heatmap (Theme Tailwinds & Capital Flow)    |
+─────────────────────────────────────────────────────────────────────────────────────────────+
```

---

## 2. What Changes on the Screens (Visual Layout)

### Screen A: Existing `Futures & Options` (Main View) — *Minimal Subtle Enhancement*
The main screen remains clean and familiar. Only two subtle, high-value indicators are added to the existing table and chart header:

1. **In the Table (Column: `5 MIN BO/BD`):**
   * *Before:* `Breakout 10:25 AM`
   * *After:* 
     ```
     [ Breakout 10:25 AM ]
     [ Solid Body 82% | VWAP +0.7% ]
     ```
   * *Benefit:* At a single glance, you immediately know if the candle is a solid institutional body or a weak wick trap, and whether it is within the safe VWAP entry zone.

2. **In the Chart Header (Right Panel):**
   * A single new chip is added next to `Structure: Breakout Rally`:
     `[ 🎯 Quality: Solid Body (84%) | VWAP: Sweet Spot (+0.68%) ]`

---

### Screen B: Dedicated `🎯 Breakout Hunter` (New Tab View)
Clicking **`Breakout Hunter`** in the left sidebar opens a specialized **Zero-Fakeout Confirmation Console**:

```
+─────────────────────────────────────────────────────────────────────────────────────────────+
|  TOP BAR: Breakout Quality Matrix (Solid Body >= 65%  |  VWAP Distance <= 1.5%)             |
+─────────────────────────────────────────────────────────────────────────────────────────────+
|  LEFT PANEL: Confirmed True Breakouts (Zero-Wick Filtered)                                  |
|  ─────────────────────────────────────────────────────────                                  |
|  #1 KAYNES    Solid Body: 84%   VWAP Dist: +0.68% (Sweet Spot)  Status: Confirmed BO        |
|  #2 CDSL      Solid Body: 76%   VWAP Dist: +0.82% (Sweet Spot)  Status: Confirmed BO        |
|  #3 HINDZINC  Solid Body: 71%   VWAP Dist: +1.10% (Safe Entry)  Status: Testing Resistance  |
+─────────────────────────────────────────────────────────────────────────────────────────────+
|  RIGHT PANEL: Interactive 5m Chart with Intraday VWAP Curve                                 |
|  • Candlesticks (Green / Red)                                                               |
|  • Orange Dashed Line: Institutional Intraday VWAP Curve                                    |
|  • Blue Horizontal Lines: PMH & PML Resistance / Support                                    |
+─────────────────────────────────────────────────────────────────────────────────────────────+
```

---

### Screen C: Dedicated `📊 Sector Analysis` (New Tab View)
Clicking **`Sector Analysis`** in the left sidebar switches to an **Institutional Sector Heatmap**:

```
+─────────────────────────────────────────────────────────────────────────────────────────────+
|  INSTITUTIONAL SECTOR FLOW & THEME TAILWIND DESK                                            |
+─────────────────────────────────────────────────────────────────────────────────────────────+
|  SECTOR TILES (Color-coded by Net Real-Time Inflow):                                        |
|  ┌────────────────────────┐  ┌────────────────────────┐  ┌────────────────────────┐         |
|  │ NIFTY METAL   +1.85%   │  │ NIFTY AUTO    +1.20%   │  │ NIFTY PHARMA   +0.75%  │         |
|  │ Tailwinds: Strong      │  │ Tailwinds: Moderate    │  │ Tailwinds: Mild        │         |
|  │ Leaders: HINDZINC, VEDL│  │ Leaders: TATAMOTORS    │  │ Leaders: DIVISLAB      │         |
|  └────────────────────────┘  └────────────────────────┘  └────────────────────────┘         |
|  ┌────────────────────────┐  ┌────────────────────────┐  ┌────────────────────────┐         |
|  │ NIFTY IT      -0.45%   │  │ NIFTY FMCG    -0.80%   │  │ NIFTY MEDIA    -1.10%  │         |
|  │ Status: Headwinds      │  │ Status: Dragging Down  │  │ Status: Weak           │         |
|  │ Lagging: INFY, TECHM   │  │ Lagging: NESTLEIND     │  │ Lagging: ZEEL          │         |
|  └────────────────────────┘  └────────────────────────┘  └────────────────────────┘         |
+─────────────────────────────────────────────────────────────────────────────────────────────+
|  THEME ALIGNMENT TABLE:                                                                     |
|  Stock       Sector       Sector Chg%   Tailwind Verdict    Trading Recommendation          |
|  ─────────────────────────────────────────────────────────────────────────────────────────  |
|  HINDZINC    Metal        +1.85%        Strong Tailwind     High-Conviction CE Buy          |
|  VEDL        Metal        +1.85%        Strong Tailwind     High-Conviction CE Buy          |
|  INFY        IT           -0.45%        Sector Headwind     Avoid CE (Peer Selling Drag)    |
+─────────────────────────────────────────────────────────────────────────────────────────────+
```

---

## 3. Mathematical & Algorithmic Implementation Details

### Module 1: Candle Body-to-Wick Ratio Engine
* **File:** `stock_suggestion_index.py`
* **Function:** `calc_candle_quality(candle)`
* **Mathematical Formula:**
  $$\text{Total Range} = \text{High} - \text{Low}$$
  $$\text{Body Size} = |\text{Close} - \text{Open}|$$
  $$\text{Upper Wick} = \text{High} - \max(\text{Open}, \text{Close})$$
  $$\text{Lower Wick} = \min(\text{Open}, \text{Close}) - \text{Low}$$
  $$\text{Body Ratio} = \frac{\text{Body Size}}{\text{Total Range}} \times 100$$
* **Classification Criteria:**
  * **$\text{Body Ratio} \ge 65\%$:** Solid Institutional Body (`Confirmed BO`).
  * **$\text{Body Ratio} < 50\%$ and $\text{Upper Wick} \ge 40\%$:** Rejected Wick Trap (`Weak BO - Wick`).

### Module 2: Intraday VWAP & Distance Calculation
* **File:** `stock_suggestion_index.py` & `serve_nifty50_app.py`
* **Function:** `compute_intraday_vwap(candles)`
* **Mathematical Formula:**
  $$\text{Typical Price}_i = \frac{\text{High}_i + \text{Low}_i + \text{Close}_i}{3}$$
  $$\text{Cumulative Volume} = \sum_{i=1}^n \text{Volume}_i$$
  $$\text{Cumulative TP} \times \text{Volume} = \sum_{i=1}^n (\text{Typical Price}_i \times \text{Volume}_i)$$
  $$\text{VWAP} = \frac{\text{Cumulative TP} \times \text{Volume}}{\text{Cumulative Volume}}$$
  $$\text{VWAP Distance \%} = \frac{\text{LTP} - \text{VWAP}}{\text{VWAP}} \times 100$$
* **Classification Zones:**
  * **$0.0\% \le \text{Dist} \le 1.2\%$:** **`Sweet Spot Entry`** (Maximum Risk-to-Reward).
  * **$1.2\% < \text{Dist} \le 2.2\%$:** **`Acceptable Momentum`**.
  * **$\text{Dist} > 2.5\%$:** **`Overextended Zone`** (Pullback Risk; do not chase).

### Module 3: Sectoral Mapping & Index Scraping
* **File:** `stock_suggestion_index.py`
* **Data Sources:** 
  * NSE Sector Indices scraped via Yahoo Finance (`^CNXMETAL`, `^CNXIT`, `^CNXAUTO`, `^CNXPHARMA`, `^CNXFMCG`, `^CNXFINANCE`).
* **Stock-to-Sector Hash Map:** Maps each of the 200 F&O symbols to its parent sector index.
* **Tailwind Calculation:**
  $$\text{Sector Relative Strength} = \text{Stock \% Change} - \text{Sector \% Change}$$
  * If both Stock and Sector are positive and sector is leading $\rightarrow$ **`Strong Tailwind`**.

---

## 4. Frontend Integration Plan (Zero Clutter Architecture)

1. **Tab Switching Router (`nifty50_ai_prediction_console.html`):**
   * Keep the single-page application (SPA) model fast and responsive.
   * Clicking `.menu-item` in the sidebar shows the corresponding container while hiding others:
     * `#viewFuturesOptions` (Current Main View)
     * `#viewBreakoutHunter` (New Zero-Fakeout View)
     * `#viewSectorAnalysis` (New Sector Heatmap View)
2. **Chart Layering:**
   * On the TradingView canvas, add a subtle **orange dashed line** representing the **Intraday VWAP curve** so you visually see how close the breakout candle is to VWAP.

---

## 5. Verification & Acceptance Criteria

1. **Screen Independence:** The main `Futures & Options` screen remains clean and uncluttered.
2. **Navigation Smoothness:** Clicking `Sector Analysis` or `Breakout Hunter` transitions instantly with zero page reloads.
3. **Execution Clarity:** Every breakout displays:
   - Body Ratio $\%$
   - VWAP Distance $\%$
   - Sector Tailwind status
4. **Data Accuracy:** Sector data and VWAP curves update in sync with the 45-second background polling cycle.
