import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def analyze_10am_rule():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    
    if yesterday.weekday() >= 5:
        yesterday = yesterday - dt.timedelta(days=2 if yesterday.weekday() == 6 else 1)
        
    print(f"Fetching yesterday's close ({yesterday}) and today's candles...")
    
    try:
        yest_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
        if yest_candles:
            yesterday_close = yest_candles[0]['close']
        else:
            y_start = dt.datetime.combine(yesterday, dt.time(9, 15))
            y_end = dt.datetime.combine(yesterday, dt.time(15, 30))
            y_candles = kc.historical_data(nifty_token, y_start, y_end, "minute")
            yesterday_close = y_candles[-1]['close'] if y_candles else None
    except Exception as e:
        print(f"Error fetching yesterday's close: {e}")
        return
        
    try:
        today_start = dt.datetime.combine(today, dt.time(9, 15))
        today_end = dt.datetime.combine(today, dt.time(15, 30))
        today_candles = kc.historical_data(nifty_token, today_start, today_end, "minute")
    except Exception as e:
        print(f"Error fetching today's candles: {e}")
        return
        
    if not today_candles or not yesterday_close:
        print("Failed to load historical data.")
        return
        
    df = pd.DataFrame(today_candles)
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    
    today_open = df.iloc[0]['open']
    
    # Determine Gap Direction
    gap = today_open - yesterday_close
    is_gap_up = gap > 0
    
    # We enter at exactly 10:00 AM today
    target_time_str = f"{today} 10:00:00"
    target_dt = pd.to_datetime(target_time_str).tz_localize(df.index.tz)
    
    if target_dt not in df.index:
        # find nearest active candle after 10:00 AM
        active_indices = df.index[df.index >= target_dt]
        if len(active_indices) > 0:
            target_dt = active_indices[0]
        else:
            print("Could not find 10:00 AM candle.")
            return
            
    entry_price = df.loc[target_dt, 'open']
    trade_side = "BUY CE (Gap Up)" if is_gap_up else "BUY PE (Gap Down)"
    
    # Slice today's data from 10:00 AM onwards
    df_trade = df.loc[target_dt:]
    eod_price = df_trade.iloc[-1]['close']
    
    # Calculate P&L
    if is_gap_up:
        # Buy CE: profit if price goes UP
        eod_pnl = eod_price - entry_price
        highest_price = df_trade['high'].max()
        lowest_price = df_trade['low'].min()
        max_profit = highest_price - entry_price
        max_drawdown = entry_price - lowest_price
    else:
        # Buy PE: profit if price goes DOWN (short)
        eod_pnl = entry_price - eod_price
        highest_price = df_trade['high'].max()
        lowest_price = df_trade['low'].min()
        max_profit = entry_price - lowest_price
        max_drawdown = highest_price - entry_price
        
    print("\n" + "="*80)
    print(f"📊 10:00 AM GAP RULE BACKTEST FOR TODAY ({today})")
    print("="*80)
    print(f"  ▸ Yesterday's Close:  ₹{yesterday_close:.2f}")
    print(f"  ▸ Today's Open:       ₹{today_open:.2f}")
    print(f"  ▸ Gap Size:           {gap:+.2f} points ({'GAP UP' if is_gap_up else 'GAP DOWN'})")
    print(f"  ▸ Rule Decision:      Since Gap Down, we {trade_side}")
    print("-"*80)
    print(f"🎯 Position Entry:      ₹{entry_price:.2f} at 10:00 AM")
    print(f"🎯 Position Exit:       ₹{eod_price:.2f} at 03:30 PM (EOD)")
    print(f"  ▸ EOD Trade P&L:      {eod_pnl:+.2f} Nifty Spot Points")
    print(f"  ▸ Max Profit Run:     {max_profit:+.2f} points")
    print(f"  ▸ Max Drawdown:       -{max_drawdown:.2f} points")
    print("-"*80)
    
    # Check if target of +20 points was hit during the day
    hit_target = max_profit >= 20.0
    if hit_target:
        # Find exact minute when it reached +20 points
        target_time = None
        for idx, row in df_trade.iterrows():
            if is_gap_up:
                current_profit = row['high'] - entry_price
            else:
                current_profit = entry_price - row['low']
                
            if current_profit >= 20.0:
                target_time = idx.strftime("%H:%M")
                break
        print(f"✅ TARGET HITS: Yes! Reached +20.0 points profit at {target_time} PM!")
    else:
        print("❌ TARGET HITS: No, trade never reached +20.0 points profit during the session.")
    print("="*80)

if __name__ == "__main__":
    analyze_10am_rule()
