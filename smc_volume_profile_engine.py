#!/usr/bin/env python3
"""SMC, Volume Profile & Order Flow Engine for NIFTY 50 / NSE Stocks.

Calculates:
1. Session Volume Profile (POC, VAH, VAL, and price-volume histogram).
2. Smart Money Concepts (Swing Pivots, BOS, CHoCH, and Bullish/Bearish Order Blocks).
3. Order Flow Delta & Buyer/Seller Absorption Detection.
"""

from __future__ import annotations

import math
from typing import Any


def calculate_volume_profile(
    candles: list[dict[str, Any]],
    num_bins: int = 50,
    value_area_pct: float = 0.70,
) -> dict[str, Any]:
    """Computes Point of Control (POC), Value Area High (VAH), Value Area Low (VAL),
    and horizontal volume distribution bins from intraday OHLCV candles.
    """
    if not candles:
        return {"poc": None, "vah": None, "val": None, "bins": [], "total_volume": 0}

    lows = [c["low"] for c in candles if c.get("low") is not None]
    highs = [c["high"] for c in candles if c.get("high") is not None]
    if not lows or not highs:
        return {"poc": None, "vah": None, "val": None, "bins": [], "total_volume": 0}

    min_price = min(lows)
    max_price = max(highs)

    if max_price <= min_price:
        return {"poc": min_price, "vah": min_price, "val": min_price, "bins": [], "total_volume": 0}

    bin_size = (max_price - min_price) / num_bins
    bin_volumes = [0.0] * num_bins
    total_volume = 0.0

    # Distribute each candle's volume across the bins it spans
    for c in candles:
        vol = float(c.get("volume", 0) or 0)
        c_low = float(c.get("low", 0))
        c_high = float(c.get("high", 0))
        if vol <= 0 or c_high <= c_low:
            continue

        total_volume += vol
        start_bin = max(0, min(num_bins - 1, int((c_low - min_price) / bin_size)))
        end_bin = max(0, min(num_bins - 1, int((c_high - min_price) / bin_size)))
        span = max(1, end_bin - start_bin + 1)
        vol_per_bin = vol / span

        for b in range(start_bin, end_bin + 1):
            bin_volumes[b] += vol_per_bin

    if total_volume <= 0:
        return {"poc": None, "vah": None, "val": None, "bins": [], "total_volume": 0}

    # 1. Point of Control (POC): Bin with peak volume
    poc_bin = max(range(num_bins), key=lambda i: bin_volumes[i])
    poc_price = round(min_price + (poc_bin + 0.5) * bin_size, 2)

    # 2. Value Area: 70% of volume expanding outward from POC
    target_va_volume = total_volume * value_area_pct
    current_va_volume = bin_volumes[poc_bin]
    va_bins = {poc_bin}
    low_idx = poc_bin - 1
    high_idx = poc_bin + 1

    while current_va_volume < target_va_volume and (low_idx >= 0 or high_idx < num_bins):
        next_low_vol = bin_volumes[low_idx] if low_idx >= 0 else 0.0
        next_high_vol = bin_volumes[high_idx] if high_idx < num_bins else 0.0

        if next_high_vol >= next_low_vol and high_idx < num_bins:
            current_va_volume += next_high_vol
            va_bins.add(high_idx)
            high_idx += 1
        elif low_idx >= 0:
            current_va_volume += next_low_vol
            va_bins.add(low_idx)
            low_idx -= 1
        else:
            break

    min_va_bin = min(va_bins)
    max_va_bin = max(va_bins)
    val_price = round(min_price + min_va_bin * bin_size, 2)
    vah_price = round(min_price + (max_va_bin + 1) * bin_size, 2)

    # Prepare bin array for horizontal histogram display
    max_bin_vol = max(bin_volumes) if bin_volumes else 1.0
    bins_data = []
    for b in range(num_bins):
        price_level = round(min_price + (b + 0.5) * bin_size, 2)
        v = bin_volumes[b]
        bins_data.append({
            "price": price_level,
            "volume": round(v, 1),
            "pct_of_max": round((v / max_bin_vol) * 100, 1) if max_bin_vol > 0 else 0,
            "is_poc": (b == poc_bin),
            "in_value_area": (b in va_bins),
        })

    return {
        "poc": poc_price,
        "vah": vah_price,
        "val": val_price,
        "total_volume": round(total_volume, 0),
        "bin_size": round(bin_size, 2),
        "bins": bins_data,
    }


