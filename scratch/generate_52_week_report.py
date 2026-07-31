import datetime as dt
import pandas as pd
import numpy as np
import sys
import os
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def generate_52_week_report():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # We need to trace back exactly 365 calendar days
    start_date = today - dt.timedelta(days=365)
    
    # Get list of weekdays in the past 365 days
    all_days = []
    curr = start_date
    while curr <= today:
        if curr.weekday() < 5:
            all_days.append(curr)
        curr += dt.timedelta(days=1)
        
    print(f"Total potential trading weekdays in the past year: {len(all_days)}")
    
    # We will fetch historical 1-minute candles in chunks of 30 days to avoid Kite API limits
    print("Fetching Nifty 1-minute candles in monthly chunks...")
    chunks = []
    chunk_start = start_date
    while chunk_start <= today:
        chunk_end = min(chunk_start + dt.timedelta(days=29), today)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + dt.timedelta(days=1)
        
    all_candles = []
    for c_start, c_end in chunks:
        # Fetch daily for close reference
        try:
            # Fetch 1-minute candles
            s_dt = dt.datetime.combine(c_start, dt.time(9, 15))
            e_dt = dt.datetime.combine(c_end, dt.time(15, 30))
            candles = kc.historical_data(nifty_token, s_dt, e_dt, "minute")
            if candles:
                all_candles.extend(candles)
            time.sleep(0.1) # short rate limit break
        except Exception as e:
            print(f"Skipped chunk {c_start} to {c_end}: {e}")
            
    # Fetch official daily closes for yesterday close references
    print("Fetching official daily candles for close price lookup...")
    daily_candles = kc.historical_data(nifty_token, start_date - dt.timedelta(days=7), today, "day")
    daily_close_map = {c['date'].date(): c['close'] for c in daily_candles}
    
    df_raw = pd.DataFrame(all_candles)
    if df_raw.empty:
        print("No historical data found.")
        return
        
    df_raw['date'] = pd.to_datetime(df_raw['date'])
    # Add a date only column for grouping
    df_raw['trade_day'] = df_raw['date'].dt.date
    
    # Group raw candles by day
    days_grouped = {day: grp for day, grp in df_raw.groupby('trade_day')}
    sorted_days = sorted(list(days_grouped.keys()))
    
    trades = []
    
    print(f"Processing {len(sorted_days)} trading days...")
    for idx, day in enumerate(sorted_days):
        if idx == 0:
            continue # need yesterday's close
            
        yesterday = sorted_days[idx - 1]
        yesterday_close = daily_close_map.get(yesterday)
        
        if yesterday_close is None:
            # fallback if not in map
            yest_df = days_grouped[yesterday]
            yesterday_close = yest_df.iloc[-1]['close']
            
        today_df = days_grouped[day].copy()
        today_df.set_index('date', inplace=True)
        
        today_open = today_df.iloc[0]['open']
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
        target_dt = pd.to_datetime(f"{day} 10:00:00").tz_localize(today_df.index.tz)
        if target_dt not in today_df.index:
            active_indices = today_df.index[today_df.index >= target_dt]
            if len(active_indices) > 0:
                target_dt = active_indices[0]
            else:
                continue
                
        entry_price = today_df.loc[target_dt, 'open']
        df_trade = today_df.loc[target_dt:]
        eod_price = df_trade.iloc[-1]['close']
        
        # Evaluate Target 20
        def eval_t20():
            for t_idx, row in df_trade.iterrows():
                if is_call:
                    if row['high'] - entry_price >= 20.0:
                        # check drawdown before hit
                        df_b = df_trade.loc[:t_idx]
                        if entry_price - df_b['low'].min() > 50.0:
                            return -50.0
                        return 20.0
                else:
                    if entry_price - row['low'] >= 20.0:
                        df_b = df_trade.loc[:t_idx]
                        if df_b['high'].max() - entry_price > 50.0:
                            return -50.0
                        return 20.0
            # EOD check
            if is_call:
                if entry_price - df_trade['low'].min() > 50.0:
                    return -50.0
                return eod_price - entry_price
            else:
                if df_trade['high'].max() - entry_price > 50.0:
                    return -50.0
                return entry_price - eod_price
                
        # Evaluate Target 40
        def eval_t40():
            for t_idx, row in df_trade.iterrows():
                if is_call:
                    if row['high'] - entry_price >= 40.0:
                        df_b = df_trade.loc[:t_idx]
                        if entry_price - df_b['low'].min() > 50.0:
                            return -50.0
                        return 40.0
                else:
                    if entry_price - row['low'] >= 40.0:
                        df_b = df_trade.loc[:t_idx]
                        if df_b['high'].max() - entry_price > 50.0:
                            return -50.0
                        return 40.0
            # EOD check
            if is_call:
                if entry_price - df_trade['low'].min() > 50.0:
                    return -50.0
                return eod_price - entry_price
            else:
                if df_trade['high'].max() - entry_price > 50.0:
                    return -50.0
                return entry_price - eod_price
                
        # Evaluate SAR Target-50 Breakeven
        def eval_sar():
            hit_sl = False
            hit_idx = None
            hit_target = False
            
            for t_idx, row in df_trade.iterrows():
                if is_call:
                    profit = row['high'] - entry_price
                    drawdown = entry_price - row['low']
                else:
                    profit = entry_price - row['low']
                    drawdown = row['high'] - entry_price
                    
                if drawdown >= 50.0:
                    hit_sl = True
                    hit_idx = t_idx
                    break
                elif profit >= 40.0:
                    hit_target = True
                    hit_idx = t_idx
                    break
                    
            if hit_target:
                return 40.0
            elif hit_sl:
                if is_call:
                    sl_price = entry_price - 50.0
                else:
                    sl_price = entry_price + 50.0
                    
                df_sar = df_trade.loc[hit_idx:]
                sar_hit_target = False
                sar_hit_sl = False
                
                for s_idx, s_row in df_sar.iterrows():
                    if is_call:
                        sar_profit = sl_price - s_row['low']
                        sar_drawdown = s_row['high'] - sl_price
                    else:
                        sar_profit = s_row['high'] - sl_price
                        sar_drawdown = sl_price - s_row['low']
                        
                    if sar_profit >= 50.0:
                        sar_hit_target = True
                        break
                    elif sar_drawdown >= 50.0:
                        sar_hit_sl = True
                        break
                        
                if sar_hit_target:
                    return 0.0 # Breakeven on the day
                elif sar_hit_sl:
                    return -100.0 # Double whipsaw
                else:
                    # Timeout at EOD
                    sar_exit_price = df_sar.iloc[-1]['close']
                    if is_call:
                        return -50.0 + (sl_price - sar_exit_price)
                    else:
                        return -50.0 + (sar_exit_price - sl_price)
            else:
                if is_call:
                    return eod_price - entry_price
                else:
                    return entry_price - eod_price
                    
        p_t20 = eval_t20()
        p_t40 = eval_t40()
        p_sar = eval_sar()
        
        trades.append({
            "date": day,
            "t20": p_t20,
            "t40": p_t40,
            "sar": p_sar
        })
        
    df_res = pd.DataFrame(trades)
    df_res['date'] = pd.to_datetime(df_res['date'])
    
    # Group by calendar week ending Friday
    df_res['week_start'] = df_res['date'].apply(lambda d: d - dt.timedelta(days=d.weekday()))
    df_res['week_end'] = df_res['week_start'].apply(lambda d: d + dt.timedelta(days=4))
    
    # Generate all weeks in range
    min_date = df_res['date'].min()
    max_date = df_res['date'].max()
    start_monday = min_date - dt.timedelta(days=min_date.weekday())
    
    calendar_weeks = []
    curr = start_monday
    while curr <= max_date:
        w_end = curr + dt.timedelta(days=4)
        calendar_weeks.append((curr, w_end))
        curr += dt.timedelta(days=7)
        
    print("\n" + "="*125)
    print("📊 52-WEEK WEEK-BY-WEEK CALENDAR STRATEGY REPORT (2 LOTS / 130 SHARES)")
    print("="*125)
    print(f"{'Week Period':<26} | {'Target +20 P&L':<18} | {'Target +40 P&L':<18} | {'🔄 SAR Strategy P&L'}")
    print("-"*125)
    
    grand_t20 = 0.0
    grand_t40 = 0.0
    grand_sar = 0.0
    
    for idx, (w_start, w_end) in enumerate(calendar_weeks):
        df_w = df_res[(df_res['date'] >= w_start) & (df_res['date'] <= w_end)]
        
        pts_t20 = df_w['t20'].sum() if not df_w.empty else 0.0
        pts_t40 = df_w['t40'].sum() if not df_w.empty else 0.0
        pts_sar = df_w['sar'].sum() if not df_w.empty else 0.0
        
        rs_t20 = pts_t20 * 130
        rs_t40 = pts_t40 * 130
        rs_sar = pts_sar * 130
        
        grand_t20 += rs_t20
        grand_t40 += rs_t40
        grand_sar += rs_sar
        
        period_str = f"{w_start.strftime('%b %d, %Y')} - {w_end.strftime('%b %d, %Y')}"
        print(f"W{idx+1:02d}: {period_str:<26} | {pts_t20:>+5.1f} pts (₹{rs_t20:>+6.0f}) | {pts_t40:>+5.1f} pts (₹{rs_t40:>+6.0f}) | {pts_sar:>+5.1f} pts (₹{rs_sar:>+6.0f})")
        
    print("-"*125)
    print(f"{'GRAND TOTAL':<26} | ₹{grand_t20:>+12.2f}      | ₹{grand_t40:>+12.2f}      | ₹{grand_sar:>+12.2f}")
    print("="*125)

if __name__ == "__main__":
    generate_52_week_report()
