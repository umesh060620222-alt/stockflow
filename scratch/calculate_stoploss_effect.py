import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def analyze_stoploss():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # Get past 91 trading days (excluding weekends)
    trading_days = []
    current_date = today
    while len(trading_days) < 92:
        if current_date.weekday() < 5:
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
    trading_days.reverse()
    
    test_days = trading_days[-90:]
    
    results = []
    
    for i, day in enumerate(test_days):
        yest_idx = trading_days.index(day) - 1
        yesterday = trading_days[yest_idx]
        
        start_dt = dt.datetime.combine(day, dt.time(9, 15))
        end_dt = dt.datetime.combine(day, dt.time(15, 30))
        
        # 1. Fetch Yesterday's Close
        yesterday_close = None
        try:
            yest_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
            if yest_candles:
                yesterday_close = yest_candles[0]['close']
            else:
                y_start = dt.datetime.combine(yesterday, dt.time(9, 15))
                y_end = dt.datetime.combine(yesterday, dt.time(15, 30))
                y_candles = kc.historical_data(nifty_token, y_start, y_end, "minute")
                yesterday_close = y_candles[-1]['close'] if y_candles else None
        except Exception:
            pass
            
        if yesterday_close is None:
            continue
            
        # 2. Fetch Today's Candles
        try:
            today_candles = kc.historical_data(nifty_token, start_dt, end_dt, "minute")
            if not today_candles:
                continue
            df = pd.DataFrame(today_candles)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
        except Exception:
            continue
            
        today_open = df.iloc[0]['open']
        gap = today_open - yesterday_close
        is_gap_up = gap > 0
        
        # Resolve 10:00 AM Entry
        target_dt = pd.to_datetime(f"{day} 10:00:00").tz_localize(df.index.tz)
        if target_dt not in df.index:
            active_indices = df.index[df.index >= target_dt]
            if len(active_indices) > 0:
                target_dt = active_indices[0]
            else:
                continue
                
        entry_price = df.loc[target_dt, 'open']
        df_trade = df.loc[target_dt:]
        
        # We find the exact minute when it hit +20 profit, and check the max drawdown *before* that minute
        hit_target = False
        target_idx = None
        
        for idx, row in df_trade.iterrows():
            if is_gap_up:
                current_profit = row['high'] - entry_price
            else:
                current_profit = entry_price - row['low']
                
            if current_profit >= 20.0:
                hit_target = True
                target_idx = idx
                break
                
        if hit_target:
            # Slice the dataframe up to the target hit candle
            df_before_target = df_trade.loc[:target_idx]
            
            # Calculate the maximum adverse drawdown *before* hitting the target
            if is_gap_up:
                lowest_before = df_before_target['low'].min()
                max_drawdown = entry_price - lowest_before
            else:
                highest_before = df_before_target['high'].max()
                max_drawdown = highest_before - entry_price
                
            results.append({
                "date": day,
                "hit_target": True,
                "drawdown": max_drawdown
            })
        else:
            # Did not hit target, calculate EOD drawdown
            if is_gap_up:
                lowest_all = df_trade['low'].min()
                max_drawdown = entry_price - lowest_all
            else:
                highest_all = df_trade['high'].max()
                max_drawdown = highest_all - entry_price
                
            results.append({
                "date": day,
                "hit_target": False,
                "drawdown": max_drawdown
            })
            
    df_res = pd.DataFrame(results)
    
    total_wins = len(df_res[df_res['hit_target'] == True])
    
    print("\n" + "="*80)
    print("📊 ANALYSIS OF DRAWDOWN BEFORE HITTING +20 TARGET")
    print("="*80)
    print(f"Total Winning Days analyzed: {total_wins}")
    print("-"*80)
    
    # Check stop loss thresholds
    for sl in [10, 15, 20, 25, 30, 35, 40, 50]:
        # Count how many winning days had drawdown > SL (and thus would have been stopped out)
        stopped_wins = len(df_res[(df_res['hit_target'] == True) & (df_res['drawdown'] > sl)])
        remaining_wins = total_wins - stopped_wins
        win_percentage = (remaining_wins / len(df_res)) * 100
        
        print(f"Stop-Loss at -{sl} points:")
        print(f"   ▸ Stopped out wins:  {stopped_wins} of {total_wins}")
        print(f"   ▸ Clean target hits: {remaining_wins}")
        print(f"   ▸ Adjusted Win Rate: {win_percentage:.1f}%")
        print("-"*80)
        
if __name__ == "__main__":
    analyze_stoploss()
