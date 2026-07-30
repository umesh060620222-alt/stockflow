import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def verify_prices():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    if yesterday.weekday() >= 5:
        yesterday = yesterday - dt.timedelta(days=2 if yesterday.weekday() == 6 else 1)
        
    print(f"Fetching raw daily data for {yesterday} and {today}...")
    try:
        daily_candles = kc.historical_data(nifty_token, yesterday, today, "day")
        for c in daily_candles:
            print(f"Daily Candle: Date={c['date']}, Open={c['open']}, High={c['high']}, Low={c['low']}, Close={c['close']}")
    except Exception as e:
        print(f"Error fetching daily candles: {e}")
        
    print(f"\nFetching today's ({today}) 1-minute candles around 10:00 AM...")
    try:
        start_dt = dt.datetime.combine(today, dt.time(9, 58))
        end_dt = dt.datetime.combine(today, dt.time(10, 5))
        min_candles = kc.historical_data(nifty_token, start_dt, end_dt, "minute")
        for c in min_candles:
            print(f"Minute Candle: Time={c['date'].strftime('%H:%M')}, Open={c['open']}, High={c['high']}, Low={c['low']}, Close={c['close']}")
    except Exception as e:
        print(f"Error fetching minute candles: {e}")
        
if __name__ == "__main__":
    verify_prices()