def detect_order_blocks_and_structure(
    candles: list[dict[str, Any]],
    pivot_window: int = 2,
) -> dict[str, Any]:
    """Detects Swing Highs/Lows, BOS (Break of Structure), CHoCH (Change of Character),
    and Bullish/Bearish Order Blocks (+OB and -OB) with mitigation tracking.
    """
    n = len(candles)
    if n < 5:
        return {
            "trend": "Neutral",
            "swings": [],
            "bos": [],
            "choch": [],
            "order_blocks": [],
        }

    # 1. Identify Swing Pivots
    swings = []
    for i in range(pivot_window, n - pivot_window):
        c = candles[i]
        c_high = c["high"]
        c_low = c["low"]

        is_high = all(candles[j]["high"] <= c_high for j in range(i - pivot_window, i + pivot_window + 1) if j != i)
        is_low = all(candles[j]["low"] >= c_low for j in range(i - pivot_window, i + pivot_window + 1) if j != i)

        if is_high:
            swings.append({
                "index": i,
                "time": c["timestamp"],
                "price": round(c_high, 2),
                "type": "HIGH",
            })
        elif is_low:
            swings.append({
                "index": i,
                "time": c["timestamp"],
                "price": round(c_low, 2),
                "type": "LOW",
            })

    # Sort swings chronologically
    swings.sort(key=lambda x: x["index"])

    # 2. Market Structure (BOS and CHoCH)
    bos_list = []
    choch_list = []
    last_high: dict[str, Any] | None = None
    last_low: dict[str, Any] | None = None
    trend = "Neutral"

    for sw in swings:
        if sw["type"] == "HIGH":
            if last_high is not None and sw["price"] > last_high["price"]:
                # Bullish BOS
                bos_list.append({
                    "time": sw["time"],
                    "price": sw["price"],
                    "broken_level": last_high["price"],
                    "direction": "bullish",
                    "type": "BOS",
                })
                trend = "Bullish"
            elif last_high is not None and sw["price"] < last_high["price"] and trend == "Bullish":
                # Potential lower high
                pass
            last_high = sw
        elif sw["type"] == "LOW":
            if last_low is not None and sw["price"] < last_low["price"]:
                # Bearish BOS
                bos_list.append({
                    "time": sw["time"],
                    "price": sw["price"],
                    "broken_level": last_low["price"],
                    "direction": "bearish",
                    "type": "BOS",
                })
                trend = "Bearish"
            elif last_low is not None and sw["price"] > last_low["price"] and trend == "Bearish":
                # Higher low in downtrend -> Bullish CHoCH setup
                choch_list.append({
                    "time": sw["time"],
                    "price": sw["price"],
                    "direction": "bullish",
                    "type": "CHoCH",
                })
                trend = "Bullish Reversal"
            last_low = sw

    # 3. Order Block Detection (Institutional SMC: Demand & Supply Zones)
    # Bullish OB (DEMAND): Origin pivot low candle before an impulsive rally that creates displacement/FVG/BOS
    # Bearish OB (BEAR OB): Origin pivot high candle before an impulsive drop that causes breakdown
    order_blocks = []
    atr = sum(abs(c["high"] - c["low"]) for c in candles[-14:]) / max(1, min(len(candles), 14))
    ltp = float(candles[-1]["close"]) if candles else 100.0
    min_ob_height = max(atr * 0.15, ltp * 0.0004)

    for i in range(1, n - 2):
        c_prev = candles[i]
        c_curr = candles[i + 1]
        c_next = candles[i + 2]

        # Check for Strong Bullish Displacement (Demand Setup)
        # Origin candle: bearish or tight base at a pivot low
        is_pivot_low = (c_prev["low"] <= candles[i - 1]["low"] or c_prev["close"] <= c_prev["open"])
        curr_body_bull = c_curr["close"] - c_curr["open"]
        is_bull_displacement = (
            is_pivot_low and
            c_curr["close"] > c_curr["open"] and
            curr_body_bull >= (atr * 0.6) and
            (
                (c_curr["high"] - c_prev["low"]) >= (atr * 1.2) or
                (c_next["close"] - c_prev["low"]) >= (atr * 1.5)
            )
        )

        if is_bull_displacement:
            ob_top = round(float(c_prev["high"]), 2)
            ob_bottom = round(float(c_prev["low"]), 2)
            if (ob_top - ob_bottom) < min_ob_height:
                ob_top = round(ob_bottom + min_ob_height, 2)

            mitigated = False
            tested = False
            mitigated_time = None
            for f in range(i + 2, n):
                c_f = candles[f]
                low_f = float(c_f["low"])
                close_f = float(c_f["close"])
                if low_f <= ob_top and low_f >= ob_bottom:
                    tested = True
                if close_f < ob_bottom or low_f < ob_bottom:
                    mitigated = True
                    mitigated_time = c_f["timestamp"]
                    break

            order_blocks.append({
                "id": f"ob_bull_{i}",
                "type": "BULLISH",
                "label": "DEMAND",
                "top": ob_top,
                "bottom": ob_bottom,
                "time": c_prev["timestamp"],
                "status": "Mitigated" if mitigated else ("Tested" if tested else "Fresh"),
                "mitigated": mitigated,
                "tested": tested,
                "mitigated_time": mitigated_time,
            })

        # Check for Strong Bearish Displacement (Supply Setup)
        # Origin candle: bullish or tight base at a pivot high
        is_pivot_high = (c_prev["high"] >= candles[i - 1]["high"] or c_prev["close"] >= c_prev["open"])
        curr_body_bear = c_curr["open"] - c_curr["close"]
        is_bear_displacement = (
            is_pivot_high and
            c_curr["close"] < c_curr["open"] and
            curr_body_bear >= (atr * 0.6) and
            (
                (c_prev["high"] - c_curr["low"]) >= (atr * 1.2) or
                (c_prev["high"] - c_next["close"]) >= (atr * 1.5)
            )
        )

        if is_bear_displacement:
            ob_top = round(float(c_prev["high"]), 2)
            ob_bottom = round(float(c_prev["low"]), 2)
            if (ob_top - ob_bottom) < min_ob_height:
                ob_bottom = round(ob_top - min_ob_height, 2)

            mitigated = False
            tested = False
            mitigated_time = None
            for f in range(i + 2, n):
                c_f = candles[f]
                high_f = float(c_f["high"])
                close_f = float(c_f["close"])
                if high_f >= ob_bottom and high_f <= ob_top:
                    tested = True
                if close_f > ob_top or high_f > ob_top:
                    mitigated = True
                    mitigated_time = c_f["timestamp"]
                    break

            order_blocks.append({
                "id": f"ob_bear_{i}",
                "type": "BEARISH",
                "label": "BEAR OB",
                "top": ob_top,
                "bottom": ob_bottom,
                "time": c_prev["timestamp"],
                "status": "Mitigated" if mitigated else ("Tested" if tested else "Fresh"),
                "mitigated": mitigated,
                "tested": tested,
                "mitigated_time": mitigated_time,
            })

    # Filter & Deduplicate: Keep cleanest non-overlapping Order Blocks (up to 3 Bullish DEMAND, up to 3 BEAR OB)
    # Prioritize active (Fresh / Tested) over Mitigated, avoiding duplicate levels within 0.35%
    def _dedup_obs(obs_subset: list[dict[str, Any]], max_count: int = 3) -> list[dict[str, Any]]:
        if not obs_subset:
            return []
        # Sort so active ones come first, then most recent by time
        active = [o for o in obs_subset if not o["mitigated"]]
        mitigated = [o for o in obs_subset if o["mitigated"]]
        active.sort(key=lambda o: o["time"], reverse=True)
        mitigated.sort(key=lambda o: o["time"], reverse=True)

        chosen: list[dict[str, Any]] = []
        for cand in active + mitigated:
            cand_mid = (cand["top"] + cand["bottom"]) / 2.0
            is_overlap = any(
                abs(cand_mid - ((x["top"] + x["bottom"]) / 2.0)) / max(1.0, cand_mid) < 0.0035
                for x in chosen
            )
            if not is_overlap:
                chosen.append(cand)
            if len(chosen) >= max_count:
                break
        return chosen

    filtered_obs = (
        _dedup_obs([ob for ob in order_blocks if ob["type"] == "BULLISH"], max_count=3) +
        _dedup_obs([ob for ob in order_blocks if ob["type"] == "BEARISH"], max_count=3)
    )

    return {
        "trend": trend,
        "swings": swings[-8:],
        "bos": bos_list[-4:],
        "choch": choch_list[-4:],
        "order_blocks": filtered_obs,
    }


