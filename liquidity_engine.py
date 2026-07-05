from typing import Dict, Any, List
import math

class LiquidityEngine:
    """
    Analyzes market liquidity including bid/ask spread, volume strength,
    OI strength, strike liquidity scores, and market participation.
    """
    
    def __init__(self):
        self.market_data = {}
        self.option_chain = {}
        
    def update_market_data(self, market_data: Dict[str, Any]):
        """Update market data for liquidity analysis."""
        self.market_data = market_data
        
    def update_option_chain(self, option_chain: Dict[str, Any]):
        """Update option chain data."""
        self.option_chain = option_chain
        
    def calculate_bid_ask_spread_quality(self) -> Dict[str, Any]:
        """
        Calculate bid/ask spread quality for ATM options.
        
        Returns:
            Dict with spread quality score and assessment.
        """
        if not self.option_chain:
            return {"spread_quality_score": 0, "assessment": "POOR", "avg_spread_pct": 0}
            
        # Get ATM options (simplified - would use actual ATM strike)
        calls = self.option_chain.get("calls", [])
        puts = self.option_chain.get("puts", [])
        
        # For demonstration, using first few strikes as proxy for ATM
        sample_size = min(5, len(calls), len(puts))
        if sample_size == 0:
            return {"spread_quality_score": 0, "assessment": "POOR", "avg_spread_pct": 0}
            
        total_spread_pct = 0
        count = 0
        
        for i in range(sample_size):
            call = calls[i] if i < len(calls) else None
            put = puts[i] if i < len(puts) else None
            
            if call:
                bid = call.get("bid", 0)
                ask = call.get("ask", 0)
                if bid > 0 and ask > 0 and ask > bid:
                    spread_pct = ((ask - bid) / ask) * 100
                    total_spread_pct += spread_pct
                    count += 1
                    
            if put:
                bid = put.get("bid", 0)
                ask = put.get("ask", 0)
                if bid > 0 and ask > 0 and ask > bid:
                    spread_pct = ((ask - bid) / ask) * 100
                    total_spread_pct += spread_pct
                    count += 1
        
        avg_spread_pct = total_spread_pct / count if count > 0 else 100
        
        # Convert to quality score (lower spread = higher quality)
        if avg_spread_pct < 1:
            spread_quality_score = 90
            assessment = "EXCELLENT"
        elif avg_spread_pct < 2:
            spread_quality_score = 80
            assessment = "GOOD"
        elif avg_spread_pct < 3:
            spread_quality_score = 60
            assessment = "FAIR"
        elif avg_spread_pct < 5:
            spread_quality_score = 40
            assessment = "POOR"
        else:
            spread_quality_score = 20
            assessment = "VERY_POOR"
            
        return {
            "spread_quality_score": spread_quality_score,
            "assessment": assessment,
            "avg_spread_pct": round(avg_spread_pct, 2)
        }
        
    def calculate_volume_strength(self) -> Dict[str, Any]:
        """
        Calculate volume strength relative to recent averages.
        
        Returns:
            Dict with volume strength score and assessment.
        """
        if not self.market_data:
            return {"volume_strength_score": 0, "assessment": "WEAK", "volume_ratio": 0}
            
        current_volume = self.market_data.get("volume", 0)
        avg_volume = self.market_data.get("avg_volume_20d", 0)  # 20-day average
        
        if avg_volume == 0:
            volume_ratio = 0
        else:
            volume_ratio = current_volume / avg_volume
            
        # Score based on volume ratio
        if volume_ratio >= 2.0:
            volume_strength_score = 90
            assessment = "VERY_STRONG"
        elif volume_ratio >= 1.5:
            volume_strength_score = 80
            assessment = "STRONG"
        elif volume_ratio >= 1.0:
            volume_strength_score = 60
            assessment = "MODERATE"
        elif volume_ratio >= 0.5:
            volume_strength_score = 40
            assessment = "WEAK"
        else:
            volume_strength_score = 20
            assessment = "VERY_WEAK"
            
        return {
            "volume_strength_score": volume_strength_score,
            "assessment": assessment,
            "volume_ratio": round(volume_ratio, 2)
        }
        
    def calculate_oi_strength(self) -> Dict[str, Any]:
        """
        Calculate Open Interest strength and concentration.
        
        Returns:
            Dict with OI strength score and assessment.
        """
        if not self.option_chain:
            return {"oi_strength_score": 0, "assessment": "WEAK", "oi_concentration": 0}
            
        calls = self.option_chain.get("calls", [])
        puts = self.option_chain.get("puts", [])
        
        # Calculate total OI
        total_call_oi = sum(item.get("oi", 0) for item in calls)
        total_put_oi = sum(item.get("oi", 0) for item in puts)
        total_oi = total_call_oi + total_put_oi
        
        if total_oi == 0:
            return {"oi_strength_score": 0, "assessment": "WEAK", "oi_concentration": 0}
            
        # Calculate OI concentration (how much OI is in top strikes)
        all_oi = []
        for item in calls + puts:
            all_oi.append(item.get("oi", 0))
            
        all_oi.sort(reverse=True)
        top_5_oi = sum(all_oi[:5]) if len(all_oi) >= 5 else sum(all_oi)
        oi_concentration = (top_5_oi / total_oi) * 100 if total_oi > 0 else 0
        
        # Score based on OI concentration and total OI level
        # Higher concentration can indicate strong levels but also potential pin risk
        if oi_concentration > 70:
            oi_strength_score = 70  # High concentration - moderate score due to pin risk
            assessment = "HIGH_CONCENTRATION"
        elif oi_concentration > 50:
            oi_strength_score = 80
            assessment = "GOOD_CONCENTRATION"
        else:
            oi_strength_score = 60
            assessment = "DISTRIBUTED"
            
        # Adjust for total OI level (higher OI generally better liquidity)
        # Normalize OI to a 0-30 point scale
        oi_level_score = min(30, math.log10(max(total_oi, 1)) * 5)  # Log scale
        
        final_score = min(100, oi_strength_score + oi_level_score)
        
        if final_score >= 80:
            final_assessment = "STRONG"
        elif final_score >= 60:
            final_assessment = "MODERATE"
        else:
            final_assessment = "WEAK"
            
        return {
            "oi_strength_score": round(final_score, 1),
            "assessment": final_assessment,
            "oi_concentration": round(oi_concentration, 1),
            "total_oi": total_oi
        }
        
    def calculate_strike_liquidity_score(self, strike: float) -> Dict[str, Any]:
        """
        Calculate liquidity score for a specific strike.
        
        Args:
            strike: The strike price to analyze
            
        Returns:
            Dict with liquidity score for the strike.
        """
        if not self.option_chain:
            return {"strike": strike, "liquidity_score": 0, "assessment": "NO_DATA"}
            
        # Find call and put data for this strike
        call_data = None
        put_data = None
        
        for item in self.option_chain.get("calls", []):
            if abs(item.get("strike", 0) - strike) < 0.5:  # Within 0.5 points
                call_data = item
                break
                
        for item in self.option_chain.get("puts", []):
            if abs(item.get("strike", 0) - strike) < 0.5:
                put_data = item
                break
                
        if not call_data and not put_data:
            return {"strike": strike, "liquidity_score": 0, "assessment": "NOT_FOUND"}
            
        # Calculate liquidity based on bid/ask spread and volume/OI
        liquidity_components = []
        
        # Call liquidity
        if call_data:
            bid = call_data.get("bid", 0)
            ask = call_data.get("ask", 0)
            volume = call_data.get("volume", 0)
            oi = call_data.get("oi", 0)
            
            # Spread component (lower spread = higher score)
            if bid > 0 and ask > 0 and ask > bid:
                spread_score = max(0, 100 - ((ask - bid) / ask) * 1000)  # Penalize wide spreads
                liquidity_components.append(spread_score * 0.4)  # 40% weight
            else:
                liquidity_components.append(0)  # No valid spread
                
            # Volume/OI component
            if oi > 0:
                volume_oi_ratio = volume / oi if oi > 0 else 0
                volume_score = min(100, volume_oi_ratio * 100)  # Higher volume/OI = better liquidity
                liquidity_components.append(volume_score * 0.3)  # 30% weight
            else:
                liquidity_components.append(0)
                
            # OI level component
            oi_score = min(100, math.log10(max(oi, 1)) * 10)  # Log scale for OI
            liquidity_components.append(oi_score * 0.3)  # 30% weight
            
        # Put liquidity (similar logic)
        if put_data:
            bid = put_data.get("bid", 0)
            ask = put_data.get("ask", 0)
            volume = put_data.get("volume", 0)
            oi = put_data.get("oi", 0)
            
            # Spread component
            if bid > 0 and ask > 0 and ask > bid:
                spread_score = max(0, 100 - ((ask - bid) / ask) * 1000)
                liquidity_components.append(spread_score * 0.4)
            else:
                liquidity_components.append(0)
                
            # Volume/OI component
            if oi > 0:
                volume_oi_ratio = volume / oi if oi > 0 else 0
                volume_score = min(100, volume_oi_ratio * 100)
                liquidity_components.append(volume_score * 0.3)
            else:
                liquidity_components.append(0)
                
            # OI level component
            oi_score = min(100, math.log10(max(oi, 1)) * 10)
            liquidity_components.append(oi_score * 0.3)
            
        # Average the components (if we have both call and put data, we'll have 6 components)
        if liquidity_components:
            strike_liquidity_score = sum(liquidity_components) / len(liquidity_components)
        else:
            strike_liquidity_score = 0
            
        # Assessment
        if strike_liquidity_score >= 80:
            assessment = "EXCELLENT"
        elif strike_liquidity_score >= 60:
            assessment = "GOOD"
        elif strike_liquidity_score >= 40:
            assessment = "FAIR"
        elif strike_liquidity_score >= 20:
            assessment = "POOR"
        else:
            assessment = "VERY_POOR"
            
        return {
            "strike": strike,
            "liquidity_score": round(strike_liquidity_score, 1),
            "assessment": assessment,
            "has_call_data": call_data is not None,
            "has_put_data": put_data is not None
        }
        
    def calculate_market_participation_score(self) -> Dict[str, Any]:
        """
        Calculate overall market participation score.
        
        Returns:
            Dict with market participation score and assessment.
        """
        if not self.market_data or not self.option_chain:
            return {"market_participation_score": 0, "assessment": "LOW", "participation_ratio": 0}
            
        # Combine volume, OI, and trade count indicators
        volume = self.market_data.get("volume", 0)
        avg_volume = self.market_data.get("avg_volume_20d", 1)
        
        total_call_oi = sum(item.get("oi", 0) for item in self.option_chain.get("calls", []))
        total_put_oi = sum(item.get("oi", 0) for item in self.option_chain.get("puts", []))
        total_oi = total_call_oi + total_put_oi
        
        avg_oi = self.market_data.get("avg_oi_20d", 1)  # Would need to track this
        
        # Participation ratio (current vs average)
        volume_ratio = volume / avg_volume if avg_volume > 0 else 0
        oi_ratio = total_oi / avg_oi if avg_oi > 0 else 0
        
        # Combined participation score
        participation_ratio = (volume_ratio + oi_ratio) / 2
        
        if participation_ratio >= 2.0:
            market_participation_score = 90
            assessment = "VERY_HIGH"
        elif participation_ratio >= 1.5:
            market_participation_score = 80
            assessment = "HIGH"
        elif participation_ratio >= 1.0:
            market_participation_score = 60
            assessment = "MODERATE"
        elif participation_ratio >= 0.5:
            market_participation_score = 40
            assessment = "LOW"
        else:
            market_participation_score = 20
            assessment = "VERY_LOW"
            
        return {
            "market_participation_score": round(market_participation_score, 1),
            "assessment": assessment,
            "participation_ratio": round(participation_ratio, 2),
            "volume_ratio": round(volume_ratio, 2),
            "oi_ratio": round(oi_ratio, 2)
        }
