import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def compare_pnl():
    kc = Z.kite()
    nifty_token = 256265
    today = dt.date.today()
    
    # Run 90-day logic for July
    trading_days = []
    current_date = today
    while len(trading_days) < 92:
        if current_date.weekday() < 5:
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
    trading_days.reverse()
    test_days = trading_days[-90:]
    
    pnl_90 = {}
    for day in test_days:
        if day < dt.date(2026, 7, 20): # only check last 2 weeks
            continue
        yest_idx = trading_days.index(day) - 1
        yesterday = trading_days[yest_idx]
        
        try:
            y_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
            yesterday_close = y_candles[0]['close'] if y_candles else None
            
            s_dt = dt.datetime.combine(day, dt.time(9, 15))
            e_dt = dt.datetime.combine(day, dt.time(15, 30))
            t_candles = kc.historical_data(nifty_token, s_dt, e_dt, "minute")
            if not t_candles:
                continue
            df = pd.DataFrame(t_candles)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            
            today_open = df.iloc[0]['open']
            gap = today_open - yesterday_close
            gap_pct = gap / yesterday_close
            is_call = True if gap > 0 else False # simplified for comparison
            if gap_pct >= 0.008:
                is_call = False
            elif gap_pct <= -0.008:
                is_call = True
                
            target_dt = pd.to_datetime(f"{day} 10:00:00").tz_localize(df.index.tz)
            if target_dt not in df.index:
                target_dt = df.index[df.index >= target_dt][0]
            entry = df.loc[target_dt, 'open']
            
            # Target 40
            hit = False
            for idx, row in df.loc[target_dt:].iterrows():
                if is_call and row['high'] - entry >= 40.0:
                    hit = True; break
                elif not is_call and entry - row['low'] >= 40.0:
                    hit = True; break
            pnl_90[day] = 40.0 if hit else -50.0 # simplified SL
        except:
            pass

    print("PNL from 90-day logic:")
    for d, p in pnl_90.items():
        print(f"  {d}: {p} pts")

if __name__ == "__main__":
    compare_pnl()