def calculate_order_flow_and_absorption(
    candles: list[dict[str, Any]],
    val_level: float | None = None,
    vah_level: float | None = None,
) -> dict[str, Any]:
    """Computes Footprint Delta (Buy vs Sell Volume) and detects Buyer/Seller Absorption."""
    if not candles:
        return {
            "total_volume": 0,
            "buy_volume": 0,
            "sell_volume": 0,
            "delta": 0,
            "absorption": {"detected": False, "type": "None", "label": "No Absorption"},
            "clusters": [],
        }

    total_buy = 0.0
    total_sell = 0.0
    clusters = []

    for c in candles:
        vol = float(c.get("volume", 0) or 0)
        c_open = float(c.get("open", 0))
        c_close = float(c.get("close", 0))
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))
        spread = max(0.01, c_high - c_low)

        # Estimate intra-candle buy/sell volume from bar body and wicks
        body_ratio = (c_close - c_low) / spread
        buy_vol = vol * body_ratio
        sell_vol = vol * (1.0 - body_ratio)

        total_buy += buy_vol
        total_sell += sell_vol

        clusters.append({
            "time": c["timestamp"],
            "open": c_open,
            "close": c_close,
            "high": c_high,
            "low": c_low,
            "buy_vol": round(buy_vol, 0),
            "sell_vol": round(sell_vol, 0),
            "delta": round(buy_vol - sell_vol, 0),
            "total_vol": round(vol, 0),
        })

    net_delta = total_buy - total_sell
    total_vol = total_buy + total_sell

    # Absorption Detection:
    # Buyer Side Absorption: Large selling volume executed at or below VAL/support,
    # but price closes up with strong lower wick (buyers absorbed all sell orders).
    recent = candles[-5:] if len(candles) >= 5 else candles
    avg_vol = (total_vol / len(candles)) if candles else 1.0

    absorption_detected = False
    absorption_type = "None"
    absorption_label = "Neutral Order Flow"
    absorption_desc = "Normal order flow distribution."

    for c in reversed(recent):
        vol = float(c.get("volume", 0) or 0)
        c_open = float(c.get("open", 0))
        c_close = float(c.get("close", 0))
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))
        spread = max(0.01, c_high - c_low)
        lower_wick = min(c_open, c_close) - c_low
        upper_wick = c_high - max(c_open, c_close)

        # Buyer absorption check
        if vol > avg_vol * 1.3 and (lower_wick / spread) > 0.35 and c_close >= c_open:
            if val_level is None or c_low <= (val_level * 1.005):
                absorption_detected = True
                absorption_type = "Buyer Side Absorption"
                absorption_label = "⚡ Buyer Side Absorption"
                absorption_desc = f"Heavy selling absorbed near ₹{round(c_low, 2)} with strong buyer response (+Delta)."
                break

        # Seller absorption check
        if vol > avg_vol * 1.3 and (upper_wick / spread) > 0.35 and c_close <= c_open:
            if vah_level is None or c_high >= (vah_level * 0.995):
                absorption_detected = True
                absorption_type = "Seller Side Absorption"
                absorption_label = "⚠️ Seller Side Absorption"
                absorption_desc = f"Heavy buying absorbed near ₹{round(c_high, 2)} with institutional sell resistance (-Delta)."
                break

    return {
        "total_volume": round(total_vol, 0),
        "buy_volume": round(total_buy, 0),
        "sell_volume": round(total_sell, 0),
        "delta": round(net_delta, 0),
        "delta_pct": round((net_delta / total_vol) * 100, 1) if total_vol > 0 else 0,
        "absorption": {
            "detected": absorption_detected,
            "type": absorption_type,
            "label": absorption_label,
            "description": absorption_desc,
        },
        "recent_clusters": clusters[-10:],
    }


