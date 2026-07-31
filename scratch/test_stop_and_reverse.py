import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_sar_backtest():
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
    
    print("\nProcessing historical data day-by-day...")
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
            is_call = False # PE (Fade)
        elif gap_pct <= -0.008:
            is_call = True # CE (Fade)
        elif gap > 0:
            is_call = True # CE (Follow)
        else:
            is_call = False # PE (Follow)
            
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
        
        # Check target +40.0 hit or -50.0 SL hit first
        hit_target = False
        hit_sl = False
        hit_idx = None
        
        # Target = 40.0, SL = 50.0
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
            # Stopped out at -50! Immediately Stop-and-Reverse (SAR)
            pnl = -50.0
            sar_taken = True
            
            # SL price
            if is_call:
                sl_price = entry_price - 50.0
                # Reverse trade: BUY PE (short)
                # Hold until EOD close
                df_sar = df_trade.loc[hit_idx:]
                sar_exit_price = df_sar.iloc[-1]['close']
                sar_pnl = sl_price - sar_exit_price
            else:
                sl_price = entry_price + 50.0
                # Reverse trade: BUY CE (long)
                # Hold until EOD close
                df_sar = df_trade.loc[hit_idx:]
                sar_exit_price = df_sar.iloc[-1]['close']
                sar_pnl = sar_exit_price - sl_price
        else:
            # Exit at EOD close
            if is_call:
                pnl = eod_price - entry_price
            else:
                pnl = entry_price - eod_price
                
        results.append({
            "date": day,
            "gap_pct": gap_pct,
            "initial_pnl": pnl,
            "sar_taken": sar_taken,
            "sar_pnl": sar_pnl,
            "net_pnl": pnl + sar_pnl
        })
        
    df_res = pd.DataFrame(results)
    
    # Filter for past 4 weeks (July 3 to July 30) for day-by-day report
    df_month = df_res[df_res['date'] >= dt.date(2026, 7, 3)]
    
    print("\n" + "="*115)
    print("📊 STOP-AND-REVERSE (SAR) DAY-BY-DAY REPORT FOR THIS MONTH (2 LOTS / 130 SHARES)")
    print("="*115)
    print(f"{'Date':<10} | {'Gap %':<7} | {'Initial P&L':<15} | {'SAR Entered':<11} | {'SAR P&L':<15} | {'Net Day P&L'}")
    print("-"*115)
    
    for idx, r in df_month.iterrows():
        init_str = f"{r['initial_pnl']:+5.1f} pts"
        sar_str = f"{r['sar_pnl']:+5.1f} pts" if r['sar_taken'] else "-"
        sar_ent = "YES" if r['sar_taken'] else "NO"
        net_pts = r['net_pnl']
        net_rs = net_pts * 130
        
        print(f"{r['date'].strftime('%Y-%m-%d'):<10} | {r['gap_pct']*100:<+6.2f}% | {init_str:<15} | {sar_ent:<11} | {sar_str:<15} | {net_pts:>+5.1f} pts (₹{net_rs:>+6.0f})")
        
    print("-"*115)
    
    # 90-Day overall summary comparison
    total_wins_init = len(df_res[df_res['initial_pnl'] == 40.0])
    total_losses_init = len(df_res[df_res['initial_pnl'] == -50.0])
    
    total_points_initial = df_res['initial_pnl'].sum()
    total_points_sar = df_res['net_pnl'].sum()
    
    rupee_init = total_points_initial * 130
    rupee_sar = total_points_sar * 130
    
    print(f"📊 90-DAY STRATEGY COMPARISON (2 LOTS / 130 SHARES):")
    print(f"   Total Traded Days:             {len(df_res)} days")
    print("-"*115)
    print(f"🎯 Standard Target +40 Strategy (with -50 SL):")
    print(f"   ▸ Net Points P&L:              {total_points_initial:+.2f} points")
    print(f"   ▸ Net Rupee Profit:            +₹{rupee_init:,.2f}")
    print("-"*115)
    print(f"🔄 Stop-And-Reverse (SAR) Strategy at SL:")
    print(f"   ▸ Net Points P&L:              {total_points_sar:+.2f} points")
    print(f"   ▸ Net Rupee Profit:            +₹{rupee_sar:,.2f}")
    print("="*115)

if __name__ == "__main__":
    run_sar_backtest()
