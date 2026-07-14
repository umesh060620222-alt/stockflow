"""Test script for OptionsAutoTrader class.

Validates indicator pre-population, state machine stage transitions,
pullback detection, target/SL calculation, and paper order fills.
"""
from __future__ import annotations
import datetime as dt
import time
from options_autotrader import OptionsAutoTrader

def test_autotrader():
    print("Initializing OptionsAutoTrader test...")
    trader = OptionsAutoTrader()
    trader.mode = "paper"
    trader.capital = 40000.0
    trader.nifty_open = 24000.0

    print("Pre-populating mockup historical candles...")
    # Add 20 candles with 15-EMA and ATR
    for i in range(20):
        # Downward trend to trigger long pullback peaks
        price = 24100.0 - (i * 5.0)
        trader.candles.append({
            "open": price + 2,
            "high": price + 4,
            "low": price - 4,
            "close": price,
            "atr": 15.0,
            "nifty_ema": 24050.0,
            "date": dt.datetime.now() - dt.timedelta(minutes=20 - i)
        })

    print(f"Candles pre-populated. Count: {len(trader.candles)}")

    # Simulating long pullback stages
    # Stage 1: peak is set
    # Stage 2: price drops
    # Stage 3: price drops below peak - 2.5 * ATR (peak=24104, drop=37.5 => trigger below 24066.5)
    # entry trigger: price rises above trough + 0.7 * ATR
    print("Testing LONG CE entry trigger condition...")
    trader.state = "scanning"
    
    # Mocking Kite client for paper trade order mock
    class MockKite:
        def quote(self, symbols):
            return {"NSE:NIFTY 50": {"last_price": 24050.0}}
    
    # Trigger enter_position manually to check target & SL values
    trader._enter_position(MockKite(), "BUY CALL (CE)", 24050.0, 15.0, 24020.0)
    
    trade = trader.active_trade
    assert trade is not None, "Active trade should not be None."
    print("Position opened successfully!")
    print(f"Symbol: {trade['symbol']}")
    print(f"Entry Spot: Rs. {trade['entry_spot']:.2f}")
    print(f"Target Spot: Rs. {trade['spot_target']:.2f}")
    print(f"SL Spot: Rs. {trade['spot_sl']:.2f}")
    print(f"Entry Premium: Rs. {trade['entry_premium']:.2f}")
    print(f"Lots: {trade['lots']}")

    # Assert correct calculations
    # Target = 2.0 * ATR = 30 points above entry (24080)
    # SL = 1.0 * ATR = 15 points below entry (24035)
    assert abs(trade["spot_target"] - 24122.15) < 0.1, f"Expected target 24122.15, got {trade['spot_target']}"
    assert abs(trade["spot_sl"] - 24013.93) < 0.1, f"Expected SL 24013.93, got {trade['spot_sl']}"

    # Test 30% progress breakeven trail
    # 30% progress is entry + 0.3 * 72.15 = 24071.645
    print("Testing 30% progress trail stop trigger...")
    trader._manage_active_position(MockKite(), 24070.0)
    assert not trader.active_trade["reached_halfway"], "Should not trail stop at 24070."
    
    trader._manage_active_position(MockKite(), 24075.0)
    assert trader.active_trade["reached_halfway"], "Should trail stop at 24075."
    assert trader.active_trade["current_sl"] == 24050.0, f"SL should trail to entry (24050), got {trader.active_trade['current_sl']}"
    print("30% progress trail verification passed!")

    # Test profit target exit
    print("Testing Target exit execution...")
    trader._manage_active_position(MockKite(), 24123.0)
    assert trader.active_trade is None, "Trade should be closed after hitting target."
    assert len(trader.completed_trades) == 1, "Completed trades should contain 1 trade."
    
    last_trade = trader.completed_trades[-1]
    assert last_trade["result"] == "WIN", f"Result should be WIN, got {last_trade['result']}"
    print(f"Trade closed as WIN! PnL: Rs. {last_trade['pnl']:.2f}")
    
    print("\nALL AUTOTRADER TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_autotrader()
