from typing import Dict, Any, List

class RiskEngine:
    """
    Calculates entry, target, and stop-loss levels based on various factors.
    """

    def __init__(self):
        pass

    def calculate_atr(self, highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """
        Calculates the Average True Range (ATR).
        """
        if len(highs) < period or len(lows) < period or len(closes) < period:
            return 0.0

        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i-1]),
                     abs(lows[i] - closes[i-1]))
            trs.append(tr)

        # Simple Moving Average of True Range
        if len(trs) < period:
            return 0.0
        return sum(trs[-period:]) / period

    def calculate_risk_levels(self,
                              current_price: float,
                              signal: str,
                              atr: float,
                              vwap: float,
                              support_levels: List[float],
                              resistance_levels: List[float]
                             ) -> Dict[str, Any]:
        """
        Calculates Entry, Target1, Target2, and StopLoss based on signal and market data.

        CALL signals: stop below entry (support), targets above entry (resistance)
        PUT signals:  stop above entry (resistance), targets below entry (support)
        """
        entry = current_price
        target1 = 0.0
        target2 = 0.0
        stoploss = 0.0
        risk_reward = 0.0

        if signal in ("STRONG_CALL", "CALL", "WEAK_CALL"):
            # ----- CALL: stop below, targets above -----
            # Base stop = entry minus 1.5 ATR
            base_stop = current_price - (atr * 1.5)

            # VWAP acts as support only when it is BELOW entry
            vwap_support = vwap if 0 < vwap < current_price else base_stop

            # Nearest support below entry (if any)
            supports_below = [s for s in support_levels if s < current_price] if support_levels else []
            nearest_support = max(supports_below) if supports_below else base_stop

            # Stop = highest of (base_stop, vwap_support, nearest_support)
            # so the stop is as close to entry as the data allows while still below.
            stoploss = max(base_stop, vwap_support, nearest_support)

            # Targets above entry
            target1 = current_price + (atr * 2.0)
            target2 = current_price + (atr * 4.0)

            # Cap target1 at nearest resistance above entry
            resistances_above = [r for r in resistance_levels if r > current_price] if resistance_levels else []
            if resistances_above:
                nearest_resistance = min(resistances_above)
                target1 = min(target1, nearest_resistance)

            # Safety: stop must never be at or above entry for a CALL
            if stoploss >= entry:
                stoploss = entry - max(atr * 1.0, 10.0)

        elif signal in ("STRONG_PUT", "PUT", "WEAK_PUT"):
            # ----- PUT: stop above, targets below -----
            # Base stop = entry plus 1.5 ATR
            base_stop = current_price + (atr * 1.5)

            # VWAP acts as resistance only when it is ABOVE entry
            vwap_resistance = vwap if vwap > current_price else base_stop

            # Nearest resistance above entry (if any)
            resistances_above = [r for r in resistance_levels if r > current_price] if resistance_levels else []
            nearest_resistance = min(resistances_above) if resistances_above else base_stop

            # Stop = lowest of (base_stop, vwap_resistance, nearest_resistance)
            # so the stop is as close to entry as the data allows while still above.
            stoploss = min(base_stop, vwap_resistance, nearest_resistance)

            # Targets below entry
            target1 = current_price - (atr * 2.0)
            target2 = current_price - (atr * 4.0)

            # Cap target1 at nearest support below entry
            supports_below = [s for s in support_levels if s < current_price] if support_levels else []
            if supports_below:
                nearest_support = max(supports_below)
                target1 = max(target1, nearest_support)

            # Safety: stop must never be at or below entry for a PUT
            if stoploss <= entry:
                stoploss = entry + max(atr * 1.0, 10.0)

        # --- Enforce minimum 1:2 risk-reward for Target1 ---
        if signal in ("STRONG_CALL", "CALL", "WEAK_CALL") and entry > stoploss:
            risk = entry - stoploss
            reward = target1 - entry
            if risk > 0 and reward / risk < 2.0:
                target1 = entry + (risk * 2.0)
        elif signal in ("STRONG_PUT", "PUT", "WEAK_PUT") and stoploss > entry:
            risk = stoploss - entry
            reward = entry - target1
            if risk > 0 and reward / risk < 2.0:
                target1 = entry - (risk * 2.0)

        # --- Calculate risk-reward ratio ---
        if signal in ("STRONG_CALL", "CALL", "WEAK_CALL") and entry > stoploss:
            risk = entry - stoploss
            if risk > 0:
                risk_reward = round((target1 - entry) / risk, 2)
        elif signal in ("STRONG_PUT", "PUT", "WEAK_PUT") and stoploss > entry:
            risk = stoploss - entry
            if risk > 0:
                risk_reward = round((entry - target1) / risk, 2)

        return {
            "entry": round(entry, 2),
            "target1": round(target1, 2),
            "target2": round(target2, 2),
            "stoploss": round(stoploss, 2),
            "risk_reward": risk_reward
        }
