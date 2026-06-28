from typing import List, Dict, Any

def is_hammer(candle: Dict[str, Any]) -> bool:
    """
    Detects a Hammer candlestick pattern.
    A Hammer is a bullish reversal pattern that forms after a decline.
    It has a small body, a long lower wick, and little or no upper wick.
    """
    open_price = candle["open"]
    close_price = candle["close"]
    high_price = candle["high"]
    low_price = candle["low"]

    body = abs(close_price - open_price)
    lower_wick = min(open_price, close_price) - low_price
    upper_wick = high_price - max(open_price, close_price)

    # Small body
    if body > (high_price - low_price) * 0.3:  # Body is less than 30% of the total range
        return False

    # Long lower wick (at least twice the body size)
    if lower_wick < body * 2:
        return False

    # Little or no upper wick
    if upper_wick > body * 0.5:  # Upper wick is less than 50% of the body
        return False

    return True

def is_inverted_hammer(candle: Dict[str, Any]) -> bool:
    """
    Detects an Inverted Hammer candlestick pattern.
    An Inverted Hammer is a bullish reversal pattern that forms after a decline.
    It has a small body, a long upper wick, and little or no lower wick.
    """
    open_price = candle["open"]
    close_price = candle["close"]
    high_price = candle["high"]
    low_price = candle["low"]

    body = abs(close_price - open_price)
    lower_wick = min(open_price, close_price) - low_price
    upper_wick = high_price - max(open_price, close_price)

    # Small body
    if body > (high_price - low_price) * 0.3:
        return False

    # Long upper wick (at least twice the body size)
    if upper_wick < body * 2:
        return False

    # Little or no lower wick
    if lower_wick > body * 0.5:
        return False

    return True

def is_bullish_engulfing(current_candle: Dict[str, Any], previous_candle: Dict[str, Any]) -> bool:
    """
    Detects a Bullish Engulfing candlestick pattern.
    A Bullish Engulfing pattern occurs when a large white (or green) candlestick
    body completely engulfs the body of the previous small black (or red) candlestick.
    """
    # Previous candle must be bearish (close < open)
    if previous_candle["close"] >= previous_candle["open"]:
        return False

    # Current candle must be bullish (close > open)
    if current_candle["close"] <= current_candle["open"]:
        return False

    # Current candle's body must engulf the previous candle's body
    if (current_candle["close"] > previous_candle["open"] and
            current_candle["open"] < previous_candle["close"]):
        return True
    return False

def is_bearish_engulfing(current_candle: Dict[str, Any], previous_candle: Dict[str, Any]) -> bool:
    """
    Detects a Bearish Engulfing candlestick pattern.
    A Bearish Engulfing pattern occurs when a large black (or red) candlestick
    body completely engulfs the body of the previous small white (or green) candlestick.
    """
    # Previous candle must be bullish (close > open)
    if previous_candle["close"] <= previous_candle["open"]:
        return False

    # Current candle must be bearish (close < open)
    if current_candle["close"] >= current_candle["open"]:
        return False

    # Current candle's body must engulf the previous candle's body
    if (current_candle["open"] > previous_candle["close"] and
            current_candle["close"] < previous_candle["open"]):
        return True
    return False


def is_doji(candle: Dict[str, Any]) -> bool:
    """
    Detects a Doji candlestick pattern.
    A Doji forms when the open and close are virtually equal.
    """
    open_price = candle["open"]
    close_price = candle["close"]
    high_price = candle["high"]
    low_price = candle["low"]
    
    body = abs(close_price - open_price)
    total_range = high_price - low_price
    
    # Doji has very small body relative to total range
    if total_range == 0:
        return False
        
    return body / total_range < 0.1  # Body less than 10% of range


def is_spinning_top(candle: Dict[str, Any]) -> bool:
    """
    Detects a Spinning Top candlestick pattern.
    Has small body with long upper and lower wicks.
    """
    open_price = candle["open"]
    close_price = candle["close"]
    high_price = candle["high"]
    low_price = candle["low"]
    
    body = abs(close_price - open_price)
    lower_wick = min(open_price, close_price) - low_price
    upper_wick = high_price - max(open_price, close_price)
    total_range = high_price - low_price
    
    if total_range == 0:
        return False
        
    # Small body (less than 30% of range)
    if body > total_range * 0.3:
        return False
        
    # Long upper and lower wicks (each at least as long as body)
    return lower_wick > body * 0.5 and upper_wick > body * 0.5


