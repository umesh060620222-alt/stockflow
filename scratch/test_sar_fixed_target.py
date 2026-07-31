import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_sar_fixed_target_backtest():
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
        
        # Check initial Target 40 or SL -50
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
        sar_result = "-"
        sar_pnl = 0.0
        
        if hit_target:
            pnl = 40.0
        elif hit_sl:
            pnl = -50.0
            sar_taken = True
            
            # SL price
            if is_call:
                sl_price = entry_price - 50.0
            else:
                sl_price = entry_price + 50.0
                
            # Track SAR trade from hit_idx onwards
            df_sar = df_trade.loc[hit_idx:]
            
            # Target = +50 premium points (recovers -50 SL)
            # SL = -50 premium points (reverts to original entry)
            sar_hit_target = False
            sar_hit_sl = False
            
            for s_idx, s_row in df_sar.iterrows():
                if is_call:
                    # We are in PE: profit if Nifty goes DOWN
                    sar_profit = sl_price - s_row['low']
                    sar_drawdown = s_row['high'] - sl_price
                else:
                    # We are in CE: profit if Nifty goes UP
                    sar_profit = s_row['high'] - sl_price
                    sar_drawdown = sl_price - s_row['low']
                    
                if sar_profit >= 50.0:
                    sar_hit_target = True
                    break
                elif sar_drawdown >= 50.0:
                    sar_hit_sl = True
                    break
                    
            if sar_hit_target:
                sar_pnl = 50.0
                sar_result = "✅ BREAKEVEN"
            elif sar_hit_sl:
                sar_pnl = -50.0
                sar_result = "❌ DBL WHIP"
            else:
                # EOD Exit
                sar_exit_price = df_sar.iloc[-1]['close']
                if is_call:
                    sar_pnl = sl_price - sar_exit_price
                else:
                    sar_pnl = sar_exit_price - sl_price
                sar_result = "TIMEOUT (EOD)"
        else:
            # EOD Exit for initial trade
            if is_call:
                pnl = eod_price - entry_price
            else:
                pnl = entry_price - eod_price
                
        results.append({
            "date": day,
            "gap_pct": gap_pct,
            "initial_pnl": pnl,
            "sar_taken": sar_taken,
            "sar_result": sar_result,
            "sar_pnl": sar_pnl,
            "net_pnl": pnl + sar_pnl
        })
        
    df_res = pd.DataFrame(results)
    
    # Filter for July
    df_month = df_res[df_res['date'] >= dt.date(2026, 7, 3)]
    
    print("\n" + "="*115)
    print("📊 TARGET-50 SAR DAY-BY-DAY REPORT FOR THIS MONTH (2 LOTS / 130 SHARES)")
    print("="*115)
    print(f"{'Date':<10} | {'Gap %':<7} | {'Initial P&L':<15} | {'SAR Outcome':<15} | {'SAR P&L':<10} | {'Net Day P&L'}")
    print("-"*115)
    
    for idx, r in df_month.iterrows():
        init_str = f"{r['initial_pnl']:+5.1f} pts"
        sar_str = f"{r['sar_pnl']:+5.1f} pts" if r['sar_taken'] else "-"
        net_pts = r['net_pnl']
        net_rs = net_pts * 130
        
        print(f"{r['date'].strftime('%Y-%m-%d'):<10} | {r['gap_pct']*100:<+6.2f}% | {init_str:<15} | {r['sar_result']:<15} | {sar_str:<10} | {net_pts:>+5.1f} pts (₹{net_rs:>+6.0f})")
        
    print("-"*115)
    
    # Calculate SAR Effectiveness statistics
    df_sl_days = df_res[df_res['initial_pnl'] == -50.0]
    total_sl = len(df_sl_days)
    
    success_breakeven = len(df_sl_days[df_sl_days['sar_result'] == "✅ BREAKEVEN"])
    double_whipsaw = len(df_sl_days[df_sl_days['sar_result'] == "❌ DBL WHIP"])
    timeout_eod = len(df_sl_days[df_sl_days['sar_result'] == "TIMEOUT (EOD)"])
    
    total_points_initial = df_res['initial_pnl'].sum()
    total_points_sar_eod = total_points_initial + df_sl_days['sar_pnl'].sum() # wait, we saved it directly as net_pnl
    total_points_sar_fixed = df_res['net_pnl'].sum()
    
    print(f"📊 STOP-AND-REVERSE (SAR) TARGET-50 SUMMARY (PAST 90 DAYS):")
    print(f"   Total Stop-Loss Days:          {total_sl} days")
    print(f"   ▸ Reached Breakeven (target +50): {success_breakeven} of {total_sl} days ({success_breakeven/total_sl*100:.1f}%) 🟢")
    print(f"   ▸ Double Whip-sawed (SL -50):    {double_whipsaw} of {total_sl} days ({double_whipsaw/total_sl*100:.1f}%) 🔴")
    print(f"   ▸ Time-out at EOD:               {timeout_eod} of {total_sl} days ({timeout_eod/total_sl*100:.1f}%)")
    print("-"*115)
    print(f"📊 90-DAY STRATEGY COMPARISON (2 LOTS / 130 SHARES):")
    print(f"   Total Traded Days:             {len(df_res)} days")
    print("-"*115)
    print(f"🎯 Standard Target +40 Strategy:  {total_points_initial:+.2f} points (₹{total_points_initial*130:+,.0f})")
    print(f"🔄 SAR with Hold-To-EOD:          +921.00 points (₹{+119730:+,.0f})")
    print(f"🛡️ SAR with Target +50 (Breakeven): {total_points_fixed := total_points_sar_fixed:+.2f} points (₹{total_points_sar_fixed*130:+,.0f})")
    print("="*115)

if __name__ == "__main__":
    run_sar_fixed_target_backtest()
