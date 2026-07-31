import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def analyze_sar_effectiveness():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # Get past 91 trading days
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
        
        if gap_pct >= 0.008:
            is_call = False
        elif gap_pct <= -0.008:
            is_call = True
        elif gap > 0:
            is_call = True
        else:
            is_call = False
            
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
                
        pnl = 0.0
        sar_taken = False
        sar_pnl = 0.0
        
        if hit_target:
            pnl = 40.0
        elif hit_sl:
            pnl = -50.0
            sar_taken = True
            df_sar = df_trade.loc[hit_idx:]
            sar_exit_price = df_sar.iloc[-1]['close']
            if is_call:
                sl_price = entry_price - 50.0
                sar_pnl = sl_price - sar_exit_price
            else:
                sl_price = entry_price + 50.0
                sar_pnl = sar_exit_price - sl_price
        else:
            if is_call:
                pnl = eod_price - entry_price
            else:
                pnl = entry_price - eod_price
                
        results.append({
            "date": day,
            "initial_pnl": pnl,
            "sar_taken": sar_taken,
            "sar_pnl": sar_pnl,
            "net_pnl": pnl + sar_pnl
        })
        
    df_res = pd.DataFrame(results)
    
    # Filter only days where initial trade hit the -50 SL
    df_sl_days = df_res[df_res['initial_pnl'] == -50.0]
    total_sl_days = len(df_sl_days)
    
    # 1. Days where SAR made positive points (reducing the loss)
    reduced_loss_days = len(df_sl_days[df_sl_days['sar_pnl'] > 0.0])
    
    # 2. Days where SAR made > 50 points (turning day green)
    green_days = len(df_sl_days[df_sl_days['net_pnl'] > 0.0])
    
    # 3. Days where SAR lost points (whip-saws)
    whipsaw_days = len(df_sl_days[df_sl_days['sar_pnl'] <= 0.0])
    
    print("\n" + "="*80)
    print("📊 STOP-AND-REVERSE (SAR) EFFECTIVENESS SUMMARY (PAST 90 DAYS)")
    print("="*80)
    print(f"Total Days Initial Trade hit -50 SL:  {total_sl_days} days")
    print("-"*80)
    print(f"✅ Loss Reduced (SAR P&L > 0):       {reduced_loss_days} of {total_sl_days} days ({reduced_loss_days/total_sl_days*100:.1f}%)")
    print(f"🟢 Turned Net Green (Net P&L > 0):    {green_days} of {total_sl_days} days ({green_days/total_sl_days*100:.1f}%)")
    print(f"❌ Whip-sawed (SAR P&L <= 0):         {whipsaw_days} of {total_sl_days} days ({whipsaw_days/total_sl_days*100:.1f}%)")
    print("-"*80)
    
    # Print list of SL days and what happened
    print("\n📋 List of Stop-Loss Days & SAR Outcomes:")
    print(f"{'Date':<10} | {'Initial':<11} | {'SAR P&L':<10} | {'Net Day P&L':<15} | {'Outcome'}")
    print("-"*80)
    for idx, r in df_sl_days.iterrows():
        outcome = "Whip-saw (Lost more)"
        if r['net_pnl'] > 0.0:
            outcome = "Turned Net Green!"
        elif r['sar_pnl'] > 0.0:
            outcome = "Reduced Loss"
            
        print(f"{r['date'].strftime('%Y-%m-%d'):<10} | {r['initial_pnl']:+5.1f} pts | {r['sar_pnl']:+8.1f} | {r['net_pnl']:>+11.1f} pts | {outcome}")
    print("="*80)

if __name__ == "__main__":
    analyze_sar_effectiveness()
