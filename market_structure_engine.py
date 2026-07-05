from typing import List, Dict, Any
# Removed numpy dependency for simplicity

class MarketStructureEngine:
    """
    Analyzes market structure based on price action (Higher Highs, Lower Lows, etc.).
    """

    def __init__(self):
        pass

    def _find_swing_points(self, candles: List[Dict[str, Any]], lookback: int = 5) -> List[Dict[str, Any]]:
        """
        Identifies swing highs and swing lows within a given lookback period.
        A swing high is a high with 'lookback' number of lower highs on both sides.
        A swing low is a low with 'lookback' number of higher lows on both sides.
        """
        swing_points = []
        if len(candles) < lookback * 2 + 1:
            return swing_points

        for i in range(lookback, len(candles) - lookback):
            # Check for Swing High
            is_swing_high = True
            for j in range(1, lookback + 1):
                if candles[i]["high"] <= candles[i - j]["high"] or \
                   candles[i]["high"] <= candles[i + j]["high"]:
                    is_swing_high = False
                    break
            if is_swing_high:
                swing_points.append({"type": "high", "index": i, "price": candles[i]["high"], "date": candles[i]["market_date"]})

            # Check for Swing Low
            is_swing_low = True
            for j in range(1, lookback + 1):
                if candles[i]["low"] >= candles[i - j]["low"] or \
                   candles[i]["low"] >= candles[i + j]["low"]:
                    is_swing_low = False
                    break
            if is_swing_low:
                swing_points.append({"type": "low", "index": i, "price": candles[i]["low"], "date": candles[i]["market_date"]})

        # Sort by index to maintain chronological order
        swing_points.sort(key=lambda x: x["index"])
        return swing_points

    def _find_supply_demand_zones(self, candles: List[Dict[str, Any]], lookback: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        """
        Identifies supply and demand zones based on price action and volume.
        Supply zone: area where price had difficulty moving up (selling pressure)
        Demand zone: area where price had difficulty moving down (buying pressure)
        """
        supply_zones = []
        demand_zones = []
        
        if len(candles) < lookback:
            return {"supply_zones": supply_zones, "demand_zones": demand_zones}
            
        # Look for strong rejections (long wicks) indicating supply/demand
        for i in range(len(candles)):
            candle = candles[i]
            high = candle["high"]
            low = candle["low"]
            open_price = candle["open"]
            close_price = candle["close"]
            volume = candle.get("volume", 0)
            
            # Calculate body and wicks
            body_size = abs(close_price - open_price)
            upper_wick = high - max(open_price, close_price)
            lower_wick = min(open_price, close_price) - low
            total_range = high - low
            
            if total_range == 0:
                continue
                
            # Strong upper rejection (potential supply zone)
            if upper_wick > total_range * 0.6 and body_size < total_range * 0.3:
                # Found a potential supply zone
                supply_zones.append({
                    "zone_type": "supply",
                    "price_low": low,
                    "price_high": high,
                    "strength": upper_wick / total_range,
                    "volume": volume,
                    "index": i,
                    "date": candle["market_date"]
                })
                
            # Strong lower rejection (potential demand zone)
            if lower_wick > total_range * 0.6 and body_size < total_range * 0.3:
                # Found a potential demand zone
                demand_zones.append({
                    "zone_type": "demand",
                    "price_low": low,
                    "price_high": high,
                    "strength": lower_wick / total_range,
                    "volume": volume,
                    "index": i,
                    "date": candle["market_date"]
                })
        
        # Merge nearby zones
        supply_zones = self._merge_zones(supply_zones)
        demand_zones = self._merge_zones(demand_zones)
        
        return {"supply_zones": supply_zones, "demand_zones": demand_zones}
    
    def _merge_zones(self, zones: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merge overlapping or nearby supply/demand zones."""
        if not zones:
            return zones
            
        # Sort by index
        zones.sort(key=lambda x: x["index"])
        merged = [zones[0]]
        
        for current in zones[1:]:
            last = merged[-1]
            # If zones are close (within 5 candles) and overlap in price
            if current["index"] - last["index"] <= 5:
                # Merge zones
                merged_zone = {
                    "zone_type": last["zone_type"],
                    "price_low": min(last["price_low"], current["price_low"]),
                    "price_high": max(last["price_high"], current["price_high"]),
                    "strength": max(last["strength"], current["strength"]),
                    "volume": last["volume"] + current["volume"],
                    "index": last["index"],  # Keep earlier index
                    "date": last["date"]
                }
                merged[-1] = merged_zone
            else:
                merged.append(current)
                
        return merged

    def analyze_market_structure(self, candles: List[Dict[str, Any]], lookback: int = 5) -> Dict[str, Any]:
        """
        Analyzes the market structure based on swing points.
        """
        structure = {
            "current_structure": "UNKNOWN",
            "higher_highs": [],
            "higher_lows": [],
            "lower_highs": [],
            "lower_lows": [],
            "break_of_structure": [],
            "change_of_character": [],
            "supply_zones": [],
            "demand_zones": [],
            "trend_strength": 0
        }

        swing_points = self._find_swing_points(candles, lookback)
        zones = self._find_supply_demand_zones(candles, lookback=10)
        structure["supply_zones"] = zones["supply_zones"]
        structure["demand_zones"] = zones["demand_zones"]

        if len(swing_points) < 2:
            return structure

        # Identify HH, HL, LH, LL
        for i in range(1, len(swing_points)):
            prev_point = swing_points[i-1]
            curr_point = swing_points[i]

            if prev_point["type"] == "high" and curr_point["type"] == "high":
                if curr_point["price"] > prev_point["price"]:
                    structure["higher_highs"].append({"date": curr_point["date"], "price": curr_point["price"]})
                elif curr_point["price"] < prev_point["price"]:
                    structure["lower_highs"].append({"date": curr_point["date"], "price": curr_point["price"]})
            elif prev_point["type"] == "low" and curr_point["type"] == "low":
                if curr_point["price"] > prev_point["price"]:
                    structure["higher_lows"].append({"date": curr_point["date"], "price": curr_point["price"]})
                elif curr_point["price"] < prev_point["price"]:
                    structure["lower_lows"].append({"date": curr_point["date"], "price": curr_point["price"]})

        # Determine current structure and trend strength
        hh_count = len(structure["higher_highs"])
        ll_count = len(structure["lower_lows"])
        hl_count = len(structure["higher_lows"])
        lh_count = len(structure["lower_highs"])
        
        if hh_count > ll_count and hl_count > lh_count:
            structure["current_structure"] = "UPTREND"
            # Trend strength based on consistency of HH/HL
            structure["trend_strength"] = min(100, (hh_count + hl_count) * 10)
        elif ll_count > hh_count and lh_count > hl_count:
            structure["current_structure"] = "DOWNTREND"
            # Trend strength based on consistency of LH/LL
            structure["trend_strength"] = min(100, (lh_count + ll_count) * 10)
        else:
            structure["current_structure"] = "RANGING"
            structure["trend_strength"] = max(0, 50 - abs(hh_count - ll_count) * 5)

        # Detect Break of Structure (BOS) and Change of Character (CHOCH)
        if len(swing_points) >= 4:
            # Get last 4 swing points
            last_four = swing_points[-4:]
            
            # BOS: Continuation of trend structure
            # In uptrend: HH followed by HL then new HH breaking previous HH
            # In downtrend: LH followed by LL then new LL breaking previous LL
            
            # Check for bullish BOS (uptrend continuation)
            if (len(last_four) >= 4 and 
                last_four[-4]["type"] == "low" and last_four[-3]["type"] == "high" and
                last_four[-2]["type"] == "low" and last_four[-1]["type"] == "high"):
                # HL, LH, HH pattern
                if (last_four[-1]["price"] > last_four[-3]["price"] and  # New HH > previous HH
                    last_four[-2]["price"] > last_four[-4]["price"]):   # New HL > previous HL
                    structure["break_of_structure"].append({
                        "type": "bullish_bos",
                        "point": last_four[-1],
                        "broken_point": last_four[-3],
                        "date": last_four[-1]["date"]
                    })
            
            # Check for bearish BOS (downtrend continuation)
            if (len(last_four) >= 4 and 
                last_four[-4]["type"] == "high" and last_four[-3]["type"] == "low" and
                last_four[-2]["type"] == "high" and last_four[-1]["type"] == "low"):
                # LH, HL, LL pattern
                if (last_four[-1]["price"] < last_four[-3]["price"] and  # New LL < previous LL
                    last_four[-2]["price"] < last_four[-4]["price"]):   # New LH < previous LH
                    structure["break_of_structure"].append({
                        "type": "bearish_bos",
                        "point": last_four[-1],
                        "broken_point": last_four[-3],
                        "date": last_four[-1]["date"]
                    })
            
            # CHOCH: Change in trend character
            # In uptrend: price breaks below recent HL (potential trend change to down)
            # In downtrend: price breaks above recent LH (potential trend change to up)
            
            # Check for bullish CHOCH (potential change to uptrend)
            if len(swing_points) >= 3:
                last_three = swing_points[-3:]
                # Look for LH followed by HL breaking previous LH (in downtrend context)
                if (len(last_three) >= 3 and 
                    last_three[-3]["type"] == "high" and last_three[-2]["type"] == "low" and
                    last_three[-1]["type"] == "high"):
                    # LH, HL, HH pattern - potential CHOCH to uptrend
                    if (last_three[-1]["price"] > last_three[-3]["price"] and  # New HH > previous LH
                        last_three[-1]["price"] > last_three[-2]["price"]):   # New HH > recent HL
                        structure["change_of_character"].append({
                            "type": "bullish_choch",
                            "point": last_three[-1],
                            "broken_point": last_three[-3],
                            "date": last_three[-1]["date"]
                        })
            
            # Check for bearish CHOCH (potential change to downtrend)
            if len(swing_points) >= 3:
                last_three = swing_points[-3:]
                # Look for HL followed by LH breaking previous HL (in uptrend context)
                if (len(last_three) >= 3 and 
                    last_three[-3]["type"] == "low" and last_three[-2]["type"] == "high" and
                    last_three[-1]["type"] == "low"):
                    # HL, LH, LL pattern - potential CHOCH to downtrend
                    if (last_three[-1]["price"] < last_three[-3]["price"] and  # New LL < previous HL
                        last_three[-1]["price"] < last_three[-2]["price"]):   # New LL < recent LH
                        structure["change_of_character"].append({
                            "type": "bearish_choch",
                            "point": last_three[-1],
                            "broken_point": last_three[-3],
                            "date": last_three[-1]["date"]
                        })

        return structure

