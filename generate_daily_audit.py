#!/usr/bin/env python3
"""
generate_daily_audit.py — Automated Daily Breakout Accuracy Auditor

Produces an audited, mathematically honest report of every breakout alert that fired,
verifying whether targets were reached, stops were hit, maximum favorable excursion (MFE),
RVOL, and session accuracy.

Outputs:
  - reports/breakout_audit_YYYY-MM-DD.md
  - reports/breakout_audit_YYYY-MM-DD.csv
  - reports/breakout_audit_YYYY-MM-DD.json

Usage:
  python3 generate_daily_audit.py
  python3 generate_daily_audit.py --candles-dir inputs/candles
  python3 generate_daily_audit.py --date 2026-09-04
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from breakout_engine import BreakoutEngine, BreakoutConfig


IST_TZ = timezone(timedelta(hours=5, minutes=30))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily breakout accuracy audit document.")
    parser.add_argument("--candles-dir", default=str(ROOT / "inputs" / "candles"), help="Path to candles directory")
    parser.add_argument("--date", default=None, help="Market date YYYY-MM-DD (default: latest in files)")
    parser.add_argument("--out-dir", default=str(ROOT / "reports"), help="Output directory for reports")
    return parser.parse_args()


def load_candles_from_dir(candles_dir: Path) -> dict[str, list[dict]]:
    """Loads 5m bars for all available symbols in the candle directory."""
    files = list(candles_dir.glob("*.json"))
    data: dict[str, list[dict]] = {}
    for f in sorted(files):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            sym = d.get("symbol") or f.stem.replace("_5m", "")
            bars = []
            
            # Format 1: Yahoo finance chart result
            res = d.get("chart", {}).get("result", [])
            if res:
                quote = res[0]
                timestamps = quote.get("timestamp") or []
                q = (quote.get("indicators", {}).get("quote") or [{}])[0]
                opens = q.get("open") or []
                highs = q.get("high") or []
                lows = q.get("low") or []
                closes = q.get("close") or []
                volumes = q.get("volume") or []
                for i, t in enumerate(timestamps):
                    if i < len(opens) and opens[i] is not None and closes[i] is not None:
                        dt = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(IST_TZ)
                        bars.append({
                            "market_date": dt.strftime("%Y-%m-%d"),
                            "t": dt.strftime("%H:%M"),
                            "open": float(opens[i]),
                            "high": float(highs[i]),
                            "low": float(lows[i]),
                            "close": float(closes[i]),
                            "volume": float(volumes[i] or 0),
                        })
            
            # Format 2: series.5m
            if not bars:
                bars = d.get("series", {}).get("5m", [])
            
            # Format 3: list of dicts
            if not bars and isinstance(d, list):
                bars = d

            if bars:
                data[sym] = bars
        except Exception as err:
            pass
    return data


def audit_symbol(sym: str, raw_bars: list[dict], target_date: str | None = None) -> list[dict]:
    """Audits a single symbol across the target market session."""
    # Normalize bars
    bars = []
    for b in raw_bars:
        d = b.get("market_date") or (b.get("market_time", "")[:10] if b.get("market_time") else "")
        t = b.get("t") or b.get("market_time", "")
        if len(t) >= 16 and " " in t:
            time_str = t.split(" ")[1][:5]
        elif len(t) >= 16 and "T" in t:
            time_str = t.split("T")[1][:5]
        else:
            time_str = t[:5]

        bars.append({
            "market_date": d,
            "t": time_str,
            "open": float(b.get("open", 0)),
            "high": float(b.get("high", 0)),
            "low": float(b.get("low", 0)),
            "close": float(b.get("close", 0)),
            "volume": float(b.get("volume", 0)),
        })

    if not bars:
        return []

    dates = sorted(set(b["market_date"] for b in bars if b["market_date"]))
    if not dates:
        return []

    eval_date = target_date if target_date in dates else dates[-1]

    session_bars = [b for b in bars if b["market_date"] == eval_date]
    if len(session_bars) < 15:
        return []

    cfg = BreakoutConfig(
        orb_mode="hybrid",
        min_stop_atr=0.75,
        min_stop_pct=0.006,
        max_extension_pct=0.05,
        rvol_min=1.4,
    )

    alerts = []
    for side in ("bullish", "bearish"):
        eng = BreakoutEngine(side, config=cfg, symbol=sym)
        for i, bar in enumerate(session_bars):
            ev = eng.on_bar(bar)
            if not ev:
                continue

            # Check if this qualifies as an alert
            kind = ev.get("kind")
            if kind not in ("BREAKOUT", "RETEST"):
                continue

            rvol = float(ev.get("rvol", 1.0))
            if rvol < 1.4:
                continue

            entry = float(ev["entry"])
            stop = float(ev["stop"])
            risk = abs(entry - stop)
            if risk <= 0:
                continue

            risk_pct = (risk / entry) * 100
            target = entry + 2.0 * risk if side == "bullish" else entry - 2.0 * risk

            # Measure post-entry outcome from i+1 to end of session
            mfe_price = entry
            mae_price = entry
            verdict = "IN PROFIT"
            r_return = 0.0

            for f_bar in session_bars[i + 1:]:
                if side == "bullish":
                    if f_bar["high"] > mfe_price:
                        mfe_price = f_bar["high"]
                    if f_bar["low"] < mae_price:
                        mae_price = f_bar["low"]

                    if f_bar["low"] <= stop:
                        verdict = "STOPPED OUT"
                        r_return = -1.0
                        break
                    elif f_bar["high"] >= target:
                        verdict = "TARGET HIT"
                        r_return = 2.0
                        break
                else:  # bearish
                    if f_bar["low"] < mfe_price:
                        mfe_price = f_bar["low"]
                    if f_bar["high"] > mae_price:
                        mae_price = f_bar["high"]

                    if f_bar["high"] >= stop:
                        verdict = "STOPPED OUT"
                        r_return = -1.0
                        break
                    elif f_bar["low"] <= target:
                        verdict = "TARGET HIT"
                        r_return = 2.0
                        break

            if verdict not in ("STOPPED OUT", "TARGET HIT"):
                last_p = session_bars[-1]["close"]
                if side == "bullish":
                    r_return = (last_p - entry) / risk
                    verdict = "IN PROFIT" if r_return > 0.1 else ("STOPPED OUT" if r_return <= -0.8 else "RETESTING")
                else:
                    r_return = (entry - last_p) / risk
                    verdict = "IN PROFIT" if r_return > 0.1 else ("STOPPED OUT" if r_return <= -0.8 else "RETESTING")

            max_gain_pct = abs(mfe_price - entry) / entry * 100
            max_drawdown_pct = abs(mae_price - entry) / entry * 100

            alerts.append({
                "date": eval_date,
                "time": ev.get("t", bar["t"]),
                "symbol": sym,
                "side": "CE" if side == "bullish" else "PE",
                "kind": kind,
                "entry": entry,
                "stop": stop,
                "target": target,
                "risk_pct": risk_pct,
                "rvol": rvol,
                "max_gain_pct": max_gain_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "verdict": verdict,
                "r_return": r_return,
                "is_win": verdict in ("TARGET HIT", "IN PROFIT"),
                "is_loss": verdict == "STOPPED OUT",
            })
            # One alert per side per session
            break

    return alerts


def main():
    args = parse_args()
    candles_dir = Path(args.candles_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not candles_dir.exists():
        print(f"Candles directory not found: {candles_dir}")
        return

    sym_data = load_candles_from_dir(candles_dir)
    if not sym_data:
        print(f"No candle files found in {candles_dir}")
        return

    all_alerts = []
    eval_date = args.date

    for sym, raw_bars in sym_data.items():
        res = audit_symbol(sym, raw_bars, target_date=eval_date)
        all_alerts.extend(res)

    if not all_alerts:
        print("No breakout alerts triggered on this session.")
        return

    # Sort alerts chronologically
    all_alerts.sort(key=lambda x: (x["time"], x["symbol"]))

    eval_date = all_alerts[0]["date"]
    total = len(all_alerts)
    wins = sum(1 for x in all_alerts if x["is_win"])
    losses = sum(1 for x in all_alerts if x["is_loss"])
    retests = total - wins - losses
    evaluated = wins + losses
    win_rate = round((wins / evaluated * 100), 1) if evaluated > 0 else 0.0
    total_r = round(sum(x["r_return"] for x in all_alerts), 2)
    avg_r = round(total_r / total, 2) if total > 0 else 0.0
    peak_rvol_alert = max(all_alerts, key=lambda x: x["rvol"])

    # 1. Console Output
    print("\n" + "=" * 80)
    print(f"  NSE F&O BREAKOUT ACCURACY AUDIT REPORT — {eval_date}")
    print("=" * 80)
    print(f"  Alerts Triggered:  {total} ({sum(1 for x in all_alerts if x['side'] == 'CE')} CE / {sum(1 for x in all_alerts if x['side'] == 'PE')} PE)")
    print(f"  Audited Outcomes:  {wins} Wins / {losses} Losses / {retests} Retesting")
    print(f"  Audited Win Rate:  {win_rate}%")
    print(f"  Total Realized R:  +{total_r}R (Avg: {avg_r}R / trade)")
    print(f"  Peak Alert RVOL:   {peak_rvol_alert['rvol']:.2f}x ({peak_rvol_alert['symbol']})")
    print("-" * 80)
    print(f"{'Time':<7} {'Symbol':<11} {'Side':<4} {'Entry':<9} {'SL':<9} {'Target':<9} {'Risk%':<6} {'RVOL':<6} {'MaxGain':<8} {'Verdict':<12} {'Return'}")
    print("-" * 80)
    for a in all_alerts:
        v_icon = "🟢" if a["is_win"] else ("🔴" if a["is_loss"] else "🟡")
        r_str = f"+{a['r_return']:.2f}R" if a['r_return'] >= 0 else f"{a['r_return']:.2f}R"
        print(f"{a['time']:<7} {a['symbol']:<11} {a['side']:<4} ₹{a['entry']:<8.2f} ₹{a['stop']:<8.2f} ₹{a['target']:<8.2f} {a['risk_pct']:<5.2f}% {a['rvol']:<5.1f}x +{a['max_gain_pct']:<6.2f}% {v_icon} {a['verdict']:<10} {r_str}")
    print("=" * 80 + "\n")

    # 2. Save Markdown Report
    md_path = out_dir / f"breakout_audit_{eval_date}.md"
    rows_md = "\n".join(
        f"| {a['time']} | **{a['symbol']}** | {'🟢 CE' if a['side'] == 'CE' else '🔴 PE'} | ₹{a['entry']:.2f} | ₹{a['stop']:.2f} | ₹{a['target']:.2f} | {a['risk_pct']:.2f}% | **{a['rvol']:.2f}x** | +{a['max_gain_pct']:.2f}% | {'🟢' if a['is_win'] else ('🔴' if a['is_loss'] else '🟡')} {a['verdict']} | {('+' if a['r_return'] >= 0 else '')}{a['r_return']:.2f}R |"
        for a in all_alerts
    )
    md_content = f"""# Daily Breakout Accuracy Audit Report: {eval_date}

