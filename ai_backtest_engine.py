"""
AI Backtest Engine
==================
Runs bar-by-bar historical backtesting of SENSEX and NIFTY 50 trading sessions
using the Gemini AI Trade Evaluator.

Evaluates every candidate setup with AI conviction scoring and outputs
a detailed institutional trade ledger with Win/Loss statistics.
"""

import sys
import os
import zoneinfo
from datetime import datetime
from typing import Any

# Ensure current directory is on python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stock_suggestion_index import fetch_5m_candles_pipeline
from smc_volume_profile_engine import detect_order_blocks_and_structure, calculate_extension_gate
from gemini_evaluator import evaluate_trade_with_gemini, get_gemini_api_key

INDIA_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")


def run_ai_session_backtest(
    sym_display: str,
    sym_code: str,
    target_date: datetime.date,
    api_key: str = "",
    min_conviction: int = 80,
    use_api: bool = False,
) -> dict[str, Any]:
    """Replay a single trading session bar-by-bar and audit with AI Evaluator."""
    pipe_res = fetch_5m_candles_pipeline(sym_code, full_range=True)
    all_candles = pipe_res.get("completed_candles") or []

    prior_and_target = [
        c for c in all_candles
        if datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() <= target_date
    ]

    target_candles = [
        c for c in prior_and_target
        if datetime.fromtimestamp(c["timestamp"], tz=INDIA_TZ).date() == target_date
    ]

    if len(target_candles) < 20:
        return {"error": f"Insufficient candles for {target_date} ({len(target_candles)})"}

    open_p = float(target_candles[0]["open"])
    orb_5h = float(target_candles[0]["high"])
    orb_5l = float(target_candles[0]["low"])
    day_high = max(c["high"] for c in target_candles)
    day_low = min(c["low"] for c in target_candles)
    close_p = float(target_candles[-1]["close"])
    day_return = (close_p - open_p) / open_p * 100.0

    min_win_pts = 150.0 if "SENSEX" in sym_display.upper() else 50.0

    print("=" * 105)
    print(f"SESSION AUDIT: {sym_display} ON {target_date} | Open: {open_p:,.1f} | High: {day_high:,.1f} | Low: {day_low:,.1f} | Close: {close_p:,.1f} ({day_return:+.2f}%)")
    print(f"Opening Range: ORB 5H = {orb_5h:,.1f} | ORB 5L = {orb_5l:,.1f}")
    print("=" * 105)

    seen_keys = set()
    trades: list[dict[str, Any]] = []

    # Bar-by-bar simulation starting after 9:30 AM
    for bar_idx in range(3, len(target_candles)):
        curr_candles = [c for c in prior_and_target if c["timestamp"] <= target_candles[bar_idx]["timestamp"]]
        curr_session = target_candles[:bar_idx + 1]
        curr_bar = target_candles[bar_idx]
        curr_dt = datetime.fromtimestamp(curr_bar["timestamp"], tz=INDIA_TZ)
        ltp = float(curr_bar["close"])

        # Calculate live session ATR
        base_ranges = [float(c["high"]) - float(c["low"]) for c in curr_session[:min(14, len(curr_session))]]
        atr = sum(base_ranges) / len(base_ranges) if base_ranges else (25.0 if "NIFTY" in sym_display else 75.0)

        # Calculate session EMA20
        closes = [c["close"] for c in curr_session]
        alpha = 2.0 / 21
        ema20 = closes[0]
        for p in closes:
            ema20 = alpha * p + (1.0 - alpha) * ema20

        # Calculate extension gate
        ext_gate = calculate_extension_gate(curr_candles)

        # Detect order blocks formed up to this bar
        ob_res = detect_order_blocks_and_structure(curr_candles)
        raw_obs = ob_res.get("order_blocks", [])

        # Check freshly formed order blocks for today
        for ob in raw_obs:
            if ob.get("status") != "Fresh" or ob.get("mitigated") or ob.get("tested"):
                continue

            f_time = ob.get("formation_time") or ob.get("time", 0)
            f_dt = datetime.fromtimestamp(f_time, tz=INDIA_TZ)
            if f_dt.date() != target_date:
                continue

            # Must have formed recently (within last 20 minutes)
            age_mins = (curr_dt - f_dt).total_seconds() / 60.0
            if age_mins < 0 or age_mins > 20:
                continue

            is_bear = (
                ob.get("type") in ("BEARISH", "-OB")
                or "BEAR" in str(ob.get("label", "")).upper()
                or "SUPPLY" in str(ob.get("label", "")).upper()
            )
            side = "SHORT" if is_bear else "LONG"
            clean_time = f_dt.strftime("%I:%M %p").lstrip("0")
            alert_key = f"{sym_display}_{side}_{clean_time}"

            if alert_key in seen_keys:
                continue
            seen_keys.add(alert_key)

            # Build market snapshot for AI Evaluator
            snapshot = {
                "symbol": sym_display,
                "time_ist": clean_time,
                "ltp": ltp,
                "day_open": open_p,
                "day_high": max(c["high"] for c in curr_session),
                "day_low": min(c["low"] for c in curr_session),
                "orb_5h": orb_5h,
                "orb_5l": orb_5l,
                "ema20": round(ema20, 2),
                "atr_5m": round(atr, 2),
                "setup_side": side,
                "ob_top": float(ob.get("top") or 0.0),
                "ob_bottom": float(ob.get("bottom") or 0.0),
                "extension_atr": ext_gate.get("extension_atr", 0.0),
                "is_extended": ext_gate.get("is_extended", False),
                "order_flow": {
                    "delta_bar": curr_bar.get("delta") or 0.0,
                },
            }

            # Query AI Evaluator
            ai_eval = evaluate_trade_with_gemini(snapshot, api_key=api_key, use_api=use_api)
            conviction = ai_eval.get("conviction_score", 0)
            verdict = ai_eval.get("verdict", "REJECTED")
            classification = ai_eval.get("trade_classification", "UNKNOWN")
            rationale = ai_eval.get("rationale", "")

            # Calculate actual future trade outcome
            remaining = target_candles[bar_idx:]
            if side == "LONG":
                peak = max(c["high"] for c in remaining)
                trough = min(c["low"] for c in remaining)
                mfe = peak - ltp
                mae = ltp - trough
                is_win = mfe >= min_win_pts and mfe > mae
            else:
                peak = max(c["high"] for c in remaining)
                trough = min(c["low"] for c in remaining)
                mfe = ltp - trough
                mae = peak - ltp
                is_win = mfe >= min_win_pts and mfe > mae

            trade_record = {
                "time": clean_time,
                "side": side,
                "entry_price": ltp,
                "ob_range": f"[{ob.get('bottom'):,.1f} - {ob.get('top'):,.1f}]",
                "ai_score": conviction,
                "ai_verdict": verdict,
                "ai_class": classification,
                "rationale": rationale,
                "mfe": mfe,
                "mae": mae,
                "is_win": is_win,
                "ai_passed": (verdict == "APPROVED" and conviction >= min_conviction),
            }
            trades.append(trade_record)

    # Print session audit table
    print(f"{'TIME':<10} | {'SIDE':<5} | {'ENTRY':<8} | {'AI SCORE':<8} | {'AI VERDICT':<10} | {'CLASSIFICATION':<24} | {'MFE / MAE':<18} | {'OUTCOME'}")
    print("-" * 105)

    approved_wins = 0
    approved_losses = 0
    rejected_traps = 0
    rejected_wins = 0

    for t in trades:
        mfe_str = f"+{t['mfe']:,.1f} / -{t['mae']:,.1f}"
        outcome_str = "✅ PROFIT" if t["is_win"] else "❌ LOSS"
        ai_badge = f"🟢 {t['ai_verdict']}" if t["ai_passed"] else f"🛡️ {t['ai_verdict']}"

        if t["ai_passed"]:
            if t["is_win"]:
                approved_wins += 1
            else:
                approved_losses += 1
        else:
            if not t["is_win"]:
                rejected_traps += 1  # AI successfully saved us from a trap!
            else:
                rejected_wins += 1

        print(f"{t['time']:<10} | {t['side']:<5} | {t['entry_price']:<8,.1f} | {t['ai_score']:<8} | {ai_badge:<12} | {t['ai_class']:<24} | {mfe_str:<18} | {outcome_str}")
        print(f"  └─ AI Insight: {t['rationale']}")

    print("\n" + "=" * 50 + " SESSION METRICS " + "=" * 50)
    total_candidates = len(trades)
    approved_total = approved_wins + approved_losses
    win_rate = (approved_wins / approved_total * 100.0) if approved_total > 0 else 0.0

    print(f"Total Setups Scanned: {total_candidates}")
    print(f"AI Approved Trades  : {approved_total} (Wins: {approved_wins}, Losses: {approved_losses}) -> AI Win Rate: {win_rate:.1f}%")
    print(f"AI Rejected Traps   : {rejected_traps} traps successfully filtered!")
    print("=" * 105 + "\n")

    return {
        "date": target_date.isoformat(),
        "symbol": sym_display,
        "total_setups": total_candidates,
        "approved": approved_total,
        "wins": approved_wins,
        "losses": approved_losses,
        "win_rate": win_rate,
        "traps_avoided": rejected_traps,
    }


