import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def calculate_pnl():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    start_dt = dt.datetime.combine(today, dt.time(9, 15))
    end_dt = dt.datetime.combine(today, dt.time(15, 30))
    
    print("Fetching today's 1-minute Nifty Spot candles...")
    try:
        candles = kc.historical_data(nifty_token, start_dt, end_dt, "minute")
    except Exception as e:
        print(f"Error: {e}")
        return
        
    df = pd.DataFrame(candles)
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    
    # We evaluate P&L for our 3 extreme PCR entry signals:
    # 1. 09:24:00 (PCR <= 0.70 - Bullish momentum start)
    # 2. 10:22:00 (PCR >= 1.30 - Bearish capitulation start)
    # 3. 13:40:00 (PCR <= 0.70 - Bullish exhaustion start)
    
    entries = [
        {"time": "09:24:00", "type": "BUY CE (Trend-Follow)", "fade_type": "BUY PE (Fade)"},
        {"time": "10:22:00", "type": "BUY CE (Fade / Bottom Reversion)", "fade_type": "BUY PE (Trend-Follow)"},
        {"time": "13:40:00", "type": "BUY PE (Fade / Top Reversion)", "fade_type": "BUY CE (Trend-Follow)"}
    ]
    
    print("\n" + "="*80)
    print(f"📊 INTRADAY PCR CLUSTER P&L REPORT (TODAY: {today})")
    print("="*80)
    
    for idx, ent in enumerate(entries):
        entry_time_str = ent["time"]
        entry_dt = pd.to_datetime(f"{today} {entry_time_str}").tz_localize(df.index.tz)
        
        if entry_dt not in df.index:
            # try next minute if exact match not found
            entry_dt = entry_dt + dt.timedelta(minutes=1)
            if entry_dt not in df.index:
                continue
                
        entry_price = df.loc[entry_dt, 'open']
        print(f"\n🚀 CLUSTER {idx+1}: Entry at {entry_dt.strftime('%H:%M:%S')} @ Spot ₹{entry_price:.2f}")
        print(f"   ▸ Setup: {ent['type']} (PCR Reversal)")
        print("-"*80)
        print(f"{'Hold Time':<10} | {'Spot Price':<12} | {'Trend Points':<15} | {'Fade Points':<15}")
        print("-"*80)
        
        # Check P&L at 5m, 10m, 20m, 30m
        for hold_min in [5, 10, 20, 30]:
            target_dt = entry_dt + dt.timedelta(minutes=hold_min)
            if target_dt in df.index:
                target_price = df.loc[target_dt, 'close']
                
                # Calculate Trend-following points
                if "BUY CE" in ent["type"]:
                    trend_pts = target_price - entry_price
                    fade_pts = entry_price - target_price
                else: # BUY PE
                    trend_pts = entry_price - target_price
                    fade_pts = target_price - entry_price
                    
                print(f"{hold_min:<9}m | ₹{target_price:<10.2f} | {trend_pts:+.2f} pts      | {fade_pts:+.2f} pts")
            else:
                print(f"{hold_min:<9}m | {'N/A (Market Closed)':<38}")
        print("-"*80)
        
    print("="*80)

if __name__ == "__main__":
    calculate_pnl()
