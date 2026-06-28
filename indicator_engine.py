from typing import List, Dict, Any

class IndicatorEngine:
    """
    Calculates various technical indicators for NIFTY 50 candles.
    """

    def __init__(self):
        pass

    def calculate_ema(self, closes: List[float], period: int) -> List[float]:
        """
        Calculates Exponential Moving Average (EMA).
        """
        if not closes:
            return []
        ema_values = []
        multiplier = 2 / (period + 1)
        ema = closes[0]
        ema_values.append(ema)
        for i in range(1, len(closes)):
            ema = ((closes[i] - ema) * multiplier) + ema
            ema_values.append(ema)
        return ema_values

    def calculate_rsi(self, closes: List[float], period: int = 14) -> List[float]:
        """
        Calculates Relative Strength Index (RSI).
        """
        if len(closes) < period + 1:
            return []

        rsi_values = [0.0] * period
        gains = []
        losses = []

        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            gains.append(max(0, change))
            losses.append(max(0, -change))

        avg_gain = sum(gains[0:period]) / period
        avg_loss = sum(losses[0:period]) / period

        if avg_loss == 0:
            rs = 200 # Arbitrarily high to signify strong bullish momentum
        else:
            rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi_values.append(rsi)

        for i in range(period, len(gains)):
            avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
            avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

            if avg_loss == 0:
                rs = 200
            else:
                rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            rsi_values.append(rsi)

        return rsi_values

    def calculate_macd(self, closes: List[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Dict[str, List[float]]:
        """
        Calculates Moving Average Convergence Divergence (MACD).
        """
        if len(closes) < slow_period + signal_period:
            return {"macd": [], "signal": [], "histogram": []}

        ema_fast = self.calculate_ema(closes, fast_period)
        ema_slow = self.calculate_ema(closes, slow_period)

        # Ensure EMAs are of appropriate length for MACD calculation
        min_len = min(len(ema_fast), len(ema_slow))
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(min_len - 1, len(ema_fast))]

        signal_line = self.calculate_ema(macd_line, signal_period)

        # Adjust macd_line to match signal_line length for histogram calculation
        macd_line_adjusted = macd_line[len(macd_line) - len(signal_line):]
        histogram = [macd_line_adjusted[i] - signal_line[i] for i in range(len(signal_line))]

        return {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": histogram
        }

    def calculate_adx(self, highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
        """
        Calculates Average Directional Index (ADX).
        """
        if len(highs) < period * 2 or len(lows) < period * 2 or len(closes) < period * 2:
            return []

        adx_values = [0.0] * (period * 2 - 1)

        # Calculate True Range (TR)
        tr_values = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i-1]),
                     abs(lows[i] - closes[i-1]))
            tr_values.append(tr)

        # Calculate Directional Movement (DM)
        plus_dm = []
        minus_dm = []
        for i in range(1, len(closes)):
            up_move = highs[i] - highs[i-1]
            down_move = lows[i-1] - lows[i]

            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        # Calculate Smoothed True Range and Directional Movement
        atr_values = self.calculate_ema(tr_values, period)
        plus_di_values = self.calculate_ema(plus_dm, period)
        minus_di_values = self.calculate_ema(minus_dm, period)

        # Calculate DI
        di_plus = []
        di_minus = []
        for i in range(len(atr_values)):
            if atr_values[i] == 0:
                di_plus.append(0)
                di_minus.append(0)
            else:
                di_plus.append((plus_di_values[i] / atr_values[i]) * 100)
                di_minus.append((minus_di_values[i] / atr_values[i]) * 100)

        # Calculate DX
        dx_values = []
        for i in range(len(di_plus)):
            if (di_plus[i] + di_minus[i]) == 0:
                dx_values.append(0)
            else:
                dx_values.append((abs(di_plus[i] - di_minus[i]) / (di_plus[i] + di_minus[i])) * 100)

        # Calculate ADX
        adx_values = self.calculate_ema(dx_values, period)

        return adx_values

    def calculate_vwap(self, typical_prices: List[float], volumes: List[float]) -> List[float]:
        """
        Calculates Volume Weighted Average Price (VWAP).
        """
        if not typical_prices or not volumes or len(typical_prices) != len(volumes):
            return []

        vwap_values = []
        cumulative_tp_volume = 0.0
        cumulative_volume = 0.0

        for i in range(len(typical_prices)):
            cumulative_tp_volume += typical_prices[i] * volumes[i]
            cumulative_volume += volumes[i]
            if cumulative_volume > 0:
                vwap_values.append(cumulative_tp_volume / cumulative_volume)
            else:
                vwap_values.append(0.0) # Handle division by zero if no volume
        return vwap_values

    def calculate_supertrend(self, highs: List[float], lows: List[float], closes: List[float], period: int = 10, multiplier: float = 3.0) -> Dict[str, List[float]]:
        """
        Calculates Supertrend indicator.
        """
        if len(highs) < period or len(lows) < period or len(closes) < period:
            return {"supertrend": [], "direction": []}

        atr_values = []
        for i in range(len(closes)):
            if i < period:
                atr_values.append(0.0) # Placeholder, ATR needs previous data
            else:
                tr_sum = 0.0
                for j in range(i - period + 1, i + 1):
                    tr = max(highs[j] - lows[j],
                             abs(highs[j] - closes[j-1]),
                             abs(lows[j] - closes[j-1]))
                    tr_sum += tr
                atr_values.append(tr_sum / period)

        basic_upper_band = []
        basic_lower_band = []
        for i in range(len(closes)):
            if i < period:
                basic_upper_band.append(0.0)
                basic_lower_band.append(0.0)
            else:
                basic_upper_band.append(((highs[i] + lows[i]) / 2) + (multiplier * atr_values[i]))
                basic_lower_band.append(((highs[i] + lows[i]) / 2) - (multiplier * atr_values[i]))

        final_upper_band = [0.0] * len(closes)
        final_lower_band = [0.0] * len(closes)
        supertrend = [0.0] * len(closes)
        direction = [0] * len(closes) # 1 for uptrend, -1 for downtrend

        for i in range(period, len(closes)):
            if i == period:
                final_upper_band[i] = basic_upper_band[i]
                final_lower_band[i] = basic_lower_band[i]
            else:
                if (basic_upper_band[i] < final_upper_band[i-1] or
                        closes[i-1] > final_upper_band[i-1]):
                    final_upper_band[i] = basic_upper_band[i]
                else:
                    final_upper_band[i] = final_upper_band[i-1]

                if (basic_lower_band[i] > final_lower_band[i-1] or
                        closes[i-1] < final_lower_band[i-1]):
                    final_lower_band[i] = basic_lower_band[i]
                else:
                    final_lower_band[i] = final_lower_band[i-1]

            if supertrend[i-1] == final_upper_band[i-1] and closes[i] <= final_upper_band[i]:
                supertrend[i] = final_upper_band[i]
                direction[i] = -1
            elif supertrend[i-1] == final_upper_band[i-1] and closes[i] > final_upper_band[i]:
                supertrend[i] = final_lower_band[i]
                direction[i] = 1
            elif supertrend[i-1] == final_lower_band[i-1] and closes[i] >= final_lower_band[i]:
                supertrend[i] = final_lower_band[i]
                direction[i] = 1
            elif supertrend[i-1] == final_lower_band[i-1] and closes[i] < final_lower_band[i]:
                supertrend[i] = final_upper_band[i]
                direction[i] = -1
            else:
                # Initial condition or no clear trend yet
                if closes[i] > final_upper_band[i]:
                    supertrend[i] = final_lower_band[i]
                    direction[i] = 1
                elif closes[i] < final_lower_band[i]:
                    supertrend[i] = final_upper_band[i]
                    direction[i] = -1
                else:
                    supertrend[i] = supertrend[i-1] # Maintain previous state
                    direction[i] = direction[i-1]

        return {"supertrend": supertrend, "direction": direction}

    def calculate_all_indicators(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Calculates all specified technical indicators for a list of candles.
        """
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        opens = [c["open"] for c in candles]
        volumes = [c["volume"] for c in candles]

        # Calculate Typical Price for VWAP
        typical_prices = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles]

        ema9 = self.calculate_ema(closes, 9)
        ema21 = self.calculate_ema(closes, 21)
        ema50 = self.calculate_ema(closes, 50)
        vwap = self.calculate_vwap(typical_prices, volumes)
        rsi14 = self.calculate_rsi(closes, 14)
        macd_data = self.calculate_macd(closes)
        adx = self.calculate_adx(highs, lows, closes)
        supertrend_data = self.calculate_supertrend(highs, lows, closes)

        return {
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "vwap": vwap,
            "rsi14": rsi14,
            "macd": macd_data["macd"],
            "macd_signal": macd_data["signal"],
            "macd_histogram": macd_data["histogram"],
            "adx": adx,
            "supertrend": supertrend_data["supertrend"],
            "supertrend_direction": supertrend_data["direction"],
        }

