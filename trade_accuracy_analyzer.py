#!/usr/bin/env python3
"""
Evaluate and calculate actual trade accuracy based on historical candles.
"""

from __future__ import annotations
import json
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pathlib import Path

# Add root to path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store_candles_mysql import connect_mysql

def evaluate_trade_accuracy(trade: Dict[str, Any], candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Evaluates the accuracy of a single trade candidate against future candles.
    Rules:
      - CALL: Target hit first -> SUCCESS; SL hit first -> FAILED
      - PUT: Target hit first -> SUCCESS; SL hit first -> FAILED
      - If neither hit within the day's candles:
        - If price ended in profit -> PARTIAL_SUCCESS
        - Else -> EXPIRED
    """
    side = str(trade.get("option_side") or "").upper()
    entry = float(trade.get("entry_price") or 0.0)
    target = float(trade.get("target_price") or 0.0)
    stop = float(trade.get("stop_loss") or 0.0)
    
    if "CALL" not in side and "PUT" not in side:
        return {"status": "EXPIRED", "exit_price": entry, "closed_time": None, "pnl_points": 0.0}
    
    target_hit_time = None
    stop_hit_time = None
    
    for candle in candles:
        high = float(candle.get("high") or 0.0)
        low = float(candle.get("low") or 0.0)
        candle_time = candle.get("market_time")
        
        if "CALL" in side:
            # Check target hit
            if high >= target and target_hit_time is None:
                target_hit_time = (target, candle_time)
            # Check stop hit
            if low <= stop and stop_hit_time is None:
                stop_hit_time = (stop, candle_time)
        else:  # PUT
            # Check target hit
            if low <= target and target_hit_time is None:
                target_hit_time = (target, candle_time)
            # Check stop hit
            if high >= stop and stop_hit_time is None:
                stop_hit_time = (stop, candle_time)
                
        # If both hit, determine order or fail conservatively
        if target_hit_time and stop_hit_time:
            # If hit in the same candle, treat as FAILED conservatively
            break

    # Resolve status
    if target_hit_time and not stop_hit_time:
        # Target hit successfully
        exit_price, closed_time = target_hit_time
        pnl = abs(target - entry) if "CALL" in side else abs(entry - target)
        return {"status": "SUCCESS", "exit_price": exit_price, "closed_time": closed_time, "pnl_points": pnl}
        
    elif stop_hit_time and not target_hit_time:
        # Stop loss hit
        exit_price, closed_time = stop_hit_time
        pnl = -abs(entry - stop)
        return {"status": "FAILED", "exit_price": exit_price, "closed_time": closed_time, "pnl_points": pnl}
        
    elif target_hit_time and stop_hit_time:
        # Both hit (likely same candle or close timeframe) - conservative failure
        exit_price, closed_time = stop_hit_time
        pnl = -abs(entry - stop)
        return {"status": "FAILED", "exit_price": exit_price, "closed_time": closed_time, "pnl_points": pnl}
        
    else:
        # Neither hit - day ended
        if not candles:
            # No future candles yet (could be open / live session)
            return {"status": "OPEN", "exit_price": None, "closed_time": None, "pnl_points": 0.0}
            
        last_candle = candles[-1]
        exit_price = float(last_candle.get("close") or entry)
        closed_time = last_candle.get("market_time")
        
        if "CALL" in side:
            pnl = exit_price - entry
            status = "PARTIAL_SUCCESS" if pnl > 0.0 else "EXPIRED"
        else:  # PUT
            pnl = entry - exit_price
            status = "PARTIAL_SUCCESS" if pnl > 0.0 else "EXPIRED"
            
        return {"status": status, "exit_price": exit_price, "closed_time": closed_time, "pnl_points": pnl}


def recalculate_all_trades() -> Dict[str, Any]:
    """
    Scans the ai_options table, fetches subsequent candles,
    evaluates trade outcomes, and updates records in the database.
    """
    conn = connect_mysql()
    cursor = conn.cursor(dictionary=True)
    
    # 1. Fetch all trades from database
    cursor.execute("SELECT * FROM ai_options ORDER BY opened_market_time ASC")
    trades = cursor.fetchall()
    
    updated_count = 0
    stats = {"SUCCESS": 0, "FAILED": 0, "PARTIAL_SUCCESS": 0, "EXPIRED": 0, "OPEN": 0}
    
    for trade in trades:
        trade_id = trade["id"]
        opened_time = trade["opened_market_time"]
        
        if not opened_time:
            continue
            
        # 2. Fetch subsequent candles of the SAME day
        cursor.execute(
            """
            SELECT open, high, low, close, market_time 
            FROM nifty_candles 
            WHERE symbol = 'NIFTY50' 
              AND timeframe = '5m' 
              AND DATE(market_time) = DATE(%s) 
              AND market_time > %s 
            ORDER BY market_time ASC
            """,
            (opened_time, opened_time)
        )
        candles = cursor.fetchall()
        
        # 3. Evaluate trade result
        result = evaluate_trade_accuracy(trade, candles)
        
        status = result["status"]
        exit_price = result["exit_price"]
        closed_time = result["closed_time"]
        pnl_points = result["pnl_points"]
        
        stats[status] += 1
        
        # If status is still OPEN but trade day is in the past, resolve to EXPIRED or PARTIAL_SUCCESS
        # (This is a safety guard for old open signals)
        if status == "OPEN" and opened_time.date() < datetime.now().date():
            # If no candles are found, it expired flat
            status = "EXPIRED"
            exit_price = float(trade["entry_price"])
            pnl_points = 0.0
            stats["OPEN"] -= 1
            stats["EXPIRED"] += 1
            
        # 4. Update the record
        cursor.execute(
            """
            UPDATE ai_options 
            SET status = %s,
                current_price = %s,
                closed_market_time = %s,
                result_points = %s
            WHERE id = %s
            """,
            (
                status,
                exit_price if exit_price is not None else trade["current_price"],
                closed_time,
                Decimal(str(pnl_points)),
                trade_id
            )
        )
        updated_count += 1
        
    conn.commit()
    conn.close()
    
    return {
        "ok": True,
        "total_evaluated": updated_count,
        "outcomes": stats
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Recalculate trade outcomes.")
    parser.add_argument("--run", action="store_true", help="Trigger recalculation")
    args = parser.parse_args()
    
    if args.run:
        res = recalculate_all_trades()
        print(json.dumps(res, indent=2))
    else:
        print("Use --run to execute trade re-evaluation.")
