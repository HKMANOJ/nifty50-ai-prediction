#!/usr/bin/env python3
"""SMC, Volume Profile & Order Flow Engine for NIFTY 50 / NSE Stocks.

Calculates:
1. Session Volume Profile (POC, VAH, VAL, and price-volume histogram).
2. Smart Money Concepts (Swing Pivots, BOS, CHoCH, and Bullish/Bearish Order Blocks).
3. Order Flow Delta & Buyer/Seller Absorption Detection.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


FIVE_MINUTES = 300
INDIA_TZ = ZoneInfo("Asia/Kolkata")


def _estimate_bar_flow(
    c: dict[str, Any],
    atr: float,
    allow_synthetic: bool = True,
) -> tuple[float, float, float, float, str]:
    """Computes consistent (vol, buy_vol, sell_vol, delta, vol_source) for a candle.
    Preserves real spot/futures volume whenever > 0 or when volume_source is already futures/spot
    (so quiet 0-vol futures bars stay 0 instead of injecting synthetic spikes).
    """
    raw_vol = float(c.get("volume", 0) or 0)
    explicit_source = str(c.get("volume_source") or "")
    c_high = float(c.get("high", 0) or 0)
    c_low = float(c.get("low", 0) or 0)
    c_close = float(c.get("close", 0) or 0)
    spread = max(0.01, c_high - c_low)
    if raw_vol > 0:
        vol = raw_vol
        vol_source = explicit_source or "real"
    elif explicit_source == "futures" or not allow_synthetic:
        vol = 0.0
        vol_source = explicit_source or "futures"
    else:
        vol = max(10000.0, round(45000.0 * (spread / max(atr, 0.5)), 0))
        vol_source = "estimated"
    body_ratio = max(0.0, min(1.0, (c_close - c_low) / spread))
    buy_vol = vol * body_ratio
    sell_vol = vol * (1.0 - body_ratio)
    delta = buy_vol - sell_vol
    return vol, buy_vol, sell_vol, delta, vol_source


def _group_1m_by_5m(candles_1m: list[dict[str, Any]] | None) -> dict[int, list[dict[str, Any]]]:
    """Buckets 1-minute candles into their parent 5-minute epoch timestamp."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for m in candles_1m or []:
        ts = m.get("timestamp")
        if ts is None:
            continue
        t_int = int(ts)
        bucket = t_int - (t_int % FIVE_MINUTES)
        grouped.setdefault(bucket, []).append(m)
    return grouped


def _compute_candle_footprint_and_totals(
    c: dict[str, Any],
    sub_1m: list[dict[str, Any]] | None,
    atr: float,
    levels_per_candle: int = 8,
    allow_synthetic: bool = True,
) -> tuple[int, int, int, int, str, str, list[dict[str, Any]]]:
    """Single source of truth for per-candle Volume, Buy/Sell Split, Delta, and Price-Level Footprint.
    Uses 1m sub-bars when >=3 active 1m volume bars exist inside the 5m bucket; falls back cleanly to
    full-bar OHLC profile on thin futures bars so illiquid ticks don't invert directional delta.
    """
    vol_raw, _, _, _, vol_source = _estimate_bar_flow(c, atr, allow_synthetic=allow_synthetic)
    vol = max(0, int(round(vol_raw)))
    c_open = float(c.get("open", 0))
    c_close = float(c.get("close", 0))
    c_high = float(c.get("high", 0))
    c_low = float(c.get("low", 0))
    spread = max(0.01, c_high - c_low)
    step = spread / levels_per_candle
    is_bullish = c_close >= c_open
    body_top = max(c_open, c_close)
    body_bottom = min(c_open, c_close)
    body_pct = min(1.0, max(0.01, body_top - body_bottom) / spread)

    level_prices = [round(c_low + step * (lv + 0.5), 2) for lv in range(levels_per_candle)]
    raw_ask = [0.0] * levels_per_candle
    raw_bid = [0.0] * levels_per_candle

    valid_1m = [m for m in (sub_1m or []) if float(m.get("high", 0)) >= float(m.get("low", 0))]
    active_1m_bars = [m for m in valid_1m if float(m.get("volume", 0) or 0) > 0]
    use_1m_subbar = (len(active_1m_bars) >= 3) and vol > 0

    if use_1m_subbar:
        delta_method = "1m_subbar"
        for m in valid_1m:
            m_low = float(m.get("low", c_low))
            m_high = float(m.get("high", c_high))
            m_close = float(m.get("close", m_low))
            m_vol = float(m.get("volume", 0) or 0)
            if m_vol <= 0:
                continue
            m_spread = max(0.01, m_high - m_low)
            close_pos = max(0.0, min(1.0, (m_close - m_low) / m_spread))
            m_ask_ratio = max(0.18, min(0.82, 0.22 + 0.56 * close_pos))
            start_lv = max(0, min(levels_per_candle - 1, int((m_low - c_low) / step)))
            end_lv = max(0, min(levels_per_candle - 1, int((m_high - c_low) / step)))
            lv_count = max(1, end_lv - start_lv + 1)
            per_lv_vol = m_vol / lv_count
            for lv in range(start_lv, end_lv + 1):
                raw_ask[lv] += per_lv_vol * m_ask_ratio
                raw_bid[lv] += per_lv_vol * (1.0 - m_ask_ratio)

        raw_sum = sum(raw_ask) + sum(raw_bid)
        if raw_sum > 0:
            scale = vol / raw_sum
            raw_ask = [v * scale for v in raw_ask]
            raw_bid = [v * scale for v in raw_bid]
    else:
        delta_method = "ohlc_profile"
        weights = [2.0 if body_bottom <= lp <= body_top else 0.6 for lp in level_prices]
        total_weight = sum(weights) or 1.0
        for lv, (lp, w) in enumerate(zip(level_prices, weights)):
            lv_vol = vol * (w / total_weight)
            if is_bullish:
                if lp < body_bottom:
                    ask_ratio = 0.76
                elif lp <= c_close:
                    ask_ratio = min(0.78, 0.55 + 0.23 * body_pct)
                else:
                    ask_ratio = 0.38
            else:
                if lp > body_top:
                    ask_ratio = 0.24
                elif lp >= c_close:
                    ask_ratio = max(0.22, 0.45 - 0.23 * body_pct)
                else:
                    ask_ratio = 0.60
            raw_ask[lv] = lv_vol * ask_ratio
            raw_bid[lv] = lv_vol * (1.0 - ask_ratio)

    levels: list[dict[str, Any]] = []
    total_ask = 0
    total_bid = 0
    for lp, a_f, b_f in zip(level_prices, raw_ask, raw_bid):
        a_i = int(round(a_f))
        b_i = int(round(b_f))
        d_i = a_i - b_i
        total_ask += a_i
        total_bid += b_i
        imbalance = None
        if a_i > 0 and b_i > 0:
            if a_i / b_i >= 2.5:
                imbalance = "BUY_IMBALANCE"
            elif b_i / a_i >= 2.5:
                imbalance = "SELL_IMBALANCE"
        levels.append({
            "price": lp,
            "bid_vol": b_i,
            "ask_vol": a_i,
            "delta": d_i,
            "imbalance": imbalance,
        })

    # Exact integer reconciliation (both +diff and -diff) so sum(ask + bid) == vol
    exact_vol = total_ask + total_bid
    if vol == 0:
        for lv_d in levels:
            lv_d["ask_vol"] = 0
            lv_d["bid_vol"] = 0
            lv_d["delta"] = 0
            lv_d["imbalance"] = None
        total_ask = 0
        total_bid = 0
        exact_vol = 0
    elif exact_vol != vol and levels:
        diff = vol - exact_vol
        step_dir = 1 if diff > 0 else -1
        rem = abs(diff)
        order = sorted(
            range(len(levels)),
            key=lambda idx: levels[idx]["ask_vol"] + levels[idx]["bid_vol"],
            reverse=True,
        )
        ptr = 0
        while rem > 0 and order:
            idx = order[ptr % len(order)]
            lv_d = levels[idx]
            if step_dir > 0:
                if lv_d["ask_vol"] >= lv_d["bid_vol"]:
                    lv_d["ask_vol"] += 1
                    total_ask += 1
                else:
                    lv_d["bid_vol"] += 1
                    total_bid += 1
                rem -= 1
            else:
                if lv_d["bid_vol"] >= lv_d["ask_vol"] and lv_d["bid_vol"] > 0:
                    lv_d["bid_vol"] -= 1
                    total_bid -= 1
                    rem -= 1
                elif lv_d["ask_vol"] > 0:
                    lv_d["ask_vol"] -= 1
                    total_ask -= 1
                    rem -= 1
            lv_d["delta"] = lv_d["ask_vol"] - lv_d["bid_vol"]
            ptr += 1
            if ptr > len(order) * (rem + 4):
                break
        exact_vol = total_ask + total_bid

    net_delta = total_ask - total_bid
    return exact_vol, total_ask, total_bid, net_delta, vol_source, delta_method, levels