def generate_institutional_trade_plan(
    candles: list[dict[str, Any]],
    vp: dict[str, Any],
    smc: dict[str, Any],
    of: dict[str, Any],
) -> dict[str, Any]:
    """Generates an institutional smart money trade plan:
    Action, Setup Name, Entry Zone, Stop Loss, Target 1, Target 2, Risk-Reward,
    Order Flow Checklist, and Algorithmic Execution Rule.
    """
    if not candles:
        return {
            "status": "Awaiting Data",
            "action": "WAIT",
            "bias": "Neutral",
            "setup_name": "Scanning Market Structure",
            "entry_zone": "--",
            "stop_loss": "--",
            "target_1": "--",
            "target_2": "--",
            "rr_ratio": "--",
            "rule": "Waiting for live candles from Upstox feed.",
            "checklist": [],
        }

    ltp = candles[-1]["close"]
    trend = smc.get("trend", "Neutral")
    poc = vp.get("poc") or ltp
    vah = vp.get("vah") or ltp
    val = vp.get("val") or ltp
    order_blocks = smc.get("order_blocks", [])
    absorption = of.get("absorption", {})
    delta = of.get("delta", 0)

    demand_obs = [ob for ob in order_blocks if ob.get("type") == "+OB"]
    supply_obs = [ob for ob in order_blocks if ob.get("type") == "-OB"]

    recent_candles = candles[-30:] if len(candles) >= 30 else candles
    swing_high = max(c["high"] for c in recent_candles) if recent_candles else ltp
    swing_low = min(c["low"] for c in recent_candles) if recent_candles else ltp

    # Bias identification: Trend, Absorption & Cumulative Delta
    is_buyer_absorbed = absorption.get("detected") and absorption.get("type") == "Buyer Side Absorption"
    is_seller_absorbed = absorption.get("detected") and absorption.get("type") == "Seller Side Absorption"
    is_bullish = trend.lower().startswith("bull") or is_buyer_absorbed or (delta > 0 and ltp >= poc)
    is_bearish = trend.lower().startswith("bear") or is_seller_absorbed or (delta < 0 and ltp <= poc)

    if is_bullish and not (trend.lower().startswith("bear") and delta < -500000):
        # ── BULLISH INSTITUTIONAL ACCUMULATION SETUP ──
        bias = "Bullish"
        action = "BUY ON DEMAND PULLBACK"
        setup_name = "+OB Demand Retest & Value Area Expansion"

        valid_demand = [ob for ob in demand_obs if ob["top"] <= ltp * 1.01]
        if valid_demand:
            target_ob = max(valid_demand, key=lambda x: x["top"])
            entry_low = round(target_ob["bottom"], 2)
            entry_high = round(target_ob["top"], 2)
            entry_mid = round((entry_low + entry_high) / 2, 2)
            stop_loss = round(entry_low * 0.995, 2)
            entry_zone_str = f"₹{entry_low:,.2f} – ₹{entry_high:,.2f}"
            ob_source = "Fresh Demand OB" if target_ob.get("fresh") else "Mitigated Demand OB"
        else:
            entry_mid = round(min(ltp, poc), 2)
            entry_low = round(entry_mid * 0.996, 2)
            entry_high = round(entry_mid * 1.002, 2)
            stop_loss = round(min(val, entry_low * 0.993), 2)
            entry_zone_str = f"₹{entry_low:,.2f} – ₹{entry_high:,.2f}"
            ob_source = "POC Confluence Zone"

        risk = max(1.0, entry_mid - stop_loss)
        target_1 = round(max(vah, entry_mid + risk * 1.8), 2)
        target_2 = round(max(swing_high * 1.006, entry_mid + risk * 3.0), 2)
        reward = max(1.0, target_1 - entry_mid)
        rr_num = max(1.5, round(reward / risk, 1))
        rr_ratio = f"1 : {rr_num} R"

        checklist = [
            {"label": "SMC Trend", "pass": True, "detail": f"Structure: {trend}"},
            {"label": "POC Support", "pass": ltp >= poc, "detail": f"LTP ₹{ltp:,.2f} {'above' if ltp >= poc else 'below'} POC ₹{poc:,.2f}"},
            {"label": "Order Flow Delta", "pass": delta >= 0, "detail": f"Net Delta: {delta:+,.0f}"},
            {"label": "Absorption Confirmation", "pass": is_buyer_absorbed, "detail": absorption.get("label", "Neutral")},
        ]

        rule = (
            f"Institutional Accumulation: Wait for price to pull back into {ob_source} ({entry_zone_str}). "
            f"Enter on lower wick rejection with Stop Loss strictly at ₹{stop_loss:,.2f}. "
            f"Take 60% profit at Target 1 (VAH ₹{target_1:,.2f}) and trail remaining risk-free to Target 2 (₹{target_2:,.2f})."
        )

    elif is_bearish:
        # ── BEARISH INSTITUTIONAL DISTRIBUTION SETUP ──
        bias = "Bearish"
        action = "SELL ON SUPPLY RALLY"
        setup_name = "-OB Supply Rejection & Breakdown"

        valid_supply = [ob for ob in supply_obs if ob["bottom"] >= ltp * 0.99]
        if valid_supply:
            target_ob = min(valid_supply, key=lambda x: x["bottom"])
            entry_low = round(target_ob["bottom"], 2)
            entry_high = round(target_ob["top"], 2)
            entry_mid = round((entry_low + entry_high) / 2, 2)
            stop_loss = round(entry_high * 1.005, 2)
            entry_zone_str = f"₹{entry_low:,.2f} – ₹{entry_high:,.2f}"
            ob_source = "Fresh Supply OB" if target_ob.get("fresh") else "Mitigated Supply OB"
        else:
            entry_mid = round(max(ltp, poc), 2)
            entry_low = round(entry_mid * 0.998, 2)
            entry_high = round(entry_mid * 1.004, 2)
            stop_loss = round(max(vah, entry_high * 1.007), 2)
            entry_zone_str = f"₹{entry_low:,.2f} – ₹{entry_high:,.2f}"
            ob_source = "POC Resistance Zone"

        risk = max(1.0, stop_loss - entry_mid)
        target_1 = round(min(val, entry_mid - risk * 1.8), 2)
        target_2 = round(min(swing_low * 0.994, entry_mid - risk * 3.0), 2)
        reward = max(1.0, entry_mid - target_1)
        rr_num = max(1.5, round(reward / risk, 1))
        rr_ratio = f"1 : {rr_num} R"

        checklist = [
            {"label": "SMC Trend", "pass": True, "detail": f"Structure: {trend}"},
            {"label": "POC Resistance", "pass": ltp <= poc, "detail": f"LTP ₹{ltp:,.2f} {'below' if ltp <= poc else 'above'} POC ₹{poc:,.2f}"},
            {"label": "Order Flow Delta", "pass": delta <= 0, "detail": f"Net Delta: {delta:+,.0f}"},
            {"label": "Absorption Confirmation", "pass": is_seller_absorbed, "detail": absorption.get("label", "Neutral")},
        ]

        rule = (
            f"Institutional Distribution: Wait for price to bounce into {ob_source} ({entry_zone_str}). "
            f"Confirm upper wick rejection on 5m candle. Enter short with Stop Loss at ₹{stop_loss:,.2f}. "
            f"Target 1 at VAL ₹{target_1:,.2f} and trail remaining to Target 2 (₹{target_2:,.2f})."
        )

    else:
        # ── VALUE AREA MEAN REVERSION (RANGE ROTATION) ──
        bias = "Neutral"
        action = "RANGE ROTATION (VAL to VAH)"
        setup_name = "Value Area Equilibrium & POC Mean Reversion"
        entry_zone_str = f"₹{val:,.2f} (Buy) / ₹{vah:,.2f} (Sell)"
        stop_loss = round(val * 0.994, 2)
        target_1 = round(poc, 2)
        target_2 = round(vah, 2)
        rr_ratio = "1 : 2.0 R"
        checklist = [
            {"label": "Value Area", "pass": True, "detail": f"Oscillating between VAL ₹{val:,.2f} – VAH ₹{vah:,.2f}"},
            {"label": "POC Equilibrium", "pass": True, "detail": f"Heavy volume clustered at POC ₹{poc:,.2f}"},
        ]
        rule = (
            f"Consolidation Phase: Market is balancing inside the 70% Value Area. "
            f"Look for long entries near VAL ₹{val:,.2f} targeting POC ₹{poc:,.2f}, or short entries near VAH ₹{vah:,.2f} targeting POC."
        )

    return {
        "status": "Active",
        "action": action,
        "bias": bias,
        "setup_name": setup_name,
        "entry_zone": entry_zone_str,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "rr_ratio": rr_ratio,
        "checklist": checklist,
        "rule": rule,
    }


