import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def generate_sar_weekly_report():
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
        
        # Stop-and-Reverse (SAR) logic based on Target 40
        hit_target = False
        hit_sl = False
        hit_idx = None
        for idx, row in df_trade.iterrows():
            if is_call:
                profit = row['high'] - entry_price
                drawdown = entry_price - row['low']
            else:
                profit = entry_price - row['low']
                drawdown = row['high'] - entry_price
                
            if drawdown >= 50.0:
                hit_sl = True
                hit_idx = idx
                break
            elif profit >= 40.0:
                hit_target = True
                hit_idx = idx
                break
                
        pnl_sar = 0.0
        if hit_target:
            pnl_sar = 40.0
        elif hit_sl:
            pnl_sar = -50.0
            df_sar = df_trade.loc[hit_idx:]
            sar_exit_price = df_sar.iloc[-1]['close']
            if is_call:
                sl_price = entry_price - 50.0
                sar_pnl = sl_price - sar_exit_price
            else:
                sl_price = entry_price + 50.0
                sar_pnl = sar_exit_price - sl_price
            pnl_sar += sar_pnl
        else:
            if is_call:
                pnl_sar = eod_price - entry_price
            else:
                pnl_sar = entry_price - eod_price
                
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
            "sar": pnl_sar,
            "trail": pnl_trail
        })
        
    df_res = pd.DataFrame(trades)
    
    # Group the trades by actual calendar weeks (ending on Friday)
    df_res['week_start'] = df_res['date'].apply(lambda d: d - dt.timedelta(days=d.weekday()))
    df_res['week_end'] = df_res['week_start'].apply(lambda d: d + dt.timedelta(days=4))
    
    # Generate all calendar weeks in the range
    min_date = df_res['date'].min()
    max_date = df_res['date'].max()
    
    # Start on the Monday of the first date
    start_monday = min_date - dt.timedelta(days=min_date.weekday())
    
    calendar_weeks = []
    curr = start_monday
    while curr <= max_date:
        w_end = curr + dt.timedelta(days=4)
        calendar_weeks.append((curr, w_end))
        curr += dt.timedelta(days=7)
        
    print("\n" + "="*125)
    print("📊 90-DAY WEEK-BY-WEEK CALENDAR STRATEGY REPORT (2 LOTS / 130 SHARES)")
    print("="*125)
    print(f"{'Week Period':<26} | {'Target +20 P&L':<18} | {'Target +40 P&L':<18} | {'🔄 SAR Strategy P&L':<21} | {'Trailing Breakeven P&L'}")
    print("-"*125)
    
    grand_t20 = 0.0
    grand_t40 = 0.0
    grand_sar = 0.0
    grand_trail = 0.0
    
    for idx, (w_start, w_end) in enumerate(calendar_weeks):
        # Filter trades in this calendar week
        df_w = df_res[(df_res['date'] >= w_start) & (df_res['date'] <= w_end)]
        
        pts_t20 = df_w['t20'].sum() if not df_w.empty else 0.0
        pts_t40 = df_w['t40'].sum() if not df_w.empty else 0.0
        pts_sar = df_w['sar'].sum() if not df_w.empty else 0.0
        pts_trail = df_w['trail'].sum() if not df_w.empty else 0.0
        
        rs_t20 = pts_t20 * 130
        rs_t40 = pts_t40 * 130
        rs_sar = pts_sar * 130
        rs_trail = pts_trail * 130
        
        grand_t20 += rs_t20
        grand_t40 += rs_t40
        grand_sar += rs_sar
        grand_trail += rs_trail
        
        period_str = f"{w_start.strftime('%b %d')} - {w_end.strftime('%b %d')}"
        print(f"W{idx+1:02d}: {period_str:<20} | {pts_t20:>+5.1f} pts (₹{rs_t20:>+6.0f}) | {pts_t40:>+5.1f} pts (₹{rs_t40:>+6.0f}) | {pts_sar:>+5.1f} pts (₹{rs_sar:>+6.0f}) | {pts_trail:>+5.1f} pts (₹{rs_trail:>+6.0f})")
        
    print("-"*125)
    print(f"{'GRAND TOTAL':<26} | ₹{grand_t20:>+12.2f}      | ₹{grand_t40:>+12.2f}      | ₹{grand_sar:>+12.2f}      | ₹{grand_trail:>+12.2f}")
    print("="*125)

if __name__ == "__main__":
    generate_sar_weekly_report()