def compute_latest_delta_bar(
    candles: list[dict[str, Any]],
    lookback: int = 14,
    candles_1m: list[dict[str, Any]] | None = None,
) -> float:
    """Computes the latest completed candle's order-flow delta consistently across scanner & alerts."""
    if not candles:
        return 0.0
    tail = candles[-lookback:]
    trs = [max(float(c.get("high", 0)) - float(c.get("low", 0)), 0.01) for c in tail]
    atr = (sum(trs) / len(trs)) if trs else 1.0
    grouped_1m = _group_1m_by_5m(candles_1m)
    last_c = candles[-1]
    _, _, _, net_delta, _, _, _ = _compute_candle_footprint_and_totals(
        last_c, grouped_1m.get(int(last_c["timestamp"])), atr
    )
    return float(net_delta)


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

    trs = [max(float(c.get("high", 0)) - float(c.get("low", 0)), 0.01) for c in candles]
    atr = (sum(trs) / len(trs)) if trs else 1.0
    allow_synthetic = not any(
        float(c.get("volume", 0) or 0) > 0 or c.get("volume_source") == "futures"
        for c in candles
    )

    # Distribute each candle's volume across the bins it spans
    for c in candles:
        c_low = float(c.get("low", 0))
        c_high = float(c.get("high", 0))
        vol, _, _, _, _ = _estimate_bar_flow(c, atr, allow_synthetic=allow_synthetic)

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
    if n < 2:
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
                if trend.lower().startswith("bear"):
                    # Breaking above previous lower high -> Bullish CHoCH (Trend Reversal)
                    choch_list.append({
                        "time": sw["time"],
                        "price": sw["price"],
                        "broken_level": last_high["price"],
                        "direction": "bullish",
                        "type": "CHoCH",
                    })
                    trend = "Bullish Reversal"
                else:
                    # Bullish BOS (Higher High Continuation)
                    bos_list.append({
                        "time": sw["time"],
                        "price": sw["price"],
                        "broken_level": last_high["price"],
                        "direction": "bullish",
                        "type": "BOS",
                    })
                    trend = "Bullish"
            elif last_high is not None and sw["price"] < last_high["price"] and trend == "Bullish":
                # Lower high in uptrend -> early warning
                pass
            last_high = sw
        elif sw["type"] == "LOW":
            if last_low is not None and sw["price"] < last_low["price"]:
                if trend.lower().startswith("bull"):
                    # Breaking below previous higher low -> Bearish CHoCH (Trend Reversal)
                    choch_list.append({
                        "time": sw["time"],
                        "price": sw["price"],
                        "broken_level": last_low["price"],
                        "direction": "bearish",
                        "type": "CHoCH",
                    })
                    trend = "Bearish Reversal"
                else:
                    # Bearish BOS (Lower Low Continuation)
                    bos_list.append({
                        "time": sw["time"],
                        "price": sw["price"],
                        "broken_level": last_low["price"],
                        "direction": "bearish",
                        "type": "BOS",
                    })
                    trend = "Bearish"
            elif last_low is not None and sw["price"] > last_low["price"] and trend == "Bearish":
                # Higher low in downtrend -> potential base, but structure still bearish until lower high breaks
                pass
            last_low = sw

    # 3. Order Block Detection (Institutional SMC: Demand & Supply Zones)
    # Bullish OB (DEMAND): Origin pivot low candle before an impulsive rally that creates displacement/FVG/BOS
    # Bearish OB (BEAR OB): Origin pivot high candle before an impulsive drop that causes breakdown
    order_blocks = []
    ranges = [c["high"] - c["low"] for c in candles]
    session_start = 0
    for i in range(n - 1):
        c_prev = candles[i]
        c_curr = candles[i + 1]
        origin = c_prev["timestamp"]
        if i and origin - candles[i - 1]["timestamp"] > 60 * 60:
            session_start = i
        if c_curr["timestamp"] != origin + FIVE_MINUTES:
            continue

        # Freeze the displacement baseline at formation. Later volatility must
        # not add or erase a previously confirmed base on the same candles.
        base_ranges = ranges[max(session_start, i - 13):i + 1]
        atr = sum(base_ranges) / len(base_ranges)
        if ranges[i] <= 0:
            continue
        # A strong first impulse can confirm the base at its close. A weaker
        # move needs a consecutive second candle with no significant price gap.
        c_next = candles[i + 2] if i + 2 < n else None
        if c_next is not None and (
            c_next["timestamp"] != c_curr["timestamp"] + FIVE_MINUTES
            or abs(c_next["open"] - c_curr["close"]) > atr * 0.15
        ):
            c_next = None

        # Check for Strong Bullish Displacement (Demand Setup)
        # Origin candle: bearish or tight base at a pivot low
        is_pivot_low = (c_prev["close"] <= c_prev["open"]
                        or (i > session_start and c_prev["low"] <= candles[i - 1]["low"]))
        curr_body_bull = c_curr["close"] - c_curr["open"]
        is_bull_displacement = (
            is_pivot_low and
            c_curr["close"] > c_curr["open"] and
            (
                (curr_body_bull >= atr * 0.6 and c_curr["high"] - c_prev["low"] >= atr) or
                (c_next is not None and c_next["close"] > c_next["open"] and
                 c_next["close"] - c_prev["low"] >= atr * 1.5 and
                 c_next["close"] - c_curr["open"] >= atr * 0.9)
            )
        )

        if is_bull_displacement:
            ob_top = round(float(c_prev["high"]), 2)
            ob_bottom = round(float(min(c_prev["low"], c_curr["low"])), 2)

            # VPOC: High-volume absorption level inside base candle (lower-to-mid 40% typical price)
            base_typ = (c_prev["open"] + c_prev["close"] + c_prev["low"] * 2) / 4.0
            vpoc = round(float(base_typ), 2)
            if vpoc < ob_bottom or vpoc > ob_top:
                vpoc = round(ob_bottom + (ob_top - ob_bottom) * 0.40, 2)

            # Order Flow Grade: Confirm strong displacement body & futures/equity volume expansion
            local_vols = [
                float(candles[j].get("volume") or 0)
                for j in range(max(session_start, i - 13), i + 1)
                if float(candles[j].get("volume") or 0) > 0
            ]
            avg_local_vol = (sum(local_vols) / len(local_vols)) if local_vols else 0.0
            curr_vol = float(c_curr.get("volume") or 0.0)
            vol_confirmed = avg_local_vol > 0 and curr_vol >= avg_local_vol * 1.15
            flow_grade = "A+" if (curr_body_bull >= atr * 0.8 or (curr_body_bull >= atr * 0.65 and vol_confirmed)) else "A"

            mitigated = False
            tested = False
            mitigated_time = None
            tested_time = None
            retest_defended = False

            # Evaluation strictly begins on subsequent candles after displacement
            eval_start = i + 2

            for f in range(eval_start, n):
                c_f = candles[f]
                low_f = float(c_f["low"])
                high_f = float(c_f["high"])
                close_f = float(c_f["close"])

                # 1. Mitigation evaluated first: candle closes below demand base or deeply penetrates
                if close_f < ob_bottom or low_f < ob_bottom - (atr * 0.20):
                    mitigated = True
                    mitigated_time = c_f["timestamp"]
                    retest_defended = False
                    break

                # 2. Retest: price pulls back meaningfully into the zone (at least 15% penetration or candle body inside)
                if not tested:
                    retest_threshold = max(atr * 0.05, (ob_top - ob_bottom) * 0.15)
                    if (low_f <= ob_top - retest_threshold) or (close_f <= ob_top and close_f >= ob_bottom):
                        tested = True
                        tested_time = c_f["timestamp"]
                        # Institutional Defense: price held above VPOC and closed in upper half
                        if close_f >= vpoc:
                            retest_defended = True

            order_blocks.append({
                "id": f"ob_bull_{i}",
                "type": "BULLISH",
                "label": "DEMAND",
                "top": ob_top,
                "bottom": ob_bottom,
                "vpoc": vpoc,
                "trigger_line": ob_top,
                "grade": flow_grade,
                "retest_defense": "DEFENDED" if (tested and not mitigated and retest_defended) else ("PENDING" if not tested else "TESTED"),
                "time": c_prev["timestamp"],
                "formation_time": origin + FIVE_MINUTES,
                "tested_time": tested_time,
                "status": "Mitigated" if mitigated else ("Tested" if tested else "Fresh"),
                "mitigated": mitigated,
                "tested": tested,
                "mitigated_time": mitigated_time,
            })

        # Check for Strong Bearish Displacement (Supply Setup)
        # Origin candle: bullish or tight base at a pivot high
        is_pivot_high = (c_prev["close"] >= c_prev["open"]
                         or (i > session_start and c_prev["high"] >= candles[i - 1]["high"]))
        curr_body_bear = c_curr["open"] - c_curr["close"]
        is_bear_displacement = (
            is_pivot_high and
            c_curr["close"] < c_curr["open"] and
            (
                (curr_body_bear >= atr * 0.6 and c_prev["high"] - c_curr["low"] >= atr) or
                (c_next is not None and c_next["close"] < c_next["open"] and
                 c_prev["high"] - c_next["close"] >= atr * 1.5 and
                 c_curr["open"] - c_next["close"] >= atr * 0.9)
            )
        )

        if is_bear_displacement:
            ob_top = round(float(max(c_prev["high"], c_curr["high"])), 2)
            ob_bottom = round(float(c_prev["low"]), 2)

            # VPOC: High-volume distribution level inside base candle (upper 40% typical price)
            base_typ = (c_prev["open"] + c_prev["close"] + c_prev["high"] * 2) / 4.0
            vpoc = round(float(base_typ), 2)
            if vpoc < ob_bottom or vpoc > ob_top:
                vpoc = round(ob_top - (ob_top - ob_bottom) * 0.40, 2)

            local_vols = [
                float(candles[j].get("volume") or 0)
                for j in range(max(session_start, i - 13), i + 1)
                if float(candles[j].get("volume") or 0) > 0
            ]
            avg_local_vol = (sum(local_vols) / len(local_vols)) if local_vols else 0.0
            curr_vol = float(c_curr.get("volume") or 0.0)
            vol_confirmed = avg_local_vol > 0 and curr_vol >= avg_local_vol * 1.15
            flow_grade = "A+" if (curr_body_bear >= atr * 0.8 or (curr_body_bear >= atr * 0.65 and vol_confirmed)) else "A"

            mitigated = False
            tested = False
            mitigated_time = None
            tested_time = None
            retest_defended = False

            eval_start = i + 2

            for f in range(eval_start, n):
                c_f = candles[f]
                low_f = float(c_f["low"])
                high_f = float(c_f["high"])
                close_f = float(c_f["close"])

                # 1. Mitigation evaluated first: candle closes above supply roof, or deeply penetrates
                if close_f > ob_top or high_f > ob_top + (atr * 0.20):
                    mitigated = True
                    mitigated_time = c_f["timestamp"]
                    retest_defended = False
                    break

                # 2. Retest: price rallies meaningfully into the zone (at least 15% penetration or candle body inside)
                if not tested:
                    retest_threshold = max(atr * 0.05, (ob_top - ob_bottom) * 0.15)
                    if (high_f >= ob_bottom + retest_threshold) or (close_f >= ob_bottom and close_f <= ob_top):
                        tested = True
                        tested_time = c_f["timestamp"]
                        if close_f <= vpoc:
                            retest_defended = True

            order_blocks.append({
                "id": f"ob_bear_{i}",
                "type": "BEARISH",
                "label": "BEAR OB",
                "top": ob_top,
                "bottom": ob_bottom,
                "vpoc": vpoc,
                "trigger_line": ob_bottom,
                "grade": flow_grade,
                "retest_defense": "DEFENDED" if (tested and not mitigated and retest_defended) else ("PENDING" if not tested else "TESTED"),
                "time": c_prev["timestamp"],
                "formation_time": origin + FIVE_MINUTES,
                "tested_time": tested_time,
                "status": "Mitigated" if mitigated else ("Tested" if tested else "Fresh"),
                "mitigated": mitigated,
                "tested": tested,
                "mitigated_time": mitigated_time,
            })

    # Filter & Deduplicate: Keep cleanest non-overlapping Order Blocks
    # Prioritize today's session zones first so active intraday zones are never displaced by historical zones.
    session_date = datetime.fromtimestamp(candles[-1]["timestamp"], tz=INDIA_TZ).date() if candles else None

    def _dedup_obs(obs_subset: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not obs_subset:
            return []
        def _sort_key(o: dict[str, Any]) -> tuple:
            o_time = o.get("time") or o.get("formation_time") or 0
            is_today = 0 if (session_date and datetime.fromtimestamp(o_time, tz=INDIA_TZ).date() == session_date) else 1
            priority = {"Fresh": 0, "Tested": 1, "Mitigated": 2}
            prio = priority.get(o.get("status"), 2)
            return (is_today, prio, -o_time)

        ordered = sorted(obs_subset, key=_sort_key)

        chosen: list[dict[str, Any]] = []
        today_count = 0
        hist_count = 0
        for cand in ordered:
            c_time = cand.get("time") or cand.get("formation_time") or 0
            c_is_today = bool(session_date and datetime.fromtimestamp(c_time, tz=INDIA_TZ).date() == session_date)
            if c_is_today and today_count >= 5:
                continue
            if not c_is_today and hist_count >= 2:
                continue

            cand_top = max(cand["top"], cand["bottom"])
            cand_bot = min(cand["top"], cand["bottom"])
            # Skip invalid zero-height auction settlement anomalies
            if cand_top - cand_bot < 0.5:
                continue

            # Real physical overlap: only reject if the zones geometrically intersect
            is_overlap = any(
                max(cand_bot, min(x["top"], x["bottom"])) < min(cand_top, max(x["top"], x["bottom"]))
                for x in chosen
            )
            if not is_overlap:
                chosen.append(cand)
                if c_is_today:
                    today_count += 1
                else:
                    hist_count += 1
        return chosen

    bull_chosen = _dedup_obs([ob for ob in order_blocks if ob["type"] == "BULLISH"])
    bear_chosen = _dedup_obs([ob for ob in order_blocks if ob["type"] == "BEARISH"])

    # Cross-type anti-collision: Resolve opposing zone collisions (e.g. Demand and Bear OB directly overlapping)
    # Prioritizes the prevailing trend-aligned zone, then Freshness, then recency so charts never show colliding double boxes.
    is_bull_trend = trend.lower().startswith("bull")
    all_candidates = bull_chosen + bear_chosen

    def _overall_prio(o: dict[str, Any]) -> tuple:
        o_time = o.get("time") or o.get("formation_time") or 0
        is_today = 0 if (session_date and datetime.fromtimestamp(o_time, tz=INDIA_TZ).date() == session_date) else 1
        stat_prio = 0 if o.get("status") == "Fresh" else (1 if o.get("status") == "Tested" else 2)
        is_align = 0 if ((o.get("type") in ("BULLISH", "+OB")) == is_bull_trend) else 1
        return (is_today, stat_prio, is_align, -o_time)

    sorted_candidates = sorted(all_candidates, key=_overall_prio)
    filtered_obs: list[dict[str, Any]] = []
    for cand in sorted_candidates:
        cand_top = max(cand["top"], cand["bottom"])
        cand_bot = min(cand["top"], cand["bottom"])
        collides = False
        for kept in filtered_obs:
            kept_top = max(kept["top"], kept["bottom"])
            kept_bot = min(kept["top"], kept["bottom"])
            if max(cand_bot, kept_bot) < min(cand_top, kept_top):
                collides = True
                break
        if not collides:
            filtered_obs.append(cand)

    return {
        "trend": trend,
        "swings": swings[-8:],
        "bos": bos_list[-4:],
        "choch": choch_list[-4:],
        "order_blocks": filtered_obs,
    }


def latest_fresh_order_block(
    candles: list[dict[str, Any]], *, bullish: bool
) -> dict[str, Any] | None:
    """Select the latest untouched block of the displayed session and side."""
    if not candles:
        return None
    latest = candles[-1]
    session = datetime.fromtimestamp(latest["timestamp"], INDIA_TZ).date()
    block_type = "BULLISH" if bullish else "BEARISH"
    all_obs = detect_order_blocks_and_structure(candles)["order_blocks"]

    # Filter for session date, side, and unmitigated Fresh status
    matching = [
        b for b in all_obs
        if b.get("type") == block_type
        and b.get("status") == "Fresh"
        and not b.get("mitigated")
        and datetime.fromtimestamp(b.get("time", 0), INDIA_TZ).date() == session
    ]
    if not matching:
        return None

    # Sort strictly chronologically descending (newest formation first)
    matching.sort(key=lambda b: (b.get("formation_time") or b.get("time", 0)), reverse=True)

    for block in matching:
        # Bullish demand: latest price is holding above the base or actively inside the demand base
        if bullish and latest["close"] >= block["bottom"]:
            return block
        # Bearish supply: latest price is below the roof or actively inside the supply ceiling
        elif not bullish and latest["close"] <= block["top"]:
            return block

    return None


def calculate_order_flow_and_absorption(
    candles: list[dict[str, Any]],
    val_level: float | None = None,
    vah_level: float | None = None,
    candles_1m: list[dict[str, Any]] | None = None,
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

    trs = [max(float(c.get("high", 0)) - float(c.get("low", 0)), 0.01) for c in candles]
    atr = (sum(trs) / len(trs)) if trs else 1.0
    grouped_1m = _group_1m_by_5m(candles_1m)
    allow_synthetic = not any(
        float(c.get("volume", 0) or 0) > 0 or c.get("volume_source") == "futures"
        for c in candles
    )

    total_buy = 0
    total_sell = 0
    clusters = []
    seen_sources: set[str] = set()
    seen_methods: set[str] = set()
    resolved_vols: list[int] = []

    for c in candles:
        sub_1m = grouped_1m.get(int(c["timestamp"])) if c.get("timestamp") is not None else None
        vol, buy_vol, sell_vol, delta, vol_source, delta_method, _ = _compute_candle_footprint_and_totals(
            c, sub_1m, atr, allow_synthetic=allow_synthetic
        )
        seen_sources.add(vol_source)
        seen_methods.add(delta_method)
        resolved_vols.append(vol)
        c_open = float(c.get("open", 0))
        c_close = float(c.get("close", 0))
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))

        total_buy += buy_vol
        total_sell += sell_vol

        clusters.append({
            "time": c["timestamp"],
            "open": c_open,
            "close": c_close,
            "high": c_high,
            "low": c_low,
            "buy_vol": buy_vol,
            "sell_vol": sell_vol,
            "delta": delta,
            "total_vol": vol,
            "volume_source": vol_source,
            "delta_method": delta_method,
        })

    net_delta = total_buy - total_sell
    total_vol = total_buy + total_sell

    # Absorption Detection over recent session baseline
    recent_window = resolved_vols[-40:] if len(resolved_vols) >= 40 else resolved_vols
    avg_vol = (sum(recent_window) / len(recent_window)) if recent_window else 1.0
    recent_pairs = list(zip(candles[-5:], resolved_vols[-5:]))

    absorption_detected = False
    absorption_type = "None"
    absorption_label = "Neutral Order Flow"
    absorption_desc = "Normal order flow distribution."

    for c, vol in reversed(recent_pairs):
        c_open = float(c.get("open", 0))
        c_close = float(c.get("close", 0))
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))
        spread = max(0.01, c_high - c_low)
        lower_wick = min(c_open, c_close) - c_low
        upper_wick = c_high - max(c_open, c_close)

        if vol > avg_vol * 1.3 and (lower_wick / spread) > 0.35 and c_close >= c_open:
            if val_level is None or c_low <= (val_level * 1.005):
                absorption_detected = True
                absorption_type = "Buyer Side Absorption"
                absorption_label = "⚡ Buyer Side Absorption"
                absorption_desc = f"Heavy selling absorbed near ₹{round(c_low, 2)} with strong buyer response (+Delta)."
                break

        if vol > avg_vol * 1.3 and (upper_wick / spread) > 0.35 and c_close <= c_open:
            if vah_level is None or c_high >= (vah_level * 0.995):
                absorption_detected = True
                absorption_type = "Seller Side Absorption"
                absorption_label = "⚠️ Seller Side Absorption"
                absorption_desc = f"Heavy buying absorbed near ₹{round(c_high, 2)} with institutional sell resistance (-Delta)."
                break

    if "futures" in seen_sources:
        volume_source = "futures"
    elif "spot" in seen_sources or "real" in seen_sources:
        volume_source = "spot"
    else:
        volume_source = "estimated"

    return {
        "total_volume": int(total_vol),
        "buy_volume": int(total_buy),
        "sell_volume": int(total_sell),
        "delta": int(net_delta),
        "delta_pct": round((net_delta / total_vol) * 100, 1) if total_vol > 0 else 0,
        "volume_source": volume_source,
        "delta_method": "1m_subbar" if "1m_subbar" in seen_methods else "ohlc_profile",
        "oi_is_real": any(bool(c.get("oi_is_real")) for c in candles),
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
    trs = [
        max(candles[idx]["high"] - candles[idx]["low"],
            abs(candles[idx]["high"] - candles[idx - 1]["close"]),
            abs(candles[idx]["low"] - candles[idx - 1]["close"]))
        for idx in range(1, len(candles))
    ]
    atr = (sum(trs[-14:]) / min(14, len(trs))) if trs else max(ltp * 0.0035, 5.0)
    atr = max(atr, 0.5)

    trend = smc.get("trend", "Neutral")
    poc = vp.get("poc") or ltp
    vah = vp.get("vah") or ltp
    val = vp.get("val") or ltp
    order_blocks = smc.get("order_blocks", [])
    absorption = of.get("absorption", {})
    delta = of.get("delta", 0)

    demand_obs = [ob for ob in order_blocks if ob.get("type") in ("+OB", "BULLISH")]
    supply_obs = [ob for ob in order_blocks if ob.get("type") in ("-OB", "BEARISH")]

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

        valid_demand = [ob for ob in demand_obs if ob["top"] <= ltp + 1.25 * atr]
        if valid_demand:
            target_ob = max(valid_demand, key=lambda x: x["top"])
            entry_low = round(target_ob["bottom"], 2)
            entry_high = round(target_ob["top"], 2)
            entry_mid = round((entry_low + entry_high) / 2, 2)
            inval_buffer = max(0.15 * atr, entry_low * 0.0005)
            stop_loss = round(entry_low - inval_buffer, 2)
            entry_zone_str = f"₹{entry_low:,.2f} – ₹{entry_high:,.2f}"
            ob_status = target_ob.get("status") or ("Fresh" if target_ob.get("fresh") else "Tested")
            ob_source = f"{ob_status} Demand OB"
        else:
            entry_mid = round(min(ltp, poc), 2)
            inval_buffer = max(0.15 * atr, entry_mid * 0.0005)
            entry_low = round(entry_mid - 0.5 * inval_buffer, 2)
            entry_high = round(entry_mid + 0.5 * inval_buffer, 2)
            stop_loss = round(min(val, entry_low - inval_buffer), 2)
            entry_zone_str = f"₹{entry_low:,.2f} – ₹{entry_high:,.2f}"
            ob_source = "POC Confluence Zone"

        risk = max(inval_buffer, entry_mid - stop_loss)
        target_1 = round(max(vah, entry_mid + risk * 1.8), 2)
        target_2 = round(max(swing_high + 0.5 * atr, entry_mid + risk * 3.0), 2)
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

        valid_supply = [ob for ob in supply_obs if ob["bottom"] >= ltp - 1.25 * atr]
        if valid_supply:
            target_ob = min(valid_supply, key=lambda x: x["bottom"])
            entry_low = round(target_ob["bottom"], 2)
            entry_high = round(target_ob["top"], 2)
            entry_mid = round((entry_low + entry_high) / 2, 2)
            inval_buffer = max(0.15 * atr, entry_high * 0.0005)
            stop_loss = round(entry_high + inval_buffer, 2)
            entry_zone_str = f"₹{entry_low:,.2f} – ₹{entry_high:,.2f}"
            ob_status = target_ob.get("status") or ("Fresh" if target_ob.get("fresh") else "Tested")
            ob_source = f"{ob_status} Supply OB"
        else:
            entry_mid = round(max(ltp, poc), 2)
            inval_buffer = max(0.15 * atr, entry_mid * 0.0005)
            entry_low = round(entry_mid - 0.5 * inval_buffer, 2)
            entry_high = round(entry_mid + 0.5 * inval_buffer, 2)
            stop_loss = round(max(vah, entry_high + inval_buffer), 2)
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
    *,
    completed_candles: list[dict[str, Any]] | None = None,
    candles_1m: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Unified master analysis: Volume Profile + Order Blocks + Order Flow Footprint + Trade Plan
    + FVG + Liquidity Pools + CVD + Per-Candle Footprint."""
    vp = calculate_volume_profile(candles)
    smc = detect_order_blocks_and_structure(candles if completed_candles is None else completed_candles)
    # Scope Order Flow, CVD, and Footprint onto the exact same active window (last 90 bars)
    # so Order Flow Total Volume == sum(Footprint bars) and Order Flow Delta == CVD == Footprint Cum Delta.
    active_of_candles = candles[-90:] if len(candles) > 90 else candles
    of = calculate_order_flow_and_absorption(
        active_of_candles, val_level=vp.get("val"), vah_level=vp.get("vah"), candles_1m=candles_1m
    )
    plan = generate_institutional_trade_plan(candles, vp, smc, of)
    fvgs = detect_fair_value_gaps(candles)
    liquidity = detect_liquidity_pools(candles)
    cvd = calculate_cvd_series(active_of_candles, candles_1m=candles_1m)
    footprints = build_candle_footprints(active_of_candles, candles_1m=candles_1m)
    mtf = calculate_multi_timeframe_matrix(candles)
    trend_band = calculate_monotonic_trend_band(candles)
    extension_gate = calculate_extension_gate(candles)
    frozen_breakouts = detect_frozen_breakout_levels(candles)

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
        "footprint_summary": footprints.get("_summary", []),
        "mtf_matrix": mtf,
        "trend_band": trend_band,
        "extension_gate": extension_gate,
        "frozen_breakouts": frozen_breakouts,
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
    candles_1m: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Computes Cumulative Volume Delta (CVD) matching Order Flow & Footprint bar-by-bar."""
    if not candles:
        return []

    series = []
    cumulative = 0
    trs = [max(float(c.get("high", 0)) - float(c.get("low", 0)), 0.01) for c in candles]
    atr = (sum(trs) / len(trs)) if trs else 1.0
    grouped_1m = _group_1m_by_5m(candles_1m)
    allow_synthetic = not any(
        float(c.get("volume", 0) or 0) > 0 or c.get("volume_source") == "futures"
        for c in candles
    )

    for c in candles:
        sub_1m = grouped_1m.get(int(c["timestamp"])) if c.get("timestamp") is not None else None
        _, _, _, delta, _, _, _ = _compute_candle_footprint_and_totals(
            c, sub_1m, atr, allow_synthetic=allow_synthetic
        )
        cumulative += delta

        series.append({
            "time": c["timestamp"],
            "value": int(cumulative),
        })

    return series


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 4: PER-CANDLE FOOTPRINT (Bid-Ask Breakdown by Price Level)
# ═══════════════════════════════════════════════════════════════════════

def build_candle_footprints(
    candles: list[dict[str, Any]],
    levels_per_candle: int = 8,
    candles_1m: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Builds a per-candle footprint ladder (bid vs ask volume at each price level)
    and synchronized 5-row order flow summary (Volume, Delta, Cum Delta, OI, OI Change).
    """
    if not candles:
        return {}

    trs = [max(c["high"] - c["low"], 0.01) for c in candles]
    atr = (sum(trs) / len(trs)) if trs else 1.0
    grouped_1m = _group_1m_by_5m(candles_1m)
    allow_synthetic = not any(
        float(c.get("volume", 0) or 0) > 0 or c.get("volume_source") == "futures"
        for c in candles
    )

    result: dict[str, Any] = {}
    summary_bars: list[dict[str, Any]] = []

    running_cum_delta = 0
    prev_real_oi: float | None = None

    for c in candles:
        c_open = float(c.get("open", 0))
        c_close = float(c.get("close", 0))
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))
        ts = str(c["timestamp"])
        is_bullish = c_close >= c_open

        sub_1m = grouped_1m.get(int(c["timestamp"])) if c.get("timestamp") is not None else None
        vol, _, _, candle_total_delta, vol_source, delta_method, levels = _compute_candle_footprint_and_totals(
            c, sub_1m, atr, levels_per_candle=levels_per_candle, allow_synthetic=allow_synthetic
        )

        running_cum_delta += candle_total_delta
        raw_oi = c.get("oi")
        has_real_oi = raw_oi is not None and (bool(c.get("oi_is_real")) or float(raw_oi or 0) > 0)
        if has_real_oi:
            curr_oi = float(raw_oi)
            oi_delta = int(round(curr_oi - prev_real_oi)) if prev_real_oi is not None else 0
            prev_real_oi = curr_oi
            oi_val: int | None = int(round(curr_oi))
        else:
            oi_val = None
            oi_delta = None

        bar_info = {
            "time": c["timestamp"],
            "open": round(c_open, 2),
            "close": round(c_close, 2),
            "high": round(c_high, 2),
            "low": round(c_low, 2),
            "total_vol": int(vol),
            "volume_source": vol_source,
            "delta_method": delta_method,
            "net_delta": int(candle_total_delta),
            "cum_delta": int(running_cum_delta),
            "oi": oi_val,
            "oi_change": oi_delta,
            "oi_is_real": has_real_oi,
            "is_up": is_bullish,
        }

        result[ts] = {
            **bar_info,
            "levels": levels,
        }
        summary_bars.append(bar_info)

    result["_summary"] = summary_bars
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


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 8: MONOTONIC TRAILING TREND BAND (EMA9(hlc3) ± 0.60*ATR)
# ═══════════════════════════════════════════════════════════════════════

def calculate_monotonic_trend_band(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Computes monotonic trailing trend band:
    Basis: EMA9(hlc3)
    Offsets: rawSupport = ema9 - 0.60*atr, rawResistance = ema9 + 0.60*atr
    Bull Trail: max(prev_trail, rawSupport)  (non-decreasing)
    Bear Trail: min(prev_trail, rawResistance) (non-increasing)
    """
    n = len(candles)
    if n < 5:
        return {"mode": "NEUTRAL", "points": [], "latest": None}

    # Calculate ATR14
    trs = [max(candles[i]["high"] - candles[i]["low"],
               abs(candles[i]["high"] - candles[i-1]["close"]),
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, n)]
    atr = (sum(trs[-14:]) / min(14, len(trs))) if trs else 5.0
    atr = max(atr, 0.5)

    # EMA9 of hlc3
    hlc3_list = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in candles]
    alpha9 = 2.0 / (9 + 1)
    ema9 = hlc3_list[0]

    # Simple EMA20 for regime
    alpha20 = 2.0 / (20 + 1)
    ema20 = candles[0]["close"]

    trail = 0.0
    mode = "BULL" if candles[-1]["close"] >= candles[0]["open"] else "BEAR"
    points: list[dict[str, Any]] = []

    for i in range(n):
        c = candles[i]
        h3 = hlc3_list[i]
        ema9 = alpha9 * h3 + (1.0 - alpha9) * ema9
        ema20 = alpha20 * c["close"] + (1.0 - alpha20) * ema20

        # Regime evaluation
        curr_mode = "BULL" if c["close"] >= ema20 else "BEAR"

        if curr_mode == "BULL":
            raw_supp = ema9 - 0.60 * atr
            if trail == 0.0 or mode != "BULL":
                trail = raw_supp
            else:
                trail = max(trail, raw_supp)
            center = round(trail + 0.15 * atr, 2)
            lower = round(trail - 0.10 * atr, 2)
            upper = round(trail + 0.45 * atr, 2)
        else:
            raw_res = ema9 + 0.60 * atr
            if trail == 0.0 or mode != "BEAR":
                trail = raw_res
            else:
                trail = min(trail, raw_res)
            center = round(trail - 0.15 * atr, 2)
            lower = round(trail - 0.45 * atr, 2)
            upper = round(trail + 0.10 * atr, 2)

        mode = curr_mode
        points.append({
            "time": c["timestamp"],
            "center": center,
            "lower": lower,
            "upper": upper,
            "trail": round(trail, 2),
            "mode": mode,
            "is_above": bool(c["close"] >= lower if mode == "BULL" else c["close"] <= upper),
        })

    latest = points[-1] if points else None
    return {
        "mode": mode,
        "points": points,
        "latest": latest,
        "atr": round(atr, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 9: ATR EXTENSION GATE ("DO NOT CHASE" RULE)
# ═══════════════════════════════════════════════════════════════════════

def calculate_rolling_atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    """Computes dynamic rolling True Range ATR over the latest `period` candles."""
    n = len(candles)
    if n < 2:
        last_close = float(candles[-1]["close"]) if candles else 100.0
        return max(last_close * 0.0035, 5.0)
    trs = [
        max(
            float(candles[i]["high"]) - float(candles[i]["low"]),
            abs(float(candles[i]["high"]) - float(candles[i - 1]["close"])),
            abs(float(candles[i]["low"]) - float(candles[i - 1]["close"])),
        )
        for i in range(1, n)
    ]
    atr = sum(trs[-period:]) / min(period, len(trs))
    return max(atr, 0.5)


def calculate_extension_gate(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluates whether current price is overextended from EMA20 by > 1.8 ATR.
    Long extension: (Close - EMA20) / ATR > 1.8 => EXTENDED_DO_NOT_CHASE
    Short extension: (EMA20 - Close) / ATR > 1.8 => EXTENDED_DO_NOT_CHASE
    """
    n = len(candles)
    if n < 20:
        return {
            "is_extended": False,
            "extension_atr": 0.0,
            "signed_extension_atr": 0.0,
            "ema20": float(candles[-1]["close"]) if candles else 0.0,
            "atr": calculate_rolling_atr(candles, 14),
            "status": "NORMAL",
            "warning": "",
        }

    atr = calculate_rolling_atr(candles, 14)

    # Calculate EMA20
    alpha20 = 2.0 / (20 + 1)
    ema20 = candles[0]["close"]
    for c in candles:
        ema20 = alpha20 * c["close"] + (1.0 - alpha20) * ema20

    curr_close = candles[-1]["close"]
    extension_atr = round((curr_close - ema20) / atr, 2)

    is_extended_long = extension_atr > 1.8
    is_extended_short = extension_atr < -1.8

    if is_extended_long:
        return {
            "is_extended": True,
            "extension_atr": extension_atr,
            "signed_extension_atr": extension_atr,
            "ema20": round(ema20, 2),
            "atr": round(atr, 2),
            "status": "EXTENDED_DO_NOT_CHASE",
            "side": "LONG",
            "warning": f"Overstretched (+{extension_atr:.1f}x ATR above EMA20). Avoid chasing highs; wait for Demand Zone pullback.",
        }
    elif is_extended_short:
        return {
            "is_extended": True,
            "extension_atr": abs(extension_atr),
            "signed_extension_atr": extension_atr,
            "ema20": round(ema20, 2),
            "atr": round(atr, 2),
            "status": "EXTENDED_DO_NOT_CHASE",
            "side": "SHORT",
            "warning": f"Overstretched ({extension_atr:.1f}x ATR below EMA20). Avoid chasing lows; wait for Supply Zone pullback.",
        }

    return {
        "is_extended": False,
        "extension_atr": abs(extension_atr),
        "signed_extension_atr": extension_atr,
        "ema20": round(ema20, 2),
        "atr": round(atr, 2),
        "status": "NORMAL",
        "side": "NEUTRAL",
        "warning": "",
    }


# ═══════════════════════════════════════════════════════════════════════
# FEATURE 10: FROZEN BREAKOUT LEVELS & BR CONFIRMATION MARKERS
# ═══════════════════════════════════════════════════════════════════════

def detect_frozen_breakout_levels(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Detects institutional breakout / breakdown confirmation with strict trend alignment:
    1. Trend Alignment: Bullish breakouts (BR ▲) only in BULL mode; Bearish breakdowns (BD ▼) in BEAR mode.
    2. Extension Gate: Suppressed if candle is already overextended (> 1.8 ATR).
    3. Failure Invalidation: If price closes back across level within 3 bars or later collapses, it is eliminated.
    Guarantees ONE UNIFIED SYSTEM OPINION: Zero contradictory signals on the chart.
    """
    n = len(candles)
    if n < 15:
        return {"levels": [], "markers": [], "primary_level": None, "latest_breakout": None, "regime": "NEUTRAL"}

    trs = [max(candles[i]["high"] - candles[i]["low"],
               abs(candles[i]["high"] - candles[i-1]["close"]),
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, n)]
    atr = (sum(trs[-14:]) / min(14, len(trs))) if trs else 5.0
    atr = max(atr, 0.5)

    alpha20 = 2.0 / (20 + 1)
    ema20 = candles[0]["close"]
    ema20_vals = []
    for c in candles:
        ema20 = alpha20 * c["close"] + (1.0 - alpha20) * ema20
        ema20_vals.append(ema20)

    curr_close = candles[-1]["close"]
    is_bull_regime = curr_close >= ema20_vals[-1]

    breakout_levels: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []

    if is_bull_regime:
        # ── BULLISH REGIME: Scan for Valid Unviolated Resistance Breakouts ──
        for i in range(12, n):
            frozen_r = max(c["high"] for c in candles[i-12:i])
            c_curr = candles[i]
            rng = max(c_curr["high"] - c_curr["low"], 0.01)
            body = abs(c_curr["close"] - c_curr["open"])
            close_loc = (c_curr["close"] - c_curr["low"]) / rng
            ext = (c_curr["close"] - ema20_vals[i]) / atr

            if (c_curr["close"] > frozen_r + 0.10 * atr and
                c_curr["close"] > c_curr["open"] and
                body / rng >= 0.50 and
                close_loc >= 0.70 and
                ext <= 2.2):  # Do not emit BR on runaway climax candles

                subsequent = candles[i+1 : min(i+4, n)]
                failed = any(sub["close"] < frozen_r - 0.10 * atr for sub in subsequent)
                is_currently_holding = (curr_close >= frozen_r - 0.15 * atr)

                if is_currently_holding and not failed:
                    b_event = {
                        "id": f"br_lvl_{i}",
                        "type": "RESISTANCE",
                        "side": "BULLISH",
                        "price": round(frozen_r, 2),
                        "breakout_price": round(c_curr["close"], 2),
                        "time": c_curr["timestamp"],
                        "status": "CONFIRMED",
                    }
                    m_event = {
                        "time": c_curr["timestamp"],
                        "position": "aboveBar",
                        "side": "BULLISH",
                        "color": "#10b981",
                        "text": "BR ▲",
                        "price": round(c_curr["high"], 2),
                    }
                    breakout_levels.append(b_event)
                    markers.append(m_event)

    else:
        # ── BEARISH REGIME: Scan for Valid Support Breakdowns (BD ▼) ──
        # When market is Bearish, NEVER show contradictory bullish BR markers!
        for i in range(12, n):
            frozen_s = min(c["low"] for c in candles[i-12:i])
            c_curr = candles[i]
            rng = max(c_curr["high"] - c_curr["low"], 0.01)
            body = abs(c_curr["close"] - c_curr["open"])
            close_loc = (c_curr["high"] - c_curr["close"]) / rng

            if (c_curr["close"] < frozen_s - 0.10 * atr and
                c_curr["close"] < c_curr["open"] and
                body / rng >= 0.50 and
                close_loc >= 0.70):

                subsequent = candles[i+1 : min(i+4, n)]
                failed = any(sub["close"] > frozen_s + 0.10 * atr for sub in subsequent)
                is_currently_holding = (curr_close <= frozen_s + 0.15 * atr)

                if is_currently_holding and not failed:
                    b_event = {
                        "id": f"bd_lvl_{i}",
                        "type": "SUPPORT",
                        "side": "BEARISH",
                        "price": round(frozen_s, 2),
                        "breakout_price": round(c_curr["close"], 2),
                        "time": c_curr["timestamp"],
                        "status": "CONFIRMED",
                    }
                    m_event = {
                        "time": c_curr["timestamp"],
                        "position": "belowBar",
                        "side": "BEARISH",
                        "color": "#ef4444",
                        "text": "BD ▼",
                        "price": round(c_curr["low"], 2),
                    }
                    breakout_levels.append(b_event)
                    markers.append(m_event)

    # Per Spec Section 10.1 & Config 23: drawOnlyPrimaryBreakoutLine: true
    primary_level = breakout_levels[-1] if breakout_levels else None
    primary_marker = markers[-1] if markers else None

    return {
        "levels": [primary_level] if primary_level else [],
        "markers": [primary_marker] if primary_marker else [],
        "all_levels": breakout_levels,
        "primary_level": primary_level,
        "latest_breakout": primary_level,
        "regime": "BULLISH" if is_bull_regime else "BEARISH",
    }