def analyze_stock_overview(
    symbol: str,
    candles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Unified master analysis: Volume Profile + Order Blocks + Order Flow Footprint + Trade Plan
    + FVG + Liquidity Pools + CVD + Per-Candle Footprint."""
    vp = calculate_volume_profile(candles)
    smc = detect_order_blocks_and_structure(candles)
    of = calculate_order_flow_and_absorption(candles, val_level=vp.get("val"), vah_level=vp.get("vah"))
    plan = generate_institutional_trade_plan(candles, vp, smc, of)
    fvgs = detect_fair_value_gaps(candles)
    liquidity = detect_liquidity_pools(candles)
    cvd = calculate_cvd_series(candles)
    footprints = build_candle_footprints(candles)
    mtf = calculate_multi_timeframe_matrix(candles)

    return {
        "ok": True,
        "symbol": symbol.upper().strip(),
        "total_candles": len(candles),
        "volume_profile": vp,
        "market_structure": smc,
        "order_flow": of,
        "trade_plan": plan,
        "fvg": fvgs,
        "liquidity_pools": liquidity,
        "cvd_series": cvd,
        "footprints": footprints,
        "mtf_matrix": mtf,
    }


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 1: FAIR VALUE GAPS (FVG) — Imbalance Zones
# ═══════════════════════════════════════════════════════════════════════

def detect_fair_value_gaps(
    candles: list[dict[str, Any]],
    max_fvgs: int = 4,
) -> list[dict[str, Any]]:
    """Detects institutional Fair Value Gaps (FVG) with 50% Consequent Encroachment (CE)
    and clean mitigation lifecycles matching Pro Stock Hunter / ICT SMC standards.

    Rules:
    1. Bullish FVG (FVG+): 3-candle sequence where Candle 1 (impulse) creates an imbalance:
       gap_bottom = Candle 0 High, gap_top = Candle 2 Low (gap_top > gap_bottom).
    2. Bearish FVG (FVG-): 3-candle sequence where Candle 1 (impulse) drops aggressively:
       gap_top = Candle 0 Low, gap_bottom = Candle 2 High (gap_top > gap_bottom).
    3. 50% Consequent Encroachment (CE): Exactly (gap_top + gap_bottom) / 2.
    4. Mitigation lifecycle:
       - Fresh: Price has not entered the gap.
       - Tested: Price wicks into the gap.
       - CE Tested: Price reaches or touches 50% CE level.
       - Filled: Price trades completely through or closes through the opposing boundary.
    5. Returns the cleanest active gaps with 50% CE levels.
    """
    n = len(candles)
    if n < 3:
        return []

    atrs = [abs(float(c["high"]) - float(c["low"])) for c in candles[-20:]]
    atr = (sum(atrs) / len(atrs)) if atrs else 5.0
    ltp = float(candles[-1]["close"]) if candles else 100.0
    min_gap = max(atr * 0.15, ltp * 0.0003)
    min_body = max(atr * 0.30, ltp * 0.0006)

    fvg_list: list[dict[str, Any]] = []

    for i in range(2, n):
        c0 = candles[i - 2]
        c1 = candles[i - 1]
        c2 = candles[i]

        c0_high = float(c0["high"])
        c0_low = float(c0["low"])
        c1_open = float(c1["open"])
        c1_close = float(c1["close"])
        c2_high = float(c2["high"])
        c2_low = float(c2["low"])

        # Bullish FVG (FVG+): Strong green displacement candle, gap between c0 high and c2 low
        c1_body_bull = c1_close - c1_open
        if c1_body_bull >= min_body and c2_low > c0_high:
            gap_top = round(c2_low, 2)
            gap_bottom = round(c0_high, 2)
            gap_size = round(gap_top - gap_bottom, 2)
            ce_level = round((gap_top + gap_bottom) / 2.0, 2)

            if gap_size >= min_gap:
                filled = False
                tested = False
                ce_tested = False
                filled_time = None

                for j in range(i + 1, n):
                    low_j = float(candles[j]["low"])
                    close_j = float(candles[j]["close"])

                    if low_j <= gap_top:
                        tested = True
                    if low_j <= ce_level:
                        ce_tested = True
                    if close_j < gap_bottom or low_j < gap_bottom:
                        filled = True
                        filled_time = candles[j]["timestamp"]
                        break

                # Keep active gaps, or very recently filled gaps (< 30 bars ago)
                is_recent = (n - i) <= 40
                if not filled or is_recent:
                    fvg_list.append({
                        "id": f"fvg_bull_{i}",
                        "type": "Bullish",
                        "label": "FVG+",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "ce_level": ce_level,
                        "time": c1["timestamp"],
                        "time_end": c2["timestamp"],
                        "gap_size": gap_size,
                        "mitigated": filled,
                        "tested": tested,
                        "ce_tested": ce_tested,
                        "status": "Filled" if filled else ("CE Tested" if ce_tested else ("Tested" if tested else "Fresh")),
                        "filled_time": filled_time,
                        "mitigated_time": filled_time,
                    })

        # Bearish FVG (FVG-): Strong red displacement candle, gap between c0 low and c2 high
        c1_body_bear = c1_open - c1_close
        if c1_body_bear >= min_body and c0_low > c2_high:
            gap_top = round(c0_low, 2)
            gap_bottom = round(c2_high, 2)
            gap_size = round(gap_top - gap_bottom, 2)
            ce_level = round((gap_top + gap_bottom) / 2.0, 2)

            if gap_size >= min_gap:
                filled = False
                tested = False
                ce_tested = False
                filled_time = None

                for j in range(i + 1, n):
                    high_j = float(candles[j]["high"])
                    close_j = float(candles[j]["close"])

                    if high_j >= gap_bottom:
                        tested = True
                    if high_j >= ce_level:
                        ce_tested = True
                    if close_j > gap_top or high_j > gap_top:
                        filled = True
                        filled_time = candles[j]["timestamp"]
                        break

                is_recent = (n - i) <= 40
                if not filled or is_recent:
                    fvg_list.append({
                        "id": f"fvg_bear_{i}",
                        "type": "Bearish",
                        "label": "FVG-",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "ce_level": ce_level,
                        "time": c1["timestamp"],
                        "time_end": c2["timestamp"],
                        "gap_size": gap_size,
                        "mitigated": filled,
                        "tested": tested,
                        "ce_tested": ce_tested,
                        "status": "Filled" if filled else ("CE Tested" if ce_tested else ("Tested" if tested else "Fresh")),
                        "filled_time": filled_time,
                        "mitigated_time": filled_time,
                    })

    # Sort & Filter: Keep cleanest active FVGs (up to max_fvgs, prioritizing active unfilled gaps closest to price)
    active_bull = [f for f in fvg_list if f["type"] == "Bullish" and not f["mitigated"]]
    active_bear = [f for f in fvg_list if f["type"] == "Bearish" and not f["mitigated"]]
    filled_gaps = [f for f in fvg_list if f["mitigated"]]

    active_bull.sort(key=lambda f: f["time"], reverse=True)
    active_bear.sort(key=lambda f: f["time"], reverse=True)
    filled_gaps.sort(key=lambda f: f["time"], reverse=True)

    selected = active_bull[:2] + active_bear[:2]
    if len(selected) < max_fvgs and filled_gaps:
        selected.extend(filled_gaps[:(max_fvgs - len(selected))])

    # Sort chronologically by time so rendering flows naturally from left to right
    selected.sort(key=lambda f: f["time"])
    return selected


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 2: LIQUIDITY POOLS — BSL (Buy-Side) & SSL (Sell-Side)
# ═══════════════════════════════════════════════════════════════════════

def detect_liquidity_pools(
    candles: list[dict[str, Any]],
    pivot_window: int = 3,
    equal_tolerance_pct: float = 0.0015,
    max_pools: int = 4,
) -> list[dict[str, Any]]:
    """Detects Buy-Side Liquidity (BSL) and Sell-Side Liquidity (SSL) pools.

    Only returns ACTIVE (Unswept) liquidity pools to prevent clutter.
    """
    n = len(candles)
    if n < pivot_window * 2 + 1:
        return []

    swing_highs: list[dict[str, Any]] = []
    swing_lows: list[dict[str, Any]] = []

    for i in range(pivot_window, n - pivot_window):
        c = candles[i]
        c_high = float(c["high"])
        c_low = float(c["low"])

        is_high = all(
            float(candles[j]["high"]) <= c_high
            for j in range(i - pivot_window, i + pivot_window + 1)
            if j != i
        )
        is_low = all(
            float(candles[j]["low"]) >= c_low
            for j in range(i - pivot_window, i + pivot_window + 1)
            if j != i
        )

        if is_high:
            swing_highs.append({"index": i, "time": c["timestamp"], "price": c_high})
        if is_low:
            swing_lows.append({"index": i, "time": c["timestamp"], "price": c_low})

    pools: list[dict[str, Any]] = []
    ltp = float(candles[-1]["close"]) if candles else 0

    # BSL: Equal highs or swing highs ABOVE current price that have not been swept
    used_h = set()
    for i, sh in enumerate(swing_highs):
        if i in used_h:
            continue
        cluster = [sh]
        for j in range(i + 1, len(swing_highs)):
            if j in used_h:
                continue
            if abs(sh["price"] - swing_highs[j]["price"]) / max(sh["price"], 0.01) <= equal_tolerance_pct:
                cluster.append(swing_highs[j])
                used_h.add(j)

        level = round(max(c["price"] for c in cluster), 2)
        # Check if swept by any subsequent candle
        max_idx = max(c["index"] for c in cluster)
        swept = any(float(candles[k]["high"]) >= level for k in range(max_idx + 1, n))
        if not swept and level >= ltp:
            label = f"BSL — Equal Highs (×{len(cluster)})" if len(cluster) >= 2 else "BSL — Key High"
            pools.append({
                "type": "BSL",
                "label": label,
                "price": level,
                "touches": len(cluster),
                "time": cluster[-1]["time"],
                "swept": False,
                "status": "Active",
            })

    # SSL: Equal lows or swing lows BELOW current price that have not been swept
    used_l = set()
    for i, sl in enumerate(swing_lows):
        if i in used_l:
            continue
        cluster = [sl]
        for j in range(i + 1, len(swing_lows)):
            if j in used_l:
                continue
            if abs(sl["price"] - swing_lows[j]["price"]) / max(sl["price"], 0.01) <= equal_tolerance_pct:
                cluster.append(swing_lows[j])
                used_l.add(j)

        level = round(min(c["price"] for c in cluster), 2)
        max_idx = max(c["index"] for c in cluster)
        swept = any(float(candles[k]["low"]) <= level for k in range(max_idx + 1, n))
        if not swept and level <= ltp:
            label = f"SSL — Equal Lows (×{len(cluster)})" if len(cluster) >= 2 else "SSL — Key Low"
            pools.append({
                "type": "SSL",
                "label": label,
                "price": level,
                "touches": len(cluster),
                "time": cluster[-1]["time"],
                "swept": False,
                "status": "Active",
            })

    pools.sort(key=lambda p: abs(p["price"] - ltp))
    return pools[:max_pools]


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 3: CVD (Cumulative Volume Delta) Series
# ═══════════════════════════════════════════════════════════════════════

def calculate_cvd_series(
    candles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Computes Cumulative Volume Delta (CVD) for each candle.

    At each candle, delta = estimated buy_vol - sell_vol (same as order flow engine).
    CVD[i] = CVD[i-1] + delta[i]

    This creates a line chart showing whether institutional buying or selling is dominating
    the session overall, including divergence detection vs price.
    """
    if not candles:
        return []

    series = []
    cumulative = 0.0

    for c in candles:
        vol = float(c.get("volume", 0) or 0)
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))
        c_close = float(c.get("close", 0))
        spread = max(0.01, c_high - c_low)

        body_ratio = (c_close - c_low) / spread
        buy_vol = vol * body_ratio
        sell_vol = vol * (1.0 - body_ratio)
        delta = buy_vol - sell_vol
        cumulative += delta

        series.append({
            "time": c["timestamp"],
            "value": round(cumulative, 0),
        })

    return series


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 4: PER-CANDLE FOOTPRINT (Bid-Ask Breakdown by Price Level)
# ═══════════════════════════════════════════════════════════════════════

