#!/usr/bin/env python3
"""
Aggregate and generate historical accuracy reports from MySQL tables.
"""

from __future__ import annotations
import json
import sys
from collections import defaultdict, Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List
from pathlib import Path

# Add root to path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store_candles_mysql import connect_mysql

def get_accuracy_dashboard_data() -> Dict[str, Any]:
    conn = connect_mysql()
    cursor = conn.cursor(dictionary=True)
    
    # ------------------------------------------------
    # 1. Fetch evaluated trades from ai_options
    # ------------------------------------------------
    cursor.execute("""
        SELECT id, opened_market_time, option_side, status, 
               entry_price, target_price, stop_loss, current_price,
               confidence, pattern_name, result_points,
               premium_entry, premium_exit, pnl_premium, option_type, option_name, mfe, mae
        FROM ai_options
        ORDER BY opened_market_time DESC
    """)
    trades = cursor.fetchall()
    
    # ------------------------------------------------
    # 2. Fetch rejected opportunities from opportunity_audit
    # ------------------------------------------------
    cursor.execute("""
        SELECT id, market_time, signal_side, pattern_name, 
               rejection_reasons, final_verdict, future_max_move_points
        FROM opportunity_audit
        ORDER BY market_time DESC
    """)
    audits = cursor.fetchall()
    
    conn.close()
    
    # --- Compute Overall Statistics ---
    wins_count = 0
    losses_count = 0
    expired_count = 0
    open_count = 0
    partial_success_count = 0
    total_trades = 0
    total_rr_points_won = 0.0
    total_rr_points_lost = 0.0
    total_points = 0.0
    
    # Group by pattern
    pattern_stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0, "expired": 0,
        "total_profit": 0.0, "total_loss": 0.0
    })
    
    # Group by option type (ATM vs OTM1 vs OTM2)
    option_stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0, "pnl_rupees": 0.0, "total_profit": 0.0, "total_loss": 0.0
    })
    
    # Group by day
    daily_stats = defaultdict(lambda: {
        "signals": 0, "wins": 0, "losses": 0, "expired": 0,
        "total_profit": 0.0, "total_loss": 0.0, "pnl_rupees": 0.0
    })
    
    total_paper_pnl = 0.0
    
    for t in trades:
        status = str(t["status"]).upper()
        pnl = float(t["result_points"] or 0.0)
        pnl_premium = float(t["pnl_premium"] or 0.0)
        pnl_rupees = pnl_premium * 65.0
        pattern = t["pattern_name"] or "Unknown"
        opt_type = t["option_type"] or "ATM"
        day_str = t["opened_market_time"].date().isoformat() if t["opened_market_time"] else "Unknown"
        
        if status == "OPEN":
            open_count += 1
            continue
            
        total_trades += 1
        total_paper_pnl += pnl_rupees
        
        # Track Option Statistics
        option_stats[opt_type]["trades"] += 1
        option_stats[opt_type]["pnl_rupees"] += pnl_rupees
        
        if status in ("SUCCESS", "PARTIAL_SUCCESS"):
            if status == "SUCCESS":
                wins_count += 1
            else:
                partial_success_count += 1
            total_rr_points_won += pnl
            pattern_stats[pattern]["wins"] += 1
            pattern_stats[pattern]["total_profit"] += pnl
            daily_stats[day_str]["wins"] += 1
            daily_stats[day_str]["total_profit"] += pnl
            
            option_stats[opt_type]["wins"] += 1
            option_stats[opt_type]["total_profit"] += pnl_premium
        else:
            if status == "FAILED":
                losses_count += 1
            else:
                expired_count += 1
            total_rr_points_lost += abs(pnl)
            pattern_stats[pattern]["losses"] += 1
            pattern_stats[pattern]["total_loss"] += abs(pnl)
            daily_stats[day_str]["losses"] += 1
            daily_stats[day_str]["total_loss"] += abs(pnl)
            
            option_stats[opt_type]["losses"] += 1
            option_stats[opt_type]["total_loss"] += abs(pnl_premium)
            
        pattern_stats[pattern]["trades"] += 1
        daily_stats[day_str]["signals"] += 1
        daily_stats[day_str]["pnl_rupees"] += pnl_rupees
        total_points += pnl

    # --- Pattern Accuracy Report Formatting ---
    pattern_report = []
    for pat, s in pattern_stats.items():
        t_count = s["trades"]
        w_count = s["wins"]
        l_count = s["losses"]
        wr = (w_count / t_count) * 100 if t_count > 0 else 0.0
        avg_p = s["total_profit"] / w_count if w_count > 0 else 0.0
        avg_l = s["total_loss"] / l_count if l_count > 0 else 0.0
        pf = s["total_profit"] / s["total_loss"] if s["total_loss"] > 0 else (s["total_profit"] if s["total_profit"] > 0 else 0.0)
        
        pattern_report.append({
            "pattern_name": pat,
            "trades": t_count,
            "wins": w_count,
            "losses": l_count,
            "win_rate": round(wr, 1),
            "avg_profit": round(avg_p, 1),
            "avg_loss": round(avg_l, 1),
            "profit_factor": round(pf, 2)
        })
        
    # Sort best to worst (by win rate, then profit factor, then trades)
    pattern_report.sort(key=lambda x: (x["profit_factor"], x["win_rate"], x["trades"]), reverse=True)

    # --- Gate Blocking Analysis ---
    category_map = {
        "Minimum move filter blocked": "Min Move",
        "projected_move_below_threshold": "Min Move",
        "NSE/OI confirmation missing": "OI/NSE",
        "Option-chain side not aligned": "OI/NSE",
        "option_chain_missing_blocked": "OI/NSE",
        "option_chain_side_not_aligned": "OI/NSE",
        "Pattern/OI/news agreement too weak": "Trend Alignment",
        "Pattern side conflicts with market score": "Trend Alignment",
        "pattern_side_conflicts_with_market_score": "Trend Alignment",
        "agreement_too_weak": "Trend Alignment",
        "base_candle_score_neutral": "Trend Alignment",
        "Base candle score is neutral": "Trend Alignment",
        "Pattern transition not cleared": "Trend Alignment",
        "pattern_transition_not_cleared": "Trend Alignment",
        "Session invalid or stale": "Session",
        "session_invalid": "Session",
        "Confidence below BUY gate": "Confidence",
        "confidence_below_buy_gate": "Confidence",
        "premium_missing_record_only": "Premium",
        "premium_missing_does_not_block_signal_display": "Premium"
    }
    
    bookkeeping_reasons = {
        "historical_replay_mode",
        "live_trade_blocked_during_replay",
        "nse_oi_confirmation_not_used_for_historical_replay"
    }
    
    gate_blocker_counts = Counter()
    for row in audits:
        reasons_raw = row["rejection_reasons"]
        if not reasons_raw:
            continue
        try:
            reasons = json.loads(reasons_raw)
        except Exception:
            continue
            
        active_blockers = set()
        for r in reasons:
            r_clean = r.replace('"', '').strip()
            if r_clean in bookkeeping_reasons:
                continue
            cat = category_map.get(r_clean)
            if not cat:
                r_lower = r_clean.lower()
                if "move" in r_lower or "threshold" in r_lower: cat = "Min Move"
                elif "option-chain" in r_lower or "nse" in r_lower or "oi" in r_lower: cat = "OI/NSE"
                elif "agreement" in r_lower or "trend" in r_lower or "neutral" in r_lower: cat = "Trend Alignment"
                elif "session" in r_lower or "closed" in r_lower or "stale" in r_lower: cat = "Session"
                elif "confidence" in r_lower: cat = "Confidence"
                elif "premium" in r_lower: cat = "Premium"
                else: cat = "Trend Alignment"
            active_blockers.add(cat)
            
        for blocker in active_blockers:
            gate_blocker_counts[blocker] += 1

    # --- Capture Rate Analysis ---
    # Captured opportunities = successful/profitable qualified trades in ai_options
    captured_count = sum(1 for t in trades if str(t["status"]).upper() == "SUCCESS" or float(t["result_points"] or 0.0) >= 50.0)
    # Missed opportunities = wait candidates that went on to move 50+ points in predicted direction
    missed_count = sum(1 for row in audits if str(row["final_verdict"]).upper() in ("MISSED_CALL", "MISSED_PUT"))
    
    total_opportunities = captured_count + missed_count
    capture_rate = (captured_count / total_opportunities) * 100 if total_opportunities > 0 else 0.0

    # --- Daily Performance ---
    daily_report = []
    for day, s in daily_stats.items():
        if day == "Unknown":
            continue
        t_count = s["signals"]
        w_count = s["wins"]
        l_count = s["losses"]
        wr = (w_count / t_count) * 100 if t_count > 0 else 0.0
        pf = s["total_profit"] / s["total_loss"] if s["total_loss"] > 0 else (s["total_profit"] if s["total_profit"] > 0 else 0.0)
        
        daily_report.append({
            "date": day,
            "signals": t_count,
            "wins": w_count,
            "losses": l_count,
            "win_rate": round(wr, 1),
            "profit_factor": round(pf, 2),
            "pnl_rupees": round(s["pnl_rupees"], 2)
        })
    daily_report.sort(key=lambda x: x["date"], reverse=True)

    # --- Option Strikes Performance Report ---
    option_report = []
    for opt, s in option_stats.items():
        t_count = s["trades"]
        w_count = s["wins"]
        l_count = s["losses"]
        wr = (w_count / t_count) * 100 if t_count > 0 else 0.0
        pf = s["total_profit"] / s["total_loss"] if s["total_loss"] > 0 else (s["total_profit"] if s["total_profit"] > 0 else 0.0)
        
        option_report.append({
            "option_type": opt,
            "trades": t_count,
            "wins": w_count,
            "losses": l_count,
            "win_rate": round(wr, 1),
            "profit_factor": round(pf, 2),
            "pnl_rupees": round(s["pnl_rupees"], 2)
        })
    # Sort option report to find the best strike
    option_report.sort(key=lambda x: x["pnl_rupees"], reverse=True)
    best_strike = option_report[0]["option_type"] if option_report else "ATM"

    # --- Overall KPI calculations ---
    overall_accuracy = ((wins_count + partial_success_count) / total_trades) * 100 if total_trades > 0 else 0.0
    profit_factor = total_rr_points_won / total_rr_points_lost if total_rr_points_lost > 0 else (total_rr_points_won if total_rr_points_won > 0 else 0.0)
    
    # Calculate average Risk/Reward of trades (target-to-entry / entry-to-stop)
    rr_vals = []
    for t in trades:
        try:
            entry = float(t["entry_price"])
            target = float(t["target_price"])
            stop = float(t["stop_loss"])
            risk = abs(entry - stop)
            if risk > 0:
                rr_vals.append(abs(target - entry) / risk)
        except Exception:
            pass
    avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0.0
    
    best_pattern = pattern_report[0]["pattern_name"] if pattern_report else "None"
    worst_pattern = pattern_report[-1]["pattern_name"] if len(pattern_report) > 1 else "None"
    
    # Most blocking gate calculation
    most_blocking = gate_blocker_counts.most_common(1)
    most_blocking_gate = most_blocking[0][0] if most_blocking else "None"

    return {
        "overall": {
            "accuracy": round(overall_accuracy, 1),
            "total_trades": total_trades,
            "wins": wins_count,
            "losses": losses_count,
            "expired": expired_count,
            "open": open_count,
            "partial_success": partial_success_count,
            "profit_factor": round(profit_factor, 2),
            "avg_rr": round(avg_rr, 2),
            "capture_rate": round(capture_rate, 1),
            "total_opportunities": total_opportunities,
            "captured_opportunities": captured_count,
            "missed_opportunities": missed_count,
            "best_pattern": best_pattern,
            "worst_pattern": worst_pattern,
            "most_blocking_gate": most_blocking_gate,
            "total_paper_pnl": round(total_paper_pnl, 2),
            "best_strike": best_strike
        },
        "patterns": pattern_report,
        "options": option_report,
        "gates": {
            "Min Move": gate_blocker_counts["Min Move"],
            "OI/NSE": gate_blocker_counts["OI/NSE"],
            "Trend Alignment": gate_blocker_counts["Trend Alignment"],
            "Confidence": gate_blocker_counts["Confidence"],
            "Session": gate_blocker_counts["Session"],
            "Premium": gate_blocker_counts["Premium"]
        },
        "daily": daily_report
    }

if __name__ == "__main__":
    data = get_accuracy_dashboard_data()
    print(json.dumps(data, indent=2))