def is_tweezer_top(current_candle: Dict[str, Any], previous_candle: Dict[str, Any]) -> bool:
    """
    Detects a Tweezer Top pattern.
    Two or more candles with matching highs.
    """
    if not current_candle or not previous_candle:
        return False
        
    current_high = current_candle["high"]
    prev_high = previous_candle["high"]
    
    # Highs are within 0.1% of each other
    if prev_high == 0:
        return False
        
    return abs(current_high - prev_high) / prev_high < 0.001


def is_tweezer_bottom(current_candle: Dict[str, Any], previous_candle: Dict[str, Any]) -> bool:
    """
    Detects a Tweezer Bottom pattern.
    Two or more candles with matching lows.
    """
    if not current_candle or not previous_candle:
        return False
        
    current_low = current_candle["low"]
    prev_low = previous_candle["low"]
    
    # Lows are within 0.1% of each other
    if prev_low == 0:
        return False
        
    return abs(current_low - prev_low) / prev_low < 0.001


def is_bullish_harami(current_candle: Dict[str, Any], previous_candle: Dict[str, Any]) -> bool:
    """
    Detects a Bullish Harami pattern.
    A small bullish candle contained within a large bearish candle.
    """
    # Previous candle must be bearish
    if previous_candle["close"] >= previous_candle["open"]:
        return False
        
    # Current candle must be bullish
    if current_candle["close"] <= current_candle["open"]:
        return False
        
    # Current body must be within previous body
    prev_body_high = max(previous_candle["open"], previous_candle["close"])
    prev_body_low = min(previous_candle["open"], previous_candle["close"])
    curr_body_high = max(current_candle["open"], current_candle["close"])
    curr_body_low = min(current_candle["open"], current_candle["close"])
    
    return curr_body_high <= prev_body_high and curr_body_low >= prev_body_low


def is_bearish_harami(current_candle: Dict[str, Any], previous_candle: Dict[str, Any]) -> bool:
    """
    Detects a Bearish Harami pattern.
    A small bearish candle contained within a large bullish candle.
    """
    # Previous candle must be bullish
    if previous_candle["close"] <= previous_candle["open"]:
        return False
        
    # Current candle must be bearish
    if current_candle["close"] >= current_candle["open"]:
        return False
        
    # Current body must be within previous body
    prev_body_high = max(previous_candle["open"], previous_candle["close"])
    prev_body_low = min(previous_candle["open"], previous_candle["close"])
    curr_body_high = max(current_candle["open"], current_candle["close"])
    curr_body_low = min(current_candle["open"], current_candle["close"])
    
    return curr_body_high <= prev_body_high and curr_body_low >= prev_body_low

def detect_patterns(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Analyzes a list of candles and detects various candlestick patterns.
    Returns the candles with detected patterns added to each candle's dictionary.
    """
    if not candles or len(candles) < 2:
        return candles

    for i in range(len(candles)):
        candle = candles[i]
        candle["patterns"] = []

        # Single candle patterns
        if is_hammer(candle):
            candle["patterns"].append("Hammer")
        if is_inverted_hammer(candle):
            candle["patterns"].append("Inverted Hammer")
        if is_doji(candle):
            candle["patterns"].append("Doji")
        if is_spinning_top(candle):
            candle["patterns"].append("Spinning Top")

        # Two-candle patterns
        if i > 0:
            previous_candle = candles[i-1]
            if is_bullish_engulfing(candle, previous_candle):
                candle["patterns"].append("Bullish Engulfing")
            if is_bearish_engulfing(candle, previous_candle):
                candle["patterns"].append("Bearish Engulfing")
            if is_tweezer_top(candle, previous_candle):
                candle["patterns"].append("Tweezer Top")
            if is_tweezer_bottom(candle, previous_candle):
                candle["patterns"].append("Tweezer Bottom")
            if is_bullish_harami(candle, previous_candle):
                candle["patterns"].append("Bullish Harami")
            if is_bearish_harami(candle, previous_candle):
                candle["patterns"].append("Bearish Harami")
    return candles

