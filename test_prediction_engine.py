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
    assert "reasoning" in prediction
    assert 0 <= prediction["confidence"] <= 100
    
    print(f"\n✓ Prediction generated successfully")
    print(f"✓ Direction: {prediction['direction']}")
    print(f"✓ Confidence: {prediction['confidence']}%")
    print(f"✓ Number of reasons: {len(prediction['reasoning'])}")
    
    return prediction

if __name__ == "__main__":
    test_prediction_engine()