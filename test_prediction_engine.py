#!/usr/bin/env python3
"""
Test script for the enhanced prediction engine
"""

from prediction_engine import PredictionEngine
from nifty_pattern_detector import detect_patterns
from market_structure_engine import MarketStructureEngine
import json

def test_prediction_engine():
    """Test the prediction engine with sample data."""
    print("Testing Enhanced Prediction Engine...")
    
    # Initialize engine
    engine = PredictionEngine()
    
    # Sample market data
    market_data = {
        "price": 23950.50,
        "vwap": 23930.25,
        "volume": 1850000,
        "avg_volume_20d": 1600000,
        "volatility": 0.012,
        "gift_nifty_change": 0.45,
        "support_levels": [23880, 23820, 23750],
        "resistance_levels": [24000, 24080, 24150]
    }
    
    # Sample FII/DII data
    fii_dii_data = {
        "net_fii": 850.5  # Positive FII flow
    }
    
    # Sample indicators
    indicators = {
        "RSI": 58,
        "MACD": 0.25
    }
    
    # Sample patterns (from pattern detector)
    sample_candles = [
        {"open": 23900, "high": 23980, "low": 23890, "close": 23950},
        {"open": 23950, "high": 24020, "low": 23940, "close": 24000},
        {"open": 24000, "high": 24050, "low": 23980, "close": 24020}
    ]
    patterns_data = {"Hammer": True, "Bullish Engulfing": True}
    
    # Sample market structure
    market_structure_engine = MarketStructureEngine()
    market_structure = market_structure_engine.analyze_market_structure(sample_candles)
    
    # Sample option chain data
    option_chain_data = {
        "calls": [
            {"strike": 23900, "oi": 15000, "change_oi": 2000, "volume": 500},
            {"strike": 24000, "oi": 8000, "change_oi": -500, "volume": 300},
            {"strike": 24100, "oi": 3000, "change_oi": -1000, "volume": 100}
        ],
        "puts": [
            {"strike": 23800, "oi": 2000, "change_oi": 500, "volume": 150},
            {"strike": 23900, "oi": 12000, "change_oi": 3000, "volume": 800},
            {"strike": 24000, "oi": 18000, "change_oi": 4000, "volume": 1200}
        ]
    }
    
    # Sample historical data for FVM
    historical_data = []
    base_price = 23900
    for i in range(30):
        import random
        daily_change = random.uniform(-0.02, 0.02)
        close_price = base_price * (1 + daily_change)
        historical_data.append({
            "open": base_price,
            "high": base_price * (1 + abs(daily_change) * 0.5),
            "low": base_price * (1 - abs(daily_change) * 0.5),
            "close": close_price,
            "volume": random.randint(1000000, 3000000)
        })
        base_price = close_price
    
    # Sample VIX data
    vix_data = {
        "close": 18.5,
        "prev_close": 17.8
    }
    
    # Update engine with data
    engine.update_data("market_data", market_data)
    engine.update_data("indicators", indicators)
    engine.update_data("patterns", patterns_data)
    engine.update_data("market_structure", market_structure)
    engine.update_options_data(option_chain_data)
    engine.update_liquidity_data(market_data, option_chain_data)
    engine.update_fvm_data(market_data, historical_data, vix_data)
    engine.update_data("fii_dii_data", fii_dii_data)
    
    # Generate prediction
    prediction = engine.generate_signal()
    
    # Print results
    print("\n" + "="*50)
    print("PREDICTION ENGINE TEST RESULTS")
    print("="*50)
    print(json.dumps(prediction, indent=2))
    
    # Validate key components
    assert "direction" in prediction
    assert "confidence" in prediction
    assert "confidence_score" in prediction
    assert "reasoning" in prediction
    assert isinstance(prediction["confidence"], int)
    assert 0 <= prediction["confidence"] <= 100
    assert -100 <= prediction["confidence_score"] <= 100
    
    print(f"\n✓ Prediction generated successfully")
    print(f"✓ Direction: {prediction['direction']}")
    print(f"✓ Confidence: {prediction['confidence']} ({prediction['confidence_score']})")
    print(f"✓ Number of reasons: {len(prediction['reasoning'])}")
    
    
    # 2. Test Fibonacci and Consolidation Box detection integration
    print("\nTesting Fibonacci and Consolidation Box Integration...")
    mock_candles = []
    
    # Day 1: Consolidation between 24310 and 24360
    for idx in range(30):
        # alternate high and low closes
        close_price = 24310 + (idx % 2) * 50
        mock_candles.append({
            "market_time": f"2026-07-06T09:{15 + idx * 5}:00",
            "open": close_price - 10,
            "high": close_price + 20,
            "low": close_price - 20,
            "close": close_price,
            "volume": 100000,
            "market_date": "2026-07-06"
        })
        
    # Day 2: Bullish impulse breakout to 24525 and consolidation retest
    for idx in range(20):
        # climb from 24360 to 24525
        close_price = 24360 + idx * 8
        mock_candles.append({
            "market_time": f"2026-07-07T09:{15 + idx * 5}:00",
            "open": close_price - 5,
            "high": close_price + 10,
            "low": close_price - 10,
            "close": close_price,
            "volume": 250000,
            "market_date": "2026-07-07"
        })
        
    # Current candle is a pullback near 24495 retesting the Fibonacci 23.6% level (Height = 24525 - 24310 = 215, 23.6% = 24474)
    # Price is currently 24490, which is near the Fib 23.6% level
    mock_candles.append({
        "market_time": "2026-07-07T11:00:00",
        "open": 24500,
        "high": 24505,
        "low": 24485,
        "close": 24490,
        "volume": 300000,
        "market_date": "2026-07-07"
    })
    
    # Update engine with candles
    engine.update_data("candles", mock_candles)
    
    # Update market_data with the current price of Day 2
    new_market_data = {
        "price": 24490.0,
        "vwap": 24480.0,
        "volume": 300000,
        "avg_volume_20d": 200000,
        "volatility": 0.012,
        "support_levels": [24300, 24260],
        "resistance_levels": [24550, 24600]
    }
    engine.update_data("market_data", new_market_data)
    # Force a pattern detection setup to trigger a signal
    engine.update_data("patterns", {"Trendline Break": {"name": "Trendline Break", "direction": "bullish", "levels": {"upper_trendline": 24480, "breakout": True}}})
    
    # Generate prediction with Fibonacci alignment active
    fib_prediction = engine.generate_signal()
    print("Fibonacci Aligned Prediction:")
    print(json.dumps(fib_prediction, indent=2))
    
    # Assertions
    assert fib_prediction["support"] > 0
    assert fib_prediction["resistance"] > 0
    assert fib_prediction["target1"] > 24490
    assert fib_prediction["stoploss"] < 24490
    
    print("\n✓ Fibonacci & Consolidation Box integration test passed successfully!")
    return prediction

if __name__ == "__main__":
    test_prediction_engine()