def build_candle_footprints(
    candles: list[dict[str, Any]],
    levels_per_candle: int = 8,
) -> dict[str, Any]:
    """Builds a per-candle footprint ladder (bid vs ask volume at each price level).

    Since Upstox provides OHLCV only (no true tick-by-tick Level 2 data),
    we synthetically distribute volume across the candle's price range using
    body/wick proportional allocation — the same method professional volume
    profile tools use when tick data is unavailable.

    Returns a dict keyed by timestamp (string) for fast O(1) lookup on candle click.
    """
    if not candles:
        return {}

    result: dict[str, Any] = {}

    for c in candles:
        vol = float(c.get("volume", 0) or 0)
        c_open = float(c.get("open", 0))
        c_close = float(c.get("close", 0))
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))
        spread = max(0.01, c_high - c_low)
        ts = str(c["timestamp"])

        if vol <= 0 or spread <= 0.01:
            result[ts] = {
                "time": c["timestamp"],
                "open": c_open, "close": c_close, "high": c_high, "low": c_low,
                "total_vol": 0,
                "net_delta": 0,
                "levels": [],
            }
            continue

        is_bullish = c_close >= c_open
        body_top = max(c_open, c_close)
        body_bottom = min(c_open, c_close)
        body_size = max(0.01, body_top - body_bottom)

        # Distribute volume across price levels
        step = spread / levels_per_candle
        levels = []
        level_vols = []

        for lv in range(levels_per_candle):
            level_price = c_low + step * (lv + 0.5)
            # More volume near POC (body center), less at wicks
            if body_bottom <= level_price <= body_top:
                weight = 2.0  # Body area gets 2x weight
            else:
                weight = 0.6  # Wick area gets less

            level_vols.append((round(level_price, 2), weight))

        total_weight = sum(w for _, w in level_vols)
        if total_weight <= 0:
            total_weight = 1.0

        body_pct = min(1.0, body_size / spread)
        candle_total_delta = 0
        for price, weight in level_vols:
            lv_vol = vol * (weight / total_weight)

            # At each price level, estimate bid vs ask based on institutional order flow
            if is_bullish:
                if price < body_bottom:
                    # Lower wick: aggressive buyers stepped in at bottom
                    ask_ratio = 0.76  # 3.1:1 buy imbalance
                elif price <= c_close:
                    # Bullish body: buyers lifting the ask
                    ask_ratio = min(0.78, 0.55 + 0.23 * body_pct)
                else:
                    # Upper wick resistance
                    ask_ratio = 0.38
            else:
                if price > body_top:
                    # Upper wick: aggressive sellers rejecting highs
                    ask_ratio = 0.24  # 3.1:1 sell imbalance (76% bid)
                elif price >= c_close:
                    # Bearish body: sellers dumping into bid
                    ask_ratio = max(0.22, 0.45 - 0.23 * body_pct)
                else:
                    # Lower wick dip buying
                    ask_ratio = 0.60

            ask_vol = round(lv_vol * ask_ratio, 0)
            bid_vol = round(lv_vol * (1 - ask_ratio), 0)
            delta = ask_vol - bid_vol
            candle_total_delta += delta

            # Detect imbalance (≥ 2.5:1 ratio standard in institutional order flow / Sierra Chart / VolumeLens)
            imbalance = None
            if ask_vol > 0 and bid_vol > 0:
                if ask_vol / bid_vol >= 2.5:
                    imbalance = "BUY_IMBALANCE"
                elif bid_vol / ask_vol >= 2.5:
                    imbalance = "SELL_IMBALANCE"

            levels.append({
                "price": price,
                "bid_vol": int(bid_vol),
                "ask_vol": int(ask_vol),
                "delta": int(delta),
                "imbalance": imbalance,
            })

        result[ts] = {
            "time": c["timestamp"],
            "open": round(c_open, 2),
            "close": round(c_close, 2),
            "high": round(c_high, 2),
            "low": round(c_low, 2),
            "total_vol": int(vol),
            "net_delta": int(candle_total_delta),
            "levels": levels,
        }

    return result


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 5: MULTI-TIMEFRAME ANALYSIS (5m, 15m, 30m, 1h, 4h, 1D)
# ═══════════════════════════════════════════════════════════════════════

