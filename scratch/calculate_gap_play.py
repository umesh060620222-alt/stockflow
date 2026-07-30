import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def analyze_gap_play():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    
    # Adjust for weekend if yesterday was Sunday/Saturday
    if yesterday.weekday() >= 5:
        yesterday = yesterday - dt.timedelta(days=2 if yesterday.weekday() == 6 else 1)
        
    print(f"Fetching close price for yesterday ({yesterday}) and today's candles ({today})...")
    
    # Fetch yesterday's data to get the close price
    try:
        yest_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
        if yest_candles:
            yesterday_close = yest_candles[0]['close']
        else:
            # Fallback to minute candles if day query fails
            yest_start = dt.datetime.combine(yesterday, dt.time(9, 15))
            yest_end = dt.datetime.combine(yesterday, dt.time(15, 30))
            y_candles = kc.historical_data(nifty_token, yest_start, yest_end, "minute")
            yesterday_close = y_candles[-1]['close'] if y_candles else None
    except Exception as e:
        print(f"Error fetching yesterday's data: {e}")
        return
        
    # Fetch today's 1-minute candles
    try:
        today_start = dt.datetime.combine(today, dt.time(9, 15))
        today_end = dt.datetime.combine(today, dt.time(15, 30))
        today_candles = kc.historical_data(nifty_token, today_start, today_end, "minute")
    except Exception as e:
        print(f"Error fetching today's data: {e}")
        return
        
    if not today_candles or not yesterday_close:
        print("Failed to retrieve yesterday's close or today's candles.")
        return
        
    df = pd.DataFrame(today_candles)
    df['date'] = pd.to_datetime(df['date'])
    
    today_open = df.iloc[0]['open']
    today_close = df.iloc[-1]['close']
    
    gap = today_open - yesterday_close
    is_gap_up = gap > 0
    
    # Calculate stats if we entered at open
    entry_price = today_open
    eod_price = today_close
    net_change = eod_price - entry_price
    
    highest_price = df['high'].max()
    lowest_price = df['low'].min()
    
    max_favorable_run = highest_price - entry_price
    max_adverse_run = entry_price - lowest_price
    
    print("\n" + "="*80)
    print(f"📊 GAP PLAY ANALYSIS REPORT FOR TODAY ({today})")
    print("="*80)
    print(f"  ▸ Yesterday's Close:  ₹{yesterday_close:.2f}")
    print(f"  ▸ Today's Open:       ₹{today_open:.2f}")
    print(f"  ▸ Gap Size:           {gap:+.2f} points ({'GAP UP' if is_gap_up else 'GAP DOWN'})")
    print("-"*80)
    print(f"🎯 Buy CE at Open (₹{entry_price:.2f}) and Hold till EOD (₹{eod_price:.2f}):")
    print(f"  ▸ EOD P&L:            {net_change:+.2f} Nifty Spot Points")
    print(f"  ▸ Max Peak (High):    {max_favorable_run:+.2f} points (Hit ₹{highest_price:.2f})")
    print(f"  ▸ Max Drawdown (Low): -{max_adverse_run:.2f} points (Hit ₹{lowest_price:.2f})")
    print("-"*80)
    
    # Check if we hit +20 points target during the day
    hit_target = False
    target_time = None
    for idx, row in df.iterrows():
        if row['high'] >= entry_price + 20.0:
            hit_target = True
            target_time = row['date'].strftime("%H:%M")
            break
            
    if hit_target:
        print(f"✅ TARGET HITS: Yes! Price hit +20.0 points at {target_time} AM/PM!")
    else:
        print("❌ TARGET HITS: No, price never reached +20.0 points above the open today.")
    print("="*80)

if __name__ == "__main__":
    analyze_gap_play()
