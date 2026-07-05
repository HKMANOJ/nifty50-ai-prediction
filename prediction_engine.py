from typing import Any, Dict, List, Optional
import json
from datetime import datetime
from options_engine import OptionsEngine
from liquidity_engine import LiquidityEngine
from fvm_engine import FVMEngine

class PredictionEngine:
    """
    The core prediction engine that aggregates data from various sources,
    applies scoring, and generates trading signals.
    """

    def __init__(self):
        self.market_data = {}
        self.indicators = {}
        self.patterns = {}
        self.market_structure = {}
        self.option_chain_analysis = {}
        self.news_sentiment = {}
        self.fii_dii_data = {}
        
        # Initialize analysis engines
        self.options_engine = OptionsEngine()
        self.liquidity_engine = LiquidityEngine()
        self.fvm_engine = FVMEngine()
        
        # Signal weights (Market Structure 50%, Support/Resistance 25%, OI/PCR 15%, Volume Confirmation 10%)
        self.weights = {
            "market_structure": 0.50,
            "support_resistance": 0.25,
            "option_chain": 0.15,
            "volume_confirmation": 0.10
        }
        
        # Dynamic weight performance tracking (backwards compatibility)
        self.weight_performance = {
            "market_structure": {"correct": 0, "total": 0},
            "support_resistance": {"correct": 0, "total": 0},
            "option_chain": {"correct": 0, "total": 0},
            "volume_confirmation": {"correct": 0, "total": 0}
        }
        
        # Prediction history for learning
        self.prediction_history = []
        
        # Load disabled patterns (for backtester auto-disable rule)
        self.disabled_patterns = []
        try:
            import os
            manifest_path = "/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50/disabled_patterns.json"
            if os.path.exists(manifest_path):
                with open(manifest_path, "r") as f:
                    self.disabled_patterns = json.load(f)
        except Exception:
            pass

    def update_data(self, data_type: str, data: Any):
        """
        Updates the internal data store with the latest information.
        """
        if data_type == "market_data":
            self.market_data = data
        elif data_type == "indicators":
            self.indicators = data
        elif data_type == "patterns":
            self.patterns = data
        elif data_type == "market_structure":
            self.market_structure = data
        elif data_type == "option_chain_analysis":
            self.option_chain_analysis = data
        elif data_type == "news_sentiment":
            self.news_sentiment = data
        elif data_type == "fii_dii_data":
            self.fii_dii_data = data
        else:
            print(f"Warning: Unknown data type {data_type}")

    def update_options_data(self, option_chain_data: Dict[str, Any]):
        """Update option chain data for options analysis."""
        self.options_engine.update_option_chain(option_chain_data)
        
    def update_historical_oi(self, historical_oi: Dict[str, Any]):
        """Update historical OI data for migration analysis."""
        self.options_engine.update_historical_oi(historical_oi)
        
    def update_liquidity_data(self, market_data: Dict[str, Any], option_chain: Dict[str, Any]):
        """Update data for liquidity analysis."""
        self.liquidity_engine.update_market_data(market_data)
        self.liquidity_engine.update_option_chain(option_chain)
        
    def update_fvm_data(self, market_data: Dict[str, Any], historical_data: List[Dict[str, Any]], vix_data: Dict[str, Any]):
        """Update data for FVM calculation."""
        self.fvm_engine.update_market_data(market_data)
        self.fvm_engine.update_historical_data(historical_data)
        self.fvm_engine.update_vix_data(vix_data)

    def generate_signal(self) -> Dict[str, Any]:
        """
        Generates a trading signal based on aggregated data and scoring rules.
        """
        reasons = []
        # Run pattern analysis first to populate self.primary_pattern
        market_struct_score, struct_reasons = self._analyze_market_structure()
        reasons.extend(struct_reasons)
        
        primary_pattern = getattr(self, "primary_pattern", "NONE")
        current_structure = self.market_structure.get("current_structure", "UNKNOWN")
        
        # 1. Market Structure Base Score (Weight: 50% Pattern / 50% Breakout Confirmation)
        pat_data = self.patterns.get(primary_pattern, {})
        direction = pat_data.get("direction", "neutral") if isinstance(pat_data, dict) else "neutral"
        
        ms_score = 0
        p_lower = primary_pattern.lower()
        if direction == "bullish":
            ms_score = 75
        elif direction == "bearish":
            ms_score = -75
            
        if current_structure == "UPTREND":
            ms_score += 25
        elif current_structure == "DOWNTREND":
            ms_score -= 25
            
        # Breakout Confirmation (50% Pattern, 50% Breakout)
        current_price = self.market_data.get("price", 0)
        support_resistance = self._calculate_support_resistance()
        support_val = support_resistance.get("support", 0)
        resistance_val = support_resistance.get("resistance", 0)
        
        breakout_val = None
        if isinstance(pat_data, dict):
            levels = pat_data.get("levels", {})
            if isinstance(levels, dict):
                # Try finding numeric levels first
                breakout_val = levels.get("neckline")
                if breakout_val is None:
                    breakout_val = levels.get("flag_high" if direction == "bullish" else "flag_low")
                if breakout_val is None:
                    breakout_val = levels.get("resistance" if direction == "bullish" else "support")
                if breakout_val is None:
                    breakout_val = levels.get("upper_trendline" if direction == "bullish" else "lower_trendline")
                if breakout_val is None:
                    # Fallback to boolean breakout check
                    is_broke = levels.get("breakout") or levels.get("breakdown")
                    if is_broke is True:
                        breakout_val = current_price
                    elif is_broke is False:
                        breakout_val = current_price + 1000 if direction == "bullish" else current_price - 1000
                    
        try:
            breakout_level = float(breakout_val) if (breakout_val is not None and not isinstance(breakout_val, bool)) else (resistance_val if direction == "bullish" else support_val)
        except (ValueError, TypeError):
            breakout_level = (resistance_val if direction == "bullish" else support_val)
        
        breakout_score = 0
        if direction == "bullish":
            if current_price >= breakout_level:
                breakout_score = 100
                reasons.append(f"Breakout candle close confirmed above {breakout_level:.1f} (+50 MS)")
        elif direction == "bearish":
            if current_price <= breakout_level:
                breakout_score = -100
                reasons.append(f"Breakdown candle close confirmed below {breakout_level:.1f} (-50 MS)")
                
        market_structure_score = ms_score * 0.50 + breakout_score * 0.50
        composite_score = market_structure_score
        
        # 2. Support / Resistance (25% weight)
        # Price bouncing from support: +15 bullish
        # Price rejecting from resistance: +15 bearish (-15 composite score)
        if support_val > 0 and resistance_val > 0:
            dist_support = abs(current_price - support_val)
            dist_resistance = abs(current_price - resistance_val)
            if dist_support < 20:
                composite_score += 15
                reasons.append("Price bouncing from support (+15 composite)")
            elif dist_resistance < 20:
                composite_score -= 15
                reasons.append("Price rejecting from resistance (-15 composite)")
                
        # 3. OI / PCR (15% weight) - V3 advisory signals
        # PCR adjustment: if pcr > 1.1: +10, elif pcr < 0.8: -10
        # OI missing: 0 (no penalty)
        oi_pcr_signals = self.options_engine.generate_oi_pcr_signals() if hasattr(self, 'options_engine') else {}
        pcr = oi_pcr_signals.get("pcr") if isinstance(oi_pcr_signals, dict) else 1.0
        if pcr is None:
            pcr = 1.0
            
        if pcr > 1.1:
            composite_score += 10
            reasons.append("Bullish PCR advisory signal (+10 composite)")
        elif pcr < 0.8:
            composite_score -= 10
            reasons.append("Bearish PCR advisory signal (-10 composite)")
            
        # Normalize pattern names to V3 whitelist classes
        normalized_pattern = "NONE"
        if "double bottom" in p_lower or "w pattern" in p_lower:
            normalized_pattern = "DOUBLE_BOTTOM"
        elif "double top" in p_lower or "m pattern" in p_lower:
            normalized_pattern = "DOUBLE_TOP"
        elif "breakout" in p_lower or "breakdown" in p_lower or "continuation" in p_lower or "trendline" in p_lower or "break" in p_lower:
            normalized_pattern = "BREAKOUT_CONTINUATION"
        elif primary_pattern != "NONE":
            normalized_pattern = primary_pattern.upper().replace(" ", "_")
            
        # 4. Volume Confirmation (10% weight) - V3 advisory signals
        # If volume_ratio > 1.2: +10 (bullish) or -10 (bearish)
        volume = self.market_data.get("volume", 0)
        avg_volume = self.market_data.get("avg_volume_20d", 1)
        volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        if volume_ratio > 1.2:
            if direction == "bullish":
                composite_score += 10
                reasons.append("Volume confirmation bullish (+10 composite)")
            elif direction == "bearish":
                composite_score -= 10
                reasons.append("Volume confirmation bearish (-10 composite)")
            
        # 5. Pattern Confidence Boosts (DOUBLE_TOP = -25, DOUBLE_BOTTOM = +25, BREAKOUT_CONTINUATION = +20)
        if normalized_pattern == "DOUBLE_TOP":
            composite_score -= 25
            reasons.append("Double Top confidence boost (-25 composite)")
        elif normalized_pattern == "DOUBLE_BOTTOM":
            composite_score += 25
            reasons.append("Double Bottom confidence boost (+25 composite)")
        elif normalized_pattern == "BREAKOUT_CONTINUATION":
            if direction == "bullish":
                composite_score += 20
                reasons.append("Breakout Continuation confidence boost (+20 composite)")
            else:
                composite_score -= 20
                reasons.append("Breakout Continuation confidence boost (-20 composite)")
            
        # Determine signal based on pattern and direction (strictly requiring breakout confirmation to avoid assumptions)
        signal = "WAIT"
        if direction == "bullish":
            if current_price >= breakout_level:
                signal = "CALL"
            else:
                reasons.append(f"Bullish setup detected, but breakout level ({breakout_level:.1f}) not yet broken. Waiting.")
        elif direction == "bearish":
            if current_price <= breakout_level:
                signal = "PUT"
            else:
                reasons.append(f"Bearish setup detected, but breakdown level ({breakout_level:.1f}) not yet broken. Waiting.")
            
        # Convert score to confidence (V3 formula): confidence = min(99, max(50, int(50 + abs(score) * 50)))
        confidence_value = min(99, max(50, int(50 + abs(composite_score / 100) * 50)))
        
        # Update confidence value for output (wait gets neutral 50)
        if signal == "WAIT":
            confidence_value = 50
            
        # Determine support and resistance levels
        support_resistance = self._calculate_support_resistance()
        support_val = support_resistance.get("support", 0)
        resistance_val = support_resistance.get("resistance", 0)
        
        # Calculate targets and stop losses using V2 formulas
        current_price = self.market_data.get("price", 0)
        target1 = current_price
        target2 = current_price
        stop_loss = current_price
        
        pattern_data = self.patterns.get(primary_pattern, {})
        if not isinstance(pattern_data, dict):
            # Try finding by normalized pattern name key
            pattern_data = self.patterns.get("Double Top" if "TOP" in normalized_pattern else "Double Bottom" if "BOTTOM" in normalized_pattern else "Trendline Breakout" if "BREAKOUT" in normalized_pattern else "NONE", {})
            if not isinstance(pattern_data, dict):
                pattern_data = {}
        levels = pattern_data.get("levels", {}) if isinstance(pattern_data, dict) else {}
        
        if normalized_pattern == "DOUBLE_TOP":
            top_1 = float(levels.get("top_1") or current_price + 50)
            top_2 = float(levels.get("top_2") or current_price + 50)
            neckline = float(levels.get("neckline") or current_price - 10)
            top = (top_1 + top_2) / 2
            height = top - neckline
            target1 = neckline - height
            target2 = neckline - (height * 2)
            stop_loss = top_2
            
        elif normalized_pattern == "DOUBLE_BOTTOM":
            bottom_1 = float(levels.get("bottom_1") or current_price - 50)
            bottom_2 = float(levels.get("bottom_2") or current_price - 50)
            neckline = float(levels.get("neckline") or current_price + 10)
            bottom = (bottom_1 + bottom_2) / 2
            height = neckline - bottom
            target1 = neckline + height
            target2 = neckline + (height * 2)
            stop_loss = bottom_2
            
        elif normalized_pattern == "BREAKOUT_CONTINUATION":
            breakout_level = float(
                levels.get("breakout")
                or levels.get("upper_trendline" if direction == "bullish" else "lower_trendline")
                or (resistance_val if direction == "bullish" else support_val)
                or current_price
            )
            breakout_range = float(levels.get("range") or 50.0)
            if direction == "bullish":
                target1 = breakout_level + breakout_range
                target2 = breakout_level + (breakout_range * 2)
            else:
                target1 = breakout_level - breakout_range
                target2 = breakout_level - (breakout_range * 2)
            stop_loss = breakout_level
            
        else:
            if direction == "bullish":
                target1 = current_price + 50.0
                target2 = current_price + 100.0
                stop_loss = current_price - 30.0
            elif direction == "bearish":
                target1 = current_price - 50.0
                target2 = current_price - 100.0
                stop_loss = current_price + 30.0
            else:
                target1 = current_price
                target2 = current_price
                stop_loss = current_price
                
        # Enforce strict Project Philosophy Risk Management rules:
        if signal != "WAIT":
            raw_sl_size = abs(stop_loss - current_price)
            # Clamp SL to stay between 10 and 15 points (default to 12 if undefined/zero)
            sl_size = max(10.0, min(15.0, raw_sl_size if raw_sl_size > 0 else 12.0))
            
            raw_target_size = abs(target1 - current_price)
            # Target must be at least 35 points, and at least 2x the SL size to guarantee >= 1:2 RR
            target_size = max(35.0, sl_size * 2.0, raw_target_size)
            
            if signal == "CALL":
                stop_loss = current_price - sl_size
                target1 = current_price + target_size
                target2 = current_price + (target_size * 1.5)
            elif signal == "PUT":
                stop_loss = current_price + sl_size
                target1 = current_price - target_size
                target2 = current_price - (target_size * 1.5)
                
            reasons.append(f"Risk Matrix aligned: Target {target_size:.1f} pts, Stop Loss {sl_size:.1f} pts (R:R 1:{target_size/sl_size:.2f})")
            
        # Backend Stop Loss Invalidation (V3 UI Spec)
        if signal == "CALL" and current_price < stop_loss:
            signal = "WAIT"
            normalized_pattern = "WAIT (Invalidated)"
        elif signal == "PUT" and current_price > stop_loss:
            signal = "WAIT"
            normalized_pattern = "WAIT (Invalidated)"

        # V3 Option Selection
        atm_val = int(round(current_price / 50) * 50)
        opt_suffix = "CE" if signal == "CALL" else "PE" if signal == "PUT" else "CE"
        
        atm_strike = f"{atm_val} {opt_suffix}"
        otm1_strike = f"{atm_val + 50 if signal == 'CALL' else atm_val - 50} {opt_suffix}"
        otm2_strike = f"{atm_val + 100 if signal == 'CALL' else atm_val - 100} {opt_suffix}"
        
        # Prepare final prediction
        prediction = {
            # 8 requested Version 2 keys
            "signal": signal,
            "confidence": confidence_value,
            "pattern": normalized_pattern,
            "support": round(support_val, 2),
            "resistance": round(resistance_val, 2),
            "target1": round(target1, 2),
            "target2": round(target2, 2),
            "stoploss": round(stop_loss, 2),
            
            # V3 Option Selection Engine keys
            "atm_strike": atm_strike,
            "otm1_strike": otm1_strike,
            "otm2_strike": otm2_strike,
            "lot_size": 65,
            
            # Backwards compatibility keys
            "direction": "BUY " + signal if signal != "WAIT" else "WAIT",
            "confidence_score": round(composite_score, 1),
            "market_structure": current_structure,
            "pcr_signal": oi_pcr_signals.get("pcr_signal", "NEUTRAL"),
            "oi_signal": oi_pcr_signals.get("oi_signal", "NEUTRAL"),
            "fvm_score": round(composite_score, 1),
            "target_15m": target1,
            "target_30m": target2,
            "target_1h": target2,
            "target_eod": target2,
            "risk_level": self._assess_risk_level(composite_score, {"fvm": {"score": 50}}),
            "reasoning": reasons[:10],
            "signal_components": {
                "market_structure": round(ms_score * 0.5, 1),
                "support_resistance": round(15.0 if "bouncing" in "".join(reasons) else -15.0 if "rejecting" in "".join(reasons) else 0.0, 1),
                "option_chain": round(composite_score - (ms_score * 0.5), 1),
                "volume_confirmation": round(10.0 if volume_ratio > 1.2 else 0.0, 1)
            }
        }
        
        # Store prediction for learning
        self._store_prediction(prediction)
        
        return prediction

    def _analyze_market_data(self) -> tuple[float, List[str]]:
        """Analyze market data and return score and reasons."""
        score = 0
        reasons = []
        
        if not self.market_data:
            return score, reasons
            
        # Price action relative to VWAP
        current_price = self.market_data.get("price", 0)
        vwap = self.market_data.get("vwap", 0)
        
        if vwap > 0:
            if current_price > vwap * 1.001:  # Above VWAP
                score += 20
                reasons.append("Price above VWAP (bullish)")
            elif current_price < vwap * 0.999:  # Below VWAP
                score -= 20
                reasons.append("Price below VWAP (bearish)")
                
        # Volume analysis
        volume = self.market_data.get("volume", 0)
        avg_volume = self.market_data.get("avg_volume_20d", 1)
        volume_ratio = volume / avg_volume if avg_volume > 0 else 0
        
        if volume_ratio > 1.5:
            score += 15
            reasons.append(f"High volume ({volume_ratio:.1f}x avg)")
        elif volume_ratio > 1.0:
            score += 10
            reasons.append("Above average volume")
            
        # FII/DII data
        fii_dii = self.fii_dii_data.get("net_fii", 0)
        if fii_dii > 1000:  # Significant FII buying
            score += 15
            reasons.append(f"Strong FII buying (+{fii_dii:.0f}cr)")
        elif fii_dii < -1000:  # Significant FII selling
            score -= 15
            reasons.append(f"Strong FII selling ({fii_dii:.0f}cr)")
            
        # Global market signals
        global_signals = self.market_data.get("global_signals", {})
        if global_signals:
            us_futures = global_signals.get("us_futures", 0)
            asian_markets = global_signals.get("asian_markets", 0)
            
            if us_futures > 0.5 and asian_markets > 0.3:
                score += 10
                reasons.append("Positive global cues")
            elif us_futures < -0.5 and asian_markets < -0.3:
                score -= 10
                reasons.append("Negative global cues")
                
        # Gift Nifty (overnight signal)
        gift_nifty = self.market_data.get("gift_nifty_change", 0)
        if abs(gift_nifty) > 0.3:
            if gift_nifty > 0:
                score += 10
                reasons.append(f"Gift Nifty up {gift_nifty:.2f}%")
            else:
                score -= 10
                reasons.append(f"Gift Nifty down {gift_nifty:.2f}%")
                
        return max(min(score, 100), -100), reasons  # Allow negative scores for bearish

    def _analyze_market_structure(self) -> tuple[float, List[str]]:
        """Analyze market structure chart patterns and trend."""
        score = 0
        reasons = []
        
        # 1. Chart patterns (only Double Top, Double Bottom, Head & Shoulders, Inverse Head & Shoulders, Trendline Breakout/Break, flags)
        allowed_patterns = {
            "Double Top": -75,
            "Double Top Breakdown": -75,
            "Double Bottom": 75,
            "Double Bottom Breakout": 75,
            "Head & Shoulders": -75,
            "Inverse Head & Shoulders": 75,
            "Trendline Break": 75,
            "Trendline Breakout": 75,
            "Bull Flag": 75,
            "Bear Flag": -75,
            "Ascending Triangle": 75,
            "Descending Triangle": -75,
            "Symmetrical Triangle": 50
        }
        
        detected_pats = []
        for pat_name, pat_score in allowed_patterns.items():
            if self.patterns.get(pat_name):
                # If it's a dict or bool
                pat_obj = self.patterns[pat_name]
                direction = None
                if isinstance(pat_obj, dict):
                    direction = pat_obj.get("direction")
                    
                actual_score = pat_score
                if direction == "bearish" and actual_score > 0:
                    actual_score = -actual_score
                elif direction == "bullish" and actual_score < 0:
                    actual_score = -actual_score
                    
                score += actual_score
                detected_pats.append(pat_name)
                reasons.append(f"Chart pattern: {pat_name} ({'Bullish' if actual_score > 0 else 'Bearish'})")
                
        # 2. Trend structure
        current_structure = self.market_structure.get("current_structure", "UNKNOWN")
        trend_strength = self.market_structure.get("trend_strength", 50)
        if current_structure == "UPTREND":
            score += min(trend_strength // 2, 25)
            reasons.append(f"Uptrend aligned (strength: {trend_strength})")
        elif current_structure == "DOWNTREND":
            score -= min(trend_strength // 2, 25)
            reasons.append(f"Downtrend aligned (strength: {trend_strength})")
            
        # V2 confidence boosts
        for pat in detected_pats:
            pat_lower = pat.lower()
            if "double top" in pat_lower:
                score -= 25
                reasons.append("Double Top confidence boost (+25 bearish)")
            elif "double bottom" in pat_lower:
                score += 25
                reasons.append("Double Bottom confidence boost (+25 bullish)")
            elif "breakout" in pat_lower or "continuation" in pat_lower:
                score += 20
                reasons.append("Breakout Continuation confidence boost (+20 bullish)")
                
        # Store primary pattern for details mapping. Prioritize enabled patterns under V2.
        enabled_detected = []
        for pat in detected_pats:
            p_lower = pat.lower()
            norm = "NONE"
            if "double bottom" in p_lower or "w pattern" in p_lower:
                norm = "DOUBLE_BOTTOM"
            elif "double top" in p_lower or "m pattern" in p_lower:
                norm = "DOUBLE_TOP"
            elif "breakout" in p_lower or "breakdown" in p_lower or "continuation" in p_lower or "trendline" in p_lower:
                norm = "BREAKOUT_CONTINUATION"
            
            if norm not in getattr(self, "disabled_patterns", []):
                enabled_detected.append(pat)
                
        if enabled_detected:
            self.primary_pattern = enabled_detected[0]
        else:
            self.primary_pattern = detected_pats[0] if detected_pats else "NONE"
        return max(min(score, 100), -100), reasons

    def _analyze_support_resistance(self) -> tuple[float, List[str]]:
        """Analyze price position relative to support/resistance."""
        score = 0
        reasons = []
        if not self.market_data:
            return score, reasons
            
        current_price = self.market_data.get("price", 0)
        if current_price == 0:
            return score, reasons
            
        sr = self._calculate_support_resistance()
        support = sr.get("support", 0)
        resistance = sr.get("resistance", 0)
        
        if support > 0 and resistance > 0:
            dist_support = abs(current_price - support)
            dist_resistance = abs(current_price - resistance)
            
            # Near support bounce
            if dist_support < 20:
                score += 15
                reasons.append(f"Price bouncing from support {support:.1f} (+15 bullish)")
            # Near resistance rejection
            elif dist_resistance < 20:
                score -= 15
                reasons.append(f"Price rejecting from resistance {resistance:.1f} (+15 bearish)")
            else:
                reasons.append(f"Price mid-range between {support:.0f} and {resistance:.0f}")
                
        return max(min(score, 100), -100), reasons

    def _analyze_breakout_confirmation(self) -> tuple[float, List[str]]:
        """Analyze breakout/breakdown confirmed by volume."""
        reasons = []
        volume = self.market_data.get("volume", 0)
        avg_volume = self.market_data.get("avg_volume_20d", 1)
        volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        
        if volume_ratio > 1.2:
            score = 100
            reasons.append(f"Volume confirmation: {volume_ratio:.1f}x average (>1.2x)")
        else:
            score = 0
            reasons.append(f"Volume normal: {volume_ratio:.1f}x average")
            
        return score, reasons

    def _analyze_option_chain(self) -> tuple[float, List[str]]:
        """Analyze call/put OI change and PCR only."""
        score = 0
        reasons = []
        
        # Get PCR and OI change details
        oi_pcr_signals = self.options_engine.generate_oi_pcr_signals() if hasattr(self, 'options_engine') else {}
        pcr = oi_pcr_signals.get("pcr") if isinstance(oi_pcr_signals, dict) else None
        if pcr is None:
            pcr = 1.0
            
        # PCR scoring adjustments (V2)
        if pcr > 1.1:
            score += 33.33
            reasons.append(f"Bullish PCR ({pcr:.2f}) adjustment (+5 composite)")
        elif pcr < 0.8:
            score -= 33.33
            reasons.append(f"Bearish PCR ({pcr:.2f}) adjustment (-5 composite)")
            
        # OI Missing scoring adjustments (V2)
        put_oi_change = self.option_chain_analysis.get("put_oi_change", 0) if self.option_chain_analysis else 0
        call_oi_change = self.option_chain_analysis.get("call_oi_change", 0) if self.option_chain_analysis else 0
        oi_missing = (put_oi_change == 0 and call_oi_change == 0)
        
        if oi_missing:
            score -= 33.33
            reasons.append("OI data missing penalty (-5 composite)")
        else:
            reasons.append("OI data available")
            
        return max(min(score, 100), -100), reasons

    # ---- Backwards compatibility stubs ----
    def _analyze_patterns(self) -> tuple[float, List[str]]:
        return self._analyze_market_structure()

    def _analyze_oi_pcr(self) -> tuple[float, List[str]]:
        return self._analyze_option_chain()

    def _analyze_fvm(self) -> tuple[float, List[str]]:
        return 0.0, ["FVM disabled per requirements"]

    def _analyze_market_data(self) -> tuple[float, List[str]]:
        return 0.0, ["Market data details folded into support/resistance"]

    def _get_market_data_details(self) -> Dict[str, Any]:
        """Get detailed market data information."""
        if not self.market_data:
            return {}
            
        return {
            "price": self.market_data.get("price", 0),
            "vwap": self.market_data.get("vwap", 0),
            "volume": self.market_data.get("volume", 0),
            "avg_volume": self.market_data.get("avg_volume_20d", 0),
            "fii_dii": self.fii_dii_data.get("net_fii", 0),
            "gift_nifty": self.market_data.get("gift_nifty_change", 0)
        }

    def _get_pattern_details(self) -> Dict[str, Any]:
        """Get detailed pattern information."""
        if not self.patterns:
            return {}
            
        # Find primary pattern (strongest signal)
        bullish_patterns = ["Hammer", "Inverted Hammer", "Bullish Engulfing", 
                           "Tweezer Bottom", "Bullish Harami", "Breakout"]
        bearish_patterns = ["Shooting Star", "Bearish Engulfing", "Tweezer Top",
                           "Bearish Harami", "Breakdown"]
                           
        primary_pattern = "NONE"
        for pattern in bullish_patterns + bearish_patterns:
            if self.patterns.get(pattern):
                primary_pattern = pattern
                break
                
        return {
            "primary_pattern": primary_pattern,
            "bullish_patterns": [p for p in bullish_patterns if self.patterns.get(p)],
            "bearish_patterns": [p for p in bearish_patterns if self.patterns.get(p)]
        }

    def _get_oi_pcr_details(self) -> Dict[str, Any]:
        """Get detailed OI/PCR information."""
        if not self.option_chain_analysis:
            return {}
            
        return self.options_engine.generate_oi_pcr_signals()

    def _get_market_structure_details(self) -> Dict[str, Any]:
        """Get detailed market structure information."""
        if not self.market_structure:
            return {}
            
        return {
            "current_structure": self.market_structure.get("current_structure", "UNKNOWN"),
            "trend_strength": self.market_structure.get("trend_strength", 0),
            "higher_highs_count": len(self.market_structure.get("higher_highs", [])),
            "higher_lows_count": len(self.market_structure.get("higher_lows", [])),
            "lower_highs_count": len(self.market_structure.get("lower_highs", [])),
            "lower_lows_count": len(self.market_structure.get("lower_lows", [])),
            "break_of_structure": self.market_structure.get("break_of_structure", []),
            "change_of_character": self.market_structure.get("change_of_character", []),
            "supply_zones_count": len(self.market_structure.get("supply_zones", [])),
            "demand_zones_count": len(self.market_structure.get("demand_zones", [])),
            "primary_pattern": getattr(self, "primary_pattern", "NONE")
        }

    def _get_fvm_details(self) -> Dict[str, Any]:
        """Get detailed FVM information."""
        if not hasattr(self, 'fvm_engine'):
            return {}
            
        fvm_result = self.fvm_engine.calculate_fvm()
        return {
            "fvm_score": fvm_result.get("fvm_score", 0),
            "volatility_regime": fvm_result.get("volatility_regime", "UNKNOWN"),
            "components": fvm_result.get("components", {})
        }

    def _score_to_signal(self, score: float) -> tuple[str, str]:
        """Convert numerical score to signal and confidence.
        
        Score ranges (0-100 scale, 50 = neutral):
          >= 85  → STRONG_CALL (HIGH confidence)
          70-84  → CALL (MEDIUM confidence)
          60-69  → WEAK_CALL (LOW confidence)
          41-59  → WAIT (LOW confidence)
          21-40  → WEAK_PUT (LOW confidence)
          6-20   → PUT (MEDIUM confidence)
          <= 5   → STRONG_PUT (HIGH confidence)
        """
        # Bullish signals (check highest first)
        if score >= 85:
            return "STRONG_CALL", "HIGH"
        elif score >= 70:
            return "CALL", "MEDIUM"
        elif score >= 60:
            return "WEAK_CALL", "LOW"
        # Bearish signals (check lowest first — critical ordering fix)
        elif score <= 5:
            return "STRONG_PUT", "HIGH"
        elif score <= 20:
            return "PUT", "MEDIUM"
        elif score <= 40:
            return "WEAK_PUT", "LOW"
        # Neutral
        else:
            return "WAIT", "LOW"

    def _generate_targets(self, signal: str, confidence_score: float) -> Dict[str, float]:
        """Generate price targets for different timeframes."""
        if not self.market_data:
            return {"target_15m": 0, "target_30m": 0, "target_1h": 0, "target_eod": 0}
            
        current_price = self.market_data.get("price", 0)
        if current_price == 0:
            return {"target_15m": 0, "target_30m": 0, "target_1h": 0, "target_eod": 0}
            
        # Calculate expected move based on volatility and confidence
        # Use ATR or volatility-based calculation
        volatility = self.market_data.get("volatility", 0.01)  # Default 1%
        if volatility == 0:
            # Try to get from FVM or historical data
            fvm_data = self._get_fvm_details()
            if fvm_data.get("fvm_score", 0) > 50:
                volatility = 0.015  # 1.5% for moderate volatility
            else:
                volatility = 0.008  # 0.8% for low volatility
                
        # Adjust volatility based on confidence
        vol_multiplier = abs(confidence_score) / 100  # 0-1 range
        adjusted_vol = volatility * (0.5 + vol_multiplier * 0.5)  # 0.5-1.0x base volatility
        
        # Timeframe multipliers (intraday scaling)
        timeframe_multipliers = {
            "target_15m": 0.25,   # 1/4 of daily volatility
            "target_30m": 0.35,   # ~1/3 of daily volatility  
            "target_1h": 0.5,     # 1/2 of daily volatility
            "target_eod": 1.0     # Full daily volatility
        }
        
        targets = {}
        for target, multiplier in timeframe_multipliers.items():
            expected_move = current_price * adjusted_vol * multiplier
            
            # Apply direction
            if "CALL" in signal or "BUY" in signal:
                target_price = current_price + expected_move
            elif "PUT" in signal or "SELL" in signal:
                target_price = current_price - expected_move
            else:
                target_price = current_price  # No change for WAIT
                
            targets[target] = round(target_price, 2)
            
        return targets

    def _calculate_support_resistance(self) -> Dict[str, float]:
        """Calculate support and resistance levels."""
        if not self.market_data:
            return {"support": 0, "resistance": 0}
            
        current_price = self.market_data.get("price", 0)
        if current_price == 0:
            return {"support": 0, "resistance": 0}
            
        # Use technical levels from market data
        support_levels = self.market_data.get("support_levels", [])
        resistance_levels = self.market_data.get("resistance_levels", [])
        
        # Find nearest support below current price
        support = 0
        if support_levels:
            valid_support = [s for s in support_levels if s < current_price]
            if valid_support:
                support = max(valid_support)
                
        # Find nearest resistance above current price  
        resistance = 0
        if resistance_levels:
            valid_resistance = [r for r in resistance_levels if r > current_price]
            if valid_resistance:
                resistance = min(valid_resistance)
                
        # Fallback to percentage-based levels if no technical levels
        if support == 0:
            support = round(current_price * 0.99, 2)  # 1% below
        if resistance == 0:
            resistance = round(current_price * 1.01, 2)  # 1% above
            
        return {"support": support, "resistance": resistance}

    def _assess_risk_level(self, confidence_score: float, signals: Dict[str, Any]) -> str:
        """Assess risk level based on signal strength and confluence."""
        # Base risk on confidence score
        if confidence_score >= 80:
            base_risk = "Low"
        elif confidence_score >= 60:
            base_risk = "Medium"
        else:
            base_risk = "High"
            
        # Adjust for signal conflicts
        conflict_penalty = 0
        
        # Check for conflicting signals between components
        scores = [signals[key]["score"] for key in signals]
        if len(scores) > 1:
            score_variance = sum((s - sum(scores)/len(scores))**2 for s in scores) / len(scores)
            if score_variance > 600:  # High variance indicates conflict
                conflict_penalty = 1
                
        # Adjust for volatility (from FVM)
        fvm_score = signals["fvm"]["score"]
        if fvm_score > 80:  # High volatility increases risk
            conflict_penalty += 1
            
        # Final risk assessment
        risk_level = base_risk
        if conflict_penalty >= 2:
            if risk_level == "Low":
                risk_level = "Medium"
            elif risk_level == "Medium":
                risk_level = "High"
            elif risk_level == "High":
                risk_level = "Very High"
                
        return risk_level

    def _store_prediction(self, prediction: Dict[str, Any]):
        """Store prediction for learning and performance tracking."""
        self.prediction_history.append({
            "timestamp": datetime.now().isoformat(),
            "prediction": prediction,
            "market_data_snapshot": self.market_data.copy() if self.market_data else {}
        })
        
        # Keep only last 100 predictions
        if len(self.prediction_history) > 100:
            self.prediction_history = self.prediction_history[-100:]

    def get_prediction(self) -> Dict[str, Any]:
        """
        Returns the latest prediction, including signal, confidence, score, and reasons.
        """
        return self.generate_signal()
