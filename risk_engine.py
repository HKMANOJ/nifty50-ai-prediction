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
        """
        entry = current_price
        target1 = 0.0
        target2 = 0.0
        stoploss = 0.0
        risk_reward = 0.0

        if signal == "STRONG_CALL" or signal == "CALL":
            # For bullish signals
            stoploss = max(current_price - (atr * 1.5), vwap, min(support_levels) if support_levels else 0)
            target1 = current_price + (atr * 2.0)
            target2 = current_price + (atr * 4.0)

            # Adjust targets/stoploss based on resistance/support levels
            if resistance_levels:
                nearest_resistance = min([r for r in resistance_levels if r > current_price], default=target1)
                target1 = min(target1, nearest_resistance)
            if support_levels:
                nearest_support = max([s for s in support_levels if s < current_price], default=stoploss)
                stoploss = max(stoploss, nearest_support)

        elif signal == "STRONG_PUT" or signal == "PUT":
            # For bearish signals
            stoploss = min(current_price + (atr * 1.5), vwap, max(resistance_levels) if resistance_levels else float('inf'))
            target1 = current_price - (atr * 2.0)
            target2 = current_price - (atr * 4.0)

            # Adjust targets/stoploss based on resistance/support levels
            if support_levels:
                nearest_support = max([s for s in support_levels if s < current_price], default=target1)
                target1 = max(target1, nearest_support)
            if resistance_levels:
                nearest_resistance = min([r for r in resistance_levels if r > current_price], default=stoploss)
                stoploss = min(stoploss, nearest_resistance)

        # Ensure risk-reward ratio is at least 1:2 for Target1
        if signal in ["STRONG_CALL", "CALL"] and entry > stoploss:
            risk = entry - stoploss
            if risk > 0 and (target1 - entry) / risk < 2.0:
                target1 = entry + (risk * 2.0)
        elif signal in ["STRONG_PUT", "PUT"] and stoploss > entry:
            risk = stoploss - entry
            if risk > 0 and (entry - target1) / risk < 2.0:
                target1 = entry - (risk * 2.0)

        return {
            "entry": round(entry, 2),
            "target1": round(target1, 2),
            "target2": round(target2, 2),
            "stoploss": round(stoploss, 2),
            "risk_reward": round(risk_reward, 2) # This will be calculated later based on actual trade outcome
        }

