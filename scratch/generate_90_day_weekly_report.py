import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def generate_90_day_weekly_report():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # Get past 91 trading days (excluding weekends) to have 90 testable days
    trading_days = []
    current_date = today
    while len(trading_days) < 92:
        if current_date.weekday() < 5:
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
    trading_days.reverse()
    
    test_days = trading_days[-90:] # exactly 90 days
    
    trades = []
    
    print("Processing historical data day-by-day...")
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
            
        # 2. Today's Candles
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
        gap_pct = gap / yesterday_close
        
        # Resolve Direction
        if gap_pct >= 0.008:
            is_call = False
        elif gap_pct <= -0.008:
            is_call = True
        elif gap > 0:
            is_call = True
        else:
            is_call = False
            
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
        
        # 3. Simulate performance
        def evaluate_strategy(target_val, sl_val):
            hit = False
            hit_idx = None
            for idx, row in df_trade.iterrows():
                if is_call:
                    profit = row['high'] - entry_price
                else:
                    profit = entry_price - row['low']
                if profit >= target_val:
                    hit = True
                    hit_idx = idx
                    break
                    
            if hit:
                df_before = df_trade.loc[:hit_idx]
                if is_call:
                    drawdown = entry_price - df_before['low'].min()
                else:
                    drawdown = df_before['high'].max() - entry_price
                    
                if drawdown > sl_val:
                    return -sl_val
                else:
                    return target_val
            else:
                if is_call:
                    lowest_all = df_trade['low'].min()
                    drawdown = entry_price - lowest_all
                else:
                    highest_all = df_trade['high'].max()
                    drawdown = highest_all - entry_price
                    
                if drawdown > sl_val:
                    return -sl_val
                else:
                    if is_call:
                        return eod_price - entry_price
                    else:
                        return entry_price - eod_price
                        
        pnl_t20 = evaluate_strategy(20.0, 50.0)
        pnl_t40 = evaluate_strategy(40.0, 50.0)
        pnl_t60 = evaluate_strategy(60.0, 50.0)
        
        # Trailing breakeven
        hit_20 = False
        hit_20_idx = None
        for idx, row in df_trade.iterrows():
            if is_call:
                profit = row['high'] - entry_price
            else:
                profit = entry_price - row['low']
            if profit >= 20.0:
                hit_20 = True
                hit_20_idx = idx
                break
                
        if hit_20:
            df_before_20 = df_trade.loc[:hit_20_idx]
            if is_call:
                dd_before_20 = entry_price - df_before_20['low'].min()
            else:
                dd_before_20 = df_before_20['high'].max() - entry_price
                
            if dd_before_20 > 50.0:
                pnl_trail = -50.0
            else:
                df_after_20 = df_trade.loc[hit_20_idx:]
                hit_40 = False
                hit_40_idx = None
                for idx, row in df_after_20.iterrows():
                    if is_call:
                        profit = row['high'] - entry_price
                    else:
                        profit = entry_price - row['low']
                    if profit >= 40.0:
                        hit_40 = True
                        hit_40_idx = idx
                        break
                        
                if hit_40:
                    df_between = df_trade.loc[hit_20_idx:hit_40_idx]
                    if is_call:
                        lowest_between = df_between['low'].min()
                        hit_breakeven = lowest_between <= entry_price
                    else:
                        highest_between = df_between['high'].max()
                        hit_breakeven = highest_between >= entry_price
                        
                    if hit_breakeven:
                        pnl_trail = 0.0
                    else:
                        pnl_trail = 40.0
                else:
                    if is_call:
                        lowest_rest = df_after_20['low'].min()
                        hit_breakeven = lowest_rest <= entry_price
                    else:
                        highest_rest = df_after_20['high'].max()
                        hit_breakeven = highest_rest >= entry_price
                        
                    if hit_breakeven:
                        pnl_trail = 0.0
                    else:
                        if is_call:
                            pnl_trail = eod_price - entry_price
                        else:
                            pnl_trail = entry_price - eod_price
        else:
            if is_call:
                lowest_all = df_trade['low'].min()
                drawdown = entry_price - lowest_all
            else:
                highest_all = df_trade['high'].max()
                drawdown = highest_all - entry_price
                
            if drawdown > 50.0:
                pnl_trail = -50.0
            else:
                if is_call:
                    pnl_trail = eod_price - entry_price
                else:
                    pnl_trail = entry_price - eod_price
                    
        trades.append({
            "date": day,
            "t20": pnl_t20,
            "t40": pnl_t40,
            "t60": pnl_t60,
            "trail": pnl_trail
        })
        
    df_res = pd.DataFrame(trades)
    
    # Slice the trades into weeks of exactly 5 trading days each (90 days = 18 weeks)
    week_results = []
    for w_idx in range(18):
        start_idx = w_idx * 5
        end_idx = start_idx + 5
        df_w = df_res.iloc[start_idx:end_idx]
        
        start_date = df_w.iloc[0]['date']
        end_date = df_w.iloc[-1]['date']
        
        pts_t20 = df_w['t20'].sum()
        pts_t40 = df_w['t40'].sum()
        pts_t60 = df_w['t60'].sum()
        pts_trail = df_w['trail'].sum()
        
        week_results.append({
            "week_num": w_idx + 1,
            "period": f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}",
            "t20_pts": pts_t20,
            "t40_pts": pts_t40,
            "t60_pts": pts_t60,
            "trail_pts": pts_trail
        })
        
    print("\n" + "="*120)
    print("📊 90-DAY (18-WEEK) WEEK-BY-WEEK STRATEGY REPORT (2 LOTS / 130 SHARES)")
    print("="*120)
    print(f"{'Week Period':<26} | {'Target +20 P&L':<18} | {'Target +40 P&L':<18} | {'Target +60 P&L':<18} | {'Trailing Breakeven P&L'}")
    print("-"*120)
    
    grand_t20 = 0.0
    grand_t40 = 0.0
    grand_t60 = 0.0
    grand_trail = 0.0
    
    for w in week_results:
        rs_t20 = w['t20_pts'] * 130
        rs_t40 = w['t40_pts'] * 130
        rs_t60 = w['t60_pts'] * 130
        rs_trail = w['trail_pts'] * 130
        
        grand_t20 += rs_t20
        grand_t40 += rs_t40
        grand_t60 += rs_t60
        grand_trail += rs_trail
        
        print(f"W{w['week_num']:02d}: {w['period']:<20} | {w['t20_pts']:>+5.1f} pts (₹{rs_t20:>+6.0f}) | {w['t40_pts']:>+5.1f} pts (₹{rs_t40:>+6.0f}) | {w['t60_pts']:>+5.1f} pts (₹{rs_t60:>+6.0f}) | {w['trail_pts']:>+5.1f} pts (₹{rs_trail:>+6.0f})")
        
    print("-"*120)
    print(f"{'GRAND TOTAL (18 WEEKS)':<26} | ₹{grand_t20:>+12.2f}      | ₹{grand_t40:>+12.2f}      | ₹{grand_t60:>+12.2f}      | ₹{grand_trail:>+12.2f}")
    print("="*120)

if __name__ == "__main__":
    generate_90_day_weekly_report()