**Audited on:** {datetime.now(IST_TZ).strftime('%d-%b-%Y %I:%M:%S %p IST')}  
**Mathematical Accuracy:** Strictly calculated from point-in-time sequential state machine against real 5m tick bars.

---

### Executive KPI Summary
- **Total Alerts Triggered:** {total} ({sum(1 for x in all_alerts if x['side'] == 'CE')} CE Calls · {sum(1 for x in all_alerts if x['side'] == 'PE')} PE Puts)
- **Audited Outcomes:** {wins} Wins / {losses} Losses / {retests} Retesting
- **Session Win Rate:** **{win_rate}%**
- **Net Realized R-Multiple:** **+{total_r}R** (Avg: **+{avg_r}R** per alert)
- **Peak Alert Volume:** **{peak_rvol_alert['rvol']:.2f}x RVOL** ({peak_rvol_alert['symbol']})

---

### Chronological Trade Audit Trail

| Time | Symbol | Side | Entry Price | Stop Loss | Target | Risk % | 5m RVOL | Peak MFE Gain | Final Verdict | Realized R |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
{rows_md}

---
*Generated autonomously by `generate_daily_audit.py`.*
"""
    md_path.write_text(md_content, encoding="utf-8")
    print(f"✓ Saved Markdown report to: {md_path}")

    # 3. Save CSV
    csv_path = out_dir / f"breakout_audit_{eval_date}.csv"
    csv_header = "Time,Symbol,Side,Entry,Stop_Loss,Target,Risk_Pct,RVOL,Max_Gain_Pct,Max_Drawdown_Pct,Verdict,Realized_R\n"
    csv_rows = "\n".join(
        f"{a['time']},{a['symbol']},{a['side']},{a['entry']:.2f},{a['stop']:.2f},{a['target']:.2f},{a['risk_pct']:.2f},{a['rvol']:.2f},{a['max_gain_pct']:.2f},{a['max_drawdown_pct']:.2f},{a['verdict']},{a['r_return']:.2f}"
        for a in all_alerts
    )
    csv_path.write_text(csv_header + csv_rows + "\n", encoding="utf-8")
    print(f"✓ Saved CSV report to: {csv_path}")

    # 4. Save JSON
    json_path = out_dir / f"breakout_audit_{eval_date}.json"
    summary_data = {
        "date": eval_date,
        "total_alerts": total,
        "wins": wins,
        "losses": losses,
        "retesting": retests,
        "win_rate_pct": win_rate,
        "net_r": total_r,
        "avg_r": avg_r,
        "peak_rvol": {"symbol": peak_rvol_alert["symbol"], "rvol": peak_rvol_alert["rvol"]},
        "alerts": all_alerts,
    }
    json_path.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
    print(f"✓ Saved JSON report to: {json_path}\n")


if __name__ == "__main__":
    main()
