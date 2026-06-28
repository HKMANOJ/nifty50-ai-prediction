from typing import Dict, Any, List, Tuple
import json

class OptionsEngine:
    """
    Analyzes option chain data to detect parent strikes, OI migration, 
    unwinding/covering activities, and generate OI/PCR signals.
    """
    
    def __init__(self):
        self.option_chain_data = {}
        self.historical_oi = {}
        
    def update_option_chain(self, option_chain: Dict[str, Any]):
        """Update the internal option chain data."""
        self.option_chain_data = option_chain
        
    def update_historical_oi(self, historical_oi: Dict[str, Any]):
        """Update historical OI data for migration analysis."""
        self.historical_oi = historical_oi
        
    def detect_parent_strikes(self) -> Dict[str, Any]:
        """
        Detect major Call and Put OI strikes (parent strikes).
        
        Returns:
            Dict containing major call strikes, major put strikes,
            and their respective OI values.
        """
        if not self.option_chain_data:
            return {"major_call_strikes": [], "major_put_strikes": []}
            
        calls = self.option_chain_data.get("calls", [])
        puts = self.option_chain_data.get("puts", [])
        
        # Find strikes with OI above 80th percentile
        call_oi_values = [item.get("oi", 0) for item in calls if item.get("oi", 0) > 0]
        put_oi_values = [item.get("oi", 0) for item in puts if item.get("oi", 0) > 0]
        
        if not call_oi_values or not put_oi_values:
            return {"major_call_strikes": [], "major_put_strikes": []}
            
        call_oi_threshold = sorted(call_oi_values)[int(len(call_oi_values) * 0.8)] if call_oi_values else 0
        put_oi_threshold = sorted(put_oi_values)[int(len(put_oi_values) * 0.8)] if put_oi_values else 0
        
        major_call_strikes = [
            {"strike": item["strike"], "oi": item["oi"], "change_oi": item.get("change_oi", 0)}
            for item in calls 
            if item.get("oi", 0) >= call_oi_threshold
        ]
        
        major_put_strikes = [
            {"strike": item["strike"], "oi": item["oi"], "change_oi": item.get("change_oi", 0)}
            for item in puts 
            if item.get("oi", 0) >= put_oi_threshold
        ]
        
        # Sort by OI descending
        major_call_strikes.sort(key=lambda x: x["oi"], reverse=True)
        major_put_strikes.sort(key=lambda x: x["oi"], reverse=True)
        
        return {
            "major_call_strikes": major_call_strikes[:5],  # Top 5
            "major_put_strikes": major_put_strikes[:5],    # Top 5
            "call_oi_threshold": call_oi_threshold,
            "put_oi_threshold": put_oi_threshold
        }
        
    def detect_oi_migration(self) -> Dict[str, Any]:
        """
        Detect OI migration between strikes (shifts in support/resistance).
        
        Returns:
            Dict indicating resistance shift, support shift, and migration strength.
        """
        if not self.option_chain_data or not self.historical_oi:
            return {"resistance_shift": "NEUTRAL", "support_shift": "NEUTRAL", "migration_strength": 0}
            
        current_calls = {item["strike"]: item.get("oi", 0) for item in self.option_chain_data.get("calls", [])}
        current_puts = {item["strike"]: item.get("oi", 0) for item in self.option_chain_data.get("puts", [])}
        
        historical_calls = self.historical_oi.get("calls", {})
        historical_puts = self.historical_oi.get("puts", {})
        
        # Calculate OI changes for calls (resistance levels)
        call_oi_changes = {}
        for strike in set(list(current_calls.keys()) + list(historical_calls.keys())):
            current = current_calls.get(strike, 0)
            historical = historical_calls.get(strike, 0)
            call_oi_changes[strike] = current - historical
            
        # Calculate OI changes for puts (support levels)
        put_oi_changes = {}
        for strike in set(list(current_puts.keys()) + list(historical_puts.keys())):
            current = current_puts.get(strike, 0)
            historical = historical_puts.get(strike, 0)
            put_oi_changes[strike] = current - historical
            
        # Find maximum OI increase in calls (new resistance)
        max_call_increase = max(call_oi_changes.values()) if call_oi_changes else 0
        max_call_strike = max(call_oi_changes, key=call_oi_changes.get) if call_oi_changes else None
        
        # Find maximum OI increase in puts (new support)
        max_put_increase = max(put_oi_changes.values()) if put_oi_changes else 0
        max_put_strike = max(put_oi_changes, key=put_oi_changes.get) if put_oi_changes else None
        
        # Determine shifts
        resistance_shift = "UP" if max_call_increase > 0 else "DOWN" if max_call_increase < 0 else "NEUTRAL"
        support_shift = "UP" if max_put_increase > 0 else "DOWN" if max_put_increase < 0 else "NEUTRAL"
        
        migration_strength = min(100, (abs(max_call_increase) + abs(max_put_increase)) / 1000)  # Normalize
        
        return {
            "resistance_shift": resistance_shift,
            "support_shift": support_shift,
            "resistance_strike": max_call_strike,
            "support_strike": max_put_strike,
            "migration_strength": migration_strength,
            "max_call_oi_increase": max_call_increase,
            "max_put_oi_increase": max_put_increase
        }
        
    def detect_unwinding_covering(self) -> Dict[str, Any]:
        """
        Detect call/put unwinding and short/long covering activities.
        
        Returns:
            Dict indicating call unwinding, put unwinding, short covering, long covering.
        """
        if not self.option_chain_data:
            return {
                "call_unwinding": False, 
                "put_unwinding": False, 
                "short_covering": False, 
                "long_covering": False
            }
            
        calls = self.option_chain_data.get("calls", [])
        puts = self.option_chain_data.get("puts", [])
        
        # Detect unwinding: OI decreasing while price moves against the option
        call_unwinding = False
        put_unwinding = False
        short_covering = False  # Put buying/OI decrease in puts during price rise
        long_covering = False   # Call buying/OI decrease in calls during price fall
        
        # Simplified logic - in practice would need price correlation
        total_call_oi_change = sum(item.get("change_oi", 0) for item in calls)
        total_put_oi_change = sum(item.get("change_oi", 0) for item in puts)
        
        # If call OI decreasing significantly, suggests unwinding or long covering
        if total_call_oi_change < -1000:  # Arbitrary threshold
            call_unwinding = True
            
        # If put OI decreasing significantly, suggests unwinding or short covering  
        if total_put_oi_change < -1000:
            put_unwinding = True
            
        # For short/long covering detection, we'd need price direction
        # This is simplified - would integrate with market data in practice
        short_covering = total_put_oi_change < -500  # Put OI decrease
        long_covering = total_call_oi_change < -500   # Call OI decrease
        
        return {
            "call_unwinding": call_unwinding,
            "put_unwinding": put_unwinding,
            "short_covering": short_covering,
            "long_covering": long_covering,
            "total_call_oi_change": total_call_oi_change,
            "total_put_oi_change": total_put_oi_change
        }
        
    def generate_oi_pcr_signals(self) -> Dict[str, Any]:
        """
        Generate OI and PCR-based trading signals.
        
        Returns:
            Dict containing PCR signal, OI signal, and strength metrics.
        """
        if not self.option_chain_data:
            return {"pcr_signal": "NEUTRAL", "oi_signal": "NEUTRAL", "pcr": 0, "oi_strength": 0}
            
        calls = self.option_chain_data.get("calls", [])
        puts = self.option_chain_data.get("puts", [])
        
        total_call_oi = sum(item.get("oi", 0) for item in calls)
        total_put_oi = sum(item.get("oi", 0) for item in puts)
        
        if total_call_oi == 0:
            pcr = 0
        else:
            pcr = total_put_oi / total_call_oi
            
        # PCR signals
        if pcr > 1.5:
            pcr_signal = "BULLISH"  # High put call ratio suggests bullish reversal
        elif pcr > 1.2:
            pcr_signal = "SLIGHTLY_BULLISH"
        elif pcr < 0.5:
            pcr_signal = "BEARISH"  # Low put call ratio suggests bearish
        elif pcr < 0.8:
            pcr_signal = "SLIGHTLY_BEARISH"
        else:
            pcr_signal = "NEUTRAL"
            
        # OI signal based on OI changes
        call_oi_change = sum(item.get("change_oi", 0) for item in calls)
        put_oi_change = sum(item.get("change_oi", 0) for item in puts)
        
        oi_signal = "NEUTRAL"
        if put_oi_change > call_oi_change * 1.5:  # Put OI increasing faster
            oi_signal = "Put Writing + Call Unwinding"  # Bullish
        elif call_oi_change > put_oi_change * 1.5:  # Call OI increasing faster
            oi_signal = "Call Writing + Put Unwinding"  # Bearish
        elif put_oi_change > 0 and call_oi_change < 0:
            oi_signal = "Put Writing + Call Unwinding"  # Bullish
        elif call_oi_change > 0 and put_oi_change < 0:
            oi_signal = "Call Writing + Put Unwinding"  # Bearish
            
        # OI strength based on total OI concentration
        max_single_strike_oi = 0
        all_oi = []
        for item in calls + puts:
            oi_val = item.get("oi", 0)
            all_oi.append(oi_val)
            if oi_val > max_single_strike_oi:
                max_single_strike_oi = oi_val
                
        total_oi = sum(all_oi)
        oi_strength = 0
        if total_oi > 0:
            oi_strength = min(100, (max_single_strike_oi / total_oi) * 100)
            
        return {
            "pcr_signal": pcr_signal,
            "oi_signal": oi_signal,
            "pcr": round(pcr, 3),
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "call_oi_change": call_oi_change,
            "put_oi_change": put_oi_change,
            "oi_strength": round(oi_strength, 1)
        }
