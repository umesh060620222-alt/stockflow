import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_filtered_backtest():
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
    
    print("\n" + "="*110)
    print("📊 10:00 AM GAP RULE 90-DAY BACKTEST (GAP LIMIT FILTER ACTIVE: 20 to 80 POINTS)")
    print("="*110)
    print(f"{'Date':<10} | {'Gap Size':<10} | {'Status':<10} | {'10AM Spot':<9} | {'EOD Spot':<9} | {'Max Peak':<8} | {'Drawdown':<8} | {'T:+20':<6} | {'T:+40':<6}")
    print("-"*110)
    
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
        abs_gap = abs(gap)
        
        # Apply Gap Limit Filter: Gap must be between 20 and 80 points
        if abs_gap < 20.0 or abs_gap > 80.0:
            status = "FILTERED"
            # Just print a filtered line and skip calculations
            print(f"{day.strftime('%Y-%m-%d'):<10} | {gap:<+10.1f} | {status:<10} | {'-':<9} | {'-':<9} | {'-':<8} | {'-':<8} | {'-':<6} | {'-':<6}")
            continue
            
        status = "TRADED"
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
        eod_price = df_trade.iloc[-1]['close']
        
        # Calculate Max Peak and Max Drawdown
        if is_gap_up:
            highest_price = df_trade['high'].max()
            lowest_price = df_trade['low'].min()
            max_profit = highest_price - entry_price
            max_drawdown = entry_price - lowest_price
        else:
            lowest_price = df_trade['low'].min()
            highest_price = df_trade['high'].max()
            max_profit = entry_price - lowest_price
            max_drawdown = highest_price - entry_price
            
        t20_hit = max_profit >= 20.0
        t40_hit = max_profit >= 40.0
        
        t20_str = "✅ HIT" if t20_hit else "❌ MS"
        t40_str = "✅ HIT" if t40_hit else "❌ MS"
        
        results.append({
            "date": day,
            "gap": gap,
            "entry_price": entry_price,
            "eod_price": eod_price,
            "max_profit": max_profit,
            "max_drawdown": max_drawdown,
            "t20_hit": t20_hit,
            "t40_hit": t40_hit
        })
        
        print(f"{day.strftime('%Y-%m-%d'):<10} | {gap:<+10.1f} | {status:<10} | {entry_price:<9.2f} | {eod_price:<9.2f} | {max_profit:<+8.1f} | -{max_drawdown:<7.1f} | {t20_str:<6} | {t40_str:<6}")
        
    print("-"*110)
    
    # 4. Print Summary Stats
    total_trades = len(results)
    if total_trades == 0:
        print("No trades triggered after filtering.")
        return
        
    # Evaluate Target 20 Stats with -50 SL
    t20_wins = 0
    t20_losses = 0
    t20_points = 0.0
    for r in results:
        # If target hit, check if it hit SL first
        if r["t20_hit"] and r["max_drawdown"] <= 50.0:
            t20_wins += 1
            t20_points += 20.0
        else:
            t20_losses += 1
            t20_points -= 50.0
            
    t20_win_rate = (t20_wins / total_trades) * 100
    
    # Evaluate Target 40 Stats with -50 SL
    t40_wins = 0
    t40_losses = 0
    t40_points = 0.0
    for r in results:
        if r["t40_hit"] and r["max_drawdown"] <= 50.0:
            t40_wins += 1
            t40_points += 40.0
        else:
            t40_losses += 1
            t40_points -= 50.0
            
    t40_win_rate = (t40_wins / total_trades) * 100
    
    print(f"📊 SUMMARY FOR FILTERED STRATEGY:")
    print(f"   Total Traded Days:         {total_trades} (of 90 days)")
    print(f"   Filtered Out Days:         {90 - total_trades} days")
    print("-"*110)
    print(f"🎯 Target +20 Points (with -50 SL):")
    print(f"   ▸ Win Rate:                {t20_win_rate:.1f}% ({t20_wins} Wins / {t20_losses} Losses)")
    print(f"   ▸ Net Points P&L:          {t20_points:+.2f} Nifty Spot Points")
    print(f"   ▸ 1 Lot Profit (75 shares): +₹{t20_points*75:.2f}")
    print("-"*110)
    print(f"🎯 Target +40 Points (with -50 SL):")
    print(f"   ▸ Win Rate:                {t40_win_rate:.1f}% ({t40_wins} Wins / {t40_losses} Losses)")
    print(f"   ▸ Net Points P&L:          {t40_points:+.2f} Nifty Spot Points")
    print(f"   ▸ 1 Lot Profit (75 shares): +₹{t40_points*75:.2f}")
    print("="*110)

if __name__ == "__main__":
    run_filtered_backtest()