def run_multi_session_audit(api_key: str = "", days_count: int = 7, use_api: bool = False):
    """Run full backtest audit across the last 7 available trading days."""
    dates = [
        datetime(2026, 11, 25).date(),
        datetime(2026, 11, 23).date(),
        datetime(2026, 11, 20).date(),
        datetime(2026, 11, 19).date(),
        datetime(2026, 11, 18).date(),
        datetime(2026, 11, 17).date(),
        datetime(2026, 11, 16).date(),
    ][:days_count]

    summary_results = []

    for sym_display, sym_code in [("SENSEX", "SENSEX"), ("NIFTY 50", "NIFTY50")]:
        print("\n" + "#" * 105)
        print(f"### RUNNING 7-DAY AI BACKTEST FOR {sym_display} ###")
        print("#" * 105)

        for d in dates:
            res = run_ai_session_backtest(sym_display, sym_code, d, api_key=api_key, use_api=use_api)
            if "error" not in res:
                summary_results.append(res)

    print("\n" + "=" * 105)
    print("### 7-DAY COMPREHENSIVE AI BACKTEST SUMMARY ###")
    print("=" * 105)
    print(f"{'DATE':<12} | {'SYMBOL':<10} | {'TOTAL SETUPS':<14} | {'AI APPROVED':<12} | {'WINS':<6} | {'LOSSES':<8} | {'WIN RATE':<10} | {'TRAPS FILTERED'}")
    print("-" * 105)
    total_app = 0
    total_w = 0
    total_l = 0
    total_traps = 0
    for r in summary_results:
        total_app += r['approved']
        total_w += r['wins']
        total_l += r['losses']
        total_traps += r['traps_avoided']
        print(f"{r['date']:<12} | {r['symbol']:<10} | {r['total_setups']:<14} | {r['approved']:<12} | {r['wins']:<6} | {r['losses']:<8} | {r['win_rate']:<9.1f}% | {r['traps_avoided']}")
    print("-" * 105)
    overall_win_rate = (total_w / total_app * 100.0) if total_app > 0 else 0.0
    print(f"OVERALL 7-DAY TOTALS: Approved Trades={total_app} | Wins={total_w} | Losses={total_l} | Win Rate={overall_win_rate:.1f}% | Traps Filtered={total_traps}")
    print("=" * 105 + "\n")


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else get_gemini_api_key()
    run_multi_session_audit(api_key=key)
