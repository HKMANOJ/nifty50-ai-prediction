from typing import Dict, Any, List
import math
import statistics
from datetime import datetime, timedelta

class FVMEngine:
    """
    Calculates Future Volatility Measure (FVM) using:
    - ATM Straddle movement
    - India VIX
    - Historical intraday volatility
    - Gap probability
    - Event impact score
    """
    
    def __init__(self):
        self.market_data = {}
        self.historical_data = []  # List of historical candles
        self.vix_data = {}
        self.event_calendar = {}  # Would be populated with economic events
        
    def update_market_data(self, market_data: Dict[str, Any]):
        """Update current market data."""
        self.market_data = market_data
        
    def update_historical_data(self, historical_data: List[Dict[str, Any]]):
        """Update historical price data for volatility calculations."""
        self.historical_data = historical_data
        
    def update_vix_data(self, vix_data: Dict[str, Any]):
        """Update India VIX data."""
        self.vix_data = vix_data
        
    def calculate_atm_straddle_movement(self) -> Dict[str, Any]:
        """
        Calculate ATM straddle price movement and implied volatility change.
        
        Returns:
            Dict with straddle movement score and volatility change.
        """
        if not self.market_data:
            return {"straddle_score": 0, "iv_change": 0, "assessment": "NO_DATA"}
            
        # Get ATM straddle price (simplified - would use actual ATM options)
        atm_call_price = self.market_data.get("atm_call_price", 0)
        atm_put_price = self.market_data.get("atm_put_price", 0)
        prev_atm_call_price = self.market_data.get("prev_atm_call_price", 0)
        prev_atm_put_price = self.market_data.get("prev_atm_put_price", 0)
        
        current_straddle = atm_call_price + atm_put_price
        prev_straddle = prev_atm_call_price + prev_atm_put_price
        
        if prev_straddle == 0:
            straddle_change_pct = 0
        else:
            straddle_change_pct = ((current_straddle - prev_straddle) / prev_straddle) * 100
            
        # Convert to score (higher straddle movement = higher expected volatility)
        # Normalize to 0-100 scale
        straddle_score = min(100, abs(straddle_change_pct) * 10)  # 10% change = 100 score
        
        if straddle_score >= 80:
            assessment = "HIGH_VOL_EXPECTED"
        elif straddle_score >= 60:
            assessment = "ABOVE_AVG_VOL"
        elif straddle_score >= 40:
            assessment = "NORMAL_VOL"
        elif straddle_score >= 20:
            assessment = "BELOW_AVG_VOL"
        else:
            assessment = "LOW_VOL_EXPECTED"
            
        return {
            "straddle_score": round(straddle_score, 1),
            "straddle_change_pct": round(straddle_change_pct, 2),
            "current_straddle": round(current_straddle, 2),
            "prev_straddle": round(prev_straddle, 2),
            "assessment": assessment
        }
        
    def calculate_vix_component(self) -> Dict[str, Any]:
        """
        Calculate volatility component based on India VIX.
        
        Returns:
            Dict with VIX score and volatility regime assessment.
        """
        if not self.vix_data:
            return {"vix_score": 0, "vix_level": 0, "assessment": "NO_DATA"}
            
        vix_level = self.vix_data.get("close", 0)
        prev_vix = self.vix_data.get("prev_close", 0)
        
        # VIX scoring: higher VIX = higher expected volatility
        # Historical VIX range: typically 10-40, but can go higher
        if vix_level >= 30:
            vix_score = 90
            assessment = "HIGH_VOLATILITY"
        elif vix_level >= 25:
            vix_score = 80
            assessment = "ELEVATED_VOLATILITY"
        elif vix_level >= 20:
            vix_score = 60
            assessment = "MODERATE_VOLATILITY"
        elif vix_level >= 15:
            vix_score = 40
            assessment = "LOW_VOLATILITY"
        else:
            vix_score = 20
            assessment = "VERY_LOW_VOLATILITY"
            
        # Adjust for VIX change (rising VIX increases fear/volatility expectation)
        if prev_vix > 0:
            vix_change_pct = ((vix_level - prev_vix) / prev_vix) * 100
            if vix_change_pct > 10:  # VIX up more than 10%
                vix_score = min(100, vix_score + 10)
            elif vix_change_pct < -10:  # VIX down more than 10%
                vix_score = max(0, vix_score - 10)
                
        return {
            "vix_score": round(vix_score, 1),
            "vix_level": round(vix_level, 2),
            "vix_change_pct": round(((vix_level - prev_vix) / prev_vix * 100) if prev_vix > 0 else 0, 2),
            "assessment": assessment
        }
        
    def calculate_historical_volatility(self, lookback_period: int = 20) -> Dict[str, Any]:
        """
        Calculate historical intraday volatility.
        
        Returns:
            Dict with historical volatility score and percentile rank.
        """
        if len(self.historical_data) < lookback_period:
            return {"hv_score": 0, "hv_percentile": 0, "assessment": "INSUFFICIENT_DATA"}
            
        # Calculate daily returns
        returns = []
        for i in range(1, len(self.historical_data)):
            prev_close = self.historical_data[i-1].get("close", 0)
            curr_close = self.historical_data[i].get("close", 0)
            if prev_close > 0:
                daily_return = (curr_close - prev_close) / prev_close
                returns.append(daily_return)
                
        if len(returns) < lookback_period:
            return {"hv_score": 0, "hv_percentile": 0, "assessment": "INSUFFICIENT_DATA"}
            
        # Use last 'lookback_period' returns
        recent_returns = returns[-lookback_period:]
        
        # Calculate standard deviation (volatility)
        if len(recent_returns) > 1:
            hv = statistics.stdev(recent_returns) * math.sqrt(252) * 100  # Annualized %
        else:
            hv = 0
            
        # Calculate percentile of current HV vs historical range
        # For simplicity, using a rolling window of past HV values
        # In practice, would maintain a longer historical HV series
        hv_values = []
        for i in range(lookback_period, len(returns)):
            period_returns = returns[i-lookback_period:i]
            if len(period_returns) > 1:
                period_hv = statistics.stdev(period_returns) * math.sqrt(252) * 100
                hv_values.append(period_hv)
                
        if len(hv_values) > 0:
            sorted_hv = sorted(hv_values)
            current_percentile = (sum(1 for x in sorted_hv if x <= hv) / len(sorted_hv)) * 100
        else:
            current_percentile = 50  # Default to median
            
        # Score based on HV level (higher volatility = higher score)
        if hv >= 30:  # 30% annualized volatility
            hv_score = 90
        elif hv >= 25:
            hv_score = 80
        elif hv >= 20:
            hv_score = 60
        elif hv >= 15:
            hv_score = 40
        else:
            hv_score = 20
            
        # Adjust for percentile (if current HV is high relative to recent history, increase score)
        if current_percentile > 80:
            hv_score = min(100, hv_score + 10)
        elif current_percentile < 20:
            hv_score = max(0, hv_score - 10)
            
        if hv_score >= 80:
            assessment = "HIGH_VOLATILITY"
        elif hv_score >= 60:
            assessment = "ABOVE_AVG_VOLATILITY"
        elif hv_score >= 40:
            assessment = "NORMAL_VOLATILITY"
        elif hv_score >= 20:
            assessment = "BELOW_AVG_VOLATILITY"
        else:
            assessment = "LOW_VOLATILITY"
            
        return {
            "hv_score": round(hv_score, 1),
            "hv_annualized": round(hv, 2),
            "hv_percentile": round(current_percentile, 1),
            "lookback_period": lookback_period,
            "assessment": assessment
        }
        
    def calculate_gap_probability(self) -> Dict[str, Any]:
        """
        Calculate probability of gap opening based on historical patterns.
        
        Returns:
            Dict with gap probability score and assessment.
        """
        if len(self.historical_data) < 10:
            return {"gap_score": 0, "gap_probability": 0, "assessment": "INSUFFICIENT_DATA"}
            
        # Calculate gap occurrences in recent history
        gaps_up = 0
        gaps_down = 0
        total_periods = 0
        
        for i in range(1, len(self.historical_data)):
            prev_high = self.historical_data[i-1].get("high", 0)
            prev_low = self.historical_data[i-1].get("low", 0)
            curr_open = self.historical_data[i].get("open", 0)
            
            if prev_high > 0 and prev_low > 0 and curr_open > 0:
                total_periods += 1
                # Gap up: current open > previous high
                if curr_open > prev_high:
                    gaps_up += 1
                # Gap down: current open < previous low
                elif curr_open < prev_low:
                    gaps_down += 1
                    
        if total_periods == 0:
            gap_probability = 0
        else:
            gap_probability = ((gaps_up + gaps_down) / total_periods) * 100
            
        # Score based on gap probability (higher probability = higher volatility expectation)
        if gap_probability >= 30:  # 30% of days have gaps
            gap_score = 90
        elif gap_probability >= 20:
            gap_score = 80
        elif gap_probability >= 15:
            gap_score = 60
        elif gap_probability >= 10:
            gap_score = 40
        else:
            gap_score = 20
            
        # Adjust for recent gap activity (if gaps occurred recently, increase expectation)
        recent_gaps = 0
        recent_periods = min(5, total_periods)
        if recent_periods > 0:
            # Check last 5 periods for gaps
            start_idx = max(1, len(self.historical_data) - recent_periods)
            for i in range(start_idx, len(self.historical_data)):
                prev_high = self.historical_data[i-1].get("high", 0)
                prev_low = self.historical_data[i-1].get("low", 0)
                curr_open = self.historical_data[i].get("open", 0)
                if prev_high > 0 and prev_low > 0 and curr_open > 0:
                    if curr_open > prev_high or curr_open < prev_low:
                        recent_gaps += 1
                        
        recent_gap_ratio = recent_gaps / recent_periods if recent_periods > 0 else 0
        if recent_gap_ratio > 0.4:  # More than 40% of recent periods had gaps
            gap_score = min(100, gap_score + 15)
        elif recent_gap_ratio < 0.1:  # Less than 10% had gaps
            gap_score = max(0, gap_score - 10)
            
        if gap_score >= 80:
            assessment = "HIGH_GAP_PROBABILITY"
        elif gap_score >= 60:
            assessment = "ABOVE_AVG_GAP_PROB"
        elif gap_score >= 40:
            assessment = "NORMAL_GAP_PROB"
        elif gap_score >= 20:
            assessment = "LOW_GAP_PROB"
        else:
            assessment = "VERY_LOW_GAP_PROB"
            
        return {
            "gap_score": round(gap_score, 1),
            "gap_probability": round(gap_probability, 2),
            "gaps_up": gaps_up,
            "gaps_down": gaps_down,
            "total_periods": total_periods,
            "recent_g.Requests": recent_gaps,
            "assessment": assessment
        }
        
    def calculate_event_impact_score(self) -> Dict[str, Any]:
        """
        Calculate potential impact of upcoming events on volatility.
        
        Returns:
            Dict with event impact score and assessment.
        """
        # In a real implementation, this would check an event calendar
        # For now, returning a simplified version based on known patterns
        
        # Check if today is known event day (simplified)
        current_date = datetime.now().strftime("%Y-%m-%d")
        day_of_week = datetime.now().weekday()  # 0=Monday, 6=Sunday
        
        event_impact = 0
        assessment = "NO_SIGNIFICANT_EVENTS"
        event_type = "NONE"
        
        # Simplified event detection (would be replaced with real calendar)
        # RBI policy days (typically Thursdays/Fridays, quarterly)
        # Budget day (February)
        # Expiry days (weekly/thursdays)
        # US non-farm payroll (first Friday)
        
        # Weekly expiry (Thursday)
        if day_of_week == 3:  # Thursday
            event_impact = 30
            assessment = "WEEKLY_EXPIRY"
            event_type = "EXPIRY"
        # US NFP (first Friday of month)
        elif day_of_week == 4 and datetime.now().day <= 7:  # First Friday
            event_impact = 25
            assessment = "US_NFP_WEEK"
            event_type = "US_EVENT"
        # Month-end
        elif datetime.now().day >= 28:
            event_impact = 20
            assessment = "MONTH_END"
            event_type = "MONTH_END"
        # Mid-month
        elif datetime.now().day == 15:
            event_impact = 15
            assessment = "MID_MONTH"
            event_type = "MONTHLY"
            
        # Convert to score
        event_score = min(100, event_impact)
        
        return {
            "event_score": event_score,
            "event_type": event_type,
            "assessment": assessment,
            "date": current_date,
            "day_of_week": day_of_week
        }
        
    def calculate_fvm(self) -> Dict[str, Any]:
        """
        Calculate the composite Future Volatility Measure (FVM).
        
        Returns:
            Dict with FVM score (0-100) and component breakdown.
        """
        # Calculate all components
        straddle_result = self.calculate_atm_straddle_movement()
        vix_result = self.calculate_vix_component()
        hv_result = self.calculate_historical_volatility()
        gap_result = self.calculate_gap_probability()
        event_result = self.calculate_event_impact_score()
        
        # Weighted combination as per requirements:
        # ATM Straddle movement (30%)
        # India VIX (25%)
        # Historical intraday volatility (20%)
        # Gap probability (15%)
        # Event impact score (10%)
        
        fvm_score = (
            straddle_result["straddle_score"] * 0.30 +
            vix_result["vix_score"] * 0.25 +
            hv_result["hv_score"] * 0.20 +
            gap_result["gap_score"] * 0.15 +
            event_result["event_score"] * 0.10
        )
        
        # Determine volatility regime
        if fvm_score >= 80:
            volatility_regime = "HIGH_VOLATILITY"
        elif fvm_score >= 60:
            volatility_regime = "ABOVE_AVERAGE_VOLATILITY"
        elif fvm_score >= 40:
            volatility_regime = "NORMAL_VOLATILITY"
        elif fvm_score >= 20:
            volatility_regime = "BELOW_AVERAGE_VOLATILITY"
        else:
            volatility_regime = "LOW_VOLATILITY"
            
        return {
            "fvm_score": round(fvm_score, 1),
            "volatility_regime": volatility_regime,
            "components": {
                "atm_straddle": straddle_result,
                "vix": vix_result,
                "historical_volatility": hv_result,
                "gap_probability": gap_result,
                "event_impact": event_result
            }
        }