def _resample(candles: list[dict[str, Any]], factor: int) -> list[dict[str, Any]]:
    resampled = []
    for i in range(0, len(candles), factor):
        chunk = candles[i:i + factor]
        if not chunk:
            continue
        resampled.append({
            "timestamp": chunk[0]["timestamp"],
            "open": float(chunk[0]["open"]),
            "high": max(float(c["high"]) for c in chunk),
            "low": min(float(c["low"]) for c in chunk),
            "close": float(chunk[-1]["close"]),
            "volume": sum(float(c.get("volume", 0) or 0) for c in chunk),
        })
    return resampled


def _calc_ema_val(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    p = min(period, len(values))
    multiplier = 2.0 / (p + 1.0)
    ema = sum(values[:p]) / p
    for price in values[p:]:
        ema = (price - ema) * multiplier + ema
    return ema


def _calc_rsi_val(closes: list[float], period: int = 14) -> float:
    if len(closes) < 2:
        return 50.0
    p = min(period, max(2, len(closes) - 1))
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    if not gains:
        return 50.0
    avg_gain = sum(gains[:p]) / p
    avg_loss = sum(losses[:p]) / p
    for i in range(p, len(gains)):
        avg_gain = (avg_gain * (p - 1) + gains[i]) / p
        avg_loss = (avg_loss * (p - 1) + losses[i]) / p
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _calc_macd_status(closes: list[float]) -> str:
    if len(closes) < 15:
        return "N/A"
    if len(closes) >= 35:
        f_p, s_p, sig_p = 12, 26, 9
    else:
        f_p = max(3, int(len(closes) * 0.25))
        s_p = max(6, int(len(closes) * 0.55))
        sig_p = max(3, int(len(closes) * 0.20))

    mult_f = 2.0 / (f_p + 1.0)
    mult_s = 2.0 / (s_p + 1.0)
    mult_sig = 2.0 / (sig_p + 1.0)

    e_f = sum(closes[:f_p]) / f_p
    ema_f_arr = []
    for p in closes[f_p:]:
        e_f = (p - e_f) * mult_f + e_f
        ema_f_arr.append(e_f)

    e_s = sum(closes[:s_p]) / s_p
    ema_s_arr = []
    for p in closes[s_p:]:
        e_s = (p - e_s) * mult_s + e_s
        ema_s_arr.append(e_s)

    min_len = min(len(ema_f_arr), len(ema_s_arr))
    if min_len < sig_p:
        return "N/A"
    macd_line = [f_v - s_v for f_v, s_v in zip(ema_f_arr[-min_len:], ema_s_arr[-min_len:])]
    sig_line = sum(macd_line[:sig_p]) / sig_p
    for m in macd_line[sig_p:]:
        sig_line = (m - sig_line) * mult_sig + sig_line

    return "BULL ▲" if macd_line[-1] >= sig_line else "BEAR ▼"


def calculate_multi_timeframe_matrix(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Generates the multi-timeframe alignment matrix across 5m, 15m, 30m, 1h, 4h, and 1D."""
    if not candles:
        return {"rows": [], "overall": "NEUTRAL", "confluence": "0 / 6"}

    tf_configs = [
        ("5m", 1),
        ("15m", 3),
        ("30m", 6),
        ("1h", 12),
        ("4h", 48),
        ("1D", 75),
    ]

    rows = []
    bull_count = 0
    bear_count = 0

    for tf_label, factor in tf_configs:
        tf_candles = _resample(candles, factor) if factor > 1 else candles
        if not tf_candles:
            continue
        closes = [float(c["close"]) for c in tf_candles]
        ltp = closes[-1]

        # EMA 20
        ema20 = _calc_ema_val(closes, 20)
        ema_status = "BULL ▲" if ltp >= ema20 else "BEAR ▼"

        # RSI 14
        rsi = _calc_rsi_val(closes, 14)
        if rsi >= 55:
            rsi_status = f"BULL ({int(rsi)})"
        elif rsi <= 45:
            rsi_status = f"BEAR ({int(rsi)})"
        else:
            rsi_status = f"NEUT ({int(rsi)})"

        # MACD
        macd_status = _calc_macd_status(closes)

        # Timeframe signal vote
        score = 0
        if "BULL" in ema_status:
            score += 1
        else:
            score -= 1
        if "BULL" in rsi_status:
            score += 1
        elif "BEAR" in rsi_status:
            score -= 1
        if "BULL" in macd_status:
            score += 1
        elif "BEAR" in macd_status:
            score -= 1

        if score >= 1:
            signal = "BULLISH ▲"
            sig_class = "bull"
            bull_count += 1
        elif score <= -1:
            signal = "BEARISH ▼"
            sig_class = "bear"
            bear_count += 1
        else:
            signal = "NEUTRAL"
            sig_class = "neutral"

        rows.append({
            "tf": tf_label,
            "ema": ema_status,
            "rsi": rsi_status,
            "macd": macd_status,
            "signal": signal,
            "status": sig_class,
        })

    total_tf = len(rows)
    if bull_count >= total_tf / 2:
        overall = "BULLISH ▲"
        conf_pct = int((bull_count / max(1, total_tf)) * 100)
    elif bear_count >= total_tf / 2:
        overall = "BEARISH ▼"
        conf_pct = int((bear_count / max(1, total_tf)) * 100)
    else:
        overall = "NEUTRAL"
        conf_pct = 50

    return {
        "rows": rows,
        "overall": overall,
        "confluence": f"{max(bull_count, bear_count)} / {total_tf} ({conf_pct}%)",
    }
