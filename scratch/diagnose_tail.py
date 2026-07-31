import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def diagnose():
    kc = Z.kite()
    nifty_token = 256265
    
    print("Fetching Nifty spot daily candles for the last 10 trading days...")
    daily = kc.historical_data(nifty_token, dt.date(2026, 7, 15), dt.date(2026, 7, 31), "day")
    for c in daily:
        print(f"Daily API: Date={c['date'].strftime('%Y-%m-%d')}, Close={c['close']}")
        
    print("\nFetching Nifty spot 1-minute candles for the last 10 trading days...")
    s_dt = dt.datetime(2026, 7, 15, 9, 15)
    e_dt = dt.datetime(2026, 7, 31, 15, 30)
    minutes = kc.historical_data(nifty_token, s_dt, e_dt, "minute")
    
    df_min = pd.DataFrame(minutes)
    df_min['trade_day'] = df_min['date'].dt.date
    grouped = df_min.groupby('trade_day')
    for day, grp in grouped:
        print(f"1-Min Group: Date={day}, Candles={len(grp)}, FirstOpen={grp.iloc[0]['open']}, LastClose={grp.iloc[-1]['close']}")

if __name__ == "__main__":
    diagnose()
